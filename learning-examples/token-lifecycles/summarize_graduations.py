"""Score the post-graduation tape recorded by record_lifecycles.py.

For every coin that graduated while recorded, the file carries the PumpSwap
trade tape from pool creation (``post_trades``). This script answers:

1. What does price do after graduation (path quantiles vs the pool-creation
   price), and where does the pool's SOL end up?
2. Who is acting: how concentrated was supply at graduation, how large was
   the buy in the pool-creation slot, and which wallets recur.
3. Is there any entry after graduation that is positive under a trailing
   stop / take-profit exit, with and without early-flow filters, split by
   time so an in-sample winner has to repeat.

Offline; no network.

    uv run learning-examples/token-lifecycles/summarize_graduations.py lifecycles_24h.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

LAMPORTS = 1e9
TOKEN_RAW = 1e6
PUMP_FEE = 0.0125  # PumpSwap lp + protocol + creator, each side (approx)
TX_FEES_LAMPORTS = 60_000
SUPPLY = 1_000_000_000 * TOKEN_RAW


def price(t: list) -> float:
    """SOL per whole token from effective reserves (pool quote + virtual quote)."""
    return (t[6] + t[7]) / LAMPORTS / (t[5] / TOKEN_RAW)


def q(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))] if s else float("nan")


def simulate(
    c: dict,
    entry_slots: int,
    *,
    trail: float,
    take_profit: float | None,
    hold: int,
    buy: int,
) -> float | None:
    """Net PnL (lamports) buying `buy` at grad+entry_slots, exiting by rule."""
    pt = c["post_trades"]
    g0 = pt[0][0]
    i = next((k for k, t in enumerate(pt) if t[0] >= g0 + entry_slots), None)
    if i is None or i == len(pt) - 1:
        return None
    st = pt[i]
    base, quote = st[5], st[6] + st[7]
    tokens = base - (base * quote) / (quote + buy * (1 - PUMP_FEE))
    cost = buy + TX_FEES_LAMPORTS

    def value(t: list) -> float:
        b, qq = t[5], t[6] + t[7]
        return (qq - (qq * b) / (b + tokens)) * (1 - PUMP_FEE)

    peak = value(st)
    land = None
    for t in pt[i + 1 :]:
        v = value(t)
        peak = max(peak, v)
        if (
            v <= peak * (1 - trail)
            or (take_profit is not None and v >= cost * (1 + take_profit))
            or t[0] - st[0] >= hold
        ):
            land = t[0] + 2
            break
    state = st
    for t in pt[i + 1 :]:
        if land is None or t[0] <= land:
            state = t
        else:
            break
    return value(state) - cost


def early_flow(c: dict, window_slots: int) -> dict | None:
    pt = c["post_trades"]
    g0 = pt[0][0]
    w = [t for t in pt if t[0] < g0 + window_slots]
    if not w:
        return None
    sol_in = sum(t[3] for t in w if t[2]) / LAMPORTS
    sol_out = sum(t[3] for t in w if not t[2]) / LAMPORTS
    return {
        "buyers": len({t[1] for t in w if t[2]}),
        "buy_ratio": sol_in / max(sol_in + sol_out, 1e-9),
    }


def supply_concentration(c: dict) -> float:
    hold: Counter = Counter()
    for t in c["trades"]:
        if t[1]:
            hold[t[1]] += t[4] if t[2] else -t[4]
    return sum(v for _, v in hold.most_common(3)) / SUPPLY


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--buy-sol", type=float, default=0.1)
    args = ap.parse_args()
    rows = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    grads = [
        c for c in rows if c.get("graduated_dslot") is not None and c.get("post_trades")
    ]
    grads.sort(key=lambda c: c["create_slot"])
    if not grads:
        sys.exit("no graduated coins with a pool tape in this file")
    print(
        f"coins={len(rows)} graduated_with_tape={len(grads)} "
        f"median_post_trades={statistics.median(len(c['post_trades']) for c in grads):.0f}"
    )

    marks = (0, 25, 75, 150, 300, 750, 1500, 3000, 4500)
    paths = []
    for c in grads:
        pt = c["post_trades"]
        g0, p0 = pt[0][0], price(pt[0])
        row = {}
        for m in marks:
            last = None
            for t in pt:
                if t[0] <= g0 + m:
                    last = t
                else:
                    break
            row[m] = price(last) / p0 if last else None
        row["peak"] = max(price(t) for t in pt) / p0
        row["end"] = price(pt[-1]) / p0
        paths.append(row)
    print("\nPrice vs pool-creation price, by slots after graduation:")
    print(f"{'':>6}" + "".join(f"{m:>8}" for m in marks) + f"{'peak':>8}{'end':>8}")
    for pq in (0.25, 0.5, 0.75):
        cells = [q([p[m] for p in paths if p[m] is not None], pq) for m in marks]
        print(
            f"p{int(pq * 100):<5}"
            + "".join(f"{v:>8.2f}" for v in cells)
            + f"{q([p['peak'] for p in paths], pq):>8.2f}{q([p['end'] for p in paths], pq):>8.2f}"
        )
    end_quote = [c["post_trades"][-1][6] / LAMPORTS for c in grads]
    start_quote = [c["post_trades"][0][6] / LAMPORTS for c in grads]
    print(
        f"\npool SOL at creation p50={q(start_quote, 0.5):.1f}; at tape end p50={q(end_quote, 0.5):.2f} "
        f"p90={q(end_quote, 0.9):.2f}; tapes ending under 2 SOL: {sum(1 for v in end_quote if v < 2)}/{len(grads)}"
    )

    print("\nActors:")
    conc = [supply_concentration(c) for c in grads]
    curve_trades = [len(c["trades"]) for c in grads]
    print(
        f"  top-3 holder share of supply at graduation: p25={q(conc, 0.25):.0%} p50={q(conc, 0.5):.0%} p75={q(conc, 0.75):.0%}"
    )
    print(
        f"  curve trades before graduation: p25={q(curve_trades, 0.25):.0f} p50={q(curve_trades, 0.5):.0f}; "
        f"bought out in <=10 trades: {sum(1 for n in curve_trades if n <= 10)}/{len(grads)}"
    )
    first_buyers: Counter = Counter()
    biggest = []
    for c in grads:
        pt = c["post_trades"]
        early = [t for t in pt if t[0] <= pt[0][0] + 2 and t[2]]
        for t in early:
            first_buyers[t[1]] += 1
        biggest.append(max((t[3] for t in early), default=0) / LAMPORTS)
    print(
        f"  largest buy within 2 slots of pool creation: p50={q(biggest, 0.5):.1f} SOL p90={q(biggest, 0.9):.1f} SOL"
    )
    print(
        "  recurring pool-creation buyers: "
        + ", ".join(f"{w[:6]}x{n}" for w, n in first_buyers.most_common(5) if n >= 2)
    )

    buy = int(args.buy_sol * LAMPORTS)
    half = len(grads) // 2
    print(
        f"\nEntry after graduation, buy {args.buy_sol} SOL (rules positive in both halves are marked *):"
    )
    print(
        f"{'entry / rule':38s}{'1st n':>6}{'mean':>8}{'win':>5}{'2nd n':>6}{'mean':>8}{'win':>5}"
    )
    for entry in (25, 75, 150, 300, 750):
        for trail in (0.3, 0.5):
            for tp in (None, 1.0):
                cells = []
                ok = True
                for grp in (grads[:half], grads[half:]):
                    pn = [
                        r / buy
                        for c in grp
                        for r in [
                            simulate(
                                c,
                                entry,
                                trail=trail,
                                take_profit=tp,
                                hold=4500,
                                buy=buy,
                            )
                        ]
                        if r is not None
                    ]
                    if not pn:
                        cells.append("     -       -    -")
                        ok = False
                        continue
                    mean = statistics.mean(pn)
                    ok = ok and mean > 0
                    cells.append(
                        f"{len(pn):>6}{mean:>+8.1%}{sum(1 for v in pn if v > 0) / len(pn):>5.0%}"
                    )
                label = f"grad+{entry:<4} trail{trail:.0%}" + (
                    f" tp{tp:.0%}" if tp else ""
                )
                print(f"{'*' if ok else ' '}{label:37s}" + "".join(cells))

    print("\nEarly-flow filter (first 75 slots) on grad+75 trail30 tp100:")
    for key, cuts in (
        ("buy_ratio", ((0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01))),
        ("buyers", ((0, 5), (5, 15), (15, 40), (40, 10**6))),
    ):
        for lo, hi in cuts:
            pn = [
                r / buy
                for c in grads
                for f in [early_flow(c, 75)]
                if f and lo <= f[key] < hi
                for r in [
                    simulate(c, 75, trail=0.3, take_profit=1.0, hold=4500, buy=buy)
                ]
                if r is not None
            ]
            if pn:
                print(
                    f"  {key} [{lo},{hi}) n={len(pn):>3} mean={statistics.mean(pn):+6.1%} win={sum(1 for v in pn if v > 0) / len(pn):.0%}"
                )


if __name__ == "__main__":
    main()
