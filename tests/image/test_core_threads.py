"""Each ffmpeg run of a real render uses a fixed number of threads (section 5.3 of docs/PLAN.md).

compose.example.yaml sizes the core's pids_limit from the threads one render
runs (tests/compose/test_compose_topology.py, FFMPEG_RUN_THREADS). ffmpeg 8's
command line runs a thread each for itself, its demuxer, its decoder, its
filter graph, its encoder and its muxer; left to itself the filter graph
also starts a pool of one thread per usable CPU, so the count, and with it
the cap that must be twice the worst case, would follow the host's CPUs
instead of the code. The renderer holds the filter graph to one thread.
Here a real render with the image's ffmpeg is watched through /proc.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from corpus.build import valid_wav
from strange_ringtone.render import FFMPEG, Renderer, RenderParams, default_prlimit

# The same number as FFMPEG_RUN_THREADS in tests/compose/test_compose_topology.py:
# the main thread, the demuxer, the decoder, the filter graph, the encoder
# and the muxer.
FFMPEG_RUN_THREADS = 6
CLIP_SECONDS = 30.0
SAMPLE_SECONDS = 0.002


def _ffmpeg_thread_counts() -> list[int]:
    """The thread count of every ffmpeg process in the container right now."""
    counts = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{entry}/exe") != FFMPEG:
                continue
            counts.append(len(os.listdir(f"/proc/{entry}/task")))
        except OSError:
            # Gone, or not ours to read: either way not a render of this test.
            continue
    return counts


class RenderThreadsTest(unittest.TestCase):
    def test_an_ffmpeg_run_uses_a_fixed_number_of_threads_whatever_the_cpus(self) -> None:
        cpus = len(os.sched_getaffinity(0))
        job = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, job, True)
        valid_wav(job / "in.wav", CLIP_SECONDS)
        params = RenderParams(fade_in=1.0, fade_out=1.0, normalize=True, target_lufs=-14.0, title="Threads",
                              duration=CLIP_SECONDS)
        renderer = Renderer((FFMPEG,), 20, prlimit=default_prlimit())

        peak = 0
        runs_seen = 0
        done = threading.Event()
        errors: list[BaseException] = []

        def render() -> None:
            try:
                renderer.render(job, params)
            except BaseException as error:  # reported below, in the test's thread
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=render)
        worker.start()
        while not done.is_set():
            counts = _ffmpeg_thread_counts()
            if counts:
                runs_seen += 1
                peak = max(peak, *counts)
            time.sleep(SAMPLE_SECONDS)
        worker.join()
        self.assertEqual(errors, [])
        # Control: the sampler caught ffmpeg with its pipeline running, or a
        # low peak would mean nothing.
        self.assertGreater(runs_seen, 0, "never saw ffmpeg running")
        self.assertGreaterEqual(peak, FFMPEG_RUN_THREADS - 1, f"saw at most {peak} threads: the sampler missed the run")
        self.assertLessEqual(
            peak, FFMPEG_RUN_THREADS,
            f"an ffmpeg run had {peak} threads with {cpus} usable CPUs; the core's pids_limit counts "
            f"{FFMPEG_RUN_THREADS} per run (is the filter graph held to one thread?)",
        )


if __name__ == "__main__":
    unittest.main()
