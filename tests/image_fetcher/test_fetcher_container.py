"""The fetcher image's contract, checked from inside the running image (docs/PLAN.md 6, 6.1 and 9.2).

The test run uses the contract's own flags (README, Development): --network
none, --read-only, a /tmp tmpfs, --cap-drop ALL, no-new-privileges, --ipc none
and --ulimit msgqueue=0. These tests prove the image works under them and that
they hold: a fixed non-root user, no writable path but /tmp, exactly the
locked yt-dlp and yt-dlp-ejs in a venv of their own while the server's
interpreter holds no third-party distribution at all, a Deno that yt-dlp finds
and that runs under the runner's resource limits, and the image's three
commands (docs/PLAN.md 6.1): the fetcher, the image's CMD, which starts and
answers /fetch/healthz without a runner; the runner
(python -I /app/fetcher/runner.py), yt-dlp's only home, which starts with no
default route, answers /healthz, fails an offline fetch, leaves its scratch
empty and loads only its own package and the modules it shares; and the
forwarder (python -I /app/fetcher/forwarder.py), which answers refusals and
loads only its own package.

Nothing here talks to YouTube. The tests that start the real yt-dlp with the
runner's real argument list, directly or through the runner command, first
prove the container has no network, so the run can only fail at name
resolution. The forwarder test sends only requests the forwarder refuses
before any DNS lookup.
"""
from __future__ import annotations

import ast
import http.client
import json
import operator
import os
import re
import resource
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The run code lives where yt-dlp runs, in the runner's package (docs/PLAN.md
# 6.1); the metadata whitelist and its FetchError are shared by both servers.
try:
    from strange_ringtone_fetch.metadata import FetchError
    from strange_ringtone_runner import ytdlp
    from strange_ringtone_runner.ytdlp import DEFAULT_COMMAND, DENO, PRLIMIT, YtDlp, default_prlimit, ytdlp_args

    _RUNNER_IMPORT_ERROR = ""
except ImportError as _error:
    # An image without the runner's package (a broken build) must still load
    # this module, so that what is missing shows up as failed assertions
    # (RunnerTest, FilesystemTest, UserTest.test_the_runner_uses_prlimit_here)
    # rather than one import error that hides every other result. The names
    # come from the fetcher's own copy of the run code while an image still
    # ships it; once it is gone, an image without the runner fails to import
    # here, loudly.
    _RUNNER_IMPORT_ERROR = f"{type(_error).__name__}: {_error}"
    from strange_ringtone_fetch import ytdlp  # type: ignore[no-redef]
    from strange_ringtone_fetch.ytdlp import (  # type: ignore[no-redef]
        DEFAULT_COMMAND,
        DENO,
        PRLIMIT,
        FetchError,
        YtDlp,
        default_prlimit,
        ytdlp_args,
    )

UID = GID = 10001
PORT = 8080
# The image's CMD (fetcher/Dockerfile). A test can't read it from inside the
# container, so a change there must change this line too.
CMD = ["python", "-I", "/app/fetcher"]
# The runner, the image's third command (compose.example.yaml), on its fixed
# port (strange_ringtone_runner.__main__.PORT), written out so that a change
# there fails here too. Its scratch directory is its own, beside the
# fetcher's (strange_ringtone_runner.config.SCRATCH_DIR).
RUNNER_CMD = ["python", "-I", "/app/fetcher/runner.py"]
RUNNER_PORT = 8080
RUNNER_HEALTH_PATH = "/healthz"
RUNNER_PACKAGE = Path("/app/fetcher/strange_ringtone_runner")
RUNNER_MODULES = {"__init__.py", "__main__.py", "config.py", "server.py", "ytdlp.py"}
RUNNER_SCRATCH = "/tmp/strange_ringtone_runner"
FETCHER_SCRATCH = "/tmp/strange_ringtone_fetch"
# The forwarder, the image's second command (compose.example.yaml), on its
# fixed port (strange_ringtone_forwarder.policy.PORT), written out so that a
# change there fails here too.
FORWARDER_CMD = ["python", "-I", "/app/fetcher/forwarder.py"]
FORWARDER_PORT = 3128
FORWARDER_PACKAGE = Path("/app/fetcher/strange_ringtone_forwarder")
LOCK = Path("/app/fetcher/requirements.txt")
# yt-dlp and yt-dlp-ejs live in a venv of their own (docs/PLAN.md 6.1): a wheel
# installed for the server's interpreter would be code in the server, since -I
# still runs .pth lines and pip writes scripts and data into the prefix.
VENV = Path("/opt/yt-dlp")
VENV_PYTHON = str(VENV / "bin" / "python")
VENV_SITE = VENV / "lib" / "python3.12" / "site-packages"
# The server's (and the forwarder's) interpreter: CMD[0] on the image's PATH.
BASE_SITE = Path("/usr/local/lib/python3.12/site-packages")
BASE_PREFIX = "/usr/local"
# What the yt-dlp and pip wheels would put into the base prefix if either were
# installed there (seen in the image before the venv): pip's and yt-dlp's
# console scripts and yt-dlp's data files. None of them may exist after the
# build; the venv's own copies live under /opt/yt-dlp instead.
BASE_WHEEL_FILES = (
    "/usr/local/bin/pip",
    "/usr/local/bin/pip3",
    "/usr/local/bin/pip3.12",
    "/usr/local/bin/yt-dlp",
    "/usr/local/share/bash-completion/completions/yt-dlp",
    "/usr/local/share/doc/yt_dlp",
    "/usr/local/share/man/man1/yt-dlp.1",
    "/usr/local/share/fish/vendor_completions.d/yt-dlp.fish",
    "/usr/local/share/zsh/site-functions/_yt-dlp",
)
# The core modules the fetcher imports, and nothing else of the core
# (fetcher/Dockerfile, fetcher/__main__.py).
CORE_FILES = {"__init__.py", "config.py", "http_guard.py"}
FORWARDER_MODULES = {"__init__.py", "__main__.py", "policy.py", "server.py"}
STARTUP_SECONDS = 15.0
RUN_SECONDS = 60
# yt-dlp's Deno options for its challenge solver, as yt-dlp 2026.10.26 builds
# them (yt_dlp/extractor/youtube/jsc/_builtin/deno.py: _DENO_BASE_OPTIONS plus
# --no-npm --cached-only for the bundled solver), reading the script on stdin.
DENO_RUN = [
    "run", "--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "--no-lock",
    "--node-modules-dir=none", "--no-config", "--no-npm", "--cached-only", "-",
]
# A format-valid ID that the run below never gets to look up.
OFFLINE_VIDEO_ID = "AAAAAAAAAAA"
_LOCK_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", re.MULTILINE)

