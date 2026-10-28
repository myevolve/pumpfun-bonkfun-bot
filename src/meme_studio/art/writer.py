"""Write an asset and a metadata sidecar after the media linter passes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from meme_studio.compliance.lint import compliance_lint
from meme_studio.compliance.models import LintResult, MediaMetadata
from meme_studio.errors import NaiveTimeError, UnknownMediaKindError, UnsafeStemError

if TYPE_CHECKING:
    from datetime import datetime

PROVIDER = "xai-grok-imagine"
MODEL_IMAGE = "grok-imagine"
MODEL_VIDEO = "grok-imagine-video"
_SUFFIXES = {"image": ".png", "video": ".mp4"}
_MODELS = {"image": MODEL_IMAGE, "video": MODEL_VIDEO}


@dataclass(frozen=True)
class ArtSpec:
    """Bytes plus the prompt that would be sent to Grok Imagine in a later phase."""

    stem: str
    kind: str
    prompt: str
    body: bytes
    timestamp: datetime
    overlays: tuple[str, ...] = ()
    channel: str = ""
    game_only: bool = False
    ticker: str = ""
    mint: str = ""


@dataclass(frozen=True)
class ArtWriteResult:
    """Where the asset landed, or a lint failure with nothing written."""

    written: bool
    asset_path: Path | None
    metadata_path: Path | None
    lint: LintResult


def write_imagine_asset(directory: Path, spec: ArtSpec) -> ArtWriteResult:
    """Lint the prompt and, on pass, store fixture bytes beside a metadata file.

    The metadata names xAI Grok Imagine. The bytes come from the caller. This
    function does not open a socket.
    """
    if spec.kind not in _MODELS:
        raise UnknownMediaKindError
    metadata = _metadata(spec)
    lint = compliance_lint(metadata)
    if not lint.passed:
        return ArtWriteResult(
            written=False,
            asset_path=None,
            metadata_path=None,
            lint=lint,
        )
    directory.mkdir(parents=True, exist_ok=True)
    asset_path, metadata_path = _contained_paths(directory, spec.stem, spec.kind)
    asset_path.write_bytes(spec.body)
    metadata_path.write_text(
        json.dumps(_sidecar(metadata, spec, lint), indent=2) + "\n",
        encoding="utf-8",
    )
    return ArtWriteResult(
        written=True,
        asset_path=asset_path,
        metadata_path=metadata_path,
        lint=lint,
    )


def _contained_paths(directory: Path, stem: str, kind: str) -> tuple[Path, Path]:
    if not _stem_is_single_name(stem):
        raise UnsafeStemError
    suffix = _SUFFIXES[kind]
    root = directory.resolve()
    asset_path = (directory / f"{stem}{suffix}").resolve()
    metadata_path = (directory / f"{stem}{suffix}.meta.json").resolve()
    if asset_path.parent != root or metadata_path.parent != root:
        raise UnsafeStemError
    return asset_path, metadata_path


def _stem_is_single_name(stem: str) -> bool:
    if not stem or stem.strip() != stem:
        return False
    if stem in {".", ".."}:
        return False
    path = Path(stem)
    if path.is_absolute():
        return False
    return len(path.parts) == 1 and "/" not in stem and "\\" not in stem


def _metadata(spec: ArtSpec) -> MediaMetadata:
    return MediaMetadata(
        prompt=spec.prompt,
        provider=PROVIDER,
        model=_MODELS[spec.kind],
        timestamp=_stamp(spec.timestamp),
        kind=spec.kind,
        channel=spec.channel,
        overlays=spec.overlays,
        game_only=spec.game_only,
        ticker=spec.ticker,
        mint=spec.mint,
    )


def _stamp(timestamp: datetime) -> str:
    if timestamp.tzinfo is None:
        raise NaiveTimeError
    return timestamp.astimezone(UTC).isoformat()


def _sidecar(
    metadata: MediaMetadata, spec: ArtSpec, lint: LintResult
) -> dict[str, object]:
    return {
        "prompt": metadata.prompt,
        "provider": metadata.provider,
        "model": metadata.model,
        "timestamp": metadata.timestamp,
        "kind": spec.kind,
        "overlays": list(spec.overlays),
        "channel": spec.channel,
        "game_only": spec.game_only,
        "lint": {
            "status": lint.status,
            "reasons": list(lint.reasons),
        },
    }
