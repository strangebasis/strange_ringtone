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
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

# GuardError, body_length, require_content_type, VIDEO_ID_RE,
# metadata_to_info and default_prlimit are what the request checks and the
# default runner use (docs/PLAN.md 6.1): the core's one implementation of
# the Content-Type and length rules, and the shared metadata whitelist.
from strange_ringtone.http_guard import GuardError, body_length, require_content_type  # noqa: F401
from strange_ringtone_fetch.metadata import VIDEO_ID_RE, metadata_to_info  # noqa: F401

from strange_ringtone_runner.config import Settings
from strange_ringtone_runner.ytdlp import YtDlp, default_prlimit  # noqa: F401

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


def parse_job_request(body: bytes) -> tuple[str, str]:
    """(video_id, job) from a POST body, or GuardError 400 with a code.

    Only a UTF-8 JSON object with exactly the keys video_id and job, both
    strings, no key repeated, is a request (bad_json otherwise); the ID must
    fullmatch VIDEO_ID_RE (bad_video_id) and the token JOB_RE (bad_job). The
    ID is checked again here, although the fetcher checked it, because it
    goes into the URL yt-dlp is given.
    """
    raise NotImplementedError


def prepare_scratch(directory: Path) -> None:
    """Create the scratch directory owner-only and empty it, as the fetcher's server does.

    The runner keeps no state across restarts, and a directory planted by
    another user, or a symlink, is refused rather than used.
    """
    raise NotImplementedError


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
        raise NotImplementedError

    def hold(self, job: str, source: Path) -> None:
        """discard_all(), then move source to directory/<job>.m4a and record clock()."""
        raise NotImplementedError

    def claim(self, job: str) -> Path | None:
        """The entry's path, once; None when it is unknown, already claimed or expired."""
        raise NotImplementedError

    def release(self, job: str) -> None:
        """Delete and forget a claimed entry, whether or not its stream completed."""
        raise NotImplementedError

    def discard_all(self) -> None:
        """Delete and forget every unclaimed entry."""
        raise NotImplementedError

    def sweep(self) -> int:
        """Delete unclaimed entries older than the deadline; how many went."""
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError


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

    def handle_one_request(self) -> None:
        """Route one request: /healthz, POST /jobs, GET /jobs/<job>/audio, or 404/405."""
        raise NotImplementedError


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
    ) -> None:
        """Bind, then prepare the scratch directory, the results and the sweeper.

        The default ytdlp is the image's yt-dlp under prlimit with the
        settings' limits and proxies; building it touches no file.
        """
        raise NotImplementedError

    def server_close(self) -> None:
        """Stop the sweeper, kill a running yt-dlp, delete every result, then close the socket."""
        raise NotImplementedError
