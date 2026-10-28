"""Static hub index for the archive of daily parody games."""

from __future__ import annotations

import html
import re

from meme_studio.game.page import DISCLOSURE


def slugify(title: str) -> str:
    """Return a URL slug for a game title.

    Args:
        title: Human title.

    Returns:
        Lowercase hyphenated slug.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "game"


def render_index(entries: list[dict[str, object]]) -> str:
    """Render the archive index.

    Args:
        entries: Catalog rows, newest first. Ticker is shown only when present.

    Returns:
        HTML document. It does not post or link to a wallet.
    """
    items = "\n".join(_item(entry) for entry in entries)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Daily parody games</title>
<style>
body {{ margin: 0; font-family: system-ui, sans-serif; background: #1b1f3b; color: #f4f1de; }}
main {{ max-width: 40rem; margin: 0 auto; padding: 16px; }}
a {{ color: #f2c14e; }}
li {{ margin: 12px 0; }}
</style>
</head>
<body>
<main>
<h1>Daily parody games</h1>
<ul>
{items}
</ul>
<footer id="disclosure">{DISCLOSURE}</footer>
</main>
</body>
</html>
"""


def _item(entry: dict[str, object]) -> str:
    title = html.escape(str(entry.get("title", "game")))
    date = html.escape(str(entry.get("date", "")))
    page = html.escape(str(entry.get("page", "")), quote=True)
    template = html.escape(str(entry.get("template_id", "")))
    ticker = entry.get("ticker")
    ticker_html = (
        f" <span>{html.escape(str(ticker))}</span>" if isinstance(ticker, str) else ""
    )
    return (
        f'<li><a href="{page}">{title}</a>{ticker_html}'
        f" <time>{date}</time> <span>{template}</span></li>"
    )
