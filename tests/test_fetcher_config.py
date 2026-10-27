"""The fetcher's settings (strange_ringtone_fetch.config) and its entry points."""
from __future__ import annotations

import ast
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts core/ and fetcher/ on sys.path)
import strange_ringtone_fetch
from strange_ringtone_fetch import __main__ as entry
from strange_ringtone_fetch import config
from strange_ringtone_fetch.config import ConfigError, Settings

ORIGIN = "https://ringtones.example.internal"
PACKAGE = Path(strange_ringtone_fetch.__file__).resolve().parent


def from_env(**values: str) -> Settings:
    environ = {"PUBLIC_ORIGIN": ORIGIN}
    environ.update(values)
    return Settings.from_env(environ)


class DefaultsTest(unittest.TestCase):
    def test_contract_defaults(self) -> None:
        settings = from_env()
        self.assertEqual(settings.public_origin, ORIGIN)
        self.assertEqual(settings.trusted_proxies, ())
        # 600, not 900: the page refuses input over ten minutes.
        self.assertEqual(settings.fetch_max_seconds, 600)
        self.assertEqual(settings.fetch_max_bytes, 41943040)
        self.assertEqual(settings.fetch_timeout_seconds, 120)
        self.assertEqual(settings.rate_limit_fetches_per_minute, 5)
        self.assertEqual(settings.fetch_result_ttl_seconds, 600)
        self.assertEqual(settings.proxies, ())
        self.assertEqual(settings.scratch_dir, config._default_scratch_dir())

    def test_direct_construction_uses_the_same_defaults(self) -> None:
        self.assertEqual(Settings(public_origin=ORIGIN), from_env())

    def test_empty_optional_values_mean_the_default(self) -> None:
        names = (
            "TRUSTED_PROXIES", "FETCH_MAX_SECONDS", "FETCH_MAX_BYTES", "FETCH_TIMEOUT_SECONDS",
            "RATE_LIMIT_FETCHES_PER_MINUTE", "FETCH_RESULT_TTL_SECONDS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
        )
        self.assertEqual(from_env(**dict.fromkeys(names, "")), Settings(public_origin=ORIGIN))

    def test_scratch_dir_is_fixed_and_not_configurable(self) -> None:
        self.assertEqual(config.SCRATCH_DIR.as_posix(), "/tmp/strange_ringtone_fetch")
        with (
            mock.patch.object(config, "_WINDOWS", False),
            mock.patch.object(tempfile, "gettempdir", side_effect=AssertionError("gettempdir called")),
            mock.patch.dict(os.environ, {"TMPDIR": "/srv", "TEMP": "/srv", "TMP": "/srv"}),
        ):
            self.assertEqual(Settings(public_origin=ORIGIN).scratch_dir, config.SCRATCH_DIR)
            self.assertEqual(from_env(SCRATCH_DIR="/srv", TMPDIR="/srv").scratch_dir, config.SCRATCH_DIR)

    def test_scratch_dir_differs_from_the_cores(self) -> None:
        # Each server empties its scratch at startup; sharing one would let a
        # fetcher start wipe the core's uploads on a development machine.
        from strange_ringtone import config as core_config

        self.assertNotEqual(config.SCRATCH_DIR, core_config.SCRATCH_DIR)
        self.assertNotEqual(config._default_scratch_dir(), core_config._default_scratch_dir())

    def test_settings_are_frozen(self) -> None:
        with self.assertRaises(AttributeError):
            Settings(public_origin=ORIGIN).public_origin = "https://evil.example.internal"  # type: ignore[misc]


