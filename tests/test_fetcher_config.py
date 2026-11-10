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
from test_config import ReadRecorder

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
        self.assertEqual(settings.fetch_max_bytes, 41943040)
        self.assertEqual(settings.fetch_timeout_seconds, 120)
        self.assertEqual(settings.rate_limit_fetches_per_minute, 5)
        self.assertEqual(settings.fetch_result_ttl_seconds, 600)
        # S2: the default-route start check is on unless the operator turns it off.
        self.assertIs(settings.allow_default_route, False)
        # The compose service of the runner, reached over `jobs` (docs/PLAN.md 6.1).
        self.assertEqual(settings.runner_url, config.DEFAULT_RUNNER_URL)
        self.assertEqual(config.DEFAULT_RUNNER_URL, "http://fetcher-runner:8080")
        self.assertEqual(settings.scratch_dir, config._default_scratch_dir())
        # The video length cap and the proxies are the runner's settings now
        # (strange_ringtone_runner.config): the fetcher runs no yt-dlp.
        self.assertFalse(hasattr(settings, "fetch_max_seconds"))
        self.assertFalse(hasattr(settings, "proxies"))

    def test_direct_construction_uses_the_same_defaults(self) -> None:
        self.assertEqual(Settings(public_origin=ORIGIN), from_env())

    def test_empty_optional_values_mean_the_default(self) -> None:
        names = (
            "TRUSTED_PROXIES", "FETCH_MAX_BYTES", "FETCH_TIMEOUT_SECONDS",
            "RATE_LIMIT_FETCHES_PER_MINUTE", "FETCH_RESULT_TTL_SECONDS", "FETCH_ALLOW_DEFAULT_ROUTE",
            "FETCH_RUNNER_URL",
        )
        self.assertEqual(from_env(**dict.fromkeys(names, "")), Settings(public_origin=ORIGIN))

    def test_allow_default_route_is_parsed_like_the_cores_booleans(self) -> None:
        # S2: the override takes the core's boolean words and nothing else. A
        # value the parser doesn't know refuses to start, so a typo never turns
        # the check off, or leaves it on against what the operator wrote.
        for value in ("true", "1", "yes", "TRUE", "Yes"):
            with self.subTest(value=value):
                self.assertIs(from_env(FETCH_ALLOW_DEFAULT_ROUTE=value).allow_default_route, True)
        for value in ("false", "0", "no", "FALSE"):
            with self.subTest(value=value):
                self.assertIs(from_env(FETCH_ALLOW_DEFAULT_ROUTE=value).allow_default_route, False)
        # No stripping and no Unicode digits (Arabic-Indic one), as in the
        # core's _parse_bool.
        for value in ("on", "2", " true", "true ", "\u0661", "y"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "FETCH_ALLOW_DEFAULT_ROUTE"):
                from_env(FETCH_ALLOW_DEFAULT_ROUTE=value)
        # The lowercase spelling is not the setting.
        self.assertIs(from_env(fetch_allow_default_route="true").allow_default_route, False)

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

    def test_an_empty_trusted_proxies_trusts_no_proxy(self) -> None:
        # compose.example.yaml sets TRUSTED_PROXIES: "" on the fetcher (owner,
        # 2026-11-05): empty must mean "trust none", never an error and never
        # a catch-all, so the rate limit keys on the socket peer, the runner's
        # address included, whatever X-Forwarded-For says.
        from strange_ringtone.http_guard import client_address

        trusted = from_env(TRUSTED_PROXIES="").trusted_proxies
        self.assertEqual(trusted, ())
        self.assertEqual(client_address("192.0.2.10", ["198.51.100.1"], trusted), "192.0.2.10")
        # Control: a listed proxy's X-Forwarded-For is read.
        listed = from_env(TRUSTED_PROXIES="192.0.2.0/24").trusted_proxies
        self.assertEqual(client_address("192.0.2.10", ["198.51.100.1"], listed), "198.51.100.1")

    def test_trusted_proxies(self) -> None:
        self.assertEqual([str(n) for n in from_env(TRUSTED_PROXIES="10.0.0.0/8, ::1").trusted_proxies],
                         ["10.0.0.0/8", "::1/128"])
        with self.assertRaisesRegex(ConfigError, "TRUSTED_PROXIES"):
            from_env(TRUSTED_PROXIES="10.0.0.0/8,,")

    def test_messages_never_repeat_the_value(self) -> None:
        secret = "s3cr3t-value"
        for name in ("FETCH_MAX_BYTES", "FETCH_RUNNER_URL", "PUBLIC_ORIGIN", "FETCH_ALLOW_DEFAULT_ROUTE"):
            # The message check stays inside the subTest: when a name raises
            # nothing, its subTest fails alone instead of erroring on the next line.
            with self.subTest(name=name):
                with self.assertRaises(ConfigError) as raised:
                    from_env(**{name: f"http://user:{secret}@example.internal x"})
                self.assertNotIn(secret, str(raised.exception))


