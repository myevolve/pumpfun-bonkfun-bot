# ruff: noqa: TRY003
"""Published take-profit plan and the SHA-256 that would be posted at launch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from meme_studio.ladder.money import (
    dec_str,
    exact_ratio,
    lamports_to_sol_str,
    sol_to_lamports,
)

DISCLOSURE = (
    "PAPER DISCLOSURE: we created this token and we hold it. "
    "This draft is not a live sale."
)
FEE_CONFLICT = (
    "UNKNOWN post-graduation creator fee: research 0.95% conflicts with "
    "measured 30 bps; both are assumptions; headline P&L is the worse case"
)
_MAX_FEE_BPS = 10_000
MEASURED_PUMPSWAP_CREATOR_BPS = 30
RESEARCH_PUMPSWAP_CREATOR_BPS = 95
_UNKNOWNS = (
    "priority_fee_microlamports_copied_from_mint_and_buy_v2_example",
    "pool_seeded_at_continuous_price_migration_fee_unknown",
    "playbook_0.95pct_post_grad_creator_fee_conflicts_with_measured_30bps",
    "create_plus_buy_v2_may_not_fit_one_packet_launch_fee_count_may_be_2",
    "post_graduation_virtual_quote_defaults_to_zero",
)


@dataclass(frozen=True, slots=True)
class FeeAssumptions:
    """Fee inputs. Priority fee is an assumption; the curve fee is the measured 1.25%."""

    curve_protocol_bps: int = 95
    curve_creator_bps: int = 30
    curve_lp_bps: int = 0
    pumpswap_lp_bps: int = 0
    pumpswap_protocol_bps: int = 95
    pumpswap_creator_bps: int | None = None
    network_fee_lamports: int = 5_000
    priority_fee_microlamports: int = 37_037
    sell_compute_unit_limit: int = 160_000


@dataclass(frozen=True, slots=True)
class LadderThresholds:
    """Stage-10 triggers. Tranches are a share of the remaining bag."""

    recover_multiple: str = "2.5"
    t1_multiple: str = "5"
    t2_multiple: str = "8"
    t3_multiple: str = "12"
    remaining_fraction: str = "0.25"
    trail_drawdown: str = "0.5"
    trail_min_interval_s: int = 1800
    miss_after_s: int = 86_400
    miss_tranches: int = 4
    miss_spacing_s: int = 3_600
    miss_preannounce_s: int = 3_600
    slippage_cap: str = "0.05"
    launch_cost_usd: str = "1"
    horizon_s: int = 86_400


@dataclass(frozen=True, slots=True)
class SellPlan:
    """The plan text that would be posted, plus its content hash."""

    dev_buy_lamports: int
    sol_usd: str
    thresholds: LadderThresholds
    fees: FeeAssumptions

    def to_dict(self) -> dict[str, object]:
        """Return the canonical plan body. The hash is not part of the body."""
        thresholds = self.thresholds
        fees = self.fees
        priority = (
            fees.sell_compute_unit_limit * fees.priority_fee_microlamports + 999_999
        ) // 1_000_000
        fraction = dec_str(thresholds.remaining_fraction)
        return {
            "disclosure": DISCLOSURE,
            "fee_conflict": FEE_CONFLICT,
            "dev_buy_sol": lamports_to_sol_str(self.dev_buy_lamports),
            "fees": {
                "curve_creator_bps": fees.curve_creator_bps,
                "curve_lp_bps": fees.curve_lp_bps,
                "curve_protocol_bps": fees.curve_protocol_bps,
                "curve_total_bps": (
                    fees.curve_protocol_bps + fees.curve_creator_bps + fees.curve_lp_bps
                ),
                "network_fee_lamports": fees.network_fee_lamports,
                "network_fee_status": "protocol_base_fee",
                "priority_fee_lamports": priority,
                "priority_fee_microlamports": fees.priority_fee_microlamports,
                "priority_fee_status": "ASSUMPTION",
                "pumpswap_creator_bps_explicit": fees.pumpswap_creator_bps,
                "pumpswap_creator_bps_measured_assumption": (
                    MEASURED_PUMPSWAP_CREATOR_BPS
                ),
                "pumpswap_creator_bps_research_assumption": (
                    RESEARCH_PUMPSWAP_CREATOR_BPS
                ),
                "pumpswap_creator_fee_status": (
                    "UNKNOWN"
                    if fees.pumpswap_creator_bps is None
                    else "explicit_assumption"
                ),
                "pumpswap_lp_bps": fees.pumpswap_lp_bps,
                "pumpswap_protocol_bps": fees.pumpswap_protocol_bps,
                "sell_compute_unit_limit": fees.sell_compute_unit_limit,
            },
            "horizon_s": thresholds.horizon_s,
            "launch_cost_usd": dec_str(thresholds.launch_cost_usd),
            "miss": {
                "action": "sell_equal_parts_of_remainder",
                "if_not_reached": "R",
                "preannounce_seconds": thresholds.miss_preannounce_s,
                "spacing_seconds": thresholds.miss_spacing_s,
                "tranches": thresholds.miss_tranches,
                "within_seconds": thresholds.miss_after_s,
            },
            "mode": "paper",
            "rules": {
                "announce_within_seconds": 60,
                "max_sells_per_trigger": 1,
                "no_buys_after_launch": True,
                "slippage_cap": dec_str(thresholds.slippage_cap),
                "tranches_are_fraction_of_remaining": True,
            },
            "sol_usd": dec_str(self.sol_usd),
            "steps": [
                {
                    "action": "recover",
                    "id": "R",
                    "multiple": dec_str(thresholds.recover_multiple),
                    "trigger": "multiple",
                },
                {
                    "action": "sell_fraction_of_remaining",
                    "fraction_of_remaining": fraction,
                    "id": "T1",
                    "multiple": dec_str(thresholds.t1_multiple),
                    "trigger": "multiple",
                },
                {
                    "action": "sell_fraction_of_remaining",
                    "fraction_of_remaining": fraction,
                    "id": "T2",
                    "multiple": dec_str(thresholds.t2_multiple),
                    "trigger": "multiple",
                },
                {
                    "action": "sell_fraction_of_remaining",
                    "fraction_of_remaining": fraction,
                    "id": "T3",
                    "multiple": dec_str(thresholds.t3_multiple),
                    "trigger": "multiple_or_graduation",
                },
                {
                    "action": "sell_fraction_of_remaining",
                    "drawdown": dec_str(thresholds.trail_drawdown),
                    "fraction_of_remaining": fraction,
                    "id": "runner",
                    "min_interval_seconds": thresholds.trail_min_interval_s,
                    "trigger": "trail_after_T3",
                },
            ],
            "unknowns": sorted(_UNKNOWNS),
            "version": "stage10.v1",
        }

    def canonical_json(self) -> str:
        """Serialize the plan with sorted keys and no extra whitespace."""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @property
    def sha256(self) -> str:
        """SHA-256 hex of the canonical plan JSON."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def build_plan(
    dev_buy_sol: str,
    *,
    sol_usd: str,
    thresholds: LadderThresholds | None = None,
    fees: FeeAssumptions | None = None,
) -> SellPlan:
    """Build the plan that would be posted for one disclosed dev-buy size.

    Args:
        dev_buy_sol: Dev buy in SOL. 0.07, 0.1 and 0.25 are the study sizes.
        sol_usd: SOL price used to turn the $1 launch cost into SOL.
        thresholds: Optional override of the stage-10 triggers.
        fees: Optional fee assumptions.

    Returns:
        A paper sell plan. There is no live mode.
    """
    chosen = thresholds or LadderThresholds()
    chosen_fees = fees or FeeAssumptions()
    _validate(chosen)
    validate_fee_assumptions(chosen_fees)
    lamports = sol_to_lamports(dev_buy_sol)
    if lamports <= 0 or lamports > sol_to_lamports("1"):
        raise ValueError("dev buy must be between 0 and 1 SOL")
    if Decimal(sol_usd) <= 0:
        raise ValueError("sol_usd must be positive")
    return SellPlan(
        dev_buy_lamports=lamports,
        sol_usd=dec_str(sol_usd),
        thresholds=chosen,
        fees=chosen_fees,
    )


