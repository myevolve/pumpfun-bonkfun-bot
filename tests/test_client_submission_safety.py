# Assertions and private receipt fixtures deliberately exercise numeric boundaries.
# ruff: noqa: S101, SLF001, PLR2004

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import httpx
import pytest
from solana.exceptions import SolanaRpcException
from solana.rpc.core import RPCException
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.rpc.errors import SendTransactionPreflightFailureMessage
from solders.rpc.responses import RpcSimulateTransactionResult
from solders.signature import Signature
from solders.transaction import Transaction
from spl.token.instructions import get_associated_token_address

from core import client as client_module
from core.client import JsonRpcError, RpcUnavailableError, SolanaClient
from core.execution_policy import ExecutionBlocked, ExecutionPolicy, TradeLimitExceeded
from core.pubkeys import WSOL_MINT
from core.transaction_ledger import EvidencePersistenceError, TransactionLedger
from core.transaction_state import TransactionOutcome, TransactionStatus


def _live_client(
    tmp_path, rpc: object
) -> tuple[SolanaClient, TransactionLedger, Keypair]:
    signer = Keypair()
    policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=1_000_000,
        max_total_fee_lamports=100_000,
        risk_session_id="test-session",
        max_session_quote_raw=10_000_000,
        max_session_fee_lamports=10_000_000,
        allow_skip_preflight=True,
    )
    ledger = TransactionLedger(tmp_path / "transactions.sqlite3")
    client = SolanaClient(
        "http://offline.invalid", execution_policy=policy, ledger=ledger
    )
    client._client = rpc
    context = client_module._BlockhashContext(Hash.default(), 100)

    async def cached_blockhash() -> object:
        return context

    async def not_expired(_context) -> bool:
        return False

    async def height_not_exceeded(
        _last_valid_height: int,
        *,
        commitment: str,
    ) -> bool:
        return False

    client._get_cached_blockhash_context = cached_blockhash
    client._blockhash_is_expired = not_expired
    client._current_block_height_exceeds = height_not_exceeded
    return client, ledger, signer


def _instruction() -> Instruction:
    return Instruction(Pubkey.new_unique(), b"trade", [])


def _unknown_error_type() -> type[RuntimeError]:
    return getattr(client_module, "TransactionSubmissionUnknown", RuntimeError)


@pytest.mark.asyncio
async def test_default_submission_uses_policy_preflight_setting(tmp_path) -> None:
    """A caller that omits the transport option must inherit safe preflight."""
    observed_opts: list[object] = []

    async def send_transaction(
        transaction: Transaction,
        opts: object,
    ) -> SimpleNamespace:
        observed_opts.append(opts)
        return SimpleNamespace(value=transaction.signatures[0])

    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=send_transaction),
    )
    client, _ledger, signer = _live_client(tmp_path, rpc)
    client.execution_policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=1_000_000,
        max_total_fee_lamports=100_000,
        risk_session_id="test-session",
        max_session_quote_raw=10_000_000,
        max_session_fee_lamports=10_000_000,
        allow_skip_preflight=False,
    )

    await client.build_and_send_transaction(
        [_instruction()],
        signer,
        quote_amount_raw=10,
        fee_lamports=5_000,
        intent_id="policy-owned-preflight",
        quote_mint=WSOL_MINT,
    )

    assert len(observed_opts) == 1
    assert observed_opts[0].skip_preflight is False


