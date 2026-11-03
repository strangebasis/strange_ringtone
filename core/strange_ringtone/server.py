"""The core HTTP server: routes, response headers and the checks before a render.

Plain http.server on purpose: the core is stdlib-only, sits behind a reverse
proxy that terminates TLS, and serves one request per connection. Every check
that decides whether a request is allowed lives in http_guard; this module
applies them in the order of docs/PLAN.md 5.2 and turns refusals into JSON.
"""
from __future__ import annotations

import http.server
import io
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from strange_ringtone.config import Settings
from strange_ringtone.http_guard import (
    ConcurrencyGate,
    GuardError,
    RateLimiter,
    body_length,
    client_address,
    require_content_type,
    require_origin,
    security_headers,
)
from strange_ringtone.names import content_disposition, parse_tone_name
from strange_ringtone.render import FFMPEG, Renderer, RenderError, RenderParams, default_prlimit
from strange_ringtone.results import ResultStore, StoreFull
from strange_ringtone.static import ALLOWED, StaticFiles
from strange_ringtone.wav import WavError, inspect_wav

SERVER_NAME = "strange_ringtone"
RENDER_PATH = "/api/render"
WAV_TYPE = "audio/wav"
# /api/result/<32 hex>.m4a or .m4r. The ID's exact shape is part of the route,
# so a path trick ("..", "%2f", a longer ID) is simply not a result route.
RESULT_PATH_RE = re.compile(r"/api/result/([0-9a-f]{32})\.(m4a|m4r)")
# The same bytes under both names; only the type differs (docs/PLAN.md 5.3).
RESULT_TYPES = {"m4a": "audio/mp4", "m4r": "audio/x-m4r"}
# How often the sweeper looks for expired results. Small against the TTL, so
# a result outlives its TTL by at most this much on disk; get() refuses it the
# moment the TTL is up.
SWEEP_INTERVAL_SECONDS = 5.0
# A full results store frees a slot only when the oldest result expires, so a
# retry in one second (as for busy) would mostly meet the same answer.
RESULTS_FULL_RETRY_SECONDS = 10

# Render options in the query string (docs/PLAN.md 5.1). Defaults and bounds
# are the plan's; the target's default comes from DEFAULT_TARGET_LUFS.
FADE_IN_DEFAULT = 0.2
FADE_OUT_DEFAULT = 1.5
FADE_MAX_SECONDS = 5.0
TARGET_LUFS_MIN = -24.0
TARGET_LUFS_MAX = -9.0
_QUERY_KEYS = frozenset({"fade_in", "fade_out", "normalize", "target_lufs"})
# What the web app's URLSearchParams produces for these keys and values. No
# '%' or '+': parse_qs would decode them, and a decoded key or number is
# something the client could spell in many ways.
_QUERY_RE = re.compile(r"[A-Za-z0-9_.=&-]*")
# A plain decimal, as config.py accepts: no exponent, inf, nan, sign other
# than '-', or whitespace, all of which float() would take.
_DECIMAL_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DECIMAL_MAX_CHARS = 16
_QUERY_MAX_FIELDS = 8
# The body is copied to disk in pieces, so a 5.8 MB upload never sits in memory
# whole and a read never asks for more than the declared length.
CHUNK_BYTES = 65536
# Wall-clock budget for receiving a whole body, counted from when it holds a
# render slot. The socket timeout alone only bounds the gap between two reads,
# so a client trickling one byte every few seconds could keep a slot for ever
# and, with two connections, lock everyone else out of rendering. 60 s moves
# the largest body (MAX_CLIP_SECONDS 40, about 7.7 MB) at roughly 1 Mbit/s.
BODY_DEADLINE_SECONDS = 60
IMMUTABLE = "public, max-age=31536000, immutable"
NO_STORE = "no-store"
# Caps what one log line can carry from a client-chosen path.
_LOG_PATH_MAX = 200
_LOG_METHOD_MAX = 16

RenderHook = Callable[[Path, "Handler"], None]


