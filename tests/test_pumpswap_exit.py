# ruff: noqa: ARG005, FBT001, FBT003, PLR0913, PLR2004, S101
from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.account import Account
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT, WSOL_MINT
from interfaces.core import Platform, TokenInfo
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFees,
    PumpFeeSnapshot,
    PumpFeeTier,
)
from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder
from platforms.pumpfun.pumpswap import (
    PUMP_SWAP_GLOBAL_CONFIG,
    PUMP_SWAP_GLOBAL_CONFIG_DISCRIMINATOR,
    PUMP_SWAP_POOL_DISCRIMINATOR,
    PUMP_SWAP_PROGRAM,
    PUMP_SWAP_SELL_DISCRIMINATOR,
    PumpSwapAddresses,
    PumpSwapManager,
    quote_pumpswap_sell,
)


class _IdlParser:
    def get_instruction_discriminators(self) -> dict[str, bytes]:
        return {
            "buy_exact_sol_in": b"legacy__",
            "sell": b"sell____",
            "buy_v2": b"buy_v2__",
            "sell_v2": b"sell_v2_",
        }


def _snapshot() -> PumpFeeSnapshot:
    fees = PumpFees(lp_fee_bps=20, protocol_fee_bps=5, creator_fee_bps=30)
    tier = PumpFeeTier(market_cap_threshold_raw=0, fees=fees)
    return PumpFeeSnapshot(
        config=PumpFeeConfig(
            bump=1,
            admin=Pubkey.new_unique(),
            flat_fees=fees,
            regular_tiers=(tier,),
            stable_tiers=(tier,),
            exotic_flat_fees=PumpFees(0, 95, 30),
            digest="fee-config",
        ),
        observed_at=1.0,
        attested_at=1.0,
    )


def _migrated_token() -> TokenInfo:
    mint = Pubkey.new_unique()
    quote = WSOL_MINT
    token = TokenInfo(
        name="Migrated",
        symbol="MIG",
        uri="https://example.invalid/migrated.json",
        mint=mint,
        platform=Platform.PUMP_FUN,
        token_program_id=TOKEN_2022_PROGRAM,
        quote_mint=quote,
        quote_token_program_id=TOKEN_PROGRAM,
        curve_complete=True,
        pool_tradeable=True,
        pool_status="pumpswap",
    )
    token.pool_state = PumpSwapAddresses.derive_canonical_pool(mint, quote)
    token.base_vault = Pubkey.new_unique()
    token.quote_vault = Pubkey.new_unique()
    token.global_config = PUMP_SWAP_GLOBAL_CONFIG
    token.platform_config = PumpSwapAddresses.find_fee_config()
    token.creator = Pubkey.new_unique()
    token.creator_vault = PumpSwapAddresses.derive_creator_vault(token.creator)
    token.protocol_fee_recipient = Pubkey.new_unique()
    token.buyback_fee_recipient = Pubkey.new_unique()
    return token


def test_pumpswap_sell_quote_uses_virtual_reserves_and_all_fees() -> None:
    gross = 100_000_000 * 2_000_000_000 // 1_100_000_000
    expected = gross
    for basis_points in (20, 5, 30):
        expected -= (gross * basis_points + 9_999) // 10_000

    amount_out = quote_pumpswap_sell(
        base_reserve_raw=1_000_000_000,
        quote_reserve_raw=1_500_000_000,
        virtual_quote_reserve_raw=500_000_000,
        base_supply_raw=1_000_000_000_000_000,
        base_amount_in_raw=100_000_000,
        quote_mint=WSOL_MINT,
        coin_creator=Pubkey.new_unique(),
        fee_snapshot=_snapshot(),
    )

    assert amount_out == expected


