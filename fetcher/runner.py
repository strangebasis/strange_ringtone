"""Run the runner: python -I fetcher/runner.py [--bind HOST] (checkout) or /app/fetcher/runner.py (image).

A script beside forwarder.py, not a subcommand of the fetcher's entry
(docs/PLAN.md 6.1): its process loads the runner's package, the three
fetcher modules it shares (strange_ringtone_fetch.config, .metadata and
.routes) and the two core modules they need (strange_ringtone.config and
.http_guard), never the fetcher's server, jobs or client. Since Python 3.11,
-I implies -P, which leaves a script's own directory off sys.path, so this
file puts two directories in front itself: the core (../core, which in the
image holds exactly those two modules) first, as fetcher/__main__.py does,
so nothing else installed can stand in for them, then its own directory,
which holds the runner's package and the fetcher's.
"""
from __future__ import annotations

import sys
from pathlib import Path

# No import allowlist binds this file (the forwarder's script avoids pathlib
# for its package's sake), so pathlib resolves the directories.
_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent / "core"
sys.path[:0] = [str(_CORE), str(_HERE)]

from strange_ringtone_runner.__main__ import main  # noqa: E402  (after the sys.path edit above)

if __name__ == "__main__":
    sys.exit(main())
