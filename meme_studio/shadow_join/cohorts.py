"""Cohort tags from the pre-registered shadow-tracker rules.

Population is SOL-quoted. Control is sha256(mint) % 10 == 0 inside that
population. Profile P requires a news match, a disclosed dev buy of at most
0.25 SOL, a Telegram link, and an X link or a website. P_x drops the
Telegram requirement. P_minus_N keeps the profile socials without the news match.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from meme_studio.ladder.money import dec_str
from meme_studio.shadow_join.schema import (
    Concept,
    StorySummary,
    TrackerCoin,
    is_sol_quote,
)


@dataclass(frozen=True, slots=True)
class TaggedCoin:
    """A coin plus the cohort and story tags the join consumes."""

    coin: TrackerCoin
    cohorts: tuple[str, ...]
    story_ids: tuple[str, ...]
    is_leader: bool

    @property
    def mint(self) -> str:
        """Mint address, unchanged from the tracker."""
        return self.coin.mint


def tag_cohorts(
    coins: tuple[TrackerCoin, ...] | list[TrackerCoin],
    concepts: tuple[Concept, ...] | list[Concept],
) -> list[TaggedCoin]:
    """Tag each coin. Leader is the same-story coin with the most outside buy SOL."""
    stories = _stories_for(coins, concepts)
    leaders = {
        story_id: summary.leader_mint
        for story_id, summary in stories.items()
        if summary.leader_mint is not None
    }
    tagged: list[TaggedCoin] = []
    for coin in coins:
        story_ids = tuple(
            story_id
            for story_id, summary in stories.items()
            if coin.mint in summary.mints
        )
        cohorts = _cohorts(coin, news=bool(story_ids))
        is_leader = any(leaders.get(story_id) == coin.mint for story_id in story_ids)
        tagged.append(
            TaggedCoin(
                coin=coin,
                cohorts=cohorts,
                story_ids=story_ids,
                is_leader=is_leader,
            )
        )
    return tagged


def summarize_stories(
    coins: tuple[TrackerCoin, ...] | list[TrackerCoin],
    concepts: tuple[Concept, ...] | list[Concept],
) -> tuple[StorySummary, ...]:
    """Return one row per concept, including concepts that matched nobody."""
    stories = _stories_for(coins, concepts)
    rows: list[StorySummary] = []
    for concept in concepts:
        summary = stories[concept.story_id]
        rows.append(
            StorySummary(
                story_id=concept.story_id,
                n_coins=len(summary.mints),
                leader_mint=summary.leader_mint,
                winner_share=summary.winner_share,
            )
        )
    return tuple(rows)


def matches_any_concept(
    coin: TrackerCoin,
    concepts: tuple[Concept, ...] | list[Concept],
) -> bool:
    """Return whether the coin matches at least one concept term."""
    if not concepts:
        return True
    return any(_matches_concept(coin, concept) for concept in concepts)


def outside_buy_raw(coin: TrackerCoin) -> int | None:
    """Outside buy SOL in lamports. Unknown creator identity censors the sum."""
    if coin.creator is None:
        return None
    total = 0
    for trade in coin.trades:
        if not trade.direction_known:
            return None
        if trade.is_buy and trade.user != coin.creator:
            total += trade.sol_amount_raw
    return total


@dataclass(frozen=True, slots=True)
class _Story:
    mints: tuple[str, ...]
    leader_mint: str | None
    winner_share: str | None


def _stories_for(
    coins: tuple[TrackerCoin, ...] | list[TrackerCoin],
    concepts: tuple[Concept, ...] | list[Concept],
) -> dict[str, _Story]:
    population = [coin for coin in coins if is_sol_quote(coin.quote_mint)]
    stories: dict[str, _Story] = {}
    for concept in concepts:
        matched = [coin for coin in population if _matches_concept(coin, concept)]
        volumes = {coin.mint: outside_buy_raw(coin) for coin in matched}
        if not matched:
            stories[concept.story_id] = _Story((), None, None)
            continue
        if any(volume is None for volume in volumes.values()):
            stories[concept.story_id] = _Story(tuple(sorted(volumes)), None, None)
            continue
        known = {mint: volume for mint, volume in volumes.items() if volume is not None}
        total = sum(known.values())
        leader = min(matched, key=lambda coin: (-int(known[coin.mint]), coin.mint))
        share = None
        if total > 0:
            share = dec_str(Decimal(known[leader.mint]) / Decimal(total))
        stories[concept.story_id] = _Story(
            tuple(sorted(volumes)),
            leader.mint,
            share,
        )
    return stories


def _matches_concept(coin: TrackerCoin, concept: Concept) -> bool:
    return any(_matches_term(coin, term) for term in concept.terms)


def _matches_term(coin: TrackerCoin, term: str) -> bool:
    if coin.symbol.casefold() == term.casefold():
        return True
    pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    for text in (coin.name, coin.symbol, coin.description or ""):
        if pattern.search(text):
            return True
    return False


def _cohorts(coin: TrackerCoin, *, news: bool) -> tuple[str, ...]:
    if not is_sol_quote(coin.quote_mint):
        return ()
    tags = ["population"]
    small_dev = _small_dev(coin.dev_buy_sol)
    telegram = bool(coin.telegram)
    x_or_site = bool(coin.x or coin.website)
    if news:
        tags.append("N")
    if news and small_dev and telegram and x_or_site:
        tags.append("P")
    if news and small_dev and x_or_site:
        tags.append("P_x")
    if not news and small_dev and telegram and x_or_site:
        tags.append("P_minus_N")
    if _is_control(coin.mint):
        tags.append("C")
    return tuple(tags)


def _small_dev(value: str | None) -> bool:
    if value is None or value == "":
        return False
    amount = Decimal(value)
    return Decimal(0) < amount <= Decimal("0.25")


def _is_control(mint: str) -> bool:
    digest = int(hashlib.sha256(mint.encode()).hexdigest(), 16)
    return digest % 10 == 0
