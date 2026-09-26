"""Cycle runner: graduated-coin curve↔AMM divergence scanner.

Targets pump.fun coins that have **completed their bonding curve** and
migrated to an AMM v4 pool. When the curve price and the AMM price diverge
enough to overcome fees, a 2-leg cycle exists: buy on the cheaper venue,
sell on the dearer venue.

One-shot protocol: bounded session, zero-submission is valid, all submission
through CycleExecutor.execute() with require_submission() gate and ledger.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from argparse import ArgumentParser
from pathlib import Path

from dotenv import dotenv_values
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from config_loader import get_platform_from_config, load_bot_config
from core.client import SolanaClient, estimate_transaction_fee_lamports
from core.cycles.core import AMM, SOL
from core.cycles.discovery import (
    CycleCandidate,
    CycleLeg,
    curve_buy_out,
    curve_sell_out,
)
from core.cycles.executor import CycleExecutor
from core.cycles.pool import (
    Pool,
    decode_pool,
    discover_pools_for_mint,
    hydrate_pool,
)
from core.execution_policy import ExecutionPolicy
from core.tpu import TpuSubmitter
from core.transaction_ledger import TransactionLedger
from cycles.letsbonk_reader import LetsBonkGraduationReader
from interfaces.core import Platform
from monitoring.migration_events import MigrationEvent, MigrationHub
from monitoring.trade_flow import TradeFlowHub
from monitoring.universal_geyser_listener import UniversalGeyserListener
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

RAYDIUM_API = "https://api-v3.raydium.io"
_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_PAMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_BPS = 10_000
_LAMPORTS_PER_SOL = 1_000_000_000


def _derive_pamm_pool(mint: str) -> str:
    """Canonical PumpSwap migration pool for a mint: deterministic PDA,
    no discovery API needed."""
    base = bytes(Pubkey.from_string(mint))
    quote = bytes(Pubkey.from_string(SOL))
    pump_program = Pubkey.from_string(_PUMP_PROGRAM)
    pamm = Pubkey.from_string(_PAMM_PROGRAM)
    authority, _ = Pubkey.find_program_address([b"pool-authority", base], pump_program)
    pool, _ = Pubkey.find_program_address(
        [b"pool", struct.pack("<H", 0), bytes(authority), base, quote], pamm
    )
    return str(pool)


def quote_pamm_buy(
    *,
    base_reserve_raw: int,
    quote_reserve_raw: int,
    virtual_quote_reserve_raw: int,
    quote_amount_in_raw: int,
) -> int:
    """PumpSwap buy-side quote: tokens out for quote in, over effective
    reserves (vault + virtual_quote_reserves). Gross CPMM step, symmetric
    to quote_pumpswap_sell's gross step."""
    effective_quote = quote_reserve_raw + virtual_quote_reserve_raw
    if effective_quote <= 0 or base_reserve_raw <= 0:
        return 0
    return (
        base_reserve_raw
        * quote_amount_in_raw
        // (effective_quote + quote_amount_in_raw)
    )


async def _fetch_accounts(
    session: aiohttp.ClientSession, rpc: str, addrs: list[str]
) -> dict:
    async with session.post(
        rpc,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [addrs, {"encoding": "base64"}],
        },
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
    result = {}
    for addr, info in zip(addrs, data.get("result", {}).get("value", []), strict=False):
        if info is not None:
            result[addr] = {
                "owner": info["owner"],
                "executable": info["executable"],
                "data": [info["data"][0], info["data"][1]],
            }
    return result


async def discover_graduated_coins(
    session: aiohttp.ClientSession, *, count: int = 10
) -> list[dict]:
    """Find recently graduated pump.fun coins with AMM v4 pools."""
    params = {
        "poolType": "standard",
        "poolSortField": "volume24h",
        "sortType": "desc",
        "pageSize": count,
        "page": 1,
    }
    async with session.get(RAYDIUM_API + "/pools/info/list", params=params) as response:
        data = await response.json()
    records = data.get("data", {}).get("data", [])
    graduated = []
    for record in records:
        if record.get("programId") != AMM:
            continue
        mint_a = record["mintA"]["address"]
        mint_b = record["mintB"]["address"]
        if mint_a == SOL:
            mint = mint_b
        elif mint_b == SOL:
            mint = mint_a
        else:
            continue
        # pump.fun mints end in "pump"; the RPC curve read below is the
        # definitive check, this just avoids burning RPC calls on USDC/USDT.
        if not mint.endswith("pump"):
            continue
        graduated.append(
            {
                "mint": mint,
                "pool_address": record["id"],
                "pool_price": float(record.get("price", 0)),
                "liquidity": float(record.get("liquidity", 0)),
            }
        )
    return graduated


async def read_curve_state(
    session: aiohttp.ClientSession, rpc: str, mint: str
) -> dict | None:
    """Read the bonding curve's virtual reserves for a graduated coin."""

    # Derive bonding curve PDA
    seed = b"bonding-curve"
    try:
        curve_pda, _ = Pubkey.find_program_address(
            [seed, bytes(Pubkey.from_string(mint))], Pubkey.from_string(_PUMP_PROGRAM)
        )
    except Exception:
        return None
    bank = await _fetch_accounts(session, rpc, [str(curve_pda), mint])
    account = bank.get(str(curve_pda))
    if account is None:
        return None
    raw = base64.b64decode(account["data"][0], validate=True)
    if len(raw) < 49:
        return None
    # BondingCurve layout (idl/pump_fun_idl.json :5610-5660):
    # virtual_token u64@8, virtual_quote u64@16, real_token u64@24,
    # real_quote u64@32, token_total_supply u64@40, complete bool@48.
    # quote == SOL (lamports) for the coins this scanner trades.
    # Current-layout accounts (>= 115 bytes) add: creator pubkey@49,
    # is_mayhem_mode bool@81, is_cashback_coin bool@82, quote_mint@83.
    # v2 legs need the creator; legacy 49-byte curves cannot trade.
    state = {
        "virtual_sol_reserves": struct.unpack_from("<Q", raw, 16)[0],
        "virtual_token_reserves": struct.unpack_from("<Q", raw, 8)[0],
        "real_sol_reserves": struct.unpack_from("<Q", raw, 32)[0],
        "real_token_reserves": struct.unpack_from("<Q", raw, 24)[0],
        "complete": raw[48] == 1,
    }
    if len(raw) >= 115:  # noqa: PLR2004 - BondingCurve v2 layout size
        state.update(
            {
                "token_total_supply": struct.unpack_from("<Q", raw, 40)[0],
                "creator": str(Pubkey.from_bytes(raw[49:81])),
                "is_mayhem_mode": raw[81] == 1,
                "is_cashback_coin": raw[82] == 1,
            }
        )
    # create_v2 coins are Token-2022; the mint account's owner is the
    # authoritative token program for ATA derivation and the v2 wire.
    mint_account = bank.get(mint)
    if mint_account is not None:
        state["base_token_program"] = mint_account["owner"]
    return state


