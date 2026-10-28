"""Static game pages: three templates, disclosure, and generated sprites when present."""

from __future__ import annotations

import json
from pathlib import Path

from meme_studio.game.concept import load_concept
from meme_studio.game.page import DISCLOSURE, FULL_COIN_DISCLOSURE, render_game_page
from tests.meme_studio.support import (
    MECHANICS,
    NO_INPUT,
    load_fixture_dict,
    step_mechanics,
    write_concept,
)

# ruff: noqa: ANN001, PLC0415, PLR2004, S101, S603, S607, TC003


def _page(
    template_id: str, tmp_path: Path, *, game_only: bool, sprite: str | None
) -> str:
    payload = load_fixture_dict()
    payload["template_id"] = template_id
    concept = load_concept(write_concept(tmp_path, payload, f"{template_id}.json"))
    return render_game_page(
        concept,
        game_only=game_only,
        sprite_href=sprite,
        background_href="assets/background.png" if sprite else None,
    )


def test_page_is_a_self_contained_mobile_game(tmp_path) -> None:
    html = _page("endless_runner", tmp_path, game_only=False, sprite=None)
    assert html.startswith("<!DOCTYPE html>")
    assert 'name="viewport"' in html
    assert 'id="play"' in html
    assert ">Play<" in html
    assert 'id="game"' in html
    assert "<canvas" in html
    assert "requestAnimationFrame" in html
    assert 'id="disclosure"' in html
    assert DISCLOSURE in html
    assert FULL_COIN_DISCLOSURE in html
    assert "touch-action" in html
    assert "min-height: 44px" in html or "min-height:44px" in html
    assert "https://x.com/intent/tweet?" in html
    assert "fetch(" not in html
    assert "sendBeacon" not in html
    assert "phaser" not in html.lower()
    assert "kaboom" not in html.lower()
    assert "cdn" not in html.lower()
    assert len(html.encode()) < 1_000_000
    assert "SOCKDRIFT" in html
    assert "CA_PLACEHOLDER_DO_NOT_USE" in html
    assert "Nimbus" in html
    assert "#1b1f3b" in html
    assert 'data-template="endless_runner"' in html


def test_game_only_strips_ticker_address_and_coin_words(tmp_path) -> None:
    html = _page("tap_clicker", tmp_path, game_only=True, sprite=None)
    assert DISCLOSURE in html
    assert "SOCKDRIFT" not in html
    assert "CA_PLACEHOLDER_DO_NOT_USE" not in html
    assert "$" not in html
    assert "coin" not in html.lower()
    assert FULL_COIN_DISCLOSURE not in html
    assert 'data-template="tap_clicker"' in html


def test_generated_sprites_are_consumed_when_present(tmp_path) -> None:
    html = _page("dodge_catch", tmp_path, game_only=True, sprite="assets/sprite.png")
    spec = _spec(html)
    assert spec["sprite"] == "assets/sprite.png"
    assert spec["background"] == "assets/background.png"
    assert "drawImage" in html
    bare = _page("dodge_catch", tmp_path, game_only=True, sprite=None)
    bare_spec = _spec(bare)
    assert not bare_spec.get("sprite")
    assert "drawCreature" in bare


