"""Score frozen meme-volume hypotheses on saved DEX candles; never connect or trade.

python evaluate_meme_patterns.py meme_market_history_20260913.json --out result.json
python evaluate_meme_patterns.py --self-check

This is a delayed-price event study, not a fill simulator or portfolio backtest.
Missing/zero-volume execution marks remain unpriced, not silently profitable exits.
Current surviving-pool selection is not an unbiased historical token universe.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from summarize_lifecycles import q

# Numeric thresholds implement the frozen research protocol, not tunable defaults.
# ruff: noqa: PLR2004
BAR = 900
DAY = 86400
RULES = ("volume_breakout", "volume_trend", "selloff_reclaim", "failed_breakout")
COSTS = (30, 60, 100, 200, 300)


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _validate(rows: list[list[float]], asof: int) -> None:
    previous = -1
    for row in rows:
        if len(row) != 6 or not all(
            isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
            for v in row
        ):
            raise ValueError("invalid_numeric_candle")
        timestamp, opened, high, low, closed, volume = row
        if (
            not isinstance(timestamp, int)
            or timestamp % BAR
            or not previous < timestamp < asof
        ):
            raise ValueError("duplicate_unordered_or_unclosed_candle")
        if (
            not 0 < low <= min(opened, closed) <= max(opened, closed) <= high
            or volume < 0
        ):
            raise ValueError("invalid_ohlcv_bounds")
        previous = timestamp


def _features(rows: list[list[float]]) -> list[dict | None]:
    volume_sums = [0.0]
    for row in rows:
        volume_sums.append(volume_sums[-1] + row[5])
    features: list[dict | None] = [None] * len(rows)
    for i in range(97, len(rows)):
        timestamp, opened, high, low, closed, volume = rows[i]
        if timestamp - rows[i - 97][0] != 97 * BAR:
            continue
        average = (volume_sums[i] - volume_sums[i - 96]) / 96
        previous_average = (volume_sums[i - 1] - volume_sums[i - 97]) / 96
        if min(average, previous_average) <= 0:
            continue
        previous = rows[i - 1]
        rvol = volume / average
        change = closed / previous[4] - 1
        previous_change = previous[4] / rows[i - 2][4] - 1
        four_hour_change = closed / rows[i - 16][4] - 1
        resistance = max(row[2] for row in rows[i - 16 : i])
        close_location = (closed - low) / (high - low) if high > low else 0.5
        conditions = (
            rvol >= 2 and closed > resistance and close_location >= 0.75,
            rvol >= 2 and change >= 0.01 and four_hour_change > 0,
            previous_change <= -0.02
            and previous[5] / previous_average >= 2
            and low < previous[3]
            and closed > opened
            and closed > (previous[2] + previous[3]) / 2
            and rvol >= 1,
            rvol >= 2
            and high > resistance
            and closed <= resistance
            and close_location <= 0.5,
        )
        features[i] = {
            "rvol": rvol,
            "change": change,
            "four_hour_change": four_hour_change,
            "signals": [
                rule for rule, active in zip(RULES, conditions, strict=True) if active
            ],
        }
    return features


def _net(gross: float, cost_bps: float) -> float:
    side = cost_bps / 20000
    return (1 + gross) * (1 - side) / (1 + side) - 1


def _mark(lookup: dict, entry: int, exit_time: int) -> dict:
    window = [lookup.get(t) for t in range(entry, exit_time + BAR, BAR)]
    if any(row is None for row in window):
        return {"status": "missing_execution_history"}
    if window[0][5] <= 0 or window[-1][5] <= 0:
        return {"status": "zero_volume_execution_mark"}
    opened = window[0][1]
    return {
        "status": "priced",
        "entry_price": opened,
        "exit_price": window[-1][1],
        "gross": window[-1][1] / opened - 1,
        "adverse_excursion": min(row[3] for row in window[:-1]) / opened - 1,
        "favorable_excursion": max(row[2] for row in window[:-1]) / opened - 1,
    }


def _events(
    history: dict,
    features: list[dict | None],
    lookups: dict,
    policy: tuple[str, int],
    window: tuple[int, int, int],
) -> list[dict]:
    rule, hold = policy
    start, split, asof = window
    market = history["market"]
    identity = (market["chain"], market["token"])
    lookup = lookups[identity]
    cost = market["primary_cost_bps"]
    if (
        not math.isfinite(cost)
        or not market.get("advertised_roundtrip_pool_fee_bps", 0) <= cost < 10000
    ):
        raise ValueError("invalid_or_below_known_fee_primary_cost")
    next_signal = start
    events = []
    for row, feature in zip(history["bars"], features, strict=True):
        timestamp = row[0]
        entry = timestamp + 2 * BAR
        exit_time = entry + hold * BAR
        if feature is None or timestamp < next_signal or exit_time >= asof:
            continue
        if rule == "fixed_clock":
            active = entry % (hold * BAR) == 0
        else:
            active = rule in feature["signals"]
        if not active or (timestamp < split <= exit_time):
            continue
        # Event spacing, including unpriced attempts; not a real position-close claim.
        next_signal = exit_time
        event = {
            "symbol": market["symbol"],
            "chain": market["chain"],
            "token": market["token"],
            "rule": rule,
            "hold_minutes": hold * 15,
            "phase": "train" if exit_time < split else "test",
            "signal": timestamp,
            "entry": entry,
            "exit": exit_time,
            "rvol": feature["rvol"],
            "primary_cost_bps": cost,
        }
        event.update(_mark(lookup, entry, exit_time))
        if event["status"] == "priced":
            event["net"] = _net(event["gross"], cost)
            peers = []
            for other, other_lookup in lookups.items():
                if other != identity and other[0] == market["chain"]:
                    mark = _mark(other_lookup, entry, exit_time)
                    if mark["status"] == "priced":
                        peers.append(mark["gross"])
            event["peer_count"] = len(peers)
            event["peer_excess_gross"] = (
                event["gross"] - statistics.fmean(peers) if peers else None
            )
        events.append(event)
    return events


def _stats(events: list[dict]) -> dict:
    priced = [event for event in events if event["status"] == "priced"]
    result = {
        "attempts": len(events),
        "priced": len(priced),
        "unpriced": len(events) - len(priced),
        "unpriced_reasons": dict(
            Counter(e["status"] for e in events if e["status"] != "priced")
        ),
    }
    if not priced:
        return result
    nets = [e["net"] for e in priced]
    excess = [
        e["peer_excess_gross"] for e in priced if e["peer_excess_gross"] is not None
    ]
    result.update(
        active_days=len({e["entry"] // DAY for e in priced}),
        mean_gross_pct=100 * statistics.fmean(e["gross"] for e in priced),
        mean_primary_net_pct=100 * statistics.fmean(nets),
        median_primary_net_pct=100 * statistics.median(nets),
        win_pct=100 * sum(value > 0 for value in nets) / len(nets),
        p05_primary_net_pct=100 * q(nets, 0.05),
        worst_primary_net_pct=100 * min(nets),
        mean_same_chain_peer_excess_gross_pct=100 * statistics.fmean(excess)
        if excess
        else None,
        peer_matched_count=len(excess),
        mean_net_pct_by_total_cost_bps={
            str(cost): 100 * statistics.fmean(_net(e["gross"], cost) for e in priced)
            for cost in COSTS
        },
        p05_adverse_excursion_pct=100
        * q([e["adverse_excursion"] for e in priced], 0.05),
        median_favorable_excursion_pct=100
        * statistics.median(e["favorable_excursion"] for e in priced),
    )
    return result


def _day_interval(events: list[dict], start: int, end: int) -> list[float] | None:
    by_day: dict[int, list[float]] = defaultdict(list)
    for event in events:
        if event["status"] == "priced":
            by_day[event["entry"] // DAY].append(event["net"])
    if len(by_day) < 2:
        return None
    totals = [
        (sum(by_day[day]), len(by_day[day]))
        for day in range(start // DAY, (end - 1) // DAY + 1)
    ]
    rng = random.Random(1703)  # noqa: S311 - deterministic statistical resampling
    draws = []
    for _ in range(2000):
        sample = rng.choices(totals, k=len(totals))
        count = sum(row[1] for row in sample)
        if count:
            draws.append(100 * sum(row[0] for row in sample) / count)
    return [q(draws, 0.025), q(draws, 0.975)]


def _selected_details(events: list[dict], split: int, asof: int) -> dict:
    test = [e for e in events if e["phase"] == "test"]
    priced = [e for e in test if e["status"] == "priced"]
    day_totals: dict[int, float] = defaultdict(float)
    for event in priced:
        day_totals[event["entry"] // DAY] += event["net"]
    best_day = max(day_totals, key=day_totals.get) if day_totals else None
    return {
        "holdout_day_block_95pct_interval": _day_interval(test, split, asof),
        "holdout_without_best_day": _stats(
            [e for e in test if e["entry"] // DAY != best_day]
        ),
        "holdout_first_half": _stats(
            [e for e in test if e["entry"] < (split + asof) // 2]
        ),
        "holdout_second_half": _stats(
            [e for e in test if e["entry"] >= (split + asof) // 2]
        ),
        "holdout_by_asset": {
            symbol: _stats([e for e in test if e["symbol"] == symbol])
            for symbol in sorted({e["symbol"] for e in test})
        },
        "events": events,
    }


def _analyze(data: dict) -> dict:
    asof = data["asof"]
    start, split = asof - 60 * DAY, asof - 30 * DAY
    lookups = {}
    features = {}
    coverage = []
    watch = []
    for history in data["histories"]:
        market, rows = history["market"], history["bars"]
        _validate(rows, asof)
        identity = (market["chain"], market["token"])
        if identity in lookups:
            raise ValueError("duplicate_token_market_would_double_count")
        lookups[identity] = {row[0]: row for row in rows}
        feature = _features(rows)
        features[identity] = feature
        tested = [row for row in rows if row[0] >= split]
        ranges = [(row[2] / row[3] - 1) * 100 for row in tested]
        coverage.append(
            {
                "market": market,
                "bars": len(rows),
                "missing_bar_count": sum(
                    (right[0] - left[0]) // BAR - 1
                    for left, right in itertools.pairwise(rows)
                ),
                "zero_volume_bars": sum(row[5] == 0 for row in rows),
                "first": _utc(rows[0][0]) if rows else None,
                "last": _utc(rows[-1][0]) if rows else None,
                "expected_bars": (asof - data["history_start"]) // BAR,
                "complete_requested_interval": bool(rows)
                and rows[0][0] == data["history_start"]
                and rows[-1][0] == asof - BAR
                and len(rows) == (asof - data["history_start"]) // BAR,
                "holdout_median_15m_range_pct": q(ranges, 0.5) if ranges else None,
                "holdout_p90_15m_range_pct": q(ranges, 0.9) if ranges else None,
            }
        )
        if rows and feature[-1] is not None:
            watch.append(
                {
                    "symbol": market["symbol"],
                    "chain": market["chain"],
                    "closed_at": _utc(rows[-1][0] + BAR),
                    **feature[-1],
                }
            )
    cells = []
    cell_events = {}
    for rule in (*RULES, "fixed_clock"):
        for hold in (4, 16):
            events = []
            for history in data["histories"]:
                market = history["market"]
                identity = (market["chain"], market["token"])
                events.extend(
                    _events(
                        history,
                        features[identity],
                        lookups,
                        (rule, hold),
                        (start, split, asof),
                    )
                )
            events.sort(key=lambda e: (e["entry"], e["chain"], e["token"]))
            cell = {
                "rule": rule,
                "hold_minutes": hold * 15,
                "train": _stats([e for e in events if e["phase"] == "train"]),
                "test": _stats([e for e in events if e["phase"] == "test"]),
            }
            cell["test_by_asset"] = {
                symbol: _stats(
                    [
                        e
                        for e in events
                        if e["phase"] == "test" and e["symbol"] == symbol
                    ]
                )
                for symbol in sorted({e["symbol"] for e in events})
            }
            cells.append(cell)
            cell_events[(rule, hold * 15)] = events
    ranked = sorted(
        (
            cell
            for cell in cells
            if cell["rule"] in RULES[:3] and cell["train"]["priced"] >= 30
        ),
        key=lambda cell: cell["train"]["mean_primary_net_pct"],
        reverse=True,
    )
    selected = None
    if ranked:
        selected = dict(ranked[0])
        selected.update(
            _selected_details(
                cell_events[(selected["rule"], selected["hold_minutes"])], split, asof
            )
        )
    alternatives = []
    for history in data.get("alternative_venues", []) if selected else []:
        market = history["market"]
        identity = (market["chain"], market["token"])
        if identity not in lookups:
            raise ValueError("alternative_must_match_an_existing_token")
        _validate(history["bars"], asof)
        alternative_lookups = {
            **lookups,
            identity: {row[0]: row for row in history["bars"]},
        }
        events = _events(
            history,
            _features(history["bars"]),
            alternative_lookups,
            (selected["rule"], selected["hold_minutes"] // 15),
            (start, split, asof),
        )
        alternatives.append(
            {
                "market": market,
                "rule": selected["rule"],
                "hold_minutes": selected["hold_minutes"],
                "bars": len(history["bars"]),
                "holdout_median_15m_range_pct": q(
                    [
                        (row[2] / row[3] - 1) * 100
                        for row in history["bars"]
                        if row[0] >= split
                    ],
                    0.5,
                ),
                "train": _stats(
                    [event for event in events if event["phase"] == "train"]
                ),
                "test": _stats([event for event in events if event["phase"] == "test"]),
                **_selected_details(events, split, asof),
            }
        )
    return {
        "kind": "exploratory_delayed_price_event_study_not_executable_pnl",
        "protocol": data["protocol"],
        "training_start": _utc(start),
        "holdout_start": _utc(split),
        "asof": _utc(asof),
        "coverage": coverage,
        "collection_failures": data.get("failures", []),
        "cells": cells,
        "training_ranked_rule_horizons": [
            {k: c[k] for k in ("rule", "hold_minutes")} for c in ranked
        ],
        "selected_on_training_only": selected,
        "latest_sampled_watch": watch,
        "source_backed_page_boundary_repairs": data.get("gap_repairs", []),
        "alternative_venue_sensitivity": alternatives,
        "venue_sensitivity_protocol": data.get("venue_sensitivity_protocol"),
        "interpretation": [
            "No trades, wallet reads, CoinLobster credit spend or bot changes.",
            "Net figures use per-market assumed all-in drag: 1% Solana, 3% deepest CASHCAT, 1% alternative CASHCAT; actual fills and total costs are unverified.",
            "Cost scenarios below advertised pool fees are not viable: deepest CASHCAT advertises 1% each side, alternative CASHCAT 0.3% each side.",
            "Unpriced attempts remain counted; mean returns and intervals are conditional on available nonzero-volume marks. No missing outcome is filled with zero return.",
            "One fixed schedule of nonoverlapping events per token/rule/horizon, not a capital-constrained compounded portfolio.",
            "No stops, take-profit fills or short trades inferred from candle high/low ordering.",
            "Day-block intervals preserve contemporaneous cross-token dependence; no stationarity, external replication or prospective-universe validation established.",
            "Current pool selection introduces survivorship bias; holdout controls rule fitting, not historical universe selection.",
            "Same-chain peer comparisons are gross controls; CASHCAT has no peer in this sample.",
            "Fixed-clock control is a separate nonoverlapping passive-entry schedule with the same delay, holding period and costs.",
            "Alternative-venue scoring holds the training-selected rule fixed but is post-hoc, same-token sensitivity, not an independent holdout replication.",
            "The provider revised one oldest candle open when more preceding context was available; the alternative history retains and documents the source-backed revision.",
        ],
    }


def _self_check() -> None:
    # One synthetic sequence checks causal features, delayed marks and censoring.
    rows = [[i * BAR, 100.0, 101.0, 99.0, 100.0, 100.0] for i in range(150)]
    rows[110] = [110 * BAR, 100.0, 105.0, 99.0, 104.0, 300.0]
    rows[112] = [112 * BAR, 110.0, 111.0, 99.0, 100.0, 100.0]
    rows[116] = [116 * BAR, 121.0, 122.0, 99.0, 100.0, 100.0]
    _validate(rows, 150 * BAR)
    features = _features(rows)
    assert "volume_breakout" in features[110]["signals"]  # noqa: S101
    changed = [row[:] for row in rows]
    changed[111:] = [
        [row[0], *(value * 2 for value in row[1:])] for row in changed[111:]
    ]
    assert _features(changed)[:111] == features[:111]  # noqa: S101
    lookup = {row[0]: row for row in rows}
    mark = _mark(lookup, 112 * BAR, 116 * BAR)
    assert (  # noqa: S101
        math.isclose(mark["gross"], 0.1)
        and _net(mark["gross"], 300) < _net(mark["gross"], 100)
    )
    market = {
        "chain": "solana",
        "token": "check",
        "symbol": "CHECK",
        "primary_cost_bps": 100,
    }
    events = _events(
        {"market": market, "bars": rows},
        features,
        {("solana", "check"): lookup},
        ("volume_breakout", 4),
        (0, 120 * BAR, 150 * BAR),
    )
    assert (  # noqa: S101
        len(events) == 1
        and events[0]["entry"] == 112 * BAR
        and math.isclose(events[0]["gross"], 0.1)
    )
    purged = _events(
        {"market": market, "bars": rows},
        features,
        {("solana", "check"): lookup},
        ("volume_breakout", 4),
        (0, 114 * BAR, 150 * BAR),
    )
    assert purged == []  # noqa: S101
    lookup[116 * BAR] = [116 * BAR, 121.0, 122.0, 99.0, 100.0, 0.0]
    assert _mark(lookup, 112 * BAR, 116 * BAR)["status"] == "zero_volume_execution_mark"  # noqa: S101
    del lookup[115 * BAR]
    assert _mark(lookup, 112 * BAR, 116 * BAR)["status"] == "missing_execution_history"  # noqa: S101
    try:
        _validate([*rows, rows[-1]], 150 * BAR)
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate_candle_accepted")
    print(
        "PASS: causal features, delayed entry, costs, split purge and unpriced outcomes"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("data", type=Path, nargs="?")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _self_check()
        return
    if args.data is None or args.out is None:
        parser.error("data and --out are required without --self-check")
    raw = args.data.read_bytes()
    report = _analyze(json.loads(raw))
    report["input_file"] = str(args.data)
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    with args.out.open("x") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    for cell in report["cells"]:
        print(
            cell["rule"],
            cell["hold_minutes"],
            "train",
            cell["train"].get("mean_primary_net_pct"),
            "test",
            cell["test"].get("mean_primary_net_pct"),
            "test_n",
            cell["test"]["priced"],
            "unpriced",
            cell["test"]["unpriced"],
        )
    print("Saved", args.out)


if __name__ == "__main__":
    main()
