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
variable is consulted and no redirect is followed.

No magic-byte or codec check on the audio: the browser decodes by content
and refuses what it can't, the page receives the bytes only as a no-store
attachment with nosniff, and a check on the first bytes would not make a
malicious MP4 safer. A decoder exploit is the browser's boundary, as it is
for a malicious YouTube format.
"""
from __future__ import annotations

import re
from pathlib import Path

# ERROR_CODES, VIDEO_ID_RE, FetchError and parse_info are what fetch()
# checks the runner's answer with (docs/PLAN.md 6.1).
from strange_ringtone_fetch.metadata import ERROR_CODES, VIDEO_ID_RE, Download, FetchError, parse_info  # noqa: F401

CONNECT_TIMEOUT_SECONDS = 10.0
# Socket timeout for each recv of a response head or body.
READ_TIMEOUT_SECONDS = 30.0
# On top of FETCH_TIMEOUT_SECONDS for the POST's total deadline: the runner's
# DRAIN_SECONDS (5) after a timeout kill, and a margin.
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
        raise NotImplementedError

    @property
    def post_deadline(self) -> float:
        """The POST's total deadline: timeout_seconds + post_grace."""
        raise NotImplementedError

    @property
    def audio_deadline(self) -> float:
        """The audio GET's total deadline: the argument, or AUDIO_DEADLINE_BASE_SECONDS + max_bytes / MIB."""
        raise NotImplementedError

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        """Have the runner fetch video_id and collect the audio into job_dir, or raise FetchError.

        ValueError for an ID that does not fullmatch VIDEO_ID_RE, before any
        request. The audio file is created by this client itself
        (O_CREAT|O_EXCL|O_NOFOLLOW, 0o600) and removed again on any failure.
        """
        raise NotImplementedError

    def cancel(self) -> None:
        """Shut down every open connection, so a blocked fetch fails; Jobs.stop() calls it."""
        raise NotImplementedError
