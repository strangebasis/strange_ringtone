"""The runner's HTTP server: one yt-dlp job at a time for the fetcher, over `jobs` (docs/PLAN.md 6.1).

Plain http.server, one request per connection, like the fetcher's. The
wire protocol is three routes and nothing else:

- GET /healthz: 200 "ok", revealing nothing, never waiting for a job;
- POST /jobs with {"video_id", "job"}: the run itself, synchronously, under
  one lock; the answer is {"status": "done", "info": {...}} with only the
  whitelisted metadata (strange_ringtone_fetch.metadata) or
  {"status": "failed", "error": code};
- GET /jobs/<job>/audio: the audio, streamed once and deleted.

A second POST, or an audio GET, while a run or a stream holds the lock gets
503 busy. At most one audio file exists on the runner's tmpfs at any time:
a new job discards any uncollected result before yt-dlp starts, and an
uncollected one is deleted COLLECT_DEADLINE_SECONDS after its run. The log
holds method, path with the token masked, status and duration only: never
a video ID, a title, a token, an address or yt-dlp's output.
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
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# The core's one implementation of the Content-Type and length rules, and the
# shared metadata whitelist (docs/PLAN.md 6.1): reused, so the runner refuses
# exactly what the fetcher refuses and answers exactly what it would keep.
from strange_ringtone.http_guard import GuardError, body_length, require_content_type
from strange_ringtone_fetch.metadata import VIDEO_ID_RE, FetchError, metadata_to_info

from strange_ringtone_runner.config import Settings
from strange_ringtone_runner.ytdlp import YtDlp, default_prlimit

SERVER_NAME = "strange_ringtone_runner"
HEALTH_PATH = "/healthz"
JOBS_PATH = "/jobs"
# A job token: 128 random bits as 32 lowercase hex characters, minted by the
# fetcher. The exact shape is part of the route, so a path trick is simply
# not a job route.
JOB_RE = re.compile(r"[0-9a-f]{32}")
AUDIO_PATH_RE = re.compile(r"/jobs/([0-9a-f]{32})/audio")
JSON_TYPE = "application/json"
AUDIO_TYPE = "audio/mp4"
# {"video_id": "<11>", "job": "<32>"} is about 60 bytes; 1 KiB leaves room
# for whitespace and nothing that could cost anything to parse.
MAX_BODY_BYTES = 1024
BUSY_RETRY_SECONDS = 5
# An uncollected audio file is deleted this long after its run, so a fetcher
# that never comes back can't keep the tmpfs full.
COLLECT_DEADLINE_SECONDS = 60.0
SWEEP_INTERVAL_SECONDS = 5.0
CHUNK_BYTES = 65536
RESULTS_DIR = "results"
NO_STORE = "no-store"
_LOG_PATH_MAX = 200
_LOG_METHOD_MAX = 16
# A token, or anything shaped like one, whatever its case: the token is all it
# takes to collect a result, so no log line hands one to whoever reads the logs.
_LOG_ID_RE = re.compile(r"[0-9A-Fa-f]{32,}")


def parse_job_request(body: bytes) -> tuple[str, str]:
    """(video_id, job) from a POST body, or GuardError 400 with a code.

    Only a UTF-8 JSON object with exactly the keys video_id and job, both
    strings, no key repeated, is a request (bad_json otherwise); the ID must
    fullmatch VIDEO_ID_RE (bad_video_id) and the token JOB_RE (bad_job). The
    ID is checked again here, although the fetcher checked it, because it
    goes into the URL yt-dlp is given.
    """
    try:
        text = body.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: deep nesting, which a 1 KiB body can't reach in
        # CPython 3.12's decoder but another build's might.
        raise GuardError(400, "bad_json") from None
    if not isinstance(data, dict) or set(data) != {"video_id", "job"}:
        raise GuardError(400, "bad_json")
    video_id, job = data["video_id"], data["job"]
    # A value that is not a string is not a request at all (SPEC 3.4).
    if not isinstance(video_id, str) or not isinstance(job, str):
        raise GuardError(400, "bad_json")
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise GuardError(400, "bad_video_id")
    # The token becomes a file name and part of a route, so its shape is
    # exact: lowercase hex only, no '/', '.', or other script's digits.
    if not JOB_RE.fullmatch(job):
        raise GuardError(400, "bad_job")
    return video_id, job


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # A repeated key could be read differently by the fetcher and this
    # server, so it is refused rather than resolved.
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate key")
        data[key] = value
    return data


# _printable and _log_path duplicate strange_ringtone_fetch.server's (the
# runner may not import the fetcher's server); only the mask's name differs.
def _printable(text: str, limit: int) -> str:
    # Log lines are plain ASCII: a client can't inject a newline, an escape
    # sequence or a fake second entry.
    cleaned = "".join(ch if 0x21 <= ord(ch) <= 0x7E else "_" for ch in text[:limit])
    return cleaned or "-"


def _log_path(raw: str) -> str:
    # The query and fragment are dropped: nothing in them is part of the API.
    path = raw.split("?", 1)[0].split("#", 1)[0]
    path = _LOG_ID_RE.sub("<job>", path)
    return _printable(path, _LOG_PATH_MAX)


def prepare_scratch(directory: Path) -> None:
    """Create the scratch directory owner-only and empty it, as the fetcher's server does.

    The runner keeps no state across restarts, and a directory planted by
    another user, or a symlink, is refused rather than used.
    """
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
    # Safe because 0o700 lets only this user list, enter or write the download directory (execute makes it enterable).
    os.chmod(directory, 0o700)  # noqa: E501  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    # The runner keeps no state across restarts: an audio file a crashed
    # runner left in results/ is deleted, never served to its token's holder.
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _unlink(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            # Only something planted there (a child the run left behind, same
            # UID) puts a directory at a result's name; it goes all the same,
            # so nothing outlives its entry.
            shutil.rmtree(path, ignore_errors=True)
            return
        path.unlink(missing_ok=True)
    except OSError:
        # Windows only (development): a file still open elsewhere can't be
        # deleted. The entry is forgotten either way, so it is never served.
        pass


class Results:
    """The audio of finished runs, waiting to be collected once; at most one at any time.

    Each entry is results/<job>.m4a. hold() discards every unclaimed entry
    before it moves the new audio in, claim() hands an entry out once and
    atomically, and sweep() deletes unclaimed entries COLLECT_DEADLINE_SECONDS
    after their run, so the tmpfs never holds more than one download.
    """

    def __init__(
        self,
        directory: Path,
        clock: Callable[[], float] = time.monotonic,
        deadline_seconds: float = COLLECT_DEADLINE_SECONDS,
    ) -> None:
        self.directory = directory
        self.clock = clock
        self.deadline_seconds = deadline_seconds
        self._lock = threading.Lock()
        # Unclaimed entries and the time each was held; claimed ones are
        # being streamed and are deleted by release().
        self._held: dict[str, float] = {}
        self._claimed: set[str] = set()
        self._closed = False
        # Owner-only, inside the owner-only scratch directory that
        # prepare_scratch has just emptied. The mode can only lose bits to the
        # umask, never gain them.
        directory.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise RuntimeError(f"results path is not a plain directory: {directory}")

    def _path(self, job: str) -> Path:
        return self.directory / f"{job}.m4a"

    def _discard_held(self) -> None:
        # Caller holds self._lock.
        for job in list(self._held):
            del self._held[job]
            _unlink(self._path(job))

    def hold(self, job: str, source: Path) -> None:
        """discard_all(), then move source (a regular file, never followed) to directory/<job>.m4a; record clock()."""
        # The token becomes a file name: its exact shape keeps it inside the
        # results directory, whoever calls this.
        if not isinstance(job, str) or not JOB_RE.fullmatch(job):
            raise ValueError("not a job token")
        with self._lock:
            # After server_close(), a run that finished in the meantime must
            # not leave a file behind that nothing will ever delete.
            if self._closed:
                raise RuntimeError("the runner is stopping")
            if job in self._claimed:
                raise RuntimeError("that token's audio is being collected")
            # Only a regular file is ever held: a FIFO, a symlink or a
            # directory at the source is not audio, and a FIFO in results/
            # would make the stream's open wait for a writer. The stream
            # checks again when it opens the file (_stream).
            if not stat.S_ISREG(os.lstat(source).st_mode):
                raise ValueError("not a regular file")
            # The rule "at most one audio file" is kept here too, not only by
            # the server discarding before each run.
            self._discard_held()
            os.replace(source, self._path(job))
            self._held[job] = self.clock()

    def claim(self, job: str) -> Path | None:
        """The entry's path, once; None when it is unknown, already claimed or expired."""
        if not isinstance(job, str) or not JOB_RE.fullmatch(job):
            return None
        with self._lock:
            held_at = self._held.pop(job, None)
            if held_at is None:
                return None
            if self.clock() - held_at >= self.deadline_seconds:
                # Past the deadline but not yet swept: gone, not handed out.
                _unlink(self._path(job))
                return None
            self._claimed.add(job)
            return self._path(job)

    def release(self, job: str) -> None:
        """Delete and forget a claimed entry, whether or not its stream completed."""
        with self._lock:
            if job not in self._claimed:
                return
            self._claimed.discard(job)
            _unlink(self._path(job))

    def discard_all(self) -> None:
        """Delete and forget every unclaimed entry."""
        with self._lock:
            self._discard_held()

    def close(self) -> None:
        """discard_all(), and refuse every later hold(); for server_close()."""
        with self._lock:
            self._closed = True
            self._discard_held()

    def sweep(self) -> int:
        """Delete unclaimed entries older than the deadline; how many went."""
        with self._lock:
            now = self.clock()
            expired = [job for job, held_at in self._held.items() if now - held_at >= self.deadline_seconds]
            for job in expired:
                del self._held[job]
                _unlink(self._path(job))
            return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._held) + len(self._claimed)


