"""Ordinary-SPL Whirlpool boundary; the native program owns all swap math."""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

import simulate_atomic_cycles as atomic

# Binary offsets, tick bounds and fee ceilings are protocol constants.
# ruff: noqa: PLR2004

PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
AMM_POOL = "Bzc9NZfMqkXR6fz1DBph7BDf9BroyEf6pnzESP7v5iiw"
WHIRLPOOL = "Tuy6gMupGQN7wCZ8rVP1EuLRYB132VSo9Smy4AJvQgn"


def discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


def pda(*seeds: bytes) -> str:
    return str(
        atomic.Pubkey.find_program_address(
            list(seeds), atomic.Pubkey.from_string(PROGRAM)
        )[0]
    )


@dataclass(frozen=True, slots=True)
class Whirlpool:
    address: str
    program: str
    mints: tuple[str, str]
    vaults: tuple[str, str]
    config: str
    spacing: int
    fee_tier: int
    tick: int

    def ticks(self, *, a_to_b: bool) -> list[tuple[str, int]]:
        """Up to three in-range arrays, padded as in the official swap SDK."""
        span = 88 * self.spacing
        base = self.tick // span * span
        shift = int(not a_to_b and self.tick + self.spacing >= base + span)
        starts = [base + (shift + (-i if a_to_b else i)) * span for i in range(3)]
        starts = [
            start for start in starts if (-443636 // span) * span <= start <= 443636
        ]
        atomic.require(bool(starts), "orca_tick_range_unsupported")
        arrays = [
            (
                pda(
                    b"tick_array",
                    bytes(atomic.Pubkey.from_string(self.address)),
                    str(start).encode(),
                ),
                start,
            )
            for start in starts
        ]
        # SparseSwapTickSequenceBuilder deduplicates these required account slots.
        return arrays + [arrays[-1]] * (3 - len(arrays))

    @property
    def oracle(self) -> str:
        return pda(b"oracle", bytes(atomic.Pubkey.from_string(self.address)))

    def dependencies(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    self.address,
                    self.config,
                    *self.mints,
                    *self.vaults,
                    self.oracle,
                    *[
                        address
                        for direction in (True, False)
                        for address, _ in self.ticks(a_to_b=direction)
                    ],
                ]
            )
        )

    def swap(
        self,
        payer: atomic.Pubkey,
        input_mint: str,
        amount: int,
        bound: int,
        *,
        exact_output: bool,
    ) -> atomic.Instruction:
        side = self.mints.index(input_mint)
        accounts = [
            atomic.meta(atomic.SPL),
            atomic.meta(payer, signer=True),
            atomic.meta(self.address, writable=True),
        ]
        for mint, vault in zip(self.mints, self.vaults, strict=True):
            accounts += [
                atomic.meta(
                    atomic.get_associated_token_address(
                        payer, atomic.Pubkey.from_string(mint)
                    ),
                    writable=True,
                ),
                atomic.meta(vault, writable=True),
            ]
        accounts += [
            atomic.meta(address, writable=True)
            for address, _ in self.ticks(a_to_b=side == 0)
        ]
        # Current legacy swap supports adaptive fees only with a writable oracle.
        accounts.append(atomic.meta(self.oracle, writable=True))
        data = (
            bytes.fromhex("f8c69e91e17587c8")
            + struct.pack("<QQ", amount, bound)
            + bytes(16)
            + bytes((not exact_output, side == 0))
        )
        return atomic.Instruction(atomic.Pubkey.from_string(PROGRAM), data, accounts)


