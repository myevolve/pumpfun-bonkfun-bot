"""Simulate a receipt-derived CLMM/PumpSwap cycle; never sign or submit.

Use native SPL-only instructions, not the reference trader's private router.
Quote by simulation so dynamic fees, virtual reserves and buyback are executed,
not guessed. Two fee profiles stay below the existing risk caps; the floor
profile is a diagnostic, not a claim about landing probability.
Reference receipts supply account layout, not necessarily arbitrage evidence.
Live pool/extension bitmaps select initialized tick arrays; Raydium's public
pool-key API supplies an additional lookup-table hint, validated on chain.

The final System Program self-transfer enforces a whole-wallet lamport floor,
after fees, tips and rent cleanup. The simulation's initial balance must match
the frozen baseline. Every run checks the deployed guard's one-lamport boundary.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import aiohttp
import base58
import simulate_atomic_cycles as base
from solana.rpc.async_api import AsyncClient
from solders.address_lookup_table_account import (
    AddressLookupTable,
    AddressLookupTableAccount,
)
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

# Wire offsets and account indexes below are protocol constants.
# ruff: noqa: PLR2004
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
PUMP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
LUT = "AddressLookupTab1e1111111111111111111111111"
REFERENCE = "4Avyz6pAepyvQJZuax5x5VWbBSoCFL5YDaewnjKzNihemCJyMjsRB7s4Anib3Dgf6sZcowL2ZhJurtJd9S9h6v5c"
PAYER = "9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe"
PROFILES = {"configured": (base.CU_PRICE, base.TIP_LAMPORTS), "floor": (0, 1000)}
# ponytail: quote 99% of the cap; search size only if near-floor edges appear.
QUOTE_INPUT = base.BUY_LAMPORTS * 99 // 100


def discriminator(name: str) -> bytes:
    """Return an Anchor instruction discriminator."""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def network_fee(profile: str) -> int:
    """Charge the whole requested CU limit, not just consumed units."""
    return 5000 + (base.CU_LIMIT * PROFILES[profile][0] + 999_999) // 1_000_000


def native_delta(receipt: dict, owner: str) -> int | None:
    """Return rent-adjusted native cashflow, or None when token inventory changed."""
    message, meta = receipt["transaction"]["message"], receipt["meta"]
    index = next(
        i for i, key in enumerate(message["accountKeys"]) if key["pubkey"] == owner
    )
    balances = []
    inventory: Counter = Counter()
    for sign, stage in ((-1, "pre"), (1, "post")):
        owned = [
            row for row in meta[f"{stage}TokenBalances"] if row.get("owner") == owner
        ]
        balances.append(
            meta[f"{stage}Balances"][index]
            + sum(meta[f"{stage}Balances"][row["accountIndex"]] for row in owned)
        )
        for row in owned:
            if row["mint"] != base.SOL:
                inventory[row["mint"]] += sign * int(row["uiTokenAmount"]["amount"])
    return None if any(inventory.values()) else balances[1] - balances[0]


def initialized_array_starts(
    pool: bytes | bytearray, extension: bytes | bytearray
) -> list[int]:
    """Decode initialized arrays, including sparse gaps and negative extensions."""
    span = struct.unpack_from("<H", pool, 235)[0] * 60
    base.require(span > 0, "invalid_tick_spacing")
    maps = [(-512, pool[904:1032])]
    maps += [
        (512 + group * 512, extension[40 + group * 64 : 104 + group * 64])
        for group in range(14)
    ]
    maps += [
        (-1024 - group * 512, extension[936 + group * 64 : 1000 + group * 64])
        for group in range(14)
    ]
    starts = []
    for offset, raw in maps:
        bitmap = int.from_bytes(raw, "little")
        while bitmap:
            bit = (bitmap & -bitmap).bit_length() - 1
            starts.append((offset + bit) * span)
            bitmap &= bitmap - 1
    return sorted(starts)


def verify_tick_bitmap() -> None:
    """Protect signed boundaries and the valid uninitialized-current-array case."""
    pool, extension = bytearray(1544), bytearray(1832)
    struct.pack_into("<H", pool, 235, 10)
    pool[904:1032] = ((1 << (512 - 116)) | (1 << (512 + 155))).to_bytes(128, "little")
    extension[40:104] = (1 << 227).to_bytes(64, "little")
    extension[936:1000] = (1 << 284).to_bytes(64, "little")
    starts = initialized_array_starts(pool, extension)
    base.require(starts == [-444000, -69600, 93000, 443400], "sparse_bitmap_check")
    base.require(
        12600 not in starts
        and max(start for start in starts if start <= 12600) == -69600,
        "sparse_direction_check",
    )


def compile_unsigned(
    payer: Pubkey,
    instructions: list[Instruction],
    tables: list[AddressLookupTableAccount],
) -> VersionedTransaction:
    """Keep one default signature, verified LUTs and the real packet limit."""
    message = MessageV0.try_compile(payer, instructions, tables, base.Hash.default())
    base.require(
        message.header.num_required_signatures == 1
        and message.account_keys[0] == payer,
        "unexpected_signer",
    )
    transaction = VersionedTransaction.populate(message, [Signature.default()])
    base.require(len(bytes(transaction)) <= 1232, "packet_limit_exceeded")
    return transaction


def resolve_keys(
    transaction: VersionedTransaction, tables: list[AddressLookupTableAccount]
) -> list[Pubkey]:
    """Resolve static, all writable, then all readonly v0 lookup accounts."""
    addresses = {table.key: table.addresses for table in tables}
    lookups = transaction.message.address_table_lookups
    return (
        list(transaction.message.account_keys)
        + [
            addresses[row.account_key][index]
            for row in lookups
            for index in row.writable_indexes
        ]
        + [
            addresses[row.account_key][index]
            for row in lookups
            for index in row.readonly_indexes
        ]
    )


def decode_lookup(address: str, account: dict, slot: int) -> AddressLookupTableAccount:
    """Admit only active on-chain lookup tables whose extension slot has passed."""
    base.require(account["data"][1] == "base64", "lookup_encoding")
    raw = base64.b64decode(account["data"][0], validate=True)
    base.checked_data(account, LUT, len(raw))
    table = AddressLookupTable.deserialize(raw)
    base.require(
        table.meta.deactivation_slot == 2**64 - 1
        and table.meta.last_extended_slot < slot,
        "lookup_not_active",
    )
    return AddressLookupTableAccount(Pubkey.from_string(address), table.addresses)


@dataclass
class Route:
    """Attested instruction accounts for one receipt-derived SPL market."""

    payer: Pubkey
    mint: Pubkey
    clmm_accounts: list[AccountMeta]
    pump_accounts: list[AccountMeta]
    tick_arrays: dict[int, Pubkey]
    bitmap: Pubkey
    tables: list[AddressLookupTableAccount]
    volume: Pubkey
    global_volume: Pubkey
    current_start: int
    sol_is_a: bool
    initial_balance: int

    @property
    def atas(self) -> tuple[Pubkey, Pubkey]:
        """Return WSOL and base ATAs, both required absent outside simulations."""
        return self.clmm_accounts[3].pubkey, self.clmm_accounts[4].pubkey

    def setup(self, profile: str) -> list[Instruction]:
        """Fund exactly the cap once; ATA rent is temporary."""
        return [
            base.set_compute_unit_limit(base.CU_LIMIT),
            base.set_compute_unit_price(PROFILES[profile][0]),
            *[
                base.create_idempotent_associated_token_account(
                    self.payer, self.payer, mint
                )
                for mint in (Pubkey.from_string(base.SOL), self.mint)
            ],
            base.transfer(
                base.TransferParams(
                    from_pubkey=self.payer,
                    to_pubkey=self.atas[0],
                    lamports=base.BUY_LAMPORTS,
                )
            ),
            base.sync_native(
                base.SyncNativeParams(base.TOKEN_PROGRAM_ID, self.atas[0])
            ),
        ]

    def closes(self) -> list[Instruction]:
        """Require zero base inventory and recover both temporary ATA rents."""
        return [
            base.close_account(
                base.CloseAccountParams(
                    base.TOKEN_PROGRAM_ID, ata, self.payer, self.payer
                )
            )
            for ata in reversed(self.atas)
        ]

    def guard(self, amount: int) -> Instruction:
        """Enforce a native balance floor without moving funds or adding accounts."""
        return base.transfer(
            base.TransferParams(
                from_pubkey=self.payer, to_pubkey=self.payer, lamports=amount
            )
        )

    async def refresh_ticks(self, session: aiohttp.ClientSession, endpoint: str) -> int:
        """Follow current initialized bitmaps, not the receipt's historical window."""
        pool = self.clmm_accounts[2].pubkey
        slot, bank = await base.bank_read(
            session, endpoint, [str(pool), str(self.bitmap)]
        )
        raw = base.checked_data(bank[str(pool)], CLMM, 1544)
        extension = base.checked_data(bank[str(self.bitmap)], CLMM, 1832)
        base.require(
            raw[:8] == hashlib.sha256(b"account:PoolState").digest()[:8],
            "clmm_pool_identity",
        )
        base.require(
            extension[:8]
            == hashlib.sha256(b"account:TickArrayBitmapExtension").digest()[:8]
            and base.key(extension, 8) == str(pool),
            "bitmap_identity",
        )
        span = struct.unpack_from("<H", raw, 235)[0] * 60
        starts = initialized_array_starts(raw, extension)
        self.current_start = struct.unpack_from("<i", raw, 269)[0] // span * span
        # ponytail: three initialized arrays per direction; wider crossings reject.
        wanted = set(
            sorted(
                (start for start in starts if start <= self.current_start), reverse=True
            )[:3]
        )
        wanted.update([start for start in starts if start >= self.current_start][:3])
        if wanted == self.tick_arrays.keys():
            return slot
        addresses = {
            start: Pubkey.find_program_address(
                [b"tick_array", bytes(pool), struct.pack(">i", start)],
                Pubkey.from_string(CLMM),
            )[0]
            for start in wanted
        }
        tick_slot, ticks = await base.bank_read(
            session, endpoint, [str(address) for address in addresses.values()]
        )
        for start, address in addresses.items():
            data = base.checked_data(ticks[str(address)], CLMM, 10240)
            base.require(
                data[:8] == hashlib.sha256(b"account:TickArrayState").digest()[:8]
                and base.key(data, 8) == str(pool)
                and struct.unpack_from("<i", data, 40)[0] == start,
                "tick_array_identity",
            )
        self.tick_arrays = addresses
        return max(slot, tick_slot)

    def clmm_swap(
        self, amount: int, bound: int, *, input_sol: bool, exact_input: bool
    ) -> Instruction:
        """Use the receipt's native legacy swap, restricted to ordinary SPL mints."""
        accounts = self.clmm_accounts[:9]
        if not input_sol:
            accounts[3], accounts[4] = accounts[4], accounts[3]
            accounts[5], accounts[6] = accounts[6], accounts[5]
        descending = input_sol == self.sol_is_a
        starts = sorted(
            (
                start
                for start in self.tick_arrays
                if (
                    start <= self.current_start
                    if descending
                    else start >= self.current_start
                )
            ),
            reverse=descending,
        )
        base.require(bool(starts), "no_initialized_tick_array_for_direction")
        accounts += [
            base.meta(self.tick_arrays[start], writable=True) for start in starts
        ]
        accounts.append(base.meta(self.bitmap, writable=True))
        data = (
            discriminator("swap")
            + struct.pack("<QQ", amount, bound)
            + bytes(16)
            + bytes([exact_input])
        )
        return Instruction(Pubkey.from_string(CLMM), data, accounts)

    def pump_swap(self, amount: int, bound: int, kind: str) -> Instruction:
        """Preserve current pool/fee accounts; add mandatory volume accounts for buys."""
        accounts = self.pump_accounts[:]
        if kind != "sell":
            accounts[19:19] = [
                base.meta(self.global_volume),
                base.meta(self.volume, writable=True),
            ]
        data = discriminator(kind) + struct.pack("<QQ", amount, bound)
        if kind != "sell":
            data += b"\x00"  # OptionBool(false), one byte, not an option tag.
        return Instruction(Pubkey.from_string(PUMP), data, accounts)

    def build(
        self, direction: str, stage: str, quantity: int, profile: str
    ) -> VersionedTransaction:
        """Build quote, economic screen or guarded cycle; all are unsigned."""
        instructions = self.setup(profile)
        forward = direction == "clmm_to_pump"
        if stage == "quote":
            instructions.append(
                self.clmm_swap(QUOTE_INPUT, 1, input_sol=True, exact_input=True)
                if forward
                else self.pump_swap(QUOTE_INPUT, 1, "buy_exact_quote_in")
            )
        else:
            base.require(quantity > 0, "empty_quote")
            instructions += (
                [
                    self.clmm_swap(
                        quantity, base.BUY_LAMPORTS, input_sol=True, exact_input=False
                    ),
                    self.pump_swap(quantity, 1, "sell"),
                ]
                if forward
                else [
                    self.pump_swap(quantity, base.BUY_LAMPORTS, "buy"),
                    self.clmm_swap(quantity, 1, input_sol=False, exact_input=True),
                ]
            )
            if not forward:
                instructions.append(
                    Instruction(
                        Pubkey.from_string(PUMP),
                        discriminator("close_user_volume_accumulator"),
                        [
                            base.meta(self.payer, writable=True, signer=True),
                            base.meta(self.volume, writable=True),
                            self.pump_accounts[15],
                            self.pump_accounts[16],
                        ],
                    )
                )
            instructions.append(
                base.transfer(
                    base.TransferParams(
                        from_pubkey=self.payer,
                        to_pubkey=Pubkey.from_string(base.TIP_ACCOUNT),
                        lamports=PROFILES[profile][1],
                    )
                )
            )
            instructions += self.closes()
            if stage == "guarded":
                instructions.append(
                    self.guard(self.initial_balance + base.PROFIT_LAMPORTS)
                )
        return compile_unsigned(self.payer, instructions, self.tables)


