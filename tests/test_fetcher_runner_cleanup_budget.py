"""The clean-up after a run has a time budget, and a failed one restarts the runner (docs/PLAN.md, section 6.1).

The whole clean-up after a run, the scratch directory, the rest of /tmp, the
System V IPC objects and the restore of the scratch directory, shares one
budget, CLEANUP_BUDGET_SECONDS, which fits inside the time the fetcher's
client leaves the runner past its timeout. remove_leftovers stops at the
deadline and says it is not done. When the budget is overrun, when anything
is left that should be gone, or when the scratch directory cannot be made
usable again, the job fails, its result is discarded and the runner writes
one line to stderr. Under the container's contract (may_clean_all_of_tmp,
the same decision as the clean-up of the rest of /tmp) the runner then
exits with EXIT_CLEAN_UP_FAILED once the answer is written, so the restart
policy starts a clean container; anywhere else it keeps serving.

No test here ends its own process: every server these tests start records
the exit code instead (_TestRunnerServer.exit_for_restart), and the real
exit is checked with os._exit replaced. The contract is faked for the
servers on temporary directories, with the clean-up of the rest of /tmp
recorded and never run; it runs for real only in the fetcher image's test
container (RealBudgetTest), where the test process is PID 1.
"""
from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import runner_client
from strange_ringtone_runner import __main__ as runner_entry
from strange_ringtone_runner import server as runner_server
from strange_ringtone_runner import ytdlp
from strange_ringtone_runner.config import SCRATCH_DIR, Settings
from strange_ringtone_runner.ytdlp import kill_and_reap
from test_fetcher_runner import LOG_LINE_RE, OTHER_ID, _TestRunnerServer, job_body, token
from test_fetcher_runner_ipc import LISTINGS, FakeLibc
from test_fetcher_runner_leftovers import (
    LITTER_BYTES,
    LITTER_NAME,
    LeftoverServerTestCase,
    TempTestCase,
    force_remove,
    inside,
    plain_directory,
    post_mode,
    release_fifos,
    seam,
    serving,
    tree,
)
from test_fetcher_runner_sweep import IN_IMAGE
from test_fetcher_server import DEADLINE, LogCapture, Response, build_request, wait_for
from test_fetcher_ytdlp import MIB, MODE_VAR, VIDEO_ID, StubYtDlp

JSON_TYPE = "application/json"
# A budget the tests can overrun quickly, and how long a slowed step takes
# to overrun it for sure.
SMALL_BUDGET_SECONDS = 0.5
SLOW_SECONDS = 1.0
FAILED = {"status": "failed", "error": "failed"}


def exit_code() -> int:
    return seam(runner_server, "EXIT_CLEAN_UP_FAILED")


def exits(server: Any) -> list[int]:
    """The exit codes a test server recorded instead of exiting."""
    return list(getattr(server, "exit_codes", []))


# --- the budget and the exit code ------------------------------------------------


class BudgetConstantsTest(unittest.TestCase):
    def test_the_budget_is_3_s_and_fits_the_fetchers_grace_with_a_margin(self) -> None:
        budget = seam(runner_server, "CLEANUP_BUDGET_SECONDS")
        self.assertEqual(budget, 3.0)
        # Before it answers, the runner may spend its drain after a timeout
        # kill, a reap at each run boundary and then the clean-up: the
        # fetcher's client must still be waiting, with 3 s to spare.
        worst = ytdlp.DRAIN_SECONDS + 2 * ytdlp.REAP_SECONDS + budget
        self.assertGreaterEqual(runner_client.POST_GRACE_SECONDS, worst + 3.0)

    def test_the_exit_code_is_one_the_entry_does_not_use(self) -> None:
        code = exit_code()
        self.assertNotIn(code, {0, 1, 2, runner_entry.EXIT_ROUTE, runner_entry.EXIT_NO_SWEEP})
        # Not one a shell or a signal gives: 126 and above mean those.
        self.assertTrue(3 <= code <= 125, code)

    def test_the_real_exit_flushes_stderr_and_ends_the_process_with_the_code(self) -> None:
        exit_for_restart = seam(runner_server.RunnerServer, "exit_for_restart")
        order: list[str] = []
        stderr = mock.Mock()
        stderr.flush.side_effect = lambda: order.append("flush")
        with mock.patch.object(runner_server.sys, "stderr", stderr), \
                mock.patch.object(runner_server.os, "_exit", side_effect=lambda code: order.append(f"exit {code}")):
            exit_for_restart(mock.Mock(), exit_code())
        self.assertEqual(order, ["flush", f"exit {exit_code()}"])


