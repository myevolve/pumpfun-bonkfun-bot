"""Dry-run the bot's real buy path against a freshly created coin.

Detects a new pump.fun coin with the bot's own listener, runs the same
PlatformAwareBuyer code the bot uses (the zero-RPC path for
CreateEvent-sourced tokens, or the curve refresh otherwise — watch the
state_from_event line in the output), but intercepts the transaction just
before submission and simulates it instead. This exercises the listener ->
event parser -> curve manager -> address provider -> instruction builder
chain as a unit.

No funds move: submission is replaced with unsigned simulation. No wallet key
or dotenv file is read. Public RPC/log subscriptions work without credentials;
SOLANA_NODE_RPC_ENDPOINT and SOLANA_NODE_WSS_ENDPOINT may override them.

Usage:
    uv run learning-examples/simulate_bot_buy_path.py
    uv run learning-examples/simulate_bot_buy_path.py --no-extreme-fast
"""

import asyncio
import os
import sys
from base64 import b64encode
from contextlib import suppress
from functools import partial
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from solders.compute_budget import (  # noqa: E402
    set_compute_unit_limit,
    set_compute_unit_price,
)
from solders.instruction import Instruction  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.null_signer import NullSigner  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import Transaction  # noqa: E402
from spl.token.instructions import get_associated_token_address  # noqa: E402

from core.client import SolanaClient, set_loaded_accounts_data_size_limit  # noqa: E402
from core.priority_fee.manager import PriorityFeeManager  # noqa: E402
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from trading.platform_aware import PlatformAwareBuyer  # noqa: E402

BUY_AMOUNT_SOL = 0.0001
EXTREME_FAST_TOKEN_AMOUNT = 20
# Matches retries.wait_after_creation in the bot configs. Only used when
# extreme_fast_mode is off, where the buyer reads the curve at `confirmed`.
CURVE_STABILIZE_SECONDS = 15


async def wait_for_token(timeout_seconds: float = 90.0) -> TokenInfo | None:
    """Wait for a fresh coin using the bot's public logs listener."""
    listener = ListenerFactory.create_listener(
        listener_type="logs",
        wss_endpoint=os.environ.get(
            "SOLANA_NODE_WSS_ENDPOINT", "wss://api.mainnet-beta.solana.com"
        ),
        platforms=[Platform.PUMP_FUN],
    )

    seen: list[TokenInfo] = []

    async def on_token(token_info: TokenInfo) -> None:
        seen.append(token_info)

    task = asyncio.create_task(listener.listen_for_tokens(on_token))
    try:
        for _ in range(int(timeout_seconds / 0.5)):
            if seen:
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    return seen[0] if seen else None


