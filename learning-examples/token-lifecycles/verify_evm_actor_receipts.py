"""Provider-only prospective block-receipt census; never sign or submit.

Collect: --credentials provider.json --scope scope.json --out exclusive.jsonl
Offline: --analyze existing.jsonl [--out exclusive-summary.json] | --self-check
Credentials: exactly {rpc_url} or {rpc_url,wss_url}; HTTPS RPC only, no ambient env.
Scope (all fields required; no extras): chain, selection_id, seed,
duration_seconds=3600, interval_seconds=30, discovery_seconds=600, lag_blocks=2,
blocks_per_window=4, request_limit (1..5000), request_interval_seconds (>=0.35),
max_output_bytes (16MiB..512MiB), trace_multi_per_window (0..3),
trace_failed_per_window (0..3), trace_control_per_window (0..3).

Each scheduled window freezes latest minus two, samples four consecutive blocks,
retains all receipts, and rechecks all four canonical hashes after tracing. No
catch-up to a later head: an overrun is an explicit unknown scheduled window.
Discovery freezes three most active multi-venue payers, three highest-count failed
payers, and three hash-ranked non-multi controls (disjoint, address ties). Holdout
scores every receipt paid by those actors *in sampled windows only*. Trace ranking
is SHA256(seed|stratum|txhash), independent of amounts/profit, within predeclared
multi-venue, failed, and non-multi successful control strata. These are distinct
status-conditioned strata, not a random sample of all market attempts.

Exact HTTP response bytes (zlib+base64, original size and SHA256), raw
blocks/receipts/traces, and derived rows share one bounded hash-chained JSONL.
Stdout contains only small summaries. Optional trace method refusals, timeouts,
and oversized responses remain explicit missing evidence; authentication,
malformed protocol, integrity/reorg, and global-bound failures remain fatal.
Native state differences already include fees. Native-wrapper Transfer logs are
cashflow evidence, not proof of token balances, ownership, or portfolio profit.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import urlsplit

import aiohttp
from evaluate_evm_capital import (
    NETWORKS,
    RPC,
    ZERO,
    RPCError,
    RPCReadError,
    abi_address,
    address,
    check_chain,
    quantity,
    raw_hex,
    require,
    uint,
)
from observe_evm_meme_cycles import (
    V4_DEPLOYMENTS,
    bounded,
    digest,
    evm_keccak,
    failure,
    header,
    read_json,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

# Protocol widths, schedule bounds and offline assertions remain explicit.
# ruff: noqa: PLR2004, S101

# Full Polygon receipt batches exceed the quote helper's 2MiB default.
RESPONSE_LIMIT = 16 * 1024 * 1024

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAPS = {
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": (
        "v3_signature_unattested",
        5,
    ),
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": (
        "v2_signature_unattested",
        4,
    ),
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83": (
        "extended_v3_signature_unattested",
        7,
    ),
}
V4_SIGNATURE = b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
WRAPPERS = {
    "polygon": NETWORKS["polygon"]["wrapped_native"],
    "robinhood": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
}
NATIVE_FACADE = "0x0000000000000000000000000000000000001010"
SCOPE_KEYS = set(
    "chain selection_id seed duration_seconds interval_seconds discovery_seconds lag_blocks blocks_per_window request_limit request_interval_seconds max_output_bytes trace_multi_per_window trace_failed_per_window trace_control_per_window".split()
)
SOURCES = [
    "https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers",
    "https://raw.githubusercontent.com/0xPolygon/bor/develop/core/types/bor_receipt.go",
    "https://raw.githubusercontent.com/maticnetwork/contracts/master/contracts/child/MRC20.sol",
    "https://raw.githubusercontent.com/OffchainLabs/token-bridge-contracts/main/contracts/tokenbridge/libraries/aeWETH.sol",
    "https://raw.githubusercontent.com/Uniswap/v4-core/main/src/libraries/Pool.sol",
    "https://developers.uniswap.org/docs/protocols/v4/deployments",
]
LIMITS = [
    "Sampled block census, not lossless head history, complete actor history, daily rate, or portfolio income.",
    "Inter-window activity, pending/dropped/private/unsubmitted attempts, and beginning/end inventories are unknown.",
    "Two-block lag and end-of-window canonical rechecks are not finality guarantees.",
    "Success is execution status only; positive base cashflow is not profit.",
    "Payer and root executor are separate custodians; their arithmetic union does not prove common ownership.",
    "Transfer logs do not attest rebases, taxes, hidden balances, positions, debt, authorization effects or token extensions.",
    "V2/V3-like emitters are unverified venues; V4 manager identity does not attest PoolKey, hooks, or final settlement.",
    "Known receipt fees are components, not additional deductions from native pre/post deltas; gasUsedForL1 is not added twice.",
]


def packed(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def source_hashes() -> dict[str, str]:
    return {
        name: digest(Path(__file__).with_name(name).read_bytes())
        for name in (
            Path(__file__).name,
            "evaluate_evm_capital.py",
            "observe_evm_meme_cycles.py",
        )
    }


def decode_response(row: dict) -> bytes:
    """Bound decompression independently of the claimed original byte count."""
    require(row.get("body_encoding") == "zlib+base64", "unsupported_response_encoding")
    require(
        type(row["response_bytes"]) is int
        and 0 <= row["response_bytes"] <= RESPONSE_LIMIT,
        "invalid_response_size",
    )
    compressed = base64.b64decode(row["body_base64"], validate=True)
    require(len(compressed) <= RESPONSE_LIMIT * 2, "compressed_response_too_large")
    decoder = zlib.decompressobj()
    data = decoder.decompress(compressed, RESPONSE_LIMIT + 1)
    require(
        len(data) <= RESPONSE_LIMIT
        and decoder.eof
        and not decoder.unconsumed_tail
        and not decoder.unused_data,
        "invalid_or_oversized_compressed_response",
    )
    require(
        len(data) == row["response_bytes"] and digest(data) == row["response_sha256"],
        "raw_response_hash_mismatch",
    )
    return data


def scope_check(s: dict) -> dict:
    require(isinstance(s, dict) and set(s) == SCOPE_KEYS, "invalid_scope_fields")
    require(s["chain"] in NETWORKS, "unsupported_chain")
    for key in ("selection_id", "seed"):
        require(
            isinstance(s[key], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", s[key]),
            "invalid_scope_label",
        )
    for key, value in {
        "duration_seconds": 3600,
        "interval_seconds": 30,
        "discovery_seconds": 600,
        "lag_blocks": 2,
        "blocks_per_window": 4,
    }.items():
        require(
            type(s[key]) is int and s[key] == value, "invalid_frozen_sampling_design"
        )
    for key, low, high in [
        ("request_limit", 1, 5000),
        ("max_output_bytes", 16 * 1024 * 1024, 512 * 1024 * 1024),
        ("trace_multi_per_window", 0, 3),
        ("trace_failed_per_window", 0, 3),
        ("trace_control_per_window", 0, 3),
    ]:
        require(type(s[key]) is int and low <= s[key] <= high, "invalid_integer_bound")
    bounded(s["request_interval_seconds"], 0.35, 30)
    return s


class Tape:
    """Exclusive file with reserved terminal capacity; no raw responses on stdout."""

    def __init__(self, out: BinaryIO, limit: int) -> None:
        self.out, self.limit, self.size, self.seq, self.previous = (
            out,
            limit,
            0,
            0,
            "0" * 64,
        )

    def put(self, event: str, *, terminal: bool = False, **fields: object) -> dict:
        row = {
            "event": event,
            "seq": self.seq,
            "previous_sha256": self.previous,
            "at": time.time(),
            "monotonic": time.monotonic(),
            **fields,
        }
        checksum = digest(packed(row))
        data = packed({**row, "sha256": checksum}) + b"\n"
        require(
            self.size + len(data) <= self.limit - (0 if terminal else 65536),
            "retained_output_bound",
        )
        self.out.write(data)
        self.out.flush()
        self.size += len(data)
        self.seq += 1
        self.previous = checksum
        return row


class RecordingContent:
    def __init__(
        self,
        response: aiohttp.ClientResponse,
        tape: Tape,
        request: dict,
        started: float,
    ) -> None:
        self.response, self.tape, self.request, self.started = (
            response,
            tape,
            request,
            started,
        )

    async def readexactly(self, n: int) -> bytes:
        incomplete = None
        try:
            data = await self.response.content.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            data, incomplete = exc.partial, exc
        # Non-200/error bodies are never retained: provider error prose may contain secrets.
        payload = None
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            pass
        if isinstance(payload, dict) and "error" in payload:
            error = payload["error"]
            require_error_envelope = (
                payload.get("jsonrpc") == "2.0"
                and type(payload.get("id")) is int
                and payload["id"] == self.request["id"]
                and "result" not in payload
                and isinstance(error, dict)
                and type(error.get("code")) is int
                and isinstance(error.get("message"), str)
            )
            if not require_error_envelope:
                raise RPCReadError("rpc_invalid_error_envelope")
            message = error["message"].casefold()
            if error["code"] in (401, 403) or any(
                text in message
                for text in (
                    "unauthoriz",
                    "forbidden",
                    "authentication",
                    "invalid api",
                    "invalid key",
                    "invalid token",
                    "invalid credential",
                    "access denied",
                    "api key required",
                )
            ):
                raise RPCReadError("rpc_provider_authentication_rejected")
            if (
                error["code"] in (-32700, -32600)
                or (
                    error["code"] == -32602
                    and self.request["method"] != "debug_traceTransaction"
                )
                or any(
                    text in message
                    for text in (
                        "rate limit",
                        "quota",
                        "credits exhausted",
                        "credit limit",
                        "monthly limit",
                        "daily limit",
                        "request limit",
                        "subscription expired",
                    )
                )
            ):
                raise RPCReadError("rpc_provider_protocol_or_global_limit_rejected")
        safe = (
            isinstance(payload, dict) and "result" in payload and "error" not in payload
        )
        self.tape.put(
            "rpc_response",
            request=self.request,
            http_status=self.response.status,
            http_started_monotonic=self.started,
            response_bytes=len(data),
            response_sha256=digest(data),
            retained=bool(safe and len(data) <= RESPONSE_LIMIT),
            response_complete=len(data) <= RESPONSE_LIMIT,
            response_hash_scope="entire_body"
            if len(data) <= RESPONSE_LIMIT
            else "bounded_prefix",
            body_encoding="zlib+base64",
            body_base64=base64.b64encode(zlib.compress(data)).decode()
            if safe and len(data) <= RESPONSE_LIMIT
            else None,
            omitted_reason=None
            if safe and len(data) <= RESPONSE_LIMIT
            else "oversized_or_invalid_or_error_body_secret_guard",
        )
        if incomplete is not None:
            raise incomplete
        return data


class RecordingResponse:
    def __init__(
        self,
        manager: AbstractAsyncContextManager[aiohttp.ClientResponse],
        tape: Tape,
        request: dict,
        started: float,
    ) -> None:
        self.manager, self.tape, self.request, self.started = (
            manager,
            tape,
            request,
            started,
        )

    async def __aenter__(self) -> RecordingResponse:
        response = await self.manager.__aenter__()
        self.status = response.status
        self.content = RecordingContent(response, self.tape, self.request, self.started)
        if response.status != 200:
            self.tape.put(
                "rpc_http_failure", request=self.request, http_status=response.status
            )
        return self

    async def __aexit__(self, *args: object) -> bool | None:
        return await self.manager.__aexit__(*args)


class RecordingSession:
    """Adapter leaves shared RPC pacing, limits, envelope parsing and failures intact."""

    def __init__(self, session: aiohttp.ClientSession, tape: Tape) -> None:
        self.session, self.tape = session, tape

    def post(self, endpoint: str, **kwargs: object) -> RecordingResponse:
        started = time.monotonic()
        self.tape.put(
            "rpc_request", request=kwargs["json"], http_started_monotonic=started
        )
        return RecordingResponse(
            self.session.post(endpoint, **kwargs), self.tape, kwargs["json"], started
        )


def optional_trace_failure(detail: dict) -> bool:
    if detail.get("error_type") == "RPCError":
        return (
            re.fullmatch(
                r"rpc_debug_traceTransaction_error_-?[0-9]+",
                detail.get("rpc_error", ""),
            )
            is not None
        )
    return detail.get("error_type") == "RPCReadError" and detail.get("reason") in {
        "rpc_response_too_large",
        "rpc_debug_traceTransaction_http_413",
        "rpc_debug_traceTransaction_transport_TimeoutError",
    }


async def trace_one(
    rpc: RPC, tape: Tape, window: int, txhash: str, tracer: str
) -> dict | None:
    """Only explicitly optional trace refusals/timeouts become missing evidence."""
    config: dict[str, Any] = {"tracer": tracer, "timeout": "5s"}
    if tracer == "prestateTracer":
        config["tracerConfig"] = {"diffMode": True}
    started = time.monotonic()
    try:
        result = await rpc.call("debug_traceTransaction", [txhash, config])
    except (RPCError, RPCReadError) as exc:
        detail = failure(exc)
        if not optional_trace_failure(detail):
            raise
        tape.put(
            "trace_outcome",
            window=window,
            transaction_hash=txhash,
            tracer=tracer,
            request_id=rpc.requests,
            status="missing_optional_trace",
            failure=detail,
            started_monotonic=started,
            finished_monotonic=time.monotonic(),
        )
        return None
    if (
        not isinstance(result, dict)
        or (tracer == "callTracer" and not {"type", "from"} <= set(result))
        or (
            tracer == "prestateTracer"
            and (
                not {"pre", "post"} <= set(result)
                or not all(isinstance(result[key], dict) for key in ("pre", "post"))
            )
        )
    ):
        raise RPCReadError("rpc_invalid_trace_result")
    tape.put(
        "trace_outcome",
        window=window,
        transaction_hash=txhash,
        tracer=tracer,
        request_id=rpc.requests,
        status="available",
        started_monotonic=started,
        finished_monotonic=time.monotonic(),
    )
    return result


def hash32(value: str) -> str:
    require(len(raw_hex(value)) == 32, "invalid_hash32")
    return value.lower()


def correlate(
    block: dict, receipts: list, chain: str, bor_hash: str | None = None
) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Exact ordinary hash/index coverage plus explicitly attested Bor synthetic receipt."""
    h = header(block)
    txs = block.get("transactions")
    require(
        isinstance(txs, list) and isinstance(receipts, list), "missing_block_receipts"
    )
    expected = {}
    for index, tx in enumerate(txs):
        require(isinstance(tx, dict), "hydrated_transaction_required")
        txhash = hash32(tx.get("hash"))
        require(
            txhash not in expected and quantity(tx.get("transactionIndex")) == index,
            "duplicate_or_wrong_transaction_index",
        )
        require(
            hash32(tx.get("blockHash")) == h["hash"]
            and quantity(tx.get("blockNumber")) == h["number"],
            "transaction_block_identity",
        )
        address(tx.get("from"))
        if tx.get("to") is not None:
            address(tx["to"])
        quantity(tx.get("value"))
        expected[txhash] = (index, tx)
    seen, indexes, log_indexes, ordinary, system = set(), set(), set(), [], []
    for receipt in receipts:
        require(isinstance(receipt, dict), "invalid_receipt")
        txhash, index = (
            hash32(receipt.get("transactionHash")),
            quantity(receipt.get("transactionIndex")),
        )
        require(txhash not in seen and index not in indexes, "duplicate_receipt")
        seen.add(txhash)
        indexes.add(index)
        require(
            hash32(receipt.get("blockHash")) == h["hash"]
            and quantity(receipt.get("blockNumber")) == h["number"],
            "receipt_block_identity",
        )
        require(quantity(receipt.get("status")) in (0, 1), "invalid_receipt_status")
        require(isinstance(receipt.get("logs"), list), "missing_receipt_logs")
        for log in receipt["logs"]:
            require(
                hash32(log.get("blockHash")) == h["hash"]
                and quantity(log.get("blockNumber")) == h["number"],
                "log_block_identity",
            )
            require(
                hash32(log.get("transactionHash")) == txhash
                and quantity(log.get("transactionIndex")) == index,
                "log_transaction_identity",
            )
            li = quantity(log.get("logIndex"))
            require(
                li not in log_indexes and log.get("removed") is False,
                "duplicate_or_removed_log",
            )
            log_indexes.add(li)
            address(log.get("address"))
            require(isinstance(log.get("topics"), list), "invalid_log_topics")
            for topic in log["topics"]:
                hash32(topic)
            raw_hex(log.get("data"))
        if txhash in expected:
            expected_index, tx = expected[txhash]
            require(
                index == expected_index
                and address(receipt.get("from")) == address(tx["from"]),
                "receipt_transaction_mismatch",
            )
            require(
                (address(receipt["to"]) if receipt.get("to") else None)
                == (address(tx["to"]) if tx.get("to") else None),
                "receipt_destination_mismatch",
            )
            ordinary.append((tx, receipt))
        else:
            require(
                chain == "polygon"
                and bor_hash is not None
                and txhash == bor_hash
                and index == len(txs),
                "unattested_extra_receipt",
            )
            system.append(receipt)
    require(
        set(expected) <= seen and len(ordinary) == len(txs),
        "incomplete_receipt_hash_coverage",
    )
    return sorted(
        ordinary, key=lambda pair: quantity(pair[1]["transactionIndex"])
    ), system


