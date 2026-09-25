"""Bounded, unsigned canonical V3/V4 meme cycles driven by first newHeads.

CLI: --credentials provider.json --scope scope.json --out exclusive.jsonl
Credentials (provider-only JSON): {"rpc_url":"https://...","wss_url":"wss://..."}.
Scope schema v1 (unknown fields rejected): chain (polygon|robinhood), selection_id,
selected_at (Unix seconds), wrapped_native, caller, executor, markets [{token,
pools:[{address,family,factory,fee} or canonical_v4 descriptors]}], amounts_raw
[decimal strings],
minimum_profit_raw, gas_limit (<=300000), gas_price_raw, max_fee_raw,
duration_seconds (1..1800), request_limit (1..1500), request_interval_seconds
(>=0.3), max_notifications (1..10000), max_latency_ms, max_head_age_seconds,
runtime_path (relative to scope), runtime_sha256 (SHA256 of decoded runtime).
canonical_v4 descriptors: {family,pool_id,manager,state_view,currency0,currency1,
fee,tick_spacing,hooks}; only hookless static-fee chain-native/meme + V3 wrapped
native/meme hybrids are admitted. Native zero is ETH on Robinhood, POL on Polygon.
Other families are emitted as unsupported, never priced as negative returns.
V3/V3 pays wrapped native; hybrids pay native after all repayments.
Runtime JSON compiler/settings/source_sha256/runtime/methodIdentifiers contract
is enforced below. Source selection is frozen before any execution results.
No URL, provider message, credentials path or runtime path is written to JSONL.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import re
import time
from itertools import permutations
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit

import aiohttp
from evaluate_evm_capital import (
    NETWORKS,
    RPC,
    ZERO,
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
    from collections.abc import Callable

# ABI widths and protocol/admission bounds are explicit constants below.
# ruff: noqa: PLR2004
READ_FAILURES = (ValueError, TypeError, KeyError, aiohttp.ClientError, TimeoutError)
COMPILER = "0.8.30+commit.73712a01"
SETTINGS = {
    "optimizer": {"enabled": True, "runs": 200},
    "evmVersion": "paris",
    "viaIR": False,
    "metadata": {"bytecodeHash": "none", "appendCBOR": False},
}
RUN = "run(address,address,address,address,uint256,uint256)"
HYBRID = "runHybrid((address,address,address,address,address,bytes32,(address,address,uint24,int24,address),bool,uint256),uint256)"
METHODS = {
    RUN,
    HYBRID,
    "uniswapV3SwapCallback(int256,int256,bytes)",
    "unlockCallback(bytes)",
}
V4_DEPLOYMENTS = {
    "robinhood": (
        "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
    ),
    "polygon": (
        "0x67366782805870060151383f4bbff9dab53e5cd6",
        "0x5ea1bd7974c8a611cbab0bdcafcb1d9cc9b3ba5a",
    ),
}
V4_FIELDS = set(
    "family pool_id manager state_view currency0 currency1 fee tick_spacing hooks".split()
)
# Copies calldata, executes Ethereum KECCAK256, returns one bytes32. No storage/value.
KECCAK_RUNTIME = "0x3660006000373660002060005260206000f3"
SCOPE_KEYS = set(
    "chain selection_id selected_at wrapped_native caller executor markets amounts_raw minimum_profit_raw gas_limit gas_price_raw max_fee_raw duration_seconds request_limit request_interval_seconds max_notifications max_latency_ms max_head_age_seconds runtime_path runtime_sha256".split()
)


def read_json(path: Path) -> object:
    """Read bounded JSON without logging its path or contents."""
    require(path.stat().st_size <= 262144, "input_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


def natural(value: str) -> int:
    """Validate a canonical unsigned decimal ABI integer."""
    require(
        isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,77}", value),
        "invalid_raw_integer",
    )
    result = int(value)
    uint(result)
    return result


def bounded(value: int | float, low: int | float, high: int | float) -> int | float:
    """Validate a finite numeric observation bound, excluding booleans."""
    require(
        type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
        "invalid_observation_bound",
    )
    return value


def digest(value: bytes) -> str:
    """Identify exact source or runtime bytes."""
    return hashlib.sha256(value).hexdigest()


def load(  # noqa: PLR0915 - frozen scope and compiler attestation
    scope_path: Path, credentials_path: Path
) -> tuple[dict, dict, str, dict, str, dict]:
    """Validate frozen inputs, pinned compiler metadata and provider-only access."""
    s = read_json(scope_path)
    require(isinstance(s, dict) and set(s) == SCOPE_KEYS, "invalid_scope_fields")
    require(s["chain"] in NETWORKS, "unsupported_chain")
    require(
        isinstance(s["selection_id"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", s["selection_id"]),
        "invalid_selection_id",
    )
    bounded(s["selected_at"], 1, time.time())
    for name in ("wrapped_native", "caller", "executor"):
        s[name] = address(s[name])
        require(s[name] != ZERO, "zero_scope_identity")
    require(
        len({s["wrapped_native"], s["caller"], s["executor"]}) == 3,
        "scope_identity_collision",
    )
    for name, low, high in (
        ("duration_seconds", 1, 1800),
        ("request_interval_seconds", 0.3, 60),
        ("max_latency_ms", 1, 120000),
        ("max_head_age_seconds", 1, 300),
        ("gas_limit", 21000, 300000),
        ("request_limit", 1, 1500),
        ("max_notifications", 1, 10000),
    ):
        bounded(s[name], low, high)
    for name in ("gas_limit", "request_limit", "max_notifications"):
        require(type(s[name]) is int, "integer_bound_required")
    require(
        isinstance(s["amounts_raw"], list) and 1 <= len(s["amounts_raw"]) <= 12,
        "invalid_amount_count",
    )
    amounts = [natural(a) for a in s["amounts_raw"]]
    cap = 10**16 if s["chain"] == "robinhood" else 10000 * 10**18
    require(
        len(set(amounts)) == len(amounts) and all(0 < a <= cap for a in amounts),
        "input_cap_exceeded",
    )
    for key in ("minimum_profit_raw", "gas_price_raw", "max_fee_raw"):
        natural(s[key])
    require(
        natural(s["gas_price_raw"]) > 0
        and natural(s["max_fee_raw"]) >= s["gas_limit"] * natural(s["gas_price_raw"]),
        "fee_envelope_exceeds_budget",
    )
    require(
        s["chain"] != "robinhood" or natural(s["gas_price_raw"]) <= 85304000,
        "gas_price_cap_exceeded",
    )
    require(
        isinstance(s["markets"], list) and 1 <= len(s["markets"]) <= 8,
        "invalid_market_count",
    )
    tokens, pools = set(), set()
    for market in s["markets"]:
        require(
            isinstance(market, dict) and set(market) == {"token", "pools"},
            "invalid_market_fields",
        )
        market["token"] = token = address(market["token"])
        require(
            token
            not in tokens | {ZERO, s["wrapped_native"], s["caller"], s["executor"]},
            "duplicate_or_invalid_token",
        )
        tokens.add(token)
        require(
            isinstance(market["pools"], list) and 1 <= len(market["pools"]) <= 8,
            "invalid_pool_count",
        )
        for pool in market["pools"]:
            require(isinstance(pool, dict), "invalid_pool_fields")
            family = pool.get("family")
            require(
                isinstance(family, str) and re.fullmatch(r"[a-z0-9_]{1,40}", family),
                "invalid_pool_family",
            )
            if family == "canonical_v4":
                require(set(pool) == V4_FIELDS, "invalid_v4_pool_fields")
                require(len(raw_hex(pool["pool_id"])) == 32, "invalid_pool_id")
                pool["pool_id"] = pool["pool_id"].lower()
                for field in (
                    "manager",
                    "state_view",
                    "currency0",
                    "currency1",
                    "hooks",
                ):
                    pool[field] = address(pool[field])
                require(
                    all(
                        pool[field]
                        not in {
                            ZERO,
                            token,
                            s["wrapped_native"],
                            s["caller"],
                            s["executor"],
                        }
                        for field in ("manager", "state_view")
                    ),
                    "invalid_v4_deployment_identity",
                )
                require(
                    type(pool["fee"]) is int and 0 <= pool["fee"] < 1 << 24,
                    "invalid_pool_fee",
                )
                require(
                    type(pool["tick_spacing"]) is int
                    and -(1 << 23) <= pool["tick_spacing"] < 1 << 23,
                    "invalid_tick_spacing",
                )
                identity = (pool["manager"], pool["pool_id"])
            else:
                require(
                    set(pool) == {"address", "family", "factory", "fee"},
                    "invalid_pool_fields",
                )
                pool["address"], pool["factory"] = (
                    address(pool["address"]),
                    address(pool["factory"]),
                )
                require(
                    pool["address"]
                    not in {
                        ZERO,
                        token,
                        s["wrapped_native"],
                        s["caller"],
                        s["executor"],
                    },
                    "invalid_pool_address",
                )
                require(
                    pool["fee"] is None
                    or (type(pool["fee"]) is int and 0 < pool["fee"] < 1000000),
                    "invalid_pool_fee",
                )
                identity = pool["address"]
            require(identity not in pools, "duplicate_pool")
            pools.add(identity)
    package = read_json(scope_path.parent / s["runtime_path"])
    require(
        package["compiler"] == COMPILER and package["settings"] == SETTINGS,
        "compiler_contract_mismatch",
    )
    source_hash = digest(
        Path(__file__).with_name("simulate_evm_meme_cycle.sol").read_bytes()
    )
    require(package["source_sha256"] == source_hash, "runtime_source_mismatch")
    runtime = raw_hex(package["runtime"])
    require(
        0 < len(runtime) <= 24576 and digest(runtime) == s["runtime_sha256"],
        "runtime_digest_mismatch",
    )
    selectors = package["methodIdentifiers"]
    require(
        isinstance(selectors, dict)
        and set(selectors) == METHODS
        and all(
            isinstance(v, str) and re.fullmatch(r"[0-9a-f]{8}", v)
            for v in selectors.values()
        )
        and len(set(selectors.values())) == len(METHODS),
        "invalid_runtime_method_identifiers",
    )
    credentials = read_json(credentials_path)
    require(
        isinstance(credentials, dict) and set(credentials) == {"rpc_url", "wss_url"},
        "provider_only_credentials_required",
    )
    for key, scheme in (("rpc_url", "https"), ("wss_url", "wss")):
        parsed = urlsplit(credentials[key])
        require(
            parsed.scheme == scheme
            and parsed.hostname
            and not parsed.fragment
            and not parsed.username
            and not parsed.password,
            "invalid_provider_endpoint",
        )
    public = {k: v for k, v in s.items() if k != "runtime_path"}
    return s, credentials, package["runtime"], selectors, source_hash, public


def failure(exc: BaseException) -> dict:
    """Retain fixed error identifiers without provider messages or URLs."""
    # All local ValueErrors have fixed diagnostics, but never trust external exception text.
    result = {"error_type": type(exc).__name__}
    if isinstance(exc, RPCError):
        result["rpc_error"] = str(exc)
        result["revert_data"] = exc.revert_data
    elif type(exc) in (ValueError, RPCReadError) and re.fullmatch(
        r"[A-Za-z0-9_]{1,120}", str(exc)
    ):
        result["reason"] = str(exc)
    return result


def header(value: dict) -> dict:
    """Decode the canonical fields needed to identify one execution bank."""
    require(isinstance(value, dict), "invalid_head")
    for key in ("hash", "parentHash"):
        require(len(raw_hex(value.get(key))) == 32, "invalid_head_hash")
    return {
        "hash": value["hash"].lower(),
        "parent_hash": value["parentHash"].lower(),
        "number": quantity(value.get("number")),
        "timestamp": quantity(value.get("timestamp")),
        "base_fee_raw": quantity(value["baseFeePerGas"])
        if value.get("baseFeePerGas") is not None
        else None,
    }


class Heads:
    """One bounded stream, correlated string subscription, one latest-head slot."""

    def __init__(self, s: dict, deadline: float, record: Callable[..., None]) -> None:
        self.s, self.deadline, self.record = s, deadline, record
        self.pending = None
        self.latest = None
        self.wake = asyncio.Event()
        self.seen = set()
        self.notifications = self.coalesced = self.missed = 0
        self.stop_reason = None
        self.branch_generation = 0

    async def listen(self, session: aiohttp.ClientSession, endpoint: str) -> None:
        """Consume one bounded, correlated head subscription without reconnecting."""
        try:
            async with session.ws_connect(
                endpoint, max_msg_size=65536, heartbeat=20, timeout=10, autoclose=True
            ) as ws:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                    }
                )
                reply = await ws.receive_json(
                    timeout=min(10, max(0.001, self.deadline - time.monotonic()))
                )
                require(
                    isinstance(reply, dict)
                    and reply.get("jsonrpc") == "2.0"
                    and type(reply.get("id")) is int
                    and reply["id"] == 1
                    and "error" not in reply,
                    "subscription_handshake_failed",
                )
                subscription = reply.get("result")
                require(
                    isinstance(subscription, str) and 1 <= len(subscription) <= 256,
                    "subscription_id_must_be_string",
                )
                self.record(
                    "subscription_ready",
                    subscription_kind="newHeads",
                    websocket_requests=1,
                )
                while (
                    time.monotonic() < self.deadline
                    and self.notifications < self.s["max_notifications"]
                ):
                    payload = await ws.receive_json(
                        timeout=max(0.001, self.deadline - time.monotonic())
                    )
                    require(
                        isinstance(payload, dict)
                        and payload.get("jsonrpc") == "2.0"
                        and payload.get("method") == "eth_subscription",
                        "invalid_subscription_envelope",
                    )
                    params = payload.get("params")
                    require(
                        isinstance(params, dict)
                        and params.get("subscription") == subscription,
                        "uncorrelated_subscription",
                    )
                    h = header(params.get("result"))
                    self.notifications += 1
                    if h["hash"] in self.seen:
                        self.record("duplicate_head", head=h)
                        continue
                    self.seen.add(h["hash"])
                    h.update(
                        received_at=time.time(), received_monotonic=time.monotonic()
                    )
                    previous = self.latest
                    gap = (
                        max(0, h["number"] - previous["number"] - 1)
                        if previous
                        else None
                    )
                    parent_matches = (
                        h["parent_hash"] == previous["hash"] if previous else None
                    )
                    if previous is not None and (
                        h["number"] <= previous["number"]
                        or (
                            h["number"] == previous["number"] + 1 and not parent_matches
                        )
                    ):
                        self.branch_generation += 1
                    h["stream_generation"] = self.branch_generation
                    h["first_pending_at"] = h["received_at"]
                    h["first_pending_monotonic"] = h["received_monotonic"]
                    self.missed += gap or 0
                    if self.pending is not None:
                        h["first_pending_at"] = self.pending["first_pending_at"]
                        h["first_pending_monotonic"] = self.pending[
                            "first_pending_monotonic"
                        ]
                        self.coalesced += 1
                        self.record("coalesced_head", head=self.pending, censored=True)
                    self.latest = self.pending = h
                    self.record(
                        "head_notification",
                        head=h,
                        missed_heads=gap,
                        parent_matches_previous=parent_matches,
                    )
                    self.wake.set()
                self.stop_reason = (
                    "notification_limit_reached"
                    if self.notifications >= self.s["max_notifications"]
                    else "deadline_reached"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - terminal stream failure, not a reconnect
            self.stop_reason = (
                "deadline_reached"
                if isinstance(exc, TimeoutError) and time.monotonic() >= self.deadline
                else "stream_unavailable"
            )
            self.record("stream_ended", reason=self.stop_reason, **failure(exc))
        finally:
            self.wake.set()

    async def take(self) -> dict | None:
        """Consume the latest pending head, or report a stopped empty stream."""
        while self.pending is None and self.stop_reason is None:
            self.wake.clear()
            await self.wake.wait()
        if self.stop_reason is not None:
            return None
        h, self.pending = self.pending, None
        return h

    def freshness(self, h: dict) -> dict:
        """Separate first-notification timing from observed branch validity."""
        now = time.monotonic()
        elapsed = (
            now - h.get("first_pending_monotonic", h["received_monotonic"])
        ) * 1000
        age = time.time() - h["timestamp"]
        replaced = (
            self.latest is not None
            and self.latest["hash"] != h["hash"]
            and (
                self.latest["number"] <= h["number"]
                or (
                    self.latest["number"] == h["number"] + 1
                    and self.latest["parent_hash"] != h["hash"]
                )
            )
        )
        branch_valid = (
            self.latest is not None
            and not replaced
            and h.get("stream_generation", 0) == self.branch_generation
        )
        # Descendant heads do not invalidate a timely pinned-bank execution.
        # Eligibility still requires the post-execution canonical-hash RPC check.
        fresh = (
            self.stop_reason is None
            and branch_valid
            and 0 <= elapsed <= self.s["max_latency_ms"]
            and 0 <= age <= self.s["max_head_age_seconds"]
        )
        return {
            "fresh": fresh,
            "first_notification_latency_ms": elapsed,
            "selected_head_latency_ms": (now - h["received_monotonic"]) * 1000,
            "block_age_seconds": age,
            "superseded": self.latest is not None and self.latest["hash"] != h["hash"],
            "stream_live": self.stop_reason is None,
            "stream_branch_valid": branch_valid,
        }


async def execution_context(
    rpc: RPC, s: dict, bank: dict, tokens: set[str], h: dict
) -> tuple[dict, int]:
    """Attest the exact bank, empty override target and caller funding in one POST."""
    executor = s["executor"]
    calls = [
        ("eth_getBlockByNumber", [hex(h["number"]), False]),
        ("eth_getCode", [executor, bank]),
        ("eth_getTransactionCount", [executor, bank]),
        ("eth_getBalance", [executor, bank]),
        ("eth_getTransactionCount", [s["caller"], bank]),
        ("eth_getBalance", [s["caller"], bank]),
    ]
    calls.extend(
        (
            "eth_call",
            [{"to": token, "data": "0x70a08231" + uint(int(executor, 16)).hex()}, bank],
        )
        for token in sorted({s["wrapped_native"], *tokens})
    )
    (
        current,
        code,
        nonce,
        native,
        caller_nonce,
        caller_balance,
        *balances,
    ) = await rpc.batch(calls)
    pinned = header(current)
    require(
        pinned["hash"] == h["hash"] and pinned["parent_hash"] == h["parent_hash"],
        "head_no_longer_canonical",
    )
    require(code == "0x", "executor_has_code")
    require(quantity(nonce) == 0, "executor_has_nonce")
    require(quantity(native) == 0, "executor_native_inventory")
    require(all(abi_int(value) == 0 for value in balances), "executor_token_inventory")
    require(
        quantity(caller_balance) >= natural(s["max_fee_raw"]),
        "caller_gas_funding_unavailable",
    )
    pinned["caller_balance_raw"] = str(quantity(caller_balance))
    return pinned, quantity(caller_nonce)


def v4_key_bytes(p: dict) -> bytes:
    """Encode PoolKey as five static words, including signed int24 extension."""
    return b"".join(
        uint(v)
        for v in (
            int(p["currency0"], 16),
            int(p["currency1"], 16),
            p["fee"],
            p["tick_spacing"] % (1 << 256),
            int(p["hooks"], 16),
        )
    )


def admit_v4(s: dict, token: str, p: dict) -> None:
    """Reject unsupported economics before creating any priced route."""
    require(
        (p["manager"], p["state_view"]) == V4_DEPLOYMENTS[s["chain"]],
        "v4_deployment_mismatch",
    )
    require(
        p["currency0"] == ZERO and p["currency1"] == token, "unsupported_v4_native_pair"
    )
    require(p["hooks"] == ZERO, "unsupported_v4_hooks")
    require(0 <= p["fee"] < 1000000, "unsupported_v4_dynamic_or_full_fee")
    require(0 < p["tick_spacing"] <= 32767, "unsupported_v4_tick_spacing")


async def evm_keccak(rpc: RPC, executor: str, payload: bytes, bank: dict) -> bytes:
    """Use EVM Keccak, not NIST SHA3; the only override is temporary pure code."""
    result = raw_hex(
        await rpc.call(
            "eth_call",
            [
                {
                    "to": executor,
                    "data": "0x" + payload.hex(),
                    "value": "0x0",
                    "gas": "0x186a0",
                },
                bank,
                {executor: {"code": KECCAK_RUNTIME}},
            ],
        )
    )
    require(len(result) == 32, "invalid_evm_keccak_result")
    return result


async def attest(rpc: RPC, s: dict, record: Callable[..., None]) -> list[dict]:  # noqa: C901, PLR0912, PLR0915 - canonical per-venue admission
    """Admit code-backed canonical V3 and hookless chain-native V4 pool pairs."""
    network = NETWORKS[s["chain"]]
    check_chain(await rpc.call("eth_chainId", []), network["chain_id"])
    h, bank = await rpc.block()
    for deployment in (network["factory"], network["quoter"], s["wrapped_native"]):
        await rpc.code(deployment, bank)
    require(
        abi_address(await rpc.contract(network["quoter"], "0xc45a0155", bank))
        == network["factory"],
        "canonical_factory_mismatch",
    )
    require(
        abi_address(await rpc.contract(network["quoter"], "0x4aa4a4fc", bank))
        == s["wrapped_native"],
        "native_wrapper_mismatch",
    )
    require(
        network["wrapped_native"] in (None, s["wrapped_native"]),
        "native_wrapper_identity_mismatch",
    )
    require(
        abi_int(await rpc.contract(s["wrapped_native"], "0x313ce567", bank), 8) == 18,
        "native_wrapper_decimals",
    )
    routes = []
    state_selectors = {}
    for market in s["markets"]:
        token, supported = market["token"], []
        for p in market["pools"]:
            if p["family"] not in {"canonical_v3", "canonical_v4"}:
                record(
                    "pool_scope_outcome",
                    token=token,
                    pool=p,
                    status="unsupported_pool_family",
                    execution_supported=False,
                )
                continue
            try:
                if p["family"] == "canonical_v4":
                    admit_v4(s, token, p)
                    for deployment in (token, p["manager"], p["state_view"]):
                        await rpc.code(deployment, bank)
                    pool_hash = await evm_keccak(
                        rpc, s["executor"], v4_key_bytes(p), bank
                    )
                    require(
                        pool_hash.hex() == p["pool_id"][2:], "v4_pool_key_hash_mismatch"
                    )
                    if not state_selectors:
                        for method in ("poolManager()", "getSlot0(bytes32)"):
                            state_selectors[method] = (
                                await evm_keccak(
                                    rpc, s["executor"], method.encode(), bank
                                )
                            )[:4].hex()
                    require(
                        abi_address(
                            await rpc.contract(
                                p["state_view"],
                                "0x" + state_selectors["poolManager()"],
                                bank,
                            )
                        )
                        == p["manager"],
                        "v4_state_manager_mismatch",
                    )
                    slot = raw_hex(
                        await rpc.contract(
                            p["state_view"],
                            "0x"
                            + state_selectors["getSlot0(bytes32)"]
                            + pool_hash.hex(),
                            bank,
                        )
                    )
                    require(len(slot) == 128, "invalid_v4_slot0")
                    price = abi_int("0x" + slot[:32].hex(), 160)
                    fee = abi_int("0x" + slot[96:].hex(), 24)
                    require(
                        price > 0 and fee == p["fee"],
                        "v4_uninitialized_or_fee_mismatch",
                    )
                    supported.append(p)
                    record(
                        "pool_scope_outcome",
                        token=token,
                        pool=p,
                        status="attested_canonical_v4",
                        execution_supported=True,
                        bank=bank,
                        sqrt_price_x96=str(price),
                        pool_key_hash=p["pool_id"],
                    )
                    continue
                require(
                    p["factory"] == network["factory"] and p["fee"] is not None,
                    "noncanonical_factory_or_fee",
                )
                await rpc.code(token, bank)
                decimals = abi_int(await rpc.contract(token, "0x313ce567", bank), 8)
                await rpc.code(p["address"], bank)
                t0 = abi_address(await rpc.contract(p["address"], "0x0dfe1681", bank))
                t1 = abi_address(await rpc.contract(p["address"], "0xd21220a7", bank))
                require({t0, t1} == {token, s["wrapped_native"]}, "pool_pair_mismatch")
                require(
                    abi_address(await rpc.contract(p["address"], "0xc45a0155", bank))
                    == p["factory"],
                    "pool_factory_mismatch",
                )
                require(
                    abi_int(await rpc.contract(p["address"], "0xddca3f43", bank), 24)
                    == p["fee"],
                    "pool_fee_mismatch",
                )
                lookup = (
                    "0x1698ee82"
                    + (uint(int(t0, 16)) + uint(int(t1, 16)) + uint(p["fee"], 24)).hex()
                )
                require(
                    abi_address(await rpc.contract(p["factory"], lookup, bank))
                    == p["address"],
                    "factory_pool_mismatch",
                )
                supported.append(p)
                record(
                    "pool_scope_outcome",
                    token=token,
                    pool=p,
                    status="attested_canonical_v3",
                    execution_supported=True,
                    bank=bank,
                    token0=t0,
                    token1=t1,
                    token_decimals=decimals,
                )
            except (BoundReached, RPCReadError):
                raise
            except READ_FAILURES as exc:
                record(
                    "pool_scope_outcome",
                    token=token,
                    pool=p,
                    status="attestation_failed",
                    execution_supported=False,
                    bank=bank,
                    **failure(exc),
                )
        market_routes = []
        for entry, exit_pool in permutations(supported, 2):
            if entry["family"] == exit_pool["family"] == "canonical_v3":
                market_routes.append(
                    {
                        "token": token,
                        "entry": entry["address"],
                        "exit": exit_pool["address"],
                    }
                )
            elif entry["family"] != exit_pool["family"]:
                v4_first = entry["family"] == "canonical_v4"
                v4, v3 = (entry, exit_pool) if v4_first else (exit_pool, entry)
                market_routes.append(
                    {
                        "token": token,
                        "entry": entry.get("address", entry.get("pool_id")),
                        "exit": exit_pool.get("address", exit_pool.get("pool_id")),
                        "v3": v3,
                        "v4": v4,
                        "v4_first": v4_first,
                    }
                )
        routes.extend(market_routes)
        if not market_routes:
            record(
                "market_coverage_gap",
                token=token,
                reason="no_attested_supported_pool_pair",
            )
    record("scope_attested", bank=bank, head=header(h), route_count=len(routes))
    return routes


async def canonical(rpc: RPC, h: dict) -> dict:
    """Verify that the selected block hash and parent remain canonical."""
    current = header(await rpc.call("eth_getBlockByNumber", [hex(h["number"]), False]))
    require(
        current["hash"] == h["hash"] and current["parent_hash"] == h["parent_hash"],
        "head_no_longer_canonical",
    )
    return current


def complete_net(gross: int, gas: int, price: int, cap: int) -> tuple[int, int]:
    """Subtract envelope gas from post-repayment payout, not principal twice."""
    require(type(gas) is int and 21000 < gas <= cap, "whole_envelope_gas_invalid")
    return gas * price, gross - gas * price


async def evaluate(  # noqa: C901, PLR0913, PLR0915 - one pinned native execution boundary
    rpc: RPC,
    s: dict,
    runtime: str,
    selectors: dict,
    route: dict,
    amount: int,
    bank: dict,
    heads: Heads,
    h: dict,
    record: Callable[..., None],
    caller_nonce: int,
) -> dict:
    """Simulate the full cycle, then price its exact unsigned envelope or abstain."""
    if "v4" in route:
        p = route["v4"]
        words = [
            int(s["wrapped_native"], 16),
            int(route["token"], 16),
            int(route["v3"]["address"], 16),
            int(p["manager"], 16),
            int(p["state_view"], 16),
            int(p["pool_id"], 16),
            int(p["currency0"], 16),
            int(p["currency1"], 16),
            p["fee"],
            p["tick_spacing"],
            int(p["hooks"], 16),
            int(route["v4_first"]),
            amount,
            natural(s["minimum_profit_raw"]),
        ]
        selector = selectors[HYBRID]
    else:
        words = [
            int(s["wrapped_native"], 16),
            int(route["token"], 16),
            int(route["entry"], 16),
            int(route["exit"], 16),
            amount,
            natural(s["minimum_profit_raw"]),
        ]
        selector = selectors[RUN]
    tx = {
        "from": s["caller"],
        "to": s["executor"],
        "data": "0x" + selector + b"".join(uint(w) for w in words).hex(),
        "value": "0x0",
        "gas": hex(s["gas_limit"]),
        "gasPrice": hex(natural(s["gas_price_raw"])),
        "nonce": hex(caller_nonce),
        "chainId": hex(NETWORKS[s["chain"]]["chain_id"]),
        "type": "0x0",
    }
    override = {s["executor"]: {"code": runtime}}
    row = {
        "route": route,
        "amount_raw": str(amount),
        "bank": bank,
        "head": h,
        "transaction": tx,
        "override": {s["executor"]: {"code_sha256": s["runtime_sha256"]}},
        "execution_started_at": time.time(),
        "gross_payout_raw": None,
        "payout_currency": "native" if "v4" in route else "wrapped_native",
        "fee_raw": None,
        "net_raw": None,
        "execution_complete": False,
        "fee_status": "not_attempted",
    }
    try:
        row["gross_payout_raw"] = str(
            abi_int(await rpc.call("eth_call", [tx, bank, override]))
        )
        row["execution_complete"] = True
        row["execution_finished_at"] = time.time()
        row["immediate_freshness"] = heads.freshness(h)
        try:
            require(
                h.get("base_fee_raw") is not None
                and natural(s["gas_price_raw"]) >= h["base_fee_raw"],
                "pinned_base_fee_context_unavailable_or_underpriced",
            )
            # Check that this provider actually applies code overrides to estimation.
            # A control success means estimateGas silently ignored override semantics.
            control = {s["executor"]: {"code": "0x60006000fd"}}
            honors_override = False
            try:
                await rpc.call("eth_estimateGas", [tx, bank, control])
            except RPCError as exc:
                honors_override = exc.revert_data is not None
                row["estimate_control"] = failure(exc)
            require(honors_override, "estimate_override_semantics_unproven")
            gas = quantity(await rpc.call("eth_estimateGas", [tx, bank, override]))
            fee, net = complete_net(
                int(row["gross_payout_raw"]),
                gas,
                natural(s["gas_price_raw"]),
                s["gas_limit"],
            )
            row.update(
                whole_envelope_estimated_gas=gas,
                fee_raw=str(fee),
                net_raw=str(net),
                fee_status="complete_envelope_estimate_not_realized_fee",
                posting_cost_included=s["chain"] == "robinhood",
                fee_semantics="Nitro_eth_estimateGas_includes_parent_posting"
                if s["chain"] == "robinhood"
                else "Polygon_whole_transaction_estimate",
            )
        except BoundReached:
            raise
        except READ_FAILURES as exc:
            row.update(
                fee_status="exact_override_envelope_pricing_unavailable",
                fee_error=failure(exc),
            )
        await canonical(rpc, h)
        row["canonical_after_execution"] = True
        row["status"] = "complete_unsigned_cycle"
    except BoundReached:
        row["status"] = "observation_bound_censored"
        raise
    except RPCReadError as exc:
        row.update(status="fatal_rpc_failure", **failure(exc))
        raise
    except READ_FAILURES as exc:
        row.update(status="execution_rejected_or_coverage_missing", **failure(exc))
        row.setdefault("immediate_freshness", heads.freshness(h))
        if (
            isinstance(exc, RPCError)
            and str(exc) == "rpc_eth_call_error_3"
            and exc.revert_data is not None
        ):
            try:
                await canonical(rpc, h)
                row["canonical_after_execution"] = True
                row["definitive_native_rejection"] = True
            except BoundReached:
                raise
            except (ValueError, aiohttp.ClientError, TimeoutError) as check_error:
                row["canonical_check_error"] = failure(check_error)
    finally:
        row["finished_at"] = time.time()
        row["freshness"] = heads.freshness(h)
        row["opportunity_eligible"] = (
            row.get("canonical_after_execution", False)
            and row["freshness"]["stream_live"]
            and row["freshness"]["stream_branch_valid"]
            and row.get("immediate_freshness", {}).get("fresh", False)
            and row["net_raw"] is not None
            and int(row["net_raw"]) > 0
        )
        record("native_cycle", **row)
    return row


async def run(  # noqa: C901, PLR0912, PLR0913, PLR0915 - one supervised observation lifecycle
    s: dict,
    credentials: dict,
    runtime: str,
    selectors: dict,
    source_hash: str,
    public: dict,
    out: TextIO,
) -> int:
    """Observe bounded native cycles without submitting or inventing fills."""
    identity = {
        "schema_version": 1,
        "chain": s["chain"],
        "chain_id": NETWORKS[s["chain"]]["chain_id"],
        "scope_sha256": digest(
            json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
        ),
        "selection_id": s["selection_id"],
        "source_sha256": source_hash,
        "signed_transactions": 0,
        "submitted_transactions": 0,
    }

    def record(event: str, **fields: object) -> None:
        emit(out, event, **identity, **fields)

    record(
        "observation_started",
        scope=public,
        compiler=COMPILER,
        compiler_settings=SETTINGS,
        observer_source_sha256=digest(Path(__file__).read_bytes()),
        simulation_gas_cap=300000,
        transport_source_sha256=digest(
            Path(__file__).with_name("evaluate_evm_capital.py").read_bytes()
        ),
        websocket_request_limit=1,
        fee_model="exact_code_override_envelope_estimate_or_unknown",
        limitations=[
            "unsigned simulations are not fills or deployment readiness",
            "no observer service costs allocated here",
            "one shared-capital episode across all routes and sizes",
            "unknown pre-window head coverage",
            "canonical_v3_pairs_and_hookless_chain_native_v4_v3_hybrids_only",
            "compiler artifact provenance supplied by packager; compilation not repeated",
        ],
    )
    deadline = time.monotonic() + s["duration_seconds"]
    heads = Heads(s, deadline, record)
    rpc = None
    episode = None
    episode_count = 0
    last_ineligible_hash = None
    evaluated = 0
    stop = "deadline_reached"
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15), trust_env=False
    ) as session:
        rpc = RPC(
            session,
            credentials["rpc_url"],
            deadline,
            request_limit=s["request_limit"],
            request_interval=s["request_interval_seconds"],
        )
        stream = asyncio.create_task(heads.listen(session, credentials["wss_url"]))
        try:
            async with asyncio.timeout(s["duration_seconds"]):
                routes = await attest(rpc, s, record)
                require(routes, "no_execution_supported_routes")
                candidates = [
                    (route, natural(amount))
                    for route in routes
                    for amount in s["amounts_raw"]
                ]
                cursor = 0
                expected = len(candidates)
                while time.monotonic() < deadline:
                    h = await heads.take()
                    if h is None:
                        stop = heads.stop_reason or "stream_ended"
                        break
                    bank = {"blockHash": h["hash"], "requireCanonical": True}
                    rows = []
                    try:
                        pinned_head, caller_nonce = await execution_context(
                            rpc, s, bank, {r["token"] for r in routes}, h
                        )
                        h["base_fee_raw"] = pinned_head.get("base_fee_raw")
                        h["caller_balance_raw"] = pinned_head["caller_balance_raw"]
                        for _ in range(expected):
                            if not heads.freshness(h)["fresh"]:
                                break
                            route, amount = candidates[cursor]
                            # Continue at the next candidate on the next bank, without
                            # extending the first-pending deadline or closing a gap.
                            cursor = (cursor + 1) % expected
                            rows.append(
                                await evaluate(
                                    rpc,
                                    s,
                                    runtime,
                                    selectors,
                                    route,
                                    amount,
                                    bank,
                                    heads,
                                    h,
                                    record,
                                    caller_nonce,
                                )
                            )
                    except BoundReached:
                        raise
                    except READ_FAILURES as exc:
                        record("head_coverage_error", head=h, **failure(exc))
                    evaluated += len(rows)
                    positive = [r for r in rows if r["opportunity_eligible"]]
                    complete = len(rows) == expected and all(
                        r.get("canonical_after_execution")
                        and r.get("immediate_freshness", {}).get("fresh", False)
                        and r.get("freshness", {}).get("stream_live", False)
                        and r.get("freshness", {}).get("stream_branch_valid", False)
                        and (
                            r["net_raw"] is not None
                            or r.get("definitive_native_rejection", False)
                        )
                        for r in rows
                    )
                    if complete and not positive:
                        last_ineligible_hash = h["hash"]
                    if episode and complete and not positive:
                        record(
                            "capital_episode_closed",
                            episode=episode,
                            right_censored=False,
                            fills=0,
                        )
                        episode = None
                    elif episode and (
                        not complete or h["parent_hash"] != episode["last_hash"]
                    ):
                        episode["coverage_censored"] = True
                    if positive:
                        best = max(positive, key=lambda row: int(row["net_raw"]))
                        if episode is None:
                            episode_count += 1
                            episode = {
                                "id": episode_count,
                                "first_hash": h["hash"],
                                "first_notification_at": h["received_at"],
                                "left_censored": last_ineligible_hash
                                != h["parent_hash"],
                                "states": 0,
                            }
                        episode.update(
                            last_hash=h["hash"],
                            states=episode["states"] + 1,
                            best_state_net_raw=best["net_raw"],
                            shared_capital_raw=best["amount_raw"],
                        )
                        record(
                            "capital_episode_observed",
                            episode=episode,
                            fills=0,
                            income_rate=None,
                        )
                    record(
                        "head_evaluated",
                        head=h,
                        attempted=len(rows),
                        expected=expected,
                        unattempted=expected - len(rows),
                        coverage_complete=complete,
                        positive_states=len(positive),
                        freshness=heads.freshness(h),
                    )
        except BoundReached as exc:
            stop = str(exc)
        except TimeoutError:
            stop = "deadline_reached"
        except Exception as exc:  # noqa: BLE001 - terminal failure, never a per-head fallback
            stop = "observation_failed"
            record("observation_error", **failure(exc))
        finally:
            stream.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream
            if episode:
                record(
                    "capital_episode_closed",
                    episode=episode,
                    right_censored=True,
                    fills=0,
                )
            if heads.pending:
                record("unprocessed_head", head=heads.pending, censored=True)
            record(
                "observation_finished",
                reason=stop,
                http_requests=rpc.http_requests,
                rpc_methods_requested=rpc.requests,
                websocket_requests=1,
                notifications=heads.notifications,
                coalesced_heads=heads.coalesced,
                known_missed_heads=heads.missed,
                evaluated_cycles=evaluated,
                independent_capital_episodes=episode_count,
                fills=0,
                execution_net_per_day=None,
                income_claim=None,
            )
    return int(stop != "deadline_reached")


def self_check() -> None:
    """Offline trust/accounting guard check; never opens a session."""
    require(
        complete_net(100000, 25000, 2, 300000) == (50000, 50000),
        "principal_subtracted_twice",
    )
    require(
        complete_net(100, 25000, 2, 300000)[1] == -49900, "fees_did_not_produce_loss"
    )
    for gas in (21000, 300001):
        try:
            complete_net(100000, gas, 2, 300000)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid_whole_envelope_estimate_accepted")
    error = RPCError("eth_call", 3, {"message": "https://secret.invalid/key"})
    require(failure(error)["revert_data"] is None, "provider_message_leaked")
    s = {"max_latency_ms": 1000, "max_head_age_seconds": 10}
    heads = Heads(s, time.monotonic() + 1, lambda *_a, **_k: None)
    h = {
        "hash": "a",
        "number": 100,
        "timestamp": time.time(),
        "received_monotonic": time.monotonic(),
    }
    heads.latest = h
    require(heads.freshness(h)["fresh"], "current_head_not_fresh")
    heads.latest = {"hash": "b", "number": 101, "parent_hash": "a"}
    require(heads.freshness(h)["fresh"], "descendant_invalidated_current_bank")
    heads.latest = {"hash": "replacement", "number": 100}
    require(not heads.freshness(h)["fresh"], "replaced_head_accepted")
    heads.latest = h
    h["received_monotonic"] -= 2
    require(not heads.freshness(h)["fresh"], "expired_head_accepted")
    print("EVM native observer self-check passed")


def main() -> None:
    """Run the explicit scope or offline checks; never load ambient credentials."""
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
        s, credentials, runtime, selectors, source_hash, public = load(
            args.scope, args.credentials
        )
        with args.out.open("x", encoding="utf-8") as out:
            status = asyncio.run(
                run(s, credentials, runtime, selectors, source_hash, public, out)
            )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, "input_or_output_rejected_" + type(exc).__name__ + "\n")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
