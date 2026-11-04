"""Compose topology tests: the fetcher image brought up with compose.example.yaml (docs/PLAN.md 6.1, T1 to T3).

They start real containers on real Docker networks (the fetcher on internal
networks only, the forwarder the only way out), so they need Docker Engine
28.0 or newer and the fetcher image already built; they publish no port and
send nothing to YouTube or any other external host. Without
STRANGE_RINGTONE_COMPOSE_TESTS=1, raising SkipTest here makes the host's
`unittest discover -s tests` report this package as skipped instead of
starting containers nobody asked for.

Run from the repository root as:
    STRANGE_RINGTONE_COMPOSE_TESTS=1 python -I -m unittest discover -s tests/compose -t tests -v
STRANGE_RINGTONE_COMPOSE_IMAGE picks the image tag (default strange_ringtone-fetcher:local).
"""
from __future__ import annotations

import os
import unittest

if os.environ.get("STRANGE_RINGTONE_COMPOSE_TESTS") != "1":
    raise unittest.SkipTest("compose topology tests; set STRANGE_RINGTONE_COMPOSE_TESTS=1 (README, Development)")
