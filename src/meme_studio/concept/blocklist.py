"""Trademark words the concept stage refuses to put on a coin.

USPTO bulk search is not called. Whether automated querying is allowed is an
open question, so the fallback is this local list.
"""

import re
import unicodedata

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
    """Return blocklisted marks that appear as words, possessives, or compounds.

    Unicode dashes and apostrophes are folded to ASCII. Hyphens stay in place
    so ``Nike-branded`` still has a ``nike`` word boundary. ``Coca-Cola`` and
    a non-breaking hyphen spelling match the ``cocacola`` mark.

    Args:
        text: Untrusted headline, name, or prompt.

    Returns:
        Brand keys from ``BRANDS``. Empty when nothing matches.
    """
    folded = _fold_brand_text(text)
    found: list[tuple[int, str]] = []
    for brand in BRANDS:
        if _brand_in(folded, brand):
            found.append((folded.find(brand), brand))
    if _MCDONALDS.search(folded) and "mcdonalds" not in {brand for _, brand in found}:
        found.append((0, "mcdonalds"))
    found.sort(key=lambda item: item[0])
    return tuple(brand for _, brand in found)


def _brand_in(folded: str, brand: str) -> bool:
    forms = [brand]
    if brand == "cocacola":
        forms.append("coca-cola")
    if brand == "mcdonalds":
        forms.extend(("mcdonald's", "mcdonalds"))
    for form in forms:
        if re.search(rf"\b{re.escape(form)}\b", folded):
            return True
        if re.search(rf"\b{re.escape(form)}(?:'s)?\b", folded):
            return True
        if re.search(rf"\b{re.escape(form)}-[a-z0-9]", folded):
            return True
    return False


_DASHES = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d]")
_APOSTROPHES = re.compile(r"[\u2018\u2019\u201b\u2032\uff07]")


def _fold_brand_text(text: str) -> str:
    """Fold diacritics, dashes, and apostrophes without gluing words together."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = stripped.casefold()
    folded = _DASHES.sub("-", folded)
    return _APOSTROPHES.sub("'", folded)
