"""Onboarding wizard: step-by-step setup validation for the trading bot.

Read-only by design: no transaction signing or submission. Env secrets are
never printed or embedded in the summary -- only key presence and blankness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from dotenv import dotenv_values

from config_loader import load_bot_config, validate_platform_listener_combination
from interfaces.core import Platform
from utils.paths import state_path

# Same repo-root convention as utils/idl_manager.py (two levels up from src/).
IDL_DIR = Path(__file__).resolve().parents[1] / "idl"
LEDGER_DIR = state_path("transaction-ledgers")

RPC_TIMEOUT_SECONDS = 5.0
GEYSER_TIMEOUT_SECONDS = 5.0
HEALTH_LATENCY_LIMIT_SECONDS = 5.0

REQUIRED_ENV_KEYS = (
    "SOLANA_NODE_RPC_ENDPOINT",
    "SOLANA_NODE_WSS_ENDPOINT",
    "GEYSER_ENDPOINT",
    "GEYSER_API_TOKEN",
    "GEYSER_AUTH_TYPE",
)
# SOLANA_PRIVATE_KEY is deliberately absent: wallet-free (observation) mode is valid.

IDL_FILES = (
    "pump_fun_idl.json",
    "pump_swap_idl.json",
    "raydium_launchlab_idl.json",
)

PASS = "pass"  # noqa: S105 - status literal, not a password
WARN = "warn"
FAIL = "fail"

_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}


def _step(step: int, name: str, status: str, detail: str) -> dict[str, str]:
    return {"step": step, "name": name, "status": status, "detail": detail}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _SEVERITY[s])


def _exc_reason(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def _env_effective_value(key: str, file_values: dict[str, str | None]) -> str | None:
    """Return the effective value for a key, or None if missing everywhere.

    Only presence and blankness are inspected; values are never returned to
    callers or logged.
    """
    if key in file_values:
        raw = file_values[key]
        return raw if raw is not None else ""
    return os.environ.get(key)


def check_env_structure(env_path: Path) -> dict[str, str]:
    """Step 1: env-file structure. Falls back to process environment."""
    if env_path.is_file():
        file_values: dict[str, str | None] = dict(dotenv_values(env_path))
        source = str(env_path)
    else:
        file_values = {}
        source = "process environment (env file not found)"

    missing = [
        key
        for key in REQUIRED_ENV_KEYS
        if _env_effective_value(key, file_values) is None
    ]
    blank = [
        key
        for key in REQUIRED_ENV_KEYS
        if key not in missing
        and not (_env_effective_value(key, file_values) or "").strip()
    ]
    if missing:
        return _step(
            1,
            "Env structure",
            FAIL,
            f"checked {source}; missing required keys: {', '.join(missing)}",
        )
    if blank:
        return _step(
            1,
            "Env structure",
            FAIL,
            f"checked {source}; blank required values: {', '.join(blank)}",
        )

    notes = [f"checked {source}; all required keys present and non-blank"]
    private_key = _env_effective_value("SOLANA_PRIVATE_KEY", file_values)
    if not private_key or not private_key.strip():
        notes.append("SOLANA_PRIVATE_KEY blank; wallet-free observation mode is valid")
    return _step(1, "Env structure", PASS, "; ".join(notes))


def check_config_load(
    config_path: Path,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Step 2: bot config loads and platform/listener are compatible."""
    try:
        config = load_bot_config(config_path)
    except Exception as exc:  # noqa: BLE001 - any loader failure is a failed step
        return (
            _step(2, "Bot config load", FAIL, f"load_bot_config failed: {exc}"),
            None,
        )

    platform_str = str(config.get("platform", "pump_fun"))
    listener = str(config.get("filters", {}).get("listener_type", ""))
    try:
        platform = Platform(platform_str)
    except ValueError:
        return (
            _step(2, "Bot config load", FAIL, f"unknown platform {platform_str!r}"),
            config,
        )
    if not validate_platform_listener_combination(platform, listener):
        return (
            _step(
                2,
                "Bot config load",
                FAIL,
                f"listener {listener!r} is not compatible with platform {platform!r}",
            ),
            config,
        )
    return (
        _step(
            2,
            "Bot config load",
            PASS,
            f"loaded and validated; platform={platform}, listener={listener}",
        ),
        config,
    )


async def check_rpc_health(rpc_endpoint: str | None) -> dict[str, str]:
    """Step 3: read-only getHealth with latency under 5 seconds."""
    if not rpc_endpoint:
        return _step(3, "RPC health", FAIL, "no rpc_endpoint available from config")

    body = {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}
    timeout = aiohttp.ClientTimeout(total=RPC_TIMEOUT_SECONDS)
    started = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(rpc_endpoint, json=body) as response:
                payload = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - report any transport/parse failure
        return _step(3, "RPC health", FAIL, f"getHealth failed: {_exc_reason(exc)}")

    latency = time.perf_counter() - started
    if payload.get("result") != "ok":
        return _step(
            3,
            "RPC health",
            FAIL,
            f"getHealth returned {payload.get('result')!r} after {latency:.2f}s",
        )
    if latency >= HEALTH_LATENCY_LIMIT_SECONDS:
        return _step(3, "RPC health", FAIL, f"getHealth latency {latency:.2f}s >= 5s")
    return _step(3, "RPC health", PASS, f"getHealth ok in {latency:.2f}s")


