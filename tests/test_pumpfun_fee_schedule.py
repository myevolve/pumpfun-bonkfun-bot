from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from collections import deque
from typing import Any

import pytest
from solders.account import Account
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction

from core.pubkeys import USDC_MINT, WSOL_MINT
from platforms.pumpfun.address_provider import PumpFunAddresses
from platforms.pumpfun.fee_schedule import (
    FEE_CONFIG_DISCRIMINATOR,
    GET_FEES_DISCRIMINATOR,
    PumpFees,
    PumpFeeSchedule,
    PumpFeeSnapshot,
    decode_fee_config_account,
    quote_buy_exact_in,
    quote_buy_exact_out,
    quote_sell_exact_in,
)

FeeValues = tuple[int, int, int]
TierValues = tuple[int, FeeValues]


def _encode_fees(fees: FeeValues) -> bytes:
    return struct.pack("<QQQ", *fees)


def _encode_tiers(tiers: tuple[TierValues, ...]) -> bytes:
    encoded = bytearray(struct.pack("<I", len(tiers)))
    for threshold, fees in tiers:
        encoded += threshold.to_bytes(16, "little")
        encoded += _encode_fees(fees)
    return bytes(encoded)


def _fee_account(
    *,
    regular: tuple[TierValues, ...] = (
        (0, (0, 95, 30)),
        (100_000, (0, 80, 20)),
    ),
    stable: tuple[TierValues, ...] = ((0, (0, 50, 10)),),
    flat: FeeValues = (25, 90, 20),
    owner: Pubkey = PumpFunAddresses.FEE_PROGRAM,
    discriminator: bytes = FEE_CONFIG_DISCRIMINATOR,
    tail: bytes = bytes(128),
) -> Account:
    data = bytearray(discriminator)
    data += bytes([253])
    data += bytes(Pubkey.new_unique())
    data += _encode_fees(flat)
    data += _encode_tiers(regular)
    data += _encode_tiers(stable)
    data += tail
    return Account(1, bytes(data), owner, False, 0)


def _snapshot(
    *,
    regular: tuple[TierValues, ...] = (
        (0, (0, 95, 30)),
        (100_000, (0, 80, 20)),
    ),
    stable: tuple[TierValues, ...] = ((0, (0, 50, 10)),),
) -> PumpFeeSnapshot:
    return PumpFeeSnapshot(
        config=decode_fee_config_account(_fee_account(regular=regular, stable=stable)),
        observed_at=100.0,
        attested_at=100.0,
    )


def _curve_state(
    *,
    quote_mint: Pubkey = WSOL_MINT,
    creator: Pubkey | None = None,
    real_token_reserves: int = 50_000,
) -> dict[str, object]:
    return {
        "virtual_token_reserves": 100_000,
        "virtual_quote_reserves": 10_000,
        "real_token_reserves": real_token_reserves,
        "real_quote_reserves": 5_000,
        "token_total_supply": 1_000_000,
        "complete": False,
        "creator": creator if creator is not None else Pubkey.new_unique(),
        "quote_mint": quote_mint,
    }


def test_decode_fee_config_account_preserves_both_tier_schedules() -> None:
    account = _fee_account()

    config = decode_fee_config_account(account)

    assert config.flat_fees == PumpFees(25, 90, 20)
    assert config.regular_tiers[0].market_cap_threshold_raw == 0
    assert config.regular_tiers[1].market_cap_threshold_raw == 100_000
    assert config.regular_tiers[1].fees == PumpFees(0, 80, 20)
    assert config.stable_tiers[0].fees == PumpFees(0, 50, 10)
    assert config.digest == hashlib.sha256(account.data).hexdigest()


def test_decode_fee_config_account_rejects_wrong_owner() -> None:
    with pytest.raises(ValueError, match="owner"):
        decode_fee_config_account(_fee_account(owner=Pubkey.new_unique()))


def test_decode_fee_config_account_rejects_wrong_discriminator() -> None:
    with pytest.raises(ValueError, match="discriminator"):
        decode_fee_config_account(_fee_account(discriminator=bytes(8)))


def test_decode_fee_config_account_rejects_truncated_tier_vector() -> None:
    account = _fee_account(tail=b"")
    truncated = Account(
        account.lamports,
        account.data[:-1],
        account.owner,
        account.executable,
        account.rent_epoch,
    )

    with pytest.raises(ValueError, match="truncated"):
        decode_fee_config_account(truncated)


def test_decode_fee_config_account_rejects_unknown_nonzero_tail() -> None:
    with pytest.raises(ValueError, match="trailing"):
        decode_fee_config_account(_fee_account(tail=bytes(127) + b"\x01"))


@pytest.mark.parametrize("field", ["regular", "stable"])
def test_decode_fee_config_account_rejects_empty_tier_schedules(field: str) -> None:
    kwargs = {field: ()}

    with pytest.raises(ValueError, match="must not be empty"):
        decode_fee_config_account(_fee_account(**kwargs))


