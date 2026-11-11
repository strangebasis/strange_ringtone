"""Settings from environment variables, validated once at startup.

Every value is checked strictly and any doubt refuses to start (fail closed): a
server that silently fell back to a default after a typo would run with limits
or an origin the operator never chose. Error messages name the variable but not
its value, so a deployment's details don't end up in a log collector.
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

DEFAULT_SOURCE_URL = "https://github.com/strangebasisdevs/strange_ringtone"
# The version outside a release. Spelled once: the server sends the static
# files of this version with no-cache instead of a year's caching, and a
# second spelling that drifted would bring the year back.
DEFAULT_VERSION = "dev"

# ASCII digits only. str.isdigit() and int() also accept other scripts' digits,
# signs, underscores and surrounding whitespace, none of which an operator means.
_INT_RE = re.compile(r"[0-9]+")
# A plain decimal: no exponent, inf, nan or Unicode minus sign, which float() would accept.
_DECIMAL_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")
# VERSION becomes the /static/<version>/ path segment that the static route
# matches exactly, so it may not hold '/', '%', '..' at the start, or anything
# that needs escaping in a URL.
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# What a browser can send as the host[:port] of an Origin: a lowercase DNS name
# (IDNs arrive as punycode) or a bracketed IPv6 literal, then an optional port.
_NETLOC_RE = re.compile(r"(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])(?::[0-9]+)?")
# Longer than any integer setting needs; stops a huge digit string from reaching
# int(), whose cost grows with the length.
_MAX_INT_DIGITS = 9

_BOOL_VALUES = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
_DEFAULT_PORTS = {"http": 80, "https": 443}

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(ValueError):
    """A setting is missing or invalid; the server must not start."""


# In the image /tmp is the only writable path (a tmpfs), so the scratch
# directory lives under it. It is fixed, not taken from tempfile.gettempdir():
# that reads TMPDIR, TEMP and TMP even under -I, and falls back to /var/tmp,
# /usr/tmp and then the working directory when /tmp is not writable, so uploads
# could land on a path the deployment contract never made writable. Nor is it
# an environment variable: the writable paths are part of the contract.
SCRATCH_DIR = Path("/tmp/strange_ringtone")
# Windows has no /tmp. It is a development platform only, never a deployment
# target, so a checkout there uses the user's temporary directory.
_WINDOWS = sys.platform == "win32"


def _default_scratch_dir() -> Path:
    if _WINDOWS:
        return Path(tempfile.gettempdir()) / "strange_ringtone"
    return SCRATCH_DIR


@dataclass(frozen=True)
class Settings:
    public_origin: str
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    max_clip_seconds: int = 30
    default_target_lufs: float = -14.0
    render_timeout_seconds: int = 20
    render_concurrency: int = 2
    rate_limit_renders_per_minute: int = 10
    result_ttl_seconds: int = 600
    max_results: int = 50
    fetch_enabled: bool = False
    source_url: str = DEFAULT_SOURCE_URL
    version: str = DEFAULT_VERSION
    scratch_dir: Path = field(default_factory=_default_scratch_dir)

    @property
    def max_body_bytes(self) -> int:
        # The largest WAV the browser can legitimately send: MAX_CLIP_SECONDS of
        # 48 kHz stereo 16-bit PCM, plus 4 KiB for the RIFF headers. Anything
        # bigger is refused before a byte of the body is read.
        return self.max_clip_seconds * 48000 * 2 * 2 + 4096

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Build Settings from a mapping such as os.environ, or raise ConfigError."""
        return cls(
            public_origin=_parse_public_origin(environ.get("PUBLIC_ORIGIN", "")),
            trusted_proxies=_parse_trusted_proxies(_optional(environ, "TRUSTED_PROXIES")),
            max_clip_seconds=_parse_int(environ, "MAX_CLIP_SECONDS", 30, 1, 40),
            default_target_lufs=_parse_lufs(environ, "DEFAULT_TARGET_LUFS", -14.0, -24.0, -9.0),
            render_timeout_seconds=_parse_int(environ, "RENDER_TIMEOUT_SECONDS", 20, 1),
            render_concurrency=_parse_int(environ, "RENDER_CONCURRENCY", 2, 1),
            rate_limit_renders_per_minute=_parse_int(environ, "RATE_LIMIT_RENDERS_PER_MINUTE", 10, 1),
            result_ttl_seconds=_parse_int(environ, "RESULT_TTL_SECONDS", 600, 1),
            max_results=_parse_int(environ, "MAX_RESULTS", 50, 1),
            fetch_enabled=_parse_bool(environ, "FETCH_ENABLED", False),
            source_url=_parse_source_url(_optional(environ, "SOURCE_URL")),
            version=_parse_version(_optional(environ, "VERSION")),
        )


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    # Compose files often write `NAME=` to mean "use the default", so an empty
    # optional variable counts as unset.
    value = environ.get(name, "")
    return value if value != "" else None


def _is_plain_ascii(value: str) -> bool:
    # No whitespace or control characters: they are invisible in a config file
    # and some URL parsers silently drop them, so what we check would differ
    # from what the operator wrote.
    return value.isascii() and all(0x20 < ord(ch) < 0x7F for ch in value)


