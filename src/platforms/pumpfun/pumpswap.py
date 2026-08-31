"""Canonical PumpSwap pool discovery, quoting, and sell construction."""
# Detailed validation failures must preserve the exact rejected invariant.
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from solders.account import Account
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from spl.token.instructions import (
    CloseAccountParams,
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

from core.pubkeys import QUOTE_TOKEN_PROGRAMS, USDC_MINT, WSOL_MINT, SystemAddresses

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from core.client import SolanaClient
    from interfaces.core import TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddresses
from platforms.pumpfun.fee_schedule import PumpFees, PumpFeeSchedule, PumpFeeSnapshot

PUMP_SWAP_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string(
    "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
)
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string(
    "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
)
PUMP_SWAP_SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")
PUMP_SWAP_EXTEND_ACCOUNT_DISCRIMINATOR = bytes((234, 102, 194, 203, 150, 72, 62, 229))
PUMP_SWAP_POOL_DISCRIMINATOR = bytes((241, 154, 109, 4, 17, 177, 109, 188))
PUMP_SWAP_GLOBAL_CONFIG_DISCRIMINATOR = bytes((149, 8, 156, 202, 160, 252, 176, 217))
_MAX_U64 = 0xFFFF_FFFF_FFFF_FFFF
_MAX_BASIS_POINTS = 10_000

_EXECUTION_ACCOUNT_COUNT = 6


class PumpSwapAddresses:
    """Derive every canonical PumpSwap address used by an exit."""

    @staticmethod
    def derive_pool_authority(base_mint: Pubkey) -> Pubkey:
        """Derive Pump's canonical pool creator for a base mint."""
        return Pubkey.find_program_address(
            [b"pool-authority", bytes(base_mint)], PumpFunAddresses.PROGRAM
        )[0]

    @staticmethod
    def derive_canonical_pool(base_mint: Pubkey, quote_mint: Pubkey) -> Pubkey:
        """Derive the index-zero PumpSwap pool created by Pump migration."""
        authority = PumpSwapAddresses.derive_pool_authority(base_mint)
        return Pubkey.find_program_address(
            [
                b"pool",
                struct.pack("<H", 0),
                bytes(authority),
                bytes(base_mint),
                bytes(quote_mint),
            ],
            PUMP_SWAP_PROGRAM,
        )[0]

    @staticmethod
    def derive_creator_vault(coin_creator: Pubkey) -> Pubkey:
        """Derive the PumpSwap coin-creator vault authority."""
        return Pubkey.find_program_address(
            [b"creator_vault", bytes(coin_creator)], PUMP_SWAP_PROGRAM
        )[0]

    @staticmethod
    def find_fee_config() -> Pubkey:
        """Derive the Pump fee-program config for PumpSwap."""
        return Pubkey.find_program_address(
            [b"fee_config", bytes(PUMP_SWAP_PROGRAM)], PumpFunAddresses.FEE_PROGRAM
        )[0]

    @staticmethod
    def derive_pool_v2(base_mint: Pubkey) -> Pubkey:
        """Derive PumpSwap's post-upgrade per-mint account."""
        return Pubkey.find_program_address(
            [b"pool-v2", bytes(base_mint)], PUMP_SWAP_PROGRAM
        )[0]

    @staticmethod
    def derive_user_volume_accumulator(user: Pubkey) -> Pubkey:
        """Derive a PumpSwap user-volume accumulator."""
        return Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)], PUMP_SWAP_PROGRAM
        )[0]


@dataclass(frozen=True, slots=True)
class PumpSwapPoolState:
    """Validated canonical pool state required for pricing and execution."""

    address: Pubkey
    creator: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    base_vault: Pubkey
    quote_vault: Pubkey
    coin_creator: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool
    virtual_quote_reserve_raw: int
    base_reserve_raw: int
    quote_reserve_raw: int
    base_supply_raw: int
    base_decimals: int
    quote_decimals: int
    fee_snapshot: PumpFeeSnapshot
    protocol_fee_recipient: Pubkey
    buyback_fee_recipient: Pubkey
    needs_extension: bool


