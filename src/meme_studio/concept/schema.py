"""concept.json for one shadow day.

The document is a draft. ``publish`` and ``transactions`` stay false, and
``phase`` stays ``0``. Downstream stages read this file; they do not post it.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from meme_studio.concept.safety import contains_hate, find_persons

SCHEMA_VERSION = "1"
PHASE = "0"
MODE = "shadow-dry-run"
NETWORK_WRITES = "none"
NAME_MAX = 32
TICKER_MIN = 3
TICKER_MAX = 10
DESCRIPTION_MIN_WORDS = 20
STORY_ID_LENGTH = 12
PACIFIC = ZoneInfo("America/Los_Angeles")
GAME_TEMPLATE_IDS = ("dodge", "runner", "flappy", "clicker", "quiz")
SKIP_REASONS = (
    "no_safe_story",
    "all_candidates_collided",
    "lookup_unavailable",
    "no_sources",
    "coverage_unknown",
)
# Truthful for an unlaunched shadow draft. It does not claim a coin exists.
SHADOW_DISCLOSURE = (
    "Shadow draft: no token has been created and none is held. "
    "A person must disclose, before any launch, that we created the coin "
    "and that we hold it."
)
_DISCLOSURE_MARKERS = (
    "no token has been created",
    "none is held",
    "we created the coin",
    "we hold it",
    "a person must disclose",
)
BANNED_PHRASES = (
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
BASE_UNKNOWNS = (
    "reddit_data_api_commercial_terms",
    "uspto_bulk_query_permitted",
    "google_trends_public_api",
    "hour_of_day_weights_unverified",
    "audience_fit_priors_empty",
    "rule_based_person_detector_is_not_a_trained_ner",
    "x_trends_http_shape_unverified_until_live_read",
    "dexscreener_volume_field_is_h24_not_7d",
)
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{2,9}$")
_HEX_COLOR = re.compile(r"^#[0-9A-F]{6}$")
_STORY_ID = re.compile(r"^[0-9a-f]{12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOP_KEYS = frozenset(
    {
        "schema_version",
        "generated_at",
        "phase",
        "mode",
        "date",
        "status",
        "skipped_reason",
        "top_stories",
        "rejected_stories",
        "selected_story_id",
        "candidates",
        "selected_ticker",
        "unknowns",
        "publish",
        "transactions",
        "network_writes",
    }
)
_STORY_KEYS = frozenset(
    {
        "story_id",
        "title",
        "summary",
        "detected_at",
        "sources",
        "category",
        "hook",
        "velocity",
        "parodyability",
        "safety_gate",
        "safety_reasons",
        "crowding_penalty",
        "audience_fit",
        "hour_multiplier",
        "score",
        "copycat_count",
        "leader_outside_buyers",
        "outlets",
        "rank",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "name",
        "ticker",
        "character",
        "joke",
        "description",
        "game_template_id",
        "theme_palette",
        "art_prompt",
        "collision_checks",
        "accepted",
    }
)
_PALETTE_KEYS = frozenset({"primary", "secondary", "accent", "background"})
_CHECK_KEYS = frozenset({"name", "passed", "reason"})
_CHECK_NAMES = (
    "existing_tokens",
    "trademark",
    "real_person",
    "own_history",
)


class ConceptSchemaError(ValueError):
    """A concept document broke the phase-0 contract."""


@dataclass(frozen=True)
class ThemePalette:
    """Four hex colors for a game template."""

    primary: str
    secondary: str
    accent: str
    background: str

    def to_dict(self) -> dict[str, str]:
        """Return the JSON object for this palette."""
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "accent": self.accent,
            "background": self.background,
        }


@dataclass(frozen=True)
class CollisionCheck:
    """One automated collision result. FAIL drops the candidate."""

    name: str
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object for this check."""
        return {"name": self.name, "passed": self.passed, "reason": self.reason}