@pytest.mark.asyncio
async def test_caller_compute_budget_instruction_is_rejected(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(client_module.ExecutionBlocked):
        await client.build_and_send_transaction(
            [set_compute_unit_limit(100_000), _instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="injected-compute-budget",
            quote_mint=WSOL_MINT,
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_newly_signed_transaction_retries_are_disabled(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(ValueError, match="max_retries must be 1"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            max_retries=2,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="unsafe-retry-count",
            quote_mint=WSOL_MINT,
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_compute_unit_limit_cannot_exceed_protocol_cap(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(ValueError, match="compute_unit_limit"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            compute_unit_limit=1_400_001,
            intent_id="oversized-compute-budget",
            quote_mint=WSOL_MINT,
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_priority_fee_cannot_exceed_wire_encoding_cap(tmp_path) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, _ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(ValueError, match="priority_fee"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            priority_fee=2**64,
            intent_id="oversized-priority-fee",
            quote_mint=WSOL_MINT,
        )

    rpc.send_transaction.assert_not_awaited()


@pytest.mark.parametrize(
    "transport_error",
    [
        aiohttp.ClientConnectionError("lost"),
        SolanaRpcException(Exception("lost"), lambda value: value, None, object()),
        RuntimeError("client wrapper failed after dispatch"),
    ],
    ids=["aiohttp", "solana-rpc", "generic-wrapper"],
)
@pytest.mark.asyncio
async def test_transport_failure_is_unknown_and_raises_with_signature(
    tmp_path,
    transport_error: BaseException,
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock(side_effect=transport_error))
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="ambiguous-send",
            quote_mint=WSOL_MINT,
        )

    signature = caught.value.signature
    outcome = ledger.get_outcome(signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN
    assert ledger.get_active_submission_record("ambiguous-send").state == "submitted"

    with pytest.raises(_unknown_error_type()):
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="ambiguous-send",
            quote_mint=WSOL_MINT,
        )
    assert rpc.send_transaction.await_count == 1


@pytest.mark.asyncio
async def test_response_signature_mismatch_is_unknown_not_success(tmp_path) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(
            return_value=SimpleNamespace(value=Signature.default())
        )
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="mismatched-response",
            quote_mint=WSOL_MINT,
        )

    outcome = ledger.get_outcome(caught.value.signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.parametrize(
    "malformed_response",
    [None, SimpleNamespace()],
    ids=["none", "missing-value"],
)
@pytest.mark.asyncio
async def test_malformed_send_response_is_signature_bearing_unknown(
    tmp_path, malformed_response
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock(return_value=malformed_response))
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id=f"malformed-response-{id(malformed_response)}",
            quote_mint=WSOL_MINT,
        )

    assert caught.value.signature
    outcome = ledger.get_outcome(caught.value.signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_rpc_error_after_send_started_raises_signature_bearing_unknown(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=RPCException("node unhealthy"))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="rpc-error-send",
            quote_mint=WSOL_MINT,
        )

    assert caught.value.signature
    assert (
        ledger.get_outcome(caught.value.signature).status is TransactionStatus.UNKNOWN
    )


@pytest.mark.asyncio
async def test_preflight_rejection_releases_wire_instead_of_recording_unknown(
    tmp_path,
) -> None:
    """Live 2026-09-03 (Ceuta): a sell rejected at preflight (6003) was filed as
    UNKNOWN, so the position froze for an hour while the coin went to zero."""
    from solders.rpc.errors import SendTransactionPreflightFailureMessage
    from solders.rpc.responses import RpcSimulateTransactionResult

    simulation = RpcSimulateTransactionResult(err=None, logs=None)
    payload = SendTransactionPreflightFailureMessage(
        "Transaction simulation failed: Error processing Instruction 2: "
        "custom program error: 0x1773",
        simulation,
    )
    rpc = SimpleNamespace(send_transaction=AsyncMock(side_effect=RPCException(payload)))
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(client_module.PreflightRejected) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="preflight-rejected",
            quote_mint=WSOL_MINT,
        )

    assert ledger.get_outcome(caught.value.signature) is None
    assert ledger.get_active_submission("preflight-rejected") is None
    # The intent may be retried with a fresh wire.
    rpc.send_transaction = AsyncMock(
        side_effect=RPCException(
            SendTransactionPreflightFailureMessage("again", simulation)
        )
    )
    with pytest.raises(client_module.PreflightRejected):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="preflight-rejected",
            quote_mint=WSOL_MINT,
        )


def _fake_tpu(*, quic_delivered: int = 0, udp_delivered: int = 0) -> SimpleNamespace:
    """TPU stub reporting delivered leader counts without touching the network."""
    return SimpleNamespace(
        start=lambda: None,
        send_quic=AsyncMock(return_value=quic_delivered),
        send=lambda _wire: udp_delivered,
        stop=AsyncMock(),
    )


def _record_prepared_wire(  # noqa: PLR0913
    ledger: TransactionLedger,
    intent_id: str,
    signer: Keypair,
    message: Message,
    transaction: Transaction,
    *,
    session_id: str = "test-session",
    receipt_destinations: tuple[str, ...] | None = None,
) -> None:
    """Reserve an exact wire the way the live path does, bound to its session."""
    message_hash = hashlib.sha256(bytes(message)).hexdigest()
    ledger.record_intent(intent_id, str(signer.pubkey()), 10, 5_000, message_hash)
    ledger.record_submission(
        intent_id,
        str(transaction.signatures[0]),
        str(transaction.message.recent_blockhash),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
        receipt_destinations=receipt_destinations,
        quote_mint=str(WSOL_MINT),
        risk_session_id=session_id,
        max_session_quote_raw=10_000_000,
        max_session_fee_lamports=10_000_000,
        intent_message_hash=message_hash,
    )


@pytest.mark.parametrize("failure", ["rate_limiter", "preflight"])
@pytest.mark.asyncio
async def test_tpu_delivered_wire_is_never_released_as_never_sent(
    tmp_path: Path, failure: str
) -> None:
    """A wire handed to TPU leaders can still land on a validator. Releasing it
    deletes the signature and its risk reservation, so the next attempt signs a
    second buy/sell for the same intent and the session cap stops counting it."""
    preflight_rejection = RPCException(
        SendTransactionPreflightFailureMessage(
            "Transaction simulation failed: Error processing Instruction 2: "
            "custom program error: 0x1773",
            RpcSimulateTransactionResult(err=None, logs=None),
        )
    )
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(
            side_effect=preflight_rejection if failure == "preflight" else None
        )
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    client._tpu = _fake_tpu(quic_delivered=1)
    if failure == "rate_limiter":
        client._rate_limiter.acquire = AsyncMock(side_effect=TimeoutError())

    with pytest.raises(client_module.TransactionSubmissionUnknown):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="tpu-delivered",
            quote_mint=WSOL_MINT,
        )

    record = ledger.get_active_submission_record("tpu-delivered")
    assert record is not None
    assert ledger.get_outcome(record.signature).status is TransactionStatus.UNKNOWN
    totals = ledger.get_session_risk_totals("test-session", str(signer.pubkey()))
    assert totals.submission_count == 1


@pytest.mark.asyncio
async def test_undelivered_preflight_rejection_still_releases_the_wire(
    tmp_path: Path,
) -> None:
    """No TPU leader took the wire, so 'never left' still holds and the caller
    may build a fresh wire for the same intent."""
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(
            side_effect=RPCException(
                SendTransactionPreflightFailureMessage(
                    "Transaction simulation failed",
                    RpcSimulateTransactionResult(err=None, logs=None),
                )
            )
        )
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    client._tpu = _fake_tpu()

    with pytest.raises(client_module.PreflightRejected):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="no-tpu-delivery",
            quote_mint=WSOL_MINT,
        )

    assert ledger.get_active_submission_record("no-tpu-delivery") is None
    totals = ledger.get_session_risk_totals("test-session", str(signer.pubkey()))
    assert totals.submission_count == 0


def test_preflight_rejection_detected_from_message_text_too() -> None:
    assert client_module.is_preflight_rejection(
        RPCException("SendTransactionPreflightFailureMessage { message: ... }")
    )
    assert not client_module.is_preflight_rejection(RPCException("node unhealthy"))


@pytest.mark.asyncio
async def test_blockhash_rpc_error_does_not_prove_expiry(tmp_path) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=RPCException("Blockhash not found"))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    async def not_proven(
        _signature: Signature,
        _last_valid_height: int,
        _commitment: str,
    ) -> bool:
        return False

    client._prove_transaction_expired = not_proven

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="unproven-blockhash-error",
            max_retries=1,
            quote_mint=WSOL_MINT,
        )

    outcome = ledger.get_outcome(caught.value.signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_prepared_wire_rpc_error_raises_signature_bearing_unknown(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(
        send_raw_transaction=AsyncMock(side_effect=RPCException("node unhealthy"))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    _record_prepared_wire(ledger, "prepared-rpc-error", signer, message, transaction)

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="prepared-rpc-error",
            quote_mint=WSOL_MINT,
        )

    assert caught.value.signature == signature
    assert ledger.get_outcome(signature).status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_prepared_malformed_response_is_signature_bearing_unknown(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock(return_value=None))
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    _record_prepared_wire(
        ledger, "prepared-malformed-response", signer, message, transaction
    )

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="prepared-malformed-response",
            quote_mint=WSOL_MINT,
        )

    assert caught.value.signature == signature
    outcome = ledger.get_outcome(signature)
    assert outcome is not None
    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_prepared_recovery_submits_the_exact_stored_wire_bytes(tmp_path) -> None:
    captured: list[bytes] = []

    async def send_raw_transaction(wire: bytes, _opts) -> object:
        captured.append(wire)
        return SimpleNamespace(value=prepared_signature)

    rpc = SimpleNamespace(send_raw_transaction=send_raw_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    prepared_wire = bytes(transaction)
    prepared_signature = transaction.signatures[0]
    _record_prepared_wire(ledger, "prepared-recovery", signer, message, transaction)

    returned = await client.build_and_send_transaction(
        [instruction],
        signer,
        quote_amount_raw=10,
        fee_lamports=5_000,
        intent_id="prepared-recovery",
        quote_mint=WSOL_MINT,
    )

    assert returned == prepared_signature
    assert captured == [prepared_wire]
    assert ledger.get_active_submission_record("prepared-recovery").state == "submitted"


@pytest.mark.asyncio
async def test_prepared_recovery_revalidates_current_budget_policy(tmp_path) -> None:
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    message = Message([_instruction()], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "prepared-over-current-budget",
        str(signer.pubkey()),
        100,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "prepared-over-current-budget",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
    )
    client.execution_policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=50,
        max_total_fee_lamports=100_000,
        risk_session_id="test-session",
        max_session_quote_raw=10_000_000,
        max_session_fee_lamports=10_000_000,
        allow_skip_preflight=True,
    )

    with pytest.raises(TradeLimitExceeded, match="quote amount"):
        await client.recover_active_submission("prepared-over-current-budget")

    rpc.send_raw_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_recovery_rejects_wire_signer_mismatch(tmp_path) -> None:
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    other_signer = Keypair()
    message = Message([_instruction()], other_signer.pubkey())
    transaction = Transaction([other_signer], message, Hash.default())
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "prepared-wrong-wire-signer",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "prepared-wrong-wire-signer",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="prepared",
    )

    with pytest.raises(ExecutionBlocked, match="does not match"):
        await client.recover_active_submission("prepared-wrong-wire-signer")

    rpc.send_raw_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepared_recovery_ignores_rebuilt_message_and_receipt_context(
    tmp_path,
) -> None:
    captured: list[bytes] = []

    async def send_raw_transaction(wire: bytes, _opts) -> object:
        captured.append(wire)
        return SimpleNamespace(value=prepared_signature)

    rpc = SimpleNamespace(send_raw_transaction=send_raw_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    original_instruction = _instruction()
    original_message = Message([original_instruction], signer.pubkey())
    transaction = Transaction([signer], original_message, Hash.default())
    prepared_wire = bytes(transaction)
    prepared_signature = transaction.signatures[0]
    original_destinations = tuple(str(Pubkey.new_unique()) for _ in range(3))
    rebuilt_destinations = tuple(str(Pubkey.new_unique()) for _ in range(3))
    _record_prepared_wire(
        ledger,
        "changed-prepared-recovery",
        signer,
        original_message,
        transaction,
        receipt_destinations=original_destinations,
    )

    returned = await client.build_and_send_transaction(
        [_instruction()],
        signer,
        priority_fee=1,
        quote_amount_raw=10,
        fee_lamports=5_000,
        intent_id="changed-prepared-recovery",
        receipt_destinations=rebuilt_destinations,
        quote_mint=WSOL_MINT,
    )

    assert returned == prepared_signature
    assert captured == [prepared_wire]
    assert await client.get_submission_receipt_destinations(
        prepared_signature
    ) == tuple(Pubkey.from_string(item) for item in original_destinations)


@pytest.mark.asyncio
async def test_prepared_recovery_precedes_compute_budget_guard(tmp_path) -> None:
    """A caller that signed the canonical final wire (fee instructions
    embedded) passes that same list with an explicit intent_id. Recovery
    must find the prepared record BEFORE the caller-supplied Compute Budget
    guard and replay the exact stored bytes once — no rebuild, no doubled
    fee instructions, no ExecutionBlocked."""
    captured: list[bytes] = []

    async def send_raw_transaction(wire: bytes, _opts) -> object:
        captured.append(wire)
        return SimpleNamespace(value=prepared_signature)

    rpc = SimpleNamespace(send_raw_transaction=send_raw_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    swap = _instruction()
    final_instructions = [
        set_compute_unit_limit(180_000),
        set_compute_unit_price(500_000),
        swap,
    ]
    message = Message(final_instructions, signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    prepared_wire = bytes(transaction)
    prepared_signature = transaction.signatures[0]
    _record_prepared_wire(ledger, "cycle-final-wire", signer, message, transaction)

    returned = await client.build_and_send_transaction(
        final_instructions,
        signer,
        quote_amount_raw=10,
        fee_lamports=5_000,
        intent_id="cycle-final-wire",
        quote_mint=WSOL_MINT,
    )

    assert returned == prepared_signature
    assert captured == [prepared_wire]
    rec = ledger.get_active_submission_record("cycle-final-wire")
    assert rec.state == "submitted"


@pytest.mark.asyncio
async def test_prepared_wire_is_not_replayed_under_another_risk_session(
    tmp_path,
) -> None:
    """A wire reserved under one risk session may not be transmitted by a
    process authorized for another: it would count against the wrong session
    cap and stay invisible in that session's totals."""
    rpc = SimpleNamespace(send_raw_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    message = Message([_instruction()], signer.pubkey())
    transaction = Transaction([signer], message, Hash.default())
    _record_prepared_wire(
        ledger,
        "cross-session-prepared",
        signer,
        message,
        transaction,
        session_id="previous-session",
    )
    client.execution_policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=1_000_000,
        max_total_fee_lamports=100_000,
        risk_session_id="current-session",
        max_session_quote_raw=10_000_000,
        max_session_fee_lamports=10_000_000,
        allow_skip_preflight=True,
    )

    with pytest.raises(ExecutionBlocked, match="risk session"):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cross-session-prepared",
            quote_mint=WSOL_MINT,
        )

    rpc.send_raw_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_cancels_the_blockhash_updater() -> None:
    """The updater loops forever, so awaiting it instead of cancelling it hangs
    shutdown: the RPC session never closes and the ledger/journal locks stay
    held across a restart."""
    client = SolanaClient("http://offline.invalid")
    updater = asyncio.create_task(asyncio.Event().wait())
    client._blockhash_updater_task = updater

    await asyncio.wait_for(client.close(), timeout=1)

    assert updater.done()
    assert client._blockhash_updater_task is None


@pytest.mark.asyncio
async def test_expired_prepared_wire_remains_unknown_without_terminal_evidence(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(
        send_raw_transaction=AsyncMock(),
        send_transaction=AsyncMock(),
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    instruction = _instruction()
    message = Message([instruction], signer.pubkey())
    stale_blockhash = Hash.new_unique()
    stale_transaction = Transaction([signer], message, stale_blockhash)
    stale_signature = stale_transaction.signatures[0]
    ledger.record_intent(
        "stale-prepared",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(message)).hexdigest(),
    )
    ledger.record_submission(
        "stale-prepared",
        str(stale_signature),
        str(stale_blockhash),
        10,
        wire_bytes=bytes(stale_transaction),
        state="prepared",
    )

    async def height_exceeded(
        _last_valid_height: int,
        *,
        commitment: str,
    ) -> bool:
        assert commitment == "finalized"
        return True

    client._current_block_height_exceeds = height_exceeded

    with pytest.raises(_unknown_error_type()) as caught:
        await client.build_and_send_transaction(
            [instruction],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="stale-prepared",
            quote_mint=WSOL_MINT,
        )

    assert caught.value.signature == str(stale_signature)
    assert ledger.get_active_submission_record("stale-prepared") is not None
    rpc.send_raw_transaction.assert_not_awaited()
    rpc.send_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_before_send_releases_only_prepared_reservation(
    tmp_path,
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    client._rate_limiter.acquire = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cancel-before-send",
            quote_mint=WSOL_MINT,
        )

    assert ledger.get_active_submission_record("cancel-before-send") is None


@pytest.mark.asyncio
async def test_cancellation_during_send_keeps_submitted_reservation(tmp_path) -> None:
    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=asyncio.CancelledError())
    )
    client, ledger, signer = _live_client(tmp_path, rpc)

    with pytest.raises(asyncio.CancelledError):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cancel-during-send",
            quote_mint=WSOL_MINT,
        )

    record = ledger.get_active_submission_record("cancel-during-send")
    assert record is not None
    assert record.state == "submitted"


