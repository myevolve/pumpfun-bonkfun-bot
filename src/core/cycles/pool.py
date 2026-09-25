"""Six-venue Pool decode, hydration and constant-product quote."""

import base64
import hashlib
import struct
from dataclasses import dataclass

import aiohttp  # noqa: TC002 - runtime session type hint

from core.cycles.core import (
    AMM,
    AUTHORITY,
    CLOCK,
    CPMM,
    FEE_DENOMINATOR,
    RAYDIUM_API,
    SOL,
    SPL,
    checked_data,
    discriminator,
    key,
    require,
    u64,
)


@dataclass(frozen=True, slots=True)
class Pool:
    """One attested pool and, after hydration, executable quote reserves."""

    address: str
    program: str
    mints: tuple[str, str]
    vaults: tuple[str, str]
    config: str | None = None
    observation: str | None = None
    reserves: tuple[int, int] = (0, 0)
    trade_rate: int = 0
    creator_rate: int = 0
    fee_on: int = 0

    def dependencies(self) -> list[str]:
        """Return all accounts required for a same-bank quote."""
        return [
            self.address,
            *self.mints,
            *self.vaults,
            *([self.config] if self.config else []),
        ]

    def quote(self, input_mint: str, amount: int) -> int:
        """Quote exact input with integer fees and full constant-product impact."""
        require(input_mint in self.mints and amount > 0, "quote_input")
        require(min(self.reserves) > 0, "empty_reserves")
        side = self.mints.index(input_mint)
        creator_on_input = self.fee_on == 0 or self.fee_on == side + 1
        rate = self.trade_rate + (self.creator_rate if creator_on_input else 0)
        fee = (amount * rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR
        net = amount - fee
        out = net * self.reserves[1 - side] // (self.reserves[side] + net)
        if not creator_on_input:
            out -= (out * self.creator_rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR
        return max(0, out)


def decode_pool(address: str, account: dict | None) -> Pool:
    """Read canonical pool references, ignoring discovery-service prices/keys."""
    require(account is not None, "pool_missing")
    program = account["owner"]
    require(program in AUTHORITY, "unsupported_program")
    raw = checked_data(account, program, 752 if program == AMM else 637)
    if program == AMM:
        require(u64(raw, 0) == 6, "unsupported_v4_status")  # noqa: PLR2004 - V4 tag
        return Pool(
            address,
            program,
            (key(raw, 400), key(raw, 432)),
            (key(raw, 336), key(raw, 368)),
        )
    require(
        raw[:8] == hashlib.sha256(b"account:PoolState").digest()[:8],
        "pool_discriminator",
    )
    require(not raw[329] & 4, "swap_disabled")
    require((key(raw, 232), key(raw, 264)) == (SPL, SPL), "unsupported_token_program")
    require(raw[389] in (0, 1, 2) and raw[390] in (0, 1), "creator_fee_flags")
    return Pool(
        address,
        program,
        (key(raw, 168), key(raw, 200)),
        (key(raw, 72), key(raw, 104)),
        key(raw, 8),
        key(raw, 296),
    )


def hydrate_pool(pool: Pool, bank: dict[str, dict | None]) -> Pool:
    """Attest references and subtract owed fees from same-bank vault balances."""
    require(
        decode_pool(pool.address, bank[pool.address]) == pool, "pool_references_changed"
    )
    balances = []
    for mint, vault in zip(pool.mints, pool.vaults, strict=True):
        mint_raw = checked_data(bank[mint], SPL, 82)
        require(mint_raw[45] == 1, "mint_uninitialized")
        raw = checked_data(bank[vault], SPL, 165)
        require(
            key(raw, 0) == mint and key(raw, 32) == AUTHORITY[pool.program],
            "vault_identity",
        )
        require(raw[108] == 1, "vault_frozen")
        balances.append(u64(raw, 64))
    raw = base64.b64decode(bank[pool.address]["data"][0])
    if pool.program == AMM:
        balances = [balances[0] - u64(raw, 192), balances[1] - u64(raw, 200)]
        numerator, denominator = u64(raw, 176), u64(raw, 184)
        require(
            denominator > 0 and FEE_DENOMINATOR % denominator == 0, "v4_fee_denominator"
        )
        trade_rate, creator_rate, fee_on = (
            numerator * (FEE_DENOMINATOR // denominator),
            0,
            0,
        )
    else:
        clock = base64.b64decode(bank[CLOCK]["data"][0])
        require(
            u64(raw, 373) <= struct.unpack_from("<q", clock, 32)[0], "pool_not_open"
        )
        config = checked_data(bank[pool.config], CPMM, 236)
        require(config[:8] == discriminator("AmmConfig"), "config_discriminator")
        # Offsets per the pinned provenance twin (simulate_atomic_cycles.py
        # hydrate_pool; live-decoded D4FPEru…: trade@12=2500, proto@20,
        # fund@28, creator@108): config+8 is bump|index|… junk, NOT a rate.
        balances = [
            balances[side]
            - sum(u64(raw, offset + side * 8) for offset in (341, 357, 397))
            for side in (0, 1)
        ]
        trade_rate = u64(config, 12)
        creator_rate = u64(config, 108) if raw[390] else 0
        fee_on = raw[389]
    require(min(balances) > 0, "empty_reserves")
    require(0 <= trade_rate + creator_rate < FEE_DENOMINATOR, "invalid_fee_rate")
    return Pool(
        pool.address,
        pool.program,
        pool.mints,
        pool.vaults,
        pool.config,
        pool.observation,
        tuple(balances),
        trade_rate,
        creator_rate,
        fee_on,
    )


async def discover_pools_for_mint(
    session: "aiohttp.ClientSession", mint: str
) -> list[dict]:
    """Find AMM v4/CPMM pools trading ``mint``/SOL via the Raydium API.

    Returns raw pool info records; the caller decodes and hydrates them.
    Uses the generic list endpoint and filters client-side because the
    mint-specific endpoint returns 500 on current mainnet.
    """
    params = {
        "poolType": "standard",
        "poolSortField": "volume24h",
        "sortType": "desc",
        "pageSize": 100,
        "page": 1,
    }
    async with session.get(RAYDIUM_API + "/pools/info/list", params=params) as response:
        require(response.status == 200, "discovery_http_error")  # noqa: PLR2004
        payload = await response.json()
    require(payload.get("success") is True, "discovery_rejected")
    records = payload["data"]["data"]
    return [
        record
        for record in records
        if record.get("programId") in AUTHORITY
        and (
            (record["mintA"]["address"] == SOL and record["mintB"]["address"] == mint)
            or (
                record["mintA"]["address"] == mint and record["mintB"]["address"] == SOL
            )
        )
    ]
