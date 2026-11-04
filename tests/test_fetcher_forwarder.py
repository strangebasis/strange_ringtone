"""Tests for the forwarder, the fetcher image's only way out (docs/PLAN.md 6.1, checks G1 to G10).

The forwarder is a CONNECT gate: it allows port 443 to youtube.com, its
subdomains and the subdomains of googlevideo.com, decides everything it can
before any DNS lookup, resolves IPv4 only, connects only to global unicast
addresses it checked and relays bytes under fixed caps. These tests never
touch real DNS or the network: the resolver, the connector, the clock and the
outcome sink are injected (a FakeResolver that records its calls, a
FakeConnector handing out socketpair ends, a FakeClock for the per-minute
windows, a sink or a captured log). Real sockets are used only on loopback,
between the test client and a Forwarder bound to 127.0.0.1:0.

Every refusal test has an acceptance counterpart in the same class, so a gate
that refuses everything fails the suite, and each test names its check ID.
The file name matches test_fetcher_*.py, so the in-image unit suite
(tests/image_fetcher/test_fetcher_unit_suite.py) runs it again against the
image's copy of the package.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import io
import ipaddress
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # puts core/ and fetcher/ on sys.path; ROOT finds the README
from strange_ringtone_forwarder import __main__ as forwarder_main
from strange_ringtone_forwarder import policy
from strange_ringtone_forwarder import server as server_module
from strange_ringtone_forwarder.policy import DEFAULT_LIMITS, Limits, Refusal
from strange_ringtone_forwarder.server import Forwarder, Outcome

# The package under test, wherever it was imported from: the checkout's
# fetcher/ on the host, /app/fetcher in the image.
PACKAGE = Path(policy.__file__).resolve().parent
FETCHER_DIR = PACKAGE.parent
FORWARDER_SCRIPT = FETCHER_DIR / "forwarder.py"

# How long a test waits for anything that should happen promptly. Generous,
# so a loaded CI runner or Windows doesn't turn a slow pass into a failure;
# a wrong implementation fails on the value, not on this timeout.
DEADLINE = 10.0
# How long a test waits to be reasonably sure something did *not* happen
# (a late log line, a second sink call). A slow runner can only make such a
# check pass when it should fail, never fail when it should pass.
SETTLE = 0.3

ALLOWED = "www.youtube.com"
# Global by Python's rules (SPEC 5.1); nothing ever connects to them, the
# FakeConnector only records the address.
GLOBAL_A = "11.0.0.1"
GLOBAL_B = "11.0.0.2"

HEAD_END = b"\r\n\r\n"
ACCEPTED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
# The test's own copy of the reason phrases, so a refusal is checked against
# the contract rather than against the module under test.
PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}

# The fields of policy.Limits, in order (SPEC 3.1); the README table carries
# one row for each.
LIMIT_FIELDS = [
    "head_max_bytes",
    "head_deadline_seconds",
    "connections_max",
    "tunnels_max_concurrent",
    "tunnels_per_minute",
    "upload_bytes_per_tunnel",
    "upload_bytes_per_minute",
    "download_bytes_per_tunnel",
    "idle_timeout_seconds",
    "lifetime_seconds",
    "resolve_deadline_seconds",
    "connect_timeout_seconds",
    "log_lines_per_minute",
]

# The whole forwarder package may import exactly these (SPEC 3.2): no os, io,
# pathlib, subprocess, ssl, http, urllib, json or logging, so the gate parses
# nothing with a library parser, runs nothing and can write no file.
ALLOWED_IMPORTS = {
    "__future__",
    "argparse",
    "collections",
    "collections.abc",
    "dataclasses",
    "functools",
    "ipaddress",
    "re",
    "selectors",
    "signal",
    "socket",
    "socketserver",
    "sys",
    "threading",
    "time",
    "typing",
    "types",
}
OWN_PACKAGE = "strange_ringtone_forwarder"


def connect_head(host: str, port: str = "443", version: str = "HTTP/1.1", headers: bytes = b"") -> bytes:
    return f"CONNECT {host}:{port} {version}\r\n".encode() + headers + b"\r\n"


def refusal_head(status: int) -> bytes:
    return f"HTTP/1.1 {status} {PHRASES[status]}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode()


def status_of(head: bytes) -> int:
    """The status code of a response head; 0 for no response at all, -1 for anything that isn't one."""
    if not head:
        return 0
    match = re.match(rb"HTTP/1\.1 (\d{3}) ", head)
    return int(match.group(1)) if match else -1


def recv_head(sock: socket.socket) -> bytes:
    """Read a response head one byte at a time, so nothing after it is consumed; b"" on EOF or reset."""
    data = b""
    while not data.endswith(HEAD_END):
        try:
            chunk = sock.recv(1)
        except (ConnectionResetError, ConnectionAbortedError):
            break
        if not chunk:
            break
        data += chunk
    return data


def recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


def read_to_eof(sock: socket.socket) -> tuple[bytes, bool]:
    """Everything until EOF, and whether the connection was reset instead of closed."""
    data = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except (ConnectionResetError, ConnectionAbortedError):
            return data, True
        if not chunk:
            return data, False
        data += chunk


def wait_until(predicate: Callable[[], bool], timeout: float = DEADLINE) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class FakeClock:
    """Monotonic seconds that move only when a test says so, for the per-minute windows."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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


class FakeResolver:
    """Answers from a table and records every (name, family) it was asked; a name missing from it fails."""

    def __init__(self, answers: dict[str, list[str] | BaseException] | None = None) -> None:
        self.answers: dict[str, list[str] | BaseException] = dict(answers or {})
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def __call__(self, name: str, family: int) -> Sequence[str]:
        with self._lock:
            self.calls.append((name, family))
        answer = self.answers.get(name)
        if answer is None:
            raise OSError("no answer in the test's table")
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)


class FakeConnector:
    """Hands the server one end of a socketpair and keeps the other as the 'upstream'; never touches a network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.fail: dict[str, BaseException] = {}
        self.upstreams: list[socket.socket] = []
        self._lock = threading.Lock()

    def __call__(self, address: str, port: int, timeout: float) -> socket.socket:
        with self._lock:
            self.calls.append((address, port, timeout))
        failure = self.fail.get(address)
        if failure is not None:
            raise failure
        near, far = socket.socketpair()
        far.settimeout(DEADLINE)
        with self._lock:
            self.upstreams.append(far)
        return near

    def close_all(self) -> None:
        with self._lock:
            upstreams = list(self.upstreams)
        for sock in upstreams:
            sock.close()


