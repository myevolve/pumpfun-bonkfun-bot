"""Read-only lifecycle coverage must not fabricate time, buyers, or completeness."""

# Assertions and direct decoded-event inputs are deliberate test boundaries.
# ruff: noqa: S101, SLF001, PLR2004, PLC0415

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_recorder():  # noqa: ANN201
    """Importing the recorder must never implicitly inspect a credential file."""
    spec = importlib.util.spec_from_file_location(
        "lifecycle_observation_recorder",
        ROOT / "learning-examples/token-lifecycles/record_lifecycles.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch(
        "dotenv.load_dotenv", side_effect=AssertionError("implicit credentials read")
    ):
        spec.loader.exec_module(module)
    return module


def test_receive_offsets_and_protocol_buyer_exclusion(tmp_path: Path) -> None:
    """The tape retains protocol trades, but not as an independent buyer."""
    module = load_recorder()
    out = tmp_path / "tape.jsonl"
    recorder = module.Recorder(out, idle_seconds=20, max_age=40)
    recorder.admission_cutoff = 200.0
    recorder._on_create(
        {
            "mint": "mint",
            "creator": "creator",
            "symbol": "TEST",
            "token_total_supply": 1000,
        },
        100,
        "synthetic",
        100.0,
        {"verified": False, "reason": "test", "source": "synthetic"},
    )
    trade = {
        "mint": "mint",
        "is_buy": True,
        "sol_amount": 10,
        "token_amount": 2,
        "real_sol_reserves": 20,
        "virtual_sol_reserves": 30,
        "virtual_token_reserves": 1000,
        "real_token_reserves": 900,
    }
    recorder._on_trade({**trade, "user": module.MAYHEM_SOL_VAULT}, 100, "vault", 100.25, 0)
    recorder._on_trade({**trade, "user": "buyer"}, 100, "buyer", 100.75, 1)
    with patch.object(module.time, "monotonic", return_value=101.0):
        recorder.flush_finished(force=True)
    recorder.out.close()
    recorder.slot_out.close()
    row = json.loads(out.read_text())
    assert row["buyers_slot0"] == 1
    assert len(row["trades"]) == 2
    assert row["trade_received_ms"] == [250.0, 750.0]
    assert row["trade_real_token_reserves"] == [900, 900]
    assert row["observed_duration_ms"] == 1000.0 and row["partial"] is True


