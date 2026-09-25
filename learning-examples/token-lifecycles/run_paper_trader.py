#!/usr/bin/env python3
"""Credential-free, continuously running SOL paper scenarios; never transactions.

Only public discovery and confirmed account reads are used. Account-derived fee
quotes are conditional: native fees and creator overrides are not attested.
"""

from __future__ import annotations

# Public wire-data errors share one boundary; raw units and existing decoder internals are intentional.
# ruff: noqa: E402, PLR2004, TRY004, TRY301, SLF001
import argparse
import asyncio
import binascii
import fcntl
import hashlib
import json
import math
import signal
import struct
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import aiohttp
from evaluate_creation_account_marks import account, mint_extensions
from evaluate_creation_paper import strict_json
from evaluate_online_paper import Mark, PaperBook, read_status
from solders.pubkey import Pubkey
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from core.pubkeys import WSOL_MINT
from monitoring.subscription import subscribe_pumpportal
from platforms.pumpfun.address_provider import PumpFunAddresses, PumpFunAddressProvider
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from platforms.pumpfun.fee_schedule import PumpFeeSnapshot, decode_fee_config_account
from utils.idl_parser import IDLParser

RPC_ENDPOINTS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
)
DISCOVERY_URL = "wss://pumpportal.fun/api/data"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
POLL_SECONDS = 2.0
MAX_READ_SECONDS = 2.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
BAD_ACCOUNT = (
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    OverflowError,
    struct.error,
    binascii.Error,
)


def utc() -> str:
    return datetime.now(UTC).isoformat()


def emit(kind: str, payload: dict) -> None:
    print(
        json.dumps(
            {"kind": kind, "utc": utc(), **payload}, allow_nan=False, sort_keys=True
        ),
        flush=True,
    )


class RpcReadError(Exception):
    """An observable failed public read, with a paced next-attempt bound."""

    def __init__(self, reason: str, cooldown: float = POLL_SECONDS) -> None:
        super().__init__(reason)
        self.cooldown = cooldown


