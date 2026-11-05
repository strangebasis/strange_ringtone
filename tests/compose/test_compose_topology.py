"""The compose layout of the fetcher, its runner and its forwarder, brought up for real (docs/PLAN.md 6.1, T1 to T3).

compose.example.yaml itself is the system under test: the fetcher on internal
networks only (`fetcher` for the reverse proxy, `jobs` for the runner), the
runner on internal networks only (`jobs`, `egress`) with its HTTPS_PROXY
pointing at the forwarder, and the forwarder alone on `outside`. The tests
start it with the already built fetcher image under a unique project name,
through a temporary override that never builds, never pulls and never reads
the developer's .env; they publish no port and tear everything down
afterwards.

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
# The runner, the image's third command: the base interpreter too, which
# starts yt-dlp under the venv's interpreter as a subprocess. Its port and
# health path, and the fetcher's (strange_ringtone_runner.__main__.PORT,
# strange_ringtone_runner.server.HEALTH_PATH).
RUNNER_COMMAND = ["python", "-I", "/app/fetcher/runner.py"]
RUNNER_PORT = 8080
RUNNER_HEALTH_PATH = "/healthz"
FETCHER_HEALTH_PATH = "/fetch/healthz"
# Each container's /tmp (README, Writable): the fetcher holds one download
# arriving from the runner and one uncollected result (2 x FETCH_MAX_BYTES);
# the runner one download in flight or one audio waiting to be collected,
# never both, plus the info JSON and Deno's cache (FETCH_MAX_BYTES + 16 MB).
FETCHER_TMPFS_SIZE = "size=80m"
RUNNER_TMPFS_SIZE = "size=56m"
# The engine release that added the bridge driver's `isolated` gateway mode,
# which the internal networks use so the Docker host has no address on them:
# Docker Engine 28.0.0 (release notes, moby/moby#49262), which accepts it on
# an internal network only. It also has the CVE-2024-29018 DNS fix of 26.0.
ENGINE_FLOOR = (28, 0)
GATEWAY_MODE = "com.docker.network.bridge.gateway_mode_ipv4"
# The IPv4 mode covers IPv4 only: with IPv6 turned on for a network, the host
# gets an IPv6 address on its bridge again unless this mode is set as well.
GATEWAY_MODE_V6 = "com.docker.network.bridge.gateway_mode_ipv6"
# A port for the IPv6 control's listener in the host's network namespace,
# bound only to the control bridge's own address.
V6_CONTROL_PORT = 18081
INTERNAL_NETWORKS = ("core", "fetcher", "jobs", "egress")
# Every service's log rotates, so a flood of refusals can't fill the host's disk.
LOGGING = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
PROXY_URL = f"http://fetcher-forwarder:{FORWARDER_PORT}"
# The services that run the fetcher image, which turn IP forwarding off and
# cap their processes and threads (compose.example.yaml).
FETCHER_IMAGE_SERVICES = ("fetcher", "fetcher-runner", "fetcher-forwarder")
IP_FORWARD = "net.ipv4.ip_forward"
# The parts of each service's worst-case thread count that aren't one of the
# code's own caps. Every service: its main thread and the health check's
# process (Docker runs one at a time). The fetcher also: the job worker and
# the sweeper (Jobs.start), and the runner client's deadline timer for the
# one exchange the job worker has open. The runner also: its result sweeper,
# and one yt-dlp run, measured offline in the image under the contract
# flags: yt-dlp's Python 1 thread, Deno's main thread, 2 tokio threads and
# V8's workers, at most 4 there (one per usable CPU up to 4), counted as 63
# for a host that reports 64 CPUs.
SERVICE_FIXED_THREADS = 1 + 1
JOB_THREADS = 2
CLIENT_THREADS = 1
RUNNER_THREADS = 1
YTDLP_RUN_THREADS = 1 + 1 + 2 + 63
# ICMP redirects (compose.example.yaml). With forwarding off the kernel takes a
# redirect when `all` or the receiving interface says 1, and turning forwarding
# off sets `all` to 1, so the fetcher-image services set all, default and lo
# through `sysctls`, and each interface Docker attaches through the endpoint
# sysctl of its network attachment (IFNAME is that interface).
ACCEPT_REDIRECTS_SYSCTLS = tuple(f"net.ipv4.conf.{name}.accept_redirects" for name in ("all", "default", "lo"))
ENDPOINT_SYSCTLS = "com.docker.network.endpoint.sysctls"
IFNAME_ACCEPT_REDIRECTS = "net.ipv4.conf.IFNAME.accept_redirects"
# Forwarding off per interface (compose.example.yaml): the kernel forwards a
# packet only when the interface it arrived on has forwarding on, so default
# and lo are set through `sysctls` and each attached interface through its
# attachment's endpoint sysctl. The global switch (ip_forward, the same value
# as conf/all/forwarding) is never written: writing it resets
# all.accept_redirects to 1, and Docker writes sysctls in no fixed order. So
# ip_forward reads 1 inside by design, and no running test asserts it.
FORWARDING_SYSCTLS = tuple(f"net.ipv4.conf.{name}.forwarding" for name in ("default", "lo"))
IFNAME_FORWARDING = "net.ipv4.conf.IFNAME.forwarding"
GLOBAL_FORWARDING_SYSCTLS = (IP_FORWARD, "net.ipv4.conf.all.forwarding")
# The behaviour proof (ForwardingBehaviourTest): UDP datagrams from a helper
# on a copy of `egress`, routed through a router container, to a listener on
# a copy of `outside`. Each phase sends this many, this far apart.
ROUTED_DATAGRAMS = 20
ROUTED_INTERVAL = 0.05
ROUTED_PORT = 18082
ROUTED_WAIT_SECONDS = 15.0


def _endpoint_sysctls(options: dict[str, Any]) -> dict[str, list[str]]:
    """The endpoint sysctls of one network attachment's driver_opts: each key with every value it is given."""
    entries: dict[str, list[str]] = {}
    for entry in str(options.get(ENDPOINT_SYSCTLS, "")).split(","):
        key, _sep, value = entry.strip().partition("=")
        if key:
            entries.setdefault(key, []).append(value)
    return entries


def _network_flag(network: str, options: dict[str, Any]) -> str:
    """docker run's --network value for one network with these driver options (a CSV field each, quoted)."""
    fields = [f"name={network}"]
    for key, value in sorted(options.items()):
        text = f"{key}={value}"
        # The value is a comma-separated list, so the field is quoted; a quote
        # inside would need CSV escaping, which no example option has.
        assert '"' not in text, text
        fields.append(f'"driver-opt={text}"')
    return ",".join(fields)


def _worst_case_threads() -> dict[str, int]:
    """Each fetcher-image service's worst-case thread count, from the code's own caps.

    The forwarder: one thread per open connection (connections_max), and the
    lookups, each in a thread that lives until glibc gives up (at most 10 s
    with the resolv.conf Docker writes for these networks) and each started
    by a tunnel within that time, so tunnels_per_minute bounds them. The
    fetcher: one thread per request worker (FetchServer.MAX_WORKERS), the job
    threads and the client's deadline timer; it runs no yt-dlp. The runner:
    one thread per request worker (RunnerServer.MAX_WORKERS), its result
    sweeper and one yt-dlp run (it runs one job at a time). Imported here,
    not copied, so raising a cap without raising the example's pids_limit
    fails LayoutTest.
    """
    import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
    from strange_ringtone_fetch.server import FetchServer
    from strange_ringtone_forwarder.policy import DEFAULT_LIMITS
    from strange_ringtone_runner.server import RunnerServer

    return {
        "fetcher-forwarder": SERVICE_FIXED_THREADS + DEFAULT_LIMITS.connections_max + DEFAULT_LIMITS.tunnels_per_minute,
        "fetcher": SERVICE_FIXED_THREADS + FetchServer.MAX_WORKERS + JOB_THREADS + CLIENT_THREADS,
        "fetcher-runner": SERVICE_FIXED_THREADS + RunnerServer.MAX_WORKERS + RUNNER_THREADS + YTDLP_RUN_THREADS,
    }

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

# Run in the host's network namespace: whether each named interface exists
# there (/proc/net/dev, which shows the reader's own netns) and its IPv6
# addresses (/proc/net/if_inet6), canonical text. A bridge without IPv6 is
# absent from if_inet6; the existence check makes "no address" mean something.
_BRIDGE_V6_PROBE = r"""
import ipaddress, json
with open('/proc/net/dev', encoding='ascii') as handle:
    present = {line.split(':', 1)[0].strip() for line in handle.read().splitlines()[2:] if ':' in line}
found = {}
try:
    with open('/proc/net/if_inet6', encoding='ascii') as handle:
        lines = handle.read().splitlines()
except FileNotFoundError:
    lines = []
for line in lines:
    fields = line.split()
    if len(fields) != 6:
        raise SystemExit('unexpected /proc/net/if_inet6 line')
    found.setdefault(fields[5], []).append(str(ipaddress.IPv6Address(int(fields[0], 16))))
print(json.dumps({name: {'present': name in present, 'ipv6': sorted(found.get(name, []))} for name in BRIDGES}))
"""