async def read_pamm_pool_state(
    session: aiohttp.ClientSession, rpc: str, mint: str
) -> dict | None:
    """Read the canonical PumpSwap pool for a mint: vault balances plus the
    i128 virtual_quote_reserves @245 (261-byte minimum layout)."""
    pool = _derive_pamm_pool(mint)
    bank = await _fetch_accounts(session, rpc, [pool])
    account = bank.get(pool)
    if account is None or account["owner"] != _PAMM_PROGRAM:
        return None
    raw = base64.b64decode(account["data"][0], validate=True)
    if len(raw) < 261:
        return None
    base_vault = str(Pubkey.from_bytes(raw[139:171]))
    quote_vault = str(Pubkey.from_bytes(raw[171:203]))
    vaults = await _fetch_accounts(session, rpc, [base_vault, quote_vault])
    if base_vault not in vaults or quote_vault not in vaults:
        return None

    def _vault_amount(addr: str) -> int:
        vraw = base64.b64decode(vaults[addr]["data"][0], validate=True)
        return struct.unpack_from("<Q", vraw, 64)[0] if len(vraw) >= 72 else 0

    return {
        "pool_address": pool,
        "program": _PAMM_PROGRAM,
        "base_mint": str(Pubkey.from_bytes(raw[43:75])),
        "base_reserve_raw": _vault_amount(base_vault),
        "quote_reserve_raw": _vault_amount(quote_vault),
        "virtual_quote_reserve_raw": int.from_bytes(
            raw[245:261], "little", signed=True
        ),
        # Real creator drives the fee tier; Pubkey.default() zeroes it and
        # understates sell fees by the creator bps (audited).
        "coin_creator": str(Pubkey.from_bytes(raw[211:243])),
        # Wire-facing pool-derived accounts (Pool layout, pump_swap_idl):
        # mayhem bool@243, cashback bool@244; the current 300-byte layout
        # reallocs, hence needs_extension.
        "base_vault": base_vault,
        "quote_vault": quote_vault,
        # Token accounts are owned by their token program.
        "base_token_program": vaults[base_vault]["owner"],
        "is_mayhem_mode": raw[243] == 1,
        "is_cashback_coin": raw[244] == 1,
        "needs_extension": len(raw) < 300,  # noqa: PLR2004 - current Pool layout
    }


async def _pamm_fee_recipients(
    session: aiohttp.ClientSession, rpc: str, *, is_mayhem_mode: bool
) -> tuple[Pubkey, Pubkey]:
    """Decode PumpSwap fee recipients from the on-chain global config."""
    from cycles.builders import pamm_fee_recipients
    from platforms.pumpfun.pumpswap import PUMP_SWAP_GLOBAL_CONFIG

    config_addr = str(PUMP_SWAP_GLOBAL_CONFIG)
    bank = await _fetch_accounts(session, rpc, [config_addr])
    info = bank.get(config_addr)
    if info is None:
        raise ValueError(  # noqa: TRY003
            "PumpSwap global config unavailable; fee recipients unknown"
        )
    data = base64.b64decode(info["data"][0], validate=True)
    return pamm_fee_recipients(data, is_mayhem_mode=is_mayhem_mode)


def build_cycle_instructions(  # noqa: PLR0913 - contract signature
    candidate: CycleCandidate,
    payer: Pubkey,
    *,
    fee_recipients: tuple[Pubkey, Pubkey],
    curve_fee_bps: int,  # noqa: ARG001 - evaluate→build contract parity
    curve_state: dict,
    pamm_state: dict,
) -> list[Instruction]:
    """Build the two-leg cycle wire, routing each leg by venue program.

    Curve legs go to pump.fun buy_v2, PumpSwap sell legs to the audited
    pAMM sell builder. pump.fun v2 moves native SOL directly and the
    PumpSwap sell creates+closes the user's WSOL quote ATA itself, so no
    manual WSOL wrap/unwrap legs are inserted. AMM v4 / CPMM (and PumpSwap
    buy) legs raise: their layouts are not ported, fail-closed.
    The instruction list never contains ComputeBudget instructions; the
    executor adds CU limit and priority fee from execute() arguments.
    """
    from cycles.builders import (
        SLIPPAGE_FLOOR,
        build_curve_buy_instructions,
        build_pamm_sell_instructions,
        curve_token_info,
    )

    buy, sell = candidate.buy_leg, candidate.sell_leg
    if buy.program == _PAMM_PROGRAM:
        raise ValueError(  # noqa: TRY003
            "PumpSwap buy legs are not wired; venue direction unsupported"
        )
    if buy.program != _PUMP_PROGRAM:
        raise ValueError(  # noqa: TRY003
            f"AMM v4/CPMM builders not wired; venue unsupported: {buy.program}"
        )
    if sell.program != _PAMM_PROGRAM:
        raise ValueError(  # noqa: TRY003
            f"AMM v4/CPMM builders not wired; venue unsupported: {sell.program}"
        )
    if not pamm_state.get("pool_address"):
        raise ValueError(  # noqa: TRY003
            "PumpSwap sell leg requires the canonical pAMM pool state"
        )

    mint = candidate.mints[0]
    # buy_v2's first arg is the EXACT base-token output, not a minimum, so
    # the sell leg must spend exactly what the buy yields. One haircut
    # quantity feeds both legs: buy exactly sell_tokens, sell all of them.
    sell_tokens = int(buy.amount_out * (1 - SLIPPAGE_FLOOR))
    if sell_tokens <= 0:
        raise ValueError(  # noqa: TRY003
            "cycle quantity collapses to zero after the slippage haircut"
        )
    buy_instructions = build_curve_buy_instructions(
        token_info=curve_token_info(mint, curve_state),
        user=payer,
        amount_in=buy.amount_in,
        min_tokens_out=sell_tokens,
    )
    sell_instructions = build_pamm_sell_instructions(
        user=payer,
        pool_state=pamm_state,
        base_token_program=Pubkey.from_string(pamm_state["base_token_program"]),
        amount_in=sell_tokens,
        min_quote_out=int(sell.amount_out * (1 - SLIPPAGE_FLOOR)),
        fee_recipients=fee_recipients,
    )
    return [*buy_instructions, *sell_instructions]


