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

import selectors
import socket
import socketserver
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from strange_ringtone_forwarder.policy import (
    ACCEPT_BYTES,
    ALLOWED_PORT,
    DEFAULT_LIMITS,
    Limits,
    Refusal,
    check_addresses,
    loggable_name,
    name_allowed,
    parse_connect,
    refusal_bytes,
    split_head,
)

# (name, family) -> address strings.
Resolver = Callable[[str, int], Sequence[str]]
# (address, port, timeout) -> connected socket. It gets an address the policy
# checked, never a name, so it can't be made to resolve anything.
Connector = Callable[[str, int, float], socket.socket]
# Monotonic seconds; injected so the per-minute windows can be tested without
# waiting a minute.
Clock = Callable[[], float]

# Bytes per recv while reading the head. The cap is checked after each read,
# so the buffer never holds more than head_max_bytes plus one read.
HEAD_READ_BYTES = 4096
# Bytes per recv while relaying.
RELAY_READ_BYTES = 65536


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
    """Distinct sockaddr[0] strings, in order, of getaddrinfo(name + ".", 443, family, SOCK_STREAM, IPPROTO_TCP).

    family is passed through so a spy can prove AF_INET; the server always
    passes socket.AF_INET, because IPv6 answers would need a second address
    policy and the deployment's networks route IPv4 only (G5).

    The name is looked up as an absolute name (a trailing dot). A relative
    name lets glibc's resolver retry it with every `search` domain of
    /etc/resolv.conf, which Docker copies from the host, after an NXDOMAIN
    the client can force by choosing the first label: `x.youtube.com` would
    become `x.youtube.com.<search domain>`, and a wildcard record there would
    hand the forwarder an address the allowlist never named. The dot only
    changes the lookup: the client's TLS, SNI included, is relayed as sent.
    parse_connect refuses a name with a trailing dot, so the name never has
    one already.
    """
    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
        name + ".", ALLOWED_PORT, family, socket.SOCK_STREAM, socket.IPPROTO_TCP
    ):
        address = sockaddr[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def connect(address: str, port: int, timeout: float) -> socket.socket:
    """socket.create_connection((address, port), timeout) and nothing else: no name ever reaches it.

    Connecting to the address the policy checked, rather than to the name,
    closes the window in which a second lookup could answer differently (G6).
    """
    return socket.create_connection((address, port), timeout)


def outcome_line(outcome: Outcome) -> str | None:
    """The log line for an outcome, or None when it logs nothing ("ok", "refused:empty").

    Format: f"forwarder {kind} {token} {outcome.name or '-'}\\n" where kind is
    "refused" or "cut" and token the part after the colon.
    """
    kind, _, token = outcome.reason.partition(":")
    if kind not in ("refused", "cut") or outcome.reason == "refused:empty":
        return None
    # The name is checked again here, not only where it was parsed: whatever
    # builds an Outcome, nothing outside the hostname grammar is printed.
    name = loggable_name(outcome.name) if outcome.name is not None else None
    return f"forwarder {kind} {token} {name or '-'}\n"


def log_outcome(outcome: Outcome, log: TextIO) -> None:
    """One line for refusals (except "refused:empty") and cuts, nothing for "ok" (outcome_line).

    Accepted tunnels and empty connections (health checks) log nothing, so
    the log holds only what an operator must look at, and never payload or
    header text (G10). Flushed per line, so a block-buffered stream loses no
    refusal when the container stops. The Forwarder's default sink writes
    through this function under its log_lines_per_minute budget.
    """
    line = outcome_line(outcome)
    if line is None:
        return
    log.write(line)
    log.flush()


class Window:
    """A sliding 60-second budget: add(amount) records it and returns True, or returns False and records nothing.

    Used for new tunnels per minute and upload bytes per minute (G8, G9); a
    refused add records nothing, so a client hammering the limit doesn't push
    its own recovery further away.
    """

    def __init__(self, limit: int, clock: Clock, seconds: float = 60.0) -> None:
        self.limit = limit
        self.clock = clock
        self.seconds = seconds
        # (time, amount) in time order, and their sum, so an add costs the
        # entries that expired since the last one and nothing more.
        self._entries: deque[tuple[float, int]] = deque()
        self._total = 0
        # Every tunnel thread shares the window.
        self._lock = threading.Lock()

    def add(self, amount: int = 1) -> bool:
        with self._lock:
            now = self.clock()
            while self._entries and now - self._entries[0][0] >= self.seconds:
                self._total -= self._entries.popleft()[1]
            if self._total + amount > self.limit:
                return False
            self._entries.append((now, amount))
            self._total += amount
            return True


class _Cut(Exception):
    """A relay cap was hit; the reason is the log token after "cut:"."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


def _close(sock: socket.socket | None) -> None:
    # Shut down before closing, so the peer sees the end even if another
    # reference to the socket (socketserver's own) is still open.
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


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
        self.limits = limits
        self.resolver = resolver
        self.connector = connector
        self.clock = clock
        self.log = log
        self.sink: Sink = sink if sink is not None else self._log_outcome
        # Open client connections (G8): taken in process_request, before a
        # thread exists, and given back when the connection's thread ends.
        self._connections = threading.BoundedSemaphore(limits.connections_max)
        # Concurrent tunnels (G8): taken at step 3, after the head is parsed
        # and the name allowed, so a refused request costs no slot.
        self._tunnels = threading.BoundedSemaphore(limits.tunnels_max_concurrent)
        # The two sliding minutes are shared by every tunnel, so many small
        # tunnels can't add up to more than one minute's budget.
        self._tunnel_window = Window(limits.tunnels_per_minute, clock)
        self._upload_window = Window(limits.upload_bytes_per_minute, clock)
        # The log's own budget: refusals cost no tunnel budget, so this is
        # what stops a client from filling the host's disk with log lines.
        # Lines over it are dropped and counted, and the count is printed
        # once the window has room again. The lock keeps that count and the
        # line order consistent across tunnel threads.
        self._log_window = Window(limits.log_lines_per_minute, clock)
        self._log_lock = threading.Lock()
        self._log_suppressed = 0
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, TunnelHandler)

    def _log_outcome(self, outcome: Outcome) -> None:
        # The default sink. Outcomes that log nothing take no log budget.
        line = outcome_line(outcome)
        if line is not None:
            self._write_log(line)

    def _write_log(self, line: str) -> None:
        """Write one line to the log within log_lines_per_minute, or count it as suppressed.

        At most log_lines_per_minute lines per sliding minute, plus one
        `forwarder log suppressed <n>` line when the window has room again
        after dropping n lines, so a flood costs the host a bounded amount of
        disk and the operator still sees that it happened.
        """
        with self._log_lock:
            if not self._log_window.add(1):
                self._log_suppressed += 1
                return
            # Looked up per line, so a stderr redirected after start is honoured.
            log = self.log if self.log is not None else sys.stderr
            if self._log_suppressed:
                log.write(f"forwarder log suppressed {self._log_suppressed}\n")
                self._log_suppressed = 0
            log.write(line)
            log.flush()

    def server_bind(self) -> None:
        """TCPServer.server_bind only: no socket.getfqdn(), a reverse DNS lookup nobody needs (as FetchServer)."""
        socketserver.TCPServer.server_bind(self)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        """connections_max gate: beyond it, close at once and emit Outcome(None, "refused:connections", 0, 0, 0.0).

        Closed without a response, before a thread is spent on it, so a flood
        of half-open connections can't exhaust threads (G8).
        """
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            self.sink(Outcome(None, "refused:connections", 0, 0, 0.0))
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # No thread was started, so nothing else will give the slot back.
            self._connections.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # One line with the exception's type and no traceback: a traceback or
        # a message could quote bytes a client sent (G10). It goes to the
        # same log, under the same budget, as every other line.
        error = sys.exc_info()[1]
        self._write_log(f"forwarder error {type(error).__name__}\n")

    def handle_tunnel(self, client: socket.socket) -> None:
        """Steps 1 to 6 for one accepted connection; always closes client; always calls the sink exactly once.

        (The one exception is a bug escaping as an unexpected exception:
        handle_error reports it, and no Outcome is made up for it.)
        """
        # Deadlines, idle and lifetime run on real time: they bound sockets,
        # which only real time moves. The injected clock drives the windows.
        accepted = time.monotonic()
        name: str | None = None
        # Set by every path the forwarder knows. It stays None only when a bug
        # escapes as an exception, which handle_error reports instead: an
        # Outcome then would have to invent a reason, and "ok" would lie.
        reason: str | None = None
        counts = [0, 0]  # upload, download
        upstream: socket.socket | None = None
        holds_tunnel = False
        try:
            try:
                head, after_head = self._read_head(client, accepted)
                # Steps 1 and 2 decide on the head alone and touch no budget.
                name = parse_connect(head)
                if not name_allowed(name):
                    raise Refusal(403, "unlisted", name)
                # Step 3, still before any lookup: a client over budget makes
                # no DNS query (G4). The slot first, then the minute, so a 429
                # records nothing in the window and gives the slot back.
                if not self._tunnels.acquire(blocking=False):
                    raise Refusal(503, "busy", name)
                holds_tunnel = True
                if not self._tunnel_window.add(1):
                    raise Refusal(429, "rate", name)
                addresses = check_addresses(self._resolve(name), name)
                upstream = self._connect(addresses, name)
            except Refusal as refusal:
                name = refusal.name
                reason = f"refused:{refusal.reason}"
                if refusal.status is not None:
                    self._send_refusal(client, refusal.status)
                return
            reason = self._relay(client, upstream, after_head, accepted, counts)
        finally:
            _close(upstream)
            _close(client)
            # The slot is free before the sink hears of it, so whoever waits
            # for an outcome can open the next tunnel at once.
            if holds_tunnel:
                self._tunnels.release()
            if reason is not None:
                self.sink(Outcome(name, reason, counts[0], counts[1], time.monotonic() - accepted))

    def _read_head(self, client: socket.socket, accepted: float) -> tuple[bytes, bytes]:
        """Step 1's reading (G7): the head and the bytes after it, or a Refusal for size, time or EOF."""
        limits = self.limits
        deadline = accepted + limits.head_deadline_seconds
        buffer = b""
        while True:
            split = split_head(buffer)
            if split is not None:
                head, after_head = split
                # The cap is on the head, blank line included; bytes after it
                # are the client's TLS, already counted as upload, not head.
                if len(head) > limits.head_max_bytes:
                    raise Refusal(431, "head_too_large")
                return head, after_head
            # No blank line within the cap: the head can only end past it, so
            # refuse now rather than wait for the deadline.
            if len(buffer) >= limits.head_max_bytes:
                raise Refusal(431, "head_too_large")
            # The deadline runs from accept, not from the last byte, so a
            # client trickling header lines can't hold a connection open.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refusal(408, "head_timeout")
            client.settimeout(remaining)
            try:
                chunk = client.recv(HEAD_READ_BYTES)
            except TimeoutError:
                raise Refusal(408, "head_timeout") from None
            except OSError:
                # A reset is an end like EOF: nobody is left to answer.
                chunk = b""
            if not chunk:
                # Nothing at all is a health check or a port scan: no reply,
                # no log line. A partial head is malformed.
                if not buffer:
                    raise Refusal(None, "empty")
                raise Refusal(400, "malformed")
            buffer += chunk

    def _resolve(self, name: str) -> Sequence[str]:
        """Step 4: the resolver's answer for (name, AF_INET) within resolve_deadline_seconds, or a Refusal."""
        result: list[Sequence[str]] = []
        failed: list[BaseException] = []

        def lookup() -> None:
            try:
                result.append(self.resolver(name, socket.AF_INET))
            except Exception as error:
                # Any failure is a failed lookup; only its existence is kept,
                # never its message, which can quote the name or a resolver's
                # reply.
                failed.append(error)

        # getaddrinfo has no timeout of its own, so the lookup runs in a
        # daemon thread that is abandoned at the deadline; it holds no socket
        # of the tunnel and ends whenever the system resolver gives up.
        thread = threading.Thread(target=lookup, name="forwarder-resolve", daemon=True)
        thread.start()
        thread.join(self.limits.resolve_deadline_seconds)
        if thread.is_alive():
            raise Refusal(504, "resolve_timeout", name)
        if failed or not result:
            raise Refusal(502, "resolve_failed", name)
        return result[0]

    def _connect(self, addresses: Sequence[Any], name: str) -> socket.socket:
        """Step 5 (G6): the first checked address that connects on 443, or a Refusal; never a second lookup."""
        timed_out = False
        for address in addresses:
            try:
                # str(address) of a checked IPv4Address: the connector never
                # sees the name, so it can't resolve it again.
                return self.connector(str(address), ALLOWED_PORT, float(self.limits.connect_timeout_seconds))
            except TimeoutError:
                timed_out = True
            except OSError:
                timed_out = False
        if timed_out:
            raise Refusal(504, "connect_timeout", name)
        raise Refusal(502, "connect_failed", name)

    def _send_refusal(self, client: socket.socket, status: int) -> None:
        # Best effort within the idle timeout: a client that has gone or
        # doesn't read gets nothing more, and the outcome is the same.
        try:
            client.settimeout(self.limits.idle_timeout_seconds)
            client.sendall(refusal_bytes(status))
        except OSError:
            pass

    def _relay(
        self,
        client: socket.socket,
        upstream: socket.socket,
        after_head: bytes,
        accepted: float,
        counts: list[int],
    ) -> str:
        """Step 6 (G9): relay until EOF ("ok") or a cap ("cut:<token>"); counts holds [upload, download]."""
        limits = self.limits
        end_of_life = accepted + limits.lifetime_seconds
        try:
            self._send(client, ACCEPT_BYTES, end_of_life)
            # The bytes that came with the head are the start of the client's
            # TLS: relayed first, unparsed, and counted as upload (G7).
            if after_head:
                self._upload(upstream, after_head, end_of_life, counts)
            last_activity = time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(client, selectors.EVENT_READ, True)
                selector.register(upstream, selectors.EVENT_READ, False)
                while True:
                    now = time.monotonic()
                    if now >= end_of_life:
                        raise _Cut("lifetime")
                    idle_left = last_activity + limits.idle_timeout_seconds - now
                    if idle_left <= 0:
                        raise _Cut("idle")
                    for key, _events in selector.select(min(idle_left, end_of_life - now)):
                        from_client = key.data
                        source = client if from_client else upstream
                        chunk = source.recv(RELAY_READ_BYTES)
                        if not chunk:
                            # TLS clients don't half-close, so one side's EOF
                            # ends the tunnel for both.
                            return "ok"
                        if from_client:
                            self._upload(upstream, chunk, end_of_life, counts)
                        else:
                            # The chunk that would pass the cap is dropped and
                            # not counted: the cap is what may ever arrive.
                            if counts[1] + len(chunk) > limits.download_bytes_per_tunnel:
                                raise _Cut("download_cap")
                            self._send(client, chunk, end_of_life)
                            counts[1] += len(chunk)
                        last_activity = time.monotonic()
        except _Cut as cut:
            return f"cut:{cut.token}"
        except OSError:
            # A reset or a send that failed: the peer has gone, which ends the
            # tunnel as EOF would.
            return "ok"

    def _upload(self, upstream: socket.socket, chunk: bytes, end_of_life: float, counts: list[int]) -> None:
        # The per-tunnel cap first, then the shared minute, so a chunk refused
        # by the cap takes nothing from the minute; a refused chunk is neither
        # sent nor counted.
        if counts[0] + len(chunk) > self.limits.upload_bytes_per_tunnel:
            raise _Cut("upload_cap")
        if not self._upload_window.add(len(chunk)):
            raise _Cut("upload_rate")
        self._send(upstream, chunk, end_of_life)
        counts[0] += len(chunk)

    def _send(self, sock: socket.socket, data: bytes, end_of_life: float) -> None:
        # A peer that stops reading would block sendall forever; bound it by
        # the idle timeout and by what is left of the lifetime, whichever
        # comes first, and cut with the matching reason.
        now = time.monotonic()
        life_left = end_of_life - now
        if life_left <= 0:
            raise _Cut("lifetime")
        idle = self.limits.idle_timeout_seconds
        sock.settimeout(min(idle, life_left))
        try:
            sock.sendall(data)
        except TimeoutError:
            raise _Cut("lifetime" if life_left < idle else "idle") from None


class TunnelHandler(socketserver.BaseRequestHandler):
    """Hands each accepted connection to Forwarder.handle_tunnel, which owns every decision."""

    server: Forwarder

    def handle(self) -> None:
        self.server.handle_tunnel(self.request)
