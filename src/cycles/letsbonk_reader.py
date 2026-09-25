"""LetsBonk (Raydium LaunchLab) graduation reader for the cycle scanner.

A coin graduates from the LaunchLab curve when its PoolState.status leaves
FUNDING (0): 1 = waiting for migration, 2 = migrated to Raydium AMM v4
(migrate_type 0) or CPMM (migrate_type 1). Both migration targets are already
decoded and quoted by ``core.cycles.pool``, so this module only needs to:

1. read the curve's PoolState (status, reserves, fees) via the platform's
   IDL-driven manager — the same code path the bot trades through, and
2. report the migration target so the scanner can evaluate the curve side
   against the Raydium venue.

There is no graduation EVENT in the LaunchLab IDL; the status flip (or a
``migrate_to_amm``/``migrate_to_cpswap`` instruction in the stream) is the
signal.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if TYPE_CHECKING:
    from core.client import SolanaClient

from solders.pubkey import Pubkey

from core.cycles.core import AMM, CPMM, SOL
from platforms.letsbonk.address_provider import (
    LetsBonkAddresses,
    LetsBonkAddressProvider,
)
from platforms.letsbonk.curve_manager import (
    LaunchLabPoolStatus,
    LetsBonkCurveManager,
)
from utils.idl_parser import IDLParser

_LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
_MIGRATION_TARGETS = {0: AMM, 1: CPMM}
# LaunchLab IDL discriminators (migrate_to_amm :2529, migrate_to_cpswap
# :3123). No graduation event exists; these protocol-gated instructions are
# the real-time signal when riding the geyser stream.
_MIGRATION_DISCS = {
    "cf52c091fecf91df",  # migrate_to_amm
    "885cc8671cda908c",  # migrate_to_cpswap
}
_PROGRAM_DATA = "Program data: "


def derive_launchlab_pool(base_mint: str) -> str:
    """LaunchLab pool PDA for a base mint (WSOL quote is the migration pair)."""
    provider = LetsBonkAddressProvider()
    return str(
        provider.derive_pool_address(
            Pubkey.from_string(base_mint), Pubkey.from_string(SOL)
        )
    )


_PROGRAM_DATA = "Program data: "


def decode_migration_signals(logs: list[str]) -> list[dict[str, str]]:
    """Scan one transaction's log lines for LaunchLab migration instructions.

    Returns one entry per matching ``Program data`` line: the migration kind
    (``amm`` or ``cpswap``) and its 8-byte discriminator. There is no
    graduation event in the LaunchLab IDL; these protocol-gated instruction
    payloads are the real-time signal. The base_mint is NOT in the payload —
    the caller derives candidate mints from the transaction's account keys.
    """
    signals: list[dict[str, str]] = []
    for log in logs:
        if not log.startswith(_PROGRAM_DATA):
            continue
        try:
            data = base64.b64decode(log[len(_PROGRAM_DATA) :])
        except ValueError:
            continue
        disc = data[:8].hex()
        if disc == "cf52c091fecf91df":
            kind = "amm"
        elif disc == "885cc8671cda908c":
            kind = "cpswap"
        else:
            continue
        signals.append({"kind": kind, "disc": disc})
    return signals


class LetsBonkGraduationReader:
    """Curve-side reader for letsbonk coins in the cycle scanner."""

    def __init__(self, client: SolanaClient) -> None:
        self._manager = LetsBonkCurveManager(
            client, IDLParser("idl/raydium_launchlab_idl.json")
        )

    async def read_curve_state(self, mint: str) -> dict[str, Any] | None:
        """Read one LaunchLab curve in the scanner's curve-state shape.

        Returns None when the pool does not exist or cannot be decoded.
        The shape mirrors ``runner.read_curve_state`` (pump.fun) so the
        scanner's evaluation logic stays venue-agnostic where possible, and
        adds letsbonk-specific fields the evaluator needs.
        """
        pool_address = Pubkey.from_string(derive_launchlab_pool(mint))
        try:
            state = await self._manager.get_pool_state(pool_address)
        except Exception:  # noqa: BLE001 - decode failure means "not a letsbonk curve"
            return None
        status_raw = state.get("status")
        try:
            status = LaunchLabPoolStatus(int(status_raw))
        except (TypeError, ValueError):
            return None
        if status is not LaunchLabPoolStatus.FUNDING:
            # Quote-able only while funding; migrated curves route to the
            # Raydium venue leg, not the curve.
            return None
        return {
            "pool_address": str(pool_address),
            # Curve-side reserves in the scanner's naming (quote == SOL).
            "virtual_sol_reserves": int(state["virtual_quote"]),
            "virtual_token_reserves": int(state["virtual_base"]),
            "real_sol_reserves": int(state["real_quote"]),
            "real_token_reserves": int(state["real_base"]),
            "token_total_supply": int(state.get("token_total_supply", 0) or 0),
            "complete": False,  # FUNDING curves are incomplete by definition
            "platform": "letsbonk",
        }

    async def migration_target(self, mint: str) -> str | None:
        """Raydium program a migrated coin trades on (AMM or CPMM), or None."""
        pool_address = Pubkey.from_string(derive_launchlab_pool(mint))
        try:
            state = await self._manager.get_pool_state(pool_address)
        except Exception:  # noqa: BLE001 - decode failure means "not a letsbonk curve"
            return None
        migrate_type = state.get("curve_param", {}).get("migrate_type")
        target = _MIGRATION_TARGETS.get(int(migrate_type or 0))
        return str(target) if target else None

    async def status(self, mint: str) -> LaunchLabPoolStatus | None:
        """Current LaunchLab pool status, or None when unreadable."""
        pool_address = Pubkey.from_string(derive_launchlab_pool(mint))
        try:
            state = await self._manager.get_pool_state(pool_address)
        except Exception:  # noqa: BLE001 - decode failure means "not a letsbonk curve"
            return None
        try:
            return LaunchLabPoolStatus(int(state.get("status")))
        except (TypeError, ValueError):
            return None


def self_check() -> bool:
    """Offline shape checks: derivations and migration-target mapping."""
    if _LAUNCHLAB_PROGRAM != str(LetsBonkAddresses.PROGRAM):
        raise ValueError(  # noqa: TRY003 - offline self-check
            "LaunchLab program constant drifted from the provider"
        )
    if _MIGRATION_TARGETS[0] != AMM or _MIGRATION_TARGETS[1] != CPMM:
        raise ValueError(  # noqa: TRY003
            "migration-target mapping drifted"
        )
    pool = derive_launchlab_pool("So11111111111111111111111111111111111111112")
    if len(pool) != 44:  # noqa: PLR2004 - base58 PDA length
        raise ValueError(  # noqa: TRY003 - offline self-check
            "pool PDA derivation is malformed"
        )
    if pool == derive_launchlab_pool("11111111111111111111111111111111"):
        raise ValueError(  # noqa: TRY003
            "pool PDA derivation is not mint-dependent"
        )
    return True


if __name__ == "__main__":
    print(self_check())
