"""The runner's defences behind its first line (strange_ringtone_runner.server), against planted files and bad input.

tests/test_fetcher_runner.py runs the runner's protocol with the stub
yt-dlp, which only ever leaves a regular file. These tests plant what a
compromised yt-dlp, or a child it left behind (same UID), could put where
the runner looks: a FIFO, a symlink, a directory, an empty or oversized
file. They also send requests whose method and path are not plain ASCII,
make a handler raise, and pin the sweeper's interval. Loopback, port 0, the
fake clock; no network. Each test names the check it guards (R3, R4).
"""
from __future__ import annotations

import contextlib
import http.client
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import metadata as metadata_module
from strange_ringtone_fetch.metadata import Download, Metadata
from strange_ringtone_runner import server as runner_server
from strange_ringtone_runner import ytdlp as ytdlp_module
from strange_ringtone_runner.server import Results
from test_fetcher_jobs import FakeClock
from test_fetcher_runner import (
    LOG_LINE_RE,
    MIB,
    OTHER_ID,
    VIDEO_ID,
    RunnerServerTestCase,
    _CappedRunnerServer,
    token,
)
from test_fetcher_server import DEADLINE, Response, build_request, exchange, wait_for

# A GET that a blocked open would hang gets this long before the test calls
# it stuck; the answer itself takes milliseconds.
QUICK_SECONDS = 3.0
STUCK_AFTER_SECONDS = 2.0
OUTSIDE = b"OUTSIDE-THE-RESULTS-DIRECTORY"


def quick_exchange(port: int, data: bytes, seconds: float = QUICK_SECONDS) -> Response:
    """exchange() with a short socket timeout, so a request the server never answers fails fast."""
    method = data.split(b" ", 1)[0].decode("latin-1")
    with socket.create_connection(("127.0.0.1", port), timeout=seconds) as sock:
        sock.sendall(data)
        response = http.client.HTTPResponse(sock, method=method)
        try:
            response.begin()
            body = response.read()
        finally:
            response.close()
    return Response(response.status, response.headers, body)


def release_fifo(path: Path) -> None:
    """Open path for writing and close it again, so a reader blocked in open() returns."""
    try:
        # Succeeds only while a reader is waiting, which is exactly the case
        # this is for; otherwise ENXIO (or ENOENT once it is gone).
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(fd)


def can_symlink(directory: Path) -> bool:
    probe = directory / "symlink-probe"
    try:
        os.symlink(directory / "nowhere", probe)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


