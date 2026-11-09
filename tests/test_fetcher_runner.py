"""The fetcher's runner (strange_ringtone_runner): yt-dlp's own container (docs/PLAN.md 6.1).

The runner takes one validated video ID at a time from the fetcher over the
internal `jobs` network, runs yt-dlp, answers only the whitelisted metadata,
and hands the audio back once. These tests run its settings, its request
parser, its entry point and its HTTP server on loopback, port 0, with the stub
yt-dlp (tests/stubs/ytdlp_stub.py) and a fake clock: no network, no DNS and
no yt-dlp, on Windows and Linux alike. Each test names the check it guards
(R1 to R4, S2, I1).
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import http.client
import io
import json
import os
import re
import secrets
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

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone.http_guard import GuardError
from strange_ringtone_fetch import config as fetch_config
from strange_ringtone_fetch.metadata import ERROR_CODES, parse_info
from strange_ringtone_runner import __main__ as runner_entry
from strange_ringtone_runner import config as runner_config
from strange_ringtone_runner import server as runner_server
from strange_ringtone_runner import ytdlp as ytdlp_module
from strange_ringtone_runner.config import RUNNER_VARIABLES, ConfigError, Settings
from strange_ringtone_runner.server import (
    COLLECT_DEADLINE_SECONDS,
    MAX_BODY_BYTES,
    RunnerServer,
    parse_job_request,
)
from strange_ringtone_runner.ytdlp import DEFAULT_COMMAND, YtDlp, default_prlimit
from test_config import ReadRecorder
from test_fetcher_jobs import FakeClock
from test_fetcher_server import DEADLINE, LogCapture, Response, build_request, exchange, wait_for
from test_fetcher_ytdlp import LOG_VAR, StubYtDlp, stub_title

VIDEO_ID = "dQw4w9WgXcQ"
OTHER_ID = "abcdefghijk"
MIB = 1024 * 1024
JSON_TYPE = "application/json"
# Found from the imported package, not the checkout's layout, so the same
# tests read the image's copy when tests/image_fetcher runs them there
# (/app/fetcher, where helpers.ROOT holds no fetcher/), as test_fetcher_config
# does for the fetcher's package.
RUNNER_PACKAGE = Path(runner_server.__file__).resolve().parent
RUNNER_SCRIPT = RUNNER_PACKAGE.parent / "runner.py"
# R4: method, path, status (or -), duration; nothing else, printable ASCII only.
LOG_LINE_RE = re.compile(r"^[!-~]+ [!-~]+ (?:[0-9]{3}|-) [0-9]+\.[0-9]{3}$")
# What the runner's process may import from the project (docs/PLAN.md 6.1):
# the core's parsers and guards and the three fetcher modules it shares,
# never the fetcher's server, jobs or client.
SHARED_MODULES = {
    "strange_ringtone.config",
    "strange_ringtone.http_guard",
    "strange_ringtone_fetch.config",
    "strange_ringtone_fetch.metadata",
    "strange_ringtone_fetch.routes",
}
# The stub's ID table (tests/stubs/ytdlp_stub.py) and the code each must give.
FAILURES = {
    "tooLong____": "too_long",
    "blocked____": "blocked",
    "notFound___": "not_found",
    "failing____": "failed",
    "noAudio____": "failed",
    "notADict___": "failed",
    "oversize___": "failed",
    "symlinked__": "failed",
}


def token() -> str:
    """A job token as the fetcher mints it: 128 random bits, 32 lowercase hex."""
    return secrets.token_hex(16)


def job_body(video_id: str, job: str) -> bytes:
    return json.dumps({"video_id": video_id, "job": job}).encode("ascii")


def exchange_to_close(port: int, data: bytes, half_close: bool = False) -> Response:
    """exchange(), returning only once the runner has closed the connection.

    The runner frees its one-job lock (and, after a stream, deletes the
    audio) before it closes the connection, but a client has the answer's
    last byte before that, so a next request sent at once could find the
    runner busy. The fetcher's client waits for the close
    (strange_ringtone_fetch.runner_client); so do these tests.
    """
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
        # Until the runner's close. A reset is a close too: Windows turns a
        # close over unread request bytes into one.
        with contextlib.suppress(ConnectionError):
            while sock.recv(65536):
                pass
    return Response(response.status, response.headers, body)


def child_env(**values: str) -> dict[str, str]:
    """A minimal environment for a subprocess of this interpreter."""
    env = {"PATH": os.environ.get("PATH", "")}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    env.update(values)
    return env


class _TestRunnerServer(RunnerServer):
    # Join the handler threads on close, so no handler still writes to the
    # scratch directory when the temporary directory is removed.
    block_on_close = True


class _CappedRunnerServer(_TestRunnerServer):
    MAX_WORKERS = 1


# --- settings ----------------------------------------------------------------


def from_env(**values: str) -> Settings:
    return Settings.from_env(values)


class RunnerDefaultsTest(unittest.TestCase):
    def test_contract_defaults(self) -> None:
        settings = from_env()
        self.assertEqual(settings.fetch_max_seconds, 600)
        self.assertEqual(settings.fetch_max_bytes, 41943040)
        self.assertEqual(settings.fetch_timeout_seconds, 120)
        self.assertEqual(settings.proxies, ())
        # S2: the default-route start check is on unless the operator turns it off.
        self.assertIs(settings.allow_default_route, False)
        self.assertEqual(settings.scratch_dir, runner_config._default_scratch_dir())

    def test_direct_construction_uses_the_same_defaults(self) -> None:
        self.assertEqual(Settings(), from_env())

    def test_empty_optional_values_mean_the_default(self) -> None:
        # Written out rather than taken from RUNNER_VARIABLES, so a variable
        # dropped from that list still has to mean its default here.
        names = (
            "FETCH_MAX_SECONDS", "FETCH_MAX_BYTES", "FETCH_TIMEOUT_SECONDS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
            "FETCH_ALLOW_DEFAULT_ROUTE",
            "FETCH_ALLOW_NO_SWEEP",
        )
        self.assertEqual(from_env(**dict.fromkeys(names, "")), Settings())

    def test_allow_default_route_is_parsed_like_the_cores_booleans(self) -> None:
        # S2: the override takes the core's boolean words and nothing else, as
        # the fetcher's does. A value the parser doesn't know refuses to start,
        # so a typo never turns the check off.
        for value in ("true", "1", "yes", "TRUE", "Yes"):
            with self.subTest(value=value):
                self.assertIs(from_env(FETCH_ALLOW_DEFAULT_ROUTE=value).allow_default_route, True)
        for value in ("false", "0", "no", "FALSE"):
            with self.subTest(value=value):
                self.assertIs(from_env(FETCH_ALLOW_DEFAULT_ROUTE=value).allow_default_route, False)
        for value in ("on", "2", " true", "true ", "\u0661", "y"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "FETCH_ALLOW_DEFAULT_ROUTE"):
                from_env(FETCH_ALLOW_DEFAULT_ROUTE=value)
        # The lowercase spelling is not the setting.
        self.assertIs(from_env(fetch_allow_default_route="true").allow_default_route, False)

    def test_reads_exactly_the_runner_variables_and_nothing_browser_facing(self) -> None:
        # R2/S2: the runner serves the fetcher, not a browser, so it never
        # reads PUBLIC_ORIGIN or TRUSTED_PROXIES, and it has no required
        # setting: an empty environment starts it.
        self.assertEqual(RUNNER_VARIABLES, (
            "FETCH_MAX_SECONDS", "FETCH_MAX_BYTES", "FETCH_TIMEOUT_SECONDS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
            "FETCH_ALLOW_DEFAULT_ROUTE",
            "FETCH_ALLOW_NO_SWEEP",
        ))

        class NoIteration(ReadRecorder):
            # Reading every variable by walking the environment would hide
            # what is read from this recorder.
            def __iter__(self) -> Any:
                raise AssertionError("the runner's settings walked the environment")

            def keys(self) -> Any:
                raise AssertionError("the runner's settings walked the environment")

            def items(self) -> Any:
                raise AssertionError("the runner's settings walked the environment")

            def values(self) -> Any:
                raise AssertionError("the runner's settings walked the environment")

        environ = NoIteration({"PUBLIC_ORIGIN": "", "TRUSTED_PROXIES": "0.0.0.0/0"})
        settings = Settings.from_env(environ)
        self.assertEqual(environ.looked_up, set(RUNNER_VARIABLES))
        self.assertNotIn("PUBLIC_ORIGIN", environ.looked_up)
        self.assertNotIn("TRUSTED_PROXIES", environ.looked_up)
        self.assertEqual(settings, Settings())
        self.assertEqual(Settings.from_env({}), Settings())
        self.assertEqual({field.name for field in dataclasses.fields(Settings)}, {
            "fetch_max_seconds", "fetch_max_bytes", "fetch_timeout_seconds", "proxies", "allow_default_route",
            "allow_no_sweep",
            "scratch_dir",
        })

    def test_bounds(self) -> None:
        cases = {
            # The page decodes at most 600 s, so a longer fetch is refused at startup.
            "FETCH_MAX_SECONDS": ("1", "600", "0", "601"),
            "FETCH_MAX_BYTES": ("1048576", "536870912", "1048575", "536870913"),
            "FETCH_TIMEOUT_SECONDS": ("1", "600", "0", "601"),
        }
        attributes = {
            "FETCH_MAX_SECONDS": "fetch_max_seconds",
            "FETCH_MAX_BYTES": "fetch_max_bytes",
            "FETCH_TIMEOUT_SECONDS": "fetch_timeout_seconds",
        }
        for name, (low, high, below, above) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(from_env(**{name: low}), attributes[name]), int(low))
                self.assertEqual(getattr(from_env(**{name: high}), attributes[name]), int(high))
                for bad in (below, above, "-5", "+5", " 5", "5.0", "1e3", "\u0665", "5_0", "x"):
                    with self.subTest(value=bad), self.assertRaisesRegex(ConfigError, name):
                        from_env(**{name: bad})

    def test_scratch_dir_is_fixed_and_differs_from_the_fetchers_and_the_cores(self) -> None:
        # R3: a writable path of the contract, never a setting; distinct from
        # the fetcher's and the core's, because each server empties its own
        # scratch at startup and a development machine runs all three.
        from strange_ringtone import config as core_config

        self.assertEqual(runner_config.SCRATCH_DIR.as_posix(), "/tmp/strange_ringtone_runner")
        with (
            mock.patch.object(runner_config, "_WINDOWS", False),
            mock.patch.object(tempfile, "gettempdir", side_effect=AssertionError("gettempdir called")),
            mock.patch.dict(os.environ, {"TMPDIR": "/srv", "TEMP": "/srv", "TMP": "/srv"}),
        ):
            self.assertEqual(Settings().scratch_dir, runner_config.SCRATCH_DIR)
            self.assertEqual(from_env(SCRATCH_DIR="/srv", TMPDIR="/srv").scratch_dir, runner_config.SCRATCH_DIR)
        for other in (fetch_config, core_config):
            with self.subTest(other=other.__name__):
                self.assertNotEqual(runner_config.SCRATCH_DIR, other.SCRATCH_DIR)
                self.assertNotEqual(runner_config._default_scratch_dir(), other._default_scratch_dir())

    def test_settings_are_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Settings().proxies = (("HTTPS_PROXY", "http://evil.example.invalid:1"),)  # type: ignore[misc]

    def test_the_default_ytdlp_gets_the_settings_proxies_and_prlimit(self) -> None:
        # R2: the yt-dlp the runner builds itself gets the operator's limits
        # and proxies; a deployment that reaches YouTube only through the
        # forwarder would otherwise fail every fetch with no setting wrong.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        proxies = (("HTTPS_PROXY", "http://fetcher-forwarder:3128"),)
        settings = Settings(
            fetch_max_seconds=300, fetch_max_bytes=2 * MIB, fetch_timeout_seconds=45, proxies=proxies,
            scratch_dir=Path(temp.name) / "scratch",
        )
        server = RunnerServer(("127.0.0.1", 0), settings)
        self.addCleanup(server.server_close)
        ytdlp = server.ytdlp
        self.assertIs(type(ytdlp), YtDlp)
        self.assertEqual(ytdlp.proxies, proxies)
        self.assertEqual((ytdlp.max_seconds, ytdlp.max_bytes, ytdlp.timeout_seconds), (300, 2 * MIB, 45))
        self.assertEqual(ytdlp.prlimit, default_prlimit())
        # I1: the image's venv, never the server's own interpreter.
        self.assertEqual(ytdlp.command, DEFAULT_COMMAND)
        env = ytdlp.child_env(Path(temp.name) / "job")
        self.assertEqual(env.get("HTTPS_PROXY"), "http://fetcher-forwarder:3128")
        self.assertNotIn("HTTP_PROXY", env)


class RunnerProxyTest(unittest.TestCase):
    """R2: the proxy variables are the runner's to read, with the fetcher's rules (moved from test_fetcher_config)."""

    def test_http_proxies_are_kept_in_a_fixed_order(self) -> None:
        settings = from_env(NO_PROXY="localhost,.example.invalid,192.0.2.0/24", HTTP_PROXY="http://proxy.example.invalid:3128",
                            HTTPS_PROXY="http://proxy.example.invalid:3128/")
        self.assertEqual(settings.proxies, (
            ("HTTPS_PROXY", "http://proxy.example.invalid:3128/"),
            ("HTTP_PROXY", "http://proxy.example.invalid:3128"),
            ("NO_PROXY", "localhost,.example.invalid,192.0.2.0/24"),
        ))
        # The compose example's value.
        self.assertEqual(from_env(HTTPS_PROXY="http://fetcher-forwarder:3128").proxies,
                         (("HTTPS_PROXY", "http://fetcher-forwarder:3128"),))

    def test_lowercase_variants_are_not_read(self) -> None:
        self.assertEqual(from_env(https_proxy="http://proxy.example.invalid:3128",
                                  http_proxy="http://proxy.example.invalid:3128", no_proxy="localhost").proxies, ())
        # Nor the runner's own process environment: only the mapping given.
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.invalid:3128"}):
            self.assertEqual(from_env().proxies, ())

    def test_bad_proxy_urls_refuse_to_start(self) -> None:
        for value in (
            # Without requests, yt-dlp's urllib handler speaks only to http:// proxies.
            "https://proxy.example.invalid:3128",
            "socks5://proxy.example.invalid:1080",
            "proxy.example.invalid:3128",
            # Credentials would be a secret outside a *_FILE variable.
            "http://user:pass@proxy.example.invalid:3128",
            "http://user@proxy.example.invalid:3128",
            "http://proxy.example.invalid:3128/path",
            "http://proxy.example.invalid:3128?x=1",
            "http://proxy.example.invalid:3128#x",
            "http://proxy.example.invalid:",
            "http://proxy.example.invalid:99999",
            "http://proxy.example.invalid:31a8",
            "http://",
            "http://proxy.example.invalid :3128",
            "http://pröxy.example.invalid:3128",
            "http://proxy.example.invalid:3128\n",
            "http://" + "a" * 1100,
        ):
            for name in ("HTTPS_PROXY", "HTTP_PROXY"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ConfigError, name):
                    from_env(**{name: value})

    def test_bad_no_proxy_refuses_to_start(self) -> None:
        for value in ("a b", "localhost;rm", "x\n", "é", "a" * 1025):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "NO_PROXY"):
                from_env(NO_PROXY=value)

    def test_messages_never_repeat_the_value(self) -> None:
        secret = "s3cr3t-value"
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "FETCH_MAX_BYTES", "FETCH_ALLOW_DEFAULT_ROUTE"):
            # The message check stays inside the subTest: when a name raises
            # nothing, its subTest fails alone instead of erroring on the next line.
            with self.subTest(name=name):
                with self.assertRaises(ConfigError) as raised:
                    from_env(**{name: f"http://user:{secret}@example.invalid x"})
                self.assertNotIn(secret, str(raised.exception))
                self.assertIn(name, str(raised.exception))


# --- the request body ----------------------------------------------------------


class JobRequestTest(unittest.TestCase):
    """R1: a job request is exactly {"video_id": <11 of [A-Za-z0-9_-]>, "job": <32 lowercase hex>}."""

    def assertRefused(self, body: bytes, *codes: str) -> None:
        with self.assertRaises(GuardError) as raised:
            parse_job_request(body)
        self.assertEqual(raised.exception.status, 400)
        self.assertIn(raised.exception.code, codes)

    def test_exactly_video_id_and_job_are_accepted(self) -> None:
        job = token()
        cases = [
            b'{"video_id": "dQw4w9WgXcQ", "job": "%s"}' % job.encode("ascii"),
            b'{"video_id":"dQw4w9WgXcQ","job":"%s"}' % job.encode("ascii"),
            b'{"job": "%s", "video_id": "dQw4w9WgXcQ"}' % job.encode("ascii"),
            b' \r\n\t{\n  "video_id" : "dQw4w9WgXcQ" ,\n  "job" : "%s"\n}\n ' % job.encode("ascii"),
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(parse_job_request(body), (VIDEO_ID, job))
        for video_id in ("a" * 11, "-_-_-_-_-_-", "AZaz09_-AZ0"):
            for other in ("0" * 32, "f" * 32, "0123456789abcdef" * 2):
                with self.subTest(video_id=video_id, job=other):
                    self.assertEqual(parse_job_request(job_body(video_id, other)), (video_id, other))

    def test_anything_else_is_refused(self) -> None:
        job = token()
        # The ID goes into the URL yt-dlp is given, so it is checked again
        # here, as tests/test_fetcher_ytdlp.py's VideoIdTest checks the URL.
        for video_id in (
            "", "a" * 10, "a" * 12, "dQw4w9WgXc.", "..........a", "dQw4w9WgX%2", "dQw4w9WgX/c", "dQw4w9WgX c",
            "dQw4w9WgXc\n", "\ndQw4w9WgXc", "dQw4w9WgX\u0665c", "dQw4w9WgX\uff41c", "dQw4w9WgX\u00e9c",
            "dQw4w9WgXc&", "dQw4w9WgX#c", "dQw4w9WgX?c", "dQw4w9WgX=c", "-" * 10 + "\x00", "https://ww",
            # A URL, however well formed: the runner builds the only URL itself.
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ dQw4w9WgXcQ",
        ):
            with self.subTest(video_id=video_id):
                self.assertRefused(json.dumps({"video_id": video_id, "job": job}).encode("utf-8"), "bad_video_id")
        # A value that is not a string: the spec names both codes (see the
        # stage report), so either 400 is a refusal; never a run.
        for video_id in (None, 12345678901, ["dQw4w9WgXcQ"], {"id": VIDEO_ID}, True):
            with self.subTest(video_id=video_id):
                self.assertRefused(json.dumps({"video_id": video_id, "job": job}).encode("utf-8"),
                                   "bad_json", "bad_video_id")
        for bad in (
            job[:31], job + "0", job.upper(), "A" + job[1:], "g" * 32, " " + job[:31], job[:31] + "\n",
            "\u0661" * 32, "\uff10" * 32, "", "0x" + job[:30], job[:16] + "-" + job[16:31],
        ):
            with self.subTest(job=bad):
                self.assertRefused(job_body(VIDEO_ID, bad), "bad_job")
        for bad_job in (None, 10 ** 31, [job], {"job": job}):
            with self.subTest(job=bad_job):
                self.assertRefused(json.dumps({"video_id": VIDEO_ID, "job": bad_job}).encode("utf-8"),
                                   "bad_json", "bad_job")
        valid = job_body(VIDEO_ID, job)
        for label, body in {
            "missing job": json.dumps({"video_id": VIDEO_ID}).encode("ascii"),
            "missing video_id": json.dumps({"job": job}).encode("ascii"),
            "extra key": json.dumps({"video_id": VIDEO_ID, "job": job, "url": "https://example.invalid/"}).encode(),
            "renamed key": json.dumps({"Video_id": VIDEO_ID, "job": job}).encode("ascii"),
            "duplicate key": b'{"video_id": "%s", "job": "%s", "job": "%s"}' % (
                VIDEO_ID.encode(), job.encode(), job.encode()),
            "duplicate id": b'{"video_id": "%s", "video_id": "abcdefghijk", "job": "%s"}' % (
                VIDEO_ID.encode(), job.encode()),
            "a list": b'["%s", "%s"]' % (VIDEO_ID.encode(), job.encode()),
            "a string": json.dumps(VIDEO_ID).encode("ascii"),
            "null": b"null",
            "a number": b"1",
            "empty": b"",
            "truncated": valid[:-1],
            "trailing data": valid + b"{}",
            "not UTF-8": b'{"video_id": "dQw4w9WgXc\xff", "job": "%s"}' % job.encode(),
            "UTF-16": valid.decode("ascii").encode("utf-16"),
            "deep nesting": b"[" * MAX_BODY_BYTES,
        }.items():
            with self.subTest(label):
                self.assertRefused(body, "bad_json")


# --- the server ----------------------------------------------------------------


class RunnerServerTestCase(unittest.TestCase):
    """A RunnerServer on loopback, port 0, with the stub yt-dlp, a fake clock and stderr captured."""

    server_class: type[RunnerServer] = _TestRunnerServer
    # High, so the sweeper thread never races a test that calls sweep() itself.
    sweep_interval = 3600.0

    def setUp(self) -> None:
        self.log = LogCapture()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        # ignore_cleanup_errors: on Windows a killed run's grandchild may still
        # hold a directory for a moment.
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        # Doesn't exist yet: the server must create it.
        self.scratch = self.temp / "scratch"
        self.runs_log = self.temp / "runs.jsonl"
        self.clock = FakeClock()
        self.closed: list[RunnerServer] = []
        patcher = mock.patch.object(runner_server, "SWEEP_INTERVAL_SECONDS", self.sweep_interval)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start()

    def start(self, timeout: int = 30, max_bytes: int = MIB, server_class: type[RunnerServer] | None = None) -> None:
        self.settings = Settings(scratch_dir=self.scratch, fetch_timeout_seconds=timeout, fetch_max_bytes=max_bytes)
        self.ytdlp = StubYtDlp(600, max_bytes, timeout, log=self.runs_log)
        cls = server_class or self.server_class
        self.server = cls(("127.0.0.1", 0), self.settings, ytdlp=self.ytdlp, clock=self.clock)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, self.server, thread)
        self.port = self.server.server_address[1]

    def _stop(self, server: RunnerServer, thread: threading.Thread) -> None:
        server.shutdown()
        if server not in self.closed:
            server.server_close()
        thread.join(DEADLINE)

    def close_server(self) -> None:
        """Stop serving and close, as the entry point does on SIGTERM."""
        self.server.shutdown()
        self.server.server_close()
        self.closed.append(self.server)

    # --- requests ---------------------------------------------------------------

    def request(self, method: str, path: str, headers: list[tuple[str, str]] | None = None,
                body: bytes = b"", **options: Any) -> Response:
        return exchange_to_close(self.port, build_request(method, path, headers, body), **options)

    def post_body(self, body: bytes, content_type: str = JSON_TYPE) -> Response:
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body)))]
        return self.request("POST", "/jobs", headers, body)

    def post(self, video_id: str = VIDEO_ID, job: str | None = None) -> Response:
        return self.post_body(job_body(video_id, job or token()))

    def post_in_background(self, video_id: str, job: str) -> tuple[threading.Thread, list[Any]]:
        """POST from a thread; the outcome list gets the Response or the exception."""
        outcome: list[Any] = []
        body = job_body(video_id, job)
        data = build_request("POST", "/jobs", [("Content-Type", JSON_TYPE), ("Content-Length", str(len(body)))], body)
        port = self.port

        def run() -> None:
            try:
                outcome.append(exchange_to_close(port, data))
            except Exception as error:  # the connection may drop when the server closes
                outcome.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    def audio(self, job: str) -> Response:
        return self.request("GET", f"/jobs/{job}/audio")

    def done(self, video_id: str = VIDEO_ID, job: str | None = None) -> str:
        """POST a job the stub completes; its token."""
        job = job or token()
        response = self.post(video_id, job)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json()["status"], "done", response.body)
        return job

    # --- what the stub and the server left ------------------------------------

    def runs(self) -> list[dict[str, Any]]:
        if not self.runs_log.exists():
            return []
        return [json.loads(line) for line in self.runs_log.read_text(encoding="utf-8").splitlines()]

    def scratch_contents(self) -> list[str]:
        """Everything under the scratch directory except the results directory itself."""
        names: list[str] = []
        # os.walk ignores a directory that disappears while it walks.
        for directory, dirs, files in os.walk(self.scratch):
            for name in dirs + files:
                names.append((Path(directory) / name).relative_to(self.scratch).as_posix())
        return sorted(name for name in names if name != "results")

    def assertScratchEmpty(self) -> None:
        with contextlib.suppress(AssertionError):
            wait_for(lambda: not self.scratch_contents(), "an empty scratch directory")
        self.assertEqual(self.scratch_contents(), [])

    def wait_for_log(self, prefix: str, count: int = 1) -> list[str]:
        def found() -> bool:
            return len([line for line in self.log.lines() if line.startswith(prefix)]) >= count

        wait_for(found, f"a log line starting {prefix!r}")
        return [line for line in self.log.lines() if line.startswith(prefix)]

    # --- assertions -----------------------------------------------------------

    def assertNoStore(self, response: Response) -> None:
        """R1/R4: what every runner response carries, whatever its status."""
        self.assertEqual(response.headers.get_all("Connection"), ["close"])
        self.assertEqual(response.headers.get_all("Cache-Control"), ["no-store"])
        self.assertEqual(response.headers.get_all("X-Content-Type-Options"), ["nosniff"])
        self.assertEqual(response.headers.get_all("Server"), ["strange_ringtone_runner"])
        self.assertEqual(response.headers.get_all("Content-Length"), [str(len(response.body))])
        self.assertFalse([name for name in response.headers if name.lower().startswith("access-control-")])

    def assertJsonError(self, response: Response, status: int, code: str) -> None:
        self.assertEqual(response.status, status, response.body)
        self.assertEqual(response.headers.get_all("Content-Type"), [JSON_TYPE])
        self.assertEqual(response.json(), {"error": code})
        self.assertNoStore(response)

    def assertBusy(self, response: Response) -> None:
        self.assertJsonError(response, 503, "busy")
        self.assertEqual(response.headers.get_all("Retry-After"), ["5"])

    def assertStubsOutsideFile(self) -> None:
        # The stub's symlink mode plants its target next to the job directory
        # (tests/stubs/ytdlp_stub.py), outside anything the runner made; it
        # is the stub's own file, removed here so the next case starts clean.
        outside = self.scratch / "outside.m4a"
        wait_for(lambda: [n for n in self.scratch_contents() if not n.startswith("job-")] == ["outside.m4a"],
                 "the stub's planted file to be the only thing left")
        outside.unlink()


class RunnerFlowTest(RunnerServerTestCase):
    def test_post_runs_the_stub_and_answers_done_with_info_only(self) -> None:
        # R1 acceptance, and the whitelist on the runner's side (S1): only
        # the three cleaned values, in yt-dlp's names, ever leave the runner.
        job = token()
        # The runner's own environment during the run: a secret and a proxy
        # the settings don't carry. Neither may reach the child (R2).
        planted = {"RUNNER_SECRET": "s3cr3t-env-value", "HTTPS_PROXY": "http://unset.example.invalid:1"}
        with mock.patch.dict(os.environ, planted):
            response = self.post(VIDEO_ID, job)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.headers.get_all("Content-Type"), [JSON_TYPE])
        self.assertNoStore(response)
        self.assertTrue(response.body.isascii(), response.body)
        answer = response.json()
        self.assertEqual(set(answer), {"status", "info"})
        self.assertEqual(answer["status"], "done")
        info = answer["info"]
        self.assertEqual(set(info), {"title", "duration", "heatmap"})
        self.assertEqual(info["title"], stub_title(VIDEO_ID))
        self.assertEqual(info["duration"], 215.5)
        self.assertEqual(len(info["heatmap"]), 100)
        for point in info["heatmap"]:
            self.assertEqual(set(point), {"start_time", "end_time", "value"})
        # The fetcher reads it back with the same whitelist, unchanged.
        self.assertEqual(parse_info(info).title, stub_title(VIDEO_ID))
        for never in ("uploader", "description", "webpage_url", "formats", '"id"', "Stub Uploader", "format_id"):
            self.assertNotIn(never.encode("ascii"), response.body)
        # The stub ran once, on the validated ID, in a fresh job directory
        # under the scratch directory, and never saw the token.
        (run,) = self.runs()
        self.assertEqual(run["argv"][-1], f"https://www.youtube.com/watch?v={VIDEO_ID}")
        self.assertTrue(Path(run["cwd"]).name.startswith("job-"), run["cwd"])
        self.assertEqual(Path(run["cwd"]).parent.name, "scratch")
        self.assertNotIn(job, json.dumps(run))
        self.assertEqual(run["env"].get(LOG_VAR), str(self.runs_log))
        # R2: the child's environment is the one YtDlp builds from scratch
        # (EnvironmentTest), never the runner process's own, through the
        # server too: no secret, and no proxy the settings didn't give.
        self.assertNotIn("s3cr3t-env-value", json.dumps(run))
        self.assertNotIn("RUNNER_SECRET", run["env"])
        self.assertNotIn("HTTPS_PROXY", run["env"])

    def test_audio_is_served_once_then_deleted(self) -> None:
        # R3: the audio leaves once, to the holder of its token, and is gone.
        job = self.done(VIDEO_ID)
        self.assertEqual(self.scratch_contents(), [f"results/{job}.m4a"])
        # Another token gets nothing, and takes nothing.
        self.assertJsonError(self.audio(token()), 404, "not_found")
        response = self.audio(job)
        expected = b"STUB-M4A\n" + VIDEO_ID.encode("ascii")
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.headers.get_all("Content-Type"), ["audio/mp4"])
        self.assertEqual(response.headers.get_all("Content-Length"), [str(len(expected))])
        self.assertEqual(response.body, expected)
        self.assertNoStore(response)
        self.assertJsonError(self.audio(job), 404, "not_found")
        self.assertScratchEmpty()

    def test_failures_answer_failed_with_the_code_and_leave_nothing_behind(self) -> None:
        # R3: every failure is the fixed code and nothing of the run is left.
        for video_id, code in FAILURES.items():
            with self.subTest(video_id=video_id):
                job = token()
                response = self.post(video_id, job)
                self.assertEqual(response.status, 200, response.body)
                self.assertEqual(response.json(), {"status": "failed", "error": code})
                self.assertNoStore(response)
                self.assertJsonError(self.audio(job), 404, "not_found")
                if video_id == "symlinked__":
                    self.assertStubsOutsideFile()
                self.assertScratchEmpty()
        # A hung run, with a short wall clock and drain.
        self.ytdlp.timeout_seconds = 1
        with mock.patch.object(ytdlp_module, "DRAIN_SECONDS", 0.5):
            job = token()
            response = self.post("hanging____", job)
        self.assertEqual(response.json(), {"status": "failed", "error": "timeout"})
        self.assertJsonError(self.audio(job), 404, "not_found")
        self.assertScratchEmpty()

    def test_the_job_directory_is_removed_on_every_path(self) -> None:
        # R3: a fresh owner-only job directory per run under the scratch
        # directory, removed in finally on success, failure and any error.
        seen: list[tuple[Path, int]] = []
        real = self.ytdlp.fetch

        def recording(video_id: str, job_dir: Path) -> Any:
            seen.append((job_dir, os.stat(job_dir).st_mode & 0o777))
            return real(video_id, job_dir)

        self.ytdlp.fetch = recording  # type: ignore[method-assign]
        job = self.done(VIDEO_ID)
        self.assertEqual(self.scratch_contents(), [f"results/{job}.m4a"])
        ((job_dir, mode),) = seen
        self.assertEqual(os.path.normcase(os.path.abspath(job_dir.parent)),
                         os.path.normcase(os.path.abspath(self.scratch)))
        self.assertTrue(job_dir.name.startswith("job-"), job_dir)
        self.assertFalse(job_dir.exists())
        if os.name == "posix":
            self.assertEqual(mode, 0o700)
        self.assertEqual(self.audio(job).status, 200)
        self.assertScratchEmpty()
        # A FetchError.
        self.assertEqual(self.post("failing____").json(), {"status": "failed", "error": "failed"})
        self.assertScratchEmpty()

        # Anything else the run raises: still "failed", nothing left, and the
        # exception's text (which could carry a title) never reaches the log.
        def exploding(video_id: str, job_dir: Path) -> Any:
            (job_dir / f"{video_id}.m4a").write_bytes(b"partial")
            raise RuntimeError("Stub Title secret")

        self.ytdlp.fetch = exploding  # type: ignore[method-assign]
        self.assertEqual(self.post(VIDEO_ID).json(), {"status": "failed", "error": "failed"})
        self.assertScratchEmpty()
        self.assertNotIn("Stub Title secret", self.log.getvalue())
        # And the lock came back: the next job runs.
        self.ytdlp.fetch = real  # type: ignore[method-assign]
        self.done(OTHER_ID)

    def test_a_new_job_discards_an_uncollected_result(self) -> None:
        # R3: at most one audio file on the runner's tmpfs at any time; a new
        # job discards the uncollected one before yt-dlp starts.
        first = self.done(VIDEO_ID)
        self.assertEqual(self.scratch_contents(), [f"results/{first}.m4a"])
        during: list[list[str]] = []
        real = self.ytdlp.fetch

        def recording(video_id: str, job_dir: Path) -> Any:
            during.append(self.scratch_contents())
            return real(video_id, job_dir)

        self.ytdlp.fetch = recording  # type: ignore[method-assign]
        second = self.done(OTHER_ID)
        self.assertEqual(len(during), 1)
        self.assertEqual([name for name in during[0] if name.startswith("results/")], [], during)
        self.assertEqual(self.scratch_contents(), [f"results/{second}.m4a"])
        self.assertEqual(len(self.server.results), 1)
        self.assertJsonError(self.audio(first), 404, "not_found")
        response = self.audio(second)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"STUB-M4A\n" + OTHER_ID.encode("ascii"))
        self.assertScratchEmpty()

    def test_an_uncollected_result_is_deleted_at_the_deadline(self) -> None:
        # R3: an audio file nobody collects is deleted COLLECT_DEADLINE_SECONDS
        # after its run, so a fetcher that never comes back can't keep the
        # tmpfs full. Acceptance first: a second before, it is still there.
        self.assertEqual(COLLECT_DEADLINE_SECONDS, 60.0)
        kept = self.done(VIDEO_ID)
        self.clock.now += COLLECT_DEADLINE_SECONDS - 1
        self.assertEqual(self.server.results.sweep(), 0)
        self.assertEqual(len(self.server.results), 1)
        self.assertEqual(self.audio(kept).status, 200)
        expired = self.done(VIDEO_ID)
        self.clock.now += COLLECT_DEADLINE_SECONDS
        self.assertEqual(self.server.results.sweep(), 1)
        self.assertEqual(len(self.server.results), 0)
        self.assertJsonError(self.audio(expired), 404, "not_found")
        self.assertScratchEmpty()
        # Past the deadline but not yet swept: not handed out either.
        late = self.done(VIDEO_ID)
        self.clock.now += COLLECT_DEADLINE_SECONDS
        self.assertJsonError(self.audio(late), 404, "not_found")
        self.server.results.sweep()
        self.assertScratchEmpty()

    def test_a_post_during_an_audio_stream_is_503_busy(self) -> None:
        # R1/R3: a stream holds the lock until its file is released, so no
        # run starts (and no second audio file appears) while one is collected.
        job = self.done(VIDEO_ID)
        entered, gate = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        results = self.server.results
        real_release = results.release

        def held_release(held: str) -> None:
            entered.set()
            gate.wait(DEADLINE)
            real_release(held)

        results.release = held_release  # type: ignore[method-assign]
        outcome: list[Response] = []
        port = self.port
        # The stream's client returns only at the runner's close, which comes
        # after release() and after busy is let go (busy covers the whole
        # stream, by design), so once it is joined the stream is over on the
        # server's side too.
        thread = threading.Thread(
            target=lambda: outcome.append(exchange_to_close(port, build_request("GET", f"/jobs/{job}/audio"))),
            daemon=True)
        thread.start()
        self.assertTrue(entered.wait(DEADLINE), "the stream never released its file")
        self.assertBusy(self.post(OTHER_ID))
        self.assertBusy(self.audio(job))
        gate.set()
        thread.join(DEADLINE)
        self.assertEqual([response.status for response in outcome], [200])
        self.assertEqual(outcome[0].body, b"STUB-M4A\n" + VIDEO_ID.encode("ascii"))
        # Acceptance: once the stream is over, the next job runs.
        self.done(OTHER_ID)
        self.assertEqual(len(self.runs()), 2)

    def test_server_close_kills_the_run_and_deletes_results(self) -> None:
        # R3: stopping the runner kills a running yt-dlp, removes its job
        # directory, and deletes any audio still waiting.
        procs: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def recording(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = real_popen(*args, **kwargs)
            procs.append(proc)
            return proc

        with mock.patch.object(ytdlp_module.subprocess, "Popen", recording):
            thread, outcome = self.post_in_background("hanging____", token())
            wait_for(lambda: len(self.runs()) == 1, "the hung run to start")
            started = time.monotonic()
            self.close_server()
            thread.join(DEADLINE)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, DEADLINE)
        self.assertEqual(len(outcome), 1)
        if isinstance(outcome[0], Response):
            # Answered before the socket closed: a failure, never "done".
            self.assertEqual(outcome[0].status, 200)
            answer = outcome[0].json()
            self.assertEqual(answer["status"], "failed")
            self.assertIn(answer["error"], ERROR_CODES)
        self.assertEqual(len(procs), 1)
        wait_for(lambda: procs[0].poll() is not None, "the run to be killed")
        self.assertScratchEmpty()
        # A held result goes with the server too.
        self.start()
        job = self.done(VIDEO_ID)
        self.assertEqual(self.scratch_contents(), [f"results/{job}.m4a"])
        self.close_server()
        self.assertEqual(self.scratch_contents(), [])


class RunnerSweeperTest(RunnerServerTestCase):
    sweep_interval = 0.05

    def test_the_sweeper_deletes_an_uncollected_result_on_its_own(self) -> None:
        # R3: the deadline holds without anyone calling sweep(): the server
        # runs its own sweeper. Acceptance first: nothing goes before it.
        job = self.done(VIDEO_ID)
        time.sleep(0.3)
        self.assertEqual(self.scratch_contents(), [f"results/{job}.m4a"])
        self.clock.now += COLLECT_DEADLINE_SECONDS
        wait_for(lambda: not self.scratch_contents(), "the sweeper to delete the uncollected audio")
        self.assertJsonError(self.audio(job), 404, "not_found")


class RunnerOneAtATimeTest(RunnerServerTestCase):
    """R1: one run at a time; a second POST or an audio GET meanwhile is 503 busy, /healthz still answers."""

    def setUp(self) -> None:
        super().setUp()
        self.hung_job = token()
        self.thread, self.outcome = self.post_in_background("hanging____", self.hung_job)
        self.addCleanup(self.finish_hung_run)
        wait_for(lambda: len(self.runs()) == 1, "the hung run to start")

    def finish_hung_run(self) -> None:
        self.ytdlp.cancel()
        self.thread.join(DEADLINE)

    def test_a_second_post_during_a_run_is_503_busy(self) -> None:
        self.assertBusy(self.post(OTHER_ID))
        self.assertBusy(self.post(VIDEO_ID, self.hung_job))
        self.assertEqual(len(self.runs()), 1)
        self.assertTrue(self.thread.is_alive())

    def test_an_audio_get_during_a_run_is_503_busy(self) -> None:
        self.assertBusy(self.audio(self.hung_job))
        self.assertBusy(self.audio(token()))
        self.assertEqual(len(self.runs()), 1)

    def test_healthz_answers_during_a_run(self) -> None:
        response = self.request("GET", "/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"ok")

    def test_the_next_job_runs_once_the_run_is_over(self) -> None:
        # Acceptance: the lock is released on failure too.
        self.finish_hung_run()
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(len(self.outcome), 1)
        self.assertIsInstance(self.outcome[0], Response)
        self.assertEqual(self.outcome[0].json()["status"], "failed")
        self.done(VIDEO_ID)
        self.assertEqual(self.post("failing____").json(), {"status": "failed", "error": "failed"})
        job = self.done(OTHER_ID)
        self.assertEqual(self.audio(job).status, 200)
        self.assertEqual(len(self.runs()), 4)


class RunnerProtocolTest(RunnerServerTestCase):
    def test_healthz_is_ok_and_reveals_nothing(self) -> None:
        response = self.request("GET", "/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(response.headers.get_all("Content-Type"), ["text/plain; charset=utf-8"])
        self.assertNoStore(response)
        # No version, queue, settings or anything else in a header.
        self.assertLessEqual({name.lower() for name in response.headers}, {
            "server", "date", "content-type", "content-length", "cache-control", "connection", "x-content-type-options",
        })

    def test_unknown_paths_404_and_wrong_methods_405(self) -> None:
        # R1: three routes, exact paths, nothing else.
        job = token()
        for method, path, allow in (
            ("GET", "/jobs", "POST"),
            ("PUT", "/jobs", "POST"),
            ("POST", "/healthz", "GET"),
            ("POST", f"/jobs/{job}/audio", "GET"),
            ("DELETE", f"/jobs/{job}/audio", "GET"),
        ):
            with self.subTest(method=method, path=path):
                response = self.request(method, path, [("Content-Length", "0")])
                self.assertJsonError(response, 405, "method_not_allowed")
                self.assertEqual(response.headers.get_all("Allow"), [allow])
        head = self.request("HEAD", "/healthz")
        self.assertEqual(head.status, 405)
        self.assertEqual(head.headers.get_all("Allow"), ["GET"])
        self.assertEqual(head.body, b"")
        # Heads only: the route is refused before a byte of a body is read.
        post_headers = [("Content-Type", JSON_TYPE), ("Content-Length", str(len(job_body(VIDEO_ID, job))))]
        for method, path in (
            ("GET", f"/jobs/{job.upper()}/audio"),
            ("GET", f"/jobs/{job[:-1]}/audio"),
            ("GET", f"/jobs/{job}0/audio"),
            ("GET", f"/jobs/{job}/audio/"),
            ("GET", f"/jobs/{job}"),
            ("GET", "/nope"),
            ("GET", "//jobs"),
            ("GET", "/Healthz"),
            ("GET", "/fetch/healthz"),
            ("POST", "/jobs?x=1"),
            ("POST", "/jobs/"),
            ("POST", "//jobs"),
            ("POST", "/Jobs"),
        ):
            with self.subTest(method=method, path=path):
                headers = post_headers if method == "POST" else []
                self.assertJsonError(self.request(method, path, headers), 404, "not_found")
        options = self.request("OPTIONS", "/jobs")
        self.assertIn(options.status, (404, 405))
        self.assertFalse([name for name in options.headers if name.lower().startswith("access-control-")])
        self.assertEqual(self.runs(), [])
        self.assertScratchEmpty()
        # The token's exact shape is the route: an upper-case copy of a real
        # token is not its audio, and leaves it to its holder.
        real = self.done(VIDEO_ID)
        self.assertJsonError(self.audio(real.upper()), 404, "not_found")
        self.assertJsonError(self.request("GET", f"//jobs/{real}/audio"), 404, "not_found")
        self.assertEqual(self.audio(real).status, 200)

    def test_post_needs_json_content_type_and_a_small_body(self) -> None:
        # R1: refused before a byte of the body is read, so these send heads
        # only (except the short body, which is the point of its case).
        body = job_body(VIDEO_ID, token())
        length = ("Content-Length", str(len(body)))
        json_type = ("Content-Type", JSON_TYPE)
        cases: list[tuple[str, list[tuple[str, str]], bytes, int, str]] = [
            ("text/plain", [("Content-Type", "text/plain"), length], b"", 415, "unsupported_media_type"),
            ("a charset", [("Content-Type", "application/json; charset=utf-8"), length], b"", 415,
             "unsupported_media_type"),
            ("two content types", [json_type, json_type, length], b"", 415, "unsupported_media_type"),
            ("no content type", [length], b"", 415, "unsupported_media_type"),
            ("no length", [json_type], b"", 411, "length_required"),
            ("too large", [json_type, ("Content-Length", str(MAX_BODY_BYTES + 1))], b"", 413, "too_large"),
            ("chunked", [json_type, ("Transfer-Encoding", "chunked")], b"", 400, "transfer_encoding"),
            ("chunked with a length", [json_type, length, ("Transfer-Encoding", "chunked")], b"", 400,
             "transfer_encoding"),
            ("a signed length", [json_type, ("Content-Length", f"+{len(body)}")], b"", 400, "bad_length"),
            ("two lengths", [json_type, length, length], b"", 400, "bad_length"),
        ]
        for label, headers, data, status, code in cases:
            with self.subTest(label):
                self.assertJsonError(self.request("POST", "/jobs", headers, data), status, code)
        short = build_request("POST", "/jobs", [json_type, length], body[:-5])
        self.assertJsonError(exchange(self.port, short, half_close=True), 400, "incomplete_body")
        self.assertEqual(self.runs(), [])
        self.assertScratchEmpty()
        # Acceptance at the limit: exactly MAX_BODY_BYTES of valid JSON runs.
        self.assertEqual(MAX_BODY_BYTES, 1024)
        padded = body[:-1] + b" " * (MAX_BODY_BYTES - len(body)) + b"}"
        self.assertEqual(len(padded), MAX_BODY_BYTES)
        response = self.post_body(padded)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json()["status"], "done")

    def test_bad_bodies_answer_400_with_a_code(self) -> None:
        # R1: the parser's refusals reach the wire as 400 with their code,
        # and none of them starts yt-dlp.
        job = token()
        for body, code in (
            (b"[]", "bad_json"),
            (b"{", "bad_json"),
            (json.dumps({"video_id": VIDEO_ID}).encode("ascii"), "bad_json"),
            (json.dumps({"video_id": VIDEO_ID, "job": job, "extra": 1}).encode("ascii"), "bad_json"),
            (b'{"video_id": "%s", "job": "%s", "job": "%s"}' % (VIDEO_ID.encode(), job.encode(), job.encode()),
             "bad_json"),
            (b"\xff", "bad_json"),
            (job_body("dQw4w9WgXc.", job), "bad_video_id"),
            (job_body("dQw4w9WgX/c", job), "bad_video_id"),
            (job_body("https://www.youtube.com/watch?v=dQw4w9WgXcQ", job), "bad_video_id"),
            (json.dumps({"video_id": ["dQw4w9WgXcQ"], "job": job}).encode("ascii"), None),
            (job_body(VIDEO_ID, job.upper()), "bad_job"),
            (job_body(VIDEO_ID, job[:31]), "bad_job"),
        ):
            with self.subTest(body=body):
                response = self.post_body(body)
                if code is None:
                    # A non-string ID: either 400 code is a refusal (see JobRequestTest).
                    self.assertEqual(response.status, 400, response.body)
                    self.assertIn(response.json().get("error"), ("bad_json", "bad_video_id"))
                    self.assertNoStore(response)
                else:
                    self.assertJsonError(response, 400, code)
        self.assertEqual(self.runs(), [])
        self.assertScratchEmpty()

    def test_every_response_closes_the_connection_and_is_no_store(self) -> None:
        responses: dict[str, Response] = {}
        responses["healthz"] = self.request("GET", "/healthz")
        responses["404"] = self.request("GET", "/nope")
        responses["405"] = self.request("GET", "/jobs")
        responses["415"] = self.request("POST", "/jobs", [("Content-Type", "text/plain"), ("Content-Length", "0")])
        responses["400"] = self.post_body(b"[]")
        with self.server.busy:
            responses["503"] = self.post(VIDEO_ID)
        self.assertBusy(responses["503"])
        job = token()
        responses["done"] = self.post(VIDEO_ID, job)
        responses["audio"] = self.audio(job)
        responses["audio 404"] = self.audio(job)
        responses["failed"] = self.post("failing____")
        for label, response in responses.items():
            with self.subTest(label):
                self.assertNoStore(response)
        # HTTP/1.0, one request per connection: keep-alive asked for and refused.
        with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
            sock.sendall(build_request("GET", "/healthz", [("Connection", "keep-alive")]))
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 200 "), data[:40])
        self.assertIn(b"\r\nConnection: close\r\n", data)

    def test_worker_cap_refuses_with_a_hand_written_503(self) -> None:
        # R1: past MAX_WORKERS connections the runner answers 503 busy
        # itself, with no handler and no thread; then the slot comes back.
        self.start(server_class=_CappedRunnerServer)
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE)
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=DEADLINE) as sock:
                data = b""
                while chunk := sock.recv(65536):
                    data += chunk
            self.assertTrue(data.startswith(b"HTTP/1.0 503 "), data[:60])
            head, _, body = data.partition(b"\r\n\r\n")
            self.assertEqual(json.loads(body), {"error": "busy"})
            self.assertIn(b"\r\nConnection: close\r\n", head)
            self.assertIn(b"\r\nServer: strange_ringtone_runner\r\n", head)
            self.assertIn(b"\r\nCache-Control: no-store\r\n", head)
        finally:
            idle.close()

        def healthy() -> bool:
            # While the slot is still taken, the runner answers this poll
            # with the hand-written 503 and closes without reading the
            # request; Windows then resets the connection before the 503 can
            # be read (Linux delivers it first). Either way the slot isn't
            # back yet, and the wait still needs a 200 within its deadline.
            try:
                return exchange(self.port, build_request("GET", "/healthz")).status == 200
            except ConnectionError:
                return False

        wait_for(healthy, "the worker to be released")


class RunnerLogTest(RunnerServerTestCase):
    """R4: method, masked path, status and duration; never an ID, a title, a token, an address or yt-dlp's output."""

    def test_log_lines_hold_method_masked_path_status_and_duration_only(self) -> None:
        job = self.done(VIDEO_ID)
        self.assertEqual(self.audio(job).status, 200)
        self.assertEqual(self.request("GET", f"/jobs/{job.upper()}/audio").status, 404)
        self.assertEqual(self.request("GET", "/nope?secret=SecretQuery", [("User-Agent", "SecretAgent")]).status, 404)
        refused = self.request("POST", "/jobs", [("Content-Type", "text/plain"), ("Content-Length", "0")])
        self.assertEqual(refused.status, 415)
        self.wait_for_log("POST /jobs 415 ")
        self.wait_for_log("GET /jobs/<job>/audio 200 ")
        self.wait_for_log("GET /jobs/<job>/audio 404 ")
        self.wait_for_log("GET /nope 404 ")
        self.wait_for_log("POST /jobs 200 ")
        lines = self.log.lines()
        self.assertEqual(len(lines), 5, lines)
        for line in lines:
            self.assertRegex(line, LOG_LINE_RE)
        log = self.log.getvalue()
        for secret in (job, job.upper(), VIDEO_ID, "127.0.0.1", "Stub", "Title", "Secret", "secret", "example"):
            self.assertNotIn(secret, log)

    def test_ytdlp_output_never_reaches_the_log(self) -> None:
        for video_id in ("ctrlTitle__", "blocked____"):
            self.assertEqual(self.post(video_id).status, 200)
        self.wait_for_log("POST /jobs 200 ", count=2)
        log = self.log.getvalue()
        for secret in ("Bad", "Title", "Sign in", "youtube", "stub", "Destination", "ERROR", "ctrlTitle__",
                       "blocked____"):
            self.assertNotIn(secret, log)
        for line in self.log.lines():
            self.assertRegex(line, LOG_LINE_RE)


