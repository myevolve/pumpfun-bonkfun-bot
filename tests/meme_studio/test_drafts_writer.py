# ruff: noqa: S101
"""Launch-day drafts are written to disk and linted. Nothing is posted."""

from __future__ import annotations

import ast
import hashlib
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meme_studio.drafts import ConceptPackage, SellEvent, write_launch_drafts
from meme_studio.errors import NaiveTimeError
from tests.meme_studio.conftest import CREATOR, MINT

_M1_CHAR_LIMIT = 280


def _concept() -> ConceptPackage:
    return ConceptPackage(
        story_id="story-pigeon",
        story_one_liner="park pigeons started a tiny union",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
        ticker="BENCH",
        character="a round cartoon pigeon in a paper hat",
        mint=MINT,
        game_url="https://games.example/bench",
        plan_url="https://games.example/bench/plan",
        plan_hash="ab" * 32,
        wallet=CREATOR,
        dev_buy_sol="0.1",
        studio="ParodyDesk",
        steve_handle="steve",
        hub_domain="games.example",
    )


def test_schedule_writes_channel_drafts(tmp_path: Path) -> None:
    t0 = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    records = write_launch_drafts(_concept(), t0, tmp_path)
    ids = [record.template_id for record in records]
    for required in (
        "M0",
        "M1",
        "M2",
        "M1-TG",
        "P1",
        "M3",
        "M4",
        "M5",
        "M7",
        "M8",
        "M10",
        "M11",
        "game_page",
    ):
        assert required in ids
    channels = {record.channel for record in records}
    assert channels == {"x", "telegram", "site"}
    m0 = next(record for record in records if record.template_id == "M0")
    assert m0.scheduled_at == t0 - timedelta(minutes=5)
    assert "BENCH" not in m0.text
    assert "Not financial advice" in m0.text
    m1 = next(record for record in records if record.template_id == "M1")
    assert m1.scheduled_at == t0 + timedelta(minutes=1)
    assert "http" not in m1.text.lower()
    assert "AI-generated" in m1.text
    assert "launched by us" in m1.text
    assert len(m1.text) <= _M1_CHAR_LIMIT
    assert m1.lint.passed is False
    assert m1.lint.human_approval_required is True
    assert "human_approval_required:trend_ref" in m1.lint.reasons
    m2 = next(record for record in records if record.template_id == "M2")
    assert m2.is_reply is True
    assert MINT in m2.text
    assert "https://games.example/bench" in m2.text
    page_early = next(record for record in records if record.offset == "T-1m")
    page_live = next(record for record in records if record.offset == "T+30s")
    assert page_early.scheduled_at == t0 - timedelta(minutes=1)
    assert page_live.scheduled_at == t0 + timedelta(seconds=30)
    assert MINT not in page_early.text
    assert MINT in page_live.text
    assert "we created this coin" in page_live.text.lower()
    m10 = next(
        record
        for record in records
        if record.template_id == "M10" and record.channel == "x"
    )
    assert m10.scheduled_at == t0 + timedelta(hours=24)
    for record in records:
        lowered = record.text.lower()
        assert "we created this coin" in lowered or "launched by us" in lowered
        assert "dev bag" in lowered or "dev buy" in lowered
        if record.template_id == "M1":
            continue
        assert record.lint.passed, (
            record.template_id,
            record.channel,
            record.lint.reasons,
        )
        payload = json.loads(record.path.read_text(encoding="utf-8"))
        assert payload["text"] == record.text
        assert payload["lint"]["status"] == "pass"
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    offsets = {row["offset"] for row in manifest["schedule"]}
    for label in ("T-30m", "T-5m", "T+1m", "T+24h"):
        assert label in offsets
    assert manifest["phase"] == "shadow"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert path.suffix == ".json"


