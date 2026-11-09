"""The fetcher's settings from environment variables, validated once at startup.

The same rules as the core's (strange_ringtone.config, whose parsers this
reuses): every value is checked strictly, any doubt refuses to start, and an
error names the variable but never its value.

The proxy variables and FETCH_MAX_SECONDS are the runner's settings now
(strange_ringtone_runner.config, docs/PLAN.md 6.1): the fetcher runs no
yt-dlp and reaches only its runner, at FETCH_RUNNER_URL. Their rules stay in
this module, public, so the runner imports them instead of keeping a copy.
"""
from __future__ import annotations

import ipaddress
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from strange_ringtone.config import (
    ConfigError,
    _optional,
    _parse_bool,
    _parse_int,
    _parse_public_origin,
    _parse_trusted_proxies,
)

__all__ = [
    "ConfigError",
    "Settings",
    "SCRATCH_DIR",
    "PROXY_VARIABLES",
    "DEFAULT_RUNNER_URL",
    "MAX_SECONDS_LIMIT",
    "MIB",
    "parse_proxies",
    "check_http_url",
]

# The fetcher's own directory under the /tmp tmpfs, fixed for the reason the
# core gives in strange_ringtone.config: tempfile.gettempdir() follows TMPDIR
# and falls back to other paths, and the writable paths are part of the
# contract. Distinct from the core's, so a development machine running both
# never has one server empty the other's scratch at startup.
SCRATCH_DIR = Path("/tmp/strange_ringtone_fetch")
# Windows is a development platform only, never a deployment target.
_WINDOWS = sys.platform == "win32"

# The upper bound of the runner's FETCH_MAX_SECONDS: decode.js refuses input
# longer than MAX_INPUT_SECONDS (600 s), so a longer video would be
# downloaded only for the phone to refuse it.
MAX_SECONDS_LIMIT = 600
MIB = 1024 * 1024
# The proxy settings the runner passes on to yt-dlp, and nothing else from its
# environment (strange_ringtone_runner.ytdlp builds the child's environment).
# The fetcher reads none of them: it reaches only its runner, directly over
# `jobs`. They are defined here because the runner's settings
# (strange_ringtone_runner.config) import these rules rather than copy them.
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
# What a NO_PROXY list holds: host names, domain suffixes, IP literals, CIDRs
# and ports, separated by commas, or a lone '*'.
_NO_PROXY_RE = re.compile(r"[A-Za-z0-9.*:_\[\]/,-]{1,1024}")
_PROXY_MAX_CHARS = 1024
# Where the fetcher finds its runner when FETCH_RUNNER_URL is unset: the
# compose service of that name, reached over the internal `jobs` network
# (docs/PLAN.md 6.1).
DEFAULT_RUNNER_URL = "http://fetcher-runner:8080"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _default_scratch_dir() -> Path:
    if _WINDOWS:
        return Path(tempfile.gettempdir()) / "strange_ringtone_fetch"
    return SCRATCH_DIR


