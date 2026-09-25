"""Offline: immediate results must not wait for slots or bypass native guards.

Only a loopback HTTP fixture is used. No credentials, signing or mainnet access.
"""

from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import simulate_atomic_cycles as atomic
import simulate_geyser_cycles as geyser
import simulate_orca_cycle as orca
from aiohttp import web

# Boundary inputs and expected transition counts deliberately remain explicit.
# ruff: noqa: PLR2004


async def verify_observer() -> None:
    """Five-decimal mint cycles survive gaps without inventing new episodes."""
    keys = [str(atomic.Pubkey.from_bytes(bytes([n]) * 32)) for n in range(1, 8)]
    scope = geyser.validate_scope(
        {
            "mint": keys[2],
            "pool_addresses": [keys[1], keys[5]],
            "expected_mint_decimals": 5,
            "selection_provenance": {"source": "offline_synthetic_fixture"},
            "scope_frozen_at": "2026-09-13T00:00:00+00:00",
            "payer": geyser.PAYER,
            "input_cap_lamports": 10_000_000,
            "configured_fee_lamports": 75_000,
            "signed_transactions_allowed": 0,
            "submitted_transactions_allowed": 0,
        }
    )
    bank = {}
    for mint, decimals in ((atomic.SOL, 9), (scope["mint"], 5)):
        data = bytearray(82)
        data[44] = decimals
        bank[mint] = {
            "owner": atomic.SPL,
            "executable": False,
            "data": [base64.b64encode(data).decode(), "base64"],
        }
    atomic.require(
        geyser.attest_mint_decimals(bank, scope["mint"], None) == 5, "actual_decimals"
    )
    try:
        geyser.attest_mint_decimals(bank, scope["mint"], 6)
    except geyser.capital.ProbeError:
        pass
    else:
        raise AssertionError("wrong_frozen_decimals_accepted")
    buy = atomic.Pool(
        keys[1],
        atomic.AMM,
        (atomic.SOL, scope["mint"]),
        (keys[3], keys[4]),
        reserves=(1_000_000_000, 2_000_000),
    )
    sell = replace(
        buy,
        address=keys[5],
        vaults=(keys[4], keys[6]),
        reserves=(1_000_000_000, 1_000_000),
    )
    events = []
    tape = SimpleNamespace(
        emit=lambda event, **fields: events.append({"event": event, **fields})
    )
    observer = geyser.Observer(
        SimpleNamespace(endpoint="offline"),
        scope,
        [buy, sell],
        [],
        geyser.Dirty(set()),
        tape,
    )
    mode = "positive"

    async def snapshot(_trigger: geyser.Trigger) -> tuple[int, dict] | None:
        return None if mode == "missing" else (100, bank)

    def hydrated(pool: atomic.Pool, _bank: dict) -> atomic.Pool:
        if mode == "partial":
            return replace(pool, reserves=(1_000_000_000, 1))
        if mode == "negative":
            return replace(pool, reserves=buy.reserves)
        return pool

    async def simulated(
        _client: object, _endpoint: str, tx: atomic.Transaction, _slot: int, _payer: str
    ) -> dict:
        atomic.require(
            all(signature == atomic.Signature.default() for signature in tx.signatures),
            "observer_signed_cycle",
        )
        return {
            "err": {"InstructionError": [7, {"Custom": 30}]}
            if mode == "rejected"
            else None,
            "net_lamports": None if mode == "rejected" else atomic.PROFIT_LAMPORTS,
        }

    with (
        patch.object(observer, "snapshot", snapshot),
        patch.object(atomic, "hydrate_pool", hydrated),
        patch.object(atomic, "simulate_cycle", simulated),
    ):
        for mode in (  # noqa: B007 - read by the fixture callbacks above
            "positive",
            "positive",
            "missing",
            "partial",
            "rejected",
            "positive",
            "negative",
            "positive",
        ):
            now = geyser.time.monotonic()
            await observer.scan(geyser.Trigger(100, keys[1], 1, now, now, 100))
    atomic.require(
        observer.counts["immediate_opportunity_episodes"] == 2,
        "missing_reset_or_persistent_overcount",
    )
    atomic.require(
        observer.counts["immediate_successes"] == 4, "observer_stopped_after_positive"
    )
    atomic.require(
        observer.counts["immediate_rejections"] == 1, "native_rejection_lost"
    )
    atomic.require(
        observer.counts["immediate_episodes_closed"] == 1,
        "complete_negative_did_not_close",
    )
    partial = next(
        row
        for row in events
        if row["event"] == "evaluation" and row["quoted_routes"] == 0
    )
    atomic.require(not partial["coverage_complete"], "zero_rounded_complete_negative")
    candidate = next(row for row in events if row["event"] == "immediate")
    frozen = atomic.Transaction.from_bytes(
        base64.b64decode(candidate["unsigned_transaction_base64"], validate=True)
    )
    expected_ata = atomic.get_associated_token_address(
        atomic.Pubkey.from_string(scope["payer"]),
        atomic.Pubkey.from_string(scope["mint"]),
    )
    atomic.require(
        expected_ata in frozen.message.account_keys, "observer_wrong_mint_inventory"
    )


