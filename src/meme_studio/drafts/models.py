"""Concept inputs and draft records for the launch-day outbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from meme_studio.compliance.models import LintResult


@dataclass(frozen=True)
class SellEvent:
    """One disclosed dev sell to render as a P2 draft."""

    n: int
    step: str
    tokens: str
    sol: str
    multiple: str
    pct: str
    sig: str
    at: datetime


@dataclass(frozen=True)
class ConceptPackage:
    """Filled marketing fields for one shadow launch day."""

    story_id: str
    story_one_liner: str
    joke_line: str
    game_title: str
    ticker: str
    character: str
    mint: str
    game_url: str
    plan_url: str
    plan_hash: str
    wallet: str
    dev_buy_sol: str
    studio: str
    steve_handle: str
    hub_domain: str
    plan_text: str | None = None
    plays: str | None = None
    score: str | None = None
    handle: str | None = None
    buyers: str | None = None
    holders: str | None = None
    fees_sol: str | None = None
    sell_count: str | None = None
    dev_bag_pct: str | None = None
    time_label: str | None = None
    graduated: bool | None = None
    sells: tuple[SellEvent, ...] = ()


@dataclass(frozen=True)
class DraftRecord:
    """One timestamped draft file and the lint result stored with it."""

    template_id: str
    channel: str
    offset: str
    scheduled_at: datetime
    text: str
    path: Path
    lint: LintResult
    is_reply: bool
    human_approval_required: bool