# Inside a container: each interface's disable_ipv6 and IPv6 addresses. An
# interface with IPv6 disabled, or with no IPv6 address, can't reach the host
# over IPv6 whatever the bridge holds.
_IFACE_V6_PROBE = r"""
import ipaddress, json
found = {}
try:
    with open('/proc/net/if_inet6', encoding='ascii') as handle:
        lines = handle.read().splitlines()
except FileNotFoundError:
    lines = []
for line in lines:
    fields = line.split()
    if len(fields) != 6:
        raise SystemExit('unexpected /proc/net/if_inet6 line')
    found.setdefault(fields[5], []).append(str(ipaddress.IPv6Address(int(fields[0], 16))))
with open('/proc/net/dev', encoding='ascii') as handle:
    names = sorted(line.split(':', 1)[0].strip() for line in handle.read().splitlines()[2:] if ':' in line)
out = {}
for name in names:
    try:
        with open(f'/proc/sys/net/ipv6/conf/{name}/disable_ipv6', encoding='ascii') as handle:
            disabled = handle.read().strip()
    except FileNotFoundError:
        disabled = 'absent'
    out[name] = {'disable_ipv6': disabled, 'ipv6': sorted(found.get(name, []))}
print(json.dumps(out))
"""

# Connect to each IPv6 target on PORT and read what the listener says; the
# outcome is 'connected:<banner>' or the exception's class name.
_V6_CONNECT_PROBE = r"""
import json, socket
out = {}
for target in TARGETS:
    try:
        with socket.create_connection((target, PORT), timeout=TIMEOUT) as connection:
            out[target] = 'connected:' + connection.recv(64).decode('ascii', 'replace')
    except OSError as error:
        out[target] = type(error).__name__
print(json.dumps(out))
"""

# A listener in the host's network namespace, bound to one address only (the
# IPv6 control's bridge address, never all interfaces): it answers each
# connection with a fixed banner, so the probe can tell it reached the host.
_HOST_LISTENER = r"""
import socket
with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as server:
    server.bind((ADDRESS, PORT))
    server.listen(8)
    while True:
        connection, _peer = server.accept()
        with connection:
            connection.sendall(b'host-reached')
"""

# PID 1 of a service container is its command; the arguments are NUL-separated.
_CMDLINE_PROBE = r"""
import json
with open('/proc/1/cmdline', 'rb') as handle:
    print(json.dumps(handle.read().decode('ascii').split('\0')[:-1]))
"""

# Forwarding and the pids cap as the container's own kernel reports them: IPv4
# forwarding globally and per interface, IPv6 forwarding per interface where
# the kernel has IPv6 (None where a file is absent), and the cgroup's pids.max
# (cgroup v2, or v1's pids controller).
_FORWARDING_PROBE = r"""
import json, os

def read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return None

def per_interface(family, key='forwarding'):
    base = '/proc/sys/net/' + family + '/conf'
    if not os.path.isdir(base):
        return {}
    return {name: read(base + '/' + name + '/' + key) for name in sorted(os.listdir(base))}

print(json.dumps({
    'ip_forward': read('/proc/sys/net/ipv4/ip_forward'),
    'ipv4': per_interface('ipv4'),
    'ipv6': per_interface('ipv6'),
    'ipv6_disabled': per_interface('ipv6', 'disable_ipv6'),
    'pids_max': read('/sys/fs/cgroup/pids.max') or read('/sys/fs/cgroup/pids/pids.max'),
}))
"""

# Every IPv4 accept_redirects value the container's kernel holds: all, default
# and one per interface, lo included.
_REDIRECTS_PROBE = r"""
import json, os
base = '/proc/sys/net/ipv4/conf'
values = {}
for name in sorted(os.listdir(base)):
    with open(base + '/' + name + '/accept_redirects') as handle:
        values[name] = handle.read().strip()
print(json.dumps(values))
"""

# A UDP listener on a copy of `outside`: it says `ready`, then prints each
# datagram's text and source address, until LIFETIME seconds pass in silence.
_ROUTED_LISTENER = r"""
import socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
    server.bind(('0.0.0.0', PORT))
    server.settimeout(LIFETIME)
    print('ready', flush=True)
    while True:
        try:
            data, peer = server.recvfrom(64)
        except socket.timeout:
            break
        print(data.decode('ascii', 'replace') + ' ' + peer[0], flush=True)
"""

# The helper on a copy of `egress` (root, NET_ADMIN and nothing else): for each
# phase it points a /32 route to the listener at that phase's router (netlink
# RTM_NEWROUTE, create or replace, in the main table), then sends the phase's
# name in UDP datagrams to the listener. It prints netlink's answer per phase
# (0 is success). A router forwards them only if the interface they arrive on
# has forwarding on.
_ROUTING_HELPER = r"""
import json, socket, struct, time
RTM_NEWROUTE, NLMSG_ERROR = 24, 2
FLAGS = 0x1 | 0x4 | 0x400 | 0x100  # NLM_F_REQUEST, ACK, CREATE, REPLACE

def set_route(destination, gateway):
    attributes = b''
    for kind, value in ((1, socket.inet_aton(destination)), (5, socket.inet_aton(gateway))):  # RTA_DST, RTA_GATEWAY
        attributes += struct.pack('=HH', 4 + len(value), kind) + value
    # rtmsg: AF_INET, /32, no source, tos 0, RT_TABLE_MAIN, RTPROT_BOOT,
    # RT_SCOPE_UNIVERSE, RTN_UNICAST, no flags.
    body = struct.pack('=BBBBBBBBI', socket.AF_INET, 32, 0, 0, 254, 3, 0, 1, 0) + attributes
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as sock:
        sock.bind((0, 0))
        sock.sendall(struct.pack('=IHHII', 16 + len(body), RTM_NEWROUTE, FLAGS, 1, 0) + body)
        reply = sock.recv(65536)
    if struct.unpack('=H', reply[4:6])[0] != NLMSG_ERROR:
        return 'unexpected reply'
    return struct.unpack('=i', reply[16:20])[0]

answers = {}
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
    for phase, router in PHASES:
        answers[phase] = set_route(LISTENER, router)
        for _ in range(COUNT):
            sender.sendto(phase.encode('ascii'), (LISTENER, PORT))
            time.sleep(INTERVAL)
print(json.dumps(answers))
"""

# The IPv4 counters of the reader's network namespace (/proc/net/snmp, the
# `Ip:` header line and its value line).
_IP_COUNTERS_PROBE = r"""
import json
with open('/proc/net/snmp', encoding='ascii') as handle:
    lines = [line.split() for line in handle.read().splitlines() if line.startswith('Ip:')]
print(json.dumps(dict(zip(lines[0][1:], lines[1][1:]))))
"""

