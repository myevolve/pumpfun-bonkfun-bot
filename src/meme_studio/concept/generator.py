"""Deterministic name, ticker, and character drafts. No model call."""

from collections.abc import Sequence

from meme_studio.concept.collisions import TokenLookup, check_candidate
from meme_studio.concept.hooks import extract
from meme_studio.concept.schema import (
    SHADOW_DISCLOSURE,
    ConceptCandidate,
    StoryCard,
    ThemePalette,
)

PALETTES: tuple[ThemePalette, ...] = (
    ThemePalette("#1B4D3E", "#F4E8C1", "#E07A5F", "#102A22"),
    ThemePalette("#243B6B", "#F2E9E4", "#E9C46A", "#0D1B2A"),
    ThemePalette("#3D2B1F", "#F6E7D8", "#C44900", "#1A120B"),
    ThemePalette("#1D3557", "#F1FAEE", "#E63946", "#0B1320"),
    ThemePalette("#2D6A4F", "#D8F3DC", "#FFB703", "#081C15"),
)
_JOKES = (
    "A cartoon {animal} treats a {obj} like a state secret.",
    "The {obj} picks a cartoon {animal} as parade marshal.",
    "Captain of nothing, the cartoon {animal} guards one {obj}.",
    "A {obj} parade needs a cartoon {animal} with a whistle.",
    "For one hour the cartoon {animal} only answers to the {obj}.",
)
_TICKER_PART = 4
_TICKER_MAX = 10
_TICKER_MIN = 3


def draft_candidates(title: str, summary: str) -> tuple[ConceptCandidate, ...]:
    """Build five unchecked drafts from the first animal and object.

    Args:
        title: Story headline.
        summary: Story summary. Combined with the title for hook extraction.

    Returns:
        Five candidates, or an empty tuple when there is no animal or object.
        Collision checks are empty and ``accepted`` is false.
    """
    hook = extract(f"{title}\n{summary}")
    animal = hook.animals[0] if hook.animals else None
    obj = hook.objects[0] if hook.objects else None
    if animal is None and obj is None:
        return ()
    if animal is None:
        animal = obj or "mascot"
    if obj is None:
        obj = "parade"
    specs = (
        (f"{animal.title()} {obj.title()}", _ticker(animal, obj), "dodge", 0),
        (f"{obj.title()} {animal.title()}", _ticker(obj, animal), "runner", 1),
        (f"Captain {animal.title()}", _ticker("cap", animal), "flappy", 2),
        (f"{obj.title()} Parade", _ticker(obj, "parade"), "clicker", 3),
        (f"{animal.title()} Hour", _ticker(animal, "hour"), "quiz", 4),
    )
    drafts: list[ConceptCandidate] = []
    for name, ticker, template, index in specs:
        joke = _JOKES[index].format(animal=animal, obj=obj)
        drafts.append(
            ConceptCandidate(
                name=name,
                ticker=ticker,
                character=f"{name} is an original cartoon {animal}",
                joke=joke,
                description=_description(name, animal, obj, joke),
                game_template_id=template,
                theme_palette=PALETTES[
                    sum(ord(char) for char in ticker) % len(PALETTES)
                ],
                art_prompt=(
                    f"original cartoon {animal} character holding a {obj}, "
                    "flat colors, no real person, no logo, no letters"
                ),
                collision_checks=(),
                accepted=False,
            )
        )
    return tuple(drafts)


def select_concept(
    stories: Sequence[StoryCard],
    *,
    lookup: TokenLookup,
    history: Sequence[str] | None,
) -> tuple[StoryCard | None, tuple[ConceptCandidate, ...], str | None]:
    """Pick the first safe story that still has a clean candidate.

    Args:
        stories: Ranked safe cards, best first.
        lookup: Public ticker lookup.
        history: Tickers already used by this studio.

    Returns:
        The chosen story, the reviewed candidates kept for the report, and a
        skip reason. The skip reason is None when a candidate is accepted.
        A lookup failure stops the loop and fails closed. If every candidate
        collides, the first story's reviewed drafts are returned.
    """
    first: tuple[ConceptCandidate, ...] = ()
    for story in stories:
        drafts = draft_candidates(story.title, story.summary)
        if not drafts:
            continue
        reviewed = tuple(
            check_candidate(draft, lookup=lookup, history=history) for draft in drafts
        )
        if any(item.accepted for item in reviewed):
            return story, reviewed, None
        if any(
            check.name == "existing_tokens" and check.reason == "lookup_unavailable"
            for item in reviewed
            for check in item.collision_checks
        ):
            return None, reviewed, "lookup_unavailable"
        if not first:
            first = reviewed
    if first:
        return None, first, "all_candidates_collided"
    return None, (), "no_safe_story"


def _ticker(left: str, right: str) -> str:
    raw = (_stem(left) + _stem(right)).upper()
    if len(raw) < _TICKER_MIN:
        raw = (raw + "XXX")[:_TICKER_MIN]
    return raw[:_TICKER_MAX]


def _stem(word: str) -> str:
    """Return the first four alphanumeric characters.

    Args:
        word: Animal, object, or prefix.

    Returns:
        Up to four characters. ``raccoon`` stems to ``racc`` and ``goose``
        stems to ``goos``.
    """
    folded = "".join(char for char in word.casefold() if char.isalnum())
    return folded[:_TICKER_PART]


def _description(name: str, animal: str, obj: str, joke: str) -> str:
    return (
        f"{name} is an original cartoon {animal} in a daily news parody. "
        f"{joke} "
        f"The browser game follows the {animal} and a {obj} through one short level. "
        f"This draft is a collectible meme concept with no launch, no posting, "
        f"and no claim that a holder will gain anything. "
        f"The character is invented and is not a real person or a brand. "
        f"{SHADOW_DISCLOSURE}"
    )
