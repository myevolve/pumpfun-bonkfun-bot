# Assertions and private lifecycle fixtures deliberately exercise numeric boundaries.
# ruff: noqa: S101, SLF001, PLR2004

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from solders.pubkey import Pubkey

from core.client import (
    RpcUnavailableError,
    TransactionStatus,
    TransactionSubmissionUnknown,
)
from core.execution_policy import ExecutionBlocked, ExecutionMode, ExecutionPolicy
from core.pubkeys import USDC_MINT, SystemAddresses
from core.transaction_ledger import EvidencePersistenceError
from core.transaction_state import TransactionOutcome
from interfaces.core import Platform, TokenInfo
from monitoring.trade_flow import FlowRules, FlowSignal, TradeQueue
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFees,
    PumpFeeSnapshot,
    PumpFeeTier,
    _FeeAttestationError,
    _FeeAttestationUnavailable,
)
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import ExitReason, Position
from trading.universal_trader import UniversalTrader

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolate_durable_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """These lifecycle unit fixtures omit persistence; the evidence verifier uses it."""
    monkeypatch.setattr(UniversalTrader, "_record_trade_evidence", Mock())


def _token(platform: Platform) -> TokenInfo:
    mint = Pubkey.new_unique()
    pool = mint if platform is Platform.LETS_BONK else Pubkey.new_unique()
    return TokenInfo(
        name="Token",
        symbol="TOK",
        uri="",
        mint=mint,
        platform=platform,
        bonding_curve=pool if platform is Platform.PUMP_FUN else None,
        pool_state=pool if platform is Platform.LETS_BONK else None,
        quote_mint=SystemAddresses.WSOL_MINT,
        quote_token_program_id=SystemAddresses.TOKEN_PROGRAM,
        creator=Pubkey.new_unique() if platform is Platform.PUMP_FUN else None,
        virtual_token_reserves=100_000_000,
        virtual_quote_reserves=40_000_000,
        real_token_reserves=50_000_000,
        token_total_supply=1_000_000_000,
        token_program_id=SystemAddresses.TOKEN_PROGRAM,
        base_decimals=6,
        quote_decimals=9,
    )


def test_recovery_token_round_trip_preserves_creation_timestamp() -> None:
    token = _token(Platform.LETS_BONK)
    token.creation_timestamp = 123.5

    payload = UniversalTrader._token_to_dict(token)
    recovered = UniversalTrader.token_from_dict(payload)

    assert payload["creation_timestamp"] == 123.5
    assert recovered.creation_timestamp == 123.5


def test_recovery_round_trip_preserves_pool_extension_requirement() -> None:
    token = _token(Platform.PUMP_FUN)
    token.pool_needs_extension = True

    payload = UniversalTrader._token_to_dict(token)
    recovered = UniversalTrader.token_from_dict(payload)

    assert payload["pool_needs_extension"] is True
    assert recovered.pool_needs_extension is True


def test_pumpfun_fast_path_requires_consistent_quote_program_metadata() -> None:
    token = _token(Platform.PUMP_FUN)
    token.state_from_event = True
    token.curve_complete = False
    buyer = object.__new__(PlatformAwareBuyer)
    buyer.trust_create_event = True

    token.quote_token_program_id = None
    assert buyer._can_skip_refresh(token) is False

    token.quote_token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
    assert buyer._can_skip_refresh(token) is False

    token.quote_token_program_id = SystemAddresses.TOKEN_PROGRAM
    token.quote_mint = None
    assert buyer._can_skip_refresh(token) is False

    token.quote_mint = SystemAddresses.WSOL_MINT
    assert buyer._can_skip_refresh(token) is True


@pytest.mark.parametrize(
    "field",
    (
        "virtual_token_reserves",
        "virtual_quote_reserves",
        "real_token_reserves",
        "token_total_supply",
    ),
)
def test_pumpfun_fast_path_requires_complete_event_reserves(field: str) -> None:
    token = _token(Platform.PUMP_FUN)
    token.state_from_event = True
    token.curve_complete = False
    buyer = object.__new__(PlatformAwareBuyer)
    buyer.trust_create_event = True
    setattr(token, field, None)

    assert buyer._can_skip_refresh(token) is False


def test_recovery_round_trip_preserves_large_event_reserve_integers() -> None:
    token = _token(Platform.PUMP_FUN)
    values = {
        "virtual_token_reserves": 9_007_199_254_740_999,
        "virtual_quote_reserves": 9_007_199_254_740_997,
        "real_token_reserves": 9_007_199_254_740_995,
        "token_total_supply": 9_007_199_254_741_001,
    }
    for field, value in values.items():
        setattr(token, field, value)

    payload = json.loads(json.dumps(UniversalTrader._token_to_dict(token)))
    recovered = UniversalTrader.token_from_dict(payload)

    for field, value in values.items():
        assert payload[field] == value
        assert getattr(recovered, field) == value


@pytest.mark.parametrize("malformed", [True, -1, 1.5, "1", 2**64])
def test_recovery_rejects_malformed_event_reserves(malformed: object) -> None:
    payload = UniversalTrader._token_to_dict(_token(Platform.PUMP_FUN))
    payload["virtual_token_reserves"] = malformed

    with pytest.raises(ValueError, match="virtual_token_reserves"):
        UniversalTrader.token_from_dict(payload)


@pytest.mark.parametrize(
    "malformed_timestamp",
    ["123.5", True, 123, float("nan"), float("inf")],
)
def test_recovery_token_rejects_malformed_creation_timestamp(
    malformed_timestamp: object,
) -> None:
    payload = UniversalTrader._token_to_dict(_token(Platform.LETS_BONK))
    payload["creation_timestamp"] = malformed_timestamp

    with pytest.raises(ValueError, match="creation_timestamp"):
        UniversalTrader.token_from_dict(payload)


def test_recovery_token_serialization_rejects_malformed_creation_timestamp() -> None:
    token = _token(Platform.LETS_BONK)
    token.creation_timestamp = "123.5"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="creation_timestamp"):
        UniversalTrader._token_to_dict(token)


def test_malformed_journal_does_not_partially_activate_positions(tmp_path) -> None:
    trader = object.__new__(UniversalTrader)
    wallet = Pubkey.new_unique()
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        position_id="buy-signature",
    )
    pending_payload = UniversalTrader._token_to_dict(_token(Platform.LETS_BONK))
    pending_payload["creation_timestamp"] = "not-a-timestamp"
    journal_path = tmp_path / "positions.json"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet),
                "platform": Platform.LETS_BONK.value,
                "positions": {
                    token_key: {
                        "token": UniversalTrader._token_to_dict(token),
                        "position": position.to_dict(),
                    }
                },
                "unresolved_buys": {},
                "pending_tokens": [pending_payload],
            }
        ),
        encoding="utf-8",
    )
    trader.wallet = SimpleNamespace(pubkey=wallet)
    trader.platform = Platform.LETS_BONK
    trader._journal_path = journal_path
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader.traded_mints = set()
    trader.traded_token_programs = {}

    with pytest.raises(RuntimeError, match="Cannot safely load recovery journal"):
        trader._load_recovery_journal()

    assert trader._active_positions == {}
    assert trader._unresolved_buys == {}
    assert trader._pending_recovery_tokens == []
    assert trader._reserved_mints == set()
    assert trader.traded_mints == set()
    assert trader.traded_token_programs == {}


@pytest.mark.parametrize(
    "ledger_record",
    [
        SimpleNamespace(signature="buy-signature", fee_lamports=40_000),
        None,
    ],
    ids=["fee-evidence", "missing-fee-evidence"],
)
def test_legacy_take_profit_journal_requires_and_persists_fee_evidence(
    tmp_path,
    ledger_record,
) -> None:
    wallet = Pubkey.new_unique()
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        take_profit_percentage=0.1,
        position_id="buy-signature",
    )
    assert position.next_exit_attempt() == 1
    assert position.next_exit_attempt() == 2
    legacy_position = position.to_dict()
    legacy_position.pop("buy_fee_lamports")
    legacy_position.pop("take_profit_net_quote_raw")
    legacy_position.pop("charged_exit_fee_lamports")
    journal_path = tmp_path / "positions.json"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet),
                "platform": Platform.LETS_BONK.value,
                "positions": {
                    token_key: {
                        "token": UniversalTrader._token_to_dict(token),
                        "position": legacy_position,
                    }
                },
                "unresolved_buys": {},
                "pending_tokens": [],
            }
        ),
        encoding="utf-8",
    )
    trader = object.__new__(UniversalTrader)
    trader.wallet = SimpleNamespace(pubkey=wallet)
    trader.platform = Platform.LETS_BONK
    trader._journal_path = journal_path
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader.traded_mints = set()
    trader.traded_token_programs = {}
    buy_intent = UniversalTrader._buy_intent_id(token)
    reverted_sell_record = SimpleNamespace(
        signature="reverted-sell",
        fee_lamports=7_000,
    )
    trader.transaction_ledger = SimpleNamespace(
        get_active_submission_record=lambda intent_id: (
            ledger_record if intent_id == buy_intent else None
        ),
        get_latest_submission_record=lambda intent_id: (
            reverted_sell_record if intent_id == "sell:buy-signature:1" else None
        ),
        get_outcome=lambda signature: TransactionOutcome(
            TransactionStatus.REVERTED,
            signature,
            "reverted",
        ),
    )

    if ledger_record is None:
        with pytest.raises(RuntimeError, match="Cannot safely load recovery journal"):
            trader._load_recovery_journal()
        assert trader._active_positions == {}
        return

    trader._load_recovery_journal()

    recovered = trader._active_positions[token_key][1]
    assert recovered.buy_fee_lamports == 40_000
    assert recovered.take_profit_net_quote_raw == 154_000
    assert recovered.charged_exit_fee_lamports == 7_000
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    persisted_position = persisted["positions"][token_key]["position"]
    assert persisted_position["buy_fee_lamports"] == 40_000
    assert persisted_position["take_profit_net_quote_raw"] == 154_000
    assert persisted_position["charged_exit_fee_lamports"] == 7_000


def test_recovery_journal_rejects_cross_platform_active_position(
    tmp_path,
) -> None:
    trader = object.__new__(UniversalTrader)
    wallet = Pubkey.new_unique()
    token = _token(Platform.PUMP_FUN)
    token_key = str(token.mint)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        position_id="buy-signature",
    )
    journal_path = tmp_path / "positions.json"
    journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet),
                "platform": Platform.LETS_BONK.value,
                "positions": {
                    token_key: {
                        "token": UniversalTrader._token_to_dict(token),
                        "position": position.to_dict(),
                    }
                },
                "unresolved_buys": {},
                "pending_tokens": [],
            }
        ),
        encoding="utf-8",
    )
    trader.wallet = SimpleNamespace(pubkey=wallet)
    trader.platform = Platform.LETS_BONK
    trader._journal_path = journal_path
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader.traded_mints = set()
    trader.traded_token_programs = {}

    with pytest.raises(RuntimeError, match="Cannot safely load recovery journal"):
        trader._load_recovery_journal()

    assert trader._active_positions == {}
    assert trader._reserved_mints == set()


