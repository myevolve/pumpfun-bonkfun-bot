"""Bounded, public-payer CreateEvent -> real buyer -> unsigned simulation evidence.

No dotenv auto-loading, wallet, transaction submission, or strategy optimization.
Run --self-check offline; live runs require --env-file and a NEW --out JSONL file.
"""

# Standalone imports, internal interception, and synthetic boundary checks are intentional.
# Provider exception boundaries record failure without leaking credential-bearing text.
# ruff: noqa: E402, SLF001, S101, PLR2004, BLE001, TRY301

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import struct
import sys
import time
from collections import Counter
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, NoReturn, TypeVar
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Production logs can contain provider error URLs. This evidence tool emits only
# its own bounded JSON, never credentials or arbitrary transport exception text.
logging.disable(logging.CRITICAL)

from decode_provider_response import (
    RATE_LIMIT_MAX_ATTEMPTS,
    RATE_LIMIT_MAX_DELAY_SECONDS,
    RATE_LIMIT_MAX_ELAPSED_SECONDS,
    body_evidence,
    headers_evidence,
    rate_limit_retry_delay,
)
from dotenv import dotenv_values
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import Message
from solders.null_signer import NullSigner
from solders.signature import Signature
from solders.transaction import Transaction
from spl.token.instructions import get_associated_token_address

from core.client import (
    JsonRpcError,
    RpcUnavailableError,
    SolanaClient,
    _BlockhashContext,
    estimate_transaction_fee_lamports,
    set_loaded_accounts_data_size_limit,
)
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import WSOL_MINT, SystemAddresses, normalize_quote_mint
from geyser.generated import geyser_pb2
from interfaces.core import Platform, TokenInfo
from monitoring.event_normalization import normalize_geyser_update
from monitoring.parser_dispatch import parse_normalized_event
from monitoring.universal_geyser_listener import UniversalGeyserListener
from platforms import get_platform_implementations
from platforms.pumpfun.address_provider import PumpFunAddresses
from trading.platform_aware import PlatformAwareBuyer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from solana.rpc.async_api import AsyncClient
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    from platforms.pumpfun.curve_manager import PumpFunCurveManager

SCOPE = ContextVar("probe_rpc_scope", default="setup")
CANDIDATE = ContextVar("probe_candidate", default=None)
RPC_EVIDENCE = ContextVar("probe_rpc_evidence", default=None)
PAYER = PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
MAX_QUOTE = 13_000_000
MAX_FEE = 250_000
PRIORITY = 200_000
HOLD_SECONDS = 10
RPC_LIMIT = 200
FINAL_RESERVE = 4
PACKET_LIMIT = 1232
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ASSUMPTIONS = {
    "mode": "read_only_unsigned_current_bank_diagnostic",
    "payer": str(PAYER),
    "payer_kind": "public_normal_protocol_fee_recipient_not_user_wallet",
    "signatures": "all_default_sigVerify_false_never_submitted",
    "source": "canonical_Geyser_processed_verified_CreateEvent",
    "timing": "local_monotonic_includes_queue_throttle_and_RPC_not_cloud_latency",
    "receive_clock": "receiver_coroutine_delivery_not_physical_network_arrival",
    "consumer_clock": "execution_task_first_run_before_readiness_checks_and_buyer",
    "HTTP_clocks": "request_evidence_start_then_HTTP_call_headers_delivery_and_body_completion_not_wire_arrival",
    "response_hash": "SHA256_of_received_decoded_HTTP_entity_bytes_complete_or_bounded_prefix",
    "stream_coverage": "only_updates_delivered_before_bounded_stream_close_final_delivered_update_fully_classified",
    "slots": "minContextSlot_is_bank_lower_bound_not_same_slot_or_leader_inclusion",
    "pacing": "candidate_RPC_starts_500ms_apart_background_and_setup_use_production_rate_limit",
    "retries": "complete_HTTP_429_only_max_3_physical_attempts_max_5s_delay_max_10s_logical_elapsed",
    "retry_accounting": "every_attempt_paced_counted_and_evidenced_no_hidden_transport_retries",
    "retry_cooldown": "client_wide_including_background_and_audit_invalid_or_unhonorable_delay_blocks_all_requests",
    "entry": "current_bank_simulation_not_executed_fill",
    "exit": "one_counterfactual_future_bank_mark_at_response_plus_10_seconds",
    "costs": "same_simulation_payer_debit_includes_all_rents_and_fees_no_rent_refund_income",
    "payer_economics": "protocol_recipient_can_receive_fee_credits_not_representative_of_a_trading_wallet",
    "net": "conditional_future_bank_quote_minus_observed_all_in_debit_minus_250000_exit_fee_bound",
    "selection": "first_bounded_creation_candidates_no_heldout_tuning_SOL_WSOL_only",
}
_Result = TypeVar("_Result")


class ProbeRefused(RuntimeError):  # noqa: N818
    """A local safety/readiness refusal, distinct from a native program error."""


def require(condition: bool, reason: str) -> None:  # noqa: FBT001
    if not condition:
        raise ProbeRefused(reason)


def unsigned_wire(transaction: Transaction) -> bytes:
    require(bool(transaction.signatures), "missing_signatures")
    require(
        all(sig == Signature.default() for sig in transaction.signatures),
        "unsafe_nondefault_signature",
    )
    require(
        len(transaction.signatures)
        == transaction.message.header.num_required_signatures,
        "signature_count_mismatch",
    )
    raw = bytes(transaction)
    require(len(raw) <= PACKET_LIMIT, "packet_limit_exceeded")
    require(transaction.message.account_keys[0] == PAYER, "nonpublic_payer")
    return raw


def execution_result(response: object, creation_slot: int) -> tuple[int, dict]:
    result = response.get("result") if isinstance(response, dict) else None
    require(isinstance(result, dict), "unknown_simulation_result")
    value, context = result.get("value"), result.get("context")
    require(isinstance(value, dict) and "err" in value, "unknown_simulation_execution")
    require(
        isinstance(context, dict) and type(context.get("slot")) is int,
        "unknown_simulation_context",
    )
    require(context["slot"] >= creation_slot, "simulation_bank_before_creation")
    if value["err"] is None:
        require(
            type(value.get("unitsConsumed")) is int and value["unitsConsumed"] > 0,
            "unknown_compute_consumption",
        )
    return context["slot"], value


async def poll_fees(operation: Callable[[], Awaitable[None]]) -> None:
    """Attribute nested production RPC tasks through context, not task names."""
    scope = SCOPE.set("background_fee")
    try:
        await operation()
    finally:
        SCOPE.reset(scope)


def dispatch_candidate(
    row: dict, operation: Callable[[dict], Awaitable[None]], emit: Callable[..., None]
) -> asyncio.Task[None]:
    """Stamp successful scheduling and first consumption at their real boundaries."""

    async def consume() -> None:
        row["consumer_start_monotonic"] = time.monotonic()
        emit(
            "consumer_start",
            candidate_id=row["candidate_id"],
            consumer_start_monotonic=row["consumer_start_monotonic"],
            dispatch_monotonic=row["dispatch_monotonic"],
        )
        await operation(row)

    task = asyncio.create_task(consume())
    row["dispatch_monotonic"] = time.monotonic()
    return task


