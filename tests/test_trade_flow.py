from __future__ import annotations

# ruff: noqa: S101, SLF001 - regression assertions and direct lifecycle state
import asyncio
import base64
import struct
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from solders.pubkey import Pubkey

from core.cycles.core import AMM, SOL
from core.cycles.discovery import CycleDiscovery
from core.cycles.pool import Pool
from core.pubkeys import DEFAULT_PUBKEY, USDC_MINT, WSOL_MINT
from geyser.generated import geyser_pb2
from interfaces.core import Platform, TokenInfo
from monitoring.trade_flow import (
    EntryGate,
    FlowMonitor,
    FlowRules,
    GateRules,
    GeyserTradeStream,
    TradeEvent,
    TradeFlowHub,
    TradeFlowLossError,
    decode_trade_events,
)
from monitoring.universal_geyser_listener import UniversalGeyserListener
from trading.universal_trader import UniversalTrader
from utils.idl_parser import IDLParser

CREATOR = "BtKJ2RYx274LJoXcyWrJ71bG7LeF53w6vTACuCNbhr9"
MINT = "67xAZNxvBtfR6YXFyvVSkRiziKVmXUWvCBvhT9Aupump"
OTHER = "dw3EJbG7Wk3RvbcASWQGKcmbaXvBn2v1uDhUfPQyY5Y"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _event(
    *,
    user: str = OTHER,
    is_buy: bool = True,
    sol: int = 1_000_000_000,
    real: int = 5_000_000_000,
    vsol: int = 35_000_000_000,
    vtok: int = 900_000_000_000_000,
    slot: int = 1,
) -> TradeEvent:
    return TradeEvent(
        mint=MINT,
        user=user,
        creator=CREATOR,
        is_buy=is_buy,
        sol_amount=sol,
        token_amount=1,
        virtual_sol_reserves=vsol,
        virtual_token_reserves=vtok,
        real_sol_reserves=real,
        real_token_reserves=vtok,
        slot=slot,
        signature="sig",
        timestamp=0,
    )


def test_creator_sell_fires_on_any_size() -> None:
    monitor = FlowMonitor(mint=MINT, creator=CREATOR, rules=FlowRules())
    assert monitor.observe(_event(user=CREATOR, is_buy=True)) is None
    assert monitor.observe(_event(user=OTHER, is_buy=False)) is None
    signal = monitor.observe(_event(user=CREATOR, is_buy=False, sol=1))
    assert signal is not None
    assert signal.rule == "creator_sell"


def test_trailing_stop_measures_from_peak_since_entry() -> None:
    rules = FlowRules(creator_sell=False, trailing_stop=0.15)
    monitor = FlowMonitor(mint=MINT, creator=CREATOR, rules=rules)
    assert monitor.observe(_event(vsol=35_000_000_000)) is None
    assert monitor.observe(_event(vsol=42_000_000_000)) is None  # new peak
    assert monitor.observe(_event(vsol=38_000_000_000)) is None  # -9.5%
    signal = monitor.observe(_event(vsol=35_000_000_000, is_buy=False))  # -16.7%
    assert signal is not None
    assert signal.rule == "trailing_stop"
    assert signal.price == pytest.approx(35 / 900_000_000)


def test_trailing_stop_peak_is_seeded_with_entry_price() -> None:
    """A pump missed between the buy and the first streamed event still counts."""
    rules = FlowRules(creator_sell=False, trailing_stop=0.15)
    entry = 42 / 900_000_000
    monitor = FlowMonitor(mint=MINT, creator=CREATOR, rules=rules, entry_price=entry)
    signal = monitor.observe(
        _event(vsol=35_000_000_000, is_buy=False)
    )  # -16.7% vs entry
    assert signal is not None and signal.rule == "trailing_stop"


def test_zero_reserves_are_rejected_at_decode() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(parser, virtual_token_reserves=0),
        f"Program {PUMP_PROGRAM} success",
    ]
    assert decode_trade_events(logs, slot=1, signature="s", idl_parser=parser) == []


def test_single_sell_fraction_uses_pre_sell_reserves() -> None:
    rules = FlowRules(creator_sell=False, single_sell_pct=0.2)
    monitor = FlowMonitor(mint=MINT, creator=CREATOR, rules=rules)
    # 1 SOL out of 5 (pre-sell 6) = 16.7%: no
    assert (
        monitor.observe(_event(is_buy=False, sol=1_000_000_000, real=5_000_000_000))
        is None
    )
    # 2 SOL out of pre-sell 7 = 28.6%: yes
    signal = monitor.observe(
        _event(is_buy=False, sol=2_000_000_000, real=5_000_000_000)
    )
    assert signal is not None
    assert signal.rule == "single_sell"


