# ruff: noqa: S101, PLR2004, S603
"""Shadow CLI: drafts only, fixtures only, no sockets and no wallet."""

import ast
import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from meme_studio.trends.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-10-28T15:00:00+00:00"
ROOT = Path(__file__).resolve().parents[2]
BASE_UNKNOWNS = {
    "reddit_data_api_commercial_terms",
    "uspto_bulk_query_permitted",
    "google_trends_public_api",
    "hour_of_day_weights_unverified",
    "audience_fit_priors_empty",
    "rule_based_person_detector_is_not_a_trained_ner",
    "x_trends_http_shape_unverified_until_live_read",
    "dexscreener_volume_field_is_h24_not_7d",
}


def _sid(title: str) -> str:
    normalized = " ".join(title.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def test_dry_run_writes_a_shadow_concept(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("REDDIT_TOS_ALLOWED", raising=False)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", "CANARY-PRIVATE-KEY-VALUE")
    monkeypatch.setenv("X_BEARER_TOKEN", "CANARY-BEARER-VALUE")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    code = main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--output",
            str(tmp_path),
            "--now",
            NOW,
        ]
    )
    assert code == 0
    concept_path = tmp_path / "concept.json"
    cards_path = tmp_path / "story_cards.json"
    raw = concept_path.read_text(encoding="utf-8")
    assert "CANARY-PRIVATE-KEY-VALUE" not in raw
    assert "CANARY-BEARER-VALUE" not in raw
    payload = json.loads(raw)
    assert payload["status"] == "draft"
    assert payload["phase"] == "0"
    assert payload["mode"] == "shadow-dry-run"
    assert payload["publish"] is False
    assert payload["transactions"] is False
    assert payload["network_writes"] == "none"
    assert payload["selected_ticker"] == "RACCDONU"
    assert payload["date"] == "2026-10-28"
    assert payload["skipped_reason"] is None
    titles = [card["title"] for card in payload["top_stories"]]
    assert titles == ["A raccoon escaped with a donut"]
    rejected = [card["title"] for card in payload["rejected_stories"]]
    assert "Earthquake hits the coast" in rejected
    assert "A goose stole a pretzel" not in titles
    assert "A goose stole a pretzel" not in rejected
    assert "reddit_source_skipped_tos_unknown" in payload["unknowns"]
    assert BASE_UNKNOWNS <= set(payload["unknowns"])
    cards = json.loads(cards_path.read_text(encoding="utf-8"))
    assert cards == payload["top_stories"]
    story_id = _sid("A raccoon escaped with a donut")
    stage = tmp_path / "runs" / "2026-10-28" / story_id
    assert (stage / "detect.json").is_file()
    assert (stage / "score.json").is_file()
    assert (stage / "concept.json").is_file()
    quake = _sid("Earthquake hits the coast")
    assert (tmp_path / "runs" / "2026-10-28" / quake / "score.json").is_file()
    again = main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--output",
            str(tmp_path),
            "--now",
            NOW,
        ]
    )
    assert again == 0
    assert concept_path.read_text(encoding="utf-8") == raw


def test_reddit_fixture_is_used_only_when_the_operator_allows_tos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("REDDIT_TOS_ALLOWED", "1")
    code = main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--output",
            str(tmp_path),
            "--now",
            NOW,
        ]
    )
    assert code == 0
    payload = json.loads((tmp_path / "concept.json").read_text(encoding="utf-8"))
    titles = [card["title"] for card in payload["top_stories"]]
    assert "A goose stole a pretzel" in titles
    assert "A raccoon escaped with a donut" in titles
    assert "reddit_source_skipped_tos_unknown" not in payload["unknowns"]
    assert payload["selected_ticker"] in {"RACCDONU", "GOOSPRET"}


def test_missing_token_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("REDDIT_TOS_ALLOWED", raising=False)
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    for name in (
        "google_news.rss",
        "history.json",
        "reddit.json",
        "x.json",
        "copycats.json",
    ):
        (fixture_dir / name).write_bytes((FIXTURES / name).read_bytes())
    out = tmp_path / "out"
    code = main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(fixture_dir),
            "--output",
            str(out),
            "--now",
            NOW,
        ]
    )
    assert code == 0
    payload = json.loads((out / "concept.json").read_text(encoding="utf-8"))
    assert payload["status"] == "skipped"
    assert payload["skipped_reason"] == "lookup_unavailable"
    assert payload["selected_ticker"] is None
    assert payload["candidates"][0]["ticker"] == "RACCDONU"
    assert payload["candidates"][0]["accepted"] is False
    assert payload["candidates"][0]["collision_checks"][0]["reason"] == (
        "lookup_unavailable"
    )


def test_without_dry_run_refuses_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = main(["run", "--output", str(out), "--fixtures", str(FIXTURES)])
    assert code == 2
    assert not out.exists()


def test_no_sources_still_writes_a_skipped_concept(tmp_path: Path) -> None:
    out = tmp_path / "out"
    empty = tmp_path / "empty"
    empty.mkdir()
    code = main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(empty),
            "--output",
            str(out),
            "--now",
            NOW,
        ]
    )
    assert code == 0
    payload = json.loads((out / "concept.json").read_text(encoding="utf-8"))
    assert payload["status"] == "skipped"
    assert payload["skipped_reason"] == "no_sources"
    assert payload["publish"] is False
    assert payload["transactions"] is False


def test_default_output_dir_is_under_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REDDIT_TOS_ALLOWED", raising=False)
    code = main(["run", "--dry-run", "--fixtures", str(FIXTURES), "--now", NOW])
    assert code == 0
    assert (tmp_path / "out" / "meme_studio" / "concept.json").is_file()


def test_module_entrypoint(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("REDDIT_TOS_ALLOWED", None)
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not current else src + os.pathsep + current
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "meme_studio.trends",
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--output",
            str(tmp_path),
            "--now",
            NOW,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "concept.json").is_file()
    assert "CANARY" not in result.stdout


def test_package_source_does_not_touch_wallets_or_posts() -> None:
    roots = [
        ROOT / "src" / "meme_studio" / "trends",
        ROOT / "src" / "meme_studio" / "concept",
    ]
    forbidden = ("SOLANA_PRIVATE_KEY", "load_dotenv", "Keypair", "solders")
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                assert needle not in text, path
            assert 'method="POST"' not in text
            assert "method='POST'" not in text
            assert "requests.post" not in text
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                assert "solders" not in names
                assert "solana" not in names
                assert "dotenv" not in names
