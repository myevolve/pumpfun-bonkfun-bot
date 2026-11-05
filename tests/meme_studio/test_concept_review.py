# ruff: noqa: S101
"""Review regressions: disclosure, people, hate, brands, unknowns, finite numbers."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from meme_studio.concept.assemble import assemble_concept
from meme_studio.concept.collisions import (
    LookupUnavailableError,
    MemoryTokenLookup,
    TokenHit,
    check_candidate,
    parse_dexscreener_search,
)
from meme_studio.concept.generator import draft_candidates
from meme_studio.concept.inputs import CountPoint, OutletHit, StoryInput
from meme_studio.concept.safety import evaluate_safety
from meme_studio.concept.schema import (
    BASE_UNKNOWNS,
    SHADOW_DISCLOSURE,
    CollisionCheck,
    ConceptCandidate,
    ConceptDocument,
    ConceptSchemaError,
    StoryCard,
    ThemePalette,
    concept_from_json,
    dump_concept,
    validate_concept,
)
from meme_studio.concept.scorer import score_stories
from meme_studio.concept.writer_contract import FORMAT_NAME, writer_packages

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
GENERATED = "2026-10-28T15:00:00+00:00"
DESCRIPTION = " ".join(["cartoon"] * 20) + " " + SHADOW_DISCLOSURE
DISCLOSURE_MARKERS = (
    "no token has been created",
    "none is held",
    "we created the coin",
    "we hold it",
    "a person must disclose",
)


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


def _story() -> StoryCard:
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
        safety_gate=1,
        safety_reasons=(),
        crowding_penalty=1.0,
        audience_fit=1.0,
        hour_multiplier=1.0,
        score=1.0,
        copycat_count=0,
        leader_outside_buyers=0,
        outlets=("Example News",),
        rank=1,
    )


def _document() -> ConceptDocument:
    story = _story()
    candidate = _candidate()
    return ConceptDocument(
        schema_version="1",
        generated_at=GENERATED,
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


def _input(title: str, *, counts: tuple[CountPoint, ...] = ()) -> StoryInput:
    return StoryInput(
        title=title,
        summary=title,
        detected_at=NOW - timedelta(minutes=10),
        sources=("google_news",),
        outlets=(
            OutletHit(name="Example News", published_at=NOW - timedelta(minutes=5)),
        ),
        post_counts=counts,
        copycats=(),
    )


def test_cartoon_only_description_is_rejected() -> None:
    candidate = _candidate()
    bare = replace(candidate, description=" ".join(["cartoon"] * 20))
    document = replace(_document(), candidates=(bare,), selected_ticker=bare.ticker)
    with pytest.raises(ConceptSchemaError):
        validate_concept(document)


def test_generator_draft_states_the_shadow_disclosure() -> None:
    draft = draft_candidates(
        "A raccoon escaped with a donut",
        "A raccoon escaped with a donut",
    )[0]
    folded = draft.description.casefold()
    for marker in DISCLOSURE_MARKERS:
        assert marker in folded
    assert "no token has been created" in folded
    assert "we hold it" in folded


def test_taylor_swift_without_an_allegation_is_a_person() -> None:
    for text in (
        "A raccoon escaped with a donut near Taylor Swift",
        "Cartoon Taylor Swift holding a donut",
        "taylor swift",
    ):
        result = evaluate_safety(text)
        assert result.gate == 0, text
        assert "real_person" in result.reasons


def test_nazi_banner_story_is_hate() -> None:
    result = evaluate_safety("A raccoon escaped with a donut and waved a Nazi banner")
    assert result.gate == 0
    assert "hate_content" in result.reasons


def test_artist_prompt_and_hate_ticker_are_not_accepted() -> None:
    person = check_candidate(
        replace(
            _candidate(accepted=False),
            art_prompt="original cartoon portrait of Taylor Swift",
            collision_checks=(),
        ),
        lookup=MemoryTokenLookup([]),
        history=set(),
    )
    assert person.accepted is False
    assert person.collision_checks[2].passed is False
    hate = check_candidate(
        _candidate(ticker="NAZI"),
        lookup=MemoryTokenLookup([]),
        history=set(),
    )
    assert hate.accepted is False
    forced = replace(hate, accepted=True)
    document = replace(
        _document(),
        candidates=(forced,),
        selected_ticker="NAZI",
    )
    with pytest.raises(ConceptSchemaError):
        validate_concept(document)


def test_rejected_candidate_still_needs_a_truthful_disclosure() -> None:
    rejected = replace(
        _candidate(accepted=False), description=" ".join(["cartoon"] * 20)
    )
    document = replace(_document(), candidates=(_candidate(), rejected))
    with pytest.raises(ConceptSchemaError):
        dump_concept(document)


def test_disclosure_markers_cannot_contradict_the_unlaunched_sentence() -> None:
    text = (
        "No token has been created. None is held. We created the coin and we "
        "hold it. A person must disclose that this paragraph describes the present."
    )
    candidate = replace(_candidate(), description=text)
    document = replace(_document(), candidates=(candidate,))
    with pytest.raises(ConceptSchemaError):
        dump_concept(document)


def test_stale_story_flags_and_brand_prompts_are_rechecked() -> None:
    hate = replace(
        _story(),
        title="A raccoon waved a Nazi banner",
        summary="A raccoon waved a Nazi banner",
        safety_gate=1,
    )
    with pytest.raises(ConceptSchemaError):
        dump_concept(replace(_document(), top_stories=(hate,)))
    branded = replace(
        _candidate(),
        art_prompt="original cartoon with a Coca-Cola logo",
    )
    with pytest.raises(ConceptSchemaError):
        dump_concept(replace(_document(), candidates=(branded,)))


def test_brand_punctuation_and_diacritics_match_the_blocklist() -> None:
    branded = check_candidate(
        replace(
            _candidate(accepted=False),
            art_prompt="original cartoon holding Coca-Cola and Pokémon",
            collision_checks=(),
        ),
        lookup=MemoryTokenLookup([]),
        history=set(),
    )
    assert branded.accepted is False
    assert branded.collision_checks[1].passed is False
    for prompt in (
        "original cartoon holding Coca\u2011Cola",
        "original cartoon of Nike's shoes",
        "original cartoon in Nike-branded colors",
    ):
        hit = check_candidate(
            replace(_candidate(accepted=False), art_prompt=prompt, collision_checks=()),
            lookup=MemoryTokenLookup([]),
            history=set(),
        )
        assert hit.accepted is False, prompt
        assert hit.collision_checks[1].passed is False


def test_absent_token_metrics_do_not_clear_a_collision() -> None:
    reviewed = check_candidate(
        _candidate(),
        lookup=MemoryTokenLookup(
            [TokenHit("RACODONU", "Elsewhere", None, None, "solana")]
        ),
        history=set(),
    )
    assert reviewed.accepted is False
    assert "metrics_unknown" in reviewed.collision_checks[0].reason


def test_malformed_dex_payloads_fail_closed_without_echoing_the_body() -> None:
    for body in (b"{}", b'{"pairs":{}}', b'{"pairs":[{}]}', b"\xffSECRET_MARKER"):
        with pytest.raises(LookupUnavailableError) as exc:
            parse_dexscreener_search(body)
        assert "SECRET_MARKER" not in str(exc.value)


def test_nan_metrics_do_not_clear_a_collision() -> None:
    reviewed = check_candidate(
        _candidate(),
        lookup=MemoryTokenLookup(
            [TokenHit("RACODONU", "Elsewhere", float("nan"), float("nan"), "solana")]
        ),
        history=set(),
    )
    assert reviewed.accepted is False
    assert "metrics_unknown" in reviewed.collision_checks[0].reason


def test_two_zero_bins_are_not_a_full_baseline() -> None:
    baseline = NOW - timedelta(minutes=390)
    story = _input(
        "A raccoon escaped with a donut",
        counts=(
            CountPoint(start=baseline, count=0),
            CountPoint(start=baseline + timedelta(minutes=30), count=0),
            CountPoint(start=NOW - timedelta(minutes=10), count=100),
        ),
    )
    batch = score_stories([story], now=NOW)
    assert batch.top_stories == ()
    assert batch.uncovered_stories[0].velocity is None


def test_non_finite_story_factors_are_rejected() -> None:
    card = replace(_story(), parodyability=math.inf)
    with pytest.raises(ConceptSchemaError):
        validate_concept(replace(_document(), top_stories=(card,)))
    card = replace(_story(), audience_fit=math.nan)
    with pytest.raises(ConceptSchemaError):
        validate_concept(replace(_document(), top_stories=(card,)))
    card = replace(_story(), hour_multiplier=math.inf)
    with pytest.raises(ConceptSchemaError):
        validate_concept(replace(_document(), top_stories=(card,)))


def test_writer_package_leaves_launch_facts_unknown() -> None:
    packages = writer_packages(_document())
    assert packages[0]["format"] == FORMAT_NAME
    assert packages[0]["lifecycle"] == "shadow-unlaunched"
    assert packages[0]["ticker"] == "RACODONU"
    assert packages[0]["mint"] is None
    assert packages[0]["wallet"] is None
    assert packages[0]["dev_buy_sol"] is None
    assert packages[0]["disclosure"]


def test_recent_counts_without_a_baseline_are_not_a_surge() -> None:
    story = _input(
        "A raccoon escaped with a donut",
        counts=(CountPoint(start=NOW - timedelta(minutes=10), count=100),),
    )
    batch = score_stories([story], now=NOW)
    assert batch.top_stories == ()
    assert batch.uncovered_stories[0].velocity is None
    assert batch.uncovered_stories[0].score is None


def test_missing_copycats_are_not_stored_as_zero() -> None:
    story = replace(_input("A raccoon escaped with a donut"), copycats=None)
    batch = score_stories([story], now=NOW)
    card = batch.uncovered_stories[0]
    assert card.copycat_count is None
    assert card.leader_outside_buyers is None
    assert card.crowding_penalty is None
    document = assemble_concept(
        batch,
        lookup=MemoryTokenLookup([]),
        history=(),
        now=NOW,
        unknowns=BASE_UNKNOWNS,
    )
    assert document.status == "skipped"
    assert document.skipped_reason == "coverage_unknown"
    assert "copycat_coverage_unknown" in document.unknowns


def test_non_finite_numbers_are_rejected() -> None:
    story = _input("A raccoon escaped with a donut")
    with pytest.raises(ValueError):
        score_stories([story], now=NOW, priors={"animal": math.inf})
    card = replace(_story(), score=math.inf)
    document = replace(_document(), top_stories=(card,))
    with pytest.raises(ConceptSchemaError):
        validate_concept(document)
    with pytest.raises(ConceptSchemaError):
        dump_concept(document)


def test_overflowing_json_number_is_rejected() -> None:
    raw = dump_concept(_document())
    payload = __import__("json").loads(raw)
    payload["top_stories"][0]["score"] = 1e309
    text = __import__("json").dumps(payload)
    with pytest.raises(ConceptSchemaError):
        concept_from_json(text)