async def load_route(  # noqa: PLR0915
    session: aiohttp.ClientSession,
    endpoint: str,
    payer: Pubkey,
    signature: str,
    out: TextIO,
) -> Route:
    """Use finalized native swap accounts, never the opaque reference router."""
    async with AsyncClient(endpoint, timeout=30) as client:
        response = await client.get_transaction(
            Signature.from_string(signature),
            encoding="jsonParsed",
            commitment="finalized",
            max_supported_transaction_version=0,
        )
    receipt = json.loads(response.to_json())["result"]
    base.require(
        receipt is not None and receipt["meta"]["err"] is None,
        "reference_not_successful",
    )
    message = receipt["transaction"]["message"]
    flags = {row["pubkey"]: row for row in message["accountKeys"]}
    fee_payer = message["accountKeys"][0]["pubkey"]
    instructions = [
        ix
        for group in receipt["meta"]["innerInstructions"]
        for ix in group["instructions"]
    ]
    clmm = [
        ix
        for ix in instructions
        if ix["programId"] == CLMM
        and "data" in ix
        and base58.b58decode(ix["data"])[:8] == discriminator("swap")
    ]
    pump = [
        ix
        for ix in instructions
        if ix["programId"] == PUMP
        and "data" in ix
        and base58.b58decode(ix["data"])[:8] == discriminator("sell")
    ]
    base.require(len(clmm) == len(pump) == 1, "reference_native_route_shape")
    ca, pa = list(clmm[0]["accounts"]), pump[0]["accounts"]
    base.require(len(ca) >= 10 and len(pa) == 24, "unsupported_reference_accounts")
    owner = ca[0]
    # Source trades may sell base tokens. Our owned template always starts SOL-in.
    source_sells_base = ca[3:5] == [pa[5], pa[6]]
    if source_sells_base:
        ca[3], ca[4] = ca[4], ca[3]
        ca[5], ca[6] = ca[6], ca[5]
    base.require(
        ca[0] == pa[1] and flags[owner]["signer"] and ca[3] == pa[6] and ca[4] == pa[5],
        "reference_user_accounts",
    )
    base.require(
        pa[4] == base.SOL and pa[11] == pa[12] == ca[8] == base.SPL, "spl_sol_only"
    )
    mint = Pubkey.from_string(pa[3])
    atas = [
        base.get_associated_token_address(payer, Pubkey.from_string(value))
        for value in (base.SOL, str(mint))
    ]
    replacements = {owner: str(payer), ca[3]: str(atas[0]), ca[4]: str(atas[1])}

    def metas(addresses: list[str]) -> list[AccountMeta]:
        return [
            base.meta(
                replacements.get(address, address),
                writable=flags[address]["writable"],
                signer=address == owner,
            )
            for address in addresses
        ]

    table_keys = [row["accountKey"] for row in message["addressTableLookups"]]
    # Captured LUTs may omit newly selected arrays in the opposite direction.
    async with session.get(
        base.API + "/pools/key/ids", params={"ids": ca[2]}
    ) as response:
        base.require(response.status == 200, "pool_lookup_http")
        metadata = await response.json()
    base.require(
        metadata.get("success") is True and len(metadata["data"]) == 1,
        "pool_lookup_metadata",
    )
    pool_keys = metadata["data"][0]
    base.require(
        pool_keys["id"] == ca[2] and pool_keys["programId"] == CLMM,
        "pool_lookup_identity",
    )
    pool_lookup = pool_keys.get("lookupTableAccount")
    if pool_lookup and pool_lookup != str(Pubkey.default()):
        table_keys = list(dict.fromkeys([*table_keys, pool_lookup]))
    volume = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(payer)], Pubkey.from_string(PUMP)
    )[0]
    global_volume = Pubkey.find_program_address(
        [b"global_volume_accumulator"], Pubkey.from_string(PUMP)
    )[0]
    bitmap = Pubkey.find_program_address(
        [b"pool_tick_array_bitmap_extension", bytes(Pubkey.from_string(ca[2]))],
        Pubkey.from_string(CLMM),
    )[0]
    slot, bank = await base.bank_read(
        session,
        endpoint,
        [
            *table_keys,
            *ca[1:3],
            *ca[5:9],
            *pa[:1],
            *pa[2:5],
            *pa[7:],
            str(payer),
            *map(str, atas),
            str(volume),
            str(global_volume),
        ],
    )
    base.require(
        bank[str(payer)] is not None
        and all(bank[str(key)] is None for key in [*atas, volume]),
        "simulation_accounts_not_empty",
    )
    for address in (str(mint), base.SOL):
        raw = base.checked_data(bank[address], base.SPL, 82)
        base.require(raw[45] == 1, "mint_uninitialized")
    cp = base64.b64decode(bank[ca[2]]["data"][0])
    pp = base64.b64decode(bank[pa[0]]["data"][0])
    base.require(
        bank[ca[2]]["owner"] == CLMM
        and cp[:8] == hashlib.sha256(b"account:PoolState").digest()[:8]
        and len(cp) >= 273,
        "clmm_pool_identity",
    )
    base.require(
        bank[pa[0]]["owner"] == PUMP
        and pp[:8] == bytes.fromhex("f19a6d0411b16dbc")
        and len(pp) >= 300,
        "pump_pool_identity",
    )
    base.require(
        base.key(cp, 9) == ca[1] and base.key(cp, 201) == ca[7], "clmm_references"
    )
    base.require(
        {base.key(cp, 73), base.key(cp, 105)} == {base.SOL, str(mint)}, "clmm_mints"
    )
    base.require(
        {base.key(cp, 137), base.key(cp, 169)} == {ca[5], ca[6]}, "clmm_vaults"
    )
    base.require(
        [base.key(pp, offset) for offset in (43, 75, 139, 171)]
        == [str(mint), base.SOL, pa[7], pa[8]]
        and pp[243] == pp[244] == 0,
        "pump_references_or_flags",
    )
    for address, token, authority in (
        (ca[5], base.SOL, ca[2]),
        (ca[6], str(mint), ca[2]),
        (pa[7], str(mint), pa[0]),
        (pa[8], base.SOL, pa[0]),
    ):
        raw = base.checked_data(bank[address], base.SPL, 165)
        base.require(
            base.key(raw, 0) == token
            and base.key(raw, 32) == authority
            and raw[108] == 1,
            "vault_identity_or_frozen",
        )
    for index, authority in ((10, pa[9]), (17, pa[18]), (23, pa[22])):
        raw = base.checked_data(bank[pa[index]], base.SPL, 165)
        base.require(
            base.key(raw, 0) == base.SOL
            and base.key(raw, 32) == authority
            and raw[108] == 1,
            "fee_account_prerequisite",
        )
    tables = []
    for address in table_keys:
        tables.append(decode_lookup(address, bank[address], slot))
    route = Route(
        payer,
        mint,
        metas(ca[:9]),
        metas(pa),
        {},
        bitmap,
        tables,
        volume,
        global_volume,
        0,
        base.key(cp, 73) == base.SOL,
        bank[str(payer)]["lamports"],
    )
    await route.refresh_ticks(session, endpoint)
    reference_deltas = [
        native_delta(receipt, address) for address in dict.fromkeys((fee_payer, owner))
    ]
    base.emit(
        out,
        "reference",
        signature=signature,
        slot=receipt["slot"],
        payer=fee_payer,
        trader=owner,
        reference_clmm_input="base" if source_sells_base else "quote",
        reference_inventory_flat=None not in reference_deltas,
        reference_native_net=(
            sum(delta for delta in reference_deltas if delta is not None)
            if None not in reference_deltas
            else None
        ),
        reference_fee=receipt["meta"]["fee"],
        mint=str(mint),
        clmm=ca[2],
        clmm_trade_fee_ppm=struct.unpack_from(
            "<I", base.checked_data(bank[ca[1]], CLMM, 117), 47
        )[0],
        pool_lookup_table=pool_lookup,
        pump=pa[0],
        initial_wallet_lamports=bank[str(payer)]["lamports"],
    )
    return route


