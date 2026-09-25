#!/usr/bin/env python3
"""Replay source-locked native account marks; event-derived entries, never fills.

Both outputs are new exclusive-create files. --self-check is entirely offline.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import evaluate_creation_paper as base
import record_lifecycles as lifecycle  # noqa: F401 - align collector's exact source closure
from decode_provider_response import (
    RATE_LIMIT_MAX_ATTEMPTS,
    RATE_LIMIT_MAX_DELAY_SECONDS,
    RATE_LIMIT_MAX_ELAPSED_SECONDS,
    rate_limit_retry_delay,
)

from core.pubkeys import WSOL_MINT, SystemAddresses
from platforms.pumpfun.address_provider import PumpFunAddressProvider
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from platforms.pumpfun.pumpswap import _decode_mint_account
from utils.idl_parser import IDLParser

if TYPE_CHECKING:
    from collections.abc import Callable

# Standalone protocol bounds, existing private decoders and offline assertions are intentional.
# ruff: noqa: PLR2004, S101, SLF001

TARGET_KEYS = {
    "target_id",
    "mint",
    "arm",
    "stage",
    "entry_at",
    "entry_slot",
    "due_at",
    "due_slot",
    "trigger_request_sequence",
}
UNKNOWN = [
    "inclusion",
    "entry_executability",
    "hypothetical_buy_impact",
    "processed_fork",
    "current_native_fee_attestation",
    "rent_net",
    "tips",
    "failed_attempts",
    "infrastructure",
    "actual_entry_and_exit_execution",
]


def marks_policy() -> dict:
    return {
        "method": "getMultipleAccounts",
        "commitment": "processed",
        "warmup_and_split": "base_lock",
        "head_availability_clock": "slot_observed_monotonic_at_account_recorder_emit",
        "head_freshness_and_entry_exit_clock": "received_monotonic",
        "trigger_delay_seconds": 10,
        "exit_delay_slots": 1,
        "maximum_mark_lateness_seconds": 2,
        "maximum_exit_head_wait_seconds": 5,
        "minimum_request_spacing_seconds": 1.0,
        "maximum_requests": 8000,
        "maximum_targets_per_batch": 32,
        "maximum_response_bytes": 8388608,
        "maximum_marks_bytes": 134217728,
        "rpc_timeout_seconds": 2,
        "rate_limit_max_attempts": RATE_LIMIT_MAX_ATTEMPTS,
        "rate_limit_max_delay_seconds": RATE_LIMIT_MAX_DELAY_SECONDS,
        "rate_limit_max_elapsed_seconds": RATE_LIMIT_MAX_ELAPSED_SECONDS,
        "rate_limit_retry_policy": "complete_bounded_http429_only_global_cooldown_physical_request_accounting",
        "public_payer": "9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe",
        "fee_basis": "prior_native_attestation_scenario_digest_must_match_each_observed_fee_account",
    }


def source_hashes() -> dict[str, str]:
    result = base.sources()
    for name in (
        "evaluate_creation_account_marks.py",
        "record_creation_account_marks.py",
        "decode_provider_response.py",
    ):
        path = Path(__file__).with_name(name)
        result[str(path.relative_to(base.ROOT))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return dict(sorted(result.items()))


def validate_marks_lock(base_lock_path: Path, marks_lock_path: Path) -> dict:
    original = base_lock_path.read_bytes()
    lock = base.strict_json(original)
    base.validate_lock(
        lock, base.ROOT / lock["tape_path"], base.ROOT / lock["slot_clock_path"]
    )
    marks = base.strict_json(marks_lock_path.read_bytes())
    base.exact(
        marks,
        {"schema_version", "run_id", "base_lock_sha256", "policy", "source_sha256"},
        "marks_lock",
    )
    base.require(
        type(marks["schema_version"]) is int and marks["schema_version"] == 1,
        "marks_lock_version",
    )
    base.require(marks["run_id"] == lock["run_id"], "marks_lock_run_mismatch")
    base.require(
        marks["base_lock_sha256"] == hashlib.sha256(original).hexdigest(),
        "base_lock_hash_mismatch",
    )
    policy = marks_policy()
    base.require(
        marks["policy"] == policy
        and all(type(marks["policy"][k]) is type(v) for k, v in policy.items()),
        "marks_policy_mismatch",
    )
    base.require(
        marks["source_sha256"] == source_hashes(), "marks_source_closure_mismatch"
    )
    return marks


def addresses(targets: list[dict], purpose: str) -> list[str]:
    fee = str(base.PumpFunAddresses.find_fee_config())
    if purpose == "audit_before":
        return [marks_policy()["public_payer"], fee]
    if purpose == "audit_after":
        return [marks_policy()["public_payer"]]
    base.require(purpose == "marks", "unknown_rpc_purpose")
    result = [fee]
    provider = PumpFunAddressProvider()
    for mint in sorted({t["mint"] for t in targets}):
        pubkey = base.Pubkey.from_string(mint)
        base.require(str(pubkey) == mint, "noncanonical_mint_text")
        result.extend((str(provider.derive_pool_address(pubkey)), mint))
    return result


def account(value: object) -> base.Account:
    base.require(isinstance(value, dict), "account_missing")
    base.require(value.get("executable") is False, "account_executable")
    data = value.get("data")
    base.require(
        isinstance(data, list) and len(data) == 2 and data[1] == "base64",
        "account_encoding",
    )
    decoded = base64.b64decode(data[0], validate=True)
    if "space" in value:
        base.require(
            base.raw(value["space"], "account_space") == len(decoded),
            "account_space_mismatch",
        )
    return base.Account(
        base.raw(value["lamports"], "lamports"),
        decoded,
        base.Pubkey.from_string(value["owner"]),
        False,  # noqa: FBT003 - solders account wire constructor
        base.raw(value["rentEpoch"], "rent_epoch"),
    )


def mint_extensions(value: base.Account, mint: base.Pubkey) -> tuple[int, list[int]]:
    program, supply, decimals = _decode_mint_account(value, mint)
    base.require(decimals == 6, "mint_not_six_decimals")
    data = value.data
    base.require(
        struct.unpack_from("<I", data, 0)[0] in (0, 1)
        and struct.unpack_from("<I", data, 46)[0] in (0, 1),
        "mint_option_malformed",
    )
    if program == SystemAddresses.TOKEN_PROGRAM or len(data) == 82:
        base.require(len(data) == 82, "legacy_mint_layout")
        return supply, []
    base.require(
        len(data) >= 166 and not any(data[82:165]) and data[165] == 1,
        "token2022_mint_layout",
    )
    offset, kinds = 166, []
    while offset < len(data):
        if not any(data[offset:]):
            break
        base.require(offset + 4 <= len(data), "mint_extension_header")
        kind, size = struct.unpack_from("<HH", data, offset)
        offset += 4
        base.require(
            kind not in kinds and offset + size <= len(data), "mint_extension_malformed"
        )
        # Only inert metadata extensions are modeled; transfer-affecting extensions are unknown.
        base.require(kind in (18, 19), "unsupported_mint_extension")
        base.require(
            (kind == 18 and size == 64) or (kind == 19 and size >= 80),
            "mint_metadata_layout",
        )
        if kind == 19:
            base.require(
                data[offset + 32 : offset + 64] == bytes(mint), "metadata_mint_mismatch"
            )
        kinds.append(kind)
        offset += size
    return supply, kinds


def native_response(
    record: dict, expected_addresses: list[str], minimum_slot: int
) -> dict:
    request = record["request"]
    base.exact(request, {"jsonrpc", "id", "method", "params"}, "rpc_request")
    base.require(
        request["jsonrpc"] == "2.0" and request["method"] == "getMultipleAccounts",
        "rpc_method",
    )
    params = request["params"]
    base.require(
        isinstance(params, list)
        and len(params) == 2
        and params[0] == expected_addresses,
        "rpc_canonical_addresses",
    )
    base.exact(params[1], {"encoding", "commitment", "minContextSlot"}, "rpc_options")
    base.require(
        params[1]
        == {
            "encoding": "base64",
            "commitment": "processed",
            "minContextSlot": minimum_slot,
        },
        "rpc_min_context_or_options",
    )
    base.require(type(params[1]["minContextSlot"]) is int, "rpc_min_context_type")
    blob = record.get("response_base64")
    if blob is None:
        base.require(
            record.get("error_type") is not None
            or record.get("error_code") is not None,
            "missing_rpc_outcome",
        )
        base.require(
            record.get("error_code") is None or type(record["error_code"]) is int,
            "rpc_error_code_type",
        )
        error_type = record.get("error_type")
        base.require(
            error_type is None
            or (
                isinstance(error_type, str)
                and error_type.isascii()
                and error_type.replace("_", "").isalnum()
            ),
            "rpc_error_type_not_redacted",
        )
        base.raw(record["response_bytes"], "failed_response_bytes")
        base.require(
            isinstance(record["response_sha256"], str)
            and len(record["response_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in record["response_sha256"]),
            "failed_response_digest",
        )
        return {
            "error": "rpc_failure",
            "error_type": record.get("error_type"),
            "error_code": record.get("error_code"),
        }
    raw_bytes = base64.b64decode(blob, validate=True)
    base.require(
        len(raw_bytes) <= marks_policy()["maximum_response_bytes"], "rpc_body_limit"
    )
    base.require(
        len(raw_bytes) == base.raw(record["response_bytes"], "response_bytes")
        and hashlib.sha256(raw_bytes).hexdigest() == record["response_sha256"],
        "rpc_body_hash_mismatch",
    )
    response = base.strict_json(raw_bytes)
    base.require(
        response.get("jsonrpc") == "2.0"
        and type(response.get("id")) is type(request["id"])
        and response.get("id") == request["id"],
        "rpc_response_identity",
    )
    base.require(
        "error" not in response
        and record.get("error_code") is None
        and record.get("error_type") is None,
        "successful_body_with_error",
    )
    result = response["result"]
    slot = base.raw(result["context"]["slot"], "rpc_context_slot")
    base.require(slot >= minimum_slot, "rpc_context_behind_request")
    base.require(
        isinstance(result["value"], list)
        and len(result["value"]) == len(expected_addresses),
        "rpc_account_count",
    )
    return {
        "slot": slot,
        "accounts": dict(zip(expected_addresses, result["value"], strict=True)),
    }


def target_entry(coin: dict, arm: str, heads: base.Heads) -> tuple[float, int]:
    at = base.clock(coin["create_received_monotonic"], "create_time")
    slot = base.raw(coin["create_slot"], "create_slot")
    if arm == "C":
        return at, slot
    index = heads.next(at, slot + base.POLICY["d2_delay_slots"])
    return heads.times[index], heads.slots[index]


def check_target(target: dict, coins: dict, heads: base.Heads, outcomes: dict) -> None:
    base.exact(target, TARGET_KEYS, "target")
    mint, arm, stage = target["mint"], target["arm"], target["stage"]
    base.require(
        mint in coins and arm in ("C", "D2") and stage in ("trigger", "exit"),
        "target_identity",
    )
    base.require(target["target_id"] == f"{mint}:{arm}:{stage}", "target_id_mismatch")
    base.require(type(target["due_slot"]) is int, "target_due_slot_type")
    base.raw(target["due_slot"], "target_due_slot")
    if target["entry_at"] is not None:
        base.clock(target["entry_at"], "target_entry_at")
        base.require(type(target["entry_slot"]) is int, "target_entry_slot_type")
        base.raw(target["entry_slot"], "target_entry_slot")
    if target["due_at"] is not None:
        base.clock(target["due_at"], "target_due_at")
    if target["trigger_request_sequence"] is not None:
        base.require(
            type(target["trigger_request_sequence"]) is int, "trigger_sequence_type"
        )
    try:
        at, slot = target_entry(coins[mint], arm, heads)
    except ValueError as exc:
        base.require(
            str(exc) == "required_future_head_unavailable"
            and arm == "D2"
            and stage == "trigger",
            "target_entry_unavailable",
        )
        base.require(
            target["entry_at"] is None
            and target["entry_slot"] is None
            and target["due_at"] is None
            and target["due_slot"] == coins[mint]["create_slot"] + 2
            and target["trigger_request_sequence"] is None,
            "missing_entry_target_mismatch",
        )
        return
    base.require(
        target["entry_at"] == at and target["entry_slot"] == slot,
        "target_entry_time_mismatch",
    )
    if stage == "trigger":
        base.require(
            target["due_at"] == at + 10
            and target["due_slot"] == slot
            and target["trigger_request_sequence"] is None,
            "trigger_target_mismatch",
        )
        return
    previous = outcomes.get(f"{mint}:{arm}:trigger")
    base.require(
        previous is not None
        and previous.get("error") is None
        and previous.get("native", {}).get("slot") is not None,
        "exit_without_successful_trigger",
    )
    rpc = previous["rpc"]
    base.require(
        target["trigger_request_sequence"] == rpc["sequence"],
        "exit_trigger_link_mismatch",
    )
    minimum = previous["native"]["slot"] + 1
    response_at = rpc["response_received_monotonic"]
    base.require(target["due_slot"] == minimum, "exit_slot_not_next")
    next_index = next(
        (
            i
            for i in range(bisect_right(heads.times, response_at), len(heads.times))
            if heads.slots[i] >= minimum
        ),
        None,
    )
    if next_index is None or heads.times[next_index] - response_at > 5:
        base.require(target["due_at"] is None, "exit_head_wait_expired")
    else:
        base.require(
            target["due_at"] == heads.times[next_index], "exit_future_assignment"
        )


def response_expired(target: dict, received: float) -> bool:
    """Keep each mark's receipt deadline independent of its batch transport."""
    return received > target["due_at"] + 2