def test_net_outflow_over_window() -> None:
    rules = FlowRules(creator_sell=False, net_outflow_pct=0.3, window=3)
    monitor = FlowMonitor(mint=MINT, creator=CREATOR, rules=rules)
    assert (
        monitor.observe(_event(is_buy=True, sol=1_000_000_000, real=10_000_000_000))
        is None
    )
    assert (
        monitor.observe(_event(is_buy=False, sol=2_000_000_000, real=8_000_000_000))
        is None
    )
    # window: +1, -2, -3 => net out 4 SOL >= 30% of 5 SOL
    signal = monitor.observe(
        _event(is_buy=False, sol=3_000_000_000, real=5_000_000_000)
    )
    assert signal is not None
    assert signal.rule == "net_outflow"


def test_other_mints_are_ignored() -> None:
    monitor = FlowMonitor(mint="other", creator=CREATOR, rules=FlowRules())
    assert monitor.observe(_event(user=CREATOR, is_buy=False)) is None


@pytest.mark.parametrize(
    "bad", [{"trailing_stop": 0}, {"trailing_stop": 1.5}, {"window": 0}]
)
def test_rules_reject_invalid_fractions(bad: dict) -> None:
    with pytest.raises(ValueError):
        FlowRules(**bad)


def _encode_trade_event(parser: IDLParser, **overrides: object) -> str:
    """Encode the legacy prefix, plus the current suffix when a quote is supplied."""
    disc = parser.get_event_discriminators()["TradeEvent"]
    fields = {
        "mint": MINT,
        "sol_amount": 1_500_000_000,
        "token_amount": 30_000_000_000_000,
        "is_buy": False,
        "user": CREATOR,
        "timestamp": 1_788_463_175,
        "virtual_sol_reserves": 34_000_000_000,
        "virtual_token_reserves": 950_000_000_000_000,
        "real_sol_reserves": 4_000_000_000,
        "fee_recipient": OTHER,
        "fee_basis_points": 100,
        "fee": 15_000_000,
        "creator": CREATOR,
        "creator_fee_basis_points": 25,
        "creator_fee": 3_750_000,
        "quote_amount": 3_000_000_000,
        "virtual_quote_reserves": 48_000_000_000,
        "real_quote_reserves": 18_000_000_000,
    }
    fields.update(overrides)
    import base58

    body = b"".join(
        [
            base58.b58decode(fields["mint"]),
            struct.pack("<Q", fields["sol_amount"]),
            struct.pack("<Q", fields["token_amount"]),
            struct.pack("<?", fields["is_buy"]),
            base58.b58decode(fields["user"]),
            struct.pack("<q", fields["timestamp"]),
            struct.pack("<Q", fields["virtual_sol_reserves"]),
            struct.pack("<Q", fields["virtual_token_reserves"]),
            struct.pack("<Q", fields["real_sol_reserves"]),
            struct.pack("<Q", 0),  # real_token_reserves
            base58.b58decode(fields["fee_recipient"]),
            struct.pack("<Q", fields["fee_basis_points"]),
            struct.pack("<Q", fields["fee"]),
            base58.b58decode(fields["creator"]),
            struct.pack("<Q", fields["creator_fee_basis_points"]),
            struct.pack("<Q", fields["creator_fee"]),
        ]
    )
    if "quote_mint" in fields:
        name = str(
            fields.get("ix_name", "buy_v2" if fields["is_buy"] else "sell_v2")
        ).encode()
        body += (
            struct.pack("<?QQQqI", 0, 0, 0, 0, 0, len(name))
            + name
            + struct.pack("<?QQQQI", 0, 0, 0, 0, 0, 0)
            + bytes(Pubkey.from_string(str(fields["quote_mint"])))
            + struct.pack(
                "<QQQ",
                fields["quote_amount"],
                fields["virtual_quote_reserves"],
                fields["real_quote_reserves"],
            )
        )
    return "Program data: " + base64.b64encode(disc + body).decode()


def test_decode_trade_events_from_program_data_logs() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    logs = [
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
        _encode_trade_event(parser),
        "Program data: bm90LWFuLWV2ZW50",
        _encode_trade_event(parser, mint=OTHER, user=OTHER),
        f"Program {PUMP_PROGRAM} success",
    ]
    events = decode_trade_events(
        logs, slot=444_044_178, signature="s", idl_parser=parser
    )
    assert [e.mint for e in events] == [MINT, OTHER]
    ev = events[0]
    assert ev.user == CREATOR and ev.creator == CREATOR and ev.is_buy is False
    assert ev.sol_amount == 1_500_000_000
    assert ev.real_sol_reserves == 4_000_000_000
    assert ev.price == pytest.approx((34.0) / (950_000_000))
    only = decode_trade_events(
        logs, slot=1, signature="s", idl_parser=parser, mint=MINT
    )
    assert [e.mint for e in only] == [MINT]