@pytest.mark.asyncio
async def test_cancellation_during_post_send_ledger_mark_waits_for_transition(
    tmp_path,
) -> None:
    async def send_transaction(transaction: Transaction, _opts) -> object:
        return SimpleNamespace(value=transaction.signatures[0])

    rpc = SimpleNamespace(send_transaction=send_transaction)
    client, ledger, signer = _live_client(tmp_path, rpc)
    mark_started = Event()
    release_mark = Event()
    original_mark = ledger.mark_submission_submitted

    def blocking_mark(signature: str) -> None:
        mark_started.set()
        release_mark.wait()
        original_mark(signature)

    ledger.mark_submission_submitted = blocking_mark
    submission = asyncio.create_task(
        client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            fee_lamports=5_000,
            intent_id="cancel-during-ledger-mark",
            quote_mint=WSOL_MINT,
        )
    )
    await asyncio.wait_for(asyncio.to_thread(mark_started.wait), timeout=1)

    submission.cancel()
    await asyncio.sleep(0)
    assert not submission.done()
    release_mark.set()

    with pytest.raises(asyncio.CancelledError):
        await submission

    record = ledger.get_active_submission_record("cancel-during-ledger-mark")
    assert record is not None
    assert record.state == "submitted"
    assert ledger.get_outcome(record.signature).status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_confirmation_does_not_promote_lower_commitment_status_to_success() -> (
    None
):
    lower_status = SimpleNamespace(
        err=None,
        slot=7,
        confirmation_status=0,
    )
    rpc = SimpleNamespace(
        confirm_transaction=AsyncMock(
            return_value=SimpleNamespace(value=[lower_status])
        )
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    async def no_transaction(_signature, *, commitment: str = "confirmed") -> None:
        return None

    async def not_expired(
        _signature: Signature,
        _last_valid_height: int,
        _requested_commitment: str,
    ) -> bool:
        return False

    client._get_transaction_result = no_transaction
    client._prove_transaction_expired = not_expired

    outcome = await client.confirm_transaction_outcome(
        Signature.default(),
        commitment="confirmed",
        timeout_seconds=1,
        last_valid_block_height=10,
    )

    assert outcome.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_unknown_poll_reuses_stored_terminal_outcome(tmp_path) -> None:
    rpc = SimpleNamespace(
        confirm_transaction=AsyncMock(return_value=SimpleNamespace(value=[]))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    transaction = Transaction(
        [signer],
        Message([_instruction()], signer.pubkey()),
        Hash.default(),
    )
    signature = str(transaction.signatures[0])
    ledger.record_intent(
        "confirmed-before-restart",
        str(signer.pubkey()),
        10,
        5_000,
        hashlib.sha256(bytes(transaction.message)).hexdigest(),
    )
    ledger.record_submission(
        "confirmed-before-restart",
        signature,
        str(Hash.default()),
        100,
        wire_bytes=bytes(transaction),
        state="submitted",
    )
    stored = TransactionOutcome(TransactionStatus.SUCCESS, signature, slot=7)
    ledger.record_outcome(stored)
    client._get_transaction_result = AsyncMock(return_value=None)
    client._prove_transaction_expired = AsyncMock(return_value=False)

    outcome = await client.confirm_transaction_outcome(
        signature,
        timeout_seconds=1,
    )

    assert outcome == stored
    assert ledger.get_outcome(signature) == stored


@pytest.mark.asyncio
async def test_processed_confirmation_is_rejected_as_nonterminal() -> None:
    client = SolanaClient("http://offline.invalid")

    with pytest.raises(ValueError, match="confirmed or finalized"):
        await client.confirm_transaction_outcome(
            Signature.default(),
            commitment="processed",
        )


@pytest.mark.asyncio
async def test_transaction_result_requires_explicit_meta_error_field() -> None:
    client = SolanaClient("http://offline.invalid")

    async def partial_response(_body, **_kwargs) -> dict:
        return {"result": {"slot": 7, "meta": {}}}

    client.post_rpc = partial_response

    assert await client._get_transaction_result(Signature.default()) is None
    assert not await client.verify_transaction_succeeded(Signature.default())


@pytest.mark.asyncio
async def test_transaction_result_treats_json_rpc_error_as_unavailable() -> None:
    client = SolanaClient("http://offline.invalid")

    async def rpc_error(_body, **_kwargs) -> dict:
        raise client_module.JsonRpcError("getTransaction", {"code": -32000})

    client.post_rpc = rpc_error

    assert await client._get_transaction_result(Signature.default()) is None


@pytest.fixture
def tracked_receipt(
    tmp_path: Path,
) -> tuple[SolanaClient, TransactionLedger, str, dict]:
    rpc = SimpleNamespace(
        confirm_transaction=AsyncMock(return_value=SimpleNamespace(value=[]))
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    signature = str(Signature.default())
    original_profile = ledger.record_evidence_profile("live", {"run": "original"}, {})
    ledger.record_intent(
        "receipt-evidence",
        str(signer.pubkey()),
        10,
        50_000,
        hashlib.sha256(b"receipt-evidence").hexdigest(),
    )
    ledger.record_submission(
        "receipt-evidence",
        signature,
        str(Hash.default()),
        100,
        evidence_profile_id=original_profile,
    )
    client.evidence_profile_id = ledger.record_evidence_profile(
        "live", {"run": "observer"}, {}
    )
    result = {
        "slot": 7,
        "transaction": {
            "signatures": [signature],
            "message": {"accountKeys": [str(signer.pubkey())]},
        },
        "meta": {"err": None, "fee": 6_123},
    }
    client.post_rpc = AsyncMock(return_value={"result": result})
    return client, ledger, signature, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, TransactionStatus.SUCCESS),
        ({"InstructionError": [0, {"Custom": 6003}]}, TransactionStatus.REVERTED),
    ],
)
async def test_tracked_receipt_preserves_actual_fee_and_deduplicates(
    tracked_receipt: tuple[SolanaClient, TransactionLedger, str, dict],
    error: dict | None,
    expected: TransactionStatus,
) -> None:
    client, ledger, signature, result = tracked_receipt
    result["meta"]["err"] = error

    for _ in range(2):
        outcome = await client.confirm_transaction_outcome(signature, timeout_seconds=1)
        assert outcome.status is expected

    rows = ledger.connection.execute(
        "SELECT signature, commitment, profile_id, observed_fee_lamports, payload_json "
        "FROM evidence_receipts"
    ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0][:4]) == (
        signature,
        "confirmed",
        client.evidence_profile_id,
        "6123",
    )
    assert json.loads(rows[0][4]) == result
    submission = ledger.get_active_submission_record("receipt-evidence")
    if expected is TransactionStatus.SUCCESS:
        assert submission.fee_lamports == 50_000
        assert submission.evidence_profile_id != client.evidence_profile_id
    client.post_rpc.assert_awaited()
    assert client.post_rpc.await_count == 2


