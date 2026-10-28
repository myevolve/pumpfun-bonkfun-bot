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

from meme_studio.ladder.money import parse_sol_lamports

_WSOL = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True, slots=True)
class Concept:
    """A would-be story and the terms that tag same-story coins."""

    story_id: str
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrackerTrade:
    """One trade. sol_amount_raw is the net quote-reserve delta in lamports."""

    ts: int | None
    user: str
    is_buy: bool
    sol_amount_raw: int
    creator_fee_raw: int | None = None
    direction_known: bool = True
    attribution_known: bool = True
    ts_known: bool = True


@dataclass(frozen=True, slots=True)
class CreatorFeeRow:
    """One PumpSwap creator-fee row. None fields were not in the tracker file."""

    ts: int | None
    user: str | None
    amount_raw: int | None


@dataclass(frozen=True, slots=True)
class TrackerCoin:
    """One mint from the shadow tracker."""

    mint: str
    create_ts: int
    creator: str | None
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
    pumpswap_creator_fee_raw: int | None = None
    pumpswap_fee_rows: tuple[CreatorFeeRow, ...] = ()
    pumpswap_fees_known: bool = True
    history_complete: bool = True
    rejected_observations: int = 0


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
    trades, rejected, history_complete = _trades(item.get("trades", []))
    fee_rows, fees_known = _pumpswap_fees(item)
    dev_buy = item.get("dev_buy_sol")
    description = item.get("description")
    creator = item.get("creator")
    creator_text = creator if isinstance(creator, str) and creator.strip() else None
    return TrackerCoin(
        mint=str(item["mint"]),
        create_ts=int(item["create_ts"]),
        creator=creator_text,
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
        pumpswap_creator_fee_raw=None,
        pumpswap_fee_rows=fee_rows,
        pumpswap_fees_known=fees_known,
        history_complete=history_complete,
        rejected_observations=rejected,
    )


def _trades(raw: object) -> tuple[tuple[TrackerTrade, ...], int, bool]:
    if not isinstance(raw, list):
        return (), 1, False
    parsed: list[TrackerTrade] = []
    rejected = 0
    complete = True
    for row in raw:
        trade = _trade(row)
        if trade is None:
            rejected += 1
            complete = False
            continue
        parsed.append(trade)
        if (
            not trade.ts_known
            or not trade.direction_known
            or not trade.attribution_known
        ):
            complete = False
    return tuple(parsed), rejected, complete


def _trade(item: object) -> TrackerTrade | None:
    if not isinstance(item, dict):
        return None
    ts = item.get("ts")
    ts_known = isinstance(ts, int) and not isinstance(ts, bool) and ts > 0
    if "sol_amount_raw" in item:
        sol_raw = _raw_lamports(item.get("sol_amount_raw"))
    elif "sol_amount" in item:
        sol_raw = _sol_field_lamports(item.get("sol_amount"))
    else:
        sol_raw = None
    direction = item.get("is_buy")
    user = item.get("user")
    attribution_known = isinstance(user, str) and bool(user.strip())
    direction_known = isinstance(direction, bool) and sol_raw is not None
    if not ts_known:
        return TrackerTrade(
            ts=None,
            user=user if attribution_known else "",
            is_buy=direction if isinstance(direction, bool) else False,
            sol_amount_raw=0,
            creator_fee_raw=_optional_fee(item),
            direction_known=False,
            attribution_known=attribution_known,
            ts_known=False,
        )
    return TrackerTrade(
        ts=ts if isinstance(ts, int) else None,
        user=user if isinstance(user, str) else "",
        is_buy=direction if isinstance(direction, bool) else False,
        sol_amount_raw=0 if sol_raw is None else sol_raw,
        creator_fee_raw=_optional_fee(item),
        direction_known=direction_known,
        attribution_known=attribution_known,
        ts_known=True,
    )


def _pumpswap_fees(item: dict[str, object]) -> tuple[tuple[CreatorFeeRow, ...], bool]:
    if "pumpswap_coin_creator_fee_rows" not in item:
        return (), False
    raw_rows = item["pumpswap_coin_creator_fee_rows"]
    if not isinstance(raw_rows, list):
        return (), False
    return tuple(_fee_row(row) for row in raw_rows), True


def _fee_row(item: object) -> CreatorFeeRow:
    if not isinstance(item, dict):
        return CreatorFeeRow(ts=None, user=None, amount_raw=None)
    user = item.get("user")
    ts = item.get("ts")
    return CreatorFeeRow(
        ts=ts if isinstance(ts, int) else None,
        user=user if isinstance(user, str) and user else None,
        amount_raw=_optional_fee(item),
    )


def _optional_fee(item: dict[str, object]) -> int | None:
    if "creator_fee_raw" in item:
        return _raw_lamports(item.get("creator_fee_raw"))
    if "creator_fee" not in item:
        return None
    return _sol_field_lamports(item.get("creator_fee"))


def _raw_lamports(value: object) -> int | None:
    """`*_raw` fields are lamports. Only a nonnegative integer is accepted."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _sol_field_lamports(value: object) -> int | None:
    """SOL fields are decimal text. A bare integer is ambiguous and rejected."""
    if not isinstance(value, str):
        return None
    return parse_sol_lamports(value)


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