@pytest.mark.parametrize(
    ("quote_mint", "legacy_amount"),
    [(str(DEFAULT_PUBKEY), 0), (str(WSOL_MINT), 1_000_000_000)],
    ids=["native-sol-zero-legacy", "wsol-conflicting-legacy"],
)
def test_canonical_sol_quantities_drive_gate_and_exit(
    quote_mint: str, legacy_amount: int
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    hub.publish_logs(
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            _encode_trade_event(
                parser,
                quote_mint=quote_mint,
                is_buy=True,
                user=OTHER,
                sol_amount=legacy_amount,
                virtual_sol_reserves=34 * legacy_amount,
                real_sol_reserves=4 * legacy_amount,
            ),
            f"Program {PUMP_PROGRAM} success",
        ],
        slot=1,
        signature="canonical-sol",
    )
    event = queue.get_nowait()
    assert (event.sol_amount, event.real_sol_reserves) == (
        3_000_000_000,
        18_000_000_000,
    )
    assert event.price == pytest.approx(48 / 950_000_000)
    gate = EntryGate(
        mint=MINT,
        creator=CREATOR,
        creation_slot=1,
        rules=GateRules(min_real_sol=10, max_real_sol=20),
    )
    decision = gate.observe(event)
    assert decision is not None and decision.accept
    monitor = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=FlowRules(creator_sell=False, trailing_stop=0.2),
        entry_price=50 / 950_000_000,
    )
    assert monitor.observe(event) is None  # 4% down, not the legacy fields' 32%.


@pytest.mark.parametrize(
    ("overrides", "trim"),
    [
        ({"quote_mint": str(USDC_MINT)}, 0),
        ({"virtual_quote_reserves": 0}, 0),
        ({}, 8),
        ({}, 56),
    ],
    ids=["non-sol", "zero-virtual", "partial-quantities", "v2-without-quote"],
)
def test_unsupported_or_incomplete_quotes_cannot_reuse_legacy_sol(
    overrides: dict[str, object], trim: int
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    fields = {"quote_mint": str(WSOL_MINT), **overrides}
    line = _encode_trade_event(parser, **fields)
    if trim:
        payload = base64.b64decode(line.removeprefix("Program data: "))
        line = "Program data: " + base64.b64encode(payload[:-trim]).decode()
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    hub.publish_logs(
        [f"Program {PUMP_PROGRAM} invoke [1]", line, f"Program {PUMP_PROGRAM} success"],
        slot=1,
        signature="invalid-quote",
    )
    assert queue.empty()


def test_zero_canonical_flow_and_real_reserves_are_not_legacy_values() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl" / "pump_fun_idl.json")
    )
    events = decode_trade_events(
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            _encode_trade_event(
                parser,
                quote_mint=str(WSOL_MINT),
                quote_amount=0,
                real_quote_reserves=0,
                user=OTHER,
            ),
            f"Program {PUMP_PROGRAM} success",
        ],
        slot=1,
        signature="zero-canonical",
        idl_parser=parser,
    )
    (event,) = events
    assert (event.sol_amount, event.real_sol_reserves) == (0, 0)
    monitor = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=FlowRules(creator_sell=False, single_sell_pct=0.2),
    )
    assert monitor.observe(event) is None


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ([f"Program {OTHER} invoke [1]"], [f"Program {OTHER} success"]),
        ([], []),
        ([f"Program {PUMP_PROGRAM} invoke [1]"], []),
        ([f"Program {PUMP_PROGRAM} invoke [2]"], [f"Program {PUMP_PROGRAM} success"]),
        ([f"Program {PUMP_PROGRAM} invoke [1]"], [f"Program {OTHER} success"]),
        (
            [f"Program {OTHER} invoke [1]", f"Program {PUMP_PROGRAM} invoke [2]"],
            [f"Program {PUMP_PROGRAM} success", f"Program {OTHER} failed: error"],
        ),
        (
            [f"Program {PUMP_PROGRAM} invoke [1]"],
            [
                f"Program {PUMP_PROGRAM} success",
                f"Program {OTHER} invoke [1]",
                f"Program {OTHER} failed: error",
            ],
        ),
        (
            [
                f"Program {OTHER} invoke [1]",
                f"Program log: Program {PUMP_PROGRAM} invoke [2]",
            ],
            [
                f"Program log: Program {PUMP_PROGRAM} success",
                f"Program {OTHER} success",
            ],
        ),
    ],
    ids=[
        "foreign",
        "unbound",
        "truncated",
        "wrong-depth",
        "wrong-completion",
        "parent-rollback",
        "later-root-failure",
        "forged-runtime-text",
    ],
)
def test_untrusted_trade_events_never_reach_gate_queue(
    before: list[str],
    after: list[str],
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    logs = [*before, _encode_trade_event(parser), *after]
    assert hub.publish_logs(logs, slot=1, signature="untrusted") == 0
    assert queue.empty()


def test_caught_parent_failure_discards_descendants_but_keeps_successful_sibling() -> (
    None
):
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    logs = [
        f"Program {OTHER} invoke [1]",
        f"Program {CREATOR} invoke [2]",
        f"Program {PUMP_PROGRAM} invoke [3]",
        _encode_trade_event(parser, user=CREATOR),
        f"Program {PUMP_PROGRAM} success",
        f"Program {CREATOR} failed: caught by router",
        f"Program {PUMP_PROGRAM} invoke [2]",
        _encode_trade_event(parser, user=OTHER),
        f"Program {PUMP_PROGRAM} success",
        f"Program {OTHER} success",
    ]
    assert hub.publish_logs(logs, slot=1, signature="caught") == 1
    assert queue.get_nowait().user == OTHER
    assert queue.empty()


def test_failed_geyser_metadata_cannot_fan_out_successful_looking_logs() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    update = geyser_pb2.SubscribeUpdate()
    update.transaction.slot = 1
    info = update.transaction.transaction
    info.signature = bytes([1]) * 64
    info.transaction.message.account_keys.extend(
        [bytes(Pubkey.from_string(MINT)), bytes(Pubkey.from_string(PUMP_PROGRAM))]
    )
    instruction = info.transaction.message.instructions.add()
    instruction.program_id_index = 1
    instruction.accounts = bytes([0])
    info.meta.log_messages.extend(
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            _encode_trade_event(parser),
            f"Program {PUMP_PROGRAM} success",
        ]
    )
    info.meta.err.err = b"transaction failed despite successful-looking logs"
    listener = object.__new__(UniversalGeyserListener)
    listener.platform_parsers = {}
    listener.trade_hub = TradeFlowHub(parser)
    queue = listener.trade_hub.subscribe(MINT)
    listener._process_update_events(update)  # noqa: SLF001 - actual ingress boundary
    assert queue.empty()

    info.meta.ClearField("err")
    info.signature = bytes([2]) * 64
    update.transaction.slot = 2
    listener._process_update_events(update)  # noqa: SLF001 - actual ingress boundary
    assert queue.get_nowait().slot == update.transaction.slot
    assert queue.empty()


