# Assertions and private corruption fixtures exercise the audit trust boundary.
# ruff: noqa: S101, PLR2004

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from solders.pubkey import Pubkey
from solders.signature import Signature

from core.transaction_ledger import TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus

if TYPE_CHECKING:
    from types import ModuleType

AUDITOR = (
    Path(__file__).resolve().parents[1]
    / "learning-examples/token-lifecycles/summarize_trade_evidence.py"
)
PAYER = str(Pubkey.from_bytes(bytes([1]) * 32))
OTHER = str(Pubkey.from_bytes(bytes([2]) * 32))
BLOCKHASH = str(Pubkey.from_bytes(bytes([3]) * 32))
SENTINEL = "SYNTHETIC_AUDIT_CREDENTIAL_MUST_NOT_APPEAR"


@pytest.fixture
def auditor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("trade_evidence_auditor", AUDITOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signature(number: int) -> str:
    return str(Signature.from_bytes(bytes([number]) * 64))


def _profile(
    ledger: TransactionLedger, kind: str = "live", run: str = "original"
) -> str:
    return ledger.record_evidence_profile(
        kind,
        {"run": run, "credential": SENTINEL, "fee_budget_lamports": 999_999},
        {"not-a-path-to-read.py": "a" * 64},
    )


def _reserve(ledger: TransactionLedger, number: int, profile: str | None) -> str:
    signature = _signature(number)
    intent = f"intent-{number}"
    ledger.record_intent(intent, PAYER, 100_000, 999_999, "b" * 64)
    ledger.record_submission(
        intent, signature, BLOCKHASH, 500, evidence_profile_id=profile
    )
    return signature


def _receipt(signature: str, fee: int = 5_000, *, reverted: bool = False) -> dict:
    return {
        "slot": 42,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": [PAYER, OTHER],
                "recentBlockhash": BLOCKHASH,
                "instructions": [],
                "addressTableLookups": [],
            },
        },
        "meta": {
            "err": {"InstructionError": [0, {"Custom": 1}]} if reverted else None,
            "fee": fee,
            "preBalances": [100_000, 0],
            "postBalances": [100_000 - fee, 0],
            "preTokenBalances": [],
            "postTokenBalances": [],
            "loadedAddresses": {"writable": [], "readonly": []},
        },
    }


def _observe(
    ledger: TransactionLedger,
    signature: str,
    profile: str | None,
    result: dict,
    commitment: str = "confirmed",
) -> str:
    return ledger.record_receipt_evidence(
        signature, commitment, result, profile_id=profile
    )


def _transactions(report: dict) -> dict[str, dict]:
    return {row["signature"]: row for row in report["transactions"]}


def _cli(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and local script; no shell
        [sys.executable, str(AUDITOR), str(path)],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_repeated_observers_and_finalization_count_actual_fees_once(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        original = _profile(ledger)
        observer = _profile(ledger, run="later")
        success = _reserve(ledger, 1, original)
        reverted = _reserve(ledger, 2, original)
        ledger.record_outcome(TransactionOutcome(TransactionStatus.UNKNOWN, success))
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.REVERTED, reverted, slot=42)
        )
        for signature, fee, failed in [
            (success, 5_000, False),
            (reverted, 6_000, True),
        ]:
            result = _receipt(signature, fee, reverted=failed)
            _observe(ledger, signature, original, result)
            _observe(ledger, signature, original, deepcopy(result))
            result["blockTime"] += 1
            _observe(ledger, signature, observer, result)
            _observe(ledger, signature, observer, result, "finalized")

    report = auditor.summarize(path)
    rows = _transactions(report)
    assert rows[success]["ledger_status"] == "unknown"
    assert rows[success]["receipt_status"] == "success"
    assert rows[reverted]["receipt_status"] == "reverted"
    assert [rows[s]["observed_network_fee_lamports"] for s in (success, reverted)] == [
        5_000,
        6_000,
    ]
    assert all(row["submission_profile_id"] == original for row in rows.values())
    assert all(row["fee_commitment"] == "finalized" for row in rows.values())
    fees = report["live_network_fees"]
    assert fees["submission_count"] == fees["observed_count"] == 2
    assert fees["unknown_count"] == 0
    assert fees["known_subtotal_lamports"] == 11_000
    assert fees["finalized_subtotal_lamports"] == 11_000
    assert fees["confirmed_subtotal_lamports"] == 0
    assert fees["complete_finalized_total_lamports"] == 11_000
    assert SENTINEL not in json.dumps(report)


