"""The runner client's deadlines and abort races (strange_ringtone_fetch.runner_client), pinned one by one.

tests/test_fetcher_runner_client.py meets every rule of the runner's answer
with an answer that breaks it. These tests pin what that leaves open: which
timeout bounds the POST's head and its body, the four places a cancel() or
the deadline timer can land between two steps of an exchange, and the URL
and answer shapes a caller that bypasses Settings could hand the client.
The same scripted runner on loopback; no network. All of it is S1
(docs/PLAN.md 6.1).
"""
from __future__ import annotations

import json
import socket
import time
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import runner_client
from strange_ringtone_fetch.runner_client import RunnerClient
from test_fetcher_runner_client import (
    AUDIO,
    DONE,
    MIB,
    SETTLE_SECONDS,
    VIDEO_ID,
    ClientTestCase,
    Request,
    ScriptedRunner,
    ScriptEntry,
    audio_response,
    drip,
    head_of,
    json_response,
)

HTTPConnection = runner_client.http.client.HTTPConnection


class ClientHeadTimeoutTest(ClientTestCase):
    """S1: the POST's head waits for the whole run (the POST deadline), not the connect or the read timeout."""

    def test_a_post_head_slower_than_the_connect_timeout_is_waited_for(self) -> None:
        # The runner answers only once yt-dlp is done, so a real fetch takes
        # far longer than CONNECT_TIMEOUT_SECONDS before the status line.
        # Here the head comes after 0.8 s, past both the connect (0.3 s) and
        # the read timeout (0.3 s), well inside the POST deadline (6 s).
        slow = drip([json_response(DONE)], 0.0, first_delay=0.8)
        download = self.accept(slow, audio_response(AUDIO), connect_timeout=0.3, read_timeout=0.3,
                               timeout_seconds=5, post_grace=1.0)
        self.assertEqual(download.metadata.duration, 215.5)

    def test_a_post_head_slower_than_the_post_deadline_fails(self) -> None:
        # The mirror case: with generous socket timeouts, the POST deadline
        # (1 + 0.5 s) alone ends a head that comes after 4 s.
        runner = self.use(drip([json_response(DONE)], 0.0, first_delay=4.0), audio_response(AUDIO))
        client = self.client(timeout_seconds=1, post_grace=0.5, connect_timeout=5.0, read_timeout=5.0)
        started = time.monotonic()
        self.assertFailed(client)
        self.assertLess(time.monotonic() - started, 1.5 + 1.0)
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()

    def test_a_post_body_that_drips_is_cut_by_the_read_timeout(self) -> None:
        # Once the head is in, each recv of the body gets the read timeout,
        # not what is left of the POST deadline: a runner that drips its
        # answer one byte at a time is cut within the read timeout.
        small = json_response({"status": "done", "info": {"title": "T"}})
        head = head_of(small)
        body = small[len(head):]
        # Acceptance: pieces faster than the read timeout (0.3 s) arrive whole.
        pieces = [head] + [body[i:i + 10] for i in range(0, len(body), 10)]
        self.accept(drip(pieces, 0.1), audio_response(AUDIO), read_timeout=0.3, timeout_seconds=5, post_grace=1.0)
        answer = json_response(DONE)
        head = head_of(answer)
        body = answer[len(head):]
        # One byte every 0.6 s: slower than the read timeout, but the POST
        # deadline (6 s) would let it go on.
        runner = self.use(drip([head] + [body[i:i + 1] for i in range(len(body))], 0.6), audio_response(AUDIO))
        started = time.monotonic()
        self.assertFailed(self.client(read_timeout=0.3, timeout_seconds=5, post_grace=1.0))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()


