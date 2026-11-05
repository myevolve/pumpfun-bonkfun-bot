"""Per-channel send governor. Draft lint skips this when no clock is supplied.

Telegram limits are the documented Bot API ceilings. The X daily cap is a
local conservative stand-in: the official automation ceiling is UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

TELEGRAM_PER_SECOND = 1
TELEGRAM_PER_MINUTE = 20
X_PER_DAY = 50
_SECOND = 1
_MINUTE = 60
_DAY = 86400


@dataclass(frozen=True)
class RatePolicy:
    """Maximum messages already sent inside each window before the next one fails."""

    per_second: int | None = None
    per_minute: int | None = None
    per_day: int | None = None


POLICIES: dict[str, RatePolicy] = {
    "telegram": RatePolicy(
        per_second=TELEGRAM_PER_SECOND,
        per_minute=TELEGRAM_PER_MINUTE,
    ),
    "x": RatePolicy(per_day=X_PER_DAY),
}


def _elapsed_seconds(later: datetime, earlier: datetime) -> float:
    """Return absolute elapsed seconds, including across a DST rollback."""
    return (_as_utc(later) - _as_utc(earlier)).total_seconds()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _within(now: datetime, recent: tuple[datetime, ...], window: int) -> int:
    count = 0
    for stamp in recent:
        delta = _elapsed_seconds(now, stamp)
        if 0 <= delta < window:
            count += 1
    return count


def rate_reasons(
    channel: str,
    now: datetime | None,
    recent: tuple[datetime, ...],
) -> list[str]:
    """Return rate-limit reasons for a post that would be sent at ``now``."""
    if now is None:
        return []
    policy = POLICIES.get(channel)
    if policy is None:
        return []
    reasons: list[str] = []
    if (
        policy.per_second is not None
        and _within(now, recent, _SECOND) >= policy.per_second
    ):
        reasons.append(f"rate_limit:{channel}_per_second")
    if (
        policy.per_minute is not None
        and _within(now, recent, _MINUTE) >= policy.per_minute
    ):
        reasons.append(f"rate_limit:{channel}_per_minute")
    if policy.per_day is not None and _within(now, recent, _DAY) >= policy.per_day:
        reasons.append(f"rate_limit:{channel}_per_day")
    return reasons
