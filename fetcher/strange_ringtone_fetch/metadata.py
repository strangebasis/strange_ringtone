"""The fetch result's types and the metadata whitelist, shared by the fetcher and its runner (docs/PLAN.md 6.1).

The runner runs yt-dlp and reads its info JSON; the fetcher receives what the
runner answers and hands it to the page. Both apply the one whitelist in this
module: the runner before it answers (metadata_to_info(parse_info(info))),
and the fetcher again on what arrives (parse_info), because the fetcher
trusts nothing the runner sends. Only a title, a duration and a heatmap ever
pass, each cleaned and bounded here.

This module imports nothing but the stdlib, so the runner's process and the
fetcher's can both load it without pulling in the other's code.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Exactly an 11-character YouTube video ID. re.ASCII and explicit ranges: no
# other script's letters or digits, no '.', '%' or '/'. fullmatch, so a
# trailing newline is refused too.
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}", re.ASCII)

ERROR_CODES = ("blocked", "too_long", "not_found", "timeout", "failed")
TITLE_MAX_CHARS = 200
HEATMAP_MAX_POINTS = 200
# yt-dlp's duration is whole seconds, while the last heatmap point ends at the
# exact length, so it may run past the duration by a fraction of a second.
HEATMAP_TOLERANCE_SECONDS = 1.0
# Unicode categories removed from the title: control, format (bidi overrides,
# zero-width characters), surrogate, private use and unassigned.
_TITLE_DROP = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


class FetchError(Exception):
    """A fetch did not produce a usable result. code is one of ERROR_CODES.

    Deliberately carries nothing yt-dlp printed, so neither the runner's
    answer nor the fetcher's status JSON can repeat it.
    """

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown fetch error code {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Metadata:
    """The only values of the info JSON that ever leave the runner, and then the fetcher."""

    title: str | None
    duration: int | float | None
    heatmap: tuple[dict[str, float], ...] | None


@dataclass(frozen=True)
class Download:
    audio: Path
    metadata: Metadata


def clean_title(value: Any) -> str | None:
    """The title as the page may show it, or None."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value)
    # Whitespace first, so a tab or newline (both Cc) still separates words.
    # split() with no argument also splits on Unicode line and paragraph
    # separators, so the title is always one line.
    text = " ".join(text.split())
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _TITLE_DROP)
    text = " ".join(text.split())
    text = text[:TITLE_MAX_CHARS].strip()
    return text or None


def _number(value: Any) -> float | None:
    # bool is an int subclass; JSON true is not a number here.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        # JSON integers have no size limit, and float() of one beyond the
        # float range raises instead of returning infinity. Such a number is
        # invalid exactly like NaN or infinity: no number, never an
        # exception that the callers' except tuples don't name.
        return None
    return number if math.isfinite(number) else None


def clean_duration(value: Any) -> int | float | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return value if isinstance(value, int) else number


def clean_heatmap(value: Any, duration: int | float | None) -> tuple[dict[str, float], ...] | None:
    """The heatmap re-emitted as {start, end, value} points, or None if anything about it is off.

    Never truncated or partly kept: a list that is too long or holds one bad
    point is dropped whole, and the page falls back to its loudness rule.
    Only a point that lies wholly in the tolerated overrun past the duration
    is left out, because it covers none of the audio.
    """
    if duration is None or not isinstance(value, list) or not 1 <= len(value) <= HEATMAP_MAX_POINTS:
        return None
    limit = float(duration) + HEATMAP_TOLERANCE_SECONDS
    points: list[dict[str, float]] = []
    for point in value:
        if not isinstance(point, dict):
            return None
        start = _number(point.get("start_time"))
        end = _number(point.get("end_time"))
        score = _number(point.get("value"))
        if start is None or end is None or score is None:
            return None
        # start < end, as the page's validHeatmap (core/static/audio/suggest.js)
        # requires: one zero-length point there turns rule 2 off for the
        # whole list, so such a list is refused here instead of sent.
        if not (0 <= start < end <= limit and 0 <= score <= 1):
            return None
        # Within the duration exactly, as the status JSON promises. A point
        # that starts at or after the whole-second duration (the overrun
        # HEATMAP_TOLERANCE_SECONDS allows) covers none of it and would come
        # out zero-length, so it is left out; it carries no weight anyway.
        end = min(end, float(duration))
        if end <= start:
            continue
        points.append({"start": start, "end": end, "value": score})
    return tuple(points) or None


def parse_info(info: Any) -> Metadata:
    """Keep title, duration and heatmap from a parsed info JSON; nothing else."""
    if not isinstance(info, dict):
        raise FetchError("failed")
    duration = clean_duration(info.get("duration"))
    return Metadata(
        title=clean_title(info.get("title")),
        duration=duration,
        heatmap=clean_heatmap(info.get("heatmap"), duration),
    )


def metadata_to_info(metadata: Metadata) -> dict[str, Any]:
    """The cleaned metadata in yt-dlp's key names, so parse_info reads it back unchanged.

    The runner answers the fetcher with this (docs/PLAN.md 6.1), and the
    fetcher runs parse_info on it again: one whitelist, applied on both sides,
    and no second validator to drift from the first. Keys whose value is None
    are left out; a heatmap point {start, end, value} goes back to yt-dlp's
    {start_time, end_time, value}.
    """
    info: dict[str, Any] = {}
    # Only these three keys: anything else yt-dlp wrote stays in the runner,
    # because parse_info never kept it.
    if metadata.title is not None:
        info["title"] = metadata.title
    if metadata.duration is not None:
        info["duration"] = metadata.duration
    if metadata.heatmap is not None:
        # A list, not the tuple Metadata holds: it goes out as JSON. Already
        # clipped to the duration and free of empty points, so parse_info
        # reads every point back as it is.
        info["heatmap"] = [
            {"start_time": point["start"], "end_time": point["end"], "value": point["value"]}
            for point in metadata.heatmap
        ]
    return info
