"""Run yt-dlp for one validated video ID and read back only what the fetcher keeps (docs/PLAN.md 6).

yt-dlp is the only code in the project that talks to the internet, so every
run is fenced in like the core's ffmpeg (strange_ringtone.render):

- an argument list, never a shell. The only value from a request in it is the
  video ID, checked against VIDEO_ID_RE before it gets here, inside a URL this
  module builds itself; no URL a user typed ever reaches yt-dlp;
- the util-linux prlimit binary in front of it on Linux (CPU seconds, file
  size, open files, processes). No address-space limit: Deno, which yt-dlp
  runs for YouTube's player JavaScript, reserves a huge virtual range at
  startup and would die under RLIMIT_AS. The deployment's cgroup limit bounds
  memory instead;
- its own session, so a wall-clock timeout kills the whole process group,
  Deno included;
- an environment built from scratch: the child sees fixed values and the
  proxy settings, never the server's own environment;
- a job directory the caller created, holding only names yt-dlp derives from
  the validated ID.

yt-dlp's stdout and stderr never leave this module and are never logged:
they carry the destination filename and the title. stderr is only matched
against a few fixed phrases to choose an error code.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fixed paths, never taken from a setting or a request: what the image ships
# (fetcher/Dockerfile).
PRLIMIT = "/usr/bin/prlimit"
DENO = "/usr/local/bin/deno"
# sys.executable is the interpreter running the server, which -I keeps from
# reading PYTHON* variables, the user site or the working directory.
DEFAULT_COMMAND: tuple[str, ...] = (sys.executable, "-I", "-m", "yt_dlp")

# Exactly an 11-character YouTube video ID. re.ASCII and explicit ranges: no
# other script's letters or digits, no '.', '%' or '/'. fullmatch, so a
# trailing newline is refused too.
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}", re.ASCII)
WATCH_URL = "https://www.youtube.com/watch?v="

ERROR_CODES = ("blocked", "too_long", "not_found", "timeout", "failed")
# yt-dlp's exit status when a --break-match-filters filter rejects the video
# (RejectedVideoReached, a DownloadCancelled): too long, or live.
EXIT_REJECTED = 101
# Phrases yt-dlp prints when YouTube refuses this client. Matched without
# regard to case, and only to pick an error code.
_BLOCKED_RE = re.compile(
    r"sign in to confirm|not a bot|captcha|po token|http error 403|this content isn.t available", re.IGNORECASE
)
_NOT_FOUND_RE = re.compile(r"video unavailable|private video|has been removed|does not exist", re.IGNORECASE)

# How long to wait for the pipes after a timeout kill (see render.py).
DRAIN_SECONDS = 5.0
SOCKET_TIMEOUT_SECONDS = 20
# A real info JSON is a few hundred kB; this bounds what json.load reads.
INFO_MAX_BYTES = 4 * 1024 * 1024
TITLE_MAX_CHARS = 200
HEATMAP_MAX_POINTS = 200
# yt-dlp's duration is whole seconds, while the last heatmap point ends at the
# exact length, so it may run past the duration by a fraction of a second.
HEATMAP_TOLERANCE_SECONDS = 1.0
# Unicode categories removed from the title: control, format (bidi overrides,
# zero-width characters), surrogate, private use and unassigned.
_TITLE_DROP = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"


class FetchError(Exception):
    """yt-dlp did not produce a usable result. code is one of ERROR_CODES.

    Deliberately carries nothing yt-dlp printed.
    """

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown fetch error code {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Limits:
    """Per-run resource limits, applied by prlimit before yt-dlp starts."""

    # CPU time, as a backstop to the wall-clock timeout.
    cpu_seconds: int = 120
    # The audio may be at most FETCH_MAX_BYTES; the margin covers the info
    # JSON and Deno's cache. A larger write kills the run with SIGXFSZ.
    file_size_bytes: int = 40 * 1024 * 1024 + 1024 * 1024
    # yt-dlp, Deno and V8 open a few dozen files between them.
    open_files: int = 256
    # RLIMIT_NPROC counts every thread of UID 10001 in the container: the
    # server's workers, yt-dlp, and Deno's V8 threads.
    processes: int = 512


@dataclass(frozen=True)
class Metadata:
    """The only values of the info JSON that ever leave the fetcher."""

    title: str | None
    duration: int | float | None
    heatmap: tuple[dict[str, float], ...] | None


@dataclass(frozen=True)
class Download:
    audio: Path
    metadata: Metadata


def default_prlimit() -> tuple[str, ...] | None:
    """prlimit on Linux, always; nothing where it doesn't exist (Windows development).

    Not conditional on the file being there: on Linux a missing prlimit fails
    every fetch instead of silently running yt-dlp without limits.
    """
    return (PRLIMIT,) if sys.platform.startswith("linux") else None


def watch_url(video_id: str) -> str:
    """The only URL yt-dlp is ever given, built from a validated ID."""
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("not a video ID")
    return WATCH_URL + video_id


def ytdlp_args(video_id: str, job_dir: Path, max_seconds: int, max_bytes: int) -> list[str]:
    """yt-dlp's arguments, after the program itself."""
    return [
        # No configuration file from anywhere: what runs is exactly this list.
        "--ignore-config",
        "--no-playlist",
        # Only the YouTube extractor, never the generic one, which would fetch
        # whatever a page pointed it to.
        "--use-extractors", "youtube",
        "--js-runtimes", f"deno:{DENO}",
        # yt-dlp's cache would sit in HOME, which is the job directory anyway;
        # nothing is meant to carry over from one job to the next.
        "--no-cache-dir",
        # AAC in MP4, which Safari decodes, so the fetcher needs no ffmpeg.
        "-f", "bestaudio[ext=m4a]",
        # A breaking filter, not --match-filters: a plain filter skips the
        # video and exits 0 having written nothing, while this one exits 101,
        # which is how too_long is told apart from other failures.
        "--break-match-filters", f"duration <= {max_seconds} & !is_live",
        "--max-filesize", str(max_bytes),
        "--write-info-json",
        "--quiet", "--no-warnings", "--no-progress",
        "--socket-timeout", str(SOCKET_TIMEOUT_SECONDS),
        "-P", str(job_dir),
        # Names derived from the ID only: the title never becomes a path.
        "-o", "%(id)s.%(ext)s",
        # Everything after -- is a URL, never an option.
        "--",
        watch_url(video_id),
    ]


