"""The publish workflow's first step: which version a run builds, and whether it may release (docs/PLAN.md 9.3).

The owner makes each vX.Y.Z release by hand, so the tag and its commit are
chosen by a person. Nothing is built until the tag is exactly the version the
merged release PR recorded, on that PR's merge commit, and a run started by
hand is only ever a rehearsal.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import load_script
from strange_ringtone.config import Settings

check = load_script("check_release_tag")

ORIGIN = "https://example.internal"
# Two commit ids, built rather than written out: 40 hex characters each.
SHA = "01234567" * 5
LATER = "89abcdef" * 5


def release(tag: str = "v0.1.0", **changes: str) -> dict[str, str]:
    """The environment of a release of tag on the release PR's merge commit, with changes applied."""
    environ = {
        "EVENT_NAME": "release", "REF_TYPE": "tag", "REF_NAME": tag, "PRERELEASE": "false",
        "COMMIT": SHA, "MANIFEST_COMMIT": SHA,
    }
    environ.update(changes)
    return environ


def rehearsal(ref_type: str = "branch", ref_name: str = "main") -> dict[str, str]:
    return {"EVENT_NAME": "workflow_dispatch", "REF_TYPE": ref_type, "REF_NAME": ref_name}


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.manifest = Path(self._dir.name) / ".release-please-manifest.json"

    def write(self, content: object) -> None:
        text = content if isinstance(content, str) else json.dumps(content)
        self.manifest.write_text(text, encoding="utf-8", newline="\n")

    def refused(self, environ: dict[str, str]) -> str:
        with self.assertRaises(check.ReleaseError) as caught:
            check.plan(environ, self.manifest)
        return str(caught.exception)

    def test_a_release_whose_tag_is_the_manifests_version_on_the_commit_that_set_it(self) -> None:
        self.write({".": "0.1.0"})
        self.assertEqual(check.plan(release(), self.manifest), ("0.1.0", True))
        self.write({".": "12.30.401"})
        self.assertEqual(check.plan(release("v12.30.401"), self.manifest), ("12.30.401", True))

    def test_a_tag_that_is_not_exactly_v_and_three_numbers_is_refused(self) -> None:
        self.write({".": "0.1.0"})
        for tag in (
            "", "0.1.0", "V0.1.0", "v0.1", "v0.1.0.0", "v0.1.0-rc1", "v0.1.0+build", "v00.1.0", "v0.01.0",
            "v0.1.00", "v0.1.0\n", "v0.1.0 ", " v0.1.0", "v0.1.x", "latest", "main", "v٠.1.0", "v0.1.0/x",
        ):
            with self.subTest(tag=tag):
                self.assertIn("not a release tag", self.refused(release(tag)))

    def test_a_tag_other_than_the_manifests_version_is_refused(self) -> None:
        self.write({".": "0.1.0"})
        for tag in ("v0.1.1", "v0.2.0", "v1.0.0", "v0.0.9"):
            with self.subTest(tag=tag):
                message = self.refused(release(tag))
                self.assertIn("0.1.0", message)
                self.assertIn(tag, message)

    def test_a_tag_on_a_commit_after_the_release_prs_merge_is_refused(self) -> None:
        # The manifest still records 0.1.0 on every later commit, until the
        # next release PR merges; only the commit that set it is the release.
        self.write({".": "0.1.0"})
        message = self.refused(release(COMMIT=LATER))
        self.assertIn("later commits are not part of this release", message)
        self.assertIn(SHA[:12], message)
        self.assertIn(LATER[:12], message)

    def test_a_release_without_both_commits_is_refused(self) -> None:
        self.write({".": "0.1.0"})
        for changes in (
            {"COMMIT": ""}, {"MANIFEST_COMMIT": ""}, {"COMMIT": "", "MANIFEST_COMMIT": ""},
            {"COMMIT": SHA[:39], "MANIFEST_COMMIT": SHA[:39]},
            {"COMMIT": LATER.upper(), "MANIFEST_COMMIT": LATER.upper()},
            {"COMMIT": SHA + "\n", "MANIFEST_COMMIT": SHA + "\n"}, {"COMMIT": "main", "MANIFEST_COMMIT": "main"},
        ):
            with self.subTest(changes=changes):
                self.assertIn("cannot tell which commit", self.refused(release(**changes)))
        environ = release()
        del environ["COMMIT"], environ["MANIFEST_COMMIT"]
        self.assertIn("cannot tell which commit", self.refused(environ))

    def test_a_pre_release_is_refused(self) -> None:
        self.write({".": "0.1.0"})
        for value in ("true", "", "True", "FALSE", "false ", "0", "no"):
            with self.subTest(prerelease=value):
                self.assertIn("pre-release", self.refused(release(PRERELEASE=value)))
        environ = release()
        del environ["PRERELEASE"]
        self.assertIn("pre-release", self.refused(environ))

    def test_a_manifest_that_records_no_release_is_refused(self) -> None:
        # 0.0.0 is the manifest before the first release PR is merged.
        self.write({".": "0.0.0"})
        self.assertIn("records no release", self.refused(release("v0.0.0")))

    def test_an_unreadable_manifest_is_refused(self) -> None:
        self.assertIn("cannot read", self.refused(release()))
        for content in ("", "{", "[]", '"0.1.0"', "null", {"other": "0.1.0"}, {".": 1}, {".": None},
                        {".": "0.1"}, {".": "v0.1.0"}, {".": "0.1.0 "}, {".": "0.1.0\n"}, {".": "01.1.0"}):
            with self.subTest(content=content):
                self.write(content)
                self.refused(release())

    def test_a_release_event_on_anything_but_a_tag_is_refused(self) -> None:
        self.write({".": "0.1.0"})
        for ref_type in ("branch", "", "Tag"):
            with self.subTest(ref_type=ref_type):
                self.assertIn("not a tag", self.refused(release(REF_TYPE=ref_type)))

    def test_a_run_started_by_hand_is_a_rehearsal_and_only_from_main(self) -> None:
        # No manifest is read: a rehearsal runs before the first release too.
        self.assertEqual(check.plan(rehearsal(), self.manifest), ("rehearsal", False))
        for ref_type, ref_name in (("branch", "feat/x"), ("branch", ""), ("tag", "v0.1.0"), ("tag", "main"),
                                   ("branch", "main\n"), ("branch", "Main")):
            with self.subTest(ref_type=ref_type, ref_name=ref_name):
                self.assertIn("only from main", self.refused(rehearsal(ref_type, ref_name)))

    def test_a_rehearsal_never_releases_whatever_else_the_environment_says(self) -> None:
        self.write({".": "0.1.0"})
        environ = release()
        environ.update(rehearsal())
        self.assertEqual(check.plan(environ, self.manifest), ("rehearsal", False))

    def test_any_other_event_is_refused(self) -> None:
        self.write({".": "0.1.0"})
        for event in ("push", "pull_request", "pull_request_target", "workflow_run", "schedule", "", "Release"):
            with self.subTest(event=event):
                self.assertIn("release or workflow_dispatch", self.refused(release(EVENT_NAME=event)))
        self.assertIn("release or workflow_dispatch", self.refused({}))

    def test_every_version_it_hands_on_is_one_the_core_accepts(self) -> None:
        # The version becomes the image's VERSION, which the core reads at start-up.
        self.write({".": "0.1.0"})
        for environ in (release(), rehearsal()):
            version, _ = check.plan(environ, self.manifest)
            with self.subTest(version=version):
                settings = Settings.from_env({"PUBLIC_ORIGIN": ORIGIN, "VERSION": version})
                self.assertEqual(settings.version, version)


class MainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.manifest = Path(self._dir.name) / ".release-please-manifest.json"
        self.manifest.write_text('{".": "0.1.0"}\n', encoding="utf-8", newline="\n")

    def run_main(self, environ: dict[str, str], argv: list[str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", environ, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check.main([str(self.manifest)] if argv is None else argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_release_prints_the_two_outputs_and_nothing_else(self) -> None:
        code, out, err = self.run_main(release())
        self.assertEqual((code, out, err), (0, "version=0.1.0\nrelease=true\n", ""))

    def test_a_rehearsal_prints_release_false(self) -> None:
        code, out, err = self.run_main(rehearsal())
        self.assertEqual((code, out, err), (0, "version=rehearsal\nrelease=false\n", ""))

    def test_a_refusal_writes_no_output_line(self) -> None:
        # stdout is appended to $GITHUB_OUTPUT: a refusal must leave it empty.
        for environ in (
            release("v0.2.0"),
            release("v0.1.0\nrelease=true"),
            release(COMMIT=LATER),
            release(PRERELEASE="true"),
            rehearsal("branch", "feat/x"),
            release(EVENT_NAME="push"),
            {},
        ):
            with self.subTest(environ=environ):
                code, out, err = self.run_main(environ)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.strip())
                # A message on one line, with no control characters from the ref.
                self.assertEqual(err.count("\n"), 1, err)

    def test_wrong_arguments_exit_2(self) -> None:
        for argv in ([], ["a", "b"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(release(), argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("check_release_tag.py MANIFEST", err)


if __name__ == "__main__":
    unittest.main()
