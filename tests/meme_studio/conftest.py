"""Path setup for shadow meme-studio tests. No network fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CREATOR = "Fund" + "1" * 36
MINT = "Ca" + "2" * 38
COLD = "Save" + "4" * 36
HIDDEN = "Extra" + "3" * 35
SIG = "5" * 88
FIXTURES = Path(__file__).resolve().parent / "fixtures"
