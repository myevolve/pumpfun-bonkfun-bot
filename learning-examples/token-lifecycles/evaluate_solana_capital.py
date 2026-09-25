# Protocol bounds are explicit; input errors retain their rejected invariant.
# ruff: noqa: PLR2004, TRY003
"""Bounded, read-only SOL capital curves; mathematical quotes, never transactions."""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import time
from collections import Counter
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlsplit

import aiohttp
import simulate_atomic_cycles as atomic
from dotenv import dotenv_values

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Hashable

DEFAULT_SIZES = "0.01,0.05,0.1,0.25,0.5,1,2,5,10"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
READ_METHODS = frozenset({"getMultipleAccounts", "getGenesisHash"})
FEE = atomic.NETWORK_FEE + atomic.TIP_LAMPORTS
LIMITATIONS = [
    "Quote-only, processed-bank observations; no signatures, submissions or fills.",
    "Frozen first-page Raydium standard SPL-token AMM v4/CPMM universe; not all Solana venues.",
    "All sizes and directions share one captured bank per market, not across markets.",
    "Constant-product impact and pool fees included; fixed 65000 network + 10000 tip lamports, not a live fee estimate.",
    "Overlapping sizes and routes are alternatives, never additive independent trades.",
    "Episodes are sampled state transitions, not realizable trade frequency or daily returns.",
    "Full-cycle simulation, inventory closure, delayed survival, failure costs and held-out frequency remain unmeasured.",
]


class ProbeError(ValueError):
    """A local, credential-free failure code."""


class DeadlineReached(Exception):  # noqa: N818
    """Normal observation-window termination, not a transport failure."""


def parse_sizes(text: str) -> list[int]:
    """Accept exact positive lamport amounts without rounding or float arithmetic."""
    try:
        parts = text.split(",")
        atomic.require(1 <= len(parts) <= 32, "invalid_sizes")
        sizes = []
        for part in parts:
            atomic.require(
                bool(re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,9})?", part.strip())),
                "invalid_sizes",
            )
            amount = Decimal(part.strip()) * 1_000_000_000
            atomic.require(
                amount.is_finite() and 0 < amount <= 2**64 - 1 - FEE, "invalid_sizes"
            )
            sizes.append(int(amount))
        atomic.require(len(set(sizes)) == len(sizes), "invalid_sizes")
        return sorted(sizes)
    except (ValueError, InvalidOperation):
        raise argparse.ArgumentTypeError(
            "sizes must be 1..32 distinct positive SOL decimals, at most 9 decimal places, within u64 lamports"
        ) from None


def safe_error(exc: Exception) -> str:
    """Never persist arbitrary exception text or provider response messages."""
    if isinstance(exc, ProbeError):
        return str(exc)
    message = str(exc)
    allowed = {
        "account_missing",
        "account_owner",
        "account_encoding",
        "account_layout",
        "pool_missing",
        "unsupported_program",
        "unsupported_v4_status",
        "pool_discriminator",
        "swap_disabled",
        "unsupported_token_program",
        "creator_fee_flags",
        "pool_references_changed",
        "mint_uninitialized",
        "vault_identity",
        "vault_frozen",
        "v4_fee_denominator",
        "pool_not_open",
        "config_discriminator",
        "empty_reserves",
        "invalid_fee_rate",
        "quote_input",
        "snapshot_account_limit",
        "snapshot_count",
        "no_eligible_pool_pairs",
        "discovery_rejected",
        "discovery_mint_mismatch",
        "rpc_method_not_read_only",
    }
    if isinstance(exc, ValueError) and (
        message in allowed
        or re.fullmatch(r"(?:rpc_http_|discovery_http_)[0-9]{3}", message)
    ):
        return message
    return type(exc).__name__


