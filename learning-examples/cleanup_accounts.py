"""Close an empty token ATA or unwrap WSOL through the live safety contract.

This tool never burns tokens. It requires a validated live bot config and the
same explicit runtime authorization used by ``pump_bot``.
"""
# ruff: noqa: TRY003

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from solders.pubkey import Pubkey
from spl.token.instructions import CloseAccountParams, close_account

from bot_runner import build_execution_policy
from config_loader import load_bot_config
from core.client import SolanaClient, TransactionSubmissionUnknown
from core.pubkeys import WSOL_MINT, SystemAddresses
from core.transaction_ledger import (
    TransactionLedger,
    resolve_transaction_ledger_path,
)
from core.wallet import Wallet
from utils.logger import get_logger

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse an explicit config, mint, and live authorization."""
    parser = argparse.ArgumentParser(
        description="Safely close an empty ATA or unwrap a WSOL ATA."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--mint", required=True, type=Pubkey.from_string)
    parser.add_argument(
        "--authorize-live",
        required=True,
        action="store_true",
        help="Acknowledge that this command can submit one live close transaction.",
    )
    return parser.parse_args()


async def resolve_token_program(client: SolanaClient, mint: Pubkey) -> Pubkey:
    """Return the supported token program that owns ``mint``."""
    info = await client.get_account_info(mint)
    owner = info.owner
    if owner not in (
        SystemAddresses.TOKEN_PROGRAM,
        SystemAddresses.TOKEN_2022_PROGRAM,
    ):
        raise ValueError(f"Mint {mint} is not owned by a supported token program")
    return owner


async def close_account_if_safe(
    client: SolanaClient,
    wallet: Wallet,
    account: Pubkey,
    mint: Pubkey,
    token_program: Pubkey,
) -> None:
    """Close an empty ATA or unwrap WSOL, refusing every token burn."""
    ledger = client.ledger
    if ledger is None:
        raise RuntimeError("cleanup requires an authorized transaction ledger")

    try:
        account_info = await client.get_account_info(account)
    except ValueError:
        logger.info("Account is already absent: %s", account)
        return
    if account_info.owner != token_program:
        raise RuntimeError(f"Token account {account} is owned by an unexpected program")

    balance = await client.get_token_account_balance(account)
    if balance > 0 and mint != WSOL_MINT:
        raise RuntimeError(
            f"Cleanup refuses to burn {balance} tokens from {account}; "
            "sell the position through the bot instead"
        )

    instruction = close_account(
        CloseAccountParams(
            account=account,
            dest=wallet.pubkey,
            owner=wallet.pubkey,
            program_id=token_program,
        )
    )
    operation_key = f"manual-cleanup:{wallet.pubkey}:{mint}:{account}"
    intent_id = await asyncio.to_thread(
        ledger.reserve_operation_intent,
        operation_key,
        str(wallet.pubkey),
    )
    try:
        signature = await client.build_and_send_transaction(
            [instruction],
            wallet.keypair,
            skip_preflight=False,
            quote_amount_raw=0,
            quote_mint=WSOL_MINT,
            intent_id=intent_id,
        )
    except TransactionSubmissionUnknown as exc:
        signature = exc.signature
        logger.warning(
            "Cleanup submission outcome is unresolved; reconciling %s",
            signature,
        )
    if not await client.confirm_transaction(signature):
        raise RuntimeError(
            f"Cleanup transaction {signature} landed without confirmed success"
        )
    try:
        await client.get_account_info(account)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            f"Cleanup transaction {signature} confirmed but account {account} "
            "still exists"
        )

    action = "Unwrapped and closed" if balance > 0 else "Closed"
    logger.info("%s token account %s", action, account)


async def run(config_path: Path, mint: Pubkey, *, authorize_live: bool) -> None:
    """Run one policy-authorized safe account close."""
    config = load_bot_config(config_path)
    policy = build_execution_policy(config, authorize_live=authorize_live)
    wallet = Wallet(config["private_key"])
    policy.validate_wallet(wallet.pubkey)

    with TransactionLedger(resolve_transaction_ledger_path(wallet.pubkey)) as ledger:
        client = SolanaClient(
            config["rpc_endpoint"],
            execution_policy=policy,
            ledger=ledger,
        )
        try:
            token_program = await resolve_token_program(client, mint)
            account = wallet.get_associated_token_address(mint, token_program)
            await close_account_if_safe(
                client,
                wallet,
                account,
                mint,
                token_program,
            )
        finally:
            await client.close()


async def main() -> int:
    """Return nonzero whenever validation, submission, or confirmation fails."""
    args = parse_args()
    try:
        await run(
            args.config,
            args.mint,
            authorize_live=args.authorize_live,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary must return nonzero.
        logger.error("Cleanup failed: %s", exc)  # noqa: TRY400
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