def test_decode_fee_config_account_rejects_unsorted_thresholds() -> None:
    regular = (
        (100_000, (0, 95, 30)),
        (99_999, (0, 80, 20)),
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        decode_fee_config_account(_fee_account(regular=regular))


@pytest.mark.parametrize(
    "fees",
    [
        (0, 10_001, 0),
        (0, 0, 10_001),
        (0, 6_000, 5_000),
    ],
)
def test_decode_fee_config_account_rejects_impossible_basis_points(
    fees: FeeValues,
) -> None:
    with pytest.raises(ValueError, match="basis points"):
        decode_fee_config_account(_fee_account(regular=((0, fees),)))


def test_buy_exact_in_selects_regular_tier_at_threshold() -> None:
    quote = quote_buy_exact_in(_curve_state(), 10_000, _snapshot())

    assert quote.market_cap_raw == 100_000
    assert quote.fees == PumpFees(0, 80, 20)
    assert quote.net_quote_raw == 9_900
    assert quote.protocol_fee_raw == 80
    assert quote.creator_fee_raw == 20
    assert quote.amount_out_raw == 49_746


def test_buy_exact_in_selects_stable_tiers_for_usdc() -> None:
    quote = quote_buy_exact_in(
        _curve_state(quote_mint=USDC_MINT),
        10_000,
        _snapshot(),
    )

    assert quote.fees == PumpFees(0, 50, 10)
    assert quote.net_quote_raw == 9_940
    assert quote.amount_out_raw == 49_847


def test_buy_exact_in_omits_creator_fee_for_default_creator() -> None:
    quote = quote_buy_exact_in(
        _curve_state(creator=Pubkey.default()),
        10_000,
        _snapshot(),
    )

    assert quote.fees == PumpFees(0, 80, 0)
    assert quote.creator_fee_raw == 0


def test_buy_exact_in_corrects_separately_rounded_fee_overshoot() -> None:
    quote = quote_buy_exact_in(
        _curve_state(),
        5,
        _snapshot(regular=((0, (0, 95, 30)),)),
    )

    assert quote.net_quote_raw == 3
    assert quote.protocol_fee_raw == 1
    assert quote.creator_fee_raw == 1
    assert quote.amount_out_raw == 19


def test_buy_exact_in_caps_output_by_real_token_reserves() -> None:
    quote = quote_buy_exact_in(
        _curve_state(real_token_reserves=1_000),
        10_000,
        _snapshot(),
    )

    assert quote.amount_out_raw == 1_000


def test_buy_exact_out_reports_required_quote_with_separate_rounding() -> None:
    quote = quote_buy_exact_out(_curve_state(), 1_000, _snapshot())

    assert quote.net_quote_raw == 103
    assert quote.protocol_fee_raw == 1
    assert quote.creator_fee_raw == 1
    assert quote.amount_in_raw == 105
    assert quote.amount_out_raw == 1_000


def test_buy_exact_out_rejects_target_at_virtual_reserve() -> None:
    with pytest.raises(ValueError, match="virtual token reserves"):
        quote_buy_exact_out(_curve_state(), 100_000, _snapshot())


def test_sell_exact_in_subtracts_separately_rounded_fees() -> None:
    quote = quote_sell_exact_in(_curve_state(), 1_000, _snapshot())

    assert quote.gross_quote_raw == 99
    assert quote.protocol_fee_raw == 1
    assert quote.creator_fee_raw == 1
    assert quote.amount_out_raw == 97


@pytest.mark.parametrize(
    "quote_function,args",
    [
        (quote_buy_exact_in, (_curve_state(), 10_000)),
        (quote_buy_exact_out, (_curve_state(), 1_000)),
        (quote_sell_exact_in, (_curve_state(), 1_000)),
    ],
)
def test_bonding_curve_quotes_reject_nonzero_lp_fees(
    quote_function: object,
    args: tuple[object, int],
) -> None:
    snapshot = _snapshot(regular=((0, (1, 80, 20)),))

    with pytest.raises(ValueError, match="LP fee"):
        quote_function(*args, snapshot)  # type: ignore[operator]


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeFeeClient:
    def __init__(self, account: Account) -> None:
        self.account = account
        self.account_calls = 0
        self.post_calls: list[dict[str, Any]] = []
        self.results: deque[PumpFees | BaseException | dict[str, Any]] = deque()
        self.block_after_first_account = False
        self.second_account_started = asyncio.Event()
        self.second_account_cancelled = asyncio.Event()

    async def get_account_info(
        self, pubkey: Pubkey, commitment: str | None = None
    ) -> Account:
        del pubkey, commitment
        self.account_calls += 1
        if self.block_after_first_account and self.account_calls > 1:
            self.second_account_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.second_account_cancelled.set()
                raise
        return self.account

    async def get_latest_blockhash(self) -> Hash:
        return Hash.default()

    async def post_rpc(self, body: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append(body)
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, dict):
            return result
        encoded = base64.b64encode(
            struct.pack(
                "<QQQ",
                result.lp_fee_bps,
                result.protocol_fee_bps,
                result.creator_fee_bps,
            )
        ).decode()
        return {
            "result": {
                "value": {
                    "err": None,
                    "logs": [
                        f"Program return: {PumpFunAddresses.FEE_PROGRAM} {encoded}"
                    ],
                }
            }
        }

    def queue_attestation(self, account: Account) -> None:
        config = decode_fee_config_account(account)
        for tier in (*config.regular_tiers, *config.stable_tiers):
            self.results.extend((tier.fees, tier.fees))


def _fee_schedule(
    client: _FakeFeeClient,
    clock: _FakeClock,
    **kwargs: float,
) -> PumpFeeSchedule:
    return PumpFeeSchedule(
        client,
        PumpFunAddresses.find_fee_config(),
        PumpFunAddresses.PROGRAM,
        clock=clock,
        poll_interval_seconds=3_600.0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_snapshot_start_requires_matching_program_attestation() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)

    await schedule.start()
    snapshot = schedule.require_snapshot()
    await schedule.close()

    assert snapshot.config.digest == hashlib.sha256(account.data).hexdigest()
    assert snapshot.observed_at == 0.0
    assert snapshot.attested_at == 0.0
    assert len(client.post_calls) == 6
    first_body = client.post_calls[0]
    assert first_body["method"] == "simulateTransaction"
    transaction = Transaction.from_bytes(
        base64.b64decode(first_body["params"][0], validate=True)
    )
    assert (
        transaction.message.account_keys[0] == PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    )
    assert transaction.signatures == [Signature.default()]
    instruction_data = bytes(transaction.message.instructions[0].data)
    assert instruction_data == (
        GET_FEES_DISCRIMINATOR
        + struct.pack("<?", True)
        + (0).to_bytes(16, "little")
        + struct.pack("<Q", 1)
        + struct.pack("<?", False)
    )


@pytest.mark.asyncio
async def test_snapshot_same_digest_refreshes_without_reattestation() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)
    await schedule.start()
    original = schedule.require_snapshot()

    clock.advance(5.0)
    refreshed = await schedule.accept_account(account)
    await schedule.close()

    assert refreshed.observed_at == 5.0
    assert refreshed.attested_at == original.attested_at
    assert len(client.post_calls) == 6