class OutcomeSink:
    """A sink that records every Outcome and lets a test wait for them."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.outcomes: list[Outcome] = []

    def __call__(self, outcome: Outcome) -> None:
        with self._condition:
            self.outcomes.append(outcome)
            self._condition.notify_all()

    def wait(self, count: int, timeout: float = DEADLINE) -> list[Outcome]:
        with self._condition:
            self._condition.wait_for(lambda: len(self.outcomes) >= count, timeout)
            return list(self.outcomes)

    def snapshot(self) -> list[Outcome]:
        with self._condition:
            return list(self.outcomes)


_SINK = object()


class ForwarderTestCase(unittest.TestCase):
    """Starts Forwarders on 127.0.0.1:0 with injected parts and talks to them over loopback."""

    def setUp(self) -> None:
        # The default log is stderr: keep anything a server prints out of the
        # test run's output, and let tests read it.
        self.stderr = LogCapture()
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.log = LogCapture()
        self.sink = OutcomeSink()
        self.clock = FakeClock()
        self.fresh()

    def fresh(self, answers: dict[str, list[str] | BaseException] | None = None) -> None:
        """A new resolver and connector for the next start(), as a subTest's own."""
        if answers is None:
            answers = {
                ALLOWED: [GLOBAL_A],
                "m.youtube.com": [GLOBAL_A],
                "youtube.com": [GLOBAL_A],
                "r1---sn-abc.googlevideo.com": [GLOBAL_B],
            }
        self.resolver = FakeResolver(answers)
        self.connector = FakeConnector()
        self.addCleanup(self.connector.close_all)
        self.sink = OutcomeSink()
        self.log = LogCapture()

    def start(
        self,
        limits: Limits | None = None,
        *,
        resolver: Any = None,
        connector: Any = None,
        clock: Any = None,
        sink: Any = _SINK,
        log: Any = None,
        omit: Sequence[str] = (),
    ) -> Forwarder:
        """A Forwarder serving in a daemon thread; sink=None means its default sink, the log."""
        kwargs: dict[str, Any] = {
            "resolver": resolver if resolver is not None else self.resolver,
            "connector": connector if connector is not None else self.connector,
            "sink": self.sink if sink is _SINK else sink,
            "log": log if log is not None else self.log,
        }
        if clock is not None:
            kwargs["clock"] = clock
        for key in omit:
            kwargs.pop(key, None)
        server = Forwarder(("127.0.0.1", 0), limits if limits is not None else DEFAULT_LIMITS, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        self.server = server
        self.port = server.server_address[1]
        return server

    @staticmethod
    def _stop(server: Forwarder, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(DEADLINE)

    def client(self) -> socket.socket:
        # A numeric address, so connecting never calls getaddrinfo (some tests
        # patch it).
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.settimeout(DEADLINE)
        sock.connect(("127.0.0.1", self.port))
        return sock

    def request(self, data: bytes) -> tuple[int, bytes]:
        """Send data on a fresh connection; the status code and the raw response head."""
        sock = self.client()
        sock.sendall(data)
        head = recv_head(sock)
        return status_of(head), head

    def assert_refused(self, data: bytes, status: int) -> None:
        """data gets exactly the bare refusal for status, and the connection is closed after it."""
        sock = self.client()
        sock.sendall(data)
        head = recv_head(sock)
        self.assertEqual(head, refusal_head(status), data)
        rest, _reset = read_to_eof(sock)
        self.assertEqual(rest, b"", "a refusal has no body and closes the connection")

    def tunnel(self, name: str = ALLOWED, after_head: bytes = b"") -> tuple[socket.socket, socket.socket]:
        """(client, upstream) of an accepted CONNECT to name."""
        before = len(self.connector.upstreams)
        client = self.client()
        client.sendall(connect_head(name, headers=f"Host: {name}:443\r\n".encode()) + after_head)
        head = recv_head(client)
        self.assertEqual(head, ACCEPTED, f"CONNECT {name}:443 was not accepted")
        self.assertEqual(len(self.connector.upstreams), before + 1)
        upstream = self.connector.upstreams[-1]
        self.addCleanup(upstream.close)
        return client, upstream

    def tunnel_when_free(self, name: str = ALLOWED) -> tuple[socket.socket, socket.socket]:
        """A tunnel, retrying while the server still answers 503: a finished tunnel frees its slot asynchronously."""
        end = time.monotonic() + DEADLINE
        while True:
            client = self.client()
            client.sendall(connect_head(name))
            head = recv_head(client)
            if status_of(head) != 503 or time.monotonic() > end:
                break
            time.sleep(0.05)
        self.assertEqual(head, ACCEPTED, f"CONNECT {name}:443 was not accepted")
        upstream = self.connector.upstreams[-1]
        self.addCleanup(upstream.close)
        return client, upstream

    def close_tunnel(self, client: socket.socket, upstream: socket.socket, expected: int) -> None:
        """Close a tunnel from the client's side and wait for its 'ok' outcome (the expected-th)."""
        client.close()
        self.assertEqual(read_to_eof(upstream)[0], b"")
        outcomes = self.sink.wait(expected)
        self.assertGreaterEqual(len(outcomes), expected)
        self.assertEqual(outcomes[expected - 1].reason, "ok")

    def wait_lines(self, log: LogCapture, count: int) -> list[str]:
        wait_until(lambda: len(log.lines()) >= count)
        return log.lines()

    def assert_refusal_raised(self, head: bytes, status: int, reason: str, name: Any = ...) -> None:
        with self.assertRaises(Refusal, msg=head) as raised:
            policy.parse_connect(head)
        self.assertEqual((raised.exception.status, raised.exception.reason), (status, reason), head)
        if name is not ...:
            self.assertEqual(raised.exception.name, name, head)


class HeadParsingTest(ForwarderTestCase):
    """G1, G2: only `CONNECT <name>:443 HTTP/1.0|1.1` with CRLF passes step 1."""

    def test_connect_to_443_yields_the_lowercased_name(self) -> None:
        # G1 (acceptance): the name comes back lowercased and validated, from
        # either HTTP/1.x version, whatever follows the request line.
        cases = {
            connect_head("www.youtube.com"): "www.youtube.com",
            connect_head("WWW.YouTube.COM"): "www.youtube.com",
            connect_head("www.youtube.com", version="HTTP/1.0"): "www.youtube.com",
            connect_head("r4---sn-4g5e6nzz.googlevideo.com"): "r4---sn-4g5e6nzz.googlevideo.com",
            connect_head("123.youtube.com"): "123.youtube.com",
            connect_head("a" * 63 + ".youtube.com"): "a" * 63 + ".youtube.com",
            # Headers are not parsed at all: a Host naming something else,
            # bytes that aren't ASCII, or a line that isn't a header change
            # nothing.
            connect_head(
                "www.youtube.com",
                headers=b"Host: example.invalid:80\r\nX-Junk: \xff\xfe\x00\r\nnot a header\r\n",
            ): "www.youtube.com",
        }
        for head, name in cases.items():
            with self.subTest(head=head):
                self.assertEqual(policy.parse_connect(head), name)
        # The server hands the lowercased name, and only it, to the resolver.
        self.start()
        status, head = self.request(connect_head("Www.YouTube.com"))
        self.assertEqual(head, ACCEPTED)
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])

    def test_other_methods_are_refused_405(self) -> None:
        # G1: anything but CONNECT, plain-HTTP proxying included, and the
        # method is case-sensitive.
        for method in ("GET", "POST", "HEAD", "PUT", "OPTIONS", "PATCH", "DELETE", "TRACE", "connect", "Connect"):
            with self.subTest(method=method):
                self.assert_refusal_raised(f"{method} www.youtube.com:443 HTTP/1.1\r\n\r\n".encode(), 405, "method",
                                           name="www.youtube.com")
        self.assert_refusal_raised(b"GET / HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n", 405, "method", name=None)
        with self.assertRaises(Refusal) as raised:
            policy.parse_connect(b"GET http://www.youtube.com/watch HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n")
        self.assertEqual((raised.exception.status, raised.exception.reason), (405, "method"))
        self.start()
        for data in (b"GET / HTTP/1.1\r\n\r\n",
                     b"GET http://www.youtube.com/ HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n",
                     b"POST www.youtube.com:443 HTTP/1.1\r\n\r\n"):
            with self.subTest(data=data):
                self.assert_refused(data, 405)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.connector.calls, [])
        # The acceptance counterpart on the same server.
        self.assertEqual(self.request(connect_head(ALLOWED))[1], ACCEPTED)

    def test_other_ports_are_refused_403(self) -> None:
        # G1: the port text must be exactly "443".
        for port in ("80", "22", "8443", "0443", "+443", "4430", "1", "443a", "65979"):
            with self.subTest(port=port):
                self.assert_refusal_raised(connect_head("www.youtube.com", port=port), 403, "port",
                                           name="www.youtube.com")
        self.assertEqual(policy.parse_connect(connect_head("www.youtube.com", port="443")), "www.youtube.com")
        self.start()
        for port in ("80", "8443", "0443"):
            with self.subTest(port=port):
                self.assert_refused(connect_head(ALLOWED, port=port), 403)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.request(connect_head(ALLOWED))[1], ACCEPTED)

    def test_refusals_are_a_bare_status_line(self) -> None:
        # G1, G2, G10: a refusal tells the client the status and nothing else.
        for status in PHRASES:
            with self.subTest(status=status):
                self.assertEqual(policy.refusal_bytes(status), refusal_head(status))

    def test_ip_literals_are_refused_400(self) -> None:
        # G2: an address in any notation, including forms only some parsers
        # read as one (a bare integer, a short dotted form, octal-looking
        # parts, a name whose last label is all digits).
        dotted = ("192.0.2.1", "11.0.0.1", "127.0.0.1", "0177.0.0.1", "192.0.2", "1.1", "011.0.0.1",
                  "3221225985", "www.youtube.123", "0")
        for host in dotted:
            with self.subTest(host=host):
                self.assert_refusal_raised(connect_head(host), 400, "literal")
        self.assert_refusal_raised(connect_head("192.0.2.1"), 400, "literal", name="192.0.2.1")
        for host in ("[::1]", "[2001:db8::1]", "[::ffff:192.0.2.1]", "[::ffff:c000:201]"):
            with self.subTest(host=host):
                self.assert_refusal_raised(connect_head(host), 400, "literal", name=None)
        # Names with digits in them are names (acceptance).
        for host in ("1.youtube.com", "r1---sn-a1b2.googlevideo.com", "0x7f.youtube.com"):
            with self.subTest(host=host):
                self.assertEqual(policy.parse_connect(connect_head(host)), host)
        self.start()
        for host in ("192.0.2.1", "3221225985", "[::1]"):
            with self.subTest(host=host):
                self.assert_refused(connect_head(host), 400)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.connector.calls, [])

    def test_hex_ip_literals_are_refused_400(self) -> None:
        # G2 (RC2-5): inet_aton also reads hex parts with a 0x prefix, so a
        # host whose last label is 0x hex is an address, not a name: 400
        # literal, never 403 unlisted.
        for host in ("0xc0000201", "0XC0000201", "0xc0.0x0.0x2.0x1", "192.0.2.0x1", "0x7f.1", "1.2.3.0x"):
            with self.subTest(host=host):
                self.assert_refusal_raised(connect_head(host), 400, "literal")
        # Acceptance: a hex-looking label anywhere but last is a name, and a
        # last label that only starts like hex is one too (refused by the
        # allowlist, not as a literal).
        self.assertEqual(policy.parse_connect(connect_head("0x7f.youtube.com")), "0x7f.youtube.com")
        self.assertEqual(policy.parse_connect(connect_head("www.0xg")), "www.0xg")
        self.assertEqual(policy.parse_connect(connect_head("www.x0c")), "www.x0c")
        self.start()
        self.assert_refused(connect_head("0xc0.0x0.0x2.0x1"), 400)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.connector.calls, [])

    def test_malformed_names_are_refused_400(self) -> None:
        # G2: names outside the hostname grammar, and authorities that aren't
        # a bare <host>:<port>.
        long_ok = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 49 + ".youtube.com"
        self.assertEqual(len(long_ok), policy.NAME_MAX_CHARS)
        long_bad = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 50 + ".youtube.com"
        bad_names = ("-www.youtube.com", "www-.youtube.com", "www..youtube.com", ".youtube.com",
                     "www.youtube.com.", "www_x.youtube.com", "a" * 64 + ".youtube.com", long_bad,
                     "*.youtube.com", "www.youtube.com%2e", "w!w.youtube.com")
        for host in bad_names:
            with self.subTest(host=host):
                self.assert_refusal_raised(connect_head(host), 400, "bad_name", name=None)
        # Control characters are refused too; whether as a bad name or as a
        # request line that isn't plain tokens, the spec leaves open.
        for host in ("www.you\x1btube.com", "www.youtube.com\x00", "www.you\x7ftube.com"):
            with self.subTest(host=host):
                with self.assertRaises(Refusal) as raised:
                    policy.parse_connect(connect_head(host))
                self.assertEqual(raised.exception.status, 400)
                self.assertIn(raised.exception.reason, ("bad_name", "malformed"))
                self.assertIsNone(raised.exception.name)
        malformed = (b"CONNECT www.youtube.com HTTP/1.1\r\n\r\n",
                     b"CONNECT :443 HTTP/1.1\r\n\r\n",
                     b"CONNECT user@www.youtube.com:443 HTTP/1.1\r\n\r\n",
                     b"CONNECT www.youtube.com:443/path HTTP/1.1\r\n\r\n",
                     b"CONNECT www.youtube.com:443:443 HTTP/1.1\r\n\r\n",
                     b"CONNECT ::1:443 HTTP/1.1\r\n\r\n",
                     b"CONNECT https://www.youtube.com:443 HTTP/1.1\r\n\r\n")
        for head in malformed:
            with self.subTest(head=head):
                with self.assertRaises(Refusal) as raised:
                    policy.parse_connect(head)
                self.assertEqual(raised.exception.status, 400, head)
                self.assertIn(raised.exception.reason, ("malformed", "bad_name"), head)
        self.assert_refusal_raised(b"CONNECT user@www.youtube.com:443 HTTP/1.1\r\n\r\n", 400, "malformed")
        self.assert_refusal_raised(b"CONNECT www.youtube.com HTTP/1.1\r\n\r\n", 400, "malformed")
        # The limits themselves are allowed (acceptance).
        self.assertEqual(policy.parse_connect(connect_head(long_ok)), long_ok)
        self.assertEqual(policy.parse_connect(connect_head("a" * 63 + ".youtube.com")), "a" * 63 + ".youtube.com")
        self.start()
        for host in ("-www.youtube.com", "www..youtube.com", "www.youtube.com.", long_bad):
            with self.subTest(host=host):
                self.assert_refused(connect_head(host), 400)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.request(connect_head(ALLOWED))[1], ACCEPTED)

    def test_non_ascii_names_are_refused_400(self) -> None:
        # G2: no IDNA, no Unicode lowering, no normalisation: a name that
        # would become youtube.com under any of them is still refused.
        hosts = ("www.y\u00f6utube.com", "\uff59\uff4f\uff55\uff54\uff55\uff42\uff45.com", "www.youtube\u3002com",
                 "www.youtube.co\u1d0d", "WWW.YOUTUBE.CO\u216f", "\u0131.youtube.com")
        for host in hosts:
            with self.subTest(host=host):
                with self.assertRaises(Refusal) as raised:
                    policy.parse_connect(connect_head(host))
                self.assertEqual(raised.exception.status, 400)
                self.assertIsNone(raised.exception.name)
        with self.assertRaises(Refusal) as raised:
            policy.parse_connect(b"CONNECT www.\xe9.youtube.com:443 HTTP/1.1\r\n\r\n")
        self.assertEqual(raised.exception.status, 400)
        # Punycode is ASCII and passes the grammar (acceptance).
        self.assertEqual(policy.parse_connect(connect_head("xn--ytube-jua.youtube.com")), "xn--ytube-jua.youtube.com")
        self.start()
        for host in hosts[:2]:
            with self.subTest(host=host):
                self.assert_refused(connect_head(host), 400)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.request(connect_head(ALLOWED))[1], ACCEPTED)

    def test_request_line_grammar_is_strict(self) -> None:
        # G2: exactly three single-space-separated tokens, a version of
        # HTTP/1.0 or HTTP/1.1, and CRLF.
        heads = (
            b"CONNECT  www.youtube.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT www.youtube.com:443  HTTP/1.1\r\n\r\n",
            b" CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.1 \r\n\r\n",
            b"CONNECT\twww.youtube.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT www.youtube.com:443\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.1 extra\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/2.0\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.2\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/0.9\r\n\r\n",
            b"CONNECT www.youtube.com:443 http/1.1\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.10\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.1\n\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.1\rX\r\n\r\n",
            b"CONNECT www.youtube.com:443 HTTP/1.1\x00\r\n\r\n",
            b"\r\n\r\n",
            b"\r\nCONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n",
        )
        for head in heads:
            with self.subTest(head=head):
                self.assert_refusal_raised(head, 400, "malformed", name=None)
        # The grammar's own forms pass (acceptance).
        for version in ("HTTP/1.0", "HTTP/1.1"):
            with self.subTest(version=version):
                self.assertEqual(policy.parse_connect(connect_head(ALLOWED, version=version)), ALLOWED)
        self.start()
        for head in (heads[0], heads[7], heads[12]):
            with self.subTest(head=head):
                self.assert_refused(head, 400)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.request(connect_head(ALLOWED, version="HTTP/1.0"))[1], ACCEPTED)


class AllowlistTest(ForwarderTestCase):
    """G3: youtube.com and its subdomains, subdomains of googlevideo.com, matched by whole labels."""

    def test_youtube_com_and_subdomains_are_allowed(self) -> None:
        # G3
        for name in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "a.b.c.youtube.com",
                     "youtube.youtube.com"):
            with self.subTest(name=name):
                self.assertIs(policy.name_allowed(name), True)

    def test_googlevideo_subdomains_only(self) -> None:
        # G3: the apex googlevideo.com serves nothing yt-dlp needs.
        for name in ("r1---sn-abc.googlevideo.com", "a.b.googlevideo.com", "googlevideo.googlevideo.com"):
            with self.subTest(name=name):
                self.assertIs(policy.name_allowed(name), True)
        self.assertIs(policy.name_allowed("googlevideo.com"), False)

    def test_whole_label_matching(self) -> None:
        # G3: a suffix or prefix match on characters rather than labels would
        # let these through.
        for name in ("notyoutube.com", "wwwyoutube.com", "evil-youtube.com", "youtube.com.example.invalid",
                     "youtube.co", "youtube.comm", "youtubecom", "xgooglevideo.com", "notgooglevideo.com",
                     "googlevideo.com.example.invalid", "a.googlevideo.co", "example.invalid", "youtu.be",
                     "ytimg.com", "com", "youtube", ""):
            with self.subTest(name=name):
                self.assertIs(policy.name_allowed(name), False)

    def test_unlisted_names_get_403_from_the_server(self) -> None:
        # G3: refused at step 2, with no lookup.
        self.start()
        for name in ("example.invalid", "notyoutube.com", "youtube.com.example.invalid", "googlevideo.com"):
            with self.subTest(name=name):
                self.assert_refused(connect_head(name), 403)
        self.assertEqual(self.resolver.calls, [])
        outcomes = self.sink.wait(4)
        self.assertEqual([(o.name, o.reason) for o in outcomes[:4]],
                         [("example.invalid", "refused:unlisted"), ("notyoutube.com", "refused:unlisted"),
                          ("youtube.com.example.invalid", "refused:unlisted"), ("googlevideo.com", "refused:unlisted")])
        # Acceptance: listed names of both kinds get a tunnel.
        for name in ("youtube.com", "r1---sn-abc.googlevideo.com"):
            with self.subTest(name=name):
                self.assertEqual(self.request(connect_head(name))[1], ACCEPTED)


