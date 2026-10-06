"""Keeper for the whale-ride paper session.

Same discipline as keep_whaleride_shadow.py, aimed at the dry-run paper
bot: the bot never signs (dry-run), so restart-on-death is safe. Watch
conditions:
- process dead -> restart (identity-verified PID file)
- no new lesson rows for STALE_SECONDS (decisions flow at ~dozens/hour;
  silence means the processor is stuck — the hung-startup lesson: a live
  PID with a silent log is not a working bot)

The gate's quiet-stretch regime is real (mayhem flow clusters), so the
staleness bound is generous (15 minutes) and a restart during a genuine
quiet stretch is harmless: the recovery journal replays cleanly.

Usage:
    uv run --offline --no-sync python -B \\
        learning-examples/token-lifecycles/keep_whaleride_paper.py
"""

# ruff: noqa: C901, PLR0912, PLR0915, PLC0415, S603 - same runner-script pattern as the shadow keeper

from __future__ import annotations

import calendar
import json
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAPER_DIR = ROOT / ".state" / "whaleride-paper"
KEEPER_LOG = PAPER_DIR / "keeper.log"
PID_FILE = PAPER_DIR / "bot.pid"
GAVE_UP = PAPER_DIR / "keeper.gave-up"
LESSONS_DB = ROOT / ".state" / "learning" / "lessons.sqlite3"
CONFIG = ROOT / ".state" / "configs" / "whaleride-paper.yaml"

BOT_CMD = [
    "env",
    "-u",
    "SOLANA_PRIVATE_KEY",
    "-u",
    "SOLANA_NODE_RPC_ENDPOINT",
    "-u",
    "SOLANA_NODE_WSS_ENDPOINT",
    "-u",
    "GEYSER_ENDPOINT",
    "-u",
    "GEYSER_API_TOKEN",
    "uv",
    "run",
    "--offline",
    "--no-sync",
    "python",
    "-B",
    str(ROOT / "src" / "bot_runner.py"),
    "--config",
    str(CONFIG),
]
MAX_RESTARTS = 5
MAX_QUICK = 3
QUICK_DEATH_SECONDS = 90.0
MIN_BACKOFF = 30.0
MAX_BACKOFF = 600.0
STALE_SECONDS = 900.0  # no lesson rows for fifteen minutes
POLL_SECONDS = 60.0


def log(event: str, payload: dict) -> None:
    line = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        + " "
        + json.dumps({**payload, "event": event}, default=str)
    )
    print(line, flush=True)
    KEEPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with KEEPER_LOG.open("a") as f:
        f.write(line + "\n")


def pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "bot_runner" in out


def spawn() -> tuple[int, subprocess.Popen]:
    log("spawn", {"config": CONFIG.name})
    proc = subprocess.Popen(
        BOT_CMD,
        cwd=str(ROOT),
        stdout=sys.stdout,
        stderr=sys.stderr,
        start_new_session=True,
    )
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    if pid is not None and pid != __import__("os").getpid() and pid_alive(pid):
        __import__("os").kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.5)
            if not pid_alive(pid):
                return
        __import__("os").kill(pid, signal.SIGKILL)


def last_row_utc() -> str | None:
    """The newest lesson row's utc, or None when the DB is unreadable."""
    try:
        conn = sqlite3.connect(f"file:{LESSONS_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT MAX(utc) FROM lessons").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def seconds_since_row() -> float | None:
    utc = last_row_utc()
    if utc is None:
        return None
    try:
        # calendar.timegm: the journal stamps UTC; mktime would read the
        # string as local time (PDT here) and land the stamp ~7h ahead,
        # inverting every staleness decision (observed live: stale_s -24058).
        stamp = calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None
    return time.time() - stamp


def backoff_seconds(quick_deaths: int) -> float:
    return min(MAX_BACKOFF, MIN_BACKOFF * 2 ** max(0, quick_deaths - 1))


def _give_up(reason: str, restarts: int, detail: dict) -> int:
    GAVE_UP.parent.mkdir(parents=True, exist_ok=True)
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
    proc: subprocess.Popen | None = None
    pid: int | None = None
    restarts = 0
    quick_deaths = 0
    started = 0.0
    log("keeper_start", {"config": str(CONFIG), "stale_s": STALE_SECONDS})

    stop_flag = {"raised": False}

    def on_term(signum: int, frame: object) -> None:  # noqa: ARG001
        stop_flag["raised"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    previous_pid_file = PID_FILE.read_text() if PID_FILE.exists() else ""
    if previous_pid_file.strip().isdigit():
        orphan = int(previous_pid_file.strip())
        if pid_alive(orphan):
            log("adopt_kill", {"pid": orphan})
            stop(None, orphan)
    PID_FILE.unlink(missing_ok=True)

    pid, proc = spawn()
    started = time.monotonic()

    while not stop_flag["raised"]:
        time.sleep(POLL_SECONDS)
        alive = (proc is not None and proc.poll() is None) or (
            pid is not None and pid_alive(pid)
        )
        stale = seconds_since_row()
        reason = None
        if not alive:
            reason = "process_dead"
        elif stale is not None and stale > STALE_SECONDS:
            reason = "no_lesson_rows"
        elif stale is None and time.monotonic() - started > STALE_SECONDS:
            reason = "journal_unreadable"
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
            {
                "reason": reason,
                "restarts": restarts,
                "quick_deaths": quick_deaths,
                "stale_s": stale is not None and round(stale),
            },
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
        while slept < wait and not stop_flag["raised"]:
            time.sleep(min(1.0, wait - slept))
            slept += 1.0
        if stop_flag["raised"]:
            break
        pid, proc = spawn()
        started = time.monotonic()

    stop(proc, pid)
    PID_FILE.unlink(missing_ok=True)
    log("keeper_stop", {})
    return 0


def _check(*, condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def self_check() -> None:
    """Decision helpers on synthetic values; no processes, no DB writes."""
    import os as _os

    _check(
        condition=pid_alive(_os.getpid()) is False, message="self pid is not the bot"
    )
    _check(
        condition=[backoff_seconds(q) for q in range(1, 6)]
        == [30.0, 60.0, 120.0, 240.0, 480.0],
        message="quick-death backoff ladder",
    )
    _check(
        condition=STALE_SECONDS > MIN_BACKOFF,
        message="stale bound respects the quiet-stretch regime",
    )
    print("self_check ok")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        self_check()
        return
    raise SystemExit(run())


if __name__ == "__main__":
    main()
