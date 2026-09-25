"""Signing/submission stage of the six-venue cycle pipeline (phase 3).

Protocol (why the module looks like this):

The caller (phase 4 wiring) builds cycle swap instructions from
``core/cycles/pool.py`` quotes and the Atomic swap programs, signs them into a
legacy ``solders`` ``Transaction`` against the client's cached blockhash, and
hands the signed wire to :meth:`CycleExecutor.execute`.  The executor never
signs: it consumes an already-signed wire so the exact bytes pushed over TPU
QUIC are the exact bytes the ledger can later replay.

Submission order:

1. Policy gate — :meth:`ExecutionPolicy.require_submission` must pass before
   anything else; a ``DRY_RUN`` or unauthorized-live policy refuses here.
2. Wire attestation — the signed transaction must carry the caller's signer,
   the caller's instruction list (no Compute Budget instructions), and the
   client's cached blockhash.  Anything else cannot be tracked for expiry or
   rebuilt deterministically, so it is refused before the ledger is touched.
3. Ledger reservation — intent + prepared submission bound to the exact wire
   with ``quote_mint`` = native SOL and the risk-session envelope from the
   policy.  A conflicting active submission for the same intent aborts the
   cycle (never double-submits).
4. Fast path — wire bytes are pushed to the current slot leaders' ``tpuQuic``
   ports via :meth:`TpuSubmitter.send_quic` (fire-and-forget; validators
   deduplicate by signature, so racing is harmless).
5. Fallback — when no leader accepted the QUIC stream, the same intent is
   handed to :meth:`SolanaClient.build_and_send_transaction`.  Because the
   intent is already reserved with the exact wire, the client's prepared-replay
   recovery resubmits those same bytes over the rate-limited RPC path instead
   of building a second transaction.
6. Confirmation and evidence — the outcome is confirmed through the client's
   existing machinery (no revert/unknown conflation, expiry proof, durable
   outcome recording) and the signer's realized SOL delta is read from the
   transaction receipt.

Everything here is offline-testable: :func:`self_check` asserts the result
shape, the policy gate, and the TPU wire framing without network or real keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from core.client import (
    COMPUTE_BUDGET_PROGRAM_ID,
    SolanaClient,
    TransactionSubmissionUnknown,
    estimate_transaction_fee_lamports,
)
from core.cycles.core import SOL
from core.execution_policy import (
    ExecutionBlocked,
    ExecutionPolicy,
    TradeLimitExceeded,
)
from core.transaction_state import TransactionStatus

if TYPE_CHECKING:
    from core.tpu import TpuSubmitter
    from core.transaction_ledger import TransactionLedger

logger = logging.getLogger(__name__)

SOL_PUBKEY = Pubkey.from_string(SOL)
_WIRE_LENGTH_HEADER_BYTES = 4


class CycleSubmissionError(RuntimeError):
    """Raised when a cycle wire cannot be submitted without ambiguity."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Terminal summary of one cycle submission attempt.

    ``net_lamports`` is the signer wallet's realized SOL delta read from the
    transaction receipt (includes fees, rent, and cycle PnL); ``None`` while
    the outcome is unknown, expired, or the receipt is unavailable.
    """

    signature: str
    status: TransactionStatus
    net_lamports: int | None
    slot: int | None


def frame_wire(wire: bytes) -> bytes:
    """TPU stream framing: 4-byte little-endian length header + raw tx bytes.

    Mirrors the wire protocol documented in ``core/tpu.py``; kept as an
    executable specification so ``self_check`` pins it.
    """
    return len(wire).to_bytes(_WIRE_LENGTH_HEADER_BYTES, "little") + wire


def derive_cycle_intent_id(
    instructions: list[Instruction],
    signer: Pubkey,
    quote_amount_raw: int,
    fee_lamports: int,
) -> str:
    """Deterministic intent id, derived exactly like the client's scheme."""
    digest = hashlib.sha256()
    digest.update(bytes(Message(list(instructions), signer)))
    digest.update(bytes(signer))
    digest.update(str(quote_amount_raw).encode())
    digest.update(str(fee_lamports).encode())
    return digest.hexdigest()