@pytest.mark.asyncio
async def test_changed_receipt_polls_and_commitments_are_retained(
    tracked_receipt: tuple[SolanaClient, TransactionLedger, str, dict],
) -> None:
    client, ledger, signature, result = tracked_receipt
    await client._get_transaction_result(signature)
    result["blockTime"] = 1_700_000_000
    await client._get_transaction_result(signature)
    await client._get_transaction_result(signature, commitment="finalized")

    rows = ledger.connection.execute(
        "SELECT commitment, payload_json FROM evidence_receipts"
    ).fetchall()
    observations = {(row[0], json.loads(row[1]).get("blockTime")) for row in rows}
    assert observations == {
        ("confirmed", None),
        ("confirmed", 1_700_000_000),
        ("finalized", 1_700_000_000),
    }


@pytest.mark.asyncio
async def test_untracked_public_receipt_is_not_execution_evidence(
    tracked_receipt: tuple[SolanaClient, TransactionLedger, str, dict],
) -> None:
    client, ledger, _signature, result = tracked_receipt
    public_signature = str(Signature.new_unique())
    result["transaction"]["signatures"] = [public_signature]

    assert await client._get_transaction_result(public_signature) == result
    assert (
        ledger.connection.execute("SELECT COUNT(*) FROM evidence_receipts").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader",
    [
        "_get_transaction_result",
        "confirm_transaction_outcome",
        "verify_transaction_succeeded",
        "get_transaction_token_balance",
        "get_buy_transaction_details",
        "get_sell_transaction_details",
        "get_buyer_pre_token_balance",
    ],
)
async def test_receipt_persistence_failure_is_not_missing_or_success(
    tracked_receipt: tuple[SolanaClient, TransactionLedger, str, dict], reader: str
) -> None:
    client, ledger, signature, _result = tracked_receipt
    ledger.connection.execute(
        "CREATE TRIGGER reject_receipt BEFORE INSERT ON evidence_receipts "
        "BEGIN SELECT RAISE(ABORT, 'evidence unavailable'); END"
    )
    args = (
        (signature,)
        if reader
        in {
            "_get_transaction_result",
            "confirm_transaction_outcome",
            "verify_transaction_succeeded",
        }
        else (signature, Pubkey.new_unique(), Pubkey.new_unique())
    )

    with pytest.raises(EvidencePersistenceError) as failure:
        await getattr(client, reader)(*args)

    assert failure.value.__cause__ is not None
    assert client.post_rpc.await_count == 1
    assert ledger.get_outcome(signature) is None
    assert (
        ledger.connection.execute("SELECT COUNT(*) FROM evidence_receipts").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["tracking_lookup", "receipt_encoding"])
