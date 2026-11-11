"""What the docs say about containing the core's ffmpeg is true of the code (sections 5.3, 5.7 and 8 of the plan).

The sentences checked here describe security controls: the caps on the core's
container, the renderer's limits and what is still not built. A sentence that
claims a control the code lacks, or denies one it has, misleads the operator
who sizes a deployment from it, so each claim is read against the code or
against compose.example.yaml.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import helpers
from strange_ringtone.render import Limits, measure_args

README = helpers.ROOT / "README.md"
PLAN = helpers.ROOT / "docs" / "PLAN.md"
COMPOSE = helpers.ROOT / "compose.example.yaml"
MIB = 1024 * 1024


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace folded to single spaces, so a phrase matches across a line break."""
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """The body under a '### ' heading that starts with heading, up to the next heading of level 2 or 3."""
    marker = f"\n### {heading}"
    if marker not in text:
        raise AssertionError(f"no '### {heading}' heading")
    body = text.split(marker, 1)[1]
    return re.split(r"\n#{2,3} ", body, maxsplit=1)[0]


def _bullet(text: str, start: str) -> str:
    """One top-level '- ' bullet, from its start to the next bullet, blank line or heading."""
    index = text.find(f"\n{start}")
    if index < 0:
        raise AssertionError(f"no bullet starting {start!r}")
    body = text[index + 1:]
    end = re.search(r"\n(?:- |\n|#)", body)
    return body if end is None else body[:end.start()]


def _core_block() -> str:
    """The core service's lines in compose.example.yaml, comments included."""
    lines = _text(COMPOSE).splitlines()
    if "  core:" not in lines:
        raise AssertionError("compose.example.yaml has no core service")
    block: list[str] = []
    for line in lines[lines.index("  core:") + 1:]:
        # Two spaces and a name: the next service. Column 0: the next top-level key.
        if re.match(r"(?:  )?[A-Za-z]", line):
            break
        block.append(line)
    return "\n".join(block)


def _core_setting(name: str) -> str:
    match = re.search(rf"^    {name}: *(\S+)$", _core_block(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"the core service in compose.example.yaml sets no {name}")
    return match[1]


class ComposeCommentTest(unittest.TestCase):
    def test_the_core_caps_say_what_they_are_counted_from(self) -> None:
        # The comments above the caps are the derivation an operator follows
        # when changing a setting; RENDER_CONCURRENCY has no upper bound in
        # config.py, so the pids_limit comment must say it grows with it.
        _core_setting("pids_limit")
        _core_setting("mem_limit")
        comments = _flat(" ".join(line.strip().lstrip("#") for line in _core_block().splitlines()
                                  if line.strip().startswith("#")))
        for name in ("MAX_WORKERS", "RENDER_CONCURRENCY"):
            with self.subTest(name=name):
                self.assertIn(name, comments)


class ReadmeTest(unittest.TestCase):
    def setUp(self) -> None:
        if not README.is_file():
            self.skipTest("README.md is not here")

    def test_the_ffmpeg_bullet_names_the_caps_the_example_sets(self) -> None:
        bullet = _flat(_bullet(_text(README), "- *ffmpeg*"))
        self.assertNotIn("no memory or process cap", bullet)
        self.assertIn("`pids_limit`", bullet)
        self.assertIn("`mem_limit`", bullet)
        # The image sets no process limit itself, so the bound is only as
        # good as the deployment's compose file.
        self.assertRegex(bullet, r"no (?:bound|limit|cap) on (?:its )?processes")


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        if not PLAN.is_file():
            self.skipTest("docs/PLAN.md is not here")
        self.plan = _text(PLAN)

    def test_the_render_pipeline_names_the_arguments_every_run_starts_with(self) -> None:
        section = _flat(_section(self.plan, "5.3 "))
        args = measure_args(Path("in.wav"))
        common = " ".join(args[:args.index("-f")])
        self.assertIn(common, section)

    def test_the_render_pipeline_states_the_limits_the_code_sets(self) -> None:
        section = _flat(_section(self.plan, "5.3 "))
        size = Limits().file_size_bytes
        self.assertEqual(size % MIB, 0, "the docs state the file-size limit in MiB")
        self.assertIn(f"{size // MIB} MiB", section)
        # The renderer reads the pipes itself and keeps a bounded tail;
        # communicate() keeps everything, and the core no longer uses it.
        self.assertNotIn("communicate()", section)
        # No process limit in prlimit, and why: the cgroup's cap does it.
        self.assertIn("RLIMIT_NPROC", section)
        self.assertIn("`pids_limit`", section)

    def test_the_contract_draft_names_the_core_caps(self) -> None:
        section = _section(self.plan, "5.7 ")
        rows = [line for line in section.splitlines() if line.startswith("| Runs with |")]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        pids = _core_setting("pids_limit")
        memory = _core_setting("mem_limit")
        self.assertIn("`pids_limit`", row)
        self.assertIn("`mem_limit`", row)
        self.assertRegex(row, rf"\b{re.escape(pids)}\b")
        match = re.fullmatch(r"(\d+)m", memory)
        self.assertIsNotNone(match, f"mem_limit {memory!r} is not in MiB as the example writes it")
        assert match is not None
        self.assertRegex(row, rf"\b{match[1]} ?(?:MB|MiB|m)\b")

    def test_the_open_risk_says_what_holds_now(self) -> None:
        bullet = _flat(_bullet(self.plan, "- **An exploited ffmpeg.**"))
        # Claims the code no longer makes true.
        for stale in (
            "only on a timeout or an error",
            "which is dumpable",
            "no `mem_limit` and no `pids_limit`",
            "a group kill at the end of every render",
            "a non-dumpable server as the runner is",
        ):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, bullet)
        # What remains, and what is still not built.
        self.assertIn("setsid()", bullet)
        self.assertIn("Not built: a UID or a container of ffmpeg's own", bullet)
        # Accepting a risk is the owner's decision, not the docs'.
        self.assertNotRegex(bullet, r"(?i)\baccepted\b")


if __name__ == "__main__":
    unittest.main()
