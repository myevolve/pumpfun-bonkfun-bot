"""xAI Grok Imagine provider.

Image generation uses ``POST /v1/images/generations`` with model
``grok-imagine-image-2.0`` ($0.04/image, quality ``low``, resolution ``1k``,
``response_format`` ``b64_json``).

Video generation uses ``POST /v1/videos/generations`` then
``GET /v1/videos/{request_id}`` with model ``grok-imagine-video-1.5``
($0.080/sec, duration clamped to 1-15, resolution ``480p``).

Docs:
https://docs.x.ai/developers/model-capabilities/images/generation
https://docs.x.ai/developers/rest-api-reference/inference/images
https://docs.x.ai/developers/model-capabilities/video/overview
https://docs.x.ai/developers/rest-api-reference/inference/videos
https://docs.x.ai/developers/models

The API key is read from ``XAI_API_KEY`` by callers. It is sent only as an
Authorization header to ``api.x.ai`` and is never written to logs, metadata,
or request URLs.
"""
# ruff: noqa: N818, PLR0913, S310, TC003, TRY003

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from meme_studio.art.guardrails import assert_safe_text, post_check_asset
from meme_studio.art.providers.base import AssetRecord, meta_path_for

logger = logging.getLogger(__name__)

IMAGE_MODEL = "grok-imagine-image-2.0"
VIDEO_MODEL = "grok-imagine-video-1.5"
IMAGE_ENDPOINT = "https://api.x.ai/v1/images/generations"
VIDEO_ENDPOINT = "https://api.x.ai/v1/videos/generations"
_API_PREFIX = "https://api.x.ai/"
USD_TICKS = 10_000_000_000
IMAGE_ESTIMATE_TICKS = 400_000_000
VIDEO_TICKS_PER_SEC = 800_000_000
RETRY_SLEEP_S = 0.25
POLL_SLEEP_S = 2.0
_MAX_POLLS = 30
_MIN_DURATION_S = 1
_MAX_DURATION_S = 15
_HTTP_TOO_MANY = 429
_HTTP_BAD = 400
_HTTP_SERVER = 500


class ImagineError(RuntimeError):
    """Imagine failed. The message is redacted."""


class CapExceeded(ImagineError):
    """The per-day image, video, or cost cap would be exceeded."""


@dataclass(frozen=True)
class HttpResponse:
    """One mocked or live HTTP response."""

    status: int
    body: bytes


