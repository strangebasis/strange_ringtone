"""Run yt-dlp for one validated video ID and read back only what the runner keeps (docs/PLAN.md 6.1).

This module runs only in the runner's container (strange_ringtone_runner),
and yt-dlp, which it starts, is the only code in the project that talks to
the internet; the fetcher never runs it. So every run is fenced in like the
core's ffmpeg (strange_ringtone.render):

- an argument list, never a shell. The only value from a request in it is the
  video ID, checked against VIDEO_ID_RE before it gets here, inside a URL this
  module builds itself; no URL a user typed ever reaches yt-dlp;
- the util-linux prlimit binary in front of it on Linux (CPU seconds, file
  size, open files, processes). No address-space limit: Deno, which yt-dlp
  runs for YouTube's player JavaScript, reserves a huge virtual range at
  startup and would die under RLIMIT_AS. The deployment's cgroup limit bounds
  memory instead;
- its own session, so a wall-clock timeout kills the whole process group,
  Deno included, and so does the end of every run;
- in the runner's container, where the runner is PID 1, a sweep at both run
  boundaries: kill(-1, SIGKILL), which reaches every other process of the
  runner's UID, a child that left the group with setsid() included, then a
  bounded waitpid(-1) that reaps what the kill and the run left behind, so
  nothing of one run reaches the next one's files or stays a zombie that
  counts against pids_limit (docs/PLAN.md 6.1). Opt-in (YtDlp's
  sweep_strays), and only where sweep_is_safe_here() says so: anywhere else
  kill(-1) would kill every process of the user;
- an environment built from scratch: the child sees fixed values and the
  proxy settings the runner's Settings read, never the server's own
  environment;
- a job directory the caller created, holding only names yt-dlp derives from
  the validated ID.

yt-dlp's stdout and stderr never leave this module and are never logged:
they carry the destination filename and the title. stderr is only matched
against a few fixed phrases to choose an error code. The metadata whitelist
and the result types live in strange_ringtone_fetch.metadata, shared with the
fetcher, which applies the same whitelist again to whatever the runner
answers. tests/test_fetcher_ytdlp.py pins each guard.
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# ERROR_CODES and Metadata are re-exported, not used here: the tests that
# moved with the run code (tests/test_fetcher_ytdlp.py) read them through this
# module with their bodies unchanged.
from strange_ringtone_fetch.metadata import ERROR_CODES as ERROR_CODES
from strange_ringtone_fetch.metadata import VIDEO_ID_RE, Download, FetchError, parse_info
from strange_ringtone_fetch.metadata import Metadata as Metadata

# Fixed paths, never taken from a setting or a request: what the image ships
# (fetcher/Dockerfile).
PRLIMIT = "/usr/bin/prlimit"
DENO = "/usr/local/bin/deno"
# The venv the image builds for yt-dlp and yt-dlp-ejs (fetcher/Dockerfile).
# Not sys.executable: the server's interpreter must never hold a third-party
# wheel, whose .pth lines, scripts and data files would be code in the server
# (docs/PLAN.md 6.1). -I keeps PYTHON* variables, the user site and the
# working directory out of the child too.
YTDLP_PYTHON = "/opt/yt-dlp/bin/python"
DEFAULT_COMMAND: tuple[str, ...] = (YTDLP_PYTHON, "-I", "-m", "yt_dlp")

WATCH_URL = "https://www.youtube.com/watch?v="

# yt-dlp's exit status when a --break-match-filters filter rejects the video
# (RejectedVideoReached, a DownloadCancelled): too long, or live.
EXIT_REJECTED = 101
# Phrases yt-dlp prints when YouTube refuses this client. Matched without
# regard to case, and only to pick an error code.
_BLOCKED_RE = re.compile(
    r"sign in to confirm|not a bot|captcha|po token|http error 403|this content isn.t available", re.IGNORECASE
)
_NOT_FOUND_RE = re.compile(r"video unavailable|private video|has been removed|does not exist", re.IGNORECASE)

# How long to wait for the pipes after a timeout kill (see render.py).
DRAIN_SECONDS = 5.0
SOCKET_TIMEOUT_SECONDS = 20
# A real info JSON is a few hundred kB; this bounds what json.load reads.
INFO_MAX_BYTES = 4 * 1024 * 1024
_CHILD_PATH = "/usr/local/bin:/usr/bin:/bin"
# How long a sweep waits for the processes it killed to exit so it can reap
# them; one still dying after that is reaped at the next boundary.
REAP_SECONDS = 2.0
_REAP_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class Limits:
    """Per-run resource limits, applied by prlimit before yt-dlp starts."""

    # CPU time, as a backstop to the wall-clock timeout.
    cpu_seconds: int = 120
    # The audio may be at most FETCH_MAX_BYTES; the margin covers the info
    # JSON and Deno's cache. A larger write kills the run with SIGXFSZ.
    file_size_bytes: int = 40 * 1024 * 1024 + 1024 * 1024
    # yt-dlp, Deno and V8 open a few dozen files between them.
    open_files: int = 256
    # RLIMIT_NPROC counts every thread of UID 10001 in the container: the
    # server's workers, yt-dlp, and Deno's V8 threads.
    processes: int = 512


def default_prlimit() -> tuple[str, ...] | None:
    """prlimit on Linux, always; nothing where it doesn't exist (Windows development).

    Not conditional on the file being there: on Linux a missing prlimit fails
    every fetch instead of silently running yt-dlp without limits.
    """
    return (PRLIMIT,) if sys.platform.startswith("linux") else None


def sweep_is_safe_here() -> bool:
    """Whether this process may sweep: PID 1 of its PID namespace, on a platform with kill, SIGKILL and waitpid.

    The runner's entry asks this once at startup. PID 1 means the runner is
    its container's first process (compose.example.yaml runs it with no init
    and no wrapper), so kill(-1) stays inside the container. On a developer
    machine the runner is never PID 1, and kill(-1) there would kill every
    process of the user.
    """
    return (
        callable(getattr(os, "kill", None))
        and callable(getattr(os, "waitpid", None))
        and hasattr(os, "WNOHANG")
        and hasattr(signal, "SIGKILL")
        and os.getpid() == 1
    )


def kill_and_reap(reap_seconds: float = REAP_SECONDS) -> int:
    """SIGKILL every other process this one may signal, then reap its exited children; how many were reaped.

    Only as PID 1 (RuntimeError otherwise, before any signal): the kernel
    sends kill(-1) to every process the caller may signal in its PID
    namespace except PID 1 and the caller, under one lock that fork() can't
    slip past, so a child that left yt-dlp's group with setsid(), or one
    that keeps forking to a new PID, is caught too. Everything such a
    process leaves becomes a child of PID 1 when its parent exits, and PID 1
    must reap it, or each one stays a zombie that counts against pids_limit
    for good. The caller must have no subprocess.Popen in flight: waitpid(-1)
    would steal that child's exit status, which subprocess then reports as 0.
    """
    # Checked here, at the one place that sends the signal, as well as by the
    # entry: a flag set by mistake must never reach kill(-1) outside PID 1.
    if os.getpid() != 1:
        raise RuntimeError("only PID 1 of its own PID namespace may sweep")
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        # Nothing to kill; earlier zombies may still wait to be reaped.
        pass
    reaped = 0
    deadline = time.monotonic() + reap_seconds
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            # No child left at all, dead or alive.
            return reaped
        if pid != 0:
            reaped += 1
            continue
        # Children exist but none has exited yet: killed processes take a
        # moment to die. Bounded, so a stuck one can't hold the run lock; the
        # next boundary reaps it.
        if time.monotonic() >= deadline:
            return reaped
        time.sleep(_REAP_POLL_SECONDS)


def watch_url(video_id: str) -> str:
    """The only URL yt-dlp is ever given, built from a validated ID."""
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("not a video ID")
    return WATCH_URL + video_id


def ytdlp_args(video_id: str, job_dir: Path, max_seconds: int, max_bytes: int) -> list[str]:
    """yt-dlp's arguments, after the program itself."""
    return [
        # No configuration file from anywhere: what runs is exactly this list.
        "--ignore-config",
        "--no-playlist",
        # Only the YouTube extractor, never the generic one, which would fetch
        # whatever a page pointed it to.
        "--use-extractors", "youtube",
        "--js-runtimes", f"deno:{DENO}",
        # yt-dlp's cache would sit in HOME, which is the job directory anyway;
        # nothing is meant to carry over from one job to the next.
        "--no-cache-dir",
        # AAC in MP4, which Safari decodes, so the fetcher needs no ffmpeg.
        "-f", "bestaudio[ext=m4a]",
        # A breaking filter, not --match-filters: a plain filter skips the
        # video and exits 0 having written nothing, while this one exits 101,
        # which is how too_long is told apart from other failures.
        "--break-match-filters", f"duration <= {max_seconds} & !is_live",
        "--max-filesize", str(max_bytes),
        "--write-info-json",
        "--quiet", "--no-warnings", "--no-progress",
        "--socket-timeout", str(SOCKET_TIMEOUT_SECONDS),
        "-P", str(job_dir),
        # Names derived from the ID only: the title never becomes a path.
        "-o", "%(id)s.%(ext)s",
        # Everything after -- is a URL, never an option.
        "--",
        watch_url(video_id),
    ]