@pytest.mark.asyncio
async def test_restart_joins_pending_buy_and_sell_intents_before_rebuild() -> None:
    trader = object.__new__(UniversalTrader)
    buy_token = _token(Platform.LETS_BONK)
    sell_token = _token(Platform.LETS_BONK)
    sell_position = Position.create_from_buy_result(
        mint=sell_token.mint,
        symbol=sell_token.symbol,
        entry_price=1.0,
        quantity=2.0,
        position_id="confirmed-buy",
    )
    sell_position.mark_exit_intent("sell-intent", ExitReason.STOP_LOSS, 0.8)
    buy_intent = UniversalTrader._buy_intent_id(buy_token)
    records = {
        buy_intent: SimpleNamespace(signature="buy-signature", fee_lamports=5_000),
        "sell-intent": SimpleNamespace(
            signature="sell-signature",
            fee_lamports=5_000,
        ),
    }
    trader.transaction_ledger = SimpleNamespace(
        get_active_submission_record=lambda intent_id: records.get(intent_id)
    )
    trader._pending_recovery_tokens = [buy_token]
    trader._unresolved_buys = {}
    trader._active_positions = {str(sell_token.mint): (sell_token, sell_position)}
    trader._reserved_mints = set()
    journal_writes: list[bool] = []
    trader._write_recovery_journal = lambda: journal_writes.append(True)

    trader._hydrate_submission_recovery()

    assert trader._pending_recovery_tokens == []
    assert trader._unresolved_buys[str(buy_token.mint)]["signature"] == "buy-signature"
    assert sell_position.pending_exit_signature == "sell-signature"
    assert journal_writes == [True]

    trader.solana_client = SimpleNamespace(
        recover_active_submission=AsyncMock(
            side_effect=[
                TransactionSubmissionUnknown("buy-signature", "response lost"),
                "sell-signature",
            ]
        )
    )
    await trader._resume_ledger_bound_submissions()

    assert [
        call.args[0]
        for call in trader.solana_client.recover_active_submission.await_args_list
    ] == [buy_intent, "sell-intent"]


def test_hydration_recovers_reverted_fee_without_journaled_signature() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        take_profit_percentage=0.1,
        position_id="buy-signature",
    )
    assert position.next_exit_attempt() == 1
    intent_id = "sell:buy-signature:1"
    position.mark_exit_intent(intent_id, ExitReason.TAKE_PROFIT, 2.0)
    terminal_record = SimpleNamespace(
        signature="reverted-sell",
        fee_lamports=7_000,
    )
    trader.transaction_ledger = SimpleNamespace(
        get_active_submission_record=lambda candidate: None,
        get_latest_submission_record=lambda candidate: (
            terminal_record if candidate == intent_id else None
        ),
        get_outcome=lambda signature: TransactionOutcome(
            TransactionStatus.REVERTED,
            signature,
            "reverted",
        ),
    )
    trader._pending_recovery_tokens = []
    trader._unresolved_buys = {}
    trader._active_positions = {str(token.mint): (token, position)}
    trader._reserved_mints = set()
    journal_writes: list[bool] = []
    trader._write_recovery_journal = lambda: journal_writes.append(True)

    trader._hydrate_submission_recovery()

    assert position.pending_exit_intent_id is None
    assert position.charged_exit_fee_lamports == 7_000
    assert journal_writes == [True]


class _AddressProviderWithoutCreatorVault:
    derive_creator_vault = None

    def derive_pool_address(
        self, mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        return mint

    def derive_base_vault(
        self, mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        return Pubkey.find_program_address(
            [b"base-vault", bytes(mint)], Pubkey.default()
        )[0]

    def derive_quote_vault(
        self, mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        return Pubkey.find_program_address(
            [b"quote-vault", bytes(mint)], Pubkey.default()
        )[0]


class _InstructionBuilder:
    async def build_sell_instruction(self, *args, **kwargs) -> list:
        return []

    def get_required_accounts_for_sell(self, *args, **kwargs) -> list:
        return []

    def get_sell_compute_unit_limit(self, override: int | None) -> int | None:
        return override

    async def build_buy_instruction(self, *args, **kwargs) -> list:
        return []

    def get_required_accounts_for_buy(self, *args, **kwargs) -> list:
        return []

    def get_buy_compute_unit_limit(self, override: int | None) -> int | None:
        return override


class _RecordingSellBuilder(_InstructionBuilder):
    def __init__(self) -> None:
        self.minimum_amount_out: int | None = None

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: object,
    ) -> list:
        del token_info, user, amount_in, address_provider
        self.minimum_amount_out = minimum_amount_out
        return []


class _CurveManager:
    async def get_pool_state_and_token_program(
        self, pool: Pubkey, mint: Pubkey, commitment: str | None = None
    ) -> tuple[dict, Pubkey]:
        provider = _AddressProviderWithoutCreatorVault()
        quote_mint = SystemAddresses.WSOL_MINT
        return (
            {
                "complete": False,
                "is_tradeable": True,
                "status_name": "funding",
                "quote_mint": quote_mint,
                "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
                "base_mint": mint,
                "base_token_program": SystemAddresses.TOKEN_PROGRAM,
                "pool_address": pool,
                "base_vault": provider.derive_base_vault(mint, quote_mint),
                "quote_vault": provider.derive_quote_vault(mint, quote_mint),
                "global_config": Pubkey.new_unique(),
                "platform_config": Pubkey.new_unique(),
                "creator": Pubkey.new_unique(),
                "base_decimals": 6,
                "quote_decimals": 9,
            },
            SystemAddresses.TOKEN_PROGRAM,
        )

    async def calculate_sell_amount_out(
        self,
        pool: Pubkey,
        amount: int,
        *,
        pool_state: dict | None = None,
    ) -> int:
        del pool, amount, pool_state
        return 1_000_000_000


class _Client:
    def __init__(self) -> None:
        self.submission_kwargs: dict = {}

    async def build_and_send_transaction(self, *args, **kwargs) -> str:
        self.submission_kwargs = kwargs
        return "sell-signature"

    async def confirm_transaction_outcome(self, signature: str) -> SimpleNamespace:
        return SimpleNamespace(
            status=TransactionStatus.SUCCESS,
            slot=123,
            error=None,
        )

    async def get_sell_transaction_details(
        self, signature: str, quote_mint: Pubkey, owner: Pubkey
    ) -> int:
        return 1_000_000_000


class _SuccessfulBuyClient(_Client):
    def __init__(self) -> None:
        self.submission_kwargs: dict = {}

    async def build_and_send_transaction(self, *args, **kwargs) -> str:
        self.submission_kwargs = kwargs
        return "buy-signature"

    async def get_buy_transaction_details(
        self,
        signature: str,
        mint: Pubkey,
        destination: Pubkey,
        **kwargs,
    ) -> tuple[int, int]:
        return 2_000_000, 1_000_000

    async def get_buyer_pre_token_balance(
        self, signature: str, mint: Pubkey, owner: Pubkey
    ) -> int:
        self.baseline_lookups = getattr(self, "baseline_lookups", 0) + 1
        return 0


class _FastFeeSchedule:
    def __init__(
        self,
        snapshot: PumpFeeSnapshot,
        error: RuntimeError | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error

    def require_snapshot(self) -> PumpFeeSnapshot:
        if self.error is not None:
            raise self.error
        return self.snapshot


class _RecordingBuyBuilder(_InstructionBuilder):
    buy_uses_exact_output = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: object,
    ) -> list:
        self.calls.append(
            {
                "token_info": token_info,
                "user": user,
                "amount_in": amount_in,
                "minimum_amount_out": minimum_amount_out,
                "address_provider": address_provider,
            }
        )
        return []


def _pump_fee_manager(
    *,
    regular: PumpFees,
    stable: PumpFees,
    error: RuntimeError | None = None,
) -> PumpFunCurveManager:
    config = PumpFeeConfig(
        bump=1,
        admin=Pubkey.new_unique(),
        flat_fees=regular,
        regular_tiers=(PumpFeeTier(0, regular),),
        stable_tiers=(PumpFeeTier(0, stable),),
        exotic_flat_fees=PumpFees(0, 95, 30),
        digest="fast-path-fees",
    )
    snapshot = PumpFeeSnapshot(
        config=config,
        observed_at=1.0,
        attested_at=1.0,
    )
    return PumpFunCurveManager(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        fee_schedule=_FastFeeSchedule(snapshot, error),  # type: ignore[arg-type]
    )


class _UnknownSubmissionClient(_Client):
    async def get_account_info(self, address: Pubkey) -> None:
        raise ValueError("account does not exist")

    async def build_and_send_transaction(self, *args, **kwargs) -> str:
        raise TransactionSubmissionUnknown(
            "deterministic-signature", "RPC response lost"
        )


class _PriorityFees:
    async def calculate_priority_fee(self, accounts: list) -> int:
        return 0


def test_take_profit_target_includes_confirmed_buy_fee() -> None:
    position = Position.create_from_buy_result(
        mint=Pubkey.new_unique(),
        symbol="TOK",
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        take_profit_percentage=0.1,
        position_id="buy-signature",
    )

    assert position.take_profit_net_quote_raw == 154_000


def test_max_hold_exit_overrides_take_profit_signal() -> None:
    position = Position.create_from_buy_result(
        mint=Pubkey.new_unique(),
        symbol="TOK",
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        take_profit_percentage=0.1,
        max_hold_time=10,
        position_id="buy-signature",
    )
    position.entry_time = datetime.now(UTC) - timedelta(seconds=11)

    assert position.should_exit(2.0) == (True, ExitReason.MAX_HOLD_TIME)


@pytest.mark.asyncio
async def test_sell_skips_submission_when_net_target_is_not_met(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _Client()
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(
        token,
        token_amount=1.0,
        token_price=1.0,
        take_profit_net_quote_raw=1_000_000_000,
    )

    assert result.success is False
    assert result.status == "target_not_met"
    assert result.fee_lamports == 5_000
    assert client.submission_kwargs == {}


@pytest.mark.asyncio
async def test_sell_slippage_floor_cannot_undercut_net_target(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    builder = _RecordingSellBuilder()
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=builder,
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _Client()
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(
        token,
        token_amount=1.0,
        token_price=1.0,
        take_profit_net_quote_raw=900_000_000,
    )

    assert result.success is True
    assert builder.minimum_amount_out == 900_005_000


@pytest.mark.asyncio
async def test_take_profit_sell_includes_cleanup_fee_in_net_target(
    monkeypatch,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        take_profit_percentage=0.1,
        position_id="buy-signature",
        account_balance_baseline_raw=0,
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=AsyncMock(return_value=2.0))
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=True,
                platform=Platform.LETS_BONK,
                tx_signature="sell-signature",
                amount=2.0,
                amount_raw=2_000_000,
                price=2.0,
                status=TransactionStatus.SUCCESS.value,
            )
        )
    )
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace(hard_cap=200_000)
    trader.cleanup_mode = "after_sell"
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    monkeypatch.setattr(
        "trading.universal_trader.stage_cleanup_after_sell",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        AsyncMock(return_value=None),
    )

    await trader._monitor_position_until_exit(token, position)

    assert (
        trader.seller.execute.await_args.kwargs["take_profit_net_quote_raw"] == 159_000
    )


def _net_roi_position(
    token: TokenInfo,
    *,
    max_hold_time: int | None = None,
) -> Position:
    return Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        take_profit_percentage=0.1,
        max_hold_time=max_hold_time,
        position_id="buy-signature",
    )


def _net_roi_monitor_trader(
    token: TokenInfo,
    *,
    prices: list[float],
    sell_results: list[TradeResult],
    pending_outcome: object | None = None,
) -> UniversalTrader:
    trader = object.__new__(UniversalTrader)
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = 3
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(
            calculate_token_price=AsyncMock(side_effect=prices)
        )
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(side_effect=sell_results),
    )
    trader.solana_client = (
        SimpleNamespace(
            confirm_transaction_outcome=AsyncMock(return_value=pending_outcome)
        )
        if pending_outcome is not None
        else SimpleNamespace()
    )
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace(hard_cap=200_000)
    trader.cleanup_mode = "disabled"
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    return trader


@pytest.mark.asyncio
async def test_take_profit_retry_carries_charged_revert_fee() -> None:
    token = _token(Platform.LETS_BONK)
    position = _net_roi_position(token)
    trader = _net_roi_monitor_trader(
        token,
        prices=[2.0, 2.0],
        sell_results=[
            TradeResult(
                success=False,
                platform=token.platform,
                tx_signature="reverted-sell",
                error_message="reverted",
                fee_lamports=7_000,
                status=TransactionStatus.REVERTED.value,
            ),
            TradeResult(
                success=True,
                platform=token.platform,
                tx_signature="successful-sell",
                amount=2.0,
                amount_raw=2_000_000,
                price=2.0,
                status=TransactionStatus.SUCCESS.value,
            ),
        ],
    )

    await trader._monitor_position_until_exit(token, position)

    targets = [
        call.kwargs["take_profit_net_quote_raw"]
        for call in trader.seller.execute.await_args_list
    ]
    assert targets == [154_000, 161_000]
    assert position.charged_exit_fee_lamports == 7_000
    assert Position.from_dict(position.to_dict()).charged_exit_fee_lamports == 7_000


