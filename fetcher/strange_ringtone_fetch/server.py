"""The fetcher's HTTP server: the /fetch/ routes and the checks before a job is queued.

The same shape as the core's server (strange_ringtone.server), on purpose:
plain http.server behind a TLS-terminating proxy, one request per
connection, every refusal a JSON {"error": code} with the core's vocabulary,
and the core's own http_guard for the Origin, Content-Type, length,
X-Forwarded-For and rate-limit rules. The proxy forwards the /fetch/ prefix
unchanged on the page's own origin, so the page reaches this server with
connect-src 'self' and the core never talks to it.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from strange_ringtone.http_guard import (
    GuardError,
    RateLimiter,
    body_length,
    client_address,
    require_content_type,
    require_origin,
    security_headers,
)

from strange_ringtone_fetch.config import Settings
from strange_ringtone_fetch.jobs import Jobs, QueueFull, Runner
from strange_ringtone_fetch.ytdlp import VIDEO_ID_RE, YtDlp, default_prlimit

SERVER_NAME = "strange_ringtone"
HEALTH_PATH = "/fetch/healthz"
JOBS_PATH = "/fetch/api/jobs"
# The ID's exact shape is part of the route, so a path trick ("..", "%2f", a
# longer ID) is simply not a job route.
JOB_PATH_RE = re.compile(r"/fetch/api/jobs/([0-9a-f]{32})")
AUDIO_PATH_RE = re.compile(r"/fetch/api/jobs/([0-9a-f]{32})/audio")
JSON_TYPE = "application/json"
# {"video_id": "<11 characters>"} is 27 bytes; 1 KiB leaves room for
# whitespace and nothing that could cost anything to parse.
MAX_BODY_BYTES = 1024
# Wall-clock budget for receiving the body, as in the core.
BODY_DEADLINE_SECONDS = 60
# A full queue frees a place only when a fetch finishes, which takes seconds.
BUSY_RETRY_SECONDS = 5
SWEEP_INTERVAL_SECONDS = 30.0
CHUNK_BYTES = 65536
NO_STORE = "no-store"
_LOG_PATH_MAX = 200
_LOG_METHOD_MAX = 16
_LOG_ID_RE = re.compile(r"[0-9A-Fa-f]{32,}")


def parse_job_request(body: bytes) -> str:
    """The video ID from a POST body, or GuardError 400.

    Only a JSON object with exactly the key video_id, holding a string, is a
    request; anything else, duplicate keys included, is bad_json. The ID must
    then be exactly 11 characters of A-Z, a-z, 0-9, '_' and '-'.
    """
    try:
        text = body.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: deep nesting, which a 1 KiB body can't reach in
        # CPython 3.12's decoder but another build's might.
        raise GuardError(400, "bad_json") from None
    if not isinstance(data, dict) or set(data) != {"video_id"} or not isinstance(data["video_id"], str):
        raise GuardError(400, "bad_json")
    video_id = data["video_id"]
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise GuardError(400, "bad_video_id")
    return video_id


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # A repeated key could be read differently by a proxy and this server, so
    # it is refused rather than resolved.
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate key")
        data[key] = value
    return data


def _printable(text: str, limit: int) -> str:
    # Log lines are plain ASCII: a client can't inject a newline, an escape
    # sequence or a fake second entry.
    cleaned = "".join(ch if 0x21 <= ord(ch) <= 0x7E else "_" for ch in text[:limit])
    return cleaned or "-"


def _log_path(raw: str) -> str:
    path = raw.split("?", 1)[0].split("#", 1)[0]
    # A job ID is all it takes to read a job's title or download its audio,
    # so no log line hands one to whoever reads the logs.
    path = _LOG_ID_RE.sub("<id>", path)
    return _printable(path, _LOG_PATH_MAX)


def prepare_scratch(directory: Path) -> None:
    """Create the scratch directory owner-only and empty it (as the core's server does)."""
    try:
        # No parents: if /tmp itself is missing, the container lacks the tmpfs
        # the contract requires, and creating /tmp would write to the root
        # filesystem instead.
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise RuntimeError(f"cannot create the scratch directory {directory}: {error.strerror}") from error
    info = os.lstat(directory)
    # Outside the container the default lives in a shared /tmp, where another
    # user could have planted a symlink or a directory of their own.
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"scratch path is not a plain directory: {directory}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"scratch directory is owned by another user: {directory}")
    # Safe because 0o700 lets only this user list, enter or write the directory
    # that holds downloads; the execute bit is what makes a directory enterable.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(directory, 0o700)
    # The fetcher keeps no state across restarts.
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


class Handler(http.server.BaseHTTPRequestHandler):
    server: FetchServer

    # One request per connection, always closed afterwards; the stdlib then
    # never sends "100 Continue", so nothing invites a body before the checks.
    protocol_version = "HTTP/1.0"
    timeout = 30
    server_version = SERVER_NAME
    sys_version = ""
    error_message_format = '{"error": "http_%(code)d"}'
    error_content_type = "application/json"

    # --- response plumbing -------------------------------------------------

    def version_string(self) -> str:
        return SERVER_NAME

    def send_response(self, code: int, message: str | None = None) -> None:
        # Some stdlib error paths run while request_version still says
        # HTTP/0.9, for which http.server writes no headers at all.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self._status = code
        super().send_response(code, message)
        # The one place every response passes through, the stdlib's own error
        # paths included.
        for name, value in security_headers(self.server.settings.public_origin):
            self.send_header(name, value)
        self.send_header("Connection", "close")

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.send_json_error(code, f"http_{code}")

    def send_head(
        self,
        status: int,
        length: int,
        content_type: str | None,
        extra_headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Status line and headers. Nothing the fetcher answers may be cached."""
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", NO_STORE)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()

    def send_body(
        self, status: int, body: bytes, content_type: str | None, extra_headers: Iterable[tuple[str, str]] = ()
    ) -> None:
        self.send_head(status, len(body), content_type, extra_headers)
        # A refused HEAD gets the headers of its 405 and no body.
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload: dict[str, Any], extra_headers: Iterable[tuple[str, str]] = ()) -> None:
        self.send_body(status, json.dumps(payload).encode("utf-8"), JSON_TYPE, extra_headers)

    def send_json_error(
        self, status: int, code: str, retry_after: int | None = None, allow: str | None = None
    ) -> None:
        if self._status is not None:
            # A response has already started; a second one would corrupt it.
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
        # The stdlib's messages quote the request line.
        pass

    def log_message(self, format: str, *args: Any) -> None:
        # Method, path without query and with IDs masked, status, duration;
        # never a client address, a header, a video ID or a title.
        sys.stderr.write((format % args) + "\n")

    def handle_one_request(self) -> None:
        self._status: int | None = None
        self.command = ""
        self.path = ""
        started = time.monotonic()
        try:
            super().handle_one_request()
        finally:
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
        # HTTP/0.9 responses could not carry the security headers.
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
            self.send_json_error(400, "http_400")
            return False
        # Every other method (HEAD, OPTIONS, a lower-case "get", any token) is
        # a JSON 405 with Allow, or a 404 on an unknown path; never a CORS
        # approval.
        if self.command not in ("GET", "POST"):
            self._refuse_method()
            return False
        return True

    # --- routing -------------------------------------------------------------

    def _route_path(self) -> str | None:
        # The path as sent: nothing is percent-decoded or normalised, and a
        # target the stdlib rewrote (split on other whitespace, leading slashes
        # collapsed) matches no route (see strange_ringtone.server).
        if not self.path.startswith("/"):
            return None
        fields = self.requestline.split(" ")
        if len(fields) != 3 or fields[1] != self.path or self.path.startswith("//"):
            return None
        try:
            return urlsplit(self.path).path
        except ValueError:
            return None

    def _allowed_methods(self, path: str | None) -> str | None:
        if path is None:
            return None
        if path == HEALTH_PATH or JOB_PATH_RE.fullmatch(path) or AUDIO_PATH_RE.fullmatch(path):
            return "GET"
        if path == JOBS_PATH:
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
        if path == HEALTH_PATH:
            # Says only that the server answers: no version, queue or settings.
            self.send_body(200, b"ok", "text/plain; charset=utf-8")
            return
        match = JOB_PATH_RE.fullmatch(path) if path is not None else None
        if match is not None:
            status = self.server.jobs.status(match[1])
            if status is None:
                # Unknown, expired and collected IDs look the same.
                self.send_json_error(404, "not_found")
            else:
                self.send_json(200, status)
            return
        match = AUDIO_PATH_RE.fullmatch(path) if path is not None else None
        if match is not None:
            self._send_audio(match[1])
            return
        self._refuse_method()

    def _send_audio(self, job_id: str) -> None:
        jobs = self.server.jobs
        path = jobs.claim(job_id)
        if path is None:
            self.send_json_error(404, "not_found")
            return
        try:
            try:
                handle = open(path, "rb")
            except OSError:
                self.send_json_error(404, "not_found")
                return
            # The with-block closes the file before release() deletes it,
            # because Windows can't delete an open file.
            with handle:
                size = os.fstat(handle.fileno()).st_size
                self.send_head(
                    200,
                    size,
                    "audio/mp4",
                    # A fixed name: the title is the page's business, and it
                    # never goes into a header.
                    [("Content-Disposition", 'attachment; filename="audio.m4a"')],
                )
                remaining = size
                # Checked before every chunk: once the worker needs this
                # download's space for the next one (Jobs._make_room), the
                # stream stops short and the client sees a truncated body.
                while remaining > 0 and jobs.streaming(job_id):
                    chunk = handle.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            # The client went away mid-stream; the download is gone either way.
            pass
        finally:
            # Once, whatever happened: the audio is deleted, then the job.
            jobs.release(job_id)

    def do_POST(self) -> None:
        path = self._route_path()
        if path != JOBS_PATH:
            self._refuse_method()
            return
        # The exact path, with no query: a query is not part of this API, and
        # nothing in one is ever read.
        if self.path != JOBS_PATH:
            self.send_json_error(404, "not_found")
            return
        try:
            self._create_job()
        except GuardError as error:
            self.send_json_error(error.status, error.code, error.retry_after)

    # --- POST /fetch/api/jobs ---------------------------------------------------

    def _create_job(self) -> None:
        settings = self.server.settings
        headers = self.headers
        # In order; each step raises GuardError and nothing after it runs. No
        # body byte is read until all of them pass.
        require_origin(headers.get_all("Origin") or [], settings.public_origin)
        # application/json is not CORS-safelisted, so a cross-origin page
        # can't send it without a preflight this server never approves.
        require_content_type(headers.get_all("Content-Type") or [], JSON_TYPE)
        length = body_length(
            headers.get_all("Content-Length") or [], headers.get_all("Transfer-Encoding") or [], MAX_BODY_BYTES
        )
        client = client_address(
            self.client_address[0], headers.get_all("X-Forwarded-For") or [], settings.trusted_proxies
        )
        server = self.server
        allowed, retry_after = server.limiter.has_room(client)
        if not allowed:
            raise GuardError(429, "rate_limited", retry_after=retry_after)
        if server.jobs.is_full():
            raise GuardError(503, "busy", retry_after=BUSY_RETRY_SECONDS)
        video_id = parse_job_request(self._receive_body(length))
        # One lock around the second look at the limit, the enqueue and the
        # count, so parallel requests from one client can't all take the last
        # slot, and a job is counted only once it is really queued: a refusal
        # never uses up the client's budget.
        with server.submit_lock:
            allowed, retry_after = server.limiter.has_room(client)
            if not allowed:
                raise GuardError(429, "rate_limited", retry_after=retry_after)
            try:
                job_id = server.jobs.submit(video_id)
            except QueueFull:
                raise GuardError(503, "busy", retry_after=BUSY_RETRY_SECONDS) from None
            server.limiter.allow(client)
        self.send_json(202, {"job_id": job_id})

    def _receive_body(self, length: int) -> bytes:
        """Exactly length body bytes, within BODY_DEADLINE_SECONDS, or GuardError."""
        parts: list[bytes] = []
        remaining = length
        deadline = time.monotonic() + BODY_DEADLINE_SECONDS
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise GuardError(408, "body_timeout")
                wait = min(self.timeout, left)
                self.connection.settimeout(wait)
                try:
                    # At most one recv per call, never past the declared length.
                    chunk = self.rfile.read1(remaining)
                except TimeoutError:
                    if wait < self.timeout:
                        raise GuardError(408, "body_timeout") from None
                    raise GuardError(400, "incomplete_body") from None
                except OSError:
                    raise GuardError(400, "incomplete_body") from None
                if not chunk:
                    raise GuardError(400, "incomplete_body")
                parts.append(chunk)
                remaining -= len(chunk)
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(parts)


class FetchServer(http.server.ThreadingHTTPServer):
    # As the core's server: daemon workers, no join on close, no
    # SO_REUSEADDR on Windows (where it lets a second process take the port).
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = sys.platform != "win32"
    MAX_WORKERS = 16

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        runner: Runner | None = None,
        jobs_factory: Callable[[Path, Runner, int], Jobs] = Jobs,
    ) -> None:
        self.settings = settings
        # The image's yt-dlp, run by this interpreter, under prlimit on Linux.
        # Building it checks nothing on disk.
        self.runner: Runner = (
            runner
            if runner is not None
            else YtDlp(
                settings.fetch_max_seconds,
                settings.fetch_max_bytes,
                settings.fetch_timeout_seconds,
                prlimit=default_prlimit(),
                proxies=settings.proxies,
            )
        )
        self.limiter = RateLimiter(settings.rate_limit_fetches_per_minute)
        self.submit_lock = threading.Lock()
        self._workers = threading.BoundedSemaphore(self.MAX_WORKERS)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        # Binding first: an instance that already owns the port keeps its
        # scratch directory.
        super().__init__(address, Handler)
        try:
            prepare_scratch(settings.scratch_dir)
            self.jobs = jobs_factory(settings.scratch_dir, self.runner, settings.fetch_result_ttl_seconds)
            self.jobs.start(SWEEP_INTERVAL_SECONDS)
        except BaseException:
            self.server_close()
            raise

    def server_close(self) -> None:
        jobs = getattr(self, "jobs", None)
        if jobs is not None:
            jobs.stop()
        super().server_close()

    def server_bind(self) -> None:
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup
        # nobody needs.
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
            self._workers.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()

    def _refuse_busy(self, request: socket.socket) -> None:
        # Written by hand because no Handler exists for this connection.
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
            request.settimeout(1)
            request.sendall(data)
        except OSError:
            pass
        sys.stderr.write("- - 503 0.000\n")

    def handle_error(self, request: Any, client_address: Any) -> None:
        # One line, no traceback: a traceback can quote a header or a path.
        error = sys.exc_info()[1]
        sys.stderr.write(f"error while handling a request: {type(error).__name__}\n")
