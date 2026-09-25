"""Frozen two-Orca exact-output experiment: collect unsigned evidence or replay offline.

No discovery, signing, broadcast, balance overrides, retries, or income projection.
Run `scope --study-id NAME [--pilot]` before collection; freeze its stdout externally.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import signal
import struct
import sys
import time
import unicodedata
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import aiohttp
import simulate_atomic_cycles as atomic
import simulate_orca_cycle as orca

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Protocol widths and preregistered numerical bounds stay visible at each check.
# ruff: noqa: PLR2004

POOLS = [
    "8erNF5u3CHrqZJXtkfY8CjSxFYF1yqHmN8uDbAhk6tWM",
    "3gYLU5tdbvXw4yFW6HjaXHNwPX2VDKNRXJtcgAz1qFsJ",
]
MINT = "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3"
PAYER = "9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe"
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
SCHEMA = 2
QUANTITY = 17_285_678
NS = 1_000_000_000
BODY_LIMIT = 8 * 1024 * 1024
TAPE_LIMIT = 256 * 1024 * 1024
TAIL_RESERVE = 2 * 1024 * 1024
SOURCES = [Path(__file__).name, "simulate_atomic_cycles.py", "simulate_orca_cycle.py"]
QUALIFICATIONS = [
    "Exact-output buy of 17285678 raw intermediate tokens, not fixed exact input.",
    "Policy differs from Menu's historical lower-fee route; no actor replication claim.",
    "Native deployed-program simulation only; no constant-product approximation.",
    "Equal returned finalized slots are not cryptographic bank pinning or inclusion.",
    "Opportunities and episodes are upper-bound sampled states, not independent fills or income.",
    "Failed coverage is unknown, never a zero-profit or zero-loss observation.",
    "Only both guard-negative eligible directions close an episode; gaps do not reset it.",
    "A successful simulation is not authorization, executable availability, or guaranteed profit.",
    "Complete means the scheduled collection and wallet audit ended, not complete eligible market coverage.",
    "Hash chaining detects content edits, not fabricated provider evidence; externally archive the terminal hash.",
]


class StudyError(Exception):
    """Credential-free local terminal code."""


class CoverageGapError(Exception):
    """Unsupported ordinary pool/dependency state, not an economic observation."""


def require(ok: bool, code: str) -> None:  # noqa: FBT001 - assertion predicate
    if not ok:
        raise StudyError(code)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw: bytes) -> object:
    def pairs(items: list) -> dict:
        out = {}
        for key, value in items:
            require(key not in out, "json_duplicate_key")
            out[key] = value
        return out

    def constant(_: str) -> None:
        raise StudyError("json_nonfinite_number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def source_hashes() -> dict:
    return {
        name: digest(Path(__file__).with_name(name).read_bytes()) for name in SOURCES
    }


def make_scope(study_id: str, *, pilot: bool) -> dict:
    """Freeze the numerical policy and current source identities without RPC."""
    require(
        bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", study_id)), "study_id"
    )
    return {
        "schema": SCHEMA,
        "study_id": study_id,
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "source_sha256": source_hashes(),
        "mode": "pilot" if pilot else "full",
        "pool_addresses": POOLS,
        "mint": MINT,
        "mint_decimals": 6,
        "payer": PAYER,
        "program": orca.PROGRAM,
        "config": orca.CONFIG,
        "genesis": GENESIS,
        "bank_protocol": "metadata_then_one_state_and_two_simulations_batch",
        "quantity_policy": "fixed_exact_output_intermediate_raw",
        "quantity_raw": QUANTITY,
        "input_cap_lamports": 10_000_000,
        "network_fee_lamports": 65_000,
        "tip_lamports": 10_000,
        "profit_floor_lamports": 1_000,
        "windows": 4 if pilot else 240,
        "split_at_window": 2 if pilot else 120,
        "interval_ns": 15 * NS,
        "startup_ns": 60 * NS,
        "window_ns": 12 * NS,
        "audit_ns": 12 * NS,
        "max_http": 24 if pilot else 1000,
        "response_header_pacing_ns": NS // 2,
        "max_http_body_bytes": BODY_LIMIT,
        "max_tape_bytes": TAPE_LIMIT,
        "commitment": "finalized",
        "signed_transactions_allowed": 0,
        "submitted_transactions_allowed": 0,
        "retries": 0,
        "cloud": {"project": "chainstack-pumpfun", "region": "europe-west2"},
    }


def validate_scope(scope: dict) -> dict:
    """Refuse source or financial-policy drift before collection and replay."""
    require(isinstance(scope, dict), "scope_shape")
    expected = make_scope(scope.get("study_id", ""), pilot=scope.get("mode") == "pilot")
    expected["frozen_at_utc"] = scope.get("frozen_at_utc")
    require(canonical(scope) == canonical(expected), "scope_or_source_mismatch")
    require(isinstance(scope["frozen_at_utc"], str), "scope_timestamp")
    require(
        datetime.fromisoformat(scope["frozen_at_utc"]).utcoffset() is not None,
        "scope_timezone",
    )
    require(
        (
            atomic.BUY_LAMPORTS,
            atomic.NETWORK_FEE,
            atomic.TIP_LAMPORTS,
            atomic.PROFIT_LAMPORTS,
        )
        == (10_000_000, 65_000, 10_000, 1_000),
        "financial_policy_changed",
    )
    return scope


def wallet_keys() -> list[str]:
    payer = atomic.Pubkey.from_string(PAYER)
    return [
        PAYER,
        *[
            str(
                atomic.get_associated_token_address(
                    payer, atomic.Pubkey.from_string(mint)
                )
            )
            for mint in (atomic.SOL, MINT)
        ],
    ]


def wallet(bank: dict, baseline: dict | None = None) -> dict:
    keys = wallet_keys()
    account = bank[keys[0]]
    atomic.checked_data(account, "11111111111111111111111111111111", 0)
    require(
        type(account["lamports"]) is int
        and 10_075_000 < account["lamports"] <= 2**64 - 1 - 1_000,
        "payer_balance_range",
    )
    require(all(bank[key] is None for key in keys[1:]), "payer_ata_present")
    state = {key: account[key] for key in ("owner", "executable", "data", "lamports")}
    require(baseline is None or state == baseline, "actual_wallet_baseline_changed")
    return state


def account_params(keys: list[str], minimum: int | None = None) -> list:
    require(0 < len(keys) <= 100 and len(keys) == len(set(keys)), "account_batch_bound")
    options = {"encoding": "base64", "commitment": "finalized"}
    if minimum is not None:
        options["minContextSlot"] = minimum
    return [keys, options]


def bank_result(result: object, keys: list[str]) -> tuple[int, dict]:
    require(
        isinstance(result, dict) and isinstance(result.get("context"), dict),
        "account_result_shape",
    )
    slot, values = result["context"].get("slot"), result.get("value")
    require(
        type(slot) is int
        and slot >= 0
        and isinstance(values, list)
        and len(values) == len(keys),
        "account_result_shape",
    )
    require(
        all(value is None or isinstance(value, dict) for value in values),
        "account_result_shape",
    )
    bank = dict(zip(keys, values, strict=True))
    if atomic.CLOCK in bank:
        clock = atomic.checked_data(
            bank[atomic.CLOCK], "Sysvar1111111111111111111111111111111111111", 40
        )
        require(atomic.u64(clock, 0) == slot, "clock_snapshot_slot_mismatch")
    return slot, bank


def read_pools(result: object, baseline: dict) -> tuple[int, list]:
    keys = [atomic.CLOCK, *wallet_keys(), *POOLS]
    slot, bank = bank_result(result, keys)
    wallet(bank, baseline)
    try:
        pools = [
            orca.decode(address, bank[address], expected_mints=(atomic.SOL, MINT))
            for address in POOLS
        ]
        for pool in pools:
            pool.dependencies()
    except (ValueError, KeyError, IndexError, struct.error) as exc:
        raise CoverageGapError("pool_state_unsupported") from exc
    return slot, pools


def dependencies(pools: list) -> list[str]:
    keys = list(
        dict.fromkeys(
            [
                atomic.CLOCK,
                *wallet_keys(),
                *[key for pool in pools for key in pool.dependencies()],
            ]
        )
    )
    require(len(keys) <= 100, "dependency_batch_bound")
    return keys


def build_candidates(pools: list, baseline: dict) -> list:
    """Build unsigned capped packets; current dependencies still require attestation."""
    payer = atomic.Pubkey.from_string(PAYER)
    return [
        atomic.build_cycle(
            buy, sell, payer, QUANTITY, initial_balance=baseline["lamports"]
        )
        for buy, sell in (pools, pools[::-1])
    ]


def transactions(
    result: object, pools: list, baseline: dict, minimum: int
) -> tuple[int, list]:
    keys = dependencies(pools)
    slot, bank = bank_result(result, keys)
    require(slot >= minimum, "snapshot_below_pool_slot")
    wallet(bank, baseline)
    try:
        live = [orca.hydrate(pool, bank) for pool in pools]
        require(
            atomic.checked_data(bank[MINT], atomic.SPL, 82)[44] == 6, "mint_decimals"
        )
        require(
            atomic.checked_data(bank[atomic.SOL], atomic.SPL, 82)[44] == 9,
            "sol_decimals",
        )
    except (ValueError, KeyError, IndexError, struct.error) as exc:
        raise CoverageGapError("pool_dependency_unsupported_or_drift") from exc
    return slot, build_candidates(live, baseline)


def simulation_calls(txs: list, slot: int, window: int, keys: list[str]) -> list:
    """Batch current state with both native simulations without a pacing slot gap."""
    calls = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "getMultipleAccounts",
            "params": account_params(keys, slot),
        }
    ]
    for direction in [0, 1] if window % 2 == 0 else [1, 0]:
        params = atomic.simulation_params(txs[direction], slot)
        params[1]["commitment"] = "finalized"
        calls.append(
            {
                "jsonrpc": "2.0",
                "id": direction,
                "method": "simulateTransaction",
                "params": params,
            }
        )
    return calls


def transaction_hashes(request: object) -> dict:
    if not isinstance(request, list):
        require(
            isinstance(request, dict)
            and request.get("method") in {"getGenesisHash", "getMultipleAccounts"},
            "read_only_method",
        )
        return {}
    require(
        len(request) == 3 and {call["id"] for call in request} == {0, 1, 2},
        "paired_request_ids",
    )
    hashes = {}
    for call in request:
        require(type(call["id"]) is int, "paired_request_ids")
        if call["id"] == 2:
            require(call["method"] == "getMultipleAccounts", "paired_state_read_only")
            keys, options = call["params"]
            require(
                type(options.get("minContextSlot")) is int
                and options["minContextSlot"] >= 0
                and canonical(call["params"])
                == canonical(account_params(keys, options["minContextSlot"])),
                "paired_state_options",
            )
            continue
        require(
            type(call["id"]) is int and call["method"] == "simulateTransaction",
            "unsigned_pair_only",
        )
        options = call["params"][1]
        require(
            type(options.get("minContextSlot")) is int
            and options["minContextSlot"] >= 0,
            "simulation_slot_floor",
        )
        wire = base64.b64decode(call["params"][0], validate=True)
        tx = atomic.Transaction.from_bytes(wire)
        expected = atomic.simulation_params(tx, options["minContextSlot"])
        expected[1]["commitment"] = "finalized"
        require(canonical(expected) == canonical(call["params"]), "simulation_options")
        hashes[str(call["id"])] = digest(wire)
    return hashes


def correlate(request: object, payload: object) -> dict:
    batch = isinstance(request, list)
    calls = request if batch else [request]
    items = payload if batch else [payload]
    require(isinstance(items, list) and len(items) == len(calls), "rpc_response_count")
    expected = {call["id"] for call in calls}
    require(len(expected) == len(calls), "rpc_request_ids")
    indexed = {}
    for item in items:
        require(
            isinstance(item, dict)
            and item.get("jsonrpc") == "2.0"
            and type(item.get("id")) is int
            and item["id"] in expected
            and item["id"] not in indexed,
            "rpc_response_correlation",
        )
        require(("result" in item) != ("error" in item), "rpc_response_shape")
        indexed[item["id"]] = item
    require(set(indexed) == expected, "rpc_response_correlation")
    return indexed


def _pool_key_request(request: object) -> list[dict]:
    require(
        isinstance(request, dict)
        and set(request) == {"method", "endpoint", "pools"}
        and request["method"] == "GET"
        and request["endpoint"] == "raydium_pool_keys"
        and isinstance(request["pools"], list)
        and 1 <= len(request["pools"]) <= 4,
        "pool_key_request_shape",
    )
    addresses = set()
    for pool in request["pools"]:
        require(
            isinstance(pool, dict) and set(pool) == {"address", "program"},
            "pool_key_request_pool",
        )
        for value in pool.values():
            require(isinstance(value, str), "pool_key_request_pubkey")
            try:
                key = atomic.Pubkey.from_string(value)
            except ValueError:
                raise StudyError("pool_key_request_pubkey") from None
            require(str(key) == value, "pool_key_request_pubkey")
        require(pool["address"] not in addresses, "pool_key_request_duplicate")
        addresses.add(pool["address"])
    return request["pools"]


def _pool_key_policy(provider: object, policy: object) -> None:
    require(
        provider == "anonymous_public_mainnet_beta"
        and isinstance(policy, dict)
        and policy.get("endpoint") == "raydium_pool_keys"
        and type(policy.get("max_requests_per_route")) is int
        and policy["max_requests_per_route"] == 1
        and type(policy.get("max_pools_per_request")) is int
        and policy["max_pools_per_request"] == 4
        and type(policy.get("request_timeout_ns")) is int
        and policy["request_timeout_ns"] == 10 * NS,
        "pool_key_policy",
    )


def rpc_results(request: object, payload: object) -> dict:
    """Validate native replies or normalize only requested public pool-table hints."""
    if isinstance(request, dict) and request.get("method") == "GET":
        pools = _pool_key_request(request)
        require(
            isinstance(payload, dict)
            and payload.get("success") is True
            and isinstance(payload.get("data"), list)
            and len(payload["data"]) == len(pools),
            "pool_key_response_shape",
        )
        requested = {pool["address"]: pool["program"] for pool in pools}
        tables = {}
        for row in payload["data"]:
            if row is None:
                continue
            require(
                isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and row["id"] in requested
                and row["id"] not in tables
                and row.get("programId") == requested[row["id"]],
                "pool_key_response_identity",
            )
            table = row.get("lookupTableAccount")
            if table is not None and table != "":
                require(isinstance(table, str), "pool_key_response_lookup")
                try:
                    key = atomic.Pubkey.from_string(table)
                except ValueError:
                    raise StudyError("pool_key_response_lookup") from None
                require(str(key) == table, "pool_key_response_lookup")
                if key == atomic.Pubkey.default():
                    table = None
            else:
                table = None
            tables[row["id"]] = table
        return {
            0: [
                {
                    "pool": pool["address"],
                    "program": pool["program"],
                    "lookup_table": tables.get(pool["address"]),
                }
                for pool in pools
            ]
        }
    indexed = correlate(request, payload)
    errors = {
        index: item["error"] for index, item in indexed.items() if "error" in item
    }
    for error in errors.values():
        require(
            isinstance(error, dict) and type(error.get("code")) is int,
            "rpc_error_shape",
        )
    if errors:
        calls = request if isinstance(request, list) else [request]
        bounded_reads = all(
            call["method"] in {"getMultipleAccounts", "simulateTransaction"}
            and len(call.get("params", [])) == 2
            and isinstance(call["params"][1], dict)
            and type(call["params"][1].get("minContextSlot")) is int
            and call["params"][1]["minContextSlot"] > 0
            for call in calls
        )
        raise StudyError(
            "rpc_min_context_slot_not_reached"
            if bounded_reads
            and all(error["code"] == -32016 for error in errors.values())
            else "rpc_api_error"
        )
    return {index: item["result"] for index, item in indexed.items()}


def native_pair(  # noqa: PLR0913 - the same-bank evidence and its frozen candidates
    txs: list, result: dict, minimum: int, baseline: dict, *, pools: list, on_time: bool
) -> dict:
    """Attest the batched state before accepting either native wallet outcome."""
    require(set(result) == {0, 1, 2}, "native_pair_missing_state_or_direction")
    slot, rebuilt = transactions(result[2], pools, baseline, minimum)
    if any(
        bytes(before) != bytes(after)
        for before, after in zip(txs, rebuilt, strict=True)
    ):
        raise CoverageGapError("pool_dependency_unsupported_or_drift")
    components = []
    for direction, tx in enumerate(txs):
        native = result[direction]
        require(
            isinstance(native, dict)
            and isinstance(native.get("context"), dict)
            and type(native["context"].get("slot")) is int
            and isinstance(native.get("value"), dict)
            and "err" in native["value"],
            "native_result_shape",
        )
        value = native["value"]
        require(
            type(value.get("fee")) is int
            and (value["err"] is None or isinstance(value["err"], str | dict)),
            "native_fee_or_error_type",
        )
        require(
            value.get("unitsConsumed") is None
            or (type(value["unitsConsumed"]) is int and value["unitsConsumed"] >= 0),
            "native_units_type",
        )
        detail = (
            value["err"].get("InstructionError")
            if isinstance(value["err"], dict)
            else None
        )
        if detail is not None:
            require(
                isinstance(detail, list)
                and len(detail) == 2
                and type(detail[0]) is int
                and 0 <= detail[0] < len(tx.message.instructions)
                and isinstance(detail[1], str | dict),
                "native_instruction_error_type",
            )
            if isinstance(detail[1], dict) and "Custom" in detail[1]:
                require(
                    type(detail[1]["Custom"]) is int
                    and 0 <= detail[1]["Custom"] < 2**32,
                    "native_custom_error_type",
                )
        row = atomic.simulation_result(
            tx,
            native,
            minimum,
            atomic.Pubkey.from_string(PAYER),
            initial_balance=baseline["lamports"],
        )
        status = (
            "success"
            if row["err"] is None
            else "guard_rejected"
            if row["guard_rejected"]
            else "other_failure"
        )
        detail = (
            row["err"].get("InstructionError") if isinstance(row["err"], dict) else None
        )
        error = None if row["err"] is None else {"kind": "native_error"}
        if isinstance(detail, list) and len(detail) == 2 and type(detail[0]) is int:
            error["instruction"] = detail[0]
            if isinstance(detail[1], dict) and type(detail[1].get("Custom")) is int:
                error["custom"] = detail[1]["Custom"]
        components.append(
            {
                "direction": "A_to_B" if direction == 0 else "B_to_A",
                "status": status,
                "simulation_slot": row["simulation_slot"],
                "net_lamports": row["net_lamports"],
                "fee_lamports": row["fee"],
                "units": row["units"],
                "error": error,
                "transaction_sha256": digest(bytes(tx)),
            }
        )
    equal_slots = all(row["simulation_slot"] == slot for row in components)
    eligible = on_time and equal_slots
    statuses = [row["status"] for row in components]
    # A non-guard failure is not a measured negative, even at an equal finalized slot.
    economic_complete = eligible and all(
        status in {"success", "guard_rejected"} for status in statuses
    )
    return {
        "status": "paired" if eligible else "pair_ineligible",
        "snapshot_slot": slot,
        "on_time": on_time,
        "equal_finalized_slots": equal_slots,
        "primary_pair_eligible": eligible,
        "economic_complete": economic_complete,
        "opportunity": economic_complete and "success" in statuses,
        "guard_negative": economic_complete
        and statuses == ["guard_rejected", "guard_rejected"],
        "components": components,
    }


def transition(active: bool, row: dict) -> tuple[bool, int]:  # noqa: FBT001 - episode state
    if row.get("opportunity"):
        return True, int(not active)
    if row.get("guard_negative"):
        return False, 0
    return active, 0


def safe_code(exc: BaseException) -> str:
    if isinstance(exc, StudyError | CoverageGapError):
        return str(exc)
    if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, TimeoutError):
        return "deadline_or_transport_timeout"
    return "local_" + type(exc).__name__


class Tape:
    def __init__(self, path: Path, scope: dict) -> None:
        self.file = path.open("xb")
        self.scope_hash, self.size, self.seq, self.previous = (
            digest(canonical(scope)),
            0,
            0,
            "0" * 64,
        )

    def emit(self, event: str, *, reserved: bool = False, **fields: object) -> dict:
        row = {
            "schema": SCHEMA,
            "seq": self.seq,
            "event": event,
            "scope_sha256": self.scope_hash,
            "previous_sha256": self.previous,
            **fields,
        }
        row["sha256"] = digest(canonical(row))
        line = canonical(row) + b"\n"
        require(
            self.size + len(line) <= TAPE_LIMIT - (0 if reserved else TAIL_RESERVE),
            "tape_byte_limit",
        )
        self.file.write(line)
        self.file.flush()
        self.size += len(line)
        self.seq += 1
        self.previous = row["sha256"]
        return row


def load_provider(path: Path) -> tuple[str, tuple[str, ...]]:
    path = path.resolve()
    require(path.suffix == ".json", "explicit_provider_json_required")
    value = strict_json(path.read_bytes())
    require(
        isinstance(value, dict)
        and "rpc_url" in value
        and set(value) <= {"rpc_url", "geyser_endpoint", "geyser_token"},
        "provider_fields",
    )
    require(
        all(
            isinstance(text, str)
            and text
            and not any(unicodedata.category(c).startswith("C") for c in text)
            for text in value.values()
        ),
        "provider_string",
    )
    url = value["rpc_url"]
    parsed = urlsplit(url)
    require(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and not any(
            c.isspace() or unicodedata.category(c).startswith("C") for c in unquote(url)
        ),
        "provider_https_url",
    )
    require(parsed.port is None or 0 < parsed.port < 65536, "provider_port")
    # Include credential-bearing URL components so an echoed token alone is caught.
    secrets = tuple(
        {
            *value.values(),
            parsed.hostname,
            *[part for part in unquote(parsed.path).split("/") if len(part) >= 8],
            *[
                part.split("=", 1)[-1]
                for part in unquote(parsed.query).split("&")
                if part
            ],
        }
    )
    return url, secrets


def contains_secret(
    value: object, secrets: tuple[str, ...], *, public_data: bool = False
) -> bool:
    """Allow public result URLs, never configured secrets or provider-error URLs."""
    if isinstance(value, str):
        return (not public_data and bool(re.search(r"https?://", value, re.I))) or any(
            secret in value for secret in secrets
        )
    if isinstance(value, dict):
        return any(
            contains_secret(key, secrets, public_data=public_data)
            or contains_secret(item, secrets, public_data=public_data)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(
        contains_secret(item, secrets, public_data=public_data) for item in value
    )


async def _pool_key_exchange(
    request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    # aiohttp retries disconnected GETs internally; preserve the failure instead.
    try:
        return await handler(request)
    except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError) as exc:
        raise StudyError(safe_code(exc)) from exc


class RPC:
    """Serial, response-header-anchored pacing; one reserved actual-wallet audit."""

    def __init__(  # noqa: PLR0913 - frozen wire policy shared by two probes
        self,
        session: aiohttp.ClientSession,
        url: str,
        secrets: tuple[str, ...],
        tape: Tape,
        scope: dict,
        *,
        validate_request: Callable[[object], dict] = transaction_hashes,
    ) -> None:
        self.session, self.url, self.secrets, self.tape, self.scope = (
            session,
            url,
            secrets,
            tape,
            scope,
        )
        self.count, self.anchor = 0, 0
        self.records = []
        self.validate_request = validate_request

    async def call(  # noqa: C901, PLR0912, PLR0915 - one bounded evidence exchange
        self, role: str, request: object, deadline: int, window: int | None = None
    ) -> dict:
        """Record one bounded native RPC or policy-approved anonymous metadata read."""
        audit = role == "audit"
        entered = time.monotonic_ns()
        start = headers = end = None
        body = b""
        status = None
        failure = None
        interruption = None
        payload = None
        complete_body = False
        received = 0
        attempted = False
        public_metadata = isinstance(request, dict) and request.get("method") == "GET"
        pools = _pool_key_request(request) if public_metadata else []
        hashes = self.validate_request(request)
        try:
            if public_metadata:
                _pool_key_policy(
                    self.scope.get("provider"), self.scope.get("pool_lookup_policy")
                )
                require(
                    role == "pool_lookup" and window is None and hashes == {},
                    "pool_key_read_only",
                )
                require(
                    not self.secrets
                    and self.session.auth is None
                    and not self.session.trust_env
                    and not self.session.cookie_jar
                    and not any(
                        name.lower()
                        in {"authorization", "proxy-authorization", "cookie"}
                        for name in self.session.headers
                    ),
                    "pool_key_anonymous_session",
                )
                deadline = min(deadline, entered + 10 * NS)
            require(
                self.count < self.scope["max_http"] - (0 if audit else 1),
                "http_request_limit",
            )
            require(time.monotonic_ns() < deadline, "request_deadline_expired")
            async with asyncio.timeout(max(0, (deadline - time.monotonic_ns()) / NS)):
                await asyncio.sleep(
                    max(
                        0,
                        (
                            self.anchor
                            + self.scope["response_header_pacing_ns"]
                            - time.monotonic_ns()
                        )
                        / NS,
                    )
                )
                require(time.monotonic_ns() < deadline, "request_deadline_expired")
                start = time.monotonic_ns()
                self.count += 1
                attempted = True
                exchange = (
                    self.session.get(
                        atomic.API + "/pools/key/ids",
                        params={"ids": ",".join(pool["address"] for pool in pools)},
                        headers={"Accept-Encoding": "identity"},
                        allow_redirects=False,
                        middlewares=(_pool_key_exchange,),
                    )
                    if public_metadata
                    else self.session.post(
                        self.url,
                        data=canonical(request),
                        headers={
                            "Content-Type": "application/json",
                            "Accept-Encoding": "identity",
                        },
                        allow_redirects=False,
                    )
                )
                async with exchange as response:
                    headers = self.anchor = time.monotonic_ns()
                    status = response.status
                    require(
                        response.headers.get("Content-Encoding", "identity").lower()
                        == "identity",
                        "http_content_encoding",
                    )
                    chunks = []
                    async for chunk in response.content.iter_chunked(65536):
                        received += len(chunk)
                        require(received <= BODY_LIMIT, "http_body_limit")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    complete_body = True
                    require(status == 200, "http_status_failure")
                    payload = strict_json(body)
                    require(
                        public_metadata
                        or all(
                            not contains_secret(
                                reply,
                                self.secrets,
                                public_data=isinstance(reply, dict)
                                and "result" in reply
                                and "error" not in reply,
                            )
                            for reply in (
                                payload if isinstance(payload, list) else [payload]
                            )
                        ),
                        "response_credential_redacted",
                    )
                    rpc_results(request, payload)
                    end = time.monotonic_ns()
                    require(end <= deadline, "response_deadline_expired")
        except BaseException as exc:  # noqa: BLE001 - persist terminal failure, never retry
            failure = safe_code(exc)
            if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt):
                interruption = exc
        end = end or time.monotonic_ns()
        # No-header failures and the final audit use the same recorded anchor.
        if headers is None:
            self.anchor = end
        raw = {
            "complete": complete_body,
            "received_bytes": received,
            "size": len(body) if complete_body else None,
            "sha256": digest(body) if complete_body else None,
            "encoding": None,
            "data": None,
        }
        # Errors may echo private URLs in arbitrary text. Never retain their raw body.
        if body and failure is None:
            raw.update(
                encoding="base64+zlib",
                data=base64.b64encode(zlib.compress(body)).decode(),
            )
        rpc_error_codes = {}
        if not public_metadata and failure is not None and payload is not None:
            try:
                replies = correlate(request, payload)
            except StudyError:
                replies = {}
            for identifier, reply in replies.items():
                error = reply.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if type(code) is int and -(2**31) <= code < 2**31:
                    # Retain only identity-correlated protocol integers, never
                    # provider messages/data that may echo credentials.
                    rpc_error_codes[str(identifier)] = code
        fields = {
            "role": role,
            "window": window,
            "request": request,
            "unsigned_transaction_sha256": hashes,
            "attempted": attempted,
            "entered_ns": entered,
            "start_ns": start,
            "headers_ns": headers,
            "end_ns": end,
            "deadline_ns": deadline,
            "http_status": status,
            "failure": failure,
            "rpc_error_codes": rpc_error_codes,
            "raw": raw,
        }
        try:
            try:
                record = self.tape.emit("rpc", reserved=audit, **fields)
            except StudyError as exc:
                require(str(exc) == "tape_byte_limit", "tape_write_failure")
                failure = fields["failure"] = "tape_byte_limit"
                raw.update(encoding=None, data=None)
                record = self.tape.emit("rpc", reserved=True, **fields)
            self.records.append(record)
        finally:
            if interruption is not None:
                raise interruption
        if failure is not None:
            raise StudyError(failure)
        return rpc_results(request, payload)


def single(method: str, params: list) -> dict:
    return {"jsonrpc": "2.0", "id": 0, "method": method, "params": params}


def missing(reason: str) -> dict:
    return {
        "status": "missing",
        "reason": reason,
        "primary_pair_eligible": False,
        "economic_complete": False,
        "opportunity": False,
        "guard_negative": False,
        "components": [],
    }


async def collect(scope: dict, provider: Path, path: Path) -> bool:  # noqa: C901, PLR0912, PLR0915 - one auditable chronological state machine
    """Collect the complete fixed schedule or an explicitly incomplete terminal."""
    url, secrets = load_provider(provider)
    tape = Tape(path, scope)
    started = time.monotonic_ns()
    origin = started + scope["startup_ns"]
    tape.emit(
        "manifest",
        scope=scope,
        start_ns=started,
        schedule_origin_ns=origin,
        started_at_utc=datetime.now(UTC).isoformat(),
    )
    baseline = None
    rows = []
    failure = None
    audit = {"unchanged": False, "reason": "not_attempted"}
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
            auto_decompress=False,
            trust_env=False,
        ) as session:
            rpc = RPC(session, url, secrets, tape, scope)
            try:
                identity = await rpc.call(
                    "genesis", single("getGenesisHash", []), origin
                )
                require(identity[0] == GENESIS, "mainnet_identity")
                result = await rpc.call(
                    "initial",
                    single(
                        "getMultipleAccounts",
                        account_params([atomic.CLOCK, *wallet_keys()]),
                    ),
                    origin,
                )
                _, initial = bank_result(result[0], [atomic.CLOCK, *wallet_keys()])
                baseline = wallet(initial)
                for index in range(scope["windows"]):
                    scheduled = origin + index * scope["interval_ns"]
                    deadline = scheduled + scope["window_ns"]
                    await asyncio.sleep(max(0, (scheduled - time.monotonic_ns()) / NS))
                    began = time.monotonic_ns()
                    record_start = len(rpc.records)
                    row = missing("missed_window_no_catchup")
                    if began < deadline:
                        try:
                            keys = [atomic.CLOCK, *wallet_keys(), *POOLS]
                            result = await rpc.call(
                                "pools",
                                single("getMultipleAccounts", account_params(keys)),
                                deadline,
                                index,
                            )
                            minimum, pools = read_pools(result[0], baseline)
                            txs = build_candidates(pools, baseline)
                            result = await rpc.call(
                                "paired_snapshot",
                                simulation_calls(
                                    txs, minimum, index, dependencies(pools)
                                ),
                                deadline,
                                index,
                            )
                            row = native_pair(
                                txs,
                                result,
                                minimum,
                                baseline,
                                pools=pools,
                                on_time=rpc.records[-1]["end_ns"] <= deadline,
                            )
                        except CoverageGapError as exc:
                            row = missing(safe_code(exc))
                        except BaseException as exc:  # noqa: BLE001 - persist failure and stop admission
                            failure = safe_code(exc)
                            row = missing(failure)
                    finished = time.monotonic_ns()
                    if row["components"] and finished > deadline:
                        row["status"] = "pair_ineligible"
                        for field in (
                            "on_time",
                            "primary_pair_eligible",
                            "economic_complete",
                            "opportunity",
                            "guard_negative",
                        ):
                            row[field] = False
                    rows.append(row)
                    tape.emit(
                        "window",
                        reserved=True,
                        window=index,
                        scheduled_ns=scheduled,
                        deadline_ns=deadline,
                        began_ns=began,
                        end_ns=finished,
                        rpc_sequences=[
                            record["seq"] for record in rpc.records[record_start:]
                        ],
                        result=row,
                    )
                    if failure is not None:
                        break
                if failure is None:
                    await asyncio.sleep(
                        max(
                            0,
                            (
                                origin
                                + scope["windows"] * scope["interval_ns"]
                                - time.monotonic_ns()
                            )
                            / NS,
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - audit even interrupted startup
                failure = safe_code(exc)
            # An audit is not a retry: one separate reserved read, even after terminal failure.
            try:
                result = await rpc.call(
                    "audit",
                    single(
                        "getMultipleAccounts",
                        account_params([atomic.CLOCK, *wallet_keys()]),
                    ),
                    time.monotonic_ns() + scope["audit_ns"],
                )
                _, final = bank_result(result[0], [atomic.CLOCK, *wallet_keys()])
                require(baseline is not None, "baseline_unavailable")
                final_wallet = wallet(final, baseline)
                audit = {
                    "unchanged": True,
                    "initial_lamports": baseline["lamports"],
                    "final_lamports": final_wallet["lamports"],
                    "temporary_accounts_absent": True,
                }
            except BaseException as exc:  # noqa: BLE001 - a failed audit cannot become success
                audit = {"unchanged": False, "reason": safe_code(exc)}
                failure = failure or "final_wallet_audit_failed"
            complete = (
                failure is None and len(rows) == scope["windows"] and audit["unchanged"]
            )
            tape.emit(
                "terminal",
                reserved=True,
                status="complete" if complete else "incomplete",
                reason=failure,
                ended_ns=time.monotonic_ns(),
                http_attempts=rpc.count,
                scheduled_windows=scope["windows"],
                recorded_windows=len(rows),
                missing_windows=sum(row["status"] == "missing" for row in rows)
                + scope["windows"]
                - len(rows),
                actual_wallet_audit=audit,
            )
            return complete
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        tape.file.close()


def decode_wire(record: dict, *, scope: dict | None = None) -> object | None:
    """Decode correlated native results; external pool hints need explicit scope."""
    raw = record["raw"]
    require(
        type(raw["complete"]) is bool
        and type(raw["received_bytes"]) is int
        and 0 <= raw["received_bytes"] <= BODY_LIMIT + 65536,
        "wire_receipt_shape",
    )
    if raw["complete"]:
        require(
            type(raw["size"]) is int
            and raw["size"] == raw["received_bytes"] <= BODY_LIMIT,
            "wire_size",
        )
        require(
            isinstance(raw["sha256"], str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", raw["sha256"])),
            "wire_hash_shape",
        )
    else:
        require(
            record["failure"] is not None
            and raw["size"] is None
            and raw["sha256"] is None,
            "incomplete_wire_claim",
        )
    if record["failure"] is not None:
        require(
            raw["encoding"] is None and raw["data"] is None, "error_body_not_redacted"
        )
        return None
    require(
        raw["encoding"] == "base64+zlib" and isinstance(raw["data"], str),
        "wire_encoding",
    )
    compressed = base64.b64decode(raw["data"], validate=True)
    require(len(compressed) <= BODY_LIMIT + 65536, "compressed_body_limit")
    decoder = zlib.decompressobj()
    body = decoder.decompress(compressed, BODY_LIMIT + 1)
    require(
        len(body) <= BODY_LIMIT
        and decoder.eof
        and not decoder.unused_data
        and not decoder.unconsumed_tail,
        "wire_decompression",
    )
    require(
        len(body) == raw["size"] and digest(body) == raw["sha256"], "wire_hash_mismatch"
    )
    payload = strict_json(body)
    request = record.get("request")
    if isinstance(request, dict) and request.get("method") == "GET":
        require(isinstance(scope, dict), "pool_key_policy")
        _pool_key_policy(scope.get("provider"), scope.get("pool_lookup_policy"))
        require(
            record.get("role") == "pool_lookup" and record.get("window") is None,
            "pool_key_read_only",
        )
    rpc_results(request, payload)
    return payload


def read_chain(path: Path, scope: dict) -> Iterator[dict]:
    """Verify every row and require a bounded, intact terminal record."""
    previous, count, size, ended = "0" * 64, 0, 0, False
    scope_hash = digest(canonical(scope))
    with path.open("rb") as source:
        while True:
            line = source.readline(2 * BODY_LIMIT + 65536)
            if not line:
                break
            size += len(line)
            require(
                size <= TAPE_LIMIT and line.endswith(b"\n"),
                "tape_truncated_or_oversize",
            )
            row = strict_json(line)
            require(isinstance(row, dict) and not ended, "tape_after_terminal")
            checksum = row.pop("sha256", None)
            require(
                row.get("schema") == SCHEMA
                and type(row.get("seq")) is int
                and row["seq"] == count
                and row.get("previous_sha256") == previous
                and row.get("scope_sha256") == scope_hash
                and checksum == digest(canonical(row)),
                "tape_chain_mismatch",
            )
            row["sha256"] = checksum
            previous, count = checksum, count + 1
            ended = row.get("event") == "terminal"
            yield row
    require(ended, "tape_terminal_missing")


def replay_window(records: list, index: int, baseline: dict, completed_ns: int) -> dict:
    """Reconstruct dependency bank and bytes; emitted summaries are never authority."""
    require(1 <= len(records) <= 2, "window_rpc_count")
    roles = ["pools", "paired_snapshot"]
    for i, record in enumerate(records):
        require(
            record["role"] == roles[i] and record["window"] == index, "window_rpc_order"
        )
    keys = [atomic.CLOCK, *wallet_keys(), *POOLS]
    expected = single("getMultipleAccounts", account_params(keys))
    pools = txs = None
    minimum = None
    for i, record in enumerate(records):
        require(
            canonical(record["request"]) == canonical(expected),
            "rpc_request_policy_mismatch",
        )
        payload = decode_wire(record)
        if payload is None:
            require(i == len(records) - 1, "rpc_after_terminal_failure")
            return missing(record["failure"])
        result = rpc_results(expected, payload)
        try:
            if i == 0:
                minimum, pools = read_pools(result[0], baseline)
                txs = build_candidates(pools, baseline)
                expected = simulation_calls(txs, minimum, index, dependencies(pools))
            else:
                # The exact request comparison above includes both rebuilt wire packets.
                return native_pair(
                    txs,
                    result,
                    minimum,
                    baseline,
                    pools=pools,
                    on_time=completed_ns <= record["deadline_ns"],
                )
        except CoverageGapError as exc:
            require(i == len(records) - 1, "rpc_after_pool_gap")
            return missing(safe_code(exc))
    raise StudyError("partial_window_without_failure")


def summarize(rows: list, scope: dict) -> dict:
    result = {}
    for name, start, end in [
        ("all", 0, scope["windows"]),
        ("first_chronological_half", 0, scope["split_at_window"]),
        ("second_chronological_half", scope["split_at_window"], scope["windows"]),
    ]:
        counts = Counter()
        active = False
        nets = {"A_to_B": [], "B_to_A": []}
        components = {"A_to_B": Counter(), "B_to_A": Counter()}
        # Carry an open episode across the split: no fabricated new holdout episode.
        for prior in rows[:start]:
            active, _ = transition(active, prior)
        carried = active
        for row in rows[start:end]:
            counts[row["status"]] += 1
            counts["primary_pair_eligible"] += int(row["primary_pair_eligible"])
            counts["economic_complete"] += int(row["economic_complete"])
            counts["opportunities"] += int(row["opportunity"])
            counts["guard_negative"] += int(row["guard_negative"])
            active, new = transition(active, row)
            counts["episode_starts"] += new
            for component in row["components"]:
                direction = component["direction"]
                components[direction][component["status"]] += 1
                if component["net_lamports"] is not None:
                    nets[direction].append(component["net_lamports"])
            if not row["components"]:
                for direction_counts in components.values():
                    direction_counts["missing"] += 1
        result[name] = {
            "scheduled_windows": end - start,
            "counts": dict(counts),
            "episode_carried_in": carried,
            "episode_open_at_end": active,
            "direction_counts": {key: dict(value) for key, value in components.items()},
            "successful_native_net_lamports_samples_not_income": nets,
        }
    return result


def analyze(path: Path, scope: dict) -> dict:  # noqa: C901, PLR0912, PLR0915 - explicit protocol replay
    """Rebuild paired outcomes from retained native bodies, not summaries."""
    validate_scope(scope)
    manifest = terminal = baseline = None
    genesis_seen = initial_seen = audit_seen = False
    audit = {"unchanged": False, "reason": "not_attempted"}
    records = []
    rows = []
    attempts, previous_end, anchor = 0, 0, 0
    terminal_failure = None
    for row in read_chain(path, scope):
        event = row["event"]
        if event == "manifest":
            require(
                manifest is None and row["seq"] == 0 and row["scope"] == scope,
                "manifest_order_or_scope",
            )
            require(
                type(row["start_ns"]) is int
                and row["schedule_origin_ns"] == row["start_ns"] + scope["startup_ns"],
                "manifest_schedule",
            )
            manifest = row
        elif event == "rpc":
            require(
                manifest is not None and not audit_seen, "rpc_manifest_or_audit_order"
            )
            role = row["role"]
            require(
                role in {"genesis", "initial", "pools", "paired_snapshot", "audit"},
                "rpc_role",
            )
            require(terminal_failure is None or role == "audit", "rpc_after_failure")
            require(
                type(row["attempted"]) is bool
                and all(
                    type(row[key]) is int
                    for key in ("entered_ns", "end_ns", "deadline_ns")
                )
                and manifest["start_ns"] <= row["entered_ns"] <= row["end_ns"],
                "rpc_timing_shape",
            )
            require(row["entered_ns"] >= previous_end, "rpc_overlap")
            require(
                row["unsigned_transaction_sha256"]
                == transaction_hashes(row["request"]),
                "transaction_hash_mismatch",
            )
            if row["attempted"]:
                attempts += 1
                require(
                    attempts <= scope["max_http"] - (0 if role == "audit" else 1),
                    "replay_http_limit",
                )
                require(
                    type(row["start_ns"]) is int
                    and row["start_ns"]
                    >= max(
                        row["entered_ns"],
                        previous_end,
                        anchor + scope["response_header_pacing_ns"],
                    )
                    and row["start_ns"] < row["deadline_ns"],
                    "rpc_pacing_or_start",
                )
                if row["headers_ns"] is not None:
                    require(
                        type(row["headers_ns"]) is int
                        and row["start_ns"] <= row["headers_ns"] <= row["end_ns"],
                        "rpc_header_timing",
                    )
                    anchor = row["headers_ns"]
                else:
                    anchor = row["end_ns"]
            else:
                require(
                    row["start_ns"] is None
                    and row["headers_ns"] is None
                    and row["failure"] is not None,
                    "unattempted_rpc_shape",
                )
            require(row["end_ns"] >= previous_end, "rpc_time_reversal")
            previous_end = row["end_ns"]
            if row["failure"] is None:
                require(
                    row["attempted"]
                    and row["http_status"] == 200
                    and row["headers_ns"] is not None
                    and row["end_ns"] <= row["deadline_ns"],
                    "rpc_success_timing",
                )
            else:
                require(
                    isinstance(row["failure"], str)
                    and bool(re.fullmatch(r"[a-zA-Z_]+", row["failure"])),
                    "unsafe_failure_code",
                )
                terminal_failure = terminal_failure or (
                    "final_wallet_audit_failed" if role == "audit" else row["failure"]
                )
            if role in {"pools", "paired_snapshot"}:
                require(
                    genesis_seen and initial_seen and baseline is not None,
                    "window_before_initial",
                )
                index = len(rows)
                scheduled = (
                    manifest["schedule_origin_ns"] + index * scope["interval_ns"]
                )
                require(
                    row["window"] == index
                    and row["deadline_ns"] == scheduled + scope["window_ns"]
                    and (not row["attempted"] or row["start_ns"] >= scheduled),
                    "rpc_window_schedule",
                )
                records.append(row)
                continue
            require(
                row["window"] is None and not records, "startup_audit_window_identity"
            )
            if role == "genesis":
                require(
                    not genesis_seen and not initial_seen and not rows, "genesis_order"
                )
                expected = single("getGenesisHash", [])
                genesis_seen = True
            else:
                expected = single(
                    "getMultipleAccounts",
                    account_params([atomic.CLOCK, *wallet_keys()]),
                )
                if role == "initial":
                    require(
                        genesis_seen and not initial_seen and not rows, "initial_order"
                    )
                    initial_seen = True
                else:
                    require(not audit_seen, "duplicate_audit")
                    audit_seen = True
                    require(
                        0 < row["deadline_ns"] - row["entered_ns"] <= scope["audit_ns"],
                        "audit_deadline_bound",
                    )
            require(
                canonical(row["request"]) == canonical(expected),
                "startup_audit_request_policy",
            )
            if role != "audit":
                require(
                    row["deadline_ns"] == manifest["schedule_origin_ns"],
                    "startup_deadline",
                )
            payload = decode_wire(row)
            if payload is None:
                if role == "audit":
                    audit = {"unchanged": False, "reason": row["failure"]}
                continue
            result = rpc_results(expected, payload)[0]
            try:
                if role == "genesis":
                    require(result == GENESIS, "mainnet_identity")
                else:
                    _, bank = bank_result(result, [atomic.CLOCK, *wallet_keys()])
                    if role == "initial":
                        baseline = wallet(bank)
                    else:
                        require(baseline is not None, "baseline_unavailable")
                        after = wallet(bank, baseline)
                        audit = {
                            "unchanged": True,
                            "initial_lamports": baseline["lamports"],
                            "final_lamports": after["lamports"],
                            "temporary_accounts_absent": True,
                        }
            except Exception as exc:  # noqa: BLE001 - replay the recorded terminal refusal
                reason = safe_code(exc)
                terminal_failure = terminal_failure or (
                    "final_wallet_audit_failed" if role == "audit" else reason
                )
                if role == "audit":
                    audit = {"unchanged": False, "reason": reason}
        elif event == "window":
            require(
                manifest is not None and baseline is not None and not audit_seen,
                "window_order",
            )
            index = len(rows)
            scheduled = manifest["schedule_origin_ns"] + index * scope["interval_ns"]
            require(
                index < scope["windows"]
                and row["window"] == index
                and row["scheduled_ns"] == scheduled
                and row["deadline_ns"] == scheduled + scope["window_ns"]
                and scheduled <= row["began_ns"] <= row["end_ns"],
                "window_schedule",
            )
            require(
                row["rpc_sequences"] == [record["seq"] for record in records],
                "window_rpc_links",
            )
            if not records:
                require(
                    row["began_ns"] >= row["deadline_ns"], "unexplained_missing_window"
                )
                replayed = missing("missed_window_no_catchup")
            else:
                try:
                    replayed = replay_window(records, index, baseline, row["end_ns"])
                except (
                    StudyError,
                    ValueError,
                    KeyError,
                    IndexError,
                    struct.error,
                ) as exc:
                    # A persisted successful wire violating its native contract is invalid,
                    # not a gap rescued by an emitted summary.
                    raise StudyError("native_replay_rejected") from exc
                require(
                    row["end_ns"] >= records[-1]["end_ns"], "window_end_before_response"
                )
            require(row["result"] == replayed, "window_summary_mismatch")
            rows.append(replayed)
            records = []
        elif event == "terminal":
            require(
                manifest is not None and not records and audit_seen, "terminal_order"
            )
            require(
                row["status"] in {"complete", "incomplete"}
                and row["scheduled_windows"] == scope["windows"]
                and row["recorded_windows"] == len(rows)
                and row["http_attempts"] == attempts,
                "terminal_counts",
            )
            require(
                row["missing_windows"]
                == sum(item["status"] == "missing" for item in rows)
                + scope["windows"]
                - len(rows),
                "terminal_missing_count",
            )
            require(
                row["actual_wallet_audit"] == audit and row["ended_ns"] >= previous_end,
                "terminal_wallet_or_time",
            )
            if row["status"] == "complete":
                require(
                    terminal_failure is None
                    and row["reason"] is None
                    and len(rows) == scope["windows"]
                    and audit["unchanged"]
                    and row["ended_ns"]
                    >= manifest["schedule_origin_ns"]
                    + scope["windows"] * scope["interval_ns"],
                    "false_complete_terminal",
                )
            else:
                require(
                    isinstance(row["reason"], str)
                    and bool(re.fullmatch(r"[a-zA-Z_]+", row["reason"])),
                    "incomplete_terminal_reason",
                )
                require(
                    row["reason"] == terminal_failure
                    or (
                        row["reason"] == "interrupted"
                        and terminal_failure in {None, "final_wallet_audit_failed"}
                    ),
                    "terminal_reason_not_replayed",
                )
            terminal = row
        else:
            raise StudyError("unknown_tape_event")
    require(terminal is not None, "terminal_missing")
    rows.extend(
        missing("terminal_unobserved") for _ in range(scope["windows"] - len(rows))
    )
    return {
        "schema": SCHEMA,
        "study_id": scope["study_id"],
        "scope_sha256": digest(canonical(scope)),
        "tape_terminal_sha256": terminal["sha256"],
        "status": terminal["status"],
        "terminal_reason": terminal["reason"],
        "actual_wallet_audit": audit,
        "scheduled_windows": scope["windows"],
        "windows": rows,
        "summary": summarize(rows, scope),
        "qualifications": QUALIFICATIONS,
    }


def self_check() -> None:
    """No provider or signing: defend paired correlation and guard-vs-gap episodes."""
    from unittest.mock import patch  # noqa: PLC0415 - offline context-boundary fixture

    request = [
        {
            "jsonrpc": "2.0",
            "id": i,
            "method": "getMultipleAccounts" if i == 2 else "simulateTransaction",
            "params": [],
        }
        for i in (0, 1, 2)
    ]
    payload = [{"jsonrpc": "2.0", "id": i, "result": {"value": i}} for i in (2, 1, 0)]
    require(set(correlate(request, payload)) == {0, 1, 2}, "self_check_valid_pair")
    for bad in (
        payload[1:],
        [payload[0], payload[0], payload[2]],
        [*payload[:2], {**payload[2], "id": 3}],
    ):
        try:
            correlate(request, bad)
        except StudyError:
            continue
        raise StudyError("self_check_bad_pair_accepted")
    # A batched simulation may precede the state read while satisfying its RPC floor.
    baseline = {"lamports": 1_000_000_000}
    vaults = [str(atomic.Pubkey.from_bytes(bytes([i]) * 32)) for i in range(1, 5)]
    pools = [
        orca.Whirlpool(
            address,
            orca.PROGRAM,
            (atomic.SOL, MINT),
            tuple(vaults[2 * i : 2 * i + 2]),
            orca.CONFIG,
            16,
            16,
            0,
        )
        for i, address in enumerate(POOLS)
    ]
    txs = build_candidates(pools, baseline)
    native = {2: None}  # The separately tested account attestation is stubbed below.
    for direction, tx in enumerate(txs):
        index = list(tx.message.account_keys).index(atomic.Pubkey.from_string(PAYER))
        before = [0] * len(tx.message.account_keys)
        before[index] = baseline["lamports"]
        after = before.copy()
        after[index] -= atomic.NETWORK_FEE
        native[direction] = {
            "context": {"slot": 100},
            "value": {
                "err": {
                    "InstructionError": [
                        len(tx.message.instructions) - 1,
                        {"Custom": 1},
                    ]
                },
                "fee": atomic.NETWORK_FEE,
                "preBalances": before,
                "postBalances": after,
            },
        }
    with patch.object(sys.modules[__name__], "transactions", return_value=(101, txs)):
        older = native_pair(txs, native, 100, baseline, pools=pools, on_time=True)
        require(
            not older["primary_pair_eligible"] and not older["guard_negative"],
            "self_check_older_simulation_became_economic_negative",
        )
        for direction in (0, 1):
            native[direction]["context"]["slot"] = 101
        matched = native_pair(txs, native, 100, baseline, pools=pools, on_time=True)
        require(matched["guard_negative"], "self_check_matched_native_pair_rejected")
    active, count = False, 0
    for row in (
        {"opportunity": True},
        missing("gap"),
        {"opportunity": True},
        {"guard_negative": True},
        {"opportunity": True},
    ):
        active, new = transition(active, row)
        count += new
    require(active and count == 2, "self_check_episode_transition")
    # A changed byte must fail the wire hash before any native result is accepted.
    body = canonical(payload)
    record = {
        "failure": None,
        "raw": {
            "complete": True,
            "received_bytes": len(body),
            "size": len(body),
            "sha256": digest(body),
            "encoding": "base64+zlib",
            "data": base64.b64encode(
                zlib.compress(body.replace(b'"value":0', b'"value":9'))
            ).decode(),
        },
    }
    try:
        decode_wire(record)
    except StudyError:
        pass
    else:
        raise StudyError("self_check_tampered_wire_accepted")
    print(
        "PASS: exact paired IDs, partial/duplicate/tampered rejection, guard-only episode closure"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    freeze = modes.add_parser("scope")
    freeze.add_argument("--study-id", required=True)
    freeze.add_argument("--pilot", action="store_true")
    gather = modes.add_parser("collect")
    gather.add_argument("--scope", type=Path, required=True)
    gather.add_argument("--provider-json", type=Path, required=True)
    gather.add_argument("--out", type=Path, required=True)
    replay = modes.add_parser("analyze")
    replay.add_argument("--scope", type=Path, required=True)
    replay.add_argument("--tape", type=Path, required=True)
    modes.add_parser("self-check")
    args = parser.parse_args()
    try:
        if args.mode == "scope":
            print(
                json.dumps(
                    make_scope(args.study_id, pilot=args.pilot),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.mode == "self-check":
            self_check()
        else:
            scope_path = args.scope.resolve()
            require(scope_path.suffix == ".json", "explicit_scope_json_required")
            scope = validate_scope(strict_json(scope_path.read_bytes()))
            if args.mode == "analyze":
                require(
                    args.tape.resolve().suffix == ".jsonl",
                    "explicit_evidence_jsonl_required",
                )
                print(json.dumps(analyze(args.tape, scope), sort_keys=True))
            elif not asyncio.run(collect(scope, args.provider_json, args.out)):
                raise SystemExit(1)  # noqa: TRY301 - CLI exit status is the contract
    except KeyboardInterrupt:
        print('{"event":"fatal","reason":"interrupted"}', file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - no credential-bearing traceback at the CLI
        print(json.dumps({"event": "fatal", "reason": safe_code(exc)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
