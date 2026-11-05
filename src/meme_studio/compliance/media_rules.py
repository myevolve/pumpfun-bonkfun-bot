"""Prompt and overlay rules for Grok Imagine metadata."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from meme_studio.compliance.blocklists import (
    brand_blocked,
    franchise_or_artist,
    hate_hit,
    person_blocked,
    tragedy_hit,
)
from meme_studio.compliance.text_rules import ADDRESS

if TYPE_CHECKING:
    from meme_studio.compliance.models import MediaMetadata

_SHORT_VIDEO = frozenset({"tiktok", "shorts", "youtube_shorts"})
_SEXUAL = re.compile(
    r"\b(?:nude|nudes|naked|nsfw|porn|pornography|erotic|sexual|xxx|genital|penis|vagina)\b",
    re.IGNORECASE,
)
_GORE = re.compile(
    r"\b(?:gore|gory|blood|bloody|guts|dismember\w*|decapitat\w*|mutilat\w*|corpse|entrails|viscera)\b",
    re.IGNORECASE,
)
_MINOR = re.compile(
    r"\b(?:minors?|children|child|kids?|toddlers?|infants?|preteens?|underage|"
    r"loli|shota|schoolgirls?|schoolboys?|young girls?|young boys?|teens?|teenagers?)\b",
    re.IGNORECASE,
)
_PERSONISH = re.compile(
    r"\b(?:person|people|man|woman|men|women|face|faces|celebrity|portrait|human)\b",
    re.IGNORECASE,
)
_PHOTOISH = re.compile(
    r"photoreal|photo-real|photo real|realistic photo|real photo",
    re.IGNORECASE,
)


def media_reasons(media: MediaMetadata) -> list[str]:
    """Screen a generated-media prompt and every burned-in overlay."""
    blob = "\n".join((media.prompt, *media.overlays))
    reasons: list[str] = []
    if person_blocked(blob):
        reasons.append("person_blocked")
    if brand_blocked(blob):
        reasons.append("media_brand")
    if franchise_or_artist(blob):
        reasons.append("artist_or_franchise")
    if tragedy_hit(blob):
        reasons.append("tragedy")
    reasons.extend(_likeness_reasons(blob))
    if _SEXUAL.search(blob):
        reasons.append("media_sexual")
    if _GORE.search(blob):
        reasons.append("media_gore")
    if hate_hit(blob):
        reasons.append("media_hate")
    if _MINOR.search(blob):
        reasons.append("media_minor")
    reasons.extend(_overlay_reasons(media))
    return reasons


def _likeness_reasons(prompt: str) -> list[str]:
    lowered = prompt.lower()
    if "deepfake" in lowered or re.search(r"\blikeness\b", lowered):
        return ["media_likeness"]
    if _PHOTOISH.search(prompt) and _PERSONISH.search(prompt):
        return ["media_likeness"]
    if re.search(r"\blooks like\b", lowered):
        return ["media_likeness"]
    return []


def _overlay_reasons(media: MediaMetadata) -> list[str]:
    if media.channel not in _SHORT_VIDEO:
        return []
    blob = " ".join((media.prompt, *media.overlays))
    reasons: list[str] = []
    if re.search(r"\bcoins?\b", blob, re.IGNORECASE):
        reasons.append("media_overlay:coin")
    if "$" in blob:
        reasons.append("media_overlay:dollar")
    if media.ticker and re.search(
        rf"\b{re.escape(media.ticker)}\b", blob, re.IGNORECASE
    ):
        reasons.append("media_overlay:ticker")
    if _address_overlay(media, blob):
        reasons.append("media_overlay:address")
    return reasons


def _address_overlay(media: MediaMetadata, blob: str) -> bool:
    if media.mint and media.mint in blob:
        return True
    if ADDRESS.search(blob):
        return True
    return (
        re.search(r"\bcontract address\b|\bmint address\b", blob, re.IGNORECASE)
        is not None
    )
