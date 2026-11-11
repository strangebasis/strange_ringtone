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

import ctypes
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

from strange_ringtone_runner.config import SCRATCH_DIR, Settings
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


# Opened only to be listed or entered: never through a symlink, and
# O_NONBLOCK, so a FIFO swapped in for a directory can't make the open wait
# for a writer (O_DIRECTORY refuses it first where it exists).
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
# Linux (the container) works relative to open directory descriptors;
# Windows, a development platform only, has none and works by path.
_BY_DESCRIPTOR = (
    {os.open, os.stat, os.unlink, os.rmdir, os.chmod} <= os.supports_dir_fd and os.listdir in os.supports_fd
)
# How many times one directory is listed and emptied before whatever is
# still in it counts as left behind. Each listing reads the whole directory,
# so one pass removes everything a finished run left; another only matters
# if something still adds entries, which the sweep rules out.
_LISTINGS = 3


def _plain_directory(info: os.stat_result) -> bool:
    # A directory itself, never a link to one; on Windows a junction or other
    # reparse point counts as a link.
    return stat.S_ISDIR(info.st_mode) and not getattr(info, "st_reparse_tag", 0)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _chmod_no_follow(path: Path, mode: int) -> None:
    if sys.platform == "win32":
        # Development only: chmod there only sets or clears the read-only
        # flag, and the caller has just seen a plain directory at the path.
        os.chmod(path, mode)
        return
    os.chmod(path, mode, follow_symlinks=False)


class _DescriptorTree:
    """remove_leftovers' steps relative to an open directory (Linux, the container).

    A name is resolved in the directory already open, never through a link
    swapped in above it, and no path grows past PATH_MAX however deep a run
    nested its directories. One descriptor is open at a time, so depth costs
    no open files.
    """

    def open_root(self, root: Path) -> Any:
        return os.open(root, _DIR_FLAGS)

    def open(self, handle: Any, name: str) -> Any:
        return os.open(name, _DIR_FLAGS, dir_fd=handle)

    def open_parent(self, handle: Any) -> Any:
        return os.open("..", _DIR_FLAGS, dir_fd=handle)

    def stat(self, handle: Any) -> os.stat_result:
        return os.fstat(handle)

    def lstat(self, handle: Any, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=handle, follow_symlinks=False)

    def listdir(self, handle: Any) -> list[str]:
        return os.listdir(handle)

    def unlink(self, handle: Any, name: str) -> None:
        os.unlink(name, dir_fd=handle)

    def rmdir(self, handle: Any, name: str) -> None:
        os.rmdir(name, dir_fd=handle)

    def chmod_self(self, handle: Any, mode: int) -> None:
        os.fchmod(handle, mode)

    def chmod(self, handle: Any, name: str, mode: int) -> None:
        # fchmodat with AT_SYMLINK_NOFOLLOW: a link swapped in is never
        # followed (NotImplementedError where the platform can't promise it).
        os.chmod(name, mode, dir_fd=handle, follow_symlinks=False)

    def close(self, handle: Any) -> None:
        os.close(handle)


class _PathTree(_DescriptorTree):
    """The same steps by path, for Windows, which has no directory descriptors (development only)."""

    def open_root(self, root: Path) -> Any:
        return str(root)

    def open(self, handle: Any, name: str) -> Any:
        return os.path.join(handle, name)

    def open_parent(self, handle: Any) -> Any:
        return os.path.dirname(handle)

    def stat(self, handle: Any) -> os.stat_result:
        return os.lstat(handle)

    def lstat(self, handle: Any, name: str) -> os.stat_result:
        return os.lstat(os.path.join(handle, name))

    def unlink(self, handle: Any, name: str) -> None:
        # Windows' unlink removes a link to a directory as a link, too.
        os.unlink(os.path.join(handle, name))

    def rmdir(self, handle: Any, name: str) -> None:
        os.rmdir(os.path.join(handle, name))

    def chmod_self(self, handle: Any, mode: int) -> None:
        os.chmod(handle, mode)

    def chmod(self, handle: Any, name: str, mode: int) -> None:
        # Only ever called for an entry lstat saw as no link.
        os.chmod(os.path.join(handle, name), mode)

    def close(self, handle: Any) -> None:
        pass