def _validate(thresholds: LadderThresholds) -> None:
    multiples = [
        Decimal(thresholds.recover_multiple),
        Decimal(thresholds.t1_multiple),
        Decimal(thresholds.t2_multiple),
        Decimal(thresholds.t3_multiple),
    ]
    if any(item <= 0 for item in multiples) or multiples != sorted(multiples):
        raise ValueError("ladder multiples must increase")
    if len(set(multiples)) != len(multiples):
        raise ValueError("ladder multiples must increase")
    fraction_num, fraction_den = exact_ratio(thresholds.remaining_fraction)
    if fraction_num <= 0 or fraction_den <= 0 or fraction_num >= fraction_den:
        raise ValueError(
            "remaining fraction must be a share of the bag, strictly below 1"
        )
    drawdown = Decimal(thresholds.trail_drawdown)
    if drawdown <= 0 or drawdown >= 1:
        raise ValueError("trail drawdown must be between 0 and 1")
    cap = Decimal(thresholds.slippage_cap)
    if cap < 0 or cap >= 1:
        raise ValueError("slippage cap must be between 0 and 1")
    if thresholds.miss_tranches < 1:
        raise ValueError("miss tranches must be positive")
    draw_num, draw_den = exact_ratio(thresholds.trail_drawdown)
    if draw_num <= 0 or draw_num >= draw_den:
        raise ValueError("trail drawdown must be between 0 and 1")


def validate_fee_assumptions(fees: FeeAssumptions) -> None:
    rates: tuple[tuple[str, int | None], ...] = (
        ("curve_protocol_bps", fees.curve_protocol_bps),
        ("curve_creator_bps", fees.curve_creator_bps),
        ("curve_lp_bps", fees.curve_lp_bps),
        ("pumpswap_lp_bps", fees.pumpswap_lp_bps),
        ("pumpswap_protocol_bps", fees.pumpswap_protocol_bps),
        ("pumpswap_creator_bps", fees.pumpswap_creator_bps),
        ("network_fee_lamports", fees.network_fee_lamports),
        ("priority_fee_microlamports", fees.priority_fee_microlamports),
        ("sell_compute_unit_limit", fees.sell_compute_unit_limit),
    )
    for name, value in rates:
        if value is None:
            if name != "pumpswap_creator_bps":
                raise ValueError(f"{name} is required")
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
        if name.endswith("_bps") and value > _MAX_FEE_BPS:
            raise ValueError(f"{name} exceeds {_MAX_FEE_BPS}")
    if fees.curve_lp_bps != 0:
        raise ValueError("curve LP fee must be 0")
    if fees.sell_compute_unit_limit <= 0:
        raise ValueError("sell compute unit limit must be positive")
    if fees.curve_protocol_bps + fees.curve_creator_bps > _MAX_FEE_BPS:
        raise ValueError("curve fees exceed 10000 bps")
    if fees.pumpswap_creator_bps is not None and (
        fees.pumpswap_lp_bps + fees.pumpswap_protocol_bps + fees.pumpswap_creator_bps
        > _MAX_FEE_BPS
    ):
        raise ValueError("pumpswap fees exceed 10000 bps")
