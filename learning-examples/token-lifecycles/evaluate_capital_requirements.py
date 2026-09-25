# Input failures must identify the rejected financial assumption.
# ruff: noqa: TRY003
"""Calculate capital from explicit trading assumptions; never trade or use a key.

Successful-trade returns must already be net of venue, network and execution
costs. Failed attempts and daily operating costs are charged separately. The
inputs are scenarios, not measured landing rates or a promise of future returns.
Native-asset amounts are alternatives for one allocation, not additive budgets.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from pathlib import Path

BPS = Decimal(10_000)
CENT = Decimal("0.01")
ASSET_DECIMALS = {"SOL": 9, "POL": 18, "ETH": 18}


def nonnegative(value: str) -> Decimal:
    """Parse a finite, nonnegative decimal without float conversion."""
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Expected a decimal amount") from exc
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("Amount must be finite and nonnegative")
    return amount


def positive(value: str) -> Decimal:
    """Parse a strictly positive decimal."""
    amount = nonnegative(value)
    if not amount:
        raise argparse.ArgumentTypeError("Amount must be positive")
    return amount


def asset_price(value: str) -> tuple[str, Decimal]:
    """Parse one supported native asset's explicit USD spot reference."""
    symbol, separator, price = value.partition("=")
    if not separator or symbol not in ASSET_DECIMALS:
        raise argparse.ArgumentTypeError("Use SOL=price, POL=price or ETH=price")
    return symbol, positive(price)


def required_principal(  # noqa: PLR0913
    *,
    target_usd: Decimal,
    attempts_per_day: int,
    success_probability: Decimal,
    successful_net_bps: Decimal,
    failed_attempt_cost_usd: Decimal,
    daily_operating_cost_usd: Decimal,
) -> Decimal:
    """Return per-position principal meeting the conditional daily target.

    The model is N * (p * principal * net_return - (1-p) * failure_fee)
    - daily_operating_cost. Round principal upward to a USD cent.
    """
    amounts = (
        target_usd,
        success_probability,
        successful_net_bps,
        failed_attempt_cost_usd,
        daily_operating_cost_usd,
    )
    if any(not value.is_finite() for value in amounts):
        raise ValueError("All financial assumptions must be finite")
    if target_usd <= 0 or attempts_per_day <= 0 or successful_net_bps <= 0:
        raise ValueError("Target, attempts and successful net return must be positive")
    if not 0 < success_probability <= 1:
        raise ValueError("Success probability must be in (0, 1]")
    if min(failed_attempt_cost_usd, daily_operating_cost_usd) < 0:
        raise ValueError("Failure and operating costs cannot be negative")
    with localcontext() as context:
        context.prec = 60
        required_attempt_return = (
            target_usd + daily_operating_cost_usd
        ) / attempts_per_day
        expected_failure_cost = (1 - success_probability) * failed_attempt_cost_usd
        return (
            (required_attempt_return + expected_failure_cost)
            / (success_probability * successful_net_bps / BPS)
        ).quantize(CENT, rounding=ROUND_CEILING)


def self_check() -> None:
    """Defend failure-cost amortization, rounding and impossible inputs."""
    inputs = {
        "target_usd": Decimal("1000.001"),
        "attempts_per_day": 100,
        "success_probability": Decimal("0.8"),
        "successful_net_bps": Decimal(25),
        "failed_attempt_cost_usd": Decimal("0.05"),
        "daily_operating_cost_usd": Decimal(5),
    }
    principal = required_principal(**inputs)
    if principal != Decimal("5030.01"):
        raise AssertionError("Failure fees were not amortized into principal")
    for candidate, should_meet_target in (
        (principal, True),
        (principal - CENT, False),
    ):
        daily_net = (
            100 * (Decimal("0.8") * candidate * Decimal("0.0025") - Decimal("0.01")) - 5
        )
        if (daily_net >= inputs["target_usd"]) != should_meet_target:
            raise AssertionError("Required principal does not bound the daily target")
    for probability in (Decimal(0), Decimal("1.01"), Decimal("NaN")):
        try:
            required_principal(**{**inputs, "success_probability": probability})
        except ValueError:
            continue
        raise AssertionError("Impossible success probability was accepted")
    print("PASS: failure-cost amortization, capital boundary and invalid probabilities")


