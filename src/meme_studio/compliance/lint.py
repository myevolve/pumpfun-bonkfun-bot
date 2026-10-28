"""compliance_lint: pass/fail plus reasons. Never rewrites copy to force a pass."""

from __future__ import annotations

import hashlib
import logging
import re

from meme_studio.compliance.blocklists import (
    brand_blocked,
    franchise_or_artist,
    person_blocked,
    tragedy_hit,
)
from meme_studio.compliance.media_rules import media_reasons
from meme_studio.compliance.models import (
    Asset,
    LintContext,
    LintResult,
    MediaMetadata,
    Post,
    StoryCard,
)
from meme_studio.compliance.phrases import banned_phrase_reasons
from meme_studio.compliance.rates import rate_reasons
from meme_studio.compliance.similarity import duplicate_reasons
from meme_studio.compliance.text_rules import (
    action_reasons,
    ai_label_reasons,
    approval_reasons,
    defamation_reasons,
    disclosure_reasons,
    game_channel_reasons,
    giveaway_reasons,
    link_reasons,
    paid_reasons,
    wallet_reasons,
)

_LOG = logging.getLogger(__name__)
_Item = Post | Asset | StoryCard | MediaMetadata


def compliance_lint(
    item: _Item,
    context: LintContext | None = None,
) -> LintResult:
    """Return pass/fail for a post, asset, story card, or media prompt.

    A failure is reported and dropped. This function does not rewrite text.
    """
    ctx = context or LintContext()
    reasons = tuple(dict.fromkeys(_collect(item, ctx)))
    flagged = _needs_human(item) or any(
        reason.startswith("human_approval_required") for reason in reasons
    )
    result = LintResult(
        passed=not reasons,
        reasons=reasons,
        human_approval_required=flagged,
        payload_hash=_hash(_payload(item)),
    )
    _LOG.info(
        "compliance %s %s %s",
        result.status,
        result.payload_hash,
        ",".join(result.reasons),
    )
    return result


def _collect(item: _Item, context: LintContext) -> list[str]:
    if isinstance(item, Post):
        return _post_reasons(item, context)
    if isinstance(item, MediaMetadata):
        return media_reasons(item)
    if isinstance(item, StoryCard):
        return _story_reasons(item)
    return _asset_reasons(item)


def _post_reasons(post: Post, context: LintContext) -> list[str]:
    reasons: list[str] = []
    reasons.extend(banned_phrase_reasons(post.text))
    reasons.extend(disclosure_reasons(post))
    reasons.extend(game_channel_reasons(post))
    reasons.extend(ai_label_reasons(post))
    reasons.extend(link_reasons(post))
    reasons.extend(paid_reasons(post))
    reasons.extend(approval_reasons(post))
    reasons.extend(wallet_reasons(post))
    reasons.extend(giveaway_reasons(post.text))
    reasons.extend(action_reasons(post))
    reasons.extend(defamation_reasons(post.text))
    reasons.extend(_identity_reasons(post.text, post.ticker, media=False))
    reasons.extend(duplicate_reasons(post.text, context.history))
    reasons.extend(rate_reasons(post.channel, context.now, context.recent_timestamps))
    return reasons


def _story_reasons(card: StoryCard) -> list[str]:
    text = f"{card.title}\n{card.text}"
    return _identity_reasons(text, "", media=False, include_tragedy=True)


def _asset_reasons(asset: Asset) -> list[str]:
    text = "\n".join((asset.name, asset.ticker, asset.text))
    reasons = _identity_reasons(text, asset.ticker, media=False, include_tragedy=True)
    if _OFFICIAL.search(asset.name) or _OFFICIAL.search(asset.ticker):
        reasons.append("official_name")
    if franchise_or_artist(text):
        reasons.append("artist_or_franchise")
    return reasons


def _identity_reasons(
    text: str,
    ticker: str,
    *,
    media: bool,
    include_tragedy: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if person_blocked(text, ticker):
        reasons.append("person_blocked")
    if brand_blocked(text):
        reasons.append("media_brand" if media else "brand_blocked")
    if include_tragedy and tragedy_hit(text):
        reasons.append("tragedy")
    return reasons


_OFFICIAL = re.compile(r"\bofficial\b", re.IGNORECASE)


def _needs_human(item: _Item) -> bool:
    if not isinstance(item, Post):
        return False
    return item.trend_ref or item.keyword_search_reply


def _payload(item: _Item) -> str:
    if isinstance(item, Post):
        return f"post|{item.channel}|{item.template_id}|{item.text}"
    if isinstance(item, MediaMetadata):
        overlays = ",".join(item.overlays)
        return f"media|{item.kind}|{item.channel}|{item.prompt}|{overlays}"
    if isinstance(item, StoryCard):
        return f"story|{item.title}|{item.text}"
    return f"asset|{item.kind}|{item.name}|{item.ticker}|{item.text}"


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
