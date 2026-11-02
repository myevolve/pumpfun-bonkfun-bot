from __future__ import annotations

# ruff: noqa: S101, SLF001, PLR2004 - assertions and direct lifecycle state exercise safety boundaries
import asyncio
import struct
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.account import Account
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address
from test_trade_flow import (
    CREATOR,
    MINT,
    OTHER,
    PUMP_PROGRAM,
    _encode_trade_event,
    _event,
)

from core.execution_policy import ExecutionMode, ExecutionPolicy
from core.pubkeys import WSOL_MINT, SystemAddresses
from interfaces.core import Platform, TokenInfo
from learning.journal import LessonJournal, LessonObservation
from learning.report import load_report
from monitoring.trade_flow import (
    MAYHEM_SOL_VAULT,
    EntryGate,
    FlowRules,
    GateRules,
    TradeEvent,
    TradeFlowHub,
    TradeFlowLossError,
    TradeQueue,
)
from platforms.pumpfun.pumpswap import (
    PUMP_SWAP_POOL_DISCRIMINATOR,
    PUMP_SWAP_PROGRAM,
    PumpSwapAddresses,
)
from trading.base import TradeResult
from trading.universal_trader import UniversalTrader, monotonic
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
            assert decision.buyers == 2


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
    assert (
        gate.observe(
            _event(
                user=mayhem_vault,
                is_buy=True,
                real=150_000_000,
                slot=CREATION_SLOT + 2,
            )
        )
        is None
    )
    assert gate.observe(
        _event(
            user=OTHER,
            is_buy=True,
            real=160_000_000,
            slot=CREATION_SLOT + 3,
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


def test_gate_never_accepts_on_sell_creator_or_vault() -> None:
    gate = _gate(min_buyers=1, min_real_sol=0.1, min_wait_slots=1, max_wait_slots=10)
    assert (
        gate.observe(
            _event(user=OTHER, is_buy=True, real=200_000_000, slot=CREATION_SLOT)
        )
        is None
    )
    assert (
        gate.observe(
            _event(user=OTHER, is_buy=False, real=180_000_000, slot=CREATION_SLOT + 1)
        )
        is None
    )
    assert (
        gate.observe(
            _event(user=CREATOR, is_buy=True, real=220_000_000, slot=CREATION_SLOT + 2)
        )
        is None
    )
    assert (
        gate.observe(
            _event(
                user=MAYHEM_SOL_VAULT,
                is_buy=True,
                real=250_000_000,
                slot=CREATION_SLOT + 3,
            )
        )
        is None
    )
    decision = gate.observe(
        _event(user=OTHER, is_buy=True, real=260_000_000, slot=CREATION_SLOT + 4)
    )
    assert decision is not None and decision.accept
    assert decision.reason == "buyers_present"
    assert decision.slots_waited == 4


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
        {"max_creator_launches": 0},
        {"max_creator_launches": True},
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
    assert hub.dropped == 3
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


def test_gate_max_buyers_vetoes_crowded_accepts() -> None:
    third = str(Pubkey.from_bytes(bytes([4]) * 32))
    gate = _gate(min_buyers=0, max_buyers=2)
    assert gate.observe(
        _event(user=OTHER, real=200_000_000, slot=CREATION_SLOT + 1)
    ).accept
    assert gate.observe(
        _event(
            user=str(Pubkey.from_bytes(bytes([3]) * 32)),
            real=300_000_000,
            slot=CREATION_SLOT + 2,
        )
    ).accept
    decision = gate.observe(
        _event(user=third, real=400_000_000, slot=CREATION_SLOT + 3)
    )
    assert decision is not None and not decision.accept
    assert decision.reason == "too_many_buyers"


def test_gate_max_buyers_none_is_unbounded() -> None:
    gate = _gate(min_buyers=0)
    users = (
        OTHER,
        str(Pubkey.from_bytes(bytes([3]) * 32)),
        str(Pubkey.from_bytes(bytes([4]) * 32)),
    )
    decision = None
    for i, user in enumerate(users):
        decision = gate.observe(
            _event(user=user, real=200_000_000 + i, slot=CREATION_SLOT + 1 + i)
        )
    assert decision is not None and decision.accept


def test_gate_max_buyers_validation() -> None:
    with pytest.raises(ValueError):
        GateRules(max_buyers=-1)
    with pytest.raises(ValueError):
        GateRules(min_buyers=3, max_buyers=2)
    assert GateRules(min_buyers=2, max_buyers=3).max_buyers == 3
    assert GateRules().max_buyers is None


def test_gate_rules_exclude_mayhem_field() -> None:
    assert GateRules(exclude_mayhem=True).exclude_mayhem is True
    assert GateRules().exclude_mayhem is False


def test_gate_min_wait_slots_holds_through_creation_slot_and_validates() -> None:
    with pytest.raises(ValueError, match="min_wait_slots"):
        GateRules(min_wait_slots=-1)
    with pytest.raises(ValueError, match="min_wait_slots"):
        GateRules(min_wait_slots=4, max_wait_slots=3)

    b1 = OTHER
    b2 = str(Pubkey.from_bytes(bytes([3]) * 32))
    gate = _gate(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=2,
        max_buyers=3,
        min_real_sol=60.0,
        max_real_sol=80.0,
        min_wait_slots=1,
        max_wait_slots=10,
    )
    assert (
        gate.observe(
            _event(user=b1, is_buy=True, real=35_000_000_000, slot=CREATION_SLOT)
        )
        is None
    )
    # Crosses 60 SOL with 2 buyers in creation_slot, but min_wait_slots=1 holds:
    assert (
        gate.observe(
            _event(user=b2, is_buy=True, real=65_000_000_000, slot=CREATION_SLOT)
        )
        is None
    )
    # Post-creation slot trade in band is accepted:
    decision = gate.observe(
        _event(user=b2, is_buy=True, real=66_000_000_000, slot=CREATION_SLOT + 1)
    )
    assert decision is not None and decision.accept
    assert decision.slots_waited == 1
    with pytest.raises(ValueError, match="max_single_buy_sol"):
        GateRules(max_single_buy_sol=0)
    sweep_gate = _gate(
        mayhem_only=False,
        min_buyers=1,
        min_real_sol=60.0,
        max_real_sol=80.0,
        max_single_buy_sol=10.0,
    )
    sweep_decision = sweep_gate.observe(
        _event(
            user=b1,
            is_buy=True,
            sol=19_500_000_000,
            real=64_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )
    assert sweep_decision is not None and not sweep_decision.accept
    assert sweep_decision.reason == "single_buy_too_large"

    with pytest.raises(ValueError, match="max_buyer_share"):
        GateRules(max_buyer_share=0)
    with pytest.raises(ValueError, match="max_buyer_share"):
        GateRules(max_buyer_share=1.01)
    assert GateRules(max_buyer_share=0.75).max_buyer_share == 0.75
    share_gate = _gate(
        mayhem_only=False,
        min_buyers=2,
        max_buyers=3,
        min_real_sol=60.0,
        max_real_sol=80.0,
        max_buyer_share=0.75,
        min_wait_slots=1,
    )
    assert (
        share_gate.observe(
            _event(
                user=b1,
                is_buy=True,
                sol=9_000_000_000,
                real=55_000_000_000,
                slot=CREATION_SLOT,
            )
        )
        is None
    )
    share_decision = share_gate.observe(
        _event(
            user=b2,
            is_buy=True,
            sol=1_000_000_000,
            real=61_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )
    assert share_decision is not None and not share_decision.accept
    assert share_decision.reason == "buyer_concentration_too_high"
    with pytest.raises(ValueError, match="max_inflow_sol_per_slot"):
        GateRules(max_inflow_sol_per_slot=0)
    inflow_gate = _gate(
        mayhem_only=False,
        min_buyers=1,
        min_real_sol=60.0,
        max_real_sol=80.0,
        max_inflow_sol_per_slot=20.0,
        max_wait_slots=10,
    )
    assert (
        inflow_gate.observe(
            _event(user=b1, is_buy=True, real=65_000_000_000, slot=CREATION_SLOT + 1)
        )
        is None
    )
    inflow_decision = inflow_gate.observe(
        _event(user=b1, is_buy=True, real=66_000_000_000, slot=CREATION_SLOT + 4)
    )
    assert inflow_decision is not None and inflow_decision.accept
    assert inflow_decision.slots_waited == 4


@pytest.mark.asyncio
async def test_await_entry_gate_drains_same_slot_bundle_and_rejects_completion() -> (
    None
):
    b1 = OTHER
    b2 = str(Pubkey.from_bytes(bytes([3]) * 32))
    b3 = str(Pubkey.from_bytes(bytes([4]) * 32))
    b4 = str(Pubkey.from_bytes(bytes([5]) * 32))
    rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=2,
        max_buyers=3,
        min_real_sol=60.0,
        max_real_sol=80.0,
        min_wait_slots=0,
        max_wait_slots=10,
    )
    trader, token, queue = _gated_trader(rules)
    token.is_mayhem_mode = False
    # 4-tranche graduation bundle in the same slot (21 -> 42 -> 64 -> 85 SOL):
    for user, real_sol in (
        (b1, 21_000_000_000),
        (b2, 42_000_000_000),
        (b3, 64_000_000_000),
        (b4, 85_005_000_000),
    ):
        queue.put_nowait(
            _event(user=user, is_buy=True, real=real_sol, slot=CREATION_SLOT)
        )

    decision = await trader._await_entry_gate(token)

    assert decision is not None and not decision.accept
    assert decision.reason == "too_much_sol"
    assert queue.empty()


@pytest.mark.asyncio
async def test_handle_token_censors_unexecutable_completed_curve_and_skips_pre_gate_sleep(
    tmp_path: Path,
) -> None:
    rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=1,
        max_buyers=3,
        min_real_sol=60.0,
        max_real_sol=80.0,
    )
    trader, token, queue = _gated_trader(rules)
    token.is_mayhem_mode = False
    token.quote_mint = WSOL_MINT
    trader.platform = Platform.PUMP_FUN
    trader.execution_policy = ExecutionPolicy(mode=ExecutionMode.DRY_RUN)
    trader.allowed_quote_mints = {WSOL_MINT}
    trader.quote_amounts = {WSOL_MINT: 0.01}
    trader.extreme_fast_mode = False
    trader.wait_time_after_creation = 15
    trader.wait_time_before_new_token = 0
    trader.yolo_mode = True
    trader.jev_scorer = None
    trader._pending_recovery_tokens = []
    trader._write_recovery_journal = lambda: None
    trader._paper_tasks = set()
    trader._position_tasks = set()
    trader._fatal_monitor_errors = asyncio.Queue()
    trader._handle_failed_buy = AsyncMock(return_value=True)
    # Buyer discovers the curve is already zeroed/completed when refreshing:
    trader.buyer = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=False,
                platform=Platform.PUMP_FUN,
                price=None,
                error_message="virtual_token_reserves must be a positive raw u64 integer",
            )
        )
    )
    queue.put_nowait(
        _event(
            user=OTHER,
            is_buy=True,
            real=64_000_000_000,
            vsol=94_000_000_000,
            vtok=340_000_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )

    db_file = tmp_path / "lessons.sqlite3"
    with closing(LessonJournal(db_file)) as journal:
        trader.lesson_journal = journal
        # Must complete well under 1s because gate_rules skips the 15s pre-gate sleep:
        handled = await asyncio.wait_for(trader._handle_token(token), timeout=1.0)
        assert handled is True
        assert not getattr(trader, "_watch_tasks", set())
        report = load_report(db_path=db_file)
        assert report["paper_coverage"]["planned_entries"] == 1
        assert report["paper_coverage"]["censored_entries"] == 1
        assert {row["reason"] for row in report["paper_censors"]} == {
            "unexecutable_completed_curve"
        }


@pytest.mark.asyncio
async def test_graduation_watch_reuses_open_read_and_skips_post_open_settle_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 100.0}

    async def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr("trading.universal_trader.monotonic", lambda: clock["now"])
    monkeypatch.setattr("trading.universal_trader.asyncio.sleep", fake_sleep)

    trader, token, _ = _gated_trader(GateRules())
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(
            get_pool_state_and_token_program=AsyncMock(
                side_effect=ValueError("zeroed completed curve")
            )
        )
    )
    settle_flags: list[bool] = []
    prices = iter([(1.0e-6, "ok"), (1.1e-6, "ok"), (0.95e-6, "ok")])

    async def fake_graduated_pool_price(
        _tok: TokenInfo, remaining: float, *, settle: bool = True
    ) -> tuple[float, str] | None:
        settle_flags.append(settle)
        if settle:
            await fake_sleep(min(2.0, remaining / 4))
        return next(prices, (1.05e-6, "ok"))

    monkeypatch.setattr(
        trader,
        "_completed_curve_pool_price",
        lambda tok, rem: fake_graduated_pool_price(tok, rem, settle=True),
    )
    monkeypatch.setattr(trader, "_graduated_pool_price", fake_graduated_pool_price)

    db_file = tmp_path / "lessons.sqlite3"
    with closing(LessonJournal(db_file)) as journal:
        trader.lesson_journal = journal
        entry_id = journal.record(
            LessonObservation(
                kind="gate_pass",
                mint=MINT,
                symbol="GRAD",
                decision="buyers_present",
                buyers=3,
                mayhem=False,
                raw={"gate": {"slots_waited": 2}},
            )
        )
        assert entry_id is not None
        journal.start_paper_marks(entry_id, 0.8e-6)
        # Stop loop right after rollover triggers at G+4s by advancing deadline:
        await trader._graduation_watch(token, entry_id, started=100.0)
        # First call (open) had settle=True; subsequent calls had settle=False!
        assert settle_flags[0] is True
        assert all(flag is False for flag in settle_flags[1:])
        grad_row = journal._conn.execute(
            "SELECT grad_elapsed_s, open_price, peak_price, rollover_price, rollover_elapsed_s, stabilized_rollover_price, stabilized_rollover_elapsed_s FROM grad_marks WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        assert grad_row == (2.0, 1.0e-6, 1.1e-6, 0.95e-6, 4.0, None, None)
        report = load_report(db_path=db_file)
        assert report["grad_marks"]["early_whipsaw_count"] == 1
        assert report["paper_by_entry_timing"] == [
            {
                "band": "in_band_2_3_non_mayhem",
                "entry_timing": "post_creation_slot_ge_1",
                "planned_entries": 1,
                "paired_entries": 0,
                "graduated_n": 1,
                "win_60s_n": 0,
                "mean_return_60s": None,
            }
        ]
        assert report["executable_funnel"][0]["stage"] == "unexecutable_poll1_bundle"
        assert report["executable_funnel"][0]["planned_entries"] == 1

        entry2_id = journal.record(
            LessonObservation(
                kind="gate_pass",
                mint=MINT,
                symbol="GRAD2",
                decision="buyers_present",
                buyers=3,
                mayhem=False,
                raw={"gate": {"slots_waited": 2}},
            )
        )
        assert entry2_id is not None
        clock["now"] = 200.0
        prices = iter(
            [
                (1.0e-6, "ok"),
                (1.1e-6, "ok"),
                (0.95e-6, "ok"),
                (1.05e-6, "ok"),
                (1.05e-6, "ok"),
                (1.05e-6, "ok"),
                (1.00e-6, "ok"),
            ]
        )
        await trader._graduation_watch(token, entry2_id, started=200.0)
        grad2_row = journal._conn.execute(
            "SELECT rollover_price, rollover_elapsed_s, stabilized_rollover_price, stabilized_rollover_elapsed_s FROM grad_marks WHERE entry_id=?",
            (entry2_id,),
        ).fetchone()
        assert grad2_row == (0.95e-6, 4.0, 1.00e-6, 12.0)


@pytest.mark.asyncio
async def test_graduated_pool_price_reads_pool_and_vaults_in_one_slot_consistent_batch() -> (
    None
):
    trader, token, _ = _gated_trader(GateRules())
    token.token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
    pool = PumpSwapAddresses.derive_canonical_pool(token.mint, WSOL_MINT)
    authority = PumpSwapAddresses.derive_pool_authority(token.mint)
    _, bump = Pubkey.find_program_address(
        [
            b"pool",
            struct.pack("<H", 0),
            bytes(authority),
            bytes(token.mint),
            bytes(WSOL_MINT),
        ],
        PUMP_SWAP_PROGRAM,
    )
    base_vault = get_associated_token_address(
        pool, token.mint, SystemAddresses.TOKEN_2022_PROGRAM
    )
    quote_vault = get_associated_token_address(
        pool, WSOL_MINT, SystemAddresses.TOKEN_PROGRAM
    )
    pool_raw = bytearray(300)
    pool_raw[0:8] = PUMP_SWAP_POOL_DISCRIMINATOR
    pool_raw[8] = bump
    pool_raw[11:43] = bytes(authority)
    pool_raw[43:75] = bytes(token.mint)
    pool_raw[75:107] = bytes(WSOL_MINT)
    pool_raw[139:171] = bytes(base_vault)
    pool_raw[171:203] = bytes(quote_vault)
    pool_raw[211:243] = bytes(token.creator)
    pool_raw[245:261] = (10_000_000_000).to_bytes(16, "little", signed=True)

    def _vault_acc(amount_raw: int, prog: Pubkey) -> Account:
        buf = bytearray(165)
        struct.pack_into("<Q", buf, 64, amount_raw)
        return Account(lamports=1, data=bytes(buf), owner=prog, executable=False)

    pool_acc = Account(
        lamports=1, data=bytes(pool_raw), owner=PUMP_SWAP_PROGRAM, executable=False
    )
    base_acc = _vault_acc(200_000_000_000_000, SystemAddresses.TOKEN_2022_PROGRAM)
    quote_acc = _vault_acc(70_000_000_000, SystemAddresses.TOKEN_PROGRAM)

    calls: list[list[Pubkey]] = []

    async def fake_get_multiple(keys: list[Pubkey], **_kw: object) -> list[Account]:
        calls.append(list(keys))
        return [pool_acc, base_acc, quote_acc]

    trader.solana_client = SimpleNamespace(get_multiple_accounts=fake_get_multiple)
    res = await trader._graduated_pool_price(token, remaining=10.0, settle=False)
    assert res is not None
    price, reason = res
    assert reason == "pool_reserves_ok"
    # (70 SOL + 10 SOL virtual) / 200M tokens = 80 / 200_000_000 = 4e-7
    assert price == pytest.approx(4e-7)
    # Only ONE RPC call was made for [pool, base_vault, quote_vault]!
    assert calls == [[pool, base_vault, quote_vault]]
    assert token.base_vault == base_vault
    assert token.quote_vault == quote_vault


@pytest.mark.asyncio
async def test_queue_token_skips_trade_subscription_when_statically_excluded_by_gate() -> (
    None
):
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    trader, token, _ = _gated_trader(GateRules(mayhem_only=False, exclude_mayhem=True))
    trader.platform = Platform.PUMP_FUN
    trader.trade_hub = hub
    trader._gate_queues = {}
    trader.token_queue = asyncio.Queue()
    trader._queue_lock = asyncio.Lock()
    trader.processed_tokens = set()
    trader._reserved_mints = set()
    trader.token_timestamps = {}
    token.is_mayhem_mode = True

    queued = await trader._queue_token(token)

    assert queued is True
    assert str(token.mint) not in trader._gate_queues
    assert not hub.active


@pytest.mark.asyncio
async def test_process_token_queue_evaluates_gates_concurrently_without_head_of_line_blocking() -> (
    None
):
    rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=1,
        min_real_sol=60.0,
        max_real_sol=80.0,
        min_wait_slots=1,
        max_wait_slots=75,
        max_wait_ms=10_000,
    )
    trader, fast_token, fast_queue = _gated_trader(rules)
    fast_token.is_mayhem_mode = False
    slow_mint = str(Pubkey.from_bytes(bytes([42]) * 32))
    slow_token = TokenInfo(
        name="SlowToken",
        symbol="SLOW",
        uri="",
        mint=Pubkey.from_string(slow_mint),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=False,
        slot=CREATION_SLOT,
        state_from_event=True,
        virtual_quote_reserves=30_000_000_000,
        virtual_token_reserves=1_073_000_000_000_000,
    )
    trader._gate_queues[slow_mint] = TradeQueue()
    fast_queue.put_nowait(
        _event(
            user=OTHER,
            is_buy=True,
            sol=5_000_000_000,
            real=65_000_000_000,
            vsol=95_000_000_000,
            vtok=330_000_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )
    trader.platform = Platform.PUMP_FUN
    trader.trade_hub = None
    trader.token_queue = asyncio.Queue()
    trader.max_token_age = 5.0
    trader.token_timestamps = {}
    trader._inflight_tokens = {}
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._pending_recovery_tokens = []
    trader.processed_tokens = set()
    trader._reserved_mints = set()
    trader._write_recovery_journal = lambda: None
    trader.allowed_quote_mints = {WSOL_MINT}
    trader.quote_amounts = {WSOL_MINT: 0.01}
    trader.extreme_fast_mode = False
    trader.wait_time_after_creation = 0
    trader.wait_time_before_new_token = 0
    trader.yolo_mode = True
    trader.lesson_journal = None
    trader.jev_scorer = None
    trader._record_trade_evidence = lambda *_a, **_kw: None
    trader._buy_intent_id = lambda t: f"buy-{t.mint}"
    bought: list[str] = []
    fast_done = asyncio.Event()

    async def fake_execute(tok: TokenInfo) -> TradeResult:
        bought.append(str(tok.mint))
        fast_done.set()
        return TradeResult(
            success=False, platform=Platform.PUMP_FUN, error_message="dry"
        )

    async def fake_failed_buy(*_a: object, **_kw: object) -> bool:
        return True

    trader.buyer = SimpleNamespace(execute=fake_execute)
    trader._handle_failed_buy = fake_failed_buy

    # Queue slow_token first (no trades -> would block 10s sequentially), then fast_token
    trader.token_queue.put_nowait(slow_token)
    trader.token_queue.put_nowait(fast_token)

    worker = asyncio.create_task(trader._process_token_queue())
    try:
        # Must complete in well under 1s despite slow_token's 10s gate wait
        await asyncio.wait_for(fast_done.wait(), timeout=0.5)
        assert bought == [MINT]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_repeat_creator_gate_skips_subscription_and_rejects_second_launch() -> (
    None
):
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=1,
        min_real_sol=0.1,
        max_creator_launches=1,
    )
    trader, first_token, _ = _gated_trader(rules)
    trader.platform = Platform.PUMP_FUN
    trader.trade_hub = hub
    trader._gate_queues = {}
    trader.token_queue = asyncio.Queue()
    trader._queue_lock = asyncio.Lock()
    trader.processed_tokens = set()
    trader._reserved_mints = set()
    trader.token_timestamps = {}
    first_token.is_mayhem_mode = True  # mayhem_excluded on first launch

    assert await trader._queue_token(first_token) is True
    assert str(first_token.mint) not in trader._gate_queues
    assert not hub.active

    second_mint = str(Pubkey.from_bytes(bytes([77]) * 32))
    second_token = TokenInfo(
        name="SecondToken",
        symbol="SEC",
        uri="",
        mint=Pubkey.from_string(second_mint),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=False,
        slot=CREATION_SLOT + 5,
        state_from_event=True,
        virtual_quote_reserves=30_000_000_000,
        virtual_token_reserves=1_073_000_000_000_000,
    )
    assert await trader._queue_token(second_token) is True
    assert second_mint not in trader._gate_queues
    assert not hub.active

    decision = await trader._await_entry_gate(second_token)
    assert decision is not None and not decision.accept
    assert decision.reason == "repeat_creator"


