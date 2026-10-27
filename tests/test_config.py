from __future__ import annotations

import ipaddress
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # puts core/ on sys.path
from strange_ringtone import config
from strange_ringtone.config import ConfigError, Settings

ORIGIN = "https://ringtones.example.internal"
ENV_EXAMPLE_FETCHER_MARKER = "# ---- Fetcher image (opt-in, docs/PLAN.md section 6) ----\n"


def from_env(**values: str) -> Settings:
    environ = {"PUBLIC_ORIGIN": ORIGIN}
    environ.update(values)
    return Settings.from_env(environ)


class DefaultsTest(unittest.TestCase):
    def test_contract_defaults(self) -> None:
        settings = Settings.from_env({"PUBLIC_ORIGIN": ORIGIN})
        self.assertEqual(settings.public_origin, ORIGIN)
        self.assertEqual(settings.trusted_proxies, ())
        self.assertEqual(settings.max_clip_seconds, 30)
        self.assertEqual(settings.default_target_lufs, -14.0)
        self.assertEqual(settings.render_timeout_seconds, 20)
        self.assertEqual(settings.render_concurrency, 2)
        self.assertEqual(settings.rate_limit_renders_per_minute, 10)
        self.assertEqual(settings.result_ttl_seconds, 600)
        self.assertEqual(settings.max_results, 50)
        self.assertIs(settings.fetch_enabled, False)
        self.assertEqual(settings.source_url, config.DEFAULT_SOURCE_URL)
        self.assertEqual(settings.version, "dev")
        self.assertEqual(settings.scratch_dir, config._default_scratch_dir())

    def test_direct_construction_uses_the_same_defaults(self) -> None:
        # Server tests build Settings directly; their defaults must match from_env's.
        self.assertEqual(Settings(public_origin=ORIGIN), Settings.from_env({"PUBLIC_ORIGIN": ORIGIN}))

    def test_empty_optional_values_mean_the_default(self) -> None:
        names = (
            "TRUSTED_PROXIES", "MAX_CLIP_SECONDS", "DEFAULT_TARGET_LUFS", "RENDER_TIMEOUT_SECONDS",
            "RENDER_CONCURRENCY", "RATE_LIMIT_RENDERS_PER_MINUTE", "RESULT_TTL_SECONDS", "MAX_RESULTS",
            "FETCH_ENABLED", "SOURCE_URL", "VERSION",
        )
        self.assertEqual(from_env(**dict.fromkeys(names, "")), Settings(public_origin=ORIGIN))

    def test_scratch_dir_is_not_configurable(self) -> None:
        # The writable paths are part of the contract, not an operator choice.
        settings = from_env(SCRATCH_DIR="/srv", TMPDIR_OVERRIDE="/srv")
        self.assertEqual(settings.scratch_dir, Settings(public_origin=ORIGIN).scratch_dir)

    def test_scratch_dir_is_fixed_under_tmp(self) -> None:
        # The contract makes /tmp the only writable path. tempfile.gettempdir()
        # would follow TMPDIR, or fall back to the working directory when /tmp
        # is not writable, so it must not be consulted at all.
        self.assertEqual(config.SCRATCH_DIR.as_posix(), "/tmp/strange_ringtone")
        with (
            mock.patch.object(config, "_WINDOWS", False),
            mock.patch.object(tempfile, "gettempdir", side_effect=AssertionError("gettempdir called")),
            mock.patch.dict(os.environ, {"TMPDIR": "/srv", "TEMP": "/srv", "TMP": "/srv"}),
        ):
            self.assertEqual(Settings(public_origin=ORIGIN).scratch_dir, config.SCRATCH_DIR)
            self.assertEqual(from_env(TMPDIR="/srv").scratch_dir, config.SCRATCH_DIR)

    @unittest.skipIf(sys.platform == "win32", "Windows checkouts use the user's temporary directory")
    def test_scratch_dir_ignores_tmpdir_here(self) -> None:
        with mock.patch.dict(os.environ, {"TMPDIR": "/srv"}), mock.patch.object(tempfile, "tempdir", None):
            self.assertEqual(Settings(public_origin=ORIGIN).scratch_dir, Path("/tmp/strange_ringtone"))

    def test_settings_are_frozen(self) -> None:
        settings = Settings(public_origin=ORIGIN)
        with self.assertRaises(AttributeError):
            settings.public_origin = "https://evil.example.internal"  # type: ignore[misc]

    def test_max_body_bytes(self) -> None:
        self.assertEqual(Settings(public_origin=ORIGIN).max_body_bytes, 30 * 48000 * 2 * 2 + 4096)
        self.assertEqual(Settings(public_origin=ORIGIN, max_clip_seconds=1).max_body_bytes, 196096)
        self.assertEqual(from_env(MAX_CLIP_SECONDS="40").max_body_bytes, 40 * 48000 * 2 * 2 + 4096)