def head_observation_times(heads: base.Heads) -> list[float]:
    """Index recorder delivery separately from the original stream receipt evidence."""
    times = []
    for row, received in zip(heads.rows, heads.times, strict=True):
        observed = base.clock(row["observed_monotonic"], "head_observed")
        base.require(observed >= received, "head_observed_before_received")
        base.require(not times or observed >= times[-1], "head_observation_reordered")
        times.append(observed)
    return times


def check_rpc_clock(
    record: dict, targets: list[dict], heads: base.Heads, observed_times: list[float]
) -> int:
    start = base.clock(record["request_started_monotonic"], "rpc_start")
    received = base.clock(record["response_received_monotonic"], "rpc_received")
    headers = record.get("response_headers_monotonic")
    base.require(received >= start, "rpc_clock_reversed")
    if headers is not None:
        base.require(
            start <= base.clock(headers, "rpc_headers") <= received, "rpc_header_clock"
        )
    if record.get("response_base64") is not None:
        base.require(
            headers is not None and received - start <= 2, "rpc_response_deadline"
        )
    index = bisect_right(observed_times, start) - 1
    if index < 0:
        base.require(
            not targets
            and record["head_slot_at_start"] is None
            and record["head_received_at_start"] is None,
            "rpc_head_missing",
        )
        minimum = 0
    else:
        base.require(
            record["head_slot_at_start"] == heads.slots[index]
            and record["head_received_at_start"] == heads.times[index],
            "rpc_head_identity",
        )
        minimum = heads.slots[index]
    for target in targets:
        base.require(
            target["due_at"] is not None and target["entry_slot"] is not None,
            "rpc_missing_due_head",
        )
        due = base.clock(target["due_at"], "target_due")
        base.require(due <= start <= due + 2, "rpc_future_or_late_target")
        minimum = max(minimum, target["entry_slot"], target["due_slot"])
    return minimum


def retry_cooldown(record: dict) -> float | None:
    """Verify the durable HTTP-only retry evidence, never trust a recorded delay."""
    base.require(
        record["http_status"] == 429
        and record["body_complete"] is True
        and record["response_base64"] is None
        and record["error_type"] in ("CaptureRefused", "MarksStorageCeiling")
        and record["error_code"] == 429
        and isinstance(record["http_diagnostics"], dict)
        and base.raw(record["response_bytes"], "retry_body_bytes")
        <= marks_policy()["maximum_response_bytes"]
        and record["http_diagnostics"].get("http_status") == 429,
        "ineligible_retry_response",
    )
    clock = base.clock(record["response_headers_unix"], "retry_header_unix")
    retry = record["http_diagnostics"]["retry_after"]
    base.require(isinstance(retry, dict), "retry_after_schema")
    state = retry.get("state")
    if state in ("absent", "invalid"):
        base.exact(retry, {"state"}, "retry_after")
    else:
        base.require(state in ("delay_seconds", "http_date"), "retry_after_state")
        field = "seconds" if state == "delay_seconds" else "unix_seconds"
        base.exact(retry, {"state", field}, "retry_after")
        base.require(
            type(retry[field]) is int and retry[field] >= 0, "retry_after_number"
        )
    return rate_limit_retry_delay(
        record["http_diagnostics"], min(record["attempt"], 2), now_unix=clock
    )


