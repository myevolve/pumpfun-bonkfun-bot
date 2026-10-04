# Operator-facing validation must retain the exact rejected invariant.
# ruff: noqa: TRY003
import argparse
import asyncio
import json
import logging
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from solders.pubkey import Pubkey

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        logging.info("Using winloop event loop policy for improved performance")
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logging.info("Using uvloop event loop policy for improved performance")
except ImportError:
    logging.info(
        "Using standard asyncio event loop (install uvloop/winloop for better performance)"
    )

from config_loader import (
    get_platform_from_config,
    get_supported_listeners_for_platform,
    load_bot_config,
    print_config_summary,
    validate_platform_listener_combination,
)
from core.client import estimate_transaction_fee_lamports
from core.execution_policy import (
    ExecutionBlocked,
    ExecutionMode,
    ExecutionPolicy,
)
from core.pubkeys import (
    WSOL_MINT,
    get_quote_asset,
    resolve_quote_amounts,
    resolve_quote_mint,
)
from core.transaction_ledger import (
    SessionRiskTotals,
    TransactionLedger,
    resolve_transaction_ledger_path,
)
from interfaces.core import Platform
from monitoring.trade_flow import FlowRules, GateRules
from platforms import platform_factory
from trading.position import Position
from trading.universal_trader import (
    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
    DEFAULT_PRICE_READ_OUTAGE_BUDGET,
    UniversalTrader,
)
from utils.logger import setup_file_logging
from utils.paths import state_path

PROCESS_POLL_INTERVAL_SECONDS = 0.2
PROCESS_SHUTDOWN_GRACE_SECONDS = 5.0
PREFLIGHT_RENT_ACCOUNT_SIZE = 512
PROCESS_TERMINATE_GRACE_SECONDS = 2.0
PROCESS_KILL_GRACE_SECONDS = 1.0
DEFAULT_PRIORITY_FEE_HARD_CAP = 500_000


