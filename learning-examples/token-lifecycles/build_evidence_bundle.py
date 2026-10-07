"""Build a sealed evidence bundle: the whale-ride audit in one artifact.

This is the customer-facing form of the repo's evidence discipline. One
command produces a single JSON snapshot containing, each with its inputs
hashed:

- tape audit — the completed-curve phantom check
  (verify_completed_curve_phantom.py's arithmetic, inline): how much of the
  tape's apparent profit is unexecutable completed-curve pricing;
- policy backtest — the whale-ride held-out table
  (simulate_graduation_exit.py conventions, inline);
- forward shadow — the coupling activation report from the collector's
  JSONL (run_whaleride_shadow.py --report conventions, inline);
- learning journal — the lesson/paper-mark counters from the running
  bot's SQLite journal (read-only);
- provenance — sha256 of every input file and of the bundle itself.

The bundle states what it does NOT attest: fills, execution, finality,
wallet state, or future coupling. It is a diagnostic snapshot, not a
performance claim — the same boundary every honest tool in this repo
carries.

Usage:
    uv run --offline --no-sync python -B \\
        learning-examples/token-lifecycles/build_evidence_bundle.py \\
        [--tape learning-examples/token-lifecycles/lifecycles_24h.jsonl] \\
        [--shadow .state/whaleride-shadow/shadow.jsonl] \\
        [--lessons .state/learning/lessons.sqlite3] \\
        [--out .state/research/evidence-bundle.json]
"""

# ruff: noqa: C901, PLR0912, PLR0915 - one-command audit builders

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BUY_LAMPORTS = 10_000_000
PUMP_FEE = 0.0125
POOL_FEE = 0.003
TX_FEES_LAMPORTS = 65_000
COST = BUY_LAMPORTS + TX_FEES_LAMPORTS
GRADUATION_LEVEL_SOL = 85.0


