"""Banned promise language, with leetspeak folded back to letters."""

from __future__ import annotations

import re

_LEET = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
    }
)
_ALLOWED = (
    "not investment advice",
    "not an investment",
    "isn't an investment",
    "is not an investment",
    "not guaranteed",
    "no guarantees",
    "no guarantee",
)
_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?<![0-9])1000x\b", "1000x"),
    (r"(?<![0-9])100x\b", "100x"),
    (r"\bguaranteed\b", "guaranteed"),
    (r"\bcan(?:not|'t|t) lose\b", "cant_lose"),
    (r"\beasy money\b", "easy_money"),
    (
        r"\bnext\s+(?:bitcoin|btc|ethereum|eth|doge|dogecoin|pepe|shib|shiba)\b",
        "next_coin",
    ),
    (r"\bget in early\b", "get_in_early"),
    (r"\bdo not miss\b|\bdon't miss\b|\bdont miss\b", "dont_miss"),
    (r"\bprice targets?\b", "price_target"),
    (r"\bfinancial freedom\b", "financial_freedom"),
    (r"\bpassive income\b", "passive_income"),
    (r"\breturns\b", "returns"),
    (r"\bprofit for holders\b", "profit_for_holders"),
    (r"\binvestments?\b", "investment"),
    (r"\broadmaps?\b", "roadmap"),
    (r"\butility\b", "utility"),
    (r"\bstaking rewards\b", "staking_rewards"),
    (r"\bbuybacks?\b", "buyback"),
    (r"\bmooning\b|\bmoonshot\b|\bmoon\b", "moon"),
)


def normalize_promises(text: str) -> str:
    """Lowercase, undo leetspeak, and drop allowed negation phrases.

    Multiplier tokens such as ``100x`` stay numeric. The digit ``1`` is also
    leetspeak for ``i``, and translating it first would hide ``100x``.
    """
    lowered = text.lower().replace("\u2019", "'")
    kept: list[str] = []

    def _hold(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f" qqk{'a' * len(kept)}qq "

    lowered = re.sub(r"\d+x\b", _hold, lowered)
    lowered = lowered.translate(_LEET)
    for index, token in enumerate(kept, start=1):
        lowered = lowered.replace(f" qqk{'a' * index}qq ", f" {token} ")
    for phrase in _ALLOWED:
        lowered = lowered.replace(phrase, " ")
    return lowered


def banned_phrase_reasons(text: str) -> list[str]:
    """Return reason codes for price promises and return language."""
    normalized = normalize_promises(text)
    reasons: list[str] = []
    for pattern, code in _PATTERNS:
        if re.search(pattern, normalized):
            reasons.append(f"banned_phrase:{code}")
    return reasons
