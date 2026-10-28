"""Marketing copy for the launch-day schedule.

Templates follow MARKETING.md and add the bot tag, the zero/advice lines, and
the AI-generated label where the compliance checker requires them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meme_studio.drafts.models import ConceptPackage, SellEvent


def render_template(
    template_id: str,
    concept: ConceptPackage,
    variant: str = "",
) -> str:
    """Return the draft text for one template id."""
    renderer = _RENDERERS[template_id]
    return renderer(concept, variant)


def render_sell(concept: ConceptPackage, sell: SellEvent) -> str:
    """Return the P2 dev-sell log line."""
    return (
        f"Dev sell #{sell.n} per plan step {sell.step}: {sell.tokens} "
        f"${concept.ticker} for {sell.sol} SOL at {sell.multiple} times entry. "
        f"Remaining dev bag {sell.pct}%. tx: {sell.sig} (automated post)"
    )


def _m0(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"🎮 New parody game in 5 min. Theme: {concept.story_one_liner}. "
        f"Automated post by {concept.studio} (bot). Not financial advice."
    )


def _game_page(concept: ConceptPackage, variant: str) -> str:
    if variant == "pending":
        contract = "CA: pending"
        links = (
            "The contract address is filled in when the create transaction confirms."
        )
    else:
        contract = f"CA: {concept.mint}"
        links = (
            f"Pump: https://pump.fun/coin/{concept.mint}\n"
            f"Plan: {concept.plan_url}\n"
            f"Plan hash: {concept.plan_hash}"
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


def _p1(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"${concept.ticker}: creator disclosure + sell plan (SHA-256 {concept.plan_hash})\n"
        f"• We created this coin. One creator wallet: {concept.wallet}. "
        f"Dev buy {concept.dev_buy_sol} SOL in the create tx.\n"
        f"• We earn pump.fun creator fees on trading volume.\n"
        "• Plan: at 2.5 times our entry we sell enough to recover cost; then 25% of "
        "what's left at 5 times, 8 times, and 12 times; then trailing 25% steps. "
        "If 2.5 times isn't hit in 24 h we sell the rest in 4 steps, "
        "each announced 1 h ahead.\n"
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
    return (
        "Studio rules: one wallet, no bundles, no paid shills without #ad, "
        "every sell posted. (automated post)"
    )


def _m4(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"Post your {concept.game_title} score in the group. "
        f"Top score at {concept.time_label} PT gets a shout-out. "
        f"(No prizes, no tokens. Just glory.) (automated post)"
    )


def _m5(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{concept.joke_line} — play free at {concept.hub_domain} "
        f"(automated post) AI-generated"
    )


def _m7(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{concept.plays} plays, top score {concept.score} by {concept.handle}. "
        f"Game: (reply). (automated post)"
    )


def _m8(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"High-score shout-out for {concept.game_title}: {concept.handle} "
        f"leads after {concept.plays} plays. Glory only. (automated post)"
    )


def _m9(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"${concept.ticker} completed the pump.fun bonding curve. "
        f"Facts: {concept.holders} holders, {concept.buyers} unique buyers. "
        f"Plan continues unchanged. (automated post)"
    )


def _m10(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"24h of {concept.game_title}: {concept.plays} plays · {concept.buyers} buyers · "
        f"creator fees {concept.fees_sol} SOL · dev sells {concept.sell_count} "
        f"(all per plan) · dev bag left {concept.dev_bag_pct}%. "
        f"Tomorrow: a new game. (automated post)"
    )


def _m11(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"{concept.game_title} is still waddling around the archive today. "
        f"(automated post) AI-generated"
    )


def _site_recap(concept: ConceptPackage, _variant: str) -> str:
    return (
        f"24h archive of {concept.game_title}. Plays {concept.plays}. "
        f"Buyers {concept.buyers}. Creator fees {concept.fees_sol} SOL. "
        f"Dev sells {concept.sell_count}, all per the published sell plan. "
        f"Dev bag left {concept.dev_bag_pct}%. "
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