class Transport(Protocol):
    """HTTP transport. Tests pass a fake; production uses urllib."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        """Perform one HTTP request."""
        ...


@dataclass
class ImagineConfig:
    """Caps, timeouts, and models for one Imagine client."""

    api_key: str
    cache_dir: Path
    timeout_s: float = 30.0
    max_attempts: int = 3
    daily_image_cap: int = 24
    daily_video_cap: int = 2
    daily_cost_cap_usd: float = 2.0
    image_model: str = IMAGE_MODEL
    video_model: str = VIDEO_MODEL
    clip_seconds: int = 10

    def __repr__(self) -> str:
        """Return a representation that omits the API key."""
        return (
            f"ImagineConfig(cache_dir={self.cache_dir!r}, timeout_s={self.timeout_s}, "
            f"max_attempts={self.max_attempts})"
        )


def api_key_from_env() -> str | None:
    """Return ``XAI_API_KEY`` when it is non-empty.

    Returns:
        The key, or None when it is unset.
    """
    value = os.environ.get("XAI_API_KEY", "").strip()
    return value or None


class UrllibTransport:
    """Stdlib transport. Does not log the URL or headers."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        """Perform one HTTP request.

        Args:
            method: HTTP method.
            url: Absolute URL. Must not contain the API key.
            headers: Request headers.
            body: Optional body.
            timeout: Socket timeout in seconds.

        Returns:
            Status and body. HTTP errors become responses instead of raises.
        """
        if not url.startswith("https://"):
            raise ImagineError("https")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())


class ImagineProvider:
    """Generate images and clips through Grok Imagine."""

    def __init__(
        self,
        config: ImagineConfig,
        transport: Transport,
        *,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        post_check: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        """Store collaborators. The key stays on the config object.

        Args:
            config: Key, caps, and models.
            transport: HTTP implementation.
            sleeper: Retry and poll delay. Defaults to ``time.sleep``.
            clock: UTC clock used for the daily ledger.
            post_check: Extra hook run after the built-in guardrails.
        """
        self.config = config
        self._transport = transport
        self._sleeper = time.sleep if sleeper is None else sleeper
        self._clock = clock or (lambda: datetime.now(UTC))
        self._post_check = post_check

    def generate_image(
        self,
        prompt: str,
        dest: Path,
        *,
        kind: str,
        aspect_ratio: str,
        style: str,
        creative_prompt: str,
    ) -> AssetRecord:
        """Generate one image, or copy it from the cache.

        Args:
            prompt: Full prompt sent to the API.
            dest: Where to write the PNG.
            kind: Asset kind recorded in metadata.
            aspect_ratio: Imagine aspect ratio.
            style: Style preset name.
            creative_prompt: Portion checked by the guardrails.

        Returns:
            The written asset.

        Raises:
            CapExceeded: The daily budget would be exceeded.
            ImagineError: The request failed.
            GuardrailError: Moderation or the post-check rejected the image.
        """
        assert_safe_text(creative_prompt, field="creative_prompt")
        digest = _digest(
            {
                "model": self.config.image_model,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": "1k",
                "quality": "low",
                "kind": kind,
            }
        )
        cached = self._read_cache(digest)
        if cached is not None:
            return self._write_asset(
                dest, cached[0], cached[1], kind=kind, cache_hit=True
            )
        self._reserve(images=1, videos=0, estimate_ticks=IMAGE_ESTIMATE_TICKS)
        payload = self._request_json(
            "POST",
            IMAGE_ENDPOINT,
            {
                "model": self.config.image_model,
                "prompt": prompt,
                "response_format": "b64_json",
                "n": 1,
                "quality": "low",
                "resolution": "1k",
                "aspect_ratio": aspect_ratio,
            },
        )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ImagineError("image response missing data")
        encoded = data[0].get("b64_json")
        if not isinstance(encoded, str):
            raise ImagineError("image response missing b64_json")
        blob = base64.b64decode(encoded)
        respect = data[0].get("respect_moderation", True)
        ticks = _usage_ticks(payload, IMAGE_ESTIMATE_TICKS)
        meta = _meta(
            model=self.config.image_model,
            prompt=prompt,
            creative_prompt=creative_prompt,
            style=style,
            kind=kind,
            aspect_ratio=aspect_ratio,
            ticks=ticks,
            respect=respect is not False,
        )
        self._commit(images=1, videos=0, ticks=ticks)
        post_check_asset(meta, hook=self._post_check)
        self._write_cache(digest, blob, meta)
        return self._write_asset(dest, blob, meta, kind=kind, cache_hit=False)

    def generate_video(
        self,
        prompt: str,
        dest: Path,
        *,
        aspect_ratio: str,
        style: str,
        creative_prompt: str,
        duration_s: int | None = None,
    ) -> AssetRecord:
        """Generate one clip, polling until the video URL is ready.

        Args:
            prompt: Full prompt. Callers keep tickers out of teaser prompts.
            dest: MP4 destination.
            aspect_ratio: Usually ``9:16``.
            style: Style preset name.
            creative_prompt: Portion checked by the guardrails.
            duration_s: Requested seconds. Clamped to 1-15. Defaults to 10.

        Returns:
            The written asset.

        Raises:
            CapExceeded: The daily budget would be exceeded.
            ImagineError: The request failed.
            GuardrailError: Moderation or the post-check rejected the clip.
        """
        assert_safe_text(creative_prompt, field="creative_prompt")
        requested = self.config.clip_seconds if duration_s is None else duration_s
        duration = min(_MAX_DURATION_S, max(_MIN_DURATION_S, int(requested)))
        digest = _digest(
            {
                "model": self.config.video_model,
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": "480p",
                "duration": duration,
                "kind": "clip",
            }
        )
        cached = self._read_cache(digest)
        if cached is not None:
            return self._write_asset(
                dest, cached[0], cached[1], kind="clip", cache_hit=True
            )
        estimate = duration * VIDEO_TICKS_PER_SEC
        self._reserve(images=0, videos=1, estimate_ticks=estimate)
        started = self._request_json(
            "POST",
            VIDEO_ENDPOINT,
            {
                "model": self.config.video_model,
                "prompt": prompt,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "resolution": "480p",
            },
        )
        request_id = started.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ImagineError("video response missing request_id")
        finished = self._poll_video(request_id)
        video = finished.get("video")
        if not isinstance(video, dict):
            raise ImagineError("video response missing video")
        respect = video.get("respect_moderation", True)
        ticks = _usage_ticks(finished, estimate)
        meta = _meta(
            model=str(finished.get("model") or self.config.video_model),
            prompt=prompt,
            creative_prompt=creative_prompt,
            style=style,
            kind="clip",
            aspect_ratio=aspect_ratio,
            ticks=ticks,
            respect=respect is not False,
        )
        meta["request_id"] = request_id
        self._commit(images=0, videos=1, ticks=ticks)
        post_check_asset(meta, hook=self._post_check)
        url = video.get("url")
        if not isinstance(url, str) or not url:
            raise ImagineError("video response missing url")
        blob = self._download(url)
        self._write_cache(digest, blob, meta)
        return self._write_asset(dest, blob, meta, kind="clip", cache_hit=False)

    def _poll_video(self, request_id: str) -> dict[str, object]:
        url = f"https://api.x.ai/v1/videos/{request_id}"
        for _poll in range(_MAX_POLLS):
            payload = self._request_json("GET", url, None)
            status = str(payload.get("status") or "")
            if status == "pending":
                self._sleeper(POLL_SLEEP_S)
                continue
            if status in {"failed", "expired"}:
                raise ImagineError(self._redact(status))
            if status == "done":
                return payload
            raise ImagineError(self._redact(f"unexpected video status {status}"))
        raise ImagineError("video poll timed out")

    def _download(self, url: str) -> bytes:
        if self.config.api_key and self.config.api_key in url:
            raise ImagineError("refusing a download url that contains the api key")
        response = self._call("GET", url, None)
        if response.status >= _HTTP_BAD:
            raise ImagineError(f"status {response.status}")
        return response.body

    def _request_json(
        self, method: str, url: str, payload: dict[str, object] | None
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode()
        response = self._call(method, url, body)
        try:
            parsed = json.loads(response.body.decode() or "{}")
        except json.JSONDecodeError:
            raise ImagineError("response was not json") from None
        if not isinstance(parsed, dict):
            raise ImagineError("response was not an object")
        return parsed

    def _call(self, method: str, url: str, body: bytes | None) -> HttpResponse:
        delay = RETRY_SLEEP_S
        last = "request failed"
        for attempt in range(self.config.max_attempts):
            try:
                response = self._transport.request(
                    method,
                    url,
                    _headers(url, self.config.api_key, json_body=body is not None),
                    body,
                    self.config.timeout_s,
                )
            except (OSError, TimeoutError) as exc:
                last = self._redact(str(exc))
                if attempt + 1 >= self.config.max_attempts:
                    logger.warning("imagine request failed: %s", last)
                    raise ImagineError(last) from None
                self._sleeper(delay)
                delay *= 2
                continue
            if response.status == _HTTP_TOO_MANY or response.status >= _HTTP_SERVER:
                last = f"status {response.status}"
                if attempt + 1 >= self.config.max_attempts:
                    logger.warning("imagine request failed: %s", last)
                    raise ImagineError(last)
                self._sleeper(delay)
                delay *= 2
                continue
            if response.status >= _HTTP_BAD:
                message = self._redact(f"status {response.status}")
                logger.warning("imagine request failed: %s", message)
                raise ImagineError(message)
            return response
        logger.warning("imagine request failed: %s", last)
        raise ImagineError(self._redact(last))

    def _reserve(self, *, images: int, videos: int, estimate_ticks: int) -> None:
        ledger = self._read_ledger()
        if ledger[0] + images > self.config.daily_image_cap:
            raise CapExceeded("daily image cap reached")
        if ledger[1] + videos > self.config.daily_video_cap:
            raise CapExceeded("daily video cap reached")
        cap_ticks = round(self.config.daily_cost_cap_usd * USD_TICKS)
        if ledger[2] + estimate_ticks > cap_ticks:
            raise CapExceeded("daily cost cap reached")

    def _commit(self, *, images: int, videos: int, ticks: int) -> None:
        images_used, videos_used, cost = self._read_ledger()
        self._write_ledger(images_used + images, videos_used + videos, cost + ticks)

    def _ledger_path(self) -> Path:
        day = self._clock().date().isoformat()
        return self.config.cache_dir / f"usage-{day}.json"

    def _read_ledger(self) -> tuple[int, int, int]:
        path = self._ledger_path()
        if not path.is_file():
            return (0, 0, 0)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return (int(raw["images"]), int(raw["videos"]), int(raw["cost_ticks"]))

    def _write_ledger(self, images: int, videos: int, cost_ticks: int) -> None:
        path = self._ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": self._clock().date().isoformat(),
            "images": images,
            "videos": videos,
            "cost_ticks": cost_ticks,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _read_cache(self, digest: str) -> tuple[bytes, dict[str, object]] | None:
        folder = self.config.cache_dir / "blobs"
        blob_path = folder / f"{digest}.bin"
        meta_path = folder / f"{digest}.json"
        if not blob_path.is_file() or not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return None
        return (blob_path.read_bytes(), meta)

    def _write_cache(self, digest: str, blob: bytes, meta: dict[str, object]) -> None:
        folder = self.config.cache_dir / "blobs"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{digest}.bin").write_bytes(blob)
        (folder / f"{digest}.json").write_text(json.dumps(meta), encoding="utf-8")

    def _write_asset(
        self,
        dest: Path,
        blob: bytes,
        meta: dict[str, object],
        *,
        kind: str,
        cache_hit: bool,
    ) -> AssetRecord:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        stored = dict(meta)
        stored["cache_hit"] = cache_hit
        if cache_hit:
            stored["cost_usd"] = 0
        meta_path = meta_path_for(dest)
        meta_path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        cost = float(stored["cost_usd"])
        return AssetRecord(dest, meta_path, "imagine", kind, cache_hit, cost)

    def _redact(self, text: str) -> str:
        key = self.config.api_key
        if key and key in text:
            return text.replace(key, "[redacted]")
        return text


def _headers(url: str, api_key: str, *, json_body: bool) -> dict[str, str]:
    headers: dict[str, str] = {}
    if json_body:
        headers["Content-Type"] = "application/json"
    if url.startswith(_API_PREFIX) and api_key and api_key not in url:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _usage_ticks(payload: dict[str, object], estimate: int) -> int:
    usage = payload.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("cost_in_usd_ticks"), int):
        return int(usage["cost_in_usd_ticks"])
    return estimate


def _meta(
    *,
    model: str,
    prompt: str,
    creative_prompt: str,
    style: str,
    kind: str,
    aspect_ratio: str,
    ticks: int,
    respect: bool,
) -> dict[str, object]:
    return {
        "provider": "imagine",
        "model": model,
        "prompt": prompt,
        "creative_prompt": creative_prompt,
        "style": style,
        "kind": kind,
        "aspect_ratio": aspect_ratio,
        "cache_hit": False,
        "cost_usd": ticks / USD_TICKS,
        "posted": False,
        "respect_moderation": respect,
    }
