"""Drawdown circuit breaker: loss ladders, per-quote isolation, latching."""

from __future__ import annotations

import pytest

from core.execution_policy import (
    DrawdownBreaker,
    ExecutionMode,
    ExecutionPolicy,
)

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS = 1_000_000_000


def _policy(**overrides: object) -> ExecutionPolicy:
    defaults: dict[str, object] = {"mode": ExecutionMode.DRY_RUN}
    defaults.update(overrides)
    return ExecutionPolicy(**defaults)


def _breaker(**kwargs: object) -> DrawdownBreaker:
    built = DrawdownBreaker.from_policy(_policy(**kwargs))
    assert built is not None
    return built


def test_no_thresholds_builds_no_breaker() -> None:
    assert DrawdownBreaker.from_policy(_policy()) is None


def test_policy_rejects_zero_and_negative_loss_threshold() -> None:
    with pytest.raises(ValueError, match="max_consecutive_losses"):
        _policy(max_consecutive_losses=0)
    with pytest.raises(ValueError, match="max_consecutive_losses"):
        _policy(max_consecutive_losses=-1)


def test_consecutive_losses_trip_and_latch() -> None:
    breaker = _breaker(max_consecutive_losses=2)
    breaker.record_close(-1_000, WSOL)
    assert breaker.entry_block(WSOL) is None
    breaker.record_close(-2_000, WSOL)
    reason = breaker.entry_block(WSOL)
    assert reason is not None and "consecutive_losses=2" in reason
    # latched: a win afterwards does not clear the trip
    breaker.record_close(5_000, WSOL)
    assert breaker.entry_block(WSOL) == reason


def test_win_resets_consecutive_ladder_below_threshold() -> None:
    breaker = _breaker(max_consecutive_losses=2)
    breaker.record_close(-1_000, WSOL)
    breaker.record_close(1_000, WSOL)
    breaker.record_close(-1_000, WSOL)
    assert breaker.entry_block(WSOL) is None


def test_scalar_drawdown_cap_applies_to_every_quote() -> None:
    breaker = _breaker(max_session_drawdown_quote_raw=3_000)
    breaker.record_close(-2_000, WSOL)
    assert breaker.entry_block(USDC) is None  # separate buckets, both under cap
    breaker.record_close(-1_000, WSOL)
    reason = breaker.entry_block(WSOL)
    assert reason is not None and "session drawdown -3000" in reason


def test_scalar_drawdown_cap_applies_to_every_quote() -> None:
    breaker = _breaker(max_session_drawdown_quote_raw=3_000)
    breaker.record_close(-2_000, WSOL)
    assert breaker.entry_block(USDC) is None  # separate buckets, both under cap
    breaker.record_close(-1_000, WSOL)
    reason = breaker.entry_block(WSOL)
    assert reason is not None and "session drawdown -3000" in reason
    # the trip is a session-wide halt: every quote is blocked once latched
    assert breaker.entry_block(USDC) == reason


def test_mapping_cap_trips_on_its_own_bucket_only() -> None:
    breaker = _breaker(max_session_drawdown_quote_raw={WSOL: 3_000, USDC: 100_000})
    breaker.record_close(-3_000, WSOL)
    reason = breaker.entry_block(WSOL)
    assert reason is not None and "session drawdown -3000" in reason
    # isolation is in the accounting: the USDC bucket never moved
    assert breaker.realized_pnl_raw.get(USDC, 0) == 0
    assert breaker.entry_block(USDC) == reason  # global halt once latched


def test_mapping_without_quote_blocks_entry_fail_closed() -> None:
    breaker = _breaker(max_session_drawdown_quote_raw={WSOL: 3_000})
    breaker.record_close(-1_000, WSOL)
    assert "no session drawdown cap" in (breaker.entry_block(USDC) or "")


def test_unpriced_close_is_ignored_not_counted() -> None:
    breaker = _breaker(max_consecutive_losses=1)
    breaker.record_close(None, WSOL)
    assert breaker.entry_block(WSOL) is None
    assert breaker.consecutive_losses == 0
    assert breaker.realized_pnl_raw == {}


def test_unknown_quote_mint_accumulates_under_unknown_key() -> None:
    breaker = _breaker(max_consecutive_losses=2)
    breaker.record_close(-1_000, None)
    assert breaker.realized_pnl_raw == {"unknown": -1_000}
    breaker.record_close(-1_000, None)
    assert breaker.entry_block(WSOL) is not None


def test_record_close_rejects_non_integer_pnl() -> None:
    breaker = _breaker(max_consecutive_losses=1)
    with pytest.raises(ValueError, match="pnl_quote_raw"):
        breaker.record_close("bad", WSOL)  # type: ignore[arg-type]


def test_tripped_breaker_ignores_further_closes() -> None:
    breaker = _breaker(max_consecutive_losses=1)
    breaker.record_close(-1_000, WSOL)
    tripped = breaker.tripped_reason
    breaker.record_close(-50_000, WSOL)
    assert breaker.realized_pnl_raw[WSOL] == -1_000  # latch froze the bucket
    assert breaker.tripped_reason == tripped


def test_from_config_passes_breaker_fields() -> None:
    policy = ExecutionPolicy.from_config(
        {
            "execution": {
                "mode": "dry_run",
                "max_consecutive_losses": 3,
                "max_session_drawdown_quote_raw": {"sol": 50_000_000},
            }
        }
    )
    assert policy.max_consecutive_losses == 3
    assert policy.max_session_drawdown_quote_raw is not None
    normalized = policy.max_session_drawdown_quote_raw
    assert isinstance(normalized, dict) and WSOL in normalized
    breaker = DrawdownBreaker.from_policy(policy)
    assert breaker is not None
    assert breaker.max_consecutive_losses == 3
