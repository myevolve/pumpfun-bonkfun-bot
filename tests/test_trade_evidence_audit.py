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
from spl.token.constants import TOKEN_2022_PROGRAM_ID

from core.transaction_ledger import TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus

if TYPE_CHECKING:
    from types import ModuleType

AUDITOR = Path(__file__).resolve().parents[1] / "src/learning/trade_evidence.py"
PAYER = str(Pubkey.from_bytes(bytes([1]) * 32))
OTHER = str(Pubkey.from_bytes(bytes([2]) * 32))
BLOCKHASH = str(Pubkey.from_bytes(bytes([3]) * 32))
TOKEN_ACCOUNT = str(Pubkey.from_bytes(bytes([4]) * 32))
MINT = str(Pubkey.from_bytes(bytes([5]) * 32))
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


def _token_receipt(signature: str) -> dict:
    result = _receipt(signature)
    result["transaction"]["message"]["addressTableLookups"] = [
        {"accountKey": BLOCKHASH, "writableIndexes": [0], "readonlyIndexes": [1]}
    ]
    result["meta"].update(
        {
            "preBalances": [1_000_000_000, 100_000_000, 0, 1_000_000],
            "postBalances": [987_955_720, 110_000_000, 2_039_280, 1_000_000],
            "loadedAddresses": {"writable": [TOKEN_ACCOUNT], "readonly": [MINT]},
            "postTokenBalances": [
                {
                    "accountIndex": 2,
                    "mint": MINT,
                    "owner": PAYER,
                    "programId": str(TOKEN_2022_PROGRAM_ID),
                    "uiTokenAmount": {
                        "amount": "25000000",
                        "decimals": 6,
                        "uiAmount": 25.0,
                    },
                }
            ],
        }
    )
    return result


def _transfer_receipt(signature: str, encoding: str, *, lookup: bool = True) -> dict:
    """Encode the same unsigned synthetic System transfer in RPC JSON formats."""
    result = _receipt(signature)
    program = str(Pubkey.default())
    keys = [PAYER, program, OTHER] if lookup else [PAYER, OTHER, program]
    program_index, destination_index = (1, 2) if lookup else (2, 1)
    message, meta = result["transaction"]["message"], result["meta"]
    message["accountKeys"] = keys[:2] if lookup else keys
    message["header"] = {
        "numRequiredSignatures": 1,
        "numReadonlySignedAccounts": 0,
        "numReadonlyUnsignedAccounts": 1,
    }
    meta["preBalances"] = [100_000, 0, 0]
    meta["postBalances"] = [85_000, 0, 0]
    meta["postBalances"][destination_index] = 10_000
    if lookup:
        message["addressTableLookups"] = [
            {"accountKey": BLOCKHASH, "writableIndexes": [0], "readonlyIndexes": []}
        ]
        meta["loadedAddresses"] = {"writable": [OTHER], "readonly": []}
    # System transfer discriminator (u32=2) and lamports (u64=10_000), base58.
    message["instructions"] = [
        {
            "programIdIndex": program_index,
            "accounts": [0, destination_index],
            "data": "3Bxs43ZMjSRQLs6o",
            "stackHeight": None,
        }
    ]
    if encoding.startswith("raw"):
        if encoding == "raw_unresolved":
            del meta["loadedAddresses"]
        return result
    message["accountKeys"] = [
        {"pubkey": key, "signer": index == 0, "writable": index != program_index}
        for index, key in enumerate(keys)
    ]
    del message["header"]
    del meta["loadedAddresses"]
    instruction = {"programId": program, "stackHeight": 1}
    if encoding == "partial":
        instruction.update(accounts=[PAYER, OTHER], data="3Bxs43ZMjSRQLs6o")
    else:
        instruction.update(
            program="system",
            parsed={
                "type": "transfer",
                "info": {"source": PAYER, "destination": OTHER, "lamports": 10_000},
            },
        )
    message["instructions"] = [instruction]
    return result


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


