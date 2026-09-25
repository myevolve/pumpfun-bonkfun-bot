"""Live cycle discovery from the Geyser trade stream (phase 2 of the executor).

Protocol
--------
The token listener already decodes every pump.fun trade into ``TradeEvent``s
and ``TradeFlowHub`` fans them out to per-mint queues. ``CycleDiscovery``
subscribes one queue per tracked mint (held or newly created coins), keeps a
bounded per-curve buffer, and consumes it with a per-mint task — purely
event-driven, no polling.

Once a curve's real SOL reserves cross ``target_sol_lamports``, both
directions of the two-leg cycle are quoted from the latest observed virtual
reserves and the caller-supplied ``core.cycles.pool.Pool`` snapshots:

    buy SOL -> mint on the bonding curve, sell mint -> SOL on the pool, or
    buy SOL -> mint on the pool, sell mint -> SOL on the curve.

The better direction wins. A candidate is emitted only when its expected
output clears ``buy_amount_lamports + min_profit_lamports`` and its
``(buy_leg, sell_leg, amount)`` key has not been emitted within
``dedup_window_slots``. ``discover()`` drains pending candidates for the
executor, which must re-attest every account and re-quote before signing:
discovery works on possibly stale snapshots and is a signal, never
authorization to trade.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from core.cycles.core import AMM, SOL, CycleError
from core.cycles.pool import Pool
from monitoring.trade_flow import TradeEvent, TradeFlowHub
from utils.logger import get_logger

logger = get_logger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_BPS = 10_000
_EMITTED_PRUNE = 4096


@dataclass(frozen=True, slots=True)
class CycleLeg:
    """One executable leg: venue address, program, and raw in/out amounts."""

    venue: str
    program: str
    input_mint: str
    output_mint: str
    amount_in: int
    amount_out: int


@dataclass(frozen=True, slots=True)
class CycleCandidate:
    """A profitable two-leg cycle proposed by discovery for executor review."""

    mints: tuple[str, ...]
    pools: tuple[str, ...]
    programs: tuple[str, ...]
    buy_leg: CycleLeg
    sell_leg: CycleLeg
    expected_out_raw: int
    created_slot: int


def curve_buy_out(
    virtual_sol_reserves: int, virtual_token_reserves: int, amount: int, fee_bps: int
) -> int:
    """Whole tokens out of the bonding curve for ``amount`` lamports in."""
    net = amount * (_BPS - fee_bps) // _BPS
    return virtual_token_reserves * net // (virtual_sol_reserves + net)


def curve_sell_out(
    virtual_sol_reserves: int, virtual_token_reserves: int, tokens: int, fee_bps: int
) -> int:
    """Lamports out of the bonding curve for ``tokens`` raw units in."""
    net = tokens * (_BPS - fee_bps) // _BPS
    return virtual_sol_reserves * net // (virtual_token_reserves + net)


class CycleDiscovery:
    """Event-driven cycle discovery over TradeFlowHub per-mint queues.

    Containment contract: ``ingest`` and every consumer task swallow their
    own errors (logged, counted in ``errors``); nothing ever propagates to
    the hub, the listener, or other subscribers.
    """

    def __init__(  # noqa: PLR0913 - config knobs mirror the phase rules
        self,
        hub: TradeFlowHub,
        *,
        target_sol_lamports: int,
        buy_amount_lamports: int,
        min_profit_lamports: int = 0,
        fee_bps: int = 100,
        dedup_window_slots: int = 20,
        buffer_size: int = 64,
    ) -> None:
        if target_sol_lamports <= 0 or buy_amount_lamports <= 0:
            raise ValueError(  # noqa: TRY003
                "target_sol_lamports and buy_amount_lamports must be > 0"
            )
        if min_profit_lamports < 0:
            raise ValueError("min_profit_lamports must be >= 0")  # noqa: TRY003
        if not 0 <= fee_bps < _BPS:
            raise ValueError("fee_bps must be in [0, 10000)")  # noqa: TRY003
        if dedup_window_slots <= 0 or buffer_size <= 0:
            raise ValueError(  # noqa: TRY003
                "dedup_window_slots and buffer_size must be > 0"
            )
        self.hub = hub
        self.target_sol_lamports = target_sol_lamports
        self.buy_amount_lamports = buy_amount_lamports
        self.min_profit_lamports = min_profit_lamports
        self.fee_bps = fee_bps
        self.dedup_window_slots = dedup_window_slots
        self.errors = 0
        self._buffer_size = buffer_size
        self._buffer: dict[str, deque[TradeEvent]] = {}
        self._pools: dict[str, dict[str, Pool]] = {}
        self._curves: dict[str, tuple[str, str]] = {}
        self._queues: dict[str, asyncio.Queue[TradeEvent]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._candidates: list[CycleCandidate] = []
        self._emitted: dict[tuple[CycleLeg, CycleLeg, int], int] = {}

    def track(
        self, mint: str, bonding_curve: str, *, program: str = PUMP_PROGRAM
    ) -> asyncio.Queue[TradeEvent]:
        """Subscribe to a mint's trades and start its consumer task.

        Requires a running event loop. Returns the hub queue so callers can
        await events directly.
        """
        if mint in self._tasks:
            return self._queues[mint]
        self._curves[mint] = (bonding_curve, program)
        self._buffer[mint] = deque(maxlen=self._buffer_size)
        queue = self.hub.subscribe(mint)
        self._queues[mint] = queue
        self._tasks[mint] = asyncio.create_task(self._consume(mint, queue))
        return queue

    def untrack(self, mint: str) -> None:
        """Stop tracking a mint: cancel its task and leave the hub."""
        task = self._tasks.pop(mint, None)
        if task is not None:
            task.cancel()
        queue = self._queues.pop(mint, None)
        if queue is not None:
            self.hub.unsubscribe(mint, queue)
        self._buffer.pop(mint, None)
        self._curves.pop(mint, None)
        self._pools.pop(mint, None)

    def update_pool(self, mint: str, pool: Pool) -> Pool:
        """Record the latest hydrated external-pool snapshot for a mint."""
        pools = self._pools.setdefault(mint, {})
        pools[pool.address] = pool
        return pool

    async def stop(self) -> None:
        """Cancel and await every consumer task."""
        tasks = [self._tasks.pop(mint) for mint in list(self._tasks)]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def discover(self) -> list[CycleCandidate]:
        """Drain pending candidates; dedup state survives the drain."""
        pending, self._candidates = self._candidates, []
        return pending

    def ingest(self, event: TradeEvent) -> None:
        """Evaluate one trade event; contained — never raises."""
        try:
            self._on_event(event)
        except Exception:  # containment is the contract
            self.errors += 1
            logger.exception("cycle discovery failed on %s", event.signature)

    async def _consume(self, _mint: str, queue: asyncio.Queue[TradeEvent]) -> None:
        """Per-mint consumer: hub queue -> ingest, event-driven, no polling."""
        while True:
            event = await queue.get()
            self.ingest(event)

    def _on_event(self, event: TradeEvent) -> None:
        mint = event.mint
        buffer = self._buffer.get(mint)
        if buffer is None:
            return
        buffer.append(event)
        curve = self._curves.get(mint)
        pools = self._pools.get(mint)
        if curve is None or not pools:
            return
        if event.real_sol_reserves < self.target_sol_lamports:
            return
        best = self._best_cycle(mint, curve, list(pools.values()), event)
        if best is None:
            return
        candidate, key = best
        if candidate.expected_out_raw <= (
            self.buy_amount_lamports + self.min_profit_lamports
        ):
            return
        seen = self._emitted.get(key)
        if seen is not None and event.slot - seen < self.dedup_window_slots:
            return
        if len(self._emitted) >= _EMITTED_PRUNE:
            # ponytail: threshold prune, not per-insert; the live window is tiny
            cutoff = event.slot - self.dedup_window_slots
            self._emitted = {k: s for k, s in self._emitted.items() if s >= cutoff}
        self._emitted[key] = event.slot
        self._candidates.append(candidate)

    def _best_cycle(
        self,
        mint: str,
        curve: tuple[str, str],
        pools: list[Pool],
        event: TradeEvent,
    ) -> tuple[CycleCandidate, tuple[CycleLeg, CycleLeg, int]] | None:
        """Quote both cycle directions against every pool; return the best."""
        curve_addr, curve_program = curve
        amount = self.buy_amount_lamports
        vsol = event.virtual_sol_reserves
        vtoken = event.virtual_token_reserves
        best: tuple[CycleCandidate, tuple[CycleLeg, CycleLeg, int]] | None = None
        for pool in pools:
            try:
                tokens = curve_buy_out(vsol, vtoken, amount, self.fee_bps)
                out_curve_first = pool.quote(mint, tokens) if tokens > 0 else 0
                pool_tokens = pool.quote(SOL, amount)
                out_pool_first = curve_sell_out(vsol, vtoken, pool_tokens, self.fee_bps)
            except CycleError:
                continue  # stale or empty pool snapshot; try the next one
            for buy, sell, out in (
                (
                    CycleLeg(curve_addr, curve_program, SOL, mint, amount, tokens),
                    CycleLeg(
                        pool.address, pool.program, mint, SOL, tokens, out_curve_first
                    ),
                    out_curve_first,
                ),
                (
                    CycleLeg(
                        pool.address, pool.program, SOL, mint, amount, pool_tokens
                    ),
                    CycleLeg(
                        curve_addr,
                        curve_program,
                        mint,
                        SOL,
                        pool_tokens,
                        out_pool_first,
                    ),
                    out_pool_first,
                ),
            ):
                if best is None or out > best[0].expected_out_raw:
                    candidate = CycleCandidate(
                        mints=(mint,),
                        pools=(curve_addr, pool.address),
                        programs=(curve_program, pool.program),
                        buy_leg=buy,
                        sell_leg=sell,
                        expected_out_raw=out,
                        created_slot=event.slot,
                    )
                    best = (candidate, (buy, sell, amount))
        return best


def self_check() -> bool:  # noqa: PLR0915 - one linear scenario reads best
    """Synthetic end-to-end check: detection, profit gate, dedup, containment."""
    mint = "DiscMint11111111111111111111111111111111111"
    curve = "DiscCurve111111111111111111111111111111111"
    good_pool_addr = "GoodPool1111111111111111111111111111111111"
    bad_pool_addr = "BadPool1111111111111111111111111111111111"
    empty_pool_addr = "EmptyPool11111111111111111111111111111111"
    vsol = 85_000_000_000
    vtoken = 100_000_000_000_000
    target = 10_000_000_000
    amount = 1_000_000_000
    slot_a = 11
    slot_dedup = 12
    slot_rearm = 100
    slot_queue = 200

    def event(slot: int, real_sol: int) -> TradeEvent:
        return TradeEvent(
            mint=mint,
            user="user",
            creator="creator",
            is_buy=True,
            sol_amount=amount,
            token_amount=1_151_000_000_000,
            virtual_sol_reserves=vsol,
            virtual_token_reserves=vtoken,
            real_sol_reserves=real_sol,
            real_token_reserves=10_000_000_000_000,
            slot=slot,
            signature=f"sig{slot}",
            timestamp=0,
        )

    def pool(address: str, reserves: tuple[int, int]) -> Pool:
        return Pool(
            address=address,
            program=AMM,
            mints=(SOL, mint),
            vaults=("vault_a", "vault_b"),
            reserves=reserves,
            trade_rate=2500,
        )

    async def scenario() -> None:
        hub = TradeFlowHub(None)  # type: ignore[arg-type]  # publish path unused
        disc = CycleDiscovery(
            hub, target_sol_lamports=target, buy_amount_lamports=amount
        )
        disc.update_pool(
            mint, pool(good_pool_addr, (1_000_000_000_000, 200_000_000_000_000))
        )
        queue = disc.track(mint, curve)
        assert hub.active  # noqa: S101

        # Below the SOL target: no evaluation, no candidate.
        disc.ingest(event(10, target // 2))
        assert disc.discover() == []  # noqa: S101

        # At the target with a profitable pool: one candidate, right shape.
        disc.ingest(event(slot_a, vsol))
        found = disc.discover()
        assert len(found) == 1  # noqa: S101
        cand = found[0]
        assert cand.mints == (mint,)  # noqa: S101
        assert cand.pools == (curve, good_pool_addr)  # noqa: S101
        assert cand.programs == (PUMP_PROGRAM, AMM)  # noqa: S101
        assert cand.expected_out_raw > amount  # noqa: S101
        assert cand.buy_leg.venue == curve and cand.buy_leg.input_mint == SOL  # noqa: S101
        assert (  # noqa: S101
            cand.sell_leg.venue == good_pool_addr and cand.sell_leg.output_mint == SOL
        )
        assert cand.created_slot == slot_a  # noqa: S101

        # Dedup by (buy_leg, sell_leg, amount): inside the window suppressed.
        disc.ingest(event(slot_dedup, vsol))
        assert disc.discover() == []  # noqa: S101
        # Outside the window the same opportunity re-arms.
        disc.ingest(event(slot_rearm, vsol))
        assert len(disc.discover()) == 1  # noqa: S101

        # Event-driven path: the consumer task emits from the hub queue.
        queue.put_nowait(event(slot_queue, vsol))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(disc.discover()) == 1  # noqa: S101
        await disc.stop()

        # Profit gate: parity-priced pool loses in both directions.
        hub2 = TradeFlowHub(None)  # type: ignore[arg-type]
        disc2 = CycleDiscovery(
            hub2, target_sol_lamports=target, buy_amount_lamports=amount
        )
        disc2.update_pool(
            mint, pool(bad_pool_addr, (8_500_000_000, 10_000_000_000_000))
        )
        disc2.ingest(event(slot_a, vsol))
        assert disc2.discover() == []  # noqa: S101

        # Containment: empty-reserve pool quoting fails inside ingest only.
        disc2.update_pool(mint, pool(empty_pool_addr, (0, 0)))
        disc2.ingest(event(slot_dedup, vsol))
        assert disc2.discover() == []  # noqa: S101
        assert disc2.errors == 0  # noqa: S101

    asyncio.run(scenario())
    return True


if __name__ == "__main__":
    print(self_check())
