"""Systematic substrate mining with train/held-out discipline.

Splits fills chronologically: first 70% train, last 30% held-out.
Grid-searches single-feature and two-feature threshold rules on train,
evaluates survivors on held-out. Reports only rules that hold.
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(".state/learning/lessons.sqlite3")


def load() -> list[tuple]:
    c = sqlite3.connect(str(DB))
    rows = c.execute(
        "SELECT utc, real_sol, buyers, mayhem, jev_quality, jev_copycat,"
        " jev_dump_risk, jev_organic, jev_liq_trap,"
        " CAST(strftime('%H', outcome_utc) AS INT) AS hour, outcome_pnl_sol"
        " FROM lessons WHERE kind='gate_pass' AND outcome_pnl_sol IS NOT NULL"
        " ORDER BY outcome_utc"
    ).fetchall()
    c.close()
    return rows


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def main() -> int:
    rows = load()
    n = len(rows)
    if n < 100:
        print(f"only {n} fills - need >=100")
        return 1
    split = int(n * 0.7)
    train, held = rows[:split], rows[split:]
    print(f"fills: {n} (train {len(train)}, held-out {len(held)})")
    print(
        f"overall: train avg {sum(r[-1] for r in train) / len(train):+.6f},"
        f" held avg {sum(r[-1] for r in held) / len(held):+.6f}"
    )

    # feature index map
    feats = {
        "real_sol": 1,
        "buyers": 2,
        "mayhem": 3,
        "jev_quality": 4,
        "jev_copycat": 5,
        "jev_dump_risk": 6,
        "jev_organic": 7,
        "jev_liq_trap": 8,
        "hour": 9,
    }

    # quantile thresholds per feature from train
    rules = []
    for name, idx in feats.items():
        vals = sorted(r[idx] for r in train if r[idx] is not None)
        if len(vals) < 40:
            continue
        for q in (0.2, 0.25, 0.33, 0.5, 0.67, 0.75, 0.8):
            thr = vals[int(len(vals) * q)]
            for op in (">=", "<="):
                sub = [
                    r
                    for r in train
                    if r[idx] is not None
                    and ((r[idx] >= thr) if op == ">=" else (r[idx] <= thr))
                ]
                if len(sub) < 30:
                    continue
                avg = sum(r[-1] for r in sub) / len(sub)
                if avg <= 0:
                    continue  # only positive train rules
                # evaluate on held-out
                hsub = [
                    r
                    for r in held
                    if r[idx] is not None
                    and ((r[idx] >= thr) if op == ">=" else (r[idx] <= thr))
                ]
                if len(hsub) < 15:
                    continue
                havg = sum(r[-1] for r in hsub) / len(hsub)
                rules.append((name, op, thr, len(sub), avg, len(hsub), havg))

    rules.sort(key=lambda x: -x[5])
    print("\npositive-on-train rules that also survive held-out (n>=15):")
    survivors = [r for r in rules if r[6] > 0]
    if not survivors:
        print("  NONE - no threshold rule holds out-of-sample")
    for name, op, thr, tn, tavg, hn, havg in survivors[:10]:
        print(
            f"  {name} {op} {thr:.4g}: train n={tn} {tavg:+.6f}"
            f" -> held n={hn} {havg:+.6f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
