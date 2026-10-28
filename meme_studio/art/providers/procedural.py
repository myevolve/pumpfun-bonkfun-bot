"""Original geometric art used when Imagine is unavailable."""
# ruff: noqa: PLR0913, PLR0917

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from meme_studio.art.png import write_rgb_png
from meme_studio.art.providers.base import AssetRecord, meta_path_for

if TYPE_CHECKING:
    from pathlib import Path

    from meme_studio.game.concept import Concept

LOGO_SIZE = (512, 512)
BANNER_SIZE = (1500, 500)
_SPRITE_SIZE = (256, 256)
_SHARE_SIZE = (640, 640)
_BACKGROUND_SIZE = (360, 640)
_SIZES = {
    "logo": LOGO_SIZE,
    "banner": BANNER_SIZE,
    "sprite": _SPRITE_SIZE,
    "share": _SHARE_SIZE,
    "background": _BACKGROUND_SIZE,
}
_BAND = 16
_ADVICE = "Parody and entertainment only. Not investment advice."


def render_procedural(
    concept: Concept,
    dest: Path,
    *,
    kind: str,
    game_only: bool,
) -> AssetRecord:
    """Draw a deterministic PNG and SVG for ``kind``.

    Args:
        concept: Validated concept. The character name seeds the shape.
        dest: PNG destination.
        kind: ``sprite``, ``background``, ``share``, ``logo``, or ``banner``.
        game_only: Omit the ticker from share-card metadata and pixels.

    Returns:
        The asset record. Cost is zero and nothing is posted.
    """
    width, height = _SIZES.get(kind, _SPRITE_SIZE)
    rgb = _draw(concept, kind=kind, game_only=game_only, width=width, height=height)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_rgb_png(dest, width, height, bytes(rgb))
    dest.with_suffix(".svg").write_text(_svg(concept), encoding="utf-8")
    meta = {
        "provider": "procedural",
        "kind": kind,
        "posted": False,
        "cache_hit": False,
        "cost_usd": 0,
        "lines": _lines(concept, kind=kind, game_only=game_only),
        "style": concept.style,
    }
    meta_path = meta_path_for(dest)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return AssetRecord(
        path=dest,
        meta_path=meta_path,
        provider="procedural",
        kind=kind,
        cache_hit=False,
        cost_usd=0,
    )


def _lines(concept: Concept, *, kind: str, game_only: bool) -> list[str]:
    lines = [concept.title, concept.character.name, _ADVICE]
    if kind == "share" and not game_only:
        lines.append(concept.ticker)
    return lines


def _mix(name: str) -> int:
    return hashlib.sha256(name.encode()).digest()[0]


def _hex(value: str) -> tuple[int, int, int]:
    raw = value.removeprefix("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _draw(
    concept: Concept,
    *,
    kind: str,
    game_only: bool,
    width: int,
    height: int,
) -> bytearray:
    background = _hex(concept.palette.background)
    primary = _hex(concept.palette.primary)
    accent = _hex(concept.palette.accent)
    ink = _hex(concept.palette.ink)
    buf = bytearray(bytes(background) * (width * height))
    mix = _mix(concept.character.name)
    radius = max(12, min(width, height) // 5)
    cx = width // 3 + (mix % max(1, width // 5))
    cy = height // 2
    _fill_circle(buf, width, height, cx, cy, radius, primary)
    _fill_circle(
        buf, width, height, cx - radius // 3, cy - radius // 5, max(2, radius // 8), ink
    )
    _fill_circle(
        buf, width, height, cx + radius // 3, cy - radius // 5, max(2, radius // 8), ink
    )
    if kind == "share":
        band = accent if game_only else primary
        _fill_rect(buf, width, height, 0, height - _BAND, width, _BAND, band)
    if kind == "background":
        _fill_rect(
            buf, width, height, 0, int(height * 0.72), width, height // 8, accent
        )
    return buf


def _fill_rect(
    buf: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    w: int,
    h: int,
    color: tuple[int, int, int],
) -> None:
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width, x + w)
    y1 = min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    span = bytes(color) * (x1 - x0)
    for row in range(y0, y1):
        start = (row * width + x0) * 3
        buf[start : start + len(span)] = span


def _fill_circle(
    buf: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    pixel = bytes(color)
    r2 = radius * radius
    for yy in range(cy - radius, cy + radius + 1):
        if yy < 0 or yy >= height:
            continue
        for xx in range(cx - radius, cx + radius + 1):
            if xx < 0 or xx >= width:
                continue
            if (xx - cx) ** 2 + (yy - cy) ** 2 <= r2:
                start = (yy * width + xx) * 3
                buf[start : start + 3] = pixel


def _svg(concept: Concept) -> str:
    name = concept.character.name.replace("&", "").replace("<", "").replace(">", "")
    return (
        f'<svg viewBox="0 0 64 64" role="img" aria-label="{name}">'
        f'<rect width="64" height="64" fill="{concept.palette.background}"/>'
        f'<circle cx="32" cy="34" r="18" fill="{concept.palette.primary}"/>'
        f'<circle cx="26" cy="30" r="2" fill="{concept.palette.ink}"/>'
        f'<circle cx="38" cy="30" r="2" fill="{concept.palette.ink}"/>'
        "</svg>"
    )