class RunnerStreamTest(RunnerServerTestCase):
    """R3: the runner holds and serves only a regular file within the cap, and nothing it finds there can wedge it."""

    def results_path(self, job: str) -> Path:
        return self.scratch / runner_server.RESULTS_DIR / f"{job}.m4a"

    def audio_quickly(self, job: str) -> tuple[Response, float]:
        started = time.monotonic()
        response = quick_exchange(self.port, build_request("GET", f"/jobs/{job}/audio"))
        return response, time.monotonic() - started

    def assertIdleAndWorking(self) -> None:
        # The lock is free once the refused GET is over, /healthz answers,
        # and the next job runs: nothing is left holding the runner.
        wait_for(lambda: not self.server.busy.locked(), "the runner's lock to be free")
        self.assertEqual(exchange(self.port, build_request("GET", "/healthz")).status, 200)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX")
    def test_a_fifo_from_a_run_never_wedges_the_runner(self) -> None:
        # A run (or a child it left behind) that leaves a FIFO as the audio:
        # held and opened blocking, the GET would wait for a writer forever
        # with the lock held, every later POST would be 503 busy, and
        # /healthz would still say ok.
        real = self.ytdlp.fetch

        def fifo_fetch(video_id: str, job_dir: Path) -> Download:
            audio = job_dir / f"{video_id}.m4a"
            os.mkfifo(audio)
            return Download(audio=audio, metadata=Metadata("T", 10, None))

        self.ytdlp.fetch = fifo_fetch  # type: ignore[method-assign]
        job = token()
        self.addCleanup(release_fifo, self.results_path(job))
        response = self.post(VIDEO_ID, job)
        self.assertEqual(response.status, 200, response.body)
        # hold() takes only a regular file, so the run is a failure.
        self.assertEqual(response.json(), {"status": "failed", "error": "failed"})
        try:
            audio, elapsed = self.audio_quickly(job)
        except OSError as error:
            self.fail(f"the audio GET never answered: {error!r}")
        self.assertJsonError(audio, 404, "not_found")
        self.assertLess(elapsed, STUCK_AFTER_SECONDS)
        self.assertIdleAndWorking()
        self.assertScratchEmpty()
        self.ytdlp.fetch = real  # type: ignore[method-assign]
        self.done(OTHER_ID)

    def swap(self, job: str, kind: str) -> None:
        """Replace the held audio of job, as a survivor of the run could, by something of this kind."""
        path = self.results_path(job)
        path.unlink()
        if kind == "fifo":
            self.addCleanup(release_fifo, path)
            os.mkfifo(path)
        elif kind == "symlink":
            outside = self.temp / "outside.m4a"
            outside.write_bytes(OUTSIDE)
            os.symlink(outside, path)
        elif kind == "directory":
            path.mkdir()
            (path / "inside").write_bytes(OUTSIDE)
        elif kind == "empty":
            path.write_bytes(b"")
        elif kind == "oversized":
            # The test server's FETCH_MAX_BYTES is 1 MiB.
            path.write_bytes(b"\0" * (MIB + 1))
        else:
            raise AssertionError(kind)

    def test_the_stream_serves_only_a_regular_file_within_the_cap(self) -> None:
        # R3: read_result checks the run's files first; the stream checks
        # again when it opens the held file (O_NOFOLLOW, O_NONBLOCK, a
        # regular file of 1..FETCH_MAX_BYTES), because a child the run left
        # behind could replace it in between.
        kinds = ["directory", "empty", "oversized"]
        if hasattr(os, "mkfifo"):
            kinds.append("fifo")
        # Windows (development only) has no O_NOFOLLOW; the link case is POSIX.
        if hasattr(os, "O_NOFOLLOW") and can_symlink(self.temp):
            kinds.append("symlink")
        for kind in kinds:
            with self.subTest(kind=kind):
                job = self.done(VIDEO_ID)
                self.swap(job, kind)
                try:
                    response, elapsed = self.audio_quickly(job)
                except OSError as error:
                    self.fail(f"{kind}: the audio GET never answered: {error!r}")
                self.assertJsonError(response, 404, "not_found")
                self.assertLess(elapsed, STUCK_AFTER_SECONDS)
                self.assertNotIn(OUTSIDE, response.body)
                self.assertIdleAndWorking()
                # Once, as for a real stream: the token is spent.
                self.assertJsonError(self.audio(job), 404, "not_found")
                # The release deletes whatever was planted, a directory too.
                self.assertFalse(os.path.lexists(self.results_path(job)))
                with contextlib.suppress(FileNotFoundError):
                    (self.temp / "outside.m4a").unlink()
                self.assertScratchEmpty()
        # Acceptance: an untouched result is still served.
        job = self.done(VIDEO_ID)
        self.assertEqual(self.audio(job).body, b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))


