"""Convert the Slinky21 PumpFun corpus into record_lifecycles.py's JSONL format.

Dataset: https://huggingface.co/datasets/Slinky21/Pumpfun_Memecoin_Corpus
(798k launches, 33.6M trades with post-trade virtual reserves, Jun-Jul 2026).
The output feeds summarize_lifecycles.py unchanged, so entry/exit policies
can be scored on tens of thousands of coins instead of one hour of stream.

Handling per the corpus's KNOWN_ISSUES.md:
- rows whose sol_amount disagrees with token_amount x price_sol by more than
  100x are dropped (§3.1); tokens with null prices are dropped (§3.2);
- wallet BwWK17... is Mayhem's sol-vault PDA, misidentified as the System
  Program in §3.3: keep its trades but blank the wallet so it cannot count
  as a buyer or creator;
- real SOL reserves are not stored; they are derived as v_sol - v_sol0.

Slots are approximated as seconds_since_launch / 0.4. Only tokens that
started tracking within --max-first-trade seconds of launch are kept.
These are inferred time buckets, not exact chain slots or bundle evidence.

    uv run --with duckdb --with pytz learning-examples/token-lifecycles/corpus_to_lifecycles.py \\
        --data-dir /tmp/pfcorpus --sample 20000 --out corpus_20k.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import duckdb

MAYHEM_SOL_VAULT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
SLOT_SECONDS = 0.4
LAMPORTS = 1_000_000_000
TOKEN_RAW = 1_000_000


def main() -> None:  # noqa: PLR0915
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=20000, help="tokens to keep (random)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--max-first-trade",
        type=float,
        default=2.0,
        help="drop tokens whose first trade is later than this many seconds",
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    d = args.data_dir
    con = duckdb.connect()
    t0 = time.time()
    con.execute(f"""
        create table tokens as
        select mint, detected_at, name, symbol, is_mayhem_mode, creator,
               initial_buy_sol, initial_buy_tokens,
               coalesce(dev_buy_pct_corrected, dev_buy_pct) as dev_buy_pct,
               v_sol_bonding_curve as v_sol0, v_tokens_bonding_curve as v_tok0,
               trade_count, epoch(detected_at) as detected_ts
        from '{d / "tokens.parquet"}'
        where trade_count >= 2 and creator is not null
    """)
    n_tokens = con.execute("select count(*) from tokens").fetchone()[0]
    random.seed(args.seed)
    chosen = con.execute("select mint from tokens order by mint").fetchall()
    chosen = [m for (m,) in chosen]
    random.shuffle(chosen)
    chosen = chosen[: args.sample * 6]  # oversample; some fail the quality filters
    con.execute("create table chosen (mint varchar)")
    con.executemany("insert into chosen values (?)", [(m,) for m in chosen])
    con.execute(f"""
        create table tr as
        select t.mint, t.seconds_since_launch as s, t.is_buy,
               t.sol_amount, t.token_amount,
               case when t.user_wallet = '{MAYHEM_SOL_VAULT}' then '' else t.user_wallet end as wallet,
               t.v_sol_bonding_curve as v_sol, t.v_tokens_bonding_curve as v_tok,
               t.price_sol, epoch(t.event_time) as ts
        from read_parquet('{d / "trades" / "trades-*.parquet"}') t
        join chosen c on c.mint = t.mint
        where t.price_sol is not null and t.sol_amount is not null
          and t.token_amount > 0 and t.v_sol_bonding_curve > 0 and t.v_tokens_bonding_curve > 0
          and (t.sol_amount / (t.token_amount * t.price_sol)) between 0.01 and 100
    """)
    print(
        f"tokens={n_tokens} chosen={len(chosen)} trades loaded in {time.time() - t0:.0f}s"
    )
    rows = con.execute("""
        select tr.mint, list(struct_pack(s := s, b := is_buy, sol := sol_amount, tok := token_amount,
                                        w := wallet, vs := v_sol, vt := v_tok, ts := ts) order by s, ts)
        from tr group by tr.mint
    """).fetchall()
    meta = {
        r[0]: r
        for r in con.execute(
            "select mint, name, symbol, is_mayhem_mode, creator, initial_buy_sol, initial_buy_tokens,"
            " dev_buy_pct, v_sol0, v_tok0, detected_ts from tokens"
        ).fetchall()
    }
    written = 0
    dropped_late = 0
    with args.out.open("w", encoding="utf-8") as out:
        for mint, trades in rows:
            if written >= args.sample:
                break
            m = meta.get(mint)
            if m is None or not trades:
                continue
            if trades[0]["s"] > args.max_first_trade:
                dropped_late += 1
                continue
            (
                _,
                name,
                symbol,
                mayhem,
                creator,
                dev_sol,
                dev_tok,
                dev_pct,
                v_sol0,
                v_tok0,
                det_ts,
            ) = m
            v_sol0 = int(v_sol0 or 30 * LAMPORTS)
            first_slot = trades[0]["s"]
            compact = []
            for t in trades:
                dslot = max(0, int(round((t["s"] - first_slot) / SLOT_SECONDS)))
                v_sol = int(t["vs"])
                compact.append(
                    [
                        dslot,
                        t["w"],
                        1 if t["b"] else 0,
                        int(t["sol"] * LAMPORTS),
                        int(t["tok"] * TOKEN_RAW),
                        max(0, v_sol - v_sol0),
                        v_sol,
                        int(t["vt"]),
                        int(t["ts"]),
                    ]
                )
            real = [c[5] for c in compact]
            peak_i = max(range(len(real)), key=real.__getitem__)
            creator_sells = [c for c in compact if c[1] == creator and c[2] == 0]
            dev_buy = next((c for c in compact if c[1] == creator and c[2] == 1), None)
            holders: dict[str, int] = {}
            holders_peak = 0
            for c in compact:
                if not c[1]:
                    continue
                holders[c[1]] = holders.get(c[1], 0) + (c[4] if c[2] else -c[4])
                holders_peak = max(
                    holders_peak, sum(1 for v in holders.values() if v > 0)
                )
            row = {
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "creator": creator,
                "create_slot": int(det_ts / SLOT_SECONDS),
                "create_sig": None,
                "create_ts": int(det_ts),
                "mayhem": bool(mayhem),
                "cashback": None,
                "quote_mint": None,
                "v_sol0": v_sol0,
                "v_tok0": int(v_tok0 or 1_073_000_000 * TOKEN_RAW),
                "supply": 2_000_000_000 * TOKEN_RAW
                if mayhem
                else 1_000_000_000 * TOKEN_RAW,
                "trades": compact,
                "partial": False,
                "n_trades": len(compact),
                "n_failed_excluded": None,
                "dev_buy_sol": (dev_buy[3] / LAMPORTS)
                if dev_buy
                else float(dev_sol or 0.0),
                "dev_buy_pct_supply": float(dev_pct or 0.0),
                "buyers_slot0": len(
                    {
                        c[1]
                        for c in compact
                        if c[0] == 0 and c[2] == 1 and c[1] and c[1] != creator
                    }
                ),
                "peak_sol": real[peak_i] / LAMPORTS,
                "peak_dslot": compact[peak_i][0],
                "creator_first_sell_dslot": creator_sells[0][0]
                if creator_sells
                else None,
                "creator_first_sell_sol_level": (creator_sells[0][5] / LAMPORTS)
                if creator_sells
                else None,
                "creator_sold_pct": (
                    sum(c[4] for c in creator_sells) / dev_buy[4] * 100
                    if dev_buy and creator_sells
                    else 0.0
                ),
                "holders_peak": holders_peak,
                "holders_end": sum(1 for v in holders.values() if v > 0),
                "last_dslot": compact[-1][0],
                "final_sol": real[-1] / LAMPORTS,
                "buy_sol": sum(c[3] for c in compact if c[2]) / LAMPORTS,
                "sell_sol": sum(c[3] for c in compact if not c[2]) / LAMPORTS,
                "source": "corpus",
            }
            out.write(json.dumps(row, separators=(",", ":")) + "\n")
            written += 1
    print(
        f"wrote {written} coins to {args.out} (dropped {dropped_late} late-tracked) in {time.time() - t0:.0f}s"
    )


if __name__ == "__main__":
    main()
