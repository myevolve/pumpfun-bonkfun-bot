"""Record a Geyser-only census of closed native cycles on six venues; never sign.

Cloud (twelve 120-second windows, one every two hours, cycle rows only)::

    python learning-examples/token-lifecycles/record_venue_cycle_census.py \
      --credentials /explicit/provider.json --out /exclusive/census.jsonl \
      --seconds 120 --windows 12 --window-interval-seconds 7200 --retain cycles
    python learning-examples/token-lifecycles/record_venue_cycle_census.py --self-check
    python learning-examples/token-lifecycles/record_venue_cycle_census.py \
      --analyze /exclusive/census.jsonl

Each window is one TLS/x-token Subscribe filtered to transactions that reference
any of the six venue programs, confirmed commitment, no HTTP/RPC, no reconnect
within a window, no retries. Every delivered transaction is accounted with
``verify_solana_actor_receipts.account_receipt``; ``--retain all`` writes every
receipt compactly, ``--retain cycles`` writes only plausible closed native cycles
and keeps raw wire bytes for the first N per window. Per-window rows summarize
population, actor concentration, tips and failed-attempt fees. Ceilings censor a
window with a recorded reason rather than silently dropping evidence.

This measures what other actors landed, not what this wallet could execute:
no trigger clocks, no inclusion latency, no route reconstruction, no profit claim.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import base58
import grpc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_solana_actor_receipts as receipts

geyser_pb2 = receipts.geyser_pb2
require = receipts.require
StudyError = receipts.StudyError
NS = 1_000_000_000
HOUR_NS = 3600 * NS
# https://docs.jito.wtf/lowlatencytxnsend/ getTipAccounts (constant per docs).
JITO_TIP_ACCOUNTS = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)
BASE_FEE_PER_SIGNATURE = 5000
# Bounded study limits, protocol widths and explicit offline assertions.
# ruff: noqa: PLR2004, S101, C901
# Kamino: record_supply_returns.py PROGRAM; MarginFi: marginfi-v2 id-crate mainnet-beta;
# Velocity (ex-Drift): carry-economic-continuation inputs "program".
LENDING_PROGRAMS = {
    "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD": "Kamino Lending",
    "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA": "MarginFi v2",
    "vELoC1audYbSYVRXn1vPaV8Axoa9oU6BYmNGZZBDZ1P": "Velocity",
}
# Protocol sources and pinned SDK IDLs are linked in the research README.
LIQUIDATION_INSTRUCTIONS = {
    "Kamino Lending": (
        "liquidate_obligation_and_redeem_reserve_collateral",
        "liquidate_obligation_and_redeem_reserve_collateral_v2",
    ),
    "MarginFi v2": (
        "lending_account_liquidate",
        "start_liquidation",
        "end_liquidation",
    ),
    "Velocity": (
        "liquidate_perp",
        "liquidate_perp_with_fill",
        "liquidate_spot",
        "liquidate_spot_with_swap_begin",
        "liquidate_spot_with_swap_end",
        "liquidate_borrow_for_perp_pnl",
        "liquidate_perp_pnl_for_deposit",
    ),
}
# Anchor discriminator, restricted to the program implementing that instruction.
LIQUIDATION_DISCRIMINATORS = {
    program: {
        hashlib.sha256(f"global:{name}".encode()).digest()[:8]: name
        for name in LIQUIDATION_INSTRUCTIONS[protocol]
    }
    for program, protocol in LENDING_PROGRAMS.items()
}
PROFILES = {"venues": receipts.PROGRAMS, "lending": LENDING_PROGRAMS}


def liquidation(update: object) -> str | None:
    """Name the first recognized instruction present, not prove executed effects."""
    info = update.transaction.transaction
    msg, meta = info.transaction.message, info.meta
    keys = [receipts.key(value) for value in receipts.resolved_keys(info)]
    groups = {group.index: group.instructions for group in meta.inner_instructions}
    for top_index, ix in enumerate(msg.instructions):
        for candidate in (ix, *groups.get(top_index, ())):
            if candidate.program_id_index < len(keys) and (
                keys[candidate.program_id_index] in LENDING_PROGRAMS
            ):
                name = LIQUIDATION_DISCRIMINATORS[keys[candidate.program_id_index]].get(
                    bytes(candidate.data[:8])
                )
                if name is not None:
                    return name
    return None


def compact(update: object, analysis: dict) -> dict:
    """Project one receipt to census fields; unknown accounting stays null."""
    info = update.transaction.transaction
    meta = info.meta
    tips = [
        row
        for row in analysis["system_transfer_trace"]
        if row["direction"] == "out" and row["destination"] in JITO_TIP_ACCOUNTS
    ]
    signatures = info.transaction.message.header.num_required_signatures
    fee = int(analysis["fee_lamports"])
    return {
        "payer": analysis["payer"],
        "successful": analysis["successful"],
        "fee_lamports": analysis["fee_lamports"],
        "priority_fee_lamports": str(max(0, fee - BASE_FEE_PER_SIGNATURE * signatures)),
        "signatures": signatures,
        "compute_units": meta.compute_units_consumed
        if meta.HasField("compute_units_consumed")
        else None,
        "venues": analysis["executed_venues"],
        "route": analysis["route"],
        "closed_cycle": analysis["plausible_closed_native_cycle"],
        "native_delta_lamports": analysis["known_owned_native_delta_lamports"],
        "inventory_flat": analysis["inventory_flat"],
        "unknown_reasons": analysis["unknown_reasons"],
        "jito_tip_lamports": str(sum(int(row["amount_lamports"]) for row in tips)),
        "jito_tip_accounts": sorted({row["destination"] for row in tips}),
        "loaded_keys": analysis["loaded_keys"],
        "inventory_delta_raw": analysis["non_sol_inventory_delta_raw"],
        "liquidation": liquidation(update),
    }


class Hour:
    """Population and concentration for one receive-time hour."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.cycle_payers: Counter = Counter()
        self.cycle_gain = 0
        self.tips = 0
        self.payer_failed_fees: Counter = Counter()
        self.slots: set[int] = set()
        self.cycle_slots: Counter = Counter()
        self.liquidators: Counter = Counter()
        self.failed_liquidators: Counter = Counter()

    def add(self, slot: int, row: dict) -> None:
        self.counts["receipts"] += 1
        self.counts["successful" if row["successful"] else "failed"] += 1
        self.slots.add(slot)
        if not row["successful"]:
            self.payer_failed_fees[row["payer"]] += int(row["fee_lamports"])
        if int(row["jito_tip_lamports"]) > 0:
            self.counts["tipped"] += 1
            self.tips += int(row["jito_tip_lamports"])
        if row["closed_cycle"]:
            self.counts["closed_cycles"] += 1
            delta = int(row["native_delta_lamports"])
            self.cycle_gain += delta
            self.counts["positive_cycles" if delta > 0 else "nonpositive_cycles"] += 1
            self.cycle_payers[row["payer"]] += 1
            self.cycle_slots[slot] += 1
            if int(row["jito_tip_lamports"]) > 0:
                self.counts["tipped_cycles"] += 1
        if row.get("liquidation") is not None:
            if row["successful"]:
                self.counts["liquidations"] += 1
                self.liquidators[row["payer"]] += 1
                if int(row["jito_tip_lamports"]) > 0:
                    self.counts["tipped_liquidations"] += 1
            else:
                self.counts["failed_liquidations"] += 1
                self.failed_liquidators[row["payer"]] += 1

    def export(self) -> dict:
        top = self.cycle_payers.most_common(5)
        cycles = sum(self.cycle_payers.values())
        return {
            "counts": dict(self.counts),
            "distinct_slots": len(self.slots),
            "cycle_gain_lamports": str(self.cycle_gain),
            "jito_tip_lamports": str(self.tips),
            "cycle_payers": len(self.cycle_payers),
            "top_cycle_payers": [
                {"payer": payer, "cycles": count} for payer, count in top
            ],
            "top5_cycle_share": (sum(count for _, count in top) / cycles)
            if cycles
            else None,
            "contested_cycle_slots": sum(
                count > 1 for count in self.cycle_slots.values()
            ),
            "cycle_actor_failed_fee_lamports": str(
                sum(
                    fee
                    for payer, fee in self.payer_failed_fees.items()
                    if payer in self.cycle_payers
                )
            ),
            "liquidators": len(self.liquidators),
            "top_liquidators": [
                {"payer": payer, "liquidations": count}
                for payer, count in self.liquidators.most_common(5)
            ],
            "failed_liquidators": len(self.failed_liquidators),
            "liquidator_failed_fee_lamports": str(
                sum(
                    fee
                    for payer, fee in self.payer_failed_fees.items()
                    if payer in self.liquidators or payer in self.failed_liquidators
                )
            ),
        }


