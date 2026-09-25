"""Ordinary-SPL DLMM boundary; native swap2 owns prices, fees and fill sufficiency.

Contract: MeteoraAg/dlmm-sdk at 576919e3e4368e542c402f000b4264724f7f23ec,
idls/dlmm.json and commons/src/{pda.rs,quote.rs,extensions/bin_array_bitmap.rs}.
Three initialized arrays per direction bound coverage, not protocol liquidity.
No local quote math, router replay, token hooks or synthetic account fallbacks.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

import simulate_atomic_cycles as atomic

# Native layouts, enum values, bitmap widths and the bounded array selection.
# ruff: noqa: PLR2004
PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


def pda(*seeds: bytes) -> str:
    return str(
        atomic.Pubkey.find_program_address(
            list(seeds), atomic.Pubkey.from_string(PROGRAM)
        )[0]
    )


def bitmap_address(address: str) -> str:
    return pda(b"bitmap", bytes(atomic.Pubkey.from_string(address)))


EVENT_AUTHORITY = pda(b"__event_authority")


def select_arrays(
    active: int, bitmaps: tuple[int, int, int], *, decreasing: bool
) -> tuple[int, ...]:
    """Traverse set bits across internal and signed extension ranges, without padding."""
    internal, positive, negative = bitmaps
    regions = (
        (positive, 512, 1, 6144),
        (internal, -512, 1, 1024),
        (negative, -513, -1, 6144),
    )
    selected = []
    for bitmap, base, sign, width in regions if decreasing else reversed(regions):
        start = (active // 70 - base) * sign
        reverse = decreasing == (sign == 1)
        if reverse:
            if start < 0:
                continue
            mask = bitmap & ((1 << min(width, start + 1)) - 1)
            offset = 0
        else:
            if start >= width:
                continue
            offset = max(start, 0)
            mask = bitmap >> offset
        while mask and len(selected) < 3:
            bit = mask.bit_length() - 1 if reverse else (mask & -mask).bit_length() - 1
            selected.append(base + sign * (bit + offset))
            mask ^= 1 << bit
        if len(selected) == 3:
            break
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class DlmmPool:
    """Attested references and bounded native dependencies, never cached prices."""

    address: str
    program: str
    mints: tuple[str, str]
    vaults: tuple[str, str]
    oracle: str
    bitmap: str
    bitmap_present: bool
    arrays: tuple[tuple[tuple[str, int], ...], ...]
    activation_type: int
    activation_point: int
    pre_activation_address: str
    pre_activation_duration: int

    def dependencies(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    self.address,
                    *self.mints,
                    *self.vaults,
                    self.oracle,
                    self.bitmap,
                    *[address for direction in self.arrays for address, _ in direction],
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
        """Only user ATAs and bin traversal reverse; stored X/Y metas do not."""
        atomic.require(
            input_mint in self.mints
            and type(amount) is int
            and type(bound) is int
            and 0 < amount < 2**64
            and 0 < bound < 2**64
            and type(exact_output) is bool,
            "dlmm_swap_input",
        )
        side = self.mints.index(input_mint)
        atomic.require(bool(self.arrays[side]), "dlmm_no_array_coverage")
        accounts = [
            atomic.meta(self.address, writable=True),
            atomic.meta(
                self.bitmap if self.bitmap_present else PROGRAM,
                writable=self.bitmap_present,
            ),
            *[atomic.meta(vault, writable=True) for vault in self.vaults],
            *[
                atomic.meta(
                    atomic.get_associated_token_address(
                        payer, atomic.Pubkey.from_string(self.mints[i])
                    ),
                    writable=True,
                )
                for i in (side, 1 - side)
            ],
            *[atomic.meta(mint) for mint in self.mints],
            atomic.meta(self.oracle, writable=True),
            atomic.meta(PROGRAM),
            atomic.meta(payer, signer=True),
            atomic.meta(atomic.SPL),
            atomic.meta(atomic.SPL),
            atomic.meta(MEMO),
            atomic.meta(EVENT_AUTHORITY),
            atomic.meta(PROGRAM),
            *[atomic.meta(address, writable=True) for address, _ in self.arrays[side]],
        ]
        data = bytes.fromhex("2bd7f784893cf351" if exact_output else "414b3f4ceb5b5b88")
        data += struct.pack(
            "<QQI",
            bound if exact_output else amount,
            amount if exact_output else bound,
            0,
        )
        return atomic.Instruction(atomic.Pubkey.from_string(PROGRAM), data, accounts)


def decode(
    address: str,
    account: dict | None,
    bitmap_account: dict | None,
    *,
    expected_mints: tuple[str, str],
) -> DlmmPool:
    """Attest one pool plus its proven-present or proven-null bitmap extension."""
    raw = atomic.checked_data(account, PROGRAM, 904)
    atomic.require(raw[:8] == bytes.fromhex("210b3162b565b10d"), "dlmm_pool_identity")
    atomic.require(raw[880:882] == bytes(2), "dlmm_spl_only")
    atomic.require(
        raw[82] == 0
        and raw[75] in (0, 1, 2, 3)
        and raw[86] in (0, 1)
        and raw[35] in (0, 1, 2)
        and raw[36] in (0, 1),
        "dlmm_pool_state",
    )
    minimum, maximum = struct.unpack_from("<ii", raw, 24)
    active = struct.unpack_from("<i", raw, 76)[0]
    step = struct.unpack_from("<H", raw, 80)[0]
    atomic.require(
        -443636 <= minimum <= active <= maximum <= 443636 and 0 < step <= 400,
        "dlmm_bin_range",
    )
    mints = (atomic.key(raw, 88), atomic.key(raw, 120))
    atomic.require(
        len(expected_mints) == len(set(expected_mints)) == 2
        and set(mints) == set(expected_mints),
        "dlmm_mint_identity",
    )
    pool_bytes = bytes(atomic.Pubkey.from_string(address))
    vaults = (atomic.key(raw, 152), atomic.key(raw, 184))
    oracle = atomic.key(raw, 552)
    atomic.require(
        vaults
        == tuple(
            pda(pool_bytes, bytes(atomic.Pubkey.from_string(mint))) for mint in mints
        )
        and oracle == pda(b"oracle", pool_bytes),
        "dlmm_account_identity",
    )
    bitmaps = (int.from_bytes(raw[584:712], "little"), 0, 0)
    if bitmap_account is not None:
        extension = atomic.checked_data(bitmap_account, PROGRAM, 1576)
        atomic.require(
            extension[:8] == bytes.fromhex("506f7c7137ed1205")
            and atomic.key(extension, 8) == address,
            "dlmm_bitmap_identity",
        )
        bitmaps = (
            bitmaps[0],
            int.from_bytes(extension[40:808], "little"),
            int.from_bytes(extension[808:1576], "little"),
        )
    arrays = []
    for decreasing in (True, False):
        indices = select_arrays(active, bitmaps, decreasing=decreasing)
        atomic.require(
            all(
                index * 70 <= maximum and index * 70 + 69 >= minimum
                for index in indices
            ),
            "dlmm_array_range",
        )
        arrays.append(
            tuple(
                (pda(b"bin_array", pool_bytes, struct.pack("<q", index)), index)
                for index in indices
            )
        )
    return DlmmPool(
        address,
        PROGRAM,
        mints,
        vaults,
        oracle,
        bitmap_address(address),
        bitmap_account is not None,
        tuple(arrays),
        raw[86],
        atomic.u64(raw, 816) if raw[75] in (1, 2) else 0,
        atomic.key(raw, 752),
        atomic.u64(raw, 824),
    )


def hydrate(pool: DlmmPool, bank: dict, *, payer: atomic.Pubkey) -> DlmmPool:
    """Rederive current dependencies and validate public access in this bank."""
    live = decode(
        pool.address, bank[pool.address], bank[pool.bitmap], expected_mints=pool.mints
    )
    atomic.require(
        (live.mints, live.vaults, live.oracle)
        == (pool.mints, pool.vaults, pool.oracle),
        "dlmm_references_changed",
    )
    atomic.require(
        all(address in bank for address in live.dependencies()),
        "dlmm_array_dependencies_changed",
    )
    for mint, vault in zip(live.mints, live.vaults, strict=True):
        mint_raw = atomic.checked_data(bank[mint], atomic.SPL, 82)
        token = atomic.checked_data(bank[vault], atomic.SPL, 165)
        atomic.require(
            mint_raw[45] == 1
            and token[108] == 1
            and atomic.key(token, 0) == mint
            and atomic.key(token, 32) == live.address,
            "dlmm_mint_or_vault_identity",
        )
    for address, index in dict(live.arrays[0] + live.arrays[1]).items():
        raw = atomic.checked_data(bank[address], PROGRAM, 10136)
        atomic.require(
            raw[:8] == bytes.fromhex("5c8e5cdc059446b5")
            and struct.unpack_from("<q", raw, 8)[0] == index
            and atomic.key(raw, 24) == live.address,
            "dlmm_array_identity",
        )
    oracle_account = bank[live.oracle]
    atomic.require(oracle_account is not None, "dlmm_oracle_missing")
    oracle = base64.b64decode(oracle_account["data"][0], validate=True)
    atomic.checked_data(oracle_account, PROGRAM, len(oracle))
    atomic.require(
        len(oracle) >= 32 and oracle[:8] == bytes.fromhex("8bc283b38cb3e5f4"),
        "dlmm_oracle_identity",
    )
    index, size, capacity = struct.unpack_from("<QQQ", oracle, 8)
    atomic.require(
        len(oracle) == 32 + 32 * capacity
        and 0 <= index < capacity
        and size <= capacity,
        "dlmm_oracle_layout",
    )
    clock = atomic.checked_data(
        bank[atomic.CLOCK], "Sysvar1111111111111111111111111111111111111", 40
    )
    point = live.activation_point
    if point and str(payer) == live.pre_activation_address:
        atomic.require(live.pre_activation_duration <= point, "dlmm_activation_range")
        point -= live.pre_activation_duration
    now = (
        atomic.u64(clock, 0)
        if live.activation_type == 0
        else struct.unpack_from("<q", clock, 32)[0]
    )
    atomic.require(now >= point, "dlmm_not_active")
    return live
