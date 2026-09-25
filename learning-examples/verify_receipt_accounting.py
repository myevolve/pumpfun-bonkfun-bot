"""Verify unknown receipt data cannot become realized prices or owned inventory.

Offline only: synthetic public keys, in-memory receipts and the real client and
recovery methods. No wallet, RPC, signing, submission, or persistent state.

Usage: uv run --offline --no-sync learning-examples/verify_receipt_accounting.py
"""

# Offline assertions and isolated construction intentionally exercise private paths.
# ruff: noqa: S101, SLF001

import asyncio
import json
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import verify_tp_sl_exit_price as fixture

from core.client import SolanaClient
from core.pubkeys import WSOL_MINT
from core.transaction_state import TransactionOutcome, TransactionStatus
from trading.position import ExitReason, Position

OWNER = fixture.Pubkey.from_bytes(bytes([2]) * 32)
MINT = fixture.Pubkey.from_bytes(bytes([3]) * 32)
TOKEN_ACCOUNT = fixture.Pubkey.from_bytes(bytes([4]) * 32)
VENUE = fixture.Pubkey.from_bytes(bytes([5]) * 32)
RENT = 2_039_280
PROCEEDS = 400_000_000
QUOTE_SPENT = 10


def deny_network(event: str, _args: tuple) -> None:
    """Make accidental network access fail before connecting."""
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        message = "Receipt verification forbids network access"
        raise RuntimeError(message)


def buy_receipt() -> dict:
    """A newly created recipient account, with an attributable positive delta."""
    return {
        "transaction": {
            "message": {"accountKeys": [str(OWNER), str(TOKEN_ACCOUNT), str(VENUE)]}
        },
        "meta": {
            "err": None,
            "fee": 5_000,
            "preBalances": [2_000_000_000, 0, 100],
            "postBalances": [1_997_955_710, RENT, 110],
            "preTokenBalances": [],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": str(MINT),
                    "owner": str(OWNER),
                    "uiTokenAmount": {"amount": "110", "decimals": 6},
                }
            ],
        },
    }


async def verify_inventory() -> None:
    """Missing metadata is unknown; genuinely new and existing accounts still work."""
    new = buy_receipt()
    existing = deepcopy(new)
    existing["meta"]["preBalances"][1] = RENT
    before = deepcopy(existing["meta"]["postTokenBalances"][0])
    before["uiTokenAmount"]["amount"] = "100"
    existing["meta"]["preTokenBalances"] = [before]
    missing_list = deepcopy(existing)
    del missing_list["meta"]["preTokenBalances"]
    missing_owner = deepcopy(existing)
    del missing_owner["meta"]["preTokenBalances"][0]["owner"]
    missing_endpoint = deepcopy(existing)
    missing_endpoint["meta"]["preTokenBalances"] = []
    client = object.__new__(SolanaClient)
    for label, receipt, baseline, acquired in (
        ("missing_list", missing_list, None, None),
        ("missing_owner", missing_owner, None, None),
        ("missing_funded_endpoint", missing_endpoint, None, None),
        ("new_account", new, 0, 110),
        ("existing_account", existing, 100, 10),
    ):
        client._get_transaction_result = AsyncMock(return_value=receipt)
        actual = await client.get_buyer_pre_token_balance("synthetic", MINT, OWNER)
        assert actual == baseline, (label, "baseline", actual, baseline)
        tokens, quote = await client.get_buy_transaction_details(
            "synthetic", MINT, VENUE, WSOL_MINT
        )
        assert tokens == acquired, (label, "acquired", tokens, acquired)
        assert quote == QUOTE_SPENT, (label, "quote", quote)


async def verify_pending_sell(*, emergency: bool) -> None:
    """Confirmed sales stay pending until actual proceeds can price their closure."""
    token = fixture._make_token_info()
    position = fixture._make_position()
    position.mark_exit_intent("synthetic-sell", ExitReason.MANUAL, 1.5e-6)
    position.mark_exit_pending(
        "synthetic-signature", ExitReason.MANUAL, fee_lamports=5_000
    )
    # Exercise the same durable representation used after a restart.
    position = Position.from_dict(position.to_dict())
    trader = fixture._make_trader(fixture.StubCurveManager([]), fixture.StubSeller())
    client = object.__new__(SolanaClient)
    client._get_transaction_result = AsyncMock(return_value=None)
    client.confirm_transaction_outcome = AsyncMock(
        return_value=TransactionOutcome(
            TransactionStatus.SUCCESS, "synthetic-signature", slot=123
        )
    )
    trader.solana_client = client
    trader.wallet = SimpleNamespace(pubkey=OWNER)
    trader.execution_policy = SimpleNamespace(
        mode=fixture.ExecutionPolicy().mode, require_submission=lambda: None
    )
    trader._cleanup_resources = AsyncMock()
    trader._active_positions = {str(token.mint): (token, position)}
    trader.seller.execute = AsyncMock(side_effect=AssertionError("Duplicate sell"))
    logged = []
    trader._log_trade = lambda _action, _token, price, *_rest: logged.append(price)
    trader._remove_position = lambda mint: trader._active_positions.pop(str(mint))

    async def stop_waiting(_seconds: float) -> bool:
        trader._shutdown_event.set()
        return True

    trader._sleep_until_shutdown = stop_waiting
    if emergency:
        result = await trader.emergency_exit(token.mint)
        assert result.unresolved and result.price is None
    else:
        await trader._monitor_position_loop(token, position)
    assert (
        position.is_active and position.pending_exit_signature == "synthetic-signature"
    )
    assert not logged and str(token.mint) in trader._active_positions

    client._get_transaction_result = AsyncMock(
        return_value={
            "transaction": {"message": {"accountKeys": [str(OWNER)]}},
            "meta": {
                "err": None,
                "fee": 5_000,
                "preBalances": [2_000_000_000],
                "postBalances": [2_000_000_000 + PROCEEDS - 5_000],
                "preTokenBalances": [],
                "postTokenBalances": [],
            },
        }
    )
    trader._shutdown_event.clear()
    expected_price = PROCEEDS / 1_000_000_000 / position.quantity
    if emergency:
        result = await trader.emergency_exit(token.mint)
        assert result.success and result.price == expected_price
        assert result.quote_amount_raw == PROCEEDS
    else:
        await trader._monitor_position_loop(token, position)
    assert not position.is_active and not trader._active_positions
    assert logged == [expected_price] and position.exit_price == expected_price
    trader.seller.execute.assert_not_awaited()


async def main() -> None:
    """Run all accounting boundaries and report each failure independently."""
    checks = {
        "inventory_unknown_and_known": verify_inventory(),
        "monitor_pending_receipt": verify_pending_sell(emergency=False),
        "emergency_pending_receipt": verify_pending_sell(emergency=True),
    }
    outcomes = await asyncio.gather(*checks.values(), return_exceptions=True)
    failures = {
        name: repr(result)
        for name, result in zip(checks, outcomes, strict=True)
        if isinstance(result, BaseException)
    }
    print(json.dumps({"checks": list(checks), "failures": failures}, sort_keys=True))
    assert not failures, failures


if __name__ == "__main__":
    sys.addaudithook(deny_network)
    asyncio.run(main())
