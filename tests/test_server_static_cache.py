"""How long a browser or proxy may keep the files under /static/<version>/.

A release has a version of its own, so every asset URL changes with it and
the files can be cached for a year. The default version "dev" names no
release: a rebuild of a checkout serves new code under the same URLs, so
those files must be revalidated on every use.
"""
from __future__ import annotations

import ast
import unittest

import helpers
import test_dev_fetch_stack
import test_server as base
from strange_ringtone import config, static
from strange_ringtone.config import Settings

IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
ASSETS = sorted(name for name in static.ALLOWED if name != "index.html")


class StaticCacheTest(base.ServerTestCase):
    def assertCaching(self, version: str, expected: str) -> None:
        for name in ASSETS:
            with self.subTest(version=version, name=name):
                response = self.request("GET", f"/static/{version}/{name}")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_all("Cache-Control"), [expected])
        # The page itself is never stored, whatever the version.
        page = self.request("GET", "/")
        self.assertEqual(page.status, 200)
        self.assertEqual(page.headers.get_all("Cache-Control"), ["no-store"])

    def test_the_default_version_is_revalidated(self) -> None:
        # setUp started the server with the default settings.
        self.assertEqual(self.settings.version, "dev")
        self.assertCaching("dev", NO_CACHE)

    def test_a_release_version_is_immutable(self) -> None:
        for version in ("1.2.3", "0.1.0-rc.1", "Dev", "dev1", "dev-1", "devel", "v0.1.0"):
            self.start(version=version)
            self.assertCaching(version, IMMUTABLE)

    def test_the_dev_stack_versions_stay_immutable(self) -> None:
        # tools/dev_fetch_stack.py gives each start a version of its own for
        # exactly this cache; that version is a release version to the core.
        version = test_dev_fetch_stack.load_stack().dev_version()
        self.start(version=version)
        self.assertCaching(version, IMMUTABLE)


class DefaultVersionTest(unittest.TestCase):
    def test_one_definition_of_the_default_version(self) -> None:
        self.assertTrue(hasattr(config, "DEFAULT_VERSION"),
                        "config.DEFAULT_VERSION: the one definition of the default version")
        self.assertEqual(config.DEFAULT_VERSION, "dev")
        self.assertEqual(Settings(public_origin=base.ORIGIN).version, config.DEFAULT_VERSION)
        self.assertEqual(Settings.from_env({"PUBLIC_ORIGIN": base.ORIGIN}).version, config.DEFAULT_VERSION)
        self.assertEqual(Settings.from_env({"PUBLIC_ORIGIN": base.ORIGIN, "VERSION": ""}).version,
                         config.DEFAULT_VERSION)

    def test_no_second_literal_can_drift(self) -> None:
        # The default is spelled once, in config.DEFAULT_VERSION; the settings
        # default, the parser and the server's caching rule all refer to it.
        package = helpers.ROOT / "core" / "strange_ringtone"
        counts = {}
        for name in ("config.py", "server.py", "static.py"):
            tree = ast.parse((package / name).read_text(encoding="utf-8"))
            counts[name] = sum(
                1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "dev"
            )
        self.assertEqual(counts, {"config.py": 1, "server.py": 0, "static.py": 0})


if __name__ == "__main__":
    unittest.main()
