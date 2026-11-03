from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import quote

import helpers  # noqa: F401  (puts core/ on sys.path)
from corpus.build import (
    CORPUS_MAX_CLIP_SECONDS,
    corpus_cases,
    cut_off_body,
    oversized_body,
    silent_bytes,
    valid_wav,
)
from strange_ringtone import __main__ as entry
from strange_ringtone import config, static
from strange_ringtone import render as render_module
from strange_ringtone import server as server_module
from strange_ringtone.config import Settings
from strange_ringtone.http_guard import GuardError
from strange_ringtone.names import content_disposition
from strange_ringtone.render import Renderer
from strange_ringtone.results import ResultStore
from strange_ringtone.server import CHUNK_BYTES, Handler, RingtoneServer

# A stand-in for ffmpeg (tests/stubs/ffmpeg_stub.py), so no server in these
# tests ever runs, or needs, the real one.
STUB = helpers.ROOT / "tests" / "stubs" / "ffmpeg_stub.py"
STUB_COMMAND = (sys.executable, "-I", str(STUB))
STUB_MODE_VAR = "STRANGE_RINGTONE_FFMPEG_STUB"
STUB_LOG_VAR = "STRANGE_RINGTONE_FFMPEG_STUB_LOG"
STUB_MAGIC = b"STUB-IPOD\n"

ORIGIN = "https://example.internal"
DEADLINE = 5.0

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


def exchange(port: int, data: bytes, half_close: bool = False, tolerate_reset: bool = False) -> Response | None:
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
        except (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected):
            if tolerate_reset:
                return None
            raise
        finally:
            response.close()
    return Response(response.status, response.headers, body)


def build_request(method: str, path: str, headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: example.internal"]
    lines += [f"{name}: {value}" for name, value in headers or []]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def wait_for(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.01)


class _TestServer(RingtoneServer):
    # Join the handler threads on close, so no handler is still writing to the
    # scratch directory when the temporary directory is removed.
    block_on_close = True


class _CappedServer(_TestServer):
    MAX_WORKERS = 2


class ServerTestCase(unittest.TestCase):
    server_class: type[RingtoneServer] = _TestServer

    def setUp(self) -> None:
        self.log = LogCapture()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        # A subdirectory that doesn't exist yet: the server must create it.
        self.scratch = self.temp / "scratch"
        self.start()

    # --- fixtures ---------------------------------------------------------------

    def start(
        self,
        render_hook: Callable[[Path, Handler], None] | None = None,
        renderer: Renderer | None = None,
        clock: Callable[[], float] | None = None,
        **overrides: Any,
    ) -> None:
        values: dict[str, Any] = {
            "public_origin": ORIGIN,
            # One second keeps MAX_BODY_BYTES (196096) small enough to send whole.
            "max_clip_seconds": 1,
            # High, so only the rate-limit tests ever meet the limit.
            "rate_limit_renders_per_minute": 1000,
            "scratch_dir": self.scratch,
        }
        values.update(overrides)
        self.settings = Settings(**values)
        if renderer is None:
            renderer = Renderer(STUB_COMMAND, self.settings.render_timeout_seconds)
        results = None
        if clock is not None:
            results = ResultStore(self.scratch / "results", self.settings.result_ttl_seconds,
                                  self.settings.max_results, clock=clock)
        self.server = self.server_class(("127.0.0.1", 0), self.settings, render_hook, renderer, results)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, self.server, thread)
        self.port = self.server.server_address[1]

    @staticmethod
    def _stop(server: RingtoneServer, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(DEADLINE)

    def request(self, method: str, path: str, headers: list[tuple[str, str]] | None = None,
                body: bytes = b"", **options: Any) -> Response:
        response = exchange(self.port, build_request(method, path, headers, body), **options)
        assert response is not None
        return response

    def render_headers(self, length: int) -> list[tuple[str, str]]:
        return [("Origin", ORIGIN), ("Content-Type", "audio/wav"), ("Content-Length", str(length))]

    def post_render(self, headers: list[tuple[str, str]], body: bytes = b"", path: str = "/api/render") -> Response:
        return self.request("POST", path, headers, body)

    def wait_for_log(self, prefix: str, count: int = 1) -> list[str]:
        def found() -> bool:
            return len([line for line in self.log.lines() if line.startswith(prefix)]) >= count

        wait_for(found, f"a log line starting {prefix!r}")
        return [line for line in self.log.lines() if line.startswith(prefix)]

    def log_has(self, prefix: str) -> bool:
        return any(line.startswith(prefix) for line in self.log.lines())

    # --- assertions ---------------------------------------------------------------

    def scratch_contents(self) -> list[str]:
        """Every path under scratch, relative and with '/', except the results directory itself."""
        names = [str(p.relative_to(self.scratch)).replace(os.sep, "/") for p in self.scratch.rglob("*")]
        return sorted(name for name in names if name != "results")

    def assertScratchEmpty(self) -> None:
        # The results directory may exist, but empty: the store keeps it.
        # Cleanup runs after the response is written, so give it a moment.
        def empty() -> bool:
            return not self.scratch_contents()

        try:
            wait_for(empty, "an empty scratch directory")
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
        self.assertFalse([name for name in response.headers if name.lower().startswith("access-control-")])

    def assertBadWav(self, response: Response, detail: str, status: int = 400, code: str = "bad_wav") -> None:
        self.assertEqual(response.status, status, response.body)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.json(), {"error": code, "detail": detail})
        self.assertSecurityHeaders(response, hsts=self.settings.public_origin.startswith("https://"))

    def assertJsonError(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status, status, response.body)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.json(), {"error": code})
        self.assertSecurityHeaders(response, hsts=self.settings.public_origin.startswith("https://"))


class HeadersTest(ServerTestCase):
    def test_every_kind_of_response_carries_the_header_set(self) -> None:
        cases: list[tuple[str, bytes, bool]] = [
            ("index", build_request("GET", "/"), False),
            ("static", build_request("GET", "/static/dev/app.js"), False),
            ("healthz", build_request("GET", "/healthz"), False),
            ("404", build_request("GET", "/nope"), False),
            ("405", build_request("POST", "/"), False),
            ("403", build_request("POST", "/api/render", [("Content-Length", "0")]), False),
            ("400 render", build_request("POST", "/api/render", self.render_headers(0)), False),
            ("400 bad request line", b"GET / HTTP/1.0 extra\r\n", True),
            ("400 HTTP/0.9", b"GET /\r\n", True),
            ("405 unknown verb", b"BREW / HTTP/1.0\r\n\r\n", False),
            # Exactly 65537 bytes: the stdlib reads that much, finds no line end,
            # and answers 414 with nothing left unread on the socket.
            ("414", b"GET /" + b"a" * (65537 - 5), False),
        ]
        for label, data, half_close in cases:
            with self.subTest(label):
                response = exchange(self.port, data, half_close=half_close)
                assert response is not None
                self.assertSecurityHeaders(response)

    def test_http_origin_gets_no_hsts(self) -> None:
        self.start(public_origin="http://example.internal")
        for path in ("/", "/healthz", "/nope"):
            with self.subTest(path):
                self.assertSecurityHeaders(self.request("GET", path), hsts=False)

    def test_stdlib_errors_are_json(self) -> None:
        cases = [
            ("bad request line", b"GET / HTTP/1.0 extra\r\n", 400),
            ("HTTP/0.9", b"GET /\r\n", 400),
            ("request line too long", b"GET /" + b"a" * (65537 - 5), 414),
        ]
        for label, data, status in cases:
            with self.subTest(label):
                response = exchange(self.port, data, half_close=True)
                assert response is not None
                self.assertJsonError(response, status, f"http_{status}")
                self.assertNotIn(b"<", response.body)
        self.assertScratchEmpty()

    def test_errors_sent_while_the_version_still_says_0_9_carry_headers(self) -> None:
        # Older stdlib releases call send_error with request_version still at
        # its HTTP/0.9 default, for which http.server writes no headers at all.
        def parse_as_old_stdlib(handler: Handler) -> bool:
            handler.request_version = "HTTP/0.9"
            handler.send_error(400)
            return False

        with mock.patch.object(Handler, "parse_request", parse_as_old_stdlib):
            response = exchange(self.port, b"GET / HTTP/1.0 extra\r\n", half_close=True)
        assert response is not None
        self.assertJsonError(response, 400, "http_400")

    def test_responses_are_http_1_0_and_close(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            sock.sendall(build_request("GET", "/healthz"))
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 200 "))


class HealthzTest(ServerTestCase):
    def test_empty_200(self) -> None:
        response = self.request("GET", "/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers.get("Content-Length"), "0")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertIsNone(response.headers.get("Content-Type"))

    def test_query_is_ignored(self) -> None:
        self.assertEqual(self.request("GET", "/healthz?verbose=1").status, 200)

    def test_other_methods(self) -> None:
        response = self.request("POST", "/healthz", [("Content-Length", "0")])
        self.assertJsonError(response, 405, "method_not_allowed")
        self.assertEqual(response.headers.get("Allow"), "GET")


class IndexTest(ServerTestCase):
    def page(self) -> str:
        response = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        return response.body.decode("utf-8")

    def test_configuration_in_data_attributes(self) -> None:
        self.start(max_clip_seconds=30, default_target_lufs=-9.5, fetch_enabled=True, version="1.2.3",
                   source_url="https://example.internal/source", result_ttl_seconds=120)
        page = self.page()
        self.assertIn(
            '<main id="app" data-max-clip-seconds="30" data-default-target-lufs="-9.5" data-fetch-enabled="true" '
            'data-result-ttl-seconds="120" data-source-url="https://example.internal/source" data-version="1.2.3" '
            'data-static-prefix="/static/1.2.3">',
            page,
        )
        self.assertIn('<link rel="stylesheet" href="/static/1.2.3/style.css">', page)
        self.assertIn('<script type="module" src="/static/1.2.3/app.js"></script>', page)
        self.assertIn('<a href="https://example.internal/source">Source</a>', page)
        self.assertNotIn("{{", page)

    def test_defaults(self) -> None:
        page = self.page()
        self.assertIn('data-max-clip-seconds="1"', page)
        self.assertIn('data-default-target-lufs="-14.0"', page)
        self.assertIn('data-fetch-enabled="false"', page)
        self.assertIn('data-result-ttl-seconds="600"', page)
        self.assertIn('data-version="dev"', page)

    def test_values_are_escaped(self) -> None:
        self.start(source_url='https://example.internal/"<x>')
        page = self.page()
        escaped = "https://example.internal/&quot;&lt;x&gt;"
        self.assertIn(f'data-source-url="{escaped}"', page)
        self.assertIn(f'<a href="{escaped}">Source</a>', page)
        self.assertNotIn('"<x>', page)
        self.assertNotIn("<x>", page)

    def test_no_inline_script_or_style(self) -> None:
        # The CSP would block them anyway (style-src 'self' also blocks style
        # attributes); this keeps the page from silently relying on them.
        page = self.page()
        for tag in re.findall(r"<script\b[^>]*>", page):
            self.assertIn(" src=", tag)
        self.assertNotIn("<style", page)
        self.assertNotIn(" style=", page)
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", page))

    def test_page_basics(self) -> None:
        page = self.page()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('<html lang="en">', page)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)
        self.assertIn("<footer>", page)
        self.assertIn("Version dev", page)

    def test_page_references_only_allowlisted_assets(self) -> None:
        page = self.page()
        references = re.findall(r'\s(?:src|href)="([^"]*)"', page)
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference):
                if reference == self.settings.source_url:
                    continue
                self.assertTrue(reference.startswith("/static/dev/"), reference)
                name = reference[len("/static/dev/"):]
                self.assertIn(name, static.ALLOWED)
                self.assertNotEqual(name, "index.html")
                self.assertEqual(self.request("GET", reference).status, 200)

    def test_youtube_section_stays_hidden_in_the_markup(self) -> None:
        # app.js reveals it when data-fetch-enabled is "true"; the markup keeps
        # it hidden either way, so without the script nothing half-works.
        for enabled in (False, True):
            with self.subTest(fetch_enabled=enabled):
                self.start(fetch_enabled=enabled)
                self.assertIn('<section id="youtube" class="panel" hidden', self.page())

    def test_template_is_never_served_raw(self) -> None:
        for path in ("/index.html", "/static/dev/index.html"):
            with self.subTest(path):
                self.assertJsonError(self.request("GET", path), 404, "not_found")

    def test_unknown_placeholder_fails_startup(self) -> None:
        copy = self.temp / "static"
        shutil.copytree(static.STATIC_DIR, copy)
        (copy / "index.html").write_text("<p>{{nope}}</p>", encoding="utf-8")
        with mock.patch.object(static, "STATIC_DIR", copy):
            with self.assertRaisesRegex(ValueError, "nope"):
                RingtoneServer(("127.0.0.1", 0), self.settings)

    def test_missing_file_fails_startup(self) -> None:
        copy = self.temp / "static"
        shutil.copytree(static.STATIC_DIR, copy)
        (copy / "app.js").unlink()
        with self.assertRaises(FileNotFoundError):
            static.StaticFiles(copy)

    def test_missing_nested_file_fails_startup(self) -> None:
        copy = self.temp / "static"
        shutil.copytree(static.STATIC_DIR, copy)
        (copy / "audio" / "wav.js").unlink()
        with self.assertRaises(FileNotFoundError):
            static.StaticFiles(copy)


