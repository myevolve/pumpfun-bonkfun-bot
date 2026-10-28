"""Build a draft hub from a concept file. Nothing is posted or deployed."""
# ruff: noqa: ANN401, PLR0913, TC003

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meme_studio.art.clip import DEFAULT_CLIP_SECONDS, capture_gameplay
from meme_studio.art.generate import ArtPack, generate_art
from meme_studio.art.guardrails import GuardrailError, assert_concept_safe
from meme_studio.game.concept import Concept, ConceptError, load_concept
from meme_studio.game.hub import render_index, slugify
from meme_studio.game.page import DISCLOSURE, render_game_page

_CATALOG_SCHEMA = "meme_studio.hub.v1"


@dataclass(frozen=True)
class BuildResult:
    """Paths written by one draft build."""

    out_dir: Path
    game_page: Path
    game_only_page: Path
    hub_index: Path
    theme_path: Path
    manifest_path: Path
    art: ArtPack


def build(
    concept_path: Path,
    out_dir: Path,
    *,
    game_only: bool = False,
    skip_clip: bool = False,
    transport: Any | None = None,
    cache_dir: Path | None = None,
    max_attempts: int = 3,
    daily_image_cap: int | None = None,
    daily_video_cap: int | None = None,
    daily_cost_cap_usd: float | None = None,
) -> BuildResult:
    """Turn ``concept.json`` into a static game page and hub index.

    Args:
        concept_path: Path to the concept file.
        out_dir: Hub root. Existing games stay in the archive.
        game_only: Omit ticker and contract from the public page.
        skip_clip: Do not generate or capture a clip.
        transport: Imagine HTTP transport. Tests inject a fake.
        cache_dir: Imagine cache directory.
        max_attempts: HTTP attempts per Imagine request.
        daily_image_cap: Override the daily image count cap.
        daily_video_cap: Override the daily video count cap.
        daily_cost_cap_usd: Override the daily USD cap.

    Returns:
        Paths and the art pack. ``posted`` and ``deployed`` stay false.

    Raises:
        ConceptError: The concept file is invalid or contains a secret field.
        GuardrailError: The concept text is unsafe to render.
    """
    concept = load_concept(concept_path)
    assert_concept_safe(concept)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(concept.title)
    game_dir = out_dir / "games" / slug
    assets = game_dir / "assets"
    art = generate_art(
        concept,
        assets,
        game_only=game_only,
        transport=transport,
        cache_dir=cache_dir,
        skip_clip=skip_clip,
        max_attempts=max_attempts,
        daily_image_cap=daily_image_cap,
        daily_video_cap=daily_video_cap,
        daily_cost_cap_usd=daily_cost_cap_usd,
    )
    page_path, only_path = _write_pages(concept, game_dir, game_only=game_only)
    art = _maybe_capture(art, only_path, assets / "clip.mp4", skip_clip=skip_clip)
    theme_path = game_dir / "theme.json"
    theme_path.write_text(
        json.dumps(_theme(concept, game_only=game_only), indent=2),
        encoding="utf-8",
    )
    manifest_path = game_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(concept, art, game_only=game_only), indent=2),
        encoding="utf-8",
    )
    _upsert_catalog(
        out_dir / "catalog.json", _entry(concept, slug, game_only=game_only)
    )
    index_path = out_dir / "index.html"
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    index_path.write_text(render_index(list(catalog["games"])), encoding="utf-8")
    return BuildResult(
        out_dir=out_dir,
        game_page=page_path,
        game_only_page=only_path,
        hub_index=index_path,
        theme_path=theme_path,
        manifest_path=manifest_path,
        art=art,
    )


def _write_pages(
    concept: Concept, game_dir: Path, *, game_only: bool
) -> tuple[Path, Path]:
    sprite = "assets/sprite.png"
    background = "assets/background.png"
    only_html = render_game_page(
        concept,
        game_only=True,
        sprite_href=sprite,
        background_href=background,
    )
    public_html = only_html
    if not game_only:
        public_html = render_game_page(
            concept,
            game_only=False,
            sprite_href=sprite,
            background_href=background,
        )
    game_dir.mkdir(parents=True, exist_ok=True)
    only_path = game_dir / "game-only.html"
    page_path = game_dir / "index.html"
    only_path.write_text(only_html, encoding="utf-8")
    page_path.write_text(public_html, encoding="utf-8")
    return page_path, only_path


def _maybe_capture(
    art: ArtPack, html_path: Path, dest: Path, *, skip_clip: bool
) -> ArtPack:
    if skip_clip or art.clip.status == "captured":
        return art
    captured = capture_gameplay(html_path, dest, seconds=DEFAULT_CLIP_SECONDS)
    if captured.status != "captured":
        return art
    return ArtPack(
        sprite=art.sprite,
        background=art.background,
        share=art.share,
        logo=art.logo,
        banner=art.banner,
        clip=captured,
        fallback_reason=art.fallback_reason,
    )


def _theme(concept: Concept, *, game_only: bool) -> dict[str, object]:
    theme: dict[str, object] = {
        "template_id": concept.template_id,
        "title": concept.title,
        "joke": concept.joke,
        "character": concept.character.name,
        "style": concept.style,
        "palette": {
            "background": concept.palette.background,
            "primary": concept.palette.primary,
            "secondary": concept.palette.secondary,
            "accent": concept.palette.accent,
            "ink": concept.palette.ink,
        },
        "sprite": "assets/sprite.png",
        "background": "assets/background.png",
        "disclosure": DISCLOSURE,
    }
    if not game_only:
        theme["ticker"] = concept.ticker
    return theme


def _manifest(concept: Concept, art: ArtPack, *, game_only: bool) -> dict[str, object]:
    return {
        "schema": "meme_studio.game.manifest.v1",
        "phase": "0-shadow",
        "posted": False,
        "deployed": False,
        "network_used": False,
        "game_only": game_only,
        "story_id": concept.story_id,
        "template_id": concept.template_id,
        "disclosure": DISCLOSURE,
        "clip": {
            "status": art.clip.status,
            "reason": art.clip.reason,
            "provider": art.clip.provider,
        },
        "art": {
            "sprite_provider": art.sprite.provider,
            "fallback_reason": art.fallback_reason,
        },
    }


def _entry(concept: Concept, slug: str, *, game_only: bool) -> dict[str, object]:
    entry: dict[str, object] = {
        "story_id": concept.story_id,
        "slug": slug,
        "title": concept.title,
        "date": concept.date,
        "template_id": concept.template_id,
        "page": f"games/{slug}/index.html",
        "game_only": game_only,
    }
    if not game_only:
        entry["ticker"] = concept.ticker
    return entry


def _upsert_catalog(path: Path, entry: dict[str, object]) -> None:
    games: list[dict[str, object]] = []
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        loaded = raw.get("games", [])
        if isinstance(loaded, list):
            games = [item for item in loaded if isinstance(item, dict)]
    games = [item for item in games if item.get("story_id") != entry["story_id"]]
    games.append(entry)
    games.sort(key=lambda item: str(item.get("date", "")), reverse=True)
    payload = {"schema": _CATALOG_SCHEMA, "games": games}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


__all__ = ["BuildResult", "ConceptError", "GuardrailError", "build"]