class ReadSession:
    """Constrain existing discovery/bank helpers at their HTTP boundary."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        deadline: float,
        interval: float,
        maximum: int,
    ) -> None:
        self.session = session
        self.endpoint = endpoint
        self.deadline = deadline
        self.interval = interval
        self.maximum = maximum
        self.requests = 0
        self.next_start = 0.0

    def check(self, verb: str, url: str, kwargs: dict) -> None:
        """Reject unapproved HTTP targets and RPC methods before any I/O."""
        if verb == "POST":
            if (
                url != self.endpoint
                or kwargs.get("json", {}).get("method") not in READ_METHODS
            ):
                raise ProbeError("rpc_method_not_read_only")
        elif verb != "GET" or url not in {
            atomic.API + "/pools/info/list",
            atomic.API + "/pools/info/mint",
        }:
            raise ProbeError("http_target_not_allowed")

    @asynccontextmanager
    async def request(
        self, verb: str, url: str, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:  # noqa: ANN401
        """Pace one approved request inside the wall-clock and request bounds."""
        self.check(verb, url, kwargs)
        if self.requests >= self.maximum:
            raise ProbeError("request_limit_reached")
        delay = max(0, self.next_start - time.monotonic())
        if time.monotonic() + delay >= self.deadline:
            raise DeadlineReached
        await asyncio.sleep(delay)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineReached
        self.requests += 1
        self.next_start = time.monotonic() + self.interval
        try:
            async with self.session.request(
                verb,
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=min(10, remaining)),
                **kwargs,
            ) as response:
                yield response
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProbeError("transport_" + type(exc).__name__) from None

    def get(
        self, url: str, **kwargs: Any
    ) -> AbstractAsyncContextManager[aiohttp.ClientResponse]:  # noqa: ANN401
        """Expose the existing discovery helper's GET interface."""
        return self.request("GET", url, **kwargs)

    def post(
        self, url: str, **kwargs: Any
    ) -> AbstractAsyncContextManager[aiohttp.ClientResponse]:  # noqa: ANN401
        """Expose the existing same-bank helper's JSON-RPC POST interface."""
        return self.request("POST", url, **kwargs)


def episode(
    active: set[Hashable], key: Hashable, net: int | None, *, complete: bool
) -> int:
    """Missing/partial nonpositive observations cannot reset an active episode."""
    if net is not None and net > 0:
        if key not in active:
            active.add(key)
            return 1
    elif net is not None and complete:
        active.discard(key)
    return 0


def pool_evidence(pool: atomic.Pool) -> dict:
    """Retain the reserves and fees needed to reproduce a mathematical quote."""
    return {
        "address": pool.address,
        "program": pool.program,
        "mints": list(pool.mints),
        "vaults": list(pool.vaults),
        "config": pool.config,
        "reserves_raw": [str(value) for value in pool.reserves],
        "trade_rate": pool.trade_rate,
        "creator_rate": pool.creator_rate,
        "fee_on": pool.fee_on,
        "fee_denominator": atomic.FEE_DENOMINATOR,
    }


def _curve_factors(pool: atomic.Pool, input_mint: str) -> tuple[int, int, int, int]:
    side = pool.mints.index(input_mint)
    creator_input = pool.fee_on == 0 or pool.fee_on == side + 1
    return (
        pool.reserves[side],
        pool.reserves[1 - side],
        atomic.FEE_DENOMINATOR
        - pool.trade_rate
        - (pool.creator_rate if creator_input else 0),
        atomic.FEE_DENOMINATOR - (0 if creator_input else pool.creator_rate),
    )


def capital_envelope(pools: list[atomic.Pool], mint: str) -> dict | None:
    """Bound every positive input, including amounts between the size-grid points.

    Without integer rounding, two distinct constant-product swaps compose to
    C*x/(D+E*x). Its maximum gross profit is (C+D-2*sqrt(C*D))/E when C>D.
    Flooring the square root gives an optimistic integer bound; actual rounded
    fees and outputs cannot improve it. The near-peak input is not a fill or
    a guaranteed integer optimum. This formula does not cover CLMM or DLMM.
    """
    f = atomic.FEE_DENOMINATOR
    f2, f4 = f * f, f**4
    buys = {pool.address: _curve_factors(pool, atomic.SOL) for pool in pools}
    sells = {pool.address: _curve_factors(pool, mint) for pool in pools}
    best = None
    bounded = 0
    for buy in pools:
        a1, b1, g1, h1 = buys[buy.address]
        for sell in pools:
            if buy.address == sell.address:
                continue
            a2, b2, g2, h2 = sells[sell.address]
            c = b1 * b2 * g1 * h1 * g2 * h2
            d = a1 * a2 * f4
            e = g1 * f * (a2 * f2 + b1 * h1 * g2)
            peak = upper = 0
            if c > d:
                root = math.isqrt(c * d)
                peak = (root - d) // e
                upper = (c + d - 2 * root) // e
            bounded += 1
            if best is None or upper > int(best["upper_gross_raw"]):
                best = {
                    "buy": buy.address,
                    "sell": sell.address,
                    "input_near_peak_raw": str(peak),
                    "upper_gross_raw": str(upper),
                    "upper_net_raw": str(upper - FEE),
                }
    if best is not None:
        best["routes_bounded"] = bounded
    return best


