"""The release flow as the workflow files state it (docs/PLAN.md 9.1 and 9.3).

Two kinds of test. The static ones read the YAML as text, because the suite is
stdlib-only: which jobs hold a write scope, what each step is given, that no
image is tagged before its scan passed, that a hand-started run can never
release, and that nothing an event carries is pasted into a shell script. The
behaviour ones take the shell scripts out of publish.yml and run them, on
POSIX, against a fake `docker`, `gh` or `git`: a check whose refusal was
removed would still read the same to the static tests.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers

WORKFLOWS = helpers.ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish.yml"
RELEASE_PLEASE = WORKFLOWS / "release-please.yml"
SECURITY_GATE = WORKFLOWS / "security_gate.yml"
DEPENDABOT = helpers.ROOT / ".github" / "dependabot.yml"
SBOM_PIN = helpers.ROOT / ".github" / "sbom" / "Dockerfile"
CONFIG = helpers.ROOT / "release-please-config.json"
MANIFEST = helpers.ROOT / ".release-please-manifest.json"
COMPOSE = helpers.ROOT / "compose.example.yaml"

SECURITY_REPOSITORY = "strangebasisdevs/strange_security"
RELEASE_ONLY = "needs.check.outputs.release == 'true'"
RUNNER = "ubuntu-24.04"
# Every write scope in the repository, by workflow file and job ("" is the
# workflow's own permissions block). A new one must be added here on purpose.
WRITE_SCOPES = {
    ("security_gate.yml", ""): {"security-events"},
    ("release-please.yml", "release-please"): {"contents", "pull-requests"},
    ("publish.yml", "build"): {"packages"},
    ("publish.yml", "scan"): {"security-events"},
    ("publish.yml", "attest"): {"attestations", "id-token", "packages"},
    ("publish.yml", "tag"): {"contents", "packages"},
    ("publish.yml", "relabel"): {"pull-requests"},
}
PUBLISH_PERMISSIONS = {
    "check": {"contents": "read"},
    "build": {"contents": "read", "packages": "write"},
    "scan": {"actions": "read", "contents": "read", "security-events": "write"},
    "attest": {"attestations": "write", "id-token": "write", "packages": "write"},
    "tag": {"contents": "write", "packages": "write"},
    "relabel": {"pull-requests": "write"},
}
VERSION = "${{ needs.check.outputs.version }}"
BUILT = {
    "CORE_IMAGE": "${{ needs.build.outputs.core-image }}",
    "CORE_DIGEST": "${{ needs.build.outputs.core-digest }}",
    "FETCHER_IMAGE": "${{ needs.build.outputs.fetcher-image }}",
    "FETCHER_DIGEST": "${{ needs.build.outputs.fetcher-digest }}",
}
LOGIN = 'printf \'%s\' "$REGISTRY_TOKEN" | docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin'
POSIX_SHELL = os.name == "posix" and shutil.which("bash") is not None

CORE = "ghcr.io/owner/tone"
FETCHER = "ghcr.io/owner/tone-fetcher"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
SHA = "01234567" * 5


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The text without comment-only lines, so a comment can neither satisfy nor fail a structural check."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _jobs(text: str, raw: bool = False) -> dict[str, str]:
    """Job id -> the job's lines; raw keeps comment lines, which a script's exact text needs."""
    source = text if raw else _code(text)
    if "\njobs:\n" not in source:
        raise AssertionError("no jobs: key")
    jobs: dict[str, str] = {}
    current: str | None = None
    for line in source.split("\njobs:\n", 1)[1].splitlines():
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if match is not None:
            current = match[1]
            jobs[current] = ""
        elif line and not line.startswith(" "):
            break
        elif current is not None:
            jobs[current] += line + "\n"
    return jobs


def _mapping(block: str, key: str, indent: int) -> dict[str, str] | None:
    """The entries under `key:` at the given indentation; {} for `key: {}`; None when the key is absent.

    A value of `|` is the block scalar that follows, its lines stripped and joined.
    """
    pad = " " * indent
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line == f"{pad}{key}: {{}}":
            return {}
        if line != f"{pad}{key}:":
            continue
        entries: dict[str, str] = {}
        cursor = index + 1
        while cursor < len(lines):
            match = re.fullmatch(rf"{pad}  ([A-Za-z0-9_-]+):(?: (.*))?", lines[cursor])
            if match is None:
                break
            cursor += 1
            value = match[2] or ""
            if value == "|":
                body: list[str] = []
                while cursor < len(lines) and lines[cursor].startswith(f"{pad}    "):
                    body.append(lines[cursor].strip())
                    cursor += 1
                value = "\n".join(body)
            entries[match[1]] = value
        return entries
    return None


def _top(text: str) -> str:
    return _code(text).split("\njobs:\n", 1)[0]


def _needs(block: str) -> set[str]:
    match = re.search(r"(?m)^    needs: (.+)$", block)
    if match is None:
        return set()
    value = match[1].strip()
    if value.startswith("["):
        return {item.strip() for item in value.strip("[]").split(",") if item.strip()}
    return {value}


def _condition(block: str) -> str | None:
    match = re.search(r"(?m)^    if: (.+)$", block)
    return None if match is None else match[1].strip()


def _steps(block: str) -> list[str]:
    """A job's steps, each as its own text."""
    if "    steps:\n" not in block:
        return []
    steps: list[str] = []
    for line in block.split("    steps:\n", 1)[1].splitlines():
        if line.startswith("      - "):
            steps.append(line + "\n")
        elif steps:
            steps[-1] += line + "\n"
    return steps


def _step(block: str, name: str) -> str:
    found = [step for step in _steps(block) if step.startswith(f"      - name: {name}\n")]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} steps named {name!r}")
    return found[0]


def _uses(step: str) -> str | None:
    match = re.search(r"(?m)^        uses: (\S+)", step)
    return None if match is None else match[1]


