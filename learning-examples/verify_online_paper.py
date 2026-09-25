"""Verify online paper accounting and forward-only learning, entirely offline.

Uses synthetic public accounts and a temporary paper database. No credentials,
network calls, signing, bot configuration, or existing state are accessed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "learning-examples" / "token-lifecycles"))

import aiohttp
from evaluate_online_paper import (
    CONFIG,
    ENTRY_RESERVATION,
    Mark,
    PaperBook,
    read_status,
)
from run_paper_trader import RPC_ENDPOINTS, Runtime
from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT
from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFees,
    PumpFeeSnapshot,
    PumpFeeTier,
)

# One deterministic consumer-side scenario; integer protocol units are intentional.
# ruff: noqa: E402, S101, PLR2004


def mint(number: int) -> str:
    """Return a deterministic synthetic public key without a private key."""
    return str(Pubkey.from_bytes(number.to_bytes(32, "big")))


def mark(number: int, at: float, slot: int, quote_reserves: int) -> Mark:
    """Make a complete synthetic mark accepted at the engine boundary."""
    fees = PumpFees(0, 100, 25)
    config = PumpFeeConfig(
        bump=1,
        admin=Pubkey.default(),
        flat_fees=fees,
        regular_tiers=(PumpFeeTier(0, fees),),
        stable_tiers=(PumpFeeTier(0, fees),),
        exotic_flat_fees=fees,
        digest="a" * 64,
    )
    return Mark(
        mint=mint(number),
        slot=slot,
        observed_at=at,
        state={
            "virtual_token_reserves": 1_000_000_000_000_000,
            "virtual_quote_reserves": quote_reserves,
            "real_token_reserves": 800_000_000_000_000,
            "real_quote_reserves": 20_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
            "complete": False,
            "creator": str(Pubkey.from_bytes(bytes([7]) * 32)),
            "is_mayhem_mode": False,
            "is_cashback_coin": False,
            "quote_mint": str(WSOL_MINT),
        },
        fees=PumpFeeSnapshot(config, at, 0.0),
        supply_raw=1_000_000_000_000_000,
        proof={
            "synthetic_offline_fixture": True,
            "native_fee_attested": False,
            "creator_fee_override_verified": False,
            "fee_model": "decoded_fee_config_scenario_unattested",
            "request_started_monotonic": at - 0.1,
            "response_received_monotonic": at,
        },
    )


def paired_cohort(book: PaperBook, number: int, at: float) -> None:
    """Complete one paired example where the thirty-second arm is best."""
    before = book.status()
    assert book.discover(mint(number), at)
    book.on_mark(mark(number, at + 1, number * 1_000, 30_000_000_000))
    entered = book.status()
    assert entered["last_cohort"]["policy_revision"] == before["policy_revision"]
    book.on_mark(mark(number, at + 11, number * 1_000 + 25, 31_000_000_000))
    partial = book.status()
    assert partial["paired_complete"] == before["paired_complete"]
    assert partial["policy_revision"] == before["policy_revision"]
    # Re-delivery must not create another exit, example or policy revision.
    book.on_mark(mark(number, at + 11, number * 1_000 + 25, 31_000_000_000))
    assert book.status()["selected_closed"] == partial["selected_closed"]
    book.on_mark(mark(number, at + 31, number * 1_000 + 75, 45_000_000_000))
    book.on_mark(mark(number, at + 61, number * 1_000 + 150, 35_000_000_000))
    after = book.status()
    assert after["paired_complete"] == before["paired_complete"] + 1
    assert after["policy_revision"] > before["policy_revision"]
    assert not book.discover(mint(number), at + 62)


def check_learning(path: Path) -> dict:
    """Check paired updates, forward application, censoring and recovery."""
    book = PaperBook(path)
    try:
        assert book.status()["paper_only"] is True
        for number in range(1, 7):
            paired_cohort(book, number, float(number * 100))
        trained = book.status()
        assert trained["policy_hold_seconds"] == 30
        assert trained["selected_closed"] == 1
        assert trained["shadow_entries"] == 5
        assert trained["reserved_lamports"] == 0
        assert trained["unknown_inventory"] == 0
        assert book.discover(mint(7), 700.0)
        book.on_mark(mark(7, 701.0, 7_000, 30_000_000_000))
        entered = book.status()
        assert entered["last_entry"]["hold_seconds"] == 30
        assert entered["last_entry"]["policy_revision"] == trained["policy_revision"]
        assert entered["cash_lamports"] < trained["cash_lamports"]
        assert entered["reserved_lamports"] > 0
        # A status command is an observation, not recovery or an inventory reset.
        assert read_status(path)["unknown_inventory"] == 0
        assert read_status(path)["selected_open"] == entered["selected_open"]
        # Even a favorable new mark cannot fill an already missed exit window.
        book.on_mark(mark(7, 768.0, 7_200, 100_000_000_000))
        book.tick(769.0)
        censored = book.status()
        assert censored["paired_complete"] == trained["paired_complete"]
        assert censored["policy_revision"] == trained["policy_revision"]
        assert censored["selected_closed"] == trained["selected_closed"]
        assert censored["unknown_inventory"] == 1
        assert censored["censored_cohorts"] >= 1
        assert censored["cash_lamports"] == entered["cash_lamports"]
        assert censored["reserved_lamports"] == entered["reserved_lamports"]
    finally:
        book.close()
    reopened = PaperBook(path)
    try:
        recovered = reopened.status()
        for key in (
            "paired_complete",
            "selected_closed",
            "unknown_inventory",
            "cash_lamports",
            "reserved_lamports",
        ):
            assert recovered[key] == censored[key], key
        assert not reopened.discover(mint(7), 800.0)
        return recovered
    finally:
        reopened.close()


def check_failed_persistence(path: Path) -> None:
    """A failed durable write cannot become an observed paper entry."""
    book = PaperBook(path)
    try:
        before = read_status(path)
        with sqlite3.connect(path) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for index, (table,) in enumerate(tables):
                quoted = '"' + table.replace('"', '""') + '"'
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER fail_{index}_{operation} "
                        f"BEFORE {operation} ON {quoted} "
                        "BEGIN SELECT RAISE(ABORT, 'injected paper persistence failure'); END"
                    )
        failed = False
        try:
            book.discover(mint(99), 1_000.0)
            book.on_mark(mark(99, 1_001.0, 99_000, 30_000_000_000))
        except sqlite3.DatabaseError:
            failed = True
        assert failed, "paper persistence failure must surface"
        after = read_status(path)
        for key in (
            "cash_lamports",
            "reserved_lamports",
            "selected_closed",
            "last_entry",
        ):
            assert after[key] == before[key], key
    finally:
        book.close()


def check_supply_and_cash(root: Path) -> None:
    """Burns cannot change normal-curve fees; fees cannot overdraw paper cash."""
    book = PaperBook(root / "supply.sqlite3")
    try:
        first = mark(201, 1_001.0, 201_000, 30_000_000_000)
        config = replace(
            first.fees.config,
            regular_tiers=(
                PumpFeeTier(0, PumpFees(0, 100, 25)),
                PumpFeeTier(29_000_000_000, PumpFees(0, 200, 100)),
            ),
        )
        first = replace(first, fees=PumpFeeSnapshot(config, 1_001.0, 0.0))
        assert book.discover(first.mint, 1_000.0)
        book.on_mark(first)
        quantity = book.status()["last_cohort"]["quantity_raw"]
        burned = replace(
            first,
            mint=mint(202),
            slot=202_000,
            observed_at=1_002.0,
            supply_raw=900_000_000_000_000,
            fees=PumpFeeSnapshot(config, 1_002.0, 0.0),
        )
        assert book.discover(burned.mint, 1_001.0)
        book.on_mark(burned)
        assert book.status()["last_cohort"]["quantity_raw"] == quantity
    finally:
        book.close()
    with patch.dict(CONFIG, {"initial_cash_lamports": ENTRY_RESERVATION}):
        book = PaperBook(root / "cash.sqlite3")
        try:
            for number in range(300, 305):
                paired_cohort(book, number, float(number * 100))
            initial = book.status()["cash_lamports"]
            assert book.discover(mint(305), 30_500.0)
            for delay in (0, 10, 30, 60):
                book.on_mark(
                    mark(305, 30_501.0 + delay, 305_000 + delay, 30_000_000_000)
                )
            depleted = book.status()
            assert depleted["entries"] == 1
            assert 0 <= depleted["cash_lamports"] < ENTRY_RESERVATION
            assert depleted["reserved_lamports"] == 0
            assert (
                depleted["cash_lamports"]
                == initial + depleted["known_paper_net_lamports"]
            )
            for number in range(306, 320):
                paired_cohort(book, number, float(number * 100))
            status = book.status()
            assert status["paired_complete"] > depleted["paired_complete"]
            assert status["scores"]["30"]["score_lamports"] > 0
            assert (
                status["last_cohort"]["portfolio_skip_reason"]
                == "insufficient_virtual_cash"
            )
            assert status["cash_lamports"] == depleted["cash_lamports"]
            assert (
                status["known_paper_net_lamports"]
                == depleted["known_paper_net_lamports"]
            )
            assert status["entries"] == depleted["entries"]
            assert status["unknown_inventory"] == 0
        finally:
            book.close()


def check_unrelated_database(path: Path) -> None:
    """Opening the wrong ledger must not add paper tables or rewrite it."""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value INTEGER)")
        connection.execute("INSERT INTO unrelated VALUES (123)")
    before = path.read_bytes()
    rejected = False
    try:
        PaperBook(path)
    except ValueError:
        rejected = True
    assert rejected
    assert path.read_bytes() == before


async def check_read_only_rpc(path: Path) -> None:
    """A transaction-submission request must be refused before any transport."""
    book = PaperBook(path)
    try:
        runtime = Runtime(book, RPC_ENDPOINTS[0])
        rejected = False
        async with aiohttp.ClientSession(trust_env=False) as session:
            try:
                await runtime.rpc(session, "sendTransaction", ["not-a-transaction"])
            except ValueError:
                rejected = True
        assert rejected, "paper RPC must not expose transaction submission"
    finally:
        book.close()


def check_paper_reserve_impact(path: Path) -> None:
    """An unchanged empty curve can repay the SOL deposited by the paper buy."""
    book = PaperBook(path)
    try:
        for number in range(490, 495):
            paired_cohort(book, number, float(number * 100))
        assert book.discover(mint(500), 50_000.0)
        entry = mark(500, 50_001.0, 500_000, 30_000_000_000)
        entry = replace(entry, state={**entry.state, "real_quote_reserves": 0})
        book.on_mark(entry)
        for hold in (10, 30, 60):
            exit_mark = mark(500, 50_001.0 + hold, 500_000 + hold, 30_000_000_000)
            exit_mark = replace(
                exit_mark, state={**exit_mark.state, "real_quote_reserves": 0}
            )
            book.on_mark(exit_mark)
        status = book.status()
        assert status["selected_closed"] == 1
        assert status["paired_complete"] == 6
        assert status["unknown_inventory"] == 0
        assert status["known_paper_net_lamports"] < 0
        assert status["reserved_lamports"] == 0
    finally:
        book.close()


def check_no_trade_shadow_learning(path: Path) -> None:
    """Losing scenarios teach abstention without spending or inventing inventory."""
    book = PaperBook(path)
    try:
        initial = book.status()["cash_lamports"]
        for number in range(600, 606):
            at = float(number * 100)
            assert book.discover(mint(number), at)
            for delay in (0, 10, 30, 60):
                book.on_mark(
                    mark(number, at + 1 + delay, number * 1_000 + delay, 30_000_000_000)
                )
        status = book.status()
        assert status["cash_lamports"] == initial
        assert status["known_paper_net_lamports"] == 0
        assert status["paired_complete"] == 6
        assert status["entries"] == 0
        assert status["shadow_entries"] == 6
        assert status["portfolio_entry_block"] == "nonpositive_score"
        assert status["last_cohort"]["policy_revision"] == 5
        assert status["last_cohort"]["portfolio_selected"] is False
        assert book.discover(mint(606), 60_600.0)
        book.on_mark(mark(606, 60_601.0, 606_000, 30_000_000_000))
        book.tick(60_670.0)
        censored = book.status()
        assert censored["unknown_inventory"] == 0
        assert censored["reserved_lamports"] == 0
        assert censored["policy_revision"] == status["policy_revision"]
    finally:
        book.close()


def check_shadow_exposure_capacity(path: Path) -> None:
    """Unpriced portfolio positions block spending, not fresh shadow learning."""
    book = PaperBook(path)
    try:
        for number in range(650, 655):
            paired_cohort(book, number, float(number * 100))
        for number in range(700, 712):
            assert book.discover(mint(number), 70_000.0)
            book.on_mark(mark(number, 70_001.0, number * 1_000, 30_000_000_000))
        assert not book.discover(mint(712), 70_002.0)
        book.tick(70_070.0)
        before = book.status()
        assert before["unknown_inventory"] == 12
        assert before["selected_closed"] == 0
        assert book.discover(mint(712), 70_100.0)
        for delay in (0, 10, 30, 60):
            book.on_mark(mark(712, 70_101.0 + delay, 712_000 + delay, 30_000_000_000))
        after = book.status()
        assert after["last_cohort"]["portfolio_skip_reason"] == "exposure_capacity"
        assert after["paired_complete"] == before["paired_complete"] + 1
        for key in (
            "cash_lamports",
            "reserved_lamports",
            "unknown_positions",
            "entries",
        ):
            assert after[key] == before[key], key
    finally:
        book.close()


def main() -> None:
    """Run isolated paper checks and print only their synthetic results."""
    with TemporaryDirectory(prefix="verify-online-paper-") as directory:
        root = Path(directory).resolve()
        result = check_learning(root / "learning.sqlite3")
        check_failed_persistence(root / "write-failure.sqlite3")
        check_supply_and_cash(root)
        check_unrelated_database(root / "unrelated.sqlite3")
        asyncio.run(check_read_only_rpc(root / "read-only-rpc.sqlite3"))
        check_paper_reserve_impact(root / "reserve-impact.sqlite3")
        check_no_trade_shadow_learning(root / "no-trade.sqlite3")
        check_shadow_exposure_capacity(root / "shadow-capacity.sqlite3")
        print(
            json.dumps(
                {
                    "checks": [
                        "paired_outcomes_only",
                        "no_lookahead_policy_application",
                        "duplicate_delivery_idempotent",
                        "late_exit_remains_unknown",
                        "virtual_reservation_not_reset",
                        "restart_preserves_learning",
                        "status_is_read_only",
                        "persistence_failure_is_fatal",
                        "holder_burn_preserves_non_mayhem_fee_tier",
                        "exit_fee_escrow_prevents_overdraft",
                        "virtual_capital_exhaustion_stops_entries",
                        "unrelated_database_unchanged",
                        "transaction_submission_refused",
                        "paper_entry_liquidity_survives_into_exit",
                        "nonpositive_scores_choose_no_trade",
                        "exhausted_cash_does_not_stop_shadow_learning",
                        "unknown_inventory_does_not_stop_shadow_learning",
                        "shadow_censoring_never_creates_portfolio_inventory",
                    ],
                    "paired_complete": result["paired_complete"],
                    "selected_closed": result["selected_closed"],
                    "policy_hold_seconds": result["policy_hold_seconds"],
                    "unknown_inventory": result["unknown_inventory"],
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
