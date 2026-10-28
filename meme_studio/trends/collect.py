"""Group headlines into stories and attach same-story coins."""

import re
from collections.abc import Sequence
from datetime import datetime

from meme_studio.concept.inputs import CopycatCoin, CountPoint, OutletHit, StoryInput
from meme_studio.concept.schema import story_id_for
from meme_studio.trends.models import RawItem, TrendSignal

_STOP = frozenset(
    """
    the a an of and to in on for with from after before about into over under
    that this have were they them your their there these those just than then
    when where what which while
    """.split()
)
_MIN_KEYWORD = 4


def build_stories(
    items: Sequence[RawItem],
    *,
    trends: Sequence[TrendSignal] = (),
    now: datetime | None = None,
) -> tuple[StoryInput, ...]:
    """Group items that share a normalized title and attach matching trends.

    Args:
        items: Headlines from any public source.
        trends: X trend names. A trend whose words all appear in a title
            donates its post counts. A trend that matches nothing becomes its
            own story when ``now`` is set.
        now: Clock used as the timestamp for a trend-only story.

    Returns:
        One story per normalized title, sorted by story id.
    """
    rows = list(items)
    rows.extend(_synthetic_trends(rows, trends, now))
    grouped: dict[str, list[RawItem]] = {}
    for item in rows:
        grouped.setdefault(story_id_for(item.title), []).append(item)
    stories = [_story_from(group, trends) for group in grouped.values() if group]
    stories.sort(key=lambda story: story_id_for(story.title))
    return tuple(stories)


def attach_copycats(
    stories: Sequence[StoryInput],
    coins: Sequence[CopycatCoin],
) -> tuple[StoryInput, ...]:
    """Attach coins whose name or ticker contains a title keyword.

    Args:
        stories: Grouped stories.
        coins: Public same-story coins. Matching is case-insensitive.

    Returns:
        Copies of the stories. Keywords shorter than 4 characters and a small
        stop list are ignored.
    """
    attached: list[StoryInput] = []
    for story in stories:
        keys = _keywords(story.title)
        matched = [
            coin
            for coin in coins
            if any(word in f"{coin.name} {coin.ticker}".casefold() for word in keys)
        ]
        matched.sort(key=lambda coin: coin.ticker)
        attached.append(
            StoryInput(
                title=story.title,
                summary=story.summary,
                detected_at=story.detected_at,
                sources=story.sources,
                outlets=story.outlets,
                post_counts=story.post_counts,
                copycats=tuple(matched),
                category=story.category,
            )
        )
    return tuple(attached)


def _synthetic_trends(
    items: Sequence[RawItem],
    trends: Sequence[TrendSignal],
    now: datetime | None,
) -> list[RawItem]:
    if now is None:
        return []
    extra: list[RawItem] = []
    for trend in trends:
        if any(_trend_matches(trend.name, item.title) for item in items):
            continue
        extra.append(
            RawItem(
                source="x",
                title=trend.name,
                summary=trend.name,
                published_at=now,
                outlet="x-trend",
                url="",
            )
        )
    return extra


def _story_from(group: Sequence[RawItem], trends: Sequence[TrendSignal]) -> StoryInput:
    ordered = sorted(
        group, key=lambda item: (item.published_at, item.source, item.title)
    )
    title = ordered[0].title
    titles = [item.title for item in ordered]
    outlets: list[OutletHit] = []
    seen: set[str] = set()
    for item in ordered:
        if item.outlet not in seen:
            seen.add(item.outlet)
            outlets.append(OutletHit(name=item.outlet, published_at=item.published_at))
    sources = tuple(sorted({item.source for item in ordered}))
    summary = next((item.summary for item in ordered if item.summary.strip()), title)
    return StoryInput(
        title=title,
        summary=summary,
        detected_at=ordered[0].published_at,
        sources=sources,
        outlets=tuple(outlets),
        post_counts=_counts_for(titles, trends),
        copycats=(),
    )


def _counts_for(
    titles: Sequence[str],
    trends: Sequence[TrendSignal],
) -> tuple[CountPoint, ...]:
    points: list[CountPoint] = []
    for trend in trends:
        if any(_trend_matches(trend.name, title) for title in titles):
            points.extend(trend.post_counts)
    return tuple(points)


def _trend_matches(trend_name: str, title: str) -> bool:
    folded = title.casefold()
    if trend_name.casefold() in folded:
        return True
    words = re.findall(r"[a-z0-9]{4,}", trend_name.casefold())
    return bool(words) and all(word in folded for word in words)


def _keywords(title: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", title.casefold())
        if len(word) >= _MIN_KEYWORD and word not in _STOP
    }
