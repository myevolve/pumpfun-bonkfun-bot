"""Wallet PnL census across the 24h lifecycles tape — READ-ONLY.

Reverse-engineering successful trades, with the tape's own valuation math
(`summarize_lifecycles.sell_value` semantics: exact constant-product SOL
out at the pre-trade reserves, 1.25% pump fee per side, 65k lamports round
trip). Mayhem's protocol sol-vault wallet is excluded per the README rule.

Per wallet × coin: replay trades in order; sells convert tokens to SOL at
the pre-trade reserves; the residual position is marked at the coin's last
recorded state. Wallet PnL = Σ sell_SOL − Σ buy_SOL − fees + mark.
Coins where sells exceed recorded buys (tape gaps) are flagged and their
PnL is capped at zero rather than believed.

Outputs the PnL distribution and a profile of the top 20 wallets by
realized PnL. Nothing moves funds.
"""

import json
import statistics
from collections import defaultdict

TAPE = "learning-examples/token-lifecycles/lifecycles_24h.jsonl"
PUMP_FEE = 0.0125
TX_FEES = 33_000 + 27_000
MAYHEM_VAULT_PREFIX = "BwWK17cb"


def sell_sol_out(v_sol: int, v_tok: int, tokens: int) -> float:
    out = v_sol - (v_sol * v_tok) / (v_tok + tokens)
    return out * (1 - PUMP_FEE)


def main() -> None:
    wallets = defaultdict(list)  # wallet -> list of per-coin PnL records
    n_coins = 0
    for line in open(TAPE):
        d = json.loads(line)
        n_coins += 1
        trades = d["trades"]
        if not trades:
            continue
        mint = d["mint"]
        per_wallet = defaultdict(lambda: {"tokens": 0, "cost": 0, "got": 0.0,
                                          "sells_exceed": False, "first": None,
                                          "last": None})
        prev_vs, prev_vt = None, None
        for t in trades:
            dslot, wallet, is_buy, sol, tok, _rsol, vsol, vtok, _ts = t
            if wallet and wallet.startswith(MAYHEM_VAULT_PREFIX):
                prev_vs, prev_vt = vsol, vtok
                continue
            w = per_wallet[wallet]
            if w["first"] is None:
                w["first"] = dslot
            w["last"] = dslot
            if is_buy:
                w["tokens"] += tok
                w["cost"] += sol * (1 + PUMP_FEE) + TX_FEES / 2
            else:
                if prev_vs is None:
                    prev_vs, prev_vt = vsol, vtok
                if tok > w["tokens"]:
                    w["sells_exceed"] = True
                out_tokens = min(tok, w["tokens"])
                if out_tokens > 0:
                    w["got"] += sell_sol_out(prev_vs, prev_vt, out_tokens)
                w["tokens"] = max(0, w["tokens"] - tok)
            prev_vs, prev_vt = vsol, vtok
        # mark residual at final state
        fvs, fvt = trades[-1][6], trades[-1][7]
        for wallet, w in per_wallet.items():
            mark = sell_sol_out(fvs, fvt, w["tokens"]) if w["tokens"] > 0 else 0.0
            pnl = w["got"] + mark - w["cost"]
            wallets[wallet].append({
                "mint": mint, "pnl": pnl, "cost": w["cost"],
                "closed": w["tokens"] == 0, "exceed": w["sells_exceed"],
                "first": w["first"], "last": w["last"],
            })
    # aggregate per wallet
    agg = []
    for wallet, coins in wallets.items():
        flagged = sum(1 for c in coins if c["exceed"])
        realized = sum(c["pnl"] for c in coins if c["closed"] and not c["exceed"])
        all_pnl = sum(c["pnl"] for c in coins)
        wins = sum(1 for c in coins if c["closed"] and not c["exceed"] and c["pnl"] > 0)
        resolved = sum(1 for c in coins if c["closed"] and not c["exceed"])
        firsts = sorted(c["first"] for c in coins if c["first"] is not None)
        spans = sorted(c["last"] - c["first"] for c in coins if c["first"] is not None)
        agg.append({
            "wallet": wallet[:10],
            "coins": len(coins), "resolved": resolved, "wins": wins,
            "realized": realized / 1e9, "total_pnl": all_pnl / 1e9,
            "flagged": flagged,
            "med_first_dslot": firsts[len(firsts) // 2] if firsts else None,
            "med_span_dslot": spans[len(spans) // 2] if spans else None,
        })
    agg.sort(key=lambda a: -a["realized"])
    pos = sum(1 for a in agg if a["realized"] > 0)
    neg = sum(1 for a in agg if a["realized"] < 0)
    print(json.dumps({
        "coins": n_coins, "wallets": len(agg),
        "realized_positive": pos, "realized_negative": neg,
        "top20": agg[:20],
        "bottom5": agg[-5:],
    }, indent=1))


if __name__ == "__main__":
    main()
