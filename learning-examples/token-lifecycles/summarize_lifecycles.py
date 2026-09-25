"""Summarize recorded pump.fun lifecycles and backtest entry/exit policies.

Reads the JSONL written by record_lifecycles.py. Two outputs:

1. Population stats: how fast coins peak and die, how early and at what
   liquidity level creators sell, how many snipers land in the creation slot.
2. Policy backtest: replay every coin's trade stream with a hypothetical
   0.01 SOL buy landing at creation + entry latency, then apply an exit policy
   (take-profit / trailing stop / creator-sell / net-outflow / max-hold) with an
   exit latency, and report win rate and net PnL after pump fees and tx fees.

Our own buy is applied to the curve for pricing but assumed not to change
anyone else's behaviour (small size). Offline; no network.

    uv run learning-examples/token-lifecycles/summarize_lifecycles.py lifecycles_1h.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, replace
from pathlib import Path

LAMPORTS = 1e9
PUMP_FEE = 0.0125  # 1% protocol + 0.25% creator, each side (SOL-paired, pre-graduation)
TX_FEES_LAMPORTS = 33_000 + 27_000 + 5_000  # buy + sell + cleanup at 140k/110k CU
INITIAL_VSOL = 30 * LAMPORTS


def q(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def dist(name: str, values: list[float], fmt: str = "{:.2f}") -> None:
    if not values:
        print(f"  {name:36s} n=0")
        return
    print(
        f"  {name:36s} n={len(values):<4d} p25={fmt.format(q(values, 0.25))} "
        f"p50={fmt.format(q(values, 0.5))} p75={fmt.format(q(values, 0.75))} "
        f"p90={fmt.format(q(values, 0.9))} max={fmt.format(max(values))}"
    )


def population(coins: list[dict]) -> None:
    traded = [c for c in coins if c["n_trades"] > 1]
    print(
        f"\ncoins={len(coins)} with_non_dev_trades={len(traded)} "
        f"mayhem={sum(c['mayhem'] for c in coins)} partial={sum(c['partial'] for c in coins)}"
    )
    print("\nPopulation (coins with at least one non-dev trade):")
    dist("dev buy SOL", [c["dev_buy_sol"] for c in traded])
    dist("dev buy % supply", [c["dev_buy_pct_supply"] for c in traded])
    dist("snipers landing in slot 0", [c["buyers_slot0"] for c in traded], "{:.0f}")
    dist("peak real SOL", [c["peak_sol"] for c in traded])
    dist(
        "peak / (dev buy)",
        [c["peak_sol"] / c["dev_buy_sol"] for c in traded if c["dev_buy_sol"] > 0],
    )
    dist(
        "slots to peak",
        [c["peak_dslot"] for c in traded if c["peak_dslot"] is not None],
        "{:.0f}",
    )
    dist(
        "slots to last trade",
        [c["last_dslot"] for c in traded if c["last_dslot"] is not None],
        "{:.0f}",
    )
    dist("holders peak", [c["holders_peak"] for c in traded], "{:.0f}")
    dist("holders end", [c["holders_end"] for c in traded], "{:.0f}")
    dist(
        "final / peak SOL",
        [c["final_sol"] / c["peak_sol"] for c in traded if c["peak_sol"] > 0],
    )
    cs = [c for c in traded if c["creator_first_sell_dslot"] is not None]
    print(f"\n  creator sold in {len(cs)}/{len(traded)} traded coins")
    dist(
        "creator first sell: slots",
        [c["creator_first_sell_dslot"] for c in cs],
        "{:.0f}",
    )
    dist(
        "creator first sell: SOL level", [c["creator_first_sell_sol_level"] for c in cs]
    )
    dist(
        "creator first sell: level/dev buy",
        [
            c["creator_first_sell_sol_level"] / c["dev_buy_sol"]
            for c in cs
            if c["dev_buy_sol"] > 0
        ],
    )
    dist("creator sold % of dev buy", [c["creator_sold_pct"] for c in cs], "{:.0f}")
    reach = {
        m: sum(
            1
            for c in traded
            if c["dev_buy_sol"] > 0 and c["peak_sol"] >= m * c["dev_buy_sol"]
        )
        for m in (1.5, 2, 3, 5)
    }
    print(
        "  P(peak >= m x dev buy): "
        + "  ".join(f"{m}x={n / max(1, len(traded)):.0%}" for m, n in reach.items())
    )


@dataclass(frozen=True, slots=True)
class Policy:
    entry_latency: int  # slots after creation our buy lands
    exit_latency: int  # slots between trigger and our sell landing
    take_profit: float | None  # net ROI target, None = off
    trailing: float | None  # drop from peak value
    stop_loss: float | None
    creator_sell: bool
    outflow: float | None  # net sell flow over last 5 trades as fraction of real SOL
    max_hold: int  # slots
    gate_no_creator_sell: bool  # skip if creator already sold before our entry
    gate_max_real_sol: float | None  # skip if curve real SOL at entry above this
    gate_no_mayhem: bool
    gate_mayhem_only: bool = False
    gate_min_buyers: int = 0  # non-dev buyers that must have landed by entry

    def label(self) -> str:
        parts = [f"e{self.entry_latency}", f"x{self.exit_latency}"]
        if self.take_profit is not None:
            parts.append(f"tp{self.take_profit:.0%}")
        if self.trailing is not None:
            parts.append(f"tr{self.trailing:.0%}")
        if self.stop_loss is not None:
            parts.append(f"sl{self.stop_loss:.0%}")
        if self.creator_sell:
            parts.append("csell")
        if self.outflow is not None:
            parts.append(f"of{self.outflow:.0%}")
        parts.append(f"h{self.max_hold}")
        if self.gate_no_creator_sell:
            parts.append("gNC")
        if self.gate_max_real_sol is not None:
            parts.append(f"gS{self.gate_max_real_sol:g}")
        if self.gate_no_mayhem:
            parts.append("gNM")
        if self.gate_mayhem_only:
            parts.append("gMO")
        if self.gate_min_buyers:
            parts.append(f"gB{self.gate_min_buyers}")
        return " ".join(parts)


def sell_value(v_sol: int, v_tok: int, tokens: int) -> float:
    """Net lamports for selling `tokens` into recorded reserves (v_sol, v_tok).

    Uses the invariant of the recorded state itself, so mayhem coins whose
    virtual params change after creation price correctly. Our own SOL is not
    in the recorded reserves; the small overstatement is accepted.
    """
    out = v_sol - (v_sol * v_tok) / (v_tok + tokens)
    return out * (1 - PUMP_FEE)


def simulate(coin: dict, p: Policy, buy_lamports: int) -> float | None:
    """Return net PnL in lamports for one coin under policy p, or None if no entry."""
    trades = coin["trades"]
    if not trades or coin["dev_buy_sol"] <= 0:
        return None
    if p.gate_no_mayhem and coin["mayhem"]:
        return None
    if p.gate_mayhem_only and not coin["mayhem"]:
        return None
    creator = coin["creator"]
    # (no global k: each recorded state carries its own invariant)
    # State when our buy lands: last trade with dslot <= entry_latency.
    idx = -1
    for i, t in enumerate(trades):
        if t[0] <= p.entry_latency:
            idx = i
        else:
            break
    if idx < 0:
        return None  # not even the dev buy landed before us: shouldn't happen
    if p.gate_no_creator_sell and any(
        t[1] == creator and t[2] == 0 for t in trades[: idx + 1]
    ):
        return None
    if p.gate_min_buyers:
        buyers = {
            t[1] for t in trades[: idx + 1] if t[2] == 1 and t[1] and t[1] != creator
        }
        if len(buyers) < p.gate_min_buyers:
            return None
    v_sol, v_tok, real_sol = trades[idx][6], trades[idx][7], trades[idx][5]
    if p.gate_max_real_sol is not None and real_sol > p.gate_max_real_sol * LAMPORTS:
        return None
    # Our buy: net of fee into the curve.
    spend_net = buy_lamports * (1 - PUMP_FEE)
    tokens = v_tok - (v_sol * v_tok) / (v_sol + spend_net)
    cost = buy_lamports + TX_FEES_LAMPORTS
    peak_value = sell_value(v_sol + spend_net, v_tok - tokens, tokens)
    target = (1 + p.take_profit) * cost if p.take_profit is not None else None
    entry_slot = p.entry_latency
    trigger_slot: int | None = None
    recent: list[tuple[int, int]] = []  # (is_buy, sol)
    for t in trades[idx + 1 :]:
        # Trades on the recorded stream after our entry. Track value and rules.
        recent.append((t[2], t[3]))
        recent = recent[-5:]
        value = sell_value(t[6], t[7], tokens)
        peak_value = max(peak_value, value)
        hold = t[0] - entry_slot
        fire = False
        if target is not None and value >= target:
            fire = True
        if p.trailing is not None and value <= peak_value * (1 - p.trailing):
            fire = True
        if p.stop_loss is not None and value <= buy_lamports * (1 - p.stop_loss):
            fire = True
        if p.creator_sell and t[1] == creator and t[2] == 0:
            fire = True
        if p.outflow is not None and t[5] > 0:
            net_out = sum(s for b, s in recent if not b) - sum(
                s for b, s in recent if b
            )
            if net_out > p.outflow * t[5]:
                fire = True
        if hold >= p.max_hold:
            fire = True
        if fire:
            trigger_slot = t[0]
            break
    if trigger_slot is None:
        trigger_slot = entry_slot + p.max_hold
    land_slot = trigger_slot + p.exit_latency
    # Our sell lands after every recorded trade with dslot <= land_slot.
    state = trades[idx]
    for t in trades[idx + 1 :]:
        if t[0] <= land_slot:
            state = t
        else:
            break
    value = sell_value(state[6], state[7], tokens)
    return value - cost


def backtest(coins: list[dict], buy_sol: float, top: int) -> None:
    buy_lamports = int(buy_sol * LAMPORTS)
    grid = [
        Policy(e, 1, tp, tr, sl, cs, of, h, gnc, gs, gnm, gmo, gb)
        for e in (1, 3)
        for tp in (None, 0.10, 0.25, 0.50)
        for tr in (None, 0.15, 0.30)
        for sl in (None, 0.25, 0.50)
        for cs in (False, True)
        for of in (None, 0.15)
        for h in (10, 25, 60)
        for gnc in (False, True)
        for gs in (None, 0.5, 5.0)
        for gnm, gmo in ((False, False), (True, False), (False, True))
        for gb in (0, 1)
    ]
    results = _rank(grid, coins, buy_lamports)
    if len(coins) >= 40:  # noqa: PLR2004
        _holdout(grid, coins, buy_lamports, top)
    print(
        f"\nBacktest: {len(grid)} policies x {len(coins)} coins, buy {buy_sol} SOL, "
        f"fees {TX_FEES_LAMPORTS / LAMPORTS:.6f} SOL + {PUMP_FEE:.2%} pump each side"
    )
    print(
        f"\n  {'policy':60s} {'n':>4s} {'win':>5s} {'total SOL':>10s} {'median':>9s} {'worst':>9s}"
    )
    for total, p, n, win, med, worst in results[:top]:
        print(
            f"  {p.label():60s} {n:>4d} {win:>5.0%} {total / LAMPORTS:>+10.4f} "
            f"{med / LAMPORTS:>+9.4f} {worst / LAMPORTS:>+9.4f}"
        )
    print("\n  ... worst 5:")
    for total, p, n, win, med, worst in results[-5:]:
        print(
            f"  {p.label():60s} {n:>4d} {win:>5.0%} {total / LAMPORTS:>+10.4f} "
            f"{med / LAMPORTS:>+9.4f} {worst / LAMPORTS:>+9.4f}"
        )
    # Marginal effect of each knob, holding the others at the best policy.
    if results:
        best = results[1]
        print(f"\n  Marginal effect around best ({best[1].label()}):")
        base = best[1]
        for field, values in (
            ("entry_latency", (1, 2, 4, 8)),
            ("exit_latency", (1, 2, 3)),
            ("stop_loss", (None, 0.25, 0.50)),
            ("trailing", (None, 0.10, 0.15, 0.30)),
            ("take_profit", (None, 0.10, 0.25, 0.50)),
            ("max_hold", (5, 10, 25, 60, 150)),
            ("gate_max_real_sol", (None, 2.0, 5.0, 10.0)),
        ):
            row = []
            for v in values:
                p = replace(base, **{field: v})
                pnls = [
                    r
                    for r in (simulate(c, p, buy_lamports) for c in coins)
                    if r is not None
                ]
                row.append(f"{v}: {sum(pnls) / LAMPORTS:+.4f}/{len(pnls)}")
            print(f"    {field:18s} " + "  ".join(row))


def _rank(grid: list[Policy], coins: list[dict], buy_lamports: int) -> list[tuple]:
    results = []
    for p in grid:
        pnls = [
            r for r in (simulate(c, p, buy_lamports) for c in coins) if r is not None
        ]
        if len(pnls) < 5:  # noqa: PLR2004
            continue
        wins = sum(1 for r in pnls if r > 0)
        results.append(
            (
                sum(pnls),
                p,
                len(pnls),
                wins / len(pnls),
                statistics.median(pnls),
                min(pnls),
            )
        )
    results.sort(key=lambda r: r[0], reverse=True)
    return results


def _holdout(
    grid: list[Policy], coins: list[dict], buy_lamports: int, top: int
) -> None:
    """Rank on the earlier half of coins, then score those winners on the later half.

    A policy that only wins in-sample is a fit to noise, not an edge.
    """
    ordered = sorted(coins, key=lambda c: c["create_slot"])
    half = len(ordered) // 2
    train, test = ordered[:half], ordered[half:]
    train_rank = _rank(grid, train, buy_lamports)
    print(f"\nHoldout: ranked on first {len(train)} coins, scored on last {len(test)}")
    print(
        f"\n  {'policy (train rank)':60s} {'train':>9s} {'test n':>6s} {'test win':>8s} {'test SOL':>9s} {'per trade':>9s}"
    )
    for total, p, n, win, _, _ in train_rank[:top]:
        pnls = [
            r for r in (simulate(c, p, buy_lamports) for c in test) if r is not None
        ]
        if not pnls:
            print(f"  {p.label():60s} {total / LAMPORTS:>+9.4f} {0:>6d}")
            continue
        print(
            f"  {p.label():60s} {total / LAMPORTS:>+9.4f} {len(pnls):>6d} "
            f"{sum(1 for r in pnls if r > 0) / len(pnls):>8.0%} {sum(pnls) / LAMPORTS:>+9.4f} "
            f"{statistics.mean(pnls) / buy_lamports:>+9.1%}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--buy-sol", type=float, default=0.01)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--no-backtest", action="store_true")
    args = ap.parse_args()
    coins = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    if not coins:
        sys.exit("no coins recorded")
    population(coins)
    if not args.no_backtest:
        backtest([c for c in coins if c["n_trades"] > 1], args.buy_sol, args.top)


if __name__ == "__main__":
    main()
