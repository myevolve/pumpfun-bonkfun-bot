"""Read-only learning evidence: separate live outcomes, gross marks and old proxies.

Run ``python -m learning.report [--json] [--db PATH] [--output NEW_FILE]``. Legacy paper_exit
values used an invalid real-reserve price model and are excluded, not deleted.
Gross marginal marks omit fees, impact, latency-to-fill and execution risk;
they cannot authorize trading or establish a profitable strategy.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from utils.paths import state_path

DB = state_path("learning", "lessons.sqlite3")

_MIN_COHORT_N = 10  # descriptive correlations only, not a significance threshold


def _battery_correlations(conn: sqlite3.Connection, signals: tuple) -> list[dict]:
    """Pearson r(signal, pnl) per battery signal on resolved outcomes."""
    # Signal names are module-owned constants, never user input.
    cols = ", ".join(signals)
    rows = conn.execute(
        f"SELECT {cols}, outcome_pnl_sol FROM nonpaper_outcomes"  # noqa: S608
        " WHERE outcome_pnl_sol IS NOT NULL AND jev_quality IS NOT NULL"
        " ORDER BY id DESC LIMIT 500"
    ).fetchall()
    out = []
    for i, name in enumerate(signals):
        pairs = [(r[i], r[-1]) for r in rows if r[i] is not None]
        if len(pairs) < _MIN_COHORT_N:
            out.append({"signal": name, "n": len(pairs), "r": None})
            continue
        xs = [x for x, _ in pairs]
        ys = [y for _, y in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        r = round(cov / (sx * sy), 3) if sx and sy else None
        out.append({"signal": name, "n": len(pairs), "r": r})
    return out


def graduation_decay(conn: sqlite3.Connection) -> dict:
    """Compare each observed horizon with the same entry's opening mark."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grad_marks'"
    ).fetchone():
        return {}
    row = conn.execute(
        """SELECT COUNT(*),
            COUNT(p5), AVG(p5 / open_price),
            COUNT(p30), AVG(p30 / open_price),
            COUNT(p120), AVG(p120 / open_price)
        FROM grad_marks WHERE open_price > 0"""
    ).fetchone()
    if not row[0]:
        return {}
    out = {"n": row[0]}
    for index, column in enumerate(("p5", "p30", "p120")):
        out[f"{column}_n"] = row[1 + index * 2]
        ratio = row[2 + index * 2]
        out[f"{column}_over_open"] = round(ratio, 4) if ratio is not None else None
    return out


_CREATOR_FEE_RATE = Decimal("0.003")
_V1_ROUNDING_TOLERANCE = Decimal("0.0000005")
_LAMPORTS_PER_SOL_DEC = Decimal(1_000_000_000)


def _row_vault_balance(row: dict) -> Decimal | None:
    if row.get("schema_version", 1) == 1:
        return Decimal(str(row["vault_sol"]))
    raw = row.get("vault_wsol_raw")
    if (
        row.get("observation_status") == "observed"
        and isinstance(raw, int)
        and not isinstance(raw, bool)
    ):
        return Decimal(raw) / _LAMPORTS_PER_SOL_DEC
    return None


def _update_pool_accrual(
    row: dict,
    last_bal: dict[str, Decimal],
    accrued: dict[str, Decimal],
    withdrawn: dict[str, Decimal],
    withdrawals: Counter[str],
) -> None:
    pool = row["pool"]
    if row.get("baseline_status") == "identity_changed":
        last_bal.pop(pool, None)
        accrued.pop(pool, None)
        withdrawn.pop(pool, None)
        withdrawals.pop(pool, None)
    bal = _row_vault_balance(row)
    if bal is None:
        return
    if pool not in accrued:
        accrued[pool] = bal
        withdrawn[pool] = Decimal(0)
    else:
        delta = bal - last_bal[pool]
        is_v1 = (
            row.get("schema_version", 1) == 1
            or row.get("baseline_status") == "legacy_unverifiable"
        )
        if is_v1 and abs(delta) <= _V1_ROUNDING_TOLERANCE:
            accrued[pool] = max(Decimal(0), accrued[pool] + delta)
        elif delta > 0:
            accrued[pool] += delta
        elif delta < 0:
            withdrawn[pool] += -delta
            withdrawals[pool] += 1
    last_bal[pool] = bal


def _creator_fee_summary(path: Path) -> dict:
    """Summarize withdrawal-adjusted creator-vault accrual when snapshots exist."""
    if not path.is_file():
        return {}
    batches: dict[str, list[dict]] = {}
    last_bal: dict[str, Decimal] = {}
    accrued: dict[str, Decimal] = {}
    withdrawn: dict[str, Decimal] = {}
    withdrawals: Counter[str] = Counter()
    total = 0
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                ts = row["ts"]
                _update_pool_accrual(row, last_bal, accrued, withdrawn, withdrawals)
                batches.setdefault(ts, []).append(row)
                total += 1
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation):
        return {}
    if not batches:
        return {}
    times = sorted(batches)
    latest = batches[times[-1]]
    pools = sorted({r["pool"] for r in latest})
    total_accrued = sum((accrued[p] for p in pools if p in accrued), Decimal(0))
    total_withdrawn = sum((withdrawn[p] for p in pools if p in withdrawn), Decimal(0))
    current_bal = sum((last_bal[p] for p in pools if p in last_bal), Decimal(0))
    return {
        "latest_ts": times[-1],
        "snapshot_count": len(times),
        "total_rows": total,
        "distinct_latest_pools": len(pools),
        "unique_observed_creator_vaults": len(
            {
                r.get("creator_vault_ata")
                for r in latest
                if r.get("observation_status") == "observed"
                and r.get("creator_vault_ata")
            }
        ),
        "pools_with_withdrawals": sum(1 for p in pools if withdrawals[p] > 0),
        "total_withdrawals": sum(withdrawals[p] for p in pools),
        "current_balance_wsol": round(float(current_bal), 9),
        "total_accrued_wsol": round(float(total_accrued), 9),
        "total_withdrawn_wsol": round(float(total_withdrawn), 9),
        "total_implied_volume_sol": round(float(total_accrued / _CREATOR_FEE_RATE), 6),
    }