def main() -> None:
    """Write a reproducible conditional funding calculation."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--target-usd", type=positive, default=Decimal(1000))
    parser.add_argument("--attempts-per-day", type=int)
    parser.add_argument("--success-probability", type=positive)
    parser.add_argument("--successful-net-bps", type=positive, nargs="+")
    parser.add_argument("--failed-attempt-cost-usd", type=nonnegative)
    parser.add_argument("--daily-operating-cost-usd", type=nonnegative)
    parser.add_argument("--concurrent-positions", type=int, default=1)
    parser.add_argument("--reserve-percent", type=nonnegative)
    parser.add_argument("--asset-price", type=asset_price, action="append", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    required = (
        args.attempts_per_day,
        args.success_probability,
        args.successful_net_bps,
        args.failed_attempt_cost_usd,
        args.daily_operating_cost_usd,
        args.reserve_percent,
        args.out,
    )
    if any(value is None for value in required):
        parser.error(
            "Supply all trading, cost, reserve assumptions and --out explicitly"
        )
    if args.attempts_per_day <= 0 or args.concurrent_positions <= 0:
        parser.error("Attempts and concurrent positions must be positive integers")
    if args.success_probability > 1:
        parser.error("Success probability must be in (0, 1]")
    if args.out.suffix != ".json":
        parser.error("--out must name a new .json file")
    prices = dict(args.asset_price)
    if len(prices) != len(args.asset_price):
        parser.error("Supply at most one price for each asset")
    scenarios = []
    with localcontext() as context:
        context.prec = 60
        for margin in args.successful_net_bps:
            principal = required_principal(
                target_usd=args.target_usd,
                attempts_per_day=args.attempts_per_day,
                success_probability=args.success_probability,
                successful_net_bps=margin,
                failed_attempt_cost_usd=args.failed_attempt_cost_usd,
                daily_operating_cost_usd=args.daily_operating_cost_usd,
            )
            working = principal * args.concurrent_positions
            reserve = (working * args.reserve_percent / 100).quantize(
                CENT, rounding=ROUND_CEILING
            )
            total = working + reserve
            scenarios.append(
                {
                    "successful_net_bps": str(margin),
                    "principal_per_position_usd": str(principal),
                    "working_capital_usd": str(working),
                    "reserve_usd": str(reserve),
                    "total_funding_usd": str(total),
                    "alternative_native_asset_amounts": {
                        symbol: str(
                            (total / price).quantize(
                                Decimal(1).scaleb(-ASSET_DECIMALS[symbol]),
                                rounding=ROUND_CEILING,
                            )
                        )
                        for symbol, price in prices.items()
                    },
                }
            )
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "evidence_level": "conditional_sizing_not_observed_returns",
        "formula": "N * (p * principal * successful_net_return - (1-p) * failed_fee) - operating_cost",
        "assumptions": {
            "target_usd_per_day": str(args.target_usd),
            "attempts_per_day": args.attempts_per_day,
            "success_probability": str(args.success_probability),
            "failed_attempt_cost_usd": str(args.failed_attempt_cost_usd),
            "daily_operating_cost_usd": str(args.daily_operating_cost_usd),
            "concurrent_positions": args.concurrent_positions,
            "reserve_percent": str(args.reserve_percent),
            "native_asset_prices_usd": {
                symbol: str(price) for symbol, price in prices.items()
            },
        },
        "scenarios": scenarios,
        "limitations": [
            "Neither the net margins, landing rates nor opportunity frequency are established by this calculation.",
            "Net successful returns must include venue, transaction, slippage and execution costs; failed attempts are charged separately.",
            "Liquidity and price impact must support the required principal; a constant margin cannot be extrapolated through a finite pool.",
            "Reserve percentage is an explicit planning assumption, not a loss bound or risk-of-ruin estimate.",
            "Native asset equivalents are alternative allocations; adding them would triple-count the same capital.",
            "Observed independent execution and failure evidence is required before treating these amounts as a funding recommendation.",
        ],
    }
    with args.out.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, indent=2)
        destination.write("\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