async def test_local_archive_failure_cannot_become_missing_receipt(
    tracked_receipt: tuple[SolanaClient, TransactionLedger, str, dict],
    failure_stage: str,
) -> None:
    client, ledger, signature, result = tracked_receipt
    if failure_stage == "tracking_lookup":
        ledger.close()
    else:
        result["meta"]["invalid_provider_value"] = float("nan")

    with pytest.raises(EvidencePersistenceError) as caught:
        await client.verify_transaction_succeeded(signature)
    assert caught.value.__cause__ is not None


@pytest.mark.asyncio
async def test_reservation_evidence_failure_prevents_network_send(
    tmp_path: Path,
) -> None:
    rpc = SimpleNamespace(send_transaction=AsyncMock())
    client, ledger, signer = _live_client(tmp_path, rpc)
    client.evidence_profile_id = "unregistered-profile"

    with pytest.raises(EvidencePersistenceError):
        await client.build_and_send_transaction(
            [_instruction()],
            signer,
            quote_amount_raw=10,
            quote_mint=WSOL_MINT,
            fee_lamports=5_000,
            intent_id="failed-evidence-reservation",
        )

    rpc.send_transaction.assert_not_awaited()
    assert ledger.get_active_submission_record("failed-evidence-reservation") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared_recovery", [False, True])
async def test_post_send_evidence_failure_propagates_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, prepared_recovery: bool
) -> None:
    async def send_transaction(
        transaction: Transaction, _opts: object
    ) -> SimpleNamespace:
        return SimpleNamespace(value=transaction.signatures[0])

    async def send_raw_transaction(wire: bytes, _opts: object) -> SimpleNamespace:
        return SimpleNamespace(value=Transaction.from_bytes(wire).signatures[0])

    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=send_transaction),
        send_raw_transaction=AsyncMock(side_effect=send_raw_transaction),
    )
    client, ledger, signer = _live_client(tmp_path, rpc)
    instructions = [_instruction()]
    if prepared_recovery:
        transaction = Transaction(
            [signer], Message(instructions, signer.pubkey()), Hash.default()
        )
        _record_prepared_wire(
            ledger,
            "post-send-evidence",
            signer,
            transaction.message,
            transaction,
        )
    failure = EvidencePersistenceError("evidence disk unavailable")

    def reject_transition(_signature: str) -> None:
        raise failure

    monkeypatch.setattr(ledger, "mark_submission_submitted", reject_transition)
    with pytest.raises(EvidencePersistenceError) as caught:
        if prepared_recovery:
            await client.recover_active_submission("post-send-evidence")
        else:
            await client.build_and_send_transaction(
                instructions,
                signer,
                quote_amount_raw=10,
                quote_mint=WSOL_MINT,
                fee_lamports=5_000,
                intent_id="post-send-evidence",
            )

    assert caught.value is failure
    assert rpc.send_transaction.await_count == (0 if prepared_recovery else 1)
    assert rpc.send_raw_transaction.await_count == (1 if prepared_recovery else 0)
    record = ledger.get_active_submission_record("post-send-evidence")
    assert record.state == "prepared"
    assert ledger.get_outcome(record.signature) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_active_wire_keeps_original_evidence_profile(
    tmp_path: Path, *, historical: bool
) -> None:
    async def send_transaction(
        transaction: Transaction, _opts: object
    ) -> SimpleNamespace:
        return SimpleNamespace(value=transaction.signatures[0])

    rpc = SimpleNamespace(send_transaction=AsyncMock(side_effect=send_transaction))
    client, ledger, signer = _live_client(tmp_path, rpc)
    original_profile = (
        None
        if historical
        else ledger.record_evidence_profile("live", {"run": "original"}, {})
    )
    client.evidence_profile_id = original_profile
    instructions = [_instruction()]
    signature = await client.build_and_send_transaction(
        instructions,
        signer,
        quote_amount_raw=10,
        quote_mint=WSOL_MINT,
        fee_lamports=5_000,
        intent_id="profile-bound-wire",
    )
    ledger.record_outcome(TransactionOutcome(TransactionStatus.SUCCESS, str(signature)))
    client.evidence_profile_id = ledger.record_evidence_profile(
        "live", {"run": "current"}, {}
    )

    assert (
        await client.build_and_send_transaction(
            instructions,
            signer,
            quote_amount_raw=10,
            quote_mint=WSOL_MINT,
            fee_lamports=5_000,
            intent_id="profile-bound-wire",
        )
        == signature
    )
    assert rpc.send_transaction.await_count == 1
    assert (
        ledger.get_active_submission_record("profile-bound-wire").evidence_profile_id
        == original_profile
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "presence", "height_ok", "expected"),
    [
        ((True, None), (True, False), True, True),  # full proof
        ((True, None), (True, False), False, False),  # height not past margin
        ((True, object()), (True, False), True, False),  # status exists: landed
        ((True, None), (True, True), True, False),  # tx present at finalized
        ((False, None), (True, False), True, False),  # status read failed
        ((True, None), (False, False), True, False),  # presence read failed
    ],
    ids=[
        "expired",
        "too-early",
        "has-status",
        "present",
        "status-rpc-down",
        "presence-rpc-down",
    ],
)
async def test_expiry_requires_all_three_proofs(
    status: tuple, presence: tuple, height_ok: bool, expected: bool
) -> None:
    """Live 2026-09-03: an expired sell stayed UNKNOWN for an hour; expiry must
    be provable, but only from complete evidence."""
    client = SolanaClient("http://offline.invalid")
    client._read_signature_status = AsyncMock(return_value=status)
    client._read_transaction_presence = AsyncMock(return_value=presence)
    client._current_block_height_exceeds = AsyncMock(return_value=height_ok)

    assert (
        await client._prove_transaction_expired(Signature.default(), 100, "confirmed")
        is expected
    )
    if expected or (
        height_ok is False and status == (True, None) and presence == (True, False)
    ):
        client._current_block_height_exceeds.assert_awaited_once_with(
            100 + client_module.EXPIRY_PROOF_MARGIN_BLOCKS, commitment="finalized"
        )


