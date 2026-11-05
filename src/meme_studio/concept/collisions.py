"""Collision checks for a draft name and ticker.

Missing public lookup data fails closed. DexScreener's public search exposes
24-hour volume, not a 7-day total, so ``volume_7d_usd`` stores that h24
figure and the gap stays in the concept unknowns.
"""

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from urllib.parse import quote

from meme_studio.concept.blocklist import BRANDS, find_brands
from meme_studio.concept.safety import contains_hate, find_persons
from meme_studio.concept.schema import (
    BANNED_PHRASES,
    DESCRIPTION_MIN_WORDS,
    GAME_TEMPLATE_IDS,
    NAME_MAX,
    CollisionCheck,
    ConceptCandidate,
    has_shadow_disclosure,
)
from meme_studio.errors import raise_without_context
from meme_studio.trends.cache import TtlCache
from meme_studio.trends.ratelimit import RateLimiter

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search?q="
_HTTP_OK = 200
_CACHE_TTL_SECONDS = 300.0
# Design rules from the pipeline spec, not measurements from this repository.
# Whether these cutoffs change buyer behavior is UNKNOWN.
_MCAP_ABOVE_USD = 10_000
_NAME_DISTANCE_MAX = 2
_TICKER_DISTANCE_MAX = 1
_TICKER_FUZZY_MIN = 5
_USER_AGENT = "meme-studio-shadow/0.1 (dry-run read)"
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{2,9}$")
_HEX_COLOR = re.compile(r"^#[0-9A-F]{6}$")
_SOLANA = frozenset({"", "solana", "sol"})
_Transport = Callable[[str, Mapping[str, str], str], tuple[int, bytes]]


class LookupUnavailableError(Exception):
    """The public token lookup could not be used. Callers fail closed."""


@dataclass(frozen=True)
class TokenHit:
    """One public coin row. Volume is the provider's h24 figure when proxied."""

    symbol: str
    name: str
    volume_7d_usd: float | None
    market_cap_usd: float | None
    chain: str


class TokenLookup:
    """Public, read-only ticker search. Implementations must not sign or trade."""

    def search(self, query: str) -> list[TokenHit]:
        """Return hits for ``query``.

        Args:
            query: Ticker or name.

        Returns:
            Hits the checker will fuzzy-match. An empty list means no hits.

        Raises:
            LookupUnavailableError: When the lookup cannot answer. That is a fail.
        """
        raise LookupUnavailableError(query)


class MemoryTokenLookup(TokenLookup):
    """In-memory public list. ``search`` returns every loaded hit."""

    def __init__(self, hits: Sequence[TokenHit]) -> None:
        """Store hits.

        Args:
            hits: Coins already loaded from a fixture or a local log.
        """
        self._hits = list(hits)

    def search(self, query: str) -> list[TokenHit]:
        """Return the full list.

        Args:
            query: Ignored. The checker applies exact and fuzzy rules itself.

        Returns:
            A copy of the loaded hits.
        """
        del query
        return list(self._hits)


class HttpTokenLookup(TokenLookup):
    """DexScreener public search over an injected GET transport."""

    def __init__(
        self,
        *,
        transport: _Transport,
        limiter: RateLimiter,
        cache: TtlCache,
    ) -> None:
        """Create a lookup that does not open sockets by itself.

        Args:
            transport: ``(url, headers, method) -> (status, body)``.
            limiter: Call window for this host.
            cache: TTL cache. Keys are the query, never a credential.
        """
        self._transport = transport
        self._limiter = limiter
        self._cache = cache

    def search(self, query: str) -> list[TokenHit]:
        """Search the public dex endpoint.

        Args:
            query: Ticker or name.

        Returns:
            Solana hits parsed from the body.

        Raises:
            LookupUnavailableError: On transport, status, or payload failure.
                The body is not included in the message.
        """
        key = f"dex:{query}"
        cached = self._cache.get(key)
        if isinstance(cached, list):
            return cached
        self._limiter.acquire()
        url = dexscreener_search_url(query)
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        try:
            status, body = self._transport(url, headers, "GET")
        except (TimeoutError, OSError) as exc:
            message = "token lookup failed"
            raise LookupUnavailableError(message) from exc
        if status != _HTTP_OK:
            message = "token lookup status"
            raise LookupUnavailableError(message)
        hits = parse_dexscreener_search(body)
        self._cache.set(key, hits, ttl=_CACHE_TTL_SECONDS)
        return hits


