"""Optional gameplay clip. Imagine is primary; Playwright is the skippable fallback."""

from __future__ import annotations

from meme_studio.art.clip import DEFAULT_CLIP_SECONDS, capture_gameplay
from meme_studio.art.providers.imagine import VIDEO_MODEL

# ruff: noqa: ANN001, PLR2004, S101, TRY003


def test_default_clip_length_fits_the_pipeline_and_the_api() -> None:
    assert VIDEO_MODEL == "grok-imagine-video-1.5"
    assert 10 <= DEFAULT_CLIP_SECONDS <= 15


def test_missing_playwright_skips_without_writing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("meme_studio.art.clip.playwright_importable", lambda: False)
    html = tmp_path / "game.html"
    html.write_text("<html><body>play</body></html>", encoding="utf-8")
    dest = tmp_path / "clip.mp4"
    result = capture_gameplay(html, dest, seconds=DEFAULT_CLIP_SECONDS)
    assert result.status == "skipped"
    assert result.path is None
    assert result.provider == "none"
    assert "playwright" in result.reason.lower()
    assert not dest.exists()


def test_capturer_success_and_failure_are_bounded(tmp_path) -> None:
    html = tmp_path / "game.html"
    html.write_text("<html></html>", encoding="utf-8")
    dest = tmp_path / "clip.mp4"

    def ok(source, output, seconds: float) -> None:
        assert source == html
        assert seconds == 10
        output.write_bytes(b"mp4")

    captured = capture_gameplay(html, dest, seconds=10, capturer=ok)
    assert captured.status == "captured"
    assert captured.provider == "playwright"
    assert dest.read_bytes() == b"mp4"

    def boom(_source, _output, _seconds: float) -> None:
        raise RuntimeError("browser missing")

    failed = capture_gameplay(html, tmp_path / "other.mp4", seconds=10, capturer=boom)
    assert failed.status == "skipped"
    assert "browser missing" in failed.reason
    assert not (tmp_path / "other.mp4").exists()
