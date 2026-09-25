"""Bounded Geyser-triggered frozen SOL/mint quotes and unsigned native cycles only."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import re
import signal
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import aiohttp
import evaluate_solana_capital as capital
import grpc
import simulate_atomic_cycles as atomic
import simulate_orca_cycle as orca

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from src.geyser.generated import geyser_pb2, geyser_pb2_grpc

# Protocol values and the fixed experiment bounds remain visible at their checks.
# ruff: noqa: PLR2004

HTTP_LIMIT = 200
REQUEST_INTERVAL = 0.5
STREAM_TIMEOUT = 20.0
READINESS_SECONDS = 1.0
READINESS_PAUSE = 0.1
PAYER = "9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe"
METHODS = frozenset(
    {"getGenesisHash", "getMultipleAccounts", "getSlot", "simulateTransaction"}
)


def require(condition: bool, reason: str) -> None:  # noqa: FBT001
    if not condition:
        raise capital.ProbeError(reason)


def safe_reason(exc: BaseException) -> str:
    """Only local codes, never arbitrary messages or gRPC details."""
    allowed = {
        "simulation_below_slot_floor",
        "simulation_fee_mismatch",
        "simulation_balances_missing",
        "simulation_balance_shape",
        "simulation_rent_or_inventory_not_closed",
        "simulation_profit_violation",
        "signed_transaction_rejected",
        "wallet_baseline_changed",
        "wallet_baseline_required",
        "wallet_baseline_range",
        "simulation_wallet_guard_missing",
        "simulation_failed_fee_delta",
        "orca_tick_dependencies_changed",
        "orca_tick_range_unsupported",
        "unexpected_signer",
        "transaction_packet_limit",
        "cycle_pair",
        "cycle_quantity",
        "slot_wait_timeout",
        "snapshot_below_slot_floor",
    }
    if isinstance(exc, ValueError) and (
        str(exc) in allowed
        or re.fullmatch(
            r"(?:rpc_error_-?[0-9]{1,10}|orca_[a-z_]{1,64}|simulation_batch_[a-z_]{1,64})",
            str(exc),
        )
    ):
        return str(exc)
    return capital.safe_error(exc)


def credentials(value: object) -> dict[str, str]:
    require(isinstance(value, dict), "credentials_not_object")
    require(
        set(value) == {"rpc_url", "geyser_endpoint", "geyser_token"},
        "credentials_fields",
    )
    for item in value.values():
        require(
            isinstance(item, str)
            and bool(item)
            and not any(unicodedata.category(c).startswith("C") for c in item),
            "credentials_control_or_empty",
        )
    for name in ("rpc_url", "geyser_endpoint"):
        item = value[name]
        parsed = urlsplit(item)
        require(
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not any(c.isspace() for c in item)
            and not any(unicodedata.category(c).startswith("C") for c in unquote(item)),
            "credentials_tls_url",
        )
        require(parsed.port is None or 0 < parsed.port < 65536, "credentials_port")
        if name == "geyser_endpoint":
            require(parsed.path in ("", "/") and not parsed.query, "geyser_url_shape")
    require(value["geyser_token"].isascii(), "geyser_token_ascii")
    return value


def validate_scope(scope: object) -> dict:
    require(isinstance(scope, dict), "scope_not_object")
    mint = scope.get("mint")
    require(isinstance(mint, str) and mint != atomic.SOL, "scope_mint")
    atomic.Pubkey.from_string(mint)
    addresses = scope.get("pool_addresses")
    require(
        isinstance(addresses, list)
        and 2 <= len(addresses) <= 6
        and all(isinstance(address, str) for address in addresses)
        and len(set(addresses)) == len(addresses),
        "scope_pools",
    )
    for address in addresses:
        atomic.Pubkey.from_string(address)
    venue_mode = scope.get("venue_mode", "raydium")
    require(venue_mode in {"raydium", "fartcoin_amm_orca_native"}, "scope_venue_mode")
    if venue_mode == "fartcoin_amm_orca_native":
        require(
            mint == orca.MINT and set(addresses) == {orca.AMM_POOL, orca.WHIRLPOOL},
            "scope_orca_frozen_pair",
        )
    decimals = scope.get("expected_mint_decimals")
    require(
        decimals is None or (type(decimals) is int and 0 <= decimals <= 255),
        "scope_mint_decimals",
    )
    require(
        isinstance(scope.get("selection_provenance"), dict)
        and bool(scope["selection_provenance"]),
        "scope_selection_provenance",
    )
    frozen_at = scope.get("scope_frozen_at")
    require(isinstance(frozen_at, str), "scope_frozen_at")
    require(
        datetime.fromisoformat(frozen_at).utcoffset() is not None, "scope_frozen_at"
    )
    require(
        scope.get("input_cap_lamports") == atomic.BUY_LAMPORTS == 10_000_000,
        "scope_input_cap",
    )
    require(scope.get("configured_fee_lamports") == capital.FEE == 75_000, "scope_fee")
    require(
        type(scope.get("signed_transactions_allowed")) is int
        and scope["signed_transactions_allowed"] == 0
        and type(scope.get("submitted_transactions_allowed")) is int
        and scope["submitted_transactions_allowed"] == 0,
        "scope_read_only",
    )
    require(scope.get("payer") == PAYER, "scope_payer")
    return scope


def attest_mint_decimals(bank: dict, mint: str, expected: int | None) -> int:
    """Keep legacy SPL identity authoritative; token quantities remain raw integers."""
    require(mint != atomic.SOL, "market_identity")
    require(
        atomic.checked_data(bank[atomic.SOL], atomic.SPL, 82)[44] == 9, "sol_decimals"
    )
    actual = atomic.checked_data(bank[mint], atomic.SPL, 82)[44]
    require(expected is None or actual == expected, "mint_decimals")
    return actual


class Tape:
    """Exclusive JSONL and stdout share one credential redactor."""

    def __init__(self, out: TextIO, secrets: dict[str, str]) -> None:
        self.out = out
        self.secrets = tuple(secrets.values())
        self.identity: dict = {}

    def emit(self, event: str, **fields: object) -> None:
        text = json.dumps(
            {"event": event, "at": time.time(), **self.identity, **fields},
            allow_nan=False,
        )
        for secret in self.secrets:
            text = text.replace(json.dumps(secret)[1:-1], "[REDACTED]")
        text = re.sub(r"https?://[^\s\"<>]+", "[REDACTED_URL]", text)
        self.out.write(text + "\n")
        self.out.flush()
        print(text, flush=True)


def latency_stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered) if ordered else None,
        "p50": ordered[int((len(ordered) - 1) * 0.5)] if ordered else None,
        "p95": ordered[int((len(ordered) - 1) * 0.95)] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


class Pace:
    """One HTTP boundary: paced starts, full bodies, and a reserved final audit."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        http_budget: int = HTTP_LIMIT,
    ) -> None:
        require(
            type(http_budget) is int and 2 <= http_budget <= 3600, "http_budget_range"
        )
        self.http_budget = http_budget
        self.session, self.endpoint = session, endpoint
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.requests = 0
        self.auditing = False
        self.methods: Counter = Counter()
        self.wait_seconds: list[float] = []
        self.response_seconds: dict[str, list[float]] = defaultdict(list)
        self.incomplete: Counter = Counter()

    @asynccontextmanager
    async def post(
        self,
        url: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        payload = kwargs.get("json", {})
        batch = isinstance(payload, list)
        calls = payload if batch else [payload]
        require(not batch or len(calls) == 2, "rpc_batch_bound")
        methods = []
        ids = []
        for item in calls:
            require(isinstance(item, dict), "rpc_payload_shape")
            method = item.get("method")
            require(
                url == self.endpoint and method in METHODS, "rpc_method_not_read_only"
            )
            require(not batch or method == "simulateTransaction", "rpc_batch_method")
            require(
                not self.auditing or method == "getMultipleAccounts", "audit_method"
            )
            methods.append(method)
            if batch:
                require(
                    item.get("jsonrpc") == "2.0" and type(item.get("id")) is int,
                    "rpc_batch_identity",
                )
                ids.append(item["id"])
            if method == "simulateTransaction":
                options = item["params"][1]
                require(
                    options.get("sigVerify") is False
                    and options.get("replaceRecentBlockhash") is True,
                    "unsigned_simulation_only",
                )
                if batch:
                    require(
                        set(options)
                        == {
                            "encoding",
                            "sigVerify",
                            "replaceRecentBlockhash",
                            "commitment",
                            "minContextSlot",
                        },
                        "rpc_batch_options",
                    )
                    tx = atomic.Transaction.from_bytes(
                        base64.b64decode(item["params"][0], validate=True)
                    )
                    atomic.simulation_params(tx, options["minContextSlot"])
        require(not batch or sorted(ids) == [0, 1], "rpc_batch_identity")
        method = "simulateTransaction_batch" if batch else methods[0]
        entered = time.monotonic()
        async with self.lock:
            require(
                self.requests
                < (self.http_budget if self.auditing else self.http_budget - 1),
                "http_budget_exhausted",
            )
            await asyncio.sleep(
                max(0.0, self.last + REQUEST_INTERVAL - time.monotonic())
            )
            began = time.monotonic()
            self.wait_seconds.append(began - entered)
            self.last = began
            self.requests += 1
            self.methods.update(methods)
        kwargs["allow_redirects"] = False
        complete = False
        try:
            async with self.session.post(url, **kwargs) as response:
                await response.read()
                self.response_seconds[method].append(time.monotonic() - began)
                complete = True
                require(not 300 <= response.status < 400, "rpc_redirect_forbidden")
                yield response
        finally:
            if not complete:
                self.incomplete.update(methods)


@dataclass(frozen=True)
class Trigger:
    slot: int
    pubkey: str
    write_version: int
    first_notification_mono: float
    latest_notification_mono: float
    floor_slot: int


class Dirty:
    """One latest-wins signal, not a reserve cache; earliest receipt cannot move."""

    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.versions: dict[str, tuple[int, int, bytes]] = {}
        self.pending: Trigger | None = None
        self.event = asyncio.Event()
        self.counts: Counter = Counter()
        self.subscribed_mono: float | None = None
        self.max_slot = 0

    def update(  # noqa: PLR0913
        self,
        key: str,
        slot: int,
        version: int,
        data: bytes,
        now: float,
        *,
        startup: bool = False,
    ) -> bool:
        self.counts["account_notifications"] += 1
        require(key in self.keys, "geyser_unsubscribed_account")
        if startup:
            self.counts["startup_ignored"] += 1
            return False
        previous = self.versions.get(key)
        if previous is not None and (slot, version) <= previous[:2]:
            self.counts["duplicate_or_older_ignored"] += 1
            return False
        digest = hashlib.sha256(data).digest()
        self.versions[key] = (slot, version, digest)
        self.max_slot = max(self.max_slot, slot)
        if previous is not None and previous[2] == digest:
            self.counts["identical_data_ignored"] += 1
            if self.pending is not None:
                self.pending = replace(self.pending, floor_slot=self.max_slot)
            return False
        first = now
        if self.pending is not None:
            first = self.pending.first_notification_mono
            self.counts["coalesced"] += 1
        self.pending = Trigger(slot, key, version, first, now, self.max_slot)
        self.counts["changed_triggers"] += 1
        self.event.set()
        return True

    async def take(self) -> Trigger:
        await self.event.wait()
        pending = self.pending
        require(pending is not None, "dirty_signal_missing")
        self.pending = None
        self.event.clear()
        self.counts["consumed"] += 1
        return pending


async def geyser(creds: dict[str, str], dirty: Dirty, tape: Tape) -> None:
    """Canonical protobuf/TLS account+slot stream; no reconnect, no raw error logs."""
    parsed = urlsplit(creds["geyser_endpoint"])
    host = parsed.hostname
    target = (
        f"[{host}]:{parsed.port or 443}"
        if ":" in host
        else f"{host}:{parsed.port or 443}"
    )
    async with grpc.aio.secure_channel(
        target, grpc.ssl_channel_credentials(), options=(("grpc.enable_http_proxy", 0),)
    ) as channel:
        call = geyser_pb2_grpc.GeyserStub(channel).Subscribe(
            metadata=(("x-token", creds["geyser_token"]),)
        )
        try:
            await asyncio.wait_for(
                call.write(
                    geyser_pb2.SubscribeRequest(
                        accounts={
                            "routes": geyser_pb2.SubscribeRequestFilterAccounts(
                                account=sorted(dirty.keys)
                            )
                        },
                        slots={
                            "statuses": geyser_pb2.SubscribeRequestFilterSlots(
                                filter_by_commitment=False
                            )
                        },
                        commitment=geyser_pb2.PROCESSED,
                    )
                ),
                STREAM_TIMEOUT,
            )
            dirty.subscribed_mono = time.monotonic()
            tape.emit(
                "stream_subscribed",
                subscription_accounts=len(dirty.keys),
                commitment="processed",
            )
            last_real = time.monotonic()
            while True:
                remaining = STREAM_TIMEOUT - (time.monotonic() - last_real)
                require(remaining > 0, "geyser_real_update_timeout")
                try:
                    message = await asyncio.wait_for(call.read(), remaining)
                except TimeoutError:
                    raise capital.ProbeError("geyser_real_update_timeout") from None
                require(message is not grpc.aio.EOF, "geyser_eof")
                kind = message.WhichOneof("update_oneof")
                if kind == "ping":
                    dirty.counts["pings"] += 1
                    remaining = STREAM_TIMEOUT - (time.monotonic() - last_real)
                    require(remaining > 0, "geyser_real_update_timeout")
                    await asyncio.wait_for(
                        call.write(
                            geyser_pb2.SubscribeRequest(
                                ping=geyser_pb2.SubscribeRequestPing(id=1)
                            )
                        ),
                        remaining,
                    )
                elif kind == "slot":
                    update = message.slot
                    dirty.counts[f"slot_status_{update.status}"] += 1
                    if update.status == geyser_pb2.DEAD:
                        tape.emit(
                            "dead_slot",
                            slot=update.slot,
                            reason="processed_fork_dead_fail_closed",
                        )
                        raise capital.ProbeError("geyser_dead_slot")
                    last_real = time.monotonic()
                elif kind == "account":
                    update = message.account
                    info = update.account
                    now = time.monotonic()
                    dirty.update(
                        str(atomic.Pubkey.from_bytes(info.pubkey)),
                        update.slot,
                        info.write_version,
                        info.data,
                        now,
                        startup=update.is_startup,
                    )
                    if not update.is_startup:
                        last_real = now
        finally:
            call.cancel()


def simulation_fields(row: dict) -> dict:
    """Retain numeric instruction errors, but discard arbitrary program/transport text."""
    fields = {
        name: row.get(name)
        for name in (
            "decision_slot",
            "simulation_slot",
            "target_delay",
            "actual_delay",
            "fee",
            "units",
            "net_lamports",
            "elapsed_seconds",
        )
    }
    error = row.get("err")
    fields["err"] = None if error is None else {"kind": "simulation_error"}
    if isinstance(error, dict):
        instruction = error.get("InstructionError")
        if (
            isinstance(instruction, list)
            and len(instruction) == 2
            and type(instruction[0]) is int
        ):
            fields["err"]["instruction"] = instruction[0]
            detail = instruction[1]
            if isinstance(detail, dict) and type(detail.get("Custom")) is int:
                fields["err"]["custom"] = detail["Custom"]
    return fields


class Observer:
    def __init__(  # noqa: PLR0913
        self,
        client: Pace,
        scope: dict,
        pools: list,
        keys: list[str],
        dirty: Dirty,
        tape: Tape,
    ) -> None:
        self.client, self.scope, self.pools, self.keys = client, scope, pools, keys
        self.dirty, self.tape = dirty, tape
        self.payer = atomic.Pubkey.from_string(scope["payer"])
        self.counts: Counter = Counter()
        self.latencies: dict[str, list[float]] = defaultdict(list)
        self.mint_decimals = scope.get("expected_mint_decimals")
        self.active_episodes: set[str] = set()
        self.active_quote_episodes: set[str] = set()
        self.expected_routes = len(pools) * (len(pools) - 1)
        self.last_complete_mono: float | None = None
        self.first_complete_mono: float | None = None
        self.best_margin: int | None = None
        self.native = scope.get("venue_mode") == "fartcoin_amm_orca_native"
        self.initial_balance: int | None = None

    def eligibility(
        self, sequence: int, net: int | None, *, complete: bool, layer: str
    ) -> None:
        """One shared-capital episode; missing/partial negatives never reset it."""
        active = (
            self.active_episodes if layer == "immediate" else self.active_quote_episodes
        )
        was_active = self.scope["mint"] in active
        opened = capital.episode(
            active,
            self.scope["mint"],
            None if net is None else net - atomic.PROFIT_LAMPORTS + 1,
            complete=complete,
        )
        is_active = self.scope["mint"] in active
        self.counts[f"{layer}_opportunity_episodes"] += opened
        self.counts[f"{layer}_episodes_closed"] += int(was_active and not is_active)
        self.tape.emit(
            "eligibility",
            sequence=sequence,
            layer=layer,
            observation_complete=complete,
            eligible=None if net is None else net >= atomic.PROFIT_LAMPORTS,
            episode_opened=bool(opened),
            episode_active=is_active,
            episode_closed=was_active and not is_active,
            opportunity_episodes=self.counts[f"{layer}_opportunity_episodes"],
        )

    async def snapshot(self, trigger: Trigger) -> tuple[int, dict] | None:
        deadline = trigger.first_notification_mono + READINESS_SECONDS
        attempts = 0
        readiness = asyncio.timeout_at(deadline)
        try:
            require(time.monotonic() < deadline, "rpc_readiness_expired")
            async with readiness:
                while True:
                    attempts += 1
                    self.counts["snapshot_attempts"] += 1
                    try:
                        result = await atomic.bank_read(
                            self.client,
                            self.client.endpoint,
                            self.keys,
                            minimum_slot=trigger.floor_slot,
                        )
                        require(
                            result[0] >= trigger.floor_slot, "snapshot_precedes_signal"
                        )
                    except ValueError as exc:
                        if str(exc) != "rpc_error_-32016":
                            raise
                        self.counts["slot_not_ready_attempts"] += 1
                        await asyncio.sleep(READINESS_PAUSE)
                    else:
                        return result
        except (TimeoutError, capital.ProbeError) as exc:
            if not readiness.expired() and str(exc) != "rpc_readiness_expired":
                raise
            self.counts["missing_readiness_samples"] += 1
            self.tape.emit(
                "missing_sample",
                reason="rpc_readiness_expired",
                signal=asdict(trigger),
                attempts=attempts,
                readiness_deadline_mono=deadline,
            )
            return None

    async def native_scan(  # noqa: C901, PLR0915 - one deadline-bound native evidence frame
        self, trigger: Trigger, sequence: int, slot: int, bank: dict
    ) -> None:
        """Use this observer's signal/deadline/lifecycle, with native venue pricing."""
        deadline = trigger.first_notification_mono + READINESS_SECONDS
        self.counts["evaluations"] += 1
        self.counts["expected_routes"] += self.expected_routes
        self.counts["native_states"] += 1
        self.counts["partial_quote_states"] += 1
        self.tape.emit(
            "native_snapshot",
            sequence=sequence,
            slot=slot,
            signal=asdict(trigger),
            accounts=bank,
            provisional=True,
            fork_status="unverified",
        )
        wallet = bank[str(self.payer)]
        require(
            wallet is not None
            and wallet["owner"] == "11111111111111111111111111111111"
            and not wallet["executable"]
            and wallet["lamports"] == self.initial_balance,
            "wallet_baseline_changed",
        )
        require(
            all(
                bank[
                    str(
                        atomic.get_associated_token_address(
                            self.payer, atomic.Pubkey.from_string(mint)
                        )
                    )
                ]
                is None
                for mint in (atomic.SOL, self.scope["mint"])
            ),
            "simulation_rent_or_inventory_not_closed",
        )
        self.mint_decimals = attest_mint_decimals(
            bank, self.scope["mint"], self.mint_decimals
        )
        try:
            live = [
                orca.hydrate(pool, bank)
                if isinstance(pool, orca.Whirlpool)
                else atomic.hydrate_pool(pool, bank)
                for pool in self.pools
            ]
        except ValueError as exc:
            if str(exc) not in {
                "orca_tick_dependencies_changed",
                "orca_tick_range_unsupported",
            }:
                raise
            self.counts["native_dependency_gaps"] += 1
            self.tape.emit(
                "missing_sample",
                sequence=sequence,
                signal=asdict(trigger),
                reason=str(exc),
                coverage_complete=False,
            )
            self.eligibility(sequence, None, complete=False, layer="immediate")
            return
        amm = next(pool for pool in live if pool.program == atomic.AMM)
        whirlpool = next(pool for pool in live if pool.program == orca.PROGRAM)
        # ponytail: one AMM-sized quantity is nonexhaustive; sizing search needs a
        # separately authorized observation budget, not a longer readiness window.
        quantity = amm.quote(atomic.SOL, atomic.BUY_LAMPORTS)
        self.tape.emit(
            "evaluation",
            sequence=sequence,
            signal=asdict(trigger),
            slot=slot,
            provisional=True,
            fork_status="unverified",
            quoted_routes=0,
            expected_routes=2,
            coverage_complete=False,
            pricing="native_cycle_only",
            first_notification_fresh=time.monotonic() < deadline,
            best_margin_above_floor=None,
            best_quoted_net_lamports=None,
            quantity=quantity,
            quantity_source="same_bank_amm_exact_input_at_cap",
            sizing_complete=False,
            attested_mint_decimals=self.mint_decimals,
            pool_evidence=[capital.pool_evidence(amm), asdict(whirlpool)],
        )
        self.eligibility(sequence, None, complete=False, layer="quote")
        if quantity <= 0:
            self.counts["native_quantity_gaps"] += 1
            self.tape.emit(
                "immediate_missing",
                sequence=sequence,
                reason="zero_rounded_candidate_quantity",
                coverage_complete=False,
            )
            self.eligibility(sequence, None, complete=False, layer="immediate")
            return
        directions = [(amm, whirlpool), (whirlpool, amm)]
        # Rotate even when the provider/deadline returns only one direction.
        if (sequence - 1) % 2:
            directions.reverse()
        transactions = [
            atomic.build_cycle(
                buy, sell, self.payer, quantity, initial_balance=self.initial_balance
            )
            for buy, sell in directions
        ]
        for batch_id, (tx, (buy, sell)) in enumerate(
            zip(transactions, directions, strict=True)
        ):
            frozen = bytes(tx)
            self.tape.emit(
                "candidate",
                sequence=sequence,
                batch_id=batch_id,
                slot=slot,
                buy=buy.address,
                sell=sell.address,
                quantity=quantity,
                margin_above_floor=None,
                pricing="native_cycle_only",
                initial_wallet_lamports=self.initial_balance,
                transaction_sha256=hashlib.sha256(frozen).hexdigest(),
                unsigned_transaction_base64=base64.b64encode(frozen).decode(),
            )
        rows = []
        readiness = asyncio.timeout_at(deadline)
        try:
            require(time.monotonic() < deadline, "rpc_readiness_expired")
            async with readiness:
                self.counts["immediate_attempts"] += 2
                self.counts["native_batch_attempts"] += 1
                rows = await atomic.simulate_cycle_batch(
                    self.client,
                    self.client.endpoint,
                    transactions,
                    slot,
                    self.payer,
                    self.initial_balance,
                    record_result=lambda result: self.tape.emit(
                        "native_batch_response", sequence=sequence, raw_response=result
                    ),
                )
        except (TimeoutError, capital.ProbeError) as exc:
            if not readiness.expired() and str(exc) != "rpc_readiness_expired":
                raise
            self.counts["missing_immediate_samples"] += 2
            self.tape.emit(
                "immediate_missing",
                sequence=sequence,
                signal=asdict(trigger),
                reason="rpc_readiness_expired",
                readiness_deadline_mono=deadline,
                coverage_complete=False,
            )
        ended = time.monotonic()
        fresh = ended < deadline
        successes = []
        classified_slots = []
        for batch_id, row in enumerate(rows):
            tx, (buy, sell) = transactions[batch_id], directions[batch_id]
            returned = row.get("simulation_slot") is not None
            eligible = returned and row["err"] is None and fresh
            classified = (
                returned
                and (row["err"] is None or row.get("guard_rejected", False))
                and fresh
            )
            self.counts["immediate_simulations"] += int(returned)
            self.counts["immediate_successes"] += int(eligible)
            self.counts["immediate_rejections"] += int(
                returned and row["err"] is not None
            )
            self.counts["missing_immediate_samples"] += int(not returned or not fresh)
            self.counts["late_native_results"] += int(returned and not fresh)
            if eligible:
                successes.append(row["net_lamports"])
            if classified:
                classified_slots.append(row["simulation_slot"])
            self.tape.emit(
                "immediate" if returned and fresh else "immediate_missing",
                sequence=sequence,
                batch_id=batch_id,
                signal=asdict(trigger),
                buy=buy.address,
                sell=sell.address,
                transaction_sha256=hashlib.sha256(bytes(tx)).hexdigest(),
                unsigned_transaction_base64=base64.b64encode(bytes(tx)).decode(),
                readiness_deadline_mono=deadline,
                provisional=True,
                fork_status="unverified",
                first_notification_to_immediate_result_seconds=ended
                - trigger.first_notification_mono,
                status="success"
                if eligible
                else "rejected"
                if returned and fresh
                else "missing",
                gap=None
                if classified
                else "unclassified_native_error"
                if returned and fresh
                else "native_result_unavailable_or_late",
                guard_rejected=row.get("guard_rejected", False),
                **simulation_fields(row),
            )
        complete = (
            len(classified_slots) == 2 and len(set(classified_slots)) == 1 and fresh
        )
        self.counts["complete_native_states"] += int(complete)
        self.counts["partial_native_states"] += int(not complete)
        if len(classified_slots) == 2 and len(set(classified_slots)) != 1:
            self.counts["native_bank_mismatch_states"] += 1
        best_net = max(successes) if successes else None
        if best_net is not None:
            margin = best_net - atomic.PROFIT_LAMPORTS
            self.best_margin = (
                margin if self.best_margin is None else max(margin, self.best_margin)
            )
        self.latencies["first_notification_to_immediate_result_seconds"].append(
            ended - trigger.first_notification_mono
        )
        self.tape.emit(
            "native_coverage",
            sequence=sequence,
            coverage_complete=complete,
            classified_routes=len(classified_slots),
            expected_routes=2,
            same_returned_bank=len(classified_slots) == 2
            and len(set(classified_slots)) == 1,
            best_native_net_lamports=best_net,
            sizing_complete=False,
            economic_floor_rejected=complete and not successes,
        )
        # Zero here is only a boolean floor-ineligible sentinel from two native
        # final guards, never a quoted price or an invented realized cashflow.
        self.eligibility(
            sequence,
            best_net if successes else 0 if complete else None,
            complete=complete,
            layer="immediate",
        )
        self.counts["delayed_diagnostics_not_scheduled"] += len(rows)

    async def scan(self, trigger: Trigger) -> None:  # noqa: C901, PLR0912, PLR0915
        self.counts["scans_started"] += 1
        sequence = self.counts["scans_started"]
        began = time.monotonic()
        snapshot = await self.snapshot(trigger)
        if snapshot is None:
            return
        slot, bank = snapshot
        quote_started = time.monotonic()
        if self.native:
            await self.native_scan(trigger, sequence, slot, bank)
            return
        live = [atomic.hydrate_pool(pool, bank) for pool in self.pools]
        decimals = attest_mint_decimals(bank, self.scope["mint"], self.mint_decimals)
        self.mint_decimals = decimals
        quotes = [
            row
            for row in atomic.quote_pairs(live, self.scope["mint"])
            if row[1] > 0 and row[0] + atomic.MIN_OUTPUT > 0
        ]
        best = max(quotes, key=lambda row: row[0]) if quotes else None
        margin = best[0] if best else None
        evaluated = time.monotonic()
        fresh = evaluated < trigger.first_notification_mono + READINESS_SECONDS
        complete = len(quotes) == self.expected_routes and fresh
        self.counts["evaluations"] += 1
        self.counts["quoted_routes"] += len(quotes)
        self.counts["expected_routes"] += self.expected_routes
        self.counts["complete_quote_states"] += int(complete)
        self.counts["partial_quote_states"] += int(not complete)
        self.counts["positive_quote_states"] += int(margin is not None and margin >= 0)
        if complete:
            self.last_complete_mono = evaluated
            if self.first_complete_mono is None:
                self.first_complete_mono = evaluated
        if margin is not None:
            self.best_margin = (
                margin if self.best_margin is None else max(margin, self.best_margin)
            )
        timing = {
            "snapshot_seconds": quote_started - began,
            "quote_seconds": evaluated - quote_started,
            "first_notification_to_evaluation_seconds": evaluated
            - trigger.first_notification_mono,
            "latest_notification_to_evaluation_seconds": evaluated
            - trigger.latest_notification_mono,
        }
        for name, value in timing.items():
            self.latencies[name].append(value)
        self.tape.emit(
            "evaluation",
            sequence=sequence,
            signal=asdict(trigger),
            slot=slot,
            provisional=True,
            fork_status="unverified",
            **timing,
            quoted_routes=len(quotes),
            expected_routes=self.expected_routes,
            missing_or_zero_rounded_routes=self.expected_routes - len(quotes),
            coverage_complete=complete,
            first_notification_fresh=fresh,
            attested_mint_decimals=decimals,
            best_margin_above_floor=margin,
            best_quoted_net_lamports=margin + atomic.PROFIT_LAMPORTS
            if margin is not None
            else None,
            pool_evidence=[capital.pool_evidence(pool) for pool in live],
        )
        net = margin + atomic.PROFIT_LAMPORTS if margin is not None and fresh else None
        self.eligibility(sequence, net, complete=complete, layer="quote")
        if complete and margin is not None and margin < 0:
            self.counts["complete_loss_of_quote_eligibility"] += 1
            self.eligibility(sequence, 0, complete=True, layer="immediate")
        if best is None or margin < 0 or not fresh:
            self.counts["immediate_not_selected"] += 1
            self.tape.emit(
                "immediate_not_selected",
                sequence=sequence,
                reason="no_positive_fresh_route",
                coverage_complete=complete,
            )
            return
        margin, quantity, buy, sell = best
        self.counts["positive_candidates"] += 1
        selection = "candidate"
        tx = atomic.build_cycle(buy, sell, self.payer, quantity)
        frozen = bytes(tx)
        digest = hashlib.sha256(frozen).hexdigest()
        encoded = base64.b64encode(frozen).decode("ascii")
        self.tape.emit(
            selection,
            sequence=sequence,
            slot=slot,
            buy=buy.address,
            sell=sell.address,
            quantity=quantity,
            margin_above_floor=margin,
            transaction_sha256=digest,
            unsigned_transaction_base64=encoded,
        )
        immediate_started = time.monotonic()
        deadline = trigger.first_notification_mono + READINESS_SECONDS
        readiness = asyncio.timeout_at(deadline)
        attempts = not_ready_attempts = 0
        row = None
        try:
            require(immediate_started < deadline, "rpc_readiness_expired")
            async with readiness:
                while True:
                    attempts += 1
                    self.counts["immediate_attempts"] += 1
                    try:
                        # Only provider bank availability can retry. Returned on-chain
                        # errors are final; the atomic helper itself never waits.
                        row = await atomic.simulate_cycle(
                            self.client, self.client.endpoint, tx, slot, self.payer
                        )
                        break
                    except ValueError as exc:
                        if str(exc) != "rpc_error_-32016":
                            raise
                        not_ready_attempts += 1
                        self.counts["immediate_slot_not_ready_attempts"] += 1
                        await asyncio.sleep(READINESS_PAUSE)
        except (TimeoutError, capital.ProbeError) as exc:
            if not readiness.expired() and str(exc) != "rpc_readiness_expired":
                self.tape.emit(
                    "immediate_incomplete",
                    sequence=sequence,
                    selection=selection,
                    transaction_sha256=digest,
                    reason=safe_reason(exc),
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - immediate_started,
                )
                raise
            self.counts["missing_immediate_samples"] += 1
        except BaseException as exc:
            self.tape.emit(
                "immediate_incomplete",
                sequence=sequence,
                selection=selection,
                transaction_sha256=digest,
                reason=safe_reason(exc),
                attempts=attempts,
                elapsed_seconds=time.monotonic() - immediate_started,
            )
            raise
        ended = time.monotonic()
        if row is not None and ended >= deadline:
            self.tape.emit(
                "late_native_result",
                sequence=sequence,
                transaction_sha256=digest,
                reason="first_notification_deadline_elapsed",
                **simulation_fields(row),
            )
            self.counts["late_native_results"] += 1
            self.counts["missing_immediate_samples"] += 1
            row = None
        timing = {
            "first_notification_to_immediate_result_seconds": ended
            - trigger.first_notification_mono,
            "latest_notification_to_immediate_result_seconds": ended
            - trigger.latest_notification_mono,
            "immediate_readiness_elapsed_seconds": ended - immediate_started,
        }
        for name, value in timing.items():
            self.latencies[name].append(value)
        self.counts["immediate_simulations"] += int(row is not None)
        fields = (
            simulation_fields(row)
            if row is not None
            else {"reason": "rpc_readiness_expired", "status": "missing"}
        )
        if row is not None:
            fields["status"] = "success" if row["err"] is None else "rejected"
        self.tape.emit(
            "immediate" if row is not None else "immediate_missing",
            sequence=sequence,
            selection=selection,
            signal=asdict(trigger),
            transaction_sha256=digest,
            unsigned_transaction_base64=encoded,
            attempts=attempts,
            slot_not_ready_attempts=not_ready_attempts,
            readiness_deadline_mono=deadline,
            provisional=True,
            fork_status="unverified",
            **timing,
            **fields,
        )
        eligible = row is not None and row["err"] is None
        self.counts["immediate_successes"] += int(eligible)
        self.counts["immediate_rejections"] += int(row is not None and not eligible)
        # A rejected best route does not disprove eligibility of other positive routes.
        # Only a complete fresh all-negative quote bank closes a shared episode.
        self.eligibility(
            sequence,
            row["net_lamports"] if eligible else None,
            complete=False,
            layer="immediate",
        )
        self.counts["delayed_diagnostics_not_scheduled"] += 1

    async def run(self) -> None:
        while True:
            trigger = await self.dirty.take()
            try:
                await self.scan(trigger)
            except BaseException as exc:
                self.counts["incomplete_scans"] += 1
                self.tape.emit(
                    "evaluation_incomplete",
                    signal=asdict(trigger),
                    reason=safe_reason(exc),
                )
                raise


async def run(  # noqa: C901, PLR0912, PLR0915
    args: argparse.Namespace, creds: dict[str, str], scope: dict, tape: Tape
) -> bool:
    started = time.monotonic()
    validate_scope(scope)
    require(
        math.isfinite(args.seconds) and 0 < args.seconds <= 1800, "seconds_out_of_range"
    )
    require(
        type(args.http_budget) is int and 2 <= args.http_budget <= 3600,
        "http_budget_range",
    )
    tape.identity = {
        "chain": "solana",
        "source": "geyser_same_bank_native_cycle",
        "source_sha256": {
            Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in (__file__, atomic.__file__, capital.__file__, orca.__file__)
        },
        "scope_sha256": hashlib.sha256(
            json.dumps(
                scope, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest(),
        "mint": scope["mint"],
        "pool_addresses": scope["pool_addresses"],
        "signed_transactions": 0,
        "submitted_transactions": 0,
    }
    tape.emit(
        "observation_scope",
        scope=scope,
        observation_window_seconds=args.seconds,
        http_request_limit=args.http_budget,
        minimum_request_start_interval_seconds=REQUEST_INTERVAL,
        final_audit_reserved_requests=1,
        delayed_diagnostics_enabled=False,
    )
    payer = atomic.Pubkey.from_string(scope["payer"])
    wallet_keys = [
        str(payer),
        *[
            str(
                atomic.get_associated_token_address(
                    payer, atomic.Pubkey.from_string(mint)
                )
            )
            for mint in (atomic.SOL, scope["mint"])
        ],
    ]
    initial = None
    wallet_verified = False
    observer = None
    dirty = None
    tasks = []
    failures = []
    cancellation = None
    stop_reason = "bootstrap_incomplete"
    observation_ended = started
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
        installed.append(sig)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10), trust_env=False
    ) as session:
        client = Pace(session, creds["rpc_url"], args.http_budget)
        observation_timeout = asyncio.timeout(args.seconds)
        try:
            # The configured bound includes bootstrap. The reserved wallet audit has
            # its own final ten-second transport bound after workers are stopped.
            async with observation_timeout:
                async with client.post(
                    creds["rpc_url"],
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getGenesisHash",
                        "params": [],
                    },
                ) as response:
                    identity = await response.json()
                    require(
                        response.status == 200
                        and "error" not in identity
                        and identity.get("result") == capital.MAINNET_GENESIS,
                        "network_identity",
                    )
                _, initial = await atomic.bank_read(
                    client, creds["rpc_url"], wallet_keys
                )
                wallet = initial[wallet_keys[0]]
                require(
                    wallet is not None
                    and wallet["owner"] == "11111111111111111111111111111111"
                    and not wallet["executable"]
                    and wallet["lamports"] > atomic.BUY_LAMPORTS + capital.FEE
                    and all(initial[key] is None for key in wallet_keys[1:]),
                    "simulation_wallet_state",
                )
                _, bank = await atomic.bank_read(
                    client, creds["rpc_url"], scope["pool_addresses"]
                )
                pools = [
                    orca.decode(
                        address,
                        bank[address],
                        expected_mints=(atomic.SOL, scope["mint"]),
                    )
                    if scope.get("venue_mode") == "fartcoin_amm_orca_native"
                    and address == orca.WHIRLPOOL
                    else atomic.decode_pool(address, bank[address])
                    for address in scope["pool_addresses"]
                ]
                require(
                    all(
                        set(pool.mints) == {atomic.SOL, scope["mint"]} for pool in pools
                    ),
                    "market_identity",
                )
                native = scope.get("venue_mode") == "fartcoin_amm_orca_native"
                if native:
                    require(
                        next(
                            pool for pool in pools if pool.address == orca.AMM_POOL
                        ).program
                        == atomic.AMM,
                        "scope_amm_program",
                    )
                    whirlpool = next(
                        pool for pool in pools if isinstance(pool, orca.Whirlpool)
                    )
                    require(
                        whirlpool.fee_tier != whirlpool.spacing,
                        "orca_adaptive_scope_changed",
                    )
                keys = list(
                    dict.fromkeys(
                        [
                            atomic.CLOCK,
                            *(wallet_keys if native else []),
                            *[key for pool in pools for key in pool.dependencies()],
                        ]
                    )
                )
                _, bank = await atomic.bank_read(client, creds["rpc_url"], keys)
                for pool in pools:
                    orca.hydrate(pool, bank) if isinstance(
                        pool, orca.Whirlpool
                    ) else atomic.hydrate_pool(pool, bank)
                decimals = attest_mint_decimals(
                    bank, scope["mint"], scope.get("expected_mint_decimals")
                )
                dirty = Dirty(set(keys) - {atomic.CLOCK})
                observer = Observer(client, scope, pools, keys, dirty, tape)
                observer.mint_decimals = decimals
                observer.initial_balance = wallet["lamports"]
                tape.identity["attested_mint_decimals"] = decimals
                tape.emit(
                    "probe_ready",
                    marker="PROBE_READY",
                    seconds=args.seconds,
                    payer=str(payer),
                    pools=scope["pool_addresses"],
                    mint=scope["mint"],
                    attested_mint_decimals=decimals,
                    expected_mint_decimals=scope.get("expected_mint_decimals"),
                    selection_provenance=scope["selection_provenance"],
                    scope_frozen_at=scope["scope_frozen_at"],
                    expected_ordered_routes=observer.expected_routes,
                    input_lamports=atomic.BUY_LAMPORTS,
                    modeled_cost_lamports=capital.FEE,
                    profit_floor_lamports=atomic.PROFIT_LAMPORTS,
                    initial_wallet_lamports=wallet["lamports"],
                    venue_mode=scope.get("venue_mode", "raydium"),
                    native_batch_methods=2 if native else 0,
                    sizing_complete=False if native else None,
                    subscription_accounts=sorted(dirty.keys),
                    minimum_request_start_interval_seconds=REQUEST_INTERVAL,
                    maximum_http_requests=args.http_budget,
                    readiness_seconds=READINESS_SECONDS,
                    readiness_retry_pause_seconds=READINESS_PAUSE,
                )
                stream = asyncio.create_task(geyser(creds, dirty, tape))
                worker = asyncio.create_task(observer.run())
                stopping = asyncio.create_task(stop.wait())
                tasks = [stream, worker, stopping]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if stream in done:
                    await stream
                    raise capital.ProbeError("geyser_ended")  # noqa: TRY301
                require(stopping not in done, "signal_interrupted")
                await worker
                raise capital.ProbeError("observer_ended")  # noqa: TRY301
        except TimeoutError as exc:
            stop_reason = (
                "deadline" if observation_timeout.expired() else "operation_timeout"
            )
            if not observation_timeout.expired():
                failures.append({"phase": "observation", "reason": safe_reason(exc)})
                tape.emit("fatal", phase="observation", reason="operation_timeout")
        except BaseException as exc:  # noqa: BLE001 - audit before returning failure
            stop_reason = safe_reason(exc)
            failures.append({"phase": "observation", "reason": stop_reason})
            tape.emit("fatal", phase="observation", reason=stop_reason)
            if isinstance(exc, asyncio.CancelledError):
                cancellation = exc
        finally:
            observation_ended = time.monotonic()
            cancelled = set()
            for task in tasks:
                if not task.done():
                    cancelled.add(task)
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results, strict=True):
                if isinstance(result, BaseException) and not (
                    task in cancelled and isinstance(result, asyncio.CancelledError)
                ):
                    failure = {
                        "phase": "worker_shutdown",
                        "reason": safe_reason(result),
                    }
                    if failure not in failures:
                        failures.append(failure)
                        tape.emit("fatal", **failure)
            if dirty is not None and dirty.pending is not None:
                tape.emit(
                    "missing_sample",
                    reason="observation_ended_with_pending_notification",
                    signal=asdict(dirty.pending),
                    stop_reason=stop_reason,
                )
            client.auditing = True
            try:
                _, final = await atomic.bank_read(client, creds["rpc_url"], wallet_keys)
                require(initial is not None, "wallet_baseline_unavailable")
                # Ignore provider metadata such as rentEpoch; compare actual wallet
                # ownership, executable flag, bytes and lamports plus ATA absence.
                before, after = initial[wallet_keys[0]], final[wallet_keys[0]]
                require(
                    before is not None
                    and after is not None
                    and all(
                        after.get(key) == before.get(key)
                        for key in ("owner", "executable", "data", "lamports")
                    )
                    and all(
                        initial[key] is None and final[key] is None
                        for key in wallet_keys[1:]
                    ),
                    "wallet_changed_during_probe",
                )
                wallet_verified = True
                tape.emit(
                    "wallet_audit",
                    unchanged=True,
                    temporary_accounts_absent=True,
                    initial_lamports=before["lamports"],
                    final_lamports=after["lamports"],
                )
            except BaseException as exc:  # noqa: BLE001 - preserve the failed final audit
                failure = {"phase": "wallet_audit", "reason": safe_reason(exc)}
                failures.append(failure)
                tape.emit("wallet_audit", unchanged=False, reason=failure["reason"])
                if isinstance(exc, asyncio.CancelledError):
                    cancellation = exc
            for sig in installed:
                loop.remove_signal_handler(sig)
    success = (
        not failures
        and wallet_verified
        and observer is not None
        and stop_reason == "deadline"
    )
    tape.emit(
        "summary",
        status="completed" if success else "failed",
        stop_reason=stop_reason,
        failures=failures,
        total_runtime_seconds=time.monotonic() - started,
        configured_observation_window_seconds=args.seconds,
        observation_runtime_seconds=observation_ended - started,
        warm_observation_seconds=(
            max(0.0, observation_ended - dirty.subscribed_mono)
            if dirty is not None and dirty.subscribed_mono is not None
            else 0.0
        ),
        first_complete_quote_offset_seconds=(
            observer.first_complete_mono - started
            if observer is not None and observer.first_complete_mono is not None
            else None
        ),
        last_complete_quote_offset_seconds=(
            observer.last_complete_mono - started
            if observer is not None and observer.last_complete_mono is not None
            else None
        ),
        expected_ordered_routes=observer.expected_routes if observer else None,
        immediate_episode_active_at_end=bool(observer.active_episodes)
        if observer
        else False,
        quote_episode_active_at_end=bool(observer.active_quote_episodes)
        if observer
        else False,
        wallet_verified=wallet_verified,
        signed_transactions=0,
        submitted_transactions=0,
        http_requests=client.requests,
        http_request_limit=args.http_budget,
        http_methods=dict(client.methods),
        http_incomplete=dict(client.incomplete),
        pacing_wait_seconds=latency_stats(client.wait_seconds),
        http_full_response_seconds={
            key: latency_stats(values)
            for key, values in client.response_seconds.items()
        },
        counts=dict(observer.counts) if observer else {},
        best_margin_above_floor=observer.best_margin if observer else None,
        latency_seconds={
            key: latency_stats(values) for key, values in observer.latencies.items()
        }
        if observer
        else {},
        stream_counts=dict(dirty.counts) if dirty else {},
        stream_pending_at_end=dirty.pending is not None if dirty else False,
        limitations=[
            "Unsigned simulation only; no signing, submissions, fills or live expectancy evidence.",
            "One frozen legacy-SPL SOL/mint market: 2..6 AMM-v4/CPMM pools or the explicit FARTCOIN AMM-v4/Whirlpool pair; fixed 0.01 SOL input and 75000 lamport modeled costs.",
            "Processed observations are provisional; minContextSlot is a floor, not proof of exact write/fork inclusion.",
            "Canonical bundled stubs lack interslot_updates; provider defaults may omit DEAD notifications.",
            "One coalesced pending signal and earliest-receipt readiness expiry censor samples, never count them as negative.",
            "Raydium mode simulates only its best positive quote; native Orca mode simulates both deterministic directions as one bounded batch at one AMM-quoted candidate quantity, not an exhaustive sizing search.",
            "Missing/partial/stale results never close episodes. Orca negatives require two classified final-wallet guard rejections at one returned bank; other native errors remain unknown.",
            "Orca tick/adaptive-fee math runs only in the deployed native program. Three directional arrays, frozen subscribed dependency range, legacy SPL only; array-range changes are censored, not losses.",
            "Batch HTTP is one paced request but two requested methods; equal returned slots are necessary, not proof of exact fork identity.",
            "No delayed diagnostics are scheduled, avoiding competition with immediate work on the shared paced HTTP boundary.",
            "Deadline cancels in-flight work; warm time is subscription duration, not continuous complete-route or native coverage.",
            "Native nets exclude cloud/provider costs, inclusion competition and paid failed-inclusion costs; they are not fills or daily income.",
            "Wallet admission does not attest temporary rent funding; native simulation rejects insufficient funding without overrides.",
            "The observation bound includes bootstrap; final reserved wallet audit adds up to ten seconds plus pacing.",
        ],
    )
    if cancellation is not None:
        raise cancellation
    return success


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--http-budget", type=int, default=HTTP_LIMIT)
    args = parser.parse_args()
    try:
        require(
            math.isfinite(args.seconds) and 0 < args.seconds <= 1800,
            "seconds_out_of_range",
        )
        require(2 <= args.http_budget <= 3600, "http_budget_range")
        require(
            args.credentials.suffix == ".json" and args.scope.suffix == ".json",
            "json_inputs_required",
        )
        for path in (args.credentials, args.scope):
            resolved = path.resolve()
            require(
                not any(
                    part in {".env", ".env~", "ENVDATA", ".state"}
                    for part in resolved.parts
                ),
                "sensitive_input_path_forbidden",
            )
        require(
            args.output.suffix == ".jsonl"
            and args.output.resolve()
            not in {args.credentials.resolve(), args.scope.resolve()},
            "output_path_invalid",
        )
        creds = credentials(json.loads(args.credentials.read_text(encoding="utf-8")))
        scope = validate_scope(json.loads(args.scope.read_text(encoding="utf-8")))
        with args.output.open("x", encoding="utf-8") as out:
            success = asyncio.run(run(args, creds, scope, Tape(out, creds)))
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as exc:
        print(json.dumps({"event": "fatal", "reason": safe_reason(exc)}), flush=True)
        raise SystemExit(1) from None
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
