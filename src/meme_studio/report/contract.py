"""JSON contract for the phase-0 shadow tracker.

The design names ``phase0/<date>/shadow_join.parquet``. No producer of that
parquet file is in this repository, so parquet compatibility is UNKNOWN.
This module validates the JSON document the report can actually read. Missing
measurement keys are not errors: the loader keeps them as UNKNOWN instead of
zero. A present empty list is a measured empty observation.
"""

from __future__ import annotations

import math

PARQUET_PRODUCER_STATUS = "UNKNOWN"
CONTRACT_VERSION = "phase0-shadow-join-json-1"
MINT_IDENTITY_FIELDS = ("mint", "creator", "name", "symbol")
MEASUREMENT_FIELDS = (
    "dev_buy_sol",
    "mayhem",
    "graduation_ts",
    "trades",
    "coin_creator_fees",
)
TRADE_FIELDS = ("ts", "user", "is_buy", "sol_amount", "creator_fee")


def validate_shadow_document(payload: object) -> tuple[str, ...]:
    """Return contract violations. Absent measurements are allowed.

    The returned tuple is empty when the document matches the JSON contract.
    It does not claim the numbers were measured on chain.
    """
    if not isinstance(payload, dict):
        return ("document_not_object",)
    reasons: list[str] = []
    if "cohorts" not in payload:
        reasons.append("missing_cohorts")
        return tuple(reasons)
    cohorts = payload.get("cohorts")
    if not isinstance(cohorts, list):
        reasons.append("cohorts_not_list")
        return tuple(reasons)
    for index, cohort in enumerate(cohorts):
        reasons.extend(_cohort_reasons(index, cohort))
    return tuple(reasons)


def _cohort_reasons(index: int, cohort: object) -> list[str]:
    if not isinstance(cohort, dict):
        return [f"cohort_{index}_not_object"]
    reasons: list[str] = []
    if "story_id" not in cohort or not isinstance(cohort.get("story_id"), str):
        reasons.append(f"cohort_{index}_story_id")
    mints = cohort.get("mints")
    if not isinstance(mints, list):
        reasons.append(f"cohort_{index}_mints")
        return reasons
    for mint_index, mint in enumerate(mints):
        reasons.extend(_mint_reasons(index, mint_index, mint))
    return reasons


def _mint_reasons(cohort_index: int, mint_index: int, mint: object) -> list[str]:
    prefix = f"cohort_{cohort_index}_mint_{mint_index}"
    if not isinstance(mint, dict):
        return [f"{prefix}_not_object"]
    reasons: list[str] = []
    for field in MINT_IDENTITY_FIELDS:
        if (
            field not in mint
            or not isinstance(mint.get(field), str)
            or not mint.get(field)
        ):
            reasons.append(f"{prefix}_missing_{field}")
    if "trades" in mint and not isinstance(mint.get("trades"), list):
        reasons.append(f"{prefix}_trades_not_list")
    if "coin_creator_fees" in mint and not isinstance(
        mint.get("coin_creator_fees"), list
    ):
        reasons.append(f"{prefix}_fees_not_list")
    reasons.extend(_mayhem_reasons(prefix, mint))
    reasons.extend(_trade_list_reasons(prefix, mint.get("trades")))
    reasons.extend(_fee_list_reasons(prefix, mint.get("coin_creator_fees")))
    return reasons


def _mayhem_reasons(prefix: str, mint: dict[str, object]) -> list[str]:
    if "mayhem" in mint and not isinstance(mint.get("mayhem"), bool):
        return [f"{prefix}_mayhem_not_bool"]
    return []


def _trade_list_reasons(prefix: str, trades: object) -> list[str]:
    if not isinstance(trades, list):
        return []
    reasons: list[str] = []
    for trade_index, trade in enumerate(trades):
        reasons.extend(_trade_reasons(prefix, trade_index, trade))
    return reasons


def _fee_list_reasons(prefix: str, fees: object) -> list[str]:
    if not isinstance(fees, list):
        return []
    reasons: list[str] = []
    for fee_index, fee in enumerate(fees):
        reasons.extend(_fee_reasons(prefix, fee_index, fee))
    return reasons


def _trade_reasons(prefix: str, trade_index: int, trade: object) -> list[str]:
    if not isinstance(trade, dict):
        return [f"{prefix}_trade_{trade_index}_not_object"]
    reasons: list[str] = []
    for field in TRADE_FIELDS:
        if field not in trade:
            reasons.append(f"{prefix}_trade_{trade_index}_missing_{field}")
    if "is_buy" in trade and not isinstance(trade.get("is_buy"), bool):
        reasons.append(f"{prefix}_trade_{trade_index}_is_buy_not_bool")
    if "creator_fee" in trade and _bad_fee(trade.get("creator_fee")):
        reasons.append(f"{prefix}_trade_{trade_index}_creator_fee")
    return reasons


def _fee_reasons(prefix: str, fee_index: int, fee: object) -> list[str]:
    label = f"{prefix}_fee_{fee_index}"
    if not isinstance(fee, dict):
        return [f"{label}_not_object"]
    if "amount_sol" not in fee:
        return [f"{label}_missing_amount_sol"]
    if _bad_fee(fee.get("amount_sol")):
        return [f"{label}_amount_sol"]
    return []


def _bad_fee(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return True
    number = float(value)
    return not math.isfinite(number) or number < 0
