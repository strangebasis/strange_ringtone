"""Rendered tones waiting to be downloaded: the core's only state (docs/PLAN.md 5.5).

A result is one file under the scratch tmpfs, results/<id>/tone.m4a, where
<id> is 128 random bits. The index in memory is the authority: a directory on
disk that the index doesn't know is never served, and the tone's name lives
only in the index, never in a path or a file. Results expire after the TTL and
nothing is evicted early, so the tmpfs has a known upper bound
(MAX_RESULTS results at once). There is no database; a restart forgets all.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Exactly what secrets.token_hex(16) produces. Checked before any lookup, so a
# client-chosen string never gets near the index or the filesystem.
RESULT_ID_RE = re.compile(r"[0-9a-f]{32}")
TONE_FILE = "tone.m4a"
# The most read_tone reads from a file at once.
_READ_BYTES = 64 * 1024


class StoreFull(Exception):
    """MAX_RESULTS results already exist; the render answers 503 until the sweep frees room."""


@dataclass(frozen=True)
class Result:
    result_id: str
    name: str
    path: Path
    created: float


class ResultStore:
    def __init__(
        self,
        directory: Path,
        ttl_seconds: int,
        max_results: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 1 or max_results < 1:
            raise ValueError("ttl_seconds and max_results must be at least 1")
        self.directory = directory
        self.ttl_seconds = ttl_seconds
        self.max_results = max_results
        self._clock = clock
        self._lock = threading.Lock()
        self._results: dict[str, Result] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Created here, inside a scratch directory the server has just checked
        # and emptied. Owner-only, like scratch itself.
        directory.mkdir(mode=0o700, exist_ok=True)

    def __len__(self) -> int:
        with self._lock:
            return len(self._results)

    def is_full(self) -> bool:
        """Whether put() would refuse now, after expiring what is due.

        The server asks before it takes a render slot, so a full store costs
        no upload and no render.
        """
        self.sweep()
        with self._lock:
            return len(self._results) >= self.max_results

    def put(self, source: Path, name: str) -> Result:
        """Move source into the store under a fresh random ID."""
        self.sweep()
        with self._lock:
            if len(self._results) >= self.max_results:
                raise StoreFull()
            result_id = secrets.token_hex(16)
            # 128 random bits never repeat in practice; checked anyway, because
            # overwriting someone else's result would hand them this tone.
            while result_id in self._results:
                result_id = secrets.token_hex(16)
            folder = self.directory / result_id
            # Not exist_ok: a directory already at this name was not made by us.
            folder.mkdir(mode=0o700)
            path = folder / TONE_FILE
            try:
                # A rename within the scratch tmpfs: no copy, and the job
                # directory no longer holds the file when it is removed.
                os.replace(source, path)
            except OSError:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            result = Result(result_id=result_id, name=name, path=path, created=self._clock())
            self._results[result_id] = result
        return result

    def get(self, result_id: str) -> Result | None:
        """The live result with this ID, or None for a malformed, unknown or expired one."""
        if not isinstance(result_id, str) or not RESULT_ID_RE.fullmatch(result_id):
            return None
        with self._lock:
            result = self._results.get(result_id)
        if result is None or self._clock() - result.created >= self.ttl_seconds:
            return None
        return result

    def read_tone(self, result: Result, max_bytes: int) -> bytes | None:
        """The tone's bytes, or None unless it is a regular file of at most max_bytes.

        The results directory belongs to the server's UID, which every
        render's ffmpeg runs as, so a process a render left behind can
        replace a stored tone. It is opened without following a link (one
        to /proc/self/maps would serve the server's memory map, one to
        /dev/zero would read until the server runs out of memory) and
        without blocking (a FIFO would hold the handler thread), refused
        unless it is a regular file, and read to at most max_bytes and the
        one byte more that shows it grew past them. Read whole and closed
        at once: the sweeper may delete the file meanwhile, and Windows
        can't delete an open file.
        """
        path = result.path
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            if not nofollow and stat.S_ISLNK(os.lstat(path).st_mode):
                # Windows (development only) has no O_NOFOLLOW, so a link
                # is looked for first; no contained ffmpeg runs there.
                return None
            # O_BINARY (Windows only): the bytes exactly, never text mode.
            fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                return None
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(fd, min(_READ_BYTES, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except OSError:
            return None
        finally:
            os.close(fd)
        return b"".join(chunks) if total <= max_bytes else None

    def sweep(self) -> int:
        """Delete every result older than the TTL; return how many went."""
        now = self._clock()
        with self._lock:
            expired = [r for r in self._results.values() if now - r.created >= self.ttl_seconds]
        removed = 0
        for result in expired:
            # The files go first and the index entry only once they are gone.
            # A deletion that fails (Windows refuses while a download still
            # holds the file open) keeps its entry, so the next sweep retries it
            # and it keeps counting against MAX_RESULTS until the bytes are
            # really off the tmpfs. No request can be handed the path meanwhile:
            # get() refuses an expired entry.
            try:
                shutil.rmtree(self.directory / result.result_id)
            except FileNotFoundError:
                pass
            except OSError:
                continue
            with self._lock:
                self._results.pop(result.result_id, None)
            removed += 1
        return removed

    def clear(self) -> None:
        """Forget every result and leave the results directory existing and empty."""
        with self._lock:
            self._results.clear()
        # Recreated when missing: the server clears scratch, results directory
        # included, before it hands the store its first result.
        self.directory.mkdir(mode=0o700, exist_ok=True)
        for child in self.directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass

    def start_sweeper(self, interval_seconds: float) -> None:
        """Sweep every interval_seconds on a daemon thread until stop()."""
        if self._thread is not None:
            raise RuntimeError("the sweeper is already running")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._stop.clear()

        def loop() -> None:
            # Event.wait doubles as the sleep and the stop signal, so stop()
            # never waits a whole interval.
            while not self._stop.wait(interval_seconds):
                try:
                    self.sweep()
                except Exception:
                    # A failed sweep is retried on the next tick; the thread
                    # must not die and leave results to pile up.
                    pass

        # A daemon: it must never keep the process alive after SIGTERM.
        self._thread = threading.Thread(target=loop, name="result-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the sweeper thread, if one runs, and wait for it."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join()