class Tape:
    def __init__(self, path: Path, ceiling: int) -> None:
        self.file = path.open("xb")
        self.ceiling, self.size, self.seq = ceiling, 0, 0

    def emit(self, event: str, *, terminal: bool = False, **fields: object) -> None:
        row = {"schema": 1, "seq": self.seq, "event": event, **fields}
        line = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode()
        require(
            self.size + len(line)
            <= self.ceiling - (0 if terminal else receipts.TAIL_RESERVE),
            "output_byte_ceiling",
        )
        self.file.write(line)
        self.file.flush()
        self.size += len(line)
        self.seq += 1
        if event != "receipt":
            print(line.decode().rstrip(), flush=True)


async def collect(args: argparse.Namespace, creds: dict, tape: Tape) -> bool:
    """Anchored sampling windows; each window is one independent stream."""
    origin = time.monotonic_ns()
    counts: Counter = Counter()
    total = Hour()
    seen: set[str] = set()
    failures = 0
    completed = 0
    tape.emit(
        "manifest",
        start_unix_ns=time.time_ns(),
        source_id=hashlib.sha256(creds["geyser_endpoint"].encode()).hexdigest(),
        collector_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        accounting_source_sha256=hashlib.sha256(
            Path(receipts.__file__).read_bytes()
        ).hexdigest(),
        transport="TLS/x-token Geyser Subscribe; confirmed; account_include profile",
        profile=args.profile,
        programs=PROFILES[args.profile],
        jito_tip_accounts=sorted(JITO_TIP_ACCOUNTS),
        limits={
            k: v
            for k, v in vars(args).items()
            if k not in {"credentials", "out", "analyze", "self_check"}
        },
        http_requests_allowed=0,
        signing_allowed=False,
    )
    for window in range(args.windows):
        due = origin + window * args.window_interval_seconds * NS
        await asyncio.sleep(max(0, (due - time.monotonic_ns()) / NS))
        tape.emit(
            "window_start",
            window_index=window,
            scheduled_elapsed_ns=due - origin,
            actual_elapsed_ns=time.monotonic_ns() - origin,
            start_unix_ns=time.time_ns(),
        )
        complete, reason, summary = await collect_window(
            args, creds, tape, window, counts, total, seen
        )
        tape.emit(
            "window_end",
            window_index=window,
            complete=complete,
            reason=reason,
            summary=summary,
            cumulative=total.export(),
            counters=dict(counts),
        )
        if complete:
            completed += 1
            failures = 0
        else:
            failures += 1
            if failures >= args.max_consecutive_failures:
                break
    tape.emit(
        "terminal",
        terminal=True,
        reason="windows_complete" if completed == args.windows else "window_failures",
        complete=completed == args.windows,
        windows_completed=completed,
        elapsed_ns=time.monotonic_ns() - origin,
        counters=dict(counts),
        cumulative=total.export(),
        distinct_receipts=len(seen),
        http_requests=0,
        signed_transactions=0,
        submitted_transactions=0,
        repeatable_profit_proven=False,
    )
    return completed == args.windows


