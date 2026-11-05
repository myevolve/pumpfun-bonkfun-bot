"""Art for draft parody games and fixture-backed Grok Imagine assets.

``generate`` drives Grok Imagine with procedural SVG/PNG fallbacks when
``XAI_API_KEY`` is unset or a request fails. ``writer`` stores caller-supplied
bytes beside a metadata sidecar after the media linter passes; it opens no
socket.
"""

from __future__ import annotations

from meme_studio.art.prompts import build_wild_meme_prompt
from meme_studio.art.writer import (
    MODEL_IMAGE,
    MODEL_VIDEO,
    PROVIDER,
    ArtSpec,
    ArtWriteResult,
    write_imagine_asset,
)

__all__ = [
    "MODEL_IMAGE",
    "MODEL_VIDEO",
    "PROVIDER",
    "ArtSpec",
    "ArtWriteResult",
    "build_wild_meme_prompt",
    "write_imagine_asset",
]
