"""Verify Pump.fun dynamic-fee decoding, quotes, and attestation safely.

The default mode is completely offline. ``--live`` reads mainnet state and
simulates unsigned trade transactions, but never signs or submits one.

Usage:
    uv run learning-examples/verify_pump_fee_schedule.py
    uv run learning-examples/verify_pump_fee_schedule.py --live
"""
# Verifier failures intentionally include the exact violated contract.
# ruff: noqa: TRY003

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import struct
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import aiohttp  # noqa: E402
from simulate_v2_trades import simulate_mint  # noqa: E402
from solders.account import Account  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import (  # noqa: E402
    USDC_MINT,
    WSOL_MINT,
    normalize_quote_mint,
)
from interfaces.core import Platform  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.fee_schedule import (  # noqa: E402
    FEE_CONFIG_DISCRIMINATOR,
    GET_FEES_DISCRIMINATOR,
    PumpFees,
    PumpFeeSchedule,
    PumpFeeSnapshot,
    decode_fee_config_account,
    quote_buy_exact_in,
    quote_buy_exact_out,
    quote_sell_exact_in,
)

FeeValues = tuple[int, int, int]
TierValues = tuple[int, FeeValues]
REGULAR_TIERS: tuple[TierValues, ...] = (
    (0, (0, 95, 30)),
    (100_000, (0, 80, 20)),
)
STABLE_TIERS: tuple[TierValues, ...] = (
    (0, (0, 50, 10)),
    (100_000, (0, 40, 5)),
)
PUMP_COINS_URL = (
    "https://frontend-api-v3.pump.fun/coins"
    "?offset={offset}&limit=50&sort=created_timestamp&order=DESC"
    "&includeNsfw=false"
)
_GET_FEES_IX_SIZE = 34


def _encode_fees(fees: FeeValues) -> bytes:
    return struct.pack("<QQQ", *fees)


def _encode_tiers(tiers: tuple[TierValues, ...]) -> bytes:
    encoded = bytearray(struct.pack("<I", len(tiers)))
    for threshold, fees in tiers:
        encoded += threshold.to_bytes(16, "little")
        encoded += _encode_fees(fees)
    return bytes(encoded)


def _fee_account() -> Account:
    data = bytearray(FEE_CONFIG_DISCRIMINATOR)
    data += bytes((253,))
    data += bytes(Pubkey.from_string("11111111111111111111111111111112"))
    data += _encode_fees((25, 90, 20))
    data += _encode_tiers(REGULAR_TIERS)
    data += _encode_tiers(STABLE_TIERS)
    data += bytes(128)
    return Account(
        lamports=1,
        data=bytes(data),
        owner=PumpFunAddresses.FEE_PROGRAM,
        executable=False,
        rent_epoch=0,
    )


def _state(
    market_cap_raw: int,
    quote_mint: Pubkey,
    *,
    creator: Pubkey | None = None,
) -> dict[str, object]:
    return {
        "virtual_token_reserves": 1_000_000,
        "virtual_quote_reserves": market_cap_raw,
        "real_token_reserves": 900_000,
        "real_quote_reserves": 1_000_000,
        "token_total_supply": 1_000_000,
        "complete": False,
        "creator": creator
        if creator is not None
        else Pubkey.from_string("11111111111111111111111111111112"),
        "quote_mint": quote_mint,
    }


def _expected_fees(market_cap_raw: int, *, is_usdc: bool) -> PumpFees:
    tiers = STABLE_TIERS if is_usdc else REGULAR_TIERS
    selected = tiers[0][1]
    for threshold, fees in reversed(tiers):
        if market_cap_raw >= threshold:
            selected = fees
            break
    return PumpFees(*selected)


