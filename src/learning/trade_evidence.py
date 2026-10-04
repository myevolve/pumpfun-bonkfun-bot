"""Audit durable trade receipts, wallet balance changes and observed network fees."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from datetime import UTC, datetime
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
    if (
        _uint(slot)
        and transaction["receipt_slot"] is not None
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
    slot = details.get("slot")
    if slot is not None and not _uint(slot):
        issues.append("invalid_event_slot")
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
    issues.extend(_outcome_claim_issues(transaction, status, slot))
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


def _event_claims(
    category: str | None, payload: dict, submissions: dict, by_intent: dict
) -> dict:
    """Project typed claims and exact recorded links, not a causal timeline."""
    links: dict[str, set[str]] = {}
    if category in ("decision", "trade_result", "chain_outcome", "position_closed"):
        intent = payload.get("intent_id")
        if category == "decision" and isinstance(intent, str):
            for transaction in by_intent.get(intent, []):
                links.setdefault(transaction["signature"], set()).add("decision_intent")
        for signature in _event_signatures(payload):
            if isinstance(signature, str) and signature in submissions:
                links.setdefault(signature, set()).add("signature_reference")
        position = payload.get("position")
        if isinstance(position, dict):
            entry = position.get("position_id")
            if isinstance(entry, str) and entry in submissions:
                links.setdefault(entry, set()).add("position_entry")
    details = payload.get("result") if category == "trade_result" else payload
    details = details if isinstance(details, dict) else {}
    gate = payload.get("gate")
    gate = gate if isinstance(gate, dict) else {}
    action = payload.get("action")
    status = details.get("status")
    return {
        "declared_action": action if action in ("buy", "sell", "skip") else None,
        "reported_status": status
        if status in ("success", "reverted", "expired", "unknown", "failed")
        else None,
        "reported_success": details.get("success")
        if type(details.get("success")) is bool
        else None,
        "reported_slot": details.get("slot") if _uint(details.get("slot")) else None,
        "gate_accepted": gate.get("accept")
        if type(gate.get("accept")) is bool
        else None,
        "submission_links": [
            {"signature": signature, "relations": sorted(links[signature])}
            for signature in sorted(links)
        ],
    }


def _events(  # noqa: C901, PLR0912 - validate each observation before counting it
    rows: list[dict], profiles: dict, submissions: dict[str, dict]
) -> tuple[dict, list, list]:
    if not rows:
        return {}, [], []
    by_intent = {}
    for transaction in submissions.values():
        by_intent.setdefault(transaction["intent_id"], []).append(transaction)
    counts = {}
    failures = []
    observations = []
    for row in rows:
        issues = []
        payload = {}
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
        issues = sorted(set(issues))
        if issues:
            failures.append({"event_id": row["event_id"], "issues": issues})
        observations.append(
            {
                "event_id": row["event_id"],
                "profile_id": row["profile_id"]
                if isinstance(row["profile_id"], str)
                else None,
                "kind": kind,
                "category": category if isinstance(category, str) else None,
                **_event_claims(
                    category,
                    payload if valid and kind in KINDS else {},
                    submissions,
                    by_intent,
                ),
                "issues": issues,
            }
        )
    return (
        {kind: dict(sorted(counts[kind].items())) for kind in sorted(counts)},
        failures,
        observations,
    )


def _instruction_fields(instructions: list) -> dict:  # noqa: C901, PLR0912 - RPC instruction variants share one fact projection
    """Compare available instruction facts without decoding program-specific data."""
    fields = {"instructionCount": len(instructions)}
    for index, instruction in enumerate(instructions):
        if not isinstance(instruction, dict) or (
            instruction.get("programIdIndex") is None
            and instruction.get("programId") is None
        ):
            reason = "missing instruction program"
            raise ValueError(reason)
        for name in (
            "programIdIndex",
            "programId",
            "accounts",
            "data",
            "parsed",
            "stackHeight",
        ):
            value = instruction.get(name)
            if value is None:
                continue
            field_name = name
            if name in {"programIdIndex", "stackHeight"}:
                bits = 8 if name == "programIdIndex" else 32
                if not _uint(value) or value >= 2**bits:
                    reason = "invalid instruction index or height"
                    raise ValueError(reason)
            elif name == "programId":
                value = _public_key(value)
            elif name == "accounts":
                if not isinstance(value, list):
                    reason = "invalid instruction accounts"
                    raise TypeError(reason)
                if instruction.get("programIdIndex") is not None:
                    if any(not _uint(item) or item >= 2**8 for item in value):
                        reason = "invalid instruction account index"
                        raise ValueError(reason)
                    field_name = "accountIndexes"
                else:
                    value = [_public_key(item) for item in value]
            elif not isinstance(value, str if name == "data" else dict):
                reason = "invalid instruction data"
                raise TypeError(reason)
            fields[f"instruction_{index}_{field_name}"] = value
    return fields


def _message_issues(fields: dict) -> list[str]:  # noqa: C901 - one pass over retained instruction references
    """Resolve compiled references using all retained, consistent account evidence."""
    issues = []
    static = fields.get("staticAccountKeys", [])
    complete = "resolvedAccountKeys" in fields
    keys = fields.get("resolvedAccountKeys", static)
    if complete and keys[: len(static)] != static:
        issues.append("conflicting_receipt_staticAccountKeys")
    for index in range(fields.get("instructionCount", 0)):
        prefix = f"instruction_{index}_"
        for index_name, address_name in (
            ("programIdIndex", "programId"),
            ("accountIndexes", "accounts"),
        ):
            indexes = fields.get(prefix + index_name)
            if indexes is None:
                continue
            addresses = fields.get(prefix + address_name)
            if index_name == "programIdIndex":
                indexes = [indexes]
                addresses = [addresses] if addresses is not None else None
            if addresses is not None and len(addresses) != len(indexes):
                issues.append("conflicting_receipt_" + prefix + address_name)
                continue
            for offset, account_index in enumerate(indexes):
                if account_index >= len(keys):
                    if complete:
                        issues.append("invalid_instruction_account_index")
                elif addresses is not None and addresses[offset] != keys[account_index]:
                    issues.append("conflicting_receipt_" + prefix + address_name)
    return issues


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
    if not isinstance(keys, list) or len(keys) < len(signatures):
        reason = "insufficient account keys"
        raise ValueError(reason)
    parsed_keys = all(isinstance(item, dict) for item in keys)
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
        "feePayer": keys[0],
        "err": meta["err"],
    }
    if not parsed_keys:
        fields["staticAccountKeys"] = keys
    for index, account in enumerate(message["accountKeys"]):
        if isinstance(account, dict) != parsed_keys:
            reason = "mixed account key formats"
            raise ValueError(reason)
        if not parsed_keys:
            continue
        signer = account.get("signer")
        writable = account.get("writable")
        if (signer is not None and signer is not (index < len(signatures))) or (
            writable is not None
            and (type(writable) is not bool or (index == 0 and not writable))
        ):
            reason = "invalid parsed account permissions"
            raise ValueError(reason)
        if writable is not None:
            fields[f"accountKeys_{index}_writable"] = writable
    for name in ("recentBlockhash", "instructions", "addressTableLookups", "header"):
        if name in message and message[name] is not None:
            if name == "recentBlockhash":
                _public_key(
                    message[name]
                )  # A blockhash is also a base58-encoded 32-byte value.
            elif name == "header":
                header = message[name]
                if (
                    not isinstance(header, dict)
                    or any(
                        not _uint(header.get(field)) or header[field] >= 2**8
                        for field in (
                            "numRequiredSignatures",
                            "numReadonlySignedAccounts",
                            "numReadonlyUnsignedAccounts",
                        )
                    )
                    or header["numRequiredSignatures"] != len(signatures)
                    or header["numReadonlySignedAccounts"] >= len(signatures)
                    or header["numReadonlyUnsignedAccounts"]
                    > len(keys) - len(signatures)
                ):
                    reason = "invalid message header"
                    raise ValueError(reason)
            elif not isinstance(message[name], list):
                reason = "invalid message field"
                raise TypeError(reason)
            if name == "instructions":
                fields.update(_instruction_fields(message[name]))
            else:
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
        elif name in {"preTokenBalances", "postTokenBalances"}:
            if any(not isinstance(item, dict) for item in value):
                reason = "invalid token balance"
                raise ValueError(reason)
            # Compare raw units, not RPC display amounts or formatting.
            balances = []
            for row in value:
                amount = row.get("uiTokenAmount")
                if isinstance(amount, dict):
                    balances.append(
                        {
                            **row,
                            "uiTokenAmount": {
                                key: item
                                for key, item in amount.items()
                                if key not in {"uiAmount", "uiAmountString"}
                            },
                        }
                    )
                else:
                    balances.append(row)
            # Token rows are indexed records; keep duplicates for balance validation.
            if all(_uint(row.get("accountIndex")) for row in balances):
                balances.sort(key=lambda row: row["accountIndex"])
            value = balances
        fields[name] = value
    if _uint(meta.get("fee")):
        fields["fee"] = meta["fee"]
    loaded = fields.get("loadedAddresses")
    if parsed_keys:
        fields["resolvedAccountKeys"] = keys
    elif loaded is not None:
        fields["resolvedAccountKeys"] = [
            *keys,
            *loaded["writable"],
            *loaded["readonly"],
        ]
    elif not fields.get("addressTableLookups"):
        fields["resolvedAccountKeys"] = keys
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


def _token_balance_changes(fields: dict, keys: list[str], signer: str) -> list[dict]:
    """Read raw owner balances; only proven account creation/closure supplies zero."""
    sides = []
    for field in ("preTokenBalances", "postTokenBalances"):
        rows = {}
        for row in fields[field]:
            index = row.get("accountIndex")
            amount = row.get("uiTokenAmount")
            if (
                not _uint(index)
                or index >= len(keys)
                or index in rows
                or not isinstance(amount, dict)
            ):
                reason = "invalid token account balance"
                raise ValueError(reason)
            raw = amount.get("amount")
            decimals = amount.get("decimals")
            if (
                not isinstance(raw, str)
                or re.fullmatch(r"[0-9]{1,20}", raw) is None
                or not _uint(int(raw))
                or type(decimals) is not int
                or not 0 <= decimals < 2**8
            ):
                reason = "invalid raw token amount"
                raise ValueError(reason)
            rows[index] = {
                "owner": _public_key(row.get("owner")),
                "mint": _public_key(row.get("mint")),
                "program_id": _public_key(row["programId"])
                if "programId" in row
                else None,
                "decimals": decimals,
                "amount": int(raw),
            }
        sides.append(rows)
    before, after = sides
    changes = []
    for index in sorted(before.keys() | after.keys()):
        pre, post = before.get(index), after.get(index)
        identity = pre if pre is not None else post
        if (
            pre is not None
            and post is not None
            and any(
                pre[field] != post[field]
                for field in ("owner", "mint", "program_id", "decimals")
            )
        ):
            reason = "token account identity changed"
            raise ValueError(reason)
        pre_amount = pre["amount"] if pre is not None else 0
        post_amount = post["amount"] if post is not None else 0
        if fields["err"] is not None and pre_amount != post_amount:
            reason = "reverted transaction changed token balance"
            raise ValueError(reason)
        if identity["owner"] != signer:
            continue
        if (pre is None and fields["preBalances"][index] != 0) or (
            post is None and fields["postBalances"][index] != 0
        ):
            reason = "unproven token account creation or closure"
            raise ValueError(reason)
        changes.append(
            {
                "account": keys[index],
                "mint": identity["mint"],
                "program_id": identity["program_id"],
                "decimals": identity["decimals"],
                "pre_amount_raw": pre_amount,
                "post_amount_raw": post_amount,
                "change_raw": post_amount - pre_amount,
            }
        )
    return changes


def _economic_receipt(fields: dict, commitments: dict[str, str], signer: str) -> dict:
    """Expose observed movements, never infer fills or classify transfers as profit."""
    report = {"native": None, "tokens": None, "token_commitment": None, "issues": []}
    required = {"resolvedAccountKeys", "preBalances", "postBalances"}
    if not required <= fields.keys():
        report["issues"].append(
            "missing_loaded_addresses"
            if "resolvedAccountKeys" not in fields
            else "missing_native_balances"
        )
        return report
    keys = fields["resolvedAccountKeys"]
    pre, post = fields["preBalances"], fields["postBalances"]
    if len(keys) != len(set(keys)) or len(pre) != len(keys) or len(post) != len(keys):
        report["issues"].append("invalid_native_balance_layout")
        return report
    fee = fields.get("fee")
    native_required = required | ({"fee"} if fee is not None else set())
    report["native"] = {
        "pre_lamports": pre[0],
        "post_lamports": post[0],
        "change_lamports": post[0] - pre[0],
        "change_excluding_network_fee_lamports": post[0] - pre[0] + fee
        if fee is not None
        else None,
        "commitment": "finalized"
        if all(commitments[name] == "finalized" for name in native_required)
        else "confirmed",
    }
    required.update(("preTokenBalances", "postTokenBalances"))
    if not required <= fields.keys():
        report["issues"].append("missing_token_balances")
        return report
    try:
        report["tokens"] = _token_balance_changes(fields, keys, signer)
    except (ValueError, TypeError):
        report["issues"].append("invalid_or_incomplete_token_balances")
        return report
    report["token_commitment"] = (
        "finalized"
        if all(commitments[name] == "finalized" for name in required)
        else "confirmed"
    )
    return report


def _orphan_outcome(row: dict) -> dict:
    """Project unassigned ledger claims without inferring a submission or receipt."""
    issues = []
    try:
        signature = _signature(row["signature"])
    except (ValueError, TypeError):
        signature = None
        issues.append("invalid_outcome_signature")
    status = row["status"]
    if status not in ("success", "reverted", "expired", "unknown"):
        status = None
        issues.append("invalid_ledger_status")
    slot = row["slot"]
    if slot is not None and not _uint(slot):
        slot = None
        issues.append("invalid_ledger_slot")
    return {
        "signature": signature,
        "ledger_status": status,
        "ledger_slot": slot,
        "issues": sorted(issues),
    }


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
    field_commitments = {}
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
            if field_commitments.get(name) != "finalized":
                field_commitments[name] = observation["commitment"]
        if "fee" in fields and (
            fee_commitment is None or observation["commitment"] == "finalized"
        ):
            fee_commitment = observation["commitment"]
    receipt_status = None
    if known:
        message_issues = _message_issues(known)
        if message_issues:
            conflict = True
            issues.extend(message_issues)
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
        if known["feePayer"] != signer:
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
        "economic_receipt": _economic_receipt(known, field_commitments, signer)
        if kind == "live" and receipt_status is not None
        else None,
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
    by_signature = {row["signature"]: row for row in transactions}
    counts, event_issues, events = _events(
        data["evidence_events"], profiles, by_signature
    )
    for transaction in transactions:
        transaction["event_ids"] = []
    for event in events:
        for link in event["submission_links"]:
            by_signature[link["signature"]]["event_ids"].append(event["event_id"])
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
    orphan_outcomes = [
        _orphan_outcome(outcome)
        for signature, outcome in outcomes.items()
        if signature not in submissions
    ]
    finalized_total_blockers = [
        {"code": code, "reason": reason, "next_check": next_check}
        for code, satisfied, reason, next_check in (
            (
                "no_attributable_live_submissions",
                bool(live),
                "No attributable live submissions are retained.",
                "Review original submission provenance. A zero observed subtotal is not a proven session total.",
            ),
            (
                "unknown_live_fees",
                len(observed) == len(live),
                "Some attributable live submissions have unknown network fees.",
                "Inspect submissions with unknown observed fees and their receipt issues. Fee budgets cannot replace receipts.",
            ),
            (
                "unfinalized_live_fees",
                all(row["fee_commitment"] == "finalized" for row in observed),
                "Some observed fees lack finalized evidence.",
                "Check retained finalized receipts for those signatures. A confirmed fee must not be relabeled finalized.",
            ),
            (
                "unattributed_or_invalid_submissions",
                not unattributed,
                "Some submissions lack valid original execution provenance.",
                "Inspect submission profile issues. Later observers cannot establish the original execution kind.",
            ),
            (
                "missing_evidence_tables",
                not missing_tables,
                "The ledger is missing evidence tables.",
                "Review missing_evidence_tables. Preserve the legacy ledger; missing history cannot be inferred.",
            ),
            (
                "missing_submission_profile_column",
                has_profile_column,
                "The submission schema has no evidence-profile column.",
                "Treat original execution provenance as unavailable. Adding a column would not establish historical attribution.",
            ),
            (
                "event_integrity_issues",
                not event_issues,
                "Lifecycle observations have unresolved validation issues.",
                "Inspect linked and unlinked event checks. Disputed claims must not erase independently observed fees.",
            ),
            (
                "orphan_ledger_outcomes",
                not orphan_outcomes,
                "Some ledger outcomes have no retained submission.",
                "Inspect orphan_outcomes and compare their signatures with retained submissions. Preserve unassigned outcomes rather than dropping them.",
            ),
            (
                "receipt_integrity_issues",
                not any(
                    set(row["issues"])
                    - {
                        "missing_receipt_fee",
                        "invalid_receipt_fee",
                        "receipt_observer_not_live",
                    }
                    for row in receipt_issues
                ),
                "Receipt validation or coverage issues remain.",
                "Inspect receipt_issues and matching submissions. Preserve rejected, conflicting and orphan observations.",
            ),
            (
                "invalid_evidence_profiles",
                all(profile["valid"] for profile in profiles.values()),
                "Some stored evidence profiles fail validation.",
                "Inspect profile issues and stored hashes. Do not replace historical provenance with a current profile.",
            ),
        )
        if not satisfied
    ]
    economics = [row["economic_receipt"] for row in live]
    native_count = sum(
        row is not None and row["native"] is not None for row in economics
    )
    token_count = sum(
        row is not None and row["tokens"] is not None for row in economics
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
        "Economic receipts show signer native and owner-attributed token balance changes, not trade fills, spendable inventory, or realized PnL.",
        "Native changes already include network fees. The fee-excluded change adds meta.fee back once; it still includes rent, wrapping, tips and other transfers, which are not separately attributed.",
        "Token changes are per account in raw units; WSOL is not combined with native SOL or valued as profit. Missing metadata, changed ownership and unproven account creation/closure remain unknown.",
        "Balance finality is no stronger than the observations containing its inputs. A finalized fee alone cannot finalize earlier balance observations.",
        "Lifecycle links are recorded associations, not a timeline or proof of causality. A decision intent may cover multiple wire attempts; result signatures never imply outcomes for sibling attempts.",
        "Event projections omit free text and raw payloads. Invalid JSON, content hashes or profiles cannot supply claims or links; consistent but disputed claims remain linked with their issues.",
        "Finalized-total blockers explain the existing network-fee completeness checks only. Their absence is not trading readiness, complete balance accounting, or proof of profit; next checks do not fetch, repair, or relabel evidence.",
        "snapshot.generated_utc is the local report generation time, not a receipt time, ledger update watermark, or proof of market freshness. Cached reports retain their original generation time.",
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
        limitations.append(
            "Orphan ledger outcomes make submission coverage uncertain. Their statuses "
            "and slots are stored claims, not verified receipts; no submission, "
            "original execution kind, fee or balance is inferred."
        )
    return {
        "version": 7,
        "snapshot": {"generated_utc": datetime.now(UTC).isoformat()},
        "missing_evidence_tables": missing_tables,
        "profiles": list(profiles.values()),
        "events_by_kind": counts,
        "events": events,
        "event_issues": event_issues,
        "receipt_issues": receipt_issues,
        "orphan_outcomes": orphan_outcomes,
        "transactions": transactions,
        "live_network_fees": {
            "submission_count": len(live),
            "observed_count": len(observed),
            "unknown_count": len(live) - len(observed),
            "confirmed_subtotal_lamports": subtotals["confirmed"],
            "finalized_subtotal_lamports": subtotals["finalized"],
            "known_subtotal_lamports": sum(subtotals.values()),
            "complete_finalized_total_lamports": subtotals["finalized"]
            if not finalized_total_blockers
            else None,
            "finalized_total_blockers": finalized_total_blockers,
        },
        "live_balance_coverage": {
            "submission_count": len(live),
            "native_observed_count": native_count,
            "native_unknown_count": len(live) - native_count,
            "token_observed_count": token_count,
            "token_unknown_count": len(live) - token_count,
        },
        "unattributed_submission_count": unattributed,
        "limitations": limitations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", type=Path, help="existing transaction ledger SQLite file"
    )
    parser.add_argument(
        "--output", type=Path, help="save JSON evidence snapshot; refuses to overwrite"
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
    if args.output is not None:
        try:
            with args.output.open("x", encoding="utf-8") as destination:
                print(encoded, file=destination)
        except OSError as exc:
            parser.exit(1, f"Cannot save evidence report: {type(exc).__name__}\n")
    print(encoded)


if __name__ == "__main__":
    main()