def dexscreener_search_url(query: str) -> str:
    """Return the public search URL for one query.

    Args:
        query: Ticker or name. It is percent-encoded.

    Returns:
        An ``https`` URL on ``api.dexscreener.com``.
    """
    return DEXSCREENER_SEARCH + quote(query)


def parse_dexscreener_search(body: bytes) -> list[TokenHit]:
    """Parse a DexScreener search body into Solana hits.

    Args:
        body: Response bytes. They are not copied into errors.

    Returns:
        Solana pairs. Other chains are dropped. A missing chain is kept.

    Raises:
        LookupUnavailableError: When the body is not the expected JSON object.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise_without_context(LookupUnavailableError("token lookup payload"))
    if not isinstance(payload, dict) or "pairs" not in payload:
        raise_without_context(LookupUnavailableError("token lookup payload"))
    pairs = payload["pairs"]
    if not isinstance(pairs, list):
        raise_without_context(LookupUnavailableError("token lookup payload"))
    hits: list[TokenHit] = []
    for pair in pairs:
        if not _pair_well_formed(pair):
            raise_without_context(LookupUnavailableError("token lookup payload"))
        hit = _pair_hit(pair)
        if hit is not None:
            hits.append(hit)
    return hits


def levenshtein(left: str, right: str) -> int:
    """Return the Levenshtein distance between two strings.

    Args:
        left: First string.
        right: Second string.

    Returns:
        The edit distance. Insertion, deletion, and substitution cost 1.
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            replace_cost = previous[j - 1] + (left_char != right_char)
            current.append(min(insert, delete, replace_cost))
        previous = current
    return previous[-1]


def check_candidate(
    candidate: ConceptCandidate,
    *,
    lookup: TokenLookup,
    history: Iterable[str] | None,
) -> ConceptCandidate:
    """Run every collision check and set ``accepted``.

    Args:
        candidate: Unchecked or previously checked draft.
        lookup: Public ticker lookup. Failures become a failed check.
        history: Tickers this studio already used. Matching is case-insensitive.

    Returns:
        A copy with four checks. ``accepted`` also requires a valid shape,
        including a description of at least 20 words.
    """
    checks = (
        _existing_tokens(candidate, lookup),
        _trademark(candidate),
        _real_person(candidate),
        _own_history(candidate, history),
    )
    accepted = _shape_ok(candidate) and all(check.passed for check in checks)
    return replace(candidate, collision_checks=checks, accepted=accepted)


def _existing_tokens(
    candidate: ConceptCandidate, lookup: TokenLookup
) -> CollisionCheck:
    try:
        hits = list(lookup.search(candidate.ticker))
        hits.extend(lookup.search(candidate.name))
    except LookupUnavailableError:
        return CollisionCheck(
            name="existing_tokens", passed=False, reason="lookup_unavailable"
        )
    reasons: list[str] = []
    seen: set[tuple[str, str, str, float, float]] = set()
    for hit in hits:
        key = (
            hit.symbol,
            hit.name,
            hit.chain,
            hit.volume_7d_usd,
            hit.market_cap_usd,
        )
        if key in seen or hit.chain.casefold() not in _SOLANA:
            continue
        seen.add(key)
        reasons.extend(_hit_reasons(candidate, hit))
    if not reasons:
        return CollisionCheck(name="existing_tokens", passed=True, reason="ok")
    unique = list(dict.fromkeys(reasons))
    return CollisionCheck(name="existing_tokens", passed=False, reason="|".join(unique))


def _hit_reasons(candidate: ConceptCandidate, hit: TokenHit) -> list[str]:
    reasons: list[str] = []
    ticker = _alnum(candidate.ticker)
    symbol = _alnum(hit.symbol)
    distance = levenshtein(ticker, symbol)
    ticker_exact = ticker == symbol
    ticker_fuzzy = (
        not ticker_exact
        and distance <= _TICKER_DISTANCE_MAX
        and len(ticker) >= _TICKER_FUZZY_MIN
        and len(symbol) >= _TICKER_FUZZY_MIN
    )
    name_close = (
        levenshtein(_norm_name(candidate.name), _norm_name(hit.name))
        <= _NAME_DISTANCE_MAX
    )
    if ticker_exact or ticker_fuzzy:
        if not _known_metric(hit.volume_7d_usd):
            reasons.append("metrics_unknown")
        elif ticker_exact and hit.volume_7d_usd > 0:
            reasons.append("exact_ticker_volume")
        elif ticker_fuzzy and hit.volume_7d_usd > 0:
            reasons.append("fuzzy_ticker")
    if name_close:
        if not _known_metric(hit.market_cap_usd):
            reasons.append("metrics_unknown")
        elif hit.market_cap_usd > _MCAP_ABOVE_USD:
            reasons.append("fuzzy_name")
    return reasons


