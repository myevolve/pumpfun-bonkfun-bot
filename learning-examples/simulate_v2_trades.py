"""Simulate pump.fun buy_v2 / sell_v2 against mainnet without spending funds.

Builds both instructions through the bot's real code path (address provider,
instruction builder, curve manager) and runs them through
`simulateTransaction`, which executes the program against live state but never
submits. Reports the program error (if any) and the compute units consumed —
use the latter to tune `get_buy_compute_unit_limit` / `get_sell_compute_unit_limit`.

The transaction uses a public-key-only payer (a funded protocol address by
default), carries only default signatures, and disables signature verification.
No dotenv file or private key is read. RPC and public log subscriptions default
to api.mainnet-beta.solana.com; SOLANA_NODE_RPC_ENDPOINT and
SOLANA_NODE_WSS_ENDPOINT may override them.

Usage:
    # simulate against a specific coin
    uv run learning-examples/simulate_v2_trades.py <MINT>

    # discover a fresh coin via the bot's public logs listener, then simulate it
    uv run learning-examples/simulate_v2_trades.py

    # use the fixed public readiness profile and a public-key-only payer
    uv run learning-examples/simulate_v2_trades.py <MINT> --configured-size --payer <PUBKEY>
"""
# Domain validation reports specific refusal reasons, as the quote engine does.
# ruff: noqa: TRY003

import argparse
import asyncio
import os
import sys
from base64 import b64encode
from contextlib import suppress
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # noqa: E402
from solders.compute_budget import (  # noqa: E402
    set_compute_unit_limit,
    set_compute_unit_price,
)
from solders.message import Message  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import Transaction  # noqa: E402
from spl.token.instructions import (  # noqa: E402
    CloseAccountParams,
    close_account,
    get_associated_token_address,
)

from core.client import (  # noqa: E402
    SolanaClient,
    estimate_transaction_fee_lamports,
    set_loaded_accounts_data_size_limit,
)
from core.pubkeys import (  # noqa: E402
    TOKEN_DECIMALS,
    SystemAddresses,
    is_sol_paired,
    normalize_quote_mint,
    quote_token_program,
)
from interfaces.core import Platform, TokenInfo  # noqa: E402
from monitoring.listener_factory import ListenerFactory  # noqa: E402
from platforms import get_platform_implementations  # noqa: E402
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402

PACKET_LIMIT = 1232
# Buy this many whole tokens in the simulation. Small enough that the quote cost
# stays well under the unsigned simulation payer's balance on a fresh curve.
SIMULATED_TOKEN_AMOUNT = 20
# Generous ceiling so simulation reports true consumption rather than hitting
# the limit. The real bot uses the builder's tuned values.
SIMULATION_CU_LIMIT = 400_000
MAX_COMPUTE_UNIT_LIMIT = 1_400_000
SIMULATION_SLIPPAGE_BPS = 3_000
_BASIS_POINTS = 10_000
CONFIGURED_PROFILE = PROJECT_ROOT / ".state/configs/live-readiness.yaml"


def load_configured_profile() -> dict[str, int]:  # noqa: C901
    """Read public numeric policy only, never interpolate or load env_file."""
    with CONFIGURED_PROFILE.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("Readiness profile must be a YAML mapping")  # noqa: TRY004
    sections = ("trade", "execution", "priority_fees", "compute_units", "filters")
    if any(not isinstance(config.get(section), dict) for section in sections):
        raise ValueError("Readiness profile is missing required sections")
    trade, execution = config["trade"], config["execution"]
    priority, compute = config["priority_fees"], config["compute_units"]
    if (
        config["filters"].get("allowed_quote_mints") != ["sol"]
        or trade.get("extreme_fast_mode") is not True
        or priority.get("enable_fixed") is not True
        or priority.get("enable_dynamic") is not False
        or priority.get("extra_percentage") != 0
    ):
        raise ValueError(
            "Configured probe requires SOL-only fixed-priority exact-out mode"
        )
    profile = {}
    for name, value, maximum in (
        (
            "token_amount",
            trade.get("extreme_fast_token_amount"),
            (2**64 - 1) // 10**TOKEN_DECIMALS,
        ),
        ("quote_cap", execution.get("max_trade_quote_raw"), 2**64 - 1),
        ("fee_cap", execution.get("max_total_fee_lamports"), 2**64 - 1),
        ("priority_fee", priority.get("fixed_amount"), 2**64 - 1),
        ("priority_cap", priority.get("hard_cap"), 2**64 - 1),
        ("buy_cu", compute.get("buy"), 1_400_000),
        ("sell_cu", compute.get("sell"), 1_400_000),
    ):
        if type(value) is not int or not 0 < value <= maximum:
            raise ValueError(f"Invalid configured {name}")
        profile[name] = value
    for side in ("buy", "sell"):
        value = trade.get(f"{side}_slippage")
        if type(value) not in (int, float):
            raise ValueError(f"Invalid configured {side} slippage")
        decimal = Decimal(str(value))
        if not decimal.is_finite() or not 0 <= decimal < 1:
            raise ValueError(f"Invalid configured {side} slippage")
        profile[f"{side}_slippage_bps"] = int(decimal * _BASIS_POINTS)
    amount = trade.get("buy_amount")
    if type(amount) not in (int, float):
        raise ValueError("Invalid configured buy amount")
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid configured buy amount")
    quote_raw = int(amount * 1_000_000_000)
    max_quote = (
        quote_raw * (_BASIS_POINTS + profile["buy_slippage_bps"]) + _BASIS_POINTS - 1
    ) // _BASIS_POINTS
    if not 0 < max_quote <= profile["quote_cap"]:
        raise ValueError("Configured buy amount plus slippage exceeds quote policy cap")
    profile["max_quote"] = max_quote
    if (
        profile["priority_fee"] > profile["priority_cap"]
        or profile["buy_cu"] + profile["sell_cu"] > MAX_COMPUTE_UNIT_LIMIT
    ):
        raise ValueError("Configured priority fee or atomic CU exceeds its cap")
    return profile


