"""Explicit execution policy for fund-moving operations.

The policy is deliberately independent of the Solana client so it can be
constructed and tested before any network or signer objects are created.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from dataclasses import field as dataclasses_field
from enum import StrEnum
from math import isfinite
from typing import Any

from solders.pubkey import Pubkey

from core.pubkeys import normalize_quote_mint, resolve_quote_mint

_MAX_RISK_SESSION_ID_LENGTH = 128

QuoteCaps = int | dict[str, int]


def _normalize_quote_caps(value: object, field_name: str) -> QuoteCaps | None:
    """Accept one cap for every quote asset, or a cap per quote mint.

    SOL has 9 decimals and USDC 6, so a single raw-unit cap cannot express
    "0.01 SOL per trade, 5 USDC per trade" - the scalar is kept as the
    every-asset form so existing configurations keep their exact meaning.
    """
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        if value is not None:
            _validate_nonnegative_int(value, field_name)
        return value
    if isinstance(value, dict):
        if not value:
            raise ValueError(f"{field_name} must not be empty when given as a mapping")
        normalized: dict[str, int] = {}
        for mint, cap in value.items():
            _validate_nonnegative_int(cap, f"{field_name}[{mint}]")
            normalized[str(resolve_quote_mint(mint))] = cap
        return normalized
    raise ValueError(f"{field_name} must be an integer or a per-mint mapping")


class ExecutionMode(StrEnum):
    """Permitted runtime modes."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class ExecutionPolicyError(RuntimeError):
    """Base error for execution-policy violations."""


class ExecutionBlocked(ExecutionPolicyError):
    """Raised when a signing or submission operation is not authorized."""