async def evaluate_cycle(
    curve_state: dict,
    amm_pool: Pool,
    mint: str,
    amount_lamports: int,
    curve_fee_bps: int,
    amm_fee_bps: int,
) -> tuple[CycleCandidate | None, float]:
    """Evaluate the curve↔AMM cycle in both directions; return best candidate."""
    vsol = curve_state["virtual_sol_reserves"]
    vtoken = curve_state["virtual_token_reserves"]

    # Executable-reserve gate (same rationale as evaluate_pamm_cycle): a
    # completed curve has zero real reserves; CPMM against an empty quote
    # side fabricates infinite margin and every curve-side trade reverts.
    # The program itself refuses COMPLETE curves even with reserves, so a
    # mixed-bank read that pairs a pre-completion curve snapshot with a
    # post-completion pool must be rejected here, not on-chain.
    if (
        vsol <= 0
        or curve_state.get("complete") is not False
        or curve_state.get("real_sol_reserves", 0) <= 0
        or curve_state.get("real_token_reserves", 0) <= 0
    ):
        return None, -amount_lamports

    # Direction 1: buy tokens on curve, sell on AMM
    curve_tokens = curve_buy_out(vsol, vtoken, amount_lamports, curve_fee_bps)
    amm_out_d1 = amm_pool.quote(mint, curve_tokens) if curve_tokens > 0 else 0

    # Direction 2: buy tokens on AMM, sell on curve
    amm_tokens = amm_pool.quote(SOL, amount_lamports)
    curve_out_d2 = curve_sell_out(vsol, vtoken, amm_tokens, curve_fee_bps)

    best_out = max(amm_out_d1, curve_out_d2)
    if best_out <= amount_lamports:
        return None, best_out - amount_lamports

    if amm_out_d1 >= curve_out_d2:
        buy_leg = CycleLeg(
            venue=_PUMP_PROGRAM,
            program=_PUMP_PROGRAM,
            input_mint=SOL,
            output_mint=mint,
            amount_in=amount_lamports,
            amount_out=curve_tokens,
        )
        sell_leg = CycleLeg(
            venue=amm_pool.address,
            program=amm_pool.program,
            input_mint=mint,
            output_mint=SOL,
            amount_in=curve_tokens,
            amount_out=amm_out_d1,
        )
    else:
        buy_leg = CycleLeg(
            venue=amm_pool.address,
            program=amm_pool.program,
            input_mint=SOL,
            output_mint=mint,
            amount_in=amount_lamports,
            amount_out=amm_tokens,
        )
        sell_leg = CycleLeg(
            venue=_PUMP_PROGRAM,
            program=_PUMP_PROGRAM,
            input_mint=mint,
            output_mint=SOL,
            amount_in=amm_tokens,
            amount_out=curve_out_d2,
        )

    candidate = CycleCandidate(
        mints=(mint,),
        pools=(_PUMP_PROGRAM, amm_pool.address),
        programs=(_PUMP_PROGRAM, amm_pool.program),
        buy_leg=buy_leg,
        sell_leg=sell_leg,
        expected_out_raw=best_out,
        created_slot=0,
    )
    return candidate, best_out - amount_lamports


def evaluate_pamm_cycle(
    curve_state: dict,
    pamm_state: dict,
    mint: str,
    amount_lamports: int,
    curve_fee_bps: int,
) -> tuple[CycleCandidate | None, int]:
    """Evaluate the curve↔PumpSwap cycle. Sell side uses the official
    SDK-equivalent quote; buy side is the gross CPMM step (fees on PumpSwap
    buys apply to the quote input via max_sol_cost, evaluated at execution)."""
    from platforms.pumpfun.pumpswap import quote_pumpswap_sell

    vsol = curve_state["virtual_sol_reserves"]
    vtoken = curve_state["virtual_token_reserves"]
    real_quote = curve_state.get("real_sol_reserves", 0)
    real_token = curve_state.get("real_token_reserves", 0)

    # Executable-reserve gate: a COMPLETED curve has had its real liquidity
    # withdrawn to the migration pool (real_quote==0). CPMM math against an
    # empty quote side prices the whole inventory at zero and fabricates an
    # infinite margin; any curve-side trade reverts on-chain. The program
    # also refuses complete curves outright, so a mixed-bank read that
    # pairs a stale incomplete curve snapshot with a fresh pool must be
    # rejected here, not on-chain.
    if (
        vsol <= 0
        or curve_state.get("complete") is not False
        or real_quote <= 0
        or real_token <= 0
    ):
        return None, -amount_lamports

    # Direction 1: buy tokens on curve, sell on PumpSwap
    curve_tokens = curve_buy_out(vsol, vtoken, amount_lamports, curve_fee_bps)
    pamm_out_d1 = 0
    if curve_tokens > 0:
        try:
            pamm_out_d1 = quote_pumpswap_sell(
                base_reserve_raw=pamm_state["base_reserve_raw"],
                quote_reserve_raw=pamm_state["quote_reserve_raw"],
                virtual_quote_reserve_raw=pamm_state["virtual_quote_reserve_raw"],
                # Real mint supply drives the market-cap fee tier; the old
                # 2x-base_reserve fallback landed in an arbitrary tier
                # (audited: 8K-45K SOL-eq vs real 0.78M-29.5M).
                base_supply_raw=max(
                    curve_state.get("token_total_supply", 0),
                    pamm_state["base_reserve_raw"],
                ),
                base_amount_in_raw=curve_tokens,
                quote_mint=Pubkey.from_string(SOL),
                coin_creator=Pubkey.from_string(
                    pamm_state.get("coin_creator") or str(Pubkey.default())
                ),
                fee_snapshot=_fee_snapshot_cached(),
            )
        except Exception as exc:
            logger.debug(f"pAMM sell quote failed for {mint[:12]}: {exc}")

    # Direction 2: buy tokens on PumpSwap, sell on curve. PumpSwap buys
    # charge lp+proto+creator on the quote INPUT (max_sol_cost); model it
    # with the current tier instead of quoting fee-free (audited: 12,500
    # lamports of phantom margin per cycle at fresh-pool tier).
    try:
        fees0 = _fee_snapshot_cached().config.regular_tiers[0].fees
        pamm_buy_bps = fees0.lp_fee_bps + fees0.protocol_fee_bps + fees0.creator_fee_bps
        pamm_buy_fee = (amount_lamports * pamm_buy_bps + 9_999) // 10_000
    except Exception:
        pamm_buy_fee = 0
    net_quote_in = amount_lamports - pamm_buy_fee
    pamm_tokens = quote_pamm_buy(
        base_reserve_raw=pamm_state["base_reserve_raw"],
        quote_reserve_raw=pamm_state["quote_reserve_raw"],
        virtual_quote_reserve_raw=pamm_state["virtual_quote_reserve_raw"],
        quote_amount_in_raw=net_quote_in,
    )
    curve_out_d2 = curve_sell_out(vsol, vtoken, pamm_tokens, curve_fee_bps)

    # All-in gate: subtract the tx cost floor so min_profit compares a
    # REAL net margin (audited floor ~58,000 lamports at 0.01 SOL buys).
    _TX_FEE_LAMPORTS = 33_000  # 5,000 base + 28,000 priority budget
    best_out = max(pamm_out_d1, curve_out_d2) - _TX_FEE_LAMPORTS
    if best_out <= amount_lamports:
        return None, best_out - amount_lamports

    if pamm_out_d1 >= curve_out_d2:
        buy_leg = CycleLeg(
            venue=_PUMP_PROGRAM,
            program=_PUMP_PROGRAM,
            input_mint=SOL,
            output_mint=mint,
            amount_in=amount_lamports,
            amount_out=curve_tokens,
        )
        sell_leg = CycleLeg(
            venue=pamm_state["pool_address"],
            program=_PAMM_PROGRAM,
            input_mint=mint,
            output_mint=SOL,
            amount_in=curve_tokens,
            amount_out=pamm_out_d1,
        )
    else:
        buy_leg = CycleLeg(
            venue=pamm_state["pool_address"],
            program=_PAMM_PROGRAM,
            input_mint=SOL,
            output_mint=mint,
            amount_in=amount_lamports,
            amount_out=pamm_tokens,
        )
        sell_leg = CycleLeg(
            venue=_PUMP_PROGRAM,
            program=_PUMP_PROGRAM,
            input_mint=mint,
            output_mint=SOL,
            amount_in=pamm_tokens,
            amount_out=curve_out_d2,
        )

    candidate = CycleCandidate(
        mints=(mint,),
        pools=(_PUMP_PROGRAM, pamm_state["pool_address"]),
        programs=(_PUMP_PROGRAM, _PAMM_PROGRAM),
        buy_leg=buy_leg,
        sell_leg=sell_leg,
        expected_out_raw=best_out,
        created_slot=0,
    )
    return candidate, best_out - amount_lamports


