"""Streamlit dashboard for the pumpfun-bonkfun-bot.

Read-only market data with operational controls. Buttons run the bot
runner / cycle scanner as subprocesses; live submission still requires
the CLI-only --authorize-live gate (never exposed here).

Run: uv run streamlit run src/dashboard.py
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import statistics
import subprocess
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

import dashboard_theme
from learning import trade_evidence
from learning.report import load_report
from utils.paths import STATE_DIR, state_path

st.set_page_config(page_title="PumpFun Terminal", page_icon="🎯", layout="wide")
dashboard_theme.inject()

# ─── Paths ───────────────────────────────────────────────────────────────────

CONFIG_PATH = state_path("configs", "live-readiness.yaml")
PAPER_CFG_PATH = state_path("configs", "paper-trade-dash.yaml")
CREDS_PATH = state_path("wallets", "live-readiness.secrets")
LEDGER_DIR = state_path("transaction-ledgers")
TRADES_DIR = Path("trades")
LOGS_DIR = Path("logs")
WATCH_PATH = state_path("letsbonk-watch.json")
RUN_LOGS = STATE_DIR

LIVE_CFG_PATH = state_path("configs", "live-trade-ui.yaml")
PAPER_LOG = RUN_LOGS / "paper-trade-ui.log"
LIVE_LOG = RUN_LOGS / "live-trade-ui.log"
SCANNER_LOG = RUN_LOGS / "scanner-ui.log"


# ─── Data loaders ────────────────────────────────────────────────────────────


@st.cache_data(ttl=5)
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open() as f:
        cfg = yaml.safe_load(f) or {}
    # Display-only interpolation of the expected wallet; never any key material.
    env_file = Path(cfg.get("env_file", ""))
    placeholder = "${SOLANA_EXPECTED_WALLET}"
    expected = cfg.get("execution", {}).get("expected_wallet")
    if env_file.exists() and expected == placeholder:
        for line in env_file.read_text().splitlines():
            if line.startswith("SOLANA_EXPECTED_WALLET="):
                cfg["execution"]["expected_wallet"] = line.split("=", 1)[1].strip()
                break
    return cfg


@st.cache_data(ttl=5)
def load_watch_set() -> dict:
    if not WATCH_PATH.exists():
        return {}
    return json.loads(WATCH_PATH.read_text())


DETECTED_TOKENS_LIMIT = 30


@st.cache_data(ttl=5)
def load_trade_files(limit: int = DETECTED_TOKENS_LIMIT) -> list[dict]:
    """Newest detected-token records, read only as far as the tab renders.

    The trades directory holds one file per detection; reading every one to
    render the newest 30 was by far the dashboard's most expensive loader.
    """
    if not TRADES_DIR.exists():
        return []
    trades = []
    for f in sorted(
        TRADES_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            data = json.loads(f.read_text())
            data["_file"] = str(f)
            data["_mtime"] = f.stat().st_mtime
            trades.append(data)
        except (json.JSONDecodeError, KeyError):
            pass
        if len(trades) >= limit:
            break
    return trades


@st.cache_data(ttl=5)
def load_ledger(wallet: str | None) -> list[dict]:
    if not wallet or not LEDGER_DIR.exists():
        return []
    db_path = LEDGER_DIR / f"{wallet}.sqlite3"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM submissions ORDER BY rowid DESC LIMIT 500"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


@st.cache_data(ttl=5)
def load_recent_logs(n: int = 2000) -> list[dict]:
    """Parsed entries from the newest logs, noisy loggers filtered out.

    event_parser emits ~10 lines per token and httpx one per RPC call, so a
    raw line window is mostly noise. Filter by logger BEFORE truncating, and
    read a generous slice (3 files x 20k lines) so activity charts cover the
    last hour, not the last 40 seconds.
    """
    if not LOGS_DIR.exists():
        return []
    log_files = sorted(
        LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    noisy = {
        "platforms.pumpfun.event_parser",
        "platforms.letsbonk.event_parser",
        "httpx",
        "urllib3",
        "asyncio",
    }
    parsed: list[dict] = []
    for lf in log_files[:3]:
        lines = lf.read_text(errors="replace").splitlines()[-20000:]
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                d = {"message": line, "level": "RAW"}
            if d.get("logger") in noisy:
                continue
            parsed.append(d)
    return parsed[-n:]


BUY_RE = re.compile(r"Buying ([\d,.]+) tokens at average quote ([\d.eE+-]+)")


@st.cache_data(ttl=5)
def load_activity() -> pd.DataFrame:
    """Per-minute event counts (detected/buy/fail/blocked) from recent logs."""
    rows: list[dict] = []
    for entry in load_recent_logs(n=2000):
        msg = entry.get("message", "")
        ts = entry.get("timestamp", "")
        if not ts:
            continue
        event = None
        if "New token detected" in msg:
            event = "detected"
        elif msg.startswith("Buying "):
            event = "buy"
        elif "Failed to buy" in msg:
            event = "fail"
        elif "blocked" in msg.lower() and "dry-run" in msg.lower():
            event = "blocked"
        if event:
            rows.append({"ts": ts, "event": event})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601", errors="coerce")
    df = df.dropna(subset=["ts"])
    df["minute"] = df["ts"].dt.floor("min")
    return (
        df.groupby(["minute", "event"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
        .set_index("minute")
    )


@st.cache_data(ttl=5)
def load_positions() -> pd.DataFrame:
    """Token position sizes parsed from 'Buying N tokens ...' log lines."""
    rows: list[dict] = []
    for entry in load_recent_logs(n=2000):
        m = BUY_RE.match(entry.get("message", ""))
        if not m:
            continue
        try:
            rows.append(
                {
                    "ts": entry.get("timestamp", ""),
                    "tokens": float(m.group(1).replace(",", "")),
                    "quote": float(m.group(2)),
                }
            )
        except ValueError:
            continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


LESSON_DB = state_path("learning", "lessons.sqlite3")


@st.cache_data(ttl=10)
def load_learning_stats() -> dict:
    """Use the same read-only eligibility rules as the CLI report."""
    data = load_report(limit=40, db_path=LESSON_DB)
    return {} if "error" in data else data


@st.cache_data(ttl=10, max_entries=8)
def load_trade_evidence(path: Path) -> dict:
    """Cache the shared audit snapshot together with its original generation time."""
    return trade_evidence.summarize(path)


def _evidence_amount(value: int | None) -> str:
    """Keep unknowns distinct from zero and integer amounts exact in the browser."""
    return "Unknown" if value is None else f"{value:,}"


def load_whaleride_coupling() -> dict:
    """Coupling per X from the shadow collector's JSONL, read-only."""
    return _coupling_from_rows(_read_shadow_rows())


