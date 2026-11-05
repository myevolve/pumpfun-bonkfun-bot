"""Shared asset record for Imagine and the procedural fallback."""
# ruff: noqa: TC003

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AssetRecord:
    """One generated file plus its audit metadata."""

    path: Path
    meta_path: Path
    provider: str
    kind: str
    cache_hit: bool
    cost_usd: float


def meta_path_for(asset: Path) -> Path:
    """Return the audit sidecar path for an asset file.

    Args:
        asset: Image or clip path.

    Returns:
        ``asset.name`` with ``.meta.json`` appended.
    """
    return asset.with_name(asset.name + ".meta.json")
