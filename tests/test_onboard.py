from __future__ import annotations

# ruff: noqa: S101, ARG001, SLF001, S603, PLC0415 - offline tests
import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

import onboard

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
ONBOARD_SCRIPT = SRC_ROOT / "onboard.py"


def bot_yaml(tmp_path: Path) -> Path:
    config = {
        "name": "test-bot",
        "rpc_endpoint": "https://rpc.example.test",
        "wss_endpoint": "wss://rpc.example.test",
        "private_key": "not-used-in-this-test",
        "enabled": False,
        "platform": "pump_fun",
        "geyser": {
            "endpoint": "https://geyser.example.test",
            "api_token": "token",
            "auth_type": "x-token",
        },
        "trade": {
            "buy_amount": 0.001,
            "buy_slippage": 0.1,
            "sell_slippage": 0.1,
            "exit_strategy": "manual",
        },
        "filters": {"listener_type": "geyser", "max_token_age": 10},
        "execution": {
            "mode": "dry_run",
            "expected_wallet": "So11111111111111111111111111111111111111112",
        },
    }
    path = tmp_path / "bot.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def env_file(tmp_path: Path, **overrides: str) -> Path:
    values = dict.fromkeys(onboard.REQUIRED_ENV_KEYS, "set")
    values["SOLANA_PRIVATE_KEY"] = ""
    values.update(overrides)
    path = tmp_path / "project.env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every environment/network/filesystem dependency at tmp_path."""
    env_path = env_file(tmp_path)
    idl_dir = tmp_path / "idl"
    idl_dir.mkdir()
    for name in onboard.IDL_FILES:
        (idl_dir / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(onboard, "IDL_DIR", idl_dir)
    monkeypatch.setattr(
        onboard, "LEDGER_DIR", tmp_path / ".state" / "transaction-ledgers"
    )

    async def healthy_rpc(endpoint: str | None) -> dict[str, str]:
        return onboard._step(3, "RPC health", onboard.PASS, "mocked ok")

    async def reachable_geyser(endpoint: str | None) -> dict[str, str]:
        return onboard._step(4, "Geyser reachability", onboard.PASS, "mocked ok")

    monkeypatch.setattr(onboard, "check_rpc_health", healthy_rpc)
    monkeypatch.setattr(onboard, "check_geyser_reachability", reachable_geyser)
    return env_path


def step(summary: dict, number: int) -> dict[str, str]:
    return next(result for result in summary["steps"] if result["step"] == number)


def test_all_steps_pass_offline(tmp_path: Path, offline_env: Path) -> None:
    summary = asyncio.run(onboard.run_onboarding(bot_yaml(tmp_path), offline_env))

    assert summary["passed"] is True
    assert [result["status"] for result in summary["steps"]] == [onboard.PASS] * 7


def test_missing_env_key_fails_step_one(tmp_path: Path, offline_env: Path) -> None:
    env_path = env_file(tmp_path, GEYSER_ENDPOINT="")
    summary = asyncio.run(onboard.run_onboarding(bot_yaml(tmp_path), env_path))

    assert summary["passed"] is False
    assert step(summary, 1)["status"] == onboard.FAIL
    assert "GEYSER_ENDPOINT" in step(summary, 1)["detail"]


def test_missing_env_file_falls_back_to_process_environment(
    tmp_path: Path,
    offline_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in onboard.REQUIRED_ENV_KEYS:
        monkeypatch.setenv(key, "provider-value")
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)

    summary = asyncio.run(
        onboard.run_onboarding(bot_yaml(tmp_path), tmp_path / "absent.env")
    )

    assert step(summary, 1)["status"] == onboard.PASS
    assert "process environment" in step(summary, 1)["detail"]
    assert "wallet-free" in step(summary, 1)["detail"]


def test_private_key_blank_is_pass_with_note(tmp_path: Path, offline_env: Path) -> None:
    summary = asyncio.run(onboard.run_onboarding(bot_yaml(tmp_path), offline_env))

    assert step(summary, 1)["status"] == onboard.PASS
    assert "SOLANA_PRIVATE_KEY blank" in step(summary, 1)["detail"]


def test_incompatible_listener_fails_step_two(
    tmp_path: Path, offline_env: Path
) -> None:
    config_path = tmp_path / "bad-listener.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "name": "test-bot",
                "rpc_endpoint": "https://rpc.example.test",
                "wss_endpoint": "wss://rpc.example.test",
                "private_key": "not-used-in-this-test",
                "enabled": False,
                "platform": "lets_bonk",
                "trade": {
                    "buy_amount": 0.001,
                    "buy_slippage": 0.1,
                    "sell_slippage": 0.1,
                },
                "filters": {"listener_type": "pumpportal", "max_token_age": 10},
            }
        ),
        encoding="utf-8",
    )
    summary = asyncio.run(onboard.run_onboarding(config_path, offline_env))

    assert summary["passed"] is False
    assert step(summary, 2)["status"] == onboard.FAIL