class PublicOriginTest(unittest.TestCase):
    def test_accepts_origins(self) -> None:
        for origin in (
            "https://ringtones.example.internal",
            "http://ringtones.example.internal",
            "https://ringtones.example.internal:8443",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://[::1]:8080",
            "https://tones-2.example.internal",
        ):
            with self.subTest(origin=origin):
                # Stored exactly as given, because the Origin check compares bytes.
                self.assertEqual(Settings.from_env({"PUBLIC_ORIGIN": origin}).public_origin, origin)

    def test_missing_or_empty_refuses_to_start(self) -> None:
        for environ in ({}, {"PUBLIC_ORIGIN": ""}):
            with self.subTest(environ=environ), self.assertRaisesRegex(ConfigError, "PUBLIC_ORIGIN"):
                Settings.from_env(environ)

    def test_refuses_shapes_a_browser_never_sends(self) -> None:
        # Each of these would either never match a real Origin header (every
        # POST fails) or mean the operator wrote something other than an origin.
        for origin in (
            "https://ringtones.example.internal/",
            "https://ringtones.example.internal/app",
            "https://ringtones.example.internal?x=1",
            "https://ringtones.example.internal#top",
            "https://ringtones.example.internal?",
            "https://ringtones.example.internal#",
            "https://user@ringtones.example.internal",
            "https://user:pass@ringtones.example.internal",
            "ftp://ringtones.example.internal",
            "javascript://ringtones.example.internal",
            "ringtones.example.internal",
            "//ringtones.example.internal",
            "https:ringtones.example.internal",
            "https://",
            "https://:8443",
            "https://ringtones.example.internal:",
            "https://ringtones.example.internal:99999",
            "https://ringtones.example.internal:84a3",
            "https://ringtones.example.internal:08443",
            "https://ringtones.example.internal:443",
            "http://ringtones.example.internal:80",
            "http://[::1",
            "https://ringtones..example.internal",
            "https://ring\\tones.example.internal",
            " https://ringtones.example.internal",
            "https://ringtones.example.internal ",
            "https://ringtones.example.internal\n",
            "https://ringtones.example.internal\t",
            "https://ringtönes.example.internal",
            "null",
        ):
            with self.subTest(origin=origin), self.assertRaisesRegex(ConfigError, "PUBLIC_ORIGIN"):
                Settings.from_env({"PUBLIC_ORIGIN": origin})

    def test_refuses_ipv6_hosts_a_browser_would_write_differently(self) -> None:
        # Browsers send [::1], never a longer spelling of the same address, so
        # the byte-for-byte Origin check would fail every POST.
        for origin in (
            "http://[0:0:0:0:0:0:0:1]:8080",
            "http://[0::1]:8080",
            "http://[::0001]:8080",
            "http://[2001:db8:0:0:0:0:0:1]",
            "http://[1:0:0:0:0:0:0:0]",
            "http://[::ffff:127.0.0.1]:8080",
            "http://[::1.2.3.4]:8080",
            "http://[:::1]:8080",
            "http://[1:2:3:4:5:6:7:8:9]:8080",
        ):
            with self.subTest(origin=origin), self.assertRaisesRegex(ConfigError, "PUBLIC_ORIGIN"):
                Settings.from_env({"PUBLIC_ORIGIN": origin})

    def test_accepts_canonical_ipv6_hosts(self) -> None:
        for origin in ("http://[::1]:8080", "https://[2001:db8::1]", "https://[2001:db8:0:1:1:1:1:1]:8443"):
            with self.subTest(origin=origin):
                self.assertEqual(Settings.from_env({"PUBLIC_ORIGIN": origin}).public_origin, origin)

    def test_refuses_uppercase_with_a_hint(self) -> None:
        # Browsers send lowercase origins; an uppercase setting would make every
        # POST fail with 403, so say why instead.
        for origin in ("HTTPS://ringtones.example.internal", "https://Ringtones.example.internal",
                       "https://RINGTONES.EXAMPLE.INTERNAL", "http://[::FFFF:7f00:1]:8080"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ConfigError, "PUBLIC_ORIGIN.*lowercase"):
                Settings.from_env({"PUBLIC_ORIGIN": origin})

    def test_error_does_not_echo_the_value(self) -> None:
        # Deployment details stay out of whatever collects stderr.
        with self.assertRaises(ConfigError) as caught:
            Settings.from_env({"PUBLIC_ORIGIN": "https://secret-host.example.internal/"})
        self.assertNotIn("secret-host", str(caught.exception))