def _script(step: str) -> str | None:
    """A step's run script as the runner's shell gets it; None for a step without one."""
    lines = step.splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"        run: (.*)", line)
        if match is None:
            continue
        if not re.fullmatch(r"[|>][+-]?", match[1]):
            return match[1] + "\n"
        body: list[str] = []
        for following in lines[index + 1:]:
            if following.strip() and not following.startswith("          "):
                break
            body.append(following[10:])
        return "\n".join(body).rstrip("\n") + "\n"
    return None


def _all_run_lines(text: str) -> list[str]:
    """Every line of every run: script in a workflow's raw text, whatever its indentation or block style."""
    found: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = re.fullmatch(r"( *)(?:- )?run:(.*)", lines[index])
        index += 1
        if match is None:
            continue
        found.append(match[2])
        depth = len(match[1])
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= depth:
                break
            found.append(line)
            index += 1
    return found


class EveryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        entries = sorted(WORKFLOWS.iterdir())
        # One spelling of the extension, so no workflow escapes the glob below.
        self.assertEqual([path.name for path in entries if path.suffix != ".yml"], [])
        self.files = entries
        self.assertGreaterEqual(len(self.files), 6, [path.name for path in self.files])

    def test_every_action_and_reusable_workflow_is_pinned_by_commit_with_its_version(self) -> None:
        for path in self.files:
            for line in _text(path).splitlines():
                if "uses:" not in line or line.lstrip().startswith("#"):
                    continue
                with self.subTest(workflow=path.name, line=line.strip()):
                    self.assertRegex(line, r"^ +(?:- )?uses: [A-Za-z0-9_./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    def test_write_scopes_are_exactly_the_listed_ones(self) -> None:
        found: dict[tuple[str, str], set[str]] = {}
        for path in self.files:
            text = _text(path)
            blocks = {"": _mapping(_top(text), "permissions", 0)}
            for job, block in _jobs(text).items():
                blocks[job] = _mapping(block, "permissions", 4)
            for job, permissions in blocks.items():
                writes = {scope for scope, access in (permissions or {}).items() if access != "read"}
                if writes:
                    found[(path.name, job)] = writes
        self.assertEqual(found, WRITE_SCOPES)

    def test_no_permission_is_written_in_a_form_the_inventory_cannot_read(self) -> None:
        # The inventory above reads block mappings at two indentations. Any
        # other spelling of a grant (write-all, a flow mapping, a quoted key,
        # a job at another depth) must fail here rather than go uncounted.
        listed = sum(len(scopes) for scopes in WRITE_SCOPES.values())
        counted = 0
        for path in self.files:
            code = _code(_text(path))
            with self.subTest(workflow=path.name):
                self.assertNotRegex(code, r"(?i)(?:write|read)-all")
                for indent, rest in re.findall(r"(?m)^( *)permissions:(.*)$", code):
                    self.assertIn(len(indent), (0, 4))
                    self.assertIn(rest, ("", " {}"))
                self.assertEqual(len(re.findall(r"permissions", code)), len(re.findall(r"(?m)^ *permissions:", code)))
                for indent in re.findall(r"(?m)^( *)runs-on:", code):
                    self.assertEqual(len(indent), 4, "a job at another depth than `jobs:` plus two spaces")
            counted += len(re.findall(r"\bwrite\b", "\n".join(
                line for line in code.splitlines() if re.fullmatch(r" +[A-Za-z-]+: write", line)
            )))
            self.assertEqual(
                len(re.findall(r"(?m)^ +[\"']?[A-Za-z-]+[\"']?: *[\"']?write[\"']?\s*$", code)),
                len(re.findall(r"(?m)^ +[A-Za-z-]+: write$", code)),
                f"{path.name}: a write scope in a spelling the inventory does not read",
            )
        self.assertEqual(counted, listed)

    def test_every_workflow_states_its_default_permissions(self) -> None:
        # Without a top-level block a job gets the repository's default token.
        for path in self.files:
            with self.subTest(workflow=path.name):
                self.assertIsNotNone(_mapping(_top(_text(path)), "permissions", 0), "no top-level permissions block")

    def test_no_workflow_runs_on_an_event_that_hands_secrets_to_untrusted_code(self) -> None:
        for path in self.files:
            with self.subTest(workflow=path.name):
                code = _code(_text(path))
                self.assertNotIn("pull_request_target", code)
                self.assertNotIn("workflow_run", code)

    def test_no_script_has_an_expression_pasted_into_it(self) -> None:
        # ${{ }} is expanded before the shell parses the script, so a tag,
        # title or branch name there is code, in a shell comment too. Values
        # reach scripts through env. The raw text is read, comments included.
        for path in self.files:
            text = _text(path)
            for line in _all_run_lines(text):
                with self.subTest(workflow=path.name, line=line.strip()[:60]):
                    self.assertNotIn("${{", line)
            with self.subTest(workflow=path.name):
                # Every run: key is one the reader above understands, and no
                # step picks its own shell.
                for rest in re.findall(r"(?m)^ *(?:- )?run:(.*)$", _code(text)):
                    self.assertRegex(rest, r"^ (?:[|>][+-]?$|[^|>\s])")
                self.assertNotRegex(_code(text), r"(?m)^ *(?:- )?shell:")


class ReleasePleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _text(RELEASE_PLEASE)
        self.jobs = _jobs(self.text)

    def test_it_runs_only_on_pushes_to_main_one_run_at_a_time(self) -> None:
        head = _top(self.text)
        self.assertRegex(head, r"(?m)^on:\n  push:\n    branches:\n      - main\n(?!  \S)")
        self.assertEqual(_mapping(head, "concurrency", 0), {"group": "release-please", "cancel-in-progress": "false"})

    def test_the_workflow_has_no_token_of_its_own_and_one_job(self) -> None:
        self.assertEqual(_mapping(_top(self.text), "permissions", 0), {})
        self.assertEqual(set(self.jobs), {"release-please"})
        block = self.jobs["release-please"]
        self.assertEqual(_mapping(block, "permissions", 4), {"contents": "write", "pull-requests": "write"})
        self.assertRegex(block, rf"(?m)^    runs-on: {re.escape(RUNNER)}$")

    def test_it_only_keeps_the_release_pr_and_never_tags(self) -> None:
        # The tag ruleset is admin-only: the owner makes each release by hand.
        steps = _steps(self.jobs["release-please"])
        # The job that can write to the repository runs one action and no script.
        self.assertEqual(len(steps), 1)
        self.assertTrue((_uses(steps[0]) or "").startswith("googleapis/release-please-action@"))
        # Exactly these inputs: no token of another identity, and no option
        # that would make the action tag or release.
        self.assertEqual(_mapping(steps[0], "with", 8), {
            "config-file": "release-please-config.json",
            "manifest-file": ".release-please-manifest.json",
            "skip-github-release": "true",
        })
        self.assertIsNone(_mapping(steps[0], "env", 8))
        self.assertEqual(_all_run_lines(self.text), [])

    def test_the_config_is_one_simple_package_whose_first_version_is_0_1_0(self) -> None:
        config = json.loads(_text(CONFIG))
        self.assertEqual(config.get("release-type"), "simple")
        self.assertIs(config.get("bump-minor-pre-major"), True)
        self.assertIs(config.get("include-component-in-tag"), False)
        # With no earlier release, release-please proposes 1.0.0 unless told
        # otherwise; initial-version is read only then, so it can stay.
        self.assertEqual(config.get("packages"), {".": {"initial-version": "0.1.0"}})
        # release-as is sticky: left in the config, every later release PR
        # would propose the same version again.
        self.assertNotIn("release-as", json.dumps(config))
        self.assertNotIn("skip-github-release", config)

    def test_the_manifest_records_one_version_for_the_root_package(self) -> None:
        manifest = json.loads(_text(MANIFEST))
        self.assertEqual(set(manifest), {"."})
        self.assertRegex(manifest["."], r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _text(PUBLISH)
        self.code = _code(self.text)
        self.jobs = _jobs(self.text)

    def test_it_runs_on_a_published_release_or_by_hand_and_on_nothing_else(self) -> None:
        head = _top(self.text)
        self.assertRegex(head, r"(?m)^on:\n  release:\n    types:\n      - published\n  workflow_dispatch:\n(?!  \S)")
        # Nothing cancels a run between its push and its tag.
        self.assertEqual(
            _mapping(head, "concurrency", 0), {"group": "publish-${{ github.ref }}", "cancel-in-progress": "false"}
        )

    def test_jobs_their_permissions_and_their_runner(self) -> None:
        self.assertEqual(_mapping(_top(self.text), "permissions", 0), {})
        self.assertEqual(list(self.jobs), list(PUBLISH_PERMISSIONS))
        for job, expected in PUBLISH_PERMISSIONS.items():
            with self.subTest(job=job):
                self.assertEqual(_mapping(self.jobs[job], "permissions", 4), expected)
                if job != "scan":
                    self.assertRegex(self.jobs[job], rf"(?m)^    runs-on: {re.escape(RUNNER)}$")

    def test_the_order_is_check_build_scan_attest_tag_relabel(self) -> None:
        self.assertEqual(_needs(self.jobs["check"]), set())
        self.assertEqual(_needs(self.jobs["build"]), {"check"})
        self.assertEqual(_needs(self.jobs["scan"]), {"build"})
        # Only a scanned image is attested, and only an attested one tagged.
        self.assertEqual(_needs(self.jobs["attest"]), {"build", "scan"})
        self.assertEqual(_needs(self.jobs["tag"]), {"check", "build", "scan", "attest"})
        self.assertEqual(_needs(self.jobs["relabel"]), {"check", "tag"})

    def test_only_a_release_tags_touches_the_release_or_relabels(self) -> None:
        for job in ("tag", "relabel"):
            with self.subTest(job=job):
                self.assertEqual(_condition(self.jobs[job]), RELEASE_ONLY)
        # The jobs a rehearsal runs carry no condition of their own: a
        # skipped scan would otherwise let `needs` pass.
        for job in ("check", "build", "scan", "attest"):
            with self.subTest(job=job):
                self.assertIsNone(_condition(self.jobs[job]))
        # No job or step overrides the default (success of what came before).
        for word in ("always()", "cancelled()", "failure()", "continue-on-error"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code)
        self.assertEqual(len(re.findall(r"(?m)^ *if:", self.code)), 2, "a step-level condition")

    def test_check_refuses_before_anything_is_built(self) -> None:
        block = self.jobs["check"]
        steps = _steps(block)
        self.assertEqual(len(steps), 3)
        self.assertEqual(_mapping(steps[0], "with", 8), {"fetch-depth": "0", "persist-credentials": "false"})
        self.assertEqual(
            _script(steps[1]),
            'if ! git merge-base --is-ancestor "$GITHUB_SHA" origin/main; then\n'
            '  echo "::error::This commit is not on main: a release is made from the merged release PR\'s commit"\n'
            "  exit 1\n"
            "fi\n",
        )
        self.assertEqual(_mapping(steps[2], "env", 8), {
            "EVENT_NAME": "${{ github.event_name }}",
            "REF_TYPE": "${{ github.ref_type }}",
            "REF_NAME": "${{ github.ref_name }}",
            "PRERELEASE": "${{ github.event.release.prerelease }}",
        })
        self.assertEqual(
            _script(steps[2]),
            'COMMIT="$GITHUB_SHA"\n'
            'MANIFEST_COMMIT="$(git log -1 --format=%H "$GITHUB_SHA" -- .release-please-manifest.json)"\n'
            "export COMMIT MANIFEST_COMMIT\n"
            'python -I scripts/check_release_tag.py .release-please-manifest.json >> "$GITHUB_OUTPUT"\n',
        )
        self.assertEqual(_mapping(block, "outputs", 4), {
            "version": "${{ steps.plan.outputs.version }}",
            "release": "${{ steps.plan.outputs.release }}",
        })
        self.assertRegex(steps[2], r"(?m)^        id: plan$")

    def test_only_check_and_build_see_the_source_at_the_events_commit(self) -> None:
        # The jobs that sign, tag and relabel run no code from the checkout,
        # and build builds the commit that check checked: no ref, no
        # repository, no credentials left behind.
        for job, block in self.jobs.items():
            checkouts = [step for step in _steps(block) if (_uses(step) or "").startswith("actions/checkout@")]
            with self.subTest(job=job):
                self.assertEqual(len(checkouts), 1 if job in ("check", "build") else 0)
        build = [step for step in _steps(self.jobs["build"]) if (_uses(step) or "").startswith("actions/checkout@")]
        self.assertEqual(_mapping(build[0], "with", 8), {"persist-credentials": "false"})

    def test_images_are_built_fresh_and_pushed_by_digest_only(self) -> None:
        block = self.jobs["build"]
        builds = [step for step in _steps(block) if (_uses(step) or "").startswith("docker/build-push-action@")]
        self.assertEqual(len(builds), 2)
        labels = {
            "org.opencontainers.image.source": "${{ steps.names.outputs.source }}",
            "org.opencontainers.image.version": VERSION,
            "org.opencontainers.image.revision": "${{ github.sha }}",
            "org.opencontainers.image.licenses": "AGPL-3.0-or-later",
        }
        for step, name in zip(builds, ("core", "fetcher"), strict=True):
            with self.subTest(image=name):
                given = _mapping(step, "with", 8)
                assert given is not None
                title = "strange_ringtone" if name == "core" else "strange_ringtone-fetcher"
                expected = {
                    "context": ".",
                    "file": f"{name}/Dockerfile",
                    "platforms": "linux/amd64",
                    # No layer from a cache that another run wrote, and the
                    # base image as the registry has it now, by its digest.
                    "no-cache": "true",
                    "pull": "true",
                    "provenance": "false",
                    "sbom": "generator=${{ steps.names.outputs.generator }}",
                    "labels": "\n".join(
                        f"{key}={value}" for key, value in {"org.opencontainers.image.title": title, **labels}.items()
                    ),
                    # Untagged: a tag appears only after the scan, in the tag job.
                    "outputs": f"type=image,name=${{{{ steps.names.outputs.{name} }}}},"
                               "push-by-digest=true,name-canonical=true,push=true",
                }
                if name == "core":
                    expected["build-args"] = f"VERSION={VERSION}\nSOURCE_URL=${{{{ steps.names.outputs.source }}}}"
                # Exactly these inputs: no tags, no cache, no secret, no
                # other build context, nothing handed to the Dockerfile.
                self.assertEqual(given, expected)
                self.assertIsNone(_mapping(step, "env", 8))
        self.assertEqual(_mapping(block, "outputs", 4), {
            f"{image}-{part}": f"${{{{ steps.pushed.outputs.{image}-{part} }}}}"
            for image in ("core", "fetcher") for part in ("image", "digest")
        })
        self.assertEqual(_mapping(_step(block, "Record what was pushed"), "env", 8), {
            "CORE_IMAGE": "${{ steps.names.outputs.core }}",
            "CORE_DIGEST": "${{ steps.core.outputs.digest }}",
            "FETCHER_IMAGE": "${{ steps.names.outputs.fetcher }}",
            "FETCHER_DIGEST": "${{ steps.fetcher.outputs.digest }}",
        })
        self.assertEqual(_mapping(_step(block, "Refuse a version that is already published"), "env", 8), {
            "RELEASE": "${{ needs.check.outputs.release }}",
            "VERSION": VERSION,
            "CORE_IMAGE": "${{ steps.names.outputs.core }}",
            "FETCHER_IMAGE": "${{ steps.names.outputs.fetcher }}",
        })

    def test_nothing_but_the_tag_job_can_name_an_image(self) -> None:
        # One command in the whole workflow gives an image a tag, and it sits
        # in the tag job, after the scan and the attestations.
        self.assertEqual(self.code.count("imagetools create"), 1)
        self.assertEqual(self.code.count("--tag "), 1)
        self.assertIn('docker buildx imagetools create --tag "$image:$VERSION" "$image@$digest"', self.jobs["tag"])
        for word in ("docker push", "docker tag", "docker image tag", "tags:", "load:", "skopeo", "crane", "oras"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code)
        self.assertNotRegex(self.code, r"(?i)\blatest\b")

    def test_the_sbom_generator_is_pinned_by_digest_where_dependabot_finds_it(self) -> None:
        lines = [line for line in _text(SBOM_PIN).splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"\AFROM docker/buildkit-syft-scanner:[a-z0-9.-]+@sha256:[0-9a-f]{64}\Z")

    def test_the_scan_is_the_organisations_and_gets_exactly_the_pushed_digests(self) -> None:
        block = self.jobs["scan"]
        workflow = rf"{SECURITY_REPOSITORY}/\.github/workflows/trivy-images\.yml"
        match = re.search(rf"(?m)^    uses: {workflow}@([0-9a-f]{{40}}) # v[0-9.]+$", block)
        self.assertIsNotNone(match)
        assert match is not None
        # The same release of strange_security as the gate on every PR.
        gate_pins = set(re.findall(rf"{re.escape(SECURITY_REPOSITORY)}/[^@\s]+@([0-9a-f]{{40}})", _text(SECURITY_GATE)))
        self.assertEqual(gate_pins, {match[1]})
        self.assertNotIn("steps:", block, "the scan job runs the shared workflow and nothing of its own")
        self.assertNotIn("secrets:", block)
        # Its only input: the two digests the build pushed. No exceptions
        # file, no allowlist, no login, no longer timeout, no SARIF opt-out.
        self.assertEqual(_mapping(block, "with", 4), {
            "images": "${{ needs.build.outputs.core-image }}@${{ needs.build.outputs.core-digest }}\n"
                      "${{ needs.build.outputs.fetcher-image }}@${{ needs.build.outputs.fetcher-digest }}",
        })

    def test_this_repository_holds_no_file_that_excuses_a_scan_finding(self) -> None:
        # trivy-images.yml reads these two paths from the caller's checkout
        # by default. An entry is bound to one digest and expires, but the
        # release's scan stays unexcused until someone decides otherwise here.
        for name in ("image-exceptions.json", "image-secret-allowlist.json"):
            with self.subTest(file=name):
                self.assertFalse((helpers.ROOT / ".github" / name).exists())
        for name in (".trivyignore", ".trivyignore.yaml", "trivy.yaml"):
            with self.subTest(file=name):
                self.assertFalse((helpers.ROOT / name).exists())

    def test_no_security_scan_is_run_by_this_repository_itself(self) -> None:
        for word in ("trivy-action", "aquasecurity", "anchore", "grype", "semgrep scan", "gitleaks detect"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code)

    def test_attest_signs_exactly_the_scanned_digests_with_githubs_own_actions(self) -> None:
        block = self.jobs["attest"]
        steps = _steps(block)
        self.assertEqual(len(steps), 6)
        self.assertEqual(_script(steps[0]), LOGIN + "\n")
        self.assertEqual(_mapping(steps[1], "env", 8), {
            "CORE": "${{ needs.build.outputs.core-image }}@${{ needs.build.outputs.core-digest }}",
            "FETCHER": "${{ needs.build.outputs.fetcher-image }}@${{ needs.build.outputs.fetcher-digest }}",
        })
        expected = []
        for image in ("core", "fetcher"):
            subject = {
                "subject-name": f"${{{{ needs.build.outputs.{image}-image }}}}",
                "subject-digest": f"${{{{ needs.build.outputs.{image}-digest }}}}",
            }
            storage = {"push-to-registry": "true", "create-storage-record": "false"}
            expected.append(("actions/attest-build-provenance", {**subject, **storage}))
            expected.append(("actions/attest", {
                **subject, "sbom-path": f"${{{{ runner.temp }}}}/{image}.spdx.json", **storage,
            }))
        found = [((_uses(step) or "").split("@")[0], _mapping(step, "with", 8)) for step in steps[2:]]
        self.assertEqual(found, expected)

    def test_tag_and_notes_are_given_the_checked_version_and_the_scanned_digests(self) -> None:
        block = self.jobs["tag"]
        steps = _steps(block)
        self.assertEqual(len(steps), 3)
        self.assertEqual(_script(steps[0]), LOGIN + "\n")
        self.assertEqual(_mapping(steps[1], "env", 8), {"VERSION": VERSION, **BUILT})
        self.assertEqual(_mapping(steps[2], "env", 8), {"GH_TOKEN": "${{ github.token }}", "VERSION": VERSION, **BUILT})
        notes = _script(steps[2]) or ""
        # The release is named by the checked version, never by the event.
        release = '"v$VERSION" --repo "$GITHUB_REPOSITORY"'
        self.assertIn(f'gh release view {release} --json body --jq .body > "$notes"', notes)
        self.assertIn(f'gh release edit {release} --notes-file "$notes"', notes)
        self.assertEqual(notes.count("gh release"), 2)

    def test_relabel_is_given_the_token_and_nothing_else(self) -> None:
        block = self.jobs["relabel"]
        steps = _steps(block)
        self.assertEqual(len(steps), 1)
        self.assertNotIn("uses:", block, "the job that can write to pull requests runs no action")
        self.assertEqual(_mapping(steps[0], "env", 8), {"GH_TOKEN": "${{ github.token }}"})
        script = _script(steps[0]) or ""
        self.assertIn('gh api "repos/$GITHUB_REPOSITORY/commits/$GITHUB_SHA/pulls"', script)
        self.assertEqual(script.count("--method "), 2)

    def test_the_registry_login_reads_the_token_from_stdin(self) -> None:
        logins = [
            (job, step) for job, block in self.jobs.items() for step in _steps(block)
            if "docker login" in step
        ]
        self.assertEqual([job for job, _ in logins], ["build", "attest", "tag"])
        for job, step in logins:
            with self.subTest(job=job):
                self.assertEqual(_script(step), LOGIN + "\n")
                self.assertEqual(_mapping(step, "env", 8), {"REGISTRY_TOKEN": "${{ github.token }}"})
        # The token appears nowhere else but the two gh steps' environment.
        self.assertEqual(self.code.count("github.token"), 5)
        self.assertNotIn("secrets.", self.code)


@unittest.skipUnless(POSIX_SHELL, "runs the workflow's shell scripts: needs bash on POSIX")
class PublishScriptsTest(unittest.TestCase):
    """The scripts of publish.yml, run as the runner runs them (`bash -e`), with docker, gh and git faked."""

    FAKE_DOCKER = """#!/bin/sh
# inspect prints what $FAKE_STATE holds for the reference, as the real one
# prints a digest or a document; create writes the source's digest there.
printf '%s\\n' "$*" >> "$FAKE_LOG"
key() { printf '%s' "$1" | tr '/:@' '___'; }
case "$1 $2 $3" in
  "buildx imagetools inspect")
    if [ -f "$FAKE_STATE/inspect-error" ]; then cat "$FAKE_STATE/inspect-error" >&2; exit 1; fi
    if [ -f "$FAKE_STATE/$(key "$4")" ]; then cat "$FAKE_STATE/$(key "$4")"; exit 0; fi
    echo "ERROR: $4: not found" >&2; exit 1 ;;
  "buildx imagetools create")
    [ "$4" = "--tag" ] || exit 96
    if [ -f "$FAKE_STATE/create-writes" ]; then cp "$FAKE_STATE/create-writes" "$FAKE_STATE/$(key "$5")"
    else printf '%s\\n' "${6##*@}" > "$FAKE_STATE/$(key "$5")"; fi ;;
  *) echo "unexpected: docker $*" >&2; exit 97 ;;
esac
"""
    FAKE_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "api repos/owner/tone/commits/"*) cat "$FAKE_STATE/pulls.tsv" ;;
  "api --method")
    if [ "$3" = "POST" ] && [ -f "$FAKE_STATE/fail-post" ]; then echo "HTTP 422" >&2; exit 1; fi ;;
  "release view") cat "$FAKE_STATE/notes" ;;
  "release edit") cp "$7" "$FAKE_STATE/notes" ;;
  *) echo "unexpected: gh $*" >&2; exit 97 ;;