@pytest.mark.asyncio
async def test_pending_reverted_sell_fee_is_carried_into_retry() -> None:
    token = _token(Platform.LETS_BONK)
    position = _net_roi_position(token)
    position.mark_exit_intent(
        "sell:buy-signature:1",
        ExitReason.TAKE_PROFIT,
        2.0,
    )
    position.mark_exit_pending(
        "pending-sell",
        ExitReason.TAKE_PROFIT,
        fee_lamports=7_000,
    )
    position = Position.from_dict(position.to_dict())
    assert position.pending_exit_fee_lamports == 7_000
    trader = _net_roi_monitor_trader(
        token,
        prices=[2.0],
        sell_results=[
            TradeResult(
                success=True,
                platform=token.platform,
                tx_signature="successful-sell",
                amount=2.0,
                amount_raw=2_000_000,
                price=2.0,
                status=TransactionStatus.SUCCESS.value,
            )
        ],
        pending_outcome=SimpleNamespace(
            status=TransactionStatus.REVERTED,
            slot=123,
            error="reverted",
        ),
    )

    await trader._monitor_position_until_exit(token, position)

    assert (
        trader.seller.execute.await_args.kwargs["take_profit_net_quote_raw"] == 161_000
    )
    assert position.charged_exit_fee_lamports == 7_000


@pytest.mark.asyncio
async def test_recovered_safety_exit_replaces_stale_take_profit_intent() -> None:
    token = _token(Platform.LETS_BONK)
    position = _net_roi_position(token, max_hold_time=10)
    position.entry_time = datetime.now(UTC) - timedelta(seconds=11)
    assert position.next_exit_attempt() == 1
    position.mark_exit_intent(
        "sell:buy-signature:1",
        ExitReason.TAKE_PROFIT,
        2.0,
    )
    trader = _net_roi_monitor_trader(
        token,
        prices=[0.5],
        sell_results=[
            TradeResult(
                success=True,
                platform=token.platform,
                tx_signature="safety-sell",
                amount=2.0,
                amount_raw=2_000_000,
                price=0.5,
                status=TransactionStatus.SUCCESS.value,
            )
        ],
    )

    await trader._monitor_position_until_exit(token, position)

    sell_kwargs = trader.seller.execute.await_args.kwargs
    assert sell_kwargs["intent_id"] == "sell:buy-signature:2"
    assert sell_kwargs["take_profit_net_quote_raw"] is None
    assert position.exit_reason is ExitReason.MAX_HOLD_TIME


def test_expected_wallet_is_validated_before_client_setup(monkeypatch) -> None:
    actual_wallet = Pubkey.new_unique()
    expected_wallet = Pubkey.new_unique()
    client_creations: list[bool] = []
    monkeypatch.setattr(
        "trading.universal_trader.Wallet",
        lambda private_key: SimpleNamespace(pubkey=actual_wallet),
    )

    def create_client(*args, **kwargs):
        client_creations.append(True)
        raise AssertionError("client setup must not run for a mismatched wallet")

    monkeypatch.setattr("trading.universal_trader.SolanaClient", create_client)

    with pytest.raises(ExecutionBlocked, match="does not match signer wallet"):
        UniversalTrader(
            rpc_endpoint="offline",
            wss_endpoint="offline",
            private_key="unused",
            buy_amount=0.1,
            buy_slippage=0.1,
            sell_slippage=0.1,
            execution_policy=ExecutionPolicy(expected_wallet=str(expected_wallet)),
        )

    assert client_creations == []


@pytest.mark.asyncio
async def test_letsbonk_sell_does_not_require_creator_vault_capability(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    provider = _AddressProviderWithoutCreatorVault()
    implementations = SimpleNamespace(
        address_provider=provider,
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _Client()
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.success is True
    assert token.creator is not None
    assert token.creator_vault is None
    assert "skip_preflight" not in client.submission_kwargs


@pytest.mark.asyncio
async def test_pumpfun_completed_curve_routes_sell_to_pumpswap(monkeypatch) -> None:
    token = _token(Platform.PUMP_FUN)
    migrated_pool = Pubkey.new_unique()
    pool_creator = Pubkey.new_unique()
    creator_vault = Pubkey.new_unique()
    protocol_fee_recipient = Pubkey.new_unique()
    buyback_fee_recipient = Pubkey.new_unique()
    builder = _InstructionBuilder()
    builder.build_sell_instruction = AsyncMock(return_value=[])
    curve_manager = SimpleNamespace(
        get_sell_state_and_token_program=AsyncMock(
            return_value=(
                {
                    "venue": "pumpswap",
                    "complete": True,
                    "is_tradeable": True,
                    "status_name": "pumpswap",
                    "pool_address": migrated_pool,
                    "base_vault": Pubkey.new_unique(),
                    "quote_vault": Pubkey.new_unique(),
                    "global_config": Pubkey.new_unique(),
                    "platform_config": Pubkey.new_unique(),
                    "creator": pool_creator,
                    "creator_vault": creator_vault,
                    "protocol_fee_recipient": protocol_fee_recipient,
                    "buyback_fee_recipient": buyback_fee_recipient,
                    "pool_needs_extension": True,
                    "quote_mint": SystemAddresses.WSOL_MINT,
                    "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
                    "base_decimals": 6,
                    "quote_decimals": 9,
                },
                SystemAddresses.TOKEN_PROGRAM,
            )
        ),
        calculate_sell_amount_out=AsyncMock(return_value=1_000_000_000),
    )
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=builder,
        curve_manager=curve_manager,
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _Client()
    seller = PlatformAwareSeller(
        client,
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.success is True
    curve_manager.get_sell_state_and_token_program.assert_awaited_once()
    assert token.curve_complete is True
    assert token.pool_status == "pumpswap"
    assert token.pool_state == migrated_pool
    assert token.creator == pool_creator
    assert token.creator_vault == creator_vault
    assert token.protocol_fee_recipient == protocol_fee_recipient
    assert token.buyback_fee_recipient == buyback_fee_recipient
    assert token.pool_needs_extension is True
    builder.build_sell_instruction.assert_awaited_once()


@pytest.mark.parametrize(
    "failing_method",
    ["_load_recovery_journal", "_hydrate_submission_recovery"],
)
def test_constructor_releases_persistence_resources_after_recovery_failure(
    monkeypatch,
    tmp_path,
    failing_method: str,
) -> None:
    wallet = Pubkey.new_unique()

    class LedgerSpy:
        instances: list[LedgerSpy] = []

        def __init__(self, path) -> None:
            self.path = path
            self.closed = False
            self.instances.append(self)

        def record_evidence_profile(self, *_args: object) -> str:
            return "d" * 64

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "trading.universal_trader.Wallet",
        lambda private_key: SimpleNamespace(pubkey=wallet),
    )
    monkeypatch.setattr("trading.universal_trader.TransactionLedger", LedgerSpy)
    monkeypatch.setattr(
        "trading.universal_trader.SolanaClient",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.PriorityFeeManager",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.get_platform_implementations",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.ListenerFactory.create_listener",
        lambda *args, **kwargs: SimpleNamespace(),
    )

    def fail_recovery(_trader: UniversalTrader) -> None:
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(UniversalTrader, failing_method, fail_recovery)

    journal_path = tmp_path / "positions.json"
    ledger_path = tmp_path / "ledger.sqlite3"
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        expected_wallet=str(wallet),
        max_trade_quote_raw=1,
        max_total_fee_lamports=1,
        risk_session_id="test-session",
        max_session_quote_raw=100,
        max_session_fee_lamports=100,
    )

    with pytest.raises(RuntimeError, match="recovery failed"):
        UniversalTrader(
            rpc_endpoint="offline",
            wss_endpoint="offline",
            private_key="unused",
            buy_amount=0.1,
            buy_slippage=0.1,
            sell_slippage=0.1,
            execution_policy=policy,
            position_journal_path=journal_path,
            transaction_ledger_path=ledger_path,
        )

    assert LedgerSpy.instances[-1].closed is True
    lock_path = journal_path.with_suffix(".json.lock")
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def test_constructor_defers_ledger_until_runtime_components_are_valid(
    monkeypatch,
) -> None:
    wallet = Pubkey.new_unique()
    ledger_opened = False

    def open_ledger(_path):
        nonlocal ledger_opened
        ledger_opened = True
        return SimpleNamespace(close=lambda: None)

    def fail_listener(**_kwargs):
        raise RuntimeError("listener setup failed")

    monkeypatch.setattr(
        "trading.universal_trader.Wallet",
        lambda private_key: SimpleNamespace(pubkey=wallet),
    )
    monkeypatch.setattr("trading.universal_trader.TransactionLedger", open_ledger)
    monkeypatch.setattr(
        "trading.universal_trader.SolanaClient",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.PriorityFeeManager",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.get_platform_implementations",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.ListenerFactory.create_listener",
        fail_listener,
    )
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        expected_wallet=str(wallet),
        max_trade_quote_raw=1,
        max_total_fee_lamports=1,
        risk_session_id="test-session",
        max_session_quote_raw=100,
        max_session_fee_lamports=100,
    )

    with pytest.raises(RuntimeError, match="listener setup failed"):
        UniversalTrader(
            rpc_endpoint="offline",
            wss_endpoint="offline",
            private_key="unused",
            buy_amount=0.1,
            buy_slippage=0.1,
            sell_slippage=0.1,
            execution_policy=policy,
        )

    assert ledger_opened is False


def test_dry_run_constructor_does_not_resolve_live_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wallet = Pubkey.new_unique()

    def reject_ledger_resolution(_wallet: Pubkey) -> None:
        raise AssertionError(  # noqa: TRY003
            "dry-run construction must not inspect live ledgers"
        )

    monkeypatch.setattr(
        "trading.universal_trader.Wallet",
        lambda _private_key: SimpleNamespace(pubkey=wallet),
    )
    monkeypatch.setattr(
        "trading.universal_trader.resolve_transaction_ledger_path",
        reject_ledger_resolution,
    )
    monkeypatch.setattr(
        "trading.universal_trader.SolanaClient",
        lambda *_args, **_kwargs: SimpleNamespace(ledger=None),
    )
    monkeypatch.setattr(
        "trading.universal_trader.PriorityFeeManager",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.PlatformAwareBuyer",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.PlatformAwareSeller",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.get_platform_implementations",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "trading.universal_trader.ListenerFactory.create_listener",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    trader = UniversalTrader(
        rpc_endpoint="offline",
        wss_endpoint="offline",
        private_key="unused",
        buy_amount=0.1,
        buy_slippage=0.1,
        sell_slippage=0.1,
        execution_policy=ExecutionPolicy(mode=ExecutionMode.DRY_RUN),
        position_journal_path=tmp_path / "positions.json",
    )

    assert trader.transaction_ledger is None


