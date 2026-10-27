"""Load the stdlib scripts under scripts/ as modules, without making scripts/ a package."""
from __future__ import annotations

import importlib.util
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
