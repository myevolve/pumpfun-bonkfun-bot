"""Keeper for the whale-ride shadow collector.

Supervises `run_whaleride_shadow.py` so the forward test survives crashes,
hung ticks and terminal-session teardown. Patterns adopted from the ECC
agent harness: loop-status.js's progress watermark and outstanding-operation
age, the observer's identity-verified PID file and TERM-then-KILL child
cleanup, loop-operator's bounded-restart escalation.

Watch conditions (any triggers a restart, except the last):
- collector process dead -> restart
- no new rows in shadow.jsonl for STALE_SECONDS (the hung-startup lesson:
  a live PID with a silent log is not a working collector)
- last traced slot frozen for FROZEN_SECONDS (rows flowing but the chain
  view stopped advancing = degraded, not dead)

Restart discipline: quick deaths within QUICK_DEATH_SECONDS escalate the
backoff, and more than MAX_QUICK in a row stops the keeper with a
`keeper.gave-up` marker instead of crash-looping.

Usage:
    uv run --offline --no-sync python -B \\
        learning-examples/token-lifecycles/keep_whaleride_shadow.py
    # ... --self-check exercises the decision helper on synthetic states.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHADOW_DIR = ROOT / ".state" / "whaleride-shadow"
SHADOW_PATH = SHADOW_DIR / "shadow.jsonl"
KEEPER_LOG = SHADOW_DIR / "keeper.log"
PID_FILE = SHADOW_DIR / "collector.pid"
GAVE_UP = SHADOW_DIR / "keeper.gave-up"

COLLECTOR_CMD = [
    sys.executable,
    "-B",
    str(ROOT / "learning-examples" / "token-lifecycles" / "run_whaleride_shadow.py"),
]
MAX_RESTARTS = 5
MAX_QUICK = 3
QUICK_DEATH_SECONDS = 60.0
MIN_BACKOFF = 30.0
MAX_BACKOFF = 600.0
STALE_SECONDS = 600.0  # no rows at all for ten minutes
FROZEN_SECONDS = 900.0  # rows flowing but slot frozen for fifteen minutes
POLL_SECONDS = 30.0


def log(event: str, payload: dict) -> None:
    line = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        + " "
        + json.dumps({**payload, "event": event}, default=str)
    )
    print(line, flush=True)
    with KEEPER_LOG.open("a") as f:
        f.write(line + "\n")


def pid_alive(pid: int) -> bool:
    """os.kill(pid, 0) proves existence, not identity (LESSONS #5): verify argv."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no untrusted input
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "run_whaleride_shadow" in out


def spawn() -> tuple[int, subprocess.Popen]:
    log("spawn", {"cmd": COLLECTOR_CMD[-1]})
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no untrusted input
        COLLECTOR_CMD,
        cwd=str(ROOT),
        stdout=sys.stdout,
        stderr=sys.stderr,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    return proc.pid, proc


def stop(proc: subprocess.Popen | None, pid: int | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return
    if pid is not None and pid != os.getpid() and pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.5)
            if not pid_alive(pid):
                return
        os.kill(pid, signal.SIGKILL)


def watch_state() -> tuple[float, int] | None:
    """(last row wall-ts, last traced slot) from the JSONL tail, or None."""
    if not SHADOW_PATH.exists():
        return None
    try:
        with SHADOW_PATH.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = [
                line for line in f.read().decode(errors="replace").splitlines() if line
            ]
    except OSError:
        return None
    last_ts, last_slot = 0.0, 0
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if last_ts == 0.0 and row.get("ts"):
            last_ts = float(row["ts"])
        if last_slot == 0 and row.get("slot"):
            last_slot = int(row["slot"])
        if last_ts and last_slot:
            break
    if last_ts == 0.0:
        return None
    return last_ts, last_slot


def health_reason(
    *,
    alive: bool,
    state: tuple[float, int] | None,
    previous: tuple[float, int] | None,
    now: float,
    started: float,
) -> str | None:
    """One restart decision per tick; None means healthy."""
    if not alive:
        return "process_dead"
    if state is None:
        return "no_rows_ever" if now - started > STALE_SECONDS else None
    last_ts, last_slot = state
    if now - last_ts > STALE_SECONDS:
        return "rows_stale"
    if (
        previous is not None
        and previous[1] == last_slot
        and now - started > FROZEN_SECONDS
    ):
        return "slot_frozen"
    return None


def backoff_seconds(quick_deaths: int) -> float:
    return min(MAX_BACKOFF, MIN_BACKOFF * 2 ** max(0, quick_deaths - 1))


def _give_up(reason: str, restarts: int, detail: dict) -> int:
    """Stop crash-looping: write the marker and hand the decision to a human."""
    GAVE_UP.write_text(
        json.dumps(
            {
                **detail,
                "reason": reason,
                "restarts": restarts,
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    )
    log("gave_up", {**detail, "restarts": restarts})
    return 2


def run() -> int:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    GAVE_UP.unlink(missing_ok=True)
    proc: subprocess.Popen | None = None
    pid: int | None = None
    restarts = 0
    quick_deaths = 0
    started = 0.0
    previous_state: tuple[float, int] | None = None
    log("keeper_start", {"shadow": str(SHADOW_PATH)})
    stop_signal = {"raised": False}

    def on_term(signum: int, frame: object) -> None:  # noqa: ARG001
        stop_signal["raised"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    pid, proc = spawn()
    started = time.monotonic()

    while not stop_signal["raised"]:
        time.sleep(POLL_SECONDS)
        alive = (proc is not None and proc.poll() is None) or (
            pid is not None and pid_alive(pid)
        )
        state = watch_state()
        reason = health_reason(
            alive=alive,
            state=state,
            previous=previous_state,
            now=time.time(),
            started=started,
        )
        previous_state = state
        if reason is None:
            continue
        if (
            reason == "process_dead"
            and time.monotonic() - started < QUICK_DEATH_SECONDS
        ):
            quick_deaths += 1
        else:
            quick_deaths = 0
        log(
            "restart",
            {"reason": reason, "restarts": restarts, "quick_deaths": quick_deaths},
        )
        stop(proc, pid)
        if quick_deaths >= MAX_QUICK:
            return _give_up(
                "quick_death_loop", restarts, {"quick_deaths": quick_deaths}
            )
        restarts += 1
        if restarts > MAX_RESTARTS:
            return _give_up("restart_budget_exhausted", restarts, {})
        wait = backoff_seconds(quick_deaths)
        log("backoff", {"seconds": wait})
        slept = 0.0
        while slept < wait and not stop_signal["raised"]:
            time.sleep(min(1.0, wait - slept))
            slept += 1.0
        if stop_signal["raised"]:
            break
        pid, proc = spawn()
        started = time.monotonic()
        previous_state = None

    stop(proc, pid)
    PID_FILE.unlink(missing_ok=True)
    log("keeper_stop", {})
    return 0


def _check(*, condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def self_check() -> None:
    """Decision helper on synthetic states; no processes started, no files."""
    _check(
        condition=pid_alive(os.getpid()) is False,
        message="self pid is not the collector",
    )
    _check(
        condition=[backoff_seconds(q) for q in range(1, 6)]
        == [30.0, 60.0, 120.0, 240.0, 480.0],
        message="quick-death backoff ladder",
    )
    _check(
        condition=STALE_SECONDS < FROZEN_SECONDS, message="stale fires before frozen"
    )
    _check(
        condition=health_reason(
            alive=False, state=None, previous=None, now=0.0, started=0.0
        )
        == "process_dead",
        message="dead detect",
    )
    _check(
        condition=health_reason(
            alive=True, state=None, previous=None, now=30.0, started=0.0
        )
        is None,
        message="quiet start is healthy",
    )
    _check(
        condition=health_reason(
            alive=True, state=None, previous=None, now=STALE_SECONDS + 1, started=0.0
        )
        == "no_rows_ever",
        message="silent start escalates",
    )
    _check(
        condition=health_reason(
            alive=True,
            state=(FROZEN_SECONDS, 7),
            previous=(FROZEN_SECONDS - 30, 7),
            now=FROZEN_SECONDS + 1,
            started=0.0,
        )
        == "slot_frozen",
        message="frozen slot escalates",
    )
    _check(
        condition=health_reason(
            alive=True,
            state=(FROZEN_SECONDS, 7),
            previous=(FROZEN_SECONDS - 30, 7),
            now=FROZEN_SECONDS + 1,
            started=FROZEN_SECONDS + 5,
        )
        is None,
        message="frozen needs tenure",
    )
    print("self_check ok")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        self_check()
        return
    raise SystemExit(run())


if __name__ == "__main__":
    main()