class _Frame:
    """One directory on remove_leftovers' way down, and what is left to do in it."""

    __slots__ = ("identity", "name", "keep", "remove", "pending", "skip", "listings")

    def __init__(self, identity: tuple[int, int], name: str, keep: tuple[str, ...] | None, remove: bool) -> None:
        self.identity = identity
        # Its name in its parent, and whether it goes once it is empty: not
        # the root, and not a directory on the way to keep.
        self.name = name
        self.remove = remove
        # The rest of the kept path below it, when keep lies below it.
        self.keep = keep
        self.pending: list[str] = []
        # Names kept, or that could not be removed, which a new listing skips.
        self.skip: set[str] = set()
        self.listings = 0


def _make_usable(tree: _DescriptorTree, handle: Any, info: os.stat_result) -> None:
    # The owner's read, write and search back on a directory of this user, so
    # its entries can be listed, looked at and removed. Another user's (/tmp
    # itself) is left as it is.
    getuid = getattr(os, "getuid", None)
    mode = stat.S_IMODE(info.st_mode)
    if getuid is not None and info.st_uid == getuid() and mode & 0o700 != 0o700:
        tree.chmod_self(handle, mode | 0o700)


def _enter(tree: _DescriptorTree, handle: Any, name: str, entry: os.stat_result) -> Any:
    """The directory name in handle, opened and made usable; None when it can't be."""
    try:
        child = tree.open(handle, name)
    except PermissionError:
        # A run took the owner's permissions away: given back, never through
        # a link, and tried once more.
        try:
            tree.chmod(handle, name, stat.S_IMODE(entry.st_mode) | 0o700)
            child = tree.open(handle, name)
        except (OSError, NotImplementedError):
            return None
    except OSError:
        return None
    try:
        info = tree.stat(child)
        # The directory the listing showed, not one swapped in since.
        if _plain_directory(info) and _identity(info) == _identity(entry):
            _make_usable(tree, child, info)
            return child
    except OSError:
        pass
    tree.close(child)
    return None


def _remove(tree: _DescriptorTree, handle: Any, name: str, entry: os.stat_result | None) -> bool:
    """Remove name from handle, an emptied directory when entry is None; whether it is gone."""
    remove = tree.rmdir if entry is None else tree.unlink
    for attempt in range(2):
        try:
            remove(handle, name)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt:
                return False
        except OSError:
            return False
        # The owner's permissions back on the directory it is in and on the
        # entry itself (on Windows a read-only file can't be deleted), never
        # on a link; then once more.
        try:
            _make_usable(tree, handle, tree.stat(handle))
            if entry is None or not _is_link(entry):
                tree.chmod(handle, name, 0o700 if entry is None else stat.S_IMODE(entry.st_mode) | 0o700)
        except (OSError, NotImplementedError):
            pass
    return False