class Handler(http.server.BaseHTTPRequestHandler):
    """The three routes of the wire protocol, each answer closing its connection and marked no-store."""

    server: RunnerServer

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
        # paths included. The runner serves no page, so of the browser-facing
        # headers only nosniff applies: nothing it answers is ever sniffed.
        self.send_header("X-Content-Type-Options", "nosniff")
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
        """Status line and headers. Nothing the runner answers may be cached."""
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
        # ensure_ascii (the default): a title's non-ASCII characters travel
        # as \u escapes, so the body is plain ASCII.
        self.send_body(status, json.dumps(payload).encode("ascii"), JSON_TYPE, extra_headers)

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
        # Method, path without query and with tokens masked, status, duration;
        # never a client address, a header, the body (which holds the video
        # ID), a title or anything yt-dlp printed.
        sys.stderr.write((format % args) + "\n")

    def handle_one_request(self) -> None:
        """Route one request: /healthz, POST /jobs, GET /jobs/<job>/audio, or 404/405."""
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
        # HTTP/0.9 responses could not carry the headers above.
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
        # The path exactly as sent: nothing is percent-decoded or normalised,
        # and a target the stdlib rewrote (split on other whitespace, leading
        # slashes collapsed) matches no route, as in the fetcher's server. No
        # route takes a query or a fragment, so a target with either is no
        # route at all.
        if not self.path.startswith("/") or self.path.startswith("//"):
            return None
        fields = self.requestline.split(" ")
        if len(fields) != 3 or fields[1] != self.path:
            return None
        if "?" in self.path or "#" in self.path:
            return None
        return self.path

    def _allowed_methods(self, path: str | None) -> str | None:
        if path is None:
            return None
        if path == HEALTH_PATH or AUDIO_PATH_RE.fullmatch(path):
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
            # Says only that the server answers: no version, job or settings,
            # and never waits for the lock a run holds.
            self.send_body(200, b"ok", "text/plain; charset=utf-8")
            return
        match = AUDIO_PATH_RE.fullmatch(path) if path is not None else None
        if match is not None:
            self._send_audio(match[1])
            return
        self._refuse_method()

    def do_POST(self) -> None:
        if self._route_path() != JOBS_PATH:
            self._refuse_method()
            return
        try:
            self._run_job()
        except GuardError as error:
            self.send_json_error(error.status, error.code, error.retry_after)

    # --- GET /jobs/<job>/audio ----------------------------------------------------

    def _send_audio(self, job: str) -> None:
        server = self.server
        # The lock first: while a run or another stream holds it, nothing is
        # claimed, so a busy answer never costs the holder its audio.
        if not server.busy.acquire(blocking=False):
            self.send_json_error(503, "busy", BUSY_RETRY_SECONDS)
            return
        try:
            path = server.results.claim(job)
            if path is None:
                # Unknown, expired and collected tokens look the same.
                self.send_json_error(404, "not_found")
                return
            try:
                self._stream(path)
            finally:
                # Once, whatever happened: the audio is deleted after its
                # handle is closed (Windows can't delete an open file).
                server.results.release(job)
        finally:
            # After release(): no run starts, and so no second audio file
            # appears, until this one is gone (R3).
            server.busy.release()

    def _stream(self, path: Path) -> None:
        # O_NONBLOCK: a FIFO planted at the path (by a yt-dlp child that
        # outlived its run, say) would otherwise block this open until a
        # writer appears, holding busy for good while /healthz still answers.
        # Non-blocking, it opens at once and the S_ISREG check below refuses
        # it; a regular file ignores the flag.
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        )
        try:
            # Never through a symlink, and only a regular file within the
            # cap: whatever yt-dlp left behind, the runner serves its own
            # audio file and nothing it points to.
            fd = os.open(path, flags)
        except OSError:
            self.send_json_error(404, "not_found")
            return
        # Checked on the descriptor before it becomes a file object: open()
        # refuses a directory's descriptor with IsADirectoryError and leaves
        # it open, and the refusal must still be an answer.
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            self.send_json_error(404, "not_found")
            return
        size = info.st_size
        if not stat.S_ISREG(info.st_mode) or not 1 <= size <= self.server.settings.fetch_max_bytes:
            os.close(fd)
            self.send_json_error(404, "not_found")
            return
        try:
            with open(fd, "rb") as handle:
                self.send_head(200, size, AUDIO_TYPE)
                remaining = size
                while remaining > 0:
                    chunk = handle.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    # Under the handler's 30 s socket timeout: a fetcher
                    # that stops reading can't hold the lock for longer per
                    # chunk.
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            # The fetcher went away mid-stream; the audio is gone either way.
            pass

    # --- POST /jobs ---------------------------------------------------------------

    def _run_job(self) -> None:
        headers = self.headers
        # In order; each step raises GuardError and nothing after it runs. No
        # body byte is read until the head passes.
        require_content_type(headers.get_all("Content-Type") or [], JSON_TYPE)
        length = body_length(
            headers.get_all("Content-Length") or [], headers.get_all("Transfer-Encoding") or [], MAX_BODY_BYTES
        )
        video_id, job = parse_job_request(self._receive_body(length))
        server = self.server
        # One job at a time (R1): a second request is refused, never queued,
        # so the runner holds one run's files at most.
        if not server.busy.acquire(blocking=False):
            raise GuardError(503, "busy", retry_after=BUSY_RETRY_SECONDS)
        try:
            payload = server._run(video_id, job)
            self.send_json(200, payload)
        finally:
            # After the answer is written, so no other run can start before
            # this one's files are settled; before the connection is closed,
            # and the fetcher's client waits for that close before its GET for
            # the audio, so the GET never finds the lock held.
            server.busy.release()

    def _receive_body(self, length: int) -> bytes:
        """Exactly length body bytes, under the handler's socket timeout, or GuardError 400."""
        try:
            body = self.rfile.read(length)
        except OSError:
            # TimeoutError included: a body that stops arriving is incomplete.
            raise GuardError(400, "incomplete_body") from None
        if len(body) != length:
            raise GuardError(400, "incomplete_body")
        return body


