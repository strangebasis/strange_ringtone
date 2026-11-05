"""The default-route start check (strange_ringtone_fetch.routes and the fetcher's and runner's entry points, S2).

The fetcher reaches only its runner and the runner reaches YouTube only
through the forwarder, so both entry points refuse to start when the kernel's
route tables hold a default route, or when they can't be read or understood
(docs/PLAN.md 6.1). The parser tests feed it
text in the kernel's exact formats; the tree tests use a temporary directory
as /proc/net; the entry tests patch the check and the server, so nothing here
reads the host's routes or opens a socket. Only RealKernelTest reads the real
/proc, inside the image under --network none.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import os
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
from strange_ringtone_fetch import __main__ as entry
from strange_ringtone_fetch import routes
from strange_ringtone_fetch.config import Settings
from strange_ringtone_fetch.routes import DefaultRoute, RouteError, default_routes, parse_ipv4_routes, parse_ipv6_routes
from strange_ringtone_runner import __main__ as runner_entry

ORIGIN = "https://ringtones.example.internal"
# The entry point's own code for "refused because of the route table",
# distinct from configuration (2) and bind (1). Hardcoded: the constant
# doesn't exist until the check is wired in.
EXIT_ROUTE = 3

# /proc/net/route's header exactly as the image's kernel prints it under
# --network none: padded with spaces to 127 characters, then a newline.
IPV4_HEADER = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"
    "                                                       \n"
)


def ipv4_line(device: str, destination: str, gateway: str, flags: str, mask: str) -> str:
    # The kernel's format (net/ipv4/fib_trie.c): eleven tab-separated fields,
    # hex in host byte order (so 192.168.0.0 is 0000A8C0 on x86), padded to
    # 127 characters.
    line = f"{device}\t{destination}\t{gateway}\t{flags}\t0\t0\t0\t{mask}\t0\t0\t0"
    return f"{line:<127}\n"


def ipv6_line(destination: str, prefix: str, metric: str, flags: str, device: str) -> str:
    # The kernel's format (net/ipv6/route.c): dest prefixlen src srcprefix
    # nexthop metric refcnt use flags device, the device right-aligned in 8.
    zeros = "0" * 32
    return f"{destination} {prefix} {zeros} 00 {zeros} {metric} 00000001 00000000 {flags} {device:>8}\n"


ZERO_V6 = "0" * 32
# The three lines of /proc/net/ipv6_route captured in the image under
# --network none: the kernel's two ::/0 reject routes on lo (flags
# RTF_REJECT|RTF_NONEXTHOP, 0x00200200), which carry nothing, and ::1/128.
NETWORK_NONE_IPV6 = (
    "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200       lo\n"
    "00000000000000000000000000000001 80 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 00000000 00000002 00000000 80200001       lo\n"
    "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200       lo\n"
)
# A real IPv6 default route: ::/0 via a gateway, flags
# RTF_UP|RTF_GATEWAY|RTF_ADDRCONF|RTF_EXPIRES|RTF_DEFAULT (0x00450003).
IPV6_DEFAULT_ETH0 = ipv6_line(ZERO_V6, "00", "00000400", "00450003", "eth0")
# A real IPv4 default route: 0.0.0.0/0 via 192.168.0.1, flags UG (0003).
IPV4_DEFAULT_ETH0 = ipv4_line("eth0", "00000000", "0100A8C0", "0003", "00000000")
# 192.168.0.0/24 on eth0, the subnet route every attached network has.
IPV4_SUBNET_ETH0 = ipv4_line("eth0", "0000A8C0", "00000000", "0001", "00FFFFFF")


def write_text(path: Path, text: str) -> None:
    # LF exactly as the kernel writes it, and closed before the test's
    # cleanup removes it (Windows can't delete an open file).
    with open(path, "w", encoding="ascii", newline="\n") as handle:
        handle.write(text)


class Ipv4ParseTest(unittest.TestCase):
    """S2: /proc/net/route text in, IPv4 default routes out."""

    def test_header_only_means_no_default_route(self) -> None:
        # S2 acceptance: --network none has the header and nothing else. The
        # header's trailing spaces and a trailing blank line are not routes.
        for text in (IPV4_HEADER, IPV4_HEADER + "\n", IPV4_HEADER + "\n   \n"):
            with self.subTest(text=text):
                self.assertEqual(parse_ipv4_routes(text), ())

    def test_a_default_entry_is_found_whatever_its_flags(self) -> None:
        # S2 refusal: 0.0.0.0/0 is a default route whether or not it is up or
        # has a gateway. A parser that looked for RTF_UP or RTF_GATEWAY would
        # miss a route that comes up after the check: fail closed.
        for flags in ("0003", "0001", "0000", "0002", "0201"):
            text = IPV4_HEADER + IPV4_SUBNET_ETH0 + ipv4_line("eth0", "00000000", "0100A8C0", flags, "00000000")
            with self.subTest(flags=flags):
                self.assertEqual(parse_ipv4_routes(text), (DefaultRoute("IPv4", "eth0"),))
        # Every default entry counts, in file order, each with its device.
        text = IPV4_HEADER + IPV4_DEFAULT_ETH0 + IPV4_SUBNET_ETH0 + ipv4_line(
            "eth1", "00000000", "0101A8C0", "0003", "00000000")
        self.assertEqual(parse_ipv4_routes(text), (DefaultRoute("IPv4", "eth0"), DefaultRoute("IPv4", "eth1")))

    def test_a_zero_mask_is_default_whatever_its_gateway_and_destination(self) -> None:
        # S2 refusal: a /0 matches every destination. An on-link default (no
        # gateway) still reaches everything on its link, and a /0 whose
        # destination column isn't zero is still a /0: neither is skipped.
        cases = {
            "on_link": ipv4_line("eth0", "00000000", "00000000", "0001", "00000000"),
            "odd_destination": ipv4_line("eth0", "0100A8C0", "0100A8C0", "0003", "00000000"),
        }
        for case, line in cases.items():
            with self.subTest(case=case):
                self.assertEqual(parse_ipv4_routes(IPV4_HEADER + IPV4_SUBNET_ETH0 + line),
                                 (DefaultRoute("IPv4", "eth0"),))

    def test_hex_columns_are_ascii_hex_digits_only(self) -> None:
        # S2: int(text, 16) alone also reads Unicode digits, underscores and a
        # 0x prefix, so a zero mask spelled that way would parse; the kernel
        # never writes such a line, so it refuses rather than being read.
        arabic_zero = "٠"
        cases = {
            "underscore_mask": ipv4_line("secretdev0", "00000000", "0100A8C0", "0003", "0000_000"),
            "arabic_indic_mask": ipv4_line("secretdev0", "00000000", "0100A8C0", "0003", arabic_zero * 8),
            "0x_mask": ipv4_line("secretdev0", "00000000", "0100A8C0", "0003", "0x000000"),
            "underscore_destination": ipv4_line("secretdev0", "0000_000", "0100A8C0", "0003", "00FFFFFF"),
            "arabic_indic_destination": ipv4_line("secretdev0", arabic_zero * 8, "0100A8C0", "0003", "00FFFFFF"),
            "0x_destination": ipv4_line("secretdev0", "0x00000A", "0100A8C0", "0003", "00FFFFFF"),
            "underscore_flags": ipv4_line("secretdev0", "0000A8C0", "00000000", "0_01", "00FFFFFF"),
            "arabic_indic_flags": ipv4_line("secretdev0", "0000A8C0", "00000000", arabic_zero * 3 + "1", "00FFFFFF"),
            "0x_flags": ipv4_line("secretdev0", "0000A8C0", "00000000", "0x01", "00FFFFFF"),
        }
        for case, line in cases.items():
            with self.subTest(case=case):
                with self.assertRaises(RouteError) as raised:
                    parse_ipv4_routes(IPV4_HEADER + line)
                self.assertNotIn("secretdev0", str(raised.exception))

    def test_only_tabs_and_spaces_separate_fields(self) -> None:
        # S2: an interface name may hold characters such as \x1c that
        # str.split() also splits on; they must not shift the columns, or the
        # parser would read the wrong one and miss a default route.
        for device in ("et\x1ch0", "et\x1fh0"):
            with self.subTest(device=device):
                text = IPV4_HEADER + ipv4_line(device, "00000000", "0100A8C0", "0003", "00000000")
                self.assertEqual(parse_ipv4_routes(text), (DefaultRoute("IPv4", device),))

    def test_other_routes_are_not_default(self) -> None:
        # S2 acceptance: an internal network still has its subnet route, and
        # host routes exist; neither carries traffic beyond the subnet.
        text = (
            IPV4_HEADER
            + IPV4_SUBNET_ETH0
            # 192.168.0.1/32, a host route.
            + ipv4_line("eth0", "0100A8C0", "00000000", "0005", "FFFFFFFF")
            # 10.0.0.0/8 via a gateway: a wide route is still not a default one.
            + ipv4_line("eth1", "0000000A", "0101A8C0", "0003", "000000FF")
        )
        self.assertEqual(parse_ipv4_routes(text), ())

    def test_malformed_input_raises_route_error(self) -> None:
        # S2: text the parser doesn't recognise refuses rather than counting
        # as "no routes"; the message names no value from the text.
        cases = {
            "empty": "",
            "wrong_header": "Interface\tDestination\tGateway\n" + IPV4_DEFAULT_ETH0,
            "header_not_first": IPV4_DEFAULT_ETH0 + IPV4_HEADER,
            "five_fields": IPV4_HEADER + "secretdev0\t00000000\t0100A8C0\t0003\t0\n",
            "ten_fields": IPV4_HEADER + "secretdev0\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\t0\t0\n",
            "non_hex_destination": IPV4_HEADER + ipv4_line("secretdev0", "0000000G", "0100A8C0", "0003", "00000000"),
            "non_hex_mask": IPV4_HEADER + ipv4_line("secretdev0", "00000000", "0100A8C0", "0003", "0000000Z"),
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                with self.assertRaises(RouteError) as raised:
                    parse_ipv4_routes(text)
                self.assertNotIn("secretdev0", str(raised.exception))


class Ipv6ParseTest(unittest.TestCase):
    """S2: /proc/net/ipv6_route text in, IPv6 default routes out."""

    def test_reject_default_routes_are_not_default(self) -> None:
        # S2 acceptance: the kernel's ::/0 reject routes in a namespace
        # without IPv6 drop everything, so --network none has no default
        # route. Counting them would refuse every fetcher container.
        self.assertEqual(parse_ipv6_routes(NETWORK_NONE_IPV6), ())

    def test_a_real_default_route_is_found(self) -> None:
        # S2 refusal: a ::/0 route without RTF_REJECT is a default route, and
        # the reject routes beside it don't hide it.
        self.assertEqual(parse_ipv6_routes(NETWORK_NONE_IPV6 + IPV6_DEFAULT_ETH0), (DefaultRoute("IPv6", "eth0"),))
        # Whatever its other flags: a route that is not up yet still refuses.
        for flags in ("00000000", "00000001", "00010003", "00450003"):
            with self.subTest(flags=flags):
                self.assertEqual(parse_ipv6_routes(ipv6_line(ZERO_V6, "00", "00000400", flags, "eth1")),
                                 (DefaultRoute("IPv6", "eth1"),))

    def test_prefix_routes_are_not_default(self) -> None:
        # S2 acceptance: link-local, loopback and a /32 are not default routes.
        text = (
            ipv6_line("fe800000000000000000000000000000", "40", "00000100", "00000001", "eth0")
            + ipv6_line("00000000000000000000000000000001", "80", "00000000", "80200001", "lo")
            + ipv6_line("20010db8000000000000000000000000", "20", "00000400", "00000003", "eth0")
            + ipv6_line("ff000000000000000000000000000000", "08", "00000100", "00000001", "eth0")
        )
        self.assertEqual(parse_ipv6_routes(text), ())

    def test_empty_file_means_no_routes(self) -> None:
        # S2 acceptance: ipv6_route has no header, so empty text is valid.
        for text in ("", "\n", "  \n\n"):
            with self.subTest(text=text):
                self.assertEqual(parse_ipv6_routes(text), ())

    def test_malformed_input_raises_route_error(self) -> None:
        # S2: an unrecognised line refuses rather than being skipped.
        default = ipv6_line(ZERO_V6, "00", "00000400", "00450003", "secretdev0")
        fields = default.split()
        cases = {
            "five_fields": " ".join(fields[:5]) + "\n",
            "nine_fields": " ".join(fields[:9]) + "\n",
            "non_hex_prefix": default.replace(" 00 ", " zz ", 1),
            "non_hex_flags": default.replace("00450003", "0045000G"),
            "garbage_after_valid_lines": NETWORK_NONE_IPV6 + "not a route table secretdev0\n",
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                with self.assertRaises(RouteError) as raised:
                    parse_ipv6_routes(text)
                self.assertNotIn("secretdev0", str(raised.exception))

    def test_hex_columns_are_ascii_hex_digits_only(self) -> None:
        # S2: as for IPv4, a prefix or flags column that only int(text, 16)
        # would read (Unicode digits, '_', 0x) refuses.
        arabic_zero = "٠"
        cases = {
            "arabic_indic_prefix": ipv6_line(ZERO_V6, arabic_zero * 2, "00000400", "00000003", "secretdev0"),
            "underscore_flags": ipv6_line(ZERO_V6, "00", "00000400", "0020_200", "secretdev0"),
            "0x_flags": ipv6_line(ZERO_V6, "00", "00000400", "0x200200", "secretdev0"),
            "arabic_indic_destination": ipv6_line(arabic_zero * 32, "00", "00000400", "00000003", "secretdev0"),
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                with self.assertRaises(RouteError) as raised:
                    parse_ipv6_routes(text)
                self.assertNotIn("secretdev0", str(raised.exception))

    def test_only_tabs_and_spaces_separate_fields(self) -> None:
        # S2: the device is read by position, so a \x1c in its name must not
        # split it into two fields.
        for device in ("et\x1ch0", "et\x1fh0"):
            with self.subTest(device=device):
                self.assertEqual(parse_ipv6_routes(ipv6_line(ZERO_V6, "00", "00000400", "00450003", device)),
                                 (DefaultRoute("IPv6", device),))


class ProcTreeTest(unittest.TestCase):
    """S2: default_routes reads route and ipv6_route under proc_net and fails closed."""

    @contextlib.contextmanager
    def proc_net(self, route: str | None, ipv6_route: str | None) -> Iterator[Path]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if route is not None:
                write_text(root / "route", route)
            if ipv6_route is not None:
                write_text(root / "ipv6_route", ipv6_route)
            before = sorted(root.iterdir())
            yield root
            # The check only reads: nothing is created or removed.
            self.assertEqual(sorted(root.iterdir()), before)

    def test_reads_both_files_and_reports_both_families(self) -> None:
        # S2 refusal: IPv4 then IPv6, each from its own file.
        with self.proc_net(IPV4_HEADER + IPV4_SUBNET_ETH0 + IPV4_DEFAULT_ETH0,
                           NETWORK_NONE_IPV6 + ipv6_line(ZERO_V6, "00", "00000400", "00450003", "eth1")) as root:
            self.assertEqual(default_routes(root), (DefaultRoute("IPv4", "eth0"), DefaultRoute("IPv6", "eth1")))
        # Either file alone is enough to refuse.
        with self.proc_net(IPV4_HEADER, NETWORK_NONE_IPV6 + IPV6_DEFAULT_ETH0) as root:
            self.assertEqual(default_routes(proc_net=root), (DefaultRoute("IPv6", "eth0"),))
        with self.proc_net(IPV4_HEADER + IPV4_DEFAULT_ETH0, NETWORK_NONE_IPV6) as root:
            self.assertEqual(default_routes(proc_net=root), (DefaultRoute("IPv4", "eth0"),))

    def test_tables_without_a_default_route_mean_none(self) -> None:
        # S2 acceptance: the internal-network layout (a subnet route and the
        # kernel's reject routes) starts.
        with self.proc_net(IPV4_HEADER + IPV4_SUBNET_ETH0, NETWORK_NONE_IPV6) as root:
            self.assertEqual(default_routes(proc_net=root), ())

    def test_missing_route_file_raises(self) -> None:
        # S2: no IPv4 table, or no /proc at all (Windows), refuses.
        with self.proc_net(None, NETWORK_NONE_IPV6) as root, self.assertRaises(RouteError):
            default_routes(proc_net=root)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RouteError):
            default_routes(proc_net=Path(tmp) / "no-such-proc" / "net")

    def test_missing_ipv6_route_file_means_no_ipv6_routes(self) -> None:
        # S2 acceptance: a kernel without IPv6 has no ipv6_route file; the
        # IPv4 table still decides.
        with self.proc_net(IPV4_HEADER + IPV4_SUBNET_ETH0, None) as root:
            self.assertEqual(default_routes(proc_net=root), ())
        with self.proc_net(IPV4_HEADER + IPV4_DEFAULT_ETH0, None) as root:
            self.assertEqual(default_routes(proc_net=root), (DefaultRoute("IPv4", "eth0"),))

    def test_an_unreadable_route_file_raises(self) -> None:
        # S2: a file that exists but can't be read refuses; only a missing
        # ipv6_route has a meaning of its own. A directory stands in for an
        # unreadable file on every platform.
        for name in ("route", "ipv6_route"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                # The other file is valid (route) or missing (ipv6_route, which
                # alone would mean "no IPv6"), so only the directory can refuse.
                if name == "ipv6_route":
                    write_text(root / "route", IPV4_HEADER)
                (root / name).mkdir()
                with self.assertRaises(RouteError) as raised:
                    default_routes(proc_net=root)
                # The message names the file, so the operator knows which.
                self.assertIn(name, str(raised.exception))
        # A parse failure in either file refuses the same way.
        for route, ipv6_route in ((IPV4_HEADER + "eth0\t0\n", NETWORK_NONE_IPV6), (IPV4_HEADER, "eth0 0\n")):
            with self.subTest(route=route, ipv6_route=ipv6_route), self.proc_net(route, ipv6_route) as root, \
                    self.assertRaises(RouteError):
                default_routes(proc_net=root)

    def test_a_non_ascii_byte_refuses(self) -> None:
        # S2: the kernel prints these tables in ASCII. A byte outside it
        # can't be told apart from a corrupt table, so the read refuses
        # rather than replacing it and parsing on.
        route = (IPV4_HEADER + ipv4_line("ethé", "0000A8C0", "00000000", "0001", "00FFFFFF")).encode("latin-1")
        ipv6_route = ipv6_line("fe800000000000000000000000000000", "40", "00000100", "00000001",
                               "ethé").encode("latin-1")
        for name, files in (("route", {"route": route}),
                            ("ipv6_route", {"route": IPV4_HEADER.encode("ascii"), "ipv6_route": ipv6_route})):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for file_name, data in files.items():
                    with open(root / file_name, "wb") as handle:
                        handle.write(data)
                with self.assertRaises(RouteError) as raised:
                    default_routes(proc_net=root)
                self.assertIn(name, str(raised.exception))
        # Acceptance: the same tables in ASCII parse, with no default route.
        with self.proc_net(IPV4_HEADER + IPV4_SUBNET_ETH0,
                           ipv6_line("fe800000000000000000000000000000", "40", "00000100", "00000001", "eth0")) as root:
            self.assertEqual(default_routes(proc_net=root), ())

    def test_default_path_is_proc_net(self) -> None:
        # S2: the default is the real /proc/net, and no platform is special:
        # where it doesn't exist (Windows), the check refuses.
        self.assertEqual(routes.PROC_NET, Path("/proc/net"))
        self.assertEqual(inspect.signature(default_routes).parameters["proc_net"].default, Path("/proc/net"))
        if (routes.PROC_NET / "route").is_file():
            found = default_routes()
            self.assertIsInstance(found, tuple)
            for route in found:
                self.assertIsInstance(route, DefaultRoute)
        else:
            with self.assertRaises(RouteError):
                default_routes()


@unittest.skipUnless(os.environ.get("STRANGE_RINGTONE_FETCHER_IMAGE_TESTS") == "1",
                     "in-image test; set STRANGE_RINGTONE_FETCHER_IMAGE_TESTS=1 (README, Development)")
class RealKernelTest(unittest.TestCase):
    def test_network_none_has_no_default_route(self) -> None:
        # S2 acceptance on a real kernel: the in-image run uses --network
        # none, whose tables hold only the reject routes, so the check passes.
        self.assertEqual(default_routes(), ())


class FakeServer:
    """Stands in for FetchServer: records that it was built, and stops at once."""

    started: list[tuple[str, int]]

    # sweep_strays: the runner's entry always passes its PID-1 decision
    # (tests/test_fetcher_runner_sweep.py tests it); the fetcher's passes none.
    def __init__(self, address: tuple[str, int], settings: Settings, sweep_strays: bool = False) -> None:
        FakeServer.started.append(address)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


class EntryRouteCheckTest(unittest.TestCase):
    """S2: main() checks the routes after the settings and before FetchServer, and exits 3."""

    def run_main(self, environ: dict[str, str], **check: Any) -> tuple[int, str, mock.MagicMock]:
        FakeServer.started = []
        log = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(entry, "default_routes", create=True, **check) as patched, \
                mock.patch.object(entry, "FetchServer", FakeServer), \
                mock.patch.object(entry.signal, "signal"), \
                contextlib.redirect_stderr(log):
            code = entry.main(["--bind", "127.0.0.1"])
        return code, log.getvalue(), patched

    def test_a_default_route_exits_3_before_the_server_starts(self) -> None:
        # S2 refusal: the server is never built, so the port is never bound.
        cases = {
            "IPv4": (DefaultRoute("IPv4", "eth0"),),
            "IPv6": (DefaultRoute("IPv6", "eth0"),),
            "IPv4, IPv6": (DefaultRoute("IPv4", "eth0"), DefaultRoute("IPv6", "eth0")),
        }
        for families, found in cases.items():
            with self.subTest(families=families):
                code, stderr, patched = self.run_main({"PUBLIC_ORIGIN": ORIGIN}, return_value=found)
                self.assertEqual(code, EXIT_ROUTE, stderr)
                self.assertEqual(FakeServer.started, [])
                patched.assert_called_once()
                self.assertIn("refusing to start", stderr)
                self.assertIn(f"default route ({families})", stderr)
                self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", stderr)
                self.assertNotIn("listening", stderr)

    def test_an_unreadable_route_table_exits_3(self) -> None:
        # S2 refusal: a table that can't be read is not "no default route".
        code, stderr, patched = self.run_main(
            {"PUBLIC_ORIGIN": ORIGIN}, side_effect=RouteError("cannot read /proc/net/route"))
        self.assertEqual(code, EXIT_ROUTE, stderr)
        self.assertEqual(FakeServer.started, [])
        patched.assert_called_once()
        self.assertIn("refusing to start", stderr)
        self.assertIn("cannot read /proc/net/route", stderr)
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", stderr)

    def test_no_default_route_starts(self) -> None:
        # S2 acceptance: with no default route the check runs, once, on the
        # real /proc/net, and the server starts on the fixed port.
        for environ in ({"PUBLIC_ORIGIN": ORIGIN}, {"PUBLIC_ORIGIN": ORIGIN, "FETCH_ALLOW_DEFAULT_ROUTE": "false"},
                        {"PUBLIC_ORIGIN": ORIGIN, "FETCH_ALLOW_DEFAULT_ROUTE": ""}):
            with self.subTest(environ=environ):
                code, stderr, patched = self.run_main(environ, return_value=())
                self.assertEqual(code, 0, stderr)
                self.assertEqual(FakeServer.started, [("127.0.0.1", 8080)])
                patched.assert_called_once()
                self.assertIn(patched.call_args, (mock.call(), mock.call(routes.PROC_NET),
                                                  mock.call(proc_net=routes.PROC_NET)))
                self.assertNotIn("refusing", stderr)
                self.assertNotIn("FETCH_ALLOW_DEFAULT_ROUTE is set", stderr)

    def test_the_override_skips_the_check_and_says_so(self) -> None:
        # S2: with the override the tables are not read at all (so a missing
        # /proc doesn't matter), and one stderr line says the check is off.
        for value in ("true", "1", "yes", "TRUE"):
            with self.subTest(value=value):
                code, stderr, patched = self.run_main(
                    {"PUBLIC_ORIGIN": ORIGIN, "FETCH_ALLOW_DEFAULT_ROUTE": value},
                    return_value=(DefaultRoute("IPv4", "eth0"),))
                self.assertEqual(code, 0, stderr)
                self.assertEqual(FakeServer.started, [("127.0.0.1", 8080)])
                patched.assert_not_called()
                self.assertIn("strange_ringtone_fetch: FETCH_ALLOW_DEFAULT_ROUTE is set", stderr)
                self.assertNotIn("refusing", stderr)

    def test_configuration_errors_come_first(self) -> None:
        # S2: a bad setting exits 2 before the tables are read, and a bad
        # override value is a configuration error, never permission.
        for environ in ({"PUBLIC_ORIGIN": ""},
                        {"PUBLIC_ORIGIN": ORIGIN, "FETCH_ALLOW_DEFAULT_ROUTE": "on"},
                        {"PUBLIC_ORIGIN": ORIGIN, "FETCH_ALLOW_DEFAULT_ROUTE": " true"}):
            with self.subTest(environ=environ):
                code, stderr, patched = self.run_main(environ, return_value=(DefaultRoute("IPv4", "eth0"),))
                self.assertEqual(code, 2, stderr)
                self.assertEqual(FakeServer.started, [])
                patched.assert_not_called()
                self.assertIn("configuration error", stderr)
                self.assertIn("PUBLIC_ORIGIN" if environ["PUBLIC_ORIGIN"] == "" else "FETCH_ALLOW_DEFAULT_ROUTE",
                              stderr)

    def test_the_message_names_no_device_or_address(self) -> None:
        # S2: the refusal names the families and the override, never the
        # interface, which would describe the deployment's network in its logs.
        found = (DefaultRoute("IPv4", "eth0"), DefaultRoute("IPv4", "ens5"), DefaultRoute("IPv6", "wg-site0"))
        code, stderr, _ = self.run_main({"PUBLIC_ORIGIN": ORIGIN}, return_value=found)
        self.assertEqual(code, EXIT_ROUTE, stderr)
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", stderr)
        self.assertIn("IPv4", stderr)
        for device in ("eth0", "ens5", "wg-site0"):
            self.assertNotIn(device, stderr)
        # Each family once, however many routes it has.
        self.assertEqual(stderr.count("IPv4"), 1, stderr)


class RunnerEntryRouteCheckTest(unittest.TestCase):
    """S2 for the runner: its main() checks the routes after the settings and before RunnerServer, and exits 3.

    The runner is the container yt-dlp runs in, so a default route there is
    the very egress the forwarder exists to replace. The same check
    (strange_ringtone_fetch.routes), the same order and the same messages as
    the fetcher's entry, under the runner's own name; the runner has no
    required setting, so an empty environment is a valid one.
    """

    PREFIX = "strange_ringtone_runner: "

    def run_main(self, environ: dict[str, str], **check: Any) -> tuple[int, str, mock.MagicMock]:
        FakeServer.started = []
        log = io.StringIO()
        # signal.signal itself: the entry must not leave a SIGTERM handler in
        # the test process; nor, on Linux, a non-dumpable test process.
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(runner_entry, "default_routes", create=True, **check) as patched, \
                mock.patch.object(runner_entry, "RunnerServer", FakeServer), \
                mock.patch.object(runner_entry, "make_not_dumpable"), \
                mock.patch.object(runner_entry, "sweep_is_safe_here", return_value=True), \
                mock.patch("signal.signal"), \
                contextlib.redirect_stderr(log):
            code = runner_entry.main(["--bind", "127.0.0.1"])
        return code, log.getvalue(), patched

    def test_a_default_route_exits_3_before_the_server_starts(self) -> None:
        # S2 refusal: the server is never built, so the port is never bound.
        cases = {
            "IPv4": (DefaultRoute("IPv4", "eth0"),),
            "IPv6": (DefaultRoute("IPv6", "eth0"),),
            "IPv4, IPv6": (DefaultRoute("IPv4", "eth0"), DefaultRoute("IPv6", "eth0")),
        }
        for families, found in cases.items():
            with self.subTest(families=families):
                code, stderr, patched = self.run_main({}, return_value=found)
                self.assertEqual(code, EXIT_ROUTE, stderr)
                self.assertEqual(FakeServer.started, [])
                patched.assert_called_once()
                self.assertIn(self.PREFIX + "refusing to start", stderr)
                self.assertIn(f"default route ({families})", stderr)
                self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", stderr)
                self.assertNotIn("listening", stderr)

    def test_an_unreadable_route_table_exits_3(self) -> None:
        # S2 refusal: a table that can't be read is not "no default route".
        code, stderr, patched = self.run_main({}, side_effect=RouteError("cannot read /proc/net/route"))
        self.assertEqual(code, EXIT_ROUTE, stderr)
        self.assertEqual(FakeServer.started, [])
        patched.assert_called_once()
        self.assertIn(self.PREFIX + "refusing to start", stderr)
        self.assertIn("cannot read /proc/net/route", stderr)
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", stderr)

    def test_no_default_route_starts(self) -> None:
        # S2 acceptance: with no default route the check runs, once, on the
        # real /proc/net, and the server starts on the fixed port.
        for environ in ({}, {"FETCH_ALLOW_DEFAULT_ROUTE": "false"}, {"FETCH_ALLOW_DEFAULT_ROUTE": ""}):
            with self.subTest(environ=environ):
                code, stderr, patched = self.run_main(environ, return_value=())
                self.assertEqual(code, 0, stderr)
                self.assertEqual(FakeServer.started, [("127.0.0.1", 8080)])
                patched.assert_called_once()
                self.assertIn(patched.call_args, (mock.call(), mock.call(routes.PROC_NET),
                                                  mock.call(proc_net=routes.PROC_NET)))
                self.assertNotIn("refusing", stderr)
                self.assertNotIn("FETCH_ALLOW_DEFAULT_ROUTE is set", stderr)
                self.assertIn("strange_ringtone_runner listening on 127.0.0.1:8080", stderr)

    def test_the_override_skips_the_check_and_says_so(self) -> None:
        # S2: with the override the tables are not read at all (so a missing
        # /proc doesn't matter), and one stderr line says the check is off.
        for value in ("true", "1", "yes", "TRUE"):
            with self.subTest(value=value):
                code, stderr, patched = self.run_main(
                    {"FETCH_ALLOW_DEFAULT_ROUTE": value}, return_value=(DefaultRoute("IPv4", "eth0"),))
                self.assertEqual(code, 0, stderr)
                self.assertEqual(FakeServer.started, [("127.0.0.1", 8080)])
                patched.assert_not_called()
                self.assertIn(self.PREFIX + "FETCH_ALLOW_DEFAULT_ROUTE is set", stderr)
                self.assertNotIn("refusing", stderr)

    def test_configuration_errors_come_first(self) -> None:
        # S2: a bad setting exits 2 before the tables are read, and a bad
        # override value is a configuration error, never permission.
        cases = (
            ({"FETCH_MAX_SECONDS": "0"}, "FETCH_MAX_SECONDS"),
            ({"FETCH_ALLOW_DEFAULT_ROUTE": "on"}, "FETCH_ALLOW_DEFAULT_ROUTE"),
            ({"FETCH_ALLOW_DEFAULT_ROUTE": " true"}, "FETCH_ALLOW_DEFAULT_ROUTE"),
        )
        for environ, name in cases:
            with self.subTest(environ=environ):
                code, stderr, patched = self.run_main(environ, return_value=(DefaultRoute("IPv4", "eth0"),))
                self.assertEqual(code, 2, stderr)
                self.assertEqual(FakeServer.started, [])
                patched.assert_not_called()
                self.assertIn(self.PREFIX + "configuration error", stderr)
                self.assertIn(name, stderr)

    def test_the_message_names_no_device_or_address(self) -> None:
        # S2: the refusal names the families and the override, never the
        # interface, which would describe the deployment's network in its logs.
        found = (DefaultRoute("IPv4", "eth0"), DefaultRoute("IPv4", "ens5"), DefaultRoute("IPv6", "wg-site0"))
        code, stderr, _ = self.run_main({}, return_value=found)
        self.assertEqual(code, EXIT_ROUTE, stderr)
        self.assertIn("FETCH_ALLOW_DEFAULT_ROUTE", stderr)
        self.assertIn("IPv4", stderr)
        for device in ("eth0", "ens5", "wg-site0"):
            self.assertNotIn(device, stderr)
        # Each family once, however many routes it has.
        self.assertEqual(stderr.count("IPv4"), 1, stderr)


if __name__ == "__main__":
    unittest.main()