@pytest.mark.asyncio
async def test_pumpfun_sell_fails_clearly_without_required_creator_vault_capability(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    monkeypatch.setattr(
        "trading.platform_aware._read_pool_state_with_retry",
        AsyncMock(
            return_value=(
                {
                    "creator": str(Pubkey.new_unique()),
                    "complete": False,
                    "quote_mint": SystemAddresses.WSOL_MINT,
                    "base_decimals": 6,
                    "quote_decimals": 9,
                },
                SystemAddresses.TOKEN_PROGRAM,
            )
        ),
    )
    seller = PlatformAwareSeller(
        _Client(),
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.success is False
    assert "requires creator-vault derivation" in result.error_message


@pytest.mark.asyncio
async def test_unknown_sell_submission_preserves_signature_and_raw_amount(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    seller = PlatformAwareSeller(
        _UnknownSubmissionClient(),
        SimpleNamespace(pubkey=Pubkey.new_unique(), keypair=object()),
        _PriorityFees(),
    )

    result = await seller.execute(token, token_amount=1.0, token_price=None)

    assert result.unresolved is True
    assert result.tx_signature == "deterministic-signature"
    assert result.amount_raw == 1_000_000


@pytest.mark.asyncio
async def test_unknown_buy_submission_preserves_signature_and_recovery_accounting(
    monkeypatch,
) -> None:
    token = _token(Platform.LETS_BONK)
    token.state_from_event = True
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=object(),
        get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
    )
    buyer = PlatformAwareBuyer(
        _UnknownSubmissionClient(),
        wallet,
        _PriorityFees(),
        amount=0.1,
        slippage=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
    )

    result = await buyer.execute(token)

    assert result.unresolved is True
    assert result.tx_signature == "deterministic-signature"
    assert result.amount_raw == 2_000_000
    assert result.quote_amount_raw == 110_000_000
    assert result.account_balance_baseline_raw == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["buy", "sell"])
async def test_evidence_failure_aborts_trade_without_retry(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    token = _token(Platform.LETS_BONK)
    token.state_from_event = True
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda _platform, _client: implementations,
    )
    client = _SuccessfulBuyClient()
    client.execution_policy = SimpleNamespace(mode=ExecutionMode.LIVE)
    client.build_and_send_transaction = AsyncMock(return_value="evidence-signature")
    failure = EvidencePersistenceError("evidence storage unavailable")
    client.confirm_transaction_outcome = AsyncMock(side_effect=failure)
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=object(),
        get_associated_token_address=lambda _mint, _program: Pubkey.new_unique(),
    )
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        _PriorityFees(),
        amount=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
    )
    seller = PlatformAwareSeller(client, wallet, _PriorityFees())

    with pytest.raises(EvidencePersistenceError) as caught:
        if action == "buy":
            await buyer.execute(token)
        else:
            await seller.execute(token, token_amount=1.0, token_price=1.0)
    assert caught.value is failure
    client.build_and_send_transaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_rpc_buy_takes_baseline_from_receipt_after_confirmation(
    monkeypatch,
) -> None:
    """Live 2026-09-03 (wind): the zero-RPC path skipped the pre-buy balance
    read, so cleanup reported ownership_unproven and left the ATA rent."""
    token = _token(Platform.LETS_BONK)
    token.state_from_event = True
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=_InstructionBuilder(),
        curve_manager=_CurveManager(),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _SuccessfulBuyClient()
    client.execution_policy = SimpleNamespace(mode=ExecutionMode.LIVE)
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=object(),
        get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
    )
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        _PriorityFees(),
        amount=0.1,
        slippage=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
    )

    result = await buyer.execute(token)

    assert result.success is True
    assert result.account_balance_baseline_raw == 0
    assert client.baseline_lookups == 1


