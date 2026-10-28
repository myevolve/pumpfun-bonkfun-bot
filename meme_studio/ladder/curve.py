# ruff: noqa: TRY003
"""Paper pump.fun curve and PumpSwap pool.

Our own trades call the repo quote functions. Outside flow is applied as a
net reserve delta, which is the shadow-study approximation: recorded SOL
does not get to see our dev buy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT
from meme_studio.ladder.money import LAMPORTS_PER_SOL
from meme_studio.ladder.plan import validate_fee_assumptions
from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFees,
    PumpFeeSnapshot,
    PumpFeeTier,
    quote_buy_exact_in,
    quote_buy_exact_out,
    quote_sell_exact_in,
)
from platforms.pumpfun.pumpswap import quote_pumpswap_sell

if TYPE_CHECKING:
    from meme_studio.ladder.plan import FeeAssumptions

VIRTUAL_TOKEN = 1_073_000_000_000_000
VIRTUAL_QUOTE = 30 * LAMPORTS_PER_SOL
REAL_TOKEN = 793_100_000_000_000
TOKEN_SUPPLY = 1_000_000_000_000_000
_BPS = 10_000
# All-zero is Pubkey.default(), and the quote engine waives the creator fee for it.
_CREATOR = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")


@dataclass(frozen=True, slots=True)
class DevFill:
    """Tokens received for the disclosed create-transaction dev buy."""

    tokens_raw: int
    net_quote_raw: int
    protocol_fee_raw: int
    creator_fee_raw: int
    gross_raw: int


@dataclass(frozen=True, slots=True)
class SellQuote:
    """Fee-split proceeds for selling a token quantity. Does not move state."""

    tokens_raw: int
    gross_raw: int
    net_raw: int
    protocol_fee_raw: int
    creator_fee_raw: int
    lp_fee_raw: int
    venue: str


def _ceil_fee(amount: int, bps: int) -> int:
    if bps <= 0 or amount <= 0:
        return 0
    return (amount * bps + _BPS - 1) // _BPS


def _snapshot(lp_bps: int, protocol_bps: int, creator_bps: int) -> PumpFeeSnapshot:
    fees = PumpFees(lp_bps, protocol_bps, creator_bps)
    tier = PumpFeeTier(0, fees)
    config = PumpFeeConfig(
        bump=255,
        admin=_CREATOR,
        flat_fees=fees,
        regular_tiers=(tier,),
        stable_tiers=(tier,),
        exotic_flat_fees=fees,
        digest="paper-ladder",
    )
    return PumpFeeSnapshot(config=config, observed_at=0.0, attested_at=0.0)


class PaperCurve:
    """Integer bonding curve that migrates into a paper PumpSwap pool."""

    def __init__(self, fees: FeeAssumptions) -> None:
        """Start from pump.fun's initial virtual reserves.

        Args:
            fees: Curve and PumpSwap fee assumptions. Curve LP must stay 0,
                matching the repo quote engine.
        """
        validate_fee_assumptions(fees)
        if fees.curve_lp_bps != 0:
            raise ValueError("curve LP fee must be 0")
        self.fees = fees
        self._swap_creator_unresolved = fees.pumpswap_creator_bps is None
        self.virtual_token = VIRTUAL_TOKEN
        self.virtual_quote = VIRTUAL_QUOTE
        self.real_token = REAL_TOKEN
        self.real_quote = 0
        self.venue = "curve"
        self.pool_base = 0
        self.pool_quote = 0
        self.pool_virtual_quote = 0
        self.pool_seed = ""
        self._curve_fees = _snapshot(
            fees.curve_lp_bps,
            fees.curve_protocol_bps,
            fees.curve_creator_bps,
        )
        swap_creator = (
            0 if fees.pumpswap_creator_bps is None else fees.pumpswap_creator_bps
        )
        self._swap_fees = _snapshot(
            fees.pumpswap_lp_bps,
            fees.pumpswap_protocol_bps,
            swap_creator,
        )

    def dev_buy(self, spendable_raw: int) -> DevFill:
        """Buy with a gross SOL budget, charging the 1.25% curve fee."""
        if self.venue != "curve":
            raise ValueError("dev buy is only the create-transaction buy")
        if spendable_raw <= 0:
            raise ValueError("dev buy must be positive")
        quote = quote_buy_exact_in(self._curve_state(), spendable_raw, self._curve_fees)
        self.virtual_token -= quote.amount_out_raw
        self.virtual_quote += quote.net_quote_raw
        self.real_token -= quote.amount_out_raw
        self.real_quote += quote.net_quote_raw
        return DevFill(
            tokens_raw=quote.amount_out_raw,
            net_quote_raw=quote.net_quote_raw,
            protocol_fee_raw=quote.protocol_fee_raw,
            creator_fee_raw=quote.creator_fee_raw,
            gross_raw=quote.gross_quote_raw,
        )

    def preview_sell(self, tokens_raw: int) -> SellQuote | None:
        """Quote a sell. None means the venue cannot pay that size."""
        if tokens_raw <= 0:
            return None
        if self.venue == "pumpswap":
            if self._swap_creator_unresolved:
                raise ValueError("pumpswap creator fee is UNKNOWN")
            return self._preview_pool_sell(tokens_raw)
        try:
            quote = quote_sell_exact_in(
                self._curve_state(),
                tokens_raw,
                self._curve_fees,
            )
        except ValueError:
            return None
        return SellQuote(
            tokens_raw=tokens_raw,
            gross_raw=quote.gross_quote_raw,
            net_raw=quote.net_quote_raw,
            protocol_fee_raw=quote.protocol_fee_raw,
            creator_fee_raw=quote.creator_fee_raw,
            lp_fee_raw=0,
            venue="curve",
        )

    def apply_sell(self, tokens_raw: int) -> SellQuote:
        """Sell tokens and move reserves by the gross quote out."""
        quote = self.preview_sell(tokens_raw)
        if quote is None:
            raise ValueError("sell size is not payable")
        if self.venue == "curve":
            self.virtual_token += tokens_raw
            self.virtual_quote -= quote.gross_raw
            self.real_token += tokens_raw
            self.real_quote -= quote.gross_raw
            return quote
        self.pool_base += tokens_raw
        self.pool_quote -= quote.gross_raw - quote.lp_fee_raw
        return quote

    def apply_outside_net(self, *, is_buy: bool, net_raw: int) -> None:
        """Apply a recorded net reserve delta from a non-creator trade.

        Args:
            is_buy: True when the recorded trade added SOL to the venue.
            net_raw: Lamports added to real quote on a buy, or removed on a sell.
        """
        if net_raw <= 0:
            raise ValueError("outside flow must be positive")
        if self.venue == "pumpswap":
            self._apply_pool_net(is_buy=is_buy, net_raw=net_raw)
            return
        if is_buy:
            self._apply_curve_net_buy(net_raw)
            return
        self._apply_curve_net_sell(net_raw)

    def price_pair(self) -> tuple[int, int]:
        """Return (quote_raw, token_raw) for the current spot price."""
        if self.venue == "pumpswap":
            return self.pool_quote + self.pool_virtual_quote, self.pool_base
        return self.virtual_quote, self.virtual_token

    def spot_gross(self, tokens_raw: int) -> int:
        """Gross quote at the current spot, with no price impact."""
        quote_raw, token_raw = self.price_pair()
        if token_raw <= 0:
            return 0
        return tokens_raw * quote_raw // token_raw

    def _curve_state(self) -> dict[str, object]:
        return {
            "virtual_token_reserves": self.virtual_token,
            "virtual_quote_reserves": self.virtual_quote,
            "real_token_reserves": self.real_token,
            "real_quote_reserves": self.real_quote,
            "token_total_supply": TOKEN_SUPPLY,
            "complete": False,
            "creator": _CREATOR,
            "quote_mint": WSOL_MINT,
        }

    def _net_to_complete(self) -> int | None:
        if self.real_token <= 0:
            return 0
        try:
            quote = quote_buy_exact_out(
                self._curve_state(),
                self.real_token,
                self._curve_fees,
            )
        except ValueError:
            return None
        return quote.net_quote_raw

    def _apply_curve_net_buy(self, net_raw: int) -> None:
        needed = self._net_to_complete()
        if needed is not None and net_raw >= needed:
            self._complete_with(needed)
            leftover = net_raw - needed
            if leftover > 0:
                self._apply_pool_net(is_buy=True, net_raw=leftover)
            return
        effective = net_raw - 1
        tokens = (
            0
            if effective <= 0
            else effective * self.virtual_token // (self.virtual_quote + effective)
        )
        if tokens >= self.real_token and self.real_token > 0:
            self._complete_with(needed or net_raw)
            return
        self.virtual_token -= tokens
        self.virtual_quote += net_raw
        self.real_token -= tokens
        self.real_quote += net_raw

    def _complete_with(self, net_raw: int) -> None:
        tokens = self.real_token
        self.virtual_token -= tokens
        self.virtual_quote += net_raw
        self.real_token = 0
        self.real_quote += net_raw
        self._open_pool()

    def _apply_curve_net_sell(self, net_raw: int) -> None:
        capped = min(net_raw, self.real_quote, self.virtual_quote - 1)
        if capped <= 0:
            return
        tokens = capped * self.virtual_token // (self.virtual_quote - capped)
        self.virtual_token += tokens
        self.virtual_quote -= capped
        self.real_token += tokens
        self.real_quote -= capped

    def _open_pool(self) -> None:
        quote = self.real_quote
        base = (
            quote * self.virtual_token // self.virtual_quote
            if self.virtual_quote
            else 1
        )
        if base <= 0:
            base = 1
        self.pool_quote = quote
        self.pool_base = base
        self.pool_virtual_quote = 0
        self.pool_seed = "continuous_price_assumption"
        self.venue = "pumpswap"

    def _apply_pool_net(self, *, is_buy: bool, net_raw: int) -> None:
        effective = self.pool_quote + self.pool_virtual_quote
        if is_buy:
            if self.pool_base <= 1:
                self.pool_quote += net_raw
                return
            tokens = net_raw * self.pool_base // (effective + net_raw)
            tokens = min(tokens, self.pool_base - 1)
            self.pool_base -= tokens
            self.pool_quote += net_raw
            return
        tokens = self._tokens_for_quote_decrease(net_raw)
        left = self._quote_decrease_for_tokens(tokens)
        if tokens <= 0 or left is None:
            return
        self.pool_base += tokens
        self.pool_quote -= left

    def _quote_decrease_for_tokens(self, tokens_raw: int) -> int | None:
        effective = self.pool_quote + self.pool_virtual_quote
        if tokens_raw <= 0 or self.pool_base <= 0 or effective <= 0:
            return None
        gross = effective * tokens_raw // (self.pool_base + tokens_raw)
        lp_fee = _ceil_fee(gross, self.fees.pumpswap_lp_bps)
        decrease = gross - lp_fee
        if decrease <= 0 or self.pool_quote < decrease:
            return None
        return decrease

    def _tokens_for_quote_decrease(self, decrease: int) -> int:
        """Invert a pool quote drop back to tokens.

        An exact own-sell decrease maps back to that sell's token size.
        If the observed drop falls between two sizes, the smaller size is
        used so the pool does not lose more quote than was observed.
        """
        if decrease <= 0 or self.pool_base <= 1:
            return 0
        lo = 0
        hi = self.pool_base - 1
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            got = 0 if mid == 0 else self._quote_decrease_for_tokens(mid)
            if got is None:
                hi = mid - 1
                continue
            if got == decrease:
                return mid
            if got < decrease:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _preview_pool_sell(self, tokens_raw: int) -> SellQuote | None:
        effective = self.pool_quote + self.pool_virtual_quote
        if self.pool_base <= 0 or effective <= 0:
            return None
        gross = effective * tokens_raw // (self.pool_base + tokens_raw)
        lp_fee = _ceil_fee(gross, self.fees.pumpswap_lp_bps)
        protocol_fee = _ceil_fee(gross, self.fees.pumpswap_protocol_bps)
        creator_fee = _ceil_fee(gross, self.fees.pumpswap_creator_bps)
        net = gross - lp_fee - protocol_fee - creator_fee
        if net <= 0 or self.pool_quote < gross - lp_fee:
            return None
        try:
            repo_net = quote_pumpswap_sell(
                base_reserve_raw=self.pool_base,
                quote_reserve_raw=self.pool_quote,
                virtual_quote_reserve_raw=self.pool_virtual_quote,
                base_supply_raw=TOKEN_SUPPLY,
                base_amount_in_raw=tokens_raw,
                quote_mint=WSOL_MINT,
                coin_creator=_CREATOR,
                fee_snapshot=self._swap_fees,
            )
        except ValueError:
            return None
        if repo_net != net:
            raise ValueError("PumpSwap quote diverged from the repo fee math")
        return SellQuote(
            tokens_raw=tokens_raw,
            gross_raw=gross,
            net_raw=net,
            protocol_fee_raw=protocol_fee,
            creator_fee_raw=creator_fee,
            lp_fee_raw=lp_fee,
            venue="pumpswap",
        )
