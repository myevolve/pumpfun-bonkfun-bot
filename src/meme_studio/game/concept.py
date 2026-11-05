"""Validated ``concept.json`` for one parody game.

Two shapes load. A flat game concept (``name``, ``template_id``, ``palette``)
follows the stage-3 fields in the pipeline design. The trends pipeline's
``concept.json`` (``meme_studio.concept.schema``) is a story document with
``candidates``; the selected accepted candidate is mapped onto the game fields.
"""
# ruff: noqa: TC003, TRY003

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from meme_studio.concept.schema import (
    ConceptSchemaError,
    concept_from_json,
    validate_concept,
)

_TEMPLATE_ALIASES = {
    "endless_runner": "endless_runner",
    "endless-runner": "endless_runner",
    "runner": "endless_runner",
    # ponytail: trends "flappy" plays on the one-button runner; "quiz" has no
    # mechanic and fails closed. Add real flappy/quiz mechanics to split them.
    "flappy": "endless_runner",
    "tap_clicker": "tap_clicker",
    "tap-clicker": "tap_clicker",
    "clicker": "tap_clicker",
    "dodge_catch": "dodge_catch",
    "dodge-catch": "dodge_catch",
    "dodge": "dodge_catch",
}
_STYLES = ("absurdist", "neon_chaos", "retro_arcade", "comic_book")
_PALETTE_KEYS = ("background", "primary", "secondary", "accent", "ink")
_FORBIDDEN_KEYS = {
    "private_key",
    "secret_key",
    "seed_phrase",
    "mnemonic",
    "solana_private_key",
    "wallet_private_key",
    "api_key",
    "env_file",
}
_NAME_MAX = 32
_MIN_WORDS = 20
_TICKER_RE = re.compile(r"[A-Z0-9]{3,10}")
_HEX_RE = re.compile(r"#[0-9a-f]{6}")
_STORY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_CONTRACT_RE = re.compile(r"[A-Za-z0-9_]{8,64}")


class ConceptError(ValueError):
    """A concept package failed validation."""


@dataclass(frozen=True)
class Character:
    """Invented character. Never a real person."""

    name: str
    kind: str
    detail: str


@dataclass(frozen=True)
class Palette:
    """Five hex colors used by the game and the procedural fallback."""

    background: str
    primary: str
    secondary: str
    accent: str
    ink: str


@dataclass(frozen=True)
class Concept:
    """One daily parody concept."""

    name: str
    title: str
    ticker: str
    character: Character
    joke: str
    description: str
    template_id: str
    palette: Palette
    story_id: str
    date: str
    style: str
    contract_address: str | None = None
    pump_url: str | None = None


def load_concept(path: Path) -> Concept:
    """Load and validate a concept file.

    Args:
        path: Path to ``concept.json``.

    Returns:
        The validated concept.

    Raises:
        ConceptError: The file is missing or fails a schema check.
    """
    if not path.is_file():
        raise ConceptError(f"path: concept file not found: {path.name}")
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConceptError("path: concept file is not json") from exc
    if not isinstance(raw, dict):
        raise ConceptError("path: concept file must be a json object")
    _reject_secrets(raw)
    if "candidates" in raw:
        return _concept_from_mapping(_game_fields_from_trends(text))
    return _concept_from_mapping(raw)


def _game_fields_from_trends(text: str) -> dict[str, object]:
    """Map the trends draft's selected accepted candidate onto game fields."""
    try:
        document = concept_from_json(text)
        validate_concept(document)
    except ConceptSchemaError as exc:
        raise ConceptError(f"concept: {exc}") from exc
    if document.status != "draft":
        raise ConceptError(
            f"status: trends concept was skipped ({document.skipped_reason})"
        )
    # validate_concept guarantees one accepted candidate has the selected ticker.
    candidate = next(
        item
        for item in document.candidates
        if item.accepted and item.ticker == document.selected_ticker
    )
    colors = candidate.theme_palette
    return {
        "name": candidate.name,
        "ticker": candidate.ticker,
        "character": {
            "name": candidate.name,
            "kind": "original cartoon character",
            "detail": candidate.character,
        },
        "joke": candidate.joke,
        "description": candidate.description,
        "template_id": candidate.game_template_id,
        "palette": {
            "background": colors.background,
            "primary": colors.primary,
            "secondary": colors.secondary,
            "accent": colors.accent,
            # Trends palettes have four colors; secondary is the light one
            # that reads on the dark background, so it doubles as text ink.
            "ink": colors.secondary,
        },
        "story_id": document.selected_story_id,
        "date": document.date,
    }


