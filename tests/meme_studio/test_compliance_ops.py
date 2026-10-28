# ruff: noqa: S101
"""Ops guard and the phase-0 ban on send-capable clients."""

from __future__ import annotations

import ast
from pathlib import Path

from meme_studio.compliance import OpsCheck, ops_guard

_FORBIDDEN_MODULES = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib",
    "tweepy",
    "telegram",
    "http",
    "socket",
    "websockets",
    "grpc",
    "openai",
    "xai",
    "xai_sdk",
}


def test_one_x_account_only() -> None:
    assert ops_guard(OpsCheck(x_accounts=("studio",))).passed is True
    extra = ops_guard(OpsCheck(x_accounts=("studio", "alt")))
    assert extra.passed is False
    assert "ops_second_account" in extra.reasons


def test_paid_engagement_dependency_fails() -> None:
    result = ops_guard(OpsCheck(dependencies=("buyfollowers",)))
    assert "ops_paid_engagement" in result.reasons
    phrase = ops_guard(OpsCheck(dependencies=("engagement pod toolkit",)))
    assert "ops_paid_engagement" in phrase.reasons


def test_secrets_in_logs_fail() -> None:
    leaked = ops_guard(OpsCheck(log_text="SOLANA_PRIVATE_KEY=abc"))
    assert "ops_secret" in leaked.reasons
    bearer = ops_guard(OpsCheck(log_text="authorization: Bearer opaque-token"))
    assert "ops_secret" in bearer.reasons
    clean = ops_guard(OpsCheck(log_text="lint pass payload hash abc"))
    assert clean.passed is True
    for leaked_log in (
        '{"api_key": "canary"}',
        '{"X_BEARER_TOKEN": "canary"}',
        '{"GEYSER_API_TOKEN": "canary"}',
        '{"service_SECRET": "canary"}',
    ):
        structured = ops_guard(OpsCheck(log_text=leaked_log))
        assert structured.passed is False
        assert "ops_secret" in structured.reasons


def test_reply_and_unknown_actions_are_not_approved() -> None:
    reply = ops_guard(OpsCheck(phase="shadow", action="reply"))
    assert reply.passed is False
    assert reply.human_approval_required is False
    assert "ops_action" in reply.reasons
    live = ops_guard(OpsCheck(phase="live", action="send_transaction"))
    assert live.passed is False
    assert "ops_phase" in live.reasons
    assert "ops_action" in live.reasons


def test_phase_shadow_blocks_publish_and_allows_drafts() -> None:
    draft = ops_guard(OpsCheck(phase="shadow", action="draft"))
    assert draft.passed is True
    for action in ("publish", "send", "post"):
        result = ops_guard(OpsCheck(phase="shadow", action=action))
        assert result.passed is False
        assert "ops_phase" in result.reasons
    browser = ops_guard(OpsCheck(action="browser_automation"))
    assert "action_forbidden:browser_automation" in browser.reasons
    follow = ops_guard(OpsCheck(action="bulk_follow"))
    assert "action_forbidden:bulk_follow" in follow.reasons


def test_package_source_has_no_network_client() -> None:
    root = Path(__file__).resolve().parents[2] / "meme_studio"
    assert root.is_dir()
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in _FORBIDDEN_MODULES:
                    offenders.append(f"{path.name}:{name}")
    assert offenders == []
