"""Simulate pump.fun buy_v2 / sell_v2 against mainnet without spending funds.

Builds both instructions through the bot's real code path (address provider,
instruction builder, curve manager) and runs them through
`simulateTransaction`, which executes the program against live state but never
submits. Reports the program error (if any) and the compute units consumed —
use the latter to tune `get_buy_compute_unit_limit` / `get_sell_compute_unit_limit`.

The transaction uses a known funded protocol address as its unsigned payer,
carries only default signatures, and disables signature verification.

Usage:
    # simulate against a specific coin
    uv run learning-examples/simulate_v2_trades.py <MINT>

    # discover a fresh coin via geyser, then simulate against it
    uv run learning-examples/simulate_v2_trades.py
"""

import asyncio
import os
import sys
from base64 import b64encode
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from solders.compute_budget import set_compute_unit_limit  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from core.pubkeys import (  # noqa: E402
    TOKEN_DECIMALS,
    SystemAddresses,
    normalize_quote_mint,
    quote_token_program,
)
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402

# Buy this many whole tokens in the simulation. Small enough that the quote cost
# stays well under the unsigned simulation payer's balance on a fresh curve.
SIMULATED_TOKEN_AMOUNT = 20
# Generous ceiling so simulation reports true consumption rather than hitting
# the limit. The real bot uses the builder's tuned values.
SIMULATION_CU_LIMIT = 400_000
SIMULATION_SLIPPAGE_BPS = 3_000
_BASIS_POINTS = 10_000


async def discover_mint(timeout_seconds: float = 45.0) -> Pubkey | None:
    """Listen for a freshly created pump.fun coin via geyser.

    Args:
        timeout_seconds: How long to wait for a creation event

    Returns:
        Mint of the first coin seen, or None on timeout
    """

    listener = ListenerFactory.create_listener(
        listener_type="geyser",
        geyser_endpoint=os.environ["GEYSER_ENDPOINT"],
        geyser_api_token=os.environ["GEYSER_API_TOKEN"],
        geyser_auth_type=os.environ.get("GEYSER_AUTH_TYPE", "x-token"),
        platforms=[Platform.PUMP_FUN],
    )

    seen: list[TokenInfo] = []

    async def on_token(token_info: TokenInfo) -> None:
        seen.append(token_info)

    task = asyncio.create_task(listener.listen_for_tokens(on_token))
    try:
        deadline = timeout_seconds / 0.5
        for _ in range(int(deadline)):
            if seen:
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()

    return seen[0].mint if seen else None


async def build_token_info(
    mint: Pubkey, client: SolanaClient
) -> tuple[TokenInfo, dict]:
    """Assemble TokenInfo for an existing coin from its on-chain curve state.

    Args:
        mint: Base token mint
        client: Solana RPC client

    Returns:
        Tuple of (TokenInfo, decoded curve state)
    """
    implementations = get_platform_implementations(Platform.PUMP_FUN, client)
    provider = implementations.address_provider
    curve_manager = implementations.curve_manager

    bonding_curve = provider.derive_pool_address(mint)
    state = await curve_manager.get_pool_state(bonding_curve, commitment="processed")

    creator = state["creator"]
    creator = Pubkey.from_string(creator) if isinstance(creator, str) else creator
    quote_mint = normalize_quote_mint(state["quote_mint"])

    # Which token program owns the base mint decides the base ATA derivation.
    account = await client.get_account_info(mint)
    base_token_program = (
        SystemAddresses.TOKEN_2022_PROGRAM
        if str(account.owner) == str(SystemAddresses.TOKEN_2022_PROGRAM)
        else SystemAddresses.TOKEN_PROGRAM
    )

    token_info = TokenInfo(
        name="simulation",
        symbol="SIM",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=provider.derive_associated_bonding_curve(
            mint, bonding_curve, base_token_program
        ),
        creator=creator,
        creator_vault=provider.derive_creator_vault(creator),
        token_program_id=base_token_program,
        is_mayhem_mode=state["is_mayhem_mode"],
        is_cashback_coin=state["is_cashback_coin"],
        quote_mint=quote_mint,
        quote_token_program_id=quote_token_program(quote_mint),
    )
    return token_info, state


async def simulate(
    client: SolanaClient,
    payer: Pubkey,
    instructions: list,
    label: str,
    *,
    allowed_custom_error: int | None = None,
) -> bool:
    """Run instructions through simulateTransaction and report the result.

    Args:
        client: Solana RPC client
        payer: Existing funded account used by the simulated message
        instructions: Instructions to simulate
        label: Human-readable name for output
        allowed_custom_error: Optional custom program error accepted as proof
            that all preceding accounts validated

    Returns:
        True if simulation succeeds or reports the explicitly allowed error
    """
    blockhash = await client.get_latest_blockhash()
    message = Message.new_with_blockhash(
        [set_compute_unit_limit(SIMULATION_CU_LIMIT), *instructions],
        payer,
        blockhash,
    )
    transaction = Transaction.populate(
        message,
        [Signature.default()] * message.header.num_required_signatures,
    )

    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                b64encode(bytes(transaction)).decode(),
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                },
            ],
        }
    )

    if not response or "result" not in response:
        print(f"  {label}: RPC call failed: {response}")
        return False

    value = response["result"]["value"]
    err = value.get("err")
    units = value.get("unitsConsumed")
    account_count = len(instructions[-1].accounts)

    print(f"  {label}: accounts={account_count} unitsConsumed={units} err={err}")
    if err:
        for line in value.get("logs") or []:
            if "Error" in line or "failed" in line or "Instruction:" in line:
                print(f"      {line}")
    if err is None:
        return True
    if allowed_custom_error is None or not isinstance(err, dict):
        return False
    instruction_error = err.get("InstructionError")
    match instruction_error:
        case [_, {"Custom": custom_error}]:
            return custom_error == allowed_custom_error
        case _:
            return False


