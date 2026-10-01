"""Per-transaction lesson journal with optional Jev scoring.

Two pieces:

``LessonJournal``
    Append-only SQLite store. One row per observed decision (gate skip,
    paper fill, live trade, exit). `application lessons'' are derived by
    simple SQL at read time; nothing is inferred at write time.

``JevScorer``
    Optional TypeSafe/Jev client. Scores a candidate from the same state
    the entry gate sees (name/symbol/buyers/reserves/flow). Reads
    ``TYPESAFE_API_KEY`` from the environment or an explicit service-only
    env file (never the root .env). Absent key -> scorer disabled; API
    failure -> score is ``None`` and the caller proceeds. Never blocks a
    trade on the model; never signs or submits anything.

Learning discipline (from learning-examples/token-lifecycles): a judgment
is a feature, not an edge. Scores are journaled alongside outcomes so
their predictive value can be measured offline before any live use.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient

from utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_DB = Path(".state/learning/lessons.sqlite3")
_MIN_BRIER_N = 10  # Brier needs at least this many resolved outcomes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    utc TEXT NOT NULL,
    kind TEXT NOT NULL,               -- gate_skip | paper_fill | buy | exit
    mint TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    platform TEXT,
    mayhem INTEGER,
    decision TEXT,                    -- accept/skip/timeout/not_mayhem/...
    buyers INTEGER,
    real_sol REAL,
    jev_quality REAL,                 -- 0-4, NULL when Jev unavailable
    jev_copycat REAL,                 -- P(copycat), NULL when unavailable
    jev_dump_risk REAL,               -- P(early dump within 60s)
    jev_organic REAL,                 -- P(buying is organic, not one wallet)
    jev_liq_trap REAL,                -- P(thin-curve exit trap)
    jev_model TEXT,
    outcome_utc TEXT,                 -- filled when the position closes
    outcome_pnl_sol REAL,             -- net quote at close, NULL until known
    outcome_reason TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_kind ON lessons(kind);
CREATE INDEX IF NOT EXISTS idx_lessons_mint ON lessons(mint);
CREATE INDEX IF NOT EXISTS idx_lessons_pending ON lessons(outcome_utc) WHERE outcome_utc IS NULL;
"""


@dataclass(slots=True)
class LessonObservation:
    """One decision point worth remembering."""

    kind: str  # gate_skip | paper_fill | buy | exit
    mint: str
    symbol: str | None = None
    name: str | None = None
    platform: str | None = None
    mayhem: bool | None = None
    decision: str | None = None
    buyers: int | None = None
    real_sol: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GateSnapshot:
    """Gate state at scoring time."""

    buyers: int = 0
    real_sol: float | None = None
    buys_recent: int = 0
    sells_recent: int = 0


