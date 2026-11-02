"""Freeze planning-selected accepts, then analyze only the checked JSON artifact.

freeze --db PATH --out NEW.json
analyze COHORT.json

Retrospective descriptive analysis only. Gross marginal marks are not fills or
net ROI; neither the checksum nor a fixed historical split authenticates data
or establishes a strategy edge. No network, funds, or strategy changes.
"""
# ruff: noqa: ANN401, PLR2004, TRY003 - untrusted JSON is validated field by field

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRAIN_N = 24
HORIZONS = (60, 300, 900)
FEATURES = ("buyers", "real_sol", "hour")
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 10_000
MAX_TEXT_CHARS = 4096
MAX_OBSERVATION_LATENESS_S = 5
EXECUTING_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
POLICY = {
    "selection": "kind=gate_pass AND decision=buyers_present AND any planned paper mark",
    "order": ["utc", "id"],
    "train_n": TRAIN_N,
    "features": list(FEATURES),
    "horizons_s": list(HORIZONS),
    "unit": "entry_id; repeated mints retained, not independent token samples",
    "split": "first 24 entries train; remainder fixed historical holdout; no outcome filtering",
    "missingness": "retain missing features and missing_plan/pending/censored horizons; paired available observations only",
    "interpretation": "retrospective descriptive comparisons; no pre-registration, confirmation or promotion claim",
    "marks": "gross marginal SOL/token returns, not executable fills or net ROI",
    "checksum": "SHA-256 of canonical UTF-8 JSON excluding content_sha256; byte integrity, not chain authenticity",
}
OBSERVATION_KEYS = {
    "planned",
    "scheduled_utc",
    "entry_price",
    "exit_price",
    "elapsed_s",
    "outcome_utc",
    "reason",
    "status",
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def bounded_json(value: Any) -> bytes:
    """Refuse oversized output before creating a destination or printing partial JSON."""
    encoder = json.JSONEncoder(
        indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    )
    with io.BytesIO() as buffer:
        for chunk in itertools.chain(encoder.iterencode(value), ("\n",)):
            encoded = chunk.encode("utf-8")
            require(
                buffer.tell() + len(encoded) <= MAX_ARTIFACT_BYTES,
                "JSON output exceeds byte limit",
            )
            buffer.write(encoded)
        return buffer.getvalue()


def code_identity() -> dict:
    return {"file": "analyze_accept_clues.py", "sha256": EXECUTING_SOURCE_SHA256}


def content_digest(artifact: dict) -> str:
    return hashlib.sha256(
        canonical({k: v for k, v in artifact.items() if k != "content_sha256"})
    ).hexdigest()


def require(condition: bool, message: str) -> None:  # noqa: FBT001 - assert-style guard
    if not condition:
        raise ValueError(message)


def keys(value: Any, expected: set[str]) -> None:
    require(type(value) is dict and set(value) == expected, "unexpected object schema")


def integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def number(value: Any, *, positive: bool = False) -> None:
    require(type(value) in (int, float), "quantity must be numeric, not boolean")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    require(
        finite and (value > 0 if positive else value >= 0), "invalid finite quantity"
    )


def text(value: Any, *, nullable: bool = False) -> None:
    require(
        (nullable and value is None)
        or (type(value) is str and len(value) <= MAX_TEXT_CHARS),
        "invalid or oversized text field",
    )


def instant(value: Any, *, nullable: bool = False) -> datetime | None:
    if nullable and value is None:
        return None
    text(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(
            parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
            "timestamp must have UTC offset",
        )
    except (ValueError, OverflowError) as error:
        raise ValueError("invalid UTC timestamp") from error
    return parsed


def hex_digest(value: Any) -> None:
    require(
        type(value) is str
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value),
        "invalid SHA-256",
    )


def observation_status(mark: dict) -> str:
    if not mark["planned"]:
        return "missing_plan"
    if mark["entry_price"] is not None and mark["exit_price"] is not None:
        return "observed"
    return "pending" if mark["outcome_utc"] is None else "censored"


def raw_return(mark: dict) -> float | None:
    if mark["status"] != "observed":
        return None
    result = mark["exit_price"] / mark["entry_price"] - 1
    require(math.isfinite(result), "nonfinite derived return")
    return result


def validate_observation(mark: Any, horizon: int, decision_utc: datetime) -> None:
    keys(mark, OBSERVATION_KEYS)
    require(type(mark["planned"]) is bool, "invalid planning flag")
    scheduled = instant(mark["scheduled_utc"], nullable=True)
    outcome = instant(mark["outcome_utc"], nullable=True)
    text(mark["reason"], nullable=True)
    for field in ("entry_price", "exit_price", "elapsed_s"):
        if mark[field] is not None:
            number(mark[field], positive=field != "elapsed_s")
    require(
        mark["status"] == observation_status(mark), "inconsistent observation status"
    )
    if not mark["planned"]:
        require(
            all(
                mark[field] is None
                for field in OBSERVATION_KEYS - {"planned", "status"}
            ),
            "unplanned horizon has observations",
        )
        return
    require(
        scheduled is not None and scheduled >= decision_utc,
        "planned horizon lacks schedule or precedes decision",
    )
    if mark["status"] == "pending":
        require(
            all(
                mark[field] is None
                for field in ("exit_price", "elapsed_s", "outcome_utc", "reason")
            ),
            "pending horizon has completion fields",
        )
        return
    require(
        outcome is not None
        and outcome >= scheduled
        and mark["elapsed_s"] is not None
        and bool(mark["reason"]),
        "completed horizon lacks metadata or precedes schedule",
    )
    if mark["status"] == "observed":
        require(
            horizon <= mark["elapsed_s"] <= horizon + MAX_OBSERVATION_LATENESS_S,
            "observed horizon missed producer timing window",
        )
    else:
        require(
            mark["exit_price"] is None,
            "censored horizon has an exit without an entry baseline",
        )
    raw_return(mark)


def validate_identity(row: dict) -> None:
    require(integer(row["id"], 1), "invalid entry identity")
    instant(row["utc"])
    text(row["mint"])
    require(bool(row["mint"]), "empty mint identity")


def validate_entry(row: Any) -> None:
    keys(row, {"id", "utc", "mint", "symbol", "features", "observations"})
    validate_identity(row)
    text(row["symbol"], nullable=True)
    features = row["features"]
    keys(features, {"buyers", "real_sol", "mayhem", "hour"})
    require(features["buyers"] is None or integer(features["buyers"]), "invalid buyers")
    if features["real_sol"] is not None:
        number(features["real_sol"])
    require(
        features["mayhem"] is None or type(features["mayhem"]) is bool, "invalid mayhem"
    )
    require(
        integer(features["hour"]) and features["hour"] == instant(row["utc"]).hour,
        "hour does not match decision timestamp",
    )
    keys(row["observations"], {str(h) for h in HORIZONS})
    for horizon in HORIZONS:
        validate_observation(
            row["observations"][str(horizon)], horizon, instant(row["utc"])
        )
    require(
        any(mark["planned"] for mark in row["observations"].values()),
        "cohort entry has no planned horizons",
    )


def validate_membership(artifact: dict) -> None:
    entries, outside = artifact["entries"], artifact["outside_study"]
    require(type(entries) is list and type(outside) is list, "membership must be lists")
    require(len(entries) + len(outside) <= MAX_MEMBERS, "membership exceeds limit")
    for entry in entries:
        validate_entry(entry)
    for row in outside:
        keys(row, {"id", "utc", "mint", "reason"})
        validate_identity(row)
        require(row["reason"] == "no_planned_marks", "invalid exclusion reason")
    combined = entries + outside
    require(
        len({row["id"] for row in combined}) == len(combined),
        "duplicate entry identity",
    )
    for rows in (entries, outside):
        require(
            rows == sorted(rows, key=lambda row: (row["utc"], row["id"])),
            "membership is not in frozen utc/id order",
        )
    watermark = artifact["source_watermark"]
    require(
        len(combined) <= watermark["lessons_count"], "membership exceeds source count"
    )
    require(
        sum(mark["planned"] for row in entries for mark in row["observations"].values())
        <= watermark["paper_marks_count"],
        "plans exceed source count",
    )
    for row in combined:
        require(
            watermark["lessons_max_id"] is not None
            and row["id"] <= watermark["lessons_max_id"]
            and watermark["lessons_max_utc"] is not None
            and row["utc"] <= watermark["lessons_max_utc"],
            "membership exceeds watermark",
        )


def validate_artifact(artifact: Any) -> dict:
    keys(
        artifact,
        {
            "schema_version",
            "generated_utc",
            "code",
            "policy",
            "source_watermark",
            "outside_study",
            "entries",
            "content_sha256",
        },
    )
    require(
        type(artifact["schema_version"]) is int and artifact["schema_version"] == 1,
        "unsupported schema version",
    )
    instant(artifact["generated_utc"])
    keys(artifact["code"], {"file", "sha256"})
    require(
        artifact["code"]["file"] == "analyze_accept_clues.py", "invalid code identity"
    )
    hex_digest(artifact["code"]["sha256"])
    require(
        canonical(artifact["policy"]) == canonical(POLICY),
        "unsupported analysis policy",
    )
    watermark = artifact["source_watermark"]
    keys(
        watermark,
        {"lessons_count", "lessons_max_id", "lessons_max_utc", "paper_marks_count"},
    )
    require(
        integer(watermark["lessons_count"]) and integer(watermark["paper_marks_count"]),
        "invalid source counts",
    )
    require(
        watermark["lessons_max_id"] is None or integer(watermark["lessons_max_id"], 1),
        "invalid source identity watermark",
    )
    instant(watermark["lessons_max_utc"], nullable=True)
    empty = watermark["lessons_count"] == 0
    require(
        (watermark["lessons_max_id"] is None) == empty
        and (watermark["lessons_max_utc"] is None) == empty,
        "inconsistent empty source watermark",
    )
    hex_digest(artifact["content_sha256"])
    validate_membership(artifact)
    require(
        artifact["content_sha256"] == content_digest(artifact),
        "artifact checksum mismatch",
    )
    return artifact


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    require(len({key for key, _ in pairs}) == len(pairs), "duplicate JSON object key")
    return dict(pairs)


def reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def load_artifact(path: Path) -> dict:
    with path.open("rb") as stream:
        encoded = stream.read(MAX_ARTIFACT_BYTES + 1)
    require(len(encoded) <= MAX_ARTIFACT_BYTES, "artifact exceeds byte limit")
    artifact = json.loads(
        encoded.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    return validate_artifact(artifact)


def stored_observation(row: dict | None) -> dict:
    fields = OBSERVATION_KEYS - {"planned", "status"}
    mark = {
        "planned": row is not None,
        **{field: row[field] if row is not None else None for field in fields},
    }
    return {**mark, "status": observation_status(mark)}


def freeze_entry(rows: list[dict]) -> dict:
    row = rows[0]
    require(
        row["mayhem"] is None
        or (type(row["mayhem"]) is int and row["mayhem"] in (0, 1)),
        "invalid stored mayhem",
    )
    plans = {mark["horizon_s"]: mark for mark in rows if mark["horizon_s"] is not None}
    require(
        len(plans) == len([mark for mark in rows if mark["horizon_s"] is not None]),
        "duplicate planned horizon",
    )
    require(set(plans) <= set(HORIZONS), "unsupported planned horizon")
    return {
        **{field: row[field] for field in ("id", "utc", "mint", "symbol")},
        "features": {
            "buyers": row["buyers"],
            "real_sol": row["real_sol"],
            "mayhem": None if row["mayhem"] is None else bool(row["mayhem"]),
            "hour": instant(row["utc"]).hour,
        },
        "observations": {str(h): stored_observation(plans.get(h)) for h in HORIZONS},
    }


def freeze(database: Path, output: Path) -> dict:
    """Read one transaction (including committed WAL), then exclusively publish JSON."""
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        watermark = dict(
            db.execute(
                "SELECT count(*) AS lessons_count,max(id) AS lessons_max_id,"
                "max(utc) AS lessons_max_utc,(SELECT count(*) FROM paper_marks) AS paper_marks_count "
                "FROM lessons"
            ).fetchone()
        )
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT l.id,l.utc,l.mint,l.symbol,l.buyers,l.real_sol,l.mayhem,"
                "p.horizon_s,p.scheduled_utc,p.entry_price,p.exit_price,p.elapsed_s,p.outcome_utc,p.reason "
                "FROM lessons l LEFT JOIN paper_marks p ON p.entry_id=l.id "
                "WHERE l.kind='gate_pass' AND l.decision='buyers_present' "
                "ORDER BY l.utc,l.id,p.horizon_s LIMIT ?",
                (MAX_MEMBERS * len(HORIZONS) + 1,),
            )
        ]
        require(
            len(rows) <= MAX_MEMBERS * len(HORIZONS), "source membership exceeds limit"
        )
        groups = [
            list(group)
            for _, group in itertools.groupby(rows, key=lambda row: row["id"])
        ]
        require(len(groups) <= MAX_MEMBERS, "source membership exceeds limit")
        entries = [
            freeze_entry(group) for group in groups if group[0]["horizon_s"] is not None
        ]
        outside = [
            {
                **{field: group[0][field] for field in ("id", "utc", "mint")},
                "reason": "no_planned_marks",
            }
            for group in groups
            if group[0]["horizon_s"] is None
        ]
    payload = {
        "schema_version": 1,
        "generated_utc": datetime.now(UTC).isoformat(),
        "code": code_identity(),
        "policy": POLICY,
        "source_watermark": watermark,
        "outside_study": outside,
        "entries": entries,
    }
    validate_membership(payload)
    bounded_json(payload)
    artifact = validate_artifact({**payload, "content_sha256": content_digest(payload)})
    encoded = bounded_json(artifact)
    with output.open("xb") as stream:
        stream.write(encoded)
    return artifact


