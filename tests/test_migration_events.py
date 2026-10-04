# ruff: noqa: S101 - regression assertions

import asyncio
import base64
import struct
from pathlib import Path

import base58

from monitoring.migration_events import (
    MigrationEvent,
    MigrationHub,
    _canonical_pamm_pool,
    decode_migration_events,
)
from monitoring.trade_flow import TradeEvent, TradeFlowHub
from utils.idl_parser import IDLParser

MINT = "67xAZNxvBtfR6YXFyvVSkRiziKVmXUWvCBvhT9Aupump"
SOL_MINT = "So11111111111111111111111111111111111111112"
OTHER = "dw3EJbG7Wk3RvbcASWQGKcmbaXvBn2v1uDhUfPQyY5Y"
IDL_DIR = Path(__file__).resolve().parents[1] / "idl"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PAMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_SLOT = 444_044_178
_BASE_AMOUNT_IN = 1_000
_QUOTE_AMOUNT_IN = 2_000
_POOL_BASE_AMOUNT = 3_000
_POOL_QUOTE_AMOUNT = 4_000

_TS = 1_788_463_175


def _parsers() -> tuple[IDLParser, IDLParser]:
    return (
        IDLParser(str(IDL_DIR / "pump_fun_idl.json")),
        IDLParser(str(IDL_DIR / "pump_swap_idl.json")),
    )


def _framed(program: str, *logs: str) -> list[str]:
    return [f"Program {program} invoke [1]", *logs, f"Program {program} success"]


def _key(seed: int) -> str:
    """A stable fake pubkey string for a seed (never all-zero)."""
    return base58.b58encode(bytes([(seed % 255) + 1] * 32)).decode()


def _raw(seed: int) -> bytes:
    return base58.b58decode(_key(seed))


def _complete_log(pump_parser: IDLParser, *, mint: str = MINT) -> str:
    disc = pump_parser.get_event_discriminators()["CompleteEvent"]
    payload = (
        disc
        + base58.b58decode(_key(1))  # user
        + base58.b58decode(mint)
        + base58.b58decode(_key(3))  # bonding_curve
        + struct.pack("<q", _TS)  # timestamp
        + base58.b58decode(SOL_MINT)  # quote_mint
    )
    return "Program data: " + base64.b64encode(payload).decode()


def _pool_log(pamm_parser: IDLParser, *, mint: str = MINT) -> str:
    disc = pamm_parser.get_event_discriminators()["CreatePoolEvent"]
    payload = (
        disc
        + struct.pack("<q", _TS)  # timestamp
        + struct.pack("<H", 0)  # index
        + base58.b58decode(_key(5))  # creator
        + base58.b58decode(mint)  # base_mint
        + base58.b58decode(SOL_MINT)  # quote_mint
        + struct.pack("<B", 6)  # base_mint_decimals
        + struct.pack("<B", 9)  # quote_mint_decimals
        + struct.pack("<Q", _BASE_AMOUNT_IN)  # base_amount_in
        + struct.pack("<Q", _QUOTE_AMOUNT_IN)  # quote_amount_in
        + struct.pack("<Q", _POOL_BASE_AMOUNT)  # pool_base_amount
        + struct.pack("<Q", _POOL_QUOTE_AMOUNT)  # pool_quote_amount
        + struct.pack("<Q", 5)  # minimum_liquidity
        + struct.pack("<Q", 6)  # initial_liquidity
        + struct.pack("<Q", 7)  # lp_token_amount_out
        + struct.pack("<B", 255)  # pool_bump
        + base58.b58decode(_canonical_pamm_pool(mint, SOL_MINT))  # pool
        + base58.b58decode(_key(9))  # lp_mint
        + base58.b58decode(_key(10))  # user_base_token_account
        + base58.b58decode(_key(11))  # user_quote_token_account
        + base58.b58decode(_key(12))  # coin_creator
        + struct.pack("<?", 1)  # is_mayhem_mode
    )
    return "Program data: " + base64.b64encode(payload).decode()


def test_decode_complete_and_pool_created() -> None:
    pump, pamm = _parsers()
    logs = _framed(
        PUMP_PROGRAM,
        _complete_log(pump),
        "Program data: bm90LWFuLWV2ZW50",  # valid b64, wrong disc
        "Program data: !!!not-base64!!!",
    ) + _framed(PAMM_PROGRAM, _pool_log(pamm))
    events = decode_migration_events(
        logs,
        slot=_SLOT,
        signature="sig",
        pump_parser=pump,
        pamm_parser=pamm,
    )
    assert [(e.kind, e.mint) for e in events] == [
        ("complete", MINT),
        ("pool_created", MINT),
    ]
    complete = events[0]
    assert complete.slot == _SLOT and complete.signature == "sig"
    assert complete.bonding_curve == _key(3)
    assert complete.pool is None
    assert complete.pool_base_amount == 0 and complete.pool_quote_amount == 0
    assert complete.timestamp == _TS
    pool = events[1]
    assert pool.pool == _canonical_pamm_pool(MINT, SOL_MINT)
    assert (
        pool.pool_base_amount == _POOL_BASE_AMOUNT
        and pool.pool_quote_amount == _POOL_QUOTE_AMOUNT
    )
    assert pool.bonding_curve is None
    assert pool.timestamp == _TS