class _AttestationClient:
    """Answer get_fees simulations from fixed independent fixture values."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self.instructions: list[bytes] = []

    async def get_account_info(
        self,
        pubkey: Pubkey,
        commitment: str | None = None,
    ) -> Account:
        del pubkey, commitment
        return self.account

    async def get_latest_blockhash(self) -> Hash:
        return Hash.default()

    async def post_rpc(self, body: dict[str, Any]) -> dict[str, Any]:
        transaction = Transaction.from_bytes(
            base64.b64decode(body["params"][0], validate=True)
        )
        data = bytes(transaction.message.instructions[0].data)
        self.instructions.append(data)
        if len(data) != _GET_FEES_IX_SIZE or data[:8] != GET_FEES_DISCRIMINATOR:
            raise AssertionError(f"unexpected get_fees payload: {data.hex()}")
        market_cap_raw = int.from_bytes(data[9:25], "little")
        trade_size_raw = struct.unpack("<Q", data[25:33])[0]
        is_usdc = bool(data[33])
        if data[8] != 1 or trade_size_raw not in (1, 1_000_000_000):
            raise AssertionError(f"unexpected get_fees arguments: {data.hex()}")
        fees = _expected_fees(market_cap_raw, is_usdc=is_usdc)
        encoded = base64.b64encode(
            struct.pack(
                "<QQQ",
                fees.lp_fee_bps,
                fees.protocol_fee_bps,
                fees.creator_fee_bps,
            )
        ).decode()
        return {
            "result": {
                "value": {
                    "err": None,
                    "logs": [
                        f"Program return: {PumpFunAddresses.FEE_PROGRAM} {encoded}"
                    ],
                }
            }
        }


def _check_quote_vectors(snapshot: PumpFeeSnapshot) -> None:
    expected = {
        (WSOL_MINT, 99_999): (PumpFees(0, 95, 30), 9_876, 89_875),
        (WSOL_MINT, 100_000): (PumpFees(0, 80, 20), 9_900, 90_073),
        (WSOL_MINT, 100_001): (PumpFees(0, 80, 20), 9_900, 90_072),
        (USDC_MINT, 99_999): (PumpFees(0, 50, 10), 9_940, 90_405),
        (USDC_MINT, 100_000): (PumpFees(0, 40, 5), 9_955, 90_528),
        (USDC_MINT, 100_001): (PumpFees(0, 40, 5), 9_955, 90_527),
    }
    for (quote_mint, market_cap_raw), vector in expected.items():
        quote = quote_buy_exact_in(
            _state(market_cap_raw, quote_mint),
            10_000,
            snapshot,
        )
        actual = (quote.fees, quote.net_quote_raw, quote.amount_out_raw)
        if actual != vector:
            raise AssertionError(
                f"buy vector mismatch for {quote_mint} at {market_cap_raw}: "
                f"expected {vector}, got {actual}"
            )

    exact_out = quote_buy_exact_out(
        _state(100_000, WSOL_MINT),
        10_000,
        snapshot,
    )
    if (
        exact_out.amount_in_raw,
        exact_out.net_quote_raw,
        exact_out.protocol_fee_raw,
        exact_out.creator_fee_raw,
    ) != (1_024, 1_012, 9, 3):
        raise AssertionError(f"exact-output vector mismatch: {exact_out}")

    sell = quote_sell_exact_in(
        _state(100_000, WSOL_MINT),
        10_000,
        snapshot,
    )
    if (
        sell.amount_out_raw,
        sell.gross_quote_raw,
        sell.protocol_fee_raw,
        sell.creator_fee_raw,
    ) != (980, 990, 8, 2):
        raise AssertionError(f"sell vector mismatch: {sell}")

    no_creator = quote_buy_exact_in(
        _state(100_000, WSOL_MINT, creator=Pubkey.default()),
        10_000,
        snapshot,
    )
    if (
        no_creator.fees,
        no_creator.net_quote_raw,
        no_creator.amount_out_raw,
    ) != (PumpFees(0, 80, 0), 9_920, 90_239):
        raise AssertionError(f"default-creator vector mismatch: {no_creator}")


async def _verify_offline() -> None:
    account = _fee_account()
    config = decode_fee_config_account(account)
    if tuple(
        (tier.market_cap_threshold_raw, tier.fees) for tier in config.regular_tiers
    ) != tuple((threshold, PumpFees(*fees)) for threshold, fees in REGULAR_TIERS):
        raise AssertionError("regular FeeConfig tiers did not round-trip")
    if tuple(
        (tier.market_cap_threshold_raw, tier.fees) for tier in config.stable_tiers
    ) != tuple((threshold, PumpFees(*fees)) for threshold, fees in STABLE_TIERS):
        raise AssertionError("stable FeeConfig tiers did not round-trip")

    snapshot = PumpFeeSnapshot(config, observed_at=0.0, attested_at=0.0)
    _check_quote_vectors(snapshot)

    client = _AttestationClient(account)
    schedule = PumpFeeSchedule(
        client,  # type: ignore[arg-type]
        PumpFunAddresses.find_fee_config(),
        PumpFunAddresses.PROGRAM,
        poll_interval_seconds=3_600.0,
    )
    await schedule.start()
    try:
        attested = schedule.require_snapshot()
        if attested.config.digest != config.digest:
            raise AssertionError("attested FeeConfig digest changed")
    finally:
        await schedule.close()

    expected_payloads = {
        GET_FEES_DISCRIMINATOR
        + bytes((1,))
        + threshold.to_bytes(16, "little")
        + struct.pack("<Q", trade_size_raw)
        + bytes((int(is_usdc),))
        for is_usdc, tiers in ((False, REGULAR_TIERS), (True, STABLE_TIERS))
        for threshold, _fees in tiers
        for trade_size_raw in (1, 1_000_000_000)
    }
    if set(client.instructions) != expected_payloads:
        raise AssertionError("get_fees attestation probes were incomplete")
    print("PASS strict FeeConfig layout and WSOL/USDC tier decoding")
    print("PASS threshold, exact-in, exact-out, sell, and creator quote vectors")
    print("PASS get_fees instruction encoding and all-tier attestation probes")


async def _discover_live_mints() -> dict[Pubkey, list[Pubkey]]:
    """Discover recent active WSOL and USDC bonding curves."""
    timeout = aiohttp.ClientTimeout(total=15.0)
    payload: list[object] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for offset in (250, 500, 1_000):
            async with session.get(PUMP_COINS_URL.format(offset=offset)) as response:
                response.raise_for_status()
                page = await response.json()
            if not isinstance(page, list):
                raise TypeError("Pump coin discovery returned an invalid response")
            payload.extend(page)
    discovered: dict[Pubkey, list[Pubkey]] = {
        WSOL_MINT: [],
        USDC_MINT: [],
    }
    for coin in payload:
        if (
            not isinstance(coin, dict)
            or coin.get("complete") is not False
            or coin.get("program") != "pump"
        ):
            continue
        try:
            mint = Pubkey.from_string(coin["mint"])
            quote_mint = normalize_quote_mint(Pubkey.from_string(coin["quote_mint"]))
        except (KeyError, TypeError, ValueError):
            continue
        candidates = discovered.get(quote_mint)
        if candidates is not None:
            candidates.append(mint)

    missing = [
        "WSOL" if quote_mint == WSOL_MINT else "USDC"
        for quote_mint, candidates in discovered.items()
        if not candidates
    ]
    if missing:
        raise RuntimeError(
            "Pump coin discovery found no active " + "/".join(missing) + " curve"
        )
    return discovered


async def _simulate_discovered_asset(
    client: SolanaClient,
    label: str,
    candidates: list[Pubkey],
) -> Pubkey:
    """Try recent candidates until one remains active through simulation."""
    payer = PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    failures: list[str] = []
    for mint in candidates[:20]:
        try:
            if await simulate_mint(client, payer, mint, slippage_bps=0):
                return mint
            failures.append(f"{mint}: simulation returned a program error")
        except Exception as exc:  # noqa: BLE001 - try the next live candidate
            failures.append(f"{mint}: {type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"No discovered {label} curve passed read-only buy+sell simulation: "
        + "; ".join(failures)
    )


async def _verify_live() -> None:
    endpoint = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")
    if not endpoint:
        raise RuntimeError(
            "SOLANA_NODE_RPC_ENDPOINT must be exported for --live; "
            "this verifier does not read .env"
        )
    client = SolanaClient(endpoint)
    implementations = get_platform_implementations(Platform.PUMP_FUN, client)
    curve_manager = implementations.curve_manager
    try:
        await curve_manager.prepare_live_execution()
        snapshot = curve_manager.fee_schedule.require_snapshot()
        print(
            "PASS live FeeConfig attestation "
            f"digest={snapshot.config.digest} "
            f"regular_tiers={len(snapshot.config.regular_tiers)} "
            f"stable_tiers={len(snapshot.config.stable_tiers)}"
        )

        discovered = await _discover_live_mints()
        wsol_mint = await _simulate_discovered_asset(
            client,
            "WSOL",
            discovered[WSOL_MINT],
        )
        print(f"PASS live WSOL fee-aware buy+sell simulation mint={wsol_mint}")
        usdc_mint = await _simulate_discovered_asset(
            client,
            "USDC",
            discovered[USDC_MINT],
        )
        print(f"PASS live USDC fee-aware buy+sell simulation mint={usdc_mint}")
    finally:
        await curve_manager.close()
        await client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="read and attest the mainnet FeeConfig via simulateTransaction",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    await _verify_offline()
    if args.live:
        await _verify_live()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except Exception as exc:  # noqa: BLE001 - report verifier failures uniformly
        print(f"FAIL {type(exc).__name__}: {exc}")
        sys.exit(1)