@dataclass(frozen=True)
class RenderRequest:
    """The render options and tone name, checked before the request costs anything."""

    fade_in: float
    fade_out: float
    normalize: bool
    target_lufs: float
    name: str


def _decimal(values: list[str], low: float, high: float) -> float:
    value = values[0]
    if len(value) > _DECIMAL_MAX_CHARS or not _DECIMAL_RE.fullmatch(value):
        raise GuardError(400, "bad_params")
    number = float(value)
    if not low <= number <= high:
        raise GuardError(400, "bad_params")
    return number


def parse_render_request(path: str, headers: Message, settings: Settings) -> RenderRequest:
    """The query's render options and the X-Tone-Name header, or GuardError 400.

    Pure, so every refusal is tested on its own. Anything not positively
    recognised is refused: unknown or repeated keys, numbers in any spelling
    but a plain decimal, and values out of range.
    """
    name = parse_tone_name(headers.get_all("X-Tone-Name") or [])
    try:
        query = urlsplit(path).query
    except ValueError:
        raise GuardError(400, "bad_params") from None
    fields: dict[str, list[str]] = {}
    if query:
        if not _QUERY_RE.fullmatch(query):
            raise GuardError(400, "bad_params")
        try:
            # strict_parsing refuses empty fields and fields without '=';
            # max_num_fields bounds the work a long query can cause.
            fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=_QUERY_MAX_FIELDS)
        except ValueError:
            raise GuardError(400, "bad_params") from None
    # A repeated key could be read differently by a proxy, a client library
    # and this server, so it is refused rather than resolved.
    if set(fields) - _QUERY_KEYS or any(len(values) != 1 for values in fields.values()):
        raise GuardError(400, "bad_params")
    normalize = True
    if "normalize" in fields:
        value = fields["normalize"][0]
        if value not in ("true", "false"):
            raise GuardError(400, "bad_params")
        normalize = value == "true"
    return RenderRequest(
        fade_in=_decimal(fields["fade_in"], 0.0, FADE_MAX_SECONDS) if "fade_in" in fields else FADE_IN_DEFAULT,
        fade_out=_decimal(fields["fade_out"], 0.0, FADE_MAX_SECONDS) if "fade_out" in fields else FADE_OUT_DEFAULT,
        normalize=normalize,
        target_lufs=(
            _decimal(fields["target_lufs"], TARGET_LUFS_MIN, TARGET_LUFS_MAX)
            if "target_lufs" in fields
            else settings.default_target_lufs
        ),
        name=name,
    )


def render_and_store(job_dir: Path, handler: Handler) -> None:
    """The render hook: validate in.wav, render it, keep the result, answer JSON.

    Runs inside the render slot with job_dir/in.wav complete. The handler
    deletes job_dir before it sends the answer, so out.m4a is moved into the
    results store before this returns.
    """
    server = handler.server
    request = handler.render_request
    try:
        info = inspect_wav(job_dir / "in.wav", server.settings.max_clip_seconds)
    except WavError as error:
        # These two carry a detail, which GuardError can't, so they are
        # answered here. The detail is one of the fixed codes in wav.py.
        if error.code == "too_long":
            handler.send_json(413, {"error": "too_large", "detail": "too_long"})
        else:
            handler.send_json(400, {"error": "bad_wav", "detail": error.code})
        return
    params = RenderParams(
        fade_in=request.fade_in,
        fade_out=request.fade_out,
        normalize=request.normalize,
        target_lufs=request.target_lufs,
        title=request.name,
        duration=info.seconds,
    )
    try:
        rendered = server.renderer.render(job_dir, params)
    except RenderError as error:
        # measure_failed is a render failure to the client; what ffmpeg said
        # stays inside the renderer.
        if error.code == "render_timeout":
            raise GuardError(504, "render_timeout") from None
        raise GuardError(500, "render_failed") from None
    try:
        result = server.results.put(rendered.output, request.name)
    except StoreFull:
        # Another render filled the store while this one ran.
        raise GuardError(503, "results_full", retry_after=RESULTS_FULL_RETRY_SECONDS) from None
    handler.send_json(200, {
        "result_id": result.result_id,
        "duration": round(rendered.duration, 3),
        "measured_lufs": round(rendered.measured_lufs, 2),
        "applied_gain_db": round(rendered.applied_gain_db, 2),
    })


