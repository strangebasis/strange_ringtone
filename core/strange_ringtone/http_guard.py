"""Request checks that run before any body byte is read (docs/PLAN.md 5.2 and 8).

Pure functions and small classes with no http.server imports, so each check is
tested on its own. Every check fails closed: anything it does not positively
recognise as allowed is refused.
"""
from __future__ import annotations

import collections
import contextlib
import ipaddress
import math
import re
import threading
import time
from collections.abc import Callable, Iterator

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# default-src 'none' denies everything not listed. Scripts, styles and media
# come only from this origin, so an injected <script> or a third-party asset
# never runs; base-uri and frame-ancestors stop <base> hijacking and framing.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

_DIGITS_RE = re.compile(r"[0-9]+")
_WINDOW_SECONDS = 60.0


class GuardError(Exception):
    """A request is refused. The server answers status with JSON {"error": code}."""

    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.retry_after = retry_after


def security_headers(public_origin: str) -> list[tuple[str, str]]:
    """The headers every response carries, errors and 404s included."""
    headers = [
        ("Content-Security-Policy", CSP),
        # Stops a browser from sniffing a response into a script or HTML.
        ("X-Content-Type-Options", "nosniff"),
        # Clickjacking: no page may frame this one (older browsers ignore
        # the CSP's frame-ancestors).
        ("X-Frame-Options", "DENY"),
        # The page URL never leaks to another origin.
        ("Referrer-Policy", "same-origin"),
        # A cross-origin window can't keep a handle to ours, and no other
        # origin can embed our responses (rendered tones included).
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        # The app needs none of these, so an injected script can't ask for them.
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    ]
    # HSTS only makes sense, and only takes effect, over https. Sending it for an
    # http origin would do nothing at best and confuse a later TLS setup at worst.
    # includeSubDomains covers the PUBLIC_ORIGIN host's subdomains too; docs/PLAN.md
    # section 8 records the value and that cost.
    if public_origin.startswith("https://"):
        headers.append(("Strict-Transport-Security", "max-age=63072000; includeSubDomains"))
    return headers


def require_origin(values: list[str], public_origin: str) -> None:
    """CSRF defence: exactly one Origin header, byte-for-byte equal to PUBLIC_ORIGIN.

    A missing Origin is refused rather than waved through, there is no Referer
    fallback, and two headers are refused because a proxy and the server might
    each read a different one.
    """
    if not public_origin or len(values) != 1 or values[0] != public_origin:
        raise GuardError(403, "bad_origin")


def require_content_type(values: list[str], expected: str) -> None:
    """Exactly one Content-Type, equal to expected, with no parameters.

    audio/wav is not a CORS-safelisted type, so a cross-origin page can't send
    it without a preflight that this server never approves.
    """
    # Only HTTP's optional whitespace is stripped; str.strip() would also
    # accept Unicode spaces that no browser sends.
    if len(values) != 1 or values[0].strip(" \t") != expected:
        raise GuardError(415, "unsupported_media_type")


def body_length(content_length_values: list[str], transfer_encoding_values: list[str], max_bytes: int) -> int:
    """The declared body length, checked before a single body byte is read."""
    # Request smuggling: when Transfer-Encoding and Content-Length disagree, a
    # proxy and this server may split the stream differently. Chunked bodies are
    # never needed, so any Transfer-Encoding at all is refused.
    if transfer_encoding_values:
        raise GuardError(400, "transfer_encoding")
    # Without a length the server can't refuse an oversized body up front.
    if not content_length_values:
        raise GuardError(411, "length_required")
    # Two lengths, or one that int() would read generously ('+5', ' 5', '5_0'),
    # is the other smuggling shape: refuse rather than guess.
    if len(content_length_values) != 1 or not _DIGITS_RE.fullmatch(content_length_values[0]):
        raise GuardError(400, "bad_length")
    digits = content_length_values[0].lstrip("0") or "0"
    # Compare lengths first so a megabyte of digits never reaches int().
    if len(digits) > len(str(max_bytes)) or int(digits) > max_bytes:
        raise GuardError(413, "too_large")
    return int(digits)


