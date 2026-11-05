# ruff: noqa: S101, PLR2004
"""Deterministic concept, name, and ticker drafts."""

import math
from datetime import UTC, datetime, timedelta

from meme_studio.concept.assemble import assemble_concept
from meme_studio.concept.collisions import (
    LookupUnavailableError,
    MemoryTokenLookup,
    TokenHit,
)
from meme_studio.concept.generator import PALETTES, draft_candidates, select_concept
from meme_studio.concept.inputs import OutletHit, StoryInput
from meme_studio.concept.schema import BASE_UNKNOWNS, validate_concept
from meme_studio.concept.scorer import score_stories

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
BANNED = (
    "moon",
    "100x",
    "1000x",
    "guaranteed",
    "can't lose",
    "cant lose",
    "easy money",
    "get in early",
    "don't miss",
    "dont miss",
    "price target",
    "financial freedom",
    "passive income",
    "returns",
    "profit",
    "investment",
    "roadmap",
    "utility",
    "staking",
    "buyback",
)
RACCOON_TICKERS = ("RACCDONU", "DONURACC", "CAPRACC", "DONUPARA", "RACCHOUR")
GOOSE_TICKERS = ("GOOSPRET", "PRETGOOS", "CAPGOOS", "PRETPARA", "GOOSHOUR")


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


def test_drafts_five_pinned_raccoon_candidates() -> None:
    drafts = draft_candidates(
        "A raccoon escaped with a donut",
        "A raccoon escaped with a donut",
    )
    assert [(item.name, item.ticker, item.game_template_id) for item in drafts] == [
        ("Raccoon Donut", "RACCDONU", "dodge"),
        ("Donut Raccoon", "DONURACC", "runner"),
        ("Captain Raccoon", "CAPRACC", "flappy"),
        ("Donut Parade", "DONUPARA", "clicker"),
        ("Raccoon Hour", "RACCHOUR", "quiz"),
    ]
    assert drafts[0].joke == "A cartoon raccoon treats a donut like a state secret."
    for draft in drafts:
        assert len(draft.name) <= 32
        assert draft.ticker.isupper()
        assert 3 <= len(draft.ticker) <= 10
        assert len(draft.description.split()) >= 20
        assert "\n" not in draft.joke
        assert "original cartoon" in draft.character
        assert "raccoon" in draft.character.casefold()
        assert "original cartoon" in draft.art_prompt
        assert "in the style of" not in draft.art_prompt.casefold()
        for color in (
            draft.theme_palette.primary,
            draft.theme_palette.secondary,
            draft.theme_palette.accent,
            draft.theme_palette.background,
        ):
            assert len(color) == 7
            assert color.startswith("#")
            int(color[1:], 16)
        blob = " ".join(
            [draft.name, draft.joke, draft.description, draft.art_prompt]
        ).casefold()
        for phrase in BANNED:
            assert phrase not in blob
        assert draft.accepted is False
        assert draft.collision_checks == ()
    again = draft_candidates(
        "A raccoon escaped with a donut",
        "A raccoon escaped with a donut",
    )
    assert again == drafts


def test_palette_is_chosen_from_the_ticker() -> None:
    draft = draft_candidates("A raccoon escaped with a donut", "")[0]
    index = sum(ord(char) for char in draft.ticker) % len(PALETTES)
    assert draft.theme_palette == PALETTES[index]


def test_no_comic_hook_yields_no_drafts() -> None:
    assert draft_candidates("Committee publishes quarterly minutes", "") == ()


def test_selection_skips_a_fully_colliding_story() -> None:
    batch = score_stories(
        [
            _story("A raccoon escaped with a donut", 3),
            _story("A goose stole a pretzel", 1),
        ],
        now=NOW,
    )
    hits = [TokenHit(ticker, ticker, 1.0, 0.0, "solana") for ticker in RACCOON_TICKERS]
    chosen, candidates, reason = select_concept(
        batch.top_stories,
        lookup=MemoryTokenLookup(hits),
        history=(),
    )
    assert reason is None
    assert chosen is not None
    assert chosen.title == "A goose stole a pretzel"
    assert candidates[0].ticker == "GOOSPRET"
    assert any(item.accepted for item in candidates)
    assert math.log1p(3) > math.log1p(1)


def test_all_collisions_skip_the_day() -> None:
    batch = score_stories(
        [
            _story("A raccoon escaped with a donut", 3),
            _story("A goose stole a pretzel", 1),
        ],
        now=NOW,
    )
    hits = [
        TokenHit(ticker, ticker, 1.0, 0.0, "solana")
        for ticker in RACCOON_TICKERS + GOOSE_TICKERS
    ]
    chosen, candidates, reason = select_concept(
        batch.top_stories,
        lookup=MemoryTokenLookup(hits),
        history=(),
    )
    assert chosen is None
    assert reason == "all_candidates_collided"
    assert candidates[0].ticker == "RACCDONU"
    assert all(not item.accepted for item in candidates)


def test_lookup_outage_fails_closed_on_the_first_story() -> None:
    batch = score_stories(
        [_story("A raccoon escaped with a donut", 1)],
        now=NOW,
    )

    class Down:
        def search(self, _query: str) -> list[TokenHit]:
            raise LookupUnavailableError("down")

    chosen, candidates, reason = select_concept(
        batch.top_stories,
        lookup=Down(),
        history=(),
    )
    assert chosen is None
    assert reason == "lookup_unavailable"
    assert candidates[0].ticker == "RACCDONU"
    assert candidates[0].collision_checks[0].reason == "lookup_unavailable"


def test_assemble_skips_when_every_story_is_unsafe() -> None:
    batch = score_stories(
        [_story("An earthquake hit overnight", 4)],
        now=NOW,
    )
    document = assemble_concept(
        batch,
        lookup=MemoryTokenLookup([]),
        history=(),
        now=NOW,
        unknowns=BASE_UNKNOWNS,
    )
    validate_concept(document)
    assert document.status == "skipped"
    assert document.skipped_reason == "no_safe_story"
    assert document.selected_ticker is None
    assert document.candidates == ()
    assert document.publish is False
    assert document.transactions is False
    assert document.rejected_stories[0].safety_gate == 0


def test_assemble_draft_validates_and_uses_pacific_date() -> None:
    batch = score_stories(
        [_story("A raccoon escaped with a donut", 1)],
        now=NOW,
    )
    document = assemble_concept(
        batch,
        lookup=MemoryTokenLookup([]),
        history=(),
        now=NOW,
        unknowns=BASE_UNKNOWNS,
    )
    validate_concept(document)
    assert document.status == "draft"
    assert document.selected_ticker == "RACCDONU"
    assert document.date == "2026-10-28"
    assert document.phase == "0"
    assert document.mode == "shadow-dry-run"
    assert document.network_writes == "none"
    assert set(BASE_UNKNOWNS) <= set(document.unknowns)