def _printable(text: str, limit: int) -> str:
    # Log lines are plain ASCII: a client can't inject a newline, an ANSI escape
    # or a fake second entry through the path or method it sends.
    cleaned = "".join(ch if 0x21 <= ord(ch) <= 0x7E else "_" for ch in text[:limit])
    return cleaned or "-"


_LOG_ID_RE = re.compile(r"[0-9A-Fa-f]{32,}")


def _log_path(raw: str) -> str:
    # The query string holds the render options, and a client could put
    # anything there; logs keep the path only (docs/PLAN.md 5.5).
    path = raw.split("?", 1)[0].split("#", 1)[0]
    # A result ID is the only thing needed to download a tone, so a log line
    # must not hand one to whoever reads the logs. The mask applies to every
    # path, not only the result route: a live ID sent to a mistyped or
    # case-varied route is just as much a token.
    path = _LOG_ID_RE.sub("<id>", path)
    return _printable(path, _LOG_PATH_MAX)


def _prepare_scratch(directory: Path) -> None:
    """Create the scratch directory owner-only and empty it."""
    try:
        # No parents: if /tmp itself is missing, the container lacks the tmpfs
        # the contract requires, and creating /tmp would write to the root
        # filesystem instead. Refusing to start says so plainly.
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"cannot create the scratch directory {directory}: {error.strerror}") from error
    info = os.lstat(directory)
    # Outside the container the default lives in a shared /tmp, where another
    # user could have planted a symlink or a directory of their own to collect
    # uploads. Refuse to start instead of writing into it.
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"scratch path is not a plain directory: {directory}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"scratch directory is owned by another user: {directory}")
    # Safe because mode 0o700 means only this user can list, enter or write the uploads directory.
    os.chmod(directory, 0o700)  # noqa: E501  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    # Startup clears whatever an earlier process left behind (docs/PLAN.md
    # 5.5): the core keeps no state across restarts.
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


