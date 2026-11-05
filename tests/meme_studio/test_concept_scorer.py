# ruff: noqa: S101, PLR2004
"""Rule scorer: velocity x parodyability x safety x crowding x fit x hour."""

import hashlib
import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from meme_studio.concept.inputs import (
    CopycatCoin,
    CountPoint,
    OutletHit,
    StoryInput,
)
from meme_studio.concept.scorer import score_stories

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def _sid(title: str) -> str:
    normalized = " ".join(title.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _counts(values: list[int], recent: int) -> tuple[CountPoint, ...]:
    baseline_start = NOW - timedelta(minutes=390)
    points = [
        CountPoint(start=baseline_start + timedelta(minutes=30 * index), count=value)
        for index, value in enumerate(values)
    ]
    points.append(CountPoint(start=NOW - timedelta(minutes=30), count=recent))
    return tuple(points)


def _story(
    title: str,
    *,
    outlets: int = 0,
    counts: tuple[CountPoint, ...] = (),
    copycats: tuple[CopycatCoin, ...] = (),
    extra_outlets: tuple[OutletHit, ...] = (),
) -> StoryInput:
    hits = [
        OutletHit(name=f"Outlet {index}", published_at=NOW - timedelta(minutes=10))
        for index in range(outlets)
    ]
    return StoryInput(
        title=title,
        summary=title,
        detected_at=NOW - timedelta(minutes=20),
        sources=("google_news",),
        outlets=tuple(hits) + extra_outlets,
        post_counts=counts,
        copycats=copycats,
    )


def _expected_z(values: list[int], recent: int) -> float:
    mean = statistics.mean(values)
    std = statistics.stdev(values)
    if std < 1e-9:
        return float(recent - mean)
    return (recent - mean) / std


def test_score_is_the_product_of_the_rule_factors() -> None:
    values = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24]
    title = "A raccoon escaped with a donut"
    old = OutletHit(name="Stale Wire", published_at=NOW - timedelta(hours=2))
    duplicate = OutletHit(name="outlet 0", published_at=NOW - timedelta(minutes=5))
    story = _story(
        title,
        outlets=3,
        counts=_counts(values, 40),
        copycats=(
            CopycatCoin("Raccoon Cash", "RACCASH", 10, 1.0, 100.0),
            CopycatCoin("Raccoon OG", "RACOG", 41, 5.0, 50000.0),
        ),
        extra_outlets=(old, duplicate),
    )
    batch = score_stories(
        [story],
        now=NOW,
        priors={"animal": 0.5},
        hour_weights={8: 2.0},
    )
    card = batch.top_stories[0]
    velocity = max(0.0, _expected_z(values, 40)) + math.log1p(3)
    crowding = (1.0 / 3.0) * 0.25
    assert card.story_id == _sid(title)
    assert card.hook == "raccoon+donut"
    assert card.category == "animal"
    assert card.parodyability == 1.0
    assert card.safety_gate == 1
    assert card.safety_reasons == ()
    assert card.velocity == pytest.approx(velocity)
    assert card.crowding_penalty == pytest.approx(crowding)
    assert card.audience_fit == pytest.approx(0.5)
    assert card.hour_multiplier == pytest.approx(2.0)
    assert card.copycat_count == 2
    assert card.leader_outside_buyers == 41
    assert card.score == pytest.approx(
        card.velocity
        * card.parodyability
        * card.safety_gate
        * card.crowding_penalty
        * card.audience_fit
        * card.hour_multiplier
    )
    assert card.rank == 1


def test_velocity_without_counts_uses_outlets_only() -> None:
    batch = score_stories(
        [_story("A raccoon escaped with a donut", outlets=1)],
        now=NOW,
    )
    card = batch.top_stories[0]
    assert card.velocity == pytest.approx(math.log1p(1))
    assert card.hour_multiplier == 1.0
    assert card.audience_fit == 1.0
    assert card.crowding_penalty == 1.0


def test_flat_baseline_uses_excess_instead_of_a_divide_by_zero() -> None:
    high = _story(
        "A raccoon escaped with a donut",
        counts=_counts([0] * 12, 40),
    )
    quiet = _story(
        "A goose stole a pretzel",
        outlets=2,
        counts=_counts([10] * 12, 4),
    )
    batch = score_stories([high, quiet], now=NOW)
    by_title = {card.title: card for card in batch.top_stories}
    assert by_title[high.title].velocity == pytest.approx(40.0)
    assert by_title[quiet.title].velocity == pytest.approx(math.log1p(2))


def test_negative_excess_does_not_reduce_outlet_velocity() -> None:
    story = _story(
        "A raccoon escaped with a donut",
        outlets=1,
        counts=_counts([2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24], 1),
    )
    card = score_stories([story], now=NOW).top_stories[0]
    assert card.velocity == pytest.approx(math.log1p(1))
    assert card.score > 0


def test_parodyability_weights_and_empty_hook() -> None:
    held = score_stories([_story("A raccoon sat on a step", outlets=1)], now=NOW)
    assert held.top_stories[0].parodyability == pytest.approx(0.5)
    assert held.top_stories[0].hook == "raccoon"
    obj = score_stories([_story("A donut sat on a plate", outlets=1)], now=NOW)
    assert obj.top_stories[0].parodyability == pytest.approx(0.4)
    assert obj.top_stories[0].category == "object"
    assert obj.top_stories[0].hook == "donut"
    plain = score_stories(
        [_story("Committee publishes quarterly minutes", outlets=1)],
        now=NOW,
    )
    assert plain.top_stories == ()
    assert plain.rejected_stories == ()


def test_plural_animal_and_object_use_the_canonical_hook() -> None:
    batch = score_stories(
        [_story("The raccoons escaped with donuts", outlets=1)],
        now=NOW,
    )
    assert batch.top_stories[0].hook == "raccoon+donut"


def test_safety_zero_rejects_and_zeroes_the_score() -> None:
    batch = score_stories(
        [_story("An earthquake hit overnight", outlets=5)],
        now=NOW,
    )
    assert batch.top_stories == ()
    card = batch.rejected_stories[0]
    assert card.safety_gate == 0
    assert card.score == 0.0
    assert "tragedy_disaster" in card.safety_reasons
    assert card.rank is None


def test_leader_at_forty_buyers_is_not_yet_the_og_penalty() -> None:
    story = _story(
        "A raccoon escaped with a donut",
        outlets=1,
        copycats=(CopycatCoin("Raccoon", "RAC", 40, 1.0, 10.0),),
    )
    card = score_stories([story], now=NOW).top_stories[0]
    assert card.crowding_penalty == pytest.approx(0.5)
    assert card.leader_outside_buyers == 40


def test_top_three_break_ties_by_story_id() -> None:
    titles = [
        "A raccoon escaped with a donut",
        "A goose stole a pretzel",
        "A panda accidentally found a balloon",
        "An otter refused to drop a waffle",
    ]
    stories = [_story(title, outlets=1) for title in titles]
    batch = score_stories(stories, now=NOW)
    assert len(batch.top_stories) == 3
    ranked_titles = [card.title for card in batch.top_stories]
    assert ranked_titles == sorted(titles, key=_sid)[:3]
    assert [card.rank for card in batch.top_stories] == [1, 2, 3]
    omitted = set(titles) - set(ranked_titles)
    assert len(omitted) == 1
    assert batch.rejected_stories == ()
