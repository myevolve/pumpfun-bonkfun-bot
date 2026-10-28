"""Cartoon parody prompts for original characters.

The wording is wild and provocative on purpose, and it stays inside the media
linter: no real person, no sexual content, no gore, no hate, no minors, no
brand marks.

This module's public helper is ``build_wild_meme_prompt``. A parallel art
pipeline names its builders ``build_image_prompt``, ``build_video_prompt``,
and ``creative_portion`` and passes a concept object plus an asset kind.
Those names are intentionally not reused here, so both can live in this
module after a merge.
"""

from __future__ import annotations


def build_wild_meme_prompt(*, character: str, joke_line: str, game_title: str) -> str:
    """Return a Grok Imagine prompt for one original cartoon character."""
    return (
        "Wild provocative absurd cartoon parody sticker, original non-human "
        "character, exaggerated shapes, flat colors, no lettering, no brand marks. "
        f"Character: {character}. Scene: {joke_line}. Title: {game_title}."
    )
