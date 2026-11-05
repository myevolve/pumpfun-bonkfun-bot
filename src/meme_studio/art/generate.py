"""Choose Imagine when a key is present, and fall back to procedural art."""
# ruff: noqa: PLR0913, TC001, TC003

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meme_studio.art.clip import ClipResult
from meme_studio.art.guardrails import GuardrailError, assert_concept_safe
from meme_studio.art.prompts import (
    build_image_prompt,
    build_video_prompt,
    creative_portion,
)
from meme_studio.art.providers.base import AssetRecord
from meme_studio.art.providers.imagine import (
    DEFAULT_DAILY_COST_USD,
    DEFAULT_DAILY_IMAGE_CAP,
    DEFAULT_DAILY_VIDEO_CAP,
    ImagineConfig,
    ImagineError,
    ImagineProvider,
    Transport,
    UrllibTransport,
    api_key_from_env,
    default_budget_dir,
)
from meme_studio.art.providers.procedural import render_procedural
from meme_studio.game.concept import Concept

logger = logging.getLogger(__name__)

_IMAGE_KINDS: tuple[tuple[str, str], ...] = (
    ("sprite", "1:1"),
    ("background", "9:16"),
    ("share", "1:1"),
    ("logo", "1:1"),
    ("banner", "5:2"),
)


@dataclass(frozen=True)
class ArtPack:
    """Images and optional clip for one concept."""

    sprite: AssetRecord
    background: AssetRecord
    share: AssetRecord
    logo: AssetRecord
    banner: AssetRecord
    clip: ClipResult
    fallback_reason: str | None
    network_used: bool


