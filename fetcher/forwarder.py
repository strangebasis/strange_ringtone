"""Run the forwarder: python -I fetcher/forwarder.py [--bind HOST] (checkout) or /app/fetcher/forwarder.py (image).

A script, not a subcommand of the fetcher's entry (docs/PLAN.md 6.1): its
process loads only its own package, strange_ringtone_forwarder, and the
stdlib, never the core or the fetcher. Since Python 3.11, -I implies -P,
which leaves a script's own directory off sys.path (only a directory given
as the program, like `python -I fetcher`, still gets it), so this file puts
its own directory first itself, with string operations only: os and pathlib
are outside the package's import allowlist. That directory holds the
forwarder package and the fetcher's; the core lives elsewhere and stays out.
"""
from __future__ import annotations

import sys

# __file__ is absolute for a script since Python 3.9; both separators, so the
# checkout runs on Windows too. The fallbacks are edge cases: a script in
# the root directory gets "/", and a bare file name (which 3.9+ never gives)
# the current directory, where that script is. Never "", which sys.path reads
# as the current directory wherever the script lives.
_SEP = max(__file__.rfind("/"), __file__.rfind("\\"))
_HERE = __file__[:_SEP] if _SEP > 0 else (__file__[:1] if _SEP == 0 else ".")
sys.path.insert(0, _HERE)

from strange_ringtone_forwarder.__main__ import main  # noqa: E402  (after the sys.path edit above)

if __name__ == "__main__":
    sys.exit(main())
