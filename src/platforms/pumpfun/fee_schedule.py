"""Authoritative Pump.fun dynamic-fee account decoding and quote math."""
# Detailed domain failures are safer here than dozens of message-only subclasses.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import struct
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from solders.account import Account
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from core.client import SolanaClient
from core.client import RpcUnavailableError
from core.pubkeys import USDC_MINT, WSOL_MINT, normalize_quote_mint
from platforms.pumpfun.address_provider import PumpFunAddresses
from utils.logger import get_logger

FEE_CONFIG_DISCRIMINATOR = bytes((143, 52, 146, 187, 219, 123, 76, 155))

logger = get_logger(__name__)

GET_FEES_DISCRIMINATOR = bytes((231, 37, 126, 85, 207, 91, 63, 52))
_MAX_BASIS_POINTS = 10_000
_FEE_TIER_SIZE = 40
_MAX_U64 = 0xFFFF_FFFF_FFFF_FFFF
_GET_FEES_RETURN_SIZE = 24


class _FeeConfigError(ValueError):
    """Rejected fee configuration or quote input."""


class _FeeAttestationError(RuntimeError):
    """Fee snapshot or program-attestation failure."""


class _FeeAttestationUnavailable(_FeeAttestationError, RpcUnavailableError):
    """Attestation could not run because the RPC transport returned nothing."""


@dataclass(frozen=True, slots=True)
class PumpFees:
    """Fee rates returned by the Pump fee program."""

    lp_fee_bps: int
    protocol_fee_bps: int
    creator_fee_bps: int


@dataclass(frozen=True, slots=True)
class PumpFeeTier:
    """Fee rates activated at a raw quote-market-cap threshold."""

    market_cap_threshold_raw: int
    fees: PumpFees


@dataclass(frozen=True, slots=True)
class PumpFeeConfig:
    """Validated immutable contents of the Pump fee-config account."""

    bump: int
    admin: Pubkey
    flat_fees: PumpFees
    regular_tiers: tuple[PumpFeeTier, ...]
    stable_tiers: tuple[PumpFeeTier, ...]
    exotic_flat_fees: PumpFees
    digest: str


@dataclass(frozen=True, slots=True)
class PumpFeeSnapshot:
    """Attested fee configuration and its local freshness timestamps."""

    config: PumpFeeConfig
    observed_at: float
    attested_at: float


@dataclass(frozen=True, slots=True)
class PumpQuote:
    """Raw fee-adjusted result for one Pump bonding-curve trade."""

    amount_in_raw: int
    amount_out_raw: int
    gross_quote_raw: int
    net_quote_raw: int
    protocol_fee_raw: int
    creator_fee_raw: int
    market_cap_raw: int
    fees: PumpFees
    config_digest: str


class _AccountCursor:
    """Bounds-checked cursor over an untrusted account-data buffer."""

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self.offset = 0

    @property
    def remaining(self) -> int:
        """Return unread bytes."""
        return len(self._data) - self.offset

    def take(self, size: int, field: str) -> bytes:
        """Consume exactly ``size`` bytes or reject truncated input."""
        end = self.offset + size
        if size < 0 or end > len(self._data):
            raise _FeeConfigError(
                f"FeeConfig account is truncated while reading {field}"
            )
        value = bytes(self._data[self.offset : end])
        self.offset = end
        return value

    def u8(self, field: str) -> int:
        """Consume one unsigned byte."""
        return self.take(1, field)[0]

    def u32(self, field: str) -> int:
        """Consume a little-endian u32."""
        return struct.unpack("<I", self.take(4, field))[0]

    def u64(self, field: str) -> int:
        """Consume a little-endian u64."""
        return struct.unpack("<Q", self.take(8, field))[0]

    def u128(self, field: str) -> int:
        """Consume a little-endian u128."""
        return int.from_bytes(self.take(16, field), "little")