def _canonical(address: Address) -> Address:
    # ::ffff:192.0.2.1 is the same client as 192.0.2.1; one key per client keeps
    # the rate limit from being doubled by switching notation.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _parse_address(text: str) -> Address | None:
    try:
        return _canonical(ipaddress.ip_address(text))
    except ValueError:
        return None


def _parse_hop(hop: str) -> Address | None:
    if not hop:
        return None
    if hop.startswith("["):
        # '[2001:db8::1]' or '[2001:db8::1]:443'
        end = hop.find("]")
        if end < 0:
            return None
        rest = hop[end + 1:]
        if rest and not (rest.startswith(":") and _DIGITS_RE.fullmatch(rest[1:])):
            return None
        return _parse_address(hop[1:end])
    if hop.count(":") == 1:
        # '192.0.2.1:443'. A bare IPv6 address has several colons and parses as-is.
        host, _, port = hop.partition(":")
        if not _DIGITS_RE.fullmatch(port):
            return None
        return _parse_address(host)
    return _parse_address(hop)


def _is_trusted(address: Address, trusted: tuple[Network, ...]) -> bool:
    # An IPv4 address is never "in" an IPv6 network and vice versa.
    return any(address in network for network in trusted)


def client_address(peer: str, forwarded_for_values: list[str], trusted: tuple[Network, ...]) -> str:
    """The address the per-client rate limit is keyed on.

    X-Forwarded-For is client-controlled, so it is read only when the socket
    peer is a trusted proxy. Each proxy appends the address it saw, so walking
    from the right, the first hop that is not a trusted proxy is the real client:
    anything further left is whatever the client chose to write.

    Without such a hop the peer is the client: when the header is missing or
    empty, when the walk meets a malformed hop first, and when every hop is a
    trusted proxy. Those requests then share the proxy's budget, which fails
    closed instead of keying on an address no untrusted party was seen at.
    """
    peer_address = _parse_address(peer)
    if peer_address is None:
        return peer
    if not _is_trusted(peer_address, trusted):
        return str(peer_address)
    hops = [hop.strip(" \t") for hop in ",".join(forwarded_for_values).split(",")]
    for hop in reversed(hops):
        address = _parse_hop(hop)
        if address is None:
            # Garbage ends the walk: what lies left of it can't be trusted to
            # have been written by one of our proxies.
            break
        if not _is_trusted(address, trusted):
            return str(address)
    return str(peer_address)


class RateLimiter:
    """At most per_minute allowed calls per key in any sliding 60-second window."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self._per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, collections.deque[float]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)

    def allow(self, key: str) -> tuple[bool, int]:
        """(True, 0) and count the call, or (False, seconds until a slot frees)."""
        return self._check(key, record=True)

    def has_room(self, key: str) -> tuple[bool, int]:
        """What allow() would answer now, without counting the call.

        Lets the server refuse an over-limit client with 429 before it asks the
        concurrency gate or reads the upload, yet count a render only once its
        upload has passed validation.
        """
        return self._check(key, record=False)

    def _check(self, key: str, record: bool) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            # Prune on every call so a stream of one-off client addresses can't
            # grow the table without bound; each key also holds at most
            # per_minute timestamps, because refused calls are not recorded.
            for name in list(self._hits):
                stamps = self._hits[name]
                while stamps and stamps[0] <= now - _WINDOW_SECONDS:
                    stamps.popleft()
                if not stamps:
                    del self._hits[name]
            stamps = self._hits.get(key, collections.deque())
            # The check and the append happen under one lock, so concurrent
            # requests from one client can't all see the last free slot.
            if len(stamps) < self._per_minute:
                if record:
                    stamps.append(now)
                    self._hits[key] = stamps
                return True, 0
            return False, max(1, math.ceil(stamps[0] + _WINDOW_SECONDS - now))


class ConcurrencyGate:
    """A non-blocking cap on concurrent renders: when full, refuse instead of queueing."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        # Bounded, so a stray extra release() raises instead of silently raising the cap.
        self._semaphore = threading.BoundedSemaphore(limit)

    def acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        # Queued requests would each hold a body in scratch and a worker thread
        # while they wait; a fast 503 keeps both bounded.
        if not self.acquire():
            raise GuardError(503, "busy", retry_after=1)
        try:
            yield
        finally:
            self.release()
