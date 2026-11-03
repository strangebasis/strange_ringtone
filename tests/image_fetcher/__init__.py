"""Tests that run only inside the built fetcher image (docs/PLAN.md 6 and 9.2, "Fetcher image tests").

They need the image's yt-dlp, Deno, prlimit, read-only root, user and missing
network, so they are worthless anywhere else. The container sets
STRANGE_RINGTONE_FETCHER_IMAGE_TESTS=1 (README, Development); without it,
raising SkipTest here makes the host's `unittest discover -s tests` report this
package as skipped instead of failing on paths that don't exist. A variable of
their own, not the core's: the core image has no Deno or yt-dlp, and the
fetcher image has no ffmpeg.

Run inside the container as:
    python -I -m unittest discover -s /tests/image_fetcher -t /tests -v
The top-level /tests puts tests/ on sys.path, so the fetcher unit tests and
their helpers are importable.
"""
from __future__ import annotations

import os
import sys
import unittest

if os.environ.get("STRANGE_RINGTONE_FETCHER_IMAGE_TESTS") != "1":
    raise unittest.SkipTest("in-image tests; run them in the fetcher image (README, Development)")

# The image's copies, precompiled at build time, rather than the checkout's:
# the point is to test what ships. /app/core holds only the three core files
# the fetcher imports (fetcher/Dockerfile), so an import of anything else of
# the core fails here.
APP_FETCHER = "/app/fetcher"
APP_CORE = "/app/core"
for _path in (APP_FETCHER, APP_CORE):
    if _path in sys.path:
        sys.path.remove(_path)
# Core last, so it ends up first, as fetcher/__main__.py puts it.
sys.path.insert(0, APP_FETCHER)
sys.path.insert(0, APP_CORE)
