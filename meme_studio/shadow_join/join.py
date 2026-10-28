"""Per-coin and cohort paper P&L for the shadow join."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from meme_studio.ladder import build_plan
from meme_studio.ladder.engine import replay
from meme_studio.ladder.money import lamports_to_sol_str, mean_lamports
from meme_studio.ladder.plan import DISCLOSURE, FEE_CONFLICT
from meme_studio.shadow_join.cohorts import TaggedCoin, tag_cohorts
from meme_studio.shadow_join.schema import TrackerFile, is_sol_quote
from meme_studio.shadow_join.sweep import (
    SweepReport,
    coin_to_path,
    sweep_membership,
    sweep_thresholds,
)

_DEV_BUYS = ("0.07", "0.1", "0.25")
_COHORTS = ("population", "N", "P", "P_x", "P_minus_N", "C")
_BOOTSTRAP_DRAWS = 10_000
_BOOTSTRAP_SEED = 1956


@dataclass(frozen=True, slots=True)
class CohortPnl:
    """Cohort aggregate at one dev-buy size."""

    cohort: str
    dev_buy_sol: str
    n: int
    mean_realized_pnl_lamports: int
    bootstrap_mean_pnl_lamports_ci95: tuple[int, int]


@dataclass(frozen=True, slots=True)
class CoinPnlRow:
    """One paper replay, one dev-buy size."""

    mint: str
    dev_buy_sol: str
    cohorts: str
    story_ids: str
    is_leader: bool
    plan_sha256: str
    realized_pnl_lamports: int | None
    realized_pnl_measured_30bps_lamports: int | None
    realized_pnl_research_95bps_lamports: int | None
    closeout_pnl_lamports: int | None
    closeout_is_mark_not_plan: bool
    held_raw: int
    reached_recover: bool
    graduated_on_our_curve: bool
    steps_fired: str
    population_split: str
    sweep_split: str
    pumpswap_creator_fee_status: str
    fee_conflict: str
    disclosure: str


@dataclass(frozen=True, slots=True)
class JoinReport:
    """Paper join of our ladder onto the tracker's coins."""

    sol_usd: str
    rows: tuple[CoinPnlRow, ...]
    cohorts: tuple[CohortPnl, ...]
    excluded_non_sol: int
    excluded_quote_unknown: int
    sweep: SweepReport


@dataclass(frozen=True, slots=True)
class WrittenJoin:
    """Files the daily report can read."""

    parquet_path: Path
    summary_path: Path


def build_join_report(tracker: TrackerFile) -> JoinReport:
    """Replay 0.07 / 0.1 / 0.25 SOL on every SOL-quoted coin.

    Args:
        tracker: Loaded shadow-tracker document, including concepts and sol_usd.

    Returns:
        Per-coin rows, cohort means, and the out-of-sample threshold sweep.
    """
    tagged = tag_cohorts(tracker.coins, tracker.concepts)
    population = [item for item in tagged if "population" in item.cohorts]
    ordered = sorted(population, key=lambda item: (item.coin.create_ts, item.mint))
    n_train = len(ordered) // 2
    population_split = {
        item.mint: "train" if index < n_train else "test"
        for index, item in enumerate(ordered)
    }
    sweep_split = sweep_membership(
        tuple(item.coin for item in population),
        tracker.concepts,
    )
    rows: list[CoinPnlRow] = []
    for item in ordered:
        for dev_buy in _DEV_BUYS:
            plan = build_plan(dev_buy, sol_usd=tracker.sol_usd)
            result = replay(coin_to_path(item.coin), plan)
            scenarios = result.scenario_realized_lamports
            rows.append(
                CoinPnlRow(
                    mint=item.mint,
                    dev_buy_sol=lamports_to_sol_str(plan.dev_buy_lamports),
                    cohorts=",".join(item.cohorts),
                    story_ids=",".join(item.story_ids),
                    is_leader=item.is_leader,
                    plan_sha256=plan.sha256,
                    realized_pnl_lamports=result.realized_pnl_lamports,
                    realized_pnl_measured_30bps_lamports=scenarios.get(
                        "measured_30bps_assumption"
                    ),
                    realized_pnl_research_95bps_lamports=scenarios.get(
                        "research_95bps_assumption"
                    ),
                    closeout_pnl_lamports=result.closeout_pnl_lamports,
                    closeout_is_mark_not_plan="closeout_is_mark_not_plan"
                    in result.flags,
                    held_raw=result.held_raw,
                    reached_recover=result.reached_recover,
                    graduated_on_our_curve=result.graduated_on_our_curve,
                    steps_fired=",".join(sell.step for sell in result.sells),
                    population_split=population_split[item.mint],
                    sweep_split=sweep_split.get(item.mint, "not_applicable"),
                    pumpswap_creator_fee_status=result.pumpswap_creator_fee_status,
                    fee_conflict=result.fee_conflict,
                    disclosure=DISCLOSURE,
                )
            )
    cohorts = _cohort_table(population, rows)
    non_sol = sum(
        1
        for coin in tracker.coins
        if coin.quote_mint is not None and not is_sol_quote(coin.quote_mint)
    )
    unknown = sum(1 for coin in tracker.coins if coin.quote_mint is None)
    sweep = sweep_thresholds(
        tuple(item.coin for item in population),
        tracker.concepts,
        sol_usd=tracker.sol_usd,
        dev_buy_sols=_DEV_BUYS,
    )
    return JoinReport(
        sol_usd=tracker.sol_usd,
        rows=tuple(rows),
        cohorts=cohorts,
        excluded_non_sol=non_sol,
        excluded_quote_unknown=unknown,
        sweep=sweep,
    )


