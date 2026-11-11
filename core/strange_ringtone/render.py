"""Measure and render a validated WAV with the image's minimal ffmpeg (docs/PLAN.md 5.3).

ffmpeg is the largest attack surface in the core, so every run is fenced in:

- an argument list, never a shell, with a fixed program path; the only value
  from the client that reaches it is the sanitised tone name, as one argv
  element after -metadata;
- the util-linux prlimit binary in front of it on Linux (address space, CPU
  seconds, file size, open files). prlimit execs into ffmpeg, so the PID is
  unchanged. preexec_fn is not used: the stdlib documents it as unsafe in a
  threaded process, and the server is threaded;
- its own session, so on a wall-clock timeout the whole process group dies,
  not only the direct child, and at the end of every other run too, before
  anything of the run is used; where the server inherits orphans (as its
  container's PID 1), the members that kill killed are then collected;
- a job directory the server created, holding only server-chosen names;
- only a regular file is an output, never a link or a FIFO, and one larger
  than the file-size limit is refused, also where prlimit is not in force;
- both pipes read to their end, but only a bounded tail of stderr kept in
  memory and nothing of stdout.

The render pass measures its own output with a trailing ebur128, because the
image has no AAC decoder and so can't measure the encoded file afterwards.
"""
from __future__ import annotations

import ctypes
import functools
import io
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Fixed paths, never taken from a setting or a request: the binaries the image
# ships (core/Dockerfile).
FFMPEG = "/usr/local/bin/ffmpeg"
PRLIMIT = "/usr/bin/prlimit"

# -1.5 dBFS, not -1.0: Phase 0 measured AAC encoding adding inter-sample overs,
# and -1.5 kept every render at or below -1.3 dBTP (docs/PLAN.md 5.3).
LIMITER_CEILING_DB = -1.5
# alimiter's limit is linear (0.0625 to 1), not dB.
LIMITER_LIMIT = f"{10 ** (LIMITER_CEILING_DB / 20):.4f}"
GAIN_CLAMP_DB = 20.0
CORRECTION_THRESHOLD_LU = 0.5
SAMPLE_RATE = 44100
BITRATE = "256k"
FADE_CURVE = "qsin"

# How long to wait for the pipes to drain after a timeout kill. Bounded so that
# a process that escaped the group kill and still holds a pipe open can't hang
# the request; the tests shorten it.
DRAIN_SECONDS = 5.0
# How long the collection after a group kill waits for the killed members to
# die. SIGKILL ends a process at once unless it is stuck in the kernel, so
# this is headroom, and it keeps such a member from hanging the request.
COLLECT_SECONDS = 1.0
# prctl(2) option (linux/prctl.h).
PR_GET_CHILD_SUBREAPER = 37

# How much of the end of ffmpeg's stderr is kept; everything before it is read
# and dropped. The summary stands at the end: a 40 s pass of the image's
# ffmpeg printed about 70 kB in all (a loudness line per 100 ms), of which
# the summary line and everything after it were 643 bytes. 16 KiB keeps that
# 25 times over, and a run that prints more after its summary fails to parse
# rather than growing what the server holds.
STDERR_TAIL_BYTES = 16 * 1024
# The most read from a pipe at once.
_READ_BYTES = 64 * 1024

# The summary f_ebur128.c prints when the filter closes. Anchored on
# "Summary:" because every per-frame line also contains "I: ... LUFS", and the
# lines in between are spelled out so a changed format fails the render
# instead of being read loosely. Integrated loudness is never -inf (ebur128
# starts at the -70 LUFS gate), but true peak is -inf for digital silence.
# Each av_log call may carry a "[Parsed_ebur128_N @ 0x...] " prefix.
_PREFIX = r"(?:\[[^\]\n]*\] )?"
_NUMBER = r"-?[0-9]+\.[0-9]"
SUMMARY_RE = re.compile(
    _PREFIX + r"Summary:\n"
    r"\n"
    r"  Integrated loudness:\n"
    rf"    I:\s+({_NUMBER}) LUFS\n"
    rf"    Threshold:\s+(?:{_NUMBER}|-inf) LUFS\n"
    r"\n"
    r"  Loudness range:\n"
    rf"    LRA:\s+{_NUMBER} LU\n"
    rf"    Threshold:\s+(?:{_NUMBER}|-inf) LUFS\n"
    rf"    LRA low:\s+(?:{_NUMBER}|-inf) LUFS\n"
    rf"    LRA high:\s+(?:{_NUMBER}|-inf) LUFS\n"
    r"\n"
    r"  True peak:\n"
    rf"    Peak:\s+({_NUMBER}|-inf) dBFS\n"
)

