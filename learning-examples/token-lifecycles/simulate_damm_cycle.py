"""Ordinary-SPL DAMM v2 boundary; the native program owns pricing and fees.

Contract: MeteoraAg/damm-v2 at a85c926607433f23f0ea60f4ca7b1ae92f4156cb,
programs/cp-amm/src/{state/pool.rs,instructions/swap/ix_swap.rs,ix_p_swap.rs}.
No router replay, transfer-hook guesses, referral account or local quote math.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import simulate_atomic_cycles as atomic

# Absolute zero-copy offsets, enum values and native activation buffers.
# ruff: noqa: PLR2004
PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
INSTRUCTIONS = "Sysvar1nstructions1111111111111111111111111"


def pda(*seeds: bytes) -> str:
    return str(
        atomic.Pubkey.find_program_address(
            list(seeds), atomic.Pubkey.from_string(PROGRAM)
        )[0]
    )


AUTHORITY = pda(b"pool_authority")
EVENT_AUTHORITY = pda(b"__event_authority")


@dataclass(frozen=True, slots=True)
class DammPool:
    """Attested immutable references, not an off-chain quote or fee cache."""

    address: str
    program: str
    mints: tuple[str, str]
    vaults: tuple[str, str]

    def dependencies(self) -> list[str]:
        return [self.address, *self.mints, *self.vaults]

    def swap(
        self,
        payer: atomic.Pubkey,
        input_mint: str,
        amount: int,
        bound: int,
        *,
        exact_output: bool,
    ) -> atomic.Instruction:
        """Build native swap2; only user accounts reverse for B-to-A."""
        atomic.require(
            input_mint in self.mints
            and type(amount) is int
            and 0 < amount < 2**64
            and type(bound) is int
            and 0 < bound < 2**64
            and type(exact_output) is bool,
            "damm_swap_input",
        )
        side = self.mints.index(input_mint)
        user = [
            atomic.get_associated_token_address(payer, atomic.Pubkey.from_string(mint))
            for mint in self.mints
        ]
        accounts = [
            atomic.meta(AUTHORITY),
            atomic.meta(self.address, writable=True),
            atomic.meta(user[side], writable=True),
            atomic.meta(user[1 - side], writable=True),
            *[atomic.meta(vault, writable=True) for vault in self.vaults],
            *[atomic.meta(mint) for mint in self.mints],
            atomic.meta(payer, signer=True),
            atomic.meta(atomic.SPL),
            atomic.meta(atomic.SPL),
            atomic.meta(PROGRAM),  # Anchor's absent-referral sentinel, readonly.
            atomic.meta(EVENT_AUTHORITY),
            atomic.meta(PROGRAM),
            # Native rate-limiter inspection needs remaining account zero. It is
            # harmless when unused; native code decides applicability and fees.
            atomic.meta(INSTRUCTIONS),
        ]
        data = bytes.fromhex("414b3f4ceb5b5b88") + struct.pack(
            "<QQB", amount, bound, 2 if exact_output else 0
        )
        return atomic.Instruction(atomic.Pubkey.from_string(PROGRAM), data, accounts)


def decode(
    address: str, account: dict | None, *, expected_mints: tuple[str, str]
) -> DammPool:
    """Attest the fixed pool, supported layout and canonical vault identities."""
    raw = atomic.checked_data(account, PROGRAM, 1112)
    atomic.require(
        raw[:8] == bytes.fromhex("f19a6d0411b16dbc"), "damm_pool_discriminator"
    )
    atomic.require(raw[481] == 0, "damm_pool_disabled")
    atomic.require(raw[482:484] == bytes(2), "damm_spl_only")
    atomic.require(
        raw[480] in (0, 1)
        and raw[484] in (0, 1, 2)
        and raw[485] in (0, 1)
        and raw[486] in (0, 1)
        and raw[696] in (0, 1)
        and raw[16] in (0, 1, 2, 3, 4),
        "damm_state_version",
    )
    mints = (atomic.key(raw, 168), atomic.key(raw, 200))
    vaults = (atomic.key(raw, 232), atomic.key(raw, 264))
    atomic.require(
        len(expected_mints) == len(set(expected_mints)) == 2
        and set(mints) == set(expected_mints),
        "damm_mint_identity",
    )
    atomic.require(
        vaults
        == tuple(
            pda(
                b"token_vault",
                bytes(atomic.Pubkey.from_string(mint)),
                bytes(atomic.Pubkey.from_string(address)),
            )
            for mint in mints
        ),
        "damm_vault_pda",
    )
    return DammPool(address, PROGRAM, mints, vaults)


def hydrate(pool: DammPool, bank: dict, *, payer: atomic.Pubkey) -> DammPool:
    """Validate one current bank; leave fees, reserves and layout upgrades native."""
    live = decode(pool.address, bank[pool.address], expected_mints=pool.mints)
    atomic.require(live == pool, "damm_references_changed")
    for mint, vault in zip(live.mints, live.vaults, strict=True):
        mint_raw = atomic.checked_data(bank[mint], atomic.SPL, 82)
        raw = atomic.checked_data(bank[vault], atomic.SPL, 165)
        atomic.require(mint_raw[45] == 1 and raw[108] == 1, "damm_mint_or_vault_state")
        atomic.require(
            atomic.key(raw, 0) == mint and atomic.key(raw, 32) == AUTHORITY,
            "damm_vault_identity",
        )
    raw = atomic.checked_data(bank[pool.address], PROGRAM, 1112)
    clock = atomic.checked_data(
        bank[atomic.CLOCK], "Sysvar1111111111111111111111111111111111111", 40
    )
    by_slot = raw[480] == 0
    point = atomic.u64(clock, 0) if by_slot else struct.unpack_from("<q", clock, 32)[0]
    activation = atomic.u64(raw, 472)
    if atomic.key(raw, 296) == str(payer):
        activation = max(0, activation - (9000 if by_slot else 3600))
    atomic.require(point >= activation, "damm_pool_not_active")
    return live