@dataclass(frozen=True)
class ConceptCandidate:
    """One name, ticker, and joke. ``accepted`` is true only after checks."""

    name: str
    ticker: str
    character: str
    joke: str
    description: str
    game_template_id: str
    theme_palette: ThemePalette
    art_prompt: str
    collision_checks: tuple[CollisionCheck, ...]
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object for this candidate."""
        return {
            "name": self.name,
            "ticker": self.ticker,
            "character": self.character,
            "joke": self.joke,
            "description": self.description,
            "game_template_id": self.game_template_id,
            "theme_palette": self.theme_palette.to_dict(),
            "art_prompt": self.art_prompt,
            "collision_checks": [check.to_dict() for check in self.collision_checks],
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class StoryCard:
    """One scored story. Rejected cards keep their reasons and a zero score."""

    story_id: str
    title: str
    summary: str
    detected_at: str
    sources: tuple[str, ...]
    category: str
    hook: str
    velocity: float | None
    parodyability: float
    safety_gate: int
    safety_reasons: tuple[str, ...]
    crowding_penalty: float | None
    audience_fit: float
    hour_multiplier: float
    score: float | None
    copycat_count: int | None
    leader_outside_buyers: int | None
    outlets: tuple[str, ...]
    rank: int | None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object for this card."""
        return {
            "story_id": self.story_id,
            "title": self.title,
            "summary": self.summary,
            "detected_at": self.detected_at,
            "sources": list(self.sources),
            "category": self.category,
            "hook": self.hook,
            "velocity": self.velocity,
            "parodyability": self.parodyability,
            "safety_gate": self.safety_gate,
            "safety_reasons": list(self.safety_reasons),
            "crowding_penalty": self.crowding_penalty,
            "audience_fit": self.audience_fit,
            "hour_multiplier": self.hour_multiplier,
            "score": self.score,
            "copycat_count": self.copycat_count,
            "leader_outside_buyers": self.leader_outside_buyers,
            "outlets": list(self.outlets),
            "rank": self.rank,
        }


@dataclass(frozen=True)
class ConceptDocument:
    """The daily draft written to ``concept.json``."""

    schema_version: str
    generated_at: str
    phase: str
    mode: str
    date: str
    status: str
    skipped_reason: str | None
    top_stories: tuple[StoryCard, ...]
    rejected_stories: tuple[StoryCard, ...]
    selected_story_id: str | None
    candidates: tuple[ConceptCandidate, ...]
    selected_ticker: str | None
    unknowns: tuple[str, ...]
    publish: bool
    transactions: bool
    network_writes: str

    def to_dict(self) -> dict[str, object]:
        """Return the JSON object for the whole document."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "phase": self.phase,
            "mode": self.mode,
            "date": self.date,
            "status": self.status,
            "skipped_reason": self.skipped_reason,
            "top_stories": [card.to_dict() for card in self.top_stories],
            "rejected_stories": [card.to_dict() for card in self.rejected_stories],
            "selected_story_id": self.selected_story_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "selected_ticker": self.selected_ticker,
            "unknowns": list(self.unknowns),
            "publish": self.publish,
            "transactions": self.transactions,
            "network_writes": self.network_writes,
        }


def story_id_for(title: str) -> str:
    """Return the stable 12-hex id for a headline.

    Args:
        title: Raw headline. Case and extra whitespace do not change the id.

    Returns:
        First 12 hex characters of the SHA-256 of the normalized title.
    """
    normalized = " ".join(title.casefold().split())
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return digest[:STORY_ID_LENGTH]


def studio_date(moment: datetime) -> str:
    """Return the Pacific calendar date for a timezone-aware moment.

    Args:
        moment: Aware datetime. Naive values are rejected.

    Returns:
        ``YYYY-MM-DD`` in America/Los_Angeles.

    Raises:
        ConceptSchemaError: When ``moment`` has no timezone.
    """
    if moment.tzinfo is None:
        message = "datetime must be timezone-aware"
        raise ConceptSchemaError(message)
    return moment.astimezone(PACIFIC).date().isoformat()


def has_shadow_disclosure(text: str) -> bool:
    """Return whether text carries the unlaunched-draft disclosure.

    Args:
        text: Candidate description.

    Returns:
        True when every required disclosure marker is present. The markers say
        no token exists yet and name the human disclosure a later launch needs.
    """
    folded = text.casefold()
    return all(marker in folded for marker in _DISCLOSURE_MARKERS)


def dump_concept(document: ConceptDocument) -> str:
    """Serialize a document with stable key order and a trailing newline.

    Args:
        document: Draft to write.

    Returns:
        UTF-8 JSON text. Non-finite numbers are rejected first.

    Raises:
        ConceptSchemaError: When the document is not a valid finite draft.
        ValueError: When serialization would emit a non-standard number.
    """
    validate_concept(document)
    payload = json.dumps(
        document.to_dict(),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    return payload + "\n"


def concept_from_json(raw: str) -> ConceptDocument:
    """Parse concept JSON. Unknown keys fail closed.

    Args:
        raw: File contents.

    Returns:
        The document. Call ``validate_concept`` before treating it as a draft.

    Raises:
        ConceptSchemaError: When the JSON shape does not match the schema.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        message = "concept json is not valid"
        raise ConceptSchemaError(message) from exc
    if not isinstance(payload, dict):
        message = "concept json must be an object"
        raise ConceptSchemaError(message)
    _exact_keys(payload, _TOP_KEYS)
    return ConceptDocument(
        schema_version=_as_str(payload, "schema_version"),
        generated_at=_as_str(payload, "generated_at"),
        phase=_as_str(payload, "phase"),
        mode=_as_str(payload, "mode"),
        date=_as_str(payload, "date"),
        status=_as_str(payload, "status"),
        skipped_reason=_as_optional_str(payload, "skipped_reason"),
        top_stories=tuple(
            _story_from(item) for item in _as_list(payload, "top_stories")
        ),
        rejected_stories=tuple(
            _story_from(item) for item in _as_list(payload, "rejected_stories")
        ),
        selected_story_id=_as_optional_str(payload, "selected_story_id"),
        candidates=tuple(
            _candidate_from(item) for item in _as_list(payload, "candidates")
        ),
        selected_ticker=_as_optional_str(payload, "selected_ticker"),
        unknowns=tuple(_as_str_list(payload, "unknowns")),
        publish=_as_bool(payload, "publish"),
        transactions=_as_bool(payload, "transactions"),
        network_writes=_as_str(payload, "network_writes"),
    )


