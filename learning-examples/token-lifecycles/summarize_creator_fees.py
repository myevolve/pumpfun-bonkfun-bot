"""Summarize retained creator-vault observations offline, without RPC or secrets.

Usage: python summarize_creator_fees.py [--snapshots PATH]
Legacy amounts remain labeled unverified; no fees, payouts or ROI are inferred.
Input identities are recorded claims, not independent PDA or chain attestation.
"""
# ruff: noqa: TRY003, TRY301, PLR2004, C901 - explicit evidence refusal paths

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_SNAPSHOTS = (
    Path(__file__).resolve().parents[2] / ".state/learning/creator_fee_snapshots.jsonl"
)
IDENTITY = ("pool", "mint", "coin_creator", "creator_vault", "creator_vault_ata")
CENSORS = {
    "pool_missing",
    "pool_invalid",
    "pool_unavailable",
    "vault_missing",
    "vault_invalid",
    "vault_unavailable",
}
BASELINES = {
    "paired",
    "no_baseline",
    "legacy_unverifiable",
    "identity_changed",
    "previous_censored",
    "non_increasing_timestamp",
    "current_censored",
}
RAW_MAX = (1 << 64) - 1
CREATOR_FEE_RATE = Decimal("0.003")
V1_ROUNDING_TOLERANCE = Decimal("0.0000005")


def pair_baseline(row: dict, previous: dict | None) -> dict:
    """Only an observed, same-identity, immediately previous row can pair."""
    empty = {
        "baseline_ts": None,
        "interval_seconds": None,
        "balance_change_wsol_raw": None,
        "balance_change_wsol": None,
    }
    if row["observation_status"] != "observed":
        status = "current_censored"
    elif previous is None:
        status = "no_baseline"
    elif previous.get("schema_version") != 2:
        status = "legacy_unverifiable"
    elif (
        previous.get("observation_status") != "observed"
        or type(previous.get("vault_wsol_raw")) is not int
        or not 0 <= previous["vault_wsol_raw"] <= 0xFFFF_FFFF_FFFF_FFFF
    ):
        status = "previous_censored"
    elif any(
        previous.get(key) != row[key]
        for key in (
            "pool",
            "mint",
            "coin_creator",
            "creator_vault",
            "creator_vault_ata",
        )
    ):
        status = "identity_changed"
    else:
        try:
            before = datetime.strptime(previous["ts"], "%Y-%m-%dT%H:%M:%SZ")
            after = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ")
        except (KeyError, TypeError, ValueError):
            return {**empty, "baseline_status": "previous_censored"}
        interval = (after - before).total_seconds()
        if interval <= 0:
            status = "non_increasing_timestamp"
        else:
            change = row["vault_wsol_raw"] - previous["vault_wsol_raw"]
            return {
                "baseline_status": "paired",
                "baseline_ts": previous["ts"],
                "interval_seconds": interval,
                "balance_change_wsol_raw": change,
                "balance_change_wsol": change / 1e9,
            }
    return {**empty, "baseline_status": status}


def observation(row: dict, previous: dict | None) -> dict:
    """Keep legacy ambiguity and validate a recorded pair against its own history."""
    result = {key: row[key] for key in ("ts", "pool")} | {"mint": row.get("mint")}
    if row.get("schema_version", 1) == 1:
        try:
            balance = Decimal(str(row["vault_sol"]))
        except (KeyError, InvalidOperation) as error:
            raise ValueError("Invalid legacy balance") from error
        if not balance.is_finite() or balance < 0:
            raise ValueError("Invalid legacy balance")
        return {
            **result,
            "observation_status": "legacy_unverifiable",
            "baseline_status": "legacy_unverifiable",
            "vault_wsol_raw": None,
            "balance_change_wsol_raw": None,
            "legacy_recorded_balance_sol": str(balance),
        }
    if row.get("schema_version") != 2 or row.get("balance_scope") != "creator_vault":
        raise ValueError("Unsupported snapshot schema or balance scope")
    status, baseline = row.get("observation_status"), row.get("baseline_status")
    raw, change = row.get("vault_wsol_raw"), row.get("balance_change_wsol_raw")
    if status not in CENSORS | {"observed"} or baseline not in BASELINES:
        raise ValueError("Unknown snapshot observation or baseline status")
    if status == "observed":
        if type(raw) is not int or not 0 <= raw <= RAW_MAX:
            raise ValueError("Observed balance requires raw u64 units")
        if any(not isinstance(row.get(key), str) or not row[key] for key in IDENTITY):
            raise ValueError("Observed balance requires the complete vault identity")
        if baseline == "current_censored":
            raise ValueError("Observed balance cannot be currently censored")
    elif raw is not None or baseline != "current_censored":
        raise ValueError("Censored balances must remain null")
    if change is not None and type(change) is not int:
        raise ValueError("Balance changes require integer raw units")
    expected = pair_baseline(row, previous)
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError("Baseline status or amount does not match retained history")
    return {
        **result,
        "observation_status": status,
        "baseline_status": baseline,
        "coin_creator": row.get("coin_creator"),
        "creator_vault": row.get("creator_vault"),
        "creator_vault_ata": row.get("creator_vault_ata"),
        "vault_wsol_raw": raw,
        "vault_wsol": raw / 1e9 if raw is not None else None,
        "balance_change_wsol_raw": change,
        "balance_change_wsol": change / 1e9 if change is not None else None,
        "baseline_ts": row.get("baseline_ts"),
        "interval_seconds": row.get("interval_seconds"),
    }