async def collect_window(  # noqa: PLR0912, PLR0913, PLR0915
    args: argparse.Namespace,
    creds: dict,
    tape: Tape,
    window: int,
    counts: Counter,
    total: Hour,
    seen: set[str],
) -> tuple[bool, str, dict]:
    """One filtered stream for one window; ceilings censor, never reconnect."""
    start = time.monotonic_ns()
    hour_index = 0
    hour = Hour()
    window_total = Hour()
    last_receive = last_transaction = start
    raw_rows = 0

    async def request(call: object, req: object) -> None:
        require(counts["requests"] < args.max_requests, "request_ceiling")
        counts["requests"] += 1
        await asyncio.wait_for(call.write(req), args.idle_seconds)

    async def receive(call: object) -> tuple:
        raw = await call.read()
        return raw, time.monotonic_ns(), time.time_ns()

    def close_hour(now: int) -> None:
        nonlocal hour, hour_index
        tape.emit(
            "hour",
            window_index=window,
            hour_index=hour_index,
            elapsed_ns=now - start,
            summary=hour.export(),
            cumulative=total.export(),
            counters=dict(counts),
        )
        hour = Hour()
        hour_index += 1

    parsed = urlsplit(creds["geyser_endpoint"])
    host = parsed.hostname
    target = (
        f"[{host}]:{parsed.port or 443}"
        if ":" in host
        else f"{host}:{parsed.port or 443}"
    )
    reason, complete = "not_started", False
    pending = None
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
                        "profile": geyser_pb2.SubscribeRequestFilterTransactions(
                            vote=False, account_include=list(PROFILES[args.profile])
                        )
                    },
                    commitment=geyser_pb2.CONFIRMED,
                ),
            )
            await asyncio.wait_for(call.initial_metadata(), args.idle_seconds)
            start = time.monotonic_ns()
            last_receive = last_transaction = start
            tape.emit(
                "stream_ready",
                window_index=window,
                start_mono_ns=start,
                start_unix_ns=time.time_ns(),
            )
            pending = asyncio.create_task(receive(call))
            while True:
                now = pending.result()[1] if pending.done() else time.monotonic_ns()
                while now - start >= (hour_index + 1) * HOUR_NS:
                    close_hour(start + (hour_index + 1) * HOUR_NS)
                if now - start >= args.seconds * NS:
                    reason, complete = "duration_complete", True
                    break
                if now - last_receive >= args.idle_seconds * NS:
                    raise StudyError("stream_idle_gap")  # noqa: TRY301
                if now - last_transaction >= args.transaction_idle_seconds * NS:
                    raise StudyError("transaction_idle_gap")  # noqa: TRY301
                boundary = min(
                    start + args.seconds * NS,
                    start + (hour_index + 1) * HOUR_NS,
                    last_receive + args.idle_seconds * NS,
                    last_transaction + args.transaction_idle_seconds * NS,
                )
                done, _ = await asyncio.wait(
                    {pending}, timeout=max(0, (boundary - now) / NS)
                )
                if not done:
                    continue
                raw, received, wall = pending.result()
                pending = None
                if raw is grpc.aio.EOF:
                    raise StudyError("stream_eof")  # noqa: TRY301
                if received - start >= args.seconds * NS:
                    reason, complete = "duration_complete", True
                    break
                while received - start >= (hour_index + 1) * HOUR_NS:
                    close_hour(start + (hour_index + 1) * HOUR_NS)
                last_receive = received
                counts["updates"] += 1
                counts["wire_bytes"] += len(raw)
                require(
                    counts["wire_bytes"] <= args.max_wire_bytes, "wire_byte_ceiling"
                )
                require(len(raw) <= args.max_message_bytes, "message_byte_ceiling")
                update = receipts.decode(raw)
                kind = update.WhichOneof("update_oneof")
                counts[f"update_{kind}"] += 1
                if kind == "ping":
                    await request(
                        call,
                        geyser_pb2.SubscribeRequest(
                            ping=geyser_pb2.SubscribeRequestPing(id=counts["requests"])
                        ),
                    )
                elif kind == "transaction":
                    last_transaction = received
                    info = update.transaction.transaction
                    require(not info.is_vote, "provider_vote_filter_violation")
                    require(len(info.signature) == 64, "invalid_signature")
                    signature = base58.b58encode(info.signature).decode("ascii")
                    if signature in seen:
                        counts["duplicate_receipts"] += 1
                    else:
                        seen.add(signature)
                        require(len(seen) <= args.max_receipts, "receipt_ceiling")
                        analysis = receipts.account_receipt(update)
                        row = compact(update, analysis)
                        slot = update.transaction.slot
                        hour.add(slot, row)
                        window_total.add(slot, row)
                        total.add(slot, row)
                        keep = row["closed_cycle"] or row["liquidation"] is not None
                        if args.retain == "all" or keep:
                            keep_raw = keep and (
                                args.retain == "all"
                                or raw_rows < args.raw_cycle_rows_per_window
                            )
                            raw_rows += keep_raw
                            tape.emit(
                                "receipt",
                                window_index=window,
                                receive_unix_ns=wall,
                                elapsed_ns=received - start,
                                slot=slot,
                                signature=signature,
                                **row,
                                raw_b64=base64.b64encode(raw).decode("ascii")
                                if keep_raw
                                else None,
                                raw_sha256=hashlib.sha256(raw).hexdigest(),
                            )
                pending = asyncio.create_task(receive(call))
    except StudyError as exc:
        reason = str(exc)
    except grpc.aio.AioRpcError as exc:
        reason = f"grpc_{exc.code().name.lower()}"
    except TimeoutError:
        reason = "transport_timeout"
    except Exception as exc:  # noqa: BLE001 - local code only, never provider text
        reason = type(exc).__name__
    finally:
        if pending is not None:
            pending.cancel()
        close_hour(time.monotonic_ns())
    return complete, reason, window_total.export()


