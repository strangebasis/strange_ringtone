"""What a yt-dlp run leaves in the runner's IPC namespace is removed before the next job (docs/PLAN.md, section 6.1).

A run has the runner's UID, so it can create System V shared memory
segments, message queues and semaphore sets. None of them is a file or a
process: neither the sweep nor the file clean-up reaches them, they outlive
the run, and a segment's pages stay charged to the container's memory limit,
so a run could leave enough of them that every later fetch runs out of
memory. After every run and its sweep, before it answers, the runner removes
them, but only under the container's contract (may_remove_ipc_objects), where
the IPC namespace is the container's own (`ipc: none`) and nothing of the
run is left running. Anywhere else, a developer's machine and these tests
above all, the namespace may hold other programs' objects and is never
touched. The removal runs for real only in the fetcher image's test
container (RealIpcCleanupTest), where the test process is PID 1.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_runner import server as runner_server
from strange_ringtone_runner.config import SCRATCH_DIR, Settings
from strange_ringtone_runner.ytdlp import kill_and_reap
from test_fetcher_runner import OTHER_ID, RunnerServerTestCase, _TestRunnerServer
from test_fetcher_runner_leftovers import force_remove, post_mode, seam, serving
from test_fetcher_runner_sweep import IN_IMAGE
from test_fetcher_server import LogCapture, Response, wait_for
from test_fetcher_ytdlp import MIB, VIDEO_ID, StubYtDlp

KINDS = ("shm", "msg", "sem")
SYSV_IPC = Path("/proc/sysvipc")
# The first lines of the kernel's listings, and one object of each kind.
LISTINGS = {
    "shm": [
        "       key      shmid perms                  size  cpid  lpid nattch   uid   gid  cuid  cgid"
        "      atime      dtime      ctime                   rss                  swap",
        "         0          3   600               1048576    45     0      0 10001 10001 10001 10001"
        "          0          0 1794364086                     0                     0",
        "         0      32771   600              33554432    46    46      0 10001 10001 10001 10001"
        " 1794364086 1794364086 1794364086              33554432                     0",
    ],
    "msg": [
        "       key      msqid perms      cbytes       qnum lspid lrpid   uid   gid  cuid  cgid"
        "      stime      rtime      ctime",
        "         0          7   600          16          1    45     0 10001 10001 10001 10001"
        " 1794364086          0 1794364086",
    ],
    "sem": [
        "       key      semid perms      nsems   uid   gid  cuid  cgid      otime      ctime",
        "         0          2   600          4 10001 10001 10001 10001          0 1794364086",
    ],
}


class FakeLibc:
    """The three calls remove_ipc_objects makes, recorded, each answering success."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if name not in ("shmctl", "msgctl", "semctl"):
            raise AttributeError(name)

        def call(*args: Any) -> int:
            self.calls.append((name, args))
            return 0

        return call


class IpcDecisionTest(unittest.TestCase):
    """may_remove_ipc_objects(scratch_dir, sweep_strays): True only under the container's contract."""

    def decide(self, scratch_dir: Path, sweep_strays: bool, pid: int = 1, platform: str = "linux") -> Any:
        decide = seam(runner_server, "may_remove_ipc_objects")
        with mock.patch.object(os, "getpid", lambda: pid), mock.patch.object(sys, "platform", platform):
            return decide(scratch_dir, sweep_strays)

    def test_the_contract_and_nothing_else(self) -> None:
        self.assertIs(self.decide(SCRATCH_DIR, True), True)
        for pid in (0, 2, 4242):
            with self.subTest(pid=pid):
                self.assertIs(self.decide(SCRATCH_DIR, True, pid=pid), False)
        # FETCH_ALLOW_NO_SWEEP's case: what a run started may still use them.
        self.assertIs(self.decide(SCRATCH_DIR, False), False)
        for platform in ("win32", "darwin", "freebsd14"):
            with self.subTest(platform=platform):
                self.assertIs(self.decide(SCRATCH_DIR, True, platform=platform), False)
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        for scratch_dir in (Path(temp.name) / "scratch", Path("/tmp/../tmp/strange_ringtone_runner"), Path("/tmp")):
            with self.subTest(scratch_dir=str(scratch_dir)):
                self.assertIs(self.decide(scratch_dir, True), False)