def _validate_fees(fees: PumpFees, field: str) -> PumpFees:
    values = (
        ("lp", fees.lp_fee_bps),
        ("protocol", fees.protocol_fee_bps),
        ("creator", fees.creator_fee_bps),
    )
    for name, value in values:
        if value > _MAX_BASIS_POINTS:
            raise _FeeConfigError(f"{field} {name} basis points exceed 10,000")
    if fees.protocol_fee_bps + fees.creator_fee_bps > _MAX_BASIS_POINTS:
        raise _FeeConfigError(
            f"{field} protocol and creator basis points exceed 10,000"
        )
    return fees


def _decode_fees(cursor: _AccountCursor, field: str) -> PumpFees:
    return _validate_fees(
        PumpFees(
            lp_fee_bps=cursor.u64(f"{field}.lp_fee_bps"),
            protocol_fee_bps=cursor.u64(f"{field}.protocol_fee_bps"),
            creator_fee_bps=cursor.u64(f"{field}.creator_fee_bps"),
        ),
        field,
    )


def _decode_tiers(cursor: _AccountCursor, field: str) -> tuple[PumpFeeTier, ...]:
    count = cursor.u32(f"{field}.length")
    if count == 0:
        raise _FeeConfigError(f"{field} must not be empty")
    if count > cursor.remaining // _FEE_TIER_SIZE:
        raise _FeeConfigError(f"FeeConfig account is truncated while reading {field}")

    tiers = tuple(
        PumpFeeTier(
            market_cap_threshold_raw=cursor.u128(
                f"{field}[{index}].market_cap_threshold"
            ),
            fees=_decode_fees(cursor, f"{field}[{index}].fees"),
        )
        for index in range(count)
    )
    if any(
        current.market_cap_threshold_raw >= following.market_cap_threshold_raw
        for current, following in pairwise(tiers)
    ):
        raise _FeeConfigError(f"{field} thresholds must be strictly increasing")
    return tiers


def decode_fee_config_account(account: Account) -> PumpFeeConfig:
    """Decode and validate the Pump program's dynamic fee configuration."""
    if account.owner != PumpFunAddresses.FEE_PROGRAM:
        raise _FeeConfigError("FeeConfig account has an unexpected owner")
    if not isinstance(account.data, bytes):
        raise _FeeConfigError("FeeConfig account data must be bytes")

    cursor = _AccountCursor(account.data)
    if cursor.take(8, "discriminator") != FEE_CONFIG_DISCRIMINATOR:
        raise _FeeConfigError("FeeConfig account has an invalid discriminator")

    bump = cursor.u8("bump")
    admin = Pubkey.from_bytes(cursor.take(32, "admin"))
    flat_fees = _decode_fees(cursor, "flat_fees")
    regular_tiers = _decode_tiers(cursor, "fee_tiers")
    stable_tiers = _decode_tiers(cursor, "stable_fee_tiers")
    exotic_flat_fees = _decode_fees(cursor, "exotic_flat_fees")
    if any(cursor.take(cursor.remaining, "trailing padding")):
        raise _FeeConfigError("FeeConfig account has unknown nonzero trailing data")

    return PumpFeeConfig(
        bump=bump,
        admin=admin,
        flat_fees=flat_fees,
        regular_tiers=regular_tiers,
        stable_tiers=stable_tiers,
        exotic_flat_fees=exotic_flat_fees,
        digest=hashlib.sha256(account.data).hexdigest(),
    )


def _require_raw_u64(value: object, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_U64
    ):
        qualifier = "positive " if positive else ""
        raise _FeeConfigError(f"{field} must be a {qualifier}raw u64 integer")
    return value


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _state_pubkey(state: Mapping[str, object], field: str) -> Pubkey:
    value = state.get(field)
    if isinstance(value, Pubkey):
        return value
    if isinstance(value, str):
        try:
            return Pubkey.from_string(value)
        except ValueError as exc:
            raise _FeeConfigError(f"{field} must be a valid public key") from exc
    raise _FeeConfigError(f"{field} must be a valid public key")