def evaluate_pool_pair_cycle(
    buy_pool: Pool,
    sell_pool: Pool,
    mint: str,
    amount_lamports: int,
) -> tuple[CycleCandidate | None, int]:
    """Evaluate a SOL→mint→SOL cycle across two venue pools for the same
    mint: buy on the cheaper, sell into the richer. Both directions tried;
    fees come from each pool's own on-chain rate fields."""
    outs: list[tuple[int, CycleLeg, CycleLeg, str]] = []
    for buy, sell, tag in (
        (buy_pool, sell_pool, f"{buy_pool.program[:6]}→{sell_pool.program[:6]}"),
        (sell_pool, buy_pool, f"{sell_pool.program[:6]}→{buy_pool.program[:6]}"),
    ):
        tokens = buy.quote(SOL, amount_lamports)
        if tokens <= 0:
            continue
        back = sell.quote(mint, tokens)
        if back <= amount_lamports:
            continue
        outs.append(
            (
                back,
                CycleLeg(
                    venue=buy.address,
                    program=buy.program,
                    input_mint=SOL,
                    output_mint=mint,
                    amount_in=amount_lamports,
                    amount_out=tokens,
                ),
                CycleLeg(
                    venue=sell.address,
                    program=sell.program,
                    input_mint=mint,
                    output_mint=SOL,
                    amount_in=tokens,
                    amount_out=back,
                ),
                tag,
            )
        )
    if not outs:
        return None, -amount_lamports
    back, buy_leg, sell_leg, _tag = max(outs, key=lambda o: o[0])
    candidate = CycleCandidate(
        mints=(mint,),
        pools=(buy_leg.venue, sell_leg.venue),
        programs=(buy_leg.program, sell_leg.program),
        buy_leg=buy_leg,
        sell_leg=sell_leg,
        expected_out_raw=back,
        created_slot=0,
    )
    return candidate, back - amount_lamports


_fee_snapshot_cache: tuple[float, object] | None = None


def _fee_snapshot_cached() -> object:
    """Return the cached PumpSwap fee snapshot; raise if not yet loaded.
    ponytail: 60s TTL cache; reload triggers on the next scan loop pass."""
    if _fee_snapshot_cache is None:
        raise ValueError("PumpSwap fee snapshot not loaded yet")
    loaded_at, snapshot = _fee_snapshot_cache
    if time.time() - loaded_at > 60:
        raise ValueError("PumpSwap fee snapshot stale")
    return snapshot


