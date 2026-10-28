# ruff: noqa: S101
"""Collision checks: public token lookup, trademarks, people, history."""

import json

import pytest

from meme_studio.concept.collisions import (
    HttpTokenLookup,
    LookupUnavailableError,
    MemoryTokenLookup,
    TokenHit,
    check_candidate,
    dexscreener_search_url,
    levenshtein,
    parse_dexscreener_search,
)
from meme_studio.concept.schema import (
    SHADOW_DISCLOSURE,
    ConceptCandidate,
    ThemePalette,
)
from meme_studio.trends.cache import TtlCache
from meme_studio.trends.ratelimit import RateLimiter

DESCRIPTION = " ".join(["cartoon"] * 20) + " " + SHADOW_DISCLOSURE


def _candidate(
    *,
    name: str = "Raccoon Donut",
    ticker: str = "RACODONU",
    description: str = DESCRIPTION,
    art_prompt: str = "original cartoon raccoon holding a donut",
) -> ConceptCandidate:
    return ConceptCandidate(
        name=name,
        ticker=ticker,
        character="Raccoon Donut is an original cartoon raccoon",
        joke="A cartoon raccoon treats a donut like a state secret.",
        description=description,
        game_template_id="dodge",
        theme_palette=ThemePalette(
            primary="#1B4D3E",
            secondary="#F4E8C1",
            accent="#E07A5F",
            background="#102A22",
        ),
        art_prompt=art_prompt,
        collision_checks=(),
        accepted=False,
    )


def _review(
    candidate: ConceptCandidate,
    hits: list[TokenHit] | None = None,
    history: set[str] | None = None,
) -> ConceptCandidate:
    lookup = MemoryTokenLookup(hits or [])
    return check_candidate(candidate, lookup=lookup, history=history or set())


@pytest.mark.parametrize(
    ("left", "right", "distance"),
    [
        ("donut", "donut", 0),
        ("donut", "donuts", 1),
        ("raccoon", "racoon", 1),
        ("cat", "caterpillar", 8),
        ("", "abc", 3),
    ],
)
def test_levenshtein_distance(left: str, right: str, distance: int) -> None:
    assert levenshtein(left, right) == distance


def test_clean_candidate_passes_every_check() -> None:
    reviewed = _review(_candidate())
    assert [check.name for check in reviewed.collision_checks] == [
        "existing_tokens",
        "trademark",
        "real_person",
        "own_history",
    ]
    assert all(check.passed for check in reviewed.collision_checks)
    assert reviewed.accepted is True


def test_exact_ticker_with_volume_fails_and_zero_volume_does_not() -> None:
    blocked = _review(
        _candidate(),
        hits=[
            TokenHit("RACODONU", "Something Else", 0.01, 0.0, "solana"),
        ],
    )
    token_check = blocked.collision_checks[0]
    assert token_check.passed is False
    assert token_check.reason == "exact_ticker_volume"
    assert blocked.accepted is False

    allowed = _review(
        _candidate(),
        hits=[TokenHit("RACODONU", "Something Else", 0.0, 0.0, "solana")],
    )
    assert allowed.collision_checks[0].passed is True
    assert allowed.accepted is True


def test_fuzzy_ticker_and_fuzzy_name_thresholds() -> None:
    fuzzy_ticker = _review(
        _candidate(ticker="DONUT"),
        hits=[TokenHit("DONUTS", "Unrelated Name", 1.0, 0.0, "solana")],
    )
    assert fuzzy_ticker.collision_checks[0].reason == "fuzzy_ticker"
    assert fuzzy_ticker.accepted is False

    short = _review(
        _candidate(ticker="CAT"),
        hits=[TokenHit("BAT", "Unrelated Name", 5.0, 0.0, "solana")],
    )
    assert short.collision_checks[0].passed is True

    below = _review(
        _candidate(name="Donut", ticker="ZZZUNIQUE"),
        hits=[TokenHit("OTHER", "Donut", 0.0, 10000.0, "solana")],
    )
    assert below.collision_checks[0].passed is True

    above = _review(
        _candidate(name="Donut", ticker="ZZZUNIQUE"),
        hits=[TokenHit("OTHER", "Donuts", 0.0, 10000.01, "solana")],
    )
    assert above.collision_checks[0].reason == "fuzzy_name"
    assert above.accepted is False

    punct = _review(
        _candidate(name="Donut-Raccoon", ticker="ZZZUNIQUE"),
        hits=[TokenHit("OTHER", "Donut Raccoon", 0.0, 50000.0, "solana")],
    )
    assert punct.collision_checks[0].passed is False
    assert "fuzzy_name" in punct.collision_checks[0].reason