def _select_fees(
    state: Mapping[str, object],
    snapshot: PumpFeeSnapshot,
) -> tuple[PumpFees, int]:
    if state.get("complete") is not False:
        raise _FeeConfigError("Pump.fun bonding curve is complete or malformed")

    virtual_token_reserves = _require_raw_u64(
        state.get("virtual_token_reserves"),
        "virtual_token_reserves",
        positive=True,
    )
    virtual_quote_reserves = _require_raw_u64(
        state.get("virtual_quote_reserves"),
        "virtual_quote_reserves",
        positive=True,
    )
    token_total_supply = _require_raw_u64(
        state.get("token_total_supply"),
        "token_total_supply",
        positive=True,
    )
    market_cap_raw = (
        virtual_quote_reserves * token_total_supply // virtual_token_reserves
    )

    quote_mint = normalize_quote_mint(_state_pubkey(state, "quote_mint"))
    if quote_mint == WSOL_MINT:
        tiers = snapshot.config.regular_tiers
    elif quote_mint == USDC_MINT:
        tiers = snapshot.config.stable_tiers
    else:
        raise _FeeConfigError(f"Unsupported Pump.fun quote mint: {quote_mint}")

    selected = tiers[0]
    for tier in reversed(tiers):
        if market_cap_raw >= tier.market_cap_threshold_raw:
            selected = tier
            break
    fees = selected.fees
    if fees.lp_fee_bps != 0:
        raise _FeeConfigError("Pump.fun bonding-curve LP fee must be zero")

    creator = _state_pubkey(state, "creator")
    if creator == Pubkey.default():
        fees = PumpFees(
            lp_fee_bps=fees.lp_fee_bps,
            protocol_fee_bps=fees.protocol_fee_bps,
            creator_fee_bps=0,
        )
    return fees, market_cap_raw


def _fee_amount(amount_raw: int, basis_points: int) -> int:
    if basis_points == 0:
        return 0
    return _ceil_div(amount_raw * basis_points, _MAX_BASIS_POINTS)


def quote_buy_exact_in(
    state: Mapping[str, object],
    spendable_raw: int,
    snapshot: PumpFeeSnapshot,
) -> PumpQuote:
    """Quote base-token output for an exact raw quote budget."""
    spendable_raw = _require_raw_u64(spendable_raw, "spendable_raw", positive=True)
    fees, market_cap_raw = _select_fees(state, snapshot)
    total_fee_bps = fees.protocol_fee_bps + fees.creator_fee_bps
    net_quote_raw = (
        spendable_raw * _MAX_BASIS_POINTS // (_MAX_BASIS_POINTS + total_fee_bps)
    )
    protocol_fee_raw = _fee_amount(net_quote_raw, fees.protocol_fee_bps)
    creator_fee_raw = _fee_amount(net_quote_raw, fees.creator_fee_bps)
    overshoot = net_quote_raw + protocol_fee_raw + creator_fee_raw - spendable_raw
    if overshoot > 0:
        net_quote_raw -= overshoot
    if net_quote_raw <= 1:
        raise _FeeConfigError("spendable quote amount produces no executable input")

    virtual_token_reserves = _require_raw_u64(
        state.get("virtual_token_reserves"),
        "virtual_token_reserves",
        positive=True,
    )
    virtual_quote_reserves = _require_raw_u64(
        state.get("virtual_quote_reserves"),
        "virtual_quote_reserves",
        positive=True,
    )
    real_token_reserves = _require_raw_u64(
        state.get("real_token_reserves"), "real_token_reserves"
    )
    effective_quote_raw = net_quote_raw - 1
    amount_out_raw = min(
        (
            effective_quote_raw
            * virtual_token_reserves
            // (virtual_quote_reserves + effective_quote_raw)
        ),
        real_token_reserves,
    )
    if amount_out_raw <= 0:
        raise _FeeConfigError("Pump.fun buy quote produced no token output")

    return PumpQuote(
        amount_in_raw=spendable_raw,
        amount_out_raw=amount_out_raw,
        gross_quote_raw=spendable_raw,
        net_quote_raw=net_quote_raw,
        protocol_fee_raw=protocol_fee_raw,
        creator_fee_raw=creator_fee_raw,
        market_cap_raw=market_cap_raw,
        fees=fees,
        config_digest=snapshot.config.digest,
    )


