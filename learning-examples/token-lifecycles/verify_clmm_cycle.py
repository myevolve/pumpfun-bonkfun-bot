"""Offline CLMM identity, sparse-array selection and native swap_v2 ABI checks.

Synthetic accounts only: no RPC, credentials, signing or submission. Layouts and
PDA seeds independently transcribed from raydium-io/raydium-clmm commit
ed7c84a54ced59c55981780546adb0b4583dcf85, programs/amm/src/states/{pool,config,
tick_array,tickarray_bitmap_extension,oracle}.rs and instructions/swap_v2.rs.
This checks account construction, not native pricing or deployed bytecode.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import TYPE_CHECKING

import simulate_atomic_cycles as atomic
import simulate_clmm_cycle as clmm
from spl.token.constants import TOKEN_2022_PROGRAM_ID

if TYPE_CHECKING:
    from collections.abc import Callable

# Binary offsets and deliberately distinct ABI inputs are protocol test vectors.
# ruff: noqa: PLR2004, S101

PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
MINTS = tuple(str(atomic.Pubkey.from_bytes(bytes([n]) * 32)) for n in (1, 2))
PAYER = atomic.Pubkey.from_bytes(bytes([3]) * 32)


def key_bytes(address: str) -> bytes:
    return bytes(atomic.Pubkey.from_string(address))


def pda(*seeds: bytes) -> tuple[str, int]:
    address, bump = atomic.Pubkey.find_program_address(
        list(seeds), atomic.Pubkey.from_string(PROGRAM)
    )
    return str(address), bump


def account(raw: bytes | bytearray, owner: str = PROGRAM) -> dict:
    return {
        "owner": owner,
        "executable": False,
        "data": [base64.b64encode(raw).decode(), "base64"],
    }


def layout(name: str, size: int) -> bytearray:
    raw = bytearray(size)
    raw[:8] = hashlib.sha256(f"account:{name}".encode()).digest()[:8]
    return raw


def changed(bank: dict, address: str, offset: int, value: bytes) -> dict:
    raw = bytearray(base64.b64decode(bank[address]["data"][0]))
    raw[offset : offset + len(value)] = value
    return {**bank, address: account(raw, bank[address]["owner"])}


def rejects(label: str, action: Callable[[], object]) -> None:
    try:
        action()
    except ValueError:
        return
    message = f"accepted {label}"
    raise AssertionError(message)


def fixture(
    tick: int, indices: tuple[int, ...], *, extension: bool
) -> tuple[str, dict]:
    """One canonical bank; indices are 60-tick arrays, not individual ticks."""
    config, config_bump = pda(b"amm_config", (7).to_bytes(2, "big"))
    address, bump = pda(b"pool", key_bytes(config), *(key_bytes(m) for m in MINTS))
    vaults = [pda(b"pool_vault", key_bytes(address), key_bytes(m))[0] for m in MINTS]
    # swap_v2 binds the pool's observation_key, including existing non-PDA accounts.
    oracle = str(atomic.Pubkey.from_bytes(bytes([4]) * 32))
    bitmap = pda(b"pool_tick_array_bitmap_extension", key_bytes(address))[0]
    raw = layout("PoolState", 1544)
    raw[8] = bump
    for offset, key in (
        (9, config),
        (41, str(PAYER)),
        (73, MINTS[0]),
        (105, MINTS[1]),
        (137, vaults[0]),
        (169, vaults[1]),
        (201, oracle),
    ):
        raw[offset : offset + 32] = key_bytes(key)
    raw[233:235] = bytes((9, 9))
    struct.pack_into("<H", raw, 235, 1)
    raw[237:253] = (1_000_000).to_bytes(16, "little")
    raw[253:269] = (1 << 64).to_bytes(16, "little")
    struct.pack_into("<i", raw, 269, tick)
    extra = layout("TickArrayBitmapExtension", 1832)
    extra[8:40] = key_bytes(address)
    # Explicit vectors exercise both sides of the asymmetric negative boundary.
    extension_bits = {
        -1026: (1000, 510),
        -1025: (1000, 511),
        -1024: (936, 0),
        -1023: (936, 1),
        -514: (936, 510),
        -513: (936, 511),
        512: (40, 0),
        514: (40, 2),
        1023: (40, 511),
        1024: (104, 0),
        1025: (104, 1),
        1026: (104, 2),
    }
    bank = {}
    for index in indices:
        if -512 <= index < 512:
            bit = index + 512
            raw[904 + bit // 8] |= 1 << (bit % 8)
        else:
            assert extension, "external bit cannot live in absent extension"
            offset, bit = extension_bits[index]
            extra[offset + bit // 8] |= 1 << (bit % 8)
        start = index * 60
        ticks = layout("TickArrayState", 10240)
        ticks[8:40] = key_bytes(address)
        struct.pack_into("<ii", ticks, 40, start, start)
        ticks[48:64] = (1).to_bytes(16, "little", signed=True)
        ticks[64:80] = (1).to_bytes(16, "little")
        ticks[10124] = 1
        bank[pda(b"tick_array", key_bytes(address), struct.pack(">i", start))[0]] = (
            account(ticks)
        )
    config_raw = layout("AmmConfig", 117)
    config_raw[8] = config_bump
    struct.pack_into("<H", config_raw, 9, 7)
    config_raw[11:43] = bytes(PAYER)
    struct.pack_into("<IIHI", config_raw, 43, 100_000, 3000, 1, 0)
    observation = layout("ObservationState", 4483)
    observation[19:51] = key_bytes(address)
    clock = bytearray(40)
    struct.pack_into("<q", clock, 32, 1_000_000_000)
    bank.update(
        {
            address: account(raw),
            config: account(config_raw),
            oracle: account(observation),
            bitmap: account(extra) if extension else None,
            atomic.CLOCK: account(clock, "Sysvar1111111111111111111111111111111111111"),
        }
    )
    for mint, vault in zip(MINTS, vaults, strict=True):
        mint_raw, vault_raw = bytearray(82), bytearray(165)
        mint_raw[44:46] = bytes((9, 1))
        vault_raw[:32], vault_raw[32:64] = key_bytes(mint), key_bytes(address)
        struct.pack_into("<Q", vault_raw, 64, 1_000_000_000)
        vault_raw[108] = 1
        bank[mint], bank[vault] = (
            account(mint_raw, atomic.SPL),
            account(vault_raw, atomic.SPL),
        )
    return address, bank


def decode(address: str, bank: dict) -> clmm.ClmmPool:
    bitmap = pda(b"pool_tick_array_bitmap_extension", key_bytes(address))[0]
    return clmm.decode(address, bank[address], bank[bitmap], expected_mints=MINTS)


def verify() -> None:
    cases = (
        (-1, (-5, -3, -1, 2, 5), False, (-1, -3, -5), (-1, 2, 5)),
        (-1, (-5, -3, 2, 5, 7), False, (-3, -5), (2, 5, 7)),
        (30719, (2, 509, 511, 512, 514), True, (511, 509, 2), (511, 512, 514)),
        (-30720, (-514, -513, -512, -3, -1), True, (-512, -513, -514), (-512, -3, -1)),
        (
            -61440,
            (-1026, -1025, -1024, -1023, -513),
            True,
            (-1024, -1025, -1026),
            (-1024, -1023, -513),
        ),
        (
            61440,
            (514, 1023, 1024, 1025, 1026),
            True,
            (1024, 1023, 514),
            (1024, 1025, 1026),
        ),
    )
    for tick, indices, extension, down, up in cases:
        address, bank = fixture(tick, indices, extension=extension)
        bitmap = pda(b"pool_tick_array_bitmap_extension", key_bytes(address))[0]
        assert clmm.bitmap_address(address) == bitmap
        pool = clmm.hydrate(decode(address, bank), bank)
        for side, selected in enumerate((down, up)):
            tick_keys = [
                pda(b"tick_array", key_bytes(address), struct.pack(">i", i * 60))[0]
                for i in selected
            ]
            for exact_output in (False, True):
                ix = pool.swap(
                    PAYER, MINTS[side], 123_456, 78_901, exact_output=exact_output
                )
                expected_data = (
                    hashlib.sha256(b"global:swap_v2").digest()[:8]
                    + struct.pack("<QQ", 123_456, 78_901)
                    + bytes(16)
                    + bytes((not exact_output,))
                )
                assert bytes(ix.data) == expected_data, "native amount/bound/mode ABI"
                config = pda(b"amm_config", (7).to_bytes(2, "big"))[0]
                oracle = str(atomic.Pubkey.from_bytes(bytes([4]) * 32))
                ordered = [
                    str(PAYER),
                    config,
                    address,
                    str(
                        atomic.get_associated_token_address(
                            PAYER, atomic.Pubkey.from_string(MINTS[side])
                        )
                    ),
                    str(
                        atomic.get_associated_token_address(
                            PAYER, atomic.Pubkey.from_string(MINTS[1 - side])
                        )
                    ),
                    pda(b"pool_vault", key_bytes(address), key_bytes(MINTS[side]))[0],
                    pda(b"pool_vault", key_bytes(address), key_bytes(MINTS[1 - side]))[
                        0
                    ],
                    oracle,
                    atomic.SPL,
                    str(TOKEN_2022_PROGRAM_ID),
                    MEMO,
                    MINTS[side],
                    MINTS[1 - side],
                ]
                ordered += ([bitmap] if extension else []) + tick_keys
                assert str(ix.program_id) == PROGRAM
                assert [str(meta.pubkey) for meta in ix.accounts] == ordered, (
                    "native direction/account order"
                )
                assert [meta.is_signer for meta in ix.accounts] == [True] + [False] * (
                    len(ordered) - 1
                )
                assert [meta.is_writable for meta in ix.accounts] == (
                    [False, False]
                    + [True] * 6
                    + [False] * 5
                    + ([False] if extension else [])
                    + [True] * len(tick_keys)
                ), "oracle/tick mutability or readonly program/mint/config ABI"

    # Reuse the full extension bank to exercise every external trust boundary.
    address, bank = fixture(-30720, (-514, -513, -512, -3, -1), extension=True)
    pool = decode(address, bank)
    bitmap = clmm.bitmap_address(address)
    config = pda(b"amm_config", (7).to_bytes(2, "big"))[0]
    oracle = str(atomic.Pubkey.from_bytes(bytes([4]) * 32))
    tick_key = pda(b"tick_array", key_bytes(address), struct.pack(">i", -30720))[0]
    for key in (address, bitmap, config, oracle, tick_key):
        raw = base64.b64decode(bank[key]["data"][0])
        for label, bad in (
            ("owner", {**bank[key], "owner": atomic.SPL}),
            ("executable", {**bank[key], "executable": True}),
            ("truncated layout", account(raw[:-1])),
            ("extended layout", account(raw + b"\0")),
            ("discriminator", account(bytes(8) + raw[8:])),
        ):
            rejects(
                f"{key} {label}",
                lambda key=key, bad=bad: clmm.hydrate(pool, {**bank, key: bad}),
            )
    for key, offset, value in (
        (address, 8, bytes((base64.b64decode(bank[address]["data"][0])[8] ^ 1,))),
        (address, 73, bytes(PAYER)),
        (address, 137, bytes(PAYER)),
        (address, 201, bytes(PAYER)),
        (config, 9, struct.pack("<H", 8)),
        (bitmap, 8, bytes(PAYER)),
        (oracle, 19, bytes(PAYER)),
        (tick_key, 8, bytes(PAYER)),
        (tick_key, 40, struct.pack("<i", -30660)),
        (tick_key, 44, struct.pack("<i", -30719)),
        (tick_key, 10124, b"\0"),
        (MINTS[0], 44, b"\x08"),
        (pool.vaults[0], 0, key_bytes(MINTS[1])),
        (pool.vaults[0], 32, bytes(PAYER)),
    ):
        rejects(
            "noncanonical identity/PDA",
            lambda key=key, offset=offset, value=value: clmm.hydrate(
                pool, changed(bank, key, offset, value)
            ),
        )
    rejects(
        "wrong pool address",
        lambda: clmm.decode(
            str(PAYER), bank[address], bank[bitmap], expected_mints=MINTS
        ),
    )
    rejects(
        "unexpected mint pair",
        lambda: clmm.decode(
            address, bank[address], bank[bitmap], expected_mints=(MINTS[0], str(PAYER))
        ),
    )
    for key in (*MINTS, *pool.vaults):
        rejects(
            "Token2022 state",
            lambda key=key: clmm.hydrate(
                pool, {**bank, key: {**bank[key], "owner": str(TOKEN_2022_PROGRAM_ID)}}
            ),
        )
    for key in (bitmap, tick_key):
        rejects(
            "omitted dependency",
            lambda key=key: clmm.hydrate(
                pool, {k: v for k, v in bank.items() if k != key}
            ),
        )
    rejects(
        "initialized array proven absent",
        lambda: clmm.hydrate(pool, {**bank, tick_key: None}),
    )
    address, bank = fixture(-1, (-5, -3, -1, 2, 5), extension=False)
    pool = decode(address, bank)
    clmm.hydrate(
        pool, bank
    )  # Proven-null extension is supported; an omitted read is not.
    bitmap = clmm.bitmap_address(address)
    rejects(
        "unknown rather than null extension",
        lambda: clmm.hydrate(pool, {k: v for k, v in bank.items() if k != bitmap}),
    )
    rejects(
        "missing extension outside internal range",
        lambda: clmm.decode(
            address,
            changed(bank, address, 269, struct.pack("<i", 30720))[address],
            None,
            expected_mints=MINTS,
        ),
    )
    print(
        "CLMM offline checks passed: identities, sparse boundaries, native ABI, ordinary SPL"
    )


if __name__ == "__main__":
    verify()
