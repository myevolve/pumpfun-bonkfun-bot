"""Offline consumer check; run: python -B learning-examples/token-lifecycles/verify_accept_clues.py.

Only synthetic temporary SQLite/JSON files are used; no subprocess, RPC or secrets.
"""
# ruff: noqa: S101, PLR2004, C901, PLR0912, PLR0915, S608 - literal synthetic SQL columns

from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import analyze_accept_clues as clues


def invoke(*args: str, succeeds: bool = True) -> str:
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            clues.main(list(args))
        except SystemExit as error:
            assert not succeeds and error.code != 0, errors.getvalue()
        else:
            assert succeeds, "invalid input unexpectedly accepted"
    return output.getvalue()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze(path: Path) -> dict:
    with patch.object(
        clues.sqlite3, "connect", side_effect=AssertionError("DB access")
    ):
        return json.loads(invoke("analyze", str(path)))


def timestamp(entry: int, elapsed: float = 0) -> str:
    return (
        datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=entry + elapsed)
    ).isoformat()


def checked_copy(artifact: dict, path: Path) -> None:
    payload = {key: value for key, value in artifact.items() if key != "content_sha256"}
    path.write_text(
        json.dumps({**payload, "content_sha256": clues.content_digest(payload)})
    )


def verify_observations(artifact: dict, root: Path) -> None:
    invalid = root / "invalid-observation.json"
    for horizon in clues.HORIZONS:
        cases = [
            (0, {"outcome_utc": None}),
            (0, {"elapsed_s": None}),
            (0, {"reason": None}),
            (0, {"reason": ""}),
            (0, {"elapsed_s": horizon - 1}),
            (0, {"elapsed_s": horizon + 6}),
            (0, {"entry_price": None}),
            (0, {"entry_price": 0}),
            (0, {"exit_price": 0}),
            (0, {"scheduled_utc": timestamp(0)}),
            (0, {"outcome_utc": timestamp(0)}),
            (4, {"exit_price": 2}),
            (4, {"elapsed_s": 0}),
            (4, {"reason": "incomplete"}),
            (4, {"outcome_utc": timestamp(5, horizon)}),
        ]
        if horizon == 300:
            cases += [
                (2, {"elapsed_s": None}),
                (2, {"reason": None}),
                (2, {"outcome_utc": None}),
                (2, {"outcome_utc": timestamp(0)}),
            ]
        if horizon == 900:
            cases += [(1, {"entry_price": 1})]
        for index, changes in cases:
            altered = copy.deepcopy(artifact)
            mark = altered["entries"][index]["observations"][str(horizon)]
            altered["entries"][index]["observations"][str(horizon)] = {
                **mark,
                **changes,
            }
            checked_copy(altered, invalid)
            invoke("analyze", str(invalid), succeeds=False)
        for elapsed in (horizon, horizon + 5):
            altered = copy.deepcopy(artifact)
            mark = altered["entries"][0]["observations"][str(horizon)]
            altered["entries"][0]["observations"][str(horizon)] = {
                **mark,
                "elapsed_s": elapsed,
                "outcome_utc": timestamp(1, elapsed),
            }
            checked_copy(altered, invalid)
            assert (
                analyze(invalid)["pooled_descriptive_outcomes"][str(horizon)]["n"] > 0
            )
    # Immediate or late censorship is genuine unknown data, not an observed zero.
    for elapsed in (0, 1000):
        altered = copy.deepcopy(artifact)
        altered["entries"][0]["observations"]["60"] = {
            **altered["entries"][0]["observations"]["60"],
            "entry_price": None,
            "exit_price": None,
            "elapsed_s": elapsed,
            "reason": "entry_price_missing",
            "outcome_utc": timestamp(1, elapsed),
            "status": "censored",
        }
        checked_copy(altered, invalid)
        report = analyze(invalid)
        assert report["horizon_status_counts"]["60"]["censored"] == 1
        assert report["pooled_descriptive_outcomes"]["60"]["missing_n"] == 3


