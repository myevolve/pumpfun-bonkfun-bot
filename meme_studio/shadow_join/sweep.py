# ruff: noqa: TRY003
"""Threshold sweep scored on an earlier time half and reported on the later half.

The published plan does not change. A sweep winner is a diagnostic, not a
new commitment. Plan changes stay outside this paper module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from meme_studio.ladder import LadderThresholds, build_plan, replay
from meme_studio.ladder.engine import CoinPath, PathTrade
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
    """Train and test closeout for one grid point."""

    params: SweepParams
    plan_sha256: str
    train_mean_closeout_lamports: int
    test_mean_closeout_lamports: int | None


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Out-of-sample sweep. Selection uses the train half only."""

    selection_basis: str
    published_plan_unchanged: bool
    published_plan_sha256: str
    n_train: int
    n_test: int
    rows: tuple[SweepRow, ...]
    selected_params: SweepParams
    selected_train_mean_closeout_lamports: int
    selected_test_mean_closeout_lamports: int | None


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
    published = build_plan(dev_buy_sols[0], sol_usd=sol_usd)
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
                    train_mean = _mean_closeout(train, plan)
                    test_mean = _mean_closeout(test, plan) if test else None
                    rows.append(
                        SweepRow(
                            params=params,
                            plan_sha256=plan.sha256,
                            train_mean_closeout_lamports=train_mean,
                            test_mean_closeout_lamports=test_mean,
                        )
                    )
    selected = min(rows, key=_rank)
    return SweepReport(
        selection_basis="train_closeout_pnl",
        published_plan_unchanged=True,
        published_plan_sha256=published.sha256,
        n_train=len(train),
        n_test=len(test),
        rows=tuple(rows),
        selected_params=selected.params,
        selected_train_mean_closeout_lamports=selected.train_mean_closeout_lamports,
        selected_test_mean_closeout_lamports=selected.test_mean_closeout_lamports,
    )


def _mean_closeout(coins: list[TrackerCoin], plan: SellPlan) -> int:
    if not coins:
        raise ValueError("closeout mean of an empty split is UNKNOWN")
    values = [replay(coin_to_path(coin), plan).closeout_pnl_lamports for coin in coins]
    return mean_lamports(values)


def coin_to_path(coin: TrackerCoin) -> CoinPath:
    """Build a ladder path. The engine drops the real creator's trades."""
    trades = tuple(
        PathTrade(
            ts=trade.ts,
            user=trade.user,
            is_buy=trade.is_buy,
            sol_amount_raw=trade.sol_amount_raw,
            creator_fee_raw=trade.creator_fee_raw,
        )
        for trade in coin.trades
    )
    return CoinPath(
        mint=coin.mint,
        create_ts=coin.create_ts,
        creator=coin.creator,
        trades=trades,
        extra_observed_creator_fee_raw=coin.pumpswap_creator_fee_raw,
    )


def _rank(row: SweepRow) -> tuple[int, Decimal, SweepParams]:
    return (
        -row.train_mean_closeout_lamports,
        _distance(row.params),
        row.params,
    )


def _distance(params: SweepParams) -> Decimal:
    return (
        abs(Decimal(params.recover_multiple) - Decimal("2.5"))
        + abs(Decimal(params.remaining_fraction) - Decimal("0.25"))
        + abs(Decimal(params.trail_drawdown) - Decimal("0.5"))
    )