@pytest.mark.asyncio
async def test_completed_curve_builds_canonical_pumpswap_sell() -> None:
    token = _migrated_token()
    user = Pubkey.new_unique()
    builder = PumpFunInstructionBuilder(_IdlParser())

    instructions = await builder.build_sell_instruction(
        token,
        user,
        amount_in=1_000_000,
        minimum_amount_out=500_000,
        address_provider=SimpleNamespace(),
    )

    assert len(instructions) == 3
    sell_instruction = instructions[-2]
    close_wsol_instruction = instructions[-1]
    assert sell_instruction.program_id == PUMP_SWAP_PROGRAM
    assert bytes(sell_instruction.data) == PUMP_SWAP_SELL_DISCRIMINATOR + struct.pack(
        "<QQ", 1_000_000, 500_000
    )
    assert len(sell_instruction.accounts) == 24
    assert sell_instruction.accounts[0].pubkey == token.pool_state
    assert sell_instruction.accounts[1].pubkey == user
    assert sell_instruction.accounts[3].pubkey == token.mint
    assert sell_instruction.accounts[4].pubkey == WSOL_MINT
    assert sell_instruction.accounts[7].pubkey == token.base_vault
    assert sell_instruction.accounts[8].pubkey == token.quote_vault
    assert sell_instruction.accounts[9].pubkey == token.protocol_fee_recipient
    assert sell_instruction.accounts[-3].pubkey == PumpSwapAddresses.derive_pool_v2(
        token.mint
    )
    assert sell_instruction.accounts[-2].pubkey == token.buyback_fee_recipient
    assert close_wsol_instruction.program_id == TOKEN_PROGRAM
    assert bytes(close_wsol_instruction.data) == b"\x09"
    assert close_wsol_instruction.accounts[1].pubkey == user
    assert close_wsol_instruction.accounts[2].pubkey == user


def test_pumpswap_priority_accounts_come_from_the_sell_instruction() -> None:
    token = _migrated_token()
    user = Pubkey.new_unique()
    builder = PumpFunInstructionBuilder(_IdlParser())

    priority_accounts = builder.get_required_accounts_for_sell(
        token,
        user,
        SimpleNamespace(),
    )

    assert token.pool_state in priority_accounts
    assert token.base_vault in priority_accounts
    assert token.quote_vault in priority_accounts
    assert (
        get_associated_token_address(
            token.protocol_fee_recipient,
            WSOL_MINT,
            TOKEN_PROGRAM,
        )
        in priority_accounts
    )


@pytest.mark.asyncio
async def test_pumpswap_sell_omits_pool_v2_for_default_coin_creator() -> None:
    token = _migrated_token()
    token.creator = Pubkey.default()
    token.creator_vault = PumpSwapAddresses.derive_creator_vault(token.creator)
    user = Pubkey.new_unique()
    builder = PumpFunInstructionBuilder(_IdlParser())

    instructions = await builder.build_sell_instruction(
        token,
        user,
        amount_in=1_000_000,
        minimum_amount_out=500_000,
        address_provider=SimpleNamespace(),
    )

    sell_accounts = instructions[-2].accounts
    assert len(sell_accounts) == 23
    assert sell_accounts[-2].pubkey == token.buyback_fee_recipient
    assert sell_accounts[-1].pubkey != PumpSwapAddresses.derive_pool_v2(token.mint)


@pytest.mark.asyncio
async def test_pumpswap_usdc_sell_does_not_close_quote_account() -> None:
    token = _migrated_token()
    token.quote_mint = USDC_MINT
    token.pool_state = PumpSwapAddresses.derive_canonical_pool(token.mint, USDC_MINT)
    user = Pubkey.new_unique()
    builder = PumpFunInstructionBuilder(_IdlParser())

    instructions = await builder.build_sell_instruction(
        token,
        user,
        amount_in=1_000_000,
        minimum_amount_out=500_000,
        address_provider=SimpleNamespace(),
    )

    assert len(instructions) == 2
    assert instructions[-1].program_id == PUMP_SWAP_PROGRAM


@pytest.mark.asyncio
async def test_pumpswap_sell_extends_short_pool_before_trading() -> None:
    token = _migrated_token()
    token.pool_needs_extension = True
    user = Pubkey.new_unique()
    builder = PumpFunInstructionBuilder(_IdlParser())

    instructions = await builder.build_sell_instruction(
        token,
        user,
        amount_in=1_000_000,
        minimum_amount_out=500_000,
        address_provider=SimpleNamespace(),
    )

    extend_instruction = instructions[0]
    assert extend_instruction.program_id == PUMP_SWAP_PROGRAM
    assert bytes(extend_instruction.data) == bytes(
        (234, 102, 194, 203, 150, 72, 62, 229)
    )
    assert extend_instruction.accounts[0].pubkey == token.pool_state
    assert extend_instruction.accounts[1].pubkey == user


def test_canonical_pool_derivation_is_bound_to_mint_and_quote() -> None:
    mint = Pubkey.new_unique()
    quote = WSOL_MINT
    authority = PumpSwapAddresses.derive_pool_authority(mint)
    expected = Pubkey.find_program_address(
        [b"pool", b"\x00\x00", bytes(authority), bytes(mint), bytes(quote)],
        PUMP_SWAP_PROGRAM,
    )[0]

    assert PumpSwapAddresses.derive_canonical_pool(mint, quote) == expected


