"""The fetcher's HTTP server (strange_ringtone_fetch.server) on loopback, port 0, with no network.

Most tests inject a fake runner that the test holds at a gate, so queue and
timing are deterministic; the flow tests run the real runner against the
stub yt-dlp (tests/stubs/ytdlp_stub.py).
"""
from __future__ import annotations

import contextlib
import functools
import http.client
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import server as server_module
from strange_ringtone_fetch.config import Settings
from strange_ringtone_fetch.jobs import Jobs
from strange_ringtone_fetch.server import FetchServer, Handler, parse_job_request, prepare_scratch
from strange_ringtone_fetch.ytdlp import FetchError
from test_fetcher_jobs import FakeClock, FakeRunner
from test_fetcher_ytdlp import StubYtDlp, stub_title

ORIGIN = "https://example.internal"
DEADLINE = 10.0
VIDEO_ID = "dQw4w9WgXcQ"
OTHER_ID = "abcdefghijk"
JOBS = "/fetch/api/jobs"

# Written out rather than taken from http_guard, so that dropping or loosening a
# header there fails here too.
SECURITY_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; "
     "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]
HSTS = "max-age=63072000; includeSubDomains"
LOG_LINE_RE = re.compile(r"[!-~]+ [!-~]+ (?:[0-9]{3}|-) [0-9]+\.[0-9]{3}")


