# ruff: noqa: S101
"""Compliance linter: disclosure, bans, channels, duplicates, wallets."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from meme_studio.compliance import (
    Asset,
    LintContext,
    Post,
    StoryCard,
    compliance_lint,
)
from tests.meme_studio.conftest import CREATOR, HIDDEN, MINT

_HASH_HEX_LEN = 64


def _launch_text() -> str:
    return (
        "Bench birds wobble. Collectible meme coin $BENCH launched by us. "
        "We hold a disclosed dev bag + earn creator fees. Sell plan pinned. "
        "(automated post)"
    )


def test_clean_launch_post_passes_and_hashes_payload() -> None:
    post = Post(text=_launch_text(), channel="x", template_id="M1")
    first = compliance_lint(post)
    second = compliance_lint(post)
    assert first.passed is True
    assert first.status == "pass"
    assert first.reasons == ()
    assert first.human_approval_required is False
    assert first.payload_hash == second.payload_hash
    assert len(first.payload_hash) == _HASH_HEX_LEN


def test_lint_logs_status_and_hash(caplog: object) -> None:
    caplog.set_level(logging.INFO)  # type: ignore[attr-defined]
    post = Post(text=_launch_text(), channel="x", template_id="M1")
    result = compliance_lint(post)
    text = caplog.text  # type: ignore[attr-defined]
    assert result.payload_hash in text
    assert "pass" in text


def test_missing_creator_disclosure_fails() -> None:
    post = Post(
        text="New game today. (automated post)",
        channel="x",
        template_id="M1",
    )
    result = compliance_lint(post)
    assert result.passed is False
    assert result.status == "fail"
    assert any(reason.startswith("missing_disclosure:") for reason in result.reasons)


def test_banned_promises_and_leetspeak_fail() -> None:
    samples = (
        "this is guaranteed",
        "gu4r4nteed gains",
        "to the moon",
        "easy 100x",
        "a 1000x story",
        "don't miss the drop",
        "get in early",
        "price target is high",
        "financial freedom",
        "passive income",
        "strong returns",
        "profit for holders",
        "solid investment",
        "the roadmap",
        "real utility",
        "staking rewards",
        "a buyback",
        "next bitcoin",
        "can't lose",
        "m00n soon",
    )
    for sample in samples:
        result = compliance_lint(Post(text=sample, channel="x"))
        assert result.passed is False, sample
        assert any(reason.startswith("banned_phrase:") for reason in result.reasons)


def test_allowed_negations_and_honeymoon_pass() -> None:
    samples = (
        "This is not investment advice and not guaranteed.",
        "The honeymoon of the pigeons was a cartoon.",
        "We return nothing because there is no promise.",
    )
    for sample in samples:
        result = compliance_lint(Post(text=sample, channel="telegram"))
        assert result.passed is True, (sample, result.reasons)


def test_bot_label_required_on_promo_template() -> None:
    post = Post(
        text=(
            "Collectible meme coin $BENCH launched by us. "
            "We hold a disclosed dev bag + earn creator fees. Sell plan pinned."
        ),
        channel="x",
        template_id="M1",
    )
    result = compliance_lint(post)
    assert "missing_bot_label" in result.reasons


def test_tiktok_shorts_and_reddit_reject_coin_ticker_and_address() -> None:
    for channel in ("tiktok", "shorts", "reddit"):
        post = Post(
            text=f"Buy the coin ${'BENCH'} at {MINT}",
            channel=channel,
            ticker="BENCH",
            mint=MINT,
        )
        result = compliance_lint(post)
        assert result.passed is False
        assert "channel_game_only:coin" in result.reasons
        assert "channel_game_only:dollar" in result.reasons
        assert "channel_game_only:ticker" in result.reasons
        assert "channel_game_only:address" in result.reasons


def test_tiktok_game_only_caption_passes() -> None:
    post = Post(
        text="The pigeons elected a bench. Play free.",
        channel="tiktok",
    )
    assert compliance_lint(post).passed is True


def test_trend_and_keyword_replies_require_human_approval() -> None:
    auto = Post(
        text="A joke about the story. (automated post)",
        channel="x",
        trend_ref=True,
        automated=True,
    )
    refused = compliance_lint(auto)
    assert refused.passed is False
    assert refused.human_approval_required is True
    assert "human_approval_required:trend_ref" in refused.reasons

    tapped = Post(
        text="A joke about the story. (automated post)",
        channel="x",
        trend_ref=True,
        human_tap=True,
    )
    allowed = compliance_lint(tapped)
    assert allowed.passed is True
    assert allowed.human_approval_required is True

    search = Post(
        text="Replying to a search hit. (automated post)",
        channel="x",
        keyword_search_reply=True,
        action="reply",
        opt_in=True,
    )
    flagged = compliance_lint(search)
    assert flagged.passed is False
    assert flagged.human_approval_required is True
    assert "human_approval_required:keyword_search_reply" in flagged.reasons


def test_x_urls_belong_in_replies() -> None:
    main = Post(
        text="Play https://games.example/bench (automated post)",
        channel="x",
    )
    reply = Post(
        text=(
            "Play https://games.example/bench We created this coin. We hold a dev bag."
        ),
        channel="x",
        is_reply=True,
        template_id="M2",
    )
    assert "x_link_placement" in compliance_lint(main).reasons
    assert compliance_lint(reply).passed is True


def test_paid_posts_need_ad_hash_not_spon() -> None:
    """Paid and fake engagement fail even when the post says #ad."""
    samples = (
        "collab spon",
        "Cartoon pigeon. #ad",
        "Cartoon pigeon. paid partnership with ParodyDesk",
        "Buy followers and fake replies #ad",
    )
    for sample in samples:
        result = compliance_lint(Post(text=sample, channel="x", paid=True))
        assert result.passed is False, sample
        assert "paid_engagement" in result.reasons
    organic = compliance_lint(
        Post(text="Cartoon pigeon. (automated post)", channel="telegram")
    )
    assert organic.passed is True


