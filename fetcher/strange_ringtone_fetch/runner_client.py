"""The fetcher's default Runner: a client for the runner container over `jobs` (docs/PLAN.md 6.1).

The fetcher no longer runs yt-dlp. It sends the runner only the validated
video ID and a token it mints itself, never its own job ID (a runner that
learned one could collect the browser's download from the fetcher's port
8080 on `jobs`), and treats everything that comes back as untrusted: the
status, the headers, the JSON's size and shape, the audio's size, type and
length, and how long each read and each exchange takes. The metadata is
re-validated with the same whitelist the runner applied
(strange_ringtone_fetch.metadata.parse_info). Anything off fails the job
closed, with nothing left in the job directory and no retry. No proxy
variable is consulted (http.client reads none) and no redirect is followed
(http.client follows none, so a 3xx is just "not 200").

Each exchange has two bounds: the socket timeout of every recv, and a hard
deadline timer that shuts the socket down when it fires. The timer is what
bounds the reads http.client makes inside getresponse() (a 100 Continue loop,
a status or header line that never ends), which the socket timeout alone
can't, because each of their recvs returns in time.

An exchange ends when the runner closes the connection, not when the
declared length has been read. The runner closes it only once its handler
has returned, after it freed its one-job lock (and, after a stream, deleted
the audio); a client that went on at the last byte could send its next
request, the audio GET or the next job's POST, while the lock was still
held, and the runner would answer 503 busy. The body and that wait are read
through http.client's own buffered reader, which is never left to close
itself at the declared length: bytes past it are seen even when they came in
the same write as the body, and fail the fetch, as does a runner that never
closes.

No magic-byte or codec check on the audio: the browser decodes by content
and refuses what it can't, the page receives the bytes only as a no-store
attachment with nosniff, and a check on the first bytes would not make a
malicious MP4 safer. A decoder exploit is the browser's boundary, as it is
for a malicious YouTube format.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import secrets
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from strange_ringtone_fetch.metadata import ERROR_CODES, VIDEO_ID_RE, Download, FetchError, Metadata, parse_info

CONNECT_TIMEOUT_SECONDS = 10.0
# Socket timeout for each recv of a response head or body.
READ_TIMEOUT_SECONDS = 30.0
# On top of FETCH_TIMEOUT_SECONDS for the POST's total deadline. The runner's
# worst case before it answers is FETCH_TIMEOUT_SECONDS, its DRAIN_SECONDS (5)
# after a timeout kill, and two bounded reaps of REAP_SECONDS (2) each, one
# at each run boundary of its sweep (strange_ringtone_runner.ytdlp): 9 s past
# the timeout, so this leaves a margin of 6 s. PostGraceTest in
# tests/test_fetcher_runner_client_hardening.py keeps at least 3 s.
POST_GRACE_SECONDS = 15.0
# The audio GET's total deadline is this plus one second per MiB of the cap
# (70 s with the default 40 MiB).
AUDIO_DEADLINE_BASE_SECONDS = 30.0
# The runner's answer holds three whitelisted fields; a real one is a few kB.
JSON_MAX_BYTES = 256 * 1024
MIB = 1024 * 1024
CHUNK_BYTES = 65536
JSON_TYPE = "application/json"
AUDIO_TYPE = "audio/mp4"
JOBS_PATH = "/jobs"
# The token the client mints (secrets.token_hex(16)), the runner's JOB_RE.
JOB_RE = re.compile(r"[0-9a-f]{32}")
# What a Content-Length must be, exactly: int() alone would also take a sign,
# surrounding whitespace, underscores and non-ASCII digits, and http.client
# itself parses the header with int().
_LENGTH_RE = re.compile(r"[0-9]{1,20}")
# The audio file is the client's own: created here, never through a link, and
# never opened if anything is already at its name (the job directory is
# fresh). O_NOFOLLOW and O_BINARY exist only where they mean something.
_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # A repeated key could be read one way here and another way by whoever
    # wrote it ("failed" then "done"), so it is refused rather than resolved.
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate key")
        data[key] = value
    return data


class _Exchange:
    """One connection to the runner, as cancel() and the deadline timer see it.

    Both only ever shut the socket down; the thread that owns the connection
    closes it. Closing it from another thread would free its file descriptor
    while the owner may still be about to read from that number, which could
    by then belong to another connection in this process. A shut-down socket
    wakes a blocked read with EOF or an error, which is all that is needed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self.aborted = False

    def attach(self, sock: socket.socket) -> None:
        """Hand over the connected socket; FetchError if the exchange was aborted meanwhile."""
        with self._lock:
            self._sock = sock
            aborted = self.aborted
        if aborted:
            # The deadline or cancel() fired while connect() ran, when there
            # was no socket to shut down yet.
            _shut_down(sock)
            raise FetchError("failed")

    def abort(self) -> None:
        """Shut the socket down (now, or as soon as it exists), so a blocked read returns."""
        with self._lock:
            self.aborted = True
            sock = self._sock
        if sock is not None:
            _shut_down(sock)

    def settimeout(self, seconds: float) -> None:
        # Through the socket this exchange kept: http.client drops its own
        # reference (conn.sock) once a Connection: close answer has begun.
        with self._lock:
            sock = self._sock
        if sock is not None:
            sock.settimeout(seconds)

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()


