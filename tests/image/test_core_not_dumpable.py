"""The image's server, started by its CMD, is non-dumpable and still renders (docs/PLAN.md 8).

tests/test_core_not_dumpable.py checks the helper; here the real server
process is probed as an exploited ffmpeg would see it: a process of the same
UID in the same container. It must not read the server's
/proc/<pid>/environ or maps, nor attach to it with ptrace. A plain process
started the same way is the control, so a probe that can't read anything
here proves nothing. The render runs through the non-dumpable server, so
prlimit and ffmpeg, which execve resets to dumpable, are shown to still work.
"""
from __future__ import annotations

import errno
import http.client
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from corpus.build import valid_wav

# The image's CMD (core/Dockerfile), as tests/image/test_container.py has it.
CMD = ["python", "-I", "/app/core"]
PORT = 8080
STARTUP_SECONDS = 15.0


def _ptrace_scope() -> int:
    """Yama's ptrace_scope, or 0 where Yama isn't there (the kernel's own rules alone)."""
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope", encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return 0


# The probing parent: it starts the target (the server's CMD, or a plain
# Python as the control), waits until it is ready, then tries what a same-UID
# process could do to it. Being the target's parent, it may ptrace a
# dumpable target under ptrace_scope 1 too, which is what the control needs.
_PROBER = r'''
import ctypes, errno, http.client, json, os, signal, subprocess, sys, time
mode, port, startup = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
env = {"PATH": os.environ.get("PATH", ""), "PUBLIC_ORIGIN": os.environ.get("PUBLIC_ORIGIN", ""), "MARKER": "x"}
if mode == "server":
    command = ["python", "-I", "/app/core"]
    stdout = subprocess.DEVNULL
else:
    # The control says when it runs. Popen returns as soon as the exec has
    # begun, and until the kernel has set the new program up its
    # /proc/<pid>/environ reads empty, which would look like a failed read.
    control = "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"
    command = ["python", "-I", "-c", control]
    stdout = subprocess.PIPE
target = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.DEVNULL, env=env)
result = {}
try:
    if mode == "server":
        deadline = time.monotonic() + startup
        while True:
            if target.poll() is not None:
                result["ready"] = f"exited {target.returncode}"
                break
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", "/healthz")
                if connection.getresponse().status == 200:
                    result["ready"] = "ready"
                    break
            except OSError:
                pass
            finally:
                connection.close()
            if time.monotonic() > deadline:
                result["ready"] = "never answered"
                break
            time.sleep(0.1)
    else:
        result["ready"] = target.stdout.readline().decode("ascii", "replace").strip()
    for name in ("environ", "maps"):
        try:
            with open(f"/proc/{target.pid}/{name}", "rb") as handle:
                result[name] = "read" if handle.read() else "empty"
        except OSError as error:
            result[name] = errno.errorcode.get(error.errno, str(error.errno))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    libc.ptrace.restype = ctypes.c_long
    # PTRACE_ATTACH (16): the attach is the permission check.
    if libc.ptrace(16, target.pid, None, None) == 0:
        result["ptrace"] = "attached"
    else:
        result["ptrace"] = errno.errorcode.get(ctypes.get_errno(), "?")
finally:
    # SIGKILL ends a ptrace-stopped tracee too, and the server with it. Sent
    # and reaped here, not by Popen, whose kill() polls first and would take
    # the ptrace stop for an exit, and then send nothing.
    try:
        os.kill(target.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    while True:
        _pid, status = os.waitpid(target.pid, 0)
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            break
    target.returncode = -9
    target.stdin.close()
    if target.stdout is not None:
        target.stdout.close()
print(json.dumps(result))
'''


class ServerNotDumpableTest(unittest.TestCase):
    def probe(self, mode: str) -> dict[str, str]:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _PROBER, mode, str(PORT), str(STARTUP_SECONDS)],
            env=dict(os.environ), capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result: dict[str, str] = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(result.get("ready"), "ready", result)
        return result

    def test_a_same_uid_process_can_neither_read_nor_attach_to_the_server(self) -> None:
        # Control: a plain process of the same UID is open to its parent.
        plain = self.probe("plain")
        self.assertEqual(plain["environ"], "read", plain)
        self.assertEqual(plain["maps"], "read", plain)
        if _ptrace_scope() <= 1:
            self.assertEqual(plain["ptrace"], "attached", plain)

        server = self.probe("server")
        self.assertIn(server["environ"], ("EACCES", "EPERM"), server)
        self.assertIn(server["maps"], ("EACCES", "EPERM"), server)
        self.assertEqual(server["ptrace"], "EPERM", server)
        print(f"\nServerNotDumpableTest: ptrace_scope {_ptrace_scope()}, control {plain}, server {server}",
              file=sys.stderr)


class RenderWhileNotDumpableTest(unittest.TestCase):
    def test_the_non_dumpable_server_renders(self) -> None:
        origin = os.environ.get("PUBLIC_ORIGIN", "")
        self.assertTrue(origin, "the in-image run sets PUBLIC_ORIGIN")
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            body = valid_wav(Path(temp) / "clip.wav", 10.0).read_bytes()
        proc = subprocess.Popen(CMD, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                env=dict(os.environ))
        self.addCleanup(self._reap, proc)
        self._wait_for_healthz(proc)
        status, data = self._request("POST", "/api/render", body,
                                     {"Origin": origin, "Content-Type": "audio/wav"})
        self.assertEqual(status, 200, data)
        payload = json.loads(data)
        status, tone = self._request("GET", f"/api/result/{payload['result_id']}.m4a")
        self.assertEqual(status, 200)
        self.assertEqual(tone[4:8], b"ftyp")
        # The server that rendered is the non-dumpable one.
        with self.assertRaises(OSError) as raised:
            with open(f"/proc/{proc.pid}/environ", "rb") as handle:
                handle.read()
        self.assertIn(raised.exception.errno, (errno.EACCES, errno.EPERM))
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        # http.client, not urllib: urllib would honour proxy variables.
        connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=120)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def _wait_for_healthz(self, proc: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if proc.poll() is not None:
                self.fail(f"server exited {proc.returncode}: {proc.stderr.read() if proc.stderr else b''!r}")
            try:
                if self._request("GET", "/healthz")[0] == 200:
                    return
            except ConnectionRefusedError:
                pass
            if time.monotonic() > deadline:
                self.fail("server did not start listening")
            time.sleep(0.1)

    @staticmethod
    def _reap(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
