"""Winner-sell exit-timer event study on the curve tape (Wave A4).

WHAT IT DOES
    Reuses the A1 machinery: per-wallet average-cost realized PnL with a
    strictly-prior running total. Then measures FORWARD price paths after
    sell trades:
      1. after SELLs by wallets whose strictly-prior realized PnL clears the
         half-1 winner threshold (walk-forward),
      2. after ALL other SELLs (baseline),
      3. after BUYs (the A1 complement),
    at horizons of 10/25/60 slots from the trade mark, with medians and
    winsorized means (A2's dust-tail lesson is baked in).

WHY WE NEED IT
    A1 closed entry-following: winner buys mark local tops. The profits
    still exist (top-100 wallets earn 30.4% of positive realized PnL); the
    untested half of the wallet signal is exit-side — if winner SELLS time
    the dumps, an exit overlay on held positions is the deployable use,
    including for the live paper bot's rollover exits.

HONEST LIMITS (read before citing)
    - Single-day tape (multi-tape rule: no graduation claim).
    - Forward prices are the next observed trade marks, not book quotes.
    - The event windows overlap within coins; the bootstrap clusters by coin.

Run: uv run learning-examples/token-lifecycles/analyze_exit_signals.py \
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
HOLDS = (10, 25, 60)
WINSOR_GROSS = 9.0  # cap single-window gross return at +900%
MIN_POINTS = 500


def implied_price(trade: list) -> float:
    sol, tok = trade[3], trade[4]
    if not tok or not sol:
        return 0.0
    return (sol / LAMPORTS) / (tok / 1e6)


def coin_paths(coins: list[dict]) -> dict[str, list[tuple[int, float]]]:
    """Per coin: slot-ordered (slot, implied price) marks."""
    out: dict[str, list[tuple[int, float]]] = {}
    for coin in coins:
        create = coin.get("create_slot") or 0
        marks = [
            (create + t[0], implied_price(t))
            for t in coin.get("trades", [])
            if implied_price(t)
        ]
        if marks:
            out[coin.get("mint")] = marks
    return out


def forward_return(marks: list[tuple[int, float]], slot: int, hold: int) -> float | None:
    """Gross price return from the mark at/before `slot` to the first mark
    at/after `slot+hold` (dead coins mark at the last observed trade)."""
    base = next((p for s, p in reversed(marks) if s <= slot), None)
    if base is None:
        return None
    fwd = next((p for s, p in marks if s >= slot + hold), None)
    if fwd is None:
        fwd = marks[-1][1]
    return fwd / base - 1.0


def robust(vals: list[float]) -> str:
    capped = [min(max(v, -0.95), WINSOR_GROSS) for v in vals]
    return (
        f"n={len(vals):>7d} median={statistics.median(vals):+.4%} "
        f"win-mean={statistics.mean(capped):+.4%} "
        f"win-rate={sum(1 for v in vals if v > 0) / len(vals):.0%}"
    )


def bootstrap_ci(vals: list[float], coin_of: list[str]) -> tuple[float, float]:
    by_coin: dict[str, list[float]] = defaultdict(list)
    for v, c in zip(vals, coin_of, strict=True):
        by_coin[c].append(v)
    means = [statistics.mean(v) for v in by_coin.values()]
    rng = random.Random(1729)  # noqa: S311 - bootstrap, not crypto
    boots = []
    for _ in range(1000):
        sample = [means[rng.randrange(len(means))] for _ in means]
        boots.append(statistics.median(sample))
    boots.sort()
    return boots[25], boots[975]


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    args = ap.parse_args()

    coins = [json.loads(line) for line in args.path.read_text().splitlines() if line.strip()]
    paths = coin_paths(coins)
    all_slots = [s for marks in paths.values() for s, _ in marks]
    midpoint = statistics.median(all_slots)
    print(f"coins: {len(coins)} | slot midpoint: {midpoint:.0f}")

    # Average-cost realized PnL with strictly-prior running totals per trade.
    basis: dict[tuple, dict] = {}
    running: dict[str, float] = defaultdict(float)
    events: list[dict] = []
    for coin in coins:
        create = coin.get("create_slot") or 0
        creator = coin.get("creator") or ""
        mint = coin.get("mint")
        for t in coin.get("trades", []):
            user, is_buy = t[1], t[2]
            sol, tok = t[3] / LAMPORTS, t[4] / 1e6
            slot = create + t[0]
            price = implied_price(t)
            if not price:
                continue
            prior = running[user]
            events.append(
                {"user": user, "mint": mint, "slot": slot, "is_buy": is_buy,
                 "prior": prior, "creator": user == creator}
            )
            st = basis.setdefault((user, mint), {"tok": 0.0, "cost": 0.0})
            if is_buy:
                st["tok"] += tok
                st["cost"] += sol
            elif st["tok"] > 0:
                avg = st["cost"] / st["tok"]
                sold = min(tok, st["tok"])
                running[user] += sol - avg * sold
                st["tok"] -= sold
                st["cost"] -= avg * sold

    # Walk-forward: winner threshold from half-1 PnL at half-1's end.
    h1_events = [e for e in events if e["slot"] < midpoint]
    pnl_h1: dict[str, float] = defaultdict(float)
    for e in h1_events:
        pnl_h1[e["user"]] = e["prior"]
    winners_h1 = sorted(v for v in pnl_h1.values() if v > 0)
    threshold = winners_h1[-max(1, len(winners_h1) // 10)] if winners_h1 else 0.0
    print(f"h1 winner threshold (top decile of positive h1 PnL): {threshold:.3f} SOL")

    h2 = [e for e in events if e["slot"] >= midpoint]
    groups: dict[str, tuple[list[float], list[str]]] = defaultdict(lambda: ([], []))
    for e in h2:
        marks = paths.get(e["mint"])
        if not marks:
            continue
        kind = (
            "winner_sell" if not e["is_buy"] and e["prior"] >= threshold
            else "winner_buy" if e["is_buy"] and e["prior"] >= threshold
            else "other_sell" if not e["is_buy"]
            else "other_buy"
        )
        for hold in HOLDS:
            r = forward_return(marks, e["slot"], hold)
            if r is not None:
                vals, tags = groups[(kind, hold)]
                vals.append(r)
                tags.append(e["mint"])

    print("\n== forward gross returns from the event mark (walk-forward, h2) ==")
    for hold in HOLDS:
        print(f"  -- horizon {hold} slots --")
        for kind in ("winner_sell", "other_sell", "winner_buy", "other_buy"):
            vals, tags = groups.get((kind, hold), ([], []))
            if len(vals) < MIN_POINTS:
                print(f"  {kind:12s} n<min")
                continue
            line = robust(vals)
            extra = ""
            if kind == "winner_sell":
                lo, hi = bootstrap_ci(vals, tags)
                extra = f"  coin-clustered median CI=[{lo:+.2%}, {hi:+.2%}]"
            print(f"  {kind:12s} {line}{extra}")

    print(
        "\nPositive forward return after winner SELLS would make 'sell when "
        "winners sell' an exit-timer candidate; here forward-return sign "
        "convention is: positive = price RISES after the event (bad for the "
        "exit-timer hypothesis if winner_sell > other_sell)."
    )
    print("Single tape; marks not quotes; medians primary.")


if __name__ == "__main__":
    main()
