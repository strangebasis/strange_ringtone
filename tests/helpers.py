"""What the tests share.

The checkout's packages on sys.path, the stdlib scripts under scripts/ loaded
as modules (without making scripts/ a package), and whether a process a test
started still runs.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# The core package lives under core/ (docs/PLAN.md section 11) and is not
# installed, so tests import it from the checkout.
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
# The fetcher's package lives under fetcher/ and imports the core's shared
# modules (strange_ringtone.config and http_guard) from CORE above.
FETCHER = ROOT / "fetcher"
if str(FETCHER) not in sys.path:
    sys.path.insert(0, str(FETCHER))


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alive(pid: int) -> bool:
    """Whether pid still runs. Killed but not yet reaped (a zombie) counts as gone. POSIX only.

    Signal 0 says whether the PID exists and /proc/<pid>/stat in what state,
    and the process can be reaped between the two looks. So a stat file that
    can't be read is no answer: the PID is asked once more, and a process
    reaped in between is then gone. Without a /proc to read (not Linux) a
    zombie can't be told from a running process and counts as running.
    """
    for _ in range(2):
        try:
            # Never on Windows, where signal 0 is CTRL_C_EVENT and not a question.
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                # The state follows the name, which stands in brackets and may hold brackets and spaces itself.
                state = handle.read().rsplit(b")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        # Z is a zombie. X is dead: its parent is collecting it at this moment.
        return state not in (b"Z", b"X")
    return True
