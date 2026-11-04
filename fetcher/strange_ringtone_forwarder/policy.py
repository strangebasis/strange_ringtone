"""The forwarder's policy: pure decisions on bytes, names and addresses (docs/PLAN.md 6.1, G1 to G5).

No sockets and no I/O here, so every rule can be tested on its own and the
server (strange_ringtone_forwarder.server) only applies what this module
decides. Everything a client sends is checked by hand against a fixed
grammar: no library parser ever sees it.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass

# The forwarder's fixed listening port: the conventional proxy port, distinct
# from the fetcher's 8080 so both can share a network namespace. No setting.
PORT = 3128
# The only port a CONNECT may name: HTTPS to YouTube and nothing else.
ALLOWED_PORT = 443
# Names allowed as themselves or with any subdomain.
ALLOW_APEX = ("youtube.com",)
# Names whose subdomains are allowed, but never the apex itself.
ALLOW_SUBDOMAINS_OF = ("googlevideo.com",)
# RFC 1035 limits on a host name and on each of its labels.
NAME_MAX_CHARS = 253
LABEL_MAX_CHARS = 63
# The reason phrases of the only statuses the forwarder ever sends.
STATUS_PHRASES = {
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
# Sent once the upstream connection is open; after it the bytes are the
# client's TLS and are relayed, never parsed.
ACCEPT_BYTES = b"HTTP/1.1 200 Connection Established\r\n\r\n"
# The blank line that ends a CONNECT head.
HEAD_END = b"\r\n\r\n"


@dataclass(frozen=True)
class Limits:
    # Provisional values (PLAN 6.1): the product check may resize them from
    # one measured fetch. Each is one field here and one README row; tests
    # inject their own small values or compare README with these.
    head_max_bytes: int = 4096
    head_deadline_seconds: int = 10
    # Open client connections, before the head: a flood of half-open
    # connections must not exhaust threads before the tunnel budgets apply.
    connections_max: int = 64
    tunnels_max_concurrent: int = 8
    tunnels_per_minute: int = 90
    upload_bytes_per_tunnel: int = 262144  # 256 KiB
    upload_bytes_per_minute: int = 4194304  # 4 MiB
    # 16 MiB: one 10 MiB yt-dlp range plus headroom (fixed by the owner,
    # 2026-11-03; never sized from FETCH_MAX_BYTES, so a compromised fetcher
    # moves at most this much per tunnel; tests/image_fetcher pins it against
    # the pinned yt-dlp).
    download_bytes_per_tunnel: int = 16777216
    idle_timeout_seconds: int = 30
    lifetime_seconds: int = 300
    resolve_deadline_seconds: int = 5
    connect_timeout_seconds: int = 10


DEFAULT_LIMITS = Limits()


def limits_table() -> dict[str, int]:
    """Field name -> value of DEFAULT_LIMITS, the rows the README's limits table must carry.

    The README names every limit with its value so an operator can see what
    the forwarder enforces; a test compares that table with this dict.
    """
    raise NotImplementedError


class Refusal(Exception):
    """A request the forwarder answers with a status line and closes.

    status: the HTTP status, or None for a connection closed before any byte
    arrived (reason "empty"), which gets no response at all; reason: the log
    token (section 3.3 of the spec, for example "unlisted"); name: the name to
    log, or None when nothing validated is known. The exception's message is
    the reason token alone, so printing the exception never prints anything
    the client sent.
    """

    def __init__(self, status: int | None, reason: str, name: str | None = None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.name = name


def split_head(buffer: bytes) -> tuple[bytes, bytes] | None:
    """(head including the blank line, the bytes after it), or None if HEAD_END is not in buffer.

    The bytes after the head are the start of the client's TLS stream; they
    are relayed unparsed (G7).
    """
    raise NotImplementedError


def loggable_name(text: str) -> str | None:
    """text if it matches the hostname grammar (ASCII letters, digits, '-', '.', 1..253 chars), else None.

    The only thing from a request that may ever be logged (G10), so no
    unvalidated byte from a client reaches the log.
    """
    raise NotImplementedError


def parse_connect(head: bytes) -> str:
    """The lowercased, validated name of `CONNECT <name>:443 HTTP/1.x`, or raise Refusal (step 1).

    Checks in this order, first failure wins:
      request line not three space-separated ASCII tokens ending in CRLF, or
        version not HTTP/1.0 or HTTP/1.1              -> Refusal(400, "malformed")
      method != "CONNECT" (case-sensitive)            -> Refusal(405, "method", name=loggable_name(authority host part))
      authority not <host>:<port> (one colon for a bare host, brackets for IPv6
        literal form), userinfo, path, empty host     -> Refusal(400, "malformed")
      port text != "443" exactly                      -> Refusal(403, "port", name)
      host parses as ipaddress.ip_address (brackets stripped), or its last
        label is all digits, or it is a bare integer  -> Refusal(400, "literal", name)
      host not ASCII, > 253 chars, a label empty or > 63 chars, a label with a
        character outside [a-z0-9-] after ASCII lowering, or starting or
        ending with '-' (a trailing dot is an empty label)  -> Refusal(400, "bad_name", name=None)
    Headers after the request line are not parsed at all: the gate needs only
    the authority, and every parsed byte is attack surface.
    """
    raise NotImplementedError


def name_allowed(name: str) -> bool:
    """Step 2: name == apex or name.endswith("." + apex) for ALLOW_APEX; name.endswith("." + d) for ALLOW_SUBDOMAINS_OF.

    Matched by whole labels, so notyoutube.com and youtube.com.example.invalid
    are refused (G3).
    """
    raise NotImplementedError


def is_global_unicast(address: ipaddress.IPv4Address) -> bool:
    """address.is_global and not address.is_multicast and address not in IPv4Network("192.88.99.0/24").

    Python calls multicast global, and 192.88.99.0/24 is the deprecated 6to4
    anycast block, so both are excluded explicitly (G5).
    """
    raise NotImplementedError


def check_addresses(answers: Sequence[str], name: str) -> tuple[ipaddress.IPv4Address, ...]:
    """Step 4's policy on a resolver answer.

    Empty -> Refusal(502, "resolve_failed", name). Any item that is not a
    textual IPv4 address (an IPv6 string, garbage) or fails is_global_unicast
    -> Refusal(403, "not_global", name): one bad answer refuses the whole
    tunnel, so a name that also points inside cannot be used to reach it.
    Order kept, duplicates dropped.
    """
    raise NotImplementedError


def refusal_bytes(status: int) -> bytes:
    """b"HTTP/1.1 %d %s\\r\\nConnection: close\\r\\nContent-Length: 0\\r\\n\\r\\n" with STATUS_PHRASES[status].

    No body and no header from the request: a refusal tells the client the
    status and nothing else.
    """
    raise NotImplementedError