@pytest.mark.asyncio
async def test_repeat_creator_gate_seeds_from_lesson_journal_and_handles_concurrent_queue(
    tmp_path: Path,
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=1,
        min_real_sol=0.1,
        max_creator_launches=1,
    )
    second_mint = str(Pubkey.from_bytes(bytes([77]) * 32))
    second_token = TokenInfo(
        name="SecondToken",
        symbol="SEC",
        uri="",
        mint=Pubkey.from_string(second_mint),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=False,
        slot=CREATION_SLOT + 5,
        state_from_event=True,
        virtual_quote_reserves=30_000_000_000,
        virtual_token_reserves=1_073_000_000_000_000,
    )
    with closing(LessonJournal(tmp_path / "lessons.sqlite3")) as journal:
        seeded_creator = str(Pubkey.from_bytes(bytes([88]) * 32))
        seeded_mint = str(Pubkey.from_bytes(bytes([89]) * 32))
        journal.record(
            LessonObservation(
                kind="gate_skip",
                platform="pump_fun",
                mint=seeded_mint,
                symbol="SEED",
                decision="mayhem_excluded",
                raw={"creator": seeded_creator},
            )
        )
        trader2, tok1, _ = _gated_trader(rules)
        trader2.platform = Platform.PUMP_FUN
        trader2.trade_hub = hub
        trader2._gate_queues = {}
        trader2.token_queue = asyncio.Queue()
        trader2._queue_lock = asyncio.Lock()
        trader2.processed_tokens = set()
        trader2._reserved_mints = set()
        trader2.token_timestamps = {}
        trader2.lesson_journal = journal
        tok1.is_mayhem_mode = False

        assert await trader2._queue_token(tok1) is True
        assert str(tok1.mint) in trader2._gate_queues
        assert hub.active

        assert await trader2._queue_token(second_token) is True
        assert second_mint not in trader2._gate_queues

        trader2._gate_queues[str(tok1.mint)].put_nowait(
            _event(
                user=OTHER,
                is_buy=True,
                real=200_000_000,
                slot=CREATION_SLOT + 1,
            )
        )
        dec1 = await trader2._await_entry_gate(tok1)
        assert dec1 is not None and dec1.accept

        dec2 = await trader2._await_entry_gate(second_token)
        assert dec2 is not None and not dec2.accept
        assert dec2.reason == "repeat_creator"

        seeded_second = TokenInfo(
            name="SeededSecond",
            symbol="S2",
            uri="",
            mint=Pubkey.from_bytes(bytes([90]) * 32),
            platform=Platform.PUMP_FUN,
            creator=Pubkey.from_string(seeded_creator),
            is_mayhem_mode=False,
            slot=CREATION_SLOT + 10,
            state_from_event=True,
            virtual_quote_reserves=30_000_000_000,
            virtual_token_reserves=1_073_000_000_000_000,
        )
        assert trader2._needs_trade_subscription(seeded_second) is False
        dec_seeded = await trader2._await_entry_gate(seeded_second)
        assert dec_seeded is not None and not dec_seeded.accept
        assert dec_seeded.reason == "repeat_creator"