def validate_concept(document: ConceptDocument) -> None:
    """Reject anything that is not a phase-0 shadow draft.

    Args:
        document: Parsed or assembled document.

    Raises:
        ConceptSchemaError: On the first contract break.
    """
    _validate_phase(document)
    _validate_status(document)
    for card in document.top_stories:
        _validate_story(card, rejected=False)
    for card in document.rejected_stories:
        _validate_story(card, rejected=True)
    for candidate in document.candidates:
        _validate_candidate(candidate)
    if document.status == "draft":
        _validate_selection(document)


def _validate_phase(document: ConceptDocument) -> None:
    if document.schema_version != SCHEMA_VERSION:
        message = "schema_version must be 1"
        raise ConceptSchemaError(message)
    if document.phase != PHASE:
        message = "phase must be 0"
        raise ConceptSchemaError(message)
    if document.mode != MODE:
        message = "mode must be shadow-dry-run"
        raise ConceptSchemaError(message)
    if document.publish:
        message = "phase 0 cannot publish"
        raise ConceptSchemaError(message)
    if document.transactions:
        message = "phase 0 cannot transact"
        raise ConceptSchemaError(message)
    if document.network_writes != NETWORK_WRITES:
        message = "network_writes must be none"
        raise ConceptSchemaError(message)
    if not _DATE.fullmatch(document.date):
        message = "date must be YYYY-MM-DD"
        raise ConceptSchemaError(message)
    if set(BASE_UNKNOWNS) - set(document.unknowns):
        message = "base unknowns are missing"
        raise ConceptSchemaError(message)


def _validate_status(document: ConceptDocument) -> None:
    if document.status not in {"draft", "skipped"}:
        message = "status must be draft or skipped"
        raise ConceptSchemaError(message)
    if document.status == "draft":
        if document.skipped_reason is not None:
            message = "a draft has no skip reason"
            raise ConceptSchemaError(message)
        if document.selected_ticker is None or document.selected_story_id is None:
            message = "a draft needs a selected story and ticker"
            raise ConceptSchemaError(message)
        return
    if document.selected_ticker is not None or document.selected_story_id is not None:
        message = "a skipped draft has no selection"
        raise ConceptSchemaError(message)
    if document.skipped_reason not in SKIP_REASONS:
        message = "skipped_reason is not a known token"
        raise ConceptSchemaError(message)


