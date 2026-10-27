"""Fetch jobs: a bounded queue, one worker, and at most one finished download held (docs/PLAN.md 6).

The index in memory is the authority, as in the core's results store: a job
is a 128-bit random ID mapped to its state, and a file on disk that the index
doesn't know is never served. Only one yt-dlp runs at a time
(FETCH_CONCURRENCY=1): a fetch is slow, talks to YouTube from the server's
address, and holds up to FETCH_MAX_BYTES on the tmpfs, so a short queue and a
fast 503 beyond it keep all three bounded.

/tmp is sized for one download in flight plus one finished, uncollected
download (2 x FETCH_MAX_BYTES + 16 MB). So when a download finishes, any
older one still waiting is deleted: the fetcher never holds more than one.
The TTL remains the upper bound of a result's life, and nothing survives a
restart.
"""
from __future__ import annotations

import os
import queue
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from strange_ringtone_fetch.ytdlp import VIDEO_ID_RE, Download, FetchError, Metadata

# Exactly what secrets.token_hex(16) produces, checked before any lookup.
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
# Four waiting plus the one running. With FETCH_TIMEOUT_SECONDS at 120 the
# last in the queue waits at most ten minutes, which the page's polling covers.
QUEUE_SIZE = 4
RESULTS_DIR = "results"
# How long stop() waits for each thread; the running yt-dlp is killed first.
STOP_SECONDS = 10.0

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
# Internal only: an audio download being streamed, or a file whose deletion
# failed and is retried by the sweep. Neither has a status a client can see.
COLLECTING = "collecting"
GONE = "gone"


class QueueFull(Exception):
    """QUEUE_SIZE jobs are already waiting; the server answers 503 busy."""


class Runner(Protocol):
    def fetch(self, video_id: str, job_dir: Path) -> Download: ...

    def cancel(self) -> None: ...


@dataclass
class Job:
    job_id: str
    status: str
    created: float
    # Kept only until the worker starts the job.
    video_id: str | None
    finished: float | None = None
    error: str | None = None
    metadata: Metadata | None = None
    audio: Path | None = None