def quote_buy_exact_out(
    state: Mapping[str, object],
    token_amount_raw: int,
    snapshot: PumpFeeSnapshot,
) -> PumpQuote:
    """Quote raw quote input required for an exact base-token output."""
    token_amount_raw = _require_raw_u64(
        token_amount_raw, "token_amount_raw", positive=True
    )
    fees, market_cap_raw = _select_fees(state, snapshot)
    virtual_token_reserves = _require_raw_u64(
        state.get("virtual_token_reserves"),
        "virtual_token_reserves",
        positive=True,
    )
    virtual_quote_reserves = _require_raw_u64(
        state.get("virtual_quote_reserves"),
        "virtual_quote_reserves",
        positive=True,
    )
    real_token_reserves = _require_raw_u64(
        state.get("real_token_reserves"), "real_token_reserves"
    )
    if token_amount_raw >= virtual_token_reserves:
        raise _FeeConfigError("token amount must be below virtual token reserves")
    if token_amount_raw > real_token_reserves:
        raise _FeeConfigError("token amount exceeds real token reserves")

    net_quote_raw = (
        _ceil_div(
            token_amount_raw * virtual_quote_reserves,
            virtual_token_reserves - token_amount_raw,
        )
        + 1
    )
    protocol_fee_raw = _fee_amount(net_quote_raw, fees.protocol_fee_bps)
    creator_fee_raw = _fee_amount(net_quote_raw, fees.creator_fee_bps)
    required_quote_raw = net_quote_raw + protocol_fee_raw + creator_fee_raw
    _require_raw_u64(required_quote_raw, "required_quote_raw", positive=True)
    return PumpQuote(
        amount_in_raw=required_quote_raw,
        amount_out_raw=token_amount_raw,
        gross_quote_raw=required_quote_raw,
        net_quote_raw=net_quote_raw,
        protocol_fee_raw=protocol_fee_raw,
        creator_fee_raw=creator_fee_raw,
        market_cap_raw=market_cap_raw,
        fees=fees,
        config_digest=snapshot.config.digest,
    )


def quote_sell_exact_in(
    state: Mapping[str, object],
    token_amount_raw: int,
    snapshot: PumpFeeSnapshot,
) -> PumpQuote:
    """Quote raw quote output for an exact base-token input."""
    token_amount_raw = _require_raw_u64(
        token_amount_raw, "token_amount_raw", positive=True
    )
    fees, market_cap_raw = _select_fees(state, snapshot)
    virtual_token_reserves = _require_raw_u64(
        state.get("virtual_token_reserves"),
        "virtual_token_reserves",
        positive=True,
    )
    virtual_quote_reserves = _require_raw_u64(
        state.get("virtual_quote_reserves"),
        "virtual_quote_reserves",
        positive=True,
    )
    real_quote_reserves = _require_raw_u64(
        state.get("real_quote_reserves"), "real_quote_reserves"
    )
    gross_quote_raw = (
        token_amount_raw
        * virtual_quote_reserves
        // (virtual_token_reserves + token_amount_raw)
    )
    if gross_quote_raw > real_quote_reserves:
        raise _FeeConfigError("sell quote exceeds real quote reserves")
    protocol_fee_raw = _fee_amount(gross_quote_raw, fees.protocol_fee_bps)
    creator_fee_raw = _fee_amount(gross_quote_raw, fees.creator_fee_bps)
    net_quote_raw = gross_quote_raw - protocol_fee_raw - creator_fee_raw
    if net_quote_raw <= 0:
        raise _FeeConfigError("Pump.fun sell quote produced no quote output")

    return PumpQuote(
        amount_in_raw=token_amount_raw,
        amount_out_raw=net_quote_raw,
        gross_quote_raw=gross_quote_raw,
        net_quote_raw=net_quote_raw,
        protocol_fee_raw=protocol_fee_raw,
        creator_fee_raw=creator_fee_raw,
        market_cap_raw=market_cap_raw,
        fees=fees,
        config_digest=snapshot.config.digest,
    )