def fee_components(receipt: dict) -> dict:
    gas = quantity(receipt["gasUsed"]) if receipt.get("gasUsed") is not None else None
    price = (
        quantity(receipt["effectiveGasPrice"])
        if receipt.get("effectiveGasPrice") is not None
        else None
    )
    execution = gas * price if gas is not None and price is not None else None
    blob = None
    if (
        quantity(receipt.get("type", "0x0")) == 3
        or "blobGasUsed" in receipt
        or "blobGasPrice" in receipt
    ):
        if (
            receipt.get("blobGasUsed") is not None
            and receipt.get("blobGasPrice") is not None
        ):
            blob = quantity(receipt["blobGasUsed"]) * quantity(receipt["blobGasPrice"])
        total = execution + blob if execution is not None and blob is not None else None
    else:
        total = execution
    return {
        "execution_fee_raw": execution,
        "separate_blob_fee_raw": blob,
        "known_receipt_fee_raw": total,
        "gas_used_for_l1_not_added": receipt.get("gasUsedForL1"),
    }


def signed_word(data: bytes, bits: int) -> int:
    value = int.from_bytes(data, "big", signed=True)
    require(
        -(1 << (bits - 1)) <= value < (1 << (bits - 1)), "invalid_signed_event_width"
    )
    return value