def replay(  # noqa: C901, PLR0912, PLR0913, PLR0915 - one ordered evidence verifier
    path: Path,
    lock: dict,
    marks_lock: dict,
    marks_lock_path: Path,
    coins: dict,
    heads: base.Heads,
) -> dict:
    base.require(
        path.stat().st_size <= marks_policy()["maximum_marks_bytes"],
        "marks_tape_size_limit",
    )
    observed_times = head_observation_times(heads)
    digest = hashlib.sha256()
    targets, outcomes, audits = {}, {}, []
    terminal, previous_start, start_record = None, None, None
    previous_received = None
    requests, request_ids = 0, set()
    rpc_counts = Counter()
    operations, target_operations = {}, {}
    cooldown_until, cooldown_refused = 0.0, False
    with path.open("rb") as stream:
        for sequence, line in enumerate(stream):
            digest.update(line)
            base.require(line.endswith(b"\n"), "incomplete_marks_record")
            record = base.strict_json(line)
            base.require(
                record.get("schema_version") == 1
                and type(record.get("schema_version")) is int
                and record.get("run_id") == lock["run_id"]
                and type(record.get("sequence")) is int
                and record["sequence"] == sequence,
                "marks_record_identity_or_sequence",
            )
            base.require(terminal is None, "records_after_marks_terminal")
            kind = record["kind"]
            if sequence == 0:
                base.require(
                    kind == "start"
                    and record["base_lock_sha256"] == marks_lock["base_lock_sha256"]
                    and record["marks_lock_sha256"]
                    == hashlib.sha256(marks_lock_path.read_bytes()).hexdigest()
                    and record["source_sha256"] == marks_lock["source_sha256"]
                    and record["policy"] == marks_policy(),
                    "marks_start_binding",
                )
                base.clock(record["started_monotonic"], "marks_started")
                start_record = record
                continue
            if kind == "target":
                target = {
                    k: v
                    for k, v in record.items()
                    if k not in ("schema_version", "run_id", "kind", "sequence")
                }
                check_target(target, coins, heads, outcomes)
                admitted = heads.times[0] + lock["capture"]["warmup_seconds"]
                base.require(
                    admitted
                    <= coins[target["mint"]]["create_received_monotonic"]
                    < admitted + lock["capture"]["admission_seconds"],
                    "target_outside_admission",
                )
                base.require(target["target_id"] not in targets, "duplicate_target")
                targets[target["target_id"]] = target
            elif kind in ("rpc", "rpc_retry", "miss"):
                batch = [record["target"]] if kind == "miss" else record["targets"]
                base.require(isinstance(batch, list), "invalid_target_batch")
                base.require(
                    len({t["target_id"] for t in batch}) == len(batch),
                    "duplicate_batch_target",
                )
                for target in batch:
                    key = target["target_id"]
                    base.require(
                        targets.get(key) == target and key not in outcomes,
                        "unknown_or_duplicate_target_outcome",
                    )
                    check_target(target, coins, heads, outcomes)
                if kind == "miss":
                    base.clock(record["received_monotonic"], "miss_time")
                    if record.get("reason") == "mark_start_deadline_expired":
                        base.require(
                            batch[0]["due_at"] is not None
                            and record["received_monotonic"] > batch[0]["due_at"] + 2,
                            "premature_expiry_miss",
                        )
                    base.require(
                        isinstance(record.get("reason"), str)
                        and bool(record["reason"]),
                        "miss_reason_missing",
                    )
                    outcomes[batch[0]["target_id"]] = {
                        "error": record["reason"],
                        "miss": record,
                    }
                    continue
                purpose = record["purpose"]
                base.require(
                    (purpose == "marks" and 0 < len(batch) <= 32)
                    or (purpose in ("audit_before", "audit_after") and not batch),
                    "rpc_batch_purpose",
                )
                base.require(
                    batch
                    == sorted(batch, key=lambda t: (t["due_at"], t["arm"], t["mint"])),
                    "nondeterministic_batch_order",
                )
                minimum = check_rpc_clock(record, batch, heads, observed_times)
                started = record["request_started_monotonic"]
                base.require(
                    previous_start is None
                    or started - previous_start
                    >= marks_lock["policy"]["minimum_request_spacing_seconds"],
                    "rpc_spacing_violation",
                )
                base.require(
                    started >= start_record["started_monotonic"],
                    "rpc_before_marks_start",
                )
                base.require(
                    previous_received is None or started >= previous_received,
                    "overlapping_serialized_requests",
                )
                previous_received = record["response_received_monotonic"]
                previous_start = started
                requests += 1
                base.require(
                    requests
                    <= marks_lock["policy"]["maximum_requests"]
                    - (0 if purpose == "audit_after" else 1),
                    "rpc_request_limit_or_final_reserve",
                )
                rpc_id = record["request"]["id"]
                base.require(
                    type(rpc_id) is int
                    and rpc_id == requests
                    and rpc_id not in request_ids,
                    "rpc_id_reused_or_invalid",
                )
                request_ids.add(rpc_id)
                native = native_response(record, addresses(batch, purpose), minimum)
                rpc_counts[kind] += 1
                if kind == "rpc" and record.get("error_type") is not None:
                    rpc_counts["rpc_failures"] += 1
                operation = record["operation_id"]
                attempt = record["attempt"]
                base.require(
                    type(operation) is int
                    and type(attempt) is int
                    and 1 <= attempt <= RATE_LIMIT_MAX_ATTEMPTS,
                    "retry_operation_identity",
                )
                logical_start = base.clock(
                    record["logical_started_monotonic"], "logical_start"
                )
                deadline = base.clock(
                    record["logical_deadline_monotonic"], "logical_deadline"
                )
                base.require(
                    start_record["started_monotonic"]
                    <= logical_start
                    <= started
                    < deadline
                    <= logical_start + RATE_LIMIT_MAX_ELAPSED_SECONDS,
                    "retry_logical_deadline",
                )
                base.require(
                    not cooldown_refused and started >= cooldown_until,
                    "rpc_before_global_cooldown",
                )
                headers = record["response_headers_monotonic"]
                base.require(
                    (headers is None and record["response_headers_unix"] is None)
                    or (
                        headers is not None
                        and base.clock(record["response_headers_unix"], "headers_unix")
                        >= 0
                    ),
                    "response_header_unix_clock",
                )
                previous = operations.get(operation)
                if attempt == 1:
                    base.require(
                        operation == rpc_id
                        and previous is None
                        and record["retry_of_sequence"] is None,
                        "retry_first_attempt_link",
                    )
                    expected_deadline = logical_start + RATE_LIMIT_MAX_ELAPSED_SECONDS
                    base.require(
                        deadline == expected_deadline, "retry_deadline_changed"
                    )
                    if purpose != "marks":
                        base.require(
                            not any(
                                r["purpose"] == purpose for r in operations.values()
                            ),
                            "audit_retry_restarted_as_new_operation",
                        )
                else:
                    base.require(
                        previous is not None
                        and previous["kind"] == "rpc_retry"
                        and record["retry_of_sequence"] == previous["sequence"]
                        and attempt == previous["attempt"] + 1
                        and purpose == previous["purpose"]
                        and logical_start == previous["logical_started_monotonic"]
                        and deadline == previous["logical_deadline_monotonic"],
                        "retry_chain_mismatch",
                    )
                    remaining = [
                        t for t in previous["targets"] if t["target_id"] not in outcomes
                    ]
                    base.require(batch == remaining, "retry_target_chain_mismatch")
                    base.require(
                        started >= previous["retry_not_before_monotonic"],
                        "retry_started_early",
                    )
                for target in batch:
                    key = target["target_id"]
                    base.require(
                        key not in target_operations
                        or target_operations[key] == operation,
                        "target_retried_as_new_operation",
                    )
                    target_operations[key] = operation
                operations[operation] = record
                complete_429 = (
                    record.get("http_status") == 429
                    and record.get("body_complete") is True
                    and record.get("error_type")
                    in ("CaptureRefused", "MarksStorageCeiling")
                    and record["response_bytes"]
                    <= marks_policy()["maximum_response_bytes"]
                    and record.get("error_code") == 429
                )
                if complete_429:
                    delay = retry_cooldown(record)
                    if delay is None:
                        cooldown_refused = True
                        base.require(
                            record["retry_not_before_monotonic"] is None,
                            "invalid_retry_delay",
                        )
                    else:
                        cooldown_until = max(cooldown_until, headers + delay)
                        base.require(
                            record["retry_not_before_monotonic"] == cooldown_until,
                            "retry_delay_mismatch",
                        )
                        cooldown_refused = cooldown_until >= deadline
                else:
                    base.require(
                        record["retry_not_before_monotonic"] is None,
                        "non429_retry_delay",
                    )
                    if record.get("http_status") == 429:
                        cooldown_refused = True
                base.require(
                    record.get("storage_error") in (None, "MarksStorageCeiling"),
                    "invalid_storage_failure",
                )
                eligible_retry = (
                    complete_429
                    and not cooldown_refused
                    and record["error_type"] == "CaptureRefused"
                    and record.get("storage_error") is None
                    and attempt < RATE_LIMIT_MAX_ATTEMPTS
                    and requests
                    < marks_lock["policy"]["maximum_requests"]
                    - (0 if purpose == "audit_after" else 1)
                    and max(
                        cooldown_until,
                        started
                        + marks_lock["policy"]["minimum_request_spacing_seconds"],
                    )
                    < deadline
                )
                base.require(
                    (kind == "rpc_retry") == eligible_retry,
                    "ineligible_or_missing_retry_attempt",
                )
                if kind == "rpc_retry":
                    continue
                if "slot" in native:
                    base.require(
                        record["http_status"] == 200
                        and record["body_complete"] is True
                        and record["response_received_monotonic"] <= deadline,
                        "successful_retry_outside_deadline",
                    )
                if purpose == "marks" and "slot" in native:
                    try:
                        fee = base.decode_fee_config_account(
                            account(
                                native["accounts"][
                                    str(base.PumpFunAddresses.find_fee_config())
                                ]
                            )
                        )
                        base.require(
                            lock["fee_input"] is not None
                            and fee.digest == lock["fee_input"]["config_digest"],
                            "observed_fee_digest_mismatch",
                        )
                    except (ValueError, KeyError, TypeError, IndexError) as exc:
                        native["error"] = str(exc)
                        rpc_counts["fee_mismatches"] += 1
                for target in batch:
                    outcomes[target["target_id"]] = {"rpc": record, "native": native}
                    if "slot" in native and response_expired(
                        target, record["response_received_monotonic"]
                    ):
                        outcomes[target["target_id"]]["error"] = (
                            "mark_response_deadline_expired"
                        )
                if purpose != "marks":
                    base.require(
                        purpose not in [a["purpose"] for a in audits],
                        "duplicate_payer_audit",
                    )
                    audits.append({"purpose": purpose, "rpc": record, "native": native})
            elif kind == "terminal":
                base.require(
                    type(record["complete"]) is bool
                    and type(record["capture_exit_code"]) is int,
                    "terminal_flags",
                )
                base.clock(record["ended_monotonic"], "marks_ended")
                base.require(
                    isinstance(record["pending_targets"], list),
                    "terminal_pending_targets_schema",
                )
                base.require(
                    len({t["target_id"] for t in record["pending_targets"]})
                    == len(record["pending_targets"]),
                    "terminal_duplicate_pending",
                )
                base.require(
                    {t["target_id"]: t for t in record["pending_targets"]}
                    == {
                        key: value
                        for key, value in targets.items()
                        if key not in outcomes
                    },
                    "terminal_pending_target_mismatch",
                )
                base.require(
                    isinstance(record["counts"], dict), "terminal_counts_schema"
                )
                for counter, expected in (
                    ("start", 1),
                    ("target", len(targets)),
                    ("rpc", rpc_counts["rpc"]),
                    ("rpc_retry", rpc_counts["rpc_retry"]),
                    ("rpc_failures", rpc_counts["rpc_failures"]),
                    ("fee_mismatches", rpc_counts["fee_mismatches"]),
                    ("miss", sum("miss" in o for o in outcomes.values())),
                    (
                        "late_marks",
                        sum(
                            o.get("error") == "mark_response_deadline_expired"
                            for o in outcomes.values()
                        ),
                    ),
                ):
                    base.require(
                        type(record["counts"].get(counter, 0)) is int
                        and record["counts"].get(counter, 0) == expected,
                        "terminal_counter_mismatch:" + counter,
                    )
                unresolved = [
                    r
                    for r in operations.values()
                    if r["kind"] == "rpc_retry"
                    and (
                        not r["targets"]
                        or any(
                            "miss" not in outcomes.get(t["target_id"], {})
                            for t in r["targets"]
                        )
                    )
                ]
                base.require(
                    not record["complete"]
                    or (
                        not unresolved
                        and not rpc_counts["rpc_failures"]
                        and not rpc_counts["fee_mismatches"]
                        and not record["pending_targets"]
                        and not any(
                            "miss" in o or "error" in o for o in outcomes.values()
                        )
                    ),
                    "complete_with_unresolved_retry_or_failure",
                )
                base.require(
                    previous_received is None
                    or record["ended_monotonic"] >= previous_received,
                    "terminal_before_rpc",
                )
                terminal = record
            else:
                raise ValueError("unknown_marks_record_kind")
    base.require(start_record is not None, "marks_start_missing")
    return {
        "sha256": digest.hexdigest(),
        "targets": targets,
        "outcomes": outcomes,
        "audits": audits,
        "terminal": terminal,
        "requests": requests,
        "rpc_retries": requests - len(operations),
        "rpc_failures": rpc_counts["rpc_failures"],
        "missing_outcomes": sorted(set(targets) - set(outcomes)),
    }


