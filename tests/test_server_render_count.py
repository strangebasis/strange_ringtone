"""When a render is counted against its client's rate limit (section 5.2 of docs/PLAN.md).

A render is counted once its upload has arrived whole and passed the WAV
validation, before ffmpeg starts. An upload that is refused for its body
costs its sender nothing: behind a reverse proxy every client may share one
key, and refused uploads that counted would lock everyone else out. A render
that ran has used the budget, whatever it then answered.

Most of these run the real render hook with the stub ffmpeg, so the rule is
tested where it matters and not only against a stand-in hook.
"""
from __future__ import annotations

import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import test_server as base
from corpus.build import CORPUS_MAX_CLIP_SECONDS, corpus_cases, cut_off_body
from strange_ringtone import server as server_module
from strange_ringtone.server import Handler


class RenderCountTestCase(base.RenderFlowTestCase):
    def hits(self) -> int:
        """Every hit the rate limiter holds, over all keys."""
        limiter = self.server.limiter
        with limiter._lock:
            return sum(len(stamps) for stamps in limiter._hits.values())

    def uploads_on_disk(self) -> list[int]:
        """The size of every upload in a job directory right now."""
        return sorted(path.stat().st_size for path in self.scratch.glob("job-*/in.wav"))

    def spy_on_allow(self) -> list[tuple[list[int], int]]:
        """Record, at each counting call, the uploads on disk and the ffmpeg runs so far."""
        limiter = self.server.limiter
        real_allow = limiter.allow
        seen: list[tuple[list[int], int]] = []

        def allow(key: str) -> tuple[bool, int]:
            seen.append((self.uploads_on_disk(), len(self.runs())))
            return real_allow(key)

        patcher = mock.patch.object(limiter, "allow", allow)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def send_whole(self, body: bytes) -> base.Response:
        """Send a render with its whole body; fail if the answer came before the body was read."""
        data = base.build_request("POST", "/api/render", self.render_headers(len(body)), body)
        # A server that answers without reading the body closes on unread
        # bytes, which the client may see as a reset or a broken pipe
        # instead of the answer.
        try:
            response = base.exchange(self.port, data, tolerate_reset=True)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            response = None
        self.assertIsNotNone(response, "the server answered before it had read the upload")
        assert response is not None
        return response

    def assertNoJobLeft(self) -> None:
        """No job directory is left; results the test rendered may stay."""
        def gone() -> bool:
            return not list(self.scratch.glob("job-*"))

        base.wait_for(gone, "the job directories to be removed")

    def assertRateLimited(self, response: base.Response) -> None:
        self.assertJsonError(response, 429, "rate_limited")
        retry_after = response.headers.get("Retry-After")
        assert retry_after is not None
        self.assertRegex(retry_after, r"^[0-9]+$")
        self.assertTrue(1 <= int(retry_after) <= 60)

    def assertOutOfRenders(self) -> None:
        """The client is over its limit: 429 on the headers alone, and ffmpeg does not run."""
        runs = len(self.runs())
        self.assertRateLimited(self.post_render(self.render_headers(1000)))
        self.assertEqual(len(self.runs()), runs)


class RefusedUploadTest(RenderCountTestCase):
    def test_a_refused_wav_costs_no_budget(self) -> None:
        # One render a minute, and every corpus file refused before it: each
        # refusal must leave that one render for the valid upload.
        self.start(clock=self.clock, max_clip_seconds=CORPUS_MAX_CLIP_SECONDS, rate_limit_renders_per_minute=1)
        corpus = self.temp / "corpus"
        corpus.mkdir()
        for case, (path, code) in corpus_cases(corpus).items():
            with self.subTest(case=case):
                response = self.send_whole(path.read_bytes())
                if code == "too_long":
                    self.assertBadWav(response, "too_long", status=413, code="too_large")
                else:
                    self.assertBadWav(response, code)
                self.assertEqual(self.hits(), 0, "a refused WAV was counted against its sender")
                self.assertScratchEmpty()
        self.assertEqual(self.runs(), [])
        self.rendered(self.render())
        self.assertEqual(self.hits(), 1)

    def test_a_body_that_fails_to_arrive_costs_no_budget(self) -> None:
        self.start(clock=self.clock, rate_limit_renders_per_minute=1)

        def cut_off() -> base.Response | None:
            sent, declared = cut_off_body()
            data = base.build_request("POST", "/api/render", self.render_headers(declared), sent)
            return base.exchange(self.port, data, half_close=True)

        def stalled() -> base.Response | None:
            # The socket timeout of a stalled upload: 400 incomplete_body.
            with mock.patch.object(Handler, "timeout", 0.3):
                data = base.build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10)
                return base.exchange(self.port, data)

        def too_slow() -> base.Response | None:
            # The overall body deadline: 408 body_timeout.
            with mock.patch.object(server_module, "BODY_DEADLINE_SECONDS", 0.3):
                data = base.build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10)
                return base.exchange(self.port, data)

        def write_failed() -> base.Response | None:
            # The upload can't be written (a full tmpfs, say): 500 internal_error.
            real_open = open

            def failing_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
                if Path(file).name == "in.wav" and "x" in mode:
                    raise OSError(28, "No space left on device")
                return real_open(file, mode, *args, **kwargs)

            with mock.patch.object(server_module, "open", failing_open, create=True):
                return self.render()

        def client_gone() -> base.Response | None:
            with socket.create_connection(("127.0.0.1", self.port), timeout=base.DEADLINE) as sock:
                sock.sendall(base.build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10))
            # Nobody is left to answer; the log line comes after the cleanup.
            self.wait_for_log("POST /api/render 400", count=3)
            return None

        cases: list[tuple[str, Callable[[], base.Response | None], int | None, str | None]] = [
            ("cut off", cut_off, 400, "incomplete_body"),
            ("stalled", stalled, 400, "incomplete_body"),
            ("past the deadline", too_slow, 408, "body_timeout"),
            ("write failed", write_failed, 500, "internal_error"),
            ("client gone", client_gone, None, None),
        ]
        for label, send, status, code in cases:
            with self.subTest(label):
                response = send()
                if status is not None and code is not None:
                    assert response is not None
                    self.assertJsonError(response, status, code)
                self.assertEqual(self.hits(), 0, "an upload that never arrived was counted against its sender")
                self.assertScratchEmpty()
        self.assertEqual(self.runs(), [])
        # The client's one render is still there.
        self.rendered(self.render())


