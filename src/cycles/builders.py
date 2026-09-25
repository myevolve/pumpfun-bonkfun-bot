# ruff: noqa: TRY003  (free-form invariant messages, mirrors pumpswap.py)
"""Per-venue swap instruction builders for the cycle runner.

Every leg routes to a wire-audited venue builder:

- pump.fun bonding-curve legs -> buy_v2/sell_v2 via
  :class:`PumpFunInstructionBuilder` (27/26 accounts; layout is
  machine-checked against idl/pump_fun_idl.json by
  learning-examples/verify_v2_account_layout.py).
- PumpSwap (pAMM) sell legs -> :func:`build_pumpswap_sell_instructions`
  (wire-correct per audit, pumpswap.py).

Raydium AMM v4 / CPMM legs are fail-closed: their 18-account layouts
(AMM v4 plus the Serum orderbook) are not ported here, and raising is the
honest gate. PumpSwap *buy* legs are fail-closed for the same reason —
only the sell direction has an audited builder.

SOL interface (why there is no manual wrap/unwrap leg): pump.fun v2
instructions move native SOL directly and only seed-check the user's quote
ATA, and the audited PumpSwap sell creates the user's WSOL quote ATA
idempotently and closes it afterwards (closing a WSOL account unwraps it).
No wired leg debits a WSOL token account, so no create+syncNative funding
leg is required; a future PumpSwap buy leg would need one.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from solders.account import Account
from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_PROGRAM, WSOL_MINT
from interfaces.core import Platform, TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddressProvider
from platforms.pumpfun.instruction_builder import PumpFunInstructionBuilder
from platforms.pumpfun.pumpswap import (
    PUMP_SWAP_GLOBAL_CONFIG,
    PUMP_SWAP_PROGRAM,
    PumpSwapAddresses,
    _decode_global_recipients,
    build_pumpswap_sell_instructions,
)
from utils.idl_parser import IDLParser

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from solders.instruction import Instruction

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PAMM_PROGRAM = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")

# CLAUDE.md sell pattern: floor the expected output by the slippage budget.
SLIPPAGE_FLOOR = 0.02

_ADDRESS_PROVIDER = PumpFunAddressProvider()
_INSTRUCTION_BUILDER_HOLDER: list[PumpFunInstructionBuilder] = []


def _instruction_builder() -> PumpFunInstructionBuilder:
    if not _INSTRUCTION_BUILDER_HOLDER:
        _INSTRUCTION_BUILDER_HOLDER.append(
            PumpFunInstructionBuilder(IDLParser("idl/pump_fun_idl.json"))
        )
    return _INSTRUCTION_BUILDER_HOLDER[0]


def _drive(coro: Coroutine[None, None, list]) -> list:
    """Run one v2-builder coroutine synchronously.

    The builders contain no awaits; when the caller already sits inside a
    running loop (run_session), asyncio.run is unavailable, so drive the
    coroutine directly. Anything that tries to await fails loudly here.
    """
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("v2 instruction builders must not await")


def _as_pubkey(value: str | Pubkey, field: str) -> Pubkey:
    if isinstance(value, Pubkey):
        return value
    if isinstance(value, str):
        return Pubkey.from_string(value)
    raise ValueError(f"{field} must be a Pubkey or base58 string")


def curve_token_info(mint: str | Pubkey, curve_state: dict) -> TokenInfo:
    """Minimal TokenInfo for the v2 curve builders from scanner curve state.

    Requires ``creator`` (BondingCurve offset 49 in the current layout);
    legacy 49-byte curve accounts cannot drive v2 legs and fail here.
    """
    mint_key = _as_pubkey(mint, "mint")
    creator = curve_state.get("creator")
    if creator is None:
        raise ValueError(
            "curve state requires creator (BondingCurve offset 49) for v2 legs"
        )
    creator = _as_pubkey(creator, "creator")
    # create_v2 mints are Token-2022; the scanner reads the mint account's
    # owner as base_token_program. A hardcoded SPL program mis-derives the
    # user's base ATA and the v2 wire's token-program account
    # (audited: IncorrectProgramId on live Token-2022 coins).
    token_program = _as_pubkey(
        curve_state.get("base_token_program"), "base_token_program"
    )
    bonding_curve = _ADDRESS_PROVIDER.derive_pool_address(mint_key)
    return TokenInfo(
        name="cycle",
        symbol="CYCLE",
        uri="",
        mint=mint_key,
        platform=Platform.PUMP_FUN,
        bonding_curve=bonding_curve,
        associated_bonding_curve=_ADDRESS_PROVIDER.derive_associated_bonding_curve(
            mint_key, bonding_curve, token_program
        ),
        creator=creator,
        creator_vault=_ADDRESS_PROVIDER.derive_creator_vault(creator),
        token_program_id=token_program,
        is_mayhem_mode=bool(curve_state.get("is_mayhem_mode", False)),
        quote_mint=WSOL_MINT,
        quote_token_program_id=TOKEN_PROGRAM,
    )


def pamm_token_info(
    pool_state: dict,
    *,
    base_token_program: Pubkey,
    fee_recipients: tuple[Pubkey, Pubkey],
) -> TokenInfo:
    """Minimal TokenInfo driving the audited PumpSwap sell builder.

    ``pool_state`` keys come from the scanner's ``read_pamm_pool_state``:
    pool_address, base_mint, base_vault, quote_vault, coin_creator,
    is_cashback_coin, needs_extension. Quote is the canonical wrapped-SOL
    migration pair; protocol/buyback fee recipients come from the decoded
    PumpSwap global config via :func:`pamm_fee_recipients`.
    """
    mint = _as_pubkey(pool_state["base_mint"], "base_mint")
    coin_creator = _as_pubkey(pool_state["coin_creator"], "coin_creator")
    pool = _as_pubkey(pool_state["pool_address"], "pool_address")
    for field in ("base_vault", "quote_vault"):
        if pool_state.get(field) is None:
            raise ValueError(
                f"PumpSwap pool state requires {field} for the sell wire"
            )
    return TokenInfo(
        name="cycle",
        symbol="CYCLE",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
        pool_state=pool,
        base_vault=_as_pubkey(pool_state["base_vault"], "base_vault"),
        quote_vault=_as_pubkey(pool_state["quote_vault"], "quote_vault"),
        global_config=PUMP_SWAP_GLOBAL_CONFIG,
        platform_config=PumpSwapAddresses.find_fee_config(),
        creator=coin_creator,
        creator_vault=PumpSwapAddresses.derive_creator_vault(coin_creator),
        token_program_id=base_token_program,
        quote_mint=WSOL_MINT,
        quote_token_program_id=TOKEN_PROGRAM,
        protocol_fee_recipient=fee_recipients[0],
        buyback_fee_recipient=fee_recipients[1],
        is_cashback_coin=bool(pool_state.get("is_cashback_coin", False)),
        pool_needs_extension=bool(pool_state.get("needs_extension", False)),
        curve_complete=True,
        pool_status="pumpswap",
    )


def build_pamm_sell_instructions(  # noqa: PLR0913 - contract signature
    *,
    user: Pubkey,
    pool_state: dict,
    base_token_program: Pubkey,
    amount_in: int,
    min_quote_out: int,
    fee_recipients: tuple[Pubkey, Pubkey],
) -> list[Instruction]:
    """Build a canonical PumpSwap sell via the audited builder.

    The returned wire creates the user's WSOL quote ATA idempotently,
    performs the swap, and closes the WSOL ATA (unwrap) when the quote is
    wrapped SOL.
    """
    token_info = pamm_token_info(
        pool_state,
        base_token_program=base_token_program,
        fee_recipients=fee_recipients,
    )
    return build_pumpswap_sell_instructions(token_info, user, amount_in, min_quote_out)


def build_curve_buy_instructions(
    *,
    token_info: TokenInfo,
    user: Pubkey,
    amount_in: int,
    min_tokens_out: int,
) -> list[Instruction]:
    """Build a pump.fun buy_v2 wire (base ATA create + 27-account swap).

    ``amount_in`` is the maximum SOL cost; SOL-paired v2 buys move native
    SOL and only seed-check the user's quote ATA, so no wrap is needed.
    """
    return _drive(
        _instruction_builder().build_buy_v2_instruction(
            token_info, user, amount_in, min_tokens_out, _ADDRESS_PROVIDER
        )
    )


def build_curve_sell_instructions(
    *,
    token_info: TokenInfo,
    user: Pubkey,
    amount_in: int,
    min_quote_out: int,
) -> list[Instruction]:
    """Build a pump.fun sell_v2 wire (26-account swap).

    SOL-paired sales pay out in native SOL, so no WSOL unwrap is needed.
    """
    return _drive(
        _instruction_builder().build_sell_v2_instruction(
            token_info, user, amount_in, min_quote_out, _ADDRESS_PROVIDER
        )
    )


def pamm_fee_recipients(
    global_config_data: bytes,
    *,
    is_mayhem_mode: bool,
    chooser: Callable[[tuple[Pubkey, ...]], Pubkey] = secrets.choice,
) -> tuple[Pubkey, Pubkey]:
    """Decode (protocol_fee_recipient, buyback_fee_recipient) from the
    PumpSwap global config account data.

    Mayhem pools draw from the reserved recipient group, every other pool
    from the standard protocol group, mirroring the PumpSwapManager.
    """
    account = Account(
        lamports=0,
        data=global_config_data,
        owner=PUMP_SWAP_PROGRAM,
        executable=False,
        rent_epoch=0,
    )
    protocol, reserved, buyback = _decode_global_recipients(account)
    group = reserved if is_mayhem_mode else protocol
    return chooser(group), chooser(buyback)
