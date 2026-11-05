"""Turn a concept into a high-energy Imagine prompt.

Safety instructions are appended after the creative text. The guardrail
scanner checks the concept and the creative portion, not the fixed suffix.
"""
# ruff: noqa: TC001

from __future__ import annotations

from meme_studio.art.guardrails import assert_concept_safe
from meme_studio.game.concept import Concept

_STYLE_LINES = {
    "absurdist": "Absurdist humor, illogical scale, deadpan chaos",
    "neon_chaos": "Neon chaos, electric color, glowing edges",
    "retro_arcade": "Retro arcade cabinet art, chunky pixels, CRT glow",
    "comic_book": "Comic book panels, halftone dots, thick ink",
}
_KIND_LINES = {
    "sprite": "Character sprite, full body, centered, readable at small size",
    "background": "Scrolling game background, no characters in the foreground",
    "share": "Square share card, huge character, bold empty space for a title",
    "logo": "Square emblem, one creature, simple silhouette",
    "banner": "Wide banner, creature running through the scene",
}
_SUFFIX = (
    "Exaggerated high-energy parody. Original fictional cartoon creature. "
    "Not a photograph of any individual. No logos. No trademarks. "
    "Clothing stays on. No injuries. No slurs. No readable ticker text."
)


def build_image_prompt(concept: Concept, *, kind: str, game_only: bool) -> str:
    """Build an image prompt for ``kind``.

    Args:
        concept: Validated concept.
        kind: ``sprite``, ``background``, ``share``, ``logo``, or ``banner``.
        game_only: When true, the share card omits the ticker.

    Returns:
        The prompt to send.

    Raises:
        GuardrailError: The concept text is not safe to send.
    """
    assert_concept_safe(concept)
    creative = _creative(concept, kind=kind, game_only=game_only, video=False)
    return f"{creative} {_SUFFIX}"


def build_video_prompt(concept: Concept, *, game_only: bool) -> str:
    """Build a short gameplay-teaser prompt with no ticker and no coin talk.

    Args:
        concept: Validated concept.
        game_only: Accepted for call-site symmetry. The teaser never names a ticker.

    Returns:
        The video prompt.

    Raises:
        GuardrailError: The concept text is not safe to send.
    """
    del game_only
    assert_concept_safe(concept)
    creative = _creative(concept, kind="sprite", game_only=True, video=True)
    return f"{creative} {_SUFFIX}"


def creative_portion(
    concept: Concept, *, kind: str, game_only: bool, video: bool
) -> str:
    """Return the creative half of a prompt, without the safety suffix.

    Args:
        concept: Validated concept.
        kind: Asset kind.
        game_only: Omit the ticker on share cards.
        video: Phrase the prompt as a gameplay teaser.

    Returns:
        Creative prompt text.
    """
    return _creative(concept, kind=kind, game_only=game_only, video=video)


def _creative(concept: Concept, *, kind: str, game_only: bool, video: bool) -> str:
    character = concept.character
    style_line = _STYLE_LINES[concept.style]
    if video:
        lead = "Gameplay teaser, short animated shot, camera follows the action"
    else:
        lead = _KIND_LINES.get(kind, "Illustration")
    parts = [
        lead,
        style_line,
        f"of {character.name}, a {character.kind}.",
        character.detail,
        concept.joke,
        f"Title mood: {concept.title}.",
        "Exaggerated proportions. High-energy.",
    ]
    if kind == "share" and not game_only and not video:
        parts.append(f"Lettering may show {concept.ticker}.")
    return " ".join(part.strip() for part in parts if part.strip())
