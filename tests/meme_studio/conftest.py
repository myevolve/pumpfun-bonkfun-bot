"""Put the repo root on ``sys.path`` so ``meme_studio`` imports in these tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_imagine_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Imagine's shared daily ledger out of the repo and out of other tests."""
    monkeypatch.setenv(
        "MEME_STUDIO_IMAGINE_BUDGET_DIR", str(tmp_path / "imagine-budget")
    )