class Handler(http.server.BaseHTTPRequestHandler):
    server: RingtoneServer
    # Set by _render once the query and the name have passed, for the hook.
    render_request: RenderRequest

    # One request per connection, always closed afterwards: no keep-alive state
    # to reason about, and no worker parked on an idle socket. It also means
    # the stdlib never sends "100 Continue", so nothing invites a body before
    # the checks have run.
    protocol_version = "HTTP/1.0"
    # Socket timeout for each read and write, so a client that stalls mid-body
    # frees its worker. BODY_DEADLINE_SECONDS bounds the whole body; other
    # slow-client defence is the proxy's job.
    timeout = 30
    server_version = SERVER_NAME
    sys_version = ""
    # The stdlib's HTML error page would echo the reason phrase; this names only
    # the status, as JSON like every other error. send_error below replaces the
    # stdlib writer, and this stays as a second line of defence.
    error_message_format = '{"error": "http_%(code)d"}'
    error_content_type = "application/json"

    # --- response plumbing -------------------------------------------------

    def version_string(self) -> str:
        # The stdlib appends " " + sys_version even when that is empty, which
        # leaves a trailing space; and the Python version is nobody's business.
        return SERVER_NAME

    def send_response(self, code: int, message: str | None = None) -> None:
        # Some stdlib error paths run while request_version still says HTTP/0.9,
        # and http.server writes no status line or headers for 0.9. Every
        # response must carry the header set, so it is always HTTP/1.0 here.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self._status = code
        super().send_response(code, message)
        # Added here, the one place every response passes through, so the
        # stdlib's own error paths (400, 414, 431, 501) carry them too.
        for name, value in security_headers(self.server.settings.public_origin):
            self.send_header(name, value)
        self.send_header("Connection", "close")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # The stdlib version adds a second Connection header and no
        # Cache-Control; its errors go through the same JSON writer instead.
        self.send_json_error(code, f"http_{code}")

    def send_body(
        self,
        status: int,
        body: bytes,
        content_type: str | None,
        cache_control: str,
        extra_headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Write a complete response. Every response states its length and caching."""
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict[str, Any], extra_headers: Iterable[tuple[str, str]] = ()) -> None:
        """A JSON response that no cache keeps. Render hooks answer through this."""
        body = json.dumps(payload).encode("utf-8")
        self.send_body(status, body, "application/json", NO_STORE, extra_headers)

    def send_json_error(
        self, status: int, code: str, retry_after: int | None = None, allow: str | None = None
    ) -> None:
        if self._status is not None:
            # A response has already started (a hook failed halfway, say); a
            # second one would corrupt the stream, so the connection just closes.
            return
        extra: list[tuple[str, str]] = []
        if retry_after is not None:
            extra.append(("Retry-After", str(retry_after)))
        if allow is not None:
            extra.append(("Allow", allow))
        self.send_json(status, {"error": code}, extra)

    # --- logging -------------------------------------------------------------

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Replaced by the single line written from handle_one_request.
        pass

    def log_error(self, format: str, *args: Any) -> None:
        # The stdlib's messages quote the request line, which carries the query.
        pass

    def log_message(self, format: str, *args: Any) -> None:
        # No client address, headers or timestamp: a line holds the method, the
        # path without its query, the status and the duration, nothing else
        # (docs/PLAN.md 5.5). sys.stderr is looked up on each call, so a
        # redirected stderr receives the lines.
        sys.stderr.write((format % args) + "\n")

    def handle_one_request(self) -> None:
        self._status: int | None = None
        self.command = ""
        self.path = ""
        started = time.monotonic()
        try:
            super().handle_one_request()
        finally:
            # A connection that closed without sending a request logs nothing.
            if self._status is not None or self.command:
                self.log_message(
                    "%s %s %s %.3f",
                    _printable(self.command or "", _LOG_METHOD_MAX),
                    _log_path(self.path or ""),
                    self._status if self._status is not None else "-",
                    time.monotonic() - started,
                )

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        # HTTP/0.9 has no headers in either direction, so its response could not
        # carry the security headers. Nothing but scanners sends it any more.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
            self.send_json_error(400, "http_400")
            return False
        # Every other method, whatever its spelling (HEAD, OPTIONS, TRACE,
        # CONNECT, a lower-case "get", any token), is refused here with a JSON
        # 405 and an Allow header, or a 404 on an unknown path: never the
        # stdlib's 501 without Allow, and never a CORS approval.
        if self.command not in ("GET", "POST"):
            self._refuse_method()
            return False
        return True

    # --- routing -------------------------------------------------------------

    def _route_path(self) -> str | None:
        # Origin-form only ("/..."). The path is compared as sent: nothing is
        # percent-decoded or normalised, so "%2e%2e", "%2f", "\" and case
        # variants simply match no route.
        if not self.path.startswith("/"):
            return None
        # The stdlib rewrites the target before it reaches self.path: it splits
        # the request line on any whitespace (so a trailing \x0b or \xa0
        # vanishes) and collapses leading slashes (gh-87389). Route only a
        # target that is exactly what the client sent between single spaces,
        # so no alias of a route is ever served. "//" stays refused even if a
        # stdlib stops collapsing it, because urlsplit would read the first
        # segment as a host and route what follows.
        fields = self.requestline.split(" ")
        if len(fields) != 3 or fields[1] != self.path or self.path.startswith("//"):
            return None
        try:
            return urlsplit(self.path).path
        except ValueError:
            # urlsplit refuses some malformed netlocs; such a path is no route.
            return None

    def _static_name(self, path: str) -> str | None:
        prefix = "/static/" + self.server.settings.version + "/"
        if not path.startswith(prefix):
            return None
        name = path[len(prefix):]
        if name in ALLOWED and self.server.static.get(name) is not None:
            return name
        return None

    def _allowed_methods(self, path: str | None) -> str | None:
        if path is None:
            return None
        if path in ("/", "/healthz") or self._static_name(path) is not None:
            return "GET"
        # Any well-formed result path, live or not: HEAD or POST to it is a
        # 405 whether or not the ID exists, so the answer reveals nothing.
        if RESULT_PATH_RE.fullmatch(path):
            return "GET"
        if path == RENDER_PATH:
            return "POST"
        return None

    def _refuse_method(self) -> None:
        allow = self._allowed_methods(self._route_path())
        if allow is None:
            self.send_json_error(404, "not_found")
        else:
            self.send_json_error(405, "method_not_allowed", allow=allow)

    def do_GET(self) -> None:
        path = self._route_path()
        if path == "/":
            self.send_body(200, self.server.index_html, ALLOWED["index.html"], NO_STORE)
            return
        if path == "/healthz":
            # Empty on purpose: no version, uptime or settings for a scanner.
            self.send_body(200, b"", None, NO_STORE)
            return
        name = self._static_name(path) if path is not None else None
        asset = self.server.static.get(name) if name is not None else None
        if asset is not None:
            body, content_type = asset
            # Safe to cache forever: a release changes VERSION, and with it
            # every asset URL.
            self.send_body(200, body, content_type, IMMUTABLE)
            return
        match = RESULT_PATH_RE.fullmatch(path) if path is not None else None
        if match is not None:
            self._send_result(match[1], match[2])
            return
        self._refuse_method()

    def _send_result(self, result_id: str, extension: str) -> None:
        # Unknown, expired and swept IDs all get the same 404, so a client
        # learns nothing about which IDs ever existed.
        result = self.server.results.get(result_id)
        body: bytes | None = None
        if result is not None:
            try:
                # Read whole and closed at once: the sweeper may delete the file
                # between get() and here, and Windows can't delete an open file.
                with open(result.path, "rb") as handle:
                    body = handle.read()
            except OSError:
                body = None
        if result is None or body is None:
            self.send_json_error(404, "not_found")
            return
        self.send_body(
            200,
            body,
            RESULT_TYPES[extension],
            # A tone is private to whoever rendered it; no cache keeps a copy.
            NO_STORE,
            [("Content-Disposition", content_disposition(result.name, extension))],
        )

    def do_POST(self) -> None:
        # Method and path come first (docs/PLAN.md 5.2 step 1): an unknown path
        # is a 404 whatever its headers say.
        if self._route_path() != RENDER_PATH:
            self._refuse_method()
            return
        try:
            self._render()
        except GuardError as error:
            self.send_json_error(error.status, error.code, error.retry_after)

    # --- the render endpoint -------------------------------------------------

    def _render(self) -> None:
        settings = self.server.settings
        headers = self.headers
        # docs/PLAN.md 5.2, in order. Each step raises GuardError and nothing
        # after it runs; no body byte is read until all of them pass.
        require_origin(headers.get_all("Origin") or [], settings.public_origin)
        require_content_type(headers.get_all("Content-Type") or [], WAV_TYPE)
        length = body_length(
            headers.get_all("Content-Length") or [],
            headers.get_all("Transfer-Encoding") or [],
            settings.max_body_bytes,
        )
        # The query and the name are checked before the rate limit and the
        # slot, so a refused request never uses up the client's budget.
        self.render_request = parse_render_request(self.path, headers, settings)
        client = client_address(
            self.client_address[0], headers.get_all("X-Forwarded-For") or [], settings.trusted_proxies
        )
        # Step 4: both limits must have room. A client over its limit gets 429
        # whether or not the gate is full, but a render is counted only once
        # the gate admits it: a 503 busy ran no render, so a client retrying
        # after its Retry-After must not burn its budget on refusals.
        allowed, retry_after = self.server.limiter.has_room(client)
        if not allowed:
            raise GuardError(429, "rate_limited", retry_after=retry_after)
        # Nothing is evicted early (docs/PLAN.md 5.5), so with MAX_RESULTS
        # results waiting a render would have nowhere to go. Refused before the
        # slot, like busy: no upload, no render, no budget used.
        if self.server.results.is_full():
            raise GuardError(503, "results_full", retry_after=RESULTS_FULL_RETRY_SECONDS)
        with self.server.gate.slot():
            # Counted here, atomically: a parallel request from the same client
            # may have taken the last hit since has_room answered.
            allowed, retry_after = self.server.limiter.allow(client)
            if not allowed:
                raise GuardError(429, "rate_limited", retry_after=retry_after)
            # A fresh directory per job, with names the server picked: nothing a
            # client sends ever becomes part of a path.
            try:
                job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=settings.scratch_dir))
            except OSError:
                # The scratch directory is gone or full. It is not recreated
                # here: the checks that make it safe to use run only at
                # startup. Every render fails, with the usual headers, until
                # a restart.
                raise GuardError(500, "internal_error") from None
            answer = b""
            try:
                failure = self._receive_body(job_dir / "in.wav", length)
                if failure is None:
                    answer = self._run_hook(job_dir)
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
        # Sent only now, after the job directory is gone and the slot is free:
        # a client that sends its next request as soon as it has read this
        # answer must not find its own render still holding the slot.
        if failure is not None:
            try:
                self.send_json_error(failure.status, failure.code)
            except OSError:
                # The client is gone; there is nobody to tell.
                pass
            return
        self.wfile.write(answer)

    def _run_hook(self, job_dir: Path) -> bytes:
        """Run the render hook with its response held in memory, and return that response.

        A result is a ringtone of at most 40 seconds, so holding it costs little,
        and an answer can be replaced whole if the hook fails halfway.
        """
        socket_file = self.wfile
        held = io.BytesIO()
        self.wfile = held
        try:
            try:
                self.server.render_hook(job_dir, self)
            except GuardError:
                self._discard_response()
                raise
            except Exception:
                # Details stay out of the response and the log: an exception
                # message could carry a tone name or a path.
                self._discard_response()
                held.seek(0)
                held.truncate()
                self.send_json_error(500, "internal_error")
        finally:
            self.wfile = socket_file
        return held.getvalue()

    def _discard_response(self) -> None:
        # Forget a response the hook began, so a clean error replaces it.
        self._status = None
        self._headers_buffer = []

    def _receive_body(self, target: Path, length: int) -> GuardError | None:
        """Stream exactly length body bytes into target; the refusal if that fails."""
        remaining = length
        deadline = time.monotonic() + BODY_DEADLINE_SECONDS
        try:
            # 'x': the file must not exist yet, in a directory created just now.
            # Anything already at that name (a file, a hard link to somewhere
            # else, a symlink) was not put there by this request, so the upload
            # never writes through it; the request fails with a clean 500.
            out = open(target, "xb")
        except OSError:
            return GuardError(500, "internal_error")
        try:
            # The with-block closes the handle before the hook or the cleanup
            # runs, because Windows can't delete a file that is still open.
            with out:
                while remaining > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return GuardError(408, "body_timeout")
                    # No single wait may run past the deadline either.
                    wait = min(self.timeout, left)
                    self.connection.settimeout(wait)
                    try:
                        # read1 makes at most one recv. read() would loop until
                        # it had the whole chunk, so a trickle of bytes could
                        # keep one call alive past the deadline. Never ask for
                        # more than is left: bytes past the declared length are
                        # not part of this request and are never read.
                        chunk = self.rfile.read1(min(CHUNK_BYTES, remaining))
                    except TimeoutError:
                        # Decided by which limit the wait had, not by the clock
                        # afterwards, which a timer that fires early can fool.
                        if wait < self.timeout:
                            return GuardError(408, "body_timeout")
                        # The socket timeout of a stalled upload.
                        return GuardError(400, "incomplete_body")
                    except OSError:
                        return GuardError(400, "incomplete_body")
                    if not chunk:
                        return GuardError(400, "incomplete_body")
                    out.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            # A full tmpfs fails the write, or the flush when the with-block
            # closes the file. Without this the error would escape and the
            # client would see a dropped connection instead of a JSON answer.
            return GuardError(500, "internal_error")
        finally:
            # The answer is written with the usual per-write timeout.
            self.connection.settimeout(self.timeout)
        return None


class RingtoneServer(http.server.ThreadingHTTPServer):
    # Worker threads never keep the process alive, and stopping does not wait
    # for them: the container must stop promptly on SIGTERM, and scratch is
    # cleared at the next start anyway. daemon_threads is what does it: the
    # stdlib never records daemon threads to join, so block_on_close = False
    # changes nothing today and only states the intent if that ever changes.
    daemon_threads = True
    block_on_close = False
    # On Windows SO_REUSEADDR lets a second process bind the same port and take
    # over its connections; on Linux it only allows a quick restart.
    allow_reuse_address = sys.platform != "win32"
    # Upper bound on handler threads (one per connection). Past it a connection
    # gets a fast 503 instead of a thread, so a flood can't exhaust memory.
    MAX_WORKERS = 16

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        render_hook: RenderHook | None = None,
        renderer: Renderer | None = None,
        results: ResultStore | None = None,
    ) -> None:
        self.settings = settings
        self.render_hook: RenderHook = render_hook if render_hook is not None else render_and_store
        # The image's ffmpeg at its fixed path. Building the Renderer checks
        # nothing on disk, so a checkout on Windows starts without ffmpeg and
        # only a render fails.
        self.renderer = (
            renderer
            if renderer is not None
            else Renderer((FFMPEG,), settings.render_timeout_seconds, prlimit=default_prlimit())
        )
        self.static = StaticFiles()
        # Rendered once: a broken template fails startup, and no request does
        # template work.
        self.index_html = self.static.render_index(settings)
        self.limiter = RateLimiter(settings.rate_limit_renders_per_minute)
        self.gate = ConcurrencyGate(settings.render_concurrency)
        self._workers = threading.BoundedSemaphore(self.MAX_WORKERS)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        # Binding comes first: when another instance already owns the port,
        # this one fails here, before it can empty the scratch directory that
        # the running instance is still using.
        super().__init__(address, Handler)
        try:
            _prepare_scratch(settings.scratch_dir)
            # Only after scratch is checked and emptied: the results directory
            # lives inside it, and startup forgets every earlier result.
            if results is None:
                results = ResultStore(
                    settings.scratch_dir / "results", settings.result_ttl_seconds, settings.max_results
                )
            else:
                # An injected store (tests) had its directory emptied, or
                # removed, with the rest of scratch just now.
                results.clear()
            self.results = results
            self.results.start_sweeper(SWEEP_INTERVAL_SECONDS)
        except BaseException:
            # Nobody will call server_close() on a half-built server.
            self.server_close()
            raise

    def server_close(self) -> None:
        # Also reached from a failed bind inside TCPServer.__init__, before
        # the results store exists.
        results = getattr(self, "results", None)
        if results is not None:
            results.stop()
        super().server_close()

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup.
        # The core makes no network queries at all, and where there is no
        # resolver that lookup stalls startup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._workers.acquire(blocking=False):
            self._refuse_busy(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The thread never started, so it can't release its own slot.
            self._workers.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()

    def _refuse_busy(self, request: socket.socket) -> None:
        # Written by hand because no Handler exists for this connection, with
        # the same headers a Handler would send.
        body = b'{"error": "busy"}'
        lines = ["HTTP/1.0 503 Service Unavailable", f"Server: {SERVER_NAME}"]
        lines += [f"{name}: {value}" for name, value in security_headers(self.settings.public_origin)]
        lines += [
            "Connection: close",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            f"Cache-Control: {NO_STORE}",
            "Retry-After: 1",
        ]
        data = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        try:
            # This runs on the accepting thread: a client that doesn't read must
            # not hold everyone else up for long.
            request.settimeout(1)
            request.sendall(data)
        except OSError:
            pass
        sys.stderr.write("- - 503 0.000\n")

    def handle_error(self, request: Any, client_address: Any) -> None:
        # One line, no traceback: a traceback can quote a header, a path or a
        # tone name. The exception type is enough to start looking.
        error = sys.exc_info()[1]
        sys.stderr.write(f"error while handling a request: {type(error).__name__}\n")
