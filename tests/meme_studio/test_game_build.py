"""End-to-end draft build: hub, disclosure, mocked Imagine, no network and no posting."""

from __future__ import annotations

import base64
import json
import urllib.request

import pytest

from meme_studio.art.generate import generate_art
from meme_studio.art.guardrails import GuardrailError
from meme_studio.art.providers.imagine import HttpResponse
from meme_studio.game.build import build
from meme_studio.game.cli import main
from meme_studio.game.concept import ConceptError, load_concept
from tests.meme_studio.support import (
    DISCLOSURE,
    FIXTURE,
    decode_png_rgb,
    load_fixture_dict,
    tiny_png,
    write_concept,
)

# ruff: noqa: ANN001, ARG002, PLR2004, S101

UNIT_KEY = "unit-test-key-do-not-log"


class ImageTransport:
    """Imagine stand-in. Image posts return a PNG; video posts return a clip."""

    def __init__(self, *, fail: bool = False, moderate: bool = True) -> None:
        self.fail = fail
        self.moderate = moderate
        self.calls: list[str] = []
        self.bodies: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        del headers, timeout
        self.calls.append(url)
        if body:
            self.bodies.append(json.loads(body))
        if self.fail:
            return HttpResponse(500, b'{"error":"down"}')
        if url.endswith("/videos/generations"):
            return HttpResponse(200, json.dumps({"request_id": "vid-1"}).encode())
        if url.endswith("/videos/vid-1"):
            return HttpResponse(
                200,
                json.dumps(
                    {
                        "status": "done",
                        "video": {
                            "url": "https://cdn.example.test/clip.mp4",
                            "duration": 10,
                            "respect_moderation": self.moderate,
                        },
                        "usage": {"cost_in_usd_ticks": 8_000_000_000},
                    }
                ).encode(),
            )
        if url.endswith("/clip.mp4"):
            return HttpResponse(200, b"fake-mp4")
        encoded = base64.b64encode(tiny_png()).decode("ascii")
        return HttpResponse(
            200,
            json.dumps(
                {
                    "data": [
                        {
                            "b64_json": encoded,
                            "mime_type": "image/png",
                            "respect_moderation": self.moderate,
                        }
                    ],
                    "usage": {"cost_in_usd_ticks": 400_000_000},
                }
            ).encode(),
        )


