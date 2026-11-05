# ruff: noqa: S101, PLR2004
"""A zero-valued tracker trade is censored at ingestion, never aborting a report."""

from __future__ import annotations

import json
from pathlib import Path

from meme_studio.shadow_join import build_join_report, load_tracker


def _coin(mint: str, trades: list[dict[str, object]]) -> dict[str, object]:
    return {
        "mint": mint,
        "create_ts": 1_700_000_000,
        "creator": "CreatorPaper111",
        "name": "Kiwi",
        "symbol": "KIWI",
        "dev_buy_sol": "0.05",
        "mayhem": False,
        "quote_mint": "sol",
        "graduation_ts": None,
        "trades": trades,
    }


def test_zero_valued_trade_is_censored_and_report_completes(tmp_path: Path) -> None:
    good = [{"ts": 1_700_000_100, "user": "BuyerA", "is_buy": True, "sol_amount_raw": 500_000_000}]
    zero = [{"ts": 1_700_000_100, "user": "BuyerA", "is_buy": True, "sol_amount_raw": 0}]
    path = tmp_path / "tracker.json"
    path.write_text(
        json.dumps(
            {
                "sol_usd": "101.47",
                "concepts": [],
                "coins": [_coin("GoodMint", good), _coin("ZeroMint", zero)],
            }
        ),
        encoding="utf-8",
    )

    report = build_join_report(load_tracker(path))

    by_mint: dict[str, set[str]] = {}
    for row in report.rows:
        by_mint.setdefault(row.mint, set()).add(row.flags)
    assert set(by_mint) == {"GoodMint", "ZeroMint"}
    assert all("invalid_trade_direction" in flags for flags in by_mint["ZeroMint"])
    assert not any("invalid_trade_direction" in flags for flags in by_mint["GoodMint"])
