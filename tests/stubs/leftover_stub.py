"""A yt-dlp stand-in that leaves three processes behind, for the runner's sweep test in the image.

Run as (sys.executable, "-I", this file, *yt-dlp arguments) by
tests/test_fetcher_runner_sweep.py's in-image test only, where the test's
unittest process is PID 1 of a throwaway container. It starts:

- a sleeper in yt-dlp's own process group, which the run-end group kill
  reaches but which then stays a zombie until PID 1 reaps it;
- a sleeper in a session of its own (setsid), which the group kill misses;
- a hopper in a session of its own that forks to a new PID every 10 ms and
  lets the old one exit, leaving a trail of zombies for PID 1;

all with their standard streams on /dev/null, so communicate() returns as
soon as the stub exits. Then it replaces itself (same PID, same group) with
tests/stubs/ytdlp_stub.py in its "ok" mode, which checks the argument list
and writes the audio and the info JSON as for any successful run.

It refuses to start anything (exit 2) unless its parent is PID 1: the hopper
is a fork loop that only the runner's sweep ends, and on a developer machine
nothing would.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

STUB = Path(__file__).resolve().parent / "ytdlp_stub.py"
MODE_VAR = "STRANGE_RINGTONE_YTDLP_STUB"
SLEEPER = "import time; time.sleep(120)"
HOPPER = (
    "import os, time\n"
    "while True:\n"
    "    time.sleep(0.01)\n"
    "    if os.fork():\n"
    "        os._exit(0)\n"
)


def leave(code: str, own_session: bool) -> None:
    # The Popen object is dropped at once: nothing in this process waits for
    # the child, which is the point.
    subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=own_session,
    )


def main() -> None:
    if not hasattr(os, "fork") or os.getppid() != 1:
        sys.stderr.write("leftover stub: runs only under PID 1 of a throwaway container\n")
        sys.exit(2)
    leave(SLEEPER, own_session=False)
    leave(SLEEPER, own_session=True)
    leave(HOPPER, own_session=True)
    env = dict(os.environ)
    env[MODE_VAR] = "ok"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, "-I", str(STUB), *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
