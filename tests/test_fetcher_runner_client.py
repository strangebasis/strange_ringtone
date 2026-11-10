"""The fetcher's runner client (strange_ringtone_fetch.runner_client) against a scripted runner on loopback.

S1 (docs/PLAN.md 6.1): the fetcher sends the runner only the validated video
ID and a token it mints itself, and treats everything the runner answers as
untrusted. ScriptedRunner below is a loopback server that answers each
request with the next scripted response, byte for byte: well-formed answers,
malformed ones, slow drips and connections held open. So every check the
client makes is met here by an answer that breaks exactly that rule while
the rest of it is valid, and a client that skipped the check would succeed
where the test expects a failure. Nothing here touches the network beyond
127.0.0.1 or resolves a name.
"""
from __future__ import annotations

import contextlib
import http
import itertools
import json
import os
import socket
import socketserver
import tempfile
import threading
import time
import unicodedata
import unittest
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import runner_client
from strange_ringtone_fetch.metadata import ERROR_CODES, Download, FetchError, Metadata, parse_info
from strange_ringtone_fetch.runner_client import JOB_RE, RunnerClient

MIB = 1024 * 1024
VIDEO_ID = "dQw4w9WgXcQ"
# How long the scripted runner keeps a held connection or a drip going at
# most, so a client that lacks a deadline fails a timing assertion instead of
# hanging the suite.
HOLD_SECONDS = 12.0
# Socket timeout of the scripted runner's own reads and writes.
HANDLER_TIMEOUT = 10.0
# Waited after a fetch before counting connections, so a retry the client
# might have started is seen.
SETTLE_SECONDS = 0.05
MAX_HEAD_BYTES = 1024 * 1024
# A title full of control and format characters, as a hostile runner (or
# YouTube through it) might send.
DIRTY_TITLE = "  A\x00\x07 ‮Title​\n\tline two﻿\x1b[31m  "
HEATMAP = [{"start_time": i * 20.0, "end_time": (i + 1) * 20.0, "value": i / 10} for i in range(10)]
INFO: dict[str, Any] = {"title": DIRTY_TITLE, "duration": 215.5, "heatmap": HEATMAP}
DONE: dict[str, Any] = {"status": "done", "info": INFO}
# Not a real MP4: the client checks size, type and length, never the codec
# (docs/PLAN.md 6.1 says why).
AUDIO = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 4
JSON_LIMIT = 256 * 1024


# --- the scripted runner ------------------------------------------------------------


@dataclass
class Request:
    method: str
    path: str
    version: str
    # In the order received, the names' case kept.
    headers: list[tuple[str, str]]
    body: bytes
    # The scripted runner's count of the connection this request came on.
    connection: int

    def header_values(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key.lower() == name.lower()]


# A script entry: the bytes to send before closing; an iterable of
# (delay, bytes) pairs sent one by one, where bytes None means "now hold the
# connection open"; or None to hold it open without answering at all.
ScriptEntry = bytes | Iterable[tuple[float, bytes | None]] | None
# What a request gets once the script has run out: the connection is closed
# at once. A client that retries meets this, and the request is recorded.
_EXHAUSTED = object()


def _read_request(sock: socket.socket, connection: int) -> Request:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            raise ValueError("closed before the end of the head")
        data += chunk
        if len(data) > MAX_HEAD_BYTES:
            raise ValueError("head too long")
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, path, version = lines[0].split(" ", 2)
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.append((name, value.strip()))
    lengths = [value for name, value in headers if name.lower() == "content-length"]
    length = int(lengths[0]) if lengths and lengths[0].isdigit() else 0
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return Request(method, path, version, headers, body[:length], connection)


class _ScriptedHandler(socketserver.BaseRequestHandler):
    server: ScriptedRunner

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(HANDLER_TIMEOUT)
        connection = self.server._connected()
        try:
            request = _read_request(sock, connection)
        except (OSError, ValueError):
            return
        entry = self.server._record(request)
        # A client that refuses an answer may close its end mid-answer.
        with contextlib.suppress(OSError):
            self.server._answer(sock, entry)