class RunnerScratchTest(unittest.TestCase):
    """R3: the runner's scratch directory is its own, owner-only, emptied at startup, and never a planted path.

    A mirror of the fetcher's ScratchTest (tests/test_fetcher_server.py)
    against the runner's prepare_scratch and RunnerServer: the runner keeps
    no state across restarts, so an audio file a crashed runner left in
    results/ is deleted rather than served to whoever holds its token.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.scratch = self.temp / "scratch"
        self.scratch.mkdir()

    def start(self, scratch: Path | None = None) -> RunnerServer:
        settings = Settings(scratch_dir=scratch or self.scratch)
        server = _TestRunnerServer(("127.0.0.1", 0), settings, ytdlp=StubYtDlp(600, MIB, 30))
        self.addCleanup(server.server_close)
        return server

    def test_startup_creates_and_empties_the_scratch_directory(self) -> None:
        (self.scratch / "job-old").mkdir()
        (self.scratch / "job-old" / "x.m4a").write_bytes(b"x")
        (self.scratch / "results").mkdir()
        (self.scratch / "results" / f"{token()}.m4a").write_bytes(b"x")
        (self.scratch / "stray").write_bytes(b"x")
        server = self.start()
        self.assertEqual(sorted(p.name for p in self.scratch.iterdir()), ["results"])
        self.assertEqual(list((self.scratch / "results").iterdir()), [])
        self.assertEqual(len(server.results), 0)
        # Acceptance: a scratch directory that doesn't exist yet is created.
        fresh = self.temp / "fresh"
        self.start(fresh)
        self.assertTrue(fresh.is_dir())
        self.assertEqual(sorted(p.name for p in fresh.iterdir()), ["results"])

    def test_missing_parent_is_refused_not_created(self) -> None:
        # /tmp missing means the container lacks its tmpfs; nothing is
        # written to the root filesystem instead. The messages are the
        # fetcher's (strange_ringtone_fetch.server.prepare_scratch), matched
        # here so that NotImplementedError, a RuntimeError too, can't pass.
        with self.assertRaisesRegex(RuntimeError, "cannot create"):
            runner_server.prepare_scratch(self.temp / "missing" / "scratch")
        self.assertFalse((self.temp / "missing").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_scratch_directory_is_owner_only(self) -> None:
        os.chmod(self.scratch, 0o777)
        self.start()
        self.assertEqual(self.scratch.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.scratch / "results").stat().st_mode & 0o777, 0o700)

    def test_scratch_directory_owned_by_another_user_is_refused(self) -> None:
        uid = os.lstat(self.scratch).st_uid
        # Patched on the os module itself (create=True: Windows has no
        # getuid), which is where the check reads it from.
        with mock.patch.object(os, "getuid", create=True, return_value=uid + 1):
            with self.assertRaisesRegex(RuntimeError, "owned by another user"):
                runner_server.prepare_scratch(self.scratch)

    def test_a_file_at_the_scratch_path_is_refused(self) -> None:
        path = self.temp / "file"
        path.write_bytes(b"x")
        with self.assertRaisesRegex(RuntimeError, "not a plain directory|cannot create"):
            runner_server.prepare_scratch(path)
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
            runner_server.prepare_scratch(link)
        self.assertEqual(list(target.iterdir()), [])

    def test_a_failed_start_closes_the_socket(self) -> None:
        # The port is bound before the scratch directory is prepared, so a
        # refusal there must release it: the entry point exits and nothing
        # keeps 8080. server_close() runs on a server with no results and no
        # sweeper yet, so it must cope with a half-built one.
        class ScratchRefused(RuntimeError):
            pass

        with mock.patch.object(runner_server, "prepare_scratch", side_effect=ScratchRefused("no")), \
                mock.patch.object(RunnerServer, "server_close", autospec=True,
                                  side_effect=RunnerServer.server_close) as close:
            with self.assertRaises(ScratchRefused):
                _TestRunnerServer(("127.0.0.1", 0), Settings(scratch_dir=self.scratch), ytdlp=StubYtDlp(600, MIB, 30))
        self.assertEqual(close.call_count, 1)


# --- what the runner's process is made of --------------------------------------


def _imports(path: Path) -> set[str]:
    """Every module a file imports, by its full dotted name (relative imports resolved into the package)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = "strange_ringtone_runner" + (f".{module}" if module else "")
            names.add(module)
    return names


