# ruff: noqa: TRY003
"""Decimal SOL and USD conversions for the paper ladder.

Prices are exact decimals. Lamport conversion uses half-up rounding so a
published plan and a replay agree on the same integer.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

LAMPORTS_PER_SOL = 1_000_000_000
_LAMPORTS = Decimal(LAMPORTS_PER_SOL)


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


def exact_ratio(value: str | Decimal) -> tuple[int, int]:
    """Return the exact rational represented by a decimal string.

    0.99999 stays 99999/100000. It is not rounded to the nearest basis point,
    which would turn it into 1 and liquidate a remainder the plan forbids.
    """
    dec = Decimal(value)
    sign, digits, exponent = dec.as_tuple()
    if not digits:
        return 0, 1
    number = 0
    for digit in digits:
        number = number * 10 + int(digit)
    if not isinstance(exponent, int):
        raise TypeError("fee ratio must be a finite decimal")
    if exponent >= 0:
        number *= 10**exponent
        den = 1
    else:
        den = 10 ** (-exponent)
    if sign:
        number = -number
    g = _gcd(abs(number), den)
    return number // g, den // g


def bps_ratio(value: str | Decimal) -> tuple[int, int]:
    """Return value as a reduced fraction with 1/10000 resolution."""
    return exact_ratio(value)


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