def write_join_report(report: JoinReport, out_dir: Path, date: str) -> WrittenJoin:
    """Write phase0/<date>/shadow_join.parquet and a JSON summary."""
    folder = out_dir / "phase0" / date
    folder.mkdir(parents=True, exist_ok=True)
    parquet_path = folder / "shadow_join.parquet"
    summary_path = folder / "shadow_join_summary.json"
    table = pa.Table.from_pylist([_row_dict(row) for row in report.rows])
    pq.write_table(table, parquet_path)
    summary_path.write_text(
        json.dumps(_summary(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return WrittenJoin(parquet_path=parquet_path, summary_path=summary_path)


def _cohort_table(
    population: list[TaggedCoin],
    rows: list[CoinPnlRow],
) -> tuple[CohortPnl, ...]:
    found: list[CohortPnl] = []
    dev_buys = sorted({row.dev_buy_sol for row in rows})
    for cohort in _COHORTS:
        members = {item.mint for item in population if cohort in item.cohorts}
        for dev_buy in dev_buys:
            values = [
                row.realized_pnl_lamports
                for row in rows
                if row.dev_buy_sol == dev_buy
                and row.mint in members
                and row.realized_pnl_lamports is not None
            ]
            if not values:
                continue
            found.append(
                CohortPnl(
                    cohort=cohort,
                    dev_buy_sol=dev_buy,
                    n=len(values),
                    mean_realized_pnl_lamports=mean_lamports(
                        [int(value) for value in values]
                    ),
                    bootstrap_mean_pnl_lamports_ci95=_bootstrap(
                        [int(value) for value in values]
                    ),
                )
            )
    return tuple(found)


def _bootstrap(values: list[int]) -> tuple[int, int]:
    rng = random.Random(_BOOTSTRAP_SEED)  # noqa: S311
    count = len(values)
    means = [
        mean_lamports([values[rng.randrange(count)] for _ in range(count)])
        for _ in range(_BOOTSTRAP_DRAWS)
    ]
    means.sort()
    last = _BOOTSTRAP_DRAWS - 1
    low = means[round(0.025 * last)]
    high = means[round(0.975 * last)]
    point = mean_lamports(values)
    return min(low, point), max(high, point)


def _row_dict(row: CoinPnlRow) -> dict[str, object]:
    return {
        "mint": row.mint,
        "dev_buy_sol": row.dev_buy_sol,
        "cohorts": row.cohorts,
        "story_ids": row.story_ids,
        "is_leader": row.is_leader,
        "plan_sha256": row.plan_sha256,
        "realized_pnl_lamports": row.realized_pnl_lamports,
        "realized_pnl_measured_30bps_lamports": (
            row.realized_pnl_measured_30bps_lamports
        ),
        "realized_pnl_research_95bps_lamports": (
            row.realized_pnl_research_95bps_lamports
        ),
        "closeout_pnl_lamports": row.closeout_pnl_lamports,
        "closeout_is_mark_not_plan": row.closeout_is_mark_not_plan,
        "held_raw": row.held_raw,
        "reached_recover": row.reached_recover,
        "graduated_on_our_curve": row.graduated_on_our_curve,
        "steps_fired": row.steps_fired,
        "population_split": row.population_split,
        "sweep_split": row.sweep_split,
        "pumpswap_creator_fee_status": row.pumpswap_creator_fee_status,
        "fee_conflict": row.fee_conflict,
        "disclosure": row.disclosure,
    }


def _summary(report: JoinReport) -> dict[str, object]:
    return {
        "paper": True,
        "disclosure": DISCLOSURE,
        "fee_conflict": FEE_CONFLICT,
        "sol_usd": report.sol_usd,
        "excluded_non_sol": report.excluded_non_sol,
        "excluded_quote_unknown": report.excluded_quote_unknown,
        "cohorts": [
            {
                "cohort": row.cohort,
                "dev_buy_sol": row.dev_buy_sol,
                "n": row.n,
                "mean_realized_pnl_lamports": row.mean_realized_pnl_lamports,
                "bootstrap_mean_pnl_lamports_ci95": list(
                    row.bootstrap_mean_pnl_lamports_ci95
                ),
            }
            for row in report.cohorts
        ],
        "sweep": {
            "selection_basis": report.sweep.selection_basis,
            "published_plan_unchanged": report.sweep.published_plan_unchanged,
            "published_plan_sha256": report.sweep.published_plan_sha256,
            "n_train": report.sweep.n_train,
            "n_test": report.sweep.n_test,
            "selected_dev_buy_sol": report.sweep.selected_dev_buy_sol,
            "selected_plan_sha256": report.sweep.selected_plan_sha256,
            "selected_thresholds": None
            if report.sweep.selected_params is None
            else {
                "recover_multiple": report.sweep.selected_params.recover_multiple,
                "remaining_fraction": report.sweep.selected_params.remaining_fraction,
                "trail_drawdown": report.sweep.selected_params.trail_drawdown,
            },
            "selected_train_mean_realized_lamports": (
                report.sweep.selected_train_mean_realized_lamports
            ),
            "selected_test_mean_realized_lamports": (
                report.sweep.selected_test_mean_realized_lamports
            ),
            "unavailable_reason": report.sweep.unavailable_reason,
            "closeout_note": (
                "closeout marks are not recorded fills and are not used to rank"
            ),
        },
    }