@pytest.mark.asyncio
async def test_null_transaction_result_is_not_found_not_malformed(caplog) -> None:
    client = SolanaClient("http://offline.invalid")
    client.post_rpc = AsyncMock(return_value={"result": None})
    with caplog.at_level("WARNING"):
        assert await client._get_transaction_result("sig") is None
    assert not any("Malformed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cached_blockhash_expiry_uses_finalized_height() -> None:
    rpc = SimpleNamespace(
        get_block_height=AsyncMock(return_value=SimpleNamespace(value=11))
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    assert await client._blockhash_is_expired(
        client_module._BlockhashContext(Hash.default(), 10)
    )
    rpc.get_block_height.assert_awaited_once_with(commitment="finalized")


@pytest.mark.asyncio
async def test_read_rpc_calls_retry_transport_failures_within_bound() -> None:
    rpc = SimpleNamespace(
        get_account_info=AsyncMock(
            side_effect=[
                aiohttp.ClientConnectionError("temporary"),
                SimpleNamespace(value={"data": "ok"}),
            ]
        )
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    result = await client.get_account_info(Pubkey.new_unique())

    assert result == {"data": "ok"}
    assert rpc.get_account_info.await_count == 2


@pytest.mark.asyncio
async def test_read_rpc_raises_typed_unavailable_after_transport_exhaustion() -> None:
    rpc = SimpleNamespace(
        get_account_info=AsyncMock(
            side_effect=aiohttp.ClientConnectionError("dns down")
        )
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    with pytest.raises(RpcUnavailableError) as raised:
        await client.get_account_info(Pubkey.new_unique())

    assert isinstance(raised.value.__cause__, aiohttp.ClientConnectionError)
    assert rpc.get_account_info.await_count == 3


@pytest.mark.asyncio
async def test_read_rpc_does_not_retry_or_retype_data_errors() -> None:
    rpc = SimpleNamespace(get_account_info=AsyncMock(side_effect=ValueError("bad")))
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    with pytest.raises(ValueError, match="bad"):
        await client.get_account_info(Pubkey.new_unique())

    assert rpc.get_account_info.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_read_rpc_reraises_permanent_http_status_unchanged(status: int) -> None:
    request = httpx.Request("POST", "http://offline.invalid")
    error = httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status)
    )

    async def wrapped_like_solana_py(*args: object, **kwargs: object) -> None:
        # solana-py's handle_async_exceptions does `raise Wrapped(exc) from exc`.
        try:
            raise error
        except httpx.HTTPStatusError as exc:
            raise SolanaRpcException(exc, lambda v: v, None, object()) from exc

    rpc = SimpleNamespace(
        get_account_info=AsyncMock(side_effect=wrapped_like_solana_py)
    )
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    with pytest.raises(SolanaRpcException):
        await client.get_account_info(Pubkey.new_unique())

    assert rpc.get_account_info.await_count == 1


@pytest.mark.asyncio
async def test_read_rpc_retries_retryable_http_status_as_transient() -> None:
    request = httpx.Request("POST", "http://offline.invalid")
    error = httpx.HTTPStatusError(
        "HTTP 503", request=request, response=httpx.Response(503)
    )
    rpc = SimpleNamespace(get_account_info=AsyncMock(side_effect=error))
    client = SolanaClient("http://offline.invalid")
    client._client = rpc

    with pytest.raises(RpcUnavailableError):
        await client.get_account_info(Pubkey.new_unique())

    assert rpc.get_account_info.await_count == 3


def _post_rpc_client(status: int, *, json_error: bool = False) -> tuple:
    """Client whose session always returns one canned HTTP status."""
    calls: list[int] = []

    class _Response:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.status = status

        def raise_for_status(self) -> None:
            if self.status >= 400:
                raise aiohttp.ClientResponseError(
                    SimpleNamespace(real_url="http://offline.invalid"),
                    (),
                    status=self.status,
                )

        async def json(self) -> dict[str, Any]:
            if json_error:
                raise aiohttp.ContentTypeError(
                    SimpleNamespace(real_url="http://offline.invalid"), ()
                )
            return {"result": "ok"}

    class _Post:
        async def __aenter__(self) -> _Response:
            calls.append(status)
            return _Response()

        async def __aexit__(self, *args: object) -> None:
            return None

    client = SolanaClient("http://offline.invalid")
    client._session = SimpleNamespace(
        post=lambda *args, **kwargs: _Post(), closed=False
    )
    return client, calls


@pytest.mark.asyncio
async def test_post_rpc_non_json_body_is_a_data_error_not_an_outage() -> None:
    client, _ = _post_rpc_client(200, json_error=True)

    with pytest.raises(JsonRpcError, match="not JSON"):
        await client.post_rpc({"jsonrpc": "2.0", "id": 1, "method": "getHealth"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_post_rpc_permanent_http_status_fails_fast(status: int) -> None:
    client, calls = _post_rpc_client(status)

    with pytest.raises(JsonRpcError, match=f"HTTP {status}"):
        await client.post_rpc({"jsonrpc": "2.0", "id": 1, "method": "getHealth"})

    assert calls == [status]


@pytest.mark.asyncio
async def test_post_rpc_server_error_retries_then_reports_no_response(
    monkeypatch,
) -> None:
    client, calls = _post_rpc_client(503)
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    result = await client.post_rpc({"jsonrpc": "2.0", "id": 1, "method": "getHealth"})

    assert result is None
    assert calls == [503, 503, 503]


@pytest.mark.asyncio
async def test_buy_receipt_attributes_token_and_quote_deltas_to_trade_accounts() -> (
    None
):
    client = SolanaClient("http://offline.invalid")
    buyer = Pubkey.new_unique()
    buyer_token = Pubkey.new_unique()
    buyer_token_two = Pubkey.new_unique()
    buyer_quote = Pubkey.new_unique()
    quote_vault = Pubkey.new_unique()
    unrelated = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    quote_mint = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 2,
                    "mint": str(mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 3,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 4,
                    "mint": str(quote_mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "0"},
                },
                {
                    "accountIndex": 5,
                    "mint": str(quote_mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "100"},
                },
                {
                    "accountIndex": 6,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "0"},
                },
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "25"},
                },
                {
                    "accountIndex": 2,
                    "mint": str(mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "999"},
                },
                {
                    "accountIndex": 3,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "40"},
                },
                {
                    "accountIndex": 4,
                    "mint": str(quote_mint),
                    "owner": str(unrelated),
                    "uiTokenAmount": {"amount": "888"},
                },
                {
                    "accountIndex": 5,
                    "mint": str(quote_mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "60"},
                },
                {
                    "accountIndex": 6,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "5"},
                },
            ],
            "preBalances": [10_000, 100, 100, 100, 100, 100, 100],
            "postBalances": [9_995, 100, 100, 100, 100, 100, 100],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    str(buyer),
                    str(buyer_token),
                    str(unrelated),
                    str(quote_vault),
                    str(Pubkey.new_unique()),
                    str(buyer_quote),
                    str(buyer_token_two),
                ]
            }
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    tokens_received, quote_spent = await client.get_buy_transaction_details(
        Signature.default(),
        mint,
        quote_vault,
        quote_mint,
    )

    assert tokens_received == 30
    assert quote_spent == 40


