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
# ruff: noqa: BLE001, N818, PLR0913, PLR0917, PLR2004, S310, TC003, TRY003

from __future__ import annotations

import base64
import binascii
import fcntl
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
# Operator policy assumptions. These are not limits enforced by xAI.
DEFAULT_DAILY_IMAGE_CAP = 24
DEFAULT_DAILY_VIDEO_CAP = 2
DEFAULT_DAILY_COST_USD = 2.0


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
    budget_dir: Path | None = None
    timeout_s: float = 30.0
    max_attempts: int = 3
    daily_image_cap: int = DEFAULT_DAILY_IMAGE_CAP
    daily_video_cap: int = DEFAULT_DAILY_VIDEO_CAP
    daily_cost_cap_usd: float = DEFAULT_DAILY_COST_USD
    image_model: str = IMAGE_MODEL
    video_model: str = VIDEO_MODEL
    clip_seconds: int = 10

    def __repr__(self) -> str:
        """Return a representation that omits the API key."""
        return (
            f"ImagineConfig(cache_dir={self.cache_dir!r}, timeout_s={self.timeout_s}, "
            f"max_attempts={self.max_attempts})"
        )


def default_budget_dir() -> Path:
    """Return the shared daily Imagine ledger directory.

    ``MEME_STUDIO_IMAGINE_BUDGET_DIR`` overrides the default. The ledger is not
    stored beside one game output, so two builds share one budget.

    Returns:
        Directory for ``usage-YYYY-MM-DD.json``.
    """
    override = os.environ.get("MEME_STUDIO_IMAGINE_BUDGET_DIR", "").strip()
    if override:
        return Path(override)
    return (
        Path(__file__).resolve().parents[3]
        / ".state"
        / "meme-studio"
        / "imagine-budget"
    )


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect leaves https://api.x.ai/."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Rebuild a redirect without forwarding credentials off-origin.

        Args:
            req: Original request.
            fp: Response body, unused.
            code: Redirect status.
            msg: Reason phrase.
            headers: Response headers.
            newurl: Destination URL.

        Returns:
            The redirected request, or None when the status is not a redirect.
        """
        del fp, headers
        redirected = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, None, code, msg, {}, newurl
        )
        if redirected is None:
            return None
        if not newurl.startswith(_API_PREFIX):
            redirected.remove_header("Authorization")
        return redirected


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
        try:
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            opener = urllib.request.build_opener(SafeRedirect)
            with opener.open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                return HttpResponse(status, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except ImagineError:
            raise
        except Exception:
            raise ImagineError("transport") from None


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
        self._reject_bad_key()
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
            cached_meta = dict(cached[1])
            cached_meta["cache_hit"] = True
            post_check_asset(cached_meta, hook=self._post_check)
            return self._write_asset(
                dest, cached[0], cached_meta, kind=kind, cache_hit=True
            )
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
            bill=(1, 0, IMAGE_ESTIMATE_TICKS),
        )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ImagineError("image response missing data")
        encoded = data[0].get("b64_json")
        if not isinstance(encoded, str):
            raise ImagineError("image response missing b64_json")
        blob = _decode_png(encoded)
        respect = _moderation_flag(data[0].get("respect_moderation"))
        observed = _usage_ticks(payload)
        if observed is not None:
            self._reconcile(IMAGE_ESTIMATE_TICKS, observed)
        meta = _meta(
            model=self.config.image_model,
            prompt=prompt,
            creative_prompt=creative_prompt,
            style=style,
            kind=kind,
            aspect_ratio=aspect_ratio,
            observed_ticks=observed,
            estimate_ticks=IMAGE_ESTIMATE_TICKS,
            respect=respect,
        )
        revised = data[0].get("revised_prompt")
        if isinstance(revised, str):
            meta["revised_prompt"] = revised
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
        self._reject_bad_key()
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
            cached_meta = dict(cached[1])
            cached_meta["cache_hit"] = True
            post_check_asset(cached_meta, hook=self._post_check)
            return self._write_asset(
                dest, cached[0], cached_meta, kind="clip", cache_hit=True
            )
        estimate = duration * VIDEO_TICKS_PER_SEC
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
            bill=(0, 1, estimate),
        )
        request_id = started.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ImagineError("video response missing request_id")
        finished = self._poll_video(request_id)
        video = finished.get("video")
        if not isinstance(video, dict):
            raise ImagineError("video response missing video")
        respect = _moderation_flag(video.get("respect_moderation"))
        observed = _usage_ticks(finished)
        if observed is not None:
            self._reconcile(estimate, observed)
        meta = _meta(
            model=str(finished.get("model") or self.config.video_model),
            prompt=prompt,
            creative_prompt=creative_prompt,
            style=style,
            kind="clip",
            aspect_ratio=aspect_ratio,
            observed_ticks=observed,
            estimate_ticks=estimate,
            respect=respect,
        )
        meta["request_id"] = request_id
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
        response = self._call("GET", url, None, bill=None)
        if response.status >= _HTTP_BAD:
            raise ImagineError("status")
        if b"ftyp" not in response.body[:32]:
            raise ImagineError("video-bytes")
        return response.body

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, object] | None,
        *,
        bill: tuple[int, int, int] | None = None,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode()
        response = self._call(method, url, body, bill=bill)
        try:
            text = response.body.decode("utf-8")
        except UnicodeDecodeError:
            raise ImagineError("encoding") from None
        try:
            parsed = json.loads(text or "{}")
        except json.JSONDecodeError:
            raise ImagineError("response was not json") from None
        if not isinstance(parsed, dict):
            raise ImagineError("response was not an object")
        return parsed

    def _call(
        self,
        method: str,
        url: str,
        body: bytes | None,
        *,
        bill: tuple[int, int, int] | None,
    ) -> HttpResponse:
        delay = RETRY_SLEEP_S
        last = "request failed"
        for attempt in range(self.config.max_attempts):
            if bill is not None:
                self._charge(*bill)
            try:
                response = self._transport.request(
                    method,
                    url,
                    _headers(url, self.config.api_key, json_body=body is not None),
                    body,
                    self.config.timeout_s,
                )
            except CapExceeded:
                raise
            except Exception as exc:
                last = self._redact(str(exc))
                logger.warning("imagine request failed: %s", last)
                if attempt + 1 >= self.config.max_attempts:
                    raise ImagineError("transport") from None
                self._sleeper(delay)
                delay *= 2
                continue
            if response.status == _HTTP_TOO_MANY or response.status >= _HTTP_SERVER:
                last = f"status {response.status}"
                if attempt + 1 >= self.config.max_attempts:
                    raise ImagineError(last)
                self._sleeper(delay)
                delay *= 2
                continue
            if response.status >= _HTTP_BAD:
                if bill is not None:
                    self._release(*bill)
                raise ImagineError("status")
            return response
        raise ImagineError(last)

    def _reject_bad_key(self) -> None:
        key = self.config.api_key
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in key):
            raise ImagineError("invalid-key")

    def _charge(self, images: int, videos: int, ticks: int) -> None:
        cap_ticks = round(self.config.daily_cost_cap_usd * USD_TICKS)

        def mutate(ledger: dict[str, int]) -> dict[str, int]:
            if ledger["images"] + images > self.config.daily_image_cap:
                raise CapExceeded("cap")
            if ledger["videos"] + videos > self.config.daily_video_cap:
                raise CapExceeded("cap")
            if ledger["cost_ticks"] + ticks > cap_ticks:
                raise CapExceeded("cap")
            ledger["images"] += images
            ledger["videos"] += videos
            ledger["cost_ticks"] += ticks
            return ledger

        self._mutate_ledger(mutate)

    def _release(self, images: int, videos: int, ticks: int) -> None:
        def mutate(ledger: dict[str, int]) -> dict[str, int]:
            ledger["images"] = max(0, ledger["images"] - images)
            ledger["videos"] = max(0, ledger["videos"] - videos)
            ledger["cost_ticks"] = max(0, ledger["cost_ticks"] - ticks)
            return ledger

        self._mutate_ledger(mutate)

    def _reconcile(self, reserved_ticks: int, actual_ticks: int) -> None:
        delta = actual_ticks - reserved_ticks
        if delta == 0:
            return

        def mutate(ledger: dict[str, int]) -> dict[str, int]:
            ledger["cost_ticks"] = max(0, ledger["cost_ticks"] + delta)
            return ledger

        self._mutate_ledger(mutate)

    def _ledger_path(self) -> Path:
        root = self.config.budget_dir or self.config.cache_dir
        day = self._clock().date().isoformat()
        return root / f"usage-{day}.json"

    def _mutate_ledger(
        self, mutator: Callable[[dict[str, int]], dict[str, int]]
    ) -> None:
        path = self._ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            raw = handle.read()
            if raw.strip():
                loaded = json.loads(raw)
                ledger = {
                    "images": int(loaded.get("images", 0)),
                    "videos": int(loaded.get("videos", 0)),
                    "cost_ticks": int(loaded.get("cost_ticks", 0)),
                }
            else:
                ledger = {"images": 0, "videos": 0, "cost_ticks": 0}
            updated = mutator(ledger)
            payload = {
                "date": self._clock().date().isoformat(),
                "images": updated["images"],
                "videos": updated["videos"],
                "cost_ticks": updated["cost_ticks"],
            }
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(payload))
            handle.flush()

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
        meta_path = meta_path_for(dest)
        meta_path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        raw_cost = stored.get("cost_usd")
        cost = 0.0 if raw_cost is None else float(raw_cost)
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


def _usage_ticks(payload: dict[str, object]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict) or "cost_in_usd_ticks" not in usage:
        return None
    ticks = usage.get("cost_in_usd_ticks")
    if isinstance(ticks, bool) or not isinstance(ticks, int):
        return None
    if ticks < 0:
        raise ImagineError("usage")
    return ticks


def _moderation_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _decode_png(encoded: str) -> bytes:
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ImagineError("image-bytes") from None
    if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImagineError("image-bytes")
    return blob


def _meta(
    *,
    model: str,
    prompt: str,
    creative_prompt: str,
    style: str,
    kind: str,
    aspect_ratio: str,
    observed_ticks: int | None,
    estimate_ticks: int,
    respect: bool | None,
) -> dict[str, object]:
    observed = None if observed_ticks is None else observed_ticks / USD_TICKS
    return {
        "provider": "imagine",
        "model": model,
        "prompt": prompt,
        "creative_prompt": creative_prompt,
        "style": style,
        "kind": kind,
        "aspect_ratio": aspect_ratio,
        "cache_hit": False,
        "cost_usd": observed,
        "cost_estimate_usd": estimate_ticks / USD_TICKS,
        "cost_observation": "observed" if observed_ticks is not None else "estimated",
        "posted": False,
        "respect_moderation": respect,
    }
