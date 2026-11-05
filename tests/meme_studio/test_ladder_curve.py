# ruff: noqa: S101, PLR2004
"""Exact bonding-curve price impact and fees for the paper ladder."""

from __future__ import annotations

from decimal import Decimal

from meme_studio.ladder import FeeAssumptions, PaperCurve, usd_to_lamports

_VT = 1_073_000_000_000_000
_VQ = 30_000_000_000


def _idl_buy(spend: int, vt: int, vq: int) -> tuple[int, int, int, int]:
    """IDL buy_exact_sol_in quote, fees left as first computed (no recompute)."""
    net = spend * 10_000 // (10_000 + 125)
    protocol = (net * 95 + 9_999) // 10_000
    creator = (net * 30 + 9_999) // 10_000
    overshoot = net + protocol + creator - spend
    if overshoot > 0:
        net -= overshoot
    tokens = (net - 1) * vt // (vq + net - 1)
    return tokens, net, protocol, creator


def _idl_sell(tokens: int, vt: int, vq: int) -> tuple[int, int, int, int]:
    gross = tokens * vq // (vt + tokens)
    protocol = (gross * 95 + 9_999) // 10_000
    creator = (gross * 30 + 9_999) // 10_000
    net_out = gross - protocol - creator
    return gross, net_out, protocol, creator


def test_fresh_curve_matches_pumpfun_virtual_reserves() -> None:
    curve = PaperCurve(FeeAssumptions())

    assert curve.virtual_token == _VT
    assert curve.virtual_quote == _VQ
    assert curve.real_token == 793_100_000_000_000
    assert curve.real_quote == 0
    assert curve.venue == "curve"


def test_dev_buy_matches_idl_formula_and_charges_1_25_percent() -> None:
    curve = PaperCurve(FeeAssumptions())
    spend = 100_000_000
    fill = curve.dev_buy(spend)
    tokens, net, protocol, creator = _idl_buy(spend, _VT, _VQ)

    assert fill.tokens_raw == tokens
    assert fill.net_quote_raw == net
    assert fill.protocol_fee_raw == protocol
    assert fill.creator_fee_raw == creator
    assert protocol + creator > 0
    assert fill.protocol_fee_raw + fill.creator_fee_raw + fill.net_quote_raw <= spend
    assert curve.real_quote == net
    assert curve.real_token == 793_100_000_000_000 - tokens
    assert curve.virtual_quote == _VQ + net
    assert curve.virtual_token == _VT - tokens


def test_sell_matches_idl_formula_and_moves_gross_sol_out_of_the_curve() -> None:
    curve = PaperCurve(FeeAssumptions())
    curve.dev_buy(100_000_000)
    qty = 1_000_000_000
    quote = curve.preview_sell(qty)
    gross, net_out, protocol, creator = _idl_sell(
        qty, curve.virtual_token, curve.virtual_quote
    )

    assert quote is not None
    assert quote.gross_raw == gross
    assert quote.net_raw == net_out
    assert quote.protocol_fee_raw == protocol
    assert quote.creator_fee_raw == creator
    assert quote.lp_fee_raw == 0
    assert quote.venue == "curve"
    real_before = curve.real_quote
    curve.apply_sell(qty)
    assert curve.real_quote == real_before - gross


def test_outside_net_buy_adds_that_sol_and_a_completing_buy_opens_pumpswap() -> None:
    curve = PaperCurve(FeeAssumptions())
    fill = curve.dev_buy(100_000_000)
    curve.apply_outside_net(is_buy=True, net_raw=1_000_000_000)
    assert curve.venue == "curve"
    assert curve.real_quote == fill.net_quote_raw + 1_000_000_000

    curve.apply_outside_net(is_buy=True, net_raw=90_000_000_000)
    assert curve.venue == "pumpswap"
    assert curve.real_token == 0
    assert curve.pool_seed == "continuous_price_assumption"
    assert curve.pool_quote > 0
    assert curve.pool_base > 0
    assert curve.pool_virtual_quote == 0


def test_post_graduation_sell_uses_pumpswap_fee_split_not_the_curve_split() -> None:
    fees = FeeAssumptions(
        pumpswap_lp_bps=5,
        pumpswap_protocol_bps=20,
        pumpswap_creator_bps=50,
    )
    curve = PaperCurve(fees)
    curve.dev_buy(100_000_000)
    curve.apply_outside_net(is_buy=True, net_raw=90_000_000_000)
    assert curve.venue == "pumpswap"

    quote = curve.preview_sell(1_000_000_000)
    assert quote is not None
    assert quote.venue == "pumpswap"
    gross = quote.gross_raw
    assert quote.lp_fee_raw == (gross * 5 + 9_999) // 10_000
    assert quote.protocol_fee_raw == (gross * 20 + 9_999) // 10_000
    assert quote.creator_fee_raw == (gross * 50 + 9_999) // 10_000
    assert quote.net_raw == gross - (
        quote.lp_fee_raw + quote.protocol_fee_raw + quote.creator_fee_raw
    )


def test_one_dollar_converts_at_the_supplied_sol_price() -> None:
    assert usd_to_lamports("1", "100") == 10_000_000
    assert usd_to_lamports("1", "101.47") == 9_855_130
    assert usd_to_lamports(Decimal("1"), Decimal("100")) == 10_000_000