def install_simulation_hook(client: SolanaClient) -> dict:
    """Replace transaction submission with simulation.

    Args:
        client: Client whose send path should be intercepted

    Returns:
        Dict that will be populated with the simulation outcome
    """
    outcome: dict = {}

    async def simulate_instead(
        instructions: list[Instruction],
        signer_keypair: NullSigner,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        account_data_size_limit: int | None = None,
        **_submission_context: object,
    ) -> str:
        outcome.clear()
        preamble = []
        if account_data_size_limit is not None:
            preamble.append(
                set_loaded_accounts_data_size_limit(account_data_size_limit)
            )
        if compute_unit_limit is not None:
            preamble.append(set_compute_unit_limit(compute_unit_limit))
        if priority_fee is not None:
            preamble.append(set_compute_unit_price(priority_fee))

        blockhash = await client.get_latest_blockhash()
        message = Message.new_with_blockhash(
            [*preamble, *instructions], signer_keypair.pubkey(), blockhash
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
        result = response.get("result") if isinstance(response, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict) or "err" not in value:
            message = "Simulation RPC returned no execution result"
            raise ValueError(message)
        units = value.get("unitsConsumed")
        if value["err"] is None and (
            isinstance(units, bool) or not isinstance(units, int) or units <= 0
        ):
            message = "Simulation RPC omitted compute consumption"
            raise ValueError(message)
        outcome.update(
            {
                "err": value.get("err"),
                "units": value.get("unitsConsumed"),
                "logs": value.get("logs") or [],
                "cu_limit": compute_unit_limit,
                "priority_fee": priority_fee,
                "instruction_count": len(instructions),
                "account_count": len(instructions[-1].accounts),
            }
        )
        # Returning a sentinel signature: confirm_transaction is stubbed below.
        return "SIMULATED"

    async def never_confirm(_signature: object, **_kwargs: object) -> bool:
        return False

    client.build_and_send_transaction = simulate_instead
    client.confirm_transaction = never_confirm
    return outcome


async def main() -> int:
    """Run the bot's buy path in simulation mode.

    Returns:
        Process exit code (0 if the simulated buy had no program error)
    """
    extreme_fast = "--no-extreme-fast" not in sys.argv

    print("Waiting for a fresh pump.fun coin via the bot's public logs listener...")
    token_info = await wait_for_token()
    if token_info is None:
        print("No coin detected before timeout.")
        return 2

    print(f"\ndetected:   {token_info.symbol} ({token_info.mint})")
    print(f"quote_mint (from CreateEvent): {token_info.quote_mint}")
    print(f"token program: {token_info.token_program_id}")
    print(f"mayhem={token_info.is_mayhem_mode} cashback={token_info.is_cashback_coin}")
    print(f"state_from_event={token_info.state_from_event} (True = zero-RPC buy path)")
    print(f"extreme_fast_mode={extreme_fast}\n")

    client = SolanaClient(
        os.environ.get(
            "SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"
        )
    )
    payer = PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    wallet = SimpleNamespace(
        pubkey=payer,
        keypair=NullSigner(payer),
        get_associated_token_address=partial(get_associated_token_address, payer),
    )
    priority_fee_manager = PriorityFeeManager(
        client=client,
        enable_dynamic_fee=False,
        enable_fixed_fee=True,
        fixed_fee=1_000_000,
        extra_fee=0.0,
        hard_cap=1_000_000,
    )

    outcome = install_simulation_hook(client)
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        priority_fee_manager,
        BUY_AMOUNT_SOL,
        slippage=0.3,
        max_retries=1,
        extreme_fast_token_amount=EXTREME_FAST_TOKEN_AMOUNT,
        extreme_fast_mode=extreme_fast,
    )

    curve_manager = get_platform_implementations(
        Platform.PUMP_FUN, client
    ).curve_manager
    try:
        # UniversalTrader.start performs this attestation before starting its
        # listener. Mirror that lifecycle so CreateEvent-backed simulations
        # carry the same validated fee snapshot as the production buy path.
        await curve_manager.prepare_live_execution()
        if not extreme_fast:
            # Mirror the bot's retries.wait_after_creation pause. Without it the
            # curve read races the account's confirmation and fails before any
            # instruction is built.
            print(f"Waiting {CURVE_STABILIZE_SECONDS}s for the curve to stabilize...")
            await asyncio.sleep(CURVE_STABILIZE_SECONDS)

        result = await buyer.execute(token_info)
    finally:
        await curve_manager.close()
        await client.close()

    if not outcome:
        print(f"Buy path never reached transaction submission: {result.error_message}")
        return 1

    print("simulated buy:")
    print(f"  instructions:  {outcome['instruction_count']}")
    print(f"  trade accounts: {outcome['account_count']}")
    print(f"  cu_limit:      {outcome['cu_limit']}")
    print(f"  unitsConsumed: {outcome['units']}")
    print(f"  err:           {outcome['err']}")

    if outcome["err"] is not None:
        for line in outcome["logs"]:
            if "Error" in line or "failed" in line or "Instruction:" in line:
                print(f"    {line}")
        return 1

    headroom = outcome["cu_limit"] - (outcome["units"] or 0)
    print(f"\nCU headroom: {headroom} ({headroom / outcome['cu_limit']:.0%})")
    print("Buy path validated end to end against live mainnet state.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
