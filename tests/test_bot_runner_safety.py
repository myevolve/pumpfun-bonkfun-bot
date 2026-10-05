from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.pubkey import Pubkey

import bot_runner
from core.execution_policy import ExecutionBlocked, ExecutionMode
from core.pubkeys import WSOL_MINT
from core.transaction_ledger import TransactionLedger
from interfaces.core import Platform, TokenInfo
from trading.position import Position
from trading.universal_trader import UniversalTrader
from utils import paths


def live_config() -> dict:
    return {
        "execution": {
            "mode": "live",
            "expected_wallet": "11111111111111111111111111111111",
            "max_trade_quote_raw": 1_000_000,
            "max_total_fee_lamports": 50_000,
            "risk_session_id": "test-session",
            "max_session_quote_raw": 10_000_000,
            "max_session_fee_lamports": 1_000_000,
        }
    }


def test_live_policy_requires_explicit_runtime_authorization() -> None:
    with pytest.raises(ExecutionBlocked, match="explicit runtime authorization"):
        bot_runner.build_execution_policy(live_config(), authorize_live=False)

    policy = bot_runner.build_execution_policy(live_config(), authorize_live=True)
    assert policy.mode is ExecutionMode.LIVE
    assert policy.can_submit


def test_dry_run_cannot_be_live_authorized() -> None:
    with pytest.raises(ExecutionBlocked, match="execution.mode='live'"):
        bot_runner.build_execution_policy({}, authorize_live=True)


def test_normal_cli_invocation_does_not_authorize_live() -> None:
    args = bot_runner.parse_args([])

    assert args.config is None
    assert args.authorize_live is False


def test_status_cli_requires_one_explicit_config() -> None:
    args = bot_runner.parse_args(["--config", "bots/live.yaml", "--status"])

    assert args.config == Path("bots/live.yaml")
    assert args.status is True
    assert args.emergency_exit is None

    with pytest.raises(SystemExit):
        bot_runner.parse_args(["--status"])


def test_preflight_cli_is_read_only_and_requires_one_config() -> None:
    args = bot_runner.parse_args(["--config", "bots/live.yaml", "--preflight"])

    assert args.config == Path("bots/live.yaml")
    assert args.preflight is True

    with pytest.raises(SystemExit):
        bot_runner.parse_args(["--preflight"])
    with pytest.raises(SystemExit):
        bot_runner.parse_args(
            [
                "--config",
                "bots/live.yaml",
                "--preflight",
                "--authorize-live",
            ]
        )


def test_resume_only_cli_requires_one_config_and_rejects_status() -> None:
    args = bot_runner.parse_args(
        ["--config", "bots/live.yaml", "--preflight", "--resume-only"]
    )

    assert args.resume_only is True

    with pytest.raises(SystemExit):
        bot_runner.parse_args(["--resume-only"])
    with pytest.raises(SystemExit):
        bot_runner.parse_args(
            ["--config", "bots/live.yaml", "--status", "--resume-only"]
        )


def test_emergency_exit_cli_requires_live_authorization_and_one_mint() -> None:
    mint = "11111111111111111111111111111111"
    args = bot_runner.parse_args(
        [
            "--config",
            "bots/live.yaml",
            "--emergency-exit",
            mint,
            "--authorize-live",
        ]
    )

    assert args.emergency_exit == mint
    assert args.authorize_live is True

    with pytest.raises(SystemExit):
        bot_runner.parse_args(["--config", "bots/live.yaml", "--emergency-exit", mint])


def test_status_and_emergency_exit_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        bot_runner.parse_args(
            [
                "--config",
                "bots/live.yaml",
                "--status",
                "--emergency-exit",
                "11111111111111111111111111111111",
                "--authorize-live",
            ]
        )


