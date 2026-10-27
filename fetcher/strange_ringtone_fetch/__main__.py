"""Run the fetcher: python -I fetcher [--bind HOST] from a checkout, python -I /app/fetcher in the image.

fetcher/__main__.py explains the directory form and how the core's shared
modules are found. Settings come from the environment (docs/PLAN.md 6); the
port is fixed by the contract and only the bind address can change, on the
command line, so a local run can stay on loopback.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
from types import FrameType

from strange_ringtone_fetch.config import ConfigError, Settings
from strange_ringtone_fetch.server import FetchServer

PORT = 8080
PROG = "strange_ringtone_fetch"


def _stop(signum: int, frame: FrameType | None) -> None:
    # Raising unwinds serve_forever on the main thread; server.shutdown() here
    # would wait for the very loop this handler interrupted.
    raise SystemExit(0)


def _bind_address(value: str) -> str:
    # An IP literal only: socket.bind resolves anything else with getaddrinfo,
    # which can be a DNS query nobody asked for.
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an IP address such as 127.0.0.1 or ::1, not a host name") from None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        raise argparse.ArgumentTypeError("must be an IP address without a zone index")
    return str(address)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description="Serve strange_ringtone's fetcher on port 8080.")
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        type=_bind_address,
        metavar="HOST",
        help="IP address to listen on (default 0.0.0.0, for the container; use 127.0.0.1 locally)",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as error:
        # Fail closed: never a server with a setting the operator didn't choose.
        print(f"{PROG}: configuration error: {error}", file=sys.stderr)
        return 2
    try:
        server = FetchServer((args.bind, PORT), settings)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"{PROG}: cannot start: {error}", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, _stop)
    print(f"{PROG} listening on {args.bind}:{PORT}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
