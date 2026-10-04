from __future__ import annotations

# ruff: noqa: S101, SLF001 - assertions and direct lifecycle state exercise safety boundaries
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.pubkey import Pubkey
from test_trade_flow import (
    CREATOR,
    MINT,
    OTHER,
    PUMP_PROGRAM,
    _encode_trade_event,
    _event,
)

from core.execution_policy import ExecutionPolicy
from interfaces.core import Platform, TokenInfo
from monitoring.trade_flow import (
    EntryGate,
    FlowRules,
    GateRules,
    TradeEvent,
    TradeFlowHub,
    TradeFlowLossError,
    TradeQueue,
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


def test_gate_rejects_precreation_and_backwards_slots() -> None:
    decision = _gate().observe(_event(real=200_000_000, slot=CREATION_SLOT - 1))
    assert decision is not None and not decision.accept
    assert decision.reason == "trade_before_creation"
    assert decision.slots_waited == 0
    assert decision.buyers == 0 and decision.last_event is None

    first = _event(real=200_000_000, slot=CREATION_SLOT + 2)
    second_user = str(Pubkey.from_bytes(bytes([3]) * 32))
    for slot_delta in (1, 2):
        gate = _gate(min_buyers=2)
        assert gate.observe(first) is None
        decision = gate.observe(
            _event(user=second_user, real=300_000_000, slot=CREATION_SLOT + slot_delta)
        )
        assert decision is not None
        if slot_delta == 1:
            assert not decision.accept
            assert decision.reason == "trade_stream_out_of_order"
            assert decision.last_event is first and decision.buyers == 1
        else:
            assert decision.accept
            assert decision.buyers == 2  # noqa: PLR2004 - two distinct same-slot buyers


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
    pump = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    logs = [
        f"Program {pump} invoke [1]",
        _encode_trade_event(parser),
        _encode_trade_event(parser, mint=OTHER),
        f"Program {pump} success",
    ]
    assert (
        hub.publish_logs(logs, slot=1, signature="s") == 0
    )  # nobody listening: no decode
    queue = hub.subscribe(MINT)
    assert hub.active
    assert hub.publish_logs(logs, slot=1, signature="s") == 1
    event = queue.get_nowait()
    assert isinstance(event, TradeEvent) and event.mint == MINT
    hub.publish_logs(logs, slot=2, signature="s2")
    hub.publish_logs(logs, slot=3, signature="s3")
    hub.publish_logs(logs, slot=4, signature="s4")
    assert hub.dropped == 3  # noqa: PLR2004 - two invalidated queued events plus overflow
    with pytest.raises(TradeFlowLossError):
        await queue.get()
    hub.unsubscribe(MINT, queue)
    assert not hub.active


def _gated_trader(rules: GateRules) -> tuple[UniversalTrader, TokenInfo, TradeQueue]:
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
    queue = TradeQueue()
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


@pytest.mark.asyncio
@pytest.mark.parametrize("loss_reason", ["overflow", "interrupted", "out_of_order"])
@pytest.mark.parametrize(
    ("min_buyers", "wait_before_loss"),
    [(1, False), (1, True), (0, False)],
    ids=["already-overflowed", "waiting-reader", "no-wait-gate"],
)
async def test_history_loss_rejects_entry_and_disables_flow_exit(
    min_buyers: int, loss_reason: str, *, wait_before_loss: bool
) -> None:
    trader, token, _ = _gated_trader(GateRules(min_buyers=min_buyers))
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser, queue_size=1 if loss_reason == "overflow" else 2)
    trader._gate_queues[MINT] = hub.subscribe(MINT)
    pending = None
    if wait_before_loss:
        pending = asyncio.create_task(trader._await_entry_gate(token))
        await asyncio.sleep(0)
    for signature, user in (("buyer", OTHER), ("creator-sell", CREATOR)):
        hub.publish_logs(
            [
                f"Program {PUMP_PROGRAM} invoke [1]",
                _encode_trade_event(
                    parser,
                    user=user,
                    is_buy=user == OTHER,
                    real_sol_reserves=200_000_000,
                ),
                f"Program {PUMP_PROGRAM} success",
            ],
            slot=CREATION_SLOT
            + (0 if loss_reason == "out_of_order" and user == CREATOR else 1),
            signature=signature,
        )
    if loss_reason == "interrupted":
        hub.stream_interrupted()
    before = (token.virtual_quote_reserves, token.virtual_token_reserves)
    decision = await asyncio.wait_for(
        pending if pending is not None else trader._await_entry_gate(token), 1
    )
    assert decision is not None and not decision.accept
    assert decision.reason == f"trade_stream_{loss_reason}"
    assert decision.last_event is None
    assert (token.virtual_quote_reserves, token.virtual_token_reserves) == before

    trader.flow_rules = FlowRules()
    trader._flow_signals = {}
    trader._flow_latched = set()
    trader._flow_wakeups = {MINT: asyncio.Event()}
    position = SimpleNamespace(entry_price=1.0, is_active=True)
    await asyncio.wait_for(trader._consume_trade_flow(token, position), 1)
    assert position.is_active
    assert trader._flow_signals == {}
    assert not trader._flow_wakeups[MINT].is_set()