class OrderingTest(ForwarderTestCase):
    """G4: steps 1 to 3 decide before any resolver call."""

    def test_no_resolver_call_on_any_refusal(self) -> None:
        # G4: every refusal before step 4 makes zero resolver calls (and no
        # connection). The resolver would answer the allowed name, so a gate
        # that looked it up first would show here.
        partial = b"CONNECT www.youtube.com:443 HTTP/1.1\r\n"
        simple = {
            "method": (b"GET www.youtube.com:443 HTTP/1.1\r\n\r\n", 405, None),
            "port": (connect_head(ALLOWED, port="80"), 403, None),
            "literal": (connect_head(GLOBAL_A), 400, None),
            "malformed": (b"CONNECT www.youtube.com:443 HTTP/1.1 x\r\n\r\n", 400, None),
            "non_ascii": (connect_head("www.y\u00f6utube.com"), 400, None),
            "unlisted": (connect_head("notyoutube.com"), 403, None),
            "bad_name": (connect_head("-www.youtube.com"), 400, None),
            "head_too_large": (partial + b"X: " + b"a" * (128 - len(partial) - 3), 431, Limits(head_max_bytes=128)),
            "head_timeout": (partial, 408, Limits(head_deadline_seconds=0.3)),
        }
        for label, (data, status, limits) in simple.items():
            with self.subTest(path=label):
                self.fresh()
                self.start(limits)
                self.assertEqual(self.request(data)[0], status)
                self.assertEqual(self.resolver.calls, [])
                self.assertEqual(self.connector.calls, [])
        with self.subTest(path="empty"):
            # A connection closed before any byte: no response, and nothing
            # looked up or connected either.
            self.fresh()
            self.start()
            sock = self.client()
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(read_to_eof(sock)[0], b"")
            self.assertEqual(self.sink.wait(1)[0].reason, "refused:empty")
            self.assertEqual(self.resolver.calls, [])
            self.assertEqual(self.connector.calls, [])
        with self.subTest(path="busy"):
            self.fresh()
            self.start(Limits(tunnels_max_concurrent=1))
            self.tunnel()
            self.assertEqual(self.request(connect_head("m.youtube.com"))[0], 503)
            self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])
        with self.subTest(path="rate"):
            self.fresh()
            self.start(Limits(tunnels_per_minute=1), clock=self.clock)
            client, upstream = self.tunnel()
            self.close_tunnel(client, upstream, 1)
            self.assertEqual(self.request(connect_head("m.youtube.com"))[0], 429)
            self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])

    def test_exactly_one_af_inet_lookup_on_acceptance(self) -> None:
        # G4: one lookup, for the name, IPv4 only, and none after.
        self.start()
        client, upstream = self.tunnel()
        client.sendall(b"hello")
        self.assertEqual(recv_exact(upstream, 5), b"hello")
        self.close_tunnel(client, upstream, 1)
        self.assertEqual(self.resolver.calls, [("www.youtube.com", socket.AF_INET)])
        self.assertEqual(len(self.connector.calls), 1)

    def test_budgets_are_checked_before_dns(self) -> None:
        # G4, G8: the second name has no answer, so a gate that resolved
        # before taking the budgets would answer 502 instead of 503 or 429.
        self.fresh({ALLOWED: [GLOBAL_A], "m.youtube.com": OSError("must not be asked")})
        self.start(Limits(tunnels_max_concurrent=1))
        self.tunnel()
        self.assertEqual(self.request(connect_head("m.youtube.com"))[0], 503)
        self.assertNotIn(("m.youtube.com", socket.AF_INET), self.resolver.calls)

        self.fresh({ALLOWED: [GLOBAL_A], "m.youtube.com": OSError("must not be asked")})
        self.start(Limits(tunnels_per_minute=1), clock=self.clock)
        client, upstream = self.tunnel()
        self.close_tunnel(client, upstream, 1)
        self.assertEqual(self.request(connect_head("m.youtube.com"))[0], 429)
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])

    def test_the_connections_cap_is_decided_before_dns(self) -> None:
        # G4, G8: a connection over connections_max is closed before any
        # head is read, so nothing is looked up or connected for it either.
        self.start(Limits(connections_max=1, head_deadline_seconds=8))
        held = self.client()
        over = self.client()
        self.assertEqual(read_to_eof(over)[0], b"")
        self.assertEqual(self.sink.wait(1)[0].reason, "refused:connections")
        time.sleep(SETTLE)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.connector.calls, [])
        # Acceptance: the held connection is served, with exactly one lookup.
        held.sendall(connect_head(ALLOWED))
        self.assertEqual(recv_head(held), ACCEPTED)
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])
        self.assertEqual(len(self.connector.calls), 1)


class AddressPolicyTest(ForwarderTestCase):
    """G5: IPv4 only, every answer global unicast, or 403 and no connection."""

    def test_is_global_unicast_table(self) -> None:
        # G5: Python's is_global, minus multicast (which it calls global),
        # minus 192.88.99.0/24 (deprecated 6to4 anycast).
        allowed = ("11.0.0.1", "11.0.0.2", "8.8.8.8", "1.1.1.1", "142.250.0.1", "192.0.0.9", "192.0.0.10",
                   "100.63.255.255", "100.128.0.0", "172.15.255.255", "172.32.0.0", "192.88.98.255",
                   "192.88.100.0", "198.17.255.255", "198.20.0.0", "223.255.255.254", "192.169.0.1")
        refused = ("10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1", "127.0.0.1", "169.254.169.254",
                   "100.64.0.1", "100.127.255.255", "0.0.0.0", "0.1.2.3", "240.0.0.1", "255.255.255.255",
                   "192.0.2.1", "198.51.100.1", "203.0.113.1", "198.18.0.1", "198.19.255.255", "192.0.0.1",
                   "192.0.0.8", "192.0.0.170", "224.0.0.1", "239.255.255.250", "233.252.0.1", "192.88.99.0",
                   "192.88.99.1", "192.88.99.255")
        for text in allowed:
            with self.subTest(address=text):
                self.assertIs(policy.is_global_unicast(ipaddress.IPv4Address(text)), True)
        for text in refused:
            with self.subTest(address=text):
                self.assertIs(policy.is_global_unicast(ipaddress.IPv4Address(text)), False)

    def test_each_bad_answer_is_refused_403_and_never_connected(self) -> None:
        # G5: one bad answer refuses the whole tunnel, whatever else the
        # answer holds (a good address first, so "try the first" or "skip
        # the bad ones" would connect).
        cases: dict[str, tuple[list[str] | BaseException, int, str]] = {
            "private": (["10.0.0.1"], 403, "refused:not_global"),
            "loopback": (["127.0.0.1"], 403, "refused:not_global"),
            "link_local_metadata": (["169.254.169.254"], 403, "refused:not_global"),
            "cgnat": (["100.64.0.1"], 403, "refused:not_global"),
            "multicast": (["224.0.0.1"], 403, "refused:not_global"),
            "reserved": (["240.0.0.1"], 403, "refused:not_global"),
            "broadcast": (["255.255.255.255"], 403, "refused:not_global"),
            "unspecified": (["0.0.0.0"], 403, "refused:not_global"),
            "test_net": (["192.0.2.1"], 403, "refused:not_global"),
            "benchmarking": (["198.18.0.1"], 403, "refused:not_global"),
            "sixto4_anycast": (["192.88.99.1"], 403, "refused:not_global"),
            "mixed": ([GLOBAL_A, "10.0.0.1"], 403, "refused:not_global"),
            "ipv6_string": ([GLOBAL_A, "2001:4860:4860::8888"], 403, "refused:not_global"),
            "garbage": ([GLOBAL_A, "not-an-address"], 403, "refused:not_global"),
            "empty_answer": ([], 502, "refused:resolve_failed"),
            "resolver_error": (OSError("lookup failed"), 502, "refused:resolve_failed"),
        }
        for label, (answer, status, reason) in cases.items():
            with self.subTest(case=label):
                if not isinstance(answer, BaseException):
                    with self.assertRaises(Refusal) as raised:
                        policy.check_addresses(answer, ALLOWED)
                    self.assertEqual((raised.exception.status, raised.exception.reason, raised.exception.name),
                                     (status, reason.split(":", 1)[1], ALLOWED))
                self.fresh({ALLOWED: answer})
                self.start()
                self.assert_refused(connect_head(ALLOWED), status)
                self.assertEqual(self.connector.calls, [])
                outcome = self.sink.wait(1)[0]
                self.assertEqual((outcome.name, outcome.reason), (ALLOWED, reason))
        # Non-canonical IPv4 text is not an IPv4 address either.
        for text in ("011.0.0.1", "11.0.0.1 ", "11.0.0.1/32", "::ffff:11.0.0.1", "0x0b.0.0.1"):
            with self.subTest(answer=text):
                with self.assertRaises(Refusal) as raised:
                    policy.check_addresses([GLOBAL_A, text], ALLOWED)
                self.assertEqual(raised.exception.status, 403)

    def test_non_text_answers_are_refused_403(self) -> None:
        # G5: an answer must be text. IPv4Address also takes an int or packed
        # bytes; those and None fail the canonical-text check as well, but an
        # object that prints as a global address and compares equal to that
        # text passes every check but the type check, so it pins that one.
        class TextLookalike:
            def __str__(self) -> str:
                return GLOBAL_A

            def __eq__(self, other: object) -> bool:
                return other == GLOBAL_A

            def __ne__(self, other: object) -> bool:
                return other != GLOBAL_A

            __hash__ = object.__hash__

        cases: dict[str, list[Any]] = {
            "int": [int(ipaddress.IPv4Address(GLOBAL_A))],
            "packed": [ipaddress.IPv4Address(GLOBAL_A).packed],
            "none": [None],
            "text_lookalike": [TextLookalike()],
            "lookalike_after_text": [GLOBAL_B, TextLookalike()],
        }
        for label, answer in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(Refusal) as raised:
                    policy.check_addresses(answer, ALLOWED)
                self.assertEqual((raised.exception.status, raised.exception.reason, raised.exception.name),
                                 (403, "not_global", ALLOWED))
                self.fresh({ALLOWED: answer})
                self.start()
                self.assert_refused(connect_head(ALLOWED), 403)
                self.assertEqual(self.connector.calls, [])
        # Acceptance: the same address as text connects.
        self.fresh({ALLOWED: [GLOBAL_A]})
        self.start()
        self.tunnel()
        self.assertEqual([call[0] for call in self.connector.calls], [GLOBAL_A])

    def test_all_global_answers_are_accepted(self) -> None:
        # G5 (acceptance): order kept, duplicates dropped.
        self.assertEqual(policy.check_addresses([GLOBAL_A, GLOBAL_B, GLOBAL_A], ALLOWED),
                         (ipaddress.IPv4Address(GLOBAL_A), ipaddress.IPv4Address(GLOBAL_B)))
        self.assertEqual(policy.check_addresses(["192.0.0.9"], ALLOWED), (ipaddress.IPv4Address("192.0.0.9"),))
        self.fresh({ALLOWED: [GLOBAL_A, GLOBAL_B]})
        self.start()
        self.tunnel()
        self.assertEqual([call[0] for call in self.connector.calls], [GLOBAL_A])

    def test_the_real_resolver_asks_for_af_inet_only(self) -> None:
        # G5: resolve_ipv4 asks getaddrinfo for AF_INET, port 443, TCP, and
        # the server's default resolver is it, called with AF_INET.
        real = socket.getaddrinfo
        asked: list[tuple[Any, ...]] = []

        def fake(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> Any:
            # Both spellings are caught, so neither ever reaches a real resolver.
            if host not in (ALLOWED, ALLOWED + "."):
                return real(host, port, family, type, proto, flags)
            asked.append((host, port, family, type, proto))
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
                    for address in (GLOBAL_A, GLOBAL_B, GLOBAL_A)]

        with mock.patch.object(server_module.socket, "getaddrinfo", side_effect=fake):
            self.assertEqual(server_module.resolve_ipv4(ALLOWED), [GLOBAL_A, GLOBAL_B])
            self.assertEqual(len(asked), 1)
            host, port, family, kind, proto = asked[0]
            self.assertEqual((host, str(port), family, kind, proto),
                             (ALLOWED + ".", "443", socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP))
            self.start(omit=("resolver",))
            self.tunnel()
        self.assertEqual(len(asked), 2)
        self.assertEqual(asked[1][2], socket.AF_INET)
        self.assertEqual([call[0] for call in self.connector.calls], [GLOBAL_A])

    def test_the_real_resolver_has_a_deadline(self) -> None:
        # G5: a lookup that hangs is abandoned after resolve_deadline_seconds
        # with 504, and nothing is connected.
        real = socket.getaddrinfo
        release = threading.Event()

        def hanging(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> Any:
            # Both spellings are caught, so neither ever reaches a real resolver.
            if host not in (ALLOWED, ALLOWED + "."):
                return real(host, port, family, type, proto, flags)
            release.wait(DEADLINE)
            raise OSError("released")

        with mock.patch.object(server_module.socket, "getaddrinfo", side_effect=hanging):
            self.start(Limits(resolve_deadline_seconds=0.3), omit=("resolver",))
            self.addCleanup(release.set)
            started = time.monotonic()
            status, head = self.request(connect_head(ALLOWED))
            elapsed = time.monotonic() - started
            release.set()
        self.assertEqual(head, refusal_head(504))
        self.assertLess(elapsed, DEADLINE / 2)
        self.assertEqual(self.connector.calls, [])
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason), (ALLOWED, "refused:resolve_timeout"))

    def test_the_real_resolver_asks_for_the_absolute_name(self) -> None:
        # G5/G6: the lookup is for name + "." and nothing else. A relative
        # name lets glibc retry it under each resolv.conf `search` domain
        # after an NXDOMAIN the client can force (any first label), so a
        # wildcard record in the owner's own zone could answer for a name the
        # allowlist never named. This spy never calls the real resolver.
        asked: list[Any] = []
        lock = threading.Lock()

        def spy(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> Any:
            with lock:
                asked.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (GLOBAL_A, 443))]

        names = (ALLOWED, "youtube.com", "r1---sn-abc.googlevideo.com", "nosuch.youtube.com")
        with mock.patch.object(server_module.socket, "getaddrinfo", side_effect=spy):
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(server_module.resolve_ipv4(name), [GLOBAL_A])
                    self.assertEqual(asked[-1], name + ".")
            # Acceptance through the server's default resolver: the tunnel is
            # made, to the checked address, after one absolute lookup.
            self.start(omit=("resolver",))
            self.tunnel("m.youtube.com")
        self.assertEqual(asked, [name + "." for name in names] + ["m.youtube.com."])
        for host in asked:
            self.assertTrue(host.endswith(".") and not host.endswith(".."), host)
        self.assertEqual([call[0] for call in self.connector.calls], [GLOBAL_A])