class ProbeClient(SolanaClient):
    """Production decoding with bounded, fully evidenced read-only 429 retries."""

    def __init__(self, endpoint: str, emit: Callable[..., None]) -> None:
        super().__init__(endpoint)
        self.emit = emit
        self.starts = []
        self.gate = asyncio.Lock()
        self.halt = asyncio.Event()
        self.halt_reason = None
        self.blockhash_observed = None
        self.last_foreground_start = None
        self._instrumented_client = None
        self.cooldown_until = 0.0
        self.cooldown_blocked = False

    async def get_client(self) -> AsyncClient:
        client = await super().get_client()
        if client is not self._instrumented_client:
            # Keep solders request construction/parsing; intercept only its HTTP seam.
            client._provider.make_request_unparsed = self._typed_request_unparsed
            self._instrumented_client = client
        return client

    async def _typed_request_unparsed(self, body: object) -> str:
        row = RPC_EVIDENCE.get()
        require(isinstance(row, dict), "unattributed_typed_RPC")
        raw = await self._http_rpc(json.loads(body.to_json()), row)
        return raw.decode("utf-8")

    async def _wait_start(self, *, foreground: bool, deadline: float | None) -> None:
        while True:
            if self.cooldown_blocked:
                raise ProbeRefused("provider_cooldown_unhonorable")
            if self.halt.is_set() and SCOPE.get() != "final_audit":
                raise ProbeRefused(self.halt_reason or "transport_exhausted")
            now = time.monotonic()
            ready = self.cooldown_until
            if foreground and self.last_foreground_start is not None:
                ready = max(ready, self.last_foreground_start + 0.5)
            if deadline is not None and max(now, ready) >= deadline:
                if self.cooldown_until >= deadline:
                    self.cooldown_blocked = True
                    self.halt_reason = "provider_cooldown_unhonorable"
                    self.halt.set()
                raise ProbeRefused("RPC_logical_deadline_exceeded")
            if ready <= now:
                return
            # Headers from concurrent in-flight requests can extend the gate.
            await asyncio.sleep(ready - now)

    async def permit(
        self,
        method: str,
        *,
        previous: dict | None = None,
        deadline: float | None = None,
    ) -> dict:
        scope = SCOPE.get()
        async with self.gate:
            limit = RPC_LIMIT if scope == "final_audit" else RPC_LIMIT - FINAL_RESERVE
            if len(self.starts) >= limit:
                self.halt_reason = "rpc_start_budget_exhausted"
                self.halt.set()
                raise ProbeRefused(self.halt_reason)
            foreground = scope.startswith(("buy", "shadow")) or scope == "final_audit"
            await self._wait_start(foreground=foreground, deadline=deadline)
            row = {
                "rpc_id": len(self.starts) + 1,
                "scope": scope,
                "candidate_id": (CANDIDATE.get() or {}).get("candidate_id")
                if scope.startswith(("buy", "shadow"))
                else None,
                "method": method,
                "start_monotonic": time.monotonic(),
                "operation_id": previous["operation_id"]
                if previous
                else len(self.starts) + 1,
                "attempt": previous["attempt"] + 1 if previous else 1,
                "previous_rpc_id": previous["rpc_id"] if previous else None,
                "logical_deadline_monotonic": deadline,
            }
            row.update(
                request_start_monotonic=None,
                http_call_monotonic=None,
                headers_monotonic=None,
                response_headers_unix=None,
                body_complete_monotonic=None,
                response_monotonic=None,
                body_state="not_started",
                response_bytes=0,
                response_sha256=None,
                response_hash_scope="unavailable",
                http_status=None,
                http_diagnostics=body_evidence(None, 0),
            )
            self.starts.append(row)
            if foreground:
                self.last_foreground_start = row["start_monotonic"]
            self.emit("rpc_start", **row)
            return row

    def failed(self, exc: BaseException) -> None:
        if self._is_transport_exception(exc):
            self.halt_reason = self.halt_reason or "rpc_transport_exhausted"
            self.halt.set()

    async def _retry_rpc(
        self,
        method: str,
        operation: Callable[[dict], Awaitable[_Result]],
        *,
        deadline: float,
        first_admitted: bool = False,
    ) -> _Result:
        previous = None
        row = None
        retry = True
        try:
            async with asyncio.timeout_at(deadline):
                while retry:
                    if previous is not None or not first_admitted:
                        await self._rate_limiter.acquire()
                    row = await self.permit(
                        method, previous=previous, deadline=deadline
                    )
                    token = RPC_EVIDENCE.set(row)
                    terminal = "rpc_response"
                    retry = False
                    try:
                        result = await operation(row)
                        if time.monotonic() > deadline:
                            raise TimeoutError("RPC_logical_deadline_exceeded")
                    except (Exception, asyncio.CancelledError) as exc:
                        terminal = "rpc_error"
                        row["error_type"] = type(exc).__name__
                        if (
                            isinstance(exc, ProbeRefused)
                            and str(exc) == "provider_throttled"
                            and row["http_status"] == 429
                            and row["body_state"] == "complete"
                        ):
                            delay = rate_limit_retry_delay(
                                row["http_diagnostics"],
                                row["attempt"],
                                now_unix=row["response_headers_unix"],
                            )
                            retry = (
                                delay is not None
                                and not self.cooldown_blocked
                                and self.cooldown_until < deadline
                                and time.monotonic() < deadline
                            )
                            if not retry:
                                self.halt_reason = "provider_throttled"
                                self.halt.set()
                        if not retry:
                            self.failed(exc)
                            raise
                    finally:
                        row["retry_planned"] = retry
                        row["response_monotonic"] = time.monotonic()
                        RPC_EVIDENCE.reset(token)
                        self.emit(terminal, **row)
                    previous = row
                return result
        except (Exception, asyncio.CancelledError) as exc:
            self.failed(exc)
            self.emit(
                "rpc_operation_error",
                operation_id=row["operation_id"] if row else None,
                last_rpc_id=row["rpc_id"] if row else None,
                error_type=type(exc).__name__,
                response_monotonic=time.monotonic(),
            )
            raise
        finally:
            if (
                row is not None
                and row["scope"] == "buy_simulation"
                and (candidate := CANDIDATE.get()) is not None
            ):
                candidate.update(
                    simulation_rpc_id=row["rpc_id"],
                    simulation_request_id=row.get("request_id"),
                    **{
                        field: row[field]
                        for field in (
                            "operation_id",
                            "attempt",
                            "previous_rpc_id",
                            "request_start_monotonic",
                            "http_call_monotonic",
                            "headers_monotonic",
                            "response_headers_unix",
                            "body_complete_monotonic",
                            "response_monotonic",
                            "http_status",
                            "body_state",
                            "response_bytes",
                            "response_sha256",
                            "response_hash_scope",
                        )
                    },
                    http_diagnostics=dict(row["http_diagnostics"]),
                )
                candidate["detection_to_response_ms"] = (
                    row["response_monotonic"] - candidate["receive_monotonic"]
                ) * 1000

    async def _read_rpc(
        self,
        operation: Callable[[AsyncClient], Awaitable[_Result]],
        *,
        deadline_seconds: float = 15,
        **kwargs: object,  # noqa: ARG002
    ) -> _Result:
        deadline_seconds = min(15, deadline_seconds)
        deadline = time.monotonic() + min(
            RATE_LIMIT_MAX_ELAPSED_SECONDS, deadline_seconds
        )

        async def measured(client: AsyncClient) -> _Result:
            async def attempt(row: dict) -> _Result:
                result = await operation(client)
                context = getattr(result, "context", None)
                row["context_slot"] = getattr(context, "slot", None)
                if row["scope"] == "shadow":
                    candidate = CANDIDATE.get()
                    lower_bound = candidate.get(
                        "simulation_context_slot", candidate["source_slot"]
                    )
                    require(
                        type(getattr(context, "slot", None)) is int
                        and context.slot >= lower_bound,
                        "shadow_bank_before_entry_or_unknown",
                    )
                return result

            return await self._retry_rpc(
                "typed_read", attempt, deadline=deadline, first_admitted=True
            )

        # Production admission/decoder remains intact, without hidden retries.
        try:
            async with asyncio.timeout_at(deadline):
                return await super()._read_rpc(
                    measured, max_attempts=1, deadline_seconds=deadline_seconds
                )
        except (Exception, asyncio.CancelledError) as exc:
            self.failed(exc)
            raise

    def _observe_rate_limit(self, row: dict) -> None:
        """Gate all subsequent requests as soon as limit headers arrive."""
        delay = rate_limit_retry_delay(
            row["http_diagnostics"],
            min(row["attempt"], 2),
            now_unix=row["response_headers_unix"],
        )
        if delay is None:
            self.cooldown_blocked = True
        else:
            self.cooldown_until = max(
                self.cooldown_until, row["headers_monotonic"] + delay
            )
            row["retry_not_before_monotonic"] = self.cooldown_until
            if self.cooldown_until >= row["logical_deadline_monotonic"]:
                self.cooldown_blocked = True
        if self.cooldown_blocked:
            self.halt_reason = "provider_throttled"
            self.halt.set()

    async def _http_rpc(self, body: dict, row: dict) -> bytes:
        """One bounded request, shared by typed reads and raw simulations."""
        method = body.get("method")
        require(
            method
            in {
                "simulateTransaction",
                "getSlot",
                "getAccountInfo",
                "getMultipleAccounts",
                "getBalance",
                "getMinimumBalanceForRentExemption",
                "getLatestBlockhash",
                "getBlockHeight",
                "getTokenAccountBalance",
                "getSignatureStatuses",
            },
            "RPC_method_not_allowlisted",
        )
        if method == "simulateTransaction":
            params = body["params"]
            require(
                params[1].get("sigVerify") is False,
                "signature_verification_not_disabled",
            )
            unsigned_wire(
                Transaction.from_bytes(base64.b64decode(params[0], validate=True))
            )
        require(row["request_start_monotonic"] is None, "multiple_requests_per_RPC")
        session = await self._get_session()
        await self._wait_start(
            foreground=False, deadline=row["logical_deadline_monotonic"]
        )
        row.update(method=body.get("method"), request_id=body.get("id"))
        digest = hashlib.sha256()
        chunks = bytearray()
        try:
            # Keep the ten-second individual transport bound inside the logical bound.
            async with asyncio.timeout(10):
                row["request_start_monotonic"] = time.monotonic()
                self.emit("rpc_request", **row)
                row["http_call_monotonic"] = time.monotonic()
                async with session.post(
                    self.rpc_endpoint, json=body, allow_redirects=False
                ) as response:
                    row.update(
                        headers_monotonic=time.monotonic(),
                        response_headers_unix=time.time(),
                        http_status=response.status,
                    )
                    row["http_diagnostics"].update(
                        headers_evidence(response.status, response.headers)
                    )
                    row["body_state"] = "interrupted"
                    if response.status == 429:
                        self._observe_rate_limit(row)
                    self.emit("rpc_headers", **row)
                    while True:
                        chunk = await response.content.read(
                            min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks))
                        )
                        if not chunk:
                            row.update(
                                body_state="complete",
                                body_complete_monotonic=time.monotonic(),
                            )
                            break
                        chunks.extend(chunk)
                        digest.update(chunk)
                        if len(chunks) > MAX_RESPONSE_BYTES:
                            row["body_state"] = "oversized"
                            raise ProbeRefused("RPC_response_body_too_large")
                    raw = bytes(chunks)
                    row["http_diagnostics"].update(body_evidence(raw, body.get("id")))
                    if response.status == 429:
                        raise ProbeRefused("provider_throttled")
                    response.raise_for_status()
                    require(response.status == 200, "unexpected_HTTP_status")
                    return raw
        finally:
            if row["http_status"] == 429 and row["body_state"] != "complete":
                self.halt_reason = "provider_throttled"
                self.halt.set()
            row["response_bytes"] = len(chunks)
            if row["headers_monotonic"] is not None:
                row["response_sha256"] = digest.hexdigest()
                row["response_hash_scope"] = (
                    "complete_decoded_HTTP_entity"
                    if row["body_state"] == "complete"
                    else "received_decoded_HTTP_entity_prefix"
                )

    async def post_rpc(
        self,
        body: dict,
        *,
        deadline_seconds: float = 10,
        **kwargs: object,  # noqa: ARG002
    ) -> dict:
        method = body.get("method")
        deadline = time.monotonic() + min(
            RATE_LIMIT_MAX_ELAPSED_SECONDS, deadline_seconds
        )

        async def attempt(row: dict) -> dict:
            raw = await self._http_rpc(body, row)
            payload = json.loads(raw)
            require(isinstance(payload, dict), "unknown_RPC_response")
            if "error" in payload:
                row["native_RPC_error"] = True
                if method == "simulateTransaction" and row["scope"] == "buy_simulation":
                    return payload
                raise JsonRpcError(method, "provider_error_recorded")
            require("result" in payload, "unknown_RPC_result")
            return payload

        return await self._retry_rpc(method, attempt, deadline=deadline)

    async def get_latest_blockhash(self) -> Hash:
        """Fee simulations replace the hash; reuse the real background cache."""
        return await self.get_cached_blockhash()

    async def _fetch_latest_blockhash_context(self) -> _BlockhashContext:
        result = await super()._fetch_latest_blockhash_context()
        self.blockhash_observed = time.monotonic()
        return result

    async def start_blockhash_updater(self, interval: float = 5.0) -> None:
        token = SCOPE.set("background_blockhash")
        try:
            await super().start_blockhash_updater(interval)
        finally:
            SCOPE.reset(token)