def quote_size(pools: list[atomic.Pool], mint: str, amount: int, expected: int) -> dict:
    """Evaluate every ordered distinct-pool cycle at the actual modeled input."""
    best = None
    quotes = positive = 0
    errors = []
    for buy in pools:
        try:
            quantity = buy.quote(atomic.SOL, amount)
            atomic.require(quantity > 0, "quote_input")
        except (ValueError, ArithmeticError) as exc:
            errors.append(
                {
                    "buy": buy.address,
                    "reason": safe_error(exc),
                    "failed_routes": len(pools) - 1,
                }
            )
            continue
        for sell in pools:
            if buy.address == sell.address:
                continue
            try:
                output = sell.quote(mint, quantity)
                atomic.require(
                    output <= 2**64 - 1 and quantity <= 2**64 - 1, "quote_input"
                )
            except (ValueError, ArithmeticError) as exc:
                errors.append(
                    {
                        "buy": buy.address,
                        "sell": sell.address,
                        "reason": safe_error(exc),
                        "failed_routes": 1,
                    }
                )
                continue
            net = output - amount - FEE
            quotes += 1
            positive += net > 0
            if best is None or net > int(best["net_raw"]):
                best = {
                    "buy": buy.address,
                    "sell": sell.address,
                    "intermediate_raw": str(quantity),
                    "output_raw": str(output),
                    "net_raw": str(net),
                }
    complete = quotes == expected
    return {
        "input_raw": str(amount),
        "quoted_routes": quotes,
        "expected_routes": expected,
        "coverage": "complete" if complete else "partial" if quotes else "unknown",
        "complete": complete,
        "positive_quotes": positive,
        "best_route": best,
        "output_raw": best["output_raw"] if best else None,
        "fee_raw": str(FEE),
        "net_raw": best["net_raw"] if best else None,
        "quote_errors": errors,
    }