def verify_orca_boundary() -> None:  # noqa: PLR0915 - one native account and wallet fixture
    """Defend native Oracle writability and whole-wallet accounting without RPC."""
    payer = atomic.Pubkey.from_string(geyser.PAYER)
    keys = [str(atomic.Pubkey.from_bytes(bytes([n]) * 32)) for n in range(1, 5)]
    amm = atomic.Pool(
        orca.AMM_POOL, atomic.AMM, (atomic.SOL, orca.MINT), tuple(keys[:2])
    )
    pool = orca.Whirlpool(
        orca.WHIRLPOOL,
        orca.PROGRAM,
        (atomic.SOL, orca.MINT),
        tuple(keys[2:]),
        orca.CONFIG,
        16,
        1030,
        -1,
    )
    raw = bytearray(653)
    raw[:8] = orca.discriminator("Whirlpool")
    for offset, key in (
        (8, pool.config),
        (101, pool.mints[0]),
        (133, pool.vaults[0]),
        (181, pool.mints[1]),
        (213, pool.vaults[1]),
    ):
        raw[offset : offset + 32] = bytes(atomic.Pubkey.from_string(key))
    struct.pack_into("<HH", raw, 41, pool.spacing, pool.spacing)
    raw[65:81] = (1 << 64).to_bytes(16, "little")
    struct.pack_into("<i", raw, 81, pool.tick)
    config = bytearray(108)
    config[:8] = orca.discriminator("WhirlpoolsConfig")
    payloads = {pool.address: (orca.PROGRAM, raw), pool.config: (orca.PROGRAM, config)}
    for mint, vault in zip(pool.mints, pool.vaults, strict=True):
        mint_raw, vault_raw = bytearray(82), bytearray(165)
        mint_raw[45] = vault_raw[108] = 1
        vault_raw[:32] = bytes(atomic.Pubkey.from_string(mint))
        vault_raw[32:64] = bytes(atomic.Pubkey.from_string(pool.address))
        payloads[mint], payloads[vault] = (
            (atomic.SPL, mint_raw),
            (atomic.SPL, vault_raw),
        )
    bank = dict.fromkeys(pool.dependencies())
    bank.update(
        {
            key: {
                "owner": owner,
                "executable": False,
                "data": [base64.b64encode(data).decode(), "base64"],
            }
            for key, (owner, data) in payloads.items()
        }
    )
    static = orca.decode(
        pool.address, bank[pool.address], expected_mints=(atomic.SOL, orca.MINT)
    )
    atomic.require(orca.hydrate(static, bank) == static, "static_oracle_required")
    struct.pack_into("<H", raw, 43, pool.fee_tier)
    bank[pool.address]["data"][0] = base64.b64encode(raw).decode()
    adaptive = orca.decode(
        pool.address, bank[pool.address], expected_mints=(atomic.SOL, orca.MINT)
    )
    try:
        orca.hydrate(adaptive, bank)
    except ValueError:
        pass
    else:
        raise AssertionError("missing_adaptive_oracle_used_static_fallback")
    atomic.require(
        [start for _, start in pool.ticks(a_to_b=True)] == [-1408, -2816, -4224]
        and [start for _, start in pool.ticks(a_to_b=False)] == [0, 1408, 2816],
        "orca_directional_tick_boundary",
    )
    initial = 1_000_000_000
    for buy, sell in ((amm, pool), (pool, amm)):
        tx = atomic.build_cycle(buy, sell, payer, 1, initial_balance=initial)
        ix = atomic.swap(
            pool, payer, atomic.SOL, 1, atomic.BUY_LAMPORTS, exact_output=True
        )
        atomic.require(
            len(ix.data) == 42 and ix.data[-2:] == bytes((0, 1)),
            "orca_exact_output_encoding",
        )
        atomic.require(
            ix.accounts[10].pubkey == atomic.Pubkey.from_string(pool.oracle)
            and ix.accounts[10].is_writable,
            "orca_adaptive_oracle_readonly",
        )
        atomic.require(
            len(bytes(tx)) <= 1232 and tx.signatures == [atomic.Signature.default()],
            "orca_unsigned_packet",
        )
        addresses = list(tx.message.account_keys)
        index = addresses.index(payer)
        guard = tx.message.instructions[-1]
        atomic.require(
            guard.data == struct.pack("<IQ", 2, initial + atomic.PROFIT_LAMPORTS)
            and list(guard.accounts) == [index, index],
            "whole_wallet_guard_not_final",
        )
        before = [0] * len(addresses)
        before[index] = initial
        after = before.copy()
        after[index] += atomic.PROFIT_LAMPORTS
        result = {
            "context": {"slot": 100},
            "value": {
                "err": None,
                "fee": atomic.NETWORK_FEE,
                "preBalances": before,
                "postBalances": after,
            },
        }
        row = atomic.simulation_result(tx, result, 100, payer, initial_balance=initial)
        atomic.require(
            row["net_lamports"] == atomic.PROFIT_LAMPORTS, "native_fees_charged_twice"
        )
        for failure in ("initial_balance", "residual_tokens"):
            if failure == "initial_balance":
                before[index] -= 1
            else:
                after[tx.message.instructions[3].accounts[1]] = 1
            try:
                atomic.simulation_result(
                    tx, result, 100, payer, initial_balance=initial
                )
            except ValueError as exc:
                expected = (
                    "wallet_baseline_changed"
                    if failure == "initial_balance"
                    else "simulation_rent_or_inventory_not_closed"
                )
                atomic.require(str(exc) == expected, "wrong_native_guard_failure")
            else:
                raise AssertionError(f"unsafe_native_result_{failure}")
            before[index] = initial
            after[tx.message.instructions[3].accounts[1]] = 0
        after[index] = initial - atomic.NETWORK_FEE
        result["value"]["err"] = {
            "InstructionError": [len(tx.message.instructions) - 1, {"Custom": 1}]
        }
        rejected = atomic.simulation_result(
            tx, result, 100, payer, initial_balance=initial
        )
        atomic.require(
            rejected["guard_rejected"] and rejected["net_lamports"] is None,
            "guard_failure_invented_cashflow",
        )


