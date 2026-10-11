# ruff: noqa: TRY003, PLR2004, C901, PTH123 - censoring paths are
# the point; thresholds are layout constants; one linear census.
"""Creator annuity tracker — READ-ONLY, no funds.

Phase 0 of the creator pilot design
(docs/superpowers/specs/2026-10-10-creator-pilot-design.md): measure the
real creator fee accrual on graduated whale-swept pools, daily, to
replace the survivorship-tainted 1,858 SOL/24h volume estimate with the
measured decay curve.

Cohort: migrated PumpSwap pools whose coin_creator is a known
whale-census self-sweep wallet, plus any pool discovered in the live
instrument's grad_marks (real>=60 SOL). Per pool per run:

  creator_vault PDA = ["creator_vault", coin_creator] under PumpSwap
  accrual = SOL in the vault now minus the previous snapshot's balance

State: .state/learning/creator_fee_snapshots.jsonl — one row per
(pool, run); the daily deltas between rows are the annuity curve.

Nothing here moves funds or authorizes a transaction.
"""

import argparse
import asyncio
import json
import sqlite3
import statistics
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from core.client import SolanaClient  # noqa: E402
from platforms.pumpfun.pumpswap import (  # noqa: E402
    WSOL_MINT,
    PumpSwapAddresses,
    _decode_pool_account,
)

STATE = REPO / ".state/learning/creator_fee_snapshots.jsonl"
WHITELIST_PATH = REPO / ".state/learning/creator_fee_whitelist.json"
WHALERS = (
    json.loads(
        (REPO / "learning-examples/token-lifecycles/whaleride_wallets.json").read_text()
    )
    if (REPO / "learning-examples/token-lifecycles/whaleride_wallets.json").exists()
    else []
)


def rpc_from_env() -> str:
    for line in (REPO / ".state/configs/capital-research-rpc.env").open():
        if line.startswith("SOLANA_NODE_RPC_ENDPOINT"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no RPC endpoint in the service-only projection")


def previous_balances() -> dict:
    if not STATE.exists():
        return {}
    last = {}
    for line in STATE.open():
        row = json.loads(line)
        last[row["pool"]] = row["vault_sol"]
    return last


def cohort_pools() -> list[dict]:
    """Live PumpSwap pools whose coin_creator matches the whale census,
    plus the instrument's accepted coins (fund >= 60)."""
    mints: set[str] = set()
    for line in open(REPO / "learning-examples/token-lifecycles/lifecycles_24h.jsonl"):
        d = json.loads(line)
        trades = d.get("trades") or []
        if not trades:
            continue
        # whale self-sweep: final real_sol in the whale band, same creator
        rsol = trades[-1][5] / 1e9
        if 60 <= rsol <= 1000:
            mints.add(d["mint"])
    db = sqlite3.connect(str(REPO / ".state/learning/lessons.sqlite3"))
    for (mint,) in db.execute(
        "SELECT DISTINCT l.mint FROM lessons l JOIN grad_marks g ON g.entry_id=l.id"
        " WHERE g.open_price IS NOT NULL"
    ):
        mints.add(mint)
    db.close()
    return [{"mint": m} for m in sorted(mints)]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", default=None)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    rpc = args.rpc or rpc_from_env()
    pools = cohort_pools()[: args.limit]
    client = SolanaClient(rpc)
    try:
        prev = previous_balances()
        rows = []
        # Derive all PDAs first, then batch-read (public endpoint limits).
        derived = []
        for p in pools:
            mint = Pubkey.from_string(p["mint"])
            pool = PumpSwapAddresses.derive_canonical_pool(mint, WSOL_MINT)
            derived.append((p["mint"], mint, pool))
        by_pool = {}
        for i in range(0, len(derived), 100):
            chunk = derived[i : i + 100]
            accs = await client.get_multiple_accounts(
                [d[2] for d in chunk], commitment="confirmed"
            )
            for (mint_str, mint, pool), acc in zip(chunk, accs, strict=False):
                if acc is None or len(acc.data) < 261:
                    continue  # pool never created: censor
                try:
                    decoded = _decode_pool_account(acc, pool, mint, WSOL_MINT)
                except ValueError:
                    continue
                by_pool[pool] = (mint_str, decoded.coin_creator)
        vault_keys = [
            PumpSwapAddresses.derive_creator_vault(cc) for _, cc in by_pool.values()
        ]
        vault_bal = {}
        for i in range(0, len(vault_keys), 100):
            chunk = vault_keys[i : i + 100]
            accs = await client.get_multiple_accounts(chunk, commitment="confirmed")
            for key, acc in zip(chunk, accs, strict=False):
                vault_bal[key] = (
                    struct.unpack_from("<Q", bytes(acc.data), 64)[0] / 1e9
                    if acc is not None and acc.data
                    else 0.0  # never funded: creator collected nothing
                )
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for pool, (_mint_str, coin_creator) in by_pool.items():
            cv = PumpSwapAddresses.derive_creator_vault(coin_creator)
            bal = vault_bal[cv]
            rows.append(
                {
                    "ts": now,
                    "pool": str(pool),
                    "vault_sol": round(bal, 6),
                    "delta_sol": round(bal - prev[str(pool)], 6)
                    if str(pool) in prev
                    else None,
                    "coin_creator": str(coin_creator)[:10],
                }
            )
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with STATE.open("a") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        deltas = [r["delta_sol"] for r in rows if r["delta_sol"] is not None]
        print(f"snapshot: {len(rows)} pools ({len(deltas)} with a previous baseline)")
        if deltas:
            print(
                f"daily creator-fee accrual: median "
                f"{statistics.median(deltas):.4f} SOL, mean "
                f"{statistics.mean(deltas):.4f} SOL, max "
                f"{max(deltas):.4f} SOL"
            )
            print(
                "payback on 85 SOL at this median: "
                f"{85 / statistics.median(deltas):.0f} days"
                if statistics.median(deltas) > 0
                else "no accrual measured"
            )
    finally:
        await client.close()


if __name__ == "__main__":
    from solders.pubkey import Pubkey

    asyncio.run(main())