class Jobs:
    def __init__(
        self,
        scratch: Path,
        runner: Runner,
        ttl_seconds: int,
        queue_size: int = QUEUE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 1 or queue_size < 1:
            raise ValueError("ttl_seconds and queue_size must be at least 1")
        self.scratch = scratch
        self.runner = runner
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # Inside a scratch directory the server has just checked and emptied;
        # owner-only like scratch itself. Finished downloads wait here under
        # their job ID, outside the job directory that is removed after every run.
        self.results = scratch / RESULTS_DIR
        self.results.mkdir(mode=0o700, exist_ok=True)

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    # --- requests -------------------------------------------------------------

    def is_full(self) -> bool:
        """Whether submit() would refuse now; asked before the request body is read."""
        return self._queue.full()

    def submit(self, video_id: str) -> str:
        """Queue a fetch of video_id and return its job ID, or raise QueueFull."""
        # The server has checked the ID already; checked again, because this
        # value ends up in yt-dlp's argument list.
        if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("not a video ID")
        with self._lock:
            job_id = secrets.token_hex(16)
            # 128 random bits never repeat in practice; checked anyway, because
            # a reused ID would hand one client another's download.
            while job_id in self._jobs:
                job_id = secrets.token_hex(16)
            try:
                self._queue.put_nowait(job_id)
            except queue.Full:
                raise QueueFull() from None
            self._jobs[job_id] = Job(job_id=job_id, status=QUEUED, created=self._clock(), video_id=video_id)
        return job_id

    def status(self, job_id: str) -> dict[str, Any] | None:
        """What GET /fetch/api/jobs/<id> answers, or None for an unknown, expired or collected job."""
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in (COLLECTING, GONE) or self._expired(job):
                return None
            answer: dict[str, Any] = {"status": job.status}
            if job.status == FAILED:
                answer["error"] = job.error
            elif job.status == DONE and job.metadata is not None:
                metadata = job.metadata
                if metadata.title is not None:
                    answer["title"] = metadata.title
                if metadata.duration is not None:
                    answer["duration"] = metadata.duration
                if metadata.heatmap is not None:
                    answer["heatmap"] = [dict(point) for point in metadata.heatmap]
            return answer

    def claim(self, job_id: str) -> Path | None:
        """The finished download of job_id, claimed for one stream; None if there is none to claim.

        Atomic under the lock, so of two concurrent requests only one gets the
        file and the other a 404. The caller must call release() afterwards.
        """
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != DONE or job.audio is None or self._expired(job):
                return None
            job.status = COLLECTING
            return job.audio

    def release(self, job_id: str) -> None:
        """Delete a claimed download, then forget the job: a later status request is a 404."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.status == COLLECTING:
                self._delete(job)

    # --- the worker -------------------------------------------------------------

    def run_next(self, timeout: float | None = None) -> bool:
        """Run the next queued job, waiting up to timeout for one; whether one ran."""
        try:
            job_id = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != QUEUED or job.video_id is None:
                return False
            job.status = RUNNING
            video_id, job.video_id = job.video_id, None
        error: str | None = None
        metadata: Metadata | None = None
        held: Path | None = None
        job_dir: Path | None = None
        try:
            # A fresh directory per job, named by the server, owner-only
            # (mkdtemp creates it 0o700).
            job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.scratch))
            download = self.runner.fetch(video_id, job_dir)
            held = self.results / f"{job_id}.m4a"
            # A rename within the tmpfs, so the job directory can go now.
            os.replace(download.audio, held)
            metadata = download.metadata
        except FetchError as failure:
            error = failure.code
            held = None
        except Exception:
            # Details stay out of the status and the log: an exception message
            # could carry a path or a title.
            error = "failed"
            held = None
        finally:
            # Every path, success included: what is kept was moved out above.
            # ignore_errors covers one known gap, on Windows only (development,
            # never a deployment): there a timed-out yt-dlp's own children
            # survive, because only the direct child can be ended (see
            # ytdlp._kill_group), and one that still has the job directory as
            # its working directory keeps it from being removed. The server
            # empties the scratch directory when it next starts. On Linux,
            # killpg ends the whole group first and nothing is left; the
            # in-image tests check that.
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)
        with self._lock:
            job.finished = self._clock()
            if error is None:
                job.status, job.metadata, job.audio = DONE, metadata, held
                # At most one finished download on the tmpfs: this one.
                for other in list(self._jobs.values()):
                    if other is not job and other.status == DONE:
                        self._delete(other)
            else:
                job.status, job.error = FAILED, error
        return True

    def sweep(self) -> int:
        """Forget every finished job older than the TTL, deleting its file first; how many went."""
        removed = 0
        with self._lock:
            for job in list(self._jobs.values()):
                # Queued and running jobs never expire: the timeout bounds them.
                if job.status == GONE or (job.status in (DONE, FAILED) and self._expired(job)):
                    if self._delete(job):
                        removed += 1
        return removed

    def start(self, sweep_interval: float) -> None:
        """Start the worker and the sweeper on daemon threads until stop()."""
        if self._threads:
            raise RuntimeError("already started")
        if sweep_interval <= 0:
            raise ValueError("sweep_interval must be positive")
        self._stop.clear()

        def work() -> None:
            while not self._stop.is_set():
                try:
                    self.run_next(timeout=0.2)
                except Exception as error:
                    # The worker must not die and leave the queue stuck. One
                    # line, no traceback, which could quote a path.
                    sys.stderr.write(f"error while running a fetch job: {type(error).__name__}\n")

        def sweep() -> None:
            # Event.wait doubles as the sleep and the stop signal.
            while not self._stop.wait(sweep_interval):
                try:
                    self.sweep()
                except Exception:
                    pass

        # Daemons: neither may keep the process alive after SIGTERM.
        self._threads = [
            threading.Thread(target=work, name="fetch-worker", daemon=True),
            threading.Thread(target=sweep, name="fetch-sweeper", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Stop both threads, killing a running yt-dlp, and wait for them."""
        self._stop.set()
        self.runner.cancel()
        threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(STOP_SECONDS)

    # --- internals ----------------------------------------------------------------

    def _expired(self, job: Job) -> bool:
        return job.finished is not None and self._clock() - job.finished >= self.ttl_seconds

    def _delete(self, job: Job) -> bool:
        """With the lock held: the file goes first, the index entry only once it is gone."""
        if job.audio is not None:
            try:
                job.audio.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Windows refuses while something holds the file open. The
                # entry stays, invisible to clients, and the sweep retries.
                job.status = GONE
                return False
        self._jobs.pop(job.job_id, None)
        return True