def _parse_public_origin(value: str) -> str:
    name = "PUBLIC_ORIGIN"
    if value == "":
        raise ConfigError(f"{name} is required, for example https://ringtones.example.internal")
    if not _is_plain_ascii(value):
        raise ConfigError(f"{name} must be plain ASCII with no whitespace")
    # The Origin check compares bytes exactly and browsers always send the
    # scheme and host in lowercase, so an uppercase letter here would make
    # every POST fail with 403 and no hint why.
    if value != value.lower():
        raise ConfigError(f"{name} must be lowercase, because browsers send lowercase origins")
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid origin") from exc
    if parts.scheme not in _DEFAULT_PORTS:
        raise ConfigError(f"{name} must start with http:// or https://")
    # Rebuilding scheme://netloc rejects any path (even a trailing '/'), query
    # or fragment: none of them appear in a browser's Origin header.
    if f"{parts.scheme}://{parts.netloc}" != value:
        raise ConfigError(f"{name} must be an origin only: no path, query, fragment or trailing slash")
    if "@" in parts.netloc:
        raise ConfigError(f"{name} must not contain user information")
    if not parts.hostname or not _NETLOC_RE.fullmatch(parts.netloc):
        raise ConfigError(f"{name} has an invalid host")
    if parts.netloc.endswith(":"):
        raise ConfigError(f"{name} has an empty port")
    if parts.netloc.startswith("["):
        _require_canonical_ipv6(name, parts.netloc[1 : parts.netloc.index("]")])
    if port is not None:
        # Browsers omit the default port and leading zeros, so either would
        # never match the Origin they send.
        if not parts.netloc.endswith(f":{port}"):
            raise ConfigError(f"{name} has a malformed port")
        if port == _DEFAULT_PORTS[parts.scheme]:
            raise ConfigError(f"{name} must omit the default port, as browsers do")
    return value


def _require_canonical_ipv6(name: str, host: str) -> None:
    # Browsers serialize an IPv6 host in its shortest lowercase form, such as
    # [::1], and the Origin check compares bytes. Any other spelling of the
    # same address would start fine and then fail every POST with 403. A dotted
    # tail is refused outright: browsers never send one, and Python only
    # started printing it in 3.13.
    try:
        canonical = ipaddress.IPv6Address(host).compressed
    except ValueError as exc:
        raise ConfigError(f"{name} has an invalid IPv6 host") from exc
    if "." in host or host != canonical:
        raise ConfigError(f"{name} must write the IPv6 host as browsers do, for example [{canonical}]")


def _parse_trusted_proxies(value: str | None) -> tuple[Network, ...]:
    name = "TRUSTED_PROXIES"
    if value is None:
        # Trust no proxy by default: otherwise any client could pick its own
        # rate-limit key by sending X-Forwarded-For.
        return ()
    networks: list[Network] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            raise ConfigError(f"{name} has an empty entry; separate CIDRs with single commas")
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ConfigError(f"{name} has an entry that is not an IP network in CIDR form") from exc
    return tuple(networks)


def _parse_int(environ: Mapping[str, str], name: str, default: int, low: int, high: int | None = None) -> int:
    value = _optional(environ, name)
    if value is None:
        return default
    bounds = f"from {low} to {high}" if high is not None else f"at least {low}"
    if not _INT_RE.fullmatch(value):
        raise ConfigError(f"{name} must be a whole number written in ASCII digits, {bounds}")
    digits = value.lstrip("0") or "0"
    if len(digits) > _MAX_INT_DIGITS:
        raise ConfigError(f"{name} is too large; it must be {bounds}")
    number = int(digits)
    if number < low or (high is not None and number > high):
        raise ConfigError(f"{name} must be {bounds}")
    return number


def _parse_lufs(environ: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    value = _optional(environ, name)
    if value is None:
        return default
    bounds = f"from {low:g} to {high:g}"
    if not _DECIMAL_RE.fullmatch(value) or len(value) > 16:
        raise ConfigError(f"{name} must be a decimal number such as -14 or -16.5, {bounds}")
    number = float(value)
    if not low <= number <= high:
        raise ConfigError(f"{name} must be {bounds}")
    return number


def _parse_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = _optional(environ, name)
    if value is None:
        return default
    # isascii() first: lower() maps some non-ASCII letters onto ASCII ones.
    if not value.isascii() or value.lower() not in _BOOL_VALUES:
        raise ConfigError(f"{name} must be one of true, false, 1, 0, yes or no")
    return _BOOL_VALUES[value.lower()]


def _parse_source_url(value: str | None) -> str:
    name = "SOURCE_URL"
    if value is None:
        return DEFAULT_SOURCE_URL
    # The URL lands in an href. Escaping stops it breaking out of the
    # attribute, but not a javascript: or data: scheme, so only absolute
    # http(s) URLs are allowed.
    if not _is_plain_ascii(value):
        raise ConfigError(f"{name} must be plain ASCII with no whitespace")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid URL") from exc
    if parts.scheme.lower() not in _DEFAULT_PORTS or not parts.netloc:
        raise ConfigError(f"{name} must be an absolute http:// or https:// URL")
    return value


def _parse_version(value: str | None) -> str:
    if value is None:
        return DEFAULT_VERSION
    if not _VERSION_RE.fullmatch(value):
        raise ConfigError(
            "VERSION must be 1 to 64 characters of letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    return value