def _validate_selection(document: ConceptDocument) -> None:
    story_ids = {card.story_id for card in document.top_stories}
    if document.selected_story_id not in story_ids:
        message = "selected story is not in top_stories"
        raise ConceptSchemaError(message)
    accepted = [
        item
        for item in document.candidates
        if item.accepted and item.ticker == document.selected_ticker
    ]
    if not accepted:
        message = "selected ticker is not an accepted candidate"
        raise ConceptSchemaError(message)


def _validate_story(card: StoryCard, *, rejected: bool) -> None:
    if _STORY_ID.fullmatch(card.story_id) is None:
        message = "story_id must be 12 hex characters"
        raise ConceptSchemaError(message)
    if not card.sources:
        message = "story needs a source"
        raise ConceptSchemaError(message)
    if card.score is None or not math.isfinite(card.score) or card.score < 0:
        message = "score must be a finite non-negative number"
        raise ConceptSchemaError(message)
    if not _finite_or_missing(card.velocity) or not _finite_or_missing(
        card.crowding_penalty
    ):
        message = "story metrics must be finite when present"
        raise ConceptSchemaError(message)
    if rejected:
        if card.safety_gate != 0 or card.rank is not None:
            message = "rejected stories have gate 0 and no rank"
            raise ConceptSchemaError(message)
        return
    if card.safety_gate != 1 or card.rank is None or card.rank < 1:
        message = "top stories are safe and ranked"
        raise ConceptSchemaError(message)
    if (
        card.score <= 0
        or card.velocity is None
        or card.crowding_penalty is None
        or card.copycat_count is None
        or card.leader_outside_buyers is None
    ):
        message = "top story is missing coverage"
        raise ConceptSchemaError(message)


def _validate_candidate(candidate: ConceptCandidate) -> None:
    if not candidate.accepted:
        return
    if len(candidate.name) < 1 or len(candidate.name) > NAME_MAX:
        message = "accepted name must be 1 to 32 characters"
        raise ConceptSchemaError(message)
    if _TICKER.fullmatch(candidate.ticker) is None:
        message = "accepted ticker must be 3 to 10 uppercase characters"
        raise ConceptSchemaError(message)
    if len(candidate.description.split()) < DESCRIPTION_MIN_WORDS:
        message = "accepted description needs at least 20 words"
        raise ConceptSchemaError(message)
    if candidate.game_template_id not in GAME_TEMPLATE_IDS:
        message = "game template is not one of the five ids"
        raise ConceptSchemaError(message)
    for color in (
        candidate.theme_palette.primary,
        candidate.theme_palette.secondary,
        candidate.theme_palette.accent,
        candidate.theme_palette.background,
    ):
        if _HEX_COLOR.fullmatch(color) is None:
            message = "palette colors must be #RRGGBB"
            raise ConceptSchemaError(message)
    _reject_unsafe_copy(candidate)
    names = tuple(check.name for check in candidate.collision_checks)
    if names != _CHECK_NAMES or not all(
        check.passed for check in candidate.collision_checks
    ):
        message = "accepted candidate has a failing collision check"
        raise ConceptSchemaError(message)


def _reject_unsafe_copy(candidate: ConceptCandidate) -> None:
    if not has_shadow_disclosure(candidate.description):
        message = "accepted description is missing the shadow disclosure"
        raise ConceptSchemaError(message)
    blob = " ".join(
        (
            candidate.name,
            candidate.ticker,
            candidate.character,
            candidate.joke,
            candidate.description,
            candidate.art_prompt,
        )
    )
    if contains_hate(blob):
        message = "accepted candidate contains hate content"
        raise ConceptSchemaError(message)
    if find_persons(blob):
        message = "accepted candidate names a person"
        raise ConceptSchemaError(message)


def _finite_or_missing(value: float | None) -> bool:
    return value is None or math.isfinite(value)


