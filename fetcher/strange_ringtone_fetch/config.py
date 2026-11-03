"""The fetcher's settings from environment variables, validated once at startup.

The same rules as the core's (strange_ringtone.config, whose parsers this
reuses): every value is checked strictly, any doubt refuses to start, and an
error names the variable but never its value.
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
    _parse_int,
    _parse_public_origin,
    _parse_trusted_proxies,
)

__all__ = ["ConfigError", "Settings", "SCRATCH_DIR", "PROXY_VARIABLES"]

# The fetcher's own directory under the /tmp tmpfs, fixed for the reason the
# core gives in strange_ringtone.config: tempfile.gettempdir() follows TMPDIR
# and falls back to other paths, and the writable paths are part of the
# contract. Distinct from the core's, so a development machine running both
# never has one server empty the other's scratch at startup.
SCRATCH_DIR = Path("/tmp/strange_ringtone_fetch")
# Windows is a development platform only, never a deployment target.
_WINDOWS = sys.platform == "win32"

# decode.js refuses input longer than MAX_INPUT_SECONDS (600 s), so a longer
# video would be downloaded only for the phone to refuse it.
MAX_SECONDS_LIMIT = 600
MIB = 1024 * 1024
# The proxy settings passed on to yt-dlp, and nothing else from the server's
# environment (strange_ringtone_fetch.ytdlp builds the child's environment).
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
# What a NO_PROXY list holds: host names, domain suffixes, IP literals, CIDRs
# and ports, separated by commas, or a lone '*'.
_NO_PROXY_RE = re.compile(r"[A-Za-z0-9.*:_\[\]/,-]{1,1024}")
_PROXY_MAX_CHARS = 1024

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _default_scratch_dir() -> Path:
    if _WINDOWS:
        return Path(tempfile.gettempdir()) / "strange_ringtone_fetch"
    return SCRATCH_DIR


@dataclass(frozen=True)
class Settings:
    public_origin: str
    trusted_proxies: tuple[Network, ...] = ()
    fetch_max_seconds: int = 600
    fetch_max_bytes: int = 40 * MIB
    fetch_timeout_seconds: int = 120
    rate_limit_fetches_per_minute: int = 5
    fetch_result_ttl_seconds: int = 600
    # (name, value) pairs from PROXY_VARIABLES that the operator set.
    proxies: tuple[tuple[str, str], ...] = ()
    scratch_dir: Path = field(default_factory=_default_scratch_dir)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Build Settings from a mapping such as os.environ, or raise ConfigError."""
        return cls(
            public_origin=_parse_public_origin(environ.get("PUBLIC_ORIGIN", "")),
            trusted_proxies=_parse_trusted_proxies(_optional(environ, "TRUSTED_PROXIES")),
            fetch_max_seconds=_parse_int(environ, "FETCH_MAX_SECONDS", 600, 1, MAX_SECONDS_LIMIT),
            # At least 1 MiB, or no real audio track fits; at most 512 MiB,
            # because /tmp must hold two of them (docs/PLAN.md 6).
            fetch_max_bytes=_parse_int(environ, "FETCH_MAX_BYTES", 40 * MIB, MIB, 512 * MIB),
            # At most 10 minutes: one job holds the only worker for this long,
            # and the page stops polling after about 15 minutes in all.
            fetch_timeout_seconds=_parse_int(environ, "FETCH_TIMEOUT_SECONDS", 120, 1, 600),
            rate_limit_fetches_per_minute=_parse_int(environ, "RATE_LIMIT_FETCHES_PER_MINUTE", 5, 1),
            fetch_result_ttl_seconds=_parse_int(environ, "FETCH_RESULT_TTL_SECONDS", 600, 1),
            proxies=_parse_proxies(environ),
        )


def _parse_proxies(environ: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    proxies: list[tuple[str, str]] = []
    for name in PROXY_VARIABLES:
        value = _optional(environ, name)
        if value is None:
            continue
        if name == "NO_PROXY":
            if not _NO_PROXY_RE.fullmatch(value):
                raise ConfigError(f"{name} must be a comma-separated list of hosts, domains or addresses")
        else:
            _check_proxy_url(name, value)
        proxies.append((name, value))
    return tuple(proxies)


def _check_proxy_url(name: str, value: str) -> None:
    # Plain ASCII with no whitespace or control characters, as for
    # PUBLIC_ORIGIN: invisible characters make the checked value differ from
    # what the operator wrote.
    if len(value) > _PROXY_MAX_CHARS or not value.isascii() or not all(0x20 < ord(ch) < 0x7F for ch in value):
        raise ConfigError(f"{name} must be plain ASCII with no whitespace")
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid proxy URL") from exc
    # yt-dlp without the optional requests package talks to a proxy through
    # urllib, which supports only plain-HTTP proxies (CONNECT for https).
    if parts.scheme != "http":
        raise ConfigError(f"{name} must start with http://; other proxy schemes are not supported")
    # Credentials in an environment variable would be a secret outside a
    # *_FILE variable (CLAUDE.md), and yt-dlp could repeat them in an error.
    if "@" in parts.netloc:
        raise ConfigError(f"{name} must not contain credentials; use a proxy that needs none")
    if not parts.hostname or parts.netloc.endswith(":"):
        raise ConfigError(f"{name} has an invalid host")
    if port is not None and not parts.netloc.endswith(f":{port}"):
        raise ConfigError(f"{name} has a malformed port")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in value or "#" in value:
        raise ConfigError(f"{name} must be a proxy address only: http://host:port")
