"""A failed run's error code is chosen from the whole tail of its stderr (docs/PLAN.md, section 6.1, R8).

The runner reads a run's stderr on a thread of its own. When the run fails,
that reader may still be behind: the tail is waited for, within the drain's
bound, before map_failure reads it, or the line that names the error could
be missing and the code would be the generic "failed". On Linux the reader
wakes as soon as data arrives, so here it is slowed on purpose.
"""
from __future__ import annotations

import time
import unittest
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_runner import ytdlp
from test_fetcher_ytdlp import VIDEO_ID, StubTestCase

# How long the reader takes over each read it is about to make; well under
# the drain's bound (ytdlp.DRAIN_SECONDS).
READER_DELAY_SECONDS = 0.3


class StderrTailTest(StubTestCase):
    def test_the_tail_is_complete_before_the_failure_is_mapped(self) -> None:
        real = ytdlp._readable

        def slow(fd: int, timeout: float) -> Any:
            available = real(fd, timeout)
            if available:
                time.sleep(READER_DELAY_SECONDS)
            return available

        self.assertLess(READER_DELAY_SECONDS * 4, ytdlp.DRAIN_SECONDS)
        with mock.patch.object(ytdlp, "_readable", slow):
            self.assertEqual(self.fetch_error(VIDEO_ID, mode="blocked"), "blocked")
            self.assertEqual(self.fetch_error(VIDEO_ID, mode="not_found"), "not_found")


if __name__ == "__main__":
    unittest.main()