class LogCapture(io.TextIOBase):
    """A stderr stand-in that handler threads can write to concurrently."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._parts: list[str] = []

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def getvalue(self) -> str:
        with self._lock:
            return "".join(self._parts)

    def lines(self) -> list[str]:
        return self.getvalue().splitlines()


class Response:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def exchange(port: int, data: bytes, half_close: bool = False) -> Response:
    """Send raw bytes on a fresh loopback connection and parse the reply."""
    method = data.split(b" ", 1)[0].decode("latin-1")
    with socket.create_connection(("127.0.0.1", port), timeout=DEADLINE) as sock:
        sock.sendall(data)
        if half_close:
            sock.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(sock, method=method)
        try:
            response.begin()
            body = response.read()
        finally:
            response.close()
    return Response(response.status, response.headers, body)


def build_request(method: str, path: str, headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: example.internal"]
    lines += [f"{name}: {value}" for name, value in headers or []]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def job_body(video_id: str = VIDEO_ID) -> bytes:
    return json.dumps({"video_id": video_id}).encode("utf-8")


def wait_for(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


class _TestServer(FetchServer):
    # Join the handler threads on close, so no handler still writes to scratch
    # when the temporary directory is removed.
    block_on_close = True


class _CappedServer(_TestServer):
    MAX_WORKERS = 2


class ServerTestCase(unittest.TestCase):
    server_class: type[FetchServer] = _TestServer

    def setUp(self) -> None:
        self.log = LogCapture()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        # Doesn't exist yet: the server must create it.
        self.scratch = self.temp / "scratch"
        self.runner: Any = FakeRunner()
        self.clock = FakeClock()
        self.start()

    def start(self, runner: Any = None, **overrides: Any) -> None:
        values: dict[str, Any] = {
            "public_origin": ORIGIN,
            # High, so only the rate-limit tests ever meet the limit.
            "rate_limit_fetches_per_minute": 1000,
            "scratch_dir": self.scratch,
        }
        values.update(overrides)
        self.settings = Settings(**values)
        if runner is not None:
            self.runner = runner
        factory = functools.partial(Jobs, clock=self.clock)
        self.server = self.server_class(("127.0.0.1", 0), self.settings, self.runner, factory)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, self.server, thread)
        self.port = self.server.server_address[1]

    @staticmethod
    def _stop(server: FetchServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(DEADLINE)

    def request(self, method: str, path: str, headers: list[tuple[str, str]] | None = None,
                body: bytes = b"", **options: Any) -> Response:
        return exchange(self.port, build_request(method, path, headers, body), **options)

    def job_headers(self, length: int, origin: str = ORIGIN) -> list[tuple[str, str]]:
        return [("Origin", origin), ("Content-Type", "application/json"), ("Content-Length", str(length))]

    def post_job(self, video_id: str = VIDEO_ID, extra: list[tuple[str, str]] | None = None) -> Response:
        body = job_body(video_id)
        return self.request("POST", JOBS, self.job_headers(len(body)) + (extra or []), body)

    def create_job(self, video_id: str = VIDEO_ID, extra: list[tuple[str, str]] | None = None) -> str:
        response = self.post_job(video_id, extra)
        self.assertEqual(response.status, 202, response.body)
        job_id = response.json()["job_id"]
        return job_id

    def status(self, job_id: str) -> Response:
        return self.request("GET", f"{JOBS}/{job_id}")

    def wait_done(self, job_id: str) -> dict[str, Any]:
        answers: list[dict[str, Any]] = []

        def finished() -> bool:
            response = self.status(job_id)
            if response.status != 200:
                return False
            answers.append(response.json())
            return answers[-1]["status"] in ("done", "failed")

        wait_for(finished, "the job to finish")
        return answers[-1]

    def gate(self, video_id: str = VIDEO_ID) -> threading.Event:
        gate = threading.Event()
        self.runner.gates[video_id] = gate
        self.addCleanup(gate.set)
        return gate

    def wait_for_log(self, prefix: str, count: int = 1) -> list[str]:
        def found() -> bool:
            return len([line for line in self.log.lines() if line.startswith(prefix)]) >= count

        wait_for(found, f"a log line starting {prefix!r}")
        return [line for line in self.log.lines() if line.startswith(prefix)]

    # --- assertions -------------------------------------------------------------

    def scratch_contents(self) -> list[str]:
        names = [p.relative_to(self.scratch).as_posix() for p in self.scratch.rglob("*")]
        return sorted(name for name in names if name != "results")

    def assertScratchEmpty(self) -> None:
        try:
            wait_for(lambda: not self.scratch_contents(), "an empty scratch directory")
        except AssertionError:
            pass
        self.assertEqual(self.scratch_contents(), [])

    def assertSecurityHeaders(self, response: Response, hsts: bool = True) -> None:
        for name, value in SECURITY_HEADERS:
            self.assertEqual(response.headers.get_all(name), [value], name)
        if hsts:
            self.assertEqual(response.headers.get_all("Strict-Transport-Security"), [HSTS])
        else:
            self.assertIsNone(response.headers.get_all("Strict-Transport-Security"))
        self.assertEqual(response.headers.get_all("Server"), ["strange_ringtone"])
        self.assertEqual(response.headers.get_all("Connection"), ["close"])
        self.assertEqual(response.headers.get_all("Content-Length"), [str(len(response.body))])
        self.assertEqual(response.headers.get_all("Cache-Control"), ["no-store"])
        self.assertFalse([name for name in response.headers if name.lower().startswith("access-control-")])

    def assertJsonError(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status, status, response.body)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")
        self.assertEqual(response.json(), {"error": code})
        self.assertSecurityHeaders(response, hsts=self.settings.public_origin.startswith("https://"))


class HeadersTest(ServerTestCase):
    def test_every_kind_of_response_carries_the_header_set(self) -> None:
        done = self.create_job(OTHER_ID)
        self.wait_done(done)
        # Held, so the job the 202 case creates never finishes and evicts this one.
        self.gate(VIDEO_ID)
        body = job_body()
        cases: list[tuple[str, bytes, bool]] = [
            ("healthz", build_request("GET", "/fetch/healthz"), False),
            ("202", build_request("POST", JOBS, self.job_headers(len(body)), body), False),
            ("status", build_request("GET", f"{JOBS}/{done}"), False),
            ("audio", build_request("GET", f"{JOBS}/{done}/audio"), False),
            ("404", build_request("GET", "/nope"), False),
            ("404 job", build_request("GET", f"{JOBS}/{'0' * 32}"), False),
            ("405", build_request("POST", "/fetch/healthz"), False),
            ("403", build_request("POST", JOBS, [("Content-Length", "0")]), False),
            ("400 json", build_request("POST", JOBS, self.job_headers(2), b"[]"), False),
            ("400 bad request line", b"GET / HTTP/1.0 extra\r\n", True),
            ("400 HTTP/0.9", b"GET /\r\n", True),
            ("405 unknown verb", b"BREW /fetch/healthz HTTP/1.0\r\n\r\n", False),
            ("414", b"GET /" + b"a" * (65537 - 5), False),
        ]
        for label, data, half_close in cases:
            with self.subTest(label):
                self.assertSecurityHeaders(exchange(self.port, data, half_close=half_close))

    def test_http_origin_gets_no_hsts(self) -> None:
        self.start(public_origin="http://example.internal")
        for path in ("/fetch/healthz", "/nope"):
            with self.subTest(path):
                self.assertSecurityHeaders(self.request("GET", path), hsts=False)

    def test_stdlib_errors_are_json(self) -> None:
        for label, data, status in (
            ("bad request line", b"GET / HTTP/1.0 extra\r\n", 400),
            ("HTTP/0.9", b"GET /\r\n", 400),
            ("request line too long", b"GET /" + b"a" * (65537 - 5), 414),
        ):
            with self.subTest(label):
                response = exchange(self.port, data, half_close=True)
                self.assertJsonError(response, status, f"http_{status}")
        self.assertScratchEmpty()

    def test_responses_are_http_1_0_and_close(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            # Keep-alive asked for, and refused: one request per connection.
            sock.sendall(build_request("GET", "/fetch/healthz", [("Connection", "keep-alive")]))
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 200 "))
        self.assertIn(b"\r\nConnection: close\r\n", data)


class HealthzTest(ServerTestCase):
    def test_ok_and_nothing_else(self) -> None:
        response = self.request("GET", "/fetch/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(response.headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertSecurityHeaders(response)
        # Nothing about the version, the queue or the settings.
        self.assertEqual(sorted(name.lower() for name in response.headers), sorted(
            [name.lower() for name, _ in SECURITY_HEADERS]
            + ["strict-transport-security", "server", "date", "connection", "content-type", "content-length",
               "cache-control"]
        ))

    def test_the_cores_health_path_is_not_ours(self) -> None:
        self.assertJsonError(self.request("GET", "/healthz"), 404, "not_found")


class MethodTest(ServerTestCase):
    def test_wrong_method_on_known_paths(self) -> None:
        job = "0" * 32
        cases = [
            ("POST", "/fetch/healthz", "GET"),
            ("GET", JOBS, "POST"),
            ("PUT", JOBS, "POST"),
            ("DELETE", JOBS, "POST"),
            ("OPTIONS", JOBS, "POST"),
            ("HEAD", JOBS, "POST"),
            ("TRACE", JOBS, "POST"),
            ("BREW", JOBS, "POST"),
            ("post", JOBS, "POST"),
            ("POST", f"{JOBS}/{job}", "GET"),
            ("DELETE", f"{JOBS}/{job}", "GET"),
            ("POST", f"{JOBS}/{job}/audio", "GET"),
            ("HEAD", f"{JOBS}/{job}/audio", "GET"),
        ]
        for method, path, allow in cases:
            with self.subTest(f"{method} {path}"):
                data = build_request(method, path, [("Content-Length", "0")])
                with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                    sock.sendall(data)
                    raw = b""
                    while chunk := sock.recv(65536):
                        raw += chunk
                head, _, rest = raw.partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(b"HTTP/1.0 405 "), head)
                self.assertIn(f"\r\nAllow: {allow}\r\n".encode(), head + b"\r\n")
                if method == "HEAD":
                    self.assertEqual(rest, b"")
                else:
                    self.assertEqual(json.loads(rest), {"error": "method_not_allowed"})
        self.assertScratchEmpty()

    def test_preflight_is_never_approved(self) -> None:
        response = self.request("OPTIONS", JOBS, [
            ("Origin", "https://other.example.internal"),
            ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "content-type"),
        ])
        self.assertJsonError(response, 405, "method_not_allowed")

    def test_unknown_paths_and_path_tricks(self) -> None:
        job = "0" * 32
        paths = [
            "/", "/healthz", "/api/render", "/static/dev/app.js", "/fetch", "/fetch/", "/fetch/api",
            f"{JOBS}/", f"{JOBS}/{job}/", f"{JOBS}/{job}/audio/", f"{JOBS}/{job.upper()}".replace("0", "A"),
            f"{JOBS}/{'0' * 31}", f"{JOBS}/{'0' * 33}", f"{JOBS}/{job}/audio.m4a", f"{JOBS}/{job}/../{job}",
            "/fetch/api/jobs/..%2f..%2fhealthz", "/fetch//api/jobs", "//fetch/api/jobs", "/FETCH/api/jobs",
            "/fetch/api/jobs%2f" + job, "/fetch/healthz/", "/fetch/healthz/..",
        ]
        for method in ("GET", "POST"):
            for path in paths:
                with self.subTest(method=method, path=path):
                    body = job_body()
                    response = self.request(method, path, self.job_headers(len(body)), body)
                    self.assertJsonError(response, 404, "not_found")
        self.assertEqual(len(self.server.jobs), 0)
        self.assertScratchEmpty()

    def test_post_with_a_query_is_not_the_jobs_route(self) -> None:
        body = job_body()
        for path in (f"{JOBS}?", f"{JOBS}?video_id=x", f"{JOBS}#x"):
            with self.subTest(path=path):
                self.assertJsonError(self.request("POST", path, self.job_headers(len(body)), body), 404, "not_found")
        self.assertEqual(len(self.server.jobs), 0)

    def test_a_rewritten_target_is_not_routed(self) -> None:
        body = job_body()
        for target in (JOBS + "\x0b", JOBS + "\xa0", "//" + JOBS.lstrip("/")):
            with self.subTest(target=target):
                data = build_request("POST", target, self.job_headers(len(body)), body)
                response = exchange(self.port, data)
                self.assertNotEqual(response.status, 202)
        self.assertEqual(len(self.server.jobs), 0)


class OriginTest(ServerTestCase):
    def test_bad_origins_are_refused(self) -> None:
        body = job_body()
        base = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        for origins in ([], ["null"], ["https://other.example.internal"], [ORIGIN + "/"], [ORIGIN.upper()],
                        ["http://example.internal"], [ORIGIN + ":443"], [ORIGIN, ORIGIN], [" " + ORIGIN + "x"]):
            with self.subTest(origins=origins):
                headers = [("Origin", origin) for origin in origins] + base
                self.assertJsonError(self.request("POST", JOBS, headers, body), 403, "bad_origin")
        self.assertEqual(len(self.server.jobs), 0)
        self.assertEqual(self.runner.calls, [])

    def test_exact_origin_passes(self) -> None:
        response = self.post_job()
        self.assertEqual(response.status, 202)
        self.assertSecurityHeaders(response)


class ContentTypeTest(ServerTestCase):
    def test_anything_but_application_json_is_refused(self) -> None:
        body = job_body()
        for types in ([], ["text/plain"], ["application/json; charset=utf-8"], ["application/json", "application/json"],
                      ["application/x-www-form-urlencoded"], ["multipart/form-data"], ["APPLICATION/JSON"],
                      ["application/jsonx"], ["text/json"]):
            with self.subTest(types=types):
                headers = [("Origin", ORIGIN), ("Content-Length", str(len(body)))]
                headers += [("Content-Type", value) for value in types]
                self.assertJsonError(self.request("POST", JOBS, headers, body), 415, "unsupported_media_type")
        self.assertEqual(len(self.server.jobs), 0)


class LengthTest(ServerTestCase):
    def base(self) -> list[tuple[str, str]]:
        return [("Origin", ORIGIN), ("Content-Type", "application/json")]

    def test_transfer_encoding_is_refused(self) -> None:
        headers = self.base() + [("Transfer-Encoding", "chunked"), ("Content-Length", "27")]
        response = self.request("POST", JOBS, headers, b"1b\r\n" + job_body() + b"\r\n0\r\n\r\n")
        self.assertJsonError(response, 400, "transfer_encoding")

    def test_missing_length(self) -> None:
        self.assertJsonError(self.request("POST", JOBS, self.base()), 411, "length_required")

    def test_malformed_lengths(self) -> None:
        for values in (["+27"], [" 27x"], ["27", "27"], ["2_7"], ["-1"], ["0x1b"], ["٢٧"]):
            with self.subTest(values=values):
                headers = self.base() + [("Content-Length", value) for value in values]
                data = build_request("POST", JOBS, headers, job_body()) if values[0].isascii() else \
                    ("\r\n".join([f"POST {JOBS} HTTP/1.1", "Host: x", *(f"{n}: {v}" for n, v in headers)])
                     + "\r\n\r\n").encode("utf-8")
                self.assertJsonError(exchange(self.port, data), 400, "bad_length")

    def test_too_large(self) -> None:
        headers = self.base() + [("Content-Length", "1025")]
        self.assertJsonError(self.request("POST", JOBS, headers, b" " * 1025), 413, "too_large")
        headers = self.base() + [("Content-Length", "9" * 400)]
        self.assertJsonError(self.request("POST", JOBS, headers), 413, "too_large")

    def test_exactly_the_limit_is_accepted(self) -> None:
        body = job_body()
        body = body + b" " * (1024 - len(body))
        response = self.request("POST", JOBS, self.base() + [("Content-Length", "1024")], body)
        self.assertEqual(response.status, 202, response.body)


class BodyTest(ServerTestCase):
    def post(self, body: bytes) -> Response:
        return self.request("POST", JOBS, self.job_headers(len(body)), body)

    def test_only_an_object_with_exactly_video_id_is_accepted(self) -> None:
        for body in (
            b"", b"x", b"null", b"[]", b'"dQw4w9WgXcQ"', b"{}", b'{"video_id": 5}', b'{"video_id": null}',
            b'{"video_id": ["dQw4w9WgXcQ"]}', b'{"video_id": {"id": "dQw4w9WgXcQ"}}',
            b'{"video_id": "dQw4w9WgXcQ", "url": "https://example.internal/"}',
            b'{"id": "dQw4w9WgXcQ"}', b'{"VIDEO_ID": "dQw4w9WgXcQ"}',
            b'{"video_id": "abcdefghijk", "video_id": "dQw4w9WgXcQ"}',
            b'{"video_id": "dQw4w9WgXcQ", "x": NaN}', b'{"video_id": "dQw4w9WgXcQ"', b"\xff\xfe{}",
            '{"video_id": "dQw4w9WgXcQ"}'.encode("utf-16"), b"[" * 600 + b"]" * 400,
        ):
            with self.subTest(body=body[:40]):
                self.assertJsonError(self.post(body), 400, "bad_json")
        self.assertEqual(len(self.server.jobs), 0)
        self.assertScratchEmpty()

    def test_the_id_must_be_eleven_safe_characters(self) -> None:
        for video_id in ("", "dQw4w9WgXc", "dQw4w9WgXcQQ", "dQw4w9WgX.Q", "../../../..", "dQw4w9WgX%2",
                         "dQw4w9W/XcQ", "dQw4w9WgX٥Q", "dQw4w9WgXc\n", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                         "dQw4w9WgX Q", "dQw4w9WgXé"):
            with self.subTest(video_id=video_id):
                self.assertJsonError(self.post(job_body(video_id)), 400, "bad_video_id")
        self.assertEqual(len(self.server.jobs), 0)
        self.assertEqual(self.runner.calls, [])
        self.assertScratchEmpty()

    def test_runaway_nesting_is_bad_json(self) -> None:
        # CPython 3.12's decoder doesn't recurse that deep within 1 KiB, but
        # a decoder that does must still get a 400, not a dropped connection.
        # json is one module for everybody, so the response is parsed only
        # once the patch is gone.
        with mock.patch.object(server_module.json, "loads", side_effect=RecursionError):
            response = self.post(job_body())
        self.assertJsonError(response, 400, "bad_json")
        self.assertEqual(len(self.server.jobs), 0)

    def test_parse_job_request(self) -> None:
        self.assertEqual(parse_job_request(b'  {"video_id" : "-_-_-_-_-_-"}\n'), "-_-_-_-_-_-")

    def test_body_shorter_than_length(self) -> None:
        data = build_request("POST", JOBS, self.job_headers(100), job_body())
        self.assertJsonError(exchange(self.port, data, half_close=True), 400, "incomplete_body")
        self.assertEqual(len(self.server.jobs), 0)

    def test_bytes_past_the_declared_length_are_never_read(self) -> None:
        body = job_body()
        data = build_request("POST", JOBS, self.job_headers(len(body)), body + b'{"video_id": "abcdefghijk"}')
        self.assertEqual(exchange(self.port, data).status, 202)
        self.assertEqual(len(self.server.jobs), 1)

    def test_stalled_body_times_out(self) -> None:
        with mock.patch.object(Handler, "timeout", 0.3):
            self.start()
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                sock.sendall(build_request("POST", JOBS, self.job_headers(100), b"{"))
                self.wait_for_log("POST /fetch/api/jobs 400")
        self.assertEqual(len(self.server.jobs), 0)

    def test_trickled_body_meets_the_deadline(self) -> None:
        self.assertGreater(server_module.BODY_DEADLINE_SECONDS, 0)
        self.assertLessEqual(server_module.BODY_DEADLINE_SECONDS, 60)
        with mock.patch.object(server_module, "BODY_DEADLINE_SECONDS", 0.5):
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                started = time.monotonic()
                sock.sendall(build_request("POST", JOBS, self.job_headers(1000), b" "))
                while time.monotonic() < started + 4 and not any(
                        line.startswith("POST /fetch/api/jobs 408") for line in self.log.lines()):
                    try:
                        sock.sendall(b" ")
                    except OSError:
                        break
                    time.sleep(0.05)
                self.wait_for_log("POST /fetch/api/jobs 408")
                self.assertLess(time.monotonic() - started, 0.5 + 1.5)
        self.assertEqual(len(self.server.jobs), 0)


class CheckOrderTest(ServerTestCase):
    def test_path_before_origin(self) -> None:
        self.assertJsonError(self.request("POST", "/fetch/api/job", [("Content-Length", "0")]), 404, "not_found")

    def test_origin_before_content_type_and_length(self) -> None:
        response = self.request("POST", JOBS, [("Origin", "https://other.example.internal")])
        self.assertJsonError(response, 403, "bad_origin")

    def test_content_type_before_length(self) -> None:
        response = self.request("POST", JOBS, [("Origin", ORIGIN), ("Content-Type", "text/plain")])
        self.assertJsonError(response, 415, "unsupported_media_type")

    def test_length_before_rate_limit_and_queue(self) -> None:
        self.start(rate_limit_fetches_per_minute=1)
        self.create_job()
        headers = [("Origin", ORIGIN), ("Content-Type", "application/json"), ("Content-Length", "2000")]
        self.assertJsonError(self.request("POST", JOBS, headers), 413, "too_large")

    def test_rate_limit_before_busy_and_before_the_body(self) -> None:
        self.start(rate_limit_fetches_per_minute=1)
        self.gate(VIDEO_ID)
        self.create_job(VIDEO_ID)
        # The body is not JSON: a 429 proves it was never read.
        response = self.request("POST", JOBS, self.job_headers(3), b"???")
        self.assertJsonError(response, 429, "rate_limited")
        self.assertTrue(response.headers.get("Retry-After", "").isdigit())

    def test_busy_before_the_body(self) -> None:
        self.gate(VIDEO_ID)
        self.create_job(VIDEO_ID)
        wait_for(lambda: self.runner.running == 1, "the first job to run")
        for _ in range(4):
            self.create_job(OTHER_ID)
        response = self.request("POST", JOBS, self.job_headers(3), b"???")
        self.assertJsonError(response, 503, "busy")
        self.assertEqual(response.headers.get("Retry-After"), "5")

    def test_refused_requests_do_not_use_up_the_rate_limit(self) -> None:
        self.start(rate_limit_fetches_per_minute=1)
        self.assertEqual(self.request("POST", JOBS, self.job_headers(2), b"[]").status, 400)
        self.assertEqual(self.request("POST", JOBS, self.job_headers(len(job_body("x"))), job_body("x")).status, 400)
        self.assertEqual(self.request("POST", JOBS, [("Origin", ORIGIN)]).status, 415)
        self.assertEqual(self.post_job().status, 202)
        self.assertEqual(self.post_job().status, 429)


class RateLimitTest(ServerTestCase):
    def test_limit_per_client_counts_accepted_jobs(self) -> None:
        self.start(rate_limit_fetches_per_minute=2)
        self.assertEqual(self.post_job().status, 202)
        self.assertEqual(self.post_job(OTHER_ID).status, 202)
        response = self.post_job()
        self.assertJsonError(response, 429, "rate_limited")
        self.assertTrue(1 <= int(response.headers["Retry-After"]) <= 60)

    def test_default_is_five_per_minute(self) -> None:
        self.assertEqual(Settings(public_origin=ORIGIN).rate_limit_fetches_per_minute, 5)

    def test_parallel_requests_cannot_overspend(self) -> None:
        self.start(rate_limit_fetches_per_minute=3)
        self.runner.gates[VIDEO_ID] = gate = threading.Event()
        self.addCleanup(gate.set)
        statuses: list[int] = []
        lock = threading.Lock()

        def post() -> None:
            status = self.post_job().status
            with lock:
                statuses.append(status)

        threads = [threading.Thread(target=post) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(DEADLINE)
        self.assertEqual(statuses.count(202), 3, statuses)
        self.assertEqual(sorted(set(statuses) - {202}), [429])

    def test_the_limit_is_checked_again_once_the_body_is_in(self) -> None:
        # Another request from the same client may have taken the last slot
        # while this one's body arrived; the second look, under the submit
        # lock, is what refuses it.
        self.start(rate_limit_fetches_per_minute=1)
        self.create_job()
        real = self.server.limiter.has_room
        answers = iter([(True, 0)])
        with mock.patch.object(self.server.limiter, "has_room",
                               side_effect=lambda key: next(answers, None) or real(key)):
            self.assertJsonError(self.post_job(OTHER_ID), 429, "rate_limited")
        self.assertEqual(len(self.server.jobs), 1)

    def test_forwarded_for_is_ignored_from_untrusted_peers(self) -> None:
        self.start(rate_limit_fetches_per_minute=1)
        self.assertEqual(self.post_job(extra=[("X-Forwarded-For", "198.51.100.1")]).status, 202)
        self.assertEqual(self.post_job(extra=[("X-Forwarded-For", "198.51.100.2")]).status, 429)

    def test_forwarded_for_is_honoured_from_trusted_proxies(self) -> None:
        import ipaddress

        self.start(rate_limit_fetches_per_minute=1, trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),))
        self.assertEqual(self.post_job(extra=[("X-Forwarded-For", "198.51.100.1")]).status, 202)
        self.assertEqual(self.post_job(extra=[("X-Forwarded-For", "198.51.100.2")]).status, 202)
        self.assertEqual(self.post_job(extra=[("X-Forwarded-For", "203.0.113.9, 198.51.100.1")]).status, 429)
        # Without the header the proxy itself is the client.
        self.assertEqual(self.post_job().status, 202)
        self.assertEqual(self.post_job().status, 429)


class QueueTest(ServerTestCase):
    def test_four_queued_plus_one_running_then_busy(self) -> None:
        gate = self.gate(VIDEO_ID)
        first = self.create_job(VIDEO_ID)
        wait_for(lambda: self.runner.running == 1, "the first job to run")
        queued = [self.create_job(OTHER_ID) for _ in range(4)]
        response = self.post_job(OTHER_ID)
        self.assertJsonError(response, 503, "busy")
        self.assertEqual(response.headers.get("Retry-After"), "5")
        self.assertEqual(self.status(first).json(), {"status": "running"})
        for job_id in queued:
            self.assertEqual(self.status(job_id).json(), {"status": "queued"})
        # One at a time: the others wait while the first is held.
        time.sleep(0.2)
        self.assertEqual(self.runner.calls, [VIDEO_ID])
        gate.set()
        # Each finished job evicts the one before it, so only the last stays.
        self.assertEqual(self.wait_done(queued[-1])["status"], "done")
        self.assertEqual(self.runner.calls, [VIDEO_ID] + [OTHER_ID] * 4)
        self.assertEqual(self.runner.max_running, 1)

    def test_busy_refusals_do_not_use_up_the_rate_limit(self) -> None:
        self.start(rate_limit_fetches_per_minute=6)
        gate = self.gate(VIDEO_ID)
        self.create_job(VIDEO_ID)
        wait_for(lambda: self.runner.running == 1, "the first job to run")
        for _ in range(4):
            self.create_job(OTHER_ID)
        for _ in range(3):
            self.assertEqual(self.post_job(OTHER_ID).status, 503)
        gate.set()
        wait_for(lambda: not self.server.jobs.is_full() and self.runner.running == 0, "the queue to drain")
        self.assertEqual(self.post_job(OTHER_ID).status, 202)


class StatusTest(ServerTestCase):
    def test_created_job(self) -> None:
        response = self.post_job()
        self.assertEqual(response.status, 202)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")
        self.assertSecurityHeaders(response)
        payload = response.json()
        self.assertEqual(sorted(payload), ["job_id"])
        self.assertRegex(payload["job_id"], r"^[0-9a-f]{32}$")

    def test_states(self) -> None:
        gate = self.gate(VIDEO_ID)
        # Held for good: once finished it would evict the first job's result.
        self.gate(OTHER_ID)
        running = self.create_job(VIDEO_ID)
        wait_for(lambda: self.status(running).json() == {"status": "running"}, "running")
        queued = self.create_job(OTHER_ID)
        self.assertEqual(self.status(queued).json(), {"status": "queued"})
        gate.set()
        done = self.wait_done(running)
        self.assertEqual(done, {"status": "done", "title": "A Title", "duration": 10.5,
                                "heatmap": [{"start": 0.0, "end": 10.5, "value": 1.0}]})
        response = self.status(running)
        self.assertSecurityHeaders(response)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")

    def test_failed(self) -> None:
        self.runner.failures[VIDEO_ID] = FetchError("too_long")
        job_id = self.create_job()
        self.assertEqual(self.wait_done(job_id), {"status": "failed", "error": "too_long"})

    def test_unknown_ids(self) -> None:
        for job_id in ("0" * 32, "f" * 32):
            with self.subTest(job_id=job_id):
                self.assertJsonError(self.status(job_id), 404, "not_found")
                self.assertJsonError(self.request("GET", f"{JOBS}/{job_id}/audio"), 404, "not_found")

    def test_expired_ids(self) -> None:
        job_id = self.create_job()
        self.wait_done(job_id)
        self.clock.now += 600
        self.assertJsonError(self.status(job_id), 404, "not_found")
        self.assertJsonError(self.request("GET", f"{JOBS}/{job_id}/audio"), 404, "not_found")


class AudioTest(ServerTestCase):
    def test_streamed_once_then_deleted(self) -> None:
        job_id = self.create_job()
        self.wait_done(job_id)
        response = self.request("GET", f"{JOBS}/{job_id}/audio")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"AUDIO " + VIDEO_ID.encode("ascii"))
        self.assertEqual(response.headers.get("Content-Type"), "audio/mp4")
        self.assertEqual(response.headers.get("Content-Disposition"), 'attachment; filename="audio.m4a"')
        self.assertSecurityHeaders(response)
        # Gone: the audio, the status and the file.
        self.assertJsonError(self.request("GET", f"{JOBS}/{job_id}/audio"), 404, "not_found")
        self.assertJsonError(self.status(job_id), 404, "not_found")
        self.assertScratchEmpty()

    def test_not_before_it_is_done(self) -> None:
        gate = self.gate(VIDEO_ID)
        job_id = self.create_job()
        self.assertJsonError(self.request("GET", f"{JOBS}/{job_id}/audio"), 404, "not_found")
        gate.set()
        self.wait_done(job_id)
        self.assertEqual(self.request("GET", f"{JOBS}/{job_id}/audio").status, 200)

    def test_large_audio_is_streamed_in_chunks(self) -> None:
        job_id = self.create_job()
        self.wait_done(job_id)
        path = self.scratch / "results" / f"{job_id}.m4a"
        data = os.urandom(3 * server_module.CHUNK_BYTES + 17)
        path.write_bytes(data)
        response = self.request("GET", f"{JOBS}/{job_id}/audio")
        self.assertEqual(response.body, data)
        # Deleted once the response is written, which may trail its last byte.
        wait_for(lambda: not path.exists(), "the audio to be deleted")

    def test_a_client_that_goes_away_still_deletes_the_audio(self) -> None:
        job_id = self.create_job()
        self.wait_done(job_id)
        path = self.scratch / "results" / f"{job_id}.m4a"
        path.write_bytes(b"x" * (8 * 1024 * 1024))
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            sock.sendall(build_request("GET", f"{JOBS}/{job_id}/audio"))
            sock.recv(100)
        self.wait_for_log("GET /fetch/api/jobs/<id>/audio 200")
        wait_for(lambda: not path.exists(), "the audio to be deleted")
        self.assertJsonError(self.status(job_id), 404, "not_found")

    def test_one_held_result(self) -> None:
        older = self.create_job(VIDEO_ID)
        self.wait_done(older)
        newer = self.create_job(OTHER_ID)
        self.wait_done(newer)
        self.assertJsonError(self.status(older), 404, "not_found")
        self.assertJsonError(self.request("GET", f"{JOBS}/{older}/audio"), 404, "not_found")
        self.assertEqual(self.scratch_contents(), [f"results/{newer}.m4a"])


class FlowTest(ServerTestCase):
    """The real runner and the stub yt-dlp, end to end over HTTP."""

    def setUp(self) -> None:
        super().setUp()
        self.start(runner=StubYtDlp(600, 1024 * 1024, 5))

    def test_fetch_poll_download(self) -> None:
        job_id = self.create_job()
        status = self.wait_done(job_id)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["title"], stub_title(VIDEO_ID))
        self.assertEqual(status["duration"], 215.5)
        self.assertEqual(len(status["heatmap"]), 100)
        self.assertEqual(sorted(status), ["duration", "heatmap", "status", "title"])
        response = self.request("GET", f"{JOBS}/{job_id}/audio")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))
        self.assertScratchEmpty()

    def test_the_log_never_holds_a_title_an_id_or_an_address(self) -> None:
        job_id = self.create_job("ctrlTitle__")
        self.wait_done(job_id)
        self.assertEqual(self.request("GET", f"{JOBS}/{job_id}/audio").status, 200)
        failed = self.create_job("blocked____")
        self.assertEqual(self.wait_done(failed), {"status": "failed", "error": "blocked"})
        self.wait_for_log("GET /fetch/api/jobs/<id>/audio 200")
        log = self.log.getvalue()
        for secret in ("Stub", "Title", "ctrlTitle__", "blocked____", job_id, failed, "127.0.0.1", "youtube"):
            self.assertNotIn(secret, log)
        for line in self.log.lines():
            self.assertRegex(line, LOG_LINE_RE)

    def test_every_failure_leaves_the_scratch_directory_empty(self) -> None:
        cases = {
            "tooLong____": "too_long", "blocked____": "blocked", "notFound___": "not_found",
            "failing____": "failed", "noAudio____": "failed", "notADict___": "failed", "hanging____": "timeout",
        }
        self.start(runner=StubYtDlp(600, 1024 * 1024, 1))
        with mock.patch("strange_ringtone_fetch.ytdlp.DRAIN_SECONDS", 0.5):
            for video_id, code in cases.items():
                with self.subTest(video_id=video_id):
                    job_id = self.create_job(video_id)
                    self.assertEqual(self.wait_done(job_id), {"status": "failed", "error": code})
                    self.assertScratchEmpty()


class LogTest(ServerTestCase):
    def test_line_format_without_query_or_headers(self) -> None:
        response = self.request("GET", "/fetch/healthz?x=SecretQuery", [("User-Agent", "SecretAgent")])
        self.assertEqual(response.status, 200)
        line = self.wait_for_log("GET /fetch/healthz ")[0]
        self.assertRegex(line, r"^GET /fetch/healthz 200 [0-9]+\.[0-9]{3}$")
        self.assertNotIn("Secret", self.log.getvalue())

    def test_job_ids_are_masked(self) -> None:
        job_id = self.create_job()
        self.status(job_id)
        self.request("GET", f"/fetch/api/jobs/{job_id.upper()}x")
        lines = self.wait_for_log("GET ", count=2)
        self.assertNotIn(job_id, self.log.getvalue())
        self.assertNotIn(job_id.upper(), self.log.getvalue())
        self.assertIn("GET /fetch/api/jobs/<id> 200", [line.rsplit(" ", 1)[0] for line in lines])

    def test_the_request_body_is_never_logged(self) -> None:
        self.post_job("SecretId123")
        self.request("POST", JOBS, self.job_headers(20), b'{"video_id": "Secret')
        self.wait_for_log("POST /fetch/api/jobs ", count=2)
        self.assertNotIn("Secret", self.log.getvalue())

    def test_control_characters_and_long_paths(self) -> None:
        self.request("GET", "/x\x1b[31mred")
        self.request("GET", "/" + "z" * 5000)
        lines = self.wait_for_log("GET /", count=2)
        self.assertNotIn("\x1b", self.log.getvalue())
        self.assertTrue(any(line.startswith("GET /x_[31mred 404 ") for line in lines))
        long_line = next(line for line in lines if line.startswith("GET /zzz"))
        self.assertEqual(long_line.split(" ")[1], "/" + "z" * 199)

    def test_unexpected_errors_log_one_line_without_a_traceback(self) -> None:
        def explode(handler: Handler) -> None:
            raise RuntimeError("SecretDetail")

        with mock.patch.object(Handler, "do_GET", explode):
            try:
                exchange(self.port, build_request("GET", "/fetch/healthz"))
            except (ConnectionError, http.client.HTTPException):
                pass
            wait_for(lambda: "error while handling a request" in self.log.getvalue(), "the error line")
        self.assertIn("error while handling a request: RuntimeError\n", self.log.getvalue())
        self.assertNotIn("Traceback", self.log.getvalue())
        self.assertNotIn("SecretDetail", self.log.getvalue())


class WorkerCapTest(ServerTestCase):
    server_class = _CappedServer

    def test_connections_past_the_cap_get_503(self) -> None:
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) for _ in range(2)]
        try:
            response = exchange(self.port, b"")
            self.assertJsonError(response, 503, "busy")
            self.assertEqual(response.headers.get("Retry-After"), "1")
        finally:
            for sock in idle:
                sock.close()

        def healthy() -> bool:
            return exchange(self.port, build_request("GET", "/fetch/healthz")).status == 200

        wait_for(healthy, "the workers to be released")


class ScratchTest(ServerTestCase):
    def test_startup_creates_and_empties_the_scratch_directory(self) -> None:
        (self.scratch / "job-old").mkdir()
        (self.scratch / "job-old" / "x.m4a").write_bytes(b"x")
        (self.scratch / "stray").write_bytes(b"x")
        self.start()
        self.assertEqual(sorted(p.name for p in self.scratch.iterdir()), ["results"])

    def test_missing_parent_is_refused_not_created(self) -> None:
        with self.assertRaises(RuntimeError):
            prepare_scratch(self.temp / "missing" / "scratch")
        self.assertFalse((self.temp / "missing").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_scratch_directory_is_owner_only(self) -> None:
        os.chmod(self.scratch, 0o777)
        self.start()
        self.assertEqual(self.scratch.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.scratch / "results").stat().st_mode & 0o777, 0o700)

    def test_scratch_directory_owned_by_another_user_is_refused(self) -> None:
        uid = os.lstat(self.scratch).st_uid
        with mock.patch.object(server_module.os, "getuid", create=True, return_value=uid + 1):
            with self.assertRaisesRegex(RuntimeError, "owned by another user"):
                prepare_scratch(self.scratch)

    def test_a_file_at_the_scratch_path_is_refused(self) -> None:
        path = self.temp / "file"
        path.write_bytes(b"x")
        with self.assertRaises(RuntimeError):
            prepare_scratch(path)
        self.assertEqual(path.read_bytes(), b"x")

    def test_symlinked_scratch_directory_is_refused(self) -> None:
        target = self.temp / "elsewhere"
        target.mkdir()
        link = self.temp / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this account can't create symlinks")
        with self.assertRaisesRegex(RuntimeError, "not a plain directory"):
            prepare_scratch(link)

    def test_a_failed_start_closes_the_socket(self) -> None:
        with mock.patch.object(server_module, "prepare_scratch", side_effect=RuntimeError("no")), \
                mock.patch.object(FetchServer, "server_close", autospec=True,
                                  side_effect=FetchServer.server_close) as close:
            with self.assertRaises(RuntimeError):
                _TestServer(("127.0.0.1", 0), self.settings, self.runner)
        self.assertEqual(close.call_count, 1)

    def test_the_default_runner_is_the_real_yt_dlp_under_prlimit_on_linux(self) -> None:
        server = self.server_class(("127.0.0.1", 0), self.settings)
        try:
            runner = server.runner
            self.assertEqual(runner.command, (sys.executable, "-I", "-m", "yt_dlp"))  # type: ignore[attr-defined]
            expected = ("/usr/bin/prlimit",) if sys.platform.startswith("linux") else None
            self.assertEqual(runner.prlimit, expected)  # type: ignore[attr-defined]
            self.assertEqual(runner.timeout_seconds, self.settings.fetch_timeout_seconds)  # type: ignore[attr-defined]
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
