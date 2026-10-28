"""Trademark words the concept stage refuses to put on a coin.

USPTO bulk search is not called. Whether automated querying is allowed is an
open question, so the fallback is this local list.
"""

import re

BRANDS: frozenset[str] = frozenset(
    {
        "adidas",
        "adobe",
        "amazon",
        "apple",
        "chatgpt",
        "cocacola",
        "disney",
        "facebook",
        "fortnite",
        "google",
        "gucci",
        "instagram",
        "intel",
        "iphone",
        "lego",
        "marvel",
        "mcdonalds",
        "meta",
        "microsoft",
        "minecraft",
        "netflix",
        "nike",
        "nintendo",
        "nvidia",
        "openai",
        "paypal",
        "pepsi",
        "playstation",
        "pokemon",
        "roblox",
        "samsung",
        "sony",
        "spotify",
        "starbucks",
        "tesla",
        "tiktok",
        "uber",
        "walmart",
        "whatsapp",
        "xbox",
        "youtube",
    }
)

_MCDONALDS = re.compile(r"\bmcdonald'?s?\b", re.IGNORECASE)


def find_brands(text: str) -> tuple[str, ...]:
    """Return blocklisted marks that appear as whole words, in text order.

    Args:
        text: Untrusted headline, name, or prompt.

    Returns:
        Brand keys from ``BRANDS``. Empty when nothing matches.
    """
    folded = text.casefold()
    found: list[tuple[int, str]] = []
    for brand in BRANDS:
        match = re.search(rf"\b{re.escape(brand)}\b", folded)
        if match is not None:
            found.append((match.start(), brand))
    mcdonalds = _MCDONALDS.search(text)
    if mcdonalds is not None and "mcdonalds" not in {brand for _, brand in found}:
        found.append((mcdonalds.start(), "mcdonalds"))
    found.sort(key=lambda item: item[0])
    return tuple(brand for _, brand in found)
