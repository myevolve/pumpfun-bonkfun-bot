"""Prompt builder: bold styles, and guardrails before anything is sent."""

from __future__ import annotations

import pytest

from meme_studio.art.guardrails import GuardrailError, post_check_asset
from meme_studio.art.prompts import build_image_prompt, build_video_prompt
from meme_studio.game.concept import load_concept
from tests.meme_studio.support import FIXTURE, load_fixture_dict, write_concept

# ruff: noqa: ANN001, S101, TRY003

STYLE_MARKERS = {
    "absurdist": "absurdist",
    "neon_chaos": "neon",
    "retro_arcade": "arcade",
    "comic_book": "comic",
}


def test_each_style_is_bold_and_distinct(tmp_path) -> None:
    base = load_fixture_dict()
    prompts: list[str] = []
    for style, marker in STYLE_MARKERS.items():
        base["style"] = style
        concept = load_concept(write_concept(tmp_path, base, f"{style}.json"))
        prompt = build_image_prompt(concept, kind="sprite", game_only=True)
        lowered = prompt.lower()
        assert marker in lowered
        assert "exaggerated" in lowered
        assert "high-energy" in lowered
        assert "Nimbus" in prompt
        assert "SOCKDRIFT" not in prompt
        assert "CA_PLACEHOLDER_DO_NOT_USE" not in prompt
        prompts.append(prompt)
    assert len(set(prompts)) == len(STYLE_MARKERS)


def test_sprite_and_video_prompts_omit_ticker_even_in_full_mode() -> None:
    concept = load_concept(FIXTURE)
    sprite = build_image_prompt(concept, kind="sprite", game_only=False)
    video = build_video_prompt(concept, game_only=False)
    assert "SOCKDRIFT" not in sprite
    assert "SOCKDRIFT" not in video
    assert "coin" not in video.lower()
    assert "gameplay" in video.lower() or "teaser" in video.lower()


def test_share_prompt_includes_ticker_only_outside_game_only() -> None:
    concept = load_concept(FIXTURE)
    full = build_image_prompt(concept, kind="share", game_only=False)
    hidden = build_image_prompt(concept, kind="share", game_only=True)
    assert "SOCKDRIFT" in full
    assert "SOCKDRIFT" not in hidden
    assert "CA_PLACEHOLDER_DO_NOT_USE" not in hidden


@pytest.mark.parametrize(
    "joke",
    [
        "a photorealistic portrait of a real person",
        "nude celebrity likeness",
        "gore and blood everywhere",
        "a nazi symbol on the cape",
        "a child holding the sock",
        "wearing a nike logo",
    ],
)
def test_unsafe_concept_text_is_rejected(tmp_path, joke: str) -> None:
    payload = load_fixture_dict()
    payload["joke"] = joke
    concept = load_concept(write_concept(tmp_path, payload))
    with pytest.raises(GuardrailError):
        build_image_prompt(concept, kind="sprite", game_only=True)


@pytest.mark.parametrize(
    "joke",
    [
        "Taylor Swift endorses our token",
        "Harry Potter wears a Gucci logo",
        "Nimbus endorses our token",
    ],
)
def test_names_brands_and_endorsements_are_rejected(tmp_path, joke: str) -> None:
    payload = load_fixture_dict()
    payload["joke"] = joke
    concept = load_concept(write_concept(tmp_path, payload, "unsafe-name.json"))
    with pytest.raises(GuardrailError):
        build_image_prompt(concept, kind="sprite", game_only=True)


def test_provocative_humor_is_allowed(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["joke"] = (
        "An unhinged screaming dust bunny, wild and chaotic and ridiculous."
    )
    concept = load_concept(write_concept(tmp_path, payload))
    prompt = build_image_prompt(concept, kind="sprite", game_only=True)
    assert "unhinged" in prompt.lower()


def test_post_check_rejects_moderation_failure_and_custom_hook() -> None:
    with pytest.raises(GuardrailError):
        post_check_asset(
            {"creative_prompt": "a round dust creature", "respect_moderation": False}
        )

    def hook(_meta: dict[str, object]) -> None:
        raise GuardrailError("custom hook")

    with pytest.raises(GuardrailError, match="custom hook"):
        post_check_asset({"creative_prompt": "a round dust creature"}, hook=hook)


def test_post_check_scans_revised_prompt() -> None:
    with pytest.raises(GuardrailError):
        post_check_asset(
            {
                "creative_prompt": "a round dust creature",
                "revised_prompt": "photorealistic child",
            }
        )
