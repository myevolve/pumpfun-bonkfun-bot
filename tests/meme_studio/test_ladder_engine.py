# ruff: noqa: S101, PLR2004
"""Paper take-profit ladder replay. No wallet, no network, no transactions."""

from __future__ import annotations

import ast
import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from meme_studio.ladder import (
    FeeAssumptions,
    LadderThresholds,
    build_plan,
    replay,
)
from meme_studio.ladder.engine import CoinPath, PathTrade

_ROOT = Path(__file__).resolve().parents[2]
_CREATE = 1_700_000_000


def _path(
    trades: list[PathTrade], *, extra_fee: int | None = None, mint: str = "mint-paper"
) -> CoinPath:
    return CoinPath(
        mint=mint,
        create_ts=_CREATE,
        creator="CreatorPaper111",
        trades=tuple(trades),
        extra_observed_creator_fee_raw=extra_fee,
        pumpswap_fees_known=True,
    )


def _buy(ts: int, sol_raw: int, *, user: str = "BuyerA", fee: int = 0) -> PathTrade:
    return PathTrade(
        ts=ts,
        user=user,
        is_buy=True,
        sol_amount_raw=sol_raw,
        creator_fee_raw=fee,
    )


def _sell(ts: int, sol_raw: int, *, user: str = "BuyerA") -> PathTrade:
    return PathTrade(
        ts=ts, user=user, is_buy=False, sol_amount_raw=sol_raw, creator_fee_raw=0
    )


def test_tranches_are_a_share_of_remaining_and_a_runner_survives() -> None:
    plan = build_plan("0.1", sol_usd="101.47")
    result = replay(
        _path([_buy(_CREATE + 10, 40_000_000_000)]),
        plan,
    )
    steps = [sell.step for sell in result.sells]

    assert steps == ["R", "T1"]
    bought = result.dev_buy.tokens_raw
    recover = result.sells[0]
    t1 = result.sells[1]
    after_recover = bought - recover.tokens_raw
    assert t1.tokens_raw == after_recover * 2500 // 10_000
    assert t1.tokens_raw != bought * 2500 // 10_000
    assert 0 < recover.tokens_raw < bought
    assert result.held_raw > 0
    assert result.held_raw == bought - recover.tokens_raw - t1.tokens_raw
    target = (
        result.dev_buy.lamports_raw
        + result.launch_cost_lamports
        + recover.tx_fee_lamports
    )
    assert recover.net_raw + recover.creator_fee_raw >= target
    assert result.reached_recover is True


def test_full_ladder_through_twelve_x_keeps_a_runner() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(_path([_buy(_CREATE + 10, 80_000_000_000)]), plan)
    steps = [sell.step for sell in result.sells]

    assert steps == ["R", "T1", "T2", "T3"]
    assert result.held_raw > 0
    bought = result.dev_buy.tokens_raw
    assert result.held_raw > bought // 10


def test_trailing_exit_fires_once_per_thirty_minutes() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(
        _path(
            [
                _buy(_CREATE + 10, 80_000_000_000),
                _buy(_CREATE + 20, 2_000_000_000),
                _sell(_CREATE + 200, 40_000_000_000),
                _buy(_CREATE + 300, 40_200_000_000),
                _sell(_CREATE + 400, 45_000_000_000),
                _sell(_CREATE + 2_000, 1),
            ]
        ),
        plan,
    )
    runners = [sell for sell in result.sells if sell.step == "runner"]

    assert len(runners) == 2
    assert runners[1].ts - runners[0].ts >= 1800
    assert result.held_raw > 0


