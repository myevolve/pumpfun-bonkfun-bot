"""Threshold sweep scored on an earlier time half and reported on the later half.

The published plan does not change. A sweep winner is a diagnostic, not a
new commitment. Plan changes stay outside this paper module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from meme_studio.ladder import LadderThresholds, build_plan, replay
from meme_studio.ladder.engine import CoinPath, FeeRow, PathTrade
from meme_studio.ladder.money import mean_lamports

if TYPE_CHECKING:
    from meme_studio.ladder.plan import SellPlan
from meme_studio.shadow_join.cohorts import matches_any_concept
from meme_studio.shadow_join.schema import Concept, TrackerCoin, is_sol_quote

_RECOVERS = ("2.0", "2.5", "3.0")
_FRACTIONS = ("0.15", "0.25", "0.35")
_DRAWS = ("0.30", "0.50", "0.70")


@dataclass(frozen=True, slots=True, order=True)
class SweepParams:
    """One grid point. Field order is the deterministic tie break."""

    recover_multiple: str
    remaining_fraction: str
    trail_drawdown: str


@dataclass(frozen=True, slots=True)
class SweepRow:
    """Train and test recorded-fill P&L for one grid point."""

    dev_buy_sol: str
    params: SweepParams
    plan_sha256: str
    train_mean_realized_lamports: int | None
    test_mean_realized_lamports: int | None
    train_mean_closeout_lamports: int | None
    test_mean_closeout_lamports: int | None


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Out-of-sample sweep. Selection uses recorded fills on the train half."""

    selection_basis: str
    published_plan_unchanged: bool
    published_plan_sha256: str | None
    n_train: int
    n_test: int
    rows: tuple[SweepRow, ...]
    selected_params: SweepParams | None
    selected_dev_buy_sol: str | None
    selected_plan_sha256: str | None
    selected_train_mean_realized_lamports: int | None
    selected_test_mean_realized_lamports: int | None
    unavailable_reason: str | None = None


def sweep_thresholds(
    coins: tuple[TrackerCoin, ...] | list[TrackerCoin],
    concepts: tuple[Concept, ...] | list[Concept],
    *,
    sol_usd: str,
    dev_buy_sols: tuple[str, ...] = ("0.10",),
) -> SweepReport:
    """Sweep recover, tranche, and trail thresholds.

    Args:
        coins: Tracker coins. Non-SOL quotes are dropped.
        concepts: When present, only coins that match a concept are swept.
        sol_usd: Price used for the $1 launch cost.
        dev_buy_sols: Dev-buy sizes. Each size is its own plan hash.

    Returns:
        Every grid point's train and test means, and the train-selected point.
    """
    pool = [
        coin
        for coin in coins
        if is_sol_quote(coin.quote_mint) and matches_any_concept(coin, concepts)
    ]
    ordered = sorted(pool, key=lambda coin: (coin.create_ts, coin.mint))
    n_train = len(ordered) // 2
    train = ordered[:n_train]
    test = ordered[n_train:]
    published = build_plan(dev_buy_sols[0], sol_usd=sol_usd) if dev_buy_sols else None
    if not train or not test:
        return SweepReport(
            selection_basis="unavailable",
            published_plan_unchanged=True,
            published_plan_sha256=None if published is None else published.sha256,
            n_train=len(train),
            n_test=len(test),
            rows=(),
            selected_params=None,
            selected_dev_buy_sol=None,
            selected_plan_sha256=None,
            selected_train_mean_realized_lamports=None,
            selected_test_mean_realized_lamports=None,
            unavailable_reason="train and test splits both need at least one coin",
        )
    rows: list[SweepRow] = []
    for dev_buy in dev_buy_sols:
        for recover in _RECOVERS:
            for fraction in _FRACTIONS:
                for draw in _DRAWS:
                    params = SweepParams(recover, fraction, draw)
                    plan = build_plan(
                        dev_buy,
                        sol_usd=sol_usd,
                        thresholds=LadderThresholds(
                            recover_multiple=recover,
                            remaining_fraction=fraction,
                            trail_drawdown=draw,
                        ),
                    )
                    train_realized, train_closeout = _means(train, plan)
                    test_realized, test_closeout = _means(test, plan)
                    rows.append(
                        SweepRow(
                            dev_buy_sol=plan.to_dict()["dev_buy_sol"]
                            if isinstance(plan.to_dict()["dev_buy_sol"], str)
                            else dev_buy,
                            params=params,
                            plan_sha256=plan.sha256,
                            train_mean_realized_lamports=train_realized,
                            test_mean_realized_lamports=test_realized,
                            train_mean_closeout_lamports=train_closeout,
                            test_mean_closeout_lamports=test_closeout,
                        )
                    )
    ranked = [row for row in rows if row.train_mean_realized_lamports is not None]
    if not ranked:
        return SweepReport(
            selection_basis="unavailable",
            published_plan_unchanged=True,
            published_plan_sha256=None if published is None else published.sha256,
            n_train=len(train),
            n_test=len(test),
            rows=tuple(rows),
            selected_params=None,
            selected_dev_buy_sol=None,
            selected_plan_sha256=None,
            selected_train_mean_realized_lamports=None,
            selected_test_mean_realized_lamports=None,
            unavailable_reason="recorded-fill P&L is UNKNOWN on the train split",
        )
    selected = min(ranked, key=_rank)
    return SweepReport(
        selection_basis="train_realized_pnl",
        published_plan_unchanged=True,
        published_plan_sha256=None if published is None else published.sha256,
        n_train=len(train),
        n_test=len(test),
        rows=tuple(rows),
        selected_params=selected.params,
        selected_dev_buy_sol=selected.dev_buy_sol,
        selected_plan_sha256=selected.plan_sha256,
        selected_train_mean_realized_lamports=selected.train_mean_realized_lamports,
        selected_test_mean_realized_lamports=selected.test_mean_realized_lamports,
    )


