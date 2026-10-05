"""
Universal trading coordinator that works with any platform.
Cleaned up to remove all platform-specific hardcoding.
"""

import asyncio

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows requires an explicit lock backend
    fcntl = None
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from time import monotonic

from solders.pubkey import Pubkey

from cleanup.manager import AccountCleanupManager
from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
    stage_cleanup_after_sell,
)
from core.client import (
    RpcUnavailableError,
    SolanaClient,
    TransactionOutcome,
    TransactionStatus,
    TransactionSubmissionUnknown,
    estimate_transaction_fee_lamports,
)
from core.execution_policy import ExecutionBlocked, ExecutionMode, ExecutionPolicy
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    WSOL_MINT,
    is_sol_paired,
    normalize_quote_mint,
    quote_units_per_token,
    resolve_quote_amounts,
    resolve_quote_mint,
)
from core.transaction_ledger import (
    EvidencePersistenceError,
    TransactionLedger,
    resolve_transaction_ledger_path,
)
from core.wallet import Wallet
from interfaces.core import Platform, TokenInfo
from learning.journal import (
    PAPER_HORIZONS,
    PAPER_MARK_MAX_LATENESS_S,
    GateSnapshot,
    JevScorer,
    LessonJournal,
    LessonObservation,
)
from monitoring.listener_factory import ListenerFactory
from monitoring.trade_flow import (
    EntryGate,
    FlowMonitor,
    FlowRules,
    FlowSignal,
    GateDecision,
    GateRules,
    GeyserTradeStream,
    TradeEvent,
    TradeFlowHub,
    TradeFlowLossError,
    TradeQueue,
)
from platforms import get_platform_implementations
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import ExitReason, Position
from utils.durable_file import atomic_write_text
from utils.logger import get_logger
from utils.paths import state_path

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    # Standard asyncio is fine, just slightly slower
    pass

logger = get_logger(__name__)

# Exit sells are attempted in bounded bursts. Positions remain journaled and
# monitored after a burst, rather than being abandoned.
DEFAULT_MAX_EXIT_SELL_ATTEMPTS = 3
# Read-only price checks ride out RPC outages for this many seconds before the
# monitor fails closed. 0 fails on the first read error.
DEFAULT_PRICE_READ_OUTAGE_BUDGET = 300.0


def _resolve_quote_config(
    buy_amount: float,
    quote_amounts: dict[str, float] | None,
    allowed_quote_mints: list[str] | None,
) -> tuple[dict[Pubkey, float], set[Pubkey] | None]:
    """Resolve quote-asset configuration into per-mint amounts and an allowlist.

    Keys may be mint addresses or the aliases "sol"/"usdc". SOL always falls
    back to trade.buy_amount, so a config that never mentions quote assets
    keeps its existing SOL-only behaviour.

    Args:
        buy_amount: SOL amount per buy from trade.buy_amount
        quote_amounts: Optional map of quote mint -> amount in whole units
        allowed_quote_mints: Optional list of quote mints permitted to trade

    Returns:
        Tuple of (amount per quote mint, allowed quote mints or None for any)
    """
    amounts = {WSOL_MINT: buy_amount, **resolve_quote_amounts(quote_amounts)}
    allowed = (
        {resolve_quote_mint(mint) for mint in allowed_quote_mints}
        if allowed_quote_mints
        else None
    )
    return amounts, allowed


def _validate_exit_config(
    exit_strategy: str,
    take_profit_percentage: float | None,
    stop_loss_percentage: float | None,
    max_hold_time: int | None,
    price_check_interval: int,
    max_exit_sell_attempts: int,
    price_read_outage_budget: float,
) -> str:
    """Validate exit configuration before any network resources are created."""
    if not isinstance(exit_strategy, str):
        raise ValueError("exit_strategy must be a string")
    strategy = exit_strategy.lower()
    if strategy not in {"tp_sl", "time_based", "manual"}:
        raise ValueError("exit_strategy must be one of: tp_sl, time_based, manual")
    if (
        isinstance(price_check_interval, bool)
        or not isinstance(price_check_interval, int)
        or price_check_interval <= 0
    ):
        raise ValueError("price_check_interval must be a positive integer")
    if (
        isinstance(max_exit_sell_attempts, bool)
        or not isinstance(max_exit_sell_attempts, int)
        or max_exit_sell_attempts <= 0
    ):
        raise ValueError("max_exit_sell_attempts must be a positive integer")
    if (
        isinstance(price_read_outage_budget, bool)
        or not isinstance(price_read_outage_budget, int | float)
        or not isfinite(price_read_outage_budget)
        or price_read_outage_budget < 0
    ):
        raise ValueError("price_read_outage_budget must be a non-negative number")
    if take_profit_percentage is not None:
        if (
            isinstance(take_profit_percentage, bool)
            or not isinstance(take_profit_percentage, int | float)
            or not isfinite(take_profit_percentage)
            or take_profit_percentage <= 0
        ):
            raise ValueError("take_profit_percentage must be finite and positive")
    if stop_loss_percentage is not None:
        if (
            isinstance(stop_loss_percentage, bool)
            or not isinstance(stop_loss_percentage, int | float)
            or not isfinite(stop_loss_percentage)
            or stop_loss_percentage <= 0
            or stop_loss_percentage >= 1
        ):
            raise ValueError("stop_loss_percentage must be finite and between 0 and 1")
    if max_hold_time is not None and (
        isinstance(max_hold_time, bool)
        or not isinstance(max_hold_time, int)
        or max_hold_time <= 0
    ):
        raise ValueError("max_hold_time must be a positive integer")
    if strategy == "tp_sl" and all(
        value is None
        for value in (
            take_profit_percentage,
            stop_loss_percentage,
            max_hold_time,
        )
    ):
        raise ValueError("tp_sl exit strategy requires at least one exit condition")
    return strategy


