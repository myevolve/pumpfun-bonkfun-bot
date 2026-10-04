"""Record prospective pump.fun lifecycles using only the read-only Geyser stream.

All outputs are new, exclusive JSONL files: lifecycles, slots, raw transactions,
and a run journal. Processed observations are not finalized receipts or fills.
No wallet or HTTP RPC is used. Importing never reads provider credentials.
Minutes bound observation after the first processed head, including admission and
drain. Startup has its own stream-idle bound; reconnects reset neither deadline.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import signal
import sys
import time
import uuid
import zlib
from collections import Counter
from itertools import chain
from pathlib import Path

import base58
import grpc
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from interfaces.core import Platform
from monitoring.event_normalization import normalize_geyser_update
from monitoring.parser_dispatch import parse_normalized_event
from monitoring.trade_flow import MAYHEM_SOL_VAULT
from platforms.pumpfun.event_parser import PumpFunEventParser
from src.geyser.generated import geyser_pb2, geyser_pb2_grpc
from utils.idl_parser import IDLParser
from utils.program_logs import attribute_program_logs

# Standalone src imports, protocol bounds and offline assertions are intentional.
# ruff: noqa: E402, PLR2004, S101

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PROGRAM_DATA = "Program data: "
LAMPORTS = 1_000_000_000
REPORT_EVERY_SECONDS = 30
MAX_SLOT_GAP_SECONDS = 1.0
TERMINAL_RESERVE = 65536


class StorageCeiling(RuntimeError):  # noqa: N818 - existing evidence-ceiling sentinel
    """Stop rather than silently discard evidence or evict deduplication keys."""


class Recorder:
    """Append evidence once; bound both active tapes and all-run replay identity."""

    def __init__(  # noqa: C901, PLR0913
        self,
        out_path: Path,
        idle_seconds: float,
        max_age: float,
        *,
        postgrad_seconds: float = 600,
        postgrad_idle_seconds: float = 120,
        max_bytes: int = 2 * 1024**3,
        max_memory_bytes: int = 256 * 1024**2,
        max_coins: int = 10000,
        max_events: int = 2_000_000,
        run_id: str | None = None,
    ) -> None:
        for value in (
            idle_seconds,
            max_age,
            postgrad_seconds,
            postgrad_idle_seconds,
            max_bytes,
            max_memory_bytes,
            max_coins,
            max_events,
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("capture bounds must be finite and positive")  # noqa: TRY003
        if max_bytes <= TERMINAL_RESERVE:
            raise ValueError("max_bytes must exceed terminal journal reserve")  # noqa: TRY003
        if run_id is not None and (
            not isinstance(run_id, str)
            or not 1 <= len(run_id) <= 128
            or any(
                not (
                    character.isascii() and (character.isalnum() or character in "._-")
                )
                for character in run_id
            )
        ):
            raise ValueError(
                "run_id must be 1-128 public ASCII letters, digits, dots, underscores or hyphens"
            )
        self.idle_seconds, self.max_age = idle_seconds, max_age
        self.postgrad_seconds = postgrad_seconds
        self.postgrad_idle_seconds = postgrad_idle_seconds
        self.max_bytes, self.max_memory_bytes = max_bytes, max_memory_bytes
        self.max_coins, self.max_events = max_coins, max_events
        self.coins: dict[str, dict] = {}
        self.pools: dict[str, str] = {}
        self.seen_events: set[tuple[str, int]] = set()
        self.created_mints: set[str] = set()
        self.failed_seen: set[str] = set()
        self.memory_bytes = 0
        self.counts = Counter(
            dict.fromkeys(
                (
                    "updates",
                    "transactions",
                    "failed_transactions",
                    "attributable_failed_transactions",
                    "duplicate_events",
                    "duplicate_creations",
                    "duplicate_failed_transactions",
                    "decode_refusals",
                    "partial_event_decodes",
                    "attribution_refusals",
                    "normalization_refusals",
                    "state_decode_refusals",
                    "out_of_universe_transactions",
                    "out_of_universe_events",
                    "outside_admission_creations",
                    "reverted_cpi_events",
                    "creations",
                    "verified_creations",
                    "unverified_creations",
                    "raw_transactions",
                    "slot_updates",
                    "slot_gaps",
                    "stream_failures",
                    "ingress_bytes",
                    "bytes_written",
                    "lifecycles_bytes",
                    "slots_bytes",
                    "raw_bytes",
                    "run_bytes",
                ),
                0,
            )
        )
        self.flush_reasons = Counter()
        self.written = self.trades_seen = self.graduations = self.post_trades_seen = 0
        self.decode_errors = 0
        self.started = time.monotonic()
        self.run_id = str(uuid.uuid4()) if run_id is None else run_id
        self.admission_cutoff = None
        self.first_head_slot = None
        self.first_head_received_monotonic = None
        self.parser = IDLParser(str(ROOT / "idl" / "pump_fun_idl.json"))
        self.amm_parser = IDLParser(str(ROOT / "idl" / "pump_swap_idl.json"))
        self.platform_parsers = {Platform.PUMP_FUN: PumpFunEventParser(self.parser)}
        instruction_discriminators = self.parser.get_instruction_discriminators()
        self.creation_instructions = {
            instruction_discriminators[name]
            for name in ("create", "create_v2")
            if name in instruction_discriminators
        }
        self.decoders = {}
        for program, parser, names in (
            (PUMP_PROGRAM, self.parser, ("CreateEvent", "TradeEvent", "CompleteEvent")),
            (
                PUMP_AMM_PROGRAM,
                self.amm_parser,
                ("CreatePoolEvent", "BuyEvent", "SellEvent"),
            ),
        ):
            discriminators = parser.get_event_discriminators()
            for name in names:
                required = frozenset(
                    field["name"] for field in parser.types[name]["type"]["fields"]
                )
                self.decoders[program, discriminators[name]] = (parser, name, required)
        self.paths = {
            "lifecycles": out_path,
            "slots": out_path.with_suffix(".slots.jsonl"),
            "raw": out_path.with_suffix(".raw.jsonl"),
            "run": out_path.with_suffix(".run.jsonl"),
        }
        if len(set(self.paths.values())) != 4:
            raise ValueError("output paths collide")  # noqa: TRY003
        self.files = {}
        try:
            for kind, path in self.paths.items():
                self.files[kind] = path.open("x", encoding="utf-8")
        except BaseException:
            for handle in self.files.values():
                handle.close()
            raise
        self.out, self.slot_out = self.files["lifecycles"], self.files["slots"]

    def emit(self, kind: str, row: dict, *, terminal: bool = False) -> None:
        line = json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
        size = len(line.encode("utf-8"))
        limit = self.max_bytes if terminal else self.max_bytes - TERMINAL_RESERVE
        if self.counts["bytes_written"] + size > limit:
            raise StorageCeiling("disk_bytes")
        self.files[kind].write(line)
        self.files[kind].flush()
        self.counts["bytes_written"] += size
        self.counts[f"{kind}_bytes"] += size

    def journal(self, kind: str, *, terminal: bool = False, **fields: object) -> None:
        self.emit(
            "run",
            {
                "schema_version": 2,
                "run_id": self.run_id,
                "kind": kind,
                "received_monotonic": time.monotonic(),
                "wall_time_ns": time.time_ns(),
                **fields,
            },
            terminal=terminal,
        )

    def reserve(self, size: int, coin: dict | None = None) -> None:
        if self.memory_bytes + size > self.max_memory_bytes:
            raise StorageCeiling("retained_memory_budget")
        self.memory_bytes += size
        if coin is not None:
            coin["_memory_bytes"] += size

    def remember(self, signature: str, index: int) -> bool:
        key = signature, index
        if key in self.seen_events:
            self.counts["duplicate_events"] += 1
            return False
        if len(self.seen_events) >= self.max_events:
            raise StorageCeiling("dedup_event_keys")
        self.reserve(384)
        self.seen_events.add(key)
        return True

    def on_transaction(
        self, update: geyser_pb2.SubscribeUpdate, now: float, wall_time_ns: int
    ) -> None:
        info = update.transaction.transaction
        slot = update.transaction.slot
        signature = base58.b58encode(bytes(info.signature)).decode()
        failed = info.meta.HasField("err")
        self.counts["transactions"] += 1
        self.counts["failed_transactions"] += int(failed)
        account_keys = [
            base58.b58encode(bytes(key)).decode()
            for key in (
                *info.transaction.message.account_keys,
                *info.meta.loaded_writable_addresses,
                *info.meta.loaded_readonly_addresses,
            )
        ]
        accounts = set(account_keys)
        related = accounts.intersection(self.coins)
        related.update(self.pools[pool] for pool in accounts.intersection(self.pools))
        logs = list(info.meta.log_messages)
        # A malformed creation still merits raw evidence, never trusted admission.
        creation_hint = PUMP_PROGRAM in accounts and any(
            log.startswith("Program log: Instruction: Create") for log in logs
        )
        if not creation_hint and PUMP_PROGRAM in accounts:
            creation_hint = any(
                instruction.program_id_index < len(account_keys)
                and account_keys[instruction.program_id_index] == PUMP_PROGRAM
                and bytes(instruction.data)[:8] in self.creation_instructions
                for instruction in chain(
                    info.transaction.message.instructions,
                    (
                        instruction
                        for group in info.meta.inner_instructions
                        for instruction in group.instructions
                    ),
                )
            )
        decoded = []
        refusal = None
        decode_refused = False
        try:
            entries = attribute_program_logs(logs)
            for index, program, log, committed in entries:
                if program not in (PUMP_PROGRAM, PUMP_AMM_PROGRAM):
                    continue
                creation_hint |= program == PUMP_PROGRAM and log.startswith(
                    "Program log: Instruction: Create"
                )
                if not log.startswith(PROGRAM_DATA):
                    continue
                encoded = log[len(PROGRAM_DATA) :]
                try:
                    data = base64.b64decode(encoded, validate=True)
                    decoder = self.decoders.get((program, data[:8]))
                    if decoder is None:
                        continue
                    parser, name, required = decoder
                    creation_hint |= name == "CreateEvent"
                    event = parser.decode_event_data(data, name)
                    if not event or not isinstance(event.get("fields"), dict):
                        raise ValueError("event_decode_refused")  # noqa: TRY301
                    fields = event["fields"]
                    if not required.issubset(fields):
                        self.counts["partial_event_decodes"] += 1
                        decode_refused = True
                        if name != "CreateEvent":
                            self.counts["decode_refusals"] += 1
                            continue
                    decoded.append((index, name, fields, committed))
                    mint = (
                        fields.get("mint")
                        or fields.get("base_mint")
                        or self.pools.get(fields.get("pool"))
                    )
                    if mint in self.coins:
                        related.add(mint)
                except (ValueError, TypeError, KeyError, OverflowError):
                    decode_refused = True
                    self.counts["decode_refusals"] += 1
                    self.decode_errors += 1
        except ValueError as exc:
            refusal = str(exc)
            self.counts["attribution_refusals"] += 1
        if not creation_hint and not related:
            self.counts["out_of_universe_transactions"] += 1
            return
        canonical = {}
        provenance_reason = "no_canonical_creation"
        normalized = None
        try:
            normalized = normalize_geyser_update(update, commitment="processed")
            if normalized is None or normalized.signature != signature:
                raise ValueError("missing_or_mismatched_transaction_identity")  # noqa: TRY301
            if creation_hint and not failed and refusal is None:
                canonical = {
                    str(token.mint): token
                    for token in parse_normalized_event(
                        normalized, self.platform_parsers
                    )
                }
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            provenance_reason = f"normalization_refused:{type(exc).__name__}"
            self.counts["normalization_refusals"] += 1
            refusal = refusal or provenance_reason
        protobuf = update.SerializeToString()
        self.emit(
            "raw",
            {
                "schema_version": 3,
                "run_id": self.run_id,
                "slot": slot,
                "signature": signature,
                "received_monotonic": now,
                "wall_time_ns": wall_time_ns,
                "failed": failed,
                "creation_candidate": creation_hint,
                "tracked_mints": sorted(related),
                "attribution_refusal": refusal,
                "event_decode_refused": decode_refused,
                "encoding": "base64:zlib:geyser.SubscribeUpdate",
                "raw_size": len(protobuf),
                "raw_sha256": hashlib.sha256(protobuf).hexdigest(),
                "protobuf_zlib_base64": base64.b64encode(
                    zlib.compress(protobuf, level=1)
                ).decode("ascii"),
            },
        )
        self.counts["raw_transactions"] += 1
        if failed:
            self.counts["attributable_failed_transactions"] += 1
            if signature not in self.failed_seen:
                if len(self.failed_seen) >= self.max_events:
                    raise StorageCeiling("failed_signature_keys")
                self.reserve(256)
                self.failed_seen.add(signature)
                for mint in related:
                    self.coins[mint]["n_failed_excluded"] += 1
            else:
                self.counts["duplicate_failed_transactions"] += 1
            return
        if refusal is not None or normalized is None:
            self.mark_evidence_failure(related, now, refusal or "normalization_missing")
            return
        # Admit creations before applying their bundled transaction states; retain
        # original log indices and relative trade order in either phase.
        for index, name, fields, committed in [
            item for item in decoded if item[1] == "CreateEvent"
        ] + [item for item in decoded if item[1] != "CreateEvent"]:
            if not committed:
                self.counts["reverted_cpi_events"] += 1
                continue
            mint = (
                fields.get("mint")
                or fields.get("base_mint")
                or self.pools.get(fields.get("pool"))
            )
            if name != "CreateEvent" and mint not in self.coins:
                self.counts["out_of_universe_events"] += 1
                continue
            if not self.remember(signature, index):
                continue
            try:
                if name == "CreateEvent":
                    token = canonical.get(fields.get("mint"))
                    verified = bool(
                        token is not None
                        and token.metadata_verified
                        and token.state_from_event
                        and token.signature == signature
                        and token.slot == slot
                        and str(token.creator) == fields.get("creator")
                    )
                    provenance = {
                        "verified": verified,
                        "reason": "canonical_instruction_and_event"
                        if verified
                        else (
                            "canonical_creation_state_unverified"
                            if token is not None
                            else provenance_reason
                        ),
                    }
                    self._on_create(fields, slot, signature, now, provenance)
                    if fields.get("mint") in self.coins:
                        related.add(fields["mint"])
                elif name == "TradeEvent":
                    self._on_trade(fields, slot, signature, now, index)
                elif name == "CompleteEvent":
                    self._on_complete(fields, slot, now)
                elif name == "CreatePoolEvent":
                    self._on_pool(fields, slot, now)
                else:
                    self._on_amm_trade(
                        fields, name == "BuyEvent", slot, signature, now, index
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                self.counts["state_decode_refusals"] += 1
                self.mark_evidence_failure((mint,), now, "state_decode_refused")
        if decode_refused:
            self.mark_evidence_failure(related, now, "event_decode_refused")

    def mark_evidence_failure(
        self,
        mints: set[str] | tuple[str, ...] | dict[str, dict],
        now: float,
        reason: str,
    ) -> None:
        for mint in mints:
            coin = self.coins.get(mint)
            if coin is not None:
                self.reserve(512, coin)
                coin["stream_gap"] = True
                coin["event_evidence_complete"] = False
                coin["event_evidence_failures"].append(
                    {
                        "received_monotonic": now,
                        "reason": reason,
                    }
                )

    def _on_create(
        self, ev: dict, slot: int, sig: str, now: float, provenance: dict
    ) -> None:
        mint = ev["mint"]
        if mint in self.created_mints:
            self.counts["duplicate_creations"] += 1
            return
        if self.admission_cutoff is None or now >= self.admission_cutoff:
            self.counts["outside_admission_creations"] += 1
            return
        if (
            len(self.coins) >= self.max_coins
            or len(self.created_mints) >= self.max_events
        ):
            raise StorageCeiling("mint_limit")
        coin = {
            "schema_version": 2,
            "run_id": self.run_id,
            "mint": mint,
            "name": ev.get("name"),
            "symbol": ev.get("symbol"),
            "creator": ev.get("creator"),
            "create_slot": slot,
            "create_sig": sig,
            "create_ts": ev.get("timestamp"),
            "create_fields": ev,
            "create_provenance": provenance,
            "mayhem": ev.get("is_mayhem_mode"),
            "cashback": ev.get("is_cashback_enabled"),
            "quote_mint": ev.get("quote_mint"),
            "v_sol0": ev.get("virtual_quote_reserves", ev.get("virtual_sol_reserves")),
            "v_tok0": ev.get("virtual_token_reserves"),
            "supply": ev.get("token_total_supply"),
            "seen_at": now,
            "last_trade_at": now,
            "create_received_monotonic": now,
            "stream_gap": False,
            "trades": [],
            "trade_received_ms": [],
            "event_evidence_complete": True,
            "event_evidence_failures": [],
            "trade_real_token_reserves": [],
            "trade_signatures": [],
            "trade_event_indices": [],
            "trade_fields": [],
            "graduated_dslot": None,
            "graduated_ts": None,
            "graduated_at": None,
            "graduated_received_monotonic": None,
            "pool": None,
            "pool_dslot": None,
            "post_trades": [],
            "post_trade_received_ms": [],
            "post_trade_signatures": [],
            "post_trade_pools": [],
            "post_trade_fields": [],
            "post_trade_event_indices": [],
            "n_failed_excluded": 0,
            "_memory_bytes": 0,
        }
        self.reserve(256)
        self.reserve(8192 + len(json.dumps(ev).encode()) * 8, coin)
        self.created_mints.add(mint)
        self.coins[mint] = coin
        self.counts["creations"] += 1
        self.counts[
            "verified_creations" if provenance["verified"] else "unverified_creations"
        ] += 1

    def _on_complete(self, ev: dict, slot: int, now: float) -> None:
        coin = self.coins.get(ev["mint"])
        if coin is None or coin["graduated_at"] is not None:
            return
        self.graduations += 1
        coin["graduated_dslot"] = slot - coin["create_slot"]
        coin["graduated_ts"] = ev.get("timestamp")
        coin["graduated_at"] = coin["graduated_received_monotonic"] = now
        coin["last_trade_at"] = now

    def _on_pool(self, ev: dict, slot: int, now: float) -> None:
        coin = self.coins.get(ev.get("base_mint"))
        if coin is None:
            return
        if ev["pool"] not in self.pools:
            self.reserve(512, coin)
        self.pools[ev["pool"]] = coin["mint"]
        coin["pool"], coin["pool_dslot"] = ev["pool"], slot - coin["create_slot"]
        coin["last_trade_at"] = now

    def _on_amm_trade(
        self, ev: dict, is_buy: bool, slot: int, sig: str, now: float, index: int
    ) -> None:
        coin = self.coins.get(self.pools.get(ev["pool"]))
        if coin is None:
            return
        trade = [
            slot - coin["create_slot"],
            ev["user"],
            int(is_buy),
            int(ev["quote_amount_in"] if is_buy else ev["quote_amount_out"]),
            int(ev["base_amount_out"] if is_buy else ev["base_amount_in"]),
            int(ev["pool_base_token_reserves"]),
            int(ev["pool_quote_token_reserves"]),
            ev.get("virtual_quote_reserves"),
            ev.get("timestamp"),
        ]
        self.reserve(4096 + len(json.dumps(ev).encode()) * 8, coin)
        coin["post_trades"].append(trade)
        coin["post_trade_received_ms"].append((now - coin["seen_at"]) * 1000)
        coin["post_trade_signatures"].append(sig)
        coin["post_trade_pools"].append(ev["pool"])
        coin["post_trade_fields"].append(ev)
        coin["post_trade_event_indices"].append(index)
        coin["last_trade_at"] = now
        self.post_trades_seen += 1

    def _on_trade(self, ev: dict, slot: int, sig: str, now: float, index: int) -> None:
        coin = self.coins.get(ev["mint"])
        if coin is None:
            return
        trade = [
            slot - coin["create_slot"],
            ev["user"],
            int(ev["is_buy"]),
            int(ev.get("quote_amount", ev.get("sol_amount"))),
            int(ev["token_amount"]),
            int(ev.get("real_quote_reserves", ev.get("real_sol_reserves"))),
            int(ev.get("virtual_quote_reserves", ev.get("virtual_sol_reserves"))),
            int(ev["virtual_token_reserves"]),
            ev.get("timestamp"),
        ]
        self.reserve(4096 + len(json.dumps(ev).encode()) * 8, coin)
        coin["trades"].append(trade)
        coin["trade_received_ms"].append((now - coin["seen_at"]) * 1000)
        coin["trade_real_token_reserves"].append(ev.get("real_token_reserves"))
        coin["trade_signatures"].append(sig)
        coin["trade_event_indices"].append(index)
        coin["trade_fields"].append(ev)
        coin["last_trade_at"] = now
        self.trades_seen += 1

    def flush_finished(
        self, *, force: bool = False, reason: str = "capture_end"
    ) -> None:
        now = time.monotonic()
        for mint in list(self.coins):
            coin = self.coins[mint]
            idle = now - coin["last_trade_at"]
            why = reason if force else None
            if not force:
                if now - coin["seen_at"] >= self.max_age:
                    why = "max_age"
                elif coin["graduated_at"] is not None:
                    if now - coin["graduated_at"] >= self.postgrad_seconds:
                        why = "postgraduation_age"
                    elif idle >= self.postgrad_idle_seconds:
                        why = "postgraduation_idle"
                elif idle >= self.idle_seconds:
                    why = "idle"
            if why is not None:
                self._write(coin, partial=force, reason=why)
                for pool in [key for key, value in self.pools.items() if value == mint]:
                    del self.pools[pool]
                self.memory_bytes -= coin["_memory_bytes"]
                del self.coins[mint]

    def _write(self, coin: dict, *, partial: bool, reason: str) -> None:
        trades, creator = coin["trades"], coin["creator"]
        real = [t[5] for t in trades]
        peak_i = max(range(len(real)), key=real.__getitem__) if real else None
        creator_sells = [t for t in trades if t[1] == creator and t[2] == 0]
        dev_buy = next((t for t in trades if t[1] == creator and t[2] == 1), None)
        holders: dict[str, int] = {}
        holders_peak = positive_holders = 0
        for t in trades:
            old = holders.get(t[1], 0)
            new = old + (t[4] if t[2] else -t[4])
            holders[t[1]] = new
            positive_holders += int(new > 0) - int(old > 0)
            holders_peak = max(holders_peak, positive_holders)
        row = {
            k: v
            for k, v in coin.items()
            if k not in {"seen_at", "last_trade_at", "graduated_at", "_memory_bytes"}
        }
        row.update(
            {
                "partial": partial,
                "flush_reason": reason,
                "observed_duration_ms": (time.monotonic() - coin["seen_at"]) * 1000,
                "clock": "local_monotonic_receive_time",
                "n_trades": len(trades),
                "dev_buy_sol": dev_buy[3] / LAMPORTS if dev_buy else None,
                "dev_buy_pct_supply": dev_buy[4] / coin["supply"] * 100
                if dev_buy and coin["supply"]
                else None,
                "buyers_slot0": len(
                    {
                        t[1]
                        for t in trades
                        if t[0] == 0
                        and t[2] == 1
                        and t[1] not in (creator, MAYHEM_SOL_VAULT)
                    }
                ),
                "peak_sol": real[peak_i] / LAMPORTS if real else None,
                "peak_dslot": trades[peak_i][0] if real else None,
                "creator_first_sell_dslot": creator_sells[0][0]
                if creator_sells
                else None,
                "creator_first_sell_sol_level": creator_sells[0][5] / LAMPORTS
                if creator_sells
                else None,
                "creator_sold_pct": sum(t[4] for t in creator_sells) / dev_buy[4] * 100
                if dev_buy and dev_buy[4]
                else None,
                "holders_peak": holders_peak,
                "holders_end": positive_holders,
                "last_dslot": trades[-1][0] if trades else None,
                "final_sol": real[-1] / LAMPORTS if real else None,
                "buy_sol": sum(t[3] for t in trades if t[2]) / LAMPORTS,
                "sell_sol": sum(t[3] for t in trades if not t[2]) / LAMPORTS,
            }
        )
        self.emit("lifecycles", row)
        self.written += 1
        self.flush_reasons[reason] += 1

    def progress(self) -> None:
        self.journal(
            "heartbeat",
            counts=dict(self.counts),
            flush_reasons=dict(self.flush_reasons),
            tracking=len(self.coins),
            written=self.written,
            trades=self.trades_seen,
            post_trades=self.post_trades_seen,
            graduations=self.graduations,
            retained_memory_budget_bytes=self.memory_bytes,
        )
        print(
            f"tracking={len(self.coins)} written={self.written} trades={self.trades_seen} "
            f"post_trades={self.post_trades_seen} bytes={self.counts['bytes_written']}",
            flush=True,
        )

    def close(self) -> None:
        for handle in self.files.values():
            handle.close()


def connect(credentials: dict[str, str]) -> grpc.aio.Channel:
    token = credentials["GEYSER_API_TOKEN"]
    if credentials.get("GEYSER_AUTH_TYPE", "x-token") == "x-token":
        auth = grpc.metadata_call_credentials(
            lambda _, cb: cb((("x-token", token),), None)
        )
    else:
        auth = grpc.metadata_call_credentials(
            lambda _, cb: cb((("authorization", f"Basic {token}"),), None)
        )
    creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)
    return grpc.aio.secure_channel(
        credentials["GEYSER_ENDPOINT"],
        creds,
        options=(("grpc.max_receive_message_length", 16 * 1024**2),),
    )


def subscription() -> geyser_pb2.SubscribeRequest:
    req = geyser_pb2.SubscribeRequest()
    req.transactions["pump"].account_include.append(PUMP_PROGRAM)
    req.transactions["amm"].account_include.append(PUMP_AMM_PROGRAM)
    # Do not set optional `failed`: both successful and reverted transactions.
    req.slots["head"].filter_by_commitment = True
    req.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return req


async def run(
    rec: Recorder,
    minutes: float,
    credentials: dict[str, str],
    *,
    admission_seconds: float | None = None,
    drain_seconds: float = 600,
    max_reconnects: int = 20,
    stream_idle_seconds: float = 30,
) -> int:
    duration = minutes * 60
    admission_seconds = (
        duration - drain_seconds if admission_seconds is None else admission_seconds
    )
    for value in (duration, admission_seconds, drain_seconds, stream_idle_seconds):
        if not math.isfinite(value) or value <= 0:
            rec.close()
            raise ValueError("run bounds must be finite and positive")  # noqa: TRY003
    if admission_seconds + drain_seconds > duration or max_reconnects < 0:
        rec.close()
        raise ValueError(
            "admission plus drain must fit capture; reconnects must be nonnegative"
        )
    startup_deadline = rec.started + stream_idle_seconds
    hard_deadline = startup_deadline + duration
    deadline = startup_deadline
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    call = None
    interrupted = None

    def request_stop(sig: int) -> None:
        nonlocal interrupted
        interrupted = signal.Signals(sig).name
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop, sig)
    last_report = last_sweep = rec.started
    last_head = None
    gap_start = rec.started
    reconnect_start = None
    reconnects = 0
    terminal_reason = "fatal_failure"
    exit_code = 1
    error_type = None
    try:
        rec.journal(
            "start",
            source="geyser",
            commitment="processed",
            recorder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            endpoint_sha256=hashlib.sha256(
                credentials["GEYSER_ENDPOINT"].encode()
            ).hexdigest(),
            filters={
                "programs": [PUMP_PROGRAM, PUMP_AMM_PROGRAM],
                "failed": "included",
                "slots": "processed",
            },
            clock={
                "name": "time.monotonic",
                "implementation": time.get_clock_info("monotonic").implementation,
                "resolution": time.get_clock_info("monotonic").resolution,
                "origin_monotonic": rec.started,
            },
            protobuf_type="geyser.SubscribeUpdate",
            raw_encoding="base64:zlib:geyser.SubscribeUpdate",
            raw_schema_version=3,
            event_index="zero_based_original_log_index",
            parser="monitoring.event_normalization.normalize_geyser_update+monitoring.parser_dispatch.parse_normalized_event+platforms.pumpfun.event_parser.PumpFunEventParser",
            idl_sha256={
                name: hashlib.sha256((ROOT / "idl" / name).read_bytes()).hexdigest()
                for name in ("pump_fun_idl.json", "pump_swap_idl.json")
            },
            output_paths={k: str(v) for k, v in rec.paths.items()},
            bounds={
                "minutes": minutes,
                "requested_observation_seconds": duration,
                "startup_timeout_seconds": stream_idle_seconds,
                "max_elapsed_seconds": stream_idle_seconds + duration,
                "admission_seconds": admission_seconds,
                "drain_seconds": drain_seconds,
                "admission_anchor": "first_processed_slot_receive",
                "idle_seconds": rec.idle_seconds,
                "max_age": rec.max_age,
                "postgrad_seconds": rec.postgrad_seconds,
                "postgrad_idle_seconds": rec.postgrad_idle_seconds,
                "max_bytes": rec.max_bytes,
                "max_memory_bytes": rec.max_memory_bytes,
                "max_coins": rec.max_coins,
                "max_events": rec.max_events,
                "max_reconnects": max_reconnects,
                "stream_idle_seconds": stream_idle_seconds,
                "startup_deadline_monotonic": startup_deadline,
                "hard_deadline_monotonic": hard_deadline,
                "effective_deadline_monotonic": deadline,
            },
        )
        while not stop.is_set() and time.monotonic() < deadline:
            channel = None
            pending = None
            try:
                channel = connect(credentials)
                call = geyser_pb2_grpc.GeyserStub(channel).Subscribe()
                await asyncio.wait_for(
                    call.write(subscription()),
                    timeout=min(15, max(0.001, deadline - time.monotonic())),
                )
                await asyncio.wait_for(
                    call.initial_metadata(),
                    timeout=min(15, max(0.001, deadline - time.monotonic())),
                )
                rec.journal("connected", reconnect_number=reconnects)
                last_update = time.monotonic()

                async def receive(
                    stream: grpc.aio.StreamStreamCall = call,
                ) -> tuple[geyser_pb2.SubscribeUpdate, float, int]:
                    update = await stream.read()
                    return update, time.monotonic(), time.time_ns()

                pending = asyncio.create_task(receive())
                while not stop.is_set() and time.monotonic() < deadline:
                    done, _ = await asyncio.wait(
                        {pending},
                        timeout=min(1, max(0.001, deadline - time.monotonic())),
                    )
                    tick = time.monotonic()
                    if tick - last_sweep >= 1:
                        rec.flush_finished()
                        last_sweep = tick
                    if (
                        gap_start is None
                        and last_head is not None
                        and tick - last_head > MAX_SLOT_GAP_SECONDS
                    ):
                        gap_start = last_head
                        rec.counts["slot_gaps"] += 1
                        rec.journal(
                            "gap_start",
                            start_monotonic=gap_start,
                            reason="slot_silence",
                        )
                        for coin in rec.coins.values():
                            coin["stream_gap"] = True
                    if tick - last_report >= REPORT_EVERY_SECONDS:
                        rec.progress()
                        last_report = tick
                    if not done:
                        if tick - last_update > stream_idle_seconds:
                            raise TimeoutError("stream_idle")  # noqa: TRY301
                        continue
                    update, now, wall_ns = pending.result()
                    pending = None
                    if now >= deadline:
                        break
                    last_update = now
                    if update is grpc.aio.EOF:
                        raise ConnectionError("stream_ended")  # noqa: TRY301
                    rec.counts["updates"] += 1
                    rec.counts["ingress_bytes"] += update.ByteSize()
                    if update.HasField("slot"):
                        rec.emit(
                            "slots",
                            {
                                "schema_version": 2,
                                "run_id": rec.run_id,
                                "slot": update.slot.slot,
                                "received_monotonic": now,
                                "gap_seconds": None
                                if last_head is None
                                else now - last_head,
                            },
                        )
                        rec.counts["slot_updates"] += 1
                        if gap_start is not None:
                            rec.journal(
                                "gap_end", start_monotonic=gap_start, end_monotonic=now
                            )
                            gap_start = None
                        if reconnect_start is not None:
                            rec.journal(
                                "reconnect_end",
                                start_monotonic=reconnect_start,
                                end_monotonic=now,
                            )
                            reconnect_start = None
                        last_head = now
                        if rec.admission_cutoff is None:
                            rec.first_head_slot = update.slot.slot
                            rec.first_head_received_monotonic = now
                            rec.admission_cutoff = now + admission_seconds
                            deadline = rec.admission_cutoff + drain_seconds
                            rec.journal(
                                "admission",
                                start_monotonic=now,
                                cutoff_monotonic=rec.admission_cutoff,
                                first_head_slot=rec.first_head_slot,
                                first_head_received_monotonic=rec.first_head_received_monotonic,
                                admission_seconds=admission_seconds,
                                drain_seconds=drain_seconds,
                                drain_deadline_monotonic=deadline,
                                effective_deadline_monotonic=deadline,
                            )
                    elif update.HasField("transaction"):
                        rec.on_transaction(update, now, wall_ns)
                        if gap_start is not None:
                            for coin in rec.coins.values():
                                coin["stream_gap"] = True
                                if coin["create_received_monotonic"] == now:
                                    rec.mark_evidence_failure(
                                        (coin["mint"],),
                                        now,
                                        "creation_during_stream_gap",
                                    )
                    elif update.HasField("ping"):
                        await asyncio.wait_for(
                            call.write(
                                geyser_pb2.SubscribeRequest(
                                    ping=geyser_pb2.SubscribeRequestPing(id=1)
                                )
                            ),
                            timeout=min(5, max(0.001, deadline - time.monotonic())),
                        )
                    pending = asyncio.create_task(receive())
            except (grpc.RpcError, ConnectionError, TimeoutError) as exc:
                if stop.is_set() or time.monotonic() >= deadline:
                    break
                reconnects += 1
                rec.counts["stream_failures"] += 1
                if reconnect_start is None:
                    reconnect_start = time.monotonic()
                    rec.mark_evidence_failure(
                        rec.coins, reconnect_start, "reconnect_gap"
                    )
                if gap_start is None:
                    gap_start = last_head if last_head is not None else reconnect_start
                for coin in rec.coins.values():
                    coin["stream_gap"] = True
                rec.journal(
                    "reconnect_start",
                    start_monotonic=reconnect_start,
                    attempt=reconnects,
                    error_type=type(exc).__name__,
                )
                if reconnects > max_reconnects:
                    terminal_reason = "reconnect_limit"
                    raise
                try:
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=min(
                            2 ** min(reconnects - 1, 4),
                            max(0.001, deadline - time.monotonic()),
                        ),
                    )
                except TimeoutError:
                    pass
            finally:
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                if call is not None:
                    call.cancel()
                if channel is not None:
                    await channel.close()
        if interrupted:
            terminal_reason = f"interrupted:{interrupted}"
            exit_code = 130 if interrupted == "SIGINT" else 143
        elif (
            rec.admission_cutoff is None
            or reconnect_start is not None
            or gap_start is not None
        ):
            terminal_reason = "capture_ended_without_live_slot_coverage"
        elif time.monotonic() < deadline:
            terminal_reason = "capture_ended_before_drain_deadline"
        else:
            terminal_reason = "capture_deadline"
            exit_code = 0
    except StorageCeiling as exc:
        rec.counts["storage_ceilings"] += 1
        terminal_reason = f"storage_ceiling:{exc}"
        error_type = type(exc).__name__
        exit_code = 1
    except asyncio.CancelledError:
        terminal_reason = "interrupted:cancelled"
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - preserve an explicit incomplete terminal record
        rec.counts["fatal_failures"] += 1
        error_type = type(exc).__name__
        exit_code = 1
    finally:
        try:
            try:
                rec.flush_finished(force=True, reason=terminal_reason)
            except StorageCeiling as exc:
                rec.counts["storage_ceilings"] += 1
                terminal_reason = f"storage_ceiling:{exc}"
                error_type = type(exc).__name__
                exit_code = 1
            except Exception as exc:  # noqa: BLE001 - terminal evidence survives a failed flush
                rec.counts["fatal_failures"] += 1
                terminal_reason = "fatal_failure"
                error_type = type(exc).__name__
                exit_code = 1
            rec.journal(
                "terminal",
                terminal=True,
                terminal_reason=terminal_reason,
                complete=exit_code == 0,
                exit_code=exit_code,
                error_type=error_type,
                first_head_slot=rec.first_head_slot,
                first_head_received_monotonic=rec.first_head_received_monotonic,
                admission_seconds=admission_seconds,
                drain_seconds=drain_seconds,
                cutoff_monotonic=rec.admission_cutoff,
                drain_deadline_monotonic=(
                    None if rec.admission_cutoff is None else deadline
                ),
                requested_observation_seconds=duration,
                startup_timeout_seconds=stream_idle_seconds,
                max_elapsed_seconds=stream_idle_seconds + duration,
                startup_deadline_monotonic=startup_deadline,
                hard_deadline_monotonic=hard_deadline,
                effective_deadline_monotonic=deadline,
                counters_scope="before_this_terminal_record",
                counts=dict(rec.counts),
                flush_reasons=dict(rec.flush_reasons),
                written=rec.written,
                trades=rec.trades_seen,
                post_trades=rec.post_trades_seen,
                unflushed_coins=len(rec.coins),
                open_gap_start_monotonic=gap_start,
                open_reconnect_start_monotonic=reconnect_start,
                ended_monotonic=time.monotonic(),
            )
        finally:
            rec.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    print(
        f"{terminal_reason}: written={rec.written} trades={rec.trades_seen}", flush=True
    )
    return exit_code


def scheduler_self_check() -> None:  # noqa: C901, PLR0915 - one bounded scheduling check
    """Exercise real run/journals on a virtual clock; no provider calls or sleeps."""
    import tempfile  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    async def scenario(directory: Path, mode: str) -> None:  # noqa: C901, PLR0915 - one bounded lifecycle scenario
        clock = 100.0
        reads = 0
        disconnected = False
        capture_task = asyncio.current_task()
        mint = base58.b58encode(bytes([1]) * 32).decode()

        class Stream:
            async def write(self, _request: object) -> None:
                pass

            async def initial_metadata(self) -> None:
                pass

            async def close(self) -> None:
                pass

            def cancel(self) -> None:
                pass

            async def read(self) -> geyser_pb2.SubscribeUpdate:
                nonlocal clock, reads, disconnected
                clock += (30 if mode == "late" else 5) if reads == 0 else 0.5
                reads += 1
                if mode in {"cancelled", "cancelled_storage"} and reads == 2:
                    rec._on_create(  # noqa: SLF001 - admit a retained coin before cancellation
                        {"mint": mint},
                        reads,
                        "create",
                        clock,
                        {"verified": False, "reason": "self_check"},
                    )
                    if mode == "cancelled_storage":
                        rec.max_bytes = rec.counts["bytes_written"] + TERMINAL_RESERVE
                    capture_task.cancel()
                if (
                    mode in {"reconnect", "absent_reconnect", "incomplete"}
                    and clock >= 110
                    and not disconnected
                ):
                    disconnected = True
                    return grpc.aio.EOF
                update = geyser_pb2.SubscribeUpdate()
                if mode in {"absent", "absent_reconnect"}:
                    update.ping.SetInParent()
                else:
                    update.slot.slot = reads
                return update

        async def wait_for(awaitable: object, timeout: float) -> object:
            nonlocal clock
            if awaitable.cr_code is asyncio.Event.wait.__code__:
                awaitable.close()
                clock += timeout
                raise TimeoutError
            return await awaitable

        stream = Stream()
        fake_time = SimpleNamespace(
            monotonic=lambda: clock,
            time_ns=lambda: int(clock * 1_000_000_000),
            get_clock_info=time.get_clock_info,
        )
        with (
            patch.dict(
                globals(),
                time=fake_time,
                connect=lambda _credentials: stream,
                REPORT_EVERY_SECONDS=math.inf,
                geyser_pb2_grpc=SimpleNamespace(
                    GeyserStub=lambda _channel: SimpleNamespace(
                        Subscribe=lambda: stream
                    )
                ),
            ),
            patch.object(asyncio, "wait_for", wait_for),
        ):
            rec = Recorder(directory / f"{mode}.jsonl", 120, 1200)
            result = await run(
                rec,
                120,
                {"GEYSER_ENDPOINT": "offline"},
                max_reconnects=0 if mode == "incomplete" else 20,
            )
        journal = [
            json.loads(line) for line in rec.paths["run"].read_text().splitlines()
        ]
        bounds, terminal = journal[0]["bounds"], journal[-1]
        assert bounds["requested_observation_seconds"] == 7200
        assert bounds["startup_timeout_seconds"] == 30
        assert bounds["max_elapsed_seconds"] == 7230
        assert bounds["startup_deadline_monotonic"] == 130
        assert bounds["hard_deadline_monotonic"] == 7330
        assert terminal["hard_deadline_monotonic"] == 7330
        assert terminal["ended_monotonic"] <= 7330
        admissions = [row for row in journal if row["kind"] == "admission"]
        if mode in {"absent", "absent_reconnect", "late"}:
            assert result != 0 and not terminal["complete"] and not admissions
            assert terminal["drain_deadline_monotonic"] is None
            assert terminal["ended_monotonic"] == 130
            assert terminal["effective_deadline_monotonic"] == 130
            assert terminal["counts"]["stream_failures"] == (mode == "absent_reconnect")
        else:
            assert len(admissions) == 1
            assert admissions[0]["start_monotonic"] == 105
            assert admissions[0]["cutoff_monotonic"] == 6705
            assert terminal["cutoff_monotonic"] == 6705
            assert admissions[0]["drain_deadline_monotonic"] == 7305
            assert terminal["drain_deadline_monotonic"] == 7305
            assert terminal["effective_deadline_monotonic"] == 7305
            if mode in {"cancelled", "cancelled_storage"}:
                rows = [
                    json.loads(line)
                    for line in rec.paths["lifecycles"].read_text().splitlines()
                ]
                refused = mode == "cancelled_storage"
                assert result != 0 and not terminal["complete"]
                assert terminal["ended_monotonic"] < 7305
                assert terminal["counts"]["creations"] == 1
                assert terminal["written"] == len(rows) == (0 if refused else 1)
                assert terminal["unflushed_coins"] == len(rec.coins) == int(refused)
                assert terminal["written"] + terminal["unflushed_coins"] == 1
                assert (
                    sum(path.stat().st_size for path in rec.paths.values())
                    <= rec.max_bytes
                )
                if refused:
                    assert terminal["terminal_reason"] == "storage_ceiling:disk_bytes"
                    assert terminal["error_type"] == "StorageCeiling"
                    assert terminal["counts"]["storage_ceilings"] == 1
                    assert terminal["flush_reasons"] == {}
                    assert mint in rec.coins
                else:
                    assert result == 130
                    assert terminal["terminal_reason"] == "interrupted:cancelled"
                    assert rows[0]["mint"] == mint and rows[0]["partial"]
                    assert rows[0]["flush_reason"] == "interrupted:cancelled"
                return
            if mode == "incomplete":
                assert result != 0 and not terminal["complete"]
                assert terminal["ended_monotonic"] < 7305
            else:
                assert result == 0 and terminal["complete"]
                assert terminal["ended_monotonic"] == 7305
                assert terminal["counts"]["stream_failures"] == (mode == "reconnect")

    with tempfile.TemporaryDirectory() as directory:
        for mode in (
            "delayed",
            "reconnect",
            "absent",
            "absent_reconnect",
            "late",
            "incomplete",
            "cancelled",
            "cancelled_storage",
        ):
            asyncio.run(scenario(Path(directory), mode))
    print("scheduler self-check passed")


def self_check() -> None:  # noqa: PLR0915 - one offline evidence check
    """No providers: attribution, replay identity, reverted updates and scheduling."""
    import tempfile  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    logs = [
        f"Program {PUMP_PROGRAM} invoke [1]",
        "Program data: YQ==",
        "Program foreign invoke [2]",
        "Program data: Yg==",
        "Program foreign success",
        f"Program {PUMP_PROGRAM} invoke [2]",
        "Program data: Yw==",
        f"Program {PUMP_PROGRAM} failed: custom error",
        "Program data: ZA==",
        f"Program {PUMP_PROGRAM} success",
    ]
    entries = attribute_program_logs(logs)
    assert [(i, p, ok) for i, p, _, ok in entries] == [
        (1, PUMP_PROGRAM, True),
        (3, "foreign", True),
        (6, PUMP_PROGRAM, False),
        (8, PUMP_PROGRAM, True),
    ]
    with tempfile.TemporaryDirectory() as directory:
        rec = Recorder(Path(directory) / "check.jsonl", 60, 600)
        try:
            assert rec.remember("signature", 1)
            assert rec.remember("signature", 2)
            assert not rec.remember("signature", 1)
            rec.admission_cutoff = time.monotonic() + 60
            mint = base58.b58encode(bytes([1]) * 32).decode()
            rec._on_create(
                {"mint": mint},
                1,
                "create",
                time.monotonic(),
                {"verified": False, "reason": "self_check"},
            )
            update = geyser_pb2.SubscribeUpdate()
            info = update.transaction.transaction
            info.signature = bytes([2]) * 64
            info.transaction.message.account_keys.append(bytes([1]) * 32)
            fields = {
                "mint": mint,
                "user": mint,
                "is_buy": True,
                "sol_amount": 1,
                "token_amount": 2,
                "real_sol_reserves": 3,
                "virtual_sol_reserves": 4,
                "virtual_token_reserves": 5,
                "real_token_reserves": 6,
            }
            discriminator = b"check123"
            rec.decoders[PUMP_PROGRAM, discriminator] = (
                SimpleNamespace(decode_event_data=lambda *_: {"fields": fields}),
                "TradeEvent",
                frozenset(fields),
            )
            payload = PROGRAM_DATA + base64.b64encode(discriminator).decode()
            info.meta.log_messages.extend(
                [
                    f"Program {PUMP_PROGRAM} invoke [1]",
                    payload,
                    payload,
                    f"Program {PUMP_PROGRAM} success",
                ]
            )
            info.meta.err.err = b"failed"
            rec.on_transaction(update, time.monotonic(), time.time_ns())
            rec.on_transaction(update, time.monotonic(), time.time_ns())
            assert rec.coins[mint]["trades"] == []
            assert rec.coins[mint]["n_failed_excluded"] == 1
            assert rec.counts["duplicate_failed_transactions"] == 1
            info.meta.ClearField("err")
            info.signature = bytes([3]) * 64
            rec.on_transaction(update, time.monotonic(), time.time_ns())
            rec.on_transaction(update, time.monotonic(), time.time_ns())
            assert len(rec.coins[mint]["trades"]) == 2
            assert rec.coins[mint]["trade_event_indices"] == [1, 2]
            assert rec.coins[mint]["trades"][0][3:8] == [1, 2, 3, 4, 5]
            modern = {
                **fields,
                "sol_amount": 0,
                "real_sol_reserves": 0,
                "virtual_sol_reserves": 0,
                "quote_amount": 7,
                "real_quote_reserves": 8,
                "virtual_quote_reserves": 9,
            }
            rec._on_trade(modern, 1, "modern", time.monotonic(), 1)  # noqa: SLF001
            assert rec.coins[mint]["trades"][-1][3:8] == [7, 2, 8, 9, 5]
            assert rec.coins[mint]["trade_fields"][-1] == modern
            rec._on_trade(  # noqa: SLF001
                {
                    **fields,
                    "quote_amount": 0,
                    "real_quote_reserves": 0,
                    "virtual_quote_reserves": 0,
                },
                1,
                "zero",
                time.monotonic(),
                1,
            )
            assert rec.coins[mint]["trades"][-1][3:8] == [0, 2, 0, 0, 5]
            retained = json.loads(rec.paths["raw"].read_text().splitlines()[-1])
            restored = zlib.decompress(
                base64.b64decode(retained["protobuf_zlib_base64"], validate=True)
            )
            assert retained["encoding"] == "base64:zlib:geyser.SubscribeUpdate"
            assert restored == update.SerializeToString()
            assert len(restored) == retained["raw_size"]
            assert hashlib.sha256(restored).hexdigest() == retained["raw_sha256"]
        finally:
            rec.close()
    scheduler_self_check()
    print("self-check passed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--minutes",
        type=float,
        default=120,
        help="post-first-processed-head observation budget, including admission and drain",
    )
    ap.add_argument("--idle", type=float, default=120)
    ap.add_argument("--max-age", type=float, default=1200)
    ap.add_argument("--postgrad-seconds", type=float, default=600)
    ap.add_argument("--postgrad-idle-seconds", type=float, default=120)
    ap.add_argument(
        "--admission-seconds",
        type=float,
        help="from first processed head; default observation budget minus drain",
    )
    ap.add_argument("--drain-seconds", type=float, default=600)
    ap.add_argument("--max-bytes", type=int, default=2 * 1024**3)
    ap.add_argument("--max-memory-bytes", type=int, default=256 * 1024**2)
    ap.add_argument("--max-coins", type=int, default=10000)
    ap.add_argument("--max-events", type=int, default=2_000_000)
    ap.add_argument("--max-reconnects", type=int, default=20)
    ap.add_argument(
        "--stream-idle-seconds",
        type=float,
        default=30,
        help="startup bound before first processed head, then stream inactivity limit",
    )
    ap.add_argument("--out", type=Path)
    ap.add_argument(
        "--run-id", help="public study identity; defaults to a generated UUID"
    )
    ap.add_argument(
        "--env-file", type=Path, help="explicit provider-only GEYSER_* file"
    )
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        self_check()
        return 0
    positive = (
        args.minutes,
        args.idle,
        args.max_age,
        args.postgrad_seconds,
        args.postgrad_idle_seconds,
        args.drain_seconds,
        args.max_bytes,
        args.max_memory_bytes,
        args.max_coins,
        args.max_events,
        args.stream_idle_seconds,
    )
    admission = (
        args.minutes * 60 - args.drain_seconds
        if args.admission_seconds is None
        else args.admission_seconds
    )
    if any(not math.isfinite(x) or x <= 0 for x in (*positive, admission)):
        ap.error("all capture/storage/age/idle bounds must be finite and positive")
    if admission + args.drain_seconds > args.minutes * 60 or args.max_reconnects < 0:
        ap.error(
            "admission plus drain must fit capture; reconnects must be nonnegative"
        )
    if args.env_file is None:
        ap.error("--env-file is required unless --self-check")
    resolved = args.env_file.resolve()
    if (
        resolved.name.casefold() in {".env", ".env~"}
        or args.env_file.name.casefold() in {".env", ".env~"}
        or any(
            part.upper().startswith("ENVDATA")
            for part in (*args.env_file.parts, *resolved.parts)
        )
    ):
        ap.error("protected provider file")
    values = dotenv_values(resolved, interpolate=False)
    credentials = {
        key: values[key]
        for key in ("GEYSER_ENDPOINT", "GEYSER_API_TOKEN", "GEYSER_AUTH_TYPE")
        if isinstance(values.get(key), str) and values[key]
    }
    if not credentials.get("GEYSER_ENDPOINT") or not credentials.get(
        "GEYSER_API_TOKEN"
    ):
        ap.error("GEYSER_ENDPOINT and GEYSER_API_TOKEN are required")
    if credentials.get("GEYSER_AUTH_TYPE", "x-token") not in {"x-token", "basic"}:
        ap.error("unsupported GEYSER_AUTH_TYPE")
    out = args.out or Path(__file__).with_name(
        f"lifecycles_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    rec = Recorder(
        out,
        args.idle,
        args.max_age,
        postgrad_seconds=args.postgrad_seconds,
        postgrad_idle_seconds=args.postgrad_idle_seconds,
        max_bytes=args.max_bytes,
        max_memory_bytes=args.max_memory_bytes,
        max_coins=args.max_coins,
        max_events=args.max_events,
        run_id=args.run_id,
    )
    return asyncio.run(
        run(
            rec,
            args.minutes,
            credentials,
            admission_seconds=admission,
            drain_seconds=args.drain_seconds,
            max_reconnects=args.max_reconnects,
            stream_idle_seconds=args.stream_idle_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
