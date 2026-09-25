"""Resolve frozen Robinhood V4 PoolKeys from bounded Initialize event evidence.

CLI: --credentials provider.json --scope scope.json --out new-exclusive.jsonl
Offline only: --self-check. Never sends transactions or accesses ambient secrets.
Every scope field is required; unknown fields are rejected. See SCOPE_FIELDS.
created_at is only a search hint: absence in the fixed window proves no more.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp
from evaluate_evm_capital import (
    NETWORKS,
    RPC,
    BoundReached,
    RPCError,
    RPCReadError,
    abi_address,
    abi_int,
    address,
    check_chain,
    emit,
    quantity,
    raw_hex,
    require,
    uint,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import TextIO

# Frozen request bounds and ABI widths deliberately remain explicit.
# ruff: noqa: PLR2004

MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INITIALIZE_SIGNATURE = (
    "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
)
INITIALIZE_SOURCE = "https://raw.githubusercontent.com/Uniswap/v4-core/main/src/interfaces/IPoolManager.sol"
# Retained Ethereum Keccak topic derived upstream from the official signature;
# do not substitute hashlib.sha3_256 (NIST SHA3 uses different padding).
INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SCOPE_FIELDS = {
    "chain",
    "manager",
    "pools",
    "maximum_seconds",
    "request_limit",
    "request_interval_seconds",
}
NO_FUNDED_ACTIONS = {
    "signed_transactions": 0,
    "submitted_transactions": 0,
    "funding_actions": 0,
    "approvals": 0,
    "contract_deployments": 0,
    "balance_overrides": 0,
}


def read_json(path: Path) -> object:
    require(path.stat().st_size <= 262144, "input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


def bytes32(value: str) -> str:
    require(len(raw_hex(value)) == 32, "invalid_bytes32")
    return value.lower()


def load(scope_path: Path, credentials_path: Path) -> tuple[dict, str]:
    scope = read_json(scope_path)
    require(
        isinstance(scope, dict) and set(scope) == SCOPE_FIELDS, "invalid_scope_fields"
    )
    require(scope["chain"] == "robinhood", "unsupported_chain")
    require(address(scope["manager"]) == MANAGER, "noncanonical_manager")
    scope["manager"] = MANAGER
    require(
        isinstance(scope["pools"], list) and 1 <= len(scope["pools"]) <= 5,
        "invalid_pool_count",
    )
    seen = set()
    for pool in scope["pools"]:
        require(
            isinstance(pool, dict) and set(pool) == {"pool_id", "created_at"},
            "invalid_pool_fields",
        )
        pool["pool_id"] = bytes32(pool["pool_id"])
        require(pool["pool_id"] not in seen, "duplicate_pool_id")
        seen.add(pool["pool_id"])
        require(
            type(pool["created_at"]) is int and 0 <= pool["created_at"] < 1 << 64,
            "invalid_created_at",
        )
    duration = scope["maximum_seconds"]
    require(
        type(duration) in (int, float)
        and math.isfinite(duration)
        and 0 < duration <= 180,
        "invalid_maximum_seconds",
    )
    limit = scope["request_limit"]
    require(type(limit) is int and 1 <= limit <= 200, "invalid_request_limit")
    interval = scope["request_interval_seconds"]
    require(
        type(interval) in (int, float) and math.isfinite(interval) and interval >= 0.3,
        "invalid_request_interval",
    )
    credentials = read_json(credentials_path)
    require(
        isinstance(credentials, dict) and set(credentials) == {"rpc_url", "wss_url"},
        "provider_only_credentials_required",
    )
    for key, scheme in (("rpc_url", "https"), ("wss_url", "wss")):
        require(isinstance(credentials[key], str), "invalid_provider_endpoint")
        parsed = urlsplit(credentials[key])
        require(
            parsed.scheme == scheme
            and bool(parsed.hostname)
            and not parsed.fragment
            and not parsed.username
            and not parsed.password,
            "invalid_provider_endpoint",
        )
    return scope, credentials["rpc_url"]


def failure(exc: BaseException) -> dict:
    """Only fixed local diagnostics; never provider messages, URLs or paths."""
    result = {"error_type": type(exc).__name__}
    if isinstance(exc, RPCError):
        result["rpc_error"] = str(exc)
    elif type(exc) in (ValueError, RPCReadError, BoundReached) and re.fullmatch(
        r"[A-Za-z0-9_]{1,120}", str(exc)
    ):
        result["reason"] = str(exc)
    elif isinstance(exc, TimeoutError):
        result["reason"] = "deadline_reached"
    return result


def header(value: object, expected_number: int | None = None) -> dict:
    """Retain raw public identity fields, not opaque provider-added metadata."""
    require(isinstance(value, dict), "block_header_missing")
    number = quantity(value.get("number"))
    require(
        expected_number is None or number == expected_number, "block_number_mismatch"
    )
    quantity(value.get("timestamp"))
    bytes32(value.get("hash"))
    bytes32(value.get("parentHash"))
    return {key: value[key] for key in ("number", "hash", "parentHash", "timestamp")}


async def lower_bound(
    timestamp: int, head: int, get_header: Callable[[int], Awaitable[dict]]
) -> int:
    """First block with timestamp >= hint, or head + 1; logarithmic reads only."""
    low, high = 0, head + 1
    while low < high:
        middle = (low + high) // 2
        if quantity((await get_header(middle))["timestamp"]) < timestamp:
            low = middle + 1
        else:
            high = middle
    return low


def int24(word: str) -> int:
    value = abi_int(word)
    signed = value - (1 << 256) if value >= 1 << 255 else value
    require(-(1 << 23) <= signed < 1 << 23, "invalid_int24_sign_extension")
    return signed


def decode_log(value: object, pool_id: str, start: int, end: int) -> tuple[dict, dict]:
    require(isinstance(value, dict), "invalid_initialize_log")
    require(value.get("removed") is False, "initialize_log_removed_or_unconfirmed")
    require(address(value.get("address")) == MANAGER, "initialize_manager_mismatch")
    topics = value.get("topics")
    require(isinstance(topics, list) and len(topics) == 4, "invalid_initialize_topics")
    require(
        bytes32(topics[0]) == INITIALIZE_TOPIC and bytes32(topics[1]) == pool_id,
        "initialize_topic_mismatch",
    )
    currency0, currency1 = abi_address(topics[2]), abi_address(topics[3])
    data = raw_hex(value.get("data"))
    require(len(data) == 160, "invalid_initialize_data_length")
    words = ["0x" + data[index : index + 32].hex() for index in range(0, len(data), 32)]
    key = {
        "currency0": currency0,
        "currency1": currency1,
        "fee": abi_int(words[0], 24),
        "tick_spacing": int24(words[1]),
        "hooks": abi_address(words[2]),
    }
    event_state = {
        "sqrt_price_x96": str(abi_int(words[3], 160)),
        "tick": int24(words[4]),
    }
    block = quantity(value.get("blockNumber"))
    require(start <= block <= end, "initialize_outside_requested_window")
    bytes32(value.get("blockHash"))
    bytes32(value.get("transactionHash"))
    quantity(value.get("transactionIndex"))
    quantity(value.get("logIndex"))
    raw = {
        field: value[field]
        for field in (
            "address",
            "topics",
            "data",
            "blockNumber",
            "blockHash",
            "transactionHash",
            "transactionIndex",
            "logIndex",
            "removed",
        )
    }
    return {"pool_key": key, "initial_state": event_state}, raw


async def run(scope: dict, endpoint: str, out: TextIO) -> int:  # noqa: C901, PLR0915 - one bounded canonical-log resolution lifecycle
    started = time.monotonic()
    outcomes = []
    terminal_error = None
    async with aiohttp.ClientSession(trust_env=False) as session:
        rpc = RPC(
            session,
            endpoint,
            started + scope["maximum_seconds"],
            request_limit=scope["request_limit"],
            request_interval=scope["request_interval_seconds"],
        )

        def record(event: str, **fields: object) -> None:
            emit(
                out,
                event,
                **fields,
                rpc_method_requests=rpc.requests,
                http_requests=rpc.http_requests,
                **NO_FUNDED_ACTIONS,
            )

        record(
            "scope",
            schema_version=1,
            scope=scope,
            initialize_signature=INITIALIZE_SIGNATURE,
            initialize_topic0=INITIALIZE_TOPIC,
            initialize_source=INITIALIZE_SOURCE,
            block_window_policy="[max(0, lower_bound-128), min(head, lower_bound+127)]",
            window_maximum_blocks=256,
            created_at_is_hint=True,
            retries=0,
            pool_key_hash_verified=False,
            current_state_attested=False,
            execution_support_assessed=False,
        )
        try:
            async with asyncio.timeout(scope["maximum_seconds"]):
                chain_id = await rpc.call("eth_chainId", [])
                check_chain(chain_id, NETWORKS["robinhood"]["chain_id"])
                head = header(await rpc.call("eth_getBlockByNumber", ["latest", False]))
                head_number = quantity(head["number"])
                cache = {head_number: head}

                async def get_header(number: int) -> dict:
                    if number not in cache:
                        cache[number] = header(
                            await rpc.call(
                                "eth_getBlockByNumber", [hex(number), False]
                            ),
                            number,
                        )
                    return cache[number]

                record("head", chain_id=chain_id, raw_header=head)
                for pool in scope["pools"]:
                    context = dict(pool)
                    try:
                        location = await lower_bound(
                            pool["created_at"], head_number, get_header
                        )
                        start, end = (
                            max(0, location - 128),
                            min(head_number, location + 127),
                        )
                        query = {
                            "address": MANAGER,
                            "topics": [INITIALIZE_TOPIC, pool["pool_id"]],
                            "fromBlock": hex(start),
                            "toBlock": hex(end),
                        }
                        context.update(
                            lower_bound_block=location,
                            log_filter=query,
                            search_headers=[
                                cache[n] for n in (location - 1, location) if n in cache
                            ],
                        )
                        record("search_window", **context)
                        logs = await rpc.call("eth_getLogs", [query])
                        require(isinstance(logs, list), "invalid_logs_response")
                        if not logs:
                            outcome = {
                                **context,
                                "status": "missing",
                                "reason": "no_initialize_in_bounded_window",
                            }
                        else:
                            evidence = []
                            for log in logs:
                                decoded, raw = decode_log(
                                    log, pool["pool_id"], start, end
                                )
                                # Fresh number lookup, not the timestamp-search cache: detect a reorg.
                                canonical = header(
                                    await rpc.call(
                                        "eth_getBlockByNumber",
                                        [raw["blockNumber"], False],
                                    ),
                                    quantity(raw["blockNumber"]),
                                )
                                require(
                                    canonical["hash"].lower()
                                    == raw["blockHash"].lower(),
                                    "initialize_block_not_canonical",
                                )
                                evidence.append(
                                    {
                                        "raw_log": raw,
                                        "raw_canonical_header": canonical,
                                        **decoded,
                                    }
                                )
                            if len(evidence) != 1:
                                outcome = {
                                    **context,
                                    "status": "failed",
                                    "reason": "ambiguous_initialize_logs",
                                    "evidence": evidence,
                                }
                            else:
                                outcome = {
                                    **context,
                                    "status": "resolved",
                                    **evidence[0],
                                    "canonical_at_header_read": True,
                                    "pool_key_hash_verified": False,
                                    "current_state_attested": False,
                                    "execution_support_assessed": False,
                                }
                    except (BoundReached, RPCReadError, RPCError):
                        raise
                    except (ValueError, TypeError, KeyError) as exc:
                        outcome = {**context, "status": "failed", **failure(exc)}
                    outcomes.append(outcome)
                    record("pool_key_outcome", **outcome)
        except (
            BoundReached,
            RPCReadError,
            RPCError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            terminal_error = failure(exc)
        if terminal_error:
            for index, pool in enumerate(scope["pools"][len(outcomes) :]):
                outcome = {
                    **pool,
                    "status": "failed" if index == 0 else "not_attempted",
                    **terminal_error,
                }
                outcomes.append(outcome)
                record("pool_key_outcome", **outcome)
        resolved = sum(row["status"] == "resolved" for row in outcomes)
        complete = resolved == len(scope["pools"])
        record(
            "summary",
            complete=complete,
            resolved=resolved,
            missing_or_failed=len(outcomes) - resolved,
            terminal_error=terminal_error,
            elapsed_seconds=time.monotonic() - started,
            all_requested_pools_accounted_for=len(outcomes) == len(scope["pools"]),
            search_scope_only=True,
            profitability_assessed=False,
        )
    return 0 if complete else 1


def self_check() -> None:
    """Compact deterministic parser/search check; no session, credentials or network."""

    async def check_search() -> None:
        timestamps = (10, 20, 20, 40)

        async def get_header(number: int) -> dict:
            return {"timestamp": hex(timestamps[number])}

        for hint, expected in ((0, 0), (10, 0), (20, 1), (21, 3), (40, 3), (41, 4)):
            require(
                await lower_bound(hint, 3, get_header) == expected,
                "lower_bound_self_check_failed",
            )

    asyncio.run(check_search())
    for value in (-(1 << 23), -60, -1, 0, 60, (1 << 23) - 1):
        require(
            int24("0x" + uint(value % (1 << 256)).hex()) == value,
            "int24_self_check_failed",
        )
    for malformed in ((1 << 24) - 1, 1 << 23, (1 << 256) - (1 << 23) - 1):
        try:
            int24("0x" + uint(malformed).hex())
        except ValueError:
            continue
        raise ValueError("int24_padding_not_rejected")
    print("offline_lower_bound_and_int24_checks_passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--scope", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if not all((args.credentials, args.scope, args.out)):
        parser.error("credentials_scope_out_required")
    try:
        require(args.out.suffix == ".jsonl", "output_requires_jsonl")
        scope, endpoint = load(args.scope, args.credentials)
        with args.out.open("x", encoding="utf-8") as out:
            status = asyncio.run(run(scope, endpoint, out))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, "input_or_output_rejected_" + type(exc).__name__ + "\n")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
