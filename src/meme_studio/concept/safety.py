"""Hard safety gate. A hit forces the story score to zero and drops the draft.

Rules cover tragedies, minors, election-disinformation tropes, named people,
defamation-shaped claims, and a hook that is only a brand. The person pass is
a heuristic, not a trained NER model.
"""

import re
from dataclasses import dataclass

from meme_studio.concept.hooks import ANIMALS, OBJECTS, extract

_DEATH = re.compile(
    r"\b(?:died|dies|dead|deaths?|killed|killing|murder(?:ed)?|homicide|"
    r"fatal|fatality|funeral|obituary|massacre|slain|deceased)\b",
    re.IGNORECASE,
)
_INJURY = re.compile(
    r"\b(?:injured|injuries|injury|wounded|wound|hospitalized)\b",
    re.IGNORECASE,
)
_DISASTER = re.compile(
    r"\b(?:disaster|earthquake|hurricane|tornado|tsunami|wildfire|"
    r"flood(?:ing)?|landslide|plane crash)\b",
    re.IGNORECASE,
)
_SHOOTING = re.compile(
    r"\b(?:shooting|shooter|gunman|gunmen|mass shooting|active shooter)\b"
    r"|\b(?:was shot|got shot|shot dead)\b",
    re.IGNORECASE,
)
_TERROR = re.compile(
    r"\b(?:terrorism|terrorists?|terror attack)\b",
    re.IGNORECASE,
)
_MINOR = re.compile(
    r"\b(?:children|child|toddlers?|infants?|babies|baby|teens?|teenagers?|"
    r"underage|minors|kids?|kindergarten)\b",
    re.IGNORECASE,
)
_AGE = re.compile(r"\b(\d{1,2})\s*-?\s*year-?\s*olds?\b", re.IGNORECASE)
_ILLNESS = re.compile(r"\b(?:outbreak|pandemic|epidemic)\b", re.IGNORECASE)
_ABUSE = re.compile(
    r"\b(?:sexual abuse|raped|rape|rapist|molest\w*|trafficking)\b",
    re.IGNORECASE,
)
# Unambiguous hate markers. Matching is case-insensitive and includes the ticker.
_HATE = re.compile(
    r"\b(?:nazis?|neo-?nazis?|swastikas?|kkk|klan)\b|white\s+supremac",
    re.IGNORECASE,
)
_ELECTION_PHRASES = (
    "stolen election",
    "election was stolen",
    "rigged election",
    "rigged the election",
    "rigged vote",
    "fake ballots",
    "ballot stuffing",
    "votes were flipped",
    "flipped the votes",
    "dead people voted",
    "dead voters",
    "stop the steal",
    "election was hacked",
    "hacked voting machines",
)
_DEFAMATION = re.compile(
    r"\b(?:accused|arrested|charged|indicted|convicted|scandal|corrupt)\b",
    re.IGNORECASE,
)
_HONORIFIC = re.compile(
    r"\b(?:prime minister|president|senator|governor|mayor|prince|princess|"
    r"pope|prof|king|queen|ceo|sen|gov|mrs|mr|ms|dr)\.?\s+"
    r"([A-Za-z]+)(?:\s+([A-Za-z]+))?",
    re.IGNORECASE,
)
_NAME_STOP = frozenset(
    {
        "a",
        "an",
        "about",
        "accused",
        "after",
        "and",
        "announced",
        "announces",
        "are",
        "arrested",
        "before",
        "claimed",
        "claims",
        "coast",
        "committee",
        "declared",
        "declares",
        "escaped",
        "found",
        "fraud",
        "from",
        "hits",
        "hour",
        "is",
        "minutes",
        "of",
        "opened",
        "opens",
        "parade",
        "publishes",
        "quarterly",
        "reported",
        "reports",
        "said",
        "says",
        "shoe",
        "stole",
        "the",
        "unveils",
        "warned",
        "warns",
        "was",
        "were",
        "with",
        "won",
        "wins",
        "banner",
        "brand",
        "cartoon",
        "character",
        "coin",
        "collectible",
        "colors",
        "concept",
        "created",
        "daily",
        "disclose",
        "draft",
        "flat",
        "game",
        "held",
        "hold",
        "holding",
        "invented",
        "launch",
        "letters",
        "logo",
        "meme",
        "must",
        "news",
        "none",
        "original",
        "parody",
        "person",
        "posting",
        "secret",
        "shadow",
        "state",
        "token",
        "short",
        "level",
        "one",
        "through",
        "follows",
        "browser",
        "like",
        "treats",
        "picks",
        "guards",
        "nothing",
        "needs",
        "only",
        "answers",
        "for",
        "to",
        "as",
        "will",
        "gain",
        "anything",
        "claim",
        "holder",
        "this",
        "that",
        "not",
        "no",
        "any",
        "we",
        "in",
        "on",
        "at",
        "by",
        "or",
        "be",
        "it",
        "its",
        "our",
        "has",
        "been",
        "minor",
        "delay",
        "city",
        "council",
        "scheduled",
        "election",
        "near",
    }
)
_MIN_NAME_CHARS = 2
_NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_ADULT_AGE = 18


