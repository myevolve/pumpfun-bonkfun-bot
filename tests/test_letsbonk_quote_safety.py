# ruff: noqa: S101
from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock

import pytest
from solders.pubkey import Pubkey

from core.cycles.core import AMM, CPMM
from core.pubkeys import SystemAddresses
from cycles.letsbonk_reader import LetsBonkGraduationReader
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from platforms.letsbonk.curve_manager import (
    LaunchLabCurveType,
    LaunchLabFees,
    LaunchLabMigrateType,
    LaunchLabPoolStatus,
    LetsBonkCurveManager,
)
from platforms.letsbonk.event_parser import LetsBonkEventParser


def _executable_pool_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "curve_type": LaunchLabCurveType.CONSTANT_PRODUCT,
        "status": LaunchLabPoolStatus.FUNDING,
        "migrate_type": LaunchLabMigrateType.AMM,
        "virtual_base": 1_000,
        "virtual_quote": 1_000,
        "real_base": 2,
        "real_quote": 3,
        "fees": LaunchLabFees(0, 0, 0),
        "base_token_program": SystemAddresses.TOKEN_PROGRAM,
        "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
        "base_transfer_fee": None,
        "quote_transfer_fee": None,
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_launchlab_quotes_are_capped_by_real_reserves() -> None:
    manager = object.__new__(LetsBonkCurveManager)
    manager.get_pool_state = AsyncMock(return_value=_executable_pool_state())

    assert await manager.calculate_buy_amount_out(Pubkey.new_unique(), 1_000) == 2
    assert await manager.calculate_sell_amount_out(Pubkey.new_unique(), 1_000) == 3


@pytest.mark.asyncio
async def test_launchlab_quotes_reuse_provided_pool_state() -> None:
    manager = object.__new__(LetsBonkCurveManager)
    manager.get_pool_state = AsyncMock(
        side_effect=AssertionError("provided state must avoid an RPC read")
    )
    state = _executable_pool_state()
    pool = Pubkey.new_unique()

    assert await manager.calculate_buy_amount_out(pool, 1_000, pool_state=state) == 2
    assert await manager.calculate_sell_amount_out(pool, 1_000, pool_state=state) == 3
    manager.get_pool_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_launchlab_exhausted_and_invalid_real_reserves_fail_safe() -> None:
    manager = object.__new__(LetsBonkCurveManager)
    manager.get_pool_state = AsyncMock(return_value=_executable_pool_state(real_base=0))

    assert await manager.calculate_buy_amount_out(Pubkey.new_unique(), 1) == 0

    for invalid_reserve in (-1, float("nan"), float("inf")):
        manager.get_pool_state.return_value = _executable_pool_state(
            real_quote=invalid_reserve
        )
        with pytest.raises(ValueError, match="real_quote"):
            await manager.calculate_sell_amount_out(Pubkey.new_unique(), 1)


@pytest.mark.asyncio
async def test_launchlab_transfer_fee_uses_ceiling_for_buy_and_sell() -> None:
    manager = object.__new__(LetsBonkCurveManager)
    transfer_fee = {"epoch": 7, "basis_points": 1, "maximum_fee_raw": 10}
    manager.get_pool_state = AsyncMock(
        return_value=_executable_pool_state(
            virtual_base=4,
            virtual_quote=1,
            real_base=10,
            real_quote=10,
            base_token_program=SystemAddresses.TOKEN_2022_PROGRAM,
            base_transfer_fee=transfer_fee,
        )
    )

    assert await manager.calculate_buy_amount_out(Pubkey.new_unique(), 1) == 1
    manager.get_pool_state.return_value = _executable_pool_state(
        virtual_base=1,
        virtual_quote=4,
        real_base=10,
        real_quote=10,
        base_token_program=SystemAddresses.TOKEN_2022_PROGRAM,
        base_transfer_fee=transfer_fee,
    )
    assert await manager.calculate_sell_amount_out(Pubkey.new_unique(), 2) == 2


def _token_2022_mint_data(
    *,
    older_epoch: int,
    older_bps: int,
    older_maximum: int,
    newer_epoch: int,
    newer_bps: int,
    newer_maximum: int,
) -> bytes:
    mint = bytearray(82)
    mint[44] = 6
    mint[45] = 1
    extension = (
        bytes(64)
        + struct.pack("<Q", 0)
        + struct.pack("<QQH", older_epoch, older_maximum, older_bps)
        + struct.pack("<QQH", newer_epoch, newer_maximum, newer_bps)
    )
    return bytes(mint) + b"\x01" + struct.pack("<HH", 1, len(extension)) + extension