@pytest.mark.asyncio
async def test_paper_flow_watch_records_first_flow_exit_and_releases_subscription(
    tmp_path: Path,
) -> None:
    trader, token, queue = _gated_trader(GateRules())
    trader.platform = Platform.PUMP_FUN
    trader.trade_hub = None
    trader.flow_rules = FlowRules(creator_sell=True, entry_floor_pct=0.10)
    trader.max_hold_time = 5
    trader._active_positions = {}
    trader._paper_flow_watches = set()
    trader._entry_slots = {MINT: CREATION_SLOT + 10}
    token.real_sol_reserves = 50_000_000_000
    token.slot = CREATION_SLOT
    fresh_queue = TradeQueue()
    trader._gate_queues[MINT] = fresh_queue
    # Pre-fill trade buffered during the gate wait must be skipped.
    fresh_queue.put_nowait(
        _event(
            user=OTHER,
            is_buy=False,
            real=40_000_000_000,
            slot=CREATION_SLOT + 9,
        )
    )
    # Post-fill trade dropping 20% below entry fires entry_floor.
    fresh_queue.put_nowait(
        _event(
            user=OTHER,
            is_buy=True,
            real=40_000_000_000,
            slot=CREATION_SLOT + 11,
        )
    )

    db_file = tmp_path / "lessons.sqlite3"
    with closing(LessonJournal(db_file)) as journal:
        trader.lesson_journal = journal
        entry_id = journal.record(
            LessonObservation(
                kind="gate_pass",
                mint=MINT,
                symbol="FLOW",
                decision="buyers_present",
                buyers=2,
                mayhem=False,
            )
        )
        assert entry_id is not None
        journal.start_paper_marks(entry_id, 1.0)
        started = monotonic()
        await asyncio.wait_for(
            trader._paper_flow_watch(token, entry_id, started, 1.0), timeout=2
        )

        row = journal._conn.execute(
            "SELECT rule, exit_price, slot FROM paper_flow_marks WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "entry_floor"
        assert row[1] > 0
        assert row[2] == CREATION_SLOT + 11

    # Zero-leak: the watch released the reservation bookkeeping and subscription.
    assert MINT not in trader._paper_flow_watches
    assert MINT not in trader._entry_slots
    assert MINT not in trader._gate_queues


def test_finish_token_reservation_preserves_subscription_during_paper_flow_watch() -> (
    None
):
    trader, token, _queue = _gated_trader(GateRules())
    trader._active_positions = {}
    trader._paper_flow_watches = {MINT}
    trader._unresolved_buys = {}
    trader._reserved_mints = {MINT}
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = []
    trader._write_recovery_journal = lambda: None
    trader.token_timestamps = {}

    trader._finish_token_reservation(token, handled=True)
    # Subscription must survive while the paper flow watch owns it.
    assert MINT in trader._gate_queues

    trader._paper_flow_watches.discard(MINT)
    trader._finish_token_reservation(token, handled=True)
    assert MINT not in trader._gate_queues
