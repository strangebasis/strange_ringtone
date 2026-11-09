"""The runner's kill-and-reap sweep at each run boundary (docs/PLAN.md 6.1; owner's decision of 2026-11-05).

A yt-dlp child that calls setsid() leaves the run's process group and
survives the run-end group kill, and the runner, as its container's PID 1,
inherits every orphan a run leaves and must reap it, or each one stays a
zombie that counts against pids_limit for good. So the runner, and only as
PID 1, sends kill(-1, SIGKILL) and then reaps with waitpid(-1, WNOHANG) for a
bounded time, before each run and after it, under the run lock, never while a
Popen is in flight.

kill(-1) outside a throwaway container would kill every process of the user
running the tests. So every test here but one replaces os.kill, os.waitpid
and os.getpid with fakes that never pass -1 on (Syscalls below); the one real
sweep (RealSweepTest) runs only as PID 1 inside the fetcher image's test
container, where the in-image suite's unittest process is PID 1.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import helpers
from strange_ringtone_fetch.metadata import FetchError
from strange_ringtone_fetch.routes import DefaultRoute
from strange_ringtone_runner import __main__ as runner_entry
from strange_ringtone_runner import config as runner_config
from strange_ringtone_runner import ytdlp
from strange_ringtone_runner.config import Settings
from strange_ringtone_runner.server import RunnerServer
from strange_ringtone_runner.ytdlp import YtDlp, kill_and_reap, sweep_is_safe_here
from test_fetcher_runner import _TestRunnerServer, job_body, token
from test_fetcher_server import LogCapture, build_request, exchange, wait_for
from test_fetcher_ytdlp import STUB_COMMAND, VIDEO_ID, StubYtDlp

MIB = 1024 * 1024
SIGKILL = getattr(signal, "SIGKILL", 9)
WNOHANG = getattr(os, "WNOHANG", 1)
LEFTOVER_STUB = helpers.ROOT / "tests" / "stubs" / "leftover_stub.py"
# The real sweep runs only where it can harm nothing but a throwaway
# container: PID 1 on Linux, in the fetcher image's test run, which sets this.
SWEEP_LINE = "strange_ringtone_runner: PID 1: killing and reaping a run's leftovers at each run boundary"
# What the entry says when FETCH_ALLOW_NO_SWEEP lets it serve where the sweep
# can't run (owner's decision Q4, 2026-11-05).
NO_SWEEP_LINE = (
    "strange_ringtone_runner: FETCH_ALLOW_NO_SWEEP is set and the sweep can't run here (not PID 1, or no "
    "kill and waitpid): a run's leftovers are not killed and reaped (README, Runs with)"
)
# The fetcher image's test run on Linux. RealSweepTest runs there and needs
# to be PID 1, which it asserts rather than skips on: it is the sweep's only
# end-to-end proof, and a container started with an init (`docker run
# --init`, or a daemon whose default is init) would otherwise drop it from a
# green run without a word.
IN_IMAGE = sys.platform.startswith("linux") and os.environ.get("STRANGE_RINGTONE_FETCHER_IMAGE_TESTS") == "1"


@contextlib.contextmanager
def without(module: Any, name: str) -> Iterator[None]:
    """module.name removed for the duration, as on a platform that lacks it."""
    saved = getattr(module, name)
    delattr(module, name)
    try:
        yield
    finally:
        setattr(module, name, saved)


class Syscalls:
    """Fakes for os.kill, os.waitpid and os.getpid that record the sweep's calls and never pass -1 on.

    A positive PID goes to the real call (Popen kills and waits for its own
    child by PID, and the tests check processes that way). kill(pid <= 0) is
    recorded and never sent; waitpid(-1) is recorded and answered from
    `reaps`, then ChildProcessError. `in_flight` is set when a sweep call
    arrives while a Popen the test recorded has no returncode yet.
    """

    def __init__(self, pid: int = 1) -> None:
        self.pid = pid
        self.calls: list[tuple[str, int, int]] = []
        self.reaps: list[tuple[int, int] | BaseException] = []
        self.kill_errors: list[BaseException | None] = []
        self.kill_delays: list[float] = []
        self.popens: list[subprocess.Popen[bytes]] = []
        # Called with each Popen the test records, before fetch sees it.
        self.popen_hook: Any = None
        self.events: list[str] = []
        self.in_flight = False
        self._real_kill = os.kill
        self._real_waitpid = getattr(os, "waitpid", None)
        self._lock = threading.Lock()

    def _note_in_flight(self) -> None:
        if any(proc.returncode is None for proc in self.popens):
            self.in_flight = True

    def kill(self, pid: int, sig: int) -> None:
        if pid > 0:
            self._real_kill(pid, sig)
            return
        with self._lock:
            self._note_in_flight()
            self.calls.append(("kill", pid, sig))
            self.events.append("sweep")
            error = self.kill_errors.pop(0) if self.kill_errors else None
            delay = self.kill_delays.pop(0) if self.kill_delays else 0.0
        if delay:
            time.sleep(delay)
        if error is not None:
            raise error

    def waitpid(self, pid: int, options: int) -> tuple[int, int]:
        if pid > 0:
            assert self._real_waitpid is not None
            return self._real_waitpid(pid, options)
        with self._lock:
            self._note_in_flight()
            self.calls.append(("waitpid", pid, options))
            answer = self.reaps.pop(0) if self.reaps else ChildProcessError()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def getpid(self) -> int:
        return self.pid

    def sweeps(self) -> list[tuple[str, int, int]]:
        return [call for call in self.calls if call[0] == "kill"]

    @contextlib.contextmanager
    def installed(self, record_popen: bool = False) -> Iterator[Syscalls]:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "kill", self.kill))
            stack.enter_context(mock.patch.object(os, "waitpid", self.waitpid, create=True))
            stack.enter_context(mock.patch.object(os, "getpid", self.getpid))
            # Windows has neither; the sweep's code names them at call time.
            stack.enter_context(mock.patch.object(signal, "SIGKILL", SIGKILL, create=True))
            stack.enter_context(mock.patch.object(os, "WNOHANG", WNOHANG, create=True))
            if record_popen:
                real_popen = subprocess.Popen

                def recording(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                    proc = real_popen(*args, **kwargs)
                    with self._lock:
                        self.popens.append(proc)
                        self.events.append("popen")
                    if self.popen_hook is not None:
                        self.popen_hook(proc)
                    return proc

                stack.enter_context(mock.patch.object(ytdlp.subprocess, "Popen", recording))
            yield self


def start_entry(
    pid: int, environ: dict[str, str], routes: tuple[DefaultRoute, ...] = (), missing: tuple[Any, str] | None = None,
) -> tuple[int, str, list[dict[str, Any]], list[str]]:
    """runner_entry.main() as process `pid`: (exit code, stderr, each RunnerServer's options, the order of steps).

    os.kill, os.waitpid and os.getpid are the fakes of Syscalls, which never
    pass -1 on, and RunnerServer is a fake that never runs a job, so no real
    kill(-1) can happen even with the PID faked to 1. The route tables are a
    fake too (`routes`), and so is the non-dumpable step: the test process
    itself stays dumpable. `missing` is a (module, name) removed once the
    fakes are in, as on a platform without that call.
    """
    started: list[dict[str, Any]] = []
    order: list[str] = []
    log = LogCapture()

    class FakeServer:
        def __init__(self, address: tuple[str, int], settings: Any, **options: Any) -> None:
            order.append("server")
            started.append(options)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    def check_routes(*args: Any, **kwargs: Any) -> tuple[DefaultRoute, ...]:
        order.append("routes")
        return routes

    def not_dumpable() -> None:
        order.append("not dumpable")

    with Syscalls(pid=pid).installed(), \
            mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.object(runner_entry, "RunnerServer", FakeServer), \
            mock.patch.object(runner_entry, "default_routes", check_routes, create=True), \
            mock.patch.object(runner_entry, "make_not_dumpable", not_dumpable), \
            mock.patch("signal.signal"), \
            contextlib.redirect_stderr(log), \
            (without(*missing) if missing else contextlib.nullcontext()):
        code = runner_entry.main(["--bind", "127.0.0.1"])
    return code, log.getvalue(), started, order


class KillAndReapTest(unittest.TestCase):
    """kill_and_reap itself: one kill(-1, SIGKILL), then waitpid(-1, WNOHANG) until no child is left, bounded."""

    def test_as_pid_1_it_kills_everything_once_then_reaps_until_no_child_is_left(self) -> None:
        fake = Syscalls(pid=1)
        fake.reaps = [(41, 9), (42, 9), (43, 0)]
        with fake.installed():
            reaped = kill_and_reap()
        self.assertEqual(reaped, 3)
        self.assertEqual(fake.calls[0], ("kill", -1, SIGKILL))
        self.assertEqual(fake.sweeps(), [("kill", -1, SIGKILL)])
        # Reaped until ChildProcessError: three children, then the fourth
        # call finds none.
        self.assertEqual(fake.calls[1:], [("waitpid", -1, WNOHANG)] * 4)

    def test_nothing_to_kill_still_reaps_old_zombies(self) -> None:
        fake = Syscalls(pid=1)
        fake.kill_errors = [ProcessLookupError()]
        fake.reaps = [(77, 0)]
        with fake.installed():
            self.assertEqual(kill_and_reap(), 1)
        self.assertEqual(len(fake.calls), 3)

    def test_it_never_signals_anything_unless_it_is_pid_1(self) -> None:
        # The guard at the one place that sends the signal: a flag set by
        # mistake on a developer machine must never reach kill(-1).
        for pid in (2, 4242):
            with self.subTest(pid=pid):
                fake = Syscalls(pid=pid)
                with fake.installed(), self.assertRaises(RuntimeError):
                    kill_and_reap()
                self.assertEqual(fake.calls, [])

    def test_the_reap_is_bounded_when_killed_children_have_not_exited(self) -> None:
        # (0, 0): children exist but none has exited. The sweep waits for
        # them, polling, and gives up after reap_seconds; the next boundary
        # reaps what is left.
        fake = Syscalls(pid=1)
        fake.reaps = [(0, 0)] * 100000
        with fake.installed():
            started = time.monotonic()
            self.assertEqual(kill_and_reap(reap_seconds=0.3), 0)
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 2.0)
        polls = [call for call in fake.calls if call[0] == "waitpid"]
        self.assertGreater(len(polls), 2, "it gave up without waiting")

    def test_it_waits_for_a_killed_child_that_exits_late(self) -> None:
        fake = Syscalls(pid=1)
        fake.reaps = [(0, 0), (0, 0), (0, 0), (55, 9)]
        with fake.installed():
            self.assertEqual(kill_and_reap(reap_seconds=5.0), 1)

    def test_the_default_wait_is_short(self) -> None:
        # About 2 s: long enough for SIGKILLed processes to exit, short
        # enough that the run lock is never held long for a stuck one.
        self.assertEqual(ytdlp.REAP_SECONDS, 2.0)


class SweepDecisionTest(unittest.TestCase):
    """sweep_is_safe_here and the entry: on only as PID 1, with kill, SIGKILL and waitpid."""

    def test_only_pid_1_may_sweep(self) -> None:
        for pid, expected in ((1, True), (2, False), (4242, False)):
            with self.subTest(pid=pid), Syscalls(pid=pid).installed():
                self.assertIs(sweep_is_safe_here(), expected)

    def test_not_without_sigkill_or_waitpid(self) -> None:
        with Syscalls(pid=1).installed():
            # Control: with all of them, PID 1 may sweep.
            self.assertIs(sweep_is_safe_here(), True)
            for module, name in ((signal, "SIGKILL"), (os, "waitpid"), (os, "WNOHANG"), (os, "kill")):
                with self.subTest(missing=name), without(module, name):
                    self.assertIs(sweep_is_safe_here(), False)

    def run_main(self, pid: int) -> list[dict[str, Any]]:
        return self.run_main_logged(pid)[0]

    def run_main_logged(self, pid: int) -> tuple[list[dict[str, Any]], str]:
        # Not PID 1, the entry refuses to start unless FETCH_ALLOW_NO_SWEEP
        # permits it (NoSweepRefusalTest has the refusal); these tests are
        # about the flag the server gets, so they set the override there.
        environ = {} if pid == 1 else {"FETCH_ALLOW_NO_SWEEP": "true"}
        code, stderr, started, _order = start_entry(pid, environ)
        self.assertEqual(code, 0, stderr)
        return started, stderr

    def test_the_entry_turns_the_sweep_on_only_as_pid_1(self) -> None:
        self.assertEqual(self.run_main(1), [{"sweep_strays": True}])
        self.assertEqual(self.run_main(4242), [{"sweep_strays": False}])

    def test_the_entry_says_whether_it_sweeps(self) -> None:
        # Under an init (a daemon whose default is init, a deployment's
        # `init: true`, a shared PID namespace) the runner is not PID 1; with
        # FETCH_ALLOW_NO_SWEEP it serves without the sweep, and one stderr
        # line is how an operator sees that, distinct from the PID-1 line.
        on = SWEEP_LINE
        off = NO_SWEEP_LINE
        for pid, expected, other in ((1, on, off), (4242, off, on), (7, off, on)):
            with self.subTest(pid=pid):
                _started, stderr = self.run_main_logged(pid)
                lines = stderr.splitlines()
                self.assertEqual(lines.count(expected), 1, stderr)
                self.assertNotIn(other, lines)
                # Said before the server listens, so it heads the log.
                self.assertLess(lines.index(expected),
                                lines.index("strange_ringtone_runner listening on 127.0.0.1:8080"))

    def test_the_flag_defaults_to_off_everywhere_else(self) -> None:
        # Tests and tools that build the server or YtDlp themselves never
        # sweep, the in-image suite included, whose process is PID 1.
        self.assertIs(YtDlp(600, MIB, 7).sweep_strays, False)
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        server = RunnerServer(("127.0.0.1", 0), Settings(scratch_dir=Path(temp.name) / "scratch"))
        self.addCleanup(server.server_close)
        self.assertIs(server.ytdlp.sweep_strays, False)

    def test_the_server_passes_the_flag_to_its_yt_dlp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        settings = Settings(scratch_dir=Path(temp.name) / "scratch")
        server = RunnerServer(("127.0.0.1", 0), settings, sweep_strays=True)
        self.addCleanup(server.server_close)
        self.assertIs(server.ytdlp.sweep_strays, True)

    def test_a_server_asked_to_sweep_refuses_a_yt_dlp_that_does_not(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        settings = Settings(scratch_dir=Path(temp.name) / "scratch")
        with self.assertRaises(ValueError):
            RunnerServer(("127.0.0.1", 0), settings, ytdlp=StubYtDlp(600, MIB, 30), sweep_strays=True)
        # Refused before binding or touching the scratch directory.
        self.assertFalse((Path(temp.name) / "scratch").exists())


class NoSweepRefusalTest(unittest.TestCase):
    """Q4 (owner, 2026-11-05): where the sweep can't run, the entry exits 4 unless FETCH_ALLOW_NO_SWEEP=true.

    Under an init (a daemon whose default is init, a deployment's
    `init: true`, a pod with a shared PID namespace) the runner is not PID 1
    and can't sweep, which would quietly bring back the setsid survivor the
    sweep removes. So it refuses, and only an operator's explicit override,
    meant for a local run or a host that can't run the runner as PID 1, lets
    it serve without the sweep.
    """

    def test_not_pid_1_without_the_override_exits_4_and_never_builds_the_server(self) -> None:
        for environ in ({}, {"FETCH_ALLOW_NO_SWEEP": "false"}, {"FETCH_ALLOW_NO_SWEEP": ""},
                        {"FETCH_ALLOW_NO_SWEEP": "0"}, {"FETCH_ALLOW_NO_SWEEP": "no"}):
            with self.subTest(environ=environ):
                code, stderr, started, order = start_entry(7, environ)
                self.assertEqual(code, 4, stderr)
                self.assertEqual(runner_entry.EXIT_NO_SWEEP, 4)
                self.assertEqual(started, [])
                self.assertNotIn("server", order)
                lines = stderr.splitlines()
                self.assertEqual(len(lines), 1, stderr)
                refusal = lines[0]
                self.assertTrue(refusal.startswith("strange_ringtone_runner: refusing to start: "), refusal)
                # It names the variable, says what it is for and what it is
                # never for, and points at the docs.
                self.assertIn("FETCH_ALLOW_NO_SWEEP=true", refusal)
                self.assertIn("only for a local run or a host that can't run the runner as PID 1", refusal)
                self.assertIn("never for a deployment that can", refusal)
                self.assertIn("PID 1", refusal)
                self.assertNotIn("listening", stderr)
                self.assertNotIn(SWEEP_LINE, lines)
                self.assertNotIn(NO_SWEEP_LINE, lines)

    def test_pid_1_on_a_platform_without_the_calls_exits_4_too(self) -> None:
        # The sweep needs kill, SIGKILL, waitpid and WNOHANG as well as PID 1.
        for module, name in ((signal, "SIGKILL"), (os, "waitpid"), (os, "WNOHANG"), (os, "kill")):
            with self.subTest(missing=name):
                code, stderr, started, _order = start_entry(1, {}, missing=(module, name))
                self.assertEqual(code, 4, stderr)
                self.assertEqual(started, [])
                self.assertIn("FETCH_ALLOW_NO_SWEEP", stderr)

    def test_the_order_is_settings_routes_not_dumpable_then_this(self) -> None:
        # Settings (2) first: a bad override value is a configuration error.
        code, stderr, _started, order = start_entry(7, {"FETCH_MAX_SECONDS": "0"})
        self.assertEqual((code, order), (2, []), stderr)
        # Then the route check (3): a default route wins over the sweep.
        code, stderr, _started, order = start_entry(7, {}, routes=(DefaultRoute("IPv4", "eth0"),))
        self.assertEqual((code, order), (3, ["routes"]), stderr)
        self.assertNotIn("FETCH_ALLOW_NO_SWEEP", stderr)
        # Then the non-dumpable step (1), then this check (4).
        code, stderr, _started, order = start_entry(7, {})
        self.assertEqual((code, order), (4, ["routes", "not dumpable"]), stderr)
        # With the override, the server comes last.
        code, stderr, _started, order = start_entry(7, {"FETCH_ALLOW_NO_SWEEP": "true"})
        self.assertEqual((code, order), (0, ["routes", "not dumpable", "server"]), stderr)

    def test_the_override_serves_without_the_sweep_and_says_so(self) -> None:
        for value in ("true", "1", "yes", "TRUE"):
            with self.subTest(value=value):
                code, stderr, started, _order = start_entry(7, {"FETCH_ALLOW_NO_SWEEP": value})
                self.assertEqual(code, 0, stderr)
                self.assertEqual(started, [{"sweep_strays": False}])
                lines = stderr.splitlines()
                self.assertEqual(lines.count(NO_SWEEP_LINE), 1, stderr)
                self.assertNotIn("refusing", stderr)
                self.assertLess(lines.index(NO_SWEEP_LINE),
                                lines.index("strange_ringtone_runner listening on 127.0.0.1:8080"))

    def test_as_pid_1_it_sweeps_whatever_the_variable_says(self) -> None:
        # The override permits serving without the sweep; it never turns the
        # sweep off where it can run.
        for environ in ({}, {"FETCH_ALLOW_NO_SWEEP": "true"}, {"FETCH_ALLOW_NO_SWEEP": "false"}):
            with self.subTest(environ=environ):
                code, stderr, started, _order = start_entry(1, environ)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(started, [{"sweep_strays": True}])
                self.assertEqual(stderr.splitlines().count(SWEEP_LINE), 1, stderr)
                self.assertNotIn(NO_SWEEP_LINE, stderr.splitlines())

    def test_a_bad_value_is_a_configuration_error_never_permission(self) -> None:
        for value in ("on", "2", " true", "true ", "y", "١"):
            for pid in (7, 1):
                with self.subTest(value=value, pid=pid):
                    code, stderr, started, order = start_entry(pid, {"FETCH_ALLOW_NO_SWEEP": value})
                    self.assertEqual(code, 2, stderr)
                    self.assertEqual((started, order), ([], []))
                    self.assertIn("strange_ringtone_runner: configuration error", stderr)
                    self.assertIn("FETCH_ALLOW_NO_SWEEP", stderr)

    def test_the_setting_is_parsed_with_the_route_overrides_helper_and_defaults_to_false(self) -> None:
        self.assertIs(Settings().allow_no_sweep, False)
        self.assertIs(Settings.from_env({}).allow_no_sweep, False)
        self.assertIs(Settings.from_env({"FETCH_ALLOW_NO_SWEEP": "true"}).allow_no_sweep, True)
        # The lowercase spelling is not the setting (a plain mapping here:
        # Windows' os.environ would fold the case).
        self.assertIs(Settings.from_env({"fetch_allow_no_sweep": "true"}).allow_no_sweep, False)
        # The same helper, with the same default, for both overrides: one
        # set of boolean words, so neither can drift into accepting a typo.
        with mock.patch.object(runner_config, "_parse_bool", wraps=runner_config._parse_bool) as parse:
            Settings.from_env({})
        self.assertIn(mock.call({}, "FETCH_ALLOW_DEFAULT_ROUTE", False), parse.call_args_list)
        self.assertIn(mock.call({}, "FETCH_ALLOW_NO_SWEEP", False), parse.call_args_list)
        # Read by the runner only: the fetcher never looks it up.
        from strange_ringtone_fetch.config import Settings as FetcherSettings
        from test_config import ReadRecorder
        environ = ReadRecorder({"PUBLIC_ORIGIN": "https://example.internal"})
        FetcherSettings.from_env(environ)
        self.assertNotIn("FETCH_ALLOW_NO_SWEEP", environ.looked_up)
        self.assertIn("FETCH_ALLOW_NO_SWEEP", runner_config.RUNNER_VARIABLES)


class YtDlpSweepTest(unittest.TestCase):
    """YtDlp.fetch with the stub yt-dlp: the sweep at both boundaries when on, and never when off."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        patcher = mock.patch.object(ytdlp, "DRAIN_SECONDS", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="job-", dir=self.temp))

    def fetch(self, runner: YtDlp, video_id: str) -> str:
        try:
            runner.fetch(video_id, self.job())
        except FetchError as error:
            return error.code
        return "done"

    def test_off_it_never_signals_minus_one_even_as_pid_1(self) -> None:
        fake = Syscalls(pid=1)
        with fake.installed(record_popen=True):
            runner = StubYtDlp(600, MIB, 1)
            outcomes = [self.fetch(runner, video_id) for video_id in (VIDEO_ID, "failing____", "hanging____")]
        self.assertEqual(outcomes, ["done", "failed", "timeout"])
        self.assertEqual(fake.calls, [])
        # Control: the runs did happen.
        self.assertEqual(fake.events, ["popen"] * 3)

    def test_on_it_sweeps_before_and_after_every_run_with_no_popen_in_flight(self) -> None:
        cases = ((VIDEO_ID, "done"), ("failing____", "failed"), ("tooLong____", "too_long"), ("hanging____", "timeout"))
        for video_id, outcome in cases:
            with self.subTest(video_id=video_id):
                fake = Syscalls(pid=1)
                with fake.installed(record_popen=True):
                    self.assertEqual(self.fetch(StubYtDlp(600, MIB, 1, sweep_strays=True), video_id), outcome)
                self.assertEqual(fake.events, ["sweep", "popen", "sweep"])
                self.assertEqual(fake.sweeps(), [("kill", -1, SIGKILL)] * 2)
                self.assertFalse(fake.in_flight, "a sweep ran while yt-dlp's Popen was in flight")
                self.assertTrue(all(proc.returncode is not None for proc in fake.popens))

    def test_a_run_that_cannot_start_was_still_swept_before(self) -> None:
        fake = Syscalls(pid=1)
        with fake.installed(record_popen=True):
            runner = YtDlp(600, MIB, 30, command=(str(self.temp / "missing-python"),), sweep_strays=True)
            self.assertEqual(self.fetch(runner, VIDEO_ID), "failed")
        self.assertEqual(fake.events, ["sweep"])

    def test_on_but_not_pid_1_it_fails_the_job_and_signals_nothing(self) -> None:
        fake = Syscalls(pid=4242)
        with fake.installed(record_popen=True):
            self.assertEqual(self.fetch(StubYtDlp(600, MIB, 30, sweep_strays=True), VIDEO_ID), "failed")
        self.assertEqual(fake.calls, [])
        # Failed before yt-dlp starts: no run next to a stray.
        self.assertEqual(fake.popens, [])

    def test_a_sweep_that_fails_before_the_run_starts_no_run(self) -> None:
        fake = Syscalls(pid=1)
        fake.kill_errors = [PermissionError()]
        with fake.installed(record_popen=True):
            self.assertEqual(self.fetch(StubYtDlp(600, MIB, 30, sweep_strays=True), VIDEO_ID), "failed")
        self.assertEqual(fake.popens, [])

    def test_a_sweep_that_fails_after_the_run_fails_the_job(self) -> None:
        # A stray may still be running next to the files read_result would
        # check, so even a good run is not trusted.
        fake = Syscalls(pid=1)
        fake.kill_errors = [None, PermissionError()]
        with fake.installed(record_popen=True):
            self.assertEqual(self.fetch(StubYtDlp(600, MIB, 30, sweep_strays=True), VIDEO_ID), "failed")
        self.assertEqual(fake.events, ["sweep", "popen", "sweep"])

    def test_no_sweep_while_yt_dlps_own_wait_has_not_reaped_it(self) -> None:
        # If the wait for yt-dlp itself fails (an exception inside
        # communicate() and wait()), Popen has not reaped its child: a
        # waitpid(-1) now would take that exit status from under it. The
        # closing sweep is skipped, the error propagates, and the next
        # run's opening sweep collects what is left.
        fake = Syscalls(pid=1)

        class Broken(RuntimeError):
            pass

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise Broken()

        def break_waits(proc: subprocess.Popen[bytes]) -> None:
            proc.communicate = broken  # type: ignore[method-assign]
            proc.wait = broken  # type: ignore[method-assign]

        fake.popen_hook = break_waits
        with fake.installed(record_popen=True):
            with self.assertRaises(Broken):
                StubYtDlp(600, MIB, 30, sweep_strays=True).fetch(VIDEO_ID, self.job())
        self.assertEqual(fake.events, ["sweep", "popen"])
        # Clean up the stub run with the real methods (its group was killed).
        # wait(), not communicate(): the failed run already closed the pipes,
        # and communicate() would start reader threads on them that fail with
        # "read of closed file" on Windows.
        for proc in fake.popens:
            del proc.communicate, proc.wait
            with contextlib.suppress(OSError):
                proc.kill()
            proc.wait(timeout=30)

    def test_concurrent_fetches_never_sweep_while_another_run_is_in_flight(self) -> None:
        # The runner's lock allows one run at a time; YtDlp keeps the rule
        # itself while sweeping, so waitpid(-1) never steals a child's exit
        # status. Two threads on one YtDlp: the second starts while the
        # first (a hung run, timed out after 1 s) is in flight.
        fake = Syscalls(pid=1)
        outcomes: dict[str, str] = {}
        with fake.installed(record_popen=True):
            runner = StubYtDlp(600, MIB, 1, sweep_strays=True)

            def run(video_id: str) -> None:
                outcomes[video_id] = self.fetch(runner, video_id)

            first = threading.Thread(target=run, args=("hanging____",))
            first.start()
            deadline = time.monotonic() + 10
            while not fake.popens and time.monotonic() < deadline:
                time.sleep(0.01)
            second = threading.Thread(target=run, args=(VIDEO_ID,))
            second.start()
            first.join(30)
            second.join(30)
        self.assertEqual(outcomes, {"hanging____": "timeout", VIDEO_ID: "done"})
        self.assertFalse(fake.in_flight, "a sweep ran while another run's Popen was in flight")
        self.assertEqual(fake.events, ["sweep", "popen", "sweep", "sweep", "popen", "sweep"])


class RunnerServerSweepTest(unittest.TestCase):
    """The runner answers a POST only after the run's closing sweep, under its lock."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.scratch = Path(temp.name) / "scratch"
        self.log = LogCapture()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_both_boundaries_sweep_before_the_post_is_answered(self) -> None:
        fake = Syscalls(pid=1)
        # The closing sweep is slow: an answer written before it would reach
        # the client with one sweep recorded, not two.
        fake.kill_delays = [0.0, 0.5]
        with fake.installed(record_popen=True):
            settings = Settings(scratch_dir=self.scratch)
            server = _TestRunnerServer(
                ("127.0.0.1", 0), settings, ytdlp=StubYtDlp(600, MIB, 30, sweep_strays=True), sweep_strays=True,
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                body = job_body(VIDEO_ID, token())
                headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
                request = build_request("POST", "/jobs", headers, body)
                # exchange() returns at the answer's last byte, which is when
                # the sweeps are counted; the runner lets go of the lock just
                # after that (before it closes), so that is waited for, bounded.
                response = exchange(port, request)
                sweeps_at_answer = list(fake.events)
                wait_for(lambda: not server.busy.locked(), "the runner to let go of its lock")
                # While the run held the lock, nothing else ran; it is free now.
                self.assertTrue(server.busy.acquire(blocking=False))
                server.busy.release()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(10)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(json.loads(response.body)["status"], "done")
        self.assertEqual(sweeps_at_answer, ["sweep", "popen", "sweep"])
        self.assertFalse(fake.in_flight)


@unittest.skipUnless(IN_IMAGE, "the real sweep runs only in the fetcher image's test container")
class RealSweepTest(unittest.TestCase):
    """In the image, as PID 1: a run's same-group, setsid and PID-hopping leftovers are killed and reaped."""

    def setUp(self) -> None:
        # First, before anything that could sweep: a failure, not a skip
        # (see IN_IMAGE). kill_and_reap checks the PID again on its own.
        self.assertEqual(os.getpid(), 1, "the in-image suite must run as its container's PID 1, with no init "
                         "(no `docker run --init`, no daemon default of init): RealSweepTest proves the sweep there")
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        # Whatever happens below, nothing this test started outlives it.
        self.addCleanup(kill_and_reap)

    def job(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="job-", dir=self.temp))

    @staticmethod
    def others() -> list[tuple[int, str]]:
        """(pid, state) of every process in this PID namespace but PID 1; Z is a zombie."""
        rows = []
        for name in os.listdir("/proc"):
            if not name.isdigit() or name == "1":
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="ascii") as handle:
                    text = handle.read()
            except OSError:
                continue
            rows.append((int(name), text.rsplit(")", 1)[1].split()[0]))
        return sorted(rows)

    def leftover_runner(self, sweep: bool) -> StubYtDlp:
        return StubYtDlp(600, MIB, 30, command=(sys.executable, "-I", str(LEFTOVER_STUB)), sweep_strays=sweep)

    def test_a_run_leaves_nothing_behind_and_the_next_run_works(self) -> None:
        # Start from a clean namespace: earlier tests' leftovers are not ours.
        kill_and_reap()
        self.assertEqual(self.others(), [])

        # Control: without the sweep, the run succeeds and leaves processes
        # the group kill can't reach (the setsid sleeper and the hopper)
        # plus zombies for PID 1.
        download = self.leftover_runner(sweep=False).fetch(VIDEO_ID, self.job())
        self.assertTrue(download.audio.is_file())
        time.sleep(0.5)
        left = self.others()
        live = [row for row in left if row[1] != "Z"]
        zombies = [row for row in left if row[1] == "Z"]
        self.assertGreaterEqual(len(live), 2, f"the stub left too little to prove anything: {left}")
        self.assertGreaterEqual(len(zombies), 1, f"no zombie for PID 1 to reap: {left}")
        print(f"\nRealSweepTest control (no sweep): {len(live)} live, {len(zombies)} zombies left",
              file=sys.stderr)
        kill_and_reap()
        self.assertEqual(self.others(), [])

        # The sweep on: the same run ends with nothing else in /proc, no
        # zombie either, and its result intact.
        download = self.leftover_runner(sweep=True).fetch(VIDEO_ID, self.job())
        self.assertEqual(download.audio.read_bytes(), b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))
        self.assertEqual(self.others(), [], "a run left a process or a zombie behind")
        # And again after the hopper had time to run: nothing came back.
        time.sleep(0.3)
        self.assertEqual(self.others(), [])

        # The next run still works (plain stub, sweep on).
        runner = StubYtDlp(600, MIB, 30, command=STUB_COMMAND, sweep_strays=True)
        download = runner.fetch(VIDEO_ID, self.job())
        self.assertEqual(download.metadata.duration, 215.5)
        self.assertEqual(self.others(), [])
        print("RealSweepTest: as PID 1, the sweep killed and reaped every leftover", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
