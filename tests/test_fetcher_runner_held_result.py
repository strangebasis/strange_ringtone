"""A job is answered done only when its result is where the runner serves it from (docs/PLAN.md 6.1, R7).

A run has the runner's UID. One that leaves a link in place of results/
must not have the runner's own move of the audio (Results.hold) follow the
link out of the scratch directory: the move is refused, nothing lands where
the link points, the clean-up after the run removes the link and makes
results/ again, and the job fails closed instead of answering done for audio
nobody can collect.
"""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch.metadata import Download
from test_fetcher_runner import MIB, OTHER_ID, RunnerServerTestCase, token
from test_fetcher_runner_leftovers import can_symlink
from test_fetcher_ytdlp import VIDEO_ID, StubYtDlp


class LinkingStubYtDlp(StubYtDlp):
    """The stub's run, then a link in place of results/ to a directory outside, as such a run would leave it."""

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        download = super().fetch(video_id, job_dir)
        scratch = job_dir.parent
        outside = scratch.parent / "outside_results"
        outside.mkdir()
        # Empty: the runner discards every held result before a run starts.
        (scratch / "results").rmdir()
        os.symlink(outside, scratch / "results", target_is_directory=True)
        return download


class HeldResultTest(RunnerServerTestCase):
    def test_a_result_moved_through_a_link_is_never_answered_done(self) -> None:
        if not can_symlink(self.temp):
            self.skipTest("this account can't create symlinks")
        self.server.ytdlp = LinkingStubYtDlp(600, MIB, 30, log=self.runs_log)
        job = token()
        response = self.post(VIDEO_ID, job)
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        self.assertJsonError(self.audio(job), 404, "not_found")
        # Nothing was moved through the link: the directory it pointed to
        # is as empty as the run left it.
        self.assertEqual(list((self.temp / "outside_results").iterdir()), [])
        # The link went as a link; results/ is a plain directory again.
        self.assertTrue(stat.S_ISDIR(os.lstat(self.scratch / "results").st_mode))
        self.assertEqual(self.scratch_contents(), [])
        # The next job, without the link, is done and collected.
        self.server.ytdlp = StubYtDlp(600, MIB, 30, log=self.runs_log)
        job = self.done(OTHER_ID)
        audio = self.audio(job)
        self.assertEqual(audio.status, 200, audio.body)
        self.assertEqual(audio.body, b"STUB-M4A\n" + OTHER_ID.encode("ascii"))


if __name__ == "__main__":
    unittest.main()
