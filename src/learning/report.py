"""Learning report: turn the lesson journal into actionable evidence.

Read-only. Prints what the journal has learned so far and whether Jev
quality scores predict realized outcomes. The promotion decision for
Jev-in-the-gate comes from the pnl_by_quality table once enough outcomes
resolve - this report is the CLI surface for that evidence.

Usage:
    uv run python -m learning.report            # summary
    uv run python -m learning.report --json     # machine-readable
    uv run python -m learning.report --limit 20 # more recent lessons
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

DB = Path(".state/learning/lessons.sqlite3")

# Jev promotion thresholds on the observed 0-1 quality scale: >= HI_MIN is
# "high", <= LO_MAX is "low". The verdict compares realized PnL across the two.
_MIN_COHORT_N = 10  # verdicts need at least this many outcomes per cohort
HI_MIN = 0.6
LO_MAX = 0.4


def _battery_correlations(conn: sqlite3.Connection, signals: tuple) -> list[dict]:
    """Pearson r(signal, pnl) per battery signal on resolved outcomes."""
    # Signal names are module-owned constants, never user input.
    cols = ", ".join(signals)
    rows = conn.execute(
        f"SELECT {cols}, outcome_pnl_sol FROM lessons"  # noqa: S608
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
        r = cov / (sx * sy) if sx and sy else 0.0
        out.append({"signal": name, "n": len(pairs), "r": round(r, 3)})
    return out


def _load(limit: int) -> dict:
    if not DB.exists():
        return {"error": f"no journal at {DB} - run the bot first"}
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    try:
        out: dict = {}
        out["total"] = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        out["by_kind"] = {
            r["kind"]: r["n"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM lessons GROUP BY kind"
            )
        }
        out["scored"] = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE jev_quality IS NOT NULL"
        ).fetchone()[0]
        out["resolved"] = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
        ).fetchone()[0]

        # Jev-vs-outcome evidence: the promotion table
        out["pnl_by_quality"] = [
            dict(r)
            for r in conn.execute(
                "SELECT ROUND(jev_quality, 1) AS q, COUNT(*) AS n,"
                " ROUND(AVG(outcome_pnl_sol), 8) AS avg_pnl,"
                " ROUND(SUM(outcome_pnl_sol), 8) AS total_pnl"
                " FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
                " AND jev_quality IS NOT NULL GROUP BY q ORDER BY q"
            )
        ]
        # Calibration: Brier score of quality-as-win-probability vs the
        # no-information base rate (win_rate*(1-win_rate)).
        out["win_rate"] = conn.execute(
            "SELECT AVG(CASE WHEN outcome_pnl_sol > 0 THEN 1.0 ELSE 0.0 END)"
            " FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
        ).fetchone()[0]
        rows_b = conn.execute(
            "SELECT jev_quality, outcome_pnl_sol FROM lessons"
            " WHERE outcome_pnl_sol IS NOT NULL AND jev_quality IS NOT NULL"
        ).fetchall()
        if len(rows_b) >= _MIN_COHORT_N:
            out["brier_win"] = round(
                sum(
                    (max(0.0, min(1.0, q)) - (1.0 if p > 0 else 0.0)) ** 2
                    for q, p in rows_b
                )
                / len(rows_b),
                6,
            )
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

        # Copycat cohort: do flagged coins fare worse?
        out["pnl_by_copycat"] = [
            dict(r)
            for r in conn.execute(
                "SELECT CASE WHEN jev_copycat >= 0.8 THEN 'copycat(>=0.8)'"
                " WHEN jev_copycat < 0.2 THEN 'organic(<0.2)'"
                " ELSE 'mixed' END AS cohort, COUNT(*) AS n,"
                " ROUND(AVG(outcome_pnl_sol), 8) AS avg_pnl"
                " FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
                " AND jev_copycat IS NOT NULL GROUP BY cohort"
            )
        ]

        # Skip reason distribution - where does the gate spend its rejections?
        out["skip_reasons"] = {
            r["decision"]: r["n"]
            for r in conn.execute(
                "SELECT decision, COUNT(*) AS n FROM lessons"
                " WHERE kind='gate_skip' GROUP BY decision ORDER BY n DESC"
            )
        }

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
    """Early render when the journal has no resolved outcomes yet."""
    print(
        "\nNo resolved outcomes yet - evidence tables appear when"
        " paper fills resolve (~60s after a gate accept) or live"
        " trades exit."
    )
    print("Keep the bot running through a mayhem window.")


def _print_quality_evidence(data: dict) -> None:
    """Bucketed PnL by Jev quality + the calibrated verdict."""
    header = "\n--- PnL by Jev quality (promotion evidence) ---"
    if data.get("brier_win") is not None:
        wr = data.get("win_rate", 0.0)
        header += (
            f"\n    Brier(win) = {data['brier_win']:.4f}"
            f"  |  no-information base rate = {wr * (1 - wr):.4f}"
            "  (lower is better)"
        )
    print(header)
    print(f"{'score':>6} {'n':>6} {'avg pnl SOL':>14} {'total':>12}")
    for row in data["pnl_by_quality"]:
        print(
            f"{row['q']:>6} {row['n']:>6} {row['avg_pnl']:>14} {row['total_pnl']:>12}"
        )
    hi = [r for r in data["pnl_by_quality"] if r["q"] >= HI_MIN]
    lo = [r for r in data["pnl_by_quality"] if r["q"] <= LO_MAX]
    hi_pnl = sum(r["total_pnl"] for r in hi)
    lo_pnl = sum(r["total_pnl"] for r in lo)
    hi_n = sum(r["n"] for r in hi)
    lo_n = sum(r["n"] for r in lo)
    if hi_n and lo_n and min(hi_n, lo_n) < _MIN_COHORT_N:
        print(
            f"\nverdict: INSUFFICIENT COHORT BALANCE ({hi_n} hi vs {lo_n}"
            f" lo) - need >= {_MIN_COHORT_N} per side before any verdict."
            " The gate only accepts high-scored coins, so the lo cohort"
            " may never fill: compare buckets WITHIN the hi range instead."
        )
    elif hi_n and lo_n:
        verdict = (
            "Jev predictive: high scores outperform low scores"
            if hi_pnl / hi_n > lo_pnl / lo_n
            else "Jev NOT predictive on this sample"
        )
        print(f"\nverdict ({hi_n} hi vs {lo_n} lo outcomes): {verdict}")
    corr = data.get("battery_corr") or []
    meaningful = [c for c in corr if c["r"] is not None]
    if meaningful:
        print("\n--- Battery signal correlation with outcome (r, on resolved) ---")
        for c in corr:
            rr = (
                f"{c['r']:+.3f}" if c["r"] is not None else f"n/a (n<{_MIN_COHORT_N})"
            )
            print(f"  {c['signal']:<18} n={c['n']:<4} r={rr}")


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
    print("\n--- gate skip reasons (where rejections go) ---")
    for reason, n in list(data["skip_reasons"].items())[:6]:
        print(f"  {n:6d}  {reason}")

    if not data["resolved"]:
        _print_no_outcomes()
        return

    _print_quality_evidence(data)

    if data["pnl_by_copycat"]:
        print("\n--- PnL by copycat cohort ---")
        for row in data["pnl_by_copycat"]:
            print(f"  {row['cohort']:>15} n={row['n']}  avg={row['avg_pnl']}")

    print(f"\n--- last {len(data['recent'])} lessons ---")
    for r in data["recent"]:
        score = f"{r['jev_quality']:.2f}" if r["jev_quality"] is not None else "-"
        outcome = (
            f"{r['outcome_pnl_sol']:+.6f}"
            if r["outcome_pnl_sol"] is not None
            else "open"
        )
        print(
            f"  {r['utc'][11:19]} {r['kind']:<10} {str(r['symbol'])[:12]:<12}"
            f" q={score} {r['decision'] or '-':<16} {outcome:>10}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--limit", type=int, default=15, help="recent rows")
    args = parser.parse_args()
    data = _load(args.limit)
    if args.json:
        print(json.dumps(data, indent=1, default=str))
    else:
        _render(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