def test_live_mode_without_wallet_fails_step_five(
    tmp_path: Path,
    offline_env: Path,
) -> None:
    config_path = tmp_path / "live.yaml"
    live = yaml.safe_load(bot_yaml(tmp_path).read_text(encoding="utf-8"))
    live["execution"] = {"mode": "live", "expected_wallet": None}
    live["execution"].update(
        {
            "risk_session_id": "test-session",
            "max_trade_quote_raw": 200_000,
            "max_total_fee_lamports": 250_000,
        }
    )
    config_path.write_text(yaml.safe_dump(live), encoding="utf-8")
    summary = asyncio.run(onboard.run_onboarding(config_path, offline_env))

    assert summary["passed"] is False
    assert step(summary, 5)["status"] == onboard.FAIL


def test_enabled_bot_warns_step_five(tmp_path: Path, offline_env: Path) -> None:
    config_path = tmp_path / "enabled.yaml"
    enabled = yaml.safe_load(bot_yaml(tmp_path).read_text(encoding="utf-8"))
    enabled["enabled"] = True
    config_path.write_text(yaml.safe_dump(enabled), encoding="utf-8")
    summary = asyncio.run(onboard.run_onboarding(config_path, offline_env))

    assert summary["passed"] is True
    assert step(summary, 5)["status"] == onboard.WARN


def test_broken_idl_file_fails_step_six(tmp_path: Path, offline_env: Path) -> None:
    (onboard.IDL_DIR / "pump_fun_idl.json").write_text("{not json", encoding="utf-8")
    summary = asyncio.run(onboard.run_onboarding(bot_yaml(tmp_path), offline_env))

    assert summary["passed"] is False
    assert step(summary, 6)["status"] == onboard.FAIL
    assert "pump_fun_idl.json" in step(summary, 6)["detail"]


def test_unwritable_ledger_dir_fails_step_seven(
    tmp_path: Path,
    offline_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = tmp_path / "blocked"
    (blocked / "child").mkdir(parents=True)
    # A regular file where a parent directory is required blocks mkdir(parents=True).
    (blocked / "child" / "ledger").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(onboard, "LEDGER_DIR", blocked / "child" / "ledger" / "ledgers")

    summary = asyncio.run(onboard.run_onboarding(bot_yaml(tmp_path), offline_env))

    assert summary["passed"] is False
    assert step(summary, 7)["status"] == onboard.FAIL


def test_main_exits_zero_when_all_pass(
    tmp_path: Path,
    offline_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def all_pass(config_path: Path, env_path: Path | None) -> dict:
        return {"passed": True, "steps": []}

    monkeypatch.setattr(onboard, "run_onboarding", all_pass)
    assert onboard.main(["--config", "x.yaml", "--env-file", str(offline_env)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True


def test_main_exits_one_on_failure(
    tmp_path: Path,
    offline_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def has_failure(config_path: Path, env_path: Path | None) -> dict:
        return {"passed": False, "steps": []}

    monkeypatch.setattr(onboard, "run_onboarding", has_failure)
    assert onboard.main(["--config", "x.yaml", "--env-file", str(offline_env)]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_cli_help_works() -> None:
    import subprocess

    completed = subprocess.run(
        [sys.executable, str(ONBOARD_SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=SRC_ROOT.parent,
    )
    assert "--config" in completed.stdout
    assert "--env-file" in completed.stdout