class ConnectTest(ForwarderTestCase):
    """G6: connect to a checked address on 443, never the name, never a second lookup."""

    def test_connects_to_an_address_from_the_answer_on_port_443(self) -> None:
        # G6
        self.fresh({ALLOWED: [GLOBAL_A, GLOBAL_B]})
        self.start(Limits(connect_timeout_seconds=7))
        client, upstream = self.tunnel()
        self.assertEqual(self.connector.calls, [(GLOBAL_A, 443, 7)])
        self.assertIs(type(self.connector.calls[0][0]), str)
        # And the tunnel is that connection.
        client.sendall(b"ping")
        self.assertEqual(recv_exact(upstream, 4), b"ping")
        upstream.sendall(b"pong")
        self.assertEqual(recv_exact(client, 4), b"pong")

    def test_no_second_lookup_after_connect(self) -> None:
        # G6: a resolver that answers differently the second time (DNS
        # rebinding) is never asked again.
        answers = [[GLOBAL_A], ["127.0.0.1"]]
        calls: list[tuple[str, int]] = []

        def rebinding(name: str, family: int) -> list[str]:
            calls.append((name, family))
            return answers[min(len(calls), 2) - 1]

        self.start(resolver=rebinding)
        client, upstream = self.tunnel()
        client.sendall(b"x" * 100)
        self.assertEqual(recv_exact(upstream, 100), b"x" * 100)
        upstream.sendall(b"y" * 100)
        self.assertEqual(recv_exact(client, 100), b"y" * 100)
        self.close_tunnel(client, upstream, 1)
        self.assertEqual(calls, [(ALLOWED, socket.AF_INET)])
        self.assertEqual([call[0] for call in self.connector.calls], [GLOBAL_A])

    def test_tries_the_next_checked_address_when_one_fails(self) -> None:
        # G6 (acceptance): a refused or timed-out first address falls through
        # to the next checked one, without another lookup.
        for label, failure in (("refused", ConnectionRefusedError("refused")), ("timeout", TimeoutError("timed out"))):
            with self.subTest(failure=label):
                self.fresh({ALLOWED: [GLOBAL_A, GLOBAL_B]})
                self.connector.fail[GLOBAL_A] = failure
                self.start()
                client, upstream = self.tunnel()
                self.assertEqual([call[:2] for call in self.connector.calls], [(GLOBAL_A, 443), (GLOBAL_B, 443)])
                self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])
                client.sendall(b"ok")
                self.assertEqual(recv_exact(upstream, 2), b"ok")

    def test_connect_failure_is_502_and_timeout_504(self) -> None:
        # G6: when every address fails, the last failure decides.
        refused = ConnectionRefusedError("refused")
        timeout = TimeoutError("timed out")
        cases = {
            "a.youtube.com": ((refused, refused), 502, "refused:connect_failed"),
            "b.youtube.com": ((timeout, timeout), 504, "refused:connect_timeout"),
            "c.youtube.com": ((timeout, refused), 502, "refused:connect_failed"),
            "d.youtube.com": ((refused, timeout), 504, "refused:connect_timeout"),
            "e.youtube.com": ((OSError("network unreachable"),), 502, "refused:connect_failed"),
            # socket.timeout is TimeoutError since 3.10; a second instance
            # catches an implementation that compares identities.
            "f.youtube.com": ((TimeoutError("timed out again"),), 504, "refused:connect_timeout"),
        }
        answers: dict[str, list[str] | BaseException] = {}
        for index, (name, (failures, _status, _reason)) in enumerate(cases.items()):
            addresses = [f"11.0.{index + 1}.{n + 1}" for n in range(len(failures))]
            answers[name] = addresses
        self.fresh(answers)
        for name, (failures, _status, _reason) in cases.items():
            for address, failure in zip(answers[name], failures, strict=True):
                self.connector.fail[str(address)] = failure
        self.start()
        for count, (name, (_failures, status, reason)) in enumerate(cases.items(), start=1):
            with self.subTest(name=name):
                self.assert_refused(connect_head(name), status)
                outcome = self.sink.wait(count)[count - 1]
                self.assertEqual((outcome.name, outcome.reason), (name, reason))
                self.assertEqual([call[0] for call in self.connector.calls if call[0] in answers[name]],
                                 answers[name])
        self.assertEqual(sorted(call[0] for call in self.resolver.calls), sorted(cases))

    def test_the_connector_never_sees_the_name(self) -> None:
        # G6: the connector gets an address string from the checked answer;
        # the default connector is create_connection to that address.
        self.start()
        for name in (ALLOWED, "youtube.com", "r1---sn-abc.googlevideo.com"):
            with self.subTest(name=name):
                self.tunnel(name)
        for address, port, _timeout in self.connector.calls:
            ipaddress.IPv4Address(address)
            self.assertEqual(port, 443)
            self.assertNotIn("youtube", address)
            self.assertNotIn("googlevideo", address)
        sentinel = object()
        with mock.patch.object(server_module.socket, "create_connection", return_value=sentinel) as create, \
                mock.patch.object(server_module.socket, "getaddrinfo", side_effect=AssertionError("a lookup")):
            self.assertIs(server_module.connect(GLOBAL_A, 443, 2.5), sentinel)
        self.assertEqual(create.call_count, 1)
        args, kwargs = create.call_args
        self.assertEqual(args[0], (GLOBAL_A, 443))
        self.assertEqual(kwargs.get("timeout", args[1] if len(args) > 1 else None), 2.5)


class HeadLimitsTest(ForwarderTestCase):
    """G7: the head within head_max_bytes and head_deadline_seconds; what follows is relayed unparsed."""

    def test_a_head_over_the_cap_is_refused_431(self) -> None:
        # G7: a head that fills the cap without its blank line is refused at
        # once (not at the deadline), and so is one that ends past the cap.
        cap = 256
        self.start(Limits(head_max_bytes=cap, head_deadline_seconds=5))
        line = b"CONNECT www.youtube.com:443 HTTP/1.1\r\n"
        filled = line + b"X: " + b"a" * (cap - len(line) - 3)
        self.assertEqual(len(filled), cap)
        started = time.monotonic()
        self.assert_refused(filled, 431)
        self.assertLess(time.monotonic() - started, 4)
        # Acceptance: a head of exactly cap bytes, blank line included.
        exact = line + b"X: " + b"a" * (cap - len(line) - 3 - len(HEAD_END)) + HEAD_END
        self.assertEqual(len(exact), cap)
        self.assertEqual(self.request(exact)[1], ACCEPTED)
        # One byte more, sent in one piece: the blank line arrives in the
        # same read, but the head is over the cap. A reset may replace the
        # status if the server stops reading, so the sink is the witness.
        over = line + b"X: " + b"a" * (cap - len(line) - 3 - len(HEAD_END) + 1) + HEAD_END
        self.assertEqual(len(over), cap + 1)
        status, _head = self.request(over)
        self.assertIn(status, (431, 0))

        def too_large() -> int:
            return [o.reason for o in self.sink.snapshot()].count("refused:head_too_large")

        wait_until(lambda: too_large() >= 2)
        self.assertEqual(too_large(), 2, self.sink.snapshot())
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])
        # Acceptance: the cap is on the head, not on the first read. A small
        # head followed, in the same send, by more bytes than the cap (a
        # client that doesn't wait for the 200) is accepted and the bytes
        # after the blank line all reach upstream.
        trailing = b"\x16\x03\x01" + b"t" * (cap * 4)
        client, upstream = self.tunnel(after_head=trailing)
        self.assertEqual(recv_exact(upstream, len(trailing)), trailing)
        expected = len(self.sink.snapshot()) + 1
        self.close_tunnel(client, upstream, expected)
        self.assertEqual(self.sink.snapshot()[expected - 1].upload_bytes, len(trailing))

    def test_a_slow_head_is_refused_408_at_the_deadline(self) -> None:
        # G7: the deadline runs from accept, not from the last byte, so a
        # client trickling header lines can't hold the connection open.
        deadline = 0.4
        self.start(Limits(head_deadline_seconds=deadline))
        partial = b"CONNECT www.youtube.com:443 HTTP/1.1\r\n"
        started = time.monotonic()
        status, head = self.request(partial)
        elapsed = time.monotonic() - started
        self.assertEqual(head, refusal_head(408))
        self.assertGreaterEqual(elapsed, deadline * 0.75)
        self.assertLess(elapsed, DEADLINE / 2)
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason), (None, "refused:head_timeout"))
        # Acceptance: a head in two pieces well inside the deadline.
        sock = self.client()
        sock.sendall(partial)
        time.sleep(0.1)
        sock.sendall(b"\r\n")
        self.assertEqual(recv_head(sock), ACCEPTED)
        # Trickling: a line every 0.1 s, each well inside the deadline.
        sock = self.client()
        started = time.monotonic()
        sock.sendall(partial)
        before = len(self.sink.snapshot())
        while time.monotonic() - started < 4 and len(self.sink.snapshot()) == before:
            time.sleep(0.1)
            try:
                sock.sendall(b"X: y\r\n")
            except OSError:
                break
        outcomes = self.sink.wait(before + 1, timeout=1)
        self.assertGreater(len(outcomes), before, "a trickling head was never cut")
        self.assertEqual(outcomes[before].reason, "refused:head_timeout")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])

    def test_bytes_after_the_head_are_relayed_unparsed_and_counted(self) -> None:
        # G7: what follows the blank line is the client's TLS: it goes
        # upstream first, byte for byte, even when it looks like another
        # request, and counts as upload.
        self.assertEqual(policy.split_head(b"CONNECT a:443 HTTP/1.1\r\n\r\nrest"),
                         (b"CONNECT a:443 HTTP/1.1\r\n\r\n", b"rest"))
        self.assertEqual(policy.split_head(b"A\r\n\r\nB\r\n\r\nC"), (b"A\r\n\r\n", b"B\r\n\r\nC"))
        self.assertEqual(policy.split_head(b"A\r\n\r\n"), (b"A\r\n\r\n", b""))
        self.assertIsNone(policy.split_head(b"CONNECT a:443 HTTP/1.1\r\n"))
        self.start()
        trailing = b"\x16\x03\x01\x00\x05hello" + connect_head("10.0.0.1") + b"\xff\x00"
        client, upstream = self.tunnel(after_head=trailing)
        self.assertEqual(recv_exact(upstream, len(trailing)), trailing)
        upstream.sendall(b"server-hello")
        # The client gets the 200 and then upstream's bytes, nothing between.
        self.assertEqual(recv_exact(client, 12), b"server-hello")
        self.close_tunnel(client, upstream, 1)
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.upload_bytes, outcome.download_bytes), (len(trailing), 12))
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)])
        self.assertEqual(len(self.connector.calls), 1)

    def test_a_connection_closed_before_any_byte_is_not_logged(self) -> None:
        # G7, G10: a connection that sends nothing (a health check) gets no
        # bytes, reaches the sink as refused:empty and leaves no log line; a
        # partial head that ends in EOF is malformed and is logged.
        buffer = io.StringIO()
        server_module.log_outcome(Outcome(None, "refused:empty", 0, 0, 0.0), buffer)
        self.assertEqual(buffer.getvalue(), "")
        self.start()
        sock = self.client()
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(read_to_eof(sock)[0], b"")
        outcome = self.sink.wait(1)[0]
        self.assertEqual(outcome, Outcome(None, "refused:empty", 0, 0, outcome.seconds))

        self.fresh()
        self.start(sink=None)
        empty = self.client()
        empty.shutdown(socket.SHUT_WR)
        self.assertEqual(read_to_eof(empty)[0], b"")
        partial = self.client()
        partial.sendall(b"CONNECT www.youtube.com:443 HT")
        partial.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_head(partial), refusal_head(400))
        lines = self.wait_lines(self.log, 1)
        time.sleep(SETTLE)
        self.assertEqual(self.log.lines(), ["forwarder refused malformed -"], lines)


