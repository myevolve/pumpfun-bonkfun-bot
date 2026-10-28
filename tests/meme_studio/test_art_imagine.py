"""Grok Imagine provider. The HTTP transport is mocked; these tests open no sockets."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

import pytest

from meme_studio.art.generate import generate_art
from meme_studio.art.guardrails import GuardrailError
from meme_studio.art.providers.imagine import (
    IMAGE_ENDPOINT,
    IMAGE_MODEL,
    VIDEO_ENDPOINT,
    VIDEO_MODEL,
    CapExceeded,
    HttpResponse,
    ImagineConfig,
    ImagineError,
    ImagineProvider,
)
from meme_studio.game.concept import load_concept
from tests.meme_studio.support import FIXTURE, ROOT, tiny_png

# ruff: noqa: ANN001, ANN202, ARG001, PLC0415, PLR2004, S101, S105, TRY003, TRY004

UNIT_KEY = "unit-test-key-do-not-log"
IMAGE_TICKS = 400_000_000  # $0.04; 1 USD = 10_000_000_000 ticks
VIDEO_TICKS = 8_000_000_000  # 10s * $0.08


def _clock() -> datetime:
    return datetime(2026, 10, 28, tzinfo=UTC)


class ScriptedTransport:
    """Return queued responses or raise queued exceptions."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        if not isinstance(action, HttpResponse):
            raise AssertionError("bad script")
        return action


def _json_response(status: int, payload: dict[str, object]) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode())


def _image_ok(png: bytes | None = None) -> HttpResponse:
    encoded = base64.b64encode(png or tiny_png()).decode("ascii")
    return _json_response(
        200,
        {
            "data": [
                {
                    "b64_json": encoded,
                    "mime_type": "image/png",
                    "respect_moderation": True,
                }
            ],
            "usage": {"cost_in_usd_ticks": IMAGE_TICKS},
        },
    )


def _provider(
    tmp_path,
    transport: ScriptedTransport,
    **overrides: object,
) -> ImagineProvider:
    config = ImagineConfig(
        api_key=UNIT_KEY,
        cache_dir=tmp_path / "cache",
        timeout_s=float(overrides.pop("timeout_s", 12.5)),
        max_attempts=int(overrides.pop("max_attempts", 3)),
        daily_image_cap=int(overrides.pop("daily_image_cap", 24)),
        daily_video_cap=int(overrides.pop("daily_video_cap", 2)),
        daily_cost_cap_usd=float(overrides.pop("daily_cost_cap_usd", 2.0)),
    )
    return ImagineProvider(
        config,
        transport,
        sleeper=overrides.pop("sleeper", lambda _delay: None),  # type: ignore[arg-type]
        clock=_clock,
        post_check=overrides.pop("post_check", None),  # type: ignore[arg-type]
    )


def test_module_cites_current_xai_docs() -> None:
    text = (ROOT / "meme_studio" / "art" / "providers" / "imagine.py").read_text(
        encoding="utf-8"
    )
    assert "https://docs.x.ai/developers/model-capabilities/images/generation" in text
    assert "https://docs.x.ai/developers/rest-api-reference/inference/videos" in text
    assert "https://docs.x.ai/developers/models" in text
    assert IMAGE_MODEL == "grok-imagine-image-2.0"
    assert VIDEO_MODEL == "grok-imagine-video-1.5"
    assert 'api_key = "' not in text


def test_image_request_shape_and_cost(tmp_path) -> None:
    png = tiny_png()
    transport = ScriptedTransport([_image_ok(png)])
    provider = _provider(tmp_path, transport)
    record = provider.generate_image(
        "a wild dust creature",
        tmp_path / "sprite.png",
        kind="sprite",
        aspect_ratio="1:1",
        style="neon_chaos",
        creative_prompt="a wild dust creature",
    )
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == IMAGE_ENDPOINT
    assert UNIT_KEY not in str(call["url"])
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == f"Bearer {UNIT_KEY}"
    assert call["timeout"] == 12.5
    payload = json.loads(call["body"])  # type: ignore[arg-type]
    assert payload["model"] == IMAGE_MODEL
    assert payload["prompt"] == "a wild dust creature"
    assert payload["response_format"] == "b64_json"
    assert payload["n"] == 1
    assert payload["quality"] == "low"
    assert payload["resolution"] == "1k"
    assert payload["aspect_ratio"] == "1:1"
    assert UNIT_KEY not in call["body"].decode()  # type: ignore[union-attr]
    assert record.path.read_bytes() == png
    assert record.cost_usd == pytest.approx(0.04)
    meta = json.loads(record.meta_path.read_text(encoding="utf-8"))
    assert meta["provider"] == "imagine"
    assert meta["model"] == IMAGE_MODEL
    assert meta["cache_hit"] is False
    assert meta["posted"] is False
    assert UNIT_KEY not in record.meta_path.read_text(encoding="utf-8")