class RunnerServer(http.server.ThreadingHTTPServer):
    # As the fetcher's server: daemon workers, no join on close, no
    # SO_REUSEADDR on Windows (where it lets a second process take the port).
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = sys.platform != "win32"
    # One job runs at a time; the other workers answer /healthz and 503 busy.
    MAX_WORKERS = 4

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        ytdlp: YtDlp | None = None,
        clock: Callable[[], float] = time.monotonic,
        sweep_strays: bool = False,
    ) -> None:
        """Bind, then prepare the scratch directory, the results and the sweeper.

        The default ytdlp is the image's yt-dlp under prlimit with the
        settings' limits and proxies; building it touches no file.
        sweep_strays (off by default) gives it kill-and-reap at both run
        boundaries (strange_ringtone_runner.ytdlp.kill_and_reap); only the
        entry turns it on, and only as PID 1.
        """
        # Refused before binding: a caller that asks for the sweep must not
        # get a run that silently skips it.
        if sweep_strays and ytdlp is not None and not ytdlp.sweep_strays:
            raise ValueError("sweep_strays needs a ytdlp that sweeps")
        self.settings = settings
        # The image's yt-dlp from its venv, under prlimit on Linux, with the
        # operator's proxies as the only part of this environment its child
        # sees (R2).
        self.ytdlp = (
            ytdlp
            if ytdlp is not None
            else YtDlp(
                settings.fetch_max_seconds,
                settings.fetch_max_bytes,
                settings.fetch_timeout_seconds,
                prlimit=default_prlimit(),
                proxies=settings.proxies,
                sweep_strays=sweep_strays,
            )
        )
        # Held by a run or a stream: the one-job-at-a-time rule (R1, R3).
        self.busy = threading.Lock()
        self._workers = threading.BoundedSemaphore(self.MAX_WORKERS)
        self._closing = threading.Event()
        self._sweeper_stop = threading.Event()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        # Binding first: an instance that already owns the port keeps its
        # scratch directory.
        super().__init__(address, Handler)
        try:
            prepare_scratch(settings.scratch_dir)
            self.results = Results(settings.scratch_dir / RESULTS_DIR, clock)
            # The interval is read now, from the module, so a test can set it.
            self._sweeper = threading.Thread(
                target=self._sweep_forever, args=(SWEEP_INTERVAL_SECONDS,), name="runner-result-sweeper", daemon=True
            )
            self._sweeper.start()
        except BaseException:
            self.server_close()
            raise

    def _run(self, video_id: str, job: str) -> dict[str, Any]:
        """One run under the caller's hold of busy: the answer's payload. The job directory never survives it."""
        results = self.results
        # Before yt-dlp writes anything: no uncollected result exists while a
        # download arrives, so the tmpfs holds one audio file at most (R3).
        results.discard_all()
        if self._closing.is_set():
            return {"status": "failed", "error": "failed"}
        # Owner-only (mkdtemp's 0o700), fresh, with a server-made name.
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.settings.scratch_dir))
        try:
            try:
                download = self.ytdlp.fetch(video_id, job_dir)
                results.hold(job, download.audio)
            except FetchError as error:
                return {"status": "failed", "error": error.code}
            except Exception:
                # Anything else is "failed" with nothing of its text: an
                # exception could quote a title or a path.
                return {"status": "failed", "error": "failed"}
            # The whitelist on the runner's side: only the three cleaned
            # values, in yt-dlp's names, ever leave this container.
            return {"status": "done", "info": metadata_to_info(download.metadata)}
        finally:
            # Every path, success included: the audio was moved out above.
            # ignore_errors covers Windows development only, where a killed
            # run's children may still hold the directory for a moment; on
            # Linux killpg ends the whole group first, and in the container
            # the sweep ends anything that left it.
            shutil.rmtree(job_dir, ignore_errors=True)

    def _sweep_forever(self, interval: float) -> None:
        while not self._sweeper_stop.wait(interval):
            try:
                self.results.sweep()
            except Exception:
                # One failed sweep must not end the deadline for every later
                # result; the next tick tries again.
                pass

    def server_close(self) -> None:
        """Stop the sweeper, kill a running yt-dlp, delete every result, then close the socket."""
        # getattr throughout: a start that failed after binding calls this on
        # a half-built server.
        closing = getattr(self, "_closing", None)
        if closing is not None:
            closing.set()
        stop = getattr(self, "_sweeper_stop", None)
        if stop is not None:
            stop.set()
        ytdlp = getattr(self, "ytdlp", None)
        if ytdlp is not None:
            ytdlp.cancel()
        results = getattr(self, "results", None)
        if results is not None:
            results.close()
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
        lines = [
            "HTTP/1.0 503 Service Unavailable",
            f"Server: {SERVER_NAME}",
            "X-Content-Type-Options: nosniff",
            "Connection: close",
            f"Content-Type: {JSON_TYPE}",
            f"Content-Length: {len(body)}",
            f"Cache-Control: {NO_STORE}",
            # A worker slot frees as soon as a short request ends.
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