# --- remove_leftovers and remove_ipc_objects stop at the deadline ------------------


class DeadlineTest(TempTestCase):
    def litter(self, count: int) -> None:
        for index in range(count):
            (self.root / f"stray-{index}").write_bytes(LITTER_BYTES)
        (self.root / "dir" / "sub").mkdir(parents=True)
        (self.root / "dir" / "sub" / "file").write_bytes(LITTER_BYTES)

    def test_a_deadline_already_past_removes_nothing_and_says_not_done(self) -> None:
        self.litter(5)
        before = tree(self.root)
        remove_leftovers = seam(runner_server, "remove_leftovers")
        self.assertIs(remove_leftovers(self.root, deadline=time.monotonic() - 1), False)
        self.assertEqual(tree(self.root), before)

    def test_a_deadline_ahead_changes_nothing(self) -> None:
        # Control: with time to spare, everything goes as without a deadline.
        self.litter(5)
        remove_leftovers = seam(runner_server, "remove_leftovers")
        self.assertIs(remove_leftovers(self.root, deadline=time.monotonic() + 60), True)
        self.assertEqual(tree(self.root), [])

    def test_the_deadline_stops_a_clean_up_on_its_way(self) -> None:
        self.litter(40)
        cls = runner_server._DescriptorTree if runner_server._BY_DESCRIPTOR else runner_server._PathTree
        real = cls.__dict__["unlink"]
        removed: list[str] = []

        def slow(tree_: Any, handle: Any, name: str) -> None:
            time.sleep(0.05)
            real(tree_, handle, name)
            removed.append(name)

        remove_leftovers = seam(runner_server, "remove_leftovers")
        started = time.monotonic()
        with mock.patch.object(cls, "unlink", slow):
            done = remove_leftovers(self.root, deadline=started + 0.3)
        self.assertIs(done, False)
        # Stopped near the deadline: some entries went, most are still there.
        self.assertLess(time.monotonic() - started, 0.3 + 0.5)
        self.assertTrue(removed)
        self.assertGreater(len(tree(self.root)), 20)

    def test_the_removal_of_ipc_objects_stops_at_the_deadline(self) -> None:
        remove_ipc_objects = seam(runner_server, "remove_ipc_objects")
        temp = Path(self.temp) / "sysvipc"
        temp.mkdir()
        for kind, lines in LISTINGS.items():
            (temp / kind).write_text("\n".join(lines) + "\n", encoding="ascii")
        # The objects in LISTINGS belong to UID 10001; a removal that the
        # deadline cut short leaves them listed, and says so.
        for deadline, expected in ((time.monotonic() - 1, 0), (time.monotonic() + 60, 4)):
            with self.subTest(past=expected == 0):
                libc = FakeLibc(temp)
                with mock.patch.object(runner_server.ctypes, "CDLL", return_value=libc), \
                        mock.patch.object(runner_server.os, "geteuid", lambda: 10001, create=True):
                    if expected == 0:
                        with self.assertRaises(RuntimeError):
                            remove_ipc_objects(temp, deadline=deadline)
                    else:
                        remove_ipc_objects(temp, deadline=deadline)
                self.assertEqual(len(libc.calls), expected, libc.calls)


# --- the server -------------------------------------------------------------------


