"""Offline verification of executable crossing-gate and paper-mark lookahead fixes.

Proves:
1. Same-slot 4-tranche graduation bundles (21 -> 42 -> 64 -> 85 SOL in creation_slot)
   are drained before finalizing an accept decision and rejected as too_much_sol
   or curve_complete rather than accepted mid-bundle on Tranche 3.
2. min_wait_slots=1 holds through creation_slot trades and accepts only when the
   curve remains in-band in slot >= creation_slot + 1.
3. Active gate_rules skips the legacy 15s wait_time_after_creation sleep.
4. When PlatformAwareBuyer discovers an already-completed curve at execution time,
   UniversalTrader censors paper_marks with unexecutable_completed_curve and never
   starts a graduation watch. When the curve is executable, entry_price anchors
   to the refreshed curve's marginal reserves.

Run: uv run --offline --no-sync python -B learning-examples/token-lifecycles/verify_executable_crossing_gate.py
"""

# ruff: noqa: ARG005, E402, PLR0913, PLR0915, PLR2004, S101, SLF001
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from solders.pubkey import Pubkey

import trading.universal_trader as ut
from bot_runner import _gate_rules_from_config
from config_loader import validate_config
from core.client import TransactionStatus
from core.execution_policy import ExecutionMode, ExecutionPolicy
from core.pubkeys import WSOL_MINT
from interfaces.core import Platform, TokenInfo
from learning.journal import LessonJournal
from learning.report import load_report
from monitoring.trade_flow import (
    MAYHEM_SOL_VAULT,
    EntryGate,
    FlowMonitor,
    FlowRules,
    GateRules,
    TradeEvent,
    TradeQueue,
)
from trading.base import TradeResult
from trading.position import ExitReason, Position
from trading.universal_trader import UniversalTrader

CREATION_SLOT = 500_000_000
MINT = str(Pubkey.from_bytes(bytes([9]) * 32))
CREATOR = str(Pubkey.from_bytes(bytes([1]) * 32))
BUYERS = [str(Pubkey.from_bytes(bytes([i]) * 32)) for i in range(2, 7)]


def _make_event(
    *,
    user: str,
    real_sol: int,
    is_buy: bool = True,
    sol_amount: int = 21_000_000_000,
    vsol: int = 94_000_000_000,
    vtok: int = 340_000_000_000_000,
    real_tok: int = 60_000_000_000_000,
    slot: int = CREATION_SLOT,
) -> TradeEvent:
    return TradeEvent(
        mint=MINT,
        user=user,
        creator=CREATOR,
        is_buy=is_buy,
        sol_amount=sol_amount,
        token_amount=100_000_000_000,
        virtual_sol_reserves=vsol,
        virtual_token_reserves=vtok,
        real_sol_reserves=real_sol,
        real_token_reserves=real_tok,
        slot=slot,
        signature=f"sig-{slot}-{real_sol}",
        timestamp=1_793_500_000,
    )