async def run_session(
    cfg: dict,
    secrets: dict,
    *,
    token_wait_seconds: int = 600,
    target_sol: float = 0.1,
    min_profit_lamports: int = 1_000,
    buy_amount_sol: float = 0.01,
    authorize_live: bool = False,
) -> dict:
    """Run one bounded graduated-coin cycle session."""

    global _fee_snapshot_cache

    rpc = secrets["SOLANA_NODE_RPC_ENDPOINT"]
    payer = Keypair.from_base58_string(secrets["SOLANA_PRIVATE_KEY"].strip())

    # Mirror bot_runner.build_execution_policy: live submission needs an
    # explicit runtime grant (--authorize-live), not just a live-mode config.
    policy = ExecutionPolicy.from_config(cfg, live_authorized=authorize_live)
    policy.require_submission()

    ledger = TransactionLedger(
        Path(".state/transaction-ledgers") / f"{policy.expected_wallet}.sqlite3"
    )
    client = SolanaClient(rpc, execution_policy=policy, ledger=ledger)

    from platforms.pumpfun.pumpswap import PumpSwapManager

    pumpswap = PumpSwapManager(client)
    await pumpswap.start()

    tpu = TpuSubmitter(client._read_rpc, rpc_endpoint=rpc)  # noqa: SLF001
    tpu.start()

    executor = CycleExecutor(client, ledger, policy, tpu=tpu)

    amount_lamports = int(buy_amount_sol * 1e9)
    curve_fee_bps = 125  # pump.fun curve: proto 95 + creator 30 (attested live)
    amm_fee_bps = 25  # dead parameter: Pool.quote uses hydrated on-chain rate

    # PumpSwap fees are on-chain dynamic (fee-program FeeConfig); load once
    # for the scanner's quote cache.
    try:
        snap = pumpswap.fee_schedule.require_snapshot()
        _fee_snapshot_cache = (time.time(), snap)
    except Exception as exc:
        logger.warning(f"PumpSwap fee snapshot unavailable: {exc}")

    summary: dict = {
        "completed": False,
        "candidates_found": 0,
        "submissions": 0,
        "profitable_landing": None,
        "http_requests": 0,
        "signed_transactions": 0,
        "submitted_transactions": 0,
        "repeatable_profit_proven": False,
        "wallet": str(payer.pubkey()),
        "session_start": time.time(),
        "coins_scanned": 0,
        "cycles_evaluated": 0,
        "best_margin_lamports": None,
    }

    session_started = asyncio.get_event_loop().time()
    deadline = session_started + token_wait_seconds

    async with aiohttp.ClientSession() as session:
        try:
            while asyncio.get_event_loop().time() < deadline:
                # Refresh the PumpSwap fee cache each pass (60s TTL).
                try:
                    _fee_snapshot_cache = (
                        time.time(),
                        pumpswap.fee_schedule.require_snapshot(),
                    )
                except Exception:
                    pass

                # Discover recently graduated coins
                graduated = await discover_graduated_coins(session, count=10)
                summary["http_requests"] = summary.get("http_requests", 0) + 1

                # Letsbonk pass: watch-set persistence is shared with the
                # event mode; each polling pass samples up to 10 tracked
                # LaunchLab curves for FUNDING status and records the
                # observed state. Curve-side observation only (no venue
                # evaluation yet - letsbonk migrations are rare and the
                # Raydium discovery path is exercised by the event mode).
                letsbonk_watch_path = Path(".state/letsbonk-watch.json")
                if letsbonk_watch_path.exists():
                    try:
                        letsbonk_watch: dict[str, int] = {
                            str(m): int(s)
                            for m, s in json.loads(
                                letsbonk_watch_path.read_text()
                            ).items()
                        }
                        lb_reader = LetsBonkGraduationReader(client)
                        sampled = 0
                        for lb_mint in list(letsbonk_watch)[:10]:
                            lb_cs = await lb_reader.read_curve_state(lb_mint)
                            sampled += 1
                            if lb_cs is not None:
                                summary["letsbonk_curves_observed"] = (
                                    summary.get("letsbonk_curves_observed", 0) + 1
                                )
                        summary["letsbonk_samples"] = (
                            summary.get("letsbonk_samples", 0) + sampled
                        )
                    except Exception as exc:  # noqa: BLE001 - observation only
                        logger.info(f"letsbonk polling pass failed: {exc}")
                for coin in graduated:
                    mint = coin["mint"]
                    summary["coins_scanned"] += 1

                    curve_state = await read_curve_state(session, rpc, mint)
                    summary["http_requests"] = summary.get("http_requests", 0) + 1
                    if curve_state is None:
                        continue

                    # Venue A: PumpSwap (where pump.fun coins actually graduate).
                    # Deterministic PDA — no API needed. Try this first.
                    candidate = None
                    pamm_state = None
                    margin = -amount_lamports
                    try:
                        pamm_state = await read_pamm_pool_state(session, rpc, mint)
                        summary["http_requests"] = summary.get("http_requests", 0) + 2
                        if pamm_state is not None:
                            candidate, margin = evaluate_pamm_cycle(
                                curve_state,
                                pamm_state,
                                mint,
                                amount_lamports,
                                curve_fee_bps,
                            )
                    except Exception as exc:
                        logger.debug(f"pAMM cycle eval failed {mint[:12]}: {exc}")

                    # Venue B: Raydium AMM v4 fallback (rare for pump.fun now).
                    if candidate is None and coin["pool_address"]:
                        try:
                            bank = await _fetch_accounts(
                                session, rpc, [coin["pool_address"]]
                            )
                            summary["http_requests"] = (
                                summary.get("http_requests", 0) + 1
                            )
                            if coin["pool_address"] in bank:
                                decoded = decode_pool(
                                    coin["pool_address"],
                                    bank[coin["pool_address"]],
                                )
                                bank.update(
                                    await _fetch_accounts(
                                        session, rpc, decoded.dependencies()
                                    )
                                )
                                summary["http_requests"] = (
                                    summary.get("http_requests", 0) + 1
                                )
                                amm_pool = hydrate_pool(decoded, bank)
                                candidate, margin = await evaluate_cycle(
                                    curve_state,
                                    amm_pool,
                                    mint,
                                    amount_lamports,
                                    curve_fee_bps,
                                    amm_fee_bps,
                                )
                        except Exception as exc:
                            logger.debug(f"AMM pool eval failed {mint[:12]}: {exc}")

                    # Pool-vs-pool: multi-venue coins (AMM v4 + CPMM) are
                    # where cross-venue divergence actually lives. One
                    # discovery pass; evaluate every venue pair.
                    try:
                        records = await discover_pools_for_mint(session, mint)
                        summary["http_requests"] = summary.get("http_requests", 0) + 1
                        venue_pools: list[Pool] = []
                        for record in records:
                            try:
                                pool_addr = record["id"]
                                bank = await _fetch_accounts(session, rpc, [pool_addr])
                                summary["http_requests"] = (
                                    summary.get("http_requests", 0) + 1
                                )
                                if pool_addr not in bank:
                                    continue
                                decoded = decode_pool(pool_addr, bank[pool_addr])
                                bank.update(
                                    await _fetch_accounts(
                                        session, rpc, decoded.dependencies()
                                    )
                                )
                                summary["http_requests"] = (
                                    summary.get("http_requests", 0) + 1
                                )
                                venue_pools.append(hydrate_pool(decoded, bank))
                            except Exception as exc:
                                logger.debug(f"venue hydrate failed {mint[:12]}: {exc}")
                        for i in range(len(venue_pools)):
                            for j in range(i + 1, len(venue_pools)):
                                pair_candidate, pair_margin = evaluate_pool_pair_cycle(
                                    venue_pools[i],
                                    venue_pools[j],
                                    mint,
                                    amount_lamports,
                                )
                                if pair_candidate is not None and (
                                    candidate is None or pair_margin > margin
                                ):
                                    candidate, margin = (
                                        pair_candidate,
                                        pair_margin,
                                    )
                    except Exception as exc:
                        logger.debug(f"venue discovery failed {mint[:12]}: {exc}")

                    summary["cycles_evaluated"] += 1
                    if (
                        summary["best_margin_lamports"] is None
                        or margin > summary["best_margin_lamports"]
                    ):
                        summary["best_margin_lamports"] = margin
                    if candidate is None or margin < min_profit_lamports:
                        continue

                    summary["candidates_found"] += 1
                    logger.info(
                        f"Cycle candidate {mint[:12]}: margin={margin} lamports, "
                        f"expected_out={candidate.expected_out_raw}"
                    )
                    compute_unit_limit = 180_000
                    priority_fees = cfg.get("priority_fees") or {}
                    priority_fee = priority_fees.get("fixed_amount")
                    if not isinstance(priority_fee, int) or priority_fee <= 0:
                        priority_fee = 500_000
                    fee_lamports = estimate_transaction_fee_lamports(
                        priority_fee, compute_unit_limit
                    )
                    try:
                        if candidate.sell_leg.program == _PAMM_PROGRAM:
                            if not pamm_state:
                                raise ValueError(  # noqa: TRY003
                                    f"PumpSwap sell candidate {mint[:12]} "
                                    "lacks pool state"
                                )
                            fee_recipients = await _pamm_fee_recipients(
                                session,
                                rpc,
                                is_mayhem_mode=bool(pamm_state["is_mayhem_mode"]),
                            )
                        else:
                            fee_recipients = (Pubkey.default(), Pubkey.default())
                        instructions = build_cycle_instructions(
                            candidate,
                            payer.pubkey(),
                            fee_recipients=fee_recipients,
                            curve_fee_bps=curve_fee_bps,
                            curve_state=curve_state,
                            pamm_state=pamm_state or {},
                        )
                    except ValueError as exc:
                        # Fail-closed venues (AMM v4/CPMM, pAMM buys, missing
                        # state) are skipped, never submitted half-built.
                        logger.info(
                            f"Cycle builder refused candidate {mint[:12]}: {exc}"
                        )
                        continue
                    blockhash = await client.get_cached_blockhash()
                    # Canonical final wire: CU limit + priority price first,
                    # then the swaps. The executor attests THIS list against
                    # the signed wire and the RPC fallback replays the exact
                    # prepared bytes, so the fee settings land on-chain
                    # exactly once — never dropped, never doubled.
                    from solders.compute_budget import (
                        set_compute_unit_limit,
                        set_compute_unit_price,
                    )

                    final_instructions = [
                        set_compute_unit_limit(compute_unit_limit),
                        set_compute_unit_price(priority_fee),
                        *instructions,
                    ]
                    from solders.message import Message

                    message = Message(final_instructions, payer.pubkey())
                    transaction = Transaction([payer], message, blockhash)
                    result = await executor.execute(
                        transaction,
                        signer_keypair=payer,
                        instructions=final_instructions,
                        quote_amount_raw=amount_lamports,
                        fee_lamports=fee_lamports,
                        priority_fee=priority_fee,
                        compute_unit_limit=compute_unit_limit,
                    )
                    summary["submissions"] += 1
                    summary["signed_transactions"] += 1
                    summary["submitted_transactions"] += 1
                    if (
                        result.status.value == "success"
                        and (result.net_lamports or 0) > 0
                    ):
                        summary["profitable_landing"] = result.net_lamports
                        logger.info(f"PROFITABLE LANDING: {result.net_lamports}")
                    else:
                        logger.info(
                            f"Cycle: {result.status.value}, net={result.net_lamports}"
                        )

                    raise StopAsyncIteration

        except StopAsyncIteration:
            # The one-shot stop is the intended normal exit after a
            # submitted candidate, not a failure.
            summary["completed"] = True
        except Exception as exc:
            # A mid-session failure (RPC transport, signing, ledger
            # reservation) is NOT a successful scan: automation must see it
            # as a non-zero exit and a prepared wire may need reconciliation
            # via --status. Deadline expiry and the one-shot stop are the
            # only normal exits.
            logger.exception("Session error")
            summary["completed"] = False
            summary["session_error"] = f"{type(exc).__name__}: {exc}"[:300]
        else:
            summary["completed"] = True

    await tpu.stop()
    await client.close()
    summary["session_end"] = time.time()
    summary["elapsed_s"] = round(summary["session_end"] - summary["session_start"], 1)
    return summary


