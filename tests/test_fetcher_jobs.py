"""The fetch job queue (strange_ringtone_fetch.jobs): one worker, a bounded queue, one held result, a TTL."""
from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import jobs as jobs_module
from strange_ringtone_fetch.jobs import JOB_ID_RE, Jobs, QueueFull
from strange_ringtone_fetch.ytdlp import Download, FetchError, Metadata
from test_fetcher_ytdlp import StubYtDlp

VIDEO_ID = "dQw4w9WgXcQ"
OTHER_ID = "abcdefghijk"
DEADLINE = 10.0
METADATA = Metadata(title="A Title", duration=10.5, heatmap=({"start": 0.0, "end": 10.5, "value": 1.0},))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeRunner:
    """Writes <id>.m4a into the job directory, or fails as told; can be held at a gate."""

    def __init__(self) -> None:
        self.failures: dict[str, BaseException] = {}
        self.gates: dict[str, threading.Event] = {}
        self.dirs: list[Path] = []
        self.dir_modes: list[int] = []
        self.calls: list[str] = []
        self.running = 0
        self.max_running = 0
        self.cancelled = 0
        self._lock = threading.Lock()

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.calls.append(video_id)
            self.dirs.append(job_dir)
            self.dir_modes.append(stat.S_IMODE(os.stat(job_dir).st_mode))
        try:
            gate = self.gates.get(video_id)
            if gate is not None and not gate.wait(DEADLINE):
                raise AssertionError("gate never opened")
            # Something left behind in the job directory, as yt-dlp leaves
            # the info JSON and Deno's cache.
            (job_dir / f"{video_id}.info.json").write_text("{}", encoding="utf-8")
            (job_dir / "deno").mkdir()
            failure = self.failures.get(video_id)
            if failure is not None:
                raise failure
            audio = job_dir / f"{video_id}.m4a"
            audio.write_bytes(b"AUDIO " + video_id.encode("ascii"))
            return Download(audio=audio, metadata=METADATA)
        finally:
            with self._lock:
                self.running -= 1

    def cancel(self) -> None:
        self.cancelled += 1
        for gate in self.gates.values():
            gate.set()