def two_hop_swap(  # noqa: PLR0913
    first: Whirlpool,
    second: Whirlpool,
    payer: atomic.Pubkey,
    input_mint: str,
    amount: int,
    bound: int,
    *,
    exact_output: bool,
) -> atomic.Instruction:
    """Native two-hop with a minimum output or maximum input, without a middle quote."""
    atomic.require(
        type(amount) is int
        and 0 < amount < 2**64
        and type(bound) is int
        and 0 <= bound < 2**64
        and type(exact_output) is bool,
        "orca_two_hop_amount",
    )
    pools = (first, second)
    atomic.require(
        all(
            isinstance(pool, Whirlpool)
            and pool.program == PROGRAM
            and pool.config == CONFIG
            and len(pool.mints) == len(set(pool.mints)) == len(pool.vaults) == 2
            and pool.spacing > 0
            and -443636 <= pool.tick <= 443636
            for pool in pools
        ),
        "orca_two_hop_pool",
    )
    atomic.require(first.address != second.address, "orca_two_hop_duplicate_pool")
    atomic.require(input_mint in first.mints, "orca_two_hop_input_mint")
    side_one = first.mints.index(input_mint)
    intermediate = first.mints[1 - side_one]
    atomic.require(intermediate in second.mints, "orca_two_hop_intermediate_mint")
    directions = (side_one == 0, second.mints.index(intermediate) == 0)
    # hydrate attests SPL mint/vault owners; legacy TwoHopSwap has no Token-2022 path.
    accounts = [
        atomic.meta(atomic.SPL),
        atomic.meta(payer, signer=True),
        *[atomic.meta(pool.address, writable=True) for pool in pools],
    ]
    for pool in pools:
        for mint, vault in zip(pool.mints, pool.vaults, strict=True):
            accounts += [
                atomic.meta(
                    atomic.get_associated_token_address(
                        payer, atomic.Pubkey.from_string(mint)
                    ),
                    writable=True,
                ),
                atomic.meta(vault, writable=True),
            ]
    for pool, a_to_b in zip(pools, directions, strict=True):
        accounts += [
            atomic.meta(address, writable=True)
            for address, _ in pool.ticks(a_to_b=a_to_b)
        ]
    # As in the official legacy SDK, both oracle slots must support adaptive fees.
    accounts += [atomic.meta(pool.oracle, writable=True) for pool in pools]
    # Canonical two_hop_swap: u64, u64, bool, bool, bool, u128, u128.
    # Zero sqrt limits delegate directional bounds to the native swap manager.
    data = (
        bytes.fromhex("c360ed6c44a2dbe6")
        + struct.pack("<QQ", amount, bound)
        + bytes((not exact_output, *directions))
        + bytes(32)
    )
    return atomic.Instruction(atomic.Pubkey.from_string(PROGRAM), data, accounts)


def decode(
    address: str, account: dict | None, *, expected_mints: tuple[str, str]
) -> Whirlpool:
    """Attest a caller-frozen pool and explicit mint pair under the canonical config."""
    raw = atomic.checked_data(account, PROGRAM, 653)
    atomic.require(
        raw[:8] == discriminator("Whirlpool") and atomic.key(raw, 8) == CONFIG,
        "orca_pool_identity",
    )
    mints = (atomic.key(raw, 101), atomic.key(raw, 181))
    atomic.require(
        len(expected_mints) == len(set(expected_mints)) == 2
        and set(mints) == set(expected_mints),
        "orca_mint_identity",
    )
    spacing, fee_tier = struct.unpack_from("<HH", raw, 41)
    tick = struct.unpack_from("<i", raw, 81)[0]
    atomic.require(spacing > 0 and -443636 <= tick <= 443636, "orca_tick_state")
    atomic.require(
        4295048016
        <= int.from_bytes(raw[65:81], "little")
        <= 79226673515401279992447579055,
        "orca_sqrt_price",
    )
    return Whirlpool(
        address,
        PROGRAM,
        mints,
        (atomic.key(raw, 133), atomic.key(raw, 213)),
        CONFIG,
        spacing,
        fee_tier,
        tick,
    )