def test_duplicate_transaction_cannot_trigger_net_outflow_exit() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(
            parser,
            quote_mint=str(WSOL_MINT),
            quote_amount=3_000_000_000,
            real_quote_reserves=10_000_000_000,
            user=OTHER,
        ),
        f"Program {PUMP_PROGRAM} success",
    ]
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    monitor = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=FlowRules(creator_sell=False, net_outflow_pct=0.5),
    )
    signals = []
    for slot in (10, 11):
        hub.publish_logs(logs, slot=slot, signature="replayed")
        while not queue.empty():
            signal = monitor.observe(queue.get_nowait())
            signals.append(signal.rule if signal else None)
    assert signals == [None]  # 3 SOL, not the replay-inflated 6 SOL exit.


def test_distinct_trades_inside_one_transaction_remain_distinct() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    payload = _encode_trade_event(
        parser, sol_amount=3_000_000_000, real_sol_reserves=10_000_000_000
    )
    invocation = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        payload,
        f"Program {PUMP_PROGRAM} success",
    ]
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    hub.publish_logs(invocation + invocation, slot=1, signature="two-trades")
    monitor = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=FlowRules(creator_sell=False, net_outflow_pct=0.5),
    )
    assert monitor.observe(queue.get_nowait()) is None
    signal = monitor.observe(queue.get_nowait())
    assert signal is not None and signal.rule == "net_outflow"
    hub.publish_logs(invocation + invocation, slot=2, signature="two-trades")
    assert queue.empty()


def test_invalid_batch_does_not_poison_later_valid_delivery() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(parser),
        f"Program {PUMP_PROGRAM} success",
    ]
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    hub.publish_logs(logs[:-1], slot=1, signature="corrected")
    hub.publish_logs(logs, slot=1, signature="")
    assert queue.empty()
    hub.publish_logs(logs, slot=1, signature="corrected")
    event = queue.get_nowait()
    assert (event.signature, event.sol_amount) == ("corrected", 1_500_000_000)
    assert queue.empty()


def test_signature_retention_is_bounded_without_refreshing_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("monitoring.trade_flow._MAX_RECENT_TRANSACTIONS", 2)
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(parser),
        f"Program {PUMP_PROGRAM} success",
    ]
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    for signature in ("a", "b", "a", "c", "b", "c", "a"):
        hub.publish_logs(logs, slot=1, signature=signature)
    received = []
    while not queue.empty():
        received.append(queue.get_nowait().signature)
    assert received == ["a", "b", "c", "a"]