def map_failure(returncode: int, stderr: bytes) -> str:
    """The error code for a run that exited with returncode and did not produce a result."""
    if returncode == EXIT_REJECTED:
        return "too_long"
    if returncode != 0:
        text = stderr.decode("utf-8", errors="replace")
        if _BLOCKED_RE.search(text):
            return "blocked"
        if _NOT_FOUND_RE.search(text):
            return "not_found"
    # Exit 0 without the audio is --max-filesize refusing a large file, which
    # yt-dlp does not treat as an error; anything else is failed too.
    return "failed"


def _open_regular(path: Path, max_bytes: int) -> tuple[int, int] | None:
    """(fd, size) for a regular file of 1..max_bytes at path, never through a symlink; or None."""
    # O_NONBLOCK: a FIFO swapped in between the lstat and the open would
    # otherwise block the open until a writer appears, and with it the run
    # and the runner's lock. Non-blocking, it opens at once and the fstat
    # check refuses it; a regular file ignores the flag.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        # lstat first for Windows, which has no O_NOFOLLOW.
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= max_bytes:
        os.close(fd)
        return None
    return fd, info.st_size


def read_result(job_dir: Path, video_id: str, max_bytes: int) -> Download:
    """The audio and metadata of a run that exited 0, or FetchError("failed").

    Success is exactly <id>.m4a and <id>.info.json in the job directory, both
    regular files within their size limits; exit 0 alone proves nothing.
    """
    audio = job_dir / f"{video_id}.m4a"
    opened = _open_regular(audio, max_bytes)
    if opened is None:
        raise FetchError("failed")
    os.close(opened[0])
    info_path = job_dir / f"{video_id}.info.json"
    opened = _open_regular(info_path, INFO_MAX_BYTES)
    if opened is None:
        raise FetchError("failed")
    fd, _size = opened
    try:
        # Closed by the with-block before anything deletes the directory,
        # because Windows can't delete an open file.
        with open(fd, "rb") as handle:
            info = json.load(handle)
    except (ValueError, RecursionError, OSError):
        raise FetchError("failed") from None
    return Download(audio=audio, metadata=parse_info(info))