def _known_metric(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _trademark(candidate: ConceptCandidate) -> CollisionCheck:
    ticker = _alnum(candidate.ticker).casefold()
    if ticker in BRANDS:
        return CollisionCheck(
            name="trademark", passed=False, reason=f"blocklist:{ticker}"
        )
    blob = " ".join(
        (
            candidate.name,
            candidate.character,
            candidate.joke,
            candidate.description,
            candidate.art_prompt,
        )
    )
    brands = find_brands(blob)
    if brands:
        return CollisionCheck(
            name="trademark", passed=False, reason=f"blocklist:{brands[0]}"
        )
    return CollisionCheck(name="trademark", passed=True, reason="ok")


def _real_person(candidate: ConceptCandidate) -> CollisionCheck:
    blob = " ".join(
        (
            candidate.name,
            candidate.ticker,
            candidate.character,
            candidate.joke,
            candidate.description,
            candidate.art_prompt,
        )
    )
    if find_persons(blob):
        return CollisionCheck(name="real_person", passed=False, reason="person_entity")
    return CollisionCheck(name="real_person", passed=True, reason="ok")


def _own_history(
    candidate: ConceptCandidate,
    history: Iterable[str] | None,
) -> CollisionCheck:
    if history is None:
        return CollisionCheck(
            name="own_history", passed=False, reason="history_unknown"
        )
    known = {item.casefold() for item in history}
    if candidate.ticker.casefold() in known:
        return CollisionCheck(name="own_history", passed=False, reason="ticker_reused")
    return CollisionCheck(name="own_history", passed=True, reason="ok")


def _shape_ok(candidate: ConceptCandidate) -> bool:
    colors = (
        candidate.theme_palette.primary,
        candidate.theme_palette.secondary,
        candidate.theme_palette.accent,
        candidate.theme_palette.background,
    )
    blob = " ".join(
        (candidate.name, candidate.joke, candidate.description, candidate.art_prompt)
    ).casefold()
    name_ok = 1 <= len(candidate.name) <= NAME_MAX
    ticker_ok = _TICKER.fullmatch(candidate.ticker) is not None
    words_ok = len(candidate.description.split()) >= DESCRIPTION_MIN_WORDS
    template_ok = candidate.game_template_id in GAME_TEMPLATE_IDS
    joke_ok = "\n" not in candidate.joke
    colors_ok = all(_HEX_COLOR.fullmatch(color) is not None for color in colors)
    banned_ok = all(phrase not in blob for phrase in BANNED_PHRASES)
    disclosed = has_shadow_disclosure(candidate.description)
    fields = " ".join(
        (
            candidate.name,
            candidate.ticker,
            candidate.character,
            candidate.joke,
            candidate.description,
            candidate.art_prompt,
        )
    )
    hate_ok = not contains_hate(fields)
    return (
        name_ok
        and ticker_ok
        and words_ok
        and template_ok
        and joke_ok
        and colors_ok
        and banned_ok
        and disclosed
        and hate_ok
    )


def _pair_well_formed(pair: object) -> bool:
    if not isinstance(pair, dict):
        return False
    base = pair.get("baseToken")
    if not isinstance(base, dict):
        return False
    symbol = base.get("symbol")
    name = base.get("name")
    if not isinstance(symbol, str) and not isinstance(name, str):
        return False
    chain = pair.get("chainId")
    return chain is None or isinstance(chain, str)


def _pair_hit(pair: object) -> TokenHit | None:
    if not isinstance(pair, dict):
        return None
    chain = str(pair.get("chainId") or "")
    if chain.casefold() not in _SOLANA:
        return None
    base = pair.get("baseToken")
    if not isinstance(base, dict):
        return None
    symbol = str(base.get("symbol") or "")
    name = str(base.get("name") or "")
    h24 = _metric(pair.get("volume"), "h24")
    mcap = _metric(pair, "marketCap")
    return TokenHit(symbol, name, h24, mcap, chain)


def _metric(payload: object, key: str) -> float | None:
    """Return a finite metric, or None when the field is absent or unusable."""
    if not isinstance(payload, dict) or key not in payload:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _alnum(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
