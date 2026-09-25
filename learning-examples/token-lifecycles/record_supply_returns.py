"""Observe a frozen, hypothetical USDC supply position; never sign or send.

Simulates ONLY Kamino's transfer-free interest refresh against finalized state.
The public payer is used with an all-zero signature and sigVerify=False. No key,
wallet constructor, dotenv, deposit, redemption or account creation is used.

A 50-USDC warm-account benchmark is independent of the existing paper portfolio.
It is not a funded position or a fill claim. Acquisition/off-ramp costs remain
unknown. Native source: Kamino klend a08760976f51a3a58c4a0c6ea27b4a0e565bca79;
SDK layout: 38845294447623f6de3afc9dec29875f959f6f48. No binary attestation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

# Standalone public-data study: protocol constants and explicit offline assertions.
# ruff: noqa: PLR2004, S101, TRY003
PROGRAM = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
MARKET = "7u3HeHxYDLhnCoErrtycNokbQYbWGzLs6JSDqGAv5PfF"
RESERVE = "D6q6wuQSrifJKZYpR1M8R4YawnLDtDsMmWM1NbBmgJ59"
COLLATERAL = "B8V6WVjPxW1UGwVDfxH2d2r8SyT4cqn7dQRK6XneVa7D"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_RESERVE = "d4A2prbA2whesmvHaL88BH6Ewn5N4bTSU2Ze8P6Bc4Q"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105 -- public SPL program ID, not a credential
PUBLIC_PAYER = "9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe"
RPC = "https://api.mainnet-beta.solana.com"
SCALE = 1 << 60
PRINCIPAL = 50_000_000
RENT = 1_488_440  # getMinimumBalanceForRentExemption(165), 2026-09-16.
INTERVAL = 300
TARGET_SECONDS = 7 * 86400
MAX_BYTES = 128 * 1024 * 1024
FEE_CASES = {
    "base_fee_floor": 10_000,
    "priority_allowance": 30_000,
    "paper_cost_budget": 85_000,
}
MARKET_AUTHORITY, _ = Pubkey.find_program_address(
    [b"lma", bytes(Pubkey.from_string(MARKET))], Pubkey.from_string(PROGRAM)
)


def integer(data: bytes, offset: int, size: int = 8, *, signed: bool = False) -> int:
    """Decode a checked account's little-endian integer without float conversion."""
    return int.from_bytes(data[offset : offset + size], "little", signed=signed)


def account(raw: dict, owner: str, length: int, discriminator: bytes = b"") -> bytes:
    """Refuse missing, malformed or differently owned account evidence."""
    if not raw or raw["owner"] != owner or raw["executable"]:
        raise ValueError("account identity/owner mismatch")
    if raw["data"][1] != "base64":
        raise ValueError("unexpected account encoding")
    data = base64.b64decode(raw["data"][0], validate=True)
    if len(data) != length or not data.startswith(discriminator):
        raise ValueError("account layout mismatch")
    return data


def reserve_account(raw: dict, mint: str) -> bytes:
    """Validate the pinned reserve family, market, underlying and token program."""
    data = account(raw, PROGRAM, 8624, bytes.fromhex("2bf2ccca1af73b7f"))
    if integer(data, 8) != 1:
        raise ValueError("unsupported reserve version")
    for offset, key in ((32, MARKET), (128, mint), (408, TOKEN_PROGRAM)):
        if data[offset : offset + 32] != bytes(Pubkey.from_string(key)):
            raise ValueError("reserve market/mint/token-program mismatch")
    return data


def capture() -> dict:
    """Execute one unsigned, no-transfer refresh simulation, retaining raw evidence."""
    instruction = Instruction(
        Pubkey.from_string(PROGRAM),
        bytes([144, 110, 26, 103, 162, 204, 252, 147, 1]),
        [
            AccountMeta(Pubkey.from_string(RESERVE), is_signer=False, is_writable=True),
            AccountMeta(Pubkey.from_string(MARKET), is_signer=False, is_writable=False),
        ],
    )
    message = MessageV0.try_compile(
        Pubkey.from_string(PUBLIC_PAYER), [instruction], [], Hash.default()
    )
    transaction = VersionedTransaction.populate(message, [Signature.default()])
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateTransaction",
        "params": [
            base64.b64encode(bytes(transaction)).decode(),
            {
                "encoding": "base64",
                "commitment": "finalized",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "accounts": {
                    "encoding": "base64",
                    "addresses": [RESERVE, MARKET, COLLATERAL, SOL_RESERVE],
                },
            },
        ],
    }
    started = time.time()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        RPC,
        data=json.dumps(request).encode(),
        headers={"Content-Type": "application/json"},
    )  # noqa: S310 -- fixed public HTTPS endpoint
    with opener.open(req, timeout=15) as response:
        if response.geturl() != RPC:
            raise ValueError("RPC origin changed")
        body = response.read(131073)
    if len(body) > 131072:
        raise ValueError("oversized RPC response")
    return {
        "started_at": started,
        "received_at": time.time(),
        "request": request,
        "raw_response": body.decode(),
        "response_sha256": hashlib.sha256(body).hexdigest(),
    }


