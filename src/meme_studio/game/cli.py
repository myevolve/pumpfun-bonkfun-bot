"""Command line for draft game builds."""

from __future__ import annotations

import argparse
from pathlib import Path

from meme_studio.art.guardrails import GuardrailError
from meme_studio.game.build import build
from meme_studio.game.concept import ConceptError


def main(argv: list[str] | None = None) -> int:
    """Run the game builder.

    Args:
        argv: Argument vector without the program name. Defaults to sys.argv.

    Returns:
        Zero on success, two on a bad concept or guardrail failure.
    """
    parser = argparse.ArgumentParser(prog="meme_studio.game")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="build a draft game and hub page")
    build_parser.add_argument("--concept", required=True, type=Path)
    build_parser.add_argument("--out", required=True, type=Path)
    build_parser.add_argument("--game-only", action="store_true")
    build_parser.add_argument("--skip-clip", action="store_true")
    args = parser.parse_args(argv)
    try:
        build(
            args.concept,
            args.out,
            game_only=args.game_only,
            skip_clip=args.skip_clip,
        )
    except (ConceptError, GuardrailError):
        return 2
    return 0


__all__ = ["main"]
