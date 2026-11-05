# ruff: noqa: S101
"""Recaps never claim plan adherence from the mere presence of plan text."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from support import CREATOR, MINT

from meme_studio.drafts import ConceptPackage, SellEvent, write_launch_drafts


def test_contradictory_sells_with_plan_text_make_no_adherence_claim(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    # The plan says one sell at 2.5x then stop; the sells break it twice.
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
        plan_text="Recover cost at 2.5 times, then stop. It can go to zero.",
        wallet=CREATOR,
        dev_buy_sol="0.1",
        studio="ParodyDesk",
        steve_handle="steve",
        hub_domain="games.example",
        sells=(
            SellEvent(
                n=1,
                step="X",
                tokens="900000",
                sol="0.9",
                multiple="1.1",
                pct="10",
                sig="5" * 88,
                at=t0 + timedelta(hours=1),
            ),
            SellEvent(
                n=2,
                step="Y",
                tokens="100000",
                sol="0.1",
                multiple="8",
                pct="0",
                sig="6" * 88,
                at=t0 + timedelta(hours=2),
            ),
        ),
    )
    recaps = ("M10", "site_recap")
    unverified = write_launch_drafts(concept, t0, tmp_path / "unverified")
    for record in unverified:
        if record.template_id in recaps:
            assert "all per plan" not in record.text
            assert "plan adherence UNKNOWN" in record.text
    verified_off = write_launch_drafts(
        replace(concept, plan_adherence_verified=False), t0, tmp_path / "off"
    )
    for record in verified_off:
        if record.template_id in recaps:
            assert "NOT all per plan" in record.text
    verified_on = write_launch_drafts(
        replace(concept, plan_adherence_verified=True), t0, tmp_path / "on"
    )
    texts = [r.text for r in verified_on if r.template_id in recaps]
    assert texts
    assert all("all per plan" in text and "NOT" not in text for text in texts)
