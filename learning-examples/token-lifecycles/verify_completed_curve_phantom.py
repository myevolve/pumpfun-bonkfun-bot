"""Detect the completed-curve phantom profit in a recorded lifecycle tape.

The trap this tool exists for: a bonding-curve backtest that prices an exit
with the constant-product formula *after* the curve has been bought out. At
completion the curve's remaining `virtual_token_reserves` are just the
constant virtual offset — the formula then hands back nearly all accumulated
SOL for a tiny token sale that the contract would reject. On modern tapes
that phantom dominates: on the Sep 4-5 2026 recording it turned a -30%
milestone strategy into a +300% hallucination, because 62% of graduations
complete within ten trades and half of those within the creation slot.

What it does, per graduated coin:
- computes the phantom: what `sell_value` (the common summarize_lifecycles
  helper) would book selling `tokens` at the post-buyout curve state;
- computes the honest alternative: selling the same tokens into the
  migration pool at its opening reserves (pre-trade, 0.3% fee);
- reports the phantom-vs-honest inflation for the tape.

Run it on ANY tape in the lifecycles format (yours included). If the phantom
column is non-zero, every backtest that exits on the curve after a buyout is
inflated. This is the check public pump.fun backtesting tools skip.

Usage:
    uv run learning-examples/token-lifecycles/verify_completed_curve_phantom.py \
        [--tape learning-examples/token-lifecycles/lifecycles_24h.jsonl]

Never moves funds; tape-model results are not fills.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from summarize_lifecycles import sell_value

POOL_FEE = 0.003


def phantom_and_honest(coin: dict, tokens: int) -> tuple[float, float] | None:
    """(phantom lamports, honest lamports) for one graduated coin, or None."""
    trades = coin["trades"]
    posts = coin["post_trades"] or []
    if coin.get("pool_dslot") is None or not posts or not trades:
        return None
    last = trades[-1]
    v_sol, v_tok = last[6], last[7]
    if v_tok <= 0 or v_sol <= 0:
        return None
    # the naive bookkeeping: selling into the completed curve's virtual state
    phantom = sell_value(v_sol, v_tok, tokens)
    # the honest bookkeeping: the pool's opening state is the only exit
    p0 = posts[0]
    base, eff = p0[5], p0[6] + (p0[7] or 0)
    if base <= 0 or eff <= 0:
        honest = 0.0  # unusable pool: the position is unrecoverable
    else:
        honest = eff * tokens / (base + tokens) * (1 - POOL_FEE)
    return phantom, honest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tape", default="learning-examples/token-lifecycles/lifecycles_24h.jsonl"
    )
    ap.add_argument("--sol", type=float, default=0.01, help="position size in SOL")
    args = ap.parse_args()
    tokens_per_sol = int(args.sol * 1e9 * 35_000)  # ~mid-curve price scale

    phantom_total = honest_total = 0.0
    phantom_coins = honest_worse = 0
    n = graduated = 0
    phantom_lamps: list[float] = []
    with Path(args.tape).open() as f:
        for line in f:
            n += 1
            coin = json.loads(line)
            if coin.get("graduated_dslot") is None:
                continue
            graduated += 1
            pair = phantom_and_honest(coin, tokens_per_sol)
            if pair is None:
                continue
            phantom, honest = pair
            if phantom > honest:
                phantom_coins += 1
            if honest < phantom:
                honest_worse += 1
            phantom_total += phantom
            honest_total += honest
            phantom_lamps.append(phantom - honest)

    if graduated == 0:
        raise SystemExit("no_graduated_coins_in_tape")
    print(f"tape {args.tape}: {n} coins, {graduated} graduated")
    print(
        f"phantom-pricing graduated coins: {phantom_coins} "
        f"(of {graduated}); honest exit worse than phantom on {honest_worse}"
    )
    print(
        f"aggregate at {args.sol} SOL positions: phantom {phantom_total / 1e9:.4f} SOL"
        f" vs honest {honest_total / 1e9:.4f} SOL"
    )
    if phantom_lamps:
        ordered = sorted(phantom_lamps)
        print(
            "phantom-minus-honest per coin: p50 "
            f"{statistics.median(ordered) / 1e9:+.4f} SOL, p90 "
            f"{ordered[int(0.9 * len(ordered))] / 1e9:+.4f} SOL"
        )
    print(
        "verdict: any backtest booking curve sales on post-buyout states is "
        "inflated by the phantom column; honest exits price against the pool"
    )


if __name__ == "__main__":
    main()
