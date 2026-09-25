"""Verify carry scheduling across clock divergence; never network or move funds."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

# Explicit clock boundaries and disposable journal fixtures.
# ruff: noqa: PLR2004, S101


def exercise(jump: int) -> None:  # noqa: PLR0915 -- isolated real-loop regression
    """Exercise the real recorder loop with a clock jump and no native process."""
    source = pathlib.Path(__file__).with_name("record_carry_returns.py")
    spec = importlib.util.spec_from_file_location("carry_clock_check", source)
    assert spec is not None and spec.loader is not None
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    root = pathlib.Path.cwd()
    paper = root / ".state/paper-trading"
    paper.mkdir(parents=True)
    probe_raw, input_raw = b"# Offline test probe; never executed.\n", b"{}\n"
    (paper / "probe.py").write_bytes(probe_raw)
    (paper / "input.json").write_bytes(input_raw)
    definition = {
        "kind": "quote_referenced_carry_counterfactual_preregistration",
        "original_gate_override": False,
        "paths": {
            "probe": ".state/paper-trading/probe.py",
            "input": ".state/paper-trading/input.json",
            "journal": ".state/paper-trading/journal.jsonl",
        },
        "schedule": {
            "interval_seconds": 3600,
            "maximum_observations": 2,
            "entry_observations": 2,
            "maximum_wall_seconds": 7200,
            "late_schedule_tolerance_seconds": 180,
            "maximum_journal_bytes": 1_000_000,
            "maximum_probe_seconds": 1,
            "maximum_chain_rpc_calls": 10,
            "maximum_simulations": 2,
        },
    }
    definition_raw = json.dumps(definition).encode()
    definition_path = paper / "definition.json"
    definition_path.write_bytes(definition_raw)
    sys.argv = [
        str(source),
        "--definition",
        str(definition_path),
        "--definition-sha256",
        hashlib.sha256(definition_raw).hexdigest(),
        "--probe-sha256",
        hashlib.sha256(probe_raw).hexdigest(),
        "--input-sha256",
        hashlib.sha256(input_raw).hexdigest(),
    ]
    clocks = [1000.0, 0.0]
    jumped = False
    wall_reads = 0

    def wall_time() -> float:
        nonlocal jumped, wall_reads
        wall_reads += 1
        if jump == -10800 and wall_reads == 2:
            clocks[0] += jump
            jumped = True
        return clocks[0]

    def sleep(seconds: float) -> None:
        nonlocal jumped
        clocks[0] += seconds + (0 if jumped else jump)
        clocks[1] += seconds
        jumped = True

    def no_native_process(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        records = [
            {
                "kind": "EXECUTED_SOURCE",
                "data": {
                    "raw": probe_raw.decode(),
                    "sha256": hashlib.sha256(probe_raw).hexdigest(),
                },
            },
            {
                "kind": "FINISH",
                "data": {
                    "signed": 0,
                    "submitted": 0,
                    "runtime_writes": 0,
                    "rpc_requests": 0,
                    "simulations": 0,
                    "exit_code": 0,
                },
            },
        ]
        stdout = "\n".join(json.dumps(row) for row in records).encode()
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    def forbid_network(event: str, _args: tuple) -> None:
        if event.startswith("socket."):
            raise AssertionError(event)

    sys.addaudithook(forbid_network)
    clock = SimpleNamespace(time=wall_time, monotonic=lambda: clocks[1], sleep=sleep)
    error = None
    with (
        patch.object(recorder, "time", clock),
        patch.object(recorder.subprocess, "run", no_native_process),
    ):
        try:
            recorder.main()
        except ValueError as exc:
            error = str(exc)
    rows = [
        json.loads(line) for line in (paper / "journal.jsonl").read_bytes().splitlines()
    ]
    attempts = [row for row in rows if row["kind"] == "observation_attempt"]
    missed = [row for row in rows if row["kind"] == "missed_scheduled_observation"]
    assert rows[-1]["kind"] == "observation_incomplete"
    assert clocks[1] <= definition["schedule"]["maximum_wall_seconds"], (
        "clock correction extended the runtime budget"
    )
    if jump == 941:
        assert [row["number"] for row in attempts] == [1], (
            "expired UTC observation executed"
        )
        assert [row["number"] for row in missed] == [0]
        assert (
            attempts[0]["started_unix_time"]
            == attempts[0]["scheduled_unix_time"]
            == 4600
        )
        assert missed[0]["lateness_seconds"] == 941
        assert error is None
    elif jump < 0:
        assert not attempts and error is not None, (
            "observation executed before its UTC schedule"
        )
    else:
        assert not attempts and error is None, (
            "observation executed beyond the wall-time budget"
        )
    print(f"PASS: clock jump {jump}, expired observations never become native attempts")


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        exercise(int(sys.argv[2]))
        return
    for jump in (941, -1, 10800, -10800):
        with tempfile.TemporaryDirectory(prefix="carry-schedule-") as root:
            subprocess.run(  # noqa: S603 -- this verifier's isolated offline child
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(pathlib.Path(__file__).resolve()),
                    "--child",
                    str(jump),
                ],
                cwd=root,
                check=True,
                timeout=10,
            )


if __name__ == "__main__":
    main()