def test_status_reads_validated_recovery_journal_without_live_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / ".state")
    wallet = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    token = TokenInfo(
        name="Token",
        symbol="TOK",
        uri="",
        mint=mint,
        platform=Platform.PUMP_FUN,
    )
    position = Position.create_from_buy_result(
        mint=mint,
        symbol="TOK",
        entry_price=0.25,
        quantity=2.0,
        quantity_raw=2_000_000,
        position_id="buy-signature",
    )
    journal = (
        tmp_path / ".state" / "positions" / f"{wallet}-{Platform.PUMP_FUN.value}.json"
    )
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "wallet": str(wallet),
                "platform": Platform.PUMP_FUN.value,
                "positions": {
                    str(mint): {
                        "token": UniversalTrader._token_to_dict(token),
                        "position": position.to_dict(),
                    }
                },
                "unresolved_buys": {},
                "pending_tokens": [],
            }
        )
    )
    config = {
        "name": "live",
        "platform": Platform.PUMP_FUN.value,
        "execution": {
            "mode": "live",
            "expected_wallet": str(wallet),
            "max_trade_quote_raw": 1_000_000,
            "max_total_fee_lamports": 50_000,
            "risk_session_id": "test-session",
            "max_session_quote_raw": 10_000_000,
            "max_session_fee_lamports": 1_000_000,
        },
    }
    ledger_path = tmp_path / ".state" / "transaction-ledgers" / f"{wallet}.sqlite3"
    with TransactionLedger(ledger_path) as ledger:
        ledger.record_intent(
            "buy-1",
            str(wallet),
            250_000,
            10_000,
            "a" * 64,
        )
        ledger.record_submission(
            "buy-1",
            "signature-1",
            "blockhash-1",
            100,
            quote_mint=str(WSOL_MINT),
            state="prepared",
            wire_bytes=b"wire",
            risk_session_id="test-session",
            max_session_quote_raw=10_000_000,
            max_session_fee_lamports=1_000_000,
            intent_message_hash="a" * 64,
        )
    cleanup_journal = tmp_path / ".state" / "cleanup" / f"{wallet}.json"
    cleanup_journal.parent.mkdir(parents=True)
    cleanup_journal.write_text(
        json.dumps(
            {
                "version": 3,
                "wallet": str(wallet),
                "entries": {
                    "k": {
                        "status": "unresolved",
                        "mint": str(mint),
                        "tx_signature": "cleanup-sig",
                        "intent_id": "cleanup:1",
                        "generation": "g1",
                    }
                },
            }
        )
    )
    monkeypatch.setattr(bot_runner, "load_bot_config", lambda _: config)

    status = bot_runner.read_bot_status("bots/live.yaml")

    assert status["wallet"] == str(wallet)
    assert status["platform"] == Platform.PUMP_FUN.value
    assert status["active_position_count"] == 1
    assert status["active_positions"] == [
        {
            "mint": str(mint),
            "symbol": "TOK",
            "quantity_raw": 2_000_000,
            "entry_price": 0.25,
            "pending_exit_signature": None,
            "automatic_exit": False,
        }
    ]
    assert status["unresolved_buy_count"] == 0
    assert status["risk_session"] == {
        "id": "test-session",
        "reserved_quote_raw_by_mint": {str(WSOL_MINT): 250_000},
        "max_quote_raw_configured": 10_000_000,
        "max_quote_raw_by_mint": {str(WSOL_MINT): 10_000_000},
        "remaining_quote_raw_by_mint": {str(WSOL_MINT): 9_750_000},
        "reserved_fee_lamports": 10_000,
        "max_fee_lamports": 1_000_000,
        "remaining_fee_lamports": 990_000,
        "submission_count": 1,
    }
    assert status["transaction_ledger_path"] == str(
        tmp_path / ".state" / "transaction-ledgers" / f"{wallet}.sqlite3"
    )
    assert status["active_submissions"] == [
        {
            "intent_id": "buy-1",
            "signature": "signature-1",
            "state": "prepared",
            "outcome": "none",
        }
    ]
    assert status["pending_cleanups"] == [
        {"mint": str(mint), "status": "unresolved", "tx_signature": "cleanup-sig"}
    ]

    config["name"] = "live-bonk"
    config["platform"] = Platform.LETS_BONK.value
    other_platform_status = bot_runner.read_bot_status("bots/live-bonk.yaml")
    assert (
        other_platform_status["transaction_ledger_path"]
        == status["transaction_ledger_path"]
    )
    assert other_platform_status["risk_session"] == status["risk_session"]


