"""Google News RSS and Reddit public JSON readers.

Reddit stays off unless the operator sets ``REDDIT_TOS_ALLOWED=1``. Commercial
terms for the Reddit Data API are an open question, so the default is to skip.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from meme_studio.errors import raise_without_context
from meme_studio.trends.cache import TtlCache
from meme_studio.trends.models import RawItem
from meme_studio.trends.ratelimit import RateLimiter

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
REDDIT_RISING_URL = "https://www.reddit.com/r/all/rising.json?limit=25"
USER_AGENT = "meme-studio-shadow/0.1 (dry-run read)"
_HTTP_OK = 200
_CACHE_TTL_SECONDS = 300.0
_GOOGLE_KEY = "google-news-rss"
_REDDIT_KEY = "reddit-rising"
_Transport = Callable[[str, Mapping[str, str], str], tuple[int, bytes]]


class SourceParseError(ValueError):
    """A public payload could not be parsed. The body is not part of the message."""


@dataclass(frozen=True)
class FetchResult:
    """Items from one source, or a skip reason when the source stayed off."""

    items: tuple[RawItem, ...]
    skipped_reason: str | None = None


class GoogleNewsRssSource:
    """Fetch and cache the public Google News RSS feed."""

    def __init__(
        self,
        *,
        transport: _Transport,
        cache: TtlCache,
        limiter: RateLimiter,
        clock: Callable[[], float],
    ) -> None:
        """Create a reader that does not open sockets by itself.

        Args:
            transport: ``(url, headers, method) -> (status, body)``.
            cache: TTL cache for the parsed items.
            limiter: Call window for this host.
            clock: Available for callers that share a fake clock with the cache.
        """
        self._transport = transport
        self._cache = cache
        self._limiter = limiter
        self._clock = clock

    def fetch(self) -> tuple[RawItem, ...]:
        """Return parsed headlines, using the cache inside its TTL.

        Returns:
            RSS items. A second call inside the TTL does not touch the transport.

        Raises:
            SourceParseError: When the status or the XML is unusable.
        """
        cached = self._cache.get(_GOOGLE_KEY)
        if isinstance(cached, tuple):
            return cached
        self._limiter.acquire()
        _ = self._clock()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml"}
        status, body = self._transport(GOOGLE_NEWS_RSS_URL, headers, "GET")
        if status != _HTTP_OK:
            message = "google news status"
            raise SourceParseError(message)
        items = parse_google_news_rss(body)
        self._cache.set(_GOOGLE_KEY, items, ttl=_CACHE_TTL_SECONDS)
        return items


class RedditPublicJsonSource:
    """Fetch Reddit rising JSON only when the operator allows the terms."""

    def __init__(
        self,
        *,
        transport: _Transport,
        cache: TtlCache,
        limiter: RateLimiter,
        tos_allowed: bool,
        clock: Callable[[], float],
    ) -> None:
        """Create a reader.

        Args:
            transport: ``(url, headers, method) -> (status, body)``.
            cache: TTL cache for the parsed items.
            limiter: Call window for this host.
            tos_allowed: True only when ``REDDIT_TOS_ALLOWED`` is exactly ``1``.
            clock: Shared clock hook. Unused for the skip path.
        """
        self._transport = transport
        self._cache = cache
        self._limiter = limiter
        self._tos_allowed = tos_allowed
        self._clock = clock

    def fetch(self) -> FetchResult:
        """Return rising posts, or a skip reason when terms are not allowed.

        Returns:
            Items plus a null skip reason, or an empty tuple and
            ``reddit_tos_unknown``. The transport is not called when skipped.

        Raises:
            SourceParseError: When an allowed fetch returns a bad payload.
        """
        if not self._tos_allowed:
            return FetchResult(items=(), skipped_reason="reddit_tos_unknown")
        cached = self._cache.get(_REDDIT_KEY)
        if isinstance(cached, tuple):
            return FetchResult(items=cached, skipped_reason=None)
        self._limiter.acquire()
        _ = self._clock()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        status, body = self._transport(REDDIT_RISING_URL, headers, "GET")
        if status != _HTTP_OK:
            message = "reddit status"
            raise SourceParseError(message)
        items = parse_reddit_listing(body)
        self._cache.set(_REDDIT_KEY, items, ttl=_CACHE_TTL_SECONDS)
        return FetchResult(items=items, skipped_reason=None)


def parse_google_news_rss(body: bytes) -> tuple[RawItem, ...]:
    """Parse a Google News RSS document.

    Args:
        body: XML bytes. They are not copied into errors.

    Returns:
        One item per ``item`` element that has a title and a date. A missing
        ``source`` tag falls back to a `` - Outlet`` suffix on the title.

    Raises:
        SourceParseError: When the XML does not parse.
    """
    try:
        root = ElementTree.fromstring(body)  # noqa: S314
    except ElementTree.ParseError:
        raise_without_context(SourceParseError("rss parse failed"))
    items: list[RawItem] = []
    for node in root.iter("item"):
        parsed = _rss_item(node)
        if parsed is not None:
            items.append(parsed)
    return tuple(items)


def parse_reddit_listing(body: bytes) -> tuple[RawItem, ...]:
    """Parse a Reddit listing JSON document.

    Args:
        body: JSON bytes. They are not copied into errors.

    Returns:
        One item per child with a title and ``created_utc``.

    Raises:
        SourceParseError: When the JSON shape is not a listing.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise_without_context(SourceParseError("reddit parse failed"))
    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        message = "reddit parse failed"
        raise SourceParseError(message)
    items: list[RawItem] = []
    for child in children:
        parsed = _reddit_child(child)
        if parsed is not None:
            items.append(parsed)
    return tuple(items)


def _rss_item(node: ElementTree.Element) -> RawItem | None:
    title = _xml_text(node, "title")
    link = _xml_text(node, "link")
    published_raw = _xml_text(node, "pubDate")
    if not title or not published_raw:
        return None
    source_node = node.find("source")
    outlet = ""
    if source_node is not None and source_node.text and source_node.text.strip():
        outlet = source_node.text.strip()
    elif " - " in title:
        headline, suffix = title.rsplit(" - ", 1)
        title = headline.strip()
        outlet = suffix.strip()
    try:
        published = parsedate_to_datetime(published_raw)
    except (TypeError, ValueError):
        raise_without_context(SourceParseError("rss parse failed"))
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    else:
        published = published.astimezone(UTC)
    return RawItem(
        source="google_news",
        title=title.strip(),
        summary=title.strip(),
        published_at=published,
        outlet=outlet or "unknown",
        url=link,
    )


def _reddit_child(child: object) -> RawItem | None:
    if not isinstance(child, dict):
        return None
    data = child.get("data")
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    created = data.get("created_utc")
    if not isinstance(title, str) or not isinstance(created, int | float):
        return None
    permalink = data.get("permalink")
    url = "https://www.reddit.com" + permalink if isinstance(permalink, str) else ""
    subreddit = data.get("subreddit")
    outlet = subreddit if isinstance(subreddit, str) else "reddit"
    summary = data.get("selftext")
    text = summary if isinstance(summary, str) and summary.strip() else title
    return RawItem(
        source="reddit",
        title=title,
        summary=text,
        published_at=datetime.fromtimestamp(float(created), UTC),
        outlet=outlet,
        url=url,
    )


def _xml_text(node: ElementTree.Element, tag: str) -> str:
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()
