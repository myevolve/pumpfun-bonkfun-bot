from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.pubkey import Pubkey
from test_trade_flow import CREATOR, MINT, OTHER, _encode_trade_event, _event

from core.execution_policy import ExecutionPolicy
from interfaces.core import Platform, TokenInfo
from monitoring.trade_flow import (
    EntryGate,
    GateRules,
    TradeEvent,
    TradeFlowHub,
)
from trading.universal_trader import UniversalTrader
from utils.idl_parser import IDLParser

CREATION_SLOT = 444_044_174


def _gate(**rules: object) -> EntryGate:
    return EntryGate(
        mint=MINT,
        creator=CREATOR,
        creation_slot=CREATION_SLOT,
        rules=GateRules(**rules),
    )


def test_gate_accepts_on_first_non_creator_buyer() -> None:
    gate = _gate()
    assert (
        gate.observe(
            _event(user=CREATOR, is_buy=True, real=100_000_000, slot=CREATION_SLOT)
        )
        is None
    )
    decision = gate.observe(
        _event(user=OTHER, is_buy=True, real=300_000_000, slot=CREATION_SLOT + 1)
    )
    assert decision is not None and decision.accept
    assert decision.reason == "buyers_present"
    assert decision.buyers == 1
    assert decision.slots_waited == 1
    assert decision.last_event is not None and decision.last_event.user == OTHER


def test_gate_rejects_creator_sell_and_high_liquidity_and_late_window() -> None:
    assert (
        _gate()
        .observe(
            _event(user=CREATOR, is_buy=False, real=100_000_000, slot=CREATION_SLOT + 1)
        )
        .reason
        == "creator_sold"
    )
    assert (
        _gate()
        .observe(
            _event(user=OTHER, is_buy=True, real=600_000_000, slot=CREATION_SLOT + 1)
        )
        .reason
        == "too_much_sol"
    )
    assert (
        _gate()
        .observe(
            _event(user=OTHER, is_buy=True, real=100_000_000, slot=CREATION_SLOT + 4)
        )
        .reason
        == "window_expired"
    )
    late = _gate(max_wait_slots=10)
    assert late.observe(
        _event(user=OTHER, is_buy=True, real=100_000_000, slot=CREATION_SLOT + 4)
    ).accept


def test_gate_waits_for_minimum_liquidity_and_ignores_mayhem_vault() -> None:
    mayhem_vault = str(
        Pubkey.find_program_address(
            [b"sol-vault"],
            Pubkey.from_string("MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"),
        )[0]
    )

    gate = _gate(min_real_sol=0.1)
    # buyer present but only 0.077 SOL in the curve (the live MH loss): wait
    assert (
        gate.observe(
            _event(user=OTHER, is_buy=True, real=77_000_000, slot=CREATION_SLOT + 1)
        )
        is None
    )
    assert gate.observe(
        _event(
            user=mayhem_vault,
            is_buy=True,
            real=150_000_000,
            slot=CREATION_SLOT + 2,
        )
    ).accept
    vault_only = _gate(min_real_sol=0.0)
    assert (
        vault_only.observe(
            _event(
                user=mayhem_vault,
                is_buy=True,
                real=150_000_000,
                slot=CREATION_SLOT + 1,
            )
        )
        is None
    )
    with pytest.raises(ValueError):
        GateRules(min_real_sol=0.5, max_real_sol=0.5)


def test_gate_counts_distinct_buyers_and_times_out() -> None:
    gate = _gate(min_buyers=2)
    assert (
        gate.observe(
            _event(user=OTHER, is_buy=True, real=100_000_000, slot=CREATION_SLOT + 1)
        )
        is None
    )
    assert (
        gate.observe(
            _event(user=OTHER, is_buy=True, real=100_000_000, slot=CREATION_SLOT + 1)
        )
        is None
    )
    timeout = gate.timed_out()
    assert not timeout.accept and timeout.reason == "timeout" and timeout.buyers == 1
    third = str(Pubkey.new_unique())
    assert gate.observe(
        _event(user=third, is_buy=True, real=100_000_000, slot=CREATION_SLOT + 2)
    ).accept