class YtDlp:
    def __init__(
        self,
        max_seconds: int,
        max_bytes: int,
        timeout_seconds: int,
        command: tuple[str, ...] = DEFAULT_COMMAND,
        prlimit: tuple[str, ...] | None = None,
        limits: Limits | None = None,
        proxies: tuple[tuple[str, str], ...] = (),
        sweep_strays: bool = False,
    ) -> None:
        # Nothing here touches the filesystem, so a checkout on Windows starts
        # without yt-dlp or Deno and only a fetch fails. sweep_strays turns on
        # kill_and_reap at both run boundaries; off by default, so only the
        # runner's entry, as PID 1, ever turns it on (sweep_is_safe_here).
        if not command:
            raise ValueError("command must name a program")
        if max_seconds < 1 or max_bytes < 1 or timeout_seconds < 1:
            raise ValueError("max_seconds, max_bytes and timeout_seconds must be at least 1")
        self.command = tuple(command)
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.prlimit = tuple(prlimit) if prlimit is not None else None
        self.limits = (
            limits
            if limits is not None
            else Limits(cpu_seconds=timeout_seconds, file_size_bytes=max_bytes + 1024 * 1024)
        )
        self.proxies = tuple(proxies)
        self.sweep_strays = bool(sweep_strays)
        self._lock = threading.Lock()
        self._running: set[subprocess.Popen[bytes]] = set()
        # Held for a whole run while sweeping, so no sweep's waitpid(-1) runs
        # while another thread's Popen is in flight. The runner's own lock
        # already allows one run at a time; this keeps the rule in here too.
        self._run_lock = threading.Lock()

    def full_command(self, args: list[str]) -> list[str]:
        """prlimit (when configured), then the yt-dlp command, then args."""
        prefix: list[str] = []
        if self.prlimit is not None:
            limits = self.limits
            prefix = [
                *self.prlimit,
                f"--cpu={limits.cpu_seconds}",
                f"--fsize={limits.file_size_bytes}",
                f"--nofile={limits.open_files}",
                f"--nproc={limits.processes}",
                # Everything after -- is the program and its arguments.
                "--",
            ]
        return [*prefix, *self.command, *args]

    def child_env(self, job_dir: Path) -> dict[str, str]:
        """The child's whole environment: fixed values plus the operator's proxy settings.

        yt-dlp hands its own environment to Deno, so this is everything either
        of them sees. A token or other secret in the server's environment
        never reaches code that talks to the internet.
        """
        env = {
            "PATH": _CHILD_PATH,
            # Anything that insists on a home or cache directory writes inside
            # the job directory, which is removed after the run.
            "HOME": str(job_dir),
            "DENO_DIR": str(job_dir / "deno"),
            "DENO_NO_UPDATE_CHECK": "1",
            "NO_COLOR": "1",
            "LC_ALL": "C.UTF-8",
        }
        env.update(self.proxies)
        if sys.platform == "win32":
            # Development only: Python on Windows can't start without
            # SYSTEMROOT (it needs it to reach the system's random source).
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return env

    def fetch(self, video_id: str, job_dir: Path) -> Download:
        """Download video_id's audio into job_dir, or raise FetchError. The caller removes job_dir."""
        if not self.sweep_strays:
            return self._fetch(video_id, job_dir)
        with self._run_lock:
            return self._fetch(video_id, job_dir)

    def _sweep(self) -> bool:
        """kill_and_reap when sweeping is on; False when the sweep could not run."""
        if not self.sweep_strays:
            return True
        try:
            kill_and_reap()
        except (OSError, RuntimeError):
            return False
        return True

    def _fetch(self, video_id: str, job_dir: Path) -> Download:
        command = self.full_command(ytdlp_args(video_id, job_dir, self.max_seconds, self.max_bytes))
        # The boundary before the run: nothing a past run (or anything else of
        # this UID) left running may watch this job's directory or results/
        # while yt-dlp writes. No Popen is in flight here. A sweep that can't
        # run fails the job rather than starting yt-dlp next to a stray.
        if not self._sweep():
            raise FetchError("failed")
        try:
            # Safe by construction: an argument list with no shell, a program
            # fixed in code (DEFAULT_COMMAND or PRLIMIT), arguments that are
            # constants, validated numbers, a server-created directory and a
            # URL built from an ID that matched VIDEO_ID_RE, an environment
            # built from scratch, and a wall-clock timeout below.
            # tests/test_fetcher_ytdlp.py pins each of these guards.
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job_dir,
                env=self.child_env(job_dir),
                # Its own session and process group, so a timeout can kill
                # everything yt-dlp started, Deno included.
                start_new_session=True,
            )
        except OSError:
            # Missing interpreter, missing prlimit, or no resources to start it.
            raise FetchError("failed") from None
        with self._lock:
            self._running.add(proc)
        try:
            try:
                # stdout is read only to keep the pipe from filling; neither
                # stream is kept or logged.
                _stdout, stderr = proc.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    # Drain, not wait(): the pipes must be read to EOF to free
                    # them, and the bound keeps a survivor from hanging us.
                    proc.communicate(timeout=DRAIN_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                finally:
                    _close_pipes(proc)
                raise FetchError("timeout") from None
            except BaseException:
                try:
                    _kill_group(proc)
                    proc.wait()
                finally:
                    _close_pipes(proc)
                raise
            # A run that ended by itself, success included, may have left a
            # child behind that closed its pipes (so communicate() returned):
            # it would keep the runner's reach between jobs, could edit the
            # next job's directory or results/ (same UID), and would count
            # against pids_limit. The group is killed before the result is
            # read, so nothing of the run can touch the files read_result
            # checks. killpg on the reaped leader's PID still reaches the
            # group's other members: Linux keeps a PID that is a live
            # group's ID from being reused, and the next run starts only
            # after this one, under the server's lock. The two paths above
            # that raise have killed it already, so every run's group is
            # killed exactly once. A child that called setsid() has left the
            # group; the sweep below reaches it.
            _kill_group(proc)
        finally:
            with self._lock:
                self._running.discard(proc)
            # The boundary after the run, on every path, before read_result
            # and so before the runner answers: kill(-1) reaches what left
            # the group, and the reap clears the zombies of everything the
            # kills ended. Only once Popen has reaped its own child
            # (returncode set), so waitpid(-1) never steals yt-dlp's status;
            # a run whose wait itself failed is swept at the next boundary.
            swept = not self.sweep_strays or (proc.returncode is not None and self._sweep())
        if not swept:
            # A stray may still be running next to the files read_result
            # would check: the result can't be trusted.
            raise FetchError("failed")
        if proc.returncode != 0:
            raise FetchError(map_failure(proc.returncode, stderr))
        return read_result(job_dir, video_id, self.max_bytes)

    def cancel(self) -> None:
        """Kill every running yt-dlp and what it started; used when the server stops."""
        with self._lock:
            running = list(self._running)
        for proc in running:
            _kill_group(proc)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            # start_new_session made the child the leader of a new group whose
            # ID is its PID. Using the PID, not getpgid(), still reaches the
            # group's other members when the leader has already exited.
            killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    # Windows has no process groups to signal; TerminateProcess on the child
    # is the best it offers.
    try:
        proc.kill()
    except OSError:
        pass


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for name in ("stdout", "stderr"):
        pipe = getattr(proc, name)
        if pipe is None or pipe.closed:
            continue
        # On Windows communicate() reads through helper threads that hold the
        # pipe's lock while blocked; such a thread closes its pipe itself at
        # EOF, so a live one is left alone (see strange_ringtone.render).
        reader = getattr(proc, f"{name}_thread", None)
        if reader is not None and reader.is_alive():
            continue
        try:
            pipe.close()
        except OSError:
            pass
