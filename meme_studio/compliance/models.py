"""Inputs and pass/fail results for the compliance checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class LintResult:
    """Pass/fail result. ``human_approval_required`` blocks unattended sending."""

    passed: bool
    reasons: tuple[str, ...]
    human_approval_required: bool
    payload_hash: str

    @property
    def status(self) -> str:
        """Return ``pass`` or ``fail``."""
        if self.passed:
            return "pass"
        return "fail"


@dataclass(frozen=True)
class Post:
    """One outbound draft. Nothing here is sent."""

    text: str
    channel: str
    template_id: str = ""
    automated: bool = True
    human_tap: bool = False
    trend_ref: bool = False
    keyword_search_reply: bool = False
    is_reply: bool = False
    paid: bool = False
    action: str = "post"
    opt_in: bool = False
    prior_replies_to_user: int = 0
    author: str = "automation"
    uses_generated_media: bool = False
    ticker: str = ""
    mint: str = ""
    registered_wallets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Asset:
    """Coin name, ticker, or art prompt to screen before use."""

    text: str
    name: str = ""
    ticker: str = ""
    kind: str = "art_prompt"


@dataclass(frozen=True)
class StoryCard:
    """Story text screened by the tragedy and IP gates."""

    text: str
    title: str = ""


@dataclass(frozen=True)
class MediaMetadata:
    """Prompt and overlay metadata for a generated image or clip."""

    prompt: str
    provider: str = ""
    model: str = ""
    timestamp: str = ""
    kind: str = "image"
    channel: str = ""
    overlays: tuple[str, ...] = ()
    game_only: bool = False
    ticker: str = ""
    mint: str = ""


@dataclass(frozen=True)
class LintContext:
    """Recent posts and timestamps used by duplicate and rate checks."""

    history: tuple[str, ...] = ()
    recent_timestamps: tuple[datetime, ...] = ()
    now: datetime | None = None


@dataclass(frozen=True)
class TxIntent:
    """A transaction the launch wrapper would sign. This object is never signed.

    ``dev_buy_cap_sol`` defaults to 0.25 as an assumption copied from the
    QD1955 compliance design (per-launch dev-buy cap). It is not a signed
    spending approval. ``daily_sol_cap`` and ``wallet_balance_cap_sol`` stay
    unset until a caller supplies them; an unset cap fails closed.
    """

    creator_wallet: str
    signers: tuple[str, ...]
    kind: str
    sol_amount: float = 0.0
    amount_tokens: int = 0
    plan_step: str = ""
    expected_step: str = ""
    expected_amount_tokens: int = 0
    plan_hash: str = ""
    committed_plan_hash: str = ""
    daily_sol_spent: float = 0.0
    daily_sol_cap: float | None = None
    dev_buy_cap_sol: float = 0.25
    wallet_balance_sol: float = 0.0
    wallet_balance_cap_sol: float | None = None
    destination: str = ""
    cold_address: str = ""
    token_destination: str = ""


@dataclass(frozen=True)
class OpsCheck:
    """Operational constraints: accounts, dependencies, logs, and phase."""

    x_accounts: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    log_text: str = ""
    phase: str = "shadow"
    action: str = "draft"