async def discover_mint(timeout_seconds: float = 45.0) -> Pubkey | None:
    """Listen for a fresh coin through the bot's public logs listener."""

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
        deadline = timeout_seconds / 0.5
        for _ in range(int(deadline)):
            if seen:
                break
            await asyncio.sleep(0.5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

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


def _verify_closed_account(value: dict, message: Message, account: Pubkey) -> bool:
    """Require a newly opened ATA to disappear, without guessing missing state."""
    before, after = value.get("preBalances"), value.get("postBalances")
    keys = message.account_keys
    states = value.get("accounts")
    fee = value.get("fee")
    if (
        not isinstance(before, list)
        or not isinstance(after, list)
        or len(before) != len(keys)
        or len(after) != len(keys)
        or any(
            type(amount) is not int or amount < 0
            for amounts in (before, after)
            for amount in amounts
        )
        or not isinstance(states, list)
        or len(states) != 1
        or type(fee) is not int
        or fee < 0
    ):
        print("      closure unverified: missing or malformed balance/account evidence")
        return False
    state = states[0]
    # RPCs report a closed account as either null or an empty System tombstone.
    closed = state is None or (
        isinstance(state, dict)
        and state.get("lamports") == 0
        and state.get("owner") == str(SystemAddresses.SYSTEM_PROGRAM)
        and state.get("executable") is False
        and state.get("data") == ["", "base64"]
    )
    index = keys.index(account)
    if before[index] != 0 or after[index] != 0 or not closed:
        print("      closure unverified: ATA pre-existed or retains balance/state")
        return False
    print(
        f"      full inventory sold; newly opened ATA closed: {account}\n"
        f"      fee={fee} lamports; payer native balance delta={after[0] - before[0]}"
        " lamports (not trading PnL; other account rent may remain)"
    )
    return True


async def simulate(  # noqa: C901, PLR0911, PLR0912, PLR0913
    client: SolanaClient,
    payer: Pubkey,
    instructions: list,
    label: str,
    *,
    allowed_custom_error: int | None = None,
    closed_account: Pubkey | None = None,
    compute_unit_limit: int = SIMULATION_CU_LIMIT,
    priority_fee: int | None = None,
    fee_cap: int | None = None,
) -> bool:
    """Run instructions through simulateTransaction and report the result.

    Args:
        client: Solana RPC client
        payer: Existing funded account used by the simulated message
        instructions: Instructions to simulate
        label: Human-readable name for output
        allowed_custom_error: Optional custom program error accepted as proof
            that all preceding accounts validated
        closed_account: Require this newly opened token account to be fully closed
        compute_unit_limit: Enforced transaction CU ceiling
        priority_fee: Fixed micro-lamports per CU, if configured
        fee_cap: Require reported and estimated transaction fees within this cap

    Returns:
        True if simulation succeeds or reports the explicitly allowed error
    """
    if (
        type(compute_unit_limit) is not int
        or not 0 < compute_unit_limit <= MAX_COMPUTE_UNIT_LIMIT
    ):
        raise ValueError("Invalid simulation CU limit")
    if priority_fee is not None and (
        type(priority_fee) is not int or not 0 <= priority_fee < 2**64
    ):
        raise ValueError("Invalid simulation priority fee")
    if fee_cap is not None and (type(fee_cap) is not int or fee_cap <= 0):
        raise ValueError("Invalid simulation fee cap")
    preamble = [
        set_loaded_accounts_data_size_limit(16 * 1024 * 1024),
        set_compute_unit_limit(compute_unit_limit),
    ]
    if priority_fee is not None:
        preamble.append(set_compute_unit_price(priority_fee))
    blockhash = await client.get_latest_blockhash()
    message = Message.new_with_blockhash(
        [*preamble, *instructions],
        payer,
        blockhash,
    )
    estimated_fee = estimate_transaction_fee_lamports(
        priority_fee, compute_unit_limit
    ) + 5_000 * (message.header.num_required_signatures - 1)
    if fee_cap is not None and estimated_fee > fee_cap:
        print(f"  {label}: estimated fee {estimated_fee} exceeds cap {fee_cap}")
        return False
    transaction = Transaction.populate(
        message,
        [Signature.default()] * message.header.num_required_signatures,
    )
    wire = bytes(transaction)
    if len(wire) > PACKET_LIMIT:
        print(f"  {label}: transaction exceeds the Solana packet limit")
        return False
    print(
        f"  {label}: packet_bytes={len(wire)}/{PACKET_LIMIT} "
        f"default_signatures={len(transaction.signatures)} "
        f"cu_limit={compute_unit_limit} priority_micro_lamports_per_cu={priority_fee} "
        f"estimated_fee_lamports={estimated_fee} fee_cap={fee_cap}"
    )

    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                b64encode(wire).decode(),
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                    **(
                        {
                            "accounts": {
                                "encoding": "base64",
                                "addresses": [str(closed_account)],
                            }
                        }
                        if closed_account is not None
                        else {}
                    ),
                },
            ],
        }
    )

    result = response.get("result") if isinstance(response, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict) or "err" not in value:
        print(f"  {label}: RPC returned no simulation result")
        return False

    err = value["err"]
    units = value.get("unitsConsumed")
    print(f"  {label}: unitsConsumed={units} err={err}")
    # Keep invocation depth and raw consumed-CU lines, including successful paths.
    logs = value.get("logs")
    if isinstance(logs, list):
        for line in logs:
            print(f"      {line}")
    if type(units) is not int or not 0 < units <= compute_unit_limit:
        print(f"  {label}: RPC omitted valid bounded simulation compute consumption")
        return False
    if fee_cap is not None:
        fee = value.get("fee")
        if (
            type(fee) is not int
            or not 0 <= fee <= fee_cap
            or not isinstance(logs, list)
            or not logs
            or any(not isinstance(line, str) for line in logs)
        ):
            print(f"  {label}: missing/invalid logs or fee, or fee exceeds cap: {fee}")
            return False
    if err is None:
        return closed_account is None or _verify_closed_account(
            value, message, closed_account
        )
    if (
        closed_account is not None
        or allowed_custom_error is None
        or not isinstance(err, dict)
    ):
        return False
    instruction_error = err.get("InstructionError")
    match instruction_error:
        case [_, {"Custom": custom_error}]:
            return custom_error == allowed_custom_error
        case _:
            return False


