"""Wallet-PnL persistence and delayed-follow entry on a pump tape.

WHAT IT DOES
    1. Per-wallet realized PnL over the tape via average-cost matching of
       trade tuples (system program and the mayhem vault are not traders).
    2. Split-half persistence: does a wallet's realized PnL in the earlier
       half of the tape predict its PnL in the later half?
    3. Concentration: how much of all realized PnL the top wallets capture.
    4. Delayed-follow entry simulation, walk-forward: the winner threshold is
       the half-1 PnL quantile; signals are half-2 buys by wallets whose
       realized PnL *strictly before the signal* clears that threshold. We
       enter the same coin a fixed detection-to-execution delay later, hold a
       fixed horizon, and price the round trip net of pump fees. The same
       simulation over ALL half-2 buys is the baseline; the delta is the
       wallet-selection edge.

WHY WE NEED IT
    Council Wave A1: wallets are pre-entry observable history, answerable
    from the existing tape before any new capture.

HONEST LIMITS (read before citing)
    - Single-day tape: the multi-tape rule (>=3 non-contiguous tapes) blocks
      any graduation claim; this is a first falsification pass.
    - Delayed entries and exits fill at the next observed trade mark, not a
      book-verified quote; depth at that mark is unknown.
    - Curve-phase trades only; post-graduation trading is a separate lane.
    - One simulated position per coin at a time (overlap purge).

Run: uv run learning-examples/token-lifecycles/analyze_wallet_pnl.py \
        learning-examples/token-lifecycles/lifecycles_24h.jsonl
"""

from __future__ import annotations

import argparse
import json
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
SYSTEM_PROGRAM = "BwWK17cbnccyENbbsPvFrVUn8oqNBsQ"  # noqa: S105
MAYHEM_VAULT = "MAYHEM_SOL_VAULT"  # placeholder label; matched by exclusion set
FEE_LAMPORTS = 33_000 + 27_000 + 5_000
PUMP_FEE = 0.0125
BUY_SOL = 0.05


def implied_price(trade: list) -> float:
    """SOL per whole token at a trade mark (amounts are lamports / 1e6-tok)."""
    sol, tok = trade[3], trade[4]
    if not tok or not sol:
        return 0.0
    return (sol / LAMPORTS) / (tok / 1e6)


def build_events(coins: list[dict]) -> list[dict]:
    """Slot-ordered curve trade events with wallet and coin references."""
    events: list[dict] = []
    for coin in coins:
        create = coin.get("create_slot") or 0
        supply = coin.get("supply")
        for t in coin.get("trades", []):
            user, is_buy, sol, tok = t[1], t[2], t[3], t[4]
            if user in (SYSTEM_PROGRAM, MAYHEM_VAULT):
                continue
            if supply and tok > supply:  # curve burn/graduation mechanics
                continue
            if not implied_price(t):
                continue
            events.append(
                {
                    "slot": create + t[0],
                    "user": user,
                    "mint": coin.get("mint"),
                    "is_buy": bool(is_buy),
                    "sol": sol / LAMPORTS,
                    "tok": tok / 1e6,
                }
            )
    events.sort(key=lambda e: e["slot"])
    return events


def pass_one(events: list[dict]) -> tuple[dict, dict, list[dict]]:
    """Average-cost realized PnL per (wallet, coin).

    Returns (running final PnL per wallet, PnL per (wallet, half) at sells,
    buy signals with the wallet's realized PnL strictly before the signal).
    """
    midpoint = statistics.median(e["slot"] for e in events)
    basis: dict[tuple, dict] = {}
    running: dict[str, float] = defaultdict(float)
    pnl_half: dict[tuple, float] = defaultdict(float)
    signals: list[dict] = []
    for ev in events:
        half = 0 if ev["slot"] < midpoint else 1
        key = (ev["user"], ev["mint"])
        st = basis.setdefault(key, {"tok": 0.0, "cost": 0.0})
        if ev["is_buy"]:
            signals.append(
                {
                    "slot": ev["slot"],
                    "user": ev["user"],
                    "mint": ev["mint"],
                    "prior_pnl": running[ev["user"]],
                    "half": half,
                }
            )
            st["tok"] += ev["tok"]
            st["cost"] += ev["sol"]
        elif st["tok"] > 0:
            avg = st["cost"] / st["tok"]
            sold = min(ev["tok"], st["tok"])
            realized = ev["sol"] - avg * sold
            running[ev["user"]] += realized
            pnl_half[(ev["user"], half)] += realized
            st["tok"] -= sold
            st["cost"] -= avg * sold
    return dict(running), dict(pnl_half), signals


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        for rank, i in enumerate(order):
            r[i] = float(rank)
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def simulate(
    signals: list[dict],
    coins_by_mint: dict[str, dict],
    qualify: dict | None,
    delays: list[int],
    holds: list[int],
) -> dict:
    """Delayed-follow round trips net of fees; one position per coin.

    `qualify=None` simulates every signal (baseline); otherwise a signal is
    taken only when its strictly-prior PnL clears the threshold.
    """
    out = {}
    for delay in delays:
        for hold in holds:
            evs = []
            taken = 0
            for sig in signals:
                thr = qualify["fn"](sig) if qualify else 0.0
                if qualify and sig["prior_pnl"] < thr:
                    continue
                coin = coins_by_mint.get(sig["mint"])
                if not coin:
                    continue
                create = coin.get("create_slot") or 0
                trades = coin.get("trades", [])
                later = [
                    t for t in trades if create + t[0] > sig["slot"]
                ]
                entry = next(
                    (t for t in later if create + t[0] >= sig["slot"] + delay), None
                )
                if entry is None:
                    continue
                entry_slot = create + entry[0]
                exit_t = next(
                    (t for t in later if create + t[0] >= entry_slot + hold), None
                )
                if exit_t is None:
                    continue
                p_in, p_out = implied_price(entry), implied_price(exit_t)
                if not p_in or not p_out:
                    continue
                gross = (p_out / p_in) * (1 - PUMP_FEE) - (1 + PUMP_FEE)
                evs.append(gross * BUY_SOL - FEE_LAMPORTS / LAMPORTS)
                taken += 1
            out[(delay, hold)] = {
                "n": taken,
                "ev": statistics.mean(evs) if evs else float("nan"),
                "win": (sum(1 for e in evs if e > 0) / len(evs)) if evs else float("nan"),
            }
    return out