class ClientAbortRaceTest(ClientTestCase):
    """S1: a cancel() or a deadline that lands between two steps of an exchange still fails the fetch closed."""

    def test_a_cancel_between_the_post_and_the_get_opens_no_new_connection(self) -> None:
        # Jobs.stop() calls cancel(); one that falls after the POST's answer
        # and before the GET must end the fetch there, so a stopping fetcher
        # collects no audio.
        self.accept(json_response(DONE), audio_response(AUDIO))
        runner = self.use(json_response(DONE), audio_response(AUDIO))
        client = self.client()
        real_post = client._post

        def post_then_cancel(*args: Any) -> Any:
            metadata = real_post(*args)
            client.cancel()
            return metadata

        client._post = post_then_cancel  # type: ignore[method-assign]
        self.assertFailed(client)
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()

    def test_an_abort_between_connect_and_attach_sends_nothing(self) -> None:
        # The deadline or cancel() can fire while connect() runs, when there
        # is no socket yet to shut down: attach() must notice and send nothing.
        runner = self.use(json_response(DONE), audio_response(AUDIO))
        client = self.client()
        real_connect = HTTPConnection.connect

        def connect_then_cancel(conn: Any) -> None:
            real_connect(conn)
            client.cancel()

        with mock.patch.object(HTTPConnection, "connect", connect_then_cancel):
            self.assertFailed(client)
        # The connection was made, and shut down before a byte was sent: the
        # scripted runner saw it close with no request on it.
        time.sleep(SETTLE_SECONDS)
        self.assertEqual(runner.connections, 1)
        self.assertEqual(runner.requests, [])
        self.assertJobDirEmpty()

    def test_an_abort_after_the_last_byte_fails_the_exchange(self) -> None:
        # A shutdown that races a complete read: every byte arrived, but the
        # exchange was aborted before it ended, so it is not an answer. For
        # the GET, the complete file the client wrote goes again.
        for which, methods in (("POST", ["POST"]), ("GET", ["POST", "GET"])):
            with self.subTest(which):
                runner = self.use(json_response(DONE), audio_response(AUDIO))
                client = self.client()
                real = client._read_exactly
                reads: list[int] = []

                def read_then_abort(response: Any, exchange: Any, length: int, sink: Any,
                                    real: Any = real, reads: list[int] = reads,
                                    abort_on: int = len(methods)) -> None:
                    real(response, exchange, length, sink)
                    reads.append(length)
                    if len(reads) == abort_on:
                        exchange.abort()

                client._read_exactly = read_then_abort  # type: ignore[method-assign]
                self.assertFailed(client)
                self.assertEqual(len(reads), len(methods))
                self.assertRequests(runner, methods)
                self.assertJobDirEmpty()

    def test_a_head_read_while_the_exchange_was_aborted_is_not_an_answer(self) -> None:
        # http.client takes a head cut off by the shutdown (EOF ends its
        # header loop) for a complete one; the client must not act on it, so
        # it never even creates the audio file.
        runner = self.use(json_response(DONE), audio_response(AUDIO))
        client = self.client()
        real_getresponse = HTTPConnection.getresponse
        responses: list[Any] = []

        def getresponse_then_cancel(conn: Any) -> Any:
            response = real_getresponse(conn)
            responses.append(response)
            if len(responses) == 2:
                # The GET's head is parsed; the exchange is aborted now.
                client.cancel()
            return response

        opened: list[Path] = []
        real_open = runner_client.os.open

        def recording_open(path: Any, *args: Any, **kwargs: Any) -> int:
            opened.append(Path(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(HTTPConnection, "getresponse", getresponse_then_cancel), \
                mock.patch.object(runner_client.os, "open", recording_open):
            self.assertFailed(client)
        self.assertEqual(len(responses), 2)
        self.assertNotIn(self.job_dir / f"{VIDEO_ID}.m4a", opened)
        self.assertRequests(runner, ["POST", "GET"])
        self.assertJobDirEmpty()


class ClientAnswerShapeTest(ClientTestCase):
    """S1: a failed answer is exactly {status, error}; anything more is "failed", never the code it names."""

    def test_a_failed_answer_with_an_extra_key_is_failed_not_its_code(self) -> None:
        # Acceptance: the exact shape passes its code through.
        runner = self.use(json_response({"status": "failed", "error": "blocked"}), audio_response(AUDIO))
        self.assertFailed(self.client(), "blocked")
        self.assertRequests(runner, ["POST"])
        for extra in ({"x": 1}, {"info": {}}, {"detail": "blocked"}):
            with self.subTest(extra=json.dumps(extra)):
                runner = self.use(json_response({"status": "failed", "error": "blocked", **extra}),
                                  audio_response(AUDIO))
                self.assertFailed(self.client(), "failed")
                self.assertRequests(runner, ["POST"])
                self.assertJobDirEmpty()


class ClientHugeNumberTest(ClientTestCase):
    """S1/S1.3: a JSON integer beyond the float range is an invalid number, cleaned like NaN or infinity.

    float() of such an integer raises OverflowError, which no except tuple of
    the client names; the shared whitelist treats it as no number at all, so
    the answer's field is dropped, the same as for every other invalid number.
    """

    HUGE = 10 ** 400

    def test_a_huge_duration_is_dropped_with_the_heatmap_that_depends_on_it(self) -> None:
        for duration in (self.HUGE, -self.HUGE):
            with self.subTest(sign=duration > 0):
                info = {"title": "T", "duration": duration,
                        "heatmap": [{"start_time": 0, "end_time": 1, "value": 0.5}]}
                download = self.accept(json_response({"status": "done", "info": info}), audio_response(AUDIO))
                self.assertEqual((download.metadata.title, download.metadata.duration, download.metadata.heatmap),
                                 ("T", None, None))
                # accept() removed the one file the fetch made: nothing else is left.
                self.assertJobDirEmpty()

    def test_a_huge_number_in_a_heatmap_point_drops_the_heatmap(self) -> None:
        for key in ("start_time", "end_time", "value"):
            with self.subTest(key=key):
                point = {"start_time": 0, "end_time": 1, "value": 0.5, key: self.HUGE}
                info = {"title": "T", "duration": 10, "heatmap": [point]}
                download = self.accept(json_response({"status": "done", "info": info}), audio_response(AUDIO))
                self.assertEqual(download.metadata.duration, 10)
                self.assertIsNone(download.metadata.heatmap)
                self.assertJobDirEmpty()


class PostGraceTest(unittest.TestCase):
    """S1/A2.5: the POST deadline's grace covers the runner's worst case past FETCH_TIMEOUT_SECONDS, with a margin."""

    def test_the_grace_covers_the_drain_and_two_reaps_with_a_margin_of_3_s(self) -> None:
        # Before it answers, the runner may spend FETCH_TIMEOUT_SECONDS on
        # the run, DRAIN_SECONDS on the pipes after a timeout kill, and one
        # bounded reap at each of the two run boundaries. A grace shorter
        # than that would fail a job the runner was still finishing, and the
        # client would then ask for audio the runner never got to hold.
        from strange_ringtone_runner import ytdlp

        worst = ytdlp.DRAIN_SECONDS + 2 * ytdlp.REAP_SECONDS
        self.assertGreaterEqual(runner_client.POST_GRACE_SECONDS, worst + 3.0)
        # And it is what a client gets by default.
        client = RunnerClient("http://127.0.0.1:9", max_bytes=MIB, timeout_seconds=120)
        self.assertEqual(client.post_deadline, 120 + runner_client.POST_GRACE_SECONDS)


class ClientUrlTest(unittest.TestCase):
    """S1: a caller that bypasses Settings still gets a client for one plain-HTTP host and port only."""

    def test_a_runner_url_with_anything_but_host_and_port_is_refused(self) -> None:
        for url in (
            "http://user@fetcher-runner:8080",
            "http://@fetcher-runner:8080",
            "http://fetcher-runner:8080/x",
            "http://fetcher-runner:8080/jobs",
            "http://fetcher-runner:8080/?a=b",
            "http://fetcher-runner:8080?a=b",
            "http://fetcher-runner:8080/#f",
            "http://fetcher-runner:8080#f",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                RunnerClient(url, MIB, 1)
        # Acceptance: the host and port, with or without the root path.
        for url in ("http://fetcher-runner:8080", "http://fetcher-runner:8080/"):
            with self.subTest(url=url):
                client = RunnerClient(url, MIB, 1)
                self.assertEqual((client.host, client.port), ("fetcher-runner", 8080))


class _TimedRunner(ScriptedRunner):
    """The scripted runner, noting when each request arrived and when each connection was closed."""

    def __init__(self, script: Iterable[ScriptEntry]) -> None:
        self.arrived: list[float] = []
        self.closed: list[float] = []
        super().__init__(script)

    def _record(self, request: Request) -> Any:
        self.arrived.append(time.monotonic())
        return super()._record(request)

    def _finish(self, sock: socket.socket) -> None:
        # Noted before the FIN goes out, so a client can see the close only
        # after this time.
        self.closed.append(time.monotonic())
        super()._finish(sock)


class ClientWaitsForCloseTest(ClientTestCase):
    """S1: an exchange ends when the runner closes the connection, which it does only after freeing its job lock.

    The runner writes its answer, then frees its one-job lock (and, after a
    stream, deletes the audio), and only then closes the connection. A client
    that went on as soon as it had the declared length could send its next
    request, the audio GET or the next job's POST, while the lock was still
    held, and get 503 busy: a fetch failed for nothing.
    """

    HOLD = 0.4

    def timed(self, *script: ScriptEntry) -> _TimedRunner:
        runner = _TimedRunner(script)
        self.addCleanup(runner.stop)
        self.runner = runner
        return runner

    def test_the_next_request_waits_until_the_runner_closed_the_last_one(self) -> None:
        # Each answer is complete at once; its connection closes HOLD later.
        runner = self.timed([(0.0, json_response(DONE)), (self.HOLD, b"")],
                            [(0.0, audio_response(AUDIO)), (self.HOLD, b"")])
        download = self.client(timeout_seconds=5, post_grace=1.0, read_timeout=2.0, audio_deadline=5.0).fetch(
            VIDEO_ID, self.job_dir)
        returned = time.monotonic()
        self.assertEqual(download.audio.read_bytes(), AUDIO)
        self.assertRequests(runner, ["POST", "GET"])
        self.assertEqual(len(runner.closed), 2)
        # The GET went out only once the POST's connection was closed, and
        # the fetch returned only once the GET's was.
        self.assertGreaterEqual(runner.arrived[1], runner.closed[0])
        self.assertGreaterEqual(returned, runner.closed[1])

    def test_a_runner_that_never_closes_fails_the_fetch_and_leaves_nothing(self) -> None:
        # The deadline, not the (long) read timeout, ends the wait. After a
        # complete POST answer no GET is sent.
        runner = self.use([(0.0, json_response(DONE)), (0.0, None)])
        started = time.monotonic()
        self.assertFailed(self.client(timeout_seconds=1, post_grace=0.5, read_timeout=5.0))
        self.assertLess(time.monotonic() - started, 1.5 + 1.0)
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()
        # After a complete audio answer, the file the client wrote goes again.
        runner = self.use(json_response(DONE), [(0.0, audio_response(AUDIO)), (0.0, None)])
        started = time.monotonic()
        self.assertFailed(self.client(audio_deadline=1.0, read_timeout=5.0))
        self.assertLess(time.monotonic() - started, 1.0 + 1.5)
        self.assertRequests(runner, ["POST", "GET"])
        self.assertJobDirEmpty()

    def test_bytes_after_the_declared_length_fail_the_fetch(self) -> None:
        # The runner writes exactly the length it declares. More, sent after
        # the answer, is no answer of the runner's.
        runner = self.use([(0.0, json_response(DONE)), (0.2, b"X")])
        self.assertFailed(self.client(read_timeout=2.0))
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()
        runner = self.use(json_response(DONE), [(0.0, audio_response(AUDIO)), (0.2, b"X")])
        self.assertFailed(self.client(read_timeout=2.0))
        self.assertRequests(runner, ["POST", "GET"])
        self.assertJobDirEmpty()


if __name__ == "__main__":
    unittest.main()
