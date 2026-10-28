# ruff: noqa: S101, PLR2004, S105
"""Guards required after the PR #4 review. These assert the corrected contract."""

from __future__ import annotations

import json

import pytest

from core.client import estimate_transaction_fee_lamports
from meme_studio.chainstack import resolve_geyser, resolve_solana_endpoint
from meme_studio.ladder import FeeAssumptions, LadderThresholds, build_plan, replay
from meme_studio.ladder.curve import PaperCurve
from meme_studio.ladder.engine import CoinPath, PathTrade
from meme_studio.ladder.money import exact_ratio
from meme_studio.ladder.plan import DISCLOSURE, FEE_CONFLICT

_CREATE = 1_700_000_000


def _buy(ts: int, raw: int, *, user: str = "BuyerA", fee: int | None = 0) -> PathTrade:
    return PathTrade(
        ts=ts,
        user=user,
        is_buy=True,
        sol_amount_raw=raw,
        creator_fee_raw=fee,
    )


def _sell(ts: int, raw: int) -> PathTrade:
    return PathTrade(
        ts=ts, user="BuyerA", is_buy=False, sol_amount_raw=raw, creator_fee_raw=0
    )


def _path(trades: tuple[PathTrade, ...] | list[PathTrade]) -> CoinPath:
    return CoinPath(
        mint="mint-paper",
        create_ts=_CREATE,
        creator="CreatorPaper111",
        trades=tuple(trades),
    )


def test_plan_discloses_creation_and_an_unresolved_pumpswap_creator_fee() -> None:
    plan = build_plan("0.1", sol_usd="101.47")
    body = plan.to_dict()
    fees = body["fees"]
    assert isinstance(fees, dict)
    text = json.dumps(body)
    assert "we created this token and we hold it" in str(body["disclosure"])
    assert FEE_CONFLICT in text
    assert fees["pumpswap_creator_fee_status"] == "UNKNOWN"
    assert fees["pumpswap_creator_bps_measured_assumption"] == 30
    assert fees["pumpswap_creator_bps_research_assumption"] == 95
    assert "measured_2026-10-07_fee_config_lp0_protocol95_creator30" not in text
    assert body["horizon_s"] == 86_400
    assert DISCLOSURE in plan.canonical_json()


def test_horizon_is_part_of_the_plan_hash() -> None:
    base = build_plan("0.1", sol_usd="100")
    short = build_plan(
        "0.1",
        sol_usd="100",
        thresholds=LadderThresholds(horizon_s=100),
    )
    assert base.sha256 != short.sha256


def test_priority_fee_uses_the_repository_ceiling() -> None:
    plan = build_plan("0.1", sol_usd="100")
    fees = plan.to_dict()["fees"]
    assert isinstance(fees, dict)
    expected = estimate_transaction_fee_lamports(
        plan.fees.priority_fee_microlamports,
        plan.fees.sell_compute_unit_limit,
    )
    assert fees["priority_fee_lamports"] + plan.fees.network_fee_lamports == expected
    assert expected == 10_926


def test_negative_fee_rate_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_plan(
            "0.1",
            sol_usd="100",
            fees=FeeAssumptions(curve_creator_bps=-95),
        )
    with pytest.raises(ValueError):
        PaperCurve(FeeAssumptions(pumpswap_creator_bps=30, curve_creator_bps=-95))


def test_near_one_fraction_does_not_liquidate_the_bag() -> None:
    assert exact_ratio("0.99999") == (99999, 100_000)
    plan = build_plan(
        "0.1",
        sol_usd="100",
        thresholds=LadderThresholds(remaining_fraction="0.99999"),
    )
    result = replay(_path([_buy(_CREATE + 10, 40_000_000_000)]), plan)
    assert result.held_raw > 0
    assert "0.99999" in plan.canonical_json()


def test_unobserved_miss_exits_are_not_realized_fills() -> None:
    plan = build_plan("0.1", sol_usd="100")
    quiet = replay(_path([_buy(_CREATE + 10, 1_000_000_000, fee=12345)]), plan)
    drained = replay(
        _path(
            [
                _buy(_CREATE + 10, 1_000_000_000, fee=12345),
                _sell(_CREATE + 90_000, 1_000_000_000),
            ]
        ),
        plan,
    )
    assert quiet.sells == []
    assert quiet.held_raw == quiet.dev_buy.tokens_raw
    assert quiet.cash_out_lamports == 0
    assert "unobserved_exit_censored" in quiet.flags
    assert "exit_after_data_cutoff" in quiet.flags
    assert quiet.cash_out_lamports == drained.cash_out_lamports
    assert quiet.realized_pnl_lamports == drained.realized_pnl_lamports
    assert quiet.closeout_pnl_lamports is None
    assert "residual_value_unknown" in quiet.flags