def setup_logging(bot_name: str) -> None:
    """Set up logging to file for a specific bot instance."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = log_dir / f"{bot_name}_{timestamp}.log"

    setup_file_logging(str(log_filename))


def build_execution_policy(
    config: dict,
    *,
    authorize_live: bool = False,
) -> ExecutionPolicy:
    """Build a policy and require an explicit runtime grant for live mode."""
    policy = ExecutionPolicy.from_config(config)
    if policy.mode is ExecutionMode.LIVE and not authorize_live:
        raise ExecutionBlocked(
            "Live execution requires explicit runtime authorization "
            "with --authorize-live"
        )
    if authorize_live:
        policy = policy.authorize_live()
    return policy


def read_bot_status(  # noqa: C901, PLR0912, PLR0915
    config_path: str | Path,
) -> dict[str, object]:
    """Read and validate one bot's durable recovery status without a signer."""

    cfg = load_bot_config(config_path)
    policy = ExecutionPolicy.from_config(cfg)
    if policy.expected_wallet is None:
        raise ValueError("Status requires execution.expected_wallet")
    platform = get_platform_from_config(cfg)
    journal_path = state_path(
        "positions", f"{policy.expected_wallet}-{platform.value}.json"
    )
    ledger_path = resolve_transaction_ledger_path(policy.expected_wallet)
    base_status: dict[str, object] = {
        "bot": cfg["name"],
        "wallet": policy.expected_wallet,
        "platform": platform.value,
        "journal_path": str(journal_path),
        "transaction_ledger_path": str(ledger_path),
        "active_position_count": 0,
        "active_positions": [],
        "unresolved_buy_count": 0,
        "unresolved_buys": [],
        "pending_token_count": 0,
        "pending_tokens": [],
        "active_submissions": [],
        "provisional_outcomes": [],
        "pending_cleanups": [],
    }
    cleanup_journal = state_path("cleanup", f"{policy.expected_wallet}.json")
    if cleanup_journal.exists():
        cleanup_payload = json.loads(cleanup_journal.read_text(encoding="utf-8"))
        if (
            not isinstance(cleanup_payload, dict)
            or cleanup_payload.get("wallet") != policy.expected_wallet
            or not isinstance(cleanup_payload.get("entries"), dict)
        ):
            raise ValueError("Cleanup journal is invalid or belongs to another wallet")
        # The journal only ever holds unresolved/failed cleanups: all pending work.
        base_status["pending_cleanups"] = sorted(
            (
                {
                    "mint": str(entry.get("mint")),
                    "status": str(entry.get("status")),
                    "tx_signature": entry.get("tx_signature"),
                }
                for entry in cleanup_payload["entries"].values()
                if isinstance(entry, dict)
            ),
            key=lambda item: item["mint"],
        )
    if policy.mode is ExecutionMode.LIVE:
        risk_session_id, max_session_quote_raw, max_session_fee_lamports = (
            policy.session_risk_limits()
        )
        risk_totals = SessionRiskTotals({}, 0, 0)
        if ledger_path.exists():
            with TransactionLedger(ledger_path) as ledger:
                risk_totals = ledger.get_session_risk_totals(
                    risk_session_id,
                    policy.expected_wallet,
                )
                base_status["active_submissions"] = ledger.list_nonterminal_submissions(
                    policy.expected_wallet
                )
                base_status["provisional_outcomes"] = ledger.list_provisional_outcomes(
                    policy.expected_wallet
                )
        base_status["risk_session"] = {
            "id": risk_session_id,
            "reserved_quote_raw_by_mint": risk_totals.quote_amount_raw_by_mint,
            "max_quote_raw_per_mint": max_session_quote_raw,
            "remaining_quote_raw_by_mint": {
                mint: max(0, max_session_quote_raw - amount)
                for mint, amount in risk_totals.quote_amount_raw_by_mint.items()
            },
            "reserved_fee_lamports": risk_totals.fee_lamports,
            "max_fee_lamports": max_session_fee_lamports,
            "remaining_fee_lamports": max(
                0, max_session_fee_lamports - risk_totals.fee_lamports
            ),
            "submission_count": risk_totals.submission_count,
        }
    if not journal_path.exists():
        return base_status

    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Recovery journal has an invalid version")
    if payload.get("wallet") != policy.expected_wallet:
        raise ValueError("Recovery journal belongs to a different wallet")
    if payload.get("platform") != platform.value:
        raise ValueError("Recovery journal belongs to a different platform")
    position_records = payload.get("positions", {})
    unresolved_records = payload.get("unresolved_buys", {})
    pending_records = payload.get("pending_tokens", [])
    if not isinstance(position_records, dict):
        raise TypeError("Recovery journal positions must be an object")
    if not isinstance(unresolved_records, dict):
        raise TypeError("Recovery journal unresolved_buys must be an object")
    if not isinstance(pending_records, list):
        raise TypeError("Recovery journal pending_tokens must be an array")

    active_positions: list[dict[str, object]] = []
    for token_key, record in position_records.items():
        if not isinstance(record, dict):
            raise TypeError("Recovery journal position record must be an object")
        token = UniversalTrader.token_from_dict(record["token"])
        position = Position.from_dict(record["position"])
        if (
            str(token.mint) != token_key
            or position.mint != token.mint
            or token.platform is not platform
        ):
            raise ValueError("Recovery journal position identity is inconsistent")
        if position.is_active:
            active_positions.append(
                {
                    "mint": token_key,
                    "symbol": token.symbol,
                    "quantity_raw": position.quantity_raw,
                    "entry_price": position.entry_price,
                    "pending_exit_signature": position.pending_exit_signature,
                    "automatic_exit": UniversalTrader.has_automatic_exit(position),
                }
            )

    unresolved_buys: list[dict[str, object]] = []
    for token_key, record in unresolved_records.items():
        if not isinstance(record, dict):
            raise TypeError("Recovery journal unresolved record must be an object")
        token = UniversalTrader.token_from_dict(record["token"])
        signature = record.get("signature")
        if (
            str(token.mint) != token_key
            or token.platform is not platform
            or not isinstance(signature, str)
            or not signature
        ):
            raise ValueError("Recovery journal unresolved buy is inconsistent")
        unresolved_buys.append(
            {
                "mint": token_key,
                "symbol": token.symbol,
                "signature": signature,
            }
        )

    pending_tokens: list[dict[str, str]] = []
    for record in pending_records:
        token = UniversalTrader.token_from_dict(record)
        if token.platform is not platform:
            raise ValueError("Recovery journal pending token platform is inconsistent")
        pending_tokens.append({"mint": str(token.mint), "symbol": token.symbol})

    active_positions.sort(key=lambda item: str(item["mint"]))
    unresolved_buys.sort(key=lambda item: str(item["mint"]))
    pending_tokens.sort(key=lambda item: item["mint"])
    base_status.update(
        {
            "active_position_count": len(active_positions),
            "active_positions": active_positions,
            "unresolved_buy_count": len(unresolved_buys),
            "unresolved_buys": unresolved_buys,
            "pending_token_count": len(pending_tokens),
            "pending_tokens": pending_tokens,
        }
    )
    return base_status


def _flow_rules_from_config(cfg: dict) -> FlowRules | None:
    """Translate the validated ``flow_exit`` section into rules, or None when off."""
    section = cfg.get("flow_exit")
    if not section or not section.get("enabled", False):
        return None
    return FlowRules(
        creator_sell=section.get("creator_sell", True),
        trailing_stop=section.get("trailing_stop"),
        single_sell_pct=section.get("single_sell_pct"),
        net_outflow_pct=section.get("net_outflow_pct"),
        window=section.get("window", 5),
    )


