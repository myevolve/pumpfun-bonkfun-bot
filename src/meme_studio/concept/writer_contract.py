"""Handoff to the human launch writer.

The compliance writer's ``ConceptPackage`` (``meme_studio.drafts.models`` on
the drafts PR) is the shared input. This module fills the fields a shadow
draft actually knows and leaves launch-only facts null. It does not invent a
mint, wallet, dev buy, URL, or plan hash. A null field means UNKNOWN, not an
empty string and not a held coin.

``writer_package.json`` is a list of these objects, one per emitted candidate,
including candidates that failed a collision check. The writer must not post
until a person replaces the null launch facts and the disclosure is still the
unlaunched sentence.
"""

from __future__ import annotations

from meme_studio.concept.schema import (
    SHADOW_DISCLOSURE,
    ConceptCandidate,
    ConceptDocument,
)

# Field names match ConceptPackage in meme_studio.drafts.models.
WRITER_PACKAGE_FIELDS = (
    "format",
    "format_version",
    "lifecycle",
    "story_id",
    "story_one_liner",
    "joke_line",
    "game_title",
    "ticker",
    "character",
    "mint",
    "game_url",
    "plan_url",
    "plan_hash",
    "wallet",
    "dev_buy_sol",
    "studio",
    "steve_handle",
    "hub_domain",
    "plan_text",
    "disclosure",
)
FORMAT_NAME = "meme_studio.drafts.ConceptPackage"
FORMAT_VERSION = "1"
LIFECYCLE = "shadow-unlaunched"


def writer_packages(document: ConceptDocument) -> list[dict[str, object]]:
    """Build one writer package per candidate.

    Args:
        document: The shadow concept document.

    Returns:
        JSON-ready packages. Launch facts are null.
    """
    story_id = story_id_from(document)
    story = _story_line(document, story_id)
    return [_package(story_id, story, candidate) for candidate in document.candidates]


def _package(
    story_id: str,
    story_line: str,
    candidate: ConceptCandidate,
) -> dict[str, object]:
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "lifecycle": LIFECYCLE,
        "story_id": story_id,
        "story_one_liner": story_line,
        "joke_line": candidate.joke,
        "game_title": candidate.name,
        "ticker": candidate.ticker,
        "character": candidate.character,
        "mint": None,
        "game_url": None,
        "plan_url": None,
        "plan_hash": None,
        "wallet": None,
        "dev_buy_sol": None,
        "studio": None,
        "steve_handle": None,
        "hub_domain": None,
        "plan_text": None,
        "disclosure": SHADOW_DISCLOSURE,
    }


def _story_line(document: ConceptDocument, story_id: str) -> str:
    for card in (*document.top_stories, *document.rejected_stories):
        if card.story_id == story_id:
            return card.title
    return ""


def story_id_from(document: ConceptDocument) -> str:
    """Return the id of the story that produced the candidates, or an empty string."""
    return document.selected_story_id or document.candidates_story_id or ""