def verify_limits(artifact: dict, root: Path) -> None:
    invalid = root / "oversized.json"
    invalid.write_bytes(b" " * (clues.MAX_ARTIFACT_BYTES + 1))
    with invalid.open("rb") as stream:
        with (
            patch.object(Path, "open", return_value=stream),
            patch.object(stream, "read", wraps=stream.read) as reading,
            patch.object(
                clues.json,
                "loads",
                side_effect=AssertionError("decoded oversized input"),
            ),
        ):
            invoke("analyze", str(invalid), succeeds=False)
            reading.assert_called_once_with(clues.MAX_ARTIFACT_BYTES + 1)
    for field in ("mint", "symbol"):
        altered = copy.deepcopy(artifact)
        altered["entries"][0][field] = "x" * (clues.MAX_TEXT_CHARS + 1)
        checked_copy(altered, invalid)
        invoke("analyze", str(invalid), succeeds=False)
    with patch.object(clues, "MAX_MEMBERS", len(artifact["entries"])):
        invoke("analyze", str(root / "cohort.json"), succeeds=False)


def main() -> None:
    with TemporaryDirectory(prefix="verify-accept-clues-") as temporary:
        root = Path(temporary)
        database, frozen = root / "synthetic.sqlite3", root / "cohort.json"
        with closing(sqlite3.connect(database)) as producer:
            producer.executescript(
                "CREATE TABLE lessons(id INTEGER PRIMARY KEY, utc TEXT, kind TEXT,"
                " mint TEXT, symbol TEXT, decision TEXT, buyers INTEGER, real_sol REAL, mayhem INTEGER);"
                "CREATE TABLE paper_marks(entry_id INTEGER, horizon_s INTEGER, scheduled_utc TEXT,"
                " entry_price REAL, exit_price REAL, elapsed_s REAL, outcome_utc TEXT, reason TEXT,"
                " PRIMARY KEY(entry_id,horizon_s));"
            )
            producer.execute("PRAGMA journal_mode=WAL")
            producer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            main_before = digest(database)
            producer.executemany(
                "INSERT INTO lessons VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        i,
                        f"2026-10-01T00:00:{i:02d}Z",
                        "gate_pass",
                        "repeat" if i < 3 else f"mint{i}",
                        f"S{i}",
                        "buyers_present",
                        None if i == 1 else i,
                        None if i < 3 else float(i),
                        None if i == 1 else i % 2,
                    )
                    for i in range(1, 31)
                ]
                + [
                    (
                        31,
                        "2026-10-01T00:00:31Z",
                        "gate_pass",
                        "skip",
                        None,
                        "timeout",
                        1,
                        1,
                        0,
                    ),
                    (
                        32,
                        "2026-10-01T00:00:32Z",
                        "paper_fill",
                        "non-gate",
                        None,
                        "buyers_present",
                        1,
                        1,
                        0,
                    ),
                ],
            )
            marks = []
            for i in range(1, 30):
                for horizon in clues.HORIZONS:
                    if i == 2 and horizon == 900:
                        continue
                    pending = i == 5 or (i == 4 and horizon == 60)
                    censored = i == 3 and horizon == 300
                    marks.append(
                        (
                            i,
                            horizon,
                            timestamp(i),
                            None if i == 5 else 1.0,
                            None if pending or censored else 1 + i / 100,
                            None if pending else float(horizon),
                            None if pending else timestamp(i, horizon),
                            None
                            if pending
                            else "missing_observation"
                            if censored
                            else "observed",
                        )
                    )
            producer.executemany(
                "INSERT INTO paper_marks VALUES(?,?,?,?,?,?,?,?)", marks
            )
            producer.commit()
            wal = Path(str(database) + "-wal")
            assert digest(database) == main_before and wal.stat().st_size > 0
            source_before = (digest(database), digest(wal))
            invoke("freeze", "--db", str(database), "--out", str(frozen))
            assert source_before == (digest(database), digest(wal))
            artifact = json.loads(frozen.read_text())
            expected_digest = hashlib.sha256(
                json.dumps(
                    {
                        key: value
                        for key, value in artifact.items()
                        if key != "content_sha256"
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            assert artifact["content_sha256"] == expected_digest
            assert len(artifact["entries"]) == 29
            assert artifact["source_watermark"]["lessons_count"] == 32
            assert artifact["outside_study"] == [
                {
                    "id": 30,
                    "utc": "2026-10-01T00:00:30Z",
                    "mint": "mint30",
                    "reason": "no_planned_marks",
                }
            ]
            assert [row["mint"] for row in artifact["entries"][:2]] == [
                "repeat",
                "repeat",
            ]
            assert artifact["entries"][0]["features"]["buyers"] is None
            assert artifact["entries"][0]["features"]["mayhem"] is None
            assert (
                artifact["entries"][1]["observations"]["900"]["status"]
                == "missing_plan"
            )
            assert artifact["entries"][2]["observations"]["300"]["status"] == "censored"
            assert artifact["entries"][3]["observations"]["60"]["status"] == "pending"
            report = analyze(frozen)
            identity = report["code_identity"]
            assert identity["producing"] == artifact["code"]
            assert identity["executing"] == {
                "file": "analyze_accept_clues.py",
                "sha256": digest(Path(clues.__file__)),
            }
            assert identity["same_sha256"] is True
            differing = {**artifact, "code": {**artifact["code"], "sha256": "0" * 64}}
            differing_path = root / "different-code.json"
            checked_copy(differing, differing_path)
            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("code path dereferenced")
            ):
                different_report = analyze(differing_path)
            assert different_report["code_identity"]["same_sha256"] is False
            assert different_report["code_identity"]["producing"] == differing["code"]
            assert (
                different_report["code_identity"]["executing"] == identity["executing"]
            )
            verify_observations(artifact, root)
            verify_limits(artifact, root)
            for name, limit in (
                ("MAX_ARTIFACT_BYTES", 100),
                ("MAX_MEMBERS", 1),
                ("MAX_TEXT_CHARS", 3),
            ):
                oversized_output = root / f"{name}.json"
                with patch.object(clues, name, limit):
                    invoke(
                        "freeze",
                        "--db",
                        str(database),
                        "--out",
                        str(oversized_output),
                        succeeds=False,
                    )
                assert not oversized_output.exists()
            for field, value in (
                ("outcome_utc", None),
                ("elapsed_s", None),
                ("reason", None),
                ("elapsed_s", 1),
                ("elapsed_s", 900),
                ("scheduled_utc", timestamp(0)),
                ("outcome_utc", timestamp(0)),
            ):
                producer.execute(
                    f"UPDATE paper_marks SET {field}=? WHERE entry_id=1 AND horizon_s=60",
                    (value,),
                )
                producer.commit()
                bad_source = root / "bad-source.json"
                invoke(
                    "freeze",
                    "--db",
                    str(database),
                    "--out",
                    str(bad_source),
                    succeeds=False,
                )
                assert not bad_source.exists()
                producer.execute(
                    f"UPDATE paper_marks SET {field}=? WHERE entry_id=1 AND horizon_s=60",
                    (artifact["entries"][0]["observations"]["60"][field],),
                )
                producer.commit()
            assert report["splits"]["train"]["entry_ids"] == list(range(1, 25))
            assert report["splits"]["holdout"]["entry_ids"] == list(range(25, 30))
            assert report["comparison_count"] == len(report["comparisons"]) == 9
            assert report["comparisons"][0]["train"]["n"] == 21
            assert report["comparisons"][0]["holdout"]["n"] == 5
            assert report["comparisons"][0]["train"]["rho"] == 1.0
            assert report["comparisons"][0]["holdout"]["rho"] == 1.0
            assert report["comparisons"][3]["train"]["n"] == 20
            assert report["horizon_status_counts"]["900"]["missing_plan"] == 1
            assert report["horizon_status_counts"]["60"]["pending"] == 2
            assert report["horizon_status_counts"]["300"]["censored"] == 1
            assert report["missing_features"]["buyers"] == 1
            original_bytes = frozen.read_bytes()
            producer.execute(
                "UPDATE paper_marks SET entry_price=1,exit_price=3,elapsed_s=horizon_s,"
                "outcome_utc=strftime('%Y-%m-%dT%H:%M:%SZ',scheduled_utc,"
                "'+' || horizon_s || ' seconds'),reason='observed' WHERE exit_price IS NULL"
            )
            producer.execute(
                "INSERT INTO lessons VALUES(33,'2026-10-02T00:00:00Z','gate_pass',"
                "'append',NULL,'buyers_present',10,10,0)"
            )
            producer.execute(
                "INSERT INTO paper_marks VALUES(33,60,'2026-10-02T00:00:00Z',1,2,60,"
                "'2026-10-02T00:01:00Z','observed')"
            )
            producer.commit()
            assert analyze(frozen) == report and frozen.read_bytes() == original_bytes
            invoke(
                "freeze", "--db", str(database), "--out", str(frozen), succeeds=False
            )
            assert frozen.read_bytes() == original_bytes
            newer = root / "newer.json"
            invoke("freeze", "--db", str(database), "--out", str(newer))
            assert len(json.loads(newer.read_text())["entries"]) == 30
            assert analyze(newer) != report
            for keep, label in ((3, "constant"), (1, "small"), (0, "empty")):
                producer.execute("DELETE FROM paper_marks WHERE entry_id > ?", (keep,))
                producer.execute("DELETE FROM lessons WHERE id > ?", (keep,))
                producer.execute("UPDATE lessons SET buyers=1,real_sol=1")
                producer.execute(
                    "UPDATE paper_marks SET entry_price=1,exit_price=2,"
                    "elapsed_s=horizon_s,reason='observed',"
                    "outcome_utc=strftime('%Y-%m-%dT%H:%M:%SZ',scheduled_utc,"
                    "'+' || horizon_s || ' seconds')"
                )
                producer.commit()
                path = root / f"{label}.json"
                invoke("freeze", "--db", str(database), "--out", str(path))
                insufficient = analyze(path)
                assert insufficient["comparisons"][0]["train"]["rho"] is None
                assert insufficient["comparisons"][0]["holdout"]["rho"] is None
            malformed = []
            altered = copy.deepcopy(artifact)
            altered["entries"][0]["features"]["buyers"] = 999
            malformed.append(altered)  # unchanged checksum
            for change in (
                "duplicate",
                "order",
                "feature",
                "status",
                "split",
                "version",
            ):
                altered = copy.deepcopy(artifact)
                if change == "duplicate":
                    altered["entries"][1]["id"] = altered["entries"][0]["id"]
                elif change == "order":
                    altered["entries"] = list(reversed(altered["entries"]))
                elif change == "feature":
                    altered["entries"][0]["features"]["buyers"] = True
                elif change == "status":
                    altered["entries"][0]["observations"]["60"]["status"] = "pending"
                elif change == "split":
                    altered["policy"]["train_n"] = 2
                else:
                    altered["schema_version"] = 2
                altered["content_sha256"] = clues.content_digest(altered)
                malformed.append(altered)
            invalid = root / "invalid.json"
            for altered in malformed:
                invalid.write_text(json.dumps(altered))
                invoke("analyze", str(invalid), succeeds=False)
            for raw in (
                '{"schema_version":1,"schema_version":1}',
                '{"x":NaN}',
                '{"x":1e999}',
                "{",
            ):
                invalid.write_text(raw)
                invoke("analyze", str(invalid), succeeds=False)
            producer.execute(
                "INSERT INTO lessons VALUES(1,'2026-10-01T00:00:00Z','gate_pass',"
                "'invalid',NULL,'buyers_present',1,?,0)",
                (float("inf"),),
            )
            producer.execute(
                "INSERT INTO paper_marks VALUES(1,60,'2026-10-01T00:00:00Z',"
                "1,2,60,'2026-10-01T00:01:00Z','observed')"
            )
            producer.commit()
            invalid_output = root / "invalid-source.json"
            invoke(
                "freeze",
                "--db",
                str(database),
                "--out",
                str(invalid_output),
                succeeds=False,
            )
            assert not invalid_output.exists()
        database.unlink()
        assert (
            analyze(frozen) == report
        )  # self-contained, not merely disconnected from an open producer
    print(
        "accept clues: OK (WAL/read-only snapshot, fixed membership, append/resolution invariance, "
        "missingness, repeated mints, nine comparisons, insufficient data, corrupt input, "
        "completion/timing semantics, code identities, bounded I/O and overwrite refusal)"
    )


if __name__ == "__main__":
    main()
