# Assertions and numeric/private boundaries are the executable ledger contract.
# ruff: noqa: S101, SLF001, PLR2004

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from core.execution_policy import TradeLimitExceeded
from core.transaction_ledger import (
    EvidencePersistenceError,
    LedgerConflict,
    TransactionLedger,
    default_transaction_ledger_path,
    resolve_transaction_ledger_path,
)
from core.transaction_state import TransactionOutcome, TransactionStatus


def _record_submission(
    ledger: TransactionLedger,
    *,
    intent: str = "intent",
    signature: str = "sig-1",
    blockhash: str = "blockhash",
    last_valid_block_height: int = 100,
    state: str = "submitted",
    wire_bytes: bytes | None = None,
    receipt_destinations: tuple[str, ...] | None = None,
    evidence_profile_id: str | None = None,
) -> str:
    ledger.record_intent(intent, "wallet", 10, 5, "a" * 64)
    return ledger.record_submission(
        intent,
        signature,
        blockhash,
        last_valid_block_height,
        state=state,
        wire_bytes=wire_bytes,
        receipt_destinations=receipt_destinations,
        evidence_profile_id=evidence_profile_id,
    )


def test_prepared_wire_reservation_is_durable_and_exact(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    wire = b"\x01signed-transaction\xff"

    with TransactionLedger(path) as ledger:
        assert _record_submission(ledger, state="prepared", wire_bytes=wire) == "sig-1"
        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
                state="prepared",
                wire_bytes=b"different-transaction",
            )
            == "sig-1"
        )

    with TransactionLedger(path) as recovered:
        active = recovered.get_active_submission_record("intent")
        assert active is not None
        assert active.signature == "sig-1"
        assert active.state == "prepared"
        assert active.wire_bytes == wire

        records = recovered.list_recoverable()
        assert len(records) == 1
        assert records[0].signature == "sig-1"
        assert records[0].state == "prepared"
        assert records[0].wire_bytes == wire


def test_receipt_destinations_are_bound_to_exact_submission(
    tmp_path: Path,
) -> None:
    destinations = ("primary", "protocol-fee", "creator-fee")
    path = tmp_path / "ledger.sqlite"

    with TransactionLedger(path) as ledger:
        _record_submission(
            ledger,
            state="prepared",
            wire_bytes=b"wire",
            receipt_destinations=destinations,
        )

    with TransactionLedger(path) as recovered:
        active = recovered.get_active_submission_record("intent")
        assert active is not None
        assert active.receipt_destinations == destinations
        assert recovered.get_receipt_destinations("sig-1") == destinations
        assert recovered.list_recoverable()[0].receipt_destinations == destinations


def test_signature_cannot_be_rebound_to_different_wire_bytes(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire-one")

        with pytest.raises(LedgerConflict, match="different wire bytes"):
            _record_submission(
                ledger,
                state="prepared",
                wire_bytes=b"wire-two",
            )

        assert ledger.get_active_submission_record("intent").wire_bytes == b"wire-one"


def test_cancelling_prepared_reservation_releases_intent(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"cancelled-wire")

        assert ledger.release_prepared_submission("sig-1") is True
        assert ledger.release_prepared_submission("sig-1") is False
        assert ledger.get_active_submission("intent") is None
        assert ledger.list_recoverable() == []

        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
                state="prepared",
                wire_bytes=b"replacement-wire",
            )
            == "sig-2"
        )