def _pool_account(
    *,
    mint: Pubkey,
    quote_mint: Pubkey,
    base_vault: Pubkey,
    quote_vault: Pubkey,
    coin_creator: Pubkey,
    padding: int = 40,
) -> Account:
    authority = PumpSwapAddresses.derive_pool_authority(mint)
    _, bump = Pubkey.find_program_address(
        [
            b"pool",
            struct.pack("<H", 0),
            bytes(authority),
            bytes(mint),
            bytes(quote_mint),
        ],
        PUMP_SWAP_PROGRAM,
    )
    data = bytearray(PUMP_SWAP_POOL_DISCRIMINATOR)
    data += bytes((bump,))
    data += struct.pack("<H", 0)
    data += bytes(authority)
    data += bytes(mint)
    data += bytes(quote_mint)
    data += bytes(Pubkey.new_unique())
    data += bytes(base_vault)
    data += bytes(quote_vault)
    data += struct.pack("<Q", 1)
    data += bytes(coin_creator)
    data += bytes((0, 0))
    data += (500_000_000).to_bytes(16, "little", signed=True)
    data += bytes(padding)
    return Account(1, bytes(data), PUMP_SWAP_PROGRAM, False, 0)


def _global_config_account(
    protocol_recipients: list[Pubkey],
    buyback_recipients: list[Pubkey],
) -> Account:
    data = bytearray(PUMP_SWAP_GLOBAL_CONFIG_DISCRIMINATOR)
    data += bytes(Pubkey.new_unique())
    data += struct.pack("<QQB", 20, 5, 0)
    for recipient in protocol_recipients:
        data += bytes(recipient)
    data += struct.pack("<Q", 30)
    data += bytes(Pubkey.new_unique())
    data += bytes(Pubkey.new_unique())
    data += bytes(protocol_recipients[0])
    data += bytes((0,))
    for recipient in protocol_recipients[1:]:
        data += bytes(recipient)
    data += bytes((0,))
    for recipient in buyback_recipients:
        data += bytes(recipient)
    data += struct.pack("<Q", 0)
    data += bytes(40)
    return Account(1, bytes(data), PUMP_SWAP_PROGRAM, False, 0)


def _token_account(
    mint: Pubkey,
    amount: int,
    owner_program: Pubkey,
    authority: Pubkey,
) -> Account:
    data = bytearray(bytes(mint))
    data += bytes(authority)
    data += struct.pack("<Q", amount)
    data += bytes(36)
    data += bytes((1,))
    data += bytes(56)
    return Account(1, bytes(data), owner_program, False, 0)


class _FeeSchedule:
    def __init__(self) -> None:
        self.snapshot = _snapshot()

    async def accept_account(self, account: Account) -> PumpFeeSnapshot:
        del account
        return self.snapshot

    def require_snapshot(self) -> PumpFeeSnapshot:
        return self.snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pool_padding", "needs_extension"),
    [(40, False), (0, True)],
)
async def test_manager_loads_slot_consistent_canonical_pool_state(
    pool_padding: int,
    needs_extension: bool,
) -> None:
    mint = Pubkey.new_unique()
    base_program = TOKEN_2022_PROGRAM
    base_vault = Pubkey.new_unique()
    quote_vault = Pubkey.new_unique()
    coin_creator = Pubkey.new_unique()
    pool = PumpSwapAddresses.derive_canonical_pool(mint, WSOL_MINT)
    pool_account = _pool_account(
        mint=mint,
        quote_mint=WSOL_MINT,
        base_vault=base_vault,
        quote_vault=quote_vault,
        coin_creator=coin_creator,
        padding=pool_padding,
    )
    protocol_recipients = [Pubkey.new_unique() for _ in range(8)]
    buyback_recipients = [Pubkey.new_unique() for _ in range(8)]
    global_account = _global_config_account(protocol_recipients, buyback_recipients)
    mint_data = bytearray(82)
    mint_data[36:44] = struct.pack("<Q", 1_000_000_000_000_000)
    mint_data[44] = 6
    mint_data[45] = 1
    mint_account = Account(1, bytes(mint_data), base_program, False, 0)
    fee_account = Account(1, b"fee", Pubkey.new_unique(), False, 0)

    class Client:
        async def get_account_info(
            self, address: Pubkey, commitment: str | None = None
        ) -> Account:
            assert address == pool
            assert commitment == "processed"
            return pool_account

        async def get_multiple_accounts(
            self, addresses: list[Pubkey], commitment: str | None = None
        ) -> list[Account]:
            assert addresses == [
                pool,
                PUMP_SWAP_GLOBAL_CONFIG,
                mint,
                base_vault,
                quote_vault,
                PumpSwapAddresses.find_fee_config(),
            ]
            assert commitment == "processed"
            return [
                pool_account,
                global_account,
                mint_account,
                _token_account(mint, 1_000_000_000, base_program, pool),
                _token_account(WSOL_MINT, 1_500_000_000, TOKEN_PROGRAM, pool),
                fee_account,
            ]

    manager = PumpSwapManager(
        Client(),  # type: ignore[arg-type]
        fee_schedule=_FeeSchedule(),  # type: ignore[arg-type]
        chooser=lambda recipients: recipients[0],
    )

    state, token_program = await manager.load_execution_state(
        mint, WSOL_MINT, commitment="processed"
    )

    assert token_program == base_program
    assert state["venue"] == "pumpswap"
    assert state["pool_address"] == pool
    assert state["creator"] == coin_creator
    assert state["creator_vault"] == PumpSwapAddresses.derive_creator_vault(
        coin_creator
    )
    assert state["protocol_fee_recipient"] == protocol_recipients[0]
    assert state["buyback_fee_recipient"] == buyback_recipients[0]
    assert state["pool_needs_extension"] is needs_extension
    assert await manager.calculate_sell_amount_out(
        pool,
        100_000_000,
        pool_state=state,
    ) == quote_pumpswap_sell(
        base_reserve_raw=1_000_000_000,
        quote_reserve_raw=1_500_000_000,
        virtual_quote_reserve_raw=500_000_000,
        base_supply_raw=1_000_000_000_000_000,
        base_amount_in_raw=100_000_000,
        quote_mint=WSOL_MINT,
        coin_creator=coin_creator,
        fee_snapshot=_snapshot(),
    )