class SharedParsersTest(unittest.TestCase):
    def test_public_origin_is_required_and_checked_like_the_cores(self) -> None:
        for environ in ({}, {"PUBLIC_ORIGIN": ""}):
            with self.subTest(environ=environ), self.assertRaisesRegex(ConfigError, "PUBLIC_ORIGIN"):
                Settings.from_env(environ)
        for origin in ("https://Ringtones.example.internal", "https://ringtones.example.internal/", "ftp://x"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ConfigError, "PUBLIC_ORIGIN"):
                from_env(PUBLIC_ORIGIN=origin)

    def test_trusted_proxies(self) -> None:
        self.assertEqual([str(n) for n in from_env(TRUSTED_PROXIES="10.0.0.0/8, ::1").trusted_proxies],
                         ["10.0.0.0/8", "::1/128"])
        with self.assertRaisesRegex(ConfigError, "TRUSTED_PROXIES"):
            from_env(TRUSTED_PROXIES="10.0.0.0/8,,")

    def test_messages_never_repeat_the_value(self) -> None:
        secret = "s3cr3t-value"
        for name in ("FETCH_MAX_BYTES", "HTTPS_PROXY", "NO_PROXY", "PUBLIC_ORIGIN"):
            with self.subTest(name=name), self.assertRaises(ConfigError) as raised:
                from_env(**{name: f"http://user:{secret}@example.internal x"})
            self.assertNotIn(secret, str(raised.exception))


class LimitsTest(unittest.TestCase):
    def test_bounds(self) -> None:
        cases = {
            # The page decodes at most 600 s, so a longer fetch is refused at startup.
            "FETCH_MAX_SECONDS": ("1", "600", "0", "601"),
            "FETCH_MAX_BYTES": ("1048576", "536870912", "1048575", "536870913"),
            "FETCH_TIMEOUT_SECONDS": ("1", "600", "0", "601"),
            "RATE_LIMIT_FETCHES_PER_MINUTE": ("1", "999", "0", None),
            "FETCH_RESULT_TTL_SECONDS": ("1", "999", "0", None),
        }
        attributes = {
            "FETCH_MAX_SECONDS": "fetch_max_seconds",
            "FETCH_MAX_BYTES": "fetch_max_bytes",
            "FETCH_TIMEOUT_SECONDS": "fetch_timeout_seconds",
            "RATE_LIMIT_FETCHES_PER_MINUTE": "rate_limit_fetches_per_minute",
            "FETCH_RESULT_TTL_SECONDS": "fetch_result_ttl_seconds",
        }
        for name, (low, high, below, above) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(from_env(**{name: low}), attributes[name]), int(low))
                self.assertEqual(getattr(from_env(**{name: high}), attributes[name]), int(high))
                for bad in (below, above, "-5", "+5", " 5", "5.0", "1e3", "\u0665", "5_0", "x"):
                    if bad is None:
                        continue
                    with self.subTest(value=bad), self.assertRaisesRegex(ConfigError, name):
                        from_env(**{name: bad})


class ProxyTest(unittest.TestCase):
    def test_http_proxies_are_kept_in_a_fixed_order(self) -> None:
        settings = from_env(NO_PROXY="localhost,.example.internal,10.0.0.0/8", HTTP_PROXY="http://proxy.example.internal:3128",
                            HTTPS_PROXY="http://proxy.example.internal:3128/")
        self.assertEqual(settings.proxies, (
            ("HTTPS_PROXY", "http://proxy.example.internal:3128/"),
            ("HTTP_PROXY", "http://proxy.example.internal:3128"),
            ("NO_PROXY", "localhost,.example.internal,10.0.0.0/8"),
        ))

    def test_lowercase_variants_are_not_read(self) -> None:
        self.assertEqual(from_env(https_proxy="http://proxy.example.internal:3128").proxies, ())

    def test_bad_proxy_urls_refuse_to_start(self) -> None:
        for value in (
            # Without requests, yt-dlp's urllib handler speaks only to http:// proxies.
            "https://proxy.example.internal:3128",
            "socks5://proxy.example.internal:1080",
            "proxy.example.internal:3128",
            # Credentials would be a secret outside a *_FILE variable.
            "http://user:pass@proxy.example.internal:3128",
            "http://user@proxy.example.internal:3128",
            "http://proxy.example.internal:3128/path",
            "http://proxy.example.internal:3128?x=1",
            "http://proxy.example.internal:3128#x",
            "http://proxy.example.internal:",
            "http://proxy.example.internal:99999",
            "http://proxy.example.internal:31a8",
            "http://",
            "http://proxy.example.internal :3128",
            "http://pröxy.example.internal:3128",
            "http://proxy.example.internal:3128\n",
            "http://" + "a" * 1100,
        ):
            for name in ("HTTPS_PROXY", "HTTP_PROXY"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ConfigError, name):
                    from_env(**{name: value})

    def test_bad_no_proxy_refuses_to_start(self) -> None:
        for value in ("a b", "localhost;rm", "x\n", "é", "a" * 1025):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "NO_PROXY"):
                from_env(NO_PROXY=value)