def test_miss_fills_require_an_observed_print_inside_the_horizon() -> None:
    horizon = 97_200
    plan = build_plan(
        "0.1",
        sol_usd="100",
        thresholds=LadderThresholds(horizon_s=horizon),
    )
    exits = [_CREATE + 86_400 + i * 3_600 for i in range(4)]
    covered = [_buy(ts, 1, fee=0) for ts in exits]
    result = replay(_path([_buy(_CREATE + 10, 1_000_000_000, fee=1), *covered]), plan)
    miss = [sell for sell in result.sells if sell.step.startswith("miss")]
    assert [sell.step for sell in miss] == ["miss-1", "miss-2", "miss-3", "miss-4"]
    assert [sell.ts for sell in miss] == exits
    bought = result.dev_buy.tokens_raw
    base = bought // 4
    assert [sell.tokens_raw for sell in miss] == [base, base, base, bought - 3 * base]
    assert result.held_raw == 0
    drained = replay(
        _path(
            [
                _buy(_CREATE + 10, 1_000_000_000, fee=1),
                *covered,
                _sell(_CREATE + 90_000, 1_000_000_000),
            ]
        ),
        plan,
    )
    assert drained.cash_out_lamports != result.cash_out_lamports


def test_pre_creation_trade_does_not_change_the_dev_buy() -> None:
    plan = build_plan("0.1", sol_usd="100")
    clean = replay(_path([]), plan)
    early = replay(_path([_buy(_CREATE - 1, 1_000_000_000)]), plan)
    assert early.dev_buy.tokens_raw == clean.dev_buy.tokens_raw
    assert "trade_before_create" in early.flags


def test_announcements_disclose_that_we_created_and_hold_the_token() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(_path([_buy(_CREATE + 10, 40_000_000_000, fee=1)]), plan)
    assert result.announcements
    for row in result.announcements:
        assert "we created this token and we hold it" in str(row["text"])
        assert FEE_CONFLICT in str(row["text"])
        assert row["plan_sha256"] == plan.sha256


def test_headline_pnl_is_the_worse_pumpswap_creator_fee_assumption() -> None:
    plan = build_plan("0.1", sol_usd="100")
    result = replay(_path([_buy(_CREATE + 10, 90_000_000_000, fee=1)]), plan)
    scenarios = result.scenario_realized_lamports
    assert set(scenarios) == {
        "measured_30bps_assumption",
        "research_95bps_assumption",
    }
    assert result.pumpswap_creator_fee_status == "UNKNOWN"
    assert FEE_CONFLICT in result.fee_conflict
    assert result.realized_pnl_lamports == min(scenarios.values())
    assert "pumpswap_creator_fee_unknown" in result.flags


def test_outside_pumpswap_sell_inverts_the_lp_adjusted_reserve_drop() -> None:
    fees = FeeAssumptions(
        pumpswap_lp_bps=5, pumpswap_protocol_bps=20, pumpswap_creator_bps=50
    )
    origin = PaperCurve(fees)
    origin.dev_buy(100_000_000)
    origin.apply_outside_net(is_buy=True, net_raw=90_000_000_000)
    replayed = PaperCurve(fees)
    replayed.dev_buy(100_000_000)
    replayed.apply_outside_net(is_buy=True, net_raw=90_000_000_000)
    tokens = 1_000_000_000_000
    quote = origin.preview_sell(tokens)
    assert quote is not None
    decrease = quote.gross_raw - quote.lp_fee_raw
    quote_before = replayed.pool_quote
    base_before = replayed.pool_base
    replayed.apply_outside_net(is_buy=False, net_raw=decrease)
    inferred = replayed.pool_base - base_before
    assert quote_before - replayed.pool_quote == decrease
    check = PaperCurve(fees)
    check.dev_buy(100_000_000)
    check.apply_outside_net(is_buy=True, net_raw=90_000_000_000)
    again = check.preview_sell(inferred)
    assert again is not None
    assert again.gross_raw - again.lp_fee_raw == decrease


def test_public_solana_rpc_is_rejected_without_reading_a_dotenv_file() -> None:
    public = "https://api.mainnet-beta.solana.com"
    with pytest.raises(ValueError, match="public"):
        resolve_solana_endpoint({"SOLANA_NODE_RPC_ENDPOINT": public})
    with pytest.raises(ValueError, match="UNKNOWN"):
        resolve_solana_endpoint({})
    resolved = resolve_solana_endpoint(
        {
            "SOLANA_NODE_RPC_ENDPOINT": "https://solana-mainnet.core.chainstack.com/example"
        }
    )
    assert resolved.startswith("https://solana-mainnet.core.chainstack.com/")
    endpoint, token = resolve_geyser(
        {
            "GEYSER_ENDPOINT": "https://yellowstone-solana-mainnet.core.chainstack.com",
            "GEYSER_API_TOKEN": "present",
        }
    )
    assert endpoint.startswith("https://yellowstone-solana-mainnet.core.chainstack.com")
    assert token == "present"
    with pytest.raises(ValueError, match="public"):
        resolve_geyser(
            {
                "GEYSER_ENDPOINT": "https://api.mainnet-beta.solana.com",
                "GEYSER_API_TOKEN": "present",
            }
        )
