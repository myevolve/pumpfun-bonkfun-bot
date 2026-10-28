# ruff: noqa: S101
"""Per-source call windows. Phase 0 never rotates credentials to dodge them."""

import pytest

from meme_studio.trends.ratelimit import RateLimiter


def test_acquire_sleeps_until_the_oldest_call_leaves_the_window() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    limiter = RateLimiter(
        max_calls=2,
        per_seconds=10,
        clock=lambda: clock["t"],
        sleep=sleep,
    )
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert slept == [pytest.approx(10.0)]
    assert clock["t"] == pytest.approx(10.0)


def test_calls_inside_the_window_do_not_sleep() -> None:
    clock = {"t": 5.0}
    slept: list[float] = []
    limiter = RateLimiter(
        max_calls=3,
        per_seconds=60,
        clock=lambda: clock["t"],
        sleep=slept.append,
    )
    limiter.acquire()
    limiter.acquire()
    assert slept == []


def test_daily_style_quota_is_the_same_window() -> None:
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    limiter = RateLimiter(
        max_calls=2,
        per_seconds=100,
        clock=lambda: clock["t"],
        sleep=sleep,
    )
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert clock["t"] == pytest.approx(100.0)