class ImportRestrictionTest(unittest.TestCase):
    """The image copies only the core's config and http_guard (fetcher/Dockerfile)."""

    def imports(self) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names.add(module)
                    names.update(f"{module}.{alias.name}" for alias in node.names)
            found[path.name] = names
        return found

    def test_only_config_and_http_guard_come_from_the_core(self) -> None:
        found = self.imports()
        self.assertEqual(
            sorted(found), ["__init__.py", "__main__.py", "config.py", "jobs.py", "server.py", "ytdlp.py"]
        )
        allowed = {"strange_ringtone.config", "strange_ringtone.http_guard"}
        used: set[str] = set()
        for name, imports in found.items():
            for module in imports:
                if module == "strange_ringtone" or module.startswith("strange_ringtone."):
                    owner = ".".join(module.split(".")[:2])
                    with self.subTest(file=name, module=module):
                        self.assertIn(owner, allowed)
                    used.add(owner)
        self.assertEqual(used, allowed)

    def test_no_third_party_imports(self) -> None:
        # yt-dlp runs as a subprocess, never in the server's process.
        stdlib = sys.stdlib_module_names
        for name, imports in self.imports().items():
            for module in imports:
                top = module.split(".")[0]
                if top in ("strange_ringtone", "strange_ringtone_fetch", "__future__") or not top:
                    continue
                with self.subTest(file=name, module=module):
                    self.assertIn(top, stdlib)


class MainTest(unittest.TestCase):
    def test_config_error_exits_2_without_starting(self) -> None:
        log = io.StringIO()
        with mock.patch.dict(os.environ, {"PUBLIC_ORIGIN": ""}, clear=True), \
                mock.patch.object(entry, "FetchServer", side_effect=AssertionError("started")), \
                contextlib.redirect_stderr(log):
            self.assertEqual(entry.main([]), 2)
        self.assertIn("configuration error", log.getvalue())

    def test_bind_must_be_an_ip_literal(self) -> None:
        for value in ("localhost", "example.internal", "fe80::1%eth0", ""):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                entry.main(["--bind", value])
            self.assertEqual(raised.exception.code, 2)

    def test_bind_and_port(self) -> None:
        started: list[tuple[str, int]] = []

        class FakeServer:
            def __init__(self, address: tuple[str, int], settings: Settings) -> None:
                started.append(address)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        with mock.patch.dict(os.environ, {"PUBLIC_ORIGIN": ORIGIN}, clear=True), \
                mock.patch.object(entry, "FetchServer", FakeServer), \
                mock.patch.object(entry.signal, "signal"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entry.main(["--bind", "127.0.0.1"]), 0)
            self.assertEqual(entry.main([]), 0)
        self.assertEqual(started, [("127.0.0.1", 8080), ("0.0.0.0", 8080)])

    def test_checkout_entry_puts_the_core_first_and_starts(self) -> None:
        # python -I fetcher: the directory form, with the core's modules found
        # from ../core and no environment. A bad setting proves it ran main().
        checkout_entry = PACKAGE.parent / "__main__.py"
        self.assertTrue(checkout_entry.is_file())
        env = {"PUBLIC_ORIGIN": "", "PATH": os.environ.get("PATH", "")}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        proc = subprocess.run(
            [sys.executable, "-I", str(PACKAGE.parent), "--bind", "127.0.0.1"],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("strange_ringtone_fetch: configuration error: PUBLIC_ORIGIN is required", proc.stderr)


if __name__ == "__main__":
    unittest.main()