def observed_quote(
    outcome: dict | None,
    coin: dict,
    snapshot: base.PumpFeeSnapshot,
    decoder: SimpleNamespace,
    heads: base.Heads,
) -> tuple[dict, dict]:
    base.require(outcome is not None, "target_outcome_missing")
    base.require("error" not in outcome, "target_missed:" + str(outcome.get("error")))
    native, rpc = outcome["native"], outcome["rpc"]
    base.require("error" not in native, "rpc_target_failure")
    at = rpc["response_received_monotonic"]
    # Replay verified this observed head's identity; delivery never resets its age.
    start = rpc["request_started_monotonic"]
    base.require(
        0
        <= start - base.clock(rpc["head_received_at_start"], "rpc_head_received")
        <= heads.max_gap,
        "decision_head_stale",
    )
    heads.coverage(start, at)
    base.require(
        native["slot"] >= rpc["head_slot_at_start"], "account_context_behind_head"
    )
    values = native["accounts"]
    fee = base.decode_fee_config_account(
        account(values[str(base.PumpFunAddresses.find_fee_config())])
    )
    base.require(fee.digest == snapshot.config.digest, "observed_fee_digest_mismatch")
    mint = base.Pubkey.from_string(coin["mint"])
    curve = PumpFunAddressProvider().derive_pool_address(mint)
    curve_account = account(values[str(curve)])
    data = PumpFunCurveManager._validated_curve_data(curve_account, curve)
    base.require(
        data[48] in (0, 1) and data[81] in (0, 1) and data[82] in (0, 1),
        "curve_flag_noncanonical",
    )
    state = PumpFunCurveManager._decode_curve_state_with_idl(decoder, data)
    base.require(state["complete"] is False, "complete_or_graduated_curve")
    base.require(
        str(state["creator"]) == coin["creator"]
        and state["is_mayhem_mode"] is coin["mayhem"]
        and state["quote_mint"] == WSOL_MINT,
        "curve_creation_identity_mismatch",
    )
    mint_account = account(values[coin["mint"]])
    supply, extensions = mint_extensions(mint_account, mint)
    expected_program = coin.get("create_fields", {}).get("token_program")
    if expected_program is not None:
        base.require(
            str(mint_account.owner) == expected_program,
            "mint_creation_program_mismatch",
        )
    base.require(
        state["token_total_supply"] == base.raw(coin["supply"], "creation_supply"),
        "curve_creation_supply_mismatch",
    )
    base.require(supply >= base.POLICY["quantity_raw"], "full_quantity_exceeds_supply")
    # Holder burns change mint supply independently of curve/CreateEvent supply.
    # Mayhem fees need entry-time mint supply; a later mark cannot backfill it.
    base.require(not state["is_mayhem_mode"], "mayhem_entry_mint_supply_unobserved")
    quote = base.quote_sell_exact_in(state, base.POLICY["quantity_raw"], snapshot)
    base.require(
        quote.amount_in_raw == base.POLICY["quantity_raw"], "partial_quantity_quote"
    )
    proof = {
        "rpc_sequence": rpc["sequence"],
        "context_slot": native["slot"],
        "request_started_monotonic": rpc["request_started_monotonic"],
        "response_received_monotonic": at,
        "response_sha256": rpc["response_sha256"],
        "curve_address": str(curve),
        "mint": coin["mint"],
        "mint_owner": str(mint_account.owner),
        "mint_supply_raw": supply,
        "mint_extensions": extensions,
        "observed_fee_digest": fee.digest,
        "state": {
            k: str(v) if isinstance(v, base.Pubkey) else v for k, v in state.items()
        },
    }
    return asdict(quote), proof