def map_failure(returncode: int, stderr: bytes) -> str:
    """The error code for a run that exited with returncode and did not produce a result."""
    if returncode == EXIT_REJECTED:
        return "too_long"
    if returncode != 0:
        text = stderr.decode("utf-8", errors="replace")
        if _BLOCKED_RE.search(text):
            return "blocked"
        if _NOT_FOUND_RE.search(text):
            return "not_found"
    # Exit 0 without the audio is --max-filesize refusing a large file, which
    # yt-dlp does not treat as an error; anything else is failed too.
    return "failed"


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
    number = float(value)
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


def _open_regular(path: Path, max_bytes: int) -> tuple[int, int] | None:
    """(fd, size) for a regular file of 1..max_bytes at path, never through a symlink; or None."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        # lstat first for Windows, which has no O_NOFOLLOW.
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= max_bytes:
        os.close(fd)
        return None
    return fd, info.st_size


def read_result(job_dir: Path, video_id: str, max_bytes: int) -> Download:
    """The audio and metadata of a run that exited 0, or FetchError("failed").

    Success is exactly <id>.m4a and <id>.info.json in the job directory, both
    regular files within their size limits; exit 0 alone proves nothing.
    """
    audio = job_dir / f"{video_id}.m4a"
    opened = _open_regular(audio, max_bytes)
    if opened is None:
        raise FetchError("failed")
    os.close(opened[0])
    info_path = job_dir / f"{video_id}.info.json"
    opened = _open_regular(info_path, INFO_MAX_BYTES)
    if opened is None:
        raise FetchError("failed")
    fd, _size = opened
    try:
        # Closed by the with-block before anything deletes the directory,
        # because Windows can't delete an open file.
        with open(fd, "rb") as handle:
            info = json.load(handle)
    except (ValueError, RecursionError, OSError):
        raise FetchError("failed") from None
    return Download(audio=audio, metadata=parse_info(info))


class YtDlp:
    def __init__(
        self,
        max_seconds: int,
        max_bytes: int,
        timeout_seconds: int,
        command: tuple[str, ...] = DEFAULT_COMMAND,
        prlimit: tuple[str, ...] | None = None,
        limits: Limits | None = None,
        proxies: tuple[tuple[str, str], ...] = (),
    ) -> None:
        # Nothing here touches the filesystem, so a checkout on Windows starts
        # without yt-dlp or Deno and only a fetch fails.
        if not command:
            raise ValueError("command must name a program")
        if max_seconds < 1 or max_bytes < 1 or timeout_seconds < 1:
            raise ValueError("max_seconds, max_bytes and timeout_seconds must be at least 1")
        self.command = tuple(command)
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.prlimit = tuple(prlimit) if prlimit is not None else None
        self.limits = (
            limits
            if limits is not None
            else Limits(cpu_seconds=timeout_seconds, file_size_bytes=max_bytes + 1024 * 1024)
        )
        self.proxies = tuple(proxies)
        self._lock = threading.Lock()
        self._running: set[subprocess.Popen[bytes]] = set()

    def full_command(self, args: list[str]) -> list[str]:
        """prlimit (when configured), then the yt-dlp command, then args."""
        prefix: list[str] = []
        if self.prlimit is not None:
            limits = self.limits
            prefix = [
                *self.prlimit,
                f"--cpu={limits.cpu_seconds}",
                f"--fsize={limits.file_size_bytes}",
                f"--nofile={limits.open_files}",
                f"--nproc={limits.processes}",
                # Everything after -- is the program and its arguments.
                "--",
            ]
        return [*prefix, *self.command, *args]

    def child_env(self, job_dir: Path) -> dict[str, str]:
        """The child's whole environment: fixed values plus the operator's proxy settings.

        yt-dlp hands its own environment to Deno, so this is everything either
        of them sees. A token or other secret in the server's environment
        never reaches code that talks to the internet.
        """
        env = {
            "PATH": _CHILD_PATH,
            # Anything that insists on a home or cache directory writes inside
            # the job directory, which is removed after the run.
            "HOME": str(job_dir),
            "DENO_DIR": str(job_dir / "deno"),
            "DENO_NO_UPDATE_CHECK": "1",
            "NO_COLOR": "1",
            "LC_ALL": "C.UTF-8",
        }
        env.update(self.proxies)
        if sys.platform == "win32":
            # Development only: Python on Windows can't start without
            # SYSTEMROOT (it needs it to reach the system's random source).
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return env

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        """Download video_id's audio into job_dir, or raise FetchError. The caller removes job_dir."""
        command = self.full_command(ytdlp_args(video_id, job_dir, self.max_seconds, self.max_bytes))
        try:
            # Safe by construction: an argument list with no shell, a program
            # fixed in code (DEFAULT_COMMAND or PRLIMIT), arguments that are
            # constants, validated numbers, a server-created directory and a
            # URL built from an ID that matched VIDEO_ID_RE, an environment
            # built from scratch, and a wall-clock timeout below.
            # tests/test_fetcher_ytdlp.py pins each of these guards.
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job_dir,
                env=self.child_env(job_dir),
                # Its own session and process group, so a timeout can kill
                # everything yt-dlp started, Deno included.
                start_new_session=True,
            )
        except OSError:
            # Missing interpreter, missing prlimit, or no resources to start it.
            raise FetchError("failed") from None
        with self._lock:
            self._running.add(proc)
        try:
            try:
                # stdout is read only to keep the pipe from filling; neither
                # stream is kept or logged.
                _stdout, stderr = proc.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    # Drain, not wait(): the pipes must be read to EOF to free
                    # them, and the bound keeps a survivor from hanging us.
                    proc.communicate(timeout=DRAIN_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                finally:
                    _close_pipes(proc)
                raise FetchError("timeout") from None
            except BaseException:
                try:
                    _kill_group(proc)
                    proc.wait()
                finally:
                    _close_pipes(proc)
                raise
        finally:
            with self._lock:
                self._running.discard(proc)
        if proc.returncode != 0:
            raise FetchError(map_failure(proc.returncode, stderr))
        return read_result(job_dir, video_id, self.max_bytes)

    def cancel(self) -> None:
        """Kill every running yt-dlp and what it started; used when the server stops."""
        with self._lock:
            running = list(self._running)
        for proc in running:
            _kill_group(proc)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            # start_new_session made the child the leader of a new group whose
            # ID is its PID. Using the PID, not getpgid(), still reaches the
            # group's other members when the leader has already exited.
            killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    # Windows has no process groups to signal; TerminateProcess on the child
    # is the best it offers.
    try:
        proc.kill()
    except OSError:
        pass


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for name in ("stdout", "stderr"):
        pipe = getattr(proc, name)
        if pipe is None or pipe.closed:
            continue
        # On Windows communicate() reads through helper threads that hold the
        # pipe's lock while blocked; such a thread closes its pipe itself at
        # EOF, so a live one is left alone (see strange_ringtone.render).
        reader = getattr(proc, f"{name}_thread", None)
        if reader is not None and reader.is_alive():
            continue
        try:
            pipe.close()
        except OSError:
            pass
