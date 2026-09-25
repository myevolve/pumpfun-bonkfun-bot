"""Measure read-only, same-block Uniswap V3 native capital frontiers; never trade.

Examples (new output paths are required):
  python evaluate_evm_capital.py --chain polygon --minutes 2 --out polygon.jsonl
  python evaluate_evm_capital.py --chain robinhood --token 0x... --out robinhood.jsonl
  python evaluate_evm_capital.py --self-check

Counterparts are addresses, not symbol-based asset identities. Every round trip
uses different fee-tier pools in one QuoterV2 call. Quoter callbacks revert their
swaps: these are quotes, NOT atomic execution simulations or successful fills.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
from decimal import Decimal, localcontext
from itertools import permutations
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

# ABI selectors and network identifiers below are protocol constants.
# ruff: noqa: PLR2004
NETWORKS = {
    "polygon": {
        "chain_id": 137,
        "rpc": "https://polygon.drpc.org",
        "native_symbol": "POL",
        "factory": "0x1f98431c8ad98523631ae4a59f267346ea31f984",
        "quoter": "0x61ffe014ba17989e743c5f6cb21bf9697530b21e",
        "wrapped_native": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
        "tokens": ["0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"],
        "sizes": "1,10,100,1000,10000",
        "source": "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-polygon-deployments",
    },
    "robinhood": {
        "chain_id": 4663,
        "rpc": "https://rpc.mainnet.chain.robinhood.com",
        "native_symbol": "ETH",
        "factory": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        "quoter": "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
        "wrapped_native": None,
        "tokens": [],
        "sizes": "0.01,0.1,1,10,100",
        "source": "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments",
    },
}
ZERO = "0x" + "00" * 20
FEES = (100, 500, 3000, 10000)
READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBlockReceipts",
        "debug_traceTransaction",
        "eth_getLogs",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getCode",
        "eth_call",
        "eth_estimateGas",
        "eth_gasPrice",
    }
)
REQUEST_LIMIT = 1500
REQUEST_INTERVAL = 0.3
RESPONSE_LIMIT = 2 * 1024 * 1024
FEE_MODEL = {
    "kind": "complete_quoter_transaction_cost_proxy_not_execution_cost",
    "gas": "ceil(eth_estimateGas(exact_quoter_calldata, pinned_hash) * 1.25)",
    "price": "max(ceil(observed_latest_eth_gasPrice * 1.25), pinned_baseFeePerGas * 2)",
    "robinhood": "Nitro eth_estimateGas includes L2 execution and parent-chain data buffer",
    "source": "https://docs.arbitrum.io/arbitrum-essentials/how-to-estimate-gas",
    "missing_estimate_or_price": "fee_raw and net_raw are null",
}
LIMITATIONS = [
    "Quote-only/native-quoter; no signed or submitted transactions and no fill evidence.",
    "Fixed counterpart addresses and standard fee tiers only; not the complete market universe.",
    "Pools and token decimals are attested at discovery; pools created later are excluded.",
    "EIP-1898 blockHash with requireCanonical is mandatory; no latest/number fallback.",
    "Native capital is modeled as wrapped native (18 decimals); no wrapping or inventory closure occurs.",
    "No token symbol, stock identity, redemption, transfer-tax or transfer restrictions inferred.",
    "Quoter gasEstimate is swap-internal gas, never used as the transaction fee estimate.",
    "Buffered unsigned zero-sender quote-transaction fee is not guaranteed execution cost; wrapping, approvals, router/atomic executor, failures and MEV are not modeled.",
    "Positive episodes are observed states, not independent fills; overlapping sizes cannot be summed.",
    "No funding recommendation or $1000/day claim without execution, survival and held-out frequency evidence.",
]


def require(condition: bool, message: str) -> None:  # noqa: FBT001
    """Reject untrusted or unsupported state with a nonsecret diagnostic."""
    if not condition:
        raise ValueError(message)


def uint(value: int, bits: int = 256) -> bytes:
    """Encode exactly one ABI word, refusing overflow and negative values."""
    require(type(value) is int and 0 <= value < 1 << bits, "abi_integer_out_of_bounds")
    return value.to_bytes(32, "big")


def address(value: str) -> str:
    require(
        isinstance(value, str)
        and re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is not None,
        "invalid_address",
    )
    return value.lower()


def raw_hex(value: str) -> bytes:
    require(
        isinstance(value, str)
        and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value) is not None,
        "invalid_rpc_hex_data",
    )
    return bytes.fromhex(value[2:])


def quantity(value: str) -> int:
    require(
        isinstance(value, str)
        and re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})", value) is not None,
        "invalid_rpc_quantity",
    )
    return int(value, 16)


def abi_int(value: str, bits: int = 256) -> int:
    data = raw_hex(value)
    require(len(data) == 32, "invalid_abi_word_length")
    result = int.from_bytes(data, "big")
    uint(result, bits)
    return result


def abi_address(value: str) -> str:
    return "0x" + abi_int(value, 160).to_bytes(20, "big").hex()


def check_chain(value: str, expected: int) -> None:
    require(quantity(value) == expected, "chain_id_mismatch")


def sizes_raw(value: str) -> list[int]:
    parts = value.split(",")
    require(1 <= len(parts) <= 12, "sizes_count_must_be_1_to_12")
    result = []
    for part in map(str.strip, parts):
        require(
            len(part) <= 80
            and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,18})?", part) is not None,
            "sizes_require_positive_decimals_with_at_most_18_places",
        )
        with localcontext() as context:
            context.prec = 100
            amount = int(Decimal(part) * Decimal(10**18))
        uint(amount, 255)  # QuoterV2 casts each exact-input amount to int256.
        require(amount > 0, "size_must_be_positive")
        result.append(amount)
    require(len(set(result)) == len(result), "duplicate_sizes")
    return sorted(result)


def quote_data(base: str, token: str, buy: int, sell: int, amount: int) -> str:
    require(buy != sell and base != token, "route_requires_distinct_pools")
    uint(amount, 255)
    require(amount > 0, "size_must_be_positive")
    path = (
        bytes.fromhex(base[2:])
        + uint(buy, 24)[-3:]
        + bytes.fromhex(token[2:])
        + uint(sell, 24)[-3:]
        + bytes.fromhex(base[2:])
    )
    # quoteExactInput(bytes,uint256): dynamic bytes offset, amount, length, bytes.
    return (
        "0xcdca1753"
        + (
            uint(64) + uint(amount) + uint(len(path)) + path + bytes((-len(path)) % 32)
        ).hex()
    )


def decode_quote(value: str) -> tuple[int, int]:
    data = raw_hex(value)
    require(len(data) == 320, "quoter_return_not_two_hop_abi")
    words = [int.from_bytes(data[n : n + 32], "big") for n in range(0, len(data), 32)]
    require(
        words[1] == 128 and words[2] == 224 and words[4] == words[7] == 2,
        "invalid_quoter_array_offsets_or_lengths",
    )
    for item in words[5:7]:
        uint(item, 160)
    for item in words[8:10]:
        uint(item, 32)
    require(words[3] > 0, "missing_quoter_swap_gas")
    return words[0], words[3]


def net_amount(amount: int, output: int, fee: int | None) -> int | None:
    return None if fee is None else output - amount - fee


def emit(out: Any, event: str, **fields: Any) -> None:  # noqa: ANN401
    row = json.dumps(
        {"event": event, "at": time.time(), **fields}, separators=(",", ":")
    )
    print(row, flush=True)
    out.write(row + "\n")
    out.flush()


class BoundReached(ValueError):  # noqa: N818
    """Normal observation-bound termination that preserves partial coverage."""


class RPCReadError(RuntimeError):
    """Fatal transport/protocol failure, never an execution revert or retry signal."""


class RPCError(ValueError):
    """Sanitized provider failure; bounded hexadecimal revert bytes only."""

    def __init__(self, method: str, code: int | str, data: object) -> None:
        super().__init__(f"rpc_{method}_error_{code}")
        for _ in range(3):
            if not isinstance(data, dict):
                break
            data = data.get("data", data.get("originalError"))
        self.revert_data = (
            data.lower()
            if isinstance(data, str)
            and len(data) <= 2050
            and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", data)
            else None
        )


class RPC:
    """One paced, bounded transport; no retries, endpoint logging or write methods."""

    def __init__(  # noqa: PLR0913 - independently bounded transport resources
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        deadline: float,
        *,
        request_limit: int = REQUEST_LIMIT,
        request_interval: float = REQUEST_INTERVAL,
        response_limit: int = RESPONSE_LIMIT,
    ) -> None:
        self.session = session
        self.endpoint = endpoint
        self.deadline = deadline
        self.requests = 0
        self.http_requests = 0
        self.last_request = 0.0
        require(
            type(request_limit) is int and 1 <= request_limit <= REQUEST_LIMIT,
            "invalid_request_limit",
        )
        require(
            math.isfinite(request_interval) and request_interval >= REQUEST_INTERVAL,
            "invalid_request_interval",
        )
        require(
            type(response_limit) is int and 1 <= response_limit <= 16 * 1024 * 1024,
            "invalid_response_limit",
        )
        self.response_limit = response_limit
        self.request_limit = request_limit
        self.request_interval = request_interval
        self.lock = asyncio.Lock()

    async def call(self, method: str, params: list) -> Any:  # noqa: ANN401
        """Read one method through the same budget and response checks as batches."""
        require(method in READ_METHODS, "rpc_method_not_read_only")
        async with self.lock:
            request = {
                "jsonrpc": "2.0",
                "id": self.requests + 1,
                "method": method,
                "params": params,
            }
            return self._result(await self._call(request, 1, method), request)

    async def batch(self, calls: list[tuple[str, list]]) -> list[Any]:
        """Read one bank in one HTTP round trip; charge every method to the budget."""
        require(0 < len(calls) <= 32, "rpc_batch_size")
        require(
            all(method in READ_METHODS for method, _ in calls),
            "rpc_method_not_read_only",
        )
        async with self.lock:
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": self.requests + index + 1,
                    "method": method,
                    "params": params,
                }
                for index, (method, params) in enumerate(calls)
            ]
            payload = await self._call(requests, len(requests), "batch")
            if not isinstance(payload, list) or len(payload) != len(requests):
                raise RPCReadError("rpc_batch_incomplete")
            if not all(
                isinstance(row, dict) and type(row.get("id")) is int for row in payload
            ):
                raise RPCReadError("rpc_invalid_response_envelope")
            responses = {row["id"]: row for row in payload}
            if set(responses) != {request["id"] for request in requests}:
                raise RPCReadError("rpc_batch_ids")
            return [
                self._result(responses[request["id"]], request) for request in requests
            ]

    @staticmethod
    def _result(payload: object, request: dict) -> Any:  # noqa: ANN401
        if not (
            isinstance(payload, dict)
            and payload.get("jsonrpc") == "2.0"
            and type(payload.get("id")) is int
            and payload["id"] == request["id"]
        ):
            raise RPCReadError("rpc_invalid_response_envelope")
        if "error" in payload:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            code = code if type(code) is int else "unknown"
            raise RPCError(
                request["method"],
                code,
                error.get("data") if isinstance(error, dict) else None,
            )
        if "result" not in payload:
            raise RPCReadError("rpc_missing_result")
        return payload["result"]

    async def _call(self, request: dict | list, count: int, method: str) -> object:
        if self.requests + count > self.request_limit:
            raise BoundReached("request_limit_reached")
        wait = max(0.0, self.last_request + self.request_interval - time.monotonic())
        if time.monotonic() + wait >= self.deadline:
            raise BoundReached("deadline_reached")
        await asyncio.sleep(wait)
        self.last_request = time.monotonic()
        remaining = self.deadline - self.last_request
        if remaining <= 0:
            raise BoundReached("deadline_reached")
        self.requests += count
        self.http_requests += 1
        try:
            async with self.session.post(
                self.endpoint,
                json=request,
                timeout=aiohttp.ClientTimeout(total=min(15.0, remaining)),
                allow_redirects=False,
            ) as response:
                # Headers arrive after dispatch; setup delays cannot shorten spacing.
                self.last_request = time.monotonic()
                if response.status != 200:
                    raise RPCReadError(f"rpc_{method}_http_{response.status}")
                try:
                    data = await response.content.readexactly(self.response_limit + 1)
                except asyncio.IncompleteReadError as exc:
                    data = exc.partial
                if len(data) > self.response_limit:
                    raise RPCReadError("rpc_response_too_large")
                try:
                    return json.loads(data)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RPCReadError("rpc_invalid_json") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError) and time.monotonic() >= self.deadline:
                raise BoundReached("deadline_reached") from exc
            raise RPCReadError(f"rpc_{method}_transport_{type(exc).__name__}") from exc

    async def contract(self, target: str, data: str, block: dict) -> str:
        return await self.call("eth_call", [{"to": target, "data": data}, block])

    async def code(self, target: str, block: dict) -> None:
        require(
            bool(raw_hex(await self.call("eth_getCode", [target, block]))),
            "deployment_bytecode_missing",
        )

    async def block(self) -> tuple[dict, dict]:
        header = await self.call("eth_getBlockByNumber", ["latest", False])
        require(isinstance(header, dict), "block_header_missing")
        require(len(raw_hex(header.get("hash"))) == 32, "block_hash_missing")
        quantity(header.get("number"))
        return header, {"blockHash": header["hash"], "requireCanonical": True}


def route_scope(pools: dict) -> list[dict]:
    routes = []
    for buy, sell in permutations(FEES, 2):
        left, right = pools[str(buy)], pools[str(sell)]
        status = "supported"
        if "error" in left or "error" in right:
            status = "pool_error"
        elif left["address"] == ZERO or right["address"] == ZERO:
            status = "no_pool"
        elif left["address"] == right["address"]:
            status = "same_pool_rejected"
        routes.append(
            {
                "buy_fee": buy,
                "sell_fee": sell,
                "buy_pool": left.get("address"),
                "sell_pool": right.get("address"),
                "status": status,
            }
        )
    return routes


async def attest(
    rpc: RPC, network: dict, tokens: list[str], markets: list[dict]
) -> tuple[str, dict]:
    check_chain(await rpc.call("eth_chainId", []), network["chain_id"])
    header, block = await rpc.block()
    for target in (network["factory"], network["quoter"]):
        await rpc.code(target, block)
    factory = abi_address(await rpc.contract(network["quoter"], "0xc45a0155", block))
    require(factory == network["factory"], "quoter_factory_mismatch")
    base = abi_address(await rpc.contract(network["quoter"], "0x4aa4a4fc", block))
    require(base != ZERO, "quoter_wrapped_native_missing")
    require(network["wrapped_native"] in (None, base), "quoter_wrapped_native_mismatch")
    await rpc.code(base, block)
    require(
        abi_int(await rpc.contract(base, "0x313ce567", block), 8) == 18,
        "wrapped_native_decimals_not_18",
    )
    for token in tokens:
        require(
            token not in (base, ZERO), "counterpart_must_differ_from_wrapped_native"
        )
        market = {
            "token": token,
            "pools": {str(fee): {"error": "not_attested"} for fee in FEES},
        }
        markets.append(
            market
        )  # Keep partial discovery if a bound interrupts hydration.
        try:
            await rpc.code(token, block)
            market["token_decimals"] = abi_int(
                await rpc.contract(token, "0x313ce567", block), 8
            )
        except BoundReached:
            raise
        except ValueError as exc:
            market["error"] = str(exc)
        for fee in FEES:
            try:
                require("error" not in market, "counterpart_attestation_failed")
                data = (
                    "0x1698ee82"
                    + (uint(int(base, 16)) + uint(int(token, 16)) + uint(fee, 24)).hex()
                )
                pool = abi_address(await rpc.contract(factory, data, block))
                if pool != ZERO:
                    await rpc.code(pool, block)
                market["pools"][str(fee)] = {"address": pool}
            except BoundReached:
                raise
            except ValueError as exc:
                market["pools"][str(fee)] = {"error": str(exc)}
        market["routes"] = route_scope(market["pools"])
    return base, header


async def sample_route(  # noqa: PLR0913 - explicit quote and fee context
    rpc: RPC,
    quoter: str,
    base: str,
    token: str,
    route: dict,
    amount: int,
    block: dict,
    gas_price: int | None,
) -> dict:
    """Quote one cycle with a complete quote-transaction fee proxy, never a fill."""
    result = dict(route)
    transaction = {
        "from": ZERO,
        "to": quoter,
        "value": "0x0",
        "data": quote_data(base, token, route["buy_fee"], route["sell_fee"], amount),
    }
    output, swap_gas = decode_quote(await rpc.call("eth_call", [transaction, block]))
    result.update(
        output_raw=str(output),
        quote_gas_units=str(swap_gas),
        fee_raw=None,
        net_raw=None,
    )
    try:
        gas = quantity(await rpc.call("eth_estimateGas", [transaction, block]))
        require(gas > 0, "complete_transaction_gas_missing")
        result["estimated_transaction_gas_units"] = str(gas)
        require(gas_price is not None, "gas_price_evidence_missing")
        fee = ((gas * 125 + 99) // 100) * gas_price
        result.update(
            status="quoted",
            fee_raw=str(fee),
            net_raw=str(net_amount(amount, output, fee)),
        )
    except ValueError as exc:
        result.update(status="fee_unknown", error=str(exc))
    return result


async def run(args: argparse.Namespace, out: Any) -> int:  # noqa: ANN401, C901, PLR0912, PLR0915
    """Keep the bounded lifecycle and incomplete-evidence reporting together."""
    started = time.monotonic()
    deadline = started + args.minutes * 60
    totals = {
        amount: {
            "input_raw": str(amount),
            "quotes": 0,
            "positive_quotes": 0,
            "positive_episodes": 0,
            "best_quoted_net_raw": None,
            "best_route": None,
        }
        for amount in args.amounts
    }
    active: set[tuple[str, int]] = set()
    snapshots = quotes = quote_errors = 0
    partial = False
    reason = None
    network = NETWORKS[args.chain]
    markets = []
    async with aiohttp.ClientSession(trust_env=False) as session:
        rpc = RPC(session, args.endpoint, deadline)
        try:
            base, discovery = await attest(rpc, network, args.tokens, markets)
            emit(
                out,
                "ready",
                chain=args.chain,
                base_symbol=network["native_symbol"],
                base_decimals=18,
                sizes_raw=[str(n) for n in args.amounts],
                evidence_level="quote-only/native-quoter",
                verified_network={
                    "chain_id": network["chain_id"],
                    "source": network["source"],
                },
                verified_contracts={
                    "factory": network["factory"],
                    "quoter_v2": network["quoter"],
                    "wrapped_native": base,
                    "block_hash": discovery["hash"],
                },
                scope={
                    "markets": markets,
                    "fee_tiers": FEES,
                    "universe_complete": False,
                },
                fee_model=FEE_MODEL,
                request_limit=REQUEST_LIMIT,
                request_interval_seconds=REQUEST_INTERVAL,
                deadline_seconds=args.minutes * 60,
                block_policy="EIP-1898 hash only, requireCanonical",
            )
            viable = any(
                route["status"] == "supported"
                for market in markets
                for route in market["routes"]
            )
            partial = any(
                "error" in market
                or not any(route["status"] == "supported" for route in market["routes"])
                or any(
                    route["status"] in ("pool_error", "same_pool_rejected")
                    for route in market["routes"]
                )
                for market in markets
            )
            last_number = -1
            while time.monotonic() < deadline and rpc.requests < REQUEST_LIMIT:
                header, block = await rpc.block()
                number = quantity(header["number"])
                if number <= last_number:
                    emit(
                        out,
                        "block_skipped",
                        chain=args.chain,
                        block_hash=header["hash"],
                        block_number=str(number),
                        reason="duplicate_or_regressed_block",
                    )
                    await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                    continue
                last_number = number
                snapshots += 1
                gas_price = None
                price_error = None
                price_at = time.time()
                try:
                    observed_price = quantity(await rpc.call("eth_gasPrice", []))
                    base_fee = quantity(header.get("baseFeePerGas"))
                    require(
                        observed_price > 0 and base_fee > 0,
                        "gas_price_evidence_missing",
                    )
                    gas_price = max((observed_price * 125 + 99) // 100, base_fee * 2)
                except BoundReached:
                    raise
                except ValueError as exc:
                    price_error = str(exc)
                    partial = True
                for market in markets:
                    rows = []
                    for amount in args.amounts:
                        results = []
                        for route in market["routes"]:
                            result = dict(route)
                            if route["status"] == "supported":
                                try:
                                    result = await sample_route(
                                        rpc,
                                        network["quoter"],
                                        base,
                                        market["token"],
                                        route,
                                        amount,
                                        block,
                                        gas_price,
                                    )
                                except BoundReached:
                                    raise
                                except ValueError as exc:
                                    result.update(status="quote_error", error=str(exc))
                                if "error" in result:
                                    partial = True
                                    quote_errors += 1
                                    emit(
                                        out,
                                        "quote_error",
                                        chain=args.chain,
                                        market=market["token"],
                                        block_hash=header["hash"],
                                        input_raw=str(amount),
                                        route=result,
                                    )
                            results.append(result)
                        successful = [item for item in results if "output_raw" in item]
                        measured = [
                            item for item in successful if item["net_raw"] is not None
                        ]
                        complete = bool(measured) and all(
                            item["status"] in ("quoted", "no_pool") for item in results
                        )
                        best = max(
                            measured,
                            key=lambda item: int(item["net_raw"]),
                            default=None,
                        )
                        if best is None:
                            best = max(
                                successful,
                                key=lambda item: int(item["output_raw"]),
                                default=None,
                            )
                        rows.append(
                            {
                                "input_raw": str(amount),
                                "quoted_routes": len(successful),
                                "coverage_complete": complete,
                                "route_coverage": results,
                                "best_route": best,
                                "output_raw": best["output_raw"] if best else None,
                                "fee_raw": best["fee_raw"] if best else None,
                                "net_raw": best["net_raw"] if best else None,
                            }
                        )
                        entry = totals[amount]
                        entry["quotes"] += len(successful)
                        quotes += len(successful)
                        entry["positive_quotes"] += sum(
                            int(item["net_raw"]) > 0 for item in measured
                        )
                        if measured:
                            net = int(best["net_raw"])
                            if entry["best_quoted_net_raw"] is None or net > int(
                                entry["best_quoted_net_raw"]
                            ):
                                entry.update(
                                    best_quoted_net_raw=str(net),
                                    best_route={
                                        "market": market["token"],
                                        "block_hash": header["hash"],
                                        **best,
                                    },
                                )
                            key = (market["token"], amount)
                            if net > 0 and key not in active:
                                entry["positive_episodes"] += 1
                                active.add(key)
                            elif complete and net <= 0:
                                active.discard(key)
                    emit(
                        out,
                        "scan",
                        chain=args.chain,
                        block_hash=header["hash"],
                        block_number=str(quantity(header["number"])),
                        market=market["token"],
                        by_size=rows,
                        evidence_level="quote-only/native-quoter",
                        universe_complete=False,
                        gas_price_raw=str(gas_price) if gas_price is not None else None,
                        gas_price_reference="latest eth_gasPrice plus pinned block baseFee; buffered assumption",
                        gas_price_observed_at=price_at,
                        gas_price_error=price_error,
                    )
                if not viable:
                    reason = "no_distinct_pool_routes_in_requested_scope"
                    partial = True
                    break
                await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
            if rpc.requests >= REQUEST_LIMIT:
                reason = "request_limit_reached"
                partial = True
            if snapshots == 0:
                partial = True
                reason = "no_snapshots"
        except (RPCReadError, ValueError, KeyError, TypeError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, RPCReadError | ValueError)
                else type(exc).__name__
            )
            partial = True
            emit(out, "fatal", chain=args.chain, reason=reason)
        finally:
            emit(
                out,
                "summary",
                chain=args.chain,
                status="partial" if partial else "complete_requested_scope",
                reason=reason,
                elapsed_seconds=round(time.monotonic() - started, 3),
                snapshots=snapshots,
                quotes=quotes,
                quote_errors=quote_errors,
                requests=rpc.requests,
                by_size=list(totals.values()),
                signed_transactions=0,
                submitted_transactions=0,
                universe_complete=False,
                discovery_coverage=markets,
                limitations=LIMITATIONS,
            )
    return 1 if partial else 0


def self_check() -> None:  # noqa: C901
    """Offline contract checks only; no RPC session or endpoint is opened."""
    from unittest.mock import patch  # noqa: PLC0415

    def rejected(function: Any, *args: Any) -> None:  # noqa: ANN401
        try:
            function(*args)
        except ValueError:
            return
        raise AssertionError("unsafe_input_was_accepted")

    rejected(check_chain, "0x89", 4663)
    rejected(uint, -1)
    rejected(uint, 1 << 256)
    rejected(abi_int, "0x" + uint(256).hex(), 8)
    rejected(quote_data, ZERO, "0x" + "11" * 20, 500, 500, 1)
    rejected(sizes_raw, "0.0000000000000000001")
    require(sizes_raw("0.000000000000000001,1") == [1, 10**18], "self_check_failed")
    require(
        net_amount(1, 100, None) is None and net_amount(10, 15, 7) == -2,
        "self_check_failed",
    )
    pools = {str(fee): {"address": "0x" + "11" * 20} for fee in FEES}
    require(
        all(route["status"] == "same_pool_rejected" for route in route_scope(pools)),
        "self_check_failed",
    )

    async def reject_write() -> None:
        client = RPC(None, "invalid://must-not-be-contacted", time.monotonic() + 1)  # type: ignore[arg-type]
        for method in (
            "eth_sendRawTransaction",
            "eth_sendTransaction",
            "eth_sign",
            "personal_sign",
        ):
            try:
                await client.call(method, [])
            except ValueError:
                pass
            else:
                raise AssertionError("write_rpc_was_accepted")
        require(client.requests == 0, "self_check_failed")

        async def expire_while_waiting(delay: float) -> None:  # noqa: ARG001
            client.deadline = 0

        with patch.object(asyncio, "sleep", expire_while_waiting):
            try:
                await client.call("eth_chainId", [])
            except BoundReached:
                require(client.requests == 0, "self_check_failed")
            else:
                raise AssertionError("expired_wait_started_rpc")

        class MissingFee:
            async def call(self, method: str, params: list) -> str:  # noqa: ARG002
                if method == "eth_call":
                    return (
                        "0x"
                        + b"".join(
                            uint(n) for n in (20, 128, 224, 5000, 2, 1, 1, 2, 0, 0)
                        ).hex()
                    )
                raise ValueError("complete_fee_estimate_unavailable")

        sample = await sample_route(
            MissingFee(),
            ZERO,
            ZERO,
            "0x" + "11" * 20,  # type: ignore[arg-type]
            {"buy_fee": 500, "sell_fee": 3000},
            10,
            {},
            100,
        )
        require(sample["status"] == "fee_unknown", "self_check_failed")
        require(
            sample["fee_raw"] is None and sample["net_raw"] is None, "self_check_failed"
        )

    asyncio.run(reject_write())
    print(
        "PASS: chain mismatch, ABI bounds, distinct pools, unknown fees and read-only RPC"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--chain", choices=tuple(NETWORKS))
    parser.add_argument("--rpc-url", help="HTTPS endpoint; never written to the tape")
    parser.add_argument(
        "--token",
        action="append",
        default=[],
        help="explicit counterpart ERC20 address; repeatable",
    )
    parser.add_argument(
        "--sizes", help="comma-separated positive native-unit decimals (at most 12)"
    )
    parser.add_argument("--minutes", type=float, default=2)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.chain is None or args.out is None:
        parser.error("--chain and --out are required")
    try:
        require(
            math.isfinite(args.minutes) and 0.1 <= args.minutes <= 30,
            "minutes_must_be_0.1_to_30",
        )
        require(args.out.suffix == ".jsonl", "out_must_be_new_jsonl")
        network = NETWORKS[args.chain]
        args.amounts = sizes_raw(args.sizes or network["sizes"])
        args.tokens = list(
            dict.fromkeys(address(token) for token in (args.token or network["tokens"]))
        )
        require(
            1 <= len(args.tokens) <= 8,
            "provide_1_to_8_explicit_counterpart_token_addresses",
        )
        require(ZERO not in args.tokens, "zero_counterpart_address")
        args.endpoint = args.rpc_url or network["rpc"]
        parsed = urlsplit(args.endpoint)
        require(
            parsed.scheme == "https" and bool(parsed.hostname) and not parsed.fragment,
            "rpc_url_requires_https_host_without_fragment",
        )
    except ValueError:
        parser.error(
            "invalid research input: use 1..12 unique positive decimal sizes, 1..8 token addresses, minutes 0.1..30, HTTPS RPC and a new .jsonl output"
        )
    try:
        with args.out.open("x", encoding="utf-8") as out:
            status = asyncio.run(run(args, out))
    except OSError as exc:
        parser.error(f"output_unavailable_{type(exc).__name__}")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
