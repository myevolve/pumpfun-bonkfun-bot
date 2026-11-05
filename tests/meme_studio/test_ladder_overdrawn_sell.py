# ruff: noqa: S101, PLR2004
"""An outside curve sell larger than the reserves is unappliable, never capped."""

from __future__ import annotations

import pytest

from meme_studio.ladder import FeeAssumptions, PaperCurve
from meme_studio.ladder.curve import UnappliableFlowError


def test_overdrawn_curve_sell_raises_and_leaves_state_untouched() -> None:
    curve = PaperCurve(FeeAssumptions())
    curve.dev_buy(100_000_000)
    curve.apply_outside_net(is_buy=True, net_raw=1_000_000_000)
    before = (curve.virtual_token, curve.virtual_quote, curve.real_token, curve.real_quote)

    with pytest.raises(UnappliableFlowError):
        curve.apply_outside_net(is_buy=False, net_raw=2_000_000_000)

    assert (curve.virtual_token, curve.virtual_quote, curve.real_token, curve.real_quote) == before