esac
"""
    FAKE_GIT = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
[ -f "$FAKE_STATE/ancestor" ]
"""

    def setUp(self) -> None:
        self.jobs = _jobs(_text(PUBLISH), raw=True)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.work = self.root / "work"
        for directory in (self.state, self.bin, self.work, self.root / "runner"):
            directory.mkdir()
        self.log = self.root / "log"
        self.output = self.root / "output"
        for name, body in (("docker", self.FAKE_DOCKER), ("gh", self.FAKE_GH), ("git", self.FAKE_GIT)):
            path = self.bin / name
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
            path.chmod(0o755)

    def run_step(self, job: str, name: str, **environ: str) -> subprocess.CompletedProcess[str]:
        script = _script(_step(self.jobs[job], name))
        assert script is not None
        path = self.root / "step.sh"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        self.log.write_text("", encoding="utf-8")
        self.output.write_text("", encoding="utf-8")
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_LOG": str(self.log), "FAKE_STATE": str(self.state),
            "RUNNER_TEMP": str(self.root / "runner"), "GITHUB_OUTPUT": str(self.output),
            "GITHUB_REPOSITORY": "owner/tone", "GITHUB_SHA": SHA, "GITHUB_SERVER_URL": "https://github.example",
            **environ,
        }
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", str(path)], cwd=self.work, env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )

    def calls(self, start: str = "") -> list[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.startswith(start)]

    def put(self, name: str, content: str) -> None:
        with open(self.state / name, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    @staticmethod
    def key(reference: str) -> str:
        return re.sub(r"[/:@]", "_", reference)

    # --- check: the commit must be on main ---------------------------------

    def test_a_commit_that_is_not_on_main_stops_the_run(self) -> None:
        result = self.run_step("check", "Check the commit is on main")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("::error::This commit is not on main", result.stdout)
        self.assertEqual(self.calls(), [f"merge-base --is-ancestor {SHA} origin/main"])
        self.put("ancestor", "")
        self.assertEqual(self.run_step("check", "Check the commit is on main").returncode, 0)

    # --- build: names, the generator pin, the already-published refusal ----

    def names(self, pin: str, repository: str = "Owner/Tone") -> subprocess.CompletedProcess[str]:
        (self.work / ".github" / "sbom").mkdir(parents=True, exist_ok=True)
        with open(self.work / ".github" / "sbom" / "Dockerfile", "w", encoding="utf-8", newline="\n") as handle:
            handle.write(pin)
        return self.run_step("build", "Name the images and the SBOM generator", GITHUB_REPOSITORY=repository)

    def test_names_come_from_the_repository_and_the_real_pin_file(self) -> None:
        result = self.names(_text(SBOM_PIN))
        self.assertEqual(result.returncode, 0, result.stderr)
        generator = [line for line in _text(SBOM_PIN).splitlines() if line.startswith("FROM ")][0][5:]
        self.assertEqual(self.output.read_text(encoding="utf-8").splitlines(), [
            "core=ghcr.io/owner/tone", "fetcher=ghcr.io/owner/tone-fetcher", f"generator={generator}",
            "source=https://github.example/Owner/Tone",
        ])

    def test_a_generator_that_is_not_the_scanner_pinned_by_digest_stops_the_build(self) -> None:
        pinned = "docker/buildkit-syft-scanner:stable-1@sha256:" + "0" * 64
        for pin in (
            "FROM docker/buildkit-syft-scanner:stable-1\n",
            "FROM docker/buildkit-syft-scanner@sha256:" + "0" * 64 + "\n",
            "FROM example/other-scanner:1@sha256:" + "0" * 64 + "\n",
            "FROM docker/buildkit-syft-scanner:stable-1@sha256:" + "0" * 63 + "\n",
            f"FROM {pinned}\nFROM {pinned}\n",
            f"FROM {pinned} extra\n",
            f"FROM registry.example/{pinned}\n",
            "# nothing here\n",
        ):
            with self.subTest(pin=pin):
                result = self.names(pin)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "")

    def test_a_repository_that_cannot_be_an_image_name_stops_the_build(self) -> None:
        pin = _text(SBOM_PIN)
        for repository in ("owner", "owner/tone/extra", "owner/to ne", "owner/tone;id", "-owner/tone", ""):
            with self.subTest(repository=repository):
                self.assertEqual(self.names(pin, repository).returncode, 1)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "")

    def published(self, release: str = "true") -> subprocess.CompletedProcess[str]:
        return self.run_step(
            "build", "Refuse a version that is already published",
            RELEASE=release, VERSION="0.1.0", CORE_IMAGE=CORE, FETCHER_IMAGE=FETCHER,
        )

    def test_a_version_that_already_exists_is_not_built_again(self) -> None:
        self.assertEqual(self.published().returncode, 0)
        self.assertEqual(len(self.calls()), 2)
        for image in (CORE, FETCHER):
            with self.subTest(image=image):
                for path in self.state.iterdir():
                    path.unlink()
                self.put(self.key(f"{image}:0.1.0"), DIGEST_A + "\n")
                result = self.published()
                self.assertEqual(result.returncode, 1)
                self.assertIn("is already published", result.stdout)

    def test_a_registry_that_cannot_say_stops_the_build_and_a_rehearsal_does_not_ask(self) -> None:
        self.put("inspect-error", "ERROR: unexpected status from HEAD request: 403 Forbidden\n")
        result = self.published()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not tell whether", result.stdout)
        for release in ("false", "", "TRUE", "1"):
            with self.subTest(release=release):
                self.assertEqual(self.published(release).returncode, 0)
                self.assertEqual(self.calls(), [], "anything but exactly true is a rehearsal")

    def test_a_build_without_two_digests_hands_nothing_on(self) -> None:
        def pushed(core: str, fetcher: str) -> subprocess.CompletedProcess[str]:
            return self.run_step(
                "build", "Record what was pushed",
                CORE_IMAGE=CORE, CORE_DIGEST=core, FETCHER_IMAGE=FETCHER, FETCHER_DIGEST=fetcher,
            )

        self.assertEqual(pushed(DIGEST_A, DIGEST_B).returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf-8").splitlines(), [
            f"core-image={CORE}", f"core-digest={DIGEST_A}", f"fetcher-image={FETCHER}", f"fetcher-digest={DIGEST_B}",
        ])
        for core, fetcher in (
            (DIGEST_A, ""), ("", DIGEST_B), (DIGEST_A[:-1], DIGEST_B), (DIGEST_A, "sha256:" + "G" * 64),
            (DIGEST_A, DIGEST_B + "\nrelease=true"), ("latest", DIGEST_B),
        ):
            with self.subTest(core=core, fetcher=fetcher):
                self.assertEqual(pushed(core, fetcher).returncode, 1)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "")

    # --- attest: the SBOM read back from the registry ----------------------

    def sboms(self) -> subprocess.CompletedProcess[str]:
        return self.run_step(
            "attest", "Read each image's SBOM from the registry",
            CORE=f"{CORE}@{DIGEST_A}", FETCHER=f"{FETCHER}@{DIGEST_B}",
        )

    def test_each_sbom_is_read_by_digest_into_its_own_file_and_must_be_spdx_with_packages(self) -> None:
        document = {"spdxVersion": "SPDX-2.3", "packages": [{"name": "x"}]}
        self.put(self.key(f"{CORE}@{DIGEST_A}"), json.dumps({**document, "name": "core"}))
        self.put(self.key(f"{FETCHER}@{DIGEST_B}"), json.dumps({**document, "name": "fetcher"}))
        result = self.sboms()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            f"buildx imagetools inspect {CORE}@{DIGEST_A} --format {{{{ json .SBOM.SPDX }}}}",
            f"buildx imagetools inspect {FETCHER}@{DIGEST_B} --format {{{{ json .SBOM.SPDX }}}}",
        ])
        for name in ("core", "fetcher"):
            with open(self.root / "runner" / f"{name}.spdx.json", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["name"], name)
        for bad in ("null", "{}", json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}),
                    json.dumps({"bomFormat": "CycloneDX", "packages": [1]}), "[]", "not json"):
            with self.subTest(fetcher_sbom=bad):
                self.put(self.key(f"{FETCHER}@{DIGEST_B}"), bad)
                self.assertNotEqual(self.sboms().returncode, 0)
        (self.state / self.key(f"{FETCHER}@{DIGEST_B}")).unlink()
        self.assertNotEqual(self.sboms().returncode, 0, "an image without an SBOM")

    # --- tag: only the scanned digest, only once ----------------------------

    def tag(self) -> subprocess.CompletedProcess[str]:
        return self.run_step(
            "tag", "Tag the scanned digests with the version", VERSION="0.1.0",
            CORE_IMAGE=CORE, CORE_DIGEST=DIGEST_A, FETCHER_IMAGE=FETCHER, FETCHER_DIGEST=DIGEST_B,
        )

    def test_a_new_version_is_tagged_on_exactly_the_scanned_digests(self) -> None:
        result = self.tag()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls("buildx imagetools create"), [
            f"buildx imagetools create --tag {CORE}:0.1.0 {CORE}@{DIGEST_A}",
            f"buildx imagetools create --tag {FETCHER}:0.1.0 {FETCHER}@{DIGEST_B}",
        ])

    def test_a_tag_that_already_names_the_digest_is_left_alone(self) -> None:
        self.put(self.key(f"{CORE}:0.1.0"), DIGEST_A + "\n")
        self.put(self.key(f"{FETCHER}:0.1.0"), DIGEST_B + "\n")
        self.assertEqual(self.tag().returncode, 0)
        self.assertEqual(self.calls("buildx imagetools create"), [])

    def test_a_version_tag_is_never_moved(self) -> None:
        self.put(self.key(f"{CORE}:0.1.0"), DIGEST_C + "\n")
        result = self.tag()
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to move a version tag", result.stdout)
        self.assertEqual(self.calls("buildx imagetools create"), [])
        self.assertEqual((self.state / self.key(f"{CORE}:0.1.0")).read_text(encoding="utf-8"), DIGEST_C + "\n")
        # The second image's tag is refused too, the first already in place.
        (self.state / self.key(f"{CORE}:0.1.0")).unlink()
        self.put(self.key(f"{FETCHER}:0.1.0"), DIGEST_C + "\n")
        self.assertEqual(self.tag().returncode, 1)
        self.assertEqual(
            self.calls("buildx imagetools create"), [f"buildx imagetools create --tag {CORE}:0.1.0 {CORE}@{DIGEST_A}"]
        )

    def test_nothing_is_tagged_when_the_registry_cannot_say_whether_the_tag_exists(self) -> None:
        for error in ("ERROR: unexpected status from HEAD request: 403 Forbidden\n", "dial tcp: i/o timeout\n", ""):
            with self.subTest(error=error):
                self.put("inspect-error", error)
                result = self.tag()
                self.assertEqual(result.returncode, 1)
                self.assertIn("Could not tell whether", result.stdout)
                self.assertEqual(self.calls("buildx imagetools create"), [])

    def test_a_tag_that_does_not_name_the_scanned_digest_afterwards_fails_the_job(self) -> None:
        self.put("create-writes", DIGEST_C + "\n")
        result = self.tag()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the scanned digest", result.stdout)

    # --- tag: the release notes ---------------------------------------------

    def notes(self) -> subprocess.CompletedProcess[str]:
        return self.run_step(
            "tag", "Add the digests to the release notes", VERSION="0.1.0",
            CORE_IMAGE=CORE, CORE_DIGEST=DIGEST_A, FETCHER_IMAGE=FETCHER, FETCHER_DIGEST=DIGEST_B,
        )

    def test_the_digests_are_appended_to_the_notes_once(self) -> None:
        self.put("notes", "What changed.\n")
        self.assertEqual(self.notes().returncode, 0)
        written = (self.state / "notes").read_text(encoding="utf-8")
        self.assertTrue(written.startswith("What changed.\n"))
        self.assertIn(f"- Core: `{CORE}@{DIGEST_A}`\n", written)
        self.assertIn(f"- Fetcher: `{FETCHER}@{DIGEST_B}`\n", written)
        self.assertEqual(self.calls("release")[0], "release view v0.1.0 --repo owner/tone --json body --jq .body")
        self.assertTrue(self.calls("release")[1].startswith("release edit v0.1.0 --repo owner/tone --notes-file "))
        self.assertEqual(self.notes().returncode, 0)
        self.assertEqual((self.state / "notes").read_text(encoding="utf-8"), written)
        self.assertEqual(len(self.calls("release edit")), 0, "a second run edits nothing")

    # --- relabel -------------------------------------------------------------

    def relabel(self, *rows: tuple[str, str, str, str, str]) -> subprocess.CompletedProcess[str]:
        self.put("pulls.tsv", "".join("\t".join(row) + "\n" for row in rows))
        return self.run_step("relabel", "Swap the label on this commit's release pull request", GH_TOKEN="x")

    def test_the_release_pr_of_this_commit_gets_the_new_label_before_it_loses_the_old(self) -> None:
        result = self.relabel(("41", SHA, "release-please--branches--main", "true", "autorelease: pending"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls("api --method"), [
            "api --method POST repos/owner/tone/issues/41/labels -f labels[]=autorelease: tagged",
            "api --method DELETE repos/owner/tone/issues/41/labels/autorelease:%20pending",
        ])

    def test_the_old_label_stays_when_the_new_one_cannot_be_added(self) -> None:
        self.put("fail-post", "")
        result = self.relabel(("41", SHA, "release-please--branches--main", "true", "autorelease: pending"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls("api --method")), 1, "no DELETE after a failed POST")

    def test_a_pull_request_already_swapped_is_left_alone(self) -> None:
        for labels in ("autorelease: tagged", "", "autorelease: tagged|other", "not autorelease: pending"):
            with self.subTest(labels=labels):
                result = self.relabel(("41", SHA, "release-please--branches--main", "true", labels))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.calls("api --method"), [])

    def test_no_other_pull_request_is_touched(self) -> None:
        other = "f" * 40
        for row in (
            ("41", other, "release-please--branches--main", "true", "autorelease: pending"),
            ("41", SHA, "release-please--branches--main", "false", "autorelease: pending"),
            ("41", SHA, "feat/something", "true", "autorelease: pending"),
            ("x1", SHA, "release-please--branches--main", "true", "autorelease: pending"),
        ):
            with self.subTest(row=row):
                result = self.relabel(row)
                self.assertEqual(result.returncode, 1)
                self.assertIn("relabel the release pull request by hand", result.stdout)
                self.assertEqual(self.calls("api --method"), [])
        self.assertEqual(self.relabel().returncode, 1, "no pull request at all")