class TrustedProxiesTest(unittest.TestCase):
    def test_parses_cidrs(self) -> None:
        settings = from_env(TRUSTED_PROXIES="192.0.2.0/24, 198.51.100.7,2001:db8::/32")
        self.assertEqual(settings.trusted_proxies, (
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.7/32"),
            ipaddress.ip_network("2001:db8::/32"),
        ))

    def test_host_bits_are_allowed(self) -> None:
        # strict=False: '192.0.2.9/24' means the /24 it sits in.
        self.assertEqual(from_env(TRUSTED_PROXIES="192.0.2.9/24").trusted_proxies,
                         (ipaddress.ip_network("192.0.2.0/24"),))

    def test_refuses_bad_entries(self) -> None:
        # A typo must not quietly trust nothing (or everything): refuse to start.
        for value in ("192.0.2.0/24,", ",192.0.2.0/24", "192.0.2.0/24,,198.51.100.0/24", " ", "proxy.example.internal",
                      "192.0.2.0/33", "300.0.0.1", "192.0.2.0/24;198.51.100.0/24", "010.0.0.1"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "TRUSTED_PROXIES"):
                from_env(TRUSTED_PROXIES=value)


class IntegerSettingsTest(unittest.TestCase):
    # name -> (default, lowest allowed, highest allowed or None)
    LIMITS = {
        "MAX_CLIP_SECONDS": (30, 1, 40),
        "RENDER_TIMEOUT_SECONDS": (20, 1, None),
        "RENDER_CONCURRENCY": (2, 1, None),
        "RATE_LIMIT_RENDERS_PER_MINUTE": (10, 1, None),
        "RESULT_TTL_SECONDS": (600, 1, None),
        "MAX_RESULTS": (50, 1, None),
    }

    @staticmethod
    def field(name: str) -> str:
        return name.lower()

    def test_accepts_bounds_and_leading_zeros(self) -> None:
        for name, (_default, low, high) in self.LIMITS.items():
            for value, expected in ((str(low), low), ("007", 7), (str(high or 1000), high or 1000)):
                with self.subTest(name=name, value=value):
                    self.assertEqual(getattr(from_env(**{name: value}), self.field(name)), expected)

    def test_refuses_out_of_range(self) -> None:
        for name, (_default, low, high) in self.LIMITS.items():
            values = [str(low - 1), "0", "1" + "0" * 30]
            if high is not None:
                values.append(str(high + 1))
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ConfigError, name):
                    from_env(**{name: value})

    def test_refuses_anything_but_ascii_digits(self) -> None:
        # int() accepts all of these; an operator writing them means something
        # we can't be sure of, so refuse to start.
        for name in self.LIMITS:
            for value in ("+5", "-1", " 5", "5 ", "1_0", "1e3", "5.0", "0x5", "٥", "５", "ten", "5\n"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ConfigError, name):
                    from_env(**{name: value})

    def test_huge_digit_strings_are_refused_quickly(self) -> None:
        with self.assertRaisesRegex(ConfigError, "MAX_RESULTS"):
            from_env(MAX_RESULTS="9" * 100_000)


class TargetLufsTest(unittest.TestCase):
    def test_accepts_range(self) -> None:
        for value, expected in (("-24", -24.0), ("-9", -9.0), ("-14", -14.0), ("-16.5", -16.5), ("-09.0", -9.0)):
            with self.subTest(value=value):
                self.assertEqual(from_env(DEFAULT_TARGET_LUFS=value).default_target_lufs, expected)

    def test_refuses_out_of_range(self) -> None:
        for value in ("-24.01", "-8.99", "-25", "0", "9", "-100"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "DEFAULT_TARGET_LUFS"):
                from_env(DEFAULT_TARGET_LUFS=value)

    def test_refuses_non_decimal_forms(self) -> None:
        # float() accepts every one of these, including values that compare
        # strangely (nan is neither inside nor outside a range).
        for value in ("nan", "-inf", "inf", "-1.4e1", "−14", "+14", "-14.", "-.5", " -14", "-14 ", "-1_4",
                      "--14", "-14,5"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "DEFAULT_TARGET_LUFS"):
                from_env(DEFAULT_TARGET_LUFS=value)


