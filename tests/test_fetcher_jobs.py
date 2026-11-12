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
from strange_ringtone_fetch.metadata import Download, FetchError, LongVideo, Metadata
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
        # The finished downloads on the tmpfs as each fetch starts.
        self.held: list[list[str]] = []
        self.running = 0
        self.max_running = 0
        self.cancelled = 0
        # Videos the runner answers "long" for, and what it says about them.
        self.long: dict[str, Metadata] = {}
        # (video ID, start) of every window asked for.
        self.windows: list[tuple[str, int]] = []
        # Set to make a window come back without its beginning.
        self.no_window_start = False
        self._lock = threading.Lock()

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        return self._run(video_id, job_dir, None)

    def fetch_window(self, video_id: str, job_dir: Path, start: int) -> Download:
        with self._lock:
            self.windows.append((video_id, start))
        return self._run(video_id, job_dir, start)

    def _run(self, video_id: str, job_dir: Path, start: int | None) -> Download:
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.calls.append(video_id)
            self.dirs.append(job_dir)
            self.dir_modes.append(stat.S_IMODE(os.stat(job_dir).st_mode))
            self.held.append(sorted(path.name for path in (job_dir.parent / jobs_module.RESULTS_DIR).iterdir()))
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
            if start is None and video_id in self.long:
                raise LongVideo(self.long[video_id])
            audio = job_dir / f"{video_id}.m4a"
            audio.write_bytes(b"AUDIO " + video_id.encode("ascii"))
            # A window begins with the fragment that holds its start: here,
            # the last multiple of ten seconds.
            begin = None if start is None or self.no_window_start else float(start - start % 10)
            return Download(audio=audio, metadata=METADATA, window_start=begin)
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

    def run_job(self, video_id: str = VIDEO_ID, start: int | None = None) -> str:
        job_id = self.jobs.submit(video_id) if start is None else self.jobs.submit(video_id, start)
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
        self.runner.failures[OTHER_ID] = FetchError("blocked")
        failed = self.run_job(OTHER_ID)
        done = self.run_job(VIDEO_ID)
        self.assertEqual(self.jobs.status(failed), {"status": "failed", "error": "blocked"})
        self.assertEqual(self.jobs.status(done)["status"], "done")  # type: ignore[index]

    def test_a_download_being_streamed_is_not_evicted(self) -> None:
        older = self.run_job(VIDEO_ID)
        path = self.jobs.claim(older)
        assert path is not None
        self.run_job(OTHER_ID)
        self.assertTrue(path.exists())
        self.assertTrue(self.jobs.streaming(older))
        self.jobs.release(older)
        self.assertFalse(path.exists())
        self.assertFalse(self.jobs.streaming(older))

    def test_a_slow_stream_counts_against_the_tmpfs(self) -> None:
        # The tmpfs holds 2 x FETCH_MAX_BYTES: the download in flight and one
        # finished. A client that keeps its audio stream open keeps that file
        # on the tmpfs, so while it lasts, the oldest uncollected download goes
        # before the next fetch starts, rather than a third file joining them.
        self.assertEqual(jobs_module.MAX_HELD, 1)
        slow = self.run_job(VIDEO_ID)
        slow_path = self.jobs.claim(slow)
        assert slow_path is not None
        held = self.run_job(OTHER_ID)
        later = [self.run_job(video_id) for video_id in ("ccccccccccc", "ddddddddddd")]
        self.assertEqual(self.runner.held, [[], [f"{slow}.m4a"], [f"{slow}.m4a"], [f"{slow}.m4a"]])
        self.assertIsNone(self.jobs.status(held))
        self.assertIsNone(self.jobs.status(later[0]))
        self.assertEqual(self.jobs.status(later[1])["status"], "done")  # type: ignore[index]
        # The slow stream itself was never cut: one held download is room enough.
        self.assertTrue(self.jobs.streaming(slow))
        self.assertTrue(slow_path.exists())
        self.jobs.release(slow)
        self.assertEqual(self.scratch_contents(), [f"results/{later[1]}.m4a"])

    def test_streams_alone_over_budget_stop_the_oldest_and_the_next_fetch_waits(self) -> None:
        older = self.run_job(VIDEO_ID)
        self.assertIsNotNone(self.jobs.claim(older))
        newer = self.run_job(OTHER_ID)
        self.assertIsNotNone(self.jobs.claim(newer))
        waiting = self.jobs.submit("ccccccccccc")
        thread = threading.Thread(target=self.jobs.run_next, kwargs={"timeout": 0}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, DEADLINE)
        wait_for(lambda: not self.jobs.streaming(older), "the oldest stream to be told to stop")
        self.assertTrue(self.jobs.streaming(newer))
        time.sleep(0.3)
        # Still waiting for the stream to let go: no third download yet.
        self.assertEqual(self.runner.calls, [VIDEO_ID, OTHER_ID])
        self.assertEqual(self.jobs.status(waiting), {"status": "running"})
        self.jobs.release(older)
        thread.join(DEADLINE)
        self.assertEqual(self.runner.held[-1], [f"{newer}.m4a"])
        self.assertEqual(self.jobs.status(waiting)["status"], "done")  # type: ignore[index]
        self.jobs.release(newer)
        self.assertEqual(self.scratch_contents(), [f"results/{waiting}.m4a"])

    def test_stop_ends_a_wait_for_room(self) -> None:
        for video_id in (VIDEO_ID, OTHER_ID):
            self.assertIsNotNone(self.jobs.claim(self.run_job(video_id)))
        waiting = self.jobs.submit("ccccccccccc")
        # A daemon, so a wait that never ends fails this test instead of hanging the run.
        thread = threading.Thread(target=self.jobs.run_next, kwargs={"timeout": 0}, daemon=True)
        thread.start()
        wait_for(lambda: self.jobs.status(waiting) == {"status": "running"}, "the job to wait for room")
        self.jobs.stop()
        thread.join(DEADLINE)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.jobs.status(waiting), {"status": "failed", "error": "failed"})
        self.assertEqual(self.runner.calls, [VIDEO_ID, OTHER_ID])


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