def hydrate(pool: Whirlpool, bank: dict) -> Whirlpool:
    """Attest current dependencies, never use fixed-fee/constant-product pricing."""
    live = decode(
        pool.address,
        bank[pool.address],
        expected_mints=pool.mints,
    )
    atomic.require(
        (live.mints, live.vaults, live.config, live.spacing, live.fee_tier)
        == (pool.mints, pool.vaults, pool.config, pool.spacing, pool.fee_tier),
        "orca_references_changed",
    )
    atomic.require(
        all(address in bank for address in live.dependencies()),
        "orca_tick_dependencies_changed",
    )
    config = atomic.checked_data(bank[live.config], PROGRAM, 108)
    atomic.require(
        config[:8] == discriminator("WhirlpoolsConfig"), "orca_config_discriminator"
    )
    for mint, vault in zip(live.mints, live.vaults, strict=True):
        mint_raw = atomic.checked_data(bank[mint], atomic.SPL, 82)
        raw = atomic.checked_data(bank[vault], atomic.SPL, 165)
        atomic.require(mint_raw[45] == 1 and raw[108] == 1, "orca_mint_or_vault_state")
        atomic.require(
            atomic.key(raw, 0) == mint and atomic.key(raw, 32) == live.address,
            "orca_vault_identity",
        )
    for address, start in dict(
        live.ticks(a_to_b=True) + live.ticks(a_to_b=False)
    ).items():
        account = bank[address]  # Proven null is distinct from an omitted response.
        if account is None:
            continue
        if account.get("owner") == "11111111111111111111111111111111":
            atomic.checked_data(account, "11111111111111111111111111111111", 0)
            continue
        atomic.require(account["data"][1] == "base64", "orca_tick_encoding")
        data = base64.b64decode(account["data"][0], validate=True)
        atomic.checked_data(account, PROGRAM, len(data))
        if data[:8] == discriminator("TickArray"):
            atomic.require(len(data) == 9988, "orca_fixed_tick_layout")
            pool_offset = 9956
        else:
            atomic.require(
                data[:8] == discriminator("DynamicTickArray")
                and 148 <= len(data) <= 10004,
                "orca_dynamic_tick_layout",
            )
            bitmap = int.from_bytes(data[44:60], "little")
            atomic.require(bitmap >> 88 == 0, "orca_tick_bitmap")
            offset = 60
            for i in range(88):
                initialized = (bitmap >> i) & 1
                atomic.require(
                    offset < len(data) and data[offset] == initialized,
                    "orca_dynamic_tick_data",
                )
                offset += 1 + 112 * initialized
            atomic.require(offset <= len(data), "orca_dynamic_tick_length")
            pool_offset = 12
        atomic.require(
            struct.unpack_from("<i", data, 8)[0] == start
            and atomic.key(data, pool_offset) == live.address,
            "orca_tick_identity",
        )
    if live.fee_tier == live.spacing:
        # Static pools may omit the oracle; never substitute this for adaptive fees.
        if bank[live.oracle] is not None:
            atomic.checked_data(
                bank[live.oracle], "11111111111111111111111111111111", 0
            )
        return live
    oracle = atomic.checked_data(bank[live.oracle], PROGRAM, 254)
    atomic.require(
        oracle[:8] == discriminator("Oracle") and atomic.key(oracle, 8) == live.address,
        "orca_oracle_identity",
    )
    clock = atomic.checked_data(
        bank[atomic.CLOCK], "Sysvar1111111111111111111111111111111111111", 40
    )
    timestamp = struct.unpack_from("<q", clock, 32)[0]
    atomic.require(
        atomic.u64(oracle, 40) <= timestamp
        and max(atomic.u64(oracle, 82), atomic.u64(oracle, 90)) <= timestamp,
        "orca_oracle_timestamp",
    )
    filter_period, decay, reduction, control, maximum, group, major = (
        struct.unpack_from("<HHHIIHH", oracle, 48)
    )
    atomic.require(
        0 < filter_period < decay
        and reduction < 10000
        and control < 100000
        and 0 < group <= live.spacing
        and live.spacing % group == 0
        and maximum * group <= 2**32 - 1
        and 0 < major <= 88 * live.spacing,
        "orca_adaptive_constants",
    )
    return live
