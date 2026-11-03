"""The fetcher's unit tests, run again inside the fetcher image (docs/PLAN.md 9.2).

On the host they run against the checkout; here they run under the image's
Python 3.12, against the image's precompiled copy of the fetcher and of the
three core modules it ships (tests/image_fetcher/__init__.py puts those first
on sys.path), with the real prlimit and the image's read-only root. They
still use the yt-dlp stub, never the real yt-dlp, so they need no network.
"""
from __future__ import annotations

import importlib
import unittest
from pathlib import Path

# The mounted tests/ directory, which `-t /tests` puts on sys.path.
TESTS = Path("/tests")


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    # Only the top-level tests/test_fetcher_*.py modules. discover() would also
    # descend into this package, find this module again and recurse.
    names = sorted(path.stem for path in TESTS.glob("test_fetcher_*.py"))
    if not names:
        raise RuntimeError(f"no fetcher unit tests under {TESTS}; mount tests/ there")
    suite = unittest.TestSuite(standard_tests)
    for name in names:
        suite.addTests(loader.loadTestsFromModule(importlib.import_module(name)))
    return suite