def endpoint_from_file(path: Path) -> str:
    """Load an endpoint from an explicitly selected non-root environment file."""
    resolved = path.resolve()
    if resolved in {atomic.ROOT / ".env", atomic.ROOT / ".env~"} or any(
        part.upper().startswith("ENVDATA") for part in (*path.parts, *resolved.parts)
    ):
        raise ProbeError("credentials_file_forbidden")
    if not resolved.is_file() or resolved.stat().st_size > 65536:
        raise ProbeError("endpoint_file_missing_or_too_large")
    endpoint = dotenv_values(resolved, interpolate=False).get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    if not endpoint:
        raise ProbeError("rpc_endpoint_missing")
    try:
        parsed = urlsplit(endpoint)
        valid = (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ProbeError("rpc_endpoint_invalid")
    return endpoint


async def run(args: argparse.Namespace, out: TextIO) -> int:  # noqa: C901, PLR0915
    """Keep the bounded observation lifecycle and partial evidence together."""
    started = time.monotonic()
    deadline = started + args.minutes * 60
    counts = Counter()
    active, market_active = set(), set()
    stats = {
        amount: {
            "input_raw": str(amount),
            "quotes": 0,
            "positive_quotes": 0,
            "positive_episodes": 0,
            "best_quoted_net_raw": None,
            "best_route": None,
        }
        for amount in args.sizes
    }
    client = None
    ready = False
    status = "complete"
    pending = None
    best_envelope = None

    def emit(event: str, **fields: Any) -> None:  # noqa: ANN401
        atomic.emit(out, event, chain="solana", **fields)

    async def observe() -> None:  # noqa: C901, PLR0912, PLR0915
        nonlocal client, ready, pending, best_envelope
        endpoint = endpoint_from_file(args.env_file)
        async with aiohttp.ClientSession() as session:
            client = ReadSession(
                session, endpoint, deadline, args.request_interval, args.max_requests
            )
            async with client.post(
                endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getGenesisHash",
                    "params": [],
                },
            ) as response:
                if response.status != 200:
                    raise ProbeError("network_attestation_failed")
                genesis = await response.json()
            if genesis.get("result") != MAINNET_GENESIS or "error" in genesis:
                raise ProbeError("network_not_solana_mainnet")
            groups = await atomic.discover(client, endpoint, args.markets, out)
            emit(
                "ready",
                base_symbol="SOL",
                base_decimals=9,
                sizes_raw=[str(amount) for amount in args.sizes],
                scope={
                    "markets": {
                        mint: [pool.address for pool in pools]
                        for mint, pools in groups.items()
                    },
                    "requested_markets": args.markets,
                    "discovery": "frozen_top_volume_first_page_standard_pools_max_six_per_mint",
                },
                fee_model={
                    "network_fee_raw": str(atomic.NETWORK_FEE),
                    "tip_raw": str(atomic.TIP_LAMPORTS),
                    "total_raw": str(FEE),
                    "pool_fees": "included_in_quotes",
                    "kind": "unchanged_fixed_assumption",
                },
                request_bounds={
                    "maximum": args.max_requests,
                    "minimum_start_interval_seconds": args.request_interval,
                    "deadline_seconds_including_initialization": args.minutes * 60,
                },
                verified_network={
                    "genesis_hash": MAINNET_GENESIS,
                    "programs": list(atomic.AUTHORITY),
                },
                evidence_level="quote-only",
                limitations=LIMITATIONS,
            )
            ready = True
            last_slots = {}
            while time.monotonic() < deadline:
                for mint, pools in groups.items():
                    if time.monotonic() >= deadline:
                        return
                    pending = {
                        "market": mint,
                        "expected_routes": len(pools) * (len(pools) - 1),
                    }
                    keys = list(
                        dict.fromkeys(
                            [
                                atomic.CLOCK,
                                *[key for pool in pools for key in pool.dependencies()],
                            ]
                        )
                    )
                    try:
                        slot, bank = await atomic.bank_read(client, endpoint, keys)
                    except DeadlineReached:
                        pending = None
                        return
                    except Exception as exc:
                        if (
                            isinstance(exc, ProbeError)
                            and str(exc) == "request_limit_reached"
                        ):
                            raise
                        counts["quote_errors"] += 1
                        emit(
                            "quote_error",
                            market=mint,
                            reason=safe_error(exc),
                            stage="bank_read",
                        )
                        emit(
                            "scan",
                            market=mint,
                            slot=None,
                            evidence_level="quote-only",
                            complete=False,
                            by_size=[
                                quote_size([], mint, amount, pending["expected_routes"])
                                for amount in args.sizes
                            ],
                        )
                        pending = None
                        continue
                    if slot <= last_slots.get(mint, -1):
                        counts["duplicate_or_regressed_banks"] += 1
                        emit(
                            "bank_skipped",
                            market=mint,
                            slot=slot,
                            reason="duplicate_or_regressed_bank",
                        )
                        pending = None
                        continue
                    last_slots[mint] = slot
                    live, exclusions = [], []
                    for pool in pools:
                        try:
                            live.append(atomic.hydrate_pool(pool, bank))
                        except (
                            ValueError,
                            KeyError,
                            TypeError,
                            ArithmeticError,
                        ) as exc:
                            exclusions.append(
                                {"pool": pool.address, "reason": safe_error(exc)}
                            )
                    counts["snapshots"] += 1
                    bound = capital_envelope(live, mint)
                    if bound is not None:
                        counts["capital_envelope_routes"] += bound["routes_bounded"]
                        counts["positive_envelope_snapshots"] += (
                            int(bound["upper_net_raw"]) > 0
                        )
                        if best_envelope is None or int(bound["upper_net_raw"]) > int(
                            best_envelope["upper_net_raw"]
                        ):
                            best_envelope = {**bound, "market": mint, "slot": slot}
                    rows = [
                        quote_size(live, mint, amount, pending["expected_routes"])
                        for amount in args.sizes
                    ]
                    complete = all(row["complete"] for row in rows)
                    if not complete:
                        counts["incomplete_snapshots"] += 1
                    counts["quote_errors"] += len(exclusions) + sum(
                        sum(error["failed_routes"] for error in row["quote_errors"])
                        for row in rows
                    )
                    scan_id = counts["snapshots"]
                    for row in rows:
                        amount = int(row["input_raw"])
                        stat = stats[amount]
                        net = (
                            int(row["net_raw"]) if row["net_raw"] is not None else None
                        )
                        counts["quotes"] += row["quoted_routes"]
                        stat["quotes"] += row["quoted_routes"]
                        stat["positive_quotes"] += row["positive_quotes"]
                        stat["positive_episodes"] += episode(
                            active, (mint, amount), net, complete=row["complete"]
                        )
                        if net is not None and (
                            stat["best_quoted_net_raw"] is None
                            or net > int(stat["best_quoted_net_raw"])
                        ):
                            stat["best_quoted_net_raw"] = str(net)
                            stat["best_route"] = {
                                **row["best_route"],
                                "market": mint,
                                "slot": slot,
                                "scan_id": scan_id,
                            }
                    observed = [
                        int(row["net_raw"])
                        for row in rows
                        if row["net_raw"] is not None
                    ]
                    counts["nonoverlapping_market_episodes"] += episode(
                        market_active,
                        mint,
                        max(observed) if observed else None,
                        complete=complete,
                    )
                    emit(
                        "scan",
                        scan_id=scan_id,
                        slot=slot,
                        market=mint,
                        evidence_level="quote-only",
                        complete=complete,
                        dependency_accounts=keys,
                        capital_envelope=bound,
                        pool_evidence=[pool_evidence(pool) for pool in live],
                        excluded_pools=exclusions,
                        by_size=rows,
                    )
                    pending = None

    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            await observe()
    except DeadlineReached:
        if not ready:
            status = "fatal"
            emit("fatal", reason="deadline_during_initialization")
    except TimeoutError:
        if not ready or pending is not None:
            status = "partial" if ready else "fatal"
            counts["quote_errors"] += 1
            emit(
                "fatal",
                reason="deadline_during_snapshot"
                if ready
                else "deadline_during_initialization",
                pending=pending,
            )
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as exc:
        status = "partial" if ready else "fatal"
        emit("fatal", reason=safe_error(exc), pending=pending)
    if status == "complete" and (
        counts["quote_errors"] or counts["incomplete_snapshots"] or not counts["quotes"]
    ):
        status = "partial" if counts["quotes"] else "no_coverage"
    emit(
        "summary",
        status=status,
        elapsed_seconds=round(time.monotonic() - started, 3),
        snapshots=counts["snapshots"],
        quotes=counts["quotes"],
        quote_errors=counts["quote_errors"],
        requests=client.requests if client else 0,
        by_size=list(stats.values()),
        capital_envelope_routes=counts["capital_envelope_routes"],
        positive_envelope_snapshots=counts["positive_envelope_snapshots"],
        best_capital_envelope=best_envelope,
        nonoverlapping_market_episodes=counts["nonoverlapping_market_episodes"],
        incomplete_snapshots=counts["incomplete_snapshots"],
        duplicate_or_regressed_banks=counts["duplicate_or_regressed_banks"],
        signed_transactions=0,
        submitted_transactions=0,
        limitations=LIMITATIONS,
    )
    return 0 if status == "complete" else 1


