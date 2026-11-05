"""Concept package loading. The fixture is local; nothing is imported from meme_studio.concept."""

from __future__ import annotations

import pytest

from meme_studio.game.concept import ConceptError, load_concept
from tests.meme_studio.support import FIXTURE, load_fixture_dict, write_concept

# ruff: noqa: ANN001, PLR2004, S101


def test_fixture_loads_pipeline_fields() -> None:
    concept = load_concept(FIXTURE)
    assert concept.name == "Sock Drawer Drift"
    assert concept.title == "Sock Drawer Drift"
    assert concept.ticker == "SOCKDRIFT"
    assert concept.character.name == "Nimbus"
    assert concept.character.kind == "round dust creature"
    assert "missing-sock" in concept.joke
    assert len(concept.description.split()) >= 20
    assert concept.template_id == "endless_runner"
    assert concept.style == "neon_chaos"
    assert concept.palette.background == "#1b1f3b"
    assert concept.palette.primary == "#f2c14e"
    assert concept.story_id == "2026-10-28-sock-drawer"
    assert concept.date == "2026-10-28"
    assert concept.contract_address == "CA_PLACEHOLDER_DO_NOT_USE"


def test_title_defaults_to_name_and_style_defaults(tmp_path) -> None:
    payload = load_fixture_dict()
    del payload["title"]
    del payload["style"]
    concept = load_concept(write_concept(tmp_path, payload))
    assert concept.title == concept.name
    assert concept.style == "absurdist"


def test_character_string_is_accepted(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["character"] = "Nimbus"
    concept = load_concept(write_concept(tmp_path, payload))
    assert concept.character.name == "Nimbus"


def test_template_aliases_canonicalize(tmp_path) -> None:
    payload = load_fixture_dict()
    for raw, canonical in (
        ("runner", "endless_runner"),
        ("endless-runner", "endless_runner"),
        ("clicker", "tap_clicker"),
        ("tap-clicker", "tap_clicker"),
        ("dodge", "dodge_catch"),
        ("dodge-catch", "dodge_catch"),
    ):
        payload["template_id"] = raw
        concept = load_concept(write_concept(tmp_path, payload, f"{raw}.json"))
        assert concept.template_id == canonical


def test_ticker_is_normalized_to_uppercase(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["ticker"] = "sockdrift"
    concept = load_concept(write_concept(tmp_path, payload))
    assert concept.ticker == "SOCKDRIFT"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticker", "AB"),
        ("ticker", "THISISTOOLONG"),
        ("ticker", "NO SPACE"),
        ("name", "N" * 33),
        ("description", "too short to publish"),
        ("template_id", "quiz"),
        ("style", "photorealistic"),
        ("date", "10/28/2026"),
        ("story_id", "../escape"),
    ],
)
def test_invalid_fields_raise(tmp_path, field: str, value: str) -> None:
    payload = load_fixture_dict()
    payload[field] = value
    with pytest.raises(ConceptError) as caught:
        load_concept(write_concept(tmp_path, payload, f"{field}.json"))
    assert field in str(caught.value)


def test_bad_palette_raises(tmp_path) -> None:
    payload = load_fixture_dict()
    palette = dict(payload["palette"])  # type: ignore[arg-type]
    palette["accent"] = "green"
    payload["palette"] = palette
    with pytest.raises(ConceptError) as caught:
        load_concept(write_concept(tmp_path, payload))
    assert "accent" in str(caught.value)


def test_secret_fields_are_rejected(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["private_key"] = "not-a-real-key"
    with pytest.raises(ConceptError) as caught:
        load_concept(write_concept(tmp_path, payload))
    assert "private_key" in str(caught.value)


def test_env_file_pointer_is_rejected(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["env_file"] = ".env"
    with pytest.raises(ConceptError) as caught:
        load_concept(write_concept(tmp_path, payload))
    assert "env_file" in str(caught.value)


def test_mint_alias_fills_contract_when_absent(tmp_path) -> None:
    payload = load_fixture_dict()
    del payload["contract_address"]
    payload["mint"] = "MINT_PLACEHOLDER_ONLY"
    concept = load_concept(write_concept(tmp_path, payload))
    assert concept.contract_address == "MINT_PLACEHOLDER_ONLY"


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(ConceptError):
        load_concept(tmp_path / "missing.json")


def test_unknown_note_field_is_ignored(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["note"] = "draft only"
    concept = load_concept(write_concept(tmp_path, payload))
    assert concept.name == "Sock Drawer Drift"
    assert not hasattr(concept, "note")
    assert "draft only" not in repr(concept)
