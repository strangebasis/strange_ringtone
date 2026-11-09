"""The default-route start check of the fetcher and its runner (docs/PLAN.md 6.1, S2).

The fetcher image's two servers reach out only to their next hop: the
fetcher to its runner over `jobs`, the runner (and the yt-dlp it starts) to
YouTube only through the forwarder (HTTPS_PROXY). If either container also
had a default route, it or anything it runs could carry traffic out some
other way, so each entry point refuses to start when the kernel's route
tables hold a default route, or can't be read or understood: fail closed.
FETCH_ALLOW_DEFAULT_ROUTE=true turns the check off where egress is filtered
another way. The tables are read from /proc/net as text, so the check needs
no capability and no extra package.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROC_NET = Path("/proc/net")
# Route flags from the kernel's include/uapi/linux/route.h and ipv6_route.h.
RTF_UP = 0x0001
# Kernels add ::/0 reject routes to namespaces without IPv6; they carry nothing.
RTF_REJECT = 0x0200

# /proc/net/route's column names (net/ipv4/fib_trie.c). The parser reads the
# Destination and Mask columns by position, so the header must name exactly
# these, in this order: a kernel that moved a column would otherwise have us
# read the wrong one and miss a default route.
_IPV4_COLUMNS = ("Iface", "Destination", "Gateway", "Flags", "RefCnt", "Use", "Metric", "Mask", "MTU", "Window",
                 "IRTT")
_IPV4_MIN_FIELDS = 11
# /proc/net/ipv6_route has no header: dest prefixlen src srcprefix nexthop
# metric refcnt use flags device (net/ipv6/route.c).
_IPV6_MIN_FIELDS = 10
# The kernel prints the columns we read as fixed-width hex. Explicit ASCII
# classes, because int(text, 16) alone would also accept Unicode digits,
# underscores and a 0x prefix, and so read a line the kernel never wrote.
_HEX8 = re.compile(r"[0-9A-Fa-f]{8}")
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
_HEX2 = re.compile(r"[0-9A-Fa-f]{2}")
_HEX32 = re.compile(r"[0-9A-Fa-f]{32}")
# The kernel separates fields with tabs (IPv4) or spaces (IPv6) and pads with
# spaces. str.split() would also split on characters such as \x1c or \x0b
# that an interface name may hold, shifting the columns, so only these two
# separate fields, and only \n separates lines (not str.splitlines()).
_SEPARATORS = re.compile(r"[ \t]+")
_PAD = " \t"


class RouteError(Exception):
    """The kernel's route tables could not be read or understood; the caller fails closed.

    Messages name the file ("cannot read /proc/net/route") and never a value
    from it, so no device or address reaches the log.
    """


@dataclass(frozen=True)
class DefaultRoute:
    family: str  # "IPv4" or "IPv6"
    device: str  # interface name; never printed by the entry point


def _fields(line: str) -> list[str]:
    return _SEPARATORS.split(line.strip(_PAD))


def parse_ipv4_routes(text: str) -> tuple[DefaultRoute, ...]:
    """Default routes in /proc/net/route text.

    The first line must be the header, starting with "Iface" and naming the
    kernel's eleven columns (else RouteError). Each later line (blank lines
    and trailing whitespace ignored) must split into at least 11 fields, with
    hex Destination, Gateway, Mask and Flags (else RouteError). A default
    route is Mask == "00000000" (a /0, whose Destination is "00000000"),
    whatever the flags: fail closed.
    """
    lines = text.split("\n")
    # The header proves this is the table we think it is; anything else (an
    # empty file included) is not "no routes".
    if tuple(_fields(lines[0])) != _IPV4_COLUMNS:
        raise RouteError("unrecognised header")
    found: list[DefaultRoute] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip(_PAD):
            continue
        fields = _fields(line)
        # Line numbers only: the message never repeats a device or address.
        if len(fields) < _IPV4_MIN_FIELDS:
            raise RouteError(f"line {number} has too few fields")
        device, destination, gateway, flags, mask = fields[0], fields[1], fields[2], fields[3], fields[7]
        if not (_HEX8.fullmatch(destination) and _HEX8.fullmatch(gateway) and _HEX8.fullmatch(mask)
                and _HEX4.fullmatch(flags)):
            raise RouteError(f"line {number} is not a route")
        # A zero mask is a /0, which matches every destination, so it is a
        # default route whatever its destination column says. The flags are
        # not consulted: a route that is down now (no RTF_UP) can come up
        # after the check, and a route without RTF_GATEWAY still reaches
        # everything on its link.
        if int(mask, 16) == 0:
            found.append(DefaultRoute("IPv4", device))
    return tuple(found)


def parse_ipv6_routes(text: str) -> tuple[DefaultRoute, ...]:
    """Default routes in /proc/net/ipv6_route text.

    Each non-blank line must split into at least 10 fields: dest(32 hex)
    prefixlen(2 hex) src srcprefix nexthop metric refcnt use flags(8 hex)
    device (else RouteError). A default route is prefixlen == 0 with
    RTF_REJECT clear in flags. Kernels add `::/0` reject routes (flags
    0x00200200, dev lo) to namespaces without IPv6, which carry nothing.
    """
    found: list[DefaultRoute] = []
    # No header: an empty table is a valid one (a namespace with no routes).
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip(_PAD):
            continue
        fields = _fields(line)
        if len(fields) < _IPV6_MIN_FIELDS:
            raise RouteError(f"line {number} has too few fields")
        destination, prefix, flags, device = fields[0], fields[1], fields[8], fields[9]
        if not (_HEX32.fullmatch(destination) and _HEX2.fullmatch(prefix) and _HEX8.fullmatch(flags)):
            raise RouteError(f"line {number} is not a route")
        # A /0 matches every destination. Only RTF_REJECT makes one harmless:
        # the kernel answers it with an error instead of sending anything.
        # Every other flag is ignored, as for IPv4: a route that isn't up yet
        # can come up after the check.
        if int(prefix, 16) == 0 and not int(flags, 16) & RTF_REJECT:
            found.append(DefaultRoute("IPv6", device))
    return tuple(found)


def _read(path: Path) -> str:
    # ASCII, strictly: the kernel prints these tables in ASCII, and an
    # interface name with other bytes can't be told apart from a corrupt
    # table, so it refuses rather than being guessed at. The with block
    # closes the handle before anything else happens.
    with open(path, encoding="ascii", errors="strict", newline="") as handle:
        return handle.read()


def default_routes(proc_net: Path = PROC_NET) -> tuple[DefaultRoute, ...]:
    """IPv4 then IPv6 default routes from proc_net/route and proc_net/ipv6_route.

    A missing or unreadable route file, or a parse failure, raises RouteError
    (so no /proc, as on Windows, refuses). A missing ipv6_route alone means a
    kernel without IPv6 and counts as no IPv6 routes.
    """
    route = proc_net / "route"
    ipv6_route = proc_net / "ipv6_route"
    # The IPv4 table must exist: without it nothing proves the container has
    # no default route (no /proc at all, as on Windows, lands here too).
    try:
        ipv4_text = _read(route)
    except (OSError, UnicodeDecodeError):
        raise RouteError(f"cannot read {route.as_posix()}") from None
    try:
        found = parse_ipv4_routes(ipv4_text)
    except RouteError as error:
        raise RouteError(f"cannot parse {route.as_posix()}: {error}") from None
    try:
        ipv6_text = _read(ipv6_route)
    except FileNotFoundError:
        # Only "missing" has a meaning of its own: a kernel built without
        # IPv6 has no ipv6_route file and no IPv6 routes. A file that exists
        # but can't be read (a directory, permissions) still refuses.
        return found
    except (OSError, UnicodeDecodeError):
        raise RouteError(f"cannot read {ipv6_route.as_posix()}") from None
    try:
        return found + parse_ipv6_routes(ipv6_text)
    except RouteError as error:
        raise RouteError(f"cannot parse {ipv6_route.as_posix()}: {error}") from None