def decode(evidence: dict) -> dict:  # noqa: C901, PLR0912, PLR0915 -- one native trust boundary
    """Read exact simulated native accrual, not a projected API APY."""
    response = json.loads(evidence["raw_response"])
    if "error" in response:
        raise ValueError(f"RPC refusal: {response['error']}")
    result = response["result"]
    value = result["value"]
    if value["err"] is not None:
        raise ValueError(f"refresh simulation failed: {value['err']}")
    raw_reserve, raw_market, raw_mint, raw_sol = value["accounts"]
    data = reserve_account(raw_reserve, USDC)
    market = account(raw_market, PROGRAM, 4664, bytes.fromhex("f6723262489d1c78"))
    mint = account(raw_mint, TOKEN_PROGRAM, 82)
    sol = reserve_account(raw_sol, WSOL)
    slot, stamp = integer(data, 16), integer(data, 28, 4)
    if slot != result["context"]["slot"] or data[24] != 0 or data[4865] != 1:
        raise ValueError("refresh did not produce coherent TrueAPR current-slot state")
    if integer(market, 8) != 1 or data[2560:2592] != bytes(
        Pubkey.from_string(COLLATERAL)
    ):
        raise ValueError("market version or collateral mint changed")
    supply = integer(data, 2592)
    # Holders may burn SPL tokens without redeeming; the program still uses its C.
    if (
        mint[44:46] != bytes([6, 1])
        or integer(mint, 36) > supply
        or integer(mint, 46, 4) != 0
    ):
        raise ValueError(
            "collateral mint decimals/initialization/supply/freeze mismatch"
        )
    if integer(mint, 0, 4) != 1 or mint[4:36] != bytes(MARKET_AUTHORITY):
        raise ValueError("collateral mint authority does not match market PDA")
    total = (
        integer(data, 224) * SCALE
        + integer(data, 232, 16)
        - sum(integer(data, offset, 16) for offset in (344, 360, 376))
    )
    if total <= 0 or supply <= 0:
        raise ValueError("nonpositive backing or share supply")
    rewards = [integer(data, 440), integer(data, 5792), integer(market, 3358, 2)]
    if any(rewards):
        raise ValueError("embedded rewards changed the interest-only experiment")
    if data[4856] != 0 or data[4862] != 0 or data[4864] != 0 or market[122] != 0:
        raise ValueError("supply/emergency configuration no longer supported")
    if integer(data, 5800) or integer(market, 3432):
        raise ValueError("permissioned configuration not supported")
    if total + PRINCIPAL * SCALE > integer(data, 5016) * SCALE:
        raise ValueError("deposit limit has insufficient room")
    capacity = integer(data, 5416, signed=True)
    used = integer(data, 5424, signed=True)
    start, duration = integer(data, 5432), integer(data, 5440)
    if duration and start > stamp:
        raise ValueError("withdrawal-cap clock is ahead of chain clock")
    remaining = (
        None
        if duration == 0
        else max(0, capacity - (used if stamp - start < duration else 0))
    )
    if duration and capacity < 0:
        remaining = 0
    free = max(0, integer(data, 224) - integer(data, 6968) * total // (supply * SCALE))
    usdc_price, sol_price = integer(data, 248, 16), integer(sol, 248, 16)
    price_ages = [stamp - integer(item, 264) for item in (data, sol)]
    # Native PriceStatusFlags::ALL_CHECKS: loaded/age/TWAP/heuristic/usage.
    if any(item[25] & 63 != 63 for item in (data, sol)):
        raise ValueError("native oracle checks did not all pass")
    if min(usdc_price, sol_price) <= 0 or any(
        age < 0 or age > 300 for age in price_ages
    ):
        raise ValueError("price marks missing, future-dated or older than five minutes")
    if abs(stamp - evidence["received_at"]) > 180:
        raise ValueError("finalized bank clock is too far from receipt time")
    return {
        "slot": slot,
        "timestamp": stamp,
        "net_liquidity_sf": str(total),
        "collateral_supply": supply,
        "mint_supply_shortfall_raw": supply - integer(mint, 36),
        "free_liquidity_raw": free,
        "withdrawal_allowance_raw": remaining,
        "usdc_price_sf": str(usdc_price),
        "sol_price_sf": str(sol_price),
        "price_ages_seconds": price_ages,
        "units_consumed": value["unitsConsumed"],
    }


def enter(mark: dict) -> dict:
    """Freeze floored shares and ceiled debit once, without funding a portfolio."""
    total, supply = int(mark["net_liquidity_sf"]), mark["collateral_supply"]
    shares = PRINCIPAL * supply * SCALE // total
    debit = (shares * total + supply * SCALE - 1) // (supply * SCALE)
    if shares <= 0 or debit > PRINCIPAL or mark["free_liquidity_raw"] < PRINCIPAL:
        raise ValueError("entry arithmetic or current liquidity failed")
    return {
        "mark": mark,
        "shares_raw": shares,
        "debit_raw": debit,
        "unspent_raw": PRINCIPAL - debit,
    }


def value_position(entry: dict, mark: dict) -> dict:
    """Mark the same shares, including own-deposit dilution and native fee budgets."""
    shares, debit = entry["shares_raw"], entry["debit_raw"]
    total = int(mark["net_liquidity_sf"]) + debit * SCALE
    supply = mark["collateral_supply"] + shares
    payout = shares * total // (supply * SCALE)
    allowance = mark["withdrawal_allowance_raw"]
    liquid = payout <= mark["free_liquidity_raw"] and (
        allowance is None or payout <= allowance
    )
    first = entry["mark"]
    p0, pt = int(first["usdc_price_sf"]), int(mark["usdc_price_sf"])
    s0, st = int(first["sol_price_sf"]), int(mark["sol_price_sf"])
    cases = {}
    for label, fees in FEE_CASES.items():
        # Native SOL rent+fees are prefunded at entry; only rent returns at exit.
        capital = Decimal(PRINCIPAL * p0) / (1_000_000 * SCALE) + Decimal(
            (RENT + fees) * s0
        ) / (1_000_000_000 * SCALE)
        terminal = Decimal((payout + entry["unspent_raw"]) * pt) / (
            1_000_000 * SCALE
        ) + Decimal(RENT * st) / (1_000_000_000 * SCALE)
        fee_usdc = (fees * st * 1_000_000 + 1_000_000_000 * pt - 1) // (
            1_000_000_000 * pt
        )
        cases[label] = {
            "round_trip_fee_lamports": fees,
            "net_usdc_raw_at_exit_fx": payout - debit - fee_usdc if liquid else None,
            "initial_usd_mark": str(capital),
            "terminal_usd_mark": str(terminal) if liquid else None,
            "usd_mark_pnl": str(terminal - capital) if liquid else None,
        }
    # Deliberately stricter than a simulated deposit: do not credit its liquidity
    # or withdrawal-cap relief. Native cap-rollover timing can change that relief.
    return {
        "shares_raw": shares,
        "redemption_raw": payout,
        "gross_yield_raw": payout - debit,
        "conservative_liquidity_screen_pass": liquid,
        "fee_sensitivities": cases,
        "all_in_profit_proven": False,
    }