def account_row(  # noqa: PLR0913 - explicit immutable evidence inputs
    original: dict,
    coin: dict,
    evidence: dict,
    snapshot: base.PumpFeeSnapshot | None,
    decoder: SimpleNamespace,
    heads: base.Heads,
    lock: dict,
) -> dict:
    row = {
        k: original[k]
        for k in (
            "mint",
            "creator",
            "arm",
            "mayhem",
            "half",
            "supported",
            "d2_flow",
            "creation_received_monotonic",
            "entered",
            "entry_state",
            "entry_quote",
            "entry_received_monotonic",
            "entry_slot",
            "modeled_max_entry_input_lamports",
            "modeled_network_cleanup_estimates_lamports",
        )
        if k in original
    }
    row.update(
        status=original["status"] if not original["entered"] else "entered_unpriced",
        reason=original["reason"]
        if not original["entered"]
        else "account_outcome_missing",
        inventory_raw=base.POLICY["quantity_raw"]
        if original["entered"]
        else original["inventory_raw"],
        modeled_net_lamports=None,
        actual_net_lamports=None,
        extra_cost_breakeven_lamports=None,
        unknown_costs=UNKNOWN,
        baseline_status=original["status"],
        baseline_reason=original["reason"],
        baseline_modeled_net_lamports=original["modeled_net_lamports"],
        event_evidence_complete=coin.get("event_evidence_complete"),
        event_evidence_failures=coin.get("event_evidence_failures"),
        graduation_received_monotonic=coin.get("graduated_received_monotonic"),
    )
    prefix = f"{coin['mint']}:{original['arm']}:"
    row["target_outcomes"] = {
        stage: (
            "missing_target"
            if prefix + stage not in evidence["targets"]
            else "missing_outcome"
            if prefix + stage not in evidence["outcomes"]
            else "miss"
            if "error" in evidence["outcomes"][prefix + stage]
            else "rpc"
        )
        for stage in ("trigger", "exit")
    }
    row["target_errors"] = {}
    for stage in ("trigger", "exit"):
        outcome = evidence["outcomes"].get(prefix + stage)
        if outcome is not None:
            native = outcome.get("native", {})
            row["target_errors"][stage] = {
                "reason": outcome.get("error", native.get("error")),
                "native_reason": native.get("error"),
                "error_code": native.get("error_code"),
                "error_type": native.get("error_type"),
            }
    if not original["entered"]:
        return row
    try:
        base.require(snapshot is not None, "frozen_fee_snapshot_missing")
        base.require(
            evidence.get("setup_audit_valid") is True, "setup_audit_unverified"
        )
        base.require(
            original["entry_quote"]["amount_out_raw"] == base.POLICY["quantity_raw"],
            "baseline_entry_quantity_mismatch",
        )
        trigger, trigger_proof = observed_quote(
            evidence["outcomes"].get(prefix + "trigger"), coin, snapshot, decoder, heads
        )
        row.update(trigger_quote=trigger, trigger_state=trigger_proof)
        floor = base.minimum_output_with_slippage(
            trigger["amount_out_raw"], lock["coverage"]["sell_slippage_bps"]
        )
        row["exit_floor_lamports"] = floor
        exit_quote, exit_proof = observed_quote(
            evidence["outcomes"].get(prefix + "exit"), coin, snapshot, decoder, heads
        )
        row.update(
            exit_quote=exit_quote,
            exit_state=exit_proof,
            exit_received_monotonic=exit_proof["response_received_monotonic"],
            exit_slot=exit_proof["context_slot"],
        )
        heads.coverage(
            original["entry_received_monotonic"],
            exit_proof["response_received_monotonic"],
        )
        base.require(
            exit_proof["context_slot"] >= trigger_proof["context_slot"] + 1,
            "exit_context_not_after_trigger",
        )
        base.require(
            exit_quote["amount_out_raw"] >= floor, "exit_slippage_floor_failed"
        )
        costs = sum(
            base.POLICY[k]
            for k in (
                "buy_network_lamports",
                "sell_network_lamports",
                "cleanup_lamports",
            )
        )
        net = (
            exit_quote["amount_out_raw"]
            - original["entry_quote"]["amount_in_raw"]
            - costs
        )
        row.update(
            status="conditionally_priced",
            reason="full_quantity_delayed_account_mark",
            inventory_raw=0,
            modeled_net_lamports=net,
            extra_cost_breakeven_lamports=net,
            modeled_network_cleanup_lamports=costs,
        )
    except (ValueError, TypeError, KeyError, IndexError, struct.error) as exc:
        row["reason"] = str(exc)
    return row


def audit_summary(
    evidence: dict, snapshot: base.PumpFeeSnapshot | None, heads: base.Heads
) -> dict:
    result = {
        "before": None,
        "after": None,
        "lamport_delta": None,
        "actual_net_lamports": None,
        "setup_valid": False,
        "errors": [],
    }
    for item in evidence["audits"]:
        phase = "before" if item["purpose"] == "audit_before" else "after"
        try:
            base.require("error" not in item["native"], "audit_rpc_failure")
            values = item["native"]["accounts"]
            payer = account(values[marks_policy()["public_payer"]])
            result[phase] = {
                "lamports": payer.lamports,
                "owner": str(payer.owner),
                "data_sha256": hashlib.sha256(payer.data).hexdigest(),
                "data_bytes": len(payer.data),
                "context_slot": item["native"]["slot"],
                "response_received_monotonic": item["rpc"][
                    "response_received_monotonic"
                ],
                "response_sha256": item["rpc"]["response_sha256"],
            }
            if phase == "before":
                fee = base.decode_fee_config_account(
                    account(values[str(base.PumpFunAddresses.find_fee_config())])
                )
                base.require(
                    snapshot is not None and fee.digest == snapshot.config.digest,
                    "setup_fee_digest_mismatch",
                )
                base.require(
                    item["rpc"]["response_received_monotonic"] <= heads.times[0],
                    "setup_audit_after_stream_admission",
                )
                result["setup_valid"] = True
            else:
                base.require(
                    item["rpc"]["request_started_monotonic"] >= heads.times[-1],
                    "payer_after_audit_before_stream_end",
                )
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            result["errors"].append(f"{phase}:{exc}")
    if result["before"] is not None and result["after"] is not None:
        result["lamport_delta"] = (
            result["after"]["lamports"] - result["before"]["lamports"]
        )
    return result


def comparisons(rows: list[dict]) -> dict:
    result = {}
    for half in ("all", "discovery", "holdout"):
        result[half] = {}
        for arm in ("C", "D2"):
            subset = [
                r
                for r in rows
                if r["arm"] == arm and (half == "all" or r.get("half") == half)
            ]
            paired = [
                r
                for r in subset
                if r["modeled_net_lamports"] is not None
                and r["baseline_modeled_net_lamports"] is not None
            ]
            result[half][arm] = {
                "all_rows": len(subset),
                "entered": sum(r["entered"] for r in subset),
                "baseline_priced": sum(
                    r["baseline_modeled_net_lamports"] is not None for r in subset
                ),
                "account_priced": sum(
                    r["modeled_net_lamports"] is not None for r in subset
                ),
                "coverage_status_pairs": dict(
                    Counter(f"{r['baseline_status']}|{r['status']}" for r in subset)
                ),
                "baseline_net_lamports": base.distribution(
                    [
                        r["baseline_modeled_net_lamports"]
                        for r in subset
                        if r["baseline_modeled_net_lamports"] is not None
                    ]
                ),
                "account_net_lamports": base.distribution(
                    [
                        r["modeled_net_lamports"]
                        for r in subset
                        if r["modeled_net_lamports"] is not None
                    ]
                ),
                "paired_intersection": len(paired),
                "paired_baseline_net_lamports": base.distribution(
                    [r["baseline_modeled_net_lamports"] for r in paired]
                ),
                "paired_account_net_lamports": base.distribution(
                    [r["modeled_net_lamports"] for r in paired]
                ),
                "paired_account_minus_baseline_lamports": base.distribution(
                    [
                        r["modeled_net_lamports"] - r["baseline_modeled_net_lamports"]
                        for r in paired
                    ]
                ),
            }
    return result