def remove_leftovers(root: Path, keep: Path | None = None) -> bool:
    """Remove everything inside root but keep and the directories on the way to it; whether nothing else is left.

    root itself stays. keep is kept whole as a plain directory, or kept as a
    regular file, and only below plain directories: a link on the way to it,
    or anywhere else, is removed as a link and nothing it points to is
    touched. A root that is no plain directory (a link a run put in its
    place) is left alone: False. An entry a run made unreadable or
    unwritable gets the owner's permissions back, never through a link and
    never on root's parent, which lies outside. Nothing but a directory is
    ever opened, so a FIFO can't block it; another filesystem mounted
    inside is never entered; it works iteratively, so neither a deep tree
    nor a large directory defeats it.
    """
    try:
        info = os.lstat(root)
    except OSError:
        return False
    if not _plain_directory(info):
        return False
    path_to_keep: tuple[str, ...] | None = None
    if keep is not None:
        try:
            path_to_keep = Path(keep).relative_to(root).parts
        except ValueError:
            path_to_keep = None
        if path_to_keep == ():
            return True
    tree = _DescriptorTree() if _BY_DESCRIPTOR else _PathTree()
    try:
        handle = tree.open_root(root)
    except PermissionError:
        try:
            _chmod_no_follow(root, stat.S_IMODE(info.st_mode) | 0o700)
            handle = tree.open_root(root)
        except (OSError, NotImplementedError):
            return False
    except OSError:
        return False
    device = info.st_dev
    clean = True
    try:
        opened = tree.stat(handle)
        if not _plain_directory(opened) or _identity(opened) != _identity(info):
            return False
        _make_usable(tree, handle, opened)
        stack = [_Frame(_identity(info), "", path_to_keep, remove=False)]
        while stack:
            frame = stack[-1]
            if not frame.pending:
                try:
                    left = [name for name in tree.listdir(handle) if name not in frame.skip]
                except OSError:
                    left, clean = [], False
                if left and frame.listings < _LISTINGS:
                    frame.listings += 1
                    frame.pending = left
                    continue
                if left:
                    clean = False
                # Done with this directory: back up to its parent, checked to
                # be the one it came from, and remove it there.
                stack.pop()
                if not stack:
                    break
                parent = tree.open_parent(handle)
                try:
                    moved = _identity(tree.stat(parent)) != stack[-1].identity
                finally:
                    tree.close(handle)
                    handle = parent
                if moved:
                    # Something moved the tree during the clean-up: stop
                    # rather than remove anything from a directory not seen
                    # on the way down.
                    return False
                if frame.remove and not _remove(tree, handle, frame.name, None):
                    stack[-1].skip.add(frame.name)
                    clean = False
                continue
            name = frame.pending.pop()
            try:
                entry = tree.lstat(handle, name)
            except FileNotFoundError:
                continue
            except OSError:
                frame.skip.add(name)
                clean = False
                continue
            if entry.st_dev != device:
                # A mount inside the tree: another filesystem is never
                # entered or removed.
                frame.skip.add(name)
                clean = False
                continue
            on_the_way = frame.keep is not None and name == frame.keep[0]
            directory = _plain_directory(entry)
            if on_the_way and len(frame.keep) == 1 and (directory or stat.S_ISREG(entry.st_mode)):
                frame.skip.add(name)
                continue
            if directory:
                child = _enter(tree, handle, name, entry)
                if child is None:
                    frame.skip.add(name)
                    clean = False
                    continue
                tree.close(handle)
                handle = child
                if on_the_way:
                    frame.skip.add(name)
                below = frame.keep[1:] if on_the_way else None
                stack.append(_Frame(_identity(entry), name, below, remove=not on_the_way))
                continue
            # A file, a link, a FIFO, a socket or a device node: removed as
            # itself, never opened or followed.
            if not _remove(tree, handle, name, entry):
                frame.skip.add(name)
                clean = False
    except OSError:
        return False
    finally:
        tree.close(handle)
    return clean


