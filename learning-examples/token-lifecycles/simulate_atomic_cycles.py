"""Observe and simulate atomic Raydium cycles; never sign or submit transactions.

Discover SOL/SPL-token AMM v4 and CPMM pools once, then quote same-bank RPC
snapshots, not API prices. Freeze each candidate before delayed simulations.
CLMM, Token-2022 and orderbook-active v4 pools are explicitly outside this probe.
Repeated positive observations of one mint are one episode, not repeated profit.
Simulation success does not establish inclusion, independence or net expectancy.

    uv run learning-examples/token-lifecycles/simulate_atomic_cycles.py --self-check
    uv run learning-examples/token-lifecycles/simulate_atomic_cycles.py \
        --env-file .state/wallets/live-readiness.secrets \
        --payer 9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe --minutes 10

Pool layouts and fees: raydium-sdk-V2/src/raydium/{liquidity,cpmm}/layout.ts;
raydium-cp-swap/programs/cp-swap/src/curve/{calculator,fees}.rs.
Amounts, CU budget, tip and profit floor are fixed research assumptions, not
changes to any bot configuration. Both token accounts must initially be absent;
setup, both swaps, tip and both closes are in the same unsigned transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import struct
import time
from collections import Counter
from dataclasses import dataclass, replace
from itertools import permutations
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from dotenv import dotenv_values
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    CloseAccountParams,
    SyncNativeParams,
    close_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# Binary layout offsets and wire enum values below are protocol constants.
# ruff: noqa: PLR2004
AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
SOL = "So11111111111111111111111111111111111111112"
SPL = str(TOKEN_PROGRAM_ID)
CLOCK = "SysvarC1ock11111111111111111111111111111111"
AUTHORITY = {
    AMM: "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    CPMM: "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
}
TIP_ACCOUNT = "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"
API = "https://api-v3.raydium.io"
FEE_DENOMINATOR = 1_000_000
BUY_LAMPORTS = 10_000_000
CU_LIMIT = 300_000
CU_PRICE = 200_000
NETWORK_FEE = 5_000 + CU_LIMIT * CU_PRICE // FEE_DENOMINATOR
TIP_LAMPORTS = 0  # User decision 2026-09-22 (P2): untipped submission; the frozen
# 10,000-lamport tip exceeded p50 observed cycle gains (9,879 lamports).
PROFIT_LAMPORTS = 1_000
MIN_OUTPUT = BUY_LAMPORTS + NETWORK_FEE + TIP_LAMPORTS + PROFIT_LAMPORTS
READ_METHODS = frozenset({"getMultipleAccounts", "getSlot", "simulateTransaction"})
ROOT = Path(__file__).resolve().parents[2]


def require(condition: bool, message: str) -> None:  # noqa: FBT001
    """Reject unsupported or inconsistent external state."""
    if not condition:
        raise ValueError(message)


def u64(data: bytes, offset: int) -> int:
    """Read a protocol little-endian integer."""
    return struct.unpack_from("<Q", data, offset)[0]


def key(data: bytes, offset: int) -> str:
    """Read a protocol public key."""
    return str(Pubkey.from_bytes(data[offset : offset + 32]))


def checked_data(account: dict | None, owner: str, size: int) -> bytes:
    """Validate ownership and exact layout before decoding an account."""
    require(account is not None, "account_missing")
    require(account["owner"] == owner and not account["executable"], "account_owner")
    require(account["data"][1] == "base64", "account_encoding")
    raw = base64.b64decode(account["data"][0], validate=True)
    require(len(raw) == size, "account_layout")
    return raw


@dataclass(frozen=True, slots=True)
class Pool:
    """One attested pool and, after hydration, executable quote reserves."""

    address: str
    program: str
    mints: tuple[str, str]
    vaults: tuple[str, str]
    config: str | None = None
    observation: str | None = None
    reserves: tuple[int, int] = (0, 0)
    trade_rate: int = 0
    creator_rate: int = 0
    fee_on: int = 0

    def dependencies(self) -> list[str]:
        """Return all accounts required for a same-bank quote."""
        return [
            self.address,
            *self.mints,
            *self.vaults,
            *([self.config] if self.config else []),
        ]

    def quote(self, input_mint: str, amount: int) -> int:
        """Quote exact input with integer fees and full constant-product impact."""
        require(input_mint in self.mints and amount > 0, "quote_input")
        require(min(self.reserves) > 0, "empty_reserves")
        side = self.mints.index(input_mint)
        creator_on_input = self.fee_on == 0 or self.fee_on == side + 1
        rate = self.trade_rate + (self.creator_rate if creator_on_input else 0)
        fee = (amount * rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR
        net = amount - fee
        out = net * self.reserves[1 - side] // (self.reserves[side] + net)
        if not creator_on_input:
            out -= (out * self.creator_rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR
        return max(0, out)


def decode_pool(address: str, account: dict | None) -> Pool:
    """Read canonical pool references, ignoring discovery-service prices/keys."""
    require(account is not None, "pool_missing")
    program = account["owner"]
    require(program in AUTHORITY, "unsupported_program")
    raw = checked_data(account, program, 752 if program == AMM else 637)
    if program == AMM:
        require(u64(raw, 0) == 6, "unsupported_v4_status")
        return Pool(
            address,
            program,
            (key(raw, 400), key(raw, 432)),
            (key(raw, 336), key(raw, 368)),
        )
    require(
        raw[:8] == hashlib.sha256(b"account:PoolState").digest()[:8],
        "pool_discriminator",
    )
    require(not raw[329] & 4, "swap_disabled")
    require((key(raw, 232), key(raw, 264)) == (SPL, SPL), "unsupported_token_program")
    require(raw[389] in (0, 1, 2) and raw[390] in (0, 1), "creator_fee_flags")
    return Pool(
        address,
        program,
        (key(raw, 168), key(raw, 200)),
        (key(raw, 72), key(raw, 104)),
        key(raw, 8),
        key(raw, 296),
    )


def hydrate_pool(pool: Pool, bank: dict[str, dict | None]) -> Pool:
    """Attest references and subtract owed fees from same-bank vault balances."""
    require(
        decode_pool(pool.address, bank[pool.address]) == pool, "pool_references_changed"
    )
    balances = []
    for mint, vault in zip(pool.mints, pool.vaults, strict=True):
        mint_raw = checked_data(bank[mint], SPL, 82)
        require(mint_raw[45] == 1, "mint_uninitialized")
        raw = checked_data(bank[vault], SPL, 165)
        require(
            key(raw, 0) == mint and key(raw, 32) == AUTHORITY[pool.program],
            "vault_identity",
        )
        require(raw[108] == 1, "vault_frozen")
        balances.append(u64(raw, 64))
    raw = base64.b64decode(bank[pool.address]["data"][0])
    if pool.program == AMM:
        balances = [balances[0] - u64(raw, 192), balances[1] - u64(raw, 200)]
        numerator, denominator = u64(raw, 176), u64(raw, 184)
        require(
            denominator > 0 and FEE_DENOMINATOR % denominator == 0, "v4_fee_denominator"
        )
        trade_rate, creator_rate, fee_on = (
            numerator * (FEE_DENOMINATOR // denominator),
            0,
            0,
        )
    else:
        clock = base64.b64decode(bank[CLOCK]["data"][0])
        require(
            u64(raw, 373) <= struct.unpack_from("<q", clock, 32)[0], "pool_not_open"
        )
        config = checked_data(bank[pool.config], CPMM, 236)
        require(
            config[:8] == hashlib.sha256(b"account:AmmConfig").digest()[:8],
            "config_discriminator",
        )
        balances = [
            balances[side]
            - sum(u64(raw, offset + side * 8) for offset in (341, 357, 397))
            for side in (0, 1)
        ]
        trade_rate = u64(config, 12)
        creator_rate = u64(config, 108) if raw[390] else 0
        fee_on = raw[389]
    require(min(balances) > 0, "empty_reserves")
    require(0 <= trade_rate + creator_rate < FEE_DENOMINATOR, "invalid_fee_rate")
    return replace(
        pool,
        reserves=tuple(balances),
        trade_rate=trade_rate,
        creator_rate=creator_rate,
        fee_on=fee_on,
    )


async def rpc(
    session: aiohttp.ClientSession, endpoint: str, method: str, params: list
) -> dict | int:
    """Make only whitelisted read/simulation RPC calls; redact transport secrets."""
    require(method in READ_METHODS, "rpc_method_not_read_only")
    try:
        async with session.post(
            endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        ) as response:
            require(response.status == 200, f"rpc_http_{response.status}")
            payload = await response.json()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ValueError(f"rpc_transport_{type(exc).__name__}") from None
    require("error" not in payload, f"rpc_error_{payload.get('error', {}).get('code')}")
    return payload["result"]


async def bank_read(
    session: aiohttp.ClientSession,
    endpoint: str,
    keys: list[str],
    *,
    minimum_slot: int = 0,
) -> tuple[int, dict]:
    """Read one bank; never combine separately slotted quote snapshots."""
    keys = list(dict.fromkeys(keys))
    require(len(keys) <= 100, "snapshot_account_limit")
    options = {"encoding": "base64", "commitment": "processed"}
    if minimum_slot:
        options["minContextSlot"] = minimum_slot
    result = await rpc(
        session,
        endpoint,
        "getMultipleAccounts",
        [keys, options],
    )
    require(len(result["value"]) == len(keys), "snapshot_count")
    require(result["context"]["slot"] >= minimum_slot, "snapshot_below_slot_floor")
    return result["context"]["slot"], dict(zip(keys, result["value"], strict=True))


async def api_pools(
    session: aiohttp.ClientSession, path: str, params: dict
) -> list[dict]:
    """Discover addresses only; bounded first-page coverage is intentional."""
    async with session.get(API + path, params=params) as response:
        require(response.status == 200, f"discovery_http_{response.status}")
        payload = await response.json()
    require(payload.get("success") is True, "discovery_rejected")
    return payload["data"]["data"]


def emit(out: Any, event: str, **fields: Any) -> None:  # noqa: ANN401
    """Write the same auditable event to stdout and the research tape."""
    row = json.dumps(
        {"event": event, "at": time.time(), **fields}, separators=(",", ":")
    )
    print(row, flush=True)
    out.write(row + "\n")
    out.flush()


async def discover(
    session: aiohttp.ClientSession,
    endpoint: str,
    count: int,
    out: Any,  # noqa: ANN401
) -> dict[str, list[Pool]]:
    """Freeze top-volume SOL mints and up to six standard pools per mint."""
    params = {
        "poolType": "standard",
        "poolSortField": "volume24h",
        "sortType": "desc",
        "pageSize": 100,
        "page": 1,
    }
    leaders = await api_pools(session, "/pools/info/list", params)
    mints = []
    for record in leaders:
        pair = (record["mintA"], record["mintB"])
        if SOL not in {mint["address"] for mint in pair} or any(
            mint["programId"] != SPL for mint in pair
        ):
            continue
        mint = next(mint["address"] for mint in pair if mint["address"] != SOL)
        if mint not in mints:
            mints.append(mint)
    groups = {}
    for mint in mints[:count]:
        params.update(mint1=SOL, mint2=mint, poolSortField="liquidity", pageSize=6)
        records = await api_pools(session, "/pools/info/mint", params)
        addresses = list(
            dict.fromkeys(
                record["id"] for record in records if record["programId"] in AUTHORITY
            )
        )
        if not addresses:
            continue
        _, bank = await bank_read(session, endpoint, addresses)
        pools = []
        for address in addresses:
            try:
                pool = decode_pool(address, bank[address])
                require(set(pool.mints) == {SOL, mint}, "discovery_mint_mismatch")
                pools.append(pool)
            except ValueError as exc:
                emit(out, "excluded", pool=address, reason=str(exc))
        if len(pools) >= 2:
            groups[mint] = pools
        emit(
            out,
            "universe",
            mint=mint,
            pools=[pool.address for pool in pools],
            eligible=len(pools) >= 2,
        )
    require(bool(groups), "no_eligible_pool_pairs")
    return groups


def meta(
    address: str | Pubkey, *, writable: bool = False, signer: bool = False
) -> AccountMeta:
    """Build one explicitly permissioned instruction account."""
    return AccountMeta(Pubkey.from_string(str(address)), signer, writable)


def swap(  # noqa: PLR0913
    pool: Pool,
    payer: Pubkey,
    input_mint: str,
    amount: int,
    bound: int,
    *,
    exact_output: bool,
) -> Instruction:
    """Build an exact-output buy or minimum-output sell, never an unconstrained leg."""
    if pool.program not in AUTHORITY:
        from simulate_orca_cycle import (  # noqa: PLC0415 - adapter imports these helpers
            PROGRAM,
            Whirlpool,
        )

        if isinstance(pool, Whirlpool):
            require(pool.program == PROGRAM, "unsupported_program")
        else:
            import simulate_damm_cycle as damm  # noqa: PLC0415 - only the DAMM lane

            if isinstance(pool, damm.DammPool):
                require(pool.program == damm.PROGRAM, "unsupported_program")
            else:
                import simulate_dlmm_cycle as dlmm  # noqa: PLC0415 - only the DLMM lane

                if isinstance(pool, dlmm.DlmmPool):
                    require(pool.program == dlmm.PROGRAM, "unsupported_program")
                else:
                    import simulate_clmm_cycle as clmm  # noqa: PLC0415 - only the CLMM lane

                    require(
                        isinstance(pool, clmm.ClmmPool)
                        and pool.program == clmm.PROGRAM,
                        "unsupported_program",
                    )
        return pool.swap(payer, input_mint, amount, bound, exact_output=exact_output)
    side = pool.mints.index(input_mint)
    user_in = get_associated_token_address(payer, Pubkey.from_string(input_mint))
    user_out = get_associated_token_address(
        payer, Pubkey.from_string(pool.mints[1 - side])
    )
    if pool.program == AMM:
        accounts = [
            meta(SPL),
            meta(pool.address, writable=True),
            meta(AUTHORITY[AMM]),
            *[meta(vault, writable=True) for vault in pool.vaults],
            meta(user_in, writable=True),
            meta(user_out, writable=True),
            meta(payer, signer=True),
        ]
        data = struct.pack(
            "<BQQ",
            17 if exact_output else 16,
            bound if exact_output else amount,
            amount if exact_output else bound,
        )
    else:
        accounts = [
            meta(payer, signer=True),
            meta(AUTHORITY[CPMM]),
            meta(pool.config),
            meta(pool.address, writable=True),
            meta(user_in, writable=True),
            meta(user_out, writable=True),
            meta(pool.vaults[side], writable=True),
            meta(pool.vaults[1 - side], writable=True),
            meta(SPL),
            meta(SPL),
            meta(input_mint),
            meta(pool.mints[1 - side]),
            meta(pool.observation, writable=True),
        ]
        name = b"global:swap_base_output" if exact_output else b"global:swap_base_input"
        data = hashlib.sha256(name).digest()[:8] + struct.pack(
            "<QQ", bound if exact_output else amount, amount if exact_output else bound
        )
    return Instruction(Pubkey.from_string(pool.program), data, accounts)


def build_cycle(
    buy: Pool,
    sell: Pool,
    payer: Pubkey,
    quantity: int,
    *,
    initial_balance: int | None = None,
) -> Transaction:
    """Build one unsigned cycle with a strict profit floor and full rent recovery."""
    require(
        buy.address != sell.address and set(buy.mints) == set(sell.mints), "cycle_pair"
    )
    require(SOL in buy.mints and quantity > 0, "cycle_quantity")
    if buy.program not in AUTHORITY or sell.program not in AUTHORITY:
        require(
            type(initial_balance) is int
            and initial_balance > BUY_LAMPORTS + NETWORK_FEE + TIP_LAMPORTS,
            "wallet_baseline_required",
        )
    require(
        initial_balance is None or 0 < initial_balance <= 2**64 - 1 - PROFIT_LAMPORTS,
        "wallet_baseline_range",
    )
    mint = next(mint for mint in buy.mints if mint != SOL)
    atas = [
        get_associated_token_address(payer, Pubkey.from_string(mint))
        for mint in (SOL, mint)
    ]
    instructions = [set_compute_unit_limit(CU_LIMIT), set_compute_unit_price(CU_PRICE)]
    instructions += [
        create_idempotent_associated_token_account(
            payer, payer, Pubkey.from_string(mint)
        )
        for mint in (SOL, mint)
    ]
    instructions += [
        transfer(
            TransferParams(from_pubkey=payer, to_pubkey=atas[0], lamports=BUY_LAMPORTS)
        ),
        sync_native(SyncNativeParams(program_id=TOKEN_PROGRAM_ID, account=atas[0])),
    ]
    instructions += [
        swap(buy, payer, SOL, quantity, BUY_LAMPORTS, exact_output=True),
        swap(
            sell,
            payer,
            mint,
            quantity,
            1 if initial_balance is not None else MIN_OUTPUT,
            exact_output=False,
        ),
    ]
    instructions.append(
        transfer(
            TransferParams(
                from_pubkey=payer,
                to_pubkey=Pubkey.from_string(TIP_ACCOUNT),
                lamports=TIP_LAMPORTS,
            )
        )
    )
    instructions += [
        close_account(
            CloseAccountParams(
                program_id=TOKEN_PROGRAM_ID, account=ata, dest=payer, owner=payer
            )
        )
        for ata in reversed(atas)
    ]
    if initial_balance is not None:
        # Same final self-transfer guard as the reference executor: unspent capped
        # input and both reclaimed rents count, not just the sell leg's output.
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=payer,
                    to_pubkey=payer,
                    lamports=initial_balance + PROFIT_LAMPORTS,
                )
            )
        )
    message = Message.new_with_blockhash(instructions, payer, Hash.default())
    require(message.header.num_required_signatures == 1, "unexpected_signer")
    transaction = Transaction.populate(message, [Signature.default()])
    require(len(bytes(transaction)) <= 1232, "transaction_packet_limit")
    return transaction


async def simulate_cycle(  # noqa: PLR0913
    session: aiohttp.ClientSession,
    endpoint: str,
    tx: Transaction,
    slot: int,
    payer: Pubkey,
    *,
    delay: int = 0,
) -> dict:
    """Immediately simulate unsigned bytes; the caller owns any deliberate wait."""
    started = time.monotonic()
    result = await rpc(
        session, endpoint, "simulateTransaction", simulation_params(tx, slot + delay)
    )
    row = simulation_result(tx, result, slot, payer, delay=delay)
    row["elapsed_seconds"] = time.monotonic() - started
    return row


def simulation_params(tx: Transaction, minimum_slot: int) -> list:
    """One unsigned wire contract shared by single and bounded batch calls."""
    require(
        tx.message.header.num_required_signatures == len(tx.signatures) == 1
        and all(signature == Signature.default() for signature in tx.signatures),
        "signed_transaction_rejected",
    )
    require(len(bytes(tx)) <= 1232, "transaction_packet_limit")
    return [
        base64.b64encode(bytes(tx)).decode(),
        {
            "encoding": "base64",
            "sigVerify": False,
            "replaceRecentBlockhash": True,
            "commitment": "processed",
            "minContextSlot": minimum_slot,
        },
    ]


def simulation_result(  # noqa: PLR0913 - shared unsigned transaction boundary
    tx: Transaction,
    result: dict,
    slot: int,
    payer: Pubkey,
    *,
    delay: int = 0,
    initial_balance: int | None = None,
) -> dict:
    """Validate native cashflow; a whole-wallet baseline also applies to failures."""
    addresses = [str(address) for address in tx.message.account_keys]
    payer_index = addresses.index(str(payer))
    require(result["context"]["slot"] >= slot + delay, "simulation_below_slot_floor")
    value = result["value"]
    row = {
        "decision_slot": slot,
        "simulation_slot": result["context"]["slot"],
        "target_delay": delay,
        "actual_delay": result["context"]["slot"] - slot,
        "err": value["err"],
        "fee": value.get("fee"),
        "units": value.get("unitsConsumed"),
        "net_lamports": None,
    }
    if initial_balance is not None:
        guard = tx.message.instructions[-1]
        require(
            addresses[guard.program_id_index] == "11111111111111111111111111111111"
            and list(guard.accounts) == [payer_index, payer_index]
            and guard.data == struct.pack("<IQ", 2, initial_balance + PROFIT_LAMPORTS),
            "simulation_wallet_guard_missing",
        )
        before, after = value.get("preBalances"), value.get("postBalances")
        require(
            isinstance(before, list)
            and isinstance(after, list)
            and len(before) == len(after) == len(addresses),
            "simulation_balance_shape",
        )
        require(
            all(type(balance) is int and balance >= 0 for balance in before + after),
            "simulation_balance_shape",
        )
        require(before[payer_index] == initial_balance, "wallet_baseline_changed")
        require(value.get("fee") == NETWORK_FEE, "simulation_fee_mismatch")
        ata_indices = [tx.message.instructions[index].accounts[1] for index in (2, 3)]
        require(
            all(before[index] == after[index] == 0 for index in ata_indices),
            "simulation_rent_or_inventory_not_closed",
        )
        if value["err"] is not None:
            require(
                after[payer_index] == initial_balance - NETWORK_FEE,
                "simulation_failed_fee_delta",
            )
    if value["err"] is None:
        require(value.get("fee") == NETWORK_FEE, "simulation_fee_mismatch")
        before, after = value.get("preBalances"), value.get("postBalances")
        require(
            isinstance(before, list) and isinstance(after, list),
            "simulation_balances_missing",
        )
        require(len(before) == len(after) == len(addresses), "simulation_balance_shape")
        # Setup is never counted as income: both newly created ATAs must close.
        ata_indices = [tx.message.instructions[index].accounts[1] for index in (2, 3)]
        require(
            all(before[index] == after[index] == 0 for index in ata_indices),
            "simulation_rent_or_inventory_not_closed",
        )
        row["net_lamports"] = after[payer_index] - before[payer_index]
        require(row["net_lamports"] >= PROFIT_LAMPORTS, "simulation_profit_violation")
    else:
        row["logs"] = value.get("logs") or []
    if initial_balance is not None:
        row["raw_result"] = result
        row["guard_rejected"] = value["err"] == {
            "InstructionError": [len(tx.message.instructions) - 1, {"Custom": 1}]
        }
    return row


async def simulate_cycle_batch(  # noqa: PLR0913 - exact two-call simulation boundary
    session: aiohttp.ClientSession,
    endpoint: str,
    transactions: list[Transaction],
    slot: int,
    payer: Pubkey,
    initial_balance: int,
    *,
    record_result: Callable[[object], None],
) -> list[dict]:
    """One paced HTTP request, two correlated read-only methods, no bank pin claim."""
    require(len(transactions) == 2, "simulation_batch_bound")
    started = time.monotonic()
    payload = [
        {
            "jsonrpc": "2.0",
            "id": index,
            "method": "simulateTransaction",
            "params": simulation_params(tx, slot),
        }
        for index, tx in enumerate(transactions)
    ]
    async with session.post(endpoint, json=payload) as response:
        require(response.status == 200, f"rpc_http_{response.status}")
        results = await response.json()
    record_result(results)
    require(isinstance(results, list) and len(results) == 2, "simulation_batch_shape")
    indexed = {}
    for item in results:
        require(
            isinstance(item, dict)
            and item.get("jsonrpc") == "2.0"
            and type(item.get("id")) is int
            and 0 <= item["id"] < 2
            and item["id"] not in indexed,
            "simulation_batch_correlation",
        )
        require(("result" in item) != ("error" in item), "simulation_batch_shape")
        indexed[item["id"]] = item
    rows = []
    for index, tx in enumerate(transactions):
        item = indexed[index]
        if "error" in item:
            require(
                isinstance(item["error"], dict)
                and type(item["error"].get("code")) is int,
                "simulation_batch_error_shape",
            )
            # Bank-not-ready is an explicit gap; other RPC failures stay terminal.
            require(
                item["error"]["code"] == -32016, f"rpc_error_{item['error']['code']}"
            )
            row = {"err": "bank_not_ready", "net_lamports": None, "raw_result": item}
        else:
            result = item["result"]
            require(
                isinstance(result, dict)
                and isinstance(result.get("context"), dict)
                and type(result["context"].get("slot")) is int
                and isinstance(result.get("value"), dict)
                and "err" in result["value"],
                "simulation_batch_result_shape",
            )
            row = simulation_result(
                tx, result, slot, payer, initial_balance=initial_balance
            )
        row["elapsed_seconds"] = time.monotonic() - started
        rows.append(row)
    return rows


async def delayed_simulations(
    session: aiohttp.ClientSession,
    endpoint: str,
    tx: Transaction,
    slot: int,
    payer: Pubkey,
) -> list[dict]:
    """Audit frozen bytes after +2/+5 slots, separately from immediate validation."""
    require(
        all(signature == Signature.default() for signature in tx.signatures),
        "signed_transaction_rejected",
    )
    results = []
    for delay in (2, 5):
        deadline = time.monotonic() + 15
        while (
            await rpc(session, endpoint, "getSlot", [{"commitment": "processed"}])
            < slot + delay
        ):
            require(time.monotonic() < deadline, "slot_wait_timeout")
            await asyncio.sleep(0.1)
        results.append(
            await simulate_cycle(session, endpoint, tx, slot, payer, delay=delay)
        )
    return results


def self_check() -> None:
    """Defend integer fee placement and constant-product round-trip losses."""
    pool = Pool(
        "fixture",
        CPMM,
        (SOL, "token"),
        ("a", "b"),
        reserves=(1_000_000_000, 2_000_000_000),
        trade_rate=3000,
        creator_rate=500,
    )
    amount = 10_000_000
    combined_net = amount - 35_000
    require(
        pool.quote(SOL, amount)
        == combined_net * 2_000_000_000 // (1_000_000_000 + combined_net),
        "input_fee_self_check",
    )
    output_fee_pool = replace(pool, fee_on=2)
    raw_out = 9_970_000 * 2_000_000_000 // 1_009_970_000
    require(
        output_fee_pool.quote(SOL, amount)
        == raw_out - (raw_out * 500 + 999_999) // 1_000_000,
        "output_fee_self_check",
    )
    quantity = pool.quote(SOL, amount)
    after_buy = replace(
        pool, reserves=(pool.reserves[0] + amount, pool.reserves[1] - quantity)
    )
    require(after_buy.quote("token", quantity) < amount, "roundtrip_self_check")
    # A whole raw unit can be economically material for a low-decimal mint.
    buy = replace(
        pool, address="buy", reserves=(9_000_000, 4), creator_rate=0, trade_rate=2500
    )
    sell = replace(buy, address="sell", reserves=(50_000_000, 1))
    require(
        any(row[0] >= 0 for row in quote_pairs([buy, sell], "token")),
        "feasible_quantity_self_check",
    )
    print("PASS: creator fees, lossy round trips, and low-decimal candidate sizing")


def quote_pairs(pools: list[Pool], mint: str) -> list[tuple[int, int, Pool, Pool]]:
    """Price every distinct pool cycle without recomputing each buy quantity."""
    quantities = {pool.address: pool.quote(SOL, BUY_LAMPORTS) for pool in pools}
    return [
        (
            sell.quote(mint, quantities[buy.address]) - MIN_OUTPUT,
            quantities[buy.address],
            buy,
            sell,
        )
        for buy, sell in permutations(pools, 2)
        if quantities[buy.address] > 0
    ]


async def run(args: argparse.Namespace, out: Any) -> None:  # noqa: ANN401, PLR0915
    """Observe a fixed universe without signing or submitting transactions.

    Reset an episode only on observed nonpositive quotes, not missing snapshots.
    """
    require(
        args.env_file.resolve() not in {ROOT / ".env", ROOT / ".env~"},
        "root_env_forbidden",
    )
    endpoint = dotenv_values(args.env_file, interpolate=False).get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    require(bool(endpoint), "rpc_endpoint_missing")
    payer = Pubkey.from_string(args.payer)
    counts: Counter = Counter()
    active: set[str] = set()
    best_margin = None
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        groups = await discover(session, endpoint, args.markets, out)
        wallet_keys = [
            str(payer),
            str(get_associated_token_address(payer, Pubkey.from_string(SOL))),
        ]
        wallet_keys += [
            str(get_associated_token_address(payer, Pubkey.from_string(mint)))
            for mint in groups
        ]
        _, initial_wallet = await bank_read(session, endpoint, wallet_keys)
        require(initial_wallet[str(payer)] is not None, "simulation_payer_missing")
        require(
            all(initial_wallet[address] is None for address in wallet_keys[1:]),
            "simulation_ata_already_exists",
        )
        initial_balance = initial_wallet[str(payer)]["lamports"]
        emit(
            out,
            "probe_ready",
            markets=len(groups),
            pools=sum(map(len, groups.values())),
            buy_lamports=BUY_LAMPORTS,
            network_fee=NETWORK_FEE,
            tip_lamports=TIP_LAMPORTS,
            profit_floor=PROFIT_LAMPORTS,
            minutes=args.minutes,
            initial_wallet_lamports=initial_balance,
        )
        started = time.monotonic()
        deadline = started + args.minutes * 60
        while time.monotonic() < deadline:
            for mint, pools in groups.items():
                if time.monotonic() >= deadline:
                    break
                keys = [
                    CLOCK,
                    *[address for pool in pools for address in pool.dependencies()],
                ]
                slot, bank = await bank_read(session, endpoint, keys)
                live = []
                for pool in pools:
                    try:
                        live.append(hydrate_pool(pool, bank))
                    except ValueError as exc:
                        counts[f"excluded:{exc}"] += 1
                counts["snapshots"] += 1
                quotes = quote_pairs(live, mint)
                counts["pair_quotes"] += len(quotes)
                if not quotes:
                    continue
                margin, quantity, buy, sell = max(quotes, key=lambda row: row[0])
                best_margin = (
                    margin if best_margin is None else max(best_margin, margin)
                )
                if margin < 0:
                    active.discard(mint)
                    continue
                counts["positive_snapshots"] += 1
                if mint in active:
                    continue
                active.add(mint)
                counts["episodes"] += 1
                episode = counts["episodes"]
                emit(
                    out,
                    "candidate",
                    episode=episode,
                    mint=mint,
                    slot=slot,
                    buy=buy.address,
                    sell=sell.address,
                    tokens_raw=quantity,
                    estimated_margin_above_floor=margin,
                )
                tx = build_cycle(buy, sell, payer, quantity)
                immediate = await simulate_cycle(session, endpoint, tx, slot, payer)
                emit(out, "simulation", episode=episode, **immediate)
                counts[
                    "immediate:success"
                    if immediate["err"] is None
                    else "immediate:rejected"
                ] += 1
                for result in await delayed_simulations(
                    session, endpoint, tx, slot, payer
                ):
                    emit(out, "simulation", episode=episode, **result)
                    counts[
                        f"delay_{result['target_delay']}:success"
                        if result["err"] is None
                        else f"delay_{result['target_delay']}:rejected"
                    ] += 1
            await asyncio.sleep(0.25)
        _, final_wallet = await bank_read(session, endpoint, wallet_keys)
        require(
            final_wallet[str(payer)]["lamports"] == initial_balance,
            "wallet_balance_changed_during_probe",
        )
        require(
            all(final_wallet[address] is None for address in wallet_keys[1:]),
            "wallet_accounts_changed_during_probe",
        )
        emit(
            out,
            "summary",
            elapsed_seconds=round(time.monotonic() - started, 3),
            counts=dict(counts),
            best_margin_above_floor=best_margin,
            wallet_lamports=initial_balance,
            actual_submissions=0,
            verdict="simulation_evidence_only_not_verified_expectancy",
        )


def main() -> None:
    """Parse bounded research inputs without automatic dotenv discovery."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--payer")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--markets", type=int, default=12)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.env_file is None or args.payer is None:
        parser.error("--env-file and --payer are required; no private key is used")
    if (
        not math.isfinite(args.minutes)
        or not 0 < args.minutes <= 60
        or not 1 <= args.markets <= 20
    ):
        parser.error("minutes must be in (0, 60], markets in [1, 20]")
    path = args.out or Path(__file__).with_name(
        f"atomic_cycles_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    if path.suffix != ".jsonl" or path.resolve() == args.env_file.resolve():
        parser.error(
            "--out must name a new .jsonl research tape, not a credentials file"
        )
    with path.open("x", encoding="utf-8") as out:
        try:
            asyncio.run(run(args, out))
        except (
            ValueError,
            KeyError,
            TypeError,
            aiohttp.ClientError,
            TimeoutError,
        ) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            emit(out, "fatal", reason=reason)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
