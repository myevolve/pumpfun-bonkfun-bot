"""Marketing copy for the launch-day schedule.

Templates follow MARKETING.md and add the bot tag, the zero/advice lines, and
the AI-generated label where the compliance checker requires them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING

from meme_studio.compliance.text_rules import ensure_creator_holding_disclosure

if TYPE_CHECKING:
    from meme_studio.drafts.models import ConceptPackage, SellEvent


def render_template(
    template_id: str,
    concept: ConceptPackage,
    variant: str = "",
    *,
    as_of: datetime | None = None,
) -> str:
    """Return the draft text for one template id."""
    renderer = _RENDERERS[template_id]
    viewed = _AsOf(concept, as_of)
    return ensure_creator_holding_disclosure(renderer(viewed, variant))


class _AsOf:
    """Concept plus the recap cutoff used to select sell events."""

    def __init__(self, concept: ConceptPackage, as_of: datetime | None) -> None:
        self._concept = concept
        self.as_of = as_of

    def __getattr__(self, name: str) -> object:
        return getattr(self._concept, name)


def render_sell(concept: ConceptPackage, sell: SellEvent) -> str:
    """Return the P2 dev-sell log line."""
    text = (
        f"Dev sell #{sell.n} per plan step {sell.step}: {sell.tokens} "
        f"${concept.ticker} for {sell.sol} SOL at {sell.multiple} times entry. "
        f"Remaining dev bag {sell.pct}%. tx: {sell.sig} (automated post)"
    )
    return ensure_creator_holding_disclosure(text)


def _known(value: str | None) -> str:
    if value is None or value == "":
        return "UNKNOWN"
    return value


def canonical_plan(concept: ConceptPackage) -> tuple[str | None, str | None]:
    """Return stripped plan text and its SHA-256, or nothing when no plan exists.

    Whitespace-only text is not a plan. The caller-supplied ``plan_hash`` is
    not used here.
    """
    raw = concept.plan_text
    if raw is None or not raw.strip():
        return None, None
    body = raw.strip()
    return body, hashlib.sha256(body.encode("utf-8")).hexdigest()


def _eligible_sells(concept: ConceptPackage) -> tuple[SellEvent, ...]:
    """Sort sells by time and drop any event after the recap cutoff."""
    chosen = list(concept.sells)
    cutoff = getattr(concept, "as_of", None)
    if isinstance(cutoff, datetime):
        chosen = [sell for sell in chosen if sell.at <= cutoff]
    chosen.sort(key=lambda sell: (sell.at, sell.n))
    return tuple(chosen)


def _sell_count(concept: ConceptPackage) -> str:
    if concept.sells:
        return str(len(_eligible_sells(concept)))
    return _known(concept.sell_count)


def _bag_left(concept: ConceptPackage) -> str:
    eligible = _eligible_sells(concept)
    if eligible:
        return eligible[-1].pct
    if concept.sells:
        return "UNKNOWN"
    return _known(concept.dev_bag_pct)


def _plan_claim(concept: ConceptPackage) -> str:
    _body, digest = canonical_plan(concept)
    if digest is None:
        return "plan adherence UNKNOWN"
    return f"all per plan SHA-256 {digest}"


def _plan_hash_line(concept: ConceptPackage) -> str:
    _body, digest = canonical_plan(concept)
    if digest is None:
        return "Plan hash: UNKNOWN"
    return f"Plan hash: {digest}"


def _m0(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"🎮 New parody game in 5 min. Theme: {concept.story_one_liner}. "
        f"Automated post by {concept.studio} (bot). Not financial advice."
    )


def _game_page(concept: ConceptPackage, variant: str) -> str:
    if variant == "pending":
        contract = "CA: pending"
        links = (
            "The contract address is filled in when the create transaction confirms.\n"
            f"{_plan_hash_line(concept)}"
        )
    else:
        contract = f"CA: {concept.mint}"
        links = (
            f"Pump: https://pump.fun/coin/{concept.mint}\n"
            f"Plan: {concept.plan_url}\n"
            f"{_plan_hash_line(concept)}"
        )
    return (
        f"# {concept.game_title}\n\n"
        f"Play this parody game.\n\n"
        f"{contract}\n"
        f"{links}\n\n"
        f"Disclosure: we created this coin. We hold a dev bag and earn creator fees. "
        f"The sell plan is published. Collectible meme coin, it can go to zero. "
        f"Not financial advice. Automated post by {concept.studio} (bot).\n"
    )


def _m1(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{concept.joke_line} 🎮 Today's parody game: {concept.game_title}. "
        f"Collectible meme coin ${concept.ticker} launched by us. "
        f"We hold a disclosed dev bag + earn creator fees. "
        f"Sell plan pinned. Game + CA ⬇️ (automated post) AI-generated"
    )


def _m2(concept: ConceptPackage, _variant: str) -> str:
    return f"Play: {concept.game_url}  CA: {concept.mint}  Plan: {concept.plan_url}"


def _m1_tg(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{concept.joke_line} 🎮 {concept.game_title}. "
        f"Collectible meme coin ${concept.ticker} launched by us. "
        f"We hold a disclosed dev bag + earn creator fees. "
        f"Play: {concept.game_url} CA: {concept.mint} Plan: {concept.plan_url} "
        f"Sell plan pinned. (automated post) Not financial advice."
    )


def _plan_section(concept: ConceptPackage) -> str:
    """Render only the plan text that was supplied, and hash that same text."""
    body, digest = canonical_plan(concept)
    if body is None or digest is None:
        return (
            "Sell plan schedule: UNKNOWN. "
            "No plan text was supplied, so no ladder is stated and no hash is claimed."
        )
    return f"Plan text: {body} SHA-256 {digest}"


def _p1(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"${concept.ticker}: creator disclosure + sell plan\n"
        f"• We created this coin. One creator wallet: {concept.wallet}. "
        f"Dev buy {concept.dev_buy_sol} SOL in the create tx.\n"
        f"• We earn pump.fun creator fees on trading volume.\n"
        f"• {_plan_section(concept)}\n"
        f"• Every sell is posted here with the tx. No other wallets. No promises: "
        f"it's a collectible meme, it can go to zero.\n"
        f"(automated post) Not financial advice."
    )


def _m3_joke(concept: ConceptPackage, _variant: str) -> str:
    return f"{concept.joke_line} 🎮 (automated post) AI-generated"


def _m3_play(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"How to play {concept.game_title}: dodge crumbs and keep score. "
        f"Top score gets a shout-out. No prizes, no tokens. (automated post)"
    )


def _m3(_concept: ConceptPackage, _variant: str) -> str:
    return (
        "How we make money: we created this coin, we hold a dev bag, "
        "and we earn creator fees. Sell plan pinned. (automated post)"
    )


def _m3_rules(_concept: ConceptPackage, _variant: str) -> str:
    return "Studio rules: one wallet, no bundles, every sell posted. (automated post)"


def _m4(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"Post your {concept.game_title} score in the group. "
        f"Top score at {_known(concept.time_label)} PT gets a shout-out. "
        f"(No prizes, no tokens. Just glory.) (automated post)"
    )


def _m5(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"Second still, hours after the drop: {concept.joke_line} "
        f"The free game is still up at {concept.hub_domain}. "
        f"No price talk in this caption. (automated post) AI-generated"
    )


def _m7(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{_known(concept.plays)} plays, top score {_known(concept.score)} "
        f"by {_known(concept.handle)}. "
        f"Game: (reply). (automated post)"
    )


def _m8(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"High-score shout-out for {concept.game_title}: {_known(concept.handle)} "
        f"leads after {_known(concept.plays)} plays. Glory only. (automated post)"
    )


def _m9(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"${concept.ticker} completed the pump.fun bonding curve. "
        f"Facts: {_known(concept.holders)} holders, "
        f"{_known(concept.buyers)} unique buyers. "
        f"Plan continues unchanged. (automated post)"
    )


def _m10(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"24h of {concept.game_title}: {_known(concept.plays)} plays · "
        f"{_known(concept.buyers)} buyers · "
        f"creator fees {_known(concept.fees_sol)} SOL · dev sells {_sell_count(concept)} "
        f"({_plan_claim(concept)}) · dev bag left {_bag_left(concept)}%. "
        f"Tomorrow: a new game. (automated post)"
    )


def _m11(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{concept.game_title} is still waddling around the archive today. "
        f"(automated post) AI-generated"
    )


def _site_recap(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"24h archive of {concept.game_title}. Plays {_known(concept.plays)}. "
        f"Buyers {_known(concept.buyers)}. "
        f"Creator fees {_known(concept.fees_sol)} SOL. "
        f"Dev sells {_sell_count(concept)}, {_plan_claim(concept)}. "
        f"Dev bag left {_bag_left(concept)}%. "
        f"We created this coin and it can go to zero. "
        f"Not financial advice. Automated post (bot).\n"
    )


_RENDERERS = {
    "M0": _m0,
    "game_page": _game_page,
    "M1": _m1,
    "M2": _m2,
    "M1-TG": _m1_tg,
    "P1": _p1,
    "M3-joke": _m3_joke,
    "M3-play": _m3_play,
    "M3": _m3,
    "M3-rules": _m3_rules,
    "M4": _m4,
    "M5": _m5,
    "M7": _m7,
    "M8": _m8,
    "M9": _m9,
    "M10": _m10,
    "M11": _m11,
    "site_recap": _site_recap,
}