def test_slot_only_stream_still_reaches_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot messages must be retained; an idle transaction stream cannot hang."""
    module = load_recorder()
    out = tmp_path / "tape.jsonl"
    recorder = module.Recorder(out, idle_seconds=20, max_age=40)

    class Channel:
        async def close(self) -> None:
            return None

    class Stream:
        def __init__(self) -> None:
            self.ping_answered = asyncio.Event()
            self._reads = 0

        async def write(self, request: object) -> None:
            if request.HasField("ping"):
                self.ping_answered.set()

        async def initial_metadata(self) -> tuple:
            return ()

        def cancel(self) -> None:
            return None

        async def read(self):  # noqa: ANN202 - matches gRPC stream call
            # run() polls stream.read(), not async iteration. Emit ping,
            # then the slot, then park forever.
            if self._reads == 0:
                self._reads = 1
                return module.geyser_pb2.SubscribeUpdate(
                    ping=module.geyser_pb2.SubscribeUpdatePing()
                )
            if self._reads == 1:
                self._reads = 2
                return module.geyser_pb2.SubscribeUpdate(
                    slot=module.geyser_pb2.SubscribeUpdateSlot(slot=42, parent=41)
                )
            await asyncio.Event().wait()

    stream = Stream()
    monkeypatch.setattr(module, "connect", lambda _credentials: Channel())
    monkeypatch.setattr(
        module.geyser_pb2_grpc,
        "GeyserStub",
        lambda _channel: SimpleNamespace(Subscribe=lambda: stream),
    )

    async def exercise() -> None:
        await asyncio.wait_for(
            module.run(recorder, 0.0005, {"GEYSER_ENDPOINT": "synthetic"}, drain_seconds=0.0001),
            timeout=1,
        )

    asyncio.run(exercise())
    slots = [
        json.loads(line)
        for line in out.with_suffix(".slots.jsonl").read_text().splitlines()
    ]
    assert [row["slot"] for row in slots] == [42]


def test_configured_exit_requires_observed_time_and_ignores_future_trades() -> None:
    """A short tape is not a ten-second hold, nor is a later trade an earlier fill."""
    spec = importlib.util.spec_from_file_location(
        "configured_lifecycle_replay",
        ROOT / "learning-examples/token-lifecycles/replay_gate.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from platforms.pumpfun.fee_schedule import PumpFeeConfig, PumpFees, PumpFeeTier

    key = module.Pubkey.from_bytes(bytes([1]) * 32)
    fees = PumpFees(0, 95, 30)
    tiers = (PumpFeeTier(0, fees),)
    snapshot = module.PumpFeeSnapshot(
        PumpFeeConfig(0, key, fees, tiers, tiers, fees, "synthetic-not-attested"),
        1.0,
        1.0,
    )
    lock = {
        "capture": {"max_slot_gap_seconds": 1.0},
        "profile": {
            "entry_gate": {
                "enabled": True,
                "mayhem_only": True,
                "min_buyers": 1,
                "max_real_sol": 0.5,
                "min_real_sol": 0.1,
                "require_creator_holding": True,
                "max_wait_slots": 3,
                "max_wait_ms": 1500,
            },
            "trade": {
                "exit_strategy": "tp_sl",
                "extreme_fast_token_amount": 250000,
                "take_profit_percentage": 0.1,
                "stop_loss_percentage": 0.25,
                "max_hold_time": 10,
                "price_check_interval": 1,
                "sell_slippage": 0.3,
            },
            "priority_fees": {"fixed_amount": 200000},
            "compute_units": {"buy": 140000, "sell": 110000},
            "cleanup": {"mode": "after_sell", "with_priority_fee": False},
        },
        "caps": {"max_trade_quote_raw": 13000000, "max_total_fee_lamports": 250000},
        "rules": {"entry_delay_slots": 1, "exit_delay_slots": 1},
    }
    coin = {
        "mint": str(key),
        "creator": "creator",
        "quote_mint": str(module.WSOL_MINT),
        "mayhem": True,
        "stream_gap": False,
        "clock": "local_monotonic_receive_time",
        "create_slot": 100,
        "create_received_monotonic": 100.0,
        "observed_duration_ms": 5000,
        "supply": 10**15,
        "trades": [
            [
                0,
                "buyer",
                1,
                200000000,
                10**12,
                200000000,
                30000000000,
                1073000000000000,
                0,
            ]
        ],
        "trade_received_ms": [250],
        "trade_real_token_reserves": [793000000000000],
    }
    coin["creator"] = str(module.Pubkey.from_bytes(bytes([2]) * 32))
    heads = [
        {"slot": 100 + index, "received_monotonic": 100.0 + index * 0.4}
        for index in range(60)
    ]
    short = module.simulate_configured(coin, heads, lock, snapshot)
    assert "entry_quote_raw" in short
    assert short["status"] == "unpriced" and short["net_lamports"] is None
    assert "observation_ends_before_required_state" in short["reason"]

    coin["observed_duration_ms"] = 20000
    complete = module.simulate_configured(coin, heads, lock, snapshot)
    assert complete["status"] == "conditionally_priced"
    assert complete["trigger"] == "max_hold_time" and complete["net_lamports"] < 0

    coin["trades"].append(
        [35, "late-buyer", 1, 10**12, 10**12, 10**12, 10**12, 10**14, 0]
    )
    coin["trade_received_ms"].append(14000)
    coin["trade_real_token_reserves"].append(793000000000000)
    later = module.simulate_configured(coin, heads, lock, snapshot)
    assert later["net_lamports"] == complete["net_lamports"]

    stalled = [dict(head) for head in heads]
    for head in stalled[10:]:
        head["received_monotonic"] += 2
    gap = module.simulate_configured(coin, stalled, lock, snapshot)
    assert gap["status"] == "unpriced" and gap["net_lamports"] is None

    # A gross +10% signal does not cover the net target after both sides' fees.
    quantity = complete["quantity_raw"]
    virtual_tokens = coin["trades"][0][7]
    gross_signal_quote = (
        complete["entry_quote_raw"] * 11 * virtual_tokens + 10 * quantity - 1
    ) // (10 * quantity) + 1
    gross_signal = {
        **coin,
        "trades": [
            coin["trades"][0],
            [
                2,
                "buyer",
                1,
                10**9,
                10**12,
                10**10,
                gross_signal_quote,
                virtual_tokens,
                0,
            ],
        ],
        "trade_received_ms": [250, 800],
        "trade_real_token_reserves": [793000000000000, 793000000000000],
    }
    net_checked = module.simulate_configured(gross_signal, heads, lock, snapshot)
    assert net_checked["status"] == "conditionally_priced"
    assert net_checked["trigger"] == "max_hold_time"

    same_slot = {
        **coin,
        "trades": [[slot, *coin["trades"][0][1:]] for slot in (0, 2, 2)],
        "trade_received_ms": [250, 800, 1200],
        "trade_real_token_reserves": [793000000000000] * 3,
    }
    assert (
        module.simulate_configured(same_slot, heads, lock, snapshot)["status"]
        == "conditionally_priced"
    )
    same_slot["trades"][-1][0] = 1
    reordered = module.simulate_configured(same_slot, heads, lock, snapshot)
    assert reordered["status"] == "unpriced" and reordered["net_lamports"] is None
    assert "trade_slot_order_ambiguous" in reordered["reason"]
