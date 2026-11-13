"""A build step of the fetcher image: delete every yt-dlp extractor that is not on the keep list (docs/PLAN.md 6.1).

The runner starts yt-dlp with --use-extractors youtube and nothing else, and
yt-dlp imports an extractor's module only when that extractor is used. The
modules for the other sites, more than nine hundred of them, are therefore
code this image never runs, and some carry keys and tokens of those sites in
their source, which an image scan rightly reports as secrets. Removing them
leaves less in the image for a compromised run to use and nothing of that
kind for a scan to find.

It runs as root during the build, once the wheels are in, so it runs on the
base interpreter with -I and -S and imports only the standard library: an
interpreter with site enabled on the venv would run a wheel's .pth lines as
root (tests/test_venv_build_steps.py). It never imports from the directory it
prunes and follows no symlink in it.

Everything is checked before anything is deleted. A name on the list that
yt-dlp no longer ships fails the build, so a bump that renames a module is
seen at build time rather than in a fetch.

Usage: python -I -S prune_extractors.py KEEP_LIST EXTRACTOR_DIR
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys

# A module as name.py or a package as name/: one path component, lower case,
# so no entry can name anything outside the directory.
_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*(?:\.py|/)")
# Without these yt-dlp cannot start, whatever else the list says.
REQUIRED = ("__init__.py", "extractors.py", "lazy_extractors.py", "common.py")


class PruneError(Exception):
    pass


def read_keep(path: str) -> list[str]:
    """The names on the keep list, in order, or PruneError."""
    try:
        with open(path, encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except (OSError, ValueError) as error:
        raise PruneError(f"cannot read the keep list: {type(error).__name__}") from error
    names: list[str] = []
    for number, line in enumerate(lines, 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if _NAME_RE.fullmatch(name) is None:
            raise PruneError(f"line {number} of the keep list is not a module (name.py) or a package (name/)")
        if name in names:
            raise PruneError(f"line {number} of the keep list repeats {name}")
        names.append(name)
    missing = [name for name in REQUIRED if name not in names]
    if missing:
        raise PruneError(f"the keep list lacks {', '.join(missing)}, which yt-dlp cannot start without")
    return names


def prune(keep: list[str], directory: str) -> tuple[int, int]:
    """Delete every entry of directory that keep does not name. Returns (kept, removed), or PruneError.

    Nothing is deleted unless directory is a real directory and holds every
    kept name as what the list says it is: a regular file for name.py, a real
    directory for name/.
    """
    try:
        info = os.lstat(directory)
    except OSError as error:
        raise PruneError(f"cannot read the extractor directory: {type(error).__name__}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise PruneError("the extractor directory is not a directory")
    with os.scandir(directory) as found:
        entries = {entry.name: entry for entry in found}
    kept: set[str] = set()
    for name in keep:
        base = name.rstrip("/")
        entry = entries.get(base)
        if entry is None:
            raise PruneError(f"{name} is on the keep list, but yt-dlp ships no such extractor: update the list")
        wanted_directory = name.endswith("/")
        is_directory = entry.is_dir(follow_symlinks=False)
        is_file = entry.is_file(follow_symlinks=False)
        if entry.is_symlink() or (wanted_directory and not is_directory) or (not wanted_directory and not is_file):
            raise PruneError(f"{name} is not what the keep list says it is")
        kept.add(base)
    removed = 0
    for name in sorted(entries):
        if name in kept:
            continue
        entry = entries[name]
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            # A file, or a symlink, which is removed and never followed.
            os.unlink(entry.path)
        removed += 1
    return len(kept), removed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    try:
        kept, removed = prune(read_keep(argv[0]), argv[1])
    except PruneError as error:
        print(f"prune_extractors: {error}", file=sys.stderr)
        return 1
    print(f"prune_extractors: kept {kept} entries of yt-dlp's extractors and removed {removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
