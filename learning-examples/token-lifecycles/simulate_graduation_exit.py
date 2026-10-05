"""Whale-ride graduation exit: backtest mid-curve entries exited into the
graduation pool, on a recorded lifecycle tape.

Hypothesis (2026-10-05, first measured on lifecycles_24h.jsonl): when a whale
sweeps a curve to graduation, pump.fun seeds the PumpSwap pool at ~79% of the
curve's final marginal price -- far above mid-curve entry prices. Entering when
the curve crosses X SOL real and exiting into the pool at its opening captured
a positive held-out mean on the Sep 4-5 2026 tape where every curve-only exit
policy lost. The Jun-Jul corpus (pre manufactured-sweep era) does not contain
this pattern; treat any single-tape result as era-dependent until re-run on a
fresh recording.

Conventions follow summarize_lifecycles.py:
- PUMP_FEE 1.25% per side on the curve, 0.3% on the PumpSwap pool,
  TX_FEES_LAMPORTS covers buy + sell + cleanup.
- Entry: first curve trade whose post-state real_sol crosses X; our buy lands
  entry_lag slots later at that state (constant product). If the curve is
  graduated by then (the whale beat us) the entry is skipped: the buy would
  revert on a completed curve.
- Exit: graduated coins sell into the pool at pool_dslot + exit_lag using the
  pre-trade reserves of that state (0.3% fee). Non-graduated coins sell on the
  curve at cross_dslot + hold_slots. A post-buyout curve state is never
  priced as a curve sale: real tokens are zero there and sell_value on it is
  the phantom that inflated earlier milestone backtests.

Usage:
    uv run learning-examples/token-lifecycles/simulate_graduation_exit.py \
        [--tape learning-examples/token-lifecycles/lifecycles_24h.jsonl]

Never moves funds; tape-model results are not fills.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import statistics
from pathlib import Path

sys_path = str(Path(__file__).parent)
import sys  # noqa: E402

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from summarize_lifecycles import PUMP_FEE, TX_FEES_LAMPORTS, sell_value  # noqa: E402

BUY_LAMPORTS = 10_000_000
POOL_FEE = 0.003
COST = BUY_LAMPORTS + TX_FEES_LAMPORTS
MIN_COHORT = 30
MIN_HOUR_SAMPLE = 10


@dataclasses.dataclass(frozen=True, slots=True)
class RideCfg:
    """Whale-ride policy knobs."""

    hold_slots: int
    entry_lag: int = 1
    exit_lag: int = 2


def _tokens_bought(v_sol: int, v_tok: int) -> int:
    spend_net = BUY_LAMPORTS * (1 - PUMP_FEE)
    return v_tok - (v_sol * v_tok) / (v_sol + spend_net)


def _pool_state_at(
    posts: list, pool_dslot: int, exit_lag: int
) -> tuple[int, int] | None:
    """(base, effective_quote) of the pre-trade pool state at dslot <= pool+lag."""
    state = None
    for p in posts:
        if p[0] <= pool_dslot + exit_lag:
            state = p
        else:
            break
    base = posts[0][5] if state is None else state[5]
    quote = (
        posts[0][6] + (posts[0][7] or 0)
        if state is None
        else state[6] + (state[7] or 0)
    )
    if base <= 0 or quote <= 0:
        return None
    return base, quote


def _land_index(trades: list, idx: int, entry_lag: int) -> int:
    land = idx
    for j in range(idx + 1, len(trades)):
        if trades[j][0] <= trades[idx][0] + entry_lag:
            land = j
        else:
            break
    return land


def _graduated_exit(
    tokens: int, posts: list, pool_dslot: int, exit_lag: int
) -> float | None:
    """Sell into the pool; a dead pool is a total loss."""
    state = _pool_state_at(posts, pool_dslot, exit_lag)
    if state is None:
        return -COST
    base, eff = state
    return eff * tokens / (base + tokens) * (1 - POOL_FEE) - COST


def _curve_exit(
    trades: list, tokens: int, land_i: int, cross_dslot: int, hold_slots: int
) -> float | None:
    state = trades[land_i]
    for j in range(land_i + 1, len(trades)):
        if trades[j][0] <= cross_dslot + hold_slots:
            state = trades[j]
        else:
            break
    if state[7] <= 0 or state[6] <= 0:
        return None
    return sell_value(state[6], state[7], tokens) - COST


def simulate_coin(coin: dict, x_sol: float, cfg: RideCfg) -> float | None:
    """Net lamports for one coin under the whale-ride policy, or None to skip."""
    return _simulate(
        coin["trades"],
        x_sol,
        cfg,
        coin.get("post_trades") or [],
        coin.get("pool_dslot"),
    )


def _simulate(
    trades: list, x_sol: float, cfg: RideCfg, posts: list | None, pool_dslot: int | None
) -> float | None:
    """Net lamports for one coin under the whale-ride policy, or None to skip."""
    n = len(trades)
    idx = next((i for i, t in enumerate(trades) if t[5] >= x_sol * 1e9), None)
    if idx is None or idx == n - 1:
        return None  # no crossing, or crossing IS the completing trade
    grad = pool_dslot is not None and posts
    land_i = _land_index(trades, idx, cfg.entry_lag)
    if grad and land_i == n - 1:
        return None  # graduation beat our buy: reverting tx, no trade
    v_sol, v_tok = trades[land_i][6], trades[land_i][7]
    if v_tok <= 0 or v_sol <= 0:
        return None
    tokens = _tokens_bought(v_sol, v_tok)
    if grad:
        return _graduated_exit(tokens, posts, pool_dslot, cfg.exit_lag)
    return _curve_exit(trades, tokens, land_i, trades[idx][0], cfg.hold_slots)


def _pctl(values: list[float], q: float) -> float:
    xs = sorted(values)
    return xs[min(int(q * len(xs)), len(xs) - 1)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tape", default="learning-examples/token-lifecycles/lifecycles_24h.jsonl"
    )
    ap.add_argument("--entry-lag", type=int, default=1)
    ap.add_argument("--exit-lag", type=int, default=2)
    ap.add_argument("--xs", default="30,40,50,60")
    ap.add_argument("--holds", default="2,5,10")
    args = ap.parse_args()

    with Path(args.tape).open() as f:
        coins = [json.loads(line) for line in f]
    graduated = sum(1 for c in coins if c.get("pool_dslot") is not None)
    print(f"tape {args.tape}: {len(coins)} coins, {graduated} graduated")
    ts_med = statistics.median(c["create_ts"] for c in coins)

    print(
        f"entry_lag={args.entry_lag} exit_lag={args.exit_lag}; costs: pump {PUMP_FEE:.2%}/side"
        f" + {TX_FEES_LAMPORTS} tx lamports, pool {POOL_FEE:.2%}"
    )
    print(
        f"{'x':>4} {'hold':>4} | {'train mean':>9} {'win':>6} {'n':>5} | "
        f"{'test mean':>9} {'win':>6} {'n':>5} | {'test p10':>8} {'p50':>7} {'p90':>8}"
    )
    for x_sol in (float(x) for x in args.xs.split(",")):
        for hold in (int(h) for h in args.holds.split(",")):
            train: list[float] = []
            test: list[float] = []
            for c in coins:
                pnl = simulate_coin(
                    c,
                    x_sol,
                    RideCfg(
                        hold_slots=hold,
                        entry_lag=args.entry_lag,
                        exit_lag=args.exit_lag,
                    ),
                )
                if pnl is None:
                    continue
                (train if c["create_ts"] <= ts_med else test).append(pnl)
            if len(train) < MIN_COHORT or len(test) < MIN_COHORT:
                continue
            wtr = sum(1 for p in train if p > 0) / len(train)
            wte = sum(1 for p in test if p > 0) / len(test)
            print(
                f"{x_sol:>4.0f} {hold:>4} | {statistics.mean(train) / 1e5:+9.1f}% {wtr:6.1%} {len(train):5} | "
                f"{statistics.mean(test) / 1e5:+9.1f}% {wte:6.1%} {len(test):5} | "
                f"{_pctl(test, 0.1) / 1e5:+8.1f}% {_pctl(test, 0.5) / 1e5:+7.1f}% {_pctl(test, 0.9) / 1e5:+8.1f}%"
            )

    x0, hold0 = 40.0, 5
    hourly: dict[int, list[float]] = {}
    for c in coins:
        pnl = simulate_coin(
            c,
            x0,
            RideCfg(hold_slots=hold0, entry_lag=args.entry_lag, exit_lag=args.exit_lag),
        )
        if pnl is None:
            continue
        hour = datetime.datetime.utcfromtimestamp(c["create_ts"]).hour
        hourly.setdefault(hour, []).append(pnl)
    if hourly:
        windows = [v for v in hourly.values() if len(v) >= MIN_HOUR_SAMPLE]
        pos = sum(1 for v in windows if statistics.mean(v) > 0)
        print(
            f"\nhours (x={x0:.0f} hold={hold0}): {pos}/{len(windows)} positive windows"
        )


if __name__ == "__main__":
    main()