def test_token_delta_attribution_rejects_duplicate_account_indexes() -> None:
    owner = str(Pubkey.new_unique())
    mint = str(Pubkey.new_unique())
    meta = {
        "preBalances": [10_000, 0],
        "postBalances": [9_895, 100],
        "preTokenBalances": [],
        "postTokenBalances": [
            {
                "accountIndex": 1,
                "mint": mint,
                "owner": owner,
                "uiTokenAmount": {"amount": "10"},
            },
            {
                "accountIndex": 1,
                "mint": mint,
                "owner": owner,
                "uiTokenAmount": {"amount": "20"},
            },
        ],
    }

    assert (
        SolanaClient._extract_positive_token_diff(
            meta,
            mint,
            owner=owner,
            account_count=2,
        )
        is None
    )


@pytest.mark.asyncio
async def test_quote_receipt_rejects_duplicate_destination_account_keys() -> None:
    client = SolanaClient("http://offline.invalid")
    buyer = Pubkey.new_unique()
    quote_vault = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    quote_mint = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "0"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(quote_vault),
                    "uiTokenAmount": {"amount": "40"},
                }
            ],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    str(buyer),
                    str(quote_vault),
                    str(quote_vault),
                ]
            }
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result
    _tokens_received, quote_spent = await client.get_buy_transaction_details(
        Signature.default(),
        mint,
        quote_vault,
        quote_mint,
    )

    assert quote_spent is None


@pytest.mark.asyncio
async def test_buy_sol_receipt_sums_primary_and_fee_destinations() -> None:
    client = SolanaClient("http://offline.invalid")
    buyer = Pubkey.new_unique()
    buyer_token = Pubkey.new_unique()
    primary_destination = Pubkey.new_unique()
    fee_destination = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "0"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(mint),
                    "owner": str(buyer),
                    "uiTokenAmount": {"amount": "5"},
                }
            ],
            "preBalances": [10_000, 100, 200, 300],
            "postBalances": [9_000, 100, 270, 325],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    str(buyer),
                    str(buyer_token),
                    str(primary_destination),
                    str(fee_destination),
                ]
            }
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    tokens_received, quote_spent = await client.get_buy_transaction_details(
        Signature.default(),
        mint,
        primary_destination,
        WSOL_MINT,
        quote_destinations=[fee_destination],
    )

    assert tokens_received == 5
    assert quote_spent == 95


@pytest.mark.asyncio
async def test_sell_sol_receipt_adds_transaction_fee_to_owner_delta() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "fee": 5,
            "preBalances": [10_000, 200],
            "postBalances": [10_100, 195],
            "preTokenBalances": [],
            "postTokenBalances": [],
        },
        "transaction": {
            "message": {"accountKeys": [str(owner), str(Pubkey.new_unique())]}
        },
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_sell_transaction_details(Signature.default(), WSOL_MINT, owner)
        == 105
    )


@pytest.mark.asyncio
async def test_sell_wsol_receipt_excludes_preexisting_closed_ata_lamports() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    quote_account = get_associated_token_address(owner, WSOL_MINT)
    result = {
        "meta": {
            "err": None,
            "fee": 5,
            "preBalances": [10_000, 200],
            "postBalances": [10_300, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(WSOL_MINT),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "50"},
                }
            ],
            "postTokenBalances": [],
        },
        "transaction": {"message": {"accountKeys": [str(owner), str(quote_account)]}},
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_sell_transaction_details(Signature.default(), WSOL_MINT, owner)
        == 105
    )


@pytest.mark.asyncio
async def test_sell_wsol_receipt_prefers_owner_token_delta() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    quote_account = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "fee": 5,
            "preBalances": [10_000, 200],
            "postBalances": [9_995, 195],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(WSOL_MINT),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "50"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(WSOL_MINT),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "75"},
                }
            ],
        },
        "transaction": {"message": {"accountKeys": [str(owner), str(quote_account)]}},
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_sell_transaction_details(Signature.default(), WSOL_MINT, owner)
        == 25
    )


@pytest.mark.asyncio
async def test_sell_spl_quote_receipt_uses_owner_token_delta() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    quote_mint = Pubkey.new_unique()
    quote_account = Pubkey.new_unique()
    result = {
        "meta": {
            "err": None,
            "preBalances": [10_000, 100],
            "postBalances": [9_995, 100],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "50"},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(quote_mint),
                    "owner": str(owner),
                    "uiTokenAmount": {"amount": "75"},
                }
            ],
        },
        "transaction": {"message": {"accountKeys": [str(owner), str(quote_account)]}},
    }

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_sell_transaction_details(
            Signature.default(), quote_mint, owner
        )
        == 25
    )


def test_receipt_amount_parsing_rejects_noncanonical_numeric_values() -> None:
    owner = str(Pubkey.new_unique())
    mint = str(Pubkey.new_unique())
    malformed = {
        "preBalances": [0],
        "postBalances": [100],
        "preTokenBalances": [],
        "postTokenBalances": [
            {
                "accountIndex": 0,
                "mint": mint,
                "owner": owner,
                "uiTokenAmount": {"amount": 10},
            }
        ],
    }

    assert (
        SolanaClient._extract_positive_token_diff(
            malformed,
            mint,
            owner=owner,
            account_count=1,
        )
        is None
    )
    assert (
        SolanaClient._validated_lamport_balances(
            {"preBalances": [0], "postBalances": [True]}, 1
        )
        is None
    )


@pytest.mark.asyncio
async def test_transaction_token_balance_rejects_malformed_amount() -> None:
    client = SolanaClient("http://offline.invalid")
    owner = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    result = {
        "slot": 1,
        "meta": {"err": None, "postTokenBalances": []},
        "transaction": {
            "signatures": [str(Signature.default())],
            "message": {"accountKeys": [str(owner)]},
        },
    }
    result["meta"]["postTokenBalances"] = [
        {
            "accountIndex": 0,
            "mint": str(mint),
            "owner": str(owner),
            "uiTokenAmount": {"amount": 10},
        }
    ]

    async def get_result(_signature, *, commitment: str = "confirmed") -> dict:
        return result

    client._get_transaction_result = get_result

    assert (
        await client.get_transaction_token_balance(Signature.default(), owner, mint)
        is None
    )


def test_canonical_transaction_requires_requested_primary_signature() -> None:
    primary = Keypair().sign_message(b"primary")
    secondary = Keypair().sign_message(b"secondary")
    result = {
        "slot": 1,
        "meta": {"err": None},
        "transaction": {
            "signatures": [str(primary), str(secondary)],
            "message": {"accountKeys": [str(Pubkey.new_unique())]},
        },
    }

    assert SolanaClient._is_canonical_transaction_result(result, str(primary))
    assert not SolanaClient._is_canonical_transaction_result(result, str(secondary))

    result["transaction"]["signatures"][1] = "not-a-signature"
    assert not SolanaClient._is_canonical_transaction_result(result, str(primary))


