from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest

from monitoring.trade_flow import (
    FlowMonitor,
    FlowRules,
    TradeEvent,
    decode_trade_events,
)
from utils.idl_parser import IDLParser

CREATOR = "BtKJ2RYx274LJoXcyWrJ71bG7LeF53w6vTACuCNbhr9"
MINT = "67xAZNxvBtfR6YXFyvVSkRiziKVmXUWvCBvhT9Aupump"
OTHER = "dw3EJbG7Wk3RvbcASWQGKcmbaXvBn2v1uDhUfPQyY5Y"


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
    logs = [_encode_trade_event(parser, virtual_token_reserves=0)]
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
    """Borsh-encode a TradeEvent log line with the leading fields we rely on."""
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
