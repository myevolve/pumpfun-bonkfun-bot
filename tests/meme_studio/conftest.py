"""Shadow meme-studio test fixtures. No network fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_imagine_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Imagine's shared daily ledger out of the repo and out of other tests."""
    monkeypatch.setenv(
        "MEME_STUDIO_IMAGINE_BUDGET_DIR", str(tmp_path / "imagine-budget")
    )