@pytest.mark.asyncio
async def test_completed_bonding_curve_switches_sell_state_to_pumpswap() -> None:
    mint = Pubkey.new_unique()
    bonding_curve = Pubkey.new_unique()
    migrated_state = {
        "venue": "pumpswap",
        "complete": True,
        "quote_mint": WSOL_MINT,
    }
    manager = object.__new__(PumpFunCurveManager)
    manager.get_pool_state_and_token_program = AsyncMock(
        return_value=(
            {"complete": True, "quote_mint": WSOL_MINT},
            TOKEN_2022_PROGRAM,
        )
    )
    manager.pumpswap = SimpleNamespace(
        load_execution_state=AsyncMock(
            return_value=(migrated_state, TOKEN_2022_PROGRAM)
        )
    )

    result = await manager.get_sell_state_and_token_program(
        bonding_curve,
        mint,
        commitment="processed",
    )

    assert result == (migrated_state, TOKEN_2022_PROGRAM)
    manager.pumpswap.load_execution_state.assert_awaited_once_with(
        mint,
        WSOL_MINT,
        commitment="processed",
    )


@pytest.mark.asyncio
async def test_token_price_refresh_applies_migrated_execution_metadata() -> None:
    token = _migrated_token()
    token.bonding_curve = None
    migrated_pool = PumpSwapAddresses.derive_canonical_pool(token.mint, WSOL_MINT)
    state = {
        "venue": "pumpswap",
        "complete": True,
        "is_tradeable": True,
        "status_name": "pumpswap",
        "pool_address": migrated_pool,
        "base_vault": Pubkey.new_unique(),
        "quote_vault": Pubkey.new_unique(),
        "global_config": PUMP_SWAP_GLOBAL_CONFIG,
        "platform_config": PumpSwapAddresses.find_fee_config(),
        "creator": Pubkey.new_unique(),
        "creator_vault": Pubkey.new_unique(),
        "protocol_fee_recipient": Pubkey.new_unique(),
        "buyback_fee_recipient": Pubkey.new_unique(),
        "pool_needs_extension": True,
        "quote_mint": WSOL_MINT,
        "quote_token_program": TOKEN_PROGRAM,
        "base_decimals": 6,
        "quote_decimals": 9,
    }
    manager = object.__new__(PumpFunCurveManager)
    manager.get_sell_state_and_token_program = AsyncMock(
        return_value=(state, TOKEN_2022_PROGRAM)
    )
    manager.pumpswap = SimpleNamespace(calculate_price=lambda pool, snapshot: 1.25)

    price = await manager.calculate_token_price(token)

    assert price == 1.25
    assert token.pool_state == migrated_pool
    assert token.curve_complete is True
    assert token.pool_status == "pumpswap"
    assert token.creator == state["creator"]
    assert token.creator_vault == state["creator_vault"]
    assert token.protocol_fee_recipient == state["protocol_fee_recipient"]
    assert token.buyback_fee_recipient == state["buyback_fee_recipient"]
    assert token.pool_needs_extension is True