# The forwarder's per-tunnel download cap must hold one yt-dlp range (G9, the
# owner's decision of 2026-11-03). TLS records and the HTTP response head add
# well under 1 MiB to a 10 MiB range, so the cap must leave at least that
# above one range.
RANGE_HEADROOM_BYTES = 1 << 20
# yt-dlp 2026.10.26 sets the range size as a function-local of
# YoutubeIE._extract_formats_and_subtitles (CHUNK_SIZE = 10 << 20), used both
# as http_chunk_size and as the dashy fragment size, so importing the module
# can't read it: the source line is read instead.
CHUNK_SIZE_RE = re.compile(r"^\s*CHUNK_SIZE\s*=\s*(?P<expr>.+?)\s*(#.*)?$", re.M)
# Run under the venv's interpreter, so the file read is the one the venv
# imports. find_spec of a top-level package imports nothing.
_RANGE_PROBE = """\
import importlib.util, json, re, sys
from pathlib import Path
spec = importlib.util.find_spec("yt_dlp")
path = Path(spec.submodule_search_locations[0]) / "extractor" / "youtube" / "_video.py"
pattern = re.compile(sys.argv[1], re.M)
text = path.read_text(encoding="utf-8")
print(json.dumps({"path": str(path), "lines": [match.group(0) for match in pattern.finditer(text)]}))
"""
# What an interpreter can import and where it looks: run under the base
# interpreter and under the venv's, in isolated mode as the image runs them.
_INTERPRETER_PROBE = """\
import importlib.metadata, importlib.util, json, os, site, sys
sites = site.getsitepackages()
print(json.dumps({
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "path": sys.path,
    "sites": sites,
    "distributions": {str(d.metadata["Name"]).lower(): d.version for d in importlib.metadata.distributions()},
    "pth": sorted(os.path.join(d, n) for d in sites if os.path.isdir(d) for n in os.listdir(d) if n.endswith(".pth")),
    "importable": sorted(n for n in ("pip", "yt_dlp", "yt_dlp_ejs") if importlib.util.find_spec(n) is not None),
}))
"""
# The files the venv's distributions installed outside their site-packages
# (console scripts, data files), relative to the venv's prefix, from RECORD.
_OUTSIDE_SITE_PROBE = """\
import importlib.metadata, json, os, sys
prefix = os.path.realpath(sys.prefix)
site = os.path.realpath(os.path.join(prefix, "lib", "python3.12", "site-packages"))
found = set()
for dist in importlib.metadata.distributions():
    for entry in dist.files or ():
        path = os.path.realpath(dist.locate_file(entry))
        if not path.startswith(site + os.sep) and path.startswith(prefix + os.sep):
            found.add(os.path.relpath(path, prefix))
print(json.dumps(sorted(found)))
"""
_VERSION_PROBE = "import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))"
# Run under the venv's interpreter: for every module of the two packages, the
# .pyc that interpreter would load (its own cache tag, no optimisation level)
# must exist and match the source, by timestamp and size or by hash (PEP 552).
# find_spec of a top-level package imports nothing.
_BYTECODE_PROBE = """\
import importlib.util, json, os, sys
bad, counts = [], {}
for package in ("yt_dlp", "yt_dlp_ejs"):
    spec = importlib.util.find_spec(package)
    root = spec.submodule_search_locations[0]
    counts[package] = 0
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            source = os.path.join(directory, name)
            counts[package] += 1
            cached = importlib.util.cache_from_source(source)
            try:
                with open(cached, "rb") as handle:
                    header = handle.read(16)
                with open(source, "rb") as handle:
                    data = handle.read()
            except OSError as error:
                bad.append([source, type(error).__name__])
                continue
            stat = os.stat(source)
            flags = int.from_bytes(header[4:8], "little")
            if header[:4] != importlib.util.MAGIC_NUMBER:
                bad.append([source, "magic"])
            elif flags & 1:
                if header[8:16] != importlib.util.source_hash(data):
                    bad.append([source, "hash"])
            elif (int.from_bytes(header[8:12], "little"), int.from_bytes(header[12:16], "little")) != (
                    int(stat.st_mtime) & 0xFFFFFFFF, stat.st_size & 0xFFFFFFFF):
                bad.append([source, "timestamp"])
print(json.dumps({"tag": sys.implementation.cache_tag, "magic": importlib.util.MAGIC_NUMBER.hex(),
                  "counts": counts, "bad": bad}))
"""
_TAG_PROBE = (
    "import importlib.util, json, sys; "
    "print(json.dumps({'tag': sys.implementation.cache_tag, 'magic': importlib.util.MAGIC_NUMBER.hex()}))"
)
_EJS_PIN_PROBE = """\
from importlib import metadata
import json
print(json.dumps({"ejs": metadata.version("yt-dlp-ejs"), "requires": metadata.requires("yt-dlp") or []}))
"""
# The strange_ringtone* modules a process starting the forwarder loads, with
# /app/fetcher first on sys.path as the entry script's own directory is.
_FORWARDER_MODULES_PROBE = """\
import json, sys
sys.path.insert(0, "/app/fetcher")
import strange_ringtone_forwarder.__main__
print(json.dumps(sorted(name for name in sys.modules if name.startswith("strange_ringtone"))))
"""
# The modules a process starting the runner loads: the entry script itself is
# run (under another name, so its main() doesn't start a server), so its own
# sys.path edit is what is tested, and the strange_ringtone* and yt_dlp*
# modules are listed afterwards.
_RUNNER_MODULES_PROBE = """\
import json, runpy, sys
runpy.run_path(sys.argv[1], run_name="strange_ringtone_runner_probe")
print(json.dumps({
    "modules": sorted(name for name in sys.modules if name.startswith("strange_ringtone")),
    "yt_dlp": sorted(name for name in sys.modules if name.split(".")[0] in ("yt_dlp", "yt_dlp_ejs")),
}))
"""
# The runner's audio route's token: 32 lowercase hex characters, as the
# fetcher mints them (secrets.token_hex(16)).
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
# A console script's interpreter line: "#!" and the interpreter's path.
_SHEBANG_RE = re.compile(r"#!\s*(\S+)")
# The base interpreter's names in its prefix (python, python3, python3.12).
_BASE_PYTHON_RE = re.compile(r"/usr/local/bin/python(3(\.12)?)?")


def _status_fields(pid: int | str = "self") -> dict[str, str]:
    fields = {}
    with open(f"/proc/{pid}/status", encoding="ascii") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def _mounts() -> dict[str, tuple[str, set[str]]]:
    """Mount point -> (filesystem type, options), the last mount winning."""
    mounts = {}
    with open("/proc/self/mounts", encoding="utf-8") as handle:
        for line in handle:
            _device, point, fstype, options, *_ = line.split()
            mounts[point] = (fstype, set(options.split(",")))
    return mounts


def _interfaces() -> set[str]:
    with open("/proc/net/dev", encoding="ascii") as handle:
        return {line.split(":", 1)[0].strip() for line in handle if ":" in line}


def _locked() -> dict[str, str]:
    """Distribution name -> version, as fetcher/requirements.txt pins them."""
    return {name.lower(): version for name, version in _LOCK_LINE_RE.findall(LOCK.read_text(encoding="utf-8"))}


def _ytdlp() -> YtDlp:
    # The runner's own construction (strange_ringtone_runner.server) with the
    # default settings: the real command, prlimit and limits.
    return YtDlp(max_seconds=600, max_bytes=40 * 1024 * 1024, timeout_seconds=120, prlimit=default_prlimit())