def quantiles(values: list[int]) -> dict | None:
    values = sorted(values)
    n = len(values)
    if not n:
        return None
    return {
        "n": n,
        "p10": values[n // 10],
        "p50": values[n // 2],
        "p90": values[int(n * 0.9)],
        "max": values[-1],
        "sum": sum(values),
    }


def market_economics(
    cycles: list[dict], actors: list[dict], windows: int, *, failed_fees_complete: bool
) -> dict:
    """Sampled cycle economics; incomplete failed-fee coverage cannot rank winners."""
    gains = [int(row["native_delta_lamports"]) for row in cycles]
    positive = [g for g in gains if g > 0]
    tipped = [row for row in cycles if int(row["jito_tip_lamports"]) > 0]
    untipped = [row for row in cycles if int(row["jito_tip_lamports"]) == 0]
    per_slot: Counter = Counter(row["slot"] for row in cycles)
    route_gain: Counter = Counter()
    for row in cycles:
        route_gain[row["route"]] += int(row["native_delta_lamports"])
    nets = sorted(
        (
            int(a["cycle_gain_minus_failed_fees_lamports"])
            for a in actors
            if a["cycle_gain_minus_failed_fees_lamports"] is not None
        ),
        reverse=True,
    )
    winners = [n for n in nets if n > 0]
    return {
        "sampled_windows": windows,
        "failed_fee_coverage": "retained_receipts"
        if failed_fees_complete
        else "unknown",
        "cycles": len(cycles),
        "cycle_gain_lamports": quantiles(gains),
        "positive_cycle_gain_lamports": quantiles(positive),
        "top_cycle_share_of_positive_gain": (
            sum(sorted(positive, reverse=True)[: max(1, len(positive) // 100)])
            / sum(positive)
            if positive
            else None
        ),
        "tipped_cycles": {
            "n": len(tipped),
            "gain_lamports": str(sum(int(r["native_delta_lamports"]) for r in tipped)),
            "tip_lamports": str(sum(int(r["jito_tip_lamports"]) for r in tipped)),
        },
        "untipped_cycles": {
            "n": len(untipped),
            "gain_lamports": str(
                sum(int(r["native_delta_lamports"]) for r in untipped)
            ),
            "priority_fee_lamports": quantiles(
                [int(r["priority_fee_lamports"]) for r in untipped]
            ),
        },
        "slots_with_cycles": len(per_slot),
        "slots_with_multiple_cycles": sum(c > 1 for c in per_slot.values()),
        "max_cycles_in_one_slot": max(per_slot.values(), default=0),
        "route_gain_lamports": {
            route: str(gain) for route, gain in route_gain.most_common(10)
        },
        "actors_net_positive_after_failed_fees": len(winners)
        if failed_fees_complete
        else None,
        "actors_net_nonpositive": len(nets) - len(winners)
        if failed_fees_complete
        else None,
        "winners_net_lamports_per_window": (
            sum(winners) / windows
            if failed_fees_complete and nets and windows
            else None
        ),
        "best_actor_net_lamports_per_window": (
            nets[0] / windows if failed_fees_complete and nets and windows else None
        ),
        "losers_net_lamports_per_window": (
            sum(n for n in nets if n <= 0) / windows
            if failed_fees_complete and nets and windows
            else None
        ),
        "note": "Sampled program-filtered windows only, not daily income or full actor PnL. Missing failed receipts leave actor nets unknown.",
    }


def analyze(path: Path) -> dict:  # noqa: PLR0912, PLR0915 - one streaming replay
    """Recompute windows and totals from receipt rows; verify cycle raw bytes."""
    hours: dict[tuple[int, int], Hour] = {}
    windows: dict[int, Hour] = {}
    total = Hour()
    emitted = {}
    emitted_windows = {}
    terminal = None
    retain = None
    stream_start = None
    expected_seq = 0
    payers: Counter = Counter()
    payer_gain: Counter = Counter()
    payer_failed: Counter = Counter()
    payer_tips: Counter = Counter()
    cycle_rows: list[dict] = []
    with path.open("rb") as file:
        for line in file:
            row = json.loads(line)
            require(row["seq"] == expected_seq, "sequence_gap")
            expected_seq += 1
            event = row["event"]
            if event == "manifest":
                retain = row["limits"].get("retain", "all")
            elif event == "stream_ready":
                stream_start = row["start_mono_ns"]
            elif event == "receipt":
                require(retain in {"all", "cycles"}, "missing_retention_manifest")
                if row["raw_b64"] is not None:
                    raw = base64.b64decode(row["raw_b64"], validate=True)
                    require(
                        hashlib.sha256(raw).hexdigest() == row["raw_sha256"], "raw_hash"
                    )
                    update = receipts.decode(raw)
                    check = compact(update, receipts.account_receipt(update))
                    require(
                        all(
                            row.get(k) == v
                            if k in row
                            else k in {"inventory_delta_raw", "liquidation"}
                            for k, v in check.items()
                        ),
                        "cycle_replay_mismatch",
                    )
                index = row.get("window_index", 0)
                key = (index, row["elapsed_ns"] // HOUR_NS)
                hours.setdefault(key, Hour()).add(row["slot"], row)
                windows.setdefault(index, Hour()).add(row["slot"], row)
                total.add(row["slot"], row)
                payers[row["payer"]] += 1
                if not row["successful"]:
                    payer_failed[row["payer"]] += int(row["fee_lamports"])
                if row["closed_cycle"]:
                    payer_gain[row["payer"]] += int(row["native_delta_lamports"])
                payer_tips[row["payer"]] += int(row["jito_tip_lamports"])
            elif event == "hour":
                emitted[(row.get("window_index", 0), row["hour_index"])] = row[
                    "summary"
                ]
            elif event == "window_end":
                emitted_windows[row["window_index"]] = row
            elif event == "terminal":
                terminal = row
            if event == "receipt" and row["closed_cycle"]:
                cycle_rows.append(row)
    for key, summary in emitted.items():
        if retain == "all" and key in hours:
            require(
                all(hours[key].export().get(k) == v for k, v in summary.items()),
                "hour_replay_mismatch",
            )
    for index, row in emitted_windows.items():
        recomputed = windows[index].export() if index in windows else Hour().export()
        require(
            recomputed["counts"].get("closed_cycles", 0)
            == row["summary"]["counts"].get("closed_cycles", 0)
            and recomputed["cycle_gain_lamports"]
            == row["summary"]["cycle_gain_lamports"],
            "window_cycle_replay_mismatch",
        )
    actors = [
        {
            "payer": payer,
            "receipts": payers[payer],
            "closed_cycles": total.cycle_payers[payer],
            "cycle_gain_lamports": str(payer_gain[payer]),
            "failed_fee_lamports": str(payer_failed[payer])
            if retain == "all"
            else None,
            "jito_tip_lamports": str(payer_tips[payer]),
            "cycle_gain_minus_failed_fees_lamports": str(
                payer_gain[payer] - payer_failed[payer]
            )
            if retain == "all"
            else None,
        }
        for payer in total.cycle_payers
    ]
    actors.sort(key=lambda a: (-a["closed_cycles"], a["payer"]))
    economics = market_economics(
        cycle_rows, actors, len(emitted_windows), failed_fees_complete=retain == "all"
    )
    retained_total = total.export()
    if retain != "all":
        retained_total["cycle_actor_failed_fee_lamports"] = None
        retained_total["liquidator_failed_fee_lamports"] = None
    per_window = [
        {
            "window_index": index,
            "complete": row["complete"],
            "reason": row["reason"],
            "summary": row["summary"],
        }
        for index, row in sorted(emitted_windows.items())
    ]
    return {
        "retain": retain,
        "windows": per_window,
        "terminal": None
        if terminal is None
        else {k: terminal[k] for k in ("reason", "complete", "elapsed_ns", "counters")},
        "stream_started": stream_start is not None,
        "hours": len(hours),
        "hours_verified": len(emitted) if retain == "all" else 0,
        "cycle_windows_verified": len(emitted_windows),
        "total_scope": "retained_receipts",
        "total": retained_total,
        "recorded_cumulative_summary": (
            emitted_windows[max(emitted_windows)].get("cumulative")
            if emitted_windows
            else None
        ),
        "cycle_actors": actors,
        "market_economics": economics,
        "limits": [
            "Confirmed venue-filtered receipts; not full market coverage or execution latency.",
            "Closed cycles are plausible target-involved cashflow of other payers, not our fills.",
            "Failed-attempt fees are attributed only when the payer is the fee payer; bundle co-signers and sponsors are invisible.",
            "Recorded cumulative summaries include omitted receipts but are not fully replay-verified with cycle-only retention.",
            "Legacy tip totals may include reverted transfers; tips do not establish bundle membership.",
        ],
    }


def self_check() -> None:
    """Tip attribution and hour aggregation on one synthetic successful receipt."""
    update = geyser_pb2.SubscribeUpdate()
    update.transaction.slot = 5
    info = update.transaction.transaction
    info.signature = bytes(range(64))
    payer = bytes([1] * 32)
    tip = base58.b58decode("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5")
    system = bytes(32)
    msg = info.transaction.message
    msg.header.num_required_signatures = 1
    msg.account_keys.extend([payer, tip, system])
    ix = msg.instructions.add(program_id_index=2, accounts=bytes([0, 1]))
    ix.data = b"\x02\x00\x00\x00" + (7000).to_bytes(8, "little")
    info.meta.fee = 15000
    info.meta.pre_balances.extend([1_000_000, 0, 1])
    info.meta.post_balances.extend([978_000, 7000, 1])
    analysis = receipts.account_receipt(update)
    row = compact(update, analysis)
    assert (
        row["jito_tip_lamports"] == "7000" and row["priority_fee_lamports"] == "10000"
    )
    assert row["native_delta_lamports"] == "-22000" and not row["closed_cycle"]
    hour = Hour()
    hour.add(5, row)
    exported = hour.export()
    assert exported["counts"] == {"receipts": 1, "successful": 1, "tipped": 1}
    assert exported["jito_tip_lamports"] == "7000" and exported["cycle_payers"] == 0
    assert row["liquidation"] is None
    # A Kamino liquidation reached only through an inner CPI is still named; a
    # matching discriminator on a non-lending program is not.
    kamino = base58.b58decode("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD")
    msg.account_keys.append(kamino)
    group = info.meta.inner_instructions.add(index=0)
    inner = group.instructions.add(program_id_index=3, accounts=bytes([0]))
    # Explicit bytes from the official Kamino SDK, independent of our hash lookup.
    inner.data = bytes([177, 71, 154, 188, 226, 133, 74, 55]) + b"\x01"
    info.meta.pre_balances.append(1)
    info.meta.post_balances.append(1)
    assert liquidation(update) == "liquidate_obligation_and_redeem_reserve_collateral"
    inner.program_id_index = 2
    assert liquidation(update) is None
    inner.program_id_index = 3
    info.meta.err.SetInParent()
    failed = compact(update, receipts.account_receipt(update))
    hour = Hour()
    hour.add(6, failed)
    assert hour.export()["counts"]["failed_liquidations"] == 1
    assert hour.export()["liquidator_failed_fee_lamports"] == "15000"
    # Official MarginFi receivership bytes were previously missed by a wrong name.
    inner.data = bytes([244, 93, 90, 214, 192, 166, 191, 21])
    assert liquidation(update) is None  # Not a Kamino instruction.
    for program, wire, expected in (
        (
            "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA",
            [244, 93, 90, 214, 192, 166, 191, 21],
            "start_liquidation",
        ),
        (
            "MFv2hWf31Z9kbCa1snEPYctwafyhdvnV7FZnsebVacA",
            [110, 11, 244, 54, 229, 181, 22, 184],
            "end_liquidation",
        ),
        (
            "vELoC1audYbSYVRXn1vPaV8Axoa9oU6BYmNGZZBDZ1P",
            [142, 88, 163, 160, 223, 75, 55, 225],
            "liquidate_spot_with_swap_end",
        ),
    ):
        msg.account_keys[3] = base58.b58decode(program)
        inner.data = bytes(wire)
        assert liquidation(update) == expected
    print(
        "PASS: tip attribution, priority fee decomposition, hourly aggregation, "
        "liquidation detection"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--analyze", type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--windows", type=int, default=1)
    parser.add_argument("--window-interval-seconds", type=int, default=0)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="venues")
    parser.add_argument("--retain", choices=("all", "cycles"), default="cycles")
    parser.add_argument("--raw-cycle-rows-per-window", type=int, default=50)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--max-wire-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--max-output-bytes", type=int, default=256 * 1024**2)
    parser.add_argument("--max-receipts", type=int, default=2_000_000)
    parser.add_argument("--max-message-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-requests", type=int, default=20_000)
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--transaction-idle-seconds", type=int, default=300)
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
        require(
            0 < args.seconds <= 86400
            and 1 <= args.windows <= 48
            and (args.windows == 1 or args.window_interval_seconds >= args.seconds)
            and (args.windows - 1) * args.window_interval_seconds + args.seconds
            <= 86400
            and 1 <= args.max_consecutive_failures <= args.windows
            and 0 <= args.raw_cycle_rows_per_window <= 10_000,
            "duration_bounds",
        )
        require(
            0 < args.max_message_bytes <= 16_000_000
            and receipts.TAIL_RESERVE * 2 < args.max_output_bytes <= 1_073_741_824
            and 1 <= args.idle_seconds <= 300
            and 1 <= args.transaction_idle_seconds <= 3600,
            "resource_bounds",
        )
        creds = receipts.load_credentials(args.credentials)
        tape = Tape(args.out, args.max_output_bytes)
        if not asyncio.run(collect(args, creds, tape)):
            raise SystemExit(2)  # noqa: TRY301
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
