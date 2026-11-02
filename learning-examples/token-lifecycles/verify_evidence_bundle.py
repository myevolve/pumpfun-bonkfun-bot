"""Offline consumer check for paired marks, WAL provenance and immutable exports.

Run: python -B learning-examples/token-lifecycles/verify_evidence_bundle.py
All inputs and outputs are synthetic and temporary; no RPC or secrets are loaded.
"""

# ruff: noqa: S101, S603, PLR2004 - fixed local CLI and exact synthetic expectations
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import build_evidence_bundle as bundle


def main() -> None:
    """Exercise the actual CLI while a synthetic producer still holds its WAL."""
    with TemporaryDirectory(prefix="verify-evidence-") as temporary:
        root = Path(temporary)
        database = root / "lessons.sqlite3"
        tape, shadow, output = (
            root / "tape.jsonl",
            root / "shadow.jsonl",
            root / "bundle.json",
        )
        tape.write_text(json.dumps({"create_ts": 1, "graduated_dslot": None}) + "\n")
        shadow.write_text(json.dumps({"k": "synthetic_no_observation"}) + "\n")
        with closing(sqlite3.connect(database)) as producer:
            producer.executescript(
                "CREATE TABLE lessons (id INTEGER PRIMARY KEY, utc TEXT, decision TEXT);"
                "CREATE TABLE paper_marks (exit_price REAL, elapsed_s REAL, reason TEXT);"
                "CREATE TABLE grad_marks (open_price REAL, p5 REAL, p30 REAL, p120 REAL);"
                "INSERT INTO grad_marks VALUES (1,2,NULL,1),(100,NULL,50,200),(NULL,NULL,NULL,NULL);"
            )
            producer.commit()
            producer.execute("PRAGMA journal_mode=WAL")
            producer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source_hash = bundle.sha256_of(database)
            producer.executemany(
                "INSERT INTO lessons VALUES (?,?,?)",
                [
                    (1, "2026-10-31T11:30:00Z", "buyers_present"),
                    (2, "2026-10-31T10:30:00Z", "too_many_buyers"),
                    (3, "2026-10-30T01:00:00Z", "too_much_sol"),
                ],
            )
            producer.commit()
            assert bundle.sha256_of(database) == source_hash
            instant = datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp()
            with patch.object(bundle.time, "time", return_value=instant):
                report = bundle.audit_lessons(database)
            assert report["lessons_total"] == 3
            assert report["lessons_last_hour"] == 1
            assert report["decisions_last_24h"] == {
                "buyers_present": 1,
                "too_many_buyers": 1,
            }
            assert report["grad_marks_total"] == 3
            decay = report["grad_decay"]
            assert decay == {
                "n": 2,
                "p5_n": 1,
                "p5_over_open": 2.0,
                "p30_n": 1,
                "p30_over_open": 0.5,
                "p120_n": 2,
                "p120_over_open": 1.5,
            }
            command = [
                sys.executable,
                "-B",
                str(Path(bundle.__file__).resolve()),
                "--tape",
                str(tape),
                "--shadow",
                str(shadow),
                "--lessons",
                str(database),
                "--out",
                str(output),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            evidence = json.loads(output.read_text())
            assert evidence["learning_journal"]["lessons_total"] == 3
            assert evidence["learning_journal"]["grad_decay"] == decay
            assert evidence["provenance"]["lessons_snapshot_sha256"] != source_hash
            digest = evidence.pop("bundle_sha256")
            assert (
                digest
                == hashlib.sha256(
                    json.dumps(
                        evidence, indent=1, default=str, allow_nan=False
                    ).encode()
                ).hexdigest()
            )
            assert bundle.sha256_of(database) == source_hash
            original = output.read_bytes()
            refused = subprocess.run(command, capture_output=True, check=False)
            assert refused.returncode != 0 and output.read_bytes() == original
            shadow.write_text('{"k":')
            corrupt_output = root / "corrupt.json"
            refused = subprocess.run(
                [*command[:-1], str(corrupt_output)], capture_output=True, check=False
            )
            assert refused.returncode != 0 and not corrupt_output.exists()
    print(
        "evidence bundle: OK (paired missingness, exact windows, WAL snapshot, immutable export, corrupt input refusal)"
    )


if __name__ == "__main__":
    main()
