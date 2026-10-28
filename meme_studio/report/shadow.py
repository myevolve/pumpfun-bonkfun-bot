"""Read the phase-0 shadow tracker file described in PIPELINE.md section 5.

The tracker design names ``phase0/<date>/shadow_join.parquet``. That file was
not on disk, and this package does not add a parquet dependency. The reader
accepts a JSON document with the same fields: mint, create_ts, creator,
name/symbol/uri, dev_buy_sol, mayhem, links, trades (ts, user, is_buy,
sol_amount, creator_fee), graduation_ts, and coin_creator_fee rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class MintMetrics:
    """Buyer and fee totals computed from one mint's trade rows."""

    mint: str
    creator: str
    name: str
    symbol: str
    outside_buyers: int | None
    creator_fees_sol: float | None
    graduated: bool | None
    dev_buy_sol: float | None
    mayhem: bool | None
    trade_count: int | None
    story_id: str


@dataclass(frozen=True)
class ShadowSnapshot:
    """One tracker file: per-mint metrics plus paper-replay fields."""

    date: str
    mints: tuple[MintMetrics, ...]
    story_ids: tuple[str, ...]
    paper_replay: dict[str, dict[str, object]]
    random_control: dict[str, object]
    by_story: dict[str, dict[str, object]]
    coverage_known: bool


def load_shadow_tracker(path: Path) -> ShadowSnapshot:
    """Load a JSON shadow-join fixture and compute outside buyers and fees."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    coverage_known = isinstance(payload, dict) and "cohorts" in payload
    cohorts = payload.get("cohorts", []) if isinstance(payload, dict) else []
    mints: list[MintMetrics] = []
    story_ids: list[str] = []
    by_story: dict[str, dict[str, object]] = {}
    for cohort in cohorts:
        if not isinstance(cohort, dict):
            continue
        story_id = str(cohort.get("story_id", ""))
        story_ids.append(story_id)
        replay = cohort.get("paper_replay") if "paper_replay" in cohort else None
        control = cohort.get("random_control") if "random_control" in cohort else None
        by_story[story_id] = {
            "paper_replay": replay if isinstance(replay, dict) else None,
            "random_control": control if isinstance(control, dict) else None,
        }
        for mint in cohort.get("mints", []):
            if isinstance(mint, dict):
                mints.append(_mint_metrics(mint, story_id))
    paper_replay: dict[str, dict[str, object]] = {}
    random_control: dict[str, object] = {}
    if len(by_story) == 1:
        only = next(iter(by_story.values()))
        replay = only.get("paper_replay")
        control = only.get("random_control")
        if isinstance(replay, dict):
            paper_replay = replay
        if isinstance(control, dict):
            random_control = control
    return ShadowSnapshot(
        date=str(payload.get("date", "")) if isinstance(payload, dict) else "",
        mints=tuple(mints),
        story_ids=tuple(story_ids),
        paper_replay=paper_replay,
        random_control=random_control,
        by_story=by_story,
        coverage_known=coverage_known,
    )


def _mint_metrics(mint: dict[str, object], story_id: str) -> MintMetrics:
    creator = mint.get("creator")
    creator_name = creator if isinstance(creator, str) else ""
    trades_known = "trades" in mint and isinstance(mint.get("trades"), list)
    fees_known = "coin_creator_fees" in mint and isinstance(
        mint.get("coin_creator_fees"), list
    )
    trade_rows = mint.get("trades") if trades_known else []
    fee_rows = mint.get("coin_creator_fees") if fees_known else []
    buyers: int | None = None
    if trades_known and isinstance(trade_rows, list):
        buyers = _outside_buyers(trade_rows, creator_name)
    fees: float | None = None
    if trades_known and fees_known and isinstance(trade_rows, list):
        fees = _creator_fees(trade_rows, fee_rows if isinstance(fee_rows, list) else [])
    return MintMetrics(
        mint=str(mint.get("mint", "")),
        creator=creator_name,
        name=str(mint.get("name", "")),
        symbol=str(mint.get("symbol", "")),
        outside_buyers=buyers,
        creator_fees_sol=fees,
        graduated=_optional_bool_timestamp(mint, "graduation_ts"),
        dev_buy_sol=_optional_float(mint, "dev_buy_sol"),
        mayhem=_optional_bool(mint, "mayhem"),
        trade_count=len(trade_rows)
        if trades_known and isinstance(trade_rows, list)
        else None,
        story_id=story_id,
    )


def _optional_float(mint: dict[str, object], key: str) -> float | None:
    if key not in mint or mint[key] is None:
        return None
    value = mint[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_bool(mint: dict[str, object], key: str) -> bool | None:
    if key not in mint or mint[key] is None:
        return None
    return bool(mint[key])


def _optional_bool_timestamp(mint: dict[str, object], key: str) -> bool | None:
    if key not in mint:
        return None
    return bool(mint[key])


def _usable_user(trade: dict[str, object]) -> str | None:
    if "user" not in trade:
        return None
    user = trade.get("user")
    if not isinstance(user, str):
        return None
    stripped = user.strip()
    if not stripped or stripped.lower() == "none":
        return None
    return stripped


def _outside_buyers(trades: list[object], creator: str) -> int | None:
    users: set[str] = set()
    saw_invalid_buy = False
    for trade in trades:
        if not isinstance(trade, dict) or not trade.get("is_buy"):
            continue
        user = _usable_user(trade)
        if user is None:
            saw_invalid_buy = True
            continue
        if user != creator:
            users.add(user)
    if saw_invalid_buy:
        return None
    return len(users)


def _creator_fees(trades: list[object], fee_rows: list[object]) -> float | None:
    total = 0.0
    for trade in trades:
        if not isinstance(trade, dict) or "creator_fee" not in trade:
            return None
        fee = trade.get("creator_fee")
        if isinstance(fee, bool) or not isinstance(fee, (int, float)):
            return None
        total += float(fee)
    for row in fee_rows:
        if not isinstance(row, dict) or "amount_sol" not in row:
            return None
        amount = row.get("amount_sol")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        total += float(amount)
    return round(total, 10)
