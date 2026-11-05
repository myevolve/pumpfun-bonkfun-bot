# ruff: noqa: S101
"""Common lowercase phrases are not people; named people still are."""

from meme_studio.concept.safety import evaluate_safety


def test_local_bakery_is_not_a_person_but_named_people_are() -> None:
    bakery = evaluate_safety("A raccoon stole a donut from a local bakery")
    assert bakery.gate == 1
    assert bakery.reasons == ()
    for text in ("Ada Lovelace was accused of fraud", "taylor swift"):
        result = evaluate_safety(text)
        assert result.gate == 0, text
        assert "real_person" in result.reasons