def settings(path: Path) -> dict:
    resolved = path.resolve()
    require(
        str(resolved).casefold()
        not in {str(ROOT / ".env").casefold(), str(ROOT / ".env~").casefold()}
        and not any(
            p.upper().startswith("ENVDATA") for p in (*path.parts, *resolved.parts)
        ),
        "credentials_file_forbidden",
    )
    require(
        resolved.is_file() and resolved.stat().st_size <= 65536,
        "credentials_file_missing_or_too_large",
    )
    # No interpolation, os.environ updates, wallet construction, or config loading.
    values = dotenv_values(resolved, interpolate=False)
    selected = {
        key: values.get(key)
        for key in (
            "SOLANA_NODE_RPC_ENDPOINT",
            "GEYSER_ENDPOINT",
            "GEYSER_API_TOKEN",
            "GEYSER_AUTH_TYPE",
        )
    }
    values.clear()
    require(
        all(
            selected.get(key)
            for key in (
                "SOLANA_NODE_RPC_ENDPOINT",
                "GEYSER_ENDPOINT",
                "GEYSER_API_TOKEN",
            )
        ),
        "provider_settings_missing",
    )
    url = urlsplit(selected["SOLANA_NODE_RPC_ENDPOINT"])
    require(
        url.scheme == "https" and bool(url.hostname) and not url.fragment,
        "https_RPC_required",
    )
    selected["GEYSER_AUTH_TYPE"] = selected["GEYSER_AUTH_TYPE"] or "x-token"
    return selected


def observed_entry(
    value: dict, token: TokenInfo, account_keys: list[str], ata: Pubkey
) -> dict:
    """Prove acquisition from an absent ATA and costs from the SAME bank's balances."""
    observation = {
        "acquired_raw": None,
        "entry_total_debit_lamports": None,
        "base_ata_lamports": None,
        "other_new_account_lamports": None,
        "entry_fee_lamports": value.get("fee"),
        "costs_complete": False,
    }
    try:
        require(value.get("err", "unknown") is None, "entry_not_successful")
        before, after = value.get("preBalances"), value.get("postBalances")
        require(
            isinstance(before, list)
            and isinstance(after, list)
            and len(before) == len(after) == len(account_keys),
            "same_bank_balances_unavailable",
        )
        require(
            all(type(n) is int and n >= 0 for n in (*before, *after)),
            "invalid_simulation_balances",
        )
        payer_index, ata_index = (
            account_keys.index(str(PAYER)),
            account_keys.index(str(ata)),
        )
        observation["base_ata_pre_lamports"] = before[ata_index]
        observation["payer_pre_lamports"] = before[payer_index]
        observation["payer_post_lamports"] = after[payer_index]
        observation["entry_total_debit_lamports"] = (
            before[payer_index] - after[payer_index]
        )
        observation["base_ata_lamports"] = after[ata_index]
        observation["other_new_account_lamports"] = sum(
            new
            for index, (old, new) in enumerate(zip(before, after, strict=True))
            if old == 0 and index != ata_index
        )
        require(before[ata_index] == 0, "preexisting_base_inventory_not_attributable")
        accounts = value.get("accounts")
        require(
            isinstance(accounts, list)
            and len(accounts) == 2
            and all(isinstance(a, dict) for a in accounts),
            "post_accounts_unavailable",
        )
        payer_account, base_account = accounts
        require(
            payer_account.get("lamports") == after[payer_index]
            and base_account.get("lamports") == after[ata_index],
            "post_accounts_balance_mismatch",
        )
        require(
            base_account.get("owner") == str(token.token_program_id),
            "base_account_program_mismatch",
        )
        encoded = base_account.get("data")
        require(
            isinstance(encoded, list) and len(encoded) == 2 and encoded[1] == "base64",
            "base_account_encoding_unknown",
        )
        data = base64.b64decode(encoded[0], validate=True)
        require(
            len(data) >= 165
            and data[:32] == bytes(token.mint)
            and data[32:64] == bytes(PAYER)
            and data[108] in (1, 2),
            "base_postaccount_mint_owner_or_state_invalid",
        )
        quantity = int.from_bytes(data[64:72], "little")
        require(quantity > 0, "no_acquired_tokens")
        observation["acquired_raw"] = quantity
        require(
            type(value.get("fee")) is int and 0 < value["fee"] <= MAX_FEE,
            "entry_fee_unknown_or_above_cap",
        )
        require(
            observation["entry_total_debit_lamports"] > 0, "entry_debit_not_positive"
        )
        observation["costs_complete"] = True
        observation["reason"] = "absent_ATA_quantity_and_same_bank_all_in_payer_debit"
    except Exception as exc:
        observation["reason"] = (
            str(exc) if isinstance(exc, ProbeRefused) else type(exc).__name__
        )
    return observation


async def shadow(
    curve: PumpFunCurveManager,
    token: TokenInfo,
    row: dict,
    response_at: float,
    emit: Callable[..., None],
) -> None:
    due = response_at + HOLD_SECONDS
    emit(
        "shadow_committed",
        candidate_id=row["candidate_id"],
        due_monotonic=due,
        hold_seconds=HOLD_SECONDS,
        marks=1,
    )
    await asyncio.sleep(max(0, due - time.monotonic()))
    SCOPE.set("shadow")
    mark = {
        "candidate_id": row["candidate_id"],
        "due_monotonic": due,
        "started_monotonic": time.monotonic(),
        "mark_type": "counterfactual_future_bank_mark",
        "status": "unpriced",
        "net_lamports": None,
        "entry_observation": row.get("entry_observation"),
        "exit_fee_estimate_lamports": MAX_FEE,
        "exit_fee_assumption": "conservative_250000_lamport_cap_not_observed_sell_fee",
        "rent_refund_income_lamports": 0,
        "acquired_raw": (row.get("entry_observation") or {}).get("acquired_raw")
        if row.get("simulation_status") == "simulated"
        else None,
    }
    try:
        async with asyncio.timeout(30):
            state, _ = await curve.get_sell_state_and_token_program(
                token.bonding_curve, token.mint, commitment="processed"
            )
            mark["venue"] = state.get("venue", "bonding_curve")
            require(
                normalize_quote_mint(state.get("quote_mint")) == WSOL_MINT,
                "shadow_quote_not_SOL",
            )
            if mark["acquired_raw"] is None:
                mark["reason"] = "acquisition_delta_unavailable_or_entry_failed"
            else:
                pool = state.get("pool_address", token.bonding_curve)
                mark[
                    "exit_quote_after_protocol_fees_lamports"
                ] = await curve.calculate_sell_amount_out(
                    pool, mark["acquired_raw"], pool_state=state
                )
                entry = row.get("entry_observation") or {}
                if entry.get("costs_complete") is True:
                    mark["net_lamports"] = (
                        mark["exit_quote_after_protocol_fees_lamports"]
                        - entry["entry_total_debit_lamports"]
                        - MAX_FEE
                    )
                    mark["status"] = "conditionally_priced"
                    mark["reason"] = (
                        "observed_all_in_debit_and_explicit_exit_fee_bound_no_refunds"
                    )
                else:
                    mark["reason"] = "entry_cost_attribution_unavailable"
    except Exception as exc:
        mark["reason"] = (
            str(exc) if isinstance(exc, ProbeRefused) else type(exc).__name__
        )
    mark["response_monotonic"] = time.monotonic()
    emit("shadow_mark", **mark)