class DependabotTest(unittest.TestCase):
    """A bump that changes an image must be a releasable commit: release-please ignores chore (docs/PLAN.md 9.1)."""

    def entries(self) -> dict[tuple[str, str], str]:
        found: dict[tuple[str, str], str] = {}
        for entry in _code(_text(DEPENDABOT)).split("  - package-ecosystem: ")[1:]:
            ecosystem = entry.splitlines()[0].strip()
            directory = re.search(r"(?m)^    directory: (\S+)$", entry)
            prefix = re.search(r"(?m)^      prefix: (\S+)$", entry)
            self.assertIsNotNone(directory, entry)
            self.assertIsNotNone(prefix, entry)
            assert directory is not None and prefix is not None
            self.assertRegex(entry, r"(?m)^      include: scope$")
            self.assertRegex(entry, r"(?m)^      default-days: 7$")
            found[(ecosystem, directory[1])] = prefix[1]
        return found

    def test_image_bumps_are_fixes_and_the_rest_are_chores(self) -> None:
        self.assertEqual(self.entries(), {
            ("github-actions", "/"): "chore",
            ("pip", "/.github/lint"): "chore",
            ("docker", "/.github/sbom"): "chore",
            ("docker", "/core"): "fix",
            ("pip", "/fetcher"): "fix",
            ("docker", "/fetcher"): "fix",
        })


class ComposeMemoryTest(unittest.TestCase):
    """Each service's memory limit is its whole bound: no swap on top of it (README, Deployment contract)."""

    def test_every_service_sets_memswap_limit_to_its_mem_limit(self) -> None:
        code = _code(_text(COMPOSE))
        services = code.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        blocks = re.split(r"(?m)^  (?=[a-z][a-z-]*:$)", services)
        seen: dict[str, tuple[str, str]] = {}
        for block in blocks:
            if not block.strip():
                continue
            name = block.splitlines()[0].rstrip(":")
            memory = re.findall(r"(?m)^    mem_limit: (\S+)$", block)
            swap = re.findall(r"(?m)^    memswap_limit: (\S+)$", block)
            self.assertEqual(len(memory), 1, f"{name}: mem_limit {memory}")
            self.assertEqual(len(swap), 1, f"{name}: memswap_limit {swap}")
            seen[name] = (memory[0], swap[0])
        self.assertEqual(set(seen), {"core", "fetcher", "fetcher-runner", "fetcher-forwarder"})
        for name, (memory, swap) in seen.items():
            with self.subTest(service=name):
                self.assertEqual(swap, memory, "memory plus swap must equal memory: no swap for this container")


if __name__ == "__main__":
    unittest.main()