def _gate_rules_from_config(cfg: dict) -> GateRules | None:
    """Translate the validated ``entry_gate`` section into rules, or None when off."""
    section = cfg.get("entry_gate")
    if not section or not section.get("enabled", False):
        return None
    defaults = GateRules()
    return GateRules(
        mayhem_only=section.get("mayhem_only", defaults.mayhem_only),
        min_buyers=section.get("min_buyers", defaults.min_buyers),
        max_real_sol=section.get("max_real_sol", defaults.max_real_sol),
        min_real_sol=section.get("min_real_sol", defaults.min_real_sol),
        require_creator_holding=section.get(
            "require_creator_holding", defaults.require_creator_holding
        ),
        max_wait_slots=section.get("max_wait_slots", defaults.max_wait_slots),
        max_wait_ms=section.get("max_wait_ms", defaults.max_wait_ms),
    )


def _create_trader(
    cfg: dict,
    policy: ExecutionPolicy,
    platform: Platform,
) -> UniversalTrader:
    """Construct one trader from validated config without starting it."""

    return UniversalTrader(
        rpc_endpoint=cfg["rpc_endpoint"],
        wss_endpoint=cfg["wss_endpoint"],
        private_key=cfg["private_key"],
        platform=platform,
        buy_amount=cfg["trade"]["buy_amount"],
        buy_slippage=cfg["trade"]["buy_slippage"],
        sell_slippage=cfg["trade"]["sell_slippage"],
        extreme_fast_mode=cfg["trade"].get("extreme_fast_mode", False),
        extreme_fast_token_amount=cfg["trade"].get("extreme_fast_token_amount", 30),
        curve_refresh_budget=cfg["trade"].get("curve_refresh_budget", 2.0),
        trust_create_event=cfg["trade"].get("trust_create_event", True),
        quote_amounts=cfg["trade"].get("quote_amounts"),
        allowed_quote_mints=cfg["filters"].get("allowed_quote_mints"),
        exit_strategy=cfg["trade"].get("exit_strategy", "time_based"),
        take_profit_percentage=cfg["trade"].get("take_profit_percentage"),
        stop_loss_percentage=cfg["trade"].get("stop_loss_percentage"),
        max_hold_time=cfg["trade"].get("max_hold_time"),
        price_check_interval=cfg["trade"].get("price_check_interval", 10),
        max_exit_sell_attempts=cfg["trade"].get(
            "max_exit_sell_attempts", DEFAULT_MAX_EXIT_SELL_ATTEMPTS
        ),
        price_read_outage_budget=cfg["trade"].get(
            "price_read_outage_budget", DEFAULT_PRICE_READ_OUTAGE_BUDGET
        ),
        listener_type=cfg["filters"]["listener_type"],
        geyser_endpoint=cfg.get("geyser", {}).get("endpoint"),
        geyser_api_token=cfg.get("geyser", {}).get("api_token"),
        geyser_auth_type=cfg.get("geyser", {}).get("auth_type", "x-token"),
        flow_rules=_flow_rules_from_config(cfg),
        gate_rules=_gate_rules_from_config(cfg),
        pumpportal_url=cfg.get("pumpportal", {}).get(
            "url", "wss://pumpportal.fun/api/data"
        ),
        enable_dynamic_priority_fee=cfg.get("priority_fees", {}).get(
            "enable_dynamic", False
        ),
        enable_fixed_priority_fee=cfg.get("priority_fees", {}).get(
            "enable_fixed", True
        ),
        fixed_priority_fee=cfg.get("priority_fees", {}).get("fixed_amount", 500000),
        extra_priority_fee=cfg.get("priority_fees", {}).get("extra_percentage", 0.0),
        hard_cap_prior_fee=cfg.get("priority_fees", {}).get(
            "hard_cap", DEFAULT_PRIORITY_FEE_HARD_CAP
        ),
        max_retries=cfg.get("retries", {}).get("max_attempts", 1),
        wait_time_after_creation=cfg.get("retries", {}).get("wait_after_creation", 15),
        wait_time_after_buy=cfg.get("retries", {}).get("wait_after_buy", 15),
        wait_time_before_new_token=cfg.get("retries", {}).get(
            "wait_before_new_token", 15
        ),
        max_token_age=cfg.get("filters", {}).get("max_token_age", 0.001),
        token_wait_timeout=cfg.get("timing", {}).get("token_wait_timeout", 120),
        cleanup_mode=cfg.get("cleanup", {}).get("mode", "disabled"),
        cleanup_force_close_with_burn=cfg.get("cleanup", {}).get(
            "force_close_with_burn", False
        ),
        cleanup_with_priority_fee=cfg.get("cleanup", {}).get(
            "with_priority_fee", False
        ),
        match_string=cfg["filters"].get("match_string"),
        bro_address=cfg["filters"].get("bro_address"),
        marry_mode=cfg["filters"].get("marry_mode", False),
        yolo_mode=cfg["filters"].get("yolo_mode", False),
        compute_units=cfg.get("compute_units", {}),
        max_rps=cfg.get("node", {}).get("max_rps", 25),
        execution_policy=policy,
    )