def restore_scratch(directory: Path) -> None:
    """Make directory and its results/ plain directories of this user, mode 0o700, again; RuntimeError when it can't.

    The check prepare_scratch makes at start-up, after every run: a run has
    this UID and may have put a file, a FIFO or a link in place of either,
    or changed its mode. Whatever is there instead is removed as itself,
    never followed, and the directory made again; a held result in
    results/ stays.
    """
    for path in (directory, directory / RESULTS_DIR):
        info: os.stat_result | None
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        except OSError as error:
            raise RuntimeError(f"cannot check {path}") from error
        if info is not None and not _plain_directory(info):
            try:
                os.unlink(path)
            except OSError as error:
                raise RuntimeError(f"cannot remove what is in place of {path}") from error
            info = None
        if info is None:
            try:
                # No parents: if the directory above is gone too, making it
                # is not this check's job (prepare_scratch).
                path.mkdir(mode=0o700)
                info = os.lstat(path)
            except OSError as error:
                raise RuntimeError(f"cannot make {path} again") from error
            if not _plain_directory(info):
                raise RuntimeError(f"{path} is not a plain directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError(f"{path} is owned by another user")
        try:
            # Owner-only, as at start-up: only this user lists, enters or
            # writes it.
            _chmod_no_follow(path, 0o700)
        except (OSError, NotImplementedError) as error:
            raise RuntimeError(f"cannot set the mode of {path}") from error


def may_clean_all_of_tmp(scratch_dir: Path, sweep_strays: bool) -> bool:
    """Whether the clean-up after a run may also remove everything else in the scratch directory's parent, /tmp.

    Only under the container's contract: on Linux, as PID 1 of its PID
    namespace, so /tmp is the container's own tmpfs; sweeping, so nothing of
    a run is still running to write there; and with exactly the contract's
    scratch directory, /tmp/strange_ringtone_runner. Anywhere else, a
    developer's machine and the tests above all, /tmp may hold other
    programs' files, and nothing outside the scratch directory is touched.
    """
    return _under_the_contract(scratch_dir, sweep_strays)


def may_remove_ipc_objects(scratch_dir: Path, sweep_strays: bool) -> bool:
    """Whether the clean-up after a run may also remove the System V IPC objects of the runner's IPC namespace.

    Under the same contract as may_clean_all_of_tmp, whose `ipc: none` gives
    the container an IPC namespace of its own, and whose sweep leaves
    nothing of a run running to use an object. Anywhere else the namespace
    may be a developer's, shared with other programs of this user, and
    nothing in it is touched.
    """
    return _under_the_contract(scratch_dir, sweep_strays)


def _under_the_contract(scratch_dir: Path, sweep_strays: bool) -> bool:
    return (
        sys.platform.startswith("linux")
        and os.getpid() == 1
        and sweep_strays is True
        and scratch_dir == SCRATCH_DIR
    )


# Where Linux lists the System V IPC objects of the caller's IPC namespace,
# one file per kind, and the command that removes an object (IPC_RMID, 0 on
# every Linux architecture).
SYSV_IPC = Path("/proc/sysvipc")
_IPC_RMID = 0


def _ipc_ids(listing: Path) -> list[int]:
    """The objects a /proc/sysvipc listing names: the second column of every line after the first."""
    try:
        lines = listing.read_text(encoding="ascii", errors="replace").splitlines()[1:]
    except OSError:
        return []
    return [int(fields[1]) for fields in (line.split() for line in lines) if len(fields) > 1 and fields[1].isdigit()]


def remove_ipc_objects(listing: Path = SYSV_IPC) -> None:
    """Remove every System V shared memory segment, message queue and semaphore set this user may remove.

    A run has the runner's UID and can leave such objects: neither a file
    nor a process, they outlive it, and a segment's pages stay charged to
    the container's memory limit until it is removed, enough to make every
    later run fail for want of memory. Only under the container's contract
    (may_remove_ipc_objects). The kernel refuses another user's object,
    which stays; one already gone is no error.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    for ident in _ipc_ids(listing / "shm"):
        libc.shmctl(ident, _IPC_RMID, None)
    for ident in _ipc_ids(listing / "msg"):
        libc.msgctl(ident, _IPC_RMID, None)
    for ident in _ipc_ids(listing / "sem"):
        # The second argument, a semaphore's number, means nothing to IPC_RMID.
        libc.semctl(ident, 0, _IPC_RMID)


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
        # Decided once, here: whether the clean-up after each run may clear
        # the rest of /tmp too, which only the container's contract allows.
        self._clean_all_of_tmp = may_clean_all_of_tmp(settings.scratch_dir, sweep_strays)
        # And whether it may empty the IPC namespace, under the same contract.
        self._remove_ipc_objects = may_remove_ipc_objects(settings.scratch_dir, sweep_strays)
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
        """One run under the caller's hold of busy: the answer's payload. Nothing the run left survives it.

        Only the job's own result, when the run produced one, is still in
        the scratch directory once this returns.
        """
        results = self.results
        # Before yt-dlp writes anything: no uncollected result exists while a
        # download arrives, so the tmpfs holds one audio file at most (R3).
        results.discard_all()
        if self._closing.is_set():
            return {"status": "failed", "error": "failed"}
        # Owner-only (mkdtemp's 0o700), fresh, with a server-made name.
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.settings.scratch_dir))
        held: str | None = None
        try:
            try:
                download = self.ytdlp.fetch(video_id, job_dir)
                results.hold(job, download.audio)
                held = job
            except FetchError as error:
                payload: dict[str, Any] = {"status": "failed", "error": error.code}
            except Exception:
                # Anything else is "failed" with nothing of its text: an
                # exception could quote a title or a path.
                payload = {"status": "failed", "error": "failed"}
            else:
                # The whitelist on the runner's side: only the three cleaned
                # values, in yt-dlp's names, ever leave this container.
                payload = {"status": "done", "info": metadata_to_info(download.metadata)}
        finally:
            # Every path, success included (the audio was moved out above),
            # after the run and its sweep and before the answer, so no other
            # job starts next to what this run left: the job directory and
            # everything else the run could write.
            usable = self._clean_up(held)
        if not usable:
            # Fail closed: a result is only ever served from a scratch
            # directory that is the runner's again.
            results.discard_all()
            return {"status": "failed", "error": "failed"}
        return payload

    def _clean_up(self, job: str | None) -> bool:
        """Remove everything a run left; whether the scratch directory is usable and holds job's result, if any.

        A run has the runner's UID and could write anywhere in its /tmp: a
        file where results/ should be, a directory it took the permissions
        off, a full tmpfs. All of it in the scratch directory goes, except
        the result the runner holds for this job, and under the container's
        contract the rest of /tmp and the run's System V IPC objects too, so
        no later job fails because of it.
        """
        scratch = self.settings.scratch_dir
        keep = scratch / RESULTS_DIR / f"{job}.m4a" if job is not None else None
        try:
            remove_leftovers(scratch, keep=keep)
        except Exception:
            # What is left is retried after the next run; whether the
            # directory is usable is decided below.
            pass
        if self._clean_all_of_tmp:
            # The rest of the container's own tmpfs, only under the contract
            # (may_clean_all_of_tmp): anything of the run's outside the
            # scratch directory, which stays whole. Before the scratch
            # directory is made again, so a tmpfs the run filled has room.
            try:
                remove_leftovers(scratch.parent, keep=scratch)
            except Exception:
                pass
        if self._remove_ipc_objects:
            # What the run left that is no file: its System V IPC objects,
            # which would keep their memory charged to this container.
            try:
                remove_ipc_objects()
            except Exception:
                pass
        try:
            restore_scratch(scratch)
            usable = True
        except Exception:
            usable = False
        if not usable:
            # One line, with no token, ID or title in it. The runner keeps
            # serving and /healthz stays as it is; docs/PLAN.md 6.1 says what
            # that leaves.
            sys.stderr.write("after a run the scratch directory could not be made usable again: the job failed\n")
            return False
        if keep is not None:
            # A run that put a link in place of the scratch directory or of
            # results/ had the result moved through it, away from where it
            # is served from; the clean-up removed the link. Only a result
            # that is where it should be counts.
            try:
                return stat.S_ISREG(os.lstat(keep).st_mode)
            except OSError:
                return False
        return True

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
