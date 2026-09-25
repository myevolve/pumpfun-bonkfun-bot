"""Replay recorded coins through the bot's real EntryGate and FlowMonitor.

Feeds each coin's recorded trades (as TradeEvents) into the production gate
and exit-rule code from src/monitoring/trade_flow.py, then prices the
resulting entry/exit with summarize_lifecycles.py's curve math. Two outputs:

1. Gate decision distribution: accept rate, rejection reasons, slots waited.
2. A consistency check: the bot's gate must accept exactly the coins that the
   backtester's independent gate (entry_latency=3, min_buyers=1,
   max_real_sol=0.5, mayhem_only, creator holding) accepts. A divergence is
   a bug in one of them.

Offline; no network.

    uv run learning-examples/token-lifecycles/replay_gate.py lifecycles_1h.jsonl
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from solders.account import Account  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core.client import estimate_transaction_fee_lamports  # noqa: E402
from core.pubkeys import WSOL_MINT, normalize_quote_mint  # noqa: E402
from core.quote_engine import minimum_output_with_slippage  # noqa: E402
from monitoring.trade_flow import (  # noqa: E402
    EntryGate,
    FlowMonitor,
    FlowRules,
    GateRules,
    TradeEvent,
)
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.fee_schedule import (  # noqa: E402
    PumpFeeSnapshot,
    decode_fee_config_account,
    quote_buy_exact_out,
    quote_sell_exact_in,
)
from trading.position import ExitReason, Position  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "summarize_lifecycles", Path(__file__).with_name("summarize_lifecycles.py")
)
sl = importlib.util.module_from_spec(_spec)
sys.modules["summarize_lifecycles"] = sl
_spec.loader.exec_module(sl)

LAMPORTS = 1_000_000_000
SCORING_SOURCES = (
    "learning-examples/token-lifecycles/replay_gate.py",
    "src/core/client.py",
    "src/core/pubkeys.py",
    "src/core/quote_engine.py",
    "src/monitoring/trade_flow.py",
    "src/trading/position.py",
    "src/platforms/pumpfun/fee_schedule.py",
    "src/platforms/pumpfun/address_provider.py",
)


def events_of(coin: dict) -> list[TradeEvent]:
    base = coin["create_slot"]
    return [
        TradeEvent(
            mint=coin["mint"],
            user=t[1],
            creator=coin["creator"],
            is_buy=bool(t[2]),
            sol_amount=t[3],
            token_amount=t[4],
            virtual_sol_reserves=t[6],
            virtual_token_reserves=t[7],
            real_sol_reserves=t[5],
            # New TradeEvent field; tape rows are index-aligned with trades
            # (run_gate asserts the same alignment below).
            real_token_reserves=coin["trade_real_token_reserves"][index],
            slot=base + t[0],
            signature="",
            timestamp=t[8],
        )
        for index, t in enumerate(coin["trades"])
        if t[6] > 0 and t[7] > 0
    ]


def run_gate(coin: dict, rules: GateRules) -> tuple[str, int, int | None]:
    """Return (reason, slots_waited, index of the accepting event or None)."""
    if rules.mayhem_only and not coin["mayhem"]:
        return "not_mayhem", 0, None
    gate = EntryGate(
        mint=coin["mint"],
        creator=coin["creator"],
        creation_slot=coin["create_slot"],
        rules=rules,
    )
    for i, ev in enumerate(events_of(coin)):
        decision = gate.observe(ev)
        if decision is not None:
            return (
                decision.reason,
                decision.slots_waited,
                (i if decision.accept else None),
            )
    d = gate.timed_out()
    return d.reason, d.slots_waited, None


def _require_tape(condition: bool, reason: str) -> None:  # noqa: FBT001
    if not condition:
        raise ValueError(reason)


def _next_head(
    heads: list[dict], head_times: list[float], when: float, minimum_slot: int
) -> dict:
    for index in range(bisect_left(head_times, when), len(heads)):
        if heads[index]["slot"] >= minimum_slot:
            return heads[index]
    raise ValueError("required_future_head_not_observed")


def simulate_configured(  # noqa: C901, PLR0912, PLR0915
    coin: dict, heads: list[dict], lock: dict, snapshot: PumpFeeSnapshot
) -> dict:
    """Evaluate one frozen observed-state counterfactual, never a native fill."""
    row = {"mint": coin.get("mint"), "status": "unpriced", "net_lamports": None}
    try:
        profile = lock["profile"]
        gate_config = profile["entry_gate"]
        _require_tape(gate_config["enabled"] is True, "configured_gate_not_enabled")
        rules = GateRules(
            **{key: value for key, value in gate_config.items() if key != "enabled"}
        )
        quote_mint = Pubkey.from_string(coin["quote_mint"])
        if normalize_quote_mint(quote_mint) != WSOL_MINT:
            return {**row, "status": "not_entered", "reason": "unsupported_quote"}
        _require_tape(type(coin.get("mayhem")) is bool, "mayhem_provenance_unknown")
        if rules.mayhem_only and not coin["mayhem"]:
            return {**row, "status": "not_entered", "reason": "not_mayhem"}
        _require_tape(coin.get("stream_gap") is False, "stream_coverage_unknown")
        _require_tape(
            coin.get("clock") == "local_monotonic_receive_time", "receive_clock_unknown"
        )
        started = coin["create_received_monotonic"]
        duration = coin["observed_duration_ms"] / 1000
        _require_tape(
            isfinite(started) and isfinite(duration) and duration >= 0,
            "invalid_coverage_clock",
        )
        covered_until = started + duration
        trades = coin["trades"]
        events = events_of(coin)
        offsets = coin["trade_received_ms"]
        real_tokens = coin["trade_real_token_reserves"]
        _require_tape(
            len(trades) == len(events) == len(offsets) == len(real_tokens),
            "unaligned_trade_evidence",
        )
        _require_tape(
            all(isfinite(value) and value >= 0 for value in offsets),
            "invalid_receive_offsets",
        )
        _require_tape(
            all(left <= right for left, right in pairwise(offsets)),
            "receive_order_ambiguous",
        )
        times = [started + offset / 1000 for offset in offsets]
        head_times = [head["received_monotonic"] for head in heads]
        _require_tape(bool(heads), "slot_clock_missing")
        _require_tape(
            all(isfinite(value) for value in head_times), "invalid_slot_clock"
        )
        _require_tape(
            all(left <= right for left, right in pairwise(head_times)),
            "slot_receive_order_ambiguous",
        )
        max_gap = lock["capture"].get("max_slot_gap_seconds")
        _require_tape(
            isinstance(max_gap, int | float)
            and not isinstance(max_gap, bool)
            and isfinite(max_gap)
            and max_gap > 0,
            "slot_coverage_bound_not_preregistered",
        )

        def require_coverage(until: float) -> None:
            _require_tape(
                until <= covered_until, "observation_ends_before_required_state"
            )
            first = bisect_right(head_times, started) - 1
            last = bisect_left(head_times, until)
            _require_tape(
                first >= 0 and last < len(heads), "slot_interval_not_bracketed"
            )
            _require_tape(
                all(
                    0 <= head_times[index] - head_times[index - 1] <= max_gap
                    and heads[index]["slot"] > heads[index - 1]["slot"]
                    for index in range(first + 1, last + 1)
                ),
                "slot_delivery_gap_or_reordering",
            )

        gate = EntryGate(
            mint=coin["mint"],
            creator=coin["creator"],
            creation_slot=coin["create_slot"],
            rules=rules,
        )
        decision = None
        accepted_index = None
        for index, event in enumerate(events):
            _require_tape(event.slot >= coin["create_slot"], "trade_before_creation")
            if offsets[index] > rules.max_wait_ms:
                break
            decision = gate.observe(event)
            require_coverage(times[index])
            if decision is not None:
                if decision.accept:
                    accepted_index = index
                break
        if accepted_index is None:
            if decision is None:
                _require_tape(
                    duration * 1000 >= rules.max_wait_ms, "gate_window_incomplete"
                )
                require_coverage(started + rules.max_wait_ms / 1000)
                decision = gate.timed_out()
            return {
                **row,
                "status": "not_entered",
                "reason": decision.reason,
                "gate_buyers": decision.buyers,
            }
        row.update(
            gate_buyers=decision.buyers, gate_received_ms=offsets[accepted_index]
        )

        def state_at(when: float, head_slot: int) -> dict:
            require_coverage(when)
            graduation = coin.get("graduated_dslot")
            _require_tape(
                graduation is None or coin["create_slot"] + graduation > head_slot,
                "graduated_before_modeled_closure",
            )
            index = bisect_right(times, when) - 1
            while index >= 0 and events[index].slot > head_slot:
                index -= 1
            _require_tape(index >= 0, "observed_curve_state_unavailable")
            trade = trades[index]
            return {
                "complete": False,
                "creator": coin["creator"],
                "quote_mint": quote_mint,
                "token_total_supply": coin["supply"],
                "virtual_quote_reserves": trade[6],
                "virtual_token_reserves": trade[7],
                "real_quote_reserves": trade[5],
                "real_token_reserves": real_tokens[index],
            }

        entry_head = _next_head(
            heads,
            head_times,
            times[accepted_index],
            events[accepted_index].slot + lock["rules"]["entry_delay_slots"],
        )
        entry_at = entry_head["received_monotonic"]
        entry_state = state_at(entry_at, entry_head["slot"])
        trade_config = profile["trade"]
        _require_tape(
            trade_config["exit_strategy"] == "tp_sl", "unsupported_exit_policy"
        )
        amount = trade_config["extreme_fast_token_amount"]
        _require_tape(type(amount) is int and amount > 0, "invalid_fixed_quantity")
        quantity_raw = amount * 1_000_000
        quote = quote_buy_exact_out(entry_state, quantity_raw, snapshot)
        if quote.amount_in_raw > lock["caps"]["max_trade_quote_raw"]:
            return {
                **row,
                "status": "not_entered",
                "reason": "entry_quote_above_cap",
                "required_quote_raw": quote.amount_in_raw,
            }
        priority = profile["priority_fees"]["fixed_amount"]
        buy_fee = estimate_transaction_fee_lamports(
            priority, profile["compute_units"]["buy"]
        )
        sell_fee = estimate_transaction_fee_lamports(
            priority, profile["compute_units"]["sell"]
        )
        _require_tape(
            profile["cleanup"]["mode"] == "after_sell"
            and profile["cleanup"]["with_priority_fee"] is False,
            "unsupported_cleanup_policy",
        )
        cleanup_fee = 5000
        _require_tape(
            max(buy_fee, sell_fee, cleanup_fee)
            <= lock["caps"]["max_total_fee_lamports"],
            "fee_cap_exceeded",
        )
        row.update(
            entry_quote_raw=quote.amount_in_raw,
            quantity_raw=quantity_raw,
            entry_slot=entry_head["slot"],
            entry_received_ms=(entry_at - started) * 1000,
            buy_fee_estimate_lamports=buy_fee,
            sell_fee_estimate_lamports=sell_fee,
            cleanup_fee_estimate_lamports=cleanup_fee,
        )
        position = Position.create_from_buy_result(
            mint=Pubkey.from_string(coin["mint"]),
            symbol=coin.get("symbol") or coin["mint"],
            entry_price=quote.amount_in_raw / LAMPORTS / amount,
            quantity=amount,
            take_profit_percentage=trade_config["take_profit_percentage"],
            stop_loss_percentage=trade_config["stop_loss_percentage"],
            max_hold_time=trade_config["max_hold_time"],
            quantity_raw=quantity_raw,
            quote_amount_raw=quote.amount_in_raw,
            buy_fee_lamports=buy_fee,
            account_balance_baseline_raw=0,
        )
        epoch = datetime(2000, 1, 1, tzinfo=UTC)
        position.entry_time = epoch
        interval = trade_config["price_check_interval"]
        _require_tape(type(interval) is int and interval > 0, "invalid_poll_interval")
        with patch("trading.position.datetime") as clock:
            for elapsed in range(0, trade_config["max_hold_time"] + interval, interval):
                when = entry_at + elapsed
                head_index = bisect_right(head_times, when) - 1
                _require_tape(head_index >= 0, "poll_head_unavailable")
                current_head = heads[head_index]
                state = state_at(when, current_head["slot"])
                price = (
                    state["virtual_quote_reserves"]
                    / LAMPORTS
                    / (state["virtual_token_reserves"] / 1_000_000)
                )
                clock.now.return_value = epoch + timedelta(seconds=elapsed)
                should_exit, reason = position.should_exit(price)
                if not should_exit:
                    continue
                current_quote = quote_sell_exact_in(
                    state, quantity_raw, snapshot
                ).amount_out_raw
                floor = minimum_output_with_slippage(
                    current_quote, int(trade_config["sell_slippage"] * 10_000)
                )
                if reason is ExitReason.TAKE_PROFIT:
                    net_floor = (
                        position.take_profit_net_quote_raw + sell_fee + cleanup_fee
                    )
                    if current_quote < net_floor:
                        continue
                    floor = max(floor, net_floor)
                row.update(
                    trigger=reason.value,
                    trigger_elapsed_seconds=elapsed,
                    minimum_exit_quote_raw=floor,
                )
                exit_head = _next_head(
                    heads,
                    head_times,
                    when,
                    current_head["slot"] + lock["rules"]["exit_delay_slots"],
                )
                exit_state = state_at(
                    exit_head["received_monotonic"], exit_head["slot"]
                )
                exit_quote = quote_sell_exact_in(
                    exit_state, quantity_raw, snapshot
                ).amount_out_raw
                row.update(exit_slot=exit_head["slot"], exit_quote_raw=exit_quote)
                _require_tape(
                    exit_quote >= floor, "potential_exit_floor_revert_not_a_sale"
                )
                row.update(
                    status="conditionally_priced",
                    reason="frozen_observed_state_model_not_native_execution",
                    net_lamports=exit_quote
                    - quote.amount_in_raw
                    - buy_fee
                    - sell_fee
                    - cleanup_fee,
                )
                return row
        _require_tape(condition=False, reason="no_fully_observed_exit")
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        row["reason"] = f"{type(exc).__name__}: {exc}"
        return row


def configured_report(path: Path, lock_path: Path, out: Path) -> dict:
    """Score only the prospective locked tape; never rank or reuse old holdouts."""
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    for source, digest in lock["source_sha256"].items():
        _require_tape(
            hashlib.sha256((ROOT / source).read_bytes()).hexdigest() == digest,
            f"source_hash_mismatch: {source}",
        )
    missing_sources = sorted(set(SCORING_SOURCES) - lock["source_sha256"].keys())
    _require_tape(
        path.resolve() == (ROOT / lock["tape_path"]).resolve(),
        "tape_does_not_match_lock",
    )
    _require_tape(
        lock["native_fee_input"].get("native_attestation") is True,
        "native_fee_attestation_missing",
    )
    payload = lock["native_fee_input"]["fee_account"]
    _require_tape(
        payload.get("address") == str(PumpFunAddresses.find_fee_config()),
        "noncanonical_fee_config_account",
    )
    account = Account(
        lamports=payload["lamports"],
        data=base64.b64decode(payload["data_base64"], validate=True),
        owner=Pubkey.from_string(payload["owner"]),
        executable=payload["executable"],
        rent_epoch=payload["rent_epoch"],
    )
    config = decode_fee_config_account(account)
    _require_tape(
        config.digest == lock["native_fee_input"]["config_digest"],
        "fee_input_digest_mismatch",
    )
    snapshot = PumpFeeSnapshot(
        config,
        lock["native_fee_input"]["observed_at"],
        lock["native_fee_input"]["attested_at"],
    )
    heads = [
        json.loads(line)
        for line in (ROOT / lock["slot_clock_path"]).read_text().splitlines()
        if line.strip()
    ]
    rows = [
        simulate_configured(json.loads(line), heads, lock, snapshot)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    priced = [row for row in rows if row["status"] == "conditionally_priced"]
    report = {
        "claim": "Preregistered immediate-handler observed-state counterfactual, not actual dispatcher admissions, fills or executable edge",
        "source_binding": {
            "all_pinned_sources_match": True,
            "scoring_sources_missing_from_preregistration": missing_sources,
            "fully_source_bound": not missing_sources,
        },
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "tape_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "counts": dict(Counter(row["status"] for row in rows)),
        "slot_clock_sha256": hashlib.sha256(
            (ROOT / lock["slot_clock_path"]).read_bytes()
        ).hexdigest(),
        "reasons": dict(Counter(row["reason"] for row in rows)),
        "entered": sum("entry_quote_raw" in row for row in rows),
        "mean_priced_net_lamports": statistics.mean(
            row["net_lamports"] for row in priced
        )
        if priced
        else None,
        "rows": rows,
        "limits": [
            "Gate timing assumes an immediate independent handler; production queue admission and scheduling were not observed.",
            "Counterfactual observed states do not include the hypothetical buy's market impact.",
            "Processed observations do not prove finality, leader inclusion, or native execution.",
            "One frozen attested fee input is not continuous fee attestation through the capture.",
            "No failed-exit retry, MEV, tip, or persistent account-rent cost is invented; failed/unknown closure stays unpriced.",
            "Independent trials are not an executable portfolio and do not reset or consume a risk session.",
        ],
    }
    with out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--buy-sol", type=float, default=0.01)
    ap.add_argument("--hold-slots", type=int, default=25)
    ap.add_argument("--exit-latency", type=int, default=1)
    ap.add_argument(
        "--lock", type=Path, help="frozen prospective profile lock; no legacy backtest"
    )
    ap.add_argument("--out", type=Path, help="new configured-model JSON report")
    args = ap.parse_args()
    if args.lock is not None:
        if args.out is None or any(
            getattr(args, key) != ap.get_default(key)
            for key in ("buy_sol", "hold_slots", "exit_latency")
        ):
            ap.error(
                "--lock requires --out and does not accept legacy policy overrides"
            )
        report = configured_report(args.path, args.lock, args.out)
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "rows"}, indent=2
            )
        )
        return
    if args.out is not None:
        ap.error("--out requires --lock")
    coins = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    coins = [c for c in coins if c["n_trades"] > 1 and c["dev_buy_sol"] > 0]
    rules = GateRules()
    buy = int(args.buy_sol * LAMPORTS)

    reasons: Counter[str] = Counter()
    waits: list[int] = []
    bot_accepts: set[str] = set()
    for c in coins:
        reason, slots, idx = run_gate(c, rules)
        reasons[reason] += 1
        if idx is not None:
            bot_accepts.add(c["mint"])
            waits.append(slots)
    print(
        f"coins={len(coins)}  bot gate accepts={len(bot_accepts)} ({len(bot_accepts) / len(coins):.1%})"
    )
    for reason, n in reasons.most_common():
        print(f"  {reason:16s} {n:>5d}")
    if waits:
        print(
            f"  slots waited on accept: p50={statistics.median(waits):.0f} "
            f"max={max(waits)} dist={dict(sorted(Counter(waits).items()))}"
        )

    # Backtester's independent gate on the same coins.
    policy = sl.Policy(
        entry_latency=rules.max_wait_slots,
        exit_latency=args.exit_latency,
        take_profit=None,
        trailing=None,
        stop_loss=None,
        creator_sell=False,
        outflow=None,
        max_hold=args.hold_slots,
        gate_no_creator_sell=rules.require_creator_holding,
        gate_max_real_sol=rules.max_real_sol,
        gate_no_mayhem=False,
        gate_mayhem_only=rules.mayhem_only,
        gate_min_buyers=rules.min_buyers,
    )
    bt_accepts = {c["mint"] for c in coins if sl.simulate(c, policy, buy) is not None}
    only_bot = bot_accepts - bt_accepts
    only_bt = bt_accepts - bot_accepts
    print(
        f"\nconsistency vs backtester gate: both={len(bot_accepts & bt_accepts)} "
        f"bot-only={len(only_bot)} backtester-only={len(only_bt)}"
    )
    by_mint = {c["mint"]: c for c in coins}
    for label, group in (("bot-only", only_bot), ("backtester-only", only_bt)):
        for mint in sorted(group)[:5]:
            c = by_mint[mint]
            print(
                f"  {label}: {c['symbol']:10s} first slots={[t[0] for t in c['trades'][:6]]} "
                f"buyers={[t[1][:4] for t in c['trades'][:6] if t[2] and t[1] != c['creator']]} "
                f"real={[round(t[5] / LAMPORTS, 3) for t in c['trades'][:6]]}"
            )

    # PnL of the bot-gated set under the fixed hold, priced by the simulator.
    pnls = [
        r
        for c in coins
        if c["mint"] in bot_accepts
        for r in [sl.simulate(c, policy, buy)]
        if r is not None
    ]
    if pnls:
        wins = sum(1 for r in pnls if r > 0)
        print(
            f"\nbot-gated PnL (hold {args.hold_slots} slots, exit +{args.exit_latency}): n={len(pnls)} "
            f"per-trade={statistics.mean(pnls) / buy:+.1%} win={wins / len(pnls):.0%} "
            f"total={sum(pnls) / LAMPORTS:+.4f} SOL"
        )

    # What the exit engine would have added on top of the fixed hold.
    flow = FlowRules(creator_sell=True, trailing_stop=None)
    fired: Counter[str] = Counter()
    for c in coins:
        if c["mint"] not in bot_accepts:
            continue
        _, _, idx = run_gate(c, rules)
        evs = events_of(c)
        entry_price = evs[idx].price
        mon = FlowMonitor(
            mint=c["mint"], creator=c["creator"], rules=flow, entry_price=entry_price
        )
        for ev in evs[idx + 1 :]:
            if ev.slot - evs[idx].slot > args.hold_slots:
                break
            sig = mon.observe(ev)
            if sig is not None:
                fired[sig.rule] += 1
                break
        else:
            fired["none_within_hold"] += 1
    print(f"exit-engine (creator_sell) within the hold window: {dict(fired)}")


if __name__ == "__main__":
    main()