def _endpoint_host_port(endpoint: str) -> tuple[str, int]:
    if "://" not in endpoint:
        endpoint = "http://" + endpoint
    parsed = urlparse(endpoint)
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme in {"https", "wss", "grpcs"}:
        port = 443
    else:
        port = 80
    return parsed.hostname or "", port


async def check_geyser_reachability(geyser_endpoint: str | None) -> dict[str, str]:
    """Step 4: TCP reachability of the geyser endpoint (not a stream test)."""
    if not geyser_endpoint:
        return _step(
            4,
            "Geyser reachability",
            FAIL,
            "no geyser endpoint available from config",
        )

    try:
        host, port = _endpoint_host_port(geyser_endpoint)
    except ValueError as exc:
        return _step(4, "Geyser reachability", FAIL, f"invalid endpoint: {exc}")
    if not host:
        return _step(
            4, "Geyser reachability", FAIL, f"no host in endpoint {geyser_endpoint!r}"
        )

    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=GEYSER_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - report any connect failure
        reason = _exc_reason(exc)
        return _step(
            4,
            "Geyser reachability",
            FAIL,
            f"TCP connect to {host}:{port} failed: {reason}",
        )
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001,S110 - close errors do not affect reachability
        pass
    return _step(
        4, "Geyser reachability", PASS, f"TCP connect to {host}:{port} succeeded"
    )


def check_execution_safety(config: dict[str, Any] | None) -> dict[str, str]:
    """Step 5: enabled flag, execution mode, expected_wallet presence."""
    if config is None:
        return _step(
            5,
            "Execution safety",
            FAIL,
            "no config loaded; cannot check execution safety",
        )

    execution = config.get("execution") or {}
    mode = str(execution.get("mode", "dry_run"))
    expected_wallet = execution.get("expected_wallet")
    enabled = bool(config.get("enabled", False))

    statuses: list[str] = []
    notes: list[str] = []
    if enabled:
        statuses.append(WARN)
        notes.append("bot is enabled; it will run when the runner starts")
    else:
        statuses.append(PASS)
        notes.append("bot is disabled")

    if mode == "live":
        if not expected_wallet:
            statuses.append(FAIL)
            notes.append("live mode requires execution.expected_wallet")
        else:
            statuses.append(WARN)
            notes.append("live mode; verify risk session id and caps before running")
    else:
        statuses.append(PASS)
        notes.append(f"mode={mode}")
        if not expected_wallet:
            statuses.append(WARN)
            notes.append("expected_wallet blank; fine for dry-run observation")

    return _step(5, "Execution safety", _worst(statuses), "; ".join(notes))


def check_idl_files() -> dict[str, str]:
    """Step 6: IDL files exist and parse as JSON."""
    broken: list[str] = []
    for name in IDL_FILES:
        path = IDL_DIR / name
        if not path.is_file():
            broken.append(f"{name} missing")
            continue
        try:
            with path.open(encoding="utf-8") as idl_file:
                json.load(idl_file)
        except (OSError, json.JSONDecodeError) as exc:
            broken.append(f"{name} unreadable/invalid: {exc}")
    if broken:
        return _step(6, "IDL files", FAIL, "; ".join(broken))
    return _step(
        6, "IDL files", PASS, f"all {len(IDL_FILES)} IDL files parse ({IDL_DIR})"
    )


def check_ledger_dir(ledger_dir: Path) -> dict[str, str]:
    """Step 7: ledger directory is writable."""
    try:
        ledger_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=ledger_dir, prefix=".onboard-probe-", delete=True
        ):
            pass
    except OSError as exc:
        return _step(7, "Ledger directory", FAIL, f"{ledger_dir} not writable: {exc}")
    return _step(7, "Ledger directory", PASS, f"{ledger_dir} is writable")


async def run_onboarding(
    config_path: Path, env_path: Path | None = None
) -> dict[str, Any]:
    """Run all onboarding checks in order and return a summary dict."""
    config_path = Path(config_path)
    env_path = Path(env_path) if env_path is not None else Path(".env")

    steps: list[dict[str, str]] = [check_env_structure(env_path)]

    step2, config = check_config_load(config_path)
    steps.append(step2)

    geyser = (config or {}).get("geyser") or {}
    steps.append(await check_rpc_health((config or {}).get("rpc_endpoint")))
    steps.append(await check_geyser_reachability(geyser.get("endpoint")))
    steps.append(check_execution_safety(config))
    steps.append(check_idl_files())
    steps.append(check_ledger_dir(LEDGER_DIR))

    return {
        "config": str(config_path),
        "env_file": str(env_path),
        "steps": steps,
        "passed": all(result["status"] != FAIL for result in steps),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: prints a JSON summary; exit 0 iff no step failed."""
    parser = argparse.ArgumentParser(
        description="Onboarding wizard: validate bot setup step by step (read-only).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("bots/bot-sniper-1-geyser.yaml"),
        help="Path to the bot YAML config (default: bots/bot-sniper-1-geyser.yaml)",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        type=Path,
        default=Path(".env"),
        help="Path to the env file (default: .env; falls back to process environment)",
    )
    args = parser.parse_args(argv)

    summary = asyncio.run(run_onboarding(args.config, args.env_file))
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