class RunnerImportsTest(unittest.TestCase):
    """I1 and the split: the runner's process holds the run code and nothing of the fetcher's server."""

    def test_runner_package_files_are_exactly(self) -> None:
        self.assertEqual(sorted(path.name for path in RUNNER_PACKAGE.glob("*.py")),
                         ["__init__.py", "__main__.py", "config.py", "server.py", "ytdlp.py"])
        self.assertEqual([path.name for path in RUNNER_PACKAGE.iterdir()
                          if path.is_dir() and path.name != "__pycache__"], [])

    def test_imports_are_stdlib_plus_the_allowed_shared_modules(self) -> None:
        stdlib = sys.stdlib_module_names
        used: set[str] = set()
        for path in sorted(RUNNER_PACKAGE.glob("*.py")):
            for module in _imports(path):
                top = module.split(".")[0]
                with self.subTest(file=path.name, module=module):
                    # yt-dlp runs as a subprocess from its venv, never in here.
                    self.assertNotEqual(top, "yt_dlp")
                    if top == "strange_ringtone_runner":
                        continue
                    if top.startswith("strange_ringtone"):
                        self.assertIn(module, SHARED_MODULES)
                        used.add(module)
                    else:
                        self.assertIn(top, stdlib | {"__future__"})
        # Reused, not copied: each shared module is what the runner uses.
        self.assertEqual(used, SHARED_MODULES)

    def test_the_runner_never_imports_the_fetchers_server_jobs_or_client(self) -> None:
        forbidden = ("strange_ringtone_fetch.server", "strange_ringtone_fetch.jobs",
                     "strange_ringtone_fetch.runner_client", "strange_ringtone_fetch.ytdlp",
                     "strange_ringtone_fetch.__main__")
        for path in sorted(RUNNER_PACKAGE.glob("*.py")):
            for module in _imports(path):
                with self.subTest(file=path.name, module=module):
                    self.assertFalse(module.startswith(forbidden), module)
        # What the real entry script loads, transitively, in a fresh
        # interpreter: exactly the runner's package, the three shared fetcher
        # modules and the two core modules, core first on sys.path.
        code = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('runner_entry_script', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print('\\n'.join(sorted(n for n in sys.modules if n.startswith(('strange_ringtone', 'yt_dlp')))))\n"
            "print('core=' + sys.modules['strange_ringtone.config'].__file__)\n"
        )
        proc = subprocess.run([sys.executable, "-I", "-c", code, str(RUNNER_SCRIPT)], env=child_env(),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(set(lines[:-1]), {
            "strange_ringtone", "strange_ringtone.config", "strange_ringtone.http_guard",
            "strange_ringtone_fetch", "strange_ringtone_fetch.config", "strange_ringtone_fetch.metadata",
            "strange_ringtone_fetch.routes",
            "strange_ringtone_runner", "strange_ringtone_runner.__main__", "strange_ringtone_runner.config",
            "strange_ringtone_runner.server", "strange_ringtone_runner.ytdlp",
        })
        core_file = Path(lines[-1].removeprefix("core="))
        # ../core beside the script's directory: the checkout's core/, or the
        # image's /app/core.
        self.assertEqual(core_file.resolve().parent.parent, (RUNNER_SCRIPT.parent.parent / "core").resolve())


class RunnerMainTest(unittest.TestCase):
    """The entry point: settings (exit 2), then the route check (S2, exit 3), then RunnerServer on port 8080."""

    def run_main(self, argv: list[str], environ: dict[str, str], server: Any,
                 routes: Callable[..., Any] | None = None) -> tuple[int, str, mock.MagicMock]:
        log = io.StringIO()
        check = mock.MagicMock(side_effect=routes or AssertionError("the route tables were read"))
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(runner_entry, "RunnerServer", server), \
                mock.patch.object(runner_entry, "default_routes", check, create=True), \
                mock.patch.object(runner_entry, "make_not_dumpable"), \
                mock.patch.object(runner_entry, "sweep_is_safe_here", return_value=True), \
                mock.patch("signal.signal"), \
                contextlib.redirect_stderr(log):
            code = runner_entry.main(argv)
        return code, log.getvalue(), check

    def fake_server(self) -> tuple[type, list[tuple[tuple[str, int], Any]]]:
        started: list[tuple[tuple[str, int], Any]] = []

        class FakeServer:
            # sweep_strays: the entry always passes its PID-1 decision
            # (tests/test_fetcher_runner_sweep.py tests it).
            def __init__(self, address: tuple[str, int], settings: Any, sweep_strays: bool = False) -> None:
                started.append((address, settings))

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        return FakeServer, started

    def test_config_error_exits_2_without_starting(self) -> None:
        code, stderr, check = self.run_main([], {"FETCH_MAX_SECONDS": "0"},
                                            mock.MagicMock(side_effect=AssertionError("started")))
        self.assertEqual(code, 2, stderr)
        self.assertIn("strange_ringtone_runner: configuration error", stderr)
        self.assertIn("FETCH_MAX_SECONDS", stderr)
        check.assert_not_called()

    def test_bind_must_be_an_ip_literal(self) -> None:
        # Anything else is resolved by bind(): a DNS query nobody asked for.
        server = mock.MagicMock(side_effect=AssertionError("started"))
        for value in ("localhost", "example.invalid", "fe80::1%eth0", ""):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(["--bind", value], {"FETCH_ALLOW_DEFAULT_ROUTE": "true"}, server)
                self.assertEqual(raised.exception.code, 2)

    def test_bind_and_port(self) -> None:
        # The override: the host has a default route (or, on Windows, no
        # /proc); S2 has its own tests in tests/test_fetcher_routes.py.
        server, started = self.fake_server()
        environ = {"FETCH_ALLOW_DEFAULT_ROUTE": "true"}
        outputs = []
        for argv in (["--bind", "127.0.0.1"], [], ["--bind", "::1"]):
            code, stderr, _check = self.run_main(argv, environ, server)
            self.assertEqual(code, 0, stderr)
            outputs.append(stderr)
        self.assertEqual([address for address, _ in started], [("127.0.0.1", 8080), ("0.0.0.0", 8080), ("::1", 8080)])
        for _address, settings in started:
            self.assertIsInstance(settings, Settings)
            self.assertIs(settings.allow_default_route, True)
        self.assertIn("strange_ringtone_runner listening on 127.0.0.1:8080", outputs[0])

    def test_an_empty_environment_is_a_valid_configuration(self) -> None:
        # The runner has no required setting. With the override the tables
        # are never read; without it, the check runs and passes.
        server, started = self.fake_server()
        code, stderr, check = self.run_main(["--bind", "127.0.0.1"], {"FETCH_ALLOW_DEFAULT_ROUTE": "true"}, server)
        self.assertEqual(code, 0, stderr)
        check.assert_not_called()
        code, stderr, check = self.run_main(["--bind", "127.0.0.1"], {}, server, routes=lambda *a, **k: ())
        self.assertEqual(code, 0, stderr)
        check.assert_called_once()
        self.assertEqual([address for address, _ in started], [("127.0.0.1", 8080), ("127.0.0.1", 8080)])
        self.assertEqual(started[1][1], Settings())

    def test_checkout_entry_puts_the_core_first_and_starts(self) -> None:
        # python -I fetcher/runner.py: the script form, with the core's
        # modules found from ../core and no other environment. A bad setting
        # proves it ran main() under the runner's name.
        self.assertTrue(RUNNER_SCRIPT.is_file())
        proc = subprocess.run(
            [sys.executable, "-I", str(RUNNER_SCRIPT), "--bind", "127.0.0.1"],
            env=child_env(FETCH_MAX_SECONDS="0"), capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("strange_ringtone_runner: configuration error: FETCH_MAX_SECONDS", proc.stderr)


if __name__ == "__main__":
    unittest.main()