async def run_probe(  # noqa: C901, PLR0912, PLR0915
    args: argparse.Namespace, config: dict, emit: Callable[..., None]
) -> int:
    client = ProbeClient(config["SOLANA_NODE_RPC_ENDPOINT"], emit)
    curve = None
    channel = call = None
    tasks = []
    counts = Counter()
    observed_skips = []
    reason = "startup_incomplete"
    window_start = None
    receiver_closed = None
    last_head = None
    listener = None
    summary = None
    try:
        impl = get_platform_implementations(Platform.PUMP_FUN, client)
        curve = impl.curve_manager
        for schedule in (curve.fee_schedule, curve.pumpswap.fee_schedule):
            schedule._poll = partial(poll_fees, schedule._poll)
        wallet = SimpleNamespace(
            pubkey=PAYER,
            keypair=NullSigner(PAYER),
            get_associated_token_address=partial(get_associated_token_address, PAYER),
        )
        priority = PriorityFeeManager(
            client=client,
            enable_dynamic_fee=False,
            enable_fixed_fee=True,
            fixed_fee=PRIORITY,
            extra_fee=0.0,
            hard_cap=PRIORITY,
        )
        buyer = PlatformAwareBuyer(
            client,
            wallet,
            priority,
            0.01,
            slippage=0.3,
            max_retries=1,
            extreme_fast_mode=True,
            extreme_fast_token_amount=250_000,
            allowed_quote_mints={WSOL_MINT},
        )

        async def intercept(
            instructions: list[Instruction],
            signer_keypair: NullSigner,
            priority_fee: int | None = None,
            compute_unit_limit: int | None = None,
            account_data_size_limit: int | None = None,
            **context: object,
        ) -> NoReturn:
            row = CANDIDATE.get()
            require(isinstance(row, dict), "missing_candidate_context")
            require(
                isinstance(signer_keypair, NullSigner)
                and signer_keypair.pubkey() == PAYER,
                "real_signer_forbidden",
            )
            require(
                type(context.get("quote_amount_raw")) is int
                and 0 < context["quote_amount_raw"] <= MAX_QUOTE,
                "quote_cap_exceeded",
            )
            require(
                priority_fee == PRIORITY
                and type(compute_unit_limit) is int
                and 0 < compute_unit_limit <= 1_400_000,
                "compute_envelope_rejected",
            )
            fee = estimate_transaction_fee_lamports(priority_fee, compute_unit_limit)
            require(
                context.get("fee_lamports") == fee and fee <= MAX_FEE,
                "fee_cap_exceeded",
            )
            require(
                normalize_quote_mint(context.get("quote_mint")) == WSOL_MINT,
                "quote_not_SOL",
            )
            buy_ix = [
                ix for ix in instructions if ix.program_id == PumpFunAddresses.PROGRAM
            ]
            require(
                len(buy_ix) == 1
                and len(buy_ix[0].data) == 24
                and buy_ix[0].data[:8]
                == impl.instruction_builder._buy_v2_discriminator,
                "unexpected_buy_instruction",
            )
            amount, quote_cap = struct.unpack("<QQ", buy_ix[0].data[8:])
            require(
                amount > 0 and 0 < quote_cap <= MAX_QUOTE,
                "invalid_instruction_floor_or_cap",
            )
            curve.fee_schedule.require_snapshot()
            require(
                client._cached_blockhash is not None
                and client.blockhash_observed is not None
                and time.monotonic() - client.blockhash_observed <= 10,
                "blockhash_readiness_miss",
            )
            preamble = []
            if account_data_size_limit is not None:
                preamble.append(
                    set_loaded_accounts_data_size_limit(account_data_size_limit)
                )
            preamble.extend(
                [
                    set_compute_unit_limit(compute_unit_limit),
                    set_compute_unit_price(priority_fee),
                ]
            )
            message = Message.new_with_blockhash(
                [*preamble, *instructions], PAYER, await client.get_cached_blockhash()
            )
            raw = unsigned_wire(
                Transaction.populate(
                    message,
                    [Signature.default()] * message.header.num_required_signatures,
                )
            )
            row.update(
                assembled_monotonic=time.monotonic(),
                packet_bytes=len(raw),
                instruction_token_amount_raw=amount,
                instruction_quote_cap_lamports=quote_cap,
                fee_envelope_lamports=fee,
                compute_limit=compute_unit_limit,
            )
            row["detection_to_assembly_ms"] = (
                row["assembled_monotonic"] - row["receive_monotonic"]
            ) * 1000
            ata = wallet.get_associated_token_address(
                row["token"].mint, row["token"].token_program_id
            )
            row["message_account_keys"] = [str(key) for key in message.account_keys]
            SCOPE.set("buy_simulation")
            response = await client.post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "simulateTransaction",
                    "params": [
                        base64.b64encode(raw).decode(),
                        {
                            "encoding": "base64",
                            "sigVerify": False,
                            "replaceRecentBlockhash": True,
                            "commitment": "processed",
                            "minContextSlot": row["source_slot"],
                            "accounts": {
                                "encoding": "base64",
                                "addresses": [str(PAYER), str(ata)],
                            },
                        },
                    ],
                }
            )
            rpc_error = response.get("error")
            if isinstance(rpc_error, dict):
                row["simulation_RPC_error_code"] = row["http_diagnostics"][
                    "rpc_error_code"
                ]
            # A response (including an unknown/malformed one) fixes this sole mark.
            result = response.get("result", {})
            raw_value = result.get("value", {}) if isinstance(result, dict) else {}
            raw_value = raw_value if isinstance(raw_value, dict) else {}
            row["pre_balances"] = raw_value.get("preBalances")
            row["post_balances"] = raw_value.get("postBalances")
            row["entry_observation"] = observed_entry(
                raw_value, row["token"], row["message_account_keys"], ata
            )
            tasks.append(
                asyncio.create_task(
                    shadow(curve, row["token"], row, row["response_monotonic"], emit)
                )
            )
            slot, value = execution_result(response, row["source_slot"])
            row.update(
                simulation_context_slot=slot,
                simulation_error=value["err"],
                units_consumed=value.get("unitsConsumed"),
                observed_fee_lamports=value.get("fee"),
                simulation_accounts=value.get("accounts"),
                base_ata=str(ata),
                simulation_logs=value.get("logs"),
                simulation_status="native_error"
                if value["err"] is not None
                else "simulated",
            )
            # Stop the actual buyer before confirmation/receipt reads. No fake txid.
            raise ProbeRefused("unsigned_simulation_complete_not_submitted")

        client.build_and_send_transaction = intercept
        async with asyncio.timeout(150):
            await client.get_cached_blockhash()
            await curve.prepare_live_execution()
            # AMM attestation spans 100 real probes; observe its config again
            # afterward rather than declaring an old snapshot fresh.
            await curve.pumpswap.fee_schedule.accept_account(
                await client.get_account_info(
                    curve.pumpswap.fee_schedule._fee_config, commitment="processed"
                )
            )
            swap_snapshot = curve.pumpswap.fee_schedule.require_snapshot()
            snapshot = curve.fee_schedule.require_snapshot()
            require(
                client.blockhash_observed is not None
                and time.monotonic() - client.blockhash_observed <= 10,
                "startup_blockhash_stale",
            )
        emit(
            "ready",
            fee_config_digest=snapshot.config.digest,
            pumpswap_fee_config_digest=swap_snapshot.config.digest,
            fee_observed_monotonic=snapshot.observed_at,
            fee_attested_monotonic=snapshot.attested_at,
            blockhash_observed_monotonic=client.blockhash_observed,
        )
        listener = UniversalGeyserListener(
            config["GEYSER_ENDPOINT"],
            config["GEYSER_API_TOKEN"],
            config["GEYSER_AUTH_TYPE"],
            [Platform.PUMP_FUN],
        )
        require(
            Platform.PUMP_FUN in listener.platform_parsers,
            "production_parser_unavailable",
        )
        stub, channel = await listener._create_geyser_connection()
        request = listener._create_subscription_request()
        slots_supported = (
            "slots" in geyser_pb2.SubscribeRequest.DESCRIPTOR.fields_by_name
        )
        if slots_supported:
            request.slots["head"].SetInParent()
        call = stub.Subscribe(iter([request]))
        await asyncio.wait_for(call.initial_metadata(), 15)
        curve.fee_schedule.require_snapshot()
        require(
            client._cached_blockhash is not None
            and client.blockhash_observed is not None
            and time.monotonic() - client.blockhash_observed <= 10,
            "window_blockhash_not_ready",
        )
        window_start = time.monotonic()
        emit(
            "window_open",
            monotonic=window_start,
            seconds=args.seconds,
            max_candidates=args.max_candidates,
            slot_subscription_requested=slots_supported,
        )

        async def execute(row: dict) -> None:
            token = CANDIDATE.set(row)
            scope = SCOPE.set("buy_read")
            try:
                try:
                    curve.fee_schedule.require_snapshot()
                    require(
                        client._cached_blockhash is not None
                        and client.blockhash_observed is not None
                        and time.monotonic() - client.blockhash_observed <= 10,
                        "blockhash_readiness_miss",
                    )
                except Exception as exc:
                    row["simulation_status"] = "readiness_miss"
                    raise ProbeRefused("fee_or_blockhash_readiness_miss") from exc
                async with asyncio.timeout(25):
                    result = await buyer.execute(row["token"])
                row["buyer_result_success"] = result.success
                if "simulation_status" not in row:
                    row["simulation_status"] = "not_successful"
                    row["reason"] = "buyer_did_not_return_valid_simulation"
                    row["buyer_error_class"] = (
                        client.halt_reason or "buyer_refused_or_failed"
                    )
            except Exception as exc:
                row.setdefault("simulation_status", "not_successful")
                row["reason"] = (
                    str(exc) if isinstance(exc, ProbeRefused) else type(exc).__name__
                )
            finally:
                rows = [
                    r for r in client.starts if r["candidate_id"] == row["candidate_id"]
                ]
                row["trade_triggered_read_RPCs"] = sum(
                    r["scope"] == "buy_read" for r in rows
                )
                row["simulation_RPCs"] = sum(
                    r["scope"] == "buy_simulation" for r in rows
                )
                row["successful_zero_read_assembly"] = (
                    row.get("simulation_status") == "simulated"
                    and row["trade_triggered_read_RPCs"] == 0
                )
                counts[row.get("simulation_status", "unknown")] += 1
                emit("execution", **{k: v for k, v in row.items() if k != "token"})
                CANDIDATE.reset(token)
                SCOPE.reset(scope)

        seen = set()
        deadline = window_start + args.seconds
        reason = "window_elapsed"
        iterator = call.__aiter__()
        while counts["candidates"] < args.max_candidates:
            if client.halt.is_set():
                reason = client.halt_reason
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                update = await asyncio.wait_for(
                    iterator.__anext__(), min(remaining, 15)
                )
            except TimeoutError:
                if time.monotonic() >= deadline:
                    break
                reason = "stream_idle_timeout_partial"
                break
            except StopAsyncIteration:
                reason = "stream_exhausted_partial"
                break
            received = time.monotonic()
            if update.HasField("slot"):
                last_head = update.slot.slot
                counts["slot_updates"] += 1
                continue
            counts["updates"] += 1
            try:
                event = normalize_geyser_update(update, commitment="processed")
                if event is None:
                    continue
                tokens = parse_normalized_event(event, listener.platform_parsers)
            except Exception as exc:
                counts["normalization_rejected"] += 1
                if counts["normalization_rejected"] <= 10:
                    emit(
                        "normalization_rejected",
                        receive_monotonic=received,
                        error_type=type(exc).__name__,
                    )
                continue
            if not tokens:
                counts["transactions_without_verified_creation"] += 1
                if any("Instruction: Create" in log for log in event.logs):
                    counts["candidates"] += 1
                    counts["skipped"] += 1
                    counts["unsupported_provenance"] += 1
                    skipped = {
                        "candidate_id": counts["candidates"],
                        "source_slot": event.slot,
                        "source_signature": event.signature,
                        "receive_monotonic": received,
                        "decision_monotonic": time.monotonic(),
                        "dispatch_monotonic": None,
                        "consumer_start_monotonic": None,
                        "observed_head_slot": last_head,
                        "quote_mint": None,
                        "provenance": {
                            "source": event.source,
                            "metadata_verified": False,
                        },
                        "decision": "skip",
                        "reason": "creation_logs_without_verified_creation",
                    }
                    observed_skips.append(skipped)
                    emit("candidate", **skipped)
                continue
            for coin in tokens:
                over_cap = counts["candidates"] >= args.max_candidates
                counts["over_cap_creations" if over_cap else "candidates"] += 1
                row = {
                    "candidate_id": None if over_cap else counts["candidates"],
                    "observed_creation_id": counts["candidates"]
                    + counts["over_cap_creations"],
                    "token": coin,
                    "mint": str(coin.mint),
                    "source_slot": coin.slot,
                    "source_signature": coin.signature,
                    "receive_monotonic": received,
                    "decision_monotonic": None,
                    "dispatch_monotonic": None,
                    "consumer_start_monotonic": None,
                    "observed_head_slot": last_head,
                    "quote_mint": str(coin.quote_mint),
                    "provenance": {
                        "source": coin.source,
                        "metadata_verified": coin.metadata_verified,
                        "state_from_event": coin.state_from_event,
                        "monitoring": (coin.additional_data or {}).get("monitoring"),
                    },
                }
                skip = None
                if over_cap:
                    skip = "candidate_cap_reached"
                elif (coin.signature, str(coin.mint)) in seen:
                    skip = "duplicate_creation"
                elif (
                    not coin.metadata_verified
                    or not coin.state_from_event
                    or type(coin.slot) is not int
                ):
                    skip = "unsupported_provenance"
                elif (
                    coin.quote_mint is None
                    or normalize_quote_mint(coin.quote_mint) != WSOL_MINT
                ):
                    skip = "unsupported_quote"
                elif not buyer._can_skip_refresh(coin):
                    skip = "creation_state_not_execution_ready"
                seen.add((coin.signature, str(coin.mint)))
                row["decision_monotonic"] = time.monotonic()
                if not skip:
                    tasks.append(dispatch_candidate(row, execute, emit))
                emit(
                    "candidate",
                    **{k: v for k, v in row.items() if k != "token"},
                    decision="skip" if skip else "eligible",
                    reason=skip or "verified_SOL_creation",
                )
                if skip:
                    observed_skips.append(
                        {k: v for k, v in row.items() if k != "token"}
                        | {"decision": "skip", "reason": skip}
                    )
                    if not over_cap:
                        counts["skipped"] += 1
                        counts[skip] += 1
        if counts["candidates"] >= args.max_candidates:
            reason = "candidate_cap_reached"
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ProbeRefused) else type(exc).__name__
        emit("probe_error", reason=reason)
    finally:
        receiver_closed = time.monotonic()
        emit(
            "receiver_closed",
            monotonic=receiver_closed,
            meaning="no_further_stream_updates_observed_pending_executions_may_continue",
        )
        if call is not None:
            call.cancel()
        if channel is not None:
            try:
                await channel.close()
            except Exception as exc:
                reason = "channel_cleanup_failed"
                emit(
                    "cleanup_error",
                    resource="geyser_channel",
                    error_type=type(exc).__name__,
                )
        # Executions can append their precommitted marks while being awaited.
        awaited = 0
        while awaited < len(tasks):
            batch = tasks[awaited:]
            awaited = len(tasks)
            outcomes = await asyncio.gather(*batch, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    reason = "candidate_task_incomplete"
                    emit("task_error", error_type=type(outcome).__name__)
        if curve is not None:
            try:
                await curve.close()
            except Exception as exc:
                reason = "fee_cleanup_failed"
                emit(
                    "cleanup_error",
                    resource="fee_schedules",
                    error_type=type(exc).__name__,
                )
        if client._blockhash_updater_task is not None:
            client._blockhash_updater_task.cancel()
            await asyncio.gather(client._blockhash_updater_task, return_exceptions=True)
        SCOPE.set("final_audit")
        try:
            audit = await client.post_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSlot",
                    "params": [{"commitment": "processed"}],
                }
            )
            emit(
                "final_audit",
                processed_slot=audit["result"],
                meaning="current_bank_head_only_no_wallet_or_inclusion_claim",
            )
        except Exception as exc:
            emit("final_audit", status="unknown", error_type=type(exc).__name__)
        await client.close()
        summary = {
            "reason": client.halt_reason or reason,
            "counts": dict(counts),
            "coverage": "bounded_window"
            if reason in {"window_elapsed", "candidate_cap_reached"}
            and not client.halt.is_set()
            else "partial",
            "window_open_monotonic": window_start,
            "receiver_closed_monotonic": receiver_closed,
            "observed_skips": observed_skips,
            "rpc_starts": len(client.starts),
            "rpc_scopes": dict(Counter(r["scope"] for r in client.starts)),
            "assumptions": ASSUMPTIONS,
        }
        emit("summary", **summary)
    return 0 if summary["coverage"] == "bounded_window" else 2