async def start_bot(
    config_path: str | Path,
    *,
    authorize_live: bool = False,
    resume_only: bool = False,
) -> None:
    """Start one validated bot, failing before signer creation when unauthorized."""
    cfg = load_bot_config(config_path)
    if not cfg["enabled"]:
        raise RuntimeError(f"Bot '{cfg['name']}' is disabled")
    policy = build_execution_policy(cfg, authorize_live=authorize_live)
    setup_logging(cfg["name"])
    print_config_summary(cfg)

    platform = get_platform_from_config(cfg)
    logging.info("Detected platform: %s", platform.value)

    if not platform_factory.registry.is_platform_supported(platform):
        raise ValueError(
            f"Platform {platform.value} is not supported. Available platforms: "
            f"{[p.value for p in platform_factory.get_supported_platforms()]}"
        )

    listener_type = cfg["filters"]["listener_type"]
    if not validate_platform_listener_combination(platform, listener_type):
        supported = get_supported_listeners_for_platform(platform)
        raise ValueError(
            f"Listener '{listener_type}' is not compatible with platform "
            f"'{platform.value}'. Supported listeners: {supported}"
        )

    try:
        trader = _create_trader(cfg, policy, platform)
        await trader.start(resume_only=resume_only)

    except Exception:
        logging.exception("Failed to initialize or start trader")
        raise


async def run_emergency_exit(
    config_path: str | Path,
    mint: str,
) -> dict[str, object]:
    """Exit one durable position under an explicitly authorized live policy."""
    cfg = load_bot_config(config_path)
    policy = build_execution_policy(cfg, authorize_live=True)
    setup_logging(cfg["name"])
    platform = get_platform_from_config(cfg)

    if not platform_factory.registry.is_platform_supported(platform):
        raise ValueError(f"Platform {platform.value} is not supported")
    try:
        mint_key = Pubkey.from_string(mint)
    except ValueError as exc:
        raise ValueError("--emergency-exit requires a valid Solana mint") from exc
    trader = _create_trader(cfg, policy, platform)
    result = await trader.emergency_exit(mint_key)
    return result.to_dict()


