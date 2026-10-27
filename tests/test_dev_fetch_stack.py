"""The offline development stack (tools/dev_fetch_stack.py) must never show stale code."""
from __future__ import annotations

import importlib.util
import unittest

import helpers
from strange_ringtone import config

STACK = helpers.ROOT / "tools" / "dev_fetch_stack.py"


def load_stack():
    spec = importlib.util.spec_from_file_location("dev_fetch_stack", STACK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DevVersionTest(unittest.TestCase):
    def test_each_start_gets_a_new_valid_version(self) -> None:
        # The core serves /static/<version>/ as immutable. The stack's version
        # changes on every start, or a browser that saw an earlier start keeps
        # running its modules after the code changed.
        stack = load_stack()
        first = stack.dev_version()
        second = stack.dev_version()
        self.assertNotEqual(first, second)
        for version in (first, second):
            self.assertTrue(version.startswith("dev-"), version)
            self.assertEqual(config._parse_version(version), version)

    def test_the_core_is_started_with_that_version(self) -> None:
        source = STACK.read_text(encoding="utf-8")
        self.assertIn("version=dev_version()", source)


if __name__ == "__main__":
    unittest.main()