class BudgetsTest(ForwarderTestCase):
    """G8: concurrent tunnels, tunnels per sliding minute, and open connections before the head."""

    def test_concurrent_tunnels_are_capped_503(self) -> None:
        # G8
        self.start(Limits(tunnels_max_concurrent=2))
        first = self.tunnel()
        self.tunnel("m.youtube.com")
        self.assert_refused(connect_head("youtube.com"), 503)
        busy = [o for o in self.sink.wait(1) if o.reason == "refused:busy"]
        self.assertEqual([(o.name, o.reason) for o in busy], [("youtube.com", "refused:busy")])
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET), ("m.youtube.com", socket.AF_INET)])
        # Acceptance: a finished tunnel frees its slot.
        self.close_tunnel(*first, expected=2)
        self.tunnel_when_free("youtube.com")

    def test_new_tunnels_per_minute_are_capped_429_and_recover(self) -> None:
        # G8: a sliding 60-second window; a 429 records nothing (or recovery
        # would move) and gives back the concurrency slot it took.
        self.start(Limits(tunnels_max_concurrent=1, tunnels_per_minute=2), clock=self.clock)

        def open_and_close() -> None:
            client, upstream = self.tunnel_when_free()
            self.close_tunnel(client, upstream, len(self.sink.snapshot()) + 1)

        open_and_close()                      # t=1000
        self.clock.now = 1030.0
        open_and_close()                      # t=1030
        self.clock.now = 1040.0
        count = len(self.sink.snapshot())
        self.assert_refused(connect_head(ALLOWED), 429)
        self.assertEqual(self.sink.wait(count + 1)[count].reason, "refused:rate")
        self.clock.now = 1061.0               # 1000 has left the window, the refused 1040 never entered it
        open_and_close()
        self.clock.now = 1062.0               # 1030 and 1061 are in the window
        self.assert_refused(connect_head(ALLOWED), 429)
        self.clock.now = 1091.0               # 1030 has left it
        open_and_close()
        self.assertEqual(self.resolver.calls, [(ALLOWED, socket.AF_INET)] * 4)

    def test_connections_over_the_hard_cap_are_closed_without_a_response(self) -> None:
        # G8: beyond connections_max open connections (before any head), a
        # new one is closed at once, with no response and no thread spent.
        self.start(Limits(connections_max=2, head_deadline_seconds=8))
        held_one = self.client()
        held_two = self.client()
        third = self.client()
        data, _reset = read_to_eof(third)
        self.assertEqual(data, b"")
        outcome = self.sink.wait(1)[0]
        self.assertEqual(outcome, Outcome(None, "refused:connections", 0, 0, 0.0))
        # Acceptance: the held connections are served.
        held_one.sendall(connect_head(ALLOWED))
        self.assertEqual(recv_head(held_one), ACCEPTED)
        # And a slot frees when a connection ends.
        held_two.close()
        self.assertTrue(wait_until(lambda: any(o.reason == "refused:empty" for o in self.sink.snapshot())))
        end = time.monotonic() + DEADLINE
        while True:
            sock = self.client()
            sock.sendall(connect_head("m.youtube.com"))
            head = recv_head(sock)
            if head or time.monotonic() > end:
                break
            time.sleep(0.05)
        self.assertEqual(head, ACCEPTED)

    def test_a_refused_request_consumes_no_tunnel_budget(self) -> None:
        # G8: refusals in steps 1 and 2 take neither the concurrency slot nor
        # a place in the per-minute window.
        self.start(Limits(tunnels_max_concurrent=1, tunnels_per_minute=2), clock=self.clock)
        refusals = (
            (b"GET / HTTP/1.1\r\n\r\n", 405),
            (connect_head(ALLOWED, port="80"), 403),
            (connect_head(GLOBAL_A), 400),
            (b"CONNECT www.youtube.com:443 HTTP/3\r\n\r\n", 400),
            (connect_head("-x.youtube.com"), 400),
            (connect_head("example.invalid"), 403),
            (connect_head("www.y\u00f6utube.com"), 400),
        )
        for data, status in refusals:
            with self.subTest(data=data):
                self.assert_refused(data, status)
        for _ in range(2):
            client, upstream = self.tunnel_when_free()
            self.close_tunnel(client, upstream, len(self.sink.snapshot()) + 1)
        # The window is real: a third tunnel in the same minute is refused.
        self.assert_refused(connect_head(ALLOWED), 429)

    def test_a_busy_refusal_takes_no_place_in_the_minute(self) -> None:
        # G8: the slot is taken before the minute, so a 503 records nothing
        # in the per-minute window (a client hammering a full forwarder must
        # not use up the minute for the tunnels that follow).
        self.start(Limits(tunnels_max_concurrent=1, tunnels_per_minute=3), clock=self.clock)
        held = self.tunnel()
        for _ in range(2):
            self.assert_refused(connect_head("m.youtube.com"), 503)
        self.assertEqual([o.reason for o in self.sink.wait(2)], ["refused:busy", "refused:busy"])
        self.close_tunnel(*held, expected=3)
        # Two more in the same minute: 1 + 2 = 3, the window's limit.
        for _ in range(2):
            client, upstream = self.tunnel_when_free()
            self.close_tunnel(client, upstream, len(self.sink.snapshot()) + 1)
        # The window is real: a fourth tunnel in the same minute is refused.
        self.assert_refused(connect_head(ALLOWED), 429)


class WindowTest(unittest.TestCase):
    """G8, G9: the sliding 60-second budget behind tunnels_per_minute and upload_bytes_per_minute."""

    def test_counts_within_a_sliding_minute(self) -> None:
        # G8
        clock = FakeClock()
        window = server_module.Window(3, clock)
        self.assertEqual([window.add() for _ in range(4)], [True, True, True, False])
        clock.now += 59.5
        self.assertIs(window.add(), False)
        clock.now += 1.0
        self.assertIs(window.add(), True)

    def test_amounts_and_refused_adds(self) -> None:
        # G9: an add that would pass the limit records nothing.
        clock = FakeClock()
        window = server_module.Window(1500, clock)
        self.assertIs(window.add(900), True)
        self.assertIs(window.add(601), False)
        self.assertIs(window.add(600), True)
        self.assertIs(window.add(1), False)
        self.assertIs(server_module.Window(10, clock).add(11), False)
        # The refused add at t+40 never entered the window, so at t+61 only
        # the one at t+30 is in it.
        clock = FakeClock()
        window = server_module.Window(2, clock)
        self.assertIs(window.add(), True)
        clock.now += 30
        self.assertIs(window.add(), True)
        clock.now += 10
        self.assertIs(window.add(), False)
        clock.now += 21
        self.assertIs(window.add(), True)

    def test_a_burst_of_tiny_adds_keeps_one_bucket_and_an_exact_total(self) -> None:
        # G9 (RS3-3): one entry per add would let a client sending one byte
        # per chunk grow the upload window past the forwarder's memory limit.
        clock = FakeClock()
        window = server_module.Window(50_000, clock)
        for step in range(50_000):
            clock.now = 1000.0 + step / 131072      # under 0.4 s in all
            self.assertIs(window.add(1), True)
        self.assertEqual(window.buckets, 1)
        # The total is exact: the 50,000 bytes fill the budget to the byte.
        self.assertIs(window.add(1), False)
        # Nothing leaves early: just under a minute after the latest add, the
        # budget is still full; a minute after it, all of it is back.
        latest = clock.now
        clock.now = latest + 59.99
        self.assertIs(window.add(1), False)
        clock.now = latest + 60.0
        self.assertIs(window.add(50_000), True)
        self.assertIs(window.add(1), False)

    def test_a_steady_trickle_keeps_the_buckets_bounded(self) -> None:
        # G9 (RS3-3): 1024 one-byte adds a second for 130 seconds (133,120
        # adds) stay within about one bucket per second of the window, and the
        # total in the window stays exact. Steps of 1/1024 s are exact in
        # binary, so the arithmetic below is too.
        clock = FakeClock()
        window = server_module.Window(70_000, clock)
        most = 0
        for step in range(1024 * 130):
            clock.now = 1000.0 + step / 1024
            self.assertIs(window.add(1), True, step)
            if step % 256 == 0:
                most = max(most, window.buckets)
        self.assertLessEqual(most, 62)
        self.assertLessEqual(window.buckets, 62)
        # At t = 1129 + 1023/1024 the buckets of seconds 1070 to 1129 are in
        # the window (1069's latest add is exactly a minute old): 60 buckets
        # of 1024 bytes. The rest of the budget fits exactly, and no more.
        self.assertIs(window.add(70_000 - 60 * 1024), True)
        self.assertIs(window.add(1), False)

    def test_a_refused_add_does_not_join_a_bucket(self) -> None:
        # G9: a refused add records nothing, bucket or not.
        clock = FakeClock()
        window = server_module.Window(10, clock)
        self.assertIs(window.add(6), True)
        clock.now += 0.5
        self.assertIs(window.add(5), False)
        self.assertIs(window.add(4), True)
        self.assertEqual(window.buckets, 1)
        # Both adds share the bucket, which leaves a minute after its latest
        # add (t+0.5), not after its first.
        clock.now = 1060.4
        self.assertIs(window.add(1), False)
        clock.now = 1060.5
        self.assertIs(window.add(10), True)


