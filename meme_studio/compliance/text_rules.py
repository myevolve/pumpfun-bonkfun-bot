"""Disclosure, channel, wallet, and action rules for post text."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meme_studio.compliance.models import Post

_COIN_TEMPLATES = frozenset(
    {"M1", "M3", "P1", "bio", "game_page", "pump_description", "M1-TG"}
)
_ZERO_TEMPLATES = frozenset({"P1", "bio", "game_page"})
_ADVICE_TEMPLATES = frozenset({"M0", "P1", "bio", "game_page"})
_BOT_TEMPLATES = frozenset(
    {
        "M0",
        "M1",
        "M1-TG",
        "M3",
        "M3-joke",
        "M3-play",
        "M3-rules",
        "M4",
        "M5",
        "M7",
        "M8",
        "M10",
        "M11",
        "P1",
        "P2",
        "bio",
        "game_page",
        "site_recap",
    }
)
_GAME_CHANNELS = frozenset({"tiktok", "shorts", "youtube_shorts", "reddit"})
_AI_CHANNELS = frozenset({"x", "tiktok", "shorts", "youtube_shorts"})
_FORBIDDEN_ACTIONS = {
    "like": "action_forbidden:like",
    "bulk_follow": "action_forbidden:bulk_follow",
    "bulk_unfollow": "action_forbidden:bulk_unfollow",
    "bulk_list_add": "action_forbidden:bulk_list_add",
    "browser_automation": "action_forbidden:browser_automation",
    "ai_reply": "action_forbidden:ai_reply",
}
_BOT_LABEL = re.compile(r"\(automated post\)|automated post|\bbot\b", re.IGNORECASE)
_URL = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_GIVEAWAY = re.compile(
    r"\b(?:airdrops?|giveaways?|cash prize|token prize|win (?:sol|tokens|money)|prize pool)\b",
    re.IGNORECASE,
)
_DEFAMATION = re.compile(
    r"\b(?:is guilty|are guilty|committed (?:the )?crime|the killer is|the suspect is)\b",
    re.IGNORECASE,
)
ADDRESS = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])"
)


def disclosure_reasons(post: Post) -> list[str]:
    """Required creator disclosure, bot label, and teaser secrecy."""
    reasons: list[str] = []
    text = post.text
    template = post.template_id
    if template in _COIN_TEMPLATES:
        reasons.extend(_coin_disclosure(text))
    if template in _ZERO_TEMPLATES and "go to zero" not in text.lower():
        reasons.append("missing_disclosure:zero")
    if template in _ADVICE_TEMPLATES and "not financial advice" not in text.lower():
        reasons.append("missing_disclosure:nfa")
    if template in _BOT_TEMPLATES and not _BOT_LABEL.search(text):
        reasons.append("missing_bot_label")
    if template == "M0":
        reasons.extend(_teaser_leaks(post))
    return reasons


def _coin_disclosure(text: str) -> list[str]:
    reasons: list[str] = []
    if not re.search(
        r"launched by us|we created (?:this|the) coin", text, re.IGNORECASE
    ):
        reasons.append("missing_disclosure:created")
    if not re.search(r"dev bag|dev buy", text, re.IGNORECASE):
        reasons.append("missing_disclosure:holding")
    if not re.search(r"creator fees?", text, re.IGNORECASE):
        reasons.append("missing_disclosure:creator_fee")
    if not re.search(r"sell plan|\bplan\b", text, re.IGNORECASE):
        reasons.append("missing_disclosure:sell_plan")
    return reasons


def _teaser_leaks(post: Post) -> list[str]:
    if post.ticker and re.search(
        rf"\b{re.escape(post.ticker)}\b", post.text, re.IGNORECASE
    ):
        return ["teaser_leaks_ticker"]
    if post.mint and post.mint in post.text:
        return ["teaser_leaks_ticker"]
    return []


def game_channel_reasons(post: Post) -> list[str]:
    """TikTok, Shorts, and Reddit stay game-only: no coin, ticker, or address."""
    if post.channel not in _GAME_CHANNELS:
        return []
    reasons: list[str] = []
    if re.search(r"\bcoins?\b", post.text, re.IGNORECASE):
        reasons.append("channel_game_only:coin")
    if "$" in post.text:
        reasons.append("channel_game_only:dollar")
    if post.ticker and re.search(
        rf"\b{re.escape(post.ticker)}\b", post.text, re.IGNORECASE
    ):
        reasons.append("channel_game_only:ticker")
    if (post.mint and post.mint in post.text) or ADDRESS.search(post.text):
        reasons.append("channel_game_only:address")
    return reasons


def ai_label_reasons(post: Post) -> list[str]:
    """X and short-video posts that use generated media need an AI-generated label."""
    if not post.uses_generated_media or post.channel not in _AI_CHANNELS:
        return []
    if "ai-generated" in post.text.lower():
        return []
    return ["missing_ai_generated_label"]


def link_reasons(post: Post) -> list[str]:
    """X main posts keep URLs out of the body. Replies may carry them."""
    if post.channel != "x" or post.is_reply:
        return []
    if _URL.search(post.text):
        return ["x_link_placement"]
    return []


def paid_reasons(post: Post) -> list[str]:
    """Paid posts need #ad or a paid-partnership phrase in the post itself."""
    if not post.paid:
        return []
    if re.search(r"(?<![A-Za-z0-9_])#ad\b", post.text, re.IGNORECASE):
        return []
    if "paid partnership" in post.text.lower():
        return []
    return ["missing_ad_disclosure"]


def approval_reasons(post: Post) -> list[str]:
    """Automated trend posts and keyword-search replies stay human-gated."""
    needs_human = post.automated and not post.human_tap
    if not needs_human:
        return []
    reasons: list[str] = []
    if post.trend_ref:
        reasons.append("human_approval_required:trend_ref")
    if post.keyword_search_reply:
        reasons.append("human_approval_required:keyword_search_reply")
    return reasons


def wallet_reasons(post: Post) -> list[str]:
    """Flag a base58 address that is not the disclosed creator wallet or mint."""
    if not post.registered_wallets:
        return []
    allowed = set(post.registered_wallets)
    for match in ADDRESS.findall(post.text):
        if match not in allowed:
            return ["hidden_wallet"]
    return []


def giveaway_reasons(text: str) -> list[str]:
    """Token or money prizes fail. A no-prize shout-out does not."""
    if _GIVEAWAY.search(text):
        return ["giveaway"]
    return []


def action_reasons(post: Post) -> list[str]:
    """Block likes, bulk social actions, and replies the user did not opt into."""
    reasons: list[str] = []
    forbidden = _FORBIDDEN_ACTIONS.get(post.action)
    if forbidden:
        reasons.append(forbidden)
    if post.action == "reply" and not post.opt_in:
        reasons.append("opt_in_required")
    if post.action == "reply" and post.prior_replies_to_user >= 1:
        reasons.append("duplicate_reply")
    return reasons


def defamation_reasons(text: str) -> list[str]:
    """Block factual guilt or suspect claims about a real person."""
    if _DEFAMATION.search(text):
        return ["defamation"]
    return []
