"""The server as its container's PID 1 collects what each run's group kill killed (docs/PLAN.md, section 5.3).

In the compose example the server is PID 1, so every process a run leaves
behind becomes the server's child once the run's leader exits. Killed by the
group kill, such a child stays a zombie that holds a slot of pids_limit until
its parent collects it. Enough of them and no render, and no health check,
can start until the container restarts.

CI runs this suite as its container's PID 1 (no --init), so the server built
here in the test's own process is PID 1 too. Its renderer runs a stand-in
that forks children into its own process group, two that sleep and one that
exits at once, and then execs the image's real ffmpeg with the arguments it
was given. Every render must succeed, and afterwards none of those children
may be left, alive or as a zombie.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from corpus.build import valid_wav
from strange_ringtone.config import Settings
from strange_ringtone.render import FFMPEG, Renderer, default_prlimit
from strange_ringtone.server import RingtoneServer

from image.test_render_real import ORIGIN

# Forks its children, records their PIDs, then becomes the real ffmpeg.
# Arguments: the file that collects the PIDs, then ffmpeg's arguments.
STAND_IN = r"""
import os, sys, time
record, args = sys.argv[1], sys.argv[2:]
for sleeps in (True, True, False):
    pid = os.fork()
    if pid == 0:
        # Off the pipes, so the run looks finished as soon as ffmpeg is.
        null = os.open("/dev/null", os.O_RDWR)
        for target in (0, 1, 2):
            os.dup2(null, target)
        if sleeps:
            time.sleep(600)
        os._exit(0)
    with open(record, "a", encoding="ascii") as handle:
        handle.write(f"{pid}\n")
os.execv(FFMPEG, [FFMPEG, *args])
""".replace("FFMPEG", repr(FFMPEG))


def _state(pid: int) -> str | None:
    """The state letter in /proc/<pid>/stat, or None once the PID is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            return handle.read().rsplit(b")", 1)[1].split()[0].decode("ascii")
    except (OSError, IndexError):
        return None


@unittest.skipUnless(os.getpid() == 1, "needs the suite to run as its container's PID 1, as CI runs it")
class CollectionAsPid1Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.record = self.temp / "children.txt"
        log = contextlib.redirect_stderr(io.StringIO())
        log.__enter__()
        self.addCleanup(log.__exit__, None, None, None)
        settings = Settings(
            public_origin=ORIGIN,
            scratch_dir=self.temp / "scratch",
            rate_limit_renders_per_minute=1000,
        )
        # The production limits in front of the stand-in, as in front of ffmpeg.
        renderer = Renderer(
            (sys.executable, "-I", "-c", STAND_IN, str(self.record)),
            settings.render_timeout_seconds,
            prlimit=default_prlimit(),
        )
        self.server = RingtoneServer(("127.0.0.1", 0), settings, renderer=renderer)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # Runs first: whatever the test leaves is ended and collected here,
        # so no later test of this process inherits it.
        self.addCleanup(self.clean_up_children)
        self.port = self.server.server_address[1]

    def children(self) -> list[int]:
        if not self.record.exists():
            return []
        return [int(line) for line in self.record.read_text(encoding="ascii").split()]

    def clean_up_children(self) -> None:
        for pid in self.children():
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass

    def render(self, body: bytes) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.request("POST", "/api/render?normalize=true", body=body,
                               headers={"Origin": ORIGIN, "Content-Type": "audio/wav"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_no_child_of_a_run_is_left_behind(self) -> None:
        body = valid_wav(self.temp / "clip.wav", 2.0).read_bytes()
        for _ in range(3):
            status, payload = self.render(body)
            self.assertEqual(status, 200, payload)
        children = self.children()
        # Two or three runs per render, three children per run.
        self.assertGreaterEqual(len(children), 3 * 2 * 3)
        deadline = time.monotonic() + 5
        while any(_state(pid) is not None for pid in children) and time.monotonic() < deadline:
            time.sleep(0.05)
        left = {pid: _state(pid) for pid in children if _state(pid) is not None}
        self.assertEqual(left, {}, "children of a run are left to the server (Z: a zombie it never collected)")


if __name__ == "__main__":
    unittest.main()