@dataclass(frozen=True, slots=True)
class _DecodedPool:
    address: Pubkey
    creator: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    base_vault: Pubkey
    quote_vault: Pubkey
    coin_creator: Pubkey
    is_mayhem_mode: bool
    is_cashback_coin: bool
    virtual_quote_reserve_raw: int
    needs_extension: bool


_POOL_MIN_ACCOUNT_SIZE = 261
_POOL_CURRENT_ACCOUNT_SIZE = 300
_GLOBAL_CONFIG_MIN_ACCOUNT_SIZE = 940
_TOKEN_ACCOUNT_MIN_SIZE = 165
_MINT_ACCOUNT_MIN_SIZE = 82
_SUPPORTED_TOKEN_PROGRAMS = frozenset(
    (SystemAddresses.TOKEN_PROGRAM, SystemAddresses.TOKEN_2022_PROGRAM)
)


def _validated_account_data(
    account: object,
    address: Pubkey,
    owner: Pubkey,
    minimum_size: int,
    discriminator: bytes | None = None,
) -> bytes:
    if not isinstance(account, Account):
        raise ValueError(f"PumpSwap account {address} was not found")
    if account.owner != owner:
        raise ValueError(f"PumpSwap account {address} has an unexpected owner")
    if not isinstance(account.data, bytes):
        raise ValueError(f"PumpSwap account {address} has invalid data")
    data = account.data
    if len(data) < minimum_size:
        raise ValueError(
            f"PumpSwap account {address} is too short: {len(data)}/{minimum_size}"
        )
    if discriminator is not None and data[: len(discriminator)] != discriminator:
        raise ValueError(f"PumpSwap account {address} has an invalid discriminator")
    return data


def _pubkey_at(data: bytes, offset: int) -> Pubkey:
    return Pubkey.from_bytes(data[offset : offset + 32])


def _bool_at(data: bytes, offset: int, field: str) -> bool:
    value = data[offset]
    if value not in (0, 1):
        raise ValueError(f"PumpSwap {field} is not a canonical boolean")
    return bool(value)


def _decode_pool_account(
    account: object,
    address: Pubkey,
    base_mint: Pubkey,
    quote_mint: Pubkey,
) -> _DecodedPool:
    data = _validated_account_data(
        account,
        address,
        PUMP_SWAP_PROGRAM,
        _POOL_MIN_ACCOUNT_SIZE,
        PUMP_SWAP_POOL_DISCRIMINATOR,
    )
    authority = PumpSwapAddresses.derive_pool_authority(base_mint)
    expected_pool, expected_bump = Pubkey.find_program_address(
        [
            b"pool",
            struct.pack("<H", 0),
            bytes(authority),
            bytes(base_mint),
            bytes(quote_mint),
        ],
        PUMP_SWAP_PROGRAM,
    )
    if address != expected_pool:
        raise ValueError("PumpSwap pool is not the canonical migrated pool")
    if data[8] != expected_bump or struct.unpack_from("<H", data, 9)[0] != 0:
        raise ValueError("PumpSwap pool has invalid canonical PDA metadata")
    creator = _pubkey_at(data, 11)
    decoded_base_mint = _pubkey_at(data, 43)
    decoded_quote_mint = _pubkey_at(data, 75)
    if creator != authority:
        raise ValueError("PumpSwap pool creator is not the migration authority")
    if decoded_base_mint != base_mint or decoded_quote_mint != quote_mint:
        raise ValueError("PumpSwap pool mints do not match the requested token")

    return _DecodedPool(
        address=address,
        creator=creator,
        base_mint=decoded_base_mint,
        quote_mint=decoded_quote_mint,
        base_vault=_pubkey_at(data, 139),
        quote_vault=_pubkey_at(data, 171),
        coin_creator=_pubkey_at(data, 211),
        is_mayhem_mode=_bool_at(data, 243, "mayhem flag"),
        is_cashback_coin=_bool_at(data, 244, "cashback flag"),
        virtual_quote_reserve_raw=int.from_bytes(data[245:261], "little", signed=True),
        needs_extension=len(data) < _POOL_CURRENT_ACCOUNT_SIZE,
    )