def generate_art(
    concept: Concept,
    out_dir: Path,
    *,
    game_only: bool,
    transport: object | None = None,
    cache_dir: Path | None = None,
    skip_clip: bool = False,
    post_check: Callable[[dict[str, object]], None] | None = None,
    max_attempts: int = 3,
    timeout_s: float = 30.0,
    daily_image_cap: int | None = None,
    daily_video_cap: int | None = None,
    daily_cost_cap_usd: float | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ArtPack:
    """Generate the asset pack. Drafts only: nothing is posted.

    Args:
        concept: Validated concept.
        out_dir: Directory for png, svg, and clip files.
        game_only: Omit ticker lettering from share art.
        transport: Imagine HTTP transport. Required for tests; urllib otherwise.
        cache_dir: Imagine cache. Defaults to a folder under ``out_dir``.
        skip_clip: Do not generate a video.
        post_check: Extra rejection hook for Imagine outputs.
        max_attempts: HTTP attempts per request.
        timeout_s: Per-request timeout.
        daily_image_cap: Override the image count cap.
        daily_video_cap: Override the video count cap.
        daily_cost_cap_usd: Override the USD cap.
        sleeper: Retry delay, injected by tests.
        clock: UTC clock for the daily ledger.

    Returns:
        The pack. Imagine failures are recorded and replaced with procedural art.

    Raises:
        GuardrailError: The concept text itself is unsafe.
    """
    assert_concept_safe(concept)
    out_dir.mkdir(parents=True, exist_ok=True)
    reasons: list[str] = []
    client: Transport = transport if transport is not None else UrllibTransport()  # type: ignore[assignment]
    meter = _Meter(client)
    imagine = _imagine(
        out_dir,
        transport=meter,
        cache_dir=cache_dir,
        post_check=post_check,
        max_attempts=max_attempts,
        timeout_s=timeout_s,
        daily_image_cap=daily_image_cap,
        daily_video_cap=daily_video_cap,
        daily_cost_cap_usd=daily_cost_cap_usd,
        sleeper=sleeper,
        clock=clock,
        reasons=reasons,
    )
    assets = {
        kind: _one_image(
            imagine,
            concept,
            out_dir / f"{kind}.png",
            kind=kind,
            aspect_ratio=ratio,
            game_only=game_only,
            reasons=reasons,
        )
        for kind, ratio in _IMAGE_KINDS
    }
    clip = _one_clip(
        imagine,
        concept,
        out_dir / "clip.mp4",
        game_only=game_only,
        skip_clip=skip_clip,
        reasons=reasons,
    )
    fallback = "; ".join(reasons) if reasons else None
    return ArtPack(
        sprite=assets["sprite"],
        background=assets["background"],
        share=assets["share"],
        logo=assets["logo"],
        banner=assets["banner"],
        clip=clip,
        fallback_reason=fallback,
        network_used=meter.used,
    )


class _Meter:
    """Count transport calls without changing the response."""

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.used = False

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> object:
        self.used = True
        return self.inner.request(method, url, headers, body, timeout)


def _imagine(
    out_dir: Path,
    *,
    transport: object | None,
    cache_dir: Path | None,
    post_check: Callable[[dict[str, object]], None] | None,
    max_attempts: int,
    timeout_s: float,
    daily_image_cap: int | None,
    daily_video_cap: int | None,
    daily_cost_cap_usd: float | None,
    sleeper: Callable[[float], None] | None,
    clock: Callable[[], datetime] | None,
    reasons: list[str],
) -> ImagineProvider | None:
    key = api_key_from_env()
    if not key:
        reasons.append("XAI_API_KEY not set")
        return None
    config = ImagineConfig(
        api_key=key,
        cache_dir=cache_dir or (out_dir / "imagine-cache"),
        budget_dir=default_budget_dir(),
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        daily_image_cap=(
            DEFAULT_DAILY_IMAGE_CAP if daily_image_cap is None else daily_image_cap
        ),
        daily_video_cap=(
            DEFAULT_DAILY_VIDEO_CAP if daily_video_cap is None else daily_video_cap
        ),
        daily_cost_cap_usd=(
            DEFAULT_DAILY_COST_USD if daily_cost_cap_usd is None else daily_cost_cap_usd
        ),
    )
    client = transport if transport is not None else UrllibTransport()
    return ImagineProvider(
        config,
        client,  # type: ignore[arg-type]
        sleeper=sleeper,
        clock=clock,
        post_check=post_check,
    )


def _one_image(
    imagine: ImagineProvider | None,
    concept: Concept,
    dest: Path,
    *,
    kind: str,
    aspect_ratio: str,
    game_only: bool,
    reasons: list[str],
) -> AssetRecord:
    if imagine is not None:
        try:
            return imagine.generate_image(
                build_image_prompt(concept, kind=kind, game_only=game_only),
                dest,
                kind=kind,
                aspect_ratio=aspect_ratio,
                style=concept.style,
                creative_prompt=creative_portion(
                    concept, kind=kind, game_only=game_only, video=False
                ),
            )
        except (ImagineError, GuardrailError) as exc:
            reasons.append(str(exc))
            logger.warning("imagine image fell back to procedural art: %s", exc)
    return render_procedural(concept, dest, kind=kind, game_only=game_only)


def _one_clip(
    imagine: ImagineProvider | None,
    concept: Concept,
    dest: Path,
    *,
    game_only: bool,
    skip_clip: bool,
    reasons: list[str],
) -> ClipResult:
    if skip_clip:
        return ClipResult("skipped", None, "skip_clip", "none")
    if imagine is None:
        return ClipResult("skipped", None, "XAI_API_KEY not set", "none")
    try:
        record = imagine.generate_video(
            build_video_prompt(concept, game_only=game_only),
            dest,
            aspect_ratio="9:16",
            style=concept.style,
            creative_prompt=creative_portion(
                concept, kind="sprite", game_only=True, video=True
            ),
        )
    except (ImagineError, GuardrailError) as exc:
        reasons.append(str(exc))
        return ClipResult("skipped", None, str(exc), "none")
    return ClipResult("captured", record.path, "imagine", "imagine")