def test_status_supports_dry_run_without_live_risk_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    wallet = Pubkey.new_unique()
    config = {
        "name": "dry-status",
        "platform": Platform.PUMP_FUN.value,
        "execution": {
            "mode": "dry_run",
            "expected_wallet": str(wallet),
        },
    }
    monkeypatch.setattr(bot_runner, "load_bot_config", lambda _: config)

    status = bot_runner.read_bot_status("bots/dry.yaml")

    assert status["wallet"] == str(wallet)  # noqa: S101
    assert status["active_position_count"] == 0  # noqa: S101
    assert "risk_session" not in status  # noqa: S101


def test_live_preflight_checks_network_and_never_authorizes_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = Pubkey.from_string("11111111111111111111111111111111")
    config = {
        "name": "live",
        "platform": Platform.PUMP_FUN.value,
        "trade": {
            "buy_amount": 0.0005,
            "buy_slippage": 0.1,
            "max_exit_sell_attempts": 2,
        },
        "filters": {},
        "cleanup": {"mode": "after_sell", "with_priority_fee": True},
        **live_config(),
    }
    status = {
        "active_position_count": 0,
        "active_positions": [],
        "unresolved_buy_count": 0,
        "pending_token_count": 0,
        "risk_session": {
            "id": "test-session",
            "reserved_quote_raw_by_mint": {},
            "reserved_fee_lamports": 0,
        },
    }
    client = SimpleNamespace(
        get_health=AsyncMock(return_value="ok"),
        get_native_balance=AsyncMock(return_value=20_000_000),
        get_minimum_balance_for_rent_exemption=AsyncMock(return_value=3_000_000),
        get_token_account_balance=AsyncMock(),
        build_and_send_transaction=AsyncMock(),
    )
    curve_manager = SimpleNamespace(prepare_live_execution=AsyncMock())
    trader = SimpleNamespace(
        wallet=SimpleNamespace(pubkey=wallet),
        solana_client=client,
        platform_implementations=SimpleNamespace(curve_manager=curve_manager),
        close=AsyncMock(),
    )
    captured_policy = None

    def create_trader(cfg, policy, platform):
        nonlocal captured_policy
        assert cfg is config
        assert platform is Platform.PUMP_FUN
        captured_policy = policy
        return trader

    monkeypatch.setattr(bot_runner, "load_bot_config", lambda _: config)
    monkeypatch.setattr(bot_runner, "read_bot_status", lambda _: status)
    monkeypatch.setattr(bot_runner, "_create_trader", create_trader)

    report = asyncio.run(bot_runner.run_live_preflight("bots/live.yaml"))

    assert report["ready"] is True
    assert captured_policy is not None
    assert captured_policy.can_submit is False
    assert {check["name"] for check in report["checks"]} >= {
        "rpc_health",
        "native_balance",
        "session_fee_budget",
        "session_quote_budget",
        "fee_attestation",
    }
    client.build_and_send_transaction.assert_not_awaited()
    trader.close.assert_awaited_once()

    status["risk_session"]["reserved_quote_raw_by_mint"] = {str(WSOL_MINT): 9_600_000}
    blocked_report = asyncio.run(bot_runner.run_live_preflight("bots/live.yaml"))
    session_check = next(
        check
        for check in blocked_report["checks"]
        if check["name"] == "session_quote_budget"
    )
    assert blocked_report["ready"] is False
    assert session_check["ok"] is False
    client.build_and_send_transaction.assert_not_awaited()
    assert trader.close.await_count == 2

    status["active_position_count"] = 1
    status["active_positions"] = [{"automatic_exit": True}]
    client.get_native_balance.return_value = 147_500
    client.get_minimum_balance_for_rent_exemption.reset_mock()
    recovery_report = asyncio.run(
        bot_runner.run_live_preflight("bots/live.yaml", resume_only=True)
    )
    recovery_session_check = next(
        check
        for check in recovery_report["checks"]
        if check["name"] == "session_quote_budget"
    )
    recovery_fee_check = next(
        check
        for check in recovery_report["checks"]
        if check["name"] == "session_fee_budget"
    )
    recovery_native_check = next(
        check
        for check in recovery_report["checks"]
        if check["name"] == "native_balance"
    )

    assert recovery_report["ready"] is True
    assert recovery_report["resume_only"] is True
    assert recovery_session_check["ok"] is True
    assert recovery_fee_check["detail"]["required_recovery_lamports"] == 147_500
    assert recovery_native_check["detail"]["required_lamports"] == 147_500
    client.get_minimum_balance_for_rent_exemption.assert_not_awaited()

    status["active_positions"] = [{"automatic_exit": False}]
    unmonitorable_report = asyncio.run(
        bot_runner.run_live_preflight("bots/live.yaml", resume_only=True)
    )
    monitor_check = next(
        check
        for check in unmonitorable_report["checks"]
        if check["name"] == "resume_only_state"
    )

    assert unmonitorable_report["ready"] is False
    assert monitor_check["ok"] is False


