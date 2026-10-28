"""V1 story score.

``score = velocity x parodyability x safety_gate x crowding_penalty
x audience_fit x hour_multiplier``

Velocity is the z-score of the latest 30-minute post bucket against the prior
six hours (sample standard deviation), plus ``log1p`` of distinct outlets in
the last 60 minutes. When the baseline has no variance, the z-score is the
raw excess of the recent bucket over the baseline mean, so a flat zero
baseline does not divide by zero or hide a new spike. Negative velocity is
clamped to zero before the outlet term. With no post counts at all, velocity
is only the outlet term.

Hour-of-day weights and audience priors are uniform until a local table is
supplied. Those gaps stay in ``BASE_UNKNOWNS``.
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from meme_studio.concept.hooks import extract
from meme_studio.concept.inputs import CountPoint, StoryInput
from meme_studio.concept.safety import evaluate_safety
from meme_studio.concept.schema import PACIFIC, StoryCard, story_id_for

_RECENT = timedelta(minutes=30)
_BASELINE = timedelta(minutes=390)
_OUTLET_WINDOW = timedelta(minutes=60)
_BIN_COUNT = 12
_BIN_SECONDS = 30 * 60
_OG_BUYERS = 40
_OG_FACTOR = 0.25
_STD_FLOOR = 1e-9
_TOP_N = 3
_NEUTRAL = 1.0


@dataclass(frozen=True)
class ScoredBatch:
    """Top safe stories and the hard rejects from one scoring pass."""

    top_stories: tuple[StoryCard, ...]
    rejected_stories: tuple[StoryCard, ...]


def score_stories(
    stories: Sequence[StoryInput],
    *,
    now: datetime,
    priors: Mapping[str, float] | None = None,
    hour_weights: Mapping[int, float] | None = None,
) -> ScoredBatch:
    """Score stories and keep the top three safe cards with a positive score.

    Args:
        stories: Candidate stories already grouped by the trend stage.
        now: Aware clock used for windows and the Pacific hour.
        priors: Optional category lift. Missing categories use 1.
        hour_weights: Optional Pacific-hour multipliers. Missing hours use 1.

    Returns:
        Ranked safe cards and rejected cards. Safe cards with a zero score
        are omitted from both lists.
    """
    cards = [
        _score_one(story, now=now, priors=priors, hour_weights=hour_weights)
        for story in stories
    ]
    cards.sort(key=lambda card: (-card.score, card.story_id))
    top = [card for card in cards if card.safety_gate == 1 and card.score > 0]
    rejected = [card for card in cards if card.safety_gate == 0]
    ranked = tuple(
        replace(card, rank=index) for index, card in enumerate(top[:_TOP_N], start=1)
    )
    return ScoredBatch(
        top_stories=ranked,
        rejected_stories=tuple(replace(card, rank=None) for card in rejected),
    )


def _score_one(
    story: StoryInput,
    *,
    now: datetime,
    priors: Mapping[str, float] | None,
    hour_weights: Mapping[int, float] | None,
) -> StoryCard:
    text = f"{story.title}\n{story.summary}"
    safety = evaluate_safety(text)
    hook = extract(text)
    category = story.category or hook.category
    velocity, outlets = _velocity(story, now)
    penalty, copycats, leader = _crowding(story)
    fit = _NEUTRAL if not priors else float(priors.get(category, _NEUTRAL))
    hour = _hour_multiplier(now, hour_weights)
    score = velocity * hook.parodyability * safety.gate * penalty * fit * hour
    detected = story.detected_at.astimezone(UTC).isoformat(timespec="seconds")
    return StoryCard(
        story_id=story_id_for(story.title),
        title=story.title,
        summary=story.summary,
        detected_at=detected,
        sources=tuple(sorted(set(story.sources))),
        category=category,
        hook=hook.label,
        velocity=velocity,
        parodyability=hook.parodyability,
        safety_gate=safety.gate,
        safety_reasons=safety.reasons,
        crowding_penalty=penalty,
        audience_fit=fit,
        hour_multiplier=hour,
        score=score,
        copycat_count=copycats,
        leader_outside_buyers=leader,
        outlets=outlets,
        rank=None,
    )


def _velocity(story: StoryInput, now: datetime) -> tuple[float, tuple[str, ...]]:
    now_utc = now.astimezone(UTC)
    window_start = now_utc - _OUTLET_WINDOW
    names: list[str] = []
    seen: set[str] = set()
    for hit in story.outlets:
        if hit.published_at.tzinfo is None:
            continue
        published = hit.published_at.astimezone(UTC)
        if window_start <= published < now_utc:
            key = hit.name.casefold()
            if key not in seen:
                seen.add(key)
                names.append(hit.name)
    outlet_term = math.log1p(len(seen))
    if not story.post_counts:
        return outlet_term, tuple(sorted(names, key=str.casefold))
    excess = _recent_z(story.post_counts, now_utc)
    return max(0.0, excess) + outlet_term, tuple(sorted(names, key=str.casefold))


def _recent_z(counts: Sequence[CountPoint], now_utc: datetime) -> float:
    recent_start = now_utc - _RECENT
    baseline_start = now_utc - _BASELINE
    bins = [0] * _BIN_COUNT
    recent = 0
    for point in counts:
        if point.start.tzinfo is None:
            continue
        start = point.start.astimezone(UTC)
        if recent_start <= start < now_utc:
            recent += point.count
        elif baseline_start <= start < recent_start:
            index = int((start - baseline_start).total_seconds() // _BIN_SECONDS)
            if 0 <= index < _BIN_COUNT:
                bins[index] += point.count
    mean = statistics.mean(bins)
    std = statistics.stdev(bins)
    if std < _STD_FLOOR:
        return float(recent - mean)
    return (recent - mean) / std


def _crowding(story: StoryInput) -> tuple[float, int, int]:
    count = len(story.copycats)
    leader = max((coin.outside_buyers for coin in story.copycats), default=0)
    penalty = 1.0 / (1.0 + count)
    if leader > _OG_BUYERS:
        penalty *= _OG_FACTOR
    return penalty, count, leader


def _hour_multiplier(
    now: datetime,
    weights: Mapping[int, float] | None,
) -> float:
    if not weights:
        return _NEUTRAL
    hour = now.astimezone(PACIFIC).hour
    return float(weights.get(hour, _NEUTRAL))
