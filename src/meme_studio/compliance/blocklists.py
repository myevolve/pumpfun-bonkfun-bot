"""Blocklists shipped with the linter. Extend them by editing the data files."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=32)
def _lines(name: str) -> tuple[str, ...]:
    path = _DATA / name
    rows: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if line and not line.startswith("#"):
            rows.append(line)
    return tuple(rows)


def _phrase_hit(text: str, phrases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for phrase in phrases:
        if " " in phrase or "-" in phrase:
            if phrase in lowered:
                return True
            continue
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            return True
    return False


def person_blocked(text: str, ticker: str = "") -> bool:
    """Return True when text or ticker names a blocked real person."""
    if ticker and ticker.upper() in {item.upper() for item in _lines("tickers.txt")}:
        return True
    return _phrase_hit(text, _lines("people.txt"))


def brand_blocked(text: str) -> bool:
    """Return True when text names a blocked brand or trade dress."""
    return _phrase_hit(text, _lines("brands.txt"))


def franchise_or_artist(text: str) -> bool:
    """Return True for living-artist style cues, franchises, or artist names."""
    if re.search(r"in the style of", text, re.IGNORECASE):
        return True
    if _phrase_hit(text, _lines("franchises.txt")):
        return True
    return _phrase_hit(text, _lines("artists.txt"))


def tragedy_hit(text: str) -> bool:
    """Return True when text hits the sensitive-event list."""
    return _phrase_hit(text, _lines("sensitive.txt"))


def hate_hit(text: str) -> bool:
    """Return True when text contains a blocked hate phrase."""
    return _phrase_hit(text, _lines("hate.txt"))


def paid_engagement_hit(dependency: str) -> bool:
    """Return True when a dependency looks like a paid-engagement service."""
    return _phrase_hit(dependency, _lines("paid_engagement.txt"))
