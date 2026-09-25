"""Unsigned probes must never sign or turn an unknown RPC outcome into success."""

# ruff: noqa: S101, SLF001, PLR2004

import importlib
import struct
from base64 import b64decode
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction


@pytest.mark.asyncio
async def test_unsigned_probe_rejects_unknown_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "learning-examples")
    )
    buy = importlib.import_module("simulate_bot_buy_path")
    v2 = importlib.import_module("simulate_v2_trades")
    payer = buy.PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    # No private key and no signing method: the hook only needs the public identity.
    signer = SimpleNamespace(pubkey=lambda: payer)
    reply = {"result": {"value": {"err": None, "unitsConsumed": 1000}}}

    async def blockhash() -> Hash:
        return Hash.default()

    async def post_rpc(payload: dict) -> dict | None:
        assert payload["method"] == "simulateTransaction"
        transaction = Transaction.from_bytes(b64decode(payload["params"][0]))
        assert transaction.signatures == [Signature.default()]
        return reply

    client = SimpleNamespace(get_latest_blockhash=blockhash, post_rpc=post_rpc)
    instruction = transfer(
        TransferParams(from_pubkey=payer, to_pubkey=Pubkey.default(), lamports=1)
    )
    outcome = buy.install_simulation_hook(client)
    await client.build_and_send_transaction([instruction], signer)
    assert outcome["err"] is None

    # A later transport failure must not leave the previous successful result visible.
    reply = None
    with pytest.raises(ValueError):
        await client.build_and_send_transaction([instruction], signer)
    assert outcome == {}
    assert not await v2.simulate(client, payer, [instruction], "unknown")

    # A superficially successful envelope without execution evidence is also unknown.
    reply = {"result": {"value": {"err": None}}}
    with pytest.raises(ValueError):
        await client.build_and_send_transaction([instruction], signer)
    assert outcome == {}
    assert not await v2.simulate(client, payer, [instruction], "incomplete")

    reply = {"result": {"value": {"err": {"InstructionError": [0, {"Custom": 3012}]}}}}
    assert not await v2.simulate(
        client,
        payer,
        [instruction],
        "partial",
        allowed_custom_error=3012,
        closed_account=Pubkey.default(),
    )


def test_full_exit_requires_new_inventory_and_closed_poststate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "learning-examples")
    )
    v2 = importlib.import_module("simulate_v2_trades")
    payer = v2.PumpFunAddresses.NORMAL_FEE_RECIPIENTS[0]
    account = Pubkey.new_unique()
    instruction = v2.close_account(
        v2.CloseAccountParams(
            program_id=v2.SystemAddresses.TOKEN_2022_PROGRAM,
            account=account,
            dest=payer,
            owner=payer,
        )
    )
    message = v2.Message.new_with_blockhash([instruction], payer, Hash.default())
    index = message.account_keys.index(account)
    value = {
        "preBalances": [1_000_000, *([0] * (len(message.account_keys) - 1))],
        "postBalances": [995_000, *([0] * (len(message.account_keys) - 1))],
        "fee": 5_000,
        "accounts": [None],
    }
    assert v2._verify_closed_account(value, message, account)
    value["accounts"] = [
        {
            "lamports": 0,
            "owner": str(Pubkey.default()),
            "executable": False,
            "data": ["", "base64"],
        }
    ]
    assert v2._verify_closed_account(value, message, account)

    value["postBalances"][index] = 1
    assert not v2._verify_closed_account(value, message, account)
    value["postBalances"][index] = 0
    value["preBalances"][index] = 1
    assert not v2._verify_closed_account(value, message, account)
    del value["preBalances"]
    assert not v2._verify_closed_account(value, message, account)


def test_configured_profile_rejects_inconsistent_or_invalid_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "learning-examples")
    )
    v2 = importlib.import_module("simulate_v2_trades")
    # No local config dependency; even a secrets-file reference must never be opened.
    config = {
        "env_file": "must-not-open.secrets",
        "private_key": "${MUST_NOT_INTERPOLATE}",
        "trade": {
            "extreme_fast_mode": True,
            "extreme_fast_token_amount": 250_000,
            "buy_amount": 0.01,
            "buy_slippage": 0.3,
            "sell_slippage": 0.3,
        },
        "execution": {
            "max_trade_quote_raw": 13_000_000,
            "max_total_fee_lamports": 250_000,
        },
        "priority_fees": {
            "enable_fixed": True,
            "enable_dynamic": False,
            "extra_percentage": 0,
            "fixed_amount": 200_000,
            "hard_cap": 200_000,
        },
        "compute_units": {"buy": 140_000, "sell": 110_000},
        "filters": {"allowed_quote_mints": ["sol"]},
    }

    def open_profile(path: Path, **_kwargs: object) -> StringIO:
        assert path == v2.CONFIGURED_PROFILE
        return StringIO(v2.yaml.safe_dump(config))

    monkeypatch.setattr(Path, "open", open_profile)
    profile = v2.load_configured_profile()
    assert profile["max_quote"] == 13_000_000
    assert profile["token_amount"] == 250_000
    config["execution"]["max_trade_quote_raw"] = 12_999_999
    with pytest.raises(ValueError, match="quote policy cap"):
        v2.load_configured_profile()
    config["execution"]["max_trade_quote_raw"] = 13_000_000
    config["priority_fees"]["fixed_amount"] = 200_001
    with pytest.raises(ValueError, match="priority fee"):
        v2.load_configured_profile()
    config["priority_fees"]["fixed_amount"] = 200_000
    config["trade"]["sell_slippage"] = float("nan")
    with pytest.raises(ValueError, match="sell slippage"):
        v2.load_configured_profile()