# Every run starts like this: no stdin, no banner or progress lines, one
# thread, and local files only, whatever the binary was built with.
# -threads covers only the codecs; the filter graph otherwise starts a thread
# per usable CPU, so -filter_threads holds it to one as well. A run then has
# the same number of threads on any host, which the container's pids_limit
# counts (compose.example.yaml).
_COMMON = ["-nostdin", "-hide_banner", "-nostats", "-threads", "1", "-filter_threads", "1",
           "-protocol_whitelist", "file"]


class RenderError(Exception):
    """ffmpeg failed. code is render_failed, render_timeout or measure_failed.

    Deliberately carries no ffmpeg output: stderr can quote the tone name and
    paths, and it never leaves this module.
    """

    def __init__(self, code: str) -> None:
        if code not in ("render_failed", "render_timeout", "measure_failed"):
            raise ValueError(f"unknown render error code {code!r}")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RenderParams:
    fade_in: float
    fade_out: float
    normalize: bool
    target_lufs: float
    title: str
    duration: float


@dataclass(frozen=True)
class RenderResult:
    duration: float
    measured_lufs: float
    applied_gain_db: float
    output: Path


@dataclass(frozen=True)
class Limits:
    """Per-run resource limits, applied by prlimit before ffmpeg starts.

    No process limit: RLIMIT_NPROC counts every process and thread of the UID
    across the whole host, not per container, and every container of the
    compose example runs as UID 10001. In the image it would bound nothing
    for this container alone, or fail renders while other containers are
    busy. What can bound ffmpeg's processes is the container's pids_limit.
    """

    # Measured headroom: a 40 s render needs far less, and a demuxer bug that
    # tries to allocate gigabytes fails here instead of taking the host's memory.
    address_space_bytes: int = 512 * 1024 * 1024
    # CPU time, as a backstop to the wall-clock timeout.
    cpu_seconds: int = 20
    # The largest legitimate output: MAX_CLIP_SECONDS at its ceiling of 40, at
    # 33,000 bytes per second (BITRATE's 256 kbit/s plus the MP4 container, as
    # the contract sizes the tmpfs): 40 x 33,000 = 1,320,000 bytes. Twice
    # that is 2,640,000, rounded up to 4 MiB. One runaway output then can't
    # fill the tmpfs that every upload and result shares (64 MB in the
    # example).
    file_size_bytes: int = 4 * 1024 * 1024
    open_files: int = 64


@dataclass(frozen=True)
class _Loudness:
    integrated: float
    true_peak: float


def default_prlimit() -> tuple[str, ...] | None:
    """prlimit on Linux, always; nothing where it doesn't exist (Windows development).

    Not conditional on the file being there: on Linux a missing prlimit fails
    every render instead of silently running ffmpeg without limits.
    """
    return (PRLIMIT,) if sys.platform.startswith("linux") else None


def clamp_gain(gain_db: float) -> float:
    return max(-GAIN_CLAMP_DB, min(GAIN_CLAMP_DB, gain_db))


def _finite(value: float, name: str) -> float:
    # A NaN or infinity formatted into a filter string would be a filter
    # argument nobody chose. The server's parser can't produce one; this keeps it so.
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def measure_args(input: Path) -> list[str]:
    """Pass 1: integrated loudness and true peak of the upload, nothing written."""
    return [*_COMMON, "-f", "wav", "-i", str(input), "-af", "ebur128=peak=true", "-f", "null", "-"]


def filter_chain(params: RenderParams, gain_db: float) -> str:
    duration = _finite(params.duration, "duration")
    # Each fade is clamped to the clip, and rounded once, here, so the check
    # for zero sees exactly what ffmpeg will be given.
    fade_in = round(min(max(_finite(params.fade_in, "fade_in"), 0.0), duration), 3)
    fade_out = round(min(max(_finite(params.fade_out, "fade_out"), 0.0), duration), 3)
    # level=false: alimiter defaults to level=true, which re-normalises its
    # output and silently undoes the gain. latency=true keeps the clip length
    # exact.
    stages = [
        f"volume={_finite(gain_db, 'gain_db'):.2f}dB",
        f"alimiter=limit={LIMITER_LIMIT}:level=false:latency=true",
    ]
    # A zero fade is left out, not passed as d=0: afade reads d=0 as "use
    # nb_samples", which is a one-second fade nobody asked for.
    if fade_in > 0:
        stages.append(f"afade=t=in:st=0:d={fade_in:.3f}:curve={FADE_CURVE}")
    if fade_out > 0:
        stages.append(f"afade=t=out:st={max(duration - fade_out, 0.0):.3f}:d={fade_out:.3f}:curve={FADE_CURVE}")
    # Last, so it measures exactly what is encoded: after the gain, the
    # limiter and the fades.
    stages.append("ebur128=peak=true")
    return ",".join(stages)


