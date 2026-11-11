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
    """The three calls remove_ipc_objects makes, recorded, answering as libc does: 0, or -1 and nothing raised.

    With a listing directory, a call that answers 0 takes its object out of
    that listing, as the kernel does; `gone` names objects that vanish
    although the call answers -1 (removed by someone else in between).
    """

    def __init__(self, listing: Path | None = None, answer: int = 0, gone: tuple[int, ...] = ()) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.listing = listing
        self.answer = answer
        self.gone = gone

    def __getattr__(self, name: str) -> Any:
        if name not in ("shmctl", "msgctl", "semctl"):
            raise AttributeError(name)

        def call(*args: Any) -> int:
            self.calls.append((name, args))
            if self.listing is not None and (self.answer == 0 or args[0] in self.gone):
                self._unlist(name[:3], args[0])
            return self.answer

        return call

    def _unlist(self, kind: str, ident: int) -> None:
        assert self.listing is not None
        path = self.listing / kind
        lines = path.read_text(encoding="ascii").splitlines()
        kept = [lines[0]] + [line for line in lines[1:] if int(line.split()[1]) != ident]
        path.write_text("\n".join(kept) + "\n", encoding="ascii")


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

    # The UID of the objects in LISTINGS.
    OWNER = 10001

    def remove(
        self, answer: int = 0, gone: tuple[int, ...] = (), euid: int = OWNER
    ) -> list[tuple[str, tuple[Any, ...]]]:
        remove_ipc_objects = seam(runner_server, "remove_ipc_objects")
        libc = FakeLibc(self.listing, answer, gone)
        with mock.patch.object(runner_server.ctypes, "CDLL", lambda *args, **kwargs: libc), \
                mock.patch.object(runner_server.os, "geteuid", lambda: euid, create=True):
            remove_ipc_objects(self.listing)
        return libc.calls

    def write_listings(self) -> None:
        for kind, lines in LISTINGS.items():
            (self.listing / kind).write_text("\n".join(lines) + "\n", encoding="ascii")

    def test_every_listed_object_is_removed(self) -> None:
        self.write_listings()
        # IPC_RMID is 0: shmctl and msgctl take no buffer for it, and semctl
        # takes a semaphore number it ignores.
        self.assertEqual(self.remove(), [
            ("shmctl", (3, 0, None)),
            ("shmctl", (32771, 0, None)),
            ("msgctl", (7, 0, None)),
            ("semctl", (2, 0, 0)),
        ])

    def test_an_object_of_this_user_that_is_still_listed_fails_the_removal(self) -> None:
        # libc answers -1 and raises nothing when the kernel refuses a
        # removal. An object of the runner's own that is still listed keeps
        # its memory charged to the container, so the removal must say so:
        # the clean-up then counts as failed, and under the contract the
        # runner restarts.
        self.write_listings()
        with self.assertRaises(RuntimeError):
            self.remove(answer=-1)
        # One kind left is enough, whichever it is.
        for kind in KINDS:
            with self.subTest(kind=kind):
                for other, lines in LISTINGS.items():
                    rows = lines if other == kind else lines[:1]
                    (self.listing / other).write_text("\n".join(rows) + "\n", encoding="ascii")
                with self.assertRaises(RuntimeError):
                    self.remove(answer=-1)

    def test_an_object_already_gone_is_no_failure(self) -> None:
        # -1 is also the answer for an object that went away between the
        # listing and the call. What decides is the listing afterwards.
        self.write_listings()
        self.remove(answer=-1, gone=(3, 32771, 7, 2))

    def test_another_users_object_may_stay(self) -> None:
        # The kernel refuses the runner an object that another user owns
        # and created; that one stays, and it is no failure of the clean-up.
        self.write_listings()
        calls = self.remove(answer=-1, euid=4242)
        self.assertEqual(len(calls), 4)

    def test_an_object_whose_owner_the_listing_does_not_name_counts_as_this_users(self) -> None:
        # A listing without the uid and cuid columns can't show whose an
        # object is: fail closed.
        for kind, lines in LISTINGS.items():
            columns = lines[0].split()
            keep = [at for at, column in enumerate(columns) if column not in ("uid", "cuid")]
            rows = [" ".join(line.split()[at] for at in keep) for line in lines]
            (self.listing / kind).write_text("\n".join(rows) + "\n", encoding="ascii")
        with self.assertRaises(RuntimeError):
            self.remove(answer=-1)

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


def ipc_limits_at_zero() -> list[str]:
    """The limits of this IPC namespace that are 0, so that no object of their kind can be created at all.

    kernel.shmmni and kernel.msgmni are the most segments and queues;
    kernel.sem is the most semaphores in a set, in all sets, per call and
    the most sets. A container started with the sysctls the compose
    example gives the runner has all of them at 0 but the per-call one.
    """
    kernel = Path("/proc/sys/kernel")
    sem = (kernel / "sem").read_text(encoding="ascii").split()
    limits = {
        "kernel.shmmni": (kernel / "shmmni").read_text(encoding="ascii").strip(),
        "kernel.msgmni": (kernel / "msgmni").read_text(encoding="ascii").strip(),
        "kernel.sem SEMMSL": sem[0],
        "kernel.sem SEMMNS": sem[1],
        "kernel.sem SEMMNI": sem[3],
    }
    return [name for name, value in limits.items() if int(value) == 0]


@unittest.skipUnless(IN_IMAGE, "the removal runs only in the fetcher image's test container")
class RealIpcCleanupTest(unittest.TestCase):
    """In the image, as PID 1 with the contract's scratch directory: a run's IPC objects go."""

    def setUp(self) -> None:
        # A failure, not a skip: see test_fetcher_runner_sweep.IN_IMAGE.
        self.assertEqual(os.getpid(), 1, "the in-image suite must run as its container's PID 1, with no init")
        # A container started with the runner's sysctls of the compose
        # example lets nobody create these objects, this test's stub run
        # included, so there is nothing to remove. Only that is a skip: a
        # stub run that cannot create them for any other reason fails below.
        at_zero = ipc_limits_at_zero()
        if at_zero:
            self.skipTest(f"this container's sysctls allow no System V IPC object ({', '.join(at_zero)} at 0)")
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
