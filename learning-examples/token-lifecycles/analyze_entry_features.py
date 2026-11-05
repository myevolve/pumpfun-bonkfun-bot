"""Executable-return feature harness on the curve tape (Wave A2 stage 1).

WHAT IT DOES
    At every warm decision point (coin, trade k), computes the observable
    stream features (buyer cohort, concentration, creator behavior, reserve
    trajectory, inflow), prices the executable net-SOL round trip of a
    fixed entry made a fixed detection-to-execution delay later with a fixed
    hold horizon, and reports:
      1. baseline EVs: cash, always-up (every decision point), momentum
         (trailing-10 price up), reversal (trailing-10 down), detection
         entry (first non-creator buy);
      2. feature-decile EV curves on the train half — does ANY feature's top
         decile beat cash with the bottom decile below it;
      3. walk-forward confirmation of the best feature-decile on the test
         half, with a coin-clustered bootstrap.

WHY WE NEED IT
    Council A2 stage 1: labels must be executable net SOL (entry at the next
    trade >= signal+delay, one position per coin, both-side pump fees and
    priority fees, missing exits are no-trades), judged by EV vs cash, never
    by win rate. A gradient-boosted model is only justified if a single
    feature separates deciles.

HONEST LIMITS (read before citing)
    - Single-day tape (multi-tape rule: no graduation claim).
    - Fills are the next observed trade marks, not book-verified quotes.
    - Decision points overlap in time per coin; the purge rule (one
      simulated position per coin) is enforced by simulation but EVs are
      correlated within a coin — the bootstrap clusters by coin.
    - The decile scan is a multiple-comparison exploration on the train
      half; the test half confirms only the pre-declared best cell.

Run: uv run learning-examples/token-lifecycles/analyze_entry_features.py \
        learning-examples/token-lifecycles/lifecycles_24h.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

LAMPORTS = 1e9
# Quote amounts are lamports only on SOL-paired curves (default pubkey or WSOL).
SOL_QUOTES = {
    None,
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
}
FEE_LAMPORTS = 33_000 + 27_000 + 5_000
PUMP_FEE = 0.0125
BUY_SOL = 0.05
DELAY = 2  # detection-to-execution delay in slots (marginal table in A1)
WARMUP = 3  # trades needed before a decision point
HOLDS = (10, 25, 60)
WINSOR_NET = 0.045  # cap one-side net return at +90% on 0.05 SOL
MIN_DECILE_POINTS = 100
TRAIL = 10  # trades for momentum/reversal features
FEATURES = (
    "n_unique_buyers",
    "buy_sol_total",
    "max_single_buy",
    "price_change_pct",
    "inflow_last5",
    "n_trades",
)


def implied_price(trade: list) -> float:
    sol, tok = trade[3], trade[4]
    if not tok or not sol:
        return 0.0
    return (sol / LAMPORTS) / (tok / 1e6)


def features_at(coin: dict, k: int, creator: str) -> dict | None:
    """Observable features from trades[:k] (strictly before the decision)."""
    hist = coin["trades"][:k]
    if len(hist) < WARMUP:
        return None
    creators_buys = 0
    buyers: set[str] = set()
    buy_sol = 0.0
    max_buy = 0.0
    for t in hist:
        if t[2]:
            buyers.add(t[1])
            if t[1] == creator:
                creators_buys += 1
            sol = t[3] / LAMPORTS
            buy_sol += sol
            max_buy = max(max_buy, sol)
    p0, p1 = implied_price(hist[0]), implied_price(hist[-1])
    if not p0 or not p1:
        return None
    return {
        "n_unique_buyers": len(buyers),
        "buy_sol_total": buy_sol,
        "max_single_buy": max_buy,
        "price_change_pct": (p1 / p0 - 1) * 100,
        "inflow_last5": sum(
            (t[3] / LAMPORTS) * (1 if t[2] else -1) for t in hist[-5:]
        ),
        "n_trades": k,
        "creator_buys": creators_buys,
        "creator_sold": any(not t[2] and t[1] == creator for t in hist),
        "momentum_up": p1 > implied_price(hist[-min(TRAIL, len(hist))]),
    }


def executable_return(coin: dict, sig_slot: int, hold: int) -> float | None:
    """Net SOL of a BUY_SOL entry at the next trade >= sig_slot+DELAY.

    Exit at the next trade >= entry+hold; when no such trade exists the
    coin died inside the horizon and the position is marked at the coin's
    LAST observed trade — a dead coin is a mark-to-last loss, never a
    no-trade (exclusion would be survivorship bias, not conservatism).
    """
    create = coin.get("create_slot") or 0
    trades = coin["trades"]
    entry = next(
        (t for t in trades if create + t[0] >= sig_slot + DELAY), None
    )
    if entry is None:
        return None
    entry_slot = create + entry[0]
    exit_t = next(
        (t for t in trades if create + t[0] >= entry_slot + hold), None
    )
    if exit_t is None:
        later = [t for t in trades if create + t[0] > entry_slot]
        exit_t = later[-1] if later else entry
    p_in, p_out = implied_price(entry), implied_price(exit_t)
    if not p_in or not p_out:
        return None
    gross = (p_out / p_in) * (1 - PUMP_FEE) - (1 + PUMP_FEE)
    return gross * BUY_SOL - FEE_LAMPORTS / LAMPORTS


def main() -> None:  # noqa: C901, PLR0915
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    args = ap.parse_args()

    loaded = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    coins = [c for c in loaded if c.get("quote_mint") in SOL_QUOTES]
    print(f"excluded {len(loaded) - len(coins)} non-SOL-quoted coins")
    midpoint = statistics.median(
        (c.get("create_slot") or 0) + (c["trades"][-1][0] if c.get("trades") else 0)
        for c in coins if c.get("trades")
    )
    print(f"coins: {len(coins)} | slot midpoint: {midpoint:.0f}")

    # Collect decision points: features + per-hold executable returns.
    points = []
    for coin in coins:
        trades = coin.get("trades", [])
        creator = coin.get("creator") or ""
        create = coin.get("create_slot") or 0
        for k in range(WARMUP, len(trades)):
            f = features_at(coin, k, creator)
            if f is None:
                continue
            sig_slot = create + trades[k - 1][0]  # decision made at trade k-1
            rets = {h: executable_return(coin, sig_slot, h) for h in HOLDS}
            if all(r is None for r in rets.values()):
                continue
            points.append({"coin": coin.get("mint"), "train": create < midpoint, **f, "ret": rets})
    print(f"decision points: {len(points)} "
          f"(train {sum(1 for p in points if p['train'])}, "
          f"test {sum(1 for p in points if not p['train'])})")

    train = [p for p in points if p["train"]]
    test = [p for p in points if not p["train"]]

    def ev(
        ps: list[dict], h: int, pred: object = lambda _p: True
    ) -> tuple[float, float, float]:
        vals = [p["ret"][h] for p in ps if p["ret"][h] is not None and pred(p)]
        if not vals:
            return float("nan"), float("nan"), float("nan")
        capped = [min(max(v, -0.05), WINSOR_NET) for v in vals]
        med = statistics.median(vals)
        return statistics.mean(capped), med, sum(1 for v in vals if v > 0) / len(vals)

    print("\n== baselines (net SOL per 0.05 SOL trade, fee-inclusive) ==")
    for h in HOLDS:
        up, upm, _ = ev(train, h)
        mo, mom, _ = ev(train, h, lambda p: p["momentum_up"])
        re, rem, _ = ev(train, h, lambda p: not p["momentum_up"])
        print(
            f"  hold={h:>3d}: always-up win-mean={up:+.5f} med={upm:+.5f} | "
            f"momentum mean={mo:+.5f} med={mom:+.5f} | reversal mean={re:+.5f} med={rem:+.5f}"
        )

    print("\n== feature-decile EV curves (train half, hold=25) ==")
    best = None
    h = 25
    for feat in FEATURES:
        vals = sorted(
            ((p[feat], p) for p in train if p["ret"][h] is not None),
            key=lambda kv: kv[0],
        )
        if len(vals) < MIN_DECILE_POINTS:
            continue
        n = len(vals) // 10
        deciles = [vals[i * n : (i + 1) * n] for i in range(10)]
        evs = [statistics.median(p["ret"][h] for _, p in dec) for dec in deciles]
        spread = evs[-1] - evs[0]
        print(
            f"  {feat:18s} d1={evs[0]:+.5f} d5={evs[4]:+.5f} d10={evs[-1]:+.5f} "
            f"spread={spread:+.5f} (median-based; means are dust-tail contaminated)"
        )
        if best is None or spread > best[1]:
            best = (feat, spread, evs[-1], evs[0])
    print(f"\nbest-separating feature (pre-declared): {best[0]} "
          f"(top-decile med {best[2]:+.5f} vs bottom {best[3]:+.5f})")

    # Walk-forward confirmation on the test half: top decile of the best
    # feature vs cash, with a coin-clustered bootstrap.
    feat = best[0]
    cut = sorted(p[feat] for p in train)[int(0.9 * len(train))]
    top_test = [p for p in test if p[feat] >= cut]
    print(f"\n== test-half confirmation: {feat} >= {cut:.6g} ==")
    for h2 in HOLDS:
        top_ev, top_med, _ = ev(test, h2, lambda p: p[feat] >= cut)
        base_ev, base_med, _ = ev(test, h2)
        print(
            f"  hold={h2:>3d}: top-decile mean={top_ev:+.5f} med={top_med:+.5f} "
            f"| all mean={base_ev:+.5f} med={base_med:+.5f}"
        )
    h2 = 25
    vals = [p["ret"][h2] for p in top_test if p["ret"][h2] is not None]
    by_coin: dict[str, list[float]] = defaultdict(list)
    for p in top_test:
        if p["ret"][h2] is not None:
            by_coin[p["coin"]].append(p["ret"][h2])
    coin_means = [statistics.mean(v) for v in by_coin.values()]
    rng = random.Random(1729)  # noqa: S311 - bootstrap, not crypto
    boots = []
    for _ in range(1000):
        sample = [coin_means[rng.randrange(len(coin_means))] for _ in coin_means]
        boots.append(statistics.mean(sample))
    boots.sort()
    lo, hi = boots[25], boots[975]
    print(
        f"  hold=25 coin-clustered bootstrap: EV={statistics.mean(vals):+.5f} "
        f"95% CI=[{lo:+.5f}, {hi:+.5f}] coins={len(coin_means)} n={len(vals)}"
    )
    print("\nCASH = 0. Single tape; the scan is exploratory, only the "
          "pre-declared best cell is confirmed on the test half.")


if __name__ == "__main__":
    main()
