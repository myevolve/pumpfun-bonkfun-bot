"""Static HTML pages for one parody game. No build step and no CDN."""
# ruff: noqa: TC001

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from urllib.parse import quote

from meme_studio.game.concept import Concept

DISCLOSURE = (
    "Parody and entertainment only. Not investment advice. "
    "This is an automated account."
)
FULL_COIN_DISCLOSURE = (
    "We created this coin, hold a dev bag, and earn creator fees. "
    "Collectible meme only; it can go to zero."
)


def share_suffix(*, game_only: bool) -> str:
    """Return the disclosure tail for a score draft.

    Args:
        game_only: Omit creator-holding wording when the page must not mention a coin.

    Returns:
        Text appended to the score sentence.
    """
    if game_only:
        return DISCLOSURE
    return f"{FULL_COIN_DISCLOSURE} {DISCLOSURE}"


def share_draft(score: int, title: str, *, game_only: bool) -> str:
    """Build one score-share draft. The viewer posts it; this function does not.

    Args:
        score: Points to advertise.
        title: Game title.
        game_only: Omit the holding disclosure.

    Returns:
        The draft text.
    """
    return f"I scored {score} in {title}. {share_suffix(game_only=game_only)}"


_MECHANICS = Path(__file__).with_name("mechanics.js")
_LABELS = {
    "endless_runner": "Endless runner",
    "tap_clicker": "Tap clicker",
    "dodge_catch": "Dodge and catch",
}


def render_game_page(
    concept: Concept,
    *,
    game_only: bool,
    sprite_href: str | None,
    background_href: str | None,
) -> str:
    """Render a self-contained mobile game page.

    Args:
        concept: Validated concept.
        game_only: Hide ticker, contract, and coin wording for clip capture.
        sprite_href: Relative sprite image, or None for canvas-only art.
        background_href: Relative background image, or None.

    Returns:
        HTML document.
    """
    spec = {
        "template_id": concept.template_id,
        "title": concept.title,
        "seed": _seed(concept.character.name),
        "sprite": sprite_href,
        "background": background_href,
        "palette": {
            "background": concept.palette.background,
            "primary": concept.palette.primary,
            "secondary": concept.palette.secondary,
            "accent": concept.palette.accent,
            "ink": concept.palette.ink,
        },
        "share_suffix": share_suffix(game_only=game_only),
    }
    spec_json = json.dumps(spec).replace("<", "\\u003c")
    share = "https://x.com/intent/tweet?text=" + quote(
        share_draft(0, concept.title, game_only=game_only)
    )
    coin = "" if game_only else f'<p id="coin-disclosure">{FULL_COIN_DISCLOSURE}</p>'
    ticker = "" if game_only else f'<p id="ticker">{html.escape(concept.ticker)}</p>'
    contract = ""
    if not game_only and concept.contract_address:
        contract = (
            f'<p id="contract-address">{html.escape(concept.contract_address)}</p>'
        )
    description = ""
    if not game_only:
        description = f'<p id="description">{html.escape(concept.description)}</p>'
    mechanics = _MECHANICS.read_text(encoding="utf-8")
    label = _LABELS[concept.template_id]
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(concept.title)}</title>
<style>
body {{ margin: 0; background: {concept.palette.background}; color: {concept.palette.ink};
  font-family: system-ui, sans-serif; font-size: 1rem; }}
main {{ max-width: 480px; margin: 0 auto; padding: 12px; }}
button {{ min-height: 44px; font-size: 1rem; margin-right: 8px; }}
canvas {{ width: 100%; max-width: 480px; height: auto; background: {concept.palette.background};
  touch-action: manipulation; }}
#disclosure {{ padding: 12px 0; }}
a {{ color: {concept.palette.primary}; }}
</style>
</head>
<body>
<!-- phase-0-shadow: draft, not posted, not deployed -->
<main data-template="{concept.template_id}">
<h1>{html.escape(concept.title)}</h1>
<p id="character">{html.escape(concept.character.name)}</p>
<p id="joke">{html.escape(concept.joke)}</p>
<p id="template-name">{html.escape(label)}</p>
{description}
{ticker}
{contract}
<button id="play" type="button">Play</button>
<button id="jump" type="button">Jump</button>
<button id="left" type="button">Left</button>
<button id="right" type="button">Right</button>
<canvas id="game" width="360" height="640"></canvas>
<p id="score">Score: 0</p>
<a id="share-score" href="{html.escape(share)}">Share score</a>
{coin}
<footer id="disclosure">{DISCLOSURE}</footer>
</main>
<script type="application/json" id="game-spec">{spec_json}</script>
<script>
{mechanics}
</script>
<script>
{_BOOT}
</script>
</body>
</html>
"""


def _seed(name: str) -> int:
    digest = hashlib.sha256(name.encode()).digest()
    value = int.from_bytes(digest[:4], "big")
    return value or 1


_BOOT = """
(function () {
  const spec = JSON.parse(document.getElementById("game-spec").textContent);
  const canvas = document.getElementById("game");
  const ctx = canvas.getContext("2d");
  const scoreEl = document.getElementById("score");
  const share = document.getElementById("share-score");
  const api = globalThis.MemeMechanics;
  const input = { jump: false, tap: false, left: false, right: false };
  const images = { sprite: null, background: null };
  if (spec.sprite) {
    const sprite = new Image();
    sprite.src = spec.sprite;
    images.sprite = sprite;
  }
  if (spec.background) {
    const background = new Image();
    background.src = spec.background;
    images.background = background;
  }
  let state = api.createState(spec);
  let running = false;
  let last = 0;
  let frameId = 0;
  function frame(ts) {
    if (!running) return;
    const dt = Math.min(0.05, last ? (ts - last) / 1000 : 0.016);
    last = ts;
    state = api.step(state, input, dt);
    input.jump = false;
    input.tap = false;
    api.draw(ctx, state, spec, images);
    const shown = Math.floor(state.score);
    scoreEl.textContent = "Score: " + shown;
    share.href = "https://x.com/intent/tweet?text=" + encodeURIComponent(
      api.shareText(shown, spec.title, spec.share_suffix)
    );
    if (state.alive) frameId = requestAnimationFrame(frame);
    else running = false;
  }
  function startGame() {
    if (frameId) cancelAnimationFrame(frameId);
    state = api.createState(spec);
    running = true;
    last = 0;
    frameId = requestAnimationFrame(frame);
  }
  document.getElementById("play").addEventListener("click", startGame);
  function hold(id, key) {
    const el = document.getElementById(id);
    el.addEventListener("pointerdown", function (event) {
      input[key] = true;
      if (key === "jump") input.tap = true;
      event.preventDefault();
    });
    function release() { input[key] = false; }
    el.addEventListener("pointerup", release);
    el.addEventListener("pointerleave", release);
  }
  hold("jump", "jump");
  hold("left", "left");
  hold("right", "right");
  canvas.addEventListener("pointerdown", function () {
    input.jump = true;
    input.tap = true;
  });
  window.addEventListener("keydown", function (event) {
    if (event.key === " ") {
      event.preventDefault();
      input.jump = true;
      input.tap = true;
    } else if (event.key === "ArrowUp") {
      input.jump = true;
      input.tap = true;
    }
    if (event.key === "ArrowLeft") input.left = true;
    if (event.key === "ArrowRight") input.right = true;
  });
  window.addEventListener("keyup", function (event) {
    if (event.key === "ArrowLeft") input.left = false;
    if (event.key === "ArrowRight") input.right = false;
  });
  api.draw(ctx, state, spec, images);
})();
"""