def score(  # noqa: PLR0915 - one exclusive report transaction
    base_lock_path: Path, marks_lock_path: Path, tape: Path, marks: Path, out: Path
) -> dict:
    marks_lock = validate_marks_lock(base_lock_path, marks_lock_path)
    lock = base.strict_json(base_lock_path.read_bytes())
    slots = base.ROOT / lock["slot_clock_path"]
    baseline_out = out.with_suffix(".event-baseline.json")
    inputs = (
        base_lock_path,
        marks_lock_path,
        tape,
        marks,
        slots,
        base.ROOT / lock["run_journal_path"],
    )
    base.require(
        out.resolve() != baseline_out.resolve()
        and all(
            p.resolve() not in (out.resolve(), baseline_out.resolve()) for p in inputs
        ),
        "output_input_collision",
    )
    base.require(
        not out.exists() and not baseline_out.exists(), "report_already_exists"
    )
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in inputs}
    base.score(tape, slots, base_lock_path, baseline_out)
    baseline_bytes = baseline_out.read_bytes()
    baseline = base.strict_json(baseline_bytes)
    coins = {}
    with tape.open("rb") as stream:
        for line in stream:
            coin = base.strict_json(line)
            coins[coin["mint"]] = {
                k: coin[k]
                for k in (
                    "mint",
                    "creator",
                    "mayhem",
                    "supply",
                    "create_slot",
                    "create_fields",
                    "create_received_monotonic",
                    "event_evidence_complete",
                    "event_evidence_failures",
                    "graduated_received_monotonic",
                )
                if k in coin
            }
    with slots.open("rb") as stream:
        heads = base.Heads(
            [base.strict_json(line) for line in stream],
            lock["coverage"]["max_head_gap_seconds"],
        )
    base.bind_journal(lock, heads)
    evidence = replay(marks, lock, marks_lock, marks_lock_path, coins, heads)
    snapshot, _ = base.fee_snapshot(lock["fee_input"])
    audits = audit_summary(evidence, snapshot, heads)
    evidence["setup_audit_valid"] = audits["setup_valid"]
    decoder = SimpleNamespace(
        _idl_parser=IDLParser(str(base.ROOT / "idl/pump_fun_idl.json"))
    )
    rows = [
        account_row(r, coins[r["mint"]], evidence, snapshot, decoder, heads, lock)
        for r in baseline["rows"]
    ]
    rows.sort(
        key=lambda r: (r.get("creation_received_monotonic", 0), r["mint"], r["arm"])
    )
    terminal = evidence["terminal"]
    partial = [
        "base_capture:" + reason
        for reason in baseline["result"]["capture"]["partial_reasons"]
    ]
    if not baseline["result"]["capture"]["scheduled_window_complete"]:
        partial.append("base_capture_incomplete")
    if terminal is None:
        partial.append("marks_terminal_missing")
    elif (
        not terminal["complete"]
        or terminal["capture_exit_code"] != 0
        or terminal.get("error_type") is not None
        or terminal.get("pending_targets")
    ):
        partial.append("marks_terminal_incomplete")
    if evidence["missing_outcomes"]:
        partial.append("target_outcomes_missing")
    if not audits["setup_valid"] or audits["after"] is None or audits["errors"]:
        partial.append("payer_or_fee_audit_unverified")
    missing_triggers = [
        f"{r['mint']}:{r['arm']}:trigger"
        for r in rows
        if r.get("half") in ("discovery", "holdout")
        and f"{r['mint']}:{r['arm']}:trigger" not in evidence["targets"]
    ]
    if missing_triggers:
        partial.append("admitted_trigger_targets_missing")
    missing_exits = sorted(
        key.rsplit(":", 1)[0] + ":exit"
        for key, value in evidence["outcomes"].items()
        if key.endswith(":trigger")
        and "slot" in value.get("native", {})
        and value.get("error") is None
        and key.rsplit(":", 1)[0] + ":exit" not in evidence["targets"]
    )
    if missing_exits:
        partial.append("successful_trigger_exit_targets_missing")
    outcome_errors = Counter(
        value.get("error", value.get("native", {}).get("error"))
        for value in evidence["outcomes"].values()
        if value.get("error", value.get("native", {}).get("error")) is not None
    )
    if outcome_errors:
        partial.append("missed_or_failed_account_outcomes")
    report = {
        "schema_version": 1,
        "run_id": lock["run_id"],
        "rows": rows,
        "result": {
            "base_lock_sha256": marks_lock["base_lock_sha256"],
            "marks_lock_sha256": hashlib.sha256(
                marks_lock_path.read_bytes()
            ).hexdigest(),
            "marks_sha256": evidence["sha256"],
            "source_sha256": source_hashes(),
            "event_baseline_path": str(baseline_out),
            "event_baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
            "base_capture": baseline["result"]["capture"],
            "marks_terminal": terminal,
            "capture_complete": not partial,
            "partial_reasons": partial,
            "marks_coverage": {
                "requests": evidence["requests"],
                "rpc_retries": evidence["rpc_retries"],
                "rpc_failures": evidence["rpc_failures"],
                "targets": len(evidence["targets"]),
                "outcomes": len(evidence["outcomes"]),
                "missing_outcomes": evidence["missing_outcomes"],
                "missing_admitted_trigger_targets": missing_triggers,
                "missing_successful_trigger_exit_targets": missing_exits,
                "outcome_errors": dict(outcome_errors),
                "native_errors": dict(
                    Counter(
                        value["native"]["error"]
                        for value in evidence["outcomes"].values()
                        if value.get("native", {}).get("error") is not None
                    )
                ),
            },
            "payer_audit": audits,
            "summary": base.summaries(rows, lock, heads.times[0]),
            "baseline_comparison": comparisons(rows),
            "clocks": {
                "entry": "unchanged event baseline C transaction receive / D2 first qualifying received head",
                "rpc_head_availability": "latest slot observed_monotonic at request start; freshness remains measured from its received_monotonic",
                "trigger": "request due entry+10s; quote at actual account response receive",
                "exit": "first head strictly after trigger response and at least context+1; quote at actual later account response receive",
                "maximum_target_lateness_seconds": 2,
            },
            "actual_net_lamports": None,
            "unmodeled_costs_and_execution": UNKNOWN,
            "limits": [
                *base.LIMITS,
                "Account observations do not repair historical event state; event failures and baseline censoring remain independently visible.",
                "An observed matching fee digest uses the prior attested scenario; no current native fee attestation was performed.",
                "Mayhem entry fees require historical native mint supply absent from the event-entry model; those inventories remain unpriced, never backfilled from later marks.",
                "No wallet was signed or traded. Public payer balance differences are not study income and can include unrelated activity.",
                "Processed account snapshots and heads may belong to different forks; minContextSlot is not fork identity or a maximum-slot bound.",
                "Hypothetical inventory is retained at full quantity until a conditional full-quantity mark passes; zero modeled residual is not an actual sale.",
            ],
        },
        "scoring_complete": True,
    }
    base.require(
        all(
            (p.stat().st_size, p.stat().st_mtime_ns) == stat
            for p, stat in before.items()
        ),
        "input_changed_during_score",
    )
    validate_marks_lock(base_lock_path, marks_lock_path)
    with out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
    return {
        "out": str(out),
        "event_baseline": str(baseline_out),
        "rows": len(rows),
        "capture_complete": not partial,
    }