class TradeLimitExceeded(ExecutionPolicyError):
    """Raised when a trade exceeds a configured risk budget."""


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Immutable policy governing signing, submission, and destructive cleanup."""

    mode: ExecutionMode = ExecutionMode.DRY_RUN
    live_authorized: bool = False
    expected_wallet: str | None = None
    max_trade_quote_raw: QuoteCaps | None = None
    max_total_fee_lamports: int | None = None
    risk_session_id: str | None = None
    max_session_quote_raw: QuoteCaps | None = None
    max_session_fee_lamports: int | None = None
    allow_skip_preflight: bool = False
    allow_force_burn: bool = False
    max_consecutive_losses: int | None = None
    max_session_drawdown_quote_raw: QuoteCaps | None = None

    def __post_init__(self) -> None:
        """Validate policy invariants at construction time."""
        if not isinstance(self.mode, ExecutionMode):
            try:
                object.__setattr__(self, "mode", ExecutionMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise ValueError("mode must be a supported execution mode") from exc
        if not isinstance(self.live_authorized, bool):
            raise ValueError("live_authorized must be a boolean")
        if not isinstance(self.allow_skip_preflight, bool):
            raise ValueError("allow_skip_preflight must be a boolean")
        if not isinstance(self.allow_force_burn, bool):
            raise ValueError("allow_force_burn must be a boolean")
        if self.mode is not ExecutionMode.LIVE and self.live_authorized:
            raise ValueError("live_authorized requires execution mode 'live'")
        for name, value in (
            ("max_trade_quote_raw", self.max_trade_quote_raw),
            ("max_session_quote_raw", self.max_session_quote_raw),
            ("max_session_drawdown_quote_raw", self.max_session_drawdown_quote_raw),
        ):
            object.__setattr__(self, name, _normalize_quote_caps(value, name))
        if self.max_consecutive_losses is not None and (
            isinstance(self.max_consecutive_losses, bool)
            or not isinstance(self.max_consecutive_losses, int)
            or self.max_consecutive_losses < 1
        ):
            raise ValueError("max_consecutive_losses must be a positive integer")  # noqa: TRY003
        for name, value in (
            ("max_total_fee_lamports", self.max_total_fee_lamports),
            ("max_session_fee_lamports", self.max_session_fee_lamports),
        ):
            if value is not None:
                _validate_nonnegative_int(value, name)
        if self.risk_session_id is not None:
            if (
                not isinstance(self.risk_session_id, str)
                or not self.risk_session_id.strip()
            ):
                raise ValueError("risk_session_id must be a non-empty string")  # noqa: TRY003
            if len(self.risk_session_id) > _MAX_RISK_SESSION_ID_LENGTH:
                raise ValueError(  # noqa: TRY003
                    "risk_session_id must be at most "
                    f"{_MAX_RISK_SESSION_ID_LENGTH} characters"
                )
        if self.mode is ExecutionMode.LIVE:
            if self.max_trade_quote_raw is None:
                raise ValueError("max_trade_quote_raw is required for live execution")
            if self.max_total_fee_lamports is None:
                raise ValueError(
                    "max_total_fee_lamports is required for live execution"
                )
            if self.risk_session_id is None:
                raise ValueError("risk_session_id is required for live execution")  # noqa: TRY003
            if self.max_session_quote_raw is None:
                raise ValueError(  # noqa: TRY003
                    "max_session_quote_raw is required for live execution"
                )
            if self.max_session_fee_lamports is None:
                raise ValueError(  # noqa: TRY003
                    "max_session_fee_lamports is required for live execution"
                )
            if self.expected_wallet is None:
                raise ValueError("expected_wallet is required for live execution")
        if self.expected_wallet is not None:
            if not isinstance(self.expected_wallet, str):
                raise ValueError("expected_wallet must be a string")
            if not self.expected_wallet.strip():
                raise ValueError("expected_wallet must not be empty")
            try:
                canonical_wallet = str(Pubkey.from_string(self.expected_wallet))
            except (TypeError, ValueError) as exc:
                raise ValueError("expected_wallet must be a valid public key") from exc
            object.__setattr__(self, "expected_wallet", canonical_wallet)

    @property
    def can_submit(self) -> bool:
        """Whether this policy permits a transaction submission."""
        return self.mode is ExecutionMode.LIVE and self.live_authorized

    def authorize_live(self) -> ExecutionPolicy:
        """Return a live-authorized copy after explicit runtime acknowledgement."""
        if self.mode is not ExecutionMode.LIVE:
            raise ExecutionBlocked(
                "Live authorization requires execution.mode='live' in configuration"
            )
        return replace(self, live_authorized=True)

    def require_submission(self) -> None:
        """Raise unless the caller is explicitly authorized to submit."""
        if not self.can_submit:
            raise ExecutionBlocked(
                "Transaction submission blocked: explicit runtime authorization "
                "is required for live execution"
            )

    def validate_wallet(self, wallet: Any) -> None:
        """Ensure the signer matches the configured public-key identity."""
        if self.expected_wallet is None:
            return
        try:
            actual = str(Pubkey.from_string(str(wallet)))
        except (TypeError, ValueError) as exc:
            raise ExecutionBlocked("Signer wallet is not a valid public key") from exc
        if actual != self.expected_wallet:
            raise ExecutionBlocked(
                f"Configured wallet does not match signer wallet ({actual})"
            )

    def quote_cap(
        self,
        caps: QuoteCaps | None,
        quote_mint: Pubkey | str | None,
        name: str,
    ) -> int | None:
        """Resolve the cap that applies to one quote asset.

        A per-mint mapping fails closed for an asset it does not name: a cap
        that silently does not apply is worse than no mapping at all.
        """
        if caps is None:
            return None
        if isinstance(caps, int):
            return caps
        # Native SOL (None) is the same asset as wrapped SOL, and aliases such
        # as "usdc" name the same mint as its base58 address.
        mint = (
            normalize_quote_mint(None)
            if quote_mint is None
            else resolve_quote_mint(quote_mint)
        )
        cap = caps.get(str(mint))
        if cap is None:
            raise ExecutionBlocked(  # noqa: TRY003
                f"{name} does not cover quote asset {mint}"
            )
        return cap

    def validate_budgets(
        self,
        quote_amount_raw: int,
        fee_lamports: int,
        *,
        quote_mint: Pubkey | str | None = None,
    ) -> None:
        """Enforce per-trade quote and total-fee budgets."""
        try:
            _validate_nonnegative_int(quote_amount_raw, "trade quote amount")
            _validate_nonnegative_int(fee_lamports, "transaction fee")
        except ValueError as exc:
            raise TradeLimitExceeded(str(exc)) from exc
        if quote_amount_raw == 0:
            # A fee-only transaction, such as account cleanup, spends no quote
            # units: resolving a per-asset cap would block rent recovery under
            # a policy that names another asset. Fee limits still apply.
            quote_cap = 0
        else:
            quote_cap = self.quote_cap(
                self.max_trade_quote_raw, quote_mint, "max_trade_quote_raw"
            )
        if quote_cap is not None and quote_amount_raw > quote_cap:
            raise TradeLimitExceeded(
                f"trade quote amount {quote_amount_raw} exceeds limit {quote_cap}"
            )
        if (
            self.max_total_fee_lamports is not None
            and fee_lamports > self.max_total_fee_lamports
        ):
            raise TradeLimitExceeded(
                f"transaction fee {fee_lamports} exceeds "
                f"limit {self.max_total_fee_lamports}"
            )

    def session_risk_limits(self) -> tuple[str, int]:
        """Return the session id and its cumulative fee limit."""
        if (
            self.risk_session_id is None
            or self.max_session_quote_raw is None
            or self.max_session_fee_lamports is None
        ):
            raise ExecutionBlocked(  # noqa: TRY003
                "Persistent session risk limits are required for live execution"
            )
        return (
            self.risk_session_id,
            self.max_session_fee_lamports,
        )

    def session_quote_cap(self, quote_mint: Pubkey | str | None) -> int:
        """Return the cumulative quote cap that applies to one quote asset."""
        self.session_risk_limits()
        cap = self.quote_cap(
            self.max_session_quote_raw, quote_mint, "max_session_quote_raw"
        )
        if cap is None:
            raise ExecutionBlocked(  # noqa: TRY003
                "Persistent session risk limits are required for live execution"
            )
        return cap

    def validate_preflight(self, skip_preflight: bool) -> None:
        """Prevent callers from bypassing simulation without a policy grant."""
        if skip_preflight and not self.allow_skip_preflight:
            raise ExecutionBlocked(
                "skip_preflight is disabled by execution policy; enable it only "
                "after an explicit live-risk review"
            )

    def validate_force_burn(self, requested: bool) -> None:
        """Prevent destructive cleanup unless explicitly permitted."""
        if requested and not self.allow_force_burn:
            raise ExecutionBlocked("force-close burn is disabled by execution policy")

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        live_authorized: bool = False,
    ) -> ExecutionPolicy:
        """Build a policy from a validated configuration mapping."""
        if not isinstance(config, dict):
            raise ValueError("configuration must be a mapping")
        if not isinstance(live_authorized, bool):
            raise ValueError("live_authorized must be a boolean")
        raw = config.get("execution", {})
        if not isinstance(raw, dict):
            raise ValueError("execution must be a mapping")
        try:
            mode = ExecutionMode(raw.get("mode", ExecutionMode.DRY_RUN.value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "execution.mode must be one of: "
                f"{', '.join(item.value for item in ExecutionMode)}"
            ) from exc
        return cls(
            mode=mode,
            live_authorized=live_authorized,
            expected_wallet=raw.get("expected_wallet"),
            max_trade_quote_raw=raw.get("max_trade_quote_raw"),
            max_total_fee_lamports=raw.get("max_total_fee_lamports"),
            risk_session_id=raw.get("risk_session_id"),
            max_session_quote_raw=raw.get("max_session_quote_raw"),
            max_session_fee_lamports=raw.get("max_session_fee_lamports"),
            max_consecutive_losses=raw.get("max_consecutive_losses"),
            max_session_drawdown_quote_raw=raw.get("max_session_drawdown_quote_raw"),
            allow_skip_preflight=raw.get("allow_skip_preflight", False),
            allow_force_burn=raw.get("allow_force_burn", False),
        )


def _validate_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def validate_finite_number(value: object, field_name: str) -> None:
    """Validate a finite integer/float configuration value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    if not isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