def test_build_without_a_key_is_procedural_and_offline(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    result = build(FIXTURE, tmp_path, skip_clip=True)
    page = result.game_page.read_text(encoding="utf-8")
    index = result.hub_index.read_text(encoding="utf-8")
    assert DISCLOSURE in page
    assert DISCLOSURE in index
    assert "Sock Drawer Drift" in index
    assert "phase-0-shadow" in page
    assert result.art.sprite.provider == "procedural"
    assert result.art.fallback_reason
    assert "XAI_API_KEY" in result.art.fallback_reason
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["posted"] is False
    assert manifest["deployed"] is False
    assert manifest["phase"] == "0-shadow"
    assert manifest["clip"]["status"] == "skipped"
    assert result.art.logo.path.is_file()
    width, height, _rgb = decode_png_rgb(result.art.logo.path.read_bytes())
    assert (width, height) == (512, 512)
    spec_marker = "assets/sprite.png"
    assert spec_marker in page
    assert UNIT_KEY not in page
    catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert len(catalog["games"]) == 1
    assert catalog["games"][0]["ticker"] == "SOCKDRIFT"


def test_game_only_build_hides_ticker_on_the_hub(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    result = build(FIXTURE, tmp_path, game_only=True, skip_clip=True)
    page = result.game_page.read_text(encoding="utf-8")
    only = result.game_only_page.read_text(encoding="utf-8")
    index = result.hub_index.read_text(encoding="utf-8")
    catalog_text = (tmp_path / "catalog.json").read_text(encoding="utf-8")
    for text in (page, only, index, catalog_text):
        assert "SOCKDRIFT" not in text
        assert "CA_PLACEHOLDER_DO_NOT_USE" not in text
        assert "coin" not in text.lower()
    assert DISCLOSURE in page


def test_second_story_is_archived_and_rebuild_does_not_duplicate(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    build(FIXTURE, tmp_path, skip_clip=True)
    payload = load_fixture_dict()
    payload["story_id"] = "2026-10-27-lint-parade"
    payload["title"] = "Lint Parade"
    payload["name"] = "Lint Parade"
    payload["date"] = "2026-10-27"
    payload["template_id"] = "clicker"
    payload["ticker"] = "LINTPAR"
    second = write_concept(tmp_path, payload, "lint.json")
    build(second, tmp_path, skip_clip=True)
    catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    titles = [entry["title"] for entry in catalog["games"]]
    assert titles == ["Sock Drawer Drift", "Lint Parade"]
    build(FIXTURE, tmp_path, skip_clip=True)
    catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    assert len(catalog["games"]) == 2
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert index.index("Sock Drawer Drift") < index.index("Lint Parade")


def test_imagine_is_primary_when_a_key_and_transport_are_present(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    transport = ImageTransport()
    result = build(FIXTURE, tmp_path, transport=transport, skip_clip=False)
    assert result.art.sprite.provider == "imagine"
    assert result.art.background.provider == "imagine"
    assert result.art.share.provider == "imagine"
    meta = json.loads(result.art.sprite.meta_path.read_text(encoding="utf-8"))
    assert meta["model"] == "grok-imagine-image-2.0"
    assert meta["prompt"]
    assert UNIT_KEY not in json.dumps(meta)
    page = result.game_page.read_text(encoding="utf-8")
    assert "assets/sprite.png" in page
    assert "assets/background.png" in page
    assert result.art.clip.status == "captured"
    assert result.art.clip.provider == "imagine"
    assert result.art.clip.path is not None
    assert result.art.clip.path.read_bytes() == b"fake-mp4"
    video_prompts = [body["prompt"] for body in transport.bodies if "duration" in body]
    assert video_prompts
    assert all("SOCKDRIFT" not in str(prompt) for prompt in video_prompts)
    only = result.game_only_page.read_text(encoding="utf-8")
    assert "SOCKDRIFT" not in only
    assert "SOCKDRIFT" in page


def test_imagine_failure_falls_back_to_procedural(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)
    transport = ImageTransport(fail=True)
    result = build(
        FIXTURE,
        tmp_path,
        transport=transport,
        skip_clip=True,
        max_attempts=1,
    )
    assert transport.calls
    assert result.art.sprite.provider == "procedural"
    assert result.art.fallback_reason
    width, height, _rgb = decode_png_rgb(result.art.logo.path.read_bytes())
    assert (width, height) == (512, 512)


def test_image_cap_falls_back_after_the_budget_is_spent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)
    transport = ImageTransport()
    result = build(
        FIXTURE,
        tmp_path,
        transport=transport,
        skip_clip=True,
        daily_image_cap=1,
    )
    providers = {
        result.art.sprite.provider,
        result.art.background.provider,
        result.art.share.provider,
        result.art.logo.provider,
        result.art.banner.provider,
    }
    assert "imagine" in providers
    assert "procedural" in providers
    image_posts = [
        url for url in transport.calls if url.endswith("/images/generations")
    ]
    assert len(image_posts) == 1


def test_unsafe_concept_does_not_call_the_provider(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)
    payload = load_fixture_dict()
    payload["joke"] = "photorealistic nude celebrity"
    path = write_concept(tmp_path, payload, "unsafe.json")
    transport = ImageTransport()
    with pytest.raises(GuardrailError):
        build(path, tmp_path / "out", transport=transport, skip_clip=True)
    assert transport.calls == []


def test_secret_concept_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    payload = load_fixture_dict()
    payload["solana_private_key"] = "nope"
    path = write_concept(tmp_path, payload, "secret.json")
    out = tmp_path / "out"
    with pytest.raises(ConceptError):
        build(path, out, skip_clip=True)
    assert not (out / "index.html").exists()


def test_cli_build_and_module_entrypoint(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    out = tmp_path / "site"
    code = main(["build", "--concept", str(FIXTURE), "--out", str(out), "--skip-clip"])
    assert code == 0
    assert (out / "index.html").is_file()
    concept = load_concept(FIXTURE)
    assert concept.template_id == "endless_runner"
    with pytest.raises(SystemExit):
        main(["build", "--out", str(out)])
    bad = write_concept(tmp_path, {}, "empty.json")
    assert (
        main(
            [
                "build",
                "--concept",
                str(bad),
                "--out",
                str(tmp_path / "bad"),
                "--skip-clip",
            ]
        )
        == 2
    )


def test_generate_art_cache_skips_the_second_round(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)
    transport = ImageTransport()
    cache = tmp_path / "cache"
    concept = load_concept(FIXTURE)
    generate_art(
        concept,
        tmp_path / "one",
        game_only=False,
        transport=transport,
        cache_dir=cache,
        skip_clip=True,
    )
    first_calls = len(transport.calls)
    generate_art(
        concept,
        tmp_path / "two",
        game_only=False,
        transport=transport,
        cache_dir=cache,
        skip_clip=True,
    )
    assert first_calls == 5
    assert len(transport.calls) == first_calls


def test_moderation_failure_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", UNIT_KEY)
    transport = ImageTransport(moderate=False)
    result = build(
        FIXTURE, tmp_path, transport=transport, skip_clip=True, max_attempts=1
    )
    assert result.art.sprite.provider == "procedural"
    assert result.art.fallback_reason
    assert "moderation" in result.art.fallback_reason.lower()
