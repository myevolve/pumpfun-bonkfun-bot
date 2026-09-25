"""Migration event fan-out: pump.fun graduation + PumpSwap pool creation.

A coin's graduation is slot-scale: the pump.fun ``CompleteEvent`` and the
PumpSwap ``CreatePoolEvent`` ride the same Geyser transaction stream the token
listener already consumes, so decoding them costs nothing extra. This module
decodes those events from raw program logs and fans them out to subscribers
(the event-driven cycle scanner) the moment they appear, instead of waiting
for the next poll.

Read-only. Nothing here builds or sends a transaction.
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from utils.idl_parser import IDLParser

logger = get_logger(__name__)

_PROGRAM_DATA = "Program data: "
_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_PAMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def _canonical_pamm_pool(base_mint: str, quote_mint: str) -> str:
    """Derive the canonical index-0 PumpSwap migration pool PDA.

    Mirrors PumpSwapAddresses.derive_canonical_pool without importing the
    platform module (keeps this decoder dependency-light).
    """
    import struct as _struct

    from solders.pubkey import Pubkey

    authority, _ = Pubkey.find_program_address(
        [b"pool-authority", bytes(Pubkey.from_string(base_mint))],
        Pubkey.from_string(_PUMP_PROGRAM),
    )
    pool, _ = Pubkey.find_program_address(
        [
            b"pool",
            _struct.pack("<H", 0),
            bytes(authority),
            bytes(Pubkey.from_string(base_mint)),
            bytes(Pubkey.from_string(quote_mint)),
        ],
        Pubkey.from_string(_PAMM_PROGRAM),
    )
    return str(pool)


@dataclass(frozen=True, slots=True)
class MigrationEvent:
    """One pump.fun graduation or PumpSwap pool creation, with tx coordinates."""

    kind: str  # "complete" | "pool_created"
    mint: str
    slot: int
    signature: str
    timestamp: int
    pool: str | None
    pool_base_amount: int
    pool_quote_amount: int
    bonding_curve: str | None


def decode_migration_events(
    logs: list[str],
    *,
    slot: int,
    signature: str,
    pump_parser: IDLParser,
    pamm_parser: IDLParser,
) -> list[MigrationEvent]:
    """Decode ``CompleteEvent`` and ``CreatePoolEvent`` from one transaction's logs.

    A graduation tx emits a pump.fun ``CompleteEvent`` (mint just filled its
    curve) and a PumpSwap ``CreatePoolEvent`` (the migration pool it now trades
    on); both are matched by discriminator prefix. Malformed payloads are
    skipped: a bad log line must never kill the stream that decoded it.
    """
    sources = {
        pump_parser.get_event_discriminators()["CompleteEvent"]: (
            pump_parser,
            "CompleteEvent",
            "complete",
        ),
        # The pAMM-path completion event carries the pool directly (and the
        # migration fee), enabling a pool-bearing complete branch with no
        # extra RPC. CompleteEvent alone carries none.
        pump_parser.get_event_discriminators()["CompletePumpAmmMigrationEvent"]: (
            pump_parser,
            "CompletePumpAmmMigrationEvent",
            "pool_created",
        ),
        pamm_parser.get_event_discriminators()["CreatePoolEvent"]: (
            pamm_parser,
            "CreatePoolEvent",
            "pool_created",
        ),
    }
    # Only SOL-quoted events are tradeable by this scanner; a USDC-quoted
    # pool's 6-decimal amounts treated as lamports would read 1000x large.
    sol_quote = "So11111111111111111111111111111111111111112"
    events: list[MigrationEvent] = []
    for log in logs:
        if not log.startswith(_PROGRAM_DATA):
            continue
        try:
            data = base64.b64decode(log[len(_PROGRAM_DATA) :])
        except ValueError:
            continue
        source = sources.get(data[:8])
        if source is None:
            continue
        parser, name, kind = source
        decoded = parser.decode_event_data(data, name)
        if not decoded:
            continue
        f = decoded["fields"]
        try:
            if kind == "complete":
                # CompleteEvent carries quote_mint (IDL field): skip non-SOL.
                if str(f.get("quote_mint", sol_quote)) != sol_quote:
                    continue
                events.append(
                    MigrationEvent(
                        kind=kind,
                        mint=f["mint"],
                        slot=slot,
                        signature=signature,
                        timestamp=int(f["timestamp"]),
                        pool=None,
                        pool_base_amount=0,
                        pool_quote_amount=0,
                        bonding_curve=f["bonding_curve"],
                    )
                )
            else:
                # Two shapes share this kind: CreatePoolEvent (base_mint,
                # pool_base_amount, pool_quote_amount) and
                # CompletePumpAmmMigrationEvent (mint, sol_amount; pool
                # carries the seeded migration liquidity).
                is_create_pool = name == "CreatePoolEvent"
                if str(f["quote_mint"]) != sol_quote:
                    continue
                base_mint = f["base_mint"] if is_create_pool else f["mint"]
                # Canonical-PDA gate: pAMM pools are permissionless; only
                # the index-0 migration pool is the graduation signal.
                if str(f["pool"]) != _canonical_pamm_pool(base_mint, f["quote_mint"]):
                    logger.debug(
                        "Non-canonical pAMM pool %s for %s (forged or index>0); skipped",
                        str(f["pool"])[:12],
                        str(base_mint)[:12],
                    )
                    continue
                events.append(
                    MigrationEvent(
                        kind=kind,
                        mint=base_mint,
                        slot=slot,
                        signature=signature,
                        timestamp=int(f["timestamp"]),
                        pool=f["pool"],
                        pool_base_amount=int(
                            f["pool_base_amount"]
                            if is_create_pool
                            else f["mint_amount"]
                        ),
                        pool_quote_amount=int(
                            f["pool_quote_amount"]
                            if is_create_pool
                            else f["sol_amount"]
                        ),
                        bonding_curve=None,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Skipped %s in %s: %s", name, signature, exc)
    return events


class MigrationHub:
    """Fan out MigrationEvents to async subscribers with error containment.

    A consumer that raises is swallowed, logged, and counted in ``errors``;
    it never propagates back to the stream that published.
    """

    def __init__(self, pump_parser: IDLParser, pamm_parser: IDLParser) -> None:
        self.pump_parser = pump_parser
        self.pamm_parser = pamm_parser
        self._callbacks: list[Callable[[MigrationEvent], Awaitable[None]]] = []
        self.errors = 0

    @property
    def active(self) -> bool:
        return bool(self._callbacks)

    def subscribe(self, callback: Callable[[MigrationEvent], Awaitable[None]]) -> None:
        self._callbacks.append(callback)

    def unsubscribe(
        self, callback: Callable[[MigrationEvent], Awaitable[None]]
    ) -> None:
        with contextlib.suppress(ValueError):
            self._callbacks.remove(callback)

    async def publish(self, events: Sequence[MigrationEvent]) -> int:
        """Deliver every event to every callback; one failure never breaks the rest."""
        delivered = 0
        for callback in self._callbacks:
            for event in events:
                try:
                    await callback(event)
                    delivered += 1
                except Exception:
                    self.errors += 1
                    logger.exception("Migration callback failed for %s", event.mint)
        return delivered