def _configured_quote_budgets_raw(
    cfg: dict,
) -> dict[Pubkey, int]:
    """Return each tradable quote mint's configured worst-case buy debit."""
    trade = cfg["trade"]
    quote_amounts = {
        WSOL_MINT: float(trade["buy_amount"]),
        **resolve_quote_amounts(trade.get("quote_amounts")),
    }
    allowed_values = cfg.get("filters", {}).get("allowed_quote_mints")
    if allowed_values is not None:
        allowed = {resolve_quote_mint(value) for value in allowed_values}
        quote_amounts = {
            mint: amount for mint, amount in quote_amounts.items() if mint in allowed
        }
    slippage_bps = int(
        (Decimal(str(trade["buy_slippage"])) * 10_000).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    budgets: dict[Pubkey, int] = {}
    for mint, amount in quote_amounts.items():
        asset = get_quote_asset(mint)
        raw_amount = int(
            (Decimal(str(amount)) * (10**asset.decimals)).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        if raw_amount <= 0:
            raise ValueError(f"Configured amount for {mint} is below one raw unit")
        budgets[mint] = (raw_amount * (10_000 + slippage_bps) + 9_999) // 10_000
    if not budgets:
        raise ValueError("No allowed quote mint has a configured buy amount")
    return budgets


async def run_live_preflight(  # noqa: C901, PLR0912, PLR0915
    config_path: str | Path,
    *,
    resume_only: bool = False,
) -> dict[str, object]:
    """Check one live configuration without authorizing or submitting a wire."""
    cfg = load_bot_config(config_path)
    policy = ExecutionPolicy.from_config(cfg)
    if policy.mode is not ExecutionMode.LIVE:
        raise ExecutionBlocked("--preflight requires execution.mode='live'")
    platform = get_platform_from_config(cfg)
    status = read_bot_status(config_path)
    checks: list[dict[str, object]] = [
        {
            "name": "configuration",
            "ok": True,
            "detail": "strict live configuration loaded",
        },
        {
            "name": "durable_state",
            "ok": True,
            "detail": {
                "active_positions": status["active_position_count"],
                "unresolved_buys": status["unresolved_buy_count"],
                "pending_tokens": status["pending_token_count"],
            },
        },
    ]
    if resume_only:
        active_positions = status.get("active_positions")
        active_count = int(status["active_position_count"])
        positions_are_monitorable = (
            isinstance(active_positions, list)
            and len(active_positions) == active_count
            and all(
                isinstance(position, dict) and position.get("automatic_exit") is True
                for position in active_positions
            )
        )
        resume_state_ok = (
            active_count > 0
            and int(status["unresolved_buy_count"]) == 0
            and int(status["pending_token_count"]) == 0
            and positions_are_monitorable
        )
        checks.append(
            {
                "name": "resume_only_state",
                "ok": resume_state_ok,
                "detail": (
                    "requires monitorable active positions and no pending buy work"
                ),
            }
        )

    quote_budgets: dict[Pubkey, int] = {}
    if not resume_only:
        try:
            quote_budgets = _configured_quote_budgets_raw(cfg)
        except (TypeError, ValueError) as exc:
            checks.append(
                {
                    "name": "quote_configuration",
                    "ok": False,
                    "detail": str(exc),
                }
            )

    try:
        trader = _create_trader(cfg, policy, platform)
    except Exception as exc:  # noqa: BLE001
        checks.append(
            {
                "name": "signer_and_state_lock",
                "ok": False,
                "detail": f"{type(exc).__name__}: initialization failed",
            }
        )
        return {
            "ready": False,
            "bot": cfg["name"],
            "wallet": policy.expected_wallet,
            "platform": platform.value,
            "checks": checks,
            "status": status,
            "resume_only": resume_only,
        }

    try:
        checks.append(
            {
                "name": "signer_and_state_lock",
                "ok": str(trader.wallet.pubkey) == policy.expected_wallet,
                "detail": "signer matches expected wallet and state lock is held",
            }
        )

        try:
            health = await trader.solana_client.get_health()
            checks.append(
                {
                    "name": "rpc_health",
                    "ok": health == "ok",
                    "detail": health,
                }
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "name": "rpc_health",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: health check failed",
                }
            )

        native_balance: int | None = None
        rent_buffer: int | None = 0 if resume_only else None
        try:
            native_balance = await trader.solana_client.get_native_balance(
                trader.wallet.pubkey
            )
            if not resume_only:
                rent_buffer = (
                    await trader.solana_client.get_minimum_balance_for_rent_exemption(
                        PREFLIGHT_RENT_ACCOUNT_SIZE
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "name": "native_balance",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: balance check failed",
                }
            )

        risk_session = status["risk_session"]
        if not isinstance(risk_session, dict):
            raise TypeError("Status returned invalid session-risk data")
        reserved_fees = int(risk_session["reserved_fee_lamports"])
        _, max_session_quote, max_session_fees = policy.session_risk_limits()
        max_transaction_fees = policy.max_total_fee_lamports
        max_trade_quote = policy.max_trade_quote_raw
        if max_transaction_fees is None or max_trade_quote is None:
            raise ExecutionBlocked("Live transaction limits are incomplete")
        remaining_fees = max(0, max_session_fees - reserved_fees)
        required_fee_lamports = max_transaction_fees
        fee_detail: dict[str, object] = {
            "remaining_lamports": remaining_fees,
            "required_next_transaction_lamports": max_transaction_fees,
        }
        if resume_only:
            active_count = int(status["active_position_count"])
            exit_attempts = int(
                cfg.get("trade", {}).get(
                    "max_exit_sell_attempts",
                    DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
                )
            )
            cleanup_cfg = cfg.get("cleanup", {})
            cleanup_fee_lamports = 0
            if cleanup_cfg.get("mode") in {"after_sell", "post_session"}:
                cleanup_priority_fee = (
                    int(
                        cfg.get("priority_fees", {}).get(
                            "hard_cap", DEFAULT_PRIORITY_FEE_HARD_CAP
                        )
                    )
                    if cleanup_cfg.get("with_priority_fee", False)
                    else 0
                )
                cleanup_fee_lamports = estimate_transaction_fee_lamports(
                    cleanup_priority_fee,
                    None,
                )
            required_fee_lamports = active_count * (
                max_transaction_fees * exit_attempts + cleanup_fee_lamports
            )
            fee_detail.update(
                {
                    "active_positions": active_count,
                    "exit_attempts_per_position": exit_attempts,
                    "cleanup_fee_per_position_lamports": cleanup_fee_lamports,
                    "required_recovery_lamports": required_fee_lamports,
                }
            )
        checks.append(
            {
                "name": "session_fee_budget",
                "ok": remaining_fees >= required_fee_lamports,
                "detail": fee_detail,
            }
        )

        if resume_only:
            checks.append(
                {
                    "name": "session_quote_budget",
                    "ok": True,
                    "detail": "not required; resume-only mode starts no token listener",
                }
            )
        else:
            reserved_by_mint = risk_session["reserved_quote_raw_by_mint"]
            if not isinstance(reserved_by_mint, dict):
                raise TypeError("Status returned invalid quote-risk totals")
            quote_budget_ok = bool(quote_budgets)
            quote_budget_details: dict[str, object] = {}
            for mint, required_raw in quote_budgets.items():
                mint_text = str(mint)
                reserved_raw = int(reserved_by_mint.get(mint_text, 0))
                remaining_raw = max(0, max_session_quote - reserved_raw)
                mint_ok = (
                    required_raw <= max_trade_quote and required_raw <= remaining_raw
                )
                quote_budget_ok = quote_budget_ok and mint_ok
                quote_budget_details[mint_text] = {
                    "required_raw": required_raw,
                    "per_trade_limit_raw": max_trade_quote,
                    "remaining_session_raw": remaining_raw,
                    "ok": mint_ok,
                }
            checks.append(
                {
                    "name": "session_quote_budget",
                    "ok": quote_budget_ok,
                    "detail": quote_budget_details,
                }
            )

        token_balance_ok = True
        token_balance_details: dict[str, object] = {}
        for mint, required_raw in quote_budgets.items():
            if mint == WSOL_MINT:
                continue
            asset = get_quote_asset(mint)
            ata = trader.wallet.get_associated_token_address(
                mint,
                asset.token_program,
            )
            try:
                balance_raw = await trader.solana_client.get_token_account_balance(ata)
                mint_ok = balance_raw >= required_raw
                token_balance_details[str(mint)] = {
                    "balance_raw": balance_raw,
                    "required_raw": required_raw,
                    "ok": mint_ok,
                }
                token_balance_ok = token_balance_ok and mint_ok
            except Exception as exc:  # noqa: BLE001
                token_balance_details[str(mint)] = {
                    "error": f"{type(exc).__name__}: balance check failed",
                    "ok": False,
                }
                token_balance_ok = False
        checks.append(
            {
                "name": "token_quote_balances",
                "ok": token_balance_ok,
                "detail": token_balance_details,
            }
        )

        if native_balance is not None and rent_buffer is not None:
            required_native = required_fee_lamports + rent_buffer
            required_native += quote_budgets.get(WSOL_MINT, 0)
            checks.append(
                {
                    "name": "native_balance",
                    "ok": native_balance >= required_native,
                    "detail": {
                        "balance_lamports": native_balance,
                        "required_lamports": required_native,
                        "rent_buffer_account_size": (
                            0 if resume_only else PREFLIGHT_RENT_ACCOUNT_SIZE
                        ),
                    },
                }
            )

        curve_manager = trader.platform_implementations.curve_manager
        prepare_live_execution = getattr(
            curve_manager,
            "prepare_live_execution",
            None,
        )
        if callable(prepare_live_execution):
            try:
                await prepare_live_execution()
                checks.append(
                    {
                        "name": "fee_attestation",
                        "ok": True,
                        "detail": "live fee state attested",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    {
                        "name": "fee_attestation",
                        "ok": False,
                        "detail": f"{type(exc).__name__}: attestation failed",
                    }
                )
        else:
            checks.append(
                {
                    "name": "fee_attestation",
                    "ok": True,
                    "detail": "platform has no separate fee attestation",
                }
            )
    finally:
        await trader.close()

    return {
        "ready": all(bool(check["ok"]) for check in checks),
        "bot": cfg["name"],
        "wallet": policy.expected_wallet,
        "platform": platform.value,
        "checks": checks,
        "resume_only": resume_only,
        "status": status,
    }


async def _run_bot_process(
    config_path: str | Path,
    *,
    authorize_live: bool = False,
    resume_only: bool = False,
) -> int | None:
    """Run one bot and translate child signals into cancellable shutdown."""
    shutdown_signal: list[int | None] = [None]
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Bot process has no active asyncio task")
    previous_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, _frame: object) -> None:
        if shutdown_signal[0] is None:
            shutdown_signal[0] = signum
        loop.call_soon_threadsafe(task.cancel)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    try:
        await start_bot(
            config_path,
            authorize_live=authorize_live,
            resume_only=resume_only,
        )
    except asyncio.CancelledError:
        if shutdown_signal[0] is None:
            raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return shutdown_signal[0]


def run_bot_process(config_path: str | Path) -> None:
    """Run a bot in a child process with nonzero signal termination status."""
    shutdown_signal = asyncio.run(_run_bot_process(config_path))
    if shutdown_signal is not None:
        raise SystemExit(128 + shutdown_signal)


def _alive_processes(
    processes: list[tuple[multiprocessing.Process, str]],
) -> list[tuple[multiprocessing.Process, str]]:
    return [(process, name) for process, name in processes if process.is_alive()]


def _join_processes(
    processes: list[tuple[multiprocessing.Process, str]],
    timeout: float,
) -> list[tuple[multiprocessing.Process, str]]:
    """Join children only in bounded increments and return any survivors."""
    deadline = time.monotonic() + max(timeout, 0.0)
    alive = _alive_processes(processes)
    while alive:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for process, _ in alive:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            process.join(timeout=min(PROCESS_POLL_INTERVAL_SECONDS, remaining))
        alive = _alive_processes(processes)

    for process, _ in processes:
        if not process.is_alive():
            process.join(timeout=0)
    return _alive_processes(processes)


def _forward_signal(
    processes: list[tuple[multiprocessing.Process, str]],
    signum: int,
) -> None:
    for process, bot_name in _alive_processes(processes):
        pid = process.pid
        if pid is None:
            continue
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            continue
        except OSError:
            logging.exception(
                "Failed to propagate signal %s to process %s for bot '%s'",
                signum,
                process.name,
                bot_name,
            )


def _stop_processes(
    processes: list[tuple[multiprocessing.Process, str]],
    signum: int,
) -> list[tuple[multiprocessing.Process, str]]:
    """Request shutdown, then terminate and kill any stubborn children."""
    _forward_signal(processes, signum)
    alive = _join_processes(processes, PROCESS_SHUTDOWN_GRACE_SECONDS)
    for process, bot_name in alive:
        logging.warning(
            "Terminating unresponsive process %s for bot '%s'",
            process.name,
            bot_name,
        )
        try:
            process.terminate()
        except ProcessLookupError:
            continue

    alive = _join_processes(processes, PROCESS_TERMINATE_GRACE_SECONDS)
    for process, bot_name in alive:
        logging.error(
            "Killing unresponsive process %s for bot '%s'",
            process.name,
            bot_name,
        )
        try:
            process.kill()
        except ProcessLookupError:
            continue

    return _join_processes(processes, PROCESS_KILL_GRACE_SECONDS)


@contextmanager
def _supervisor_signal_handlers(
    processes: list[tuple[multiprocessing.Process, str]],
) -> Iterator[list[int | None]]:
    shutdown_signal: list[int | None] = [None]
    previous_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, _frame: object) -> None:
        if shutdown_signal[0] is None:
            shutdown_signal[0] = signum

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    try:
        yield shutdown_signal
    finally:
        alive = _alive_processes(processes)
        if alive:
            cleanup_signal = shutdown_signal[0] or signal.SIGTERM
            survivors = _stop_processes(processes, cleanup_signal)
            for process, bot_name in survivors:
                logging.critical(
                    "Process %s for bot '%s' survived kill escalation",
                    process.name,
                    bot_name,
                )
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _supervise_processes(
    processes: list[tuple[multiprocessing.Process, str]],
    shutdown_signal: list[int | None],
) -> int:
    """Watch children and fail closed on signals or any child failure."""
    observed: set[int] = set()
    child_failed = False

    while True:
        for process, bot_name in processes:
            if process.exitcode is None or id(process) in observed:
                continue
            observed.add(id(process))
            logging.info(
                "Process %s for bot '%s' exited with status %s",
                process.name,
                bot_name,
                process.exitcode,
            )
            if process.exitcode != 0:
                child_failed = True

        alive = _alive_processes(processes)
        if shutdown_signal[0] is not None or child_failed:
            signum = shutdown_signal[0] or signal.SIGTERM
            survivors = _stop_processes(processes, signum)
            for process, bot_name in survivors:
                logging.critical(
                    "Process %s for bot '%s' survived kill escalation",
                    process.name,
                    bot_name,
                )
            break
        if not alive:
            break

        _join_processes(processes, PROCESS_POLL_INTERVAL_SECONDS)

    if shutdown_signal[0] is not None:
        return 128 + shutdown_signal[0]
    return 1 if child_failed else 0