class PumpFeeSchedule:
    """Keep a fresh Pump fee account snapshot attested by the fee program."""

    _TRADE_SIZE_PROBES = (1, 1_000_000_000)

    def __init__(  # noqa: PLR0913
        self,
        client: SolanaClient,
        fee_config: Pubkey,
        config_program: Pubkey,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = 2.0,
        max_observation_age_seconds: float = 10.0,
        reattest_after_seconds: float = 60.0,
        max_attestation_age_seconds: float = 120.0,
    ) -> None:
        self._client = client
        self._fee_config = fee_config
        self._config_program = config_program
        self._clock = clock
        self._poll_interval_seconds = poll_interval_seconds
        self._max_observation_age_seconds = max_observation_age_seconds
        self._reattest_after_seconds = reattest_after_seconds
        self._max_attestation_age_seconds = max_attestation_age_seconds
        self._simulation_payer = PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
        self._snapshot: PumpFeeSnapshot | None = None
        self._validation_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Fetch, attest, and begin polling the authoritative fee account."""
        async with self._lifecycle_lock:
            if self._closed:
                raise _FeeAttestationError("Pump fee schedule is closed")
            if self._poll_task is not None and not self._poll_task.done():
                self.require_snapshot()
                return

            account = await self._client.get_account_info(
                self._fee_config,
                commitment="processed",
            )
            if not isinstance(account, Account):
                raise _FeeAttestationError("FeeConfig RPC returned an invalid account")
            await self.accept_account(account)
            self._poll_task = asyncio.create_task(
                self._poll(),
                name="pump-fee-schedule-poll",
            )

    async def close(self) -> None:
        """Stop the background fee-account poll."""
        async with self._lifecycle_lock:
            self._closed = True
            task = self._poll_task
            self._poll_task = None
            if task is None:
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def accept_account(self, account: Account) -> PumpFeeSnapshot:
        """Validate an observed account and promote it only after attestation."""
        async with self._validation_lock:
            try:
                config = decode_fee_config_account(account)
            except (TypeError, ValueError):
                self._snapshot = None
                raise

            now = self._clock()
            current = self._snapshot
            same_digest = current is not None and current.config.digest == config.digest
            needs_attestation = (
                not same_digest
                or current is None
                or now - current.attested_at >= self._reattest_after_seconds
            )
            if needs_attestation:
                try:
                    await self._attest(config)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._snapshot = None
                    raise
                attested_at = now
            else:
                attested_at = current.attested_at

            snapshot = PumpFeeSnapshot(
                config=config,
                observed_at=now,
                attested_at=attested_at,
            )
            self._snapshot = snapshot
            return snapshot

    def require_snapshot(self) -> PumpFeeSnapshot:
        """Return a currently fresh snapshot or fail closed."""
        snapshot = self._snapshot
        if snapshot is None:
            raise _FeeAttestationError("Pump fee schedule has no validated snapshot")
        now = self._clock()
        observation_age = now - snapshot.observed_at
        attestation_age = now - snapshot.attested_at
        if observation_age < 0 or attestation_age < 0:
            raise _FeeAttestationError("Pump fee schedule clock moved backwards")
        if observation_age > self._max_observation_age_seconds:
            raise _FeeAttestationError("Pump fee schedule observation is stale")
        if attestation_age > self._max_attestation_age_seconds:
            raise _FeeAttestationError("Pump fee schedule attestation has expired")
        return snapshot

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_seconds)
            try:
                account = await self._client.get_account_info(
                    self._fee_config,
                    commitment="processed",
                )
                if not isinstance(account, Account):
                    raise _FeeAttestationError(  # noqa: TRY301
                        "FeeConfig RPC returned an invalid account"
                    )
                await self.accept_account(account)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pump fee schedule refresh failed")

    async def _attest(self, config: PumpFeeConfig) -> None:
        probes = (
            (False, config.regular_tiers),
            (True, config.stable_tiers),
        )
        for is_usdc, tiers in probes:
            for tier in tiers:
                for trade_size_raw in self._TRADE_SIZE_PROBES:
                    actual = await self._simulate_get_fees(
                        market_cap_raw=tier.market_cap_threshold_raw,
                        trade_size_raw=trade_size_raw,
                        is_usdc=is_usdc,
                    )
                    if actual != tier.fees:
                        raise _FeeAttestationError(
                            "Pump fee attestation mismatch: "
                            f"expected {tier.fees}, got {actual}"
                        )

    async def _simulate_get_fees(
        self,
        *,
        market_cap_raw: int,
        trade_size_raw: int,
        is_usdc: bool,
    ) -> PumpFees:
        data = (
            GET_FEES_DISCRIMINATOR
            + bytes((1,))
            + market_cap_raw.to_bytes(16, "little")
            + struct.pack("<Q", trade_size_raw)
            + struct.pack("<?", is_usdc)
        )
        instruction = Instruction(
            PumpFunAddresses.FEE_PROGRAM,
            data,
            [
                AccountMeta(
                    pubkey=self._fee_config,
                    is_signer=False,
                    is_writable=False,
                ),
                AccountMeta(
                    pubkey=self._config_program,
                    is_signer=False,
                    is_writable=False,
                ),
            ],
        )
        blockhash = await self._client.get_latest_blockhash()
        message = Message.new_with_blockhash(
            [instruction],
            self._simulation_payer,
            blockhash,
        )
        transaction = Transaction.populate(message, [Signature.default()])
        response = await self._client.post_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "simulateTransaction",
                "params": [
                    base64.b64encode(bytes(transaction)).decode(),
                    {
                        "encoding": "base64",
                        "sigVerify": False,
                        "replaceRecentBlockhash": True,
                        "commitment": "processed",
                    },
                ],
            }
        )
        if not isinstance(response, dict):
            # post_rpc returns None only after exhausting transport retries.
            raise _FeeAttestationUnavailable(
                "Pump fee attestation RPC returned no response"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise _FeeAttestationError("Pump fee attestation RPC omitted its result")
        value = result.get("value")
        if not isinstance(value, dict) or value.get("err") is not None:
            raise _FeeAttestationError("Pump fee attestation simulation failed")
        logs = value.get("logs")
        if not isinstance(logs, list) or any(
            not isinstance(line, str) for line in logs
        ):
            raise _FeeAttestationError("Pump fee attestation returned invalid logs")

        prefix = f"Program return: {PumpFunAddresses.FEE_PROGRAM} "
        returned = [
            line.removeprefix(prefix) for line in logs if line.startswith(prefix)
        ]
        if len(returned) != 1:
            raise _FeeAttestationError("Pump fee attestation return value is missing")
        try:
            decoded = base64.b64decode(returned[0], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _FeeAttestationError(
                "Pump fee attestation return value is not valid base64"
            ) from exc
        if len(decoded) != _GET_FEES_RETURN_SIZE:
            raise _FeeAttestationError(
                "Pump fee attestation return value must be exactly 24 bytes"
            )
        return _validate_fees(
            PumpFees(*struct.unpack("<QQQ", decoded)),
            "get_fees return",
        )
