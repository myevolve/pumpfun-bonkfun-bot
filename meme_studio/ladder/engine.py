# ruff: noqa: TRY003
"""Deterministic paper replay of the stage-10 take-profit ladder."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from heapq import heappop, heappush
from typing import TYPE_CHECKING

from meme_studio.ladder.curve import PaperCurve, SellQuote
from meme_studio.ladder.money import bps_ratio, lamports_to_sol_str, usd_to_lamports

if TYPE_CHECKING:
    from meme_studio.ladder.plan import SellPlan

_LOG = logging.getLogger("meme_studio.ladder.paper")


@dataclass(frozen=True, slots=True)
class PathTrade:
    """One recorded trade. sol_amount_raw is the net quote-reserve delta."""

    ts: int
    user: str
    is_buy: bool
    sol_amount_raw: int
    creator_fee_raw: int = 0


@dataclass(frozen=True, slots=True)
class CoinPath:
    """A coin's recorded path. The real creator's trades are not our flow."""

    mint: str
    create_ts: int
    creator: str
    trades: tuple[PathTrade, ...]
    extra_observed_creator_fee_raw: int = 0


@dataclass(frozen=True, slots=True)
class DevBuyRecord:
    """The single create-transaction buy."""

    tokens_raw: int
    lamports_raw: int


@dataclass(frozen=True, slots=True)
class SimulatedSell:
    """One paper sell, including the link that would have been posted."""

    step: str
    ts: int
    tokens_raw: int
    gross_raw: int
    net_raw: int
    protocol_fee_raw: int
    creator_fee_raw: int
    lp_fee_raw: int
    tx_fee_lamports: int
    venue: str
    tx_link: str


@dataclass(slots=True)
class ReplayResult:
    """Realized paper result. An open runner is not marked as sold."""

    plan_sha256: str
    dev_buy: DevBuyRecord
    sells: list[SimulatedSell]
    announcements: list[dict[str, object]]
    cash_out_lamports: int
    tx_fee_lamports: int
    launch_cost_lamports: int
    own_creator_rebate_lamports: int
    observed_outside_creator_fee_lamports: int
    realized_pnl_lamports: int
    closeout_pnl_lamports: int
    held_raw: int
    reached_recover: bool
    graduated_on_our_curve: bool
    flags: list[str]


def replay(path: CoinPath, plan: SellPlan) -> ReplayResult:
    """Replay the published plan on a recorded path.

    Args:
        path: Trades in net-SOL units. Creator trades are dropped.
        plan: The hash-committed paper plan. Sells follow it and nothing else.

    Returns:
        Realized cash P&L and the announcement log. No transaction is built.
    """
    if plan.to_dict()["mode"] != "paper":
        raise ValueError("ladder replay is paper only")
    return _Session(path, plan).run()


@dataclass(slots=True)
class _Session:
    path: CoinPath
    plan: SellPlan
    curve: PaperCurve = field(init=False)
    held: int = 0
    tokens_bought: int = 0
    dev_lamports: int = 0
    dev_creator_fee: int = 0
    sells: list[SimulatedSell] = field(default_factory=list)
    announcements: list[dict[str, object]] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    fired: set[str] = field(default_factory=set)
    observed_fee: int = 0
    seq: int = 0
    anchor: tuple[int, int] | None = None
    trail_armed: bool = False
    trail_pending: bool = False
    last_runner_ts: int | None = None
    miss_locked: bool = False
    miss_parts: list[int] | None = None
    miss_done: set[int] = field(default_factory=set)
    miss_announced: set[int] = field(default_factory=set)
    recover_fired: bool = False
    reached_recover: bool = False
    replaced_dev: bool = False
    graduated: bool = False
    _heap: list[int] = field(default_factory=list)
    _seen: set[int] = field(default_factory=set)
    _trades: dict[int, list[PathTrade]] = field(default_factory=dict)

    def run(self) -> ReplayResult:
        self.curve = PaperCurve(self.plan.fees)
        self.flags.add("outside_flows_fixed_in_sol")
        self._group_trades()
        self._schedule(self.path.create_ts)
        horizon = self.path.create_ts + self.plan.thresholds.horizon_s
        for trade in self.path.trades:
            if trade.ts > horizon:
                self.flags.add("trade_after_horizon")
                continue
            self._schedule(trade.ts)
        for ann_ts, _exec_ts in self._miss_times():
            self._schedule(ann_ts)
            self._schedule(_exec_ts)
        while self._heap:
            ts = heappop(self._heap)
            self._on_tick(ts)
        return self._finish()

    def _group_trades(self) -> None:
        grouped: dict[int, list[PathTrade]] = {}
        for trade in self.path.trades:
            grouped.setdefault(trade.ts, []).append(trade)
        self._trades = grouped

    def _schedule(self, ts: int) -> None:
        if ts in self._seen:
            return
        self._seen.add(ts)
        heappush(self._heap, ts)

    def _on_tick(self, ts: int) -> None:
        if ts == self.path.create_ts and self.tokens_bought == 0:
            self._do_dev_buy()
        before = self.curve.venue
        self._apply_trades(ts)
        if before == "curve" and self.curve.venue == "pumpswap":
            self.graduated = True
            self.flags.add("post_graduation_path_replayed")
        if not self.miss_locked:
            self._fire_due(ts)
            self._update_trail(ts)
        self._handle_miss(ts)

    def _do_dev_buy(self) -> None:
        fill = self.curve.dev_buy(self.plan.dev_buy_lamports)
        self.held = fill.tokens_raw
        self.tokens_bought = fill.tokens_raw
        self.dev_lamports = fill.gross_raw
        self.dev_creator_fee = fill.creator_fee_raw

    def _apply_trades(self, ts: int) -> None:
        horizon = self.path.create_ts + self.plan.thresholds.horizon_s
        for trade in self._trades.get(ts, []):
            if trade.ts > horizon:
                continue
            if trade.user == self.path.creator:
                self.replaced_dev = True
                continue
            self.curve.apply_outside_net(
                is_buy=trade.is_buy,
                net_raw=trade.sol_amount_raw,
            )
            self.observed_fee += trade.creator_fee_raw

    def _fire_due(self, ts: int) -> None:
        steps = (
            ("R", self.plan.thresholds.recover_multiple),
            ("T1", self.plan.thresholds.t1_multiple),
            ("T2", self.plan.thresholds.t2_multiple),
            ("T3", self.plan.thresholds.t3_multiple),
        )
        for step, multiple in steps:
            if step in self.fired:
                continue
            graduated_trigger = step == "T3" and self.curve.venue == "pumpswap"
            if not graduated_trigger and not self._multiple_reached(multiple):
                break
            if step == "R":
                sold = self._sell_recover(ts)
            else:
                sold = self._sell_fraction(ts, step)
            if not sold:
                break
            self.fired.add(step)
            if step == "R":
                self.recover_fired = True
            if step == "T3":
                self._arm_trail()

    def _multiple_reached(self, multiple: str) -> bool:
        if self.tokens_bought <= 0:
            return False
        num, den = bps_ratio(multiple)
        quote_raw, token_raw = self.curve.price_pair()
        if token_raw <= 0:
            return False
        return (
            quote_raw * self.tokens_bought * den >= num * self.dev_lamports * token_raw
        )

    def _sell_recover(self, ts: int) -> bool:
        target = self.dev_lamports + self._launch_cost() + self._tx_fee()
        qty = self._qty_for_target(target)
        if qty <= 0:
            self.flags.add("slippage_blocked")
            return False
        quote = self.curve.apply_sell(qty)
        self._record(ts, "R", quote)
        self.reached_recover = quote.net_raw + quote.creator_fee_raw >= target
        if not self.reached_recover:
            self.flags.add("recover_shortfall")
        return True

    def _sell_fraction(self, ts: int, step: str) -> bool:
        num, den = bps_ratio(self.plan.thresholds.remaining_fraction)
        desired = self.held * num // den
        qty = self._max_under_slippage(desired)
        if qty <= 0:
            self.flags.add("slippage_blocked")
            return False
        quote = self.curve.apply_sell(qty)
        self._record(ts, step, quote)
        return True

    def _qty_for_target(self, target: int) -> int:
        capped = self._max_under_slippage(self.held)
        if capped <= 0:
            return 0
        if self._proceeds(capped) < target:
            return capped
        lo = 1
        hi = capped
        while lo < hi:
            mid = (lo + hi) // 2
            if self._proceeds(mid) >= target:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _proceeds(self, qty: int) -> int:
        preview = self.curve.preview_sell(qty)
        if preview is None:
            return -1
        return preview.net_raw + preview.creator_fee_raw

    def _max_under_slippage(self, desired: int) -> int:
        if desired <= 0:
            return 0
        if self._impact_ok(desired):
            return desired
        lo = 0
        hi = desired
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self._impact_ok(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _impact_ok(self, qty: int) -> bool:
        if qty <= 0:
            return True
        preview = self.curve.preview_sell(qty)
        if preview is None:
            return False
        spot = self.curve.spot_gross(qty)
        if spot <= 0:
            return False
        num, den = bps_ratio(self.plan.thresholds.slippage_cap)
        if num == 0:
            # Any positive size moves the constant product. Integer spot can
            # hide that, so a zero cap rejects every sell.
            return False
        return (spot - preview.gross_raw) * den <= spot * num

    def _arm_trail(self) -> None:
        self.anchor = self.curve.price_pair()
        self.trail_armed = True
        self.trail_pending = False

    def _update_trail(self, ts: int) -> None:
        if "T3" not in self.fired or self.anchor is None:
            return
        quote_raw, token_raw = self.curve.price_pair()
        anchor_quote, anchor_token = self.anchor
        if token_raw <= 0 or anchor_token <= 0:
            return
        if quote_raw * anchor_token > anchor_quote * token_raw:
            self.anchor = (quote_raw, token_raw)
            self.trail_armed = True
            self.trail_pending = False
            return
        draw_num, draw_den = bps_ratio(self.plan.thresholds.trail_drawdown)
        # Price at or below (1 - drawdown) of the anchor.
        kept_num = draw_den - draw_num
        if quote_raw * anchor_token * draw_den <= anchor_quote * token_raw * kept_num:
            self.trail_pending = True
        self._maybe_trail(ts)

    def _maybe_trail(self, ts: int) -> None:
        if not self.trail_pending or not self.trail_armed:
            return
        interval = self.plan.thresholds.trail_min_interval_s
        if self.last_runner_ts is not None and ts < self.last_runner_ts + interval:
            self._schedule(self.last_runner_ts + interval)
            return
        if self._sell_fraction(ts, "runner"):
            self.last_runner_ts = ts
            self.trail_armed = False
            self.trail_pending = False

    def _miss_times(self) -> list[tuple[int, int]]:
        thresholds = self.plan.thresholds
        start = self.path.create_ts + thresholds.miss_after_s
        times: list[tuple[int, int]] = []
        for index in range(thresholds.miss_tranches):
            execute_ts = start + index * thresholds.miss_spacing_s
            times.append((execute_ts - thresholds.miss_preannounce_s, execute_ts))
        return times

    def _handle_miss(self, ts: int) -> None:
        if self.recover_fired:
            if self.miss_announced and "miss_cancelled" not in self.flags:
                self.flags.add("miss_cancelled")
                self._announce(
                    _Note(
                        "CANCEL",
                        "miss",
                        ts,
                        0,
                        0,
                        "none",
                        f"paper://{self.path.mint}/miss/cancel",
                    )
                )
            return
        times = self._miss_times()
        deadline = self.path.create_ts + self.plan.thresholds.miss_after_s
        if ts >= deadline:
            self.miss_locked = True
            if self.miss_parts is None:
                self.miss_parts = _equal_parts(
                    self.held,
                    self.plan.thresholds.miss_tranches,
                )
        for index, (ann_ts, exec_ts) in enumerate(times):
            if (
                ts == ann_ts
                and index not in self.miss_announced
                and (index == 0 or self.miss_locked)
            ):
                self.miss_announced.add(index)
                self._announce(
                    _Note(
                        "PREANNOUNCE",
                        f"miss-{index + 1}",
                        ts,
                        0,
                        0,
                        "none",
                        f"paper://{self.path.mint}/miss-{index + 1}/pre",
                    )
                )
            if self.miss_locked and ts == exec_ts and index not in self.miss_done:
                self._execute_miss(ts, index)

    def _execute_miss(self, ts: int, index: int) -> None:
        self.miss_done.add(index)
        parts = self.miss_parts or []
        desired = parts[index] if index < len(parts) else 0
        qty = min(desired, self.held)
        qty = self._max_under_slippage(qty)
        if qty <= 0:
            self.flags.add("slippage_blocked")
            return
        quote = self.curve.apply_sell(qty)
        self._record(ts, f"miss-{index + 1}", quote)

    def _record(self, ts: int, step: str, quote: SellQuote) -> None:
        self.held -= quote.tokens_raw
        self.seq += 1
        link = f"paper://{self.path.mint}/{step}/{self.seq}"
        tx_fee = self._tx_fee()
        sell = SimulatedSell(
            step=step,
            ts=ts,
            tokens_raw=quote.tokens_raw,
            gross_raw=quote.gross_raw,
            net_raw=quote.net_raw,
            protocol_fee_raw=quote.protocol_fee_raw,
            creator_fee_raw=quote.creator_fee_raw,
            lp_fee_raw=quote.lp_fee_raw,
            tx_fee_lamports=tx_fee,
            venue=quote.venue,
            tx_link=link,
        )
        self.sells.append(sell)
        self._announce(
            _Note(
                "SELL",
                step,
                ts,
                quote.tokens_raw,
                quote.net_raw,
                quote.venue,
                link,
            )
        )

    def _announce(self, note: _Note) -> None:
        sol_text = lamports_to_sol_str(note.sol_net)
        text = (
            f"PAPER {note.kind} step={note.step} mint={self.path.mint} ts={note.ts} "
            f"tokens_raw={note.tokens_raw} sol_net={sol_text} venue={note.venue} "
            f"plan_sha256={self.plan.sha256} tx={note.link}"
        )
        row: dict[str, object] = {
            "kind": note.kind,
            "step": note.step,
            "ts": note.ts,
            "plan_sha256": self.plan.sha256,
            "tx": note.link,
            "text": text,
        }
        if note.kind == "SELL":
            row["within_seconds"] = 60
        self.announcements.append(row)
        _LOG.info(text)

    def _tx_fee(self) -> int:
        fees = self.plan.fees
        priority = (
            fees.sell_compute_unit_limit * fees.priority_fee_microlamports // 1_000_000
        )
        return fees.network_fee_lamports + priority

    def _launch_cost(self) -> int:
        return usd_to_lamports(
            self.plan.thresholds.launch_cost_usd,
            self.plan.sol_usd,
        )

    def _finish(self) -> ReplayResult:
        if self.replaced_dev:
            self.flags.add("replaced_real_dev")
        observed = self.observed_fee + self.path.extra_observed_creator_fee_raw
        cash = sum(sell.net_raw for sell in self.sells)
        tx_fees = sum(sell.tx_fee_lamports for sell in self.sells)
        rebate = self.dev_creator_fee + sum(sell.creator_fee_raw for sell in self.sells)
        launch = self._launch_cost()
        realized = cash - self.dev_lamports - tx_fees - launch + rebate + observed
        closeout = realized
        if self.held > 0:
            self.flags.add("closeout_is_mark_not_plan")
            if self.reached_recover:
                self.flags.add("open_runner")
            preview = self.curve.preview_sell(self.held)
            if preview is not None:
                closeout = (
                    realized
                    + preview.net_raw
                    + preview.creator_fee_raw
                    - self._tx_fee()
                )
        return ReplayResult(
            plan_sha256=self.plan.sha256,
            dev_buy=DevBuyRecord(self.tokens_bought, self.dev_lamports),
            sells=self.sells,
            announcements=self.announcements,
            cash_out_lamports=cash,
            tx_fee_lamports=tx_fees,
            launch_cost_lamports=launch,
            own_creator_rebate_lamports=rebate,
            observed_outside_creator_fee_lamports=observed,
            realized_pnl_lamports=realized,
            closeout_pnl_lamports=closeout,
            held_raw=self.held,
            reached_recover=self.reached_recover,
            graduated_on_our_curve=self.graduated,
            flags=sorted(self.flags),
        )


@dataclass(frozen=True, slots=True)
class _Note:
    kind: str
    step: str
    ts: int
    tokens_raw: int
    sol_net: int
    venue: str
    link: str


def _equal_parts(total: int, parts: int) -> list[int]:
    if parts <= 0:
        return []
    base = total // parts if total > 0 else 0
    amounts = [base] * (parts - 1)
    amounts.append(total - base * (parts - 1))
    return amounts
