# ruff: noqa: S101, S106
"""Review regressions for X credentials, rate limits, and parser errors."""

import logging
import traceback
from pathlib import Path

import pytest

from meme_studio.trends.fixtures import load_fixtures
from meme_studio.trends.ratelimit import RateLimiter
from meme_studio.trends.sources import SourceParseError, parse_google_news_rss
from meme_studio.trends.x_client import HttpXClient, XClientError


def test_bearer_token_is_absent_from_the_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", "super-secret-value")

    def transport(
        _url: str,
        headers: dict[str, str],
        _method: str,
    ) -> tuple[int, bytes]:
        raise OSError(headers["Authorization"])

    client = HttpXClient(
        transport=transport,
        token_env="X_BEARER_TOKEN",
        limiter=_limiter(),
    )
    with pytest.raises(XClientError) as exc:
        client.get_trends(woeid=23424977)
    formatted = "".join(traceback.format_exception(exc.value))
    assert "super-secret-value" not in formatted
    assert exc.value.__cause__ is None
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("meme_studio.trends.x_client")
    logger.addHandler(handler)
    try:
        logger.error("%s", exc.value)
    finally:
        logger.removeHandler(handler)
    rendered = " ".join(record.getMessage() for record in records)
    assert "super-secret-value" not in rendered


def test_x_client_requires_a_limiter() -> None:
    def transport(
        _url: str,
        _headers: dict[str, str],
        _method: str,
    ) -> tuple[int, bytes]:
        return 200, b'{"data":[]}'

    with pytest.raises(TypeError):
        HttpXClient(transport=transport, token_env="X_BEARER_TOKEN")


def test_missing_copycats_file_is_not_zero(tmp_path: Path) -> None:
    bundle = load_fixtures(tmp_path, reddit_tos_allowed=False)
    assert bundle.copycats is None


def test_bad_rss_timestamp_does_not_echo_the_payload() -> None:
    body = (
        b"<rss><channel><item><title>Hello</title>"
        b"<pubDate>not-a-date SECRET_MARKER</pubDate></item></channel></rss>"
    )
    with pytest.raises(SourceParseError) as exc:
        parse_google_news_rss(body)
    formatted = "".join(traceback.format_exception(exc.value))
    assert "SECRET_MARKER" not in formatted


def _limiter() -> RateLimiter:
    return RateLimiter(
        max_calls=5,
        per_seconds=60,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