@pytest.mark.asyncio
async def test_configured_quote_above_cap_never_reaches_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "learning-examples")
    )
    v2 = importlib.import_module("simulate_v2_trades")
    monkeypatch.setattr(
        v2,
        "load_configured_profile",
        lambda: {
            "token_amount": 250_000,
            "max_quote": 13_000_000,
            "buy_cu": 140_000,
            "sell_cu": 110_000,
            "priority_fee": 200_000,
            "fee_cap": 250_000,
            "sell_slippage_bps": 3000,
        },
    )
    payer, mint = Pubkey.new_unique(), Pubkey.new_unique()
    token = SimpleNamespace(
        quote_mint=v2.SystemAddresses.WSOL_MINT,
        bonding_curve=Pubkey.new_unique(),
        token_program_id=v2.SystemAddresses.TOKEN_2022_PROGRAM,
        is_mayhem_mode=True,
        is_cashback_coin=False,
    )

    async def token_info(*_args: object) -> tuple:
        return token, {"complete": False, "price_per_token": 0.0000001}

    async def prepare() -> None:
        pass

    async def cost(_pool: Pubkey, quantity: int, **_kwargs: object) -> int:
        assert quantity == 250_000 * 10**v2.TOKEN_DECIMALS
        return 13_000_001

    async def forbidden_simulation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unaffordable configured buy must not be simulated")

    implementations = SimpleNamespace(
        address_provider=None,
        instruction_builder=None,
        curve_manager=SimpleNamespace(
            prepare_live_execution=prepare, calculate_buy_cost=cost
        ),
    )
    monkeypatch.setattr(v2, "build_token_info", token_info)
    monkeypatch.setattr(
        v2, "get_platform_implementations", lambda *_args: implementations
    )
    monkeypatch.setattr(v2, "simulate", forbidden_simulation)
    assert not await v2.simulate_mint(None, payer, mint, configured_size=True)


@pytest.mark.asyncio
async def test_configured_simulation_enforces_fees_and_stays_unsigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "learning-examples")
    )
    v2 = importlib.import_module("simulate_v2_trades")
    payer = Pubkey.from_string("9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe")
    instruction = transfer(
        TransferParams(from_pubkey=payer, to_pubkey=Pubkey.default(), lamports=1)
    )
    value = {
        "err": None,
        "unitsConsumed": 1000,
        "fee": 33_000,
        "logs": ["Program log: smoke"],
    }
    calls = 0

    async def blockhash() -> Hash:
        return Hash.default()

    async def post_rpc(payload: dict) -> dict:
        nonlocal calls
        calls += 1
        assert payload["method"] == "simulateTransaction"
        assert payload["params"][1]["sigVerify"] is False
        wire = b64decode(payload["params"][0])
        assert len(wire) <= v2.PACKET_LIMIT
        transaction = Transaction.from_bytes(wire)
        assert transaction.signatures == [Signature.default()]
        assert transaction.message.account_keys[0] == payer
        budget_program = Pubkey.from_string(
            "ComputeBudget111111111111111111111111111111"
        )
        budget_data = {
            bytes(ix.data)
            for ix in transaction.message.instructions
            if transaction.message.account_keys[ix.program_id_index] == budget_program
        }
        assert b"\x02" + struct.pack("<I", 140_000) in budget_data
        assert b"\x03" + struct.pack("<Q", 200_000) in budget_data
        assert b"\x04" + struct.pack("<I", 16 * 1024 * 1024) in budget_data
        return {"result": {"value": value}}

    client = SimpleNamespace(get_latest_blockhash=blockhash, post_rpc=post_rpc)
    options = {
        "compute_unit_limit": 140_000,
        "priority_fee": 200_000,
        "fee_cap": 250_000,
    }
    assert await v2.simulate(client, payer, [instruction], "configured", **options)
    value["fee"] = 250_001
    assert not await v2.simulate(
        client, payer, [instruction], "fee-over-cap", **options
    )
    del value["fee"]
    assert not await v2.simulate(client, payer, [instruction], "unknown-fee", **options)
    options["fee_cap"] = 32_999
    before = calls
    assert not await v2.simulate(
        client, payer, [instruction], "estimated-over-cap", **options
    )
    assert calls == before