class ScriptedRunner(socketserver.ThreadingTCPServer):
    """A fake runner on 127.0.0.1: answers each request with the next script entry and records it."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, script: Iterable[ScriptEntry] = ()) -> None:
        self.script: list[Any] = list(script)
        self.requests: list[Request] = []
        self.connections = 0
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _ScriptedHandler)
        self.port = self.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.shutdown()
        self.server_close()
        self._thread.join(HANDLER_TIMEOUT)

    def _connected(self) -> int:
        with self._lock:
            self.connections += 1
            return self.connections

    def _record(self, request: Request) -> Any:
        with self._lock:
            self.requests.append(request)
            return self.script.pop(0) if self.script else _EXHAUSTED

    def _answer(self, sock: socket.socket, entry: Any) -> None:
        if entry is _EXHAUSTED:
            return
        if entry is None:
            self._hold(sock)
            return
        if isinstance(entry, bytes):
            sock.sendall(entry)
        else:
            for delay, chunk in entry:
                if self.stopping.wait(delay):
                    return
                if chunk is None:
                    self._hold(sock)
                    return
                sock.sendall(chunk)
        self._finish(sock)

    def _hold(self, sock: socket.socket) -> None:
        # Keep the connection open and silent until the client closes it, the
        # test ends, or HOLD_SECONDS pass.
        sock.settimeout(0.05)
        deadline = time.monotonic() + HOLD_SECONDS
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                if not sock.recv(65536):
                    return
            except TimeoutError:
                continue

    def _finish(self, sock: socket.socket) -> None:
        # A graceful close: FIN after the answer, then wait for the client's,
        # so Windows never resets the connection under unread answer bytes.
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(0.05)
        deadline = time.monotonic() + 2.0
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                if not sock.recv(65536):
                    return
            except TimeoutError:
                continue


# --- answers --------------------------------------------------------------------------


def http_answer(status: int, headers: list[tuple[str, str]], body: bytes, reason: str | None = None,
                version: str = "HTTP/1.0") -> bytes:
    if reason is None:
        try:
            reason = http.HTTPStatus(status).phrase
        except ValueError:
            reason = "Whatever"
    head = f"{version} {status} {reason}\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers)
    return (head + "\r\n").encode("latin-1") + body


def _headers(defaults: list[tuple[str, str]], overrides: dict[str, Any]) -> list[tuple[str, str]]:
    # Content_Type="x" replaces Content-Type, None drops it, a list repeats it;
    # a name not among the defaults is added.
    named = {key.replace("_", "-"): value for key, value in overrides.items()}
    headers = [(name, value) for name, value in defaults if name not in named]
    for name, value in named.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        headers.extend((name, str(item)) for item in values)
    return headers


def _defaults(content_type: str, body: bytes) -> list[tuple[str, str]]:
    # What the real runner sends (docs/PLAN.md 6.1, the runner's wire protocol).
    return [
        ("Server", "strange_ringtone_runner"),
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Connection", "close"),
    ]


def json_response(payload: Any, *, status: int = 200, body: bytes | None = None, reason: str | None = None,
                  **header_overrides: Any) -> bytes:
    data = json.dumps(payload).encode("ascii") if body is None else body
    return http_answer(status, _headers(_defaults("application/json", data), header_overrides), data, reason)


def audio_response(data: bytes, *, status: int = 200, body: bytes | None = None, reason: str | None = None,
                   **overrides: Any) -> bytes:
    """An audio answer; body, when given, is sent instead of data (whose length the header declares)."""
    sent = data if body is None else body
    return http_answer(status, _headers(_defaults("audio/mp4", data), overrides), sent, reason)


def raw(data: bytes) -> bytes:
    """Bytes sent exactly as written, for answers that are not HTTP at all."""
    return data


def drip(chunks: Iterable[bytes], delay: float, first_delay: float = 0.0, then_hold: bool = False
         ) -> Iterator[tuple[float, bytes | None]]:
    for index, chunk in enumerate(chunks):
        yield (first_delay if index == 0 else delay, chunk)
    if then_hold:
        yield (0.0, None)


def chunked(body: bytes) -> bytes:
    return b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n"


def padded(payload: Any, size: int) -> bytes:
    """payload as JSON, padded with trailing whitespace (still valid JSON) to exactly size bytes."""
    data = json.dumps(payload).encode("ascii")
    assert len(data) <= size
    return data + b" " * (size - len(data))


def head_of(answer: bytes) -> bytes:
    return answer.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"


def closed_port() -> int:
    """A loopback port nothing listens on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AssertionError(f"duplicate keys in {keys}")
    return dict(pairs)


# --- tests ------------------------------------------------------------------------------