class StaticTest(ServerTestCase):
    def test_assets(self) -> None:
        expected = {
            "style.css": "text/css; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "waveform.js": "text/javascript; charset=utf-8",
            "player.js": "text/javascript; charset=utf-8",
            "share.js": "text/javascript; charset=utf-8",
            "audio/decode.js": "text/javascript; charset=utf-8",
            "audio/wav.js": "text/javascript; charset=utf-8",
            "audio/loudness.js": "text/javascript; charset=utf-8",
            "audio/suggest.js": "text/javascript; charset=utf-8",
            "audio/fades.js": "text/javascript; charset=utf-8",
            "audio/request.js": "text/javascript; charset=utf-8",
        }
        # Written out, so a file dropped from the allowlist or given another
        # type fails here; the template is the one listed name never served.
        self.assertEqual({**expected, "index.html": "text/html; charset=utf-8"}, static.ALLOWED)
        for name, content_type in expected.items():
            with self.subTest(name):
                response = self.request("GET", f"/static/dev/{name}")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), content_type)
                self.assertEqual(response.headers.get("Cache-Control"), "public, max-age=31536000, immutable")
                self.assertEqual(response.body, (static.STATIC_DIR / name).read_bytes())

    def test_query_is_ignored(self) -> None:
        self.assertEqual(self.request("GET", "/static/dev/app.js?v=2").status, 200)

    def test_path_tricks_are_not_found(self) -> None:
        paths = [
            "/static/dev/../dev/app.js",
            "/static/dev/./app.js",
            "/static/dev/%2e%2e/dev/app.js",
            "/static/dev/%61pp.js",
            "/static/dev%2Fapp.js",
            "/static/dev%2fapp.js",
            "/static/dev/app%2Ejs",
            "/static/dev\\app.js",
            "/static\\dev\\app.js",
            "/static/dev//app.js",
            "/static/dev/app.js/",
            "/static/dev/app.js%00",
            "/static/dev/app.js.map",
            "/static/DEV/app.js",
            "/static/dev/APP.JS",
            "/static/dev/App.js",
            "/STATIC/dev/app.js",
            "/static/dev/",
            "/static/dev",
            "/static/",
            "/static/app.js",
            "/static/0.0.1/app.js",
            "/static/dev/app.js;x",
            # Subpaths: only the exact key "audio/wav.js" is a file.
            "/static/dev/audio/../app.js",
            "/static/dev/audio/./wav.js",
            "/static/dev/./audio/wav.js",
            "/static/dev/audio/%2e%2e/app.js",
            "/static/dev/audio%2Fwav.js",
            "/static/dev/audio%2fwav.js",
            "/static/dev/audio//wav.js",
            "/static/dev/audio\\wav.js",
            "/static/dev/audio/wav.js/",
            "/static/dev/AUDIO/wav.js",
            "/static/dev/audio/",
            "/static/dev/audio",
            "/static/dev/wav.js",
            "/static/dev/audio/app.js",
            "/static/dev/../dev/audio/wav.js",
            # The ES-module marker for the Node tests is never served.
            "/static/dev/package.json",
            "/static/dev/audio/package.json",
            "/core/static/app.js",
            "/strange_ringtone/server.py",
            "http://example.internal/static/dev/app.js",
            "*",
            # The stdlib collapses leading slashes and splits the request line on
            # any whitespace, so these reach the handler as "/static/dev/app.js".
            "//static/dev/app.js",
            "///static/dev/app.js",
            "/static/dev/app.js",
            "/static/dev/app.js ",
            "/static/dev/app.js",
        ]
        for path in paths:
            with self.subTest(path):
                self.assertJsonError(self.request("GET", path), 404, "not_found")
        self.assertScratchEmpty()

    def test_only_the_current_version(self) -> None:
        self.start(version="1.2.3")
        self.assertEqual(self.request("GET", "/static/1.2.3/app.js").status, 200)
        self.assertJsonError(self.request("GET", "/static/dev/app.js"), 404, "not_found")
        self.assertJsonError(self.request("GET", "/static/1.2.4/app.js"), 404, "not_found")

    def test_nothing_is_read_from_disk_at_request_time(self) -> None:
        copy = self.temp / "static"
        shutil.copytree(static.STATIC_DIR, copy)
        originals = {name: (copy / name).read_bytes() for name in static.ALLOWED}
        with mock.patch.object(static, "STATIC_DIR", copy):
            self.start()
        shutil.rmtree(copy)
        for name in static.ALLOWED:
            if name != "index.html":
                with self.subTest(name):
                    self.assertEqual(self.request("GET", f"/static/dev/{name}").body, originals[name])
        self.assertEqual(self.request("GET", "/").status, 200)

    def test_get_never_returns_the_template_or_unlisted_names(self) -> None:
        files = static.StaticFiles()
        self.assertIsNone(files.get("index.html"))
        self.assertIsNone(files.get("nope.js"))
        self.assertIsNone(files.get("../static/app.js"))
        self.assertIsNone(files.get("audio/../app.js"))
        self.assertIsNone(files.get("audio"))
        self.assertIsNone(files.get("package.json"))
        # Exact, case included: the routes check ALLOWED first, so this pins
        # the lookup itself, which must not be the one place that folds case.
        for name in ("APP.JS", "App.js", "AUDIO/wav.js", "audio/WAV.js", "Style.css"):
            with self.subTest(name):
                self.assertIsNone(files.get(name))
        self.assertIsNotNone(files.get("app.js"))
        self.assertIsNotNone(files.get("audio/wav.js"))

    def test_allowlist_names_are_plain_relative_paths(self) -> None:
        static.check_names(static.ALLOWED)
        for name in ("../server.py", "audio/../app.js", "./app.js", "audio/./wav.js", "/app.js", "audio/",
                     "audio//wav.js", "audio\\wav.js", "C:/app.js", "app.js%00", "app js", "", "..", "."):
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    static.check_names([name])

    def test_a_bad_allowlist_name_fails_at_import(self) -> None:
        # The module checks ALLOWED as it is imported, so a bad entry stops the
        # server before it binds a port, whatever builds StaticFiles later. A
        # copy of the module with one bad key is imported from a temporary
        # directory; the unmodified copy is imported too, so the failure is
        # the check and not the copying.
        with open(static.__file__, encoding="utf-8") as handle:
            source = handle.read()
        entry = '    "app.js": _JS,\n'
        self.assertEqual(source.count(entry), 1)
        bad = source.replace(entry, entry + '    "../strange_ringtone/server.py": _JS,\n')

        def load(text: str, name: str) -> None:
            path = self.temp / f"{name}.py"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            spec = importlib.util.spec_from_file_location(name, path)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

        load(source, "static_copy_good")
        with self.assertRaisesRegex(ValueError, "bad static file name"):
            load(bad, "static_copy_bad")

    def test_a_bad_allowlist_name_fails_startup(self) -> None:
        # StaticFiles is where a name becomes a path, so it checks too: a ".."
        # entry would otherwise read, and serve, a file outside static/.
        bad = {"../strange_ringtone/server.py": "text/plain; charset=utf-8"}
        with mock.patch.dict(static.ALLOWED, bad):
            with self.assertRaisesRegex(ValueError, "bad static file name"):
                static.StaticFiles()

    def test_routing_never_trusts_a_rewritten_or_double_slash_target(self) -> None:
        # Called directly, so the "//" refusal is pinned even on a stdlib that
        # no longer collapses leading slashes before the handler sees them.
        def route(requestline: str, path: str) -> str | None:
            return Handler._route_path(mock.Mock(requestline=requestline, path=path))

        self.assertEqual(route("GET /static/dev/app.js HTTP/1.1", "/static/dev/app.js"), "/static/dev/app.js")
        self.assertIsNone(route("GET //static/dev/app.js HTTP/1.1", "//static/dev/app.js"))
        self.assertIsNone(route("GET //static/dev/app.js HTTP/1.1", "/static/dev/app.js"))
        self.assertIsNone(route("GET /static/dev/app.js\x0b HTTP/1.1", "/static/dev/app.js"))
        self.assertIsNone(route("GET  /static/dev/app.js HTTP/1.1", "/static/dev/app.js"))


class MethodTest(ServerTestCase):
    def test_wrong_method_on_known_paths(self) -> None:
        cases = [
            ("POST", "/", "GET"),
            ("POST", "/static/dev/app.js", "GET"),
            ("GET", "/api/render", "POST"),
            ("PUT", "/api/render", "POST"),
            ("DELETE", "/api/render", "POST"),
            ("PATCH", "/api/render", "POST"),
            ("OPTIONS", "/api/render", "POST"),
            ("OPTIONS", "/", "GET"),
            # Methods with no handler of their own, which the stdlib answers 501.
            ("TRACE", "/api/render", "POST"),
            ("CONNECT", "/api/render", "POST"),
            ("BREW", "/api/render", "POST"),
            ("get", "/api/render", "POST"),
            ("post", "/api/render", "POST"),
            ("BREW", "/", "GET"),
            ("TRACE", "/static/dev/app.js", "GET"),
        ]
        for method, path, allow in cases:
            with self.subTest(f"{method} {path}"):
                response = self.request(method, path, [("Content-Length", "0")])
                self.assertJsonError(response, 405, "method_not_allowed")
                self.assertEqual(response.headers.get("Allow"), allow)
        self.assertScratchEmpty()

    def test_preflight_is_never_approved(self) -> None:
        response = self.request("OPTIONS", "/api/render", [
            ("Origin", "https://other.example.internal"),
            ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "content-type"),
        ])
        self.assertJsonError(response, 405, "method_not_allowed")

    def test_head_has_no_body(self) -> None:
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            sock.sendall(build_request("HEAD", "/healthz"))
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 405 "))
        self.assertIn(b"\r\nAllow: GET", head)
        self.assertEqual(rest, b"")

    def test_unknown_paths(self) -> None:
        for method in ("GET", "POST", "PUT", "OPTIONS", "TRACE", "BREW"):
            with self.subTest(method):
                self.assertJsonError(self.request(method, "/api/nope", [("Content-Length", "0")]), 404, "not_found")
        self.assertScratchEmpty()


class RenderHook:
    """Records what a render hook saw, then answers 200 (or whatever it's told)."""

    def __init__(self, action: Callable[[Path, Handler], None] | None = None) -> None:
        self.calls: list[tuple[Path, list[str], bytes, bool]] = []
        self.action = action

    def __call__(self, job_dir: Path, handler: Handler) -> None:
        names = sorted(p.name for p in job_dir.iterdir())
        data = (job_dir / "in.wav").read_bytes()
        self.calls.append((job_dir, names, data, job_dir.parent == handler.server.settings.scratch_dir))
        if self.action is not None:
            self.action(job_dir, handler)
        else:
            handler.send_json(200, {"ok": True})


