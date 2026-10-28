"""Comic-hook extraction for the v1 parodyability rule.

The later fitted model replaces this. v1 only recognizes an animal, an object,
or an absurd phrase that is not itself a brand.
"""

import re
from dataclasses import dataclass

from meme_studio.concept.blocklist import find_brands

ANIMALS: tuple[str, ...] = (
    "raccoon",
    "goose",
    "panda",
    "otter",
    "penguin",
    "frog",
    "pigeon",
    "squirrel",
    "moose",
    "llama",
    "alpaca",
    "rooster",
    "parrot",
    "owl",
    "fox",
    "koala",
    "sloth",
    "hamster",
    "turtle",
    "hedgehog",
    "platypus",
    "axolotl",
    "pelican",
    "flamingo",
    "badger",
    "corgi",
    "ferret",
)

OBJECTS: tuple[str, ...] = (
    "donut",
    "pretzel",
    "balloon",
    "waffle",
    "toaster",
    "potato",
    "sandwich",
    "pickle",
    "cactus",
    "umbrella",
    "tractor",
    "sock",
    "fridge",
    "blender",
    "pumpkin",
    "noodle",
    "cheese",
    "pizza",
    "burrito",
    "satellite",
    "rocket",
    "canoe",
    "hammock",
    "kettle",
    "muffin",
    "pancake",
)

_ABSURD_PHRASES: tuple[str, ...] = (
    "refuses to",
    "refused to",
    "mistaken for",
    "stuck in",
    "stole a",
    "stole the",
)

_ABSURD_WORDS: tuple[str, ...] = (
    "escaped",
    "escape",
    "accidentally",
    "befriends",
    "parade",
)

# Unvalidated design assumptions for the v1 rubric. They are not a fitted
# model, and their empirical effect on buyers is UNKNOWN.
ANIMAL_WEIGHT = 0.5
OBJECT_WEIGHT = 0.4
ABSURD_WEIGHT = 0.3


@dataclass(frozen=True)
class Hook:
    """Subjects a parody can hang on without naming a person."""

    animals: tuple[str, ...]
    objects: tuple[str, ...]
    brands: tuple[str, ...]
    absurd: bool

    @property
    def label(self) -> str:
        """Return ``animal+object``, or whichever side exists."""
        animal = self.animals[0] if self.animals else ""
        obj = self.objects[0] if self.objects else ""
        if animal and obj:
            return f"{animal}+{obj}"
        return animal or obj

    @property
    def category(self) -> str:
        """Return ``animal``, ``object``, or ``general``."""
        if self.animals:
            return "animal"
        if self.objects:
            return "object"
        return "general"

    @property
    def parodyability(self) -> float:
        """Return the v1 0-1 rubric, capped at 1."""
        score = 0.0
        if self.animals:
            score += ANIMAL_WEIGHT
        if self.objects:
            score += OBJECT_WEIGHT
        if self.absurd:
            score += ABSURD_WEIGHT
        if score > 1.0:
            return 1.0
        return score


def extract(text: str) -> Hook:
    """Find the earliest animal, object, brand, and absurd phrase.

    Args:
        text: Headline plus summary.

    Returns:
        A hook. Lists are in order of appearance and use canonical spellings.
    """
    return Hook(
        animals=_vocab(text, ANIMALS),
        objects=_vocab(text, OBJECTS),
        brands=find_brands(text),
        absurd=_absurd(text),
    )


def _vocab(text: str, words: tuple[str, ...]) -> tuple[str, ...]:
    found: list[tuple[int, str]] = []
    for word in words:
        match = re.search(rf"\b{re.escape(word)}s?\b", text, re.IGNORECASE)
        if match is not None:
            found.append((match.start(), word))
    found.sort(key=lambda item: item[0])
    seen: list[str] = []
    for _, word in found:
        if word not in seen:
            seen.append(word)
    return tuple(seen)


def _absurd(text: str) -> bool:
    folded = text.casefold()
    for phrase in _ABSURD_PHRASES:
        if phrase in folded:
            return True
    return any(
        re.search(rf"\b{re.escape(word)}\b", folded) is not None
        for word in _ABSURD_WORDS
    )