def test_cache_hit_does_not_call_transport_again(tmp_path) -> None:
    transport = ScriptedTransport([_image_ok(), _image_ok()])
    provider = _provider(tmp_path, transport)
    dest1 = tmp_path / "one.png"
    dest2 = tmp_path / "two.png"
    first = provider.generate_image(
        "same",
        dest1,
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="same",
    )
    second = provider.generate_image(
        "same",
        dest2,
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="same",
    )
    assert len(transport.calls) == 1
    assert second.cache_hit is True
    assert first.path.read_bytes() == second.path.read_bytes()
    meta = json.loads(second.meta_path.read_text(encoding="utf-8"))
    assert meta["cache_hit"] is True


def test_retries_server_errors_then_succeeds(tmp_path) -> None:
    delays: list[float] = []
    transport = ScriptedTransport(
        [
            _json_response(503, {"error": "busy"}),
            _image_ok(),
        ]
    )
    provider = _provider(tmp_path, transport, sleeper=delays.append)
    provider.generate_image(
        "retry me",
        tmp_path / "sprite.png",
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="retry me",
    )
    assert len(transport.calls) == 2
    assert delays == [0.25]


def test_client_error_does_not_retry(tmp_path) -> None:
    transport = ScriptedTransport([_json_response(400, {"error": "bad prompt"})])
    provider = _provider(tmp_path, transport)
    with pytest.raises(ImagineError):
        provider.generate_image(
            "nope",
            tmp_path / "sprite.png",
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="nope",
        )
    assert len(transport.calls) == 1


