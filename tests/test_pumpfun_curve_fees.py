from __future__ import annotations

import struct

import pytest
from solders.account import Account
from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT, SystemAddresses
from platforms.pumpfun.address_provider import PumpFunAddresses
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from platforms.pumpfun.fee_schedule import (
    FEE_CONFIG_DISCRIMINATOR,
    PumpFeeSchedule,
    PumpFeeSnapshot,
    decode_fee_config_account,
)

_BONDING_CURVE_DISCRIMINATOR = bytes((23, 183, 248, 55, 96, 216, 172, 96))


def _encode_fees(fees: tuple[int, int, int]) -> bytes:
    return struct.pack("<QQQ", *fees)


def _fee_account() -> Account:
    data = bytearray(FEE_CONFIG_DISCRIMINATOR)
    data += bytes([253])
    data += bytes(Pubkey.new_unique())
    data += _encode_fees((0, 80, 20))
    data += struct.pack("<I", 1)
    data += (0).to_bytes(16, "little") + _encode_fees((0, 80, 20))
    data += struct.pack("<I", 1)
    data += (0).to_bytes(16, "little") + _encode_fees((0, 50, 10))
    data += bytes(128)
    return Account(1, bytes(data), PumpFunAddresses.FEE_PROGRAM, False, 0)


def _snapshot() -> PumpFeeSnapshot:
    return PumpFeeSnapshot(
        config=decode_fee_config_account(_fee_account()),
        observed_at=1.0,
        attested_at=1.0,
    )


def _decoded_state() -> dict[str, object]:
    return {
        "virtual_token_reserves": 100_000,
        "virtual_quote_reserves": 10_000,
        "real_token_reserves": 50_000,
        "real_quote_reserves": 5_000,
        "token_total_supply": 1_000_000,
        "complete": False,
        "creator": Pubkey.new_unique(),
        "is_mayhem_mode": False,
        "is_cashback_coin": False,
        "quote_mint": WSOL_MINT,
    }


def _curve_account() -> Account:
    return Account(
        1,
        _BONDING_CURVE_DISCRIMINATOR + bytes(107),
        PumpFunAddresses.PROGRAM,
        False,
        0,
    )


class _Parser:
    def decode_account_data(
        self,
        data: bytes,
        account_type: str,
        *,
        skip_discriminator: bool,
    ) -> dict[str, object]:
        del data, account_type, skip_discriminator
        return _decoded_state()


class _Client:
    def __init__(self, accounts: dict[Pubkey, Account]) -> None:
        self.accounts = accounts
        self.requested_batches: list[list[Pubkey]] = []

    async def get_account_info(
        self, pubkey: Pubkey, commitment: str | None = None
    ) -> Account:
        del pubkey, commitment
        raise AssertionError("single-account reads are not allowed for Pump quotes")

    async def get_multiple_accounts(
        self,
        pubkeys: list[Pubkey],
        commitment: str | None = None,
    ) -> list[Account | None]:
        del commitment
        self.requested_batches.append(pubkeys)
        return [self.accounts.get(pubkey) for pubkey in pubkeys]


class _Schedule:
    def __init__(self, snapshot: PumpFeeSnapshot) -> None:
        self.snapshot = snapshot
        self.accepted: list[Account] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def accept_account(self, account: Account) -> PumpFeeSnapshot:
        self.accepted.append(account)
        return self.snapshot

    def require_snapshot(self) -> PumpFeeSnapshot:
        return self.snapshot