@pytest.mark.parametrize(
    "bad",
    [
        {"min_buyers": -1},
        {"max_real_sol": 0},
        {"max_wait_slots": 0},
        {"max_wait_ms": 0},
    ],
)
def test_gate_rules_validate(bad: dict) -> None:
    with pytest.raises(ValueError):
        GateRules(**bad)


@pytest.mark.asyncio
async def test_hub_fans_out_only_to_subscribed_mints() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser, queue_size=2)
    assert not hub.active
    logs = [_encode_trade_event(parser), _encode_trade_event(parser, mint=OTHER)]
    assert (
        hub.publish_logs(logs, slot=1, signature="s") == 0
    )  # nobody listening: no decode
    queue = hub.subscribe(MINT)
    assert hub.active
    assert hub.publish_logs(logs, slot=1, signature="s") == 1
    event = queue.get_nowait()
    assert isinstance(event, TradeEvent) and event.mint == MINT
    hub.publish_logs(logs, slot=2, signature="s")
    hub.publish_logs(logs, slot=3, signature="s")
    hub.publish_logs(logs, slot=4, signature="s")
    assert hub.dropped == 1  # bounded queue
    hub.unsubscribe(MINT, queue)
    assert not hub.active


def _gated_trader(rules: GateRules) -> tuple[UniversalTrader, TokenInfo, asyncio.Queue]:
    trader = object.__new__(UniversalTrader)
    trader.execution_policy = ExecutionPolicy()
    trader.gate_rules = rules
    trader._gate_queues = {}
    token = TokenInfo(
        name="Token",
        symbol="TOK",
        uri="",
        mint=Pubkey.from_string(MINT),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=True,
        slot=CREATION_SLOT,
        state_from_event=True,
        virtual_quote_reserves=30_000_000_000,
        virtual_token_reserves=1_073_000_000_000_000,
    )
    queue: asyncio.Queue = asyncio.Queue()
    trader._gate_queues[MINT] = queue
    return trader, token, queue


@pytest.mark.asyncio
async def test_trader_gate_accepts_and_refreshes_reserves_from_last_event() -> None:
    trader, token, queue = _gated_trader(GateRules())
    queue.put_nowait(
        _event(
            user=OTHER,
            is_buy=True,
            real=200_000_000,
            vsol=31_000_000_000,
            vtok=1_040_000_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )

    decision = await trader._await_entry_gate(token)

    assert decision is not None and decision.accept
    assert token.virtual_quote_reserves == 31_000_000_000
    assert token.virtual_token_reserves == 1_040_000_000_000_000


@pytest.mark.asyncio
async def test_trader_gate_skips_non_mayhem_without_waiting_and_times_out_on_silence() -> (
    None
):
    trader, token, _ = _gated_trader(GateRules(max_wait_ms=50))
    token.is_mayhem_mode = False
    decision = await trader._await_entry_gate(token)
    assert decision is not None and decision.reason == "not_mayhem"

    trader, token, _ = _gated_trader(GateRules(max_wait_ms=50))
    decision = await asyncio.wait_for(trader._await_entry_gate(token), 2)
    assert decision is not None and decision.reason == "timeout"


@pytest.mark.asyncio
async def test_handle_token_skips_buy_when_gate_rejects() -> None:
    trader, token, queue = _gated_trader(GateRules())
    trader.platform = Platform.PUMP_FUN
    trader.allowed_quote_mints = None
    trader.quote_amounts = {}
    trader.extreme_fast_mode = True
    trader.buyer = SimpleNamespace(execute=AsyncMock())
    trader._pending_recovery_tokens = []
    trader._write_recovery_journal = lambda: None
    trader.yolo_mode = False
    token.state_from_event = False
    queue.put_nowait(
        _event(user=CREATOR, is_buy=False, real=100_000_000, slot=CREATION_SLOT + 1)
    )

    handled = await trader._handle_token(token)

    assert handled is True
    trader.buyer.execute.assert_not_awaited()
