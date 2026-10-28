# ruff: noqa: S101
"""Daily 08:00 PT shadow report from concept, lint, drafts, and tracker fixtures."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from meme_studio.drafts import ConceptPackage, write_launch_drafts
from meme_studio.report import (
    ReportRequest,
    generate_daily_report,
    load_shadow_tracker,
    report_due_at_0800_pt,
    run_scheduled_report,
)
from tests.meme_studio.conftest import CREATOR, FIXTURES, MINT

if TYPE_CHECKING:
    from pathlib import Path

_PIGEON_BUYERS = 2
_CONTROL_BUYERS = 3


def test_report_due_only_during_the_0800_pt_minute() -> None:
    pdt_due = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    pdt_early = datetime(2026, 10, 28, 14, 59, tzinfo=UTC)
    pdt_late = datetime(2026, 10, 28, 15, 1, tzinfo=UTC)
    pst_due = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
    pst_early = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
    assert report_due_at_0800_pt(pdt_due) is True
    assert report_due_at_0800_pt(pdt_early) is False
    assert report_due_at_0800_pt(pdt_late) is False
    assert report_due_at_0800_pt(pst_due) is True
    assert report_due_at_0800_pt(pst_early) is False


def test_shadow_fixture_metrics_exclude_creator_and_sum_fees() -> None:
    snapshot = load_shadow_tracker(FIXTURES / "shadow_join.json")
    by_mint = {mint.mint: mint for mint in snapshot.mints}
    pigeon = by_mint["MintPigeon"]
    assert pigeon.outside_buyers == _PIGEON_BUYERS
    assert pigeon.creator_fees_sol == pytest.approx(0.0067)
    assert pigeon.graduated is False
    assert pigeon.dev_buy_sol == pytest.approx(0.1)
    assert pigeon.mayhem is False
    assert pigeon.symbol == "BENCH"
    graded = by_mint["MintGrad"]
    assert graded.graduated is True
    assert graded.outside_buyers == 1
    assert graded.creator_fees_sol == pytest.approx(0.01)
    assert snapshot.paper_replay["0.1"]["plan_adherence"] == 1.0
    assert snapshot.paper_replay["0.1"]["label"] == "PAPER"
    assert snapshot.random_control["outside_buyers_24h"] == _CONTROL_BUYERS
    assert snapshot.story_ids == ("story-pigeon",)
    assert snapshot.date == "2026-10-27"


def _request(tmp_path: Path, now: datetime, drafts: Path, art: Path) -> ReportRequest:
    return ReportRequest(
        output_dir=tmp_path,
        concept={
            "story_id": "story-pigeon",
            "name": "Bench Birds",
            "ticker": "BENCH",
            "chosen": True,
            "skip_reason": "",
            "score": 0.42,
            "stories_seen": [
                {
                    "id": "story-pigeon",
                    "score": 0.42,
                    "chosen": True,
                    "skip_reason": "",
                },
                {
                    "id": "story-storm",
                    "score": 0.05,
                    "chosen": False,
                    "skip_reason": "below threshold",
                },
            ],
        },
        compliance_results=(
            {
                "status": "fail",
                "reasons": ["banned_phrase:moon"],
                "human_approval_required": False,
                "template_id": "sample",
            },
        ),
        drafts_dir=drafts,
        shadow_path=FIXTURES / "shadow_join.json",
        now=now,
        incidents=("linter blocked one phrase",),
        pnl_label="PAPER",
        art_dir=art,
    )


def test_generate_report_combines_inputs(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts"
    art = tmp_path / "art"
    art.mkdir()
    (art / "logo.meta.json").write_text(
        json.dumps(
            {
                "prompt": "wild cartoon pigeon",
                "provider": "xai-grok-imagine",
                "model": "grok-imagine",
                "timestamp": "2026-10-28T18:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    concept = ConceptPackage(
        story_id="story-pigeon",
        story_one_liner="park pigeons started a tiny union",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
        ticker="BENCH",
        character="a round cartoon pigeon in a paper hat",
        mint=MINT,
        game_url="https://games.example/bench",
        plan_url="https://games.example/bench/plan",
        plan_hash="ab" * 32,
        wallet=CREATOR,
        dev_buy_sol="0.1",
        studio="ParodyDesk",
        steve_handle="steve",
        hub_domain="games.example",
    )
    write_launch_drafts(concept, datetime(2026, 10, 28, 18, 0, tzinfo=UTC), drafts)
    now = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    body = generate_daily_report(_request(tmp_path / "out", now, drafts, art))
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    on_disk = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert body["phase"] == "shadow"
    assert body["schedule"] == "08:00 PT"
    assert body["pnl_label"] == "PAPER"
    assert body["due"] is True
    assert on_disk["pnl_label"] == "PAPER"
    assert "PAPER" in markdown
    assert "08:00 PT" in markdown
    assert "below threshold" in markdown
    assert "banned_phrase:moon" in markdown
    assert "xai-grok-imagine" in markdown
    assert body["plan_adherence"] == 1.0
    assert body["shadow"]["mints"][0]["outside_buyers"] == _PIGEON_BUYERS
    assert "linter blocked one phrase" in body["incidents"]
    assert "30" in body["priors"]
    channels = set(body["drafts"]["channels"])
    assert {"x", "telegram", "site"} <= channels
    assert "M1" in body["drafts"]["human_approval_required"]


def test_scheduled_run_skips_other_minutes(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "manifest.json").write_text(
        json.dumps({"phase": "shadow", "schedule": [], "posts": []}),
        encoding="utf-8",
    )
    art = tmp_path / "art"
    art.mkdir()
    early = _request(
        tmp_path / "early",
        datetime(2026, 10, 28, 14, 59, tzinfo=UTC),
        drafts,
        art,
    )
    assert run_scheduled_report(early) is None
    assert not (tmp_path / "early" / "report.md").exists()
    due = _request(
        tmp_path / "due",
        datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
        drafts,
        art,
    )
    written = run_scheduled_report(due)
    assert written is not None
    assert (tmp_path / "due" / "report.md").exists()
    assert (tmp_path / "due" / "report.json").exists()


def test_report_does_not_open_sockets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", boom)
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "manifest.json").write_text(
        json.dumps(
            {
                "phase": "shadow",
                "schedule": [],
                "posts": [
                    {
                        "template_id": "P1",
                        "channel": "x",
                        "human_approval_required": False,
                        "lint": {"status": "pass"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    art = tmp_path / "art"
    art.mkdir()
    generate_daily_report(
        _request(
            tmp_path / "out",
            datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
            drafts,
            art,
        )
    )