def test_missing_metadata_and_boolean_fee_are_unknown_but_zero_is_observed(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        missing = _reserve(ledger, 1, profile)
        boolean = _reserve(ledger, 2, profile)
        zero = _reserve(ledger, 3, profile)
        missing_result = _receipt(missing)
        missing_result["meta"] = None
        boolean_result = _receipt(boolean)
        boolean_result["meta"]["fee"] = True
        for signature, result in [
            (missing, missing_result),
            (boolean, boolean_result),
            (zero, _receipt(zero, 0)),
        ]:
            _observe(ledger, signature, profile, result, "finalized")

    report = auditor.summarize(path)
    rows = _transactions(report)
    assert rows[missing]["observed_network_fee_lamports"] is None
    assert rows[missing]["receipt_status"] is None
    assert rows[boolean]["observed_network_fee_lamports"] is None
    assert rows[zero]["observed_network_fee_lamports"] == 0
    assert rows[zero]["fee_commitment"] == "finalized"
    assert report["live_network_fees"]["observed_count"] == 1
    assert report["live_network_fees"]["unknown_count"] == 2
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_finalized_receipt_without_fee_cannot_promote_confirmed_fee(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _receipt(signature)
        _observe(ledger, signature, profile, result)
        del result["meta"]["fee"]
        _observe(ledger, signature, profile, result, "finalized")

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["receipt_status"] == "success"
    assert row["observed_network_fee_lamports"] == 5_000
    assert row["fee_commitment"] == "confirmed"
    assert report["live_network_fees"]["confirmed_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["finalized_subtotal_lamports"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None

    with TransactionLedger(path) as ledger:
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
    reconciled = auditor.summarize(path)
    assert reconciled["live_network_fees"]["complete_finalized_total_lamports"] == 5_000


@pytest.mark.parametrize("disagreement", ["status", "slot", "payer", "fee", "balances"])
def test_conflicting_chain_or_ledger_evidence_withholds_fee(
    tmp_path: Path, auditor: ModuleType, disagreement: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _receipt(signature)
        if disagreement == "status":
            ledger.record_outcome(
                TransactionOutcome(TransactionStatus.REVERTED, signature, slot=42)
            )
        elif disagreement == "slot":
            ledger.record_outcome(
                TransactionOutcome(TransactionStatus.SUCCESS, signature, slot=43)
            )
        elif disagreement == "payer":
            result["transaction"]["message"]["accountKeys"][0] = OTHER
        elif disagreement == "fee":
            _observe(ledger, signature, profile, result)
            result["meta"]["fee"] += 1
        else:
            _observe(ledger, signature, profile, result)
            result["meta"]["postBalances"][0] -= 1
        _observe(ledger, signature, profile, result, "finalized")

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["issues"]
    assert report["live_network_fees"]["observed_count"] == 0
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_newly_available_fields_are_not_conflicts_but_message_changes_are(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        full = _receipt(signature)
        partial = deepcopy(full)
        del partial["meta"]["preTokenBalances"]
        del partial["transaction"]["message"]["addressTableLookups"]
        _observe(ledger, signature, profile, partial)
        _observe(ledger, signature, profile, full, "finalized")
        report = auditor.summarize(path)
        assert (
            _transactions(report)[signature]["observed_network_fee_lamports"] == 5_000
        )
        changed = deepcopy(full)
        changed["transaction"]["message"]["recentBlockhash"] = OTHER
        _observe(ledger, signature, profile, changed, "finalized")

    report = auditor.summarize(path)
    assert _transactions(report)[signature]["observed_network_fee_lamports"] is None
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0


@pytest.mark.parametrize(
    "corruption", ["hash", "duplicate_json", "nonfinite_json", "fee_column"]
)
def test_one_corrupt_live_observation_cannot_hide_behind_a_valid_receipt(
    tmp_path: Path, auditor: ModuleType, corruption: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _receipt(signature)
        _observe(ledger, signature, profile, result)
        receipt_id = _observe(ledger, signature, profile, result, "finalized")
        if corruption == "fee_column":
            ledger.connection.execute(
                "UPDATE evidence_receipts SET observed_fee_lamports = '1' WHERE receipt_id = ?",
                (receipt_id,),
            )
        else:
            if corruption == "hash":
                result["blockTime"] += 1
                payload = json.dumps(result)
            elif corruption == "duplicate_json":
                payload = json.dumps(result).replace(
                    '"slot": 42', '"slot": 41, "slot": 42'
                )
            else:
                payload = json.dumps(result).replace(
                    '"blockTime": 1700000000', '"blockTime": NaN'
                )
            ledger.connection.execute(
                "UPDATE evidence_receipts SET payload_json = ? WHERE receipt_id = ?",
                (payload, receipt_id),
            )
        ledger.connection.commit()

    report = auditor.summarize(path)
    assert _transactions(report)[signature]["observed_network_fee_lamports"] is None
    assert receipt_id in {row["receipt_id"] for row in report["receipt_issues"]}
    assert report["live_network_fees"]["observed_count"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


@pytest.mark.parametrize("corruption", ["profile_hash", "dangling_profile"])
def test_invalid_original_profile_is_not_replaced_by_valid_observer(
    tmp_path: Path, auditor: ModuleType, corruption: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        original = _profile(ledger)
        observer = _profile(ledger, run="observer")
        signature = _reserve(ledger, 1, original)
        _observe(ledger, signature, observer, _receipt(signature), "finalized")
    with closing(sqlite3.connect(path)) as connection, connection:
        if corruption == "profile_hash":
            connection.execute(
                "UPDATE evidence_profiles SET settings_json = '{}' WHERE profile_id = ?",
                (original,),
            )
        else:
            connection.execute(
                "DELETE FROM evidence_profiles WHERE profile_id = ?", (original,)
            )

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["submission_profile_id"] == original
    assert row["kind"] == "invalid"
    assert row["issues"]
    assert report["unattributed_submission_count"] == 1
    assert report["live_network_fees"]["submission_count"] == 0
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_originally_unattributed_stays_unattributed_despite_live_observer(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        signature = _reserve(ledger, 1, None)
        observer = _profile(ledger, run="observer")
        _observe(ledger, signature, observer, _receipt(signature), "finalized")
        live = _reserve(ledger, 2, observer)
        _observe(ledger, live, observer, _receipt(live), "finalized")

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["kind"] == "unattributed"
    assert row["submission_profile_id"] is None
    assert row["observed_network_fee_lamports"] == 5_000
    assert report["unattributed_submission_count"] == 1
    assert report["live_network_fees"]["submission_count"] == 1
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_practice_profiles_and_events_never_inflate_live_fees(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    kinds = ("live", "paper", "simulation", "dry_run")
    with TransactionLedger(path) as ledger:
        profiles = {kind: _profile(ledger, kind) for kind in kinds}
        signatures = {}
        for number, kind in enumerate(kinds, start=1):
            signature = signatures[kind] = _reserve(ledger, number, profiles[kind])
            _observe(
                ledger, signature, profiles[kind], _receipt(signature), "finalized"
            )
            payload = {"result": {"tx_signature": signature, "success": True}}
            ledger.record_trade_evidence(profiles[kind], "trade_result", payload)
            ledger.record_trade_evidence(profiles[kind], "trade_result", payload)
        contaminated = _receipt(signatures["live"], 99_999)
        practice_receipt = _observe(
            ledger, signatures["live"], profiles["paper"], contaminated, "finalized"
        )

    report = auditor.summarize(path)
    assert {s: row["kind"] for s, row in _transactions(report).items()} == {
        signature: kind for kind, signature in signatures.items()
    }
    assert all(report["events_by_kind"][kind]["trade_result"] == 1 for kind in kinds)
    assert report["live_network_fees"]["submission_count"] == 1
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
    assert practice_receipt in {row["receipt_id"] for row in report["receipt_issues"]}


@pytest.mark.parametrize("observer_kind", ["absent", "dangling", "corrupt"])
def test_unknown_or_corrupt_receipt_observer_cannot_supply_live_fee(
    tmp_path: Path, auditor: ModuleType, observer_kind: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        original = _profile(ledger)
        observer = (
            None if observer_kind == "absent" else _profile(ledger, run="observer")
        )
        signature = _reserve(ledger, 1, original)
        receipt_id = _observe(
            ledger, signature, observer, _receipt(signature), "finalized"
        )
    with closing(sqlite3.connect(path)) as connection, connection:
        if observer_kind == "dangling":
            connection.execute(
                "DELETE FROM evidence_profiles WHERE profile_id = ?", (observer,)
            )
        elif observer_kind == "corrupt":
            connection.execute(
                "UPDATE evidence_profiles SET sources_json = '{}' WHERE profile_id = ?",
                (observer,),
            )

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["kind"] == "live"
    assert row["observed_network_fee_lamports"] is None
    assert receipt_id in {row["receipt_id"] for row in report["receipt_issues"]}
    assert report["live_network_fees"]["unknown_count"] == 1


def test_orphan_receipts_and_untracked_event_signatures_are_exposed(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
        orphan = _signature(2)
        orphan_id = _observe(ledger, orphan, profile, _receipt(orphan), "finalized")
        event_ids = {
            ledger.record_trade_evidence(profile, category, payload)
            for category, payload in [
                ("trade_result", {"result": {"tx_signature": orphan, "success": True}}),
                (
                    "chain_outcome",
                    {"signature": orphan, "status": "success", "slot": 42},
                ),
                ("position_closed", {"signature": orphan, "action": "sell"}),
            ]
        }

    report = auditor.summarize(path)
    assert orphan_id in {row["receipt_id"] for row in report["receipt_issues"]}
    assert event_ids <= {row["event_id"] for row in report["event_issues"]}
    assert orphan not in _transactions(report)
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_tampered_and_dangling_events_are_not_valid_observations(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        live = _profile(ledger)
        dangling = _profile(ledger, "paper")
        tampered_id = ledger.record_trade_evidence(live, "decision", {"action": "skip"})
        dangling_id = ledger.record_trade_evidence(
            dangling, "decision", {"action": "skip"}
        )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE evidence_events SET payload_json = ? WHERE event_id = ?",
            ('{"action":"buy"}', tampered_id),
        )
        connection.execute(
            "DELETE FROM evidence_profiles WHERE profile_id = ?", (dangling,)
        )

    report = auditor.summarize(path)
    assert {tampered_id, dangling_id} <= {
        row["event_id"] for row in report["event_issues"]
    }
    assert report["events_by_kind"].get("live", {}).get("decision", 0) == 0
    assert report["events_by_kind"].get("paper", {}).get("decision", 0) == 0


def test_cli_keeps_legacy_schema_and_data_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    signature = _signature(1)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE intents (intent_id TEXT PRIMARY KEY, signer TEXT, "
            "quote_amount_raw TEXT, fee_lamports TEXT, message_hash TEXT);"
            "CREATE TABLE submissions (signature TEXT PRIMARY KEY, intent_id TEXT, "
            "blockhash TEXT, last_valid_block_height INTEGER);"
            "CREATE TABLE outcomes (signature TEXT PRIMARY KEY, status TEXT, error TEXT, slot INTEGER);"
        )
        connection.execute(
            "INSERT INTO intents VALUES (?, ?, ?, ?, ?)",
            ("legacy", PAYER, "100", "999999", "a" * 64),
        )
        connection.execute(
            "INSERT INTO submissions VALUES (?, ?, ?, ?)",
            (signature, "legacy", BLOCKHASH, 500),
        )
        connection.execute(
            "INSERT INTO outcomes VALUES (?, 'success', NULL, 42)", (signature,)
        )
    before_bytes = path.read_bytes()
    result = _cli(path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report["missing_evidence_tables"]) == {
        "evidence_profiles",
        "evidence_events",
        "evidence_receipts",
    }
    row = _transactions(report)[signature]
    assert row["kind"] == "unattributed"
    assert row["submission_state"] is None
    assert row["observed_network_fee_lamports"] is None
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None
    assert path.read_bytes() == before_bytes
    with closing(sqlite3.connect(path)) as connection:
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        } == {"intents", "submissions", "outcomes"}


def test_cli_missing_file_is_not_created_and_invalid_database_is_an_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite"
    result = _cli(missing)
    assert result.returncode != 0
    assert not missing.exists()
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_bytes(b"not a SQLite database")
    result = _cli(invalid)
    assert result.returncode != 0
    assert invalid.read_bytes() == b"not a SQLite database"
    incomplete = tmp_path / "incomplete.sqlite"
    with closing(sqlite3.connect(incomplete)) as connection, connection:
        connection.execute("CREATE TABLE intents (intent_id TEXT)")
    assert _cli(incomplete).returncode != 0


def test_cli_reads_committed_wal_without_mutating_schema_or_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wal ledger #1.sqlite"
    with TransactionLedger(path) as ledger:
        ledger.connection.execute("PRAGMA wal_autocheckpoint=0")
        ledger.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
        assert path.with_name(path.name + "-wal").stat().st_size > 0
        before_dump = tuple(ledger.connection.iterdump())
        before_database = path.read_bytes()
        before_wal = path.with_name(path.name + "-wal").read_bytes()
        result = _cli(path)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert (
            _transactions(report)[signature]["observed_network_fee_lamports"] == 5_000
        )
        assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
        assert SENTINEL not in result.stdout
        assert tuple(ledger.connection.iterdump()) == before_dump
        assert path.read_bytes() == before_database
        assert path.with_name(path.name + "-wal").read_bytes() == before_wal


@pytest.mark.parametrize(
    "malformation",
    [
        "signature_mismatch",
        "invalid_signature",
        "invalid_account",
        "invalid_slot",
        "missing_error",
    ],
)
def test_hash_valid_but_noncanonical_receipt_cannot_supply_fee(
    tmp_path: Path, auditor: ModuleType, malformation: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _receipt(signature)
        if malformation == "signature_mismatch":
            result["transaction"]["signatures"] = [_signature(2)]
        elif malformation == "invalid_signature":
            result["transaction"]["signatures"].append("not-a-signature")
        elif malformation == "invalid_account":
            result["transaction"]["message"]["accountKeys"][1] = "not-a-pubkey"
        elif malformation == "invalid_slot":
            result["slot"] = True
        else:
            del result["meta"]["err"]
        receipt_id = _observe(ledger, signature, profile, result, "finalized")

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["receipt_status"] is None
    assert row["observed_network_fee_lamports"] is None
    assert receipt_id in {entry["receipt_id"] for entry in report["receipt_issues"]}
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_missing_intent_keeps_fee_unknown_without_aborting_audit(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM intents")

    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["signer"] is None
    assert "missing_submission_intent" in row["issues"]
    assert row["observed_network_fee_lamports"] is None
    assert report["live_network_fees"]["unknown_count"] == 1
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_success_claim_cannot_hide_a_reverted_receipt(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        _observe(
            ledger, signature, profile, _receipt(signature, reverted=True), "finalized"
        )
        event_id = ledger.record_trade_evidence(
            profile,
            "trade_result",
            {
                "action": "buy",
                "intent_id": "intent-1",
                "result": {
                    "success": True,
                    "status": "success",
                    "tx_signature": signature,
                },
            },
        )

    report = auditor.summarize(path)
    assert any(
        row["event_id"] == event_id and "event_receipt_status_conflict" in row["issues"]
        for row in report["event_issues"]
    )
    # A false success claim does not erase the actual network fee of the revert.
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_uncertain_and_unaccounted_observations_allow_later_success(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        ledger.record_trade_evidence(
            profile,
            "chain_outcome",
            {"signature": signature, "status": "unknown", "slot": 1},
        )
        for status in ("unknown", "failed", "success"):
            # Local failure/unavailable accounting is not a claim of chain revert.
            ledger.record_trade_evidence(
                profile,
                "trade_result",
                {
                    "result": {
                        "tx_signature": signature,
                        "success": False,
                        "status": status,
                    }
                },
            )
        ledger.record_trade_evidence(
            profile,
            "decision",
            {"action": "buy", "intent_id": "never-submitted", "gate": None},
        )
        ledger.record_trade_evidence(
            profile,
            "trade_result",
            {
                "intent_id": "never-submitted",
                "result": {"success": False, "status": "failed"},
            },
        )
        _observe(ledger, signature, profile, _receipt(signature), "finalized")

    report = auditor.summarize(path)
    assert not report["event_issues"]
    assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000


def test_terminal_event_requires_receipt_and_matching_slot(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        event_id = ledger.record_trade_evidence(
            profile,
            "chain_outcome",
            {"signature": signature, "status": "success", "slot": 42},
        )
        report = auditor.summarize(path)
        assert "event_receipt_unavailable" in report["event_issues"][0]["issues"]
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
        assert not auditor.summarize(path)["event_issues"]
        wrong_slot = ledger.record_trade_evidence(
            profile,
            "chain_outcome",
            {"signature": signature, "status": "success", "slot": 43},
        )
        expired = ledger.record_trade_evidence(
            profile, "chain_outcome", {"signature": signature, "status": "expired"}
        )

    issues = {
        row["event_id"]: row["issues"]
        for row in auditor.summarize(path)["event_issues"]
    }
    assert event_id not in issues
    assert "event_receipt_slot_conflict" in issues[wrong_slot]
    assert "event_receipt_status_conflict" in issues[expired]


@pytest.mark.parametrize(
    ("disagreement", "expected_issue"),
    [
        ("none", None),
        ("missing_entry", "untracked_position_entry"),
        ("entry_reverted", "position_entry_receipt_status_conflict"),
        ("wallet", "position_signer_mismatch"),
        ("intent", "event_intent_mismatch"),
        ("pending_signature", "position_exit_signature_mismatch"),
        ("same_signature", "position_entry_exit_same_signature"),
    ],
)
def test_closure_checks_entry_exit_relationships(
    tmp_path: Path, auditor: ModuleType, disagreement: str, expected_issue: str | None
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        original = _profile(ledger)
        observer = _profile(ledger, run="recovery")
        entry = _reserve(ledger, 1, original)
        exit_signature = _reserve(ledger, 2, observer)
        entry_receipt = _receipt(entry, reverted=disagreement == "entry_reverted")
        if disagreement == "wallet":
            ledger.connection.execute(
                "UPDATE intents SET signer = ? WHERE intent_id = ?", (OTHER, "intent-1")
            )
            ledger.connection.commit()
            entry_receipt["transaction"]["message"]["accountKeys"] = [OTHER, PAYER]
        _observe(ledger, entry, observer, entry_receipt, "finalized")
        _observe(
            ledger, exit_signature, observer, _receipt(exit_signature), "finalized"
        )
        position = {
            "position_id": entry,
            "is_active": True,  # Producer records evidence before local removal.
            "pending_exit_intent_id": "intent-2",
            "pending_exit_signature": None,  # Immediate confirmed exit, not recovery.
        }
        if disagreement == "missing_entry":
            position["position_id"] = _signature(3)
        elif disagreement == "intent":
            position["pending_exit_intent_id"] = "intent-1"
        elif disagreement == "pending_signature":
            position["pending_exit_signature"] = entry
        elif disagreement == "same_signature":
            position["position_id"] = exit_signature
        event_id = ledger.record_trade_evidence(
            observer,
            "position_closed",
            {"action": "sell", "signature": exit_signature, "position": position},
        )

    report = auditor.summarize(path)
    issues = {row["event_id"]: row["issues"] for row in report["event_issues"]}
    assert report["live_network_fees"]["known_subtotal_lamports"] == 10_000
    if expected_issue is None:
        assert event_id not in issues
        assert (
            report["live_network_fees"]["complete_finalized_total_lamports"] == 10_000
        )
    else:
        assert expected_issue in issues[event_id]
        assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_live_claims_cannot_promote_practice_submissions(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        live = _profile(ledger)
        paper = _profile(ledger, "paper")
        signature = _reserve(ledger, 1, paper)
        _observe(ledger, signature, live, _receipt(signature), "finalized")
        decision = ledger.record_trade_evidence(
            live, "decision", {"action": "buy", "intent_id": "intent-1"}
        )
        result = ledger.record_trade_evidence(
            live,
            "trade_result",
            {"result": {"success": True, "tx_signature": signature}},
        )
    report = auditor.summarize(path)
    issues = {row["event_id"]: row["issues"] for row in report["event_issues"]}
    assert "decision_submission_not_live" in issues[decision]
    assert "event_submission_not_live" in issues[result]
    assert report["live_network_fees"]["submission_count"] == 0


@pytest.mark.parametrize(
    ("category", "payload", "expected_issue"),
    [
        (
            "decision",
            {"action": "buy", "intent_id": "unsubmitted", "gate": {"accept": False}},
            "decision_gate_action_conflict",
        ),
        ("trade_result", {"result": {"success": True}}, "missing_event_signature"),
        ("trade_result", {"result": {"success": "yes"}}, "invalid_event_success"),
        (
            "trade_result",
            {"result": {"success": False, "status": "failed"}, "position": []},
            "invalid_event_position",
        ),
    ],
)
def test_hash_valid_but_invalid_live_claim_is_reported(
    tmp_path: Path,
    auditor: ModuleType,
    category: str,
    payload: dict,
    expected_issue: str,
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        event_id = ledger.record_trade_evidence(profile, category, payload)
    report = auditor.summarize(path)
    assert any(
        row["event_id"] == event_id and expected_issue in row["issues"]
        for row in report["event_issues"]
    )