def test_exceptions_and_logs_redact_the_key(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    transport = ScriptedTransport(
        [OSError(f"connect failed {UNIT_KEY} bearer {UNIT_KEY}")]
    )
    provider = _provider(tmp_path, transport, max_attempts=1)
    with caplog.at_level(logging.DEBUG), pytest.raises(ImagineError) as caught:
        provider.generate_image(
            "x",
            tmp_path / "sprite.png",
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="x",
        )
    assert UNIT_KEY not in str(caught.value)
    assert UNIT_KEY not in caplog.text


def test_daily_image_cap_blocks_the_second_call(tmp_path) -> None:
    transport = ScriptedTransport([_image_ok(), _image_ok()])
    provider = _provider(tmp_path, transport, daily_image_cap=1)
    provider.generate_image(
        "first",
        tmp_path / "a.png",
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="first",
    )
    with pytest.raises(CapExceeded):
        provider.generate_image(
            "second",
            tmp_path / "b.png",
            kind="background",
            aspect_ratio="9:16",
            style="absurdist",
            creative_prompt="second",
        )
    assert len(transport.calls) == 1


def test_daily_cost_cap_uses_estimate_before_the_request(tmp_path) -> None:
    transport = ScriptedTransport([_image_ok(), _image_ok()])
    provider = _provider(tmp_path, transport, daily_cost_cap_usd=0.05)
    provider.generate_image(
        "first",
        tmp_path / "a.png",
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="first",
    )
    with pytest.raises(CapExceeded):
        provider.generate_image(
            "second",
            tmp_path / "b.png",
            kind="share",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="second",
        )
    assert len(transport.calls) == 1


def test_video_request_polls_and_downloads_through_the_transport(tmp_path) -> None:
    mp4 = b"\x00\x00\x00\x18ftypisom"
    video_url = "https://cdn.example.test/clip.mp4"
    transport = ScriptedTransport(
        [
            _json_response(200, {"request_id": "req-1"}),
            _json_response(
                200,
                {
                    "status": "done",
                    "model": VIDEO_MODEL,
                    "video": {
                        "url": video_url,
                        "duration": 10,
                        "respect_moderation": True,
                    },
                    "usage": {"cost_in_usd_ticks": VIDEO_TICKS},
                },
            ),
            HttpResponse(200, mp4),
        ]
    )
    provider = _provider(tmp_path, transport)
    record = provider.generate_video(
        "gameplay teaser of a dust creature",
        tmp_path / "clip.mp4",
        aspect_ratio="9:16",
        style="retro_arcade",
        creative_prompt="gameplay teaser of a dust creature",
        duration_s=20,
    )
    assert [call["url"] for call in transport.calls] == [
        VIDEO_ENDPOINT,
        "https://api.x.ai/v1/videos/req-1",
        video_url,
    ]
    start = json.loads(transport.calls[0]["body"])  # type: ignore[arg-type]
    assert start["model"] == VIDEO_MODEL
    assert start["duration"] == 15
    assert start["aspect_ratio"] == "9:16"
    assert start["resolution"] == "480p"
    assert UNIT_KEY not in start["prompt"]
    assert all(UNIT_KEY not in str(call["url"]) for call in transport.calls)
    assert record.path is not None
    assert record.path.read_bytes() == mp4
    assert record.cost_usd == pytest.approx(0.80)


def test_video_pending_then_done_sleeps(tmp_path) -> None:
    delays: list[float] = []
    transport = ScriptedTransport(
        [
            _json_response(200, {"request_id": "req-2"}),
            _json_response(202, {"request_id": "req-2", "status": "pending"}),
            _json_response(
                200,
                {
                    "status": "done",
                    "video": {
                        "url": "https://cdn.example.test/ready.mp4",
                        "respect_moderation": True,
                        "duration": 10,
                    },
                    "usage": {"cost_in_usd_ticks": VIDEO_TICKS},
                },
            ),
            HttpResponse(200, b"\x00\x00\x00\x18ftypisom"),
        ]
    )
    provider = _provider(tmp_path, transport, sleeper=delays.append)
    provider.generate_video(
        "teaser",
        tmp_path / "clip.mp4",
        aspect_ratio="9:16",
        style="comic_book",
        creative_prompt="teaser",
    )
    assert delays == [2.0]


def test_moderation_failure_is_not_cached(tmp_path) -> None:
    encoded = base64.b64encode(tiny_png()).decode("ascii")
    transport = ScriptedTransport(
        [
            _json_response(
                200,
                {"data": [{"b64_json": encoded, "respect_moderation": False}]},
            )
        ]
    )
    provider = _provider(tmp_path, transport)
    dest = tmp_path / "sprite.png"
    with pytest.raises(GuardrailError):
        provider.generate_image(
            "creature",
            dest,
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    assert not dest.exists()
    assert list((tmp_path / "cache").rglob("*.png")) == []


def test_post_check_hook_discards_the_image(tmp_path) -> None:
    transport = ScriptedTransport([_image_ok()])

    def hook(_meta: dict[str, object]) -> None:
        raise GuardrailError("hooked")

    provider = _provider(tmp_path, transport, post_check=hook)
    dest = tmp_path / "sprite.png"
    with pytest.raises(GuardrailError, match="hooked"):
        provider.generate_image(
            "creature",
            dest,
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    assert not dest.exists()


def test_newline_in_the_key_never_reaches_the_network_or_the_error(
    tmp_path, monkeypatch
) -> None:
    import http.client

    from meme_studio.art.providers.imagine import UrllibTransport

    secret = "synthetic-key-line1\nsynthetic-key-line2"
    connects: list[int] = []

    def refuse(self) -> None:
        connects.append(1)
        raise AssertionError("connect")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)
    with pytest.raises(ImagineError) as caught:
        UrllibTransport().request(
            "POST",
            "https://api.x.ai/v1/images/generations",
            {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            b"{}",
            1.0,
        )
    assert secret not in str(caught.value)
    assert "synthetic-key" not in str(caught.value)
    assert connects == []
    transport = ScriptedTransport([_image_ok()])
    provider = _provider(tmp_path, transport)
    provider.config.api_key = secret
    with pytest.raises(ImagineError) as provider_caught:
        provider.generate_image(
            "creature",
            tmp_path / "sprite.png",
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    assert secret not in str(provider_caught.value)
    assert transport.calls == []


def test_transport_value_error_is_redacted(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "synthetic-header-secret"
    transport = ScriptedTransport([ValueError(f"Invalid header {secret}")])
    provider = _provider(tmp_path, transport, max_attempts=1)
    provider.config.api_key = secret
    with caplog.at_level(logging.DEBUG), pytest.raises(ImagineError) as caught:
        provider.generate_image(
            "creature",
            tmp_path / "sprite.png",
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_ambiguous_failures_consume_the_daily_cap(tmp_path) -> None:
    transport = ScriptedTransport([TimeoutError("timed out")] * 6)
    provider = _provider(
        tmp_path,
        transport,
        max_attempts=3,
        daily_image_cap=1,
        daily_cost_cap_usd=0.04,
    )
    with pytest.raises((CapExceeded, ImagineError)):
        provider.generate_image(
            "creature",
            tmp_path / "sprite.png",
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    posts = [
        call
        for call in transport.calls
        if str(call["url"]).endswith("/images/generations")
    ]
    assert len(posts) == 1
    ledgers = list((tmp_path / "cache").rglob("usage-*.json"))
    assert ledgers
    ledger = json.loads(ledgers[0].read_text(encoding="utf-8"))
    assert ledger["images"] >= 1
    assert ledger["cost_ticks"] >= IMAGE_TICKS


def test_invalid_image_bytes_are_not_cached(tmp_path) -> None:
    transport = ScriptedTransport(
        [
            _json_response(
                200, {"data": [{"b64_json": "!!!!", "respect_moderation": True}]}
            )
        ]
    )
    provider = _provider(tmp_path, transport)
    dest = tmp_path / "sprite.png"
    with pytest.raises(ImagineError):
        provider.generate_image(
            "creature",
            dest,
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    assert not dest.exists()
    assert list((tmp_path / "cache").rglob("*.bin")) == []


def test_missing_moderation_stays_unknown_and_negative_usage_is_rejected(
    tmp_path,
) -> None:
    encoded = base64.b64encode(tiny_png()).decode("ascii")
    transport = ScriptedTransport(
        [
            _json_response(200, {"data": [{"b64_json": encoded}]}),
            _json_response(
                200,
                {
                    "data": [{"b64_json": encoded, "respect_moderation": True}],
                    "usage": {"cost_in_usd_ticks": -1},
                },
            ),
        ]
    )
    provider = _provider(tmp_path, transport)
    record = provider.generate_image(
        "known",
        tmp_path / "known.png",
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="known",
    )
    meta = json.loads(record.meta_path.read_text(encoding="utf-8"))
    assert meta["respect_moderation"] is None
    assert meta["cost_observation"] == "estimated"
    assert meta["cost_usd"] is None
    with pytest.raises(ImagineError):
        provider.generate_image(
            "negative",
            tmp_path / "negative.png",
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="negative",
        )


def test_cache_hit_runs_the_current_post_check(tmp_path) -> None:
    transport = ScriptedTransport([_image_ok()])
    first = _provider(tmp_path, transport)
    first.generate_image(
        "same",
        tmp_path / "one.png",
        kind="sprite",
        aspect_ratio="1:1",
        style="absurdist",
        creative_prompt="same",
    )
    seen: list[str] = []

    def hook(_meta: dict[str, object]) -> None:
        seen.append("ran")
        raise GuardrailError("hooked")

    second = _provider(tmp_path, ScriptedTransport([]), post_check=hook)
    dest = tmp_path / "two.png"
    with pytest.raises(GuardrailError):
        second.generate_image(
            "same",
            dest,
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="same",
        )
    assert seen == ["ran"]
    assert not dest.exists()


def test_revised_prompt_is_checked_before_cache(tmp_path) -> None:
    encoded = base64.b64encode(tiny_png()).decode("ascii")
    transport = ScriptedTransport(
        [
            _json_response(
                200,
                {
                    "data": [
                        {
                            "b64_json": encoded,
                            "respect_moderation": True,
                            "revised_prompt": "photorealistic child",
                        }
                    ],
                    "usage": {"cost_in_usd_ticks": IMAGE_TICKS},
                },
            )
        ]
    )
    provider = _provider(tmp_path, transport)
    dest = tmp_path / "sprite.png"
    with pytest.raises(GuardrailError):
        provider.generate_image(
            "creature",
            dest,
            kind="sprite",
            aspect_ratio="1:1",
            style="absurdist",
            creative_prompt="creature",
        )
    assert not dest.exists()
    assert list((tmp_path / "cache").rglob("*.bin")) == []


def test_redirect_does_not_forward_authorization(monkeypatch) -> None:
    import http.client
    import urllib.request

    from meme_studio.art.providers.imagine import SafeRedirect

    secret = "synthetic-redirect-secret"
    connects: list[str] = []

    def refuse(self) -> None:
        connects.append(str(getattr(self, "host", "")))
        raise AssertionError("connect")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    request = urllib.request.Request(
        "https://api.x.ai/v1/images/generations",
        headers={"Authorization": f"Bearer {secret}"},
        method="GET",
    )
    redirected = SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://other.invalid/landed"
    )
    names = [key.lower() for key, _value in redirected.header_items()]
    assert "authorization" not in names
    same = SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://api.x.ai/v1/images/generations"
    )
    same_headers = {key.lower(): value for key, value in same.header_items()}
    assert same_headers["authorization"] == f"Bearer {secret}"
    assert connects == []


def test_malformed_transport_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)

    class Garbage:
        def request(self, method, url, headers, body, timeout):
            del method, url, headers, body, timeout
            return HttpResponse(200, b"\xff\xfe")

    concept = load_concept(FIXTURE)
    pack = generate_art(
        concept,
        tmp_path / "out",
        game_only=True,
        transport=Garbage(),
        cache_dir=tmp_path / "cache",
        skip_clip=True,
        max_attempts=1,
    )
    assert pack.sprite.provider == "procedural"