class OriginTest(ServerTestCase):
    BAD = [
        ("missing", []),
        ("duplicated", [ORIGIN, ORIGIN]),
        ("duplicated, one wrong", [ORIGIN, "https://other.example.internal"]),
        ("http scheme", ["http://example.internal"]),
        ("other host", ["https://other.example.internal"]),
        ("subdomain trick", ["https://example.internal.other.example"]),
        ("explicit port", ["https://example.internal:443"]),
        ("other port", ["https://example.internal:8443"]),
        ("trailing slash", ["https://example.internal/"]),
        ("upper-case host", ["https://Example.internal"]),
        ("upper-case scheme", ["HTTPS://example.internal"]),
        ("null", ["null"]),
        ("empty", [""]),
    ]

    def test_bad_origins_are_refused(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        for label, origins in self.BAD:
            with self.subTest(label):
                headers = [("Origin", value) for value in origins]
                headers += [("Content-Type", "audio/wav"), ("Content-Length", "4")]
                # No body is sent: a refused request is answered before reading it.
                self.assertJsonError(self.post_render(headers), 403, "bad_origin")
                self.assertScratchEmpty()
        self.assertEqual(hook.calls, [])

    def test_exact_origin_passes(self) -> None:
        # Past every check to the WAV validation, which refuses four bytes.
        response = self.post_render(self.render_headers(4), b"RIFF")
        self.assertBadWav(response, "truncated")
        self.assertScratchEmpty()


class ContentTypeTest(ServerTestCase):
    def test_anything_but_audio_wav_is_refused(self) -> None:
        cases = [
            ("missing", []),
            ("duplicated", ["audio/wav", "audio/wav"]),
            ("x-wav", ["audio/x-wav"]),
            ("wave", ["audio/wave"]),
            ("parameter", ["audio/wav; codecs=1"]),
            ("upper case", ["AUDIO/WAV"]),
            ("form", ["application/x-www-form-urlencoded"]),
            ("plain text", ["text/plain"]),
            ("multipart", ["multipart/form-data; boundary=x"]),
            ("octet stream", ["application/octet-stream"]),
            ("json", ["application/json"]),
        ]
        for label, types in cases:
            with self.subTest(label):
                headers = [("Origin", ORIGIN)] + [("Content-Type", value) for value in types]
                headers.append(("Content-Length", "4"))
                self.assertJsonError(self.post_render(headers), 415, "unsupported_media_type")
                self.assertScratchEmpty()


class LengthTest(ServerTestCase):
    def base(self) -> list[tuple[str, str]]:
        return [("Origin", ORIGIN), ("Content-Type", "audio/wav")]

    def test_transfer_encoding_is_refused(self) -> None:
        for label, extra in (("chunked", [("Transfer-Encoding", "chunked")]),
                             ("chunked and length", [("Transfer-Encoding", "chunked"), ("Content-Length", "4")]),
                             ("identity", [("Transfer-Encoding", "identity"), ("Content-Length", "4")])):
            with self.subTest(label):
                self.assertJsonError(self.post_render(self.base() + extra), 400, "transfer_encoding")
                self.assertScratchEmpty()

    def test_missing_length(self) -> None:
        self.assertJsonError(self.post_render(self.base()), 411, "length_required")
        self.assertScratchEmpty()

    def test_malformed_lengths(self) -> None:
        for value in ("+4", "-1", "0x4", "4.0", "4,4", "4 ", "four", "²", "4_0"):
            with self.subTest(value):
                self.assertJsonError(self.post_render(self.base() + [("Content-Length", value)]), 400, "bad_length")
                self.assertScratchEmpty()
        duplicated = self.base() + [("Content-Length", "4"), ("Content-Length", "4")]
        self.assertJsonError(self.post_render(duplicated), 400, "bad_length")
        self.assertScratchEmpty()

    def test_too_large(self) -> None:
        # Only the headers are sent: the refusal must come before any body read.
        for value in (str(self.settings.max_body_bytes + 1), "9" * 40):
            with self.subTest(value):
                self.assertJsonError(self.post_render(self.base() + [("Content-Length", value)]), 413, "too_large")
                self.assertScratchEmpty()

    def test_exactly_the_limit_is_accepted(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        size = self.settings.max_body_bytes
        self.assertEqual(size, 1 * 48000 * 2 * 2 + 4096)
        body = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
        response = self.post_render(self.render_headers(size), body)
        self.assertEqual(response.status, 200)
        self.assertEqual(hook.calls[0][2], body)
        self.wait_for_log("POST /api/render 200")
        self.assertScratchEmpty()


class CheckOrderTest(ServerTestCase):
    def test_path_before_origin(self) -> None:
        self.assertJsonError(self.post_render([("Content-Length", "4")], path="/api/nope"), 404, "not_found")

    def test_aliases_of_the_render_path_are_not_found(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        for path in ("//api/render", "/api/render", "/api/render "):
            with self.subTest(path):
                self.assertJsonError(self.post_render(self.render_headers(4), b"RIFF", path=path), 404, "not_found")
        self.assertEqual(hook.calls, [])
        self.assertScratchEmpty()

    def test_origin_before_content_type_and_length(self) -> None:
        headers = [("Origin", "https://other.example.internal"), ("Content-Type", "text/plain"),
                   ("Transfer-Encoding", "chunked")]
        self.assertJsonError(self.post_render(headers), 403, "bad_origin")

    def test_content_type_before_length(self) -> None:
        headers = [("Origin", ORIGIN), ("Content-Type", "text/plain"), ("Transfer-Encoding", "chunked")]
        self.assertJsonError(self.post_render(headers), 415, "unsupported_media_type")

    def test_length_checks_before_rate_limit(self) -> None:
        self.start(rate_limit_renders_per_minute=1)
        self.assertBadWav(self.post_render(self.render_headers(4), b"RIFF"), "truncated")
        refusals = [
            ([("Content-Type", "audio/wav"), ("Content-Length", "4")], 403),
            ([("Origin", ORIGIN), ("Content-Type", "text/plain"), ("Content-Length", "4")], 415),
            ([("Origin", ORIGIN), ("Content-Type", "audio/wav"), ("Transfer-Encoding", "chunked")], 400),
            ([("Origin", ORIGIN), ("Content-Type", "audio/wav")], 411),
            ([("Origin", ORIGIN), ("Content-Type", "audio/wav"), ("Content-Length", "99999999")], 413),
        ]
        for headers, status in refusals:
            with self.subTest(status):
                self.assertEqual(self.post_render(headers).status, status)
        self.assertJsonError(self.post_render(self.render_headers(4)), 429, "rate_limited")
        self.assertScratchEmpty()

    def test_refused_requests_do_not_use_up_the_rate_limit(self) -> None:
        self.start(rate_limit_renders_per_minute=1)
        for _ in range(3):
            self.assertEqual(self.post_render([("Content-Length", "4")]).status, 403)
        self.assertBadWav(self.post_render(self.render_headers(4), b"RIFF"), "truncated")
        self.assertScratchEmpty()


class RateLimitTest(ServerTestCase):
    def test_limit_per_client(self) -> None:
        self.start(rate_limit_renders_per_minute=2)
        for _ in range(2):
            self.assertBadWav(self.post_render(self.render_headers(4), b"RIFF"), "truncated")
        response = self.post_render(self.render_headers(4))
        self.assertJsonError(response, 429, "rate_limited")
        retry_after = response.headers.get("Retry-After")
        assert retry_after is not None
        self.assertRegex(retry_after, r"^[0-9]+$")
        self.assertTrue(1 <= int(retry_after) <= 60)
        self.assertScratchEmpty()

    def test_forwarded_for_is_ignored_from_untrusted_peers(self) -> None:
        self.start(rate_limit_renders_per_minute=1)
        first = self.render_headers(4) + [("X-Forwarded-For", "192.0.2.1")]
        second = self.render_headers(4) + [("X-Forwarded-For", "192.0.2.2")]
        self.assertEqual(self.post_render(first, b"RIFF").status, 400)
        # Same socket peer, so the same client whatever the header claims.
        self.assertJsonError(self.post_render(second), 429, "rate_limited")
        self.assertScratchEmpty()

    def test_forwarded_for_is_honoured_from_trusted_proxies(self) -> None:
        self.start(rate_limit_renders_per_minute=1, trusted_proxies=(ipaddress.ip_network("127.0.0.0/8"),))
        first = self.render_headers(4) + [("X-Forwarded-For", "192.0.2.1")]
        second = self.render_headers(4) + [("X-Forwarded-For", "198.51.100.7, 192.0.2.2")]
        self.assertEqual(self.post_render(first, b"RIFF").status, 400)
        self.assertEqual(self.post_render(second, b"RIFF").status, 400)
        # 192.0.2.1 again, now behind a client-written hop on the left.
        again = self.render_headers(4) + [("X-Forwarded-For", "203.0.113.9, 192.0.2.1")]
        self.assertJsonError(self.post_render(again), 429, "rate_limited")
        # Garbage falls back to the peer, which has its own budget.
        garbage = self.render_headers(4) + [("X-Forwarded-For", "not-an-address")]
        self.assertEqual(self.post_render(garbage, b"RIFF").status, 400)
        self.assertEqual(self.post_render(garbage).status, 429)
        self.assertScratchEmpty()


class GateTest(ServerTestCase):
    def test_busy_when_renders_are_in_flight(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def block(job_dir: Path, handler: Handler) -> None:
            entered.set()
            release.wait(DEADLINE)
            handler.send_json(200, {"ok": True})

        hook = RenderHook(block)
        self.start(render_hook=hook, render_concurrency=1)
        results: list[Response] = []
        first = threading.Thread(target=lambda: results.append(self.post_render(self.render_headers(4), b"RIFF")))
        first.start()
        try:
            self.assertTrue(entered.wait(DEADLINE))
            response = self.post_render(self.render_headers(4), b"RIFF")
            self.assertJsonError(response, 503, "busy")
            self.assertEqual(response.headers.get("Retry-After"), "1")
        finally:
            release.set()
            first.join(DEADLINE)
        self.assertEqual(results[0].status, 200)
        self.assertEqual(len(hook.calls), 1)
        # The slot is free again.
        release.set()
        self.assertEqual(self.post_render(self.render_headers(4), b"RIFF").status, 200)
        self.wait_for_log("POST /api/render 200", count=2)
        self.assertScratchEmpty()

    def hold_the_slot(self, **overrides: Any) -> tuple[threading.Event, threading.Thread, list[Response]]:
        """Start a server with one render slot and a first render that holds it until released."""
        entered = threading.Event()
        release = threading.Event()

        def block(job_dir: Path, handler: Handler) -> None:
            entered.set()
            release.wait(DEADLINE)
            handler.send_json(200, {"ok": True})

        self.start(render_hook=RenderHook(block), render_concurrency=1, **overrides)
        results: list[Response] = []
        first = threading.Thread(target=lambda: results.append(self.post_render(self.render_headers(4), b"RIFF")))
        first.start()
        self.addCleanup(first.join, DEADLINE)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(DEADLINE))
        return release, first, results

    def test_busy_refusals_do_not_use_up_the_rate_limit(self) -> None:
        # A 503 ran no render. Counting it would let a UI that retries after
        # Retry-After: 1 burn its budget and then meet 429 for up to a minute.
        release, first, results = self.hold_the_slot(rate_limit_renders_per_minute=2)
        for _ in range(3):
            self.assertJsonError(self.post_render(self.render_headers(4), b"RIFF"), 503, "busy")
        release.set()
        first.join(DEADLINE)
        self.assertEqual(results[0].status, 200)
        # One render ran, so one of the two is left.
        self.assertEqual(self.post_render(self.render_headers(4), b"RIFF").status, 200)
        self.assertJsonError(self.post_render(self.render_headers(4)), 429, "rate_limited")
        self.wait_for_log("POST /api/render 200", count=2)
        self.assertScratchEmpty()

    def test_a_client_over_its_limit_gets_429_even_while_busy(self) -> None:
        # The rate limit is still asked first: a client out of renders learns
        # how long to wait, not that a retry in a second might work.
        release, first, results = self.hold_the_slot(rate_limit_renders_per_minute=1)
        response = self.post_render(self.render_headers(4), b"RIFF")
        self.assertJsonError(response, 429, "rate_limited")
        self.assertIsNotNone(response.headers.get("Retry-After"))
        release.set()
        first.join(DEADLINE)
        self.assertEqual(results[0].status, 200)
        self.wait_for_log("POST /api/render 200")
        self.assertScratchEmpty()

    def test_slot_is_released_after_a_failed_hook(self) -> None:
        def fail(job_dir: Path, handler: Handler) -> None:
            raise RuntimeError("boom")

        hook = RenderHook(fail)
        self.start(render_hook=hook, render_concurrency=1)
        for _ in range(3):
            self.assertJsonError(self.post_render(self.render_headers(4), b"RIFF"), 500, "internal_error")
        self.assertEqual(len(hook.calls), 3)
        self.wait_for_log("POST /api/render 500", count=3)
        self.assertScratchEmpty()

    def test_slot_and_job_directory_are_free_before_the_client_sees_the_answer(self) -> None:
        # A slow cleanup widens the window between the answer and the release
        # of the slot. A client that sends its next request as soon as it has
        # read an answer must never find its own render still holding the slot.
        real_rmtree = shutil.rmtree

        def slow_rmtree(*args: Any, **kwargs: Any) -> None:
            time.sleep(0.2)
            real_rmtree(*args, **kwargs)

        def fail(job_dir: Path, handler: Handler) -> None:
            raise RuntimeError("boom")

        for label, action, status in (("answered", None, 200), ("failed", fail, 500)):
            with self.subTest(label):
                hook = RenderHook(action)
                self.start(render_hook=hook, render_concurrency=1)
                with mock.patch("shutil.rmtree", slow_rmtree):
                    for _ in range(3):
                        response = self.post_render(self.render_headers(4), b"RIFF")
                        self.assertEqual(response.status, status, response.body)
                        self.assertFalse(hook.calls[-1][0].exists())
                self.assertEqual(len(hook.calls), 3)
                self.assertScratchEmpty()

    def test_a_hook_that_fails_halfway_still_gets_a_clean_500(self) -> None:
        def fail_halfway(job_dir: Path, handler: Handler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "audio/mp4")
            raise RuntimeError("boom")

        self.start(render_hook=RenderHook(fail_halfway))
        self.assertJsonError(self.post_render(self.render_headers(4), b"RIFF"), 500, "internal_error")
        self.assertScratchEmpty()


class BodyTest(ServerTestCase):
    def test_default_hook_validates_the_wav(self) -> None:
        response = self.post_render(self.render_headers(4), b"RIFF")
        self.assertBadWav(response, "truncated")
        self.assertEqual(response.body, b'{"error": "bad_wav", "detail": "truncated"}')
        self.wait_for_log("POST /api/render 400")
        self.assertScratchEmpty()

    def test_hook_gets_in_wav_with_the_exact_bytes(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        # Several chunks' worth, so the streaming loop runs more than once.
        body = os.urandom(150_000)
        headers = self.render_headers(len(body)) + [("X-Tone-Name", "../../escape")]
        response = self.post_render(headers, body)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"ok": True})
        job_dir, names, data, under_scratch = hook.calls[0]
        self.assertEqual(names, ["in.wav"])
        self.assertEqual(data, body)
        self.assertTrue(under_scratch)
        self.assertTrue(job_dir.name.startswith("job-"))
        self.wait_for_log("POST /api/render 200")
        self.assertFalse(job_dir.exists())
        self.assertScratchEmpty()
        self.assertEqual(sorted(p.name for p in self.temp.iterdir()), ["scratch"])

    def test_a_full_disk_answers_500_not_a_dropped_connection(self) -> None:
        real_open = open

        class FullDisk:
            def __init__(self, fail_on: str) -> None:
                self.fail_on = fail_on

            def __enter__(self) -> FullDisk:
                return self

            def __exit__(self, *exc: object) -> None:
                # The buffered flush on close is where a full tmpfs can show up.
                if self.fail_on == "close":
                    raise OSError(28, "No space left on device")

            def write(self, chunk: bytes) -> int:
                if self.fail_on == "write":
                    raise OSError(28, "No space left on device")
                return len(chunk)

        for fail_on in ("write", "close"):
            with self.subTest(fail_on=fail_on):
                def fake_open(file: Any, mode: str = "r", *args: Any, fail_on: str = fail_on, **kwargs: Any) -> Any:
                    if Path(file).name == "in.wav":
                        return FullDisk(fail_on)
                    return real_open(file, mode, *args, **kwargs)

                with mock.patch.object(server_module, "open", fake_open, create=True):
                    response = self.post_render(self.render_headers(4), b"RIFF")
                self.assertJsonError(response, 500, "internal_error")
                self.wait_for_log("POST /api/render 500")
                self.assertScratchEmpty()

    def test_job_directory_is_removed_when_the_hook_raises(self) -> None:
        def fail(job_dir: Path, handler: Handler) -> None:
            raise RuntimeError("SecretToneName")

        hook = RenderHook(fail)
        self.start(render_hook=hook)
        response = self.post_render(self.render_headers(4), b"RIFF")
        self.assertJsonError(response, 500, "internal_error")
        self.wait_for_log("POST /api/render 500")
        self.assertEqual(len(hook.calls), 1)
        self.assertFalse(hook.calls[0][0].exists())
        self.assertScratchEmpty()
        self.assertNotIn("SecretToneName", self.log.getvalue())
        self.assertNotIn("Traceback", self.log.getvalue())

    def test_hook_guard_errors_pass_through(self) -> None:
        def refuse(job_dir: Path, handler: Handler) -> None:
            raise GuardError(422, "bad_wav")

        self.start(render_hook=RenderHook(refuse))
        self.assertJsonError(self.post_render(self.render_headers(4), b"RIFF"), 422, "bad_wav")
        self.wait_for_log("POST /api/render 422")
        self.assertScratchEmpty()

    def test_hook_guard_error_after_starting_a_response_replaces_it(self) -> None:
        # The begun 200 is dropped whole: the client gets the refusal with the
        # full header set, and the log records the refusal, not the 200.
        def refuse_halfway(job_dir: Path, handler: Handler) -> None:
            handler.send_json(200, {"partial": 1})
            raise GuardError(422, "bad_wav")

        self.start(render_hook=RenderHook(refuse_halfway))
        response = self.post_render(self.render_headers(4), b"RIFF")
        self.assertJsonError(response, 422, "bad_wav")
        self.assertNotIn(b"partial", response.body)
        self.wait_for_log("POST /api/render 422")
        self.assertFalse(self.log_has("POST /api/render 200"))
        self.assertScratchEmpty()

    def test_upload_never_writes_through_a_file_already_at_its_name(self) -> None:
        # in.wav is opened with exclusive create. Here a hard link to a file
        # outside scratch already sits at that name: writing through it would
        # overwrite the outside file with the upload.
        outside = self.temp / "outside.wav"
        outside.write_bytes(b"KEEP")
        real_mkdtemp = tempfile.mkdtemp

        def planted(*args: Any, **kwargs: Any) -> str:
            job_dir = real_mkdtemp(*args, **kwargs)
            os.link(outside, Path(job_dir) / "in.wav")
            return job_dir

        hook = RenderHook()
        self.start(render_hook=hook)
        with mock.patch.object(server_module.tempfile, "mkdtemp", planted):
            response = self.post_render(self.render_headers(4), b"RIFF")
        self.assertJsonError(response, 500, "internal_error")
        self.assertEqual(hook.calls, [])
        self.assertEqual(outside.read_bytes(), b"KEEP")
        self.wait_for_log("POST /api/render 500")
        self.assertNotIn("error while handling a request", self.log.getvalue())
        self.assertScratchEmpty()

    def test_body_shorter_than_length_client_closes(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            sock.sendall(build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10))
        # The client is gone, so there is no reply to read; the log line is
        # written after the cleanup has run.
        self.wait_for_log("POST /api/render 400")
        self.assertEqual(hook.calls, [])
        self.assertScratchEmpty()

    def test_body_shorter_than_length_client_waits(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        data = build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10)
        response = exchange(self.port, data, half_close=True)
        assert response is not None
        self.assertJsonError(response, 400, "incomplete_body")
        self.assertEqual(hook.calls, [])
        self.wait_for_log("POST /api/render 400")
        self.assertScratchEmpty()

    def test_body_is_streamed_in_bounded_reads(self) -> None:
        reads: list[int] = []
        original_setup = Handler.setup

        class ReadSpy:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def read(self, size: int = -1) -> bytes:
                reads.append(size)
                return self._inner.read(size)

            def read1(self, size: int = -1) -> bytes:
                reads.append(size)
                return self._inner.read1(size)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        def spying_setup(handler: Handler) -> None:
            original_setup(handler)
            handler.rfile = ReadSpy(handler.rfile)

        hook = RenderHook()
        with mock.patch.object(Handler, "setup", spying_setup):
            self.start(render_hook=hook, max_clip_seconds=2)
            body = os.urandom(3 * CHUNK_BYTES + 123)
            response = self.post_render(self.render_headers(len(body)), body)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(hook.calls[0][2], body)
        # Never the whole body at once, and never more than one chunk per read.
        self.assertGreaterEqual(len(reads), 4)
        self.assertTrue(all(0 < size <= CHUNK_BYTES for size in reads), reads)
        self.wait_for_log("POST /api/render 200")
        self.assertScratchEmpty()

    def test_handler_has_a_finite_socket_timeout(self) -> None:
        # At this layer the socket timeout is the only thing that ends a
        # stalled body read; test_stalled_body_times_out patches it shorter.
        self.assertIsNotNone(Handler.timeout)
        self.assertGreater(Handler.timeout, 0)
        self.assertLessEqual(Handler.timeout, 60)

    def test_stalled_body_times_out(self) -> None:
        hook = RenderHook()
        with mock.patch.object(Handler, "timeout", 0.3):
            self.start(render_hook=hook)
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                sock.sendall(build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10))
                self.wait_for_log("POST /api/render 400")
        self.assertEqual(hook.calls, [])
        self.assertScratchEmpty()

    def test_body_has_a_finite_overall_deadline(self) -> None:
        self.assertGreater(server_module.BODY_DEADLINE_SECONDS, 0)
        self.assertLessEqual(server_module.BODY_DEADLINE_SECONDS, 120)

    def test_trickled_body_loses_its_slot_at_the_deadline(self) -> None:
        # One byte at a time, each well inside the 30 s socket timeout: without
        # an overall deadline this one request holds the only render slot for
        # as long as the client keeps trickling.
        hook = RenderHook()
        with mock.patch.object(server_module, "BODY_DEADLINE_SECONDS", 0.5):
            self.start(render_hook=hook, render_concurrency=1)
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                started = time.monotonic()
                sock.sendall(build_request("POST", "/api/render", self.render_headers(1000), b"x"))
                # The client keeps trickling for 4 s, far past the deadline, so
                # only the deadline can end this read early.
                give_up = started + 4
                while time.monotonic() < give_up and not self.log_has("POST /api/render 408"):
                    try:
                        sock.sendall(b"x")
                    except OSError:
                        break
                    time.sleep(0.05)
                self.wait_for_log("POST /api/render 408")
                self.assertLess(time.monotonic() - started, 0.5 + 1.5)
            # The slot is free again for everyone else.
            self.assertEqual(self.post_render(self.render_headers(4), b"RIFF").status, 200)
        self.assertEqual(len(hook.calls), 1)
        self.wait_for_log("POST /api/render 200")
        self.assertScratchEmpty()

    def test_body_deadline_is_checked_before_every_read(self) -> None:
        # Driven by a fake clock, so the order of checks is exact: each read
        # takes 30 s, and the third read would start at the 60 s deadline.
        now = [0.0]
        timeouts: list[float] = []
        reads: list[int] = []

        class Clock:
            @staticmethod
            def monotonic() -> float:
                return now[0]

        class Connection:
            @staticmethod
            def settimeout(value: float) -> None:
                timeouts.append(value)

        class Body:
            @staticmethod
            def read1(size: int) -> bytes:
                reads.append(size)
                now[0] += 30
                return b"x"

            @staticmethod
            def read(size: int = -1) -> bytes:
                # read() loops until it has size bytes, so a trickle of bytes
                # could keep one call alive past the deadline.
                raise AssertionError("read() instead of read1()")

        handler = Handler.__new__(Handler)
        handler.connection = Connection()  # type: ignore[assignment]
        handler.rfile = Body()  # type: ignore[assignment]
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(server_module, "time", Clock),
            mock.patch.object(server_module, "BODY_DEADLINE_SECONDS", 60),
        ):
            failure = handler._receive_body(Path(temp) / "in.wav", 1000)
            self.assertEqual(len(list(Path(temp).iterdir())), 1)
        assert failure is not None
        self.assertEqual((failure.status, failure.code), (408, "body_timeout"))
        self.assertEqual(reads, [1000, 999])
        # Every wait was positive and within the deadline, and the per-write
        # timeout is back for the answer.
        self.assertEqual(timeouts, [30, 30, Handler.timeout])

    def test_body_deadline_answers_408(self) -> None:
        hook = RenderHook()
        with mock.patch.object(server_module, "BODY_DEADLINE_SECONDS", 0.3):
            self.start(render_hook=hook)
            data = build_request("POST", "/api/render", self.render_headers(1000), b"x" * 10)
            # The client stops sending but keeps its side open to read the answer.
            response = exchange(self.port, data)
        assert response is not None
        self.assertJsonError(response, 408, "body_timeout")
        self.assertEqual(hook.calls, [])
        self.assertScratchEmpty()

    def test_body_longer_than_length(self) -> None:
        hook = RenderHook()
        self.start(render_hook=hook)
        body = b"a" * 100
        data = build_request("POST", "/api/render", self.render_headers(100), body + b"b" * 50)
        response = exchange(self.port, data, tolerate_reset=True)
        if response is not None:
            self.assertEqual(response.status, 200)
        self.wait_for_log("POST /api/render 200")
        self.assertEqual(hook.calls[0][2], body)
        self.assertScratchEmpty()