def _story_from(value: object) -> StoryCard:
    payload = _as_dict(value)
    _exact_keys(payload, _STORY_KEYS)
    rank = payload["rank"]
    if rank is not None and not isinstance(rank, int):
        message = "rank must be an integer or null"
        raise ConceptSchemaError(message)
    return StoryCard(
        story_id=_as_str(payload, "story_id"),
        title=_as_str(payload, "title"),
        summary=_as_str(payload, "summary"),
        detected_at=_as_str(payload, "detected_at"),
        sources=tuple(_as_str_list(payload, "sources")),
        category=_as_str(payload, "category"),
        hook=_as_str(payload, "hook"),
        velocity=_as_optional_float(payload, "velocity"),
        parodyability=_as_float(payload, "parodyability"),
        safety_gate=_as_int(payload, "safety_gate"),
        safety_reasons=tuple(_as_str_list(payload, "safety_reasons")),
        crowding_penalty=_as_optional_float(payload, "crowding_penalty"),
        audience_fit=_as_float(payload, "audience_fit"),
        hour_multiplier=_as_float(payload, "hour_multiplier"),
        score=_as_optional_float(payload, "score"),
        copycat_count=_as_optional_int(payload, "copycat_count"),
        leader_outside_buyers=_as_optional_int(payload, "leader_outside_buyers"),
        outlets=tuple(_as_str_list(payload, "outlets")),
        rank=rank,
    )


def _candidate_from(value: object) -> ConceptCandidate:
    payload = _as_dict(value)
    _exact_keys(payload, _CANDIDATE_KEYS)
    palette_raw = payload["theme_palette"]
    palette = _as_dict(palette_raw)
    _exact_keys(palette, _PALETTE_KEYS)
    checks: list[CollisionCheck] = []
    for item in _as_list(payload, "collision_checks"):
        check = _as_dict(item)
        _exact_keys(check, _CHECK_KEYS)
        passed = check["passed"]
        if not isinstance(passed, bool):
            message = "collision passed must be a boolean"
            raise ConceptSchemaError(message)
        checks.append(
            CollisionCheck(
                name=_as_str(check, "name"),
                passed=passed,
                reason=_as_str(check, "reason"),
            )
        )
    return ConceptCandidate(
        name=_as_str(payload, "name"),
        ticker=_as_str(payload, "ticker"),
        character=_as_str(payload, "character"),
        joke=_as_str(payload, "joke"),
        description=_as_str(payload, "description"),
        game_template_id=_as_str(payload, "game_template_id"),
        theme_palette=ThemePalette(
            primary=_as_str(palette, "primary"),
            secondary=_as_str(palette, "secondary"),
            accent=_as_str(palette, "accent"),
            background=_as_str(palette, "background"),
        ),
        art_prompt=_as_str(payload, "art_prompt"),
        collision_checks=tuple(checks),
        accepted=_as_bool(payload, "accepted"),
    )


def _exact_keys(payload: dict[str, object], keys: frozenset[str]) -> None:
    if set(payload) != keys:
        message = "concept keys do not match the schema"
        raise ConceptSchemaError(message)


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        message = "concept json must be an object"
        raise ConceptSchemaError(message)
    return value


def _as_list(payload: dict[str, object], key: str) -> list[object]:
    value = payload[key]
    if not isinstance(value, list):
        message = f"{key} must be a list"
        raise ConceptSchemaError(message)
    return value


def _as_str(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        message = f"{key} must be a string"
        raise ConceptSchemaError(message)
    return value


def _as_optional_str(payload: dict[str, object], key: str) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        message = f"{key} must be a string or null"
        raise ConceptSchemaError(message)
    return value


def _as_bool(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        message = f"{key} must be a boolean"
        raise ConceptSchemaError(message)
    return value


def _as_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"{key} must be an integer"
        raise ConceptSchemaError(message)
    return value


def _as_float(payload: dict[str, object], key: str) -> float:
    number = _as_optional_float(payload, key)
    if number is None:
        message = f"{key} must be a number"
        raise ConceptSchemaError(message)
    return number


def _as_optional_float(payload: dict[str, object], key: str) -> float | None:
    value = payload[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        message = f"{key} must be a number"
        raise ConceptSchemaError(message)
    number = float(value)
    if not math.isfinite(number):
        message = f"{key} must be finite"
        raise ConceptSchemaError(message)
    return number


def _as_optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"{key} must be an integer"
        raise ConceptSchemaError(message)
    return value


def _as_str_list(payload: dict[str, object], key: str) -> list[str]:
    values = _as_list(payload, key)
    texts: list[str] = []
    for value in values:
        if not isinstance(value, str):
            message = f"{key} must be a list of strings"
            raise ConceptSchemaError(message)
        texts.append(value)
    return texts