def _run(argv: list[str], timeout: float = RUN_SECONDS) -> subprocess.CompletedProcess[str]:
    """argv's result. A program that can't start (no venv in the image) comes back as exit 127 with the reason.

    So a missing or broken layout fails an assertion on the exit code rather
    than erroring out of the test.
    """
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except OSError as error:
        return subprocess.CompletedProcess(argv, 127, "", f"cannot run {argv[0]}: {error}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]} did not finish within {timeout} s")


def _python_json(python: str, code: str, *args: str) -> tuple[Any, str]:
    """(the JSON code printed under `python -I`, "") or (None, why it didn't)."""
    proc = _run([python, "-I", "-c", code, *args])
    if proc.returncode != 0:
        return None, f"{python} -I exited {proc.returncode}: {proc.stderr}"
    try:
        return json.loads(proc.stdout), ""
    except ValueError:
        return None, f"{python} -I printed no JSON: {proc.stdout!r} {proc.stderr!r}"


def _venv_version(name: str) -> str | None:
    """The version of distribution name as the venv's interpreter sees it, or None."""
    proc = _run([VENV_PYTHON, "-I", "-c", _VERSION_PROBE, name])
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


_INT_OPERATORS: dict[type[ast.operator], Callable[[int, int], int]] = {
    ast.LShift: operator.lshift,
    ast.Mult: operator.mul,
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Pow: operator.pow,
}
# Every constant and every intermediate value of the range-size expression
# must fit in this many bits. A range is a few MiB, so 64 bits is far more
# than a real one needs, and with operands that small and shift counts and
# exponents of at most 64, no single step can grow large. Bounding the shift
# count and the exponent alone let nested powers grow 64-fold at every level
# (Copilot's review of 96b7db0).
_INT_EXPRESSION_BITS = 64