class ScratchTest(ServerTestCase):
    def test_startup_creates_and_empties_the_scratch_directory(self) -> None:
        self.assertTrue(self.scratch.is_dir())
        (self.scratch / "stale.wav").write_bytes(b"old")
        (self.scratch / "job-old").mkdir()
        (self.scratch / "job-old" / "in.wav").write_bytes(b"old")
        self.start()
        self.assertScratchEmpty()

    def test_scratch_directory_removed_while_running_answers_500(self) -> None:
        # Not recreated: whatever removed it could have left something in its
        # place, and the checks that make it safe run only at startup.
        hook = RenderHook()
        self.start(render_hook=hook, render_concurrency=1)
        shutil.rmtree(self.scratch)
        for _ in range(2):
            response = self.post_render(self.render_headers(4), b"RIFF")
            self.assertJsonError(response, 500, "internal_error")
        self.assertEqual(hook.calls, [])
        self.assertFalse(self.scratch.exists())
        self.wait_for_log("POST /api/render 500", count=2)
        self.assertNotIn("error while handling a request", self.log.getvalue())
        # The render slot was given back each time.
        self.scratch.mkdir()
        self.assertEqual(self.post_render(self.render_headers(4), b"RIFF").status, 200)
        self.wait_for_log("POST /api/render 200")
        self.assertScratchEmpty()

    def test_a_failed_bind_leaves_the_running_servers_scratch_alone(self) -> None:
        # Starting the documented command twice must not delete the active
        # jobs of the instance that owns the port.
        (self.scratch / "job-active").mkdir()
        (self.scratch / "job-active" / "in.wav").write_bytes(b"active")
        with self.assertRaises(OSError):
            RingtoneServer(("127.0.0.1", self.port), self.settings)
        self.assertEqual((self.scratch / "job-active" / "in.wav").read_bytes(), b"active")

    def test_the_socket_is_closed_when_scratch_preparation_fails(self) -> None:
        closed: list[bool] = []

        class Spy(RingtoneServer):
            def server_close(self) -> None:
                closed.append(True)
                super().server_close()

        with mock.patch.object(server_module, "_prepare_scratch", side_effect=RuntimeError("no scratch")):
            with self.assertRaisesRegex(RuntimeError, "no scratch"):
                Spy(("127.0.0.1", 0), self.settings)
        self.assertEqual(closed, [True])

    def test_missing_parent_is_refused_not_created(self) -> None:
        # Without the /tmp tmpfs the core must not create /tmp on the root
        # filesystem and write uploads there.
        parent = self.temp / "no-tmpfs"
        with self.assertRaisesRegex(RuntimeError, "cannot create the scratch directory"):
            RingtoneServer(("127.0.0.1", 0), Settings(public_origin=ORIGIN, scratch_dir=parent / "scratch"))
        self.assertFalse(parent.exists())

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX permissions")
    def test_scratch_directory_is_owner_only(self) -> None:
        self.assertEqual(self.scratch.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX permissions")
    def test_an_existing_scratch_directory_is_made_owner_only(self) -> None:
        # Created by someone else's umask, or loosened later: startup tightens it.
        os.chmod(self.scratch, 0o777)
        self.start()
        self.assertEqual(self.scratch.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership")
    def test_scratch_directory_owned_by_another_user_is_refused(self) -> None:
        other = os.lstat(self.scratch).st_uid + 1
        with mock.patch("os.getuid", return_value=other):
            with self.assertRaisesRegex(RuntimeError, "owned by another user"):
                RingtoneServer(("127.0.0.1", 0), self.settings)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks")
    def test_symlinked_scratch_directory_is_refused(self) -> None:
        target = self.temp / "elsewhere"
        target.mkdir()
        link = self.temp / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            # Windows allows symlinks only with Developer Mode or elevation.
            self.skipTest("cannot create a symlink here")
        with self.assertRaisesRegex(RuntimeError, "not a plain directory"):
            RingtoneServer(("127.0.0.1", 0), Settings(public_origin=ORIGIN, scratch_dir=link))
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks")
    def test_startup_removes_planted_symlinks_without_following_them(self) -> None:
        # A link inside scratch is removed as a link: its target, outside
        # scratch, is neither emptied nor left reachable from scratch.
        outside_dir = self.temp / "outside"
        outside_dir.mkdir()
        (outside_dir / "keep").write_bytes(b"keep")
        outside_file = self.temp / "outside.wav"
        outside_file.write_bytes(b"keep")
        try:
            (self.scratch / "planted").symlink_to(outside_dir, target_is_directory=True)
            (self.scratch / "planted.wav").symlink_to(outside_file)
        except OSError:
            # Windows allows symlinks only with Developer Mode or elevation.
            self.skipTest("cannot create a symlink here")
        self.start()
        self.assertScratchEmpty()
        self.assertEqual(sorted(p.name for p in outside_dir.iterdir()), ["keep"])
        self.assertEqual((outside_dir / "keep").read_bytes(), b"keep")
        self.assertEqual(outside_file.read_bytes(), b"keep")


class WorkerCapTest(ServerTestCase):
    server_class = _CappedServer

    def test_connections_past_the_cap_get_503(self) -> None:
        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) for _ in range(2)]
        try:
            # Accepted in order, so these two hold both workers while they send nothing.
            response = exchange(self.port, b"")
            assert response is not None
            self.assertJsonError(response, 503, "busy")
            self.assertEqual(response.headers.get("Retry-After"), "1")
        finally:
            for sock in idle:
                sock.close()

        def healthy() -> bool:
            response = exchange(self.port, build_request("GET", "/healthz"))
            return response is not None and response.status == 200

        wait_for(healthy, "the workers to be released")