def _cli(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and local script; no shell
        [sys.executable, "-m", "learning.trade_evidence", str(path), *args],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _fee_blocker_codes(report: dict) -> set[str]:
    return {
        blocker["code"]
        for blocker in report["live_network_fees"]["finalized_total_blockers"]
    }


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
    assert not _fee_blocker_codes(report)
    assert SENTINEL not in json.dumps(report)
    reverted_native = rows[reverted]["economic_receipt"]["native"]
    assert reverted_native["change_lamports"] == -6_000
    assert reverted_native["change_excluding_network_fee_lamports"] == 0


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
    assert _fee_blocker_codes(report) == {
        "unknown_live_fees",
        "receipt_integrity_issues",
    }
    assert (
        rows[boolean]["economic_receipt"]["native"][
            "change_excluding_network_fee_lamports"
        ]
        is None
    )


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
    assert _fee_blocker_codes(report) == {"unfinalized_live_fees"}

    with TransactionLedger(path) as ledger:
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
    reconciled = auditor.summarize(path)
    assert reconciled["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
    assert not _fee_blocker_codes(reconciled)


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
    assert row["economic_receipt"] is None
    assert row["issues"]
    assert report["live_network_fees"]["observed_count"] == 0
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


@pytest.mark.parametrize("message_field", ["recentBlockhash", "header"])
def test_newly_available_fields_are_not_conflicts_but_message_changes_are(
    tmp_path: Path, auditor: ModuleType, message_field: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        full = _receipt(signature)
        full["transaction"]["message"]["header"] = {
            "numRequiredSignatures": 1,
            "numReadonlySignedAccounts": 0,
            "numReadonlyUnsignedAccounts": 0,
        }
        partial = deepcopy(full)
        del partial["meta"]["preTokenBalances"]
        del partial["transaction"]["message"]["addressTableLookups"]
        del partial["transaction"]["message"]["header"]
        _observe(ledger, signature, profile, partial)
        _observe(ledger, signature, profile, full, "finalized")
        report = auditor.summarize(path)
        assert (
            _transactions(report)[signature]["observed_network_fee_lamports"] == 5_000
        )
        changed = deepcopy(full)
        if message_field == "header":
            changed["transaction"]["message"]["header"][
                "numReadonlyUnsignedAccounts"
            ] = 1
        else:
            changed["transaction"]["message"]["recentBlockhash"] = OTHER
        _observe(ledger, signature, profile, changed, "finalized")

    report = auditor.summarize(path)
    assert _transactions(report)[signature]["observed_network_fee_lamports"] is None
    assert (
        "conflicting_receipt_" + message_field
        in _transactions(report)[signature]["issues"]
    )
    assert report["live_network_fees"]["known_subtotal_lamports"] == 0


def test_display_amount_changes_preserve_raw_balance_reconciliation(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        meta = result["meta"]
        meta["preTokenBalances"] = [deepcopy(meta["postTokenBalances"][0])]
        meta["preTokenBalances"][0]["uiTokenAmount"].update(amount="0", uiAmount=0.0)
        meta["preBalances"][2] = meta["postBalances"][2]
        meta["postBalances"][0] = 989_995_000
        _observe(ledger, signature, profile, result)
        formatted = deepcopy(result)
        for field, display_amount in [
            ("preTokenBalances", "0.000000"),
            ("postTokenBalances", "25.000000"),
        ]:
            amount = formatted["meta"][field][0]["uiTokenAmount"]
            amount["uiAmount"] = None
            amount["uiAmountString"] = display_amount
        _observe(ledger, signature, profile, formatted, "finalized")
        report = auditor.summarize(path)
        row = _transactions(report)[signature]
        assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
        assert row["fee_commitment"] == "finalized"
        assert not row["issues"]
        economic = row["economic_receipt"]
        assert economic["token_commitment"] == "finalized"  # noqa: S105 - public chain commitment
        assert economic["tokens"][0]["pre_amount_raw"] == 0
        assert economic["tokens"][0]["post_amount_raw"] == 25_000_000
        assert economic["tokens"][0]["change_raw"] == 25_000_000

        changed = deepcopy(formatted)
        changed["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "25000001"
        _observe(ledger, signature, profile, changed, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert "conflicting_receipt_postTokenBalances" in row["issues"]
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_token_balance_order_does_not_hide_fees_or_reassign_amounts(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        meta = result["meta"]
        template = meta["postTokenBalances"][0]
        for field, amounts in [
            ("preTokenBalances", ("20000000", "0")),
            ("postTokenBalances", ("10000000", "25000000")),
        ]:
            meta[field] = [
                {
                    **template,
                    "accountIndex": index,
                    "uiTokenAmount": {"amount": amount, "decimals": 6},
                }
                for index, amount in zip((1, 2), amounts, strict=True)
            ]
        meta["preBalances"][2] = meta["postBalances"][2]
        meta["postBalances"][0] = 989_995_000
        _observe(ledger, signature, profile, result)
        reordered = deepcopy(result)
        for field in ("preTokenBalances", "postTokenBalances"):
            reordered["meta"][field].reverse()
        _observe(ledger, signature, profile, reordered, "finalized")
        report = auditor.summarize(path)
        row = _transactions(report)[signature]
        assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
        assert not row["issues"]
        economic = row["economic_receipt"]
        assert economic["token_commitment"] == "finalized"  # noqa: S105 - public chain commitment
        assert {
            token["account"]: token["change_raw"] for token in economic["tokens"]
        } == {OTHER: -10_000_000, TOKEN_ACCOUNT: 25_000_000}

        changed = deepcopy(reordered)
        first, second = changed["meta"]["postTokenBalances"]
        first["accountIndex"], second["accountIndex"] = (
            second["accountIndex"],
            first["accountIndex"],
        )
        _observe(ledger, signature, profile, changed, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert "conflicting_receipt_postTokenBalances" in row["issues"]
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


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
    expected = {
        "no_attributable_live_submissions",
        "unattributed_or_invalid_submissions",
    }
    if corruption == "profile_hash":
        expected.add("invalid_evidence_profiles")
    assert _fee_blocker_codes(report) == expected


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
    assert row["economic_receipt"] is None
    assert report["unattributed_submission_count"] == 1
    assert report["live_network_fees"]["submission_count"] == 1
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None
    assert _fee_blocker_codes(report) == {"unattributed_or_invalid_submissions"}


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
    assert not _fee_blocker_codes(report)
    assert practice_receipt in {row["receipt_id"] for row in report["receipt_issues"]}
    assert report["live_balance_coverage"]["native_observed_count"] == 1
    assert all(
        _transactions(report)[signature]["economic_receipt"] is None
        for kind, signature in signatures.items()
        if kind != "live"
    )


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
    assert _fee_blocker_codes(report) == {
        "event_integrity_issues",
        "receipt_integrity_issues",
    }


@pytest.mark.parametrize(
    ("unrelated", "expected"),
    [
        ("outcome", "orphan_ledger_outcomes"),
        ("profile", "invalid_evidence_profiles"),
    ],
)
def test_unrelated_integrity_failure_blocks_only_the_complete_fee_total(
    tmp_path: Path, auditor: ModuleType, unrelated: str, expected: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        unused_profile = _profile(ledger, run="unrelated")
        signature = _reserve(ledger, 1, profile)
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.SUCCESS, signature, slot=42)
        )
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
    with closing(sqlite3.connect(path)) as connection, connection:
        if unrelated == "outcome":
            connection.execute(
                "INSERT INTO outcomes(signature, status, slot) VALUES (?, 'success', 42)",
                (_signature(2),),
            )
        else:
            connection.execute(
                "UPDATE evidence_profiles SET settings_json = '{}' WHERE profile_id = ?",
                (unused_profile,),
            )
    report = auditor.summarize(path)
    assert _fee_blocker_codes(report) == {expected}
    fees = report["live_network_fees"]
    assert fees["complete_finalized_total_lamports"] is None
    assert fees["known_subtotal_lamports"] == 5_000
    assert fees["finalized_subtotal_lamports"] == 5_000
    assert report["orphan_outcomes"] == (
        [
            {
                "signature": _signature(2),
                "ledger_status": "success",
                "ledger_slot": 42,
                "issues": [],
            }
        ]
        if unrelated == "outcome"
        else []
    )
    assert list(_transactions(report)) == [signature]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (
            (_signature(2), SENTINEL, SENTINEL),
            {
                "signature": _signature(2),
                "ledger_status": None,
                "ledger_slot": None,
                "issues": ["invalid_ledger_slot", "invalid_ledger_status"],
            },
        ),
        (
            (SENTINEL, "unknown", None),
            {
                "signature": None,
                "ledger_status": "unknown",
                "ledger_slot": None,
                "issues": ["invalid_outcome_signature"],
            },
        ),
    ],
)
def test_invalid_orphan_outcomes_withhold_untrusted_fields_not_paid_fees(
    tmp_path: Path,
    auditor: ModuleType,
    stored: tuple[str, str, str | None],
    expected: dict,
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        paid = _reserve(ledger, 1, profile)
        _observe(ledger, paid, profile, _receipt(paid), "finalized")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "INSERT INTO outcomes(signature, status, slot, error) VALUES (?, ?, ?, ?)",
            (*stored, SENTINEL),
        )
    report = auditor.summarize(path)
    assert report["orphan_outcomes"] == [expected]
    assert list(_transactions(report)) == [paid]
    assert _fee_blocker_codes(report) == {"orphan_ledger_outcomes"}
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None
    assert SENTINEL not in json.dumps(report, allow_nan=False)


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
    assert _fee_blocker_codes(report) == {
        "no_attributable_live_submissions",
        "unattributed_or_invalid_submissions",
        "missing_evidence_tables",
        "missing_submission_profile_column",
    }
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
    output = tmp_path / "snapshot.json"
    result = _cli(missing, "--output", str(output))
    assert result.returncode != 0
    assert not missing.exists()
    assert not output.exists()
    invalid = tmp_path / "invalid.sqlite"
    invalid.write_bytes(b"not a SQLite database")
    result = _cli(invalid, "--output", str(output))
    assert result.returncode != 0
    assert invalid.read_bytes() == b"not a SQLite database"
    assert not output.exists()
    incomplete = tmp_path / "incomplete.sqlite"
    with closing(sqlite3.connect(incomplete)) as connection, connection:
        connection.execute("CREATE TABLE intents (intent_id TEXT)")
    assert _cli(incomplete, "--output", str(output)).returncode != 0
    assert not output.exists()


def test_cli_reads_committed_wal_without_mutating_schema_or_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wal ledger #1.sqlite"
    output = tmp_path / f"{SENTINEL}.json"
    linked_output = tmp_path / "snapshot-link.json"
    linked_output.symlink_to(output)
    missing_target = tmp_path / "never-created.json"
    dangling_output = tmp_path / "dangling.json"
    dangling_output.symlink_to(missing_target)
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
        result = _cli(path, "--output", str(output))
        assert result.returncode == 0, result.stderr
        report = json.loads(output.read_text(encoding="utf-8"))
        assert (
            _transactions(report)[signature]["observed_network_fee_lamports"] == 5_000
        )
        assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
        assert SENTINEL not in result.stdout
        saved_bytes = output.read_bytes()
        assert SENTINEL.encode() not in saved_bytes
        for refused in (
            output,
            path,
            linked_output,
            dangling_output,
            tmp_path / SENTINEL / "snapshot.json",
        ):
            rejected = _cli(path, "--output", str(refused))
            assert rejected.returncode != 0
            assert rejected.stdout == ""
            assert SENTINEL not in rejected.stderr
        assert output.read_bytes() == saved_bytes
        assert linked_output.is_symlink() and linked_output.readlink() == output
        assert dangling_output.is_symlink()
        assert dangling_output.readlink() == missing_target
        assert not missing_target.exists()
        assert tuple(ledger.connection.iterdump()) == before_dump
        assert path.read_bytes() == before_database
        assert path.with_name(path.name + "-wal").read_bytes() == before_wal


@pytest.mark.parametrize(
    "malformation",
    [
        "signature_mismatch",
        "invalid_signature",
        "too_many_signatures",
        "mixed_account_keys",
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
        elif malformation == "too_many_signatures":
            result["transaction"]["signatures"].extend([_signature(2), _signature(3)])
        elif malformation == "mixed_account_keys":
            result["transaction"]["message"]["accountKeys"][1] = {"pubkey": OTHER}
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, []),
        (None, {}),
        ("numRequiredSignatures", True),
        ("numReadonlyUnsignedAccounts", -1),
        ("numReadonlySignedAccounts", 2**8),
        ("numRequiredSignatures", 2),
        ("numReadonlySignedAccounts", 1),
        ("numReadonlyUnsignedAccounts", 2),
    ],
)
def test_invalid_message_header_cannot_supply_receipt_evidence(
    tmp_path: Path, auditor: ModuleType, field: str | None, value: object
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        _observe(ledger, signature, profile, result)
        header = {
            "numRequiredSignatures": 1,
            "numReadonlySignedAccounts": 0,
            "numReadonlyUnsignedAccounts": 0,
        }
        result["transaction"]["message"]["header"] = (
            value if field is None else {**header, field: value}
        )
        receipt_id = _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert any(
        entry["receipt_id"] == receipt_id
        and "invalid_receipt_payload" in entry["issues"]
        for entry in report["receipt_issues"]
    )
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


@pytest.mark.parametrize(
    ("index", "flag", "value"),
    [
        (0, "signer", False),
        (0, "writable", False),
        (1, "signer", True),
        (0, "signer", 1),
        (1, "writable", "false"),
    ],
)
def test_invalid_parsed_account_permissions_withhold_receipt_evidence(
    tmp_path: Path, auditor: ModuleType, index: int, flag: str, value: object
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _receipt(signature)
        keys = [
            {"pubkey": PAYER, "signer": True, "writable": True},
            {"pubkey": OTHER, "signer": False, "writable": True},
        ]
        result["transaction"]["message"]["accountKeys"] = keys
        _observe(ledger, signature, profile, result)
        keys[index][flag] = value
        receipt_id = _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert any(
        entry["receipt_id"] == receipt_id
        and "invalid_receipt_payload" in entry["issues"]
        for entry in report["receipt_issues"]
    )
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_new_parsed_permissions_are_evidence_but_changed_permissions_conflict(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _receipt(signature)
        keys = [{"pubkey": PAYER}, {"pubkey": OTHER, "writable": None}]
        result["transaction"]["message"]["accountKeys"] = keys
        _observe(ledger, signature, profile, result)
        for index, key in enumerate(keys):
            key.update(signer=index == 0, writable=True)
        _observe(ledger, signature, profile, result, "finalized")
        report = auditor.summarize(path)
        assert (
            _transactions(report)[signature]["observed_network_fee_lamports"] == 5_000
        )
        assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
        keys[1]["writable"] = False
        _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert "conflicting_receipt_accountKeys_1_writable" in row["issues"]
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
    event = next(row for row in report["events"] if row["event_id"] == event_id)
    assert event["reported_success"] is True
    assert event["reported_status"] == "success"
    assert "event_receipt_status_conflict" in event["issues"]
    assert event["submission_links"] == [
        {"signature": signature, "relations": ["signature_reference"]}
    ]
    assert _transactions(report)[signature]["event_ids"] == [event_id]
    assert _transactions(report)[signature]["receipt_status"] == "reverted"


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
        closure = next(row for row in report["events"] if row["event_id"] == event_id)
        assert closure["profile_id"] == observer
        assert {
            link["signature"]: link["relations"] for link in closure["submission_links"]
        } == {entry: ["position_entry"], exit_signature: ["signature_reference"]}
        assert _transactions(report)[entry]["event_ids"] == [event_id]
        assert _transactions(report)[exit_signature]["event_ids"] == [event_id]
        assert _transactions(report)[entry]["submission_profile_id"] == original
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
        (
            "chain_outcome",
            {"signature": _signature(1), "status": "unknown", "slot": True},
            "invalid_event_slot",
        ),
        (
            "trade_result",
            {"result": {"success": False, "status": "failed", "slot": SENTINEL}},
            "invalid_event_slot",
        ),
        (
            "chain_outcome",
            {"signature": _signature(1), "status": "success", "slot": -1},
            "invalid_event_slot",
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
        signature = _reserve(ledger, 1, profile)
        _observe(ledger, signature, profile, _receipt(signature), "finalized")
        event_id = ledger.record_trade_evidence(profile, category, payload)
    report = auditor.summarize(path)
    assert any(
        row["event_id"] == event_id and expected_issue in row["issues"]
        for row in report["event_issues"]
    )
    assert _transactions(report)[signature]["observed_network_fee_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None
    assert SENTINEL not in json.dumps(report)


@pytest.mark.parametrize("lookup", [False, True])
@pytest.mark.parametrize(
    "encodings",
    [
        ("raw", "partial"),
        ("raw", "parsed"),
        ("raw_unresolved", "partial"),
        ("parsed", "raw_unresolved"),
        ("partial", "parsed"),
    ],
)
def test_equivalent_receipt_encodings_preserve_fees_and_balance_finality(
    tmp_path: Path, auditor: ModuleType, encodings: tuple[str, str], *, lookup: bool
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        for encoding, commitment in zip(
            encodings, ("confirmed", "finalized"), strict=True
        ):
            result = _transfer_receipt(signature, encoding, lookup=lookup)
            _observe(ledger, signature, profile, result, commitment)
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["issues"] == []
    assert row["observed_network_fee_lamports"] == 5_000
    assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
    native = row["economic_receipt"]["native"]
    assert native["change_lamports"] == -15_000
    assert native["change_excluding_network_fee_lamports"] == -10_000
    assert native["commitment"] == (
        "confirmed" if lookup and encodings[1] == "raw_unresolved" else "finalized"
    )


@pytest.mark.parametrize(
    "change",
    [
        (
            "raw",
            "partial",
            ("instructions", 0, "data"),
            "3",
            "conflicting_receipt_instruction_0_data",
        ),
        (
            "raw",
            "partial",
            ("instructions", 0, "programId"),
            MINT,
            "conflicting_receipt_instruction_0_programId",
        ),
        (
            "raw_unresolved",
            "partial",
            ("instructions", 0, "accounts"),
            [OTHER, PAYER],
            "conflicting_receipt_instruction_0_accounts",
        ),
        (
            "raw",
            "parsed",
            ("instructions",),
            [],
            "conflicting_receipt_instructionCount",
        ),
        (
            "raw_unresolved",
            "parsed",
            ("accountKeys", 1, "pubkey"),
            MINT,
            "conflicting_receipt_staticAccountKeys",
        ),
        (
            "parsed",
            "parsed",
            ("instructions", 0, "parsed", "info", "lamports"),
            20_000,
            "conflicting_receipt_instruction_0_parsed",
        ),
        (
            "raw",
            "raw",
            ("instructions", 0, "accounts"),
            [2, 0],
            "conflicting_receipt_instruction_0_accountIndexes",
        ),
    ],
)
def test_encoding_normalization_preserves_real_instruction_conflicts(
    tmp_path: Path, auditor: ModuleType, change: tuple
) -> None:
    first, second, location, value, expected_issue = change
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        _observe(ledger, signature, profile, _transfer_receipt(signature, first))
        changed = _transfer_receipt(signature, second)
        target = changed["transaction"]["message"]
        for key in location[:-1]:
            target = target[key]
        target[location[-1]] = value
        _observe(ledger, signature, profile, changed, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert expected_issue in row["issues"]
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


@pytest.mark.parametrize("account_index", [True, -1, 3, 256])
def test_invalid_compiled_instruction_indexes_withhold_fees(
    tmp_path: Path, auditor: ModuleType, account_index: object
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _transfer_receipt(signature, "raw")
        result["transaction"]["message"]["instructions"][0]["accounts"] = [
            account_index
        ]
        _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] is None
    assert row["economic_receipt"] is None
    assert report["live_network_fees"]["complete_finalized_total_lamports"] is None


def test_unresolved_instruction_accounts_remain_unknown_not_invalid(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        _observe(
            ledger,
            signature,
            profile,
            _transfer_receipt(signature, "raw_unresolved"),
            "finalized",
        )
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] == 5_000
    assert row["issues"] == []
    assert row["economic_receipt"]["native"] is None
    assert "missing_loaded_addresses" in row["economic_receipt"]["issues"]


@pytest.mark.parametrize("encoding", ["raw", "parsed"])
def test_economic_receipt_resolves_v0_accounts_without_double_counting_fees(
    tmp_path: Path, auditor: ModuleType, encoding: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        buy = _reserve(ledger, 1, profile)
        sell = _reserve(ledger, 2, profile)
        bought = _token_receipt(buy)
        if encoding == "parsed":
            bought["transaction"]["message"]["accountKeys"] = [
                {"pubkey": key} for key in (PAYER, OTHER, TOKEN_ACCOUNT, MINT)
            ]
            del bought["meta"]["loadedAddresses"]
        sold = deepcopy(bought)
        sold["transaction"]["signatures"] = [sell]
        sold["meta"].update(
            {
                "fee": 6_000,
                "preBalances": bought["meta"]["postBalances"],
                "postBalances": [1_000_989_000, 99_000_000, 0, 1_000_000],
                "preTokenBalances": bought["meta"]["postTokenBalances"],
                "postTokenBalances": [],
            }
        )
        for signature, receipt in ((buy, bought), (sell, sold)):
            _observe(ledger, signature, profile, receipt)
            _observe(ledger, signature, profile, receipt, "finalized")

    rows = _transactions(auditor.summarize(path))
    entry, exit_receipt = [rows[s]["economic_receipt"] for s in (buy, sell)]
    assert entry["native"]["change_lamports"] == -12_044_280
    assert entry["native"]["change_excluding_network_fee_lamports"] == -12_039_280
    assert exit_receipt["native"]["change_lamports"] == 13_033_280
    assert exit_receipt["native"]["change_excluding_network_fee_lamports"] == 13_039_280
    assert (
        entry["native"]["change_lamports"] + exit_receipt["native"]["change_lamports"]
        == 989_000
    )
    assert entry["tokens"] == [
        {
            "account": TOKEN_ACCOUNT,
            "mint": MINT,
            "program_id": str(TOKEN_2022_PROGRAM_ID),
            "decimals": 6,
            "pre_amount_raw": 0,
            "post_amount_raw": 25_000_000,
            "change_raw": 25_000_000,
        }
    ]
    assert exit_receipt["tokens"][0]["pre_amount_raw"] == 25_000_000
    assert exit_receipt["tokens"][0]["post_amount_raw"] == 0
    assert exit_receipt["tokens"][0]["change_raw"] == -25_000_000


def test_wrapping_sol_does_not_erase_rent_or_turn_principal_into_income(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        result["meta"]["postBalances"][1:3] = [100_000_000, 12_039_280]
        token = result["meta"]["postTokenBalances"][0]
        token["mint"] = "So11111111111111111111111111111111111111112"
        token["programId"] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        token["uiTokenAmount"] = {"amount": "10000000", "decimals": 9}
        _observe(ledger, signature, profile, result, "finalized")
    receipt = _transactions(auditor.summarize(path))[signature]["economic_receipt"]
    assert receipt["native"]["change_lamports"] == -12_044_280
    assert receipt["native"]["change_excluding_network_fee_lamports"] == -12_039_280
    assert receipt["tokens"][0]["mint"] == token["mint"]
    assert receipt["tokens"][0]["change_raw"] == 10_000_000


def test_balance_finality_requires_the_balance_fields_not_just_a_finalized_fee(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        complete = _token_receipt(signature)
        _observe(ledger, signature, profile, complete)
        partial = deepcopy(complete)
        for field in (
            "preBalances",
            "postBalances",
            "preTokenBalances",
            "postTokenBalances",
        ):
            del partial["meta"][field]
        _observe(ledger, signature, profile, partial, "finalized")
        report = auditor.summarize(path)
        row = _transactions(report)[signature]
        assert not _fee_blocker_codes(report)
        assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000
        assert row["fee_commitment"] == "finalized"
        assert row["economic_receipt"]["native"]["commitment"] == "confirmed"
        assert row["economic_receipt"]["token_commitment"] == "confirmed"  # noqa: S105 - public chain commitment
        partial["meta"]["preBalances"] = complete["meta"]["preBalances"]
        partial["meta"]["postBalances"] = complete["meta"]["postBalances"]
        _observe(ledger, signature, profile, partial, "finalized")
        receipt = _transactions(auditor.summarize(path))[signature]["economic_receipt"]
        assert receipt["native"]["commitment"] == "finalized"
        assert receipt["token_commitment"] == "confirmed"  # noqa: S105 - public chain commitment
        _observe(ledger, signature, profile, complete, "finalized")
    receipt = _transactions(auditor.summarize(path))[signature]["economic_receipt"]
    assert receipt["token_commitment"] == "finalized"  # noqa: S105 - public chain commitment
    assert receipt["tokens"][0]["change_raw"] == 25_000_000


@pytest.mark.parametrize(
    "index_fields", [{}, {"accountIndex": "2"}], ids=["missing", "mixed_type"]
)
def test_malformed_token_indices_do_not_hide_observed_fees(
    tmp_path: Path, auditor: ModuleType, index_fields: dict[str, str]
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        balances = result["meta"]["postTokenBalances"]
        malformed = deepcopy(balances[0])
        del malformed["accountIndex"]
        malformed.update(index_fields)
        balances.append(malformed)
        _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] == 5_000
    assert row["economic_receipt"]["tokens"] is None
    assert "invalid_or_incomplete_token_balances" in row["economic_receipt"]["issues"]
    assert report["live_balance_coverage"]["token_unknown_count"] == 1


@pytest.mark.parametrize(
    "missing",
    [
        "owner",
        "before",
        "after",
        "stable_identity",
        "unique_index",
        "bounded_index",
        "bounded_amount",
        "native_balances",
        "token_balances",
        "loaded_addresses",
    ],
)
def test_incomplete_balances_never_become_zero_or_hide_observed_fees(
    tmp_path: Path, auditor: ModuleType, missing: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        meta = result["meta"]
        token = meta["postTokenBalances"][0]
        if missing == "owner":
            del token["owner"]
        elif missing == "before":
            meta["preBalances"][2] = 2_039_280
        elif missing == "after":
            meta["preBalances"][2] = 2_039_280
            meta["preTokenBalances"] = meta["postTokenBalances"]
            meta["postTokenBalances"] = []
        elif missing == "stable_identity":
            meta["preTokenBalances"] = [dict(token, owner=OTHER)]
        elif missing == "unique_index":
            meta["postTokenBalances"].append(deepcopy(token))
        elif missing == "bounded_index":
            token["accountIndex"] = 4
        elif missing == "bounded_amount":
            token["uiTokenAmount"]["amount"] = str(2**64)
        elif missing == "native_balances":
            del meta["preBalances"]
        elif missing == "token_balances":
            del meta["preTokenBalances"]
        else:
            del meta["loadedAddresses"]
        _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["observed_network_fee_lamports"] == 5_000
    assert row["economic_receipt"]["tokens"] is None
    assert row["economic_receipt"]["token_commitment"] is None
    assert report["live_balance_coverage"]["token_unknown_count"] == 1
    assert report["live_balance_coverage"]["native_unknown_count"] == (
        1 if missing in {"native_balances", "loaded_addresses"} else 0
    )


@pytest.mark.parametrize("owner", [PAYER, OTHER], ids=["signer", "other_owner"])
def test_reverted_token_movement_is_unknown_without_erasing_the_fee(
    tmp_path: Path, auditor: ModuleType, owner: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        signature = _reserve(ledger, 1, profile)
        result = _token_receipt(signature)
        result["meta"]["err"] = {"InstructionError": [0, {"Custom": 1}]}
        result["meta"]["postTokenBalances"][0]["owner"] = owner
        _observe(ledger, signature, profile, result, "finalized")
    report = auditor.summarize(path)
    row = _transactions(report)[signature]
    assert row["receipt_status"] == "reverted"
    assert row["observed_network_fee_lamports"] == 5_000
    assert row["economic_receipt"]["tokens"] is None
    assert report["live_balance_coverage"]["token_unknown_count"] == 1
    assert report["live_network_fees"]["complete_finalized_total_lamports"] == 5_000


def test_decisions_link_attempts_without_spreading_result_claims(
    tmp_path: Path, auditor: ModuleType
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        profile = _profile(ledger)
        first = _reserve(ledger, 1, profile)
        ledger.record_outcome(
            TransactionOutcome(TransactionStatus.REVERTED, first, slot=42)
        )
        _observe(ledger, first, profile, _receipt(first, reverted=True), "finalized")
        second = _signature(2)
        ledger.record_submission(
            "intent-1", second, BLOCKHASH, 501, evidence_profile_id=profile
        )
        _observe(ledger, second, profile, _receipt(second), "finalized")
        unrelated = _reserve(ledger, 3, profile)
        decision = ledger.record_trade_evidence(
            profile,
            "decision",
            {
                "intent_id": "intent-1",
                "action": "buy",
                "gate": {"accept": True, "reason": SENTINEL},
                "reason": SENTINEL,
            },
        )
        result = ledger.record_trade_evidence(
            profile,
            "trade_result",
            {
                "intent_id": "intent-1",
                "signature": second,
                "result": {
                    "tx_signature": second,
                    "success": True,
                    "status": "success",
                    "error_message": SENTINEL,
                },
            },
        )
        unsubmitted = ledger.record_trade_evidence(
            profile,
            "decision",
            {
                "intent_id": "never-submitted",
                "action": "skip",
                "gate": {"accept": False},
            },
        )
        practice = ledger.record_trade_evidence(
            _profile(ledger, "paper"),
            "decision",
            {"intent_id": "intent-1", "action": "buy"},
        )

    report = auditor.summarize(path)
    events = {row["event_id"]: row for row in report["events"]}
    transactions = _transactions(report)
    assert {
        link["signature"]: link["relations"]
        for link in events[decision]["submission_links"]
    } == {first: ["decision_intent"], second: ["decision_intent"]}
    assert events[result]["submission_links"] == [
        {"signature": second, "relations": ["signature_reference"]}
    ]
    assert sorted(transactions[first]["event_ids"]) == sorted([decision, practice])
    assert sorted(transactions[second]["event_ids"]) == sorted(
        [decision, result, practice]
    )
    assert transactions[unrelated]["event_ids"] == []
    assert events[unsubmitted]["submission_links"] == []
    assert events[unsubmitted]["gate_accepted"] is False
    assert events[practice]["kind"] == "paper"
    assert transactions[first]["kind"] == "live"
    assert transactions[first]["receipt_status"] == "reverted"
    assert report["live_network_fees"]["known_subtotal_lamports"] == 10_000
    assert SENTINEL not in json.dumps(report)


@pytest.mark.parametrize(
    "corruption", ["payload_hash", "json", "profile_hash", "missing_profile"]
)
def test_corrupt_observations_cannot_attach_claims_to_a_submission(
    tmp_path: Path, auditor: ModuleType, corruption: str
) -> None:
    path = tmp_path / "ledger.sqlite"
    with TransactionLedger(path) as ledger:
        original = _profile(ledger)
        observer = _profile(ledger, run="observer")
        signature = _reserve(ledger, 1, original)
        _observe(ledger, signature, original, _receipt(signature), "finalized")
        payload = {
            "action": "buy",
            "result": {"tx_signature": signature, "success": False, "status": "failed"},
        }
        event_id = ledger.record_trade_evidence(observer, "trade_result", payload)
    with closing(sqlite3.connect(path)) as connection, connection:
        if corruption in ("payload_hash", "json"):
            payload["action"] = "sell"
            payload["private"] = SENTINEL
            connection.execute(
                "UPDATE evidence_events SET payload_json = ? WHERE event_id = ?",
                (
                    json.dumps(payload)
                    if corruption == "payload_hash"
                    else '{"private":"' + SENTINEL,
                    event_id,
                ),
            )
        elif corruption == "profile_hash":
            connection.execute(
                "UPDATE evidence_profiles SET settings_json = '{}' WHERE profile_id = ?",
                (observer,),
            )
        else:
            connection.execute(
                "DELETE FROM evidence_profiles WHERE profile_id = ?", (observer,)
            )
    report = auditor.summarize(path)
    event = next(row for row in report["events"] if row["event_id"] == event_id)
    assert event["issues"]
    assert event["submission_links"] == []
    assert event["declared_action"] is None
    assert event["reported_success"] is None
    assert _transactions(report)[signature]["event_ids"] == []
    assert report["live_network_fees"]["known_subtotal_lamports"] == 5_000
    assert SENTINEL not in json.dumps(report)
