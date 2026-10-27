"""A stand-in for yt-dlp, so the fetcher tests run anywhere with no network and no yt-dlp.

The fetcher runs it as (sys.executable, "-I", this file, *yt-dlp arguments).
It refuses (exit 2) any argument list that is not exactly what the fetcher
must send (strange_ringtone_fetch.ytdlp.ytdlp_args), so a loosened argument
fails the tests here too. The video ID is taken from the watch URL.

The behaviour comes from STRANGE_RINGTONE_YTDLP_STUB when set, and otherwise
from the video ID (BY_ID below; any other ID is "ok"). The fetcher builds the
child's environment from scratch, so tests that set the variable add it to
that environment on purpose; the ID table works with no help at all, which is
what tools/dev_fetch_stack.py relies on.

    ok            <id>.m4a and <id>.info.json: title "Stub Title <id> é",
                  duration 215.5, a 100-point heatmap, and keys that must
                  never be passed on. The title is printed on stdout and
                  stderr, as yt-dlp prints the destination filename.
    too_long      exit 101, nothing written (a --break-match-filters reject)
    blocked       exit 1, "Sign in to confirm you're not a bot" on stderr
    not_found     exit 1, "Video unavailable" on stderr
    fail          exit 1
    oversize      writes --max-filesize + 1 bytes of audio, then exits 0
    no_audio      the info JSON only, exit 0 (what --max-filesize does)
    ctrl_title    a title full of control and format characters
    huge_heatmap  201 heatmap points
    bad_heatmap   one heatmap value above 1
    bare_info     an info JSON with neither title, duration nor heatmap
    not_a_dict    an info JSON that is a list
    symlink       <id>.m4a is a symlink to a file outside the job directory
    files_101     both files as "ok", then exit 101
    files_1       both files as "ok", then exit 1
    hang          sleep 60 s
    grandchild    start a sleeping grandchild, write its PID to grandchild.pid
                  in the working directory, then sleep 60 s

STRANGE_RINGTONE_YTDLP_STUB_LOG names a file each run appends one JSON line
to: argv, working directory, environment and (on POSIX) resource limits.
STRANGE_RINGTONE_YTDLP_STUB_AUDIO names a file whose bytes "ok" writes as
the audio, and STRANGE_RINGTONE_YTDLP_STUB_DURATION overrides the duration,
so the development stack can hand the browser something it can decode.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MODE_VAR = "STRANGE_RINGTONE_YTDLP_STUB"
LOG_VAR = "STRANGE_RINGTONE_YTDLP_STUB_LOG"
AUDIO_VAR = "STRANGE_RINGTONE_YTDLP_STUB_AUDIO"
DURATION_VAR = "STRANGE_RINGTONE_YTDLP_STUB_DURATION"
MODES = (
    "ok", "too_long", "blocked", "not_found", "fail", "oversize", "no_audio", "ctrl_title", "huge_heatmap",
    "bad_heatmap", "bare_info", "not_a_dict", "symlink", "files_101", "files_1", "hang", "grandchild",
)
# Eleven characters each, so they pass the fetcher's ID check.
BY_ID = {
    "tooLong____": "too_long",
    "blocked____": "blocked",
    "notFound___": "not_found",
    "failing____": "fail",
    "oversize___": "oversize",
    "noAudio____": "no_audio",
    "ctrlTitle__": "ctrl_title",
    "hugeHeatmap": "huge_heatmap",
    "badHeatmap_": "bad_heatmap",
    "bareInfo___": "bare_info",
    "notADict___": "not_a_dict",
    "symlinked__": "symlink",
    "tooLongFile": "files_101",
    "failedFiles": "files_1",
    "hanging____": "hang",
    "grandchild_": "grandchild",
}
AUDIO_MAGIC = b"STUB-M4A\n"
DURATION = 215.5
HEATMAP_POINTS = 100
URL_RE = re.compile(r"https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})", re.ASCII)
FILTER_RE = re.compile(r"duration <= [0-9]+ & !is_live", re.ASCII)
DIGITS_RE = re.compile(r"[0-9]+", re.ASCII)

# The pinned list, with None where the value depends on the run.
EXPECTED = [
    "--ignore-config", "--no-playlist", "--use-extractors", "youtube",
    "--js-runtimes", "deno:/usr/local/bin/deno", "--no-cache-dir",
    "-f", "bestaudio[ext=m4a]",
    "--break-match-filters", None,
    "--max-filesize", None,
    "--write-info-json", "--quiet", "--no-warnings", "--no-progress",
    "--socket-timeout", "20",
    "-P", None,
    "-o", "%(id)s.%(ext)s",
    "--", None,
]


def refuse(message: str) -> None:
    sys.stderr.write(f"yt-dlp stub: {message}\n")
    sys.exit(2)


def parse(argv: list[str]) -> tuple[str, int, Path]:
    """(video ID, max bytes, output directory) from a valid argument list; exits 2 otherwise."""
    if len(argv) != len(EXPECTED):
        refuse("wrong number of arguments")
    for index, (expected, actual) in enumerate(zip(EXPECTED, argv, strict=True)):
        if expected is not None and actual != expected:
            refuse(f"argument {index} must be {expected!r}")
    if not FILTER_RE.fullmatch(argv[10]):
        refuse("unexpected --break-match-filters value")
    if not DIGITS_RE.fullmatch(argv[12]):
        refuse("--max-filesize must be a number of bytes")
    directory = Path(argv[20])
    if not directory.is_dir() or not os.path.samefile(directory, os.getcwd()):
        refuse("-P must be the working directory")
    match = URL_RE.fullmatch(argv[24])
    if match is None:
        refuse("the URL must be a plain watch URL")
        raise AssertionError  # unreachable; refuse() exits
    return match[1], int(argv[12]), directory


def log_run(argv: list[str]) -> None:
    path = os.environ.get(LOG_VAR)
    if not path:
        return
    entry: dict[str, object] = {"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}
    try:
        import resource
    except ImportError:
        pass
    else:
        entry["limits"] = {
            name: resource.getrlimit(getattr(resource, name))[0]
            for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_NPROC")
        }
    handle = open(path, "a", encoding="utf-8")
    try:
        handle.write(json.dumps(entry) + "\n")
    finally:
        handle.close()


def title_for(video_id: str) -> str:
    return f"Stub Title {video_id} é"


def heatmap(duration: float, points: int) -> list[dict[str, float]]:
    step = duration / points
    # A bump around 60 % of the way in, so the suggestion has somewhere to go.
    return [
        {
            "start_time": round(i * step, 3),
            "end_time": round(min((i + 1) * step, duration), 3),
            "value": round(max(0.0, 1.0 - abs(i / points - 0.6) * 4), 3),
        }
        for i in range(points)
    ]


def write_bytes(path: Path, data: bytes) -> None:
    handle = open(path, "wb")
    try:
        handle.write(data)
    finally:
        handle.close()


def main(argv: list[str]) -> int:
    video_id, max_bytes, directory = parse(argv)
    mode = os.environ.get(MODE_VAR) or BY_ID.get(video_id, "ok")
    if mode not in MODES:
        refuse(f"unknown mode {mode!r}")
    log_run(argv)
    title = title_for(video_id)
    # yt-dlp prints the destination filename and errors that quote the title;
    # the fetcher must never pass either on.
    sys.stdout.write(f"[download] Destination: {title}.m4a\n")
    sys.stdout.flush()
    sys.stderr.write(f"stub: {title}\n")
    sys.stderr.flush()
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "grandchild":
        # Inherits stdout and stderr, like anything yt-dlp starts (Deno): the
        # fetcher must not hang on pipes this process keeps open.
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        Path("grandchild.pid.tmp").write_text(str(child.pid), encoding="ascii")
        os.replace("grandchild.pid.tmp", "grandchild.pid")
        time.sleep(60)
        return 0
    if mode == "too_long":
        return 101
    if mode == "blocked":
        sys.stderr.write(f"ERROR: [youtube] {video_id}: Sign in to confirm you're not a bot\n")
        return 1
    if mode == "not_found":
        sys.stderr.write(f"ERROR: [youtube] {video_id}: Video unavailable\n")
        return 1
    if mode == "fail":
        sys.stderr.write("ERROR: something else went wrong\n")
        return 1

    duration: float = float(os.environ.get(DURATION_VAR) or DURATION)
    info: object = {
        "id": video_id,
        "title": title,
        "duration": duration,
        "heatmap": heatmap(duration, HEATMAP_POINTS),
        # Never to be passed on.
        "uploader": "Stub Uploader",
        "description": "stub description",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "formats": [{"format_id": "140", "url": "https://example.internal/audio"}],
    }
    assert isinstance(info, dict)
    if mode == "ctrl_title":
        info["title"] = "  Bad\x00\x07 ‮Title​\n\tline two﻿  "
    elif mode == "huge_heatmap":
        info["heatmap"] = heatmap(duration, 201)
    elif mode == "bad_heatmap":
        info["heatmap"][3]["value"] = 1.5
    elif mode == "bare_info":
        info = {"id": video_id}
    elif mode == "not_a_dict":
        info = [title]
    info_path = directory / f"{video_id}.info.json"
    write_bytes(info_path, json.dumps(info).encode("utf-8"))
    if mode == "no_audio":
        return 0
    audio = directory / f"{video_id}.m4a"
    if mode == "oversize":
        write_bytes(audio, b"\0" * (max_bytes + 1))
    elif mode == "symlink":
        target = directory.parent / "outside.m4a"
        write_bytes(target, AUDIO_MAGIC)
        os.symlink(target, audio)
    else:
        source = os.environ.get(AUDIO_VAR)
        if source:
            handle = open(source, "rb")
            try:
                data = handle.read()
            finally:
                handle.close()
        else:
            data = AUDIO_MAGIC + video_id.encode("ascii")
        write_bytes(audio, data)
    # A failed run that still left both files behind: the exit status wins.
    if mode == "files_101":
        return 101
    if mode == "files_1":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
