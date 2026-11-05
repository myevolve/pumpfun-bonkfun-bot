# ruff: noqa: S101
"""A malformed tokens.json row voids the lookup instead of clearing collisions."""

import json
from pathlib import Path

import pytest

from meme_studio.trends.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-10-28T15:00:00+00:00"


def test_malformed_token_row_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("REDDIT_TOS_ALLOWED", raising=False)
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    for name in ("google_news.rss", "history.json", "x.json", "copycats.json"):
        (fixture_dir / name).write_bytes((FIXTURES / name).read_bytes())
    (fixture_dir / "tokens.json").write_text(
        json.dumps([{"symbol": "RACCDONU"}]), encoding="utf-8"
    )
    out = tmp_path / "out"
    code = main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(fixture_dir),
            "--output",
            str(out),
            "--now",
            NOW,
        ]
    )
    assert code == 0
    payload = json.loads((out / "concept.json").read_text(encoding="utf-8"))
    assert payload["skipped_reason"] == "lookup_unavailable"
    assert payload["selected_ticker"] is None
    assert payload["candidates"][0]["collision_checks"][0]["reason"] == (
        "lookup_unavailable"
    )
