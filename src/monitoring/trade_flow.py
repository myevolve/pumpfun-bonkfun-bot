"""Real-time pump.fun trade flow for one held coin: stream + exit rules.

The position monitor polls price every ``price_check_interval`` seconds. A
pump.fun coin's whole life is often 15-20 seconds and its dump happens inside
one slot (~0.4s), so a poll cannot see it. Every signal needed is already in
the ``TradeEvent`` the program emits on each trade: who traded, which side,
how much, and the reserves after. This module decodes those events from a
Geyser stream filtered on the coin's bonding curve and evaluates exit rules
on each one, so the monitor can react within a slot instead of an interval.

Read-only. Nothing here builds or sends a transaction.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import base58
import grpc
from solders.pubkey import Pubkey

from core.pubkeys import is_sol_paired
from geyser.generated import geyser_pb2, geyser_pb2_grpc
from monitoring.migration_events import decode_migration_events
from utils.logger import get_logger
from utils.program_logs import attribute_program_logs

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from monitoring.migration_events import MigrationHub
    from utils.idl_parser import IDLParser
logger = get_logger(__name__)

_PROGRAM_DATA = "Program data: "
_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Mayhem's ["sol-vault"] PDA, not a distinct human buyer.
MAYHEM_SOL_VAULT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
_TOKEN_DECIMALS = 6
_LAMPORTS_PER_SOL = 1_000_000_000
_MAX_RECENT_TRANSACTIONS = 4096


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """One decoded pump.fun ``TradeEvent`` with its transaction coordinates."""

    mint: str
    user: str
    creator: str
    is_buy: bool
    sol_amount: int
    token_amount: int
    virtual_sol_reserves: int
    virtual_token_reserves: int
    real_sol_reserves: int
    real_token_reserves: int
    slot: int
    signature: str
    timestamp: int

    @property
    def price(self) -> float:
        """Marginal SOL per whole token implied by the post-trade reserves."""
        return (self.virtual_sol_reserves / _LAMPORTS_PER_SOL) / (
            self.virtual_token_reserves / 10**_TOKEN_DECIMALS
        )


def decode_trade_events(  # noqa: C901 - keep attribution and wire validation together
    logs: list[str],
    *,
    slot: int,
    signature: str,
    idl_parser: IDLParser,
    mint: str | None = None,
    mints: set[str] | None = None,
) -> list[TradeEvent]:
    """Decode every ``TradeEvent`` in one transaction's logs, optionally for one mint.

    Only SOL-denominated flow is supported. Modern events require an explicit
    SOL quote and complete canonical quantities; zero is never a legacy fallback.
    Events predating quote fields retain their legacy SOL-only interpretation.
    Payloads must belong to a successful pump.fun invocation, including all
    ancestors. Missing or ambiguous runtime frames reject the whole log batch.
    """
    trade_disc = idl_parser.get_event_discriminators()["TradeEvent"]
    events: list[TradeEvent] = []
    try:
        entries = attribute_program_logs(logs)
    except ValueError as exc:
        logger.debug("Rejected TradeEvent log attribution in %s: %s", signature, exc)
        return []
    for _, program, log, committed in entries:
        if (
            not committed
            or program != _PUMP_PROGRAM
            or not log.startswith(_PROGRAM_DATA)
        ):
            continue
        try:
            data = base64.b64decode(log[len(_PROGRAM_DATA) :], validate=True)
        except ValueError:
            continue
        if data[:8] != trade_disc:
            continue
        decoded = idl_parser.decode_event_data(data, "TradeEvent")
        if not decoded:
            continue
        f = decoded["fields"]
        try:
            if mint is not None and f["mint"] != mint:
                continue
            if mints is not None and f["mint"] not in mints:
                continue
            canonical_quote = "quote_mint" in f or f.get("ix_name") in {
                "buy_v2",
                "sell_v2",
                "buy_exact_quote_in_v2",
            }
            if canonical_quote and not is_sol_paired(
                Pubkey.from_string(f["quote_mint"])
            ):
                continue
            sol_amount = int(f["quote_amount" if canonical_quote else "sol_amount"])
            virtual_sol_reserves = int(
                f[
                    "virtual_quote_reserves"
                    if canonical_quote
                    else "virtual_sol_reserves"
                ]
            )
            real_sol_reserves = int(
                f["real_quote_reserves" if canonical_quote else "real_sol_reserves"]
            )
            if virtual_sol_reserves <= 0 or int(f["virtual_token_reserves"]) <= 0:
                raise ValueError("non-positive virtual reserves")
            events.append(
                TradeEvent(
                    mint=f["mint"],
                    user=f["user"],
                    creator=f["creator"],
                    is_buy=bool(f["is_buy"]),
                    sol_amount=sol_amount,
                    token_amount=int(f["token_amount"]),
                    virtual_sol_reserves=virtual_sol_reserves,
                    virtual_token_reserves=int(f["virtual_token_reserves"]),
                    real_sol_reserves=real_sol_reserves,
                    real_token_reserves=int(f.get("real_token_reserves", 0)),
                    slot=slot,
                    signature=signature,
                    timestamp=int(f.get("timestamp", 0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Missing modern quantities must not silently reuse legacy values.
            logger.debug("Skipped TradeEvent in %s: %s", signature, exc)
    return events


@dataclass(frozen=True, slots=True)
class FlowRules:
    """Exit rules evaluated on every trade of a held coin. None disables a rule."""

    creator_sell: bool = True
    trailing_stop: float | None = None  # fraction below peak price since entry
    single_sell_pct: float | None = None  # one sell >= this fraction of real SOL
    net_outflow_pct: float | None = None  # net sell flow over window vs real SOL
    window: int = 5

    def __post_init__(self) -> None:
        for name in ("trailing_stop", "single_sell_pct", "net_outflow_pct"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 < value < 1
            ):
                raise ValueError(f"{name} must be a fraction in (0, 1) or None")
        if (
            isinstance(self.window, bool)
            or not isinstance(self.window, int)
            or self.window < 1
        ):
            raise ValueError("window must be a positive integer")

    @property
    def enabled(self) -> bool:
        return self.creator_sell or any(
            v is not None
            for v in (self.trailing_stop, self.single_sell_pct, self.net_outflow_pct)
        )


@dataclass(frozen=True, slots=True)
class FlowSignal:
    """A fired exit rule with the price observed when it fired."""

    rule: str
    detail: str
    price: float
    slot: int


class FlowMonitor:
    """Stateful rule evaluator for one held coin.

    ``observe`` is pure with respect to I/O: feed it trade events in slot
    order and it returns a ``FlowSignal`` the first time a rule fires. It
    keeps firing on later events so a caller that missed one can still act.
    """

    def __init__(
        self,
        *,
        mint: str,
        creator: str,
        rules: FlowRules,
        entry_price: float | None = None,
    ) -> None:
        """``entry_price`` seeds the trailing-stop peak so a pump that happened
        between the buy and the first streamed event still counts as the peak."""
        self.mint = mint
        self.creator = creator
        self.rules = rules
        self.peak_price: float | None = (
            entry_price if entry_price is not None and entry_price > 0 else None
        )
        self.last_price: float | None = None
        self._recent: deque[tuple[bool, int]] = deque(maxlen=rules.window)

    def observe(self, event: TradeEvent) -> FlowSignal | None:
        if event.mint != self.mint:
            return None
        price = event.price
        self.last_price = price
        if self.peak_price is None or price > self.peak_price:
            self.peak_price = price
        self._recent.append((event.is_buy, event.sol_amount))
        rules = self.rules

        if rules.creator_sell and not event.is_buy and event.user == self.creator:
            return FlowSignal(
                "creator_sell",
                f"creator sold {event.sol_amount / _LAMPORTS_PER_SOL:.4f} SOL",
                price,
                event.slot,
            )
        if (
            rules.single_sell_pct is not None
            and not event.is_buy
            and event.real_sol_reserves + event.sol_amount > 0
            and event.sol_amount
            >= rules.single_sell_pct * (event.real_sol_reserves + event.sol_amount)
        ):
            return FlowSignal(
                "single_sell",
                f"one sell took {event.sol_amount / _LAMPORTS_PER_SOL:.4f} SOL "
                f"of {(event.real_sol_reserves + event.sol_amount) / _LAMPORTS_PER_SOL:.4f}",
                price,
                event.slot,
            )
        if rules.net_outflow_pct is not None and event.real_sol_reserves > 0:
            net_out = sum(s for b, s in self._recent if not b) - sum(
                s for b, s in self._recent if b
            )
            if net_out >= rules.net_outflow_pct * event.real_sol_reserves:
                return FlowSignal(
                    "net_outflow",
                    f"net {net_out / _LAMPORTS_PER_SOL:.4f} SOL out over "
                    f"last {len(self._recent)} trades",
                    price,
                    event.slot,
                )
        if (
            rules.trailing_stop is not None
            and self.peak_price is not None
            and price <= self.peak_price * (1 - rules.trailing_stop)
        ):
            return FlowSignal(
                "trailing_stop",
                f"price {price:.3e} is {1 - price / self.peak_price:.0%} below "
                f"peak {self.peak_price:.3e}",
                price,
                event.slot,
            )
        return None


def _remember_transaction(recent: OrderedDict[str, None], signature: str) -> None:
    """Remember an admitted batch without unbounded signature retention."""
    # ponytail: process-local FIFO; durable identities if restart-safe delivery is needed.
    recent[signature] = None
    if len(recent) > _MAX_RECENT_TRANSACTIONS:
        recent.popitem(last=False)


_LossReason = Literal["overflow", "interrupted", "out_of_order"]


class TradeFlowLossError(ConnectionError):
    """A subscription lost trade history and cannot support flow decisions."""

    def __init__(self, reason: _LossReason) -> None:
        self.reason = reason
        super().__init__(f"Trade subscription history lost: {reason}")


class TradeQueue(asyncio.Queue[TradeEvent]):
    """A bounded trade queue whose readers all fail once history is lost."""

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize=maxsize)
        self.loss_reason: _LossReason | None = None
        self._last_slot: int | None = None
        self._readable = asyncio.Event()

    def put_nowait(self, item: TradeEvent) -> None:
        if self.loss_reason:
            raise TradeFlowLossError(self.loss_reason)
        if self._last_slot is not None and item.slot < self._last_slot:
            raise TradeFlowLossError("out_of_order")
        super().put_nowait(item)
        self._last_slot = item.slot
        self._readable.set()

    def get_nowait(self) -> TradeEvent:
        if self.loss_reason:
            raise TradeFlowLossError(self.loss_reason)
        event = super().get_nowait()
        if self.empty():
            self._readable.clear()
        return event

    async def get(self) -> TradeEvent:
        while True:
            await self._readable.wait()
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                continue  # Another reader consumed the last item after wakeup.

    def invalidate(self, reason: _LossReason) -> int:
        """Discard unconsumed history and wake every reader with a sticky failure."""
        if self.loss_reason is None:
            self.loss_reason = reason
        discarded = self.qsize()
        while not self.empty():
            super().get_nowait()
            self.task_done()
        self._readable.set()
        return discarded


class TradeFlowHub:
    """Fan out TradeEvents from one program-wide stream to per-mint subscribers.

    The token listener already receives every pump.fun transaction; decoding
    trades from that stream costs nothing extra and avoids a second Geyser
    subscription per held coin (providers cap concurrent streams). Decoding
    only happens while at least one mint is subscribed. Recently admitted
    transaction signatures survive listener reconnects for this hub's lifetime.
    """

    def __init__(
        self,
        idl_parser: IDLParser,
        *,
        queue_size: int = 512,
        migration_hub: MigrationHub | None = None,
    ) -> None:
        self.idl_parser = idl_parser
        self.queue_size = queue_size
        self._subscribers: dict[str, list[TradeQueue]] = {}
        self._recent_transactions: OrderedDict[str, None] = OrderedDict()
        self._interrupted = False
        self.dropped = 0
        self.migration_hub = migration_hub
        # Strong refs: the loop holds only weak refs to tasks.
        self._migration_tasks: set[asyncio.Task[None]] = set()

    @property
    def active(self) -> bool:
        return bool(self._subscribers) or (
            self.migration_hub is not None and self.migration_hub.active
        )

    def subscribe(self, mint: str) -> TradeQueue:
        queue = TradeQueue(maxsize=self.queue_size)
        if self._interrupted:
            queue.invalidate("interrupted")
            return queue
        self._subscribers.setdefault(mint, []).append(queue)
        return queue

    def unsubscribe(self, mint: str, queue: TradeQueue) -> None:
        queues = self._subscribers.get(mint)
        if not queues:
            return
        with contextlib.suppress(ValueError):
            queues.remove(queue)
        if not queues:
            del self._subscribers[mint]

    def stream_interrupted(self) -> None:
        """Invalidate history before teardown; reject subscriptions during the gap."""
        self._interrupted = True
        for queues in self._subscribers.values():
            for queue in queues:
                self.dropped += queue.invalidate("interrupted")
        self._subscribers.clear()

    def stream_resumed(self) -> None:
        """Allow new subscriptions after acknowledgement; never revive old queues."""
        self._interrupted = False

    def publish_logs(self, logs: list[str], *, slot: int, signature: str) -> int:
        """Deliver each recent transaction once, preserving all of its events."""
        if self._interrupted or not signature or signature in self._recent_transactions:
            return 0
        delivered = 0
        if self._subscribers:
            events = decode_trade_events(
                logs,
                slot=slot,
                signature=signature,
                idl_parser=self.idl_parser,
                mints=set(self._subscribers),
            )
            if events:
                _remember_transaction(self._recent_transactions, signature)
            for event in events:
                # Failed subscriptions are removed without skipping healthy siblings.
                for queue in reversed(self._subscribers.get(event.mint, ())):
                    try:
                        queue.put_nowait(event)
                        delivered += 1
                    except (asyncio.QueueFull, TradeFlowLossError) as exc:
                        reason = (
                            exc.reason
                            if isinstance(exc, TradeFlowLossError)
                            else "overflow"
                        )
                        discarded = queue.invalidate(reason) + 1
                        self.dropped += discarded
                        self.unsubscribe(event.mint, queue)
                        logger.warning(
                            "Trade subscription loss for %s (%s): discarded %d events",
                            event.mint,
                            reason,
                            discarded,
                        )
        migrations = self._publish_migrations(logs, slot=slot, signature=signature)
        if migrations:
            _remember_transaction(self._recent_transactions, signature)
        return delivered + migrations

    def _publish_migrations(self, logs: list[str], *, slot: int, signature: str) -> int:
        """Decode and schedule migration-event fan-out; contained, never raises."""
        hub = self.migration_hub
        if hub is None or not hub.active:
            return 0
        try:
            events = decode_migration_events(
                logs,
                slot=slot,
                signature=signature,
                pump_parser=hub.pump_parser,
                pamm_parser=hub.pamm_parser,
            )
            if not events:
                return 0
            task = asyncio.ensure_future(hub.publish(events))
        except Exception:
            # Containment contract: consumer problems never reach the listener.
            logger.exception("Migration fan-out failed in %s", signature)
            return 0
        self._migration_tasks.add(task)
        task.add_done_callback(self._migration_tasks.discard)
        return len(events)


@dataclass(frozen=True, slots=True)
class GateRules:
    """Entry gate evaluated on the first slots of a new coin's trades.

    A filter, not a demonstrated profitable entry strategy. The first hour's
    apparent edge disappeared after excluding protocol-vault trades from buyer
    counts; larger held-out samples were negative.
    """

    mayhem_only: bool = True
    min_buyers: int = 1  # distinct non-creator buyers required before we buy
    max_real_sol: float | None = 0.5  # skip if the curve already holds more
    min_real_sol: float = 0.1
    require_creator_holding: bool = True  # any creator sell => skip
    max_wait_slots: int = 3  # give up if no decision by creation + N slots
    max_wait_ms: int = 1500  # wall-clock bound in case the stream stalls

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_buyers, bool)
            or not isinstance(self.min_buyers, int)
            or self.min_buyers < 0
        ):
            raise ValueError("min_buyers must be a non-negative integer")
        if self.max_real_sol is not None and (
            isinstance(self.max_real_sol, bool)
            or not isinstance(self.max_real_sol, int | float)
            or self.max_real_sol <= 0
        ):
            raise ValueError("max_real_sol must be positive or None")
        if (
            isinstance(self.min_real_sol, bool)
            or not isinstance(self.min_real_sol, int | float)
            or self.min_real_sol < 0
            or (
                self.max_real_sol is not None and self.min_real_sol >= self.max_real_sol
            )
        ):
            raise ValueError("min_real_sol must be >= 0 and below max_real_sol")
        for name in ("max_wait_slots", "max_wait_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class GateDecision:
    accept: bool
    reason: str
    buyers: int
    real_sol: float | None
    slots_waited: int
    last_event: TradeEvent | None = None


class EntryGate:
    """Step evaluator: feed trade events in order; returns a decision once known."""

    def __init__(
        self, *, mint: str, creator: str, creation_slot: int, rules: GateRules
    ) -> None:
        self.mint = mint
        self.creator = creator
        self.creation_slot = creation_slot
        self.rules = rules
        self.buyers: set[str] = set()
        self.last_event: TradeEvent | None = None

    def _decision(
        self, accept: bool, reason: str, event: TradeEvent | None
    ) -> GateDecision:
        slot = event.slot if event is not None else self.creation_slot
        real = (
            event.real_sol_reserves / _LAMPORTS_PER_SOL if event is not None else None
        )
        return GateDecision(
            accept, reason, len(self.buyers), real, slot - self.creation_slot, event
        )

    def observe(self, event: TradeEvent) -> GateDecision | None:
        if event.mint != self.mint:
            return None
        if event.slot < self.creation_slot:
            return self._decision(
                accept=False, reason="trade_before_creation", event=self.last_event
            )
        if self.last_event is not None and event.slot < self.last_event.slot:
            return self._decision(
                accept=False, reason="trade_stream_out_of_order", event=self.last_event
            )
        self.last_event = event
        rules = self.rules
        if event.slot - self.creation_slot > rules.max_wait_slots:
            return self._decision(False, "window_expired", event)
        if (
            rules.require_creator_holding
            and not event.is_buy
            and event.user == self.creator
        ):
            return self._decision(False, "creator_sold", event)
        if (
            rules.max_real_sol is not None
            and event.real_sol_reserves > rules.max_real_sol * _LAMPORTS_PER_SOL
        ):
            return self._decision(False, "too_much_sol", event)
        if (
            event.is_buy
            and event.user != self.creator
            and event.user != MAYHEM_SOL_VAULT
        ):
            self.buyers.add(event.user)
        if len(self.buyers) >= rules.min_buyers:
            if event.real_sol_reserves < rules.min_real_sol * _LAMPORTS_PER_SOL:
                return None  # wait for more liquidity within the window
            return self._decision(True, "buyers_present", event)
        return None

    def timed_out(self) -> GateDecision:
        return self._decision(False, "timeout", self.last_event)


class GeyserTradeStream:
    """Yield decoded TradeEvents for one bonding curve from a Geyser subscription."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_token: str,
        auth_type: str,
        idl_parser: IDLParser,
        subscription_timeout: float = 10.0,
    ) -> None:
        if auth_type not in {"x-token", "basic"}:
            raise ValueError("auth_type must be x-token or basic")
        self.endpoint = endpoint
        self.api_token = api_token
        self.auth_type = auth_type
        self.idl_parser = idl_parser
        self.subscription_timeout = subscription_timeout

    def _credentials(self) -> grpc.ChannelCredentials:
        if self.auth_type == "x-token":
            auth = grpc.metadata_call_credentials(
                lambda _, cb: cb((("x-token", self.api_token),), None)
            )
        else:
            auth = grpc.metadata_call_credentials(
                lambda _, cb: cb((("authorization", f"Basic {self.api_token}"),), None)
            )
        return grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), auth)

    @staticmethod
    def _request(bonding_curve: str) -> geyser_pb2.SubscribeRequest:
        request = geyser_pb2.SubscribeRequest()
        flt = request.transactions["curve"]
        flt.account_include.append(bonding_curve)
        flt.failed = False
        request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
        return request

    async def stream(
        self,
        *,
        mint: str,
        bonding_curve: str,
        on_disconnect: Callable[[], None] | None = None,
    ) -> AsyncIterator[TradeEvent]:
        """Stream trades touching ``bonding_curve`` until cancelled or the stream ends.

        Raises on connection or stream failure; the caller decides whether to
        fall back to polling. No reconnect here: a held coin's life is shorter
        than a backoff schedule. Recent signatures are deduplicated per stream.
        ``on_disconnect`` invalidates pending decisions before asynchronous cleanup.
        """
        channel = grpc.aio.secure_channel(self.endpoint, self._credentials())
        call = None
        recent_transactions: OrderedDict[str, None] = OrderedDict()
        last_slot: int | None = None
        try:
            stub = geyser_pb2_grpc.GeyserStub(channel)
            call = stub.Subscribe(iter([self._request(bonding_curve)]))
            await asyncio.wait_for(
                call.initial_metadata(), timeout=self.subscription_timeout
            )
            async for update in call:
                if not update.HasField("transaction"):
                    continue
                info = update.transaction.transaction
                if info.meta.HasField("err"):
                    continue
                signature = base58.b58encode(bytes(info.signature)).decode()
                if not signature or signature in recent_transactions:
                    continue
                events = decode_trade_events(
                    list(info.meta.log_messages),
                    slot=update.transaction.slot,
                    signature=signature,
                    idl_parser=self.idl_parser,
                    mint=mint,
                )
                if events:
                    if last_slot is not None and update.transaction.slot < last_slot:
                        raise TradeFlowLossError("out_of_order")
                    last_slot = update.transaction.slot
                    _remember_transaction(recent_transactions, signature)
                for event in events:
                    yield event
            raise ConnectionError("Geyser trade stream ended")
        finally:
            try:
                if on_disconnect is not None:
                    on_disconnect()
            finally:
                if call is not None:
                    call.cancel()
                await channel.close()