class _MedianAggregate:
    def __init__(self) -> None:
        self.vals: list[float] = []

    def step(self, value: object) -> None:
        if value is not None:
            self.vals.append(float(value))

    def finalize(self) -> float | None:
        if not self.vals:
            return None
        self.vals.sort()
        n = len(self.vals)
        mid = n // 2
        if n % 2 == 1:
            return self.vals[mid]
        return (self.vals[mid - 1] + self.vals[mid]) / 2.0


def _roll_col_sql(g_cols: set[str]) -> str:
    parts = [
        f"g.{col}, "
        for col in ("stabilized_rollover_price", "rollover_price")
        if col in g_cols
    ]
    return "".join(parts)


def _grad_marks_summary(conn: sqlite3.Connection, g_cols: set[str]) -> dict:
    if "rollover_price" not in g_cols:
        return {}
    stab_sql = (
        """COUNT(stabilized_rollover_price) AS stabilized_rollover_n,
            AVG(stabilized_rollover_price / open_price - 1.0)
                AS mean_stabilized_rollover_vs_open,
            AVG(stabilized_rollover_elapsed_s)
                AS mean_stabilized_rollover_elapsed_s,"""
        if "stabilized_rollover_price" in g_cols
        else ""
    )
    return dict(
        conn.execute(
            f"""SELECT COUNT(*) AS graduated_n,
                COUNT(peak_price) AS peak_n,
                COUNT(rollover_price) AS rollover_n,
                {stab_sql}
                COALESCE(SUM(grad_elapsed_s < 4.0), 0) AS poll1_already_complete_n,
                COALESCE(SUM(grad_elapsed_s >= 4.0), 0) AS post_entry_grad_n,
                AVG(peak_price / open_price - 1.0) AS mean_peak_vs_open,
                AVG(peak_elapsed_s) AS mean_peak_elapsed_s,
                AVG(rollover_price / open_price - 1.0) AS mean_rollover_vs_open,
                AVG(rollover_elapsed_s) AS mean_rollover_elapsed_s,
                COALESCE(SUM(peak_price >= open_price * 1.01), 0) AS rallied_1pct_n,
                COALESCE(
                    SUM(peak_price >= open_price * 1.01 AND rollover_price < open_price),
                    0
                ) AS whipsaw_below_open_n,
                COALESCE(SUM(rollover_price >= open_price), 0) AS rollover_above_open_n,
                COALESCE(
                    SUM(
                        rollover_elapsed_s <= 10.0
                        AND rollover_price < open_price
                        AND (p30 > open_price OR p120 > open_price)
                    ),
                    0
                ) AS early_whipsaw_count,
                AVG(
                    CASE
                        WHEN rollover_elapsed_s <= 10.0
                            AND rollover_price > 0.85 * open_price
                            AND COALESCE(p5, rollover_price) > 0.90 * open_price
                        THEN COALESCE(p30, rollover_price) / open_price - 1.0
                        ELSE rollover_price / open_price - 1.0
                    END
                ) AS post_shakeout_p30_vs_open
            FROM grad_marks WHERE open_price > 0"""  # noqa: S608
        ).fetchone()
    )


def _paper_buyer_cohorts(
    conn: sqlite3.Connection,
    *,
    has_grad: bool,
    grad_join: str,
    roll_col: str,
) -> list[dict]:
    grad_col = (
        "COUNT(g.open_price) AS graduated_n," if has_grad else "0 AS graduated_n,"
    )
    policy_expr = (
        f"""CASE
            WHEN g.entry_id IS NOT NULL AND g.open_price > 0 THEN
                CASE
                    WHEN g.open_price <= p60.entry_price
                    THEN g.open_price / p60.entry_price - 1.0
                    ELSE COALESCE(
                        {roll_col}g.p120, g.p30, g.p5, g.open_price
                    ) / p60.entry_price - 1.0
                END
            WHEN p60.exit_price IS NOT NULL
            THEN p60.exit_price / p60.entry_price - 1.0
        END"""
        if has_grad
        else """CASE
            WHEN p60.exit_price IS NOT NULL
            THEN p60.exit_price / p60.entry_price - 1.0
        END"""
    )
    return [
        dict(row)
        for row in conn.execute(
            f"""WITH buyer_mints AS (
                    SELECT json_extract(raw, '$.gate.last_event.user') AS buyer,
                        COUNT(DISTINCT mint) AS buyer_mints
                    FROM lessons
                    WHERE json_extract(raw, '$.gate.last_event.user') IS NOT NULL
                        AND json_extract(raw, '$.gate.last_event.user') != ''
                    GROUP BY 1
                )
                SELECT CASE
                        WHEN bm.buyer = 'BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s'
                            THEN 'mayhem_vault_buyer'
                        WHEN bm.buyer_mints > 1 THEN 'organic_repeat_buyer'
                        ELSE 'single_entry_buyer'
                    END AS buyer_cohort,
                    COUNT(*) AS planned_entries,
                    COUNT(DISTINCT bm.buyer) AS distinct_buyers,
                    {grad_col}
                    SUM(CASE WHEN p60.exit_price IS NOT NULL
                        AND p60.exit_price > p60.entry_price THEN 1 ELSE 0 END) AS win_60s_n,
                    ROUND(AVG(CASE WHEN p60.exit_price IS NOT NULL
                        THEN p60.exit_price / p60.entry_price - 1.0 END), 4) AS mean_return_60s,
                    ROUND(AVG({policy_expr}), 4) AS mean_policy_return
                FROM paper_cohorts c
                JOIN lessons l ON l.id=c.entry_id
                JOIN buyer_mints bm
                    ON bm.buyer=json_extract(l.raw, '$.gate.last_event.user')
                LEFT JOIN paper_marks p60
                    ON p60.entry_id=c.entry_id AND p60.horizon_s=60
                {grad_join}
                GROUP BY buyer_cohort
                ORDER BY planned_entries DESC, buyer_cohort"""  # noqa: S608
        )
    ]


