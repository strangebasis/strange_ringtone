"""The fetcher's default-route start check (docs/PLAN.md 6.1, S2).

The fetcher reaches YouTube only through the forwarder (HTTPS_PROXY). If its
container also had a default route, yt-dlp or anything it runs could carry
traffic out some other way, so the entry point refuses to start when the
kernel's route tables hold a default route, or can't be read or understood:
fail closed. FETCH_ALLOW_DEFAULT_ROUTE=true turns the check off where egress
is filtered another way. The tables are read from /proc/net as text, so the
check needs no capability and no extra package.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROC_NET = Path("/proc/net")
# Route flags from the kernel's include/uapi/linux/route.h and ipv6_route.h.
RTF_UP = 0x0001
# Kernels add ::/0 reject routes to namespaces without IPv6; they carry nothing.
RTF_REJECT = 0x0200


class RouteError(Exception):
    """The kernel's route tables could not be read or understood; the caller fails closed.

    Messages name the file ("cannot read /proc/net/route") and never a value
    from it, so no device or address reaches the log.
    """


@dataclass(frozen=True)
class DefaultRoute:
    family: str  # "IPv4" or "IPv6"
    device: str  # interface name; never printed by the entry point


def parse_ipv4_routes(text: str) -> tuple[DefaultRoute, ...]:
    """Default routes in /proc/net/route text.

    First line must start with "Iface" (else RouteError). Each later line
    (blank lines and trailing whitespace ignored) must split into at least 11
    fields (else RouteError). A default route is Destination == "00000000"
    and Mask == "00000000", whatever the flags: fail closed.
    """
    raise NotImplementedError


def parse_ipv6_routes(text: str) -> tuple[DefaultRoute, ...]:
    """Default routes in /proc/net/ipv6_route text.

    Each non-blank line must split into at least 10 fields: dest(32 hex)
    prefixlen(2 hex) src srcprefix nexthop metric refcnt use flags(8 hex)
    device (else RouteError). A default route is prefixlen == 0 with
    RTF_REJECT clear in flags. Kernels add `::/0` reject routes (flags
    0x00200200, dev lo) to namespaces without IPv6, which carry nothing.
    """
    raise NotImplementedError


def default_routes(proc_net: Path = PROC_NET) -> tuple[DefaultRoute, ...]:
    """IPv4 then IPv6 default routes from proc_net/route and proc_net/ipv6_route.

    A missing or unreadable route file, or a parse failure, raises RouteError
    (so no /proc, as on Windows, refuses). A missing ipv6_route alone means a
    kernel without IPv6 and counts as no IPv6 routes.
    """
    raise NotImplementedError