def _shut_down(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class RunnerClient:
    """One fetch at a time against the runner at runner_url, every answer checked before use."""

    runner_url: str
    host: str
    port: int
    max_bytes: int
    timeout_seconds: int

    def __init__(
        self,
        runner_url: str,
        max_bytes: int,
        timeout_seconds: int,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = READ_TIMEOUT_SECONDS,
        post_grace: float = POST_GRACE_SECONDS,
        audio_deadline: float | None = None,
    ) -> None:
        """Parse runner_url (http://host[:port] only) and check the limits; ValueError otherwise.

        Touches no socket and no file, so the fetcher starts without a runner
        and resolves nothing at startup.
        """
        # Settings validated FETCH_RUNNER_URL already (check_http_url); these
        # checks keep a caller that bypasses Settings from pointing the client
        # at anything but one plain-HTTP host and port.
        parts = urlsplit(runner_url)
        port = parts.port  # ValueError for a non-numeric or out-of-range port
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError("runner_url must be http://host[:port]")
        if "@" in parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("runner_url must be http://host[:port] and nothing else")
        if max_bytes < 1 or timeout_seconds < 1:
            raise ValueError("max_bytes and timeout_seconds must be at least 1")
        if connect_timeout <= 0 or read_timeout <= 0 or post_grace < 0:
            raise ValueError("timeouts must be positive")
        if audio_deadline is not None and audio_deadline <= 0:
            raise ValueError("audio_deadline must be positive")
        self.runner_url = runner_url
        self.host = parts.hostname
        self.port = port if port is not None else 80
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._post_grace = post_grace
        self._audio_deadline = audio_deadline
        # The exchanges in flight, for cancel(), and how often cancel() was
        # called: a fetch that started before a cancel() opens no new
        # connection after it, so a cancel() that falls between the POST and
        # the GET still ends the fetch.
        self._lock = threading.Lock()
        self._exchanges: set[_Exchange] = set()
        self._cancels = 0

    @property
    def post_deadline(self) -> float:
        """The POST's total deadline: timeout_seconds + post_grace."""
        # The runner answers only once yt-dlp is done, which its own
        # FETCH_TIMEOUT_SECONDS bounds; the grace covers its drain, its two
        # reaps and a margin (POST_GRACE_SECONDS).
        return self.timeout_seconds + self._post_grace

    @property
    def audio_deadline(self) -> float:
        """The audio GET's total deadline: the argument, or AUDIO_DEADLINE_BASE_SECONDS + max_bytes / MIB."""
        if self._audio_deadline is not None:
            return self._audio_deadline
        return AUDIO_DEADLINE_BASE_SECONDS + self.max_bytes / MIB

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        """Have the runner fetch video_id and collect the audio into job_dir, or raise FetchError.

        ValueError for an ID that does not fullmatch VIDEO_ID_RE, before any
        request. The audio file is created by this client itself
        (O_CREAT|O_EXCL|O_NOFOLLOW, 0o600) and removed again on any failure.
        """
        # Checked again, as Jobs does: this value goes on the wire, and the
        # runner builds yt-dlp's URL from it.
        if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("not a video ID")
        # A token of the client's own, never the fetcher's job ID: the runner
        # can reach the fetcher's port 8080 over `jobs`, where a job ID is
        # all it takes to collect the browser's download.
        job = secrets.token_hex(16)
        with self._lock:
            generation = self._cancels
        try:
            metadata = self._post(video_id, job, generation)
            audio = self._collect(job, job_dir / f"{video_id}.m4a", generation)
        except FetchError:
            raise
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            # A refused connection, a timeout, the deadline's or cancel()'s
            # shutdown, anything http.client could not parse: the job fails
            # closed, with no detail that could carry a path or a title.
            raise FetchError("failed") from None
        return Download(audio=audio, metadata=metadata)

    def cancel(self) -> None:
        """Shut down every open connection, so a blocked fetch fails; Jobs.stop() calls it."""
        with self._lock:
            self._cancels += 1
            exchanges = list(self._exchanges)
        for exchange in exchanges:
            exchange.abort()

    # --- the two exchanges --------------------------------------------------------

    def _post(self, video_id: str, job: str, generation: int) -> Metadata:
        """POST /jobs and return the re-validated metadata, or raise FetchError with the runner's code."""
        body = json.dumps({"video_id": video_id, "job": job}).encode("ascii")
        # The status line arrives only when yt-dlp is done, so the head's
        # per-recv timeout is the whole deadline; the timer is the backstop.
        with self._exchange(self.post_deadline, self.post_deadline, generation) as (conn, exchange):
            # Nothing browser-facing: no Origin, no cookie, no forwarded
            # address. http.client adds Host, Accept-Encoding: identity and
            # Content-Length.
            conn.request("POST", JOBS_PATH, body=body, headers={"Content-Type": JSON_TYPE, "Connection": "close"})
            response = conn.getresponse()
            try:
                length = self._check_head(response, exchange, JSON_TYPE, JSON_MAX_BYTES)
                data = bytearray()
                self._read_exactly(response, exchange, length, data.extend)
                self._expect_close(response, exchange)
            finally:
                response.close()
        return self._parse_answer(bytes(data))

    def _collect(self, job: str, path: Path, generation: int) -> Path:
        """GET the audio of job into path, a file this client creates, or raise."""
        written = False
        try:
            with self._exchange(self.audio_deadline, self._read_timeout, generation) as (conn, exchange):
                # The path is built from the client's own token: no value the
                # runner sent ever enters a URL.
                conn.request("GET", f"{JOBS_PATH}/{job}/audio", headers={"Connection": "close"})
                response = conn.getresponse()
                try:
                    length = self._check_head(response, exchange, AUDIO_TYPE, self.max_bytes)
                    self._write_audio(response, exchange, length, path)
                    written = True
                    self._expect_close(response, exchange)
                finally:
                    response.close()
        except BaseException:
            # A complete file whose exchange still failed (the deadline or
            # cancel() shut it down after the last byte): the fetch fails,
            # so the file this client made goes too. _write_audio removes a
            # partial one itself.
            if written:
                with contextlib.suppress(OSError):
                    path.unlink()
            raise
        return path

    @contextlib.contextmanager
    def _exchange(
        self, deadline: float, head_timeout: float, generation: int
    ) -> Iterator[tuple[http.client.HTTPConnection, _Exchange]]:
        """A fresh connection under a hard deadline, registered for cancel() while it is open."""
        exchange = _Exchange()
        with self._lock:
            if self._cancels != generation:
                # cancel() ran since this fetch began: open nothing more.
                raise FetchError("failed")
            self._exchanges.add(exchange)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self._connect_timeout)
        timer = threading.Timer(deadline, exchange.abort)
        timer.daemon = True
        timer.start()
        try:
            # Neither the deadline nor cancel() can cut the name lookup or the
            # TCP connect inside connect(): there is no socket to shut down
            # yet. Both are bounded all the same, each connect attempt by
            # CONNECT_TIMEOUT_SECONDS and the lookup by the resolver's own
            # timeouts, and an exchange whose deadline passed meanwhile fails
            # at attach(). The runner can hold an attempt open no longer than
            # the connect timeout, and it can't touch the lookup: Docker's
            # embedded DNS answers the name in FETCH_RUNNER_URL.
            conn.connect()
            sock = conn.sock
            if not isinstance(sock, socket.socket):
                raise FetchError("failed")
            exchange.attach(sock)
            exchange.settimeout(head_timeout)
            yield conn, exchange
            # Anything the timer cut short has already failed a check; this
            # makes sure a shutdown that raced a complete read fails too.
            if exchange.aborted:
                raise FetchError("failed")
        finally:
            timer.cancel()
            with self._lock:
                self._exchanges.discard(exchange)
            conn.close()
            exchange.close()

    # --- checks -----------------------------------------------------------------------

    def _check_head(self, response: http.client.HTTPResponse, exchange: _Exchange, content_type: str,
                    limit: int) -> int:
        """The declared body length, if the head is exactly what the runner sends; FetchError otherwise."""
        if exchange.aborted:
            # The timer shut the socket down while the head was read: what
            # http.client made of a cut-off head is not an answer.
            raise FetchError("failed")
        # 200 only: a 3xx is not followed, and a 503 busy is not retried
        # (under the fetcher's one worker the runner is idle between jobs, so
        # busy means something is wrong, and failing closed is right).
        if response.status != 200:
            raise FetchError("failed")
        headers = response.headers
        # Exactly one, exactly this string: a parameter or a second value is
        # an answer the runner never writes.
        if headers.get_all("Content-Type") != [content_type]:
            raise FetchError("failed")
        # http.client would decode chunks itself, with no length to cap them.
        if headers.get_all("Transfer-Encoding"):
            raise FetchError("failed")
        lengths = headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not _LENGTH_RE.fullmatch(lengths[0]):
            raise FetchError("failed")
        length = int(lengths[0])
        if not 1 <= length <= limit:
            raise FetchError("failed")
        # The head is in; from here each recv of the body has the short timeout.
        exchange.settimeout(self._read_timeout)
        return length

    def _read_exactly(self, response: http.client.HTTPResponse, exchange: _Exchange, length: int,
                      sink: Any) -> None:
        """Pass exactly length body bytes to sink; FetchError on an early end.

        Read from the response's buffered reader itself: HTTPResponse.read()
        closes it once the declared length is in, and with it whatever it had
        already buffered past that length, which _expect_close must still see.
        """
        fp = response.fp
        if fp is None:
            raise FetchError("failed")
        remaining = length
        while remaining:
            chunk = fp.read(min(CHUNK_BYTES, remaining))
            if not chunk or exchange.aborted:
                raise FetchError("failed")
            sink(chunk)
            remaining -= len(chunk)

    def _expect_close(self, response: http.client.HTTPResponse, exchange: _Exchange) -> None:
        """Return at the runner's close, right after the declared length; FetchError on anything else."""
        fp = response.fp
        if fp is None:
            raise FetchError("failed")
        # One more read from the same buffer as the body: a byte here is past
        # the declared length, an answer the runner never writes, even if it
        # arrived with the body. EOF is the runner's close, unless the
        # deadline's or cancel()'s shutdown made it. The socket's read timeout
        # and the deadline bound the wait, as for every read of the body.
        if fp.read(1) or exchange.aborted:
            raise FetchError("failed")

    def _write_audio(self, response: http.client.HTTPResponse, exchange: _Exchange, length: int,
                     path: Path) -> None:
        # FileExistsError (anything at the name, a symlink included) is an
        # OSError: the fetch fails, and what was there is left alone, because
        # this client did not make it.
        fd = os.open(path, _CREATE_FLAGS, 0o600)
        complete = False
        try:
            try:
                self._read_exactly(response, exchange, length, lambda chunk: _write_all(fd, chunk))
                # The size on disk is what was declared and read, or the file is not the answer.
                if os.fstat(fd).st_size != length:
                    raise FetchError("failed")
            finally:
                # Closed before anything else happens, so the unlink below
                # works on Windows too.
                os.close(fd)
            complete = True
        finally:
            if not complete:
                with contextlib.suppress(OSError):
                    path.unlink()

    def _parse_answer(self, body: bytes) -> Metadata:
        """The runner's JSON answer: done with info, or failed with a known code; FetchError otherwise."""
        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError: deep nesting, which a 256 KiB body can reach.
            raise FetchError("failed") from None
        if not isinstance(data, dict):
            raise FetchError("failed")
        status = data.get("status")
        if status == "failed" and set(data) == {"status", "error"}:
            error = data["error"]
            # A known code passes through as the job's error; any other value
            # is "failed", never a code of the runner's choosing.
            if isinstance(error, str) and error in ERROR_CODES:
                raise FetchError(error)
            raise FetchError("failed")
        if status == "done" and set(data) == {"status", "info"} and isinstance(data["info"], dict):
            # The same whitelist the runner applied, applied again: nothing
            # in info is used except through parse_info.
            return parse_info(data["info"])
        raise FetchError("failed")


def _write_all(fd: int, data: bytes) -> None:
    # os.write may write less than asked (ENOSPC raises OSError, which fails the job).
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]