def render_args(input: Path, output: Path, params: RenderParams, gain_db: float) -> list[str]:
    """Pass 2 (and 3): gain, limiter, fades and post-chain measurement, encoded to ipod AAC."""
    return [
        # -y: the corrective pass overwrites out.m4a, and with -nostdin ffmpeg
        # would otherwise refuse instead of asking.
        *_COMMON, "-y", "-f", "wav", "-i", str(input),
        "-af", filter_chain(params, gain_db),
        "-ar", str(SAMPLE_RATE),
        # No metadata from the input, and +bitexact drops the encoder tag, so
        # the title below is the only metadata in the file.
        "-map_metadata", "-1", "-fflags", "+bitexact",
        # One argv element: the sanitised name is never parsed by a shell and
        # can't become an option of its own.
        "-metadata", f"title={params.title}",
        "-c:a", "aac", "-b:a", BITRATE,
        "-movflags", "+faststart",
        "-f", "ipod", str(output),
    ]


def parse_summary(stderr: str) -> _Loudness | None:
    """The one ebur128 summary in stderr, or None when there isn't exactly one."""
    matches = SUMMARY_RE.findall(stderr.replace("\r\n", "\n"))
    if len(matches) != 1:
        return None
    integrated, peak = matches[0]
    return _Loudness(float(integrated), float(peak))