def ranks(values: list[float]) -> list[float]:
    """Average ranks for ties, preserving each entry as the observation unit."""
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranked = {}
    for _, group in itertools.groupby(enumerate(ordered), key=lambda pair: pair[1][1]):
        ties = list(group)
        average = (ties[0][0] + ties[-1][0]) / 2 + 1
        ranked.update({original: average for _, (original, _) in ties})
    return [ranked[index] for index in range(len(values))]


def comparison(entries: list[dict], feature: str, horizon: int) -> dict:
    pairs = [
        (row["features"][feature], raw_return(row["observations"][str(horizon)]))
        for row in entries
    ]
    available = [(x, y) for x, y in pairs if x is not None and y is not None]
    n = len(available)
    if n < 3:
        return {"n": n, "rho": None, "reason": "insufficient_pairs_min_3"}
    rx, ry = (ranks(list(values)) for values in zip(*available, strict=False))
    mx, my = sum(rx) / n, sum(ry) / n
    vx = sum((value - mx) ** 2 for value in rx)
    vy = sum((value - my) ** 2 for value in ry)
    if vx == 0 or vy == 0:
        return {"n": n, "rho": None, "reason": "constant_feature_or_outcome"}
    rho = sum((x - mx) * (y - my) for x, y in zip(rx, ry, strict=False)) / math.sqrt(
        vx * vy
    )
    return {"n": n, "rho": max(-1.0, min(1.0, rho)), "reason": None}