@pytest.mark.parametrize("later_slot", [1, 2])
def test_standalone_geyser_stream_deduplicates_before_flow_rules(
    monkeypatch: pytest.MonkeyPatch, later_slot: int
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    update = geyser_pb2.SubscribeUpdate()
    update.transaction.slot = 1
    info = update.transaction.transaction
    info.signature = bytes([1]) * 64
    info.meta.log_messages.extend(
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            _encode_trade_event(
                parser,
                quote_mint=str(WSOL_MINT),
                quote_amount=3_000_000_000,
                real_quote_reserves=10_000_000_000,
                user=OTHER,
            ),
            f"Program {PUMP_PROGRAM} success",
        ]
    )
    later = geyser_pb2.SubscribeUpdate()
    later.CopyFrom(update)
    later.transaction.slot = later_slot
    later.transaction.transaction.signature = bytes([2]) * 64
    later.transaction.transaction.meta.log_messages[1] = _encode_trade_event(
        parser,
        quote_mint=str(WSOL_MINT),
        quote_amount=1_000_000_000,
        real_quote_reserves=9_000_000_000,
        user=OTHER,
    )
    call = MagicMock()
    call.initial_metadata = AsyncMock(return_value=())
    call.__aiter__.return_value = [update, update, later]
    channel = SimpleNamespace(close=AsyncMock())
    stub = SimpleNamespace(Subscribe=lambda _requests: call)
    monkeypatch.setattr(
        "monitoring.trade_flow.grpc.aio.secure_channel", lambda *_args: channel
    )
    monkeypatch.setattr(
        "monitoring.trade_flow.geyser_pb2_grpc.GeyserStub", lambda _channel: stub
    )
    stream = GeyserTradeStream(
        endpoint="offline.invalid:443",
        api_token="",
        auth_type="x-token",
        idl_parser=parser,
    )
    monitor = FlowMonitor(
        mint=MINT,
        creator=CREATOR,
        rules=FlowRules(creator_sell=False, net_outflow_pct=0.5),
    )

    async def consume() -> list[tuple[int, str | None]]:
        observations = []
        with pytest.raises(ConnectionError):
            async for event in stream.stream(mint=MINT, bonding_curve=OTHER):
                signal = monitor.observe(event)
                observations.append((event.sol_amount, signal.rule if signal else None))
        return observations

    assert asyncio.run(consume()) == [(3_000_000_000, None), (1_000_000_000, None)]


@pytest.mark.asyncio
async def test_overflow_invalidates_only_the_lagging_subscriber() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser, queue_size=1)
    slow = hub.subscribe(MINT)
    fast = hub.subscribe(MINT)
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(parser),
        f"Program {PUMP_PROGRAM} success",
    ]
    hub.publish_logs(logs, slot=1, signature="first")
    assert fast.get_nowait().signature == "first"
    hub.publish_logs(logs, slot=2, signature="overflow")
    assert fast.get_nowait().signature == "overflow"
    with pytest.raises(TradeFlowLossError):
        await asyncio.wait_for(slow.get(), 1)
    with pytest.raises(TradeFlowLossError):
        slow.get_nowait()
    with pytest.raises(TradeFlowLossError):
        slow.put_nowait(_event())
    assert slow.empty()  # No unconsumed history survives the gap.
    hub.publish_logs(logs, slot=3, signature="fresh")
    assert fast.get_nowait().signature == "fresh"
    assert hub.dropped == 2  # noqa: PLR2004 - queued item plus incoming overflow
    hub.unsubscribe(MINT, fast)
    assert not hub.active


def test_slot_regression_quarantines_only_affected_mint_subscriptions() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    fast, slow = hub.subscribe(MINT), hub.subscribe(MINT)
    other = hub.subscribe(OTHER)
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(parser),
        f"Program {PUMP_PROGRAM} success",
    ]
    for signature in ("first", "same-slot"):
        hub.publish_logs(logs, slot=10, signature=signature)
        assert fast.get_nowait().signature == signature
    # A known duplicate is ignored before ordering checks.
    hub.publish_logs(logs, slot=9, signature="first")
    assert fast.loss_reason is None and slow.loss_reason is None
    hub.publish_logs(logs, slot=9, signature="backwards")
    hub.publish_logs(logs, slot=11, signature="cannot-revive")
    for queue in (fast, slow):
        assert queue.empty()
        with pytest.raises(TradeFlowLossError) as error:
            queue.get_nowait()
        assert error.value.reason == "out_of_order"
    logs[1] = _encode_trade_event(parser, mint=OTHER)
    hub.publish_logs(logs, slot=5, signature="independent-mint")
    assert other.get_nowait().signature == "independent-mint"