class ResultsHoldTest(unittest.TestCase):
    """R3: Results.hold() moves in a regular file only, never a FIFO, a symlink or a directory."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.temp = Path(directory.name)
        self.results = Results(self.temp / "results", FakeClock())

    def held(self) -> list[str]:
        return sorted(path.name for path in (self.temp / "results").iterdir())

    def test_only_a_regular_file_is_held(self) -> None:
        sources: dict[str, Path] = {}
        directory = self.temp / "a-directory"
        directory.mkdir()
        sources["directory"] = directory
        if hasattr(os, "mkfifo"):
            fifo = self.temp / "a-fifo"
            os.mkfifo(fifo)
            sources["fifo"] = fifo
        if can_symlink(self.temp):
            target = self.temp / "target.m4a"
            target.write_bytes(OUTSIDE)
            link = self.temp / "a-link"
            os.symlink(target, link)
            sources["symlink"] = link
        for kind, source in sources.items():
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.results.hold(token(), source)
                # Nothing moved: the source is where it was and nothing is held.
                self.assertTrue(os.path.lexists(source))
                self.assertEqual(self.held(), [])
                self.assertEqual(len(self.results), 0)
        # Acceptance: a regular file is held under its token's name.
        audio = self.temp / "audio.m4a"
        audio.write_bytes(b"audio")
        job = token()
        self.results.hold(job, audio)
        self.assertEqual(self.held(), [f"{job}.m4a"])
        self.assertFalse(audio.exists())


class RunnerLogHardeningTest(RunnerServerTestCase):
    """R4: a log line is printable ASCII with its method and path capped, whatever the request held."""

    def test_log_lines_are_printable_ascii_and_capped(self) -> None:
        requests = [
            # Bytes over 0x7F and terminal escapes in the path.
            b"GET /\x80\xff\x1b[31mfake\x1b[0m HTTP/1.0\r\n\r\n",
            # And in the method.
            b"G\xe9\x1bT / HTTP/1.0\r\n\r\n",
            # Long ones: the method is capped at 16, the path at 200.
            b"Z" * 40 + b" / HTTP/1.0\r\n\r\n",
            b"GET /" + b"z" * 300 + b" HTTP/1.0\r\n\r\n",
        ]
        for data in requests:
            response = exchange(self.port, data)
            self.assertIn(response.status, (404, 405), response.body)
        wait_for(lambda: len(self.log.lines()) >= len(requests), "a log line per request")
        lines = self.log.lines()
        self.assertEqual(len(lines), len(requests), lines)
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(line.isascii(), repr(line))
                self.assertRegex(line, LOG_LINE_RE)
                method, path = line.split(" ")[:2]
                self.assertLessEqual(len(method), 16)
                self.assertLessEqual(len(path), 200)
        log = self.log.getvalue()
        self.assertNotIn("\x1b", log)
        self.assertTrue(log.isascii(), repr(log))
        # The caps cut, they don't drop: the first 16 and 200 characters stay.
        self.assertIn("Z" * 16 + " / ", log)
        self.assertIn(" /" + "z" * 199 + " ", log)

    def test_a_handler_error_is_one_line_with_the_class_name_only(self) -> None:
        # handle_error: no traceback and no text of the exception, which could
        # quote a title, an ID or a path.
        secret = "Secret Stub Title " + VIDEO_ID
        with mock.patch.object(runner_server, "parse_job_request", side_effect=RuntimeError(secret)):
            with contextlib.suppress(http.client.HTTPException, OSError):
                self.post(VIDEO_ID)
            wait_for(lambda: "error while handling a request" in self.log.getvalue(), "the error line")
        log = self.log.getvalue()
        self.assertIn("error while handling a request: RuntimeError\n", log)
        for never in (secret, "Secret", "Traceback", VIDEO_ID, "parse_job_request", ".py"):
            self.assertNotIn(never, log)
        for line in self.log.lines():
            if not line.startswith("error while handling a request: "):
                self.assertRegex(line, LOG_LINE_RE)
        # Acceptance: the server goes on.
        self.done(OTHER_ID)

    def test_the_worker_cap_refusal_logs_no_address(self) -> None:
        # R4: the hand-written 503 has no handler, so its line is written by
        # hand too: no method, no path, and never the peer's address.
        self.start(server_class=_CappedRunnerServer)
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE)
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                data = b""
                while chunk := sock.recv(65536):
                    data += chunk
            self.assertTrue(data.startswith(b"HTTP/1.0 503 "), data[:60])
        finally:
            idle.close()
        wait_for(lambda: "- - 503" in self.log.getvalue(), "the refusal's log line")
        lines = self.log.lines()
        self.assertIn("- - 503 0.000", lines)
        self.assertEqual([line for line in lines if " 503 " in line], ["- - 503 0.000"])
        self.assertNotIn("127.0.0.1", self.log.getvalue())
        for line in lines:
            self.assertRegex(line, LOG_LINE_RE)


class RunnerLogCapTest(unittest.TestCase):
    """R4/N2: the log line's caps, pinned at their values and at the helpers that apply them."""

    def test_the_caps_are_16_and_200(self) -> None:
        self.assertEqual((runner_server._LOG_METHOD_MAX, runner_server._LOG_PATH_MAX), (16, 200))

    def test_the_path_helper_cuts_at_the_cap(self) -> None:
        self.assertEqual(runner_server._log_path("/" + "z" * 300), "/" + "z" * 199)
        # Masked first, then cut: a token never survives in part.
        self.assertEqual(runner_server._log_path("/jobs/" + "a" * 32 + "/audio"), "/jobs/<job>/audio")

    def test_the_printable_helper_cuts_at_its_limit(self) -> None:
        self.assertEqual(runner_server._printable("Z" * 40, runner_server._LOG_METHOD_MAX), "Z" * 16)


