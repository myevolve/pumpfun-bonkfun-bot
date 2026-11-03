"""Raydium LaunchLab (letsbonk.fun) curve-phase lifecycle capture.

Read-only Geyser research instrument mirroring record_lifecycles.py: the same
proven admission/drain/reconnect run loop and the same tape schema wherever
pump.fun and LaunchLab semantics match. Protocol divergences are explicit:

- Creation has no event. The initialize / initialize_v2 /
  initialize_with_token_2022 instruction (LetsBonkEventParser + IDLParser)
  admits the coin; ``fund_target`` and ``supply`` come from the curve args
  (``total_quote_fund_raising`` / ``supply`` inside ``CurveParams``).
- Graduation has no event. It is derived from fund-target arithmetic on
  TradeEvent reserves (``real_sol_reserves >= fund_target``) or from
  migrate_to_amm / migrate_to_cpswap instructions in-stream. The signal used
  is recorded as ``graduated_signal``; the migration instruction also sets
  ``migrated_dslot`` and ``migration_name``.
- The LaunchLab TradeEvent carries no user (the shared decoder uses the fee
  payer ``account_keys[0]``), no creator (empty) and no timestamp (0);
  creation timestamps do not exist in instructions at all. Divergent tape
  fields are null, never invented.
- ``post_trades`` after migration would arrive on Raydium AMM/CPMM programs,
  which are not subscribed here; ``post_trades`` stays empty and the
  postgraduation window only bounds row flush.

Never signs, never sends. Requires a provider env projection (``--env-file``)
carrying GEYSER_ENDPOINT / GEYSER_API_TOKEN; refuses the root .env and
ENVDATA* paths by name exactly like record_lifecycles.py.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import struct
import sys
import time
import zlib
from dataclasses import asdict
from itertools import chain
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import base58
from dotenv import dotenv_values
from record_lifecycles import Recorder, StorageCeiling, run

from monitoring.trade_flow import TradeEvent, decode_launchlab_trade_events
from platforms.letsbonk.event_parser import LetsBonkEventParser
from src.geyser.generated import geyser_pb2
from utils.idl_parser import IDLParser

# Standalone src imports, protocol bounds and offline assertions are intentional.
# ruff: noqa: E402, PLR2004, S101

LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
LAUNCHLAB_IDL = ROOT / "idl" / "raydium_launchlab_idl.json"

# initialize / initialize_v2 / initialize_with_token_2022 discriminators.
INIT_DISCS = frozenset(
    (
        bytes.fromhex("afaf6d1f0d989bed"),
        bytes.fromhex("4399af27da102620"),
        bytes.fromhex("25be7ede2c9aab11"),
    )
)

# discriminator -> (instruction name, pool_state account index). base_mint is
# account index 1 in both variants.
MIGRATION_DISCS = {
    bytes.fromhex("cf52c091fecf91df"): ("migrate_to_amm", 23),
    bytes.fromhex("885cc8671cda908c"): ("migrate_to_cpswap", 17),
}


class LetsBonkRecorder(Recorder):
    """Recorder with the LetsBonk creation/trade/graduation pipeline.

    The inherited bookkeeping (bounds, dedup, raw tape, journals, flush) is
    reused unchanged; only the per-transaction decode is LetsBonk-specific.
    """

    def __init__(  # noqa: PLR0913 - mirrors record_lifecycles.Recorder
        self,
        out_path: Path,
        idle_seconds: float,
        max_age: float,
        *,
        postgrad_seconds: float = 60.0,
        postgrad_idle_seconds: float = 30.0,
        max_bytes: int = 2 * 1024**3,
        max_memory_bytes: int = 256 * 1024**2,
        max_coins: int = 2000,
        max_events: int = 200_000,
        run_id: str | None = None,
    ) -> None:
        super().__init__(
            out_path,
            idle_seconds,
            max_age,
            postgrad_seconds=postgrad_seconds,
            postgrad_idle_seconds=postgrad_idle_seconds,
            max_bytes=max_bytes,
            max_memory_bytes=max_memory_bytes,
            max_coins=max_coins,
            max_events=max_events,
            run_id=run_id,
        )
        self.parser = IDLParser(str(LAUNCHLAB_IDL))
        self.letsbonk_parser = LetsBonkEventParser(self.parser)
        self.pool_to_mint: dict[str, str] = {}
        self.recorder_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.idl_sha256 = hashlib.sha256(LAUNCHLAB_IDL.read_bytes()).hexdigest()

    def journal(self, kind: str, *, terminal: bool = False, **fields: object) -> None:
        """Rewrite the pump-shaped start entry with LetsBonk protocol truth."""
        if kind == "start":
            fields.update(
                filters={
                    "programs": [LAUNCHLAB_PROGRAM],
                    "failed": "included",
                    "slots": "processed",
                },
                parser=(
                    "platforms.letsbonk.event_parser.LetsBonkEventParser"
                    "+monitoring.trade_flow.decode_launchlab_trade_events"
                ),
                idl_sha256={"raydium_launchlab_idl.json": self.idl_sha256},
                recorder="run_letsbonk_lifecycle_capture.py",
                recorder_sha256=self.recorder_sha256,
            )
        super().journal(kind, terminal=terminal, **fields)

    def on_transaction(  # noqa: C901, PLR0912, PLR0915 - mirror of the pump decoder path
        self, update: geyser_pb2.SubscribeUpdate, now: float, wall_time_ns: int
    ) -> None:
        """Decode one LaunchLab transaction: creations, trades, migrations."""
        info = update.transaction.transaction
        slot = update.transaction.slot
        signature = base58.b58encode(bytes(info.signature)).decode()
        failed = info.meta.HasField("err")
        self.counts["transactions"] += 1
        self.counts["failed_transactions"] += int(failed)
        account_keys: list[str] = []
        raw_keys: list[bytes] = []
        for key in (
            *info.transaction.message.account_keys,
            *info.meta.loaded_writable_addresses,
            *info.meta.loaded_readonly_addresses,
        ):
            raw = bytes(key)
            raw_keys.append(raw)
            account_keys.append(base58.b58encode(raw).decode())
        accounts = set(account_keys)
        logs = list(info.meta.log_messages)
        events = decode_launchlab_trade_events(
            logs,
            slot=slot,
            signature=signature,
            idl_parser=self.parser,
            pool_to_mint=self.pool_to_mint,
            account_keys=account_keys,
        )
        # The mint linkage for LaunchLab trades lives inside the events
        # (pool_state -> mint registry), so decode precedes the related check.
        related = accounts.intersection(self.coins)
        related.update(
            self.pool_to_mint[pool] for pool in accounts.intersection(self.pool_to_mint)
        )
        related.update(event.mint for event in events if event.mint in self.coins)
        creations: list[tuple[int, object]] = []
        migrations: list[tuple[str, str]] = []
        for ordinal, instruction in enumerate(
                chain(
                    info.transaction.message.instructions,
                    (
                        instruction
                        for group in info.meta.inner_instructions
                        for instruction in group.instructions
                    ),
                )
        ):
            if (
                instruction.program_id_index >= len(account_keys)
                or account_keys[instruction.program_id_index] != LAUNCHLAB_PROGRAM
            ):
                continue
            disc = bytes(instruction.data)[:8]
            if disc in INIT_DISCS:
                creations.append((ordinal, instruction))
            elif disc in MIGRATION_DISCS and len(instruction.accounts) > 1:
                # base_mint is account index 1 in both migration variants.
                base_index = instruction.accounts[1]
                if base_index < len(account_keys):
                    migrations.append(
                        (MIGRATION_DISCS[disc][0], account_keys[base_index])
                    )
        creation_hint = bool(creations)
        if not creation_hint and not related:
            self.counts["out_of_universe_transactions"] += 1
            return
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
                "attribution_refusal": None,
                "event_decode_refused": False,
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
        for ordinal, instruction in creations:
            if not self.remember(signature, ordinal):
                continue
            self._on_launchlab_creation(
                bytes(instruction.data), list(instruction.accounts), raw_keys,
                slot, signature, now,
            )
        for name, base_mint in migrations:
            coin = self.coins.get(base_mint)
            if coin is None:
                self.counts["out_of_universe_events"] += 1
                continue
            coin["migrated_dslot"] = slot - coin["create_slot"]
            coin["migration_name"] = name
            if coin["graduated_at"] is None:
                self._graduate(coin, slot, now, signal="migrate_instruction")
        for ordinal, event in enumerate(events):
            coin = self.coins.get(event.mint)
            if coin is None:
                self.counts["out_of_universe_events"] += 1
                continue
            # Trades share the signature's dedup namespace with creations;
            # negative ordinals cannot collide with instruction ordinals.
            if not self.remember(signature, -(ordinal + 1)):
                continue
            self._on_launchlab_trade(event, coin, slot, signature, now, ordinal)

    def _on_launchlab_creation(  # noqa: PLR0913 - mirror of record_lifecycles.Recorder
        self,
        data: bytes,
        accounts: list[int],
        raw_keys: list[bytes],
        slot: int,
        signature: str,
        now: float,
    ) -> None:
        """Admit one coin from a LaunchLab initialize instruction."""
        token = self.letsbonk_parser.parse_token_creation_from_instruction(
            data, accounts, raw_keys
        )
        if token is None:
            self.counts["decode_refusals"] += 1
            return
        curve_data = (token.additional_data.get("curve_param") or {}).get("data") or {}
        if "supply" not in curve_data:
            # IDLParser's defined-type decode wraps the enum payload once more.
            curve_data = curve_data.get("data") or {}
        fields = {
            "mint": str(token.mint),
            "name": token.name,
            "symbol": token.symbol,
            "creator": str(token.creator),
            "timestamp": None,
            "is_mayhem_mode": None,
            "is_cashback_enabled": None,
            "quote_mint": str(token.quote_mint),
            "virtual_quote_reserves": None,
            "virtual_token_reserves": None,
            "token_total_supply": curve_data.get("supply"),
            "fund_target": curve_data.get("total_quote_fund_raising"),
            "instruction_name": token.additional_data.get("instruction_name"),
            "pool_state": str(token.pool_state),
        }
        self._on_create(
            fields,
            slot,
            signature,
            now,
            {"verified": True, "reason": "initialize_instruction"},
        )
        coin = self.coins.get(fields["mint"])
        if coin is not None:
            coin["fund_target"] = fields["fund_target"]
            coin["pool_state"] = fields["pool_state"]
            coin["graduated_signal"] = None
            coin["migrated_dslot"] = None
            coin["migration_name"] = None
            self.pool_to_mint[fields["pool_state"]] = fields["mint"]

    def _on_launchlab_trade(  # noqa: PLR0913 - mirror of record_lifecycles.Recorder
        self,
        event: TradeEvent,
        coin: dict,
        slot: int,
        signature: str,
        now: float,
        index: int,
    ) -> None:
        """Append one decoded trade tuple; derive fund-target graduation."""
        trade = [
            slot - coin["create_slot"],
            event.user,
            int(event.is_buy),
            int(event.sol_amount),
            int(event.token_amount),
            int(event.real_sol_reserves),
            int(event.virtual_sol_reserves),
            int(event.virtual_token_reserves),
            event.timestamp,
        ]
        self.reserve(4096 + len(json.dumps(asdict(event)).encode()) * 8, coin)
        coin["trades"].append(trade)
        coin["trade_received_ms"].append((now - coin["seen_at"]) * 1000)
        coin["trade_real_token_reserves"].append(event.real_token_reserves)
        coin["trade_signatures"].append(signature)
        coin["trade_event_indices"].append(index)
        coin["trade_fields"].append(asdict(event))
        coin["last_trade_at"] = now
        self.trades_seen += 1
        if (
            coin["graduated_at"] is None
            and coin["fund_target"] is not None
            and event.real_sol_reserves >= coin["fund_target"]
        ):
            self._graduate(coin, slot, now, signal="fund_target")

    def _graduate(self, coin: dict, slot: int, now: float, *, signal: str) -> None:
        """Record one graduation with the LetsBonk detection signal."""
        self.graduations += 1
        coin["graduated_dslot"] = slot - coin["create_slot"]
        coin["graduated_ts"] = None
        coin["graduated_at"] = coin["graduated_received_monotonic"] = now
        coin["graduated_signal"] = signal
        coin["last_trade_at"] = now

    def flush_finished(
        self, *, force: bool = False, reason: str = "capture_end"
    ) -> None:
        """Flush finished coins and drop their pool_state registry entries."""
        before = set(self.coins)
        super().flush_finished(force=force, reason=reason)
        flushed = before - set(self.coins)
        for pool in [key for key, mint in self.pool_to_mint.items() if mint in flushed]:
            del self.pool_to_mint[pool]


def letsbonk_subscription() -> geyser_pb2.SubscribeRequest:
    """One LaunchLab transactions filter plus a processed-slot heartbeat."""
    req = geyser_pb2.SubscribeRequest()
    req.transactions["launchlab"].account_include.append(LAUNCHLAB_PROGRAM)
    # Do not set optional `failed`: both successful and reverted transactions.
    req.slots["head"].filter_by_commitment = True
    req.commitment = geyser_pb2.CommitmentLevel.PROCESSED
    return req


def _encode_initialize_v2(
    parser: IDLParser, *, fund_target: int, supply: int
) -> bytes:
    """Anchor-encode initialize_v2 args for the offline self-check."""

    def enc_str(value: str) -> bytes:
        raw = value.encode()
        return struct.pack("<I", len(raw)) + raw

    return (
        parser.get_instruction_discriminators()["initialize_v2"]
        + struct.pack("<B", 6)  # MintParams.decimals
        + enc_str("Check")
        + enc_str("CHK")
        + enc_str("https://example.test")
        + struct.pack("<B", 1)  # CurveParams::Fixed
        + struct.pack("<QQB", supply, fund_target, 0)  # supply, fund target, migrate
        + struct.pack("<QQQ", 0, 0, 0)  # VestingParams
        + struct.pack("<B", 0)  # AmmCreatorFeeOn::QuoteToken
    )


def _encode_trade_event(
    parser: IDLParser,
    *,
    pool: str,
    direction: int = 0,
    real_quote_after: int,
    amount_in: int = 500_000_000,
    amount_out: int = 15_000_000,
) -> str:
    """Anchor-encode one LaunchLab TradeEvent as a Program-data log line."""
    disc = parser.get_event_discriminators()["TradeEvent"]
    body = (
        base58.b58decode(pool)
        + struct.pack(
            "<" + "Q" * 13,
            1_000_000,  # total_base_sell
            900_000_000,  # virtual_base (token)
            30_000_000_000,  # virtual_quote (SOL)
            12_000_000,  # real_base_before
            20_000_000_000,  # real_quote_before
            30_000_000,  # real_base_after
            real_quote_after,
            amount_in,
            amount_out,
            1,  # protocol_fee
            2,  # platform_fee
            3,  # creator_fee
            4,  # share_fee
        )
        + struct.pack("<BB", direction, 2)  # TradeDirection, PoolStatus::Trade
        + struct.pack("<?", True)  # exact_in
    )
    return "Program data: " + base64.b64encode(disc + body).decode()


def _update(  # noqa: PLR0913 - synthetic frame builder
    signature: bytes,
    slot: int,
    keys: list[bytes],
    *,
    instructions: list[tuple[list[int], bytes]],
    logs: list[str],
    failed: bool = False,
) -> geyser_pb2.SubscribeUpdate:
    """Build one synthetic geyser transaction update for the self-check."""
    update = geyser_pb2.SubscribeUpdate()
    info = update.transaction.transaction
    info.signature = signature
    update.transaction.slot = slot
    program_index = len(keys)
    for key in (*keys, base58.b58decode(LAUNCHLAB_PROGRAM)):
        info.transaction.message.account_keys.append(key)
    for accounts, data in instructions:
        instruction = info.transaction.message.instructions.add()
        instruction.program_id_index = program_index
        instruction.accounts = bytes(accounts)
        instruction.data = data
    info.meta.log_messages.extend(logs)
    if failed:
        info.meta.err.err = b"failed"
    return update


def self_check() -> None:  # noqa: PLR0915 - one offline evidence check
    """No providers: creation/trade/graduation decode through real components."""
    import tempfile  # noqa: PLC0415

    from core.pubkeys import SystemAddresses  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as directory:
        rec = LetsBonkRecorder(Path(directory) / "check.jsonl", 60, 600)
        try:
            rec.admission_cutoff = time.monotonic() + 60
            token_programs = [
                bytes(SystemAddresses.TOKEN_PROGRAM),
                bytes(SystemAddresses.TOKEN_2022_PROGRAM),
            ]
            keys = [bytes([index]) * 32 for index in range(18)]
            keys[11], keys[12] = token_programs
            mint = base58.b58encode(bytes([6]) * 32).decode()
            pool = base58.b58encode(bytes([5]) * 32).decode()
            fund_target = 85_000_000_000
            supply = 1_000_000_000_000
            creation = _update(
                bytes([1]) * 64,
                100,
                keys,
                instructions=[(list(range(18)), _encode_initialize_v2(
                    rec.parser, fund_target=fund_target, supply=supply
                ))],
                logs=[f"Program {LAUNCHLAB_PROGRAM} invoke [1]",
                      f"Program {LAUNCHLAB_PROGRAM} success"],
            )
            rec.on_transaction(creation, time.monotonic(), time.time_ns())
            assert mint in rec.coins and pool in rec.pool_to_mint
            coin = rec.coins[mint]
            assert coin["fund_target"] == fund_target
            assert coin["supply"] == supply
            assert coin["creator"] == base58.b58encode(bytes([1]) * 32).decode()
            assert rec.counts["verified_creations"] == 1

            buyer = bytes([9]) * 32
            trade_keys = [buyer, keys[0]]
            logs = [
                f"Program {LAUNCHLAB_PROGRAM} invoke [1]",
                _encode_trade_event(rec.parser, pool=pool, real_quote_after=40_000_000_000),
                f"Program {LAUNCHLAB_PROGRAM} success",
            ]
            trade = _update(
                bytes([2]) * 64, 105, trade_keys, instructions=[], logs=logs
            )
            rec.on_transaction(trade, time.monotonic(), time.time_ns())
            assert len(coin["trades"]) == 1 and coin["graduated_at"] is None
            assert coin["trades"][0][0] == 5  # dslot
            assert coin["trades"][0][1] == base58.b58encode(buyer).decode()
            assert coin["trades"][0][3:8] == [
                500_000_000, 15_000_000, 40_000_000_000, 30_000_000_000,
                900_000_000,
            ]
            # Duplicate delivery: deduped, not double-counted.
            rec.on_transaction(trade, time.monotonic(), time.time_ns())
            assert len(coin["trades"]) == 1
            assert rec.counts["duplicate_events"] == 1

            # Fund-target crossing trade graduates the coin.
            crossing = _update(
                bytes([3]) * 64,
                112,
                trade_keys,
                instructions=[],
                logs=[
                    f"Program {LAUNCHLAB_PROGRAM} invoke [1]",
                    _encode_trade_event(
                        rec.parser, pool=pool, real_quote_after=fund_target
                    ),
                    f"Program {LAUNCHLAB_PROGRAM} success",
                ],
            )
            rec.on_transaction(crossing, time.monotonic(), time.time_ns())
            assert coin["graduated_at"] is not None
            assert coin["graduated_signal"] == "fund_target"
            assert coin["graduated_dslot"] == 12
            assert rec.graduations == 1

            # A second coin graduates via the migrate instruction instead.
            keys2 = [bytes([index + 32]) * 32 for index in range(18)]
            keys2[11], keys2[12] = token_programs
            mint2 = base58.b58encode(bytes([6 + 32]) * 32).decode()
            rec.on_transaction(
                _update(
                    bytes([4]) * 64, 200, keys2,
                    instructions=[(list(range(18)), _encode_initialize_v2(
                        rec.parser, fund_target=fund_target, supply=supply
                    ))],
                    logs=[f"Program {LAUNCHLAB_PROGRAM} success"],
                ),
                time.monotonic(),
                time.time_ns(),
            )
            assert mint2 in rec.coins
            migrate = _update(
                bytes([5]) * 64,
                240,
                keys2,
                instructions=[(
                    [0, 6, *([0] * 30)],
                    bytes.fromhex("cf52c091fecf91df") + b"x" * 8,
                )],
                logs=[f"Program {LAUNCHLAB_PROGRAM} success"],
            )
            rec.on_transaction(migrate, time.monotonic(), time.time_ns())
            coin2 = rec.coins[mint2]
            assert coin2["graduated_signal"] == "migrate_instruction"
            assert coin2["migrated_dslot"] == 40
            assert coin2["migration_name"] == "migrate_to_amm"
            assert rec.graduations == 2

            # Failed transactions are retained raw but excluded from the tape.
            failed_trade = _update(
                bytes([6]) * 64, 260, trade_keys, instructions=[], logs=logs,
                failed=True,
            )
            rec.on_transaction(failed_trade, time.monotonic(), time.time_ns())
            assert coin["n_failed_excluded"] == 1
            assert len(coin["trades"]) == 2
            rec.on_transaction(failed_trade, time.monotonic(), time.time_ns())
            assert rec.counts["duplicate_failed_transactions"] == 1

            # Reverted CPI frames are excluded by the shared decoder.
            reverted = _update(
                bytes([7]) * 64,
                270,
                trade_keys,
                instructions=[],
                logs=[
                    f"Program {LAUNCHLAB_PROGRAM} invoke [1]",
                    _encode_trade_event(rec.parser, pool=pool, real_quote_after=1),
                    f"Program {LAUNCHLAB_PROGRAM} invoke [2]",
                    _encode_trade_event(rec.parser, pool=pool, real_quote_after=1),
                    f"Program {LAUNCHLAB_PROGRAM} failed: custom error",
                    f"Program {LAUNCHLAB_PROGRAM} success",
                ],
            )
            rec.on_transaction(reverted, time.monotonic(), time.time_ns())
            assert len(coin["trades"]) == 3

            # Start-journal truth: pump-shaped defaults rewritten for LetsBonk.
            rec.journal(
                "start",
                source="geyser",
                filters={"programs": ["pump"]},
                parser="pump",
                idl_sha256={},
                recorder_sha256="pump",
            )
            entry = json.loads(rec.paths["run"].read_text().splitlines()[-1])
            assert entry["filters"] == {
                "programs": [LAUNCHLAB_PROGRAM], "failed": "included",
                "slots": "processed",
            }
            assert "raydium_launchlab_idl.json" in entry["idl_sha256"]
            assert entry["recorder_sha256"] == rec.recorder_sha256

            # Flushed rows carry LetsBonk fields and pump-compatible analytics.
            rec.flush_finished(force=True)
            rows = [
                json.loads(line)
                for line in rec.paths["lifecycles"].read_text().splitlines()
            ]
            first = next(row for row in rows if row["mint"] == mint)
            assert first["fund_target"] == fund_target
            assert first["supply"] == supply
            assert first["graduated_signal"] == "fund_target"
            assert first["migrated_dslot"] is None
            assert first["buy_sol"] == 1.5
            second = next(row for row in rows if row["mint"] == mint2)
            assert second["graduated_signal"] == "migrate_instruction"
            assert second["migrated_dslot"] == 40
            assert second["create_fields"]["pool_state"] == second["pool_state"]
            assert rec.pool_to_mint == {}
        finally:
            rec.close()
    print("self-check passed")


def main() -> int:
    """Run the LetsBonk lifecycle capture from parsed arguments."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env-file", type=Path)
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--idle", type=float, default=120)
    ap.add_argument("--max-age", type=float, default=1200)
    ap.add_argument("--postgrad-seconds", type=float, default=60)
    ap.add_argument("--postgrad-idle-seconds", type=float, default=30)
    ap.add_argument("--admission-seconds", type=float)
    ap.add_argument("--drain-seconds", type=float, default=600)
    ap.add_argument("--max-bytes", type=int, default=2 * 1024**3)
    ap.add_argument("--max-memory-bytes", type=int, default=256 * 1024**2)
    ap.add_argument("--max-coins", type=int, default=2000)
    ap.add_argument("--max-events", type=int, default=200_000)
    ap.add_argument("--max-reconnects", type=int, default=20)
    ap.add_argument("--stream-idle-seconds", type=float, default=30)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--run-id")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        self_check()
        return 0
    if args.minutes is None:
        ap.error("--minutes is required unless --self-check")
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
        f"letsbonk_lifecycles_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    rec = LetsBonkRecorder(
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
            subscription_request=letsbonk_subscription(),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