async def verify() -> None:  # noqa: C901, PLR0915 - one supervised loopback fixture
    """Exercise the real JSON-RPC boundary with controlled bank advancement."""
    verify_orca_boundary()
    await verify_observer()
    keys = [str(atomic.Pubkey.from_bytes(bytes([n]) * 32)) for n in range(1, 8)]
    payer = atomic.Pubkey.from_string(keys[0])
    buy = atomic.Pool(keys[1], atomic.AMM, (atomic.SOL, keys[2]), (keys[3], keys[4]))
    sell = replace(buy, address=keys[5], vaults=(keys[4], keys[6]))
    transaction = atomic.build_cycle(buy, sell, payer, 1)
    payer_index = list(transaction.message.account_keys).index(payer)
    ata_index = transaction.message.instructions[2].accounts[1]
    before = [0] * len(transaction.message.account_keys)
    before[payer_index] = 1_000_000_000
    state = {"slot": 100, "mode": "profit"}
    slot_requested, advance = asyncio.Event(), asyncio.Event()

    async def respond(request: web.Request) -> web.Response:
        payload = await request.json()
        if isinstance(payload, list):
            native = atomic.Transaction.from_bytes(
                base64.b64decode(payload[0]["params"][0])
            )
            balances = [0] * len(native.message.account_keys)
            index = list(native.message.account_keys).index(payer)
            balances[index] = before[payer_index]
            final = balances.copy()
            final[index] += atomic.PROFIT_LAMPORTS
            responses = [
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32016}},
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "result": {
                        "context": {"slot": 100},
                        "value": {
                            "err": None,
                            "fee": atomic.NETWORK_FEE,
                            "preBalances": balances,
                            "postBalances": final,
                        },
                    },
                },
            ]
            if state["mode"] == "truncated_batch":
                responses.pop()
            elif state["mode"] == "duplicate_batch":
                responses[0]["id"] = 0
            return web.json_response(responses)
        method = payload["method"]
        if method == "getSlot":
            slot_requested.set()
            await advance.wait()
            return web.json_response({"result": state["slot"]})
        if method == "getMultipleAccounts":
            return web.json_response(
                {"result": {"context": {"slot": state["slot"]}, "value": [None]}}
            )
        atomic.require(method == "simulateTransaction", "fixture_read_only")
        after = before.copy()
        after[payer_index] += atomic.PROFIT_LAMPORTS
        fee = atomic.NETWORK_FEE
        if state["mode"] == "bad_fee":
            fee += 1
        elif state["mode"] == "unclosed_rent":
            after[ata_index] = 1
        elif state["mode"] == "loss":
            after[payer_index] = before[payer_index] - 1
        return web.json_response(
            {
                "result": {
                    "context": {"slot": state["slot"]},
                    "value": {
                        "err": None,
                        "fee": fee,
                        "preBalances": before,
                        "postBalances": after,
                    },
                }
            }
        )

    app = web.Application()
    app.router.add_post("/", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    endpoint = f"http://127.0.0.1:{runner.addresses[0][1]}/"
    try:
        async with aiohttp.ClientSession() as session:
            # Slot polling is held indefinitely: immediate validation still completes.
            result = await asyncio.wait_for(
                atomic.simulate_cycle(session, endpoint, transaction, 100, payer), 2
            )
            atomic.require(
                result["net_lamports"] == atomic.PROFIT_LAMPORTS,
                "immediate_profit_unavailable",
            )
            delayed = asyncio.create_task(
                atomic.delayed_simulations(session, endpoint, transaction, 100, payer)
            )
            try:
                await asyncio.wait_for(slot_requested.wait(), 2)
                atomic.require(not delayed.done(), "survival_wait_skipped")
                state["slot"] = 105
                advance.set()
                results = await asyncio.wait_for(delayed, 2)
                atomic.require(
                    [row["target_delay"] for row in results] == [2, 5]
                    and all(
                        row["net_lamports"] >= atomic.PROFIT_LAMPORTS for row in results
                    ),
                    "survival_result_invalid",
                )
            finally:
                advance.set()
                if not delayed.done():
                    delayed.cancel()
                await asyncio.gather(delayed, return_exceptions=True)
            for mode in ("bad_fee", "unclosed_rent", "loss"):
                state["mode"] = mode
                try:
                    await atomic.simulate_cycle(
                        session, endpoint, transaction, 100, payer
                    )
                except ValueError:
                    continue
                raise AssertionError(f"unsafe_native_result_{mode}")
            state.update(mode="profit", slot=99)
            for action in (
                atomic.simulate_cycle(session, endpoint, transaction, 100, payer),
                atomic.bank_read(session, endpoint, [keys[2]], minimum_slot=100),
            ):
                try:
                    await action
                except ValueError:
                    continue
                raise AssertionError("bank_below_slot_floor_accepted")
            forged = atomic.Transaction.populate(
                transaction.message, [atomic.Signature.from_bytes(bytes([1]) * 64)]
            )
            state["slot"] = 105
            for simulate in (atomic.simulate_cycle, atomic.delayed_simulations):
                try:
                    await simulate(session, endpoint, forged, 100, payer)
                except ValueError:
                    continue
                raise AssertionError("nonzero_signature_accepted")
            native = atomic.build_cycle(
                buy, sell, payer, 1, initial_balance=before[payer_index]
            )
            rows = await atomic.simulate_cycle_batch(
                session,
                endpoint,
                [native, native],
                100,
                payer,
                before[payer_index],
                record_result=lambda _value: None,
            )
            atomic.require(
                rows[0]["net_lamports"] == atomic.PROFIT_LAMPORTS
                and rows[1]["err"] == "bank_not_ready",
                "reversed_batch_misclassified",
            )
            for mode in ("truncated_batch", "duplicate_batch"):
                state["mode"] = mode
                try:
                    await atomic.simulate_cycle_batch(
                        session,
                        endpoint,
                        [native, native],
                        100,
                        payer,
                        before[payer_index],
                        record_result=lambda _value: None,
                    )
                except ValueError:
                    continue
                raise AssertionError(f"unsafe_{mode}_accepted")
    finally:
        advance.set()
        await runner.cleanup()
    print(
        "PASS: immediate result precedes slot waits; fee, rent, profit, unsigned and freshness guards hold"
    )


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(verify(), 10))