class InfoNumberRangeTest(unittest.TestCase):
    """S1.3: an info JSON integer beyond the float range is cleaned like NaN or infinity, in the runner's read."""

    HUGE = 10 ** 400

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name) / "job"
        self.job.mkdir()

    def read(self, info_text: str) -> Metadata:
        (self.job / f"{VIDEO_ID}.m4a").write_bytes(b"STUB-M4A")
        (self.job / f"{VIDEO_ID}.info.json").write_text(info_text, encoding="ascii")
        download: Download = ytdlp_module.read_result(self.job, VIDEO_ID, MIB)
        return download.metadata

    def test_the_whitelist_drops_a_huge_integer(self) -> None:
        for raw in (self.HUGE, -self.HUGE):
            with self.subTest(sign=raw > 0):
                self.assertIsNone(metadata_module.clean_duration(raw))
                point = {"start_time": 0, "end_time": 1, "value": raw}
                self.assertIsNone(metadata_module.clean_heatmap([point], 10))
                self.assertEqual(metadata_module.parse_info({"title": "T", "duration": raw}),
                                 Metadata(title="T", duration=None, heatmap=None))
        # Control: a large integer that a float holds is still a number.
        self.assertEqual(metadata_module.clean_duration(10 ** 300), 10 ** 300)

    def test_the_runners_read_drops_a_huge_duration_and_its_heatmap(self) -> None:
        text = ('{"title": "T", "duration": ' + str(self.HUGE)
                + ', "heatmap": [{"start_time": 0, "end_time": 1, "value": 0.5}]}')
        self.assertEqual(self.read(text), Metadata(title="T", duration=None, heatmap=None))
        # The read closed what it opened: the job directory can go at once.
        for path in self.job.iterdir():
            path.unlink()
        self.job.rmdir()
        self.assertFalse(self.job.exists())

    def test_the_runners_read_drops_a_heatmap_with_a_huge_value(self) -> None:
        text = '{"title": "T", "duration": 10, "heatmap": [{"start_time": 0, "end_time": ' + str(self.HUGE) + \
            ', "value": 0.5}]}'
        self.assertEqual(self.read(text), Metadata(title="T", duration=10, heatmap=None))


class RunnerSweepIntervalTest(unittest.TestCase):
    """R3: the shipped sweep interval keeps COLLECT_DEADLINE_SECONDS meaningful."""

    def test_the_sweep_interval_is_well_under_the_collect_deadline(self) -> None:
        # The tests set the interval themselves, so only this pins what ships:
        # an uncollected result lives at most the deadline plus one interval.
        self.assertEqual(runner_server.SWEEP_INTERVAL_SECONDS, 5.0)
        self.assertEqual(runner_server.COLLECT_DEADLINE_SECONDS, 60.0)
        self.assertLessEqual(runner_server.SWEEP_INTERVAL_SECONDS, runner_server.COLLECT_DEADLINE_SECONDS / 4)

    def test_the_server_sweeps_at_the_module_interval(self) -> None:
        # Read when the server starts, from the module, so the constant above
        # is the one a real runner uses.
        intervals: list[float] = []
        real = runner_server.RunnerServer._sweep_forever

        def recording(server: Any, interval: float) -> None:
            intervals.append(interval)
            real(server, interval)

        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        settings = runner_server.Settings(scratch_dir=Path(temp.name) / "scratch")
        with mock.patch.object(runner_server.RunnerServer, "_sweep_forever", recording):
            server = runner_server.RunnerServer(("127.0.0.1", 0), settings)
            try:
                wait_for(lambda: bool(intervals), "the sweeper to start")
            finally:
                server.server_close()
        self.assertEqual(intervals, [runner_server.SWEEP_INTERVAL_SECONDS])


if __name__ == "__main__":
    unittest.main()