def test_main_returns_nonzero_when_live_preflight_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def preflight(
        _config: Path, *, resume_only: bool = False
    ) -> dict[str, object]:
        return {"ready": False, "checks": []}

    monkeypatch.setattr(bot_runner, "run_live_preflight", preflight)

    result = bot_runner.main(["--config", "bots/live.yaml", "--preflight"])

    assert result == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


def test_main_dispatches_status_without_starting_bot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        bot_runner,
        "read_bot_status",
        lambda _: {"active_position_count": 0},
    )
    monkeypatch.setattr(
        bot_runner,
        "_run_bot_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("status must not start a bot")
        ),
    )

    assert bot_runner.main(["--config", "bots/live.yaml", "--status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"active_position_count": 0}


def test_main_returns_nonzero_for_unresolved_emergency_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def emergency_exit(_config: Path, _mint: str) -> dict[str, object]:
        return {"success": False, "status": "unknown", "tx_signature": "pending"}

    monkeypatch.setattr(bot_runner, "run_emergency_exit", emergency_exit)

    result = bot_runner.main(
        [
            "--config",
            "bots/live.yaml",
            "--emergency-exit",
            "11111111111111111111111111111111",
            "--authorize-live",
        ]
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"


def test_start_bot_rejects_disabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda _: {"name": "disabled", "enabled": False},
    )

    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(bot_runner.start_bot("unused.yaml"))


def test_start_bot_propagates_fatal_trader_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "name": "fatal-trader",
        "enabled": True,
        "platform": "pump_fun",
        "rpc_endpoint": "https://rpc.example.test",
        "wss_endpoint": "wss://rpc.example.test",
        "private_key": "unused-test-key",
        "execution": {"mode": "dry_run"},
        "trade": {
            "buy_amount": 0.001,
            "buy_slippage": 0.1,
            "sell_slippage": 0.1,
        },
        "filters": {
            "listener_type": "pumpportal",
            "max_token_age": 10,
        },
    }

    class FatalTrader:
        def __init__(self, **_kwargs):
            return None

        async def start(self, *, resume_only: bool = False) -> None:
            raise RuntimeError("listener failed")

    monkeypatch.setattr(bot_runner, "load_bot_config", lambda _: config)
    monkeypatch.setattr(bot_runner, "setup_logging", lambda _: None)
    monkeypatch.setattr(bot_runner, "print_config_summary", lambda _: None)
    monkeypatch.setattr(bot_runner, "UniversalTrader", FatalTrader)

    with pytest.raises(RuntimeError, match="listener failed"):
        asyncio.run(bot_runner.start_bot("unused.yaml"))


def test_child_signal_cancels_bot_for_cleanup_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed_handlers: dict[int, object] = {}
    cleaned_up = False

    monkeypatch.setattr(signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed_handlers.__setitem__(signum, handler),
    )

    async def fake_start_bot(
        _config_path: str | Path,
        *,
        authorize_live: bool = False,
        resume_only: bool = False,
    ) -> None:
        nonlocal cleaned_up
        try:
            handler = installed_handlers[signal.SIGTERM]
            handler(signal.SIGTERM, None)
            await asyncio.sleep(0)
        finally:
            cleaned_up = True

    monkeypatch.setattr(bot_runner, "start_bot", fake_start_bot)

    with pytest.raises(SystemExit) as exit_info:
        bot_runner.run_bot_process("unused.yaml")

    assert exit_info.value.code == 128 + signal.SIGTERM
    assert cleaned_up


def test_bulk_runner_reports_failed_child_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot_dir = tmp_path / "bots"
    bot_dir.mkdir()
    (bot_dir / "child.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda _: {
            "name": "child",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
    )

    class FailedProcess:
        def __init__(self, *, target, args, name):
            self.name = name
            self.pid = 4312
            self.exitcode = None

        def start(self) -> None:
            self.exitcode = 7

        def is_alive(self) -> bool:
            return self.exitcode is None

        def join(self, timeout: float | None = None) -> None:
            return None

        def terminate(self) -> None:
            raise AssertionError("an exited process must not be terminated")

        def kill(self) -> None:
            raise AssertionError("an exited process must not be killed")

    monkeypatch.setattr(bot_runner.multiprocessing, "Process", FailedProcess)

    with caplog.at_level(logging.INFO):
        exit_status = bot_runner.run_all_bots()

    assert exit_status == 1
    assert "exited with status 7" in caplog.text


def test_bulk_runner_stops_siblings_after_fatal_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_dir = tmp_path / "bots"
    bot_dir.mkdir()
    (bot_dir / "failed.yaml").write_text("{}")
    (bot_dir / "running.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)

    configs = {
        "failed.yaml": {
            "name": "failed",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
        "running.yaml": {
            "name": "running",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
    }
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda path: configs[Path(path).name],
    )

    created = []

    class FakeProcess:
        def __init__(self, *, target, args, name):
            self.name = name
            self.pid = 5000 + len(created)
            self.exitcode = None
            self.join_timeouts: list[float | None] = []
            self.terminated = False
            self.killed = False
            created.append(self)

        def start(self) -> None:
            if self.name == "bot-failed":
                self.exitcode = 9

        def is_alive(self) -> bool:
            return self.exitcode is None

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)

        def terminate(self) -> None:
            self.terminated = True
            self.exitcode = -signal.SIGTERM

        def kill(self) -> None:
            self.killed = True
            self.exitcode = -signal.SIGKILL

    forwarded: list[tuple[int, int]] = []
    monkeypatch.setattr(bot_runner.multiprocessing, "Process", FakeProcess)
    monkeypatch.setattr(os, "kill", lambda pid, sig: forwarded.append((pid, sig)))
    monkeypatch.setattr(bot_runner, "PROCESS_SHUTDOWN_GRACE_SECONDS", 0.0)

    assert bot_runner.run_all_bots() == 1

    failed, running = created
    assert failed.exitcode == 9
    assert not failed.terminated
    assert (running.pid, signal.SIGTERM) in forwarded
    assert running.terminated
    assert all(timeout is not None for timeout in running.join_timeouts)


def test_bulk_runner_propagates_signal_and_kills_stubborn_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot_dir = tmp_path / "bots"
    bot_dir.mkdir()
    (bot_dir / "running.yaml").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bot_runner,
        "load_bot_config",
        lambda _: {
            "name": "running",
            "enabled": True,
            "separate_process": True,
            "platform": "pump_fun",
            "execution": {"mode": "dry_run"},
        },
    )

    installed_handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(
        signal,
        "signal",
        lambda signum, handler: installed_handlers.__setitem__(signum, handler),
    )
    forwarded: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: forwarded.append((pid, sig)))

    created = []

    class StubbornProcess:
        def __init__(self, *, target, args, name):
            self.name = name
            self.pid = 8123
            self.exitcode = None
            self.join_timeouts: list[float | None] = []
            self.terminate_calls = 0
            self.kill_calls = 0
            self.triggered_signal = False
            created.append(self)

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return self.exitcode is None

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)
            if not self.triggered_signal:
                self.triggered_signal = True
                handler = installed_handlers[signal.SIGTERM]
                handler(signal.SIGTERM, None)

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.exitcode = -signal.SIGKILL

    monkeypatch.setattr(bot_runner.multiprocessing, "Process", StubbornProcess)
    monkeypatch.setattr(bot_runner, "PROCESS_SHUTDOWN_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(bot_runner, "PROCESS_TERMINATE_GRACE_SECONDS", 0.0)

    assert bot_runner.run_all_bots() == 128 + signal.SIGTERM

    process = created[0]
    assert (process.pid, signal.SIGTERM) in forwarded
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert all(timeout is not None for timeout in process.join_timeouts)
