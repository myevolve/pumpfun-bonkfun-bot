# ruff: noqa: S101
"""Transaction guard: one wallet, create-only buys, plan sells, no hidden bags."""

from __future__ import annotations

from meme_studio.compliance import TxIntent, tx_guard
from tests.meme_studio.conftest import COLD, CREATOR, HIDDEN

_HASH_HEX_LEN = 64


def _create(**overrides: object) -> TxIntent:
    base = TxIntent(
        creator_wallet=CREATOR,
        signers=(CREATOR,),
        kind="create_buy",
        sol_amount=0.1,
        daily_sol_spent=0.0,
        daily_sol_cap=1.0,
        wallet_balance_sol=0.2,
        wallet_balance_cap_sol=1.0,
    )
    values = base.__dict__.copy()
    values.update(overrides)
    return TxIntent(**values)


def test_disclosed_create_buy_passes() -> None:
    result = tx_guard(_create())
    assert result.passed is True
    assert result.status == "pass"
    assert len(result.payload_hash) == _HASH_HEX_LEN


def test_other_signer_fails() -> None:
    result = tx_guard(_create(signers=(CREATOR, HIDDEN)))
    assert "tx_signer" in result.reasons
    alone = tx_guard(_create(signers=(HIDDEN,)))
    assert "tx_signer" in alone.reasons


def test_buy_after_create_fails() -> None:
    result = tx_guard(_create(kind="buy", sol_amount=0.05))
    assert result.passed is False
    assert "tx_buy_after_create" in result.reasons


def test_sell_must_match_committed_plan() -> None:
    ok = tx_guard(
        _create(
            kind="sell",
            sol_amount=0.0,
            amount_tokens=100,
            plan_step="T1",
            expected_step="T1",
            expected_amount_tokens=100,
            plan_hash="a" * 64,
            committed_plan_hash="a" * 64,
        )
    )
    assert ok.passed is True
    wrong_step = tx_guard(
        _create(
            kind="sell",
            sol_amount=0.0,
            amount_tokens=100,
            plan_step="T2",
            expected_step="T1",
            expected_amount_tokens=100,
            plan_hash="a" * 64,
            committed_plan_hash="a" * 64,
        )
    )
    assert "tx_plan_step" in wrong_step.reasons
    wrong_amount = tx_guard(
        _create(
            kind="sell",
            sol_amount=0.0,
            amount_tokens=99,
            plan_step="T1",
            expected_step="T1",
            expected_amount_tokens=100,
            plan_hash="a" * 64,
            committed_plan_hash="a" * 64,
        )
    )
    assert "tx_plan_amount" in wrong_amount.reasons
    wrong_hash = tx_guard(
        _create(
            kind="sell",
            sol_amount=0.0,
            amount_tokens=100,
            plan_step="T1",
            expected_step="T1",
            expected_amount_tokens=100,
            plan_hash="b" * 64,
            committed_plan_hash="a" * 64,
        )
    )
    assert "tx_plan_hash" in wrong_hash.reasons


def test_dev_buy_daily_and_balance_caps() -> None:
    assert tx_guard(_create(sol_amount=0.25)).passed is True
    over = tx_guard(_create(sol_amount=0.26))
    assert "tx_dev_buy_cap" in over.reasons
    daily = tx_guard(_create(daily_sol_spent=0.9, sol_amount=0.2, daily_sol_cap=1.0))
    assert "tx_daily_cap" in daily.reasons
    heavy = tx_guard(_create(wallet_balance_sol=1.5, wallet_balance_cap_sol=1.0))
    assert "tx_balance_cap" in heavy.reasons


def test_unknown_kinds_fail_closed() -> None:
    for kind in ("bundle_buy", "wash_trade", "fund_wallet", "round_trip"):
        result = tx_guard(_create(kind=kind, sol_amount=0.0))
        assert result.passed is False
        assert "tx_kind_unsupported" in result.reasons


def test_non_finite_and_negative_amounts_fail() -> None:
    nan = tx_guard(_create(sol_amount=float("nan")))
    assert nan.passed is False
    assert "tx_amount" in nan.reasons
    negative = tx_guard(_create(sol_amount=-1.0))
    assert negative.passed is False
    assert "tx_amount" in negative.reasons
    bad_balance = tx_guard(_create(wallet_balance_sol=float("inf")))
    assert "tx_amount" in bad_balance.reasons


def test_token_transfer_and_withdraw_destinations() -> None:
    moved = tx_guard(
        _create(kind="token_transfer", sol_amount=0.0, token_destination=HIDDEN)
    )
    assert "tx_hidden_distribution" in moved.reasons
    cold = tx_guard(
        _create(
            kind="sol_withdraw",
            sol_amount=0.05,
            destination=COLD,
            cold_address=COLD,
        )
    )
    assert cold.passed is True
    side = tx_guard(
        _create(
            kind="sol_withdraw",
            sol_amount=0.05,
            destination=HIDDEN,
            cold_address=COLD,
        )
    )
    assert "tx_withdraw_destination" in side.reasons