@pytest.mark.asyncio
async def test_unchanged_snapshot_is_periodically_reattested() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)
    await schedule.start()

    clock.advance(61.0)
    client.queue_attestation(account)
    refreshed = await schedule.accept_account(account)
    await schedule.close()

    assert refreshed.observed_at == 61.0
    assert refreshed.attested_at == 61.0
    assert len(client.post_calls) == 12


@pytest.mark.asyncio
async def test_changed_snapshot_cannot_promote_on_attestation_mismatch() -> None:
    account = _fee_account()
    changed = _fee_account(regular=((0, (0, 70, 10)),))
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)
    await schedule.start()
    client.results.append(PumpFees(0, 95, 30))

    with pytest.raises(RuntimeError, match="attestation mismatch"):
        await schedule.accept_account(changed)
    with pytest.raises(RuntimeError, match="no validated"):
        schedule.require_snapshot()
    await schedule.close()


@pytest.mark.asyncio
async def test_changed_snapshot_promotes_after_matching_attestation() -> None:
    account = _fee_account()
    changed = _fee_account(regular=((0, (0, 70, 10)),))
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)
    await schedule.start()
    client.queue_attestation(changed)
    clock.advance(1.0)

    promoted = await schedule.accept_account(changed)
    await schedule.close()

    assert promoted.config.digest == hashlib.sha256(changed.data).hexdigest()
    assert promoted.attested_at == 1.0


@pytest.mark.asyncio
async def test_attestation_rejects_trade_size_dependent_fees() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    config = decode_fee_config_account(account)
    client.results.extend(
        (
            config.regular_tiers[0].fees,
            PumpFees(0, 1, 1),
        )
    )
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)

    with pytest.raises(RuntimeError, match="attestation mismatch"):
        await schedule.start()
    with pytest.raises(RuntimeError, match="no validated"):
        schedule.require_snapshot()


@pytest.mark.asyncio
async def test_snapshot_rejects_stale_observation() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(client, clock)
    await schedule.start()
    clock.advance(10.1)

    with pytest.raises(RuntimeError, match="observation is stale"):
        schedule.require_snapshot()
    await schedule.close()


@pytest.mark.asyncio
async def test_snapshot_rejects_expired_attestation() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    clock = _FakeClock()
    schedule = _fee_schedule(
        client,
        clock,
        max_observation_age_seconds=1_000.0,
    )
    await schedule.start()
    clock.advance(120.1)

    with pytest.raises(RuntimeError, match="attestation has expired"):
        schedule.require_snapshot()
    await schedule.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_refresh_poll() -> None:
    account = _fee_account()
    client = _FakeFeeClient(account)
    client.queue_attestation(account)
    client.block_after_first_account = True
    clock = _FakeClock()
    schedule = PumpFeeSchedule(
        client,
        PumpFunAddresses.find_fee_config(),
        PumpFunAddresses.PROGRAM,
        clock=clock,
        poll_interval_seconds=0.0,
    )
    await schedule.start()
    await asyncio.wait_for(client.second_account_started.wait(), timeout=1.0)

    await schedule.close()

    assert client.second_account_cancelled.is_set()