def _update_pool_accrual(  # noqa: PLR0913
    pool: str,
    item: dict,
    last_balance: dict[str, Decimal],
    accrued: dict[str, Decimal],
    withdrawn: dict[str, Decimal],
    withdrawals: Counter[str],
) -> None:
    if item.get("baseline_status") == "identity_changed":
        last_balance.pop(pool, None)
        accrued.pop(pool, None)
        withdrawn.pop(pool, None)
        withdrawals.pop(pool, None)
    bal: Decimal | None = None
    if item["observation_status"] == "legacy_unverifiable":
        bal = Decimal(item["legacy_recorded_balance_sol"])
    elif item["observation_status"] == "observed":
        bal = Decimal(item["vault_wsol_raw"]) / Decimal(1_000_000_000)
    if bal is not None:
        if pool not in accrued:
            accrued[pool] = bal
            withdrawn[pool] = Decimal(0)
        else:
            delta = bal - last_balance[pool]
            if (
                item.get("baseline_status") == "legacy_unverifiable"
                and abs(delta) <= V1_ROUNDING_TOLERANCE
            ):
                accrued[pool] = max(Decimal(0), accrued[pool] + delta)
            elif delta > 0:
                accrued[pool] += delta
            elif delta < 0:
                withdrawn[pool] += -delta
                withdrawals[pool] += 1
        last_balance[pool] = bal
    pool_accrued = accrued.get(pool)
    pool_withdrawn = withdrawn.get(pool)
    item["accrued_wsol"] = (
        round(float(pool_accrued), 9) if pool_accrued is not None else None
    )
    item["withdrawn_wsol"] = (
        round(float(pool_withdrawn), 9) if pool_withdrawn is not None else None
    )
    item["withdrawal_count"] = withdrawals[pool]
    item["implied_volume_sol"] = (
        round(float(pool_accrued / CREATOR_FEE_RATE), 6)
        if pool_accrued is not None
        else None
    )


def load_snapshot_summary(path: Path = DEFAULT_SNAPSHOTS) -> dict:
    """Read immutable history and report coverage, not a sum of shared balances."""
    batches: dict[str, list[dict]] = {}
    previous_by_pool: dict[str, dict] = {}
    last_balance_by_pool: dict[str, Decimal] = {}
    accrued_by_pool: dict[str, Decimal] = {}
    withdrawn_by_pool: dict[str, Decimal] = {}
    withdrawals_by_pool: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    total = 0
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or any(
                    not isinstance(row.get(key), str) or not row[key]
                    for key in ("ts", "pool")
                ):
                    raise ValueError("Missing row identity")
                if row.get("schema_version") == 2 and (
                    not isinstance(row.get("mint"), str) or not row["mint"]
                ):
                    raise ValueError("Missing v2 mint identity")
                datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ")
                identity = (row["ts"], row["pool"])
                if identity in seen:
                    raise ValueError("Duplicate pool in one snapshot")
                pool = row["pool"]
                item = observation(row, previous_by_pool.get(pool))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid snapshot history at line {number}"
                ) from error
            _update_pool_accrual(
                pool,
                item,
                last_balance_by_pool,
                accrued_by_pool,
                withdrawn_by_pool,
                withdrawals_by_pool,
            )
            seen.add(identity)
            batches.setdefault(row["ts"], []).append(item)
            previous_by_pool[pool] = row
            total += 1
    if not batches:
        raise ValueError("No retained snapshots")
    times = sorted(batches)
    rows = sorted(batches[times[-1]], key=lambda row: row["pool"])
    return {
        "latest_ts": times[-1],
        "previous_ts": times[-2] if len(times) > 1 else None,
        "snapshot_count": len(times),
        "total_rows": total,
        "latest_rows": len(rows),
        "distinct_latest_pools": len({row["pool"] for row in rows}),
        "observation_status_counts": dict(
            Counter(row["observation_status"] for row in rows)
        ),
        "baseline_status_counts": dict(Counter(row["baseline_status"] for row in rows)),
        "unique_observed_creator_vaults": len(
            {
                row["creator_vault_ata"]
                for row in rows
                if row["observation_status"] == "observed"
            }
        ),
        "pools_with_withdrawals": sum(1 for row in rows if row["withdrawal_count"] > 0),
        "total_accrued_wsol": round(
            sum(row["accrued_wsol"] for row in rows if row["accrued_wsol"] is not None),
            9,
        ),
        "total_withdrawn_wsol": round(
            sum(
                row["withdrawn_wsol"]
                for row in rows
                if row["withdrawn_wsol"] is not None
            ),
            9,
        ),
        "total_implied_volume_sol": round(
            sum(
                row["implied_volume_sol"]
                for row in rows
                if row["implied_volume_sol"] is not None
            ),
            6,
        ),
        "not_attested": [
            "pool-attributed fees or creator income",
            "collections or payout recipients",
            "lifetime earnings, daily rates or investment payback",
            "authenticity of imported identities, PDA derivations or account observations",
            "chain finality",
        ],
        "observations": rows,
    }


def main() -> None:
    """Print a passive, machine-readable report; never start a collector."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    args = parser.parse_args()
    try:
        summary = load_snapshot_summary(args.snapshots)
    except (OSError, ValueError) as error:
        parser.error(f"Snapshot summary failed: {type(error).__name__}")
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