def test_token_2022_transfer_fee_selects_exact_current_epoch_schedule() -> None:
    mint_data = _token_2022_mint_data(
        older_epoch=0,
        older_bps=100,
        older_maximum=5,
        newer_epoch=10,
        newer_bps=1,
        newer_maximum=10,
    )

    older = LetsBonkCurveManager._decode_transfer_fee_schedule(
        mint_data, current_epoch=9, expected_decimals=6
    )
    newer = LetsBonkCurveManager._decode_transfer_fee_schedule(
        mint_data, current_epoch=10, expected_decimals=6
    )

    assert older == {"epoch": 0, "basis_points": 100, "maximum_fee_raw": 5}
    assert newer == {"epoch": 10, "basis_points": 1, "maximum_fee_raw": 10}


def test_token_2022_mint_without_transfer_fee_remains_supported() -> None:
    mint = bytearray(82)
    mint[44] = 6
    mint[45] = 1

    assert (
        LetsBonkCurveManager._decode_transfer_fee_schedule(
            bytes(mint) + b"\x01", current_epoch=0, expected_decimals=6
        )
        is None
    )


def test_malformed_token_2022_transfer_fee_fails_closed() -> None:
    mint = bytearray(82)
    mint[44] = 6
    mint[45] = 1
    malformed_extension = (
        bytes(mint) + b"\x01" + struct.pack("<HH", 1, 108) + bytes(107)
    )

    with pytest.raises(ValueError, match="malformed"):
        LetsBonkCurveManager._decode_transfer_fee_schedule(
            malformed_extension, current_epoch=0, expected_decimals=6
        )


class _InstructionParser:
    def __init__(self, decoded: dict[str, object]) -> None:
        self.decoded = decoded

    def get_instruction_discriminators(self) -> dict[str, bytes]:
        return {
            "initialize": b"initialize"[:8],
            "initialize_v2": b"init_v2_",
            "initialize_with_token_2022": b"token22_",
        }

    def decode_instruction(
        self, instruction_data: bytes, account_keys: list[bytes], accounts: list[int]
    ) -> dict[str, object]:
        return self.decoded


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "   "),
        ("name", 7),
        ("symbol", ""),
        ("symbol", "   "),
        ("symbol", 7),
    ],
)
def test_letsbonk_parser_rejects_invalid_name_and_symbol(
    field: str, value: object
) -> None:
    account_names = (
        "payer",
        "creator",
        "global_config",
        "platform_config",
        "pool_state",
        "base_mint",
        "quote_mint",
        "base_vault",
        "quote_vault",
    )
    decoded_accounts = {name: str(Pubkey.new_unique()) for name in account_names}
    decoded_accounts.update(
        {
            "base_token_program": str(SystemAddresses.TOKEN_PROGRAM),
            "quote_token_program": str(SystemAddresses.TOKEN_PROGRAM),
        }
    )
    metadata: dict[str, object] = {
        "name": "Token",
        "symbol": "T",
        "uri": "",
    }
    metadata[field] = value
    parser = LetsBonkEventParser(
        _InstructionParser(
            {
                "instruction_name": "initialize_v2",
                "accounts": decoded_accounts,
                "args": {"base_mint_param": metadata},
            }
        )
    )

    assert parser.parse_token_creation_from_instruction(b"init_v2_", [], []) is None


def test_event_parser_preserves_initial_transfer_fee_metadata() -> None:
    account_names = (
        "payer",
        "creator",
        "global_config",
        "platform_config",
        "pool_state",
        "base_mint",
        "quote_mint",
        "base_vault",
        "quote_vault",
    )
    decoded_accounts = {name: str(Pubkey.new_unique()) for name in account_names}
    decoded_accounts.update(
        {
            "base_token_program": str(SystemAddresses.TOKEN_2022_PROGRAM),
            "quote_token_program": str(SystemAddresses.TOKEN_PROGRAM),
        }
    )
    transfer_fee = {"transfer_fee_basis_points": 1, "maximum_fee": 10}
    parser = LetsBonkEventParser(
        _InstructionParser(
            {
                "instruction_name": "initialize_with_token_2022",
                "accounts": decoded_accounts,
                "args": {
                    "base_mint_param": {"name": "Token", "symbol": "T", "uri": ""},
                    "curve_param": {"variant": "ConstantProductCurve"},
                    "transfer_fee_extension_param": transfer_fee,
                },
            }
        )
    )

    token = parser.parse_token_creation_from_instruction(b"token22_", [], [])

    assert token is not None
    assert token.additional_data["transfer_fee_extension_param"] == transfer_fee