@pytest.mark.asyncio
async def test_session_budget_blocks_second_wire_before_network_send(tmp_path) -> None:
    async def send_transaction(
        transaction: Transaction,
        _opts: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(value=transaction.signatures[0])

    rpc = SimpleNamespace(
        send_transaction=AsyncMock(side_effect=send_transaction),
    )
    client, _ledger, signer = _live_client(tmp_path, rpc)
    client.execution_policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer.pubkey()),
        max_trade_quote_raw=1_000_000,
        max_total_fee_lamports=100_000,
        risk_session_id="bounded-session",
        max_session_quote_raw=1_000_000,
        max_session_fee_lamports=100_000,
        allow_skip_preflight=True,
    )

    await client.build_and_send_transaction(
        [_instruction()],
        signer,
        quote_amount_raw=600_000,
        fee_lamports=5_000,
        intent_id="session-buy-1",
        quote_mint=WSOL_MINT,
    )

    with pytest.raises(TradeLimitExceeded, match="session quote"):
        await client.build_and_send_transaction(
            [Instruction(Pubkey.new_unique(), b"trade-2", [])],
            signer,
            quote_amount_raw=600_000,
            fee_lamports=5_000,
            intent_id="session-buy-2",
            quote_mint=WSOL_MINT,
        )

    assert rpc.send_transaction.await_count == 1


@pytest.mark.asyncio
async def test_native_balance_and_rent_reads_validate_rpc_shape() -> None:
    client = object.__new__(SolanaClient)
    client.post_rpc = AsyncMock(
        side_effect=[
            {"result": {"context": {"slot": 1}, "value": 123_456}},
            {"result": 3_000_000},
        ]
    )
    wallet = Pubkey.new_unique()

    assert await client.get_native_balance(wallet) == 123_456
    assert await client.get_minimum_balance_for_rent_exemption(512) == 3_000_000
    assert client.post_rpc.await_args_list[0].args[0]["method"] == "getBalance"
    assert (
        client.post_rpc.await_args_list[1].args[0]["method"]
        == "getMinimumBalanceForRentExemption"
    )


@pytest.mark.asyncio
async def test_native_balance_rejects_malformed_rpc_value() -> None:
    client = object.__new__(SolanaClient)
    client.post_rpc = AsyncMock(return_value={"result": {"value": True}})

    with pytest.raises(ValueError, match="getBalance"):
        await client.get_native_balance(Pubkey.new_unique())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_baseline", "expected_acquired"),
    [
        ("new", 0, 110),
        ("existing", 100, 10),
        ("missing_pre_list", None, None),
        ("missing_post_list", None, None),
        ("missing_owner", None, None),
        ("unknown_owner_with_known_acquisition", None, None),
        ("owner_transfer", None, None),
        ("mint_changed", None, None),
        ("funded_missing_pre", None, None),
        ("funded_missing_post", None, None),
        ("unseen_inventory", None, None),
        ("duplicate_index", None, None),
        ("invalid_index", None, None),
        ("invalid_amount", None, None),
        ("invalid_lamports", None, None),
        ("missing_receipt", None, None),
    ],
)
async def test_buyer_pre_token_balance_from_receipt(  # noqa: C901, PLR0912
    case: str, expected_baseline: int | None, expected_acquired: int | None
) -> None:
    client = object.__new__(SolanaClient)
    mint, owner, other = Pubkey.new_unique(), Pubkey.new_unique(), Pubkey.new_unique()
    venue = Pubkey.new_unique()
    pre = {
        "accountIndex": 1,
        "mint": str(mint),
        "owner": str(owner),
        "uiTokenAmount": {"amount": "100"},
    }
    post = {**pre, "uiTokenAmount": {"amount": "110"}}
    meta = {
        "err": None,
        "preBalances": [10_000, 100, 100, 100],
        "postBalances": [9_985, 100, 100, 110],
        "preTokenBalances": [pre],
        "postTokenBalances": [post],
    }
    receipt = {
        "transaction": {
            "message": {
                "accountKeys": [
                    str(owner),
                    str(Pubkey.new_unique()),
                    str(Pubkey.new_unique()),
                    str(venue),
                ]
            }
        },
        "meta": meta,
    }
    if case in {"new", "invalid_lamports"}:
        meta["preTokenBalances"] = []
        meta["preBalances"][1] = 0 if case == "new" else False
    elif case == "missing_pre_list":
        del meta["preTokenBalances"]
    elif case == "missing_post_list":
        del meta["postTokenBalances"]
    elif case == "missing_owner":
        del pre["owner"]
    elif case == "unknown_owner_with_known_acquisition":
        meta["preTokenBalances"].append({**pre, "accountIndex": 2, "owner": None})
        meta["postTokenBalances"].append({**post, "accountIndex": 2, "owner": None})
    elif case == "owner_transfer":
        pre["owner"] = str(other)
    elif case == "mint_changed":
        pre["mint"] = str(other)
    elif case == "funded_missing_pre":
        meta["preTokenBalances"] = []
    elif case == "funded_missing_post":
        meta["postTokenBalances"] = []
    elif case == "unseen_inventory":
        meta["preTokenBalances"] = []
        meta["postTokenBalances"] = []
    elif case == "duplicate_index":
        meta["preTokenBalances"].append(dict(pre))
    elif case == "invalid_index":
        pre["accountIndex"] = 4
    elif case == "invalid_amount":
        pre["uiTokenAmount"]["amount"] = 100
    client._get_transaction_result = AsyncMock(
        return_value=None if case == "missing_receipt" else receipt
    )

    assert (
        await client.get_buyer_pre_token_balance("s", mint, owner) == expected_baseline
    )
    acquired, quote = await client.get_buy_transaction_details("s", mint, venue)
    assert acquired == expected_acquired
    assert quote == (None if case in {"invalid_lamports", "missing_receipt"} else 10)


def test_token_account_creation_and_closure_preserve_delta_direction() -> None:
    owner, mint = str(Pubkey.new_unique()), str(Pubkey.new_unique())
    endpoint = {
        "accountIndex": 1,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": "25"},
    }
    creation = {
        "preBalances": [10_000, 0],
        "postBalances": [9_895, 100],
        "preTokenBalances": [],
        "postTokenBalances": [endpoint],
    }
    closure = {
        "preBalances": [10_000, 100],
        "postBalances": [10_095, 0],
        "preTokenBalances": [endpoint],
        "postTokenBalances": [],
    }
    assert (
        SolanaClient._extract_positive_token_diff(
            creation, mint, owner=owner, account_count=2
        )
        == 25
    )
    assert (
        SolanaClient._extract_negative_token_diff(
            creation, mint, owner=owner, account_count=2
        )
        is None
    )
    assert (
        SolanaClient._extract_negative_token_diff(
            closure, mint, owner=owner, account_count=2
        )
        == 25
    )
    assert (
        SolanaClient._extract_positive_token_diff(
            closure, mint, owner=owner, account_count=2
        )
        is None
    )
    closure["postBalances"][1] = 100
    assert (
        SolanaClient._extract_negative_token_diff(
            closure, mint, owner=owner, account_count=2
        )
        is None
    )