class LessonJournal:
    """Append-only SQLite journal. Safe to call from async code (sync writes
    are short); one writer at a time by convention (single bot process)."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        path = Path(db_path) if db_path is not None else _DEFAULT_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        # WAL lets the dashboard and report CLI read while the bot writes.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # Idempotent migration: existing journals lack the battery columns.
        existing = {
            r[1] for r in self._conn.execute("PRAGMA table_info(lessons)")
        }
        for col in ("jev_dump_risk", "jev_organic", "jev_liq_trap"):
            if col not in existing:
                self._conn.execute(f"ALTER TABLE lessons ADD COLUMN {col} REAL")
        self._conn.commit()

    def record(
        self,
        obs: LessonObservation,
        jev: dict[str, Any] | None = None,
    ) -> None:
        """Append one observation. Jev answers, when present, are columns."""
        try:
            self._conn.execute(
                "INSERT INTO lessons (utc, kind, mint, symbol, name, platform,"
                " mayhem, decision, buyers, real_sol, jev_quality, jev_copycat,"
                " jev_dump_risk, jev_organic, jev_liq_trap,"
                " jev_model, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _utc(),
                    obs.kind,
                    obs.mint,
                    obs.symbol,
                    obs.name,
                    obs.platform,
                    _to_int(v=obs.mayhem),
                    obs.decision,
                    obs.buyers,
                    obs.real_sol,
                    _score_of(jev, "quality"),
                    _prob_of(jev, "is_copycat"),
                    _prob_of(jev, "early_dump_risk"),
                    _prob_of(jev, "momentum_organic"),
                    _prob_of(jev, "liquidity_trap"),
                    (jev or {}).get("model"),
                    json.dumps(obs.raw, default=str)[:8000],
                ),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.exception("Lesson journal write failed (non-fatal)")

    def link_outcome(
        self,
        mint: str,
        pnl_sol: float | None,
        reason: str | None = None,
    ) -> None:
        """Attach an outcome to the most recent open lesson for this mint."""
        try:
            row = self._conn.execute(
                "SELECT id FROM lessons WHERE mint=? AND kind IN"
                " ('gate_pass','paper_fill','buy') AND outcome_utc IS NULL"
                " ORDER BY id DESC LIMIT 1",
                (mint,),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                "UPDATE lessons SET outcome_utc=?, outcome_pnl_sol=?,"
                " outcome_reason=? WHERE id=?",
                (_utc(), pnl_sol, reason, row[0]),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.exception("Lesson outcome link failed (non-fatal)")

    def stats(self) -> dict[str, Any]:
        """Summary for the dashboard: counts and Jev-vs-outcome agreement."""
        cur = self._conn
        out: dict[str, Any] = {}
        out["total"] = cur.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        out["by_kind"] = dict(
            cur.execute("SELECT kind, COUNT(*) FROM lessons GROUP BY kind").fetchall()
        )
        resolved = cur.execute(
            "SELECT COUNT(*) FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
        ).fetchone()[0]
        out["resolved_outcomes"] = resolved
        if resolved:
            # Does a higher Jev quality score predict better PnL? Buckets on
            # the observed 0-1 scale (ROUND to 0.1), not the old INT cast.
            out["pnl_by_quality"] = [
                list(r)
                for r in cur.execute(
                    "SELECT ROUND(jev_quality, 1),"
                    " ROUND(AVG(outcome_pnl_sol),8), COUNT(*)"
                    " FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
                    " AND jev_quality IS NOT NULL GROUP BY 1 ORDER BY 1"
                ).fetchall()
            ]
            out["brier_win"] = self._brier(cur)
            out["win_rate"] = cur.execute(
                "SELECT AVG(CASE WHEN outcome_pnl_sol > 0 THEN 1.0 ELSE 0.0 END)"
                " FROM lessons WHERE outcome_pnl_sol IS NOT NULL"
            ).fetchone()[0]
        return out

    @staticmethod
    def _brier(cur: sqlite3.Cursor) -> float | None:
        """Brier score for 'win' as the outcome and jev_quality as the
        forecast, capped to [0,1]. 0 = perfect calibration. Mirrors the
        buberlo/jev-trader calibration loop (state, decision, outcome)."""
        rows = cur.execute(
            "SELECT jev_quality, outcome_pnl_sol FROM lessons"
            " WHERE outcome_pnl_sol IS NOT NULL AND jev_quality IS NOT NULL"
        ).fetchall()
        if len(rows) < _MIN_BRIER_N:
            return None
        b = sum(
            (max(0.0, min(1.0, q)) - (1.0 if p > 0 else 0.0)) ** 2
            for q, p in rows
        )
        return round(b / len(rows), 6)

    def close(self) -> None:
        self._conn.close()


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _to_int(*, v: bool | None) -> int | None:
    return None if v is None else int(v)


def _score_of(jev: dict[str, Any] | None, key: str) -> float | None:
    """Accept either the flat {'quality': 2.1} dict score_candidate returns
    or a raw API answer object {'quality': {'score': 2.1}}."""
    if not jev:
        return None
    value = jev.get(key)
    if isinstance(value, dict):
        value = value.get("score")
    return float(value) if isinstance(value, int | float) else None


def _prob_of(jev: dict[str, Any] | None, key: str) -> float | None:
    if not jev:
        return None
    value = jev.get(key)
    if isinstance(value, dict):
        value = value.get("bool") or value.get("noul")
    return float(value) if isinstance(value, int | float) else None


class JevScorer:
    """Optional Jev (TypeSafe) scorer. Disabled without TYPESAFE_API_KEY.

    Key lookup order: environment variable, then an explicit service-only
    env file (key=value lines). The root .env is never read.
    """

    def __init__(self, env_file: Path | str | None = None) -> None:
        self._api_key = os.environ.get("TYPESAFE_API_KEY")
        if not self._api_key and env_file is not None:
            path = Path(env_file)
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.startswith("TYPESAFE_API_KEY="):
                        self._api_key = line.split("=", 1)[1].strip()
                        break
        self._client = None
        if self._api_key:
            try:
                self._client = AsyncTypeSafeClient(api_key=self._api_key)
            except Exception:
                logger.exception("TypeSafe SDK unavailable; Jev scorer disabled")
                self._client = None
        if self._client is None:
            logger.info("Jev scorer disabled (no TYPESAFE_API_KEY)")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def score_candidate(
        self,
        *,
        name: str | None,
        symbol: str | None,
        mayhem: bool,
        gate: GateSnapshot,
    ) -> dict[str, Any] | None:
        """Score one candidate. Returns {'quality':…, 'is_copycat':…,
        'model':…} or None. Failures are logged, never raised."""
        if self._client is None:
            return None
        state = {
            "token": {"symbol": symbol, "name": name, "mayhem": mayhem},
            "gate": {
                "buyers": gate.buyers,
                "real_sol": gate.real_sol,
                "buys_recent": gate.buys_recent,
                "sells_recent": gate.sells_recent,
            },
        }
        try:
            response = await self._client.system_one(
                state=state,
                questions={
                    "quality": {
                        "type": "score",
                        "instructions": (
                            "Rate this pump.fun snipe candidate on launch "
                            "quality. state.token has name/symbol/mayhem; "
                            "state.gate has buyers (distinct non-creator "
                            "buyers so far), real_sol (SOL on the curve), "
                            "buys_recent/sells_recent (trades in the last "
                            "seconds)."
                        ),
                        "criteria": [
                            "dead or hostile launch, must avoid",
                            "weak, no momentum",
                            "neutral, nothing special",
                            "promising momentum, credible name/community",
                            "exceptional setup, rare",
                        ],
                    },
                    "is_copycat": {
                        "type": "noul",
                        "instructions": (
                            "Does state.token.name/symbol look like a "
                            "copycat of a famous existing project with no "
                            "added substance? (deploy-and-dump pattern)"
                        ),
                    },
                    "early_dump_risk": {
                        "type": "noul",
                        "instructions": (
                            "Given state.token and state.gate, how likely "
                            "is it that the curve reserves drop by half "
                            "or more within the next minute (early dump "
                            "by the deployer or a coordinated holder)?"
                        ),
                    },
                    "momentum_organic": {
                        "type": "noul",
                        "instructions": (
                            "Is the buying activity described by state.gate "
                            "organic crowd interest rather than one wallet "
                            "or the deployer pushing volume to attract "
                            "snipers? buys_recent/sells_recent and buyers "
                            "are the evidence."
                        ),
                    },
                    "liquidity_trap": {
                        "type": "noul",
                        "instructions": (
                            "Given real_sol on the curve, would an exit "
                            "of a typical snipe-size position move the "
                            "price so much that the trade cannot close "
                            "profitably? (thin-curve trap)"
                        ),
                    },
                },
            )
        except Exception:
            logger.exception("Jev scoring failed (non-fatal, proceeding)")
            return None
        out: dict[str, Any] = {}
        try:
            out["quality"] = response.scores["quality"].score
            out["is_copycat"] = response.nouls["is_copycat"].noul
            out["early_dump_risk"] = response.nouls["early_dump_risk"].noul
            out["momentum_organic"] = response.nouls["momentum_organic"].noul
            out["liquidity_trap"] = response.nouls["liquidity_trap"].noul
            out["model"] = response.model
        except (KeyError, AttributeError):
            logger.exception("Jev response shape unexpected")
            return None
        return out

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except (OSError, RuntimeError) as exc:
                logger.debug("Jev client close failed: %s", exc)
