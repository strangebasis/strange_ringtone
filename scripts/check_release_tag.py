"""Decide what a run of the publish workflow builds, before it builds anything.

The owner creates every vX.Y.Z release by hand (docs/PLAN.md 9.3), so the tag
is typed by a person. A release is published only when the tag is exactly the
version the merged release PR recorded in the release-please manifest at the
tagged commit: a typo, a tag on the wrong commit or a tag made before the
release PR was merged stops here, with nothing built or pushed.

A run started by hand (workflow_dispatch) is a rehearsal: it builds, pushes
untagged, scans and attests, and never tags an image or touches a release. It
runs from main only.

On success, prints the two step outputs, `version=` and `release=`. On a
refusal, prints nothing to stdout, because stdout is appended to
$GITHUB_OUTPUT.

Usage: EVENT_NAME=... REF_TYPE=... REF_NAME=... python -I scripts/check_release_tag.py MANIFEST
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ASCII digits only, and no leading zeros: one spelling per version. Matched
# with fullmatch, so a trailing newline is not part of a tag.
_NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
TAG = re.compile(rf"v({_NUMBER}\.{_NUMBER}\.{_NUMBER})")
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


def plan(event_name: str, ref_type: str, ref_name: str, manifest: Path) -> tuple[str, bool]:
    """(version, whether this run releases), or ReleaseError with the reason nothing may be built."""
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
    recorded = manifest_version(manifest)
    if recorded == "0.0.0":
        raise ReleaseError("the release-please manifest records no release yet: merge the release PR first")
    if tagged != recorded:
        raise ReleaseError(
            f"the tag v{tagged} is not the version the manifest records at this commit, {recorded}: "
            f"create the release from the merged release PR's commit, as v{recorded}"
        )
    return tagged, True


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    environ = os.environ
    try:
        version, release = plan(
            environ.get("EVENT_NAME", ""), environ.get("REF_TYPE", ""), environ.get("REF_NAME", ""), Path(argv[0])
        )
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
