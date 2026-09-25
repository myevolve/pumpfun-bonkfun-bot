"""Offline, fresh-process durable recovery evidence; no RPC or signing.

Run: uv run learning-examples/verify_durable_position_recovery.py
Pump.fun startup refusal: add --platform pump_fun (no native attestation fabricated).
Each child runs in a disposable directory with an explicit source import path.
The LIVE policy exists only inside this synthetic process to exercise real
recovery gates. No user configuration, wallet, or state is loaded.
"""

# Real callback signatures and late imports preserve the isolated transport guard.
# Synthetic boundary values and failures are deliberate, not runtime fallbacks.
# ruff: noqa: ARG001, ARG002, PLR2004, PLC0415, SLF001, TRY003, BLE001

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
PHASES = ("seed", "unknown_outage", "reverted_retry", "reopen_retry")
PUMP_PHASES = (
    "seed",
    "fee_account_unavailable",
    "fee_attestation_unavailable",
    "reopen_attestation_unavailable",
)


def require(condition: bool, behavior: str) -> None:  # noqa: FBT001
    if not condition:
        raise AssertionError(behavior)


def deny_network(event: str, args: tuple) -> None:
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.bind",
    }:
        raise RuntimeError("offline recovery probe forbids network access")


async def child(phase: str, platform_name: str = "lets_bonk") -> dict:  # noqa: C901, PLR0912, PLR0915
    work = Path.cwd().resolve()
    require(
        str(work) == os.environ.get("RECOVERY_PROBE_ROOT")
        and work.name.startswith("durable-recovery-")
        and work.is_relative_to(Path(tempfile.gettempdir()).resolve())
        and (work / "synthetic-only").read_text() == "offline recovery fixture\n",
        "child must run inside the parent-created disposable state directory",
    )
    sys.addaudithook(deny_network)
    sys.path.insert(0, str(ROOT / "src"))
    logging.disable(logging.CRITICAL)

    from solders.account import Account
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import Transaction

    from core.client import RpcUnavailableError
    from core.execution_policy import ExecutionMode, ExecutionPolicy, TradeLimitExceeded
    from core.pubkeys import WSOL_MINT, SystemAddresses
    from core.transaction_ledger import TransactionLedger
    from core.transaction_state import TransactionOutcome, TransactionStatus
    from interfaces.core import Platform, TokenInfo
    from platforms.pumpfun.address_provider import PumpFunAddresses
    from platforms.pumpfun.fee_schedule import (
        FEE_CONFIG_DISCRIMINATOR,
        GET_FEES_DISCRIMINATOR,
        _FeeAttestationError,
        _FeeAttestationUnavailable,
        decode_fee_config_account,
    )
    from trading.position import ExitReason, Position
    from trading.universal_trader import UniversalTrader

    platform = Platform(platform_name)
    pump = platform is Platform.PUMP_FUN
    quantity_raw = 250_000 * 10**6 if pump else 1_000_000
    quote_raw = 13_000_000 if pump else 100_000
    # The small synthetic session fee budget remains exhausted by two 5k
    # reservations; this is not the production session or its authorization.
    fixture_path = work / "synthetic-fixture.json"
    if phase == "seed":
        key = Keypair()
        fixture_path.write_text(
            json.dumps(
                {
                    "synthetic_private_key": str(key),
                    "wallet": str(key.pubkey()),
                    "mint": str(Pubkey.new_unique()),
                    # Opaque signature-shaped identifiers, NOT signatures of any wire.
                    "buy_signature": str(Signature.from_bytes(bytes([11]) * 64)),
                    "sell_signature": str(Signature.from_bytes(bytes([12]) * 64)),
                }
            )
        )
        fixture_path.chmod(0o600)
    fixture = json.loads(fixture_path.read_text())
    wallet = fixture["wallet"]
    mint = fixture["mint"]
    buy_signature = fixture["buy_signature"]
    sell_signature = fixture["sell_signature"]
    original_intent = f"sell:{buy_signature}:1"
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        live_authorized=True,
        expected_wallet=wallet,
        max_trade_quote_raw=quote_raw,
        max_total_fee_lamports=250_000 if pump else 5_000,
        risk_session_id="synthetic-recovery-only",
        max_session_quote_raw=quote_raw,
        max_session_fee_lamports=10_000,
    )
    journal = work / "positions.json"
    ledger_path = work / "transactions.sqlite3"
    trader = UniversalTrader(
        rpc_endpoint="http://offline.invalid",
        wss_endpoint="ws://offline.invalid",
        private_key=fixture["synthetic_private_key"],
        buy_amount=quote_raw / 10**9,
        buy_slippage=0.3 if pump else 0.1,
        sell_slippage=0.3 if pump else 0.1,
        platform=platform,
        listener_type="blocks",
        extreme_fast_token_amount=250_000 if pump else 30,
        allowed_quote_mints=["sol"] if pump else None,
        fixed_priority_fee=200_000,
        compute_units={"buy": 140_000, "sell": 110_000} if pump else None,
        execution_policy=policy,
        exit_strategy="tp_sl",
        stop_loss_percentage=0.5,
        price_check_interval=1,
        max_exit_sell_attempts=1,
        position_journal_path=journal,
        transaction_ledger_path=ledger_path,
    )
    ledger = trader.transaction_ledger
    require(ledger is not None, "real LIVE constructor opens durable SQLite ledger")
    calls = {
        "buy": 0,
        "listener": 0,
        "sell": 0,
        "submission": 0,
        "confirm": 0,
        "receipt": 0,
        "price": 0,
        "pool_read": 0,
        "fee_account": 0,
        "fee_decode": 0,
        "blockhash": 0,
        "attestation_request": 0,
        "signing": 0,
        "queue": 0,
        "reconciliation": 0,
        "monitor": 0,
        "prepare": 0,
        "submission_recovery": 0,
    }
    observed_retries = []
    reads_done = asyncio.Event()
    startup_refusal = None
    readiness_refusals = {}
    fee_fixture_digest = None

    def ledger_state() -> dict:
        # Read exact durable rows, including reservations, rather than only totals.
        with sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True) as connection:
            return {
                table: [
                    list(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608 - fixed table names
                    )
                ]
                for table in (
                    "intents",
                    "submissions",
                    "outcomes",
                    "risk_reservations",
                    "operation_intents",
                )
            }

    async def forbidden_queue() -> NoReturn:
        calls["queue"] += 1
        raise AssertionError("unattested startup attempted queue processing")

    async def forbidden_reconciliation() -> NoReturn:
        calls["reconciliation"] += 1
        raise AssertionError("unattested startup attempted buy reconciliation")

    async def forbidden_monitor(*args: object, **kwargs: object) -> NoReturn:
        calls["monitor"] += 1
        raise AssertionError("unattested startup attempted position monitoring")

    def forbidden_keypair(_wallet: object) -> NoReturn:
        calls["signing"] += 1
        raise AssertionError("offline probe forbids access to signing material")

    # A synthetic key is needed by the real constructor, never by execution.
    type(trader.wallet).keypair = property(forbidden_keypair)

    async def forbidden_buy(*args: object, **kwargs: object) -> NoReturn:
        calls["buy"] += 1
        raise AssertionError("resume-only attempted a new buy")

    async def forbidden_listener(*args: object, **kwargs: object) -> NoReturn:
        calls["listener"] += 1
        raise AssertionError("resume-only attempted a listener")

    async def forbidden_submission(*args: object, **kwargs: object) -> NoReturn:
        calls["submission"] += 1
        raise AssertionError("offline probe blocked submission before signing/network")

    # Only low-level transport is replaced. Confirmation, expiry proof, ledger
    # recovery, journal hydration, monitoring, and seller.execute remain real.
    class ReadOnlyTransport:
        async def confirm_transaction(
            self, signature: Signature, **kwargs: object
        ) -> NoReturn:
            require(
                str(signature) == sell_signature,
                "confirmation uses exact old signature",
            )
            calls["confirm"] += 1
            raise RpcUnavailableError("synthetic confirmation transport outage")

        async def get_block_height(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(value=10)  # Still valid: absence is NOT expiry.

        async def get_account_info(
            self, pubkey: Pubkey, **kwargs: object
        ) -> SimpleNamespace:
            nonlocal fee_fixture_digest
            if pump:
                require(
                    pubkey == PumpFunAddresses.find_fee_config(),
                    "startup must read the canonical Pump FeeConfig first",
                )
                calls["fee_account"] += 1
                if phase == "fee_account_unavailable":
                    raise RpcUnavailableError("synthetic startup fee-account outage")
                # Same public schema/values as tests/test_pumpfun_fee_schedule.py
                # _fee_account; bytes are locally constructed, NOT a chain capture.
                data = bytearray(FEE_CONFIG_DISCRIMINATOR)
                data += bytes([253]) + bytes(Pubkey.default())
                data += struct.pack("<QQQ", 25, 90, 20)
                for tiers in (
                    ((0, (0, 95, 30)), (100_000, (0, 80, 20))),
                    ((0, (0, 50, 10)),),
                ):
                    data += struct.pack("<I", len(tiers))
                    for threshold, fees in tiers:
                        data += threshold.to_bytes(16, "little")
                        data += struct.pack("<QQQ", *fees)
                data += struct.pack("<QQQ", 0, 95, 30) + bytes(128)
                account = Account(
                    lamports=1,
                    data=bytes(data),
                    owner=PumpFunAddresses.FEE_PROGRAM,
                    executable=False,
                    rent_epoch=0,
                )
                config = decode_fee_config_account(account)
                calls["fee_decode"] += 1
                fee_fixture_digest = config.digest
                require(
                    len(config.regular_tiers) == 2
                    and len(config.stable_tiers) == 1
                    and config.regular_tiers[0].fees.protocol_fee_bps == 95,
                    "real decoder accepts the explicit synthetic fee fixture",
                )
                return SimpleNamespace(value=account)
            calls["pool_read"] += 1
            raise RpcUnavailableError("synthetic pool transport outage")

        async def get_latest_blockhash(self, **kwargs: object) -> SimpleNamespace:
            require(pump, "only the Pump attestation boundary needs a blockhash")
            calls["blockhash"] += 1
            return SimpleNamespace(
                value=SimpleNamespace(
                    blockhash=Hash.default(), last_valid_block_height=100
                )
            )

        async def get_multiple_accounts(
            self, *args: object, **kwargs: object
        ) -> NoReturn:
            calls["pool_read"] += 1
            raise RpcUnavailableError("synthetic pool transport outage")

        async def send_raw_transaction(
            self, *args: object, **kwargs: object
        ) -> NoReturn:
            return await forbidden_submission(*args, **kwargs)

        async def close(self) -> None:
            pass

    async def rpc(body: dict, *args: object, **kwargs: object) -> dict | None:
        method = body["method"]
        if method == "simulateTransaction":
            require(pump and phase != "fee_account_unavailable", "attestation only")
            calls["attestation_request"] += 1
            transaction = Transaction.from_bytes(base64.b64decode(body["params"][0]))
            require(
                transaction.signatures == [Signature.default()]
                and body["params"][1]["sigVerify"] is False
                and body["params"][1]["replaceRecentBlockhash"] is True,
                "production attestation request must remain unsigned",
            )
            instruction = transaction.message.instructions[0]
            require(
                len(transaction.message.instructions) == 1
                and transaction.message.account_keys[instruction.program_id_index]
                == PumpFunAddresses.FEE_PROGRAM
                and bytes(instruction.data).startswith(GET_FEES_DISCRIMINATOR),
                "only the read-only get_fees instruction may reach this boundary",
            )
            # Never return invented program logs. No request leaves this process,
            # no native simulation executes, and no wire bytes enter the report.
            return None
        if method == "getHealth":
            raise RpcUnavailableError("synthetic warm-up transport outage")
        if method == "getTransaction":
            require(
                body["params"][0] == sell_signature,
                "receipt lookup retains exact signature",
            )
            calls["receipt"] += 1
            if calls["receipt"] >= 2:
                reads_done.set()
            if phase == "reverted_retry":
                # Synthetic canonical envelope; real parser and outcome rules
                # consume meta.err and persist REVERTED into the real ledger.
                return {
                    "result": {
                        "slot": 20,
                        "transaction": {
                            "signatures": [sell_signature],
                            "message": {"accountKeys": [wallet]},
                        },
                        "meta": {
                            "err": {"InstructionError": [0, {"Custom": 6003}]},
                            "fee": 5_000,
                        },
                    }
                }
            raise RpcUnavailableError("synthetic transaction-history outage")
        if method in {"getAccountInfo", "getMultipleAccounts"}:
            calls["pool_read"] += 1
            raise RpcUnavailableError("synthetic pool transport outage")
        raise AssertionError(f"unexpected offline RPC method: {method}")

    async def price(*args: object, **kwargs: object) -> float:
        calls["price"] += 1
        require(phase == "reverted_retry", "unknown sell must bypass price gates")
        return 0.00004  # Synthetic price, below the stored stop-loss threshold.

    real_sell = trader.seller.execute

    async def observe_sell(token: TokenInfo, *args: object, **kwargs: object) -> object:
        calls["sell"] += 1
        require(phase == "reverted_retry", "unknown sell cannot create another sell")
        persisted = json.loads(journal.read_text())["positions"][mint]["position"]
        intent = kwargs["intent_id"]
        require(
            intent == f"sell:{buy_signature}:2",
            "retry allocates fresh monotonic intent",
        )
        require(
            persisted["pending_exit_intent_id"] == intent,
            "fresh retry intent is durable before real seller executes",
        )
        require(
            persisted["pending_exit_signature"] is None,
            "fresh retry never reuses unknown signature",
        )
        result = await real_sell(token, *args, **kwargs)
        require(
            not result.success and not result.unresolved,
            "real seller fails closed when authoritative pool remains unavailable",
        )
        require(
            result.tx_signature is None,
            "failed read cannot invent a new submitted wire",
        )
        observed_retries.append(
            {
                "intent_id": intent,
                "status": "blocked_before_wire",
                "error": result.error_message,
            }
        )
        trader._shutdown_event.set()
        return result

    trader.solana_client._client = ReadOnlyTransport()
    trader.solana_client.post_rpc = rpc
    trader.solana_client.build_and_send_transaction = forbidden_submission
    trader.buyer.execute = forbidden_buy
    trader.token_listener.listen_for_tokens = forbidden_listener
    trader.seller.execute = observe_sell
    trader.platform_implementations.curve_manager.calculate_price = price
    if pump:
        real_prepare = (
            trader.platform_implementations.curve_manager.prepare_live_execution
        )
        real_submission_recovery = trader._resume_ledger_bound_submissions

        async def observe_prepare() -> None:
            calls["prepare"] += 1
            await real_prepare()

        async def observe_submission_recovery() -> None:
            calls["submission_recovery"] += 1
            await real_submission_recovery()

        trader.platform_implementations.curve_manager.prepare_live_execution = (
            observe_prepare
        )
        trader._resume_ledger_bound_submissions = observe_submission_recovery
        trader._process_token_queue = forbidden_queue
        trader._reconcile_unresolved_buys = forbidden_reconciliation
        trader._monitor_position_until_exit = forbidden_monitor

    def reserve(intent: str, signature: str, quote_raw: int) -> str:
        policy.validate_wallet(Pubkey.from_string(wallet))
        policy.validate_budgets(quote_raw, 5_000)
        digest = hashlib.sha256(intent.encode()).hexdigest()
        ledger.record_intent(intent, wallet, quote_raw, 5_000, digest)
        return ledger.record_submission(
            intent,
            signature,
            str(Hash.default()),
            100,
            state="submitted",
            quote_mint=str(WSOL_MINT),
            risk_session_id=policy.risk_session_id,
            max_session_quote_raw=policy.max_session_quote_raw,
            max_session_fee_lamports=policy.max_session_fee_lamports,
            intent_message_hash=digest,
        )

    def check_accounting(db: TransactionLedger) -> dict:
        totals = asdict(db.get_session_risk_totals(policy.risk_session_id, wallet))
        require(
            totals
            == {
                "quote_amount_raw_by_mint": {str(WSOL_MINT): quote_raw},
                "fee_lamports": 10_000,
                "submission_count": 2,
            },
            "restart must neither reset nor double-reserve cumulative exposure",
        )
        return totals

    started = False
    try:
        if phase == "seed":
            token = TokenInfo(
                name="Synthetic recovery fixture",
                symbol="SYNTH",
                uri="",
                mint=Pubkey.from_string(mint),
                platform=platform,
                pool_state=(
                    trader.platform_implementations.address_provider.derive_pool_address(
                        Pubkey.from_string(mint), WSOL_MINT
                    )
                    if not pump
                    else None
                ),
                bonding_curve=(
                    trader.platform_implementations.address_provider.derive_pool_address(
                        Pubkey.from_string(mint)
                    )
                    if pump
                    else None
                ),
                token_program_id=SystemAddresses.TOKEN_PROGRAM,
                quote_mint=WSOL_MINT,
                quote_token_program_id=SystemAddresses.TOKEN_PROGRAM,
                base_decimals=6,
                quote_decimals=9,
            )
            reserve(f"buy:{platform.value}:{mint}", buy_signature, quote_raw)
            ledger.record_outcome(
                TransactionOutcome(TransactionStatus.SUCCESS, buy_signature, slot=1)
            )
            position = Position.create_from_buy_result(
                token.mint,
                token.symbol,
                quote_raw / 10**9 / (quantity_raw / 10**6),
                quantity_raw / 10**6,
                stop_loss_percentage=0.5,
                quantity_raw=quantity_raw,
                quote_amount_raw=quote_raw,
                buy_fee_lamports=5_000,
                account_balance_baseline_raw=17,
                position_id=buy_signature,
            )
            require(position.next_exit_attempt() == 1, "first exit sequence")
            position.mark_exit_intent(original_intent, ExitReason.STOP_LOSS, 0.00004)
            trader._persist_position(token, position)
            reserve(original_intent, sell_signature, 0)
            ledger.record_outcome(
                TransactionOutcome(
                    TransactionStatus.UNKNOWN,
                    sell_signature,
                    error="synthetic send outage",
                )
            )
            # Simulate crash after ledger commit and before signature journal update.
            require(
                position.pending_exit_signature is None, "seed has crash-window journal"
            )
            if pump:
                (work / "seed-ledger.json").write_text(json.dumps(ledger_state()))
        else:
            token, position = trader._active_positions[mint]
            require(
                position.is_active
                and position.quantity_raw == quantity_raw
                and position.account_balance_baseline_raw == 17
                and position.quote_amount_raw == quote_raw,
                "source inventory, preexisting baseline, and exposure survive reopening",
            )
            check_accounting(ledger)
            if pump:
                require(
                    ledger_state()
                    == json.loads((work / "seed-ledger.json").read_text()),
                    "every seeded ledger row and exact reservation survives fresh-process hydration",
                )
            if phase != "reopen_retry":
                require(
                    position.pending_exit_intent_id == original_intent
                    and position.pending_exit_signature == sell_signature,
                    "real constructor hydrates exact ledger-bound pending intent/signature",
                )
                if not pump:
                    require(
                        reserve(original_intent, sell_signature, 0) == sell_signature,
                        "same durable submission replay is idempotent",
                    )
                    check_accounting(ledger)
                    # Prove exhausted risk remains enforced in the reopened ledger.
                    try:
                        reserve(
                            "synthetic-over-budget",
                            str(Signature.from_bytes(bytes([13]) * 64)),
                            0,
                        )
                    except TradeLimitExceeded:
                        pass
                    else:
                        raise AssertionError(
                            "reopened session incorrectly permits excess fees"
                        )
                    check_accounting(ledger)
                started = True
                if pump:
                    before_position = position.to_dict()
                    started_at = asyncio.get_running_loop().time()
                    try:
                        await asyncio.wait_for(
                            trader.start(resume_only=True), timeout=20
                        )
                    except RpcUnavailableError as exc:
                        require(
                            (
                                phase == "fee_account_unavailable"
                                and type(exc) is RpcUnavailableError
                            )
                            or (
                                phase != "fee_account_unavailable"
                                and isinstance(exc, _FeeAttestationUnavailable)
                            ),
                            "refusal must come from the real expected startup attestation gate",
                        )
                        startup_refusal = {
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "elapsed_seconds": asyncio.get_running_loop().time()
                            - started_at,
                            "deadline_seconds": 20,
                        }
                    else:
                        raise AssertionError(
                            "unavailable native attestation incorrectly permitted startup"
                        )
                    require(
                        position.to_dict() == before_position,
                        "startup refusal leaves hydrated position untouched",
                    )
                    manager = trader.platform_implementations.curve_manager
                    for venue, schedule in (
                        ("pump_fun", manager.fee_schedule),
                        ("pumpswap", manager.pumpswap.fee_schedule),
                    ):
                        try:
                            schedule.require_snapshot()
                        except _FeeAttestationError as exc:
                            readiness_refusals[venue] = str(exc)
                        else:
                            raise AssertionError(
                                "offline fixture incorrectly promoted an attested snapshot"
                            )
                else:
                    run = asyncio.create_task(trader.start(resume_only=True))

                    async def stop_after_unknown_reads() -> None:
                        await reads_done.wait()
                        trader._shutdown_event.set()

                    stopper = (
                        asyncio.create_task(stop_after_unknown_reads())
                        if phase == "unknown_outage"
                        else None
                    )
                    try:
                        await asyncio.wait_for(run, timeout=20)
                    finally:
                        if stopper is not None:
                            stopper.cancel()
                            await asyncio.gather(stopper, return_exceptions=True)
            else:
                require(
                    position.pending_exit_signature is None
                    and position.pending_exit_intent_id is None
                    and position.exit_attempt_sequence == 2
                    and position.charged_exit_fee_lamports == 5_000,
                    "terminal fee and bounded failed retry persist exactly once",
                )
        if not started:
            await trader._cleanup_resources()
        with TransactionLedger(ledger_path) as reopened:
            totals = check_accounting(reopened)
            outcome = reopened.get_outcome(sell_signature)
            expected = (
                TransactionStatus.REVERTED
                if phase in {"reverted_retry", "reopen_retry"}
                else TransactionStatus.UNKNOWN
            )
            require(
                outcome is not None and outcome.status is expected,
                "real reconciliation outcome survives SQLite close/reopen",
            )
        persisted = json.loads(journal.read_text())["positions"][mint]["position"]
        require(
            persisted["quantity_raw"] == quantity_raw and persisted["is_active"],
            "unliquidated source inventory remains durable after shutdown",
        )
        require(
            calls["buy"]
            == calls["listener"]
            == calls["submission"]
            == calls["signing"]
            == 0,
            "no buy, listener, signing/submission path is permitted",
        )
        if phase == "unknown_outage":
            require(
                calls["receipt"] >= 2 and calls["sell"] == calls["price"] == 0,
                "repeated unknown reads retain position without rebuilding exit",
            )
            require(
                persisted["pending_exit_signature"] == sell_signature
                and persisted["pending_exit_intent_id"] == original_intent,
                "outage shutdown preserves exact unknown work",
            )
        if phase == "reverted_retry":
            require(
                calls["sell"] == 1 and calls["pool_read"] > 0,
                "one bounded real seller retry fails closed at read-only transport",
            )
        if pump:
            require(
                ledger_state() == json.loads((work / "seed-ledger.json").read_text()),
                "startup/shutdown preserves every ledger row and exact reservation",
            )
            require(
                all(
                    calls[name] == 0
                    for name in (
                        "sell",
                        "price",
                        "pool_read",
                        "confirm",
                        "receipt",
                        "queue",
                        "reconciliation",
                        "monitor",
                        "submission_recovery",
                    )
                ),
                "unattested startup does not reconcile, monitor, price, or retry unknown work",
            )
            if phase != "seed":
                expected_requests = 0 if phase == "fee_account_unavailable" else 1
                require(
                    calls["prepare"] == 1
                    and calls["fee_account"] >= 1
                    and calls["attestation_request"]
                    == calls["blockhash"]
                    == calls["fee_decode"]
                    == expected_requests
                    and len(readiness_refusals) == 2,
                    "real preparation fails before either venue becomes ready",
                )
                require(
                    persisted["pending_exit_signature"] == sell_signature
                    and persisted["pending_exit_intent_id"] == original_intent
                    and persisted["exit_attempt_sequence"] == 1
                    and persisted["charged_exit_fee_lamports"] == 0
                    and persisted == before_position,
                    "refusal preserves exact unknown exit, inventory, baseline, and fee fields",
                )
        return {
            "phase": phase,
            "status": "pass",
            "platform": platform.value,
            "startup_result": (
                "seed_only"
                if phase == "seed"
                else "unavailable_attestation_safely_refused"
                if pump
                else "synthetic_ready_resume_exercised"
            ),
            "ready_resume_exercised": not pump and started,
            "native_fee_attestation_proven": False,
            "startup_refusal": startup_refusal,
            "readiness_refusals": readiness_refusals,
            "fee_fixture": {
                "provenance": "synthetic schema/values from tests/test_pumpfun_fee_schedule.py::_fee_account; not captured chain data",
                "digest": fee_fixture_digest,
                "native_simulation_executed": False,
            }
            if pump
            else None,
            "exact_seeded_ledger_rows_preserved": True if pump else None,
            "risk_reservations": ledger_state()["risk_reservations"] if pump else None,
            "synthetic": True,
            "calls": calls,
            "risk_totals": totals,
            "position_quantity_raw": persisted["quantity_raw"],
            "pending_intent": persisted["pending_exit_intent_id"],
            "pending_signature": persisted["pending_exit_signature"],
            "sell_outcome": expected.value,
            "retries": observed_retries,
        }
    finally:
        if trader.transaction_ledger is not None:
            await trader._cleanup_resources()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform", choices=("lets_bonk", "pump_fun"), default="lets_bonk"
    )
    parser.add_argument(
        "--phase",
        choices=tuple(dict.fromkeys((*PHASES, *PUMP_PHASES))),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    phases = PUMP_PHASES if args.platform == "pump_fun" else PHASES
    if args.phase is not None:
        require(args.phase in phases, "child phase must belong to selected platform")
        try:
            result = asyncio.run(child(args.phase, args.platform))
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "phase": args.phase,
                        "platform": args.platform,
                        "status": "fail",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:2000],
                    }
                )
            )
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0
    reports = []
    with tempfile.TemporaryDirectory(prefix="durable-recovery-") as directory:
        work = Path(directory).resolve()
        (work / "synthetic-only").write_text("offline recovery fixture\n")
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONNOUSERSITE": "1",
            "RECOVERY_PROBE_ROOT": str(work),
            "TMPDIR": str(work.parent),
        }
        for phase in phases:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed interpreter, script, and allowlisted phase
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--platform",
                        args.platform,
                        "--phase",
                        phase,
                    ],
                    check=False,
                    cwd=work,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=35,
                )
                report = json.loads(completed.stdout)
                reports.append(report)
                if completed.returncode != 0 or report.get("status") != "pass":
                    break
            except subprocess.TimeoutExpired as exc:
                reports.append(
                    {
                        "phase": phase,
                        "status": "fail",
                        "error_type": type(exc).__name__,
                        "error": "child exceeded the 35-second deadline",
                    }
                )
                break
            except ValueError as exc:
                reports.append(
                    {
                        "phase": phase,
                        "status": "fail",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "returncode": completed.returncode,
                        "stdout_excerpt": completed.stdout[-2000:],
                        "stderr_excerpt": completed.stderr[-2000:],
                    }
                )
                break
    passed = len(reports) == len(phases) and all(r["status"] == "pass" for r in reports)
    print(
        json.dumps(
            {
                "status": "pass" if passed else "fail",
                "platform": args.platform,
                "claim": (
                    "Unavailable startup attestation safely refuses Pump.fun resume; no ready-resume proof"
                    if args.platform == "pump_fun"
                    else "Synthetic LetsBonk durable recovery and bounded failed fresh retry"
                ),
                "ready_resume_exercised": any(
                    r.get("ready_resume_exercised", False) for r in reports
                ),
                "native_fee_attestation_proven": False,
                "phases": reports,
                "temp_state_removed": True,
                "limits": [
                    "Synthetic unfunded identity; no signing or network",
                    "Opaque synthetic signatures, no replayable wire bytes",
                    "Buy success is seeded fixture provenance, not a verified purchase",
                    "Pump.fun synthetic profile: 250000 base tokens, SOL quote cap13000000, slippage0.3, priority200000, CU buy140000/sell110000, per-tx fee cap250000; reserved fees remain synthetic 5000 each, session fee cap10000"
                    if args.platform == "pump_fun"
                    else "Synthetic reverted receipt parsed by real reconciliation",
                    "No ready Pump.fun/PumpSwap snapshot: locally encoded public test schema is not native attestation evidence"
                    if args.platform == "pump_fun"
                    else "Real retry stops at unavailable pool metadata before wire construction",
                    "No funded execution, successful liquidation, or production latency claim",
                    "Pump.fun gate refusal only; reverted-receipt/fresh-retry recovery remains covered by default LetsBonk invocation"
                    if args.platform == "pump_fun"
                    else "LetsBonk recovery lifecycle only; Pump.fun fee attestation not exercised",
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