def test_joke_is_escaped_and_share_is_a_user_link(tmp_path) -> None:
    payload = load_fixture_dict()
    payload["joke"] = "</p><script>alert(1)</script>"
    concept = load_concept(write_concept(tmp_path, payload))
    html = render_game_page(
        concept, game_only=True, sprite_href=None, background_href=None
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert (
        'addEventListener("click", startGame)' in html
        or "addEventListener('click', startGame)" in html
    )
    assert "window.open" not in html


def test_three_templates_expose_different_mechanics(tmp_path) -> None:
    runner = _page("endless_runner", tmp_path, game_only=True, sprite=None)
    clicker = _page("tap_clicker", tmp_path, game_only=True, sprite=None)
    dodge = _page("dodge_catch", tmp_path, game_only=True, sprite=None)
    assert "gravity" in runner
    assert "combo" in clicker
    assert "catch" in dodge
    assert runner != clicker != dodge


def _spec(html: str) -> dict[str, object]:
    marker = '<script type="application/json" id="game-spec">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    spec: dict[str, object] = json.loads(html[start:end])
    return spec


def test_runner_jump_gravity_score_and_collision() -> None:
    state = step_mechanics("create", {"template_id": "endless_runner", "seed": 1})
    assert state["y"] == 0
    jumped = step_mechanics(
        "step",
        {"state": state, "input": {**NO_INPUT, "jump": True}, "dt": 1 / 60},
    )
    assert jumped["y"] > 0
    assert jumped["vy"] > 0
    high = dict(state)
    high["y"] = 100
    high["vy"] = 0
    high["obstacles"] = [{"x": 5000, "w": 20, "h": 20}]
    fallen = step_mechanics("step", {"state": high, "input": NO_INPUT, "dt": 0.1})
    assert fallen["y"] < 100
    running = dict(state)
    running["obstacles"] = [{"x": 5000, "w": 20, "h": 20}]
    moved = step_mechanics("step", {"state": running, "input": NO_INPUT, "dt": 0.5})
    assert moved["alive"] is True
    assert float(moved["score"]) > 0
    crash = dict(state)
    crash["obstacles"] = [{"x": crash["player_x"], "w": 40, "h": 40}]
    crash["y"] = 0
    dead = step_mechanics("step", {"state": crash, "input": NO_INPUT, "dt": 0.016})
    assert dead["alive"] is False
    snapshot = json.dumps(state)
    step_mechanics("step", {"state": state, "input": NO_INPUT, "dt": 0.016})
    assert json.dumps(state) == snapshot


def test_clicker_combo_and_decay() -> None:
    state = step_mechanics("create", {"template_id": "tap_clicker", "seed": 1})
    one = step_mechanics(
        "step", {"state": state, "input": {**NO_INPUT, "tap": True}, "dt": 0.05}
    )
    assert one["score"] == 1
    assert one["combo"] == 1
    two = step_mechanics(
        "step", {"state": one, "input": {**NO_INPUT, "tap": True}, "dt": 0.05}
    )
    assert two["score"] == 3
    assert two["combo"] == 2
    cooled = step_mechanics("step", {"state": two, "input": NO_INPUT, "dt": 1.0})
    assert cooled["combo"] == 0
    again = step_mechanics(
        "step", {"state": cooled, "input": {**NO_INPUT, "tap": True}, "dt": 0.05}
    )
    assert again["score"] == 4


def test_dodge_catches_goods_and_dies_on_bads() -> None:
    state = step_mechanics("create", {"template_id": "dodge_catch", "seed": 1})
    caught_state = dict(state)
    caught_state["items"] = [
        {
            "x": state["player_x"],
            "y": state["player_y"],
            "w": 10,
            "h": 10,
            "kind": "good",
            "vy": 10,
        }
    ]
    caught = step_mechanics(
        "step", {"state": caught_state, "input": NO_INPUT, "dt": 0.016}
    )
    assert caught["score"] >= 1
    bad_state = dict(state)
    bad_state["items"] = [
        {
            "x": state["player_x"],
            "y": state["player_y"],
            "w": 10,
            "h": 10,
            "kind": "bad",
            "vy": 10,
        }
    ]
    died = step_mechanics("step", {"state": bad_state, "input": NO_INPUT, "dt": 0.016})
    assert died["alive"] is False
    moved = step_mechanics(
        "step",
        {"state": state, "input": {**NO_INPUT, "right": True}, "dt": 0.1},
    )
    assert moved["player_x"] > state["player_x"]


def test_full_share_draft_states_we_created_and_hold_the_coin(tmp_path) -> None:
    from html import unescape
    from urllib.parse import unquote

    html = _page("endless_runner", tmp_path, game_only=False, sprite=None)
    href = html.split('id="share-score" href="', 1)[1].split('"', 1)[0]
    draft = unquote(unescape(href.split("text=", 1)[1]))
    assert "We created this coin, hold a dev bag" in draft
    updated = step_mechanics(
        "share",
        {
            "score": 31,
            "title": "Sock Drawer Drift",
            "suffix": (
                "We created this coin, hold a dev bag, and earn creator fees. "
                "Collectible meme only; it can go to zero. "
                "Parody and entertainment only. Not investment advice. "
                "This is an automated account."
            ),
        },
    )
    assert "We created this coin, hold a dev bag" in str(updated["text"])
    assert "31" in str(updated["text"])


def test_dodge_keeps_spawning_after_the_opening_pair() -> None:
    state = step_mechanics("create", {"template_id": "dodge_catch", "seed": 1})
    for _frame in range(600):
        state = step_mechanics(
            "step", {"state": state, "input": NO_INPUT, "dt": 1 / 60}
        )
    assert int(state["spawned"]) > 2
    assert len(state["items"]) >= 1
    assert state["alive"] is True


def test_restart_cancels_the_previous_frame(tmp_path) -> None:
    html = _page("endless_runner", tmp_path, game_only=True, sprite=None)
    assert "cancelAnimationFrame" in html
    assert 'if (event.key === " ")' in html
    assert html.index('if (event.key === " ")') < html.index(
        "event.preventDefault()", html.index('if (event.key === " ")')
    )


def test_mechanics_file_is_syntax_checked() -> None:
    assert MECHANICS.is_file()
    import subprocess

    proc = subprocess.run(
        ["node", "--check", str(MECHANICS)], check=False, capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