class CycleExecutor:
    """Submit already-signed cycle wires TPU-first with the RPC client as fallback."""

    def __init__(
        self,
        client: SolanaClient | None,
        ledger: TransactionLedger | None,
        policy: ExecutionPolicy,
        *,
        tpu: TpuSubmitter | None = None,
    ) -> None:
        """``client``/``ledger``/``tpu`` may be ``None`` only for offline checks."""
        self._client = client
        self._ledger = ledger
        self._policy = policy
        self._tpu = tpu if tpu is not None else getattr(client, "_tpu", None)

    def _authorize(
        self, signer: Pubkey, quote_amount_raw: int, fee_lamports: int
    ) -> None:
        """Single policy choke point: nothing may sign or submit without it."""
        self._policy.require_submission()
        self._policy.validate_wallet(signer)
        self._policy.validate_budgets(quote_amount_raw, fee_lamports)

    def _validate_args(
        self,
        signer_keypair: Keypair,
        instructions: list[Instruction],
        quote_amount_raw: int,
        fee_lamports: int,
    ) -> None:
        """Reject malformed cycle submissions before any durable state exists."""
        if self._client is None or self._ledger is None:
            raise ExecutionBlocked(  # noqa: TRY003
                "Cycle submission requires a SolanaClient and a durable ledger"
            )
        if not isinstance(signer_keypair, Keypair):
            raise TypeError("signer_keypair must be a solders Keypair")  # noqa: TRY003
        if not isinstance(instructions, list) or not instructions:
            raise ValueError("instructions must be a non-empty list")  # noqa: TRY003
        for name, value in (
            ("quote_amount_raw", quote_amount_raw),
            ("fee_lamports", fee_lamports),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")  # noqa: TRY003

    def _attest_wire(
        self,
        transaction: Transaction,
        signer: Pubkey,
        instructions: list[Instruction],
    ) -> tuple[str, bytes, str]:
        """Verify the signed wire matches the declared cycle; return signature/wire/hash.

        The message hash is derived exactly like the client's
        ``_derive_intent_id`` (blockhash-less message bytes), so both
        submission channels bind the same ledger intent.
        """
        if not isinstance(transaction, Transaction):
            raise TypeError("transaction must be a signed solders Transaction")  # noqa: TRY003
        if transaction.signatures[0] == Signature.default():
            raise CycleSubmissionError("transaction wire is not signed")  # noqa: TRY003
        if transaction.message.account_keys[0] != signer:
            raise CycleSubmissionError(  # noqa: TRY003
                "signer keypair does not match the wire's fee payer"
            )
        for instruction in instructions:
            if instruction.program_id == COMPUTE_BUDGET_PROGRAM_ID:
                raise CycleSubmissionError(  # noqa: TRY003
                    "Compute Budget instructions belong to fee arguments, "
                    "not cycle swap instructions"
                )
        rebuilt = Message.new_with_blockhash(
            list(instructions), signer, transaction.message.recent_blockhash
        )
        if bytes(rebuilt) != bytes(transaction.message):
            raise CycleSubmissionError(  # noqa: TRY003
                "signed wire does not match the declared cycle instructions; "
                "the RPC fallback could not rebuild it"
            )
        message_hash = hashlib.sha256(
            bytes(Message(list(instructions), signer))
        ).hexdigest()
        return str(transaction.signatures[0]), bytes(transaction), message_hash

    async def execute(  # noqa: PLR0913
        self,
        transaction: Transaction,
        *,
        signer_keypair: Keypair,
        instructions: list[Instruction],
        quote_amount_raw: int,
        fee_lamports: int,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        intent_id: str | None = None,
        receipt_destinations: tuple[str, ...] | None = None,
        skip_preflight: bool | None = None,
        confirmation_timeout_seconds: float = 45.0,
    ) -> CycleResult:
        """Submit a signed cycle wire TPU-first, then confirm and account for it.

        ``instructions`` must be the exact swap instruction list inside
        ``transaction`` (signed by the caller over the client's cached
        blockhash); they are re-derived by the RPC fallback, so the caller must
        not embed Compute Budget instructions — pass ``priority_fee`` /
        ``compute_unit_limit`` instead.  ``fee_lamports`` is bumped to the
        client's fee floor so the ledger intent stays byte-stable across both
        channels.
        """
        assert self._client is not None and self._ledger is not None  # noqa: S101
        self._validate_args(
            signer_keypair, instructions, quote_amount_raw, fee_lamports
        )

        # The RPC fallback re-derives the fee floor from the same fee
        # arguments; bumping here keeps the ledger intent byte-stable across
        # both channels.
        fee_lamports = max(
            fee_lamports,
            estimate_transaction_fee_lamports(priority_fee, compute_unit_limit),
        )
        signer = signer_keypair.pubkey()
        self._authorize(signer, quote_amount_raw, fee_lamports)
        signature, wire, message_hash = self._attest_wire(
            transaction, signer, instructions
        )

        context = await self._client._get_cached_blockhash_context()  # noqa: SLF001
        if str(transaction.message.recent_blockhash) != str(context.blockhash):
            raise CycleSubmissionError(  # noqa: TRY003
                "cycle wire was signed with a foreign or stale blockhash; "
                "rebuild against the client's cached blockhash"
            )
        if intent_id is None:
            intent_id = derive_cycle_intent_id(
                instructions, signer, quote_amount_raw, fee_lamports
            )

        # Intent first: record_submission's JOIN/FK requires the intent row.
        await asyncio.to_thread(
            self._ledger.record_intent,
            intent_id,
            str(signer),
            quote_amount_raw,
            fee_lamports,
            message_hash,
        )
        reserved = await asyncio.to_thread(
            self._ledger.record_submission,
            intent_id,
            signature,
            str(context.blockhash),
            context.last_valid_block_height,
            wire_bytes=wire,
            state="prepared",
            receipt_destinations=receipt_destinations,
            quote_mint=str(SOL_PUBKEY),
            risk_session_id=self._policy.risk_session_id,
            max_session_quote_raw=self._policy.max_session_quote_raw,
            max_session_fee_lamports=self._policy.max_session_fee_lamports,
            intent_message_hash=message_hash,
        )
        if reserved != signature:
            raise CycleSubmissionError(  # noqa: TRY003
                f"ledger bound intent {intent_id!r} to a different submission "
                f"({reserved}); refusing to submit a second wire"
            )

        delivered = 0
        if self._tpu is not None:
            self._tpu.start()
            delivered = await self._tpu.send_quic(wire)
            if delivered:
                logger.info(f"cycle wire pushed over TPU QUIC to {delivered} leader(s)")
        if delivered:
            await asyncio.to_thread(self._ledger.mark_submission_submitted, signature)
        else:
            signature = str(
                await self._fallback_rpc(
                    signer_keypair,
                    instructions,
                    quote_amount_raw,
                    fee_lamports,
                    priority_fee=priority_fee,
                    compute_unit_limit=compute_unit_limit,
                    intent_id=intent_id,
                    receipt_destinations=receipt_destinations,
                    skip_preflight=skip_preflight,
                )
            )

        outcome = await self._client.confirm_transaction_outcome(
            signature,
            last_valid_block_height=context.last_valid_block_height,
            timeout_seconds=confirmation_timeout_seconds,
        )
        net_lamports: int | None = None
        if outcome.status in (TransactionStatus.SUCCESS, TransactionStatus.REVERTED):
            receipt = await self._client._get_transaction_result(signature)  # noqa: SLF001
            net_lamports = _signer_sol_delta(receipt, str(signer))
        return CycleResult(
            signature=signature,
            status=outcome.status,
            net_lamports=net_lamports,
            slot=outcome.slot,
        )

    async def _fallback_rpc(  # noqa: PLR0913 - mirrors the client submission signature
        self,
        signer_keypair: Keypair,
        instructions: list[Instruction],
        quote_amount_raw: int,
        fee_lamports: int,
        *,
        priority_fee: int | None,
        compute_unit_limit: int | None,
        intent_id: str,
        receipt_destinations: tuple[str, ...] | None,
        skip_preflight: bool | None,
    ) -> str:
        """Rate-limited RPC fallback through the client's standard pipeline.

        The intent is already reserved with the exact wire, so this path
        resubmits the reserved bytes (or, on ``TransactionSubmissionUnknown``,
        the ledger already holds the ambiguous outcome).
        """
        try:
            signature = await self._client.build_and_send_transaction(
                instructions,
                signer_keypair,
                skip_preflight=skip_preflight,
                max_retries=1,
                priority_fee=priority_fee,
                compute_unit_limit=compute_unit_limit,
                quote_amount_raw=quote_amount_raw,
                quote_mint=SOL_PUBKEY,
                fee_lamports=fee_lamports,
                intent_id=intent_id,
                receipt_destinations=receipt_destinations,
            )
        except TransactionSubmissionUnknown as exc:
            logger.warning(f"cycle RPC send ambiguous for {exc.signature}: {exc.error}")
            return exc.signature
        return str(signature)

    async def submit_wire(self, wire: bytes) -> int:
        """Fire-and-forget one wire through TPU QUIC; returns leaders reached."""
        if self._tpu is None:
            raise ExecutionBlocked("no TpuSubmitter is available for QUIC submission")  # noqa: TRY003
        self._tpu.start()
        return await self._tpu.send_quic(wire)


def _signer_sol_delta(receipt: dict[str, Any] | None, signer: str) -> int | None:
    """Signer wallet SOL delta from a jsonParsed transaction receipt."""
    if not isinstance(receipt, dict):
        return None
    meta = receipt.get("meta")
    if not isinstance(meta, dict):
        return None
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(post):
        return None
    message = receipt.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys")
    index = 0
    if isinstance(account_keys, list):
        for position, entry in enumerate(account_keys):
            pubkey = entry.get("pubkey") if isinstance(entry, dict) else entry
            if pubkey == signer:
                index = position
                break
    if index >= len(pre):
        return None
    try:
        return int(post[index]) - int(pre[index])
    except (TypeError, ValueError):
        return None


def self_check() -> None:
    """Offline protocol assertions: no network, no ledger files, no real keys."""
    # CycleResult shape: frozen, four fields in contract order.
    assert [field.name for field in fields(CycleResult)] == [  # noqa: S101
        "signature",
        "status",
        "net_lamports",
        "slot",
    ]
    result = CycleResult("sig", TransactionStatus.UNKNOWN, None, None)
    try:
        result.signature = "mutated"  # type: ignore[misc]
    except Exception:  # noqa: BLE001, S110 - immutability is the assertion
        pass
    else:
        raise AssertionError("CycleResult must be frozen")  # noqa: TRY003

    # Wire framing: 4-byte LE length header + tx bytes.
    assert frame_wire(b"") == b"\x00\x00\x00\x00"  # noqa: S101
    assert frame_wire(b"ab") == b"\x02\x00\x00\x00ab"  # noqa: S101
    wire = bytes(Keypair().pubkey()) * 2
    framed = frame_wire(wire)
    assert framed[:4] == len(wire).to_bytes(4, "little") and framed[4:] == wire  # noqa: S101

    # Policy gate refuses DRY_RUN and unauthorized LIVE before anything runs.
    signer = Keypair().pubkey()
    live_unauthorized = ExecutionPolicy(
        mode="live",
        expected_wallet=str(signer),
        max_trade_quote_raw=1_000,
        max_total_fee_lamports=10_000,
        risk_session_id="self-check",
        max_session_quote_raw=10_000,
        max_session_fee_lamports=100_000,
    )
    for policy in (ExecutionPolicy(), live_unauthorized):
        executor = CycleExecutor(None, None, policy)
        try:
            executor._authorize(signer, 1_000, 5_000)  # noqa: SLF001
        except ExecutionBlocked:
            pass
        else:
            raise AssertionError("policy gate must refuse unauthorized execution")  # noqa: TRY003

    # Budget gate still applies once authorized.
    live_policy = ExecutionPolicy(
        mode="live",
        live_authorized=True,
        expected_wallet=str(signer),
        max_trade_quote_raw=1_000,
        max_total_fee_lamports=10_000,
        risk_session_id="self-check",
        max_session_quote_raw=10_000,
        max_session_fee_lamports=100_000,
    )
    authorized = CycleExecutor(None, None, live_policy)
    authorized._authorize(signer, 500, 5_000)  # noqa: SLF001
    try:
        authorized._authorize(signer, 5_000, 5_000)  # noqa: SLF001
    except TradeLimitExceeded:
        pass
    else:
        raise AssertionError("budget gate must refuse oversized cycles")  # noqa: TRY003

    # Signed-wire roundtrip: legacy serialization is stable and frames cleanly.
    payer = Keypair()
    instruction = Instruction(Pubkey.from_string(SOL), b"", [])
    transaction = Transaction(
        [payer], Message([instruction], payer.pubkey()), Hash.default()
    )
    replayed = Transaction.from_bytes(bytes(transaction))
    assert str(replayed.signatures[0]) == str(transaction.signatures[0])  # noqa: S101
    assert replayed.message.account_keys[0] == payer.pubkey()  # noqa: S101
    executor = CycleExecutor(None, None, live_policy)
    signature, tx_wire, _ = executor._attest_wire(  # noqa: SLF001
        transaction, payer.pubkey(), [instruction]
    )
    assert signature == str(transaction.signatures[0])  # noqa: S101
    assert frame_wire(tx_wire)[4:] == tx_wire  # noqa: S101

    print("cycles.executor self_check: ok")


if __name__ == "__main__":
    self_check()