class WindowJobsTest(JobsTestCase):
    """Long videos and windows of them (docs/PLAN.md 6.2), with the fake runner."""

    LONG = Metadata(title="A long one", duration=3600, heatmap=({"start": 0.0, "end": 36.0, "value": 1.0},))

    def test_a_long_video_ends_long_with_its_length_and_nothing_to_collect(self) -> None:
        self.runner.long[VIDEO_ID] = self.LONG
        job_id = self.run_job(VIDEO_ID)
        self.assertEqual(self.jobs.status(job_id), {
            "status": "long", "title": "A long one", "duration": 3600,
            "heatmap": [{"start": 0.0, "end": 36.0, "value": 1.0}],
        })
        self.assertIsNone(self.jobs.claim(job_id))
        # The status is still there after the refused claim, and nothing is on the tmpfs.
        self.assertEqual(self.jobs.status(job_id)["status"], "long")  # type: ignore[index]
        self.assertEqual(self.scratch_contents(), [])
        self.assertEqual(self.runner.windows, [])

    def test_a_long_answer_expires_like_any_finished_job(self) -> None:
        self.runner.long[VIDEO_ID] = self.LONG
        job_id = self.run_job(VIDEO_ID)
        self.clock.now += 599
        self.assertEqual(self.jobs.sweep(), 0)
        self.assertEqual(self.jobs.status(job_id)["status"], "long")  # type: ignore[index]
        self.clock.now += 1
        self.assertIsNone(self.jobs.status(job_id))
        self.assertEqual(self.jobs.sweep(), 1)
        self.assertEqual(len(self.jobs), 0)

    def test_a_long_answer_does_not_evict_the_download_that_waits(self) -> None:
        # Nothing was downloaded for it, so nothing needs the space.
        done = self.run_job(OTHER_ID)
        self.runner.long[VIDEO_ID] = self.LONG
        self.run_job(VIDEO_ID)
        self.assertEqual(self.jobs.status(done)["status"], "done")  # type: ignore[index]
        self.assertIsNotNone(self.jobs.claim(done))
        self.jobs.release(done)

    def test_a_job_with_a_start_time_asks_the_runner_for_a_window(self) -> None:
        job_id = self.run_job(VIDEO_ID, 1234)
        self.assertEqual(self.runner.windows, [(VIDEO_ID, 1234)])
        self.assertEqual(self.runner.calls, [VIDEO_ID])
        status = self.jobs.status(job_id)
        assert status is not None
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["window_start"], 1230.0)
        path = self.jobs.claim(job_id)
        assert path is not None
        self.assertEqual(path.read_bytes(), b"AUDIO " + VIDEO_ID.encode("ascii"))
        self.jobs.release(job_id)

    def test_a_start_of_zero_is_a_window_too(self) -> None:
        job_id = self.run_job(VIDEO_ID, 0)
        self.assertEqual(self.runner.windows, [(VIDEO_ID, 0)])
        self.assertEqual(self.jobs.status(job_id)["window_start"], 0.0)  # type: ignore[index]

    def test_a_whole_download_s_status_has_no_window_start(self) -> None:
        job_id = self.run_job(VIDEO_ID)
        self.assertNotIn("window_start", self.jobs.status(job_id))  # type: ignore[arg-type]
        self.assertEqual(self.runner.windows, [])

    def test_a_long_answer_to_a_job_with_a_start_time_is_impossible(self) -> None:
        # The fake runner answers long only without a start, as the real
        # ones do; a job with one gets its window.
        self.runner.long[VIDEO_ID] = self.LONG
        job_id = self.run_job(VIDEO_ID, 90)
        self.assertEqual(self.jobs.status(job_id)["status"], "done")  # type: ignore[index]

    def test_a_long_answer_to_a_job_with_a_start_time_fails_the_job(self) -> None:
        # No runner of ours answers so. One that did would send the page
        # round in a circle: it would ask for a start time again.
        self.runner.failures[VIDEO_ID] = LongVideo(self.LONG)
        job_id = self.run_job(VIDEO_ID, 90)
        self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": "failed"})
        self.assertEqual(self.scratch_contents(), [])
        # To a job without a start time the same answer is the "long" status.
        job_id = self.run_job(VIDEO_ID)
        self.assertEqual(self.jobs.status(job_id)["status"], "long")  # type: ignore[index]

    def test_a_window_without_its_beginning_fails_the_job(self) -> None:
        self.runner.no_window_start = True
        job_id = self.run_job(VIDEO_ID, 1234)
        self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": "failed"})
        self.assertEqual(self.scratch_contents(), [])

    def test_a_value_that_is_no_start_is_never_queued(self) -> None:
        for start in (-1, 360_000, True, False, 1.5, 90.0, "90", [90], 10**30):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.jobs.submit(VIDEO_ID, start)  # type: ignore[arg-type]
        self.assertEqual(len(self.jobs), 0)
        self.assertFalse(self.jobs.run_next(timeout=0))

    def test_the_start_is_kept_only_until_the_job_starts(self) -> None:
        job_id = self.jobs.submit(VIDEO_ID, 1234)
        self.assertEqual(self.jobs._jobs[job_id].start, 1234)
        self.assertTrue(self.jobs.run_next(timeout=0))
        self.assertIsNone(self.jobs._jobs[job_id].start)
        self.assertIsNone(self.jobs._jobs[job_id].video_id)

    def test_a_failed_window_is_a_failed_job_with_its_code(self) -> None:
        for code in ("past_end", "live", "blocked", "timeout", "failed"):
            with self.subTest(code=code):
                self.runner.failures[VIDEO_ID] = FetchError(code)
                job_id = self.run_job(VIDEO_ID, 5)
                self.assertEqual(self.jobs.status(job_id), {"status": "failed", "error": code})
                self.assertEqual(self.scratch_contents(), [])


