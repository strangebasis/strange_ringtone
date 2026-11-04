"""The compose layout of the fetcher and its forwarder, brought up for real (docs/PLAN.md 6.1, T1 to T3).

compose.example.yaml itself is the system under test: the fetcher on internal
networks only (`fetcher`, `egress`), the forwarder alone on `outside`, and the
fetcher's HTTPS_PROXY pointing at the forwarder. The tests start it with the
already built fetcher image under a unique project name, through a temporary
override that never builds, never pulls and never reads the developer's .env;
they publish no port and tear everything down afterwards.

Nothing here sends a byte to YouTube or any other external host. T3's names
are refused before the forwarder resolves anything; T1's external-name probe
runs only where no default route exists, on internal networks whose embedded
DNS answers without forwarding (EngineTest fails where an older engine would
forward); T2's reachability probes connect to container addresses, never to a
name on the `outside` network. Every `docker run` passes `--pull never`, so a
missing image is an error rather than a registry request.

Static checks (LayoutTest) read `docker compose config` and so still report
the layout when the stack can't start; a missing service or network is a
failed assertion, never an error in setUpClass.
"""
from __future__ import annotations

import builtins
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.example.yaml"
# The tag compose.example.yaml builds; CI builds its own and names it here.
IMAGE = os.environ.get("STRANGE_RINGTONE_COMPOSE_IMAGE", "strange_ringtone-fetcher:local")
FORWARDER_PORT = 3128
FETCHER_PORT = 8080
ORIGIN = "https://example.internal"
PROBE_TIMEOUT = 3
# Reserved (RFC 2606): it resolves nowhere, so even a misconfigured layout
# asks no real name of anybody's DNS.
EXTERNAL_NAME = "example.invalid"
EXIT_ROUTE = 3
COMPOSE_TIMEOUT = 120
PROBE_RUN_TIMEOUT = 60
OVERRIDE_HEALTH_SECONDS = 15.0
LOG_WAIT_SECONDS = 5.0
UID_GID = "10001:10001"
FORWARDER_COMMAND = ["python", "-I", "/app/fetcher/forwarder.py"]
# The image's CMD (fetcher/Dockerfile): the base interpreter, isolated. Not the
# venv's interpreter, which holds yt-dlp (docs/PLAN.md 6.1, I1).
FETCHER_COMMAND = ["python", "-I", "/app/fetcher"]
# The engine release that added the bridge driver's `isolated` gateway mode,
# which the internal networks use so the Docker host has no address on them:
# Docker Engine 28.0.0 (release notes, moby/moby#49262), which accepts it on
# an internal network only. It also has the CVE-2024-29018 DNS fix of 26.0.
ENGINE_FLOOR = (28, 0)
GATEWAY_MODE = "com.docker.network.bridge.gateway_mode_ipv4"
INTERNAL_NETWORKS = ("core", "fetcher", "egress")
# Every service's log rotates, so a flood of refusals can't fill the host's disk.
LOGGING = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
PROXY_URL = f"http://fetcher-forwarder:{FORWARDER_PORT}"

# The flags the contract runs a container with (README, Deployment contract),
# for the containers the tests start outside compose.
HARDENING = [
    "--user", UID_GID,
    "--read-only",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges:true",
]
FETCHER_TMPFS = ["--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,mode=1777"]

# T3: each request goes to the running forwarder on a fresh connection, and
# each must be refused at the gate, before any DNS lookup: an unlisted name, a
# lookalike and a suffix trick (403 unlisted), an IP literal (400), port 80
# (403) and plain-HTTP proxying (405). The Host headers and the watch?v= path
# are there so the log test can prove such text never reaches the log. The
# last request gives the `method -` line a request whose target has no host.
T3_REQUESTS = [
    b"CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid:443\r\n\r\n",
    b"CONNECT notyoutube.com:443 HTTP/1.1\r\nHost: notyoutube.com:443\r\n\r\n",
    b"CONNECT youtube.com.example.invalid:443 HTTP/1.1\r\nHost: youtube.com.example.invalid:443\r\n\r\n",
    b"CONNECT 192.0.2.1:443 HTTP/1.1\r\nHost: 192.0.2.1:443\r\n\r\n",
    b"CONNECT www.youtube.com:80 HTTP/1.1\r\nHost: www.youtube.com:80\r\n\r\n",
    b"GET http://www.youtube.com/watch?v=AAAAAAAAAAA HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n",
    b"GET / HTTP/1.1\r\n\r\n",
]
T3_STATUSES = [403, 403, 403, 400, 403, 405, 405]
T3_LOG_LINES = [
    "forwarder refused unlisted example.invalid",
    "forwarder refused unlisted notyoutube.com",
    "forwarder refused unlisted youtube.com.example.invalid",
    "forwarder refused literal 192.0.2.1",
    "forwarder refused port www.youtube.com",
    "forwarder refused method -",
]
# G10's whole log format: a kind, a token, and a name that matched the
# hostname grammar or a dash. Anything else on a `forwarder ` line is a leak.
FORWARDER_LOG_RE = re.compile(r"^forwarder (refused|cut) [a-z_]+ (-|[A-Za-z0-9.-]{1,253})$")
LEAK_MARKERS = ("watch?v=", "Host:", "http://", "HTTP/1.1")