class LimitsTest(unittest.TestCase):
    def test_bounds(self) -> None:
        cases = {
            "FETCH_MAX_BYTES": ("1048576", "536870912", "1048575", "536870913"),
            "FETCH_TIMEOUT_SECONDS": ("1", "600", "0", "601"),
            "RATE_LIMIT_FETCHES_PER_MINUTE": ("1", "999", "0", None),
            "FETCH_RESULT_TTL_SECONDS": ("1", "999", "0", None),
        }
        attributes = {
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


class RunnerUrlTest(unittest.TestCase):
    """FETCH_RUNNER_URL: where the fetcher finds its runner, checked like a proxy URL (docs/PLAN.md 6.1)."""

    def test_default_is_the_compose_service(self) -> None:
        # The variable is read; unset or empty means the compose service.
        environ = ReadRecorder({"PUBLIC_ORIGIN": ORIGIN})
        self.assertEqual(Settings.from_env(environ).runner_url, "http://fetcher-runner:8080")
        self.assertIn("FETCH_RUNNER_URL", environ.looked_up)
        self.assertEqual(from_env(FETCH_RUNNER_URL="").runner_url, "http://fetcher-runner:8080")

    def test_a_valid_url_is_kept_as_written(self) -> None:
        for value in ("http://runner.example.invalid:8081", "http://198.51.100.7:8080", "http://[2001:db8::7]:8080",
                      "http://fetcher-runner"):
            with self.subTest(value=value):
                self.assertEqual(from_env(FETCH_RUNNER_URL=value).runner_url, value)

    def test_bad_urls_refuse_to_start(self) -> None:
        for value in (
            # Plain HTTP over `jobs` only: the client speaks nothing else.
            "https://runner.example.invalid:8080",
            "socks5://runner.example.invalid:1080",
            "runner.example.invalid:8080",
            # Credentials would be a secret outside a *_FILE variable.
            "http://user:pass@runner.example.invalid:8080",
            "http://user@runner.example.invalid:8080",
            "http://runner.example.invalid:8080/path",
            "http://runner.example.invalid:8080?x=1",
            "http://runner.example.invalid:8080#x",
            "http://runner.example.invalid:",
            "http://runner.example.invalid:99999",
            "http://runner.example.invalid:80a8",
            "http://",
            "http://runner.example.invalid :8080",
            "http://r\u00fcnner.example.invalid:8080",
            "http://runner.example.invalid:8080\n",
            "http://" + "a" * 1100,
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "FETCH_RUNNER_URL"):
                from_env(FETCH_RUNNER_URL=value)

    def test_the_refusals_fit_a_runner_address_too(self) -> None:
        # RC1.9: one check for the proxies and FETCH_RUNNER_URL, so no
        # message may call the runner's address a proxy.
        for value in ("http://runner.example.invalid:99999", "https://runner.example.invalid:8080",
                      "http://user@runner.example.invalid:8080", "http://runner.example.invalid:8080/path"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "FETCH_RUNNER_URL") as raised:
                from_env(FETCH_RUNNER_URL=value)
            self.assertNotIn("proxy", str(raised.exception).lower())

    def test_the_fetcher_reads_no_proxy_variable_and_no_max_seconds(self) -> None:
        # R2: the proxies reach the runner's yt-dlp, and FETCH_MAX_SECONDS is
        # the runner's filter; the fetcher looks up none of them, so it can't
        # pass them anywhere and a value it doesn't use can't stop it.
        environ = ReadRecorder({"PUBLIC_ORIGIN": ORIGIN})
        Settings.from_env(environ)
        self.assertIn("FETCH_RUNNER_URL", environ.looked_up)
        never = {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "FETCH_MAX_SECONDS"}
        self.assertEqual(environ.looked_up & never, set())
        settings = from_env(HTTPS_PROXY="socks5://x.example.invalid:1", FETCH_MAX_SECONDS="0")
        self.assertFalse(hasattr(settings, "proxies"))
        self.assertFalse(hasattr(settings, "fetch_max_seconds"))
        self.assertEqual(settings, Settings(public_origin=ORIGIN))


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
            sorted(found),
            ["__init__.py", "__main__.py", "config.py", "jobs.py", "metadata.py", "routes.py", "runner_client.py",
             "server.py"],
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

    def test_the_fetcher_never_imports_the_runner_package(self) -> None:
        # The fetcher's process holds no yt-dlp run code: that is the runner's
        # (docs/PLAN.md 6.1), and yt-dlp itself runs only in its venv.
        for name, imports in self.imports().items():
            for module in imports:
                top = module.split(".")[0]
                with self.subTest(file=name, module=module):
                    self.assertNotIn(top, ("strange_ringtone_runner", "yt_dlp", "yt_dlp_ejs"))

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

        # The override: the host has a default route (or, on Windows, no
        # /proc), so the start check would refuse here; S2 has its own tests
        # in tests/test_fetcher_routes.py.
        environ = {"PUBLIC_ORIGIN": ORIGIN, "FETCH_ALLOW_DEFAULT_ROUTE": "true"}
        with mock.patch.dict(os.environ, environ, clear=True), \
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