def wait_for(predicate: Any, what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


class JobsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.scratch = Path(temp.name) / "scratch"
        self.scratch.mkdir()
        self.clock = FakeClock()
        self.runner = FakeRunner()
        self.jobs = Jobs(self.scratch, self.runner, ttl_seconds=600, clock=self.clock)
        self.addCleanup(self.jobs.stop)

    def scratch_contents(self) -> list[str]:
        """Every path under scratch, relative and with '/', except the results directory itself."""
        names = [p.relative_to(self.scratch).as_posix() for p in self.scratch.rglob("*")]
        return sorted(name for name in names if name != "results")

    def run_job(self, video_id: str = VIDEO_ID) -> str:
        job_id = self.jobs.submit(video_id)
        self.assertTrue(self.jobs.run_next(timeout=0))
        return job_id


class SubmitTest(JobsTestCase):
    def test_ids_are_128_random_bits_and_start_queued(self) -> None:
        ids = {self.jobs.submit(VIDEO_ID) for _ in range(4)}
        self.assertEqual(len(ids), 4)
        for job_id in ids:
            self.assertTrue(JOB_ID_RE.fullmatch(job_id))
            self.assertEqual(self.jobs.status(job_id), {"status": "queued"})

    def test_ids_come_from_secrets_token_hex_16(self) -> None:
        # The ID is the only thing that lets a client see a job's title and
        # take its audio, so it must be unguessable: 128 bits from the
        # secrets module, never a counter or anything derived from the job.
        values = ["0123456789abcdef" * 2, "fedcba9876543210" * 2]
        with mock.patch.object(jobs_module.secrets, "token_hex", side_effect=values) as token_hex:
            ids = [self.jobs.submit(VIDEO_ID) for _ in values]
        self.assertEqual(ids, values)
        self.assertEqual(token_hex.call_args_list, [mock.call(16)] * len(values))

    def test_a_repeated_id_is_drawn_again(self) -> None:
        first = "0123456789abcdef" * 2
        second = "fedcba9876543210" * 2
        with mock.patch.object(jobs_module.secrets, "token_hex", side_effect=[first, first, second]):
            self.assertEqual(self.jobs.submit(VIDEO_ID), first)
            self.assertEqual(self.jobs.submit(OTHER_ID), second)
        self.assertEqual(len(self.jobs), 2)

    def test_ids_are_not_sequential(self) -> None:
        ids = [self.jobs.submit(VIDEO_ID) for _ in range(4)]
        numbers = sorted(int(job_id, 16) for job_id in ids)
        # Four draws of 128 bits land within 2**64 of each other with
        # vanishing probability; a counter or a clock always would.
        self.assertGreater(numbers[-1] - numbers[0], 2**64)

    def test_the_queue_is_bounded(self) -> None:
        self.assertEqual(jobs_module.QUEUE_SIZE, 4)
        for _ in range(4):
            self.assertFalse(self.jobs.is_full())
            self.jobs.submit(VIDEO_ID)
        self.assertTrue(self.jobs.is_full())
        with self.assertRaises(QueueFull):
            self.jobs.submit(VIDEO_ID)
        # The refused job left no entry behind.
        self.assertEqual(len(self.jobs), 4)

    def test_only_valid_ids_are_queued(self) -> None:
        for video_id in ("", "a" * 12, "../../etc/x", "dQw4w9WgXc\n", None):
            with self.subTest(video_id=video_id), self.assertRaises(ValueError):
                self.jobs.submit(video_id)  # type: ignore[arg-type]
        self.assertEqual(len(self.jobs), 0)

    def test_malformed_ids_are_unknown(self) -> None:
        for job_id in ("", "0" * 31, "0" * 33, "G" * 32, "A" * 32, "../" * 11, None):
            with self.subTest(job_id=job_id):
                self.assertIsNone(self.jobs.status(job_id))  # type: ignore[arg-type]
                self.assertIsNone(self.jobs.claim(job_id))  # type: ignore[arg-type]
        self.assertIsNone(self.jobs.status("0" * 32))

    def test_the_id_shape_is_checked_before_the_index(self) -> None:
        # The index is keyed by what secrets.token_hex(16) returns, so a
        # malformed ID never matches an entry anyway; the shape check must
        # still refuse it on its own, before any lookup. Planting entries under
        # malformed keys shows that: only the check stands between them and
        # the caller.
        for job_id in ("A" * 32, "0123456789ABCDEF" * 2, "0" * 31, "0" * 33, "g" * 32, " " + "0" * 31):
            with self.subTest(job_id=job_id):
                with mock.patch.object(jobs_module.secrets, "token_hex", return_value=job_id):
                    self.assertEqual(self.run_job(), job_id)
                self.assertEqual(self.jobs._jobs[job_id].status, "done")
                self.assertIsNone(self.jobs.status(job_id))
                self.assertIsNone(self.jobs.claim(job_id))
                self.assertEqual(self.jobs._jobs[job_id].status, "done")
        # A valid ID, and anything that is not a string, never reaches the index either.
        for job_id in (None, 0, b"0" * 32, ["0" * 32]):
            with self.subTest(job_id=job_id):
                self.assertIsNone(self.jobs.status(job_id))  # type: ignore[arg-type]
                self.assertIsNone(self.jobs.claim(job_id))  # type: ignore[arg-type]


class RunTest(JobsTestCase):
    def test_a_done_job_shows_only_the_whitelisted_metadata(self) -> None:
        job_id = self.run_job()
        self.assertEqual(self.jobs.status(job_id), {
            "status": "done",
            "title": "A Title",
            "duration": 10.5,
            "heatmap": [{"start": 0.0, "end": 10.5, "value": 1.0}],
        })
        # The audio moved out of the job directory, which is gone.
        self.assertEqual(self.scratch_contents(), [f"results/{job_id}.m4a"])

    def test_absent_metadata_is_omitted(self) -> None:
        with mock.patch.object(sys.modules[__name__], "METADATA", Metadata(None, None, None)):
            job_id = self.run_job()
        self.assertEqual(self.jobs.status(job_id), {"status": "done"})

    def test_failures_carry_only_the_code(self) -> None:
        self.runner.failures[VIDEO_ID] = FetchError("blocked")
        job_id = self.run_job()
        self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": "blocked"})
        self.assertEqual(self.scratch_contents(), [])

    def test_unexpected_errors_are_failed(self) -> None:
        self.runner.failures[VIDEO_ID] = RuntimeError("Stub Title in a message")
        job_id = self.run_job()
        self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": "failed"})
        self.assertEqual(self.scratch_contents(), [])

    def test_the_job_directory_is_fresh_owner_only_and_removed_on_every_path(self) -> None:
        self.runner.failures[OTHER_ID] = FetchError("not_found")
        self.run_job(VIDEO_ID)
        self.run_job(OTHER_ID)
        self.runner.failures["failing____"] = ValueError()
        self.run_job("failing____")
        self.assertEqual(len(set(self.runner.dirs)), 3)
        for job_dir in self.runner.dirs:
            self.assertEqual(job_dir.parent, self.scratch)
            self.assertTrue(job_dir.name.startswith("job-"))
            self.assertFalse(job_dir.exists())
        if os.name == "posix":
            self.assertEqual(self.runner.dir_modes, [0o700] * 3)

    def test_a_scratch_directory_that_vanished_fails_the_job(self) -> None:
        job_id = self.jobs.submit(VIDEO_ID)
        with mock.patch.object(jobs_module.tempfile, "mkdtemp", side_effect=OSError("gone")):
            self.jobs.run_next(timeout=0)
        self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": "failed"})
        self.assertEqual(self.runner.calls, [])

    def test_the_video_id_is_dropped_once_the_job_starts(self) -> None:
        job_id = self.run_job()
        self.assertIsNone(self.jobs._jobs[job_id].video_id)

    def test_one_fetch_at_a_time(self) -> None:
        gate = threading.Event()
        self.runner.gates[VIDEO_ID] = gate
        self.jobs.start(sweep_interval=3600)
        first = self.jobs.submit(VIDEO_ID)
        second = self.jobs.submit(OTHER_ID)
        wait_for(lambda: self.jobs.status(first) == {"status": "running"}, "the first job to run")
        time.sleep(0.3)
        self.assertEqual(self.jobs.status(second), {"status": "queued"})
        self.assertEqual(self.runner.calls, [VIDEO_ID])
        gate.set()
        wait_for(lambda: (self.jobs.status(second) or {}).get("status") == "done", "the second job")
        self.assertEqual(self.runner.max_running, 1)
        self.assertEqual(self.runner.calls, [VIDEO_ID, OTHER_ID])

    def test_only_a_queued_job_is_started(self) -> None:
        # An ID taken from the queue whose job has moved on (or was never a
        # waiting one) must not run again: that would fetch twice under one ID.
        job_id = self.jobs.submit(VIDEO_ID)
        self.jobs._jobs[job_id].status = "failed"
        self.assertFalse(self.jobs.run_next(timeout=0))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.jobs._jobs[job_id].status, "failed")
        self.assertEqual(self.scratch_contents(), [])

    def test_the_worker_logs_only_the_exception_type(self) -> None:
        # The worker survives an unexpected error with one line on stderr. An
        # exception message could carry a title or a path, so only the type
        # is written.
        calls: list[float | None] = []

        def run_next(timeout: float | None = None) -> bool:
            calls.append(timeout)
            if len(calls) == 1:
                raise RuntimeError(f"Stub Title {VIDEO_ID} in {self.scratch}")
            time.sleep(0.01)
            return False

        log = io.StringIO()
        with mock.patch.object(self.jobs, "run_next", side_effect=run_next), contextlib.redirect_stderr(log):
            self.jobs.start(sweep_interval=3600)
            wait_for(lambda: len(calls) > 1, "the worker to go on after the error")
            self.jobs.stop()
        self.assertEqual(log.getvalue(), "error while running a fetch job: RuntimeError\n")

    def test_stop_cancels_the_running_fetch(self) -> None:
        self.runner.gates[VIDEO_ID] = threading.Event()
        self.jobs.start(sweep_interval=3600)
        self.jobs.submit(VIDEO_ID)
        wait_for(lambda: self.runner.running == 1, "the job to start")
        started = time.monotonic()
        self.jobs.stop()
        self.assertGreaterEqual(self.runner.cancelled, 1)
        self.assertLess(time.monotonic() - started, DEADLINE)
        self.assertEqual(self.runner.running, 0)