def test_unsubmitted_intent_can_be_replanned_after_restart(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.record_intent("intent", "wallet", 10, 5, "a" * 64)

        ledger.record_intent("intent", "wallet", 20, 7, "b" * 64)

        row = ledger.connection.execute(
            """
            SELECT quote_amount_raw, fee_lamports, message_hash
            FROM intents WHERE intent_id = 'intent'
            """
        ).fetchone()
        assert tuple(row) == ("20", "7", "b" * 64)


def test_submitted_intent_cannot_be_replanned(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire")

        with pytest.raises(LedgerConflict, match="different data"):
            ledger.record_intent("intent", "wallet", 20, 7, "b" * 64)


def test_submitted_reservation_cannot_be_cancelled(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"sent-wire")
        ledger.mark_submission_submitted("sig-1")

        assert ledger.release_prepared_submission("sig-1") is False
        active = ledger.get_active_submission_record("intent")
        assert active is not None
        assert active.state == "submitted"
        assert active.wire_bytes == b"sent-wire"


def test_unknown_outcome_blocks_duplicate_and_remains_recoverable(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, wire_bytes=b"submitted-wire")
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.UNKNOWN, "sig-1", "rpc timeout")
        )

        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
            )
            == "sig-1"
        )
        outcome = ledger.get_outcome("sig-1")
        assert outcome == TransactionOutcome(
            TransactionStatus.UNKNOWN,
            "sig-1",
            "rpc timeout",
        )
        assert [record.signature for record in ledger.list_recoverable()] == ["sig-1"]

        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.SUCCESS, "sig-1", slot=123)
        )
        assert ledger.list_recoverable() == []
        assert ledger.get_active_submission("intent") == "sig-1"


