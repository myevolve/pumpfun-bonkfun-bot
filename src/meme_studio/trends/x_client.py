"""Injectable X read client.

The HTTP paths follow the public read API as documented for pay-per-use
trends, recent search, and recent counts. The exact JSON shape for trends is
still an open question, so parsers accept the small fixture shape used here
and a couple of field aliases. The bearer token is read from the named
environment variable at request time and is never put in a URL or an error.
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from meme_studio.errors import raise_without_context
from meme_studio.trends.cache import TtlCache
from meme_studio.trends.ratelimit import RateLimiter
from meme_studio.trends.sources import USER_AGENT

TRENDS_URL = "https://api.x.com/2/trends/by/woeid/{woeid}"
NEWS_URL = "https://api.x.com/2/news/search"
POSTS_URL = "https://api.x.com/2/tweets/search/recent"
COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"
_HTTP_OK = 200
_CACHE_TTL_SECONDS = 300.0
_DEFAULT_WOEID = 23424977
_Transport = Callable[[str, Mapping[str, str], str], tuple[int, bytes]]


class XClientError(RuntimeError):
    """A public X read failed. The message does not include the token or body."""


class XClientConfigError(XClientError):
    """The named environment variable for the read token is missing."""


@dataclass(frozen=True)
class XTrend:
    """One trend name and an optional post volume."""

    name: str
    tweet_count: int | None


@dataclass(frozen=True)
class XNewsItem:
    """One news row in the normalized fixture shape."""

    title: str
    outlet: str
    published_at: datetime
    url: str


@dataclass(frozen=True)
class XPost:
    """One recent post body."""

    text: str


@dataclass(frozen=True)
class XCount:
    """One post-count bucket."""

    start: datetime
    count: int


class HttpXClient:
    """X reads over an injected GET transport."""

    def __init__(
        self,
        *,
        transport: _Transport,
        limiter: RateLimiter,
        token_env: str = "X_BEARER_TOKEN",  # noqa: S107
        cache: TtlCache | None = None,
    ) -> None:
        """Create a client. The token is not read until a request is made.

        Args:
            transport: ``(url, headers, method) -> (status, body)``.
            limiter: Required call window. Unbounded reads are refused.
            token_env: Environment variable name. The value is not logged.
            cache: Optional TTL cache for trend reads.
        """
        self._transport = transport
        self._token_env = token_env
        self._cache = cache
        self._limiter = limiter

    def get_trends(self, *, woeid: int = _DEFAULT_WOEID) -> tuple[XTrend, ...]:
        """Read trends for a WOEID. The US id is 23424977.

        Args:
            woeid: Yahoo where-on-earth id.

        Returns:
            Trend names in payload order.
        """
        key = f"x:trends:{woeid}"
        cached = self._cached_tuple(key)
        if cached is not None:
            return cached
        payload = self._get(TRENDS_URL.format(woeid=woeid))
        trends = _parse_trends(payload)
        self._store(key, trends)
        return trends

    def search_news(self, *, query: str, max_age_hours: int) -> tuple[XNewsItem, ...]:
        """Read normalized news rows for a query.

        Args:
            query: Search text.
            max_age_hours: Maximum age requested from the source.

        Returns:
            News items. An empty payload returns an empty tuple.
        """
        url = f"{NEWS_URL}?query={quote(query)}&max_age_hours={max_age_hours}"
        return _parse_news(self._get(url))

    def search_posts(self, *, query: str, limit: int) -> tuple[XPost, ...]:
        """Read recent post text for a query.

        Args:
            query: Search text.
            limit: Maximum posts requested.

        Returns:
            Post texts in payload order.
        """
        url = f"{POSTS_URL}?query={quote(query)}&max_results={limit}"
        return _parse_posts(self._get(url))

    def get_post_counts_recent(
        self,
        *,
        query: str,
        granularity_minutes: int,
    ) -> tuple[XCount, ...]:
        """Read recent post-count buckets.

        Args:
            query: Search text.
            granularity_minutes: Bucket size requested from the source.

        Returns:
            Count buckets in payload order.
        """
        url = (
            f"{COUNTS_URL}?query={quote(query)}"
            f"&granularity_minutes={granularity_minutes}"
        )
        return _parse_counts(self._get(url))

    def _cached_tuple(self, key: str) -> tuple[XTrend, ...] | None:
        if self._cache is None:
            return None
        cached = self._cache.get(key)
        if isinstance(cached, tuple):
            return cached
        return None

    def _store(self, key: str, value: tuple[XTrend, ...]) -> None:
        if self._cache is not None:
            self._cache.set(key, value, ttl=_CACHE_TTL_SECONDS)

    def _get(self, url: str) -> dict[str, object]:
        token = os.environ.get(self._token_env)
        if not token:
            message = f"missing environment variable {self._token_env}"
            raise XClientConfigError(message)
        self._limiter.acquire()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        try:
            status, body = self._transport(url, headers, "GET")
        except (TimeoutError, OSError):
            raise_without_context(XClientError("x read failed"))
        if status != _HTTP_OK:
            raise_without_context(XClientError("x read status"))
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise_without_context(XClientError("x read payload"))
        if not isinstance(payload, dict):
            message = "x read payload"
            raise XClientError(message)
        return payload


def _parse_trends(payload: dict[str, object]) -> tuple[XTrend, ...]:
    trends: list[XTrend] = []
    for row in _rows(payload):
        name = row.get("name") or row.get("trend_name") or ""
        raw_count = row.get("tweet_count")
        count = int(raw_count) if isinstance(raw_count, int) else None
        trends.append(XTrend(str(name), count))
    return tuple(trends)


def _parse_news(payload: dict[str, object]) -> tuple[XNewsItem, ...]:
    items: list[XNewsItem] = []
    for row in _rows(payload):
        title = row.get("title")
        outlet = row.get("outlet")
        published = row.get("published_at")
        url = row.get("url")
        if not isinstance(title, str) or not isinstance(outlet, str):
            continue
        if not isinstance(published, str) or not isinstance(url, str):
            continue
        items.append(XNewsItem(title, outlet, _parse_time(published), url))
    return tuple(items)


def _parse_posts(payload: dict[str, object]) -> tuple[XPost, ...]:
    posts: list[XPost] = []
    for row in _rows(payload):
        text = row.get("text")
        if isinstance(text, str):
            posts.append(XPost(text))
    return tuple(posts)


def _parse_counts(payload: dict[str, object]) -> tuple[XCount, ...]:
    counts: list[XCount] = []
    for row in _rows(payload):
        start = row.get("start")
        raw_count = row.get("tweet_count", row.get("count"))
        if isinstance(start, str) and isinstance(raw_count, int):
            counts.append(XCount(_parse_time(start), raw_count))
    return tuple(counts)


def _rows(payload: dict[str, object]) -> list[dict[str, object]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise_without_context(XClientError("x read payload"))
