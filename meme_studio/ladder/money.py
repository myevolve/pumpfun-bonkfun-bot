# ruff: noqa: TRY003
"""Decimal SOL and USD conversions for the paper ladder.

Prices are exact decimals. Lamport conversion uses half-up rounding so a
published plan and a replay agree on the same integer.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

LAMPORTS_PER_SOL = 1_000_000_000
_LAMPORTS = Decimal(LAMPORTS_PER_SOL)
_BPS = Decimal(10_000)


def dec_str(value: str | Decimal) -> str:
    """Render a decimal without exponent and without trailing zeros."""
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def sol_to_lamports(sol: str | Decimal) -> int:
    """Convert a SOL decimal string to lamports."""
    amount = (Decimal(sol) * _LAMPORTS).to_integral_value(rounding=ROUND_HALF_UP)
    return int(amount)


def lamports_to_sol_str(lamports: int) -> str:
    """Render lamports as a 9-decimal SOL string."""
    sign = "-" if lamports < 0 else ""
    raw = abs(lamports)
    return f"{sign}{raw // LAMPORTS_PER_SOL}.{raw % LAMPORTS_PER_SOL:09d}"


def usd_to_lamports(usd: str | Decimal, sol_usd: str | Decimal) -> int:
    """Convert a USD amount at a SOL price into lamports.

    Args:
        usd: Dollar amount, such as the $1 per-launch cost.
        sol_usd: SOL price in USD. This is an input, not a live quote.

    Returns:
        Rounded lamports.
    """
    sol = Decimal(usd) / Decimal(sol_usd)
    amount = (sol * _LAMPORTS).to_integral_value(rounding=ROUND_HALF_UP)
    return int(amount)


def lamports_to_usd_str(lamports: int, sol_usd: str | Decimal) -> str:
    """Convert lamports to a USD string at half-up to 1e-8."""
    usd = (Decimal(lamports) * Decimal(sol_usd)) / _LAMPORTS
    rounded = usd.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def bps_ratio(value: str | Decimal) -> tuple[int, int]:
    """Return value as a reduced fraction with 1/10000 resolution."""
    num = int((Decimal(value) * _BPS).to_integral_value(rounding=ROUND_HALF_UP))
    den = 10_000
    if num == 0:
        return 0, 1
    g = _gcd(num, den)
    return num // g, den // g


def mean_lamports(values: list[int]) -> int:
    """Arithmetic mean of lamport values, half away from zero."""
    count = len(values)
    if count == 0:
        raise ValueError("mean of an empty sample is UNKNOWN")
    total = sum(values)
    half = count // 2
    if total >= 0:
        return (total + half) // count
    return -((-total + half) // count)


def _gcd(left: int, right: int) -> int:
    while right:
        left, right = right, left % right
    return abs(left)