class Runtime:
    """Free discovery plus qualified public account marks for the paper book."""

    def __init__(self, book: PaperBook, endpoint: str) -> None:
        if endpoint not in RPC_ENDPOINTS:
            raise ValueError("paper_rpc_endpoint_is_not_public_allowlisted")
        self.book = book
        self.endpoint = endpoint
        self.decoder = SimpleNamespace(
            _idl_parser=IDLParser(str(ROOT / "idl/pump_fun_idl.json"))
        )
        self.addresses = PumpFunAddressProvider()
        self.counts: Counter[str] = Counter()
        self.last_error: dict | None = None
        self.connected = False
        self.genesis_verified = False
        self.ready = False
        self.acknowledged = False
        self.last_slot = 0
        self.last_mark_at: float | None = None
        self.last_rpc_success_utc: str | None = None
        self.request_id = 0
        self.last_progress: tuple | None = None
        self.last_log: dict[str, float] = {}
        self.source_hashes = {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__).resolve(),
                Path(__file__).with_name("evaluate_online_paper.py").resolve(),
                Path(__file__)
                .with_name("evaluate_creation_account_marks.py")
                .resolve(),
                ROOT / "src/platforms/pumpfun/curve_manager.py",
                ROOT / "src/platforms/pumpfun/fee_schedule.py",
                ROOT / "idl/pump_fun_idl.json",
            )
        }

    def observation(self, *, running: bool = True) -> dict:
        return {
            "running": running,
            "discovery_connected": self.connected,
            "discovery_acknowledged": self.acknowledged,
            "genesis_verified": self.genesis_verified,
            "ready": self.ready,
            "endpoint": self.endpoint,
            "discovery_endpoint": DISCOVERY_URL,
            "commitment": "confirmed",
            "last_response_slot": self.last_slot,
            "last_mark_monotonic": self.last_mark_at,
            "last_rpc_success_utc": self.last_rpc_success_utc,
            "counts": dict(self.counts),
            "last_error": self.last_error,
            "observed_utc": utc(),
            "fee_model": "decoded_fee_config_scenario_unattested",
            "native_fee_attested": False,
            "creator_fee_override_verified": False,
            "freshness_rule": "strictly advancing RPC context slot; read latency <=2s; no independent head attestation",
            "source_sha256": self.source_hashes,
        }

    def publish(self, *, heartbeat: bool = False, running: bool = True) -> None:
        self.book.note("runtime", self.observation(running=running))
        status = self.book.status()
        progress = (
            status.get("cohorts_started"),
            status.get("closures"),
            status.get("policy_revision"),
            status.get("censored_cohorts"),
        )
        if heartbeat or progress != self.last_progress:
            emit(
                "heartbeat" if heartbeat else "paper_progress",
                {
                    key: status[key]
                    for key in (
                        "running",
                        "connected",
                        "paper_only",
                        "entries",
                        "cohorts_started",
                        "shadow_entries",
                        "selected_closed",
                        "unknown_inventory",
                        "paired_complete",
                        "censored_cohorts",
                        "policy_revision",
                        "policy_hold_seconds",
                        "policy_action",
                        "portfolio_entry_block",
                        "cash_lamports",
                        "reserved_lamports",
                        "known_paper_net_lamports",
                        "last_entry",
                        "last_cohort",
                        "last_closure",
                        "last_learning",
                    )
                },
            )
            self.last_progress = progress

    def problem(self, category: str, detail: str, **extra: object) -> None:
        self.counts[category] += 1
        self.last_error = {
            "category": category,
            "detail": detail[:300],
            "utc": utc(),
            **extra,
        }
        now = time.monotonic()
        # Bounded diagnostics; heartbeat retains exact counts between samples.
        if now - self.last_log.get(category, -math.inf) >= 15:
            self.last_log[category] = now
            self.book.note(category, self.last_error)
            emit("paper_observation", self.last_error)

    async def rpc(  # noqa: C901 - bounded transport plus wire qualification
        self, session: aiohttp.ClientSession, method: str, params: list
    ) -> tuple[object, dict]:
        if method not in ("getGenesisHash", "getMultipleAccounts"):
            raise ValueError("paper_rpc_method_is_not_read_only")
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }
        started = time.monotonic()
        self.counts["rpc_requests"] += 1
        try:
            async with session.post(
                self.endpoint, json=request, allow_redirects=False
            ) as response:
                if response.status != 200:
                    cooldown = POLL_SECONDS
                    if response.status == 429:
                        cooldown = 15.0
                        try:
                            cooldown = min(
                                60.0,
                                max(
                                    15.0,
                                    float(response.headers.get("Retry-After", "15")),
                                ),
                            )
                        except ValueError:
                            pass
                    raise RpcReadError(f"http_{response.status}", cooldown)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise RpcReadError("response_too_large")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RpcReadError(type(exc).__name__, 5.0) from exc
        received = time.monotonic()
        if (
            not all(math.isfinite(t) for t in (started, received))
            or not 0 <= received - started <= MAX_READ_SECONDS
        ):
            raise RpcReadError("rpc_read_latency")
        try:
            payload = strict_json(bytes(raw))
            if (
                not isinstance(payload, dict)
                or payload.get("jsonrpc") != "2.0"
                or type(payload.get("id")) is not int
                or payload["id"] != self.request_id
            ):
                raise ValueError("rpc_envelope")
            if payload.get("error") is not None:
                error = payload["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise RpcReadError(
                    f"rpc_error_{code}", 15.0 if code == 429 else POLL_SECONDS
                )
            result = payload["result"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise RpcReadError(f"malformed_response:{type(exc).__name__}") from exc
        self.last_rpc_success_utc = utc()
        return result, {
            "rpc_request": request,
            "rpc_endpoint": self.endpoint,
            "request_started_monotonic": started,
            "response_received_monotonic": received,
            "read_latency_seconds": received - started,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "observed_utc": self.last_rpc_success_utc,
            "genesis_hash": MAINNET_GENESIS,
            "commitment": "confirmed",
            "native_fee_attested": False,
            "creator_fee_override_verified": False,
            "fee_model": "decoded_fee_config_scenario_unattested",
            "source_sha256": self.source_hashes,
        }

    def discovery_frame(self, frame: str | bytes) -> None:
        self.counts["discovery_frames"] += 1
        try:
            data = strict_json(frame)
            if not isinstance(data, dict):
                raise ValueError("discovery_not_object")
            if data.get("method") == "newToken":
                params = data.get("params")
                if (
                    not isinstance(params, list)
                    or not params
                    or not isinstance(params[0], dict)
                ):
                    raise ValueError("new_token_params")
                data = params[0]
            mint = data.get("mint")
            if not isinstance(mint, str) or str(Pubkey.from_string(mint)) != mint:
                raise ValueError("discovery_mint")
        except BAD_ACCOUNT as exc:
            self.problem("discovery_invalid", str(exc))
            return
        # Feed supplies only the identifier, never a price, curve address or fee.
        admitted = self.book.discover(mint, time.monotonic())
        self.counts[
            "discoveries_admitted" if admitted else "discoveries_not_admitted"
        ] += 1

    async def discover(self) -> None:
        backoff = 1.0
        while True:
            connected_at = time.monotonic()
            try:
                async with connect(
                    DISCOVERY_URL,
                    proxy=None,
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=262144,
                    max_queue=32,
                ) as websocket:
                    result = await subscribe_pumpportal(
                        websocket, request_id=1, timeout=10
                    )
                    self.connected = self.acknowledged = True
                    self.counts["discovery_connections"] += 1
                    self.publish()
                    for frame in result.pending_frames:
                        self.discovery_frame(frame)
                    async for frame in websocket:
                        self.discovery_frame(frame)
                    raise ConnectionError("discovery_stream_ended")
            except (WebSocketException, OSError, TimeoutError, ConnectionError) as exc:
                self.connected = False
                self.problem(
                    "discovery_disconnect",
                    type(exc).__name__,
                    reconnect_after_seconds=backoff,
                )
                self.publish()
                if time.monotonic() - connected_at >= 30:
                    backoff = 1.0
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def make_mark(  # noqa: PLR0913 - explicit same-bank evidence inputs
        self,
        mint: str,
        curve: str,
        slot: int,
        values: list,
        offset: int,
        fees: PumpFeeSnapshot,
        proof: dict,
    ) -> Mark:
        curve_account = account(values[offset])
        mint_account = account(values[offset + 1])
        raw_curve = PumpFunCurveManager._validated_curve_data(
            curve_account, Pubkey.from_string(curve)
        )
        if len(raw_curve) < 83 or any(
            raw_curve[index] not in (0, 1) for index in (48, 81, 82)
        ):
            raise ValueError("noncanonical_curve_flags")
        state = PumpFunCurveManager._decode_curve_state_with_idl(
            self.decoder, raw_curve
        )
        if (
            state["quote_mint"] != WSOL_MINT
            or state["complete"]
            or state["is_mayhem_mode"]
        ):
            raise ValueError("unsupported_non_sol_complete_or_mayhem")
        supply, extensions = mint_extensions(mint_account, Pubkey.from_string(mint))
        if type(supply) is not int or not 0 < supply <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("invalid_mint_supply")
        if state["real_token_reserves"] > supply:
            raise ValueError("real_token_reserves_exceed_current_supply")
        primitive_state = {
            key: str(value) if isinstance(value, Pubkey) else value
            for key, value in state.items()
        }
        evidence = {
            **proof,
            "slot": slot,
            "curve_address": curve,
            "mint_address": mint,
            "mint_owner": str(mint_account.owner),
            "mint_extensions": extensions,
            "supply_raw": supply,
            "fee_digest": fees.config.digest,
            "fee_address": str(PumpFunAddresses.find_fee_config()),
            "fee_account": values[0],
            "curve_account": values[offset],
            "mint_account": values[offset + 1],
            "response_account_indexes": {"fee": 0, "curve": offset, "mint": offset + 1},
        }
        return Mark(
            mint=mint,
            slot=slot,
            observed_at=proof["response_received_monotonic"],
            state=primitive_state,
            fees=fees,
            supply_raw=supply,
            proof=evidence,
        )

    async def mark_loop(self, session: aiohttp.ClientSession) -> None:  # noqa: C901, PLR0912, PLR0915 - one paced batch admission boundary
        next_start = time.monotonic()
        while True:
            await asyncio.sleep(max(0, next_start - time.monotonic()))
            started = time.monotonic()
            next_start = started + POLL_SECONDS
            self.book.tick(started)
            mints = self.book.tracked_mints()
            if len(mints) > 12:
                raise RuntimeError("engine_tracking_capacity_exceeded")
            curves = [
                str(self.addresses.derive_pool_address(Pubkey.from_string(mint)))
                for mint in mints
            ]
            keys = [str(PumpFunAddresses.find_fee_config())]
            for curve, mint in zip(curves, mints, strict=True):
                keys.extend((curve, mint))
            try:
                result, proof = await self.rpc(
                    session,
                    "getMultipleAccounts",
                    [
                        keys,
                        {
                            "encoding": "base64",
                            "commitment": "confirmed",
                            "minContextSlot": self.last_slot + 1,
                        },
                    ],
                )
            except RpcReadError as exc:
                self.problem("rpc_missed_data", str(exc), cooldown_seconds=exc.cooldown)
                next_start = max(next_start, time.monotonic() + exc.cooldown)
                continue
            try:
                if not isinstance(result, dict) or not isinstance(
                    result.get("context"), dict
                ):
                    raise ValueError("missing_rpc_context")
                slot = result["context"]["slot"]
                values = result["value"]
                if (
                    type(slot) is not int
                    or slot <= self.last_slot
                    or slot > 0xFFFFFFFFFFFFFFFF
                ):
                    raise ValueError("nonadvancing_or_invalid_slot")
                if not isinstance(values, list) or len(values) != len(keys):
                    raise ValueError("wrong_account_batch_size")
                config = decode_fee_config_account(account(values[0]))
                fees = PumpFeeSnapshot(config, proof["response_received_monotonic"], 0)
            except BAD_ACCOUNT as exc:
                self.problem("account_batch_rejected", str(exc))
                continue
            self.last_slot = slot
            self.counts["fresh_account_batches"] += 1
            # Decode errors never catch book writes: persistence failures are fatal.
            for i, (mint, curve) in enumerate(zip(mints, curves, strict=True)):
                try:
                    mark = self.make_mark(
                        mint, curve, slot, values, 1 + 2 * i, fees, proof
                    )
                except BAD_ACCOUNT as exc:
                    self.problem("mark_rejected", str(exc), mint=mint)
                    continue
                before = self.book.status()["accepted_marks"]
                self.book.on_mark(mark)
                if self.book.status()["accepted_marks"] > before:
                    self.counts["accepted_marks"] += 1
                    self.last_mark_at = mark.observed_at
                    if self.acknowledged and not self.ready:
                        self.ready = True
                        self.publish()
                        print(
                            "PAPER_READY "
                            + json.dumps(
                                {
                                    "mint": mint,
                                    "slot": slot,
                                    "paper_only": True,
                                    "native_fee_attested": False,
                                }
                            ),
                            flush=True,
                        )
                else:
                    self.counts["engine_rejected_marks"] += 1
            self.publish_progress()

    def publish_progress(self) -> None:
        status = self.book.status()
        progress = (
            status.get("cohorts_started"),
            status.get("closures"),
            status.get("policy_revision"),
            status.get("censored_cohorts"),
        )
        if progress != self.last_progress:
            self.publish()

    async def heartbeat(self) -> None:
        last = -math.inf
        while True:
            now = time.monotonic()
            self.book.tick(now)
            if now - last >= 15:
                self.publish(heartbeat=True)
                last = now
            else:
                self.publish_progress()
            await asyncio.sleep(0.5)

    async def work(self) -> None:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=MAX_READ_SECONDS),
            auto_decompress=True,
            trust_env=False,
        ) as session:
            while not self.genesis_verified:
                try:
                    genesis, _ = await self.rpc(session, "getGenesisHash", [])
                except RpcReadError as exc:
                    self.problem(
                        "genesis_transport_error",
                        str(exc),
                        cooldown_seconds=exc.cooldown,
                    )
                    self.publish()
                    await asyncio.sleep(exc.cooldown)
                    continue
                if genesis != MAINNET_GENESIS:
                    raise RuntimeError("public_rpc_is_not_solana_mainnet")
                self.genesis_verified = True
            self.publish()
            async with asyncio.TaskGroup() as group:
                group.create_task(self.discover())
                group.create_task(self.mark_loop(session))
                group.create_task(self.heartbeat())