@pytest.mark.asyncio
async def test_overflow_wakes_every_waiting_reader() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser, queue_size=1)
    queue = hub.subscribe(MINT)
    readers = [asyncio.create_task(queue.get()) for _ in range(3)]
    await asyncio.sleep(0)
    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        _encode_trade_event(parser),
        f"Program {PUMP_PROGRAM} success",
    ]
    hub.publish_logs(logs, slot=1, signature="buffered")
    hub.publish_logs(logs, slot=2, signature="lost")
    results = await asyncio.wait_for(
        asyncio.gather(*readers, return_exceptions=True), 1
    )
    assert all(isinstance(result, TradeFlowLossError) for result in results)
    with pytest.raises(TradeFlowLossError):
        await asyncio.wait_for(queue.get(), 1)  # Later readers fail too.

    fresh = hub.subscribe(MINT)
    cancelled = asyncio.create_task(fresh.get())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    readers = [asyncio.create_task(fresh.get()) for _ in range(2)]
    await asyncio.sleep(0)
    hub.publish_logs(logs, slot=3, signature="after-cancellation")
    await asyncio.sleep(0)  # Both wake; only one can take the first event.
    hub.publish_logs(logs, slot=4, signature="next")
    events = await asyncio.wait_for(asyncio.gather(*readers), 1)
    assert {event.signature for event in events} == {"after-cancellation", "next"}


@pytest.mark.asyncio
async def test_discovery_discards_pending_candidates_from_lost_history() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser, queue_size=1)
    discovery = CycleDiscovery(
        hub, target_sol_lamports=10_000_000_000, buy_amount_lamports=1_000_000_000
    )
    event = _event(real=10_000_000_000, vsol=85_000_000_000, vtok=100_000_000_000_000)
    for mint in (MINT, OTHER):
        discovery.track(mint, "curve-" + mint)
        discovery.update_pool(
            mint,
            Pool(
                address="pool-" + mint,
                program=AMM,
                mints=(SOL, mint),
                vaults=("vault-a", "vault-b"),
                reserves=(1_000_000_000_000, 200_000_000_000_000),
                trade_rate=2500,
            ),
        )
        discovery.ingest(replace(event, mint=mint))
    reader = discovery._tasks[MINT]  # noqa: SLF001 - await the actual consumer's shutdown
    try:
        assert {candidate.mints for candidate in discovery.discover()} == {
            (MINT,),
            (OTHER,),
        }
        for mint in (MINT, OTHER):
            discovery.ingest(replace(event, mint=mint, slot=100))
        logs = [
            f"Program {PUMP_PROGRAM} invoke [1]",
            _encode_trade_event(parser),
            f"Program {PUMP_PROGRAM} success",
        ]
        hub.publish_logs(logs, slot=100, signature="buffered")
        hub.publish_logs(logs, slot=101, signature="lost")
        # Check before the consumer gets an event-loop turn to clean up.
        assert [candidate.mints for candidate in discovery.discover()] == [(OTHER,)]
        await asyncio.wait_for(reader, 1)
        assert discovery.errors == 1
        for mint in (MINT, OTHER):
            discovery.ingest(replace(event, mint=mint, slot=200))
        assert [candidate.mints for candidate in discovery.discover()] == [(OTHER,)]
        discovery.ingest(replace(event, mint=OTHER, slot=300))
        other_reader = discovery._tasks[OTHER]
        hub.stream_interrupted()
        assert discovery.discover() == []
        await asyncio.wait_for(other_reader, 1)
        assert discovery.errors == 2  # noqa: PLR2004 - overflow, then transport loss
    finally:
        await discovery.stop()
        discovery.untrack(MINT)
        discovery.untrack(OTHER)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_profitable", [False, True])
