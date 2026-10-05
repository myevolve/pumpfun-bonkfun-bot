from __future__ import annotations

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import atexit
import shutil
import tempfile

from utils import paths as state_paths

# A test that writes a durable path without patching STATE_DIR lands in the
# operator's real .state directory, where a stray legacy ledger fails closed
# the whole runtime for that wallet. Redirect every state path to a throwaway
# directory for the session; a test that needs its own patches it back.
# This runs at import time so module-level constants that capture a state path
# (the lesson journal default, the report default) are redirected too.
_STATE_ROOT = Path(tempfile.mkdtemp(prefix="pumpfun-test-state-"))
state_paths.STATE_DIR = _STATE_ROOT / ".state"
atexit.register(shutil.rmtree, _STATE_ROOT, True)