# Probes run with `python -I -c` inside containers. Raw strings: the \r\n in
# them is Python source for the probe, not a line break in the argument.
# The default-route probe resolves names only when the container has no
# default route, so a broken layout fails on the route and asks no DNS.
_ROUTES_PROBE = r"""
import json, socket
with open('/proc/net/route', encoding='ascii') as handle:
    lines = handle.read().splitlines()
if not lines or not lines[0].startswith('Iface'):
    raise SystemExit('unexpected /proc/net/route header')
v4 = []
for line in lines[1:]:
    fields = line.split()
    if not fields:
        continue
    if len(fields) < 11:
        raise SystemExit('unexpected /proc/net/route line')
    if fields[1] == '00000000' and fields[7] == '00000000':
        v4.append(fields[0])
try:
    with open('/proc/net/ipv6_route', encoding='ascii') as handle:
        text = handle.read()
except FileNotFoundError:
    text = ''
v6 = []
for line in text.splitlines():
    fields = line.split()
    if not fields:
        continue
    if len(fields) < 10:
        raise SystemExit('unexpected /proc/net/ipv6_route line')
    if fields[1] == '00' and not int(fields[8], 16) & 0x0200:
        v6.append(fields[9])
resolves = {}
if not v4 and not v6:
    for name in NAMES:
        try:
            socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        except OSError:
            resolves[name] = False
        else:
            resolves[name] = True
print(json.dumps({'ipv4_default': v4, 'ipv6_default': v6, 'resolves': resolves}))
"""

_CONNECT_PROBE = r"""
import json, socket
results = []
for host, port in TARGETS:
    try:
        socket.create_connection((host, port), timeout=TIMEOUT).close()
    except OSError as error:
        results.append(type(error).__name__)
    else:
        results.append('connected')
print(json.dumps(results))
"""

_REQUEST_PROBE = r"""
import json, socket
lines = []
for request in REQUESTS:
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as connection:
        connection.sendall(request)
        data = b''
        while b'\r\n' not in data and len(data) < 4096:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
    lines.append(data.split(b'\r\n', 1)[0].decode('ascii', 'replace'))
print(json.dumps(lines))
"""

# Run in the host's network namespace: the IPv4 address of each named
# interface (SIOCGIFADDR), or the errno name when it has none. A bridge of an
# isolated network has none (EADDRNOTAVAIL); a missing bridge is ENODEV.
_BRIDGE_PROBE = r"""
import errno, fcntl, json, socket, struct
SIOCGIFADDR = 0x8915
out = {}
for name in BRIDGES:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            packed = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, struct.pack('256s', name.encode('ascii')))
        except OSError as error:
            out[name] = errno.errorcode.get(error.errno, str(error.errno))
        else:
            out[name] = socket.inet_ntoa(packed[20:24])
print(json.dumps(out))
"""

# PID 1 of a service container is its command; the arguments are NUL-separated.
_CMDLINE_PROBE = r"""
import json
with open('/proc/1/cmdline', 'rb') as handle:
    print(json.dumps(handle.read().decode('ascii').split('\0')[:-1]))
"""

# http.client to loopback, as the service's own healthcheck does.
_HEALTH_PROBE = r"""
import http.client
connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=TIMEOUT)
connection.request('GET', '/fetch/healthz')
response = connection.getresponse()
print(response.status, response.read().decode('ascii', 'replace'))
"""

# Run under --network none with resolv.conf `nameserver 127.0.0.1` and
# `search example.internal`: a loopback DNS server that records every
# question, answers A records for the names in ANSWERS and NXDOMAIN for the
# rest. The search-suffixed name has a "wildcard" answer, as a catch-all zone
# would. Three lookups: the relative name through glibc (the control, which
# must show the suffixed question, or the setup proves nothing), the
# forwarder's resolver for a name with no record of its own, and the whole
# Forwarder, with its default resolver and connector, for the same name.
_SEARCH_DOMAIN_PROBE = r"""
import json, socket, struct, sys, threading, time
sys.path.insert(0, '/app/fetcher')
from strange_ringtone_forwarder.server import Forwarder, resolve_ipv4
ANSWERS = {NAME + '.' + SEARCH + '.': '11.0.0.1', ALLOWED + '.': '11.0.0.2'}
questions = []
lock = threading.Lock()

def question_name(packet):
    labels, index = [], 12
    while packet[index]:
        size = packet[index]
        labels.append(packet[index + 1:index + 1 + size].decode('ascii'))
        index += 1 + size
    return '.'.join(labels) + '.', index + 1

def serve(sock):
    while True:
        packet, peer = sock.recvfrom(4096)
        name, end = question_name(packet)
        qtype = struct.unpack('!H', packet[end:end + 2])[0]
        with lock:
            questions.append([name, qtype])
        flags, records = 0x8183, b''
        if name in ANSWERS:
            flags = 0x8180
            if qtype == 1:
                records = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + socket.inet_aton(ANSWERS[name])
        count = 1 if records else 0
        sock.sendto(packet[:2] + struct.pack('!HHHHH', flags, 1, count, 0, 0) + packet[12:end + 4] + records, peer)

dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
dns.bind(('127.0.0.1', 53))
threading.Thread(target=serve, args=(dns,), daemon=True).start()

def run(call):
    with lock:
        questions.clear()
    try:
        result = {'answer': call()}
    except OSError as error:
        result = {'error': type(error).__name__}
    with lock:
        result['questions'] = list(questions)
    return result

def through_the_forwarder():
    outcomes = []
    server = Forwarder(('127.0.0.1', 0), sink=outcomes.append)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    try:
        with socket.create_connection(('127.0.0.1', server.server_address[1]), timeout=TIMEOUT) as client:
            client.sendall(('CONNECT ' + NAME + ':443 HTTP/1.1\r\n\r\n').encode('ascii'))
            status = client.recv(64).split(b' ')[1].decode('ascii')
        end = time.monotonic() + TIMEOUT
        while not outcomes and time.monotonic() < end:
            time.sleep(0.02)
        return [status, outcomes[0].reason if outcomes else None]
    finally:
        server.shutdown()
        server.server_close()

def relative():
    infos = socket.getaddrinfo(NAME, 443, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})

print(json.dumps({
    'relative': run(relative),
    'resolve_ipv4': run(lambda: resolve_ipv4(NAME)),
    'resolve_ipv4_answered': run(lambda: resolve_ipv4(ALLOWED)),
    'forwarder': run(through_the_forwarder),
}))
"""