def self_check() -> None:
    """Defend rounding, losses, fee denomination and unavailable exits offline."""
    mark = {
        "net_liquidity_sf": str(120_000_000 * SCALE),
        "collateral_supply": 100_000_000,
        "free_liquidity_raw": 120_000_000,
        "withdrawal_allowance_raw": None,
        "usdc_price_sf": str(SCALE),
        "sol_price_sf": str(100 * SCALE),
    }
    entry = enter(mark)
    assert entry["shares_raw"] == 41_666_666 and entry["debit_raw"] == PRINCIPAL
    same = value_position(entry, mark)
    assert same["gross_yield_raw"] == -1
    assert (
        same["fee_sensitivities"]["base_fee_floor"]["net_usdc_raw_at_exit_fx"] == -1001
    )
    worse = value_position(
        entry, {**mark, "net_liquidity_sf": str(119_000_000 * SCALE)}
    )
    assert worse["gross_yield_raw"] < 0
    blocked = value_position(entry, {**mark, "withdrawal_allowance_raw": 0})
    assert not blocked["conservative_liquidity_screen_pass"]
    assert all(
        row["usd_mark_pnl"] is None for row in blocked["fee_sensitivities"].values()
    )
    stronger = value_position(entry, {**mark, "sol_price_sf": str(110 * SCALE)})
    assert (
        stronger["fee_sensitivities"]["base_fee_floor"]["net_usdc_raw_at_exit_fx"]
        == -1101
    )
    print("PASS: share rounding, real losses, native fee FX and unavailable exits")