def test_person_brand_tragedy_and_official_names_fail() -> None:
    person = compliance_lint(
        Asset(text="cartoon", name="Elon Musk coin", ticker="ELON")
    )
    assert "person_blocked" in person.reasons
    brand = compliance_lint(Asset(text="hat", name="Nike Birds", ticker="BIRD"))
    assert "brand_blocked" in brand.reasons
    official = compliance_lint(Asset(text="bird", name="Official Bench", ticker="B"))
    assert "official_name" in official.reasons
    story = compliance_lint(
        StoryCard(text="Earthquake death toll rises after the blast")
    )
    assert story.passed is False
    assert "tragedy" in story.reasons
    calm = compliance_lint(StoryCard(text="Park pigeons started a tiny union"))
    assert calm.passed is True


def test_artist_and_franchise_prompts_fail() -> None:
    styled = compliance_lint(
        Asset(text="in the style of Greg Rutkowski, a pigeon", kind="art_prompt")
    )
    assert "artist_or_franchise" in styled.reasons
    franchise = compliance_lint(Asset(text="a pokemon pigeon", kind="art_prompt"))
    assert "artist_or_franchise" in franchise.reasons


def test_duplicate_posts_use_last_50_only() -> None:
    text = "identical phrase alpha beta gamma"
    duplicate = compliance_lint(
        Post(text=text, channel="x"),
        LintContext(history=(text,)),
    )
    assert "duplicate" in duplicate.reasons
    fillers = tuple(f"unique filler number {i} zebra" for i in range(50))
    aged = compliance_lint(
        Post(text=text, channel="x"),
        LintContext(history=(text, *fillers)),
    )
    assert aged.passed is True


