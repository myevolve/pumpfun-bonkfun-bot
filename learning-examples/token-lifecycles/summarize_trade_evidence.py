"""Audit durable trade evidence and observed network fees without changing the ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

from solders.pubkey import Pubkey
from solders.signature import Signature

KINDS = {"live", "dry_run", "simulation", "paper"}
EVIDENCE_TABLES = ("evidence_profiles", "evidence_events", "evidence_receipts")
COLUMNS = {
    "intents": ("intent_id", "signer"),
    "submissions": ("signature", "intent_id"),
    "outcomes": ("signature", "status", "slot"),
    "evidence_profiles": ("profile_id", "kind", "settings_json", "sources_json"),
    "evidence_events": ("event_id", "profile_id", "category", "payload_json"),
    "evidence_receipts": (
        "receipt_id",
        "signature",
        "commitment",
        "profile_id",
        "observed_fee_lamports",
        "payload_json",
    ),
}


def _object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            reason = "duplicate JSON key"
            raise ValueError(reason)
        value[key] = item
    return value


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        reason = "nonfinite JSON number"
        raise ValueError(reason)
    return value


def _reject_constant(_: str) -> None:
    reason = "nonfinite JSON number"
    raise ValueError(reason)


def _json_object(text: object) -> dict:
    if not isinstance(text, str):
        reason = "JSON must be text"
        raise TypeError(reason)
    value = json.loads(
        text,
        object_pairs_hook=_object,
        parse_float=_finite_float,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        reason = "JSON must be an object"
        raise TypeError(reason)
    return value


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _uint(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**64


def _public_key(value: object) -> str:
    if not isinstance(value, str):
        reason = "invalid public key"
        raise TypeError(reason)
    return str(Pubkey.from_string(value))


def _signature(value: object) -> str:
    if not isinstance(value, str):
        reason = "invalid signature"
        raise TypeError(reason)
    return str(Signature.from_string(value))


def _snapshot(path: Path) -> tuple[dict, list[str], bool]:
    # mode=ro must not be replaced with immutable=1: committed WAL data matters.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"intents", "submissions", "outcomes"} <= tables:
            reason = "missing required ledger tables"
            raise ValueError(reason)
        data = {}
        has_profile_column = False
        for table, required in COLUMNS.items():
            if table not in tables:
                data[table] = []
                continue
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not set(required) <= columns:
                reason = "missing required ledger columns"
                raise ValueError(reason)
            selected = list(required)
            if table == "submissions":
                has_profile_column = "evidence_profile_id" in columns
                selected.extend(
                    name for name in ("state", "evidence_profile_id") if name in columns
                )
            rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT {', '.join(selected)} FROM {table} ORDER BY {required[0]}"  # noqa: S608 - identifiers come only from COLUMNS and fixed optional names
                )
            ]
            seen = set()
            for row in rows:
                identifier = row[required[0]]
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier in seen
                ):
                    reason = "invalid or duplicate ledger identifier"
                    raise ValueError(reason)
                seen.add(identifier)
            data[table] = rows
        return data, sorted(set(EVIDENCE_TABLES) - tables), has_profile_column
    finally:
        connection.close()


def _profiles(rows: list[dict]) -> dict[str, dict]:
    profiles = {}
    for row in rows:
        issues = []
        kind = row["kind"]
        valid_kind = isinstance(kind, str) and kind in KINDS
        if not valid_kind:
            issues.append("invalid_profile_kind")
        has_sources = False
        try:
            settings = _json_object(row["settings_json"])
            sources = _json_object(row["sources_json"])
            if any(not isinstance(value, str) for value in sources.values()):
                issues.append("invalid_profile_sources")
            else:
                has_sources = bool(sources) and all(
                    re.fullmatch(r"[0-9a-f]{64}", value) for value in sources.values()
                )
            if (
                _digest({"kind": kind, "settings": settings, "sources": sources})
                != row["profile_id"]
            ):
                issues.append("profile_hash_mismatch")
        except (ValueError, TypeError, RecursionError):
            issues.append("invalid_profile_json")
        profiles[row["profile_id"]] = {
            "profile_id": row["profile_id"],
            "kind": kind if valid_kind else "invalid",
            "valid": not issues,
            "has_source_hashes": has_sources,
            "issues": sorted(issues),
        }
    return profiles


def _kind(profile_id: object, profiles: dict) -> str:
    if profile_id is None:
        return "unattributed"
    if not isinstance(profile_id, str):
        return "invalid"
    profile = profiles.get(profile_id)
    return profile["kind"] if profile and profile["valid"] else "invalid"


def _event_signatures(payload: dict) -> list[object]:
    references = []
    for obj in (payload, payload.get("result"), payload.get("position")):
        if isinstance(obj, dict):
            for name in ("signature", "tx_signature", "pending_exit_signature"):
                if obj.get(name) is not None:
                    references.append(obj[name])
    return references


def _outcome_claim_issues(transaction: dict, status: object, slot: object) -> list[str]:
    """Compare terminal claims, not earlier uncertainty, with retained outcomes."""
    issues = []
    if transaction["kind"] != "live":
        issues.append("event_submission_not_live")
    if status not in ("success", "reverted", "expired"):
        return issues
    receipt_status = transaction["receipt_status"]
    if receipt_status is None:
        if status != "expired" or transaction["ledger_status"] != "expired":
            issues.append("event_receipt_unavailable")
    elif status != receipt_status:
        issues.append("event_receipt_status_conflict")
    if slot is not None:
        if not _uint(slot):
            issues.append("invalid_event_slot")
        elif (
            transaction["receipt_slot"] is not None
            and slot != transaction["receipt_slot"]
        ):
            issues.append("event_receipt_slot_conflict")
    return issues


def _live_event_issues(  # noqa: C901, PLR0911, PLR0912, PLR0915 - one event's declared relationships
    category: str, payload: dict, submissions: dict[str, dict], by_intent: dict
) -> list[str]:
    """Check declared status and links; never turn a decision into a fill."""
    issues = []
    position = payload.get("position", {})
    if not isinstance(position, dict):
        return ["invalid_event_position"]
    if category == "decision":
        action = payload.get("action")
        if action not in ("buy", "sell", "skip"):
            return ["invalid_decision_action"]
        gate = payload.get("gate")
        if gate is not None:
            if not isinstance(gate, dict) or type(gate.get("accept")) is not bool:
                issues.append("invalid_decision_gate")
            elif action in ("buy", "skip") and gate["accept"] != (action == "buy"):
                issues.append("decision_gate_action_conflict")
        if action in ("buy", "sell"):
            intent = payload.get("intent_id")
            if not isinstance(intent, str) or not intent:
                issues.append("missing_decision_intent")
            elif any(row["kind"] != "live" for row in by_intent.get(intent, [])):
                issues.append("decision_submission_not_live")
            if action == "sell" and position.get("pending_exit_intent_id") not in (
                None,
                intent,
            ):
                issues.append("event_intent_mismatch")
        # A decision may fail before any wire is reserved; no submission is valid.
        return issues
    if category not in ("trade_result", "chain_outcome", "position_closed"):
        return issues
    details = payload.get("result") if category == "trade_result" else payload
    if not isinstance(details, dict):
        return ["invalid_event_result"]
    status = details.get("status")
    if category == "trade_result":
        success = details.get("success")
        if type(success) is not bool:
            return ["invalid_event_success"]
        if success:
            if status not in (None, "success"):
                issues.append("event_success_status_conflict")
            status = "success"
        elif status not in (
            None,
            "failed",
            "unknown",
            "success",
            "reverted",
            "expired",
        ):
            issues.append("invalid_event_status")
    elif category == "position_closed":
        status = "success"
    elif status not in ("unknown", "success", "reverted", "expired"):
        issues.append("invalid_event_status")
    signature = details.get(
        "tx_signature" if category == "trade_result" else "signature"
    )
    if signature is None:
        if category != "trade_result" or status in ("success", "reverted", "expired"):
            issues.append("missing_event_signature")
        return issues
    transaction = submissions.get(signature)
    if transaction is None:
        return issues  # The common signature check reports untracked references.
    issues.extend(_outcome_claim_issues(transaction, status, details.get("slot")))
    intents = [payload.get("intent_id")]
    if payload.get("action") == "sell" or category == "position_closed":
        intents.append(position.get("pending_exit_intent_id"))
        if position.get("pending_exit_signature") not in (None, signature):
            issues.append("position_exit_signature_mismatch")
    if any(
        intent is not None and intent != transaction["intent_id"] for intent in intents
    ):
        issues.append("event_intent_mismatch")
    if category == "position_closed":
        entry_signature = position.get("position_id")
        entry = (
            submissions.get(entry_signature)
            if isinstance(entry_signature, str)
            else None
        )
        if entry is None:
            issues.append("untracked_position_entry")
        else:
            if entry_signature == signature:
                issues.append("position_entry_exit_same_signature")
            if entry["signer"] != transaction["signer"]:
                issues.append("position_signer_mismatch")
            issues.extend(
                "position_entry_" + issue.removeprefix("event_")
                for issue in _outcome_claim_issues(entry, "success", None)
            )
    return issues


def _events(  # noqa: C901, PLR0912 - validate each observation before counting it
    rows: list[dict], profiles: dict, submissions: dict[str, dict]
) -> tuple[dict, list]:
    if not rows:
        return {}, []
    by_intent = {}
    for transaction in submissions.values():
        by_intent.setdefault(transaction["intent_id"], []).append(transaction)
    counts = {}
    failures = []
    for row in rows:
        issues = []
        kind = _kind(row["profile_id"], profiles)
        category = row["category"]
        valid = isinstance(category, str) and bool(category)
        if not valid:
            issues.append("invalid_event_category")
        if kind in {"invalid", "unattributed"}:
            issues.append(
                "invalid_event_profile"
                if kind == "invalid"
                else "missing_event_profile"
            )
        try:
            payload = _json_object(row["payload_json"])
            if (
                _digest(
                    {
                        "profile_id": row["profile_id"],
                        "category": category,
                        "payload": payload,
                    }
                )
                != row["event_id"]
            ):
                valid = False
                issues.append("event_hash_mismatch")
            if valid and category in {
                "trade_result",
                "chain_outcome",
                "position_closed",
            }:
                for reference in _event_signatures(payload):
                    try:
                        signature = _signature(reference)
                    except (ValueError, TypeError):
                        issues.append("invalid_event_signature")
                        valid = False
                        continue
                    if signature not in submissions:
                        issues.append("untracked_event_signature")
            if valid and kind == "live":
                issues.extend(
                    _live_event_issues(category, payload, submissions, by_intent)
                )
        except (ValueError, TypeError, RecursionError):
            valid = False
            issues.append("invalid_event_json")
        if valid and kind != "invalid":
            categories = counts.setdefault(kind, {})
            categories[category] = categories.get(category, 0) + 1
        if issues:
            failures.append(
                {"event_id": row["event_id"], "issues": sorted(set(issues))}
            )
    return {
        kind: dict(sorted(counts[kind].items())) for kind in sorted(counts)
    }, failures


def _receipt_fields(result: dict, signature: str) -> dict:  # noqa: C901, PLR0912, PLR0915 - one receipt trust boundary
    """Normalize identities but retain stable chain facts for cross-read comparison."""
    if not _uint(result.get("slot")):
        reason = "invalid receipt slot"
        raise ValueError(reason)
    transaction = result.get("transaction")
    if not isinstance(transaction, dict):
        reason = "invalid transaction"
        raise TypeError(reason)
    signatures = transaction.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        reason = "missing signatures"
        raise ValueError(reason)
    signatures = [_signature(item) for item in signatures]
    if signatures[0] != _signature(signature):
        reason = "receipt signature mismatch"
        raise ValueError(reason)
    message = transaction.get("message")
    if not isinstance(message, dict):
        reason = "invalid message"
        raise TypeError(reason)
    keys = message.get("accountKeys")
    if not isinstance(keys, list) or not keys:
        reason = "missing account keys"
        raise ValueError(reason)
    keys = [
        _public_key(item.get("pubkey") if isinstance(item, dict) else item)
        for item in keys
    ]
    meta = result.get("meta")
    if not isinstance(meta, dict) or "err" not in meta:
        reason = "missing chain error status"
        raise ValueError(reason)
    if meta["err"] is not None and not isinstance(meta["err"], dict | list | str):
        reason = "invalid chain error status"
        raise ValueError(reason)
    fields = {
        "slot": result["slot"],
        "signatures": signatures,
        "accountKeys": keys,
        "err": meta["err"],
    }
    for name in ("recentBlockhash", "instructions", "addressTableLookups"):
        if name in message and message[name] is not None:
            if name == "recentBlockhash":
                _public_key(
                    message[name]
                )  # A blockhash is also a base58-encoded 32-byte value.
            elif not isinstance(message[name], list):
                reason = "invalid message field"
                raise TypeError(reason)
            fields[name] = message[name]
    for name in (
        "preBalances",
        "postBalances",
        "preTokenBalances",
        "postTokenBalances",
        "loadedAddresses",
    ):
        if name not in meta or meta[name] is None:
            continue
        value = meta[name]
        if name == "loadedAddresses":
            if not isinstance(value, dict) or any(
                not isinstance(value.get(side), list)
                for side in ("writable", "readonly")
            ):
                reason = "invalid loaded addresses"
                raise ValueError(reason)
            value = {
                side: [_public_key(key) for key in value[side]]
                for side in ("writable", "readonly")
            }
        elif not isinstance(value, list):
            reason = "invalid receipt balances"
            raise TypeError(reason)
        elif name in {"preBalances", "postBalances"} and any(
            not _uint(item) for item in value
        ):
            reason = "invalid native balance"
            raise ValueError(reason)
        elif name in {"preTokenBalances", "postTokenBalances"} and any(
            not isinstance(item, dict) for item in value
        ):
            reason = "invalid token balance"
            raise ValueError(reason)
        fields[name] = value
    if _uint(meta.get("fee")):
        fields["fee"] = meta["fee"]
    return fields


def _receipts(  # noqa: C901 - retain invalid observations alongside valid ones
    rows: list[dict], profiles: dict, submissions: set[str]
) -> tuple[dict, list]:
    grouped = {}
    failures = []
    for row in rows:
        signature = row["signature"]
        if not isinstance(signature, str) or not signature:
            reason = "invalid receipt signature column"
            raise ValueError(reason)
        issues = []
        observer = _kind(row["profile_id"], profiles)
        if observer != "live":
            issues.append(
                "receipt_observer_not_live"
                if observer in KINDS
                else "invalid_receipt_observer"
            )
        if signature not in submissions:
            issues.append("orphan_receipt")
        valid = row["commitment"] in ("confirmed", "finalized")
        if not valid:
            issues.append("invalid_receipt_commitment")
        fields = None
        try:
            result = _json_object(row["payload_json"])
            if (
                _digest(
                    {
                        "signature": signature,
                        "commitment": row["commitment"],
                        "profile_id": row["profile_id"],
                        "result": result,
                    }
                )
                != row["receipt_id"]
            ):
                valid = False
                issues.append("receipt_hash_mismatch")
            fields = _receipt_fields(result, signature)
            fee = fields.get("fee")
            if fee is None:
                issues.append(
                    "missing_receipt_fee"
                    if "fee" not in result["meta"]
                    else "invalid_receipt_fee"
                )
            if row["observed_fee_lamports"] != (str(fee) if fee is not None else None):
                valid = False
                issues.append("receipt_fee_column_mismatch")
        except (ValueError, TypeError, RecursionError):
            valid = False
            issues.append("invalid_receipt_payload")
        observation = {
            "fields": fields,
            "valid": valid,
            "observer": observer,
            "commitment": row["commitment"],
            "issues": issues,
        }
        grouped.setdefault(signature, []).append(observation)
        if issues:
            failures.append(
                {
                    "receipt_id": row["receipt_id"],
                    "signature": signature,
                    "issues": sorted(set(issues)),
                }
            )
    return grouped, failures


def _transaction(  # noqa: C901, PLR0912, PLR0915 - one signature's reconciliation state
    row: dict,
    intent: dict | None,
    outcome: dict | None,
    profiles: dict,
    observations: list[dict],
) -> dict:
    issues = []
    profile_id = row.get("evidence_profile_id")
    kind = _kind(profile_id, profiles)
    if kind in {"unattributed", "invalid"}:
        issues.append(
            "missing_submission_profile"
            if kind == "unattributed"
            else "invalid_submission_profile"
        )
    signer = intent["signer"] if intent else None
    valid_ledger = True
    try:
        _signature(row["signature"])
        _public_key(signer)
    except (ValueError, TypeError):
        issues.append("invalid_submission_identity")
        valid_ledger = False
    if intent is None:
        issues.append("missing_submission_intent")
    state = row.get("state")
    if state not in (None, "prepared", "submitted"):
        issues.append("invalid_submission_state")
        valid_ledger = False
        state = None
    status = outcome["status"] if outcome else None
    slot = outcome["slot"] if outcome else None
    if status not in (None, "success", "reverted", "expired", "unknown") or (
        outcome is not None and status is None
    ):
        issues.append("invalid_ledger_status")
        valid_ledger = False
        status = None
    if slot is not None and not _uint(slot):
        issues.append("invalid_ledger_slot")
        valid_ledger = False
    known = {}
    known_hashes = {}
    conflict = False
    fee_commitment = None
    for observation in observations:
        issues.extend(observation["issues"])
        if observation["observer"] != "live":
            continue
        if not observation["valid"]:
            conflict = True
            issues.append("invalid_live_receipt")
            continue
        fields = observation["fields"]
        for name, value in fields.items():
            value_hash = _digest({"value": value})
            if name in known_hashes and known_hashes[name] != value_hash:
                conflict = True
                issues.append("conflicting_receipt_" + name)
            else:
                known[name] = value
                known_hashes[name] = value_hash
        if "fee" in fields and (
            fee_commitment is None or observation["commitment"] == "finalized"
        ):
            fee_commitment = observation["commitment"]
    receipt_status = None
    if known:
        receipt_status = "success" if known["err"] is None else "reverted"
        if status in {"success", "reverted", "expired"} and status != receipt_status:
            conflict = True
            issues.append("ledger_receipt_status_conflict")
        if (
            status in {"success", "reverted", "expired"}
            and slot is not None
            and slot != known["slot"]
        ):
            conflict = True
            issues.append("ledger_receipt_slot_conflict")
        if known["accountKeys"][0] != signer:
            valid_ledger = False
            issues.append("receipt_payer_mismatch")
    else:
        issues.append("missing_live_receipt")
    if conflict or not valid_ledger:
        receipt_status = None
    fee = known.get("fee") if not conflict and valid_ledger else None
    if kind in {"dry_run", "simulation", "paper", "invalid"}:
        fee = None
    if fee is None:
        fee_commitment = None
    return {
        "signature": row["signature"],
        "intent_id": row["intent_id"],
        "signer": signer if isinstance(signer, str) else None,
        "submission_state": state,
        "submission_profile_id": profile_id if isinstance(profile_id, str) else None,
        "kind": kind,
        "ledger_status": status,
        "receipt_status": receipt_status,
        "receipt_slot": known["slot"] if receipt_status is not None else None,
        "receipt_observations": len(observations),
        "observed_network_fee_lamports": fee,
        "fee_commitment": fee_commitment,
        "issues": sorted(set(issues)),
    }


def summarize(path: Path) -> dict:
    """Return an integrity-checked report from one read-only, WAL-aware snapshot."""
    data, missing_tables, has_profile_column = _snapshot(path)
    profiles = _profiles(data["evidence_profiles"])
    submissions = {row["signature"] for row in data["submissions"]}
    intents = {row["intent_id"]: row for row in data["intents"]}
    outcomes = {row["signature"]: row for row in data["outcomes"]}
    receipts, receipt_issues = _receipts(
        data["evidence_receipts"], profiles, submissions
    )
    transactions = []
    for row in data["submissions"]:
        if not isinstance(row["intent_id"], str) or not row["intent_id"]:
            reason = "invalid submission intent column"
            raise ValueError(reason)
        transactions.append(
            _transaction(
                row,
                intents.get(row["intent_id"]),
                outcomes.get(row["signature"]),
                profiles,
                receipts.get(row["signature"], []),
            )
        )
    counts, event_issues = _events(
        data["evidence_events"],
        profiles,
        {row["signature"]: row for row in transactions},
    )
    live = [row for row in transactions if row["kind"] == "live"]
    observed = [row for row in live if row["observed_network_fee_lamports"] is not None]
    unattributed = sum(
        row["kind"] in {"unattributed", "invalid"} for row in transactions
    )
    subtotals = {
        commitment: sum(
            row["observed_network_fee_lamports"]
            for row in observed
            if row["fee_commitment"] == commitment
        )
        for commitment in ("confirmed", "finalized")
    }
    orphan_outcomes = bool(set(outcomes) - submissions)
    complete = (
        bool(live)
        and len(observed) == len(live)
        and all(row["fee_commitment"] == "finalized" for row in observed)
        and not unattributed
        and not missing_tables
        and has_profile_column
        and not event_issues
        and not orphan_outcomes
        and not any(
            set(row["issues"])
            - {
                "missing_receipt_fee",
                "invalid_receipt_fee",
                "receipt_observer_not_live",
            }
            for row in receipt_issues
        )
        and all(profile["valid"] for profile in profiles.values())
    )
    limitations = [
        "Network fees alone do not establish all-in return, fills, strategy validity, or capacity; no PnL is computed.",
        "Fee subtotals cover only consistent, attributable live receipts, including successful and reverted transactions; fee budgets are never observations.",
        "Confirmed fees are not final. Known subtotals can be partial; a null complete finalized total means coverage is not established.",
        "Profiles and source hashes prove only stored self-consistency, not authenticity, source verification, or preregistration.",
        "Event counts are observations, not unique trades. Receipt observation counts include excluded rows; repeated observations never multiply fees.",
        "Live event checks cover declared status, slot, gate action, intent and entry/exit links only; fill quantities, prices, token identity, event order and decision quality are not verified.",
        "Unknown or locally failed observations do not contradict later success. Expiry claims are compared with the retained ledger, not independently verified on chain.",
        "Coverage is limited to this ledger snapshot; unrecorded transactions cannot be ruled out.",
    ]
    if not has_profile_column:
        limitations.append(
            "Legacy submissions lack evidence_profile_id; original execution kind is unattributed."
        )
    if missing_tables:
        limitations.append(
            "Evidence tables are missing; legacy evidence is not backfilled or inferred."
        )
    if orphan_outcomes:
        limitations.append("Orphan ledger outcomes make submission coverage uncertain.")
    return {
        "version": 2,
        "missing_evidence_tables": missing_tables,
        "profiles": list(profiles.values()),
        "events_by_kind": counts,
        "event_issues": event_issues,
        "receipt_issues": receipt_issues,
        "transactions": transactions,
        "live_network_fees": {
            "submission_count": len(live),
            "observed_count": len(observed),
            "unknown_count": len(live) - len(observed),
            "confirmed_subtotal_lamports": subtotals["confirmed"],
            "finalized_subtotal_lamports": subtotals["finalized"],
            "known_subtotal_lamports": sum(subtotals.values()),
            "complete_finalized_total_lamports": subtotals["finalized"]
            if complete
            else None,
        },
        "unattributed_submission_count": unattributed,
        "limitations": limitations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", type=Path, help="existing transaction ledger SQLite file"
    )
    args = parser.parse_args()
    try:
        report = summarize(args.path)
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
        # SQLite/provider/JSON errors can contain private paths or stored content.
        print(
            "Could not audit ledger: unreadable file, invalid data, or unsupported schema.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(encoded)


if __name__ == "__main__":
    main()
