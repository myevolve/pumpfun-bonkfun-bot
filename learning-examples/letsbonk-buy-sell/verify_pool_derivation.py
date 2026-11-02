# ruff: noqa: PLR2004, S101, TRY003
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
    uv run learning-examples/letsbonk-buy-sell/verify_pool_derivation.py --self-check
    uv run learning-examples/letsbonk-buy-sell/verify_pool_derivation.py \
        [--rpc-env .state/configs/capital-research-rpc.env] [--samples 25]
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

import aiohttp
from solders.pubkey import Pubkey

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from platforms.letsbonk.address_provider import (  # noqa: E402
    LetsBonkAddresses,
    LetsBonkAddressProvider,
)

POOL_STATE_DISCRIMINATOR = bytes([247, 237, 227, 245, 215, 195, 222, 70])
BASE_MINT_OFFSET = 205
QUOTE_MINT_OFFSET = 237
STATUS_OFFSET = 17
FUND_OFFSET = 69
POOL_STATE_SIZE = 429
SAMPLE_SIZE = 64
PUBLIC_ENDPOINT = "https://api.mainnet-beta.solana.com"
PROGRAM = LetsBonkAddresses.PROGRAM
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
USD1_MINT = Pubkey.from_string("USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB")

_PRIMITIVE_SIZES: dict[str, int] = {
    "u8": 1,
    "u16": 2,
    "u32": 4,
    "u64": 8,
    "i8": 1,
    "i16": 2,
    "i32": 4,
    "i64": 8,
    "bool": 1,
    "pubkey": 32,
}


def derive_pool_state_for_token(
    base_token_mint: Pubkey, quote_mint: Pubkey = WSOL_MINT
) -> Pubkey:
    """Derive the LaunchLab pool state PDA for `(base_token_mint, quote_mint)`."""
    pool_state_pda, _ = Pubkey.find_program_address(
        [b"pool", bytes(base_token_mint), bytes(quote_mint)], PROGRAM
    )
    return pool_state_pda


def _idl_type_size(type_spec: str | dict[str, Any], types_map: dict[str, Any]) -> int:
    if isinstance(type_spec, str):
        return _PRIMITIVE_SIZES[type_spec]
    if "array" in type_spec:
        elem_type, length = type_spec["array"]
        return _idl_type_size(elem_type, types_map) * int(length)
    if "defined" in type_spec:
        defined = type_spec["defined"]
        name = defined["name"] if isinstance(defined, dict) else defined
        defined_type = types_map[name]["type"]
        if defined_type["kind"] == "struct":
            return sum(
                _idl_type_size(field["type"], types_map)
                for field in defined_type["fields"]
            )
        if defined_type["kind"] == "enum":
            max_variant = 0
            for variant in defined_type.get("variants", []):
                fields = variant.get("fields", [])
                variant_size = sum(
                    _idl_type_size(
                        f["type"] if isinstance(f, dict) else f,
                        types_map,
                    )
                    for f in fields
                )
                max_variant = max(max_variant, variant_size)
            return 1 + max_variant
    raise ValueError(f"Unsupported IDL type spec: {type_spec!r}")


def compute_pool_state_layout(
    idl_path: Path | None = None,
) -> tuple[dict[str, int], int]:
    """Compute on-chain PoolState field byte offsets (including 8-byte discriminator)."""
    path = idl_path or (ROOT / "idl" / "raydium_launchlab_idl.json")
    idl = json.loads(path.read_text())
    types_map = {entry["name"]: entry for entry in idl.get("types", [])}
    pool_state_fields = types_map["PoolState"]["type"]["fields"]
    offset = len(POOL_STATE_DISCRIMINATOR)
    offsets: dict[str, int] = {}
    for field in pool_state_fields:
        offsets[field["name"]] = offset
        offset += _idl_type_size(field["type"], types_map)
    return offsets, offset


def _load_script_derive_fn(script_path: Path) -> Any:
    tree = ast.parse(script_path.read_text(), filename=str(script_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and (
            node.name == "derive_pool_state_for_token"
        ):
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict[str, Any] = {
                "Pubkey": Pubkey,
                "WSOL_MINT": WSOL_MINT,
                "RAYDIUM_LAUNCHLAB_PROGRAM_ID": PROGRAM,
            }
            exec(compile(module, str(script_path), "exec"), namespace)  # noqa: S102
            return namespace["derive_pool_state_for_token"]
    raise AssertionError(f"derive_pool_state_for_token missing in {script_path}")


def self_check() -> int:
    """Run offline IDL layout and pool derivation self-checks without network I/O."""
    offsets, total_size = compute_pool_state_layout()
    assert offsets["base_mint"] == BASE_MINT_OFFSET == 205
    assert offsets["quote_mint"] == QUOTE_MINT_OFFSET == 237
    assert offsets["status"] == STATUS_OFFSET == 17
    assert offsets["total_quote_fund_raising"] == FUND_OFFSET == 69
    assert total_size == POOL_STATE_SIZE == 429

    provider = LetsBonkAddressProvider()
    sample_base = Pubkey.from_string("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
    expected_wsol = provider.derive_pool_address(sample_base, WSOL_MINT)
    expected_usd1 = provider.derive_pool_address(sample_base, USD1_MINT)

    assert derive_pool_state_for_token(sample_base) == expected_wsol
    assert derive_pool_state_for_token(sample_base, WSOL_MINT) == expected_wsol
    assert derive_pool_state_for_token(sample_base, USD1_MINT) == expected_usd1
    assert expected_wsol != expected_usd1

    example_dir = Path(__file__).resolve().parent
    for script_name in (
        "manual_buy_exact_in.py",
        "manual_sell_exact_in.py",
        "manual_buy_exact_out.py",
        "manual_sell_exact_out.py",
    ):
        fn = _load_script_derive_fn(example_dir / script_name)
        assert fn(sample_base) == expected_wsol
        assert fn(sample_base, WSOL_MINT) == expected_wsol
        assert fn(sample_base, USD1_MINT) == expected_usd1

    print(
        json.dumps(
            {
                "self_check": "ok",
                "offsets": {
                    "status": offsets["status"],
                    "total_quote_fund_raising": offsets["total_quote_fund_raising"],
                    "base_mint": offsets["base_mint"],
                    "quote_mint": offsets["quote_mint"],
                },
                "pool_state_size": total_size,
                "wsol_pool": str(expected_wsol),
                "usd1_pool": str(expected_usd1),
            },
            sort_keys=True,
        )
    )
    return 0


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
                        "offset": BASE_MINT_OFFSET,
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
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--rpc-env", default=None)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    if args.self_check:
        raise SystemExit(self_check())
    endpoint = load_endpoint(args.rpc_env)
    raise SystemExit(asyncio.run(verify(endpoint, args.samples)))


if __name__ == "__main__":
    main()
