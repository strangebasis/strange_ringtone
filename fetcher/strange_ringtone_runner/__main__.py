"""Run the runner: python -I fetcher/runner.py [--bind HOST] (checkout) or /app/fetcher/runner.py (image).

fetcher/runner.py explains the script form and how the core's and the
fetcher's shared modules are found. Settings come from the environment
(docs/PLAN.md 6.1); the port is fixed by the contract and only the bind
address can change, on the command line, so a local run can stay on
loopback. Before it binds, the entry refuses to start (exit 3) when the
container has a default route or its route tables can't be read, unless
FETCH_ALLOW_DEFAULT_ROUTE=true; strange_ringtone_fetch.routes says why.

A mirror of strange_ringtone_fetch.__main__, line for line, under the
runner's name: the runner may not import the fetcher's entry, which would
load the fetcher's server into this process.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
from types import FrameType

from strange_ringtone_fetch.routes import RouteError, default_routes

from strange_ringtone_runner.config import ConfigError, Settings
from strange_ringtone_runner.server import RunnerServer

PORT = 8080
PROG = "strange_ringtone_runner"
# Refused because of the route table: distinct from a configuration error (2)
# and a failure to start (1), so an operator can tell the three apart.
EXIT_ROUTE = 3


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
    """Parse --bind, read the settings (exit 2), check the route table (exit 3), then serve on port 8080."""
    parser = argparse.ArgumentParser(prog=PROG, description="Serve strange_ringtone's fetcher runner on port 8080.")
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
    # After the settings, so a bad FETCH_ALLOW_DEFAULT_ROUTE is a configuration
    # error and never permission; before RunnerServer, so a container where
    # yt-dlp could reach out past the forwarder never binds its port
    # (docs/PLAN.md 6.1, S2).
    if settings.allow_default_route:
        print(f"{PROG}: FETCH_ALLOW_DEFAULT_ROUTE is set: not checking for a default route", file=sys.stderr)
    else:
        try:
            routes = default_routes()
        except RouteError as error:
            # Fail closed: a table we can't read proves nothing about egress.
            print(f"{PROG}: refusing to start: cannot check the route table ({error}); "
                  "set FETCH_ALLOW_DEFAULT_ROUTE=true only where egress is filtered another way", file=sys.stderr)
            return EXIT_ROUTE
        if routes:
            # Families only, each once: device names would describe the
            # deployment's network in its logs.
            families = ", ".join(sorted({route.family for route in routes}))
            print(f"{PROG}: refusing to start: this container has a default route ({families}), so something "
                  "other than the forwarder could carry traffic out; set FETCH_ALLOW_DEFAULT_ROUTE=true only "
                  "where egress is filtered another way", file=sys.stderr)
            return EXIT_ROUTE
    try:
        server = RunnerServer((args.bind, PORT), settings)
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
