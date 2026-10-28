# ruff: noqa: S101
"""Grok Imagine metadata files beside fixture assets. No network."""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from meme_studio.art import (
    MODEL_IMAGE,
    MODEL_VIDEO,
    PROVIDER,
    ArtSpec,
    build_wild_meme_prompt,
    write_imagine_asset,
)
from meme_studio.compliance import MediaMetadata, compliance_lint
from meme_studio.errors import UnsafeStemError

if TYPE_CHECKING:
    from pathlib import Path


def test_constants_name_grok_imagine() -> None:
    assert PROVIDER == "xai-grok-imagine"
    assert MODEL_IMAGE == "grok-imagine"
    assert MODEL_VIDEO == "grok-imagine-video"


def test_wild_prompt_is_compliant() -> None:
    prompt = build_wild_meme_prompt(
        character="a round cartoon pigeon in a paper hat",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
    )
    lowered = prompt.lower()
    assert "wild" in lowered
    assert "provocative" in lowered
    assert "pigeon" in lowered
    result = compliance_lint(MediaMetadata(prompt=prompt, kind="image"))
    assert result.passed is True, result.reasons


def test_fixture_image_and_video_write_sidecar_metadata(
    tmp_path: Path,
) -> None:
    stamp = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    image = ArtSpec(
        stem="logo",
        kind="image",
        prompt=build_wild_meme_prompt(
            character="a round cartoon pigeon in a paper hat",
            joke_line="The pigeons elected a bench.",
            game_title="Bench Birds",
        ),
        body=b"fixture-png-bytes",
        timestamp=stamp,
    )
    video = ArtSpec(
        stem="clip",
        kind="video",
        prompt=image.prompt,
        body=b"fixture-mp4-bytes",
        timestamp=stamp,
        channel="tiktok",
        game_only=True,
        overlays=("final score 12",),
    )
    image_result = write_imagine_asset(tmp_path, image)
    video_result = write_imagine_asset(tmp_path, video)
    assert image_result.written is True
    assert video_result.written is True
    assert image_result.asset_path is not None
    assert video_result.asset_path is not None
    assert image_result.asset_path.read_bytes() == b"fixture-png-bytes"
    assert image_result.asset_path.suffix == ".png"
    assert video_result.asset_path.suffix == ".mp4"
    assert image_result.metadata_path == tmp_path / "logo.png.meta.json"
    assert video_result.metadata_path == tmp_path / "clip.mp4.meta.json"
    meta = json.loads(image_result.metadata_path.read_text(encoding="utf-8"))
    assert meta["prompt"] == image.prompt
    assert meta["provider"] == PROVIDER
    assert meta["model"] == MODEL_IMAGE
    assert meta["timestamp"] == "2026-10-28T18:00:00+00:00"
    clip_meta = json.loads(video_result.metadata_path.read_text(encoding="utf-8"))
    assert clip_meta["model"] == MODEL_VIDEO
    assert clip_meta["provider"] == PROVIDER


def test_rejected_prompt_writes_no_asset(tmp_path: Path) -> None:
    spec = ArtSpec(
        stem="blocked",
        kind="image",
        prompt="photorealistic likeness of Elon Musk, nude, gore",
        body=b"should-not-land",
        timestamp=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
    )
    result = write_imagine_asset(tmp_path, spec)
    assert result.written is False
    assert result.lint.passed is False
    assert result.asset_path is None
    assert not (tmp_path / "blocked.png").exists()
    assert not (tmp_path / "blocked.meta.json").exists()
    assert not (tmp_path / "blocked.png.meta.json").exists()
    assert result.metadata_path is None
    assert list(tmp_path.iterdir()) == []


def test_tiktok_coin_overlay_is_not_written(tmp_path: Path) -> None:
    spec = ArtSpec(
        stem="overlay",
        kind="video",
        prompt="cartoon pigeon plays",
        body=b"clip",
        timestamp=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
        channel="tiktok",
        game_only=True,
        overlays=("$BENCH",),
        ticker="BENCH",
    )
    result = write_imagine_asset(tmp_path, spec)
    assert result.written is False
    assert any(reason.startswith("media_overlay:") for reason in result.lint.reasons)
    assert list(tmp_path.iterdir()) == []


def test_stem_cannot_escape_output_directory(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    spec = ArtSpec(
        stem="../escaped",
        kind="image",
        prompt=build_wild_meme_prompt(
            character="a round cartoon pigeon in a paper hat",
            joke_line="The pigeons elected a bench.",
            game_title="Bench Birds",
        ),
        body=b"fixture-png-bytes",
        timestamp=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
    )
    with pytest.raises(UnsafeStemError):
        write_imagine_asset(assets, spec)
    assert not (tmp_path / "escaped.png").exists()
    assert not (tmp_path / "escaped.png.meta.json").exists()


def test_same_stem_keeps_separate_image_and_video_sidecars(tmp_path: Path) -> None:
    stamp = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    prompt = build_wild_meme_prompt(
        character="a round cartoon pigeon in a paper hat",
        joke_line="The pigeons elected a bench.",
        game_title="Bench Birds",
    )
    image = write_imagine_asset(
        tmp_path,
        ArtSpec(
            stem="same",
            kind="image",
            prompt=prompt,
            body=b"png",
            timestamp=stamp,
        ),
    )
    video = write_imagine_asset(
        tmp_path,
        ArtSpec(
            stem="same",
            kind="video",
            prompt=prompt,
            body=b"mp4",
            timestamp=stamp,
        ),
    )
    assert image.written is True
    assert video.written is True
    assert image.metadata_path != video.metadata_path
    assert image.metadata_path is not None
    assert video.metadata_path is not None
    image_meta = json.loads(image.metadata_path.read_text(encoding="utf-8"))
    video_meta = json.loads(video.metadata_path.read_text(encoding="utf-8"))
    assert image_meta["kind"] == "image"
    assert image_meta["model"] == MODEL_IMAGE
    assert video_meta["kind"] == "video"
    assert video_meta["model"] == MODEL_VIDEO


def test_writer_does_not_open_sockets(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", boom)
    spec = ArtSpec(
        stem="logo",
        kind="image",
        prompt=build_wild_meme_prompt(
            character="a round cartoon pigeon in a paper hat",
            joke_line="The pigeons elected a bench.",
            game_title="Bench Birds",
        ),
        body=b"fixture-png-bytes",
        timestamp=datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
    )
    result = write_imagine_asset(tmp_path, spec)
    assert result.written is True
