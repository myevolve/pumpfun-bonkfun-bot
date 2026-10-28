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
    outside_buyers: int
    creator_fees_sol: float
    graduated: bool
    dev_buy_sol: float
    mayhem: bool
    trade_count: int
    story_id: str


@dataclass(frozen=True)
class ShadowSnapshot:
    """One tracker file: per-mint metrics plus paper-replay fields."""

    date: str
    mints: tuple[MintMetrics, ...]
    story_ids: tuple[str, ...]
    paper_replay: dict[str, dict[str, object]]
    random_control: dict[str, object]


def load_shadow_tracker(path: Path) -> ShadowSnapshot:
    """Load a JSON shadow-join fixture and compute outside buyers and fees."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cohorts = payload.get("cohorts", [])
    mints: list[MintMetrics] = []
    story_ids: list[str] = []
    paper_replay: dict[str, dict[str, object]] = {}
    random_control: dict[str, object] = {}
    for index, cohort in enumerate(cohorts):
        story_id = str(cohort.get("story_id", ""))
        story_ids.append(story_id)
        if index == 0:
            paper_replay = dict(cohort.get("paper_replay") or {})
            random_control = dict(cohort.get("random_control") or {})
        for mint in cohort.get("mints", []):
            mints.append(_mint_metrics(mint, story_id))
    return ShadowSnapshot(
        date=str(payload.get("date", "")),
        mints=tuple(mints),
        story_ids=tuple(story_ids),
        paper_replay=paper_replay,
        random_control=random_control,
    )


def _mint_metrics(mint: dict[str, object], story_id: str) -> MintMetrics:
    trades = mint.get("trades")
    trade_rows = trades if isinstance(trades, list) else []
    fees = mint.get("coin_creator_fees")
    fee_rows = fees if isinstance(fees, list) else []
    creator = str(mint.get("creator", ""))
    return MintMetrics(
        mint=str(mint.get("mint", "")),
        creator=creator,
        name=str(mint.get("name", "")),
        symbol=str(mint.get("symbol", "")),
        outside_buyers=_outside_buyers(trade_rows, creator),
        creator_fees_sol=_creator_fees(trade_rows, fee_rows),
        graduated=bool(mint.get("graduation_ts")),
        dev_buy_sol=float(mint.get("dev_buy_sol") or 0),
        mayhem=bool(mint.get("mayhem")),
        trade_count=len(trade_rows),
        story_id=story_id,
    )


def _outside_buyers(trades: list[object], creator: str) -> int:
    users: set[str] = set()
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        if trade.get("is_buy") and trade.get("user") != creator:
            users.add(str(trade.get("user")))
    return len(users)


def _creator_fees(trades: list[object], fee_rows: list[object]) -> float:
    total = 0.0
    for trade in trades:
        if isinstance(trade, dict):
            total += float(trade.get("creator_fee") or 0)
    for row in fee_rows:
        if isinstance(row, dict):
            total += float(row.get("amount_sol") or 0)
    return round(total, 10)