def _decode_global_recipients(
    account: object,
) -> tuple[tuple[Pubkey, ...], tuple[Pubkey, ...], tuple[Pubkey, ...]]:
    data = _validated_account_data(
        account,
        PUMP_SWAP_GLOBAL_CONFIG,
        PUMP_SWAP_PROGRAM,
        _GLOBAL_CONFIG_MIN_ACCOUNT_SIZE,
        PUMP_SWAP_GLOBAL_CONFIG_DISCRIMINATOR,
    )
    disable_flags = data[56]
    if disable_flags & (1 << 4):
        raise ValueError("PumpSwap sells are disabled by global configuration")
    protocol = tuple(_pubkey_at(data, 57 + index * 32) for index in range(8))
    reserved_first = _pubkey_at(data, 385)
    _bool_at(data, 417, "mayhem flag")
    reserved = (
        reserved_first,
        *(_pubkey_at(data, 418 + index * 32) for index in range(7)),
    )
    _bool_at(data, 642, "cashback flag")
    buyback = tuple(_pubkey_at(data, 643 + index * 32) for index in range(8))
    _bool_at(data, 939, "boost flag")
    for group_name, recipients in (
        ("protocol", protocol),
        ("reserved", reserved),
        ("buyback", buyback),
    ):
        if any(recipient == Pubkey.default() for recipient in recipients):
            raise ValueError(
                f"PumpSwap global config contains a default {group_name} recipient"
            )
    return protocol, reserved, buyback


def _decode_mint_account(account: object, mint: Pubkey) -> tuple[Pubkey, int, int]:
    if not isinstance(account, Account):
        raise ValueError(f"PumpSwap base mint {mint} was not found")
    token_program = account.owner
    if token_program not in _SUPPORTED_TOKEN_PROGRAMS:
        raise ValueError("PumpSwap base mint has an unsupported owner")
    data = _validated_account_data(
        account,
        mint,
        token_program,
        _MINT_ACCOUNT_MIN_SIZE,
    )
    if data[45] != 1:
        raise ValueError("PumpSwap base mint is not initialized")
    supply = struct.unpack_from("<Q", data, 36)[0]
    decimals = data[44]
    if supply == 0:
        raise ValueError("PumpSwap base mint has no supply")
    return token_program, supply, decimals


def _decode_token_account_amount(
    account: object,
    address: Pubkey,
    *,
    token_program: Pubkey,
    mint: Pubkey,
    authority: Pubkey,
) -> int:
    data = _validated_account_data(
        account,
        address,
        token_program,
        _TOKEN_ACCOUNT_MIN_SIZE,
    )
    if _pubkey_at(data, 0) != mint:
        raise ValueError(f"PumpSwap vault {address} has an unexpected mint")
    if _pubkey_at(data, 32) != authority:
        raise ValueError(f"PumpSwap vault {address} has an unexpected authority")
    if data[108] not in (1, 2):
        raise ValueError(f"PumpSwap vault {address} is not initialized")
    return struct.unpack_from("<Q", data, 64)[0]