def self_check() -> dict:  # noqa: PLR0915 - one native decoding and refusal scenario
    """One offline adversarial scenario: identity, freshness and missing inventory."""
    from copy import deepcopy  # noqa: PLC0415 - offline self-check only
    from tempfile import TemporaryDirectory  # noqa: PLC0415 - offline replay only

    from decode_provider_response import headers_evidence  # noqa: PLC0415

    heads = base.Heads(
        [
            {
                "slot": 10,
                "received_monotonic": 1.0,
                "observed_monotonic": 1.0,
                "gap_seconds": None,
            },
            {
                "slot": 11,
                "received_monotonic": 1.5,
                "observed_monotonic": 1.5,
                "gap_seconds": 0.5,
            },
            {
                "slot": 12,
                "received_monotonic": 2.0,
                "observed_monotonic": 2.0,
                "gap_seconds": 0.5,
            },
        ],
        1,
    )
    observed_times = head_observation_times(heads)
    target = {
        "target_id": "m:C:exit",
        "mint": "m",
        "arm": "C",
        "stage": "exit",
        "entry_at": 1.0,
        "entry_slot": 10,
        "due_at": 2.0,
        "due_slot": 12,
        "trigger_request_sequence": 3,
    }
    coins = {"m": {"create_received_monotonic": 1.0, "create_slot": 10}}
    outcomes = {
        "m:C:trigger": {
            "native": {"slot": 11},
            "rpc": {"sequence": 3, "response_received_monotonic": 1.5},
        }
    }
    check_target(target, coins, heads, outcomes)

    def refuses(call: Callable[[], object]) -> None:
        try:
            call()
        except (ValueError, KeyError, TypeError):
            return
        raise AssertionError("invalid_evidence_accepted")

    for observed in (None, True, float("nan"), 0.9):
        invalid = deepcopy(heads.rows)
        invalid[0]["observed_monotonic"] = observed
        refuses(lambda rows=invalid: head_observation_times(base.Heads(rows, 1)))
    invalid = deepcopy(heads.rows)
    invalid[0].pop("observed_monotonic")
    refuses(lambda: head_observation_times(base.Heads(invalid, 1)))
    invalid = deepcopy(heads.rows)
    invalid[0]["observed_monotonic"] = 1.6
    refuses(lambda: head_observation_times(base.Heads(invalid, 1)))

    wrong = dict(target, due_at=1.5)
    refuses(lambda: check_target(wrong, coins, heads, outcomes))
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getMultipleAccounts",
        "params": [
            ["canonical"],
            {"encoding": "base64", "commitment": "processed", "minContextSlot": 10},
        ],
    }
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"context": {"slot": 10}, "value": [None]},
        }
    ).encode()
    rpc = {
        "request": request,
        "response_base64": base64.b64encode(payload).decode(),
        "response_sha256": hashlib.sha256(payload).hexdigest(),
        "response_bytes": len(payload),
    }
    assert native_response(rpc, ["canonical"], 10)["slot"] == 10
    refuses(lambda: native_response(rpc, ["other"], 10))
    refuses(lambda: native_response(rpc, ["canonical"], 11))
    corrupt = deepcopy(rpc)
    corrupt["response_sha256"] = "0" * 64
    refuses(lambda: native_response(corrupt, ["canonical"], 10))
    refuses(lambda: account({"data": ["not base64!", "base64"], "executable": False}))
    timed = {
        **rpc,
        "request_started_monotonic": 1.5,
        "response_headers_monotonic": 1.6,
        "response_received_monotonic": 1.7,
        "head_slot_at_start": 11,
        "head_received_at_start": 1.5,
    }
    refuses(lambda: check_rpc_clock(timed, [target], heads, observed_times))
    # Receive task finished at 1.5, but the recorder delivered that head at 1.8.
    pending_rows = deepcopy(heads.rows)
    pending_rows[1]["observed_monotonic"] = 1.8
    pending_rows[2]["observed_monotonic"] = 3.5
    pending_heads = base.Heads(pending_rows, 1)
    pending_times = head_observation_times(pending_heads)
    pending_rpc = dict(
        timed,
        request_started_monotonic=1.6,
        head_slot_at_start=10,
        head_received_at_start=1.0,
    )
    assert check_rpc_clock(pending_rpc, [], pending_heads, pending_times) == 10
    refuses(
        lambda: check_rpc_clock(
            dict(pending_rpc, head_slot_at_start=11, head_received_at_start=1.5),
            [],
            pending_heads,
            pending_times,
        )
    )
    delivered_rpc = dict(
        timed,
        request_started_monotonic=1.8,
        response_headers_monotonic=1.8,
        response_received_monotonic=1.9,
    )
    assert check_rpc_clock(delivered_rpc, [], pending_heads, pending_times) == 11
    stale_rpc = dict(
        timed,
        request_started_monotonic=3.6,
        response_headers_monotonic=3.6,
        response_received_monotonic=3.7,
        head_slot_at_start=12,
        head_received_at_start=2.0,
    )
    assert check_rpc_clock(stale_rpc, [], pending_heads, pending_times) == 12
    mixed = dict(
        timed,
        request_started_monotonic=3.9,
        response_headers_monotonic=4.0,
        response_received_monotonic=4.1,
        head_slot_at_start=12,
        head_received_at_start=2.0,
    )
    peer = dict(target, target_id="n:C:exit", mint="n", due_at=3.0)
    assert check_rpc_clock(mixed, [target, peer], heads, observed_times) == 12
    assert response_expired(target, mixed["response_received_monotonic"])
    assert not response_expired(peer, mixed["response_received_monotonic"])
    assert not response_expired(target, 4.0)
    refuses(
        lambda: check_rpc_clock(
            dict(mixed, response_received_monotonic=6.0),
            [target, peer],
            heads,
            observed_times,
        )
    )
    late_trigger = deepcopy(outcomes)
    late_trigger["m:C:trigger"]["error"] = "mark_response_deadline_expired"
    refuses(lambda: check_target(target, coins, heads, late_trigger))

    def packed_account(data: bytes | bytearray, owner: base.Pubkey) -> dict:
        return {
            "data": [base64.b64encode(data).decode(), "base64"],
            "owner": str(owner),
            "lamports": 1,
            "rentEpoch": 0,
            "executable": False,
        }

    # A holder burn changes native supply, not the normal curve's fee basis.
    rates = struct.pack("<QQQ", 0, 95, 30)
    tier = struct.pack("<I", 1) + bytes(16) + rates
    fee_value = packed_account(
        hashlib.sha256(b"account:FeeConfig").digest()[:8]
        + bytes(33)
        + rates
        + tier
        + tier
        + rates,
        base.PumpFunAddresses.FEE_PROGRAM,
    )
    snapshot = base.PumpFeeSnapshot(
        base.decode_fee_config_account(account(fee_value)), 0, 0
    )
    # Real replay, including same-target recovery: no relaxed duplicate outcomes.
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        lock_path, tape_path = directory / "lock.json", directory / "marks.jsonl"
        lock_path.write_text("{}")
        retry_mint = str(base.Pubkey.new_unique())
        retry_target = {
            "target_id": f"{retry_mint}:C:trigger",
            "mint": retry_mint,
            "arm": "C",
            "stage": "trigger",
            "entry_at": 1.0,
            "entry_slot": 10,
            "due_at": 11.0,
            "due_slot": 10,
            "trigger_request_sequence": None,
        }
        replay_lock = {
            "run_id": "offline",
            "capture": {"warmup_seconds": 0, "admission_seconds": 20},
            "fee_input": {"config_digest": snapshot.config.digest},
        }
        replay_marks_lock = {
            "base_lock_sha256": "offline",
            "source_sha256": {},
            "policy": marks_policy(),
        }
        replay_coins = {
            retry_mint: {"create_received_monotonic": 1.0, "create_slot": 10}
        }
        first_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [
                addresses([retry_target], "marks"),
                {"encoding": "base64", "commitment": "processed", "minContextSlot": 12},
            ],
        }
        first_attempt = {
            "kind": "rpc_retry",
            "request": first_request,
            "purpose": "marks",
            "targets": [retry_target],
            "operation_id": 1,
            "attempt": 1,
            "retry_of_sequence": None,
            "logical_started_monotonic": 11.0,
            "logical_deadline_monotonic": 21.0,
            "request_started_monotonic": 11.0,
            "response_headers_monotonic": 11.01,
            "response_headers_unix": 1000.0,
            "response_received_monotonic": 11.05,
            "retry_not_before_monotonic": 12.01,
            "head_slot_at_start": 12,
            "head_received_at_start": 2.0,
            "response_base64": None,
            "response_bytes": 0,
            "response_sha256": hashlib.sha256(b"").hexdigest(),
            "body_complete": True,
            "http_status": 429,
            "http_diagnostics": headers_evidence(429, {"Retry-After": "1"}),
            "error_type": "CaptureRefused",
            "error_code": 429,
        }
        success_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"context": {"slot": 12}, "value": [fee_value, None, None]},
            }
        ).encode()
        last_attempt = {
            **first_attempt,
            "kind": "rpc",
            "request": {**first_request, "id": 2},
            "attempt": 2,
            "retry_of_sequence": 2,
            "request_started_monotonic": 12.05,
            "response_headers_monotonic": 12.06,
            "response_headers_unix": 1001.05,
            "response_received_monotonic": 12.1,
            "retry_not_before_monotonic": None,
            "http_status": 200,
            "http_diagnostics": headers_evidence(200, {}),
            "error_type": None,
            "error_code": None,
            "response_base64": base64.b64encode(success_body).decode(),
            "response_bytes": len(success_body),
            "response_sha256": hashlib.sha256(success_body).hexdigest(),
        }
        rows = [
            {
                "kind": "start",
                **replay_marks_lock,
                "marks_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
                "started_monotonic": 0.5,
            },
            {"kind": "target", **retry_target},
            first_attempt,
            last_attempt,
            {
                "kind": "terminal",
                "complete": True,
                "capture_exit_code": 0,
                "ended_monotonic": 12.2,
                "pending_targets": [],
                "error_type": None,
                "counts": {"start": 1, "target": 1, "rpc_retry": 1, "rpc": 1},
            },
        ]

        def replay_rows(candidate: list[dict]) -> dict:
            tape_path.write_text(
                "".join(
                    json.dumps(
                        {
                            **row,
                            "schema_version": 1,
                            "run_id": "offline",
                            "sequence": index,
                        }
                    )
                    + "\n"
                    for index, row in enumerate(candidate)
                )
            )
            return replay(
                tape_path,
                replay_lock,
                replay_marks_lock,
                lock_path,
                replay_coins,
                heads,
            )

        recovered = replay_rows(rows)
        assert recovered["requests"] == 2 and recovered["rpc_retries"] == 1
        assert recovered["rpc_failures"] == 0 and recovered["terminal"]["complete"]
        assert recovered["outcomes"][retry_target["target_id"]]["rpc"]["attempt"] == 2
        late = deepcopy(rows)
        late[3]["response_received_monotonic"] = 13.1
        late[4].update(
            complete=False,
            ended_monotonic=13.2,
            counts={"start": 1, "target": 1, "rpc_retry": 1, "rpc": 1, "late_marks": 1},
        )
        late_result = replay_rows(late)
        assert (
            late_result["outcomes"][retry_target["target_id"]]["error"]
            == "mark_response_deadline_expired"
        )
        assert (
            late_result["rpc_failures"] == 0 and not late_result["terminal"]["complete"]
        )
        stored_failure = deepcopy([*rows[:3], rows[-1]])
        stored_failure[2].update(kind="rpc", storage_error="MarksStorageCeiling")
        stored_failure[3].update(
            complete=False,
            counts={"start": 1, "target": 1, "rpc": 1, "rpc_failures": 1},
        )
        assert replay_rows(stored_failure)["rpc_failures"] == 1
        stored_failure[2].update(
            error_type="CancelledError", retry_not_before_monotonic=None
        )
        assert replay_rows(stored_failure)["rpc_failures"] == 1
        for index, field, value in (
            (2, "retry_not_before_monotonic", 11.5),
            (2, "http_status", 503),
            (2, "body_complete", False),
            (3, "request_started_monotonic", 12.0),
            (3, "retry_of_sequence", 1),
            (3, "operation_id", 2),
            (3, "attempt", 4),
            (3, "logical_deadline_monotonic", 14.0),
            (4, "counts", {"start": 1, "target": 1, "rpc": 2}),
        ):
            invalid = deepcopy(rows)
            invalid[index][field] = value
            refuses(lambda candidate=invalid: replay_rows(candidate))
        refuses(lambda: replay_rows([*rows[:3], rows[-1]]))
        invalid = deepcopy(rows)
        invalid[2]["http_diagnostics"]["retry_after"] = {
            "state": "delay_seconds",
            "seconds": 3,
        }
        refuses(lambda: replay_rows(invalid))
        dated = deepcopy(rows)
        dated[2]["http_diagnostics"]["retry_after"] = {
            "state": "http_date",
            "unix_seconds": 1001,
        }
        assert replay_rows(dated)["rpc_retries"] == 1
        dated[2]["response_headers_unix"] = 999.0
        refuses(lambda: replay_rows(dated))
        missed = deepcopy(rows)
        missed[3] = {
            "kind": "miss",
            "target": retry_target,
            "reason": "mark_start_deadline_expired",
            "received_monotonic": 13.1,
        }
        missed[4].update(
            complete=False,
            ended_monotonic=13.2,
            counts={"start": 1, "target": 1, "rpc_retry": 1, "miss": 1},
        )
        expired = replay_rows(missed)
        assert "miss" in expired["outcomes"][retry_target["target_id"]]
        assert not expired["terminal"]["complete"]
        assert expired["requests"] == 1 and expired["rpc_retries"] == 0
        missed[3]["received_monotonic"] = 12.0
        refuses(lambda: replay_rows(missed))

    mint, creator = base.Pubkey.new_unique(), base.Pubkey.new_unique()
    curve = PumpFunAddressProvider().derive_pool_address(mint)
    curve_data = bytearray(
        hashlib.sha256(b"account:BondingCurve").digest()[:8]
        + struct.pack(
            "<5QB32sBB32s",
            10**15,
            30_000_000_000,
            800_000_000_000_000,
            5_000_000_000,
            10**15,
            0,
            bytes(creator),
            0,
            0,
            bytes(WSOL_MINT),
        )
        + bytes(36)
    )
    mint_data = bytearray(82)
    struct.pack_into("<QB", mint_data, 36, 10**15, 6)
    mint_data[45] = 1
    values = {
        str(base.PumpFunAddresses.find_fee_config()): fee_value,
        str(curve): packed_account(curve_data, base.PumpFunAddresses.PROGRAM),
        str(mint): packed_account(mint_data, SystemAddresses.TOKEN_PROGRAM),
    }
    native_coin = {
        "mint": str(mint),
        "creator": str(creator),
        "mayhem": False,
        "supply": 10**15,
    }
    native_outcome = {
        "native": {"slot": 11, "accounts": values},
        "rpc": {**timed, "sequence": 3},
    }
    decoder = SimpleNamespace(
        _idl_parser=IDLParser(str(base.ROOT / "idl/pump_fun_idl.json"))
    )
    initial_quote, _ = observed_quote(
        native_outcome, native_coin, snapshot, decoder, heads
    )
    pending_quote, _ = observed_quote(
        {
            "native": {"slot": 10, "accounts": values},
            "rpc": {**pending_rpc, "sequence": 4},
        },
        native_coin,
        snapshot,
        decoder,
        pending_heads,
    )
    assert pending_quote == initial_quote
    # Delivery at 3.5 cannot freshen the head whose actual receipt was at 2.0.
    try:
        observed_quote(
            {
                "native": {"slot": 12, "accounts": values},
                "rpc": {**stale_rpc, "sequence": 5},
            },
            native_coin,
            snapshot,
            decoder,
            pending_heads,
        )
    except ValueError as exc:
        assert str(exc) == "decision_head_stale"
    else:
        raise AssertionError("late_delivery_reset_head_freshness")
    struct.pack_into("<Q", mint_data, 36, 750_000_000_000_000)
    values[str(mint)] = packed_account(mint_data, SystemAddresses.TOKEN_PROGRAM)
    burned_quote, _ = observed_quote(
        native_outcome, native_coin, snapshot, decoder, heads
    )
    assert burned_quote == initial_quote
    struct.pack_into("<Q", mint_data, 36, base.POLICY["quantity_raw"] - 1)
    values[str(mint)] = packed_account(mint_data, SystemAddresses.TOKEN_PROGRAM)
    refuses(
        lambda: observed_quote(native_outcome, native_coin, snapshot, decoder, heads)
    )
    struct.pack_into("<Q", mint_data, 36, 2 * 10**15)
    values[str(mint)] = packed_account(mint_data, SystemAddresses.TOKEN_PROGRAM)
    curve_data[81] = 1
    values[str(curve)] = packed_account(curve_data, base.PumpFunAddresses.PROGRAM)
    native_coin["mayhem"] = True
    refuses(
        lambda: observed_quote(native_outcome, native_coin, snapshot, decoder, heads)
    )
    original = {
        "mint": "m",
        "creator": "c",
        "arm": "C",
        "mayhem": False,
        "half": "holdout",
        "entered": True,
        "status": "conditionally_priced",
        "reason": "baseline",
        "inventory_raw": 0,
        "modeled_net_lamports": 42,
    }
    row = account_row(
        original,
        {"mint": "m"},
        {"targets": {}, "outcomes": {}},
        None,
        SimpleNamespace(),
        heads,
        {},
    )
    assert (
        row["entered"]
        and row["inventory_raw"] == base.POLICY["quantity_raw"]
        and row["modeled_net_lamports"] is None
        and row["actual_net_lamports"] is None
    )
    assert row["target_outcomes"] == {
        "trigger": "missing_target",
        "exit": "missing_target",
    }
    return {
        "self_check": "passed",
        "rpc_calls": 0,
        "checks": [
            "canonical_response",
            "bounded429_same_target_replay",
            "retry_delay_chain_attempt_and_count_tampering",
            "retry_expiry_remains_unknown",
            "native_hash",
            "freshness",
            "pending_receive_head_availability",
            "late_delivery_preserves_receipt_freshness",
            "required_ordered_observation_clocks",
            "future_assignment",
            "malformed_account",
            "missing_inventory",
            "burned_mint_supply_and_unknown_mayhem_fees",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base-lock", "marks-lock", "tape", "marks", "out"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        result = self_check()
    else:
        if any(
            getattr(args, k) is None
            for k in ("base_lock", "marks_lock", "tape", "marks", "out")
        ):
            parser.error("--base-lock --marks-lock --tape --marks --out are required")
        result = score(args.base_lock, args.marks_lock, args.tape, args.marks, args.out)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
