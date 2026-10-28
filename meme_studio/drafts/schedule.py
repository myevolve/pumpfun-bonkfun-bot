"""Launch-day slots from T-30m through T+24h. Internal rows are not posts."""

from __future__ import annotations

from dataclasses import dataclass

_MINUTE = 60
_HOUR = 3600


@dataclass(frozen=True)
class Slot:
    """One scheduled draft. ``gate`` ``alive`` is skipped in recap-only mode."""

    offset: str
    delta_seconds: int
    channel: str
    template_id: str
    target: str = ""
    gate: str = "always"
    reply: bool = False
    trend_ref: bool = False
    generated: bool = False
    variant: str = ""


SLOTS: tuple[Slot, ...] = (
    Slot("T-5m", -5 * _MINUTE, "telegram", "M0", target="channel"),
    Slot("T-1m", -1 * _MINUTE, "site", "game_page", variant="pending"),
    Slot("T+30s", 30, "site", "game_page"),
    Slot("T+1m", _MINUTE, "x", "M1", trend_ref=True, generated=True),
    Slot("T+1m", _MINUTE, "x", "M2", reply=True),
    Slot("T+1m", _MINUTE, "telegram", "M1-TG", target="channel"),
    Slot("T+1m", _MINUTE, "telegram", "M1-TG", target="group"),
    Slot("T+2m", 2 * _MINUTE, "x", "P1"),
    Slot("T+5m", 5 * _MINUTE, "x", "M3-joke", generated=True),
    Slot("T+5m", 5 * _MINUTE, "x", "M3-play"),
    Slot("T+5m", 5 * _MINUTE, "x", "M3"),
    Slot("T+5m", 5 * _MINUTE, "x", "M3-rules"),
    Slot("T+15m", 15 * _MINUTE, "telegram", "M4", target="group"),
    Slot("T+30m", 30 * _MINUTE, "x", "M5", generated=True),
    Slot("T+60m", _HOUR, "x", "M7", gate="alive"),
    Slot("T+60m", _HOUR, "telegram", "M7", target="channel", gate="alive"),
    Slot("T+6h", 6 * _HOUR, "x", "M8", gate="alive"),
    Slot("T+6h", 6 * _HOUR, "telegram", "M8", target="channel", gate="alive"),
    Slot("T+12h", 12 * _HOUR, "x", "M11", gate="alive", generated=True),
    Slot("T+24h", 24 * _HOUR, "x", "M10"),
    Slot("T+24h", 24 * _HOUR, "telegram", "M10", target="channel"),
    Slot("T+24h", 24 * _HOUR, "site", "site_recap"),
)

INTERNAL: tuple[tuple[str, str], ...] = (
    ("T-30m", "Story card scored; concepts generated; collision checks run"),
    ("T-25m", "Art and game built; clip captured; unlisted URL"),
    ("T-20m", "Compliance lint on launch-day templates"),
    ("T-18m", "Approval card drafted for Steve; not sent"),
    ("T-15m", "Waiting for approval; auto-expire means no launch"),
    ("T0", "create_v2 would run only after a later phase approval"),
    ("T+3m", "pump.fun creator reply stays manual"),
    ("T+10m", "KPI check"),
    ("T+20m", "Livestream stays manual"),
    ("T+60m", "Kill check: outside buyers"),
    ("T+2h", "Community repost stays manual"),
    ("T+3h", "Ask-the-dev window stays manual"),
    ("T+24h", "KPI snapshot"),
)