async def test_discovery_quarantines_backwards_direct_ingestion(
    *, initial_profitable: bool
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    discovery = CycleDiscovery(
        TradeFlowHub(parser),
        target_sol_lamports=10_000_000_000,
        buy_amount_lamports=1_000_000_000,
    )
    discovery.track(MINT, "curve")
    discovery.update_pool(
        MINT,
        Pool(
            address="pool",
            program=AMM,
            mints=(SOL, MINT),
            vaults=("a", "b"),
            reserves=(1_000_000_000_000, 200_000_000_000_000),
            trade_rate=2500,
        ),
    )
    profitable = _event(
        slot=12, real=10_000_000_000, vsol=85_000_000_000, vtok=100_000_000_000_000
    )
    unprofitable = replace(profitable, virtual_sol_reserves=500_000_000_000)
    reader = discovery._tasks[MINT]
    try:
        discovery.ingest(profitable if initial_profitable else unprofitable)
        discovery.ingest(
            replace(unprofitable if initial_profitable else profitable, slot=11)
        )
        assert discovery.discover() == []
        discovery.ingest(replace(profitable, slot=13))
        assert discovery.discover() == []
        await asyncio.wait_for(reader, 1)
        assert discovery.errors == 1
    finally:
        await discovery.stop()
        discovery.untrack(MINT)


def _geyser_trade_update(
    parser: IDLParser, **fields: object
) -> geyser_pb2.SubscribeUpdate:
    update = geyser_pb2.SubscribeUpdate()
    update.transaction.slot = 1
    info = update.transaction.transaction
    info.signature = bytes([1]) * 64
    info.transaction.message.account_keys.extend(
        [bytes(Pubkey.from_string(MINT)), bytes(Pubkey.from_string(PUMP_PROGRAM))]
    )
    instruction = info.transaction.message.instructions.add()
    instruction.program_id_index = 1
    instruction.accounts = bytes([0])
    info.meta.log_messages.extend(
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            _encode_trade_event(parser, **fields),
            f"Program {PUMP_PROGRAM} success",
        ]
    )
    return update


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["eof", "error", "cancel"])
async def test_geyser_loss_invalidates_all_readers_before_cleanup(ending: str) -> None:  # noqa: PLR0915 - transport/cleanup race harness
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    buffered = hub.subscribe(MINT)
    pending = asyncio.create_task(hub.subscribe(OTHER).get())
    update = _geyser_trade_update(
        parser, user=OTHER, is_buy=True, real_sol_reserves=200_000_000
    )
    delivered = False
    reading = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    async def next_update() -> geyser_pb2.SubscribeUpdate:
        nonlocal delivered
        if not delivered:
            delivered = True
            return update
        if ending == "cancel":
            reading.set()
            await asyncio.Event().wait()
        if ending == "error":
            raise ConnectionError
        raise StopAsyncIteration

    async def close() -> None:
        closing.set()
        await release.wait()

    call = MagicMock()
    call.initial_metadata = AsyncMock(return_value=())
    call.__aiter__.side_effect = lambda: call
    call.__anext__.side_effect = next_update
    stub = SimpleNamespace(Subscribe=lambda _requests: call)
    listener = UniversalGeyserListener(
        "offline.invalid:443", "", "x-token", platforms=[Platform.PUMP_FUN]
    )
    listener.trade_hub = hub
    listener._create_geyser_connection = AsyncMock(
        return_value=(stub, SimpleNamespace(close=close))
    )
    listener.wait_before_reconnect = AsyncMock(side_effect=asyncio.CancelledError)
    task = asyncio.create_task(listener.listen_for_tokens(AsyncMock()))
    try:
        if ending == "cancel":
            await asyncio.wait_for(reading.wait(), 1)
            task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        with pytest.raises(TradeFlowLossError) as buffered_error:
            buffered.get_nowait()
        assert buffered_error.value.reason == "interrupted"
        with pytest.raises(TradeFlowLossError) as waiting_error:
            await asyncio.wait_for(pending, 1)
        assert waiting_error.value.reason == "interrupted"
        with pytest.raises(TradeFlowLossError):
            hub.subscribe(OTHER).get_nowait()
        assert hub.dropped == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_geyser_reconnect_needs_ack_and_does_not_revive_old_history() -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    old_queue = hub.subscribe(MINT)
    update = _geyser_trade_update(parser)
    first = MagicMock()
    first.initial_metadata = AsyncMock(return_value=())
    first.__aiter__.return_value = [update]
    handshaking = asyncio.Event()
    acknowledge = asyncio.Event()
    receiving = asyncio.Event()
    incoming: asyncio.Queue[geyser_pb2.SubscribeUpdate] = asyncio.Queue()

    async def metadata() -> tuple[object, ...]:
        handshaking.set()
        await acknowledge.wait()
        return ()

    async def next_update() -> geyser_pb2.SubscribeUpdate:
        receiving.set()
        return await incoming.get()

    second = MagicMock()
    second.initial_metadata = metadata
    second.__aiter__.side_effect = lambda: second
    second.__anext__.side_effect = next_update
    listener = UniversalGeyserListener(
        "offline.invalid:443", "", "x-token", platforms=[Platform.PUMP_FUN]
    )
    listener.trade_hub = hub
    listener._create_geyser_connection = AsyncMock(
        side_effect=[
            (
                SimpleNamespace(Subscribe=lambda _: first),
                SimpleNamespace(close=AsyncMock()),
            ),
            (
                SimpleNamespace(Subscribe=lambda _: second),
                SimpleNamespace(close=AsyncMock()),
            ),
        ]
    )
    listener.wait_before_reconnect = AsyncMock()
    task = asyncio.create_task(listener.listen_for_tokens(AsyncMock()))
    try:
        await asyncio.wait_for(handshaking.wait(), 1)
        during_gap = hub.subscribe(MINT)
        with pytest.raises(TradeFlowLossError):
            during_gap.get_nowait()
        acknowledge.set()
        await asyncio.wait_for(receiving.wait(), 1)
        fresh_queue = hub.subscribe(MINT)
        incoming.put_nowait(
            update
        )  # Previously admitted signature remains deduplicated.
        fresh = geyser_pb2.SubscribeUpdate()
        fresh.CopyFrom(update)
        fresh.transaction.slot = 2
        fresh.transaction.transaction.signature = bytes([2]) * 64
        incoming.put_nowait(fresh)
        assert (
            await asyncio.wait_for(fresh_queue.get(), 1)
        ).slot == fresh.transaction.slot
        for queue in (old_queue, during_gap):
            with pytest.raises(TradeFlowLossError):
                queue.get_nowait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["eof", "error", "cancel", "out_of_order"])