class LogTest(ServerTestCase):
    def test_line_format_without_query_or_headers(self) -> None:
        response = self.request("GET", "/healthz?tone=SecretQuery", [("X-Tone-Name", "SecretHeader")])
        self.assertEqual(response.status, 200)
        line = self.wait_for_log("GET /healthz ")[0]
        self.assertRegex(line, r"^GET /healthz 200 [0-9]+\.[0-9]{3}$")
        self.assertNotIn("Secret", self.log.getvalue())

    def test_duration_is_measured(self) -> None:
        # A fake clock in the server module only: each reading is 1.25 s
        # after the last, so the one request reads it exactly twice.
        readings = iter(100.0 + 1.25 * n for n in range(1000))

        class Clock:
            @staticmethod
            def monotonic() -> float:
                return next(readings)

        with mock.patch.object(server_module, "time", Clock):
            self.assertEqual(self.request("GET", "/healthz").status, 200)
            line = self.wait_for_log("GET /healthz ")[0]
        self.assertEqual(line, "GET /healthz 200 1.250")

    def test_fragment_is_not_logged(self) -> None:
        response = self.request("GET", "/healthz#SecretFragment")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.wait_for_log("GET /")[0].rsplit(" ", 1)[0], "GET /healthz 200")
        self.assertNotIn("Secret", self.log.getvalue())

    def test_a_connection_without_a_request_logs_nothing(self) -> None:
        socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE).close()
        self.assertEqual(self.request("GET", "/healthz").status, 200)
        # Stopping joins every handler thread, so the empty connection's
        # handler has finished, and logged whatever it was going to.
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual([line.rsplit(" ", 1)[0] for line in self.log.lines()], ["GET /healthz 200"])

    def test_render_request_logs_nothing_private(self) -> None:
        headers = self.render_headers(10) + [("X-Tone-Name", "SecretHeader"), ("User-Agent", "SecretAgent")]
        response = self.post_render(headers, b"SecretBody")
        self.assertEqual(response.status, 400)
        self.request("POST", "/api/render?name=SecretQuery&fade_in=1", [("Content-Length", "0")])
        lines = self.wait_for_log("POST /api/render ", count=2)
        # Each line is written after its response, so their order can vary.
        self.assertEqual(sorted(line.rsplit(" ", 1)[0] for line in lines),
                         ["POST /api/render 400", "POST /api/render 403"])
        for line in lines:
            self.assertRegex(line, r"^POST /api/render [0-9]{3} [0-9]+\.[0-9]{3}$")
        log = self.log.getvalue()
        self.assertNotIn("Secret", log)
        self.assertNotIn("127.0.0.1", log)
        for line in self.log.lines():
            self.assertRegex(line, LOG_LINE_RE)

    def test_control_characters_and_long_paths(self) -> None:
        self.request("GET", "/x\x1b[31mred")
        # "z" is not a hex digit, so the result-ID mask leaves the path alone
        # and only the length cap applies.
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
            self.assertIsNone(exchange(self.port, build_request("GET", "/"), tolerate_reset=True))
            wait_for(lambda: "error while handling a request" in self.log.getvalue(), "the error line")
        self.assertIn("error while handling a request: RuntimeError\n", self.log.getvalue())
        self.assertNotIn("Traceback", self.log.getvalue())
        self.assertNotIn("SecretDetail", self.log.getvalue())

    def test_stdlib_errors_log_one_line_without_the_request_line(self) -> None:
        exchange(self.port, b"GET /?SecretQuery HTTP/1.0 extra\r\n", half_close=True)
        line = self.wait_for_log("- ")[0]
        self.assertRegex(line, r"^- - 400 [0-9]+\.[0-9]{3}$")
        self.assertNotIn("Secret", self.log.getvalue())

    def test_refused_methods_log_one_line_without_the_query(self) -> None:
        exchange(self.port, b"BREW /?SecretQuery HTTP/1.0\r\n\r\n")
        line = self.wait_for_log("BREW ")[0]
        self.assertRegex(line, r"^BREW / 405 [0-9]+\.[0-9]{3}$")
        self.assertNotIn("Secret", self.log.getvalue())


