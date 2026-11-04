"""The forwarder's server: accept, gate, resolve, connect and relay one CONNECT tunnel (docs/PLAN.md 6.1, G1 to G10).

For each connection, in order: read the head within a size cap and a
deadline, parse it (strange_ringtone_forwarder.policy), check the allowlist,
take the tunnel budgets, and only then resolve the name, IPv4 only, check
every answer, connect to a checked address on port 443 and relay bytes under
byte, idle and lifetime caps. Every refusal path before the resolver costs no
DNS query, so a client can't make the forwarder look up a name it would
refuse. The resolver, the connector, the clock and the outcome sink are
injected so tests need no DNS, no network and no sleeping.
"""
from __future__ import annotations

import socket
import socketserver
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from strange_ringtone_forwarder.policy import DEFAULT_LIMITS, Limits

# (name, family) -> address strings.
Resolver = Callable[[str, int], Sequence[str]]
# (address, port, timeout) -> connected socket. It gets an address the policy
# checked, never a name, so it can't be made to resolve anything.
Connector = Callable[[str, int, float], socket.socket]
# Monotonic seconds; injected so the per-minute windows can be tested without
# waiting a minute.
Clock = Callable[[], float]


@dataclass(frozen=True)
class Outcome:
    """What happened to one connection, for the sink: the log by default, a measurement in the product check."""

    name: str | None  # loggable name, or None
    reason: str  # "ok", "refused:<token>" or "cut:<token>"
    upload_bytes: int  # client -> upstream, after the head
    download_bytes: int  # upstream -> client
    seconds: float  # from accept to close


Sink = Callable[[Outcome], None]


def resolve_ipv4(name: str, family: int = socket.AF_INET) -> list[str]:
    """Distinct sockaddr[0] strings, in order, of getaddrinfo(name, 443, family, SOCK_STREAM, IPPROTO_TCP).

    family is passed through so a spy can prove AF_INET; the server always
    passes socket.AF_INET, because IPv6 answers would need a second address
    policy and the deployment's networks route IPv4 only (G5).
    """
    raise NotImplementedError


def connect(address: str, port: int, timeout: float) -> socket.socket:
    """socket.create_connection((address, port), timeout) and nothing else: no name ever reaches it.

    Connecting to the address the policy checked, rather than to the name,
    closes the window in which a second lookup could answer differently (G6).
    """
    raise NotImplementedError


def log_outcome(outcome: Outcome, log: TextIO) -> None:
    """The default sink: one line for refusals (except "refused:empty") and cuts, nothing for "ok".

    Format: f"forwarder {kind} {token} {outcome.name or '-'}\\n" where kind is
    "refused" or "cut" and token the part after the colon. Accepted tunnels
    and empty connections (health checks) log nothing, so the log holds only
    what an operator must look at, and never payload or header text (G10).
    """
    raise NotImplementedError


class Window:
    """A sliding 60-second budget: add(amount) records it and returns True, or returns False and records nothing.

    Used for new tunnels per minute and upload bytes per minute (G8, G9); a
    refused add records nothing, so a client hammering the limit doesn't push
    its own recovery further away.
    """

    def __init__(self, limit: int, clock: Clock, seconds: float = 60.0) -> None:
        raise NotImplementedError

    def add(self, amount: int = 1) -> bool:
        raise NotImplementedError


class Forwarder(socketserver.ThreadingTCPServer):
    # As FetchServer: daemon workers, no join on close, and no SO_REUSEADDR on
    # Windows (where it lets a second process take the port).
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self,
        address: tuple[str, int],
        limits: Limits = DEFAULT_LIMITS,
        *,
        resolver: Resolver = resolve_ipv4,
        connector: Connector = connect,
        clock: Clock = time.monotonic,
        sink: Sink | None = None,
        log: TextIO | None = None,
    ) -> None:
        """Bind address and keep the injected parts.

        sink defaults to functools.partial(log_outcome, log=log); log defaults
        to sys.stderr, resolved at call time when None so a redirected stderr
        is honoured.
        """
        raise NotImplementedError

    def server_bind(self) -> None:
        """TCPServer.server_bind only: no socket.getfqdn(), a reverse DNS lookup nobody needs (as FetchServer)."""
        raise NotImplementedError

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        """connections_max gate: beyond it, close at once and emit Outcome(None, "refused:connections", 0, 0, 0.0).

        Closed without a response, before a thread is spent on it, so a flood
        of half-open connections can't exhaust threads (G8).
        """
        raise NotImplementedError

    def handle_tunnel(self, client: socket.socket) -> None:
        """Steps 1 to 6 for one accepted connection; always closes client; always calls the sink exactly once."""
        raise NotImplementedError


class TunnelHandler(socketserver.BaseRequestHandler):
    """Hands each accepted connection to Forwarder.handle_tunnel, which owns every decision."""

    def handle(self) -> None:
        raise NotImplementedError
