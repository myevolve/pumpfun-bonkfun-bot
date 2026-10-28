# ruff: noqa: S101
"""Missing, late, and mis-labeled tracker evidence stays unknown."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from meme_studio.ladder import build_plan
from meme_studio.ladder.engine import replay
from meme_studio.shadow_join import load_tracker, sweep_thresholds
from meme_studio.shadow_join.schema import Concept
from meme_studio.shadow_join.sweep import coin_to_path

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, coins: list[dict[str, object]]) -> Path:
    path = tmp_path / "tracker.json"
    path.write_text(
        json.dumps(
            {
                "sol_usd": "100",
                "concepts": [{"story_id": "story-kiwi", "terms": ["kiwi"]}],
                "coins": coins,
            }
        ),
        encoding="utf-8",
    )
    return path


def _coin(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "mint": "So10000paperMint",
        "create_ts": 1_700_000_000,
        "creator": "CreatorPaper111",
        "name": "Kiwi",
        "symbol": "KIWI",
        "uri": "https://example.invalid/k.json",
        "description": "kiwi",
        "dev_buy_sol": "0.05",
        "mayhem": False,
        "links": {"telegram": "https://t.me/k", "x": "https://x.com/k"},
        "quote_mint": "sol",
        "graduation_ts": None,
        "trades": [
            {
                "ts": 1_700_000_010,
                "user": "BuyerA",
                "is_buy": True,
                "sol_amount": "1.0",
                "creator_fee": "0.001",
            }
        ],
        "pumpswap_coin_creator_fee_rows": [],
    }
    base.update(overrides)
    return base


def test_missing_fee_does_not_become_zero(tmp_path: Path) -> None:
    trade = {
        "ts": 1_700_000_010,
        "user": "BuyerA",
        "is_buy": True,
        "sol_amount": "1.0",
    }
    tracker = load_tracker(tmp_path and _write(tmp_path, [_coin(trades=[trade])]))
    assert tracker.coins[0].trades[0].creator_fee_raw is None
    omitted = load_tracker(
        _write(
            tmp_path,
            [_coin(mint="So10001paperMint", pumpswap_coin_creator_fee_rows=None)],
        )
    )
    # Key deleted below.
    raw = json.loads((tmp_path / "tracker.json").read_text(encoding="utf-8"))
    del raw["coins"][0]["pumpswap_coin_creator_fee_rows"]
    (tmp_path / "tracker.json").write_text(json.dumps(raw), encoding="utf-8")
    omitted = load_tracker(tmp_path / "tracker.json")
    assert omitted.coins[0].pumpswap_fees_known is False


def test_creator_fee_row_outside_the_window_is_not_income(tmp_path: Path) -> None:
    late = {
        "ts": 1_700_000_000 + 1_000_000,
        "user": "CreatorPaper111",
        "creator_fee": "1.23456789",
    }
    tracker = load_tracker(
        _write(tmp_path, [_coin(pumpswap_coin_creator_fee_rows=[late])])
    )
    result = replay(coin_to_path(tracker.coins[0]), build_plan("0.1", sol_usd="100"))
    plain = load_tracker(_write(tmp_path, [_coin()]))
    baseline = replay(coin_to_path(plain.coins[0]), build_plan("0.1", sol_usd="100"))
    assert result.realized_pnl_lamports == baseline.realized_pnl_lamports
    assert "fee_outside_window" in result.flags


def test_unverifiable_fee_row_blocks_pnl(tmp_path: Path) -> None:
    row = {"ts": 1_700_000_100, "creator_fee": "0.5"}
    tracker = load_tracker(
        _write(tmp_path, [_coin(pumpswap_coin_creator_fee_rows=[row])])
    )
    result = replay(coin_to_path(tracker.coins[0]), build_plan("0.1", sol_usd="100"))
    assert result.realized_pnl_lamports is None
    assert result.observed_outside_creator_fee_lamports is None
    assert "creator_fee_unknown" in result.flags


def test_null_creator_and_string_direction_are_not_valid_observations(
    tmp_path: Path,
) -> None:
    tracker = load_tracker(
        _write(
            tmp_path,
            [
                _coin(
                    creator=None,
                    trades=[
                        {
                            "ts": 1_700_000_010,
                            "user": "CreatorPaper111",
                            "is_buy": "false",
                            "sol_amount": "50",
                            "creator_fee": "0",
                        }
                    ],
                )
            ],
        )
    )
    coin = tracker.coins[0]
    assert coin.creator is None
    assert coin.creator != "None"
    assert coin.trades[0].direction_known is False
    assert coin.trades[0].is_buy is False
    stories = {row.story_id: row for row in tracker.story_summaries()}
    assert stories["story-kiwi"].winner_share is None


def test_unavailable_sweep_does_not_abort_or_invent_a_mean() -> None:
    concepts = (Concept("story-kiwi", ("kiwi",)),)
    empty = sweep_thresholds([], concepts, sol_usd="100")
    assert empty.selection_basis == "unavailable"
    assert empty.selected_train_mean_realized_lamports is None
    assert empty.selected_plan_sha256 is None
    assert empty.rows == ()