def receipt_features(tx: dict, receipt: dict, chain: str, v4_topic: str) -> dict:
    payer = address(tx["from"])
    root = (
        address(tx["to"])
        if tx.get("to")
        else (
            address(receipt["contractAddress"])
            if receipt.get("contractAddress")
            else None
        )
    )
    transfers, swaps, ambiguous = [], [], []
    for log in receipt["logs"]:
        topics = [x.lower() for x in log["topics"]]
        data, emitter = raw_hex(log["data"]), address(log["address"])
        if not topics:
            ambiguous.append(
                {
                    "emitter": emitter,
                    "log_index": log["logIndex"],
                    "reason": "anonymous_log",
                }
            )
            continue
        topic = topics[0]
        if topic == TRANSFER and len(topics) == 3 and len(data) == 32:
            transfers.append(
                {
                    "token": emitter,
                    "from": abi_address(topics[1]),
                    "to": abi_address(topics[2]),
                    "amount_raw": int.from_bytes(data, "big"),
                    "log_index": quantity(log["logIndex"]),
                    "semantics": "native_facade_not_independent_asset"
                    if chain == "polygon" and emitter == NATIVE_FACADE
                    else "erc20_shape_not_balance_attestation",
                }
            )
        elif topic in SWAPS or topic == v4_topic:
            family, words = SWAPS.get(topic, ("v4_signature", 6))
            valid = len(topics) == 3 and len(data) == words * 32
            if not valid:
                ambiguous.append(
                    {
                        "emitter": emitter,
                        "log_index": log["logIndex"],
                        "reason": "swap_shape_mismatch",
                    }
                )
                continue
            abi_address(topics[2])
            if topic != v4_topic:
                abi_address(topics[1])
            values = [data[i : i + 32] for i in range(0, len(data), 32)]
            amounts = (
                [int.from_bytes(x, "big") for x in values[:4]]
                if words == 4
                else [
                    signed_word(x, 128 if topic == v4_topic else 256)
                    for x in values[:2]
                ]
            )
            if words != 4:
                uint(int.from_bytes(values[2], "big"), 160)
                uint(int.from_bytes(values[3], "big"), 128)
                signed_word(values[4], 24)
            if topic == v4_topic:
                uint(int.from_bytes(values[5], "big"), 24)
            canonical = topic == v4_topic and emitter == V4_DEPLOYMENTS[chain][0]
            identity = emitter + (":" + topics[1] if topic == v4_topic else "")
            swaps.append(
                {
                    "venue": identity,
                    "emitter": emitter,
                    "family": family,
                    "amounts_raw": amounts,
                    "canonical_manager": canonical,
                    "pool_key_hooks_attested": False,
                    "canonical_pool": None,
                    "amount_orientation": "caller_entitlement_negative_input"
                    if topic == v4_topic
                    else "pool_receives_positive"
                    if words != 4
                    else "amount0In_amount1In_amount0Out_amount1Out",
                    "log_index": quantity(log["logIndex"]),
                }
            )
        else:
            ambiguous.append(
                {
                    "emitter": emitter,
                    "log_index": log["logIndex"],
                    "topic0": topic,
                    "reason": "uninterpreted_event_or_token_extension",
                }
            )
    status = quantity(receipt["status"])
    classification = (
        "failed_paid_attempt_unclassified"
        if not status
        else "multi_venue_signature_candidate"
        if len({s["venue"] for s in swaps}) > 1
        else "single_venue_signature"
        if swaps
        else "no_recognized_swap_not_proof_non_dex"
    )
    return {
        "transaction_hash": hash32(tx["hash"]),
        "block_hash": hash32(receipt["blockHash"]),
        "transaction_index": quantity(receipt["transactionIndex"]),
        "payer": payer,
        "root_executor": root,
        "status": status,
        "classification": classification,
        "multi_venue": len({s["venue"] for s in swaps}) > 1,
        "multi_swap": len(swaps) > 1,
        "swaps": swaps,
        "transfers": transfers,
        "native_cashflow_candidate": quantity(tx["value"]) > 0
        or any(t["token"] == WRAPPERS[chain] for t in transfers),
        "uninterpreted_logs": ambiguous,
        "transaction_value_raw": quantity(tx["value"]),
        "authorization_effects_possible": bool(tx.get("authorizationList")),
        **fee_components(receipt),
    }


def native_delta(diff: dict, who: str) -> int | None:
    """Sparse fields unchanged; absent account unchanged; insert/delete are zero boundaries."""
    require(
        isinstance(diff, dict) and set(diff) >= {"pre", "post"}, "invalid_state_diff"
    )
    require(
        all(
            isinstance(diff[side], dict)
            and all(isinstance(v, dict) for v in diff[side].values())
            for side in ("pre", "post")
        ),
        "invalid_state_diff_accounts",
    )
    pre, post = (
        {address(k): v for k, v in diff[side].items()} for side in ("pre", "post")
    )
    if who not in pre and who not in post:
        return 0
    if who not in pre:
        return quantity(post[who]["balance"]) if "balance" in post[who] else 0
    if "balance" not in pre[who]:
        return None
    before = quantity(pre[who]["balance"])
    if who not in post:
        return -before
    return quantity(post[who]["balance"]) - before if "balance" in post[who] else 0


def value_edges(trace: dict) -> list[dict]:
    """Committed EVM transfers only; inherited DELEGATECALL value is not a transfer."""
    require(
        isinstance(trace, dict) and "type" in trace and "from" in trace,
        "invalid_call_trace",
    )
    edges, stack = [], [(trace, False)]
    while stack:
        frame, reverted = stack.pop()
        require(isinstance(frame, dict), "invalid_call_frame")
        reverted = reverted or bool(frame.get("error"))
        children = frame.get("calls", [])
        require(isinstance(children, list), "invalid_trace_children")
        stack.extend((child, reverted) for child in children)
        if not reverted and frame.get("type", "").upper() in {
            "CALL",
            "CREATE",
            "CREATE2",
            "SELFDESTRUCT",
            "SUICIDE",
        }:
            value = quantity(frame.get("value", "0x0"))
            if value:
                edges.append(
                    {
                        "from": address(frame["from"]),
                        "to": address(frame["to"]),
                        "amount_raw": value,
                    }
                )
    return edges


def account_cashflow(
    feature: dict, owners: set[str], diff: dict | None, trace: dict | None, chain: str
) -> dict:
    wrapper = WRAPPERS[chain]
    ledger = Counter()
    external = []
    for transfer in feature["transfers"]:
        if chain == "polygon" and transfer["token"] == NATIVE_FACADE:
            continue
        amount = transfer["amount_raw"]
        ledger[transfer["token"]] += amount * (
            (transfer["to"] in owners) - (transfer["from"] in owners)
        )
        if amount and (transfer["to"] in owners) != (transfer["from"] in owners):
            external.append(transfer)
    native = (
        [native_delta(diff, who) for who in sorted(owners)]
        if diff is not None
        else None
    )
    native_sum = (
        sum(native)
        if native is not None and all(x is not None for x in native)
        else None
    )
    inventory = {
        token: value
        for token, value in ledger.items()
        if token != wrapper and value != 0
    }
    # Transfer logs alone miss wrapper mint/burn effects such as WETH9 Withdrawal.
    wrapper_unresolved = any(
        log["emitter"] == wrapper for log in feature["uninterpreted_logs"]
    )
    base = (
        native_sum + ledger[wrapper]
        if native_sum is not None and not wrapper_unresolved
        else None
    )
    edges = value_edges(trace) if trace is not None else None
    native_external = (
        [edge for edge in edges if (edge["from"] in owners) != (edge["to"] in owners)]
        if edges is not None
        else None
    )
    # A base delta remains observable even if inventory/ownership prevents a profit claim.
    return {
        "addresses": sorted(owners),
        "native_delta_raw_fee_already_included": native_sum,
        "trusted_wrapper_transfer_delta_raw": ledger[wrapper],
        "wrapper_balance_changes_unresolved": wrapper_unresolved,
        "observed_base_cashflow_raw": base,
        "base_cashflow_sign": "unknown"
        if base is None
        else "positive"
        if base > 0
        else "negative"
        if base < 0
        else "zero",
        "non_base_transfer_inventory_raw": inventory,
        "excluded_from_closed_route": bool(inventory)
        or base is None
        or bool(feature["uninterpreted_logs"]),
        "external_token_flows_ownership_unknown": external,
        "external_native_flows_ownership_unknown": native_external,
        "unconditional_profit_raw": None,
        "profit_qualification": "inventory_change"
        if inventory
        else "native_trace_missing"
        if native_sum is None
        else "wrapper_balance_changes_unresolved"
        if wrapper_unresolved
        else "token_extensions_obligations_and_external_funding_unresolved",
    }


