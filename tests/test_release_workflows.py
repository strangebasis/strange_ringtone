"""The release flow as the workflow files state it (docs/PLAN.md 9.1 and 9.3).

Nothing here runs a workflow: these tests read the YAML as text, because the
suite is stdlib-only and the properties checked are ones a reviewer would
otherwise have to re-read on every change: which jobs hold a write scope, that
no image is tagged before its scan passed, that a hand-started run can never
release, and that nothing an event carries is pasted into a shell script.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import helpers

WORKFLOWS = helpers.ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish.yml"
RELEASE_PLEASE = WORKFLOWS / "release-please.yml"
DEPENDABOT = helpers.ROOT / ".github" / "dependabot.yml"
SBOM_PIN = helpers.ROOT / ".github" / "sbom" / "Dockerfile"
CONFIG = helpers.ROOT / "release-please-config.json"
MANIFEST = helpers.ROOT / ".release-please-manifest.json"
COMPOSE = helpers.ROOT / "compose.example.yaml"

SECURITY_REPOSITORY = "strangebasisdevs/strange_security"
RELEASE_ONLY = "needs.check.outputs.release == 'true'"
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


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The text without comment lines and trailing comments, so a comment can't satisfy or fail a check."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(lines)


def _jobs(text: str) -> dict[str, str]:
    """Job id -> the job's lines, comments removed."""
    code = _code(text)
    if "\njobs:\n" not in code:
        raise AssertionError("no jobs: key")
    body = code.split("\njobs:\n", 1)[1]
    jobs: dict[str, str] = {}
    current: str | None = None
    for line in body.splitlines():
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
    """The scalar entries under `key:` at the given indentation; {} for `key: {}`; None when the key is absent."""
    pad = " " * indent
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line == f"{pad}{key}: {{}}":
            return {}
        if line == f"{pad}{key}:":
            entries: dict[str, str] = {}
            for entry in lines[index + 1:]:
                match = re.fullmatch(rf"{pad}  ([A-Za-z0-9_-]+): (\S.*)", entry)
                if match is None:
                    break
                entries[match[1]] = match[2]
            return entries
    return None


def _top_permissions(text: str) -> dict[str, str] | None:
    return _mapping(_code(text).split("\njobs:\n", 1)[0], "permissions", 0)


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


def _run_scripts(text: str) -> list[str]:
    """Every run: script in a workflow, block or single line."""
    scripts: list[str] = []
    lines = _code(text).splitlines()
    index = 0
    while index < len(lines):
        match = re.fullmatch(r"( *)(?:- )?run: (.*)", lines[index])
        index += 1
        if match is None:
            continue
        if match[2] not in ("|", ">", "|-", ">-"):
            scripts.append(match[2])
            continue
        depth = len(match[1])
        body: list[str] = []
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= depth:
                break
            body.append(line)
            index += 1
        scripts.append("\n".join(body))
    return scripts


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


class EveryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertGreaterEqual(len(self.files), 6, [path.name for path in self.files])

    def test_every_action_and_reusable_workflow_is_pinned_by_commit_with_its_version(self) -> None:
        for path in self.files:
            for line in _text(path).splitlines():
                if not re.match(r"\s*(?:- )?uses:", line):
                    continue
                with self.subTest(workflow=path.name, line=line.strip()):
                    self.assertRegex(line, r"uses: [A-Za-z0-9_./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    def test_write_scopes_are_exactly_the_listed_ones(self) -> None:
        found: dict[tuple[str, str], set[str]] = {}
        for path in self.files:
            text = _text(path)
            blocks = {"": _top_permissions(text)}
            for job, block in _jobs(text).items():
                blocks[job] = _mapping(block, "permissions", 4)
            for job, permissions in blocks.items():
                writes = {scope for scope, access in (permissions or {}).items() if access != "read"}
                if writes:
                    found[(path.name, job)] = writes
        self.assertEqual(found, WRITE_SCOPES)

    def test_every_workflow_states_its_default_permissions(self) -> None:
        # Without a top-level block a job gets the repository's default token.
        for path in self.files:
            with self.subTest(workflow=path.name):
                self.assertIsNotNone(_top_permissions(_text(path)), "no top-level permissions block")

    def test_no_workflow_runs_on_an_event_that_hands_secrets_to_untrusted_code(self) -> None:
        for path in self.files:
            with self.subTest(workflow=path.name):
                code = _code(_text(path))
                self.assertNotIn("pull_request_target", code)
                self.assertNotIn("workflow_run", code)

    def test_no_script_has_an_expression_pasted_into_it(self) -> None:
        # ${{ }} is expanded before the shell parses the script, so a tag,
        # title or branch name there is code. Values reach scripts through env.
        for path in self.files:
            for script in _run_scripts(_text(path)):
                with self.subTest(workflow=path.name, script=script.strip()[:60]):
                    self.assertNotIn("${{", script)


class ReleasePleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _text(RELEASE_PLEASE)
        self.jobs = _jobs(self.text)

    def test_it_runs_only_on_pushes_to_main(self) -> None:
        head = _code(self.text).split("\njobs:\n", 1)[0]
        self.assertRegex(head, r"(?m)^on:\n  push:\n    branches:\n      - main\n(?!  \S)")

    def test_the_workflow_has_no_token_of_its_own_and_one_job(self) -> None:
        self.assertEqual(_top_permissions(self.text), {})
        self.assertEqual(set(self.jobs), {"release-please"})
        self.assertEqual(_mapping(self.jobs["release-please"], "permissions", 4),
                         {"contents": "write", "pull-requests": "write"})

    def test_it_only_keeps_the_release_pr_and_never_tags(self) -> None:
        # The tag ruleset is admin-only: the owner makes each release by hand.
        block = self.jobs["release-please"]
        self.assertIn("googleapis/release-please-action@", block)
        self.assertRegex(block, r"(?m)^          skip-github-release: true$")
        self.assertRegex(block, r"(?m)^          config-file: release-please-config\.json$")
        self.assertRegex(block, r"(?m)^          manifest-file: \.release-please-manifest\.json$")
        # The job that can write to the repository runs one action and no script.
        self.assertEqual(len(_steps(block)), 1)
        self.assertEqual(_run_scripts(self.text), [])

    def test_the_config_is_one_simple_package_with_no_pinned_version(self) -> None:
        config = json.loads(_text(CONFIG))
        self.assertEqual(config.get("release-type"), "simple")
        self.assertIs(config.get("bump-minor-pre-major"), True)
        self.assertIs(config.get("include-component-in-tag"), False)
        self.assertEqual(config.get("packages"), {".": {}})
        # release-as is sticky: left in the config, every later release PR
        # would propose the same version again.
        self.assertNotIn("release-as", json.dumps(config))
        # The action's skip-github-release input is what stops the tagging;
        # the workflow test above pins it, and nothing here may contradict it.
        self.assertIsNot(config.get("skip-github-release"), False)

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
        head = self.code.split("\njobs:\n", 1)[0]
        self.assertRegex(head, r"(?m)^on:\n  release:\n    types:\n      - published\n  workflow_dispatch:\n(?!  \S)")

    def test_jobs_and_their_permissions(self) -> None:
        self.assertEqual(_top_permissions(self.text), {})
        self.assertEqual(list(self.jobs), list(PUBLISH_PERMISSIONS))
        for job, expected in PUBLISH_PERMISSIONS.items():
            with self.subTest(job=job):
                self.assertEqual(_mapping(self.jobs[job], "permissions", 4), expected)

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
        # No job overrides the default (success of everything it needs).
        for word in ("always()", "!cancelled()", "failure()", "continue-on-error"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code)

    def test_check_refuses_before_anything_is_built(self) -> None:
        block = self.jobs["check"]
        scripts = _run_scripts("jobs:\n  check:\n" + block)
        self.assertEqual(len(scripts), 2, scripts)
        self.assertIn('if ! git merge-base --is-ancestor "$GITHUB_SHA" origin/main; then', scripts[0])
        self.assertIn("exit 1", scripts[0])
        self.assertEqual(
            scripts[1], 'python -I scripts/check_release_tag.py .release-please-manifest.json >> "$GITHUB_OUTPUT"'
        )
        self.assertRegex(block, r"(?m)^          fetch-depth: 0$")
        self.assertRegex(block, r"(?m)^          EVENT_NAME: \$\{\{ github\.event_name \}\}$")
        self.assertRegex(block, r"(?m)^          REF_TYPE: \$\{\{ github\.ref_type \}\}$")
        self.assertRegex(block, r"(?m)^          REF_NAME: \$\{\{ github\.ref_name \}\}$")
        self.assertRegex(block, r"(?m)^      version: \$\{\{ steps\.plan\.outputs\.version \}\}$")
        self.assertRegex(block, r"(?m)^      release: \$\{\{ steps\.plan\.outputs\.release \}\}$")

    def test_no_checkout_keeps_its_credentials(self) -> None:
        for job, block in self.jobs.items():
            for step in _steps(block):
                if "actions/checkout@" in step:
                    with self.subTest(job=job):
                        self.assertRegex(step, r"(?m)^          persist-credentials: false$")

    def test_only_check_and_build_see_the_source(self) -> None:
        # The jobs that sign, tag and relabel run no code from the checkout.
        for job, block in self.jobs.items():
            with self.subTest(job=job):
                self.assertEqual("actions/checkout@" in block, job in ("check", "build"))

    def test_images_are_built_fresh_and_pushed_by_digest_only(self) -> None:
        block = self.jobs["build"]
        builds = [step for step in _steps(block) if "docker/build-push-action@" in step]
        self.assertEqual(len(builds), 2)
        for step, dockerfile in zip(builds, ("core/Dockerfile", "fetcher/Dockerfile"), strict=True):
            with self.subTest(dockerfile=dockerfile):
                self.assertRegex(step, rf"(?m)^          file: {re.escape(dockerfile)}$")
                self.assertRegex(step, r"(?m)^          context: \.$")
                self.assertRegex(step, r"(?m)^          platforms: linux/amd64$")
                # No layer from a cache that another run wrote, and the base
                # image as the registry has it now, by its pinned digest.
                self.assertRegex(step, r"(?m)^          no-cache: true$")
                self.assertRegex(step, r"(?m)^          pull: true$")
                self.assertNotIn("cache-from", step)
                self.assertNotIn("cache-to", step)
                # Untagged: a tag appears only after the scan, in the tag job.
                self.assertNotRegex(step, r"(?m)^          tags:")
                self.assertNotRegex(step, r"(?m)^          push: ")
                self.assertRegex(
                    step,
                    r"(?m)^          outputs: type=image,name=\$\{\{ steps\.names\.outputs\.\w+ \}\},"
                    r"push-by-digest=true,name-canonical=true,push=true$",
                )
                self.assertRegex(step, r"(?m)^          provenance: false$")
                self.assertRegex(step, r"(?m)^          sbom: generator=\$\{\{ steps\.names\.outputs\.generator \}\}$")
        self.assertIn("VERSION=${{ needs.check.outputs.version }}", builds[0])
        self.assertNotIn("VERSION=", builds[1], "the fetcher image takes no build argument")

    def test_the_sbom_generator_is_pinned_by_digest_where_dependabot_finds_it(self) -> None:
        lines = [line for line in _text(SBOM_PIN).splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"\AFROM docker/buildkit-syft-scanner:[a-z0-9.-]+@sha256:[0-9a-f]{64}\Z")
        script = "\n".join(_run_scripts("jobs:\n  build:\n" + self.jobs["build"]))
        self.assertIn(".github/sbom/Dockerfile", script)
        # The workflow checks the line it read before handing it to the build.
        self.assertIn("^docker/buildkit-syft-scanner:[a-z0-9.-]+@sha256:[0-9a-f]{64}$", script)

    def test_the_scan_is_the_organisations_and_gets_exactly_the_pushed_digests(self) -> None:
        block = self.jobs["scan"]
        workflow = rf"{SECURITY_REPOSITORY}/\.github/workflows/trivy-images\.yml"
        self.assertRegex(block, rf"(?m)^    uses: {workflow}@[0-9a-f]{{40}}$")
        self.assertNotIn("steps:", block, "the scan job runs the shared workflow and nothing of its own")
        self.assertNotIn("secrets:", block)
        images = re.search(r"(?m)^      images: \|\n((?:        .+\n)+)", block)
        self.assertIsNotNone(images)
        assert images is not None
        self.assertEqual(
            [line.strip() for line in images[1].splitlines()],
            ["${{ needs.build.outputs.core-image }}@${{ needs.build.outputs.core-digest }}",
             "${{ needs.build.outputs.fetcher-image }}@${{ needs.build.outputs.fetcher-digest }}"],
        )
        # Nothing that would let a finding through from this repository.
        for word in ("exceptions-file", "secret-allowlist-file", "upload-sarif", "registry-login"):
            with self.subTest(word=word):
                self.assertNotIn(word, block)

    def test_no_security_scan_is_run_by_this_repository_itself(self) -> None:
        for word in ("trivy-action", "aquasecurity", "anchore", "grype", "semgrep scan", "gitleaks detect"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.code)

    def test_attest_signs_both_digests_and_runs_no_third_party_action(self) -> None:
        block = self.jobs["attest"]
        used = re.findall(r"uses: (\S+)@", block)
        self.assertEqual(sorted(used), ["actions/attest-build-provenance", "actions/attest-build-provenance",
                                        "actions/attest-sbom", "actions/attest-sbom"])
        for image in ("core", "fetcher"):
            with self.subTest(image=image):
                name = f"subject-name: ${{{{ needs.build.outputs.{image}-image }}}}"
                digest = f"subject-digest: ${{{{ needs.build.outputs.{image}-digest }}}}"
                self.assertEqual(block.count(name), 2)
                self.assertEqual(block.count(digest), 2)
        self.assertEqual(block.count("push-to-registry: true"), 4)

    def test_tag_names_only_the_version_and_never_moves_one(self) -> None:
        block = self.jobs["tag"]
        script = "\n".join(_run_scripts("jobs:\n  tag:\n" + block))
        self.assertIn('--tag "$image:$VERSION" "$image@$digest"', script)
        self.assertRegex(block, r"(?m)^          VERSION: \$\{\{ needs\.check\.outputs\.version \}\}$")
        # A version tag that exists must already be this digest.
        self.assertIn("refusing to move", script)
        self.assertNotRegex(self.code, r"(?i)\blatest\b")

    def test_relabel_touches_only_the_merged_release_pr_of_this_commit(self) -> None:
        block = self.jobs["relabel"]
        script = "\n".join(_run_scripts("jobs:\n  relabel:\n" + block))
        self.assertIn("autorelease: pending", script)
        self.assertIn("autorelease: tagged", script)
        self.assertIn("$GITHUB_SHA", script)
        self.assertIn("--state merged", script)
        self.assertNotIn("uses:", block, "the job that can write to pull requests runs no action")

    def test_the_registry_login_reads_the_token_from_stdin(self) -> None:
        logins = [script for script in _run_scripts(self.text) if "docker login" in script]
        self.assertEqual(len(logins), 3, "build, attest and tag each log in")
        for script in logins:
            self.assertIn("--password-stdin", script)
            self.assertNotRegex(script, r"--password[ =]")
            self.assertNotRegex(script, r" -p ")


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
