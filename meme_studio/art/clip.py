"""Optional Playwright gameplay capture.

Imagine is the primary clip source. This module is the fallback and is
skipped when Playwright or a browser is not installed. It never posts.
"""
# ruff: noqa: PLC0415, TRY003

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CLIP_SECONDS = 10
_VIEW_W = 360
_VIEW_H = 640

Capturer = Callable[[Path, Path, float], None]


@dataclass(frozen=True)
class ClipResult:
    """Outcome of a clip attempt."""

    status: str
    path: Path | None
    reason: str
    provider: str


def playwright_importable() -> bool:
    """Return whether the Playwright package imports.

    Returns:
        True when the package is installed. This does not download a browser.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def capture_gameplay(
    html_path: Path,
    dest: Path,
    *,
    seconds: float,
    capturer: Capturer | None = None,
) -> ClipResult:
    """Record ``html_path`` to an mp4, or skip when capture is unavailable.

    Args:
        html_path: Game-only page to open.
        dest: MP4 destination.
        seconds: Capture length.
        capturer: Injected recorder for tests. When omitted, Playwright is used.

    Returns:
        A captured or skipped result. Failures are skipped, not raised.
    """
    if capturer is not None:
        return _run_capturer(capturer, html_path, dest, seconds)
    if not playwright_importable():
        return ClipResult("skipped", None, "playwright unavailable", "none")
    try:
        _capture_with_playwright(html_path, dest, seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("optional capture skipped: %s", type(exc).__name__)
        return ClipResult("skipped", None, "capture skipped", "none")
    if not dest.is_file():
        return ClipResult("skipped", None, "playwright produced no file", "none")
    return ClipResult("captured", dest, "playwright", "playwright")


def _run_capturer(
    capturer: Capturer, html_path: Path, dest: Path, seconds: float
) -> ClipResult:
    try:
        capturer(html_path, dest, seconds)
    except Exception as exc:  # noqa: BLE001
        return ClipResult("skipped", None, str(exc), "none")
    if not dest.is_file():
        return ClipResult("skipped", None, "capturer produced no file", "none")
    return ClipResult("captured", dest, "capturer", "playwright")


def _capture_with_playwright(html_path: Path, dest: Path, seconds: float) -> None:
    from playwright.sync_api import sync_playwright

    dest.parent.mkdir(parents=True, exist_ok=True)
    video_dir = dest.parent / "playwright-video"
    video_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": _VIEW_W, "height": _VIEW_H},
            record_video_dir=str(video_dir),
            record_video_size={"width": _VIEW_W, "height": _VIEW_H},
        )
        page = context.new_page()
        page.goto(html_path.resolve().as_uri())
        page.click("#play")
        page.wait_for_timeout(int(seconds * 1000))
        page.close()
        video = page.video
        webm = Path(video.path()) if video is not None else None
        context.close()
        browser.close()
    if webm is None or not webm.is_file():
        raise RuntimeError("playwright recorded no video")
    _to_mp4(webm, dest)


def _to_mp4(webm: Path, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg unavailable")
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-i",
            str(webm),
            "-an",
            "-movflags",
            "+faststart",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
