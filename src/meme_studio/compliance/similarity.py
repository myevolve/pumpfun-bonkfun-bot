"""Bag-of-words cosine similarity for the X near-duplicate rule."""

from __future__ import annotations

import math
import re
from collections import Counter

DUPLICATE_THRESHOLD = 0.9
HISTORY_LIMIT = 50


def cosine_similarity(left: str, right: str) -> float:
    """Return the cosine similarity of two texts' word counts."""
    left_counts = Counter(re.findall(r"[a-z0-9]+", left.lower()))
    right_counts = Counter(re.findall(r"[a-z0-9]+", right.lower()))
    if not left_counts or not right_counts:
        return 0.0
    dot = sum(
        left_counts[token] * right_counts[token]
        for token in left_counts
        if token in right_counts
    )
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def duplicate_reasons(text: str, history: tuple[str, ...]) -> list[str]:
    """Fail when text is too close to one of the last 50 posts."""
    for prior in history[-HISTORY_LIMIT:]:
        if cosine_similarity(text, prior) > DUPLICATE_THRESHOLD:
            return ["duplicate"]
    return []
