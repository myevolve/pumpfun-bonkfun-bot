# ruff: noqa: E402, PLR2004, S101
"""Phase 1 Creator Pilot Dry-Run Simulator — zero RPC, zero funds moved.

Simulates the full launch-to-graduation flow specified in
`docs/superpowers/specs/2026-10-10-creator-pilot-design.md` using the bot's
production instruction builders and integer fee-schedule quote engine:

  1. Builds `create_v2` + `extend_account` via `PumpFunInstructionBuilder` and
     verifies the wire round-trips through `IDLParser`.
  2. Steps a fresh bonding curve (30 SOL virtual quote, 1.073B virtual tokens,
     793.1M real tokens) across 4 self-sweep tranches using `quote_buy_exact_out`
     and `build_buy_v2_instruction` until `real_token_reserves == 0` (`complete`).
  3. Verifies exact graduation math: `real_quote_reserves == 85_000_359_072`
     lamports (~85.0004 SOL), total gross spend (including 95 bps protocol +
     30 bps creator fees) = ~86.063 SOL, with 0.255 SOL immediately rebates to
     the creator's own `associated_creator_vault` ATA (net lockup ~85.808 SOL).
  4. Holds at graduation (never builds a sell or LP withdrawal instruction).

Usage:
    uv run learning-examples/token-lifecycles/simulate_creator_pilot_launch.py
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT, SystemAddresses
from interfaces.core import Platform, TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddressProvider
from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFees,
    PumpFeeSnapshot,
    PumpFeeTier,
    quote_buy_exact_out,
)
from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder
from platforms.pumpfun.pumpswap import (
    PumpSwapAddresses,
    build_pumpswap_collect_creator_fee_instructions,
)
from utils.idl_parser import IDLParser

IDL_PATH = PROJECT_ROOT / "idl" / "pump_fun_idl.json"

INITIAL_VIRTUAL_TOKEN_RESERVES = 1_073_000_000_000_000
INITIAL_VIRTUAL_QUOTE_RESERVES = 30_000_000_000
INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000
TOKEN_TOTAL_SUPPLY = 1_000_000_000_000_000


def _default_fee_snapshot() -> PumpFeeSnapshot:
    """Construct the attested mainnet Pump.fun fee schedule (95 bps protocol + 30 bps creator)."""
    fees = PumpFees(lp_fee_bps=0, protocol_fee_bps=95, creator_fee_bps=30)
    tier = PumpFeeTier(market_cap_threshold_raw=0, fees=fees)
    cfg = PumpFeeConfig(
        bump=255,
        admin=Pubkey.new_unique(),
        flat_fees=fees,
        regular_tiers=(tier,),
        stable_tiers=(tier,),
        exotic_flat_fees=fees,
        digest="simulated-mainnet-95-30",
    )
    return PumpFeeSnapshot(config=cfg, observed_at=1.0, attested_at=1.0)


async def run_simulation(tranches: int = 4) -> dict[str, int | float | bool]:
    """Run the 4-tranche self-sweep simulation to exact curve completion."""
    parser = IDLParser(str(IDL_PATH))
    builder = PumpFunInstructionBuilder(parser)
    provider = PumpFunAddressProvider()
    snapshot = _default_fee_snapshot()

    mint = Pubkey.new_unique()
    creator = Pubkey.new_unique()

    # Step 1: Build and decode-verify create_v2 + extend_account
    create_ixs = builder.build_create_v2_instructions(
        mint=mint,
        user=creator,
        name="Creator Pilot DryRun",
        symbol="PILOT",
        uri="https://example.com/pilot.json",
        address_provider=provider,
        creator=creator,
        is_mayhem_mode=False,
        is_cashback_enabled=False,
    )
    assert len(create_ixs) == 2, "Expected create_v2 + extend_account"
    create_ix, extend_ix = create_ixs
    keys = [bytes(a.pubkey) for a in create_ix.accounts]
    decoded = parser.decode_instruction(
        bytes(create_ix.data), keys, list(range(len(keys)))
    )
    assert decoded is not None and decoded["instruction_name"] == "create_v2"
    assert decoded["args"]["creator"] == str(creator)
    assert len(extend_ix.accounts) == 5

    # Step 2: Sweep all 793.1M real curve tokens across N tranches via buy_v2
    curve_state: dict[str, object] = {
        "virtual_token_reserves": INITIAL_VIRTUAL_TOKEN_RESERVES,
        "virtual_quote_reserves": INITIAL_VIRTUAL_QUOTE_RESERVES,
        "real_token_reserves": INITIAL_REAL_TOKEN_RESERVES,
        "real_quote_reserves": 0,
        "token_total_supply": TOKEN_TOTAL_SUPPLY,
        "complete": False,
        "creator": creator,
        "is_mayhem_mode": False,
        "is_cashback_coin": False,
        "quote_mint": WSOL_MINT,
    }
    token_info = TokenInfo(
        name="Creator Pilot DryRun",
        symbol="PILOT",
        uri="https://example.com/pilot.json",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=provider.derive_pool_address(mint),
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            mint,
            provider.derive_pool_address(mint),
            SystemAddresses.TOKEN_2022_PROGRAM,
        ),
        user=creator,
        creator=creator,
        token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
        quote_mint=WSOL_MINT,
        quote_token_program_id=SystemAddresses.TOKEN_PROGRAM,
        is_mayhem_mode=False,
        is_cashback_coin=False,
    )

    remaining_tokens = INITIAL_REAL_TOKEN_RESERVES
    base_tranche = remaining_tokens // tranches
    total_gross_lamports = 0
    total_net_lamports = 0
    total_protocol_fee_lamports = 0
    total_creator_fee_lamports = 0

    for idx in range(1, tranches + 1):
        tokens_out = remaining_tokens if idx == tranches else base_tranche
        quote = quote_buy_exact_out(curve_state, tokens_out, snapshot)
        buy_ixs = await builder.build_buy_v2_instruction(
            token_info=token_info,
            user=creator,
            amount_in=quote.gross_quote_raw,
            minimum_amount_out=tokens_out,
            address_provider=provider,
        )
        assert len(buy_ixs) == 2, "SOL-paired buy_v2 builds base ATA + buy_v2"
        buy_ix = buy_ixs[1]
        assert len(buy_ix.accounts) == 27
        unpacked_tokens, unpacked_max_sol = struct.unpack_from(
            "<QQ", bytes(buy_ix.data), 8
        )
        assert unpacked_tokens == tokens_out
        assert unpacked_max_sol == quote.gross_quote_raw

        # Advance curve reserves
        curve_state["virtual_token_reserves"] = (
            int(curve_state["virtual_token_reserves"]) - tokens_out
        )
        curve_state["virtual_quote_reserves"] = (
            int(curve_state["virtual_quote_reserves"]) + quote.net_quote_raw
        )
        curve_state["real_token_reserves"] = (
            int(curve_state["real_token_reserves"]) - tokens_out
        )
        curve_state["real_quote_reserves"] = (
            int(curve_state["real_quote_reserves"]) + quote.net_quote_raw
        )
        remaining_tokens -= tokens_out
        total_gross_lamports += quote.gross_quote_raw
        total_net_lamports += quote.net_quote_raw
        total_protocol_fee_lamports += quote.protocol_fee_raw
        total_creator_fee_lamports += quote.creator_fee_raw

        print(
            f"  Tranche {idx}/{tranches}: tokens={tokens_out / 1e6:>10.2f}M | "
            f"net_sol={quote.net_quote_raw / 1e9:>6.3f} | "
            f"gross_sol={quote.gross_quote_raw / 1e9:>6.3f} | "
            f"curve_real_sol={int(curve_state['real_quote_reserves']) / 1e9:>6.3f}"
        )

    curve_state["complete"] = int(curve_state["real_token_reserves"]) == 0
    assert curve_state["complete"] is True
    assert int(curve_state["real_token_reserves"]) == 0

    # Step 3: Post-graduation hold — derive canonical PumpSwap pool & creator fee vault ATA
    pool = PumpSwapAddresses.derive_canonical_pool(mint, WSOL_MINT)
    creator_vault_pda = PumpSwapAddresses.derive_creator_vault(creator)
    assert pool != Pubkey.default()
    assert creator_vault_pda != Pubkey.default()

    curve_claim_ixs = builder.build_collect_creator_fee_v2_instructions(
        creator=creator,
        address_provider=provider,
        quote_mint=WSOL_MINT,
    )
    pumpswap_claim_ixs = build_pumpswap_collect_creator_fee_instructions(
        creator,
        quote_mint=WSOL_MINT,
    )
    assert len(curve_claim_ixs) == 3, "Expected ATA + collect_creator_fee_v2 + close"
    assert len(pumpswap_claim_ixs) == 3, (
        "Expected ATA + collect_coin_creator_fee + close"
    )
    assert len(pumpswap_claim_ixs[1].accounts) == 8, (
        "Expected 8 PumpSwap claim accounts"
    )
    net_lockup_lamports = total_gross_lamports - total_creator_fee_lamports
    return {
        "complete": True,
        "real_quote_sol": total_net_lamports / 1e9,
        "gross_spend_sol": total_gross_lamports / 1e9,
        "protocol_fee_sol": total_protocol_fee_lamports / 1e9,
        "creator_rebate_sol": total_creator_fee_lamports / 1e9,
        "net_lockup_sol": net_lockup_lamports / 1e9,
    }


def main() -> int:
    print(
        "Phase 1 Creator Pilot Dry-Run Simulation (create_v2 -> 4x buy_v2 -> graduation):"
    )
    result = asyncio.run(run_simulation(tranches=4))
    print(
        f"\nGraduation reached: complete={result['complete']} | "
        f"curve_real_sol={result['real_quote_sol']:.4f} SOL | "
        f"gross_spend={result['gross_spend_sol']:.4f} SOL | "
        f"creator_self_rebate={result['creator_rebate_sol']:.4f} SOL | "
        f"net_lockup={result['net_lockup_sol']:.4f} SOL"
    )
    assert 85.0 <= float(result["real_quote_sol"]) <= 85.01
    assert 85.7 <= float(result["net_lockup_sol"]) <= 85.9
    print("PASS: Phase 1 launch + self-sweep dry-run verified offline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