def score(feature: dict, diff: dict | None, trace: dict | None, chain: str) -> dict:
    payer, root = feature["payer"], feature["root_executor"]
    owners = {payer} | ({root} if root else set())
    nitro = []
    if trace is not None:
        require(address(trace["from"]) == payer, "trace_payer_mismatch")
        if root is not None:
            require(address(trace.get("to")) == root, "trace_root_mismatch")
        require(
            bool(trace.get("error")) == (feature["status"] == 0),
            "trace_status_mismatch",
        )
        for stage in ("beforeEVMTransfers", "afterEVMTransfers"):
            require(isinstance(trace.get(stage, []), list), "invalid_nitro_transfers")
            for transfer in trace.get(stage, []):
                nitro.append(
                    {
                        "stage": stage,
                        "purpose": transfer.get("purpose"),
                        "from": address(transfer["from"])
                        if transfer.get("from")
                        else None,
                        "to": address(transfer["to"]) if transfer.get("to") else None,
                        "amount_raw": quantity(transfer["value"]),
                    }
                )
    nitro_payer = sum(
        t["amount_raw"] * ((t["to"] == payer) - (t["from"] == payer)) for t in nitro
    )
    result = {
        "transaction_hash": feature["transaction_hash"],
        "payer": payer,
        "status": feature["status"],
        "payer_cashflow": account_cashflow(feature, {payer}, diff, trace, chain),
        "payer_plus_root_cashflow_not_owned_portfolio": account_cashflow(
            feature, owners, diff, trace, chain
        ),
        "root_cashflow_separate_custody": account_cashflow(
            feature, {root}, diff, trace, chain
        )
        if root
        else None,
        "nitro_protocol_transfers": nitro,
        "nitro_payer_delta_raw_not_added_again": nitro_payer if nitro else None,
        "known_receipt_fee_raw": feature["known_receipt_fee_raw"],
        "traced": diff is not None and trace is not None,
    }
    if trace is not None:
        evm_payer = sum(
            e["amount_raw"] * ((e["to"] == payer) - (e["from"] == payer))
            for e in value_edges(trace)
        )
        expected = (
            evm_payer + nitro_payer
            if nitro
            else evm_payer - feature["known_receipt_fee_raw"]
            if feature["known_receipt_fee_raw"] is not None
            else None
        )
        observed = result["payer_cashflow"]["native_delta_raw_fee_already_included"]
        result["native_reconciliation"] = {
            "evm_value_delta_raw": evm_payer,
            "evm_plus_protocol_or_fee_delta_raw": expected,
            "prestate_delta_raw": observed,
            "difference_raw": observed - expected
            if observed is not None and expected is not None
            else None,
            "reconciled": observed == expected
            if observed is not None and expected is not None
            else None,
            "note": "Nitro before/after includes fee payment/refund/collection; use instead of subtracting receipt fee; discrepancy stays unresolved",
        }
    result["failed_known_fee_burden_raw"] = (
        feature["known_receipt_fee_raw"] if feature["status"] == 0 else None
    )
    return result


def rank(seed: str, stratum: str, identity: str) -> str:
    return digest(f"{seed}|{stratum}|{identity}".encode())


def select_traces(features: list[dict], scope: dict) -> tuple[dict[str, str], dict]:
    strata = {"multi": [], "failed": [], "control": []}
    for feature in features:
        name = (
            "failed"
            if feature["status"] == 0
            else "multi"
            if feature["multi_venue"]
            else "control"
        )
        strata[name].append(feature["transaction_hash"])
    selected = {}
    for name, values in strata.items():
        ordered = sorted(values, key=lambda h: (rank(scope["seed"], name, h), h))
        for txhash in ordered[: scope[f"trace_{name}_per_window"]]:
            selected[txhash] = name
    return selected, {
        name: {
            "eligible": len(values),
            "selected": sum(x == name for x in selected.values()),
            "inclusion_fraction": [
                min(len(values), scope[f"trace_{name}_per_window"]),
                len(values),
            ],
        }
        for name, values in strata.items()
    }


class Study:
    def __init__(self, scope: dict) -> None:
        self.scope, self.discovery, self.cohort = scope, defaultdict(Counter), None
        self.cumulative = {"discovery": Counter(), "holdout": Counter()}
        self.actor_totals = defaultdict(Counter)
        self.blocks = {}

    def freeze(self) -> dict:
        require(self.cohort is None, "cohort_already_frozen")
        candidates = sorted(
            (a for a, c in self.discovery.items() if c["multi"]),
            key=lambda a: (-self.discovery[a]["multi"], a),
        )[:3]
        failed = sorted(
            (
                a
                for a, c in self.discovery.items()
                if c["failed"] and a not in candidates
            ),
            key=lambda a: (-self.discovery[a]["failed"], a),
        )[:3]
        controls = sorted(
            (
                a
                for a, c in self.discovery.items()
                if c["non_multi_success"] and not c["multi"] and a not in failed
            ),
            key=lambda a: (rank(self.scope["seed"], "actor_control", a), a),
        )[:3]
        self.cohort = {
            "candidate_payers": candidates,
            "failed_fee_burden_payers_not_proven_net_losers": failed,
            "control_payers": controls,
        }
        return {
            "cohort": self.cohort,
            "eligible_actor_count": len(self.discovery),
            "selected_discovery_counts": {
                a: dict(self.discovery[a]) for a in candidates + failed + controls
            },
        }

    def summarize(self, phase: str, features: list[dict], scored: list[dict]) -> dict:  # noqa: C901
        counts = Counter(receipts=len(features))
        cohort = {a for values in (self.cohort or {}).values() for a in values}
        for feature, result in zip(features, scored, strict=True):
            counts["failed" if feature["status"] == 0 else "success"] += 1
            counts[feature["classification"]] += 1
            counts["traced"] += int(result["traced"])
            counts["payer_base_" + result["payer_cashflow"]["base_cashflow_sign"]] += 1
            counts["inventory_changed"] += int(
                bool(
                    result["payer_plus_root_cashflow_not_owned_portfolio"][
                        "non_base_transfer_inventory_raw"
                    ]
                )
            )
            counts["fee_unknown"] += int(feature["known_receipt_fee_raw"] is None)
            for label in (
                "payer_cashflow",
                "payer_plus_root_cashflow_not_owned_portfolio",
            ):
                value = result[label]["observed_base_cashflow_raw"]
                if value is not None:
                    counts[label + "_known_subset_sum_raw"] += value
                    counts[label + "_known_subset_receipts"] += 1
            if feature["known_receipt_fee_raw"] is not None:
                counts["known_fee_components_raw"] += feature["known_receipt_fee_raw"]
                if not feature["status"]:
                    counts["failed_known_fee_burden_raw"] += feature[
                        "known_receipt_fee_raw"
                    ]
            actor = feature["payer"]
            if phase == "discovery":
                self.discovery[actor]["receipts"] += 1
                self.discovery[actor]["multi"] += int(feature["multi_venue"])
                self.discovery[actor]["failed"] += int(not feature["status"])
                self.discovery[actor]["non_multi_success"] += int(
                    feature["status"] == 1 and not feature["multi_venue"]
                )
            elif actor in cohort:
                self.actor_totals[actor]["sampled_receipts"] += 1
                self.actor_totals[actor]["failed"] += int(not feature["status"])
                self.actor_totals[actor]["traced"] += int(result["traced"])
                self.actor_totals[actor][
                    "payer_base_" + result["payer_cashflow"]["base_cashflow_sign"]
                ] += 1
                self.actor_totals[actor]["unknown_fee_receipts"] += int(
                    feature["known_receipt_fee_raw"] is None
                )
                for label in (
                    "payer_cashflow",
                    "payer_plus_root_cashflow_not_owned_portfolio",
                ):
                    value = result[label]["observed_base_cashflow_raw"]
                    if value is not None:
                        self.actor_totals[actor][label + "_known_subset_sum_raw"] += (
                            value
                        )
                        self.actor_totals[actor][label + "_known_subset_receipts"] += 1
                if feature["known_receipt_fee_raw"] is not None:
                    self.actor_totals[actor]["known_fee_components_raw"] += feature[
                        "known_receipt_fee_raw"
                    ]
        self.cumulative[phase].update(counts)
        return {
            "window": dict(counts),
            "cumulative": {p: dict(c) for p, c in self.cumulative.items()},
            "holdout_actor_sampled_totals": {
                a: dict(self.actor_totals[a]) for a in sorted(cohort)
            },
            "unconditional_profit_raw": None,
        }


