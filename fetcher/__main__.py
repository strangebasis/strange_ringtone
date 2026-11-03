"""Entry point for the fetcher: python -I fetcher [--bind HOST] (checkout) or python -I /app/fetcher (image).

-I keeps the environment, the user site and the current directory off
sys.path. Python still puts the directory it runs as a program first on
sys.path, which makes strange_ringtone_fetch next to this file importable.

The fetcher reuses two modules of the core, strange_ringtone.config and
strange_ringtone.http_guard, so both servers apply one implementation of the
Origin check, the length rules, X-Forwarded-For and the rate limit. They are
imported from ../core: in a checkout that is the core's source directory, and
in the image /app/core holds exactly those modules (fetcher/Dockerfile). The
directory goes first on sys.path, so nothing else installed can stand in for
them.
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = str(Path(__file__).resolve().parent.parent / "core")
if sys.path[:1] != [_CORE]:
    sys.path.insert(0, _CORE)

from strange_ringtone_fetch.__main__ import main  # noqa: E402  (needs the core on sys.path first)

if __name__ == "__main__":
    sys.exit(main())
