"""Fixture-backed Grok Imagine assets and sidecar metadata.

Phase 0 records the provider name and stores caller-supplied bytes. It does
not call xAI, and it does not contain a client that can send a prompt.
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