def sweep_membership(
    coins: tuple[TrackerCoin, ...] | list[TrackerCoin],
    concepts: tuple[Concept, ...] | list[Concept],
) -> dict[str, str]:
    """Return mint -> train/test/not_applicable for the sweep population."""
    pool = [
        coin
        for coin in coins
        if is_sol_quote(coin.quote_mint) and matches_any_concept(coin, concepts)
    ]
    ordered = sorted(pool, key=lambda coin: (coin.create_ts, coin.mint))
    n_train = len(ordered) // 2
    membership = {coin.mint: "not_applicable" for coin in coins}
    for index, coin in enumerate(ordered):
        membership[coin.mint] = "train" if index < n_train else "test"
    return membership


def _means(coins: list[TrackerCoin], plan: SellPlan) -> tuple[int | None, int | None]:
    if not coins:
        return None, None
    results = [replay(coin_to_path(coin), plan) for coin in coins]
    realized = [item.realized_pnl_lamports for item in results]
    closeout = [item.closeout_pnl_lamports for item in results]
    if any(value is None for value in realized):
        realized_mean = None
    else:
        realized_mean = mean_lamports([int(value) for value in realized])
    if any(value is None for value in closeout):
        closeout_mean = None
    else:
        closeout_mean = mean_lamports([int(value) for value in closeout])
    return realized_mean, closeout_mean


def coin_to_path(coin: TrackerCoin) -> CoinPath:
    """Build a ladder path. The engine drops the real creator's trades."""
    trades = tuple(
        PathTrade(
            ts=trade.ts,
            user=trade.user,
            is_buy=trade.is_buy,
            sol_amount_raw=trade.sol_amount_raw,
            creator_fee_raw=trade.creator_fee_raw,
            direction_known=trade.direction_known,
            attribution_known=trade.attribution_known,
        )
        for trade in coin.trades
        if isinstance(trade.ts, int)
    )
    return CoinPath(
        mint=coin.mint,
        create_ts=coin.create_ts,
        creator=coin.creator,
        trades=trades,
        extra_observed_creator_fee_raw=None,
        graduation_ts=coin.graduation_ts,
        history_complete=coin.history_complete,
        rejected_observations=coin.rejected_observations,
        pumpswap_fee_rows=tuple(
            FeeRow(row.ts, row.user, row.amount_raw) for row in coin.pumpswap_fee_rows
        ),
        pumpswap_fees_known=coin.pumpswap_fees_known,
    )


def _rank(row: SweepRow) -> tuple[int, Decimal, str, SweepParams]:
    realized = row.train_mean_realized_lamports or 0
    return (
        -realized,
        _distance(row.params),
        row.dev_buy_sol,
        row.params,
    )


def _distance(params: SweepParams) -> Decimal:
    return (
        abs(Decimal(params.recover_multiple) - Decimal("2.5"))
        + abs(Decimal(params.remaining_fraction) - Decimal("0.25"))
        + abs(Decimal(params.trail_drawdown) - Decimal("0.5"))
    )