def summary(values: list[float | None]) -> dict:
    available = sorted(value for value in values if value is not None)
    n = len(available)
    median = None if not n else (available[(n - 1) // 2] / 2 + available[n // 2] / 2)
    return {"n": n, "missing_n": len(values) - n, "median": median}


def describe(entries: list[dict]) -> dict:
    return {
        str(h): summary([raw_return(row["observations"][str(h)]) for row in entries])
        for h in HORIZONS
    }


def analyze(artifact: dict) -> dict:
    validate_artifact(artifact)
    entries = artifact["entries"]
    splits = {"train": entries[:TRAIN_N], "holdout": entries[TRAIN_N:]}
    comparisons = [
        {
            "feature": feature,
            "horizon_s": horizon,
            **{
                name: comparison(rows, feature, horizon)
                for name, rows in splits.items()
            },
        }
        for feature in FEATURES
        for horizon in HORIZONS
    ]
    executing = code_identity()
    return {
        "content_sha256": artifact["content_sha256"],
        "code_identity": {
            "producing": artifact["code"],
            "executing": executing,
            "same_sha256": artifact["code"]["sha256"] == executing["sha256"],
            "scope": "local source bytes captured at import; no proof of origin or native execution",
        },
        "interpretation": POLICY["interpretation"],
        "marks": POLICY["marks"],
        "unit": POLICY["unit"],
        "cohort_n": len(entries),
        "unique_mints_n": len({row["mint"] for row in entries}),
        "outside_study": artifact["outside_study"],
        "splits": {
            name: {
                "n": len(rows),
                "entry_ids": [row["id"] for row in rows],
                "window_utc": [rows[0]["utc"], rows[-1]["utc"]] if rows else None,
                "outcomes": describe(rows),
            }
            for name, rows in splits.items()
        },
        "missing_features": {
            key: sum(row["features"][key] is None for row in entries)
            for key in (*FEATURES, "mayhem")
        },
        "horizon_status_counts": {
            str(h): {
                status: sum(
                    row["observations"][str(h)]["status"] == status for row in entries
                )
                for status in ("missing_plan", "pending", "censored", "observed")
            }
            for h in HORIZONS
        },
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
        "pooled_descriptive_outcomes": describe(entries),
        "pooled_fund_bands": [
            {
                "lo_sol": lo,
                "hi_sol_exclusive": hi,
                "n": len(band),
                "outcomes": describe(band),
            }
            for lo, hi in ((0, 62), (62, 75), (75, 1000))
            for band in [
                [
                    row
                    for row in entries
                    if row["features"]["real_sol"] is not None
                    and lo <= row["features"]["real_sol"] < hi
                ]
            ]
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freezing = commands.add_parser(
        "freeze", help="capture planned membership and observations once"
    )
    freezing.add_argument("--db", type=Path, required=True)
    freezing.add_argument("--out", type=Path, required=True)
    analyzing = commands.add_parser(
        "analyze", help="analyze only a checked self-contained artifact"
    )
    analyzing.add_argument("cohort", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            artifact = freeze(args.db, args.out)
            result = {
                "content_sha256": artifact["content_sha256"],
                "cohort_n": len(artifact["entries"]),
                "outside_study_n": len(artifact["outside_study"]),
            }
        else:
            result = analyze(load_artifact(args.cohort))
        print(bounded_json(result).decode("utf-8"), end="")
    except (OSError, ValueError, sqlite3.Error, OverflowError, RecursionError) as error:
        parser.exit(2, f"accept-clues: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