@pytest.mark.asyncio
async def test_pumpfun_usdc_buy_has_no_native_receipt_destinations(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    token.quote_mint = SystemAddresses.USDC_MINT
    token.quote_decimals = 6
    token.state_from_event = True
    token.curve_complete = False
    builder = _RecordingBuyBuilder()
    curve_manager = _pump_fee_manager(
        regular=PumpFees(0, 10_000, 0),
        stable=PumpFees(0, 0, 0),
    )
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=builder,
        curve_manager=curve_manager,
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    client = _SuccessfulBuyClient()
    wallet = SimpleNamespace(
        pubkey=Pubkey.new_unique(),
        keypair=object(),
        get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
    )
    client.execution_policy = SimpleNamespace(mode=ExecutionMode.LIVE)
    buyer = PlatformAwareBuyer(
        client,
        wallet,
        _PriorityFees(),
        amount=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
        quote_amounts={SystemAddresses.USDC_MINT: 1.0},
    )

    result = await buyer.execute(token)

    assert result.success is True
    assert client.submission_kwargs["receipt_destinations"] is None
    assert builder.calls[0]["minimum_amount_out"] == 2_000_000
    assert "skip_preflight" not in client.submission_kwargs


@pytest.mark.asyncio
async def test_pumpfun_buy_enforces_allowlist_after_authoritative_refresh(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    token.quote_mint = None
    token.state_from_event = False
    builder = _RecordingBuyBuilder()
    curve_manager = _pump_fee_manager(
        regular=PumpFees(0, 80, 20),
        stable=PumpFees(0, 50, 10),
    )
    pool_state = {
        "complete": False,
        "quote_mint": SystemAddresses.USDC_MINT,
        "base_decimals": 6,
        "quote_decimals": 6,
        "virtual_token_reserves": 100_000_000,
        "virtual_quote_reserves": 40_000_000,
        "real_token_reserves": 50_000_000,
        "token_total_supply": 1_000_000_000,
        "_pump_fee_snapshot": curve_manager.fee_schedule.snapshot,
    }
    monkeypatch.setattr(
        "trading.platform_aware._read_pool_state_with_retry",
        AsyncMock(
            return_value=(pool_state, SystemAddresses.TOKEN_PROGRAM),
        ),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: SimpleNamespace(
            address_provider=_AddressProviderWithoutCreatorVault(),
            instruction_builder=builder,
            curve_manager=curve_manager,
        ),
    )
    buyer = PlatformAwareBuyer(
        _SuccessfulBuyClient(),
        SimpleNamespace(
            pubkey=Pubkey.new_unique(),
            keypair=object(),
            get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
        ),
        _PriorityFees(),
        amount=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
        quote_amounts={SystemAddresses.USDC_MINT: 1.0},
        allowed_quote_mints={SystemAddresses.WSOL_MINT},
    )

    result = await buyer.execute(token)

    assert result.success is False
    assert result.error_message == (
        f"Quote mint {SystemAddresses.USDC_MINT} is not allowed"
    )
    assert token.quote_mint == SystemAddresses.USDC_MINT
    assert builder.calls == []


@pytest.mark.asyncio
async def test_pumpfun_fast_buy_skips_target_above_fee_aware_quote_cap(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    token.state_from_event = True
    token.curve_complete = False
    builder = _RecordingBuyBuilder()
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=builder,
        curve_manager=_pump_fee_manager(
            regular=PumpFees(0, 10_000, 0),
            stable=PumpFees(0, 0, 0),
        ),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    buyer = PlatformAwareBuyer(
        _SuccessfulBuyClient(),
        SimpleNamespace(
            pubkey=Pubkey.new_unique(),
            keypair=object(),
            get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
        ),
        _PriorityFees(),
        amount=0.0005,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
    )

    result = await buyer.execute(token)

    assert result.success is False
    assert "exceeds configured quote cap" in result.error_message
    assert builder.calls == []


@pytest.mark.asyncio
async def test_pumpfun_fast_buy_refuses_expired_fee_snapshot_without_rpc(
    monkeypatch,
) -> None:
    token = _token(Platform.PUMP_FUN)
    token.state_from_event = True
    token.curve_complete = False
    builder = _RecordingBuyBuilder()
    implementations = SimpleNamespace(
        address_provider=_AddressProviderWithoutCreatorVault(),
        instruction_builder=builder,
        curve_manager=_pump_fee_manager(
            regular=PumpFees(0, 95, 30),
            stable=PumpFees(0, 50, 10),
            error=RuntimeError("Pump fee schedule attestation has expired"),
        ),
    )
    monkeypatch.setattr(
        "trading.platform_aware.get_platform_implementations",
        lambda platform, client: implementations,
    )
    buyer = PlatformAwareBuyer(
        _SuccessfulBuyClient(),
        SimpleNamespace(
            pubkey=Pubkey.new_unique(),
            keypair=object(),
            get_associated_token_address=lambda mint, program: Pubkey.new_unique(),
        ),
        _PriorityFees(),
        amount=0.1,
        extreme_fast_token_amount=2,
        extreme_fast_mode=True,
    )

    result = await buyer.execute(token)

    assert result.success is False
    assert "attestation has expired" in result.error_message
    assert builder.calls == []


def _lifecycle_trader(*, yolo_mode: bool) -> UniversalTrader:
    trader = object.__new__(UniversalTrader)
    trader.platform = Platform.LETS_BONK
    trader.exit_strategy = "manual"
    trader.yolo_mode = yolo_mode
    trader.match_string = None
    trader.bro_address = None
    trader.token_wait_timeout = 1
    trader.token_queue = asyncio.Queue()
    trader._queue_lock = asyncio.Lock()
    trader._shutdown_event = asyncio.Event()
    trader._position_tasks = set()
    trader._position_monitor_tasks = set()
    trader._fatal_monitor_errors = asyncio.Queue()
    trader._unresolved_buy_state_changed = asyncio.Event()
    trader._active_positions = {}
    trader._pending_recovery_tokens = []
    trader._reserved_mints = set()
    trader._unresolved_buys = {}
    trader._inflight_tokens = {}
    trader.processed_tokens = set()
    trader.token_timestamps = {}
    trader.solana_client = SimpleNamespace(get_health=AsyncMock(return_value="ok"))
    trader.execution_policy = ExecutionPolicy(mode=ExecutionMode.DRY_RUN)
    trader._write_recovery_journal = lambda: None

    async def process_queue() -> None:
        await asyncio.Event().wait()

    async def reconcile() -> None:
        return None

    trader._process_token_queue = process_queue
    trader._reconcile_unresolved_buys = reconcile
    return trader


@pytest.mark.asyncio
async def test_resume_only_start_skips_listener_and_monitors_recovered_position() -> (
    None
):
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    monitored = asyncio.Event()

    async def monitor_position() -> None:
        monitored.set()

    def schedule_position_monitor(_token: TokenInfo, _position: object) -> None:
        task = asyncio.create_task(monitor_position())
        trader._position_monitor_tasks.add(task)

    position = SimpleNamespace(
        is_active=True,
        take_profit_price=1.0,
        stop_loss_price=None,
        max_hold_time=None,
    )
    trader._active_positions[str(token.mint)] = (token, position)
    trader._schedule_position_monitor = schedule_position_monitor
    trader._wait_for_token = AsyncMock(
        side_effect=AssertionError("resume-only mode started a token listener")
    )
    trader._cleanup_resources = AsyncMock()
    trader._resume_staged_cleanups = AsyncMock(
        side_effect=AssertionError("resume-only mode consumed staged cleanup")
    )

    await trader.start(resume_only=True)

    assert monitored.is_set()
    trader._wait_for_token.assert_not_awaited()
    trader._cleanup_resources.assert_awaited_once()
    trader._resume_staged_cleanups.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_only_rejects_unresolved_buy_before_submission_recovery() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    position = SimpleNamespace(
        is_active=True,
        take_profit_price=1.0,
        stop_loss_price=None,
        max_hold_time=None,
    )
    trader._active_positions[str(token.mint)] = (token, position)
    trader._unresolved_buys[str(Pubkey.new_unique())] = {}
    trader._resume_ledger_bound_submissions = AsyncMock()
    trader._cleanup_resources = AsyncMock()

    with pytest.raises(RuntimeError, match="pending or unresolved buys"):
        await trader.start(resume_only=True)

    trader._resume_ledger_bound_submissions.assert_not_awaited()
    trader._cleanup_resources.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_only_rejects_position_without_automatic_exit() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    position = SimpleNamespace(
        is_active=True,
        take_profit_price=None,
        stop_loss_price=None,
        max_hold_time=None,
    )
    trader._active_positions[str(token.mint)] = (token, position)
    trader._resume_ledger_bound_submissions = AsyncMock()
    trader._cleanup_resources = AsyncMock()

    with pytest.raises(RuntimeError, match="automatic exit"):
        await trader.start(resume_only=True)

    trader._resume_ledger_bound_submissions.assert_not_awaited()
    trader._cleanup_resources.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_trade_waits_for_unresolved_buy_before_shutdown() -> None:
    """A late-confirming buy must stay under reconciliation in one-shot mode."""
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    reconciliation_started = asyncio.Event()
    release_reconciliation = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def wait_for_token() -> TokenInfo:
        return token

    async def handle_token(_token_info: TokenInfo) -> bool:
        trader._unresolved_buys[token_key] = {
            "token": token,
            "signature": "late-signature",
        }
        reconciliation_started.set()
        return False

    async def reconcile() -> None:
        await reconciliation_started.wait()
        await release_reconciliation.wait()
        trader._unresolved_buys.pop(token_key)
        trader._unresolved_buy_state_changed.set()

    async def cleanup() -> None:
        cleanup_started.set()

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._reconcile_unresolved_buys = reconcile
    trader._cleanup_resources = cleanup

    start_task = asyncio.create_task(trader.start())
    await asyncio.wait_for(reconciliation_started.wait(), timeout=0.1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not cleanup_started.is_set()
    assert not start_task.done()

    release_reconciliation.set()
    await asyncio.wait_for(start_task, timeout=0.1)

    assert cleanup_started.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "execution_mode",
    [ExecutionMode.DRY_RUN, ExecutionMode.LIVE],
)
async def test_pump_fee_attestation_failure_prevents_processing_and_listening(
    execution_mode: ExecutionMode,
) -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    trader.platform = Platform.PUMP_FUN
    trader.execution_policy = SimpleNamespace(mode=execution_mode)
    events: list[str] = []

    async def prepare_live_execution() -> None:
        events.append("prepare")
        raise RuntimeError("fee attestation failed")

    async def process_queue() -> None:
        events.append("process")

    async def listen(*args, **kwargs) -> None:
        events.append("listen")

    async def cleanup() -> None:
        events.append("cleanup")

    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(
            prepare_live_execution=prepare_live_execution,
        )
    )
    trader._process_token_queue = process_queue
    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="fee attestation failed"):
        await trader.start()

    assert events == ["prepare", "cleanup"]


@pytest.mark.asyncio
async def test_start_propagates_fatal_reconciliation_error_after_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def reconcile() -> None:
        raise RuntimeError("reconciliation failed")

    async def listen(*args, **kwargs) -> None:
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader._reconcile_unresolved_buys = reconcile
    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    # 0.1 s expired under full-suite CPU contention before the immediate
    # raise could surface; the deadline only guards against a hang.
    with pytest.raises(RuntimeError, match="reconciliation failed"):
        await asyncio.wait_for(trader.start(), timeout=1)

    assert events == ["cleanup"]


def _held_position_monitor(trader: UniversalTrader) -> tuple[asyncio.Event, list]:
    """Install a monitor that finishes only when released; returns the gate."""
    release = asyncio.Event()
    events: list[str] = []
    token = _token(Platform.LETS_BONK)
    position = SimpleNamespace(
        is_active=True,
        take_profit_price=1.0,
        stop_loss_price=None,
        max_hold_time=None,
    )

    async def monitor() -> None:
        events.append("monitor started")
        await release.wait()
        events.append("monitor finished")

    def schedule(_token: TokenInfo, _position: object) -> None:
        task = asyncio.create_task(monitor())
        trader._position_monitor_tasks.add(task)
        trader._position_tasks.add(task)

    trader._active_positions[str(token.mint)] = (token, position)
    trader._schedule_position_monitor = schedule
    return release, events


@pytest.mark.asyncio
async def test_yolo_listener_failure_holds_positions_until_monitors_exit() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    release, events = _held_position_monitor(trader)

    async def listen(*args, **kwargs) -> None:
        events.append("listener died")
        raise RuntimeError("reconnect limit reached")

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup
    start_task = asyncio.create_task(trader.start())

    await asyncio.sleep(0.05)
    assert not start_task.done(), "listener death must not tear down the monitor"
    release.set()
    with pytest.raises(RuntimeError, match="reconnect limit reached"):
        await asyncio.wait_for(start_task, timeout=1)

    assert events == [
        "monitor started",
        "listener died",
        "monitor finished",
        "cleanup",
    ]


@pytest.mark.asyncio
async def test_single_shot_listener_failure_holds_positions_until_monitors_exit() -> (
    None
):
    trader = _lifecycle_trader(yolo_mode=False)
    release, events = _held_position_monitor(trader)

    async def wait_for_token() -> TokenInfo:
        events.append("listener died")
        raise RuntimeError("single listener failed")

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._cleanup_resources = cleanup
    start_task = asyncio.create_task(trader.start())

    await asyncio.sleep(0.05)
    assert not start_task.done()
    release.set()
    with pytest.raises(RuntimeError, match="single listener failed"):
        await asyncio.wait_for(start_task, timeout=1)

    assert events[-2:] == ["monitor finished", "cleanup"]


@pytest.mark.asyncio
async def test_listener_failure_without_positions_stays_fatal() -> None:
    trader = _lifecycle_trader(yolo_mode=True)

    async def listen(*args, **kwargs) -> None:
        raise RuntimeError("reconnect limit reached")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = AsyncMock()

    with pytest.raises(RuntimeError, match="reconnect limit reached"):
        await asyncio.wait_for(trader.start(), timeout=1)


@pytest.mark.asyncio
async def test_reconciliation_failure_interrupts_stalled_queue_drain() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_queue.put_nowait(_token(Platform.LETS_BONK))
    events: list[str] = []

    async def reconcile() -> None:
        raise RuntimeError("reconciliation failed during drain")

    async def cleanup() -> None:
        events.append("cleanup")

    trader._reconcile_unresolved_buys = reconcile
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="reconciliation failed during drain"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_single_token_listener_failure_propagates_without_waiting_for_timeout() -> (
    None
):
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_wait_timeout = 60

    async def listen(*args, **kwargs) -> None:
        raise RuntimeError("single listener failed")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._wait_for_token = UniversalTrader._wait_for_token.__get__(trader)

    with pytest.raises(RuntimeError, match="single listener failed"):
        await asyncio.wait_for(trader._wait_for_token(), timeout=0.1)


@pytest.mark.asyncio
async def test_recovered_token_keeps_original_age_when_queued() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    trader.max_token_age = 10
    trader._handle_token = AsyncMock()
    trader._process_token_queue = UniversalTrader._process_token_queue.__get__(trader)
    token = _token(Platform.LETS_BONK)
    token.creation_timestamp = monotonic() - 100

    queued = await trader._queue_token(token, recovered=True)
    assert trader.token_timestamps[str(token.mint)] == token.creation_timestamp
    processor_task = asyncio.create_task(trader._process_token_queue())
    await asyncio.wait_for(trader.token_queue.join(), timeout=0.1)
    processor_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await processor_task

    assert queued is True
    assert trader.token_timestamps.get(str(token.mint)) is None
    trader._handle_token.assert_not_awaited()
    assert str(token.mint) in trader.processed_tokens
    assert str(token.mint) not in trader._reserved_mints


@pytest.mark.asyncio
async def test_single_token_listener_reserves_only_first_matching_token() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    first_token = _token(Platform.LETS_BONK)
    second_token = _token(Platform.LETS_BONK)

    async def listen(callback, *args, **kwargs) -> None:
        await callback(first_token)
        await callback(second_token)
        await asyncio.Event().wait()

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._wait_for_token = UniversalTrader._wait_for_token.__get__(trader)

    found_token = await trader._wait_for_token()

    assert found_token is first_token
    assert trader._reserved_mints == {str(first_token.mint)}
    assert set(trader.token_timestamps) == {str(first_token.mint)}


@pytest.mark.asyncio
async def test_single_token_listener_normal_return_is_lifecycle_failure() -> None:
    trader = _lifecycle_trader(yolo_mode=False)

    async def listen(*args, **kwargs) -> None:
        return None

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._wait_for_token = UniversalTrader._wait_for_token.__get__(trader)

    with pytest.raises(
        RuntimeError,
        match="Token listener stopped before detecting a token",
    ):
        await trader._wait_for_token()


@pytest.mark.asyncio
async def test_unresolved_buy_reconciliation_rides_out_rpc_outage(caplog) -> None:
    """A transport outage on one record must not stop the others or the monitors."""
    trader = object.__new__(UniversalTrader)
    flaky = _token(Platform.LETS_BONK)
    healthy = _token(Platform.LETS_BONK)
    trader._shutdown_event = asyncio.Event()
    trader._unresolved_buys = {
        str(flaky.mint): {
            "token": flaky,
            "signature": "flaky-signature",
            "baseline_raw": 0,
        },
        str(healthy.mint): {
            "token": healthy,
            "signature": "healthy-signature",
            "baseline_raw": 0,
        },
    }
    trader._reserved_mints = {str(flaky.mint), str(healthy.mint)}
    trader.processed_tokens = set()
    trader._write_recovery_journal = lambda: None
    trader._unresolved_buy_state_changed = asyncio.Event()
    seen: list[str] = []

    async def confirm(signature: str) -> TransactionOutcome:
        seen.append(signature)
        if len(seen) >= 3:
            trader._shutdown_event.set()
        if signature == "flaky-signature":
            raise RpcUnavailableError("dns down")
        return TransactionOutcome(TransactionStatus.EXPIRED, signature)

    trader.solana_client = SimpleNamespace(confirm_transaction_outcome=confirm)
    trader.price_check_interval = 1

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(trader._reconcile_unresolved_buys(), timeout=5)

    assert "healthy-signature" in seen
    assert str(flaky.mint) in trader._unresolved_buys
    assert str(healthy.mint) not in trader._unresolved_buys
    assert trader._unresolved_buys[str(flaky.mint)]["reconcile_checks"] >= 2
    assert any("still rpc unavailable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_unresolved_buy_reconciliation_propagates_fatal_error() -> None:
    """A confirmed buy that cannot be reconciled is unmonitored funds: fail loudly."""
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    trader._shutdown_event = asyncio.Event()
    trader._unresolved_buys = {
        str(token.mint): {
            "token": token,
            "signature": "buy-signature",
            "baseline_raw": 0,
        }
    }
    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            side_effect=RuntimeError("ledger record missing")
        )
    )
    trader.price_check_interval = 1

    with pytest.raises(RuntimeError, match="ledger record missing"):
        await asyncio.wait_for(trader._reconcile_unresolved_buys(), timeout=0.1)

    assert str(token.mint) in trader._unresolved_buys


@pytest.mark.asyncio
async def test_unresolved_buy_reconciliation_logs_unknown_at_escalating_cadence(
    caplog,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    trader._shutdown_event = asyncio.Event()
    trader._unresolved_buys = {
        str(token.mint): {"token": token, "signature": "sig", "baseline_raw": 0}
    }
    calls = 0

    async def confirm(signature: str) -> TransactionOutcome:
        nonlocal calls
        calls += 1
        if calls >= 5:
            trader._shutdown_event.set()
        return TransactionOutcome(TransactionStatus.UNKNOWN, signature)

    trader.solana_client = SimpleNamespace(confirm_transaction_outcome=confirm)
    trader.price_check_interval = 0

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(trader._reconcile_unresolved_buys(), timeout=5)

    unresolved_logs = [r for r in caplog.records if "still unknown" in r.message]
    assert [int(r.message.split("after ")[1].split()[0]) for r in unresolved_logs] == [
        1,
        2,
        4,
    ]


@pytest.mark.asyncio
async def test_unresolved_buy_recovery_uses_exact_durable_receipt_destinations() -> (
    None
):
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    primary = Pubkey.new_unique()
    protocol_fee = Pubkey.new_unique()
    trader._shutdown_event = asyncio.Event()
    trader._unresolved_buy_state_changed = asyncio.Event()
    trader._unresolved_buys = {
        token_key: {
            "token": token,
            "signature": "buy-signature",
            "baseline_raw": 0,
        }
    }

    async def read_receipt(*args, **kwargs) -> tuple[int, int]:
        trader._shutdown_event.set()
        return 2_000_000, 1_050_000_000

    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            return_value=SimpleNamespace(
                status=TransactionStatus.SUCCESS,
                slot=123,
            )
        ),
        get_submission_receipt_destinations=AsyncMock(
            return_value=(primary, protocol_fee)
        ),
        get_buy_transaction_details=AsyncMock(side_effect=read_receipt),
    )
    trader.transaction_ledger = SimpleNamespace(
        get_active_submission_record=lambda intent_id: SimpleNamespace(
            signature="buy-signature",
            fee_lamports=41_000,
        )
    )
    trader._handle_successful_buy = AsyncMock()
    trader.processed_tokens = set()
    trader._reserved_mints = {token_key}
    trader.price_check_interval = 1

    await trader._reconcile_unresolved_buys()

    trader.solana_client.get_buy_transaction_details.assert_awaited_once_with(
        "buy-signature",
        token.mint,
        primary,
        quote_mint=SystemAddresses.WSOL_MINT,
        quote_destinations=[protocol_fee],
    )
    recovered_result = trader._handle_successful_buy.await_args.args[1]
    assert recovered_result.quote_amount_raw == 1_050_000_000
    assert recovered_result.fee_lamports == 41_000