async def simulate(  # noqa: PLR0913
    session: aiohttp.ClientSession,
    endpoint: str,
    route: Route,
    transaction: VersionedTransaction,
    profile: str,
    minimum_slot: int = 0,
    *,
    quote: bool = False,
) -> dict:
    """Execute only simulateTransaction and validate fee, cleanup and cashflow."""
    base.require(
        all(signature == Signature.default() for signature in transaction.signatures),
        "signed_transaction_rejected",
    )
    options = {
        "encoding": "base64",
        "sigVerify": False,
        "replaceRecentBlockhash": True,
        "commitment": "processed",
        "minContextSlot": minimum_slot,
    }
    if quote:
        options["accounts"] = {"encoding": "base64", "addresses": [str(route.atas[1])]}
    result = await base.rpc(
        session,
        endpoint,
        "simulateTransaction",
        [base64.b64encode(bytes(transaction)).decode(), options],
    )
    value = result["value"]
    row = {
        "slot": result["context"]["slot"],
        "err": value["err"],
        "fee": value.get("fee"),
        "units": value.get("unitsConsumed"),
        "wire_bytes": len(bytes(transaction)),
    }
    before, after = value.get("preBalances"), value.get("postBalances")
    base.require(
        isinstance(before, list) and before[0] == route.initial_balance,
        "wallet_baseline_changed",
    )
    if value["err"] is not None:
        row["logs"] = (value.get("logs") or [])[-8:]
        return row
    base.require(value.get("fee") == network_fee(profile), "simulation_fee_mismatch")
    if quote:
        raw = base.checked_data(value["accounts"][0], base.SPL, 165)
        base.require(
            base.key(raw, 0) == str(route.mint)
            and base.key(raw, 32) == str(route.payer),
            "quote_account_identity",
        )
        row["quantity"] = base.u64(raw, 64)
    else:
        keys = resolve_keys(transaction, route.tables)
        base.require(
            isinstance(before, list)
            and isinstance(after, list)
            and len(before) == len(after) == len(keys),
            "simulation_balance_shape",
        )
        for address in (*route.atas, route.volume):
            if address in keys:
                index = keys.index(address)
                base.require(
                    before[index] == after[index] == 0, "rent_or_inventory_not_closed"
                )
        row["net_lamports"] = after[0] - before[0]
    return row


