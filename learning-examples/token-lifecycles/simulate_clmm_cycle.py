"""Ordinary-SPL Raydium CLMM boundary; native swap_v2 owns all swap math."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

import simulate_atomic_cycles as atomic
from spl.token.constants import TOKEN_2022_PROGRAM_ID

# Protocol layouts: https://github.com/raydium-io/raydium-clmm/tree/ed7c84a54ced59c55981780546adb0b4583dcf85
# programs/amm/src/{states/{pool,config,tick_array,tickarray_bitmap_extension,oracle},
# instructions/{create_pool,admin/create_amm_config,swap_v2},libraries/tick_math}.rs
# This layout pin is not an attestation of deployed bytecode; native simulation is required.
# Binary offsets, bitmap widths, tick bounds and fee ceilings are protocol constants.
# ruff: noqa: PLR2004
PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


def discriminator(name: str) -> bytes:
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]


def pda(*seeds: bytes) -> str:
    return str(
        atomic.Pubkey.find_program_address(
            list(seeds), atomic.Pubkey.from_string(PROGRAM)
        )[0]
    )


def bitmap_address(address: str) -> str:
    return pda(
        b"pool_tick_array_bitmap_extension", bytes(atomic.Pubkey.from_string(address))
    )


def select_arrays(
    tick: int, spacing: int, bitmap: int, *, decreasing: bool
) -> tuple[int, ...]:
    """Select initialized array starts, including the current array when initialized."""
    current = tick // (60 * spacing) + 7680
    mask = bitmap & ((1 << (current + 1)) - 1) if decreasing else bitmap >> current
    selected = []
    # ponytail: three initialized arrays per direction (six fetched maximum); larger
    # traversals must fail natively, not pad accounts. Raise only within the shared
    # 64-account/transaction-byte limits after measuring a concrete excluded route.
    while mask and len(selected) < 3:
        bit = mask.bit_length() - 1 if decreasing else (mask & -mask).bit_length() - 1
        index = bit if decreasing else bit + current
        selected.append((index - 7680) * 60 * spacing)
        mask ^= 1 << bit
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class ClmmPool:
    """Attested references and bounded native dependencies, never cached prices."""

    address: str
    program: str
    mints: tuple[str, str]
    vaults: tuple[str, str]
    config: str
    oracle: str
    bitmap: str
    bitmap_present: bool
    spacing: int
    decimals: tuple[int, int]
    open_time: int
    arrays: tuple[tuple[tuple[str, int], ...], ...]

    def dependencies(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    self.address,
                    *self.mints,
                    *self.vaults,
                    self.config,
                    self.oracle,
                    self.bitmap,
                    atomic.CLOCK,
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
        """Bound is minimum received for exact input, maximum paid for exact output."""
        atomic.require(
            input_mint in self.mints
            and type(amount) is int
            and type(bound) is int
            and 0 < amount < 2**64
            and 0 < bound < 2**64
            and type(exact_output) is bool,
            "clmm_swap_input",
        )
        side = self.mints.index(input_mint)
        atomic.require(bool(self.arrays[side]), "clmm_no_array_coverage")
        accounts = [
            atomic.meta(payer, signer=True),
            atomic.meta(self.config),
            atomic.meta(self.address, writable=True),
            *[
                atomic.meta(
                    atomic.get_associated_token_address(
                        payer, atomic.Pubkey.from_string(self.mints[i])
                    ),
                    writable=True,
                )
                for i in (side, 1 - side)
            ],
            *[atomic.meta(self.vaults[i], writable=True) for i in (side, 1 - side)],
            atomic.meta(self.oracle, writable=True),
            atomic.meta(atomic.SPL),
            atomic.meta(TOKEN_2022_PROGRAM_ID),
            atomic.meta(MEMO),
            *[atomic.meta(self.mints[i]) for i in (side, 1 - side)],
        ]
        if self.bitmap_present:
            accounts.append(atomic.meta(self.bitmap))
        accounts.extend(
            atomic.meta(address, writable=True) for address, _ in self.arrays[side]
        )
        # Zero sqrt limit enforces full-fill on both exact modes in swap_v2.
        data = hashlib.sha256(b"global:swap_v2").digest()[:8]
        data += (
            struct.pack("<QQ", amount, bound) + bytes(16) + bytes([not exact_output])
        )
        return atomic.Instruction(atomic.Pubkey.from_string(PROGRAM), data, accounts)


def decode(
    address: str,
    account: dict | None,
    bitmap_account: dict | None,
    *,
    expected_mints: tuple[str, str],
) -> ClmmPool:
    """Decode one pool and an explicitly fetched, possibly proven-null extension."""
    raw = atomic.checked_data(account, PROGRAM, 1544)
    atomic.require(raw[:8] == discriminator("PoolState"), "clmm_pool_discriminator")
    mints = (atomic.key(raw, 73), atomic.key(raw, 105))
    mint_bytes = tuple(bytes(atomic.Pubkey.from_string(mint)) for mint in mints)
    atomic.require(
        len(expected_mints) == len(set(expected_mints)) == 2
        and set(mints) == set(expected_mints)
        and mint_bytes[0] < mint_bytes[1],
        "clmm_mint_identity",
    )
    # Permissioned seed-index pools are outside this public legacy-pool lane.
    atomic.require(raw[391:393] == bytes(2), "clmm_permissioned_pool")
    config = atomic.key(raw, 9)
    canonical, bump = atomic.Pubkey.find_program_address(
        [b"pool", bytes(atomic.Pubkey.from_string(config)), *mint_bytes],
        atomic.Pubkey.from_string(PROGRAM),
    )
    atomic.require(str(canonical) == address and raw[8] == bump, "clmm_pool_identity")
    pool_bytes = bytes(canonical)
    vaults = (atomic.key(raw, 137), atomic.key(raw, 169))
    # Existing pools may use non-PDA oracles; hydrate attests their pool binding.
    oracle = atomic.key(raw, 201)
    atomic.require(
        vaults == tuple(pda(b"pool_vault", pool_bytes, mint) for mint in mint_bytes),
        "clmm_account_identity",
    )
    spacing = struct.unpack_from("<H", raw, 235)[0]
    tick = struct.unpack_from("<i", raw, 269)[0]
    atomic.require(
        0 < spacing <= 1000 and -443636 <= tick <= 443636,
        "clmm_tick_state",
    )
    atomic.require(
        4295048016
        <= int.from_bytes(raw[253:269], "little")
        < 79226673521066979257578248091,
        "clmm_sqrt_price",
    )
    atomic.require(
        raw[389] < 64 and not raw[389] & 16 and raw[390] <= 2, "clmm_pool_state"
    )
    # Unified bits cover array indices [-7680, 7679]. Negative extension chunks
    # run outward, but bits WITHIN each 512-bit chunk run toward increasing ticks.
    bitmap = int.from_bytes(raw[904:1032], "little") << 7168
    if bitmap_account is not None:
        extension = atomic.checked_data(bitmap_account, PROGRAM, 1832)
        atomic.require(
            extension[:8] == discriminator("TickArrayBitmapExtension")
            and atomic.key(extension, 8) == address,
            "clmm_bitmap_identity",
        )
        bitmap |= int.from_bytes(extension[40:936], "little") << 8192
        for chunk in range(14):
            offset = 936 + chunk * 64
            bitmap |= int.from_bytes(extension[offset : offset + 64], "little") << (
                6656 - chunk * 512
            )
    else:
        atomic.require(-512 <= tick // (60 * spacing) < 512, "clmm_bitmap_missing")
    low = -443636 // (60 * spacing) + 7680
    high = 443636 // (60 * spacing) + 7680
    atomic.require(
        bitmap >> (high + 1) == 0 and bitmap & ((1 << low) - 1) == 0,
        "clmm_bitmap_range",
    )
    arrays = tuple(
        tuple(
            (pda(b"tick_array", pool_bytes, struct.pack(">i", start)), start)
            for start in select_arrays(tick, spacing, bitmap, decreasing=decreasing)
        )
        for decreasing in (True, False)
    )
    return ClmmPool(
        address,
        PROGRAM,
        mints,
        vaults,
        config,
        oracle,
        bitmap_address(address),
        bitmap_account is not None,
        spacing,
        (raw[233], raw[234]),
        atomic.u64(raw, 1080),
        arrays,
    )


def hydrate(pool: ClmmPool, bank: dict) -> ClmmPool:
    """Re-select from the live bank, then attest every native account reference."""
    atomic.require(
        pool.address in bank and pool.bitmap in bank, "clmm_metadata_missing"
    )
    live = decode(
        pool.address, bank[pool.address], bank[pool.bitmap], expected_mints=pool.mints
    )
    atomic.require(
        (live.mints, live.vaults, live.config, live.oracle, live.spacing, live.decimals)
        == (
            pool.mints,
            pool.vaults,
            pool.config,
            pool.oracle,
            pool.spacing,
            pool.decimals,
        ),
        "clmm_references_changed",
    )
    atomic.require(
        set(live.dependencies()) == set(pool.dependencies())
        and all(address in bank for address in live.dependencies()),
        "clmm_array_dependencies_changed",
    )
    config = atomic.checked_data(bank[live.config], PROGRAM, 117)
    index = struct.unpack_from("<H", config, 9)[0]
    canonical, bump = atomic.Pubkey.find_program_address(
        [b"amm_config", struct.pack(">H", index)], atomic.Pubkey.from_string(PROGRAM)
    )
    atomic.require(
        config[:8] == discriminator("AmmConfig")
        and str(canonical) == live.config
        and config[8] == bump
        and struct.unpack_from("<H", config, 51)[0] == live.spacing,
        "clmm_config_identity",
    )
    protocol, trade = struct.unpack_from("<II", config, 43)
    fund = struct.unpack_from("<I", config, 53)[0]
    atomic.require(trade < 1_000_000 and protocol + fund <= 1_000_000, "clmm_fee_state")
    for mint, vault, decimals in zip(
        live.mints, live.vaults, live.decimals, strict=True
    ):
        mint_raw = atomic.checked_data(bank[mint], atomic.SPL, 82)
        token = atomic.checked_data(bank[vault], atomic.SPL, 165)
        atomic.require(
            mint_raw[45] == 1
            and mint_raw[44] == decimals
            and token[108] == 1
            and atomic.key(token, 0) == mint
            and atomic.key(token, 32) == live.address,
            "clmm_mint_or_vault_identity",
        )
    for address, start in dict(live.arrays[0] + live.arrays[1]).items():
        raw = atomic.checked_data(bank[address], PROGRAM, 10240)
        atomic.require(
            raw[:8] == discriminator("TickArrayState")
            and atomic.key(raw, 8) == live.address
            and struct.unpack_from("<i", raw, 40)[0] == start,
            "clmm_array_identity",
        )
        initialized = 0
        for i in range(60):
            offset = 44 + i * 168
            if (
                int.from_bytes(raw[offset + 20 : offset + 36], "little")
                or atomic.u64(raw, offset + 124)
                or atomic.u64(raw, offset + 132)
            ):
                tick = struct.unpack_from("<i", raw, offset)[0]
                atomic.require(
                    tick == start + i * live.spacing and -443636 <= tick <= 443636,
                    "clmm_tick_identity",
                )
                initialized += 1
        atomic.require(0 < initialized == raw[10124] <= 60, "clmm_array_initialized")
    oracle = atomic.checked_data(bank[live.oracle], PROGRAM, 4483)
    atomic.require(
        oracle[:8] == discriminator("ObservationState")
        and atomic.key(oracle, 19) == live.address
        and oracle[8] in (0, 1)
        and struct.unpack_from("<H", oracle, 17)[0] < 100,
        "clmm_oracle_identity",
    )
    clock = atomic.checked_data(
        bank[atomic.CLOCK], "Sysvar1111111111111111111111111111111111111", 40
    )
    atomic.require(
        struct.unpack_from("<q", clock, 32)[0] > live.open_time, "clmm_not_active"
    )
    return live