@dataclass(slots=True)
class DrawdownBreaker:
    """Session-scoped loss circuit breaker.

    Counts consecutive losing closes and per-quote-asset realized drawdown;
    once a threshold trips, the latch holds until a new risk session starts
    (a restart). Unpriced closes (sold amount unknown) cannot be attributed
    and are ignored rather than guessed.
    """

    max_consecutive_losses: int | None
    max_session_drawdown_quote_raw: QuoteCaps | None
    consecutive_losses: int = 0
    realized_pnl_raw: dict[str, int] = dataclasses_field(default_factory=dict)
    tripped_reason: str | None = None

    @classmethod
    def from_policy(cls, policy: ExecutionPolicy) -> DrawdownBreaker | None:
        """Build the breaker, or None when no threshold is configured."""
        if (
            policy.max_consecutive_losses is None
            and policy.max_session_drawdown_quote_raw is None
        ):
            return None
        return cls(
            max_consecutive_losses=policy.max_consecutive_losses,
            max_session_drawdown_quote_raw=policy.max_session_drawdown_quote_raw,
        )

    def record_close(self, pnl_quote_raw: int | None, quote_mint: object) -> None:
        """Update loss counters from one closed position."""
        if self.tripped_reason is not None or pnl_quote_raw is None:
            return
        if isinstance(pnl_quote_raw, bool) or not isinstance(pnl_quote_raw, int):
            raise ValueError("pnl_quote_raw must be an integer or None")
        key = "unknown" if quote_mint is None else str(quote_mint)
        self.realized_pnl_raw[key] = self.realized_pnl_raw.get(key, 0) + pnl_quote_raw
        if pnl_quote_raw < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if (
            self.max_consecutive_losses is not None
            and self.consecutive_losses >= self.max_consecutive_losses
        ):
            self.tripped_reason = (
                f"consecutive_losses={self.consecutive_losses}"
                f" (threshold {self.max_consecutive_losses})"
            )
            return
        cap = self._cap_for(key)
        if cap is not None and -self.realized_pnl_raw[key] >= cap:
            self.tripped_reason = (
                f"session drawdown {self.realized_pnl_raw[key]} on {key} (cap {cap})"
            )

    def _cap_for(self, key: str) -> int | None:
        caps = self.max_session_drawdown_quote_raw
        if caps is None:
            return None
        if isinstance(caps, int):
            return caps
        return caps.get(key)

    def entry_block(self, quote_mint: object) -> str | None:
        """Reason new entries are halted, or None. Missing per-mint cap: fail-closed."""
        if self.tripped_reason is not None:
            return self.tripped_reason
        key = "unknown" if quote_mint is None else str(quote_mint)
        caps = self.max_session_drawdown_quote_raw
        if isinstance(caps, dict) and key not in caps:
            return f"no session drawdown cap configured for quote {key}"
        return None
