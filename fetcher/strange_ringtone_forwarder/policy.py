"""The forwarder's policy: pure decisions on bytes, names and addresses (docs/PLAN.md 6.1, G1 to G5).

No sockets and no I/O here, so every rule can be tested on its own and the
server (strange_ringtone_forwarder.server) only applies what this module
decides. Everything a client sends is checked by hand against a fixed
grammar: no HTTP or URL library parser ever sees it, and ipaddress only ever
sees text that already passed the request-line grammar.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass, fields

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
    # Fixed for v0.1.0 (PLAN 6.1) as shipped, after real fetches stayed well
    # inside them: they are part of the README's deployment contract, so
    # changing one is a breaking change. Each is one field here and one README row; tests
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
    # 2026-11-03; never sized from FETCH_MAX_BYTES, so a compromised runner
    # moves at most this much per tunnel; tests/image_fetcher pins it against
    # the pinned yt-dlp).
    download_bytes_per_tunnel: int = 16777216
    idle_timeout_seconds: int = 30
    lifetime_seconds: int = 300
    resolve_deadline_seconds: int = 5
    connect_timeout_seconds: int = 10
    # Lines the default log writes per sliding minute. Refusals cost a client
    # no tunnel budget, so without this a compromised runner could write log
    # lines as fast as the forwarder answers and fill the Docker host's disk.
    # 600 keeps an operator's view of a real incident; the rest are counted.
    log_lines_per_minute: int = 600


DEFAULT_LIMITS = Limits()


def limits_table() -> dict[str, int]:
    """Field name -> value of DEFAULT_LIMITS, the rows the README's limits table must carry.

    The README names every limit with its value so an operator can see what
    the forwarder enforces; a test compares that table with this dict.
    """
    return {field.name: getattr(DEFAULT_LIMITS, field.name) for field in fields(Limits)}


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


# The only versions a CONNECT may carry; anything else is not the protocol
# the gate understands, so it is refused rather than guessed at.
_VERSIONS = (b"HTTP/1.0", b"HTTP/1.1")
# A request-line token: visible ASCII, no space. Control characters (ESC,
# NUL, DEL, a bare CR or LF) and every byte >= 0x80 fall outside it, so a
# token carries neither a terminal escape nor a second request line, and no
# Unicode rule (IDNA, case folding, normalisation) ever gets a byte to read.
_TOKEN = re.compile(rb"[\x21-\x7e]+")
# The hostname grammar for logging (G10): text it matches can't forge a log
# line or a field in one. Explicit ranges, so no Unicode class applies.
_LOGGABLE = re.compile(r"[A-Za-z0-9.-]{1,253}")
# One label after ASCII lowering: letters, digits and '-', not at either end.
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
# A last label inet_aton reads as a number: decimal or octal digits, or hex
# with a 0x prefix (0xc0000201, 192.0.2.0x1). No top-level domain starts
# with a digit, so neither form is ever a real name's last label.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9A-Fa-f]*")
# Python calls the deprecated 6to4 relay anycast block global; it reaches
# whichever relay is nearest, not YouTube.
_SIXTO4_ANYCAST = ipaddress.IPv4Network("192.88.99.0/24")


def split_head(buffer: bytes) -> tuple[bytes, bytes] | None:
    """(head including the blank line, the bytes after it), or None if HEAD_END is not in buffer.

    The bytes after the head are the start of the client's TLS stream; they
    are relayed unparsed (G7).
    """
    end = buffer.find(HEAD_END)
    if end < 0:
        return None
    end += len(HEAD_END)
    return buffer[:end], buffer[end:]


def loggable_name(text: str) -> str | None:
    """text if it matches the hostname grammar (ASCII letters, digits, '-', '.', 1..253 chars), else None.

    The only thing from a request that may ever be logged (G10), so no
    unvalidated byte from a client reaches the log.
    """
    return text if _LOGGABLE.fullmatch(text) else None


def _split_authority(target: str) -> tuple[str, str, bool] | None:
    """(host part as sent, port text, bracketed) of a bare `host:port`, or None for any other form.

    Userinfo ('@'), a path or a scheme ('/'), a missing or extra colon, and
    an empty host or port are not an authority. Refusing them here means no
    later step has to guess which part another parser would take as the host.
    """
    if "@" in target or "/" in target:
        return None
    if target.startswith("["):
        # The IPv6 literal form: the brackets hold the colons, and the port
        # follows the closing bracket. Whatever is inside is refused later.
        close = target.find("]")
        if close < 0 or not target.startswith(":", close + 1):
            return None
        host, port = target[: close + 1], target[close + 2 :]
        bracketed = True
    else:
        if target.count(":") != 1:
            return None
        host, port = target.split(":")
        bracketed = False
    if host in ("", "[]") or not port:
        return None
    return host, port, bracketed


def _is_literal(host: str) -> bool:
    """True for an IP address in any notation a resolver or a socket call might accept.

    ipaddress reads only the canonical forms, so the last-label rule also
    catches what inet_aton would read as an address: a bare integer
    (3221225985), a short dotted form (192.0.2, 1.1), octal-looking parts
    (0177.0.0.1) and hex parts (0xc0000201, 0xc0.0.2.0x1). inet_aton reads a
    string as an address only when its last part is a number, so testing the
    last label is enough. No top-level domain starts with a digit, so no real
    name is lost.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    last = host.rsplit(".", 1)[-1]
    # An explicit ASCII class: str.isdigit and \d would also take Unicode
    # digits, which later fail the name grammar anyway.
    return _NUMERIC_LABEL.fullmatch(last) is not None


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
        label is all ASCII digits or 0x hex, or it is a bare integer
                                                      -> Refusal(400, "literal", name)
      host not ASCII, > 253 chars, a label empty or > 63 chars, a label with a
        character outside [a-z0-9-] after ASCII lowering, or starting or
        ending with '-' (a trailing dot is an empty label)  -> Refusal(400, "bad_name", name=None)
    Headers after the request line are not parsed at all: the gate needs only
    the authority, and every parsed byte is attack surface.
    """
    # The request line ends at the first CRLF. A bare LF or CR before it stays
    # inside the line and fails the token rule, so no line-ending ambiguity
    # survives (request smuggling lives on two parsers disagreeing there).
    end = head.find(b"\r\n")
    if end < 0:
        raise Refusal(400, "malformed")
    parts = head[:end].split(b" ")
    # Exactly one space between tokens: an empty part is a doubled, leading
    # or trailing space.
    if len(parts) != 3 or not all(_TOKEN.fullmatch(part) for part in parts):
        raise Refusal(400, "malformed")
    if parts[2] not in _VERSIONS:
        raise Refusal(400, "malformed")
    # Visible ASCII only by now, so decoding can't fail and every later check
    # sees exactly the characters the client sent.
    method, target = parts[0].decode("ascii"), parts[1].decode("ascii")
    authority = _split_authority(target)
    if method != "CONNECT":
        # Plain-HTTP proxying (GET http://...) and everything else. Only an
        # authority's host part can be a name worth logging; an origin-form
        # or absolute-form target logs '-'.
        raise Refusal(405, "method", loggable_name(authority[0]) if authority is not None else None)
    if authority is None:
        raise Refusal(400, "malformed")
    host, port, bracketed = authority
    name = loggable_name(host)
    # Exactly the text "443": no leading zero, no sign, no other port.
    if port != str(ALLOWED_PORT):
        raise Refusal(403, "port", name)
    if bracketed:
        # Brackets are only for an IPv6 literal, which is refused either way.
        if _is_literal(host[1:-1]):
            raise Refusal(400, "literal", name)
        raise Refusal(400, "malformed")
    # Before the name grammar, which would pass 192.0.2.1 as four labels.
    if _is_literal(host):
        raise Refusal(400, "literal", name)
    # str.lower() on ASCII only, so no Unicode case rule can turn another
    # character into a listed one (the dotless i, the Kelvin sign and such).
    if not host.isascii() or len(host) > NAME_MAX_CHARS:
        raise Refusal(400, "bad_name")
    lowered = host.lower()
    for label in lowered.split("."):
        if len(label) > LABEL_MAX_CHARS or not _LABEL.fullmatch(label):
            raise Refusal(400, "bad_name")
    return lowered


def name_allowed(name: str) -> bool:
    """Step 2: name == apex or name.endswith("." + apex) for ALLOW_APEX; name.endswith("." + d) for ALLOW_SUBDOMAINS_OF.

    Matched by whole labels, so notyoutube.com and youtube.com.example.invalid
    are refused (G3).
    """
    for apex in ALLOW_APEX:
        if name == apex or name.endswith("." + apex):
            return True
    return any(name.endswith("." + domain) for domain in ALLOW_SUBDOMAINS_OF)


def is_global_unicast(address: ipaddress.IPv4Address) -> bool:
    """address.is_global and not address.is_multicast and address not in IPv4Network("192.88.99.0/24").

    Python calls multicast global, and 192.88.99.0/24 is the deprecated 6to4
    anycast block, so both are excluded explicitly (G5).
    """
    return address.is_global and not address.is_multicast and address not in _SIXTO4_ANYCAST


def check_addresses(answers: Sequence[str], name: str) -> tuple[ipaddress.IPv4Address, ...]:
    """Step 4's policy on a resolver answer.

    Empty -> Refusal(502, "resolve_failed", name). Any item that is not a
    textual IPv4 address (an IPv6 string, garbage) or fails is_global_unicast
    -> Refusal(403, "not_global", name): one bad answer refuses the whole
    tunnel, so a name that also points inside cannot be used to reach it.
    Order kept, duplicates dropped.
    """
    if not answers:
        raise Refusal(502, "resolve_failed", name)
    checked: list[ipaddress.IPv4Address] = []
    for answer in answers:
        # Text only: IPv4Address also takes an int or packed bytes, which no
        # resolver answer should be.
        if not isinstance(answer, str):
            raise Refusal(403, "not_global", name)
        try:
            address = ipaddress.IPv4Address(answer)
        except ValueError:
            raise Refusal(403, "not_global", name) from None
        # The canonical text only, so what is connected to is exactly what was
        # checked, with no second reading of a looser form.
        if str(address) != answer or not is_global_unicast(address):
            raise Refusal(403, "not_global", name)
        if address not in checked:
            checked.append(address)
    return tuple(checked)


def refusal_bytes(status: int) -> bytes:
    """b"HTTP/1.1 %d %s\\r\\nConnection: close\\r\\nContent-Length: 0\\r\\n\\r\\n" with STATUS_PHRASES[status].

    No body and no header from the request: a refusal tells the client the
    status and nothing else.
    """
    return f"HTTP/1.1 {status} {STATUS_PHRASES[status]}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode(
        "ascii"
    )