class ClientTestCase(unittest.TestCase):
    """A fresh job directory and a scripted runner per test; a client with short deadlines."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        # The fetcher's own per-job directory (Jobs makes it with mkdtemp).
        self.job_dir = self.temp / "job-test"
        self.job_dir.mkdir()
        self.runner = self.scripted()

    def scripted(self, *script: ScriptEntry) -> ScriptedRunner:
        runner = ScriptedRunner(script)
        self.addCleanup(runner.stop)
        return runner

    def use(self, *script: ScriptEntry) -> ScriptedRunner:
        """A new scripted runner for the next case; the client() default from now on."""
        self.runner = self.scripted(*script)
        return self.runner

    def client(self, runner: ScriptedRunner | None = None, **overrides: Any) -> RunnerClient:
        values: dict[str, Any] = {
            "runner_url": (runner or self.runner).url,
            "max_bytes": MIB,
            "timeout_seconds": 2,
            "read_timeout": 0.5,
            "post_grace": 0.5,
            "audio_deadline": 2.0,
        }
        values.update(overrides)
        return RunnerClient(values.pop("runner_url"), values.pop("max_bytes"), values.pop("timeout_seconds"), **values)

    def assertFailed(self, client: RunnerClient, code: str = "failed", video_id: str = VIDEO_ID) -> FetchError:
        with self.assertRaises(FetchError) as raised:
            client.fetch(video_id, self.job_dir)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def assertJobDirEmpty(self) -> None:
        self.assertEqual(sorted(path.name for path in self.job_dir.iterdir()), [])

    def assertRequests(self, runner: ScriptedRunner, methods: list[str]) -> None:
        """Exactly these requests, each on a connection of its own: nothing more, so nothing retried."""
        time.sleep(SETTLE_SECONDS)
        self.assertEqual([request.method for request in runner.requests], methods)
        self.assertEqual(runner.connections, len(methods))

    def accept(self, *script: ScriptEntry, **client_overrides: Any) -> Download:
        """The control case: a run with this script succeeds; its file is removed for the next case."""
        runner = self.use(*script)
        download = self.client(**client_overrides).fetch(VIDEO_ID, self.job_dir)
        self.assertEqual(download.audio, self.job_dir / f"{VIDEO_ID}.m4a")
        self.assertRequests(runner, ["POST", "GET"])
        download.audio.unlink()
        return download


class ClientRequestTest(ClientTestCase):
    """What the fetcher sends: the validated ID and its own token, nothing else (S1)."""

    def test_the_post_carries_only_the_validated_id_and_a_fresh_token(self) -> None:
        # S1: the body is exactly {video_id, job}; the token is minted per fetch.
        runner = self.use(json_response(DONE), audio_response(AUDIO), json_response(DONE), audio_response(AUDIO))
        client = self.client()
        client.fetch(VIDEO_ID, self.job_dir).audio.unlink()
        self.assertEqual(len(runner.requests), 2)
        post = runner.requests[0]
        self.assertEqual((post.method, post.path), ("POST", "/jobs"))
        self.assertEqual(post.header_values("Content-Type"), ["application/json"])
        self.assertEqual(post.header_values("Content-Length"), [str(len(post.body))])
        sent = json.loads(post.body.decode("utf-8"), object_pairs_hook=no_duplicates)
        self.assertEqual(sorted(sent), ["job", "video_id"])
        self.assertEqual(sent["video_id"], VIDEO_ID)
        self.assertIsInstance(sent["job"], str)
        self.assertTrue(JOB_RE.fullmatch(sent["job"]), sent["job"])
        # A second fetch mints a new token: one per run, never reused.
        second = self.temp / "job-second"
        second.mkdir()
        client.fetch(VIDEO_ID, second)
        self.assertEqual(len(runner.requests), 4)
        token = json.loads(runner.requests[2].body.decode("utf-8"))["job"]
        self.assertTrue(JOB_RE.fullmatch(token), token)
        self.assertNotEqual(token, sent["job"])
        # The ID goes on the wire, so it is checked again here, before any
        # request: nothing reaches the runner for a value that is not an ID.
        for bad in ("", "dQw4w9WgXc", "dQw4w9WgXcQQ", "dQw4w9WgXcQ\n", "dQw4w9WgX/Q", "../../etc/p",
                    "dQw4w9WgX%2", "dQw4w9WgXc١", " dQw4w9WgXc"):
            with self.subTest(video_id=bad), self.assertRaises(ValueError):
                client.fetch(bad, self.job_dir)
        self.assertEqual(len(runner.requests), 4)
        self.assertEqual(runner.connections, 4)
        self.assertJobDirEmpty()

    def test_the_audio_get_uses_the_clients_token_and_a_fresh_connection(self) -> None:
        # S1: the GET's path is built from the client's own token. The info
        # here offers another token and a path; a client that took either
        # from the runner's answer would ask for the wrong path.
        other = "f" * 32
        info = dict(INFO, job=other, audio=f"/jobs/{other}/audio", url="/elsewhere")
        runner = self.use(json_response({"status": "done", "info": info}), audio_response(AUDIO))
        self.client().fetch(VIDEO_ID, self.job_dir)
        self.assertRequests(runner, ["POST", "GET"])
        token = json.loads(runner.requests[0].body.decode("utf-8"))["job"]
        get = runner.requests[1]
        self.assertEqual(get.path, f"/jobs/{token}/audio")
        self.assertEqual(get.body, b"")
        self.assertEqual(get.header_values("Content-Length"), [])
        # Two connections, one request each.
        self.assertEqual([request.connection for request in runner.requests], [1, 2])

    def test_no_proxy_variable_is_consulted(self) -> None:
        # S1: the fetcher reaches the runner directly over `jobs`. Every proxy
        # variable points at a decoy; a client built on urllib would go there.
        decoy = self.scripted(json_response(DONE), audio_response(AUDIO))
        runner = self.use(json_response(DONE), audio_response(AUDIO))
        names = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
        with mock.patch.dict(os.environ, dict.fromkeys(names, decoy.url)):
            for name in ("NO_PROXY", "no_proxy"):
                os.environ.pop(name, None)
            download = self.client().fetch(VIDEO_ID, self.job_dir)
        self.assertEqual(download.audio.read_bytes(), AUDIO)
        self.assertRequests(runner, ["POST", "GET"])
        self.assertEqual(decoy.requests, [])
        self.assertEqual(decoy.connections, 0)

    def test_requests_carry_connection_close_and_no_origin(self) -> None:
        # S1: no browser-facing header (Origin, cookies, forwarded addresses)
        # and nothing beyond what http.client adds itself.
        runner = self.use(json_response(DONE), audio_response(AUDIO))
        self.client().fetch(VIDEO_ID, self.job_dir)
        self.assertRequests(runner, ["POST", "GET"])
        post, get = runner.requests
        allowed = {
            "POST": {"host", "accept-encoding", "content-type", "content-length", "connection"},
            "GET": {"host", "accept-encoding", "connection"},
        }
        for request in (post, get):
            with self.subTest(method=request.method):
                names = [name.lower() for name, _ in request.headers]
                self.assertLessEqual(set(names), allowed[request.method], names)
                self.assertEqual(len(names), len(set(names)), names)
                self.assertEqual(request.header_values("Connection"), ["close"])
                for name in ("Origin", "X-Forwarded-For", "Forwarded", "Cookie", "Authorization",
                             "Proxy-Authorization", "Referer"):
                    self.assertEqual(request.header_values(name), [], name)
                self.assertEqual(request.header_values("Host"), [f"127.0.0.1:{runner.port}"])

    def test_construction_touches_no_socket(self) -> None:
        # S1: the fetcher starts without a runner, so building the client
        # resolves nothing and connects nowhere.
        refuse = AssertionError("touched the network")
        with (
            mock.patch.object(socket, "getaddrinfo", side_effect=refuse),
            mock.patch.object(socket, "gethostbyname", side_effect=refuse),
            mock.patch.object(socket, "create_connection", side_effect=refuse),
            mock.patch.object(socket.socket, "connect", side_effect=refuse),
        ):
            client = RunnerClient("http://runner.example.invalid:8081", MIB, 45)
            default_port = RunnerClient("http://runner.example.invalid", MIB, 45)
        self.assertEqual(client.runner_url, "http://runner.example.invalid:8081")
        self.assertEqual((client.host, client.port), ("runner.example.invalid", 8081))
        self.assertEqual((client.max_bytes, client.timeout_seconds), (MIB, 45))
        self.assertEqual((default_port.host, default_port.port), ("runner.example.invalid", 80))
        self.assertEqual(self.runner.connections, 0)


class ClientHappyPathTest(ClientTestCase):
    """A well-behaved runner's answers become a Download in the job directory (S1 acceptance)."""

    def test_done_and_audio_yield_a_download_in_the_job_directory(self) -> None:
        # S1: the audio lands under a name the client chose, in the fetcher's
        # job directory, and the metadata passes the whitelist again.
        runner = self.use(json_response(DONE), audio_response(AUDIO))
        download = self.client().fetch(VIDEO_ID, self.job_dir)
        self.assertRequests(runner, ["POST", "GET"])
        self.assertIsInstance(download, Download)
        self.assertEqual(download.audio, self.job_dir / f"{VIDEO_ID}.m4a")
        self.assertEqual(sorted(path.name for path in self.job_dir.iterdir()), [f"{VIDEO_ID}.m4a"])
        self.assertEqual(download.audio.read_bytes(), AUDIO)
        if os.name == "posix":
            self.assertEqual(download.audio.stat().st_mode & 0o777, 0o600)
        self.assertEqual(download.metadata, parse_info(INFO))
        title = download.metadata.title
        assert title is not None
        self.assertFalse([ch for ch in title if unicodedata.category(ch) in ("Cc", "Cf")], title)
        self.assertNotEqual(title, DIRTY_TITLE)
        self.assertEqual(download.metadata.duration, 215.5)
        heatmap = download.metadata.heatmap
        assert heatmap is not None
        self.assertEqual(len(heatmap), len(HEATMAP))
        for point in heatmap:
            self.assertEqual(sorted(point), ["end", "start", "value"])

    def test_failed_codes_pass_through(self) -> None:
        # S1 acceptance: a known code is the job's error; the GET is never sent.
        for code in ERROR_CODES:
            with self.subTest(code=code):
                runner = self.use(json_response({"status": "failed", "error": code}), audio_response(AUDIO))
                self.assertFailed(self.client(), code)
                self.assertRequests(runner, ["POST"])
                self.assertJobDirEmpty()

    def test_oversized_metadata_values_are_whitelisted_not_trusted(self) -> None:
        # S1: the fetcher runs parse_info on what the runner says; a value the
        # whitelist bounds or refuses never reaches the page as sent.
        extra = {"uploader": "Someone", "description": "x" * 1000, "webpage_url": "https://example.invalid/",
                 "formats": [{"url": "https://example.invalid/a"}], "id": VIDEO_ID}
        many_points = [{"start_time": i * 1.0, "end_time": i + 1.0, "value": 0.5} for i in range(201)]
        cases = {
            "long title, string duration": (
                dict(extra, title="x" * (100 * 1024), duration="215", heatmap=HEATMAP),
                Metadata(title="x" * 200, duration=None, heatmap=None),
            ),
            "201 heatmap points": (
                dict(extra, title="T", duration=215.5, heatmap=many_points),
                Metadata(title="T", duration=215.5, heatmap=None),
            ),
            "only unknown keys": (extra, Metadata(title=None, duration=None, heatmap=None)),
        }
        for label, (info, expected) in cases.items():
            with self.subTest(label):
                answer = json_response({"status": "done", "info": info})
                self.assertLessEqual(len(answer.split(b"\r\n\r\n", 1)[1]), JSON_LIMIT)
                runner = self.use(answer, audio_response(AUDIO))
                download = self.client().fetch(VIDEO_ID, self.job_dir)
                self.assertEqual(download.metadata, expected)
                self.assertEqual(download.metadata, parse_info(info))
                self.assertRequests(runner, ["POST", "GET"])
                download.audio.unlink()

    def test_bytes_past_the_declared_length_fail_the_fetch_even_in_one_write(self) -> None:
        # S1: the runner writes exactly the length it declares, then closes.
        # Bytes past it are no answer of the runner's, also when they come in
        # the same write as the body, so that a buffered reader already holds
        # them when the declared length is reached: the fetch fails and
        # leaves nothing behind, for either exchange.
        data = bytes(range(10))
        # Control: exactly the declared length, then the close.
        runner = self.use(json_response(DONE), audio_response(data))
        download = self.client().fetch(VIDEO_ID, self.job_dir)
        self.assertEqual(download.audio.read_bytes(), data)
        self.assertRequests(runner, ["POST", "GET"])
        download.audio.unlink()
        # Past the JSON answer: no GET is ever sent.
        runner = self.use(raw(json_response(DONE) + b"Z" * 10), audio_response(data))
        self.assertFailed(self.client())
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()
        # Past the audio: the complete file the client wrote goes again.
        runner = self.use(json_response(DONE), audio_response(data, body=data + b"Z" * 10))
        self.assertFailed(self.client())
        self.assertRequests(runner, ["POST", "GET"])
        self.assertJobDirEmpty()