def self_check() -> None:
    """One deterministic contract check; never performs network I/O."""
    from dataclasses import replace  # noqa: PLC0415

    pool = atomic.Pool(
        "buy",
        atomic.CPMM,
        (atomic.SOL, "token"),
        ("v0", "v1"),
        reserves=(1_000_000_000, 2_000_000_000),
        trade_rate=3000,
    )
    sell = replace(pool, address="sell")
    sizes = parse_sizes("0.01,0.1,1")
    small, large = pool.quote(atomic.SOL, sizes[0]), pool.quote(atomic.SOL, sizes[-1])
    atomic.require(
        small == 9_970_000 * 2_000_000_000 // 1_009_970_000, "self_check_failed"
    )
    atomic.require(large * sizes[0] < small * sizes[-1], "self_check_failed")
    row = quote_size([pool, sell], "token", sizes[0], 2)
    atomic.require(
        int(row["net_raw"]) == int(row["output_raw"]) - sizes[0] - 75000 < 0,
        "self_check_failed",
    )
    balanced_bound = capital_envelope([pool, sell], "token")
    atomic.require(
        balanced_bound["upper_gross_raw"] == "0", "balanced_curve_has_no_gross_edge"
    )
    spread_buy = replace(pool, trade_rate=0)
    spread_sell = replace(sell, reserves=(2_000_000_000, 1_000_000_000), trade_rate=0)
    spread_bound = capital_envelope([spread_buy, spread_sell], "token")
    atomic.require(
        spread_bound["input_near_peak_raw"] == "333333333", "continuous_peak"
    )
    atomic.require(spread_bound["upper_gross_raw"] == "333333333", "continuous_bound")
    for mode in (0, 1, 2):
        left = replace(spread_buy, trade_rate=3000, creator_rate=1000, fee_on=mode)
        right = replace(
            spread_sell, trade_rate=2000, creator_rate=2000, fee_on=(mode + 1) % 3
        )
        bound = capital_envelope([left, right], "token")
        for amount in (1, 100_000, 333_333_333, 1_000_000_000):
            middle = left.quote(atomic.SOL, amount)
            output = right.quote("token", middle) if middle else 0
            atomic.require(
                output - amount - FEE <= int(bound["upper_net_raw"]),
                "integer_quote_exceeds_envelope",
            )
    for invalid in (
        "",
        "0",
        "-1",
        "NaN",
        "Infinity",
        "1e2",
        "0.0000000001",
        "1,1",
        "1,",
        "18446744074",
    ):
        try:
            parse_sizes(invalid)
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError("invalid size accepted")
    active = set()
    atomic.require(
        episode(active, "market", 1, complete=True) == 1, "self_check_failed"
    )
    atomic.require(
        episode(active, "market", None, complete=False) == 0, "self_check_failed"
    )
    atomic.require(
        episode(active, "market", -1, complete=False) == 0, "self_check_failed"
    )
    atomic.require(
        episode(active, "market", 2, complete=True) == 0, "self_check_failed"
    )
    atomic.require(
        episode(active, "market", 0, complete=True) == 0, "self_check_failed"
    )
    atomic.require(
        episode(active, "market", 1, complete=True) == 1, "self_check_failed"
    )
    client = ReadSession(None, "https://invalid.example", time.monotonic() + 1, 1, 1)  # type: ignore[arg-type]
    for method in ("sendTransaction", "simulateTransaction", "requestAirdrop"):
        try:
            client.check("POST", client.endpoint, {"json": {"method": method}})
        except ProbeError:
            pass
        else:
            raise AssertionError("non-read-only RPC accepted")
    atomic.require(client.requests == 0, "self_check_failed")
    print(
        "PASS: integer impact/fees, continuous capital bounds, invalid sizes, censored episodes, read-only RPC"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--sizes", type=parse_sizes, default=DEFAULT_SIZES)
    parser.add_argument("--minutes", type=float, default=1)
    parser.add_argument("--markets", type=int, default=12)
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.env_file is None or args.out is None:
        parser.error(
            "--env-file and --out are required; no payer or private key is used"
        )
    if (
        not math.isfinite(args.minutes)
        or not 0.1 <= args.minutes <= 30
        or not 1 <= args.markets <= 20
    ):
        parser.error("minutes must be in [0.1, 30], markets in [1, 20]")
    if (
        not 1 <= args.max_requests <= 10000
        or not math.isfinite(args.request_interval)
        or not 0.25 <= args.request_interval <= 60
    ):
        parser.error(
            "max-requests must be in [1, 10000], request-interval in [0.25, 60] seconds"
        )
    if args.out.suffix != ".jsonl" or args.out.resolve() == args.env_file.resolve():
        parser.error("--out must name a new .jsonl file, not the endpoint file")
    try:
        with args.out.open("x", encoding="utf-8") as out:
            result = asyncio.run(run(args, out))
    except (OSError, ValueError) as exc:
        parser.exit(
            1, "capital probe could not write tape: " + type(exc).__name__ + "\n"
        )
    raise SystemExit(result)


if __name__ == "__main__":
    main()
