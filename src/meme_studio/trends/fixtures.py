"""Load a dry-run fixture directory. Missing files are skipped, not fetched."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meme_studio.concept.collisions import MemoryTokenLookup, TokenHit, TokenLookup
from meme_studio.concept.inputs import CopycatCoin, CountPoint
from meme_studio.trends.collect import build_stories
from meme_studio.trends.models import RawItem, TrendSignal
from meme_studio.trends.sources import parse_google_news_rss, parse_reddit_listing


@dataclass(frozen=True)
class FixtureBundle:
    """Everything the dry-run needs from disk."""

    items: tuple[RawItem, ...]
    trends: tuple[TrendSignal, ...]
    copycats: tuple[CopycatCoin, ...] | None
    lookup: TokenLookup | None
    history: tuple[str, ...]
    hour_weights: dict[int, float] | None
    priors: dict[str, float] | None
    unknowns: tuple[str, ...]


def empty_bundle() -> FixtureBundle:
    """Return a bundle with no sources and no token lookup.

    Returns:
        An empty bundle. The pipeline records ``no_sources``.
    """
    return FixtureBundle(
        items=(),
        trends=(),
        copycats=None,
        lookup=None,
        history=(),
        hour_weights=None,
        priors=None,
        unknowns=(),
    )


def load_fixtures(path: Path, *, reddit_tos_allowed: bool) -> FixtureBundle:
    """Read optional fixture files from ``path``.

    Args:
        path: Directory of RSS and JSON fixtures.
        reddit_tos_allowed: When false, ``reddit.json`` is not parsed and the
            skip is recorded as an unknown.

    Returns:
        Items, trends, an in-memory lookup when ``tokens.json`` exists, and
        history. A missing ``tokens.json`` leaves the lookup unset so the
        concept stage fails closed.
    """
    items: list[RawItem] = []
    trends: list[TrendSignal] = []
    unknowns: list[str] = []
    rss = path / "google_news.rss"
    if rss.is_file():
        items.extend(parse_google_news_rss(rss.read_bytes()))
    x_path = path / "x.json"
    if x_path.is_file():
        x_items, x_trends = _load_x(x_path)
        items.extend(x_items)
        trends.extend(x_trends)
    reddit = path / "reddit.json"
    if reddit.is_file():
        if reddit_tos_allowed:
            items.extend(parse_reddit_listing(reddit.read_bytes()))
        else:
            unknowns.append("reddit_source_skipped_tos_unknown")
    tokens_path = path / "tokens.json"
    lookup: TokenLookup | None = None
    if tokens_path.is_file():
        lookup = MemoryTokenLookup(_load_tokens(tokens_path))
    history_path = path / "history.json"
    history = _load_history(history_path) if history_path.is_file() else ()
    copycats_path = path / "copycats.json"
    copycats = _load_copycats(copycats_path) if copycats_path.is_file() else None
    weights_path = path / "hour_weights.json"
    priors_path = path / "priors.json"
    return FixtureBundle(
        items=tuple(items),
        trends=tuple(trends),
        copycats=copycats,
        lookup=lookup,
        history=history,
        hour_weights=_load_weights(weights_path) if weights_path.is_file() else None,
        priors=_load_priors(priors_path) if priors_path.is_file() else None,
        unknowns=tuple(unknowns),
    )


def grouped_stories(bundle: FixtureBundle, *, now: datetime) -> tuple:
    """Group a bundle's items. Present so the CLI stays thin.

    Args:
        bundle: Loaded fixtures.
        now: Clock for trend-only stories.

    Returns:
        Story inputs before copycats are attached.
    """
    return build_stories(bundle.items, trends=bundle.trends, now=now)


def _load_x(path: Path) -> tuple[list[RawItem], list[TrendSignal]]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return [], []
    items: list[RawItem] = []
    for row in payload.get("news") or []:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        published = row.get("published_at")
        if not isinstance(published, str):
            continue
        title = str(row["title"])
        outlet = row.get("outlet")
        url = row.get("url")
        summary = row.get("summary")
        items.append(
            RawItem(
                source="x",
                title=title,
                summary=summary if isinstance(summary, str) else title,
                published_at=datetime.fromisoformat(published),
                outlet=outlet if isinstance(outlet, str) else "x",
                url=url if isinstance(url, str) else "",
            )
        )
    trends: list[TrendSignal] = []
    for row in payload.get("trends") or []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        counts: list[CountPoint] = []
        for bucket in row.get("counts") or []:
            if not isinstance(bucket, dict):
                continue
            start = bucket.get("start")
            count = bucket.get("count")
            if isinstance(start, str) and isinstance(count, int):
                counts.append(CountPoint(datetime.fromisoformat(start), count))
        trends.append(TrendSignal(name=str(row["name"]), post_counts=tuple(counts)))
    return items, trends


def _load_tokens(path: Path) -> list[TokenHit]:
    payload = _read_json(path)
    if not isinstance(payload, list):
        return []
    hits: list[TokenHit] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        name = row.get("name")
        if not isinstance(symbol, str) or not isinstance(name, str):
            continue
        chain = row.get("chain")
        hits.append(
            TokenHit(
                symbol,
                name,
                _optional_metric(row, "volume_7d_usd"),
                _optional_metric(row, "market_cap_usd"),
                chain if isinstance(chain, str) else "",
            )
        )
    return hits


def _load_history(path: Path) -> tuple[str, ...]:
    payload = _read_json(path)
    if not isinstance(payload, list):
        return ()
    return tuple(item for item in payload if isinstance(item, str))


def _load_copycats(path: Path) -> tuple[CopycatCoin, ...]:
    payload = _read_json(path)
    if not isinstance(payload, list):
        return ()
    coins: list[CopycatCoin] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        ticker = row.get("ticker")
        buyers = row.get("outside_buyers")
        if not isinstance(name, str) or not isinstance(ticker, str):
            continue
        if not isinstance(buyers, int):
            continue
        volume = row.get("volume_7d_usd", 0)
        market = row.get("market_cap_usd", 0)
        coins.append(
            CopycatCoin(
                name,
                ticker,
                buyers,
                float(volume) if isinstance(volume, int | float) else 0.0,
                float(market) if isinstance(market, int | float) else 0.0,
            )
        )
    return tuple(coins)


def _load_weights(path: Path) -> dict[int, float]:
    payload = _read_json(path)
    weights: dict[int, float] = {}
    if not isinstance(payload, dict):
        return weights
    for key, value in payload.items():
        if isinstance(value, int | float):
            weights[int(key)] = float(value)
    return weights


def _load_priors(path: Path) -> dict[str, float]:
    payload = _read_json(path)
    priors: dict[str, float] = {}
    if not isinstance(payload, dict):
        return priors
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, int | float):
            priors[key] = float(value)
    return priors


def _optional_metric(row: dict[str, object], key: str) -> float | None:
    if key not in row:
        return None
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