def _routes_probe(names: list[str]) -> str:
    return f"NAMES = {names!r}\n" + _ROUTES_PROBE


def _connect_probe(targets: list[tuple[str, int]]) -> str:
    return f"TARGETS = {targets!r}\nTIMEOUT = {PROBE_TIMEOUT!r}\n" + _CONNECT_PROBE


def _request_probe(host: str, port: int, requests: list[bytes]) -> str:
    return f"HOST = {host!r}\nPORT = {port!r}\nREQUESTS = {requests!r}\nTIMEOUT = {PROBE_TIMEOUT!r}\n" + _REQUEST_PROBE


def _bridge_probe(bridges: list[str]) -> str:
    return f"BRIDGES = {bridges!r}\n" + _BRIDGE_PROBE


def _health_probe() -> str:
    return f"PORT = {FETCHER_PORT!r}\nTIMEOUT = {PROBE_TIMEOUT!r}\n" + _HEALTH_PROBE


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # An argument list, never a shell; UTF-8 whatever the host's code page.
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _docker(*args: str, timeout: float = COMPOSE_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *args], timeout)


def _describe(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return f"exit {error.returncode}: {(error.stderr or '').strip() or (error.stdout or '').strip()}"
    if isinstance(error, subprocess.TimeoutExpired):
        stderr = error.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return f"timed out after {error.timeout} s: {(stderr or '').strip()}"
    return f"{type(error).__name__}: {error}"


def _is_os_error(name: str) -> bool:
    """True when a probe's exception class name is a builtin OSError subclass (refused, unreachable, timed out)."""
    cls = getattr(builtins, name, None)
    return isinstance(cls, type) and issubclass(cls, OSError)


def _is_ip(text: object) -> bool:
    if not isinstance(text, str) or not text:
        return False
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _override_yaml(test_env: Path) -> str:
    # JSON strings are valid YAML double-quoted scalars, so a temp path with
    # spaces or a drive letter needs no escaping of our own.
    env = json.dumps(test_env.as_posix())
    image = json.dumps(IMAGE)
    # compose resolves every service's env_file, `config` and services it
    # doesn't start included, so the core's is overridden too: the
    # developer's .env is never read, and CI has none.
    return (
        "services:\n"
        "  core:\n"
        "    env_file: !override\n"
        f"      - {env}\n"
        "  fetcher:\n"
        "    build: !reset null\n"
        f"    image: {image}\n"
        "    pull_policy: never\n"
        "    env_file: !override\n"
        f"      - {env}\n"
        "  fetcher-forwarder:\n"
        "    build: !reset null\n"
        f"    image: {image}\n"
        "    pull_policy: never\n"
    )


class Stack:
    """One compose project of compose.example.yaml with the test override, and the containers the tests add."""

    def __init__(self) -> None:
        # Unique per run, so parallel runs and a developer's own stack never
        # share a container or network.
        self.project = f"strange-ringtone-compose-{os.getpid()}-{secrets.token_hex(3)}"
        self._dir = Path(tempfile.mkdtemp(prefix="strange-ringtone-compose-"))
        self.test_env = self._dir / "test.env"
        self.override = self._dir / "override.yaml"
        self._probes = 0
        # Containers started with plain `docker run`: `compose down` doesn't
        # know them, and one still attached would keep a network alive.
        self._extra: list[str] = []
        try:
            with open(self.test_env, "w", encoding="ascii", newline="\n") as handle:
                handle.write(f"PUBLIC_ORIGIN={ORIGIN}\n")
            with open(self.override, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_override_yaml(self.test_env))
        except BaseException:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise

    def compose(
        self, *args: str, check: bool = False, timeout: float = COMPOSE_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            "docker", "compose", "-p", self.project,
            "--project-directory", str(ROOT),
            "-f", str(COMPOSE_FILE),
            "-f", str(self.override),
            "--env-file", str(self.test_env),
            *args,
        ]
        result = _run(argv, timeout)
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
        return result

    def start(self) -> None:
        self.compose(
            "up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "fetcher", "fetcher-forwarder",
            check=True,
        )

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)

    def stop(self) -> None:
        try:
            for name in self._extra:
                _docker("rm", "-f", name, timeout=60)
            down = self.compose("down", "--volumes", "--remove-orphans", "--timeout", "5")
        finally:
            self.close()
        if down.returncode != 0:
            # Visible as an error of the class: something may be left behind.
            raise RuntimeError(f"compose down failed for {self.project}: {down.stderr.strip()}")

    def run_in(self, service: str, code: str) -> subprocess.CompletedProcess[str]:
        """A fresh container of the service, with its networks and hardening, running python -I -c code."""
        return self.compose("run", "--rm", "--no-deps", "-T", service, "python", "-I", "-c", code)

    def exec_in(self, service: str, code: str) -> subprocess.CompletedProcess[str]:
        """python -I -c code inside the service's running container."""
        return self.compose("exec", "-T", service, "python", "-I", "-c", code)

    def container_ids(self, service: str) -> list[str]:
        return self.compose("ps", "-q", service).stdout.split()

    def addresses(self, container_id: str) -> dict[str, str]:
        """Network name (<project>_<net>) -> the container's IPv4 address on it."""
        result = _docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id)
        if result.returncode != 0:
            raise AssertionError(f"docker inspect {container_id} failed: {result.stderr.strip()}")
        networks = json.loads(result.stdout)
        return {name: (info or {}).get("IPAddress", "") for name, info in networks.items()}

    def network(self, name: str) -> str:
        return f"{self.project}_{name}"

    def logs(self, service: str) -> subprocess.CompletedProcess[str]:
        return self.compose("logs", "--no-log-prefix", "--no-color", service)

    def container_name(self, suffix: str) -> str:
        name = f"{self.project}-{suffix}"
        self._extra.append(name)
        return name

    def docker_run(self, *args: str, timeout: float = PROBE_RUN_TIMEOUT) -> subprocess.CompletedProcess[str]:
        # --pull never: a missing image must fail here, not fetch from a registry.
        return _docker("run", "--pull", "never", *args, timeout=timeout)

    def probe_on_network(self, net: str, code: str) -> subprocess.CompletedProcess[str]:
        """A throwaway hardened container of the image on one network, running python -I -c code."""
        self._probes += 1
        name = self.container_name(f"probe-{self._probes}")
        return self.docker_run(
            "--rm", "--name", name, "--network", net, *HARDENING, IMAGE, "python", "-I", "-c", code,
        )


