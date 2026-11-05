"""Post-washout delayed entry on the post-graduation tape (Wave A3).

WHAT IT DOES
    For every graduated coin in the tape, prices fixed delayed entries into
    the PumpSwap pool at WASHOUT minutes after graduation (default 30, past
    the measured slot-0 sniper/dump window), at holds of 1h/2h/4h:
      - entry at the first post-grad trade at/after grad_ts + washout,
        exit at the first trade at/after entry_ts + hold;
      - a pool that stops trading inside the horizon is marked at its last
        observed trade (dead pools are losses, never exclusions);
      - also reports the liquidity-retention cut: entries only where the
        pool's quote reserves at entry are >= RETAIN_FRACTION of the pool's
        opening quote reserves, vs entries where they have bled below it;
      - robust stats only: medians primary, winsorized means secondary.

WHY WE NEED IT
    Council A3 / astra's opportunity: every prior measurement priced the
    launch and graduation windows; the post-washout window (retail latency
    irrelevant, 0.01-0.05 SOL negligible slippage, dead coins in the
    denominator) is the untested cohort.

HONEST LIMITS (read before citing)
    - Single-day tape (multi-tape rule: no graduation claim).
    - Fills are trade marks; post-grad pools are thin, depth unknown.
    - The washout boundary (30 min) is pre-declared from the measured
      25-minute drain, not tuned.

Run: uv run learning-examples/token-lifecycles/analyze_postgrad_washout.py \
        learning-examples/token-lifecycles/lifecycles_24h.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

LAMPORTS = 1e9
# Quote amounts are lamports only on SOL-paired curves (default pubkey or WSOL).
SOL_QUOTES = {
    None,
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
}
WASHOUT_MIN = 30
HOLDS_MIN = (60, 120, 240)
WINSOR_NET = 0.045  # cap net return at +90% on 0.05 SOL
BUY_SOL = 0.05
FEE_LAMPORTS = 33_000 + 27_000 + 5_000
PUMP_FEE = 0.0125
RETAIN_FRACTION = 0.5  # liquidity-retention cut: quote reserves >= half open
MIN_COINS = 30


def net_return(entry: list, exit_t: list) -> float | None:
    """Net SOL on BUY_SOL at the entry mark, exit at the exit mark."""
    sol_in, tok_out = entry[3], entry[4]
    sol_out, tok_in = exit_t[3], exit_t[4]
    if not tok_out or not sol_in or not tok_in or not sol_out:
        return None
    p_in = (sol_in / LAMPORTS) / (tok_out / 1e6)
    p_out = (sol_out / LAMPORTS) / (tok_in / 1e6)
    if not p_in or not p_out:
        return None
    gross = (p_out / p_in) * (1 - PUMP_FEE) - (1 + PUMP_FEE)
    return gross * BUY_SOL - FEE_LAMPORTS / LAMPORTS


def study(coins: list[dict], washout_min: int) -> list[dict]:
    """One row per (graduated coin, hold) with entry context."""
    rows = []
    for coin in coins:
        post = coin.get("post_trades") or []
        if not post:
            continue
        grad_ts = coin.get("graduated_ts")
        if not grad_ts:
            continue
        # opening quote reserves: the first post-grad trade's pool quote
        open_quote = post[0][6]
        entry_ix = next(
            (i for i, t in enumerate(post) if t[8] >= grad_ts + washout_min * 60),
            None,
        )
        if entry_ix is None:
            rows.append(
                {"mint": coin.get("mint"), "hold": None, "net": None,
                 "dead_before_washout": True,
                 "retained": False, "quote_at_entry": 0.0}
            )
            continue
        entry = post[entry_ix]
        quote_at_entry = entry[6]
        retained = (
            open_quote > 0 and quote_at_entry >= RETAIN_FRACTION * open_quote
        )
        for hold_min in HOLDS_MIN:
            target_ts = entry[8] + hold_min * 60
            exit_t = next(
                (t for t in post[entry_ix + 1 :] if t[8] >= target_ts), None
            )
            if exit_t is None:
                later = post[entry_ix + 1 :]
                exit_t = later[-1] if later else entry
            rows.append(
                {
                    "mint": coin.get("mint"),
                    "hold": hold_min,
                    "net": net_return(entry, exit_t),
                    "dead_before_washout": False,
                    "retained": retained,
                    "quote_at_entry": quote_at_entry,
                    "exit_was_mark_to_last": exit_t[8] < target_ts,
                }
            )
    return rows


def med(vals: list[float]) -> float:
    return statistics.median(vals) if vals else float("nan")


def win_mean(vals: list[float]) -> float:
    capped = [min(max(v, -0.05), WINSOR_NET) for v in vals]
    return statistics.mean(capped) if capped else float("nan")


def line(label: str, vals: list[float], coins: int) -> str:
    return (
        f"  {label:28s} coins={coins:>4d} n={len(vals):>5d} "
        f"median={med(vals):+.5f} SOL win-mean={win_mean(vals):+.5f} "
        f"win-rate={sum(1 for v in vals if v > 0) / len(vals):.0%}"
        if vals
        else f"  {label:28s} no data"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--washout-min", type=int, default=WASHOUT_MIN)
    args = ap.parse_args()

    loaded = [
        json.loads(line) for line in args.path.read_text().splitlines() if line.strip()
    ]
    coins = [c for c in loaded if c.get("quote_mint") in SOL_QUOTES]
    print(f"excluded {len(loaded) - len(coins)} non-SOL-quoted coins")
    grads = [c for c in coins if c.get("post_trades")]
    print(
        f"coins: {len(coins)} | graduated with pool tape: {len(grads)} | "
        f"washout: {args.washout_min} min post-graduation"
    )

    rows = study(coins, args.washout_min)
    dead = [r for r in rows if r["dead_before_washout"]]
    print(f"pools dead before washout (never tradable at entry): {len(dead)}")
    live = [r for r in rows if not r["dead_before_washout"]]

    coverage = {
        c.get("mint"): ((c["post_trades"][-1][8] - c["graduated_ts"]) / 60)
        for c in coins
        if c.get("post_trades") and c.get("graduated_ts")
    }
    for hold in HOLDS_MIN:
        tradable = {
            m for m, span in coverage.items() if span >= args.washout_min + hold
        }
        subset = [r for r in live if r["hold"] == hold and r["mint"] in tradable]
        vals = [r["net"] for r in subset if r["net"] is not None]
        print(f"\n-- hold {hold} min --")
        print(line("ALL post-washout entries", vals, len({r["mint"] for r in subset})))
        kept = [r["net"] for r in subset if r["retained"] and r["net"] is not None]
        bled = [r["net"] for r in subset if not r["retained"] and r["net"] is not None]
        print(line("liquidity RETAINED (>=50% open)", kept, MIN_COINS))
        print(line("liquidity BLED (<50% open)", bled, MIN_COINS))
        marked = sum(1 for r in subset if r["exit_was_mark_to_last"])
        if subset:
            print(
                f"  exits marked at last trade (pool died in horizon): "
                f"{marked}/{len(subset)} = {marked / len(subset):.0%}"
            )

    print(
        "\nCASH = 0. Single tape; marks not quotes; the dead-before-washout "
        "pools are excluded because entry is impossible, but in-horizon pool "
        "deaths are mark-to-last losses in the denominator."
    )


if __name__ == "__main__":
    main()
