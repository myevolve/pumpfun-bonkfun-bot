"""Verify the LaunchLab pool derivation order against live PoolState accounts.

LaunchLab (letsbonk) pool seeds are `[b"pool", base_mint, quote_mint]` —
confirmed 2026-10-06 on live PoolState accounts (issue #214 follow-up).
This verifier machine-checks the order against live chain state:
1. `getProgramAccounts` over the LaunchLab program, filtered to PoolState
   discriminators, sliced to the two seed-source fields (offset 205: base,
   offset 237: quote).
2. For every sampled pool: derive `[b"pool", base, quote]` and require it to
   reproduce the account's own address, and require the swapped
   `[b"pool", quote, base]` order NOT to (guards a regression).

Read-only network access; no keys, no signing, no funds. The full scan is
~1.5M accounts; a dedicated RPC endpoint (via `--rpc-env`) is strongly
recommended — the public endpoint works but is slow and rate-limited.

Usage:
    uv run learning-examples/letsbonk-buy-sell/verify_pool_derivation.py \
        [--rpc-env .state/configs/capital-research-rpc.env] [--samples 25]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import aiohttp
from solders.pubkey import Pubkey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from platforms.letsbonk.address_provider import LetsBonkAddresses  # noqa: E402

POOL_STATE_DISCRIMINATOR = bytes([247, 237, 227, 245, 215, 195, 222, 70])
QUOTE_OFFSET = 205
BASE_OFFSET = 237
SAMPLE_SIZE = 64
PUBLIC_ENDPOINT = "https://api.mainnet-beta.solana.com"
PROGRAM = LetsBonkAddresses.PROGRAM


def _base58(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, rem = divmod(number, 58)
        encoded = alphabet[rem] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + encoded


def load_endpoint(rpc_env: str | None) -> str:
    if not rpc_env:
        return PUBLIC_ENDPOINT
    for line in Path(rpc_env).read_text().splitlines():
        if line.startswith("SOLANA_NODE_RPC_ENDPOINT="):
            endpoint = line.split("=", 1)[1].strip().strip('"').strip("'")
            if endpoint.startswith("http"):
                return endpoint
    raise SystemExit("rpc_env_missing_endpoint")


async def verify(endpoint: str, samples: int) -> int:
    async with aiohttp.ClientSession() as session:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts",
            "params": [
                str(PROGRAM),
                {
                    "encoding": "base64",
                    "filters": [
                        {
                            "memcmp": {
                                "offset": 0,
                                "bytes": _base58(POOL_STATE_DISCRIMINATOR),
                            }
                        }
                    ],
                    "dataSlice": {
                        "offset": QUOTE_OFFSET,
                        "length": SAMPLE_SIZE,
                    },
                },
            ],
        }
        async with session.post(endpoint, json=body) as response:
            payload = await response.json()
        accounts = payload.get("result")
        if not isinstance(accounts, list):
            raise SystemExit("GPA failed: " + json.dumps(payload)[:200])
        checked = skipped = 0
        for account in accounts[:samples] if samples else accounts:
            address = Pubkey.from_string(account["pubkey"])
            data = base64.b64decode(account["account"]["data"][0])
            if len(data) < SAMPLE_SIZE:
                skipped += 1
                continue
            # PoolState layout (2026-10-06, issue #214 follow-up): base_mint
            # at offset 205, quote_mint at offset 237 — the IDL's
            # VestingSchedule is 40 bytes, putting these two pubkeys exactly
            # at the slice's 0 and 32.
            base = Pubkey.from_bytes(data[0:32])
            quote = Pubkey.from_bytes(data[32:64])
            derived, _ = Pubkey.find_program_address(
                [b"pool", bytes(base), bytes(quote)], PROGRAM
            )
            if derived != address:
                print(f"FAIL {address}: derived {derived}")
                return 1
            reversed_attempt, _ = Pubkey.find_program_address(
                [b"pool", bytes(quote), bytes(base)], PROGRAM
            )
            if reversed_attempt == address:
                print(f"FAIL {address}: swapped [quote, base] order also matches")
                return 1
            checked += 1
        print(
            f"verified {checked} pools ({skipped} skipped): "
            "LaunchLab seeds are [b'pool', base, quote]"
        )
        return 0 if checked else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc-env", default=None)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    endpoint = load_endpoint(args.rpc_env)
    raise SystemExit(asyncio.run(verify(endpoint, args.samples)))


if __name__ == "__main__":
    main()