class StubRunTest(JobsTestCase):
    """The real runner with the stub: every outcome leaves only what the index knows."""

    def setUp(self) -> None:
        super().setUp()
        self.jobs = Jobs(self.scratch, StubYtDlp(600, 1024, 1), ttl_seconds=600, clock=self.clock)
        patcher = mock.patch("strange_ringtone_runner.ytdlp.DRAIN_SECONDS", 0.5)
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
            "liveStream_": "live",
            "rejected___": "failed",
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

    def test_a_stubbed_long_video_and_a_window_of_it(self) -> None:
        long_id = self.run_job("tooLong____")
        status = self.jobs.status(long_id)
        assert status is not None
        self.assertEqual(sorted(status), ["duration", "heatmap", "status", "title"])
        self.assertEqual((status["status"], status["duration"]), ("long", 3600))
        self.assertEqual(self.scratch_contents(), [])
        job_id = self.run_job("tooLong____", 1234)
        status = self.jobs.status(job_id)
        assert status is not None
        self.assertEqual(sorted(status), ["duration", "heatmap", "status", "title", "window_start"])
        self.assertEqual((status["status"], status["duration"], status["window_start"]), ("done", 3600, 1230.0))
        self.assertEqual(self.scratch_contents(), [f"results/{job_id}.m4a"])
        path = self.jobs.claim(job_id)
        assert path is not None
        self.assertEqual(path.read_bytes(), b"STUB-WINDOW\n" + b"tooLong____@1234")
        self.jobs.release(job_id)

    def test_a_stubbed_window_s_failures_leave_nothing_behind(self) -> None:
        cases = {"liveStream_": "live", "pastEnd____": "past_end", "winBlocked_": "blocked",
                 "winTimeout_": "timeout", "winFailing_": "failed", "winHanging_": "timeout"}
        for video_id, code in cases.items():
            with self.subTest(video_id=video_id):
                job_id = self.run_job(video_id, 1234)
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