def test_miss_sells_four_equal_tranches_each_preannounced_one_hour_ahead() -> None:
    plan = build_plan(
        "0.1",
        sol_usd="100",
        thresholds=LadderThresholds(horizon_s=97_200),
    )
    exits = [_CREATE + 86_400 + index * 3_600 for index in range(4)]
    result = replay(
        _path(
            [
                _buy(_CREATE + 10, 1_000_000_000, fee=12345),
                *[_buy(ts, 1) for ts in exits],
            ]
        ),
        plan,
    )
    miss = [sell for sell in result.sells if sell.step.startswith("miss")]
    pre = [row for row in result.announcements if row["kind"] == "PREANNOUNCE"]

    assert [sell.step for sell in miss] == ["miss-1", "miss-2", "miss-3", "miss-4"]
    assert result.reached_recover is False
    assert result.held_raw == 0
    bought = result.dev_buy.tokens_raw
    base = bought // 4
    amounts = [sell.tokens_raw for sell in miss]
    assert amounts == [base, base, base, bought - 3 * base]
    assert [sell.ts for sell in miss] == [
        _CREATE + 86_400 + i * 3_600 for i in range(4)
    ]
    assert result.held_raw == 0
    assert result.closeout_pnl_lamports == result.realized_pnl_lamports
    assert "open_runner" not in result.flags
    assert len(pre) == 4
    for row, sell in zip(pre, miss, strict=True):
        assert row["ts"] == sell.ts - 3_600
        assert row["step"] == sell.step
    assert result.observed_outside_creator_fee_lamports == 12345


def test_conditional_miss_preannounce_cancels_when_recover_prints() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(_path([_buy(_CREATE + 82_800 + 100, 40_000_000_000)]), plan)
    kinds = [row["kind"] for row in result.announcements]

    assert "PREANNOUNCE" in kinds
    assert "CANCEL" in kinds
    assert not any(sell.step.startswith("miss") for sell in result.sells)
    assert any(sell.step == "R" for sell in result.sells)


def test_real_creator_trades_are_removed_because_we_replace_the_dev() -> None:
    plan = build_plan("0.1", sol_usd="100")
    quiet = replay(_path([]), plan)
    replaced = replay(
        _path([_buy(_CREATE + 5, 50_000_000_000, user="CreatorPaper111", fee=99)]),
        plan,
    )

    assert [sell.tokens_raw for sell in quiet.sells] == [
        sell.tokens_raw for sell in replaced.sells
    ]
    assert replaced.observed_outside_creator_fee_lamports == 0
    assert "replaced_real_dev" in replaced.flags


def test_realized_pnl_identity_includes_fees_launch_cost_and_creator_rebate() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(_path([_buy(_CREATE + 10, 40_000_000_000, fee=5000)]), plan)
    priority = (
        plan.fees.priority_fee_microlamports * plan.fees.sell_compute_unit_limit
        + 999_999
    ) // 1_000_000
    per_sell = plan.fees.network_fee_lamports + priority

    assert result.launch_cost_lamports == 10_000_000
    assert per_sell == 10_926
    assert result.tx_fee_lamports == per_sell * len(result.sells)
    assert result.own_creator_rebate_lamports > 0
    assert result.observed_outside_creator_fee_lamports == 5000
    assert result.realized_pnl_lamports == (
        result.cash_out_lamports
        - result.dev_buy.lamports_raw
        - result.tx_fee_lamports
        - result.launch_cost_lamports
        + result.own_creator_rebate_lamports
        + result.observed_outside_creator_fee_lamports
    )


def test_open_runner_is_not_marked_as_sold_in_realized_pnl() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(_path([_buy(_CREATE + 10, 80_000_000_000)]), plan)

    assert result.held_raw > 0
    assert "open_runner" in result.flags
    assert result.closeout_pnl_lamports != result.realized_pnl_lamports
    assert "closeout_is_mark_not_plan" in result.flags


def test_every_simulated_sell_is_logged_as_a_paper_announcement() -> None:
    plan = build_plan("0.07", sol_usd="101.47")
    result = replay(_path([_buy(_CREATE + 10, 40_000_000_000)]), plan)
    sell_rows = [row for row in result.announcements if row["kind"] == "SELL"]

    assert [row["step"] for row in sell_rows] == [sell.step for sell in result.sells]
    for row, sell in zip(sell_rows, result.sells, strict=True):
        assert row["plan_sha256"] == plan.sha256
        assert row["tx"].startswith("paper://")
        assert "paper" in row["tx"]
        assert row["within_seconds"] == 60
        assert sell.tx_link == row["tx"]
        assert row["text"].startswith("PAPER SELL ")
        assert "We have not created this token and we do not hold it" in row["text"]
        assert plan.sha256 in row["text"]
        assert "http://" not in row["text"]
        assert "https://" not in row["text"]