def _manager(
    client: _Client,
    schedule: _Schedule,
) -> PumpFunCurveManager:
    return PumpFunCurveManager(
        client,  # type: ignore[arg-type]
        _Parser(),  # type: ignore[arg-type]
        fee_schedule=schedule,  # type: ignore[arg-type]
        pumpswap_manager=_Schedule(schedule.snapshot),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_provided_state_avoids_account_reads_and_applies_buy_fees() -> None:
    client = _Client({})
    schedule = _Schedule(_snapshot())
    manager = _manager(client, schedule)
    state = _decoded_state()
    state["_pump_fee_snapshot"] = schedule.snapshot

    amount_out = await manager.calculate_buy_amount_out(
        Pubkey.new_unique(),
        10_000,
        pool_state=state,
    )

    assert amount_out == 49_746
    assert client.requested_batches == []


@pytest.mark.asyncio
async def test_standalone_quote_batches_curve_and_fee_config() -> None:
    pool = Pubkey.new_unique()
    fee_config = PumpFunAddresses.find_fee_config()
    fee_account = _fee_account()
    client = _Client({pool: _curve_account(), fee_config: fee_account})
    schedule = _Schedule(_snapshot())
    manager = _manager(client, schedule)

    amount_out = await manager.calculate_buy_amount_out(pool, 10_000)

    assert amount_out == 49_746
    assert client.requested_batches == [[pool, fee_config]]
    assert schedule.accepted == [fee_account]


@pytest.mark.asyncio
async def test_curve_and_mint_refresh_reads_three_ordered_accounts() -> None:
    mint = Pubkey.new_unique()
    pool = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)], PumpFunAddresses.PROGRAM
    )[0]
    fee_config = PumpFunAddresses.find_fee_config()
    fee_account = _fee_account()
    mint_account = Account(
        1,
        bytes(82),
        SystemAddresses.TOKEN_2022_PROGRAM,
        False,
        0,
    )
    client = _Client(
        {
            pool: _curve_account(),
            mint: mint_account,
            fee_config: fee_account,
        }
    )
    schedule = _Schedule(_snapshot())
    manager = _manager(client, schedule)

    state, token_program = await manager.get_pool_state_and_token_program(
        pool,
        mint,
        commitment="processed",
    )

    assert client.requested_batches == [[pool, mint, fee_config]]
    assert state["_pump_fee_snapshot"] is schedule.snapshot
    assert token_program == SystemAddresses.TOKEN_2022_PROGRAM


@pytest.mark.asyncio
async def test_curve_refresh_rejects_missing_mint_account() -> None:
    mint = Pubkey.new_unique()
    pool = Pubkey.find_program_address(
        [b"bonding-curve", bytes(mint)], PumpFunAddresses.PROGRAM
    )[0]
    fee_config = PumpFunAddresses.find_fee_config()
    client = _Client(
        {
            pool: _curve_account(),
            fee_config: _fee_account(),
        }
    )
    manager = _manager(client, _Schedule(_snapshot()))

    with pytest.raises(ValueError, match="Mint account .* not found"):
        await manager.get_pool_state_and_token_program(pool, mint)


@pytest.mark.asyncio
async def test_sell_output_and_exact_buy_cost_are_fee_adjusted() -> None:
    client = _Client({})
    schedule = _Schedule(_snapshot())
    manager = _manager(client, schedule)
    state = _decoded_state()
    state["_pump_fee_snapshot"] = schedule.snapshot

    sell_output = await manager.calculate_sell_amount_out(
        Pubkey.new_unique(),
        1_000,
        pool_state=state,
    )
    buy_cost = await manager.calculate_buy_cost(
        Pubkey.new_unique(),
        1_000,
        pool_state=state,
    )

    assert sell_output == 97
    assert buy_cost == 105


@pytest.mark.asyncio
async def test_live_execution_lifecycle_delegates_to_fee_schedule() -> None:
    client = _Client({})
    schedule = _Schedule(_snapshot())
    manager = _manager(client, schedule)

    await manager.prepare_live_execution()
    await manager.close()

    assert schedule.started is True
    assert schedule.closed is True
    assert manager.pumpswap.started is True  # type: ignore[attr-defined]
    assert manager.pumpswap.closed is True  # type: ignore[attr-defined]
    assert isinstance(manager.fee_schedule, PumpFeeSchedule | _Schedule)