class PumpSwapManager:
    """Load and quote the canonical PumpSwap pool created by migration."""

    def __init__(
        self,
        client: SolanaClient,
        *,
        fee_schedule: PumpFeeSchedule | None = None,
        chooser: Callable[[Sequence[Pubkey]], Pubkey] = secrets.choice,
    ) -> None:
        self.client = client
        self.fee_schedule = fee_schedule or PumpFeeSchedule(
            client,
            PumpSwapAddresses.find_fee_config(),
            PUMP_SWAP_PROGRAM,
        )
        self._chooser = chooser

    async def start(self) -> None:
        """Start fee-config observation and attestation."""
        await self.fee_schedule.start()

    async def close(self) -> None:
        """Stop fee-config observation."""
        await self.fee_schedule.close()

    async def load_execution_state(
        self,
        base_mint: Pubkey,
        quote_mint: Pubkey,
        commitment: str | None = None,
    ) -> tuple[dict[str, object], Pubkey]:
        """Load one slot-consistent snapshot for a canonical migrated sell."""
        quote_program = QUOTE_TOKEN_PROGRAMS.get(quote_mint)
        if quote_program is None:
            raise ValueError(f"Unsupported PumpSwap quote mint: {quote_mint}")
        pool_address = PumpSwapAddresses.derive_canonical_pool(base_mint, quote_mint)
        preliminary_account = await self.client.get_account_info(
            pool_address,
            commitment=commitment,
        )
        preliminary = _decode_pool_account(
            preliminary_account,
            pool_address,
            base_mint,
            quote_mint,
        )
        fee_config = PumpSwapAddresses.find_fee_config()
        accounts = await self.client.get_multiple_accounts(
            [
                pool_address,
                PUMP_SWAP_GLOBAL_CONFIG,
                base_mint,
                preliminary.base_vault,
                preliminary.quote_vault,
                fee_config,
            ],
            commitment=commitment,
        )
        if not isinstance(accounts, list) or len(accounts) != _EXECUTION_ACCOUNT_COUNT:
            raise ValueError("PumpSwap execution account batch is malformed")
        (
            pool_account,
            global_account,
            mint_account,
            base_vault_account,
            quote_vault_account,
            fee_account,
        ) = accounts
        decoded = _decode_pool_account(
            pool_account,
            pool_address,
            base_mint,
            quote_mint,
        )
        if (
            decoded.base_vault != preliminary.base_vault
            or decoded.quote_vault != preliminary.quote_vault
        ):
            raise ValueError("PumpSwap pool vaults changed during execution refresh")
        base_program, base_supply, base_decimals = _decode_mint_account(
            mint_account, base_mint
        )
        base_reserve = _decode_token_account_amount(
            base_vault_account,
            decoded.base_vault,
            token_program=base_program,
            mint=base_mint,
            authority=pool_address,
        )
        quote_reserve = _decode_token_account_amount(
            quote_vault_account,
            decoded.quote_vault,
            token_program=quote_program,
            mint=quote_mint,
            authority=pool_address,
        )
        if base_reserve == 0 or quote_reserve == 0:
            raise ValueError("PumpSwap canonical pool has empty reserves")
        if not isinstance(fee_account, Account):
            raise ValueError(f"PumpSwap fee config {fee_config} was not found")
        snapshot = await self.fee_schedule.accept_account(fee_account)
        protocol, reserved, buyback = _decode_global_recipients(global_account)
        fee_recipients = reserved if decoded.is_mayhem_mode else protocol
        state = PumpSwapPoolState(
            address=pool_address,
            creator=decoded.creator,
            base_mint=base_mint,
            quote_mint=quote_mint,
            base_vault=decoded.base_vault,
            quote_vault=decoded.quote_vault,
            coin_creator=decoded.coin_creator,
            is_mayhem_mode=decoded.is_mayhem_mode,
            is_cashback_coin=decoded.is_cashback_coin,
            virtual_quote_reserve_raw=decoded.virtual_quote_reserve_raw,
            base_reserve_raw=base_reserve,
            quote_reserve_raw=quote_reserve,
            base_supply_raw=base_supply,
            base_decimals=base_decimals,
            quote_decimals=9 if quote_mint == WSOL_MINT else 6,
            fee_snapshot=snapshot,
            protocol_fee_recipient=self._chooser(fee_recipients),
            buyback_fee_recipient=self._chooser(buyback),
            needs_extension=decoded.needs_extension,
        )
        if state.quote_reserve_raw + state.virtual_quote_reserve_raw <= 0:
            raise ValueError("PumpSwap effective quote reserves are invalid")
        return self._execution_state(state, base_program, quote_program), base_program

    @staticmethod
    def _execution_state(
        state: PumpSwapPoolState,
        base_program: Pubkey,
        quote_program: Pubkey,
    ) -> dict[str, object]:
        return {
            "venue": "pumpswap",
            "complete": True,
            "is_tradeable": True,
            "status_name": "pumpswap",
            "pool_address": state.address,
            "base_mint": state.base_mint,
            "quote_mint": state.quote_mint,
            "base_token_program": base_program,
            "quote_token_program": quote_program,
            "base_vault": state.base_vault,
            "quote_vault": state.quote_vault,
            "global_config": PUMP_SWAP_GLOBAL_CONFIG,
            "platform_config": PumpSwapAddresses.find_fee_config(),
            "creator": state.coin_creator,
            "creator_vault": PumpSwapAddresses.derive_creator_vault(state.coin_creator),
            "protocol_fee_recipient": state.protocol_fee_recipient,
            "buyback_fee_recipient": state.buyback_fee_recipient,
            "is_mayhem_mode": state.is_mayhem_mode,
            "is_cashback_coin": state.is_cashback_coin,
            "base_decimals": state.base_decimals,
            "quote_decimals": state.quote_decimals,
            "token_total_supply": state.base_supply_raw,
            "virtual_quote_reserves": state.virtual_quote_reserve_raw,
            "pool_needs_extension": state.needs_extension,
            "_pumpswap_pool": state,
        }

    def _state_for_quote(
        self,
        pool_address: Pubkey,
        pool_state: dict[str, object],
    ) -> tuple[PumpSwapPoolState, PumpFeeSnapshot]:
        state = pool_state.get("_pumpswap_pool")
        if not isinstance(state, PumpSwapPoolState) or state.address != pool_address:
            raise ValueError("PumpSwap quote has no matching execution snapshot")
        current = self.fee_schedule.require_snapshot()
        if current.config.digest != state.fee_snapshot.config.digest:
            raise ValueError("PumpSwap execution fee snapshot is obsolete")
        return state, current

    async def calculate_sell_amount_out(
        self,
        pool_address: Pubkey,
        amount: int,
        *,
        pool_state: dict[str, object] | None = None,
    ) -> int:
        """Quote a sell against a validated execution snapshot."""
        if pool_state is None:
            raise ValueError("PumpSwap sell quote requires an execution snapshot")
        state, snapshot = self._state_for_quote(pool_address, pool_state)
        return quote_pumpswap_sell(
            base_reserve_raw=state.base_reserve_raw,
            quote_reserve_raw=state.quote_reserve_raw,
            virtual_quote_reserve_raw=state.virtual_quote_reserve_raw,
            base_supply_raw=state.base_supply_raw,
            base_amount_in_raw=amount,
            quote_mint=state.quote_mint,
            coin_creator=state.coin_creator,
            fee_snapshot=snapshot,
        )

    def calculate_price(
        self,
        pool_address: Pubkey,
        pool_state: dict[str, object],
    ) -> float:
        """Return the migrated token price in quote-token units."""
        state, _snapshot = self._state_for_quote(pool_address, pool_state)
        effective_quote = state.quote_reserve_raw + state.virtual_quote_reserve_raw
        return (
            effective_quote
            * (10**state.base_decimals)
            / state.base_reserve_raw
            / (10**state.quote_decimals)
        )


