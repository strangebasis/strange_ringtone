"""The fetcher image's contract, checked from inside the running image (docs/PLAN.md 6 and 9.2).

The test run uses the contract's own flags (README, Development): --network
none, --read-only, a /tmp tmpfs, --cap-drop ALL, no-new-privileges, --ipc none
and --ulimit msgqueue=0. These tests prove the image works under them and that
they hold: a fixed non-root user, no writable path but /tmp, exactly the
locked yt-dlp and yt-dlp-ejs, a Deno that yt-dlp finds and that runs under the
fetcher's resource limits, and a server that starts with the image's CMD and
answers /fetch/healthz.

Nothing here talks to YouTube. The one test that starts the real yt-dlp with
the fetcher's real argument list first proves the container has no network,
so the run can only fail at name resolution.
"""
from __future__ import annotations

import http.client
import importlib.metadata
import os
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from strange_ringtone_fetch import ytdlp
from strange_ringtone_fetch.ytdlp import DEFAULT_COMMAND, DENO, PRLIMIT, FetchError, YtDlp, default_prlimit, ytdlp_args

UID = GID = 10001
PORT = 8080
# The image's CMD (fetcher/Dockerfile). A test can't read it from inside the
# container, so a change there must change this line too.
CMD = ["python", "-I", "/app/fetcher"]
LOCK = Path("/app/fetcher/requirements.txt")
SITE_PACKAGES = Path("/usr/local/lib/python3.12/site-packages")
# The core modules the fetcher imports, and nothing else of the core
# (fetcher/Dockerfile, fetcher/__main__.py).
CORE_FILES = {"__init__.py", "config.py", "http_guard.py"}
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


def _fetcher() -> YtDlp:
    # The server's own construction (strange_ringtone_fetch.server) with the
    # default settings: the real command, prlimit and limits.
    return YtDlp(max_seconds=600, max_bytes=40 * 1024 * 1024, timeout_seconds=120, prlimit=default_prlimit())


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

    def test_the_fetcher_uses_prlimit_here(self) -> None:
        # On Linux the fetcher never runs yt-dlp without its limits.
        self.assertEqual(default_prlimit(), (PRLIMIT,))


class FilesystemTest(unittest.TestCase):
    def test_nothing_outside_tmp_is_writable(self) -> None:
        for directory in (
            "/", "/app", "/app/core", "/app/core/strange_ringtone", "/app/fetcher",
            "/app/fetcher/strange_ringtone_fetch", str(SITE_PACKAGES), str(SITE_PACKAGES / "yt_dlp"),
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
            DENO, str(LOCK), "/app/fetcher/__main__.py", "/app/fetcher/strange_ringtone_fetch/ytdlp.py",
            "/app/core/strange_ringtone/http_guard.py", str(SITE_PACKAGES / "yt_dlp" / "__init__.py"),
        ):
            with self.subTest(path=path), self.assertRaises(OSError):
                open(path, "ab").close()

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
    def test_lock_holds_exactly_yt_dlp_and_yt_dlp_ejs(self) -> None:
        self.assertEqual(set(_locked()), {"yt-dlp", "yt-dlp-ejs"})

    def test_installed_distributions_are_the_lock(self) -> None:
        # --no-deps installs nothing a package's metadata asks for, so the
        # image holds the lock and the base image's pip, nothing more.
        installed = {dist.metadata["Name"].lower(): dist.version for dist in importlib.metadata.distributions()}
        installed.pop("pip", None)
        self.assertEqual(installed, _locked())

    def test_yt_dlp_ejs_is_the_version_yt_dlp_pins(self) -> None:
        # The lock sets both by hand, so a Dependabot bump of one without the
        # other would leave yt-dlp with solver scripts it doesn't expect.
        ejs = importlib.metadata.version("yt-dlp-ejs")
        pins = []
        for requirement in importlib.metadata.requires("yt-dlp") or []:
            spec, _, marker = requirement.partition(";")
            if re.search(r"""extra\s*==\s*["']default["']""", marker) and spec.strip().startswith("yt-dlp-ejs"):
                pins.append(spec.strip())
        self.assertEqual(pins, [f"yt-dlp-ejs=={ejs}"])

    def test_both_import_under_isolated_mode(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", "import yt_dlp, yt_dlp_ejs, yt_dlp.version as v; print(v.__version__)"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=RUN_SECONDS,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), _locked()["yt-dlp"])

    def test_yt_dlp_runs_as_a_module(self) -> None:
        proc = subprocess.run(
            [*DEFAULT_COMMAND, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=RUN_SECONDS,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), _locked()["yt-dlp"])


class DenoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job_dir = Path(tempfile.mkdtemp(prefix="job-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.job_dir, ignore_errors=True)
        self.fetcher = _fetcher()

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
        # the README says). Without a URL it stops after the header.
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
        ejs = re.escape(importlib.metadata.version("yt-dlp-ejs"))
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
        self.fetcher = _fetcher()

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


class ServerTest(unittest.TestCase):
    def test_cmd_serves_healthz_and_stops_on_sigterm(self) -> None:
        proc = subprocess.Popen(
            CMD,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        self.addCleanup(self._reap, proc)
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

    @staticmethod
    def _reap(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
