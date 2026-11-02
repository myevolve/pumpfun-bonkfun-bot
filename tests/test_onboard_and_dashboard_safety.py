"""Onboarding wizard safety checks (read-only; no network dependencies)."""

# ruff: noqa: S101, TRY003
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import yaml
from aiohttp import web
from test_config_loader_safety import minimal_config

import onboard

if TYPE_CHECKING:
    from pathlib import Path


REQUIRED_ENV = {
    "SOLANA_NODE_RPC_ENDPOINT": "https://rpc.example.test",
    "SOLANA_NODE_WSS_ENDPOINT": "wss://rpc.example.test",
    "GEYSER_ENDPOINT": "geyser.example.test:443",
    "GEYSER_API_TOKEN": "test-token",
    "GEYSER_AUTH_TYPE": "x-token",
}


def _write_config(tmp_path: Path, config: dict) -> Path:
    config_path = tmp_path / "bot.yaml"
    config_path.write_text(yaml.safe_dump(config))
    env_path = tmp_path / "env"
    env_path.write_text(
        "".join(f"{key}={value}\n" for key, value in REQUIRED_ENV.items())
    )
    return config_path


def _stub_outside_steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_rpc_health(_endpoint: str | None) -> dict[str, str]:
        return {"step": "3", "name": "RPC health", "status": "pass", "detail": "stub"}

    monkeypatch.setattr(onboard, "check_rpc_health", fake_rpc_health)
    monkeypatch.setattr(onboard, "LEDGER_DIR", tmp_path / "ledgers")


@pytest.mark.asyncio
async def test_run_onboarding_skips_geyser_reachability_for_pumpportal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path, minimal_config())

    async def real_geyser_must_not_run(endpoint: str | None) -> dict[str, str]:
        raise AssertionError(f"geyser check must not run, got {endpoint!r}")

    _stub_outside_steps(monkeypatch, tmp_path)
    monkeypatch.setattr(onboard, "check_geyser_reachability", real_geyser_must_not_run)

    summary = await onboard.run_onboarding(config_path, env_path=tmp_path / "env")

    assert summary["passed"] is True
    geyser_step = next(
        s for s in summary["steps"] if s["name"] == "Geyser reachability"
    )
    assert geyser_step["status"] == "pass"
    assert "geyser not required" in geyser_step["detail"]


@pytest.mark.asyncio
async def test_run_onboarding_requires_geyser_for_geyser_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = minimal_config()
    config["filters"] = dict(config["filters"], listener_type="geyser")
    config_path = _write_config(tmp_path, config)

    _stub_outside_steps(monkeypatch, tmp_path)

    summary = await onboard.run_onboarding(config_path, env_path=tmp_path / "env")

    # No geyser endpoint configured: the real reachability check fails closed.
    assert summary["passed"] is False
    geyser_step = next(
        s for s in summary["steps"] if s["name"] == "Geyser reachability"
    )
    assert geyser_step["status"] == "fail"
    assert "no geyser endpoint" in geyser_step["detail"]


@pytest.mark.asyncio
async def test_check_rpc_health_rejects_non_dict_payload() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(["unexpected", "array"])

    app = web.Application()
    app.router.add_post("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        result = await asyncio.wait_for(
            onboard.check_rpc_health(f"http://127.0.0.1:{port}/"), timeout=5
        )
        assert result["status"] == "fail"
        assert "unexpected payload" in result["detail"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_check_rpc_health_reports_transport_failure_cleanly() -> None:
    result = await onboard.check_rpc_health("http://127.0.0.1:1/")
    assert result["status"] == "fail"
    assert result["detail"].startswith("getHealth failed:")
