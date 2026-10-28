# ruff: noqa: S101
"""Schema contract for the daily shadow concept.json."""

from datetime import UTC, datetime

import pytest

from meme_studio.concept.schema import (
    BASE_UNKNOWNS,
    CollisionCheck,
    ConceptCandidate,
    ConceptDocument,
    ConceptSchemaError,
    StoryCard,
    ThemePalette,
    concept_from_json,
    dump_concept,
    studio_date,
    validate_concept,
)

NOW = "2026-10-28T15:00:00+00:00"
DESCRIPTION = " ".join(["cartoon"] * 20)


def _candidate(*, accepted: bool = True, ticker: str = "RACODONU") -> ConceptCandidate:
    return ConceptCandidate(
        name="Raccoon Donut",
        ticker=ticker,
        character="Raccoon Donut is an original cartoon raccoon",
        joke="A cartoon raccoon treats a donut like a state secret.",
        description=DESCRIPTION,
        game_template_id="dodge",
        theme_palette=ThemePalette(
            primary="#1B4D3E",
            secondary="#F4E8C1",
            accent="#E07A5F",
            background="#102A22",
        ),
        art_prompt="original cartoon raccoon holding a donut, flat colors",
        collision_checks=(
            CollisionCheck(name="existing_tokens", passed=True, reason="ok"),
            CollisionCheck(name="trademark", passed=True, reason="ok"),
            CollisionCheck(name="real_person", passed=True, reason="ok"),
            CollisionCheck(name="own_history", passed=True, reason="ok"),
        ),
        accepted=accepted,
    )


def _story(*, rank: int | None = 1, gate: int = 1) -> StoryCard:
    return StoryCard(
        story_id="0123456789ab",
        title="A raccoon escaped with a donut",
        summary="A raccoon escaped with a donut",
        detected_at="2026-10-28T14:30:00+00:00",
        sources=("google_news",),
        category="animal",
        hook="raccoon+donut",
        velocity=1.0,
        parodyability=1.0,
        safety_gate=gate,
        safety_reasons=() if gate == 1 else ("tragedy_disaster",),
        crowding_penalty=1.0,
        audience_fit=1.0,
        hour_multiplier=1.0,
        score=1.0 if gate == 1 else 0.0,
        copycat_count=0,
        leader_outside_buyers=0,
        outlets=("Example News",),
        rank=rank,
    )


def _document(**overrides: object) -> ConceptDocument:
    story = _story()
    candidate = _candidate()
    base = ConceptDocument(
        schema_version="1",
        generated_at=NOW,
        phase="0",
        mode="shadow-dry-run",
        date="2026-10-28",
        status="draft",
        skipped_reason=None,
        top_stories=(story,),
        rejected_stories=(),
        selected_story_id=story.story_id,
        candidates=(candidate,),
        selected_ticker=candidate.ticker,
        unknowns=BASE_UNKNOWNS,
        publish=False,
        transactions=False,
        network_writes="none",
    )
    if not overrides:
        return base
    data = base.to_dict()
    data.update(overrides)
    return concept_from_json(__import__("json").dumps(data))


def test_base_unknowns_are_the_phase_zero_gaps() -> None:
    assert set(BASE_UNKNOWNS) == {
        "reddit_data_api_commercial_terms",
        "uspto_bulk_query_permitted",
        "google_trends_public_api",
        "hour_of_day_weights_unverified",
        "audience_fit_priors_empty",
        "rule_based_person_detector_is_not_a_trained_ner",
        "x_trends_http_shape_unverified_until_live_read",
        "dexscreener_volume_field_is_h24_not_7d",
    }


def test_roundtrip_is_byte_stable() -> None:
    document = _document()
    validate_concept(document)
    raw = dump_concept(document)
    again = dump_concept(concept_from_json(raw))
    assert again == raw
    payload = __import__("json").loads(raw)
    assert set(payload) == {
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
    assert set(payload["candidates"][0]) == {
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
    assert set(payload["candidates"][0]["theme_palette"]) == {
        "primary",
        "secondary",
        "accent",
        "background",
    }
    assert payload["publish"] is False
    assert payload["transactions"] is False
    assert payload["phase"] == "0"
    assert payload["mode"] == "shadow-dry-run"
    assert payload["network_writes"] == "none"


def test_studio_date_uses_pacific_time() -> None:
    early = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    later = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    assert studio_date(early) == "2026-10-27"
    assert studio_date(later) == "2026-10-28"


@pytest.mark.parametrize(
    "overrides",
    [
        {"publish": True},
        {"transactions": True},
        {"phase": "1"},
        {"mode": "live"},
        {"network_writes": "posts"},
        {"schema_version": "2"},
        {"status": "launched"},
        {"selected_ticker": "NOPE"},
    ],
)
def test_validate_rejects_phase_zero_violations(overrides: dict[str, object]) -> None:
    document = _document(**overrides)
    with pytest.raises(ConceptSchemaError):
        validate_concept(document)


def test_validate_rejects_short_ticker_and_short_description() -> None:
    short = _candidate(ticker="AB")
    document = ConceptDocument(
        schema_version="1",
        generated_at=NOW,
        phase="0",
        mode="shadow-dry-run",
        date="2026-10-28",
        status="draft",
        skipped_reason=None,
        top_stories=(_story(),),
        rejected_stories=(),
        selected_story_id="0123456789ab",
        candidates=(
            ConceptCandidate(
                name=short.name,
                ticker="AB",
                character=short.character,
                joke=short.joke,
                description="too short",
                game_template_id=short.game_template_id,
                theme_palette=short.theme_palette,
                art_prompt=short.art_prompt,
                collision_checks=short.collision_checks,
                accepted=True,
            ),
        ),
        selected_ticker="AB",
        unknowns=BASE_UNKNOWNS,
        publish=False,
        transactions=False,
        network_writes="none",
    )
    with pytest.raises(ConceptSchemaError):
        validate_concept(document)


def test_validate_rejects_unknown_top_level_key() -> None:
    raw = dump_concept(_document())
    payload = __import__("json").loads(raw)
    payload["wallet"] = "not-allowed"
    with pytest.raises(ConceptSchemaError):
        concept_from_json(__import__("json").dumps(payload))


def test_skipped_document_has_no_selected_ticker() -> None:
    document = ConceptDocument(
        schema_version="1",
        generated_at=NOW,
        phase="0",
        mode="shadow-dry-run",
        date="2026-10-28",
        status="skipped",
        skipped_reason="no_safe_story",
        top_stories=(),
        rejected_stories=(_story(rank=None, gate=0),),
        selected_story_id=None,
        candidates=(),
        selected_ticker=None,
        unknowns=BASE_UNKNOWNS,
        publish=False,
        transactions=False,
        network_writes="none",
    )
    validate_concept(document)
    assert document.selected_ticker is None
