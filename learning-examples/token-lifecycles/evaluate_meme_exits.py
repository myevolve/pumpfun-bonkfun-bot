"""Lock a May-only exit selection before a separate June historical evaluation.

Offline retained CSV-in-ZIP data only; no native fills or portfolio ROI are inferred.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from evaluate_meme_patterns import _day_interval, _net
from summarize_lifecycles import q

# Thresholds are frozen protocol values, deliberately not CLI tuning parameters.
# Assertions below are confined to the offline self-check.
# ruff: noqa: PLR2004, S101
MINUTE = 60
BAR = 900
DAY = 86400
ENTRIES = ("flow_breakout", "flow_reclaim", "support_reclaim")
EXITS = ("hold_15m", "hold_60m", "close_trailing")
COSTS = (20, 50, 100, 150, 200)
MANIFEST_SHA256 = "ce1f9261011ea02df1b32324daae6c3182f33bce2595cf2c15bb1176dffa6a5c"
ASSUMPTIONS = [
    "15m returns are close / previous completed 15m close - 1, for token and SOL alike.",
    "Prior-bar midpoint means (high + low) / 2; green means close > open.",
    "Close time is the next minute/bar boundary, not the source's last microsecond.",
    "Signal bar must start inside its outcome month; earlier bars are warmup only.",
    "A boundary purge requires the entire maximum-hold exit minute inside the month, before reading execution prices.",
    "Signals while an event is open or entry is pending are skipped; unknown outcomes reserve maximum hold.",
    "Missing or zero-volume trailing observations make the path unpriced, not a carried-forward close.",
    "Ties in May mean net use frozen entry order then exit order; a negative winner still receives June evaluation.",
    "Best day is highest summed equal-notional event net; this is not compounded or a portfolio return.",
]
LIMITS = [
    "Surviving-token universe has selection bias; FARTCOIN and POPCAT have explicit 404 coverage gaps.",
    "Disjoint retrospective replication, not prospective evidence: hypotheses use later knowledge.",
    "CEX spot marks are not Solana DEX fills, depth, slippage, impact, routing, or native cost evidence.",
    "Nonzero-volume opens are marks, not guaranteed executable fills; stop triggers are not loss caps.",
    "Priced-only statistics may be biased by missing, zero-volume, and censored outcomes.",
    "No compounding, portfolio ROI, funded execution, deployment approval, or live-readiness conclusion.",
]


class Candle(NamedTuple):
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote: float
    buy_quote: float


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "evaluate_meme_exits.py",
            "evaluate_meme_patterns.py",
            "summarize_lifecycles.py",
        )
    }


def _month_bounds(month: str) -> tuple[int, int]:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=UTC)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp())


def _manifest(path: Path) -> dict:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        message = "Manifest does not match the exact frozen 20260913 protocol/metadata"
        raise ValueError(message)
    return json.loads(raw)


def _read_archive(directory: Path, metadata: dict) -> tuple[dict[int, Candle], dict]:
    # Recorded paths are repo-relative; only their expected basename is local.
    name = f"{metadata['symbol']}-1m-{metadata['month']}"
    if (
        Path(metadata["path"]).name != name + ".zip"
        or metadata["member"] != name + ".csv"
    ):
        message = "Unexpected archive/member name"
        raise ValueError(message)
    path = (directory / (name + ".zip")).resolve()
    if path.parent != directory.resolve():
        message = "Archive must reside directly inside the manifest directory"
        raise ValueError(message)
    with zipfile.ZipFile(path) as archive:
        if archive.namelist() != [metadata["member"]]:
            message = f"Unexpected ZIP members: {path.name}"
            raise ValueError(message)
        raw = archive.read(metadata["member"])
    csv_hash = hashlib.sha256(raw).hexdigest()
    if csv_hash != metadata["csv_sha256"]:
        message = f"CSV SHA256 mismatch: {path.name}; repacked ZIP hashes are not used"
        raise ValueError(message)
    start, end = _month_bounds(metadata["month"])
    minutes: dict[int, Candle] = {}
    previous = -1
    for line, row in enumerate(csv.reader(io.StringIO(raw.decode("utf-8"))), 1):
        if len(row) != 12:
            message = f"{path.name}:{line}: expected exactly 12 columns"
            raise ValueError(message)
        opened, closed = int(row[0]), int(row[6])
        timestamp = opened // 1_000_000
        values = [float(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10, 11)]
        if not all(math.isfinite(value) for value in values):
            message = f"{path.name}:{line}: nonfinite value"
            raise ValueError(message)
        candle = Candle(*values[:6], values[7])
        if (
            opened % 60_000_000 != 0
            or closed != opened + 60_000_000 - 1
            or not start <= timestamp < end
            or timestamp <= previous
            or candle.low <= 0
            or candle.low > min(candle.open, candle.close)
            or candle.high < max(candle.open, candle.close)
            or min(candle.volume, candle.quote, candle.buy_quote, values[6]) < 0
            or candle.buy_quote > candle.quote
            or values[6] > candle.volume
            or int(row[8]) < 0
        ):
            message = f"{path.name}:{line}: invalid microsecond minute/OHLC/volume row"
            raise ValueError(message)
        minutes[timestamp] = candle
        previous = timestamp
    actual = {
        "rows": len(minutes),
        "first": min(minutes) if minutes else None,
        "last": max(minutes) if minutes else None,
        "zero_volume_rows": sum(row.volume == 0 for row in minutes.values()),
    }
    if any(actual[key] != metadata[key] for key in actual):
        message = f"CSV metadata mismatch: {path.name}"
        raise ValueError(message)
    return minutes, {
        "symbol": metadata["symbol"],
        "month": metadata["month"],
        "member": metadata["member"],
        "csv_sha256": csv_hash,
        **actual,
        "missing_minutes": (end - start) // MINUTE - len(minutes),
    }


def _load(
    path: Path, manifest: dict, months: tuple[str, ...]
) -> tuple[dict, list[dict]]:
    data: dict[str, dict[int, Candle]] = defaultdict(dict)
    evidence = []
    for metadata in manifest["archives"]:
        if metadata["month"] in months:
            minutes, source = _read_archive(path.resolve().parent, metadata)
            data[metadata["symbol"]].update(minutes)
            evidence.append(source)
    return dict(data), evidence


def _bars(minutes: dict[int, Candle]) -> dict[int, Candle]:
    result = {}
    for timestamp in minutes:
        if timestamp % BAR:
            continue
        window = [minutes.get(t) for t in range(timestamp, timestamp + BAR, MINUTE)]
        if any(row is None for row in window):
            continue
        result[timestamp] = Candle(
            window[0].open,
            max(row.high for row in window),
            min(row.low for row in window),
            window[-1].close,
            sum(row.volume for row in window),
            sum(row.quote for row in window),
            sum(row.buy_quote for row in window),
        )
    return result


def _rvol(bars: dict[int, Candle], timestamp: int) -> float | None:
    prior = [bars.get(t) for t in range(timestamp - 96 * BAR, timestamp, BAR)]
    if timestamp not in bars or any(row is None for row in prior):
        return None
    average = statistics.fmean(row.quote for row in prior)
    return bars[timestamp].quote / average if average > 0 else None


def _signal(  # noqa: PLR0911
    bars: dict[int, Candle], benchmark: dict[int, Candle], timestamp: int, rule: str
) -> bool | None:
    """None means unavailable features, not an observed negative signal."""
    current = bars[timestamp]
    if current.quote <= 0:
        return None
    buy_share = current.buy_quote / current.quote
    previous = bars.get(timestamp - BAR)
    if rule == "flow_reclaim":
        before_previous = bars.get(timestamp - 2 * BAR)
        previous_rvol = _rvol(bars, timestamp - BAR)
        if previous is None or before_previous is None or previous_rvol is None:
            return None
        return (
            previous.close / before_previous.close - 1 <= -0.02
            and previous_rvol >= 2
            and current.low < previous.low
            and current.close > current.open
            and current.close > (previous.high + previous.low) / 2
            and buy_share >= 0.55
        )
    rvol = _rvol(bars, timestamp)
    if rvol is None:
        return None
    prior = [bars[t] for t in range(timestamp - 16 * BAR, timestamp, BAR)]
    upper = current.high > current.low and current.close >= current.low + 0.75 * (
        current.high - current.low
    )
    if rule == "support_reclaim":
        support = min(row.low for row in prior)
        return (
            rvol >= 2
            and current.low < support < current.close
            and upper
            and buy_share >= 0.55
        )
    if rule != "flow_breakout":
        message = f"Unknown entry: {rule}"
        raise ValueError(message)
    sol, sol_previous = benchmark.get(timestamp), benchmark.get(timestamp - BAR)
    if sol is None or sol_previous is None or previous is None:
        return None
    return (
        rvol >= 2
        and current.close > max(row.high for row in prior)
        and upper
        and buy_share >= 0.55
        and current.close / previous.close - 1 > sol.close / sol_previous.close - 1
    )


def _mark(minutes: dict[int, Candle], entry: int, exit_rule: str) -> dict:
    maximum = entry + (15 if exit_rule == "hold_15m" else 60) * MINUTE
    event = {"entry": entry, "exit": maximum, "max_exit": maximum}
    opened = minutes.get(entry)
    if opened is None or opened.volume == 0:
        return {**event, "status": "unpriced_entry_missing_or_zero_volume"}
    exit_time = maximum
    reason = "fixed_hold" if exit_rule != "close_trailing" else "maximum_hold"
    trigger = None
    if exit_rule == "close_trailing":
        highest = opened.open
        armed = False
        # Minute beginning t completes at t+60; its delayed fill is t+120.
        for timestamp in range(entry, maximum - MINUTE, MINUTE):
            row = minutes.get(timestamp)
            if row is None or row.volume == 0:
                return {
                    **event,
                    "status": "unpriced_trailing_observation_missing_or_zero_volume",
                }
            highest = max(highest, row.close)
            armed = armed or row.close >= opened.open * 1.01
            if row.close <= opened.open * 0.98 or (
                armed and row.close <= highest * 0.99
            ):
                trigger = timestamp + MINUTE
                exit_time = min(trigger + MINUTE, maximum)
                reason = (
                    "stop_close"
                    if row.close <= opened.open * 0.98
                    else "trailing_close"
                )
                break
    closed = minutes.get(exit_time)
    event.update(exit=exit_time, trigger_close=trigger, exit_reason=reason)
    if closed is None or closed.volume == 0:
        return {**event, "status": "unpriced_exit_missing_or_zero_volume"}
    gross = closed.open / opened.open - 1
    return {**event, "status": "priced", "gross": gross, "net": _net(gross, 100)}


def _events(  # noqa: PLR0913
    data: dict,
    bars: dict,
    symbols: list[str],
    policy: dict,
    start: int,
    end: int,
    delay: int,
) -> tuple[list[dict], dict]:
    events = []
    coverage = {}
    for symbol in symbols:
        counts: Counter = Counter()
        busy_until = start
        for timestamp in sorted(bars[symbol]):
            if not start <= timestamp < end:
                continue
            counts["complete_signal_bars"] += 1
            active = _signal(bars[symbol], bars["SOLUSDT"], timestamp, policy["entry"])
            if active is None:
                counts["feature_unavailable_bars"] += 1
                continue
            if not active:
                continue
            counts["signals"] += 1
            known = timestamp + BAR
            if known < busy_until:
                counts["overlap_skipped"] += 1
                continue
            entry = known + delay
            maximum = entry + (15 if policy["exit"] == "hold_15m" else 60) * MINUTE
            if maximum + MINUTE > end:
                events.append(
                    {
                        "symbol": symbol,
                        "signal_close": known,
                        "entry": entry,
                        "max_exit": maximum,
                        "status": "purged_boundary",
                    }
                )
                counts["purged_boundary"] += 1
                continue
            event = _mark(data[symbol], entry, policy["exit"])
            event.update(symbol=symbol, signal_close=known)
            events.append(event)
            busy_until = event["exit"] if event["status"] == "priced" else maximum
        counts["missing_or_incomplete_signal_bars"] = (end - start) // BAR - counts[
            "complete_signal_bars"
        ]
        coverage[symbol] = dict(counts)
    return events, coverage


def _stats(events: list[dict]) -> dict:
    attempted = [e for e in events if e["status"] != "purged_boundary"]
    priced = [e for e in attempted if e["status"] == "priced"]
    result = {
        "attempts": len(attempted),
        "priced": len(priced),
        "unpriced": len(attempted) - len(priced),
        "purged_boundary": len(events) - len(attempted),
        "unpriced_reasons": dict(
            Counter(e["status"] for e in attempted if e["status"] != "priced")
        ),
        "assets_priced": len({e["symbol"] for e in priced}),
        "days_priced": len({e["entry"] // DAY for e in priced}),
        "mean_primary_net_pct": None,
        "by_total_cost_bps": {},
    }
    for cost in COSTS:
        nets = [_net(e["gross"], cost) for e in priced]
        result["by_total_cost_bps"][str(cost)] = {
            "mean_net_pct": 100 * statistics.fmean(nets) if nets else None,
            "median_net_pct": 100 * statistics.median(nets) if nets else None,
            "win_rate_pct": 100 * sum(n > 0 for n in nets) / len(nets)
            if nets
            else None,
            "p05_net_pct": 100 * q(nets, 0.05) if nets else None,
            "p95_net_pct": 100 * q(nets, 0.95) if nets else None,
            "worst_net_pct": 100 * min(nets) if nets else None,
            "best_net_pct": 100 * max(nets) if nets else None,
        }
    result["mean_primary_net_pct"] = result["by_total_cost_bps"]["100"]["mean_net_pct"]
    return result


def _details(events: list[dict], start: int, end: int, symbols: list[str]) -> dict:
    priced = [e for e in events if e["status"] == "priced"]
    totals: dict[int, float] = defaultdict(float)
    for event in priced:
        totals[event["entry"] // DAY] += event["net"]
    best_day = max(sorted(totals), key=totals.get) if totals else None
    without = [e for e in events if e["entry"] // DAY != best_day]
    return {
        **_stats(events),
        "day_block_95pct_mean_primary_net_interval": _day_interval(events, start, end),
        "interval_method": "2000 deterministic UTC-day-block bootstrap draws, seed1703; includes zero-event days; event-weighted mean; percent units",
        "best_day_utc": datetime.fromtimestamp(best_day * DAY, UTC).date().isoformat()
        if best_day is not None
        else None,
        "without_best_day": _stats(without),
        "per_asset": {
            symbol: _stats([e for e in events if e["symbol"] == symbol])
            for symbol in symbols
        },
        "per_day_utc": {
            datetime.fromtimestamp(day, UTC).date().isoformat(): _stats(
                [e for e in events if e["entry"] // DAY == day // DAY]
            )
            for day in range(start, end, DAY)
        },
    }


def _selection(cells: list[dict]) -> dict | None:
    eligible = [cell for cell in cells if cell["summary"]["priced"] >= 30]
    return (
        max(eligible, key=lambda cell: cell["summary"]["mean_primary_net_pct"])[
            "policy"
        ]
        if eligible
        else None
    )


def _base(manifest: dict) -> dict:
    return {
        "manifest_sha256": MANIFEST_SHA256,
        "code_sha256": _code_hashes(),
        "protocol": manifest["protocol"],
        "assumptions": ASSUMPTIONS,
        "limits": LIMITS,
        "missing_archive_coverage": manifest["failures"],
        "source_integrity": "Exact CSV SHA256 and 12-column microsecond rows; local ZIP is repacked",
        "cost_model": "Symmetric total cost bps: (1+gross)*(1-cost/20000)/(1+cost/20000)-1",
        "native_costs": "not_established",
        "deployment_gate": "not_established",
        "live_ready": False,
    }


def _train(path: Path, manifest: dict) -> dict:
    month = manifest["protocol"]["training_month"]
    data, evidence = _load(path, manifest, (month,))
    bars = {symbol: _bars(minutes) for symbol, minutes in data.items()}
    start, end = _month_bounds(month)
    cells = []
    for entry in ENTRIES:
        for exit_rule in EXITS:
            policy = {"entry": entry, "exit": exit_rule}
            events, coverage = _events(
                data, bars, manifest["complete_symbols"], policy, start, end, 60
            )
            cells.append(
                {"policy": policy, "summary": _stats(events), "coverage": coverage}
            )
    selected = _selection(cells)
    report = {
        **_base(manifest),
        "stage": "train",
        "months_read": [month],
        "ordering": "Only May CSVs read; selection locked in this exclusive-create output before any June evaluation",
        "source_evidence": evidence,
        "training_cells": cells,
        "selected_policy": selected,
        "selection_status": "locked"
        if selected is not None
        else "none_eligible_minimum_30_priced",
    }
    report["lock_sha256"] = _digest(report)
    return report


def _lock(path: Path, manifest: dict) -> dict:
    report = json.loads(path.read_bytes())
    lock = report.pop("lock_sha256", None)
    if lock != _digest(report):
        message = "Training lock digest mismatch"
        raise ValueError(message)
    expected_pairs = [
        {"entry": entry, "exit": exit_rule} for entry in ENTRIES for exit_rule in EXITS
    ]
    cells = report.get("training_cells", [])
    if (
        report.get("stage") != "train"
        or report.get("manifest_sha256") != MANIFEST_SHA256
        or report.get("protocol") != manifest["protocol"]
        or report.get("code_sha256") != _code_hashes()
        or report.get("months_read") != [manifest["protocol"]["training_month"]]
        or [cell["policy"] for cell in cells] != expected_pairs
        or report.get("selected_policy") != _selection(cells)
    ):
        message = (
            "Training lock protocol, code, stage, source month, or selection mismatch"
        )
        raise ValueError(message)
    report["lock_sha256"] = lock
    return report


def _validate(path: Path, manifest: dict, training: dict) -> dict:
    # Lock is verified by the caller BEFORE this function can open any June ZIP.
    report = {
        **_base(manifest),
        "stage": "validate",
        "training_lock_sha256": training["lock_sha256"],
        "selected_policy": training["selected_policy"],
        "ordering": "Training lock verified before June read; only the locked pair is evaluated, including if May mean was negative",
    }
    if training["selected_policy"] is None:
        return {
            **report,
            "status": "no_eligible_training_policy",
            "months_read": [],
            "economic_screen": {"passed": False, "reason": "no_locked_pair"},
        }
    selected_cell = next(
        cell
        for cell in training["training_cells"]
        if cell["policy"] == training["selected_policy"]
    )
    report["locked_training_summary"] = selected_cell["summary"]
    report["training_edge_label"] = (
        "positive_at_100bps"
        if selected_cell["summary"]["mean_primary_net_pct"] > 0
        else "nonpositive_at_100bps; precommitted validation still performed"
    )
    months = (
        manifest["protocol"]["training_month"],
        manifest["protocol"]["untouched_validation_month"],
    )
    # Recheck May source evidence before opening any heldout archive.
    data, warmup = _load(path, manifest, (months[0],))
    if warmup != training["source_evidence"]:
        message = "May CSV evidence differs from locked training sources"
        raise ValueError(message)
    heldout, evidence = _load(path, manifest, (months[1],))
    for symbol, minutes in heldout.items():
        data[symbol].update(minutes)
    bars = {symbol: _bars(minutes) for symbol, minutes in data.items()}
    start, end = _month_bounds(months[1])
    events, coverage = _events(
        data,
        bars,
        manifest["complete_symbols"],
        training["selected_policy"],
        start,
        end,
        60,
    )
    delayed, delayed_coverage = _events(
        data,
        bars,
        manifest["complete_symbols"],
        training["selected_policy"],
        start,
        end,
        120,
    )
    details = _details(events, start, end, manifest["complete_symbols"])
    sensitivity = _details(delayed, start, end, manifest["complete_symbols"])
    interval = details["day_block_95pct_mean_primary_net_interval"]
    without = details["without_best_day"]["mean_primary_net_pct"]
    delay_mean = sensitivity["mean_primary_net_pct"]
    checks = {
        "at_least_100_priced": details["priced"] >= 100,
        "at_least_3_assets": details["assets_priced"] >= 3,
        "positive_day_block_lower_bound": interval is not None and interval[0] > 0,
        "positive_without_best_day": without is not None and without > 0,
        "positive_120_second_delay": delay_mean is not None and delay_mean > 0,
    }
    return {
        **report,
        "status": "locked_pair_evaluated",
        "months_read": list(months),
        "source_evidence": warmup + evidence,
        "warmup": "May features only; June signal bars/outcomes only",
        "primary_60_second_delay": details,
        "coverage": coverage,
        "events": events,
        "sensitivity_120_second_delay": sensitivity,
        "sensitivity_coverage": delayed_coverage,
        "sensitivity_events": delayed,
        "economic_screen": {
            "checks_at_100bps": checks,
            "historical_numeric_checks_pass": all(checks.values()),
            "observed_native_costs_required": True,
            "observed_native_costs_established": False,
            "deployment_established": False,
            "passed": False,
            "conclusion": "CEX data cannot establish native-cost or deployment gates, regardless of historical screen results",
        },
    }


def _self_check() -> None:  # noqa: PLR0915
    # One synthetic path defends causality, quote flow, latency and unknown marks.
    flat = Candle(100, 101, 99, 100, 1, 100, 60)
    bars = {i * BAR: flat for i in range(110)}
    signal = 100 * BAR
    bars[signal] = Candle(100, 105, 99, 104.5, 1, 300, 180)
    sol = {i * BAR: flat for i in range(110)}
    assert _signal(bars, sol, signal, "flow_breakout") is True
    changed = dict(bars)
    changed[signal + BAR] = Candle(1000, 9999, 1, 3000, 999, 999999, 999999)
    assert _signal(changed, sol, signal, "flow_breakout") is True
    changed[signal] = bars[signal]._replace(buy_quote=150)
    assert _signal(changed, sol, signal, "flow_breakout") is False
    reclaimed = dict(bars)
    reclaimed[signal - BAR] = Candle(100, 101, 96, 97, 1, 300, 120)
    reclaimed[signal] = Candle(97, 101, 95, 100, 1, 100, 60)
    assert _signal(reclaimed, sol, signal, "flow_reclaim") is True
    reclaimed[signal] = Candle(98, 101, 97, 100.5, 1, 300, 180)
    assert _signal(reclaimed, sol, signal, "support_reclaim") is False
    reclaimed[signal] = reclaimed[signal]._replace(low=95)
    assert _signal(reclaimed, sol, signal, "support_reclaim") is True
    minutes = dict.fromkeys(range(0, 70 * MINUTE, MINUTE), flat)
    minutes[0] = Candle(100, 150, 50, 100, 1, 100, 60)
    assert _mark(minutes, 0, "close_trailing")["exit"] == 3600
    minutes[0] = minutes[0]._replace(close=98)
    minutes[120] = flat._replace(open=90, low=89)
    stopped = _mark(minutes, 0, "close_trailing")
    assert stopped["trigger_close"] == 60 and stopped["exit"] == 120
    assert math.isclose(stopped["gross"], -0.1)
    minutes[120] = minutes[120]._replace(volume=0)
    assert (
        _mark(minutes, 0, "close_trailing")["status"]
        == "unpriced_exit_missing_or_zero_volume"
    )
    minutes[0] = flat._replace(close=102, high=103)
    minutes[60] = flat._replace(close=100.9)
    minutes[120] = flat
    assert _mark(minutes, 0, "close_trailing")["exit"] == 180
    minutes[60] = flat._replace(volume=0)
    assert _mark(minutes, 0, "close_trailing")["status"].startswith("unpriced_trailing")
    minutes[0] = flat._replace(volume=0)
    assert _mark(minutes, 0, "hold_15m")["status"].startswith("unpriced_entry")
    entry = signal + BAR + 60
    marks = dict.fromkeys(range(entry, entry + 4000, MINUTE), flat)
    marks[entry] = flat._replace(open=80, low=79)
    policy = {"entry": "flow_breakout", "exit": "hold_15m"}
    data = {"TEST": marks}
    tables = {"TEST": bars, "SOLUSDT": sol}
    events, _ = _events(data, tables, ["TEST"], policy, signal, signal + 5 * BAR, 60)
    assert events[0]["entry"] == entry and math.isclose(events[0]["gross"], 0.25)
    delayed, _ = _events(data, tables, ["TEST"], policy, signal, signal + 5 * BAR, 120)
    assert delayed[0]["entry"] == entry + 60 and delayed[0]["gross"] == 0
    purged, _ = _events({"TEST": {}}, tables, ["TEST"], policy, signal, entry + 900, 60)
    assert purged[0]["status"] == "purged_boundary"
    minute_bars = dict.fromkeys(range(0, BAR, MINUTE), flat)
    assert _bars(minute_bars)[0].quote == 1500
    del minute_bars[60]
    assert _bars(minute_bars) == {}
    print(
        "Self-check passed: causal quote-flow signals, completed bars, delayed close-only exits, unpriced marks and boundary purge"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stage", choices=("train", "validate"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--policy",
        type=Path,
        help="Previously written TRAIN JSON; verified before any June archive is opened",
    )
    parser.add_argument(
        "--out", type=Path, help="New JSON path; existing files are never overwritten"
    )
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        if any((args.stage, args.manifest, args.policy, args.out)):
            parser.error("--self-check is standalone")
        _self_check()
        return
    if args.stage is None or args.manifest is None or args.out is None:
        parser.error("--stage, --manifest and --out are required")
    if (args.stage == "validate") != (args.policy is not None):
        parser.error(
            "--policy is required only for validate; training must be separate"
        )
    if args.out.exists():
        parser.error(
            "--out already exists; preserve locked evidence and use a new path"
        )
    try:
        manifest = _manifest(args.manifest)
        if args.stage == "train":
            report = _train(args.manifest, manifest)
        else:
            training = _lock(args.policy, manifest)
            report = _validate(args.manifest, manifest, training)
        with args.out.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Research failed: {error}\n")
    print(
        f"Saved {args.stage}: {args.out}; selected={report['selected_policy']}; live_ready=False"
    )


if __name__ == "__main__":
    main()