async def self_check() -> None:  # noqa: C901, PLR0912, PLR0915
    """Synthetic safety/attribution proof only; never opens a provider or .state."""
    message = Message.new_with_blockhash(
        [set_compute_unit_limit(1)], PAYER, Hash.default()
    )
    valid = Transaction.populate(message, [Signature.default()])
    unsigned_wire(valid)
    for invalid in (
        Transaction.populate(message, [Signature.from_bytes(bytes([1]) * 64)]),
    ):
        try:
            unsigned_wire(invalid)
        except ProbeRefused:
            pass
        else:
            raise AssertionError("unsafe signature accepted")  # noqa: TRY003
    for response in (
        {},
        {"result": {"context": {"slot": 1}, "value": {}}},
        {"result": {"context": {"slot": 1}, "value": {"err": None}}},
    ):
        try:
            execution_result(response, 1)
        except ProbeRefused:
            pass
        else:
            raise AssertionError("unknown execution accepted")  # noqa: TRY003
    assert (
        execution_result(
            {
                "result": {
                    "context": {"slot": 4},
                    "value": {"err": None, "unitsConsumed": 1},
                }
            },
            3,
        )[0]
        == 4
    )
    coin = SimpleNamespace(mint=PAYER, token_program_id=SystemAddresses.TOKEN_PROGRAM)
    ata = get_associated_token_address(PAYER, PAYER)
    keys = [str(PAYER), str(ata)]
    assert observed_entry({}, coin, keys, ata)["acquired_raw"] is None
    data = bytearray(165)
    data[:32], data[32:64], data[64:72], data[108] = (
        bytes(PAYER),
        bytes(PAYER),
        (20).to_bytes(8, "little"),
        1,
    )
    value = {
        "err": None,
        "fee": 5000,
        "preBalances": [100000, 0],
        "postBalances": [90000, 1000],
        "accounts": [
            {"lamports": 90000},
            {
                "lamports": 1000,
                "owner": str(coin.token_program_id),
                "data": [base64.b64encode(data).decode(), "base64"],
            },
        ],
    }
    observation = observed_entry(value, coin, keys, ata)
    assert observation["acquired_raw"] == 20 and observation["costs_complete"]
    assert observation["entry_total_debit_lamports"] == 10000
    value["preBalances"][1] = 1
    assert observed_entry(value, coin, keys, ata)["acquired_raw"] is None
    evidence = []
    client = ProbeClient(
        "https://invalid.example", lambda kind, **row: evidence.append((kind, row))
    )
    scope = SCOPE.set("buy_read")
    candidate = CANDIDATE.set({"candidate_id": 1})
    try:
        await client.permit("synthetic_read")

        async def background() -> None:
            await asyncio.wait_for(
                client.permit("synthetic_background_read"), timeout=1
            )

        await asyncio.create_task(poll_fees(background))
        await client.permit("synthetic_foreground_after_background")
        assert SCOPE.get() == "buy_read"
        assert client.starts[0]["candidate_id"] == 1
        assert client.starts[1]["candidate_id"] is None
        assert client.starts[1]["scope"] == "background_fee"
        assert (
            client.starts[2]["start_monotonic"] - client.starts[0]["start_monotonic"]
            >= 0.5
        )
    finally:
        CANDIDATE.reset(candidate)
        SCOPE.reset(scope)
        await client.close()

    async def unavailable_state(*args: object, **kwargs: object) -> NoReturn:  # noqa: ARG001
        raise RpcUnavailableError("synthetic shadow outage")  # noqa: TRY003

    await shadow(
        SimpleNamespace(get_sell_state_and_token_program=unavailable_state),
        SimpleNamespace(bonding_curve=PAYER, mint=PAYER),
        {"candidate_id": 1},
        time.monotonic() - HOLD_SECONDS,
        lambda kind, **row: evidence.append((kind, row)),
    )
    kind, mark = evidence[-1]
    assert kind == "shadow_mark" and mark["status"] == "unpriced"
    assert mark["reason"] == "RpcUnavailableError" and mark["net_lamports"] is None
    from tempfile import TemporaryDirectory  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    # In-process response contexts exercise the real transport and typed decoder.
    secret = "SYNTHETIC_CREDENTIAL_MUST_NOT_APPEAR"  # noqa: S105 - synthetic sentinel
    transport_events = []
    overlap = asyncio.Event()
    entered = 0
    mode = "complete"
    requests = []

    class SyntheticResponse:
        def __init__(
            self,
            body: dict,
            *,
            status: int | None = None,
            retry_after: str = "7",
            success: dict | None = None,
        ) -> None:
            self.status = (
                status
                if status is not None
                else 429
                if body["method"] == "simulateTransaction"
                else 200
            )
            self.headers = {
                "Retry-After": retry_after,
                "X-RateLimit-Remaining": "0",
                "Authorization": secret,
                "Location": "https://invalid.example/" + secret,
            }
            payload = (
                {
                    "error": {
                        "code": -32000,
                        "message": "connection limit exceeded",
                        "data": secret,
                    }
                }
                if self.status == 429
                else success
                if success is not None
                else {"result": 12}
            )
            self.raw = json.dumps(
                {"jsonrpc": "2.0", "id": body["id"], **payload}
            ).encode()
            self.content = self
            self.offset = 0

        async def __aenter__(self) -> object:
            nonlocal entered
            entered += 1
            if entered == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 2)
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def read(self, size: int) -> bytes:
            if mode == "interrupted":
                raise TimeoutError(secret)
            if mode == "oversized":
                self.offset += size
                return b"x" * size
            chunk = self.raw[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def raise_for_status(self) -> None:
            assert self.status == 200

    class SyntheticSession:
        closed = False

        def post(self, endpoint: str, *, json: dict, allow_redirects: bool) -> object:
            assert endpoint == "https://invalid.example" and not allow_redirects
            requests.append(json)
            return SyntheticResponse(json)

        async def close(self) -> None:
            self.closed = True

    def capture(kind: str, **row: object) -> None:
        transport_events.append((kind, json.loads(json.dumps(row))))

    linked = ProbeClient("https://invalid.example", capture)
    linked._session = SyntheticSession()
    creation = {
        "candidate_id": 41,
        "receive_monotonic": time.monotonic(),
        "decision_monotonic": time.monotonic(),
        "dispatch_monotonic": time.monotonic(),
        "consumer_start_monotonic": None,
    }
    simulation = {
        "jsonrpc": "2.0",
        "id": 71,
        "method": "simulateTransaction",
        "params": [
            base64.b64encode(unsigned_wire(valid)).decode(),
            {"sigVerify": False},
        ],
    }

    async def synthetic_buy(row: dict) -> None:
        candidate = CANDIDATE.set(row)
        scope = SCOPE.set("buy_simulation")
        try:
            assert row["consumer_start_monotonic"] is not None
            row["assembled_monotonic"] = time.monotonic()
            await linked.post_rpc(simulation)
        finally:
            SCOPE.reset(scope)
            CANDIDATE.reset(candidate)

    async def synthetic_background() -> int:
        scope = SCOPE.set("background_fee")
        try:
            result = await linked._read_rpc(lambda rpc: rpc.get_block_height())
            return result.value
        finally:
            SCOPE.reset(scope)

    try:
        scheduled = []
        create_task = asyncio.create_task

        def measured_schedule(
            operation: Coroutine[object, object, None],
        ) -> asyncio.Task[None]:
            task = create_task(operation)
            scheduled.append(time.monotonic())
            return task

        with patch.object(asyncio, "create_task", measured_schedule):
            consumer = dispatch_candidate(creation, synthetic_buy, capture)
        assert creation["dispatch_monotonic"] >= scheduled[0]
        assert creation["consumer_start_monotonic"] is None
        outcomes = await asyncio.gather(
            consumer, synthetic_background(), return_exceptions=True
        )
        assert isinstance(outcomes[0], ProbeRefused) and outcomes[1] == 12
        assert len(requests) == 2 and entered == 2
        terminals = [
            row
            for kind, row in transport_events
            if kind in {"rpc_error", "rpc_response"}
        ]
        assert len(terminals) == 2
        failed = next(row for row in terminals if row["candidate_id"] == 41)
        typed = next(row for row in terminals if row["candidate_id"] is None)
        assert (
            typed["method"] == "getBlockHeight" and typed["scope"] == "background_fee"
        )
        assert failed["rpc_id"] != typed["rpc_id"]
        assert failed["rpc_id"] == creation["simulation_rpc_id"]
        assert failed["request_id"] == 71
        assert failed["http_status"] == 429
        assert failed["http_diagnostics"]["http_status"] == 429
        assert (
            failed["http_diagnostics"]["provider_reported_reason"] == "connection_limit"
        )
        assert failed["http_diagnostics"]["retry_after"] == {
            "state": "delay_seconds",
            "seconds": 7,
        }
        assert failed["body_state"] == "complete"
        assert failed["response_hash_scope"] == "complete_decoded_HTTP_entity"
        assert len(failed["response_sha256"]) == 64
        clocks = [
            creation[field]
            for field in (
                "receive_monotonic",
                "decision_monotonic",
                "dispatch_monotonic",
                "consumer_start_monotonic",
                "assembled_monotonic",
                "request_start_monotonic",
                "http_call_monotonic",
                "headers_monotonic",
                "body_complete_monotonic",
                "response_monotonic",
            )
        ]
        assert clocks == sorted(clocks)
        for terminal in terminals:
            assert terminal["start_monotonic"] <= terminal["request_start_monotonic"]
            assert terminal["request_start_monotonic"] <= terminal["headers_monotonic"]
            assert terminal["headers_monotonic"] <= terminal["body_complete_monotonic"]
            assert terminal["body_complete_monotonic"] <= terminal["response_monotonic"]
            headers = next(
                row
                for kind, row in transport_events
                if kind == "rpc_headers" and row["rpc_id"] == terminal["rpc_id"]
            )
            assert headers["candidate_id"] == terminal["candidate_id"]
            assert headers["body_complete_monotonic"] is None
        # A refusal remains a refusal when its body fails; headers survive.
        for mode in ("interrupted", "oversized"):
            linked.halt.clear()
            linked.halt_reason = None
            linked.cooldown_blocked = False
            linked.cooldown_until = 0.0
            start = len(transport_events)
            try:
                await synthetic_buy(creation)
            except (ProbeRefused, TimeoutError):
                pass
            else:
                raise AssertionError("failed body accepted")  # noqa: TRY003
            terminal = [
                row
                for kind, row in transport_events[start:]
                if kind in {"rpc_error", "rpc_response"}
            ]
            assert len(terminal) == 1
            refusal = terminal[0]
            assert refusal["http_diagnostics"]["http_status"] == 429
            assert refusal["body_state"] == mode
            assert refusal["body_complete_monotonic"] is None
            assert (
                refusal["response_hash_scope"] == "received_decoded_HTTP_entity_prefix"
            )
            assert refusal["response_bytes"] <= MAX_RESPONSE_BYTES + 1
            assert linked.halt_reason == "provider_throttled"
        assert len(requests) == 4  # no diagnostic retries
        linked.halt.clear()
        linked.halt_reason = None
        linked.cooldown_blocked = False
        linked.cooldown_until = 0.0
        try:
            await linked._read_rpc(lambda rpc: rpc.request_airdrop(PAYER, 1))
        except ProbeRefused as exc:
            assert str(exc) == "RPC_method_not_allowlisted"
        else:
            raise AssertionError("typed mutation accepted")  # noqa: TRY003
        assert len(requests) == 4

        async def halt_during_pacing(_delay: float) -> None:
            linked.halt_reason = "provider_throttled"
            linked.halt.set()

        starts = len(linked.starts)
        scope = SCOPE.set("buy_read")
        try:
            with patch.object(asyncio, "sleep", halt_during_pacing):
                try:
                    await linked.permit("getSlot")
                except ProbeRefused:
                    pass
                else:
                    raise AssertionError("halted request admitted")  # noqa: TRY003
        finally:
            SCOPE.reset(scope)
        assert len(linked.starts) == starts and len(requests) == 4
        assert secret not in json.dumps(transport_events)
    finally:
        await linked.close()

    class RetrySession(SyntheticSession):
        def __init__(
            self,
            statuses: list[int],
            *,
            retry_after: str = "0",
            success: dict | None = None,
        ) -> None:
            self.statuses = list(statuses)
            self.retry_after = retry_after
            self.success = success
            self.calls = 0

        def post(self, endpoint: str, *, json: dict, allow_redirects: bool) -> object:
            assert endpoint == "https://invalid.example" and not allow_redirects
            assert self.statuses, "unexpected extra physical request"
            self.calls += 1
            return SyntheticResponse(
                json,
                status=self.statuses.pop(0),
                retry_after=self.retry_after,
                success=self.success,
            )

    mode = "complete"
    read_body = {"jsonrpc": "2.0", "id": 81, "method": "getSlot", "params": []}
    # Exercise the same loop through solders decoding and unsigned raw simulation.
    for typed_path in (True, False):
        transport_events.clear()
        linked = ProbeClient("https://invalid.example", capture)
        native = {"result": {"context": {"slot": 12}, "value": {"err": None}}}
        session = RetrySession([429, 200], success=None if typed_path else native)
        linked._session = session
        scope = SCOPE.set("background_fee" if typed_path else "buy_simulation")
        candidate = CANDIDATE.set(creation)
        try:
            if typed_path:
                result = await linked._read_rpc(lambda rpc: rpc.get_block_height())
                assert result.value == 12
            else:
                assert await linked.post_rpc(simulation) == {
                    "jsonrpc": "2.0",
                    "id": 71,
                    **native,
                }
                assert creation["simulation_rpc_id"] == linked.starts[-1]["rpc_id"]
                assert creation["attempt"] == 2 and creation["http_status"] == 200
            assert session.calls == 2 and not linked.halt.is_set()
            first, final = linked.starts
            assert first["http_status"] == 429 and final["http_status"] == 200
            assert first["attempt"] == 1 and final["attempt"] == 2
            assert first["previous_rpc_id"] is None
            assert final["previous_rpc_id"] == first["rpc_id"]
            assert final["operation_id"] == first["operation_id"] == first["rpc_id"]
            assert (
                final["request_start_monotonic"] >= first["retry_not_before_monotonic"]
            )
            assert first["response_headers_unix"] is not None
            assert final["response_monotonic"] <= final["logical_deadline_monotonic"]
            assert [
                kind
                for kind, _ in transport_events
                if kind in {"rpc_error", "rpc_response"}
            ] == ["rpc_error", "rpc_response"]
            for kind in ("rpc_start", "rpc_request"):
                assert [
                    row["rpc_id"] for event, row in transport_events if event == kind
                ] == [
                    first["rpc_id"],
                    final["rpc_id"],
                ]
            assert first["retry_planned"] and not final["retry_planned"]
            assert RPC_EVIDENCE.get() is None
        finally:
            CANDIDATE.reset(candidate)
            SCOPE.reset(scope)
            await linked.close()

    # Buffered reads may never yield to the event loop's timeout callback.
    original_read = SyntheticResponse.read

    async def buffered_read(response: SyntheticResponse, size: int) -> bytes:
        time.sleep(0.03)
        return await original_read(response, size)

    linked = ProbeClient("https://invalid.example", capture)
    session = RetrySession([200])
    linked._session = session
    try:
        with patch.object(SyntheticResponse, "read", buffered_read):
            try:
                await linked.post_rpc(read_body, deadline_seconds=0.02)
            except TimeoutError:
                pass
            else:
                raise AssertionError("buffered_response_exceeded_deadline")
        assert session.calls == 1 and linked.halt.is_set()
    finally:
        await linked.close()

    # Exhaustion is terminal; a final audit still honors the last shared delay.
    linked = ProbeClient("https://invalid.example", capture)
    session = RetrySession([429, 429, 429])
    linked._session = session
    try:
        try:
            await linked.post_rpc(read_body)
        except ProbeRefused as exc:
            assert str(exc) == "provider_throttled"
        else:
            raise AssertionError("429 exhaustion accepted")  # noqa: TRY003
        assert session.calls == RATE_LIMIT_MAX_ATTEMPTS
        assert linked.halt.is_set() and not linked.starts[-1]["retry_planned"]
        assert linked.starts[-1]["attempt"] == RATE_LIMIT_MAX_ATTEMPTS
    finally:
        await linked.close()

    # Cancellation during retry admission must not create or rewrite an attempt.
    terminal_seen = asyncio.Event()

    def capture_retry(kind: str, **row: object) -> None:
        capture(kind, **row)
        if kind == "rpc_error" and row.get("retry_planned"):
            terminal_seen.set()

    transport_events.clear()
    linked = ProbeClient("https://invalid.example", capture_retry)
    session = RetrySession([429, 200])
    linked._session = session
    pending = asyncio.create_task(linked.post_rpc(read_body))
    try:
        await asyncio.wait_for(terminal_seen.wait(), 2)
        retained = json.dumps(linked.starts[0], sort_keys=True)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("retry cancellation swallowed")  # noqa: TRY003
        assert session.calls == 1 and len(linked.starts) == 1
        assert json.dumps(linked.starts[0], sort_keys=True) == retained
        assert any(
            kind == "rpc_operation_error" and row["error_type"] == "CancelledError"
            for kind, row in transport_events
        )
        scope = SCOPE.set("final_audit")
        try:
            assert (await linked.post_rpc(read_body))["result"] == 12
            assert linked.starts[-1]["request_start_monotonic"] >= linked.cooldown_until
        finally:
            SCOPE.reset(scope)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await linked.close()

    # A recovered request cannot consume an audit's reserved admission slots.
    linked = ProbeClient("https://invalid.example", capture)
    linked.starts = [{} for _ in range(RPC_LIMIT - FINAL_RESERVE - 1)]
    session = RetrySession([429, 200])
    linked._session = session
    try:
        try:
            await linked.post_rpc(read_body)
        except ProbeRefused as exc:
            assert str(exc) == "rpc_start_budget_exhausted"
        else:
            raise AssertionError("retry consumed audit reserve")  # noqa: TRY003
        assert session.calls == 1 and len(linked.starts) == RPC_LIMIT - FINAL_RESERVE
        scope = SCOPE.set("final_audit")
        try:
            assert (await linked.post_rpc(read_body))["result"] == 12
            assert linked.starts[-1]["request_start_monotonic"] >= linked.cooldown_until
            linked.starts.extend({} for _ in range(RPC_LIMIT - len(linked.starts)))
            try:
                await linked.post_rpc(read_body)
            except ProbeRefused as exc:
                assert str(exc) == "rpc_start_budget_exhausted"
            else:
                raise AssertionError("global physical request cap bypassed")  # noqa: TRY003
            assert session.calls == 2
        finally:
            SCOPE.reset(scope)
    finally:
        await linked.close()

    # Invalid/too-long Retry-After, or a shorter operation deadline, blocks cleanup.
    for retry_after, deadline_seconds in (("7", 10), ("invalid", 10), ("1", 0.5)):
        linked = ProbeClient("https://invalid.example", capture)
        session = RetrySession([429], retry_after=retry_after)
        linked._session = session
        try:
            try:
                await linked.post_rpc(read_body, deadline_seconds=deadline_seconds)
            except ProbeRefused:
                pass
            else:
                raise AssertionError("unhonorable cooldown retried")  # noqa: TRY003
            scope = SCOPE.set("final_audit")
            try:
                try:
                    await linked.post_rpc(read_body)
                except ProbeRefused as exc:
                    assert str(exc) == "provider_cooldown_unhonorable"
                else:
                    raise AssertionError("audit bypassed blocked cooldown")  # noqa: TRY003
            finally:
                SCOPE.reset(scope)
            assert session.calls == 1
        finally:
            await linked.close()

    # An in-flight response may extend a cooldown while other scopes await it.
    linked = ProbeClient("https://invalid.example", capture)
    session = RetrySession([200, 200])
    linked._session = session
    sleeping = asyncio.Event()
    sleep = asyncio.sleep

    async def observe_delay(delay: float) -> None:
        sleeping.set()
        await sleep(delay)

    async def scoped_read(scope_name: str) -> dict:
        scope = SCOPE.set(scope_name)
        try:
            return await linked.post_rpc(read_body)
        finally:
            SCOPE.reset(scope)

    linked.cooldown_until = time.monotonic() + 0.05
    try:
        with patch.object(asyncio, "sleep", observe_delay):
            foreground = asyncio.create_task(scoped_read("buy_read"))
            background = asyncio.create_task(scoped_read("background_fee"))
            await asyncio.wait_for(sleeping.wait(), 2)
            extended = time.monotonic() + 0.1
            linked.cooldown_until = extended
            outcomes = await asyncio.gather(foreground, background)
        assert all(outcome["result"] == 12 for outcome in outcomes)
        assert all(row["request_start_monotonic"] >= extended for row in linked.starts)
        assert session.calls == 2
    finally:
        await linked.close()

    # Native/JSON-RPC errors and non-429 statuses are never retry triggers.
    for status, success in (
        (503, None),
        (200, {"error": {"code": -32000, "message": "native rejection"}}),
        (200, {"result": {"context": {"slot": 12}, "value": {"err": "native"}}}),
    ):
        linked = ProbeClient("https://invalid.example", capture)
        session = RetrySession([status], success=success)
        linked._session = session
        scope = SCOPE.set("buy_simulation")
        try:
            try:
                payload = await linked.post_rpc(simulation)
            except AssertionError:
                assert status == 503
            else:
                assert status == 200
                assert payload.get("error") or payload["result"]["value"]["err"]
            assert session.calls == 1
        finally:
            SCOPE.reset(scope)
            await linked.close()

    linked = ProbeClient("https://invalid.example", capture)
    session = RetrySession([])
    linked._session = session
    signed = Transaction.populate(message, [Signature.from_bytes(bytes([1]) * 64)])
    try:
        for unsafe in (
            {**simulation, "method": "sendTransaction"},
            {**simulation, "params": [simulation["params"][0], {"sigVerify": True}]},
            {
                **simulation,
                "params": [
                    base64.b64encode(bytes(signed)).decode(),
                    {"sigVerify": False},
                ],
            },
        ):
            try:
                await linked.post_rpc(unsafe)
            except ProbeRefused:
                pass
            else:
                raise AssertionError("unsafe request reached transport")  # noqa: TRY003
        assert session.calls == 0
    finally:
        await linked.close()
    # A synthetic root keeps this regression safe even if the guard breaks again.
    with TemporaryDirectory(prefix="creation-probe-synthetic-") as directory:
        fake_root = Path(directory).resolve()
        alias = fake_root / ".ENV"
        alias.write_text(
            "SOLANA_NODE_RPC_ENDPOINT=https://unused.invalid\n"
            "GEYSER_ENDPOINT=unused.invalid:443\n"
            "GEYSER_API_TOKEN=synthetic-not-secret\n",
            encoding="utf-8",
        )
        with patch.dict(globals(), {"ROOT": fake_root}):
            try:
                settings(alias)
            except ProbeRefused:
                pass
            else:
                raise AssertionError("protected dotenv alias accepted")  # noqa: TRY003
    print(
        json.dumps(
            {
                "kind": "self_check",
                "synthetic": True,
                "status": "passed",
                "checks": [
                    "unsafe_signature_rejected",
                    "unknown_result_rejected",
                    "slot_lower_bound",
                    "absent_ATA_quantity_and_all_in_debit",
                    "preexisting_inventory_not_inferred",
                    "context_local_attribution",
                    "failed_shadow_mark_retained",
                    "protected_dotenv_alias_rejected",
                    "safe_429_headers_and_bounded_body_evidence",
                    "typed_mutation_refused_before_transport",
                    "halt_during_pacing_prevents_dispatch",
                    "dispatch_clock_after_actual_scheduling",
                    "single_terminal_RPC_failure_record",
                    "overlapping_typed_raw_context_isolation",
                    "receive_to_native_request_clock_linkage",
                    "consumer_first_run_distinct_from_dispatch",
                    "interrupted_and_oversized_body_refusals",
                    "typed_and_raw_429_recovery_with_attempt_chain",
                    "bounded_429_exhaustion",
                    "retry_delay_cancellation_retains_terminal_evidence",
                    "retry_and_audit_preserve_global_cap_and_reserve",
                    "concurrent_cooldown_extension_rechecked",
                    "unhonorable_cooldown_blocks_final_audit",
                    "native_and_non429_errors_never_retried",
                    "signed_and_mutating_raw_requests_refused",
                ],
                "network_calls": 0,
            }
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    try:
        require(1 <= args.seconds <= 600, "seconds_out_of_bounds")
        require(1 <= args.max_candidates <= 30, "candidate_limit_out_of_bounds")
        if args.self_check:
            require(
                args.env_file is None and args.out is None,
                "self_check_is_offline_no_env_or_output_file",
            )
            asyncio.run(self_check())
            return 0
        require(
            args.env_file is not None and args.out is not None,
            "live_requires_env_file_and_new_out",
        )
        config = settings(args.env_file)
        require(
            args.out.suffix == ".jsonl" and ".state" not in args.out.resolve().parts,
            "new_JSONL_outside_state_required",
        )
        with args.out.open("x") as output:

            def emit(kind: str, **fields: object) -> None:
                text = json.dumps(
                    {"kind": kind, **fields}, default=str, separators=(",", ":")
                )
                output.write(text + "\n")
                output.flush()
                if kind in {"ready", "window_open", "summary"}:
                    print(text, flush=True)

            emit(
                "metadata",
                **ASSUMPTIONS,
                buy_amount_SOL=0.01,
                max_quote_lamports=MAX_QUOTE,
                started_unix=time.time(),
                max_fee_lamports=MAX_FEE,
                priority_micro_lamports_per_CU=PRIORITY,
                extreme_fast_token_amount=250_000,
                rate_limit_max_attempts=RATE_LIMIT_MAX_ATTEMPTS,
                rate_limit_max_delay_seconds=RATE_LIMIT_MAX_DELAY_SECONDS,
                rate_limit_max_elapsed_seconds=RATE_LIMIT_MAX_ELAPSED_SECONDS,
                production_transport_max_attempts=1,
                rpc_start_limit=RPC_LIMIT,
                foreground_rpc_start_spacing_ms=500,
                reserved_final_audit_starts=FINAL_RESERVE,
                shadow_hold_seconds=HOLD_SECONDS,
            )
            return asyncio.run(run_probe(args, config, emit))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "kind": "fatal",
                    "reason": str(exc)
                    if isinstance(exc, ProbeRefused)
                    else type(exc).__name__,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
