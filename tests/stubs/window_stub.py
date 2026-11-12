"""A stand-in for the runner's window program, so the runner's tests never open a connection.

The runner runs it as (sys.executable, "-I", this file, *the six arguments of
strange_ringtone_runner.ytdlp.window_args). It refuses (exit 2) anything but
those six: three paths in the working directory named after one video ID,
then the start, the longest window and the largest file as plain numbers.
It also refuses an info JSON that is not the one tests/stubs/ytdlp_stub.py
writes for a --skip-download run, so the two children are tested as a pair.

The behaviour comes from STRANGE_RINGTONE_WINDOW_STUB when set, and
otherwise from the video ID (BY_ID below; any other ID is "ok"):

    ok          <id>.m4a (AUDIO_MAGIC, the ID and the start, or the bytes of
                the file STRANGE_RINGTONE_YTDLP_STUB_AUDIO names) and
                <id>.window.json: the window begins at the last multiple of
                ten seconds at or before the start and lasts the longest
                window allowed, or to the end of the video
    past_end    exit 3, nothing written; also what "ok" does for a start at
                or after the info JSON's duration
    blocked     exit 4, nothing written
    timed_out   exit 5, nothing written
    fail        exit 1, nothing written
    hang        sleep 60 s
    no_result   the audio only, exit 0
    no_audio    the result only, exit 0
    late        a result that begins after the start asked for
    early       a result that begins before second zero
    too_long    a result longer than the longest window allowed
    not_json    a result that is no JSON
    extra_key   a result with a third key
    oversize    audio of one byte more than the largest file allowed
    symlink     <id>.m4a is a symlink to a file outside the job directory
    files_1     both files as "ok", then exit 1
    loud        a flood on stdout and stderr, then both files as "ok"

STRANGE_RINGTONE_YTDLP_STUB_LOG, the yt-dlp stub's own variable, names a
file each run appends one JSON line to: argv, working directory, environment
and (on POSIX) resource limits. One file so lists a job's two children in the
order they ran.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

MODE_VAR = "STRANGE_RINGTONE_WINDOW_STUB"
# Shared with the yt-dlp stub: one log for both children, and one audio file
# for the development stack to set.
LOG_VAR = "STRANGE_RINGTONE_YTDLP_STUB_LOG"
AUDIO_VAR = "STRANGE_RINGTONE_YTDLP_STUB_AUDIO"
MODES = (
    "ok", "past_end", "blocked", "timed_out", "fail", "hang", "no_result", "no_audio", "late", "early", "too_long",
    "not_json", "extra_key", "oversize", "symlink", "files_1", "loud",
)
# Eleven characters each, so they pass the fetcher's ID check.
BY_ID = {
    "pastEnd____": "past_end",
    "winBlocked_": "blocked",
    "winTimeout_": "timed_out",
    "winFailing_": "fail",
    "winHanging_": "hang",
}
AUDIO_MAGIC = b"STUB-WINDOW\n"
# As tests/stubs/ytdlp_stub.py writes them for a --skip-download run.
WINDOW_URL = "https://rr1---sn-stub.googlevideo.com/videoplayback?id="
NAME_RE = re.compile(r"([A-Za-z0-9_-]{11})\.(info\.json|m4a|window\.json)", re.ASCII)
DIGITS_RE = re.compile(r"[0-9]{1,10}", re.ASCII)
LOUD_BYTES = 4 * 1024 * 1024


def refuse(message: str) -> None:
    sys.stderr.write(f"window stub: {message}\n")
    sys.exit(2)


def write_bytes(path: Path, data: bytes) -> None:
    handle = open(path, "wb")
    try:
        handle.write(data)
    finally:
        handle.close()


def parse(argv: list[str]) -> tuple[str, Path, Path, Path, int, int, int]:
    """(video ID, info, audio, result, start, max seconds, max bytes) from a valid argument list; exits 2 otherwise."""
    if len(argv) != 6:
        refuse("expected six arguments")
    paths = [Path(text) for text in argv[:3]]
    ids = set()
    for path, suffix in zip(paths, ("info.json", "m4a", "window.json"), strict=True):
        match = NAME_RE.fullmatch(path.name)
        if match is None or match[2] != suffix:
            refuse(f"argument {path.name!r} is not <id>.{suffix}")
            raise AssertionError  # unreachable; refuse() exits
        if not path.is_absolute() or not os.path.samefile(path.parent, os.getcwd()):
            refuse("every path must be absolute and lie in the working directory")
        ids.add(match[1])
    if len(ids) != 1:
        refuse("the three paths name different videos")
    for text in argv[3:]:
        if not DIGITS_RE.fullmatch(text):
            refuse(f"{text!r} is not a plain number")
    return ids.pop(), paths[0], paths[1], paths[2], int(argv[3]), int(argv[4]), int(argv[5])


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


def main(argv: list[str]) -> int:
    video_id, info_path, audio, result, start, max_seconds, max_bytes = parse(argv)
    mode = os.environ.get(MODE_VAR) or BY_ID.get(video_id, "ok")
    if mode not in MODES:
        refuse(f"unknown mode {mode!r}")
    log_run(argv)
    handle = open(info_path, "rb")
    try:
        info = json.load(handle)
    finally:
        handle.close()
    # The pair fits together: the address is the one the yt-dlp stub wrote
    # for this video, at the top level, with its protocol and extension.
    if not isinstance(info, dict) or info.get("url") != WINDOW_URL + video_id or info.get("protocol") != "https" \
            or info.get("ext") != "m4a" or not isinstance(info.get("http_headers"), dict):
        refuse("the info JSON is not the yt-dlp stub's for a --skip-download run")
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "blocked":
        return 4
    if mode == "timed_out":
        return 5
    if mode == "fail":
        sys.stderr.write("window: failed\n")
        return 1
    duration = float(info.get("duration") or 0)
    if mode == "past_end" or start >= duration:
        sys.stderr.write("window: the start lies at or after the end\n")
        return 3
    if mode == "loud":
        chunk = b"window stub: noise to fill the pipe\n" * 1024
        for stream in (sys.stdout.buffer, sys.stderr.buffer):
            written = 0
            while written < LOUD_BYTES:
                stream.write(chunk)
                written += len(chunk)
            stream.flush()

    begin = float(start - start % 10)
    answer: dict[str, object] = {"start": begin, "duration": round(min(float(max_seconds), duration - begin), 3)}
    if mode == "late":
        answer["start"] = float(start) + 5.0
    elif mode == "early":
        answer["start"] = -1.0
    elif mode == "too_long":
        answer["duration"] = float(max_seconds) + 5.0
    elif mode == "extra_key":
        answer["address"] = info["url"]
    if mode != "no_audio":
        if mode == "oversize":
            write_bytes(audio, b"\0" * (max_bytes + 1))
        elif mode == "symlink":
            target = audio.parent.parent / "outside.m4a"
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
                data = AUDIO_MAGIC + f"{video_id}@{start}".encode("ascii")
            write_bytes(audio, data)
    if mode != "no_result":
        write_bytes(result, b"{not json" if mode == "not_json" else json.dumps(answer).encode("ascii") + b"\n")
    return 1 if mode == "files_1" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