class FakeClock:
    """The results store's clock, moved only by the test."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class RenderFlowTestCase(ServerTestCase):
    """The default hook end to end, with the stub ffmpeg and a fake results clock."""

    def setUp(self) -> None:
        super().setUp()
        self.runs_log = self.temp / "runs.jsonl"
        self.stub_mode("ok")
        self.clock = FakeClock()
        self.start(clock=self.clock)

    def stub_mode(self, mode: str) -> None:
        patcher = mock.patch.dict(os.environ, {STUB_MODE_VAR: mode, STUB_LOG_VAR: str(self.runs_log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def runs(self) -> list[list[str]]:
        if not self.runs_log.exists():
            return []
        return [json.loads(line)["argv"] for line in self.runs_log.read_text(encoding="utf-8").splitlines()]

    def wav(self, seconds: float = 1.0) -> bytes:
        return valid_wav(self.temp / "upload.wav", seconds).read_bytes()

    def render(self, query: str = "", name: str | None = None, body: bytes | None = None,
               extra: list[tuple[str, str]] | None = None) -> Response:
        data = self.wav() if body is None else body
        headers = self.render_headers(len(data))
        if name is not None:
            headers.append(("X-Tone-Name", name))
        headers += extra or []
        return self.post_render(headers, data, path="/api/render" + query)

    def rendered(self, response: Response) -> str:
        self.assertEqual(response.status, 200, response.body)
        payload = response.json()
        self.assertEqual(sorted(payload), ["applied_gain_db", "duration", "measured_lufs", "result_id"])
        self.assertRegex(payload["result_id"], r"^[0-9a-f]{32}$")
        return str(payload["result_id"])

    def chain(self, run: list[str]) -> str:
        return run[run.index("-af") + 1]


class RenderFlowTest(RenderFlowTestCase):
    def test_render_and_download(self) -> None:
        name = "My Tone \U0001F3B5"
        response = self.render("?fade_in=0.5&fade_out=0.25&normalize=true&target_lufs=-11", quote(name, safe=""))
        result_id = self.rendered(response)
        self.assertEqual(response.headers.get("Content-Type"), "application/json")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertSecurityHeaders(response)
        # The stub's upload measures -23 LUFS, so the gain is 12 dB.
        self.assertEqual(response.json(), {
            "result_id": result_id, "duration": 1.0, "measured_lufs": -11.0, "applied_gain_db": 12.0,
        })
        measure, render_run = self.runs()
        self.assertNotIn("-y", measure)
        self.assertEqual(
            self.chain(render_run),
            "volume=12.00dB,alimiter=limit=0.8414:level=false:latency=true,afade=t=in:st=0:d=0.500:curve=qsin,"
            "afade=t=out:st=0.750:d=0.250:curve=qsin,ebur128=peak=true",
        )
        self.assertEqual(render_run[render_run.index("-metadata") + 1], f"title={name}")

        tone = self.request("GET", f"/api/result/{result_id}.m4a")
        self.assertEqual(tone.status, 200)
        self.assertTrue(tone.body.startswith(STUB_MAGIC))
        self.assertEqual(json.loads(tone.body[len(STUB_MAGIC):])["title"], name)
        self.assertEqual(tone.headers.get_all("Content-Type"), ["audio/mp4"])
        self.assertEqual(tone.headers.get_all("Cache-Control"), ["no-store"])
        self.assertEqual(tone.headers.get_all("Content-Disposition"), [content_disposition(name, "m4a")])
        self.assertSecurityHeaders(tone)
        ringtone = self.request("GET", f"/api/result/{result_id}.m4r")
        self.assertEqual(ringtone.status, 200)
        self.assertEqual(ringtone.body, tone.body)
        self.assertEqual(ringtone.headers.get_all("Content-Type"), ["audio/x-m4r"])
        self.assertEqual(ringtone.headers.get_all("Content-Disposition"), [content_disposition(name, "m4r")])
        # A download never deletes: the result lives its whole TTL.
        self.assertEqual(self.request("GET", f"/api/result/{result_id}.m4a").body, tone.body)

        # Only the result remains, under its ID; the name is in no path.
        self.wait_for_log("GET /api/result/", count=3)
        self.assertEqual(self.scratch_contents(), [f"results/{result_id}", f"results/{result_id}/tone.m4a"])
        log = self.log.getvalue()
        for secret in (result_id, "Tone", "%F0", "fade", "target", "-11"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, log)
        self.assertIn("GET /api/result/<id>.m4a 200 ", log)
        self.assertIn("GET /api/result/<id>.m4r 200 ", log)
        self.assertIn("POST /api/render 200 ", log)

    def test_defaults(self) -> None:
        self.start(clock=self.clock, max_clip_seconds=2, default_target_lufs=-16.0)
        response = self.render(body=self.wav(2.0))
        result_id = self.rendered(response)
        self.assertEqual(response.json()["applied_gain_db"], 7.0)
        self.assertEqual(
            self.chain(self.runs()[1]),
            "volume=7.00dB,alimiter=limit=0.8414:level=false:latency=true,afade=t=in:st=0:d=0.200:curve=qsin,"
            "afade=t=out:st=0.500:d=1.500:curve=qsin,ebur128=peak=true",
        )
        tone = self.request("GET", f"/api/result/{result_id}.m4a")
        self.assertEqual(tone.headers.get("Content-Disposition"), content_disposition("ringtone", "m4a"))

    def test_json_rounding(self) -> None:
        # 44123 frames is 1.000521 s; a -14.333 target gives a gain of 8.667 dB.
        self.start(clock=self.clock, max_clip_seconds=2)
        response = self.render("?target_lufs=-14.333", body=silent_bytes(44123))
        self.rendered(response)
        payload = response.json()
        self.assertEqual(payload["duration"], 1.001)
        self.assertEqual(payload["applied_gain_db"], 8.67)
        # The stub prints one decimal, as ffmpeg does.
        self.assertEqual(payload["measured_lufs"], -14.3)
        self.assertIn(b'"applied_gain_db": 8.67}', response.body)

    def test_normalize_false_skips_the_measure_pass(self) -> None:
        response = self.render("?normalize=false")
        self.rendered(response)
        self.assertEqual(response.json()["applied_gain_db"], 0.0)
        self.assertEqual(response.json()["measured_lufs"], -23.0)
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue(self.chain(self.runs()[0]).startswith("volume=0.00dB,"))

    def test_a_missed_target_gets_one_corrective_pass(self) -> None:
        self.stub_mode("offset")
        response = self.render()
        self.rendered(response)
        self.assertEqual(response.json()["applied_gain_db"], 10.0)
        self.assertEqual(response.json()["measured_lufs"], -14.0)
        self.assertEqual(len(self.runs()), 3)

    def test_the_name_never_becomes_a_path(self) -> None:
        result_id = self.rendered(self.render(name=quote("../../outside", safe="")))
        self.assertEqual(self.scratch_contents(), [f"results/{result_id}", f"results/{result_id}/tone.m4a"])
        self.assertEqual(sorted(p.name for p in self.temp.iterdir()), ["runs.jsonl", "scratch", "upload.wav"])
        tone = self.request("GET", f"/api/result/{result_id}.m4a")
        self.assertEqual(tone.headers.get("Content-Disposition"), content_disposition("outside", "m4a"))

    def test_statelessness(self) -> None:
        result_id = self.rendered(self.render(name="Secret"))
        self.assertEqual(self.request("GET", f"/api/result/{result_id}.m4r").status, 200)
        # After render and download only the result remains.
        self.assertEqual(self.scratch_contents(), [f"results/{result_id}", f"results/{result_id}/tone.m4a"])
        self.clock.advance(self.settings.result_ttl_seconds)
        # Refused the moment the TTL is up, before any sweep.
        self.assertJsonError(self.request("GET", f"/api/result/{result_id}.m4a"), 404, "not_found")
        self.server.results.sweep()
        # After the TTL sweep scratch holds nothing.
        self.assertScratchEmpty()

    def test_a_restart_forgets_every_result(self) -> None:
        result_id = self.rendered(self.render())
        self.start(clock=self.clock)
        self.assertScratchEmpty()
        self.assertJsonError(self.request("GET", f"/api/result/{result_id}.m4a"), 404, "not_found")


class RenderParamsTest(RenderFlowTestCase):
    BAD_QUERIES = [
        "?fade_in=5.01", "?fade_in=-0.1", "?fade_out=6", "?fade_out=-1",
        "?target_lufs=-8.9", "?target_lufs=-25", "?target_lufs=0",
        "?fade_in=1e0", "?fade_in=.5", "?fade_in=0.5.", "?fade_in=1.", "?fade_in=+1", "?fade_in=0x1",
        "?target_lufs=nan", "?target_lufs=-inf", "?target_lufs=%2D14", "?fade%5Fin=1", "?fade_in=1+",
        "?fade_in=" + "0" * 20 + "1",
        "?fade_in=", "?fade_in", "?fade_in=1&fade_in=1", "?fade_in=1&", "?&fade_in=1", "?fade_in=1&&fade_out=1",
        "?fade_in=1;fade_out=1", "?fade_in=1=2",
        "?normalize=TRUE", "?normalize=1", "?normalize=yes", "?normalize=",
        "?volume=3", "?name=x", "?FADE_IN=1",
        "?a=1&b=2&c=3&d=4&e=5&f=6&g=7&h=8&i=9",
    ]

    def test_bad_params_are_refused_before_the_rate_limit(self) -> None:
        self.start(clock=self.clock, rate_limit_renders_per_minute=1)
        for query in self.BAD_QUERIES:
            with self.subTest(query=query):
                self.assertJsonError(self.render(query), 400, "bad_params")
                self.assertScratchEmpty()
        self.assertEqual(self.runs(), [])
        # None of them used the client's one render.
        self.rendered(self.render())

    def test_boundaries_are_accepted(self) -> None:
        for query in ("?fade_in=0&fade_out=0", "?fade_in=5&fade_out=5.0", "?target_lufs=-24", "?target_lufs=-9",
                      "?target_lufs=-9.0", "?normalize=true", "?fade_in=0001.250", ""):
            with self.subTest(query=query):
                self.rendered(self.render(query))

    def test_params_after_the_length_checks(self) -> None:
        # Origin, type and length come first (docs/PLAN.md 5.2).
        response = self.post_render([("Content-Type", "audio/wav"), ("Content-Length", "4")],
                                    path="/api/render?fade_in=99")
        self.assertJsonError(response, 403, "bad_origin")
        headers = [("Origin", ORIGIN), ("Content-Type", "audio/wav"), ("Content-Length", "99999999")]
        self.assertJsonError(self.post_render(headers, path="/api/render?fade_in=99"), 413, "too_large")

    def test_bad_names_are_refused_before_the_rate_limit(self) -> None:
        self.start(clock=self.clock, rate_limit_renders_per_minute=1)
        cases = [
            [("X-Tone-Name", "a"), ("X-Tone-Name", "b")],
            [("X-Tone-Name", "%FF")],
            [("X-Tone-Name", "%E2%82")],
            [("X-Tone-Name", "a b")],
            [("X-Tone-Name", "caf\xe9")],
            [("X-Tone-Name", "x" * 513)],
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                self.assertJsonError(self.render(extra=extra), 400, "bad_name")
                self.assertScratchEmpty()
        self.assertEqual(self.runs(), [])
        self.rendered(self.render(name="x" * 512))


class RenderInputTest(RenderFlowTestCase):
    def test_every_corpus_case_is_refused_and_leaves_nothing(self) -> None:
        self.start(clock=self.clock, max_clip_seconds=CORPUS_MAX_CLIP_SECONDS)
        corpus = self.temp / "corpus"
        corpus.mkdir()
        for case, (path, code) in corpus_cases(corpus).items():
            with self.subTest(case=case):
                response = self.render(name="SecretName", body=path.read_bytes())
                if code == "too_long":
                    self.assertBadWav(response, "too_long", status=413, code="too_large")
                else:
                    self.assertBadWav(response, code)
                self.assertScratchEmpty()
        # Nothing ever reached ffmpeg.
        self.assertEqual(self.runs(), [])
        self.assertEqual(len(self.server.results), 0)
        self.assertNotIn("SecretName", self.log.getvalue())

    def test_an_oversized_body_is_refused_on_its_length(self) -> None:
        length = len(oversized_body(self.settings.max_body_bytes))
        headers = [("Origin", ORIGIN), ("Content-Type", "audio/wav"), ("Content-Length", str(length))]
        self.assertJsonError(self.post_render(headers), 413, "too_large")
        self.assertScratchEmpty()

    def test_a_body_cut_off_mid_stream(self) -> None:
        sent, declared = cut_off_body()
        data = build_request("POST", "/api/render", self.render_headers(declared), sent)
        response = exchange(self.port, data, half_close=True)
        assert response is not None
        self.assertJsonError(response, 400, "incomplete_body")
        self.assertEqual(self.runs(), [])
        self.assertScratchEmpty()


class RenderFailureTest(RenderFlowTestCase):
    def assertFailed(self, response: Response, status: int, code: str) -> None:
        self.assertJsonError(response, status, code)
        self.wait_for_log(f"POST /api/render {status}")
        self.assertScratchEmpty()
        self.assertEqual(len(self.server.results), 0)
        self.assertNotIn("SecretName", self.log.getvalue())
        self.assertNotIn("Traceback", self.log.getvalue())

    def test_ffmpeg_failure(self) -> None:
        self.stub_mode("fail")
        for query in ("", "?normalize=false"):
            with self.subTest(query=query):
                self.assertFailed(self.render(query, name="SecretName"), 500, "render_failed")

    def test_a_non_zero_exit_fails_even_with_a_valid_summary(self) -> None:
        self.stub_mode("late_fail")
        for query in ("", "?normalize=false"):
            with self.subTest(query=query):
                self.assertFailed(self.render(query, name="SecretName"), 500, "render_failed")

    def test_unparsable_measurement_is_a_render_failure(self) -> None:
        self.stub_mode("unparsable")
        for query in ("", "?normalize=false"):
            with self.subTest(query=query):
                self.assertFailed(self.render(query, name="SecretName"), 500, "render_failed")

    def test_timeout(self) -> None:
        self.stub_mode("hang")
        self.start(clock=self.clock, render_timeout_seconds=1)
        started = time.monotonic()
        self.assertFailed(self.render(name="SecretName"), 504, "render_timeout")
        self.assertLess(time.monotonic() - started, 10)

    def test_missing_ffmpeg(self) -> None:
        # What a checkout on Windows does with the production renderer.
        self.start(clock=self.clock, renderer=Renderer((str(self.temp / "no-ffmpeg"),), 20))
        self.assertFailed(self.render(name="SecretName"), 500, "render_failed")

    def test_the_slot_is_free_after_a_failure(self) -> None:
        self.stub_mode("fail")
        self.start(clock=self.clock, render_concurrency=1)
        for _ in range(3):
            self.assertEqual(self.render().status, 500)
        self.stub_mode("ok")
        self.rendered(self.render())


class ResultsFullTest(RenderFlowTestCase):
    def test_full_store_is_refused_before_the_slot(self) -> None:
        self.start(clock=self.clock, max_results=1, rate_limit_renders_per_minute=2)
        self.rendered(self.render())
        self.assertEqual(len(self.runs()), 2)
        for _ in range(3):
            response = self.render()
            self.assertJsonError(response, 503, "results_full")
            self.assertEqual(response.headers.get("Retry-After"), "10")
        # No upload was rendered and no budget was used.
        self.assertEqual(len(self.runs()), 2)
        self.clock.advance(self.settings.result_ttl_seconds)
        self.rendered(self.render())
        # The rate limit is still asked first.
        self.assertJsonError(self.render(), 429, "rate_limited")

    def test_store_filled_during_the_render(self) -> None:
        self.start(clock=self.clock, max_results=1)
        first = self.rendered(self.render())
        # Another render took the last place after this one passed the check.
        with mock.patch.object(self.server.results, "is_full", return_value=False):
            response = self.render(name="SecretName")
        self.assertJsonError(response, 503, "results_full")
        self.assertEqual(response.headers.get("Retry-After"), "10")
        self.assertEqual(len(self.runs()), 4)
        self.wait_for_log("POST /api/render 503")
        # The refused render left nothing: its job directory and output are gone.
        self.assertEqual(self.scratch_contents(), [f"results/{first}", f"results/{first}/tone.m4a"])


class ResultRouteTest(RenderFlowTestCase):
    def test_unknown_malformed_and_tricky_ids_are_not_found(self) -> None:
        result_id = self.rendered(self.render())
        paths = [
            f"/api/result/{'0' * 32}.m4a",
            f"/api/result/{result_id.upper()}.m4a",
            f"/api/result/{result_id[:-1]}.m4a",
            f"/api/result/{result_id}0.m4a",
            f"/api/result/{result_id}.mp3",
            f"/api/result/{result_id}.M4A",
            f"/api/result/{result_id}.m4a/",
            f"/api/result/{result_id}.m4a.m4a",
            f"/api/result/{result_id}",
            f"/api/result/{result_id}/tone.m4a",
            f"/api/result/../{result_id}.m4a",
            f"/api/result/%2e%2e/{result_id}.m4a",
            f"/api/result/{result_id}%2Em4a",
            f"/api/result//{result_id}.m4a",
            f"//api/result/{result_id}.m4a",
            f"/api/results/{result_id}.m4a",
            f"/API/result/{result_id}.m4a",
            "/api/result/",
            "/api/result",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertJsonError(self.request("GET", path), 404, "not_found")
        self.assertEqual(self.request("GET", f"/api/result/{result_id}.m4a?download=1").status, 200)

    def test_a_file_swept_between_lookup_and_read_is_not_found(self) -> None:
        result_id = self.rendered(self.render())
        result = self.server.results.get(result_id)
        assert result is not None
        result.path.unlink()
        self.assertJsonError(self.request("GET", f"/api/result/{result_id}.m4a"), 404, "not_found")
        # Any read failure, not only a missing file, is a 404 rather than a
        # dropped connection.
        result.path.mkdir()
        self.assertJsonError(self.request("GET", f"/api/result/{result_id}.m4r"), 404, "not_found")
        self.assertNotIn("error while handling a request", self.log.getvalue())

    def test_other_methods(self) -> None:
        result_id = self.rendered(self.render())
        for method, extension in (("POST", "m4a"), ("PUT", "m4r"), ("DELETE", "m4a"), ("OPTIONS", "m4a")):
            with self.subTest(method=method):
                response = self.request(method, f"/api/result/{result_id}.{extension}", [("Content-Length", "0")])
                self.assertJsonError(response, 405, "method_not_allowed")
                self.assertEqual(response.headers.get("Allow"), "GET")
        # The same answer for an ID that never existed.
        response = self.request("POST", f"/api/result/{'0' * 32}.m4a", [("Content-Length", "0")])
        self.assertEqual((response.status, response.headers.get("Allow")), (405, "GET"))
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            sock.sendall(build_request("HEAD", f"/api/result/{result_id}.m4a"))
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 405 "))
        self.assertIn(b"\r\nAllow: GET", head)
        self.assertEqual(rest, b"")
        # The result is untouched by any of it.
        self.assertEqual(self.request("GET", f"/api/result/{result_id}.m4a").status, 200)
        self.wait_for_log("GET /api/result/", count=1)
        self.assertNotIn(result_id, self.log.getvalue())

    def test_only_the_exact_route_shape_is_a_result_route(self) -> None:
        # A route-level check, not only the store's: a path that is not 32
        # lowercase hex digits and .m4a or .m4r is no result route at all, so a
        # wrong method gets 404, never the 405 Allow: GET of a real route.
        hex_id = "0123456789abcdef" * 2
        mixed_case = hex_id[:16] + hex_id[16:].upper()
        for path in (f"/api/result/{hex_id[:-1]}.m4a", f"/api/result/{hex_id}0.m4a",
                     f"/api/result/{hex_id}.wav", f"/api/result/{hex_id}.mp4",
                     f"/api/result/{hex_id.upper()}.m4a", f"/api/result/{mixed_case}.m4r"):
            with self.subTest(path=path):
                response = self.request("POST", path, [("Content-Length", "0")])
                self.assertJsonError(response, 404, "not_found")
                self.assertIsNone(response.headers.get("Allow"))
                with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                    sock.sendall(build_request("HEAD", path))
                    data = b""
                    while chunk := sock.recv(65536):
                        data += chunk
                head = data.partition(b"\r\n\r\n")[0]
                self.assertTrue(head.startswith(b"HTTP/1.0 404 "), head)
                self.assertNotIn(b"\r\nAllow:", head)
        for extension in ("m4a", "m4r"):
            with self.subTest(extension=extension):
                response = self.request("POST", f"/api/result/{hex_id}.{extension}", [("Content-Length", "0")])
                self.assertEqual((response.status, response.headers.get("Allow")), (405, "GET"))

    def test_log_hides_ids_in_any_result_path(self) -> None:
        probe_id = "0123456789abcdef0123456789abcdef"
        self.request("GET", f"/api/result/{probe_id}.m4a?x=1")
        self.request("GET", f"/api/result/{probe_id.upper()}{probe_id}.mp3")
        # A live ID sent to any other path is still a download token, so it is
        # masked whatever route the request missed.
        for other in (f"/API/result/{probe_id}.m4a", f"/api/results/{probe_id}.m4a",
                      f"/static/{probe_id}.m4a", f"/{probe_id}"):
            self.request("GET", other)
        self.wait_for_log("GET /api/result/", count=2)
        self.wait_for_log("GET /", count=6)
        self.assertNotIn(probe_id, self.log.getvalue().lower())
        self.assertIn("GET /api/result/<id>.m4a 404 ", self.log.getvalue())
        self.assertIn("GET /API/result/<id>.m4a 404 ", self.log.getvalue())
        self.assertIn("GET /<id> 404 ", self.log.getvalue())


class ServerWiringTest(unittest.TestCase):
    def test_default_renderer_and_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            settings = Settings(public_origin=ORIGIN, scratch_dir=Path(temp) / "scratch", render_timeout_seconds=7,
                                result_ttl_seconds=60, max_results=3)
            server = RingtoneServer(("127.0.0.1", 0), settings)
            try:
                # The image's ffmpeg at its fixed path, behind prlimit on Linux.
                self.assertEqual(server.renderer.command, (render_module.FFMPEG,))
                self.assertEqual(server.renderer.prlimit, render_module.default_prlimit())
                self.assertEqual(server.renderer.timeout_seconds, 7)
                self.assertEqual(server.results.directory, settings.scratch_dir / "results")
                self.assertEqual((server.results.ttl_seconds, server.results.max_results), (60, 3))
                self.assertTrue(server.results.directory.is_dir())
                self.assertIs(server.render_hook, server_module.render_and_store)
            finally:
                server.server_close()

    def test_closing_stops_the_sweeper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server = RingtoneServer(("127.0.0.1", 0), Settings(public_origin=ORIGIN, scratch_dir=Path(temp) / "s"))
            thread = server.results._thread
            assert thread is not None
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)
            server.server_close()
            self.assertFalse(thread.is_alive())

    def test_query_parsing_is_strict_and_bounded(self) -> None:
        with mock.patch.object(server_module, "parse_qs", wraps=server_module.parse_qs) as spy:
            server_module.parse_render_request("/api/render?fade_in=1", http.client.HTTPMessage(),
                                               Settings(public_origin=ORIGIN))
        self.assertEqual(spy.call_args.kwargs,
                         {"keep_blank_values": True, "strict_parsing": True, "max_num_fields": 8})


class ParseRenderRequestTest(unittest.TestCase):
    """The pure parser, which the hook relies on for every option."""

    def parse(self, path: str, names: list[str] | None = None) -> server_module.RenderRequest:
        headers = http.client.HTTPMessage()
        for value in names or []:
            headers["X-Tone-Name"] = value
        return server_module.parse_render_request(path, headers, Settings(public_origin=ORIGIN))

    def test_defaults(self) -> None:
        self.assertEqual(self.parse("/api/render"), server_module.RenderRequest(
            fade_in=0.2, fade_out=1.5, normalize=True, target_lufs=-14.0, name="ringtone"))

    def test_values(self) -> None:
        self.assertEqual(
            self.parse("/api/render?target_lufs=-9.5&normalize=false&fade_out=0&fade_in=5", ["Caf%C3%A9"]),
            server_module.RenderRequest(fade_in=5.0, fade_out=0.0, normalize=False, target_lufs=-9.5, name="Café"),
        )

    def test_refusals(self) -> None:
        for query in RenderParamsTest.BAD_QUERIES:
            with self.subTest(query=query), self.assertRaises(GuardError) as raised:
                self.parse("/api/render" + query)
            self.assertEqual((raised.exception.status, raised.exception.code), (400, "bad_params"))
        with self.assertRaises(GuardError) as raised:
            self.parse("/api/render", ["a", "b"])
        self.assertEqual((raised.exception.status, raised.exception.code), (400, "bad_name"))


class MainTest(unittest.TestCase):
    CONTRACT = ("PUBLIC_ORIGIN", "TRUSTED_PROXIES", "MAX_CLIP_SECONDS", "DEFAULT_TARGET_LUFS",
                "RENDER_TIMEOUT_SECONDS", "RENDER_CONCURRENCY", "RATE_LIMIT_RENDERS_PER_MINUTE",
                "RESULT_TTL_SECONDS", "MAX_RESULTS", "FETCH_ENABLED", "SOURCE_URL", "VERSION")

    def run_main(self, **variables: str) -> subprocess.CompletedProcess[str]:
        # Keep the rest of the environment: Windows needs SYSTEMROOT to start Python.
        env = {name: value for name, value in os.environ.items() if name not in self.CONTRACT}
        env.update(variables)
        # The documented command, -I included: an exit code of 2 shows that the
        # package was found and main() ran. Every case here is a configuration
        # that must refuse to start, so the server never binds. --bind keeps it
        # on loopback if one ever didn't.
        return subprocess.run(
            [sys.executable, "-I", str(helpers.CORE), "--bind", "127.0.0.1"],
            cwd=helpers.ROOT, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_docs_name_the_command_run_here(self) -> None:
        # Under -I a checkout can't run `python -I -m strange_ringtone` (see
        # core/__main__.py), so the README and the plan must name the form
        # that these tests run, or a reader follows a command that never starts.
        readme = (helpers.ROOT / "README.md").read_text(encoding="utf-8")
        plan = (helpers.ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
        self.assertIn("python -I core --bind 127.0.0.1", readme)
        self.assertIn("python -I core [--bind HOST]", plan)

    def test_missing_public_origin_refuses_to_start(self) -> None:
        result = self.run_main()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("PUBLIC_ORIGIN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_public_origin_refuses_to_start(self) -> None:
        result = self.run_main(PUBLIC_ORIGIN="not an origin")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("PUBLIC_ORIGIN", result.stderr)

    def test_invalid_optional_setting_refuses_to_start(self) -> None:
        result = self.run_main(PUBLIC_ORIGIN=ORIGIN, MAX_CLIP_SECONDS="41")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("MAX_CLIP_SECONDS", result.stderr)


class EntryPointTest(unittest.TestCase):
    """main() up to the point where it would serve, with RingtoneServer replaced."""

    def setUp(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        self.addresses: list[tuple[str, int]] = []
        self.error: BaseException | None = None
        test = self

        class FakeServer:
            def __init__(self, address: tuple[str, int], settings: Settings) -> None:
                test.addresses.append(address)
                if test.error is not None:
                    raise test.error

            def serve_forever(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        env = {name: value for name, value in os.environ.items() if name not in MainTest.CONTRACT}
        env["PUBLIC_ORIGIN"] = ORIGIN
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(entry, "RingtoneServer", FakeServer)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                code = entry.main(argv)
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return code, stderr.getvalue()

    def test_contract_port(self) -> None:
        # Part of the deployment contract: changing it is a breaking change.
        self.assertEqual(entry.PORT, 8080)

    def test_default_bind_is_all_addresses_on_8080(self) -> None:
        # The container passes no --bind. Only the address tuple is checked;
        # nothing is bound.
        code, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(self.addresses, [("0.0.0.0", 8080)])

    def test_bind_takes_ip_literals(self) -> None:
        for value, expected in (("127.0.0.1", "127.0.0.1"), ("::1", "::1"), ("0:0::1", "::1")):
            with self.subTest(value=value):
                self.addresses.clear()
                code, _ = self.run_main(["--bind", value])
                self.assertEqual(code, 0)
                self.assertEqual(self.addresses, [(expected, 8080)])

    def test_bind_refuses_host_names_before_any_lookup(self) -> None:
        # A host name would reach getaddrinfo, a DNS query the core never makes.
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("getaddrinfo called")):
            for value in ("localhost", "nonexistent.example.invalid", "127.0.0.1 ", "fe80::1%eth0", "", "1"):
                with self.subTest(value=value):
                    code, stderr = self.run_main(["--bind", value])
                    self.assertEqual(code, 2)
                    self.assertIn("--bind", stderr)
                    self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.addresses, [])

    def test_startup_failures_refuse_cleanly(self) -> None:
        # The scratch checks raise RuntimeError, a busy port OSError and a
        # broken page template ValueError: each is one line and exit code 1.
        for error in (RuntimeError("scratch directory is owned by another user"),
                      OSError("address in use"), ValueError("bad template")):
            with self.subTest(error=type(error).__name__):
                self.error = error
                code, stderr = self.run_main(["--bind", "127.0.0.1"])
                self.assertEqual(code, 1)
                self.assertIn("strange_ringtone: cannot start: ", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_unusable_scratch_directory_refuses_cleanly(self) -> None:
        # The real server this time, with the default scratch path moved under
        # a directory that doesn't exist, as in a container without the tmpfs.
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "no-tmpfs" / "strange_ringtone"
            with (
                mock.patch.object(entry, "RingtoneServer", RingtoneServer),
                mock.patch.object(config, "_WINDOWS", False),
                mock.patch.object(config, "SCRATCH_DIR", missing),
            ):
                code, stderr = self.run_main(["--bind", "127.0.0.1"])
            self.assertFalse(missing.parent.exists())
        self.assertEqual(code, 1)
        self.assertIn("cannot create the scratch directory", stderr)
        self.assertNotIn("Traceback", stderr)


class SigtermTest(unittest.TestCase):
    """As PID 1 in a container, a process with no SIGTERM handler ignores the signal."""

    def setUp(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)

    def test_sigterm_unwinds_serve_forever_and_closes_the_server(self) -> None:
        closed: list[bool] = []

        class FakeServer:
            def __init__(self, address: tuple[str, int], settings: Settings) -> None:
                pass

            def serve_forever(self) -> None:
                # What the interpreter does when the signal arrives.
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler), handler
                handler(signal.SIGTERM, None)

            def server_close(self) -> None:
                closed.append(True)

        env = {name: value for name, value in os.environ.items() if name not in MainTest.CONTRACT}
        env["PUBLIC_ORIGIN"] = ORIGIN
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(entry, "RingtoneServer", FakeServer), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                entry.main(["--bind", "127.0.0.1"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(closed, [True])

    @unittest.skipIf(sys.platform == "win32", "Windows has no catchable SIGTERM between processes")
    def test_sigterm_stops_a_running_server_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = {name: value for name, value in os.environ.items() if name not in MainTest.CONTRACT}
            env.update(PUBLIC_ORIGIN=ORIGIN)
            # Port 0 instead of the contract's 8080, so a busy port or a second
            # test run can't collide, and a scratch directory of the test's own
            # instead of the shared /tmp one; everything else is the real main().
            code = ("import pathlib, sys; sys.path.insert(0, sys.argv[1]); "
                    "from strange_ringtone import __main__ as entry, config; entry.PORT = 0; "
                    "config.SCRATCH_DIR = pathlib.Path(sys.argv[2]); "
                    "sys.exit(entry.main(['--bind', '127.0.0.1']))")
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", code, str(helpers.CORE), str(Path(temp) / "scratch")],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            try:
                assert process.stderr is not None
                line = process.stderr.readline()
                self.assertIn("listening on 127.0.0.1:", line)
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=DEADLINE), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stderr is not None:
                    process.stderr.close()


class ServerBindTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "SO_REUSEADDR is only unsafe on Windows")
    def test_no_address_reuse_on_windows(self) -> None:
        # There SO_REUSEADDR lets a second process bind the same port and take
        # over its connections.
        self.assertIs(RingtoneServer.allow_reuse_address, False)
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(public_origin=ORIGIN, scratch_dir=Path(temp) / "scratch")
            server = RingtoneServer(("127.0.0.1", 0), settings)
            try:
                self.assertEqual(server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
            finally:
                server.server_close()

    @unittest.skipIf(sys.platform == "win32", "SO_REUSEADDR is only unsafe on Windows")
    def test_address_reuse_elsewhere(self) -> None:
        # On Linux it only lets a restarted container bind again at once.
        self.assertIs(RingtoneServer.allow_reuse_address, True)

    def test_no_reverse_dns_lookup_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(public_origin=ORIGIN, scratch_dir=Path(temp) / "scratch")
            with mock.patch("socket.getfqdn", side_effect=AssertionError("getfqdn called")):
                server = RingtoneServer(("127.0.0.1", 0), settings)
            try:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertEqual(server.server_port, server.server_address[1])
            finally:
                server.server_close()

    def test_ipv6_literal_binds_an_ipv6_socket(self) -> None:
        # --bind takes IPv6 literals too, and those need an AF_INET6 socket.
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("no IPv6 loopback here")
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            settings = Settings(public_origin=ORIGIN, scratch_dir=Path(temp) / "scratch")
            server = _TestServer(("::1", 0), settings)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
                connection = http.client.HTTPConnection("::1", server.server_address[1], timeout=DEADLINE)
                try:
                    connection.request("GET", "/healthz")
                    response = connection.getresponse()
                    self.assertEqual((response.status, response.read()), (200, b""))
                finally:
                    connection.close()
            finally:
                ServerTestCase._stop(server, thread)

    def test_a_stalled_connection_does_not_delay_stopping(self) -> None:
        # Handler threads are daemons and server_close() does not join them:
        # a client that sent half a request (its worker waits up to the 30 s
        # socket timeout) must not hold up a container that was told to stop.
        code = (
            "import pathlib, socket, sys, threading, time; sys.path.insert(0, sys.argv[1]); "
            "from strange_ringtone.config import Settings; "
            "from strange_ringtone.server import RingtoneServer; "
            "settings = Settings(public_origin=sys.argv[3], scratch_dir=pathlib.Path(sys.argv[2])); "
            "server = RingtoneServer(('127.0.0.1', 0), settings); "
            "threading.Thread(target=server.serve_forever, daemon=True).start(); "
            "before = threading.active_count(); "
            "stalled = socket.create_connection(server.server_address[:2]); "
            "stalled.sendall(b'GET /healthz HTTP/1.1\\r\\n'); "
            "deadline = time.monotonic() + 5\n"
            "while threading.active_count() == before and time.monotonic() < deadline: time.sleep(0.01)\n"
            "assert threading.active_count() > before, 'no worker started'; "
            "server.shutdown(); started = time.monotonic(); server.server_close(); "
            "print('closed in %.3f' % (time.monotonic() - started), flush=True)"
        )
        with tempfile.TemporaryDirectory() as temp:
            started = time.monotonic()
            try:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", code, str(helpers.CORE), str(Path(temp) / "scratch"), ORIGIN],
                    capture_output=True, text=True, timeout=10,
                )
            except subprocess.TimeoutExpired:
                self.fail("the process waited for the stalled connection's worker")
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.fullmatch(r"closed in ([0-9.]+)\n", result.stdout)
        assert match is not None, result.stdout
        self.assertLess(float(match.group(1)), 2.0)
        self.assertLess(elapsed, 10.0)


if __name__ == "__main__":
    unittest.main()