def _paper_evidence(conn: sqlite3.Connection) -> dict:
    """Expose missingness and within-entry differences, never unpaired rankings."""
    out = {
        "paper_marks": [],
        "paper_censors": [],
        "paper_comparisons": [],
        "paper_by_entry_timing": [],
        "paper_by_creator_cohort": [],
        "paper_by_buyer_cohort": [],
        "executable_funnel": [],
        "grad_decay": {},
        "paper_coverage": {
            "planned_entries": 0,
            "paired_entries": 0,
            "distinct_mints": 0,
            "paired_mints": 0,
            "pending_entries": 0,
            "censored_entries": 0,
            "first_entry_utc": None,
            "last_entry_utc": None,
        },
    }
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_marks'"
    ).fetchone():
        return out
    conn.execute(
        """CREATE TEMP VIEW paper_cohorts AS
        SELECT entry_id, COUNT(*) AS planned, COUNT(exit_price) AS marked,
            MAX(outcome_utc IS NULL) AS pending,
            MAX(outcome_utc IS NOT NULL AND exit_price IS NULL) AS censored
        FROM paper_marks GROUP BY entry_id"""
    )
    out["paper_coverage"] = dict(
        conn.execute(
            """SELECT COUNT(*) AS planned_entries,
            COALESCE(SUM(c.planned=3 AND c.marked=3), 0) AS paired_entries,
            COUNT(DISTINCT l.mint) AS distinct_mints,
            COUNT(DISTINCT CASE WHEN c.planned=3 AND c.marked=3 THEN l.mint END)
                AS paired_mints,
            COALESCE(SUM(c.pending), 0) AS pending_entries,
            COALESCE(SUM(c.censored), 0) AS censored_entries,
            MIN(l.utc) AS first_entry_utc, MAX(l.utc) AS last_entry_utc
        FROM paper_cohorts c JOIN lessons l ON l.id=c.entry_id"""
        ).fetchone()
    )
    out["paper_marks"] = [
        dict(row)
        for row in conn.execute(
            """SELECT p.horizon_s, COUNT(*) AS planned,
                SUM(p.outcome_utc IS NULL) AS pending,
                SUM(p.outcome_utc IS NOT NULL AND p.exit_price IS NULL) AS censored,
                COUNT(p.exit_price) AS marked,
                SUM(c.planned=3 AND c.marked=3) AS paired_n,
                AVG(CASE WHEN c.planned=3 AND c.marked=3
                    THEN p.exit_price / p.entry_price - 1 END) AS paired_mean_return
            FROM paper_marks p JOIN paper_cohorts c USING(entry_id)
            GROUP BY p.horizon_s ORDER BY p.horizon_s"""
        )
    ]
    out["paper_censors"] = [
        dict(row)
        for row in conn.execute(
            """SELECT horizon_s, reason, COUNT(*) AS n FROM paper_marks
            WHERE outcome_utc IS NOT NULL AND exit_price IS NULL
            GROUP BY horizon_s, reason ORDER BY horizon_s, n DESC, reason"""
        )
    ]
    actions = {
        "cancelled": "Allow the bounded observation drain; distinguish stop from loss.",
        "late": "Check reader/scheduler timing; do not accept stale marks as on-time.",
        "migrated_or_invalid_completion": "Inspect curve completion; AMM exits need their own validated model.",
        "unsupported_quote": "Use a quote-specific model; do not scale this asset as SOL.",
        "unsupported_or_missing_entry": "Check the accepted gate event and quote; never infer an entry price.",
        "unexecutable_completed_curve": "Curve was already complete or unquotable when the buyer refreshed state; not a fillable entry.",
        "invalid_price": "Inspect decoded virtual reserves; do not substitute a zero.",
        "read_error": "Inspect provider/state attestation with the existing stop and retry policy.",
    }
    for row in out["paper_censors"]:
        reason = row["reason"] or ""
        row["next_step"] = actions.get(
            "read_error" if reason.startswith("read_error:") else reason,
            "Inspect retained entry and exit evidence before comparison.",
        )
    out["paper_comparisons"] = [
        dict(row)
        for row in conn.execute(
            """WITH returns AS (
                SELECT p.entry_id, p.horizon_s, p.exit_price/p.entry_price-1 AS r
                FROM paper_marks p JOIN paper_cohorts c USING(entry_id)
                WHERE c.planned=3 AND c.marked=3
            )
            SELECT p.horizon_s, COUNT(*) AS paired_n,
                AVG(p.r-b.r) AS mean_delta_vs_60s,
                SUM(p.r>b.r) AS improved, SUM(p.r<b.r) AS worsened,
                SUM(p.r=b.r) AS unchanged
            FROM returns p JOIN returns b ON b.entry_id=p.entry_id AND b.horizon_s=60
            WHERE p.horizon_s!=60 GROUP BY p.horizon_s ORDER BY p.horizon_s"""
        )
    ]
    if "raw" in {row[1] for row in conn.execute("PRAGMA table_info(lessons)")}:
        has_grad = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grad_marks'"
            ).fetchone()
        )
        grad_join = (
            "LEFT JOIN grad_marks g ON g.entry_id=c.entry_id" if has_grad else ""
        )
        grad_col = (
            "COUNT(g.open_price) AS graduated_n," if has_grad else "0 AS graduated_n,"
        )
        out["paper_by_entry_timing"] = [
            dict(row)
            for row in conn.execute(
                f"""SELECT CASE
                        WHEN COALESCE(l.mayhem, 0) = 0 AND l.buyers BETWEEN 2 AND 3
                            THEN 'in_band_2_3_non_mayhem'
                        ELSE 'out_of_band'
                    END AS band,
                    CASE
                        WHEN json_extract(l.raw, '$.gate.slots_waited') IS NULL
                            THEN 'unclassified_slot_timing'
                        WHEN CAST(json_extract(l.raw, '$.gate.slots_waited') AS INTEGER) = 0
                            THEN 'slot_0_creation_bundle'
                        ELSE 'post_creation_slot_ge_1'
                    END AS entry_timing,
                    COUNT(*) AS planned_entries,
                    SUM(c.planned=3 AND c.marked=3) AS paired_entries,
                    {grad_col}
                    SUM(CASE WHEN p60.exit_price IS NOT NULL
                        AND p60.exit_price > p60.entry_price THEN 1 ELSE 0 END) AS win_60s_n,
                    ROUND(AVG(CASE WHEN p60.exit_price IS NOT NULL
                        THEN p60.exit_price / p60.entry_price - 1.0 END), 4) AS mean_return_60s
                FROM paper_cohorts c
                JOIN lessons l ON l.id=c.entry_id
                LEFT JOIN paper_marks p60
                    ON p60.entry_id=c.entry_id AND p60.horizon_s=60
                {grad_join}
                GROUP BY band, entry_timing
                ORDER BY band, planned_entries DESC, entry_timing"""  # noqa: S608
            )
        ]
        out["paper_by_creator_cohort"] = [
            dict(row)
            for row in conn.execute(
                f"""WITH pass_creators AS (
                        SELECT json_extract(raw, '$.gate.last_event.creator') AS creator,
                            COUNT(DISTINCT mint) AS pass_mints
                        FROM lessons
                        WHERE kind='gate_pass'
                            AND json_extract(raw, '$.gate.last_event.creator') IS NOT NULL
                            AND json_extract(raw, '$.gate.last_event.creator') != ''
                        GROUP BY 1
                    )
                    SELECT CASE
                            WHEN pc.pass_mints > 1 THEN 'repeat_pass_creator'
                            ELSE 'single_pass_creator'
                        END AS creator_cohort,
                        COUNT(*) AS planned_entries,
                        COUNT(DISTINCT pc.creator) AS distinct_creators,
                        {grad_col}
                        SUM(CASE WHEN p60.exit_price IS NOT NULL
                            AND p60.exit_price > p60.entry_price THEN 1 ELSE 0 END) AS win_60s_n,
                        ROUND(AVG(CASE WHEN p60.exit_price IS NOT NULL
                            THEN p60.exit_price / p60.entry_price - 1.0 END), 4) AS mean_return_60s
                    FROM paper_cohorts c
                    JOIN lessons l ON l.id=c.entry_id
                    JOIN pass_creators pc
                        ON pc.creator=json_extract(l.raw, '$.gate.last_event.creator')
                    LEFT JOIN paper_marks p60
                        ON p60.entry_id=c.entry_id AND p60.horizon_s=60
                    {grad_join}
                    GROUP BY creator_cohort
                    ORDER BY planned_entries DESC, creator_cohort"""  # noqa: S608
            )
        ]
        roll_col = ""
        if has_grad:
            g_cols = {row[1] for row in conn.execute("PRAGMA table_info(grad_marks)")}
            roll_col = _roll_col_sql(g_cols)
            conn.create_aggregate("median", 1, _MedianAggregate)
            out["executable_funnel"] = [
                dict(row)
                for row in conn.execute(
                    f"""WITH pass_creators AS (
                            SELECT json_extract(raw, '$.gate.last_event.creator') AS creator,
                                COUNT(DISTINCT mint) AS pass_mints
                            FROM lessons
                            WHERE kind='gate_pass'
                                AND json_extract(raw, '$.gate.last_event.creator') IS NOT NULL
                                AND json_extract(raw, '$.gate.last_event.creator') != ''
                            GROUP BY 1
                        ),
                        base AS (
                            SELECT
                                c.entry_id,
                                l.buyers,
                                COALESCE(l.mayhem, 0) AS mayhem,
                                CAST(
                                    json_extract(l.raw, '$.gate.slots_waited') AS INTEGER
                                ) AS slots_waited,
                                CAST(
                                    json_extract(l.raw, '$.gate.last_event.sol_amount')
                                    AS REAL
                                ) / 1e9 AS last_buy_sol,
                                COALESCE(
                                    json_extract(l.raw, '$.gate.last_event.user'),
                                    ''
                                ) AS trigger_user,
                                COALESCE(pc.pass_mints, 1) AS pass_mints,
                                g.grad_elapsed_s,
                                g.open_price,
                                p60.entry_price,
                                p60.exit_price,
                                CASE
                                    WHEN p60.exit_price IS NOT NULL
                                    THEN p60.exit_price / p60.entry_price - 1.0
                                END AS r60,
                                CASE
                                    WHEN g.entry_id IS NOT NULL AND g.open_price > 0 THEN
                                        CASE
                                            WHEN g.open_price <= p60.entry_price
                                            THEN g.open_price / p60.entry_price - 1.0
                                            ELSE COALESCE(
                                                {roll_col}g.p120, g.p30, g.p5, g.open_price
                                            ) / p60.entry_price - 1.0
                                        END
                                    WHEN p60.exit_price IS NOT NULL
                                    THEN p60.exit_price / p60.entry_price - 1.0
                                END AS policy_return
                            FROM paper_cohorts c
                            JOIN lessons l ON l.id = c.entry_id
                            LEFT JOIN paper_marks p60
                                ON p60.entry_id = c.entry_id AND p60.horizon_s = 60
                            LEFT JOIN grad_marks g ON g.entry_id = c.entry_id
                            LEFT JOIN pass_creators pc
                                ON pc.creator = json_extract(
                                    l.raw, '$.gate.last_event.creator'
                                )
                        )
                        SELECT
                            'unexecutable_poll1_bundle' AS stage,
                            COUNT(*) AS planned_entries,
                            COUNT(exit_price) AS observed_60s_n,
                            COUNT(open_price) AS graduated_n,
                            COALESCE(
                                SUM(CASE WHEN exit_price > entry_price THEN 1 ELSE 0 END),
                                0
                            ) AS win_60s_n,
                            ROUND(AVG(r60), 4) AS mean_return_60s,
                            ROUND(median(r60), 4) AS median_return_60s,
                            ROUND(AVG(policy_return), 4) AS mean_policy_return
                        FROM base WHERE grad_elapsed_s < 4.0
                        UNION ALL
                        SELECT
                            'executable_curve_all' AS stage,
                            COUNT(*) AS planned_entries,
                            COUNT(exit_price) AS observed_60s_n,
                            COUNT(open_price) AS graduated_n,
                            COALESCE(
                                SUM(CASE WHEN exit_price > entry_price THEN 1 ELSE 0 END),
                                0
                            ) AS win_60s_n,
                            ROUND(AVG(r60), 4) AS mean_return_60s,
                            ROUND(median(r60), 4) AS median_return_60s,
                            ROUND(AVG(policy_return), 4) AS mean_policy_return
                        FROM base WHERE grad_elapsed_s IS NULL OR grad_elapsed_s >= 4.0
                        UNION ALL
                        SELECT
                            'executable_in_band_post_slot' AS stage,
                            COUNT(*) AS planned_entries,
                            COUNT(exit_price) AS observed_60s_n,
                            COUNT(open_price) AS graduated_n,
                            COALESCE(
                                SUM(CASE WHEN exit_price > entry_price THEN 1 ELSE 0 END),
                                0
                            ) AS win_60s_n,
                            ROUND(AVG(r60), 4) AS mean_return_60s,
                            ROUND(median(r60), 4) AS median_return_60s,
                            ROUND(AVG(policy_return), 4) AS mean_policy_return
                        FROM base
                        WHERE (grad_elapsed_s IS NULL OR grad_elapsed_s >= 4.0)
                            AND buyers BETWEEN 2 AND 3
                            AND mayhem = 0
                            AND slots_waited >= 1
                        UNION ALL
                        SELECT
                            'executable_full_gate' AS stage,
                            COUNT(*) AS planned_entries,
                            COUNT(exit_price) AS observed_60s_n,
                            COUNT(open_price) AS graduated_n,
                            COALESCE(
                                SUM(CASE WHEN exit_price > entry_price THEN 1 ELSE 0 END),
                                0
                            ) AS win_60s_n,
                            ROUND(AVG(r60), 4) AS mean_return_60s,
                            ROUND(median(r60), 4) AS median_return_60s,
                            ROUND(AVG(policy_return), 4) AS mean_policy_return
                        FROM base
                        WHERE (grad_elapsed_s IS NULL OR grad_elapsed_s >= 4.0)
                            AND buyers BETWEEN 2 AND 3
                            AND mayhem = 0
                            AND slots_waited >= 1
                            AND COALESCE(last_buy_sol, 0.0) <= 45.0
                            AND trigger_user
                                != 'BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s'
                            AND pass_mints <= 1"""  # noqa: S608
                )
            ]
        out["paper_by_buyer_cohort"] = _paper_buyer_cohorts(
            conn,
            has_grad=has_grad,
            grad_join=grad_join,
            roll_col=roll_col,
        )
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grad_marks'"
    ).fetchone():
        g_cols = {row[1] for row in conn.execute("PRAGMA table_info(grad_marks)")}
        out["grad_decay"] = graduation_decay(conn)
        grad_marks = _grad_marks_summary(conn, g_cols)
        if grad_marks:
            out["grad_marks"] = grad_marks
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_flow_marks'"
    ).fetchone():
        out["paper_flow_exits"] = [
            dict(row)
            for row in conn.execute(
                """SELECT pf.rule AS flow_exit_rule,
                    COUNT(*) AS flow_exit_n,
                    ROUND(AVG(pf.elapsed_s), 4) AS mean_elapsed_s,
                    ROUND(AVG(
                        CASE WHEN p60.entry_price > 0
                        THEN pf.exit_price / p60.entry_price - 1.0 END
                    ), 4) AS mean_return_vs_entry
                FROM paper_flow_marks pf
                LEFT JOIN paper_marks p60
                    ON p60.entry_id = pf.entry_id AND p60.horizon_s = 60
                GROUP BY pf.rule ORDER BY flow_exit_n DESC"""
            )
        ]
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reclaim_marks'"
    ).fetchone():
        out["reclaim_marks"] = {
            "graduated_n": conn.execute(
                "SELECT COUNT(*) FROM grad_marks WHERE open_price > 0"
            ).fetchone()[0],
            "triggered_n": conn.execute(
                "SELECT COUNT(*) FROM reclaim_marks"
            ).fetchone()[0],
            "by_rule": [
                dict(row)
                for row in conn.execute(
                    """SELECT r.reclaim_rule AS rule,
                        COUNT(*) AS n,
                        ROUND(AVG(
                            r.reclaim_exit_price / r.reclaim_entry_price - 1.0
                        ), 4) AS mean_gross_return,
                        ROUND(AVG(r.reclaim_elapsed_s), 4) AS mean_elapsed_s,
                        SUM(r.reclaim_exit_price IS NULL) AS censored_n
                    FROM reclaim_marks r
                    GROUP BY r.reclaim_rule ORDER BY n DESC"""
                )
            ],
        }
    return out