class EngineTest(unittest.TestCase):
    def test_docker_engine_is_at_least_26(self) -> None:
        """Engine 26.0+ (supporting T1): older engines forward DNS from internal networks; a failure, not a skip."""
        try:
            result = _docker("version", "--format", "{{.Server.Version}}", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.fail(f"docker version failed: {_describe(error)}")
        self.assertEqual(result.returncode, 0, f"docker version failed: {result.stderr.strip()}")
        version = result.stdout.strip()
        match = re.match(r"(\d+)\.", version)
        self.assertIsNotNone(match, f"unexpected Docker Engine version {version!r}")
        assert match is not None
        major = int(match.group(1))
        self.assertGreaterEqual(
            major,
            26,
            f"Docker Engine 26.0 or newer is required (CVE-2024-29018: older engines forward DNS from "
            f"internal networks); found {version}",
        )


    def test_docker_engine_supports_the_isolated_gateway_mode(self) -> None:
        """Engine 28.0+ (supporting T1): the internal networks' `isolated` gateway mode; a failure, not a skip."""
        try:
            result = _docker("version", "--format", "{{.Server.Version}}", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.fail(f"docker version failed: {_describe(error)}")
        self.assertEqual(result.returncode, 0, f"docker version failed: {result.stderr.strip()}")
        version = result.stdout.strip()
        match = re.match(r"(\d+)\.(\d+)", version)
        self.assertIsNotNone(match, f"unexpected Docker Engine version {version!r}")
        assert match is not None
        found = (int(match.group(1)), int(match.group(2)))
        self.assertGreaterEqual(
            found,
            ENGINE_FLOOR,
            f"Docker Engine {ENGINE_FLOOR[0]}.{ENGINE_FLOOR[1]} or newer is required (the release that added "
            f"the bridge driver's isolated gateway mode, which keeps the Docker host off the internal "
            f"networks); found {version}",
        )


class LayoutTest(unittest.TestCase):
    """compose.example.yaml as compose resolves it, with the test override (T2 static, hardening)."""

    stack: ClassVar[Stack | None] = None
    config: ClassVar[dict[str, Any] | None] = None
    error: ClassVar[str | None] = None

    @classmethod
    def setUpClass(cls) -> None:
        # Never raises: a broken layout must show up as failed assertions in
        # each test, not as an error that hides which property broke.
        cls.stack = None
        cls.config = None
        cls.error = None
        try:
            cls.stack = Stack()
            result = cls.stack.compose("config", "--format", "json")
            if result.returncode != 0:
                cls.error = f"docker compose config exited {result.returncode}: {result.stderr.strip()}"
            else:
                cls.config = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            cls.error = f"docker compose config failed: {_describe(error)}"

    @classmethod
    def tearDownClass(cls) -> None:
        # `config` starts nothing, so there is nothing to bring down.
        if cls.stack is not None:
            cls.stack.close()

    def _config(self) -> dict[str, Any]:
        self.assertIsNone(self.error, self.error)
        self.assertIsInstance(self.config, dict)
        assert self.config is not None
        return self.config

    def _services(self) -> dict[str, Any]:
        services = self._config().get("services")
        self.assertIsInstance(services, dict, "compose config has no services")
        return services

    def _service(self, name: str) -> dict[str, Any]:
        services = self._services()
        self.assertIn(name, services, f"compose.example.yaml has no {name!r} service")
        return services[name]

    def _networks_of(self, service: dict[str, Any]) -> set[str]:
        return set(service.get("networks") or {})

    def test_services_networks_and_flags(self) -> None:
        """T2 (static): the fetcher only on internal networks, the forwarder alone on `outside`."""
        services = self._services()
        self.assertLessEqual({"core", "fetcher", "fetcher-forwarder"}, set(services))
        networks = self._config().get("networks") or {}
        for name in ("core", "fetcher", "egress", "outside"):
            self.assertIn(name, networks, f"compose.example.yaml has no {name!r} network")
        # internal: no route out and no external DNS (with Engine 26.0+).
        self.assertIs(networks["egress"].get("internal"), True, "egress must be internal")
        self.assertIs(networks["fetcher"].get("internal"), True, "fetcher must be internal")
        self.assertIs(networks["core"].get("internal"), True, "core must be internal")
        # compose omits `internal: false`; a missing key is the normal network.
        self.assertIs(networks["outside"].get("internal", False), False, "outside is the forwarder's way out")
        self.assertEqual(self._networks_of(services["fetcher"]), {"fetcher", "egress"})
        self.assertEqual(self._networks_of(services["fetcher-forwarder"]), {"egress", "outside"})
        self.assertEqual(self._networks_of(services["core"]), {"core"})

    def test_internal_networks_give_the_host_no_address(self) -> None:
        """T1/T2 (static): every internal network is also isolated from the Docker host; `outside` keeps its gateway."""
        networks = self._config().get("networks") or {}
        for name in INTERNAL_NETWORKS:
            with self.subTest(network=name):
                self.assertIn(name, networks, f"compose.example.yaml has no {name!r} network")
                options = networks[name].get("driver_opts") or {}
                # internal: true alone leaves the host an address on the
                # bridge, and with it every service the host listens on.
                self.assertEqual(options.get(GATEWAY_MODE), "isolated", f"{name} gives the host an address")
        self.assertIn("outside", networks)
        outside = (networks["outside"].get("driver_opts") or {}).get(GATEWAY_MODE)
        self.assertNotEqual(outside, "isolated", "outside needs its gateway: it is the forwarder's route out")

    def test_every_service_rotates_its_log(self) -> None:
        """Supporting (G10): a json-file log with max-size and max-file on every service, so no log fills the disk."""
        for name, service in self._services().items():
            with self.subTest(service=name):
                logging = service.get("logging") or {}
                self.assertEqual(logging.get("driver"), LOGGING["driver"], f"{name}: {logging!r}")
                self.assertEqual(logging.get("options"), LOGGING["options"], f"{name}: {logging!r}")

    def test_fetcher_points_https_proxy_at_the_forwarder(self) -> None:
        """T2 (static): yt-dlp's only way out is the forwarder, set on the service so .env can't change it."""
        fetcher = self._service("fetcher")
        environment = fetcher.get("environment") or {}
        self.assertEqual(environment.get("HTTPS_PROXY"), PROXY_URL)
        # `docker compose up -d fetcher` must bring the forwarder up too.
        self.assertIn("fetcher-forwarder", fetcher.get("depends_on") or {})

    def test_forwarder_command_and_hardening(self) -> None:
        """Supporting: the forwarder command of the fetcher image, with the contract's flags and nothing writable."""
        forwarder = self._service("fetcher-forwarder")
        self.assertEqual(forwarder.get("command"), FORWARDER_COMMAND)
        self.assertEqual(forwarder.get("user"), UID_GID)
        self.assertIs(forwarder.get("read_only"), True)
        self.assertEqual(forwarder.get("cap_drop"), ["ALL"])
        self.assertIn("no-new-privileges:true", forwarder.get("security_opt") or [])
        self.assertEqual(forwarder.get("ipc"), "none")
        # The forwarder writes nothing and reads no setting.
        self.assertNotIn("tmpfs", forwarder)
        self.assertNotIn("env_file", forwarder)
        self.assertNotIn("environment", forwarder)
        self.assertIn("mem_limit", forwarder)

    def test_no_service_publishes_a_port(self) -> None:
        """Supporting: no port on any host interface; a reverse proxy joins the networks instead."""
        for name, service in self._services().items():
            with self.subTest(service=name):
                self.assertFalse(service.get("ports"), f"{name} publishes {service.get('ports')!r}")

    def test_fetcher_hardening_unchanged(self) -> None:
        """Supporting: PR A changes the fetcher's networks and proxy, not its hardening."""
        fetcher = self._service("fetcher")
        self.assertEqual(fetcher.get("user"), UID_GID)
        self.assertIs(fetcher.get("read_only"), True)
        self.assertEqual(fetcher.get("cap_drop"), ["ALL"])
        self.assertIn("no-new-privileges:true", fetcher.get("security_opt") or [])
        self.assertEqual(fetcher.get("ipc"), "none")
        tmpfs = fetcher.get("tmpfs") or []
        self.assertEqual(len(tmpfs), 1, tmpfs)
        self.assertTrue(tmpfs[0].startswith("/tmp:"), tmpfs)
        self.assertIn("size=96m", tmpfs[0].split(":", 1)[1].split(","))
        self.assertIn("mem_limit", fetcher)


class StackTest(unittest.TestCase):
    """The running layout: routes, reachability and the forwarder's gate (T1, T2, T3)."""

    stack: ClassVar[Stack | None] = None
    start_error: ClassVar[BaseException | None] = None
    _t3_result: ClassVar[subprocess.CompletedProcess[str] | None] = None

    @classmethod
    def setUpClass(cls) -> None:
        # The start error is recorded, not raised: every test then fails with
        # it in setUp, and tearDownClass still brings down whatever started.
        cls.stack = None
        cls.start_error = None
        cls._t3_result = None
        try:
            cls.stack = Stack()
            cls.stack.start()
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as error:
            cls.start_error = error

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.stack is not None:
            cls.stack.stop()

    def setUp(self) -> None:
        if self.start_error is not None:
            self.fail(f"the stack did not start: {_describe(self.start_error)}")
        self.assertIsNotNone(self.stack)

    @property
    def _stack(self) -> Stack:
        assert self.stack is not None
        return self.stack

    # Helpers that turn a failed docker command into a failed assertion.

    def _probe_output(self, result: subprocess.CompletedProcess[str], what: str) -> Any:
        self.assertEqual(result.returncode, 0, f"{what} failed: {result.stderr.strip()}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"{what} printed nothing: {result.stderr.strip()}")
        return json.loads(lines[-1])

    def _addresses(self, service: str) -> dict[str, str]:
        ids = self._stack.container_ids(service)
        self.assertEqual(len(ids), 1, f"expected one {service} container, found {ids}")
        return self._stack.addresses(ids[0])

    def _address(self, addresses: dict[str, str], network: str) -> str:
        key = self._stack.network(network)
        self.assertIn(key, addresses, f"not on {key}: {addresses}")
        address = addresses[key]
        # An empty or bogus address would make "unreachable" true for the
        # wrong reason.
        self.assertTrue(_is_ip(address), f"no address on {key}: {address!r}")
        return address

    def _assert_unreachable(self, outcome: str, target: str) -> None:
        self.assertTrue(_is_os_error(outcome), f"{target} was reachable or the probe misbehaved: {outcome!r}")

    def _t3(self) -> list[str]:
        # One batch for both T3 tests, sent from a fetcher container (on
        # `egress`), so each test also passes when run alone.
        if type(self)._t3_result is None:
            type(self)._t3_result = self._stack.run_in(
                "fetcher", _request_probe("fetcher-forwarder", FORWARDER_PORT, T3_REQUESTS),
            )
        assert type(self)._t3_result is not None
        lines = self._probe_output(type(self)._t3_result, "T3 request probe")
        self.assertIsInstance(lines, list)
        return lines

    # T1

    def test_t1_fetcher_has_no_default_route_and_no_external_dns(self) -> None:
        """T1: the fetcher has no default route (IPv4 or IPv6), resolves no external name, and is healthy."""
        result = self._probe_output(
            self._stack.run_in("fetcher", _routes_probe([EXTERNAL_NAME, "fetcher-forwarder"])),
            "fetcher route probe",
        )
        self.assertEqual(result["ipv4_default"], [], "the fetcher has an IPv4 default route")
        self.assertEqual(result["ipv6_default"], [], "the fetcher has an IPv6 default route")
        # Control: the embedded DNS answers, so the external failure means
        # something.
        self.assertIs(result["resolves"].get("fetcher-forwarder"), True, result)
        self.assertIs(result["resolves"].get(EXTERNAL_NAME), False, result)
        health = self._stack.exec_in("fetcher", _health_probe())
        self.assertEqual(health.returncode, 0, health.stderr.strip())
        self.assertEqual(health.stdout.strip(), "200 ok")

    def test_t1_forwarder_has_the_only_default_route(self) -> None:
        """T1: the forwarder, on `outside`, has exactly one IPv4 default route."""
        result = self._probe_output(
            self._stack.run_in("fetcher-forwarder", _routes_probe([])),
            "forwarder route probe",
        )
        self.assertEqual(len(result["ipv4_default"]), 1, result)

    def test_t1_fetcher_refuses_to_start_on_the_outside_network(self) -> None:
        """T1/S2: the fetcher's CMD on a network with a route out exits 3 and says why."""
        name = self._stack.container_name("refusal")
        try:
            result = self._stack.docker_run(
                "--rm", "--name", name, "--network", self._stack.network("outside"),
                *HARDENING, *FETCHER_TMPFS, "-e", f"PUBLIC_ORIGIN={ORIGIN}", IMAGE,
            )
        except subprocess.TimeoutExpired:
            self.fail("the fetcher kept running on the outside network instead of refusing to start")
        self.assertEqual(result.returncode, EXIT_ROUTE, result.stderr.strip())
        self.assertIn("refusing to start", result.stderr)
        self.assertIn("has a default route", result.stderr)
        self.assertIn("IPv4", result.stderr)
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", result.stderr)

    def test_t1_the_override_lets_it_start_there(self) -> None:
        """T1/S2: the same container with FETCH_ALLOW_DEFAULT_ROUTE=true starts, serves healthz and says so."""
        name = self._stack.container_name("override")
        self.addCleanup(_docker, "rm", "-f", name, timeout=60)
        started = self._stack.docker_run(
            "-d", "--name", name, "--network", self._stack.network("outside"),
            *HARDENING, *FETCHER_TMPFS,
            "-e", f"PUBLIC_ORIGIN={ORIGIN}", "-e", "FETCH_ALLOW_DEFAULT_ROUTE=true", IMAGE,
        )
        self.assertEqual(started.returncode, 0, started.stderr.strip())
        deadline = time.monotonic() + OVERRIDE_HEALTH_SECONDS
        health = ""
        while time.monotonic() < deadline:
            probe = _docker("exec", name, "python", "-I", "-c", _health_probe(), timeout=PROBE_RUN_TIMEOUT)
            health = probe.stdout.strip()
            if probe.returncode == 0 and health == "200 ok":
                break
            time.sleep(0.5)
        self.assertEqual(health, "200 ok", "the fetcher did not serve healthz with the override set")
        logs = _docker("logs", name, timeout=PROBE_RUN_TIMEOUT)
        output = logs.stdout + logs.stderr
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE is set", output)
        self.assertNotIn("refusing to start", output)

    def test_t1_internal_networks_give_the_host_no_address(self) -> None:
        """T1: the Docker host has no address on the `fetcher` and `egress` bridges, so the fetcher can't reach it."""
        bridges: dict[str, str] = {}
        for name in ("fetcher", "egress", "outside"):
            network = self._stack.network(name)
            result = _docker("network", "inspect", "--format", "{{json .}}", network, timeout=PROBE_RUN_TIMEOUT)
            self.assertEqual(result.returncode, 0, f"docker network inspect {network}: {result.stderr.strip()}")
            info = json.loads(result.stdout)
            bridges[name] = "br-" + info["Id"][:12]
            gateways = [config.get("Gateway") for config in (info.get("IPAM") or {}).get("Config") or []]
            if name == "outside":
                # Control: the network with a route out has its gateway.
                self.assertTrue(any(gateways), f"{network} has no gateway: {info.get('IPAM')}")
                continue
            self.assertEqual((info.get("Options") or {}).get(GATEWAY_MODE), "isolated", network)
            self.assertFalse(any(gateways), f"{network} gives the host an address: {gateways}")
        # The host's own view, from its network namespace: the isolated
        # bridges hold no IPv4 address, while `outside`'s does (so the probe
        # finds the bridges and reads addresses).
        name = self._stack.container_name("host-probe")
        result = self._stack.docker_run(
            "--rm", "--name", name, "--network", "host", *HARDENING, IMAGE,
            "python", "-I", "-c", _bridge_probe(sorted(bridges.values())),
        )
        addresses = self._probe_output(result, "host bridge probe")
        self.assertTrue(_is_ip(addresses.get(bridges["outside"])), f"control failed: {addresses}")
        for network in ("fetcher", "egress"):
            self.assertEqual(addresses.get(bridges[network]), "EADDRNOTAVAIL",
                             f"the host has an address on {network}: {addresses}")

    def test_the_image_and_the_services_run_the_base_interpreter_isolated(self) -> None:
        """I1: the image's CMD and both running services use the base interpreter with -I, never the venv's."""
        result = _docker("image", "inspect", "--format", "{{json .Config.Cmd}}", IMAGE, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        self.assertEqual(json.loads(result.stdout), FETCHER_COMMAND, "the image's CMD changed")
        for service, expected in (("fetcher", FETCHER_COMMAND), ("fetcher-forwarder", FORWARDER_COMMAND)):
            with self.subTest(service=service):
                running = self._probe_output(self._stack.exec_in(service, _CMDLINE_PROBE), f"{service} cmdline")
                self.assertEqual(running, expected)

    # T2

    def test_t2_fetcher_reaches_the_forwarder_over_egress(self) -> None:
        """T2 (a): a GET from the fetcher to fetcher-forwarder:3128 gets the forwarder's 405."""
        lines = self._probe_output(
            self._stack.run_in(
                "fetcher", _request_probe("fetcher-forwarder", FORWARDER_PORT, [b"GET / HTTP/1.1\r\n\r\n"]),
            ),
            "fetcher to forwarder probe",
        )
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("HTTP/1.1 405"), lines)

    def test_t2_forwarder_cannot_reach_the_fetcher(self) -> None:
        """T2 (b): the forwarder is not on the `fetcher` network and can't reach the fetcher's address there."""
        fetcher = self._addresses("fetcher")
        forwarder = self._addresses("fetcher-forwarder")
        self.assertNotIn(self._stack.network("fetcher"), forwarder, "the forwarder is on the fetcher network")
        target = self._address(fetcher, "fetcher")
        # Control: the same probe does reach the forwarder over `egress`, so
        # a failure to reach the fetcher is the network, not the probe.
        control = self._address(forwarder, "egress")
        outcomes = self._probe_output(
            self._stack.run_in(
                "fetcher-forwarder", _connect_probe([(target, FETCHER_PORT), (control, FORWARDER_PORT)]),
            ),
            "forwarder reachability probe",
        )
        self.assertEqual(outcomes[1], "connected", f"control failed: {outcomes}")
        self._assert_unreachable(outcomes[0], f"the fetcher at {target}:{FETCHER_PORT}")

    def test_t2_nothing_on_the_fetcher_network_reaches_the_forwarder(self) -> None:
        """T2 (c): a container on the `fetcher` network reaches the fetcher but neither forwarder address."""
        fetcher = self._addresses("fetcher")
        forwarder = self._addresses("fetcher-forwarder")
        control = self._address(fetcher, "fetcher")
        on_egress = self._address(forwarder, "egress")
        on_outside = self._address(forwarder, "outside")
        outcomes = self._probe_output(
            self._stack.probe_on_network(
                self._stack.network("fetcher"),
                _connect_probe([(control, FETCHER_PORT), (on_egress, FORWARDER_PORT), (on_outside, FORWARDER_PORT)]),
            ),
            "fetcher-network probe",
        )
        self.assertEqual(outcomes[0], "connected", f"control failed: {outcomes}")
        self._assert_unreachable(outcomes[1], f"the forwarder at {on_egress}:{FORWARDER_PORT}")
        self._assert_unreachable(outcomes[2], f"the forwarder at {on_outside}:{FORWARDER_PORT}")

    # T3

    def test_t3_forwarder_refuses_unlisted_literal_and_port_80_before_dns(self) -> None:
        """T3/G1: unlisted names 403, an IP literal 400, port 80 403, GET 405; 403 not 502 shows no lookup ran."""
        lines = self._t3()
        self.assertEqual(len(lines), len(T3_REQUESTS), lines)
        for line in lines:
            self.assertTrue(line.startswith("HTTP/1.1 "), lines)
        statuses = [int(line.split()[1]) if len(line.split()) > 1 and line.split()[1].isdigit() else None
                    for line in lines]
        self.assertEqual(statuses, T3_STATUSES, lines)

    def test_t3_refusals_are_logged_with_name_and_reason_only(self) -> None:
        """T3/G10: each refusal logs `forwarder refused <reason> <name|->` and nothing from the request else."""
        self._t3()
        deadline = time.monotonic() + LOG_WAIT_SECONDS
        while True:
            result = self._stack.logs("fetcher-forwarder")
            self.assertEqual(result.returncode, 0, result.stderr.strip())
            lines = [line.strip() for line in (result.stdout + result.stderr).splitlines()]
            missing = [line for line in T3_LOG_LINES if line not in lines]
            if not missing or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        self.assertEqual(missing, [], f"missing log lines; the log was: {lines}")
        for line in lines:
            for marker in LEAK_MARKERS:
                self.assertNotIn(marker, line, f"request text reached the log: {line!r}")
            if line.startswith("forwarder "):
                self.assertRegex(line, FORWARDER_LOG_RE)


class ResolverSearchDomainTest(unittest.TestCase):
    """G5/G6 against the image's glibc: the forwarder looks up the absolute name, never one with a search domain."""

    # Like every probe here, this sends nothing to an external host: it runs
    # under `--network none` against a fake DNS server on the container's own
    # loopback.

    # An allowed name with no record of its own: the client picks the first
    # label freely, so it can always force the NXDOMAIN that makes glibc try
    # the search domains. SEARCH is a placeholder zone, never a deployment's.
    NAME = "nosuch.youtube.com"
    ALLOWED = "www.youtube.com"
    SEARCH = "example.internal"

    def test_the_forwarder_never_appends_a_search_domain(self) -> None:
        """No network at all (--network none): the DNS server is a loopback fake inside the container."""
        name = f"strange-ringtone-compose-{os.getpid()}-{secrets.token_hex(3)}-dns"
        self.addCleanup(_docker, "rm", "-f", name, timeout=60)
        code = (
            f"NAME = {self.NAME!r}\nALLOWED = {self.ALLOWED!r}\nSEARCH = {self.SEARCH!r}\n"
            f"TIMEOUT = {PROBE_TIMEOUT!r}\n" + _SEARCH_DOMAIN_PROBE
        )
        result = _docker(
            "run", "--pull", "never", "--rm", "--name", name,
            "--network", "none", "--dns", "127.0.0.1", "--dns-search", self.SEARCH,
            # The fake DNS listens on port 53 as UID 10001 with no capability;
            # this namespaced sysctl only affects the container's own netns.
            "--sysctl", "net.ipv4.ip_unprivileged_port_start=53",
            *HARDENING, IMAGE, "python", "-I", "-c", code,
            timeout=PROBE_RUN_TIMEOUT,
        )
        self.assertEqual(result.returncode, 0, f"the probe failed: {result.stderr.strip()}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"the probe printed nothing: {result.stderr.strip()}")
        out = json.loads(lines[-1])
        absolute = [self.NAME + ".", 1]
        suffixed = [f"{self.NAME}.{self.SEARCH}.", 1]
        # Control: a relative lookup does reach the search domain and takes
        # its wildcard answer, so the setup can show the attack.
        self.assertEqual(out["relative"], {"answer": ["11.0.0.1"], "questions": [absolute, suffixed]}, out)
        # The forwarder's resolver asks for the absolute name only and fails.
        self.assertEqual(out["resolve_ipv4"], {"error": "gaierror", "questions": [absolute]}, out)
        # Acceptance: an allowed name with a record of its own resolves.
        self.assertEqual(
            out["resolve_ipv4_answered"], {"answer": ["11.0.0.2"], "questions": [[self.ALLOWED + ".", 1]]}, out,
        )
        # The whole Forwarder: one absolute question, then 502 resolve_failed
        # (with a relative name it would take 11.0.0.1 and try to connect).
        self.assertEqual(out["forwarder"], {"answer": ["502", "refused:resolve_failed"], "questions": [absolute]}, out)


if __name__ == "__main__":
    unittest.main()
