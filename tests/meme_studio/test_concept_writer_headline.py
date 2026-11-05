# ruff: noqa: S101
"""The writer handoff headline belongs to the selected story."""

from datetime import UTC, datetime, timedelta

from meme_studio.concept.assemble import assemble_concept
from meme_studio.concept.collisions import (
    LookupUnavailableError,
    MemoryTokenLookup,
    TokenHit,
    TokenLookup,
)
from meme_studio.concept.generator import select_concept
from meme_studio.concept.inputs import OutletHit, StoryInput
from meme_studio.concept.scorer import score_stories
from meme_studio.concept.writer_contract import writer_packages

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
RACCOON_TICKERS = ("RACCDONU", "DONURACC", "CAPRACC", "DONUPARA", "RACCHOUR")


def _story(title: str, outlets: int) -> StoryInput:
    hits = tuple(
        OutletHit(name=f"Outlet {index}", published_at=NOW - timedelta(minutes=5))
        for index in range(outlets)
    )
    return StoryInput(
        title=title,
        summary=title,
        detected_at=NOW - timedelta(minutes=15),
        sources=("google_news",),
        outlets=hits,
        post_counts=(),
        copycats=(),
    )


def test_second_story_selection_exports_its_own_headline() -> None:
    batch = score_stories(
        [
            _story("A raccoon escaped with a donut", 3),
            _story("A goose stole a pretzel", 1),
        ],
        now=NOW,
    )
    hits = [TokenHit(ticker, ticker, 1.0, 0.0, "solana") for ticker in RACCOON_TICKERS]
    document = assemble_concept(
        batch, lookup=MemoryTokenLookup(hits), history=(), now=NOW, unknowns=()
    )
    assert document.top_stories[0].title == "A raccoon escaped with a donut"
    packages = writer_packages(document)
    assert packages[0]["ticker"] == "GOOSPRET"
    assert {package["story_id"] for package in packages} == {
        document.selected_story_id
    }
    assert {package["story_one_liner"] for package in packages} == {
        "A goose stole a pretzel"
    }


class _GooseOutage(TokenLookup):
    """Raccoon tickers all collide; any goose or pretzel query is an outage."""

    def search(self, query: str) -> list[TokenHit]:
        folded = query.casefold()
        if "goos" in folded or "pret" in folded:
            raise LookupUnavailableError(query)
        return [TokenHit(ticker, ticker, 1.0, 0.0, "solana") for ticker in RACCOON_TICKERS]


def test_second_story_outage_exports_the_producing_story() -> None:
    batch = score_stories(
        [
            _story("A raccoon escaped with a donut", 3),
            _story("A goose stole a pretzel", 1),
        ],
        now=NOW,
    )
    document = assemble_concept(
        batch, lookup=_GooseOutage(), history=(), now=NOW, unknowns=()
    )
    assert document.skipped_reason == "lookup_unavailable"
    assert document.candidates[0].ticker == "GOOSPRET"
    goose = document.top_stories[1]
    assert goose.title == "A goose stole a pretzel"
    packages = writer_packages(document)
    assert {package["story_id"] for package in packages} == {goose.story_id}
    assert {package["story_one_liner"] for package in packages} == {goose.title}


class _OutageAfter(TokenLookup):
    """Raccoon tickers collide for ``budget`` calls, then the lookup goes down."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.calls = 0

    def search(self, query: str) -> list[TokenHit]:
        self.calls += 1
        if self.calls > self.budget:
            raise LookupUnavailableError(query)
        return [TokenHit(ticker, ticker, 1.0, 0.0, "solana") for ticker in RACCOON_TICKERS]


def test_shared_hook_outage_exports_the_story_that_was_checked() -> None:
    batch = score_stories(
        [
            _story("A raccoon escaped with a donut", 3),
            _story("A raccoon stole a donut at the fair", 1),
        ],
        now=NOW,
    )
    first, second = batch.top_stories
    counter = _OutageAfter(budget=10**6)
    select_concept((first,), lookup=counter, history=())
    document = assemble_concept(
        batch,
        lookup=_OutageAfter(budget=counter.calls),
        history=(),
        now=NOW,
        unknowns=(),
    )
    assert document.skipped_reason == "lookup_unavailable"
    assert document.candidates[0].ticker == "RACCDONU"
    packages = writer_packages(document)
    assert {package["story_id"] for package in packages} == {second.story_id}
    assert {package["story_one_liner"] for package in packages} == {second.title}
