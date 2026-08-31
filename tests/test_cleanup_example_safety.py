# ruff: noqa: S101
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from core.client import TransactionSubmissionUnknown
from core.pubkeys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT


def _load_cleanup_example() -> ModuleType:
    path = Path(__file__).parents[1] / "learning-examples" / "cleanup_accounts.py"
    spec = importlib.util.spec_from_file_location("cleanup_accounts_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_manual_cleanup_refuses_to_burn_nonzero_tokens() -> None:
    cleanup = _load_cleanup_example()
    account = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    wallet = SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=Keypair())
    ledger = SimpleNamespace(reserve_operation_intent=MagicMock())
    client = SimpleNamespace(
        ledger=ledger,
        get_account_info=AsyncMock(
            return_value=SimpleNamespace(owner=TOKEN_2022_PROGRAM)
        ),
        get_token_account_balance=AsyncMock(return_value=1),
        build_and_send_transaction=AsyncMock(),
        confirm_transaction=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="refuses to burn"):
        await cleanup.close_account_if_safe(
            client,
            wallet,
            account,
            mint,
            TOKEN_2022_PROGRAM,
        )

    client.build_and_send_transaction.assert_not_awaited()
    ledger.reserve_operation_intent.assert_not_called()


@pytest.mark.asyncio
async def test_manual_cleanup_uses_authorized_safe_submission_contract() -> None:
    cleanup = _load_cleanup_example()
    account = Pubkey.new_unique()
    wallet = SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=Keypair())
    signature = Signature.default()
    intent_id = f"manual-cleanup:{wallet.pubkey}:{WSOL_MINT}:{account}:1"
    ledger = SimpleNamespace(reserve_operation_intent=MagicMock(return_value=intent_id))
    client = SimpleNamespace(
        ledger=ledger,
        get_account_info=AsyncMock(
            side_effect=[
                SimpleNamespace(owner=TOKEN_PROGRAM),
                ValueError("account not found"),
            ]
        ),
        get_token_account_balance=AsyncMock(return_value=50_000),
        build_and_send_transaction=AsyncMock(return_value=signature),
        confirm_transaction=AsyncMock(return_value=True),
    )

    await cleanup.close_account_if_safe(
        client,
        wallet,
        account,
        WSOL_MINT,
        TOKEN_PROGRAM,
    )

    submission = client.build_and_send_transaction.await_args
    assert submission.kwargs["skip_preflight"] is False
    assert submission.kwargs["quote_amount_raw"] == 0
    assert submission.kwargs["quote_mint"] == WSOL_MINT
    assert submission.kwargs["intent_id"] == intent_id
    ledger.reserve_operation_intent.assert_called_once_with(
        f"manual-cleanup:{wallet.pubkey}:{WSOL_MINT}:{account}",
        str(wallet.pubkey),
    )
    instructions = submission.args[0]
    assert len(instructions) == 1
    assert instructions[0].program_id == TOKEN_PROGRAM
    assert bytes(instructions[0].data) == b"\x09"
    client.confirm_transaction.assert_awaited_once_with(signature)


@pytest.mark.asyncio
async def test_manual_cleanup_recovers_ambiguous_submission() -> None:
    cleanup = _load_cleanup_example()
    account = Pubkey.new_unique()
    wallet = SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=Keypair())
    ambiguous_signature = str(Signature.default())
    ledger = SimpleNamespace(
        reserve_operation_intent=MagicMock(return_value="cleanup-intent:1")
    )
    client = SimpleNamespace(
        ledger=ledger,
        get_account_info=AsyncMock(
            side_effect=[
                SimpleNamespace(owner=TOKEN_PROGRAM),
                ValueError("account not found"),
            ]
        ),
        get_token_account_balance=AsyncMock(return_value=0),
        build_and_send_transaction=AsyncMock(
            side_effect=TransactionSubmissionUnknown(
                ambiguous_signature,
                "RPC response lost",
            )
        ),
        confirm_transaction=AsyncMock(return_value=True),
    )

    await cleanup.close_account_if_safe(
        client,
        wallet,
        account,
        WSOL_MINT,
        TOKEN_PROGRAM,
    )

    client.confirm_transaction.assert_awaited_once_with(ambiguous_signature)


@pytest.mark.asyncio
async def test_manual_cleanup_rejects_false_success_when_account_remains() -> None:
    cleanup = _load_cleanup_example()
    account = Pubkey.new_unique()
    wallet = SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=Keypair())
    signature = Signature.default()
    ledger = SimpleNamespace(
        reserve_operation_intent=MagicMock(return_value="cleanup-intent:1")
    )
    client = SimpleNamespace(
        ledger=ledger,
        get_account_info=AsyncMock(return_value=SimpleNamespace(owner=TOKEN_PROGRAM)),
        get_token_account_balance=AsyncMock(return_value=0),
        build_and_send_transaction=AsyncMock(return_value=signature),
        confirm_transaction=AsyncMock(return_value=True),
    )

    with pytest.raises(RuntimeError, match="still exists"):
        await cleanup.close_account_if_safe(
            client,
            wallet,
            account,
            WSOL_MINT,
            TOKEN_PROGRAM,
        )

    expected_account_reads = 2
    assert client.get_account_info.await_count == expected_account_reads
