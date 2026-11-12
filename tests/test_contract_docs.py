"""The README's deployment contract for the core says what the code and the compose example do.

From v0.1.0 a deployment sizes and configures itself from that section, and CI
protects its text (scripts/check_contract_change.py). These tests protect its
truth: a default, a range or a cap that the README states and the code or
compose.example.yaml no longer has fails here.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import helpers
from strange_ringtone.config import ConfigError, Settings

README = helpers.ROOT / "README.md"
COMPOSE = helpers.ROOT / "compose.example.yaml"
CONFIG = helpers.CORE / "strange_ringtone" / "config.py"
ORIGIN = "https://example.internal"
MINUS = "\N{MINUS SIGN}"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _contract() -> str:
    text = _text(README)
    if "\n## Deployment contract\n" not in text:
        raise AssertionError("the README has no '## Deployment contract' section")
    return re.split(r"\n## ", text.split("\n## Deployment contract\n", 1)[1], maxsplit=1)[0]


def _subsection(heading: str) -> str:
    contract = _contract()
    marker = f"\n### {heading}\n"
    if marker not in contract:
        raise AssertionError(f"the contract has no '### {heading}' section")
    return re.split(r"\n### ", contract.split(marker, 1)[1], maxsplit=1)[0]


def _rows(section: str, first_header: str) -> dict[str, list[str]]:
    """The table whose first header cell is first_header: first cell -> the other cells."""
    rows: dict[str, list[str]] = {}
    inside = False
    for line in section.splitlines():
        if not line.startswith("|"):
            if inside:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        if cells[0] == first_header:
            inside = True
            continue
        if inside and not set(cells[0]) <= {"-"}:
            rows[cells[0]] = cells[1:]
    if not rows:
        raise AssertionError(f"no table starting with '| {first_header} |'")
    return rows


def _core_setting(name: str) -> str:
    lines = _text(COMPOSE).splitlines()
    block: list[str] = []
    for line in lines[lines.index("  core:") + 1:]:
        if re.match(r"(?:  )?[A-Za-z]", line):
            break
        block.append(line)
    match = re.search(rf"^    {name}: *(\S+)$", "\n".join(block), re.MULTILINE)
    if match is None:
        raise AssertionError(f"the core service in compose.example.yaml sets no {name}")
    return match[1]


class CoreVariablesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = _rows(_subsection("Core image"), "Variable")
        self.defaults = Settings.from_env({"PUBLIC_ORIGIN": ORIGIN})

    def default(self, name: str) -> str:
        self.assertIn(f"`{name}`", self.rows)
        return self.rows[f"`{name}`"][0]

    def test_the_table_lists_exactly_the_variables_the_core_reads(self) -> None:
        source = _text(CONFIG)
        body = source.split("def from_env", 1)[1].split("\n    @", 1)[0].split("\ndef ", 1)[0]
        read = set(re.findall(r'"([A-Z][A-Z_]+)"', body))
        self.assertIn("PUBLIC_ORIGIN", read)
        self.assertEqual({name.strip("`") for name in self.rows}, read)

    def test_the_required_variable_is_required(self) -> None:
        self.assertEqual(self.default("PUBLIC_ORIGIN"), "*(required)*")
        with self.assertRaises(ConfigError):
            Settings.from_env({})

    def test_the_numeric_defaults_are_the_codes(self) -> None:
        for name, value in (
            ("MAX_CLIP_SECONDS", self.defaults.max_clip_seconds),
            ("RENDER_TIMEOUT_SECONDS", self.defaults.render_timeout_seconds),
            ("RENDER_CONCURRENCY", self.defaults.render_concurrency),
            ("RATE_LIMIT_RENDERS_PER_MINUTE", self.defaults.rate_limit_renders_per_minute),
            ("RESULT_TTL_SECONDS", self.defaults.result_ttl_seconds),
            ("MAX_RESULTS", self.defaults.max_results),
        ):
            with self.subTest(variable=name):
                self.assertEqual(self.default(name), str(value))
        lufs = self.default("DEFAULT_TARGET_LUFS").replace(MINUS, "-")
        self.assertEqual(lufs, f"{self.defaults.default_target_lufs:g}")

    def test_the_other_defaults_are_the_codes(self) -> None:
        self.assertEqual(self.default("TRUSTED_PROXIES"), "empty")
        self.assertEqual(self.defaults.trusted_proxies, ())
        self.assertEqual(self.default("FETCH_ENABLED"), "false")
        self.assertIs(self.defaults.fetch_enabled, False)
        self.assertIn(f"`{self.defaults.version}` outside a release", self.default("VERSION"))

    def test_the_ranges_it_states_are_the_ones_the_code_enforces(self) -> None:
        notes = self.rows["`MAX_CLIP_SECONDS`"][1]
        match = re.fullmatch(r"(\d+) to (\d+)", notes)
        self.assertIsNotNone(match, notes)
        assert match is not None
        low, high = int(match[1]), int(match[2])
        for value, accepted in ((low, True), (high, True), (low - 1, False), (high + 1, False)):
            with self.subTest(max_clip_seconds=value):
                environ = {"PUBLIC_ORIGIN": ORIGIN, "MAX_CLIP_SECONDS": str(value)}
                if accepted:
                    self.assertEqual(Settings.from_env(environ).max_clip_seconds, value)
                else:
                    with self.assertRaises(ConfigError):
                        Settings.from_env(environ)
        notes = self.rows["`DEFAULT_TARGET_LUFS`"][1].replace(MINUS, "-")
        match = re.fullmatch(r"(-\d+) to (-\d+)", notes)
        self.assertIsNotNone(match, notes)
        assert match is not None
        low, high = int(match[1]), int(match[2])
        for value, accepted in ((low, True), (high, True), (low - 1, False), (high + 1, False)):
            with self.subTest(default_target_lufs=value):
                environ = {"PUBLIC_ORIGIN": ORIGIN, "DEFAULT_TARGET_LUFS": str(value)}
                if accepted:
                    self.assertEqual(Settings.from_env(environ).default_target_lufs, value)
                else:
                    with self.assertRaises(ConfigError):
                        Settings.from_env(environ)


class CoreContainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = _rows(_subsection("Core image"), "Item")

    def test_the_caps_are_the_compose_examples(self) -> None:
        row = self.rows["Runs with"][0]
        for name in ("pids_limit", "mem_limit", "memswap_limit"):
            with self.subTest(setting=name):
                self.assertIn(f"`{name}: {_core_setting(name)}`", row)
        # No swap on top of the memory limit: the contract's figure is the bound.
        self.assertEqual(_core_setting("memswap_limit"), _core_setting("mem_limit"))

    def test_user_port_and_health_are_the_images(self) -> None:
        dockerfile = _text(helpers.CORE / "Dockerfile")
        self.assertIn("\nUSER 10001:10001\n", dockerfile)
        self.assertIn("UID 10001, GID 10001", self.rows["User"][0])
        self.assertIn("\nEXPOSE 8080\n", dockerfile)
        self.assertTrue(self.rows["Port"][0].startswith("8080/tcp"))
        self.assertIn("`GET /healthz` returns 200", self.rows["Health"][0])
        self.assertEqual(_core_setting("user"), '"10001:10001"')
        self.assertEqual(_core_setting("read_only"), "true")


class ImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _subsection("Images and verification")
        self.flat = " ".join(self.section.split())

    def test_the_verification_command_names_the_workflow_that_publishes_and_a_release_tag(self) -> None:
        commands = [line for line in self.section.splitlines() if line.startswith("gh attestation verify ")]
        self.assertEqual(len(commands), 1, commands)
        command = commands[0]
        self.assertIn(" oci://ghcr.io/<owner>/strange_ringtone@sha256:<digest> ", command)
        self.assertIn(" --repo <owner>/strange_ringtone ", command)
        self.assertTrue((helpers.ROOT / ".github" / "workflows" / "publish.yml").is_file())
        self.assertIn(" --signer-workflow <owner>/strange_ringtone/.github/workflows/publish.yml ", command)
        # Without the tag's ref, a rehearsal's untagged image would verify too.
        self.assertTrue(command.endswith(" --source-ref refs/tags/v<version>"), command)

    def test_it_promises_what_the_publish_workflow_does(self) -> None:
        self.assertIn("There is no `latest` tag", self.flat)
        self.assertIn("set once and never moved", self.flat)
        self.assertIn("Pin the digest", self.flat)
        rows = _rows(self.section, "Image")
        self.assertEqual(rows["Core"], ["`ghcr.io/<owner>/strange_ringtone`"])
        self.assertEqual(rows["Fetcher (opt-in)"], ["`ghcr.io/<owner>/strange_ringtone-fetcher`"])


class FetcherContractTest(unittest.TestCase):
    def test_no_fetcher_container_gets_swap_either(self) -> None:
        row = _rows(_subsection("Fetcher image (opt-in)"), "Item")["Runs with"][0]
        self.assertIn("`memswap_limit` equal to `mem_limit`", row)

    def test_the_forwarders_limits_are_no_longer_provisional(self) -> None:
        contract = " ".join(_contract().split())
        self.assertNotRegex(contract, r"(?i)\bprovisional\b")
        self.assertNotRegex(contract, r"(?i)\bdraft\b")
        self.assertIn("a release that changes one is a breaking change", contract)


if __name__ == "__main__":
    unittest.main()
