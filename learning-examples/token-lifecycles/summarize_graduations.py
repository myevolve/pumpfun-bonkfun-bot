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

# ruff: noqa: PLR0911, PLR0913, PLR2004, S101
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


def post_reserves(t: list) -> tuple[int, int]:
    """Post-trade (base, effective_quote) from PumpSwap pre-trade event reserves."""
    base = t[5] - t[4] if t[2] else t[5] + t[4]
    quote = t[6] + (t[7] or 0) + (t[3] if t[2] else -t[3])
    return base, quote


def post_real_quote(t: list) -> int:
    """Post-trade real quote reserves (lamports)."""
    return t[6] + (t[3] if t[2] else -t[3])


def open_price(t0: list) -> float:
    """Opening SOL per whole token before the first pool trade."""
    return (t0[6] + (t0[7] or 0)) / LAMPORTS / (t0[5] / TOKEN_RAW)


def price(t: list) -> float:
    """SOL per whole token from post-trade effective reserves."""
    return (
        (t[6] + (t[7] or 0) + (t[3] if t[2] else -t[3]))
        / LAMPORTS
        / ((t[5] - t[4] if t[2] else t[5] + t[4]) / TOKEN_RAW)
    )


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
    base, quote = post_reserves(st)
    tokens = base - (base * quote) / (quote + buy * (1 - PUMP_FEE))
    cost = buy + TX_FEES_LAMPORTS

    def value(t: list) -> float:
        b, qq = post_reserves(t)
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


def is_organic_dispersed(coin: dict) -> bool:
    """True if graduation was organic: >=20 dslot, >=15 non-creator buyers, <25% single-buyer share."""
    grad_dslot = coin.get("graduated_dslot")
    if grad_dslot is None or grad_dslot < 20:
        return False
    creator = coin.get("creator")
    curve_buys = [t for t in coin.get("trades", []) if t[2]]
    if not curve_buys:
        return False
    non_creator_buyers = {t[1] for t in curve_buys if t[1] and t[1] != creator}
    if len(non_creator_buyers) < 15:
        return False
    buyer_sol: Counter[str] = Counter()
    for t in curve_buys:
        if t[1]:
            buyer_sol[t[1]] += t[3]
    total_buy_sol = sum(buyer_sol.values())
    if total_buy_sol <= 0:
        return False
    return (max(buyer_sol.values()) / total_buy_sol) < 0.25


def simulate_reclaim(  # noqa: C901, PLR0912
    pt: list,
    *,
    pullback_min: float = 0.10,
    pullback_max: float = 0.35,
    reclaim_pct: float = 0.05,
    min_quote_sol: float = 55.0,
    trail_pct: float = 0.10,
    target_pct: float = 0.15,
    max_hold_slots: int = 300,
    entry_lag: int = 1,
    exit_lag: int = 1,
    fee: float = 0.003,
) -> tuple[float, int] | None:
    """Strategy 1b-ii: +1 slot entry on +5% reclaim off -10%..-35% low while quote >= 55 SOL."""
    if not pt:
        return None
    p0 = open_price(pt[0])
    if p0 <= 0:
        return None

    low = p0
    trigger_i: int | None = None
    for i, t in enumerate(pt):
        p = price(t)
        low = min(low, p)
        pullback = (p0 - low) / p0
        if pullback > pullback_max:
            return None
        if (
            pullback >= pullback_min
            and p >= low * (1 + reclaim_pct)
            and (post_real_quote(t) / LAMPORTS) >= min_quote_sol
        ):
            trigger_i = i
            break

    if trigger_i is None:
        return None

    target_entry_slot = pt[trigger_i][0] + entry_lag
    entry_i = trigger_i
    for j in range(trigger_i + 1, len(pt)):
        if pt[j][0] <= target_entry_slot:
            entry_i = j
        else:
            break

    if entry_i >= len(pt) - 1:
        return None

    entry_trade = pt[entry_i]
    entry_price = price(entry_trade)
    if entry_price <= 0:
        return None
    entry_slot = entry_trade[0]

    peak_price = entry_price
    exit_i: int | None = None
    for k in range(entry_i + 1, len(pt)):
        t = pt[k]
        p = price(t)
        peak_price = max(peak_price, p)
        if (
            p <= peak_price * (1 - trail_pct)
            or p >= entry_price * (1 + target_pct)
            or t[0] - entry_slot >= max_hold_slots
        ):
            target_exit_slot = t[0] + exit_lag
            exit_land = k
            for j in range(k + 1, len(pt)):
                if pt[j][0] <= target_exit_slot:
                    exit_land = j
                else:
                    break
            exit_i = exit_land
            break

    if exit_i is None:
        exit_i = len(pt) - 1

    exit_trade = pt[exit_i]
    exit_price = price(exit_trade)
    if exit_price <= 0:
        return None
    exit_slot = exit_trade[0]
    hold_slots = max(0, exit_slot - entry_slot)
    net_return = (exit_price / entry_price) * ((1.0 - fee) ** 2) - 1.0
    return net_return, hold_slots


