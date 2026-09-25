"""Offline regression: omitted failed receipts cannot turn a loss into a winner.

Run: uv run learning-examples/token-lifecycles/verify_census_accounting.py
No network, wallet access, signing or submission.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import record_venue_cycle_census as census

# Explicit financial scenario values and executable regression assertions.
# ruff: noqa: S101, PLR2004


def main() -> None:
    """Compare complete receipt accounting with the same cycle-only sample."""
    cycle = {
        "event": "receipt",
        "payer": "actor",
        "successful": True,
        "fee_lamports": "5000",
        "jito_tip_lamports": "0",
        "closed_cycle": True,
        "native_delta_lamports": "10000",
        "slot": 1,
        "elapsed_ns": 0,
        "raw_b64": None,
        "route": "synthetic_cycle",
        "priority_fee_lamports": "0",
    }
    failed = {
        **cycle,
        "successful": False,
        "fee_lamports": "15000",
        "native_delta_lamports": "-15000",
        "closed_cycle": False,
    }
    population = census.Hour()
    for row in (cycle, failed):
        population.add(row["slot"], row)
    with TemporaryDirectory() as directory:
        for retain in ("all", "cycles"):
            rows = [
                {"event": "manifest", "limits": {"retain": retain}},
                cycle,
                *([failed] if retain == "all" else []),
                {
                    "event": "hour",
                    "hour_index": 0,
                    "summary": population.export(),
                },
                {
                    "event": "window_end",
                    "window_index": 0,
                    "complete": True,
                    "reason": "duration_complete",
                    "summary": population.export(),
                    "cumulative": population.export(),
                },
            ]
            path = Path(directory) / f"{retain}.jsonl"
            path.write_text(
                "".join(
                    json.dumps({"seq": index, **row}) + "\n"
                    for index, row in enumerate(rows)
                )
            )
            report = census.analyze(path)
            actor = report["cycle_actors"][0]
            economics = report["market_economics"]
            assert actor["cycle_gain_lamports"] == "10000"
            assert (
                report["recorded_cumulative_summary"]["cycle_actor_failed_fee_lamports"]
                == "15000"
            )
            if retain == "all":
                assert actor["cycle_gain_minus_failed_fees_lamports"] == "-5000"
                assert economics["actors_net_positive_after_failed_fees"] == 0
                assert economics["actors_net_nonpositive"] == 1
                assert economics["losers_net_lamports_per_window"] == -5000
                assert report["hours_verified"] == 1
            else:
                assert actor["failed_fee_lamports"] is None
                assert actor["cycle_gain_minus_failed_fees_lamports"] is None
                assert economics["actors_net_positive_after_failed_fees"] is None
                assert economics["actors_net_nonpositive"] is None
                assert economics["winners_net_lamports_per_window"] is None
                assert report["total"]["cycle_actor_failed_fee_lamports"] is None
                assert report["hours_verified"] == 0
    print("PASS: full receipts expose the loss; omitted fees leave net results unknown")


if __name__ == "__main__":
    main()