def load_report(limit: int = 15, db_path: Path = DB) -> dict:
    """Read one consistent snapshot without modifying the historical journal."""
    if not db_path.exists():
        return {"error": f"no journal at {db_path}"}
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lessons)")}
    if "outcome_source" not in columns:
        conn.close()
        return {
            "error": (
                "journal predates the live-close marker; start the bot once so "
                "LessonJournal migrates it, then reload"
            )
        }
    try:
        conn.execute("BEGIN")
        conn.execute(
            """CREATE TEMP VIEW nonpaper_outcomes AS
            SELECT * FROM lessons WHERE outcome_source = 'live_close'"""
        )
        out: dict = {}
        out["report_schema_version"] = 2
        out["snapshot"] = {
            "generated_utc": datetime.now(UTC).isoformat(),
            **dict(
                conn.execute(
                    "SELECT MAX(id) AS last_lesson_id, MAX(utc) AS latest_lesson_utc"
                    " FROM lessons"
                ).fetchone()
            ),
        }
        out["total"] = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        out["by_kind"] = dict(
            conn.execute("SELECT kind, COUNT(*) FROM lessons GROUP BY kind")
        )
        out["scored"] = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE jev_quality IS NOT NULL"
        ).fetchone()[0]
        out["resolved"] = conn.execute(
            "SELECT COUNT(*) FROM nonpaper_outcomes"
        ).fetchone()[0]
        out["excluded_unclassified"] = conn.execute(
            """SELECT COUNT(*) FROM lessons WHERE outcome_source IS NULL
            AND (outcome_utc IS NOT NULL OR outcome_reason IS NOT NULL
                 OR outcome_pnl_sol IS NOT NULL
                 OR outcome_pnl_quote_raw IS NOT NULL)"""
        ).fetchone()[0]
        out["excluded_legacy_paper"] = conn.execute(
            """SELECT COUNT(*) FROM lessons WHERE kind LIKE 'horizon_%'
            OR outcome_reason LIKE 'paper_exit_%'
            OR outcome_reason='invalid_migration_artifact'"""
        ).fetchone()[0]
        out.update(_paper_evidence(conn))
        out["creator_fee_summary"] = _creator_fee_summary(
            db_path.parent / "creator_fee_snapshots.jsonl"
        )
        out["promotion_allowed"] = False
        out["measurement_note"] = (
            "Only rows marked live_close are eligible; older outcomes without "
            "that mark stay excluded rather than passing a name filter. "
            "Averages are SOL-denominated: a close in another quote asset keeps "
            "its raw units and mint per row and is left out of these "
            "aggregates. The amount is swap spread, not fee-inclusive net "
            "profit. Legacy paper PnL is excluded: invalid real-reserve "
            "pricing. New marks are gross marginal returns, not fills or net "
            "profit. Missing/migrated/cancelled marks remain visible; "
            "complete-pair selection can bias results. No strategy or model "
            "promotion follows."
        )
        out["pnl_by_quality"] = [
            dict(r)
            for r in conn.execute(
                "SELECT ROUND(jev_quality, 1) AS q, COUNT(*) AS n,"
                " ROUND(AVG(outcome_pnl_sol), 8) AS avg_pnl,"
                " ROUND(SUM(outcome_pnl_sol), 8) AS total_pnl"
                " FROM nonpaper_outcomes"
                " WHERE jev_quality IS NOT NULL AND outcome_pnl_sol IS NOT NULL"
                " GROUP BY q ORDER BY q"
            )
        ]
        out["battery_corr"] = _battery_correlations(
            conn,
            (
                "jev_quality",
                "jev_copycat",
                "jev_dump_risk",
                "jev_organic",
                "jev_liq_trap",
            ),
        )
        out["pnl_by_copycat"] = [
            dict(r)
            for r in conn.execute(
                "SELECT CASE WHEN jev_copycat >= 0.8 THEN 'copycat(>=0.8)'"
                " WHEN jev_copycat < 0.2 THEN 'organic(<0.2)'"
                " ELSE 'mixed' END AS cohort, COUNT(*) AS n,"
                " ROUND(AVG(outcome_pnl_sol), 8) AS avg_pnl"
                " FROM nonpaper_outcomes"
                " WHERE jev_copycat IS NOT NULL AND outcome_pnl_sol IS NOT NULL"
                " GROUP BY cohort"
            )
        ]
        out["skip_reasons"] = dict(
            conn.execute(
                "SELECT decision, COUNT(*) FROM lessons"
                " WHERE kind='gate_skip' GROUP BY decision ORDER BY COUNT(*) DESC"
            )
        )
        out["observation_exclusions"] = [
            dict(row)
            for row in conn.execute(
                """SELECT decision AS reason, COUNT(*) AS n,
                    COUNT(DISTINCT mint) AS distinct_mints,
                    AVG(json_extract(raw, '$.queue_age_s')) AS mean_queue_age_s,
                    MAX(json_extract(raw, '$.queue_age_s')) AS max_queue_age_s,
                    MIN(utc) AS first_utc, MAX(utc) AS last_utc
                FROM lessons WHERE kind='observation_skip'
                GROUP BY decision ORDER BY n DESC, reason"""
            )
        ]
        out["recent"] = [
            dict(r)
            for r in conn.execute(
                "SELECT utc, kind, symbol, decision, jev_quality, jev_copycat,"
                " outcome_pnl_sol, outcome_pnl_quote_raw, outcome_quote_mint,"
                " outcome_source, outcome_reason FROM lessons"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]
        return out
    finally:
        conn.close()


def _print_no_outcomes() -> None:
    """Keep gross shadow marks distinct from actual trade outcomes."""
    print("\nNo eligible live trade outcomes. Paper marks do not count as fills.")


def _print_quality_evidence(data: dict) -> None:
    """Descriptive associations only; quality is not a win probability."""
    print("\n--- Live outcome associations by raw Jev quality (not calibration) ---")
    print(f"{'score':>6} {'n':>6} {'avg pnl SOL':>14} {'total':>12}")
    print("  (SOL-denominated closes only; other quote assets keep raw units)")
    for row in data["pnl_by_quality"]:
        print(
            f"{row['q']:>6} {row['n']:>6} {row['avg_pnl']:>14} {row['total_pnl']:>12}"
        )
    corr = data.get("battery_corr") or []
    meaningful = [c for c in corr if c["r"] is not None]
    if meaningful:
        print("\n--- Battery signal correlation with outcome (r, on resolved) ---")
        for c in corr:
            rr = f"{c['r']:+.3f}" if c["r"] is not None else "n/a (sample or variance)"
            print(f"  {c['signal']:<18} n={c['n']:<4} r={rr}")


def _print_paper_evidence(data: dict) -> None:
    """Explain coverage and conditional comparisons without a promotion verdict."""
    coverage = data["paper_coverage"]
    print(
        f"paper entries: {coverage['planned_entries']}, paired: {coverage['paired_entries']},"
        f" distinct mints: {coverage['distinct_mints']} (paired: {coverage['paired_mints']})"
    )
    print(
        f"pending entries: {coverage['pending_entries']},"
        f" entries with censoring: {coverage['censored_entries']}"
        " (overlapping counts; pending is not proof of liveness)"
    )
    if not coverage["paired_entries"]:
        print("No complete three-horizon cohorts; no comparative horizon conclusion.")
    print("\n--- Gross mark returns, exact paired cohorts; not net PnL ---")
    for row in data["paper_marks"]:
        print(
            f"  {row['horizon_s']}s: planned={row['planned']}"
            f" marked={row['marked']} censored={row['censored']}"
            f" pending={row['pending']} paired={row['paired_n']}"
            f" paired_mean_return={row['paired_mean_return']}"
        )
    for row in data["paper_comparisons"]:
        print(
            f"  {row['horizon_s']}s vs 60s: n={row['paired_n']}"
            f" mean return difference={row['mean_delta_vs_60s']:+.6f}"
            f" improved={row['improved']} worsened={row['worsened']}"
            f" unchanged={row['unchanged']} (descriptive, not a policy ranking)"
        )
    if data.get("paper_by_entry_timing"):
        print("\n--- Paper entries by band & creation-slot timing ---")
        for row in data["paper_by_entry_timing"]:
            print(
                f"  {row['band']} | {row['entry_timing']}:"
                f" planned={row['planned_entries']}"
                f" paired={row['paired_entries']}"
                f" grad={row['graduated_n']}"
                f" win_60s={row['win_60s_n']}"
                f" mean_return_60s={row['mean_return_60s']}"
            )
    if data["paper_censors"]:
        print("\n--- Missing observations: causes and next checks ---")
        for row in data["paper_censors"]:
            print(
                f"  {row['horizon_s']}s {row['reason']}: {row['n']} — {row['next_step']}"
            )
    _print_cohort_and_fee_evidence(data)


def _print_cohort_and_fee_evidence(data: dict) -> None:
    if data.get("paper_by_creator_cohort"):
        print("\n--- Paper entries by creator cohort ---")
        for row in data["paper_by_creator_cohort"]:
            print(
                f"  {row['creator_cohort']}:"
                f" planned={row['planned_entries']}"
                f" creators={row['distinct_creators']}"
                f" grad={row['graduated_n']}"
                f" win_60s={row['win_60s_n']}"
                f" mean_return_60s={row['mean_return_60s']}"
            )
    if data.get("paper_by_buyer_cohort"):
        print("\n--- Paper entries by trigger buyer cohort ---")
        for row in data["paper_by_buyer_cohort"]:
            print(
                f"  {row['buyer_cohort']}:"
                f" planned={row['planned_entries']}"
                f" buyers={row['distinct_buyers']}"
                f" grad={row['graduated_n']}"
                f" win_60s={row['win_60s_n']}"
                f" mean_return_60s={row['mean_return_60s']}"
                f" mean_policy={row['mean_policy_return']}"
            )
    if data.get("executable_funnel"):
        print("\n--- Executable entry funnel & policy marks ---")
        for row in data["executable_funnel"]:
            print(
                f"  {row['stage']}:"
                f" planned={row['planned_entries']}"
                f" obs_60s={row['observed_60s_n']}"
                f" grad={row['graduated_n']}"
                f" win_60s={row['win_60s_n']}"
                f" mean_60s={row['mean_return_60s']}"
                f" med_60s={row['median_return_60s']}"
                f" mean_policy={row['mean_policy_return']}"
            )
    if data.get("paper_flow_exits"):
        print("\n--- Paper flow exits (FlowMonitor curve rules) ---")
        for row in data["paper_flow_exits"]:
            print(
                f"  {row['flow_exit_rule']}:"
                f" n={row['flow_exit_n']}"
                f" mean_elapsed_s={row['mean_elapsed_s']}"
                f" mean_return_vs_entry={row['mean_return_vs_entry']}"
            )
    if data.get("reclaim_marks"):
        rm = data["reclaim_marks"]
        print(
            f"\n--- Post-graduation reclaim arm"
            f" (graduated={rm['graduated_n']},"
            f" triggered={rm['triggered_n']}) ---"
        )
        for row in rm["by_rule"]:
            print(
                f"  {row['rule']}:"
                f" n={row['n']}"
                f" mean_gross_return={row['mean_gross_return']}"
                f" mean_elapsed_s={row['mean_elapsed_s']}"
                f" censored={row['censored_n']}"
            )
    _print_grad_and_fee_evidence(data)


def _print_grad_marks(gm: dict | None) -> None:
    if not gm or not gm.get("graduated_n"):
        return
    print(
        f"\n--- Graduation & rollover marks (n={gm['graduated_n']} graduated,"
        f" {gm['rollover_n']} rollover) ---"
    )
    if gm.get("peak_n") and gm.get("mean_peak_vs_open") is not None:
        peak_elapsed = gm.get("mean_peak_elapsed_s") or 0.0
        print(
            f"  peak vs pool-open: {gm['mean_peak_vs_open']:+.4f}"
            f" (mean peak G+{peak_elapsed:.1f}s)"
            f" | rallied>=1%: {gm['rallied_1pct_n']}"
            f" whipsaw<open: {gm['whipsaw_below_open_n']}"
            f" early_whipsaw<=10s: {gm.get('early_whipsaw_count', 0)}"
            f" rollover>=open: {gm['rollover_above_open_n']}"
        )
    if gm["rollover_n"] and gm["mean_rollover_vs_open"] is not None:
        rollover_elapsed = gm.get("mean_rollover_elapsed_s") or 0.0
        shakeout = gm.get("post_shakeout_p30_vs_open")
        shakeout_str = (
            f" | p5-confirmed shakeout->p30 vs open: {shakeout:+.4f}"
            if shakeout is not None
            else ""
        )
        print(
            f"  rollover vs pool-open: {gm['mean_rollover_vs_open']:+.4f}"
            f" (mean trigger G+{rollover_elapsed:.1f}s)"
            f"{shakeout_str}"
        )
    if (
        gm.get("stabilized_rollover_n")
        and gm.get("mean_stabilized_rollover_vs_open") is not None
    ):
        stab_elapsed = gm.get("mean_stabilized_rollover_elapsed_s") or 0.0
        print(
            f"  stabilized rollover vs pool-open:"
            f" {gm['mean_stabilized_rollover_vs_open']:+.4f}"
            f" (n={gm['stabilized_rollover_n']}, mean trigger G+{stab_elapsed:.1f}s)"
        )


def _print_grad_and_fee_evidence(data: dict) -> None:
    _print_grad_marks(data.get("grad_marks"))
    cf = data.get("creator_fee_summary")
    if cf:
        print(
            f"\n--- Creator-vault fee snapshots ({cf['snapshot_count']} snapshots,"
            f" latest {cf['latest_ts']}) ---"
        )
        print(
            f"  pools={cf['distinct_latest_pools']}"
            f" vaults={cf['unique_observed_creator_vaults']}"
            f" pools_with_withdrawals={cf['pools_with_withdrawals']}"
            f" ({cf['total_withdrawals']} withdrawals)"
        )
        print(
            f"  current_wsol={cf['current_balance_wsol']:.4f}"
            f" accrued_wsol={cf['total_accrued_wsol']:.4f}"
            f" withdrawn_wsol={cf['total_withdrawn_wsol']:.4f}"
            f" implied_volume_sol={cf['total_implied_volume_sol']:.1f}"
        )


def _render(data: dict) -> None:
    if "error" in data:
        print(data["error"])
        return
    print("=== Learning journal ===")
    print(
        f"lessons: {data['total']}  (jev-scored: {data['scored']},"
        f" resolved outcomes: {data['resolved']})"
    )
    print(f"by kind: {data['by_kind']}")
    print(f"excluded legacy paper rows: {data['excluded_legacy_paper']}")
    print(data["measurement_note"])
    _print_paper_evidence(data)
    print("\n--- gate skip reasons (where rejections go) ---")
    for reason, n in list(data["skip_reasons"].items())[:6]:
        print(f"  {n:6d}  {reason}")
    if data["observation_exclusions"]:
        print("\n--- Discovery/queue exclusions (events, not gate outcomes) ---")
        for row in data["observation_exclusions"]:
            print(
                f"  {row['reason']}: events={row['n']} mints={row['distinct_mints']}"
                f" mean_queue_age_s={row['mean_queue_age_s']}"
                f" max_queue_age_s={row['max_queue_age_s']}"
            )

    if not data["resolved"]:
        _print_no_outcomes()
    else:
        _print_quality_evidence(data)

    if data["pnl_by_copycat"]:
        print("\n--- PnL by copycat cohort ---")
        for row in data["pnl_by_copycat"]:
            print(f"  {row['cohort']:>15} n={row['n']}  avg={row['avg_pnl']}")

    print(
        f"\n--- last {len(data['recent'])} lessons (archive; legacy PnL ineligible) ---"
    )
    for r in data["recent"]:
        score = f"{r['jev_quality']:.2f}" if r["jev_quality"] is not None else "-"
        outcome = (
            f"{r['outcome_pnl_sol']:+.6f}"
            if r["outcome_pnl_sol"] is not None
            else "unresolved"
        )
        print(
            f"  {r['utc'][11:19]} {r['kind']:<10} {str(r['symbol'])[:12]:<12}"
            f" q={score} {r['decision'] or '-':<16} {outcome:>10}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--limit", type=int, default=15, help="recent rows")
    parser.add_argument("--db", type=Path, default=DB, help="read-only journal path")
    parser.add_argument(
        "--output", type=Path, help="save JSON evidence snapshot; refuses to overwrite"
    )
    args = parser.parse_args()
    data = load_report(args.limit, args.db)
    if args.output is not None and "error" not in data:
        payload = json.dumps(data, indent=2, allow_nan=False) + "\n"
        try:
            with args.output.open("x", encoding="utf-8") as destination:
                destination.write(payload)
        except OSError as exc:
            parser.exit(1, f"Cannot save evidence report: {type(exc).__name__}\n")
    if args.json:
        print(json.dumps(data, indent=1, default=str))
    else:
        _render(data)
    return int("error" in data)


if __name__ == "__main__":
    sys.exit(main())
