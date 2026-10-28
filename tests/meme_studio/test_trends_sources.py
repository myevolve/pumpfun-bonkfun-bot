# ruff: noqa: S101, PLR2004, S106
"""Public source parsers and the injectable X client. No live network."""

from datetime import UTC, datetime

import pytest

from meme_studio.concept.inputs import CopycatCoin, CountPoint
from meme_studio.trends.cache import TtlCache
from meme_studio.trends.collect import attach_copycats, build_stories
from meme_studio.trends.models import RawItem, TrendSignal
from meme_studio.trends.ratelimit import RateLimiter
from meme_studio.trends.sources import (
    GOOGLE_NEWS_RSS_URL,
    REDDIT_RISING_URL,
    GoogleNewsRssSource,
    RedditPublicJsonSource,
    SourceParseError,
    parse_google_news_rss,
    parse_reddit_listing,
)
from meme_studio.trends.x_client import (
    COUNTS_URL,
    TRENDS_URL,
    HttpXClient,
    XClientConfigError,
    XClientError,
)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>A raccoon escaped with a donut</title>
      <link>https://example.com/raccoon</link>
      <pubDate>Wed, 28 Oct 2026 14:30:00 GMT</pubDate>
      <source url="https://example.com">Example News</source>
    </item>
    <item>
      <title>A goose stole a pretzel - City Paper</title>
      <link>https://example.com/goose</link>
      <pubDate>Wed, 28 Oct 2026 14:40:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

REDDIT = b"""{
  "data": {
    "children": [
      {
        "data": {
          "title": "A goose stole a pretzel",
          "subreddit": "aww",
          "permalink": "/r/aww/comments/abc/a_goose_stole_a_pretzel",
          "created_utc": 1793197800,
          "selftext": ""
        }
      }
    ]
  }
}
"""


def test_google_rss_parser_reads_source_and_title_suffix() -> None:
    items = parse_google_news_rss(RSS)
    assert len(items) == 2
    assert items[0].source == "google_news"
    assert items[0].title == "A raccoon escaped with a donut"
    assert items[0].outlet == "Example News"
    assert items[0].url == "https://example.com/raccoon"
    assert items[0].published_at == datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    assert items[1].outlet == "City Paper"
    assert items[1].title == "A goose stole a pretzel"


def test_parser_errors_hide_the_payload() -> None:
    with pytest.raises(SourceParseError) as exc:
        parse_google_news_rss(b"<rss>SECRET_BODY_TOKEN")
    assert "SECRET_BODY_TOKEN" not in str(exc.value)
    with pytest.raises(SourceParseError) as reddit_exc:
        parse_reddit_listing(b"not-json SECRET_BODY_TOKEN")
    assert "SECRET_BODY_TOKEN" not in str(reddit_exc.value)


def test_reddit_parser_builds_a_public_url() -> None:
    items = parse_reddit_listing(REDDIT)
    assert len(items) == 1
    assert items[0].source == "reddit"
    assert items[0].outlet == "aww"
    assert items[0].title == "A goose stole a pretzel"
    assert items[0].url == (
        "https://www.reddit.com/r/aww/comments/abc/a_goose_stole_a_pretzel"
    )
    assert items[0].published_at == datetime(2026, 10, 28, 14, 30, tzinfo=UTC)


def test_google_source_rate_limits_and_caches() -> None:
    clock = {"t": 0.0}
    calls: list[str] = []

    def transport(
        url: str,
        headers: dict[str, str],
        method: str,
    ) -> tuple[int, bytes]:
        calls.append(method)
        assert url == GOOGLE_NEWS_RSS_URL
        assert method == "GET"
        assert "Authorization" not in headers
        return 200, RSS

    limiter = RateLimiter(
        max_calls=5,
        per_seconds=60,
        clock=lambda: clock["t"],
        sleep=lambda _seconds: None,
    )
    cache = TtlCache(clock=lambda: clock["t"])
    source = GoogleNewsRssSource(
        transport=transport,
        cache=cache,
        limiter=limiter,
        clock=lambda: clock["t"],
    )
    first = source.fetch()
    second = source.fetch()
    assert calls == ["GET"]
    assert first == second
    clock["t"] = 301
    source.fetch()
    assert calls == ["GET", "GET"]


def test_reddit_stays_off_unless_tos_is_explicitly_allowed() -> None:
    def transport(
        url: str,
        _headers: dict[str, str],
        _method: str,
    ) -> tuple[int, bytes]:
        raise AssertionError(url)

    source = RedditPublicJsonSource(
        transport=transport,
        cache=TtlCache(clock=lambda: 0.0),
        limiter=RateLimiter(
            max_calls=1,
            per_seconds=60,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
        ),
        tos_allowed=False,
        clock=lambda: 0.0,
    )
    result = source.fetch()
    assert result.items == ()
    assert result.skipped_reason == "reddit_tos_unknown"


def test_reddit_fetch_sends_a_user_agent_and_no_credentials() -> None:
    seen: list[tuple[str, dict[str, str], str]] = []

    def transport(
        url: str,
        headers: dict[str, str],
        method: str,
    ) -> tuple[int, bytes]:
        seen.append((url, dict(headers), method))
        return 200, REDDIT

    source = RedditPublicJsonSource(
        transport=transport,
        cache=TtlCache(clock=lambda: 0.0),
        limiter=RateLimiter(
            max_calls=2,
            per_seconds=60,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
        ),
        tos_allowed=True,
        clock=lambda: 0.0,
    )
    result = source.fetch()
    assert result.skipped_reason is None
    assert result.items[0].title == "A goose stole a pretzel"
    url, headers, method = seen[0]
    assert url == REDDIT_RISING_URL
    assert method == "GET"
    assert "Authorization" not in headers
    assert "meme-studio-shadow" in headers["User-Agent"]


