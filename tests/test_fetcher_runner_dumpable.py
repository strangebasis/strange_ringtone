"""The runner makes itself non-dumpable before it serves (docs/PLAN.md 6.1, "What it rests on").

yt-dlp runs as the runner's UID in the runner's container and is not the
runner's ancestor. Where the host's kernel.yama.ptrace_scope is 0, a
same-UID process may ptrace a dumpable one, so a malicious yt-dlp could
attach to the runner, PID 1, and plant code there that the kill(-1) sweep
never reaches; on any host it can read a dumpable process's
/proc/<pid>/environ and maps. A non-dumpable target needs CAP_SYS_PTRACE
for all of that, which the container doesn't have.

The host tests replace ctypes and the entry's helper, so no test process
here is made non-dumpable. The real check (RealNotDumpableTest) runs on
Linux, the in-image suite included, in a process tree of its own: a parent
probes a child that did or did not call the runner's helper, so the
control and the check differ only in that call.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
import strange_ringtone.config as core_config
from strange_ringtone_runner import __main__ as runner_entry
from test_fetcher_server import LogCapture

# Found from the imported packages, so the same paths work in the checkout
# and in the image (/app/fetcher, /app/core).
FETCHER_DIR = Path(runner_entry.__file__).resolve().parent.parent
CORE_DIR = Path(core_config.__file__).resolve().parent.parent

# Capabilities that let a process read or trace a non-dumpable one anyway
# (linux/capability.h): CAP_DAC_OVERRIDE, CAP_DAC_READ_SEARCH, CAP_SYS_PTRACE.
_OVERRIDING_CAPS = (1 << 1) | (1 << 2) | (1 << 19)


def _effective_caps() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("CapEff:"):
                    return int(line.split()[1], 16)
    except OSError:
        pass
    return 0


def _ptrace_scope() -> int:
    """Yama's ptrace_scope, or 0 where Yama isn't there (the kernel's own rules alone)."""
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 0


# The probing parent: it starts the target, which calls the runner's own
# helper when told to, then tries what a same-UID process could do to it.
# Being the target's parent, it may ptrace a dumpable target under
# ptrace_scope 1 too, which is what the control needs.
_PROBER = r'''
import ctypes, errno, json, os, signal, subprocess, sys
python, core, fetcher, mode = sys.argv[1:5]
target_code = (
    "import sys\n"
    "sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
    "from strange_ringtone_runner.__main__ import make_not_dumpable\n"
    "if sys.argv[3] == 'hardened':\n"
    "    make_not_dumpable()\n"
    "sys.stdout.write('ready\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)
target = subprocess.Popen([python, "-I", "-c", target_code, core, fetcher, mode], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env={"PATH": os.environ.get("PATH", ""), "MARKER": "x"})
result = {}
try:
    result["ready"] = target.stdout.readline().decode("ascii", "replace").strip()
    for name in ("environ", "maps"):
        try:
            with open(f"/proc/{target.pid}/{name}", "rb") as handle:
                result[name] = "read" if handle.read() else "empty"
        except OSError as error:
            result[name] = errno.errorcode.get(error.errno, str(error.errno))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    libc.ptrace.restype = ctypes.c_long
    # PTRACE_ATTACH (16): the attach is the permission check.
    if libc.ptrace(16, target.pid, None, None) == 0:
        result["ptrace"] = "attached"
    else:
        result["ptrace"] = errno.errorcode.get(ctypes.get_errno(), "?")
finally:
    # SIGKILL ends a ptrace-stopped tracee too. Sent and reaped here, not by
    # Popen, whose kill() polls first and would take the ptrace stop for an
    # exit, and then send nothing.
    os.kill(target.pid, signal.SIGKILL)
    while True:
        _pid, status = os.waitpid(target.pid, 0)
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            break
    target.returncode = -9
    target.stdin.close()
    target.stdout.close()
print(json.dumps(result))
'''


class MakeNotDumpableTest(unittest.TestCase):
    """The helper itself, with ctypes replaced: prctl(PR_SET_DUMPABLE, 0), read back, or OSError; Linux only."""

    def run_helper(self, answers: list[int], errno_value: int = 0, platform: str = "linux") -> mock.MagicMock:
        lib = mock.MagicMock()
        lib.prctl.side_effect = list(answers)
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(runner_entry.ctypes, "CDLL", return_value=lib) as cdll, \
                mock.patch.object(runner_entry.ctypes, "get_errno", return_value=errno_value):
            try:
                runner_entry.make_not_dumpable()
            finally:
                self.cdll = cdll
        return lib

    def test_it_sets_the_flag_and_reads_it_back(self) -> None:
        lib = self.run_helper([0, 0])
        self.cdll.assert_called_once_with(None, use_errno=True)
        # PR_SET_DUMPABLE (4) to 0, then PR_GET_DUMPABLE (3).
        self.assertEqual(lib.prctl.call_args_list, [mock.call(4, 0, 0, 0, 0), mock.call(3, 0, 0, 0, 0)])
        self.assertEqual((runner_entry.PR_SET_DUMPABLE, runner_entry.PR_GET_DUMPABLE), (4, 3))
        # Declared as the kernel reads them: an int option, then unsigned
        # longs, so no stray upper bits make PR_SET_DUMPABLE refuse.
        self.assertEqual(lib.prctl.argtypes,
                         [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong])

    def test_a_refused_prctl_raises_with_its_errno(self) -> None:
        with self.assertRaises(OSError) as raised:
            self.run_helper([-1], errno_value=errno.EINVAL)
        self.assertEqual(raised.exception.errno, errno.EINVAL)

    def test_a_process_still_dumpable_after_the_call_raises(self) -> None:
        with self.assertRaises(OSError):
            self.run_helper([0, 1])

    def test_nothing_happens_off_linux(self) -> None:
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                lib = self.run_helper([], platform=platform)
                self.cdll.assert_not_called()
                lib.prctl.assert_not_called()


class EntryNotDumpableTest(unittest.TestCase):
    """main() makes itself non-dumpable before RunnerServer exists, and exits 1 without serving if it can't."""

    def run_main(self, helper: Any) -> tuple[int, str, list[str]]:
        order: list[str] = []
        log = LogCapture()

        class FakeServer:
            def __init__(self, address: tuple[str, int], settings: Any, **options: Any) -> None:
                order.append("server")

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        def record() -> None:
            order.append("not dumpable")
            helper()

        # Not PID 1 (the sweep decision is faked to False), so the override
        # that lets the entry serve without the sweep (Q4).
        environ = {"FETCH_ALLOW_DEFAULT_ROUTE": "true", "FETCH_ALLOW_NO_SWEEP": "true"}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(runner_entry, "RunnerServer", FakeServer), \
                mock.patch.object(runner_entry, "make_not_dumpable", record), \
                mock.patch.object(runner_entry, "sweep_is_safe_here", return_value=False), \
                mock.patch("signal.signal"), \
                contextlib.redirect_stderr(log):
            code = runner_entry.main(["--bind", "127.0.0.1"])
        return code, log.getvalue(), order

    def test_it_is_called_once_before_the_server(self) -> None:
        code, stderr, order = self.run_main(lambda: None)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(order, ["not dumpable", "server"])

    def test_a_failure_exits_1_and_never_serves(self) -> None:
        def refuse() -> None:
            raise OSError(errno.EPERM, "prctl(PR_SET_DUMPABLE, 0) failed: Operation not permitted")

        code, stderr, order = self.run_main(refuse)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(order, ["not dumpable"])
        self.assertIn("strange_ringtone_runner: cannot start: ", stderr)
        self.assertIn("PR_SET_DUMPABLE", stderr)
        self.assertNotIn("listening", stderr)


@unittest.skipUnless(sys.platform.startswith("linux"), "prctl and /proc are Linux's")
@unittest.skipIf(_effective_caps() & _OVERRIDING_CAPS, "a process with these capabilities reads and traces anyone")
class RealNotDumpableTest(unittest.TestCase):
    """On Linux: after the runner's helper, a same-UID process can't read the target's /proc entries or attach."""

    def probe(self, mode: str) -> dict[str, str]:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _PROBER, sys.executable, str(CORE_DIR), str(FETCHER_DIR), mode],
            env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result: dict[str, str] = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(result.get("ready"), "ready", proc.stderr)
        return result

    def test_a_same_uid_process_can_neither_read_nor_attach(self) -> None:
        # Control: the same target without the call is open to its parent.
        plain = self.probe("plain")
        self.assertEqual(plain["environ"], "read", plain)
        self.assertEqual(plain["maps"], "read", plain)
        if _ptrace_scope() <= 1:
            self.assertEqual(plain["ptrace"], "attached", plain)

        hardened = self.probe("hardened")
        self.assertIn(hardened["environ"], ("EACCES", "EPERM"), hardened)
        self.assertIn(hardened["maps"], ("EACCES", "EPERM"), hardened)
        self.assertEqual(hardened["ptrace"], "EPERM", hardened)
        print(f"\nRealNotDumpableTest: ptrace_scope {_ptrace_scope()}, control {plain}, hardened {hardened}",
              file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