async def _run_checks(tmp_dir: Path) -> dict[str, object]:
    rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=2,
        max_buyers=3,
        min_real_sol=60.0,
        max_real_sol=80.0,
        min_wait_slots=0,
        max_wait_slots=75,
        max_wait_ms=1000,
    )

    # 1. Same-slot 4-tranche bundle drain
    trader = object.__new__(UniversalTrader)
    trader.execution_policy = ExecutionPolicy(mode=ExecutionMode.DRY_RUN)
    trader.gate_rules = rules
    queue = TradeQueue()
    trader._gate_queues = {MINT: queue}
    token = TokenInfo(
        name="Bundle",
        symbol="BNDL",
        uri="",
        mint=Pubkey.from_string(MINT),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=False,
        slot=CREATION_SLOT,
        state_from_event=True,
        quote_mint=WSOL_MINT,
        virtual_quote_reserves=30_000_000_000,
        virtual_token_reserves=1_073_000_000_000_000,
    )
    for buyer, real_sol, real_tok in (
        (BUYERS[0], 21_000_000_000, 500_000_000_000_000),
        (BUYERS[1], 42_000_000_000, 300_000_000_000_000),
        (BUYERS[2], 64_000_000_000, 120_000_000_000_000),
        (BUYERS[3], 85_005_000_000, 0),
    ):
        queue.put_nowait(
            _make_event(
                user=buyer,
                real_sol=real_sol,
                real_tok=real_tok,
                slot=CREATION_SLOT,
            )
        )
    bundle_decision = await trader._await_entry_gate(token)
    assert bundle_decision is not None and not bundle_decision.accept
    assert bundle_decision.reason == "too_much_sol"
    assert queue.empty()

    # 2. min_wait_slots=1 holds through slot 0 and accepts in slot + 1
    post_slot_rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=2,
        max_buyers=3,
        min_real_sol=60.0,
        max_real_sol=80.0,
        min_wait_slots=1,
        max_wait_slots=75,
        max_wait_ms=30000,
    )
    gate = EntryGate(
        mint=MINT,
        creator=CREATOR,
        creation_slot=CREATION_SLOT,
        rules=post_slot_rules,
    )
    assert (
        gate.observe(
            _make_event(user=BUYERS[0], real_sol=35_000_000_000, slot=CREATION_SLOT)
        )
        is None
    )
    assert (
        gate.observe(
            _make_event(user=BUYERS[1], real_sol=64_000_000_000, slot=CREATION_SLOT)
        )
        is None
    )
    accepted = gate.observe(
        _make_event(user=BUYERS[1], real_sol=66_000_000_000, slot=CREATION_SLOT + 1)
    )
    assert accepted is not None and accepted.accept and accepted.slots_waited == 1

    # 3. Config loader + _gate_rules_from_config round-trip
    cfg = {
        "name": "verify-paper",
        "rpc_endpoint": "https://rpc.example.test",
        "wss_endpoint": "wss://rpc.example.test",
        "private_key": "test-key",
        "enabled": False,
        "platform": "pump_fun",
        "execution": {"mode": "dry_run"},
        "trade": {
            "buy_amount": 0.01,
            "buy_slippage": 0.3,
            "sell_slippage": 0.3,
            "exit_strategy": "rollover",
            "max_hold_time": 60,
        },
        "filters": {"listener_type": "pumpportal", "max_token_age": 0.001},
        "retries": {
            "max_attempts": 1,
            "wait_after_creation": 0,
            "wait_before_new_token": 0,
        },
        "entry_gate": {
            "enabled": True,
            "mayhem_only": False,
            "exclude_mayhem": True,
            "min_buyers": 2,
            "max_buyers": 3,
            "min_real_sol": 60.0,
            "max_real_sol": 80.0,
            "min_wait_slots": 1,
            "max_wait_slots": 75,
            "max_wait_ms": 30000,
        },
    }
    validate_config(cfg)
    parsed_rules = _gate_rules_from_config(cfg)
    assert parsed_rules == post_slot_rules

    # 4. Unexecutable curve at buyer.execute is censored, while executable curve
    #    anchors paper_marks to the refreshed marginal reserve price.
    db_path = tmp_dir / "lessons.sqlite3"
    with closing(LessonJournal(db_path)) as journal:
        trader.lesson_journal = journal
        trader.platform = Platform.PUMP_FUN
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

        # Case 4a: Unexecutable completed curve
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
            _make_event(user=BUYERS[0], real_sol=35_000_000_000, slot=CREATION_SLOT)
        )
        queue.put_nowait(
            _make_event(user=BUYERS[1], real_sol=65_000_000_000, slot=CREATION_SLOT + 1)
        )
        assert await asyncio.wait_for(trader._handle_token(token), timeout=1.0)
        assert not getattr(trader, "_watch_tasks", set())

        # Case 4b: Executable curve refreshed by buyer.execute before dry-run gate
        async def _refresh_and_block(tok: TokenInfo) -> TradeResult:
            tok.virtual_quote_reserves = 100_000_000_000
            tok.virtual_token_reserves = 250_000_000_000_000
            return TradeResult(
                success=False,
                platform=Platform.PUMP_FUN,
                price=4.05e-7,
                error_message="Transaction submission blocked: explicit runtime authorization is required for live execution",
            )

        trader.buyer = SimpleNamespace(
            execute=AsyncMock(side_effect=_refresh_and_block)
        )
        queue = TradeQueue()
        trader._gate_queues[MINT] = queue
        queue.put_nowait(
            _make_event(user=BUYERS[0], real_sol=35_000_000_000, slot=CREATION_SLOT)
        )
        queue.put_nowait(
            _make_event(
                user=BUYERS[1],
                real_sol=65_000_000_000,
                vsol=95_000_000_000,
                vtok=330_000_000_000_000,
                slot=CREATION_SLOT + 1,
            )
        )
        assert await asyncio.wait_for(trader._handle_token(token), timeout=1.0)
        for task in tuple(trader._paper_tasks) + tuple(
            getattr(trader, "_watch_tasks", set())
        ):
            task.cancel()
        await asyncio.gather(
            *trader._paper_tasks,
            *getattr(trader, "_watch_tasks", set()),
            return_exceptions=True,
        )

        report = load_report(db_path=db_path)
        entry_prices = [
            r[0]
            for r in journal._conn.execute(
                "SELECT DISTINCT entry_price FROM paper_marks ORDER BY entry_id"
            )
        ]
        # First entry is censored (None); second entry uses refreshed 100 / 250M = 4e-7
        assert entry_prices == [None, 4e-7]
        censor_reasons = {r["reason"] for r in report["paper_censors"]}
        assert "unexecutable_completed_curve" in censor_reasons

    # 5. max_single_buy_sol (single_buy_too_large) and max_buyer_share (buyer_concentration_too_high)
    sweep_rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=2,
        max_buyers=4,
        min_real_sol=60.0,
        max_real_sol=80.0,
        max_single_buy_sol=25.0,
        max_buyer_share=0.65,
        min_wait_slots=1,
        max_wait_slots=75,
        max_wait_ms=30000,
    )
    gate_single = EntryGate(
        mint=MINT,
        creator=CREATOR,
        creation_slot=CREATION_SLOT,
        rules=sweep_rules,
    )
    dec_single = gate_single.observe(
        _make_event(
            user=BUYERS[0],
            real_sol=30_000_000_000,
            sol_amount=30_000_000_000,
            slot=CREATION_SLOT,
        )
    )
    assert dec_single is not None and not dec_single.accept
    assert dec_single.reason == "single_buy_too_large"

    gate_conc = EntryGate(
        mint=MINT,
        creator=CREATOR,
        creation_slot=CREATION_SLOT,
        rules=sweep_rules,
    )
    for r_sol in (20_000_000_000, 40_000_000_000, 60_000_000_000):
        assert (
            gate_conc.observe(
                _make_event(
                    user=BUYERS[0],
                    real_sol=r_sol,
                    sol_amount=20_000_000_000,
                    slot=CREATION_SLOT,
                )
            )
            is None
        )
    dec_conc = gate_conc.observe(
        _make_event(
            user=BUYERS[1],
            real_sol=65_000_000_000,
            sol_amount=5_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )
    assert dec_conc is not None and not dec_conc.accept
    assert dec_conc.reason == "buyer_concentration_too_high"

    gate_organic = EntryGate(
        mint=MINT,
        creator=CREATOR,
        creation_slot=CREATION_SLOT,
        rules=sweep_rules,
    )
    for idx, r_sol in enumerate((20_000_000_000, 40_000_000_000, 62_000_000_000)):
        assert (
            gate_organic.observe(
                _make_event(
                    user=BUYERS[idx % 2],
                    real_sol=r_sol,
                    sol_amount=20_000_000_000,
                    slot=CREATION_SLOT,
                )
            )
            is None
        )
    assert (
        gate_organic.observe(
            _make_event(
                user=BUYERS[0],
                is_buy=False,
                real_sol=61_000_000_000,
                sol_amount=1_000_000_000,
                slot=CREATION_SLOT + 1,
            )
        )
        is None
    )
    assert (
        gate_organic.observe(
            _make_event(
                user=CREATOR,
                is_buy=True,
                real_sol=63_000_000_000,
                sol_amount=2_000_000_000,
                slot=CREATION_SLOT + 2,
            )
        )
        is None
    )
    assert (
        gate_organic.observe(
            _make_event(
                user=MAYHEM_SOL_VAULT,
                is_buy=True,
                real_sol=64_000_000_000,
                sol_amount=1_000_000_000,
                slot=CREATION_SLOT + 3,
            )
        )
        is None
    )
    dec_organic = gate_organic.observe(
        _make_event(
            user=BUYERS[1],
            is_buy=True,
            real_sol=65_000_000_000,
            sol_amount=5_000_000_000,
            slot=CREATION_SLOT + 4,
        )
    )
    assert dec_organic is not None and dec_organic.accept
    assert dec_organic.reason == "buyers_present"

    # 6. max_creator_launches (repeat_creator + _needs_trade_subscription skipping TradeFlowHub)
    rep_trader = object.__new__(UniversalTrader)
    rep_trader.platform = Platform.PUMP_FUN
    rep_trader.allowed_quote_mints = {WSOL_MINT}
    rep_trader.gate_rules = GateRules(
        mayhem_only=False,
        exclude_mayhem=True,
        min_buyers=2,
        min_real_sol=60.0,
        max_real_sol=80.0,
        max_creator_launches=1,
        min_wait_slots=1,
        max_wait_slots=75,
        max_wait_ms=1,
    )
    rep_trader._gate_queues = {}
    rep_trader._queue_lock = asyncio.Lock()
    rep_trader.processed_tokens = set()
    rep_trader._reserved_mints = set()
    rep_trader.token_timestamps = {}
    rep_trader.token_queue = asyncio.Queue(maxsize=16)
    rep_trader.max_token_age = 10.0
    subscribed_mints: list[str] = []
    rep_trader.trade_hub = SimpleNamespace(
        subscribe=lambda m: (subscribed_mints.append(m), TradeQueue())[1],
        unsubscribe=lambda m, q: None,
    )
    mint_1 = Pubkey.from_bytes(bytes([31]) * 32)
    mint_2 = Pubkey.from_bytes(bytes([32]) * 32)
    tok_1 = TokenInfo(
        name="First",
        symbol="FST",
        uri="",
        mint=mint_1,
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=False,
        slot=CREATION_SLOT,
        state_from_event=True,
        quote_mint=WSOL_MINT,
    )
    tok_2 = TokenInfo(
        name="Second",
        symbol="SND",
        uri="",
        mint=mint_2,
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        is_mayhem_mode=False,
        slot=CREATION_SLOT + 1,
        state_from_event=True,
        quote_mint=WSOL_MINT,
    )
    assert await rep_trader._queue_token(tok_1)
    assert subscribed_mints == [str(mint_1)]
    dec_1 = await rep_trader._await_entry_gate(tok_1)
    assert dec_1 is not None and dec_1.reason == "timeout"
    assert rep_trader._needs_trade_subscription(tok_2) is False
    assert await rep_trader._queue_token(tok_2)
    assert subscribed_mints == [str(mint_1)]
    rep_decision = await rep_trader._await_entry_gate(tok_2)
    assert rep_decision is not None and not rep_decision.accept
    assert rep_decision.reason == "repeat_creator"

    # 7. Concurrent _process_token_queue gate evaluation (fast token bypasses slow 10s gate wait)
    conc_trader = object.__new__(UniversalTrader)
    conc_trader.gate_rules = rules
    conc_trader.max_token_age = 10.0
    conc_trader.token_queue = asyncio.Queue()
    conc_trader.token_timestamps = {}
    conc_trader._inflight_tokens = {}
    conc_trader._active_positions = {}
    conc_trader._unresolved_buys = {}
    conc_trader._pending_recovery_tokens = []
    conc_trader.processed_tokens = set()
    conc_trader._reserved_mints = set()
    conc_trader._gate_queues = {}
    conc_trader._write_recovery_journal = lambda: None
    slow_mint = Pubkey.from_bytes(bytes([41]) * 32)
    fast_mint = Pubkey.from_bytes(bytes([42]) * 32)
    fast_done = asyncio.Event()

    async def _mock_handle(tok: TokenInfo) -> bool:
        if tok.mint == slow_mint:
            await asyncio.sleep(10.0)
            return True
        fast_done.set()
        return True

    conc_trader._handle_token = _mock_handle
    conc_trader.token_queue.put_nowait(
        TokenInfo(
            name="Slow",
            symbol="SLW",
            uri="",
            mint=slow_mint,
            platform=Platform.PUMP_FUN,
        )
    )
    conc_trader.token_queue.put_nowait(
        TokenInfo(
            name="Fast",
            symbol="FST",
            uri="",
            mint=fast_mint,
            platform=Platform.PUMP_FUN,
        )
    )
    queue_worker = asyncio.create_task(conc_trader._process_token_queue())
    try:
        await asyncio.wait_for(fast_done.wait(), timeout=0.5)
    finally:
        queue_worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queue_worker

    # 8. FlowRules(max_stall_slots=..., entry_floor_pct=...) (velocity_stall and entry_floor)
    flow_rules = FlowRules(
        creator_sell=False,
        max_stall_slots=8,
        entry_floor_pct=0.05,
    )
    mon_floor = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=flow_rules,
        entry_price=1e-6,
        entry_sol=65_000_000_000,
        entry_slot=CREATION_SLOT,
    )
    sig_floor = mon_floor.observe(
        _make_event(
            user=BUYERS[0],
            real_sol=61_000_000_000,
            slot=CREATION_SLOT + 1,
        )
    )
    assert sig_floor is not None and sig_floor.rule == "entry_floor"

    mon_stall = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=flow_rules,
        entry_price=1e-6,
        entry_sol=65_000_000_000,
        entry_slot=CREATION_SLOT,
    )
    assert (
        mon_stall.observe(
            _make_event(
                user=BUYERS[0],
                real_sol=67_000_000_000,
                slot=CREATION_SLOT + 2,
            )
        )
        is None
    )
    sig_stall = mon_stall.observe(
        _make_event(
            user=BUYERS[1],
            real_sol=66_500_000_000,
            slot=CREATION_SLOT + 10,
        )
    )
    assert sig_stall is not None and sig_stall.rule == "velocity_stall"

    # 9. Two-tier shakeout-tolerant rollover exit in _monitor_position_until_exit
    roll_trader = object.__new__(UniversalTrader)
    roll_tok = TokenInfo(
        name="Rollover",
        symbol="ROLL",
        uri="",
        mint=Pubkey.from_bytes(bytes([51]) * 32),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
        bonding_curve=Pubkey.from_bytes(bytes([52]) * 32),
    )
    roll_tok.pool_status = "funding"
    roll_pos = Position.create_from_buy_result(
        mint=roll_tok.mint,
        symbol=roll_tok.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        position_id="buy-rollover",
    )
    roll_trader._shutdown_event = asyncio.Event()
    roll_trader.exit_strategy = "rollover"
    roll_trader.price_check_interval = 0.001
    roll_trader.max_exit_sell_attempts = 1
    roll_trader.price_read_outage_budget = 300.0
    roll_trader.flow_rules = None
    roll_trader._flow_signals = {}
    roll_trader._flow_wakeups = {}
    roll_trader._gate_queues = {}
    roll_prices = iter([1.80, 1.656, 2.00, 1.88])

    async def _read_pool_price(*_args: object, **_kwargs: object) -> float:
        roll_tok.pool_status = "pumpswap"
        return next(roll_prices)

    calc_price = AsyncMock(side_effect=_read_pool_price)
    roll_trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=calc_price)
    )
    roll_trader.seller = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=True,
                platform=Platform.PUMP_FUN,
                tx_signature="sell-rollover",
                amount=2.0,
                amount_raw=2_000_000,
                price=1.88,
                status=TransactionStatus.SUCCESS.value,
            )
        )
    )
    roll_trader.solana_client = SimpleNamespace()
    roll_trader.wallet = SimpleNamespace()
    roll_trader.priority_fee_manager = SimpleNamespace()
    roll_trader.cleanup_mode = None
    roll_trader.cleanup_with_priority_fee = False
    roll_trader.cleanup_force_close_with_burn = False
    roll_trader._get_pool_address = lambda t: t.bonding_curve
    roll_trader._persist_position = lambda t, p: None
    roll_trader._log_trade = lambda *_a, **_kw: None
    roll_trader._remove_position = lambda _m: None
    roll_trader._record_trade_evidence = lambda *_a, **_kw: None
    orig_cleanup = ut.handle_cleanup_after_sell
    ut.handle_cleanup_after_sell = AsyncMock(return_value=None)
    try:
        await asyncio.wait_for(
            roll_trader._monitor_position_until_exit(roll_tok, roll_pos),
            timeout=2.0,
        )
    finally:
        ut.handle_cleanup_after_sell = orig_cleanup
    assert calc_price.await_count == 4
    assert roll_pos.is_active is False
    assert roll_pos.exit_reason is ExitReason.GRADUATED
    assert roll_pos.exit_price == 1.88

    return {
        "same_slot_bundle_rejected": True,
        "min_wait_slots_verified": True,
        "pre_gate_sleep_skipped": True,
        "unexecutable_completed_curve_censored": True,
        "executable_quote_anchored": True,
        "anti_sweep_filters_verified": True,
        "repeat_creator_cap_verified": True,
        "concurrent_gate_queue_verified": True,
        "flow_stall_and_floor_verified": True,
        "rollover_shakeout_grace_verified": True,
    }


def main() -> None:
    with TemporaryDirectory(prefix="verify-executable-gate-") as tmp:
        result = asyncio.run(_run_checks(Path(tmp)))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
