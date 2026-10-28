"""Text guardrails for prompts and generated-asset metadata.

Provocative humor is allowed. Photorealistic likenesses of real people,
sexual content, gore, hate, minors, and real brand logos are not.
"""
# ruff: noqa: TC003, TRY003

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meme_studio.game.concept import Concept

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"photoreal|\breal person\b|\blikeness\b|\bcelebrity\b|\bdeepfake\b",
            re.IGNORECASE,
        ),
        "likeness",
    ),
    (
        re.compile(r"\b(nude|naked|nsfw|porn|erotic|sexual)\b", re.IGNORECASE),
        "sexual",
    ),
    (
        re.compile(r"\b(gore|blood|dismember|mutilat)", re.IGNORECASE),
        "gore",
    ),
    (
        re.compile(r"\b(nazi|swastika|white power|kkk)\b", re.IGNORECASE),
        "hate",
    ),
    (
        re.compile(
            r"\b(child|children|kid|kids|minor|minors|underage|teen|teens|"
            r"teenager|loli|shota|toddler|infant|baby)\b",
            re.IGNORECASE,
        ),
        "minors",
    ),
    (
        re.compile(
            r"\b(nike|disney|marvel|pokemon|nintendo|mcdonald|coca-cola|"
            r"cocacola|starbucks|iphone|playstation|xbox|batman|pikachu)\b|"
            r"mickey mouse",
            re.IGNORECASE,
        ),
        "brand",
    ),
)


class GuardrailError(ValueError):
    """A prompt or asset failed the parody guardrails."""


def assert_safe_text(text: str, *, field: str) -> None:
    """Raise if ``text`` asks for disallowed content.

    Args:
        text: Concept field or creative prompt.
        field: Name used in the error.

    Raises:
        GuardrailError: A blocked pattern matched.
    """
    for pattern, label in _RULES:
        if pattern.search(text):
            raise GuardrailError(f"{field}: blocked {label} content")


def assert_concept_safe(concept: Concept) -> None:
    """Raise if any concept field is unsafe to draw or send.

    Args:
        concept: Validated concept package.

    Raises:
        GuardrailError: A field matched a blocked pattern.
    """
    fields = {
        "name": concept.name,
        "title": concept.title,
        "ticker": concept.ticker,
        "character": " ".join(
            (concept.character.name, concept.character.kind, concept.character.detail)
        ),
        "joke": concept.joke,
        "description": concept.description,
        "style": concept.style,
    }
    for field, value in fields.items():
        assert_safe_text(value, field=field)


def post_check_asset(
    meta: dict[str, object],
    *,
    hook: Callable[[dict[str, object]], None] | None = None,
) -> None:
    """Run the post-generation check on prompt metadata.

    Args:
        meta: Provider metadata. ``respect_moderation`` false fails closed.
        hook: Optional extra check. Raising ``GuardrailError`` rejects the asset.

    Raises:
        GuardrailError: The asset must be discarded.
    """
    creative = str(meta.get("creative_prompt") or "")
    revised = str(meta.get("revised_prompt") or "")
    if creative:
        assert_safe_text(creative, field="creative_prompt")
    if revised:
        assert_safe_text(revised, field="revised_prompt")
    if meta.get("respect_moderation") is False:
        raise GuardrailError("moderation rejected the asset")
    if hook is not None:
        hook(meta)