async def run(scope: dict, credentials: dict, tape: Tape) -> int:  # noqa: C901, PLR0912, PLR0915
    started = time.monotonic()
    deadline = started + scope["duration_seconds"]
    study, completed, attempted = Study(scope), 0, 0
    status, error, v4_topic = "complete_scheduled_study", None, None
    rpc = None
    current_window = None
    tape.put(
        "manifest",
        schema_version=2,
        scope=scope,
        scope_sha256=digest(packed(scope)),
        sources=[*SOURCES, NETWORKS[scope["chain"]]["source"]],
        limitations=LIMITS,
        source_sha256=source_hashes(),
        scheduled_windows=120,
        sample_start_monotonic=started,
        sample_deadline_monotonic=deadline,
        response_limit_bytes=RESPONSE_LIMIT,
        retries=0,
        trace_rule="SHA256(seed|stratum|txhash), ascending; multi/failed/control disjoint",
        cohort_rule="top3 multi counts; top3 failure counts excluding candidates; top3 seeded controls with zero observed multi receipts; address ties",
        raw_format="exact successful HTTP response bytes compressed zlib+base64; original size/SHA256 and bounded decoding; hash-chain; provider error prose omitted",
    )
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            rpc = RPC(
                RecordingSession(session, tape),
                credentials["rpc_url"],
                deadline,
                request_limit=min(scope["request_limit"], 1500),
                request_interval=scope["request_interval_seconds"],
                response_limit=RESPONSE_LIMIT,
            )
            # The shared default collector caps at 1500; this explicitly bounded study allows 5000.
            rpc.request_limit = scope["request_limit"]
            chain = scope["chain"]
            check_chain(await rpc.call("eth_chainId", []), NETWORKS[chain]["chain_id"])
            initial, bank = await rpc.block()
            base = abi_address(
                await rpc.contract(NETWORKS[chain]["quoter"], "0x4aa4a4fc", bank)
            )
            require(base == WRAPPERS[chain], "native_wrapper_identity_mismatch")
            await rpc.code(base, bank)
            await rpc.code(V4_DEPLOYMENTS[chain][0], bank)
            v4_topic = "0x" + (await evm_keccak(rpc, ZERO, V4_SIGNATURE, bank)).hex()
            tape.put(
                "attestation",
                block_hash=initial["hash"],
                wrapped_native=base,
                v4_manager=V4_DEPLOYMENTS[chain][0],
                v4_swap_topic=v4_topic,
                canonical_v3_pools_attested=False,
                v4_keys_hooks_attested=False,
            )
            previous_end = None
            for index in range(120):
                current_window = index
                scheduled = started + index * scope["interval_seconds"]
                await asyncio.sleep(max(0, scheduled - time.monotonic()))
                # Freeze before *any* holdout reads; never include a delayed discovery window.
                if (
                    study.cohort is None
                    and time.monotonic() >= started + scope["discovery_seconds"]
                ):
                    tape.put(
                        "cohort_lock",
                        **study.freeze(),
                        discovery_closed_monotonic=time.monotonic(),
                    )
                if time.monotonic() >= scheduled + scope["interval_seconds"]:
                    tape.put(
                        "unknown_window",
                        window=index,
                        scheduled_monotonic=scheduled,
                        reason="schedule_overrun_no_late_head_substitution",
                    )
                    continue
                phase = "discovery" if index < 20 else "holdout"
                if phase == "discovery" and study.cohort is not None:
                    tape.put(
                        "unknown_window",
                        window=index,
                        scheduled_monotonic=scheduled,
                        reason="discovery_cutoff_passed",
                    )
                    continue
                if phase == "holdout" and study.cohort is None:
                    tape.put(
                        "cohort_lock",
                        **study.freeze(),
                        discovery_closed_monotonic=time.monotonic(),
                    )
                attempted += 1
                latest, _ = await rpc.block()
                end = quantity(latest["number"]) - 2
                first = end - 3
                require(first > 0, "chain_too_short")
                frozen = time.monotonic()
                if frozen >= scheduled + scope["interval_seconds"]:
                    tape.put(
                        "unknown_window",
                        window=index,
                        scheduled_monotonic=scheduled,
                        frozen_monotonic=frozen,
                        reason="head_returned_after_scheduled_window",
                    )
                    continue
                tape.put(
                    "window_plan",
                    window=index,
                    phase=phase,
                    scheduled_monotonic=scheduled,
                    frozen_monotonic=frozen,
                    latest_hash=latest["hash"],
                    latest_number=quantity(latest["number"]),
                    requested_numbers=list(range(first, end + 1)),
                    unsampled_block_gap=None
                    if previous_end is None
                    else max(0, first - previous_end - 1),
                )
                blocks, all_features, system_count = [], [], 0
                parent = await rpc.call("eth_getBlockByNumber", [hex(first - 1), False])
                prior = header(parent)
                require(prior["number"] == first - 1, "parent_number_mismatch")
                exposure_start = prior["timestamp"]
                for number in range(first, end + 1):
                    block = await rpc.call("eth_getBlockByNumber", [hex(number), True])
                    h = header(block)
                    require(
                        h["number"] == number
                        and h["parent_hash"] == prior["hash"]
                        and h["timestamp"] >= prior["timestamp"],
                        "block_parent_linkage",
                    )
                    if number in study.blocks:
                        require(
                            study.blocks[number] == h["hash"], "previous_sample_reorged"
                        )
                        tape.put(
                            "duplicate_sample_block",
                            window=index,
                            number=number,
                            block_hash=h["hash"],
                            independent_observation=False,
                        )
                        blocks.append((h, False))
                        prior = h
                        continue
                    receipts = await rpc.call("eth_getBlockReceipts", [h["hash"]])
                    bor_hash = None
                    if (
                        chain == "polygon"
                        and isinstance(receipts, list)
                        and len(receipts) > len(block["transactions"])
                    ):
                        key = (
                            b"matic-bor-receipt-"
                            + number.to_bytes(8, "big")
                            + raw_hex(h["hash"])
                        )
                        bor_hash = (
                            "0x"
                            + (
                                await evm_keccak(
                                    rpc,
                                    ZERO,
                                    key,
                                    {"blockHash": h["hash"], "requireCanonical": True},
                                )
                            ).hex()
                        )
                    ordinary, system = correlate(block, receipts, chain, bor_hash)
                    system_count += len(system)
                    tape.put(
                        "block_correlated",
                        window=index,
                        number=number,
                        block_hash=h["hash"],
                        ordinary_receipts=len(ordinary),
                        system_receipts=len(system),
                        bor_derived_hash=bor_hash,
                        parent_timestamp=prior["timestamp"],
                        timestamp=h["timestamp"],
                    )
                    for receipt in system:
                        tape.put(
                            "system_receipt_classification",
                            window=index,
                            block_hash=h["hash"],
                            transaction_hash=receipt["transactionHash"],
                            transaction_index=quantity(receipt["transactionIndex"]),
                            classification="bor_derived_system_receipt_no_ordinary_payer",
                            status=quantity(receipt["status"]),
                            ordinary_actor=False,
                            attributable_fee_raw=None,
                            profit_raw=None,
                        )
                    for tx, receipt in ordinary:
                        feature = receipt_features(tx, receipt, chain, v4_topic)
                        all_features.append(feature)
                    blocks.append((h, True))
                    prior = h
                selected, denominator = select_traces(all_features, scope)
                tape.put(
                    "trace_selection",
                    window=index,
                    denominators=denominator,
                    selected=selected,
                    total_ordinary_receipts=len(all_features),
                    system_receipts_excluded=system_count,
                )
                traces = {}
                for txhash in selected:
                    call = await trace_one(rpc, tape, index, txhash, "callTracer")
                    diff = await trace_one(rpc, tape, index, txhash, "prestateTracer")
                    traces[txhash] = (call, diff)
                if (
                    phase == "discovery"
                    and time.monotonic() >= started + scope["discovery_seconds"]
                ):
                    tape.put(
                        "window_abandoned",
                        window=index,
                        phase=phase,
                        reason="discovery_acquisition_overrun",
                        completed_discovery_only=True,
                    )
                    continue
                for h, _ in blocks:
                    recheck = header(
                        await rpc.call(
                            "eth_getBlockByNumber", [hex(h["number"]), False]
                        )
                    )
                    require(
                        recheck["hash"] == h["hash"]
                        and recheck["number"] == h["number"],
                        "canonical_recheck_failed",
                    )
                if (
                    phase == "discovery"
                    and time.monotonic() >= started + scope["discovery_seconds"]
                ):
                    tape.put(
                        "window_abandoned",
                        window=index,
                        phase=phase,
                        reason="discovery_verification_overrun",
                        completed_discovery_only=True,
                    )
                    continue
                scores = []
                for feature in all_features:
                    call, diff = traces.get(feature["transaction_hash"], (None, None))
                    result = score(feature, diff, call, chain)
                    scores.append(result)
                    cohort = {
                        a for values in (study.cohort or {}).values() for a in values
                    }
                    tape.put(
                        "receipt_analysis",
                        window=index,
                        phase=phase,
                        feature=feature,
                        score=result,
                        frozen_actor=feature["payer"] in cohort,
                    )
                for h, is_new in blocks:
                    if is_new:
                        study.blocks[h["number"]] = h["hash"]
                summary = study.summarize(phase, all_features, scores)
                trace_availability = {
                    "selected_transactions": len(selected),
                    "call_traces_available": sum(
                        call is not None for call, _ in traces.values()
                    ),
                    "state_diffs_available": sum(
                        diff is not None for _, diff in traces.values()
                    ),
                    "complete_pairs": sum(
                        call is not None and diff is not None
                        for call, diff in traces.values()
                    ),
                    "missing_halves": sum(call is None for call, _ in traces.values())
                    + sum(diff is None for _, diff in traces.values()),
                }
                complete = tape.put(
                    "window_complete",
                    window=index,
                    phase=phase,
                    summary=summary,
                    trace_denominators=denominator,
                    system_receipts=system_count,
                    trace_availability=trace_availability,
                    selected_blocks=4,
                    unique_new_blocks=sum(new for _, new in blocks),
                    sample_timestamp_start=exposure_start,
                    sample_timestamp_end=prior["timestamp"],
                    sampled_chain_seconds=prior["timestamp"] - exposure_start,
                    exposure_note="union of new parent-to-block timestamp intervals; duplicate intervals excluded offline; same-second blocks still distinct",
                    finished_monotonic=time.monotonic(),
                    canonical_rechecked=True,
                )
                print(
                    json.dumps(
                        {
                            k: complete[k]
                            for k in (
                                "event",
                                "window",
                                "phase",
                                "summary",
                                "unique_new_blocks",
                                "sampled_chain_seconds",
                                "trace_availability",
                            )
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                completed += 1
                previous_end = max(
                    end, previous_end if previous_end is not None else end
                )
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            if completed != 120:
                status = "completed_schedule_with_missing_windows"
    except (Exception, KeyboardInterrupt) as exc:
        status, error = "stopped_partial_no_continuation", failure(exc)
    terminal = tape.put(
        "terminal",
        terminal=True,
        status=status,
        failure=error,
        attempted_windows=attempted,
        completed_windows=completed,
        unknown_or_incomplete_windows=120 - completed,
        last_window=current_window,
        elapsed_seconds=time.monotonic() - started,
        scheduled_duration_seconds=3600,
        unique_complete_blocks=len(study.blocks),
        requests=rpc.requests if rpc else 0,
        http_requests=rpc.http_requests if rpc else 0,
        summary={p: dict(c) for p, c in study.cumulative.items()},
        retained_bytes_before_terminal=tape.size,
        final_prior_sha256=tape.previous,
        complete_actor_history=False,
        unconditional_profit_raw=None,
    )
    print(json.dumps(terminal, separators=(",", ":")), flush=True)
    return 0 if status == "complete_scheduled_study" and completed == 120 else 1


def analyze(path: Path) -> dict:  # noqa: C901, PLR0912, PLR0915
    """Stream exact bytes, replay correlation/selection/accounting, and verify coverage."""
    require(path.stat().st_size <= 512 * 1024 * 1024, "analysis_input_too_large")
    previous, seq, study, manifest, v4_topic = "0" * 64, 0, None, None, None
    terminal, windows, intervals, requests = None, [], [], {}
    pending, latest, last_http_start = None, None, None
    retained_responses = 0
    with path.open("rb") as source:
        for line in source:
            require(
                len(line) <= RESPONSE_LIMIT * 2 and line.endswith(b"\n"),
                "incomplete_or_oversized_tape_line",
            )
            row = json.loads(line)
            checksum = row.pop("sha256")
            require(
                row["seq"] == seq
                and row["previous_sha256"] == previous
                and digest(packed(row)) == checksum,
                "tape_hash_chain_mismatch",
            )
            require(terminal is None, "records_after_terminal")
            previous, seq = checksum, seq + 1
            event = row["event"]
            if event == "manifest":
                require(study is None, "duplicate_manifest")
                manifest = row
                study = Study(scope_check(row["scope"]))
                require(
                    row.get("schema_version") == 2
                    and row["source_sha256"] == source_hashes(),
                    "frozen_replay_source_or_schema_mismatch",
                )
                require(
                    digest(packed(study.scope)) == row["scope_sha256"],
                    "scope_hash_mismatch",
                )
            elif event == "attestation":
                v4_topic = hash32(row["v4_swap_topic"])
                require(
                    row["wrapped_native"] == WRAPPERS[study.scope["chain"]],
                    "wrapper_attestation_mismatch",
                )
            elif event == "rpc_request":
                request = row["request"]
                require(
                    request["id"] not in requests
                    and len(requests) < study.scope["request_limit"],
                    "request_budget_or_duplicate",
                )
                started = row["http_started_monotonic"]
                require(
                    last_http_start is None
                    or started - last_http_start
                    >= study.scope["request_interval_seconds"] - 0.001,
                    "http_pacing_mismatch",
                )
                last_http_start = started
                requests[request["id"]] = request
            elif event == "window_plan":
                require(
                    pending is None and latest is not None,
                    "window_plan_without_closed_predecessor_or_head",
                )
                index, phase = row["window"], row["phase"]
                require(index not in {w["window"] for w in windows}, "duplicate_window")
                require(
                    phase == ("discovery" if index < 20 else "holdout"),
                    "window_phase_mismatch",
                )
                require(
                    phase != "holdout" or study.cohort is not None,
                    "holdout_before_cohort_lock",
                )
                require(
                    row["latest_hash"] == latest["hash"]
                    and row["latest_number"] == latest["number"],
                    "frozen_head_mismatch",
                )
                require(
                    row["scheduled_monotonic"]
                    <= row["frozen_monotonic"]
                    < row["scheduled_monotonic"] + study.scope["interval_seconds"],
                    "head_frozen_outside_window",
                )
                wanted = list(range(latest["number"] - 5, latest["number"] - 1))
                require(row["requested_numbers"] == wanted, "sampling_design_mismatch")
                pending = {
                    "plan": row,
                    "blocks": {},
                    "receipts": {},
                    "headers": {},
                    "features": [],
                    "traces": {},
                    "bor_hashes": {},
                    "correlated": {},
                    "analysis": {},
                    "selected": None,
                    "trace_outcomes": {},
                    "last_trace_seq": -1,
                }
            elif event == "rpc_response" and row["retained"]:
                data = decode_response(row)
                request = row["request"]
                require(
                    requests.get(request["id"]) == request,
                    "response_without_matching_request",
                )
                result = RPC._result(json.loads(data), request)  # noqa: SLF001 - replay the same envelope validator
                retained_responses += 1
                method, params = request["method"], request["params"]
                if method == "eth_getBlockByNumber" and params == ["latest", False]:
                    latest = header(result)
                if pending is not None:
                    if method == "eth_getBlockByNumber" and params[1] is True:
                        h = header(result)
                        require(
                            quantity(params[0]) == h["number"],
                            "requested_block_number_mismatch",
                        )
                        pending["blocks"][h["number"]] = result
                    elif method == "eth_getBlockByNumber" and params[0] != "latest":
                        h = header(result)
                        require(
                            quantity(params[0]) == h["number"],
                            "requested_header_number_mismatch",
                        )
                        pending["headers"].setdefault(h["number"], []).append((seq, h))
                    elif method == "eth_getBlockReceipts":
                        pending["receipts"][hash32(params[0])] = result
                    elif method == "eth_call" and len(params) == 3:
                        key = raw_hex(params[0]["data"])
                        if key.startswith(b"matic-bor-receipt-"):
                            pending["bor_hashes"][key] = hash32(result)
                    elif method == "debug_traceTransaction":
                        pending["traces"].setdefault(params[0], {})[
                            params[1]["tracer"]
                        ] = result
                        pending["last_trace_seq"] = seq
            elif event == "trace_outcome":
                require(
                    pending is not None and row["window"] == pending["plan"]["window"],
                    "trace_outcome_window_mismatch",
                )
                txhash, tracer = row["transaction_hash"], row["tracer"]
                require(
                    txhash in pending["selected"]
                    and tracer in ("callTracer", "prestateTracer"),
                    "unselected_trace_outcome",
                )
                require(
                    tracer not in pending["trace_outcomes"].setdefault(txhash, {}),
                    "duplicate_trace_outcome",
                )
                request = requests[row["request_id"]]
                require(
                    request["method"] == "debug_traceTransaction"
                    and request["params"][0] == txhash
                    and request["params"][1]["tracer"] == tracer,
                    "trace_outcome_request_mismatch",
                )
                present = tracer in pending["traces"].get(txhash, {})
                require(
                    row["status"]
                    == ("available" if present else "missing_optional_trace"),
                    "trace_outcome_status_mismatch",
                )
                require(
                    present or optional_trace_failure(row["failure"]),
                    "fatal_failure_mislabeled_optional",
                )
                pending["trace_outcomes"][txhash][tracer] = row["status"]
                pending["last_trace_seq"] = seq
            elif event == "window_abandoned":
                require(
                    pending is not None
                    and row["window"] == pending["plan"]["window"]
                    and row["phase"] == "discovery",
                    "invalid_abandoned_window",
                )
                require(
                    row["monotonic"] >= manifest["sample_start_monotonic"] + 600,
                    "premature_discovery_abandonment",
                )
                pending = None
            elif event == "block_correlated":
                require(
                    pending is not None and row["window"] == pending["plan"]["window"],
                    "block_window_mismatch",
                )
                number, h = row["number"], row["block_hash"]
                require(
                    number not in study.blocks and number not in pending["correlated"],
                    "duplicate_independent_block",
                )
                block = pending["blocks"][number]
                require(header(block)["hash"] == h, "correlated_block_hash_mismatch")
                bor_hash = row["bor_derived_hash"]
                if bor_hash is not None:
                    key = b"matic-bor-receipt-" + number.to_bytes(8, "big") + raw_hex(h)
                    require(
                        pending["bor_hashes"].get(key) == bor_hash,
                        "bor_hash_without_keccak_evidence",
                    )
                ordinary, system = correlate(
                    block, pending["receipts"][h], study.scope["chain"], bor_hash
                )
                require(
                    len(ordinary) == row["ordinary_receipts"]
                    and len(system) == row["system_receipts"],
                    "receipt_count_mismatch",
                )
                pending["features"].extend(
                    receipt_features(tx, receipt, study.scope["chain"], v4_topic)
                    for tx, receipt in ordinary
                )
                pending["correlated"][number] = row
            elif event == "cohort_lock":
                require(pending is None, "cohort_lock_during_acquisition")
                expected = study.freeze()
                require(
                    expected == {key: row[key] for key in expected},
                    "cohort_recomputation_mismatch",
                )
                require(
                    row["discovery_closed_monotonic"]
                    >= manifest["sample_start_monotonic"] + 600,
                    "premature_cohort_lock",
                )
            elif event == "trace_selection":
                selected, denominator = select_traces(pending["features"], study.scope)
                require(
                    selected == row["selected"] and denominator == row["denominators"],
                    "trace_selection_mismatch",
                )
                pending["selected"] = selected
            elif event == "receipt_analysis":
                feature = row["feature"]
                require(
                    feature["transaction_hash"] not in pending["analysis"],
                    "duplicate_receipt_analysis",
                )
                pending["analysis"][feature["transaction_hash"]] = (
                    feature,
                    row["score"],
                )
            elif event == "window_complete":
                require(
                    pending is not None and row["window"] == pending["plan"]["window"],
                    "window_identity_mismatch",
                )
                wanted = pending["plan"]["requested_numbers"]
                require(set(pending["blocks"]) == set(wanted), "missing_sampled_blocks")
                prior = pending["headers"][wanted[0] - 1][0][1]
                new_intervals = []
                for number in wanted:
                    h = header(pending["blocks"][number])
                    require(
                        h["parent_hash"] == prior["hash"]
                        and h["timestamp"] >= prior["timestamp"],
                        "sample_parent_linkage_mismatch",
                    )
                    recheck_seq, recheck = pending["headers"][number][-1]
                    require(
                        recheck["hash"] == h["hash"]
                        and recheck_seq > pending["last_trace_seq"],
                        "missing_post_trace_canonical_recheck",
                    )
                    if number in study.blocks:
                        require(
                            study.blocks[number] == h["hash"], "previous_sample_reorged"
                        )
                    else:
                        evidence = pending["correlated"][number]
                        require(
                            evidence["parent_timestamp"] == prior["timestamp"]
                            and evidence["timestamp"] == h["timestamp"],
                            "exposure_timestamp_mismatch",
                        )
                        new_intervals.append((prior["timestamp"], h["timestamp"]))
                        study.blocks[number] = h["hash"]
                    prior = h
                require(
                    pending["selected"] is not None
                    and set(pending["trace_outcomes"]) == set(pending["selected"]),
                    "incomplete_selected_trace_outcomes",
                )
                require(
                    all(
                        set(t) == {"callTracer", "prestateTracer"}
                        for t in pending["trace_outcomes"].values()
                    ),
                    "missing_trace_pair_outcome",
                )
                availability = {
                    "selected_transactions": len(pending["selected"]),
                    "call_traces_available": sum(
                        "callTracer" in pair for pair in pending["traces"].values()
                    ),
                    "state_diffs_available": sum(
                        "prestateTracer" in pair for pair in pending["traces"].values()
                    ),
                    "complete_pairs": sum(
                        set(pair) == {"callTracer", "prestateTracer"}
                        for pair in pending["traces"].values()
                    ),
                    "missing_halves": sum(
                        status == "missing_optional_trace"
                        for pair in pending["trace_outcomes"].values()
                        for status in pair.values()
                    ),
                }
                require(
                    availability == row["trace_availability"],
                    "trace_availability_mismatch",
                )
                features, scores = pending["features"], []
                for feature in features:
                    pair = pending["traces"].get(feature["transaction_hash"], {})
                    result = score(
                        feature,
                        pair.get("prestateTracer"),
                        pair.get("callTracer"),
                        study.scope["chain"],
                    )
                    require(
                        pending["analysis"].get(feature["transaction_hash"])
                        == (feature, result),
                        "receipt_analysis_mismatch",
                    )
                    scores.append(result)
                require(
                    len(pending["analysis"]) == len(features), "extra_receipt_analysis"
                )
                summary = study.summarize(row["phase"], features, scores)
                require(
                    summary == row["summary"]
                    and row["unique_new_blocks"] == len(new_intervals),
                    "summary_recomputation_mismatch",
                )
                intervals.extend(new_intervals)
                windows.append(
                    {
                        "window": row["window"],
                        "phase": row["phase"],
                        "summary": summary,
                        "unique_new_block_timestamp_seconds": sum(
                            b - a for a, b in new_intervals
                        ),
                        "trace_denominators": row["trace_denominators"],
                        "trace_availability": availability,
                    }
                )
                pending = None
            elif event == "terminal":
                terminal = row
                require(
                    row["completed_windows"] == len(windows)
                    and row["requests"] == len(requests),
                    "terminal_counts_mismatch",
                )
                require(
                    row["status"] != "complete_scheduled_study" or len(windows) == 120,
                    "terminal_falsely_claims_complete_coverage",
                )
    require(study is not None, "missing_manifest")
    union, end = 0, None
    for start, stop in sorted(intervals):
        union += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return {
        "schema_version": 2,
        "scope": manifest["scope"],
        "tape_final_sha256": previous,
        "tape_bytes": path.stat().st_size,
        "tape_records": seq,
        "terminal_present": terminal is not None,
        "requests": len(requests),
        "retained_responses": retained_responses,
        "terminal": terminal,
        "completed_windows": len(windows),
        "unknown_or_incomplete_windows": 120 - len(windows),
        "unique_complete_blocks": len(study.blocks),
        "sampled_chain_timestamp_union_seconds": union,
        "sampled_chain_span_seconds": max(b for _, b in intervals)
        - min(a for a, _ in intervals)
        if intervals
        else None,
        "cohort": study.cohort,
        "windows": windows,
        "limitations": LIMITS,
        "inter_window_activity": "unknown",
        "complete_actor_history": False,
        "unconditional_profit_raw": None,
    }


def self_check() -> None:  # noqa: C901, PLR0915
    """Small offline accounting/correlation checks; never opens an RPC session."""
    from io import BytesIO  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    a, b, token = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    h, th = "0x" + "aa" * 32, "0x" + "bb" * 32
    receipt = {
        "blockHash": h,
        "blockNumber": "0x1",
        "transactionHash": th,
        "transactionIndex": "0x0",
        "from": a,
        "to": b,
        "status": "0x0",
        "logs": [],
        "gasUsed": "0xa",
        "effectiveGasPrice": "0x2",
        "type": "0x0",
    }
    tx = {
        "hash": th,
        "blockHash": h,
        "blockNumber": "0x1",
        "transactionIndex": "0x0",
        "from": a,
        "to": b,
        "value": "0x0",
    }
    block = {
        "hash": h,
        "parentHash": "0x" + "00" * 32,
        "number": "0x1",
        "timestamp": "0x1",
        "transactions": [tx],
    }
    assert len(correlate(block, [receipt], "robinhood")[0]) == 1
    for invalid in (
        [],
        [receipt, receipt],
        [{**receipt, "transactionHash": "0x" + "cc" * 32}],
        [{**receipt, "transactionIndex": "0x1"}],
    ):
        try:
            correlate(block, invalid, "robinhood")
        except ValueError:
            pass
        else:
            raise AssertionError("incomplete_or_mismatched_receipts_accepted")
    diff = {
        "pre": {a: {"balance": "0x64"}, b: {"balance": "0x7", "nonce": 1}},
        "post": {a: {"balance": "0x50"}, b: {"nonce": 2}},
    }
    assert native_delta(diff, a) == -20 and native_delta(diff, b) == 0
    assert native_delta({"pre": {a: {"balance": "0x7"}}, "post": {}}, a) == -7
    assert native_delta({"pre": {}, "post": {a: {"balance": "0x7"}}}, a) == 7
    assert native_delta({"pre": {a: {}}, "post": {a: {"nonce": 1}}}, a) is None
    feature = receipt_features(tx, receipt, "robinhood", "0x" + "dd" * 32)
    trace = {
        "type": "CALL",
        "from": a,
        "to": b,
        "value": "0x0",
        "error": "execution reverted",
        "beforeEVMTransfers": [
            {"from": a, "to": None, "purpose": "feePayment", "value": "0x1e"}
        ],
        "afterEVMTransfers": [
            {"from": None, "to": a, "purpose": "gasRefund", "value": "0xa"}
        ],
    }
    result = score(feature, diff, trace, "robinhood")
    assert (
        result["known_receipt_fee_raw"] == 20
        and result["payer_cashflow"]["observed_base_cashflow_raw"] == -20
    )
    assert result["nitro_payer_delta_raw_not_added_again"] == -20
    assert result["native_reconciliation"]["reconciled"] is True
    unknown_fee = fee_components({"gasUsed": "0xa"})
    assert unknown_fee["known_receipt_fee_raw"] is None
    feature["transfers"] = [{"token": token, "from": a, "to": b, "amount_raw": 5}]
    inventory = account_cashflow(feature, {a}, diff, trace, "robinhood")
    assert (
        inventory["excluded_from_closed_route"]
        and inventory["profit_qualification"] == "inventory_change"
    )
    assert inventory["unconditional_profit_raw"] is None
    # A wrapper Withdrawal need not emit a burn Transfer (native Polygon receipt).
    wrapper = WRAPPERS["polygon"]
    wrapper_receipt = {
        **receipt,
        "status": "0x1",
        "logs": [
            {
                "address": wrapper,
                "logIndex": "0x0",
                "topics": [
                    TRANSFER,
                    "0x" + "00" * 12 + b[2:],
                    "0x" + "00" * 12 + a[2:],
                ],
                "data": "0x" + f"{40:064x}",
            },
            {
                "address": wrapper,
                "logIndex": "0x1",
                "topics": [
                    "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65",
                    "0x" + "00" * 12 + a[2:],
                ],
                "data": "0x" + f"{40:064x}",
            },
        ],
    }
    wrapping = account_cashflow(
        receipt_features(tx, wrapper_receipt, "polygon", "0x" + "dd" * 32),
        {a},
        diff,
        None,
        "polygon",
    )
    assert wrapping["native_delta_raw_fee_already_included"] == -20
    assert wrapping["observed_base_cashflow_raw"] is None
    assert wrapping["base_cashflow_sign"] == "unknown"
    assert value_edges(
        {
            "type": "CALL",
            "from": a,
            "to": b,
            "value": "0x1",
            "calls": [
                {"type": "DELEGATECALL", "from": a, "to": b, "value": "0x5"},
                {"type": "CALL", "from": b, "to": a, "value": "0x9", "error": "revert"},
            ],
        }
    ) == [{"from": a, "to": b, "amount_raw": 1}]
    body = b'{"jsonrpc":"2.0","id":1,"result":[]}'
    compressed_row = {
        "body_encoding": "zlib+base64",
        "body_base64": base64.b64encode(zlib.compress(body)).decode(),
        "response_bytes": len(body),
        "response_sha256": digest(body),
    }
    assert decode_response(compressed_row) == body
    for bad in (
        zlib.compress(body) + b"trailing",
        zlib.compress(b"x" * (RESPONSE_LIMIT + 1)),
    ):
        try:
            decode_response(
                {**compressed_row, "body_base64": base64.b64encode(bad).decode()}
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid_compression_accepted")
    cohort = Study({"seed": "offline"})
    multi_actors = ["0x" + f"{n:040x}" for n in range(1, 5)]
    for actor in multi_actors:
        cohort.discovery[actor].update(multi=1, non_multi_success=1, receipts=2)
    cohort.discovery[token].update(non_multi_success=1, receipts=1)
    assert cohort.freeze()["cohort"]["control_payers"] == [token]
    assert optional_trace_failure(
        failure(RPCError("debug_traceTransaction", -32601, None))
    )
    assert optional_trace_failure(failure(RPCReadError("rpc_response_too_large")))
    assert not optional_trace_failure(
        failure(RPCReadError("rpc_invalid_response_envelope"))
    )
    assert not optional_trace_failure(
        failure(RPCReadError("rpc_provider_authentication_rejected"))
    )

    async def rejected_trace_parameters() -> None:
        error_body = packed(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "unsupported tracer parameters"},
            }
        )

        class ErrorBody:
            async def readexactly(self, n: int) -> bytes:
                raise asyncio.IncompleteReadError(error_body, n)

        for method in ("debug_traceTransaction", "eth_getBlockByNumber"):
            request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": []}
            recorded = RecordingContent(
                SimpleNamespace(status=200, content=ErrorBody()),
                Tape(BytesIO(), 16 * 1024 * 1024),
                request,
                time.monotonic(),
            )
            try:
                await recorded.readexactly(RESPONSE_LIMIT + 1)
            except asyncio.IncompleteReadError as exc:
                assert method == "debug_traceTransaction" and exc.partial == error_body
                try:
                    RPC._result(json.loads(exc.partial), request)  # noqa: SLF001
                except RPCError as error:
                    assert optional_trace_failure(failure(error))
                else:
                    raise AssertionError("trace_error_not_classified")
            except RPCReadError:
                assert method == "eth_getBlockByNumber"
            else:
                raise AssertionError("provider_error_not_rejected")

    asyncio.run(rejected_trace_parameters())
    print(
        "self_check_passed_receipt_correlation_sparse_diffs_failed_fee_inventory_and_reverted_flows"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--scope", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--analyze", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        require(
            not any((args.credentials, args.scope, args.out, args.analyze)),
            "self_check_exclusive",
        )
        self_check()
        return
    try:
        if args.analyze:
            require(
                not args.credentials and not args.scope,
                "analysis_requires_no_credentials",
            )
            result = analyze(args.analyze)
            if args.out:
                with args.out.open("xb") as out:
                    out.write(packed(result) + b"\n")
            else:
                print(json.dumps(result, separators=(",", ":")))
            return
        require(
            all((args.credentials, args.scope, args.out)),
            "credentials_scope_out_required",
        )
        scope = scope_check(read_json(args.scope))
        credentials = read_json(args.credentials)
        require(
            isinstance(credentials, dict)
            and set(credentials) in ({"rpc_url"}, {"rpc_url", "wss_url"}),
            "provider_only_credentials_required",
        )
        for name, scheme in (("rpc_url", "https"), ("wss_url", "wss")):
            if name not in credentials:
                continue
            require(isinstance(credentials[name], str), "invalid_provider_endpoint")
            parsed = urlsplit(credentials[name])
            require(
                parsed.scheme == scheme
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.fragment,
                "invalid_provider_endpoint",
            )
        require(args.out.suffix == ".jsonl", "output_requires_jsonl")
        with args.out.open("xb") as out:
            result = asyncio.run(
                run(scope, credentials, Tape(out, scope["max_output_bytes"]))
            )
        raise SystemExit(result)
    except (OSError, ValueError, TypeError, KeyError, RPCReadError) as exc:
        parser.exit(2, "receipt_study_rejected_" + type(exc).__name__ + "\n")


if __name__ == "__main__":
    main()
