# ruff: noqa: S101
"""Likeness guardrail: negated disclaimers pass, likeness requests still fail."""

from __future__ import annotations

import pytest

from meme_studio.art.guardrails import GuardrailError, assert_safe_text


@pytest.mark.parametrize(
    "text",
    [
        "The character is invented and is not a real person or a brand.",
        "original cartoon raccoon, flat colors, no real person, no logo",
    ],
)
def test_negated_real_person_disclaimer_passes(text: str) -> None:
    assert_safe_text(text, field="description")


@pytest.mark.parametrize(
    "text",
    [
        "Draw a real person holding a donut.",
        "A raccoon that looks like a real person.",
        "Elon Musk as a raccoon. The character is not a real person.",
        "A celebrity raccoon. This is not a real person.",
    ],
)
def test_likeness_requests_still_fail(text: str) -> None:
    with pytest.raises(GuardrailError):
        assert_safe_text(text, field="description")
