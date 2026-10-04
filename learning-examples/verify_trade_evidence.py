"""Verify durable trade evidence without keys, network, signing, or live state.

Uses synthetic receipts, the real receipt parser, coordinator and temporary SQLite
and recovery files. Simulations/paper profiles are not execution receipts. This
is an accounting/durability check, not trading or profitability evidence.

Usage: uv run --offline --no-sync python -B learning-examples/verify_trade_evidence.py
"""

# Assertions and private lifecycle methods are the executable regression contract.
# ruff: noqa: S101, SLF001

import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import verify_receipt_accounting as receipts
from solders.signature import Signature

from core.execution_policy import ExecutionMode, ExecutionPolicy
from core.transaction_ledger import EvidencePersistenceError, TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus
from trading.base import TradeResult
from trading.position import ExitReason
from trading.universal_trader import UniversalTrader

ROOT = Path(__file__).resolve().parent.parent
BUY_SIGNATURE = str(Signature.from_bytes(bytes([1]) * 64))
SELL_SIGNATURE = str(Signature.from_bytes(bytes([2]) * 64))
SENTINEL = "SYNTHETIC_CREDENTIAL_MUST_NOT_BE_CAPTURED"
FEE_BUDGET = 12_345
SELL_FEE = 6_000
SELL_PROCEEDS = 20


