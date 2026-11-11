"""The container contract, checked from inside the running image (docs/PLAN.md 5.7).

The test run uses the contract's own flags (README, Development): --network
none, --read-only, a /tmp tmpfs, --cap-drop ALL, no-new-privileges, --ipc none
and --ulimit msgqueue=0. These tests prove the image works under them and that
they hold: a fixed non-root user, no writable path but /tmp, no network, and a
server that starts with the image's CMD and answers /healthz.
"""
from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

UID = GID = 10001
PORT = 8080
# The image's CMD (core/Dockerfile). A test can't read it from inside the
# container, so a change there must change this line too.
CMD = ["python", "-I", "/app/core"]
FFMPEG = "/usr/local/bin/ffmpeg"
PRLIMIT = "/usr/bin/prlimit"
STARTUP_SECONDS = 15.0


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

    def test_prlimit_can_lower_limits_without_privileges(self) -> None:
        # The renderer wraps ffmpeg in prlimit (docs/PLAN.md 5.3). Lowering a
        # limit needs no capability, so it must work under cap-drop ALL.
        self.assertTrue(os.access(PRLIMIT, os.X_OK))
        proc = subprocess.run(
            [
                PRLIMIT, "--as=536870912", "--cpu=20", "--fsize=4194304", "--nofile=64", "--",
                "python", "-I", "-c",
                "import resource as r; print(*(r.getrlimit(k) for k in "
                "(r.RLIMIT_AS, r.RLIMIT_CPU, r.RLIMIT_FSIZE, r.RLIMIT_NOFILE)))",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(
            proc.stdout.split(),
            ["(536870912,", "536870912)", "(20,", "20)", "(4194304,", "4194304)", "(64,", "64)"],
        )


class FilesystemTest(unittest.TestCase):
    def test_nothing_outside_tmp_is_writable(self) -> None:
        for directory in (
            "/", "/app", "/app/core", "/app/core/strange_ringtone", "/app/core/static", "/app/ffmpeg",
            "/usr/local/bin", "/usr/local/lib", "/etc", "/var/tmp", "/root", "/home", "/run",
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
            FFMPEG, "/app/core/__main__.py", "/app/core/strange_ringtone/server.py", "/app/ffmpeg/components.txt",
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
        # noexec: nothing written under /tmp, such as an upload, can be run.
        self.assertLessEqual({"rw", "noexec", "nosuid"}, options)
        self.assertIn("ro", mounts["/"][1])


class NetworkTest(unittest.TestCase):
    def test_only_loopback_exists(self) -> None:
        with open("/proc/net/dev", encoding="ascii") as handle:
            interfaces = {line.split(":", 1)[0].strip() for line in handle if ":" in line}
        self.assertEqual(interfaces, {"lo"})

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
        status, body = self._get_healthz(proc)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        server = _status_fields(proc.pid)
        self.assertEqual(server["Uid"].split(), [str(UID)] * 4)
        self.assertEqual(server["Gid"].split(), [str(GID)] * 4)
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)

    def _get_healthz(self, proc: subprocess.Popen[bytes]) -> tuple[int, bytes]:
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if proc.poll() is not None:
                self.fail(f"server exited {proc.returncode}: {proc.stderr.read() if proc.stderr else b''!r}")
            # http.client, not urllib: urllib would honour proxy variables.
            connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
            try:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                return response.status, response.read()
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
