"""Run the runner: python -I fetcher/runner.py [--bind HOST] (checkout) or /app/fetcher/runner.py (image).

fetcher/runner.py explains the script form and how the core's and the
fetcher's shared modules are found. Settings come from the environment
(docs/PLAN.md 6.1); the port is fixed by the contract and only the bind
address can change, on the command line, so a local run can stay on
loopback. Before it binds, the entry refuses to start (exit 3) when the
container has a default route or its route tables can't be read, unless
FETCH_ALLOW_DEFAULT_ROUTE=true; strange_ringtone_fetch.routes says why.
"""
from __future__ import annotations

import sys

# Settings, RouteError, default_routes and RunnerServer are what main uses:
# the same order of checks as the fetcher's entry (configuration, then the
# route table, then the server).
from strange_ringtone_fetch.routes import RouteError, default_routes  # noqa: F401

from strange_ringtone_runner.config import ConfigError, Settings  # noqa: F401
from strange_ringtone_runner.server import RunnerServer  # noqa: F401

PORT = 8080
PROG = "strange_ringtone_runner"
# Refused because of the route table: distinct from a configuration error (2)
# and a failure to start (1), so an operator can tell the three apart.
EXIT_ROUTE = 3


def main(argv: list[str] | None = None) -> int:
    """Parse --bind, read the settings (exit 2), check the route table (exit 3), then serve on port 8080."""
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