class ClientRefusalTest(ClientTestCase):
    """Every answer that breaks a rule fails the job closed: no file, no GET after a bad POST, no retry (S1).

    Each refused answer is valid in every other respect, and each test starts
    with an accepted control, so neither a client that skips the check nor
    one that refuses everything passes.
    """

    def refuse_post(self, label: str, answer: ScriptEntry, **client_overrides: Any) -> None:
        # A valid audio answer comes next: a client that went on to the GET,
        # or retried, would meet it.
        with self.subTest(label):
            runner = self.use(answer, audio_response(AUDIO))
            self.assertFailed(self.client(**client_overrides))
            self.assertRequests(runner, ["POST"])
            self.assertJobDirEmpty()

    def refuse_audio(self, label: str, answer: ScriptEntry, **client_overrides: Any) -> None:
        # A valid audio answer comes next: a client that retried the GET would meet it.
        with self.subTest(label):
            runner = self.use(json_response(DONE), answer, audio_response(AUDIO))
            self.assertFailed(self.client(**client_overrides))
            self.assertRequests(runner, ["POST", "GET"])
            self.assertJobDirEmpty()

    def post_cases(self) -> list[tuple[str, ScriptEntry]]:
        body = json.dumps(DONE).encode("ascii")
        elsewhere = self.scripted()
        cases: list[tuple[str, ScriptEntry]] = [
            (f"status {status}", json_response(DONE, status=status))
            for status in (201, 400, 404, 500)
        ]
        cases += [
            ("status 204", json_response(DONE, status=204)),
            ("status 302", json_response(DONE, status=302, Location=f"{elsewhere.url}/jobs")),
            ("status 503 busy", json_response({"error": "busy"}, status=503, Retry_After="5")),
            ("status 503 with a done body", json_response(DONE, status=503, Retry_After="5")),
            ("Content-Type text/plain", json_response(DONE, Content_Type="text/plain")),
            ("Content-Type with a charset", json_response(DONE, Content_Type="application/json; charset=utf-8")),
            ("Content-Type upper case", json_response(DONE, Content_Type="Application/JSON")),
            ("no Content-Type", json_response(DONE, Content_Type=None)),
            ("two Content-Types", json_response(DONE, Content_Type=["application/json", "application/json"])),
            ("chunked", json_response(DONE, body=chunked(body), Content_Length=None, Transfer_Encoding="chunked")),
            # http.client would decode the chunks to exactly the declared length.
            ("chunked and a length", json_response(DONE, body=chunked(body), Content_Length=str(len(body)),
                                                   Transfer_Encoding="chunked")),
            ("no Content-Length", json_response(DONE, Content_Length=None)),
            ("two Content-Lengths", json_response(DONE, Content_Length=[str(len(body)), str(len(body))])),
            ("Content-Length 0", json_response(DONE, body=b"", Content_Length="0")),
            ("Content-Length +n", json_response(DONE, Content_Length=f"+{len(body)}")),
            ("Content-Length with a space", json_response(DONE, Content_Length=f"{len(body)} ")),
            ("Content-Length over the limit", json_response(DONE, body=padded(DONE, JSON_LIMIT + 1))),
            ("HTTP/0.9 garbage", raw(b"garbage that is no status line\r\n\r\n" + body)),
            ("no answer at all", raw(b"")),
            ("a 70000-byte header line", json_response(DONE, X_Long="a" * 70000)),
            ("101 headers", json_response(DONE, **{f"X_Extra_{i}": "v" for i in range(101)})),
        ]
        return cases

    def test_post_responses_that_are_not_a_200_json_object_are_failed(self) -> None:
        # S1: status 200, exactly one Content-Type of application/json, exactly
        # one Content-Length of 1..JSON_MAX_BYTES ASCII digits, no
        # Transfer-Encoding; http.client's own limits become "failed" too.
        self.accept(json_response(DONE), audio_response(AUDIO))
        self.accept(json_response(DONE, body=padded(DONE, JSON_LIMIT)), audio_response(AUDIO))
        for label, answer in self.post_cases():
            self.refuse_post(label, answer)

    def json_cases(self) -> list[tuple[str, ScriptEntry]]:
        body = json.dumps(DONE).encode("ascii")
        array = b"[" + b",".join([b"0"] * 1000) + b"]"
        return [
            ("shorter than declared, then closed",
             json_response(DONE, Content_Length=str(len(body) + 10))),
            ("invalid JSON", json_response(None, body=body[:-1])),
            ("not UTF-8", json_response(None, body=b'{"status": "done", "info": {"title": "\xff\xfe"}}')),
            ("100000 nested brackets", json_response(None, body=b"[" * 100000)),
            ("a JSON array of exactly the limit", json_response(None, body=array + b" " * (JSON_LIMIT - len(array)))),
            ("a JSON string", json_response("done")),
            ("JSON null", json_response(None)),
            ("a number", json_response(200)),
        ]

    def test_malformed_or_oversized_json_is_failed(self) -> None:
        # S1: the body is JSON of at most JSON_MAX_BYTES that is an object.
        self.accept(json_response(DONE), audio_response(AUDIO))
        for label, answer in self.json_cases():
            self.refuse_post(label, answer)

    def shape_cases(self) -> list[tuple[str, ScriptEntry]]:
        info = json.dumps(INFO)
        return [
            ("no status", json_response({"info": INFO})),
            ("status running", json_response({"status": "running", "info": INFO})),
            ("status DONE", json_response({"status": "DONE", "info": INFO})),
            ("status not a string", json_response({"status": 1, "info": INFO})),
            ("done without info", json_response({"status": "done"})),
            ("done with info a list", json_response({"status": "done", "info": [INFO]})),
            ("done with info a string", json_response({"status": "done", "info": "x"})),
            ("done with an extra key", json_response({"status": "done", "info": INFO, "size": 10})),
            ("done with an error too", json_response({"status": "done", "info": INFO, "error": "blocked"})),
            ("failed with an unknown code", json_response({"status": "failed", "error": "nope"})),
            ("failed with a number", json_response({"status": "failed", "error": 7})),
            ("failed with no error", json_response({"status": "failed"})),
            ("failed with a code in another case", json_response({"status": "failed", "error": "BLOCKED"})),
            # A parser that keeps the last of a repeated key would see a done job.
            ("duplicate status keys", json_response(
                None, body=f'{{"status": "failed", "status": "done", "info": {info}}}'.encode("ascii"))),
            ("duplicate info keys", json_response(
                None, body=f'{{"status": "done", "info": [], "info": {info}}}'.encode("ascii"))),
        ]

    def test_wrong_shapes_and_unknown_codes_are_failed(self) -> None:
        # S1: exactly {status, info} with info an object, or {status, error}
        # with a known code; anything else is "failed", never another code.
        self.accept(json_response(DONE), audio_response(AUDIO))
        for label, answer in self.shape_cases():
            self.refuse_post(label, answer)

    def audio_cases(self) -> list[tuple[str, ScriptEntry]]:
        elsewhere = self.scripted()
        return [
            ("status 404", audio_response(AUDIO, status=404)),
            ("status 500", audio_response(AUDIO, status=500)),
            ("status 206", audio_response(AUDIO, status=206)),
            ("status 302", audio_response(AUDIO, status=302, Location=f"{elsewhere.url}/audio")),
            ("Content-Type audio/mpeg", audio_response(AUDIO, Content_Type="audio/mpeg")),
            ("Content-Type video/mp4", audio_response(AUDIO, Content_Type="video/mp4")),
            ("Content-Type with a parameter", audio_response(AUDIO, Content_Type="audio/mp4; codecs=mp4a")),
            ("no Content-Type", audio_response(AUDIO, Content_Type=None)),
            ("two Content-Types", audio_response(AUDIO, Content_Type=["audio/mp4", "audio/mp4"])),
            ("chunked", audio_response(AUDIO, body=chunked(AUDIO), Content_Length=None,
                                       Transfer_Encoding="chunked")),
            # The declared length is the decoded one, so only the
            # Transfer-Encoding check refuses it.
            ("chunked and a length", audio_response(AUDIO, body=chunked(AUDIO), Transfer_Encoding="chunked")),
            ("no Content-Length", audio_response(AUDIO, Content_Length=None)),
            ("two Content-Lengths", audio_response(AUDIO, Content_Length=[str(len(AUDIO))] * 2)),
            ("Content-Length 0", audio_response(b"")),
            ("Content-Length over max_bytes", audio_response(b"\0" * (MIB + 1))),
            ("Content-Length +n", audio_response(AUDIO[:10], Content_Length="+10")),
            ("Content-Length negative", audio_response(AUDIO[:10], Content_Length="-10")),
            ("HTTP/0.9 garbage", raw(b"garbage\r\n\r\n" + AUDIO)),
        ]

    def test_audio_responses_with_bad_status_type_or_length_are_failed(self) -> None:
        # S1: status 200, exactly one Content-Type of audio/mp4, exactly one
        # Content-Length of 1..max_bytes, no Transfer-Encoding.
        self.accept(json_response(DONE), audio_response(AUDIO))
        self.accept(json_response(DONE), audio_response(b"\0" * MIB))
        for label, answer in self.audio_cases():
            self.refuse_audio(label, answer)

    def test_audio_bodies_that_stop_short_drip_or_never_end_are_failed(self) -> None:
        # S1: exactly Content-Length bytes within the per-recv and the total
        # deadline; the partial file is removed every time.
        data = b"\1" * 1000
        # Acceptance: slow but steady, within both deadlines.
        slow = audio_response(data)
        pieces = [head_of(slow)] + [data[i:i + 100] for i in range(0, 1000, 100)]
        self.accept(json_response(DONE), drip(pieces, 0.1))
        with self.subTest("999 of 1000 bytes, then closed"):
            self.refuse_audio("short", audio_response(data, body=data[:999]))
        # Silent after 100 bytes: only the per-recv timeout (0.5 s) can end
        # it, since the total deadline here is long.
        silent = drip([head_of(slow) + data[:100]], 0.0, then_hold=True)
        started = time.monotonic()
        self.refuse_audio("100 bytes, then silence", silent, audio_deadline=30.0)
        self.assertLess(time.monotonic() - started, 5.0)
        # One byte every 0.2 s never trips the per-recv timeout; only the
        # total deadline (2 s) can end it.
        endless = audio_response(b"\1" * MIB)
        trickle = drip(itertools.chain([head_of(endless)], itertools.repeat(b"\1", int(HOLD_SECONDS / 0.2))), 0.2)
        started = time.monotonic()
        self.refuse_audio("a drip that never ends", trickle)
        self.assertLess(time.monotonic() - started, 2.0 + 3.0)
        self.assertEqual(list(self.temp.rglob("*.m4a")), [])

    def test_endless_interim_responses_and_endless_status_lines_are_cut(self) -> None:
        # S1: the POST's whole exchange has a hard deadline (timeout_seconds +
        # post_grace = 2.5 s here); http.client loops on 100 Continue and reads
        # a status line with only the socket timeout to bound it.
        client = self.client()
        limit = 2.5 + 1.0
        continues = drip(itertools.repeat(b"HTTP/1.1 100 Continue\r\n\r\n", int(HOLD_SECONDS / 0.1)), 0.1)
        started = time.monotonic()
        self.refuse_post("100 Continue forever", continues)
        self.assertLess(time.monotonic() - started, limit)
        status_line = drip(itertools.chain([b"HTTP/1.0 200 "], itertools.repeat(b"a", int(HOLD_SECONDS / 0.1))), 0.1)
        started = time.monotonic()
        self.refuse_post("a status line that never ends", status_line)
        self.assertLess(time.monotonic() - started, limit)
        header_line = drip(itertools.chain([b"HTTP/1.0 200 OK\r\nX-Slow: "],
                                           itertools.repeat(b"a", int(HOLD_SECONDS / 0.1))), 0.1)
        started = time.monotonic()
        self.refuse_post("a header line that never ends", header_line)
        self.assertLess(time.monotonic() - started, limit)
        started = time.monotonic()
        self.refuse_post("no answer, connection held", None)
        self.assertLess(time.monotonic() - started, limit)
        self.assertEqual(client.post_deadline, 2.5)

    def test_redirects_are_not_followed(self) -> None:
        # S1: a 3xx is just "not 200"; the place it names is never asked.
        self.accept(json_response(DONE), audio_response(AUDIO))
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status, request="POST"):
                elsewhere = self.scripted(json_response(DONE), audio_response(AUDIO))
                runner = self.use(json_response(DONE, status=status, Location=f"{elsewhere.url}/jobs"))
                self.assertFailed(self.client())
                self.assertRequests(runner, ["POST"])
                self.assertEqual(elsewhere.requests, [])
                self.assertEqual(elsewhere.connections, 0)
                self.assertJobDirEmpty()
            with self.subTest(status=status, request="GET"):
                elsewhere = self.scripted(audio_response(AUDIO))
                runner = self.use(json_response(DONE),
                                  audio_response(AUDIO, status=status, Location=f"{elsewhere.url}/audio"))
                self.assertFailed(self.client())
                self.assertRequests(runner, ["POST", "GET"])
                self.assertEqual(elsewhere.connections, 0)
                self.assertJobDirEmpty()

    def test_nothing_is_left_in_the_job_directory_after_any_refusal(self) -> None:
        # S1: across every kind of refusal, the job directory is as the
        # fetcher made it (empty) and no audio exists anywhere under the temp root.
        self.accept(json_response(DONE), audio_response(AUDIO))
        data = b"\1" * 1000
        cases: list[tuple[str, list[ScriptEntry]]] = [
            (label, [answer, audio_response(AUDIO)])
            for label, answer in self.post_cases() + self.json_cases() + self.shape_cases()
        ]
        cases += [(label, [json_response(DONE), answer]) for label, answer in self.audio_cases()]
        cases += [
            ("audio cut short", [json_response(DONE), audio_response(data, body=data[:500])]),
            ("audio then silence", [json_response(DONE),
                                    drip([head_of(audio_response(data)) + data[:500]], 0.0, then_hold=True)]),
        ]
        for label, script in cases:
            with self.subTest(label):
                self.use(*script)
                with self.assertRaises(FetchError):
                    self.client().fetch(VIDEO_ID, self.job_dir)
                self.assertEqual(list(self.job_dir.iterdir()), [])
                self.assertEqual(list(self.temp.rglob("*.m4a")), [])


