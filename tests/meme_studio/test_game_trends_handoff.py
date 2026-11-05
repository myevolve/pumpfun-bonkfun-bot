# ruff: noqa: S101
"""Trends concept.json -> game loader -> game build. No network."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from support import FIXTURES

from meme_studio.game.build import build
from meme_studio.game.concept import ConceptError, load_concept
from meme_studio.trends.cli import main as trends_main


def _trends_concept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("REDDIT_TOS_ALLOWED", raising=False)
    code = trends_main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--output",
            str(tmp_path),
            "--now",
            "2026-10-28T15:00:00+00:00",
        ]
    )
    assert code == 0
    return tmp_path / "concept.json"


def test_game_builds_from_the_trends_concept_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    path = _trends_concept(tmp_path / "trends", monkeypatch)
    raw = json.loads(path.read_text(encoding="utf-8"))
    selected = next(
        item
        for item in raw["candidates"]
        if item["accepted"] and item["ticker"] == raw["selected_ticker"]
    )

    concept = load_concept(path)

    assert concept.ticker == raw["selected_ticker"] == "RACCDONU"
    assert concept.name == selected["name"]
    assert concept.template_id == "dodge_catch"
    assert concept.story_id == raw["selected_story_id"]
    assert concept.date == raw["date"]
    assert concept.palette.background == selected["theme_palette"]["background"].lower()
    assert concept.palette.ink == selected["theme_palette"]["secondary"].lower()
    assert concept.contract_address is None

    result = build(path, tmp_path / "hub", skip_clip=True)
    assert result.game_page.parent.name == raw["selected_story_id"]
    assert "RACCDONU" in result.hub_index.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"selected_ticker": "NOPE"}, "concept:"),
        ({"phase": "1"}, "concept:"),
        ({"candidates": "oops"}, "concept:"),
    ],
)
def test_malformed_trends_concept_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
    message: str,
) -> None:
    path = _trends_concept(tmp_path, monkeypatch)
    raw = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**raw, **change}), encoding="utf-8")
    with pytest.raises(ConceptError, match=message):
        load_concept(path)