@pytest.mark.asyncio
async def test_cleanup_attempts_all_resources_and_raises_first_failure(
    monkeypatch,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    events: list[str] = []

    async def blocked_background_task() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("background cancelled")

    background_task = asyncio.create_task(blocked_background_task())
    await asyncio.sleep(0)
    trader._pending_recovery_tokens = []
    trader._inflight_tokens = {}
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader.token_queue = asyncio.Queue()
    trader._write_recovery_journal = lambda: events.append("journal")
    trader._position_tasks = {background_task}
    trader.traded_mints = {token.mint}
    trader.traded_token_programs = {str(token.mint): token.token_program_id}
    trader.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
    trader.priority_fee_manager = object()
    trader.cleanup_mode = object()
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False

    async def close_curve_manager() -> None:
        events.append("curve manager close")

    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(close=close_curve_manager)
    )

    async def cleanup_post_session(*args, **kwargs) -> None:
        events.append("post-session cleanup")
        raise RuntimeError("post-session cleanup failed")

    async def close_client() -> None:
        events.append("client close")
        raise RuntimeError("client close failed")

    def close_ledger() -> None:
        events.append("ledger close")

    trader.solana_client = SimpleNamespace(close=close_client)
    trader.transaction_ledger = SimpleNamespace(close=close_ledger)
    trader._journal_lock_handle = None
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_post_session",
        cleanup_post_session,
    )

    with pytest.raises(RuntimeError, match="post-session cleanup failed"):
        await asyncio.wait_for(trader._cleanup_resources(), timeout=0.1)

    assert events == [
        "journal",
        "background cancelled",
        "post-session cleanup",
        "curve manager close",
        "client close",
        "ledger close",
    ]
    assert trader.transaction_ledger is None
    assert trader.solana_client.ledger is None


@pytest.mark.asyncio
async def test_start_propagates_fatal_listener_error_after_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        raise RuntimeError("listener failed")

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="listener failed"):
        await trader.start()

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_yolo_listener_normal_return_is_lifecycle_failure() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        return None

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="Token listener stopped unexpectedly"):
        await trader.start()

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_start_cancellation_still_cleans_up_and_propagates() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    listener_started = asyncio.Event()
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        listener_started.set()
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup
    start_task = asyncio.create_task(trader.start())
    await listener_started.wait()

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_yolo_start_propagates_fatal_monitor_error_after_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    await trader._fatal_monitor_errors.put(RuntimeError("monitor crashed"))
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="monitor crashed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_single_start_propagates_monitor_error_while_waiting_for_token() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    await trader._fatal_monitor_errors.put(RuntimeError("monitor crashed"))
    events: list[str] = []

    async def wait_for_token() -> None:
        await asyncio.Event().wait()

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="monitor crashed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_monitor_error_winning_after_listener_is_still_propagated() -> None:
    trader = _lifecycle_trader(yolo_mode=True)
    events: list[str] = []

    async def listen(*args, **kwargs) -> None:
        return None

    async def drain(
        processor_task: asyncio.Task,
        lifecycle_failure_task: asyncio.Task,
    ) -> None:
        trader._fatal_monitor_errors.put_nowait(RuntimeError("late monitor crash"))
        await asyncio.sleep(0)

    async def cleanup() -> None:
        events.append("cleanup")

    trader.token_listener = SimpleNamespace(listen_for_tokens=listen)
    trader._await_queue_drain = drain
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="late monitor crash"):
        await trader.start()

    assert events == ["cleanup"]


@pytest.mark.asyncio
async def test_start_finalizes_reservation_and_propagates_fatal_trade_error() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    events: list[tuple[str, bool] | str] = []

    async def wait_for_token() -> TokenInfo:
        trader._reserved_mints.add(str(token.mint))
        return token

    async def handle_token(token_info: TokenInfo) -> bool:
        raise RuntimeError("trade failed")

    def finish_token(token_info: TokenInfo, handled: bool) -> None:
        events.append(("finish", handled))

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._finish_token_reservation = finish_token
    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="trade failed"):
        await trader.start()

    assert events == [("finish", False), "cleanup"]


@pytest.mark.asyncio
async def test_handle_token_propagates_fatal_buy_exception_and_keeps_recovery() -> None:
    trader = object.__new__(UniversalTrader)
    trader.execution_policy = ExecutionPolicy(mode=ExecutionMode.DRY_RUN)
    token = _token(Platform.LETS_BONK)
    trader.platform = Platform.LETS_BONK
    trader.allowed_quote_mints = None
    trader.quote_amounts = {SystemAddresses.WSOL_MINT: 1.0}
    trader.extreme_fast_mode = True
    trader._pending_recovery_tokens = []
    trader._write_recovery_journal = lambda: None
    trader.buyer = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("submission crashed"))
    )

    with pytest.raises(RuntimeError, match="submission crashed"):
        await trader._handle_token(token)

    assert trader._pending_recovery_tokens == [token]


@pytest.mark.asyncio
async def test_handle_token_defers_unverified_pump_quote_allowlist_check() -> None:
    trader = object.__new__(UniversalTrader)
    trader.execution_policy = ExecutionPolicy(mode=ExecutionMode.DRY_RUN)
    token = _token(Platform.PUMP_FUN)
    token.quote_mint = None
    token.state_from_event = False
    trader.platform = Platform.PUMP_FUN
    trader.allowed_quote_mints = {SystemAddresses.USDC_MINT}
    trader.quote_amounts = {SystemAddresses.USDC_MINT: 1.0}
    trader.extreme_fast_mode = True
    trader._pending_recovery_tokens = []
    trader._write_recovery_journal = lambda: None
    trader.buyer = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("authoritative refresh reached"))
    )

    with pytest.raises(RuntimeError, match="authoritative refresh reached"):
        await trader._handle_token(token)

    trader.buyer.execute.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_queue_processor_propagates_unexpected_trading_exception() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    trader.token_queue = asyncio.Queue()
    trader.token_queue.put_nowait(token)
    trader._inflight_tokens = {}
    trader.token_timestamps = {}
    trader.max_token_age = 60
    trader._handle_token = AsyncMock(side_effect=RuntimeError("worker crashed"))
    trader._finish_token_reservation = lambda token_info, handled: None

    with pytest.raises(RuntimeError, match="worker crashed"):
        await asyncio.wait_for(trader._process_token_queue(), timeout=0.1)

    assert trader.token_queue.empty()
    await asyncio.wait_for(trader.token_queue.join(), timeout=0.1)


@pytest.mark.asyncio
async def test_start_notices_queue_processor_failure_before_queue_drain() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_queue.put_nowait(_token(Platform.LETS_BONK))
    trader.token_queue.put_nowait(_token(Platform.LETS_BONK))
    trader.max_token_age = 60
    trader._process_token_queue = UniversalTrader._process_token_queue.__get__(trader)
    trader._handle_token = AsyncMock(side_effect=RuntimeError("queue trade failed"))
    trader._finish_token_reservation = lambda token_info, handled: None
    trader._wait_for_token = AsyncMock(
        side_effect=AssertionError("must not wait after processor failure")
    )
    cleanup_calls: list[bool] = []

    async def cleanup() -> None:
        cleanup_calls.append(True)

    trader._cleanup_resources = cleanup

    with pytest.raises(RuntimeError, match="queue trade failed"):
        await asyncio.wait_for(trader.start(), timeout=0.1)

    assert cleanup_calls == [True]


@pytest.mark.asyncio
async def test_single_token_start_awaits_automatic_monitor_before_cleanup() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    token = _token(Platform.LETS_BONK)
    events: list[str] = []

    async def wait_for_token() -> TokenInfo:
        return token

    async def monitor() -> None:
        await asyncio.sleep(0.01)
        events.append("monitor")

    async def handle_token(token_info: TokenInfo) -> bool:
        task = asyncio.create_task(monitor())
        trader._position_tasks.add(task)
        trader._position_monitor_tasks.add(task)
        return True

    async def cleanup() -> None:
        events.append("cleanup")

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._finish_token_reservation = lambda token_info, handled: None
    trader._cleanup_resources = cleanup

    await trader.start()

    assert events == ["monitor", "cleanup"]


@pytest.mark.asyncio
async def test_single_shot_keeps_listening_past_gate_skips_until_a_buy() -> None:
    """One-shot means one buy, not one detection: skipped coins do not end the run."""
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_wait_timeout = 5
    trader._buy_attempts = 0
    tokens = [_token(Platform.LETS_BONK) for _ in range(4)]
    served = iter(tokens)
    handled_symbols: list[str] = []

    async def wait_for_token() -> TokenInfo:
        return next(served)

    async def handle_token(token_info: TokenInfo) -> bool:
        handled_symbols.append(str(token_info.mint))
        if len(handled_symbols) < 3:
            return True  # gate skip: handled, no attempt, no state
        trader._buy_attempts += 1
        trader._active_positions[str(token_info.mint)] = (token_info, object())
        return True

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._finish_token_reservation = lambda token_info, handled: None
    trader._cleanup_resources = AsyncMock()

    await asyncio.wait_for(trader.start(), 3)

    assert handled_symbols == [str(t.mint) for t in tokens[:3]]
    trader._cleanup_resources.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_attempt_continues_past_later_gate_skips() -> None:
    """A blocked attempt and later skips must not prematurely end discovery."""
    trader = _lifecycle_trader(yolo_mode=False)
    trader.token_wait_timeout = 5
    trader._buy_attempts = 0
    trader.trade_hub = None
    # Fill FIRST, then skips: the live failure mode. A hoisted attempt
    # baseline made every post-fill skip look like a buy and ended the run.
    tokens = [_token(Platform.LETS_BONK) for _ in range(3)]
    served = iter([*tokens, None, None, None])
    handled_symbols: list[str] = []

    async def wait_for_token() -> TokenInfo:
        return next(served)

    async def handle_token(token_info: TokenInfo) -> bool:
        handled_symbols.append(str(token_info.mint))
        if len(handled_symbols) == 1:
            trader._buy_attempts += 1  # blocked at the submission gate
            return True
        return True  # plain gate skip

    trader._wait_for_token = wait_for_token
    trader._handle_token = handle_token
    trader._finish_token_reservation = lambda token_info, handled: None
    trader._cleanup_resources = AsyncMock()

    await asyncio.wait_for(trader.start(), 3)

    # All three handled: the fill continues past, and post-fill skips keep
    # scanning (the hoisted-baseline bug ended the run right after a fill).
    assert handled_symbols == [str(t.mint) for t in tokens]
    assert trader._cleanup_resources.await_count == 1


@pytest.mark.asyncio
async def test_dry_run_drains_existing_marks_after_entry_window_closes() -> None:
    trader = _lifecycle_trader(yolo_mode=False)
    entry_window_closed, finish_mark = asyncio.Event(), asyncio.Event()
    mark = asyncio.create_task(finish_mark.wait())
    trader._paper_tasks = {mark}
    trader._position_tasks.add(mark)

    async def no_more_entries() -> None:
        entry_window_closed.set()

    trader._wait_for_token = no_more_entries
    trader._cleanup_resources = AsyncMock()
    run = asyncio.create_task(trader.start())
    try:
        await asyncio.wait_for(entry_window_closed.wait(), 1)
        await asyncio.sleep(0)
        assert not run.done()
        assert not mark.done()
        trader._cleanup_resources.assert_not_awaited()
    finally:
        finish_mark.set()
        await asyncio.wait_for(run, 1)
    assert mark.result() is True
    trader._cleanup_resources.assert_awaited_once()