class RelayCapsTest(ForwarderTestCase):
    """G9: byte, idle and lifetime caps cut the tunnel; either side's EOF ends it normally."""

    def caps(self, **overrides: Any) -> Limits:
        values: dict[str, Any] = {
            "upload_bytes_per_tunnel": 1000,
            "upload_bytes_per_minute": 1500,
            "download_bytes_per_tunnel": 1000,
        }
        values.update(overrides)
        return Limits(**values)

    def test_upload_bytes_per_tunnel_cut(self) -> None:
        # G9: exactly the cap passes; the chunk that would pass it cuts.
        self.start(self.caps(), clock=self.clock)
        client, upstream = self.tunnel()
        client.sendall(b"u" * 600)
        self.assertEqual(recv_exact(upstream, 600), b"u" * 600)
        client.sendall(b"u" * 400)
        self.assertEqual(recv_exact(upstream, 400), b"u" * 400)
        upstream.sendall(b"alive")
        self.assertEqual(recv_exact(client, 5), b"alive")
        client.sendall(b"u")
        self.assertEqual(read_to_eof(upstream)[0], b"")
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason, outcome.upload_bytes), (ALLOWED, "cut:upload_cap", 1000))

    def test_upload_bytes_per_minute_cut_across_tunnels(self) -> None:
        # G9: the per-minute upload budget is shared by every tunnel.
        self.start(self.caps(), clock=self.clock)
        client, upstream = self.tunnel()
        client.sendall(b"a" * 900)
        self.assertEqual(recv_exact(upstream, 900), b"a" * 900)
        self.close_tunnel(client, upstream, 1)
        client, upstream = self.tunnel()
        client.sendall(b"b" * 600)
        self.assertEqual(recv_exact(upstream, 600), b"b" * 600)
        client.sendall(b"b")
        self.assertEqual(read_to_eof(upstream)[0], b"")
        outcome = self.sink.wait(2)[1]
        self.assertEqual((outcome.reason, outcome.upload_bytes), ("cut:upload_rate", 600))
        # Acceptance: the budget recovers when the minute has passed.
        self.clock.now += 61
        client, upstream = self.tunnel()
        client.sendall(b"c" * 900)
        self.assertEqual(recv_exact(upstream, 900), b"c" * 900)
        self.close_tunnel(client, upstream, 3)

    def test_a_chunk_cut_by_the_tunnel_cap_takes_nothing_from_the_minute(self) -> None:
        # G9: the per-tunnel cap is checked before the shared minute, so the
        # chunk that cuts a tunnel is neither relayed nor charged: the minute
        # still holds exactly what was relayed, and the rest of its budget
        # passes through later tunnels.
        self.start(self.caps(), clock=self.clock)
        client, upstream = self.tunnel()
        client.sendall(b"x" * 1001)
        relayed, _reset = read_to_eof(upstream)
        first = self.sink.wait(1)[0]
        self.assertEqual(first.reason, "cut:upload_cap")
        self.assertEqual(len(relayed), first.upload_bytes)
        left = 1500 - first.upload_bytes
        sent = 0
        count = 1
        while sent < left:
            size = min(1000, left - sent)
            client, upstream = self.tunnel()
            client.sendall(b"y" * size)
            self.assertEqual(recv_exact(upstream, size), b"y" * size, "the cut chunk was charged to the minute")
            count += 1
            self.close_tunnel(client, upstream, count)
            sent += size
        # And the minute is then really spent.
        client, upstream = self.tunnel()
        client.sendall(b"z")
        self.assertEqual(read_to_eof(upstream)[0], b"")
        self.assertEqual(self.sink.wait(count + 1)[count].reason, "cut:upload_rate")

    def test_download_bytes_per_tunnel_cut(self) -> None:
        # G9: exactly the cap passes; the chunk that would pass it cuts.
        self.start(self.caps(), clock=self.clock)
        client, upstream = self.tunnel()
        upstream.sendall(b"d" * 600)
        self.assertEqual(recv_exact(client, 600), b"d" * 600)
        upstream.sendall(b"d" * 400)
        self.assertEqual(recv_exact(client, 400), b"d" * 400)
        client.sendall(b"alive")
        self.assertEqual(recv_exact(upstream, 5), b"alive")
        upstream.sendall(b"d")
        self.assertEqual(read_to_eof(client)[0], b"")
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason, outcome.download_bytes),
                         (ALLOWED, "cut:download_cap", 1000))

    def test_idle_timeout_cuts(self) -> None:
        # G9: real time, a small idle timeout. Traffic either way keeps the
        # tunnel alive past it (acceptance); silence cuts it.
        # Traffic every 0.1 s against a 0.5 s idle timeout: a loaded runner
        # has five times the gap before a slow sleep could cut a live tunnel.
        idle = 0.5
        self.start(Limits(idle_timeout_seconds=idle, lifetime_seconds=30))
        client, upstream = self.tunnel()
        for _ in range(8):
            client.sendall(b"k")
            self.assertEqual(recv_exact(upstream, 1), b"k")
            time.sleep(0.1)
        upstream.sendall(b"alive")
        self.assertEqual(recv_exact(client, 5), b"alive")
        quiet = time.monotonic()
        data, _reset = read_to_eof(client)
        elapsed = time.monotonic() - quiet
        self.assertEqual(data, b"")
        self.assertGreaterEqual(elapsed, idle * 0.6)
        self.assertLess(elapsed, DEADLINE / 2)
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason), (ALLOWED, "cut:idle"))

    def test_lifetime_cuts_a_busy_tunnel(self) -> None:
        # G9: a tunnel that never goes idle still ends at its lifetime.
        lifetime = 0.6
        self.start(Limits(idle_timeout_seconds=5, lifetime_seconds=lifetime))
        started = time.monotonic()
        client, upstream = self.tunnel()
        client.sendall(b"t")
        self.assertEqual(recv_exact(upstream, 1), b"t")
        while time.monotonic() - started < 5 and not self.sink.snapshot():
            time.sleep(0.1)
            try:
                client.sendall(b"t")
            except OSError:
                break
        outcomes = self.sink.wait(1, timeout=1)
        self.assertTrue(outcomes, "a busy tunnel outlived its lifetime")
        self.assertEqual((outcomes[0].name, outcomes[0].reason), (ALLOWED, "cut:lifetime"))
        self.assertGreaterEqual(outcomes[0].seconds, lifetime * 0.8)
        self.assertLess(time.monotonic() - started, 4)

    def test_lifetime_runs_from_accept_not_from_the_head(self) -> None:
        # G9: the time a client takes over its head counts against the
        # lifetime, so a slow head can't extend a tunnel past it.
        lifetime = 1.2
        delay = 0.8
        self.start(Limits(idle_timeout_seconds=5, lifetime_seconds=lifetime, head_deadline_seconds=5))
        client = self.client()
        connected = time.monotonic()
        time.sleep(delay)
        client.sendall(connect_head(ALLOWED))
        self.assertEqual(recv_head(client), ACCEPTED)
        self.addCleanup(self.connector.upstreams[-1].close)
        while time.monotonic() - connected < 5 and not self.sink.snapshot():
            try:
                client.sendall(b"t")
            except OSError:
                break
            time.sleep(0.1)
        outcomes = self.sink.wait(1, timeout=1)
        self.assertTrue(outcomes, "a busy tunnel outlived its lifetime")
        self.assertEqual((outcomes[0].name, outcomes[0].reason), (ALLOWED, "cut:lifetime"))
        # About 1.2 s from accept; a lifetime counted from the head would
        # give about 2.0 s.
        self.assertGreaterEqual(outcomes[0].seconds, lifetime * 0.8)
        self.assertLess(outcomes[0].seconds, lifetime + delay / 2)

    def test_lifetime_cuts_a_quiet_tunnel_on_time(self) -> None:
        # G9: a tunnel that moves no byte after the accept, with a lifetime
        # shorter than the idle timeout, ends at its lifetime and says so
        # (not later, as "idle").
        lifetime = 0.5
        self.start(Limits(idle_timeout_seconds=5, lifetime_seconds=lifetime))
        client, upstream = self.tunnel()
        data, _reset = read_to_eof(client)
        self.assertEqual(data, b"")
        self.assertEqual(read_to_eof(upstream)[0], b"")
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason), (ALLOWED, "cut:lifetime"))
        self.assertGreaterEqual(outcome.seconds, lifetime * 0.8)
        self.assertLess(outcome.seconds, 1.5)
        # Acceptance: a tunnel with a lifetime to spare is not cut while it
        # stays within the idle timeout.
        self.fresh()
        self.start(Limits(idle_timeout_seconds=5, lifetime_seconds=30))
        client, upstream = self.tunnel()
        time.sleep(lifetime * 2)
        client.sendall(b"still here")
        self.assertEqual(recv_exact(upstream, 10), b"still here")
        self.assertEqual(self.sink.snapshot(), [])

    def test_idle_timeout_cuts_a_peer_that_stops_reading(self) -> None:
        # G9: a send to a peer that reads nothing is bounded by the idle
        # timeout too, not only by the lifetime, so a stalled peer can't pin
        # a tunnel slot for the whole lifetime. The client here never reads
        # and has a small receive buffer; the upstream keeps sending, so the
        # forwarder ends up blocked in its send to the client.
        idle = 0.5
        self.start(Limits(idle_timeout_seconds=idle, lifetime_seconds=30))
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(DEADLINE)
        client.connect(("127.0.0.1", self.port))
        client.sendall(connect_head(ALLOWED))
        self.assertEqual(recv_head(client), ACCEPTED)
        upstream = self.connector.upstreams[-1]
        self.addCleanup(upstream.close)
        # Well under download_bytes_per_tunnel (16 MiB), far more than the
        # socket buffers on either side hold.
        total = 8 << 20
        writer: dict[str, Any] = {}

        def write() -> None:
            sent = 0
            piece = b"d" * 65536
            try:
                while sent < total:
                    sent += upstream.send(piece)
                    writer["last"] = time.monotonic()
                writer["result"] = "complete"
            except OSError as error:
                writer["result"] = type(error).__name__

        thread = threading.Thread(target=write, daemon=True)
        started = time.monotonic()
        thread.start()
        outcomes = self.sink.wait(1)
        thread.join(DEADLINE)
        self.assertTrue(outcomes, "a peer that stopped reading kept the tunnel open past the idle timeout")
        outcome = outcomes[0]
        self.assertEqual((outcome.name, outcome.reason), (ALLOWED, "cut:idle"))
        self.assertLess(time.monotonic() - started, DEADLINE / 2)
        # The forwarder stopped reading the upstream because its send to the
        # client was stuck, so the writer never got its 8 MiB through: the
        # cut came from the send, not from a quiet receive side.
        self.assertNotEqual(writer.get("result"), "complete", "the forwarder never blocked on the client")
        self.assertLess(outcome.download_bytes, total)

    def test_a_reset_ends_the_tunnel_ok_and_logs_nothing(self) -> None:
        # G9/G10: a peer that resets (or a send that fails) has gone, which
        # ends the tunnel as EOF would: reason "ok", no log line, no error.
        recorded = OutcomeSink()

        def sink(outcome: Outcome) -> None:
            server_module.log_outcome(outcome, self.log)
            recorded(outcome)

        self.start(sink=sink)
        client, upstream = self.tunnel()
        client.sendall(b"c" * 10)
        self.assertEqual(recv_exact(upstream, 10), b"c" * 10)
        # SO_LINGER on with a zero timeout: close() sends a RST, not a FIN.
        # struct linger is two ints on Linux and two u_shorts on Windows.
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        client.close()
        self.assertEqual(read_to_eof(upstream)[0], b"")
        outcome = recorded.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason, outcome.upload_bytes), (ALLOWED, "ok", 10))
        time.sleep(SETTLE)
        self.assertEqual(self.log.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_eof_on_either_side_ends_the_tunnel_ok(self) -> None:
        # G9: TLS clients don't half-close, so one side's EOF ends both.
        self.start(clock=self.clock)
        client, upstream = self.tunnel()
        client.sendall(b"c" * 10)
        self.assertEqual(recv_exact(upstream, 10), b"c" * 10)
        upstream.sendall(b"s" * 20)
        self.assertEqual(recv_exact(client, 20), b"s" * 20)
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(read_to_eof(upstream)[0], b"")
        self.assertEqual(read_to_eof(client)[0], b"")
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.reason, outcome.upload_bytes, outcome.download_bytes), ("ok", 10, 20))

        client, upstream = self.tunnel()
        upstream.sendall(b"bye")
        self.assertEqual(recv_exact(client, 3), b"bye")
        upstream.close()
        self.assertEqual(read_to_eof(client)[0], b"")
        outcome = self.sink.wait(2)[1]
        self.assertEqual((outcome.reason, outcome.upload_bytes, outcome.download_bytes), ("ok", 0, 3))

    def test_the_sink_sees_byte_counts_and_duration(self) -> None:
        # G9: one Outcome per connection, with the name, the bytes each way
        # (the bytes after the head included) and the seconds from accept.
        self.start()
        started = time.monotonic()
        client, upstream = self.tunnel(after_head=b"12345")
        self.assertEqual(recv_exact(upstream, 5), b"12345")
        client.sendall(b"u" * 100)
        self.assertEqual(recv_exact(upstream, 100), b"u" * 100)
        upstream.sendall(b"d" * 300)
        self.assertEqual(recv_exact(client, 300), b"d" * 300)
        time.sleep(0.3)
        self.close_tunnel(client, upstream, 1)
        elapsed = time.monotonic() - started
        time.sleep(SETTLE)
        outcomes = self.sink.snapshot()
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual((outcome.name, outcome.reason, outcome.upload_bytes, outcome.download_bytes),
                         (ALLOWED, "ok", 105, 300))
        self.assertGreaterEqual(outcome.seconds, 0.25)
        self.assertLessEqual(outcome.seconds, elapsed + 0.5)
        # A refusal moves no bytes.
        self.assert_refused(connect_head("example.invalid"), 403)
        refused = self.sink.wait(2)[1]
        self.assertEqual((refused.upload_bytes, refused.download_bytes), (0, 0))
        self.assertGreaterEqual(refused.seconds, 0.0)

    def test_the_forwarders_upload_window_is_bucketed(self) -> None:
        # G9 (RS3-3, M3-1): WindowTest pins Window's own default; this pins
        # that the Forwarder's real upload window uses buckets too. One entry
        # per add there would let a client relaying one byte per send grow it
        # past the forwarder's memory limit.
        server = self.start(clock=self.clock)
        window = server._upload_window
        self.assertEqual(window.limit, DEFAULT_LIMITS.upload_bytes_per_minute)
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        far.settimeout(DEADLINE)
        counts = [0, 0]
        end_of_life = time.monotonic() + 60 * DEADLINE
        # 4096 one-byte chunks through the relay's own upload path, all
        # within half a second of the window's clock. Read as they go: on
        # Linux each one-byte send holds a whole buffer of the socketpair's
        # send budget until it is read.
        received = b""
        for step in range(4096):
            self.clock.now = 1000.0 + step / 8192
            server._upload(near, b"x", end_of_life, counts)
            if step % 64 == 63:
                received += recv_exact(far, 64)
        self.assertEqual(counts[0], 4096)
        self.assertEqual(received, b"x" * 4096)
        self.assertEqual(window.buckets, 1)
        # A steady trickle, 64 one-byte adds a second for 130 seconds, stays
        # at about one bucket per second of the window.
        most = 0
        for step in range(64 * 130):
            self.clock.now = 1001.0 + step / 64
            self.assertIs(window.add(1), True, step)
            most = max(most, window.buckets)
        self.assertLessEqual(most, 62)

    def test_connection_attempts_stop_at_the_lifetime(self) -> None:
        # G8/G9 (RS3b-3): the tunnel slot is held from step 3, so attempts to
        # an answer full of unresponsive addresses stop at the lifetime from
        # accept, not after connect_timeout_seconds per address.
        slow = [f"11.0.9.{n}" for n in range(1, 7)]
        self.fresh({"slow.youtube.com": slow, ALLOWED: [GLOBAL_A]})
        attempts: list[tuple[str, float]] = []
        lock = threading.Lock()

        def connector(address: str, port: int, timeout: float) -> socket.socket:
            with lock:
                attempts.append((address, timeout))
            if address in slow:
                time.sleep(timeout)
                raise TimeoutError("timed out")
            return self.connector(address, port, timeout)

        # A lifetime that isn't a multiple of the connect timeout, so the last
        # attempt must be shortened to what is left: 2 s, then 1 s.
        connect_timeout = 2
        lifetime = 3
        self.start(
            Limits(connect_timeout_seconds=connect_timeout, lifetime_seconds=lifetime, tunnels_max_concurrent=1),
            connector=connector,
        )
        started = time.monotonic()
        sock = self.client()
        sock.sendall(connect_head("slow.youtube.com"))
        head = recv_head(sock)
        elapsed = time.monotonic() - started
        self.assertEqual(head, refusal_head(504))
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason), ("slow.youtube.com", "refused:connect_timeout"))
        # Six addresses at two seconds each would take twelve seconds, and
        # two full attempts four; the attempts end with the lifetime.
        self.assertLess(outcome.seconds, lifetime + 0.9)
        self.assertLess(elapsed, lifetime + 0.9)
        with lock:
            made = list(attempts)
        self.assertLess(len(made), len(slow), made)
        self.assertLessEqual(sum(timeout for _address, timeout in made), lifetime + 0.1, made)
        for _address, timeout in made:
            self.assertLessEqual(timeout, connect_timeout)
        # Acceptance: the slot is free again within the lifetime's bound, and
        # a responsive answer connects with the full connect timeout.
        client, upstream = self.tunnel_when_free()
        self.assertEqual(self.connector.calls[-1], (GLOBAL_A, 443, float(connect_timeout)))
        client.sendall(b"ok")
        self.assertEqual(recv_exact(upstream, 2), b"ok")