@pytest.mark.parametrize("latched", [False, True])
async def test_standalone_loss_clears_pending_exit_before_cleanup(  # noqa: PLR0915 - transport/cleanup race harness
    monkeypatch: pytest.MonkeyPatch, ending: str, *, latched: bool
) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    token = TokenInfo(
        name="Token",
        symbol="TOK",
        uri="",
        mint=Pubkey.from_string(MINT),
        platform=Platform.PUMP_FUN,
        creator=Pubkey.from_string(CREATOR),
    )
    trader = object.__new__(UniversalTrader)
    trader._gate_queues = {}
    trader._flow_signals = {}
    trader._flow_latched = {MINT} if latched else set()
    trader._flow_wakeups = {MINT: asyncio.Event()}
    trader.flow_rules = FlowRules(creator_sell=True)
    trader.geyser_endpoint = "offline.invalid:443"
    trader.geyser_api_token = ""
    trader.geyser_auth_type = "x-token"
    trader.platform_implementations = SimpleNamespace(
        event_parser=SimpleNamespace(_idl_parser=parser)
    )
    trader._get_pool_address = lambda _token: token.mint
    position = SimpleNamespace(entry_price=1.0, is_active=True)
    update = _geyser_trade_update(parser, user=CREATOR, is_buy=False)
    delivered = False
    observed = asyncio.Event()
    disconnect = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    async def next_update() -> geyser_pb2.SubscribeUpdate:
        nonlocal delivered
        if not delivered:
            delivered = True
            return update
        observed.set()
        await disconnect.wait()
        if ending == "out_of_order":
            if update.transaction.slot == 0:
                await asyncio.Event().wait()
            update.transaction.slot = 0
            update.transaction.transaction.signature = bytes([2]) * 64
            return update
        if ending == "error":
            raise ConnectionError
        raise StopAsyncIteration

    async def close() -> None:
        closing.set()
        await release.wait()

    call = MagicMock()
    call.initial_metadata = AsyncMock(return_value=())
    call.__aiter__.side_effect = lambda: call
    call.__anext__.side_effect = next_update
    monkeypatch.setattr(
        "monitoring.trade_flow.grpc.aio.secure_channel",
        lambda *_args: SimpleNamespace(close=close),
    )
    monkeypatch.setattr(
        "monitoring.trade_flow.geyser_pb2_grpc.GeyserStub",
        lambda _channel: SimpleNamespace(Subscribe=lambda _requests: call),
    )
    task = asyncio.create_task(trader._consume_trade_flow(token, position))
    try:
        await asyncio.wait_for(observed.wait(), 1)
        assert (MINT in trader._flow_signals) is not latched
        if ending == "cancel":
            task.cancel()
        else:
            disconnect.set()
        await asyncio.wait_for(closing.wait(), 1)
        assert trader._flow_signals == {}
        assert trader._flow_latched == ({MINT} if latched else set())
        assert position.is_active
    finally:
        release.set()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connection", "acknowledgement", "parsers"])
async def test_unstarted_geyser_stream_invalidates_subscriptions(stage: str) -> None:
    parser = IDLParser(
        str(Path(__file__).resolve().parents[1] / "idl/pump_fun_idl.json")
    )
    hub = TradeFlowHub(parser)
    queue = hub.subscribe(MINT)
    queue.put_nowait(_event())
    listener = UniversalGeyserListener(
        "offline.invalid:443", "", "x-token", platforms=[Platform.PUMP_FUN]
    )
    listener.trade_hub = hub
    call = SimpleNamespace(
        initial_metadata=AsyncMock(side_effect=TimeoutError), cancel=MagicMock()
    )
    listener._create_geyser_connection = AsyncMock(
        side_effect=ConnectionError if stage == "connection" else None,
        return_value=(
            SimpleNamespace(Subscribe=lambda _requests: call),
            SimpleNamespace(close=AsyncMock()),
        ),
    )
    listener.wait_before_reconnect = AsyncMock(side_effect=asyncio.CancelledError)
    if stage == "parsers":
        listener.platform_parsers = {}
        await asyncio.wait_for(listener.listen_for_tokens(AsyncMock()), 1)
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(listener.listen_for_tokens(AsyncMock()), 1)
    with pytest.raises(TradeFlowLossError) as error:
        queue.get_nowait()
    assert error.value.reason == "interrupted"
    with pytest.raises(TradeFlowLossError):
        hub.subscribe(MINT).get_nowait()
    assert hub.dropped == 1