def test_failed_pending_recovery_keeps_reservation() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = [token]
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._reserved_mints = {token_key}
    trader.token_timestamps = {token_key: 1.0}
    trader._write_recovery_journal = lambda: None

    trader._finish_token_reservation(token, handled=False)

    assert trader._pending_recovery_tokens == [token]
    assert token_key in trader._reserved_mints
    assert token_key not in trader.processed_tokens


def test_handled_recovery_clears_pending_state_and_reservation() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = [token]
    trader._active_positions = {}
    trader._unresolved_buys = {}
    trader._reserved_mints = {token_key}
    trader.token_timestamps = {token_key: 1.0}
    trader._write_recovery_journal = lambda: None

    trader._finish_token_reservation(token, handled=True)

    assert trader._pending_recovery_tokens == []
    assert token_key not in trader._reserved_mints
    assert token_key in trader.processed_tokens


@pytest.mark.parametrize("resolved_state", ["active", "unresolved"])
def test_recovery_token_is_cleared_when_durable_state_owns_reservation(
    resolved_state: str,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token_key = str(token.mint)
    trader.processed_tokens = set()
    trader._pending_recovery_tokens = [token]
    trader._active_positions = (
        {token_key: (token, object())} if resolved_state == "active" else {}
    )
    trader._unresolved_buys = (
        {token_key: {"token": token, "signature": "buy-signature"}}
        if resolved_state == "unresolved"
        else {}
    )
    trader._reserved_mints = {token_key}
    trader.token_timestamps = {token_key: 1.0}
    journal_writes: list[bool] = []
    trader._write_recovery_journal = lambda: journal_writes.append(True)

    trader._finish_token_reservation(token, handled=False)

    assert trader._pending_recovery_tokens == []
    assert token_key in trader._reserved_mints
    assert token_key not in trader.processed_tokens
    assert journal_writes == [True]


@pytest.mark.asyncio
async def test_position_monitor_propagates_unexpected_error() -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    trader.price_read_outage_budget = 0.0
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(
            calculate_price=AsyncMock(side_effect=RuntimeError("price monitor crashed"))
        )
    )
    trader._get_pool_address = lambda token_info: token_info.pool_state

    with pytest.raises(RuntimeError, match="price monitor crashed"):
        await asyncio.wait_for(
            trader._monitor_position_until_exit(token, position),
            timeout=0.1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quote_mint", "quote_raw"),
    [(None, 400_000_000), (USDC_MINT, 400_000)],
)
async def test_confirmed_pending_sell_stages_cleanup_before_position_removal(
    monkeypatch: pytest.MonkeyPatch,
    quote_mint: Pubkey | None,
    quote_raw: int,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token.quote_mint = quote_mint
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        position_id="buy-signature",
        account_balance_baseline_raw=0,
    )
    position.mark_exit_intent("sell:buy-signature:1", ExitReason.TAKE_PROFIT, 2.0)
    position.mark_exit_pending(
        "pending-sell",
        ExitReason.TAKE_PROFIT,
        fee_lamports=5_000,
    )
    events: list[str] = []

    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    calculate_price = AsyncMock(
        side_effect=AssertionError("price must not gate pending sell reconciliation")
    )
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_price=calculate_price)
    )
    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            return_value=SimpleNamespace(
                status=TransactionStatus.SUCCESS,
                slot=123,
                error=None,
            )
        ),
        get_sell_transaction_details=AsyncMock(
            side_effect=[None, 0, -1, True, 1.5, float("nan"), 10**400, quote_raw]
        ),
    )
    trader.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
    trader.seller = SimpleNamespace(execute=AsyncMock())
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = "after_sell"
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    logged_prices: list[float] = []

    def log_trade(
        _side: str, _token_info: TokenInfo, price: float, *_args: object
    ) -> None:
        events.append("log")
        logged_prices.append(price)

    trader._log_trade = log_trade
    trader._remove_position = lambda mint: events.append("remove")
    staged_manager = SimpleNamespace()
    monkeypatch.setattr(
        "trading.universal_trader.stage_cleanup_after_sell",
        lambda *args, **kwargs: events.append("stage") or staged_manager,
    )
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )

    async def wait_for_receipt(_delay: float) -> bool:
        assert position.is_active
        assert position.pending_exit_signature == "pending-sell"
        assert position.pending_exit_fee_lamports == 5_000
        assert events == []
        trader.seller.execute.assert_not_awaited()
        cleanup_after_sell.assert_not_awaited()
        return False

    trader._sleep_until_shutdown = AsyncMock(side_effect=wait_for_receipt)

    await trader._monitor_position_until_exit(token, position)

    assert events == ["stage", "log", "remove"]
    assert logged_prices == [pytest.approx(0.2)]
    assert position.exit_price == pytest.approx(0.2)
    assert trader._sleep_until_shutdown.await_count == 7
    trader.seller.execute.assert_not_awaited()
    assert all(
        call.args[1] == (quote_mint or SystemAddresses.WSOL_MINT)
        for call in trader.solana_client.get_sell_transaction_details.await_args_list
    )
    assert cleanup_after_sell.await_args.kwargs["confirmed_sold_raw"] is None
    assert cleanup_after_sell.await_args.kwargs["staged_manager"] is staged_manager

    calculate_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_sell_passes_raw_delta_to_cleanup(monkeypatch) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        account_balance_baseline_raw=0,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    calculate_token_price = AsyncMock(return_value=2.0)
    calculate_price = AsyncMock(
        side_effect=AssertionError("token-aware price lookup must take precedence")
    )
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(
            calculate_token_price=calculate_token_price,
            calculate_price=calculate_price,
        )
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=True,
                platform=Platform.LETS_BONK,
                tx_signature="sell-signature",
                amount=1.5,
                amount_raw=1_500_000,
                price=2.0,
                status=TransactionStatus.SUCCESS.value,
            )
        )
    )
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )

    await trader._monitor_position_until_exit(token, position)

    assert cleanup_after_sell.await_args.kwargs["confirmed_sold_raw"] == 1_500_000
    calculate_token_price.assert_awaited_once_with(token)
    calculate_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_rides_out_transient_price_read_outage(monkeypatch) -> None:
    """An RPC outage during the read-only price check must not kill the monitor."""
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = 1
    trader.price_read_outage_budget = 60.0
    calculate_token_price = AsyncMock(
        side_effect=[
            RpcUnavailableError("dns down"),
            _FeeAttestationUnavailable("attestation RPC returned no response"),
            2.0,
        ]
    )
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=calculate_token_price)
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=True,
                platform=Platform.LETS_BONK,
                tx_signature="sell-signature",
                amount=2.0,
                amount_raw=2_000_000,
                price=2.0,
                status=TransactionStatus.SUCCESS.value,
            )
        )
    )
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        AsyncMock(return_value=None),
    )

    await trader._monitor_position_until_exit(token, position)

    assert calculate_token_price.await_count == 3
    trader.seller.execute.assert_awaited_once()
    assert position.is_active is False


@pytest.mark.asyncio
async def test_flow_signal_wakes_monitor_and_sells_unconditionally_at_event_price(
    monkeypatch,
) -> None:
    """A creator sell on the stream must exit within the tick, not the interval,
    at the event's price, bypassing the take-profit net-ROI gate."""
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.PUMP_FUN)
    token.creator = Pubkey.new_unique()
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.5,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 60  # a poll would never come in time
    trader.max_exit_sell_attempts = 1
    trader.price_read_outage_budget = 300.0
    trader.flow_rules = FlowRules(creator_sell=True)
    trader.geyser_endpoint = "geyser.invalid"
    trader.geyser_api_token = "token"
    trader.geyser_auth_type = "x-token"
    trader._flow_signals = {}
    trader._flow_wakeups = {}
    trader._gate_queues = {}
    calculate_token_price = AsyncMock(return_value=1.2)  # below TP; poll would hold
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=calculate_token_price)
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(
            return_value=TradeResult(
                success=True,
                platform=Platform.PUMP_FUN,
                tx_signature="sell-signature",
                amount=2.0,
                amount_raw=2_000_000,
                price=1.1,
                status=TransactionStatus.SUCCESS.value,
            )
        )
    )
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.bonding_curve
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        AsyncMock(return_value=None),
    )
    token_key = str(token.mint)

    async def fake_flow(token_info: TokenInfo, _position: Position) -> None:
        # First tick reads 1.2 via RPC (no exit), then the creator sells.
        await asyncio.sleep(0.05)
        trader._flow_signals[token_key] = FlowSignal(
            "creator_sell", "creator sold 1.0000 SOL", 1.1, 444_044_178
        )
        trader._flow_wakeups[token_key].set()

    trader._consume_trade_flow = fake_flow

    await asyncio.wait_for(trader._monitor_position_until_exit(token, position), 5)

    assert calculate_token_price.await_count == 1
    sell_kwargs = trader.seller.execute.await_args.kwargs
    assert sell_kwargs["token_price"] == 1.1
    assert sell_kwargs["take_profit_net_quote_raw"] is None
    assert position.is_active is False
    assert position.exit_reason is ExitReason.TRADE_FLOW
    assert token_key not in trader._flow_wakeups


@pytest.mark.asyncio
async def test_flow_exit_latches_across_a_reverted_sell_with_no_further_events(
    monkeypatch,
) -> None:
    """After a flow-triggered sell reverts on a now-silent coin, the next poll
    must retry as TRADE_FLOW with a fresh RPC price, not wait for a new event."""
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.PUMP_FUN)
    token.creator = Pubkey.new_unique()
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.5,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = 3
    trader.price_read_outage_budget = 300.0
    trader.flow_rules = FlowRules(creator_sell=True)
    trader.geyser_endpoint = "geyser.invalid"
    trader.geyser_api_token = "token"
    trader.geyser_auth_type = "x-token"
    trader._gate_queues = {}
    calculate_token_price = AsyncMock(return_value=1.05)
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=calculate_token_price)
    )
    trader.seller = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                TradeResult(
                    success=False,
                    platform=Platform.PUMP_FUN,
                    tx_signature="reverted-signature",
                    error_message="6003 TooLittleSolReceived",
                    fee_lamports=27_000,
                    status=TransactionStatus.REVERTED.value,
                ),
                TradeResult(
                    success=True,
                    platform=Platform.PUMP_FUN,
                    tx_signature="sell-signature",
                    amount=2.0,
                    amount_raw=2_000_000,
                    price=1.05,
                    status=TransactionStatus.SUCCESS.value,
                ),
            ]
        )
    )
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.bonding_curve
    trader._persist_position = lambda token_info, active_position: None
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: None
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        AsyncMock(return_value=None),
    )
    token_key = str(token.mint)

    async def one_signal_then_silence(token_info: TokenInfo, _p: Position) -> None:
        trader._flow_signals[token_key] = FlowSignal("creator_sell", "x", 1.1, 1)
        trader._flow_wakeups[token_key].set()
        await asyncio.Event().wait()

    trader._consume_trade_flow = one_signal_then_silence

    await asyncio.wait_for(trader._monitor_position_until_exit(token, position), 5)

    prices = [c.kwargs["token_price"] for c in trader.seller.execute.await_args_list]
    assert prices == [1.1, 1.05]  # event price first, fresh RPC price on retry
    assert position.is_active is False
    assert position.exit_reason is ExitReason.TRADE_FLOW
    assert token_key not in trader._flow_latched


