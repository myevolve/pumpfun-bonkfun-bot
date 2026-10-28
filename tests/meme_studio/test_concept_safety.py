# ruff: noqa: S101
"""Hard safety gate for tragedies, elections disinfo, people, and brands."""

import pytest

from meme_studio.concept.safety import evaluate_safety


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("The driver died at the scene", "tragedy_death"),
        ("A player was injured", "tragedy_injury"),
        ("An earthquake hit overnight", "tragedy_disaster"),
        ("Police report a shooting downtown", "tragedy_shooting"),
        ("A terrorist threat closed the square", "tragedy_terrorism"),
        ("A child found a balloon", "tragedy_minors"),
        ("A pandemic warning was issued", "tragedy_illness"),
        ("The report described sexual abuse", "tragedy_abuse"),
        (
            "Activists claim fake ballots flipped the votes",
            "elections_disinfo",
        ),
        ("President Ada opened a park", "real_person"),
        ("Mr. Smith was accused of fraud", "defamation_risk"),
        ("Nike unveils a shoe", "hook_requires_brand"),
        ("", "empty_text"),
        ("   ", "empty_text"),
    ],
)
def test_safety_rejects(text: str, reason: str) -> None:
    result = evaluate_safety(text)
    assert result.gate == 0
    assert reason in result.reasons


@pytest.mark.parametrize(
    "text",
    [
        "A raccoon escaped with a donut",
        "A 40-year-old raccoon escaped with a donut",
        "A minor delay before the goose stole a pretzel",
        ("The city council scheduled an election and a raccoon escaped with a donut"),
        "A raccoon was accused of stealing a donut",
        "A raccoon escaped with a nike donut",
        "Committee publishes quarterly minutes",
    ],
)
def test_safety_passes_non_tragic_hooks(text: str) -> None:
    result = evaluate_safety(text)
    assert result.gate == 1
    assert result.reasons == ()


def test_under_18_age_is_a_minor() -> None:
    result = evaluate_safety("A 5-year-old raccoon escaped with a donut")
    assert result.gate == 0
    assert "tragedy_minors" in result.reasons


def test_shooting_that_kills_a_child_collects_each_reason() -> None:
    result = evaluate_safety("A shooting killed a child")
    assert result.gate == 0
    assert "tragedy_shooting" in result.reasons
    assert "tragedy_death" in result.reasons
    assert "tragedy_minors" in result.reasons


def test_named_person_accusation_is_defamation_risk() -> None:
    result = evaluate_safety("Mr. Smith was accused of fraud")
    assert "real_person" in result.reasons
    assert "defamation_risk" in result.reasons
    assert result.gate == 0


def test_two_word_name_with_accusation_is_a_person() -> None:
    result = evaluate_safety("Ada Lovelace was accused of fraud")
    assert "real_person" in result.reasons
    assert "defamation_risk" in result.reasons


def test_brand_accusation_is_defamation_and_a_brand_hook() -> None:
    result = evaluate_safety("Nike was accused of fraud")
    assert "defamation_risk" in result.reasons
    assert "hook_requires_brand" in result.reasons
    assert "real_person" not in result.reasons


def test_safety_matches_case_insensitively() -> None:
    result = evaluate_safety("EARTHQUAKE HITS")
    assert result.gate == 0
    assert "tragedy_disaster" in result.reasons


def test_incidental_brand_does_not_block_an_animal_hook() -> None:
    result = evaluate_safety("A raccoon escaped with a Nike donut")
    assert result.gate == 1
    assert "hook_requires_brand" not in result.reasons