@pytest.mark.parametrize(
    "terminal_status",
    [TransactionStatus.REVERTED, TransactionStatus.EXPIRED],
)
def test_terminal_failure_allows_fresh_submission_for_same_intent(
    tmp_path: Path,
    terminal_status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(TransactionOutcome(terminal_status, "sig-1"))

        assert ledger.get_active_submission("intent") is None
        assert ledger.list_recoverable() == []
        assert (
            _record_submission(
                ledger,
                signature="sig-2",
                blockhash="blockhash-2",
                last_valid_block_height=101,
            )
            == "sig-2"
        )
        assert ledger.get_active_submission("intent") == "sig-2"


def test_ledger_does_not_overwrite_final_outcome(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(TransactionOutcome(TransactionStatus.REVERTED, "sig-1"))

        with pytest.raises(LedgerConflict):
            ledger.record_outcome(
                TransactionOutcome(TransactionStatus.SUCCESS, "sig-1")
            )

        assert ledger.get_outcome("sig-1").status is TransactionStatus.REVERTED


@pytest.mark.parametrize(
    "terminal_status",
    [
        TransactionStatus.SUCCESS,
        TransactionStatus.REVERTED,
        TransactionStatus.EXPIRED,
    ],
)
def test_terminal_signature_cannot_be_reserved_again(
    tmp_path: Path,
    terminal_status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(TransactionOutcome(terminal_status, "sig-1"))

        with pytest.raises(LedgerConflict, match="terminal outcome"):
            _record_submission(ledger)


def test_latest_submission_record_includes_terminal_outcome(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.REVERTED, "sig-1", "reverted")
        )

        assert ledger.get_active_submission_record("intent") is None
        latest = ledger.get_latest_submission_record("intent")
        assert latest is not None
        assert latest.signature == "sig-1"
        assert latest.fee_lamports == 5


def test_mark_submitted_rejects_missing_or_outcome_bound_submission(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(LedgerConflict, match="not tracked"):
            ledger.mark_submission_submitted("missing")

        _record_submission(ledger)
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.UNKNOWN, "sig-1", "timeout")
        )

        with pytest.raises(LedgerConflict, match="already has an outcome"):
            ledger.mark_submission_submitted("sig-1")


def test_mark_submitted_allows_idempotent_same_state_retry(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire")

        ledger.mark_submission_submitted("sig-1")
        ledger.mark_submission_submitted("sig-1")

        assert ledger.get_active_submission_record("intent").state == "submitted"


@pytest.mark.parametrize("status", list(TransactionStatus))
def test_outcome_rejects_missing_or_prepared_submission(
    tmp_path: Path,
    status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(LedgerConflict, match="not tracked"):
            ledger.record_outcome(TransactionOutcome(status, "missing"))

        _record_submission(
            ledger,
            signature="prepared",
            state="prepared",
            wire_bytes=b"wire",
        )
        with pytest.raises(LedgerConflict, match="not submitted"):
            ledger.record_outcome(TransactionOutcome(status, "prepared"))


@pytest.mark.parametrize("status", list(TransactionStatus))
def test_outcome_allows_idempotent_same_status_retry(
    tmp_path: Path,
    status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        outcome = TransactionOutcome(status, "sig-1", "evidence", slot=7)

        ledger.record_outcome(outcome)
        ledger.record_outcome(outcome)

        assert ledger.get_outcome("sig-1") == outcome


@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.SUCCESS,
        TransactionStatus.REVERTED,
        TransactionStatus.EXPIRED,
    ],
)
def test_terminal_outcome_rejects_same_status_with_conflicting_evidence(
    tmp_path: Path,
    status: TransactionStatus,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        recorded = TransactionOutcome(status, "sig-1", "first evidence", slot=7)
        ledger.record_outcome(recorded)

        with pytest.raises(LedgerConflict, match="conflicting evidence"):
            ledger.record_outcome(
                TransactionOutcome(status, "sig-1", "different evidence", slot=8)
            )

        assert ledger.get_outcome("sig-1") == recorded


def test_unknown_observations_converge_to_terminal_success(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger)
        first = TransactionOutcome(
            TransactionStatus.UNKNOWN,
            "sig-1",
            "first timeout",
        )
        ledger.record_outcome(first)
        ledger.record_outcome(
            TransactionOutcome(
                TransactionStatus.UNKNOWN,
                "sig-1",
                "second timeout",
                slot=8,
            )
        )

        assert ledger.get_outcome("sig-1") == first

        success = TransactionOutcome(
            TransactionStatus.SUCCESS,
            "sig-1",
            slot=9,
        )
        ledger.record_outcome(success)
        assert ledger.get_outcome("sig-1") == success


def test_close_is_idempotent_and_does_not_corrupt_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = TransactionLedger(path)
    _record_submission(ledger)

    ledger.close()
    ledger.close()

    with TransactionLedger(path) as reopened:
        assert reopened.get_active_submission("intent") == "sig-1"


def test_legacy_schema_is_migrated_without_losing_submission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE intents (
                intent_id TEXT PRIMARY KEY,
                signer TEXT NOT NULL,
                quote_amount_raw TEXT,
                fee_lamports TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE submissions (
                signature TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                blockhash TEXT NOT NULL,
                last_valid_block_height INTEGER NOT NULL,
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE outcomes (
                signature TEXT PRIMARY KEY REFERENCES submissions(signature),
                status TEXT NOT NULL CHECK (
                    status IN ('success', 'reverted', 'expired', 'unknown')
                ),
                error TEXT,
                slot INTEGER,
                observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO intents (
                intent_id, signer, quote_amount_raw, fee_lamports
            ) VALUES ('legacy-intent', 'legacy-wallet', '10', '5');
            INSERT INTO submissions (
                signature, intent_id, blockhash, last_valid_block_height
            ) VALUES ('legacy-sig', 'legacy-intent', 'legacy-blockhash', 99);
            """
        )

    with TransactionLedger(path) as ledger:
        active = ledger.get_active_submission_record("legacy-intent")
        assert active is not None
        assert active.signature == "legacy-sig"
        assert active.state == "submitted"
        assert active.wire_bytes is None
        assert active.evidence_profile_id is None
        current_profile = ledger.record_evidence_profile("live", {}, {})
        ledger.record_submission(
            "legacy-intent",
            "legacy-sig",
            "legacy-blockhash",
            99,
            evidence_profile_id=current_profile,
        )
        assert (
            ledger.get_latest_submission_record("legacy-intent").evidence_profile_id
            is None
        )
        assert ledger.list_recoverable()[0].evidence_profile_id is None
        assert [record.signature for record in ledger.list_recoverable()] == [
            "legacy-sig"
        ]

        assert (
            _record_submission(
                ledger,
                intent="new-intent",
                signature="new-sig",
                state="prepared",
                wire_bytes=b"new-wire",
            )
            == "new-sig"
        )


def _reserve_risk_submission(
    ledger: TransactionLedger,
    *,
    intent: str,
    signature: str,
    quote_mint: str = "SOL",
    quote_amount_raw: int,
    fee_lamports: int,
    session_id: str = "2026-05-01-live",
    max_session_quote_raw: int = 100,
    max_session_fee_lamports: int = 25,
    evidence_profile_id: str | None = None,
) -> None:
    ledger.record_intent(
        intent,
        "wallet",
        quote_amount_raw,
        fee_lamports,
        "a" * 64,
    )
    ledger.record_submission(
        intent,
        signature,
        f"blockhash-{signature}",
        100,
        state="prepared",
        wire_bytes=f"wire-{signature}".encode(),
        quote_mint=quote_mint,
        risk_session_id=session_id,
        max_session_quote_raw=max_session_quote_raw,
        max_session_fee_lamports=max_session_fee_lamports,
        intent_message_hash="a" * 64,
        evidence_profile_id=evidence_profile_id,
    )


def test_session_quote_budget_is_durable_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        _reserve_risk_submission(
            ledger,
            intent="buy-1",
            signature="sig-1",
            quote_amount_raw=60,
            fee_lamports=10,
        )

    with TransactionLedger(path) as ledger:
        with pytest.raises(TradeLimitExceeded, match="session quote"):
            _reserve_risk_submission(
                ledger,
                intent="buy-2",
                signature="sig-2",
                quote_amount_raw=41,
                fee_lamports=10,
            )

        totals = ledger.get_session_risk_totals("2026-05-01-live", "wallet")
        assert totals.quote_amount_raw_by_mint == {"SOL": 60}
        assert totals.fee_lamports == 10
        assert totals.submission_count == 1
        assert ledger.get_active_submission("buy-2") is None


def test_session_fee_budget_counts_each_signed_submission(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _reserve_risk_submission(
            ledger,
            intent="sell-1",
            signature="sig-1",
            quote_amount_raw=0,
            fee_lamports=15,
        )
        ledger.mark_submission_submitted("sig-1")
        ledger.record_outcome(TransactionOutcome(TransactionStatus.REVERTED, "sig-1"))

        with pytest.raises(TradeLimitExceeded, match="session fee"):
            _reserve_risk_submission(
                ledger,
                intent="sell-1",
                signature="sig-2",
                quote_amount_raw=0,
                fee_lamports=15,
            )

        totals = ledger.get_session_risk_totals("2026-05-01-live", "wallet")
        assert totals.fee_lamports == 15
        assert totals.submission_count == 1


def test_new_session_id_explicitly_resets_cumulative_budget(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _reserve_risk_submission(
            ledger,
            intent="buy-1",
            signature="sig-1",
            quote_amount_raw=100,
            fee_lamports=25,
        )
        _reserve_risk_submission(
            ledger,
            intent="buy-2",
            signature="sig-2",
            quote_amount_raw=100,
            fee_lamports=25,
            session_id="2026-05-02-live",
        )

        assert ledger.get_session_risk_totals(
            "2026-05-01-live", "wallet"
        ).quote_amount_raw_by_mint == {"SOL": 100}
        assert ledger.get_session_risk_totals(
            "2026-05-02-live", "wallet"
        ).quote_amount_raw_by_mint == {"SOL": 100}


def test_releasing_never_submitted_wire_releases_session_budget(
    tmp_path: Path,
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        profile = ledger.record_evidence_profile("live", {"strategy": "first"}, {})
        _reserve_risk_submission(
            ledger,
            intent="buy-1",
            signature="sig-1",
            quote_amount_raw=100,
            fee_lamports=25,
            evidence_profile_id=profile,
        )

        assert ledger.release_prepared_submission("sig-1") is True
        totals = ledger.get_session_risk_totals("2026-05-01-live", "wallet")
        assert totals.quote_amount_raw_by_mint == {}
        assert totals.fee_lamports == 0
        assert totals.submission_count == 0
        assert (
            ledger.connection.execute(
                "SELECT 1 FROM intents WHERE intent_id = 'buy-1'"
            ).fetchone()
            is None
        )
        released = ledger.connection.execute(
            "SELECT profile_id, payload_json FROM evidence_events "
            "WHERE category = 'submission_released'"
        ).fetchone()
        assert released["profile_id"] == profile
        assert json.loads(released["payload_json"]) == {
            "signature": "sig-1",
            "intent_id": "buy-1",
            "quote_amount_raw": 100,
            "fee_budget_lamports": 25,
            "wire_sha256": hashlib.sha256(b"wire-sig-1").hexdigest(),
        }
        assert ledger.release_prepared_submission("sig-1") is False
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM evidence_events"
            ).fetchone()[0]
            == 1
        )


def test_releasing_operation_wire_preserves_its_generation(tmp_path: Path) -> None:
    operation_key = "manual-cleanup:wallet:mint:account"
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        intent = ledger.reserve_operation_intent(operation_key, "wallet")
        _reserve_risk_submission(
            ledger,
            intent=intent,
            signature="cleanup-signature",
            quote_amount_raw=0,
            fee_lamports=5,
        )

        assert ledger.release_prepared_submission("cleanup-signature") is True
        assert ledger.get_active_submission(intent) is None
        assert ledger.reserve_operation_intent(operation_key, "wallet") == intent

        totals = ledger.get_session_risk_totals("2026-05-01-live", "wallet")
        assert totals.quote_amount_raw_by_mint == {}
        assert totals.fee_lamports == 0
        assert totals.submission_count == 0


def test_submission_rejects_a_concurrently_replanned_intent(
    tmp_path: Path,
) -> None:
    original_hash = "a" * 64
    replacement_hash = "b" * 64
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        ledger.record_intent("buy", "wallet", 60, 5, original_hash)
        ledger.record_intent("buy", "wallet", 1, 1, replacement_hash)

        with pytest.raises(LedgerConflict, match="intent changed before submission"):
            ledger.record_submission(
                "buy",
                "stale-signature",
                "stale-blockhash",
                100,
                state="prepared",
                wire_bytes=b"stale-wire",
                quote_mint="SOL",
                risk_session_id="concurrent-session",
                max_session_quote_raw=100,
                max_session_fee_lamports=100,
                intent_message_hash=original_hash,
            )

        assert (
            ledger.get_session_risk_totals(
                "concurrent-session",
                "wallet",
            ).submission_count
            == 0
        )

        ledger.record_submission(
            "buy",
            "replacement-signature",
            "replacement-blockhash",
            100,
            state="prepared",
            wire_bytes=b"replacement-wire",
            quote_mint="SOL",
            risk_session_id="concurrent-session",
            max_session_quote_raw=100,
            max_session_fee_lamports=100,
            intent_message_hash=replacement_hash,
        )

        totals = ledger.get_session_risk_totals("concurrent-session", "wallet")
        assert totals.quote_amount_raw_by_mint == {"SOL": 1}
        assert totals.fee_lamports == 1


def test_operation_generation_reuses_only_nonterminal_work(tmp_path: Path) -> None:
    operation_key = "manual-cleanup:wallet:mint:account"
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        first_intent = ledger.reserve_operation_intent(operation_key, "wallet")
        assert ledger.reserve_operation_intent(operation_key, "wallet") == first_intent

        ledger.record_intent(first_intent, "wallet", 0, 5, "a" * 64)
        ledger.record_submission(
            first_intent,
            "first-signature",
            "first-blockhash",
            100,
            state="submitted",
            wire_bytes=b"first-wire",
        )
        assert ledger.reserve_operation_intent(operation_key, "wallet") == first_intent

        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.SUCCESS, "first-signature")
        )

    with TransactionLedger(path) as ledger:
        second_intent = ledger.reserve_operation_intent(operation_key, "wallet")
        assert second_intent != first_intent
        assert second_intent.endswith(":2")

    with TransactionLedger(path) as ledger:
        assert ledger.reserve_operation_intent(operation_key, "wallet") == second_intent


def test_concurrent_session_reservations_cannot_overspend(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path):
        pass

    barrier = Barrier(2)

    def reserve(index: int) -> str:
        with TransactionLedger(path) as ledger:
            intent = f"buy-{index}"
            signature = f"sig-{index}"
            ledger.record_intent(intent, "wallet", 60, 5, "a" * 64)
            barrier.wait()
            try:
                ledger.record_submission(
                    intent,
                    signature,
                    f"blockhash-{index}",
                    100,
                    state="prepared",
                    wire_bytes=f"wire-{index}".encode(),
                    quote_mint="SOL",
                    risk_session_id="concurrent-session",
                    max_session_quote_raw=100,
                    max_session_fee_lamports=100,
                    intent_message_hash="a" * 64,
                )
            except TradeLimitExceeded:
                return "blocked"
            return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (1, 2)))

    assert sorted(results) == ["blocked", "reserved"]
    with TransactionLedger(path) as ledger:
        totals = ledger.get_session_risk_totals(
            "concurrent-session",
            "wallet",
        )
    assert totals.quote_amount_raw_by_mint == {"SOL": 60}
    assert totals.submission_count == 1


def test_quote_budget_is_accounted_independently_per_mint(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _reserve_risk_submission(
            ledger,
            intent="sol-buy",
            signature="sol-sig",
            quote_mint="SOL",
            quote_amount_raw=60,
            fee_lamports=5,
        )
        _reserve_risk_submission(
            ledger,
            intent="usdc-buy",
            signature="usdc-sig",
            quote_mint="USDC",
            quote_amount_raw=60,
            fee_lamports=5,
        )

        totals = ledger.get_session_risk_totals("2026-05-01-live", "wallet")
        assert totals.quote_amount_raw_by_mint == {"SOL": 60, "USDC": 60}


def test_wallet_ledger_path_rejects_unreconciled_legacy_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    wallet = "11111111111111111111111111111111"
    shared = default_transaction_ledger_path(wallet)
    legacy = shared.with_name(f"{wallet}-pump_fun.sqlite3")
    with TransactionLedger(legacy):
        pass

    with pytest.raises(LedgerConflict, match="legacy platform-scoped"):
        resolve_transaction_ledger_path(wallet)

    assert not shared.exists()


def test_wallet_ledger_path_is_shared_when_no_legacy_ledgers_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    wallet = "11111111111111111111111111111111"

    assert resolve_transaction_ledger_path(wallet) == (
        Path(".state") / "transaction-ledgers" / f"{wallet}.sqlite3"
    )


def test_evidence_profiles_are_classified_and_content_addressed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    settings = {"strategy": "momentum", "limits": {"fee": 25, "quote": 100}}
    sources = {"trader.py": "a" * 64, "buyer.py": "b" * 64}
    with TransactionLedger(path) as ledger:
        profiles = {
            kind: ledger.record_evidence_profile(kind, settings, sources)
            for kind in ("live", "dry_run", "simulation", "paper")
        }
        assert len(set(profiles.values())) == 4
        assert (
            ledger.record_evidence_profile(
                "live",
                {"limits": {"quote": 100, "fee": 25}, "strategy": "momentum"},
                {"buyer.py": "b" * 64, "trader.py": "a" * 64},
            )
            == profiles["live"]
        )
        changed_settings = ledger.record_evidence_profile(
            "live", {**settings, "strategy": "other"}, sources
        )
        changed_sources = ledger.record_evidence_profile(
            "live", settings, {**sources, "trader.py": "c" * 64}
        )
        assert len({profiles["live"], changed_settings, changed_sources}) == 3
        with pytest.raises(ValueError):
            ledger.record_evidence_profile("backtest", settings, sources)
        with pytest.raises(ValueError):
            ledger.record_evidence_profile("live", {"value": float("nan")}, sources)
        with pytest.raises(TypeError):
            ledger.record_evidence_profile(
                "live", {"nested": {1: "ambiguous"}}, sources
            )

    with TransactionLedger(path) as reopened:
        rows = reopened.connection.execute("SELECT * FROM evidence_profiles").fetchall()
        assert len(rows) == 6
        for kind, profile_id in profiles.items():
            row = next(row for row in rows if row["profile_id"] == profile_id)
            assert row["kind"] == kind
            assert json.loads(row["settings_json"]) == settings
            assert json.loads(row["sources_json"]) == sources


def test_receipt_observations_are_immutable_and_deduplicate(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    first = {
        "slot": 10,
        "meta": {"err": None, "fee": 5000},
        "transaction": {"signatures": ["sig"]},
    }
    changed = {**first, "slot": 11}
    with TransactionLedger(path) as ledger:
        observer = ledger.record_evidence_profile("live", {"strategy": "current"}, {})
        first_id = ledger.record_receipt_evidence("sig", "confirmed", first)
        original = dict(
            ledger.connection.execute(
                "SELECT * FROM evidence_receipts WHERE receipt_id = ?", (first_id,)
            ).fetchone()
        )
        assert (
            ledger.record_receipt_evidence(
                "sig",
                "confirmed",
                {
                    "transaction": first["transaction"],
                    "meta": first["meta"],
                    "slot": 10,
                },
            )
            == first_id
        )
        changed_id = ledger.record_receipt_evidence("sig", "confirmed", changed)
        finalized_id = ledger.record_receipt_evidence("sig", "finalized", changed)
        attributed_id = ledger.record_receipt_evidence(
            "sig", "confirmed", first, profile_id=observer
        )
        assert len({first_id, changed_id, finalized_id, attributed_id}) == 4

    with TransactionLedger(path) as reopened:
        rows = {
            row["receipt_id"]: dict(row)
            for row in reopened.connection.execute("SELECT * FROM evidence_receipts")
        }
        assert rows[first_id] == original
        assert json.loads(rows[changed_id]["payload_json"]) == changed
        assert rows[finalized_id]["commitment"] == "finalized"
        assert rows[attributed_id]["profile_id"] == observer
        assert rows[first_id]["profile_id"] is None
        assert all(row["observed_fee_lamports"] == "5000" for row in rows.values())


@pytest.mark.parametrize(
    ("meta", "expected_fee"),
    [
        (None, None),
        ({}, None),
        ({"fee": True}, None),
        ({"fee": "5000"}, None),
        ({"fee": 1.5}, None),
        ({"fee": -1}, None),
        ({"fee": 2**64}, None),
        ({"fee": 0}, "0"),
        ({"fee": 2**64 - 1}, str(2**64 - 1)),
        ({"err": {"InstructionError": [0, "Custom"]}}, None),
        ({"err": {"InstructionError": [0, "Custom"]}, "fee": 5000}, "5000"),
    ],
)
def test_receipt_fee_is_observed_or_explicitly_unknown(
    tmp_path: Path, meta: dict | None, expected_fee: str | None
) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, signature="sig")
        receipt = {"slot": 1, "meta": meta}
        receipt_id = ledger.record_receipt_evidence("sig", "confirmed", receipt)
        row = ledger.connection.execute(
            "SELECT observed_fee_lamports, payload_json FROM evidence_receipts "
            "WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        assert row["observed_fee_lamports"] == expected_fee
        assert json.loads(row["payload_json"]) == receipt
        assert ledger.get_latest_submission_record("intent").fee_lamports == 5


@pytest.mark.parametrize("original_known", [False, True])
def test_submission_profile_survives_restart_and_reuse(
    tmp_path: Path, *, original_known: bool
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        original = (
            ledger.record_evidence_profile("live", {"strategy": "original"}, {})
            if original_known
            else None
        )
        _record_submission(
            ledger,
            state="prepared",
            wire_bytes=b"original",
            evidence_profile_id=original,
        )
    with TransactionLedger(path) as reopened:
        current = reopened.record_evidence_profile("live", {"strategy": "current"}, {})
        assert (
            _record_submission(
                reopened,
                signature="sig-2",
                state="prepared",
                wire_bytes=b"other",
                evidence_profile_id=current,
            )
            == "sig-1"
        )
        assert (
            _record_submission(
                reopened,
                state="submitted",
                wire_bytes=b"original",
                evidence_profile_id=current,
            )
            == "sig-1"
        )
        assert (
            reopened.get_active_submission_record("intent").evidence_profile_id
            == original
        )
        assert (
            reopened.get_latest_submission_record("intent").evidence_profile_id
            == original
        )
        assert reopened.list_recoverable()[0].evidence_profile_id == original


def test_trade_evidence_retains_changed_observations(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        first = ledger.record_trade_evidence(None, "decision", {"action": "skip"})
        assert (
            ledger.record_trade_evidence(None, "decision", {"action": "skip"}) == first
        )
        changed = ledger.record_trade_evidence(None, "decision", {"action": "buy"})
        assert changed != first
        rows = ledger.connection.execute(
            "SELECT profile_id, payload_json FROM evidence_events ORDER BY rowid"
        ).fetchall()
        assert [
            (row["profile_id"], json.loads(row["payload_json"])) for row in rows
        ] == [(None, {"action": "skip"}), (None, {"action": "buy"})]


@pytest.mark.parametrize(
    "operation", ["profile", "trade", "receipt", "submission", "release"]
)
def test_evidence_database_errors_fail_closed(tmp_path: Path, operation: str) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _reserve_risk_submission(
            ledger,
            intent="intent",
            signature="sig-1",
            quote_amount_raw=100,
            fee_lamports=25,
        )
        ledger.record_intent("new-intent", "wallet", 10, 5, "a" * 64)
        ledger.connection.execute("PRAGMA query_only=ON")
        with pytest.raises(EvidencePersistenceError) as failure:
            if operation == "profile":
                ledger.record_evidence_profile("live", {}, {})
            elif operation == "trade":
                ledger.record_trade_evidence(None, "decision", {})
            elif operation == "receipt":
                ledger.record_receipt_evidence(
                    "sig-1", "confirmed", {"meta": {"fee": 5000}}
                )
            elif operation == "submission":
                ledger.record_submission("new-intent", "sig-2", "blockhash", 100)
            else:
                ledger.release_prepared_submission("sig-1")
        assert isinstance(failure.value.__cause__, sqlite3.Error)
        assert ledger.get_active_submission("intent") == "sig-1"
        assert ledger.get_active_submission("new-intent") is None
        assert (
            ledger.get_session_risk_totals("2026-05-01-live", "wallet").fee_lamports
            == 25
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM evidence_events"
            ).fetchone()[0]
            == 0
        )
        assert not ledger.connection.in_transaction


def test_release_rolls_back_evidence_if_deletion_fails(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        _record_submission(ledger, state="prepared", wire_bytes=b"wire")
        ledger.connection.execute(
            """
            CREATE TRIGGER deny_release BEFORE DELETE ON submissions
            BEGIN SELECT RAISE(ABORT, 'release blocked'); END
            """
        )
        with pytest.raises(EvidencePersistenceError) as failure:
            ledger.release_prepared_submission("sig-1")
        assert isinstance(failure.value.__cause__, sqlite3.Error)
        assert ledger.get_active_submission("intent") == "sig-1"
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM evidence_events"
            ).fetchone()[0]
            == 0
        )


def test_unknown_evidence_profile_rejects_new_submission(tmp_path: Path) -> None:
    with TransactionLedger(tmp_path / "ledger.sqlite") as ledger:
        with pytest.raises(EvidencePersistenceError) as failure:
            _record_submission(ledger, evidence_profile_id="unregistered")
        assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
        assert ledger.get_active_submission("intent") is None