def test_non_solana_hits_are_ignored_and_blank_chain_counts() -> None:
    eth = _review(
        _candidate(),
        hits=[TokenHit("RACODONU", "Raccoon Donut", 9.0, 90000.0, "ethereum")],
    )
    assert eth.collision_checks[0].passed is True
    blank = _review(
        _candidate(),
        hits=[TokenHit("RACODONU", "Elsewhere", 9.0, 0.0, "")],
    )
    assert blank.collision_checks[0].passed is False


def test_trademark_blocklist_matches_ticker_and_name_words() -> None:
    nike = _review(_candidate(name="Nike Goose", ticker="NIKE"))
    assert nike.collision_checks[1].passed is False
    assert nike.collision_checks[1].reason == "blocklist:nike"
    metal = _review(_candidate(name="Metal Goose", ticker="METAL"))
    assert metal.collision_checks[1].passed is True
    assert metal.accepted is True


def test_person_in_art_prompt_or_description_fails() -> None:
    art = _review(_candidate(art_prompt="portrait of President Ada, original cartoon"))
    assert art.collision_checks[2].passed is False
    assert art.collision_checks[2].reason == "person_entity"
    text = _review(
        _candidate(description="Mr. Smith waves at " + " ".join(["cartoon"] * 18))
    )
    assert text.collision_checks[2].passed is False


def test_own_history_blocks_a_reused_ticker() -> None:
    reviewed = _review(_candidate(), history={"racodonu"})
    assert reviewed.collision_checks[3].passed is False
    assert reviewed.collision_checks[3].reason == "ticker_reused"
    assert reviewed.accepted is False


def test_lookup_failure_fails_closed_without_skipping_other_checks() -> None:
    class Down:
        def search(self, _query: str) -> list[TokenHit]:
            raise LookupUnavailableError("down")

    reviewed = check_candidate(
        _candidate(name="Nike Goose", ticker="NIKE"),
        lookup=Down(),
        history={"NIKE"},
    )
    assert reviewed.collision_checks[0].reason == "lookup_unavailable"
    assert reviewed.collision_checks[1].passed is False
    assert reviewed.collision_checks[3].passed is False
    assert reviewed.accepted is False


def test_short_description_cannot_be_accepted() -> None:
    reviewed = _review(_candidate(description="too short"))
    assert all(check.passed for check in reviewed.collision_checks)
    assert reviewed.accepted is False


def test_memory_lookup_returns_the_full_public_list() -> None:
    hit = TokenHit("RACODONU", "Raccoon", 1.0, 1.0, "solana")
    lookup = MemoryTokenLookup([hit])
    assert lookup.search("unrelated") == [hit]


def test_dexscreener_url_and_parser_keep_solana_h24_volume() -> None:
    assert (
        dexscreener_search_url("Donut Raccoon")
        == "https://api.dexscreener.com/latest/dex/search?q=Donut%20Raccoon"
    )
    body = json.dumps(
        {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"name": "Donuts", "symbol": "DONUTS"},
                    "volume": {"h24": 12.5},
                    "marketCap": 20000,
                },
                {
                    "chainId": "ethereum",
                    "baseToken": {"name": "Donut", "symbol": "DONUT"},
                    "volume": {"h24": 99},
                    "marketCap": 999999,
                },
            ]
        }
    ).encode()
    hits = parse_dexscreener_search(body)
    assert len(hits) == 1
    assert hits[0].symbol == "DONUTS"
    assert hits[0].chain == "solana"
    assert hits[0].volume_7d_usd == pytest.approx(12.5)
    assert hits[0].market_cap_usd == pytest.approx(20000)


def test_http_lookup_is_get_only_cached_and_hides_bodies() -> None:
    calls: list[tuple[str, str]] = []
    clock = {"t": 0.0}

    def transport(
        url: str,
        _headers: dict[str, str],
        method: str,
    ) -> tuple[int, bytes]:
        calls.append((url, method))
        if "fail" in url:
            return 200, b"not-json SECRET_BODY_TOKEN"
        body = json.dumps(
            {
                "pairs": [
                    {
                        "chainId": "solana",
                        "baseToken": {"name": "Donut", "symbol": "DONUT"},
                        "volume": {"h24": 1},
                        "marketCap": 1,
                    }
                ]
            }
        ).encode()
        return 200, body

    limiter = RateLimiter(
        max_calls=10,
        per_seconds=60,
        clock=lambda: clock["t"],
        sleep=lambda _seconds: None,
    )
    cache = TtlCache(clock=lambda: clock["t"])
    lookup = HttpTokenLookup(transport=transport, limiter=limiter, cache=cache)
    first = lookup.search("DONUT")
    second = lookup.search("DONUT")
    assert first == second
    assert calls == [
        (
            "https://api.dexscreener.com/latest/dex/search?q=DONUT",
            "GET",
        )
    ]
    with pytest.raises(LookupUnavailableError) as exc:
        lookup.search("fail")
    assert "SECRET_BODY_TOKEN" not in str(exc.value)