class HeldResultTest(JobsTestCase):
    def test_audio_is_claimed_once_then_deleted_and_forgotten(self) -> None:
        job_id = self.run_job()
        path = self.jobs.claim(job_id)
        self.assertEqual(path, self.scratch / "results" / f"{job_id}.m4a")
        assert path is not None
        self.assertEqual(path.read_bytes(), b"AUDIO " + VIDEO_ID.encode("ascii"))
        # Claimed: a second download and a status request see nothing.
        self.assertIsNone(self.jobs.claim(job_id))
        self.assertIsNone(self.jobs.status(job_id))
        self.jobs.release(job_id)
        self.assertFalse(path.exists())
        self.assertIsNone(self.jobs.status(job_id))
        self.assertEqual(len(self.jobs), 0)
        self.assertEqual(self.scratch_contents(), [])

    def test_only_done_jobs_can_be_claimed(self) -> None:
        self.runner.failures[OTHER_ID] = FetchError("failed")
        failed = self.run_job(OTHER_ID)
        queued = self.jobs.submit(VIDEO_ID)
        self.assertIsNone(self.jobs.claim(failed))
        self.assertIsNone(self.jobs.claim(queued))
        self.runner.gates[VIDEO_ID] = gate = threading.Event()
        thread = threading.Thread(target=self.jobs.run_next, kwargs={"timeout": 0})
        thread.start()
        wait_for(lambda: self.jobs.status(queued) == {"status": "running"}, "the job to run")
        self.assertIsNone(self.jobs.claim(queued))
        gate.set()
        thread.join(DEADLINE)
        self.assertIsNotNone(self.jobs.claim(queued))

    def test_release_without_a_claim_does_nothing(self) -> None:
        job_id = self.run_job()
        self.jobs.release(job_id)
        self.jobs.release("0" * 32)
        self.assertEqual(self.jobs.status(job_id)["status"], "done")  # type: ignore[index]

    def test_a_newer_download_evicts_the_older_one(self) -> None:
        older = self.run_job(VIDEO_ID)
        older_path = self.scratch / "results" / f"{older}.m4a"
        self.assertTrue(older_path.exists())
        newer = self.run_job(OTHER_ID)
        # At most one held result: the older file is gone, then its entry.
        self.assertFalse(older_path.exists())
        self.assertIsNone(self.jobs.status(older))
        self.assertIsNone(self.jobs.claim(older))
        self.assertEqual(self.jobs.status(newer)["status"], "done")  # type: ignore[index]
        self.assertEqual(self.scratch_contents(), [f"results/{newer}.m4a"])

    def test_a_failed_job_evicts_nothing(self) -> None:
        held = self.run_job(VIDEO_ID)
        self.runner.failures[OTHER_ID] = FetchError("failed")
        self.run_job(OTHER_ID)
        self.assertEqual(self.jobs.status(held)["status"], "done")  # type: ignore[index]

    def test_a_newer_download_does_not_evict_a_failed_job(self) -> None:
        # Only older downloads are evicted: a client still polling a failed
        # job must see its error code, not a 404.
        self.runner.failures[OTHER_ID] = FetchError("too_long")
        failed = self.run_job(OTHER_ID)
        done = self.run_job(VIDEO_ID)
        self.assertEqual(self.jobs.status(failed), {"status": "failed", "error": "too_long"})
        self.assertEqual(self.jobs.status(done)["status"], "done")  # type: ignore[index]

    def test_a_download_being_streamed_is_not_evicted(self) -> None:
        older = self.run_job(VIDEO_ID)
        path = self.jobs.claim(older)
        assert path is not None
        self.run_job(OTHER_ID)
        self.assertTrue(path.exists())
        self.jobs.release(older)
        self.assertFalse(path.exists())


