"""The runner's worst-case count of threads counts every thread the runner starts (docs/PLAN.md, section 6.1).

compose.example.yaml sets the runner's pids_limit to at least twice its
worst case, which tests/compose/test_compose_topology.py computes from the
code's caps plus RUNNER_THREADS: the threads the runner starts besides its
main thread and its request workers. Here they are counted in the code
itself: the result sweeper, which a server starts, and what one run adds
while it goes on, so a thread added without being counted fails.
"""
from __future__ import annotations

import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch.metadata import FetchError
from strange_ringtone_runner.config import Settings
from test_fetcher_runner import MIB, _TestRunnerServer
from test_fetcher_ytdlp import VIDEO_ID, StubYtDlp

TOPOLOGY = Path(__file__).resolve().parent / "compose" / "test_compose_topology.py"
# How long the stub's run hangs before the runner's timeout ends it; the
# threads are sampled meanwhile.
RUN_SECONDS = 2


def counted_runner_threads() -> int:
    """RUNNER_THREADS as the compose tests define it: a sum of whole numbers."""
    match = re.search(r"^RUNNER_THREADS = ([0-9 +]+)$", TOPOLOGY.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise AssertionError(f"no RUNNER_THREADS line in {TOPOLOGY}")
    return sum(int(part) for part in match[1].split("+"))


def other_threads(*known: threading.Thread) -> set[threading.Thread]:
    return set(threading.enumerate()) - set(known)


class RunnerThreadsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)

    def server_threads(self) -> set[threading.Thread]:
        """The threads a runner server starts of its own, before any request."""
        before = set(threading.enumerate())
        server = _TestRunnerServer(("127.0.0.1", 0), Settings(scratch_dir=self.temp / "scratch"))
        try:
            return set(threading.enumerate()) - before
        finally:
            server.server_close()

    def run_threads(self) -> tuple[set[str], set[threading.Thread]]:
        """The names of the threads one run adds while it goes on, and the threads still there after it."""
        job = self.temp / "job"
        job.mkdir()
        ytdlp = StubYtDlp(600, MIB, RUN_SECONDS, mode="hang")
        before = set(threading.enumerate())
        outcome: list[BaseException] = []

        def run() -> None:
            try:
                ytdlp.fetch(VIDEO_ID, job)
            except BaseException as error:  # checked on the test's thread
                outcome.append(error)

        caller = threading.Thread(target=run, daemon=True)
        caller.start()
        seen: set[str] = set()
        while caller.is_alive():
            seen |= {thread.name for thread in other_threads(caller, *before)}
            time.sleep(0.05)
        caller.join(30)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], FetchError)
        return seen, other_threads(caller, *before)

    def test_runner_threads_counts_the_sweeper_and_what_a_run_adds(self) -> None:
        server = self.server_threads()
        run, _left = self.run_threads()
        self.assertEqual({thread.name for thread in server}, {"runner-result-sweeper"})
        self.assertEqual(run, {"ytdlp-stderr"})
        self.assertEqual(counted_runner_threads(), len(server) + len(run))

    def test_a_runs_thread_ends_with_the_run(self) -> None:
        _run, left = self.run_threads()
        deadline = time.monotonic() + 5
        while any(thread.is_alive() for thread in left) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual({thread.name for thread in left if thread.is_alive()}, set())


if __name__ == "__main__":
    unittest.main()