class ClientCleanupTest(ClientTestCase):
    """The client writes only a file it created itself, and removes only that (S1)."""

    def test_the_audio_file_is_created_exclusively(self) -> None:
        # S1: O_CREAT|O_EXCL|O_NOFOLLOW. Anything already at the name is
        # wrong (the job directory is fresh), so the fetch fails, and what was
        # there is neither written through nor removed: the client didn't make it.
        self.accept(json_response(DONE), audio_response(AUDIO))
        target = self.job_dir / f"{VIDEO_ID}.m4a"
        with self.subTest("a file already there"):
            target.write_bytes(b"theirs")
            runner = self.use(json_response(DONE), audio_response(AUDIO))
            self.assertFailed(self.client())
            self.assertEqual(target.read_bytes(), b"theirs")
            self.assertRequests(runner, ["POST", "GET"])
            target.unlink()
        with self.subTest("a symlink to a file outside the job directory"):
            outside = self.temp / "outside.bin"
            outside.write_bytes(b"outside")
            try:
                os.symlink(outside, target)
            except (OSError, NotImplementedError):
                self.skipTest("this account can't create symlinks")
            runner = self.use(json_response(DONE), audio_response(AUDIO))
            self.assertFailed(self.client())
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertTrue(target.is_symlink())
            self.assertRequests(runner, ["POST", "GET"])

    def test_cancel_closes_an_open_connection(self) -> None:
        # S1: Jobs.stop() calls cancel(); a fetch blocked on the runner then
        # fails at once, whatever its deadlines (long here, so only cancel ends it).
        runner = self.use(None)
        client = self.client(timeout_seconds=60, read_timeout=30.0, post_grace=15.0, audio_deadline=60.0)
        outcome: list[BaseException | Download] = []

        def run() -> None:
            try:
                outcome.append(client.fetch(VIDEO_ID, self.job_dir))
            except BaseException as exc:  # noqa: BLE001  (reported to the test thread)
                outcome.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + HANDLER_TIMEOUT
        while not runner.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(runner.requests), 1, "the POST never arrived")
        started = time.monotonic()
        client.cancel()
        thread.join(5.0)
        self.assertFalse(thread.is_alive(), "cancel() did not end the fetch")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], FetchError)
        assert isinstance(outcome[0], FetchError)
        self.assertEqual(outcome[0].code, "failed")
        self.assertRequests(runner, ["POST"])
        self.assertJobDirEmpty()

    def test_a_closed_runner_port_is_failed_fast(self) -> None:
        # S1: no runner listening is "failed", not an exception or a hang.
        client = self.client(runner_url=f"http://127.0.0.1:{closed_port()}")
        started = time.monotonic()
        self.assertFailed(client)
        self.assertLess(time.monotonic() - started, 8.0)
        self.assertJobDirEmpty()


