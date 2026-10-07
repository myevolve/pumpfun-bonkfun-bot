"""Simulate LP income on graduated pump.fun pools — READ-ONLY, no funds.

The whale census found the creator's business model: self-sweep ~85 SOL,
graduate the coin, and collect a fee annuity on the pool (median pool
volume 1,858 SOL/24h; creator-set fee bps; payback 5-9 days). The
follower cannot win the entry race (the latency frontier: entry drift
+25-56% within 1-2 trades), but PumpSwap pools accept third-party
liquidity — a follower could own a share of the same fee stream without
racing for the crossing.

For N whale-swept graduated coins from the lifecycles tape this script:
 1. derives the canonical pool and reads its LIVE account, both vault
    balances, and the fee program's fee_config PDA (the market-cap
    tiered schedule),
 2. reconstructs the pool's opening liquidity and 24h volume from the
    tape's post_trades,
 3. simulates a fixed-size deposit at pool-open: fee income = volume x
    lp_fee share x liquidity share, and impermanent loss from the
    price ratio (constant-product IL),
 4. reports income vs IL vs the creator's own annuity.

Honest limits, stated up front: today's fee config is applied to the
tape's 24h volumes (the schedule is dynamic and may have differed);
opening liquidity is reconstructed from trade deltas, not snapshots;
second-order effects (the deposit's own impact on the pool) are ignored.
Nothing here moves funds or authorizes a transaction.
"""

import argparse
import asyncio
import json
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.fee_schedule import (  # noqa: E402
    PumpFeeSnapshot,
    decode_fee_config_account,
)
from platforms.pumpfun.pumpswap import (  # noqa: E402
    WSOL_MINT,
    PumpSwapAddresses,
    _decode_pool_account,
    _fees_for_market_cap,
)

WSOL = WSOL_MINT  # already a Pubkey in the pumpswap module
TAPE = REPO / "learning-examples/token-lifecycles/lifecycles_24h.jsonl"
SYSTEM = "BwWK17cb"
SWEEP_MIN_SOL = 20
DEPOSIT_SOL = 10.0


def load_cohort() -> list[dict]:
    """Whale-swept graduated coins with pool trades, richest volume first."""
    cohort = []
    with TAPE.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("graduated_dslot") is None or row.get("partial"):
                continue
            trades = row["trades"]
            if isinstance(trades, str):
                trades = json.loads(trades)
            post = row["post_trades"]
            if isinstance(post, str):
                post = json.loads(post)
            if not post:
                continue
            buys: dict[str, float] = defaultdict(float)
            for t in trades:
                if t[2] == 1 and not t[1].startswith(SYSTEM):
                    buys[t[1]] += t[3]
            if not buys:
                continue
            whale, swept = max(buys.items(), key=lambda kv: kv[1])
            if swept < SWEEP_MIN_SOL * 1e9:
                continue
            volume = sum(t[3] for t in post if t[3] > 0) / 1e9
            cohort.append(
                {
                    "mint": row["mint"],
                    "whale": whale,
                    "swept_sol": swept / 1e9,
                    "volume_sol": volume,
                    "self_sweep": whale == row["creator"],
                    "post": post,
                }
            )
    cohort.sort(key=lambda c: -c["volume_sol"])
    return cohort


def pool_path(post: list) -> tuple[float, float, float]:
    """(opening liquidity SOL, closing SOL, price ratio end/start) from deltas."""
    net = 0.0
    first_price = None
    last_price = None
    for t in post:
        sol, tok = t[3], t[4]
        net += sol if t[2] == 1 else -sol
        if sol > 0 and tok > 0:
            price = sol / tok
            if first_price is None:
                first_price = price
            last_price = price
    opening = max(net, 0.0)
    ratio = (last_price / first_price) if first_price and last_price else 0.0
    return opening, max(net, 0.0), ratio


def il_fraction(price_ratio: float) -> float:
    """Constant-product impermanent loss for a price ratio k (end/start)."""
    if price_ratio <= 0:
        return -1.0
    k = price_ratio
    return 2 * (k**0.5) / (1 + k) - 1


def vault_amount(raw: bytes) -> int:
    """SPL/Token-2022 vault balance: the u64 at offset 64."""
    return struct.unpack_from("<Q", raw, 64)[0]