class LoggingTest(ForwarderTestCase):
    """G10: `forwarder refused|cut <reason> <name>` only, never payload or header text."""

    LINE = re.compile(r"^forwarder (refused|cut) [a-z_]+ ([a-z0-9.-]+|-)$")

    def test_refusals_log_name_and_reason_only(self) -> None:
        # G10
        self.fresh({ALLOWED: [GLOBAL_A], "i.youtube.com": ["10.0.0.1"], "m.youtube.com": OSError("no")})
        self.start(sink=None)
        requests = (
            (connect_head("example.invalid", headers=b"Host: example.invalid:443\r\nUser-Agent: header-marker\r\n"),
             "forwarder refused unlisted example.invalid"),
            (connect_head("192.0.2.1"), "forwarder refused literal 192.0.2.1"),
            (connect_head(ALLOWED, port="80"), "forwarder refused port www.youtube.com"),
            (b"GET / HTTP/1.1\r\nHost: www.youtube.com\r\nCookie: cookie-marker\r\n\r\n", "forwarder refused method -"),
            (connect_head("-bad-.youtube.com"), "forwarder refused bad_name -"),
            (connect_head(ALLOWED, version="HTTP/9.9"), "forwarder refused malformed -"),
            (connect_head("i.youtube.com"), "forwarder refused not_global i.youtube.com"),
            (connect_head("m.youtube.com"), "forwarder refused resolve_failed m.youtube.com"),
        )
        for count, (data, _line) in enumerate(requests, start=1):
            self.request(data)
            self.wait_lines(self.log, count)
        time.sleep(SETTLE)
        lines = self.log.lines()
        self.assertEqual(lines, [line for _data, line in requests])
        for line in lines:
            self.assertRegex(line, self.LINE)
        for marker in ("header-marker", "cookie-marker", "Host", "User-Agent", "HTTP/"):
            self.assertNotIn(marker, self.log.getvalue())
        self.assertEqual(self.stderr.getvalue(), "")

    def test_cuts_log_name_and_reason_only(self) -> None:
        # G10
        self.start(Limits(download_bytes_per_tunnel=100, upload_bytes_per_tunnel=100), clock=self.clock, sink=None)
        client, upstream = self.tunnel()
        upstream.sendall(b"PAYLOAD-MARKER-" * 10)
        read_to_eof(client)
        self.wait_lines(self.log, 1)
        client, upstream = self.tunnel("m.youtube.com")
        client.sendall(b"UPLOAD-MARKER--" * 10)
        read_to_eof(upstream)
        self.wait_lines(self.log, 2)
        time.sleep(SETTLE)
        self.assertEqual(self.log.lines(), ["forwarder cut download_cap www.youtube.com",
                                            "forwarder cut upload_cap m.youtube.com"])
        self.assertNotIn("MARKER", self.log.getvalue())
        buffer = io.StringIO()
        server_module.log_outcome(Outcome("www.youtube.com", "cut:idle", 1, 2, 0.5), buffer)
        self.assertEqual(buffer.getvalue(), "forwarder cut idle www.youtube.com\n")

    def test_accepted_tunnels_log_nothing(self) -> None:
        # G10
        buffer = io.StringIO()
        server_module.log_outcome(Outcome(ALLOWED, "ok", 10, 20, 1.0), buffer)
        self.assertEqual(buffer.getvalue(), "")
        self.start(sink=None)
        client, upstream = self.tunnel()
        client.sendall(b"hello")
        self.assertEqual(recv_exact(upstream, 5), b"hello")
        upstream.sendall(b"world")
        self.assertEqual(recv_exact(client, 5), b"world")
        upstream.close()
        self.assertEqual(read_to_eof(client)[0], b"")
        time.sleep(SETTLE)
        # A refusal afterwards proves the log is live, and is its only line.
        self.request(b"GET / HTTP/1.1\r\n\r\n")
        self.wait_lines(self.log, 1)
        time.sleep(SETTLE)
        self.assertEqual(self.log.lines(), ["forwarder refused method -"])

    def test_unvalidated_text_is_logged_as_a_dash(self) -> None:
        # G10: only text that matched the hostname grammar is ever printed.
        cases = {
            "www.youtube.com": "www.youtube.com",
            "WWW.Example.invalid": "WWW.Example.invalid",
            "192.0.2.1": "192.0.2.1",
            "a" * 253: "a" * 253,
            "a" * 254: None,
            "": None,
            "a b": None,
            "a\nb": None,
            "a\rb": None,
            "a\x1b[2Jb": None,
            "[::1]": None,
            "::1": None,
            "ex_ample.com": None,
            "ex\u00e4mple.com": None,
            "a/b": None,
            "%2e": None,
            "a:443": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(policy.loggable_name(text), expected)
        buffer = io.StringIO()
        server_module.log_outcome(Outcome(None, "refused:malformed", 0, 0, 0.0), buffer)
        self.assertEqual(buffer.getvalue(), "forwarder refused malformed -\n")
        self.start(sink=None)
        requests = (
            (b"CONNECT evil\x1b[2J.youtube.com:443 HTTP/1.1\r\n\r\n", r"forwarder refused (bad_name|malformed) -"),
            (b"POST /x%0d%0aforged HTTP/1.1\r\n\r\n", r"forwarder refused method -"),
            (b"CONNECT ex%41mple.youtube.com:443 HTTP/1.1\r\n\r\n", r"forwarder refused bad_name -"),
            (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", r"forwarder refused literal -"),
        )
        for count, (data, _pattern) in enumerate(requests, start=1):
            self.request(data)
            self.wait_lines(self.log, count)
        time.sleep(SETTLE)
        lines = self.log.lines()
        self.assertEqual(len(lines), len(requests), lines)
        for line, (_data, pattern) in zip(lines, requests, strict=True):
            self.assertRegex(line, "^" + pattern + "$")
        for marker in ("\x1b", "%0d", "forged", "%41", "::1"):
            self.assertNotIn(marker, self.log.getvalue())

    def test_relayed_bytes_never_reach_the_log(self) -> None:
        # G10: neither the bytes after the head nor anything relayed, even
        # text shaped like a log line.
        self.start(Limits(download_bytes_per_tunnel=200), clock=self.clock, sink=None)
        client, upstream = self.tunnel(after_head=b"AFTER-HEAD-MARKER\n")
        self.assertEqual(recv_exact(upstream, 18), b"AFTER-HEAD-MARKER\n")
        upload = b"UPLOAD-MARKER\nforwarder refused fake -\n"
        client.sendall(upload)
        self.assertEqual(recv_exact(upstream, len(upload)), upload)
        upstream.sendall(b"DOWNLOAD-MARKER\n")
        self.assertEqual(recv_exact(client, 16), b"DOWNLOAD-MARKER\n")
        upstream.sendall(b"Z" * 300)
        read_to_eof(client)
        self.wait_lines(self.log, 1)
        time.sleep(SETTLE)
        self.assertEqual(self.log.lines(), ["forwarder cut download_cap www.youtube.com"])
        for marker in ("MARKER", "fake", "ZZZ"):
            self.assertNotIn(marker, self.log.getvalue())
            self.assertNotIn(marker, self.stderr.getvalue())

    def test_the_default_sink_is_the_log_and_a_sink_replaces_it(self) -> None:
        # G10: no sink means log_outcome on the log; a sink replaces the log
        # entirely (the product check's measurement); no log means stderr,
        # looked up when the line is written.
        self.start(sink=None)
        self.request(b"GET / HTTP/1.1\r\n\r\n")
        self.assertEqual(self.wait_lines(self.log, 1), ["forwarder refused method -"])

        self.fresh()
        quiet = LogCapture()
        self.start(log=quiet)
        self.request(b"GET / HTTP/1.1\r\n\r\n")
        outcome = self.sink.wait(1)[0]
        self.assertEqual((outcome.name, outcome.reason, outcome.upload_bytes, outcome.download_bytes),
                         (None, "refused:method", 0, 0))
        time.sleep(SETTLE)
        self.assertEqual(quiet.getvalue(), "")

        self.fresh()
        self.start(sink=None, omit=("log",))
        later = LogCapture()
        with contextlib.redirect_stderr(later):
            self.request(b"GET / HTTP/1.1\r\n\r\n")
            self.assertEqual(self.wait_lines(later, 1), ["forwarder refused method -"])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_the_log_checks_every_name_again(self) -> None:
        # G10: whatever builds an Outcome, a name outside the hostname grammar
        # is printed as '-', both by log_outcome and by the server's default
        # sink; and a 405's name is the host part only when it is valid.
        for name in ("a\nforged", "a b", "ev;il", "x\x1b[2J"):
            with self.subTest(name=name):
                buffer = io.StringIO()
                server_module.log_outcome(Outcome(name, "refused:unlisted", 0, 0, 0.0), buffer)
                self.assertEqual(buffer.getvalue(), "forwarder refused unlisted -\n")
        self.assert_refusal_raised(b"POST ev;il:443 HTTP/1.1\r\n\r\n", 405, "method", None)
        self.assert_refusal_raised(b"POST a\x1b[2J:443 HTTP/1.1\r\n\r\n", 400, "malformed", None)
        # Acceptance: a valid host part is kept.
        self.assert_refusal_raised(b"POST www.example.invalid:443 HTTP/1.1\r\n\r\n", 405, "method",
                                   "www.example.invalid")
        server = self.start(sink=None)
        server.sink(Outcome("a\nforged", "cut:idle", 0, 0, 0.0))
        self.request(b"POST ev;il:443 HTTP/1.1\r\n\r\n")
        self.request(b"POST www.example.invalid:443 HTTP/1.1\r\n\r\n")
        lines = self.wait_lines(self.log, 3)
        time.sleep(SETTLE)
        self.assertEqual(self.log.lines(), ["forwarder cut idle -", "forwarder refused method -",
                                            "forwarder refused method www.example.invalid"], lines)
        self.assertNotIn("forged", self.log.getvalue())
        self.assertNotIn("ev;il", self.log.getvalue())

    def test_every_line_is_flushed(self) -> None:
        # G10: a line still in a block-buffered stream when the container is
        # stopped is lost, so each one is flushed as it is written.
        class FlushRecorder(io.TextIOBase):
            def __init__(self) -> None:
                super().__init__()
                self._lock = threading.Lock()
                self.pending = ""
                self.flushed = ""

            def write(self, text: str) -> int:
                with self._lock:
                    self.pending += text
                return len(text)

            def flush(self) -> None:
                with self._lock:
                    self.flushed += self.pending
                    self.pending = ""

        stream = FlushRecorder()
        server_module.log_outcome(Outcome(ALLOWED, "refused:busy", 0, 0, 0.0), stream)
        self.assertEqual((stream.pending, stream.flushed), ("", "forwarder refused busy www.youtube.com\n"))
        stream = FlushRecorder()
        self.start(sink=None, log=stream)
        self.request(b"GET / HTTP/1.1\r\n\r\n")
        self.assertTrue(wait_until(lambda: "forwarder refused method -\n" in stream.flushed),
                        f"the line was not flushed: pending {stream.pending!r}")
        self.assertEqual(stream.pending, "")

    def test_an_unexpected_error_logs_its_type_only(self) -> None:
        # G10: a bug escaping as an exception is reported as one line naming
        # its type: no traceback and no message, either of which could quote
        # bytes a client sent.
        def broken_sink(outcome: Outcome) -> None:
            raise RuntimeError("secret-token from the client")

        self.start(sink=broken_sink, omit=("log",))
        status, _head = self.request(b"GET / HTTP/1.1\r\nCookie: secret-token\r\n\r\n")
        self.assertEqual(status, 405)
        self.assertTrue(wait_until(lambda: self.stderr.getvalue().endswith("\n")), "nothing was reported")
        time.sleep(SETTLE)
        self.assertEqual(self.stderr.getvalue(), "forwarder error RuntimeError\n")
        # With a log injected, the line goes there and stderr stays quiet.
        self.fresh()
        before = self.stderr.getvalue()
        self.start(sink=broken_sink)
        self.request(b"GET / HTTP/1.1\r\n\r\n")
        self.assertEqual(self.wait_lines(self.log, 1), ["forwarder error RuntimeError"])
        time.sleep(SETTLE)
        self.assertEqual(self.stderr.getvalue(), before)

    def test_log_lines_are_rate_limited(self) -> None:
        # G10/G8: refusals cost no tunnel budget, so the log has its own: at
        # most log_lines_per_minute lines per sliding minute, the rest
        # dropped and counted, and the count printed once there is room.
        server = self.start(Limits(log_lines_per_minute=5), clock=self.clock, sink=None)
        # Outcomes that log nothing take no budget.
        for _ in range(10):
            server.sink(Outcome(ALLOWED, "ok", 1, 1, 0.1))
            server.sink(Outcome(None, "refused:empty", 0, 0, 0.0))
        for _ in range(50):
            server.sink(Outcome("example.invalid", "refused:unlisted", 0, 0, 0.0))
        self.assertEqual(self.log.lines(), ["forwarder refused unlisted example.invalid"] * 5)
        # Still the same minute: dropped and counted.
        self.clock.now += 59
        server.sink(Outcome(None, "refused:method", 0, 0, 0.0))
        self.assertEqual(len(self.log.lines()), 5)
        # A minute after the five: the count, then the line.
        self.clock.now += 2
        server.sink(Outcome(None, "refused:method", 0, 0, 0.0))
        self.assertEqual(self.log.lines()[5:], ["forwarder log suppressed 46", "forwarder refused method -"])
        server.sink(Outcome(None, "refused:method", 0, 0, 0.0))
        self.assertEqual(self.log.lines()[7:], ["forwarder refused method -"])
        # The live server's own refusals go through the same budget.
        self.fresh()
        self.start(Limits(log_lines_per_minute=3), clock=self.clock, sink=None)
        for _ in range(10):
            self.assert_refused(b"GET / HTTP/1.1\r\n\r\n", 405)
        self.wait_lines(self.log, 3)
        time.sleep(SETTLE)
        self.assertEqual(self.log.lines(), ["forwarder refused method -"] * 3)


class ServerClassTest(ForwarderTestCase):
    """Supporting (SPEC 3.2): the server class behaves like FetchServer where it must, and binding looks nothing up."""

    def test_class_attributes_match_fetch_server(self) -> None:
        # Daemon workers and no join on close, so a stop never waits on a
        # tunnel; no SO_REUSEADDR on Windows, where it lets a second process
        # take the port.
        self.assertIs(Forwarder.daemon_threads, True)
        self.assertIs(Forwarder.block_on_close, False)
        self.assertEqual(Forwarder.allow_reuse_address, sys.platform != "win32")
        self.assertTrue(issubclass(Forwarder, server_module.socketserver.ThreadingTCPServer))
        self.assertTrue(issubclass(server_module.TunnelHandler, server_module.socketserver.BaseRequestHandler))

    def test_binding_does_no_reverse_lookup(self) -> None:
        # TCPServer.server_bind only: socketserver's HTTP servers call
        # getfqdn(), a reverse DNS query nobody asked for.
        with mock.patch.object(server_module.socket, "getfqdn", side_effect=AssertionError("getfqdn was called")):
            self.start()
            self.assertEqual(self.request(connect_head(ALLOWED))[1], ACCEPTED)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.port, 0)


class ImportsTest(unittest.TestCase):
    """The forwarder's process holds the stdlib modules it needs and its own package, nothing else (SPEC 3.2)."""

    def sources(self) -> list[Path]:
        return sorted(PACKAGE.glob("*.py")) + [FORWARDER_SCRIPT]

    @staticmethod
    def imports(path: Path) -> Iterator[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                yield from (alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # A relative import stays inside the package.
                yield OWN_PACKAGE if node.level else (node.module or "")

    def test_imports_are_exactly_the_allowed_stdlib_modules(self) -> None:
        # G1 to G10 rest on this: no library parser, no file, no subprocess.
        names = [path.name for path in self.sources()]
        self.assertEqual(names, ["__init__.py", "__main__.py", "policy.py", "server.py", "forwarder.py"])
        for path in self.sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module in self.imports(path):
                with self.subTest(file=path.name, module=module):
                    self.assertFalse(module == "strange_ringtone" or module.startswith("strange_ringtone."))
                    self.assertFalse(module.startswith("strange_ringtone_fetch"))
                    own = module == OWN_PACKAGE or module.startswith(OWN_PACKAGE + ".")
                    self.assertTrue(own or module in ALLOWED_IMPORTS, f"{path.name} imports {module}")
            # Nor around the import statement: no dynamic import, no file.
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    with self.subTest(file=path.name, name=node.id):
                        self.assertNotIn(node.id, {"__import__", "open", "exec", "eval", "importlib"})


class LimitsTableTest(unittest.TestCase):
    """The README names every forwarder limit with its value (SPEC 8), and the defaults are sane."""

    def test_readme_names_every_limit_with_its_value(self) -> None:
        readme = helpers.ROOT / "README.md"
        if not readme.is_file():
            # The image ships no README; the host run checks it.
            self.skipTest("README.md is not here (in-image run)")
        text = readme.read_text(encoding="utf-8")
        self.assertIn("\n#### Forwarder limits\n", text, "the README has no '#### Forwarder limits' section")
        section = text.split("\n#### Forwarder limits\n", 1)[1]
        section = re.split(r"\n#{1,6} ", section, maxsplit=1)[0]
        rows = re.findall(r"^\| `([a-z_]+)` \| (\d+) \|", section, re.MULTILINE)
        self.assertEqual([name for name, _value in rows], LIMIT_FIELDS, "one row per field, in the dataclass's order")
        self.assertEqual({name: int(value) for name, value in rows}, policy.limits_table())

    def test_defaults_are_positive_ints(self) -> None:
        fields = dataclasses.fields(Limits)
        self.assertEqual([field.name for field in fields], LIMIT_FIELDS)
        for field in fields:
            with self.subTest(field=field.name):
                value = getattr(DEFAULT_LIMITS, field.name)
                self.assertIs(type(value), int)
                self.assertGreater(value, 0)
        self.assertEqual(DEFAULT_LIMITS, Limits())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            DEFAULT_LIMITS.head_max_bytes = 1  # type: ignore[misc]
        table = policy.limits_table()
        self.assertEqual(list(table), LIMIT_FIELDS)
        self.assertEqual(table, {name: getattr(DEFAULT_LIMITS, name) for name in LIMIT_FIELDS})


class EnvironmentRecorder(dict[str, str]):
    """An os.environ stand-in that records every variable looked up, and '*' for any walk over all of them."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.looked_up: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.looked_up.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self.looked_up.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.looked_up.add(key)
        return super().__contains__(key)

    def __iter__(self) -> Iterator[str]:
        self.looked_up.add("*")
        return super().__iter__()

    def keys(self) -> Any:
        self.looked_up.add("*")
        return super().keys()

    def items(self) -> Any:
        self.looked_up.add("*")
        return super().items()

    def values(self) -> Any:
        self.looked_up.add("*")
        return super().values()

    def copy(self) -> dict[str, str]:
        self.looked_up.add("*")
        return dict(super().items())


class FakeServer:
    """Stands in for Forwarder in main(): records the address and stops like Ctrl-C."""

    addresses: list[Any] = []
    closed = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        FakeServer.addresses.append(args[0] if args else kwargs.get("address"))

    def serve_forever(self, *args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        FakeServer.closed += 1


def subprocess_env() -> dict[str, str]:
    # No environment but what the interpreter needs to start, as
    # test_fetcher_config's checkout-entry test does.
    env = {"PATH": os.environ.get("PATH", "")}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


class EntryTest(unittest.TestCase):
    """The forwarder's entry: --bind only, fixed port 3128, no environment, only its own package."""

    def setUp(self) -> None:
        FakeServer.addresses = []
        FakeServer.closed = 0

    def test_bind_must_be_an_ip_literal(self) -> None:
        # A host name would be resolved by bind(), a lookup nobody asked for.
        with mock.patch.object(forwarder_main, "Forwarder", side_effect=AssertionError("started")):
            for value in ("localhost", "example.internal", "fe80::1%eth0", ""):
                with self.subTest(value=value):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                        forwarder_main.main(["--bind", value])
                    self.assertEqual(raised.exception.code, 2)

    def test_main_starts_on_the_fixed_port_and_stops_on_keyboard_interrupt(self) -> None:
        log = io.StringIO()
        with mock.patch.object(forwarder_main, "Forwarder", FakeServer), \
                mock.patch.object(signal, "signal") as installed, contextlib.redirect_stderr(log):
            self.assertEqual(forwarder_main.main(["--bind", "127.0.0.1"]), 0)
            self.assertEqual(forwarder_main.main([]), 0)
        self.assertEqual(FakeServer.addresses, [("127.0.0.1", 3128), ("0.0.0.0", 3128)])
        self.assertEqual(policy.PORT, 3128)
        self.assertEqual(FakeServer.closed, 2)
        self.assertIn("strange_ringtone_forwarder listening on 127.0.0.1:3128", log.getvalue())
        # SIGTERM stops it cleanly: the handler raises SystemExit(0).
        handlers = [call.args[1] for call in installed.call_args_list if call.args[0] == signal.SIGTERM]
        self.assertTrue(handlers, "no SIGTERM handler installed")
        with self.assertRaises(SystemExit) as raised:
            handlers[0](signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 0)

    def test_a_bind_failure_exits_1(self) -> None:
        log = io.StringIO()
        with mock.patch.object(forwarder_main, "Forwarder", side_effect=OSError("address in use")), \
                mock.patch.object(signal, "signal"), contextlib.redirect_stderr(log):
            self.assertEqual(forwarder_main.main(["--bind", "127.0.0.1"]), 1)
        self.assertIn("strange_ringtone_forwarder: cannot start: address in use", log.getvalue())

    def test_main_reads_no_environment_variable(self) -> None:
        # The forwarder has no setting, so nothing in a shared .env can
        # loosen it. argparse's own lookups are the stdlib's, not settings:
        # the terminal width (COLUMNS, LINES, shutil.get_terminal_size) and
        # the message language (LANGUAGE, LC_ALL, LC_MESSAGES, LANG, gettext).
        environ = EnvironmentRecorder({"HTTPS_PROXY": "http://example.internal:3128", "FORWARDER_PORT": "80"})
        with mock.patch.object(os, "environ", environ), \
                mock.patch.object(forwarder_main, "Forwarder", FakeServer), \
                mock.patch.object(signal, "signal"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(forwarder_main.main(["--bind", "127.0.0.1"]), 0)
        self.assertEqual(FakeServer.addresses, [("127.0.0.1", 3128)])
        self.assertLessEqual(environ.looked_up, {"COLUMNS", "LINES", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"})

    def test_checkout_entry_runs_main(self) -> None:
        # python -I fetcher/forwarder.py, with no environment: a bad --bind
        # proves the script reached main() and its argument parser.
        self.assertTrue(FORWARDER_SCRIPT.is_file())
        proc = subprocess.run(
            [sys.executable, "-I", str(FORWARDER_SCRIPT), "--bind", "not-an-ip"],
            env=subprocess_env(), capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("strange_ringtone_forwarder: error: argument --bind", proc.stderr)

    def test_entry_process_imports_only_the_forwarder_package(self) -> None:
        # The script's directory first on sys.path, as `python -I
        # fetcher/forwarder.py` has it: the process loads the forwarder's
        # modules and nothing of the core or the fetcher.
        code = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import strange_ringtone_forwarder.__main__\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('strange_ringtone'))))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code, str(FETCHER_DIR)],
            env=subprocess_env(), capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            "strange_ringtone_forwarder",
            "strange_ringtone_forwarder.__main__",
            "strange_ringtone_forwarder.policy",
            "strange_ringtone_forwarder.server",
        ])

    def test_entry_directory_fallbacks_never_give_the_empty_string(self) -> None:
        # I1 (M3-2): the script puts its own directory first on sys.path, and
        # "" there would mean the current directory wherever the script
        # lives. Each __file__ shape gets its own directory: a root keeps its
        # separator ("C:" alone is drive C's current directory), and a bare
        # file name is ".", where such a script is.
        code = compile(FORWARDER_SCRIPT.read_text(encoding="utf-8"), str(FORWARDER_SCRIPT), "exec")
        cases = {
            "/app/fetcher/forwarder.py": "/app/fetcher",
            "/a/b/forwarder.py": "/a/b",
            "/forwarder.py": "/",
            "forwarder.py": ".",
            "C:\\forwarder.py": "C:\\",
            "C:/forwarder.py": "C:/",
            "C:\\a\\b\\forwarder.py": "C:\\a\\b",
            "D:\\a/b\\forwarder.py": "D:\\a/b",
        }
        for file, expected in cases.items():
            with self.subTest(file=file):
                saved = list(sys.path)
                try:
                    with mock.patch.object(forwarder_main, "main", return_value=0) as main:
                        with self.assertRaises(SystemExit) as raised:
                            exec(code, {"__file__": file, "__name__": "__main__"})
                    inserted = sys.path[0]
                    rest = sys.path[1:]
                finally:
                    sys.path[:] = saved
                self.assertEqual(inserted, expected)
                self.assertNotEqual(inserted, "")
                self.assertEqual(rest, saved, "only one entry, inserted first")
                # The script then runs the package's main() and exits with it.
                main.assert_called_once_with()
                self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