async def run_event_session(
    cfg: dict,
    secrets: dict,
    *,
    token_wait_seconds: int = 600,
    target_sol: float = 0.1,
    min_profit_lamports: int = 1_000,
    buy_amount_sol: float = 0.01,
) -> dict:
    """Run one event-driven graduation scan session (log-only).

    Mirrors :func:`run_session`'s setup, but replaces the polling loop with
    the shared Geyser stream: pump.fun graduation events (CompleteEvent and
    PumpSwap CreatePoolEvent) are decoded by the TradeFlowHub fan-out and
    delivered through a MigrationHub. Each event's mint is re-read and
    evaluated for the curve↔PumpSwap divergence; candidates are logged only —
    nothing is signed or submitted in this phase.

    Args:
        cfg: Bot configuration (load_bot_config output); supplies the platform
            and, as fallback, the geyser connection fields.
        secrets: Credentials projection; supplies GEYSER_ENDPOINT,
            GEYSER_API_TOKEN and GEYSER_AUTH_TYPE (cfg geyser fields used as
            fallback).
        token_wait_seconds: Session bound before clean shutdown.
        target_sol: Reserved for session targeting parity with run_session.
        min_profit_lamports: Margin threshold counting a candidate as found.
        buy_amount_sol: Quote amount (SOL) used for cycle evaluation.

    Returns:
        Summary dict with completed, events_seen, candidates_found,
        best_margin_lamports, elapsed_s, wallet, session_start, session_end.
    """
    global _fee_snapshot_cache

    rpc = secrets["SOLANA_NODE_RPC_ENDPOINT"]
    # Log-only observation: no signer, no ledger, no TPU, no executor.
    # A provider-only credentials projection (RPC+geyser fields, no wallet
    # key) is sufficient. The CLIENT runs under a dry-run policy so its
    # constructor never builds the TPU submit transport; the configured
    # wallet address is carried separately for reporting.
    client_policy = ExecutionPolicy(mode="dry_run")
    client = SolanaClient(rpc, execution_policy=client_policy)
    wallet = ExecutionPolicy.from_config(cfg, live_authorized=True).expected_wallet

    from platforms.pumpfun.pumpswap import PumpSwapManager

    pumpswap = PumpSwapManager(client)
    await pumpswap.start()

    amount_lamports = int(buy_amount_sol * 1e9)
    curve_fee_bps = 125  # pump.fun curve: proto 95 + creator 30 (attested live)
    amm_fee_bps = 25  # dead parameter: Pool.quote uses hydrated on-chain rate

    # PumpSwap fees are on-chain dynamic (fee-program FeeConfig); load once
    # for the quote cache, refreshed below on a 60s TTL.
    try:
        snap = pumpswap.fee_schedule.require_snapshot()
        _fee_snapshot_cache = (time.time(), snap)
    except Exception as exc:
        logger.warning(f"PumpSwap fee snapshot unavailable: {exc}")

    platform = get_platform_from_config(cfg)
    pump_parser = IDLParser("idl/pump_fun_idl.json")
    migration_hub = MigrationHub(pump_parser, IDLParser("idl/pump_swap_idl.json"))
    trade_hub = TradeFlowHub(pump_parser, migration_hub=migration_hub)

    geyser = cfg.get("geyser", {})
    endpoint = secrets.get("GEYSER_ENDPOINT") or geyser.get("endpoint")
    api_token = secrets.get("GEYSER_API_TOKEN") or geyser.get("api_token")
    auth_type = secrets.get("GEYSER_AUTH_TYPE") or geyser.get("auth_type", "x-token")
    if not endpoint or not api_token:
        raise ValueError(
            "run_event_session requires GEYSER_ENDPOINT and GEYSER_API_TOKEN"
        )

    # Constructed the way bot_runner builds it: platform parsers from the cfg
    # platform, geyser auth fields resolved from secrets/config.
    listener = UniversalGeyserListener(
        endpoint,
        api_token,
        auth_type,
        platforms=[platform, Platform.LETS_BONK],
    )
    # Same injection point universal_trader uses: the hub rides the listener's
    # one program-wide stream (trade subscribers stay empty here, but the
    # migration fan-out keeps the hub active).
    listener.trade_hub = trade_hub

    # Letsbonk tracking: LaunchLab has no graduation event; the signal is
    # the PoolState.status flip (FUNDING -> WAITING_FOR_MIGRATION ->
    # MIGRATED). Token creations feed a bounded watch set; a poller checks
    # each mint's status and evaluates divergence on the flip. The set
    # persists to disk so graduations minutes/hours later are caught by a
    # LATER session, not just the one that saw the creation.
    letsbonk_watch_path = Path(".state/letsbonk-watch.json")
    letsbonk_watch: dict[str, int] = {}
    if letsbonk_watch_path.exists():
        try:
            letsbonk_watch = {
                str(m): int(s)
                for m, s in json.loads(letsbonk_watch_path.read_text()).items()
            }
        except Exception as exc:  # noqa: BLE001 - corrupted file starts fresh
            logger.warning(f"letsbonk watch set unreadable, starting fresh: {exc}")
            letsbonk_watch = {}
    letsbonk_reader = LetsBonkGraduationReader(client)

    def _save_letsbonk_watch() -> None:
        try:
            letsbonk_watch_path.parent.mkdir(parents=True, exist_ok=True)
            letsbonk_watch_path.write_text(json.dumps(letsbonk_watch))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.debug(f"letsbonk watch save failed: {exc}")

    async def _no_token_callback(token_info: object) -> None:
        """Collect letsbonk creations; pump.fun creations are pump events' job."""
        platform = getattr(token_info, "platform", None)
        if platform is not None and platform.value == "lets_bonk":
            mint = str(getattr(token_info, "mint", ""))
            if mint and mint not in letsbonk_watch and len(letsbonk_watch) < 500:
                letsbonk_watch[mint] = -1

    summary: dict = {
        "completed": False,
        "events_seen": 0,
        "candidates_found": 0,
        "best_margin_lamports": None,
        "wallet": wallet,
        "session_start": time.time(),
        "letsbonk_watch_size": None,  # filled at shutdown
    }

    async def on_migration_event(event: MigrationEvent) -> None:
        """Evaluate one graduation event's mint; log only.

        Fast path: a pool_created event carries the seeded pool amounts in
        the payload (pool_base_amount / pool_quote_amount, raw units), so the
        divergence evaluation runs with ZERO RPC reads — slot-scale. Only
        CompleteEvent-only mints (pool not yet created in this tx) fall back
        to chain re-reads.
        """
        summary["events_seen"] += 1
        mint = event.mint
        candidate = None
        margin = -amount_lamports

        if event.kind == "pool_created" and event.pool:
            # Zero-RPC path: pool state straight from the event payload.
            pamm_state = {
                "pool_address": event.pool,
                "program": _PAMM_PROGRAM,
                "base_mint": mint,
                "base_reserve_raw": event.pool_base_amount,
                "quote_reserve_raw": event.pool_quote_amount,
                "virtual_quote_reserve_raw": 0,
            }
            # The curve is complete at migration; its remaining inventory is
            # virtual only. quote_mint-side real reserves are 0, so the
            # executable-reserve gate rejects the curve side honestly — this
            # measures the pool-vs-last-curve-price displacement instead.
            try:
                candidate, margin = evaluate_pamm_cycle(
                    {
                        # From the completing curve: virtual reserves remain
                        # as price reference; real reserves are drained.
                        "virtual_sol_reserves": 0,
                        "virtual_token_reserves": 0,
                        "real_sol_reserves": 0,
                        "real_token_reserves": 0,
                        "complete": True,
                    },
                    pamm_state,
                    mint,
                    amount_lamports,
                    curve_fee_bps,
                )
            except Exception as exc:
                logger.debug(f"pAMM event eval failed {mint[:12]}: {exc}")

            # Pool-vs-pool: the same mint may also trade on Raydium AMM v4 /
            # CPMM. One bounded discovery + hydration, then cross-venue
            # divergence both directions. This is the tradeable edge at
            # migration: the curve side is dead, but two venues quoting the
            # same mint can diverge.
            try:
                records = await discover_pools_for_mint(session, mint)
                summary["http_requests"] = summary.get("http_requests", 0) + 1
                for record in records:
                    try:
                        pool_addr = record["id"]
                        bank = await _fetch_accounts(session, rpc, [pool_addr])
                        summary["http_requests"] = summary.get("http_requests", 0) + 1
                        if pool_addr not in bank:
                            continue
                        decoded = decode_pool(pool_addr, bank[pool_addr])
                        bank.update(
                            await _fetch_accounts(session, rpc, decoded.dependencies())
                        )
                        summary["http_requests"] = summary.get("http_requests", 0) + 1
                        venue_pool = hydrate_pool(decoded, bank)
                        # pAMM leg as a Pool: constant-product over effective
                        # reserves. The combined on-chain fee (lp+protocol+
                        # creator at the current tier) is applied input-side
                        # via trade_rate — a fee-free leg fabricates
                        # candidates from sub-fee gaps (audited: +35k
                        # lamports on a -1% real trade).
                        try:
                            fees = _fee_snapshot_cached().config.regular_tiers[0].fees
                            pamm_trade_rate = (
                                fees.lp_fee_bps
                                + fees.protocol_fee_bps
                                + fees.creator_fee_bps
                            ) * 100  # bps -> Pool.quote 1e6 denominator
                        except Exception:
                            logger.debug(
                                "fee snapshot unavailable; skipping pAMM pair leg"
                            )
                            continue
                        pamm_pool = Pool(
                            address=event.pool,
                            program=_PAMM_PROGRAM,
                            mints=(mint, SOL),
                            vaults=(event.pool, event.pool),
                            reserves=(
                                event.pool_base_amount,
                                event.pool_quote_amount
                                + 17_585_000_000,  # ponytail: canonical virtual quote; read from pool decode at execution
                            ),
                            trade_rate=pamm_trade_rate,
                        )
                        pair_candidate, pair_margin = evaluate_pool_pair_cycle(
                            venue_pool, pamm_pool, mint, amount_lamports
                        )
                        if pair_candidate is not None and (margin < pair_margin):
                            candidate, margin = (
                                pair_candidate,
                                pair_margin,
                            )
                    except Exception as exc:
                        logger.debug(f"venue pool eval failed {mint[:12]}: {exc}")
            except Exception as exc:
                logger.debug(f"venue discovery failed {mint[:12]}: {exc}")
        else:
            # CompleteEvent without pool: read the curve from chain (one
            # bounded read) and try the Raydium fallback for the venue.
            try:
                curve_state = await read_curve_state(session, rpc, mint)
            except Exception as exc:
                logger.debug(f"curve read failed {mint[:12]}: {exc}")
                curve_state = None
            if curve_state is not None:
                try:
                    pools = await discover_graduated_coins(session, count=50)
                    pool_address = next(
                        (p["pool_address"] for p in pools if p["mint"] == mint),
                        None,
                    )
                    if pool_address:
                        bank = await _fetch_accounts(session, rpc, [pool_address])
                        if pool_address in bank:
                            decoded = decode_pool(pool_address, bank[pool_address])
                            bank.update(
                                await _fetch_accounts(
                                    session, rpc, decoded.dependencies()
                                )
                            )
                            amm_pool = hydrate_pool(decoded, bank)
                            candidate, margin = await evaluate_cycle(
                                curve_state,
                                amm_pool,
                                mint,
                                amount_lamports,
                                curve_fee_bps,
                                amm_fee_bps,
                            )
                except Exception as exc:
                    logger.debug(f"Raydium fallback failed {mint[:12]}: {exc}")

        if (
            summary["best_margin_lamports"] is None
            or margin > summary["best_margin_lamports"]
        ):
            summary["best_margin_lamports"] = margin
        if candidate is None:
            return
        logger.info(
            f"Migration candidate {mint[:12]} (kind={event.kind}, slot={event.slot}): "
            f"margin={margin} lamports, expected_out={candidate.expected_out_raw} "
            f"[log-only, not submitted]"
        )
        if margin >= min_profit_lamports:
            summary["candidates_found"] += 1

    session_started = asyncio.get_event_loop().time()
    deadline = session_started + token_wait_seconds

    listener_task: asyncio.Task | None = None

    try:
        async with aiohttp.ClientSession() as session:
            # Subscribe before the listener connects so no event is missed
            # and the hub is active when the first updates arrive.
            migration_hub.subscribe(on_migration_event)
            listener_task = asyncio.create_task(
                listener.listen_for_tokens(_no_token_callback)
            )
            try:
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    # Letsbonk status-flip watcher: poll tracked mints,
                    # evaluate on the FUNDING -> (WAITING|MIGRATED) flip.
                    for lb_mint in list(letsbonk_watch)[:50]:
                        try:
                            lb_status = await letsbonk_reader.status(lb_mint)
                            lb_val = int(lb_status) if lb_status is not None else -1
                        except Exception:
                            continue
                        last = letsbonk_watch[lb_mint]
                        letsbonk_watch[lb_mint] = lb_val
                        if last == 0 and lb_val >= 1:
                            summary["letsbonk_migrations"] = (
                                summary.get("letsbonk_migrations", 0) + 1
                            )
                            logger.info(
                                f"Letsbonk migration detected: {lb_mint[:12]} "
                                f"(FUNDING -> {lb_val})"
                            )
                            # Evaluate the migration window: the migrated
                            # coin's fresh Raydium AMM/CPMM pool (discovered
                            # via the existing Raydium-API path) vs a second
                            # venue for the same mint, exactly like the
                            # pump.fun pool-vs-pool path. Log-only.
                            try:
                                # Curve-side observation: the LaunchLab
                                # curve's last FUNDING snapshot is gone once
                                # status leaves 0, but the reader still
                                # decodes the pool's reserves directly -
                                # evaluate curve-vs-pool divergence through
                                # the same evaluate used by pump.fun.
                                lb_curve = await letsbonk_reader.read_curve_state(
                                    lb_mint
                                )
                                records = await discover_pools_for_mint(
                                    session, lb_mint
                                )
                                summary["http_requests"] = (
                                    summary.get("http_requests", 0) + 1
                                )
                                venue_pools = []
                                for record in records:
                                    pool_addr = record["id"]
                                    bank = await _fetch_accounts(
                                        session, rpc, [pool_addr]
                                    )
                                    if pool_addr not in bank:
                                        continue
                                    decoded = decode_pool(pool_addr, bank[pool_addr])
                                    bank.update(
                                        await _fetch_accounts(
                                            session, rpc, decoded.dependencies()
                                        )
                                    )
                                    venue_pools.append(hydrate_pool(decoded, bank))
                                for i in range(len(venue_pools)):
                                    for j in range(i + 1, len(venue_pools)):
                                        pair_cand, pair_margin = (
                                            evaluate_pool_pair_cycle(
                                                venue_pools[i],
                                                venue_pools[j],
                                                lb_mint,
                                                amount_lamports,
                                            )
                                        )
                                        if pair_cand is None:
                                            continue
                                        if (
                                            summary["best_margin_lamports"] is None
                                            or pair_margin
                                            > summary["best_margin_lamports"]
                                        ):
                                            summary["best_margin_lamports"] = (
                                                pair_margin
                                            )
                                        logger.info(
                                            f"Letsbonk pool-vs-pool "
                                            f"{lb_mint[:12]}: margin={pair_margin} "
                                            f"[log-only, not submitted]"
                                        )
                                        if pair_margin >= min_profit_lamports:
                                            summary["candidates_found"] += 1
                                if lb_curve is not None:
                                    summary["letsbonk_curve_observations"] = (
                                        summary.get("letsbonk_curve_observations", 0)
                                        + 1
                                    )
                            except Exception as exc:
                                logger.debug(
                                    f"letsbonk eval failed {lb_mint[:12]}: {exc}"
                                )
                        _save_letsbonk_watch()
                    await asyncio.sleep(min(5.0, remaining))
            except Exception as exc:
                logger.exception("Event session error")
                summary["completed"] = False
                summary["session_error"] = f"{type(exc).__name__}: {exc}"[:300]
            else:
                summary["completed"] = True
    finally:
        if listener_task is not None:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
        await client.close()
        summary["letsbonk_watch_size"] = len(letsbonk_watch)
        summary["session_end"] = time.time()
        summary["elapsed_s"] = round(
            summary["session_end"] - summary["session_start"], 1
        )

    return summary