async def simulate_pool(client: SolanaClient, coin: dict) -> dict | None:
    """Read one pool's live state and simulate the deposit. None = skip."""
    mint = Pubkey.from_string(coin["mint"])
    pool = PumpSwapAddresses.derive_canonical_pool(mint, WSOL)
    fee_config_pda, _ = Pubkey.find_program_address(
        [b"fee_config", bytes(PumpFunAddresses.PROGRAM)],
        PumpFunAddresses.FEE_PROGRAM,
    )
    accounts = await client.get_multiple_accounts(
        [pool, fee_config_pda], commitment="processed"
    )
    pool_v, fee_v = accounts
    if pool_v is None or fee_v is None:
        return None
    decoded = _decode_pool_account(pool_v, pool, mint, WSOL)
    # Direct decode, no attestation ceremony: this is a simulation,
    # the fee bps are the account's current on-chain values.
    import time

    snapshot = PumpFeeSnapshot(
        config=decode_fee_config_account(fee_v),
        observed_at=time.time(),
        attested_at=time.time(),
    )
    opening, _closing, price_ratio = pool_path(coin["post"])
    if opening <= 0:
        return None

    vault_accounts = await client.get_multiple_accounts(
        [decoded.base_vault, decoded.quote_vault], commitment="processed"
    )
    base_v, quote_v = vault_accounts
    if base_v is None or quote_v is None:
        return None
    base_reserve = vault_amount(bytes(base_v.data))
    quote_reserve = vault_amount(bytes(quote_v.data))
    if base_reserve <= 0 or quote_reserve <= 0:
        return None

    price_sol = quote_reserve / 1e9 / (base_reserve / 1e6)
    market_cap_raw = int(
        price_sol * 1e9 * 1e9
    )  # price_sol (SOL) as lamports x 1e9 supply
    fees = _fees_for_market_cap(snapshot, market_cap_raw, WSOL)
    share = DEPOSIT_SOL / (opening + DEPOSIT_SOL)
    income = coin["volume_sol"] * fees.lp_fee_bps / 10_000 * share
    return {
        "mint": coin["mint"][:8],
        "vol": coin["volume_sol"],
        "lp_bps": fees.lp_fee_bps,
        "share": share,
        "income": income,
        "il": il_fraction(price_ratio),
        "pool_liq": quote_reserve / 1e9,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=None, help="RPC endpoint override")
    parser.add_argument("--n", type=int, default=25, help="pools to sample")
    args = parser.parse_args()

    rpc = args.rpc
    if rpc is None:
        for line in (REPO / ".state/configs/capital-research-rpc.env").open():
            if line.startswith("SOLANA_NODE_RPC_ENDPOINT="):
                rpc = line.split("=", 1)[1].strip().strip("\"'")
    client = SolanaClient(rpc)

    cohort = load_cohort()
    self_sweeps = [c for c in cohort if c["self_sweep"]]
    sample = self_sweeps[: args.n]
    print(
        f"tape: {len(cohort)} whale-swept pools ({len(self_sweeps)} self-sweeps); "
        f"simulating {len(sample)} richest-volume pools, deposit {DEPOSIT_SOL} SOL"
    )

    rows = []
    for coin in sample:
        try:
            row = await simulate_pool(client, coin)
        except Exception as exc:  # noqa: BLE001 - per-pool failures skip
            print(f"  skip {coin['mint'][:8]}: {type(exc).__name__}: {exc}")
            continue
        if row is not None:
            rows.append(row)

    await client.close()
    if not rows:
        print("no pools simulated")
        return
    inc = [r["income"] for r in rows]
    print(f"simulated {len(rows)} pools, deposit {DEPOSIT_SOL} SOL at pool-open:")
    lo_i, hi_i = sorted(inc)[len(inc) // 4], sorted(inc)[3 * len(inc) // 4]
    print(
        f"  fee income/24h: median {statistics.median(inc):.3f} SOL "
        f"(IQR {lo_i:.3f}-{hi_i:.3f})"
    )
    print(
        f"  median lp_fee_bps: {statistics.median(r['lp_bps'] for r in rows):.0f}"
        f" | median liquidity share: {statistics.median(r['share'] for r in rows):.2%}"
    )
    print(
        "  median price ratio end/open: "
        f"{statistics.median(r['il'] for r in rows):.3f} as IL fraction "
        "(constant-product, from per-trade prices)"
    )
    print("  annuity baseline (creator's own game): 9-17 SOL/day on 85 SOL swept")


if __name__ == "__main__":
    asyncio.run(main())