def sha256_of(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tokens_bought(v_quote: int, v_token: int) -> float:
    spend_net = BUY_LAMPORTS * (1 - PUMP_FEE)
    return v_token - (v_quote * v_token) / (v_quote + spend_net)


def curve_sell(v_quote: int, v_token: int, tokens: float) -> float:
    return v_quote * tokens / (v_token + tokens) * (1 - PUMP_FEE)


def pool_sell(eff_quote: float, base: int, tokens: float) -> float:
    return eff_quote * tokens / (base + tokens) * (1 - POOL_FEE)


def audit_tape(tape: Path) -> dict:
    """Phantom-vs-honest on every graduated coin, plus the whale-ride table."""
    graduated = 0
    total = 0
    phantom_inflation = 0.0
    rows: list[dict] = []
    with tape.open() as f:
        coins = [json.loads(line) for line in f]
    total = len(coins)
    split_ts = statistics.median(c["create_ts"] for c in coins)
    for coin in coins:
        posts = coin.get("post_trades") or []
        if coin.get("graduated_dslot") is None or coin.get("pool_dslot") is None:
            continue
        if not posts:
            continue
        graduated += 1
        trades = coin["trades"]
        if not trades:
            continue
        last = trades[-1]
        v_tok, v_sol = last[7], last[6]
        if v_tok > 0 and v_sol > 0:
            tokens = tokens_bought(50_000_000_000, 700_000_000_000_000)
            phantom = curve_sell(v_sol, v_tok, tokens)
            p0 = posts[0]
            base, eff = p0[5], p0[6] + (p0[7] or 0)
            honest = pool_sell(eff, base, tokens) if base > 0 and eff > 0 else 0.0
            phantom_inflation += phantom - honest
        rows.append(coin)
    # whale-ride policy table at the honest exit
    table = {}
    for x in (30, 40, 50, 60):
        test: list[float] = []
        for coin in rows:
            trades = coin["trades"]
            posts = coin["post_trades"] or []
            idx = next((i for i, t in enumerate(trades) if t[5] >= x * 1e9), None)
            if idx is None or idx == len(trades) - 1:
                continue
            # entry lands one slot after the crossing; skip if graduation won
            land = idx
            for j in range(idx + 1, len(trades)):
                if trades[j][0] <= trades[idx][0] + 1:
                    land = j
                else:
                    break
            if land == len(trades) - 1:
                continue
            v_sol, v_tok = trades[land][6], trades[land][7]
            if v_tok <= 0 or v_sol <= 0:
                continue
            tokens = tokens_bought(v_sol, v_tok)
            p0 = posts[0]
            base, eff = p0[5], p0[6] + (p0[7] or 0)
            if base <= 0 or eff <= 0:
                pnl = -float(COST)
            else:
                pnl = pool_sell(eff, base, tokens) - COST
            if coin["create_ts"] > split_ts:
                test.append(pnl)
        if test:
            table[f"x{x}"] = {
                "test_n": len(test),
                "test_mean_pct": round(statistics.mean(test) / 1e5, 2),
                "win_rate": round(sum(1 for p in test if p > 0) / len(test), 3),
            }
    return {
        "coins": total,
        "graduated": graduated,
        "phantom_inflation_sol": round(phantom_inflation / 1e9, 4),
        "whaleride_held_out": table,
        "note": (
            "phantom_inflation_sol is what a completed-curve-exit backtest "
            "over-books at 0.01-SOL position scale; whale-ride_held_out "
            "prices every post-buyout exit against the pool"
        ),
    }


def audit_shadow(shadow: Path) -> dict:
    """Coupling per X from the collector's JSONL (report conventions)."""
    curves: dict[str, list] = defaultdict(list)
    pools: dict[str, list] = defaultdict(list)
    with shadow.open() as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("k") == "c":
                curves[row["m"]].append(row)
            elif row.get("k") == "p":
                pools[row["m"]].append(row)
    out: dict[str, dict] = {}
    for x in (20, 30, 40, 50, 60):
        grad_arm: list[float] = []
        cold_arm: list[float] = []
        grad_after = crossers = 0
        for mint, cs in curves.items():
            cs.sort(key=lambda r: r["ts"])
            hit = next((r for r in cs if r["rq"] >= x * 1e9), None)
            if hit is None or hit["cp"]:
                continue
            graduated_after = any(r["cp"] for r in cs)
            tokens = tokens_bought(hit["vq"], hit["vt"])
            if graduated_after:
                pool_row = next(
                    (p for p in pools[mint] if p["ts"] >= hit["ts"] + 1.0), None
                )
                if pool_row is None:
                    continue
                eff, base = pool_row["q"] + pool_row["vrq"], pool_row["b"]
                pnl = (
                    -float(COST)
                    if base <= 0 or eff <= 0
                    else pool_sell(eff, base, tokens) - COST
                )
                grad_arm.append(pnl)
                grad_after += 1
            else:
                exit_row = next((r for r in cs if r["ts"] >= hit["ts"] + 2.0), None)
                if exit_row is None:
                    continue
                cold_arm.append(
                    curve_sell(exit_row["vq"], exit_row["vt"], tokens) - COST
                )
            crossers += 1
        if not crossers or not grad_arm or not cold_arm:
            out[f"x{x}"] = {"status": "insufficient_data", "crossers": crossers}
            continue
        grad_mean = statistics.mean(grad_arm)
        cold_mean = statistics.mean(cold_arm)
        coupling = grad_after / crossers
        denom = grad_mean - cold_mean
        break_even = (-cold_mean / denom) if denom > 0 else 10.0
        out[f"x{x}"] = {
            "coupling": round(coupling, 4),
            "grad_n": grad_after,
            "crossers": crossers,
            "grad_mean_pct": round(grad_mean / 1e5, 2),
            "cold_mean_pct": round(cold_mean / 1e5, 2),
            "break_even_coupling": round(break_even, 4),
            "state": "ACTIVE" if coupling >= break_even else "dormant",
        }
    return out


def audit_lessons(db: Path) -> dict:
    """Read-only counters from the running bot's journal."""
    if not db.exists():
        return {"status": "no_journal"}

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        total = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE utc >= ?",
            (time.strftime("%Y-%m-%dT%H", time.gmtime(time.time() - 3600)) + ":00",),
        ).fetchone()[0]
        marks = conn.execute("SELECT COUNT(*) FROM paper_marks").fetchone()[0]
        # Avg realized hold, from resolved marks only (exit_price present).
        # Hold policy is condition-driven; this reports what exits actually
        # took, it is not a hold target. Censored rows have no elapsed time.
        holds = {
            reason: {"avg_s": avg_s, "n": n}
            for reason, avg_s, n in conn.execute(
                "SELECT reason, ROUND(AVG(elapsed_s), 1), COUNT(*)"
                " FROM paper_marks WHERE exit_price IS NOT NULL"
                " GROUP BY reason ORDER BY 3 DESC"
            ).fetchall()
        }
        avg_hold_s = conn.execute(
            "SELECT ROUND(AVG(elapsed_s), 1), COUNT(*) FROM paper_marks"
            " WHERE exit_price IS NOT NULL"
        ).fetchone()
        decisions = dict(
            conn.execute(
                "SELECT decision, COUNT(*) FROM lessons"
                " WHERE utc >= datetime('now', '-24 hours') GROUP BY decision"
            ).fetchall()
        )
        # G-anchored decay: pool price at graduation vs G+5/30/120s samples,
        # as mean ratios over rows where both columns resolve. The table is
        # absent in journals not yet opened by the new schema.
        grad = {
            "n": 0,
            "p5_over_open": None,
            "p30_over_open": None,
            "p120_over_open": None,
        }
        grad_n = 0
        try:
            grad_row = conn.execute(
                "SELECT COUNT(*), AVG(open_price), AVG(p5), AVG(p30), AVG(p120)"
                " FROM grad_marks WHERE open_price IS NOT NULL"
            ).fetchone()
            grad_n = conn.execute("SELECT COUNT(*) FROM grad_marks").fetchone()[0]
        except sqlite3.OperationalError:
            grad_row = None
    finally:
        conn.close()
    return {
        "lessons_total": total,
        "lessons_last_hour": recent,
        "paper_marks_total": marks,
        "decisions_last_24h": decisions,
        "avg_hold_s": avg_hold_s[0],
        "resolved_marks": avg_hold_s[1],
        "avg_hold_by_reason": holds,
        "grad_marks_total": grad_n,
        "grad_decay": grad,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tape", default="learning-examples/token-lifecycles/lifecycles_24h.jsonl"
    )
    ap.add_argument("--shadow", default=".state/whaleride-shadow/shadow.jsonl")
    ap.add_argument("--lessons", default=".state/learning/lessons.sqlite3")
    ap.add_argument("--out", default=".state/research/evidence-bundle.json")
    args = ap.parse_args()

    tape_path, shadow_path = ROOT / args.tape, ROOT / args.shadow
    lessons_path, out_path = ROOT / args.lessons, ROOT / args.out

    bundle = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "not_attested": [
            "fills or execution",
            "chain finality or bank freshness",
            "wallet balances or authorization",
            "future coupling or regime persistence",
        ],
        "tape_audit": audit_tape(tape_path),
        "shadow_coupling": audit_shadow(shadow_path),
        "learning_journal": audit_lessons(lessons_path),
        "provenance": {
            "tape_sha256": sha256_of(tape_path),
            "shadow_sha256": sha256_of(shadow_path),
            "lessons_sha256": sha256_of(lessons_path),
            "code": {
                name: sha256_of(ROOT / "learning-examples" / "token-lifecycles" / name)
                for name in (
                    "build_evidence_bundle.py",
                    "simulate_graduation_exit.py",
                    "verify_completed_curve_phantom.py",
                    "run_whaleride_shadow.py",
                )
            },
        },
    }
    payload = json.dumps(bundle, indent=1, default=str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n")
    bundle["bundle_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    out_path.write_text(json.dumps(bundle, indent=1, default=str) + "\n")

    # console summary
    print(f"evidence bundle -> {out_path}")
    print(f"bundle_sha256 {bundle['bundle_sha256'][:16]}…")
    tape = bundle["tape_audit"]
    print(
        f"tape: {tape['coins']} coins, {tape['graduated']} graduated, "
        f"phantom inflation {tape['phantom_inflation_sol']} SOL"
    )
    for key, row in bundle["shadow_coupling"].items():
        if row.get("state"):
            print(
                f"{key}: coupling {row['coupling']:.1%} vs break-even "
                f"{row['break_even_coupling']:.1%} -> {row['state']}"
            )
    journal = bundle["learning_journal"]
    if journal.get("status") != "no_journal":
        print(
            f"journal: {journal['lessons_total']} lessons, "
            f"{journal['paper_marks_total']} paper marks"
        )
        if journal.get("resolved_marks"):
            print(
                f"avg realized hold: {journal['avg_hold_s']}s over "
                f"{journal['resolved_marks']} resolved marks "
                f"(condition-driven; by reason: {journal['avg_hold_by_reason']})"
            )
        gdec = journal.get("grad_decay") or {}
        if gdec.get("n"):
            print(
                f"grad-anchored decay (n={gdec['n']}): "
                f"G+5s {gdec['p5_over_open']}, G+30s {gdec['p30_over_open']}, "
                f"G+120s {gdec['p120_over_open']} (pool price / open price)"
            )


if __name__ == "__main__":
    main()