def main() -> None:  # noqa: C901, PLR0912, PLR0915 -- one ordered durable observation loop
    """Collect seven days of append-only evidence; never reset an entry or a loss."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.out is None:
        parser.error("--out is required (new exclusive JSONL evidence file)")
    source = Path(__file__).read_bytes()
    target_seconds = 0 if args.once else TARGET_SECONDS
    started, deadline = time.time(), time.monotonic() + target_seconds + 2 * INTERVAL
    reached_target = False
    entry, previous = None, None
    marks, outages, gap_seen = 0, 0, False
    with args.out.open("x", encoding="utf-8") as out:

        def emit(row: dict) -> None:
            encoded = json.dumps(row, separators=(",", ":")) + "\n"
            if out.tell() + len(encoded.encode()) > MAX_BYTES:
                raise ValueError("evidence ceiling reached; no data discarded")
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())

        emit(
            {
                "kind": "frozen_definition",
                "created_utc": datetime.fromtimestamp(started, UTC).isoformat(),
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "source": source.decode(),
                "principal_usdc_raw": PRINCIPAL,
                "refundable_collateral_ata_rent_lamports": RENT,
                "fee_budgets_lamports": FEE_CASES,
                "interval_seconds": INTERVAL,
                "target_chain_seconds": target_seconds,
                "maximum_runtime_seconds": target_seconds + 2 * INTERVAL,
                "reserve": RESERVE,
                "accounting": "unsigned native refresh plus fixed-share counterfactual; not booked wallet income",
                "assumptions": [
                    "existing funded USDC token account",
                    "full collateral-account rent reclaimed at exit",
                    "unchanged external borrow/deposit behavior",
                    "only sampled withdrawal availability is known",
                ],
                "unpriced": [
                    "USDC and SOL acquisition/off-ramp",
                    "actual priority fees, failures and retries",
                    "execution and future withdrawal availability",
                    "bad debt, depeg and upgrade risk",
                ],
                "existing_paper_portfolio_changed": False,
            }
        )
        while time.monotonic() < deadline:
            try:
                evidence = capture()
            except urllib.error.HTTPError as exc:
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    emit({"kind": "fatal_http", "at": time.time(), "status": exc.code})
                    raise
                outages += 1
                gap_seen = True
                emit({"kind": "unavailable", "at": time.time(), "reason": str(exc)})
            except (urllib.error.URLError, TimeoutError) as exc:
                outages += 1
                gap_seen = True
                emit(
                    {
                        "kind": "unavailable",
                        "at": time.time(),
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                emit({"kind": "native_simulation", "evidence": evidence})
                try:
                    mark = decode(evidence)
                    if previous is not None and (
                        mark["slot"] <= previous["slot"]
                        or mark["timestamp"] <= previous["timestamp"]
                    ):
                        raise ValueError("native time did not advance")  # noqa: TRY301 -- persist this refusal with other unusable evidence
                    if entry is None:
                        entry = enter(mark)
                        emit({"kind": "frozen_entry", "entry": entry})
                    if (
                        previous is not None
                        and mark["timestamp"] - previous["timestamp"] > 2 * INTERVAL
                    ):
                        gap_seen = True
                    position = value_position(entry, mark)
                except (ValueError, KeyError, TypeError) as exc:
                    emit(
                        {
                            "kind": "fatal_unusable_evidence",
                            "at": time.time(),
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    raise
                marks += 1
                previous = mark
                reached_target = (
                    mark["timestamp"] - entry["mark"]["timestamp"] >= target_seconds
                )
                emit(
                    {
                        "kind": "mark",
                        "mark": mark,
                        "position": position,
                        "elapsed_chain_seconds": mark["timestamp"]
                        - entry["mark"]["timestamp"],
                        "gap_seen": gap_seen,
                    }
                )
                if marks == 1:
                    print("SUPPLY_OBSERVER_READY", flush=True)
                print(
                    json.dumps(
                        {
                            "marks": marks,
                            "outages": outages,
                            "elapsed_hours": (
                                mark["timestamp"] - entry["mark"]["timestamp"]
                            )
                            / 3600,
                            "gross_usdc": str(
                                Decimal(position["gross_yield_raw"]) / 1_000_000
                            ),
                            "liquidity_screen_pass": position[
                                "conservative_liquidity_screen_pass"
                            ],
                            "gap_seen": gap_seen,
                            "all_in_profit_proven": False,
                        }
                    ),
                    flush=True,
                )
            if args.once or reached_target:
                break
            time.sleep(min(INTERVAL, max(0, deadline - time.monotonic())))
        emit(
            {
                "kind": "observation_finished"
                if reached_target
                else "observation_incomplete",
                "marks": marks,
                "outages": outages,
                "gap_seen": gap_seen,
                "target_reached": reached_target,
                "coverage_complete": reached_target and not gap_seen,
                "all_in_profit_proven": False,
            }
        )
        if not reached_target:
            raise RuntimeError(
                "observation target not reached; retained incomplete evidence"
            )


if __name__ == "__main__":
    main()