class CountedRenderTest(RenderCountTestCase):
    def test_a_valid_wav_costs_exactly_one_hit(self) -> None:
        self.start(clock=self.clock, rate_limit_renders_per_minute=2)
        self.rendered(self.render())
        self.assertEqual(self.hits(), 1)
        self.rendered(self.render())
        self.assertEqual(self.hits(), 2)
        self.assertOutOfRenders()
        self.assertNoJobLeft()

    def test_a_render_that_ran_has_used_the_budget_whatever_it_answered(self) -> None:
        def results_full() -> None:
            # Another render took the last place while this one ran.
            patcher = mock.patch.object(self.server.results, "is_full", return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

        cases: list[tuple[str, int, str, dict[str, Any]]] = [
            ("render_failed", 500, "fail", {}),
            ("render_timeout", 504, "hang", {"render_timeout_seconds": 1}),
            ("results_full", 503, "ok", {"max_results": 1}),
        ]
        for code, status, mode, overrides in cases:
            with self.subTest(code):
                self.stub_mode("ok")
                # Two renders a minute: the first fills the only place in the
                # store for the results_full case, and is spent either way.
                self.start(clock=self.clock, rate_limit_renders_per_minute=2, **overrides)
                self.rendered(self.render())
                if code == "results_full":
                    results_full()
                self.stub_mode(mode)
                runs = len(self.runs())
                self.assertJsonError(self.render(), status, code)
                self.assertGreater(len(self.runs()), runs, "ffmpeg never ran")
                self.assertEqual(self.hits(), 2)
                self.assertOutOfRenders()

    def test_the_count_comes_after_validation_and_before_ffmpeg(self) -> None:
        seen = self.spy_on_allow()
        body = self.wav()
        self.rendered(self.render(body=body))
        # Counted once, through the limiter's own locked check, with the whole
        # upload on disk and validated, and before the first ffmpeg run.
        self.assertEqual(seen, [([len(body)], 0)])
        self.assertEqual(len(self.runs()), 2)
        # A refused WAV is never counted at all.
        self.assertBadWav(self.render(body=b"RIFF"), "truncated")
        self.assertEqual(len(seen), 1)

    def test_a_render_hook_never_sees_an_unvalidated_upload(self) -> None:
        # The count is the server's, not the hook's: a stand-in hook that never
        # counts still costs one render, and an upload the validator refuses
        # never reaches it.
        seen: list[Any] = []

        def record(job_dir: Path, handler: Handler) -> None:
            seen.append(getattr(handler, "wav_info", None))
            handler.send_json(200, {"ok": True})

        hook = base.RenderHook(record)
        self.start(render_hook=hook, rate_limit_renders_per_minute=1)
        self.assertBadWav(self.post_render(self.render_headers(4), b"RIFF"), "truncated")
        self.assertEqual(hook.calls, [], "the hook was given an upload the validator refuses")
        self.assertEqual(self.hits(), 0)
        self.assertEqual(self.post_valid().status, 200)
        self.assertEqual(len(hook.calls), 1)
        info = seen[0]
        self.assertIsNotNone(info, "Handler.wav_info: the upload's WavInfo, set before the hook runs")
        self.assertEqual((info.channels, info.sample_rate, info.frames), (1, 44100, 26460))
        self.assertOutOfRenders()
        self.assertEqual(len(hook.calls), 1)
        self.assertScratchEmpty()


class LostCountTest(RenderCountTestCase):
    def race(self) -> None:
        """Let a parallel request of the same key take the last hit right after has_room answers."""
        limiter = self.server.limiter
        real_has_room = limiter.has_room
        real_allow = limiter.allow

        def has_room(key: str) -> tuple[bool, int]:
            answer = real_has_room(key)
            real_allow(key)
            return answer

        patcher = mock.patch.object(limiter, "has_room", has_room)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_count_lost_to_a_parallel_request_is_a_429_after_validation(self) -> None:
        self.start(clock=self.clock, rate_limit_renders_per_minute=1, render_concurrency=1)
        self.race()
        seen = self.spy_on_allow()
        body = self.wav()
        self.assertRateLimited(self.send_whole(body))
        # The count was taken on the validated upload, not before it arrived.
        self.assertEqual(seen, [([len(body)], 0)])
        # The slot is free the moment the answer is out.
        self.assertTrue(self.server.gate.acquire(), "the render slot was still held")
        self.server.gate.release()
        # No render ran, and the upload and its job directory are gone.
        self.assertEqual(self.runs(), [])
        self.assertEqual(len(self.server.results), 0)
        self.wait_for_log("POST /api/render 429")
        self.assertScratchEmpty()

    def test_a_refused_wav_is_refused_for_its_body_even_when_the_count_is_lost(self) -> None:
        # Validation comes first, so the sender learns what is wrong with the
        # upload, and the refusal is free.
        self.start(clock=self.clock, rate_limit_renders_per_minute=1)
        self.race()
        seen = self.spy_on_allow()
        self.assertBadWav(self.send_whole(b"RIFF"), "truncated")
        self.assertEqual(seen, [])
        self.assertEqual(self.runs(), [])
        self.assertScratchEmpty()


class RefusedBeforeTheSlotTest(RenderCountTestCase):
    def test_refusals_before_the_slot_record_no_hit(self) -> None:
        self.start(clock=self.clock, rate_limit_renders_per_minute=1)
        length = str(len(self.wav()))
        # Headers only: each of these is refused before a body byte is read.
        refusals: list[tuple[str, list[tuple[str, str]], str, int]] = [
            ("origin", [("Content-Type", "audio/wav"), ("Content-Length", length)], "", 403),
            ("content type", [("Origin", base.ORIGIN), ("Content-Type", "text/plain"),
                              ("Content-Length", length)], "", 415),
            ("length", [("Origin", base.ORIGIN), ("Content-Type", "audio/wav"), ("Content-Length", "x")], "", 400),
            ("query", self.render_headers(int(length)), "?fade_in=9", 400),
            ("name", self.render_headers(int(length)) + [("X-Tone-Name", "%FF")], "", 400),
        ]
        for label, headers, query, status in refusals:
            with self.subTest(label):
                response = self.post_render(headers, path="/api/render" + query)
                self.assertEqual(response.status, status, response.body)
                self.assertEqual(self.hits(), 0)
        with mock.patch.object(self.server.results, "is_full", return_value=True):
            self.assertJsonError(self.post_render(self.render_headers(int(length))), 503, "results_full")
        self.assertEqual(self.hits(), 0)
        with mock.patch.object(self.server.gate, "acquire", return_value=False):
            self.assertJsonError(self.post_render(self.render_headers(int(length))), 503, "busy")
        self.assertEqual(self.hits(), 0)
        self.assertEqual(self.runs(), [])
        self.rendered(self.render())

    def test_a_client_over_its_limit_is_refused_before_it_uploads(self) -> None:
        # has_room still answers before the slot and before any body byte: a
        # client out of renders gets its 429 while its upload is unsent.
        self.start(clock=self.clock, rate_limit_renders_per_minute=1)
        self.rendered(self.render())
        started = time.monotonic()
        self.assertOutOfRenders()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.hits(), 1)
        self.assertNoJobLeft()


class UnreadableUploadTest(RenderCountTestCase):
    def test_an_upload_that_cannot_be_read_back_is_a_clean_500(self) -> None:
        # The upload arrived whole, but reading it back for validation fails
        # (the scratch directory went away, say). The answer is the JSON 500
        # of a failed write, not a dropped connection or a logged traceback,
        # and an upload that was never validated costs nothing and never
        # reaches the hook.
        hook = base.RenderHook()
        self.start(render_hook=hook, clock=self.clock, rate_limit_renders_per_minute=1)
        unreadable = OSError(2, "No such file or directory")
        with mock.patch.object(server_module, "inspect_wav", side_effect=unreadable):
            self.assertJsonError(self.send_whole(self.valid_body()), 500, "internal_error")
        self.assertEqual(hook.calls, [], "the hook was given an upload that was never validated")
        self.assertEqual(self.hits(), 0, "an upload that was never validated was counted")
        self.wait_for_log("POST /api/render 500")
        self.assertNotIn("error while handling a request", self.log.getvalue())
        self.assertScratchEmpty()
        # The client's one render is still there.
        self.assertEqual(self.post_valid().status, 200)
        self.assertEqual(len(hook.calls), 1)