async def simulate_mint(
    client: SolanaClient,
    payer: Pubkey,
    mint: Pubkey,
    *,
    slippage_bps: int = SIMULATION_SLIPPAGE_BPS,
) -> bool:
    """Simulate fee-aware buy_v2 and sell_v2 paths for one active curve."""
    if (
        isinstance(slippage_bps, bool)
        or not isinstance(slippage_bps, int)
        or not 0 <= slippage_bps < _BASIS_POINTS
    ):
        message = "slippage_bps must be an integer from 0 through 9,999"
        raise ValueError(message)
    token_info, state = await build_token_info(mint, client)
    quote_mint = token_info.quote_mint

    print(f"\nmint:          {mint}")
    print(f"bonding_curve: {token_info.bonding_curve}")
    print(f"quote_mint:    {quote_mint}")
    print(f"base program:  {token_info.token_program_id}")
    print(
        f"mayhem={token_info.is_mayhem_mode} "
        f"cashback={token_info.is_cashback_coin} "
        f"complete={state['complete']}"
    )
    print(f"price:         {state['price_per_token']:.10f} quote/token")
    print(f"payer:         {payer}\n")

    implementations = get_platform_implementations(Platform.PUMP_FUN, client)
    provider = implementations.address_provider
    builder = implementations.instruction_builder
    curve_manager = implementations.curve_manager
    await curve_manager.prepare_live_execution()

    token_raw = SIMULATED_TOKEN_AMOUNT * 10**TOKEN_DECIMALS
    quoted_buy_raw = await curve_manager.calculate_buy_cost(
        token_info.bonding_curve,
        token_raw,
        pool_state=state,
    )
    max_quote_raw = (
        quoted_buy_raw * (_BASIS_POINTS + slippage_bps) + _BASIS_POINTS - 1
    ) // _BASIS_POINTS
    exact_in_token_raw = await curve_manager.calculate_buy_amount_out(
        token_info.bonding_curve,
        quoted_buy_raw,
        pool_state=state,
    )
    quoted_sell_raw = await curve_manager.calculate_sell_amount_out(
        token_info.bonding_curve,
        token_raw,
        pool_state=state,
    )
    min_quote_raw = quoted_sell_raw * (_BASIS_POINTS - slippage_bps) // _BASIS_POINTS

    print("simulating:")
    exact_in_instructions = await builder.build_buy_v2_instruction(
        token_info,
        payer,
        quoted_buy_raw,
        exact_in_token_raw,
        provider,
    )
    exact_in_ok = await simulate(
        client,
        payer,
        exact_in_instructions,
        "buy_v2 exact-in",
    )
    buy_instructions = await builder.build_buy_v2_instruction(
        token_info,
        payer,
        max_quote_raw,
        token_raw,
        provider,
    )
    buy_ok = await simulate(client, payer, buy_instructions, "buy_v2 ")

    sell_instructions = await builder.build_sell_v2_instruction(
        token_info,
        payer,
        token_raw,
        min_quote_raw,
        provider,
    )
    sell_layout_ok = await simulate(
        client,
        payer,
        sell_instructions,
        "sell_v2",
        allowed_custom_error=3012,
    )
    if sell_layout_ok:
        print(
            "      ^ expected to fail with AccountNotInitialized on "
            "associated_base_user unless\n        the payer already holds this "
            "coin. That error means all 26 accounts validated."
        )
    else:
        print("      ^ unexpected standalone sell failure; verifier will fail")

    # Buy and sell in one transaction so the position exists mid-tx. This is
    # the only way to get a representative sell_v2 CU number without actually
    # holding the coin. Sell slightly less than bought to stay inside the
    # realised balance.
    resell_raw = token_raw * 9 // 10
    quoted_resell_raw = await curve_manager.calculate_sell_amount_out(
        token_info.bonding_curve,
        resell_raw,
        pool_state=state,
    )
    min_resell_raw = quoted_resell_raw * (_BASIS_POINTS - slippage_bps) // _BASIS_POINTS
    combined = [*buy_instructions, *sell_instructions[:-1]]
    combined.append(
        (
            await builder.build_sell_v2_instruction(
                token_info,
                payer,
                resell_raw,
                min_resell_raw,
                provider,
            )
        )[-1]
    )
    combined_ok = await simulate(client, payer, combined, "buy+sell")
    if combined_ok:
        print("      ^ subtract the buy_v2 figure above to estimate sell_v2 CU")
    return exact_in_ok and buy_ok and sell_layout_ok and combined_ok


async def main() -> int:
    """Discover or select a coin, then run the read-only simulation."""
    load_dotenv(PROJECT_ROOT / ".env")
    mint_arg = sys.argv[1] if len(sys.argv) > 1 else None

    if mint_arg:
        mint = Pubkey.from_string(mint_arg)
    else:
        print("No mint given; listening for a fresh pump.fun coin via geyser...")
        mint = await discover_mint()
        if mint is None:
            print("No coin seen before timeout. Pass a mint explicitly.")
            return 2

    client = SolanaClient(os.environ["SOLANA_NODE_RPC_ENDPOINT"])
    payer = PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    try:
        return 0 if await simulate_mint(client, payer, mint) else 1
    finally:
        implementations = get_platform_implementations(Platform.PUMP_FUN, client)
        await implementations.curve_manager.close()
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