def run_self_check() -> None:
    """Offline synthetic check verifying reserves, pricing, dispersion, and reclaim simulation."""
    t_buy = [
        1,
        "buyer",
        1,
        1_000_000_000,
        10_000_000,
        200_000_000,
        30_000_000_000,
        0,
        1000,
    ]
    assert post_reserves(t_buy) == (190_000_000, 31_000_000_000)
    t_sell = [
        2,
        "seller",
        0,
        500_000_000,
        5_000_000,
        190_000_000,
        31_000_000_000,
        0,
        1001,
    ]
    assert post_reserves(t_sell) == (195_000_000, 30_500_000_000)

    t0 = [
        0,
        "init",
        1,
        0,
        0,
        200_000_000 * int(TOKEN_RAW),
        60 * int(LAMPORTS),
        0,
        1000,
    ]
    assert abs(open_price(t0) - (60.0 / 200_000_000)) < 1e-12

    trades_ok = [
        [i, f"buyer_{i}", 1, 1_000_000_000, 100_000, 0, 0, 0, 1000 + i]
        for i in range(15)
    ]
    coin_ok = {"creator": "dev", "graduated_dslot": 25, "trades": trades_ok}
    assert is_organic_dispersed(coin_ok) is True
    assert is_organic_dispersed({**coin_ok, "graduated_dslot": 15}) is False
    assert is_organic_dispersed({**coin_ok, "trades": trades_ok[:10]}) is False
    trades_whale = [
        *trades_ok,
        [16, "whale", 1, 100_000_000_000, 1_000_000, 0, 0, 0, 1016],
    ]
    assert is_organic_dispersed({**coin_ok, "trades": trades_whale}) is False

    pt = [
        [
            0,
            "open",
            1,
            0,
            0,
            200_000_000 * int(TOKEN_RAW),
            60 * int(LAMPORTS),
            0,
            1000,
        ],
        [
            2,
            "dump",
            0,
            4 * int(LAMPORTS),
            25_000_000 * int(TOKEN_RAW),
            200_000_000 * int(TOKEN_RAW),
            60 * int(LAMPORTS),
            0,
            1002,
        ],
        [
            5,
            "bounce",
            1,
            int(1.5 * LAMPORTS),
            5_000_000 * int(TOKEN_RAW),
            225_000_000 * int(TOKEN_RAW),
            56 * int(LAMPORTS),
            0,
            1005,
        ],
        [
            6,
            "fill",
            1,
            int(0.1 * LAMPORTS),
            300_000 * int(TOKEN_RAW),
            220_000_000 * int(TOKEN_RAW),
            int(57.5 * LAMPORTS),
            0,
            1006,
        ],
        [
            10,
            "target",
            1,
            int(9.4 * LAMPORTS),
            int(19.7 * 1e6 * TOKEN_RAW),
            int(219.7 * 1e6 * TOKEN_RAW),
            int(57.6 * LAMPORTS),
            0,
            1010,
        ],
        [
            11,
            "exit",
            1,
            int(0.1 * LAMPORTS),
            int(0.2 * 1e6 * TOKEN_RAW),
            int(200 * 1e6 * TOKEN_RAW),
            int(67 * LAMPORTS),
            0,
            1011,
        ],
    ]
    res = simulate_reclaim(pt)
    assert res is not None and res[0] > 0 and res[1] == 5
    pt_deep = [
        [
            0,
            "open",
            1,
            0,
            0,
            200_000_000 * int(TOKEN_RAW),
            60 * int(LAMPORTS),
            0,
            1000,
        ],
        [
            2,
            "rug",
            0,
            30 * int(LAMPORTS),
            100_000_000 * int(TOKEN_RAW),
            200_000_000 * int(TOKEN_RAW),
            60 * int(LAMPORTS),
            0,
            1002,
        ],
    ]
    assert simulate_reclaim(pt_deep) is None

    print(
        json.dumps(
            {
                "self_check": "ok",
                "post_reserves": True,
                "open_price": True,
                "is_organic_dispersed": True,
                "simulate_reclaim": True,
            }
        )
    )
    sys.exit(0)


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path, nargs="?", default=None)
    ap.add_argument("--buy-sol", type=float, default=0.1)
    ap.add_argument(
        "--self-check",
        action="store_true",
        help="Run offline synthetic checks without reading data tape",
    )
    args = ap.parse_args()
    if args.self_check:
        run_self_check()
    if args.path is None:
        ap.error("the following arguments are required: path")
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
        g0, p0 = pt[0][0], open_price(pt[0])
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
    end_quote = [post_real_quote(c["post_trades"][-1]) / LAMPORTS for c in grads]
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

    def _reclaim_returns(cohort: list[dict]) -> list[float]:
        return [
            r[0]
            for c in cohort
            if (r := simulate_reclaim(c["post_trades"])) is not None
        ]

    def _format_cell(pn: list[float]) -> str:
        if not pn:
            return f"{0:>6}{'-':>8}{'-':>6}"
        mean = statistics.mean(pn)
        win = sum(1 for v in pn if v > 0) / len(pn)
        return f"{len(pn):>6}{mean:>+8.1%}{win:>6.0%}"

    train_grads, lock_grads = grads[:half], grads[half:]
    train_org = [c for c in train_grads if is_organic_dispersed(c)]
    lock_org = [c for c in lock_grads if is_organic_dispersed(c)]
    print(
        "\nStrategy 1b-ii Reclaim (+5% off -10%..-35% low, pool_quote >= 55 SOL, trail 10%, tp 15%, hold 300sl):"
    )
    print(
        f"{'cohort':38s}{'TRAIN n':>6}{'mean':>8}{'win':>6}{'LOCK n':>6}{'mean':>8}{'win':>6}"
    )
    for label, tr, lk in (
        (
            "All Graduations",
            _reclaim_returns(train_grads),
            _reclaim_returns(lock_grads),
        ),
        (
            "Organic Dispersed Graduations",
            _reclaim_returns(train_org),
            _reclaim_returns(lock_org),
        ),
    ):
        ok = bool(tr and statistics.mean(tr) > 0 and lk and statistics.mean(lk) > 0)
        print(f"{'*' if ok else ' '}{label:37s}" + _format_cell(tr) + _format_cell(lk))


if __name__ == "__main__":
    main()
