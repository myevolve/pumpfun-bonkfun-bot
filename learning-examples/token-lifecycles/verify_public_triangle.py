"""Offline triangle safety check: synthetic RPC plus the real local Solana VM.

No keys, provider files or external connections. Synthetic market replies test
orchestration, not profitability; LiteSVM tests the native fee/rent/balance guard.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import struct
import time
from dataclasses import replace
from fractions import Fraction
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import simulate_public_cycles as tri
from aiohttp import web
from solders.account import Account
from solders.instruction import CompiledInstruction
from solders.litesvm import LiteSVM
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.transaction_metadata import FailedTransactionMetadata

if TYPE_CHECKING:
    from collections.abc import Callable

# Explicit adversarial inputs and in-memory fixture access are intentional.
# ruff: noqa: PLR2004, S101, SLF001
BASELINE = 1_000_000_000
SLOT = 100
ROUTE = tri.TRIANGLE


def account(
    owner: str, data: bytes | bytearray = b"", lamports: int = 10_000_000
) -> dict:
    return {
        "owner": owner,
        "data": [base64.b64encode(data).decode(), "base64"],
        "lamports": lamports,
        "executable": False,
    }


def pub(number: int) -> str:
    return str(tri.atomic.Pubkey.from_bytes(bytes([number]) * 32))


def fixture() -> dict:
    """One complete bank includes the non-SOL Whirlpool and active on-chain LUTs."""
    a = tri.atomic
    bank = {
        str(tri.PAYER): account("11111111111111111111111111111111", lamports=BASELINE)
    }
    bank.update(dict.fromkeys(map(str, ROUTE.atas)))
    clock = bytearray(40)
    struct.pack_into("<Q", clock, 0, SLOT)
    struct.pack_into("<q", clock, 32, 2_000_000)
    bank[a.CLOCK] = account("Sysvar1111111111111111111111111111111111111", clock)
    for mint, decimals in zip(ROUTE.mints, (9, 4, 6), strict=True):
        raw = bytearray(82)
        raw[44:46] = bytes((decimals, 1))
        bank[mint] = account(a.SPL, raw)
    config = bytearray(108)
    config[:8] = tri.orca.discriminator("WhirlpoolsConfig")
    bank[tri.orca.CONFIG] = account(tri.orca.PROGRAM, config)
    for i, mints in enumerate(((a.SOL, tri.MINT), (tri.MINT, tri.USDC))):
        raw = bytearray(653)
        raw[:8] = tri.orca.discriminator("Whirlpool")
        spacing = 16 if i == 0 else 32896
        struct.pack_into("<HH", raw, 41, spacing, spacing)
        raw[65:81] = (1 << 64).to_bytes(16, "little")
        vaults = (pub(10 + 2 * i), pub(11 + 2 * i))
        for offset, key in (
            (8, tri.orca.CONFIG),
            (101, mints[0]),
            (133, vaults[0]),
            (181, mints[1]),
            (213, vaults[1]),
        ):
            raw[offset : offset + 32] = bytes(a.Pubkey.from_string(key))
        bank[ROUTE.pools[i]] = account(tri.orca.PROGRAM, raw)
        for mint, vault in zip(mints, vaults, strict=True):
            token = bytearray(165)
            token[:32] = bytes(a.Pubkey.from_string(mint))
            token[32:64] = bytes(a.Pubkey.from_string(ROUTE.pools[i]))
            token[108] = 1
            struct.pack_into("<Q", token, 64, 10**12)
            bank[vault] = account(a.SPL, token)
        pool = tri.orca.decode(
            ROUTE.pools[i], bank[ROUTE.pools[i]], expected_mints=mints
        )
        for key in pool.dependencies():
            bank.setdefault(key, None)
    raw = bytearray(637)
    raw[:8] = tri.orca.discriminator("PoolState")
    for offset, key in (
        (8, pub(20)),
        (72, pub(21)),
        (104, pub(22)),
        (168, a.SOL),
        (200, tri.USDC),
        (232, a.SPL),
        (264, a.SPL),
        (296, pub(23)),
    ):
        raw[offset : offset + 32] = bytes(a.Pubkey.from_string(key))
    bank[ROUTE.pools[2]] = account(a.CPMM, raw)
    config = bytearray(236)
    config[:8] = tri.orca.discriminator("AmmConfig")
    struct.pack_into("<Q", config, 12, 3000)
    bank[pub(20)] = account(a.CPMM, config)
    bank[pub(23)] = account(a.CPMM)
    for mint, vault in zip((a.SOL, tri.USDC), (pub(21), pub(22)), strict=True):
        token = bytearray(165)
        token[:32] = bytes(a.Pubkey.from_string(mint))
        token[32:64] = bytes(a.Pubkey.from_string(a.AUTHORITY[a.CPMM]))
        token[108] = 1
        struct.pack_into("<Q", token, 64, (2 if mint == a.SOL else 1) * 10**12)
        bank[vault] = account(a.SPL, token)
    addresses = list(
        dict.fromkeys(
            [
                *bank,
                a.TIP_ACCOUNT,
                a.AUTHORITY[a.CPMM],
                tri.orca.PROGRAM,
                a.CPMM,
                a.SPL,
                tri.orca.CONFIG,
            ]
        )
    )
    for i, key in enumerate(ROUTE.lookups):
        raw = bytearray(56)
        struct.pack_into("<IQQ", raw, 0, 1, 2**64 - 1, 1)
        raw += b"".join(bytes(a.Pubkey.from_string(key)) for key in addresses[i::4])
        bank[key] = account(tri.native.LUT, raw)
    return bank


def response_for(
    tx: VersionedTransaction, tables: list, legs: int, route: tri.Route = ROUTE
) -> dict:
    keys = tri.native.resolve_keys(tx, tables)
    before = [0] * len(keys)
    before[0] = BASELINE
    after = before.copy()
    after[0] += tri.atomic.PROFIT_LAMPORTS
    accounts = [None] * len(route.mints)
    if legs < len(route.pools):
        after[0] = (
            BASELINE - tri.atomic.NETWORK_FEE - 1_000_000 - len(route.mints) * 2_039_280
        )
        amounts = [0] * len(route.mints)
        amounts[legs] = (379, 111303, 217)[legs - 1]
        for i, mint in enumerate(route.mints):
            raw = bytearray(165)
            raw[:32] = bytes(tri.atomic.Pubkey.from_string(mint))
            raw[32:64] = bytes(tri.PAYER)
            raw[108] = 1
            struct.pack_into("<Q", raw, 64, amounts[i])
            accounts[i] = account(tri.atomic.SPL, raw, 2_039_280)
            after[keys.index(route.atas[i])] = 2_039_280
    return {
        "context": {"slot": SLOT},
        "value": {
            "err": None,
            "fee": tri.atomic.NETWORK_FEE,
            "unitsConsumed": 200000,
            "preBalances": before,
            "postBalances": after,
            "accounts": accounts,
        },
    }


def check_refused(operation: Callable[[], object], reason: str) -> None:
    try:
        operation()
    except (ValueError, tri.evidence.StudyError) as exc:
        assert str(exc) == reason, (str(exc), reason)
    else:
        raise AssertionError(reason)


def verify_accounting(bank: dict) -> None:
    pools = tri.read_pools(ROUTE, bank)
    live, tables = tri.hydrate(ROUTE, pools, bank, SLOT)
    check_refused(
        lambda: tri.orca.decode(
            ROUTE.pools[1],
            bank[ROUTE.pools[1]],
            expected_mints=(tri.atomic.SOL, tri.MINT),
        ),
        "orca_mint_identity",
    )
    check_refused(
        lambda: tri.orca.decode(
            ROUTE.pools[1], bank[ROUTE.pools[1]], expected_mints=(tri.MINT, tri.MINT)
        ),
        "orca_mint_identity",
    )
    tx = tri.build_packet(ROUTE, live, tables, 1_000_000, [111303], BASELINE)
    result = response_for(tx, tables, 3)
    row = tri.native_result(ROUTE, tx, tables, result, BASELINE, 3)
    assert row["net_lamports"] == 1000 and row["status"] == "native_guarded_success"
    ata_index = tri.native.resolve_keys(tx, tables).index(ROUTE.atas[2])
    for field, index, delta, reason in (
        ("preBalances", 0, 1, "wallet_baseline_changed"),
        ("preBalances", ata_index, 1, "preexisting_inventory"),
        ("postBalances", ata_index, 1, "rent_or_inventory_not_closed"),
        ("postBalances", 0, -1, "native_profit_violation"),
    ):
        bad = copy.deepcopy(result)
        bad["value"][field][index] += delta
        check_refused(
            partial(tri.native_result, ROUTE, tx, tables, bad, BASELINE, 3), reason
        )
    failed = copy.deepcopy(result)
    failed["value"]["err"] = {
        "InstructionError": [len(tx.message.instructions) - 1, {"Custom": 1}]
    }
    failed["value"]["postBalances"][0] = BASELINE - tri.atomic.NETWORK_FEE
    assert (
        tri.native_result(ROUTE, tx, tables, failed, BASELINE, 3)["status"]
        == "guard_rejected"
    )
    failed["value"]["postBalances"][0] += 1
    check_refused(
        lambda: tri.native_result(ROUTE, tx, tables, failed, BASELINE, 3),
        "failed_native_delta",
    )
    quote = tri.build_packet(ROUTE, live, tables, 1_000_000, [], BASELINE)
    keys = tri.native.resolve_keys(quote, tables)
    swaps = [
        ix
        for ix in quote.message.instructions
        if str(keys[ix.program_id_index]) in ROUTE.programs
    ]
    # Canonical exact-input two_hop_swap has no externally quoted leg-one amount.
    assert len(swaps) == 1
    assert str(keys[swaps[0].program_id_index]) == tri.orca.PROGRAM
    assert bytes(swaps[0].data) == bytes.fromhex("c360ed6c44a2dbe6") + struct.pack(
        "<QQBBB", 1_000_000, 1, 1, 1, 1
    ) + bytes(32) and [
        bytes(ix.data)
        for ix in tx.message.instructions
        if str(tx.message.account_keys[ix.program_id_index]) == tri.orca.PROGRAM
    ] == [
        bytes.fromhex("c360ed6c44a2dbe6")
        + struct.pack("<QQBBB", 111303, 1_000_000, 0, 1, 1)
        + bytes(32)
    ]
    assert [keys[swaps[0].accounts[i]] for i in (2, 3)] == [
        tri.atomic.Pubkey.from_string(pool.address) for pool in live[:2]
    ]
    assert keys[swaps[0].accounts[6]] == keys[swaps[0].accounts[8]] == ROUTE.atas[1]
    native_quote = response_for(quote, tables, 2)
    quoted = tri.native_result(ROUTE, quote, tables, native_quote, BASELINE, 2)
    assert quoted["net_lamports"] is None and quoted["quantity"] == 111303
    for index in (0, 1):
        residual = copy.deepcopy(native_quote)
        wrong = bytearray(
            base64.b64decode(residual["value"]["accounts"][index]["data"][0])
        )
        struct.pack_into("<Q", wrong, 64, 1)
        residual["value"]["accounts"][index]["data"][0] = base64.b64encode(
            wrong
        ).decode()
        check_refused(
            partial(tri.native_result, ROUTE, quote, tables, residual, BASELINE, 2),
            "prefix_inventory_not_consumed",
        )
    check_refused(
        partial(tri.native_result, ROUTE, quote, tables, native_quote, BASELINE, 3),
        "wallet_guard_missing",
    )
    for quantities in ([379, 111303], [0], [True], [2**64]):
        check_refused(
            partial(
                tri.build_packet, ROUTE, live, tables, 1_000_000, quantities, BASELINE
            ),
            "intermediate_quantity",
        )
    check_refused(
        partial(
            tri.build_packet,
            ROUTE,
            [live[0], replace(live[1], mints=(pub(81), tri.USDC)), live[2]],
            tables,
            1_000_000,
            [],
            BASELINE,
        ),
        "orca_two_hop_intermediate_mint",
    )
    request = tri.simulation_calls(ROUTE, tx, tri.dependencies(ROUTE, pools), SLOT)
    tri.validate_request(ROUTE, request)
    signed = VersionedTransaction.populate(
        tx.message, [tri.atomic.Signature.from_bytes(bytes([1]) * 64)]
    )
    request[1]["params"][0] = base64.b64encode(bytes(signed)).decode()
    check_refused(
        lambda: tri.validate_request(ROUTE, request), "signed_transaction_rejected"
    )
    request = tri.simulation_calls(ROUTE, tx, tri.dependencies(ROUTE, pools), SLOT)
    request[1]["params"][1]["accountOverrides"] = {}
    check_refused(lambda: tri.validate_request(ROUTE, request), "simulation_options")
    check_refused(
        lambda: tri.validate_request(ROUTE, tri.evidence.single("sendTransaction", [])),
        "unsigned_simulation_only",
    )
    check_refused(lambda: tri.scope(ROUTE, [10_000_001]), "input_size_policy")
    check_refused(
        lambda: tri.fresh_result(
            result, SLOT + 1, SLOT, time.monotonic_ns() + tri.WINDOW_NS
        ),
        "state_simulation_slot_mismatch",
    )
    check_refused(
        lambda: tri.fresh_result(
            result, SLOT, SLOT - 3, time.monotonic_ns() + tri.WINDOW_NS
        ),
        "quote_slot_age",
    )
    check_refused(
        lambda: tri.fresh_result(result, SLOT, SLOT, time.monotonic_ns() - 1),
        "candidate_deadline_expired",
    )


def verify_native_guard(bank: dict) -> None:
    """Real local SVM: setup/closes cannot mint profit; exact balance boundary holds."""
    a = tri.atomic
    live, tables = tri.hydrate(ROUTE, tri.read_pools(ROUTE, bank), bank, SLOT)
    tx = tri.build_packet(ROUTE, live, tables, 1_000_000, [111303], BASELINE)
    keys = tri.native.resolve_keys(tx, tables)
    instructions = [
        ix
        for ix in tx.message.instructions
        if str(keys[ix.program_id_index]) not in (tri.orca.PROGRAM, a.CPMM)
    ]
    svm = LiteSVM().with_sigverify(sigverify=False).with_blockhash_check(check=False)
    svm.warp_to_slot(SLOT)
    for key in (str(tri.PAYER), *ROUTE.mints, *ROUTE.lookups):
        value = bank[key]
        svm.set_account(
            a.Pubkey.from_string(key),
            Account(
                value["lamports"],
                base64.b64decode(value["data"][0]),
                a.Pubkey.from_string(value["owner"]),
            ),
        )
    svm.set_account(
        a.Pubkey.from_string(a.TIP_ACCOUNT),
        Account(10_000_000, b"", a.Pubkey.default()),
    )

    def simulate(selected: list) -> object:
        message = MessageV0(
            tx.message.header,
            tx.message.account_keys,
            tx.message.recent_blockhash,
            selected,
            tx.message.address_table_lookups,
        )
        return svm.simulate_transaction(
            VersionedTransaction.populate(message, [a.Signature.default()])
        )

    # This LiteSVM version has a different priority-fee schedule. Calibrate the
    # balance boundary with a no-guard control, not an assumed RPC fee amount.
    control = simulate(instructions[:-1])
    assert not isinstance(control, FailedTransactionMetadata), control
    accounts = dict(control.post_accounts())
    closed_balance = accounts[tri.PAYER].lamports
    assert closed_balance < BASELINE - a.TIP_LAMPORTS
    assert all(accounts[ata].lamports == 0 for ata in ROUTE.atas)
    for bound in (closed_balance, closed_balance + 1, BASELINE + a.PROFIT_LAMPORTS):
        last = instructions[-1]
        guard = CompiledInstruction(
            last.program_id_index, struct.pack("<IQ", 2, bound), last.accounts
        )
        outcome = simulate([*instructions[:-1], guard])
        assert isinstance(outcome, FailedTransactionMetadata) == (
            bound > closed_balance
        ), outcome
        assert svm.get_balance(tri.PAYER) == BASELINE


def verify_spot_screen(bank: dict) -> None:
    """Protect inverse prices, effective reserves, capped spend and stale admission."""
    pools, _ = tri.hydrate(ROUTE, tri.read_pools(ROUTE, bank), bank, SLOT)
    changed = copy.deepcopy(bank)
    raw = bytearray(base64.b64decode(changed[ROUTE.pools[1]]["data"][0]))
    raw[65:81] = (1 << 65).to_bytes(16, "little")
    raw[101:133], raw[181:213] = raw[181:213], raw[101:133]
    raw[133:165], raw[213:245] = raw[213:245], raw[133:165]
    changed[ROUTE.pools[1]] = account(tri.orca.PROGRAM, raw)
    live, _ = tri.hydrate(ROUTE, tri.read_pools(ROUTE, changed), changed, SLOT)
    assert tri._optimistic_ratio(ROUTE, live, changed) == Fraction(1, 2)
    raw = bytearray(base64.b64decode(bank[ROUTE.pools[2]]["data"][0]))
    struct.pack_into("<Q", raw, 349, 5 * 10**11)  # Owed input fees are not liquidity.
    changed = {**bank, ROUTE.pools[2]: account(tri.atomic.CPMM, raw)}
    ratio = tri._optimistic_ratio(ROUTE, pools, changed)
    state = {"slot": SLOT, "screen": (ratio, SLOT, 0)}
    assert (
        tri._screen_candidate(state, 30_000, 0) is None
    )  # Raw vaults would falsely reject.
    assert tri._optimistic_ratio(tri.DAMM_CYCLE, [], {}) is None

    for ratio, expected in (
        (Fraction(1, 2), -65_000),
        (
            Fraction(2_131_999, 2_000_000),
            999,
        ),  # Just below the profit floor; floor the half-lamport. Tip 0 since P2.
    ):
        state = {"slot": SLOT, "screen": (ratio, SLOT, 0)}
        result = tri._screen_candidate(state, 1_000_000, 0)
        assert result["status"] == "screened_out" and result["net_lamports"] is None
        assert result["optimistic_net_bound_lamports"] == expected
    state = {"slot": SLOT, "screen": (Fraction(533, 500), SLOT, 0)}
    assert tri._screen_candidate(state, 1_000_000, 0) is None  # Exactly +1000.
    assert (
        tri._screen_candidate(state, 100_000, 0) is None
    )  # Never reuse after dispatch.
    for age_ns, age_slots in ((tri.WINDOW_NS + 1, 0), (0, 3), (-1, 0), (0, -1)):
        state = {"slot": SLOT + age_slots, "screen": (Fraction(1), SLOT, 0)}
        assert tri._screen_candidate(state, 1_000_000, age_ns) is None


async def verify_snapshot_quote(bank: dict) -> None:
    """A snapshot seed removes one RPC without resetting quote age or pricing dust."""
    route = replace(
        ROUTE,
        mints=(ROUTE.mints[0], ROUTE.mints[2], ROUTE.mints[1]),
        decimals=(ROUTE.decimals[0], ROUTE.decimals[2], ROUTE.decimals[1]),
        pools=tuple(reversed(ROUTE.pools)),
        programs=tuple(reversed(ROUTE.programs)),
    )
    pools, tables = tri.hydrate(route, tri.read_pools(route, bank), bank, SLOT)
    now = 10 * tri.evidence.NS
    for drift, age, dust, expected in (
        (0, 0, False, None),
        (2, 0, False, None),
        (3, 0, False, "quote_slot_age"),
        (-1, 0, False, "quote_slot_age"),
        (0, tri.WINDOW_NS + 1, False, "quote_snapshot_expired"),
        (0, -1, False, "quote_snapshot_expired"),
        (0, 0, True, "rent_or_inventory_not_closed"),
    ):
        calls = []
        state = {
            "pools": pools,
            "tables": tables,
            "baseline": tri.wallet(route, bank),
            "slot": SLOT,
            "quote_snapshot": (SLOT, now - age, bank),
        }

        async def call(
            role: str,
            request: list,
            deadline: int,
            _index: int,
            case: tuple = (drift, age, dust, calls),
        ) -> list:
            drift, age, dust, calls = case
            assert role == "guarded", "redundant_first_leg_native_quote"
            assert deadline <= now - age + tri.WINDOW_NS
            tri.validate_request(route, request)
            calls.append(role)
            tx = VersionedTransaction.from_bytes(
                base64.b64decode(request[1]["params"][0])
            )
            reply = response_for(tx, tables, len(route.pools), route)
            reply["context"]["slot"] = SLOT + drift
            if dust:
                keys = tri.native.resolve_keys(tx, tables)
                reply["value"]["postBalances"][keys.index(route.atas[1])] = 1
            clock = bytearray(base64.b64decode(bank[tri.atomic.CLOCK]["data"][0]))
            struct.pack_into("<Q", clock, 0, SLOT + drift)
            current = {
                **bank,
                tri.atomic.CLOCK: account(bank[tri.atomic.CLOCK]["owner"], clock),
            }
            return [
                {
                    "context": {"slot": SLOT + drift},
                    "value": [current[key] for key in request[0]["params"][0]],
                },
                reply,
            ]

        rpc = SimpleNamespace(
            call=call, tape=SimpleNamespace(emit=lambda *_args, **_kwargs: None)
        )
        with patch.object(tri.time, "monotonic_ns", return_value=now):
            try:
                result = await tri.candidate(route, rpc, state, 1_000_000, 0)
            except ValueError as exc:
                assert expected == str(exc), (expected, str(exc))
            else:
                assert expected is None
                assert result["status"] == "native_guarded_success"
                assert result["first_quote_slot"] == SLOT
                assert result["net_lamports"] == tri.atomic.PROFIT_LAMPORTS
        assert calls == ([] if expected == "quote_snapshot_expired" else ["guarded"])


async def verify_snapshot_prefix(bank: dict) -> None:
    """Quote all leading pools, keep native closure, and never reuse the seed."""
    a = tri.atomic
    route = replace(
        ROUTE,
        mints=(*ROUTE.mints, pub(60)),
        decimals=(*ROUTE.decimals, 6),
        pools=tuple(pub(70 + i) for i in range(4)),
        programs=(a.CPMM,) * 4,
    )
    bank = dict(bank)
    bank[pub(60)] = bank[tri.USDC]
    bank.update(dict.fromkeys(map(str, route.atas)))
    added = [pub(60), str(route.atas[-1])]
    for i, address in enumerate(route.pools):
        mints = (route.mints[i], route.mints[(i + 1) % 4])
        vaults = (pub(90 + 2 * i), pub(91 + 2 * i))
        observation = pub(110 + i)
        raw = bytearray(base64.b64decode(bank[ROUTE.pools[2]]["data"][0]))
        for offset, key in (
            (72, vaults[0]),
            (104, vaults[1]),
            (168, mints[0]),
            (200, mints[1]),
            (296, observation),
        ):
            raw[offset : offset + 32] = bytes(a.Pubkey.from_string(key))
        bank[address] = account(a.CPMM, raw)
        bank[observation] = account(a.CPMM)
        for mint, vault in zip(mints, vaults, strict=True):
            token = bytearray(base64.b64decode(bank[pub(21)]["data"][0]))
            token[:32] = bytes(a.Pubkey.from_string(mint))
            struct.pack_into("<Q", token, 64, 10**12)
            bank[vault] = account(a.SPL, token)
        added.extend((address, *vaults, observation))
    raw = base64.b64decode(bank[route.lookups[0]]["data"][0])
    raw += b"".join(bytes(a.Pubkey.from_string(key)) for key in added)
    bank[route.lookups[0]] = account(tri.native.LUT, raw)
    pools, tables = tri.hydrate(route, tri.read_pools(route, bank), bank, SLOT)
    state = {
        "pools": pools,
        "tables": tables,
        "baseline": tri.wallet(route, bank),
        "slot": SLOT,
        "quote_snapshot": (SLOT, 0, bank),
        "screen": (Fraction(269, 250), SLOT, 0),
    }
    calls = []

    async def call(role: str, request: list, _deadline: int, index: int) -> list:
        calls.append(role)
        assert role == ("guarded" if index == 0 else "prefix_1"), (
            "redundant_prefix_quote"
        )
        tri.validate_request(route, request)
        if index == 1:
            raise ValueError("unseeded_prefix_observed")
        tx = VersionedTransaction.from_bytes(base64.b64decode(request[1]["params"][0]))
        return [
            {
                "context": {"slot": SLOT},
                "value": [bank[key] for key in request[0]["params"][0]],
            },
            response_for(tx, tables, 4, route),
        ]

    rpc = SimpleNamespace(call=call, tape=SimpleNamespace(emit=lambda *_a, **_k: None))
    with patch.object(tri.time, "monotonic_ns", return_value=0):
        screened = await tri.candidate(route, rpc, state, 100_000, 0)
        assert screened["status"] == "screened_out"
        result = await tri.candidate(route, rpc, state, 1_000_000, 0)
        assert result["status"] == "native_guarded_success"
        assert result["intermediate_quantities"] == [996999, 994007, 991023]
        try:
            await tri.candidate(route, rpc, state, 1_000_000, 1)
        except ValueError as exc:
            assert str(exc) == "unseeded_prefix_observed"
        else:
            raise AssertionError("unseeded_prefix_missing")
    assert calls == ["guarded", "prefix_1"]


async def verify_native_snapshot(bank: dict) -> None:  # noqa: C901, PLR0915 - paired-response safety matrix
    """A paired first quote saves a request without trusting stale or altered data."""
    pools, tables = tri.hydrate(ROUTE, tri.read_pools(ROUTE, bank), bank, SLOT)
    keys = tri.dependencies(ROUTE, pools)
    tx = tri.build_packet(ROUTE, pools, tables, 1_000_000, [], BASELINE)
    now = 10 * tri.evidence.NS
    cases = (
        ("valid", None),
        ("packet_changed", "candidate_dependencies_changed"),
        ("slot_mismatch", "state_simulation_slot_mismatch"),
        ("expired", "native_snapshot_expired"),
        ("future", "native_snapshot_expired"),
        ("fee_changed", "native_fee_mismatch"),
        ("inventory", "prefix_inventory_not_consumed"),
        ("later_slot_age", "quote_slot_age"),
        ("later_deadline", "candidate_deadline_expired"),
    )
    for case, expected in cases:
        request = tri.simulation_calls(ROUTE, tx, keys, SLOT)
        response = [
            {"context": {"slot": SLOT}, "value": [bank[key] for key in keys]},
            response_for(tx, tables, ROUTE.quote_legs[0]),
        ]
        requested, expires = now - 350_000_000, now + 1_400_000_000
        if case == "packet_changed":
            changed = tri.build_packet(ROUTE, pools, tables, 1_000_001, [], BASELINE)
            request = tri.simulation_calls(ROUTE, changed, keys, SLOT)
        elif case == "slot_mismatch":
            response[1]["context"]["slot"] += 1
        elif case == "expired":
            expires = now - 1
        elif case == "future":
            requested = now + 1
        elif case == "fee_changed":
            response[1]["value"]["fee"] += 1
        elif case == "inventory":
            token = response[1]["value"]["accounts"][0]
            raw = bytearray(base64.b64decode(token["data"][0]))
            struct.pack_into("<Q", raw, 64, 1)
            token["data"][0] = base64.b64encode(raw).decode()
        state = {
            "pools": pools,
            "tables": tables,
            "baseline": tri.wallet(ROUTE, bank),
            "slot": SLOT,
            "quote_snapshot": (SLOT, requested, bank),
            "native_snapshot": (request, response, requested, expires),
        }
        remaining = 1

        async def call(
            role: str,
            calls: list,
            _deadline: int,
            _index: int,
            case_info: tuple[str, int] = (case, expires),
        ) -> list:
            nonlocal remaining
            case, expires = case_info
            tri.atomic.require(remaining > 0, "http_request_limit")
            remaining -= 1
            tri.validate_request(ROUTE, calls)
            packet = VersionedTransaction.from_bytes(
                base64.b64decode(calls[1]["params"][0])
            )
            legs = len(ROUTE.pools) if role == "guarded" else int(role.split("_")[1])
            result = response_for(packet, tables, legs)
            current = bank
            if case == "later_slot_age":
                result["context"]["slot"] = SLOT + 3
                raw = bytearray(base64.b64decode(bank[tri.atomic.CLOCK]["data"][0]))
                struct.pack_into("<Q", raw, 0, SLOT + 3)
                current = {
                    **bank,
                    tri.atomic.CLOCK: account(bank[tri.atomic.CLOCK]["owner"], raw),
                }
            elif case == "later_deadline":
                clock.return_value = expires + 1
            return [
                {
                    "context": result["context"],
                    "value": [current[key] for key in calls[0]["params"][0]],
                },
                result,
            ]

        rpc = SimpleNamespace(
            call=call, tape=SimpleNamespace(emit=lambda *_a, **_k: None)
        )
        with patch.object(tri.time, "monotonic_ns", return_value=now) as clock:
            try:
                result = await tri.candidate(ROUTE, rpc, state, 1_000_000, 0)
            except ValueError as exc:
                assert expected == str(exc), (case, expected, str(exc))
            else:
                assert expected is None, case
                assert result["status"] == "native_guarded_success"
                assert result["net_lamports"] == tri.atomic.PROFIT_LAMPORTS
                assert result["first_quote_slot"] == SLOT
                remaining = 1
                try:
                    await tri.candidate(ROUTE, rpc, state, 1_000_000, 1)
                except ValueError as exc:
                    assert str(exc) == "http_request_limit"
                else:
                    raise AssertionError("native_snapshot_reused")


async def verify_loopback(bank: dict) -> None:  # noqa: C901, PLR0915 - one bounded HTTP failure matrix
    """Exercise full sweeps, late validation, active failures and interrupted audits."""
    _, tables = tri.hydrate(ROUTE, tri.read_pools(ROUTE, bank), bank, SLOT)
    observed = []
    mode = "normal"
    drift = 0
    clock = time.monotonic_ns
    original_call = tri.evidence.RPC.call
    original_emit = tri.evidence.Tape.emit
    original_result = tri.native_result
    no_edge = copy.deepcopy(bank)
    raw = bytearray(base64.b64decode(no_edge[pub(21)]["data"][0]))
    struct.pack_into("<Q", raw, 64, 10**12)
    no_edge[pub(21)] = account(tri.atomic.SPL, raw)

    def now() -> int:
        return clock() + drift

    async def call(rpc: object, role: str, *args: object, **kwargs: object) -> dict:
        if (mode == "cancel_audit" and role == "audit") or (
            mode == "cancel_terminal" and role == "guarded"
        ):
            raise asyncio.CancelledError
        return await original_call(rpc, role, *args, **kwargs)

    def emit(tape: object, event: str, **fields: object) -> dict:
        if mode == "cancel_terminal" and event == "terminal":
            raise OSError("synthetic_terminal_failure")
        return original_emit(tape, event, **fields)

    def native_result(*args: object) -> dict:
        nonlocal drift
        result = original_result(*args)
        if mode == "late_validation" and args[-1] == 3:
            drift = 3 * tri.evidence.NS
        return result

    async def reply(request: web.Request) -> web.Response:
        body = await request.json()
        calls = body if isinstance(body, list) else [body]
        replies = []
        for item in calls:
            observed.append(item["method"])
            if item["method"] == "getGenesisHash":
                result = tri.evidence.GENESIS
            elif item["method"] == "getMultipleAccounts":
                result = {
                    "context": {"slot": SLOT},
                    "value": [
                        (no_edge if mode == "screened_out" else bank)[key]
                        for key in item["params"][0]
                    ],
                }
                if item["params"][0] == ROUTE.wallet_keys:
                    if mode == "audit_failure":
                        return web.Response(status=429, text="synthetic refusal")
                    if mode == "audit_regression":
                        result["context"]["slot"] -= 1
            else:
                tx = VersionedTransaction.from_bytes(
                    base64.b64decode(item["params"][0])
                )
                keys = tri.native.resolve_keys(tx, tables)
                legs = sum(
                    2 if bytes(ix.data[:8]) == bytes.fromhex("c360ed6c44a2dbe6") else 1
                    for ix in tx.message.instructions
                    if str(keys[ix.program_id_index])
                    in (tri.orca.PROGRAM, tri.atomic.CPMM)
                )
                if mode == "prefix_failure" and legs == 2:
                    return web.Response(status=429, text="synthetic refusal")
                result = response_for(tx, tables, legs)
            replies.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
        return web.json_response(replies if isinstance(body, list) else replies[0])

    app = web.Application()
    app.router.add_post("/", reply)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"
    failures = []
    try:
        with TemporaryDirectory(prefix="triangle-proof-") as directory:
            for mode in (
                "normal",
                "screened_out",
                "audit_failure",
                "audit_regression",
                "prefix_failure",
                "cancel_audit",
                "cancel_terminal",
                "late_validation",
            ):
                drift = 0
                observed.clear()
                inputs = (
                    [1_000_000, 100_000] if mode == "prefix_failure" else [1_000_000]
                )
                policy = tri.scope(ROUTE, inputs)
                policy["response_header_pacing_ns"] = 0
                path = Path(directory) / f"{mode}.jsonl"
                tape = tri.evidence.Tape(path, policy)
                result, raised = None, None
                try:
                    with (
                        patch.object(tri.evidence.RPC, "call", call),
                        patch.object(tri.evidence.Tape, "emit", emit),
                        patch.object(tri, "native_result", native_result),
                        patch.object(tri.time, "monotonic_ns", now),
                    ):
                        result = await tri.observe(ROUTE, url, (), tape, policy)
                except (asyncio.CancelledError, OSError) as exc:
                    raised = type(exc)
                finally:
                    tape.file.close()
                records = [json.loads(line) for line in path.read_text().splitlines()]
                try:
                    if mode.startswith("cancel"):
                        assert raised is asyncio.CancelledError, raised
                        if mode == "cancel_audit":
                            assert records[-1]["event"] == "terminal"
                            assert records[-1]["failure"] == "interrupted"
                    elif mode == "prefix_failure":
                        assert not result["completed"]
                        assert any(
                            row["input_lamports"] == 1_000_000
                            and row["status"] == "failed"
                            for row in result["candidates"]
                        ), result
                        assert result["unattempted_inputs"] == [100_000], result
                    elif mode == "late_validation":
                        assert result["candidates"][0]["status"] == "refused", result
                        assert (
                            result["candidates"][0]["reason"]
                            == "candidate_deadline_expired"
                        ), result
                        assert result["candidates"][0]["net_lamports"] is None
                    elif mode == "screened_out":
                        assert (
                            result["completed"] and result["wallet_audit"]["unchanged"]
                        )
                        candidate = result["candidates"][0]
                        assert candidate["status"] == "screened_out", result
                        assert candidate["net_lamports"] is None
                        assert candidate["optimistic_net_bound_lamports"] == -65_000
                    else:
                        assert result["completed"] == (mode == "normal"), result
                        assert result["wallet_audit"]["unchanged"] == (
                            mode == "normal"
                        ), result
                        assert (
                            result["candidates"][0]["status"]
                            == "native_guarded_success"
                        ), result
                    rpc_rows = [row for row in records if row["event"] == "rpc"]
                    expected_roles = ["identity", "metadata", "dependencies"]
                    expected_roles += {
                        "screened_out": ["audit"],
                        "cancel_terminal": ["prefix_2"],
                        "prefix_failure": ["prefix_2", "audit"],
                        "cancel_audit": ["prefix_2", "guarded"],
                    }.get(mode, ["prefix_2", "guarded", "audit"])
                    assert [row["role"] for row in rpc_rows] == expected_roles
                    assert all(row["attempted"] for row in rpc_rows)
                    if result is not None:
                        assert result["http_requests"] == len(expected_roles)
                        assert result["http_requests"] <= policy["max_http"]
                    assert set(observed) == (
                        {"getGenesisHash", "getMultipleAccounts"}
                        if mode == "screened_out"
                        else {
                            "getGenesisHash",
                            "getMultipleAccounts",
                            "simulateTransaction",
                        }
                    )
                except AssertionError:
                    failures.append(mode)
    finally:
        await runner.cleanup()
    assert not failures, failures


async def verify_cancelled_transport(storage_failure: bool) -> None:  # noqa: FBT001
    rows = []

    class CancelSession:
        def post(self, *_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError

    def emit(_event: str, **fields: object) -> dict:
        if storage_failure:
            raise OSError("synthetic_evidence_failure")
        rows.append(fields)
        return fields

    rpc = tri.evidence.RPC(
        CancelSession(),
        "https://invalid.test",
        (),
        SimpleNamespace(emit=emit),
        {"max_http": 2, "response_header_pacing_ns": 0},
    )
    try:
        await rpc.call(
            "identity",
            tri.evidence.single("getGenesisHash", []),
            time.monotonic_ns() + tri.WINDOW_NS,
        )
    except asyncio.CancelledError:
        assert storage_failure or rows[-1]["failure"] == "interrupted"
    else:
        raise AssertionError("transport_swallowed_cancellation")


def verify_dlmm_boundary() -> None:  # noqa: PLR0915 - one native ABI and dependency boundary
    """Cover signed bitmap transitions, canonical swap2 and missing-bank refusals."""
    a, d = tri.atomic, tri.dlmm
    bits = (
        sum(1 << n for n in (0, 510, 511, 512, 513, 1023)),
        1 | (1 << 511) | (1 << 512),
        1 | (1 << 511) | (1 << 512),
    )
    initialized = (-1025, -1024, -513, -512, -2, -1, 0, 1, 511, 512, 1023, 1024)
    for active in (-1025 * 70, -513 * 70, -71, -70, -1, 0, 511 * 70, 1024 * 70):
        for decreasing in (True, False):
            expected = sorted(
                (
                    n
                    for n in initialized
                    if (n <= active // 70 if decreasing else n >= active // 70)
                ),
                reverse=decreasing,
            )[:3]
            assert d.select_arrays(active, bits, decreasing=decreasing) == tuple(
                expected
            )
    # Public identities from the pinned SDK's independent ordinary-SPL fixture;
    # the account state below is synthetic, not market or native-price evidence.
    address = "EtAdVRLFH22rjWh3mcUasKFF27WtHhsaCvK27tPFFWig"
    mints = ("Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump", a.SOL)
    vaults = (
        "BmW4cCRpJwwL8maFB1AoAuEQf96t64Eq5gUvXikZardM",
        "FDZDrPtCjmSHeq14goCxp5pCJSRekSXY3XSgGz5Rvass",
    )
    raw = bytearray(904)
    raw[:8] = bytes.fromhex("210b3162b565b10d")
    struct.pack_into("<ii", raw, 24, -443636, 443636)
    struct.pack_into("<H", raw, 80, 50)
    for offset, key in (
        (88, mints[0]),
        (120, mints[1]),
        (152, vaults[0]),
        (184, vaults[1]),
        (552, "Fnkg415DEx72GSPooKUWTbPS9wzKucQe4qnvFrrvcZK2"),
    ):
        raw[offset : offset + 32] = bytes(a.Pubkey.from_string(key))
    raw[584:712] = (7 << 511).to_bytes(128, "little")
    pool = d.decode(address, account(d.PROGRAM, raw), None, expected_mints=mints)
    assert pool.arrays[0] == (
        ("5Sm2ecMeqohRkNpFJPWSqHL1BkA7AEW4ck8TmdF1gD4t", 0),
        ("E6gur9Jw8675DCR7GpJVhoSrkruRgt8EdEVqLAc5RLUt", -1),
    )
    for side in (0, 1):
        ix = a.swap(pool, tri.PAYER, mints[side], 1_000_000, 7, exact_output=False)
        expected = [
            address,
            d.PROGRAM,
            *vaults,
            *[
                str(
                    a.get_associated_token_address(
                        tri.PAYER, a.Pubkey.from_string(mints[i])
                    )
                )
                for i in (side, 1 - side)
            ],
            *mints,
            pool.oracle,
            d.PROGRAM,
            str(tri.PAYER),
            a.SPL,
            a.SPL,
            "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
            "D1ZN9Wj1fRSUQfCjhvnu1hqDMT7hzjzBBpi12nVniYD6",
            d.PROGRAM,
            *[key for key, _ in pool.arrays[side]],
        ]
        assert [str(meta.pubkey) for meta in ix.accounts] == expected
        assert [i for i, meta in enumerate(ix.accounts) if meta.is_writable] == [
            0,
            2,
            3,
            4,
            5,
            8,
            16,
            17,
        ]
        assert [i for i, meta in enumerate(ix.accounts) if meta.is_signer] == [10]
        assert bytes(ix.data) == bytes.fromhex(
            "414b3f4ceb5b5b88 40420f0000000000 0700000000000000 00000000"
        )
    assert bytes(
        a.swap(pool, tri.PAYER, mints[0], 7, 1_000_000, exact_output=True).data
    ) == bytes.fromhex("2bd7f784893cf351 40420f0000000000 0700000000000000 00000000")
    bank = {address: account(d.PROGRAM, raw), pool.bitmap: None}
    clock = bytearray(40)
    struct.pack_into("<Q", clock, 0, SLOT)
    bank[a.CLOCK] = account("Sysvar1111111111111111111111111111111111111", clock)
    oracle = bytearray(64)
    oracle[:8] = bytes.fromhex("8bc283b38cb3e5f4")
    struct.pack_into("<Q", oracle, 24, 1)
    bank[pool.oracle] = account(d.PROGRAM, oracle)
    for mint, vault in zip(mints, vaults, strict=True):
        mint_raw, token = bytearray(82), bytearray(165)
        mint_raw[45], token[108] = 1, 1
        token[:32] = bytes(a.Pubkey.from_string(mint))
        token[32:64] = bytes(a.Pubkey.from_string(address))
        bank[mint], bank[vault] = account(a.SPL, mint_raw), account(a.SPL, token)
    for key, index in dict(pool.arrays[0] + pool.arrays[1]).items():
        array = bytearray(10136)
        array[:8] = bytes.fromhex("5c8e5cdc059446b5")
        struct.pack_into("<q", array, 8, index)
        array[16] = 1  # Supported legacy arrays must not be forced to version3.
        array[24:56] = bytes(a.Pubkey.from_string(address))
        bank[key] = account(d.PROGRAM, array)
    d.hydrate(pool, bank, payer=tri.PAYER)
    extension = bytearray(1576)
    extension[:8] = bytes.fromhex("506f7c7137ed1205")
    bank[pool.bitmap] = account(d.PROGRAM, extension)
    check_refused(
        partial(d.hydrate, pool, bank, payer=tri.PAYER), "dlmm_bitmap_identity"
    )
    extension[8:40] = bytes(a.Pubkey.from_string(address))
    bank[pool.bitmap] = account(d.PROGRAM, extension)
    extended = d.hydrate(pool, bank, payer=tri.PAYER)
    ix = a.swap(extended, tri.PAYER, mints[0], 1_000_000, 7, exact_output=False)
    assert str(ix.accounts[1].pubkey) == pool.bitmap and ix.accounts[1].is_writable
    raw[75] = 1
    struct.pack_into("<Q", raw, 816, SLOT + 1)
    bank[address] = account(d.PROGRAM, raw)
    check_refused(partial(d.hydrate, pool, bank, payer=tri.PAYER), "dlmm_not_active")
    raw[752:784] = bytes(tri.PAYER)
    struct.pack_into("<Q", raw, 824, 1)
    bank[address] = account(d.PROGRAM, raw)
    d.hydrate(pool, bank, payer=tri.PAYER)  # Exact privileged activation boundary.
    check_refused(
        partial(d.hydrate, pool, bank, payer=a.Pubkey.default()), "dlmm_not_active"
    )
    raw[86] = 1  # Timestamp activation must not substitute the higher slot clock.
    bank[address] = account(d.PROGRAM, raw)
    check_refused(partial(d.hydrate, pool, bank, payer=tri.PAYER), "dlmm_not_active")
    raw[75] = 0  # An ordinary permissionless pool ignores the activation point.
    bank[address] = account(d.PROGRAM, raw)
    d.hydrate(pool, bank, payer=tri.PAYER)
    selected = pool.arrays[1][-1][0]
    bank[selected] = None
    check_refused(partial(d.hydrate, pool, bank, payer=tri.PAYER), "account_missing")
    del bank[selected]
    check_refused(
        partial(d.hydrate, pool, bank, payer=tri.PAYER),
        "dlmm_array_dependencies_changed",
    )
    raw[880] = 1
    check_refused(
        partial(d.decode, address, account(d.PROGRAM, raw), None, expected_mints=mints),
        "dlmm_spl_only",
    )


def verify_four_leg_boundary() -> None:
    """A third prefix is unpriced; only the fourth leg may close every account."""
    a = tri.atomic
    route = tri.Route(
        "four-leg-check",
        (*ROUTE.mints, pub(80)),
        (*ROUTE.decimals, 9),
        tuple(pub(i) for i in range(40, 44)),
        (a.AMM,) * 4,
        (pub(99),),
        "synthetic_no_market_evidence",
    )
    policy = tri.scope(route, [1_000_000])
    assert policy["quote_legs"] == (1, 2, 3, 4) and policy["max_http"] == 8
    pools = [
        a.Pool(
            address,
            a.AMM,
            (route.mints[i], route.mints[(i + 1) % 4]),
            (pub(50 + i * 2), pub(51 + i * 2)),
        )
        for i, address in enumerate(route.pools)
    ]
    addresses = [
        *route.mints,
        *map(str, route.atas),
        a.SPL,
        a.TIP_ACCOUNT,
        a.AUTHORITY[a.AMM],
        *route.pools,
        *[vault for pool in pools for vault in pool.vaults],
    ]
    raw = bytearray(56)
    struct.pack_into("<IQQ", raw, 0, 1, 2**64 - 1, 1)
    raw += b"".join(bytes(a.Pubkey.from_string(key)) for key in addresses)
    tables = [tri.native.decode_lookup(pub(99), account(tri.native.LUT, raw), SLOT)]
    prefix = tri.build_packet(route, pools, tables, 1_000_000, [379, 111303], BASELINE)
    quote = tri.native_result(
        route, prefix, tables, response_for(prefix, tables, 3, route), BASELINE, 3
    )
    assert quote["status"] == "quote_only" and quote["net_lamports"] is None
    final = tri.build_packet(
        route, pools, tables, 1_000_000, [379, 111303, quote["quantity"]], BASELINE
    )
    result = response_for(final, tables, 4, route)
    assert (
        tri.native_result(route, final, tables, result, BASELINE, 4)["net_lamports"]
        == 1000
    )
    keys = tri.native.resolve_keys(final, tables)
    result["value"]["postBalances"][keys.index(route.atas[3])] = 1
    check_refused(
        partial(tri.native_result, route, final, tables, result, BASELINE, 4),
        "rent_or_inventory_not_closed",
    )


def verify_middle_orca_boundary(bank: dict) -> None:
    """A middle two-hop consumes its non-SOL input; only the final leg is priced."""
    a = tri.atomic
    prior, _ = tri.hydrate(ROUTE, tri.read_pools(ROUTE, bank), bank, SLOT)
    route = tri.Route(
        "middle-orca-check",
        (a.SOL, pub(80), *ROUTE.mints[1:]),
        (9, 9, *ROUTE.decimals[1:]),
        (pub(40), *ROUTE.pools[:2], pub(43)),
        (a.AMM, tri.orca.PROGRAM, tri.orca.PROGRAM, a.AMM),
        (pub(99),),
        "synthetic_no_market_evidence",
    )
    pools = [
        a.Pool(route.pools[0], a.AMM, route.mints[:2], (pub(50), pub(51))),
        replace(prior[0], mints=route.mints[1:3]),
        prior[1],
        a.Pool(route.pools[3], a.AMM, (route.mints[3], a.SOL), (pub(56), pub(57))),
    ]
    addresses = list(
        dict.fromkeys(
            [
                *route.mints,
                *map(str, route.atas),
                a.SPL,
                a.TIP_ACCOUNT,
                a.AUTHORITY[a.AMM],
                *route.pools,
                *[key for pool in pools for key in pool.dependencies()],
            ]
        )
    )
    raw = bytearray(56)
    struct.pack_into("<IQQ", raw, 0, 1, 2**64 - 1, 1)
    raw += b"".join(bytes(a.Pubkey.from_string(key)) for key in addresses)
    tables = [tri.native.decode_lookup(pub(99), account(tri.native.LUT, raw), SLOT)]
    assert route.quote_legs == (1, 3, 4)
    for quantities, legs in (([], 1), ([379], 3), ([379, 111303], 4)):
        tx = tri.build_packet(route, pools, tables, 1_000_000, quantities, BASELINE)
        keys = tri.native.resolve_keys(tx, tables)
        swaps = [
            ix
            for ix in tx.message.instructions
            if str(keys[ix.program_id_index]) in route.programs
        ]
        assert [str(keys[ix.program_id_index]) for ix in swaps] == (
            [a.AMM, tri.orca.PROGRAM, a.AMM][: len(quantities) + 1]
        )
        if quantities:
            # Exact output here could leave a non-SOL remainder and prevent closure.
            assert bytes(swaps[1].data[:8]) == bytes.fromhex("c360ed6c44a2dbe6")
            assert struct.unpack_from("<QQ?", bytes(swaps[1].data), 8) == (379, 1, True)
        response = response_for(tx, tables, legs, route)
        result = tri.native_result(route, tx, tables, response, BASELINE, legs)
        assert (result["net_lamports"] is None) == (legs < 4)
        if legs == 3:
            residual = bytearray(
                base64.b64decode(response["value"]["accounts"][2]["data"][0])
            )
            struct.pack_into("<Q", residual, 64, 1)
            response["value"]["accounts"][2]["data"][0] = base64.b64encode(
                residual
            ).decode()
            check_refused(
                partial(tri.native_result, route, tx, tables, response, BASELINE, legs),
                "prefix_inventory_not_consumed",
            )


def verify_orca_composition_boundaries(bank: dict) -> None:
    """Trailing pairs and Orca runs consume inventory before pricing full closure."""
    a = tri.atomic
    prior, _ = tri.hydrate(ROUTE, tri.read_pools(ROUTE, bank), bank, SLOT)
    for name, programs, boundaries in (
        ("trailing-pair", (a.AMM, tri.orca.PROGRAM, tri.orca.PROGRAM), (1, 3)),
        ("three-orcas", (tri.orca.PROGRAM,) * 3, (2, 3)),
        ("four-orcas", (tri.orca.PROGRAM,) * 4, (2, 4)),
    ):
        count = len(programs)
        route = tri.Route(
            name,
            (*ROUTE.mints, pub(80))[:count],
            (*ROUTE.decimals, 9)[:count],
            tuple(pub(40 + i) for i in range(count)),
            programs,
            (pub(99),),
            "synthetic_no_market_evidence",
        )
        pools = []
        for i, program in enumerate(programs):
            identity = {
                "address": route.pools[i],
                "mints": (route.mints[i], route.mints[(i + 1) % count]),
                "vaults": (pub(50 + i * 2), pub(51 + i * 2)),
            }
            pools.append(
                replace(prior[i % 2], **identity)
                if program == tri.orca.PROGRAM
                else a.Pool(program=program, **identity)
            )
        addresses = list(
            dict.fromkeys(
                [
                    *route.mints,
                    *map(str, route.atas),
                    a.SPL,
                    a.TIP_ACCOUNT,
                    a.AUTHORITY[a.AMM],
                    *route.pools,
                    *[key for pool in pools for key in pool.dependencies()],
                ]
            )
        )
        raw = bytearray(56)
        struct.pack_into("<IQQ", raw, 0, 1, 2**64 - 1, 1)
        raw += b"".join(bytes(a.Pubkey.from_string(key)) for key in addresses)
        tables = [tri.native.decode_lookup(pub(99), account(tri.native.LUT, raw), SLOT)]
        assert route.quote_legs == boundaries
        quantities = []
        for legs in boundaries:
            tx = tri.build_packet(route, pools, tables, 1_000_000, quantities, BASELINE)
            keys = tri.native.resolve_keys(tx, tables)
            swaps = [
                ix
                for ix in tx.message.instructions
                if str(keys[ix.program_id_index]) in programs
            ]
            assert len(swaps) == len(quantities) + 1
            final = legs == count
            for ix, start, end in zip(
                swaps, (0, boundaries[0]), boundaries, strict=False
            ):
                data = bytes(ix.data)
                if programs[start] != tri.orca.PROGRAM:
                    continue
                if end - start == 2:
                    assert data[:8] == bytes.fromhex("c360ed6c44a2dbe6")
                    assert [keys[ix.accounts[j]] for j in (2, 3)] == [
                        a.Pubkey.from_string(pool.address) for pool in pools[start:end]
                    ]
                    # Only the leading SOL pair may cap input using exact-output.
                    expected = (
                        (quantities[0], 1_000_000, False)
                        if final and start == 0
                        else (1_000_000 if start == 0 else quantities[0], 1, True)
                    )
                    assert struct.unpack_from("<QQ?", data, 8) == expected
                else:
                    # Three consecutive Orcas must leave a real exact-input singleton.
                    assert data[:8] == bytes.fromhex("f8c69e91e17587c8")
                    assert keys[ix.accounts[2]] == a.Pubkey.from_string(
                        pools[start].address
                    )
                    assert struct.unpack_from("<QQ", data, 8) == (quantities[0], 1)
                    assert data[40] == 1
            response = response_for(tx, tables, legs, route)
            result = tri.native_result(route, tx, tables, response, BASELINE, legs)
            if not final:
                assert (
                    result["status"] == "quote_only" and result["net_lamports"] is None
                )
                quantities.append(result["quantity"])
                # A consumed mint may not retain even one token beside the output.
                bad = copy.deepcopy(response)
                residual = bytearray(
                    base64.b64decode(bad["value"]["accounts"][legs - 1]["data"][0])
                )
                struct.pack_into("<Q", residual, 64, 1)
                bad["value"]["accounts"][legs - 1]["data"][0] = base64.b64encode(
                    residual
                ).decode()
                check_refused(
                    partial(tri.native_result, route, tx, tables, bad, BASELINE, legs),
                    "prefix_inventory_not_consumed",
                )
            else:
                assert result["status"] == "native_guarded_success"
                assert result["net_lamports"] == a.PROFIT_LAMPORTS
                # Wrong-mode later swaps can strand their non-SOL input. Neither
                # remaining inventory nor rent may be counted as closed-cycle profit.
                input_index = boundaries[0]
                bad = copy.deepcopy(response)
                bad["value"]["postBalances"][keys.index(route.atas[input_index])] = 1
                check_refused(
                    partial(tri.native_result, route, tx, tables, bad, BASELINE, legs),
                    "rent_or_inventory_not_closed",
                )


def verify_damm_boundary() -> None:
    """Protect native ABI direction, legacy/compounding states and public access."""
    a, d = tri.atomic, tri.damm
    # Keys independently decoded from Meteora's pinned public pool_account.bin,
    # Git blob 5727eee1adb3ca79c32c8e2114a2afde8ea7a329 (program a85c926).
    # The remaining state and balances below are synthetic, not native quotes.
    address = "E8zRkDw3UdzRc8qVWmqyQ9MLj7jhgZDHSroYud5t25A7"
    mints = ("CudisfkgWvMKnZ3TWf6iCuHm8pN2ikXhDcWytwz6f6RN", a.SOL)
    vaults = (
        "CRtD3Rct9yse7N33PgHWejrid9Powyg24RGh91R1soVy",
        "1yC3m5qmip5DTTxLQtvWJPYVrNWJJL13TqcMp21cJX7",
    )
    raw = bytearray(1112)
    raw[:8] = bytes.fromhex("f19a6d0411b16dbc")
    raw[16], raw[480], raw[484] = 1, 1, 1
    for offset, key in zip((168, 200, 232, 264), (*mints, *vaults), strict=True):
        raw[offset : offset + 32] = bytes(a.Pubkey.from_string(key))
    pool = d.decode(address, account(d.PROGRAM, raw), expected_mints=mints)
    user = [
        a.get_associated_token_address(tri.PAYER, a.Pubkey.from_string(mint))
        for mint in mints
    ]
    for side in (0, 1):
        ix = a.swap(pool, tri.PAYER, mints[side], 1_000_000, 7, exact_output=False)
        expected = [
            "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",
            address,
            str(user[side]),
            str(user[1 - side]),
            *vaults,
            *mints,
            str(tri.PAYER),
            a.SPL,
            a.SPL,
            d.PROGRAM,
            "3rmHSu74h1ZcmAisVcWerTCiRDQbUrBKmcwptYGjHfet",
            d.PROGRAM,
            "Sysvar1nstructions1111111111111111111111111",
        ]
        assert str(ix.program_id) == d.PROGRAM
        assert bytes(ix.data) == bytes.fromhex(
            "414b3f4ceb5b5b88 40420f0000000000 0700000000000000 00"
        )
        assert [
            (str(meta.pubkey), meta.is_signer, meta.is_writable) for meta in ix.accounts
        ] == [(key, i == 8, 1 <= i <= 5) for i, key in enumerate(expected)]
    assert bytes(
        a.swap(pool, tri.PAYER, mints[0], 7, 1_000_000, exact_output=True).data
    ) == bytes.fromhex("414b3f4ceb5b5b88 0700000000000000 40420f0000000000 02")
    bank = {address: account(d.PROGRAM, raw)}
    clock = bytearray(40)
    struct.pack_into("<Q", clock, 0, SLOT)
    struct.pack_into("<q", clock, 32, 2_000_000)
    bank[a.CLOCK] = account("Sysvar1111111111111111111111111111111111111", clock)
    for mint, vault in zip(mints, vaults, strict=True):
        mint_raw, token = bytearray(82), bytearray(165)
        mint_raw[45], token[108] = 1, 1
        token[:32] = bytes(a.Pubkey.from_string(mint))
        token[32:64] = bytes(a.Pubkey.from_string(expected[0]))
        bank[mint], bank[vault] = account(a.SPL, mint_raw), account(a.SPL, token)
    assert d.hydrate(pool, bank, payer=tri.PAYER) == pool
    raw[484], raw[696] = 2, 1  # Compounding permits zero/unbounded price ranges.
    bank[address] = account(d.PROGRAM, raw)
    assert d.hydrate(pool, bank, payer=tri.PAYER) == pool
    for offset, value, reason in (
        (482, 1, "damm_spl_only"),
        (481, 1, "damm_pool_disabled"),
        (696, 2, "damm_state_version"),
    ):
        bad = raw.copy()
        bad[offset] = value
        check_refused(
            partial(d.decode, address, account(d.PROGRAM, bad), expected_mints=mints),
            reason,
        )
    bad = raw.copy()
    bad[232:264] = bytes(a.Pubkey.from_string(vaults[1]))
    check_refused(
        lambda: d.decode(address, account(d.PROGRAM, bad), expected_mints=mints),
        "damm_vault_pda",
    )
    struct.pack_into("<Q", raw, 472, 2_000_001)
    bank[address] = account(d.PROGRAM, raw)
    check_refused(
        lambda: d.hydrate(pool, bank, payer=tri.PAYER), "damm_pool_not_active"
    )
    raw[296:328] = bytes(tri.PAYER)
    struct.pack_into("<Q", raw, 472, 2_003_600)
    bank[address] = account(d.PROGRAM, raw)
    assert d.hydrate(pool, bank, payer=tri.PAYER) == pool
    struct.pack_into("<Q", raw, 472, 2_003_601)
    bank[address] = account(d.PROGRAM, raw)
    check_refused(
        lambda: d.hydrate(pool, bank, payer=tri.PAYER), "damm_pool_not_active"
    )


async def verify() -> None:
    verify_damm_boundary()
    verify_four_leg_boundary()
    verify_dlmm_boundary()
    bank = fixture()
    verify_middle_orca_boundary(bank)
    verify_orca_composition_boundaries(bank)
    verify_accounting(bank)
    verify_native_guard(bank)
    verify_spot_screen(bank)
    await verify_snapshot_quote(bank)
    await verify_snapshot_prefix(bank)
    await verify_native_snapshot(bank)
    await verify_loopback(bank)
    for storage_failure in (False, True):
        await verify_cancelled_transport(storage_failure)
    print(
        "PASS: canonical atomic Orca two-hop, DLMM/DAMM native ABI and dependencies, quote-boundary HTTP accounting, four-leg prefix/closure boundary, unsigned v0 packets, no dust/rent profit, native one-lamport guard, freshness, loopback sweep, audit failures and cancellation"
    )


if __name__ == "__main__":
    asyncio.run(verify())
