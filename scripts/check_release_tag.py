"""Decide what a run of the publish workflow builds, before it builds anything.

The owner creates every vX.Y.Z release by hand (docs/PLAN.md 9.3), so the tag
and the commit it sits on are chosen by a person. A release is published only
when all of this holds, and otherwise nothing is built or pushed:

- the tag is exactly 'v' and the version the release-please manifest records
  at the tagged commit (a typo, or a tag made before the release PR merged);
- the tagged commit is the one that last changed the manifest, which is the
  release PR's merge commit (GitHub's release form offers the tip of main,
  which may already hold later changes that the CHANGELOG doesn't list);
- the release is not marked as a pre-release.

A run started by hand (workflow_dispatch) is a rehearsal: it builds, pushes
untagged, scans and attests, and never tags an image or touches a release. It
runs from main only.

On success, prints the two step outputs, `version=` and `release=`. On a
refusal, prints nothing to stdout, because stdout is appended to
$GITHUB_OUTPUT.

Usage: EVENT_NAME=... REF_TYPE=... REF_NAME=... PRERELEASE=... COMMIT=... MANIFEST_COMMIT=... \\
    python -I scripts/check_release_tag.py MANIFEST
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

# ASCII digits only, and no leading zeros: one spelling per version. Matched
# with fullmatch, so a trailing newline is not part of a tag.
_NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
TAG = re.compile(rf"v({_NUMBER}\.{_NUMBER}\.{_NUMBER})")
COMMIT = re.compile(r"[0-9a-f]{40}")
# What a rehearsal's images call themselves: a VERSION the core accepts, and
# one no release can have.
REHEARSAL = "rehearsal"
DEFAULT_BRANCH = "main"


class ReleaseError(Exception):
    pass


def manifest_version(manifest: Path) -> str:
    """The version the manifest records for the root package, or ReleaseError."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"cannot read the release-please manifest {manifest.name}: {type(exc).__name__}") from exc
    version = data.get(".") if isinstance(data, dict) else None
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise ReleaseError(f"the release-please manifest {manifest.name} has no 'X.Y.Z' version under '.'")
    return version


def plan(environ: Mapping[str, str], manifest: Path) -> tuple[str, bool]:
    """(version, whether this run releases), or ReleaseError with the reason nothing may be built.

    environ holds what the workflow passes: EVENT_NAME, REF_TYPE and REF_NAME
    from the event, PRERELEASE from the release, COMMIT (the commit the run
    builds) and MANIFEST_COMMIT (the last commit that changed the manifest).
    A missing value is an empty string, which every check refuses.
    """
    event_name = environ.get("EVENT_NAME", "")
    ref_type = environ.get("REF_TYPE", "")
    ref_name = environ.get("REF_NAME", "")
    if event_name == "workflow_dispatch":
        if ref_type != "branch" or ref_name != DEFAULT_BRANCH:
            raise ReleaseError(f"a rehearsal runs only from {DEFAULT_BRANCH}")
        return REHEARSAL, False
    if event_name != "release":
        raise ReleaseError("this workflow runs on release or workflow_dispatch only")
    if ref_type != "tag":
        raise ReleaseError("the release's ref is not a tag")
    match = TAG.fullmatch(ref_name)
    if match is None:
        raise ReleaseError(f"{ref_name!r} is not a release tag: it must be 'v' and a version such as v0.1.0")
    tagged = match[1]
    # Exactly "false": anything else, a missing value included, is refused.
    if environ.get("PRERELEASE", "") != "false":
        raise ReleaseError("the release is marked as a pre-release, or doesn't say: only full releases publish images")
    recorded = manifest_version(manifest)
    if recorded == "0.0.0":
        raise ReleaseError("the release-please manifest records no release yet: merge the release PR first")
    if tagged != recorded:
        raise ReleaseError(
            f"the tag v{tagged} is not the version the manifest records at this commit, {recorded}: "
            f"create the release from the merged release PR's commit, as v{recorded}"
        )
    commit = environ.get("COMMIT", "")
    manifest_commit = environ.get("MANIFEST_COMMIT", "")
    if COMMIT.fullmatch(commit) is None or COMMIT.fullmatch(manifest_commit) is None:
        raise ReleaseError("cannot tell which commit last changed the release-please manifest")
    if commit != manifest_commit:
        raise ReleaseError(
            f"v{tagged} is tagged on {commit[:12]}, but the commit that set the manifest to {recorded} is "
            f"{manifest_commit[:12]}, the release PR's merge commit: later commits are not part of this release"
        )
    return tagged, True


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    try:
        version, release = plan(os.environ, Path(argv[0]))
    except ReleaseError as error:
        # One line: repr() in the messages above keeps a ref's control
        # characters out of the log.
        print(f"::error::{error}", file=sys.stderr)
        return 1
    print(f"version={version}")
    print(f"release={'true' if release else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