class ClientLimitsTest(unittest.TestCase):
    """The client's limits, as docs/PLAN.md 6.1 states them (S1)."""

    def test_limits_have_their_values(self) -> None:
        # S1: per-recv 30 s, connect 10 s, the POST's grace 15 s on top of
        # FETCH_TIMEOUT_SECONDS, 256 KiB of JSON, 30 s plus 1 s per MiB for the audio.
        self.assertEqual(runner_client.CONNECT_TIMEOUT_SECONDS, 10)
        self.assertEqual(runner_client.READ_TIMEOUT_SECONDS, 30)
        self.assertEqual(runner_client.POST_GRACE_SECONDS, 15)
        self.assertEqual(runner_client.JSON_MAX_BYTES, 262144)
        self.assertEqual(runner_client.AUDIO_DEADLINE_BASE_SECONDS, 30)
        client = RunnerClient("http://fetcher-runner:8080", 40 * MIB, 120)
        self.assertEqual(client.audio_deadline, 70)
        self.assertEqual(client.post_deadline, 135)

    def test_post_deadline_follows_fetch_timeout_seconds(self) -> None:
        # S1: the runner's own timeout plus the grace, so the client never
        # gives up on a run the runner would still finish, and never waits much longer.
        for timeout in (1, 45, 600):
            with self.subTest(timeout=timeout):
                self.assertEqual(RunnerClient("http://fetcher-runner:8080", MIB, timeout).post_deadline, timeout + 15)
                client = RunnerClient("http://fetcher-runner:8080", MIB, timeout, post_grace=0.5)
                self.assertEqual(client.post_deadline, timeout + 0.5)
        self.assertEqual(RunnerClient("http://fetcher-runner:8080", MIB, 5).audio_deadline, 31)
        self.assertEqual(RunnerClient("http://fetcher-runner:8080", 512 * MIB, 5).audio_deadline, 542)
        self.assertEqual(RunnerClient("http://fetcher-runner:8080", MIB, 5, audio_deadline=2.0).audio_deadline, 2.0)

    def test_bad_arguments_are_refused(self) -> None:
        # S1: settings already checked these; a bad value here is a bug and
        # refuses construction instead of meeting the runner.
        good = RunnerClient("http://fetcher-runner:8080", 1, 1)
        self.assertEqual((good.host, good.port, good.max_bytes, good.timeout_seconds), ("fetcher-runner", 8080, 1, 1))
        for url, max_bytes, timeout in (
            ("http://fetcher-runner:8080", 0, 120),
            ("http://fetcher-runner:8080", -1, 120),
            ("http://fetcher-runner:8080", MIB, 0),
            ("http://fetcher-runner:8080", MIB, -5),
            ("https://fetcher-runner:8080", MIB, 120),
            ("ftp://fetcher-runner:8080", MIB, 120),
            ("fetcher-runner:8080", MIB, 120),
            ("http://", MIB, 120),
            ("http://:8080", MIB, 120),
            ("http://fetcher-runner:99999", MIB, 120),
        ):
            with self.subTest(url=url, max_bytes=max_bytes, timeout=timeout), self.assertRaises(ValueError):
                RunnerClient(url, max_bytes, timeout)


if __name__ == "__main__":
    unittest.main()