def test_malformed_payloads_are_skipped() -> None:
    pump, pamm = _parsers()
    disc = pump.get_event_discriminators()["CompleteEvent"]
    logs = _framed(
        PUMP_PROGRAM,
        "Program data: " + base64.b64encode(disc + b"\x01\x02").decode(),
        _complete_log(pump),
    )
    events = decode_migration_events(
        logs, slot=1, signature="s", pump_parser=pump, pamm_parser=pamm
    )
    assert [e.kind for e in events] == ["complete"]


def test_canonical_payloads_from_wrong_emitters_are_rejected() -> None:
    pump, pamm = _parsers()
    logs = (
        _framed(OTHER, _complete_log(pump), _pool_log(pamm))
        + _framed(PUMP_PROGRAM, _pool_log(pamm))
        + _framed(PAMM_PROGRAM, _complete_log(pump))
    )
    assert (
        decode_migration_events(
            logs, slot=1, signature="forged", pump_parser=pump, pamm_parser=pamm
        )
        == []
    )


def _hub_and_events() -> tuple[MigrationHub, list[str]]:
    pump, pamm = _parsers()
    hub = MigrationHub(pump_parser=pump, pamm_parser=pamm)
    logs = _framed(PUMP_PROGRAM, _complete_log(pump)) + _framed(
        PAMM_PROGRAM, _pool_log(pamm, mint=OTHER)
    )
    return hub, decode_migration_events(
        logs, slot=7, signature="s", pump_parser=pump, pamm_parser=pamm
    )


def test_hub_fan_out_contains_errors() -> None:
    hub, events = _hub_and_events()
    assert len(events) == 2  # noqa: PLR2004 -- count is the point
    received: list[str] = []

    async def broken(_event: MigrationEvent) -> None:
        raise RuntimeError("boom")

    async def good(event: MigrationEvent) -> None:
        received.append(event.mint)

    hub.subscribe(broken)
    hub.subscribe(good)
    delivered = asyncio.run(hub.publish(events))
    assert delivered == len(events)  # only the good callback counts
    assert received == [MINT, OTHER]
    assert hub.errors == len(events)  # broken raises on both; good got both
    assert hub.active


def test_unsubscribe_stops_delivery() -> None:
    hub, events = _hub_and_events()
    seen: list[str] = []

    async def cb(event: MigrationEvent) -> None:
        seen.append(event.mint)

    hub.subscribe(cb)
    assert hub.active
    hub.unsubscribe(cb)
    assert not hub.active
    assert asyncio.run(hub.publish(events)) == 0
    assert seen == []
    hub.unsubscribe(cb)  # idempotent


def test_publish_logs_with_migration_hub_delivers_without_trade_subs() -> None:
    pump, pamm = _parsers()
    hub = MigrationHub(pump_parser=pump, pamm_parser=pamm)
    seen: list[str] = []

    async def cb(event: MigrationEvent) -> None:
        seen.append(event.mint)

    hub.subscribe(cb)
    trade_hub = TradeFlowHub(pump, migration_hub=hub)
    assert trade_hub.active
    logs = _framed(PUMP_PROGRAM, _complete_log(pump)) + _framed(
        PAMM_PROGRAM, _pool_log(pamm, mint=OTHER)
    )

    async def run() -> int:
        count = trade_hub.publish_logs(logs, slot=9, signature="s")
        assert trade_hub.publish_logs(logs, slot=10, signature="s") == 0
        await asyncio.sleep(0)  # let the scheduled fan-out task run
        return count

    count = asyncio.run(run())
    assert count == 2  # noqa: PLR2004 -- both events scheduled
    assert seen == [MINT, OTHER]
    assert hub.errors == 0


def test_publish_logs_without_migration_hub_is_unchanged() -> None:
    pump, _ = _parsers()
    hub = TradeFlowHub(pump)
    assert not hub.active

    # No subscribers, no migration hub: same early-zero behavior.
    logs = _framed(PUMP_PROGRAM, _complete_log(pump))
    assert hub.publish_logs(logs, slot=1, signature="s") == 0

    # Trade subscriber path: decode + deliver as before.
    queue = hub.subscribe(MINT)
    logs.insert(-1, _encode_trade_event(pump))
    delivered = hub.publish_logs(logs, slot=2, signature="s")
    assert delivered == 1
    event = queue.get_nowait()
    assert isinstance(event, TradeEvent) and event.mint == MINT


def _encode_trade_event(parser: IDLParser) -> str:
    """Borsh-encode a TradeEvent log line (same layout as test_trade_flow)."""
    disc = parser.get_event_discriminators()["TradeEvent"]
    body = b"".join(
        [
            base58.b58decode(MINT),
            struct.pack("<Q", 1_500_000_000),
            struct.pack("<Q", 30_000_000_000_000),
            struct.pack("<?", 0),
            base58.b58decode(OTHER),
            struct.pack("<q", _TS),
            struct.pack("<Q", 34_000_000_000),
            struct.pack("<Q", 950_000_000_000_000),
            struct.pack("<Q", 4_000_000_000),
            struct.pack("<Q", 0),
            base58.b58decode(OTHER),
            struct.pack("<Q", 100),
            struct.pack("<Q", 15_000_000),
            base58.b58decode(OTHER),
            struct.pack("<Q", 25),
            struct.pack("<Q", 3_750_000),
        ]
    )
    return "Program data: " + base64.b64encode(disc + body).decode()
