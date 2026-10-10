"""Reverse-engineer successful accepts — TRAIN/LOCK correlation study.

Discipline (per the threshold-miner lesson):
 - cohort: the 40 accepted entries with a resolved 60s mark; outcomes
   r60/r300/r900 from paper_marks; decision-time features only
   (buyers, real_sol, mayhem, hour-of-day, fund band).
 - split: TRAIN = first 24 by utc, LOCK = last 16. Candidates are read
   on TRAIN, then re-tested on LOCK; a "survivor" is sign-consistent
   with a TRAIN effect.
 - candidates are counted: 6 features x 2 tests = 12 tried. The count is
   reported so nobody mistakes a survivor for a discovery.
 - n=40 is small; this is a descriptive study that generates
   PRE-REGISTERED hypotheses for the >=30-cohort referee, not edges.

Read-only; no funds.
"""

import sqlite3
import statistics
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / ".state/learning/lessons.sqlite3"
TRAIN_N = 24


def main() -> None:
    db = sqlite3.connect(str(DB))
    rows = db.execute(
        "SELECT l.id, l.utc, l.buyers, l.real_sol, l.mayhem, l.symbol,"
        " p60.entry_price, p60.exit_price,"
        " p300.exit_price, p900.exit_price"
        " FROM lessons l"
        " JOIN paper_marks p60 ON p60.entry_id=l.id AND p60.horizon_s=60"
        " JOIN paper_marks p300 ON p300.entry_id=l.id AND p300.horizon_s=300"
        " JOIN paper_marks p900 ON p900.entry_id=l.id AND p900.horizon_s=900"
        " WHERE l.decision='buyers_present'"
        " AND p60.exit_price IS NOT NULL AND p300.exit_price IS NOT NULL"
        " AND p900.exit_price IS NOT NULL"
        " ORDER BY l.utc"
    ).fetchall()
    print(f"cohort: {len(rows)} resolved accepts")
    feats = []
    for lid, utc, buyers, real_sol, mayhem, symbol, e60_in, e60, e300, e900 in rows:
        entry = e60_in
        r60 = (e60 / entry - 1) if entry else None
        r300 = (e300 / entry - 1) if entry else None
        r900 = (e900 / entry - 1) if entry else None
        hour = int(utc[11:13])
        feats.append(
            {
                "id": lid,
                "utc": utc,
                "buyers": buyers or 0,
                "real_sol": real_sol or 0.0,
                "mayhem": bool(mayhem),
                "hour": hour,
                "r60": r60,
                "r300": r300,
                "r900": r900,
            }
        )
    train, lock = feats[:TRAIN_N], feats[TRAIN_N:]
    print(f"TRAIN n={len(train)}  LOCK n={len(lock)}")
    print(f"TRAIN window: {train[0]['utc'][:16]} .. {train[-1]['utc'][:16]}")
    print(f"LOCK  window: {lock[0]['utc'][:16]} .. {lock[-1]['utc'][:16]}")
    print()

    med60 = statistics.median(f["r60"] for f in feats)
    winners = [f for f in feats if f["r60"] >= med60]
    losers = [f for f in feats if f["r60"] < med60]
    print(
        f"r60 median: {med60:.3f}  winners(>=med): {len(winners)}  "
        f"losers: {len(losers)}"
    )
    print()
    print("== descriptive medians (winners vs losers) ==")
    for key, label in (
        ("buyers", "buyers at accept"),
        ("real_sol", "real_sol at accept"),
        ("hour", "accept hour (UTC)"),
    ):
        mw = statistics.median(f[key] for f in winners)
        ml = statistics.median(f[key] for f in losers)
        print(f"  {label:<20} winners {mw:>10.1f}   losers {ml:>10.1f}")
    mw_may = sum(f["mayhem"] for f in winners) / len(winners)
    ml_may = sum(f["mayhem"] for f in losers) / len(losers)
    print(f"  mayhem share         winners {mw_may:>10.1%}   losers {ml_may:>10.1%}")
    print()

    def _ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        ranks = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    def spearman(xs, ys):
        rx, ry = _ranks(xs), _ranks(ys)
        n = len(xs)
        mx, my = sum(rx) / n, sum(ry) / n
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=False))
        den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
        return num / den if den else 0.0

    print("== pre-registered candidates (12 tried) ==")
    print(f"{'feature':<22} {'TRAIN rho':>10} {'LOCK rho':>10}  survivor?")
    for key, label in (
        ("buyers", "buyers"),
        ("real_sol", "real_sol"),
        ("hour", "hour-of-day"),
    ):
        for outcome in ("r60", "r300", "r900"):
            t_rho = spearman([f[key] for f in train], [f[outcome] for f in train])
            l_rho = spearman([f[key] for f in lock], [f[outcome] for f in lock])
            surv = t_rho * l_rho > 0 and abs(l_rho) > 0.25
            print(
                f"{label + ' x ' + outcome:<22} {t_rho:>10.3f} "
                f"{l_rho:>10.3f}  {'YES' if surv else 'no'}"
            )
    print()
    print("== outcome medians by fund band (TRAIN/LOCK pooled, all descriptive) ==")
    for lo, hi in ((0, 62), (62, 75), (75, 1000)):
        band = [f for f in feats if lo <= f["real_sol"] < hi]
        if not band:
            continue
        m = lambda k: statistics.median(f[k] for f in band)
        print(
            f"  band {lo}-{hi} SOL (n={len(band)}): r60 {m('r60'):.3f} "
            f"r300 {m('r300'):.3f} r900 {m('r900'):.3f}"
        )
    print()
    print("== biggest winners (r60 top 5, full rows) ==")
    for f in sorted(feats, key=lambda f: -f["r60"])[:5]:
        print(
            f"  {f['utc'][:16]} id={f['id']} r60 {f['r60']:.2f} "
            f"r300 {f['r300']:.2f} r900 {f['r900']:.2f} "
            f"buyers {f['buyers']} real_sol {f['real_sol']:.1f} "
            f"mayhem {f['mayhem']} hour {f['hour']}"
        )
    print()
    print("== biggest losers (r60 bottom 5) ==")
    for f in sorted(feats, key=lambda f: f["r60"])[:5]:
        print(
            f"  {f['utc'][:16]} id={f['id']} r60 {f['r60']:.2f} "
            f"r300 {f['r300']:.2f} r900 {f['r900']:.2f} "
            f"buyers {f['buyers']} real_sol {f['real_sol']:.1f} "
            f"mayhem {f['mayhem']} hour {f['hour']}"
        )
    db.close()


if __name__ == "__main__":
    main()
