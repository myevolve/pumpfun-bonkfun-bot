"""Record one preregistered carry counterfactual; never sign or submit.

The frozen child probe executes unsigned simulations against public native state.
This recorder preserves its raw evidence before interpreting any result. Funding
claims, hypothetical settlement, and wallet income remain distinct. Run
``--self-check`` offline; observation requires explicit frozen paths and hashes.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from decimal import Decimal
from typing import Any

# Standalone integer accounting and explicit offline invariants, as in the supply recorder.
# ruff: noqa: PLR2004, S101, TRY003


def require(condition: bool, message: str) -> None:  # noqa: FBT001 -- assertion predicate, not an operation mode
    """Fail closed on an invalid observation without losing the terminal record."""
    if not condition:
        raise ValueError(message)


def trunc_div(numerator: int, denominator: int) -> int:
    """Match the SDK's signed integer division, including sub-unit debits."""
    return (abs(numerator) // denominator) * (-1 if numerator < 0 else 1)


def funding_raw(index_delta: int) -> int:
    """Value one fixed -0.56 SOL interval, not overlapping hourly coupons."""
    return -trunc_div(trunc_div(index_delta * -560_000_000, 10**9), 10**3)


def settled_quote(scaled: int, interest: int, pnl: int) -> int | None:
    """Floor an internal PnL transfer; withdrawal-only rounding does not apply."""
    change = abs(pnl) * 10**13 // interest
    if pnl < 0:
        scaled -= change
    else:
        scaled += change
    return None if scaled < 0 else scaled * interest // 10**13


def leg(frame: dict[str, Any], direction: str) -> tuple[int, int]:
    """Use native full-fill proceeds and fees, not an oracle entry price."""
    fills = [f for f in frame["fills"] if f["taker_order_direction"] == direction]
    if sum(f["base_asset_amount_filled"] for f in fills) != 560_000_000:
        raise ValueError("native leg is not the fixed full quantity")
    return (
        sum(f["quote_asset_amount_filled"] for f in fills),
        sum(f["taker_fee"] for f in fills),
    )


def freeze_entry(frame: dict[str, Any]) -> dict[str, Any]:
    """Freeze a fresh native entry quote and explicitly calculated paper balances."""
    if not frame["funding_index_unchanged_from_observed_bank"]:
        raise ValueError("no unchanged observed funding checkpoint")
    deposits = {d["market_index"]: d for d in frame["deposits"]}
    scaled = {}
    for index, amount, precision in [(0, 500_000, 10**13), (1, 500_000_000, 10**10)]:
        deposit = deposits[index]
        interest = deposit["market_cumulative_deposit_interest"]
        if deposit["amount"] != amount or interest <= 0:
            raise ValueError("deposit identity/interest differs")
        scaled[index] = amount * precision // interest
        if scaled[index] * interest // precision != deposit["user_token_amount_after"]:
            raise ValueError("native deposit does not match pinned integer math")
    prices = frame["unsnapped_prices_micro_usd"]
    entry_timestamp = max(
        f["ts"] for f in frame["fills"] if f["taker_order_direction"] == "Short"
    )
    require(
        frame["chain_timestamp"] <= entry_timestamp <= frame["chain_timestamp"] + 180,
        "entry fill timestamp differs from its native bank",
    )
    entry = {
        "native": frame,
        "entry_chain_timestamp": entry_timestamp,
        "scaled_quote": scaled[0],
        "scaled_sol": scaled[1],
        "initial_sol_redemption": scaled[1]
        * frame["spot1"]["cumulative_deposit_interest"]
        // 10**10,
        "initial_nav_usd": str(
            Decimal(frame["payer_lamports"])
            * prices[frame["spot1"]["oracle"]]
            / Decimal(10**15)
        ),
    }
    if (
        value_position(entry, frame)["hypothetical_withdrawn_usdt_raw"]
        != frame["returned_usdt_raw"]
    ):
        raise ValueError("native cold withdrawal does not match settlement rounding")
    return entry


def value_position(entry: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any]:
    """Mark the same paper exposure; unknown payment never becomes realized cash."""
    initial = entry["native"]
    if not frame["funding_index_unchanged_from_observed_bank"]:
        raise ValueError("simulation advanced an unobserved funding checkpoint")
    for index, decimals in [(0, 6), (1, 9)]:
        market = frame[f"spot{index}"]
        original = initial[f"spot{index}"]
        if market["mint"] != original["mint"] or market["decimals"] != decimals:
            raise ValueError("spot denomination changed")
        if market["cumulative_deposit_interest"] <= 0:
            raise ValueError("invalid native deposit interest")
    short_quote, short_fee = leg(initial, "Short")
    close_quote, close_fee = leg(frame, "Long")
    funding = funding_raw(frame["funding_index_short"] - initial["funding_index_short"])
    pnl = short_quote - close_quote - short_fee - close_fee + funding
    quote_interest = frame["spot0"]["cumulative_deposit_interest"]
    sol_interest = frame["spot1"]["cumulative_deposit_interest"]
    quote_tokens = settled_quote(entry["scaled_quote"], quote_interest, pnl)
    sol_redemption = entry["scaled_sol"] * sol_interest // 10**10
    # The native cold-cycle SOL delta includes the actual swap, fee, rents and dust.
    # Add only the separate exit/failure reserves and later fixed-balance accrual.
    sol_cash = initial["payer_lamports"] + initial["payer_delta_lamports"] - 130_000
    sol_cash += sol_redemption - entry["initial_sol_redemption"]
    prices = frame["unsnapped_prices_micro_usd"]
    sol_usd = prices[frame["spot1"]["oracle"]]
    quote_usd = prices[frame["spot0"]["oracle"]]
    if sol_usd <= 0 or quote_usd <= 0:
        raise ValueError("nonpositive native USD price")
    quote_claim = entry["scaled_quote"] * quote_interest // 10**13 + pnl
    indicative_nav = Decimal(sol_cash) * sol_usd / Decimal(10**15)
    indicative_nav += Decimal(quote_claim) * quote_usd / Decimal(10**12)
    nav = None
    pnl_usd = None
    if quote_tokens is not None and sol_cash >= 0:
        nav = Decimal(sol_cash) * sol_usd / Decimal(10**15)
        nav += Decimal(quote_tokens) * quote_usd / Decimal(10**12)
        pnl_usd = nav - Decimal(entry["initial_nav_usd"])
    return {
        "elapsed_chain_seconds": max(
            f["ts"] for f in frame["fills"] if f["taker_order_direction"] == "Long"
        )
        - entry["entry_chain_timestamp"],
        "funding_claim_usdt_raw": funding,
        "hypothetical_settlement_pnl_usdt_raw": pnl,
        "hypothetical_withdrawn_usdt_raw": quote_tokens,
        "hypothetical_liquid_sol_lamports": sol_cash,
        "sol_balance_change_from_transferred_raw": sol_redemption - 500_000_000,
        "quote_balance_change_before_settlement_raw": entry["scaled_quote"]
        * quote_interest
        // 10**13
        - 500_000,
        "residual_cash_delta_sol_lamports": sol_cash - 560_000_000,
        "initial_nav_usd": entry["initial_nav_usd"],
        "indicative_nav_including_unsettled_claim_usd": str(indicative_nav),
        "indicative_pnl_including_unsettled_claim_usd": str(
            indicative_nav - Decimal(entry["initial_nav_usd"])
        ),
        "quote_cash_shortfall_before_borrow_raw": max(0, -quote_claim),
        "collateral_sol_is_not_automatically_converted_to_usdt": True,
        "hypothetical_terminal_cash_nav_usd": None if nav is None else str(nav),
        "hypothetical_cash_pnl_usd": None if pnl_usd is None else str(pnl_usd),
        "hypothetical_cash_pnl_usdt_at_exit_fx": None
        if pnl_usd is None
        else str(pnl_usd * 1_000_000 / quote_usd),
        "quote_buffer_still_positive": quote_tokens is not None
        and quote_tokens >= 250_000,
        "held_position_margin_proven": False,
        "held_position_settlement_proven": False,
        "held_position_withdrawal_proven": False,
        "funding_income_proven": False,
        "all_in_profit_proven": False,
    }


def self_check() -> None:
    """Defend funding signs, native rounding and missing cash rather than defaults."""
    assert funding_raw(10**9) == 560_000
    assert funding_raw(-(10**9)) == -560_000
    assert funding_raw(1) == funding_raw(-1) == 0
    interest = 10_000_006_084
    scaled = 500_000 * 10**13 // interest
    assert settled_quote(scaled, interest, -293_206) == 206_793
    assert settled_quote(scaled, interest, -600_000) is None
    assert settled_quote(scaled, interest, 560_000) == 1_059_999
    # The first prospective native cycle distinguishes internal transfer rounding
    # from the extra scaled unit charged only when funds leave the protocol.
    interest = 10_000_006_098
    assert settled_quote(500_000 * 10**13 // interest, interest, -138_575) == 361_425
    print("PASS: signed funding, native internal-settlement rounding, unavailable cash")


def interpret(
    stdout: str, expected_source: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Verify raw framing and exact wallet equality before using a native frame."""
    records = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    source = next(r["data"] for r in records if r["kind"] == "EXECUTED_SOURCE")
    require(source["sha256"] == expected_source, "executed probe fingerprint differs")
    for row in records:
        if row["kind"] in {"EXECUTED_SOURCE", "RAW_REQUEST", "RAW_RESPONSE"}:
            data = row["data"]
            require(
                hashlib.sha256(data["raw"].encode()).hexdigest() == data["sha256"],
                "raw evidence fingerprint differs",
            )
        if row["kind"] == "RAW_REQUEST":
            request = json.loads(row["data"]["raw"])
            require(
                request["method"] in {"getMultipleAccounts", "simulateTransaction"},
                "unexpected native method",
            )
            if request["method"] == "simulateTransaction":
                options = request["params"][1]
                require(
                    options["sigVerify"] is False
                    and options["replaceRecentBlockhash"] is True,
                    "simulation signature/blockhash policy differs",
                )
    finish = next(r["data"] for r in records if r["kind"] == "FINISH")
    require(
        not (finish["signed"] or finish["submitted"] or finish["runtime_writes"]),
        "read-only policy differs",
    )
    require(
        finish["rpc_requests"] <= 5 and finish["simulations"] <= 1,
        "scheduled native budget exceeded",
    )
    before = next((r["data"] for r in records if r["kind"] == "ACTUAL_BEFORE"), None)
    if before is not None:
        after = next((r["data"] for r in records if r["kind"] == "ACTUAL_AFTER"), None)
        require(
            after is not None and before["accounts"] == after["accounts"],
            "actual wallet/PDAs changed or final verification missing",
        )
    frame = next(
        (r["data"] for r in records if r["kind"] == "FORWARD_NATIVE_INPUTS"), None
    )
    require(
        frame is None or finish["exit_code"] == 0,
        "native frame has an unsuccessful process outcome",
    )
    return records, frame


def recover_startup(
    raw: bytes, *, definition_sha256: str, probe_sha256: str, input_sha256: str
) -> dict[str, Any]:
    """Recover the same fully observed first candidate, never pick a replacement."""
    require(raw.endswith(b"\n"), "incomplete journal tail cannot be recovered")
    rows = [json.loads(line) for line in raw.splitlines()]
    # ponytail: startup-only recovery; a held-position interruption needs journal replay.
    require(
        [row["kind"] for row in rows]
        == [
            "frozen_definition",
            "observation_attempt",
            "native_observation",
            "observation_incomplete",
        ],
        "recovery requires one failed startup, not an existing or reset entry",
    )
    header, attempt, native, terminal = rows
    require(
        header["definition_sha256"] == definition_sha256
        and header["probe_source_sha256"] == probe_sha256
        and header["public_input_sha256"] == input_sha256,
        "recovery fingerprints differ",
    )
    require(
        attempt["number"] == native["number"] == 0 and native["returncode"] == 0,
        "first native observation was not complete",
    )
    require(
        terminal["reason"]
        == "ValueError: native cold withdrawal does not match settlement rounding"
        and terminal["counts_complete"]
        and not terminal["entry_frozen"],
        "not the known recoverable accounting failure",
    )
    require(
        hashlib.sha256(native["stdout"].encode()).hexdigest()
        == native["stdout_sha256"],
        "retained native output fingerprint differs",
    )
    records, frame = interpret(native["stdout"], probe_sha256)
    if frame is None:
        raise ValueError("retained native frame is unavailable")
    require(
        frame["cost_gate_pass"]
        and frame["buffer_gate_pass"]
        and frame["funding_index_unchanged_from_observed_bank"],
        "the original candidate did not qualify",
    )
    return {
        "entry": freeze_entry(frame),
        "records": records,
        "origin_wall": attempt["scheduled_unix_time"],
        "journal_prefix_sha256": hashlib.sha256(raw).hexdigest(),
        "previous_recorder_sha256": header["recorder_source_sha256"],
        "native_stdout_sha256": native["stdout_sha256"],
    }


def main() -> None:  # noqa: C901, PLR0912, PLR0915 -- one ordered fail-closed durable loop
    """Observe once per scheduled hour; exclusive journal creation forbids resets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--definition", type=pathlib.Path)
    parser.add_argument("--definition-sha256")
    parser.add_argument("--probe-sha256")
    parser.add_argument("--input-sha256")
    parser.add_argument(
        "--recover-startup",
        action="store_true",
        help="Recover the retained first native observation after the audited accounting fix; no new entry selection.",
    )
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if not all(
        (args.definition, args.definition_sha256, args.probe_sha256, args.input_sha256)
    ):
        parser.error("explicit definition and three frozen fingerprints are required")
    root = pathlib.Path.cwd().resolve()
    paper = root / ".state/paper-trading"
    definition_path = args.definition.resolve()
    journal: pathlib.Path | None = None
    probe: pathlib.Path | None = None
    allowed = {definition_path}

    def guard(event: str, values: tuple[Any, ...]) -> None:
        if event == "open":
            name = values[0]
            if isinstance(name, str | bytes | os.PathLike):
                path = pathlib.Path(os.fsdecode(name)).resolve()
                if any(
                    part.startswith(".env") or part == "ENVDATA" for part in path.parts
                ) or ".state/wallets" in str(path):
                    raise RuntimeError("credential/operational file access prohibited")
                if path.is_relative_to(root / ".state") and path not in allowed:
                    raise RuntimeError("unapproved state path")
                writable = isinstance(values[1], str) and any(
                    c in values[1] for c in "wax+"
                )
                writable |= isinstance(values[2], int) and bool(
                    values[2]
                    & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                )
                if writable and path != journal:
                    raise RuntimeError("only the new study journal is writable")
        if event == "subprocess.Popen" and values[1] != [
            sys.executable,
            "-I",
            "-B",
            str(probe),
        ]:
            raise RuntimeError("only the frozen read-only child is executable")
        if event in {
            "socket.connect",
            "socket.getaddrinfo",
            "urllib.Request",
            "os.system",
            "os.remove",
            "os.rename",
            "os.mkdir",
            "os.rmdir",
        }:
            raise RuntimeError("recorder cannot trade, network or mutate other paths")

    sys.addaudithook(guard)
    if definition_path.parent != paper:
        raise ValueError("definition must be in the research directory")
    definition_raw = definition_path.read_bytes()
    if hashlib.sha256(definition_raw).hexdigest() != args.definition_sha256:
        raise ValueError("definition fingerprint differs")
    definition = json.loads(definition_raw)
    if (
        definition["kind"] != "quote_referenced_carry_counterfactual_preregistration"
        or definition["original_gate_override"] is not False
    ):
        raise ValueError("wrong prospective experiment")
    paths = {
        key: (root / value).resolve() for key, value in definition["paths"].items()
    }
    if any(path.parent != paper for path in paths.values()):
        raise ValueError("research path escaped its directory")
    allowed.update(paths.values())
    journal = paths["journal"]
    probe = paths["probe"]
    input_raw = paths["input"].read_bytes()
    probe_raw = probe.read_bytes()
    if (
        hashlib.sha256(input_raw).hexdigest() != args.input_sha256
        or hashlib.sha256(probe_raw).hexdigest() != args.probe_sha256
    ):
        raise ValueError("frozen public input/probe fingerprint differs")
    source = pathlib.Path(__file__).read_bytes()
    schedule = definition["schedule"]
    interval = schedule["interval_seconds"]
    origin = time.monotonic()
    origin_wall = time.time()
    entry = None
    last_time = None
    wallet = None
    deployments = None
    marks = 0
    calls = 0
    simulations = 0
    counts_complete = True
    gap = False
    reached = False
    checkpoints: set[int] = set()
    reason = "operational limit before complete horizon"
    start_number = 0
    recovery_raw = None
    recovery = None
    if args.recover_startup:
        recovery_raw = journal.read_bytes()
        recovery = recover_startup(
            recovery_raw,
            definition_sha256=args.definition_sha256,
            probe_sha256=args.probe_sha256,
            input_sha256=args.input_sha256,
        )
        entry = recovery["entry"]
        origin_wall = recovery["origin_wall"]
        require(time.time() >= origin_wall, "wall clock predates the frozen schedule")
        origin = time.monotonic() - (time.time() - origin_wall)
        records = recovery["records"]
        wallet = next(
            r["data"]["accounts"] for r in records if r["kind"] == "ACTUAL_BEFORE"
        )
        deployments = next(
            r["data"]["programs"] for r in records if r["kind"] == "NATIVE_DEPLOYMENTS"
        )
        last_time = entry["native"]["chain_timestamp"]
        finish = next(r["data"] for r in records if r["kind"] == "FINISH")
        calls, simulations = finish["rpc_requests"], finish["simulations"]
        start_number, marks = 1, 1

    def interrupted(_number: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with journal.open("ab" if recovery is not None else "xb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if recovery is not None:
            require(
                journal.read_bytes() == recovery_raw,
                "journal changed before exclusive recovery",
            )

        def emit(row: dict[str, Any], *, terminal: bool = False) -> None:
            encoded = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            reserve = 0 if terminal else 65_536
            if (
                stream.tell() + len(encoded)
                > schedule["maximum_journal_bytes"] - reserve
            ):
                raise ValueError("journal ceiling reached; existing evidence retained")
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

        if recovery is None:
            emit(
                {
                    "kind": "frozen_definition",
                    "definition": definition,
                    "definition_sha256": args.definition_sha256,
                    "recorder_source": source.decode(),
                    "recorder_source_sha256": hashlib.sha256(source).hexdigest(),
                    "probe_source": probe_raw.decode(),
                    "probe_source_sha256": args.probe_sha256,
                    "public_input": json.loads(input_raw),
                    "public_input_sha256": args.input_sha256,
                    "automatic_restart": False,
                }
            )
        else:
            emit(
                {
                    "kind": "implementation_recovery",
                    "reason": "Internal PnL transfers use floored deposit scaling; withdrawal-only rounding was incorrect. Anchor elapsed holding time to the actual native short-fill timestamp.",
                    "journal_prefix_sha256": recovery["journal_prefix_sha256"],
                    "previous_recorder_sha256": recovery["previous_recorder_sha256"],
                    "recorder_source": source.decode(),
                    "recorder_source_sha256": hashlib.sha256(source).hexdigest(),
                    "native_stdout_sha256": recovery["native_stdout_sha256"],
                    "original_entry_and_schedule_preserved": True,
                    "new_native_requests": 0,
                    "financial_limits_changed": False,
                    "negative_or_failed_observations_removed": False,
                }
            )
            emit(
                {
                    "kind": "frozen_entry",
                    "number": 0,
                    "entry": entry,
                    "recovered_from_retained_observation": True,
                }
            )
            emit(
                {
                    "kind": "mark",
                    "number": 0,
                    "native": entry["native"],
                    "position": value_position(entry, entry["native"]),
                    "gap_seen": gap,
                    "recovered_from_retained_observation": True,
                }
            )
            print(
                json.dumps(
                    {
                        "recovered_first_observation": True,
                        "native_calls": calls,
                        "native_simulations": simulations,
                        "entry_chain_timestamp": entry["entry_chain_timestamp"],
                        "all_in_profit_proven": False,
                    }
                ),
                flush=True,
            )
            print("CARRY_OBSERVER_READY", flush=True)
        try:
            for number in range(start_number, schedule["maximum_observations"]):
                if entry is None and number >= schedule["entry_observations"]:
                    reason = "rejected: no qualifying entry in the registered window"
                    break
                due = origin_wall + number * interval
                remaining = schedule["maximum_wall_seconds"] - (
                    time.monotonic() - origin
                )
                time.sleep(max(0, min(due - time.time(), remaining)))
                now = time.time()
                late = now - due
                require(late >= 0, "wall clock moved before the scheduled observation")
                if (
                    max(now - origin_wall, time.monotonic() - origin)
                    > schedule["maximum_wall_seconds"]
                ):
                    break
                if late > schedule["late_schedule_tolerance_seconds"]:
                    gap = True
                    emit(
                        {
                            "kind": "missed_scheduled_observation",
                            "number": number,
                            "scheduled_unix_time": origin_wall + number * interval,
                            "lateness_seconds": late,
                        }
                    )
                    continue
                require(
                    probe.read_bytes() == probe_raw
                    and paths["input"].read_bytes() == input_raw
                    and definition_path.read_bytes() == definition_raw,
                    "frozen source/input/definition changed",
                )
                emit(
                    {
                        "kind": "observation_attempt",
                        "number": number,
                        "scheduled_unix_time": origin_wall + number * interval,
                        "started_unix_time": time.time(),
                    }
                )
                counts_complete = False
                try:
                    result = subprocess.run(  # noqa: S603 -- exact frozen argv, no shell, no signing
                        [sys.executable, "-I", "-B", str(probe)],
                        input=input_raw,
                        capture_output=True,
                        timeout=schedule["maximum_probe_seconds"] + 5,
                        check=False,
                        env={
                            "PATH": os.defpath,
                            "HOME": str(pathlib.Path.home()),
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "UV_NO_ENV_FILE": "1",
                        },
                    )
                except subprocess.TimeoutExpired as error:
                    emit(
                        {
                            "kind": "native_timeout",
                            "number": number,
                            "stdout": (error.stdout or b"").decode(errors="replace"),
                            "stderr": (error.stderr or b"").decode(errors="replace"),
                        }
                    )
                    raise ValueError("native observation outcome incomplete") from error
                stdout = result.stdout.decode()
                emit(
                    {
                        "kind": "native_observation",
                        "number": number,
                        "returncode": result.returncode,
                        "stdout": stdout,
                        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                        "stderr": result.stderr.decode(errors="replace"),
                    }
                )
                records, frame = interpret(stdout, args.probe_sha256)
                finish = next(r["data"] for r in records if r["kind"] == "FINISH")
                calls += finish["rpc_requests"]
                simulations += finish["simulations"]
                counts_complete = True
                require(
                    calls <= schedule["maximum_chain_rpc_calls"]
                    and simulations <= schedule["maximum_simulations"],
                    "registered total native budget exceeded",
                )
                require(
                    not any(
                        r["kind"] == "RAW_RESPONSE"
                        and r["data"]["http_status"] in (401, 403, 429)
                        for r in records
                    ),
                    "access denial/throttling: no future retry",
                )
                before = next(
                    (
                        r["data"]["accounts"]
                        for r in records
                        if r["kind"] == "ACTUAL_BEFORE"
                    ),
                    None,
                )
                deployment = next(
                    (
                        r["data"]["programs"]
                        for r in records
                        if r["kind"] == "NATIVE_DEPLOYMENTS"
                    ),
                    None,
                )
                require(
                    wallet is None or before is None or before == wallet,
                    "public wallet context changed between observations",
                )
                require(
                    deployments is None
                    or deployment is None
                    or deployment == deployments,
                    "native program deployment changed",
                )
                if wallet is None and before is not None:
                    wallet = before
                if deployments is None and deployment is not None:
                    deployments = deployment
                clock = next(
                    (r["data"] for r in records if r["kind"] == "NATIVE_CHAIN_CLOCK"),
                    None,
                )
                position = None
                if clock is not None:
                    timestamp = clock["chain_timestamp"]
                    require(
                        last_time is None or timestamp > last_time,
                        "chain time did not advance",
                    )
                    last_time = timestamp
                    if entry is not None:
                        reached = (
                            timestamp - entry["entry_chain_timestamp"]
                            >= schedule["holding_chain_seconds"]
                        )
                if frame is None:
                    gap = True
                    emit(
                        {
                            "kind": "unavailable",
                            "number": number,
                            "errors": [
                                r["data"]
                                for r in records
                                if r["kind"]
                                in {
                                    "EXPLICIT_FAILURE",
                                    "AFTER_FAILURE",
                                    "RAW_TRANSPORT_ERROR",
                                }
                            ],
                        }
                    )
                else:
                    if entry is None:
                        eligible = (
                            frame["cost_gate_pass"]
                            and frame["buffer_gate_pass"]
                            and frame["funding_index_unchanged_from_observed_bank"]
                        )
                        if eligible:
                            entry = freeze_entry(frame)
                            emit(
                                {
                                    "kind": "frozen_entry",
                                    "number": number,
                                    "entry": entry,
                                }
                            )
                        else:
                            emit(
                                {
                                    "kind": "admission_rejected",
                                    "number": number,
                                    "cold_cash_cost_usdt": frame["cold_cash_cost_usdt"],
                                    "returned_usdt_raw": frame["returned_usdt_raw"],
                                    "unchanged_funding_checkpoint": frame[
                                        "funding_index_unchanged_from_observed_bank"
                                    ],
                                }
                            )
                    if entry is not None:
                        if not frame["funding_index_unchanged_from_observed_bank"]:
                            gap = True
                            emit(
                                {
                                    "kind": "unavailable_position_mark",
                                    "number": number,
                                    "reason": "simulation advanced funding outside the observed bank checkpoint",
                                }
                            )
                        else:
                            position = value_position(entry, frame)
                            marks += 1
                            reached |= (
                                position["elapsed_chain_seconds"]
                                >= schedule["holding_chain_seconds"]
                            )
                            emit(
                                {
                                    "kind": "mark",
                                    "number": number,
                                    "native": frame,
                                    "position": position,
                                    "gap_seen": gap,
                                }
                            )
                if entry is not None and clock is not None:
                    elapsed = clock["chain_timestamp"] - entry["entry_chain_timestamp"]
                    if position is not None:
                        elapsed = max(elapsed, position["elapsed_chain_seconds"])
                    for boundary in schedule["checkpoint_chain_seconds"]:
                        if elapsed >= boundary and boundary not in checkpoints:
                            checkpoints.add(boundary)
                            emit(
                                {
                                    "kind": "checkpoint",
                                    "boundary_chain_seconds": boundary,
                                    "actual_elapsed_chain_seconds": elapsed,
                                    "position": position,
                                    "gap_seen": gap,
                                    "all_in_profit_proven": False,
                                }
                            )
                    if reached:
                        reason = (
                            "registered chain horizon reached"
                            if position is not None
                            else "registered horizon reached with unavailable terminal valuation"
                        )
                print(
                    json.dumps(
                        {
                            "observation": number,
                            "marks": marks,
                            "entry_frozen": entry is not None,
                            "native_calls": calls,
                            "native_simulations": simulations,
                            "gap_seen": gap,
                            "all_in_profit_proven": False,
                        }
                    ),
                    flush=True,
                )
                if number == 0:
                    print("CARRY_OBSERVER_READY", flush=True)
                if reached:
                    break
                if entry is None and number + 1 >= schedule["entry_observations"]:
                    reason = "rejected: no qualifying entry in the registered window"
                    break
        except KeyboardInterrupt:
            gap = True
            reason = "interrupted; evidence retained, no automatic restart"
        except Exception as error:
            gap = True
            reason = f"{type(error).__name__}: {error}"
            raise
        finally:
            emit(
                {
                    "kind": "observation_finished"
                    if reached
                    else "observation_incomplete",
                    "reason": reason,
                    "marks": marks,
                    "native_calls": calls if counts_complete else None,
                    "native_simulations": simulations if counts_complete else None,
                    "completed_observation_calls": calls,
                    "completed_observation_simulations": simulations,
                    "counts_complete": counts_complete,
                    "entry_frozen": entry is not None,
                    "target_reached": reached,
                    "coverage_complete": reached and not gap,
                    "all_in_profit_proven": False,
                },
                terminal=True,
            )
            print(
                json.dumps(
                    {
                        "finished": reached,
                        "reason": reason,
                        "coverage_complete": reached and not gap,
                        "all_in_profit_proven": False,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