def _int_expression(text: str) -> int:
    """The value of a plain integer expression (ints combined by <<, *, +, - and **), or ValueError.

    Never eval: the text comes from a third-party file. Every constant and
    every intermediate value must fit in _INT_EXPRESSION_BITS bits, and shift
    counts and exponents in 0 to 64, so a hostile line can't make the test
    hang or exhaust memory.
    """
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"not an integer expression: {text!r}") from error

    def bounded(number: int) -> int:
        if number.bit_length() > _INT_EXPRESSION_BITS:
            raise ValueError(f"a value wider than {_INT_EXPRESSION_BITS} bits in {text!r}")
        return number

    def value(node: ast.AST) -> int:
        # type() is int, not isinstance: True and False are ints too.
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return bounded(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _INT_OPERATORS:
            left, right = value(node.left), value(node.right)
            if isinstance(node.op, (ast.LShift, ast.Pow)) and not 0 <= right <= 64:
                raise ValueError(f"shift or exponent out of range in {text!r}")
            # Both operands fit in 64 bits, so even a step whose result is
            # too wide, and is refused, costs next to nothing.
            return bounded(_INT_OPERATORS[type(node.op)](left, right))
        raise ValueError(f"not a plain integer expression: {text!r}")

    return value(tree.body)


class UserTest(unittest.TestCase):
    def test_runs_as_the_fixed_non_root_user(self) -> None:
        self.assertEqual((os.getuid(), os.geteuid()), (UID, UID))
        self.assertEqual((os.getgid(), os.getegid()), (GID, GID))
        self.assertLessEqual(set(os.getgroups()), {GID})

    def test_has_no_capabilities_and_cannot_gain_privileges(self) -> None:
        status = _status_fields()
        for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            with self.subTest(field=field):
                self.assertEqual(int(status[field], 16), 0)
        self.assertEqual(status["NoNewPrivs"], "1")

    def test_prlimit_can_lower_the_fetchers_limits_without_privileges(self) -> None:
        # The fetcher wraps yt-dlp in prlimit (docs/PLAN.md 6). Lowering a
        # limit needs no capability, so it must work under cap-drop ALL; and
        # RLIMIT_AS is left alone, because Deno reserves a huge address space.
        self.assertTrue(os.access(PRLIMIT, os.X_OK))
        proc = subprocess.run(
            [
                PRLIMIT, "--cpu=20", "--fsize=67108864", "--nofile=64", "--nproc=512", "--",
                "python", "-I", "-c",
                "import resource as r; print(*(r.getrlimit(k)[0] for k in "
                "(r.RLIMIT_CPU, r.RLIMIT_FSIZE, r.RLIMIT_NOFILE, r.RLIMIT_NPROC, r.RLIMIT_AS)))",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(proc.stdout.split(), ["20", "67108864", "64", "512", str(resource.RLIM_INFINITY)])

    def test_the_runner_uses_prlimit_here(self) -> None:
        # R2/I1: on Linux the runner never runs yt-dlp without its limits. The
        # names must be the runner's own, from its package in the image, not
        # a copy the fetcher kept.
        self.assertEqual(_RUNNER_IMPORT_ERROR, "", "the runner's package does not import in the image")
        self.assertEqual(default_prlimit.__module__, "strange_ringtone_runner.ytdlp")
        self.assertEqual(default_prlimit(), (PRLIMIT,))


class FilesystemTest(unittest.TestCase):
    def test_nothing_outside_tmp_is_writable(self) -> None:
        # The directories this image adds for the venv, the forwarder and the
        # runner must exist, or the probe below would pass on them without
        # proving anything (a missing directory refuses a file too).
        for directory in (
            str(VENV), str(VENV_SITE), str(VENV_SITE / "yt_dlp"), str(FORWARDER_PACKAGE), str(RUNNER_PACKAGE),
        ):
            with self.subTest(exists=directory):
                self.assertTrue(os.path.isdir(directory), f"{directory} is not in the image")
        for directory in (
            "/", "/app", "/app/core", "/app/core/strange_ringtone", "/app/fetcher",
            "/app/fetcher/strange_ringtone_fetch", str(FORWARDER_PACKAGE), str(RUNNER_PACKAGE),
            str(BASE_SITE), str(BASE_SITE / "yt_dlp"),
            # The venv is root's: neither the server, the forwarder nor yt-dlp
            # can change the code yt-dlp runs.
            "/opt", str(VENV), str(VENV_SITE), str(VENV_SITE / "yt_dlp"),
            "/usr/local/bin", "/usr/local/lib", "/etc", "/var/tmp", "/root", "/home", "/run", "/nonexistent",
            # Docker gives every container a writable /dev/shm tmpfs and an
            # mqueue mount whatever --read-only says; --ipc none removes the
            # first and --ulimit msgqueue=0 stops anything being created in
            # the second.
            "/dev", "/dev/shm", "/dev/mqueue",
        ):
            path = Path(directory) / "strange_ringtone-write-probe"
            with self.subTest(directory=directory):
                try:
                    handle = open(path, "x")
                except OSError:
                    continue
                handle.close()
                os.unlink(path)
                self.fail(f"{directory} is writable")
        self.assertFalse("/dev/shm" in _mounts(), "a /dev/shm tmpfs is mounted; run with --ipc none")

    def test_shipped_files_cannot_be_modified(self) -> None:
        for path in (
            DENO, str(LOCK), "/app/fetcher/__main__.py", "/app/fetcher/strange_ringtone_fetch/metadata.py",
            "/app/core/strange_ringtone/http_guard.py", str(VENV_SITE / "yt_dlp" / "__init__.py"),
            VENV_PYTHON, str(VENV / "pyvenv.cfg"), "/app/fetcher/forwarder.py", str(FORWARDER_PACKAGE / "server.py"),
            "/app/fetcher/runner.py", str(RUNNER_PACKAGE / "server.py"), str(RUNNER_PACKAGE / "ytdlp.py"),
        ):
            with self.subTest(path=path):
                # It must ship, or the refused open below proves nothing.
                self.assertTrue(os.path.isfile(path), f"{path} is not in the image")
                with self.assertRaises(OSError):
                    open(path, "ab").close()

    def test_root_owns_the_code_and_nobody_else_can_write_it(self) -> None:
        # The read-only root filesystem refuses every write whoever owns a
        # file, so the probes above would pass even if the build handed /app
        # or the venv to the server's UID. Ownership is the second defence
        # (the Dockerfile's "root owns everything"): checked here on every
        # entry, so a chown or a group- or world-writable mode fails.
        problems: list[str] = []
        checked = 0

        def check(path: str) -> None:
            nonlocal checked
            info = os.lstat(path)
            checked += 1
            if (info.st_uid, info.st_gid) != (0, 0):
                problems.append(f"{path} is owned by {info.st_uid}:{info.st_gid}")
            # A symlink's own mode is always rwxrwxrwx and means nothing; its
            # target is checked where it lives.
            if not stat.S_ISLNK(info.st_mode) and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                problems.append(f"{path} has mode {stat.filemode(info.st_mode)}")

        def refuse(error: OSError) -> None:
            # A directory the walk can't read hides what is in it.
            raise error

        for root in ("/app", str(VENV)):
            with self.subTest(root=root):
                self.assertTrue(os.path.isdir(root), f"{root} is not in the image")
            check(root)
            for directory, names, files in os.walk(root, onerror=refuse):
                for name in names + files:
                    check(os.path.join(directory, name))
        for path in (DENO, "/opt"):
            check(path)
        # The walk saw the venv's packages and the forwarder, not an empty tree.
        self.assertGreater(checked, 500)
        self.assertEqual(problems, [])

    def test_tmp_is_the_writable_tmpfs(self) -> None:
        fd, name = tempfile.mkstemp(dir="/tmp")
        try:
            os.write(fd, b"probe")
        finally:
            os.close(fd)
            os.unlink(name)
        mounts = _mounts()
        fstype, options = mounts["/tmp"]
        self.assertEqual(fstype, "tmpfs")
        # noexec: nothing yt-dlp downloads can be run.
        self.assertLessEqual({"rw", "noexec", "nosuid"}, options)
        self.assertIn("ro", mounts["/"][1])

    def test_only_the_three_shared_core_modules_ship(self) -> None:
        # The fetcher serves no page and renders nothing: no server, renderer,
        # static files or ffmpeg of the core's are in this image.
        core = Path("/app/core")
        self.assertEqual({p.name for p in core.iterdir()}, {"strange_ringtone"})
        package = core / "strange_ringtone"
        self.assertEqual({p.name for p in package.iterdir() if p.name != "__pycache__"}, CORE_FILES)
        self.assertIsNone(shutil.which("ffmpeg", path="/usr/local/bin:/usr/bin:/bin"))


class DependenciesTest(unittest.TestCase):
    """I1: the venv holds exactly the lock and is what yt-dlp runs from; the base interpreter holds nothing."""

    def test_lock_holds_exactly_yt_dlp_and_yt_dlp_ejs(self) -> None:
        self.assertEqual(set(_locked()), {"yt-dlp", "yt-dlp-ejs"})

    def test_base_interpreter_has_no_distributions_no_pth_and_no_venv_on_sys_path(self) -> None:
        # I1. The interpreter that runs the fetcher and the forwarder (CMD[0],
        # FORWARDER_CMD[0]) in a fresh process, as they start: no installed
        # distribution (pip removed too), no .pth line to run at startup,
        # nothing of the venv on sys.path, nothing third-party importable.
        self.assertEqual(CMD[0], FORWARDER_CMD[0])
        data, why = _python_json(CMD[0], _INTERPRETER_PROBE)
        self.assertIsNotNone(data, why)
        assert data is not None
        self.assertEqual((data["prefix"], data["base_prefix"]), (BASE_PREFIX, BASE_PREFIX))
        self.assertIn(str(BASE_SITE), data["sites"])
        self.assertEqual(data["distributions"], {})
        self.assertEqual(data["pth"], [])
        self.assertEqual(data["importable"], [])
        self.assertEqual([entry for entry in data["path"] if entry.startswith("/opt")], [])
        self.assertNotIn(str(VENV_SITE), data["path"])
        # Nothing in the base site-packages but the base image's README: no
        # package, module, .pth, .dist-info or .egg-link of anyone's.
        self.assertEqual(sorted(set(os.listdir(BASE_SITE)) - {"README.txt", "__pycache__"}), [])

    def test_base_prefix_holds_no_wheel_scripts_or_data(self) -> None:
        # I1. pip writes a wheel's console scripts and data files into the
        # interpreter's prefix; none of the lock's (nor pip's own) may be in
        # /usr/local, where the server's PATH would find them.
        self.assertEqual([path for path in BASE_WHEEL_FILES if os.path.lexists(path)], [])
        # Every file the venv's distributions put outside their site-packages
        # (from their RECORD), which landed under /opt/yt-dlp, is absent from
        # the base prefix at the same place.
        outside, why = _python_json(VENV_PYTHON, _OUTSIDE_SITE_PROBE)
        self.assertIsNotNone(outside, why)
        assert outside is not None
        self.assertIn("bin/yt-dlp", outside)
        for relative in outside:
            with self.subTest(relative=relative):
                self.assertTrue(os.path.lexists(VENV / relative), f"{VENV / relative} is listed but missing")
                self.assertFalse(os.path.lexists(Path(BASE_PREFIX) / relative), f"{BASE_PREFIX}/{relative} exists")

    def test_venv_scripts_went_to_the_venv_not_the_base_prefix(self) -> None:
        # I2. The venv is filled by the base interpreter's pip through
        # --prefix /opt/yt-dlp, never by pip running under the venv's
        # interpreter (--python), which would have had the venv on its path
        # while it wrote the wheels. pip writes a console script's
        # interpreter line from the interpreter pip itself runs under, so
        # yt-dlp's script names the base interpreter, not the venv's. It
        # lands in the venv's bin, which no PATH holds, and nothing runs it:
        # yt-dlp runs as a module of the venv's interpreter.
        script = VENV / "bin" / "yt-dlp"
        try:
            info = os.lstat(script)
            with open(script, "rb") as handle:
                first = handle.readline(256)
        except OSError as error:
            self.fail(f"{script} is not in the image: {error}")
        self.assertTrue(stat.S_ISREG(info.st_mode), f"{script} is not a regular file")
        self.assertEqual((info.st_uid, info.st_gid), (0, 0), f"{script} is not root's")
        match = _SHEBANG_RE.match(first.decode("ascii", "replace"))
        self.assertIsNotNone(match, f"{script} has no interpreter line: {first!r}")
        assert match is not None
        interpreter = match.group(1)
        self.assertFalse(interpreter.startswith(str(VENV) + "/"),
                         f"{script} names the venv's interpreter {interpreter}: pip ran under it (--python)")
        self.assertIsNotNone(_BASE_PYTHON_RE.fullmatch(interpreter),
                             f"{script} names {interpreter}, not the base interpreter")
        self.assertIsNone(shutil.which("yt-dlp", path=os.environ.get("PATH", "")),
                          "a yt-dlp script is on the image's PATH")
        self.assertNotIn(str(script), DEFAULT_COMMAND)
        self.assertNotIn("yt-dlp", [Path(part).name for part in DEFAULT_COMMAND])

    def test_venv_distributions_are_the_lock(self) -> None:
        # I1. --no-deps installs nothing a package's metadata asks for and the
        # venv is made --without-pip, so it holds the lock and nothing more.
        data, why = _python_json(VENV_PYTHON, _INTERPRETER_PROBE)
        self.assertIsNotNone(data, why)
        assert data is not None
        self.assertEqual(data["distributions"], _locked())
        self.assertEqual(data["pth"], [])

    def test_venv_excludes_the_base_site_packages(self) -> None:
        # I1. The venv sees its own site-packages and the stdlib only, so
        # nothing ever installed in the base prefix can reach yt-dlp either.
        data, why = _python_json(VENV_PYTHON, _INTERPRETER_PROBE)
        self.assertIsNotNone(data, why)
        assert data is not None
        self.assertEqual((data["prefix"], data["base_prefix"]), (str(VENV), BASE_PREFIX))
        self.assertIn(str(VENV_SITE), data["path"])
        self.assertNotIn(str(BASE_SITE), data["path"])
        self.assertIn(str(VENV_SITE), data["sites"])
        self.assertNotIn(str(BASE_SITE), data["sites"])
        try:
            config_text = (VENV / "pyvenv.cfg").read_text(encoding="utf-8")
        except OSError as error:
            self.fail(f"cannot read {VENV / 'pyvenv.cfg'}: {error}")
        config = {}
        for line in config_text.splitlines():
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
        self.assertEqual(config.get("include-system-site-packages"), "false")

    def test_yt_dlp_ejs_is_the_version_yt_dlp_pins(self) -> None:
        # The lock sets both by hand, so a Dependabot bump of one without the
        # other would leave yt-dlp with solver scripts it doesn't expect. Read
        # in the venv, where both are installed.
        data, why = _python_json(VENV_PYTHON, _EJS_PIN_PROBE)
        self.assertIsNotNone(data, why)
        assert data is not None
        pins = []
        for requirement in data["requires"]:
            spec, _, marker = requirement.partition(";")
            if re.search(r"""extra\s*==\s*["']default["']""", marker) and spec.strip().startswith("yt-dlp-ejs"):
                pins.append(spec.strip())
        self.assertEqual(pins, [f"yt-dlp-ejs=={data['ejs']}"])

    def test_both_import_under_isolated_mode(self) -> None:
        proc = _run([VENV_PYTHON, "-I", "-c", "import yt_dlp, yt_dlp_ejs, yt_dlp.version as v; print(v.__version__)"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), _locked()["yt-dlp"])

    def test_yt_dlp_runs_as_a_module(self) -> None:
        # The command the fetcher runs, and the venv's interpreter written out,
        # so a DEFAULT_COMMAND that works only because the base interpreter can
        # import yt-dlp fails the second.
        for command in (list(DEFAULT_COMMAND), [VENV_PYTHON, "-I", "-m", "yt_dlp"]):
            with self.subTest(command=command):
                proc = _run([*command, "--version"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), _locked()["yt-dlp"])

    def test_default_command_is_the_venv_interpreter(self) -> None:
        # I1: what the server really runs (its default runner) is the venv's
        # interpreter in isolated mode, and that interpreter is the venv's.
        self.assertEqual(ytdlp.YTDLP_PYTHON, VENV_PYTHON)
        self.assertEqual(DEFAULT_COMMAND, (VENV_PYTHON, "-I", "-m", "yt_dlp"))
        self.assertEqual(_ytdlp().command, DEFAULT_COMMAND)
        proc = _run([DEFAULT_COMMAND[0], "-I", "-c", "import sys; print(sys.prefix)"])
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, str(VENV)), proc.stderr)

    def test_venv_bytecode_is_current_for_the_venvs_interpreter(self) -> None:
        # The build compiles the venv with the base interpreter under -I -S,
        # so no .pth line runs as root (tests/test_venv_build_steps.py).
        # That must still leave bytecode the venv's interpreter loads: the
        # root filesystem is read-only at run time, so a missing or stale
        # .pyc would make every yt-dlp run compile its modules again.
        data, why = _python_json(VENV_PYTHON, _BYTECODE_PROBE)
        self.assertIsNotNone(data, why)
        assert data is not None
        base, why = _python_json(CMD[0], _TAG_PROBE)
        self.assertIsNotNone(base, why)
        assert base is not None
        # The interpreter that compiled them shares the venv's cache tag and
        # magic number: the same binary.
        self.assertEqual((data["tag"], data["magic"]), (base["tag"], base["magic"]))
        self.assertGreater(data["counts"]["yt_dlp"], 500, data["counts"])
        self.assertGreater(data["counts"]["yt_dlp_ejs"], 0, data["counts"])
        self.assertEqual(data["bad"], [])

    def test_one_yt_dlp_range_plus_headroom_fits_the_download_cap(self) -> None:
        # G9, the owner's decision of 2026-11-03: the forwarder's per-tunnel
        # download cap stays a fixed 16 MiB, and a yt-dlp bump that changes
        # its YouTube range size must fail here, in CI, before it fails
        # fetches. Read with the venv's interpreter, from where it imports.
        data, why = _python_json(VENV_PYTHON, _RANGE_PROBE, CHUNK_SIZE_RE.pattern)
        self.assertIsNotNone(data, why)
        assert data is not None
        path, lines = data["path"], data["lines"]
        self.assertTrue(path.startswith(str(VENV_SITE) + "/"), f"yt_dlp was found outside the venv: {path}")
        self.assertEqual(len(lines), 1, f"expected exactly one CHUNK_SIZE assignment in {path}, found {lines!r}")
        match = CHUNK_SIZE_RE.search(lines[0])
        self.assertIsNotNone(match, lines[0])
        assert match is not None
        try:
            chunk = _int_expression(match["expr"])
        except ValueError as error:
            self.fail(f"{path}: cannot evaluate {lines[0].strip()!r}: {error}")
        self.assertGreater(chunk, 0, f"{path}: {lines[0].strip()!r}")
        try:
            from strange_ringtone_forwarder.policy import DEFAULT_LIMITS
        except ImportError as error:
            self.fail(f"the forwarder's policy is not in the image: {error}")
        cap = DEFAULT_LIMITS.download_bytes_per_tunnel
        self.assertLessEqual(
            chunk + RANGE_HEADROOM_BYTES,
            cap,
            f"yt-dlp's range of {chunk} bytes plus {RANGE_HEADROOM_BYTES} bytes of headroom exceeds the forwarder's "
            f"download_bytes_per_tunnel of {cap}: raise Limits.download_bytes_per_tunnel and the README row together",
        )


class IntExpressionTest(unittest.TestCase):
    """The range-size test's own evaluator: integers only, never eval."""

    def test_evaluates_plain_integer_expressions(self) -> None:
        for text, expected in (("10 << 20", 10485760), ("10485760", 10485760), ("10 * 1024 * 1024", 10485760),
                               ("(1 << 23) + (1 << 21)", 10485760), ("2 ** 20 - 1", 1048575)):
            with self.subTest(text=text):
                self.assertEqual(_int_expression(text), expected)

    def test_refuses_anything_else(self) -> None:
        for text in ("CHUNK", "int('10')", "10.0 * 1024", "True << 20", "-1", "10 // 2", "1 << 1000", "2 ** 99",
                     "__import__('os')", "x.y", "", "10 <<"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _int_expression(text)

    def test_refuses_values_wider_than_64_bits(self) -> None:
        # Copilot's review of 96b7db0: bounding shift counts and exponents
        # alone let nested powers grow without limit ((2 ** 63) ** 64 is
        # already 4,033 bits, and every further ** 64 multiplies that by 64),
        # so a hostile CHUNK_SIZE line could exhaust memory or stall the job.
        # Every constant and every intermediate value must fit in 64 bits:
        # the last case ends at 2 ** 64 - 1 but passes 2 ** 64 on the way.
        for text in ("(2 ** 63) ** 64", "((2 ** 63) ** 64) ** 64", "(1 << 63) * (1 << 63)", "(1 << 63) << 2",
                     "18446744073709551616", "0x100000000000000000", "(1 << 63) + (1 << 63) - 1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _int_expression(text)
        # Acceptance: the widest value within the bound still evaluates.
        self.assertEqual(_int_expression("(1 << 63) + ((1 << 63) - 1)"), (1 << 64) - 1)


class DenoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job_dir = Path(tempfile.mkdtemp(prefix="job-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.job_dir, ignore_errors=True)
        self.fetcher = _ytdlp()

    def _deno(self, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        # The fetcher's prlimit prefix and limits, with Deno in yt-dlp's place.
        command = YtDlp(
            max_seconds=600, max_bytes=40 * 1024 * 1024, timeout_seconds=120, command=(DENO,), prlimit=default_prlimit()
        ).full_command(DENO_RUN)
        return subprocess.run(
            command, input=b"console.log(6 * 7)", capture_output=True, env=env, cwd=self.job_dir, timeout=RUN_SECONDS
        )

    def test_deno_version_runs_as_the_fetcher_user(self) -> None:
        proc = subprocess.run(
            [DENO, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.fetcher.child_env(self.job_dir),
            timeout=RUN_SECONDS,
            check=True,
        )
        self.assertRegex(proc.stdout, r"^deno \d+\.\d+\.\d+")

    def test_deno_runs_yt_dlps_solver_invocation_under_the_fetchers_limits(self) -> None:
        # yt-dlp treats any stderr output from Deno as a failed challenge, so
        # a warning (an unwritable cache, an update notice) would fail every
        # fetch. Under the child's environment and the prlimit set it is silent.
        proc = self._deno(self.fetcher.child_env(self.job_dir))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"42\n", b""))
        # Its caches land under DENO_DIR in the job directory, which the
        # fetcher removes after every run, and nowhere else.
        written = {p.relative_to(self.job_dir).parts[0] for p in self.job_dir.rglob("*")}
        self.assertLessEqual(written, {"deno"})

    def test_deno_with_an_unwritable_cache_still_runs_silently(self) -> None:
        # If DENO_DIR ever pointed at the read-only root, Deno must keep
        # working in memory rather than print an error yt-dlp would fail on.
        env = self.fetcher.child_env(self.job_dir)
        env["DENO_DIR"] = "/app/deno"
        proc = self._deno(env)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"42\n", b""))
        self.assertFalse(os.path.exists("/app/deno"))

    def test_yt_dlp_finds_deno_and_the_solver_scripts(self) -> None:
        # yt-dlp's verbose header, from the fetcher's command and environment:
        # the JS runtime at the fixed path, the yt-dlp-ejs package, and only
        # the urllib request handler (no requests: http:// proxies only, as
        # the README says). Without a URL it stops after the header. The
        # yt-dlp-ejs version comes from the venv, the only place it is
        # installed.
        ejs_version = _venv_version("yt-dlp-ejs")
        self.assertIsNotNone(ejs_version, f"{VENV_PYTHON} can't report yt-dlp-ejs's version")
        assert ejs_version is not None
        proc = subprocess.run(
            [*DEFAULT_COMMAND, "--ignore-config", "--no-cache-dir", "--js-runtimes", f"deno:{DENO}", "-v"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.fetcher.child_env(self.job_dir),
            cwd=self.job_dir,
            timeout=RUN_SECONDS,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertRegex(proc.stderr, r"\[debug\] JS runtimes: deno-\d+\.\d+\.\d+\n")
        ejs = re.escape(ejs_version)
        self.assertRegex(proc.stderr, rf"\[debug\] Optional libraries: .*\byt_dlp_ejs-{ejs}\b")
        self.assertIn("[debug] Request Handlers: urllib\n", proc.stderr)
        self.assertIn("[debug] Plugin directories: none\n", proc.stderr)


class RealYtDlpOfflineTest(unittest.TestCase):
    """The pinned yt-dlp accepts the fetcher's exact argument list (docs/PLAN.md 6).

    A rejected option exits 2 with a usage message; with no network the run
    instead gets as far as resolving YouTube's host name, fails there, exits 1
    and writes no audio.
    """

    def setUp(self) -> None:
        # Never let this run where it could reach YouTube.
        self.assertEqual(_interfaces(), {"lo"}, "refusing to run yt-dlp with a network")
        self.job_dir = Path(tempfile.mkdtemp(prefix="job-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.job_dir, ignore_errors=True)
        self.fetcher = _ytdlp()

    def test_the_argument_list_is_accepted(self) -> None:
        command = self.fetcher.full_command(ytdlp_args(OFFLINE_VIDEO_ID, self.job_dir, 600, 40 * 1024 * 1024))
        self.assertEqual(command[0], PRLIMIT)
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=self.fetcher.child_env(self.job_dir),
            cwd=self.job_dir,
            timeout=RUN_SECONDS,
            start_new_session=True,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("no such option", proc.stderr)
        self.assertRegex(proc.stderr, r"(?i)resolution|resolve|getaddrinfo|name or service")
        self.assertFalse((self.job_dir / f"{OFFLINE_VIDEO_ID}.m4a").exists())

    def test_the_fetcher_reports_failed(self) -> None:
        with self.assertRaises(FetchError) as caught:
            self.fetcher.fetch(OFFLINE_VIDEO_ID, self.job_dir)
        self.assertEqual(caught.exception.code, "failed")
        self.assertEqual(ytdlp.map_failure(1, b"Temporary failure in name resolution"), "failed")


class NetworkTest(unittest.TestCase):
    # The CI run has no network; in a deployment the fetcher has egress, and
    # restricting it to YouTube is the deployment's job (README).
    def test_only_loopback_exists(self) -> None:
        self.assertEqual(_interfaces(), {"lo"})

    def test_no_outbound_connection(self) -> None:
        # 192.0.2.1 is TEST-NET-1 (RFC 5737): nothing answers there anyway.
        with self.assertRaises(OSError):
            socket.create_connection(("192.0.2.1", 80), timeout=1).close()

    def test_no_name_resolution(self) -> None:
        with self.assertRaises(OSError):
            socket.getaddrinfo("example.internal", 80)


def _reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


class ServerTest(unittest.TestCase):
    def test_cmd_serves_healthz_and_stops_on_sigterm(self) -> None:
        proc = subprocess.Popen(
            CMD,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        self.addCleanup(_reap, proc)
        status, headers, body = self._get_healthz(proc)
        self.assertEqual(status, 200)
        # Says only that the server answers.
        self.assertEqual(body, b"ok")
        self.assertEqual(headers.get("cache-control"), "no-store")
        server = _status_fields(proc.pid)
        self.assertEqual(server["Uid"].split(), [str(UID)] * 4)
        self.assertEqual(server["Gid"].split(), [str(GID)] * 4)
        # The scratch directory is the server's own, under the /tmp tmpfs.
        scratch = os.lstat("/tmp/strange_ringtone_fetch")
        self.assertEqual((scratch.st_uid, scratch.st_mode & 0o777), (UID, 0o700))
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        # S2's acceptance on a real kernel: under --network none there is no
        # default route, so the start check passes without the override.
        self.assertNotIn(b"refusing to start", err)

    def _get_healthz(self, proc: subprocess.Popen[bytes]) -> tuple[int, dict[str, str], bytes]:
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if proc.poll() is not None:
                self.fail(f"server exited {proc.returncode}: {proc.stderr.read() if proc.stderr else b''!r}")
            # http.client, not urllib: urllib would honour proxy variables.
            connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
            try:
                connection.request("GET", "/fetch/healthz")
                response = connection.getresponse()
                headers = {name.lower(): value for name, value in response.getheaders()}
                return response.status, headers, response.read()
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    self.fail("server did not start listening")
                time.sleep(0.1)
            finally:
                connection.close()


class ForwarderTest(unittest.TestCase):
    """The image's second command (docs/PLAN.md 6.1): the CONNECT gate, started as compose starts it.

    Every request here is one the forwarder refuses before any DNS lookup
    (G1, G2, G3; G4 proves the ordering in the unit tests), so nothing is
    resolved or connected even if this ever ran with a network.
    """

    def _wait_for_port(self, proc: subprocess.Popen[bytes]) -> None:
        # A connection closed before any byte is reported to the sink as
        # refused:empty and never logged, so this probe leaves no log line.
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if proc.poll() is not None:
                _out, err = proc.communicate(timeout=10)
                self.fail(f"forwarder exited {proc.returncode}: {err!r}")
            try:
                socket.create_connection(("127.0.0.1", FORWARDER_PORT), timeout=2).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    self.fail("forwarder did not start listening")
                time.sleep(0.1)

    @staticmethod
    def _status_line(request: bytes) -> bytes:
        """The first line the forwarder answers request with, on a fresh connection."""
        with socket.create_connection(("127.0.0.1", FORWARDER_PORT), timeout=5) as client:
            client.sendall(request)
            response = b""
            while b"\r\n" not in response and len(response) < 4096:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        return response.split(b"\r\n", 1)[0]

    def test_forwarder_entry_serves_refusals_without_dns_and_stops_on_sigterm(self) -> None:
        proc = subprocess.Popen(
            [*FORWARDER_CMD, "--bind", "127.0.0.1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        self.addCleanup(_reap, proc)
        self._wait_for_port(proc)
        for request, status in (
            # G2: an IP literal, whatever the Host header says.
            (b"CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n", b"HTTP/1.1 400 "),
            # G1: anything but CONNECT, plain-HTTP proxying included.
            (b"GET / HTTP/1.1\r\n\r\n", b"HTTP/1.1 405 "),
            # G3: a well-formed name that is not on the allowlist.
            (b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n", b"HTTP/1.1 403 "),
        ):
            with self.subTest(request=request):
                try:
                    line = self._status_line(request)
                except OSError as error:
                    self.fail(f"no answer to {request!r}: {error}")
                self.assertTrue(line.startswith(status), line)
        forwarder = _status_fields(proc.pid)
        self.assertEqual(forwarder["Uid"].split(), [str(UID)] * 4)
        self.assertEqual(forwarder["Gid"].split(), [str(GID)] * 4)
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        lines = err.decode("utf-8", "replace").splitlines()
        # G10: name and reason only; a name that never parsed is "-".
        for expected in (
            "forwarder refused literal 192.0.2.1",
            "forwarder refused method -",
            "forwarder refused unlisted example.invalid",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, lines)
        # No header text a client sent ever reaches the log.
        self.assertNotIn(b"Host:", err)

    def test_forwarder_process_loads_only_its_own_package(self) -> None:
        # The entry is a script beside its package, so its process loads the
        # forwarder's own modules and the stdlib only: neither the core nor
        # the fetcher package, though both sit in /app.
        data, why = _python_json(FORWARDER_CMD[0], _FORWARDER_MODULES_PROBE)
        self.assertIsNotNone(data, why)
        self.assertEqual(data, [
            "strange_ringtone_forwarder",
            "strange_ringtone_forwarder.__main__",
            "strange_ringtone_forwarder.policy",
            "strange_ringtone_forwarder.server",
        ])

    def test_forwarder_files_ship_precompiled(self) -> None:
        # The root filesystem is read-only at run time and -I ignores
        # PYTHONDONTWRITEBYTECODE, so the bytecode must be in the image.
        try:
            modules = {path.name for path in FORWARDER_PACKAGE.glob("*.py")}
            cached = {path.name for path in (FORWARDER_PACKAGE / "__pycache__").glob("*.pyc")}
        except OSError as error:
            self.fail(f"cannot list {FORWARDER_PACKAGE}: {error}")
        self.assertEqual(modules, FORWARDER_MODULES)
        tag = sys.implementation.cache_tag
        self.assertLessEqual({f"{Path(name).stem}.{tag}.pyc" for name in modules}, cached)


class RunnerTest(unittest.TestCase):
    """The image's third command (docs/PLAN.md 6.1): yt-dlp's container, started as compose starts it.

    The one request that starts a run asks for a format-valid ID with the
    container proven to have no network, so the real yt-dlp the runner starts
    can only fail at name resolution.
    """

    def _exchange(
        self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None,
        timeout: float = 5,
    ) -> tuple[int, dict[str, str], bytes]:
        # http.client, not urllib: urllib would honour proxy variables.
        connection = http.client.HTTPConnection("127.0.0.1", RUNNER_PORT, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            try:
                received = {name.lower(): value for name, value in response.getheaders()}
                # The body and then the runner's close, through the response's
                # own reader (response.read() would close it at the declared
                # length). The runner lets go of its one-job lock just before
                # it closes the connection, so a next request sent at the
                # last byte could find it busy; the fetcher's client waits
                # for the close too (strange_ringtone_fetch.runner_client).
                fp = response.fp
                if fp is None:
                    return response.status, received, b""
                length = received.get("content-length", "")
                data = fp.read(int(length)) if length.isdigit() else fp.read()
                while fp.read(65536):
                    pass
                return response.status, received, data
            finally:
                response.close()
        finally:
            connection.close()

    def _wait_for_healthz(self, proc: subprocess.Popen[bytes]) -> tuple[int, dict[str, str], bytes]:
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if proc.poll() is not None:
                _out, err = proc.communicate(timeout=10)
                self.fail(f"runner exited {proc.returncode}: {err!r}")
            try:
                return self._exchange("GET", RUNNER_HEALTH_PATH, timeout=2)
            except ConnectionRefusedError:
                if time.monotonic() > deadline:
                    self.fail("runner did not start listening")
                time.sleep(0.1)

    def test_runner_command_serves_healthz_fails_a_real_fetch_offline_and_leaves_nothing(self) -> None:
        # S2 (acceptance on a real kernel) and R3. Never let the real yt-dlp
        # run where it could reach YouTube.
        self.assertEqual(_interfaces(), {"lo"}, "refusing to run the runner with a network")
        env = dict(os.environ)
        # No override: under --network none there is no default route, so
        # the start check must pass on its own.
        env.pop("FETCH_ALLOW_DEFAULT_ROUTE", None)
        # The unittest process is this container's PID 1, so the runner is
        # not and can't sweep: the override lets it serve without the sweep
        # (Q4), which RealSweepTest proves on its own.
        env["FETCH_ALLOW_NO_SWEEP"] = "true"
        env["FETCH_TIMEOUT_SECONDS"] = str(RUN_SECONDS)
        proc = subprocess.Popen(
            [*RUNNER_CMD, "--bind", "127.0.0.1"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.addCleanup(_reap, proc)
        status, headers, body = self._wait_for_healthz(proc)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(headers.get("cache-control"), "no-store")
        runner = _status_fields(proc.pid)
        self.assertEqual(runner["Uid"].split(), [str(UID)] * 4)
        self.assertEqual(runner["Gid"].split(), [str(GID)] * 4)
        # The scratch directory is the runner's own, under the /tmp tmpfs.
        scratch = os.lstat(RUNNER_SCRATCH)
        self.assertTrue(stat.S_ISDIR(scratch.st_mode), f"{RUNNER_SCRATCH} is not a directory")
        self.assertEqual((scratch.st_uid, scratch.st_mode & 0o777), (UID, 0o700))

        # A real run through the runner: the fetcher's request, the real
        # argument list and environment, and yt-dlp failing at name
        # resolution, which the runner answers as `failed`.
        token = secrets.token_hex(16)
        self.assertRegex(token, _TOKEN_RE)
        request = json.dumps({"video_id": OFFLINE_VIDEO_ID, "job": token}).encode("ascii")
        try:
            status, headers, body = self._exchange(
                "POST", "/jobs", request, {"Content-Type": "application/json"}, timeout=RUN_SECONDS + 30,
            )
        except OSError as error:
            self.fail(f"the runner gave no answer to a job: {error}")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers.get("content-type"), "application/json")
        self.assertEqual(json.loads(body), {"status": "failed", "error": "failed"})
        # Nothing to collect: the failed run left no audio.
        status, _headers, _body = self._exchange("GET", f"/jobs/{token}/audio")
        self.assertEqual(status, 404)
        # The job directory is gone and no result waits: only the empty
        # results directory remains.
        self.assertEqual(sorted(os.listdir(RUNNER_SCRATCH)), ["results"])
        self.assertEqual(os.listdir(os.path.join(RUNNER_SCRATCH, "results")), [])

        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        log = err.decode("utf-8", "replace")
        self.assertNotIn("refusing to start", log)
        self.assertNotIn("FETCH_ALLOW_DEFAULT_ROUTE is set", log)
        # Not PID 1 here: it served only because of the override, and said so.
        self.assertIn("strange_ringtone_runner: FETCH_ALLOW_NO_SWEEP is set and the sweep can't run here", log)
        # Control: the job's request line is there, so the absences below
        # are read from a log that holds the run (R4's format: method, path,
        # status, duration).
        self.assertRegex(log, r"(?m)^POST /jobs 200 ")
        # Nothing of yt-dlp's output, the video ID or the token.
        self.assertNotIn("youtube", log.lower())
        self.assertNotIn(OFFLINE_VIDEO_ID, log)
        self.assertNotIn(token, log)

    def test_runner_process_loads_only_the_allowed_modules(self) -> None:
        # The entry is a script beside the packages, so its process loads the
        # runner's package, the three fetcher modules it shares and the two
        # core modules they need: never the fetcher's server, jobs or client,
        # and never yt-dlp, which runs only as the venv's subprocess.
        data, why = _python_json(RUNNER_CMD[0], _RUNNER_MODULES_PROBE, RUNNER_CMD[2])
        self.assertIsNotNone(data, why)
        assert data is not None
        self.assertEqual(data["modules"], [
            "strange_ringtone",
            "strange_ringtone.config",
            "strange_ringtone.http_guard",
            "strange_ringtone_fetch",
            "strange_ringtone_fetch.config",
            "strange_ringtone_fetch.metadata",
            "strange_ringtone_fetch.routes",
            "strange_ringtone_runner",
            "strange_ringtone_runner.__main__",
            "strange_ringtone_runner.config",
            "strange_ringtone_runner.server",
            "strange_ringtone_runner.ytdlp",
        ])
        self.assertEqual(data["yt_dlp"], [])

    def test_runner_files_ship_precompiled(self) -> None:
        # The root filesystem is read-only at run time and -I ignores
        # PYTHONDONTWRITEBYTECODE, so the bytecode must be in the image.
        try:
            modules = {path.name for path in RUNNER_PACKAGE.glob("*.py")}
            cached = {path.name for path in (RUNNER_PACKAGE / "__pycache__").glob("*.pyc")}
        except OSError as error:
            self.fail(f"cannot list {RUNNER_PACKAGE}: {error}")
        self.assertEqual(modules, RUNNER_MODULES)
        tag = sys.implementation.cache_tag
        self.assertLessEqual({f"{Path(name).stem}.{tag}.pyc" for name in modules}, cached)

    def test_runner_and_fetcher_scratch_directories_differ(self) -> None:
        # Each server empties its own scratch directory at startup, so the
        # two must never share one, nor nest one in the other.
        try:
            from strange_ringtone_fetch.config import SCRATCH_DIR as fetcher_scratch
            from strange_ringtone_runner.config import SCRATCH_DIR as runner_scratch
            from strange_ringtone_runner.config import Settings as RunnerSettings
        except ImportError as error:
            self.fail(f"the runner's configuration does not import in the image: {error}")
        self.assertEqual(str(runner_scratch), RUNNER_SCRATCH)
        self.assertEqual(str(fetcher_scratch), FETCHER_SCRATCH)
        self.assertEqual(RunnerSettings().scratch_dir, runner_scratch)
        self.assertFalse(runner_scratch.is_relative_to(fetcher_scratch), (runner_scratch, fetcher_scratch))
        self.assertFalse(fetcher_scratch.is_relative_to(runner_scratch), (runner_scratch, fetcher_scratch))


if __name__ == "__main__":
    unittest.main()