def _read_shadow_rows() -> tuple[dict[str, list], dict[str, list]]:
    """Curve and pool rows from the shadow collector's JSONL."""
    path = state_path("whaleride-shadow", "shadow.jsonl")
    curves: dict[str, list] = {}
    pools: dict[str, list] = {}
    if not path.exists():
        return curves, pools
    try:
        with path.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                mint = row.get("m") or row.get("mint")
                if not mint:
                    continue
                if row.get("k") == "c":
                    curves.setdefault(mint, []).append(row)
                elif row.get("k") == "p":
                    pools.setdefault(mint, []).append(row)
    except OSError:
        return {}, {}
    return curves, pools


def _coupling_from_rows(curves: dict[str, list], pools: dict[str, list]) -> dict:
    """Score every X level the way run_whaleride_shadow.report() does."""

    def tokens_bought(vq: int, vt: int) -> float:
        return vt - (vq * vt) / (vq + 9_875_000)

    out: dict[str, dict] = {}
    for x in (20, 30, 40, 50, 60):
        grad_arm: list[float] = []
        cold_arm: list[float] = []
        grad_after = crossers = 0
        for mint, cs in curves.items():
            cs.sort(key=lambda r: r["ts"])
            hit = next((r for r in cs if r["rq"] >= x * 1e9), None)
            if hit is None or hit["cp"]:
                continue
            graduated_after = any(r["cp"] for r in cs)
            tokens = tokens_bought(hit["vq"], hit["vt"])
            if graduated_after:
                pool_row = next(
                    (p for p in pools.get(mint, []) if p["ts"] >= hit["ts"] + 1.0),
                    None,
                )
                if pool_row is None:
                    continue
                eff, base = pool_row["q"] + pool_row["vrq"], pool_row["b"]
                pnl = (
                    -10_065_000
                    if base <= 0 or eff <= 0
                    else eff * tokens / (base + tokens) * 0.997 - 10_065_000
                )
                grad_arm.append(pnl)
                grad_after += 1
            else:
                exit_row = next((r for r in cs if r["ts"] >= hit["ts"] + 2.0), None)
                if exit_row is None:
                    continue
                pnl = (
                    exit_row["vq"] * tokens / (exit_row["vt"] + tokens) * 0.9875
                    - 10_065_000
                )
                cold_arm.append(pnl)
            crossers += 1
        out[f"x{x}"] = _coupling_row(grad_arm, cold_arm, grad_after, crossers)
    return out


def _coupling_row(
    grad_arm: list[float], cold_arm: list[float], grad_after: int, crossers: int
) -> dict:
    if not crossers or not grad_arm or not cold_arm:
        return {"status": "insufficient_data", "crossers": crossers}
    grad_mean = statistics.mean(grad_arm)
    cold_mean = statistics.mean(cold_arm)
    coupling = grad_after / crossers
    denom = grad_mean - cold_mean
    break_even = (-cold_mean / denom) if denom > 0 else 10.0
    return {
        "coupling": round(coupling, 4),
        "grad_n": grad_after,
        "crossers": crossers,
        "break_even_coupling": round(break_even, 4),
        "state": "ACTIVE" if coupling >= break_even else "dormant",
    }


# ─── Process control ─────────────────────────────────────────────────────────


def _pid_path(key: str) -> Path:
    return RUN_LOGS / f"ui-{key}.pid"


def _ps_args(pid: int) -> str:
    """argv of the process, empty if gone (macOS ps)."""
    try:
        r = subprocess.run(  # noqa: S603 - fixed local args
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip()


def _pid_alive(pid: int) -> bool:
    """True only if pid exists AND its argv still matches the expected
    command for this key. Guards against killing a reused PID."""
    argv = _ps_args(pid)
    return "bot_runner.py" in argv or "cycles/runner.py" in argv


def is_running(key: str) -> bool:
    proc: subprocess.Popen | None = st.session_state.get(f"proc_{key}")
    if proc is not None:
        code = proc.poll()
        if code is None:
            return True
        # Child exited: record outcome and clean the handle + pid file.
        st.session_state[f"last_exit_{key}"] = code
        st.session_state.pop(f"proc_{key}", None)
        _pid_path(key).unlink(missing_ok=True)
        if code not in (0, -15):
            st.session_state[f"failed_start_{key}"] = code
    pid_file = _pid_path(key)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid_file.unlink(missing_ok=True)
        else:
            if _pid_alive(pid):
                return True
            pid_file.unlink(missing_ok=True)
    return False


def start_process(key: str, cmd: list[str], log_file: Path) -> None:
    st.session_state.pop(f"failed_start_{key}", None)
    log = log_file.open("ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 - locally constructed command
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=Path.cwd(),
        )
    except OSError:
        log.close()
        raise
    st.session_state[f"proc_{key}"] = proc
    st.session_state[f"last_exit_{key}"] = None
    _pid_path(key).write_text(str(proc.pid))


def stop_process(key: str) -> None:
    proc: subprocess.Popen | None = st.session_state.get(f"proc_{key}")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)  # reap: no zombie left behind
    pid_file = _pid_path(key)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            pid = None
        if pid is not None and _pid_alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                if not _pid_alive(pid):
                    break
            else:
                os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
    st.session_state.pop(f"proc_{key}", None)


def run_command(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(  # noqa: S603 - locally constructed command
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=Path.cwd(),
        )
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {timeout}s"
    except FileNotFoundError as e:
        return 127, str(e)


def paper_config() -> Path:
    """Write a temp bot config: enabled, forced dry-run (no submissions)."""
    cfg = load_config()
    cfg["enabled"] = True
    cfg.setdefault("execution", {})["mode"] = "dry_run"
    PAPER_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAPER_CFG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return PAPER_CFG_PATH


def live_config() -> Path:
    """Write a temp bot config: enabled, live mode (needs --authorize-live)."""
    cfg = load_config()
    cfg["enabled"] = True
    cfg.setdefault("execution", {})["mode"] = "live"
    LIVE_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_CFG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return LIVE_CFG_PATH


def _json_from_output(out: str) -> dict:
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def run_all_checks() -> tuple[bool, list[dict]]:
    """Run --status and --preflight; returns (all_ok, checks) for display."""
    checks: list[dict] = []
    code, out = run_command(
        [
            "uv",
            "run",
            "python",
            "src/bot_runner.py",
            "--config",
            str(CONFIG_PATH),
            "--status",
        ]
    )
    status = _json_from_output(out)
    busy = bool(
        status.get("active_position_count")
        or status.get("active_submissions")
        or status.get("pending_cleanups")
        or status.get("unresolved_buy_count")
        or status.get("pending_token_count")
    )
    status_ok = code == 0 and bool(status) and not busy
    checks.append(
        {
            "name": "Status: no open positions or unresolved submissions",
            "ok": status_ok,
            "detail": out[:600] if not status_ok else "clean",
        }
    )
    code2, out2 = run_command(
        [
            "uv",
            "run",
            "python",
            "src/bot_runner.py",
            "--config",
            str(CONFIG_PATH),
            "--preflight",
        ]
    )
    pre = _json_from_output(out2)
    ready = bool(pre.get("ready"))
    pre_ok = code2 == 0 and ready
    checks.append(
        {
            "name": "Preflight: wallet, RPC, fees, ledger all ready",
            "ok": pre_ok,
            "detail": out2[:600] if not pre_ok else "ready",
        }
    )
    return status_ok and pre_ok, checks


def kill_all() -> None:
    """Kill every bot/scanner process now (UI-managed and orphaned)."""
    for key in ("paper", "live", "scanner"):
        stop_process(key)
    for pattern in ("src/bot_runner.py", "src/cycles/runner.py"):
        subprocess.run(  # noqa: S603 - static kill pattern
            ["/usr/bin/pkill", "-f", pattern], check=False, capture_output=True
        )
    st.session_state.pop("live_confirm", None)


# ─── Dashboard ───────────────────────────────────────────────────────────────

config = load_config()
wallet = config.get("execution", {}).get("expected_wallet", "")
risk_session = config.get("execution", {}).get("risk_session_id", "n/a")
enabled = config.get("enabled", False)
mode = config.get("execution", {}).get("mode", "unknown")
platform = config.get("platform", "unknown")

live_running = is_running("live")
paper_running = is_running("paper")
scanner_running = is_running("scanner")
# The base config says "live"; the *running* bot's mode is what's true.
if paper_running:
    mode = "dry_run (paper)"
elif live_running:
    mode = "live"

hdr_l, hdr_r = st.columns([3, 2])
with hdr_l:
    st.markdown("## 🎯 PumpFun Terminal")
with hdr_r:
    if live_running:
        tone, label = "red", "● LIVE"
    elif paper_running:
        tone, label = "green", "● PAPER"
    else:
        tone, label = "grey", "○ IDLE"
    extra = dashboard_theme.pill(platform, "blue")
    st.markdown(
        f"<div style='text-align:right'>{dashboard_theme.pill(label, tone)} "
        f"{extra} "
        f"{dashboard_theme.pill(f'risk: {risk_session[-12:]}', 'grey')}</div>",
        unsafe_allow_html=True,
    )

# ─── Sidebar ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Config")
    st.text(f"Name: {config.get('name', 'n/a')}")
    st.text(f"Platform: {platform}")
    st.text(f"Mode: {mode}")
    enabled_color = "🟢" if enabled else "🔴"
    st.markdown(f"{enabled_color} Enabled: **{enabled}**")
    st.text(f"Risk session: {risk_session}")
    buy_amount = config.get("trade", {}).get("buy_amount", "n/a")
    st.text(f"Buy amount: {buy_amount} SOL")
    exit_strategy = config.get("trade", {}).get("exit_strategy", "n/a")
    st.text(f"Exit: {exit_strategy}")

    st.divider()
    st.header("🎛 Controls")

    st.subheader("What's running")
    if live_running:
        st.error(
            "🔴 **LIVE TRADING** — real funds from wallet "
            f"`{wallet[:8]}…`. The bot submits real transactions: one buy "
            "plus its configured exit, capped by the risk session."
        )
    if paper_running:
        st.success(
            "🟢 **Paper trading** — watches the live market and simulates "
            "trades; every submission is blocked at the dry-run gate. "
            "No funds move."
        )
    if scanner_running:
        st.info(
            "🔭 **Cycle scanner** — watches graduated coins for "
            "curve↔AMM divergence. Observation only, never submits."
        )
    if not (live_running or paper_running or scanner_running):
        st.caption("⚪ Idle — nothing running.")

    if st.button("🛑 KILL SWITCH", type="primary", use_container_width=True):
        kill_all()
        st.rerun()
    st.caption(
        "One click stops everything immediately: paper bot, live bot, "
        "scanner, and any orphaned bot processes."
    )

    with st.expander("Last check results"):
        results = st.session_state.get("check_results", [])
        if not results:
            st.caption(
                "No checks run yet — they run automatically when you "
                "start paper or live."
            )
        for c in results:
            icon = "✅" if c["ok"] else "❌"
            st.markdown(f"{icon} {c['name']}")
            if not c["ok"]:
                st.code(c["detail"], language="json")

    st.divider()
    st.subheader("▶️ Paper trading")
    if paper_running or live_running:
        st.caption("A bot is already running — use the kill switch first.")
    elif st.button("▶️ Start Paper Trading", use_container_width=True):
        ok, checks = run_all_checks()
        st.session_state["check_results"] = checks
        if ok:
            paper_config()
            start_process(
                "paper",
                [
                    "uv",
                    "run",
                    "python",
                    "src/bot_runner.py",
                    "--config",
                    str(PAPER_CFG_PATH),
                ],
                PAPER_LOG,
            )
        st.rerun()
    st.caption(
        "One click: runs status + preflight automatically, then starts the "
        "bot in **dry-run** mode (real tokens, real prices, zero on-chain "
        "transactions)."
    )

    st.divider()
    st.subheader("🚀 Live trading")
    if live_running:
        st.caption("Live bot is running — the kill switch stops it.")
    elif paper_running:
        st.caption("Paper bot is running — kill switch, then go live.")
    else:
        typed = st.text_input(
            "Type AUTHORIZE LIVE to arm",
            key="live_confirm",
            placeholder="AUTHORIZE LIVE",
        )
        armed = typed.strip() == "AUTHORIZE LIVE"
        if not armed:
            st.caption("The GO LIVE button unlocks once the text matches exactly.")
        if st.button(
            "🚀 GO LIVE",
            type="primary",
            use_container_width=True,
            disabled=not armed,
        ):
            ok, checks = run_all_checks()
            st.session_state["check_results"] = checks
            if ok:
                live_config()
                start_process(
                    "live",
                    [
                        "uv",
                        "run",
                        "python",
                        "src/bot_runner.py",
                        "--config",
                        str(LIVE_CFG_PATH),
                        "--authorize-live",
                    ],
                    LIVE_LOG,
                )
            st.rerun()
        st.caption(
            "Runs all checks automatically and starts only if status is "
            "clean **and** preflight is ready. Real buys from the wallet "
            "within the configured risk-session caps."
        )

    st.divider()
    st.subheader("🔭 Cycle scanner")
    if scanner_running:
        st.caption("Scanner is running — the kill switch stops it.")
    elif st.button("🔭 Scan (event mode)", use_container_width=True):
        start_process(
            "scanner",
            [
                "uv",
                "run",
                "python",
                "src/cycles/runner.py",
                "--config",
                str(CONFIG_PATH),
                "--credentials",
                str(CREDS_PATH),
                "--event",
            ],
            SCANNER_LOG,
        )
        st.rerun()
    st.caption(
        "Event mode only — watches migrations live, logs divergence "
        "opportunities, submits nothing. (Polling mode requires "
        "--authorize-live and can submit, so it is CLI-only.)"
    )

    failed = {
        k: st.session_state.get(f"failed_start_{k}")
        for k in ("paper", "live", "scanner")
        if st.session_state.get(f"failed_start_{k}") is not None
    }
    if failed:
        st.error(f"Last launch failed with exit code(s): {failed}")

    for key, log in (
        ("paper", PAPER_LOG),
        ("live", LIVE_LOG),
        ("scanner", SCANNER_LOG),
    ):
        if is_running(key) and log.exists():
            with st.expander(f"📜 {key} output", expanded=False):
                text = log.read_text(errors="replace").splitlines()
                st.code("\n".join(text[-40:]) or "(no output yet)")

    st.divider()
    st.header("🔄 Live updates")
    refresh_secs = st.select_slider(
        "Auto-refresh",
        options=[0, 2, 5, 10, 30],
        value=5,
        format_func=lambda v: "Off" if v == 0 else f"{v}s",
    )
    st.session_state["refresh_secs"] = refresh_secs

    st.caption(f"Config: {CONFIG_PATH}")
    st.caption("Live trading requires typing AUTHORIZE LIVE plus clean checks.")


# ─── Tabs ────────────────────────────────────────────────────────────────────

tab_overview, tab_charts, tab_learning, tab_trades, tab_watch, tab_logs = st.tabs(
    ["📊 Overview", "📈 Charts", "🧠 Learning", "💱 Trades", "🔍 Watch", "📋 Logs"]
)

# ─── Overview tab ────────────────────────────────────────────────────────────

with tab_overview:
    trade_cfg = config.get("trade", {})
    trade_cfg_amt = trade_cfg.get("buy_amount", "n/a")
    max_hold = trade_cfg.get("max_hold_time", "n/a")
    trade_files = load_trade_files()
    act = load_activity()
    detected_n = (
        int(act["detected"].sum())
        if (not act.empty and "detected" in act.columns)
        else 0
    )
    buys_n = int(act["buy"].sum()) if (not act.empty and "buy" in act.columns) else 0
    fails_n = int(act["fail"].sum()) if (not act.empty and "fail" in act.columns) else 0

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.metric("Detected", detected_n)
    with m2:
        st.metric("Buys", buys_n)
    with m3:
        st.metric("Fails", fails_n)
    with m4:
        st.metric("Tracked", len(trade_files))
    with m5:
        st.metric("Buy size", f"{trade_cfg_amt} SOL")
    with m6:
        st.metric("Max hold", f"{max_hold}s")

    st.divider()

    lk, rk = st.columns([1, 2])
    with lk:
        st.subheader("Execution")
        st.markdown(
            f"Mode {dashboard_theme.pill(mode, 'blue' if mode == 'dry_run' else 'amber')}  \n"
            f"Enabled {dashboard_theme.pill(str(enabled), 'green' if enabled else 'red')}  \n"
            f"Platform {dashboard_theme.pill(platform, 'grey')}  \n"
            f"Wallet `{wallet[:8]}…`  \n"
            f"Risk session `{risk_session}`",
            unsafe_allow_html=True,
        )
    with rk:
        st.subheader("Risk limits (lamports)")
        exec_cfg = config.get("execution", {})
        limits = pd.DataFrame(
            {
                "limit": [
                    "max trade quote",
                    "max session quote",
                    "max trade fee",
                    "max session fee",
                ],
                "value": [
                    exec_cfg.get("max_trade_quote_raw", "n/a"),
                    exec_cfg.get("max_session_quote_raw", "n/a"),
                    exec_cfg.get("max_total_fee_lamports", "n/a"),
                    exec_cfg.get("max_session_fee_lamports", "n/a"),
                ],
            }
        ).set_index("limit")
        st.dataframe(limits, width="stretch", height=150)

    st.subheader("Trade parameters")
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        st.metric("Buy amount", f"{trade_cfg.get('buy_amount', 'n/a')} SOL")
        st.metric("Buy slippage", trade_cfg.get("buy_slippage", "n/a"))
    with t2:
        st.metric("Sell slippage", trade_cfg.get("sell_slippage", "n/a"))
        st.metric("Exit", trade_cfg.get("exit_strategy", "n/a"))
    with t3:
        st.metric("Max hold", f"{max_hold}s")
        st.metric("Max sell attempts", trade_cfg.get("max_sell_attempts", "n/a"))
    with t4:
        ef = trade_cfg.get("extreme_fast_mode", False)
        st.metric("Extreme fast", "ON" if ef else "off")
        eg = config.get("entry_gate", {}).get("enabled", False)
        st.metric("Entry gate", "ON" if eg else "off")

# ─── Charts tab ──────────────────────────────────────────────────────────────

with tab_charts:
    act = load_activity()
    if act.empty:
        st.info("No activity in recent logs — start the paper bot to see live data.")
    else:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.subheader("Token activity · per minute")
            st.area_chart(act, color=dashboard_theme.CHART_COLORS[: len(act.columns)])
        with c2:
            st.subheader("Latest minutes")
            st.dataframe(act.tail(8).sort_index(ascending=False), height=260)

    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Submissions by intent · per day")
        submissions = load_ledger(wallet)
        if submissions:
            sub_df = pd.DataFrame(submissions)
            sub_df["day"] = pd.to_datetime(
                sub_df["submitted_at"], errors="coerce"
            ).dt.date
            sub_df["kind"] = sub_df["intent_id"].str.split(":").str[0]
            pivot = sub_df.groupby(["day", "kind"]).size().unstack(fill_value=0)
            st.bar_chart(
                pivot, color=dashboard_theme.CHART_COLORS[: len(pivot.columns)]
            )
        else:
            st.info("No ledger submissions")

    with c2:
        st.subheader("Watch set funding · SOL")
        watch = load_watch_set()
        snaps = {
            m[:8]: {
                "virtual": s.get("virtual_sol", 0) / 1e9,
                "real": s.get("real_sol", 0) / 1e9,
            }
            for m, s in watch.items()
            if isinstance(s, dict)
        }
        if snaps:
            st.bar_chart(pd.DataFrame(snaps).T, color=["#3b82f6", "#22c55e"])
        else:
            st.info("Watch set empty")

    st.subheader("Buy sizes · tokens per attempt")
    pos = load_positions()
    if not pos.empty:
        hist = pos["tokens"].pipe(lambda s: pd.cut(s, bins=15))
        counts = hist.value_counts().sort_index()
        counts.index = counts.index.astype(str)
        st.bar_chart(counts, color="#f59e0b")
    else:
        st.info("No buy attempts in recent logs")

# ─── Learning tab ────────────────────────────────────────────────────────────

with tab_learning:
    st.subheader("Learning journal — measurement coverage")
    lstats = load_learning_stats()
    if not lstats:
        st.info(
            "No lesson journal found (.state/learning/lessons.sqlite3). "
            "Verify the measurement path offline before starting any collector."
        )
        st.code("uv run --offline --no-sync learning-examples/verify_paper_horizons.py")
    else:
        l1, l2, l3, l4 = st.columns(4)
        with l1:
            st.metric("Lessons", lstats["total"])
        with l2:
            st.metric("Jev-scored", lstats["scored"])
        with l3:
            st.metric("Eligible live outcomes", lstats["resolved"])
        with l4:
            skips = lstats["by_kind"].get("gate_skip", 0)
            passes = lstats["by_kind"].get("gate_pass", 0)
            st.metric("Pass / skip", f"{passes} / {skips}")

        st.warning(lstats["measurement_note"])
        st.metric("Excluded legacy paper rows", lstats["excluded_legacy_paper"])
        coverage = lstats["paper_coverage"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Planned paper entries", coverage["planned_entries"])
        c2.metric("Complete paired entries", coverage["paired_entries"])
        c3.metric("Distinct paired mints", coverage["paired_mints"])
        st.caption(
            f"Entry window: {coverage['first_entry_utc'] or 'none'} to "
            f"{coverage['last_entry_utc'] or 'none'}. "
            f"Pending entries: {coverage['pending_entries']}; "
            f"entries with censoring: {coverage['censored_entries']} (can overlap). "
            "Pending is not proof that a collector is alive. Repeated mints are not "
            "independent market samples."
        )
        if not coverage["paired_entries"]:
            st.info("No complete three-horizon cohorts. No comparative conclusion yet.")
        st.download_button(
            "Download evidence report (JSON)",
            data=json.dumps(lstats, indent=2, allow_nan=False),
            file_name="learning-evidence.json",
            mime="application/json",
            on_click="ignore",
        )
        st.caption(
            "A summary of one database snapshot, not a frozen dataset, replay package, "
            "profit certification, or execution authorization."
        )
        if lstats["paper_marks"]:
            st.subheader("Gross mark returns by horizon")
            st.caption(
                "Same entry IDs at all three horizons. Return fractions, not SOL PnL. "
                "Pending and censored observations remain in the denominator."
            )
            st.dataframe(pd.DataFrame(lstats["paper_marks"]), width="stretch")
        if lstats["paper_comparisons"]:
            st.subheader("Same-entry changes versus 60 seconds")
            st.caption(
                "Differences in gross return fractions on complete three-arm entries. "
                "Fees, impact and missing exits are not priced; this is not a ranking "
                "of executable strategies."
            )
            st.dataframe(pd.DataFrame(lstats["paper_comparisons"]), width="stretch")
        if lstats["paper_censors"]:
            st.subheader("Missing observations: causes and next checks")
            st.dataframe(pd.DataFrame(lstats["paper_censors"]), width="stretch")

        if lstats["resolved"] == 0:
            st.info(
                "No eligible live outcomes. Historical paper proxies are excluded. "
                "Gross marks are separate observations, not simulated fills."
            )

        st.subheader("Whale-ride coupling — is the graduation exit alive?")
        st.caption(
            "P(graduation | crossed X) from the shadow collector's traces, "
            "against the EV break-even implied by the graduator and non-"
            "graduator arms. ACTIVE means a crossing at that level prices "
            "positive expected value in this snapshot. One reading of one "
            "collector, not a fill record."
        )
        wr_rows = []
        for key, row in load_whaleride_coupling().items():
            if row.get("state") is None and row.get("status"):
                continue
            wr_rows.append(
                {
                    "X (SOL)": key[1:],
                    "coupling": row.get("coupling"),
                    "break-even": row.get("break_even_coupling"),
                    "state": row.get("state", "—"),
                    "grad n": row.get("grad_n"),
                    "crossers": row.get("crossers"),
                }
            )
        if wr_rows:
            wrdf = pd.DataFrame(wr_rows).set_index("X (SOL)")
            st.dataframe(wrdf, width="stretch")
        else:
            st.info(
                "No shadow coupling data yet. Start the collector: "
                "keep_whaleride_shadow.py"
            )

        if lstats["pnl_by_quality"]:
            st.subheader("Live outcome associations by raw Jev quality")
            st.caption(
                "Descriptive only. Quality is an ordinal rubric, not a calibrated "
                "win probability; these associations do not establish an edge."
            )
            qdf = (
                pd.DataFrame(lstats["pnl_by_quality"])
                .rename(
                    columns={
                        "q": "jev score",
                        "n": "count",
                        "avg_pnl": "avg pnl (SOL)",
                        "total_pnl": "total pnl (SOL)",
                    }
                )
                .set_index("jev score")
            )
            st.dataframe(qdf, width="stretch")
            st.bar_chart(qdf["avg pnl (SOL)"], color="#3b82f6")

        st.subheader("Recent lessons (archive)")
        st.caption(
            "Legacy paper PnL values below are retained, but excluded from evidence."
        )
        rdf = pd.DataFrame(
            lstats["recent"],
            columns=[
                "utc",
                "kind",
                "symbol",
                "decision",
                "jev_quality",
                "jev_copycat",
                "outcome_pnl_sol",
                "outcome_reason",
            ],
        )
        st.dataframe(rdf, width="stretch", height=420)

# ─── Trades tab ──────────────────────────────────────────────────────────────

with tab_trades:
    st.subheader("Detected Tokens")
    trades = load_trade_files()
    if not trades:
        st.info("No trade files found in trades/")
    else:
        for t in trades[:DETECTED_TOKENS_LIMIT]:
            name = t.get("name", "?")
            symbol = t.get("symbol", "?")
            mint = t.get("mint", "?")
            with st.expander(f"{symbol} — {name} ({mint[:16]}...)"):
                col1, col2 = st.columns(2)
                with col1:
                    st.text(f"Mint: {mint}")
                    st.text(f"Creator: {t.get('creator', 'n/a')}")
                    if t.get("bonding_curve"):
                        st.text(f"Curve: {t['bonding_curve'][:20]}...")
                with col2:
                    vq = t.get("virtual_quote_reserves")
                    if vq:
                        st.text(f"Virtual quote: {int(vq) / 1e9:.2f} SOL")
                    rt = t.get("real_token_reserves")
                    if rt:
                        st.text(f"Real tokens: {int(rt) / 1e6:.0f}")
                if t.get("uri"):
                    st.caption(t["uri"])

    st.divider()
    st.subheader("Economic receipts")
    st.caption(
        "Read-only ledger evidence. Balance movements are not fills or realized PnL. "
        "Rent, wrapping, tips and other transfers are not separately attributed."
    )
    ledger_paths = sorted(
        path
        for path in LEDGER_DIR.glob("*.sqlite3")
        if path.is_file() and not path.is_symlink()
    )
    if not ledger_paths:
        st.info("No transaction ledgers found in .state/transaction-ledgers.")
    else:
        ledger_path = st.selectbox(
            "Evidence ledger",
            ledger_paths,
            format_func=lambda path: path.name,
        )
        try:
            evidence = load_trade_evidence(ledger_path)
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            st.error(
                "Could not audit this ledger: unreadable file, invalid data, "
                "or unsupported schema. No accounting result is available."
            )
        else:
            fees = evidence["live_network_fees"]
            balances = evidence["live_balance_coverage"]
            with st.container(horizontal=True):
                st.metric("Recorded submissions", len(evidence["transactions"]))
                st.metric("Attributable live submissions", fees["submission_count"])
                st.metric(
                    "Unattributed or invalid submissions",
                    evidence["unattributed_submission_count"],
                )
            if fees["complete_finalized_total_lamports"] is None:
                st.warning(
                    "Complete finalized network fees are unknown. Known subtotals "
                    "are partial evidence, not total session spend or zero cost."
                )
                with st.expander("Why the finalized fee total is unknown"):
                    for blocker in fees["finalized_total_blockers"]:
                        st.markdown(f"**{blocker['reason']}**")
                        st.caption("Next check: " + blocker["next_check"])
            with st.container(horizontal=True):
                st.metric(
                    "Confirmed fee subtotal (lamports)",
                    _evidence_amount(fees["confirmed_subtotal_lamports"]),
                )
                st.metric(
                    "Finalized fee subtotal (lamports)",
                    _evidence_amount(fees["finalized_subtotal_lamports"]),
                )
                st.metric(
                    "Complete finalized fees (lamports)",
                    _evidence_amount(fees["complete_finalized_total_lamports"]),
                )
            st.caption(
                f"Fees observed for {fees['observed_count']} of "
                f"{fees['submission_count']} attributable live submissions. "
                f"Native accounting unknown: {balances['native_unknown_count']}; "
                f"token accounting unknown: {balances['token_unknown_count']}. "
                "Unattributed submissions are excluded from these counts and totals."
            )
            st.download_button(
                "Download trade evidence (JSON)",
                data=json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False),
                file_name=f"trade-evidence-{ledger_path.stem}.json",
                mime="application/json",
                on_click="ignore",
            )
            st.caption(
                f"Report generated (UTC): {evidence['snapshot']['generated_utc']}. "
                "Cached for 10 seconds between reruns. The download matches this "
                "displayed snapshot, including its generation time. This local report "
                "time is not receipt time or market freshness; the snapshot is not a "
                "replay package or trading authorization."
            )
            evidence_rows = []
            for transaction in evidence["transactions"]:
                economic = transaction["economic_receipt"] or {}
                native = economic.get("native") or {}
                evidence_rows.append(
                    {
                        "Signature": transaction["signature"],
                        "Original kind": transaction["kind"],
                        "Recorded status": transaction["ledger_status"] or "Unknown",
                        "Receipt status": transaction["receipt_status"] or "Unverified",
                        "Network fee (lamports)": _evidence_amount(
                            transaction["observed_network_fee_lamports"]
                        ),
                        "Fee commitment": transaction["fee_commitment"] or "Unknown",
                        "Native change (lamports)": _evidence_amount(
                            native.get("change_lamports")
                        ),
                        "Native commitment": native.get("commitment", "Unknown"),
                    }
                )
            unlinked_events = {
                event["event_id"]: event
                for event in evidence["events"]
                if not event["submission_links"]
            }
            st.caption(
                f"{len(unlinked_events)} lifecycle observations have no linkable retained "
                "submission. They are observations, not trade counts."
            )
            if unlinked_events:
                with st.expander(
                    "Unlinked lifecycle observations", expanded=not evidence_rows
                ):
                    st.caption(
                        "A decision may stop before submission, reference an absent "
                        "record, or have quarantined claims. No link does not prove "
                        "no transaction was sent. Invalid observations cannot supply "
                        "claims; raw payloads and free-form reasons remain excluded."
                    )
                    st.dataframe(
                        [
                            {
                                "Category": event["category"] or "Unknown",
                                "Event kind": event["kind"],
                                "Checks": ", ".join(event["issues"])
                                or "No detected inconsistency",
                                "Event ID": event_id,
                            }
                            for event_id, event in unlinked_events.items()
                        ],
                        hide_index=True,
                        width="stretch",
                    )
                    inspected_event_id = st.selectbox(
                        "Inspect unlinked observation",
                        list(unlinked_events),
                        index=None,
                        placeholder="Choose an observation",
                        format_func=lambda event_id: (
                            f"{event_id[:16]} | {unlinked_events[event_id]['kind']} | "
                            f"{unlinked_events[event_id]['category'] or 'Unknown'}"
                        ),
                    )
                    if inspected_event_id is not None:
                        st.code(
                            json.dumps(
                                unlinked_events[inspected_event_id],
                                indent=2,
                                sort_keys=True,
                                allow_nan=False,
                            ),
                            language="json",
                        )
            if not evidence_rows:
                st.info("This ledger has no recorded submissions.")
            else:
                st.dataframe(evidence_rows, hide_index=True, width="stretch")
                by_signature = {
                    transaction["signature"]: transaction
                    for transaction in evidence["transactions"]
                }
                inspected_signature = st.selectbox(
                    "Inspect submission",
                    list(by_signature),
                    index=None,
                    placeholder="Choose a signature",
                    format_func=lambda signature: (
                        f"{signature[:16]} | {by_signature[signature]['kind']} | "
                        f"{by_signature[signature]['receipt_status'] or 'unverified'}"
                    ),
                )
                if inspected_signature is not None:
                    inspected = by_signature[inspected_signature]
                    st.code(inspected_signature, language=None)
                    economic = inspected["economic_receipt"]
                    issues = inspected["issues"] + (
                        economic["issues"] if economic is not None else []
                    )
                    if issues:
                        st.warning(
                            "Accounting issues: " + ", ".join(sorted(set(issues)))
                        )
                    if economic is None:
                        st.info(
                            "No attributable live economic receipt. Missing or excluded "
                            "evidence does not mean zero cost."
                        )
                    else:
                        native = economic["native"]
                        if native is None:
                            st.info("Native balance accounting is unknown.")
                        else:
                            st.table(
                                {
                                    "Before (lamports)": _evidence_amount(
                                        native["pre_lamports"]
                                    ),
                                    "After (lamports)": _evidence_amount(
                                        native["post_lamports"]
                                    ),
                                    "Change including network fee (lamports)": _evidence_amount(
                                        native["change_lamports"]
                                    ),
                                    "Change excluding network fee (lamports)": _evidence_amount(
                                        native["change_excluding_network_fee_lamports"]
                                    ),
                                    "Native commitment": native["commitment"],
                                }
                            )
                        if economic["tokens"] is None:
                            st.info("Token balance accounting is unknown.")
                        elif not economic["tokens"]:
                            st.info(
                                "No signer-owned token balances were reported for "
                                "this transaction."
                            )
                        else:
                            st.caption(
                                f"Token commitment: {economic['token_commitment']}. "
                                "Raw units, not token valuations or spendable inventory."
                            )
                            st.dataframe(
                                [
                                    {
                                        "Account": token["account"],
                                        "Mint": token["mint"],
                                        "Program": token["program_id"] or "Unknown",
                                        "Decimals": str(token["decimals"]),
                                        "Before (raw)": _evidence_amount(
                                            token["pre_amount_raw"]
                                        ),
                                        "After (raw)": _evidence_amount(
                                            token["post_amount_raw"]
                                        ),
                                        "Change (raw)": _evidence_amount(
                                            token["change_raw"]
                                        ),
                                    }
                                    for token in economic["tokens"]
                                ],
                                hide_index=True,
                                width="stretch",
                            )
                    st.markdown("#### Linked lifecycle observations")
                    st.caption(
                        "Recorded associations, ordered by identifier, not a timeline. "
                        "A decision intent may cover multiple attempts. Links do not "
                        "certify fills, promote practice evidence, or change accounting."
                    )
                    event_ids = set(inspected["event_ids"])
                    linked_events = [
                        event
                        for event in evidence["events"]
                        if event["event_id"] in event_ids
                    ]
                    if not linked_events:
                        st.info(
                            "No linkable lifecycle observations for this submission. "
                            "Missing evidence does not prove no decision was made."
                        )
                    else:
                        if any(event["issues"] for event in linked_events):
                            st.warning(
                                "Some linked observations lack supporting evidence or "
                                "contain conflicting claims. Review their checks."
                            )
                        st.dataframe(
                            [
                                {
                                    "Category": event["category"] or "Unknown",
                                    "Event kind": event["kind"],
                                    "Relation": ", ".join(
                                        relation.replace("_", " ")
                                        for link in event["submission_links"]
                                        if link["signature"] == inspected_signature
                                        for relation in link["relations"]
                                    ),
                                    "Checks": ", ".join(event["issues"])
                                    or "No detected inconsistency",
                                    "Declared action": event["declared_action"]
                                    or "Unknown",
                                    "Reported status": event["reported_status"]
                                    or "Unknown",
                                    "Reported success": str(event["reported_success"])
                                    if event["reported_success"] is not None
                                    else "Unknown",
                                    "Gate accepted": str(event["gate_accepted"])
                                    if event["gate_accepted"] is not None
                                    else "Unknown",
                                    "Event ID": event["event_id"],
                                }
                                for event in linked_events
                            ],
                            hide_index=True,
                            width="stretch",
                        )
                        with st.expander("Linked observation records"):
                            st.code(
                                json.dumps(
                                    linked_events,
                                    indent=2,
                                    sort_keys=True,
                                    allow_nan=False,
                                ),
                                language="json",
                            )
                    with st.expander("Reconciled submission record"):
                        st.code(
                            json.dumps(
                                inspected, indent=2, sort_keys=True, allow_nan=False
                            ),
                            language="json",
                        )
            with st.expander("Integrity and accounting limits"):
                st.code(
                    json.dumps(
                        {
                            "missing_evidence_tables": evidence[
                                "missing_evidence_tables"
                            ],
                            "receipt_issues": evidence["receipt_issues"],
                            "event_issues": evidence["event_issues"],
                            "orphan_outcomes": evidence["orphan_outcomes"],
                            "profiles": evidence["profiles"],
                        },
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    ),
                    language="json",
                )
                for limitation in evidence["limitations"]:
                    st.write(limitation)

# ─── LetsBonk Watch tab ──────────────────────────────────────────────────────

with tab_watch:
    st.subheader("LetsBonk Watch Set")
    watch = load_watch_set()
    if not watch:
        st.info("Watch set is empty")
    else:
        st.metric("Total Watched", len(watch))
        snapshots = {}
        ints = {}
        for m, s in watch.items():
            if isinstance(s, dict):
                snapshots[m] = s
            else:
                ints[m] = s
        col1, col2 = st.columns(2)
        with col1:
            st.metric("FUNDING Snapshots", len(snapshots))
        with col2:
            st.metric("Pending (int)", len(ints))

        if snapshots:
            st.subheader("FUNDING Snapshots")
            for m, s in snapshots.items():
                status = s.get("status", "?")
                vq = s.get("virtual_sol", 0)
                rq = s.get("real_sol", 0)
                with st.expander(f"{m[:20]}... (status={status})"):
                    st.text(f"Virtual SOL: {vq / 1e9:.2f}")
                    st.text(f"Real SOL: {rq / 1e9:.2f}")
                    st.text(f"Virtual token: {s.get('virtual_token', 'n/a')}")
                    st.text(f"Real token: {s.get('real_token', 'n/a')}")

        if ints:
            st.subheader("Pending Status Check")
            st.text(f"{len(ints)} mints awaiting first status poll")

# ─── Logs tab ────────────────────────────────────────────────────────────────

with tab_logs:
    st.subheader("Recent Log Entries")
    logs = load_recent_logs()
    if not logs:
        st.info("No logs found")
    else:
        levels = sorted({entry.get("level", "RAW") for entry in logs})
        selected = st.multiselect("Filter by level", levels, default=levels)
        filtered = [e for e in logs if e.get("level", "RAW") in selected]
        for entry in reversed(filtered[-100:]):
            level = entry.get("level", "RAW")
            msg = entry.get("message", str(entry))
            ts = entry.get("timestamp", "")
            logger_name = entry.get("logger", "")
            icon = {"ERROR": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(level, "⚪")
            st.markdown(f"{icon} `{ts}` **{level}** [{logger_name}] {msg}")

# ─── Live refresh: must be the LAST statement so the page fully renders
# ─── before the rerun fires (in-sidebar rerun aborts before tabs render).

refresh_secs = st.session_state.get("refresh_secs", 0)
if refresh_secs:
    time.sleep(refresh_secs)
    st.rerun()
