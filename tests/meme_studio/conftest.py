"""Path setup for shadow meme-studio tests. No network fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for _path in (SRC, ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

CREATOR = "Fund" + "1" * 36
MINT = "Ca" + "2" * 38
COLD = "Save" + "4" * 36
HIDDEN = "Extra" + "3" * 35
SIG = "5" * 88
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_imagine_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Imagine's shared daily ledger out of the repo and out of other tests."""
    monkeypatch.setenv(
        "MEME_STUDIO_IMAGINE_BUDGET_DIR", str(tmp_path / "imagine-budget")
    )