def test_replay_is_deterministic() -> None:
    plan = build_plan("0.25", sol_usd="101.47")
    path = _path([_buy(_CREATE + 10, 20_000_000_000, fee=7)])
    first = replay(path, plan)
    second = replay(path, plan)

    assert first.announcements == second.announcements
    assert first.realized_pnl_lamports == second.realized_pnl_lamports
    assert first.plan_sha256 == plan.sha256 == second.plan_sha256


def test_zero_slippage_cap_blocks_the_sell() -> None:
    plan = build_plan(
        "0.1",
        sol_usd="100",
        thresholds=LadderThresholds(slippage_cap="0"),
    )
    result = replay(_path([_buy(_CREATE + 10, 40_000_000_000)]), plan)

    assert result.sells == []
    assert result.held_raw == result.dev_buy.tokens_raw
    assert "slippage_blocked" in result.flags


def test_tracker_graduation_does_not_push_later_flow_through_our_open_curve() -> None:
    plan = build_plan("0.1", sol_usd="100")
    early = _buy(_CREATE + 10, 1_000_000_000)
    stopped = replay(_path([early]), plan)
    continued = replay(
        CoinPath(
            mint="mint-paper",
            create_ts=_CREATE,
            creator="CreatorPaper111",
            trades=(early, _buy(_CREATE + 20, 5_000_000_000)),
            graduation_ts=_CREATE + 15,
        ),
        plan,
    )

    assert [sell.tokens_raw for sell in continued.sells] == [
        sell.tokens_raw for sell in stopped.sells
    ]
    assert continued.cash_out_lamports == stopped.cash_out_lamports
    assert continued.graduated_on_our_curve is False
    assert "real_coin_graduated_our_curve_did_not" in continued.flags


def test_graduation_sell_is_on_pumpswap_with_configured_fees() -> None:
    plan = build_plan(
        "0.1",
        sol_usd="100",
        fees=FeeAssumptions(
            pumpswap_lp_bps=5,
            pumpswap_protocol_bps=20,
            pumpswap_creator_bps=50,
        ),
    )
    result = replay(_path([_buy(_CREATE + 10, 90_000_000_000)]), plan)
    pumped = [sell for sell in result.sells if sell.venue == "pumpswap"]

    assert result.graduated_on_our_curve is True
    assert pumped
    assert pumped[-1].lp_fee_raw > 0
    assert "post_graduation_path_replayed" in result.flags or pumped


def test_replay_logs_each_sell(caplog: pytest.LogCaptureFixture) -> None:
    plan = build_plan("0.1", sol_usd="100")
    with caplog.at_level(logging.INFO, logger="meme_studio.ladder.paper"):
        result = replay(_path([_buy(_CREATE + 10, 40_000_000_000)]), plan)

    sell_lines = [
        rec.message for rec in caplog.records if rec.message.startswith("PAPER SELL")
    ]
    assert len(sell_lines) == len(result.sells) > 0
    assert all(plan.sha256 in line for line in sell_lines)
    assert all(
        "We have not created this token and we do not hold it" in line
        for line in sell_lines
    )


def test_package_source_has_no_wallet_rpc_or_network_client() -> None:
    banned = (
        "send_transaction",
        "Keypair",
        "private_key",
        "SOLANA_PRIVATE",
        "aiohttp",
        "websockets",
        "httpx",
        "urllib",
        "requests",
        ".env",
        "AsyncClient",
    )
    root = _ROOT / "meme_studio"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        ast.parse(text)
        for token in banned:
            if token in text:
                offenders.append(f"{path.name}:{token}")
    assert offenders == []


def test_replay_does_not_open_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    plan = build_plan("0.1", sol_usd="100")
    replay(_path([_buy(_CREATE + 10, 1_000_000_000)]), plan)