def _require_raw_u64(value: object, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_U64
    ):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{field} must be a {qualifier}raw u64 integer")
    return value


def _fee_amount(amount_raw: int, basis_points: int) -> int:
    if not 0 <= basis_points <= _MAX_BASIS_POINTS:
        raise ValueError("fee basis points must be between 0 and 10,000")
    if basis_points == 0:
        return 0
    return (amount_raw * basis_points + _MAX_BASIS_POINTS - 1) // _MAX_BASIS_POINTS


def _fees_for_market_cap(
    snapshot: PumpFeeSnapshot,
    market_cap_raw: int,
    quote_mint: Pubkey,
) -> PumpFees:
    if quote_mint == WSOL_MINT:
        tiers = snapshot.config.regular_tiers
    elif quote_mint == USDC_MINT:
        tiers = snapshot.config.stable_tiers
    else:
        raise ValueError(f"Unsupported PumpSwap quote mint: {quote_mint}")
    selected = tiers[0]
    for tier in reversed(tiers):
        if market_cap_raw >= tier.market_cap_threshold_raw:
            selected = tier
            break
    return selected.fees


def quote_pumpswap_sell(  # noqa: PLR0913
    *,
    base_reserve_raw: int,
    quote_reserve_raw: int,
    virtual_quote_reserve_raw: int,
    base_supply_raw: int,
    base_amount_in_raw: int,
    quote_mint: Pubkey,
    coin_creator: Pubkey,
    fee_snapshot: PumpFeeSnapshot,
) -> int:
    """Return the official SDK-equivalent fee-adjusted sell output."""
    base_reserve_raw = _require_raw_u64(
        base_reserve_raw, "base_reserve_raw", positive=True
    )
    quote_reserve_raw = _require_raw_u64(
        quote_reserve_raw, "quote_reserve_raw", positive=True
    )
    base_supply_raw = _require_raw_u64(
        base_supply_raw, "base_supply_raw", positive=True
    )
    base_amount_in_raw = _require_raw_u64(
        base_amount_in_raw, "base_amount_in_raw", positive=True
    )
    if isinstance(virtual_quote_reserve_raw, bool) or not isinstance(
        virtual_quote_reserve_raw, int
    ):
        raise ValueError("virtual_quote_reserve_raw must be an integer")
    effective_quote_reserve = quote_reserve_raw + virtual_quote_reserve_raw
    if not 0 < effective_quote_reserve <= _MAX_U64:
        raise ValueError("effective PumpSwap quote reserves are invalid")

    gross_quote_output = (
        effective_quote_reserve
        * base_amount_in_raw
        // (base_reserve_raw + base_amount_in_raw)
    )
    market_cap_raw = effective_quote_reserve * base_supply_raw // base_reserve_raw
    fees = _fees_for_market_cap(fee_snapshot, market_cap_raw, quote_mint)
    lp_fee = _fee_amount(gross_quote_output, fees.lp_fee_bps)
    protocol_fee = _fee_amount(gross_quote_output, fees.protocol_fee_bps)
    creator_fee = (
        0
        if coin_creator == Pubkey.default()
        else _fee_amount(gross_quote_output, fees.creator_fee_bps)
    )
    if quote_reserve_raw < gross_quote_output - lp_fee:
        raise ValueError("PumpSwap sell exceeds real quote reserves")
    output = gross_quote_output - lp_fee - protocol_fee - creator_fee
    if output <= 0:
        raise ValueError("PumpSwap sell quote produces no output")
    return output


