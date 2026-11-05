"""The runner's settings from environment variables, validated once at startup.

The same rules as the fetcher's (strange_ringtone_fetch.config, whose proxy
parser this reuses, and through it the core's): every value is checked
strictly, any doubt refuses to start, and an error names the variable but
never its value. The runner reads only what yt-dlp's run needs and the
default-route override, never anything browser-facing (PUBLIC_ORIGIN,
TRUSTED_PROXIES): it serves the fetcher, not a browser.
"""
from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# _parse_bool, _parse_int, MAX_SECONDS_LIMIT, PROXY_VARIABLES and
# parse_proxies are what from_env reads with (docs/PLAN.md 6.1): the same
# parsers, bounds and messages as the fetcher's.
from strange_ringtone.config import ConfigError, _parse_bool, _parse_int  # noqa: F401
from strange_ringtone_fetch.config import MAX_SECONDS_LIMIT, MIB, PROXY_VARIABLES, parse_proxies  # noqa: F401

__all__ = ["ConfigError", "Settings", "SCRATCH_DIR", "RUNNER_VARIABLES"]

# The runner's own directory under the /tmp tmpfs, fixed for the reason the
# core gives in strange_ringtone.config: the writable paths are part of the
# contract. Distinct from the fetcher's and the core's, so a development
# machine running all three never has one server empty another's scratch.
SCRATCH_DIR = Path("/tmp/strange_ringtone_runner")
# Windows is a development platform only, never a deployment target.
_WINDOWS = sys.platform == "win32"

# Every variable the runner reads, for the docs test (.env.example's fetcher
# block lists the union of the fetcher's and the runner's variables).
RUNNER_VARIABLES = (
    "FETCH_MAX_SECONDS",
    "FETCH_MAX_BYTES",
    "FETCH_TIMEOUT_SECONDS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "FETCH_ALLOW_DEFAULT_ROUTE",
)


def _default_scratch_dir() -> Path:
    if _WINDOWS:
        return Path(tempfile.gettempdir()) / "strange_ringtone_runner"
    return SCRATCH_DIR


@dataclass(frozen=True)
class Settings:
    fetch_max_seconds: int = 600
    fetch_max_bytes: int = 40 * MIB
    fetch_timeout_seconds: int = 120
    # (name, value) pairs from PROXY_VARIABLES that the operator set: yt-dlp's
    # way to the forwarder, and the only part of this environment its child
    # sees.
    proxies: tuple[tuple[str, str], ...] = ()
    # True turns the default-route start check off (docs/PLAN.md 6.1) where
    # egress is filtered some other way. Off by default: fail closed.
    allow_default_route: bool = False
    scratch_dir: Path = field(default_factory=_default_scratch_dir)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Build Settings from a mapping such as os.environ, or raise ConfigError.

        Reads exactly RUNNER_VARIABLES, with the fetcher's parsers, bounds and
        messages: FETCH_MAX_SECONDS 1..600, FETCH_MAX_BYTES 1 MiB..512 MiB,
        FETCH_TIMEOUT_SECONDS 1..600, the proxies through parse_proxies, the
        override through the core's boolean words. Nothing is required, so an
        empty environment is a valid configuration.
        """
        raise NotImplementedError