def _raw_pool_state_fixture(
    migrate_type: object,
) -> tuple[
    LetsBonkCurveManager,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    Pubkey,
]:
    provider = LetsBonkAddressProvider()
    base_mint = Pubkey.new_unique()
    quote_mint = SystemAddresses.SOL_MINT
    pool_address = provider.derive_pool_address(base_mint, quote_mint)
    manager = object.__new__(LetsBonkCurveManager)
    manager.address_provider = provider
    pool_state: dict[str, object] = {
        "status": 0,
        "migrate_type": migrate_type,
        "base_decimals": 6,
        "quote_decimals": 9,
        "supply": 1_000_000_000,
        "virtual_base": 1_000_000,
        "virtual_quote": 1_000_000,
        "real_base": 500_000,
        "real_quote": 500_000,
        "token_program_flag": 0,
        "global_config": Pubkey.new_unique(),
        "platform_config": Pubkey.new_unique(),
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "base_vault": provider.derive_base_vault(base_mint, quote_mint),
        "quote_vault": provider.derive_quote_vault(base_mint, quote_mint),
        "creator": Pubkey.new_unique(),
    }
    decoded_global: dict[str, object] = {
        "curve_type": 0,
        "quote_mint": quote_mint,
        "trade_fee_rate": 2_500,
    }
    decoded_platform: dict[str, object] = {
        "fee_rate": 5_000,
        "creator_fee_rate": 2_500,
    }
    return manager, pool_state, decoded_global, decoded_platform, pool_address


@pytest.mark.parametrize(
    ("raw_migrate_type", "expected_type", "expected_name"),
    [
        (0, LaunchLabMigrateType.AMM, "amm"),
        (1, LaunchLabMigrateType.CPSWAP, "cpswap"),
    ],
)
def test_launchlab_pool_state_decodes_amm_and_cpswap_migrate_types(
    raw_migrate_type: int,
    expected_type: LaunchLabMigrateType,
    expected_name: str,
) -> None:
    manager, pool_state, decoded_global, decoded_platform, pool_address = (
        _raw_pool_state_fixture(raw_migrate_type)
    )
    parser = MagicMock()
    parser.decode_account_data.return_value = dict(pool_state)
    manager._idl_parser = parser

    decoded_raw = manager._decode_pool_state_with_idl(b"pool")
    enriched = manager._attach_authoritative_metadata(
        decoded_raw,
        decoded_global=decoded_global,
        decoded_platform=decoded_platform,
        pool_address=pool_address,
        base_transfer_fee=None,
        quote_transfer_fee=None,
    )

    assert enriched["migrate_type"] == expected_type
    assert isinstance(enriched["migrate_type"], LaunchLabMigrateType)
    assert enriched["migrate_type_name"] == expected_name


def test_launchlab_pool_state_rejects_invalid_migrate_type() -> None:
    manager, pool_state, decoded_global, decoded_platform, pool_address = (
        _raw_pool_state_fixture(2)
    )
    with pytest.raises(ValueError, match="Unknown LaunchLab migrate_type 2"):
        manager._attach_authoritative_metadata(
            pool_state,
            decoded_global=decoded_global,
            decoded_platform=decoded_platform,
            pool_address=pool_address,
            base_transfer_fee=None,
            quote_transfer_fee=None,
        )

    missing_migrate_type = dict(pool_state)
    missing_migrate_type.pop("migrate_type")
    parser = MagicMock()
    parser.decode_account_data.return_value = missing_migrate_type
    manager._idl_parser = parser
    with pytest.raises(ValueError, match="migrate_type"):
        manager._decode_pool_state_with_idl(b"pool")


@pytest.mark.asyncio
async def test_letsbonk_graduation_reader_resolves_cpswap_and_amm_targets() -> None:
    reader = object.__new__(LetsBonkGraduationReader)
    manager = object.__new__(LetsBonkCurveManager)
    mint = str(Pubkey.new_unique())
    manager.get_pool_state = AsyncMock(
        return_value={"migrate_type": LaunchLabMigrateType.CPSWAP}
    )
    reader._manager = manager

    assert await reader.migration_target(mint) == str(CPMM)

    manager.get_pool_state.return_value = {"migrate_type": LaunchLabMigrateType.AMM}
    assert await reader.migration_target(mint) == str(AMM)