async def verify_guard(
    session: aiohttp.ClientSession, endpoint: str, route: Route, out: TextIO
) -> None:
    """Prove exact-balance acceptance and one-lamport-short rejection on chain."""
    verify_tick_bitmap()
    for extra in (0, 1):
        bound = route.initial_balance - network_fee("configured") + extra
        transaction = compile_unsigned(
            route.payer,
            [*route.setup("configured"), *route.closes(), route.guard(bound)],
            route.tables,
        )
        row = await simulate(session, endpoint, route, transaction, "configured")
        base.require(
            row["err"] == ({"InstructionError": [8, {"Custom": 1}]} if extra else None),
            "balance_guard_boundary_failed",
        )
        if not extra:
            base.require(
                row["net_lamports"] == -network_fee("configured"),
                "guard_control_cashflow",
            )
        base.emit(out, "guard_check", required_balance=bound, **row)


async def run(args: argparse.Namespace, out: TextIO) -> None:  # noqa: PLR0915, C901
    """Observe fixed routes, with separate episodes for each diagnostic fee profile."""
    base.require(
        args.env_file.resolve() not in {base.ROOT / ".env", base.ROOT / ".env~"},
        "root_env_forbidden",
    )
    endpoint = base.dotenv_values(args.env_file, interpolate=False).get(
        "SOLANA_NODE_RPC_ENDPOINT"
    )
    base.require(bool(endpoint), "rpc_endpoint_missing")
    counts: Counter = Counter()
    active: set[tuple[str, str]] = set()
    best: dict[str, int] = {}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as session:
        route = await load_route(
            session, endpoint, Pubkey.from_string(args.payer), args.reference, out
        )
        wallet_keys = [str(route.payer), *map(str, route.atas), str(route.volume)]
        _, initial = await base.bank_read(session, endpoint, wallet_keys)
        await verify_guard(session, endpoint, route, out)
        base.emit(
            out,
            "probe_ready",
            minutes=args.minutes,
            input_cap=base.BUY_LAMPORTS,
            quote_input=QUOTE_INPUT,
            profiles={
                name: {"network_fee": network_fee(name), "tip": values[1]}
                for name, values in PROFILES.items()
            },
            floor_profile_is_landing_unverified=True,
        )
        started = time.monotonic()
        while time.monotonic() - started < args.minutes * 60:
            tick_slot = await route.refresh_ticks(session, endpoint)
            for direction in ("clmm_to_pump", "pump_to_clmm"):
                quoted = await simulate(
                    session,
                    endpoint,
                    route,
                    route.build(direction, "quote", 0, "configured"),
                    "configured",
                    tick_slot,
                    quote=True,
                )
                counts["quotes"] += 1
                if quoted["err"] is not None:
                    counts["quote_rejected"] += 1
                    base.emit(out, "quote_rejected", direction=direction, **quoted)
                    continue
                for profile in PROFILES:
                    screen = await simulate(
                        session,
                        endpoint,
                        route,
                        route.build(direction, "screen", quoted["quantity"], profile),
                        profile,
                        quoted["slot"],
                    )
                    counts[f"{direction}:{profile}:screens"] += 1
                    base.emit(
                        out,
                        "screen",
                        direction=direction,
                        profile=profile,
                        quantity=quoted["quantity"],
                        **screen,
                    )
                    if screen["err"] is not None:
                        counts["screen_rejected"] += 1
                        continue
                    key = (direction, profile)
                    label = ":".join(key)
                    best[label] = max(
                        best.get(label, screen["net_lamports"]), screen["net_lamports"]
                    )
                    positive = screen["net_lamports"] >= base.PROFIT_LAMPORTS
                    if not positive:
                        active.discard(key)
                        if counts[f"{label}:negative_controls"]:
                            continue
                        counts[f"{label}:negative_controls"] += 1
                    else:
                        counts[f"{label}:positive_screens"] += 1
                        if key in active:
                            continue
                        active.add(key)
                        counts["episodes"] += 1
                    event = "candidate" if positive else "negative_control"
                    episode = counts["episodes"] if positive else None
                    base.emit(
                        out,
                        event,
                        episode=episode,
                        direction=direction,
                        profile=profile,
                        decision_slot=screen["slot"],
                        quantity=quoted["quantity"],
                        net_lamports=screen["net_lamports"],
                    )
                    transaction = route.build(
                        direction, "guarded", quoted["quantity"], profile
                    )
                    for delay in (2, 5):
                        deadline = time.monotonic() + 15
                        while (
                            await base.rpc(
                                session,
                                endpoint,
                                "getSlot",
                                [{"commitment": "processed"}],
                            )
                            < screen["slot"] + delay
                        ):
                            base.require(
                                time.monotonic() < deadline, "slot_wait_timeout"
                            )
                            await base.asyncio.sleep(0.1)
                        row = await simulate(
                            session,
                            endpoint,
                            route,
                            transaction,
                            profile,
                            screen["slot"] + delay,
                        )
                        if row["err"] is None:
                            base.require(
                                row["net_lamports"] >= base.PROFIT_LAMPORTS,
                                "profit_guard_violation",
                            )
                        counts[
                            f"{label}:{event}:delay_{delay}:"
                            + ("success" if row["err"] is None else "rejected")
                        ] += 1
                        base.emit(
                            out,
                            "delayed",
                            selection=event,
                            episode=episode,
                            direction=direction,
                            profile=profile,
                            target_delay=delay,
                            actual_delay=row["slot"] - screen["slot"],
                            transaction_sha256=hashlib.sha256(
                                bytes(transaction)
                            ).hexdigest(),
                            **row,
                        )
            await base.asyncio.sleep(0.25)
        _, final = await base.bank_read(session, endpoint, wallet_keys)
        base.require(
            final[str(route.payer)]["lamports"] == initial[str(route.payer)]["lamports"]
            and all(final[key] is None for key in wallet_keys[1:]),
            "wallet_changed_during_probe",
        )
        base.emit(
            out,
            "summary",
            elapsed_seconds=round(time.monotonic() - started, 3),
            counts=dict(counts),
            best_net_lamports=best,
            wallet_lamports=final[str(route.payer)]["lamports"],
            actual_submissions=0,
            verdict="simulation_only_not_verified_live_expectancy",
        )


def main() -> None:
    """Run a bounded unsigned experiment with explicit credentials and public payer."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--payer", default=PAYER)
    parser.add_argument("--reference", default=REFERENCE)
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    base.require(0 <= args.minutes <= 20, "duration_out_of_bounds")
    path = args.out or Path(__file__).with_name(
        "reference_cycles_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + ".jsonl"
    )
    base.require(
        path.suffix == ".jsonl" and path.resolve() != args.env_file.resolve(),
        "unsafe_output_path",
    )
    with path.open("x") as out:
        try:
            base.asyncio.run(run(args, out))
        except Exception as exc:  # noqa: BLE001 - abort; never expose endpoint credentials
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            base.emit(out, "fatal", reason=reason)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
