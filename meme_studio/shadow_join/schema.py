# ruff: noqa: TRY003
"""Shadow-tracker documents from PIPELINE.md section 5.

The tracker is built elsewhere. This module only reads its JSON shape:
mint, create time, creator, name, symbol, uri, dev buy, mayhem, links,
per-trade rows, graduation time, and PumpSwap creator-fee rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from meme_studio.ladder.money import sol_to_lamports

_WSOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class Concept:
    """A would-be story and the terms that tag same-story coins."""

    story_id: str
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrackerTrade:
    """One trade. sol_amount_raw is the net quote-reserve delta in lamports."""

    ts: int
    user: str
    is_buy: bool
    sol_amount_raw: int
    creator_fee_raw: int = 0


@dataclass(frozen=True, slots=True)
class TrackerCoin:
    """One mint from the shadow tracker."""

    mint: str
    create_ts: int
    creator: str
    name: str
    symbol: str
    uri: str
    description: str | None
    dev_buy_sol: str | None
    mayhem: bool | None
    telegram: str | None
    x: str | None
    website: str | None
    quote_mint: str | None
    graduation_ts: int | None
    trades: tuple[TrackerTrade, ...]
    pumpswap_creator_fee_raw: int = 0


@dataclass(frozen=True, slots=True)
class StorySummary:
    """Same-story cohort and the leader's share of outside buy volume."""

    story_id: str
    n_coins: int
    leader_mint: str | None
    winner_share: str | None


@dataclass(frozen=True, slots=True)
class TrackerFile:
    """A fixture or a daily tracker dump."""

    sol_usd: str
    coins: tuple[TrackerCoin, ...]
    concepts: tuple[Concept, ...]

    def story_summaries(self) -> tuple[StorySummary, ...]:
        """Winner share for each concept, SOL-quoted coins only."""
        from meme_studio.shadow_join.cohorts import (  # noqa: PLC0415
            summarize_stories,
        )

        return summarize_stories(self.coins, self.concepts)


def is_sol_quote(quote_mint: str | None) -> bool:
    """Return whether the coin is quoted in native SOL."""
    if quote_mint is None:
        return False
    if quote_mint == _WSOL:
        return True
    return quote_mint.casefold() in {"sol", "wsol"}


def load_tracker(path: Path) -> TrackerFile:
    """Load a tracker JSON file from disk. No network."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("tracker file must be an object")
    sol_usd = payload.get("sol_usd")
    if sol_usd is None:
        raise ValueError("sol_usd is UNKNOWN")
    concepts = tuple(_concept(item) for item in payload.get("concepts", []))
    coins = tuple(_coin(item) for item in payload.get("coins", []))
    return TrackerFile(sol_usd=str(sol_usd), coins=coins, concepts=concepts)


def _concept(item: object) -> Concept:
    if not isinstance(item, dict):
        raise TypeError("concept must be an object")
    terms = tuple(str(term) for term in item.get("terms", []) if str(term))
    return Concept(story_id=str(item["story_id"]), terms=terms)


def _coin(item: object) -> TrackerCoin:
    if not isinstance(item, dict):
        raise TypeError("coin must be an object")
    links = item.get("links") or {}
    if not isinstance(links, dict):
        links = {}
    trades = tuple(_trade(row) for row in item.get("trades", []))
    pumpswap_fee = 0
    for row in item.get("pumpswap_coin_creator_fee_rows", []):
        pumpswap_fee += _creator_fee(row)
    dev_buy = item.get("dev_buy_sol")
    description = item.get("description")
    return TrackerCoin(
        mint=str(item["mint"]),
        create_ts=int(item["create_ts"]),
        creator=str(item["creator"]),
        name=str(item.get("name") or ""),
        symbol=str(item.get("symbol") or ""),
        uri=str(item.get("uri") or ""),
        description=None if description is None else str(description),
        dev_buy_sol=None if dev_buy is None else str(dev_buy),
        mayhem=_optional_bool(item.get("mayhem")),
        telegram=_link(links, "telegram", "tg"),
        x=_link(links, "x", "twitter"),
        website=_link(links, "website", "site"),
        quote_mint=None if item.get("quote_mint") is None else str(item["quote_mint"]),
        graduation_ts=_optional_int(item.get("graduation_ts")),
        trades=trades,
        pumpswap_creator_fee_raw=pumpswap_fee,
    )


def _trade(item: object) -> TrackerTrade:
    if not isinstance(item, dict):
        raise TypeError("trade must be an object")
    if "sol_amount_raw" in item:
        sol_raw = int(item["sol_amount_raw"])
    else:
        sol_raw = sol_to_lamports(str(item["sol_amount"]))
    return TrackerTrade(
        ts=int(item["ts"]),
        user=str(item["user"]),
        is_buy=bool(item["is_buy"]),
        sol_amount_raw=sol_raw,
        creator_fee_raw=_creator_fee(item),
    )


def _creator_fee(item: object) -> int:
    if not isinstance(item, dict):
        return 0
    if "creator_fee_raw" in item:
        return int(item["creator_fee_raw"])
    if item.get("creator_fee") is None:
        return 0
    return sol_to_lamports(str(item["creator_fee"]))


def _link(links: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = links.get(key)
        if value:
            return str(value)
    return None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise TypeError("mayhem must be a boolean or null")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)  # type: ignore[arg-type]