def test_telegram_rate_governor() -> None:
    now = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    burst = compliance_lint(
        Post(text="hello friends", channel="telegram"),
        LintContext(
            now=now,
            recent_timestamps=(now - timedelta(milliseconds=200),),
        ),
    )
    assert "rate_limit:telegram_per_second" in burst.reasons
    priors = tuple(now - timedelta(seconds=50 - index) for index in range(20))
    minute = compliance_lint(
        Post(text="hello friends", channel="telegram"),
        LintContext(now=now, recent_timestamps=priors),
    )
    assert "rate_limit:telegram_per_minute" in minute.reasons
    quiet = compliance_lint(
        Post(text="hello friends", channel="telegram"),
        LintContext(now=now, recent_timestamps=(now - timedelta(seconds=5),)),
    )
    assert quiet.passed is True


def test_hidden_wallet_in_post_fails() -> None:
    leaked = compliance_lint(
        Post(
            text=f"Also send fees to {HIDDEN}",
            channel="x",
            registered_wallets=(CREATOR, MINT),
        )
    )
    assert "hidden_wallet" in leaked.reasons
    disclosed = compliance_lint(
        Post(
            text=f"One creator wallet: {CREATOR}",
            channel="x",
            registered_wallets=(CREATOR, MINT),
        )
    )
    assert disclosed.passed is True


def test_token_prizes_fail_and_shoutouts_pass() -> None:
    prize = compliance_lint(
        Post(text="Airdrop giveaway: win tokens", channel="telegram")
    )
    assert "giveaway" in prize.reasons
    shout = compliance_lint(
        Post(
            text="Top score gets a shout-out. (No prizes, no tokens. Just glory.)",
            channel="telegram",
        )
    )
    assert shout.passed is True


def test_replies_need_opt_in_and_one_per_interaction() -> None:
    cold = compliance_lint(
        Post(text="hello", channel="x", action="reply", is_reply=True)
    )
    assert "opt_in_required" in cold.reasons
    second = compliance_lint(
        Post(
            text="hello again",
            channel="x",
            action="reply",
            is_reply=True,
            opt_in=True,
            prior_replies_to_user=1,
        )
    )
    assert "duplicate_reply" in second.reasons
    liked = compliance_lint(Post(text="hi", channel="x", action="like"))
    assert "action_forbidden:like" in liked.reasons


def test_defamation_pattern_fails() -> None:
    result = compliance_lint(
        Post(text="The mayor is guilty of the thing.", channel="x")
    )
    assert "defamation" in result.reasons


def test_plan_bio_and_game_page_need_zero_and_advice_lines() -> None:
    thin = compliance_lint(
        Post(
            text=(
                "We created this coin. Dev buy 0.1 SOL. "
                "We earn creator fees. Sell plan pinned. (automated post)"
            ),
            channel="x",
            template_id="P1",
        )
    )
    assert "missing_disclosure:zero" in thin.reasons
    full = (
        "We created this coin. We hold a dev bag and earn creator fees. "
        "Sell plan pinned. It can go to zero. Not financial advice. "
        "Automated post by the studio (bot)."
    )
    for template_id, channel in (
        ("P1", "x"),
        ("bio", "x"),
        ("game_page", "site"),
    ):
        result = compliance_lint(
            Post(text=full, channel=channel, template_id=template_id)
        )
        assert result.passed is True, (template_id, result.reasons)


def test_m0_teaser_rejects_ticker() -> None:
    clean = compliance_lint(
        Post(
            text=(
                "New parody game in 5 min. Theme: pigeons. "
                "Automated post by ParodyDesk (bot). Not financial advice. "
                "We created this coin. We hold a dev bag."
            ),
            channel="telegram",
            template_id="M0",
            ticker="BENCH",
        )
    )
    assert clean.passed is True
    leaked = compliance_lint(
        Post(
            text=(
                "New parody game $BENCH. Automated post by ParodyDesk (bot). "
                "Not financial advice. We created this coin. We hold a dev bag."
            ),
            channel="telegram",
            template_id="M0",
            ticker="BENCH",
        )
    )
    assert "teaser_leaks_ticker" in leaked.reasons