def _reject_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise ConceptError(f"{key}: secret fields are not allowed")
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)


def _concept_from_mapping(raw: dict[str, object]) -> Concept:
    name = _required_text(raw, "name")
    if len(name) > _NAME_MAX:
        raise ConceptError("name: must be 32 characters or fewer")
    title = _optional_text(raw, "title") or name
    if len(title) > _NAME_MAX:
        raise ConceptError("title: must be 32 characters or fewer")
    ticker = _required_text(raw, "ticker").upper()
    if _TICKER_RE.fullmatch(ticker) is None:
        raise ConceptError("ticker: must be 3-10 letters or digits")
    description = _required_text(raw, "description")
    if len(description.split()) < _MIN_WORDS:
        raise ConceptError("description: need at least 20 words")
    template_raw = _required_text(raw, "template_id").lower()
    template_id = _TEMPLATE_ALIASES.get(template_raw)
    if template_id is None:
        raise ConceptError("template_id: unknown template")
    style = (_optional_text(raw, "style") or "absurdist").lower()
    if style not in _STYLES:
        raise ConceptError("style: unknown style preset")
    story_id = _required_text(raw, "story_id")
    if _STORY_RE.fullmatch(story_id) is None:
        raise ConceptError("story_id: expected a lowercase slug")
    date = _required_text(raw, "date")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ConceptError("date: expected YYYY-MM-DD") from None
    return Concept(
        name=name,
        title=title,
        ticker=ticker,
        character=_character(raw.get("character")),
        joke=_required_text(raw, "joke"),
        description=description,
        template_id=template_id,
        palette=_palette(raw.get("palette")),
        story_id=story_id,
        date=date,
        style=style,
        contract_address=_contract(raw),
        pump_url=_pump_url(raw.get("pump_url")),
    )


def _required_text(raw: dict[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConceptError(f"{field}: required")
    return value.strip()


def _optional_text(raw: dict[str, object], field: str) -> str:
    value = raw.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConceptError(f"{field}: expected a string")
    return value.strip()


def _character(value: object) -> Character:
    if isinstance(value, str) and value.strip():
        return Character(name=value.strip(), kind="original creature", detail="")
    if isinstance(value, dict):
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConceptError("character: name is required")
        kind = (
            value.get("kind")
            if isinstance(value.get("kind"), str)
            else "original creature"
        )
        detail = value.get("detail") if isinstance(value.get("detail"), str) else ""
        return Character(
            name=name.strip(), kind=str(kind).strip(), detail=str(detail).strip()
        )
    raise ConceptError("character: expected a name or an object")


def _palette(value: object) -> Palette:
    if not isinstance(value, dict):
        raise ConceptError("palette: expected an object")
    colors: dict[str, str] = {}
    for key in _PALETTE_KEYS:
        raw = value.get(key, "")
        text = str(raw).strip().lower()
        if _HEX_RE.fullmatch(text) is None:
            raise ConceptError(f"{key}: expected #rrggbb")
        colors[key] = text
    return Palette(
        background=colors["background"],
        primary=colors["primary"],
        secondary=colors["secondary"],
        accent=colors["accent"],
        ink=colors["ink"],
    )


def _contract(raw: dict[str, object]) -> str | None:
    value = raw.get("contract_address")
    if not isinstance(value, str) or not value.strip():
        value = raw.get("mint")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _CONTRACT_RE.fullmatch(text) is None:
        raise ConceptError("contract_address: invalid")
    return text


def _pump_url(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ConceptError("pump_url: expected an https url")
    return value
