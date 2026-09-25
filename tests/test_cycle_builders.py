# ruff: noqa: S101, PLR2004
"""Offline unit checks for the per-venue cycle instruction builders.

No network, no signing: build a full 2-leg cycle wire for synthetic
pAMM+curve state and assert program IDs, IDL discriminators, and account
counts against the vendored IDLs.
"""

import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from core.client import COMPUTE_BUDGET_PROGRAM_ID, estimate_transaction_fee_lamports
from core.cycles.core import AMM, CPMM
from core.cycles.discovery import CycleCandidate, CycleLeg
from core.pubkeys import ASSOCIATED_TOKEN_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
from cycles.builders import (
    PAMM_PROGRAM,
    PUMP_PROGRAM,
    SLIPPAGE_FLOOR,
    build_curve_buy_instructions,
    build_curve_sell_instructions,
    build_pamm_sell_instructions,
    curve_token_info,
    pamm_fee_recipients,
)
from cycles.runner import build_cycle_instructions
from platforms.pumpfun.pumpswap import (
    PUMP_SWAP_GLOBAL_CONFIG_DISCRIMINATOR,
    PumpSwapAddresses,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IDL_DIR = PROJECT_ROOT / "idl"

MINT = Pubkey.from_string("CU7nUQaJ4beyYjC3xAUrh5RiSjw14fhU6oWTwRBse8gj")
CREATOR = Pubkey.from_string("5wyFsNExysbXf2hTtcn8Tqd3urs9Nv85Zx1zNdAfTMmX")
USER = Pubkey.from_string("Ba99j1dYxidfQZvuNGMaXGxJsUeWXu6VNW8damkrdLVd")


def _idl(name: str, instruction: str) -> dict:
    idl = json.loads((IDL_DIR / name).read_text())
    return next(i for i in idl["instructions"] if i["name"] == instruction)


def _curve_state() -> dict:
    return {
        "virtual_sol_reserves": 2_000_000_000,
        "virtual_token_reserves": 500_000_000_000_000,
        "real_sol_reserves": 100_000_000,
        "real_token_reserves": 10_000_000_000_000,
        "token_total_supply": 1_000_000_000_000_000,
        "complete": True,
        "creator": str(CREATOR),
        "is_mayhem_mode": False,
        "is_cashback_coin": False,
        "base_token_program": str(TOKEN_PROGRAM),
    }


def _pamm_state() -> dict:
    return {
        "pool_address": str(PumpSwapAddresses.derive_canonical_pool(MINT, WSOL_MINT)),
        "program": str(PAMM_PROGRAM),
        "base_mint": str(MINT),
        "base_vault": str(Pubkey.new_unique()),
        "quote_vault": str(Pubkey.new_unique()),
        "base_token_program": str(TOKEN_PROGRAM),
        "base_reserve_raw": 800_000_000_000_000,
        "quote_reserve_raw": 3_000_000_000,
        "virtual_quote_reserve_raw": 0,
        "coin_creator": str(CREATOR),
        "is_mayhem_mode": False,
        "is_cashback_coin": False,
        "needs_extension": False,
    }


def _fee_recipients() -> tuple[Pubkey, Pubkey]:
    return Pubkey.new_unique(), Pubkey.new_unique()


def _candidate() -> CycleCandidate:
    buy = CycleLeg(
        venue=str(PUMP_PROGRAM),
        program=str(PUMP_PROGRAM),
        input_mint=str(WSOL_MINT),
        output_mint=str(MINT),
        amount_in=10_000_000,
        amount_out=2_000_000_000_000,
    )
    sell = CycleLeg(
        venue=str(PumpSwapAddresses.derive_canonical_pool(MINT, WSOL_MINT)),
        program=str(PAMM_PROGRAM),
        input_mint=str(MINT),
        output_mint=str(WSOL_MINT),
        amount_in=2_000_000_000_000,
        amount_out=11_000_000,
    )
    return CycleCandidate(
        mints=(str(MINT),),
        pools=(str(PUMP_PROGRAM), sell.venue),
        programs=(str(PUMP_PROGRAM), str(PAMM_PROGRAM)),
        buy_leg=buy,
        sell_leg=sell,
        expected_out_raw=11_000_000,
        created_slot=0,
    )


def test_curve_buy_v2_matches_idl() -> None:
    idl = _idl("pump_fun_idl.json", "buy_v2")
    instructions = build_curve_buy_instructions(
        token_info=curve_token_info(MINT, _curve_state()),
        user=USER,
        amount_in=10_000_000,
        min_tokens_out=1_900_000_000_000,
    )
    assert len(instructions) == 2  # base ATA create + swap (native SOL quote)
    swap = instructions[-1]
    assert swap.program_id == PUMP_PROGRAM
    assert bytes(swap.data[:8]) == bytes(idl["discriminator"])
    assert len(swap.accounts) == len(idl["accounts"])


def test_curve_sell_v2_matches_idl() -> None:
    idl = _idl("pump_fun_idl.json", "sell_v2")
    instructions = build_curve_sell_instructions(
        token_info=curve_token_info(MINT, _curve_state()),
        user=USER,
        amount_in=2_000_000_000_000,
        min_quote_out=9_000_000,
    )
    assert len(instructions) == 1  # SOL-paired sale pays native SOL
    swap = instructions[0]
    assert swap.program_id == PUMP_PROGRAM
    assert bytes(swap.data[:8]) == bytes(idl["discriminator"])
    assert len(swap.accounts) == len(idl["accounts"]) == 26


def test_pamm_sell_matches_idl() -> None:
    idl = _idl("pump_swap_idl.json", "sell")
    instructions = build_pamm_sell_instructions(
        user=USER,
        pool_state=_pamm_state(),
        base_token_program=TOKEN_PROGRAM,
        amount_in=2_000_000_000_000,
        min_quote_out=9_000_000,
        fee_recipients=_fee_recipients(),
    )
    # WSOL ATA create + swap + close (unwrap).
    assert len(instructions) == 3
    swap = instructions[1]
    assert swap.program_id == PAMM_PROGRAM
    assert bytes(swap.data[:8]) == bytes(idl["discriminator"])
    assert swap.accounts[0].pubkey == Pubkey.from_string(_pamm_state()["pool_address"])
    assert swap.accounts[1].pubkey == USER


def test_full_cycle_wire() -> None:
    instructions = build_cycle_instructions(
        _candidate(),
        USER,
        fee_recipients=_fee_recipients(),
        curve_fee_bps=125,
        curve_state=_curve_state(),
        pamm_state=_pamm_state(),
    )
    # curve buy: ATA + buy_v2; pAMM sell: WSOL ATA + sell + close (unwrap).
    assert [i.program_id for i in instructions] == [
        ASSOCIATED_TOKEN_PROGRAM,
        PUMP_PROGRAM,
        ASSOCIATED_TOKEN_PROGRAM,
        PAMM_PROGRAM,
        TOKEN_PROGRAM,
    ]
    for instruction in instructions:
        assert instruction.program_id != COMPUTE_BUDGET_PROGRAM_ID
    assert instructions[1].data[:8] == bytes(
        _idl("pump_fun_idl.json", "buy_v2")["discriminator"]
    )
    assert instructions[3].data[:8] == bytes(
        _idl("pump_swap_idl.json", "sell")["discriminator"]
    )
    # Args encode the 2%-slippage floors from the candidate legs.
    buy_floor = int(2_000_000_000_000 * (1 - SLIPPAGE_FLOOR))
    sell_floor = int(11_000_000 * (1 - SLIPPAGE_FLOOR))
    assert instructions[1].data[8:16] == buy_floor.to_bytes(8, "little")
    assert instructions[1].data[16:24] == (10_000_000).to_bytes(8, "little")
    assert instructions[3].data[8:16] == (2_000_000_000_000).to_bytes(8, "little")
    assert instructions[3].data[16:24] == sell_floor.to_bytes(8, "little")


def test_amm_v4_and_cpmm_legs_raise() -> None:
    for program in (AMM, CPMM):
        candidate = _candidate()
        broken_buy = CycleLeg(
            venue="pool",
            program=program,
            input_mint=str(WSOL_MINT),
            output_mint=str(MINT),
            amount_in=10_000_000,
            amount_out=1,
        )
        object.__setattr__(candidate, "buy_leg", broken_buy)
        with pytest.raises(ValueError, match="not wired"):
            build_cycle_instructions(
                candidate,
                USER,
                fee_recipients=_fee_recipients(),
                curve_fee_bps=125,
                curve_state=_curve_state(),
                pamm_state=_pamm_state(),
            )


def test_pamm_buy_direction_raises() -> None:
    candidate = _candidate()
    pamm_buy = CycleLeg(
        venue=_candidate().sell_leg.venue,
        program=str(PAMM_PROGRAM),
        input_mint=str(WSOL_MINT),
        output_mint=str(MINT),
        amount_in=10_000_000,
        amount_out=2_000_000_000_000,
    )
    object.__setattr__(candidate, "buy_leg", pamm_buy)
    with pytest.raises(ValueError, match="not wired"):
        build_cycle_instructions(
            candidate,
            USER,
            fee_recipients=_fee_recipients(),
            curve_fee_bps=125,
            curve_state=_curve_state(),
            pamm_state=_pamm_state(),
        )


def test_pamm_sell_without_pool_state_raises() -> None:
    with pytest.raises(ValueError, match="pool state"):
        build_cycle_instructions(
            _candidate(),
            USER,
            fee_recipients=_fee_recipients(),
            curve_fee_bps=125,
            curve_state=_curve_state(),
            pamm_state={},
        )


def test_fee_budget_uses_estimator() -> None:
    # 5,000 lamports signature + 180,000 CU at 500,000 µlam/CU.
    assert estimate_transaction_fee_lamports(500_000, 180_000) == 95_000

def test_pamm_fee_recipients_decoding() -> None:

    protocol = tuple(Pubkey.new_unique() for _ in range(8))
    reserved = tuple(Pubkey.new_unique() for _ in range(8))
    buyback = tuple(Pubkey.new_unique() for _ in range(8))
    data = bytearray(940)
    data[:8] = PUMP_SWAP_GLOBAL_CONFIG_DISCRIMINATOR
    data[56] = 0  # sells enabled
    for index, recipient in enumerate(protocol):
        data[57 + index * 32 : 89 + index * 32] = bytes(recipient)
    data[385:417] = bytes(reserved[0])
    data[417] = 0
    for index, recipient in enumerate(reserved[1:]):
        data[418 + index * 32 : 450 + index * 32] = bytes(recipient)
    data[642] = 0
    for index, recipient in enumerate(buyback):
        data[643 + index * 32 : 675 + index * 32] = bytes(recipient)
    data[939] = 0

    first = lambda group: group[0]  # noqa: E731
    assert pamm_fee_recipients(bytes(data), is_mayhem_mode=False, chooser=first) == (
        protocol[0],
        buyback[0],
    )
    assert pamm_fee_recipients(bytes(data), is_mayhem_mode=True, chooser=first) == (
        reserved[0],
        buyback[0],
    )

