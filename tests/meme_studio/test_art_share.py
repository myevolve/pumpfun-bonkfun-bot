"""Procedural fallback art: original PNG/SVG, no third-party images."""

from __future__ import annotations

import json

from meme_studio.art.providers.procedural import (
    BANNER_SIZE,
    LOGO_SIZE,
    render_procedural,
)
from meme_studio.game.concept import load_concept
from tests.meme_studio.support import (
    FIXTURE,
    decode_png_rgb,
    load_fixture_dict,
    write_concept,
)

# ruff: noqa: ANN001, PLC0415, PLR2004, S101, S314


def _ink_pixels(rgb: bytes, width: int, height: int) -> int:
    ink = bytes.fromhex("f4f1de")
    count = 0
    start_row = max(0, height - 24)
    for y in range(start_row, height):
        for x in range(width):
            index = (y * width + x) * 3
            if rgb[index : index + 3] == ink:
                count += 1
    return count


def test_logo_and_banner_match_stage_four_sizes(tmp_path) -> None:
    concept = load_concept(FIXTURE)
    logo = render_procedural(
        concept, tmp_path / "logo.png", kind="logo", game_only=False
    )
    banner = render_procedural(
        concept, tmp_path / "banner.png", kind="banner", game_only=False
    )
    logo_w, logo_h, _logo_rgb = decode_png_rgb(logo.path.read_bytes())
    banner_w, banner_h, _banner_rgb = decode_png_rgb(banner.path.read_bytes())
    assert (logo_w, logo_h) == LOGO_SIZE == (512, 512)
    assert (banner_w, banner_h) == BANNER_SIZE == (1500, 500)
    meta = json.loads(logo.meta_path.read_text(encoding="utf-8"))
    assert meta["provider"] == "procedural"
    assert meta["posted"] is False
    assert "<svg" in (tmp_path / "logo.svg").read_text(encoding="utf-8") or meta["svg"]


def test_sprite_is_deterministic_and_uses_the_palette(tmp_path) -> None:
    concept = load_concept(FIXTURE)
    other_payload = load_fixture_dict()
    character = dict(other_payload["character"])  # type: ignore[arg-type]
    character["name"] = "Pebble"
    other_payload["character"] = character
    other = load_concept(write_concept(tmp_path, other_payload))
    first = render_procedural(
        concept, tmp_path / "a.png", kind="sprite", game_only=True
    )
    second = render_procedural(
        concept, tmp_path / "b.png", kind="sprite", game_only=True
    )
    third = render_procedural(other, tmp_path / "c.png", kind="sprite", game_only=True)
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.path.read_bytes() != third.path.read_bytes()
    _w, _h, rgb = decode_png_rgb(first.path.read_bytes())
    assert rgb[0:3] == bytes.fromhex("1b1f3b")


def test_game_only_share_image_differs_and_omits_ticker(tmp_path) -> None:
    concept = load_concept(FIXTURE)
    full = render_procedural(
        concept, tmp_path / "full.png", kind="share", game_only=False
    )
    hidden = render_procedural(
        concept, tmp_path / "hidden.png", kind="share", game_only=True
    )
    assert full.path.read_bytes() != hidden.path.read_bytes()
    hidden_meta = json.loads(hidden.meta_path.read_text(encoding="utf-8"))
    full_meta = json.loads(full.meta_path.read_text(encoding="utf-8"))
    assert "SOCKDRIFT" not in json.dumps(hidden_meta)
    assert "SOCKDRIFT" in json.dumps(full_meta)
    assert "Not investment advice" in json.dumps(hidden_meta)
    assert "We created this coin, hold a dev bag" in json.dumps(full_meta)
    width, height, rgb = decode_png_rgb(full.path.read_bytes())
    assert _ink_pixels(rgb, width, height) > 30


def test_procedural_svg_is_namespaced_and_escaped(tmp_path) -> None:
    import xml.etree.ElementTree as ET

    payload = load_fixture_dict()
    character = dict(payload["character"])  # type: ignore[arg-type]
    character["name"] = 'Nimbus" onload="alert(1)'
    payload["character"] = character
    concept = load_concept(write_concept(tmp_path, payload, "svg.json"))
    record = render_procedural(
        concept, tmp_path / "sprite.png", kind="sprite", game_only=True
    )
    svg = record.path.with_suffix(".svg").read_text(encoding="utf-8")
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert "onload" not in root.attrib
    assert "<image" not in svg
    assert "href=" not in svg