def run_all_bots() -> int:
    """Run enabled non-live bots and return a process-style exit status."""
    bot_dir = Path("bots")
    if not bot_dir.exists():
        logging.error("Bot directory '%s' not found", bot_dir)
        return 1

    bot_files = sorted(bot_dir.glob("*.yaml"))
    if not bot_files:
        logging.error("No bot configuration files found in '%s'", bot_dir)
        return 1

    logging.info("Found %d bot configuration files", len(bot_files))
    processes: list[tuple[multiprocessing.Process, str]] = []
    disabled_count = 0
    started_count = 0
    failure_count = 0
    supervisor_status = 0

    with _supervisor_signal_handlers(processes) as shutdown_signal:
        for config_file in bot_files:
            if shutdown_signal[0] is not None:
                break
            try:
                cfg = load_bot_config(config_file)
                bot_name = cfg["name"]
                if not cfg["enabled"]:
                    logging.info("Skipping disabled bot '%s'", bot_name)
                    disabled_count += 1
                    continue

                # Bulk startup intentionally has no live-authorization path. A live
                # bot must be selected explicitly with --config --authorize-live.
                build_execution_policy(cfg, authorize_live=False)
                platform = get_platform_from_config(cfg)

                if cfg.get("separate_process", False):
                    process = multiprocessing.Process(
                        target=run_bot_process,
                        args=(config_file,),
                        name=f"bot-{bot_name}",
                    )
                    process.start()
                    processes.append((process, bot_name))
                    started_count += 1
                    logging.info(
                        "Started bot '%s' (%s) in process %s",
                        bot_name,
                        platform.value,
                        process.name,
                    )
                else:
                    logging.info(
                        "Starting bot '%s' (%s) in the main process",
                        bot_name,
                        platform.value,
                    )
                    main_signal = asyncio.run(_run_bot_process(config_file))
                    if main_signal is not None:
                        shutdown_signal[0] = main_signal
                        break
                    started_count += 1
            except Exception:
                failure_count += 1
                logging.exception("Failed to start bot from %s", config_file)

        supervisor_status = _supervise_processes(processes, shutdown_signal)
        if supervisor_status != 0:
            failure_count += 1

    logging.info(
        "Bot run summary: started=%d disabled=%d failed=%d",
        started_count,
        disabled_count,
        failure_count,
    )
    if supervisor_status >= 128:
        return supervisor_status
    return 1 if failure_count else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the safe runner command line."""
    parser = argparse.ArgumentParser(description="Run configured trading bots")
    parser.add_argument(
        "--config",
        type=Path,
        help="Run exactly one bot configuration instead of scanning bots/",
    )
    parser.add_argument(
        "--authorize-live",
        action="store_true",
        help=(
            "Explicitly authorize live transaction submission for the single "
            "configuration selected with --config"
        ),
    )
    parser.add_argument(
        "--resume-only",
        action="store_true",
        help="Monitor and exit journaled positions without accepting a new token",
    )
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument(
        "--status",
        action="store_true",
        help="Print recovered position and unresolved-submission status as JSON",
    )
    operations.add_argument(
        "--preflight",
        action="store_true",
        help="Check live signer, RPC, balances, fees, and durable risk without submitting",
    )
    operations.add_argument(
        "--emergency-exit",
        metavar="MINT",
        help="Sell exactly one journaled position without starting a listener",
    )
    args = parser.parse_args(argv)
    if args.authorize_live and args.config is None:
        parser.error("--authorize-live requires an explicit --config path")
    if args.resume_only and args.config is None:
        parser.error("--resume-only requires an explicit --config path")
    if args.resume_only and (args.status or args.emergency_exit is not None):
        parser.error("--resume-only is valid only for startup or --preflight")
    if (
        args.status or args.preflight or args.emergency_exit is not None
    ) and args.config is None:
        parser.error(
            "--status, --preflight, and --emergency-exit require "
            "an explicit --config path"
        )
    if args.emergency_exit is not None and not args.authorize_live:
        parser.error("--emergency-exit requires --authorize-live")
    if (args.status or args.preflight) and args.authorize_live:
        parser.error(
            "--status and --preflight are read-only and must not use --authorize-live"
        )
    return args


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = parse_args(argv)

    try:
        if args.status:
            status = read_bot_status(args.config)
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0
        if args.preflight:
            report = asyncio.run(
                run_live_preflight(args.config, resume_only=args.resume_only)
            )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report.get("ready") is True else 1
        if args.emergency_exit is not None:
            result = asyncio.run(run_emergency_exit(args.config, args.emergency_exit))
            print(json.dumps(result, indent=2, sort_keys=True))
            if result.get("success") is True:
                return 0
            return 2 if result.get("status") == "unknown" else 1
        if args.config is not None:
            shutdown_signal = asyncio.run(
                _run_bot_process(
                    args.config,
                    authorize_live=args.authorize_live,
                    resume_only=args.resume_only,
                )
            )
            return 128 + shutdown_signal if shutdown_signal is not None else 0
        return run_all_bots()
    except Exception:
        logging.exception("Bot runner stopped before successful startup")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