class Renderer:
    def __init__(
        self,
        command: tuple[str, ...],
        timeout_seconds: int,
        prlimit: tuple[str, ...] | None = None,
        limits: Limits | None = None,
    ) -> None:
        # Nothing here touches the filesystem: on Windows the production ffmpeg
        # path doesn't exist, and a server must still start for development.
        if not command:
            raise ValueError("command must name a program")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.prlimit = tuple(prlimit) if prlimit is not None else None
        # CPU seconds default to the wall-clock budget: a run can't use more
        # CPU than it is given time for, with one thread.
        self.limits = limits if limits is not None else Limits(cpu_seconds=timeout_seconds)

    def full_command(self, args: list[str]) -> list[str]:
        """prlimit (when configured), then the ffmpeg command, then args."""
        prefix: list[str] = []
        if self.prlimit is not None:
            limits = self.limits
            prefix = [
                *self.prlimit,
                f"--as={limits.address_space_bytes}",
                f"--cpu={limits.cpu_seconds}",
                f"--fsize={limits.file_size_bytes}",
                f"--nofile={limits.open_files}",
                # Everything after -- is the program and its arguments, never
                # an option of prlimit's.
                "--",
            ]
        return [*prefix, *self.command, *args]

    def render(self, job_dir: Path, params: RenderParams) -> RenderResult:
        """Render job_dir/in.wav to job_dir/out.m4a, or raise RenderError."""
        source = job_dir / "in.wav"
        output = job_dir / "out.m4a"
        try:
            if params.normalize:
                measured = self._run(measure_args(source), job_dir, "measure_failed")
                gain = clamp_gain(params.target_lufs - measured.integrated)
            else:
                # No measure pass at all: the user asked for the clip as it is.
                gain = 0.0
            post = self._render_once(source, output, params, gain, job_dir)
            if params.normalize and abs(post.integrated - params.target_lufs) > CORRECTION_THRESHOLD_LU:
                # The limiter and the fades move the loudness; one more run from
                # the original input with the error added back. A gain already
                # at the clamp can't move, so that run would change nothing.
                corrected = clamp_gain(gain + (params.target_lufs - post.integrated))
                if corrected != gain:
                    gain = corrected
                    post = self._render_once(source, output, params, gain, job_dir)
        except BaseException:
            # A half-written output is never served or left behind.
            _remove(output)
            raise
        return RenderResult(
            duration=params.duration, measured_lufs=post.integrated, applied_gain_db=gain, output=output
        )

    def _render_once(self, source: Path, output: Path, params: RenderParams, gain: float, job_dir: Path) -> _Loudness:
        loudness = self._run(render_args(source, output, params, gain), job_dir, "render_failed")
        # Only a regular file is a result. lstat, not stat or is_file(): the
        # job directory is writable by the UID ffmpeg runs as, and a link
        # left there would be followed by the size check here and opened by
        # the server on download (its own /proc/self/maps, say). A FIFO or
        # anything else is refused too. render() removes what is refused,
        # and removing a link never touches its target.
        try:
            info = os.lstat(output)
        except OSError:
            raise RenderError("render_failed") from None
        if not stat.S_ISREG(info.st_mode):
            raise RenderError("render_failed")
        # prlimit's RLIMIT_FSIZE stops ffmpeg writing past the limit, but only
        # where prlimit runs (not on Windows, for development). Checked here
        # too, so an oversized file is never a result anywhere; render()
        # removes it. Equal to the limit is what RLIMIT_FSIZE itself allows.
        if info.st_size > self.limits.file_size_bytes:
            raise RenderError("render_failed")
        return loudness

    def _run(self, args: list[str], job_dir: Path, failure: str) -> _Loudness:
        command = self.full_command(args)
        try:
            # Safe by construction: an argument list with no shell, a program
            # path fixed in code (FFMPEG, PRLIMIT), arguments that are server
            # constants, server-created paths and validated numbers plus the
            # sanitised title as one element, and a wall-clock timeout below.
            # tests/test_render.py pins each of these guards.
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=job_dir,
                # Its own session and process group, so a timeout can kill
                # everything ffmpeg (or anything it started) is running.
                start_new_session=True,
            )
        except OSError:
            # Missing binary, missing prlimit, or no resources to start it.
            raise RenderError(failure) from None
        try:
            stderr = _collect(proc, self.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                # Drain, not wait(): the pipes must be read to EOF to free them,
                # and the bound keeps a survivor holding them from hanging us.
                _collect(proc, DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            finally:
                # A drain that gave up leaves both pipes open; close them here
                # rather than whenever the Popen object is collected.
                _close_pipes(proc)
                _collect_group(proc)
            raise RenderError("render_timeout") from None
        except BaseException:
            try:
                _kill_group(proc)
                proc.wait()
            finally:
                _close_pipes(proc)
                _collect_group(proc)
            raise
        # A run that ended by itself, success included, may have left a child
        # behind that closed its pipes (so _collect returned). It would run on
        # as the server's UID next to the job directory, so the group is
        # killed before anything of the run is used: before the exit status,
        # the summary or the output. killpg on the reaped leader's PID still
        # reaches the group's other members: Linux doesn't reuse a PID that
        # is a live group's ID. Once the group is empty the PID is free, but
        # PIDs are handed out in turn, so another render's ffmpeg could hold
        # it only after the PID space wrapped in between. The two paths
        # above that raise have killed the group already, so every run's
        # group is killed exactly once, and each kill is followed by the
        # collection of what it killed. A child that called setsid() or
        # setpgid() has left the group and is not reached; the core runs
        # renders side by side, so it can't sweep its UID's processes as
        # the fetcher's runner does.
        _kill_group(proc)
        _collect_group(proc)
        if proc.returncode != 0:
            raise RenderError(failure)
        loudness = parse_summary(stderr.decode("utf-8", errors="replace"))
        if loudness is None:
            raise RenderError(failure)
        return loudness


def _collect(proc: subprocess.Popen[bytes], timeout: float) -> bytes:
    """Read proc's stdout and stderr to EOF at once, wait for it, and return the end of stderr.

    In place of communicate(), which keeps everything both streams carry: a
    run that printed without end would grow the server's memory. stdout is
    read and dropped, and only the last STDERR_TAIL_BYTES of stderr are kept.
    Both pipes are read until they close, so ffmpeg never blocks on a full
    one. Raises subprocess.TimeoutExpired after timeout seconds; a later
    call on the same proc carries on where this one stopped. Each pipe is
    closed once it reaches EOF.
    """
    deadline = time.monotonic() + timeout
    if os.name == "posix":
        tail = _read_selecting(proc, deadline, timeout)
    else:
        tail = _read_threaded(proc, deadline, timeout)
    try:
        proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        raise subprocess.TimeoutExpired(proc.args, timeout) from None
    return tail


def _read_selecting(proc: subprocess.Popen[bytes], deadline: float, timeout: float) -> bytes:
    # One thread and a selector, as communicate() reads on POSIX: nothing is
    # left blocked on a pipe after a timeout, so _close_pipes can close both
    # even while a survivor still holds the other end.
    tail = bytearray()
    with selectors.DefaultSelector() as selector:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            for key, _events in selector.select(remaining):
                # The selector said readable, so this returns without blocking.
                chunk = os.read(key.fd, _READ_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                elif key.fileobj is proc.stderr:
                    tail += chunk
                    del tail[:-STDERR_TAIL_BYTES]
    return bytes(tail)


class _PipeReader(threading.Thread):
    """Reads one pipe to EOF, keeping at most keep bytes of its end, then closes it."""

    def __init__(self, pipe: io.BufferedReader, keep: int) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.keep = keep
        self.tail = bytearray()

    def run(self) -> None:
        try:
            while chunk := self.pipe.read1(_READ_BYTES):
                if self.keep:
                    self.tail += chunk
                    del self.tail[:-self.keep]
        except (OSError, ValueError):
            # The pipe broke or was closed under us: nothing more to read.
            pass
        finally:
            self.pipe.close()


def _read_threaded(proc: subprocess.Popen[bytes], deadline: float, timeout: float) -> bytes:
    # Windows (development only) can't select on pipes, so a thread reads
    # each, as communicate() does there. The threads are kept on proc under
    # the names communicate() uses, so a second call, the drain after a
    # timeout, joins the same readers, and _close_pipes leaves a pipe whose
    # reader still runs to that reader.
    for name, keep in (("stdout", 0), ("stderr", STDERR_TAIL_BYTES)):
        pipe = getattr(proc, name)
        if pipe is not None and getattr(proc, f"{name}_thread", None) is None:
            reader = _PipeReader(pipe, keep)
            setattr(proc, f"{name}_thread", reader)
            reader.start()
    readers = [getattr(proc, f"{name}_thread", None) for name in ("stdout", "stderr")]
    for reader in readers:
        if reader is None:
            continue
        reader.join(max(deadline - time.monotonic(), 0.0))
        if reader.is_alive():
            raise subprocess.TimeoutExpired(proc.args, timeout)
    return bytes(readers[1].tail) if readers[1] is not None else b""


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


def _collect_group(proc: subprocess.Popen[bytes]) -> None:
    """Collect the dead members of proc's process group that are this process's children.

    A run's leader is proc itself, which Popen waits for. Its own children
    become children of whatever process orphans go to: the init of their
    PID namespace, or the nearest child subreaper. In the compose example
    the server is its container's PID 1, so each child a run leaves in its
    group is the server's once the leader is gone, and once the group kill
    has killed it, it stays a zombie that holds a slot of the container's
    pids_limit until the server waits for it. Enough of them and no render
    and no health check can start until the container restarts.

    Only after the leader has been waited for, so this can never take the
    leader's status from Popen. Only the run's own group (P_PGID): the core
    runs renders side by side, and another render's ffmpeg, which leads a
    group of its own, may have exited and not yet been waited for; taking it
    would hand that render a status of 0. The group's ID is the leader's
    PID, which Linux doesn't hand out again while any member, a zombie
    included, is left in the group. Bounded by COLLECT_SECONDS, so a member
    that doesn't die at once can't hang the request; it then stays until the
    container restarts, as does a child that left the group with setsid()
    or setpgid(), alive or dead. Where orphans go to another process, that
    process collects them, and this does nothing at all.
    """
    if proc.returncode is None or not _orphans_come_here():
        return
    deadline = time.monotonic() + COLLECT_SECONDS
    while True:
        try:
            if os.waitid(os.P_PGID, proc.pid, os.WEXITED | os.WNOHANG) is not None:
                continue
        except OSError:
            # ECHILD: no child of this process is left in the group.
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


@functools.cache
def _orphans_come_here() -> bool:
    """Whether a run's orphaned children become this process's children: PID 1, or a child subreaper (Linux)."""
    if not sys.platform.startswith("linux"):
        return False
    if os.getpid() == 1:
        return True
    return _is_child_subreaper()


def _is_child_subreaper() -> bool:
    """What prctl(PR_GET_CHILD_SUBREAPER) says; False when it can't be asked."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        # Declared, as variadic prctl needs: the second argument is a
        # pointer the kernel writes the flag to.
        prctl.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        flag = ctypes.c_int(0)
        if prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) != 0:
            return False
        return flag.value != 0
    except (OSError, AttributeError):
        return False


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for name in ("stdout", "stderr"):
        pipe = getattr(proc, name)
        if pipe is None or pipe.closed:
            continue
        # On Windows _collect reads through helper threads that hold the
        # pipe's lock while blocked in a read. Closing under a live one would
        # wait for whatever survivor still holds the pipe open, which is the
        # hang the bounded drain exists to avoid; such a thread closes its
        # pipe itself once the pipe reaches EOF, so it is left to the thread.
        reader = getattr(proc, f"{name}_thread", None)
        if reader is not None and reader.is_alive():
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # The job directory is removed by the caller in any case.
        pass
