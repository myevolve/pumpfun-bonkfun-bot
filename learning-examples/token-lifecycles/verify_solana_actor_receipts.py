"""Read-only, bounded Solana receipt discovery and preregistered actor holdout.

Cloud (existing grpcio/base58/protobuf dependencies and src/geyser stubs required)::

    python learning-examples/token-lifecycles/verify_solana_actor_receipts.py \
      --credentials /explicit/provider.json --out /exclusive/receipts.jsonl \
      --seconds 3600 --discovery-seconds 600 --sample-per-minute 120 \
      --cohort-size 3 --max-updates 20000000 --max-wire-bytes 17179869184 \
      --max-output-bytes 268435456 --max-receipts 50000 --max-message-bytes 2000000 \
      --max-requests 2000 --idle-seconds 45 --sample-seed solana-receipts-v1
    python learning-examples/token-lifecycles/verify_solana_actor_receipts.py --self-check
    python learning-examples/token-lifecycles/verify_solana_actor_receipts.py \
      --analyze /exclusive/receipts.jsonl

Credentials JSON: geyser_endpoint (https URL with no path/query/userinfo),
geyser_token; optional rpc_url is accepted for compatibility but NEVER used.
No environment/default credential reads, HTTP/RPC requests, signing or writes to
chain. One TLS Subscribe, x-token metadata, no reconnect. The subscription is ALL
non-vote finalized transactions from the outset: avoids an unacknowledged dynamic
filter change losing early holdout actor receipts. Provider must permit that feed;
this is materially more bandwidth than a venue filter. Local discovery eligibility
is any resolved account reference to the six named programs (NOT success or profit).
Only actor-address matches are retained in holdout, on any venue or no venue.
Wire counters meter delivered protobuf payload bytes, excluding TLS/gRPC overhead;
the first over-budget message stops the stream (one-message overshoot plus transport
buffering is possible). Incoming 16 GiB and retained 256 MiB ceilings are separate.
Finalized batching/receive silence is NOT execution latency: a silence ceiling
censors this observer, never labels transactions slow or failed.

Sampling preregistration: each monotonic receive-time minute keeps the K smallest
SHA256(seed || NUL || signature) priorities, with signature as tiebreaker. Each
minute is finalized before its receipts enter discovery statistics. Selection is
independent of execution/outcome, spans the whole discovery period, and is bounded
by K * max-message-bytes raw memory. Stop rather than silently censor on ceilings.
The cohort is frozen at discovery-seconds: among sampled fee payers with at least
one successful plausible closed native cycle, rank by descending COUNT of such
cycles, then SHA256(seed || NUL || payer), then address, take cohort-size. Neither
profit sign nor magnitude ranks actors. This is still discovery/survivorship bias;
unsampled actors, sponsored traders, unknown shapes and non-native strategies are
not represented. Empty discovery-selected cohort is a valid result. The predeclared
historical control CQwT1byuHgjKnL6vzmuNaAywKfDBVxDmFVgsQDBWxcWt is ALWAYS added
to holdout, separately labeled, regardless of discovery evidence; up to three other
actors are selected. Prior success does not imply future success. Discovery control
receipts remain sampled; the control's full address-match denominator starts at the
holdout boundary. No rolling reselection. Record or byte ceilings CENSOR the run.

JSONL schema version 1 (all financial fields raw integer decimal strings):
* manifest: immutable limits, selection/cohort rules, program IDs/source references,
  protocol/source hashes and credential-free source identity, setup clock anchor.
* stream_ready: monotonic/Unix observation anchor after subscription metadata;
  the discovery/holdout windows exclude setup and connection time.
* receipt: phase, receive_unix_ns/receive_mono_ns/elapsed_ns, slot, index, signature,
  priority, source_id, raw_b64 (EXACT SubscribeUpdate wire bytes), raw_sha256,
  raw_size, actor_matches and analysis. Raw schema is the bundled geyser.proto /
  solana-storage.proto, not JSON-rounded protobuf financial values.
* minute: receive-time window, denominators, sampled retained/rejected/evicted,
  transport timing, slot range, known drops and separate discovery/holdout summaries.
* cohort: freeze time, deterministic evidence counts and chosen addresses, coverage.
* terminal: reason, complete flag, counters and coverage limits; summaries are on
  preceding minute rows and recomputed offline even for censored tapes.
All rows have seq/event/schema; lines are flushed immediately. Raw receipts are
written on minute finalization (discovery) or immediately (holdout). Compact minute
counts also go to stdout; full summaries and raw protobuf remain in the tape.
Fatal errors use bounded local codes, never provider error details. A reserved output tail allows a
terminal record on the configured disk ceiling (actual OS disk failure goes stderr).
Offline analyzer streams the tape, verifies hashes/decoding/accounting and cohort
selection, detects missing terminal/sequence gaps, and prints separate phase actor
and route totals. Never projects a daily profit or counts fee-only balance changes
as trade profits. Partial tapes retain useful observed lower-bound evidence.

Accounting: account keys = static + loaded writable + loaded readonly. Executed
programs come from runtime invocation logs and recorded inner instructions; message
programs are separately labeled declared (failed transactions can stop early).
Fee-payer native holdings = payer SOL + raw WSOL + native lamports held in known
payer-owned token accounts minus the WSOL already counted. The last component
keeps rent reserves in wealth: account closure is not income and rent funding is
not a trading loss. Unknown/missing owner, unresolved balances, extensions or account
shape makes full native accounting unknown, not zero. All non-SOL inventory deltas
are retained; non-flat inventory excludes closed-cycle interpretation. Known-token
wealth excludes unknown non-token PDA holdings, off-chain payments and other actors.
Closed-cycle evidence additionally requires committed owned WSOL debit AND credit,
a non-native token roundtrip in the instruction trace, at least one executed target
venue, complete inner instructions and transfer/accounting reconciliation. Same-
program cross-pool cycles are eligible; other legs need not use a named program.
This is target-involved plausible closed native cashflow, NOT proof of pure arbitrage,
an ordered pool route, or whole-wallet profitability.
Fees are already in payer SOL delta; failed fees are reported, never subtracted twice.
Explicit outbound System transfers are included as costs; external inbound SOL
prevents closed-cycle classification. An exact failed fee-only lamport vector
retains its known fee loss even when rolled-back token provenance is unavailable.

Protocol references: src/geyser/proto/{geyser,solana-storage}.proto;
https://github.com/rpcpool/yellowstone-grpc/tree/master/yellowstone-grpc-proto/proto
https://solana.com/docs/rpc/json-structures
https://www.solana-program.com/docs/token (Transfer, TransferChecked, InitializeAccount,
CloseAccount; Token-2022 extensions intentionally not assumed equivalent).
Provider convention: simulate_geyser_cycles.py:credentials/geyser. Cashflow precedent:
simulate_reference_cycles.py:native_delta and actor_routes_20260907.json. Program
IDs below are copied from named local sources, not discovered from token symbols.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import heapq
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import base58
import grpc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from src.geyser.generated import geyser_pb2  # noqa: E402

# Protocol widths, bounded study limits and regression quantities stay explicit.
# ruff: noqa: PLR2004

SOL = "So11111111111111111111111111111111111111112"
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PROGRAMS = {
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
}
CONTROL = "CQwT1byuHgjKnL6vzmuNaAywKfDBVxDmFVgsQDBWxcWt"
SOURCES = [
    "learning-examples/token-lifecycles/simulate_atomic_cycles.py",
    "learning-examples/token-lifecycles/simulate_reference_cycles.py",
    "learning-examples/token-lifecycles/simulate_orca_cycle.py",
    "learning-examples/token-lifecycles/fresh_actor_cohort_20260911.json",
    "src/geyser/proto/geyser.proto",
    "src/geyser/proto/solana-storage.proto",
    "src/geyser/generated/geyser_pb2.py",
    "src/geyser/generated/solana_storage_pb2.py",
]
LIMITATIONS = [
    "One provider's finalized non-vote stream; no replay, no independent completeness attestation.",
    "No slot/block census: slot jumps and receipt silence are timing diagnostics, not proven missing receipts.",
    "Receive-time split, not execution-time split; finalized feed latency is not leader/landing latency.",
    "Discovery samples program references, including failed/unexecuted attempts; not every referenced venue executed.",
    "Cohort is discovery-selected, not representative; repeated actor/route receipts are correlated.",
    "Holdout matches resolved actor addresses, not token-account owners absent from message keys; such owner-only activity is missing.",
    "Sponsored actors, unknown PDAs, off-chain transfers, Token-2022 extensions and multi-transaction inventory are incomplete.",
    "Observed receipt cashflow is not pure arbitrage attribution, portfolio profitability, fill probability or daily profit.",
    "No chain identity RPC attestation; source is explicit endpoint hash plus TLS/provider subscription.",
]
INVOKE = re.compile(r"^Program ([1-9A-HJ-NP-Za-km-z]{32,44}) invoke \[([0-9]+)\]$")
TAIL_RESERVE = 1_000_000


class StudyError(Exception):
    """Local, credential-free terminal reason."""


def require(ok: bool, code: str) -> None:  # noqa: FBT001
    if not ok:
        raise StudyError(code)


def key(value: bytes) -> str:
    require(len(value) == 32, "invalid_account_key")
    return base58.b58encode(value).decode("ascii")


def priority(seed: str, identity: str) -> int:
    return int.from_bytes(
        hashlib.sha256((seed + "\0" + identity).encode()).digest(), "big"
    )


def resolved_keys(info: object) -> list[bytes]:
    msg, meta = info.transaction.message, info.meta
    keys = [
        *msg.account_keys,
        *meta.loaded_writable_addresses,
        *meta.loaded_readonly_addresses,
    ]
    require(all(len(value) == 32 for value in keys), "invalid_account_key")
    expected_w = sum(len(v.writable_indexes) for v in msg.address_table_lookups)
    expected_r = sum(len(v.readonly_indexes) for v in msg.address_table_lookups)
    require(
        expected_w == len(meta.loaded_writable_addresses), "loaded_writable_mismatch"
    )
    require(
        expected_r == len(meta.loaded_readonly_addresses), "loaded_readonly_mismatch"
    )
    require(bool(keys), "missing_payer")
    return keys


def decode(raw: bytes) -> object:
    update = geyser_pb2.SubscribeUpdate()
    update.ParseFromString(raw)
    return update


def account_receipt(update: object) -> dict:  # noqa: C901, PLR0912, PLR0915
    """Decode exact raw quantities; unknown accounting remains explicitly null."""
    require(update.WhichOneof("update_oneof") == "transaction", "not_transaction")
    info = update.transaction.transaction
    require(
        info.HasField("transaction") and info.HasField("meta"),
        "missing_receipt_metadata",
    )
    meta, msg = info.meta, info.transaction.message
    keys = [key(value) for value in resolved_keys(info)]
    payer = keys[0]
    failed = meta.HasField("err")
    issues: set[str] = set()
    balance_ok = len(meta.pre_balances) == len(meta.post_balances) == len(keys)
    if not balance_ok:
        issues.add("balance_vector_shape")
    if not msg.HasField("header") or msg.header.num_required_signatures < 1:
        issues.add("payer_signature_shape")
    instructions = []
    declared = []
    groups = {group.index: group for group in meta.inner_instructions}
    if len(groups) != len(meta.inner_instructions):
        issues.add("duplicate_inner_group")
    for top_index, ix in enumerate(msg.instructions):
        require(ix.program_id_index < len(keys), "program_index_out_of_bounds")
        declared.append(keys[ix.program_id_index])
        instructions.append(("top", top_index, ix))
        if top_index in groups:
            instructions.extend(
                ("inner", top_index, inner) for inner in groups[top_index].instructions
            )
    if any(index >= len(msg.instructions) for index in groups):
        issues.add("inner_group_out_of_bounds")
    for _, _, ix in instructions:
        require(
            ix.program_id_index < len(keys) and all(i < len(keys) for i in ix.accounts),
            "instruction_index_out_of_bounds",
        )
    invocations = [
        match.group(1) for log in meta.log_messages if (match := INVOKE.fullmatch(log))
    ]
    inner_programs = [
        keys[ix.program_id_index] for level, _, ix in instructions if level == "inner"
    ]
    executed = list(
        dict.fromkeys([*invocations, *inner_programs, *([] if failed else declared)])
    )
    venues = sorted(PROGRAMS[p] for p in executed if p in PROGRAMS)
    if meta.inner_instructions_none:
        issues.add("inner_instructions_unavailable")

    # A missing endpoint is zero ONLY when the account's endpoint lamports are zero.
    stages: list[dict] = []
    identity: dict[int, tuple[str, str, str]] = {}
    for rows in (meta.pre_token_balances, meta.post_token_balances):
        stage = {}
        for row in rows:
            i = row.account_index
            require(i < len(keys), "token_index_out_of_bounds")
            require(i not in stage, "duplicate_token_balance")
            text = row.ui_token_amount.amount
            require(
                bool(text) and text.isascii() and text.isdecimal(),
                "invalid_raw_token_amount",
            )
            amount = int(text)
            require(amount < 2**64, "token_amount_overflow")
            ident = (row.owner, row.mint, row.program_id)
            if not row.owner or not row.mint or not row.program_id:
                issues.add("missing_token_identity")
            if i in identity and identity[i] != ident:
                issues.add("token_identity_changed")
            identity[i] = ident
            stage[i] = amount
            if row.program_id != SPL:
                issues.add("token_extensions_or_unknown_program")
        stages.append(stage)
    pre, post = stages
    owned = {i for i, ident in identity.items() if ident[0] == payer}
    inventory: Counter = Counter()
    wsol = [0, 0]
    token_lamports = [0, 0]
    account_changes = []
    inventory_known = balance_ok and not issues.intersection(
        {
            "missing_token_identity",
            "token_identity_changed",
            "token_extensions_or_unknown_program",
        }
    )
    if balance_ok:
        for i, (owner, mint, _) in identity.items():
            for side, stage in enumerate(stages):
                lamports = (meta.pre_balances, meta.post_balances)[side][i]
                if i not in stage and lamports != 0:
                    issues.add("missing_nonempty_token_endpoint")
                    inventory_known = False
            if i not in owned:
                continue
            before, after = pre.get(i), post.get(i)
            if before is None and meta.pre_balances[i] == 0:
                before = 0
            if after is None and meta.post_balances[i] == 0:
                after = 0
            if before is None or after is None:
                continue
            if mint == SOL:
                wsol[0] += before
                wsol[1] += after
                if meta.pre_balances[i] < before or meta.post_balances[i] < after:
                    issues.add("wsol_exceeds_account_lamports")
                    inventory_known = False
            else:
                inventory[mint] += after - before
            token_lamports[0] += meta.pre_balances[i]
            token_lamports[1] += meta.post_balances[i]
            account_changes.append(
                {
                    "address": keys[i],
                    "mint": mint,
                    "owner": owner,
                    "pre_raw": str(before),
                    "post_raw": str(after),
                    "pre_lamports": str(meta.pre_balances[i]),
                    "post_lamports": str(meta.post_balances[i]),
                    "created": meta.pre_balances[i] == 0 < meta.post_balances[i],
                    "closed": meta.post_balances[i] == 0 < meta.pre_balances[i],
                }
            )

    # Endpoint-absent temporary accounts can be identified by committed SPL init.
    closes = []
    transfers = []
    system_transfers = []
    for level, top_index, ix in instructions:
        pid, data, accounts = (
            keys[ix.program_id_index],
            bytes(ix.data),
            list(ix.accounts),
        )
        if (
            pid == "11111111111111111111111111111111"
            and data[:4] == b"\x02\x00\x00\x00"
        ):
            if len(data) != 12 or len(accounts) < 2:
                issues.add("system_transfer_shape")
            else:
                system_transfers.append(
                    (
                        level,
                        top_index,
                        accounts[0],
                        accounts[1],
                        int.from_bytes(data[4:], "little"),
                    )
                )
        if pid == TOKEN2022:
            issues.add("token_extensions_or_unknown_program")
            inventory_known = False
        if pid != SPL or not data:
            continue
        op = data[0]
        if not failed and op in (1, 16, 18):
            expected = 1 if op == 1 else 33
            if len(data) != expected or len(accounts) < (3 if op == 1 else 2):
                issues.add("initialize_account_shape")
                continue
            i, mint_index = accounts[:2]
            owner = keys[accounts[2]] if op == 1 else key(data[1:33])
            ident = (owner, keys[mint_index], SPL)
            if i in identity and identity[i] != ident:
                issues.add("initialized_identity_conflict")
            elif i not in identity:
                if balance_ok and meta.pre_balances[i] == meta.post_balances[i] == 0:
                    identity[i] = ident
                else:
                    issues.add("initialized_account_missing_balance")
        if op == 9:
            if len(data) != 1 or len(accounts) < 3:
                issues.add("close_account_shape")
            else:
                closes.append(
                    {
                        "account": keys[accounts[0]],
                        "destination": keys[accounts[1]],
                        "authority": keys[accounts[2]],
                        "committed": not failed,
                    }
                )
        if op in (3, 12):
            if len(data) != (9 if op == 3 else 10) or len(accounts) < (
                3 if op == 3 else 4
            ):
                issues.add("transfer_shape")
                continue
            source, dest = accounts[0], accounts[1 if op == 3 else 2]
            transfers.append(
                (
                    level,
                    top_index,
                    source,
                    dest,
                    int.from_bytes(data[1:9], "little"),
                    None if op == 3 else keys[accounts[1]],
                )
            )
    known_token_addresses = {keys[i] for i in identity}
    for close in closes:
        if close["account"] not in known_token_addresses:
            issues.add("close_identity_unknown")

    flows: dict[str, Counter] = {}
    transfer_rows = []
    for level, top_index, source, dest, amount, explicit_mint in transfers:
        left, right = identity.get(source), identity.get(dest)
        if not left or not right:
            issues.add("transfer_identity_unknown")
            continue
        if left[1] != right[1] or explicit_mint not in (None, left[1]):
            issues.add("transfer_mint_mismatch")
            continue
        mint = left[1]
        direction = None
        if left[0] == payer != right[0]:
            direction = "out"
        elif right[0] == payer != left[0]:
            direction = "in"
        if direction:
            flows.setdefault(mint, Counter())[direction] += amount
            transfer_rows.append(
                {
                    "level": level,
                    "top_index": top_index,
                    "source": keys[source],
                    "destination": keys[dest],
                    "mint": mint,
                    "direction": direction,
                    "amount_raw": str(amount),
                    "committed": not failed,
                }
            )
    native_owners = {
        0,
        *(index for index, ident in identity.items() if ident[0] == payer),
    }
    system_flow: Counter = Counter()
    system_rows = []
    for level, top_index, source, dest, amount in system_transfers:
        direction = (
            "out"
            if source in native_owners and dest not in native_owners
            else "in"
            if dest in native_owners and source not in native_owners
            else None
        )
        if direction is not None:
            if not failed:
                system_flow[direction] += amount
            system_rows.append(
                {
                    "level": level,
                    "top_index": top_index,
                    "source": keys[source],
                    "destination": keys[dest],
                    "direction": direction,
                    "amount_lamports": str(amount),
                    "committed": not failed,
                }
            )
    native_flow = flows.get(SOL, Counter())
    if not identity and (SPL in executed or TOKEN2022 in executed):
        issues.add("missing_all_token_metadata")
        inventory_known = False
    trace_known = not issues.intersection(
        {
            "transfer_identity_unknown",
            "transfer_mint_mismatch",
            "transfer_shape",
            "inner_instructions_unavailable",
            "token_extensions_or_unknown_program",
            "missing_all_token_metadata",
            "missing_token_identity",
            "system_transfer_shape",
        }
    )
    wallet_delta = meta.post_balances[0] - meta.pre_balances[0] if balance_ok else None
    known = (
        balance_ok
        and inventory_known
        and not issues.intersection(
            {
                "payer_signature_shape",
                "initialized_identity_conflict",
                "initialized_account_missing_balance",
                "wsol_exceeds_account_lamports",
                "initialize_account_shape",
                "close_account_shape",
                "transfer_identity_unknown",
                "transfer_mint_mismatch",
                "transfer_shape",
                "close_identity_unknown",
            }
        )
    )
    wsol_delta = wsol[1] - wsol[0] if known else None
    reserve_delta = (
        token_lamports[1] - token_lamports[0] - (wsol[1] - wsol[0]) if known else None
    )
    native_delta = (
        wallet_delta + token_lamports[1] - token_lamports[0] if known else None
    )
    flat = not any(inventory.values()) if inventory_known else None
    fee_only = failed and wallet_delta == -meta.fee
    if failed:
        fee_vector = (
            balance_ok
            and fee_only
            and "payer_signature_shape" not in issues
            and all(
                before == after
                for before, after in zip(
                    meta.pre_balances[1:], meta.post_balances[1:], strict=True
                )
            )
        )
        if fee_vector:
            known = True
            native_delta = -meta.fee
            wsol_delta = reserve_delta = 0 if inventory_known else None
        else:
            issues.add("failed_native_nonfee_change")
            known = False
            wsol_delta = reserve_delta = native_delta = None
    reconciles = (
        native_delta is not None
        and native_flow["in"]
        - native_flow["out"]
        + system_flow["in"]
        - system_flow["out"]
        - meta.fee
        == native_delta
    )
    roundtrip = any(
        mint != SOL and amounts["in"] > 0 and amounts["out"] > 0
        for mint, amounts in flows.items()
    )
    cycle = (
        not failed
        and known
        and flat is True
        and bool(venues)
        and roundtrip
        and native_flow["in"] > 0
        and native_flow["out"] > 0
        and system_flow["in"] == 0
        and reconciles
        and not issues
    )
    return {
        "payer": payer,
        "successful": not failed,
        "error_b64": base64.b64encode(meta.err.err).decode() if failed else None,
        "fee_lamports": str(meta.fee),
        "wallet_sol_delta_lamports": None
        if wallet_delta is None
        else str(wallet_delta),
        "wsol_delta_lamports": None if wsol_delta is None else str(wsol_delta),
        "owned_token_native_reserve_delta_lamports": None
        if reserve_delta is None
        else str(reserve_delta),
        "known_owned_native_delta_lamports": None
        if native_delta is None
        else str(native_delta),
        "native_accounting_known": known,
        "inventory_known": inventory_known,
        "inventory_flat": flat,
        "non_sol_inventory_delta_raw": {
            mint: str(value) for mint, value in sorted(inventory.items())
        },
        "owned_token_accounts": account_changes,
        "close_instructions": closes,
        "fee_only_failed_wallet": fee_only,
        "failed_fee_lamports": str(meta.fee if failed else 0),
        "declared_top_programs": declared,
        "recorded_inner_programs": inner_programs,
        "logged_invocations": invocations,
        "executed_programs": executed,
        "executed_venues": venues,
        "route": "+".join(venues) or "no_known_executed_venue",
        "native_transfer_trace": transfer_rows,
        "system_transfer_trace": system_rows,
        "system_native_in_lamports": str(system_flow["in"]),
        "system_native_out_lamports": str(system_flow["out"]),
        "native_out_raw": str(native_flow["out"]) if trace_known else None,
        "native_in_raw": str(native_flow["in"]) if trace_known else None,
        "trace_reconciles_after_fee": reconciles,
        "plausible_closed_native_cycle": bool(cycle),
        "unknown_reasons": sorted(issues),
        "loaded_keys": len(keys) - len(msg.account_keys),
    }


class Summary:
    """Bounded by retained discovery count and the frozen cohort in holdout."""

    def __init__(self) -> None:
        self.actors: dict[str, Counter] = {}
        self.routes: dict[str, Counter] = {}

    def add(self, analysis: dict, actors: list[str]) -> None:
        for table, names in ((self.actors, actors), (self.routes, [analysis["route"]])):
            for name in names:
                count = table.setdefault(name, Counter())
                count["receipts"] += 1
                count["successful" if analysis["successful"] else "failed"] += 1
                # Actors included but not paying are not assigned another payer's P&L.
                if table is self.actors and name != analysis["payer"]:
                    count["nonpayer_receipts_unaccounted"] += 1
                    continue
                count["fee_lamports"] += int(analysis["fee_lamports"])
                count["failed_fee_lamports"] += int(analysis["failed_fee_lamports"])
                if analysis["inventory_known"]:
                    for mint, delta in analysis["non_sol_inventory_delta_raw"].items():
                        count[f"inventory_raw:{mint}"] += int(delta)
                value = analysis["known_owned_native_delta_lamports"]
                if value is None:
                    count["unknown_native_receipts"] += 1
                else:
                    count["known_native_receipts"] += 1
                    count["known_native_delta_lamports"] += int(value)
                if analysis["inventory_flat"] is False:
                    count["nonflat_inventory_receipts"] += 1
                elif analysis["inventory_flat"] is None:
                    count["unknown_inventory_receipts"] += 1
                if analysis["plausible_closed_native_cycle"]:
                    count["closed_cycles"] += 1
                    count["closed_native_delta_lamports"] += int(value)
                    count[
                        "positive_cycles"
                        if int(value) > 0
                        else "negative_cycles"
                        if int(value) < 0
                        else "zero_cycles"
                    ] += 1

    def export(self) -> dict:
        def rows(table: dict) -> dict:
            return {
                name: {
                    k: str(v)
                    if k.endswith("lamports") or k.startswith("inventory_raw:")
                    else v
                    for k, v in sorted(count.items())
                }
                for name, count in sorted(table.items())
            }

        return {"actors": rows(self.actors), "routes": rows(self.routes)}


def choose_cohort(summary: Summary, seed: str, size: int) -> list[str]:
    eligible = [
        a for a, c in summary.actors.items() if a != CONTROL and c["closed_cycles"] > 0
    ]
    return sorted(
        eligible,
        key=lambda a: (-summary.actors[a]["closed_cycles"], priority(seed, a), a),
    )[:size]


class Tape:
    def __init__(self, path: Path, ceiling: int) -> None:
        self.file = path.open("xb")
        self.ceiling, self.size, self.seq = ceiling, 0, 0

    def emit(self, event: str, *, terminal: bool = False, **fields: object) -> None:
        row = {"schema": 1, "seq": self.seq, "event": event, **fields}
        line = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode()
        require(
            self.size + len(line) <= self.ceiling - (0 if terminal else TAIL_RESERVE),
            "output_byte_ceiling",
        )
        self.file.write(line)
        self.file.flush()
        self.size += len(line)
        self.seq += 1
        if event == "minute":
            brief = {k: v for k, v in row.items() if k != "summaries"}
            brief["phase_totals"] = {
                phase: {
                    "actors": len(summary["actors"]),
                    "routes": len(summary["routes"]),
                    "fee_lamports": str(
                        sum(
                            int(r.get("fee_lamports", "0"))
                            for r in summary["routes"].values()
                        )
                    ),
                    "failed_fee_lamports": str(
                        sum(
                            int(r.get("failed_fee_lamports", "0"))
                            for r in summary["routes"].values()
                        )
                    ),
                }
                for phase, summary in row["summaries"].items()
            }
            print(json.dumps(brief, separators=(",", ":")), flush=True)
        elif event != "receipt":
            print(line.decode().rstrip(), flush=True)


def load_credentials(path: Path) -> dict:
    value = json.loads(path.read_text())
    require(
        isinstance(value, dict)
        and {"geyser_endpoint", "geyser_token"} <= value.keys()
        and value.keys() <= {"geyser_endpoint", "geyser_token", "rpc_url"},
        "credentials_fields",
    )
    for name in ("geyser_endpoint", "geyser_token"):
        text = value[name]
        require(
            isinstance(text, str)
            and bool(text)
            and not any(unicodedata.category(c).startswith("C") for c in text),
            "credentials_shape",
        )
    parsed = urlsplit(value["geyser_endpoint"])
    require(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in ("", "/")
        and not any(c.isspace() for c in value["geyser_endpoint"]),
        "credentials_tls_endpoint",
    )
    require(parsed.port is None or 0 < parsed.port < 65536, "credentials_port")
    require(value["geyser_token"].isascii(), "credentials_token_ascii")
    return value


async def collect(args: argparse.Namespace, creds: dict, tape: Tape) -> bool:  # noqa: C901, PLR0912, PLR0915
    """One raw-wire gRPC stream; bounded exact sample, no provider retry policy."""
    start = time.monotonic_ns()
    source_id = hashlib.sha256(creds["geyser_endpoint"].encode()).hexdigest()
    counts: Counter = Counter()
    window: Counter = Counter()
    summaries = {"discovery": Summary(), "holdout": Summary()}
    reservoir = []
    selected_signatures: set[str] = set()
    retained_signatures: set[str] = set()
    last_transaction = start
    max_transaction_gap = 0
    cohort: list[str] | None = None
    program_keys = {base58.b58decode(address): address for address in PROGRAMS}
    cohort_keys: dict[bytes, str] = {}
    minute_index = 0
    last_receive = start
    max_gap = 0
    min_slot = max_slot = None
    limits = {
        k: v
        for k, v in vars(args).items()
        if k not in {"credentials", "out", "analyze", "self_check"}
    }
    tape.emit(
        "manifest",
        start_unix_ns=time.time_ns(),
        start_mono_ns=start,
        source_id=source_id,
        collector_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        transport="TLS/x-token Geyser Subscribe",
        subscription="all non-vote finalized; failed unset",
        limits=limits,
        programs=PROGRAMS,
        sources={
            p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            if (ROOT / p).exists()
            else None
            for p in SOURCES
        },
        selection="per-receive-minute bottom-K SHA256(seed NUL signature), outcome-independent program-reference eligibility",
        cohort_rule="sampled target-involved closed native cashflow count descending; seeded payer hash then address; no profit ranking",
        predeclared_controls=[CONTROL],
        control_discovery_coverage="sampled only; full address-match control holdout regardless discovery outcome",
        limitations=LIMITATIONS,
    )

    def totals() -> dict:
        return {phase: summary.export() for phase, summary in summaries.items()}

    def store(
        raw: bytes,
        received: int,
        wall: int,
        phase: str,
        score: int | None,
        actors: list[str],
    ) -> None:
        require(
            counts["retained_receipts"] < args.max_receipts, "retained_receipt_ceiling"
        )
        update = decode(raw)
        info = update.transaction.transaction
        analysis = account_receipt(update)
        signature = base58.b58encode(info.signature).decode("ascii")
        require(signature not in retained_signatures, "duplicate_retained_signature")
        tape.emit(
            "receipt",
            phase=phase,
            receive_unix_ns=wall,
            receive_mono_ns=received,
            elapsed_ns=received - start,
            slot=update.transaction.slot,
            index=info.index,
            signature=signature,
            priority=None if score is None else f"{score:064x}",
            source_id=source_id,
            raw_b64=base64.b64encode(raw).decode("ascii"),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_size=len(raw),
            actor_matches=actors,
            actor_roles={
                a: "predeclared_control" if a == CONTROL else "discovery_selected"
                for a in actors
            },
            analysis=analysis,
        )
        summaries[phase].add(
            analysis, [analysis["payer"]] if phase == "discovery" else actors
        )
        counts[f"{phase}_retained"] += 1
        window[f"{phase}_retained"] += 1
        counts["retained_receipts"] += 1
        retained_signatures.add(signature)

    def flush_discovery() -> None:
        # Remove each committed row immediately: a later disk failure cannot replay it.
        reservoir.sort(key=lambda row: row[3], reverse=True)
        while reservoir:
            negative, signature, raw, received, wall = reservoir[-1]
            store(raw, received, wall, "discovery", -negative, [])
            reservoir.pop()
            selected_signatures.remove(signature)

    def minute(now: int, *, partial: bool = False) -> None:
        nonlocal minute_index
        if cohort is None:
            flush_discovery()
        tape.emit(
            "minute",
            minute_index=minute_index,
            elapsed_ns=now - start,
            partial=partial,
            counters=dict(window),
            cumulative=dict(counts),
            min_slot=min_slot,
            max_slot=max_slot,
            largest_receive_gap_ns=max_gap,
            exact_provider_drop_count=None,
            local_unreported_drops=0,
            largest_transaction_receive_gap_ns=max_transaction_gap,
            transaction_silence_ns=now - last_transaction,
            summaries=totals(),
        )
        window.clear()
        minute_index += 1

    def freeze(now: int) -> None:
        nonlocal cohort
        flush_discovery()
        selected = choose_cohort(
            summaries["discovery"], args.sample_seed, args.cohort_size
        )
        cohort = [CONTROL, *selected]
        cohort_keys.update((base58.b58decode(address), address) for address in cohort)
        tape.emit(
            "cohort",
            freeze_elapsed_ns=now - start,
            scheduled_boundary_ns=args.discovery_seconds * 1_000_000_000,
            actors=cohort,
            eligible_actor_count=sum(
                c["closed_cycles"] > 0 for c in summaries["discovery"].actors.values()
            ),
            predeclared_controls=[CONTROL],
            discovery_selected=selected,
            evidence_counts={
                a: summaries["discovery"].actors.get(a, Counter())["closed_cycles"]
                for a in cohort
            },
            selection_frozen_before_holdout=True,
            subscription_changed=False,
            missing_coverage="owner-only activity absent from resolved account keys; upstream completeness unknown",
        )

    async def request(call: object, req: object) -> None:
        require(counts["requests"] < args.max_requests, "request_ceiling")
        counts["requests"] += 1
        await asyncio.wait_for(call.write(req), args.idle_seconds)

    async def receive(call: object) -> tuple:
        raw = await call.read()
        return raw, time.monotonic_ns(), time.time_ns()

    parsed = urlsplit(creds["geyser_endpoint"])
    host = parsed.hostname
    target = (
        f"[{host}]:{parsed.port or 443}"
        if ":" in host
        else f"{host}:{parsed.port or 443}"
    )
    reason, complete = "not_started", False
    pending = call = None
    try:
        async with grpc.aio.secure_channel(
            target,
            grpc.ssl_channel_credentials(),
            options=(
                ("grpc.enable_http_proxy", 0),
                ("grpc.max_receive_message_length", args.max_message_bytes),
                ("grpc.enable_retries", 0),
            ),
        ) as channel:
            # Raw deserializer is essential: reserialization is NOT original wire evidence.
            subscribe = channel.stream_stream(
                "/geyser.Geyser/Subscribe",
                request_serializer=geyser_pb2.SubscribeRequest.SerializeToString,
                response_deserializer=lambda raw: raw,
            )
            call = subscribe(metadata=(("x-token", creds["geyser_token"]),))
            await request(
                call,
                geyser_pb2.SubscribeRequest(
                    transactions={
                        "all_nonvote": geyser_pb2.SubscribeRequestFilterTransactions(
                            vote=False
                        )
                    },
                    commitment=geyser_pb2.FINALIZED,
                ),
            )
            await asyncio.wait_for(call.initial_metadata(), args.idle_seconds)
            setup_start = start
            start = time.monotonic_ns()
            last_receive = last_transaction = start
            tape.emit(
                "stream_ready",
                start_mono_ns=start,
                start_unix_ns=time.time_ns(),
                setup_elapsed_ns=start - setup_start,
            )
            pending = asyncio.create_task(receive(call))
            while True:
                now = pending.result()[1] if pending.done() else time.monotonic_ns()
                elapsed = now - start
                if cohort is None and elapsed >= args.discovery_seconds * 1_000_000_000:
                    freeze(now)
                while elapsed >= (minute_index + 1) * 60_000_000_000:
                    minute(start + (minute_index + 1) * 60_000_000_000)
                if elapsed >= args.seconds * 1_000_000_000:
                    reason, complete = "duration_complete", True
                    break
                if now - last_receive >= args.idle_seconds * 1_000_000_000:
                    raise StudyError("stream_idle_gap")  # noqa: TRY301
                if now - last_transaction >= args.idle_seconds * 1_000_000_000:
                    raise StudyError("transaction_idle_gap")  # noqa: TRY301
                boundaries = [
                    start + args.seconds * 1_000_000_000,
                    start + (minute_index + 1) * 60_000_000_000,
                    last_receive + args.idle_seconds * 1_000_000_000,
                    last_transaction + args.idle_seconds * 1_000_000_000,
                ]
                if cohort is None:
                    boundaries.append(start + args.discovery_seconds * 1_000_000_000)
                done, _ = await asyncio.wait(
                    {pending}, timeout=max(0, (min(boundaries) - now) / 1_000_000_000)
                )
                if not done:
                    continue
                raw, received, wall = pending.result()
                pending = None
                if raw is grpc.aio.EOF:
                    raise StudyError("stream_eof")  # noqa: TRY301
                if received - start >= args.seconds * 1_000_000_000:
                    counts["out_of_window_updates"] += 1
                    counts["out_of_window_wire_bytes"] += len(raw)
                    reason, complete = "duration_complete", True
                    break
                max_gap = max(max_gap, received - last_receive)
                last_receive = received
                # Finalize the previous sampling window before attributing this receipt.
                # Use the same receive-time boundary before inspecting any holdout outcome.
                if (
                    cohort is None
                    and received - start >= args.discovery_seconds * 1_000_000_000
                ):
                    freeze(received)
                while received - start >= (minute_index + 1) * 60_000_000_000:
                    minute(start + (minute_index + 1) * 60_000_000_000)
                counts["updates"] += 1
                counts["wire_bytes"] += len(raw)
                window["updates"] += 1
                window["wire_bytes"] += len(raw)
                require(counts["updates"] <= args.max_updates, "update_ceiling")
                require(
                    counts["wire_bytes"] <= args.max_wire_bytes, "wire_byte_ceiling"
                )
                require(len(raw) <= args.max_message_bytes, "message_byte_ceiling")
                update = decode(raw)
                kind = update.WhichOneof("update_oneof")
                counts[f"update_{kind}"] += 1
                window[f"update_{kind}"] += 1
                if kind == "ping":
                    await request(
                        call,
                        geyser_pb2.SubscribeRequest(
                            ping=geyser_pb2.SubscribeRequestPing(id=counts["requests"])
                        ),
                    )
                elif kind == "transaction":
                    max_transaction_gap = max(
                        max_transaction_gap, received - last_transaction
                    )
                    last_transaction = received
                    info = update.transaction.transaction
                    require(not info.is_vote, "provider_vote_filter_violation")
                    require(len(info.signature) == 64, "invalid_signature")
                    keys = resolved_keys(info)
                    slot = update.transaction.slot
                    min_slot = slot if min_slot is None else min(min_slot, slot)
                    if max_slot is not None and slot < max_slot:
                        counts["slot_regressions"] += 1
                    if max_slot is not None and slot > max_slot + 1:
                        counts["observed_slot_jump_events"] += 1
                    max_slot = slot if max_slot is None else max(max_slot, slot)
                    counts["nonvote_receipts"] += 1
                    window["nonvote_receipts"] += 1
                    phase = "discovery" if cohort is None else "holdout"
                    counts[f"{phase}_receipts_seen"] += 1
                    window[f"{phase}_receipts_seen"] += 1
                    outcome = "failed" if info.meta.HasField("err") else "successful"
                    counts[f"{phase}_{outcome}_seen"] += 1
                    window[f"{phase}_{outcome}_seen"] += 1
                    if cohort is None:
                        matching = {
                            program_keys[value]
                            for value in keys
                            if value in program_keys
                        }
                        if matching:
                            counts["discovery_eligible"] += 1
                            window["discovery_eligible"] += 1
                            for p in matching:
                                counts[f"referenced_{PROGRAMS[p]}"] += 1
                                window[f"referenced_{PROGRAMS[p]}"] += 1
                            signature = base58.b58encode(info.signature).decode("ascii")
                            score = priority(args.sample_seed, signature)
                            row = (-score, signature, raw, received, wall)
                            if signature in selected_signatures:
                                raise StudyError("duplicate_selected_signature")  # noqa: TRY301
                            if len(reservoir) < args.sample_per_minute:
                                heapq.heappush(reservoir, row)
                                selected_signatures.add(signature)
                            elif score < -reservoir[0][0]:
                                old = heapq.heapreplace(reservoir, row)
                                selected_signatures.remove(old[1])
                                selected_signatures.add(signature)
                                counts["sample_evictions"] += 1
                                window["sample_evictions"] += 1
                            else:
                                counts["sample_rejections"] += 1
                                window["sample_rejections"] += 1
                    else:
                        actors = sorted(
                            {
                                cohort_keys[value]
                                for value in keys
                                if value in cohort_keys
                            }
                        )
                        if actors:
                            counts["holdout_actor_matches"] += 1
                            window["holdout_actor_matches"] += 1
                            store(raw, received, wall, "holdout", None, actors)
                pending = asyncio.create_task(receive(call))
    except StudyError as exc:
        reason = str(exc)
    except grpc.aio.AioRpcError as exc:
        reason = "grpc_" + exc.code().name
    except TimeoutError:
        reason = "transport_timeout"
    except asyncio.CancelledError:
        reason = "cancelled"
    except Exception as exc:  # noqa: BLE001
        reason = "fatal_" + type(exc).__name__
    finally:
        pending_record = bool(
            pending is not None
            and pending.done()
            and not pending.cancelled()
            and pending.exception() is None
            and pending.result()[0] is not grpc.aio.EOF
        )
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if call is not None:
            call.cancel()
        try:
            if reason not in ("output_byte_ceiling", "retained_receipt_ceiling"):
                if reservoir:
                    flush_discovery()
                minute(time.monotonic_ns(), partial=True)
        except Exception as exc:  # noqa: BLE001
            complete = False
            if reason == "duration_complete":
                reason = "final_flush_" + type(exc).__name__
        # Full summaries are on minute rows; keep terminal comfortably inside reserve.
        tape.emit(
            "terminal",
            terminal=True,
            reason=reason,
            complete=complete,
            elapsed_ns=time.monotonic_ns() - start,
            counters=dict(counts),
            cohort=cohort,
            censored=not complete,
            transaction_silence_ns=time.monotonic_ns() - last_transaction,
            output_bytes_before_terminal=tape.size,
            unflushed_selected=len(reservoir),
            exact_provider_drop_count=None,
            pending_stream_item_at_stop=pending_record,
            transport_buffer_backlog="unknown_requires_external_finalized_slot_comparison",
            coverage_complete=False,
            limitations=LIMITATIONS,
        )
    return complete


def analyze(path: Path) -> dict:  # noqa: C901, PLR0912, PLR0915
    """Streaming replay verifies retained evidence, not unsaved sample candidates."""
    summaries = {"discovery": Summary(), "holdout": Summary()}
    manifest = ready = cohort = terminal = None
    count = 0
    seen: set[str] = set()
    warnings = []
    receipt_count = Counter()
    program_keys = {base58.b58decode(address) for address in PROGRAMS}
    cohort_keys: dict[bytes, str] = {}
    with path.open("rb") as source:
        for line in source:
            if not line.endswith(b"\n"):
                warnings.append("truncated_final_line")
                break
            row = json.loads(line)
            require(
                row.get("schema") == 1 and row.get("seq") == count,
                "tape_schema_or_sequence",
            )
            count += 1
            event = row["event"]
            require(terminal is None, "rows_after_terminal")
            if event == "manifest":
                require(manifest is None and count == 1, "manifest_order")
                manifest = row
                require(
                    row["collector_source_sha256"]
                    == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "frozen_collector_source_required",
                )
                require(
                    row["programs"] == PROGRAMS
                    and row["predeclared_controls"] == [CONTROL],
                    "manifest_universe_mismatch",
                )
            elif event == "stream_ready":
                require(
                    manifest is not None and ready is None and not seen,
                    "stream_ready_order",
                )
                require(
                    row["start_mono_ns"] >= manifest["start_mono_ns"],
                    "stream_ready_clock",
                )
                ready = row
            elif event == "cohort":
                require(ready is not None and cohort is None, "cohort_order")
                expected = choose_cohort(
                    summaries["discovery"],
                    manifest["limits"]["sample_seed"],
                    manifest["limits"]["cohort_size"],
                )
                require(
                    row["actors"] == [CONTROL, *expected]
                    and row["discovery_selected"] == expected
                    and row["predeclared_controls"] == [CONTROL],
                    "cohort_selection_mismatch",
                )
                require(
                    row["freeze_elapsed_ns"] >= row["scheduled_boundary_ns"],
                    "early_cohort_freeze",
                )
                cohort = row
                cohort_keys = {
                    base58.b58decode(address): address for address in cohort["actors"]
                }
            elif event == "receipt":
                require(
                    manifest is not None and ready is not None,
                    "receipt_before_stream_ready",
                )
                limits = manifest["limits"]
                raw = base64.b64decode(row["raw_b64"], validate=True)
                require(
                    len(raw) == row["raw_size"] <= limits["max_message_bytes"],
                    "raw_size_mismatch",
                )
                require(
                    hashlib.sha256(raw).hexdigest() == row["raw_sha256"],
                    "raw_hash_mismatch",
                )
                require(
                    row["source_id"] == manifest["source_id"],
                    "source_identity_mismatch",
                )
                require(
                    row["receive_mono_ns"] - ready["start_mono_ns"]
                    == row["elapsed_ns"],
                    "receipt_timing_mismatch",
                )
                require(
                    0 <= row["elapsed_ns"] < limits["seconds"] * 1_000_000_000,
                    "receipt_outside_study_window",
                )
                update = decode(raw)
                info = update.transaction.transaction
                require(not info.is_vote, "tape_vote")
                signature = base58.b58encode(info.signature).decode("ascii")
                require(
                    signature == row["signature"]
                    and update.transaction.slot == row["slot"]
                    and info.index == row["index"],
                    "receipt_identity_mismatch",
                )
                receipt_keys = set(resolved_keys(info))
                analysis = account_receipt(update)
                require(analysis == row["analysis"], "accounting_replay_mismatch")
                phase = row["phase"]
                if phase == "discovery":
                    require(
                        cohort is None
                        and row["elapsed_ns"]
                        < limits["discovery_seconds"] * 1_000_000_000,
                        "discovery_after_freeze",
                    )
                    require(
                        receipt_keys.intersection(program_keys),
                        "ineligible_discovery_receipt",
                    )
                    score = priority(limits["sample_seed"], signature)
                    require(
                        row["priority"] == f"{score:064x}", "sample_priority_mismatch"
                    )
                    bucket = row["elapsed_ns"] // 60_000_000_000
                    receipt_count[bucket] += 1
                    require(
                        receipt_count[bucket] <= limits["sample_per_minute"],
                        "sample_minute_ceiling",
                    )
                    actors = [analysis["payer"]]
                else:
                    require(
                        phase == "holdout" and cohort is not None,
                        "holdout_before_freeze",
                    )
                    require(
                        row["elapsed_ns"]
                        >= limits["discovery_seconds"] * 1_000_000_000,
                        "early_holdout",
                    )
                    actors = sorted(
                        cohort_keys[value]
                        for value in receipt_keys
                        if value in cohort_keys
                    )
                    require(
                        bool(actors) and actors == row["actor_matches"],
                        "holdout_actor_mismatch",
                    )
                require(len(seen) < limits["max_receipts"], "retained_receipt_ceiling")
                require(signature not in seen, "duplicate_retained_receipt")
                seen.add(signature)
                summaries[phase].add(analysis, actors)
            elif event == "terminal":
                terminal = row
            elif event == "minute":
                require(
                    row["summaries"] == {p: s.export() for p, s in summaries.items()},
                    "minute_summary_mismatch",
                )
            else:
                raise StudyError("unknown_tape_event")
    require(manifest is not None, "missing_manifest")
    if terminal is None:
        warnings.append("missing_terminal_incomplete_capture")
    if ready is None:
        warnings.append("observation_never_started")
    if cohort is None:
        warnings.append("cohort_never_frozen_no_holdout_claim")
    return {
        "event": "offline_analysis",
        "rows_verified": count,
        "terminal": terminal,
        "cohort": None if cohort is None else cohort["actors"],
        "warnings": warnings,
        "sample_selection_verifiable": "retained priorities/ceilings only; rejected raw transactions were not saved",
        "summaries": {phase: summary.export() for phase, summary in summaries.items()},
        "limitations": LIMITATIONS,
    }


def self_check() -> None:  # noqa: PLR0915
    """One compact offline protobuf scenario covers four accounting traps."""
    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    info.signature = bytes(range(64))
    msg, meta = info.transaction.message, info.meta
    payer, wsol_account, token_account = (bytes([n]) * 32 for n in (1, 2, 3))
    msg.header.num_required_signatures = 1
    msg.versioned = True
    msg.account_keys.extend([payer, wsol_account, token_account])
    venue = next(iter(PROGRAMS))
    lookup = msg.address_table_lookups.add()
    lookup.account_key = bytes([8]) * 32
    lookup.readonly_indexes = b"\x00"
    meta.loaded_readonly_addresses.append(base58.b58decode(venue))
    msg.instructions.add(program_id_index=3)
    meta.pre_balances.extend([1_000_000, 3_000_000, 2_000_000, 1])
    meta.post_balances.extend([995_000, 3_020_000, 2_000_000, 1])
    meta.fee = 5_000
    owner = key(payer)
    for rows, amount in (
        (meta.pre_token_balances, 1_000_000),
        (meta.post_token_balances, 1_020_000),
    ):
        row = rows.add(account_index=1, mint=SOL, owner=owner, program_id=SPL)
        row.ui_token_amount.amount = str(amount)
        row.ui_token_amount.decimals = 9
        row = rows.add(
            account_index=2, mint=key(bytes([9]) * 32), owner=owner, program_id=SPL
        )
        row.ui_token_amount.amount = "10"
    raw = update.SerializeToString()
    result = account_receipt(decode(raw))
    require(
        result["known_owned_native_delta_lamports"] == "15000"
        and result["wallet_sol_delta_lamports"] == "-5000",
        "selfcheck_wsol_fee_doublecount",
    )
    require(
        result["loaded_keys"] == 1 and venue in result["executed_programs"],
        "selfcheck_alt",
    )
    # Same holdings with actual two-venue CPI token roundtrips: positive AND losing
    # closed cycles must both qualify, never only winning discovery evidence.
    cycle_update = decode(raw)
    cm = cycle_update.transaction.transaction.transaction.message
    ct = cycle_update.transaction.transaction.meta
    cm.address_table_lookups[0].writable_indexes = b"\x01\x02"
    cm.address_table_lookups[0].readonly_indexes = b"\x00\x03\x04"
    ct.loaded_writable_addresses.extend([bytes([4]) * 32, bytes([5]) * 32])
    ct.loaded_readonly_addresses.extend(
        [base58.b58decode(SPL), base58.b58decode(list(PROGRAMS)[1])]
    )
    del cm.instructions[:]
    cm.instructions.add(program_id_index=5)
    cm.instructions.add(program_id_index=7)
    for balances, native_vault in (
        (ct.pre_balances, 3_000_000),
        (ct.post_balances, 2_980_000),
    ):
        del balances[3:]
        balances.extend([native_vault, 2_000_000, 1, 1, 1])
    for balances, native_amount in (
        (ct.pre_token_balances, "1000000"),
        (ct.post_token_balances, "980000"),
    ):
        row = balances.add(
            account_index=3, owner=key(bytes([6]) * 32), mint=SOL, program_id=SPL
        )
        row.ui_token_amount.amount = native_amount
        row = balances.add(
            account_index=4,
            owner=key(bytes([6]) * 32),
            mint=key(bytes([9]) * 32),
            program_id=SPL,
        )
        row.ui_token_amount.amount = "10"
    for index, legs in enumerate(
        (((1, 3, 100_000), (4, 2, 10)), ((2, 4, 10), (3, 1, 120_000)))
    ):
        group = ct.inner_instructions.add(index=index)
        for source, destination, amount in legs:
            group.instructions.add(
                program_id_index=6,
                accounts=bytes([source, destination, 0]),
                data=b"\x03" + amount.to_bytes(8, "little"),
                stack_height=2,
            )
    same_venue = decode(cycle_update.SerializeToString())
    same_venue.transaction.transaction.transaction.message.instructions[
        1
    ].program_id_index = 5
    require(
        account_receipt(same_venue)["plausible_closed_native_cycle"],
        "selfcheck_same_program_roundtrip",
    )
    require(
        account_receipt(cycle_update)["plausible_closed_native_cycle"],
        "selfcheck_positive_closed_cycle",
    )
    tipped = decode(cycle_update.SerializeToString())
    tm = tipped.transaction.transaction.transaction.message
    tt = tipped.transaction.transaction.meta
    tm.address_table_lookups[0].readonly_indexes += b"\x05"
    tt.loaded_readonly_addresses.append(bytes(32))
    tt.pre_balances.append(1)
    tt.post_balances.append(1)
    tm.instructions.add(
        program_id_index=8,
        accounts=bytes([0, 3]),
        data=(2).to_bytes(4, "little") + (7_000).to_bytes(8, "little"),
    )
    tt.post_balances[0] -= 7_000
    tt.post_balances[3] += 7_000
    result = account_receipt(tipped)
    require(
        result["plausible_closed_native_cycle"]
        and result["known_owned_native_delta_lamports"] == "8000",
        "selfcheck_explicit_native_tip",
    )
    ct.inner_instructions[1].instructions[1].data = b"\x03" + (90_000).to_bytes(
        8, "little"
    )
    ct.post_token_balances[0].ui_token_amount.amount = "990000"
    ct.post_token_balances[2].ui_token_amount.amount = "1010000"
    ct.post_balances[1] = 2_990_000
    ct.post_balances[3] = 3_010_000
    losing = account_receipt(cycle_update)
    require(
        losing["plausible_closed_native_cycle"]
        and losing["known_owned_native_delta_lamports"] == "-15000",
        "selfcheck_losing_closed_cycle",
    )
    ct.post_token_balances[1].ui_token_amount.amount = "9"
    require(
        not account_receipt(cycle_update)["plausible_closed_native_cycle"],
        "selfcheck_cycle_inventory_exclusion",
    )
    meta.post_token_balances[1].ui_token_amount.amount = "9"
    result = account_receipt(update)
    require(
        result["inventory_flat"] is False
        and not result["plausible_closed_native_cycle"],
        "selfcheck_liquidation_exclusion",
    )
    meta.post_token_balances[1].ui_token_amount.amount = "10"
    meta.post_token_balances[0].ui_token_amount.amount = "1000000"
    meta.post_balances[1] = 3_000_000
    meta.err.err = b"\x01"
    result = account_receipt(update)
    require(
        result["known_owned_native_delta_lamports"] == "-5000"
        and result["failed_fee_lamports"] == "5000"
        and not result["successful"],
        "selfcheck_failed_fee",
    )
    anomalous = decode(update.SerializeToString())
    anomalous.transaction.transaction.meta.post_balances[0] += 1
    result = account_receipt(anomalous)
    require(
        result["known_owned_native_delta_lamports"] is None
        and not result["native_accounting_known"],
        "selfcheck_failed_nonfee_anomaly",
    )
    rolled_back = decode(update.SerializeToString())
    rt = rolled_back.transaction.transaction.meta
    rt.loaded_readonly_addresses[0] = base58.b58decode(TOKEN2022)
    del rt.pre_token_balances[:]
    del rt.post_token_balances[:]
    result = account_receipt(rolled_back)
    require(
        result["known_owned_native_delta_lamports"] == "-5000"
        and result["native_accounting_known"]
        and not result["inventory_known"],
        "selfcheck_rolled_back_unknown_tokens",
    )
    meta.ClearField("err")
    meta.pre_token_balances[0].owner = ""
    require(
        account_receipt(update)["known_owned_native_delta_lamports"] is None,
        "selfcheck_unknown_owner",
    )
    print(
        json.dumps(
            {
                "event": "self_check",
                "passed": True,
                "cases": [
                    "WSOL_gain_fee_only_SOL_loss",
                    "loaded_keys",
                    "positive_and_losing_closed_cycles",
                    "explicit_native_tip",
                    "inventory_liquidation_exclusion",
                    "failed_fee",
                    "failed_nonfee_anomaly",
                    "rolled_back_unknown_tokens",
                    "unknown_owner",
                ],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--analyze", type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--discovery-seconds", type=int, default=600)
    parser.add_argument("--sample-per-minute", type=int, default=120)
    parser.add_argument("--cohort-size", type=int, default=3)
    parser.add_argument("--sample-seed", default="solana-receipts-v1")
    parser.add_argument("--max-updates", type=int, default=20_000_000)
    parser.add_argument("--max-wire-bytes", type=int, default=17_179_869_184)
    parser.add_argument("--max-output-bytes", type=int, default=268_435_456)
    parser.add_argument("--max-receipts", type=int, default=50_000)
    parser.add_argument("--max-message-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--idle-seconds", type=int, default=45)
    args = parser.parse_args()
    tape = None
    try:
        if args.self_check:
            self_check()
            return
        if args.analyze:
            print(json.dumps(analyze(args.analyze), sort_keys=True))
            return
        require(
            args.credentials is not None and args.out is not None,
            "explicit_credentials_and_out_required",
        )
        require(0 < args.discovery_seconds < args.seconds <= 86400, "duration_bounds")
        require(
            0 < args.sample_per_minute <= 1000
            and 0 <= args.cohort_size <= 3
            and 0 < args.max_receipts <= 50_000,
            "sample_bounds",
        )
        require(
            0 < args.max_message_bytes <= 16_000_000
            and args.sample_per_minute * args.max_message_bytes <= 512_000_000,
            "memory_ceiling",
        )
        require(
            TAIL_RESERVE * 2 < args.max_output_bytes <= 1_073_741_824
            and args.max_wire_bytes > 0
            and args.max_updates > 0
            and args.max_requests > 0
            and 1 <= args.idle_seconds <= 300,
            "resource_bounds",
        )
        require(
            0 < len(args.sample_seed) <= 128 and args.sample_seed.isascii(),
            "seed_bounds",
        )
        creds = load_credentials(args.credentials)
        tape = Tape(args.out, args.max_output_bytes)
        success = asyncio.run(collect(args, creds, tape))
        if not success:
            raise SystemExit(1)  # noqa: TRY301
    except KeyboardInterrupt:
        print('{"event":"fatal","reason":"interrupted"}', file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001
        code = str(exc) if isinstance(exc, StudyError) else type(exc).__name__
        print(json.dumps({"event": "fatal", "reason": code}), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if tape is not None:
            tape.file.close()


if __name__ == "__main__":
    main()
