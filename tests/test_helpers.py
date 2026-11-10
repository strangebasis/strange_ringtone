"""The tests' own look at whether a process still runs (tests/helpers.py, alive()).

The kill tests (the core's render timeout, the runner's R3) ask it about a
process they did not start and that something else reaps. A wrong "still
runs" fails a check that held, and a wrong "gone" passes one that didn't, so
both answers are pinned here: with the two looks scripted, on every
platform, and against a real process where there is a /proc.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from typing import Any
from unittest import mock

import helpers
from helpers import alive

PID = 4242


def stat_line(state: str, name: str = "python3") -> bytes:
    """A /proc/<pid>/stat line as Linux prints it: the PID, the name in brackets, the state, then numbers."""
    return f"{PID} ({name}) {state} 1 {PID} {PID} 0 -1 4194560 120 0 0 0 0 0 0 0 20 0 1 0 5000 0 0\n".encode("ascii")


class GoneWhileRead(io.BytesIO):
    """A stat file that opened and then lost its process: the read fails as the kernel's does."""

    def read(self, *args: Any) -> bytes:
        raise ProcessLookupError()


class ScriptedLooksTest(unittest.TestCase):
    """alive() with its two looks scripted: signal 0, then /proc/<pid>/stat."""

    def ask(self, kills: list[Any], stats: list[Any]) -> bool:
        """alive(PID) with these answers to signal 0 and these stat files, each used once and in order.

        An exception is raised by the look, None is a signal that was
        delivered, and bytes are a stat file's content.
        """
        self.kill = mock.Mock(side_effect=kills)
        self.open = mock.Mock(side_effect=[io.BytesIO(stat) if isinstance(stat, bytes) else stat for stat in stats])
        with mock.patch.object(helpers.os, "kill", self.kill), \
                mock.patch.object(helpers, "open", self.open, create=True):
            return alive(PID)

    def test_no_such_process_is_gone(self) -> None:
        self.assertFalse(self.ask([ProcessLookupError()], []))
        self.kill.assert_called_once_with(PID, 0)
        self.open.assert_not_called()

    def test_a_process_of_another_user_runs(self) -> None:
        # Signal 0 was refused, so there is a process to refuse it for.
        self.assertTrue(self.ask([PermissionError()], []))
        self.open.assert_not_called()

    def test_a_process_in_any_living_state_runs(self) -> None:
        # Running, sleeping, in an uninterruptible wait, stopped, traced, idle (proc(5)).
        for state in ("R", "S", "D", "T", "t", "I"):
            with self.subTest(state=state):
                self.assertTrue(self.ask([None], [stat_line(state)]))
                self.kill.assert_called_once_with(PID, 0)
                self.open.assert_called_once_with(f"/proc/{PID}/stat", "rb")

    def test_a_zombie_is_gone(self) -> None:
        self.assertFalse(self.ask([None], [stat_line("Z")]))

    def test_a_process_its_parent_is_collecting_is_gone(self) -> None:
        # State X shows between the parent's wait and the release of the PID.
        self.assertFalse(self.ask([None], [stat_line("X")]))

    def test_the_state_is_the_field_after_the_name(self) -> None:
        # A process chooses its own name, brackets and spaces included.
        self.assertTrue(self.ask([None], [stat_line("S", name="a) Z (b")]))
        self.assertFalse(self.ask([None], [stat_line("Z", name="a) S (b")]))

    def test_reaped_between_the_two_looks_is_gone(self) -> None:
        # The flaw this had: a zombie answered signal 0, its parent reaped it,
        # and the stat file that could no longer be read was taken for a
        # running process, right after a look that had said "gone". The kill
        # tests then failed now and then, with nothing left running.
        unreadable = {
            "no stat file left to open": FileNotFoundError(),
            "no process left to open it for": ProcessLookupError(),
            "opened, then no process left to read": GoneWhileRead(),
        }
        for how, stat in unreadable.items():
            with self.subTest(how=how):
                self.assertFalse(self.ask([None, ProcessLookupError()], [stat]))
                self.assertEqual(self.kill.call_args_list, [mock.call(PID, 0)] * 2)
                self.assertEqual(self.open.call_count, 1)

    def test_without_a_proc_to_read_a_pid_that_answers_runs(self) -> None:
        # Not Linux: there is no state to read, so a zombie can't be told
        # apart, and "runs" is the answer that can't pass a kill test by
        # mistake. Two looks and no more: this never waits for a /proc.
        self.assertTrue(self.ask([None, None], [FileNotFoundError(), FileNotFoundError()]))
        self.assertEqual(self.kill.call_count, 2)
        self.assertEqual(self.open.call_count, 2)

    def test_a_stat_file_that_cannot_be_parsed_is_read_again(self) -> None:
        for garbled in (b"", b"4242 (python3", b"4242 (python3)", b"4242 (python3) "):
            with self.subTest(garbled=garbled):
                self.assertFalse(self.ask([None, None], [garbled, stat_line("Z")]))
                self.assertTrue(self.ask([None, None], [garbled, stat_line("S")]))
                # Still no answer: counted as running, like a missing /proc.
                self.assertTrue(self.ask([None, None], [garbled, garbled]))


@unittest.skipUnless(os.path.exists(f"/proc/{os.getpid()}/stat"), "needs Linux's /proc")
class RealProcessTest(unittest.TestCase):
    """alive() against the kernel's own stat file: a child of this test, from running to reaped."""

    def test_a_child_runs_then_is_a_zombie_then_is_reaped(self) -> None:
        proc = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        # Last in, first out: killed, then waited for, whatever happens below.
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertTrue(alive(proc.pid), "a running child counts as gone")
        proc.kill()
        # Wait for the death without collecting it: the child is now a
        # zombie, and keeps its PID, until this test reaps it.
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        self.assertFalse(alive(proc.pid), "a zombie counts as running")
        proc.wait()
        self.assertFalse(alive(proc.pid), "a reaped child counts as running")


if __name__ == "__main__":
    unittest.main()