def _required_pubkey(token_info: TokenInfo, field: str) -> Pubkey:
    value = getattr(token_info, field, None)
    if not isinstance(value, Pubkey):
        raise ValueError(f"Migrated PumpSwap sell requires {field}")
    return value


# Keep invariant checks and the exact wire-account order together for auditability.
def build_pumpswap_sell_instructions(  # noqa: C901
    token_info: TokenInfo,
    user: Pubkey,
    amount_in: int,
    minimum_amount_out: int,
) -> list[Instruction]:
    """Build a canonical PumpSwap sell and its quote-account prerequisite."""
    _require_raw_u64(amount_in, "amount_in", positive=True)
    _require_raw_u64(minimum_amount_out, "minimum_amount_out")
    if token_info.curve_complete is not True or token_info.pool_status != "pumpswap":
        raise ValueError("PumpSwap sell requires authoritative migrated-pool state")
    if token_info.token_program_id not in (
        SystemAddresses.TOKEN_PROGRAM,
        SystemAddresses.TOKEN_2022_PROGRAM,
    ):
        raise ValueError("PumpSwap sell requires a supported base token program")
    quote_mint = _required_pubkey(token_info, "quote_mint")
    quote_program = QUOTE_TOKEN_PROGRAMS.get(quote_mint)
    if quote_program is None or token_info.quote_token_program_id != quote_program:
        raise ValueError("PumpSwap sell has unsupported quote metadata")

    pool = _required_pubkey(token_info, "pool_state")
    expected_pool = PumpSwapAddresses.derive_canonical_pool(token_info.mint, quote_mint)
    if pool != expected_pool:
        raise ValueError("PumpSwap pool is not the canonical migrated pool")
    base_vault = _required_pubkey(token_info, "base_vault")
    quote_vault = _required_pubkey(token_info, "quote_vault")
    coin_creator = _required_pubkey(token_info, "creator")
    creator_vault = _required_pubkey(token_info, "creator_vault")
    if creator_vault != PumpSwapAddresses.derive_creator_vault(coin_creator):
        raise ValueError("PumpSwap creator vault does not match the pool creator")
    global_config = _required_pubkey(token_info, "global_config")
    if global_config != PUMP_SWAP_GLOBAL_CONFIG:
        raise ValueError("PumpSwap global config is not canonical")
    fee_config = _required_pubkey(token_info, "platform_config")
    if fee_config != PumpSwapAddresses.find_fee_config():
        raise ValueError("PumpSwap fee config is not canonical")
    protocol_fee_recipient = _required_pubkey(token_info, "protocol_fee_recipient")
    buyback_fee_recipient = _required_pubkey(token_info, "buyback_fee_recipient")

    user_base = get_associated_token_address(
        user, token_info.mint, token_info.token_program_id
    )
    user_quote = get_associated_token_address(user, quote_mint, quote_program)
    protocol_fee_ata = get_associated_token_address(
        protocol_fee_recipient, quote_mint, quote_program
    )
    creator_vault_ata = get_associated_token_address(
        creator_vault, quote_mint, quote_program
    )
    accounts = [
        AccountMeta(pubkey=pool, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user, is_signer=True, is_writable=True),
        AccountMeta(pubkey=global_config, is_signer=False, is_writable=False),
        AccountMeta(pubkey=token_info.mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=quote_mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=user_base, is_signer=False, is_writable=True),
        AccountMeta(pubkey=user_quote, is_signer=False, is_writable=True),
        AccountMeta(pubkey=base_vault, is_signer=False, is_writable=True),
        AccountMeta(pubkey=quote_vault, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=protocol_fee_recipient,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(pubkey=protocol_fee_ata, is_signer=False, is_writable=True),
        AccountMeta(
            pubkey=token_info.token_program_id,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(pubkey=quote_program, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=SystemAddresses.SYSTEM_PROGRAM,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=SystemAddresses.ASSOCIATED_TOKEN_PROGRAM,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=PUMP_SWAP_EVENT_AUTHORITY,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(
            pubkey=PUMP_SWAP_PROGRAM,
            is_signer=False,
            is_writable=False,
        ),
        AccountMeta(pubkey=creator_vault_ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=False),
        AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
        AccountMeta(
            pubkey=PumpFunAddresses.FEE_PROGRAM,
            is_signer=False,
            is_writable=False,
        ),
    ]
    if token_info.is_cashback_coin:
        volume_accumulator = PumpSwapAddresses.derive_user_volume_accumulator(user)
        accounts.extend(
            [
                AccountMeta(
                    pubkey=get_associated_token_address(
                        volume_accumulator, quote_mint, quote_program
                    ),
                    is_signer=False,
                    is_writable=True,
                ),
                AccountMeta(
                    pubkey=volume_accumulator,
                    is_signer=False,
                    is_writable=True,
                ),
            ]
        )
    if coin_creator != Pubkey.default():
        accounts.append(
            AccountMeta(
                pubkey=PumpSwapAddresses.derive_pool_v2(token_info.mint),
                is_signer=False,
                is_writable=False,
            )
        )
    accounts.extend(
        [
            AccountMeta(
                pubkey=buyback_fee_recipient,
                is_signer=False,
                is_writable=False,
            ),
            AccountMeta(
                pubkey=get_associated_token_address(
                    buyback_fee_recipient, quote_mint, quote_program
                ),
                is_signer=False,
                is_writable=True,
            ),
        ]
    )
    data = PUMP_SWAP_SELL_DISCRIMINATOR + struct.pack(
        "<QQ", amount_in, minimum_amount_out
    )
    instructions: list[Instruction] = []
    if token_info.pool_needs_extension:
        instructions.append(
            Instruction(
                PUMP_SWAP_PROGRAM,
                PUMP_SWAP_EXTEND_ACCOUNT_DISCRIMINATOR,
                [
                    AccountMeta(pubkey=pool, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=user, is_signer=True, is_writable=False),
                    AccountMeta(
                        pubkey=SystemAddresses.SYSTEM_PROGRAM,
                        is_signer=False,
                        is_writable=False,
                    ),
                    AccountMeta(
                        pubkey=PUMP_SWAP_EVENT_AUTHORITY,
                        is_signer=False,
                        is_writable=False,
                    ),
                    AccountMeta(
                        pubkey=PUMP_SWAP_PROGRAM,
                        is_signer=False,
                        is_writable=False,
                    ),
                ],
            )
        )
    instructions.extend(
        [
            create_idempotent_associated_token_account(
                user, user, quote_mint, quote_program
            ),
            Instruction(PUMP_SWAP_PROGRAM, data, accounts),
        ]
    )
    if quote_mint == WSOL_MINT:
        instructions.append(
            close_account(
                CloseAccountParams(
                    account=user_quote,
                    dest=user,
                    owner=user,
                    program_id=quote_program,
                )
            )
        )
    return instructions