def guard(event: str, args: tuple) -> None:
    """Reject network and live-state/credential access before any operation."""
    receipts.deny_network(event, args)
    if event not in {"open", "os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
        return
    for value in args[:2] if event == "os.rename" else args[:1]:
        if not isinstance(value, str | bytes | os.PathLike):
            continue
        path = Path(os.fsdecode(value)).absolute()
        if path.name in {".env", ".env~", "ENVDATA"} or path.is_relative_to(
            ROOT / ".state"
        ):
            message = "Evidence verification forbids credentials and live state"
            raise RuntimeError(message)


def reserve(
    ledger: TransactionLedger, intent: str, signature: str, profile: str
) -> None:
    """Record synthetic identifiers; no signed wire or network submission exists."""
    ledger.record_intent(intent, str(receipts.OWNER), 10, FEE_BUDGET, "a" * 64)
    ledger.record_submission(
        intent, signature, "offline-blockhash", 100, evidence_profile_id=profile
    )


async def verify(work: Path) -> dict:  # noqa: PLR0915 - one durable lifecycle
    """Exercise capture, provenance, recovery and a real SQLite write failure."""
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        live_authorized=False,
        expected_wallet=str(receipts.OWNER),
        max_trade_quote_raw=1_000_000,
        max_total_fee_lamports=20_000,
        risk_session_id="offline-evidence-check",
        max_session_quote_raw=1_000_000,
        max_session_fee_lamports=40_000,
    )
    path = work / "ledger.sqlite3"
    journal = work / "positions.json"
    with (
        patch("learning.journal._DEFAULT_DB", work / "lessons.sqlite3"),
        patch("trading.universal_trader.JevScorer", return_value=None),
        patch(
            "trading.universal_trader.Wallet",
            return_value=SimpleNamespace(pubkey=receipts.OWNER),
        ),
        patch(
            "trading.universal_trader.ListenerFactory.create_listener",
            return_value=SimpleNamespace(),
        ),
    ):
        trader = UniversalTrader(
            rpc_endpoint=f"https://offline.invalid/{SENTINEL}",
            wss_endpoint=f"wss://offline.invalid/{SENTINEL}",
            private_key=SENTINEL,
            geyser_api_token=SENTINEL,
            buy_amount=0.001,
            buy_slippage=0.1,
            sell_slippage=0.1,
            exit_strategy="manual",
            cleanup_mode="disabled",
            execution_policy=policy,
            transaction_ledger_path=path,
            position_journal_path=journal,
        )
    try:
        ledger = trader.transaction_ledger
        assert ledger is not None
        original_profile = trader.solana_client.evidence_profile_id
        profile = ledger.connection.execute(
            "SELECT * FROM evidence_profiles WHERE profile_id = ?", (original_profile,)
        ).fetchone()
        assert profile["kind"] == "live"
        assert SENTINEL not in profile["settings_json"]
        assert json.loads(profile["settings_json"])["allowed_quote_mints"] is None
        sources = json.loads(profile["sources_json"])
        assert "src/trading/universal_trader.py" in sources
        assert "idl/pump_fun_idl.json" in sources

        token = receipts.fixture._make_token_info()
        token.mint = receipts.MINT
        token.bonding_curve = receipts.VENUE
        trader._log_trade = Mock()  # Legacy diagnostic log must not write to the repo.
        trader.seller.execute = AsyncMock(side_effect=AssertionError("Must not submit"))
        trader._record_trade_evidence(
            "decision",
            token,
            action="buy",
            intent_id=trader._buy_intent_id(token),
            reason="entry_gate_disabled",
            gate=None,
        )
        reserve(ledger, trader._buy_intent_id(token), BUY_SIGNATURE, original_profile)
        body = receipts.buy_receipt()
        body["slot"] = 10
        body["transaction"]["signatures"] = [BUY_SIGNATURE]
        trader.solana_client.post_rpc = AsyncMock(return_value={"result": body})
        amount_raw, quote_raw = await trader.solana_client.get_buy_transaction_details(
            BUY_SIGNATURE, token.mint, receipts.VENUE
        )
        amount = amount_raw / 1_000_000
        await trader._handle_successful_buy(
            token,
            TradeResult(
                success=True,
                tx_signature=BUY_SIGNATURE,
                amount=amount,
                amount_raw=amount_raw,
                quote_amount_raw=quote_raw,
                price=quote_raw / 1_000_000_000 / amount,
                fee_lamports=FEE_BUDGET,
                account_balance_baseline_raw=0,
                slot=10,
            ),
        )
        position = trader._active_positions[str(token.mint)][1]
        assert json.loads(journal.read_text())["positions"]

        # Recovery under changed configuration must not relabel the original buy.
        trader.buy_slippage = 0.2
        trader._initialize_evidence_profile({"listener_type": "offline-recovery"})
        observer_profile = trader.solana_client.evidence_profile_id
        assert observer_profile != original_profile
        intent = f"sell:{position.position_id}:1"
        position.mark_exit_intent(intent, ExitReason.MANUAL, position.entry_price * 2)
        position.mark_exit_pending(
            SELL_SIGNATURE, ExitReason.MANUAL, fee_lamports=FEE_BUDGET
        )
        trader._persist_position(token, position)
        trader._record_trade_evidence(
            "decision",
            token,
            position=position,
            action="sell",
            intent_id=intent,
            reason=ExitReason.MANUAL.value,
            trigger_price=position.pending_exit_price,
        )
        reserve(ledger, intent, SELL_SIGNATURE, observer_profile)
        trader.solana_client.confirm_transaction_outcome = AsyncMock(
            return_value=TransactionOutcome(
                TransactionStatus.SUCCESS, SELL_SIGNATURE, slot=11
            )
        )
        trader.solana_client.post_rpc.return_value = {"result": None}

        async def stop_waiting(_seconds: float) -> bool:
            trader._shutdown_event.set()
            return True

        trader._sleep_until_shutdown = stop_waiting
        await trader._monitor_position_loop(token, position)
        assert position.is_active and position.pending_exit_signature == SELL_SIGNATURE
        assert (
            ledger.connection.execute(
                "SELECT count(*) FROM evidence_events WHERE category = 'position_closed'"
            ).fetchone()[0]
            == 0
        )

        sell_body = {
            "slot": 11,
            "transaction": {
                "signatures": [SELL_SIGNATURE],
                "message": {"accountKeys": [str(receipts.OWNER)]},
            },
            "meta": {
                "err": None,
                "fee": SELL_FEE,
                "preBalances": [1_000_000_000],
                "postBalances": [1_000_000_000 + SELL_PROCEEDS - SELL_FEE],
                "preTokenBalances": [],
                "postTokenBalances": [],
            },
        }
        trader.solana_client.post_rpc.return_value = {"result": sell_body}
        ledger.connection.execute(
            "CREATE TEMP TRIGGER reject_close BEFORE INSERT ON evidence_events "
            "WHEN NEW.category = 'position_closed' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END"
        )
        trader._shutdown_event.clear()
        try:
            await trader._monitor_position_loop(token, position)
        except EvidencePersistenceError as exc:
            assert exc.__cause__ is not None
        else:
            message = "Evidence loss must prevent local position removal"
            raise AssertionError(message)
        assert position.is_active and json.loads(journal.read_text())["positions"]
        ledger.connection.execute("DROP TRIGGER reject_close")
        await trader._monitor_position_loop(token, position)
        assert (
            not position.is_active and not json.loads(journal.read_text())["positions"]
        )
        trader.seller.execute.assert_not_awaited()
    finally:
        await trader._cleanup_resources()

    with TransactionLedger(path) as reopened:
        closed = reopened.connection.execute(
            "SELECT payload_json FROM evidence_events WHERE category = 'position_closed'"
        ).fetchall()
        assert len(closed) == 1
        assert json.loads(closed[0][0])["quote_amount_raw"] == SELL_PROCEEDS
        assert (
            reopened.get_active_submission_record(
                f"buy:{token.platform.value}:{token.mint}"
            ).evidence_profile_id
            == original_profile
        )
        fees = dict(
            reopened.connection.execute(
                "SELECT signature, observed_fee_lamports FROM evidence_receipts"
            ).fetchall()
        )
        assert fees == {BUY_SIGNATURE: "5000", SELL_SIGNATURE: str(SELL_FEE)}
        assert FEE_BUDGET not in map(int, fees.values())
        # Same scenario/settings cannot share a fingerprint across evidence kinds.
        kinds = ["live", "dry_run", "simulation", "paper"]
        identities = [reopened.record_evidence_profile(kind, {}, {}) for kind in kinds]
        assert len(set(identities)) == len(kinds)
        # Exercise the actual read-only CLI while committed rows are still in WAL.
        before = path.read_bytes()
        audit_process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-m",
            "learning.trade_evidence",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await audit_process.communicate()
        assert audit_process.returncode == 0, stderr.decode()
        assert SENTINEL not in stdout.decode()
        audit = json.loads(stdout)
        assert not audit["event_issues"], audit["event_issues"]
        assert audit["live_network_fees"]["submission_count"] == len(fees)
        assert audit["live_network_fees"]["observed_count"] == len(fees)
        assert audit["live_network_fees"]["known_subtotal_lamports"] == 5_000 + SELL_FEE
        assert audit["live_network_fees"]["complete_finalized_total_lamports"] is None
        assert {
            blocker["code"]
            for blocker in audit["live_network_fees"]["finalized_total_blockers"]
        } == {"unfinalized_live_fees"}
        original = next(
            row for row in audit["transactions"] if row["signature"] == BUY_SIGNATURE
        )
        assert original["submission_profile_id"] == original_profile
        sold = next(
            row for row in audit["transactions"] if row["signature"] == SELL_SIGNATURE
        )
        native = sold["economic_receipt"]["native"]
        assert native["change_lamports"] == SELL_PROCEEDS - SELL_FEE
        assert native["change_excluding_network_fee_lamports"] == SELL_PROCEEDS
        assert native["commitment"] == "confirmed"
        assert audit["version"] == 7  # noqa: PLR2004 - public report schema contract
        events = {event["event_id"]: event for event in audit["events"]}
        closure = next(
            event for event in events.values() if event["category"] == "position_closed"
        )
        assert {
            link["signature"]: link["relations"] for link in closure["submission_links"]
        } == {
            BUY_SIGNATURE: ["position_entry"],
            SELL_SIGNATURE: ["signature_reference"],
        }
        assert closure["event_id"] in original["event_ids"]
        assert closure["event_id"] in sold["event_ids"]
        assert "decision" in {
            events[event_id]["category"] for event_id in original["event_ids"]
        }
        assert path.read_bytes() == before
    return {
        "checks": [
            "secret_free_profile",
            "original_provenance",
            "observed_fees",
            "unknown_receipt_stays_open",
            "write_failure_keeps_recovery",
            "closed_history_survives_restart",
            "practice_kinds_distinct",
            "read_only_audit_with_committed_wal",
            "lifecycle_claims_reconcile",
            "observed_native_balance_changes",
            "linked_lifecycle_observations",
            "finalized_fee_total_blockers",
        ],
        "live_execution": False,
        "storage": "temporary SQLite and recovery journal",
    }


async def main() -> None:
    """Run the bounded offline proof and remove its temporary storage."""
    with TemporaryDirectory(prefix="verify-trade-evidence-") as directory:
        print(json.dumps(await verify(Path(directory)), sort_keys=True))


if __name__ == "__main__":
    sys.addaudithook(guard)
    asyncio.run(main())
