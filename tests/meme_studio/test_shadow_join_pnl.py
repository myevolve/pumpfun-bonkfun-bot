# ruff: noqa: S101, PLR2004
"""Per-coin and cohort paper P&L, parquet output, and the out-of-sample sweep."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from meme_studio.ladder import build_plan
from meme_studio.shadow_join import (
    Concept,
    TrackerCoin,
    TrackerTrade,
    build_join_report,
    load_tracker,
    sweep_thresholds,
    write_join_report,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shadow_tracker_sample.json"
_CREATE = 1_700_000_000


def _coin(
    mint: str,
    create_ts: int,
    trades: list[TrackerTrade],
) -> TrackerCoin:
    return TrackerCoin(
        mint=mint,
        create_ts=create_ts,
        creator="CreatorPaper111",
        name="Kiwi",
        symbol="KIWI",
        uri="https://example.invalid/k.json",
        description="kiwi",
        dev_buy_sol="0.05",
        mayhem=False,
        telegram="https://t.me/k",
        x="https://x.com/k",
        website=None,
        quote_mint="sol",
        graduation_ts=None,
        trades=tuple(trades),
        pumpswap_creator_fee_raw=0,
    )


def _buy(ts: int, raw: int) -> TrackerTrade:
    return TrackerTrade(
        ts=ts,
        user="BuyerA",
        is_buy=True,
        sol_amount_raw=raw,
        creator_fee_raw=0,
    )


def _sell(ts: int, raw: int) -> TrackerTrade:
    return TrackerTrade(
        ts=ts,
        user="BuyerA",
        is_buy=False,
        sol_amount_raw=raw,
        creator_fee_raw=0,
    )


def test_join_replays_each_dev_buy_and_writes_parquet(tmp_path: Path) -> None:
    tracker = load_tracker(_FIXTURE)
    report = build_join_report(tracker)
    written = write_join_report(report, tmp_path, "2026-10-28")
    table = pq.read_table(written.parquet_path)

    assert (
        written.parquet_path
        == tmp_path / "phase0" / "2026-10-28" / "shadow_join.parquet"
    )
    assert written.summary_path.is_file()
    population = [
        coin.mint
        for coin in tracker.coins
        if coin.quote_mint in {"sol", "So11111111111111111111111111111111111111112"}
    ]
    assert table.num_rows == len(population) * 3
    dev_buys = set(table.column("dev_buy_sol").to_pylist())
    assert dev_buys == {"0.070000000", "0.100000000", "0.250000000"}
    assert set(table.column("plan_sha256").to_pylist()) == {
        build_plan(size, sol_usd="101.47").sha256 for size in ("0.07", "0.1", "0.25")
    }
    summary = written.summary_path.read_text(encoding="utf-8")
    assert '"paper": true' in summary
    assert "So10004paperMint" not in summary or "excluded" in summary
    cohorts = {
        row.cohort: row for row in report.cohorts if row.dev_buy_sol == "0.100000000"
    }
    assert cohorts["P"].n >= 1
    assert cohorts["C"].n >= 1
    assert cohorts["N"].n >= cohorts["P"].n
    assert report.excluded_non_sol == 1


def test_parameter_sweep_selects_on_train_and_reports_test() -> None:
    train_dump = [
        _buy(_CREATE + 10, 75_000_000_000),
        _sell(_CREATE + 500, 60_000_000_000),
    ]
    test_moon = [_buy(_CREATE + 10, 75_000_000_000)]
    coins = [
        _coin("early-a", _CREATE, train_dump),
        _coin("early-b", _CREATE + 1, train_dump),
        _coin("late-a", _CREATE + 100_000, test_moon),
        _coin("late-b", _CREATE + 100_001, test_moon),
    ]
    concepts = (Concept(story_id="story-kiwi", terms=("kiwi",)),)
    sweep = sweep_thresholds(
        coins,
        concepts,
        sol_usd="100",
        dev_buy_sols=("0.10",),
    )

    assert sweep.selection_basis == "train_closeout_pnl"
    assert sweep.published_plan_unchanged is True
    assert sweep.n_train == 2
    assert sweep.n_test == 2
    assert len(sweep.rows) == 27
    train_scores = [row.train_mean_closeout_lamports for row in sweep.rows]
    assert sweep.selected_train_mean_closeout_lamports == max(train_scores)
    assert sweep.selected_test_mean_closeout_lamports is not None
    published = build_plan("0.1", sol_usd="100")
    assert sweep.published_plan_sha256 == published.sha256
    assert any(row.plan_sha256 == published.sha256 for row in sweep.rows)
    again = sweep_thresholds(
        coins,
        concepts,
        sol_usd="100",
        dev_buy_sols=("0.10",),
    )
    assert again.selected_params == sweep.selected_params
    assert (
        again.selected_test_mean_closeout_lamports
        == sweep.selected_test_mean_closeout_lamports
    )


def test_bootstrap_ci_is_deterministic_and_brackets_a_constant_mean() -> None:
    tracker = load_tracker(_FIXTURE)
    first = build_join_report(tracker)
    second = build_join_report(tracker)
    row = next(
        item
        for item in first.cohorts
        if item.cohort == "population" and item.dev_buy_sol == "0.100000000"
    )
    again = next(
        item
        for item in second.cohorts
        if item.cohort == "population" and item.dev_buy_sol == "0.100000000"
    )

    assert (
        row.bootstrap_mean_pnl_lamports_ci95 == again.bootstrap_mean_pnl_lamports_ci95
    )
    assert row.n >= 1
    low, high = row.bootstrap_mean_pnl_lamports_ci95
    assert low <= row.mean_realized_pnl_lamports <= high