def test_missing_metrics_stay_unknown_and_sells_reconcile(tmp_path: Path) -> None:
    t0 = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    bare = write_launch_drafts(_concept(), t0, tmp_path / "bare")
    m7 = next(record for record in bare if record.template_id == "M7")
    m10 = next(
        record
        for record in bare
        if record.template_id == "M10" and record.channel == "x"
    )
    assert "UNKNOWN" in m7.text
    assert "player1" not in m7.text
    assert "900" not in m7.text
    assert "0.01" not in m10.text
    assert "UNKNOWN" in m10.text
    plan = "Recover cost at 2.5 times, then stop. It can go to zero."
    planned = ConceptPackage(
        story_id="story-pigeon",
        story_one_liner="park pigeons started a tiny union",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
        ticker="BENCH",
        character="a round cartoon pigeon in a paper hat",
        mint=MINT,
        game_url="https://games.example/bench",
        plan_url="https://games.example/bench/plan",
        plan_hash="not-the-real-hash",
        plan_text=plan,
        wallet=CREATOR,
        dev_buy_sol="0.1",
        studio="ParodyDesk",
        steve_handle="steve",
        hub_domain="games.example",
        sells=(
            SellEvent(
                n=1,
                step="R",
                tokens="1000",
                sol="0.2",
                multiple="2.5",
                pct="55",
                sig="5" * 88,
                at=datetime(2026, 10, 28, 19, 0, tzinfo=UTC),
            ),
        ),
    )
    drafted = write_launch_drafts(planned, t0, tmp_path / "planned")
    p1 = next(record for record in drafted if record.template_id == "P1")
    digest = hashlib.sha256(plan.encode("utf-8")).hexdigest()
    assert digest in p1.text
    assert "not-the-real-hash" not in p1.text
    assert "5 times, 8 times" not in p1.text
    recap = next(
        record
        for record in drafted
        if record.template_id == "M10" and record.channel == "x"
    )
    assert "dev sells 1" in recap.text
    assert "55%" in recap.text
    assert "dev sells 0" not in recap.text
    assert "100%" not in recap.text


def test_naive_sell_timestamp_writes_nothing(tmp_path: Path) -> None:
    concept = ConceptPackage(
        story_id="story-pigeon",
        story_one_liner="park pigeons started a tiny union",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
        ticker="BENCH",
        character="a round cartoon pigeon in a paper hat",
        mint=MINT,
        game_url="https://games.example/bench",
        plan_url="https://games.example/bench/plan",
        plan_hash="ab" * 32,
        wallet=CREATOR,
        dev_buy_sol="0.1",
        studio="ParodyDesk",
        steve_handle="steve",
        hub_domain="games.example",
        sells=(
            SellEvent(
                n=1,
                step="R",
                tokens="1000",
                sol="0.2",
                multiple="2.5",
                pct="55",
                sig="5" * 88,
                at=datetime(2026, 10, 28, 19, 0),
            ),
        ),
    )
    with pytest.raises(NaiveTimeError):
        write_launch_drafts(
            concept, datetime(2026, 10, 28, 18, 0, tzinfo=UTC), tmp_path
        )
    assert list(tmp_path.iterdir()) == []


def test_recap_only_keeps_the_24h_post_and_drops_leaderboard(tmp_path: Path) -> None:
    t0 = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    records = write_launch_drafts(_concept(), t0, tmp_path, mode="recap_only")
    ids = {record.template_id for record in records}
    assert "M10" in ids
    assert "M0" in ids
    assert "M7" not in ids
    assert "M8" not in ids
    assert "M11" not in ids


def test_sell_log_draft_is_written_not_sent(tmp_path: Path) -> None:
    concept = ConceptPackage(
        story_id="story-pigeon",
        story_one_liner="park pigeons started a tiny union",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
        ticker="BENCH",
        character="a round cartoon pigeon in a paper hat",
        mint=MINT,
        game_url="https://games.example/bench",
        plan_url="https://games.example/bench/plan",
        plan_hash="ab" * 32,
        wallet=CREATOR,
        dev_buy_sol="0.1",
        studio="ParodyDesk",
        steve_handle="steve",
        hub_domain="games.example",
        sells=(
            SellEvent(
                n=1,
                step="R",
                tokens="1000",
                sol="0.2",
                multiple="2.5",
                pct="55",
                sig="5" * 88,
                at=datetime(2026, 10, 28, 19, 0, tzinfo=UTC),
            ),
        ),
    )
    records = write_launch_drafts(
        concept, datetime(2026, 10, 28, 18, 0, tzinfo=UTC), tmp_path
    )
    sell = next(record for record in records if record.template_id == "P2")
    assert sell.lint.passed is True
    assert "Dev sell #1" in sell.text
    assert sell.scheduled_at == datetime(2026, 10, 28, 19, 0, tzinfo=UTC)


def test_drafts_package_has_no_publish_function() -> None:
    root = Path(__file__).resolve().parents[2] / "meme_studio" / "drafts"
    banned = {"publish", "send_message", "post_tweet", "create_tweet"}
    found: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in banned:
                found.append(node.name)
    assert found == []


def test_writer_does_not_open_sockets(tmp_path: Path, monkeypatch: object) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", boom)
    records = write_launch_drafts(
        _concept(),
        datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
        tmp_path,
    )
    assert records