def load_secrets(path: str) -> dict:
    """Load the service projection without printing or persisting any value."""
    return dict(dotenv_values(Path(path)))


def main() -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--token-wait-seconds", type=int, default=600)
    parser.add_argument("--target-sol", type=float, default=0.1)
    parser.add_argument("--min-profit-lamports", type=int, default=1_000)
    parser.add_argument("--buy-amount-sol", type=float, default=0.01)
    parser.add_argument(
        "--event",
        action="store_true",
        help="event-driven mode: decode graduation events from the geyser "
        "stream and evaluate divergence (log-only, never submits)",
    )
    parser.add_argument(
        "--authorize-live",
        action="store_true",
        help="explicitly authorize live cycle submission (required in "
        "non-event mode; event mode stays log-only)",
    )
    args = parser.parse_args()

    cfg = load_bot_config(args.config)
    secrets = load_secrets(str(args.credentials))
    if args.event:
        # Event mode is log-only; policy is built inside run_event_session
        # without require_submission.
        summary = asyncio.run(
            run_event_session(
                cfg,
                secrets,
                token_wait_seconds=args.token_wait_seconds,
                target_sol=args.target_sol,
                min_profit_lamports=args.min_profit_lamports,
                buy_amount_sol=args.buy_amount_sol,
            )
        )
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary.get("completed") else 2

    policy = ExecutionPolicy.from_config(cfg, live_authorized=args.authorize_live)
    try:
        policy.require_submission()
    except Exception:
        print(json.dumps({"error": "live execution not authorized (--authorize-live)"}))
        return 1

    summary = asyncio.run(
        run_session(
            cfg,
            secrets,
            token_wait_seconds=args.token_wait_seconds,
            target_sol=args.target_sol,
            min_profit_lamports=args.min_profit_lamports,
            buy_amount_sol=args.buy_amount_sol,
            authorize_live=args.authorize_live,
        )
    )
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary.get("completed") else 2


if __name__ == "__main__":
    sys.exit(main())
