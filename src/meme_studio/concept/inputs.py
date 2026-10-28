"""Scorer inputs. Sources fill these; nothing here reads the network."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CountPoint:
    """One post-count bucket. ``start`` is timezone-aware."""

    start: datetime
    count: int


@dataclass(frozen=True)
class OutletHit:
    """One outlet mention used by the velocity rule."""

    name: str
    published_at: datetime


@dataclass(frozen=True)
class CopycatCoin:
    """A same-story coin already live, from a public lookup or a local log."""

    name: str
    ticker: str
    outside_buyers: int
    volume_7d_usd: float
    market_cap_usd: float


@dataclass(frozen=True)
class StoryInput:
    """One candidate story before scoring."""

    title: str
    summary: str
    detected_at: datetime
    sources: tuple[str, ...]
    outlets: tuple[OutletHit, ...]
    post_counts: tuple[CountPoint, ...]
    copycats: tuple[CopycatCoin, ...] | None = ()
    category: str = ""