async def serve(book: PaperBook, endpoint: str, seconds: float | None) -> None:
    runtime = Runtime(book, endpoint)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    timer = loop.call_later(seconds, stop.set) if seconds is not None else None
    worker = asyncio.create_task(runtime.work())
    stopping = asyncio.create_task(stop.wait())
    primary: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            (worker, stopping), return_when=asyncio.FIRST_COMPLETED
        )
        if worker in done:
            await worker
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if timer is not None:
            timer.cancel()
        stopping.cancel()
        worker.cancel()
        results = await asyncio.gather(worker, stopping, return_exceptions=True)
        cleanup_error = next(
            (
                error
                for error in results
                if isinstance(error, BaseException)
                and not isinstance(error, asyncio.CancelledError)
            ),
            None,
        )
        try:
            runtime.connected = False
            runtime.publish(running=False)
        except BaseException as exc:  # noqa: BLE001 - retain primary failure while reporting shutdown failure
            if primary is not None:
                primary.add_note(f"shutdown_status_failure: {exc!r}")
            elif cleanup_error is None:
                cleanup_error = exc
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if primary is None and cleanup_error is not None:
            raise cleanup_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=ROOT / ".state/paper-trading/paper.sqlite3"
    )
    parser.add_argument("--rpc", choices=RPC_ENDPOINTS, default=RPC_ENDPOINTS[0])
    parser.add_argument(
        "--status",
        action="store_true",
        help="Read existing status without locking, writing or network access",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        help="Optional bounded run; otherwise run until SIGINT/SIGTERM",
    )
    args = parser.parse_args()
    if args.seconds is not None and (
        not math.isfinite(args.seconds) or args.seconds <= 0
    ):
        parser.error("--seconds must be finite and positive")
    if args.status:
        emit("paper_status", read_status(args.db))
        return
    path = args.db.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("paper_database_already_has_a_writer") from exc
        book = PaperBook(path)
        primary: BaseException | None = None
        try:
            asyncio.run(serve(book, args.rpc, args.seconds))
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                book.close()
            except BaseException as exc:
                if primary is None:
                    raise
                primary.add_note(f"paper_book_close_failure: {exc!r}")
        emit("paper_stopped", read_status(path))


if __name__ == "__main__":
    main()
