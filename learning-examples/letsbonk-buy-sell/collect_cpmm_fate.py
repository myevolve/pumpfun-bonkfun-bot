"""Collect migrated letsbonk pool fate snapshots — READ-ONLY, no funds.

The letsbonk fee census measured: creator 0 bps, migration LP 100% to the
platform (StonkFun), and the migrated pool is a Raydium CPMM whose trade
fees accrue to LPs pro-rata — the first census venue where the follower's
LP route is mechanically open. The open question is economic: does the
post-graduation decay that killed every pump-side follower role also kill
LP income here?

Methodology (verified live, this exact code ran on 2026-10-09):
 1. LaunchLab PoolState is 429 bytes; status==2 (MIGRATED) sits at byte 17.
    memcmp filter bytes are BASE-58 encoded ("3" = 0x02, NOT "2").
 2. The full cohort (17,878 pools) blows past the public endpoint's 413 on
    one GPA; two dataSlice passes (0:96 for fund, 213:245 for mint) keep
    each response small. The whale band is fund 60-1000 SOL (the 85-SOL
    self-sweep median lives here); 16,248 of 17,878 are dust (<1 SOL).
 3. Per pool: DexScreener token-pairs for the migrated CPMM pair — price,
    liquidity, 24h volume, pair age.
 4. Re-running this script appends another snapshot; the deltas between
    snapshots are the LP verdict's inputs (decay, volume, liquidity).

Nothing here moves funds or authorizes a transaction.
"""

import argparse
import asyncio
import json
import struct
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from solana.rpc.types import DataSliceOpts, MemcmpOpts  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from platforms.letsbonk.address_provider import LetsBonkAddresses  # noqa: E402

OUT = REPO / "learning-examples/letsbonk-buy-sell/cpmm_fate_snapshots.jsonl"
LAUNCHLAB = LetsBonkAddresses.PROGRAM
STATUS_MIGRATED = 2  # LaunchLabPoolStatus.MIGRATED (byte 17)
# base58 encoding of a single 0x02 byte: the alphabet maps '2' -> 1, '3' -> 2
STATUS_MIGRATED_B58 = "3"
FUND_OFFSET = 77  # on-chain = IDL offset 69 + 8-byte anchor
MINT_OFFSET = 213  # base_mint (IDL 205 + 8)
BAND_MIN_SOL = 60.0
BAND_MAX_SOL = 1000.0
DEXSCREENER = "https://api.dexscreener.com/token-pairs/v1/solana/"


def rpc_from_env() -> str:
    for line in (REPO / ".state/configs/capital-research-rpc.env").open():
        if line.startswith("SOLANA_NODE_RPC_ENDPOINT"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no RPC endpoint in the service-only projection")


def dexscreener_pair(base_mint: str) -> dict | None:
    req = urllib.request.Request(
        DEXSCREENER + base_mint,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            pairs = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - aggregator outages censor, never block
        return None
    if not pairs:
        return None
    pairs.sort(key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
    p = pairs[0]
    return {
        "dex": p.get("dexId"),
        "pair": p.get("pairAddress"),
        "price_usd": p.get("priceUsd"),
        "liq_usd": (p.get("liquidity") or {}).get("usd"),
        "vol24_usd": (p.get("volume") or {}).get("h24"),
        "txns24": ((p.get("txns") or {}).get("h24") or {}).get("buys", 0)
        + ((p.get("txns") or {}).get("h24") or {}).get("sells", 0),
        "created": p.get("pairCreatedAt"),
    }


def decode_cpswap_config(buf: bytes) -> int:
    """Post-migration trade fee (hundredths of a bip): anchor + epoch + rate."""
    return struct.unpack_from("<Q", buf, 16)[0]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", default=None)
    parser.add_argument("--limit", type=int, default=0, help="cap DEX queries")
    args = parser.parse_args()
    rpc = args.rpc or rpc_from_env()
    client = SolanaClient(rpc)
    try:
        raw = await client.get_client()
        fund_accs = await raw.get_program_accounts(
            LAUNCHLAB,
            encoding="base64",
            filters=[429, MemcmpOpts(offset=17, bytes=STATUS_MIGRATED_B58)],
            data_slice=DataSliceOpts(offset=0, length=FUND_OFFSET + 8),
        )
        mint_accs = await raw.get_program_accounts(
            LAUNCHLAB,
            encoding="base64",
            filters=[429, MemcmpOpts(offset=17, bytes=STATUS_MIGRATED_B58)],
            data_slice=DataSliceOpts(offset=MINT_OFFSET, length=32),
        )
        mint_by_pool = {
            str(a.pubkey): str(Pubkey.from_bytes(bytes(a.account.data)))
            for a in mint_accs.value
        }
        band = [
            a
            for a in fund_accs.value
            if BAND_MIN_SOL
            <= struct.unpack_from("<Q", bytes(a.account.data), FUND_OFFSET)[0] / 1e9
            <= BAND_MAX_SOL
        ]
        print(f"migrated cohort: {len(fund_accs.value)}, whale band: {len(band)}")
        if args.limit:
            band = band[: args.limit]
        n_pair = 0
        with OUT.open("a") as out:
            for a in band:
                pool = str(a.pubkey)
                fund = (
                    struct.unpack_from("<Q", bytes(a.account.data), FUND_OFFSET)[0]
                    / 1e9
                )
                mint = mint_by_pool.get(pool, "")
                if len(mint) < 30:
                    continue
                time.sleep(1.2)  # DexScreener free tier: 60 req/min
                pair = dexscreener_pair(mint)
                n_pair += pair is not None
                out.write(
                    json.dumps(
                        {
                            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "pool": pool,
                            "mint": mint,
                            "fund_sol": round(fund, 1),
                            "pair": pair,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        print(f"DEX pairs found: {n_pair}/{len(band)} -> {OUT.name}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