class ExpiryTest(JobsTestCase):
    def test_finished_jobs_expire_after_the_ttl(self) -> None:
        done = self.run_job(VIDEO_ID)
        self.runner.failures[OTHER_ID] = FetchError("not_found")
        # The failure finishes later, so it is not evicted with the done job.
        self.clock.now += 1
        failed = self.run_job(OTHER_ID)
        self.clock.now += 599
        self.assertIsNone(self.jobs.status(done))
        self.assertIsNone(self.jobs.claim(done))
        self.assertEqual(self.jobs.status(failed), {"status": "failed", "error": "not_found"})
        # Refused before the sweep, deleted by it.
        self.assertTrue((self.scratch / "results" / f"{done}.m4a").exists())
        self.assertEqual(self.jobs.sweep(), 1)
        self.assertEqual(self.scratch_contents(), [])
        self.clock.now += 1
        self.assertIsNone(self.jobs.status(failed))
        self.assertEqual(self.jobs.sweep(), 1)
        self.assertEqual(len(self.jobs), 0)

    def test_the_ttl_runs_from_completion_not_submission(self) -> None:
        # A job that waited in the queue for most of the TTL must still be
        # collectable once it finishes: the page fetches its audio only then.
        job_id = self.jobs.submit(VIDEO_ID)
        self.clock.now += 599
        self.assertTrue(self.jobs.run_next(timeout=0))
        self.clock.now += 599
        self.assertEqual(self.jobs.sweep(), 0)
        self.assertEqual(self.jobs.status(job_id)["status"], "done")  # type: ignore[index]
        self.assertIsNotNone(self.jobs.claim(job_id))
        self.jobs.release(job_id)

    def test_a_job_expires_the_ttl_after_completion(self) -> None:
        job_id = self.jobs.submit(VIDEO_ID)
        self.clock.now += 300
        self.assertTrue(self.jobs.run_next(timeout=0))
        self.clock.now += 599
        self.assertEqual(self.jobs.status(job_id)["status"], "done")  # type: ignore[index]
        self.clock.now += 1
        self.assertIsNone(self.jobs.status(job_id))
        self.assertIsNone(self.jobs.claim(job_id))
        self.assertEqual(self.jobs.sweep(), 1)
        self.assertEqual(self.scratch_contents(), [])

    def test_queued_and_running_jobs_never_expire(self) -> None:
        queued = self.jobs.submit(VIDEO_ID)
        self.clock.now += 10_000
        self.assertEqual(self.jobs.sweep(), 0)
        self.assertEqual(self.jobs.status(queued), {"status": "queued"})

    def test_the_file_goes_before_the_entry(self) -> None:
        job_id = self.run_job()
        self.clock.now += 600
        path = self.scratch / "results" / f"{job_id}.m4a"
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            self.assertEqual(self.jobs.sweep(), 0)
        # The entry stays, invisible, until the file is really gone.
        self.assertEqual(len(self.jobs), 1)
        self.assertIsNone(self.jobs.status(job_id))
        self.assertIsNone(self.jobs.claim(job_id))
        self.assertTrue(path.exists())
        self.assertEqual(self.jobs.sweep(), 1)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.jobs), 0)

    def test_a_file_that_would_not_go_hides_its_job_before_it_expires(self) -> None:
        # An eviction whose unlink failed leaves a GONE entry for the sweep to
        # retry; until then the job is invisible, though its TTL has not run out.
        older = self.run_job(VIDEO_ID)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("in use")):
            newer = self.run_job(OTHER_ID)
        self.assertEqual(self.jobs._jobs[older].status, "gone")
        self.assertIsNone(self.jobs.status(older))
        self.assertIsNone(self.jobs.claim(older))
        self.assertEqual(self.jobs.status(newer)["status"], "done")  # type: ignore[index]
        self.assertEqual(self.jobs.sweep(), 1)
        self.assertEqual(self.scratch_contents(), [f"results/{newer}.m4a"])

    def test_a_download_being_streamed_outlives_its_ttl(self) -> None:
        # The sweep must not pull the file from under a stream in progress;
        # release() deletes it when the stream ends.
        job_id = self.run_job()
        path = self.jobs.claim(job_id)
        assert path is not None
        self.clock.now += 10_000
        self.assertEqual(self.jobs.sweep(), 0)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.jobs), 1)
        self.jobs.release(job_id)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.jobs), 0)

    def test_the_sweeper_thread_runs(self) -> None:
        job_id = self.run_job()
        self.clock.now += 600
        self.jobs.start(sweep_interval=0.05)
        wait_for(lambda: len(self.jobs) == 0, "the sweeper")
        self.assertFalse((self.scratch / "results" / f"{job_id}.m4a").exists())

    def test_bad_construction(self) -> None:
        for kwargs in ({"ttl_seconds": 0}, {"ttl_seconds": 1, "queue_size": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Jobs(self.scratch, self.runner, **kwargs)


class StubRunTest(JobsTestCase):
    """The real runner with the stub: every outcome leaves only what the index knows."""

    def setUp(self) -> None:
        super().setUp()
        self.jobs = Jobs(self.scratch, StubYtDlp(600, 1024, 1), ttl_seconds=600, clock=self.clock)
        patcher = mock.patch("strange_ringtone_fetch.ytdlp.DRAIN_SECONDS", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stubbed_download(self) -> None:
        job_id = self.run_job(VIDEO_ID)
        status = self.jobs.status(job_id)
        assert status is not None
        self.assertEqual(sorted(status), ["duration", "heatmap", "status", "title"])
        self.assertEqual(status["title"], f"Stub Title {VIDEO_ID} é")
        self.assertEqual(self.scratch_contents(), [f"results/{job_id}.m4a"])

    def test_failures_leave_nothing_behind(self) -> None:
        cases = {
            "tooLong____": "too_long",
            "blocked____": "blocked",
            "notFound___": "not_found",
            "failing____": "failed",
            "oversize___": "failed",
            "noAudio____": "failed",
            "notADict___": "failed",
            "hanging____": "timeout",
        }
        for video_id, code in cases.items():
            with self.subTest(video_id=video_id):
                job_id = self.run_job(video_id)
                self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": code})
                self.assertEqual(self.scratch_contents(), [])

    @unittest.skipUnless(hasattr(os, "killpg"), "Windows can end only the direct child (see Jobs.run_next)")
    def test_a_timed_out_run_with_a_grandchild_leaves_nothing_behind(self) -> None:
        # Runs in the image (tests/image_fetcher): killpg ends the grandchild
        # too, so nothing holds the job directory and it is removed.
        job_id = self.run_job("grandchild_")
        self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": "timeout"})
        self.assertEqual(self.scratch_contents(), [])


if __name__ == "__main__":
    unittest.main()
