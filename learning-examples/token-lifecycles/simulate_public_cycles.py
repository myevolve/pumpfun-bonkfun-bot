"""Probe fixed public native cycles; never sign, submit or load a wallet.

An attested snapshot can quote leading AMM-v4/CPMM legs locally; unsigned native
prefixes determine later quantities. The final packet repeats every swap, closes
each initially absent ATA and checks the payer's fee/tip/rent-inclusive balance.
Dust or changed quantities fail closed. No historical quantities, synthetic
balances or account overrides enter provider requests. This is not an ROI study.

    uv run learning-examples/token-lifecycles/simulate_public_cycles.py self-check
    uv run learning-examples/token-lifecycles/simulate_public_cycles.py observe \
        --route orca --public-rpc --inputs 1000000 --out /tmp/public-cycle.jsonl

Private providers require an explicit provider JSON (rpc_url), never a dotenv or
key file. No retries: throttling or transport failure terminates the sweep.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from functools import cached_property, partial
from pathlib import Path

import aiohttp
import simulate_atomic_cycles as atomic
import simulate_clmm_cycle as clmm
import simulate_damm_cycle as damm
import simulate_dlmm_cycle as dlmm
import simulate_menu_orca_pair as evidence
import simulate_orca_cycle as orca
import simulate_reference_cycles as native
from solders.transaction import VersionedTransaction

# Protocol widths and frozen research limits are intentionally explicit.
# ruff: noqa: PLR2004
MINT = "DRb8V9MRvsvkNHJn52ToE1NDUnhCihbfCh3mYeCSsVKL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
PAYER = atomic.Pubkey.from_string(evidence.PAYER)
WINDOW_NS = 2 * evidence.NS
MAX_SLOT_AGE = 2
SOURCES = (
    Path(__file__).name,
    "simulate_atomic_cycles.py",
    "simulate_clmm_cycle.py",
    "simulate_damm_cycle.py",
    "simulate_dlmm_cycle.py",
    "simulate_orca_cycle.py",
    "simulate_reference_cycles.py",
    "simulate_menu_orca_pair.py",
)


@dataclass(frozen=True)
class Route:
    """One fixed receipt-derived path, with cached public wallet addresses."""

    name: str
    mints: tuple[str, ...]
    decimals: tuple[int, ...]
    pools: tuple[str, ...]
    programs: tuple[str, ...]
    lookups: tuple[str, ...]
    reference: str

    @cached_property
    def atas(self) -> tuple[atomic.Pubkey, ...]:
        return tuple(
            atomic.get_associated_token_address(PAYER, atomic.Pubkey.from_string(mint))
            for mint in self.mints
        )

    @cached_property
    def wallet_keys(self) -> list[str]:
        return [str(PAYER), *map(str, self.atas)]

    @cached_property
    def metadata_keys(self) -> list[str]:
        """Include bitmap accounts needed to discover live pool dependencies."""
        return [
            atomic.CLOCK,
            *self.wallet_keys,
            *self.pools,
            *[
                (dlmm if program == dlmm.PROGRAM else clmm).bitmap_address(address)
                for address, program in zip(self.pools, self.programs, strict=True)
                if program in (dlmm.PROGRAM, clmm.PROGRAM)
            ],
        ]

    @cached_property
    def quote_legs(self) -> tuple[int, ...]:
        """Logical pool boundaries needing another native quote packet."""
        legs, end = [], 0
        while end < len(self.pools):
            end += (
                2 if self.programs[end : end + 2] == (orca.PROGRAM, orca.PROGRAM) else 1
            )
            legs.append(end)
        return tuple(legs)


TRIANGLE = Route(
    name="orca",
    mints=(atomic.SOL, MINT, USDC),
    decimals=(9, 4, 6),
    pools=(
        "9uvygBxPEvcqT5eLgEwyfZCgK3uTh6QQRzLFmB9pJURM",
        "EL4JWvY2WDmfrCZVBhxiisB8aPtmcD7TJxpnLHg5jm6",
        "fAjTnZ9QqJkUmrr8cXutkYhpVge2qqtSZNt9qKn7YC2",
    ),
    programs=(orca.PROGRAM, orca.PROGRAM, atomic.CPMM),
    lookups=(
        "2GsvNSkaJ4Qg2v9S3LdgHaurdar27BQJTyW94q1oL7x2",
        "21krTHqweuDpJ36VoGFGFyWM6B3J9JCYsthmaudbMPpj",
        "ArX96JpdZZ2BnjCeUzALoX2u7u7i2254mazdjC6HymPk",
        "GcPaajsS5YqeM6PRG2jQMvqnRFXvXKkrzG3eSvGJyP9d",
    ),
    reference="3WSCEzkQmpuBJ2qSA15Kf5aoMUkURJhpVnVPvfpspiroyiS9g3SaTMB5aZo7gAVaWbjpU494hSLN7V2FRpP2Xz8i",
)

DAMM_CYCLE = Route(
    name="damm",
    mints=(
        atomic.SOL,
        "HRw8mqK8N3ASKFKJGMJpy4FodwR3GKvCFKPDQNqUNuEP",
        "3ehUvrk48c5XVrcjoUcJGexN7sTLj8PkLAwZqGAMM5SM",
    ),
    decimals=(9, 9, 8),
    pools=(
        "DSTT9vABgB7UNKViELJBC6MAsoQmuqKbxL6JaQGsZME5",
        "HVKW1tsXL5gAjRHVAh3d4Hd5otrFzdQTiU6pL13uLT2T",
        "7Fd6uV9qwRZNrEk5xfwXaKu3Wndmx913DDiCxEcUPjuu",
    ),
    programs=(atomic.AMM, damm.PROGRAM, damm.PROGRAM),
    lookups=(
        "5tQLzp6mRrSG51UgxrmvcVWNdKyUccUmQNGXY9813uSm",
        "AwZPBY4EeiiUnatBobUZyhp1Vbhg9JNREtN4cggKAo3o",
        "8fcuzpHCrJBVewfLTCEohbz7t4YabZmXhx5kEoBCqf9",
        "GcPaajsS5YqeM6PRG2jQMvqnRFXvXKkrzG3eSvGJyP9d",
    ),
    reference="2w6CzSHVZJYhSvK2vSQY7ANpHCouV37TENSfpBQcZFRNYaPZgigbmzVEYkLreQwdg8nacHzMvJ7u8wJHAsNSCH2X",
)
DLMM_CYCLE = Route(
    name="dlmm",
    mints=(
        atomic.SOL,
        "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "k3nvpKdRTb4faKp6JpocCR1CTT1G6Rr4rKn35G8BAGS",
        "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    ),
    decimals=(9, 6, 9, 6),
    pools=(
        "7qt1qBnQ5CNNpMH1no6jYAzuyazP5QWXsUZB7dot5kga",
        "88W2ritLi9pLHB5iBdEHq8Hh3QySaE51LfMsykPRSrhq",
        "3WzCyhyrru6GcFQ3KSYeU1e7dhobxvqDckZjtwtMTw2X",
        "8erNF5u3CHrqZJXtkfY8CjSxFYF1yqHmN8uDbAhk6tWM",
    ),
    programs=(dlmm.PROGRAM, damm.PROGRAM, damm.PROGRAM, orca.PROGRAM),
    lookups=(
        "ERHmjJr2opu5Rb2wAQ71bN46xPg5bpa7T1AgB6BK9tSb",
        "4jjNj5YwHg8pLpd2GEamfEg3N8hAvUZVp9euwfM1xdYT",
        "3aSqq2HnrFV2CfYx3dtorfAFiGQysG1kXyQyK3u225Ab",
        "5XK1oVwyNsxHK37RNPzpVP1gSsuzHnnWA6BVDtqNzVoW",
        "2QAVBAxvSGakNsnUCHHoY6HEHkrGduBC3oyybGYbk5Zt",
    ),
    reference="2QvtdgRbKYhNi2G9MaMHaytukdRp3Vot9C1bccJmKnfoHHBke2sgHiU5JuaDydtsBwL2EkRFLP4hS29LU1NvJcRN",
)

ORCA_AMM_CYCLE = Route(
    name="orca-amm",
    mints=(
        atomic.SOL,
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "6vVfbQVRSXcfyQamPqCzcqmA86vCzb2d7B7gmDDqpump",
    ),
    decimals=(9, 6, 6),
    pools=(
        "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE",
        "3khXeyKPWC6Z56mWMjEtjNgnzp6sNzcD8SiFwd1gfpy3",
        "9gJtVVWdCbW4zHX7NxcGCt62Ff7CbNFvceTVSbkTtayb",
    ),
    programs=(orca.PROGRAM, orca.PROGRAM, atomic.AMM),
    lookups=(
        "Be3tERBQM66wyoTR4AKXV4TwpLD9DjmrWPaC7oWAoyfL",
        "QMDZUDWrNzh3J4gqt2vZkpxyyvWrje1haohXXCkw5tU",
        "EuebLC9AYtFAECbCSxFBkzxVnkpxH7BnxQyWPJFm89PW",
        "GcPaajsS5YqeM6PRG2jQMvqnRFXvXKkrzG3eSvGJyP9d",
    ),
    reference="5kPBzaLmcaNpvojMAnJS2JMf3tP8gu9NBxD3vm3GasZ2hXS6TifZWAnJpdXHeWHLnzz7gi7EeeAN8AzJUBthaZux",
)
DLMM_DAMM_CYCLE = Route(
    name="dlmm-damm",
    mints=(
        atomic.SOL,
        "METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL",
        "4KsGXPQ6BZGgCdYVDrqacDUuhFhSf8TKfbjACcApgLPF",
    ),
    decimals=(9, 6, 6),
    pools=(
        "GYqytuXSiX3GaPuCkLMY4jeRR3mAtyAuQqhD32d45g5y",
        "EpPX6QQapCM2ok4WR4u9EhtLL7dbwepghGEfvDN3ce5w",
        "L5ggskr6dVtmmXeLnDyqF7iHyLxFtHuPcmUpyJS2sM2",
    ),
    programs=(dlmm.PROGRAM, damm.PROGRAM, damm.PROGRAM),
    lookups=(
        "DtsejH5wwsHvMS7t42gRQS9JsucQexEtQYLpPd7bCocm",
        "6BGw1F8FpsF14f1e1xMjz3SV3R72Hcb4m25ENn4R4Nbw",
        "5yVELFCKguZXzmVgHEHApCysJjmzuPJAhxB4T99VHJ5H",
    ),
    reference="4TKjZbZ9yVJeW9YdHWhtjtZAwABvpVcppuQse5pasFQAM83YuYpZUTpg4eEi6AFEzX3Bh863eH2eKZKsa5PPHX8Y",
)

ROUTES = {
    route.name: route
    for route in (
        TRIANGLE,
        DAMM_CYCLE,
        DLMM_CYCLE,
        ORCA_AMM_CYCLE,
        DLMM_DAMM_CYCLE,
    )
}


def scope(route: Route, inputs: list[int]) -> dict:
    """Freeze sizes, financial limits, source identities and non-trading semantics."""
    atomic.require(
        len(route.mints)
        == len(route.decimals)
        == len(route.pools)
        == len(route.programs)
        in (2, 3, 4)
        and len(set(route.mints)) == len(route.mints)
        and len(set(route.pools)) == len(route.pools)
        and route.mints[0] == atomic.SOL,
        "route_shape",
    )
    atomic.require(
        1 <= len(inputs) <= 3
        and len(set(inputs)) == len(inputs)
        and all(type(amount) is int and 0 < amount <= 10_000_000 for amount in inputs),
        "input_size_policy",
    )
    atomic.require(
        (
            atomic.BUY_LAMPORTS,
            atomic.NETWORK_FEE,
            atomic.TIP_LAMPORTS,
            atomic.PROFIT_LAMPORTS,
            atomic.CU_LIMIT,
            atomic.CU_PRICE,
        )
        == (10_000_000, 65_000, 0, 1_000, 300_000, 200_000),
        "financial_policy_changed",
    )
    return {
        "study": "public_cycle_feasibility",
        "route": route.name,
        "reference": route.reference,
        "pools": route.pools,
        "programs": route.programs,
        "quote_legs": route.quote_legs,
        "quantity_policy": (
            "bounded_exact_output_orca_bridge"
            if route.quote_legs[0] == 2
            else "fixed_exact_input_legs"
        ),
        "snapshot_quote_policy": "single_use_attested_constant_product_prefix",
        "economic_screen": "initial_spot_upper_bound_orca_orca_ray_only",
        "mints": route.mints,
        "mint_decimals": route.decimals,
        "lookup_tables": route.lookups,
        "payer": str(PAYER),
        "inputs_lamports": inputs,
        "network_fee_lamports": atomic.NETWORK_FEE,
        "tip_lamports": atomic.TIP_LAMPORTS,
        "profit_floor_lamports": atomic.PROFIT_LAMPORTS,
        "compute_limit": atomic.CU_LIMIT,
        "compute_price": atomic.CU_PRICE,
        "max_http": 4 + len(route.quote_legs) * len(inputs),
        "response_header_pacing_ns": evidence.NS // 4,
        "window_ns": WINDOW_NS,
        "max_quote_age_slots": MAX_SLOT_AGE,
        "commitment": "processed",
        "retries": 0,
        "source_sha256": {
            name: evidence.digest(Path(__file__).with_name(name).read_bytes())
            for name in SOURCES
        },
        "qualifications": [
            "unsigned_simulation_only_not_fills_or_paid_attempts",
            "native_prefixes_are_quotes_not_closed_cycle_returns",
            "equal_processed_slots_do_not_pin_a_bank_or_guarantee_inclusion",
            "one_fixed_route_size_sweep_not_independent_opportunity_capacity",
            "failed_or_unpriced_candidates_are_not_zero_return_trades",
            "spot_bounds_screen_snapshots_not_native_returns_or_future_prices",
        ],
    }


def account_params(keys: list[str], minimum: int = 0) -> list:
    params = evidence.account_params(keys, minimum)
    params[1]["commitment"] = "processed"
    return params


def validate_request(route: Route, request: object) -> dict:
    """Reject signing, broadcasts, overrides and unexpected RPC request shapes."""
    calls = request if isinstance(request, list) else [request]
    atomic.require(1 <= len(calls) <= 2, "request_count")
    atomic.require(
        [call.get("id") for call in calls] == list(range(len(calls))), "request_ids"
    )
    hashes = {}
    for call in calls:
        method, params = call["method"], call["params"]
        atomic.require(call.get("jsonrpc") == "2.0", "request_version")
        if method == "getGenesisHash":
            atomic.require(len(calls) == 1 and params == [], "genesis_options")
        elif method == "getMultipleAccounts":
            atomic.require(
                call["id"] == 0
                and len(params) == 2
                and params
                == account_params(params[0], params[1].get("minContextSlot", 0)),
                "account_options",
            )
        else:
            atomic.require(
                method == "simulateTransaction"
                and len(calls) == 2
                and calls[0]["method"] == "getMultipleAccounts"
                and call["id"] == 1,
                "unsigned_simulation_only",
            )
            tx = VersionedTransaction.from_bytes(
                base64.b64decode(params[0], validate=True)
            )
            expected = atomic.simulation_params(
                tx, calls[0]["params"][1]["minContextSlot"]
            )
            expected[1]["accounts"] = {
                "encoding": "base64",
                "addresses": list(map(str, route.atas)),
            }
            atomic.require(params == expected, "simulation_options")
            atomic.require(tx.message.account_keys[0] == PAYER, "unexpected_payer")
            hashes["1"] = evidence.digest(bytes(tx))
    return hashes


def wallet(route: Route, bank: dict, baseline: dict | None = None) -> dict:
    account = bank[str(PAYER)]
    atomic.checked_data(account, "11111111111111111111111111111111", 0)
    atomic.require(
        type(account["lamports"]) is int
        and 10_075_000 < account["lamports"] <= 2**64 - 1 - atomic.PROFIT_LAMPORTS,
        "payer_balance_range",
    )
    atomic.require(
        all(bank[str(ata)] is None for ata in route.atas), "payer_ata_present"
    )
    state = {key: account[key] for key in ("owner", "executable", "data", "lamports")}
    atomic.require(baseline is None or state == baseline, "wallet_baseline_changed")
    return state


def read_pools(route: Route, bank: dict) -> list:
    pools = []
    for i, (address, program) in enumerate(
        zip(route.pools, route.programs, strict=True)
    ):
        expected = (route.mints[i], route.mints[(i + 1) % len(route.mints)])
        if program == orca.PROGRAM:
            pool = orca.decode(address, bank[address], expected_mints=expected)
        elif program == damm.PROGRAM:
            pool = damm.decode(address, bank[address], expected_mints=expected)
        elif program == dlmm.PROGRAM:
            pool = dlmm.decode(
                address,
                bank[address],
                bank[dlmm.bitmap_address(address)],
                expected_mints=expected,
            )
        elif program == clmm.PROGRAM:
            pool = clmm.decode(
                address,
                bank[address],
                bank[clmm.bitmap_address(address)],
                expected_mints=expected,
            )
        else:
            atomic.require(program in atomic.AUTHORITY, "unsupported_route_program")
            pool = atomic.decode_pool(address, bank[address])
        atomic.require(
            pool.program == program and set(pool.mints) == set(expected),
            "route_pool_identity",
        )
        pools.append(pool)
    return pools


def dependencies(route: Route, pools: list) -> list[str]:
    return list(
        dict.fromkeys(
            [
                atomic.CLOCK,
                *route.wallet_keys,
                *route.lookups,
                *[address for pool in pools for address in pool.dependencies()],
                *[pool.observation for pool in pools if pool.program == atomic.CPMM],
            ]
        )
    )


def hydrate(route: Route, pools: list, bank: dict, slot: int) -> tuple[list, list]:
    for mint, decimals in zip(route.mints, route.decimals, strict=True):
        atomic.require(
            atomic.checked_data(bank[mint], atomic.SPL, 82)[44] == decimals,
            "mint_decimals",
        )
    live = []
    for pool in pools:
        if isinstance(pool, orca.Whirlpool):
            live.append(orca.hydrate(pool, bank))
        elif isinstance(pool, damm.DammPool):
            live.append(damm.hydrate(pool, bank, payer=PAYER))
        elif isinstance(pool, dlmm.DlmmPool):
            live.append(dlmm.hydrate(pool, bank, payer=PAYER))
        elif isinstance(pool, clmm.ClmmPool):
            live.append(clmm.hydrate(pool, bank))
        else:
            # Raydium hydration adds quote caches, not native instruction inputs.
            atomic.hydrate_pool(pool, bank)
            live.append(pool)
    tables = [
        native.decode_lookup(address, bank[address], slot) for address in route.lookups
    ]
    return live, tables


def _optimistic_ratio(route: Route, pools: list, bank: dict) -> Fraction | None:
    """Bound same-snapshot output/input, ignoring fees and adverse price impact."""
    if route.programs not in (
        (orca.PROGRAM, orca.PROGRAM, atomic.AMM),
        (orca.PROGRAM, orca.PROGRAM, atomic.CPMM),
    ):
        return None
    ratio = Fraction(1)
    for mint, pool in zip(route.mints[:2], pools[:2], strict=True):
        raw = atomic.checked_data(bank[pool.address], orca.PROGRAM, 653)
        sqrt_price = int.from_bytes(raw[65:81], "little")
        price = Fraction(sqrt_price * sqrt_price, 1 << 128)
        ratio *= price if mint == pool.mints[0] else 1 / price
    # Keep quote caches out of native dependency identities.
    ray = atomic.hydrate_pool(pools[2], bank)
    side = ray.mints.index(route.mints[2])
    return ratio * Fraction(ray.reserves[1 - side], ray.reserves[side])


def _screen_candidate(state: dict, amount: int, now: int) -> dict | None:
    """Reject only a fresh, fee-insufficient initial snapshot; never accept profit."""
    snapshot = state.get("screen")
    if snapshot is not None:
        ratio, slot, requested = snapshot
        if (
            0 <= now - requested <= WINDOW_NS
            and 0 <= state["slot"] - slot <= MAX_SLOT_AGE
        ):
            # Exact-output acquisition may spend any C <= amount, refunding the rest.
            gross = max(
                0, amount * (ratio.numerator - ratio.denominator) // ratio.denominator
            )
            bound = gross - atomic.NETWORK_FEE - atomic.TIP_LAMPORTS
            if bound < atomic.PROFIT_LAMPORTS:
                return {
                    "status": "screened_out",
                    "reason": "optimistic_profit_below_floor",
                    "net_lamports": None,
                    "optimistic_net_bound_lamports": bound,
                    "snapshot_slot": slot,
                    "snapshot_age_ns": now - requested,
                    "spot_ratio_numerator": str(ratio.numerator),
                    "spot_ratio_denominator": str(ratio.denominator),
                }
    # ponytail: startup screen only; reuse later attested banks if multi-size traffic warrants it.
    state.pop("screen", None)
    return None


def build_packet(  # noqa: PLR0913 - explicit route and native wire inputs
    route: Route,
    pools: list,
    tables: list,
    amount: int,
    quantities: list[int],
    baseline: int,
) -> VersionedTransaction:
    """Build native quote boundaries; Orca owns its internal two-hop quantity."""
    atomic.require(
        type(amount) is int and 0 < amount <= atomic.BUY_LAMPORTS, "input_cap"
    )
    atomic.require(
        len(quantities) < len(route.quote_legs)
        and all(type(n) is int and 0 < n < 2**64 for n in quantities),
        "intermediate_quantity",
    )
    atomic.require(
        type(baseline) is int and 0 < baseline <= 2**64 - 1 - atomic.PROFIT_LAMPORTS,
        "baseline_range",
    )
    instructions = [
        atomic.set_compute_unit_limit(atomic.CU_LIMIT),
        atomic.set_compute_unit_price(atomic.CU_PRICE),
    ]
    instructions += [
        atomic.create_idempotent_associated_token_account(
            PAYER, PAYER, atomic.Pubkey.from_string(mint)
        )
        for mint in route.mints
    ]
    instructions += [
        atomic.transfer(
            atomic.TransferParams(
                from_pubkey=PAYER, to_pubkey=route.atas[0], lamports=amount
            )
        ),
        atomic.sync_native(
            atomic.SyncNativeParams(atomic.TOKEN_PROGRAM_ID, route.atas[0])
        ),
    ]
    guarded = len(quantities) == len(route.quote_legs) - 1
    for start, end, quantity in zip(
        (0, *route.quote_legs[:-1]),
        route.quote_legs,
        (amount, *quantities),
        strict=False,
    ):
        if end - start == 2:
            exact_output = guarded and start == 0
            instructions.append(
                orca.two_hop_swap(
                    pools[start],
                    pools[start + 1],
                    PAYER,
                    route.mints[start],
                    quantities[0] if exact_output else quantity,
                    amount if exact_output else 1,
                    exact_output=exact_output,
                )
            )
        else:
            instructions.append(
                atomic.swap(
                    pools[start],
                    PAYER,
                    route.mints[start],
                    quantity,
                    1,
                    exact_output=False,
                )
            )
    if guarded:
        if atomic.TIP_LAMPORTS:
            instructions.append(
                atomic.transfer(
                    atomic.TransferParams(
                        from_pubkey=PAYER,
                        to_pubkey=atomic.Pubkey.from_string(atomic.TIP_ACCOUNT),
                        lamports=atomic.TIP_LAMPORTS,
                    )
                )
            )
        instructions += [
            atomic.close_account(
                atomic.CloseAccountParams(atomic.TOKEN_PROGRAM_ID, ata, PAYER, PAYER)
            )
            for ata in reversed(route.atas)
        ]
        instructions.append(
            atomic.transfer(
                atomic.TransferParams(
                    from_pubkey=PAYER,
                    to_pubkey=PAYER,
                    lamports=baseline + atomic.PROFIT_LAMPORTS,
                )
            )
        )
    return native.compile_unsigned(PAYER, instructions, tables)


def simulation_calls(
    route: Route, tx: VersionedTransaction, keys: list[str], slot: int
) -> list:
    params = atomic.simulation_params(tx, slot)
    params[1]["accounts"] = {
        "encoding": "base64",
        "addresses": list(map(str, route.atas)),
    }
    return [
        evidence.single("getMultipleAccounts", account_params(keys, slot)),
        {"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction", "params": params},
    ]


def native_result(  # noqa: PLR0913 - explicit route and native evidence
    route: Route,
    tx: VersionedTransaction,
    tables: list,
    result: dict,
    baseline: int,
    legs: int,
) -> dict:
    """Only a fully closed native cycle can report fee-inclusive profit."""
    keys = native.resolve_keys(tx, tables)
    value = result["value"]
    before, after = value.get("preBalances"), value.get("postBalances")
    atomic.require(
        isinstance(before, list)
        and isinstance(after, list)
        and len(before) == len(after) == len(keys)
        and all(type(n) is int and 0 <= n < 2**64 for n in before + after),
        "native_balance_shape",
    )
    atomic.require(
        keys[0] == PAYER and before[0] == baseline, "wallet_baseline_changed"
    )
    atomic.require(
        type(value.get("fee")) is int and value["fee"] == atomic.NETWORK_FEE,
        "native_fee_mismatch",
    )
    indices = [keys.index(ata) for ata in route.atas]
    atomic.require(
        all(before[index] == 0 for index in indices), "preexisting_inventory"
    )
    atomic.require(
        "err" in value
        and (value["err"] is None or isinstance(value["err"], str | dict)),
        "native_error_shape",
    )
    row = {
        "status": "native_failure",
        "err": value["err"],
        "simulation_slot": result["context"]["slot"],
        "fee_lamports": value["fee"],
        "units": value.get("unitsConsumed"),
        "net_lamports": None,
        "quantity": None,
        "wire_bytes": len(bytes(tx)),
    }
    if legs == len(route.pools):
        guard = tx.message.instructions[-1]
        atomic.require(
            str(keys[guard.program_id_index]) == "11111111111111111111111111111111"
            and list(guard.accounts) == [0, 0]
            and guard.data == struct.pack("<IQ", 2, baseline + atomic.PROFIT_LAMPORTS),
            "wallet_guard_missing",
        )
    if value["err"] is not None:
        atomic.require(
            after[0] == baseline - atomic.NETWORK_FEE
            and all(after[i] == 0 for i in indices),
            "failed_native_delta",
        )
        if legs == len(route.pools) and value["err"] == {
            "InstructionError": [len(tx.message.instructions) - 1, {"Custom": 1}]
        }:
            row["status"] = "guard_rejected"
        return row
    accounts = value.get("accounts")
    atomic.require(
        isinstance(accounts, list) and len(accounts) == len(route.mints),
        "native_accounts_missing",
    )
    if legs == len(route.pools):
        atomic.require(
            all(account is None for account in accounts)
            and all(after[i] == 0 for i in indices),
            "rent_or_inventory_not_closed",
        )
        net = after[0] - before[0]
        atomic.require(net >= atomic.PROFIT_LAMPORTS, "native_profit_violation")
        row.update(status="native_guarded_success", net_lamports=net)
    else:
        amounts = []
        for mint, account, index in zip(route.mints, accounts, indices, strict=True):
            raw = atomic.checked_data(account, atomic.SPL, 165)
            atomic.require(
                atomic.key(raw, 0) == mint
                and atomic.key(raw, 32) == str(PAYER)
                and raw[108] == 1
                and account["lamports"] == after[index],
                "prefix_account_identity",
            )
            amounts.append(atomic.u64(raw, 64))
        atomic.require(
            1 <= legs < len(route.pools)
            and amounts[legs] > 0
            and all(n == 0 for i, n in enumerate(amounts) if i != legs),
            "prefix_inventory_not_consumed",
        )
        row.update(
            status="quote_only",
            quantity=amounts[legs],
            temporary_account_lamports=sum(after[i] for i in indices),
        )
    return row


def fresh_result(
    result: dict, slot: int, first_slot: int | None, deadline: int
) -> None:
    observed = result.get("context", {}).get("slot")
    atomic.require(
        type(observed) is int and observed == slot, "state_simulation_slot_mismatch"
    )
    atomic.require(
        first_slot is None or 0 <= slot - first_slot <= MAX_SLOT_AGE, "quote_slot_age"
    )
    atomic.require(time.monotonic_ns() <= deadline, "candidate_deadline_expired")


def safe_reason(exc: BaseException) -> str:
    if isinstance(exc, ValueError) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}", str(exc)):
        return str(exc)
    return evidence.safe_code(exc)


async def candidate(  # noqa: PLR0915 - keep quote and acceptance gates together
    route: Route, rpc: evidence.RPC, state: dict, amount: int, index: int
) -> dict:
    """Bound snapshot/native quote age through guarded native validation."""
    pools, tables, baseline = (state[key] for key in ("pools", "tables", "baseline"))
    slot = state["slot"]
    started = time.monotonic_ns()
    deadline = started + WINDOW_NS
    native_snapshot = state.pop("native_snapshot", None)
    if native_snapshot is not None:
        snapshot = state.get("quote_snapshot")
        _, _, requested, expires = native_snapshot
        atomic.require(
            route.programs[0] not in atomic.AUTHORITY
            and snapshot is not None
            and snapshot[:2] == (slot, requested),
            "native_snapshot_source",
        )
        deadline = min(deadline, requested + WINDOW_NS, expires)
        atomic.require(requested <= started <= deadline, "native_snapshot_expired")
    screened = _screen_candidate(state, amount, started)
    if screened is not None:
        return screened
    quantities = []
    first_slot = None
    keys = dependencies(route, pools)
    # ponytail: seed once; later standalone sizes retain native-prefix discovery.
    snapshot = state.pop("quote_snapshot", None)
    if snapshot is not None and route.programs[0] in atomic.AUTHORITY:
        first_slot, requested, bank = snapshot
        atomic.require(first_slot == slot, "quote_snapshot_slot")
        atomic.require(0 <= started - requested <= WINDOW_NS, "quote_snapshot_expired")
        deadline = min(deadline, requested + WINDOW_NS)
        for leg in range(len(pools) - 1):
            if route.programs[leg] not in atomic.AUTHORITY:
                break
            quantities.append(
                atomic.hydrate_pool(pools[leg], bank).quote(
                    route.mints[leg], quantities[-1] if quantities else amount
                )
            )
    for legs in route.quote_legs[len(quantities) :]:
        tx = build_packet(
            route, pools, tables, amount, quantities, baseline["lamports"]
        )
        if native_snapshot is not None:
            request, response, _, _ = native_snapshot
            validate_request(route, request)
            response_keys = request[0]["params"][0]
            minimum = request[0]["params"][1]["minContextSlot"]
            atomic.require(
                request == simulation_calls(route, tx, response_keys, minimum),
                "candidate_dependencies_changed",
            )
            current_slot, bank = evidence.bank_result(response[0], response_keys)
            atomic.require(minimum <= current_slot == slot, "native_snapshot_slot")
            native_snapshot = None
        else:
            response = await rpc.call(
                f"prefix_{legs}" if legs < len(route.pools) else "guarded",
                simulation_calls(route, tx, keys, slot),
                deadline,
                index,
            )
            current_slot, bank = evidence.bank_result(response[0], keys)
        state["slot"] = max(state["slot"], current_slot)
        native_slot = response[1].get("context", {}).get("slot")
        if type(native_slot) is int:
            state["slot"] = max(state["slot"], native_slot)
        fresh_result(response[1], current_slot, first_slot, deadline)
        atomic.require(current_slot >= slot, "snapshot_slot_regressed")
        wallet(route, bank, baseline)
        live, current_tables = hydrate(route, pools, bank, current_slot)
        rebuilt = build_packet(
            route, live, current_tables, amount, quantities, baseline["lamports"]
        )
        atomic.require(bytes(rebuilt) == bytes(tx), "candidate_dependencies_changed")
        row = native_result(
            route, tx, current_tables, response[1], baseline["lamports"], legs
        )
        fresh_result(response[1], current_slot, first_slot, deadline)
        validated_at = time.monotonic_ns()
        atomic.require(validated_at <= deadline, "candidate_deadline_expired")
        rpc.tape.emit(
            "stage", candidate=index, legs=legs, validated_ns=validated_at, **row
        )
        if row["status"] != "quote_only":
            return {
                **row,
                "elapsed_ns": validated_at - started,
                "first_quote_slot": first_slot,
                "intermediate_quantities": quantities,
            }
        quantities.append(row["quantity"])
        first_slot = current_slot if first_slot is None else first_slot
        slot, tables = current_slot, current_tables
    raise ValueError("missing_guarded_result")


async def observe(  # noqa: C901, PLR0912, PLR0915 - one terminal evidence state machine
    route: Route, url: str, secrets: tuple[str, ...], tape: evidence.Tape, policy: dict
) -> dict:
    """One bounded sweep; fail on transport errors and audit the unchanged wallet."""
    baseline = None
    state = {"slot": 0}
    outcomes = []
    failure = None
    interruption = None
    audit = {"unchanged": False, "reason": "baseline_unavailable"}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None),
        auto_decompress=False,
        trust_env=False,
    ) as session:
        rpc = evidence.RPC(
            session,
            url,
            secrets,
            tape,
            policy,
            validate_request=partial(validate_request, route),
        )
        try:
            identity = await rpc.call(
                "identity",
                evidence.single("getGenesisHash", []),
                time.monotonic_ns() + 10 * evidence.NS,
            )
            atomic.require(identity[0] == evidence.GENESIS, "wrong_chain")
            keys = route.metadata_keys
            response = await rpc.call(
                "metadata",
                evidence.single("getMultipleAccounts", account_params(keys)),
                time.monotonic_ns() + 10 * evidence.NS,
            )
            slot, bank = evidence.bank_result(response[0], keys)
            state["slot"] = slot
            baseline = wallet(route, bank)
            pools = read_pools(route, bank)
            keys = dependencies(route, pools)
            snapshot_started = time.monotonic_ns()
            response = await rpc.call(
                "dependencies",
                evidence.single("getMultipleAccounts", account_params(keys, slot)),
                time.monotonic_ns() + 10 * evidence.NS,
            )
            current_slot, bank = evidence.bank_result(response[0], keys)
            state["slot"] = max(slot, current_slot)
            atomic.require(current_slot >= slot, "snapshot_slot_regressed")
            wallet(route, bank, baseline)
            pools, tables = hydrate(route, pools, bank, current_slot)
            ratio = _optimistic_ratio(route, pools, bank)
            state.update(
                pools=pools,
                tables=tables,
                baseline=baseline,
                quote_snapshot=(current_slot, snapshot_started, bank),
                screen=(ratio, current_slot, snapshot_started)
                if ratio is not None
                else None,
            )
            for index, amount in enumerate(policy["inputs_lamports"]):
                outcomes.append(
                    {
                        "input_lamports": amount,
                        "status": "started",
                        "net_lamports": None,
                    }
                )
                try:
                    row = await candidate(route, rpc, state, amount, index)
                except (ValueError, KeyError, IndexError, struct.error) as exc:
                    row = {
                        "status": "refused",
                        "reason": safe_reason(exc),
                        "net_lamports": None,
                    }
                except (Exception, asyncio.CancelledError) as exc:
                    outcomes[-1].update(status="failed", reason=safe_reason(exc))
                    raise
                outcomes[-1].update(row)
                tape.emit("candidate", candidate=index, **outcomes[-1])
        except (Exception, asyncio.CancelledError) as exc:
            failure = safe_reason(exc)
            if isinstance(exc, asyncio.CancelledError):
                interruption = exc
        finally:
            if baseline is not None and interruption is None:
                try:
                    response = await rpc.call(
                        "audit",
                        evidence.single(
                            "getMultipleAccounts",
                            account_params(route.wallet_keys, state["slot"]),
                        ),
                        time.monotonic_ns() + 5 * evidence.NS,
                    )
                    audit_slot, bank = evidence.bank_result(
                        response[0], route.wallet_keys
                    )
                    atomic.require(audit_slot >= state["slot"], "audit_slot_regressed")
                    wallet(route, bank, baseline)
                    audit = {
                        "unchanged": True,
                        "slot": audit_slot,
                        "minimum_slot": state["slot"],
                    }
                except (Exception, asyncio.CancelledError) as exc:
                    audit = {"unchanged": False, "reason": safe_reason(exc)}
                    failure = failure or "wallet_audit_failed"
                    if isinstance(exc, asyncio.CancelledError):
                        interruption, failure = exc, "interrupted"
            elif interruption is not None:
                audit = {"unchanged": False, "reason": "interrupted"}
            summary = {
                "completed": failure is None
                and len(outcomes) == len(policy["inputs_lamports"])
                and audit["unchanged"],
                "failure": failure,
                "candidates": outcomes,
                "unattempted_inputs": policy["inputs_lamports"][len(outcomes) :],
                "wallet_audit": audit,
                "http_requests": rpc.count,
                "signed_transactions": 0,
                "submitted_transactions": 0,
                "observed_live_roi": None,
            }
            try:
                tape.emit("terminal", reserved=True, **summary)
            except BaseException as exc:
                if interruption is not None:
                    raise interruption from exc
                raise
            if interruption is not None:
                raise interruption
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("self-check")
    run = modes.add_parser("observe")
    run.add_argument("--route", choices=ROUTES, default="orca")
    provider = run.add_mutually_exclusive_group(required=True)
    provider.add_argument("--public-rpc", action="store_true")
    provider.add_argument("--provider-json", type=Path)
    run.add_argument(
        "--inputs", type=int, nargs="+", default=[100_000, 1_000_000, 10_000_000]
    )
    run.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "self-check":
            from verify_public_triangle import verify  # noqa: PLC0415 - offline only

            asyncio.run(verify())
            return 0
        route = ROUTES[args.route]
        policy = scope(route, args.inputs)
        atomic.require(args.out.suffix == ".jsonl", "evidence_jsonl_required")
        url, secrets = (
            (PUBLIC_RPC, ())
            if args.public_rpc
            else evidence.load_provider(args.provider_json)
        )
        tape = evidence.Tape(args.out, policy)
        try:
            tape.emit(
                "manifest", scope=policy, started_at_utc=datetime.now(UTC).isoformat()
            )
            summary = asyncio.run(observe(route, url, secrets, tape, policy))
        finally:
            pending = sys.exception()
            try:
                tape.file.close()
            except BaseException as exc:
                if isinstance(pending, asyncio.CancelledError | KeyboardInterrupt):
                    raise pending from exc
                raise
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary["completed"] else 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('{"event":"fatal","reason":"interrupted"}', file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - credential-free CLI failure boundary
        print(
            json.dumps({"event": "fatal", "reason": safe_reason(exc)}), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
