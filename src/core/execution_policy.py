"""Explicit execution policy for fund-moving operations.

The policy is deliberately independent of the Solana client so it can be
constructed and tested before any network or signer objects are created.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    if value is None or isinstance(value, int) and not isinstance(value, bool):
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
        ):
            object.__setattr__(self, name, _normalize_quote_caps(value, name))
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