@dataclass(frozen=True)
class SafetyResult:
    """0/1 gate plus stable reason tokens."""

    gate: int
    reasons: tuple[str, ...]


def evaluate_safety(text: str) -> SafetyResult:
    """Score one story text as safe or rejected.

    Args:
        text: Headline plus summary. Empty text fails closed.

    Returns:
        Gate 1 only when no exclusion fired. Reasons are stable tokens.
    """
    if not text or not text.strip():
        return SafetyResult(gate=0, reasons=("empty_text",))
    reasons: list[str] = []
    reasons.extend(_tragedy_reasons(text))
    if contains_hate(text):
        reasons.append("hate_content")
    persons = find_persons(text)
    brands = extract(text).brands
    if persons:
        reasons.append("real_person")
    if _DEFAMATION.search(text) and (persons or brands):
        reasons.append("defamation_risk")
    hook = extract(text)
    if brands and not hook.animals and not hook.objects:
        reasons.append("hook_requires_brand")
    if reasons:
        return SafetyResult(gate=0, reasons=tuple(reasons))
    return SafetyResult(gate=1, reasons=())


def _is_ticker(word: str) -> bool:
    """Return whether a token is an all-caps ticker rather than a name."""
    return len(word) >= _MIN_NAME_CHARS and word.isupper()


def contains_hate(text: str) -> bool:
    """Return whether text contains an unambiguous hate marker.

    Args:
        text: Story, ticker, or any generated field.

    Returns:
        True for markers such as Nazi or a swastika. Ordinary words do not match.
    """
    return _HATE.search(text) is not None


def find_persons(text: str) -> tuple[str, ...]:
    """Return heuristic person mentions.

    Honorifics count, and so does any capitalized pair that is not an animal,
    object, brand, or headline stop word. A defamation verb is not required:
    naming an artist or other person is enough. This is not a trained NER model.

    Args:
        text: Any generated or source string.

    Returns:
        Display strings for the detected mentions. May be empty.
    """
    found: list[str] = []
    for match in _HONORIFIC.finditer(text):
        first = match.group(1)
        second = match.group(2)
        if first.casefold() in _NAME_STOP or first.casefold() in ANIMALS:
            continue
        if second and second.casefold() not in _NAME_STOP:
            found.append(f"{first} {second}")
        else:
            found.append(first)
    blocked = _NAME_STOP | set(ANIMALS) | set(OBJECTS)
    words = [word for word in _NAME_WORD.findall(text) if not _is_ticker(word)]
    for index in range(len(words) - 1):
        first, second = words[index], words[index + 1]
        if first.casefold() in blocked or second.casefold() in blocked:
            continue
        if len(first) < _MIN_NAME_CHARS or len(second) < _MIN_NAME_CHARS:
            continue
        found.append(f"{first} {second}")
    unique: list[str] = []
    for name in found:
        if name not in unique:
            unique.append(name)
    return tuple(unique)


def _tragedy_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    if _DEATH.search(text):
        reasons.append("tragedy_death")
    if _INJURY.search(text):
        reasons.append("tragedy_injury")
    if _DISASTER.search(text):
        reasons.append("tragedy_disaster")
    if _SHOOTING.search(text):
        reasons.append("tragedy_shooting")
    if _TERROR.search(text):
        reasons.append("tragedy_terrorism")
    if _minor(text):
        reasons.append("tragedy_minors")
    if _ILLNESS.search(text):
        reasons.append("tragedy_illness")
    if _ABUSE.search(text):
        reasons.append("tragedy_abuse")
    folded = text.casefold()
    if any(phrase in folded for phrase in _ELECTION_PHRASES):
        reasons.append("elections_disinfo")
    return reasons


def _minor(text: str) -> bool:
    if _MINOR.search(text):
        return True
    for match in _AGE.finditer(text):
        age = int(match.group(1))
        if age < _ADULT_AGE:
            return True
    return False