def report_grid(title: str, grid: dict, fees_sol: float) -> None:
    print(f"\n== {title} ==")
    for (delay, hold), r in sorted(grid.items()):
        gross = r["ev"] + fees_sol
        print(
            f"  delay={delay:>2d} hold={hold:>3d}: n={r['n']:>6d} "
            f"net EV={r['ev']:+.5f} gross~{gross:+.5f} SOL win={r['win']:.0%}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    args = ap.parse_args()

    loaded = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    coins = [c for c in loaded if c.get("quote_mint") in SOL_QUOTES]
    print(f"excluded {len(loaded) - len(coins)} non-SOL-quoted coins")
    coins_by_mint = {c.get("mint"): c for c in coins}
    print(f"coins: {len(coins)}")

    events = build_events(coins)
    running, pnl_half, signals = pass_one(events)
    # midpoint lives inside pass_one; nothing needs it here.
    print(f"curve trade events: {len(events)} | wallets: {len(running)}")

    both = {u for (u, h) in pnl_half if h == 0} & {u for (u, h) in pnl_half if h == 1}
    xs = [pnl_half.get((u, 0), 0.0) for u in both]
    ys = [pnl_half.get((u, 1), 0.0) for u in both]
    rho = spearman(xs, ys)
    h1_sorted = sorted(xs)
    top_decile_cut = h1_sorted[-max(1, len(h1_sorted) // 10)]
    winners_h1 = {u for u in both if pnl_half.get((u, 0), 0.0) >= top_decile_cut}
    pos_h2 = sum(v for (u, h), v in pnl_half.items() if h == 1 and v > 0)
    top_share = (
        sum(pnl_half.get((u, 1), 0.0) for u in winners_h1) / pos_h2 if pos_h2 else 0.0
    )
    print(
        f"split-half persistence: spearman={rho:+.3f} wallets-in-both={len(both)}\n"
        f"  top-decile(h1) wallets capture {top_share:.1%} of positive h2 PnL"
    )

    total_pos = sum(v for v in running.values() if v > 0)
    ranked = sorted(running.items(), key=lambda kv: -kv[1])
    print(
        f"concentration: top-10 earn {sum(v for _, v in ranked[:10]) / total_pos:.1%}, "
        f"top-100 earn {sum(v for _, v in ranked[:100]) / total_pos:.1%} "
        f"of all positive realized PnL"
    )

    # Walk-forward: threshold from half-1 PnL, signals only in half-2.
    h1_pos = sorted(v for (u, h), v in pnl_half.items() if h == 0 and v > 0)
    thresholds = {
        "top-decile": h1_pos[-max(1, len(h1_pos) // 10)] if h1_pos else 0.0,
        "median-winner": statistics.median(h1_pos) if h1_pos else 0.0,
    }
    h2_signals = [s for s in signals if s["half"] == 1]
    fees_sol = (2 * PUMP_FEE * BUY_SOL) + FEE_LAMPORTS / LAMPORTS

    base = simulate(h2_signals, coins_by_mint, None, [2], [25])
    report_grid("baseline: ALL half-2 buys (delay 2, hold 25)", base, fees_sol)

    for label, thr in thresholds.items():
        qualify = {"fn": lambda _sig, _t=thr: _t}
        grid = simulate(h2_signals, coins_by_mint, qualify, [1, 2, 4, 8], [10, 25, 60])
        report_grid(
            f"delayed-follow on {label} h1-winner wallets (thr {thr:.3f} SOL)",
            grid,
            fees_sol,
        )

    print("\nCASH baseline EV = 0. Single tape; no graduation claim. "
          "Fills are trade marks, not book quotes.")


if __name__ == "__main__":
    main()