class UniversalTrader:
    """Universal trading coordinator that works with any supported platform."""

    # Learning integrations are optional; bare instances (tests, offline
    # tools) run without them.
    lesson_journal: object | None = None
    jev_scorer: object | None = None

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        # Platform configuration
        platform: Platform | str = Platform.PUMP_FUN,
        # Listener configuration
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        # Trading configuration
        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        curve_refresh_budget: float = 2.0,
        *,
        trust_create_event: bool = True,
        execution_policy: ExecutionPolicy | None = None,
        # Quote asset configuration (pump.fun non-SOL pairs)
        quote_amounts: dict[str, float] | None = None,
        allowed_quote_mints: list[str] | None = None,
        # Exit strategy configuration
        exit_strategy: str = "time_based",
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        price_check_interval: int = 10,
        max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        price_read_outage_budget: float = DEFAULT_PRICE_READ_OUTAGE_BUDGET,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        # Retry and timeout settings
        max_retries: int = 1,
        wait_time_after_creation: int = 15,
        wait_time_after_buy: int = 15,
        wait_time_before_new_token: int = 15,
        max_token_age: int | float = 0.001,
        token_wait_timeout: int = 30,
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        # Real-time exit rules evaluated on the coin's trade stream (pump.fun)
        flow_rules: FlowRules | None = None,
        # Entry gate evaluated on the first slots of trades (pump.fun + geyser)
        gate_rules: GateRules | None = None,
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        # Compute unit configuration
        compute_units: dict | None = None,
        # Queue and recovery configuration
        max_rps: float = 25.0,
        token_queue_size: int = 128,
        position_journal_path: str | Path | None = None,
        transaction_ledger_path: str | Path | None = None,
    ):
        """Initialize the universal trader."""
        self.exit_strategy = _validate_exit_config(
            exit_strategy,
            take_profit_percentage,
            stop_loss_percentage,
            max_hold_time,
            price_check_interval,
            max_exit_sell_attempts,
            price_read_outage_budget,
        )
        if (
            isinstance(token_queue_size, bool)
            or not isinstance(token_queue_size, int)
            or token_queue_size <= 0
        ):
            raise ValueError("token_queue_size must be a positive integer")
        if self.exit_strategy == "time_based" and (
            isinstance(wait_time_after_buy, bool)
            or not isinstance(wait_time_after_buy, int)
            or wait_time_after_buy <= 0
        ):
            raise ValueError(
                "wait_time_after_buy must be a positive integer for time_based exit"
            )
        resolved_quote_amounts, resolved_allowed_quote_mints = _resolve_quote_config(
            buy_amount,
            quote_amounts,
            allowed_quote_mints,
        )
        if (
            self.exit_strategy == "tp_sl"
            and take_profit_percentage is not None
            and resolved_allowed_quote_mints != {WSOL_MINT}
        ):
            raise ValueError(
                "Net take profit requires a SOL-only allowed_quote_mints list"
            )
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.wallet = Wallet(private_key)
        self.execution_policy.validate_wallet(self.wallet.pubkey)
        self.platform = Platform(platform) if isinstance(platform, str) else platform
        self.transaction_ledger: TransactionLedger | None = None
        self.solana_client = SolanaClient(
            rpc_endpoint,
            max_rps=max_rps,
            execution_policy=self.execution_policy,
            ledger=None,
        )
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
        )
        logger.info(f"Initialized Universal Trader for platform: {self.platform.value}")

        # Validate platform support
        try:
            from platforms import platform_factory

            if not platform_factory.registry.is_platform_supported(self.platform):
                raise ValueError(f"Platform {self.platform.value} is not supported")
        except Exception:
            logger.exception("Platform validation failed")
            raise

        # Get platform-specific implementations
        self.platform_implementations = get_platform_implementations(
            self.platform, self.solana_client
        )

        # Store compute unit and quote-asset configuration
        self.compute_units = compute_units or {}
        self.quote_amounts = resolved_quote_amounts
        self.allowed_quote_mints = resolved_allowed_quote_mints

        # Create platform-aware traders
        self.buyer, self.seller = (
            PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                buy_amount,
                buy_slippage,
                max_retries,
                extreme_fast_token_amount,
                extreme_fast_mode,
                compute_units=self.compute_units,
                quote_amounts=self.quote_amounts,
                allowed_quote_mints=self.allowed_quote_mints,
                curve_refresh_budget=curve_refresh_budget,
                trust_create_event=trust_create_event,
            ),
            PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                sell_slippage,
                max_retries,
                compute_units=self.compute_units,
            ),
        )

        # Initialize the appropriate listener with platform filtering
        self.token_listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            wss_endpoint=wss_endpoint,
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            pumpportal_url=pumpportal_url,
            platforms=[self.platform],  # Only listen for our platform
        )
        self.geyser_endpoint = geyser_endpoint
        self.geyser_api_token = geyser_api_token
        self.geyser_auth_type = geyser_auth_type
        # One program-wide stream feeds the entry gate and the exit rules.
        self.trade_hub: TradeFlowHub | None = None
        if self.platform is Platform.PUMP_FUN and hasattr(
            self.token_listener, "trade_hub"
        ):
            self.trade_hub = TradeFlowHub(
                self.platform_implementations.event_parser._idl_parser  # noqa: SLF001
            )
            self.token_listener.trade_hub = self.trade_hub
        self._gate_queues: dict[str, TradeQueue] = {}
        self._listener_task: asyncio.Task | None = None
        self._paper_tasks: set[asyncio.Task] = set()
        self._buy_attempts = 0
        self._oneshot_found: TokenInfo | None = None
        self._oneshot_event = asyncio.Event()

        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

        # Exit strategy parameters
        self.take_profit_percentage = take_profit_percentage
        self.stop_loss_percentage = stop_loss_percentage
        self.max_hold_time = max_hold_time
        self.price_check_interval = price_check_interval
        self.max_exit_sell_attempts = max_exit_sell_attempts
        self.price_read_outage_budget = float(price_read_outage_budget)

        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.token_wait_timeout = token_wait_timeout

        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode
        self.flow_rules = flow_rules
        self.gate_rules = gate_rules
        self._flow_signals: dict[str, FlowSignal] = {}
        self._flow_wakeups: dict[str, asyncio.Event] = {}
        self._flow_latched: set[str] = set()

        # Learning (optional): per-transaction lesson journal with optional
        # Jev scoring. Both fail open (disabled) without configuration.
        self.lesson_journal = LessonJournal()
        self.jev_scorer = JevScorer(env_file=state_path("configs", "typesafe.env"))

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[str, Pubkey] = {}
        self.token_queue: asyncio.Queue[TokenInfo] = asyncio.Queue(
            maxsize=token_queue_size
        )
        self.processed_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}
        self._reserved_mints: set[str] = set()
        self._inflight_tokens: dict[str, TokenInfo] = {}
        self._queue_lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()
        self._position_tasks: set[asyncio.Task] = set()
        self._position_monitor_tasks: set[asyncio.Task] = set()
        self._fatal_monitor_errors: asyncio.Queue[BaseException] = asyncio.Queue()
        self._unresolved_buy_state_changed = asyncio.Event()
        self._active_positions: dict[str, tuple[TokenInfo, Position]] = {}
        self._unresolved_buys: dict[str, dict] = {}
        self._pending_recovery_tokens: list[TokenInfo] = []
        self._journal_path = (
            Path(position_journal_path)
            if position_journal_path is not None
            else state_path(
                "positions", f"{self.wallet.pubkey}-{self.platform.value}.json"
            )
        )
        self._journal_lock_handle = None
        try:
            if self.execution_policy.mode is ExecutionMode.LIVE:
                if fcntl is None:
                    raise RuntimeError(  # noqa: TRY003, TRY301
                        "Live recovery journal locking is unavailable on this platform"
                    )
                ledger_path = (
                    Path(transaction_ledger_path)
                    if transaction_ledger_path is not None
                    else resolve_transaction_ledger_path(self.wallet.pubkey)
                )
                self.transaction_ledger = TransactionLedger(ledger_path)
                self.solana_client.ledger = self.transaction_ledger
                self._journal_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self._journal_path.with_suffix(
                    f"{self._journal_path.suffix}.lock"
                )
                self._journal_lock_handle = lock_path.open("a+b")
                try:
                    fcntl.flock(
                        self._journal_lock_handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError as exc:
                    raise RuntimeError(  # noqa: TRY003
                        f"Another live trader owns recovery journal {self._journal_path}"
                    ) from exc
                self._initialize_evidence_profile(
                    {
                        "listener_type": listener_type,
                        "curve_refresh_budget": curve_refresh_budget,
                        "trust_create_event": trust_create_event,
                        "enable_dynamic_priority_fee": enable_dynamic_priority_fee,
                        "enable_fixed_priority_fee": enable_fixed_priority_fee,
                        "fixed_priority_fee": fixed_priority_fee,
                        "extra_priority_fee": extra_priority_fee,
                        "hard_cap_prior_fee": hard_cap_prior_fee,
                        "max_rps": max_rps,
                        "token_queue_size": token_queue_size,
                    }
                )
            self._load_recovery_journal()
            self._hydrate_submission_recovery()
        except BaseException:
            for stage, error in self._release_persistence_resources():
                logger.error(
                    "Failure during constructor cleanup: %s",
                    stage,
                    exc_info=(type(error), error, error.__traceback__),
                )
            raise

    def _initialize_evidence_profile(self, runtime_settings: dict[str, object]) -> None:
        """Fingerprint allowlisted settings and source files once, before trading."""
        if self.transaction_ledger is None:
            message = "Live evidence requires a ledger"
            raise EvidencePersistenceError(message)
        settings = {
            name: getattr(self, name)
            for name in (
                "buy_amount",
                "buy_slippage",
                "sell_slippage",
                "max_retries",
                "extreme_fast_mode",
                "extreme_fast_token_amount",
                "exit_strategy",
                "take_profit_percentage",
                "stop_loss_percentage",
                "max_hold_time",
                "price_check_interval",
                "max_exit_sell_attempts",
                "price_read_outage_budget",
                "wait_time_after_creation",
                "wait_time_after_buy",
                "wait_time_before_new_token",
                "max_token_age",
                "token_wait_timeout",
                "cleanup_mode",
                "cleanup_force_close_with_burn",
                "cleanup_with_priority_fee",
                "match_string",
                "bro_address",
                "marry_mode",
                "yolo_mode",
                "compute_units",
            )
        }
        settings.update(runtime_settings)
        settings.update(
            {
                "platform": self.platform.value,
                "wallet": str(self.wallet.pubkey),
                "execution": asdict(self.execution_policy),
                "quote_amounts": {
                    str(mint): amount for mint, amount in self.quote_amounts.items()
                },
                "allowed_quote_mints": (
                    sorted(map(str, self.allowed_quote_mints))
                    if self.allowed_quote_mints is not None
                    else None
                ),
                "entry_gate": asdict(self.gate_rules) if self.gate_rules else None,
                "flow_exit": asdict(self.flow_rules) if self.flow_rules else None,
                "python_version": list(sys.version_info[:3]),
            }
        )
        source_root = Path(__file__).resolve().parents[1]
        root = source_root.parent
        paths = [
            *source_root.rglob("*.py"),
            *(root / "idl").glob("*.json"),
            root / "pyproject.toml",
            root / "uv.lock",
        ]
        sources = {
            str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)
            if path.is_file() and not path.is_symlink()
        }
        self.solana_client.evidence_profile_id = (
            self.transaction_ledger.record_evidence_profile(
                self.execution_policy.mode.value, settings, sources
            )
        )

    def _record_trade_evidence(
        self,
        category: str,
        token_info: TokenInfo,
        *,
        result: TradeResult | None = None,
        position: Position | None = None,
        **details: object,
    ) -> None:
        """Persist observations before discarding recoverable work; never infer fills."""
        if self.execution_policy.mode is not ExecutionMode.LIVE:
            # Dry-run authorization checks are not simulated fills.
            return
        if (
            self.transaction_ledger is None
            or self.solana_client.evidence_profile_id is None
        ):
            message = "Live evidence profile is unavailable"
            raise EvidencePersistenceError(message)
        payload = {"token": self._token_to_dict(token_info), **details}
        if result is not None:
            outcome = result.to_dict()
            outcome["fee_budget_lamports"] = outcome.pop("fee_lamports")
            # Provider exceptions can contain credential-bearing URLs. Structured
            # status and public chain errors are retained; text stays in the logger.
            outcome["has_error"] = outcome.pop("error_message") is not None
            if not result.success:
                for name in ("price", "amount_raw", "quote_amount_raw"):
                    outcome[name] = None
            payload["result"] = outcome
        if position is not None:
            snapshot = position.to_dict()
            for name in ("buy_fee", "charged_exit_fee", "pending_exit_fee"):
                snapshot[f"{name}_budget_lamports"] = snapshot.pop(f"{name}_lamports")
            payload["position"] = snapshot
        self.transaction_ledger.record_trade_evidence(
            self.solana_client.evidence_profile_id, category, payload
        )

    def _release_persistence_resources(
        self,
    ) -> list[tuple[str, BaseException]]:
        """Release the ledger and journal lock, attempting both on failure."""
        failures: list[tuple[str, BaseException]] = []

        ledger = self.transaction_ledger
        self.transaction_ledger = None
        self.solana_client.ledger = None
        if ledger is not None:
            try:
                ledger.close()
            except BaseException as exc:
                failures.append(("transaction ledger close", exc))

        lock_handle = self._journal_lock_handle
        self._journal_lock_handle = None
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except BaseException as exc:
                failures.append(("recovery journal unlock", exc))
            try:
                lock_handle.close()
            except BaseException as exc:
                failures.append(("recovery journal lock close", exc))

        return failures

    @staticmethod
    def _validate_creation_timestamp(value: object) -> float | None:
        """Accept only finite timestamps matching TokenInfo's declared type."""
        if value is None:
            return None
        if not isinstance(value, float) or not isfinite(value):
            raise ValueError(
                "Recovery token creation_timestamp must be a finite float or null"
            )
        return value

    @staticmethod
    def _validate_optional_raw_u64(
        value: object,
        field_name: str,
        *,
        positive: bool = False,
    ) -> int | None:
        """Preserve optional raw integers exactly across recovery JSON."""
        if value is None:
            return None
        minimum = 1 if positive else 0
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= 0xFFFF_FFFF_FFFF_FFFF
        ):
            qualifier = "positive " if positive else ""
            raise ValueError(
                f"Recovery token {field_name} must be a {qualifier}raw u64 "
                "integer or null"
            )
        return value

    @staticmethod
    def _token_to_dict(token_info: TokenInfo) -> dict:
        """Serialize only authoritative TokenInfo fields needed for recovery."""
        pubkey_fields = (
            "mint",
            "bonding_curve",
            "associated_bonding_curve",
            "pool_state",
            "base_vault",
            "quote_vault",
            "global_config",
            "platform_config",
            "user",
            "creator",
            "creator_vault",
            "protocol_fee_recipient",
            "buyback_fee_recipient",
            "token_program_id",
            "quote_mint",
            "quote_token_program_id",
        )
        payload = {
            "name": token_info.name,
            "symbol": token_info.symbol,
            "uri": token_info.uri,
            "platform": token_info.platform.value,
            "is_mayhem_mode": token_info.is_mayhem_mode,
            "is_cashback_coin": token_info.is_cashback_coin,
            "pool_needs_extension": token_info.pool_needs_extension,
            "virtual_token_reserves": UniversalTrader._validate_optional_raw_u64(
                token_info.virtual_token_reserves,
                "virtual_token_reserves",
                positive=True,
            ),
            "virtual_quote_reserves": UniversalTrader._validate_optional_raw_u64(
                token_info.virtual_quote_reserves,
                "virtual_quote_reserves",
                positive=True,
            ),
            "real_token_reserves": UniversalTrader._validate_optional_raw_u64(
                token_info.real_token_reserves,
                "real_token_reserves",
            ),
            "token_total_supply": UniversalTrader._validate_optional_raw_u64(
                token_info.token_total_supply,
                "token_total_supply",
                positive=True,
            ),
            "state_from_event": token_info.state_from_event,
            "curve_complete": token_info.curve_complete,
            "pool_tradeable": token_info.pool_tradeable,
            "pool_status": token_info.pool_status,
            "base_decimals": token_info.base_decimals,
            "quote_decimals": token_info.quote_decimals,
            "source": token_info.source,
            "signature": token_info.signature,
            "slot": token_info.slot,
            "commitment": token_info.commitment,
            "transaction_index": token_info.transaction_index,
            "inner_instruction_index": token_info.inner_instruction_index,
            "metadata_verified": token_info.metadata_verified,
            "creation_timestamp": UniversalTrader._validate_creation_timestamp(
                token_info.creation_timestamp
            ),
        }
        for field_name in pubkey_fields:
            value = getattr(token_info, field_name)
            payload[field_name] = str(value) if value is not None else None
        return payload

    @staticmethod
    def token_from_dict(payload: dict) -> TokenInfo:
        """Restore TokenInfo without guessing missing protocol metadata."""
        if not isinstance(payload, dict):
            raise ValueError("Recovery token record must be an object")
        pubkey_fields = (
            "mint",
            "bonding_curve",
            "associated_bonding_curve",
            "pool_state",
            "base_vault",
            "quote_vault",
            "global_config",
            "platform_config",
            "user",
            "creator",
            "creator_vault",
            "token_program_id",
            "protocol_fee_recipient",
            "buyback_fee_recipient",
            "quote_mint",
            "quote_token_program_id",
        )
        values = {
            field_name: (
                Pubkey.from_string(payload[field_name])
                if payload.get(field_name)
                else None
            )
            for field_name in pubkey_fields
        }
        boolean_fields = (
            "is_mayhem_mode",
            "is_cashback_coin",
            "state_from_event",
            "metadata_verified",
            "pool_needs_extension",
        )
        for field_name in boolean_fields:
            value = payload.get(field_name, False)
            if not isinstance(value, bool):
                raise ValueError(f"Recovery token {field_name} must be a boolean")
        for field_name in ("curve_complete", "pool_tradeable"):
            value = payload.get(field_name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(
                    f"Recovery token {field_name} must be a boolean or null"
                )
        pool_status = payload.get("pool_status")
        if pool_status is not None and not isinstance(pool_status, str):
            raise ValueError("Recovery token pool_status must be a string or null")
        event_reserves = {
            "virtual_token_reserves": UniversalTrader._validate_optional_raw_u64(
                payload.get("virtual_token_reserves"),
                "virtual_token_reserves",
                positive=True,
            ),
            "virtual_quote_reserves": UniversalTrader._validate_optional_raw_u64(
                payload.get("virtual_quote_reserves"),
                "virtual_quote_reserves",
                positive=True,
            ),
            "real_token_reserves": UniversalTrader._validate_optional_raw_u64(
                payload.get("real_token_reserves"),
                "real_token_reserves",
            ),
            "token_total_supply": UniversalTrader._validate_optional_raw_u64(
                payload.get("token_total_supply"),
                "token_total_supply",
                positive=True,
            ),
        }
        return TokenInfo(
            name=str(payload["name"]),
            symbol=str(payload["symbol"]),
            uri=str(payload.get("uri", "")),
            platform=Platform(payload["platform"]),
            is_mayhem_mode=payload.get("is_mayhem_mode", False),
            is_cashback_coin=payload.get("is_cashback_coin", False),
            **event_reserves,
            state_from_event=payload.get("state_from_event", False),
            curve_complete=payload.get("curve_complete"),
            pool_tradeable=payload.get("pool_tradeable"),
            pool_status=payload.get("pool_status"),
            pool_needs_extension=payload.get("pool_needs_extension", False),
            creation_timestamp=UniversalTrader._validate_creation_timestamp(
                payload.get("creation_timestamp")
            ),
            base_decimals=payload.get("base_decimals"),
            quote_decimals=payload.get("quote_decimals"),
            source=payload.get("source"),
            signature=payload.get("signature"),
            slot=payload.get("slot"),
            commitment=payload.get("commitment"),
            transaction_index=payload.get("transaction_index"),
            inner_instruction_index=payload.get("inner_instruction_index"),
            metadata_verified=payload.get("metadata_verified", False),
            **values,
        )

    def _load_recovery_journal(self) -> None:
        """Load active positions and unresolved work, failing closed on corruption."""
        if not self._journal_path.exists():
            return
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("recovery journal must contain an object")
            if payload.get("version") != 1:
                raise ValueError("unsupported journal version")
            if payload.get("wallet") != str(self.wallet.pubkey):
                raise ValueError("recovery journal belongs to a different wallet")
            if payload.get("platform") != self.platform.value:
                raise ValueError("recovery journal belongs to a different platform")

            position_records = payload.get("positions", {})
            unresolved_records = payload.get("unresolved_buys", {})
            pending_records = payload.get("pending_tokens", [])
            if not isinstance(position_records, dict):
                raise ValueError("recovery journal positions must be an object")
            if not isinstance(unresolved_records, dict):
                raise ValueError("recovery journal unresolved_buys must be an object")
            if not isinstance(pending_records, list):
                raise ValueError("recovery journal pending_tokens must be an array")

            active_positions = dict(self._active_positions)
            unresolved_buys = dict(self._unresolved_buys)
            reserved_mints = set(self._reserved_mints)
            traded_mints = set(self.traded_mints)
            traded_token_programs = dict(self.traded_token_programs)
            cleanup_records: list[tuple[TokenInfo, Position]] = []
            migrated_positions = False

            for token_key, record in position_records.items():
                if not isinstance(record, dict):
                    raise ValueError("position journal record must be an object")
                token_info = self.token_from_dict(record["token"])
                position_payload = record["position"]
                position = Position.from_dict(position_payload)
                if str(position.mint) != token_key or position.mint != token_info.mint:
                    raise ValueError("position journal mint mismatch")
                if token_info.platform is not self.platform:
                    raise ValueError("position journal platform mismatch")
                if self._migrate_legacy_position(
                    token_info,
                    position,
                    recover_exit_fee_history=(
                        "charged_exit_fee_lamports" not in position_payload
                    ),
                ):
                    record["position"] = position.to_dict()
                    migrated_positions = True
                if position.is_active:
                    active_positions[token_key] = (token_info, position)
                    reserved_mints.add(token_key)
                    traded_mints.add(token_info.mint)
                    if token_info.token_program_id is not None:
                        traded_token_programs[token_key] = token_info.token_program_id
                        if (
                            position.account_balance_baseline_raw is not None
                            and position.quantity_raw is not None
                        ):
                            cleanup_records.append((token_info, position))

            for token_key, record in unresolved_records.items():
                if not isinstance(record, dict):
                    raise ValueError("unresolved buy journal record must be an object")
                token_info = self.token_from_dict(record["token"])
                signature = record.get("signature")
                if (
                    str(token_info.mint) != token_key
                    or not isinstance(signature, str)
                    or not signature
                ):
                    raise ValueError("invalid unresolved buy journal record")
                if token_info.platform is not self.platform:
                    raise ValueError("unresolved buy journal platform mismatch")
                unresolved_buys[token_key] = {
                    "token": token_info,
                    "signature": signature,
                    "baseline_raw": record.get("baseline_raw"),
                }
                reserved_mints.add(token_key)

            pending_recovery_tokens: list[TokenInfo] = []
            for item in pending_records:
                if not isinstance(item, dict):
                    raise ValueError("pending recovery token record must be an object")
                pending_token = self.token_from_dict(item)
                if pending_token.platform is not self.platform:
                    raise ValueError("pending recovery token platform mismatch")
                pending_recovery_tokens.append(pending_token)

            if migrated_positions:
                payload["updated_at"] = datetime.now(UTC).isoformat()
                atomic_write_text(
                    self._journal_path,
                    json.dumps(payload, indent=2, sort_keys=True),
                )

            for token_info, position in cleanup_records:
                AccountCleanupManager.record_bot_owned_balance(
                    self.wallet.pubkey,
                    token_info.mint,
                    token_info.token_program_id,
                    baseline_raw=position.account_balance_baseline_raw,
                    acquired_raw=position.quantity_raw,
                    ownership_id=position.position_id,
                )

            self._active_positions = active_positions
            self._unresolved_buys = unresolved_buys
            self._pending_recovery_tokens = pending_recovery_tokens
            self._reserved_mints = reserved_mints
            self.traded_mints = traded_mints
            self.traded_token_programs = traded_token_programs
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot safely load recovery journal {self._journal_path}"
            ) from exc

    @staticmethod
    def _buy_intent_id(token_info: TokenInfo) -> str:
        """Return the stable ledger intent used by the buyer."""
        return f"buy:{token_info.platform.value}:{token_info.mint}"

    @staticmethod
    def _recover_legacy_charged_exit_fees(
        ledger: TransactionLedger,
        position: Position,
    ) -> int:
        """Sum fees from prior reverted exit submissions."""
        if position.exit_attempt_sequence == 0:
            return 0
        if position.position_id is None:
            raise ValueError("cannot recover exit fees without a position id")

        charged_fees = 0
        for sequence in range(1, position.exit_attempt_sequence + 1):
            intent_ids = (
                f"sell:{position.position_id}:{sequence}",
                f"emergency-sell:{position.position_id}:{sequence}",
            )
            records = [
                (intent_id, record)
                for intent_id in intent_ids
                if (record := ledger.get_latest_submission_record(intent_id))
                is not None
            ]
            if len(records) > 1:
                raise ValueError(
                    f"multiple exit submissions found for attempt {sequence}"
                )
            if not records:
                continue
            intent_id, record = records[0]
            if position.pending_exit_intent_id == intent_id:
                continue
            outcome = ledger.get_outcome(record.signature)
            if outcome is None or outcome.status is TransactionStatus.UNKNOWN:
                raise ValueError(
                    f"prior exit attempt {sequence} has no terminal outcome"
                )
            if outcome.status is TransactionStatus.SUCCESS:
                raise ValueError(
                    f"active position has successful prior exit attempt {sequence}"
                )
            if outcome.status is TransactionStatus.REVERTED:
                if record.fee_lamports is None:
                    raise ValueError(
                        f"reverted exit attempt {sequence} has no durable fee"
                    )
                charged_fees += record.fee_lamports
            elif outcome.status is not TransactionStatus.EXPIRED:
                raise ValueError(
                    f"prior exit attempt {sequence} has unsupported outcome"
                )
        return charged_fees

    def _migrate_legacy_position(
        self,
        token_info: TokenInfo,
        position: Position,
        *,
        recover_exit_fee_history: bool,
    ) -> bool:
        """Migrate fee-sensitive fields using exact durable ledger evidence."""
        changed = False
        ledger = getattr(self, "transaction_ledger", None)
        if (
            position.is_active
            and position.take_profit_price is not None
            and position.take_profit_net_quote_raw is None
        ):
            if ledger is None:
                raise ValueError(
                    "cannot recover net take-profit target without a transaction ledger"
                )
            buy_record = ledger.get_active_submission_record(
                self._buy_intent_id(token_info)
            )
            if (
                buy_record is None
                or buy_record.signature != position.position_id
                or buy_record.fee_lamports is None
            ):
                raise ValueError(
                    "cannot recover net take-profit target without exact buy fee evidence"
                )
            position.migrate_legacy_take_profit_target(buy_record.fee_lamports)
            changed = True

        if (
            recover_exit_fee_history
            and position.is_active
            and position.take_profit_price is not None
        ):
            if ledger is None:
                raise ValueError(
                    "cannot recover charged exit fees without a transaction ledger"
                )
            position.record_charged_exit_fee(
                self._recover_legacy_charged_exit_fees(ledger, position)
            )
            changed = True

        if (
            position.pending_exit_signature is not None
            and position.pending_exit_fee_lamports is None
        ):
            if ledger is None or position.pending_exit_intent_id is None:
                raise ValueError(
                    "cannot recover pending sell fee without a transaction ledger"
                )
            sell_record = ledger.get_latest_submission_record(
                position.pending_exit_intent_id
            )
            if (
                sell_record is None
                or sell_record.signature != position.pending_exit_signature
                or sell_record.fee_lamports is None
                or position.pending_exit_reason is None
            ):
                raise ValueError(
                    "cannot recover pending sell fee from exact ledger submission"
                )
            position.mark_exit_pending(
                sell_record.signature,
                position.pending_exit_reason,
                fee_lamports=sell_record.fee_lamports,
            )
            changed = True
        return changed

    def _hydrate_submission_recovery(self) -> None:
        """Join journaled work to exact ledger signatures before age/price gates."""
        if self.transaction_ledger is None:
            return
        changed = False
        remaining_pending: list[TokenInfo] = []
        for token_info in self._pending_recovery_tokens:
            token_key = str(token_info.mint)
            intent_id = self._buy_intent_id(token_info)
            record = self.transaction_ledger.get_active_submission_record(intent_id)
            if record is None:
                remaining_pending.append(token_info)
                continue
            self._unresolved_buys[token_key] = {
                "token": token_info,
                "signature": record.signature,
                "baseline_raw": None,
                "intent_id": intent_id,
            }
            self._reserved_mints.add(token_key)
            changed = True
        if len(remaining_pending) != len(self._pending_recovery_tokens):
            self._pending_recovery_tokens = remaining_pending

        for token_key, (token_info, position) in self._active_positions.items():
            intent_id = position.pending_exit_intent_id
            if intent_id is None or position.pending_exit_signature is not None:
                continue
            record = self.transaction_ledger.get_active_submission_record(intent_id)
            if record is None:
                terminal_record = self.transaction_ledger.get_latest_submission_record(
                    intent_id
                )
                if terminal_record is not None:
                    outcome = self.transaction_ledger.get_outcome(
                        terminal_record.signature
                    )
                    if outcome is None or outcome.status not in {
                        TransactionStatus.REVERTED,
                        TransactionStatus.EXPIRED,
                    }:
                        raise RuntimeError(
                            f"Position {token_key} has an inconsistent exit outcome"
                        )
                    if outcome.status is TransactionStatus.REVERTED:
                        if terminal_record.fee_lamports is None:
                            raise RuntimeError(
                                f"Position {token_key} has a reverted exit "
                                "without a durable fee"
                            )
                        position.record_charged_exit_fee(terminal_record.fee_lamports)
                position.clear_pending_exit()
                self._active_positions[token_key] = (token_info, position)
                changed = True
                continue
            if position.pending_exit_reason is None:
                raise RuntimeError(
                    f"Position {token_key} has a pending exit intent without a reason"
                )
            if record.fee_lamports is None:
                raise RuntimeError(
                    f"Position {token_key} has a pending exit without a durable fee"
                )
            position.mark_exit_pending(
                record.signature,
                position.pending_exit_reason,
                fee_lamports=record.fee_lamports,
            )
            self._active_positions[token_key] = (token_info, position)
            changed = True

        if changed:
            self._write_recovery_journal()

    async def _resume_ledger_bound_submissions(self) -> None:
        """Resume prepared exact wires before listeners, age checks, or prices."""
        if getattr(self, "transaction_ledger", None) is None:
            return
        intents: dict[str, str] = {}
        for record in self._unresolved_buys.values():
            token_info = record["token"]
            intent_id = record.get("intent_id") or self._buy_intent_id(token_info)
            intents[intent_id] = record["signature"]
        for _token_info, position in self._active_positions.values():
            if (
                position.pending_exit_intent_id is not None
                and position.pending_exit_signature is not None
            ):
                intents[position.pending_exit_intent_id] = (
                    position.pending_exit_signature
                )

        for intent_id, expected_signature in intents.items():
            try:
                recovered = await self.solana_client.recover_active_submission(
                    intent_id
                )
            except TransactionSubmissionUnknown as exc:
                recovered_signature = exc.signature
            else:
                recovered_signature = None if recovered is None else str(recovered)
            if (
                recovered_signature is not None
                and recovered_signature != expected_signature
            ):
                raise RuntimeError(
                    f"Ledger recovery signature mismatch for intent {intent_id}"
                )

    async def _resume_staged_cleanups(self) -> None:
        """Consume durable post-sell cleanup work before accepting new tokens."""
        if getattr(self, "cleanup_mode", None) not in {"after_sell", "post_session"}:
            return
        manager = AccountCleanupManager(
            self.solana_client,
            self.wallet,
            self.priority_fee_manager,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )
        results = await manager.resume_pending_cleanups()
        for result in results:
            if not result.success:
                logger.warning(
                    "Recovered cleanup remains %s for mint %s",
                    result.status.value,
                    result.mint,
                )

    def _write_recovery_journal(self) -> None:
        """Atomically persist every active or unfinished unit of work."""
        payload = {
            "version": 1,
            "wallet": str(self.wallet.pubkey),
            "platform": self.platform.value,
            "updated_at": datetime.now(UTC).isoformat(),
            "positions": {
                token_key: {
                    "token": self._token_to_dict(token_info),
                    "position": position.to_dict(),
                }
                for token_key, (token_info, position) in self._active_positions.items()
                if position.is_active
            },
            "unresolved_buys": {
                token_key: {
                    "token": self._token_to_dict(record["token"]),
                    "signature": record["signature"],
                    "baseline_raw": record.get("baseline_raw"),
                }
                for token_key, record in self._unresolved_buys.items()
            },
            "pending_tokens": [
                self._token_to_dict(token_info)
                for token_info in self._pending_recovery_tokens
            ],
        }
        atomic_write_text(
            self._journal_path,
            json.dumps(payload, indent=2, sort_keys=True),
        )

    def _persist_position(self, token_info: TokenInfo, position: Position) -> None:
        """Record an active holding before monitoring or returning control."""
        token_key = str(token_info.mint)
        self._active_positions[token_key] = (token_info, position)
        self._reserved_mints.add(token_key)
        self.traded_mints.add(token_info.mint)
        if token_info.token_program_id is not None:
            self.traded_token_programs[token_key] = token_info.token_program_id
            if (
                position.account_balance_baseline_raw is not None
                and position.quantity_raw is not None
            ):
                AccountCleanupManager.record_bot_owned_balance(
                    self.wallet.pubkey,
                    token_info.mint,
                    token_info.token_program_id,
                    baseline_raw=position.account_balance_baseline_raw,
                    acquired_raw=position.quantity_raw,
                    ownership_id=position.position_id,
                )
        self._write_recovery_journal()

    def _remove_position(self, mint: Pubkey) -> None:
        """Remove only a confirmed-closed position from the active journal."""
        token_key = str(mint)
        self._active_positions.pop(token_key, None)
        self._reserved_mints.discard(token_key)
        self.processed_tokens.add(token_key)
        self._write_recovery_journal()

    @staticmethod
    def has_automatic_exit(position: Position) -> bool:
        """Return whether an active position has a monitorable exit trigger."""
        return position.is_active and (
            position.take_profit_price is not None
            or position.stop_loss_price is not None
            or position.max_hold_time is not None
        )

    def _schedule_position_monitor(
        self, token_info: TokenInfo, position: Position
    ) -> asyncio.Task | None:
        """Start one monitor task for an active automatic-exit position."""
        if not self.has_automatic_exit(position):
            return None
        task = asyncio.create_task(
            self._monitor_position_until_exit(token_info, position)
        )
        self._position_tasks.add(task)
        self._position_monitor_tasks.add(task)

        def _task_finished(done_task: asyncio.Task) -> None:
            self._position_tasks.discard(done_task)
            self._position_monitor_tasks.discard(done_task)
            if done_task.cancelled():
                return
            error = done_task.exception()
            if error is not None:
                self._fatal_monitor_errors.put_nowait(error)
                logger.error(
                    "Position monitor stopped unexpectedly for %s",
                    token_info.mint,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_task_finished)
        return task

    async def _await_queue_drain(
        self,
        processor_task: asyncio.Task,
        lifecycle_failure_task: asyncio.Task,
    ) -> None:
        """Wait for queued work unless its processor or lifecycle fails."""
        drain_task = asyncio.create_task(self.token_queue.join())
        try:
            done, _ = await asyncio.wait(
                {drain_task, processor_task, lifecycle_failure_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lifecycle_failure_task in done:
                raise await lifecycle_failure_task
            if processor_task in done:
                await processor_task
                raise RuntimeError("Token queue processor stopped unexpectedly")
            await drain_task
            if lifecycle_failure_task.done():
                raise await lifecycle_failure_task
        finally:
            if not drain_task.done():
                drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

    async def _await_unresolved_buy_resolution(
        self,
        token_key: str,
        lifecycle_failure_task: asyncio.Task,
    ) -> None:
        """Keep one-shot execution alive until its ambiguous buy is resolved."""
        while token_key in self._unresolved_buys:
            self._unresolved_buy_state_changed.clear()
            if token_key not in self._unresolved_buys:
                break
            state_changed_task = asyncio.create_task(
                self._unresolved_buy_state_changed.wait()
            )
            try:
                done, _ = await asyncio.wait(
                    {state_changed_task, lifecycle_failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lifecycle_failure_task in done:
                    raise await lifecycle_failure_task
                await state_changed_task
            finally:
                if not state_changed_task.done():
                    state_changed_task.cancel()
                try:
                    await state_changed_task
                except asyncio.CancelledError:
                    pass

    async def _get_pending_sell_receipt(
        self, token_info: TokenInfo, position: Position
    ) -> tuple[int, float] | None:
        """Price a confirmed sell only from its actual quote proceeds."""
        quote_mint = normalize_quote_mint(token_info.quote_mint)
        quote_raw = await self.solana_client.get_sell_transaction_details(
            position.pending_exit_signature, quote_mint, self.wallet.pubkey
        )
        if (
            isinstance(quote_raw, bool)
            or not isinstance(quote_raw, int)
            or quote_raw <= 0
        ):
            return None
        try:
            exit_price = (
                quote_raw / quote_units_per_token(quote_mint)
            ) / position.quantity
        except (OverflowError, ValueError):
            return None
        if not isfinite(exit_price) or exit_price <= 0:
            return None
        return quote_raw, exit_price

    def _defer_unpriced_sell_result(
        self, token_info: TokenInfo, position: Position, result: TradeResult
    ) -> bool:
        """Keep a malformed successful receipt pending instead of realizing a quote."""
        if not result.success or (
            not isinstance(result.price, bool)
            and isinstance(result.price, int | float)
            and isfinite(result.price)
            and result.price > 0
        ):
            self._record_trade_evidence(
                "trade_result",
                token_info,
                result=result,
                position=position,
                action="sell",
            )
            return False
        if not result.tx_signature:
            message = "Unpriced successful sell has no recovery signature"
            raise RuntimeError(message)
        # A missing fee can be recovered from the existing submission ledger.
        position.pending_exit_signature = result.tx_signature
        position.pending_exit_fee_lamports = None
        if result.fee_lamports is not None:
            position.mark_exit_pending(
                result.tx_signature,
                position.pending_exit_reason,
                fee_lamports=result.fee_lamports,
            )
        self._persist_position(token_info, position)
        result.success = False
        result.status = TransactionStatus.UNKNOWN.value
        result.price = None
        result.quote_amount_raw = None
        result.error_message = "Confirmed sell receipt accounting is unavailable"
        self._record_trade_evidence(
            "trade_result", token_info, result=result, position=position, action="sell"
        )
        return True

    def _link_lesson_outcome(
        self,
        position: Position,
        *,
        quote_mint: Pubkey | str | None,
        sold_quote_raw: int | None,
        reason: str,
    ) -> None:
        """Record a closed position's outcome on the lesson row that opened it."""
        if self.lesson_journal is None:
            return
        pnl_quote_raw = (
            sold_quote_raw - position.quote_amount_raw
            if sold_quote_raw is not None and position.quote_amount_raw is not None
            else None
        )
        self.lesson_journal.link_outcome(
            str(position.mint),
            pnl_quote_raw,
            quote_mint=quote_mint,
            reason=reason,
            entry_id=position.entry_lesson_id,
        )

    async def _finalize_emergency_exit(
        self,
        token_info: TokenInfo,
        position: Position,
        *,
        exit_price: float,
        tx_signature: str | None,
        sold_raw: int | None,
        quote_amount_raw: int | None,
    ) -> None:
        """Close one journaled position after confirmed emergency sale."""
        self._record_trade_evidence(
            "position_closed",
            token_info,
            position=position,
            action="sell",
            signature=tx_signature,
            exit_reason=ExitReason.MANUAL.value,
            price=exit_price,
            amount_raw=sold_raw,
            quote_amount_raw=quote_amount_raw,
        )
        self._link_lesson_outcome(
            position,
            quote_mint=normalize_quote_mint(token_info.quote_mint),
            sold_quote_raw=quote_amount_raw,
            reason=ExitReason.MANUAL.value,
        )
        cleanup_raw = (
            sold_raw
            if (
                position.account_balance_baseline_raw is not None
                and token_info.token_program_id is not None
            )
            else None
        )
        staged_cleanup = (
            stage_cleanup_after_sell(
                self.solana_client,
                self.wallet,
                token_info.mint,
                token_info.token_program_id,
                self.priority_fee_manager,
                self.cleanup_mode,
                self.cleanup_with_priority_fee,
                self.cleanup_force_close_with_burn,
                cleanup_raw,
            )
            if cleanup_raw is not None
            else None
        )
        position.close_position(exit_price, ExitReason.MANUAL)
        self._log_trade(
            "sell",
            token_info,
            exit_price,
            position.quantity,
            tx_signature,
        )
        self._remove_position(token_info.mint)
        await handle_cleanup_after_sell(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
            confirmed_sold_raw=(cleanup_raw if staged_cleanup is None else None),
            staged_manager=staged_cleanup,
        )

    async def emergency_exit(self, mint: Pubkey) -> TradeResult:  # noqa: C901
        """Sell one recovered position without starting any token listener."""
        try:
            self.execution_policy.require_submission()
            token_key = str(mint)
            active = self._active_positions.get(token_key)
            if active is None:
                raise ValueError(  # noqa: TRY003
                    f"No active position is journaled for mint {mint}"
                )
            token_info, position = active
            curve_manager = self.platform_implementations.curve_manager

            prepare_live_execution = getattr(
                curve_manager, "prepare_live_execution", None
            )
            if callable(prepare_live_execution):
                await prepare_live_execution()

            if position.pending_exit_signature is not None:
                outcome = await self.solana_client.confirm_transaction_outcome(
                    position.pending_exit_signature
                )
                self._record_trade_evidence(
                    "chain_outcome",
                    token_info,
                    position=position,
                    action="sell",
                    signature=position.pending_exit_signature,
                    status=outcome.status.value,
                    slot=outcome.slot,
                )
                if outcome.status is TransactionStatus.UNKNOWN:
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=position.pending_exit_signature,
                        error_message=outcome.error,
                        amount=position.quantity,
                        amount_raw=position.quantity_raw,
                        fee_lamports=position.pending_exit_fee_lamports,
                        slot=outcome.slot,
                        status=TransactionStatus.UNKNOWN.value,
                    )
                if outcome.status is TransactionStatus.SUCCESS:
                    receipt = await self._get_pending_sell_receipt(token_info, position)
                    if receipt is None:
                        return TradeResult(
                            success=False,
                            platform=token_info.platform,
                            tx_signature=position.pending_exit_signature,
                            error_message="Confirmed sell receipt accounting is unavailable",
                            amount=position.quantity,
                            amount_raw=position.quantity_raw,
                            fee_lamports=position.pending_exit_fee_lamports,
                            slot=outcome.slot,
                            status=TransactionStatus.UNKNOWN.value,
                        )
                    quote_raw, exit_price = receipt
                    fee_lamports = position.pending_exit_fee_lamports
                    signature = position.pending_exit_signature
                    await self._finalize_emergency_exit(
                        token_info,
                        position,
                        exit_price=exit_price,
                        tx_signature=signature,
                        sold_raw=position.quantity_raw,
                        quote_amount_raw=quote_raw,
                    )
                    return TradeResult(
                        success=True,
                        platform=token_info.platform,
                        tx_signature=signature,
                        amount=position.quantity,
                        amount_raw=position.quantity_raw,
                        price=exit_price,
                        quote_amount_raw=quote_raw,
                        fee_lamports=fee_lamports,
                        slot=outcome.slot,
                        status=TransactionStatus.SUCCESS.value,
                    )
                if outcome.status is TransactionStatus.REVERTED:
                    if position.pending_exit_fee_lamports is None:
                        raise RuntimeError(
                            "Reverted pending emergency sell has no durable fee"
                        )
                    position.record_charged_exit_fee(position.pending_exit_fee_lamports)
                position.clear_pending_exit()
                self._persist_position(token_info, position)

            calculate_token_price = getattr(
                curve_manager, "calculate_token_price", None
            )
            if callable(calculate_token_price):
                current_price = await calculate_token_price(token_info)
            else:
                pool_address = self._get_pool_address(token_info)
                current_price = await curve_manager.calculate_price(pool_address)
            if (
                isinstance(current_price, bool)
                or not isinstance(current_price, int | float)
                or not isfinite(current_price)
                or current_price <= 0
            ):
                raise ValueError(  # noqa: TRY003
                    "Platform returned an invalid emergency-exit price"
                )
            if not position.position_id:
                raise ValueError(  # noqa: TRY003
                    "Emergency exit requires a durable position id"
                )
            attempt_sequence = position.next_exit_attempt()
            intent_id = f"emergency-sell:{position.position_id}:{attempt_sequence}"
            position.mark_exit_intent(
                intent_id,
                ExitReason.MANUAL,
                float(current_price),
            )
            self._persist_position(token_info, position)
            self._record_trade_evidence(
                "decision",
                token_info,
                position=position,
                action="sell",
                intent_id=intent_id,
                reason=ExitReason.MANUAL.value,
                trigger_price=current_price,
            )
            result = await self.seller.execute(
                token_info,
                token_amount=position.quantity,
                token_price=float(current_price),
                token_amount_raw=position.quantity_raw,
                intent_id=intent_id,
            )
            if self._defer_unpriced_sell_result(token_info, position, result):
                return result
            if result.success:
                await self._finalize_emergency_exit(
                    token_info,
                    position,
                    exit_price=result.price,
                    tx_signature=result.tx_signature,
                    sold_raw=result.amount_raw,
                    quote_amount_raw=result.quote_amount_raw,
                )
                return result
            if result.unresolved:
                result.price = None
                result.quote_amount_raw = None
            if result.unresolved and result.tx_signature:
                if result.fee_lamports is None:
                    raise RuntimeError(
                        "Unresolved emergency sell has no durable transaction fee"
                    )
                position.mark_exit_pending(
                    result.tx_signature,
                    ExitReason.MANUAL,
                    fee_lamports=result.fee_lamports,
                )
                self._persist_position(token_info, position)
                return result
            if result.status == TransactionStatus.REVERTED.value:
                if result.fee_lamports is None:
                    raise RuntimeError(
                        "Reverted emergency sell has no durable transaction fee"
                    )
                position.record_charged_exit_fee(result.fee_lamports)
            position.clear_pending_exit()
            self._persist_position(token_info, position)
            return result
        finally:
            await self._cleanup_resources()

    async def start(self, *, resume_only: bool = False) -> None:
        """Start trading or monitor recovered positions after orderly recovery."""
        logger.info(f"Starting Universal Trader for {self.platform.value}")
        logger.info(f"Exit strategy: {self.exit_strategy}")
        processor_task: asyncio.Task | None = None
        listener_task: asyncio.Task | None = None
        monitor_failure_task: asyncio.Task | None = None
        monitor_group_task: asyncio.Future | None = None
        token_wait_task: asyncio.Task | None = None
        primary_error: BaseException | None = None
        primary_traceback = None

        try:
            if resume_only:
                if self.yolo_mode:
                    raise RuntimeError("Resume-only mode does not support yolo mode")
                if self._pending_recovery_tokens or self._unresolved_buys:
                    raise RuntimeError(
                        "Resume-only mode requires no pending or unresolved buys"
                    )
                if not self._active_positions:
                    raise RuntimeError(
                        "Resume-only mode requires at least one active position"
                    )
                if any(
                    not self.has_automatic_exit(position)
                    for _, position in self._active_positions.values()
                ):
                    raise RuntimeError(
                        "Resume-only mode requires an automatic exit "
                        "for every active position"
                    )
                logger.info(
                    "Resume-only mode: monitoring %d journaled position(s)",
                    len(self._active_positions),
                )
            if self.platform is Platform.PUMP_FUN:
                curve_manager = getattr(
                    getattr(self, "platform_implementations", None),
                    "curve_manager",
                    None,
                )
                prepare_fee_schedule = getattr(
                    curve_manager,
                    "prepare_live_execution",
                    None,
                )
                if not callable(prepare_fee_schedule):
                    raise RuntimeError(
                        "Pump.fun execution requires fee attestation support"
                    )
                await prepare_fee_schedule()
            await self._resume_ledger_bound_submissions()
            if not resume_only:
                await self._resume_staged_cleanups()
            processor_task = asyncio.create_task(self._process_token_queue())
            reconciliation_task = asyncio.create_task(self._reconcile_unresolved_buys())
            self._position_tasks.add(reconciliation_task)

            def _reconciliation_finished(done_task: asyncio.Task) -> None:
                self._position_tasks.discard(done_task)
                if done_task.cancelled():
                    return
                error = done_task.exception()
                if error is not None:
                    self._fatal_monitor_errors.put_nowait(error)
                    logger.error(
                        "Unresolved-buy reconciliation stopped unexpectedly",
                        exc_info=(type(error), error, error.__traceback__),
                    )

            reconciliation_task.add_done_callback(_reconciliation_finished)
            monitor_failure_task = asyncio.create_task(self._fatal_monitor_errors.get())

            for token_info, position in tuple(self._active_positions.values()):
                self._schedule_position_monitor(token_info, position)
            for token_info in tuple(self._pending_recovery_tokens):
                queued = await self._queue_token(token_info, recovered=True)
                token_key = str(token_info.mint)
                if not queued and (
                    token_key in self._active_positions
                    or token_key in self._unresolved_buys
                ):
                    self._finish_token_reservation(token_info, handled=False)
            self._write_recovery_journal()

            try:
                health_resp = await self.solana_client.get_health()
                logger.info(f"RPC warm-up successful (getHealth passed: {health_resp})")
            except Exception as exc:
                logger.warning(f"RPC warm-up failed: {exc!s}")

            async def await_position_monitors() -> None:
                nonlocal monitor_group_task
                automatic_monitors = tuple(self._position_monitor_tasks)
                if not automatic_monitors:
                    return
                monitor_group_task = asyncio.gather(*automatic_monitors)
                done, _ = await asyncio.wait(
                    {monitor_group_task, monitor_failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if monitor_group_task in done:
                    await monitor_group_task
                else:
                    raise await monitor_failure_task

            def hold_positions_after_listener_failure(exc: BaseException) -> None:
                # A dead listener only stops new buys. Held positions keep their
                # monitors so tp/sl exits still fire; the failure is re-raised
                # once every monitor has finished.
                if not self._position_monitor_tasks:
                    raise exc
                logger.error(
                    "Token listener failed; holding %d position(s) until exit",
                    len(self._position_monitor_tasks),
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

            listener_error: BaseException | None = None
            if not self.yolo_mode:
                await self._await_queue_drain(
                    processor_task,
                    monitor_failure_task,
                )
                if not resume_only:
                    # One-shot means one buy, not one detection: an entry gate
                    # skips most coins, so keep taking coins until a buy is
                    # attempted or the wait budget is spent.
                    wait_deadline = monotonic() + self.token_wait_timeout
                    attempts_before = getattr(self, "_buy_attempts", 0)
                    while True:
                        if monotonic() >= wait_deadline:
                            logger.info(
                                "Token wait budget of %ss spent without a buy",
                                self.token_wait_timeout,
                            )
                            break
                        token_wait_task = asyncio.create_task(self._wait_for_token())
                        done, _ = await asyncio.wait(
                            {token_wait_task, monitor_failure_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if monitor_failure_task in done:
                            monitor_error = await monitor_failure_task
                            raise monitor_error
                        token_info = None
                        try:
                            token_info = await token_wait_task
                        except Exception as exc:
                            listener_error = exc
                            break
                        if token_info is None:
                            break
                        attempts_before_token = getattr(self, "_buy_attempts", 0)
                        handled = False
                        try:
                            handled = await self._handle_token(token_info)
                        finally:
                            self._finish_token_reservation(token_info, handled)
                        token_key_check = str(token_info.mint)
                        if (
                            self.execution_policy.mode is ExecutionMode.DRY_RUN
                            and handled
                            and getattr(self, "_buy_attempts", 0)
                            > attempts_before_token
                            and token_key_check not in self._active_positions
                            and token_key_check not in self._unresolved_buys
                            and not self._position_monitor_tasks
                        ):
                            # A blocked attempt is not a fill. Gate observations
                            # are scheduled separately, with no executable-PnL claim.
                            logger.info(
                                "Dry-run attempt complete for %s; continuing scan",
                                token_info.symbol,
                            )
                            continue
                        if str(token_info.mint) in self._unresolved_buys:
                            await self._await_unresolved_buy_resolution(
                                str(token_info.mint),
                                monitor_failure_task,
                            )
                        token_key = str(token_info.mint)
                        skipped = (
                            handled
                            and getattr(self, "_buy_attempts", 0)
                            == attempts_before_token
                            and token_key not in self._active_positions
                            and token_key not in self._unresolved_buys
                            and not self._position_monitor_tasks
                        )
                        if not skipped:
                            break
                    if monitor_failure_task.done():
                        monitor_error = await monitor_failure_task
                        raise monitor_error
                    if listener_error is not None:
                        hold_positions_after_listener_failure(listener_error)
                await await_position_monitors()
                # Stop accepting entries at the existing deadline, but finish
                # bounded read-only marks before normal resource cleanup.
                if self.execution_policy.mode is ExecutionMode.DRY_RUN:
                    await self._drain_paper_marks()
                if listener_error is not None:
                    raise listener_error
            else:
                listener_task = asyncio.create_task(
                    self.token_listener.listen_for_tokens(
                        lambda token: self._queue_token(token),
                        self.match_string,
                        self.bro_address,
                    )
                )
                done, _ = await asyncio.wait(
                    {listener_task, processor_task, monitor_failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if monitor_failure_task in done:
                    monitor_error = await monitor_failure_task
                    raise monitor_error
                if processor_task in done:
                    await processor_task
                    raise RuntimeError("Token queue processor stopped unexpectedly")
                try:
                    await listener_task
                    raise RuntimeError("Token listener stopped unexpectedly")
                except Exception as exc:
                    listener_error = exc
                await self._await_queue_drain(
                    processor_task,
                    monitor_failure_task,
                )
                if monitor_failure_task.done():
                    monitor_error = await monitor_failure_task
                    raise monitor_error
                hold_positions_after_listener_failure(listener_error)
                await await_position_monitors()
                raise listener_error
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
        finally:
            self._shutdown_event.set()
            shutdown_errors: list[tuple[str, BaseException]] = []
            if token_wait_task is not None:
                if not token_wait_task.done():
                    token_wait_task.cancel()
                try:
                    await token_wait_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("token wait", exc))

            if monitor_group_task is not None:
                if not monitor_group_task.done():
                    monitor_group_task.cancel()
                try:
                    await monitor_group_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("position monitor group", exc))

            if listener_task is not None:
                if not listener_task.done():
                    listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("token listener", exc))

            if processor_task is not None:
                if not processor_task.done():
                    processor_task.cancel()
                try:
                    await processor_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if exc is not primary_error:
                        shutdown_errors.append(("token queue processor", exc))

            try:
                await self._cleanup_resources()
            except BaseException as exc:
                shutdown_errors.append(("resource cleanup", exc))

            await asyncio.sleep(0)
            monitor_errors: list[BaseException] = []
            if monitor_failure_task is not None:
                if not monitor_failure_task.done():
                    monitor_failure_task.cancel()
                try:
                    monitor_error = await monitor_failure_task
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    monitor_errors.append(exc)
                else:
                    monitor_errors.append(monitor_error)
            while not self._fatal_monitor_errors.empty():
                monitor_errors.append(self._fatal_monitor_errors.get_nowait())
            for monitor_error in monitor_errors:
                if primary_error is None:
                    primary_error = monitor_error
                    primary_traceback = monitor_error.__traceback__
                elif monitor_error is not primary_error:
                    shutdown_errors.append(("position monitor", monitor_error))

            if primary_error is None and shutdown_errors:
                _, primary_error = shutdown_errors.pop(0)
                primary_traceback = primary_error.__traceback__
            for stage, error in shutdown_errors:
                logger.error(
                    "Secondary failure during %s",
                    stage,
                    exc_info=(type(error), error, error.__traceback__),
                )
            if self._active_positions:
                logger.warning(
                    "%d active position(s) remain journaled and unmonitored: %s",
                    len(self._active_positions),
                    ", ".join(self._active_positions),
                )
            logger.info("Universal Trader has shut down")

        if primary_error is not None:
            raise primary_error.with_traceback(primary_traceback)

    async def _wait_for_token(
        self, *, timeout: float | None = None
    ) -> TokenInfo | None:
        """Wait for and atomically reserve a single token mint.

        With a trade hub the listener stream is kept alive across calls (the
        gate and the hold need it), so a second call reuses it instead of
        opening another Geyser subscription.
        """
        self._oneshot_found = None
        self._oneshot_event = asyncio.Event()

        async def token_callback(token: TokenInfo) -> None:
            token_key = str(token.mint)
            async with self._queue_lock:
                if (
                    self._oneshot_found is not None
                    or token_key in self.processed_tokens
                    or token_key in self._reserved_mints
                ):
                    return
                self._reserved_mints.add(token_key)
                self.token_timestamps[token_key] = monotonic()
                self._subscribe_trades(token_key)
                self._oneshot_found = token
                self._oneshot_event.set()

        kept_task = getattr(self, "_listener_task", None)
        if kept_task is not None and not kept_task.done():
            listener_task = kept_task
        else:
            listener_task = asyncio.create_task(
                self.token_listener.listen_for_tokens(
                    token_callback,
                    self.match_string,
                    self.bro_address,
                )
            )
        token_found_task = asyncio.create_task(self._oneshot_event.wait())
        try:
            done, _ = await asyncio.wait(
                {token_found_task, listener_task},
                timeout=self.token_wait_timeout if timeout is None else timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            found_token = self._oneshot_found
            if listener_task in done:
                await listener_task
                if found_token is None:
                    raise RuntimeError(
                        "Token listener stopped before detecting a token"
                    )
            if token_found_task in done or found_token is not None:
                if (
                    getattr(self, "trade_hub", None) is not None
                    and not listener_task.done()
                ):
                    # The hub needs this stream through the gate and the hold;
                    # later creations are ignored by the callback above.
                    self._listener_task = listener_task
                    self._position_tasks.add(listener_task)
                    listener_task.add_done_callback(self._position_tasks.discard)
                return found_token
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a token"
            )
            return None
        finally:
            kept = getattr(self, "_listener_task", None)
            keep = {kept} if kept is not None else set()
            for task in (token_found_task, listener_task):
                if task not in keep and not task.done():
                    task.cancel()
            for task in (token_found_task, listener_task):
                if task in keep:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def close(self) -> None:
        """Close all runtime resources without starting the trading loop."""
        await self._cleanup_resources()

    async def _cleanup_resources(self) -> None:
        """Persist unfinished work, then attempt every independent cleanup."""
        failures: list[tuple[str, BaseException]] = []

        def record_failure(stage: str, error: BaseException) -> None:
            failures.append((stage, error))

        try:
            pending_tokens = {
                str(token_info.mint): token_info
                for token_info in self._pending_recovery_tokens
            }
            pending_tokens.update(
                {
                    token_key: token_info
                    for token_key, token_info in self._inflight_tokens.items()
                    if token_key not in self._active_positions
                    and token_key not in self._unresolved_buys
                }
            )
            while True:
                try:
                    token_info = self.token_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                pending_tokens[str(token_info.mint)] = token_info
                self.token_queue.task_done()
            self._pending_recovery_tokens = list(pending_tokens.values())
            self._write_recovery_journal()
        except BaseException as exc:
            record_failure("recovery journal", exc)

        if self._position_tasks:
            background_tasks = tuple(self._position_tasks)
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            try:
                task_results = await asyncio.gather(
                    *background_tasks,
                    return_exceptions=True,
                )
            except BaseException as exc:
                record_failure("background tasks", exc)
            else:
                for result in task_results:
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        record_failure("background task", result)

        try:
            protected_mints = {
                Pubkey.from_string(token_key)
                for token_key in (
                    self._active_positions.keys() | self._unresolved_buys.keys()
                )
            }
            cleanup_mints = self.traded_mints - protected_mints
        except BaseException as exc:
            record_failure("cleanup planning", exc)
            cleanup_mints = set()

        if cleanup_mints:
            try:
                mints_list = list(cleanup_mints)
                token_program_ids = [
                    self.traded_token_programs.get(str(mint)) for mint in mints_list
                ]
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    mints_list,
                    token_program_ids,
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
            except BaseException as exc:
                record_failure("post-session cleanup", exc)

        curve_manager = getattr(
            getattr(self, "platform_implementations", None),
            "curve_manager",
            None,
        )
        close_curve_manager = getattr(curve_manager, "close", None)
        if callable(close_curve_manager):
            try:
                await close_curve_manager()
            except BaseException as exc:
                record_failure("curve manager close", exc)

        try:
            await self.solana_client.close()
        except BaseException as exc:
            record_failure("Solana client close", exc)

        if self.jev_scorer is not None:
            try:
                await self.jev_scorer.close()
            except BaseException as exc:  # noqa: BLE001 - shutdown must not raise
                record_failure("Jev scorer close", exc)
        if self.lesson_journal is not None:
            try:
                self.lesson_journal.close()
            except BaseException as exc:  # noqa: BLE001 - shutdown must not raise
                record_failure("lesson journal close", exc)
        failures.extend(self._release_persistence_resources())

        if failures:
            _, first_error = failures[0]
            for stage, error in failures[1:]:
                logger.error(
                    "Secondary failure during %s",
                    stage,
                    exc_info=(type(error), error, error.__traceback__),
                )
            raise first_error.with_traceback(first_error.__traceback__)

    async def _queue_token(
        self, token_info: TokenInfo, *, recovered: bool = False
    ) -> bool:
        """Atomically coalesce duplicate mints into a bounded queue."""
        token_key = str(token_info.mint)
        async with self._queue_lock:
            if token_key in self.processed_tokens or token_key in self._reserved_mints:
                return False
            self._reserved_mints.add(token_key)
            if not recovered:
                self._subscribe_trades(token_key)
            queued_at = monotonic()
            if recovered:
                creation_timestamp = self._validate_creation_timestamp(
                    token_info.creation_timestamp
                )
                if creation_timestamp is None or creation_timestamp > queued_at:
                    creation_timestamp = queued_at - max(self.max_token_age, 0) - 1
                queued_at = creation_timestamp
            self.token_timestamps[token_key] = queued_at
            try:
                self.token_queue.put_nowait(token_info)
            except asyncio.QueueFull:
                self._reserved_mints.discard(token_key)
                self.token_timestamps.pop(token_key, None)
                logger.warning(
                    f"Token queue full; dropped {token_info.symbol} ({token_info.mint})"
                )
                return False
        logger.info(
            f"Queued {'recovered ' if recovered else ''}token: "
            f"{token_info.symbol} ({token_info.mint})"
        )
        return True

    def _subscribe_trades(self, token_key: str) -> None:
        """Start collecting a reserved mint's trades before any are missed."""
        hub = getattr(self, "trade_hub", None)
        if hub is None or token_key in self._gate_queues:
            return
        self._gate_queues[token_key] = hub.subscribe(token_key)

    def _unsubscribe_trades(self, token_key: str) -> None:
        queue = getattr(self, "_gate_queues", {}).pop(token_key, None)
        hub = getattr(self, "trade_hub", None)
        if queue is not None and hub is not None:
            hub.unsubscribe(token_key, queue)

    def _finish_token_reservation(self, token_info: TokenInfo, handled: bool) -> None:
        """Release a mint only when it is safe for another callback to claim."""
        token_key = str(token_info.mint)
        has_durable_state = (
            token_key in self._active_positions or token_key in self._unresolved_buys
        )
        pending_count = len(self._pending_recovery_tokens)
        if handled:
            self.processed_tokens.add(token_key)
        if handled or has_durable_state:
            self._pending_recovery_tokens = [
                pending
                for pending in self._pending_recovery_tokens
                if str(pending.mint) != token_key
            ]
        pending_changed = len(self._pending_recovery_tokens) != pending_count
        has_pending_recovery = any(
            str(pending.mint) == token_key for pending in self._pending_recovery_tokens
        )
        if not has_durable_state and not has_pending_recovery:
            self._reserved_mints.discard(token_key)
        if token_key not in self._active_positions:
            self._unsubscribe_trades(token_key)
        self.token_timestamps.pop(token_key, None)
        if handled or pending_changed:
            self._write_recovery_journal()

    async def _process_token_queue(self) -> None:
        """Process claimed queue items and acknowledge only actual claims."""
        while True:
            claimed = False
            token_info: TokenInfo | None = None
            handled = False
            try:
                token_info = await self.token_queue.get()
                claimed = True
                token_key = str(token_info.mint)
                self._inflight_tokens[token_key] = token_info
                token_age = monotonic() - self.token_timestamps.get(
                    token_key, monotonic()
                )
                if token_age > self.max_token_age:
                    logger.info(
                        f"Skipping stale token {token_info.symbol} "
                        f"({token_age:.1f}s > {self.max_token_age}s)"
                    )
                    self._record_trade_evidence(
                        "decision", token_info, action="skip", reason="stale_token"
                    )
                    handled = True
                else:
                    handled = await self._handle_token(token_info)
            except asyncio.CancelledError:
                if (
                    claimed
                    and token_info is not None
                    and str(token_info.mint) not in self._active_positions
                    and str(token_info.mint) not in self._unresolved_buys
                    and all(
                        pending.mint != token_info.mint
                        for pending in self._pending_recovery_tokens
                    )
                ):
                    self._pending_recovery_tokens.append(token_info)
                    self._write_recovery_journal()
                logger.info("Token queue processor was cancelled")
                raise
            except ExecutionBlocked as exc:
                if self.execution_policy.mode is ExecutionMode.DRY_RUN:
                    logger.info(
                        "Paper fill blocked by dry-run gate: %s — "
                        "continuing to next token",
                        exc,
                    )
                    continue
                logger.exception("Fatal error in token queue processor")
                raise
            except Exception:
                logger.exception("Fatal error in token queue processor")
                raise
            finally:
                if claimed and token_info is not None:
                    self._inflight_tokens.pop(str(token_info.mint), None)
                    self._finish_token_reservation(token_info, handled)
                    self.token_queue.task_done()

    def _schedule_paper_marks(
        self,
        token_info: TokenInfo,
        entry_id: int | None,
        event: TradeEvent | None,
        started: float,
    ) -> None:
        """Observe an accepted gate decision; do not pretend a blocked buy filled."""
        if entry_id is None:
            message = "Paper gate evidence was not saved"
            raise EvidencePersistenceError(message)
        entry_price = None
        if (
            token_info.platform is Platform.PUMP_FUN
            and is_sol_paired(token_info.quote_mint)
            and event is not None
            and event.mint == str(token_info.mint)
            and event.virtual_sol_reserves > 0
            and event.virtual_token_reserves > 0
        ):
            entry_price = event.price
        self.lesson_journal.start_paper_marks(entry_id, entry_price)
        for horizon in PAPER_HORIZONS:
            if entry_price is None:
                self.lesson_journal.finish_paper_mark(
                    entry_id,
                    horizon,
                    elapsed_s=monotonic() - started,
                    reason="unsupported_or_missing_entry",
                )
                continue
            task = asyncio.create_task(
                self._paper_mark(token_info, entry_id, horizon, started)
            )
            self._paper_tasks.add(task)
            self._position_tasks.add(task)
            task.add_done_callback(
                lambda done, h=horizon: self._paper_mark_finished(
                    done, entry_id, h, started
                )
            )

    def _paper_mark_finished(
        self, task: asyncio.Task, entry_id: int, horizon_s: int, started: float
    ) -> None:
        self._paper_tasks.discard(task)
        self._position_tasks.discard(task)
        try:
            if task.cancelled():
                self.lesson_journal.finish_paper_mark(
                    entry_id,
                    horizon_s,
                    elapsed_s=monotonic() - started,
                    reason="cancelled",
                )
            elif task.exception() is not None:
                self._fatal_monitor_errors.put_nowait(task.exception())
        except Exception as exc:  # noqa: BLE001 - forward persistence failures to shutdown
            self._fatal_monitor_errors.put_nowait(exc)

    async def _drain_paper_marks(self) -> None:
        """Finish existing horizons, without accepting entries or increasing caps."""
        tasks = tuple(getattr(self, "_paper_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks)

    async def _paper_mark(
        self, token_info: TokenInfo, entry_id: int, horizon_s: int, started: float
    ) -> None:
        """Record comparable virtual-reserve marks, or an explicit missing outcome."""
        await asyncio.sleep(max(0.0, started + horizon_s - monotonic()))
        price = None
        evidence = None
        reason = "late"
        remaining = started + horizon_s + PAPER_MARK_MAX_LATENESS_S - monotonic()
        if remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    (
                        state,
                        _,
                    ) = await self.platform_implementations.curve_manager.get_pool_state_and_token_program(
                        token_info.bonding_curve,
                        token_info.mint,
                        commitment="processed",
                    )
                evidence = {
                    key: state.get(key)
                    for key in (
                        "virtual_quote_reserves",
                        "virtual_token_reserves",
                        "real_quote_reserves",
                        "real_token_reserves",
                        "complete",
                    )
                }
                evidence["quote_mint"] = str(state.get("quote_mint"))
                if state.get("complete") is not False:
                    reason = "migrated_or_invalid_completion"
                elif state.get("is_sol_paired") is not True:
                    reason = "unsupported_quote"
                else:
                    candidate = state.get("price_per_token")
                    if (
                        not isinstance(candidate, bool)
                        and isinstance(candidate, int | float)
                        and isfinite(candidate)
                        and candidate > 0
                    ):
                        price = float(candidate)
                        reason = "gross_virtual_reserve_mark"
                    else:
                        reason = "invalid_price"
            except Exception as exc:  # noqa: BLE001 - explicit censored read, not success
                # No retries or transport text that could disclose credentials.
                reason = f"read_error:{type(exc).__name__}"
        elapsed = monotonic() - started
        if elapsed > horizon_s + PAPER_MARK_MAX_LATENESS_S:
            price, reason = None, "late"
        self.lesson_journal.finish_paper_mark(
            entry_id,
            horizon_s,
            elapsed_s=elapsed,
            exit_price=price,
            reason=reason,
            exit_state=evidence,
        )

    async def _handle_token(self, token_info: TokenInfo) -> bool:
        """Handle a token, returning true only after resolved handling."""
        try:
            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                self._record_trade_evidence(
                    "decision", token_info, action="skip", reason="platform_mismatch"
                )
                return True

            # An unverified Pump listener value is only a hint. PumpPortal omits
            # the quote mint entirely, and instruction-derived metadata can be
            # stale; the buyer enforces both the amount map and allowlist after
            # its authoritative curve refresh.
            token_quote_mint = None
            quote_metadata_is_authoritative = (
                token_info.platform is not Platform.PUMP_FUN
                or token_info.state_from_event
            )
            if quote_metadata_is_authoritative:
                token_quote_mint = normalize_quote_mint(token_info.quote_mint)
                if (
                    self.allowed_quote_mints is not None
                    and token_quote_mint not in self.allowed_quote_mints
                ):
                    logger.info(
                        f"Skipping {token_info.symbol} - quote mint "
                        f"{token_quote_mint} not in allowed_quote_mints"
                    )
                    self._record_trade_evidence(
                        "decision",
                        token_info,
                        action="skip",
                        reason="quote_not_allowed",
                    )
                    return True
                if token_quote_mint not in self.quote_amounts:
                    logger.info(
                        f"Skipping {token_info.symbol} - no buy amount configured "
                        f"for quote mint {token_quote_mint}"
                    )
                    self._record_trade_evidence(
                        "decision",
                        token_info,
                        action="skip",
                        reason="quote_amount_missing",
                    )
                    return True
            if not self.extreme_fast_mode:
                await self._save_token_info(token_info)
                logger.info(
                    f"Waiting for {self.wait_time_after_creation} seconds "
                    "for the pool/curve to stabilize..."
                )
                await asyncio.sleep(self.wait_time_after_creation)

            decision = await self._await_entry_gate(token_info)
            entry_started = monotonic()
            entry_lesson_id = None
            if self.lesson_journal is not None:
                jev = None
                if self.jev_scorer is not None and self.jev_scorer.enabled:
                    jev = await self.jev_scorer.score_candidate(
                        name=token_info.name,
                        symbol=token_info.symbol,
                        mayhem=token_info.is_mayhem_mode,
                        gate=GateSnapshot(
                            buyers=decision.buyers if decision else 0,
                            real_sol=decision.real_sol if decision else None,
                        ),
                    )
                entry_lesson_id = self.lesson_journal.record(
                    LessonObservation(
                        kind="gate_skip"
                        if decision and not decision.accept
                        else "gate_pass",
                        mint=str(token_info.mint),
                        symbol=token_info.symbol,
                        name=token_info.name,
                        platform=token_info.platform.value,
                        mayhem=token_info.is_mayhem_mode,
                        decision=decision.reason if decision else "entry_gate_disabled",
                        buyers=decision.buyers if decision else None,
                        real_sol=decision.real_sol if decision else None,
                        raw={"gate": asdict(decision)} if decision else {},
                    ),
                    jev=jev,
                )
            self._record_trade_evidence(
                "decision",
                token_info,
                action="buy" if decision is None or decision.accept else "skip",
                intent_id=self._buy_intent_id(token_info),
                reason=decision.reason
                if decision is not None
                else "entry_gate_disabled",
                gate=asdict(decision) if decision is not None else None,
            )
            if decision is not None and not decision.accept:
                return True

            if (
                self.execution_policy.mode is ExecutionMode.DRY_RUN
                and self.lesson_journal is not None
            ):
                self._schedule_paper_marks(
                    token_info,
                    entry_lesson_id,
                    decision.last_event if decision else None,
                    entry_started,
                )

            if token_quote_mint is None:
                logger.info(
                    f"Buying {token_info.symbol} on {token_info.platform.value} "
                    "after authoritative quote-asset refresh..."
                )
            else:
                logger.info(
                    f"Buying {self.quote_amounts[token_quote_mint]:.6f} of quote "
                    f"{token_quote_mint} worth of {token_info.symbol} "
                    f"on {token_info.platform.value}..."
                )
            token_key = str(token_info.mint)
            if all(
                str(pending.mint) != token_key
                for pending in self._pending_recovery_tokens
            ):
                self._pending_recovery_tokens.append(token_info)
                self._write_recovery_journal()
            self._buy_attempts = getattr(self, "_buy_attempts", 0) + 1
            buy_result: TradeResult = await self.buyer.execute(token_info)
            if buy_result.success:
                await self._handle_successful_buy(
                    token_info, buy_result, entry_lesson_id=entry_lesson_id
                )
                handled = True
            else:
                handled = await self._handle_failed_buy(
                    token_info, buy_result, entry_lesson_id=entry_lesson_id
                )
            self._pending_recovery_tokens = [
                pending
                for pending in self._pending_recovery_tokens
                if str(pending.mint) != token_key
            ]
            self._write_recovery_journal()
            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)
            return handled

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")
            raise

    async def _await_entry_gate(self, token_info: TokenInfo) -> GateDecision | None:
        """Hold the buy until the gate accepts, or skip. None when the gate is off.

        Uses the trades already streaming into this mint's hub queue since the
        reservation, so nothing from the creation slot onward is missed. On
        accept, the last event's reserves replace the CreateEvent's so the
        zero-RPC buy prices against the current curve, not the one at t=0.
        """
        rules = getattr(self, "gate_rules", None)
        if rules is None or token_info.platform is not Platform.PUMP_FUN:
            return None
        if rules.mayhem_only and not token_info.is_mayhem_mode:
            logger.info("Gate skip %s: not_mayhem", token_info.symbol)
            return GateDecision(False, "not_mayhem", 0, None, 0)
        token_key = str(token_info.mint)
        queue = self._gate_queues.get(token_key)
        if queue is None or token_info.slot is None:
            logger.warning("Gate skip %s: no_trade_stream", token_info.symbol)
            return GateDecision(False, "no_trade_stream", 0, None, 0)
        gate = EntryGate(
            mint=token_key,
            creator=str(token_info.creator) if token_info.creator else "",
            creation_slot=token_info.slot,
            rules=rules,
        )
        started = monotonic()
        deadline = started + rules.max_wait_ms / 1000
        decision: GateDecision | None = None
        if rules.min_buyers == 0 and not queue.loss_reason:
            decision = GateDecision(True, "no_wait", 0, None, 0)
        while decision is None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                decision = gate.timed_out()
                break
            try:
                # Keep dequeue and evaluation in one task so history loss cannot race
                # a wait_for child task that has already removed an older event.
                async with asyncio.timeout(remaining):
                    event = await queue.get()
            except TimeoutError:
                decision = gate.timed_out()
                break
            except TradeFlowLossError as exc:
                decision = GateDecision(
                    accept=False,
                    reason=f"trade_stream_{exc.reason}",
                    buyers=0,
                    real_sol=None,
                    slots_waited=0,
                )
                break
            decision = gate.observe(event)
        waited_ms = (monotonic() - started) * 1000
        last = decision.last_event
        if decision.accept and last is not None and token_info.state_from_event:
            token_info.virtual_quote_reserves = last.virtual_sol_reserves
            token_info.virtual_token_reserves = last.virtual_token_reserves
            # The trigger buyer's TradeEvent carries the freshest real
            # reserves on the curve; the CreateEvent values are stale by the
            # entire gate wait. Quote bounds computed from stale reserves are
            # what produced the live 6002 revert (see bound-displacement math).
            token_info.real_token_reserves = last.real_token_reserves
            token_info.real_sol_reserves = last.real_sol_reserves
        logger.info(
            "Gate %s %s: %s after %.0f ms, %d slot(s), buyers=%d real=%s",
            "accept" if decision.accept else "skip",
            token_info.symbol,
            decision.reason,
            waited_ms,
            decision.slots_waited,
            decision.buyers,
            f"{decision.real_sol:.3f}" if decision.real_sol is not None else "?",
        )
        return decision

    async def _handle_successful_buy(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        *,
        replace_unresolved: bool = False,
        entry_lesson_id: int | None = None,
    ) -> None:
        """Journal a confirmed holding before starting any exit monitor."""
        if token_info.slot is not None and buy_result.slot is not None:
            logger.info(
                "Buy for %s landed at creation+%d slot(s)",
                token_info.symbol,
                buy_result.slot - token_info.slot,
            )
        if (
            buy_result.amount is None
            or buy_result.amount <= 0
            or buy_result.price is None
            or buy_result.price <= 0
        ):
            raise ValueError("Successful buy result is missing receipt accounting")
        self._record_trade_evidence(
            "trade_result",
            token_info,
            result=buy_result,
            action="buy",
            intent_id=self._buy_intent_id(token_info),
        )
        self._log_trade(
            "buy",
            token_info,
            buy_result.price,
            buy_result.amount,
            buy_result.tx_signature,
        )
        take_profit = (
            self.take_profit_percentage if self.exit_strategy == "tp_sl" else None
        )
        stop_loss = self.stop_loss_percentage if self.exit_strategy == "tp_sl" else None
        if self.exit_strategy == "tp_sl":
            max_hold_time = self.max_hold_time
        elif self.exit_strategy == "time_based":
            max_hold_time = self.wait_time_after_buy
        else:
            max_hold_time = None
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=buy_result.price,
            quantity=buy_result.amount,
            take_profit_percentage=take_profit,
            stop_loss_percentage=stop_loss,
            max_hold_time=max_hold_time,
            quantity_raw=buy_result.amount_raw,
            quote_amount_raw=buy_result.quote_amount_raw,
            buy_fee_lamports=buy_result.fee_lamports,
            account_balance_baseline_raw=(buy_result.account_balance_baseline_raw),
            position_id=buy_result.tx_signature
            or f"{token_info.platform.value}:{token_info.mint}",
            entry_lesson_id=entry_lesson_id,
        )
        token_key = str(token_info.mint)
        unresolved_record = (
            self._unresolved_buys.pop(token_key, None) if replace_unresolved else None
        )
        try:
            self._persist_position(token_info, position)
        except Exception:
            self._active_positions.pop(token_key, None)
            if unresolved_record is not None:
                self._unresolved_buys[token_key] = unresolved_record
            raise
        logger.info(f"Journaled active position: {position}")
        if self.marry_mode or self.exit_strategy == "manual":
            logger.info("Position retained for manual exit")
            return
        self._schedule_position_monitor(token_info, position)

    async def _handle_failed_buy(
        self,
        token_info: TokenInfo,
        buy_result: TradeResult,
        *,
        entry_lesson_id: int | None = None,
    ) -> bool:
        """Keep unknown buys unresolved; clean up only terminal failures."""
        logger.error(f"Failed to buy {token_info.symbol}: {buy_result.error_message}")
        self._record_trade_evidence(
            "trade_result",
            token_info,
            result=buy_result,
            action="buy",
            intent_id=self._buy_intent_id(token_info),
        )
        if (
            buy_result.unresolved
            or buy_result.status == TransactionStatus.SUCCESS.value
        ):
            if not buy_result.tx_signature:
                raise RuntimeError("Unresolved buy has no transaction signature")
            token_key = str(token_info.mint)
            self._unresolved_buys[token_key] = {
                "token": token_info,
                "signature": buy_result.tx_signature,
                "baseline_raw": buy_result.account_balance_baseline_raw,
                "entry_lesson_id": entry_lesson_id,
            }
            self._reserved_mints.add(token_key)
            self._write_recovery_journal()
            logger.warning(
                f"Buy outcome unresolved for {token_info.symbol}; "
                "cleanup and duplicate submission are blocked"
            )
            return False

        await handle_cleanup_after_failure(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )
        return True

    async def _reconcile_unresolved_buys(self) -> None:
        """Resolve prior signatures without submitting a duplicate buy."""
        while not self._shutdown_event.is_set():
            for token_key, record in tuple(self._unresolved_buys.items()):
                checks = record.get("reconcile_checks", 0) + 1
                record["reconcile_checks"] = checks
                try:
                    signature = record["signature"]
                    outcome = await self.solana_client.confirm_transaction_outcome(
                        signature
                    )
                    self._record_trade_evidence(
                        "chain_outcome",
                        record["token"],
                        action="buy",
                        signature=signature,
                        status=outcome.status.value,
                        slot=outcome.slot,
                    )
                    if outcome.status is TransactionStatus.UNKNOWN:
                        self._log_still_unresolved(token_key, checks, "unknown")
                        continue
                    if outcome.status is not TransactionStatus.SUCCESS:
                        self._unresolved_buys.pop(token_key, None)
                        self._reserved_mints.discard(token_key)
                        self.processed_tokens.add(token_key)
                        self._write_recovery_journal()
                        self._unresolved_buy_state_changed.set()
                        continue

                    token_info = record["token"]
                    quote_mint = normalize_quote_mint(token_info.quote_mint)
                    quote_destinations: list[Pubkey] | None = None
                    if is_sol_paired(quote_mint):
                        durable_destinations = await self.solana_client.get_submission_receipt_destinations(
                            signature
                        )
                        if not durable_destinations:
                            if not record.get("missing_receipt_context_logged"):
                                logger.error(
                                    "Cannot reconcile native buy %s without exact "
                                    "durable receipt destinations",
                                    signature,
                                )
                                record["missing_receipt_context_logged"] = True
                            continue
                        destination = durable_destinations[0]
                        quote_destinations = list(durable_destinations[1:])
                    else:
                        implementations = get_platform_implementations(
                            token_info.platform, self.solana_client
                        )
                        destination = self.buyer._get_sol_destination(
                            token_info, implementations.address_provider
                        )
                    (
                        tokens_raw,
                        quote_spent_raw,
                    ) = await self.solana_client.get_buy_transaction_details(
                        signature,
                        token_info.mint,
                        destination,
                        quote_mint=quote_mint,
                        quote_destinations=quote_destinations,
                    )
                    if (
                        isinstance(tokens_raw, bool)
                        or not isinstance(tokens_raw, int)
                        or tokens_raw <= 0
                        or isinstance(quote_spent_raw, bool)
                        or not isinstance(quote_spent_raw, int)
                        or quote_spent_raw <= 0
                    ):
                        self._log_still_unresolved(
                            token_key, checks, "confirmed but receipt unreadable"
                        )
                        continue
                    base_decimals = token_info.base_decimals
                    if base_decimals is None:
                        if token_info.platform is not Platform.PUMP_FUN:
                            raise ValueError(
                                "Cannot reconcile position without base decimals"
                            )
                        base_decimals = TOKEN_DECIMALS
                    if (
                        isinstance(base_decimals, bool)
                        or not isinstance(base_decimals, int)
                        or not 0 <= base_decimals <= 18
                    ):
                        raise ValueError("Invalid base decimals in recovery token")
                    token_info.base_decimals = base_decimals
                    token_amount = tokens_raw / 10**base_decimals
                    quote_unit = quote_units_per_token(quote_mint)
                    average_price = (quote_spent_raw / quote_unit) / token_amount
                    if self.transaction_ledger is None:
                        raise RuntimeError(
                            "Cannot recover buy fee without a transaction ledger"
                        )
                    submission_record = (
                        self.transaction_ledger.get_active_submission_record(
                            self._buy_intent_id(token_info)
                        )
                    )
                    if (
                        submission_record is None
                        or submission_record.signature != signature
                        or submission_record.fee_lamports is None
                    ):
                        raise RuntimeError(
                            "Cannot recover confirmed buy fee from transaction ledger"
                        )
                    await self._handle_successful_buy(
                        token_info,
                        TradeResult(
                            success=True,
                            platform=token_info.platform,
                            tx_signature=signature,
                            amount=token_amount,
                            price=average_price,
                            amount_raw=tokens_raw,
                            quote_amount_raw=quote_spent_raw,
                            fee_lamports=submission_record.fee_lamports,
                            account_balance_baseline_raw=record.get("baseline_raw"),
                            slot=outcome.slot,
                            status=outcome.status.value,
                        ),
                        replace_unresolved=True,
                        entry_lesson_id=record.get("entry_lesson_id"),
                    )
                    self.processed_tokens.add(token_key)
                    self._unresolved_buy_state_changed.set()
                except RpcUnavailableError:
                    self._log_still_unresolved(token_key, checks, "rpc unavailable")
                except Exception:
                    # A confirmed buy that cannot be reconciled is held tokens
                    # with no Position and no exit monitor: fail loudly. The
                    # record stays journaled and blocks resume-only until the
                    # operator resolves it.
                    logger.exception(
                        "Failed to reconcile unresolved buy for %s (check %d)",
                        token_key,
                        checks,
                    )
                    raise
            await self._reconcile_provisional_outcomes()
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=max(1, self.price_check_interval),
                )
            except TimeoutError:
                pass

    async def _reconcile_provisional_outcomes(self) -> None:
        """Re-read confirmed-only outcomes at finality and apply a correction.

        A finalized answer may supersede a confirmation, and that is the only
        moment a trade a fork dropped can still be corrected. Runs on the
        unresolved-buy tick, so an idle session costs one local ledger read
        and no RPC at all.
        """
        ledger = getattr(self, "transaction_ledger", None)
        wallet = getattr(self, "wallet", None)
        if ledger is None or wallet is None:
            return
        provisional = await asyncio.to_thread(
            ledger.list_provisional_outcomes,
            str(wallet.pubkey),
        )
        for row in provisional:
            signature = row["signature"]
            try:
                await self.solana_client.confirm_transaction_outcome(
                    signature, commitment="finalized"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one signature must not stop the rest
                logger.warning(
                    "Provisional re-read failed for %s: %s", signature[:16], exc
                )
                continue
            corrected = await asyncio.to_thread(
                self.transaction_ledger.get_outcome, signature
            )
            if corrected is None or corrected.status.value == row["status"]:
                continue
            self._apply_provisional_correction(row, corrected)

    def _apply_provisional_correction(
        self, row: dict[str, str], outcome: TransactionOutcome
    ) -> None:
        """Release a fork-removed buy, or escalate every other correction.

        Handled here: a buy that finality reveals as failed or expired, whose
        position still holds nothing. Deliberately NOT handled here: a
        reverted-to-success buy, and any sell correction - the sell's position
        was already removed and its cleanup may already have run, and the
        cleanup journal is not safely reversible. Those are escalated loudly
        for manual review instead of guessed at with real tokens.
        """
        signature = row["signature"]
        # Only a confirmed buy that finality proves never executed is handled
        # automatically; every other direction escalates at the bottom.
        automated = (
            outcome.status is not TransactionStatus.SUCCESS
            and row["status"] == "success"
        )
        for token_key, (token_info, position) in tuple(self._active_positions.items()):
            if position.position_id != signature or not automated:
                continue
            logger.error(
                "Fork correction: buy %s for %s is %s at finality, not %s;"
                " releasing the position that never received tokens",
                signature[:16],
                token_key,
                outcome.status.value,
                row["status"],
            )
            self._record_trade_evidence(
                "chain_outcome",
                token_info,
                position=position,
                action="buy",
                signature=signature,
                status=outcome.status.value,
                slot=outcome.slot,
                reason="fork_correction",
            )
            position.is_active = False
            self._active_positions.pop(token_key, None)
            # The mint stays reserved and processed: it was traded, and
            # re-entering a coin whose buy never landed is exactly the
            # duplicate this pass exists to prevent.
            self._reserved_mints.add(token_key)
            self.processed_tokens.add(token_key)
            self._write_recovery_journal()
            return
        logger.error(
            "Fork correction: %s was recorded as %s at %s but is %s at finality"
            " and its position was already released - manual review required",
            signature,
            row["status"],
            row["commitment"] or "no recorded commitment",
            outcome.status.value,
        )

    @staticmethod
    def _log_still_unresolved(token_key: str, checks: int, state: str) -> None:
        """Log an unresolved buy at checks 1, 2, 4, 8, ... so it is never silent."""
        if checks & (checks - 1) == 0:
            logger.warning(
                "Unresolved buy %s still %s after %d check(s)", token_key, state, checks
            )

    async def _sleep_until_shutdown(self, seconds: float) -> bool:
        """Sleep interruptibly, returning true when shutdown was requested."""
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    def _cleanup_fee_reserve_lamports(self) -> int:
        """Return the per-position fee reserve for configured success cleanup."""
        if self.cleanup_mode not in {"after_sell", "post_session"}:
            return 0
        priority_fee = (
            self.priority_fee_manager.hard_cap if self.cleanup_with_priority_fee else 0
        )
        return estimate_transaction_fee_lamports(priority_fee, None)

    def _flow_enabled_for(self, token_info: TokenInfo) -> bool:
        """Real-time flow exits need rules, a SOL-paired pump.fun coin, and Geyser.

        TradeEvent's trusted fields are the SOL-denominated ones; a USDC-paired
        coin would be priced in the wrong unit, so it stays on polling.
        """
        rules = getattr(self, "flow_rules", None)
        return (
            rules is not None
            and rules.enabled
            and token_info.platform is Platform.PUMP_FUN
            and is_sol_paired(normalize_quote_mint(token_info.quote_mint))
            and bool(getattr(self, "geyser_endpoint", None))
            and bool(getattr(self, "geyser_api_token", None))
        )

    async def _consume_trade_flow(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Feed the coin's trade stream to the flow rules; wake the monitor on a hit.

        Any stream failure degrades to polling: this task logs and returns,
        the poll loop never depends on it.
        """
        token_key = str(token_info.mint)
        monitor = FlowMonitor(
            mint=token_key,
            creator=str(token_info.creator) if token_info.creator else "",
            rules=self.flow_rules,
            entry_price=position.entry_price,
        )
        if not token_info.creator:
            logger.warning(
                "Trade flow for %s has no creator; creator_sell rule inactive",
                token_info.symbol,
            )
        hub_queue = self._gate_queues.get(token_key)

        async def hub_events() -> AsyncIterator[TradeEvent]:
            while True:
                yield await hub_queue.get()

        if hub_queue is not None:
            events = hub_events()
        else:

            def discard_pending_signal() -> None:
                self._flow_signals.pop(token_key, None)

            stream = GeyserTradeStream(
                endpoint=self.geyser_endpoint,
                api_token=self.geyser_api_token,
                auth_type=self.geyser_auth_type,
                idl_parser=self.platform_implementations.event_parser._idl_parser,  # noqa: SLF001
            )
            events = stream.stream(
                mint=token_key,
                bonding_curve=str(self._get_pool_address(token_info)),
                on_disconnect=discard_pending_signal,
            )
        try:
            async for event in events:
                if not position.is_active:
                    return
                signal = monitor.observe(event)
                if (
                    signal is None
                    or token_key in self._flow_signals
                    or token_key in self._flow_latched
                ):
                    continue
                logger.warning(
                    "Flow exit for %s: %s (%s) at slot %d",
                    token_info.symbol,
                    signal.rule,
                    signal.detail,
                    signal.slot,
                )
                self._flow_signals[token_key] = signal
                self._flow_wakeups[token_key].set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Trade flow for %s unavailable (%s: %s); polling only",
                token_info.symbol,
                type(exc).__name__,
                exc,
            )

    async def _wait_for_tick(self, token_key: str) -> bool:
        """Sleep one interval, but wake at once on shutdown or a flow signal."""
        wakeup = self._flow_wakeups.get(token_key)
        if wakeup is None:
            return await self._sleep_until_shutdown(self.price_check_interval)
        shutdown = asyncio.create_task(self._shutdown_event.wait())
        woken = asyncio.create_task(wakeup.wait())
        try:
            done, _ = await asyncio.wait(
                {shutdown, woken},
                timeout=self.price_check_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (shutdown, woken):
                if not task.done():
                    task.cancel()
        wakeup.clear()
        return self._shutdown_event.is_set()

    async def _monitor_position_until_exit(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Monitor until confirmed exit; unknown sells are reconciled in place."""
        token_key = str(token_info.mint)
        flow_task: asyncio.Task | None = None
        for attr, factory in (
            ("_flow_signals", dict),
            ("_flow_wakeups", dict),
            ("_flow_latched", set),
        ):
            if not hasattr(self, attr):
                setattr(self, attr, factory())
        if self._flow_enabled_for(token_info):
            self._flow_wakeups[token_key] = asyncio.Event()
            flow_task = asyncio.create_task(
                self._consume_trade_flow(token_info, position)
            )
        try:
            await self._monitor_position_loop(token_info, position)
        finally:
            if flow_task is not None and not flow_task.done():
                flow_task.cancel()
                try:
                    await flow_task
                except asyncio.CancelledError:
                    pass
            self._flow_wakeups.pop(token_key, None)
            self._flow_signals.pop(token_key, None)
            self._flow_latched.discard(token_key)
            if hasattr(self, "_gate_queues"):
                self._unsubscribe_trades(token_key)

    async def _monitor_position_loop(  # noqa: C901, PLR0912, PLR0915
        self, token_info: TokenInfo, position: Position
    ) -> None:
        token_key = str(token_info.mint)
        pool_address = self._get_pool_address(token_info)
        curve_manager = self.platform_implementations.curve_manager
        exit_sell_attempts = 0
        price_outage_started: float | None = None

        while position.is_active and not self._shutdown_event.is_set():
            try:
                if position.pending_exit_signature is not None:
                    outcome = await self.solana_client.confirm_transaction_outcome(
                        position.pending_exit_signature
                    )
                    self._record_trade_evidence(
                        "chain_outcome",
                        token_info,
                        position=position,
                        action="sell",
                        signature=position.pending_exit_signature,
                        status=outcome.status.value,
                        slot=outcome.slot,
                    )
                    if outcome.status is TransactionStatus.UNKNOWN:
                        await self._sleep_until_shutdown(self.price_check_interval)
                        continue
                    if outcome.status is TransactionStatus.SUCCESS:
                        exit_reason = position.pending_exit_reason or ExitReason.MANUAL
                        receipt = await self._get_pending_sell_receipt(
                            token_info, position
                        )
                        if receipt is None:
                            await self._sleep_until_shutdown(self.price_check_interval)
                            continue
                        quote_raw, exit_price = receipt
                        pending_signature = position.pending_exit_signature
                        sold_raw = (
                            position.quantity_raw
                            if (
                                position.account_balance_baseline_raw is not None
                                and token_info.token_program_id is not None
                            )
                            else None
                        )
                        self._record_trade_evidence(
                            "position_closed",
                            token_info,
                            position=position,
                            action="sell",
                            signature=pending_signature,
                            exit_reason=exit_reason.value,
                            price=exit_price,
                            amount_raw=position.quantity_raw,
                            quote_amount_raw=quote_raw,
                        )
                        self._link_lesson_outcome(
                            position,
                            quote_mint=normalize_quote_mint(token_info.quote_mint),
                            sold_quote_raw=quote_raw,
                            reason=exit_reason.value,
                        )
                        staged_cleanup = (
                            stage_cleanup_after_sell(
                                self.solana_client,
                                self.wallet,
                                token_info.mint,
                                token_info.token_program_id,
                                self.priority_fee_manager,
                                self.cleanup_mode,
                                self.cleanup_with_priority_fee,
                                self.cleanup_force_close_with_burn,
                                sold_raw,
                            )
                            if sold_raw is not None
                            else None
                        )
                        position.close_position(exit_price, exit_reason)
                        self._log_trade(
                            "sell",
                            token_info,
                            exit_price,
                            position.quantity,
                            pending_signature,
                        )
                        self._remove_position(token_info.mint)
                        await handle_cleanup_after_sell(
                            self.solana_client,
                            self.wallet,
                            token_info.mint,
                            token_info.token_program_id,
                            self.priority_fee_manager,
                            self.cleanup_mode,
                            self.cleanup_with_priority_fee,
                            self.cleanup_force_close_with_burn,
                            confirmed_sold_raw=(
                                sold_raw if staged_cleanup is None else None
                            ),
                            staged_manager=staged_cleanup,
                        )
                        break
                    if outcome.status is TransactionStatus.REVERTED:
                        if position.pending_exit_fee_lamports is None:
                            raise RuntimeError(
                                "Reverted pending sell has no durable fee"
                            )
                        position.record_charged_exit_fee(
                            position.pending_exit_fee_lamports
                        )
                    position.clear_pending_exit()
                    self._persist_position(token_info, position)

                flow_signal = self._flow_signals.pop(token_key, None)
                if (
                    flow_signal is not None
                    and (queue := self._gate_queues.get(token_key)) is not None
                    and queue.loss_reason
                ):
                    # Reject a pending signal before latching or pricing an exit.
                    # An exit already latched before loss still retries normally.
                    logger.warning(
                        "Discarded pending flow signal for %s: %s",
                        token_key,
                        queue.loss_reason,
                    )
                    flow_signal = None
                if flow_signal is not None:
                    # A fired rule latches until the position closes: a failed
                    # sell on a dead coin gets no further events to re-fire it.
                    self._flow_latched.add(token_key)
                flow_exit = token_key in self._flow_latched
                calculate_token_price = getattr(
                    curve_manager, "calculate_token_price", None
                )
                try:
                    if flow_signal is not None:
                        # The event's post-trade reserves are fresher than any
                        # RPC read and cost zero round trips.
                        current_price = flow_signal.price
                    elif callable(calculate_token_price):
                        current_price = await calculate_token_price(token_info)
                        pool_address = self._get_pool_address(token_info)
                    else:
                        current_price = await curve_manager.calculate_price(
                            pool_address
                        )
                except RpcUnavailableError:
                    # Read-only transport outage: no wire was built, so ride it
                    # out until the budget expires, then fail closed as before.
                    # Data/attestation errors still propagate immediately.
                    now = monotonic()
                    if price_outage_started is None:
                        price_outage_started = now
                    elapsed = now - price_outage_started
                    if elapsed >= self.price_read_outage_budget:
                        logger.error(
                            "Price read outage for %s exceeded %.0fs budget",
                            token_info.symbol,
                            self.price_read_outage_budget,
                        )
                        raise
                    logger.warning(
                        "Price read failed for %s (%.0fs into outage); retrying",
                        token_info.symbol,
                        elapsed,
                        exc_info=True,
                    )
                    if await self._sleep_until_shutdown(self.price_check_interval):
                        break
                    continue
                if current_price <= 0:
                    raise ValueError("Platform returned an invalid current price")
                if price_outage_started is not None:
                    logger.info(
                        "Price read recovered for %s after %.0fs",
                        token_info.symbol,
                        monotonic() - price_outage_started,
                    )
                    price_outage_started = None

                should_exit, exit_reason = position.should_exit(current_price)
                if flow_exit and not (
                    should_exit and exit_reason is ExitReason.STOP_LOSS
                ):
                    # A flow rule is an unconditional exit like stop-loss: it
                    # must not be gated by the take-profit net-ROI target.
                    should_exit, exit_reason = True, ExitReason.TRADE_FLOW
                if should_exit and exit_reason:
                    exit_sell_attempts += 1
                    if (
                        position.pending_exit_intent_id is not None
                        and position.pending_exit_reason is not exit_reason
                    ):
                        position.clear_pending_exit()
                    if position.pending_exit_intent_id is None:
                        attempt_sequence = position.next_exit_attempt()
                        sell_intent_id = (
                            f"sell:{position.position_id}:{attempt_sequence}"
                        )
                        position.mark_exit_intent(
                            sell_intent_id,
                            exit_reason,
                            current_price,
                        )
                        self._persist_position(token_info, position)
                    else:
                        sell_intent_id = position.pending_exit_intent_id
                        exit_reason = position.pending_exit_reason or exit_reason
                    take_profit_net_quote_raw = None
                    if exit_reason is ExitReason.TAKE_PROFIT:
                        if position.take_profit_net_quote_raw is None:
                            raise RuntimeError(
                                "Take-profit position has no durable net quote target"
                            )
                        take_profit_net_quote_raw = (
                            position.take_profit_net_quote_raw
                            + position.charged_exit_fee_lamports
                            + self._cleanup_fee_reserve_lamports()
                        )
                    self._record_trade_evidence(
                        "decision",
                        token_info,
                        position=position,
                        action="sell",
                        intent_id=sell_intent_id,
                        reason=exit_reason.value,
                        trigger_price=current_price,
                        take_profit_net_quote_raw=take_profit_net_quote_raw,
                        flow_signal=asdict(flow_signal)
                        if flow_signal is not None
                        else None,
                    )
                    sell_task = asyncio.create_task(
                        self.seller.execute(
                            token_info,
                            token_amount=position.quantity,
                            token_price=current_price,
                            token_amount_raw=position.quantity_raw,
                            intent_id=sell_intent_id,
                            take_profit_net_quote_raw=take_profit_net_quote_raw,
                        )
                    )
                    sell_cancellation: asyncio.CancelledError | None = None
                    while True:
                        try:
                            sell_result = await asyncio.shield(sell_task)
                            break
                        except asyncio.CancelledError as exc:
                            if sell_cancellation is None:
                                sell_cancellation = exc
                            if sell_task.done():
                                sell_result = await sell_task
                                break
                    if self._defer_unpriced_sell_result(
                        token_info, position, sell_result
                    ):
                        if sell_cancellation is not None:
                            raise sell_cancellation
                        await self._sleep_until_shutdown(self.price_check_interval)
                        continue
                    if sell_result.success:
                        exit_price = sell_result.price
                        sold_raw = (
                            sell_result.amount_raw
                            if (
                                position.account_balance_baseline_raw is not None
                                and token_info.token_program_id is not None
                            )
                            else None
                        )
                        self._link_lesson_outcome(
                            position,
                            quote_mint=normalize_quote_mint(token_info.quote_mint),
                            sold_quote_raw=sell_result.quote_amount_raw,
                            reason=exit_reason.value,
                        )
                        self._record_trade_evidence(
                            "position_closed",
                            token_info,
                            position=position,
                            action="sell",
                            signature=sell_result.tx_signature,
                            exit_reason=exit_reason.value,
                            price=exit_price,
                            amount_raw=sell_result.amount_raw,
                            quote_amount_raw=sell_result.quote_amount_raw,
                        )
                        staged_cleanup = (
                            stage_cleanup_after_sell(
                                self.solana_client,
                                self.wallet,
                                token_info.mint,
                                token_info.token_program_id,
                                self.priority_fee_manager,
                                self.cleanup_mode,
                                self.cleanup_with_priority_fee,
                                self.cleanup_force_close_with_burn,
                                sold_raw,
                            )
                            if sold_raw is not None
                            else None
                        )
                        position.close_position(exit_price, exit_reason)
                        self._log_trade(
                            "sell",
                            token_info,
                            exit_price,
                            sell_result.amount or position.quantity,
                            sell_result.tx_signature,
                        )
                        self._remove_position(token_info.mint)
                        await handle_cleanup_after_sell(
                            self.solana_client,
                            self.wallet,
                            token_info.mint,
                            token_info.token_program_id,
                            self.priority_fee_manager,
                            self.cleanup_mode,
                            self.cleanup_with_priority_fee,
                            self.cleanup_force_close_with_burn,
                            confirmed_sold_raw=(
                                sold_raw if staged_cleanup is None else None
                            ),
                            staged_manager=staged_cleanup,
                        )
                        if sell_cancellation is not None:
                            raise sell_cancellation
                        break
                    if sell_result.status == PlatformAwareSeller.TARGET_NOT_MET_STATUS:
                        position.clear_pending_exit()
                        self._persist_position(token_info, position)
                        exit_sell_attempts = 0
                        logger.info(
                            f"Net take-profit target not yet met for "
                            f"{token_info.symbol}: {sell_result.error_message}"
                        )
                        if sell_cancellation is not None:
                            raise sell_cancellation
                    elif sell_result.unresolved and sell_result.tx_signature:
                        if sell_result.fee_lamports is None:
                            raise RuntimeError(
                                "Unresolved sell has no durable transaction fee"
                            )
                        position.mark_exit_pending(
                            sell_result.tx_signature,
                            exit_reason,
                            fee_lamports=sell_result.fee_lamports,
                        )
                        self._persist_position(token_info, position)
                        logger.warning(
                            f"Sell outcome unresolved for {token_info.symbol}; "
                            "monitor will reconcile the same signature"
                        )
                    else:
                        if sell_result.status == TransactionStatus.REVERTED.value:
                            if sell_result.fee_lamports is None:
                                raise RuntimeError(
                                    "Reverted sell has no durable transaction fee"
                                )
                            position.record_charged_exit_fee(sell_result.fee_lamports)
                        position.clear_pending_exit()
                        self._persist_position(token_info, position)
                        logger.error(
                            f"Exit sell failed ({exit_sell_attempts}/"
                            f"{self.max_exit_sell_attempts}): "
                            f"{sell_result.error_message}"
                        )
                        if sell_cancellation is not None:
                            raise sell_cancellation
                        if exit_sell_attempts >= self.max_exit_sell_attempts:
                            logger.error(
                                f"Exit burst exhausted for {token_info.symbol}; "
                                "position remains journaled and monitored"
                            )
                            exit_sell_attempts = 0
                            if await self._sleep_until_shutdown(
                                max(30, self.price_check_interval)
                            ):
                                break
                            continue
                    if sell_cancellation is not None:
                        raise sell_cancellation
                else:
                    exit_sell_attempts = 0

                if await self._wait_for_tick(token_key):
                    break
            except Exception:
                logger.exception(f"Fatal error monitoring position {token_info.symbol}")
                raise

    def _get_pool_address(self, token_info: TokenInfo) -> Pubkey:
        """Get the pool/curve address for price monitoring using platform-agnostic method."""
        address_provider = self.platform_implementations.address_provider

        # Use platform-specific logic to get the appropriate address
        if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
            return token_info.bonding_curve
        elif hasattr(token_info, "pool_state") and token_info.pool_state:
            return token_info.pool_state
        else:
            # Fallback to deriving the address using platform provider
            return address_provider.derive_pool_address(token_info.mint)

    async def _save_token_info(self, token_info: TokenInfo) -> None:
        """Save token information to a file."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            file_path = trades_dir / f"{token_info.mint}.txt"

            # Convert to dictionary for saving - platform-agnostic
            token_dict = {
                "name": token_info.name,
                "symbol": token_info.symbol,
                "uri": token_info.uri,
                "mint": str(token_info.mint),
                "platform": token_info.platform.value,
                "user": str(token_info.user) if token_info.user else None,
                "creator": str(token_info.creator) if token_info.creator else None,
                "creation_timestamp": token_info.creation_timestamp,
            }

            # Add platform-specific fields only if they exist
            platform_fields = {
                "bonding_curve": token_info.bonding_curve,
                "associated_bonding_curve": token_info.associated_bonding_curve,
                "creator_vault": token_info.creator_vault,
                "pool_state": token_info.pool_state,
                "base_vault": token_info.base_vault,
                "quote_vault": token_info.quote_vault,
            }

            for field_name, field_value in platform_fields.items():
                if field_value is not None:
                    token_dict[field_name] = str(field_value)

            file_path.write_text(json.dumps(token_dict, indent=2))

            logger.info(f"Token information saved to {file_path}")
        except OSError:
            logger.exception("Failed to save token information")

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
    ) -> None:
        """Log trade information."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)

            log_entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "action": action,
                "platform": token_info.platform.value,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
            }

            log_file_path = trades_dir / "trades.log"
            with log_file_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except OSError:
            logger.exception("Failed to log trade information")


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility
