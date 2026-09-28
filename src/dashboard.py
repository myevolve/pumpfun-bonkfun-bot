"""Streamlit dashboard for the pumpfun-bonkfun-bot.

Read-only: loads from the SQLite ledger, JSON trade files, letsbonk watch
set, structured logs, and the bot YAML config. No transaction submission.

Run: uv run streamlit run src/dashboard.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

st.set_page_config(page_title="PumpFun Bot", page_icon="🎯", layout="wide")

# ─── Paths ───────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(".state/configs/live-readiness.yaml")
LEDGER_DIR = Path(".state/transaction-ledgers")
TRADES_DIR = Path("trades")
LOGS_DIR = Path("logs")
WATCH_PATH = Path(".state/letsbonk-watch.json")
POSITIONS_DIR = Path(".state/positions")


# ─── Data loaders ────────────────────────────────────────────────────────────


@st.cache_data(ttl=30)
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


@st.cache_data(ttl=30)
def load_watch_set() -> dict:
    if not WATCH_PATH.exists():
        return {}
    return json.loads(WATCH_PATH.read_text())


@st.cache_data(ttl=30)
def load_trade_files() -> list[dict]:
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
    return trades


@st.cache_data(ttl=30)
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
            "SELECT * FROM submissions ORDER BY rowid DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


@st.cache_data(ttl=30)
def load_recent_logs(n: int = 200) -> list[dict]:
    if not LOGS_DIR.exists():
        return []
    log_files = sorted(
        LOGS_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    entries = []
    for lf in log_files[:3]:
        lines = lf.read_text(errors="replace").splitlines()
        entries.extend(lines[-n:])
    entries = entries[-n:]
    parsed = []
    for line in entries:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"message": line, "level": "RAW"})
    return parsed


# ─── Dashboard ───────────────────────────────────────────────────────────────

config = load_config()
wallet = config.get("execution", {}).get("expected_wallet", "")
risk_session = config.get("execution", {}).get("risk_session_id", "n/a")
enabled = config.get("enabled", False)
mode = config.get("execution", {}).get("mode", "unknown")
platform = config.get("platform", "unknown")

st.title("🎯 PumpFun Bot Dashboard")

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
    st.caption(f"Config: {CONFIG_PATH}")

# ─── Tabs ────────────────────────────────────────────────────────────────────

tab_overview, tab_trades, tab_watch, tab_logs = st.tabs(
    ["📊 Overview", "💱 Trades", "🔍 LetsBonk Watch", "📋 Logs"]
)

# ─── Overview tab ────────────────────────────────────────────────────────────

with tab_overview:
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Enabled", str(enabled))
    with col2:
        st.metric("Mode", mode)
    with col3:
        st.metric("Platform", platform)
    with col4:
        trade_files = load_trade_files()
        st.metric("Tracked Tokens", len(trade_files))

    st.divider()

    # Risk session info
    st.subheader("Risk Session")
    wallet_safe = wallet[:8] + "..." if wallet else "n/a"
    st.text(f"Wallet: {wallet_safe}")
    st.text(f"Risk session: {risk_session}")
    exec_cfg = config.get("execution", {})
    col1, col2 = st.columns(2)
    with col1:
        st.text(f"Max trade: {exec_cfg.get('max_trade_quote_raw', 'n/a')} lamports")
        st.text(
            f"Max session quote: {exec_cfg.get('max_session_quote_raw', 'n/a')} lamports"
        )
    with col2:
        st.text(f"Max fee: {exec_cfg.get('max_total_fee_lamports', 'n/a')} lamports")
        st.text(
            f"Max session fee: {exec_cfg.get('max_session_fee_lamports', 'n/a')} lamports"
        )

    st.divider()

    # Trade parameters
    st.subheader("Trade Parameters")
    trade_cfg = config.get("trade", {})
    col1, col2, col3 = st.columns(3)
    with col1:
        st.text(f"Buy amount: {trade_cfg.get('buy_amount', 'n/a')} SOL")
        st.text(f"Slippage: {trade_cfg.get('buy_slippage', 'n/a')}")
    with col2:
        st.text(f"Exit: {trade_cfg.get('exit_strategy', 'n/a')}")
        st.text(f"Max hold: {trade_cfg.get('max_hold_time', 'n/a')}s")
    with col3:
        st.text(f"Extreme fast: {trade_cfg.get('extreme_fast_mode', False)}")
        st.text(f"Entry gate: {config.get('entry_gate', {}).get('enabled', False)}")

# ─── Trades tab ──────────────────────────────────────────────────────────────

with tab_trades:
    st.subheader("Detected Tokens")
    trades = load_trade_files()
    if not trades:
        st.info("No trade files found in trades/")
    else:
        for t in trades[:30]:
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
    st.subheader("Ledger Submissions")
    submissions = load_ledger(wallet)
    if not submissions:
        st.info("No ledger submissions found")
    else:
        for s in submissions[:20]:
            status = s.get("state", "?")
            sig = s.get("signature", "?")[:16]
            with st.expander(f"{status} — {sig}..."):
                st.text(f"State: {status}")
                st.text(f"Signature: {s.get('signature', 'n/a')}")
                st.text(f"Intent: {s.get('intent_id', 'n/a')}")
                st.text(f"Quote amount: {s.get('quote_amount_raw', 'n/a')} lamports")
                st.text(f"Fee: {s.get('fee_lamports', 'n/a')} lamports")

# ─── LetsBonk Watch tab ──────────────────────────────────────────────────────

with tab_watch:
    st.subheader("LetsBonk Watch Set")
    watch = load_watch_set()
    if not watch:
        st.info("Watch set is empty")
    else:
        st.metric("Total Watched", len(watch))
        # Separate snapshots from int entries
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

# ─── Logs tab ────────────────────────────────────────────────────────────────

with tab_logs:
    st.subheader("Recent Log Entries")
    logs = load_recent_logs()
    if not logs:
        st.info("No logs found")
    else:
        # Filter dropdown
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