@pytest.mark.asyncio
@pytest.mark.parametrize("latched", [False, True])
@pytest.mark.parametrize("interrupt_stream", [False, True])
async def test_history_loss_rejects_pending_signal_but_preserves_latched_exit(
    monkeypatch: pytest.MonkeyPatch, *, latched: bool, interrupt_stream: bool
) -> None:
    token = _token(Platform.PUMP_FUN)
    position = _net_roi_position(token)
    trader = _net_roi_monitor_trader(
        token,
        prices=[],
        sell_results=[
            TradeResult(
                success=True,
                platform=token.platform,
                tx_signature="confirmed-exit",
                amount=2.0,
                amount_raw=2_000_000,
                price=1.0,
                status=TransactionStatus.SUCCESS.value,
            )
        ],
    )
    token_key = str(token.mint)
    queue = TradeQueue(maxsize=1)
    queue.invalidate("interrupted" if interrupt_stream else "overflow")
    trader._gate_queues = {token_key: queue}
    trader._flow_signals = {
        token_key: FlowSignal("creator_sell", "pending before loss", 9.0, 1)
    }
    trader._flow_latched = {token_key} if latched else set()
    trader._flow_wakeups = {}

    async def read_price(_token: TokenInfo) -> float:
        trader._shutdown_event.set()  # Stop after this real monitor iteration.
        return 1.0

    trader.platform_implementations.curve_manager.calculate_token_price = read_price
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        AsyncMock(return_value=None),
    )
    await asyncio.wait_for(trader._monitor_position_loop(token, position), 1)
    assert trader._flow_signals == {}
    if latched:
        assert not position.is_active
        assert position.exit_reason is ExitReason.TRADE_FLOW
        assert trader.seller.execute.await_args.kwargs["token_price"] == 1.0
    else:
        assert position.is_active
        assert token_key not in trader._flow_latched
        trader.seller.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_fails_closed_when_price_read_outage_budget_expires(
    monkeypatch,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = 1
    trader.price_read_outage_budget = 10.0
    calculate_token_price = AsyncMock(side_effect=RpcUnavailableError("dns down"))
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=calculate_token_price)
    )
    trader._get_pool_address = lambda token_info: token_info.pool_state
    clock = iter([0.0, 5.0, 10.0])
    monkeypatch.setattr("trading.universal_trader.monotonic", lambda: next(clock))

    with pytest.raises(RpcUnavailableError, match="dns down"):
        await trader._monitor_position_until_exit(token, position)

    assert calculate_token_price.await_count == 3
    assert position.is_active is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("Invalid bonding curve state"), ValueError),
        (
            _FeeAttestationError("Pump fee attestation mismatch"),
            _FeeAttestationError,
        ),
        (0.0, ValueError),
    ],
    ids=["data-error", "attestation-mismatch", "invalid-price"],
)
async def test_monitor_fails_immediately_on_non_transient_price_error(
    failure: object, expected: type[Exception]
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        position_id="buy-signature",
    )
    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 0
    trader.max_exit_sell_attempts = 1
    trader.price_read_outage_budget = 300.0
    calculate_token_price = AsyncMock(
        side_effect=failure if isinstance(failure, Exception) else None,
        return_value=None if isinstance(failure, Exception) else failure,
    )
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_token_price=calculate_token_price)
    )
    trader._get_pool_address = lambda token_info: token_info.pool_state

    with pytest.raises(expected):
        await trader._monitor_position_until_exit(token, position)

    assert calculate_token_price.await_count == 1
    assert position.is_active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_price", [2.0, float("nan")])
async def test_monitor_cancellation_drains_inflight_sell_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
    receipt_price: float,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        take_profit_percentage=0.1,
        quantity_raw=2_000_000,
        quote_amount_raw=100_000,
        buy_fee_lamports=40_000,
        account_balance_baseline_raw=0,
        position_id="buy-signature",
    )
    sell_started = asyncio.Event()
    release_sell = asyncio.Event()
    events: list[str] = []

    async def sell(*args, **kwargs) -> TradeResult:
        sell_started.set()
        await release_sell.wait()
        return TradeResult(
            success=True,
            platform=Platform.LETS_BONK,
            tx_signature="sell-signature",
            amount=2.0,
            amount_raw=2_000_000,
            price=receipt_price,
            fee_lamports=5_000,
            status=TransactionStatus.SUCCESS.value,
        )

    trader._shutdown_event = asyncio.Event()
    trader.price_check_interval = 1
    trader.max_exit_sell_attempts = 1
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(calculate_price=AsyncMock(return_value=2.0))
    )
    trader.seller = SimpleNamespace(execute=AsyncMock(side_effect=sell))
    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            return_value=SimpleNamespace(
                status=TransactionStatus.SUCCESS, slot=123, error=None
            )
        ),
        get_sell_transaction_details=AsyncMock(return_value=400_000_000),
    )
    trader.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    trader._get_pool_address = lambda token_info: token_info.pool_state
    persisted: list[dict] = []
    trader._persist_position = lambda token_info, active_position: persisted.append(
        active_position.to_dict()
    )
    trader._log_trade = lambda *args, **kwargs: events.append("log")
    trader._remove_position = lambda mint: events.append("remove")
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )

    monitor_task = asyncio.create_task(
        trader._monitor_position_until_exit(token, position)
    )
    await sell_started.wait()
    monitor_task.cancel()
    await asyncio.sleep(0)
    completed_before_sell = monitor_task.done()
    release_sell.set()

    with pytest.raises(asyncio.CancelledError):
        await monitor_task

    assert completed_before_sell is False
    if receipt_price != 2.0:
        assert position.is_active
        assert position.pending_exit_signature == "sell-signature"
        assert persisted[-1]["pending_exit_signature"] == "sell-signature"
        assert events == []
        cleanup_after_sell.assert_not_awaited()
        await trader._monitor_position_until_exit(token, position)
        assert position.exit_price == pytest.approx(0.2)
    trader.seller.execute.assert_awaited_once()
    assert events == ["log", "remove"]
    cleanup_after_sell.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_price", [0.75, None, 0.0, float("nan")])
async def test_emergency_exit_sells_one_loaded_position_without_listener(
    monkeypatch: pytest.MonkeyPatch,
    receipt_price: float | None,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        account_balance_baseline_raw=0,
        position_id="buy-signature",
    )
    policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        expected_wallet=str(Pubkey.new_unique()),
        max_trade_quote_raw=10_000_000_000,
        max_total_fee_lamports=1_000_000,
        risk_session_id="test-session",
        max_session_quote_raw=100_000_000_000,
        max_session_fee_lamports=10_000_000,
    ).authorize_live()
    trader.execution_policy = policy
    trader._active_positions = {str(token.mint): (token, position)}
    curve_manager = SimpleNamespace(
        prepare_live_execution=AsyncMock(),
        calculate_token_price=AsyncMock(return_value=0.75),
    )
    trader.platform_implementations = SimpleNamespace(curve_manager=curve_manager)
    sell_result = TradeResult(
        success=True,
        platform=Platform.LETS_BONK,
        tx_signature="emergency-sell-signature",
        amount=2.0,
        amount_raw=2_000_000,
        price=receipt_price,
        fee_lamports=5_000,
        status=TransactionStatus.SUCCESS.value,
    )
    trader.seller = SimpleNamespace(execute=AsyncMock(return_value=sell_result))
    trader.solana_client = SimpleNamespace()
    trader.wallet = SimpleNamespace()
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    persisted: list[dict] = []
    trader._persist_position = lambda token_info, active_position: persisted.append(
        active_position.to_dict()
    )
    trader._log_trade = lambda *args, **kwargs: None
    trader._remove_position = lambda mint: trader._active_positions.pop(str(mint))
    trader._cleanup_resources = AsyncMock()
    cleanup_after_sell = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell",
        cleanup_after_sell,
    )
    stage_cleanup = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        "trading.universal_trader.stage_cleanup_after_sell", stage_cleanup
    )

    result = await trader.emergency_exit(token.mint)

    assert result is sell_result
    curve_manager.prepare_live_execution.assert_awaited_once()
    curve_manager.calculate_token_price.assert_awaited_once_with(token)
    trader.seller.execute.assert_awaited_once()
    assert trader.seller.execute.await_args.kwargs["intent_id"].startswith(
        "emergency-sell:buy-signature:"
    )
    if receipt_price == 0.75:
        assert position.is_active is False
        assert str(token.mint) not in trader._active_positions
    else:
        assert result.unresolved
        assert result.price is None
        assert result.quote_amount_raw is None
        assert position.is_active
        assert position.pending_exit_signature == "emergency-sell-signature"
        assert persisted[-1]["pending_exit_signature"] == "emergency-sell-signature"
        assert str(token.mint) in trader._active_positions
        cleanup_after_sell.assert_not_awaited()
        stage_cleanup.assert_not_called()
    trader._cleanup_resources.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quote_mint", "quote_raw"),
    [(None, 400_000_000), (USDC_MINT, 400_000)],
)
async def test_emergency_exit_reconciles_pending_signature_without_resubmitting(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    quote_mint: Pubkey | None,
    quote_raw: int,
) -> None:
    trader = object.__new__(UniversalTrader)
    token = _token(Platform.LETS_BONK)
    token.quote_mint = quote_mint
    position = Position.create_from_buy_result(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=1.0,
        quantity=2.0,
        quantity_raw=2_000_000,
        position_id="buy-signature",
    )
    position.mark_exit_intent("sell:buy-signature:1", ExitReason.MANUAL, 0.75)
    position.mark_exit_pending(
        "pending-signature",
        ExitReason.MANUAL,
        fee_lamports=5_000,
    )
    trader.execution_policy = ExecutionPolicy(
        mode=ExecutionMode.LIVE,
        expected_wallet=str(Pubkey.new_unique()),
        max_trade_quote_raw=10_000_000_000,
        max_total_fee_lamports=1_000_000,
        risk_session_id="test-session",
        max_session_quote_raw=100_000_000_000,
        max_session_fee_lamports=10_000_000,
    ).authorize_live()
    trader._active_positions = {str(token.mint): (token, position)}
    trader.platform_implementations = SimpleNamespace(
        curve_manager=SimpleNamespace(prepare_live_execution=AsyncMock())
    )
    trader.solana_client = SimpleNamespace(
        confirm_transaction_outcome=AsyncMock(
            return_value=SimpleNamespace(
                status=TransactionStatus.UNKNOWN,
                error="RPC unavailable",
                slot=None,
            )
        )
    )
    trader.seller = SimpleNamespace(execute=AsyncMock())
    trader._cleanup_resources = AsyncMock()
    trader.wallet = SimpleNamespace(pubkey=Pubkey.new_unique())
    trader.priority_fee_manager = SimpleNamespace()
    trader.cleanup_mode = None
    trader.cleanup_with_priority_fee = False
    trader.cleanup_force_close_with_burn = False
    logs: list[float] = []
    trader._log_trade = lambda _side, _token_info, price, *_args: logs.append(price)
    trader._remove_position = lambda mint: trader._active_positions.pop(str(mint))
    cleanup_after_sell = AsyncMock()
    stage_cleanup = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        "trading.universal_trader.handle_cleanup_after_sell", cleanup_after_sell
    )
    monkeypatch.setattr(
        "trading.universal_trader.stage_cleanup_after_sell", stage_cleanup
    )

    result = await trader.emergency_exit(token.mint)

    assert result.unresolved is True
    assert result.tx_signature == "pending-signature"
    assert result.price is None
    assert result.quote_amount_raw is None
    trader.seller.execute.assert_not_awaited()
    assert position.pending_exit_signature == "pending-signature"
    trader._cleanup_resources.assert_awaited_once()

    trader.solana_client.confirm_transaction_outcome.return_value = SimpleNamespace(
        status=TransactionStatus.SUCCESS, slot=123, error=None
    )
    trader.solana_client.get_sell_transaction_details = AsyncMock(
        side_effect=[None, quote_raw]
    )
    result = await trader.emergency_exit(token.mint)
    assert result.unresolved
    assert result.price is None
    assert result.quote_amount_raw is None
    assert position.is_active
    assert position.pending_exit_signature == "pending-signature"
    assert str(token.mint) in trader._active_positions
    assert logs == []
    cleanup_after_sell.assert_not_awaited()
    stage_cleanup.assert_not_called()
    trader.seller.execute.assert_not_awaited()

    result = await trader.emergency_exit(token.mint)
    assert result.success
    assert result.status == TransactionStatus.SUCCESS.value
    assert result.tx_signature == "pending-signature"
    assert result.price == pytest.approx(0.2)
    assert result.quote_amount_raw == quote_raw
    assert result.fee_lamports == 5_000
    assert result.slot == 123
    assert logs == [pytest.approx(0.2)]
    assert not position.is_active
    assert str(token.mint) not in trader._active_positions
    trader.seller.execute.assert_not_awaited()
    cleanup_after_sell.assert_awaited_once()