# http.client to loopback, as the service's own healthcheck does.
_HEALTH_PROBE = r"""
import http.client
connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=TIMEOUT)
connection.request('GET', PATH)
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


def _bridge_v6_probe(bridges: list[str]) -> str:
    return f"BRIDGES = {bridges!r}\n" + _BRIDGE_V6_PROBE


def _v6_connect_probe(targets: list[str], port: int) -> str:
    return f"TARGETS = {targets!r}\nPORT = {port!r}\nTIMEOUT = {PROBE_TIMEOUT!r}\n" + _V6_CONNECT_PROBE


def _health_probe(port: int = FETCHER_PORT, path: str = FETCHER_HEALTH_PATH) -> str:
    return f"PORT = {port!r}\nPATH = {path!r}\nTIMEOUT = {PROBE_TIMEOUT!r}\n" + _HEALTH_PROBE


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
        "  fetcher-runner:\n"
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
            "up", "-d", "--no-build", "--wait", "--wait-timeout", "90", "fetcher", "fetcher-runner",
            "fetcher-forwarder", check=True,
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
        """T1/T2 (static): the fetcher and the runner only on internal networks, the forwarder alone on `outside`."""
        services = self._services()
        self.assertLessEqual({"core", "fetcher", "fetcher-runner", "fetcher-forwarder"}, set(services))
        networks = self._config().get("networks") or {}
        for name in ("core", "fetcher", "jobs", "egress", "outside"):
            self.assertIn(name, networks, f"compose.example.yaml has no {name!r} network")
        # internal: no route out and no external DNS (with Engine 26.0+).
        self.assertIs(networks["egress"].get("internal"), True, "egress must be internal")
        self.assertIs(networks["jobs"].get("internal"), True, "jobs must be internal")
        self.assertIs(networks["fetcher"].get("internal"), True, "fetcher must be internal")
        self.assertIs(networks["core"].get("internal"), True, "core must be internal")
        # compose omits `internal: false`; a missing key is the normal network.
        self.assertIs(networks["outside"].get("internal", False), False, "outside is the forwarder's way out")
        # The fetcher: the reverse proxy's network and the runner's, never
        # `egress`. The runner: the fetcher's and the forwarder's, never the
        # reverse proxy's. The forwarder: the runner's and the way out.
        self.assertEqual(self._networks_of(services["fetcher"]), {"fetcher", "jobs"})
        self.assertEqual(self._networks_of(services["fetcher-runner"]), {"jobs", "egress"})
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

    def test_internal_networks_give_the_host_no_ipv6_address_either(self) -> None:
        """T1/T2 (static): every internal network also sets the IPv6 isolated mode; `outside` doesn't."""
        networks = self._config().get("networks") or {}
        for name in INTERNAL_NETWORKS:
            with self.subTest(network=name):
                self.assertIn(name, networks, f"compose.example.yaml has no {name!r} network")
                options = networks[name].get("driver_opts") or {}
                # The IPv4 mode alone gives the host an IPv6 address on the
                # bridge again once IPv6 is turned on for the network.
                self.assertEqual(options.get(GATEWAY_MODE_V6), "isolated", f"{name} gives the host an IPv6 address")
        self.assertIn("outside", networks)
        outside = (networks["outside"].get("driver_opts") or {}).get(GATEWAY_MODE_V6)
        self.assertNotEqual(outside, "isolated", "outside needs its gateway: it is the forwarder's route out")

    def test_every_service_rotates_its_log(self) -> None:
        """Supporting (G10): a json-file log with max-size and max-file on every service, so no log fills the disk."""
        for name, service in self._services().items():
            with self.subTest(service=name):
                logging = service.get("logging") or {}
                self.assertEqual(logging.get("driver"), LOGGING["driver"], f"{name}: {logging!r}")
                self.assertEqual(logging.get("options"), LOGGING["options"], f"{name}: {logging!r}")

    def test_runner_points_https_proxy_at_the_forwarder_and_the_fetcher_has_none(self) -> None:
        """T2 (static): yt-dlp's only way out is the forwarder, set on the runner so .env can't change it."""
        runner = self._service("fetcher-runner")
        environment = runner.get("environment") or {}
        self.assertEqual(environment.get("HTTPS_PROXY"), PROXY_URL)
        # `up -d fetcher-runner` must bring the forwarder up too.
        self.assertIn("fetcher-forwarder", runner.get("depends_on") or {})
        # The fetcher talks to the runner only; it runs no yt-dlp and reads
        # no proxy variable, so the example gives it none.
        fetcher = self._service("fetcher")
        fetcher_environment = fetcher.get("environment") or {}
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
            self.assertNotIn(name, fetcher_environment, f"the fetcher sets {name}")
        # `docker compose up -d fetcher` must bring the runner (and with it
        # the forwarder) up too.
        self.assertIn("fetcher-runner", fetcher.get("depends_on") or {})

    def test_the_fetcher_finds_the_runner_at_its_compose_service(self) -> None:
        """T2 (static): the fetcher's FETCH_RUNNER_URL is unset (its default) or exactly the runner service."""
        # Pointed anywhere else (the forwarder, say), the fetcher's jobs
        # would go to a peer the layout never meant it to talk to.
        default = "http://fetcher-runner:8080"
        environment = self._service("fetcher").get("environment") or {}
        self.assertIsInstance(environment, dict, environment)
        self.assertEqual(environment.get("FETCH_RUNNER_URL", default), default)
        # Acceptance: the default names a service this file defines, on `jobs`.
        runner = self._service("fetcher-runner")
        self.assertIn("jobs", self._networks_of(runner))

    def test_the_fetcher_trusts_no_proxy_whatever_the_env_file_says(self) -> None:
        """Q3 (owner, 2026-11-05): the fetcher's own TRUSTED_PROXIES is empty and wins over the shared .env."""
        # The runner reaches the fetcher over `jobs`, whose subnet Docker may
        # change when it recreates the network: a CIDR in the fetcher's
        # TRUSTED_PROXIES could come to cover the runner and let it write
        # its own X-Forwarded-For past the rate limit.
        environment = self._service("fetcher").get("environment") or {}
        self.assertIn("TRUSTED_PROXIES", environment, "the fetcher's environment doesn't set TRUSTED_PROXIES")
        self.assertEqual(environment["TRUSTED_PROXIES"], "")
        # With a CIDR in the env file, as an operator would set for the core:
        # the fetcher still resolves to empty, and the core (control) gets
        # the CIDR, so the file was read and the override is what wins.
        stack = Stack()
        try:
            with open(stack.test_env, "a", encoding="ascii", newline="\n") as handle:
                handle.write("TRUSTED_PROXIES=192.0.2.0/24\n")
            result = stack.compose("config", "--format", "json")
        finally:
            stack.close()
        self.assertEqual(result.returncode, 0, f"docker compose config: {result.stderr.strip()}")
        services = json.loads(result.stdout).get("services") or {}
        fetcher = (services.get("fetcher") or {}).get("environment") or {}
        core = (services.get("core") or {}).get("environment") or {}
        self.assertEqual(core.get("TRUSTED_PROXIES"), "192.0.2.0/24", core)
        self.assertEqual(fetcher.get("TRUSTED_PROXIES"), "", fetcher)

    def test_the_runner_stays_its_containers_pid_1(self) -> None:
        """Q2 (owner, 2026-11-05): no init and no wrapper, so the runner is PID 1 and sweeps only its container."""
        # The runner turns on its kill-and-reap sweep only as PID 1. Under an
        # init it would never sweep, and the orphans of every run would be
        # left to the init. StackTest's cmdline probe reads /proc/1 in the
        # running container.
        runner = self._service("fetcher-runner")
        self.assertFalse(runner.get("init"), f"the runner runs under an init: {runner.get('init')!r}")
        # `compose config` writes `entrypoint: null` when none is set.
        entrypoint = runner.get("entrypoint")
        self.assertIn(entrypoint, (None, []), f"the runner has an entrypoint: {entrypoint!r}")
        self.assertEqual(runner.get("command"), RUNNER_COMMAND)
        # The image adds no entrypoint in front of the command either.
        result = _docker("image", "inspect", "--format", "{{json .Config.Entrypoint}}", IMAGE,
                         timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        self.assertIn(json.loads(result.stdout), (None, []), "the image has an entrypoint")

    def test_the_runner_pins_init_off_against_the_daemons_default(self) -> None:
        """Q2: `init: false`, stated, because an unset key takes the daemon's default, which may be init."""
        # A daemon started with --init (daemon.json "init": true) runs every
        # container whose HostConfig.Init is unset under docker-init; the
        # runner would then be PID 7, not 1, and could not sweep (since Q4 it
        # then refuses to start). Only an explicit false overrides that default.
        runner = self._service("fetcher-runner")
        self.assertIn("init", runner, "the runner leaves init to the daemon's default")
        self.assertIs(runner["init"], False, f"the runner's init is {runner['init']!r}")

    def test_the_runner_is_never_permitted_to_serve_without_its_sweep(self) -> None:
        """Q4 (owner, 2026-11-05): the example never sets FETCH_ALLOW_NO_SWEEP, so a runner that can't sweep refuses."""
        # The override is for a local run or a host that can't run the
        # runner as PID 1, never for this layout, which can: set here, it
        # would let a runner started under an init serve without its sweep.
        runner = self._service("fetcher-runner")
        environment = runner.get("environment") or {}
        self.assertIsInstance(environment, dict, environment)
        # Control: the resolved environment is the one compose builds (the
        # forwarder's address is set there), so its absence is meaningful.
        self.assertIn("HTTPS_PROXY", environment)
        self.assertNotIn("FETCH_ALLOW_NO_SWEEP", environment)
        for name, service in self._services().items():
            with self.subTest(service=name):
                self.assertNotIn("FETCH_ALLOW_NO_SWEEP", service.get("environment") or {})
        text = COMPOSE_FILE.read_text(encoding="utf-8")
        setting = [line for line in text.splitlines()
                   if "FETCH_ALLOW_NO_SWEEP" in line and not line.lstrip().startswith("#")]
        self.assertEqual(setting, [], "compose.example.yaml sets FETCH_ALLOW_NO_SWEEP")

    def test_no_service_has_a_volume_and_only_tmp_is_writable(self) -> None:
        """Stateless (CLAUDE.md): no volume, a read-only root, and /tmp's tmpfs the only writable mount."""
        config = self._config()
        self.assertFalse(config.get("volumes"), f"compose.example.yaml declares volumes: {config.get('volumes')!r}")
        for name, service in self._services().items():
            with self.subTest(service=name):
                # No data directory, bind mount or named volume: the
                # services write only to /tmp, and the forwarder nothing.
                self.assertNotIn("volumes", service, f"{name} mounts {service.get('volumes')!r}")
                self.assertNotIn("volumes_from", service, f"{name} mounts another service's volumes")
                self.assertIs(service.get("read_only"), True, f"{name} has a writable root filesystem")
                tmpfs = service.get("tmpfs") or []
                if name == "fetcher-forwarder":
                    self.assertEqual(tmpfs, [], f"the forwarder writes nothing: {tmpfs!r}")
                    continue
                self.assertEqual(len(tmpfs), 1, f"{name}: {tmpfs!r}")
                self.assertTrue(tmpfs[0].startswith("/tmp:"), f"{name}: {tmpfs!r}")

    def test_runner_command_and_hardening(self) -> None:
        """T2 (static): the runner command of the fetcher image, with the contract's flags and /tmp for one download."""
        runner = self._service("fetcher-runner")
        self.assertEqual(runner.get("command"), RUNNER_COMMAND)
        self.assertEqual(runner.get("user"), UID_GID)
        self.assertIs(runner.get("read_only"), True)
        self.assertEqual(runner.get("cap_drop"), ["ALL"])
        self.assertIn("no-new-privileges:true", runner.get("security_opt") or [])
        self.assertEqual(runner.get("ipc"), "none")
        tmpfs = runner.get("tmpfs") or []
        self.assertEqual(len(tmpfs), 1, tmpfs)
        self.assertTrue(tmpfs[0].startswith("/tmp:"), tmpfs)
        self.assertIn(RUNNER_TMPFS_SIZE, tmpfs[0].split(":", 1)[1].split(","))
        # Deno reserves a huge address space, so prlimit can't bound memory:
        # the cgroup limit does.
        self.assertIn("mem_limit", runner)
        # No env_file check: `compose config` folds env_file into
        # `environment` and drops the key, and the test override sets the
        # runner's env_file anyway, so neither form would tell whether
        # compose.example.yaml gives it one.

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

    def test_fetcher_hardening_and_tmpfs_for_two_downloads(self) -> None:
        """T2 (static): the fetcher's hardening, and a /tmp for one download from the runner and one result."""
        fetcher = self._service("fetcher")
        self.assertEqual(fetcher.get("user"), UID_GID)
        self.assertIs(fetcher.get("read_only"), True)
        self.assertEqual(fetcher.get("cap_drop"), ["ALL"])
        self.assertIn("no-new-privileges:true", fetcher.get("security_opt") or [])
        self.assertEqual(fetcher.get("ipc"), "none")
        tmpfs = fetcher.get("tmpfs") or []
        self.assertEqual(len(tmpfs), 1, tmpfs)
        self.assertTrue(tmpfs[0].startswith("/tmp:"), tmpfs)
        self.assertIn(FETCHER_TMPFS_SIZE, tmpfs[0].split(":", 1)[1].split(","))
        self.assertIn("mem_limit", fetcher)

    def test_the_fetcher_image_services_turn_ip_forwarding_off(self) -> None:
        """T2 (static): no kernel routing between the networks of a two-network container, around the CONNECT gate."""
        for name in FETCHER_IMAGE_SERVICES:
            with self.subTest(service=name):
                service = self._service(name)
                sysctls = service.get("sysctls") or {}
                self.assertIsInstance(sysctls, dict, sysctls)
                # A new network namespace copies the host's 1, and the kernel
                # forwards whatever arrives on an interface whose forwarding
                # is on, so default (interfaces made later) and lo are 0 here
                # and each attached interface is 0 below.
                for key in FORWARDING_SYSCTLS:
                    self.assertEqual(str(sysctls.get(key)), "0", f"{name}: {key} in {sysctls!r}")
                # Never the global switch: writing it resets
                # all.accept_redirects to 1, and Docker writes sysctls in no
                # fixed order, so the redirect refusal would hold only in
                # some starts.
                for key in GLOBAL_FORWARDING_SYSCTLS:
                    self.assertNotIn(key, sysctls, f"{name} writes the global forwarding switch {key}")
                # IPv6 forwarding is already 0 there (StackTest), and an IPv6
                # sysctl would stop the container from starting on a kernel
                # without IPv6.
                self.assertEqual([key for key in sysctls if key.startswith("net.ipv6.")], [], sysctls)
                networks = service.get("networks") or {}
                self.assertIsInstance(networks, dict, networks)
                self.assertTrue(networks, f"{name} joins no network")
                for network, attachment in networks.items():
                    entries = _endpoint_sysctls((attachment or {}).get("driver_opts") or {})
                    # Exactly one setting each, 0, on the interface Docker
                    # creates for this attachment.
                    self.assertEqual(entries.get(IFNAME_FORWARDING), ["0"], f"{name} on {network}: {entries!r}")
                    self.assertEqual(entries.get(IFNAME_ACCEPT_REDIRECTS), ["0"], f"{name} on {network}: {entries!r}")
                    for key in GLOBAL_FORWARDING_SYSCTLS:
                        self.assertNotIn(key, entries, f"{name} on {network}: {entries!r}")

    def test_the_fetcher_image_services_cap_processes_at_twice_their_worst_case(self) -> None:
        """Supporting (G8): a cgroup pids_limit of at least twice each service's worst case from its own caps."""
        for name, worst in _worst_case_threads().items():
            with self.subTest(service=name, worst=worst):
                limit = self._service(name).get("pids_limit")
                # A bool is an int; -1 and 0 mean no limit to Docker.
                self.assertIsInstance(limit, int, f"{name} sets no pids_limit")
                self.assertNotIsInstance(limit, bool)
                self.assertGreaterEqual(
                    limit, 2 * worst,
                    f"{name}: pids_limit {limit} is under twice its worst case of {worst} threads; "
                    f"raise it with the cap, and the sizing in compose.example.yaml's comment and the README",
                )

    def test_the_fetcher_image_services_refuse_icmp_redirects(self) -> None:
        """T2 (static): accept_redirects 0 for all, default and lo, and on the interface of every network attachment."""
        for name in FETCHER_IMAGE_SERVICES:
            with self.subTest(service=name):
                service = self._service(name)
                sysctls = service.get("sysctls") or {}
                self.assertIsInstance(sysctls, dict, sysctls)
                # Forwarding off sets `all` to 1, and with forwarding off any
                # one value of 1 on the path lets a redirect in.
                for key in ACCEPT_REDIRECTS_SYSCTLS:
                    self.assertEqual(str(sysctls.get(key)), "0", f"{name}: {key} in {sysctls!r}")
                networks = service.get("networks") or {}
                self.assertIsInstance(networks, dict, networks)
                self.assertTrue(networks, f"{name} joins no network")
                for network, attachment in networks.items():
                    options = (attachment or {}).get("driver_opts") or {}
                    entries = [entry.strip() for entry in str(options.get(ENDPOINT_SYSCTLS, "")).split(",")]
                    values = [entry.partition("=")[2] for entry in entries
                              if entry.partition("=")[0] == IFNAME_ACCEPT_REDIRECTS]
                    # Exactly one setting, 0: interface names follow the attach
                    # order, so each attachment sets its own interface.
                    self.assertEqual(values, ["0"], f"{name} on {network}: {options!r}")


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
        # One batch for both T3 tests, sent from a runner container (on
        # `egress`, the only container that reaches the forwarder), so each
        # test also passes when run alone.
        if type(self)._t3_result is None:
            type(self)._t3_result = self._stack.run_in(
                "fetcher-runner", _request_probe("fetcher-forwarder", FORWARDER_PORT, T3_REQUESTS),
            )
        assert type(self)._t3_result is not None
        lines = self._probe_output(type(self)._t3_result, "T3 request probe")
        self.assertIsInstance(lines, list)
        return lines

    def _wait_for_health(self, name: str, port: int, path: str) -> str:
        """The health probe's last answer from a container started with plain `docker run`, within the wait."""
        deadline = time.monotonic() + OVERRIDE_HEALTH_SECONDS
        health = ""
        while time.monotonic() < deadline:
            probe = _docker("exec", name, "python", "-I", "-c", _health_probe(port, path), timeout=PROBE_RUN_TIMEOUT)
            health = probe.stdout.strip()
            if probe.returncode == 0 and health == "200 ok":
                break
            time.sleep(0.5)
        return health

    def _interfaces_without_ipv6(self, service: str) -> None:
        """The running service has IPv6 off (or no address) on its two non-loopback interfaces."""
        interfaces = self._probe_output(self._stack.exec_in(service, _IFACE_V6_PROBE), f"{service} IPv6 probe")
        others = {name: state for name, state in interfaces.items() if name != "lo"}
        # Control: the service is on two networks, so two interfaces.
        self.assertEqual(len(others), 2, f"{service}: {interfaces}")
        for interface, state in others.items():
            self.assertTrue(state["disable_ipv6"] == "1" or not state["ipv6"],
                            f"{service} has IPv6 on {interface}: {interfaces}")

    # T1

    def test_t1_fetcher_has_no_default_route_and_no_external_dns(self) -> None:
        """T1: the fetcher has no default route (IPv4 or IPv6), resolves its runner, not the forwarder, and is up."""
        result = self._probe_output(
            self._stack.run_in("fetcher", _routes_probe([EXTERNAL_NAME, "fetcher-runner", "fetcher-forwarder"])),
            "fetcher route probe",
        )
        self.assertEqual(result["ipv4_default"], [], "the fetcher has an IPv4 default route")
        self.assertEqual(result["ipv6_default"], [], "the fetcher has an IPv6 default route")
        # Control: the embedded DNS answers for the fetcher's own peer, so
        # the failures below mean something.
        self.assertIs(result["resolves"].get("fetcher-runner"), True, result)
        # The fetcher shares no network with the forwarder, so the embedded
        # DNS doesn't even name it there.
        self.assertIs(result["resolves"].get("fetcher-forwarder"), False, result)
        self.assertIs(result["resolves"].get(EXTERNAL_NAME), False, result)
        health = self._stack.exec_in("fetcher", _health_probe(FETCHER_PORT, FETCHER_HEALTH_PATH))
        self.assertEqual(health.returncode, 0, health.stderr.strip())
        self.assertEqual(health.stdout.strip(), "200 ok")

    def test_t1_runner_has_no_default_route_and_no_external_dns(self) -> None:
        """T1: the runner has no default route (IPv4 or IPv6), resolves the forwarder, no external name, and is up."""
        result = self._probe_output(
            self._stack.run_in("fetcher-runner", _routes_probe([EXTERNAL_NAME, "fetcher-forwarder"])),
            "runner route probe",
        )
        self.assertEqual(result["ipv4_default"], [], "the runner has an IPv4 default route")
        self.assertEqual(result["ipv6_default"], [], "the runner has an IPv6 default route")
        # Control: the embedded DNS answers for the runner's own peer, so the
        # external failure means something.
        self.assertIs(result["resolves"].get("fetcher-forwarder"), True, result)
        self.assertIs(result["resolves"].get(EXTERNAL_NAME), False, result)
        health = self._stack.exec_in("fetcher-runner", _health_probe(RUNNER_PORT, RUNNER_HEALTH_PATH))
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
        health = self._wait_for_health(name, FETCHER_PORT, FETCHER_HEALTH_PATH)
        self.assertEqual(health, "200 ok", "the fetcher did not serve healthz with the override set")
        logs = _docker("logs", name, timeout=PROBE_RUN_TIMEOUT)
        output = logs.stdout + logs.stderr
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE is set", output)
        self.assertNotIn("refusing to start", output)

    def test_t1_runner_refuses_to_start_on_the_outside_network(self) -> None:
        """T1/S2: the runner's command on a network with a route out exits 3 and says why; it needs no setting."""
        name = self._stack.container_name("runner-refusal")
        try:
            result = self._stack.docker_run(
                "--rm", "--name", name, "--network", self._stack.network("outside"),
                *HARDENING, *FETCHER_TMPFS, IMAGE, *RUNNER_COMMAND,
            )
        except subprocess.TimeoutExpired:
            self.fail("the runner kept running on the outside network instead of refusing to start")
        self.assertEqual(result.returncode, EXIT_ROUTE, result.stderr.strip())
        self.assertIn("refusing to start", result.stderr)
        self.assertIn("has a default route", result.stderr)
        self.assertIn("IPv4", result.stderr)
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", result.stderr)

    def test_t1_the_override_lets_the_runner_start_there(self) -> None:
        """T1/S2: the runner with FETCH_ALLOW_DEFAULT_ROUTE=true starts there, serves /healthz and says so."""
        name = self._stack.container_name("runner-override")
        self.addCleanup(_docker, "rm", "-f", name, timeout=60)
        started = self._stack.docker_run(
            "-d", "--name", name, "--network", self._stack.network("outside"),
            *HARDENING, *FETCHER_TMPFS, "-e", "FETCH_ALLOW_DEFAULT_ROUTE=true", IMAGE, *RUNNER_COMMAND,
        )
        self.assertEqual(started.returncode, 0, started.stderr.strip())
        health = self._wait_for_health(name, RUNNER_PORT, RUNNER_HEALTH_PATH)
        self.assertEqual(health, "200 ok", "the runner did not serve /healthz with the override set")
        logs = _docker("logs", name, timeout=PROBE_RUN_TIMEOUT)
        output = logs.stdout + logs.stderr
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE is set", output)
        self.assertNotIn("refusing to start", output)

    def test_t1_internal_networks_give_the_host_no_address(self) -> None:
        """T1: the Docker host has no address on the `fetcher`, `jobs` and `egress` bridges, for either to reach."""
        bridges: dict[str, str] = {}
        for name in ("fetcher", "jobs", "egress", "outside"):
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
        for network in ("fetcher", "jobs", "egress"):
            self.assertEqual(addresses.get(bridges[network]), "EADDRNOTAVAIL",
                             f"the host has an address on {network}: {addresses}")

    def test_t1_the_host_has_no_ipv6_address_the_fetcher_or_runner_can_reach(self) -> None:
        """T1: the `fetcher`, `jobs` and `egress` bridges hold no IPv6 host address but link-local; no IPv6 inside."""
        bridges: dict[str, str] = {}
        for name in ("fetcher", "jobs", "egress", "outside"):
            network = self._stack.network(name)
            result = _docker("network", "inspect", "--format", "{{json .}}", network, timeout=PROBE_RUN_TIMEOUT)
            self.assertEqual(result.returncode, 0, f"docker network inspect {network}: {result.stderr.strip()}")
            info = json.loads(result.stdout)
            bridges[name] = "br-" + info["Id"][:12]
            if name != "outside":
                self.assertEqual((info.get("Options") or {}).get(GATEWAY_MODE_V6), "isolated", network)
        name = self._stack.container_name("host-v6-probe")
        seen = self._probe_output(
            self._stack.docker_run(
                "--rm", "--name", name, "--network", "host", *HARDENING, IMAGE,
                "python", "-I", "-c", _bridge_v6_probe(sorted(bridges.values())),
            ),
            "host IPv6 bridge probe",
        )
        # Control: the probe runs in the host's netns and finds all four
        # bridges, so an empty address list is a fact, not a wrong namespace.
        for network, bridge in bridges.items():
            self.assertIs((seen.get(bridge) or {}).get("present"), True, f"{network} bridge not found: {seen}")
        for network in ("fetcher", "jobs", "egress"):
            # A bridge may keep an fe80:: link-local address; nothing else.
            routable = [address for address in seen[bridges[network]]["ipv6"]
                        if not ipaddress.IPv6Address(address).is_link_local]
            self.assertEqual(routable, [], f"the host has an IPv6 address on {network}: {seen}")
        # And the running fetcher and runner have IPv6 off (or no address) on
        # every interface but loopback, so even a link-local host address is
        # out of their reach.
        for service in ("fetcher", "fetcher-runner"):
            with self.subTest(service=service):
                self._interfaces_without_ipv6(service)

    def test_the_image_and_the_services_run_the_base_interpreter_isolated(self) -> None:
        """I1: the image's CMD and the three running services use the base interpreter with -I, never the venv's."""
        result = _docker("image", "inspect", "--format", "{{json .Config.Cmd}}", IMAGE, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        self.assertEqual(json.loads(result.stdout), FETCHER_COMMAND, "the image's CMD changed")
        for service, expected in (
            ("fetcher", FETCHER_COMMAND),
            ("fetcher-runner", RUNNER_COMMAND),
            ("fetcher-forwarder", FORWARDER_COMMAND),
        ):
            with self.subTest(service=service):
                running = self._probe_output(self._stack.exec_in(service, _CMDLINE_PROBE), f"{service} cmdline")
                self.assertEqual(running, expected)

    def test_the_running_runner_is_pid_1_sweeps_and_is_not_dumpable(self) -> None:
        """Q2: the engine got init false, the runner says it sweeps, and a same-UID process can't read PID 1."""
        ids = self._stack.container_ids("fetcher-runner")
        self.assertEqual(len(ids), 1, ids)
        result = _docker("inspect", "--format", "{{json .HostConfig.Init}}", ids[0], timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        self.assertIs(json.loads(result.stdout), False, "the runner's container leaves init to the daemon")
        logs = self._stack.logs("fetcher-runner")
        self.assertEqual(logs.returncode, 0, logs.stderr.strip())
        self.assertIn("strange_ringtone_runner: PID 1: killing and reaping", logs.stdout + logs.stderr)
        # `compose exec` runs as the service's user, 10001, like yt-dlp. The
        # runner made itself non-dumpable, so its environ is closed to it;
        # cmdline (control) stays world-readable.
        probe = (
            "import json\n"
            "out = {}\n"
            "for name in ('cmdline', 'environ'):\n"
            "    try:\n"
            "        with open('/proc/1/' + name, 'rb') as handle:\n"
            "            out[name] = 'read' if handle.read() else 'empty'\n"
            "    except OSError as error:\n"
            "        out[name] = type(error).__name__\n"
            "print(json.dumps(out))\n"
        )
        state = self._probe_output(self._stack.exec_in("fetcher-runner", probe), "runner /proc/1 probe")
        self.assertEqual(state, {"cmdline": "read", "environ": "PermissionError"})

    def test_t2_the_running_services_forward_nothing_and_have_the_examples_pids_cap(self) -> None:
        """K1/T2: in the three running services, IPv4 and IPv6 forwarding read 0, and pids.max is the example's."""
        config = self._stack.compose("config", "--format", "json")
        self.assertEqual(config.returncode, 0, f"docker compose config: {config.stderr.strip()}")
        services = json.loads(config.stdout).get("services") or {}
        for service in FETCHER_IMAGE_SERVICES:
            with self.subTest(service=service):
                expected = (services.get(service) or {}).get("pids_limit")
                self.assertIsInstance(expected, int, f"{service} sets no pids_limit")
                state = self._probe_output(self._stack.exec_in(service, _FORWARDING_PROBE), f"{service} forwarding")
                # Per interface: the kernel forwards only what arrives on an
                # interface whose forwarding is on. Control: default, lo and
                # one interface per network are there to read. `all` (the
                # global switch, ip_forward) is left alone by design and
                # reads 1, so it isn't asserted.
                interfaces = {name for name in state["ipv4"] if name not in ("all", "default", "lo")}
                self.assertLessEqual({"default", "lo"}, set(state["ipv4"]), state)
                self.assertEqual(len(interfaces), 2, state)
                per_interface = {name: value for name, value in state["ipv4"].items() if name != "all"}
                self.assertEqual({name: value for name, value in per_interface.items() if value != "0"}, {}, state)
                # IPv6 forwarding is off without a sysctl of ours, wherever the
                # kernel has IPv6 at all.
                self.assertTrue({value for value in state["ipv6"].values()} <= {"0", None}, state)
                self.assertEqual(state["pids_max"], str(expected), state)

    def test_t2_the_running_services_refuse_icmp_redirects(self) -> None:
        """K1/T2: in the three running services, every accept_redirects value reads 0 (all, default, lo, eth)."""
        for service in FETCHER_IMAGE_SERVICES:
            with self.subTest(service=service):
                values = self._probe_output(self._stack.exec_in(service, _REDIRECTS_PROBE), f"{service} redirects")
                # Control: all, default, lo and one interface per network the
                # container is on, so the zeros are read from every interface.
                networks = self._addresses(service)
                interfaces = sorted(name for name in values if name not in ("all", "default", "lo"))
                self.assertLessEqual({"all", "default", "lo"}, set(values), values)
                self.assertEqual(len(interfaces), len(networks), f"{values} for {sorted(networks)}")
                self.assertEqual(len(interfaces), 2, values)
                self.assertEqual({name: value for name, value in values.items() if value != "0"}, {}, values)

    # T2

    def test_t2_runner_reaches_the_forwarder_over_egress(self) -> None:
        """T2: a GET from the runner to fetcher-forwarder:3128 gets the forwarder's 405 (acceptance)."""
        lines = self._probe_output(
            self._stack.run_in(
                "fetcher-runner", _request_probe("fetcher-forwarder", FORWARDER_PORT, [b"GET / HTTP/1.1\r\n\r\n"]),
            ),
            "runner to forwarder probe",
        )
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("HTTP/1.1 405"), lines)

    def test_t2_fetcher_cannot_reach_the_forwarder(self) -> None:
        """T2: the fetcher is not on `egress` and reaches neither forwarder address, but the runner on `jobs`."""
        fetcher = self._addresses("fetcher")
        runner = self._addresses("fetcher-runner")
        forwarder = self._addresses("fetcher-forwarder")
        self.assertNotIn(self._stack.network("egress"), fetcher, "the fetcher is on the egress network")
        self.assertNotIn(self._stack.network("outside"), fetcher, "the fetcher is on the outside network")
        on_egress = self._address(forwarder, "egress")
        on_outside = self._address(forwarder, "outside")
        # Control: the same probe from the same service reaches the runner
        # over `jobs`, so a failure to reach the forwarder is the network,
        # not the probe.
        control = self._address(runner, "jobs")
        outcomes = self._probe_output(
            self._stack.run_in(
                "fetcher",
                _connect_probe([(on_egress, FORWARDER_PORT), (on_outside, FORWARDER_PORT), (control, RUNNER_PORT)]),
            ),
            "fetcher reachability probe",
        )
        self.assertEqual(outcomes[2], "connected", f"control failed: {outcomes}")
        self._assert_unreachable(outcomes[0], f"the forwarder at {on_egress}:{FORWARDER_PORT}")
        self._assert_unreachable(outcomes[1], f"the forwarder at {on_outside}:{FORWARDER_PORT}")

    def test_t2_forwarder_cannot_reach_the_fetcher(self) -> None:
        """T2: the forwarder is on neither `fetcher` nor `jobs`, reaches neither fetcher address, but the runner."""
        fetcher = self._addresses("fetcher")
        runner = self._addresses("fetcher-runner")
        forwarder = self._addresses("fetcher-forwarder")
        self.assertNotIn(self._stack.network("fetcher"), forwarder, "the forwarder is on the fetcher network")
        self.assertNotIn(self._stack.network("jobs"), forwarder, "the forwarder is on the jobs network")
        on_fetcher = self._address(fetcher, "fetcher")
        on_jobs = self._address(fetcher, "jobs")
        # Control: the same probe reaches the runner over `egress`, so a
        # failure to reach the fetcher is the network, not the probe.
        control = self._address(runner, "egress")
        outcomes = self._probe_output(
            self._stack.run_in(
                "fetcher-forwarder",
                _connect_probe([(on_fetcher, FETCHER_PORT), (on_jobs, FETCHER_PORT), (control, RUNNER_PORT)]),
            ),
            "forwarder reachability probe",
        )
        self.assertEqual(outcomes[2], "connected", f"control failed: {outcomes}")
        self._assert_unreachable(outcomes[0], f"the fetcher at {on_fetcher}:{FETCHER_PORT}")
        self._assert_unreachable(outcomes[1], f"the fetcher at {on_jobs}:{FETCHER_PORT}")

    def test_t2_nothing_on_the_fetcher_network_reaches_the_forwarder_or_the_runner(self) -> None:
        """T2: a container on the `fetcher` network reaches the fetcher but neither forwarder nor runner address."""
        fetcher = self._addresses("fetcher")
        runner = self._addresses("fetcher-runner")
        forwarder = self._addresses("fetcher-forwarder")
        control = self._address(fetcher, "fetcher")
        targets = [
            (self._address(forwarder, "egress"), FORWARDER_PORT),
            (self._address(forwarder, "outside"), FORWARDER_PORT),
            (self._address(runner, "jobs"), RUNNER_PORT),
            (self._address(runner, "egress"), RUNNER_PORT),
        ]
        outcomes = self._probe_output(
            self._stack.probe_on_network(
                self._stack.network("fetcher"), _connect_probe([(control, FETCHER_PORT), *targets]),
            ),
            "fetcher-network probe",
        )
        self.assertEqual(outcomes[0], "connected", f"control failed: {outcomes}")
        self.assertEqual(len(outcomes), 1 + len(targets), outcomes)
        for (address, port), outcome in zip(targets, outcomes[1:], strict=True):
            self._assert_unreachable(outcome, f"{address}:{port}")

    def test_t2_nothing_on_the_jobs_network_reaches_the_forwarder(self) -> None:
        """T2: a container on the `jobs` network reaches the runner but neither forwarder address."""
        runner = self._addresses("fetcher-runner")
        forwarder = self._addresses("fetcher-forwarder")
        control = self._address(runner, "jobs")
        on_egress = self._address(forwarder, "egress")
        on_outside = self._address(forwarder, "outside")
        outcomes = self._probe_output(
            self._stack.probe_on_network(
                self._stack.network("jobs"),
                _connect_probe([(control, RUNNER_PORT), (on_egress, FORWARDER_PORT), (on_outside, FORWARDER_PORT)]),
            ),
            "jobs-network probe",
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


class Ipv6HostIsolationTest(unittest.TestCase):
    """T1 on IPv6: an internal network with IPv6 turned on and the example's options gives the host no address.

    The example's networks have no IPv6, but an operator can turn it on
    (`enable_ipv6: true`, or the daemon's `default-network-opts`) without
    touching the fetcher. The test creates two throwaway internal networks
    with IPv6, both named after a unique project: one with the `driver_opts`
    that compose.example.yaml gives `egress`, and a control with the IPv4
    isolated mode alone, where the host keeps an IPv6 address and a hardened
    container reaches a listener the test binds to it. Nothing is published
    and nothing leaves the host: the listener is bound to the control
    bridge's own address.
    """

    def setUp(self) -> None:
        self.stack = Stack()
        self.addCleanup(self.stack.close)

    def _network(self, suffix: str, options: dict[str, str]) -> str:
        name = self.stack.network(suffix)
        argv = ["network", "create", "--internal", "--ipv6"]
        for key, value in sorted(options.items()):
            argv += ["-o", f"{key}={value}"]
        result = _docker(*argv, name, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, f"docker network create {name}: {result.stderr.strip()}")
        # Cleanups run last in, first out: the containers on it go first.
        self.addCleanup(_docker, "network", "rm", name, timeout=60)
        return name

    def _container(self, suffix: str) -> str:
        name = f"{self.stack.project}-{suffix}"
        self.addCleanup(_docker, "rm", "-f", name, timeout=60)
        return name

    def _inspect(self, network: str) -> dict[str, Any]:
        result = _docker("network", "inspect", "--format", "{{json .}}", network, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, f"docker network inspect {network}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def _output(self, result: subprocess.CompletedProcess[str], what: str) -> Any:
        self.assertEqual(result.returncode, 0, f"{what} failed: {result.stderr.strip()}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"{what} printed nothing: {result.stderr.strip()}")
        return json.loads(lines[-1])

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # --pull never: a missing image must fail here, not fetch from a registry.
        return _docker("run", "--pull", "never", *args, timeout=PROBE_RUN_TIMEOUT)

    def test_an_ipv6_internal_network_with_the_examples_options_gives_the_host_no_address(self) -> None:
        """Both isolated modes: no IPv6 on the host's bridge; the IPv4 mode alone: the host is reachable (control)."""
        config = self.stack.compose("config", "--format", "json")
        self.assertEqual(config.returncode, 0, f"docker compose config: {config.stderr.strip()}")
        egress = (json.loads(config.stdout).get("networks") or {}).get("egress") or {}
        options = dict(egress.get("driver_opts") or {})
        self.assertEqual(options.get(GATEWAY_MODE), "isolated", options)
        isolated = self._network("v6-isolated", options)
        control = self._network("v6-control", {GATEWAY_MODE: "isolated"})
        bridges = {name: "br-" + self._inspect(name)["Id"][:12] for name in (isolated, control)}
        # A bridge comes up, and gets its addresses, once a container is on it.
        for network, suffix in ((isolated, "v6-sleeper-isolated"), (control, "v6-sleeper-control")):
            started = self._run(
                "-d", "--rm", "--name", self._container(suffix), "--network", network, *HARDENING, IMAGE,
                "python", "-I", "-c", "import time; time.sleep(300)",
            )
            self.assertEqual(started.returncode, 0, started.stderr.strip())
        seen = self._output(
            self._run("--rm", "--name", self._container("v6-host-probe"), "--network", "host", *HARDENING, IMAGE,
                      "python", "-I", "-c", _bridge_v6_probe(sorted(bridges.values()))),
            "host IPv6 bridge probe",
        )
        for network, bridge in bridges.items():
            self.assertIs((seen.get(bridge) or {}).get("present"), True, f"{network} bridge not found: {seen}")
        # Control: with the IPv4 mode alone the host holds a routable IPv6
        # address on the bridge, and a hardened container reaches a host
        # listener on it. Without this the assertion below proves nothing.
        routable = [address for address in seen[bridges[control]]["ipv6"]
                    if not ipaddress.IPv6Address(address).is_link_local]
        self.assertTrue(routable, f"control: the host has no IPv6 address on {control}: {seen}")
        listener = self._run(
            "-d", "--rm", "--name", self._container("v6-listener"), "--network", "host", *HARDENING, IMAGE,
            "python", "-I", "-c", f"ADDRESS = {routable[0]!r}\nPORT = {V6_CONTROL_PORT!r}\n" + _HOST_LISTENER,
        )
        self.assertEqual(listener.returncode, 0, listener.stderr.strip())
        deadline = time.monotonic() + OVERRIDE_HEALTH_SECONDS
        attempt = 0
        while True:
            attempt += 1
            reached = self._output(
                self._run("--rm", "--name", self._container(f"v6-control-probe-{attempt}"),
                          "--network", control, *HARDENING, IMAGE,
                          "python", "-I", "-c", _v6_connect_probe([routable[0]], V6_CONTROL_PORT)),
                "control reachability probe",
            )
            if reached.get(routable[0]) == "connected:host-reached" or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        self.assertEqual(reached.get(routable[0]), "connected:host-reached",
                         f"control: the host listener was not reachable over IPv6: {reached}")
        # The example's options: the host's bridge has no IPv6 address at
        # all, link-local included, so a container there has nothing of the
        # host's to connect to.
        self.assertEqual(seen[bridges[isolated]]["ipv6"], [],
                         f"the host has an IPv6 address on {isolated}: {seen}")
        subnets = [entry for entry in (self._inspect(isolated).get("IPAM") or {}).get("Config") or []
                   if ":" in (entry.get("Subnet") or "")]
        self.assertTrue(subnets, f"{isolated} has no IPv6 subnet")
        self.assertFalse(any(entry.get("Gateway") for entry in subnets),
                         f"{isolated} gives the host an IPv6 gateway: {subnets}")

    def test_ipv6_forwarding_is_off_on_an_ipv6_internal_network_with_the_examples_sysctls(self) -> None:
        """T2 on IPv6: with IPv6 on, the example's sysctls start the image and leave IPv6 forwarding at 0 unasked."""
        config = self.stack.compose("config", "--format", "json")
        self.assertEqual(config.returncode, 0, f"docker compose config: {config.stderr.strip()}")
        loaded = json.loads(config.stdout)
        options = dict(((loaded.get("networks") or {}).get("egress") or {}).get("driver_opts") or {})
        network = self._network("v6-forwarding", options)
        for service in FETCHER_IMAGE_SERVICES:
            with self.subTest(service=service):
                definition = (loaded.get("services") or {}).get(service) or {}
                sysctls = definition.get("sysctls") or {}
                for key in FORWARDING_SYSCTLS:
                    self.assertEqual(str(sysctls.get(key)), "0", sysctls)
                # The service's first network attachment, in name order
                # (`egress` for the runner and the forwarder; the fetcher is
                # not on `egress`): its endpoint sysctls set the interface
                # this network gets, as each attachment does for its own.
                attachments = definition.get("networks") or {}
                self.assertTrue(attachments, f"{service} joins no network")
                first = sorted(attachments)[0] if attachments else ""
                attachment = dict((attachments.get(first) or {}).get("driver_opts") or {})
                self.assertEqual(_endpoint_sysctls(attachment).get(IFNAME_FORWARDING), ["0"], attachment)
                flags: list[str] = []
                for key, value in sorted(sysctls.items()):
                    flags += ["--sysctl", f"{key}={value}"]
                state = self._output(
                    self._run("--rm", "--name", self._container(f"v6-forwarding-{service}"),
                              "--network", _network_flag(network, attachment),
                              *HARDENING, *flags, IMAGE, "python", "-I", "-c", _FORWARDING_PROBE),
                    f"{service} forwarding probe on {network}",
                )
                # IPv4 per interface (default, lo, eth0); `all` is the global
                # switch, left alone by design.
                self.assertLessEqual({"default", "lo", "eth0"}, set(state["ipv4"]), state)
                self.assertEqual({name: value for name, value in state["ipv4"].items()
                                  if name != "all" and value != "0"}, {}, state)
                # Control: IPv6 is really on for the container's interface,
                # so the zeros below are read from a live IPv6 stack.
                self.assertEqual(state["ipv6_disabled"].get("eth0"), "0", state)
                self.assertEqual({value for value in state["ipv6"].values()}, {"0"}, state)


class ForwardingBehaviourTest(unittest.TestCase):
    """T2 by behaviour: a container with the forwarder's settings routes nothing, although ip_forward reads 1.

    The example turns forwarding off per interface and leaves the global
    switch alone, so net.ipv4.ip_forward reads 1 inside; this shows that the
    kernel still forwards nothing. Two throwaway bridge networks named after
    a unique project stand in for `egress` and `outside`. Both are plain
    bridges: on an internal network Docker's own host rules drop a packet
    whose destination is off the network's subnet before it reaches any
    container (measured on Engine 29.8: the control router received
    nothing), which would hide the kernel's behaviour behind the host's. On
    both, the router under test runs the forwarder's command with the
    forwarder's sysctls and endpoint sysctls from `compose config`, and a
    control router has forwarding on (net.ipv4.ip_forward=1). A listener
    sits on the `outside` stand-in, and a helper on the `egress` stand-in
    sends datagrams to it through the router under test first and then
    through the control, which must deliver them or the setup proves
    nothing. The router's own counters show the datagrams reached it. Only
    the helper has a capability (root with NET_ADMIN alone, to set its own
    route). Nothing is published and nothing leaves the host: every
    destination is a container's address on these two networks.
    """

    def setUp(self) -> None:
        self.stack = Stack()
        self.addCleanup(self.stack.close)

    def _network(self, suffix: str) -> str:
        name = self.stack.network(suffix)
        result = _docker("network", "create", name, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, f"docker network create {name}: {result.stderr.strip()}")
        # Cleanups run last in, first out: the containers on it go first.
        self.addCleanup(_docker, "network", "rm", name, timeout=60)
        return name

    def _container(self, suffix: str) -> str:
        name = f"{self.stack.project}-{suffix}"
        self.addCleanup(_docker, "rm", "-f", name, timeout=60)
        return name

    def _start(self, *args: str) -> None:
        # --pull never: a missing image must fail here, not fetch from a registry.
        result = _docker("run", "--pull", "never", "-d", *args, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())

    def _address(self, container: str, network: str) -> str:
        result = _docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container,
                         timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        address = ((json.loads(result.stdout) or {}).get(network) or {}).get("IPAddress", "")
        self.assertTrue(_is_ip(address), f"{container} has no address on {network}: {result.stdout}")
        return address

    def _listener_lines(self, container: str) -> list[str]:
        result = _docker("logs", container, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def test_a_container_with_the_forwarders_settings_routes_nothing(self) -> None:
        """Datagrams routed through the forwarder's settings never arrive; through the control router they do."""
        config = self.stack.compose("config", "--format", "json")
        self.assertEqual(config.returncode, 0, f"docker compose config: {config.stderr.strip()}")
        loaded = json.loads(config.stdout)
        forwarder = (loaded.get("services") or {}).get("fetcher-forwarder") or {}
        attachments = forwarder.get("networks") or {}
        sysctls = forwarder.get("sysctls") or {}
        endpoint = {name: dict(((attachments.get(name) or {}).get("driver_opts")) or {})
                    for name in ("egress", "outside")}
        for name, options in endpoint.items():
            self.assertEqual(_endpoint_sysctls(options).get(IFNAME_FORWARDING), ["0"], f"{name}: {options!r}")
        egress = self._network("route-egress")
        outside = self._network("route-outside")
        flags: list[str] = []
        for key, value in sorted(sysctls.items()):
            flags += ["--sysctl", f"{key}={value}"]

        listener = self._container("route-listener")
        self._start("--name", listener, "--network", outside, *HARDENING, IMAGE, "python", "-I", "-c",
                    f"PORT = {ROUTED_PORT!r}\nLIFETIME = 120\n" + _ROUTED_LISTENER)
        router = self._container("route-forwarder")
        self._start("--name", router, "--network", _network_flag(egress, endpoint["egress"]),
                    "--network", _network_flag(outside, endpoint["outside"]),
                    *HARDENING, *flags, IMAGE, *FORWARDER_COMMAND)
        control = self._container("route-control")
        self._start("--name", control, "--network", egress, "--network", outside, *HARDENING,
                    "--sysctl", f"{IP_FORWARD}=1", IMAGE, "python", "-I", "-c", "import time; time.sleep(300)")

        deadline = time.monotonic() + ROUTED_WAIT_SECONDS
        while "ready" not in self._listener_lines(listener) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertIn("ready", self._listener_lines(listener), "the listener did not start")
        target = self._address(listener, outside)
        phases = [("forwarder", self._address(router, egress)), ("control", self._address(control, egress))]

        state = self._output_of(_docker("exec", router, "python", "-I", "-c", _FORWARDING_PROBE,
                                        timeout=PROBE_RUN_TIMEOUT), "router forwarding probe")
        # Control: the router is on both networks. The switch nobody writes
        # goes into the failure message, not an assertion.
        interfaces = {name for name in state["ipv4"] if name not in ("all", "default", "lo")}
        self.assertEqual(len(interfaces), 2, state)
        before = self._ip_counters(router)

        helper = self._container("route-helper")
        answers = self._output_of(
            _docker("run", "--pull", "never", "--rm", "--name", helper, "--network", egress,
                    "--user", "0:0", "--read-only", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                    "--security-opt", "no-new-privileges:true", IMAGE, "python", "-I", "-c",
                    f"PHASES = {phases!r}\nLISTENER = {target!r}\nPORT = {ROUTED_PORT!r}\n"
                    f"COUNT = {ROUTED_DATAGRAMS!r}\nINTERVAL = {ROUTED_INTERVAL!r}\n" + _ROUTING_HELPER,
                    timeout=PROBE_RUN_TIMEOUT),
            "routing helper",
        )
        self.assertEqual(answers, {"forwarder": 0, "control": 0}, f"setting the helper's route failed: {answers}")

        # The control's datagrams were sent last: once they are in, anything
        # the router under test had forwarded would be in too.
        deadline = time.monotonic() + ROUTED_WAIT_SECONDS
        while True:
            lines = self._listener_lines(listener)
            if any(line.startswith("control ") for line in lines) or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        subnet = ipaddress.ip_network(self._subnet(egress))
        received = [line.split() for line in lines if line != "ready"]
        control_sources = {fields[1] for fields in received if fields[0] == "control"}
        self.assertTrue(control_sources, f"control: nothing arrived through the forwarding router: {lines}")
        # Routed, not translated: the listener sees the helper's own address.
        self.assertTrue(all(ipaddress.ip_address(source) in subnet for source in control_sources), lines)
        self.assertEqual(
            [fields for fields in received if fields[0] == "forwarder"], [],
            f"the forwarder's settings forwarded datagrams (ip_forward reads {state['ip_forward']!r}): {state}",
        )
        # The router under test received them (so they were refused there,
        # not lost on the way) and forwarded none.
        after = self._ip_counters(router)
        self.assertGreaterEqual(after["InReceives"] - before["InReceives"], ROUTED_DATAGRAMS, (before, after))
        self.assertEqual(after["ForwDatagrams"], before["ForwDatagrams"], (before, after))

    def _ip_counters(self, container: str) -> dict[str, int]:
        """The container's own IPv4 counters from /proc/net/snmp (its network namespace's)."""
        return {key: int(value) for key, value in self._output_of(
            _docker("exec", container, "python", "-I", "-c", _IP_COUNTERS_PROBE, timeout=PROBE_RUN_TIMEOUT),
            f"{container} IP counters",
        ).items()}

    def _subnet(self, network: str) -> str:
        result = _docker("network", "inspect", "--format", "{{json .IPAM.Config}}", network, timeout=PROBE_RUN_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        subnets = [entry.get("Subnet") for entry in json.loads(result.stdout) or []
                   if "." in (entry.get("Subnet") or "")]
        self.assertEqual(len(subnets), 1, result.stdout)
        return subnets[0]

    def _output_of(self, result: subprocess.CompletedProcess[str], what: str) -> Any:
        self.assertEqual(result.returncode, 0, f"{what} failed: {result.stderr.strip()}")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"{what} printed nothing: {result.stderr.strip()}")
        return json.loads(lines[-1])


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
