#!/usr/bin/env python3
"""Score a source-locked C versus D2 creation tape, offline and without fills.

Print the strict public lock template with --lock-schema, archive a completed
lock BEFORE capture, then pass --tape --slots --lock --out. Paths in the lock
are repository-relative; outputs are exclusive. --self-check needs no provider.

fee_input is null (unpriced), or an exact object with native_attestation (bool),
config_digest (SHA256 of account bytes), observed_at and attested_at (finite
monotonic seconds), and fee_account. The latter has exactly address, owner,
data_base64, lamports, executable=false, rent_epoch. Only the canonical Pump
fee-config account is accepted. Historical attestations remain frozen scenarios,
never continuous attestations. No private keys, URLs or credentials belong here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import random
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from solders.account import Account
from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_DECIMALS, WSOL_MINT, normalize_quote_mint
from core.quote_engine import minimum_output_with_slippage
from monitoring.trade_flow import MAYHEM_SOL_VAULT
from platforms.pumpfun.address_provider import PumpFunAddresses
from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFees,
    PumpFeeSnapshot,
    PumpFeeTier,
    decode_fee_config_account,
    quote_buy_exact_out,
    quote_sell_exact_in,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Standalone src imports, protocol bounds and offline assertions are intentional.
# ruff: noqa: E402, PLR2004, S101

POLICY = {
    "arms": ["C", "D2"],
    "quantity_raw": 250000000000,
    "max_entry_quote_lamports": 13000000,
    "buy_network_lamports": 33000,
    "sell_network_lamports": 27000,
    "cleanup_lamports": 5000,
    "hold_seconds": 10,
    "exit_delay_slots": 1,
    "d2_delay_slots": 2,
    "quote": "SOL/WSOL",
    "token_decimals": 6,
}
LIMITS = [
    "Conditional observed-state counterfactual, NOT fills, realized profit, portfolio expectancy or income/day.",
    "Hypothetical buy impact is not propagated into later recorded states; trials are independent, not a funded portfolio.",
    "Processed heads and event provenance do not prove finality, complete transaction delivery, inclusion or actual execution.",
    "Frozen canonical fee snapshot is conditional; future fee changes, failed-attempt fees, tips and inclusion costs are unknown.",
    "Rent funding/refunds and persistent account balances are unknown, not zero; extra-cost breakeven is a budget, not net profit.",
    "C is an optimistic transaction-arrival benchmark, not proof a transaction could land at that time.",
    "D2 flow is descriptive at D2 only; it never filters C. Paired and priced subsets may have selective missingness.",
    "Time-block uncertainty is conditional on priced rows and cannot repair unpriced inventory or represent market-wide uncertainty.",
    "Lock hashes verify exact inputs, not that the lock was archived before capture; retain independent preregistration evidence.",
    "Creator and vault exclusions apply only to independent-flow features, never market state; no audited holder claims.",
]


def require(condition: bool, reason: str) -> None:  # noqa: FBT001
    if not condition:
        raise ValueError(reason)


def raw(value: object, name: str) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 2**64 - 1
    ):
        raise ValueError(f"invalid_raw:{name}")
    return value


def clock(value: object, name: str) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"invalid_clock:{name}")
    return value


def exact(obj: object, keys: Iterable[str], name: str) -> None:
    require(isinstance(obj, dict) and set(obj) == set(keys), f"invalid_schema:{name}")


def strict_json(data: str | bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate_json_key:{key}")
            result[key] = value
        return result

    result = json.loads(
        data,
        object_pairs_hook=pairs,
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"invalid_json:{x}")),
    )
    require(isinstance(result, dict), "json_record_must_be_object")
    return result


def sources() -> dict[str, str]:
    # Bind transitive local quote imports too, without importing the network recorder.
    paths = {
        Path(__file__).resolve(),
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        ROOT / "learning-examples/token-lifecycles/record_lifecycles.py",
        ROOT / "idl/pump_fun_idl.json",
        ROOT / "idl/pump_swap_idl.json",
    }
    paths.update(
        ROOT / p
        for p in (
            "src/utils/idl_parser.py",
            "src/monitoring/event_normalization.py",
            "src/monitoring/parser_dispatch.py",
            "src/platforms/pumpfun/event_parser.py",
        )
    )
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.is_relative_to(ROOT / "src") and path.suffix == ".py":
                paths.add(path)
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }


def lock_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "locked_at_utc": "REPLACE_WITH_PRECAPTURE_ISO8601_UTC",
        "tape_path": "REPLACE.jsonl",
        "slot_clock_path": "REPLACE.slots.jsonl",
        "run_id": "REPLACE_WITH_PUBLIC_RUN_ID",
        "run_journal_path": "REPLACE.run.jsonl",
        "capture": {
            "anchor": "first_processed_head_receive",
            "warmup_seconds": 60,
            "admission_seconds": 86400,
            "drain_seconds": 120,
            "commitment": "processed",
            "split": "admission_time_midpoint",
        },
        "policy": POLICY.copy(),
        "coverage": {
            "max_head_gap_seconds": 1,
            "max_state_age_seconds": 2,
            "sell_slippage_bps": 3000,
            "uncertainty_block_seconds": 60,
        },
        "fee_input": None,
        "source_sha256": sources(),
    }


def fee_snapshot(
    payload: dict[str, Any] | None,
) -> tuple[PumpFeeSnapshot | None, str | None]:
    if payload is None:
        return None, "fee_snapshot_unavailable"
    exact(
        payload,
        {
            "native_attestation",
            "config_digest",
            "observed_at",
            "attested_at",
            "fee_account",
        },
        "fee_input",
    )
    exact(
        payload["fee_account"],
        {"address", "owner", "data_base64", "lamports", "executable", "rent_epoch"},
        "fee_account",
    )
    account = payload["fee_account"]
    require(
        account["address"] == str(PumpFunAddresses.find_fee_config()),
        "noncanonical_fee_account",
    )
    require(account["executable"] is False, "executable_fee_account")
    config = decode_fee_config_account(
        Account(
            raw(account["lamports"], "fee_lamports"),
            base64.b64decode(account["data_base64"], validate=True),
            Pubkey.from_string(account["owner"]),
            False,
            raw(account["rent_epoch"], "rent_epoch"),
        )
    )
    require(config.digest == payload["config_digest"], "fee_digest_mismatch")
    observed = clock(payload["observed_at"], "fee_observed")
    attested = clock(payload["attested_at"], "fee_attested")
    require(type(payload["native_attestation"]) is bool, "invalid_fee_attestation")
    if not payload["native_attestation"]:
        return None, "fee_snapshot_not_attested"
    return PumpFeeSnapshot(config, observed, attested), None


def validate_lock(
    lock: dict[str, Any],
    tape: Path,
    slots: Path,
) -> tuple[PumpFeeSnapshot | None, str | None]:
    exact(lock, lock_template(), "lock")
    require(
        type(lock["schema_version"]) is int and lock["schema_version"] == 1,
        "lock_version",
    )
    timestamp = datetime.fromisoformat(lock["locked_at_utc"].replace("Z", "+00:00"))
    require(
        timestamp.utcoffset() is not None
        and timestamp.utcoffset().total_seconds() == 0,
        "lock_time_not_UTC",
    )
    require(
        isinstance(lock["run_id"], str)
        and 1 <= len(lock["run_id"]) <= 128
        and all(c.isascii() and (c.isalnum() or c in "._-") for c in lock["run_id"]),
        "invalid_public_run_id",
    )
    require(
        isinstance(lock["run_journal_path"], str) and bool(lock["run_journal_path"]),
        "invalid_run_journal_path",
    )
    for path, key in ((tape, "tape_path"), (slots, "slot_clock_path")):
        require(
            (ROOT / lock[key]).resolve() == path.resolve(),
            f"locked_path_mismatch:{key}",
        )
    require(
        lock["policy"] == POLICY
        and all(type(lock["policy"][k]) is type(v) for k, v in POLICY.items()),
        "policy_not_prespecified",
    )
    exact(lock["capture"], lock_template()["capture"], "capture")
    capture = lock["capture"]
    require(
        capture["anchor"] == "first_processed_head_receive"
        and capture["commitment"] == "processed"
        and capture["split"] == "admission_time_midpoint",
        "unsupported_capture",
    )
    for key in ("warmup_seconds", "admission_seconds", "drain_seconds"):
        require(clock(capture[key], key) > 0, f"nonpositive:{key}")
    require(capture["drain_seconds"] >= 12, "drain_shorter_than_policy")
    exact(lock["coverage"], lock_template()["coverage"], "coverage")
    for key in (
        "max_head_gap_seconds",
        "max_state_age_seconds",
        "uncertainty_block_seconds",
    ):
        require(clock(lock["coverage"][key], key) > 0, f"nonpositive:{key}")
    require(
        type(lock["coverage"]["sell_slippage_bps"]) is int
        and 0 <= lock["coverage"]["sell_slippage_bps"] < 10000,
        "invalid_slippage",
    )
    require(lock["source_sha256"] == sources(), "exact_source_set_or_hash_mismatch")
    return fee_snapshot(lock["fee_input"])


def bind_journal(lock: dict[str, Any], heads: Heads) -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    """Verify capture identity/settings; retain partial capture rather than erase early marks."""
    path = (ROOT / lock["run_journal_path"]).resolve()
    digest = hashlib.sha256()
    rows, truncated = [], False
    before = path.stat()
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            if not line.endswith(b"\n"):
                truncated = True
                break
            row = strict_json(line)
            require(
                isinstance(row, dict)
                and row.get("run_id") == lock["run_id"]
                and row.get("schema_version") == 2,
                "journal_run_identity_mismatch",
            )
            clock(row["received_monotonic"], "journal_receive")
            rows.append(row)
    after = path.stat()
    require(
        (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
        "journal_changed_during_score",
    )
    require(bool(rows) and rows[0].get("kind") == "start", "journal_start_missing")
    starts = [r for r in rows if r.get("kind") == "start"]
    admissions = [r for r in rows if r.get("kind") == "admission"]
    terminals = [r for r in rows if r.get("kind") == "terminal"]
    require(
        len(starts) == 1 and len(admissions) == 1 and len(terminals) <= 1,
        "journal_manifest_ambiguous",
    )
    require(not terminals or terminals[0] is rows[-1], "journal_records_after_terminal")
    start, admission = starts[0], admissions[0]
    require(start.get("commitment") == "processed", "journal_commitment_mismatch")
    require(
        start.get("recorder_sha256")
        == lock["source_sha256"][
            "learning-examples/token-lifecycles/record_lifecycles.py"
        ],
        "journal_recorder_hash_mismatch",
    )
    for name in ("pump_fun_idl.json", "pump_swap_idl.json"):
        require(
            start["idl_sha256"][name] == lock["source_sha256"][f"idl/{name}"],
            "journal_idl_hash_mismatch",
        )
    bounds = start["bounds"]
    capture = lock["capture"]
    admission_seconds = capture["warmup_seconds"] + capture["admission_seconds"]
    drain_seconds = capture["drain_seconds"]
    require(
        bounds.get("admission_anchor") == "first_processed_slot_receive"
        and bounds.get("admission_seconds") == admission_seconds
        and bounds.get("drain_seconds") == drain_seconds,
        "journal_capture_settings_mismatch",
    )
    first = heads.times[0]
    cutoff = first + admission_seconds
    planned_end = cutoff + drain_seconds
    deadline = min(
        clock(bounds["hard_deadline_monotonic"], "hard_deadline"), planned_end
    )
    expected = {
        "first_head_slot": heads.slots[0],
        "first_head_received_monotonic": first,
        "admission_seconds": admission_seconds,
        "drain_seconds": drain_seconds,
        "cutoff_monotonic": cutoff,
        "drain_deadline_monotonic": deadline,
    }
    require(
        admission.get("start_monotonic") == first
        and all(admission.get(k) == v for k, v in expected.items()),
        "journal_admission_anchor_or_cutoff_mismatch",
    )
    require(
        start["received_monotonic"] <= first <= admission["received_monotonic"],
        "journal_anchor_clock_mismatch",
    )
    terminal = terminals[0] if terminals else None
    if terminal:
        require(
            type(terminal.get("complete")) is bool
            and type(terminal.get("exit_code")) is int,
            "journal_terminal_status_unknown",
        )
        require(
            clock(terminal["ended_monotonic"], "terminal_end") >= heads.times[-1],
            "journal_terminal_before_last_head",
        )
        require(
            all(terminal.get(k) == v for k, v in expected.items()),
            "journal_terminal_capture_settings_mismatch",
        )
    partial_reasons = []
    if truncated:
        partial_reasons.append("journal_truncated")
    if terminal is None:
        partial_reasons.append("terminal_missing")
    elif not terminal["complete"] or terminal["exit_code"] != 0:
        partial_reasons.append("terminal_incomplete")
    if terminal and terminal["ended_monotonic"] < planned_end:
        partial_reasons.append("terminal_before_locked_schedule")
    if terminal and (
        terminal.get("unflushed_coins") != 0
        or terminal.get("open_gap_start_monotonic") is not None
        or terminal.get("open_reconnect_start_monotonic") is not None
    ):
        partial_reasons.append("terminal_unflushed_or_open_coverage_gap")
    if deadline < planned_end:
        partial_reasons.append("hard_deadline_truncates_locked_drain")
    if heads.times[-1] + lock["coverage"]["max_head_gap_seconds"] < planned_end:
        partial_reasons.append("slot_clock_stale_at_locked_schedule_end")
    open_gaps, gaps = {}, []
    for row in rows:
        kind = row.get("kind", "")
        if kind in ("gap_start", "reconnect_start"):
            family = kind.removesuffix("_start")
            at = clock(row["start_monotonic"], "journal_gap_start")
            require(
                family not in open_gaps or open_gaps[family] == at,
                "overlapping_same_kind_journal_gaps",
            )
            open_gaps[family] = at
        elif kind in ("gap_end", "reconnect_end"):
            family = kind.removesuffix("_end")
            at = clock(row["start_monotonic"], "journal_gap_start")
            end = clock(row["end_monotonic"], "journal_gap_end")
            previous = open_gaps.pop(family, None)
            require(
                (previous is None or previous == at) and end >= at,
                "journal_gap_pair_mismatch",
            )
            gaps.append((at, end))
    gaps.extend((at, None) for at in open_gaps.values())
    heads.journal_gaps = gaps
    economic_start = first + capture["warmup_seconds"]
    if any(
        left < cutoff and (right is None or right >= economic_start)
        for left, right in gaps
    ):
        partial_reasons.append("admission_journal_gap_missing_creation_denominator")
    if any(
        heads.bad[i] != heads.bad[i - 1]
        and heads.times[i - 1] < cutoff
        and heads.times[i] >= economic_start
        for i in range(1, len(heads.times))
    ):
        partial_reasons.append("admission_head_gap_missing_creation_denominator")
    return {
        "run_id": lock["run_id"],
        "path": str(path),
        "sha256": digest.hexdigest(),
        "settings_verified": True,
        "capture_status": "partial_censored" if partial_reasons else "complete",
        "partial_reasons": partial_reasons,
        "terminal": terminal,
        "planned_end": planned_end,
        "effective_deadline": deadline,
        "coverage_gap_intervals": gaps,
    }


class Heads:
    """One receive-time index and one bad-edge prefix sum for the entire run."""

    def __init__(self, rows: list[dict[str, Any]], max_gap: float) -> None:
        require(bool(rows), "slot_clock_empty")
        self.rows = rows
        self.times = [clock(r["received_monotonic"], "head") for r in rows]
        self.slots = [raw(r["slot"], "head_slot") for r in rows]
        self.bad = [0]
        self.journal_gaps = []
        for i, row in enumerate(rows):
            gap = row["gap_seconds"]
            if i or gap is not None:
                gap = clock(gap, "head_gap")
            if i:
                require(self.times[i] >= self.times[i - 1], "head_clock_reordered")
                bad = (
                    self.times[i] - self.times[i - 1] > max_gap
                    or gap > max_gap
                    or self.slots[i] <= self.slots[i - 1]
                )
                self.bad.append(self.bad[-1] + int(bad))
        self.max_gap = max_gap

    def current(self, when: float) -> int:
        index = bisect_right(self.times, when) - 1
        require(index >= 0, "head_before_decision_unavailable")
        require(when - self.times[index] <= self.max_gap, "decision_head_stale")
        return index

    def next(self, when: float, minimum_slot: int) -> int:
        for i in range(bisect_left(self.times, when), len(self.times)):
            if self.slots[i] >= minimum_slot:
                return i
        raise ValueError("required_future_head_unavailable")

    def coverage(self, start: float, end: float) -> None:
        first = bisect_right(self.times, start) - 1
        last = bisect_left(self.times, end)
        require(first >= 0 and last < len(self.times), "coverage_not_bracketed")
        require(self.bad[last] == self.bad[first], "relevant_head_gap_or_reordering")
        require(
            not any(
                left <= end and (right is None or right >= start)
                for left, right in self.journal_gaps
            ),
            "relevant_journal_coverage_gap",
        )


def evidence(coin: dict[str, Any]) -> list[dict[str, Any]]:
    require(coin.get("schema_version") == 2, "lifecycle_schema_not_v2")
    require(
        coin.get("clock") == "local_monotonic_receive_time", "unsupported_receive_clock"
    )
    start = clock(coin["create_received_monotonic"], "creation")
    arrays = [
        coin[k]
        for k in (
            "trades",
            "trade_received_ms",
            "trade_real_token_reserves",
            "trade_signatures",
            "trade_event_indices",
            "trade_fields",
        )
    ]
    require(
        all(isinstance(a, list) and len(a) == len(arrays[0]) for a in arrays),
        "unaligned_trade_evidence",
    )
    events, seen, last = [], set(), -1
    coin_quote = coin.get("quote_mint")
    require(isinstance(coin_quote, str), "quote_provenance_unknown")
    coin_quote = normalize_quote_mint(Pubkey.from_string(coin_quote))
    require(coin_quote == WSOL_MINT, "unsupported_quote")
    for trade, offset, real_tokens, sig, log_index, fields in zip(*arrays, strict=True):
        offset = clock(offset, "trade_offset")  # noqa: PLW2901
        require(offset >= last, "trade_receive_order_ambiguous")
        last = offset
        require(isinstance(sig, str) and bool(sig), "trade_signature_unknown")
        log_index = raw(log_index, "event_index")  # noqa: PLW2901
        require((sig, log_index) not in seen, "duplicate_trade_evidence")
        seen.add((sig, log_index))
        require(
            isinstance(trade, list) and len(trade) == 9 and isinstance(fields, dict),
            "malformed_trade",
        )
        require(fields.get("mint") == coin["mint"], "trade_mint_mismatch")
        # Legacy SOL-only events predate quote_mint. Modern quantities require
        # explicit asset evidence; native SOL's default pubkey normalizes to WSOL.
        if "quote_mint" in fields or any(
            key in fields
            for key in ("quote_amount", "real_quote_reserves", "virtual_quote_reserves")
        ):
            require(isinstance(fields.get("quote_mint"), str), "trade_quote_unknown")
            require(
                normalize_quote_mint(Pubkey.from_string(fields["quote_mint"]))
                == coin_quote,
                "trade_quote_mismatch",
            )
        slot = raw(coin["create_slot"], "create_slot") + raw(trade[0], "trade_dslot")
        require(
            fields.get("user") == trade[1]
            and type(fields.get("is_buy")) is bool
            and int(fields["is_buy"]) == trade[2],
            "trade_identity_mismatch",
        )
        for index, name, legacy in (
            (3, "quote_amount", "sol_amount"),
            (4, "token_amount", "token_amount"),
            (5, "real_quote_reserves", "real_sol_reserves"),
            (6, "virtual_quote_reserves", "virtual_sol_reserves"),
            (7, "virtual_token_reserves", "virtual_token_reserves"),
        ):
            require(
                raw(trade[index], name)
                == raw(fields.get(name, fields.get(legacy)), name),
                f"trade_field_mismatch:{name}",
            )
        # Missing real reserves stay unavailable at the selected state, not zero.
        require(
            real_tokens == fields.get("real_token_reserves"),
            "real_token_evidence_mismatch",
        )
        events.append(
            {
                "at": start + offset / 1000,
                "slot": slot,
                "signature": sig,
                "event_index": log_index,
                "fields": fields,
            }
        )
    return events


def universe(coin: dict[str, Any]) -> str:
    provenance = coin.get("create_provenance")
    require(
        isinstance(provenance, dict)
        and provenance.get("verified") is True
        and isinstance(provenance.get("reason"), str),
        "unsupported_creation_provenance",
    )
    fields = coin["create_fields"]
    require(
        fields.get("mint") == coin["mint"] and fields.get("creator") == coin["creator"],
        "creation_identity_mismatch",
    )
    require(
        type(coin.get("mayhem")) is bool
        and fields.get("is_mayhem_mode") is coin["mayhem"],
        "mayhem_provenance_unknown",
    )
    require(
        fields.get("quote_mint") == coin.get("quote_mint"), "quote_provenance_mismatch"
    )
    require(
        normalize_quote_mint(Pubkey.from_string(coin["quote_mint"])) == WSOL_MINT,
        "unsupported_quote",
    )
    # Canonical create instructions fix six decimals; explicit contradicting evidence is refused.
    require(
        TOKEN_DECIMALS == 6
        and all(fields.get(k, 6) == 6 for k in ("decimals", "token_decimals"))
        and coin.get("token_decimals", 6) == 6,
        "unsupported_decimals",
    )
    require(
        raw(coin["supply"], "supply")
        == raw(fields["token_total_supply"], "create_supply"),
        "supply_mismatch",
    )
    return "source_bound_canonical_create_six_decimals"


def state_at(  # noqa: PLR0913
    coin: dict[str, Any],
    events: list[dict[str, Any]],
    when: float,
    head: int,
    max_age: float,
    creation: bool = False,  # noqa: FBT001, FBT002
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [
        e
        for e in events
        if e["at"] <= when
        and e["slot"] <= head
        and (not creation or e["signature"] == coin["create_sig"])
    ]
    if creation:
        own = [e for e in events if e["signature"] == coin["create_sig"]]
        require(
            all(
                e["at"] == coin["create_received_monotonic"]
                and e["slot"] == coin["create_slot"]
                for e in own
            ),
            "creation_transaction_receive_mismatch",
        )
        if own:
            selected = max(own, key=lambda e: e["event_index"])
            require(
                selected in candidates,
                "creation_post_transaction_state_not_yet_observed",
            )
        else:
            # No initial-reserve fallback: CreateEvent does not establish full final reserves.
            raise ValueError("creation_post_transaction_reserves_unavailable")
    else:
        require(bool(candidates), "received_state_unavailable")
        selected = max(candidates, key=lambda e: (e["at"], e["slot"], e["event_index"]))
    require(when - selected["at"] <= max_age, "required_state_stale")
    fields = selected["fields"]
    state = {
        "complete": False,
        "creator": coin["creator"],
        "quote_mint": coin["quote_mint"],
        "token_total_supply": raw(coin["supply"], "supply"),
    }
    for name, source in (
        ("virtual_quote_reserves", "virtual_sol_reserves"),
        ("virtual_token_reserves", "virtual_token_reserves"),
        ("real_quote_reserves", "real_sol_reserves"),
        ("real_token_reserves", "real_token_reserves"),
    ):
        state[name] = raw(fields.get(name, fields.get(source)), name)
    return state, {
        "signature": selected["signature"],
        "event_index": selected["event_index"],
        "slot": selected["slot"],
        "received_monotonic": selected["at"],
        "age_seconds": when - selected["at"],
        "reserves": state,
    }


def flow_at(
    coin: dict[str, Any],
    events: list[dict[str, Any]],
    when: float,
    head: int,
) -> dict[str, int | str]:
    buy = sell = creator_sell = 0
    buyers = set()
    for event in events:
        if event["at"] > when or event["slot"] > head:
            continue
        fields = event["fields"]
        amount = raw(fields.get("quote_amount", fields.get("sol_amount")), "flow_quote")
        user = fields["user"]
        if user == coin["creator"] and not fields["is_buy"]:
            creator_sell += amount
        if user in (coin["creator"], MAYHEM_SOL_VAULT):
            continue
        if fields["is_buy"]:
            buy += amount
            buyers.add(user)
        else:
            sell += amount
    return {
        "independent_buy_lamports": buy,
        "independent_sell_lamports": sell,
        "independent_net_lamports": buy - sell,
        "independent_buyers": len(buyers),
        "creator_sell_lamports": creator_sell,
        "flow_stratum": "positive" if buy > sell else "nonpositive",
        "creator_stratum": "sold" if creator_sell else "no_observed_sale",
    }


def evaluate(  # noqa: PLR0913, PLR0915
    coin: dict[str, Any],
    arm: str,
    heads: Heads,
    lock: dict[str, Any],
    snapshot: PumpFeeSnapshot | None,
    fee_reason: str | None,
) -> dict[str, Any]:
    row = {
        "mint": coin.get("mint"),
        "creator": coin.get("creator"),
        "arm": arm,
        "mayhem": coin.get("mayhem"),
        "status": "unpriced_not_entered",
        "reason": None,
        "entered": False,
        "inventory_raw": None,
        "modeled_net_lamports": None,
        "extra_cost_breakeven_lamports": None,
        "actual_net_lamports": None,
        "unknown_costs": [
            "inclusion",
            "tips",
            "failed_attempts",
            "rent_net",
            "future_fee_changes",
        ],
    }
    try:
        started = clock(coin["create_received_monotonic"], "creation")
        row["creation_received_monotonic"] = started
        capture = lock["capture"]
        admission = heads.times[0] + capture["warmup_seconds"]
        end = admission + capture["admission_seconds"]
        if not admission <= started < end:
            row["half"] = "outside_admission"
            row.update(
                status="outside_admission",
                reason="warmup" if started < admission else "drain_or_after",
            )
            return row
        row["half"] = "discovery" if started < (admission + end) / 2 else "holdout"
        row["decimals_basis"] = universe(coin)
        row["supported"] = True
        events = evidence(coin)
        covered = started + clock(coin["observed_duration_ms"], "duration") / 1000
        coverage = lock["coverage"]
        failures = coin.get("event_evidence_failures")
        require(isinstance(failures, list), "event_evidence_coverage_unknown")
        require(
            coin.get("event_evidence_complete") is True or bool(failures),
            "untimed_event_evidence_failure",
        )
        failure_times = [
            clock(failure["received_monotonic"], "evidence_failure")
            for failure in failures
        ]

        def require_interval(until: float) -> None:
            require(until <= covered, "coin_observation_ended")
            require(
                until <= end + capture["drain_seconds"],
                "required_state_after_locked_drain",
            )
            heads.coverage(started, until)
            require(
                not any(started <= timestamp <= until for timestamp in failure_times),
                "relevant_event_evidence_failure",
            )
            graduated = coin.get("graduated_received_monotonic")
            if coin.get("graduated_dslot") is not None and graduated is None:
                raise ValueError("graduation_receive_time_unknown")  # noqa: TRY301
            if graduated is not None:
                require(
                    clock(graduated, "graduation") > until, "graduation_before_closure"
                )

        if arm == "C":
            at, head = started, coin["create_slot"]
            # The complete received transaction is itself the C slot evidence.
        else:
            index = heads.next(started, coin["create_slot"] + POLICY["d2_delay_slots"])
            at, head = heads.times[index], heads.slots[index]
        row.update(
            entry_received_monotonic=at,
            entry_slot=head,
            entry_slot_lag=head - coin["create_slot"],
        )
        row["modeled_max_entry_input_lamports"] = POLICY["max_entry_quote_lamports"]
        row["modeled_network_cleanup_estimates_lamports"] = {
            "buy": POLICY["buy_network_lamports"],
            "sell": POLICY["sell_network_lamports"],
            "cleanup": POLICY["cleanup_lamports"],
        }
        require_interval(at)
        if arm == "D2":
            row["d2_flow"] = flow_at(coin, events, at, head)
        state, proof = state_at(
            coin, events, at, head, coverage["max_state_age_seconds"], arm == "C"
        )
        row["entry_state"] = proof
        require(snapshot is not None, fee_reason or "unpriced_fees")
        quote = quote_buy_exact_out(state, POLICY["quantity_raw"], snapshot)
        row["entry_quote"] = asdict(quote)
        if quote.amount_in_raw > POLICY["max_entry_quote_lamports"]:
            row.update(
                status="not_entered", reason="entry_quote_above_cap", inventory_raw=0
            )
            return row
        row.update(
            entered=True,
            inventory_raw=POLICY["quantity_raw"],
            status="entered_unpriced",
        )
        trigger_at = at + POLICY["hold_seconds"]
        require_interval(trigger_at)
        trigger_head = heads.slots[heads.current(trigger_at)]
        require(trigger_head >= head, "trigger_head_behind_entry")
        trigger_state, trigger_proof = state_at(
            coin, events, trigger_at, trigger_head, coverage["max_state_age_seconds"]
        )
        row["trigger_state"] = trigger_proof
        trigger_quote = quote_sell_exact_in(
            trigger_state, POLICY["quantity_raw"], snapshot
        )
        floor = minimum_output_with_slippage(
            trigger_quote.amount_out_raw, coverage["sell_slippage_bps"]
        )
        row.update(trigger_quote=asdict(trigger_quote), exit_floor_lamports=floor)
        exit_index = heads.next(trigger_at, trigger_head + POLICY["exit_delay_slots"])
        exit_at, exit_head = heads.times[exit_index], heads.slots[exit_index]
        require_interval(exit_at)
        exit_state, exit_proof = state_at(
            coin, events, exit_at, exit_head, coverage["max_state_age_seconds"]
        )
        row["exit_state"] = exit_proof
        exit_quote = quote_sell_exact_in(exit_state, POLICY["quantity_raw"], snapshot)
        row.update(
            exit_quote=asdict(exit_quote),
            exit_received_monotonic=exit_at,
            exit_slot=exit_head,
            exit_delay_seconds=exit_at - trigger_at,
        )
        require(exit_quote.amount_out_raw >= floor, "exit_slippage_floor_failed")
        net = (
            exit_quote.amount_out_raw
            - quote.amount_in_raw
            - sum(
                POLICY[k]
                for k in (
                    "buy_network_lamports",
                    "sell_network_lamports",
                    "cleanup_lamports",
                )
            )
        )
        row.update(
            status="conditionally_priced",
            reason="full_quantity_observed_state_quote",
            inventory_raw=0,
            modeled_net_lamports=net,
            extra_cost_breakeven_lamports=net,
            modeled_network_cleanup_lamports=65000,
        )
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        row["reason"] = str(exc)
    return row


def distribution(values: list[int]) -> dict[str, int | str | None]:
    values = sorted(values)
    if not values:
        return {
            "count": 0,
            "sum": None,
            "mean": None,
            "min": None,
            "p05": None,
            "median": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values),
        "sum": sum(values),
        "mean": str(Decimal(sum(values)) / len(values)),
        "min": values[0],
        "p05": values[(len(values) - 1) * 5 // 100],
        "median": str(
            (Decimal(values[(len(values) - 1) // 2]) + values[len(values) // 2]) / 2
        ),
        "p95": values[(len(values) - 1) * 95 // 100],
        "max": values[-1],
    }


def summarize(
    rows: list[dict[str, Any]],
    block_seconds: float,
    anchor: float,
) -> dict[str, Any]:
    priced = [r for r in rows if r["modeled_net_lamports"] is not None]
    values = [r["modeled_net_lamports"] for r in priced]
    groups = {
        "mint": defaultdict(int),
        "creator": defaultdict(int),
        "time_block": defaultdict(int),
    }
    blocks = defaultdict(list)
    for row in priced:
        net = row["modeled_net_lamports"]
        block = int((row["creation_received_monotonic"] - anchor) // block_seconds)
        blocks[block].append(net)
        for name, key in (
            ("mint", row["mint"]),
            ("creator", row["creator"]),
            ("time_block", str(block)),
        ):
            groups[name][key] += net
    concentration = {}
    for name, totals in groups.items():
        positive = sorted((v for v in totals.values() if v > 0), reverse=True)
        concentration[name] = {
            "groups": len(totals),
            "positive_net_sum_lamports": sum(positive),
            "top_positive_group_lamports": positive[0] if positive else None,
            "top_five_positive_sum_lamports": sum(positive[:5]) if positive else None,
            "largest_absolute_group_lamports": max(
                map(abs, totals.values()), default=None
            ),
        }
    uncertainty = {
        "method": "deterministic_1000_resample_time_block_bootstrap_priced_only",
        "seed": 0,
        "block_seconds": block_seconds,
        "priced_blocks": len(blocks),
        "mean_interval_95_lamports": None,
    }
    if len(blocks) >= 2:
        rng = random.Random(0)  # noqa: S311 - reproducible statistical resampling, not secrets
        totals = [(sum(v), len(v)) for _, v in sorted(blocks.items())]
        means = []
        for _ in range(1000):
            draw = rng.choices(totals, k=len(totals))
            means.append(Decimal(sum(v[0] for v in draw)) / sum(v[1] for v in draw))
        means.sort()
        uncertainty["mean_interval_95_lamports"] = [str(means[24]), str(means[974])]
    return {
        "all_rows": len(rows),
        "admitted_creations": sum(r["status"] != "outside_admission" for r in rows),
        "supported": sum(r.get("supported") is True for r in rows),
        "statuses": dict(Counter(r["status"] for r in rows)),
        "reasons": dict(Counter(r["reason"] for r in rows)),
        "entered": sum(r["entered"] for r in rows),
        "priced": len(priced),
        "entered_unpriced": sum(
            r["entered"] and r["modeled_net_lamports"] is None for r in rows
        ),
        "unknown_inventory_rows": sum(r["inventory_raw"] is None for r in rows),
        "pending_inventory_raw": sum(
            r["inventory_raw"] for r in rows if r["inventory_raw"] is not None
        ),
        "actual_net_unknown": len(rows),
        "modeled_wins": sum(v > 0 for v in values),
        "modeled_losses": sum(v < 0 for v in values),
        "modeled_flat": values.count(0),
        "conditional_net_lamports": distribution(values),
        "concentration": concentration,
        "uncertainty": uncertainty,
    }


def compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: row[k]
        for k in (
            "mint",
            "creator",
            "arm",
            "mayhem",
            "half",
            "status",
            "reason",
            "entered",
            "inventory_raw",
            "modeled_net_lamports",
            "creation_received_monotonic",
            "supported",
            "d2_flow",
        )
        if k in row
    }


def summaries(
    rows: list[dict[str, Any]],
    lock: dict[str, Any],
    anchor: float,
) -> dict[str, Any]:
    block = lock["coverage"]["uncertainty_block_seconds"]
    result = {}
    for half in ("all", "discovery", "holdout"):
        subset = [r for r in rows if half == "all" or r.get("half") == half]
        arms = {}
        for arm in POLICY["arms"]:
            arm_rows = [r for r in subset if r["arm"] == arm]
            strata = defaultdict(list)
            for row in arm_rows:
                strata[f"mayhem:{row['mayhem']}"].append(row)
                if arm == "D2":
                    flow = row.get("d2_flow", {})
                    strata[f"flow:{flow.get('flow_stratum', 'unknown')}"].append(row)
                    strata[f"creator:{flow.get('creator_stratum', 'unknown')}"].append(
                        row
                    )
            arms[arm] = {
                "overall": summarize(arm_rows, block, anchor),
                "strata": {
                    key: summarize(value, block, anchor)
                    for key, value in sorted(strata.items())
                },
            }
        pairs = defaultdict(dict)
        for row in subset:
            pairs[row["mint"]][row["arm"]] = row
        paired = []
        patterns = Counter()
        for pair in pairs.values():
            c, d = pair["C"], pair["D2"]
            patterns[f"{c['status']}|{d['status']}"] += 1
            if (
                c["modeled_net_lamports"] is not None
                and d["modeled_net_lamports"] is not None
            ):
                paired.append(
                    {
                        **d,
                        "modeled_net_lamports": d["modeled_net_lamports"]
                        - c["modeled_net_lamports"],
                    }
                )
        result[half] = {
            "arms": arms,
            "paired": {
                "all_mints": len(pairs),
                "status_pairs": dict(patterns),
                "both_priced_D2_minus_C": summarize(paired, block, anchor),
            },
        }
    return result


def score(tape: Path, slots: Path, lock_path: Path, out: Path) -> dict[str, Any]:
    lock_bytes = lock_path.read_bytes()
    lock = strict_json(lock_bytes)
    snapshot, fee_reason = validate_lock(lock, tape, slots)
    slot_hash = hashlib.sha256()
    head_rows = []
    with slots.open("rb") as stream:
        for line in stream:
            slot_hash.update(line)
            require(line.endswith(b"\n"), "incomplete_slot_record")
            head_rows.append(strict_json(line))
    require(
        all(
            row.get("run_id") == lock["run_id"] and row.get("schema_version") == 2
            for row in head_rows
        ),
        "slot_run_identity_mismatch",
    )
    heads = Heads(head_rows, lock["coverage"]["max_head_gap_seconds"])
    journal = bind_journal(lock, heads)
    tape_hash = hashlib.sha256()
    rows, seen = [], set()
    initial_stat = tape.stat()
    with out.open("x", encoding="utf-8") as output, tape.open("rb") as stream:
        output.write('{"schema_version":1,"rows":[')
        first = True
        for line in stream:
            tape_hash.update(line)
            require(line.endswith(b"\n"), "incomplete_lifecycle_record")
            coin = strict_json(line)
            require(
                isinstance(coin, dict) and isinstance(coin.get("mint"), str),
                "invalid_coin_identity",
            )
            require(
                coin.get("run_id") == lock["run_id"], "lifecycle_run_identity_mismatch"
            )
            require(coin["mint"] not in seen, "duplicate_mint_record")
            seen.add(coin["mint"])
            for arm in POLICY["arms"]:
                row = evaluate(coin, arm, heads, lock, snapshot, fee_reason)
                output.write(
                    ("" if first else ",")
                    + json.dumps(row, separators=(",", ":"), allow_nan=False)
                )
                first = False
                rows.append(compact(row))
        require(
            (initial_stat.st_size, initial_stat.st_mtime_ns)
            == (tape.stat().st_size, tape.stat().st_mtime_ns),
            "tape_changed_during_score",
        )
        terminal = journal["terminal"]
        if terminal and (
            terminal.get("written") != len(seen)
            or terminal.get("counts", {}).get("slot_updates") != len(head_rows)
        ):
            journal["partial_reasons"].append("terminal_artifact_row_count_mismatch")
            journal["capture_status"] = "partial_censored"
        capture = lock["capture"]
        required_end = heads.times[0] + sum(
            capture[k] for k in ("warmup_seconds", "admission_seconds", "drain_seconds")
        )
        report = {
            "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "tape_sha256": tape_hash.hexdigest(),
            "slot_clock_sha256": slot_hash.hexdigest(),
            "source_binding": "exact_required_set_verified",
            "run_journal": journal,
            "capture": {
                "first_head": heads.times[0],
                "last_head": heads.times[-1],
                "scheduled_end": required_end,
                "scheduled_window_complete": journal["capture_status"] == "complete",
                "last_head_at_or_after_scheduled_end": heads.times[-1] >= required_end,
                "status": journal["capture_status"],
                "partial_reasons": journal["partial_reasons"],
                "coins": len(seen),
                "heads": len(head_rows),
            },
            "fee_reason": fee_reason,
            "summary": summaries(rows, lock, heads.times[0]),
            "limits": LIMITS,
        }
        # The final marker is distinct: failures leave invalid JSON, never a success report.
        output.write(
            '],"result":'
            + json.dumps(report, separators=(",", ":"), allow_nan=False)
            + ',"scoring_complete":true}\n'
        )
    return {
        "out": str(out),
        "coins": len(seen),
        "rows": len(rows),
        "capture_complete": journal["capture_status"] == "complete",
    }


def self_check() -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    from copy import deepcopy  # noqa: PLC0415
    from tempfile import TemporaryDirectory  # noqa: PLC0415

    creator, mint = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    fees = PumpFees(0, 95, 30)
    snapshot = PumpFeeSnapshot(
        PumpFeeConfig(
            1,
            Pubkey.default(),
            fees,
            (PumpFeeTier(0, fees),),
            (PumpFeeTier(0, fees),),
            fees,
            "synthetic",
        ),
        0,
        0,
    )
    coin = {
        "schema_version": 2,
        "mint": mint,
        "creator": creator,
        "quote_mint": str(WSOL_MINT),
        "mayhem": False,
        "supply": 10**15,
        "create_slot": 100,
        "create_sig": "create",
        "create_received_monotonic": 1,
        "clock": "local_monotonic_receive_time",
        "observed_duration_ms": 20000,
        "graduated_dslot": None,
        "graduated_received_monotonic": None,
        "create_provenance": {"verified": True, "reason": "synthetic_canonical"},
        "event_evidence_complete": True,
        "event_evidence_failures": [],
        "create_fields": {
            "mint": mint,
            "creator": creator,
            "quote_mint": str(WSOL_MINT),
            "is_mayhem_mode": False,
            "token_total_supply": 10**15,
            "virtual_sol_reserves": 30000000000,
        },
        "trades": [],
        "trade_fields": [],
        "trade_received_ms": [],
        "trade_signatures": [],
        "trade_event_indices": [],
        "trade_real_token_reserves": [],
    }
    for offset, index, sol in (
        (0, 1, 31000000000),
        (0, 3, 32000000000),
        (11000, 4, 36000000000),
        (11500, 5, 36500000000),
    ):
        fields = {
            "mint": mint,
            "user": creator,
            "is_buy": True,
            "sol_amount": 1000000,
            "token_amount": 1000000,
            "real_sol_reserves": 6000000000,
            "virtual_sol_reserves": sol,
            "virtual_token_reserves": 1000000000000000,
            "real_token_reserves": 800000000000000,
        }
        coin["trades"].append(
            [
                0 if not offset else 22,
                creator,
                1,
                1000000,
                1000000,
                fields["real_sol_reserves"],
                sol,
                fields["virtual_token_reserves"],
                0,
            ]
        )
        coin["trade_fields"].append(fields)
        coin["trade_received_ms"].append(offset)
        coin["trade_signatures"].append("create" if not offset else str(index))
        coin["trade_event_indices"].append(index)
        coin["trade_real_token_reserves"].append(fields["real_token_reserves"])
    events = evidence(coin)
    state, proof = state_at(coin, events, 1, 100, 2, creation=True)
    assert state["virtual_quote_reserves"] == 32000000000 and proof["event_index"] == 3
    state, _ = state_at(coin, events, 11.5, 121, 20)
    assert (
        state["virtual_quote_reserves"] == 32000000000
    )  # Future arrival and slot excluded.
    head_rows = [
        {"received_monotonic": i / 2, "slot": 98 + i, "gap_seconds": 0.5}
        for i in range(43)
    ]
    heads = Heads(head_rows, 1)
    lock = lock_template()
    lock["capture"].update(warmup_seconds=0.5, admission_seconds=30, drain_seconds=20)
    lock["coverage"]["max_state_age_seconds"] = 20
    refused = deepcopy(coin)
    refused.update(
        event_evidence_complete=False,
        event_evidence_failures=[{"received_monotonic": 20, "reason": "late_decode"}],
    )
    assert (
        evaluate(refused, "C", heads, lock, snapshot, None)["status"]
        == "conditionally_priced"
    )
    refused["event_evidence_failures"][0]["received_monotonic"] = 1
    assert (
        evaluate(refused, "C", heads, lock, snapshot, None)["reason"]
        == "relevant_event_evidence_failure"
    )
    priced = evaluate(coin, "C", heads, lock, snapshot, None)
    assert priced["status"] == "conditionally_priced", priced
    modern = deepcopy(coin)
    for fields in modern["trade_fields"]:
        fields["quote_mint"] = str(Pubkey.default())
        for canonical, legacy in (
            ("quote_amount", "sol_amount"),
            ("real_quote_reserves", "real_sol_reserves"),
            ("virtual_quote_reserves", "virtual_sol_reserves"),
        ):
            fields[canonical], fields[legacy] = fields[legacy], 0
    assert evaluate(modern, "C", heads, lock, snapshot, None) == priced
    flow_events = deepcopy(evidence(modern))
    for event in flow_events:
        event["fields"]["user"] = mint
    assert flow_at(modern, flow_events, 1, 100)["independent_buy_lamports"] == 2000000
    for quote, reason in (
        (str(Pubkey.new_unique()), "trade_quote_mismatch"),
        (None, "trade_quote_unknown"),
    ):
        invalid = deepcopy(modern)
        invalid["trade_fields"][0]["quote_mint"] = quote
        rejected = evaluate(invalid, "C", heads, lock, snapshot, None)
        assert rejected["reason"] == reason and rejected["modeled_net_lamports"] is None
    zero = deepcopy(modern)
    for trade, fields in zip(zero["trades"], zero["trade_fields"], strict=True):
        fields.update(
            quote_amount=0,
            sol_amount=1000000,
            virtual_quote_reserves=0,
            virtual_sol_reserves=32000000000,
        )
        trade[3] = trade[6] = 0
    zero_events = evidence(zero)
    assert (
        state_at(zero, zero_events, 1, 100, 2, creation=True)[0][
            "virtual_quote_reserves"
        ]
        == 0
    )
    for event in zero_events:
        event["fields"]["user"] = mint
    assert flow_at(zero, zero_events, 1, 100)["independent_buy_lamports"] == 0
    missing = deepcopy(modern)
    missing["trade_fields"][0]["quote_amount"] = None
    assert (
        evaluate(missing, "C", heads, lock, snapshot, None)["reason"]
        == "invalid_raw:quote_amount"
    )
    late_gap = deepcopy(head_rows)
    late_gap[-1]["gap_seconds"] = 10
    assert (
        evaluate(coin, "C", Heads(late_gap, 1), lock, snapshot, None)["status"]
        == "conditionally_priced"
    )
    early_gap = deepcopy(head_rows)
    early_gap[10]["gap_seconds"] = 10
    pending = evaluate(coin, "C", Heads(early_gap, 1), lock, snapshot, None)
    assert (
        pending["status"] == "entered_unpriced"
        and pending["inventory_raw"] == POLICY["quantity_raw"]
    )
    assert pending["modeled_net_lamports"] is None
    nofee = evaluate(coin, "C", heads, lock, None, "fee_snapshot_unavailable")
    assert nofee["modeled_net_lamports"] is None and nofee["inventory_raw"] is None
    d2 = evaluate(coin, "D2", heads, lock, snapshot, None)
    assert d2["entry_slot"] == 102 and d2["entry_received_monotonic"] == 2
    assert d2["d2_flow"]["independent_buyers"] == 0
    # Exercise the real streaming writer, exact lock, unknown fees and exclusive output.
    with TemporaryDirectory(prefix="creation-paper-check-") as directory:
        directory = Path(directory)  # noqa: PLW2901
        coin["run_id"] = "creation-paper-self-check"
        for head in head_rows:
            head.update(run_id=coin["run_id"], schema_version=2)
        journal_path = directory / "run.jsonl"
        tape, slots, lock_path, out = (
            directory / name
            for name in ("tape.jsonl", "slots.jsonl", "lock.json", "report.json")
        )
        tape.write_text(json.dumps(coin) + "\n")
        slots.write_text("".join(json.dumps(row) + "\n" for row in head_rows))
        check_lock = deepcopy(lock)
        check_lock.update(
            locked_at_utc="2026-01-01T00:00:00Z",
            tape_path=str(tape),
            slot_clock_path=str(slots),
            run_id=coin["run_id"],
            run_journal_path=str(journal_path),
            fee_input=None,
            source_sha256=sources(),
        )
        lock_path.write_text(json.dumps(check_lock))
        admission_seconds = (
            check_lock["capture"]["warmup_seconds"]
            + check_lock["capture"]["admission_seconds"]
        )
        journal_rows = [
            {
                "kind": "start",
                "commitment": "processed",
                "received_monotonic": 0,
                "recorder_sha256": check_lock["source_sha256"][
                    "learning-examples/token-lifecycles/record_lifecycles.py"
                ],
                "idl_sha256": {
                    name: check_lock["source_sha256"][f"idl/{name}"]
                    for name in ("pump_fun_idl.json", "pump_swap_idl.json")
                },
                "bounds": {
                    "admission_seconds": admission_seconds,
                    "drain_seconds": 20,
                    "admission_anchor": "first_processed_slot_receive",
                    "hard_deadline_monotonic": 100,
                },
            },
            {
                "kind": "admission",
                "received_monotonic": 0,
                "start_monotonic": 0,
                "first_head_slot": head_rows[0]["slot"],
                "first_head_received_monotonic": 0,
                "admission_seconds": admission_seconds,
                "drain_seconds": 20,
                "cutoff_monotonic": admission_seconds,
                "drain_deadline_monotonic": admission_seconds + 20,
            },
        ]
        for journal_row in journal_rows:
            journal_row.update(run_id=coin["run_id"], schema_version=2)
        journal_path.write_text("".join(json.dumps(r) + "\n" for r in journal_rows))
        score(tape, slots, lock_path, out)
        result = strict_json(out.read_bytes())
        assert result["scoring_complete"] is True
        assert len(result["rows"]) == 2 and all(
            r["modeled_net_lamports"] is None for r in result["rows"]
        )
        assert result["result"]["capture"]["status"] == "partial_censored"
        assert "terminal_missing" in result["result"]["capture"]["partial_reasons"]
        for changed, reason in (
            (
                {**check_lock, "run_id": "different-run"},
                "journal_run_identity_mismatch",
            ),
            (
                {
                    **check_lock,
                    "capture": {**check_lock["capture"], "admission_seconds": 31},
                },
                "journal_capture_settings_mismatch",
            ),
        ):
            try:
                bind_journal(changed, heads)
            except ValueError as exc:
                assert str(exc) == reason
            else:
                raise AssertionError("journal_binding_guard_missing")
        try:
            score(tape, slots, lock_path, out)
        except FileExistsError:
            pass
        else:
            raise AssertionError("exclusive_output_guard_missing")
        check_lock["source_sha256"][next(iter(check_lock["source_sha256"]))] = "0" * 64
        try:
            validate_lock(check_lock, tape, slots)
        except ValueError as exc:
            assert str(exc) == "exact_source_set_or_hash_mismatch"
        else:
            raise AssertionError("source_lock_guard_missing")
    absent = deepcopy(coin)
    absent["trade_signatures"][0:2] = ["other", "other"]
    assert (
        evaluate(absent, "C", heads, lock, snapshot, None)["reason"]
        == "creation_post_transaction_reserves_unavailable"
    )
    return {
        "self_check": "passed",
        "cases": [
            "same_transaction_post_buy",
            "canonical_quote_with_zero_legacy",
            "canonical_zero_without_legacy_fallback",
            "legacy_sol_only",
            "mismatched_or_missing_trade_quote_refused",
            "lookahead_rejection",
            "irrelevant_gap",
            "relevant_gap_pending_inventory",
            "unpriced_fees",
            "missing_creation_poststate",
            "causal_D2",
            "streaming_report",
            "exclusive_output",
            "exact_source_lock",
            "mixed_run_rejection",
            "admission_manifest_rejection",
            "partial_journal_retained",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("tape", "slots", "lock", "out"):
        parser.add_argument(f"--{option}", type=Path)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--lock-schema", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        require(
            not args.lock_schema
            and not any((args.tape, args.slots, args.lock, args.out)),
            "self_check_exclusive",
        )
        result = self_check()
    elif args.lock_schema:
        require(
            not any((args.tape, args.slots, args.lock, args.out)),
            "lock_schema_exclusive",
        )
        result = lock_template()
    else:
        require(
            all((args.tape, args.slots, args.lock, args.out)),
            "required: --tape --slots --lock --out",
        )
        result = score(args.tape, args.slots, args.lock, args.out)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
