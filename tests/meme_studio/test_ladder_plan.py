# ruff: noqa: S101, PLR2004
"""Published sell-plan JSON and its SHA-256 commitment."""

from __future__ import annotations

import hashlib
import json

import pytest

from meme_studio.ladder import FeeAssumptions, LadderThresholds, build_plan


def test_plan_hash_is_sha256_of_canonical_json_without_the_hash() -> None:
    plan = build_plan("0.1", sol_usd="101.47")
    body = json.loads(plan.canonical_json())

    assert body["mode"] == "paper"
    assert body["dev_buy_sol"] == "0.100000000"
    assert body["launch_cost_usd"] == "1"
    assert "sha256" not in body
    assert body["rules"]["no_buys_after_launch"] is True
    assert body["rules"]["max_sells_per_trigger"] == 1
    assert body["rules"]["announce_within_seconds"] == 60
    assert body["rules"]["tranches_are_fraction_of_remaining"] is True
    steps = {step["id"]: step for step in body["steps"]}
    assert steps["R"]["action"] == "recover"
    assert steps["R"]["multiple"] == "2.5"
    assert steps["T1"]["fraction_of_remaining"] == "0.25"
    assert steps["T1"]["multiple"] == "5"
    assert steps["T2"]["multiple"] == "8"
    assert steps["T3"]["multiple"] == "12"
    assert steps["T3"]["trigger"] == "multiple_or_graduation"
    assert steps["runner"]["drawdown"] == "0.5"
    assert steps["runner"]["min_interval_seconds"] == 1800
    assert steps["runner"]["fraction_of_remaining"] == "0.25"
    assert body["miss"]["tranches"] == 4
    assert body["miss"]["spacing_seconds"] == 3600
    assert body["miss"]["preannounce_seconds"] == 3600
    assert body["fees"]["curve_total_bps"] == 125
    assert body["fees"]["priority_fee_status"] == "ASSUMPTION"
    encoded = plan.canonical_json().encode("utf-8")
    assert plan.sha256 == hashlib.sha256(encoded).hexdigest()
    assert plan.sha256 == hashlib.sha256(encoded).hexdigest()


def test_disclosed_dev_buy_sizes_change_the_hash_and_rebuilds_match() -> None:
    hashes = {
        size: build_plan(size, sol_usd="101.47").sha256
        for size in ("0.07", "0.1", "0.25")
    }

    assert len(set(hashes.values())) == 3
    assert build_plan("0.10", sol_usd="101.47").sha256 == hashes["0.1"]
    assert build_plan("0.070000000", sol_usd="101.47").sha256 == hashes["0.07"]


def test_threshold_change_changes_the_published_hash() -> None:
    base = build_plan("0.1", sol_usd="101.47")
    moved = build_plan(
        "0.1",
        sol_usd="101.47",
        thresholds=LadderThresholds(recover_multiple="2.0"),
    )

    assert base.sha256 != moved.sha256


def test_plan_rejects_a_tranche_that_would_sell_the_whole_bag() -> None:
    with pytest.raises(ValueError, match="remaining"):
        build_plan(
            "0.1",
            sol_usd="101.47",
            thresholds=LadderThresholds(remaining_fraction="1"),
        )


def test_plan_records_fee_unknowns_instead_of_hiding_them() -> None:
    plan = build_plan("0.25", sol_usd="101.47")
    unknowns = set(plan.to_dict()["unknowns"])

    assert "priority_fee_microlamports_copied_from_mint_and_buy_v2_example" in unknowns
    assert "pool_seeded_at_continuous_price_migration_fee_unknown" in unknowns
    assert (
        "playbook_0.95pct_post_grad_creator_fee_conflicts_with_measured_30bps"
        in unknowns
    )
    assert plan.to_dict()["fees"]["pumpswap_creator_bps"] == 30
    assert isinstance(FeeAssumptions().priority_fee_microlamports, int)