def test_http_x_client_reads_the_named_env_and_never_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", "super-secret-value")
    seen: list[tuple[str, dict[str, str], str]] = []

    def transport(
        url: str,
        headers: dict[str, str],
        method: str,
    ) -> tuple[int, bytes]:
        seen.append((url, dict(headers), method))
        if "counts" in url:
            return 200, (
                b'{"data":[{"start":"2026-10-28T14:00:00.000Z","tweet_count":5}]}'
            )
        if "news" in url:
            return 200, (
                b'{"data":[{"title":"A raccoon escaped with a donut",'
                b'"outlet":"Example News","published_at":"2026-10-28T14:30:00+00:00",'
                b'"url":"https://example.com/raccoon"}]}'
            )
        if "search" in url:
            return 200, b'{"data":[{"id":"1","text":"hello raccoon"}]}'
        return 200, b'{"data":[{"name":"Raccoon Donut","tweet_count":3}]}'

    client = HttpXClient(transport=transport, token_env="X_BEARER_TOKEN")
    trends = client.get_trends(woeid=23424977)
    news = client.search_news(query="raccoon", max_age_hours=1)
    posts = client.search_posts(query="raccoon", limit=5)
    counts = client.get_post_counts_recent(query="raccoon", granularity_minutes=1)
    assert trends[0].name == "Raccoon Donut"
    assert trends[0].tweet_count == 3
    assert news[0].outlet == "Example News"
    assert posts[0].text == "hello raccoon"
    assert counts[0].count == 5
    assert seen[0][0] == TRENDS_URL.format(woeid=23424977)
    assert seen[0][2] == "GET"
    assert seen[0][1]["Authorization"] == "Bearer super-secret-value"
    assert "super-secret-value" not in seen[0][0]
    assert all(method == "GET" for _url, _headers, method in seen)
    assert COUNTS_URL.split("?", maxsplit=1)[0] in seen[-1][0]


def test_x_client_errors_do_not_echo_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", "super-secret-value")

    def transport(
        _url: str,
        _headers: dict[str, str],
        _method: str,
    ) -> tuple[int, bytes]:
        raise TimeoutError

    client = HttpXClient(transport=transport, token_env="X_BEARER_TOKEN")
    with pytest.raises(XClientError) as exc:
        client.get_trends(woeid=23424977)
    assert "super-secret-value" not in str(exc.value)

    monkeypatch.delenv("X_BEARER_TOKEN")
    bare = HttpXClient(transport=transport, token_env="X_BEARER_TOKEN")
    with pytest.raises(XClientConfigError) as missing:
        bare.get_trends(woeid=23424977)
    assert "X_BEARER_TOKEN" in str(missing.value)
    assert "super-secret-value" not in str(missing.value)


def test_x_client_caches_trend_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_BEARER_TOKEN", "super-secret-value")
    calls = {"n": 0}

    def transport(
        _url: str,
        _headers: dict[str, str],
        _method: str,
    ) -> tuple[int, bytes]:
        calls["n"] += 1
        return 200, b'{"data":[{"trend_name":"Raccoon","tweet_count":1}]}'

    clock = {"t": 0.0}
    client = HttpXClient(
        transport=transport,
        token_env="X_BEARER_TOKEN",
        cache=TtlCache(clock=lambda: clock["t"]),
        limiter=RateLimiter(
            max_calls=10,
            per_seconds=60,
            clock=lambda: clock["t"],
            sleep=lambda _seconds: None,
        ),
    )
    first = client.get_trends(woeid=23424977)
    second = client.get_trends(woeid=23424977)
    assert first[0].name == "Raccoon"
    assert second == first
    assert calls["n"] == 1


def test_stories_group_titles_and_attach_trends_and_copycats() -> None:
    published = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    items = [
        RawItem(
            source="google_news",
            title="A  Raccoon escaped with a donut",
            summary="A raccoon escaped with a donut",
            published_at=published,
            outlet="Example News",
            url="https://example.com/a",
        ),
        RawItem(
            source="x",
            title="a raccoon escaped with a donut",
            summary="a raccoon escaped with a donut",
            published_at=published,
            outlet="Wire",
            url="https://example.com/b",
        ),
    ]
    trends = (
        TrendSignal(
            name="raccoon donut",
            post_counts=(CountPoint(start=published, count=4),),
        ),
    )
    stories = build_stories(items, trends=trends)
    assert len(stories) == 1
    assert set(stories[0].sources) == {"google_news", "x"}
    assert {hit.name for hit in stories[0].outlets} == {"Example News", "Wire"}
    assert stories[0].post_counts[0].count == 4
    attached = attach_copycats(
        stories,
        (
            CopycatCoin("Super Raccoon", "SUPRAC", 3, 1.0, 10.0),
            CopycatCoin("Notes about weather", "NOTE", 1, 1.0, 10.0),
        ),
    )
    assert len(attached[0].copycats) == 1
    assert attached[0].copycats[0].ticker == "SUPRAC"


def test_unmatched_trend_becomes_its_own_story() -> None:
    published = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
    stories = build_stories(
        (),
        trends=(
            TrendSignal(
                name="A raccoon escaped with a donut",
                post_counts=(),
            ),
        ),
        now=published,
    )
    assert len(stories) == 1
    assert stories[0].sources == ("x",)
    assert stories[0].title == "A raccoon escaped with a donut"
    assert stories[0].outlets[0].name == "x-trend"