@dataclass(frozen=True)
class Settings:
    public_origin: str
    trusted_proxies: tuple[Network, ...] = ()
    fetch_max_bytes: int = 40 * MIB
    fetch_timeout_seconds: int = 120
    rate_limit_fetches_per_minute: int = 5
    fetch_result_ttl_seconds: int = 600
    # True turns the default-route start check off (docs/PLAN.md 6.1) where
    # egress is filtered some other way. Off by default: fail closed.
    allow_default_route: bool = False
    # Where the runner is: validated like a proxy URL (http://host[:port],
    # nothing else). The fetcher trusts nothing the runner answers (S1), so
    # pointing this elsewhere can break fetches but not the posture.
    runner_url: str = DEFAULT_RUNNER_URL
    scratch_dir: Path = field(default_factory=_default_scratch_dir)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Build Settings from a mapping such as os.environ, or raise ConfigError."""
        return cls(
            public_origin=_parse_public_origin(environ.get("PUBLIC_ORIGIN", "")),
            trusted_proxies=_parse_trusted_proxies(_optional(environ, "TRUSTED_PROXIES")),
            # At least 1 MiB, or no real audio track fits; at most 512 MiB,
            # because /tmp must hold two of them (docs/PLAN.md 6).
            fetch_max_bytes=_parse_int(environ, "FETCH_MAX_BYTES", 40 * MIB, MIB, 512 * MIB),
            # At most 10 minutes: one job holds the only worker for this long,
            # and the page stops polling after about 15 minutes in all.
            fetch_timeout_seconds=_parse_int(environ, "FETCH_TIMEOUT_SECONDS", 120, 1, 600),
            rate_limit_fetches_per_minute=_parse_int(environ, "RATE_LIMIT_FETCHES_PER_MINUTE", 5, 1),
            fetch_result_ttl_seconds=_parse_int(environ, "FETCH_RESULT_TTL_SECONDS", 600, 1),
            # The core's boolean words only, unstripped: a value the parser
            # doesn't know is a configuration error, so a typo never turns
            # the start check off (docs/PLAN.md 6.1, S2).
            allow_default_route=_parse_bool(environ, "FETCH_ALLOW_DEFAULT_ROUTE", False),
            runner_url=_parse_runner_url(environ),
        )


def _parse_runner_url(environ: Mapping[str, str]) -> str:
    # The proxy URLs' rules (http://host[:port], plain ASCII, no credentials,
    # path, query or fragment), because the client speaks plain HTTP to one
    # host and port and nothing else. Unset or empty means the compose service.
    value = _optional(environ, "FETCH_RUNNER_URL")
    if value is None:
        return DEFAULT_RUNNER_URL
    check_http_url("FETCH_RUNNER_URL", value)
    return value


def parse_proxies(environ: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The (name, value) pairs of PROXY_VARIABLES that are set, in that order, or ConfigError.

    Uppercase names only: the runner hands yt-dlp exactly these, and a
    lowercase variant would be a second source of truth. Read by the
    runner's settings (strange_ringtone_runner.config), never by the fetcher's.
    """
    proxies: list[tuple[str, str]] = []
    for name in PROXY_VARIABLES:
        value = _optional(environ, name)
        if value is None:
            continue
        if name == "NO_PROXY":
            if not _NO_PROXY_RE.fullmatch(value):
                raise ConfigError(f"{name} must be a comma-separated list of hosts, domains or addresses")
        else:
            check_http_url(name, value)
        proxies.append((name, value))
    return tuple(proxies)


def check_http_url(name: str, value: str) -> None:
    """Refuse (ConfigError naming name, never value) anything but http://host[:port].

    The rule for the proxy variables and for FETCH_RUNNER_URL alike.
    """
    # Plain ASCII with no whitespace or control characters, as for
    # PUBLIC_ORIGIN: invisible characters make the checked value differ from
    # what the operator wrote.
    if len(value) > _PROXY_MAX_CHARS or not value.isascii() or not all(0x20 < ord(ch) < 0x7F for ch in value):
        raise ConfigError(f"{name} must be plain ASCII with no whitespace")
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid http://host[:port] URL") from exc
    # yt-dlp without the optional requests package talks to a proxy through
    # urllib, which supports only plain-HTTP proxies (CONNECT for https), and
    # the runner client speaks plain HTTP only.
    if parts.scheme != "http":
        raise ConfigError(f"{name} must start with http://; no other scheme is supported")
    # Credentials in an environment variable would be a secret outside a
    # *_FILE variable (CLAUDE.md), and yt-dlp could repeat them in an error.
    if "@" in parts.netloc:
        raise ConfigError(f"{name} must not contain credentials; use an address that needs none")
    if not parts.hostname or parts.netloc.endswith(":"):
        raise ConfigError(f"{name} has an invalid host")
    if port is not None and not parts.netloc.endswith(f":{port}"):
        raise ConfigError(f"{name} has a malformed port")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in value or "#" in value:
        raise ConfigError(f"{name} must be an address only: http://host[:port]")
