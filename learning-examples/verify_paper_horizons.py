"""Verify journal horizons offline: real decoder, virtual clock, temporary SQLite.

No wallet, dotenv, network, signing, real sleeps, or existing state access.
Run: uv run --offline --no-sync learning-examples/verify_paper_horizons.py
"""

from __future__ import annotations

# This isolated consumer scenario intentionally exercises private integration seams.
# ruff: noqa: E402, S101, SLF001, PLR2004
import asyncio
import json
import math
import sqlite3
import struct
import sys
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey

from core.pubkeys import USDC_MINT, WSOL_MINT
from interfaces.core import Platform, TokenInfo
from learning.journal import PAPER_HORIZONS, LessonJournal, LessonObservation
from learning.report import load_report
from monitoring.trade_flow import TradeEvent
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from trading.universal_trader import UniversalTrader
from utils.idl_parser import IDLParser


class Clock:
    """Advance only the sampler's observation clock, not asyncio internals."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waiters: list[tuple[float, asyncio.Future]] = []
        self.yield_loop = asyncio.sleep

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    async def advance(self, now: float) -> None:
        self.now = now
        for due, future in self.waiters:
            if due <= now and not future.done():
                future.set_result(None)
        await self.yield_loop(0)
        await self.yield_loop(0)


class Accounts:
    """Feed encoded synthetic curve bytes through the production decoder."""

    def __init__(self) -> None:
        self.manager = object.__new__(PumpFunCurveManager)
        self.manager._idl_parser = IDLParser(str(ROOT / "idl/pump_fun_idl.json"))
        self.quote = 30_000_000_000
        self.complete = False
        self.quote_mint = Pubkey.default()
        self.error = False

    async def get_pool_state_and_token_program(
        self, curve: Pubkey, mint: Pubkey, commitment: str
    ) -> tuple[dict, Pubkey]:
        del curve, mint, commitment
        if self.error:
            message = "credential-bearing transport text must not be retained"
            raise TimeoutError(message)
        raw = bytes((23, 183, 248, 55, 96, 216, 172, 96))
        raw += struct.pack(
            "<5Q?",
            1_000_000_000_000_000,
            self.quote,
            800_000_000_000_000,
            100_000_000,
            1_000_000_000_000_000,
            self.complete,
        )
        raw += bytes(32) + bytes(2) + bytes(self.quote_mint) + bytes(36)
        return self.manager._decode_curve_state_with_idl(raw), Pubkey.default()


async def scenario(path: Path) -> dict:  # noqa: PLR0915 - one end-to-end scenario
    """Exercise pricing, exact identity, censoring, shutdown and report eligibility."""
    journal = LessonJournal(path)
    clock, accounts = Clock(), Accounts()
    token = TokenInfo(
        name="Synthetic",
        symbol="OFFLINE",
        uri="",
        mint=Pubkey.new_unique(),
        platform=Platform.PUMP_FUN,
        bonding_curve=Pubkey.new_unique(),
        quote_mint=WSOL_MINT,
    )
    event = TradeEvent(
        mint=str(token.mint),
        user="synthetic",
        creator="synthetic",
        is_buy=True,
        sol_amount=1,
        token_amount=1,
        virtual_sol_reserves=30_000_000_000,
        virtual_token_reserves=1_000_000_000_000_000,
        real_sol_reserves=100_000_000,
        real_token_reserves=800_000_000_000_000,
        slot=1,
        signature="synthetic",
        timestamp=1,
    )
    trader = object.__new__(UniversalTrader)
    trader.lesson_journal = journal
    trader._position_tasks = set()
    trader._paper_tasks = set()
    trader._fatal_monitor_errors = asyncio.Queue()
    trader.platform_implementations = SimpleNamespace(curve_manager=accounts)

    def start(entry_event: TradeEvent | None = event) -> int:
        entry_id = journal.record(
            LessonObservation(
                kind="gate_pass",
                mint=str(token.mint),
                symbol=token.symbol,
                buyers=3,
                raw={"event": asdict(entry_event)} if entry_event else {},
            ),
            jev={"quality": 2.0, "is_copycat": 0.2},
        )
        assert entry_id is not None
        trader._schedule_paper_marks(token, entry_id, entry_event, clock.now)
        return entry_id

    async def finish_window() -> None:
        started = clock.now
        await clock.yield_loop(0)
        for horizon in PAPER_HORIZONS:
            await clock.advance(started + horizon)
        await trader._drain_paper_marks()

    with (
        patch("trading.universal_trader.monotonic", lambda: clock.now),
        patch("trading.universal_trader.asyncio.sleep", clock.sleep),
    ):
        first = start()
        before = load_report(db_path=path)
        assert [row["pending"] for row in before["paper_marks"]] == [1, 1, 1]
        assert before["paper_coverage"]["pending_entries"] == 1
        assert before["paper_comparisons"] == []
        await clock.yield_loop(0)
        await clock.advance(60)
        accounts.quote = 33_000_000_000
        await clock.advance(300)
        await clock.advance(600)
        draining = asyncio.create_task(trader._drain_paper_marks())
        await clock.yield_loop(0)
        assert not draining.done(), "900s mark must survive the 600s entry window"
        accounts.quote = 27_000_000_000
        await clock.advance(900)
        await draining
        report = load_report(db_path=path)
        returns = [row["paired_mean_return"] for row in report["paper_marks"]]
        assert all(
            math.isclose(a, b, abs_tol=1e-12)
            for a, b in zip(returns, (0.0, 0.1, -0.1), strict=True)
        ), returns
        assert report["resolved"] == 0, "Gross marks must not become realized PnL"

        # Same mint, different entry price: no cross-link or Cartesian pairing.
        accounts.quote = 30_000_000_000
        second = start(replace(event, virtual_sol_reserves=60_000_000_000))
        await finish_window()
        actual = journal._conn.execute(
            "SELECT entry_id, horizon_s, exit_price/entry_price-1 FROM paper_marks"
            " WHERE entry_id=? ORDER BY horizon_s",
            (second,),
        ).fetchall()
        assert [row[2] for row in actual] == [-0.5, -0.5, -0.5]
        assert all(
            row["paired_n"] == 2 for row in load_report(db_path=path)["paper_marks"]
        )

        # Cancellations before the coroutine's first instruction still get a row.
        cancelled = start()
        for task in tuple(trader._paper_tasks):
            task.cancel()
        await clock.yield_loop(0)
        await clock.yield_loop(0)
        reasons = journal._conn.execute(
            "SELECT reason, exit_price FROM paper_marks WHERE entry_id=?",
            (cancelled,),
        ).fetchall()
        assert reasons == [("cancelled", None)] * 3

        accounts.complete = True
        start()
        await finish_window()
        accounts.complete = False
        accounts.quote_mint = USDC_MINT
        start()
        await finish_window()
        accounts.quote_mint = Pubkey.default()
        accounts.error = True
        start()
        await finish_window()
        accounts.error = False
        start(None)
        late = start()
        await clock.yield_loop(0)
        await clock.advance(clock.now + 920)
        await trader._drain_paper_marks()
        assert (
            journal._conn.execute(
                "SELECT reason FROM paper_marks WHERE entry_id=?",
                (late,),
            ).fetchall()
            == [("late",)] * 3
        )
        assert trader._fatal_monitor_errors.empty()

    # A pre-cutover record remains immutable but is ineligible for analysis.
    journal._conn.execute(
        "UPDATE lessons SET outcome_pnl_sol=0.5, outcome_reason='paper_exit_60s'"
        " WHERE id=?",
        (first,),
    )
    journal._conn.commit()

    # Quote-aware outcome links: a SOL close converts into the SOL column,
    # another quote asset keeps raw units and its mint and stays out of the
    # SOL-denominated aggregates.
    journal.record(
        LessonObservation(kind="gate_pass", mint="closed-sol", symbol="SOLCLOSE")
    )
    journal.record(
        LessonObservation(kind="gate_pass", mint="closed-usdc", symbol="USDCCLOSE")
    )
    journal.link_outcome(
        "closed-sol", 11_000_000 - 10_000_000, WSOL_MINT, "take_profit"
    )
    journal.link_outcome(
        "closed-usdc", 12_000_000 - 10_000_000, USDC_MINT, "take_profit"
    )
    journal.close()
    report = load_report(db_path=path)
    assert report["excluded_legacy_paper"] == 1
    assert report["pnl_by_quality"] == [] and report["resolved"] == 2
    # The pre-cutover row above has no source mark, so it is excluded and counted.
    assert report["excluded_unclassified"] == 1
    sol_close = next(r for r in report["recent"] if r["symbol"] == "SOLCLOSE")
    assert sol_close["outcome_pnl_sol"] == 0.001
    assert sol_close["outcome_quote_mint"] == str(WSOL_MINT)
    usdc_close = next(r for r in report["recent"] if r["symbol"] == "USDCCLOSE")
    assert usdc_close["outcome_pnl_sol"] is None
    assert usdc_close["outcome_pnl_quote_raw"] == 2_000_000
    assert usdc_close["outcome_quote_mint"] == str(USDC_MINT)
    assert "brier_win" not in report
    assert report["promotion_allowed"] is False
    assert all(
        row["planned"] == 8
        and row["censored"] == 6
        and row["pending"] == 0
        and row["paired_n"] == 2
        for row in report["paper_marks"]
    )
    coverage = report["paper_coverage"]
    assert (
        coverage["planned_entries"],
        coverage["paired_entries"],
        coverage["distinct_mints"],
        coverage["paired_mints"],
        coverage["pending_entries"],
        coverage["censored_entries"],
    ) == (8, 2, 1, 1, 0, 6)
    comparisons = report["paper_comparisons"]
    assert [row["horizon_s"] for row in comparisons] == [300, 900]
    assert all(
        math.isclose(row["mean_delta_vs_60s"], expected, abs_tol=1e-12)
        for row, expected in zip(comparisons, (0.05, -0.05), strict=True)
    )
    assert [
        (row["improved"], row["worsened"], row["unchanged"]) for row in comparisons
    ] == [(1, 0, 1), (0, 1, 1)]
    assert {row["reason"] for row in report["paper_censors"]} == {
        "cancelled",
        "late",
        "migrated_or_invalid_completion",
        "unsupported_quote",
        "unsupported_or_missing_entry",
        "read_error:TimeoutError",
    }
    assert sum(row["n"] for row in report["paper_censors"]) == 18
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        assert (
            conn.execute(
                "SELECT outcome_pnl_sol FROM lessons WHERE id=?", (first,)
            ).fetchone()[0]
            == 0.5
        )
        assert "credential-bearing" not in str(
            conn.execute("SELECT * FROM paper_marks").fetchall()
        )
    return {
        "paired": 2,
        "censored_cohorts": 6,
        "unchanged_price_return": returns[0],
        "900s_drained": True,
        "legacy_excluded_not_deleted": True,
        "paired_horizon_deltas": [row["mean_delta_vs_60s"] for row in comparisons],
    }


def main() -> None:
    with TemporaryDirectory(prefix="verify-paper-horizons-") as directory:
        print(json.dumps(asyncio.run(scenario(Path(directory) / "lessons.sqlite3"))))


if __name__ == "__main__":
    main()
