"""The runner's settings from environment variables, validated once at startup.

The same rules as the fetcher's (strange_ringtone_fetch.config, whose proxy
parser this reuses, and through it the core's): every value is checked
strictly, any doubt refuses to start, and an error names the variable but
never its value. The runner reads only what yt-dlp's run needs and the
default-route and no-sweep overrides, never anything browser-facing
(PUBLIC_ORIGIN, TRUSTED_PROXIES): it serves the fetcher, not a browser.
"""
from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# The core's parsers and the fetcher's proxy rules, reused rather than copied
# (docs/PLAN.md 6.1): one implementation of each check, so the runner can't
# drift into accepting a value the fetcher's documentation refuses.
from strange_ringtone.config import ConfigError, _parse_bool, _parse_int
from strange_ringtone_fetch.config import MAX_SECONDS_LIMIT, MIB, PROXY_VARIABLES, parse_proxies

__all__ = ["ConfigError", "Settings", "SCRATCH_DIR", "RUNNER_VARIABLES"]

# The runner's own directory under the /tmp tmpfs, fixed for the reason the
# core gives in strange_ringtone.config: the writable paths are part of the
# contract. Distinct from the fetcher's and the core's, so a development
# machine running all three never has one server empty another's scratch.
SCRATCH_DIR = Path("/tmp/strange_ringtone_runner")
# Windows is a development platform only, never a deployment target.
_WINDOWS = sys.platform == "win32"

# Every variable the runner reads, for the docs test (.env.example's fetcher
# block lists the union of the fetcher's and the runner's variables). The
# proxy names come from the fetcher's config, whose parser reads them.
RUNNER_VARIABLES = (
    "FETCH_MAX_SECONDS",
    "FETCH_MAX_BYTES",
    "FETCH_TIMEOUT_SECONDS",
    *PROXY_VARIABLES,
    "FETCH_ALLOW_DEFAULT_ROUTE",
    "FETCH_ALLOW_NO_SWEEP",
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
    # True lets the entry serve where it can't sweep (not PID 1), for a local
    # run or a host that can't run the runner as PID 1 (owner's decision Q4,
    # 2026-11-05). It never turns the sweep off where it can run. Off by
    # default: a runner that can't sweep refuses to start.
    allow_no_sweep: bool = False
    scratch_dir: Path = field(default_factory=_default_scratch_dir)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Build Settings from a mapping such as os.environ, or raise ConfigError.

        Reads exactly RUNNER_VARIABLES, with the fetcher's parsers, bounds and
        messages: FETCH_MAX_SECONDS 1..600, FETCH_MAX_BYTES 1 MiB..512 MiB,
        FETCH_TIMEOUT_SECONDS 1..600, the proxies through parse_proxies, the
        two overrides through the core's boolean words. Nothing is required,
        so an empty environment is a valid configuration (as PID 1, the
        entry adds, where the sweep can run).
        """
        # Each variable is looked up by name, never by walking the mapping,
        # so nothing else in the runner's environment can become a setting.
        return cls(
            # decode.js refuses input longer than 600 s (MAX_SECONDS_LIMIT),
            # so a longer video would be downloaded only to be refused.
            fetch_max_seconds=_parse_int(environ, "FETCH_MAX_SECONDS", 600, 1, MAX_SECONDS_LIMIT),
            # At least 1 MiB, or no real audio track fits; at most 512 MiB,
            # the fetcher's bound, since the two read one shared .env.
            fetch_max_bytes=_parse_int(environ, "FETCH_MAX_BYTES", 40 * MIB, MIB, 512 * MIB),
            # At most 10 minutes: one run holds the runner's only job slot
            # for this long.
            fetch_timeout_seconds=_parse_int(environ, "FETCH_TIMEOUT_SECONDS", 120, 1, 600),
            # Uppercase names only, http:// only, no credentials: the rules
            # strange_ringtone_fetch.config.parse_proxies applies and explains.
            proxies=parse_proxies(environ),
            # The core's boolean words only, unstripped: a value the parser
            # doesn't know is a configuration error, so a typo never turns
            # the start check off (docs/PLAN.md 6.1, S2).
            allow_default_route=_parse_bool(environ, "FETCH_ALLOW_DEFAULT_ROUTE", False),
            # The same helper and the same rule: a typo is a configuration
            # error, never permission to serve without the sweep.
            allow_no_sweep=_parse_bool(environ, "FETCH_ALLOW_NO_SWEEP", False),
        )
