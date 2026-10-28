# ruff: noqa: S101
"""Generated-media prompt checks and synthetic-media labels."""

from __future__ import annotations

import pytest

from meme_studio.compliance import MediaMetadata, Post, compliance_lint


def test_photorealistic_real_person_likeness_fails() -> None:
    result = compliance_lint(
        MediaMetadata(
            prompt=(
                "photorealistic portrait of a real woman, studio likeness, sharp face"
            ),
            kind="image",
        )
    )
    assert result.passed is False
    assert "media_likeness" in result.reasons


def test_photorealistic_cartoon_animal_is_not_a_person_likeness() -> None:
    result = compliance_lint(
        MediaMetadata(
            prompt="photorealistic pigeon feathers on a wild cartoon bird",
            kind="image",
        )
    )
    assert "media_likeness" not in result.reasons
    assert result.passed is True


@pytest.mark.parametrize(
    ("prompt", "code"),
    [
        ("a nude cartoon pigeon", "media_sexual"),
        ("blood and gore on the stage", "media_gore"),
        ("white power banner behind the bird", "media_hate"),
        ("a child pigeon in the scene", "media_minor"),
        ("wearing a Nike swoosh hat", "media_brand"),
        ("Elon Musk riding a bench", "person_blocked"),
    ],
)
def test_disallowed_prompt_categories(prompt: str, code: str) -> None:
    result = compliance_lint(MediaMetadata(prompt=prompt, kind="image"))
    assert result.passed is False
    assert code in result.reasons


def test_overlays_are_screened_on_every_channel() -> None:
    result = compliance_lint(
        MediaMetadata(
            prompt="cartoon pigeon",
            overlays=("Elon Musk",),
            channel="x",
            kind="image",
        )
    )
    assert result.passed is False
    assert "person_blocked" in result.reasons


def test_provocative_wild_cartoon_prompt_passes() -> None:
    result = compliance_lint(
        MediaMetadata(
            prompt=(
                "Wild provocative absurd cartoon parody sticker, original "
                "non-human character, exaggerated shapes, flat colors, "
                "no lettering, no brand marks."
            ),
            kind="image",
        )
    )
    assert result.passed is True, result.reasons


@pytest.mark.parametrize("channel", ["x", "tiktok", "shorts"])
def test_generated_media_posts_need_ai_generated_label(channel: str) -> None:
    missing = compliance_lint(
        Post(
            text="Pigeons wobble (automated post)",
            channel=channel,
            uses_generated_media=True,
        )
    )
    assert "missing_ai_generated_label" in missing.reasons
    labeled = compliance_lint(
        Post(
            text="Pigeons wobble (automated post) AI-generated",
            channel=channel,
            uses_generated_media=True,
        )
    )
    assert labeled.passed is True


def test_telegram_does_not_require_ai_generated_label() -> None:
    result = compliance_lint(
        Post(
            text="Pigeons wobble (automated post)",
            channel="telegram",
            uses_generated_media=True,
        )
    )
    assert "missing_ai_generated_label" not in result.reasons
    assert result.passed is True


@pytest.mark.parametrize("channel", ["tiktok", "shorts"])
def test_game_only_clip_overlays_block_coin_ticker_and_address(
    channel: str,
) -> None:
    dirty = compliance_lint(
        MediaMetadata(
            prompt="overlay the contract address on the clip",
            kind="video",
            channel=channel,
            game_only=True,
            overlays=("$BENCH",),
            ticker="BENCH",
        )
    )
    assert dirty.passed is False
    assert "media_overlay:dollar" in dirty.reasons
    assert "media_overlay:ticker" in dirty.reasons
    assert "media_overlay:address" in dirty.reasons
    clean = compliance_lint(
        MediaMetadata(
            prompt="cartoon pigeon plays a free game",
            kind="video",
            channel=channel,
            game_only=True,
            overlays=("final score 12",),
        )
    )
    assert clean.passed is True, clean.reasons
