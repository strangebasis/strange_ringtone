"""A stand-in for the image's ffmpeg, so the render tests run anywhere without ffmpeg.

The renderer runs it as (sys.executable, "-I", this file, *ffmpeg arguments).
It refuses (exit 2) any argument list that is not exactly what the renderer
must send (docs/PLAN.md 5.3), so a loosened argument fails the tests here too.
Its loudness comes from its argv, as if the upload measured -23 LUFS: the
measure pass reports -23.0 and a render pass -23.0 plus the volume gain. The
summary on stderr is f_ebur128.c's text exactly, with per-frame lines before
it that carry a different I value, so a parser not anchored on "Summary:"
reads the wrong number.

STRANGE_RINGTONE_FFMPEG_STUB selects a behaviour:
    ok          (default) as above
    offset      every render pass reports 1 LU under the gain's promise, so
                the corrective pass is needed and lands
    stubborn    every render pass reports -30.0 whatever the gain
    silence     the measure pass reports I -70.0 and peak -inf
    unparsable  no summary on stderr, exit 0 (a render pass still writes out.m4a)
    fail        exit 1 without output
    late_fail   everything ok does (summary, and out.m4a on a render pass),
                then exit 1, so only the exit status says the run failed
    hang        sleep 60 s
    grandchild  start a sleeping grandchild, write its PID to grandchild.pid in
                the working directory, then sleep 60 s
    survivor    everything ok does, then start a sleeping grandchild in its
                own process group with its standard streams on the null
                device, append its PID to survivors.pid in the working
                directory, and exit 0: a child left behind by a run that
                succeeded, which the pipes alone never reveal
    flood       write FLOOD_BYTES to stdout and as many to stderr, in
                alternating chunks, then everything ok does: a run whose
                output would fill memory if it were kept, and which blocks
                on a full pipe unless both are read at once
STRANGE_RINGTONE_FFMPEG_STUB_LOG, when set, names a file that each run appends
one JSON line to: its argv, working directory and (on POSIX) resource limits.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MODE_VAR = "STRANGE_RINGTONE_FFMPEG_STUB"
LOG_VAR = "STRANGE_RINGTONE_FFMPEG_STUB_LOG"
MODES = (
    "ok", "offset", "stubborn", "silence", "unparsable", "fail", "late_fail", "hang", "grandchild", "survivor", "flood",
)
# What "flood" writes to each stream: far more than any bound on what the
# renderer keeps, and more than a pipe's buffer many times over.
FLOOD_BYTES = 24 * 1024 * 1024
FLOOD_CHUNK = 64 * 1024
INPUT_LUFS = -23.0
INPUT_PEAK = -20.0
OUTPUT_MAGIC = b"STUB-IPOD\n"

COMMON = ["-nostdin", "-hide_banner", "-nostats", "-threads", "1", "-protocol_whitelist", "file"]
MEASURE_TAIL = ["-af", "ebur128=peak=true", "-f", "null", "-"]
CHAIN_RE = re.compile(
    r"volume=(-?[0-9]+\.[0-9]{2})dB"
    r",alimiter=limit=0\.8414:level=false:latency=true"
    r"(?:,afade=t=in:st=0:d=[0-9]+\.[0-9]{3}:curve=qsin)?"
    r"(?:,afade=t=out:st=[0-9]+\.[0-9]{3}:d=[0-9]+\.[0-9]{3}:curve=qsin)?"
    r",ebur128=peak=true"
)


def refuse(message: str) -> None:
    sys.stderr.write(f"ffmpeg stub: {message}\n")
    sys.exit(2)


def parse(argv: list[str]) -> tuple[str, Path, Path | None, float, str]:
    """(kind, input, output, gain, title) from a valid argument list; exits 2 otherwise."""
    if argv[: len(COMMON)] != COMMON:
        refuse(f"arguments must start with {COMMON}")
    rest = argv[len(COMMON):]
    render = rest[:1] == ["-y"]
    if render:
        rest = rest[1:]
    if rest[:3] != ["-f", "wav", "-i"] or len(rest) < 4:
        refuse("expected -f wav -i <input>")
    source = Path(rest[3])
    if source.name != "in.wav" or not source.is_file():
        refuse("the input must be an existing in.wav")
    tail = rest[4:]
    if not render:
        if tail != MEASURE_TAIL:
            refuse(f"a measure pass ends with {MEASURE_TAIL}")
        return "measure", source, None, 0.0, ""
    expected = ["-af", None, "-ar", "44100", "-map_metadata", "-1", "-fflags", "+bitexact", "-metadata", None,
                "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "ipod", None]
    if len(tail) != len(expected) or any(e is not None and e != t for e, t in zip(expected, tail, strict=True)):
        refuse("unexpected render arguments")
    match = CHAIN_RE.fullmatch(tail[1])
    if match is None:
        refuse("unexpected filter chain")
        raise AssertionError  # unreachable; refuse() exits
    if not tail[9].startswith("title="):
        refuse("-metadata must set the title")
    output = Path(tail[18])
    if output.name != "out.m4a" or output.parent != source.parent:
        refuse("the output must be out.m4a next to in.wav")
    return "render", source, output, float(match[1]), tail[9][len("title="):]


def log_run(argv: list[str]) -> None:
    path = os.environ.get(LOG_VAR)
    if not path:
        return
    entry: dict[str, object] = {"argv": argv, "cwd": os.getcwd()}
    try:
        import resource
    except ImportError:
        pass
    else:
        entry["limits"] = {
            name: resource.getrlimit(getattr(resource, name))[0]
            for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE")
        }
    handle = open(path, "a", encoding="utf-8")
    try:
        handle.write(json.dumps(entry) + "\n")
    finally:
        handle.close()


def summary(integrated: float, peak: float) -> str:
    # f_ebur128.c's per-frame line and summary, %5.1f included. The frame lines
    # report a different running I on purpose.
    frames = "".join(
        f"[Parsed_ebur128_4 @ 0x5581d1e0c040] t: {t / 10:<10.6g} TARGET:-23 LUFS    M: -99.0 S:-120.7     "
        f"I: -99.0 LUFS       LRA:   0.0 LU  FTPK: -99.0 dBFS  TPK: -99.0 dBFS\n"
        for t in range(1, 4)
    )
    return (
        frames
        + "[Parsed_ebur128_4 @ 0x5581d1e0c040] Summary:\n"
        "\n"
        "  Integrated loudness:\n"
        f"    I:         {integrated:5.1f} LUFS\n"
        f"    Threshold: {integrated - 10:5.1f} LUFS\n"
        "\n"
        "  Loudness range:\n"
        f"    LRA:       {0.0:5.1f} LU\n"
        f"    Threshold: {0.0:5.1f} LUFS\n"
        f"    LRA low:   {0.0:5.1f} LUFS\n"
        f"    LRA high:  {0.0:5.1f} LUFS\n"
        "\n"
        "  True peak:\n"
        f"    Peak:      {peak:5.1f} dBFS\n"
        "[out#0/null @ 0x5581d1e0d000] video:0KiB audio:345KiB subtitle:0KiB other streams:0KiB\n"
    )


def main(argv: list[str]) -> int:
    mode = os.environ.get(MODE_VAR, "ok")
    if mode not in MODES:
        refuse(f"unknown mode {mode!r}")
    kind, _source, output, gain, title = parse(argv)
    log_run(argv)
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "grandchild":
        # Inherits stdout and stderr, like anything ffmpeg could start: the
        # renderer must not hang on pipes this process keeps open.
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        # Written aside and renamed into place, so a test that sees
        # grandchild.pid never reads it empty or half-written.
        Path("grandchild.pid.tmp").write_text(str(child.pid), encoding="ascii")
        os.replace("grandchild.pid.tmp", "grandchild.pid")
        time.sleep(60)
        return 0
    if mode == "fail":
        sys.stderr.write("ffmpeg stub: failing as asked\n")
        return 1
    if mode == "flood":
        flood()
    err = sys.stderr.buffer
    # Real ffmpeg echoes the output's metadata, title included, on stderr; the
    # renderer must never pass that on.
    err.write(f"  Metadata:\n    title           : {title}\n".encode())
    if kind == "render":
        assert output is not None
        output.write_bytes(OUTPUT_MAGIC + json.dumps({"title": title, "gain": gain}).encode("utf-8"))
    if mode == "unparsable":
        err.write(b"[Parsed_ebur128_0 @ 0x1] Summary: nothing to see\n")
        return 0
    base = -70.0 if mode == "silence" else INPUT_LUFS
    peak = float("-inf") if mode == "silence" else INPUT_PEAK
    if kind == "measure":
        integrated = base
    elif mode == "stubborn":
        integrated = -30.0
    else:
        integrated = base + gain - (1.0 if mode == "offset" else 0.0)
        peak = min(peak + gain, -1.5)
    err.write(summary(integrated, peak).encode("ascii"))
    err.flush()
    if mode == "survivor":
        leave_survivor()
    return 1 if mode == "late_fail" else 0


def flood() -> None:
    # Alternating, so a reader that drains one stream to its end before it
    # reads the other leaves this process blocked on the full second pipe.
    line = b"[Parsed_ebur128_4 @ 0x5581d1e0c040] flood of output nobody needs\n"
    chunk = (line * (FLOOD_CHUNK // len(line) + 1))[:FLOOD_CHUNK]
    out, err = sys.stdout.buffer, sys.stderr.buffer
    for _ in range(FLOOD_BYTES // FLOOD_CHUNK):
        out.write(chunk)
        out.flush()
        err.write(chunk)
        err.flush()


def leave_survivor() -> None:
    # Same process group (no new session) and no inherited pipe, so the run
    # looks finished to the renderer while the child keeps running. The
    # Popen object is dropped: nothing here waits for it.
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # One line per run, appended: a render with a measure pass leaves two.
    handle = open("survivors.pid", "a", encoding="ascii")
    try:
        handle.write(f"{child.pid}\n")
    finally:
        handle.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