class RemoveIpcObjectsTest(unittest.TestCase):
    """remove_ipc_objects: IPC_RMID for every object the kernel lists, with no real call made here."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.listing = Path(temp.name)

    def remove(self) -> list[tuple[str, tuple[Any, ...]]]:
        remove_ipc_objects = seam(runner_server, "remove_ipc_objects")
        libc = FakeLibc()
        with mock.patch.object(runner_server.ctypes, "CDLL", lambda *args, **kwargs: libc):
            remove_ipc_objects(self.listing)
        return libc.calls

    def test_every_listed_object_is_removed(self) -> None:
        for kind, lines in LISTINGS.items():
            (self.listing / kind).write_text("\n".join(lines) + "\n", encoding="ascii")
        # IPC_RMID is 0: shmctl and msgctl take no buffer for it, and semctl
        # takes a semaphore number it ignores.
        self.assertEqual(self.remove(), [
            ("shmctl", (3, 0, None)),
            ("shmctl", (32771, 0, None)),
            ("msgctl", (7, 0, None)),
            ("semctl", (2, 0, 0)),
        ])

    def test_a_listing_with_no_objects_or_none_at_all_removes_nothing(self) -> None:
        for kind, lines in LISTINGS.items():
            (self.listing / kind).write_text(lines[0] + "\n", encoding="ascii")
        self.assertEqual(self.remove(), [])
        for kind in KINDS:
            (self.listing / kind).unlink()
        self.assertEqual(self.remove(), [])


class IpcWiringTest(RunnerServerTestCase):
    """When the server asks for the removal, with the removal itself never run here."""

    def record_removals(self) -> list[bool]:
        seam(runner_server, "remove_ipc_objects")
        calls: list[bool] = []

        def recording(*args: Any, **kwargs: Any) -> None:
            # Whether the job's run is over and its answer not yet given:
            # the runner still holds its one-job lock.
            calls.append(self.server.busy.locked() and self.runs_log.is_file())

        patcher = mock.patch.object(runner_server, "remove_ipc_objects", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def post_mode(self, mode: str) -> Response:
        response, _job = post_mode(self.port, self.ytdlp, mode)
        wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        return response

    def test_never_off_the_contract(self) -> None:
        calls = self.record_removals()
        for mode in ("ok", "fail"):
            self.post_mode(mode)
        self.assertEqual(calls, [])

    def test_under_the_contract_after_every_run_before_the_answer(self) -> None:
        seam(runner_server, "may_remove_ipc_objects")
        calls = self.record_removals()
        with mock.patch.object(runner_server, "may_remove_ipc_objects", lambda *args, **kwargs: True):
            self.close_server()
            self.start()
            self.assertEqual(self.post_mode("ok").json()["status"], "done")
            self.assertEqual(self.post_mode("fail").json(), {"status": "failed", "error": "failed"})
        self.assertEqual(calls, [True, True])


def ipc_objects() -> dict[str, list[str]]:
    """The objects each listing of this IPC namespace names, as its lines."""
    return {kind: (SYSV_IPC / kind).read_text(encoding="ascii").splitlines()[1:] for kind in KINDS}


@unittest.skipUnless(IN_IMAGE, "the removal runs only in the fetcher image's test container")
class RealIpcCleanupTest(unittest.TestCase):
    """In the image, as PID 1 with the contract's scratch directory: a run's IPC objects go."""

    def setUp(self) -> None:
        # A failure, not a skip: see test_fetcher_runner_sweep.IN_IMAGE.
        self.assertEqual(os.getpid(), 1, "the in-image suite must run as its container's PID 1, with no init")
        self.addCleanup(force_remove, SCRATCH_DIR)
        remove = getattr(runner_server, "remove_ipc_objects", None)
        if remove is not None:
            self.addCleanup(remove)
        self.addCleanup(kill_and_reap)
        log = LogCapture()
        redirect = contextlib.redirect_stderr(log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.ytdlp = StubYtDlp(600, MIB, 30, sweep_strays=True)
        self.server = _TestRunnerServer(("127.0.0.1", 0), Settings(scratch_dir=SCRATCH_DIR), ytdlp=self.ytdlp,
                                        sweep_strays=True)
        context = serving(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def post(self, mode: str, video_id: str = VIDEO_ID) -> Response:
        response, _job = post_mode(self.server.server_address[1], self.ytdlp, mode, video_id)
        wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        return response

    def test_what_a_run_leaves_in_the_ipc_namespace_goes_and_the_next_job_runs(self) -> None:
        empty: dict[str, list[str]] = {kind: [] for kind in KINDS}
        self.assertEqual(ipc_objects(), empty, "the container's IPC namespace holds objects before the run")
        # Control: without the removal, the run's objects outlive the run,
        # its sweep and the file clean-up.
        with mock.patch.object(runner_server, "remove_ipc_objects", lambda *args, **kwargs: None, create=True):
            response = self.post("sysv")
        self.assertEqual(response.json()["status"], "done", response.body)
        left = ipc_objects()
        self.assertEqual({kind: len(lines) for kind, lines in left.items()}, {kind: 1 for kind in KINDS}, left)
        # The same run again, now with the removal: its objects and the
        # control's are gone before the answer.
        response = self.post("sysv")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertEqual(ipc_objects(), empty)
        self.assertEqual(self.post("ok", OTHER_ID).json()["status"], "done")


if __name__ == "__main__":
    unittest.main()