class FetchEnabledTest(unittest.TestCase):
    def test_accepts_boolean_words(self) -> None:
        for value, expected in (("true", True), ("TRUE", True), ("Yes", True), ("1", True),
                                ("false", False), ("False", False), ("NO", False), ("0", False)):
            with self.subTest(value=value):
                self.assertIs(from_env(FETCH_ENABLED=value).fetch_enabled, expected)

    def test_refuses_anything_else(self) -> None:
        for value in ("on", "off", "y", "n", "2", "enabled", " true", "true ", "ｔｒｕｅ", "truıe"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "FETCH_ENABLED"):
                from_env(FETCH_ENABLED=value)


class SourceUrlTest(unittest.TestCase):
    def test_accepts_http_urls(self) -> None:
        for value in ("https://git.example.internal/fork/strange_ringtone", "http://git.example.internal"):
            with self.subTest(value=value):
                self.assertEqual(from_env(SOURCE_URL=value).source_url, value)

    def test_refuses_other_schemes_and_relative_urls(self) -> None:
        # The URL becomes an href, where escaping does not stop javascript: or data:.
        for value in ("javascript:alert(1)", "JavaScript:alert(1)", "data:text/html,x", "//git.example.internal/x",
                      "/source", "git.example.internal/x", "https:///x", "ftp://git.example.internal/x",
                      " https://git.example.internal", "https://git.example.internal/\nx",
                      "java\tscript:alert(1)", "https://git.example.internal/ü"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "SOURCE_URL"):
                from_env(SOURCE_URL=value)


class VersionTest(unittest.TestCase):
    def test_accepts_release_and_dev_versions(self) -> None:
        for value in ("dev", "0.1.0", "1.2.3-rc.1", "v0.1.0", "a" * 64, "build_7"):
            with self.subTest(value=value):
                self.assertEqual(from_env(VERSION=value).version, value)

    def test_refuses_values_unsafe_as_a_path_segment(self) -> None:
        # VERSION is matched exactly as /static/<version>/; separators, dots at
        # the start, or escapes would let it collide with other paths.
        for value in ("..", ".", "../x", "a/b", "a\\b", "a%2fb", "-dev", ".dev", "_dev", "a" * 65, "dév",
                      "dev ", " dev", "dev\n", "a b", "a?b", "a#b"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "VERSION"):
                from_env(VERSION=value)


class ReadRecorder(dict[str, str]):
    """An environment that remembers every variable the settings look up."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.looked_up: set[str] = set()

    def get(self, key: str, default: object = None) -> object:
        self.looked_up.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self.looked_up.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.looked_up.add(key)
        return super().__contains__(key)


class ContractDocsTest(unittest.TestCase):
    """The variables the server reads, the PLAN 5.7 table and .env.example agree."""

    def read_variables(self) -> set[str]:
        environ = ReadRecorder({"PUBLIC_ORIGIN": ORIGIN})
        Settings.from_env(environ)
        return environ.looked_up

    def plan_variables(self) -> set[str]:
        text = (helpers.ROOT / "docs" / "PLAN.md").read_text(encoding="utf-8")
        section = text.split("### 5.7 ", 1)[1].split("\n## ", 1)[0]
        return set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", section, re.MULTILINE))

    def env_example_blocks(self) -> tuple[str, str]:
        # .env.example serves both images (compose.example.yaml gives both the
        # same .env); the fetcher's settings follow its marker line.
        text = (helpers.ROOT / ".env.example").read_text(encoding="utf-8")
        core, marker, fetcher = text.partition(ENV_EXAMPLE_FETCHER_MARKER)
        self.assertTrue(marker, "the fetcher block's marker line is missing from .env.example")
        return core, fetcher

    @staticmethod
    def variables_in(text: str) -> set[str]:
        return set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))

    def test_plan_table_lists_every_variable_read(self) -> None:
        self.assertEqual(self.plan_variables(), self.read_variables())

    def test_env_example_lists_every_variable_read(self) -> None:
        self.assertEqual(self.variables_in(self.env_example_blocks()[0]), self.read_variables())

    def test_env_example_fetcher_block_lists_every_fetcher_variable(self) -> None:
        from strange_ringtone_fetch.config import Settings as FetcherSettings

        environ = ReadRecorder({"PUBLIC_ORIGIN": ORIGIN})
        FetcherSettings.from_env(environ)
        # PUBLIC_ORIGIN and TRUSTED_PROXIES are shared, documented once above.
        shared = {"PUBLIC_ORIGIN", "TRUSTED_PROXIES"}
        self.assertLessEqual(shared, environ.looked_up)
        self.assertEqual(self.variables_in(self.env_example_blocks()[1]), environ.looked_up - shared)


if __name__ == "__main__":
    unittest.main()
