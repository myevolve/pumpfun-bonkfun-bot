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
from datetime import UTC, datetime
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


def _paper_evidence(conn: sqlite3.Connection) -> dict:
    """Expose missingness and within-entry differences, never unpaired rankings."""
    out = {
        "paper_marks": [],
        "paper_censors": [],
        "paper_comparisons": [],
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
    return out


def load_report(limit: int = 15, db_path: Path = DB) -> dict:
    """Read one consistent snapshot without modifying the historical journal."""
    if not db_path.exists():
        return {"error": f"no journal at {db_path}"}
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        conn.execute(
            """CREATE TEMP VIEW nonpaper_outcomes AS
            SELECT * FROM lessons WHERE outcome_pnl_sol IS NOT NULL
                AND kind NOT LIKE 'horizon_%'
                AND COALESCE(outcome_reason, '') NOT LIKE 'paper_exit_%'
                AND COALESCE(outcome_reason, '') != 'invalid_migration_artifact'"""
        )
        out: dict = {}
        out["report_schema_version"] = 1
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
        out["excluded_legacy_paper"] = conn.execute(
            """SELECT COUNT(*) FROM lessons WHERE kind LIKE 'horizon_%'
            OR outcome_reason LIKE 'paper_exit_%'
            OR outcome_reason='invalid_migration_artifact'"""
        ).fetchone()[0]
        out.update(_paper_evidence(conn))
        out["promotion_allowed"] = False
        out["measurement_note"] = (
            "Legacy paper PnL is excluded: invalid real-reserve pricing. "
            "New marks are gross marginal returns, not fills or net profit. "
            "Missing/migrated/cancelled marks remain visible; complete-pair "
            "selection can bias results. No strategy or model promotion follows."
        )
        out["pnl_by_quality"] = [
            dict(r)
            for r in conn.execute(
                "SELECT ROUND(jev_quality, 1) AS q, COUNT(*) AS n,"
                " ROUND(AVG(outcome_pnl_sol), 8) AS avg_pnl,"
                " ROUND(SUM(outcome_pnl_sol), 8) AS total_pnl"
                " FROM nonpaper_outcomes WHERE jev_quality IS NOT NULL"
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
                " FROM nonpaper_outcomes WHERE jev_copycat IS NOT NULL GROUP BY cohort"
            )
        ]
        out["skip_reasons"] = dict(
            conn.execute(
                "SELECT decision, COUNT(*) FROM lessons"
                " WHERE kind='gate_skip' GROUP BY decision ORDER BY COUNT(*) DESC"
            )
        )
        out["recent"] = [
            dict(r)
            for r in conn.execute(
                "SELECT utc, kind, symbol, decision, jev_quality, jev_copycat,"
                " outcome_pnl_sol, outcome_reason FROM lessons"
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
    if data["paper_censors"]:
        print("\n--- Missing observations: causes and next checks ---")
        for row in data["paper_censors"]:
            print(
                f"  {row['horizon_s']}s {row['reason']}: {row['n']} — {row['next_step']}"
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
