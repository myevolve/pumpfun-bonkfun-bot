# ruff: noqa: S101
"""Shadow-tracker cohort tags, story match, and control hash."""

from __future__ import annotations

import hashlib
from pathlib import Path

from meme_studio.shadow_join import load_tracker, tag_cohorts

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shadow_tracker_sample.json"


def _tags(mint: str, tagged: list[object]) -> set[str]:
    for coin in tagged:
        if coin.mint == mint:
            return set(coin.cohorts)
    raise AssertionError(mint)


def test_fixture_tags_profile_control_and_whole_word_stories() -> None:
    tracker = load_tracker(_FIXTURE)
    tagged = tag_cohorts(tracker.coins, tracker.concepts)
    by_mint = {coin.mint: coin for coin in tagged}

    assert _tags("So10000paperMint", tagged) >= {"population", "N", "P", "P_x"}
    assert "C" not in _tags("So10000paperMint", tagged)
    assert "story-kiwi" in by_mint["So10000paperMint"].story_ids
    assert by_mint["So10000paperMint"].is_leader is True

    control = _tags("So10002paperMint", tagged)
    digest = int(hashlib.sha256(b"So10002paperMint").hexdigest(), 16)
    assert digest % 10 == 0
    assert control >= {"population", "N", "P", "P_x", "C"}
    assert by_mint["So10002paperMint"].is_leader is False

    plain = _tags("So10001paperMint", tagged)
    assert plain >= {"population", "P_minus_N"}
    assert "N" not in plain
    assert "P" not in plain

    big_dev = _tags("So10003paperMint", tagged)
    assert "N" in big_dev
    assert "P" not in big_dev
    assert "P_x" not in big_dev

    assert "population" not in _tags("So10004paperMint", tagged)
    assert "C" not in _tags("So10004paperMint", tagged)
    assert "N" not in _tags("So10004paperMint", tagged)

    squash = by_mint["So10005paperMint"]
    assert "story-kiwi" not in squash.story_ids
    assert "N" not in squash.cohorts
    assert "P_x" not in squash.cohorts
    assert "population" in squash.cohorts


def test_winner_share_uses_outside_buy_volume_only() -> None:
    tracker = load_tracker(_FIXTURE)
    stories = {row.story_id: row for row in tracker.story_summaries()}

    kiwi = stories["story-kiwi"]
    assert kiwi.leader_mint == "So10000paperMint"
    assert kiwi.winner_share == "0.8"
    assert stories["story-marmot"].n_coins == 0
    assert stories["story-marmot"].winner_share is None
