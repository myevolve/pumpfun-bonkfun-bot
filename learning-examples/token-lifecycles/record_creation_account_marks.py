#!/usr/bin/env python3
"""Collect source-locked, read-only C/D2 account marks alongside lifecycle evidence."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import heapq
import json
import logging
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import ClassVar

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Provider exceptions and imported library logs must never print credentials.
logging.disable(logging.CRITICAL)

import aiohttp
import evaluate_creation_account_marks as scoring
import evaluate_creation_paper as base
import record_lifecycles as lifecycle
from decode_provider_response import (
    MAX_ERROR_BODY_BYTES,
    RATE_LIMIT_MAX_ELAPSED_SECONDS,
    body_evidence,
    headers_evidence,
    rate_limit_retry_delay,
)
from solders.account import Account
from solders.pubkey import Pubkey

from platforms.pumpfun.address_provider import PumpFunAddresses, PumpFunAddressProvider
from platforms.pumpfun.fee_schedule import decode_fee_config_account

# Standalone import root and offline assertions are intentional.
# ruff: noqa: E402, PLR2004, S101

MAX_PENDING = 4096
FINAL_RESERVE = 16 * 1024**2
FEE_ADDRESS = str(PumpFunAddresses.find_fee_config())
ADDRESSES = PumpFunAddressProvider()


class CaptureRefused(RuntimeError):  # noqa: N818 - existing evidence sentinel convention
    """Stop acquisition, preserving an explicitly partial evidence tape."""


def provider_projection(value: dict) -> tuple[dict[str, str], tuple[str, ...]]:
    """Same strict projection/HTTPS convention as simulate_menu_orca_pair.load_provider."""
    base.exact(value, {"rpc_url", "geyser_endpoint", "geyser_token"}, "provider")
    base.require(
        all(
            isinstance(v, str)
            and v
            and not any(
                c.isspace() or unicodedata.category(c).startswith("C") for c in v
            )
            for v in value.values()
        ),
        "provider_string",
    )
    parsed = urlsplit(value["rpc_url"])
    base.require(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and not any(
            c.isspace() or unicodedata.category(c).startswith("C")
            for c in unquote(value["rpc_url"])
        ),
        "provider_https_url",
    )
    base.require(parsed.port is None or 0 < parsed.port < 65536, "provider_port")
    endpoint = urlsplit(value["geyser_endpoint"])
    base.require(
        endpoint.scheme == "https"
        and bool(endpoint.hostname)
        and (endpoint.port is None or 0 < endpoint.port < 65536)
        and not endpoint.path
        and not endpoint.query
        and not endpoint.fragment
        and endpoint.username is None
        and endpoint.password is None,
        "provider_geyser_endpoint",
    )
    secrets = tuple(
        {
            *value.values(),
            parsed.hostname,
            endpoint.hostname,
            *[part for part in unquote(parsed.path).split("/") if len(part) >= 8],
            *[
                part.split("=", 1)[-1]
                for part in unquote(parsed.query).split("&")
                if part
            ],
        }
    )
    return value, secrets


def contains_secret(value: object, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return bool(re.search(r"https?://", value, re.I)) or any(
            s in value for s in secrets
        )
    if isinstance(value, dict):
        return any(
            contains_secret(k, secrets) or contains_secret(v, secrets)
            for k, v in value.items()
        )
    return isinstance(value, list) and any(contains_secret(v, secrets) for v in value)


def target(  # noqa: PLR0913 - flat cross-file evidence schema
    mint: str,
    arm: str,
    stage: str,
    entry_at: float | None,
    entry_slot: int | None,
    due_at: float | None,
    due_slot: int,
    trigger_request_sequence: int | None = None,
) -> dict:
    return {
        "target_id": f"{mint}:{arm}:{stage}",
        "mint": mint,
        "arm": arm,
        "stage": stage,
        "entry_at": entry_at,
        "entry_slot": entry_slot,
        "due_at": due_at,
        "due_slot": due_slot,
        "trigger_request_sequence": trigger_request_sequence,
    }


def account_request(identifier: int, keys: list[str], minimum: int) -> dict:
    base.require(
        0 < len(keys) <= 100 and len(keys) == len(set(keys)), "account_batch_bound"
    )
    for key in keys:
        base.require(str(Pubkey.from_string(key)) == key, "noncanonical_public_key")
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "getMultipleAccounts",
        "params": [
            keys,
            {
                "encoding": "base64",
                "commitment": "processed",
                "minContextSlot": base.raw(minimum, "minContextSlot"),
            },
        ],
    }


def native_account(value: object) -> Account:
    base.require(
        isinstance(value, dict) and value.get("executable") is False,
        "account_missing_or_executable",
    )
    data = value.get("data")
    base.require(
        isinstance(data, list) and len(data) == 2 and data[1] == "base64",
        "account_encoding",
    )
    return Account(
        base.raw(value["lamports"], "account_lamports"),
        base64.b64decode(data[0], validate=True),
        Pubkey.from_string(value["owner"]),
        False,  # noqa: FBT003 - solders account wire constructor
        base.raw(value["rentEpoch"], "account_rent_epoch"),
    )


def fee_matches(value: object, digest: str) -> bool:
    return decode_fee_config_account(native_account(value)).digest == digest


def future_head(row: dict, after: float, minimum: int) -> bool:
    return row["received_monotonic"] > after and row["slot"] >= minimum


class Marks:
    """One bounded queue and one serialized RPC worker; no transaction capability."""

    def __init__(
        self,
        path: Path,
        lock: dict,
        marks_lock: dict,
        base_bytes: bytes,
        marks_bytes: bytes,
    ) -> None:
        self.lock, self.policy = lock, scoring.marks_policy()
        self.stream = path.open("x", encoding="utf-8")
        self.sequence = self.bytes = 0
        self.counts = Counter()
        self.pending: dict[str, dict] = {}
        self.waiting: dict[str, tuple[float, float | None]] = {}
        self.announced: set[str] = set()
        self.queue: list[tuple[float, str, str, str, str]] = []
        self.head = None
        self.anchor = None
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.last_start = None
        self.requests = 0
        self.reserved = False
        self.cooldown_until = 0.0
        self.cooldown_refused = False
        self.emit(
            "start",
            base_lock_sha256=hashlib.sha256(base_bytes).hexdigest(),
            marks_lock_sha256=hashlib.sha256(marks_bytes).hexdigest(),
            source_sha256=marks_lock["source_sha256"],
            policy=self.policy,
            started_monotonic=time.monotonic(),
        )

    def emit(self, kind: str, **fields: object) -> int:
        row = dict(
            schema_version=1,
            run_id=self.lock["run_id"],
            kind=kind,
            sequence=self.sequence,
            **fields,
        )
        line = json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
        size = len(line.encode())
        limit = self.policy["maximum_marks_bytes"] - (
            0 if self.reserved else FINAL_RESERVE
        )
        if self.bytes + size > limit:
            raise CaptureRefused("marks_storage_ceiling")
        self.stream.write(line)
        self.stream.flush()
        self.bytes += size
        self.sequence += 1
        self.counts[kind] += 1
        return self.sequence - 1

    def announce(self, item: dict) -> None:
        identifier = item["target_id"]
        if identifier not in self.announced:
            self.emit("target", **item)
            self.announced.add(identifier)

    def add(
        self, item: dict, after: float | None = None, expires: float | None = None
    ) -> None:
        identifier = item["target_id"]
        if identifier in self.pending:
            raise CaptureRefused("duplicate_target")
        if len(self.pending) >= MAX_PENDING:
            # Preserve the refused target as well as every previously queued target.
            self.reserved = True
            self.emit("target", **item)
            self.emit(
                "miss",
                target=item,
                reason="pending_target_ceiling",
                received_monotonic=time.monotonic(),
            )
            raise CaptureRefused("pending_target_ceiling")
        self.pending[identifier] = item
        if after is not None:
            self.waiting[identifier] = (after, expires)
        else:
            self.schedule(item)
        self.wake.set()

    def schedule(self, item: dict) -> None:
        self.announce(item)
        heapq.heappush(
            self.queue,
            (
                item["due_at"],
                item["arm"],
                item["mint"],
                item["stage"],
                item["target_id"],
            ),
        )

    def forget(self, item: dict) -> None:
        identifier = item["target_id"]
        self.pending.pop(identifier, None)
        self.waiting.pop(identifier, None)
        self.announced.discard(identifier)

    def miss(self, item: dict, reason: str) -> None:
        self.announce(item)
        self.emit(
            "miss", target=item, reason=reason, received_monotonic=time.monotonic()
        )
        self.forget(item)

    def creation(self, mint: str, slot: int, now: float) -> None:
        if self.anchor is None:
            return
        start = self.anchor + self.lock["capture"]["warmup_seconds"]
        if not start <= now < start + self.lock["capture"]["admission_seconds"]:
            return
        self.add(
            target(
                mint,
                "C",
                "trigger",
                now,
                slot,
                now + self.policy["trigger_delay_seconds"],
                slot,
            )
        )
        self.add(target(mint, "D2", "trigger", None, None, None, slot + 2), after=now)

    def on_head(self, row: dict) -> None:
        self.head = row
        if self.anchor is None:
            self.anchor = row["received_monotonic"]
        # Scan only pending head waits on slot updates, never all coins on transactions.
        for identifier, (after, expires) in tuple(self.waiting.items()):
            item = self.pending[identifier]
            when = row["received_monotonic"]
            ready = (
                when >= after and row["slot"] >= item["due_slot"]
                if item["stage"] == "trigger"
                else future_head(row, after, item["due_slot"])
            )
            if expires is not None and when > expires:
                self.miss(item, "exit_head_wait_expired")
            elif ready:
                self.waiting.pop(identifier)
                if item["stage"] == "trigger":
                    item.update(
                        entry_at=when,
                        entry_slot=row["slot"],
                        due_slot=row["slot"],
                        due_at=when + self.policy["trigger_delay_seconds"],
                    )
                else:
                    item["due_at"] = when
                self.schedule(item)
        self.wake.set()

    async def rpc(
        self,
        session: aiohttp.ClientSession,
        provider: dict,
        secrets: tuple[str, ...],
        purpose: str,
        items: list[dict],
    ) -> tuple[dict | None, int, float]:
        """Retry only complete HTTP 429s; the physical boundary owns all evidence."""
        operation_id = self.requests + 1
        logical_started = time.monotonic()
        deadline = logical_started + RATE_LIMIT_MAX_ELAPSED_SECONDS
        attempt, previous = 1, None
        while True:
            payload, sequence, received, retry = await self._rpc_attempt(
                session,
                provider,
                secrets,
                purpose,
                items,
                operation_id,
                attempt,
                previous,
                logical_started,
                deadline,
            )
            if not retry:
                return payload, sequence, received
            attempt, previous = attempt + 1, sequence

    async def _rpc_attempt(  # noqa: C901, PLR0912, PLR0913, PLR0915 - physical transport boundary
        self,
        session: aiohttp.ClientSession,
        provider: dict,
        secrets: tuple[str, ...],
        purpose: str,
        items: list[dict],
        operation_id: int,
        attempt: int,
        previous: int | None,
        logical_started: float,
        deadline: float,
    ) -> tuple[dict | None, int, float, bool]:
        audit = purpose != "marks"
        if self.cooldown_refused:
            raise CaptureRefused("rate_limit_cooldown_unavailable")
        ceiling = self.policy["maximum_requests"] - (
            0 if purpose == "audit_after" else 1
        )
        if self.requests >= ceiling:
            raise CaptureRefused("rpc_request_ceiling")
        not_before = max(
            self.cooldown_until,
            (self.last_start + self.policy["minimum_request_spacing_seconds"])
            if self.last_start is not None
            else 0,
        )
        if not_before >= deadline:
            if self.cooldown_until >= deadline:
                self.cooldown_refused = True
            raise CaptureRefused("rpc_admission_deadline")
        await asyncio.sleep(max(0, not_before - time.monotonic()))
        while True:
            now = time.monotonic()
            if not audit:
                fresh = []
                for item in items:
                    if scoring.response_expired(item, now):
                        self.miss(item, "mark_start_deadline_expired")
                    else:
                        fresh.append(item)
                items[:] = fresh
                if not items:
                    return None, -1, now, False
            if now >= deadline:
                raise CaptureRefused("rpc_logical_deadline")
            keys = [self.policy["public_payer"]] if audit else [FEE_ADDRESS]
            if purpose == "audit_before":
                keys.append(FEE_ADDRESS)
            elif not audit:
                for mint in sorted({item["mint"] for item in items}):
                    keys.extend(
                        [
                            str(
                                ADDRESSES.derive_pool_address(Pubkey.from_string(mint))
                            ),
                            mint,
                        ]
                    )
            minimum = max(
                [
                    self.head["slot"] if self.head else 0,
                    *[item["entry_slot"] for item in items],
                    *[item["due_slot"] for item in items],
                ]
            )
            request = account_request(self.requests + 1, keys, minimum)
            started = time.monotonic()
            if started >= deadline:
                raise CaptureRefused("rpc_logical_deadline")
            if not any(scoring.response_expired(item, started) for item in items):
                break
            # Construction crossed a cutoff: drop expired members and rebuild.
        fields = {
            "request_started_monotonic": started,
            "operation_id": operation_id,
            "attempt": attempt,
            "retry_of_sequence": previous,
            "logical_started_monotonic": logical_started,
            "logical_deadline_monotonic": deadline,
            "retry_not_before_monotonic": None,
            "response_headers_monotonic": None,
            "response_headers_unix": None,
            "response_received_monotonic": None,
            "head_slot_at_start": self.head["slot"] if self.head else None,
            "head_received_at_start": self.head["received_monotonic"]
            if self.head
            else None,
            "request": request,
            "targets": list(items),
            "purpose": purpose,
            "response_base64": None,
            "response_sha256": None,
            "response_bytes": 0,
            "body_complete": False,
            "http_status": None,
            "http_diagnostics": None,
            "error_code": None,
            "error_type": None,
        }
        self.last_start = fields["request_started_monotonic"]
        self.requests += 1
        payload = None
        body_hash = hashlib.sha256()
        chunks = []
        fatal = None
        received = fields["request_started_monotonic"]
        timeout = self.policy["rpc_timeout_seconds"]
        try:
            async with asyncio.timeout(min(timeout, deadline - time.monotonic())):
                async with session.post(
                    provider["rpc_url"],
                    json=request,
                    headers={"Accept-Encoding": "identity"},
                    allow_redirects=False,
                ) as response:
                    fields["response_headers_monotonic"] = time.monotonic()
                    fields["response_headers_unix"] = time.time()
                    fields["http_status"] = response.status
                    fields["http_diagnostics"] = {
                        **headers_evidence(response.status, response.headers),
                        **body_evidence(None, request["id"]),
                    }
                    if (
                        response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                    ):
                        raise CaptureRefused("http_content_encoding")  # noqa: TRY301
                    async for chunk in response.content.iter_chunked(65536):
                        fields["response_bytes"] += len(chunk)
                        body_hash.update(chunk)
                        if (
                            fields["response_bytes"]
                            > self.policy["maximum_response_bytes"]
                        ):
                            raise CaptureRefused("rpc_body_ceiling")  # noqa: TRY301
                        chunks.append(chunk)
                    fields["body_complete"] = True
                    received = time.monotonic()
                    if received > min(
                        deadline, fields["request_started_monotonic"] + timeout
                    ):
                        raise TimeoutError("rpc_response_deadline")  # noqa: TRY301 - cooperative timeout may not run on buffered data
                    # Only full EOF can supply body diagnostics; large HTTP failures stay unparsed.
                    body = (
                        b"".join(chunks)
                        if response.status == 200
                        or fields["response_bytes"] <= MAX_ERROR_BODY_BYTES
                        else None
                    )
                    fields["http_diagnostics"].update(
                        body_evidence(body, request["id"])
                    )
                    if body is None:
                        fields["http_diagnostics"]["body_kind"] = (
                            "over_diagnostic_limit"
                        )
                    if response.status != 200:
                        fields["error_code"] = response.status
                        raise CaptureRefused("http_status_failure")  # noqa: TRY301
                    payload = base.strict_json(body)
                    base.require(
                        payload.get("jsonrpc") == "2.0"
                        and type(payload.get("id")) is int
                        and payload["id"] == request["id"],
                        "rpc_identity",
                    )
                    if "error" in payload:
                        error = payload["error"]
                        code = error.get("code") if isinstance(error, dict) else None
                        fields["error_code"] = code if type(code) is int else None
                        fields["error_type"] = "JsonRpcError"
                        payload = None
                    else:
                        base.require(
                            not contains_secret(payload, secrets),
                            "response_credential_redacted",
                        )
                        base.exact(payload, {"jsonrpc", "id", "result"}, "rpc_response")
                        result = payload["result"]
                        base.require(
                            isinstance(result, dict)
                            and isinstance(result.get("context"), dict),
                            "rpc_context",
                        )
                        slot = result["context"].get("slot")
                        base.require(
                            type(slot) is int and slot >= minimum,
                            "rpc_context_before_request",
                        )
                        base.require(
                            isinstance(result.get("value"), list)
                            and len(result["value"]) == len(keys),
                            "rpc_account_count",
                        )
                        fields["response_base64"] = base64.b64encode(body).decode(
                            "ascii"
                        )
        except BaseException as exc:  # noqa: BLE001 - record cancellation, then re-raise
            fields["error_type"] = type(exc).__name__
            received = time.monotonic()
            payload = None
            fields["response_base64"] = None
            fatal = exc
        fields["response_received_monotonic"] = received
        fields["response_sha256"] = body_hash.hexdigest()
        retry = False
        if (
            fields["http_status"] == 429
            and fields["body_complete"]
            and fields["response_bytes"] <= self.policy["maximum_response_bytes"]
            and isinstance(fatal, CaptureRefused)
            and str(fatal) == "http_status_failure"
        ):
            diagnostics = fields["http_diagnostics"]
            header_clock = fields["response_headers_unix"]
            # Even exhaustion must honor the provider's cooldown before cleanup.
            cooldown = rate_limit_retry_delay(
                diagnostics, min(attempt, 2), now_unix=header_clock
            )
            if cooldown is None:
                self.cooldown_refused = True
            else:
                self.cooldown_until = max(
                    self.cooldown_until,
                    fields["response_headers_monotonic"] + cooldown,
                )
                fields["retry_not_before_monotonic"] = self.cooldown_until
                if self.cooldown_until >= deadline:
                    self.cooldown_refused = True
            delay = rate_limit_retry_delay(diagnostics, attempt, now_unix=header_clock)
            retry = bool(
                delay is not None
                and not self.cooldown_refused
                and self.requests < ceiling
                and max(
                    self.cooldown_until,
                    self.last_start + self.policy["minimum_request_spacing_seconds"],
                )
                < deadline
            )
        elif fields["http_status"] == 429:
            # Partial/oversize/transport failure cannot justify an early cleanup.
            self.cooldown_refused = True
        try:
            sequence = self.emit("rpc_retry" if retry else "rpc", **fields)
        except CaptureRefused:
            # Keep the request, target linkage and body digest even when raw storage is exhausted.
            self.reserved = True
            fields.update(
                response_base64=None,
                error_type=fields["error_type"] or "MarksStorageCeiling",
                storage_error="MarksStorageCeiling",
            )
            try:
                sequence = self.emit("rpc", **fields)
            except CaptureRefused as storage_error:
                if isinstance(fatal, asyncio.CancelledError):
                    raise fatal from storage_error
                raise
            if not isinstance(fatal, asyncio.CancelledError):
                fatal = CaptureRefused("marks_storage_ceiling")
            retry = False
            payload = None
        if retry:
            return None, sequence, received, True
        for item in items:
            self.forget(item)
        if fields["error_type"] is not None:
            self.counts["rpc_failures"] += 1
        if fatal is not None:
            raise fatal
        return payload, sequence, received, False

    async def worker(  # noqa: C901, PLR0912 - one serialized deadline queue
        self, session: aiohttp.ClientSession, provider: dict, secrets: tuple[str, ...]
    ) -> None:
        while not self.stop.is_set():
            self.wake.clear()
            now = time.monotonic()
            for identifier, (_, expires) in tuple(self.waiting.items()):
                if expires is not None and now > expires:
                    self.miss(self.pending[identifier], "exit_head_wait_expired")
            items = []
            while (
                self.queue
                and self.queue[0][0] <= now
                and len(items) < self.policy["maximum_targets_per_batch"]
            ):
                identifier = heapq.heappop(self.queue)[-1]
                if identifier in self.pending:
                    items.append(self.pending[identifier])
            if items:
                payload, sequence, received = await self.rpc(
                    session, provider, secrets, "marks", items
                )
                if payload is not None:
                    slot = payload["result"]["context"]["slot"]
                    # Fee drift never substitutes a new schedule. Scorer retains the raw unknown mark.
                    try:
                        matches = fee_matches(
                            payload["result"]["value"][0],
                            self.lock["fee_input"]["config_digest"],
                        )
                    except (ValueError, TypeError, KeyError):
                        matches = False
                    if not matches:
                        self.counts["fee_mismatches"] += 1
                    for item in items:
                        if scoring.response_expired(item, received):
                            self.counts["late_marks"] += 1
                            continue
                        # The gate removes missed targets from this batch before the request.
                        if item["stage"] == "trigger":
                            self.add(
                                target(
                                    item["mint"],
                                    item["arm"],
                                    "exit",
                                    item["entry_at"],
                                    item["entry_slot"],
                                    None,
                                    slot + self.policy["exit_delay_slots"],
                                    sequence,
                                ),
                                after=received,
                                expires=received
                                + self.policy["maximum_exit_head_wait_seconds"],
                            )
                continue
            delay = 0.25
            if self.queue:
                delay = min(delay, max(0, self.queue[0][0] - time.monotonic()))
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    def finish_pending(self, reason: str) -> None:
        self.reserved = True
        for item in list(self.pending.values()):
            self.miss(item, reason)
        self.queue.clear()


class AccountRecorder(lifecycle.Recorder):
    """Only observe the existing creation/slot hooks; do not rewrite base evidence."""

    def __init__(self, out: Path, lock: dict, marks: Marks) -> None:
        self.marks = marks
        self.capture_terminal = None
        super().__init__(
            out,
            120,
            1200,
            postgrad_seconds=600,
            postgrad_idle_seconds=120,
            max_bytes=4 * 1024**3,
            max_memory_bytes=512 * 1024**2,
            max_coins=10000,
            max_events=4_000_000,
            run_id=lock["run_id"],
        )

    def _on_create(
        self, ev: dict, slot: int, sig: str, now: float, provenance: dict
    ) -> None:
        exists = ev["mint"] in self.created_mints
        super()._on_create(ev, slot, sig, now, provenance)
        if not exists and ev["mint"] in self.coins:
            self.marks.creation(ev["mint"], slot, now)

    def emit(self, kind: str, row: dict, *, terminal: bool = False) -> None:
        if kind == "slots":
            row = dict(row, observed_monotonic=time.monotonic())
        super().emit(kind, row, terminal=terminal)
        if kind == "slots":
            self.marks.on_head(row)
        elif kind == "run" and row.get("kind") == "terminal":
            self.capture_terminal = row


async def collect(  # noqa: C901, PLR0912, PLR0915 - joint capture and teardown boundary
    base_path: Path, marks_path: Path, provider_path: Path, out: Path
) -> int:
    marks_lock = scoring.validate_marks_lock(base_path, marks_path)
    base_bytes, marks_bytes = base_path.read_bytes(), marks_path.read_bytes()
    base.require(
        base.strict_json(marks_bytes) == marks_lock
        and hashlib.sha256(base_bytes).hexdigest() == marks_lock["base_lock_sha256"],
        "locks_changed_during_validation",
    )
    lock = base.strict_json(base_bytes)
    snapshot, reason = base.validate_lock(lock, out, out.with_suffix(".slots.jsonl"))
    base.require(snapshot is not None and reason is None, "attested_fee_input_required")
    base.require(
        (ROOT / lock["run_journal_path"]).resolve()
        == out.with_suffix(".run.jsonl").resolve(),
        "locked_journal_path",
    )
    base.require(
        ".state" not in provider_path.parts or "wallets" not in provider_path.parts,
        "wallet_provider_path_forbidden",
    )
    provider_path = provider_path.resolve()
    base.require(
        provider_path.suffix == ".json"
        and not (".state" in provider_path.parts and "wallets" in provider_path.parts),
        "explicit_provider_json_required",
    )
    with provider_path.open("rb") as stream:
        projection_bytes = stream.read(65537)
    base.require(len(projection_bytes) <= 65536, "provider_projection_size")
    provider, secrets = provider_projection(base.strict_json(projection_bytes))
    paths = [
        out,
        *[
            out.with_suffix(s)
            for s in (".slots.jsonl", ".raw.jsonl", ".run.jsonl", ".marks.jsonl")
        ],
    ]
    base.require(len({p.resolve() for p in paths}) == 5, "output_path_collision")
    base.require(not any(p.exists() for p in paths), "output_already_exists")
    marks = Marks(paths[-1], lock, marks_lock, base_bytes, marks_bytes)
    rec = None
    capture_task = marker_task = None
    capture_code = 1
    error_type = None
    cancellation = None
    caller = asyncio.current_task()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=marks.policy["rpc_timeout_seconds"]),
            auto_decompress=False,
            trust_env=False,
        ) as session:
            try:
                payload, _, _ = await marks.rpc(
                    session, provider, secrets, "audit_before", []
                )
                if payload is None or not fee_matches(
                    payload["result"]["value"][1], lock["fee_input"]["config_digest"]
                ):
                    raise CaptureRefused("setup_fee_digest_mismatch_or_missing")  # noqa: TRY301
                native_account(payload["result"]["value"][0])
                rec = AccountRecorder(out, lock, marks)
                capture = lock["capture"]
                admission = capture["warmup_seconds"] + capture["admission_seconds"]
                credentials = {
                    "GEYSER_ENDPOINT": urlsplit(provider["geyser_endpoint"]).hostname,
                    "GEYSER_API_TOKEN": provider["geyser_token"],
                }
                marker_task = asyncio.create_task(
                    marks.worker(session, provider, secrets)
                )
                capture_task = asyncio.create_task(
                    lifecycle.run(
                        rec,
                        (admission + capture["drain_seconds"]) / 60,
                        credentials,
                        admission_seconds=admission,
                        drain_seconds=capture["drain_seconds"],
                        max_reconnects=0,
                        stream_idle_seconds=30,
                    )
                )
                done, _ = await asyncio.wait(
                    {capture_task, marker_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if marker_task in done:
                    # Await propagates background failure; it cannot disappear behind a healthy stream.
                    await marker_task
                    raise CaptureRefused("marker_ended_before_capture")  # noqa: TRY301
                capture_code = await capture_task
                marks.stop.set()
                marks.wake.set()
                await marker_task
            except BaseException as exc:  # noqa: BLE001 - cancel sibling and retain failure
                error_type = type(exc).__name__
                if isinstance(exc, asyncio.CancelledError):
                    cancellation = exc
            finally:
                marks.stop.set()
                marks.wake.set()
                for task in (capture_task, marker_task):
                    if task is None:
                        continue
                    if not task.done():
                        task.cancel()
                    try:
                        async with asyncio.timeout(5):
                            result = await task
                        if task is capture_task:
                            capture_code = result
                    except BaseException as exc:  # noqa: BLE001 - cleanup failure remains partial
                        error_type = error_type or type(exc).__name__
                        if (
                            isinstance(exc, asyncio.CancelledError)
                            and caller is not None
                            and caller.cancelling()
                        ):
                            cancellation = cancellation or exc
                if rec is not None:
                    rec.close()
                marks.finish_pending(
                    "capture_ended" if error_type is None else "capture_failed"
                )
                try:
                    payload, _, _ = await marks.rpc(
                        session, provider, secrets, "audit_after", []
                    )
                    base.require(payload is not None, "public_payer_after_missing")
                    native_account(payload["result"]["value"][0])
                except BaseException as exc:  # noqa: BLE001 - final audit must record cancellation
                    error_type = error_type or type(exc).__name__
                    if isinstance(exc, asyncio.CancelledError):
                        cancellation = exc
    except BaseException as exc:  # noqa: BLE001 - terminal evidence before exit
        error_type = error_type or type(exc).__name__
        if isinstance(exc, asyncio.CancelledError):
            cancellation = exc
    finally:
        if cancellation is not None:
            capture_code = 130
        complete = bool(
            capture_code == 0
            and error_type is None
            and rec is not None
            and rec.capture_terminal
            and rec.capture_terminal.get("complete") is True
            and not marks.pending
            and not marks.counts["miss"]
            and not marks.counts["rpc_failures"]
            and not marks.counts["fee_mismatches"]
            and not marks.counts["late_marks"]
        )
        try:
            marks.reserved = True
            marks.emit(
                "terminal",
                complete=complete,
                capture_exit_code=capture_code,
                ended_monotonic=time.monotonic(),
                counts=dict(marks.counts),
                pending_targets=list(marks.pending.values()),
                error_type=error_type,
            )
        finally:
            marks.stream.close()
            if cancellation is not None:
                raise cancellation
    return 0 if complete else (capture_code or 1)


def self_check() -> None:  # noqa: C901, PLR0915 - offline boundary scenarios
    """Offline guards: no network, clock patch, credentials, or retained artifacts."""
    from tempfile import TemporaryDirectory  # noqa: PLC0415 - offline self-check only
    from types import SimpleNamespace  # noqa: PLC0415 - offline fixtures only
    from unittest.mock import patch  # noqa: PLC0415 - no live transports in checks

    async def transport_check(directory: Path) -> None:
        secret = "offline-provider-secret-sentinel"  # noqa: S105 - synthetic sentinel
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32005, "message": "quota exceeded", "data": secret},
            }
        ).encode()

        class RefusalResponse:
            status = 429
            headers: ClassVar[dict[str, str]] = {
                "Retry-After": "2",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": secret,
                "Authorization": secret,
            }

            def __init__(self, *, interrupted: bool) -> None:
                self.interrupted = interrupted
                self.content = self

            async def __aenter__(self) -> RefusalResponse:
                return self

            async def __aexit__(self, *_: object) -> bool:
                return False

            async def iter_chunked(self, _: int) -> AsyncIterator[bytes]:
                yield body[:17] if self.interrupted else body
                if self.interrupted:
                    raise aiohttp.ClientPayloadError(secret)

            def post(self, *_args: object, **_kwargs: object) -> RefusalResponse:
                return self

        for interrupted in (False, True):
            path = directory / f"refusal-{interrupted}.jsonl"
            marks = Marks(
                path, {"run_id": "offline"}, {"source_sha256": {}}, b"{}", b"{}"
            )
            try:
                try:
                    await marks.rpc(
                        RefusalResponse(interrupted=interrupted),
                        {"rpc_url": f"https://rpc.invalid/{secret}"},
                        (secret,),
                        "audit_after",
                        [],
                    )
                except (CaptureRefused, aiohttp.ClientPayloadError) as exc:
                    assert type(exc) is (
                        aiohttp.ClientPayloadError if interrupted else CaptureRefused
                    )
                else:
                    raise AssertionError("http_refusal_returned_success")
                assert marks.requests == (1 if interrupted else 3)
                assert marks.counts["rpc_failures"] == 1
                assert marks.counts["rpc_retry"] == (0 if interrupted else 2)
            finally:
                marks.stream.close()
            text = path.read_text()
            assert (
                secret not in text
                and "Authorization" not in text
                and base64.b64encode(body).decode() not in text
            )
            row = json.loads(text.splitlines()[1])
            assert row["http_status"] == 429 and row["body_complete"] is not interrupted
            assert row["response_base64"] is None
            assert row["error_code"] == (None if interrupted else 429)
            observed = body[:17] if interrupted else body
            assert row["response_bytes"] == len(observed)
            assert row["response_sha256"] == hashlib.sha256(observed).hexdigest()
            assert (
                row["request_started_monotonic"]
                <= row["response_headers_monotonic"]
                <= row["response_received_monotonic"]
            )
            diagnostics = row["http_diagnostics"]
            assert diagnostics["http_status"] == 429
            assert diagnostics["retry_after"] == {
                "state": "delay_seconds",
                "seconds": 2,
            }
            assert diagnostics["rate_limit_headers"] == {"X-RateLimit-Remaining": 0}
            assert diagnostics["invalid_rate_limit_headers"] == ["X-RateLimit-Reset"]
            assert diagnostics["body_kind"] == (
                "unavailable" if interrupted else "json"
            )
            assert diagnostics["rpc_error_code"] == (None if interrupted else -32005)
            assert diagnostics["provider_reported_reason"] == (
                "unknown" if interrupted else "quota_exhausted"
            )
            assert (
                scoring.native_response(row, [marks.policy["public_payer"]], 0)["error"]
                == "rpc_failure"
            )

    async def retry_check(directory: Path) -> None:  # noqa: C901, PLR0912 - offline transport scenarios
        class Reply:
            def __init__(self, request: dict, status: int, delay: str) -> None:
                self.status, self.headers, self.content = (
                    status,
                    {"Retry-After": delay},
                    self,
                )
                self.body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "context": {"slot": request["params"][1]["minContextSlot"]},
                            "value": [None] * len(request["params"][0]),
                        },
                    }
                ).encode()

            async def __aenter__(self) -> Reply:
                return self

            async def __aexit__(self, *_: object) -> bool:
                return False

            async def iter_chunked(self, _: int) -> AsyncIterator[bytes]:
                yield self.body

        class Session:
            def __init__(self, statuses: list[int], delay: str = "1") -> None:
                self.statuses, self.delay, self.calls = list(statuses), delay, 0

            def post(self, _url: str, *, json: dict, **_kwargs: object) -> Reply:
                self.calls += 1
                return Reply(json, self.statuses.pop(0), self.delay)

        provider = {"rpc_url": "https://offline.invalid"}
        for name in ("recovery", "expiry", "reserve", "unhonorable"):
            path = directory / f"retry-{name}.jsonl"
            marks = Marks(
                path, {"run_id": "offline"}, {"source_sha256": {}}, b"{}", b"{}"
            )
            session = Session([429, 200], "6" if name == "unhonorable" else "1")
            try:
                items, purpose = [], "audit_after"
                if name == "expiry":
                    now = time.monotonic()
                    mint = str(Pubkey.new_unique())
                    items = [
                        target(mint, "C", "trigger", now - 11.6, 1, now - 1.6, 1),
                        target(mint, "D2", "trigger", now - 10.2, 1, now - 0.2, 1),
                    ]
                    for item in items:
                        marks.add(item)
                    purpose = "marks"
                if name == "reserve":
                    marks.requests = marks.policy["maximum_requests"] - 2
                    purpose = "audit_before"
                try:
                    payload, _, _ = await marks.rpc(
                        session, provider, (), purpose, items
                    )
                except CaptureRefused:
                    assert name in ("reserve", "unhonorable")
                    assert session.calls == 1 and marks.counts["rpc_retry"] == 0
                    if name == "reserve":
                        payload, _, _ = await marks.rpc(
                            session, provider, (), "audit_after", []
                        )
                        assert (
                            payload is not None
                            and marks.requests == marks.policy["maximum_requests"]
                        )
                    else:
                        try:
                            await marks.rpc(session, provider, (), "audit_after", [])
                        except CaptureRefused:
                            assert session.calls == 1
                        else:
                            raise AssertionError(
                                "early_cleanup_after_unhonorable_delay"
                            )
                else:
                    assert name in ("recovery", "expiry") and payload is not None
                    assert session.calls == 2 and marks.counts["rpc_retry"] == 1
                    assert marks.counts["rpc_failures"] == 0 and not marks.pending
                    if name == "expiry":
                        assert len(items) == 1 and items[0]["arm"] == "D2"
                        assert marks.counts["miss"] == 1
            finally:
                marks.stream.close()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            attempts = [row for row in rows if row["kind"] in ("rpc", "rpc_retry")]
            if name in ("recovery", "expiry"):
                first, last = attempts
                assert first["operation_id"] == last["operation_id"] == 1
                assert (
                    last["attempt"] == 2
                    and last["retry_of_sequence"] == first["sequence"]
                )
                assert (
                    last["request_started_monotonic"]
                    >= first["retry_not_before_monotonic"]
                )
                assert (
                    last["logical_deadline_monotonic"]
                    == first["logical_deadline_monotonic"]
                )

    async def boundary_check(directory: Path) -> None:  # noqa: C901, PLR0912, PLR0915 - coupled failure boundaries
        module = sys.modules[__name__]
        original_request = account_request

        class Reply:
            status = 200
            headers: ClassVar[dict[str, str]] = {}

            def __init__(self, session: Session, request: dict) -> None:
                self.session, self.content = session, self
                self.body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "context": {"slot": 1},
                            "value": [None] * len(request["params"][0]),
                        },
                    }
                ).encode()

            async def __aenter__(self) -> Reply:
                self.session.entered.set()
                if self.session.mode == "storage_cancel":
                    await asyncio.Event().wait()
                if self.session.mode == "late_response":
                    await asyncio.sleep(0.08)
                return self

            async def __aexit__(self, *_: object) -> bool:
                return False

            async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
                if self.session.mode == "buffered_timeout":
                    time.sleep(0.05)
                yield self.body
                if self.session.mode == "late_response":
                    self.session.marks.stop.set()

        class Session:
            def __init__(self, mode: str, marks: Marks) -> None:
                self.mode, self.marks, self.entered = mode, marks, asyncio.Event()

            def post(self, _url: str, *, json: dict, **_kwargs: object) -> Reply:
                return Reply(self, json)

        def build_after(cutoff: float, *args: object) -> dict:
            # Simulate local construction crossing a cutoff, without changing clocks.
            time.sleep(max(0, cutoff - time.monotonic()) + 0.02)
            return original_request(*args)

        for mode in (
            "storage_cancel",
            "preparation_expiry",
            "late_response",
            "buffered_timeout",
        ):
            path = directory / f"{mode}.jsonl"
            marks = Marks(path, {"run_id": mode}, {"source_sha256": {}}, b"{}", b"{}")
            session = Session(mode, marks)
            try:
                if mode == "buffered_timeout":
                    marks.policy["rpc_timeout_seconds"] = 0.02
                    try:
                        await marks.rpc(
                            session,
                            {"rpc_url": "https://offline.invalid"},
                            (),
                            "audit_after",
                            [],
                        )
                    except TimeoutError:
                        pass
                    else:
                        raise AssertionError("buffered_response_exceeded_deadline")
                    continue
                if mode == "storage_cancel":
                    marks.policy["maximum_marks_bytes"] = marks.bytes + FINAL_RESERVE
                    pending = asyncio.create_task(
                        marks.rpc(
                            session,
                            {"rpc_url": "https://offline.invalid"},
                            (),
                            "audit_after",
                            [],
                        )
                    )
                    await session.entered.wait()
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass
                    else:
                        raise AssertionError("storage_fallback_swallowed_cancellation")
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    assert rows[-1]["error_type"] == "CancelledError"
                    assert rows[-1]["storage_error"] == "MarksStorageCeiling"
                    assert marks.requests == marks.counts["rpc_failures"] == 1
                    continue
                now = time.monotonic()
                first = target(
                    str(Pubkey.new_unique()),
                    "C",
                    "trigger",
                    now - 11.96,
                    1,
                    now - 1.96,
                    1,
                )
                items = [first]
                if mode == "preparation_expiry":
                    items.append(
                        target(
                            str(Pubkey.new_unique()),
                            "C",
                            "trigger",
                            now - 10,
                            1,
                            now,
                            1,
                        )
                    )
                for item in items:
                    marks.add(item)
                if mode == "preparation_expiry":
                    with patch.object(
                        module,
                        "account_request",
                        lambda *args, cutoff=first["due_at"] + 2: build_after(
                            cutoff, *args
                        ),
                    ):
                        await marks.rpc(
                            session,
                            {"rpc_url": "https://offline.invalid"},
                            (),
                            "marks",
                            items,
                        )
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    assert marks.counts["miss"] == 1
                    assert all(
                        row["request_started_monotonic"] <= item["due_at"] + 2
                        for row in rows
                        if row["kind"] == "rpc"
                        for item in row["targets"]
                    )
                else:
                    await marks.worker(
                        session, {"rpc_url": "https://offline.invalid"}, ()
                    )
                    assert marks.counts["late_marks"] == 1 and not marks.pending
            finally:
                marks.stream.close()

        # Cancel the collector while it waits for an already-cancelled sibling.
        out = directory / "cleanup.jsonl"
        base_path, lock_path, provider_path = (
            directory / name for name in ("base.json", "lock.json", "provider.json")
        )
        base_path.write_text(
            json.dumps(
                {
                    "run_id": "cleanup",
                    "run_journal_path": str(out.with_suffix(".run.jsonl")),
                    "fee_input": {"config_digest": "offline"},
                    "capture": {
                        "warmup_seconds": 0,
                        "admission_seconds": 1,
                        "drain_seconds": 0,
                    },
                }
            )
        )
        lock = {
            "base_lock_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(),
            "source_sha256": {},
        }
        lock_path.write_text(json.dumps(lock))
        provider_path.write_text(
            json.dumps(
                {
                    "rpc_url": "https://offline.invalid",
                    "geyser_endpoint": "https://offline.invalid",
                    "geyser_token": "synthetic-token",
                }
            )
        )
        started, cleaning = asyncio.Event(), asyncio.Event()

        async def capture(*_args: object, **_kwargs: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await asyncio.Event().wait()
                raise

        async def fail_marker(*_args: object) -> None:
            await started.wait()
            raise CaptureRefused("synthetic_marker_failure")

        async def audit(*_args: object) -> tuple[dict, int, float]:
            return {"result": {"value": [None, None]}}, 0, time.monotonic()

        class OfflineSession:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def __aenter__(self) -> OfflineSession:
                return self

            async def __aexit__(self, *_args: object) -> bool:
                return False

        with (
            patch.object(scoring, "validate_marks_lock", return_value=lock),
            patch.object(base, "validate_lock", return_value=(object(), None)),
            patch.object(module, "fee_matches", return_value=True),
            patch.object(module, "native_account"),
            patch.object(
                module,
                "AccountRecorder",
                return_value=SimpleNamespace(
                    close=lambda: None, capture_terminal={"complete": False}
                ),
            ),
            patch.object(aiohttp, "ClientSession", OfflineSession),
            patch.object(Marks, "rpc", audit),
            patch.object(Marks, "worker", fail_marker),
            patch.object(lifecycle, "run", capture),
        ):
            pending = asyncio.create_task(
                collect(base_path, lock_path, provider_path, out)
            )
            async with asyncio.timeout(2):
                await cleaning.wait()
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("collector_cleanup_swallowed_cancellation")
        terminal = json.loads(
            out.with_suffix(".marks.jsonl").read_text().splitlines()[-1]
        )
        assert not terminal["complete"] and terminal["capture_exit_code"] == 130

    with TemporaryDirectory() as directory:
        asyncio.run(transport_check(Path(directory)))
        asyncio.run(retry_check(Path(directory)))
        asyncio.run(boundary_check(Path(directory)))
        check = Marks(
            Path(directory) / "marks.jsonl",
            {
                "run_id": "offline",
                "capture": {"warmup_seconds": 1, "admission_seconds": 20},
            },
            {"source_sha256": {}},
            b"{}",
            b"{}",
        )
        try:
            check.on_head({"slot": 10, "received_monotonic": 100.0})
            check.creation("warmup", 10, 100.5)
            assert not check.pending
            check.creation("mint", 10, 101.0)
            assert check.pending["mint:C:trigger"]["due_at"] == 111.0
            check.on_head({"slot": 11, "received_monotonic": 102.0})
            assert check.pending["mint:D2:trigger"]["entry_at"] is None
            check.on_head({"slot": 12, "received_monotonic": 103.0})
            assert check.pending["mint:D2:trigger"]["due_at"] == 113.0
            check.add(
                target("mint", "C", "exit", 101.0, 10, None, 15, 9),
                after=104.0,
                expires=109.0,
            )
            check.on_head({"slot": 15, "received_monotonic": 104.0})
            assert check.pending["mint:C:exit"]["due_at"] is None
            check.on_head({"slot": 15, "received_monotonic": 104.1})
            assert check.pending["mint:C:exit"]["due_at"] == 104.1
            recorder = AccountRecorder(
                Path(directory) / "capture.jsonl", check.lock, check
            )
            try:
                slot = {
                    "schema_version": 2,
                    "run_id": "offline",
                    "slot": 16,
                    "received_monotonic": time.monotonic(),
                    "gap_seconds": None,
                }
                recorder.emit("slots", slot)
                saved = json.loads(recorder.paths["slots"].read_text())
                assert "observed_monotonic" not in slot
                assert saved["received_monotonic"] == slot["received_monotonic"]
                assert saved["observed_monotonic"] >= slot["received_monotonic"]
                assert check.head == saved
            finally:
                for handle in recorder.files.values():
                    handle.close()
        finally:
            check.stream.close()
    assert not future_head({"slot": 12, "received_monotonic": 4.0}, 4.0, 12)
    assert not future_head({"slot": 11, "received_monotonic": 4.1}, 4.0, 12)
    assert future_head({"slot": 12, "received_monotonic": 4.1}, 4.0, 12)
    item = target("mint", "C", "trigger", 1.0, 7, 11.0, 7)
    assert (
        item["due_at"] - item["entry_at"] == 10
        and item["trigger_request_sequence"] is None
    )
    request = account_request(1, [FEE_ADDRESS], 7)
    assert request["method"] == "getMultipleAccounts" and request["params"][1] == {
        "encoding": "base64",
        "commitment": "processed",
        "minContextSlot": 7,
    }
    good = {
        "rpc_url": "https://rpc.invalid/credential-fragment",
        "geyser_endpoint": "https://stream.invalid",
        "geyser_token": "test-token",
    }
    _, secrets = provider_projection(good)
    assert contains_secret({"result": "test-token"}, secrets)
    for bad in (
        {**good, "private_key": "forbidden"},
        {**good, "rpc_url": "http://rpc.invalid"},
        {**good, "geyser_endpoint": "https://stream.invalid/path"},
    ):
        try:
            provider_projection(bad)
        except ValueError:
            continue
        raise AssertionError("unsafe_provider_projection_accepted")
    print("account capture self-check passed (offline)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("base-lock", "marks-lock", "provider-json", "out"):
        parser.add_argument("--" + option, type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if any(
        getattr(args, key) is None
        for key in ("base_lock", "marks_lock", "provider_json", "out")
    ):
        parser.error(
            "--base-lock, --marks-lock, --provider-json and --out are required"
        )
    try:
        return asyncio.run(
            collect(args.base_lock, args.marks_lock, args.provider_json, args.out)
        )
    except BaseException as exc:  # noqa: BLE001 - redact even terminal exception text
        # Never render arbitrary exception messages, provider bodies, URL or token.
        print(
            json.dumps({"complete": False, "error_type": type(exc).__name__}),
            file=sys.stderr,
        )
        return 130 if isinstance(exc, KeyboardInterrupt | asyncio.CancelledError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