async def simulate_mint(  # noqa: PLR0915
    client: SolanaClient,
    payer: Pubkey,
    mint: Pubkey,
    *,
    slippage_bps: int = SIMULATION_SLIPPAGE_BPS,
    configured_size: bool = False,
) -> bool:
    """Simulate fee-aware buy_v2 and sell_v2 paths for one active curve."""
    if (
        isinstance(slippage_bps, bool)
        or not isinstance(slippage_bps, int)
        or not 0 <= slippage_bps < _BASIS_POINTS
    ):
        message = "slippage_bps must be an integer from 0 through 9,999"
        raise ValueError(message)
    profile = load_configured_profile() if configured_size else None
    buy_options = (
        {
            "compute_unit_limit": profile["buy_cu"],
            "priority_fee": profile["priority_fee"],
            "fee_cap": profile["fee_cap"],
        }
        if profile
        else {}
    )
    sell_options = (
        {**buy_options, "compute_unit_limit": profile["sell_cu"]} if profile else {}
    )
    implementations = get_platform_implementations(Platform.PUMP_FUN, client)
    provider = implementations.address_provider
    builder = implementations.instruction_builder
    curve_manager = implementations.curve_manager
    await curve_manager.prepare_live_execution()
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
    if profile and not is_sol_paired(quote_mint):
        print("Configured profile rejects non-SOL quote mint")
        return False
    if state["complete"]:
        print("Completed curve cannot exercise a bonding-curve buy")
        return False
    token_amount = profile["token_amount"] if profile else SIMULATED_TOKEN_AMOUNT
    print(
        f"requested whole tokens={token_amount}; raw tokens={token_amount * 10**TOKEN_DECIMALS}"
    )
    if profile:
        print(f"configured bounds: {profile}")
    print(
        "Atomic buy+100% sell+close is NOT two separately submitted transactions. "
        "Aggregate CU success does not prove each leg fits its standalone CU limit "
        "or a later-time liquidation; raw per-instruction logs follow."
    )

    token_raw = token_amount * 10**TOKEN_DECIMALS
    quoted_buy_raw = await curve_manager.calculate_buy_cost(
        token_info.bonding_curve,
        token_raw,
        pool_state=state,
    )
    max_quote_raw = (
        quoted_buy_raw * (_BASIS_POINTS + slippage_bps) + _BASIS_POINTS - 1
    ) // _BASIS_POINTS
    if profile:
        max_quote_raw = profile["max_quote"]
        if type(quoted_buy_raw) is not int or not 0 < quoted_buy_raw <= max_quote_raw:
            print(
                f"Configured buy rejected: observed quote={quoted_buy_raw} "
                f"exceeds/invalid for unchanged ceiling={max_quote_raw}"
            )
            return False
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
    sell_slippage_bps = profile["sell_slippage_bps"] if profile else slippage_bps
    min_quote_raw = (
        quoted_sell_raw * (_BASIS_POINTS - sell_slippage_bps) // _BASIS_POINTS
    )
    print(
        f"observed_buy_quote_raw={quoted_buy_raw} max_buy_quote_raw={max_quote_raw} "
        f"min_sell_quote_raw={min_quote_raw}; full_sell_quantity_raw={token_raw}"
    )

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
        "buy_v2 inverse-quote exact-out",
        **buy_options,
    )
    buy_instructions = await builder.build_buy_v2_instruction(
        token_info,
        payer,
        max_quote_raw,
        token_raw,
        provider,
    )
    buy_ok = await simulate(
        client, payer, buy_instructions, "buy_v2 exact-out", **buy_options
    )

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
        **sell_options,
    )
    if sell_layout_ok:
        print(
            "      ^ expected to fail with AccountNotInitialized on "
            "associated_base_user unless\n        the payer already holds this "
            "coin. Full sell execution is checked by buy+sell below."
        )
    else:
        print("      ^ unexpected standalone sell failure; verifier will fail")

    # Exact-out buy acquires token_raw; sell all of it and reclaim the base ATA.
    # Closing fails on dust. Pre/post evidence also rejects pre-existing inventory.
    base_account = get_associated_token_address(
        payer, mint, token_program_id=token_info.token_program_id
    )
    combined = [
        *buy_instructions,
        *sell_instructions,
        close_account(
            CloseAccountParams(
                program_id=token_info.token_program_id,
                account=base_account,
                dest=payer,
                owner=payer,
            )
        ),
    ]
    combined_options = (
        {**buy_options, "compute_unit_limit": profile["buy_cu"] + profile["sell_cu"]}
        if profile
        else {}
    )
    combined_ok = await simulate(
        client,
        payer,
        combined,
        "atomic buy+full-sell+close",
        closed_account=base_account,
        **combined_options,
    )
    print(
        f"acquired_and_closed_inventory_verified={combined_ok}; "
        f"requested_raw={token_raw}; standalone_sell_is_not_liquidation=True"
    )
    return exact_in_ok and buy_ok and sell_layout_ok and combined_ok


async def main() -> int:
    """Discover or select a coin, then run the read-only simulation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mint", nargs="?", type=Pubkey.from_string)
    parser.add_argument("--configured-size", action="store_true")
    parser.add_argument("--payer", type=Pubkey.from_string)
    args = parser.parse_args()
    if args.configured_size:
        load_configured_profile()  # Reject invalid public policy before RPC/discovery.

    if args.mint:
        mint = args.mint
    else:
        print("No mint given; listening for a fresh pump.fun coin via public logs...")
        mint = await discover_mint()
        if mint is None:
            print("No coin seen before timeout. Pass a mint explicitly.")
            return 2

    client = SolanaClient(
        os.environ.get(
            "SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"
        )
    )
    payer = (
        args.payer
        if args.payer is not None
        else PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    )
    try:
        return (
            0
            if await simulate_mint(
                client, payer, mint, configured_size=args.configured_size
            )
            else 1
        )
    finally:
        implementations = get_platform_implementations(Platform.PUMP_FUN, client)
        await implementations.curve_manager.close()
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