class BudgetServerTestCase(LeftoverServerTestCase):
    """A runner server on a temporary directory, with the steps of its clean-up made slow or failing on demand."""

    def setUp(self) -> None:
        super().setUp()
        # What a test asks of the clean-up: a slow restore, a restore that
        # fails, or a scratch directory where something is left.
        self.slow_restore = False
        self.failed_restore = False
        self.left_behind = False
        self.wide: list[tuple[Path, Path | None]] = []
        real_remove = seam(runner_server, "remove_leftovers")
        real_restore = seam(runner_server, "restore_scratch")

        def remove(root: Path, keep: Path | None = None, **kwargs: Any) -> bool:
            if not inside(root, self.scratch):
                # The clean-up of the rest of /tmp: recorded, never run here.
                self.wide.append((Path(root), keep))
                return True
            done = real_remove(root, keep, **kwargs)
            return False if self.left_behind else done

        def restore(directory: Path) -> None:
            if self.slow_restore:
                time.sleep(SLOW_SECONDS)
            if self.failed_restore:
                raise RuntimeError("scratch directory unusable")
            real_restore(directory)

        for name, value in (("remove_leftovers", remove), ("restore_scratch", restore),
                            ("CLEANUP_BUDGET_SECONDS", SMALL_BUDGET_SECONDS)):
            patcher = mock.patch.object(runner_server, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def under_the_contract(self) -> None:
        """Start the server again as if it ran under the container's contract."""
        seam(runner_server, "may_clean_all_of_tmp")
        patcher = mock.patch.object(runner_server, "may_clean_all_of_tmp", lambda *args, **kwargs: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.close_server()
        self.start()

    def fail_the_clean_up(self, how: str) -> None:
        setattr(self, {"overrun": "slow_restore", "leftover": "left_behind", "restore": "failed_restore"}[how], True)

    def heal_the_clean_up(self) -> None:
        self.slow_restore = self.failed_restore = self.left_behind = False

    def assertOneQuietLine(self, job: str) -> None:
        lines = self.other_lines()
        self.assertEqual(len(lines), 1, self.log.getvalue())
        for secret in (job, VIDEO_ID, "Stub"):
            self.assertNotIn(secret, lines[0])

    def assertHealthy(self) -> None:
        health = self.request("GET", "/healthz")
        self.assertEqual((health.status, health.body), (200, b"ok"))


class ContractExitTest(BudgetServerTestCase):
    """Under the contract: a clean-up that overruns, leaves something or can't restore fails the job and exits."""

    def setUp(self) -> None:
        super().setUp()
        self.under_the_contract()

    def test_a_clean_up_in_time_answers_and_the_runner_stays(self) -> None:
        # Control: the faked contract alone changes nothing for a clean run.
        for mode in ("ok", "litter", "wedge"):
            with self.subTest(mode=mode):
                response, job = self.post_mode(mode)
                self.assertEqual(response.status, 200, response.body)
                if mode == "wedge":
                    self.assertEqual(response.json(), FAILED)
                else:
                    self.assertEqual(response.json()["status"], "done", response.body)
                    self.assertEqual(self.audio(job).status, 200)
        self.assertEqual(exits(self.server), [])
        self.assertTrue(self.wide, "the faked contract did not reach the clean-up of the rest of /tmp")
        self.assertEqual(self.other_lines(), [])

    def test_each_failure_fails_the_job_discards_the_result_and_stops_the_runner(self) -> None:
        for how in ("overrun", "leftover", "restore"):
            with self.subTest(how=how):
                self.log = LogCapture()
                with contextlib.redirect_stderr(self.log):
                    self.fail_the_clean_up(how)
                    # A run that succeeded: only the clean-up makes it fail.
                    response, job = self.post_mode("ok")
                    self.heal_the_clean_up()
                    self.assertEqual(response.status, 200, response.body)
                    self.assertEqual(response.json(), FAILED)
                    self.assertEqual(exits(self.server), [exit_code()])
                    self.assertOneQuietLine(job)
                    # The held result went with the failure.
                    self.assertJsonError(self.audio(job), 404, "not_found")
                    self.assertEqual(self.scratch_contents(), [])
                    # /healthz stays as it is.
                    self.assertHealthy()
                    # The server here goes on, so the next case starts clean.
                    self.server.exit_codes.clear()

    def test_the_exit_comes_after_the_answer_with_the_lock_still_held(self) -> None:
        answered = threading.Event()
        seen: list[tuple[int, bool, bool]] = []

        def recorder(code: int) -> None:
            # Whether the client had the whole answer before the exit, and
            # whether the runner still held its one-job lock.
            seen.append((code, answered.wait(DEADLINE), self.server.busy.locked()))

        self.server.exit_for_restart = recorder
        self.fail_the_clean_up("leftover")
        response, _job = post_mode(self.port, self.ytdlp, "ok")
        answered.set()
        wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        self.assertEqual(response.json(), FAILED)
        self.assertEqual(seen, [(exit_code(), True, True)])

    def test_the_exit_comes_even_when_the_answer_cannot_be_written(self) -> None:
        real = runner_server.Handler.send_json

        def broken(handler: Any, status: int, payload: dict[str, Any], *args: Any) -> None:
            if "status" in payload:
                raise BrokenPipeError("the fetcher went away")
            real(handler, status, payload, *args)

        self.fail_the_clean_up("restore")
        self.ytdlp.extra[MODE_VAR] = "ok"
        body = job_body(VIDEO_ID, token())
        request = build_request("POST", "/jobs", [("Content-Type", JSON_TYPE), ("Content-Length", str(len(body)))],
                                body)
        with mock.patch.object(runner_server.Handler, "send_json", broken):
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                sock.sendall(request)
                with contextlib.suppress(OSError):
                    while sock.recv(65536):
                        pass
            wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        self.assertEqual(exits(self.server), [exit_code()])


class ContractLeftoverOutsideTest(BudgetServerTestCase):
    """Under the contract: what the clean-up leaves outside the scratch directory counts as left behind too."""

    def setUp(self) -> None:
        super().setUp()
        # What a test asks of the steps outside the scratch directory: the
        # clean-up of the rest of /tmp leaves something, or the removal of
        # the System V IPC objects fails. Both are faked: neither runs here.
        self.outside_left = False
        self.ipc_failed = False
        self.ipc_calls = 0
        faked_remove = runner_server.remove_leftovers

        def remove(root: Path, keep: Path | None = None, **kwargs: Any) -> bool:
            done = faked_remove(root, keep, **kwargs)
            if not inside(root, self.scratch) and self.outside_left:
                return False
            return done

        def remove_ipc(*args: Any, **kwargs: Any) -> None:
            self.ipc_calls += 1
            if self.ipc_failed:
                raise OSError("no IPC object could be removed")

        for name, value in (("remove_leftovers", remove), ("remove_ipc_objects", remove_ipc),
                            ("may_remove_ipc_objects", lambda *args, **kwargs: True)):
            patcher = mock.patch.object(runner_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.under_the_contract()

    def test_a_clean_up_with_both_steps_clean_answers_and_the_runner_stays(self) -> None:
        # Control: the two faked steps alone change nothing for a clean run.
        response, job = self.post_mode("ok")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertEqual(self.audio(job).status, 200)
        self.assertEqual(exits(self.server), [])
        self.assertTrue(self.wide, "the faked contract did not reach the clean-up of the rest of /tmp")
        self.assertEqual(self.ipc_calls, 1)
        self.assertEqual(self.other_lines(), [])

    def test_a_leftover_outside_or_a_failed_ipc_removal_fails_the_job_and_stops_the_runner(self) -> None:
        for flag in ("outside_left", "ipc_failed"):
            with self.subTest(failure=flag):
                self.log = LogCapture()
                with contextlib.redirect_stderr(self.log):
                    setattr(self, flag, True)
                    # A run that succeeded, and a scratch directory left clean:
                    # only the step outside it failed.
                    response, job = self.post_mode("ok")
                    setattr(self, flag, False)
                    self.assertEqual(response.json(), FAILED)
                    self.assertEqual(exits(self.server), [exit_code()])
                    self.assertOneQuietLine(job)
                    self.assertJsonError(self.audio(job), 404, "not_found")
                    self.assertHealthy()
                    self.server.exit_codes.clear()


class ContractDeadlineWiringTest(BudgetServerTestCase):
    """Under the contract: every step of the clean-up is given the one deadline, so it stops there, not later.

    The tests above see an overrun only after the clean-up is over; these
    see that each step of it was told when to stop. A step run without the
    deadline would hold the answer as long as the step takes.
    """

    # Entries of the scratch directory a slowed removal takes far longer
    # than the small budget to remove, and how long each removal takes.
    STRAYS = 80
    SLOW_UNLINK_SECONDS = 0.05

    def setUp(self) -> None:
        super().setUp()
        # Each call of remove_leftovers and remove_ipc_objects, with the
        # deadline it was given and when it was called. The clean-up of the
        # rest of /tmp and the IPC removal stay faked: neither runs here.
        self.calls: list[tuple[str, float | None, float]] = []
        self.strays_to_leave = 0
        self.slow = False
        faked_remove = runner_server.remove_leftovers

        def remove(root: Path, keep: Path | None = None, **kwargs: Any) -> bool:
            in_scratch = inside(root, self.scratch)
            self.calls.append(("scratch" if in_scratch else "wide", kwargs.get("deadline"), time.monotonic()))
            if in_scratch and self.strays_to_leave:
                # What a run could leave: many entries, put there as the
                # clean-up starts, so only its scratch pass meets them.
                for index in range(self.strays_to_leave):
                    (self.scratch / f"stray-{index}").write_bytes(LITTER_BYTES)
                self.strays_to_leave = 0
            return faked_remove(root, keep, **kwargs)

        def remove_ipc(*args: Any, **kwargs: Any) -> None:
            self.calls.append(("ipc", kwargs.get("deadline"), time.monotonic()))

        cls = runner_server._DescriptorTree if runner_server._BY_DESCRIPTOR else runner_server._PathTree
        real_unlink = cls.__dict__["unlink"]

        def unlink(tree_: Any, handle: Any, name: str) -> None:
            # Slowed as DeadlineTest slows it, but only for the strays.
            if self.slow and name.startswith("stray-"):
                time.sleep(self.SLOW_UNLINK_SECONDS)
            real_unlink(tree_, handle, name)

        for target, name, value in ((runner_server, "remove_leftovers", remove),
                                    (runner_server, "remove_ipc_objects", remove_ipc),
                                    (runner_server, "may_remove_ipc_objects", lambda *args, **kwargs: True),
                                    (cls, "unlink", unlink)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.under_the_contract()

    def test_each_step_is_given_a_deadline_no_later_than_the_start_plus_the_budget(self) -> None:
        started = time.monotonic()
        response, _job = self.post_mode("ok")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertEqual(exits(self.server), [])
        self.assertEqual(sorted({step for step, _deadline, _at in self.calls}), ["ipc", "scratch", "wide"],
                         self.calls)
        for step, deadline, at in self.calls:
            with self.subTest(step=step):
                self.assertIsNotNone(deadline, f"the {step} step was given no deadline")
                assert deadline is not None
                # The deadline is the clean-up's own, set once as it starts:
                # not before this job, and no later than its first step.
                self.assertGreaterEqual(deadline, started + SMALL_BUDGET_SECONDS)
                self.assertLessEqual(deadline, at + SMALL_BUDGET_SECONDS)

    def test_a_slow_scratch_pass_stops_at_the_deadline_and_the_answer_comes_in_time(self) -> None:
        self.log = LogCapture()
        with contextlib.redirect_stderr(self.log):
            self.strays_to_leave = self.STRAYS
            self.slow = True
            response, job = post_mode(self.port, self.ytdlp, "ok")
            answered = time.monotonic()
            at_answer = self.scratch_contents()
            wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
            self.slow = False
        first = min(at for step, _deadline, at in self.calls if step == "scratch")
        # Removing every stray would take STRAYS x 0.05 s = 4 s; the answer
        # comes once the budget is spent, with a margin for a busy machine.
        self.assertLess(answered - first, SMALL_BUDGET_SECONDS + 1.0)
        self.assertEqual(response.json(), FAILED)
        self.assertEqual(exits(self.server), [exit_code()])
        self.assertOneQuietLine(job)
        # The pass stopped on its way: strays are still there at the answer.
        self.assertTrue([name for name in at_answer if name.startswith("stray-")], at_answer)


class OffContractTest(BudgetServerTestCase):
    """Anywhere else: the same failures fail the job, and the runner keeps serving."""

    def test_each_failure_fails_the_job_and_the_runner_keeps_serving(self) -> None:
        for how in ("overrun", "leftover", "restore"):
            with self.subTest(how=how):
                self.log = LogCapture()
                with contextlib.redirect_stderr(self.log):
                    self.fail_the_clean_up(how)
                    response, job = self.post_mode("ok")
                    self.heal_the_clean_up()
                    self.assertEqual(response.json(), FAILED)
                    self.assertOneQuietLine(job)
                    self.assertJsonError(self.audio(job), 404, "not_found")
                    self.assertEqual(exits(self.server), [])
                    self.assertHealthy()
                    self.assertNextJobWorks()
        self.assertEqual(self.wide, [])
        self.assertOutsideUntouched()


# --- in the image, as PID 1 -------------------------------------------------------


@unittest.skipUnless(IN_IMAGE, "the contract's clean-up runs only in the fetcher image's test container")
class RealBudgetTest(unittest.TestCase):
    """In the image, as PID 1 with the contract's scratch directory: a tree past the budget restarts the runner."""

    def setUp(self) -> None:
        # A failure, not a skip: see test_fetcher_runner_sweep.IN_IMAGE.
        self.assertEqual(os.getpid(), 1, "the in-image suite must run as its container's PID 1, with no init")
        self.tmp = SCRATCH_DIR.parent
        self.clear()
        self.addCleanup(self.clear)
        self.addCleanup(kill_and_reap)
        self.log = LogCapture()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.ytdlp = StubYtDlp(600, MIB, 30, sweep_strays=True)
        self.server = _TestRunnerServer(("127.0.0.1", 0), Settings(scratch_dir=SCRATCH_DIR), ytdlp=self.ytdlp,
                                        sweep_strays=True)
        context = serving(self.server)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def clear(self) -> None:
        release_fifos(self.tmp / "strange_ringtone_runner")
        for name in os.listdir(self.tmp):
            if name == "strange_ringtone_runner" or name.startswith(LITTER_NAME):
                force_remove(self.tmp / name)

    def post(self, mode: str, video_id: str = VIDEO_ID) -> tuple[Response, str]:
        response, job = post_mode(self.server.server_address[1], self.ytdlp, mode, video_id)
        wait_for(lambda: not self.server.busy.locked(), "the runner to let go of its lock")
        return response, job

    def assertOnlyScratchInTmp(self, job: str | None) -> None:
        self.assertEqual(os.listdir(self.tmp), ["strange_ringtone_runner"])
        for directory in (SCRATCH_DIR, SCRATCH_DIR / "results"):
            self.assertTrue(plain_directory(directory), f"{directory} is not a plain directory")
        self.assertEqual(tree(SCRATCH_DIR), ["results", f"results/{job}.m4a"] if job else ["results"])

    def test_a_tree_the_budget_cannot_cover_fails_the_job_and_stops_the_runner(self) -> None:
        # Control: the same tree within the real budget is no reason to stop.
        response, job = self.post("tree")
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertOnlyScratchInTmp(job)
        self.assertEqual(exits(self.server), [])
        # The budget made small instead of the tree huge.
        seam(runner_server, "CLEANUP_BUDGET_SECONDS")
        with mock.patch.object(runner_server, "CLEANUP_BUDGET_SECONDS", 0.01):
            response, job = self.post("tree")
        self.assertEqual(response.json(), FAILED)
        self.assertEqual(exits(self.server), [exit_code()])
        lines = [line for line in self.log.lines() if not LOG_LINE_RE.match(line)]
        self.assertEqual(len(lines), 1, self.log.getvalue())
        for secret in (job, VIDEO_ID, "Stub"):
            self.assertNotIn(secret, lines[0])
        # In a deployment the container ends here and starts again empty.
        # This server goes on, and its next clean-up removes the rest.
        response, job = self.post("ok", OTHER_ID)
        self.assertEqual(response.json()["status"], "done", response.body)
        self.assertOnlyScratchInTmp(job)


if __name__ == "__main__":
    unittest.main()
