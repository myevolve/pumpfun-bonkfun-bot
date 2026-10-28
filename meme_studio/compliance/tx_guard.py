"""tx_guard: one creator wallet, create-only buys, plan-matched sells."""

from __future__ import annotations

import hashlib
import logging

from meme_studio.compliance.models import LintResult, TxIntent

_LOG = logging.getLogger(__name__)
_SPEND_KINDS = frozenset({"create_buy", "buy", "sol_withdraw"})


def tx_guard(tx: TxIntent) -> LintResult:
    """Refuse a transaction that spends outside the disclosed plan.

    This does not sign or broadcast. Callers in phase 0 only record the result.
    """
    reasons = tuple(dict.fromkeys(_reasons(tx)))
    result = LintResult(
        passed=not reasons,
        reasons=reasons,
        human_approval_required=False,
        payload_hash=_hash(tx),
    )
    _LOG.info(
        "tx_guard %s %s %s",
        result.status,
        result.payload_hash,
        ",".join(result.reasons),
    )
    return result


def _reasons(tx: TxIntent) -> list[str]:
    reasons: list[str] = []
    if tx.signers != (tx.creator_wallet,):
        reasons.append("tx_signer")
    reasons.extend(_kind_reasons(tx))
    if tx.wallet_balance_sol > tx.wallet_balance_cap_sol:
        reasons.append("tx_balance_cap")
    return reasons


def _kind_reasons(tx: TxIntent) -> list[str]:
    reasons: list[str] = []
    if tx.kind == "buy":
        reasons.append("tx_buy_after_create")
    if tx.kind == "create_buy" and tx.sol_amount > tx.dev_buy_cap_sol:
        reasons.append("tx_dev_buy_cap")
    if tx.kind == "sell":
        reasons.extend(_sell_reasons(tx))
    if tx.kind == "token_transfer":
        reasons.append("tx_hidden_distribution")
    if tx.kind == "sol_withdraw" and _bad_withdraw(tx):
        reasons.append("tx_withdraw_destination")
    if tx.kind in _SPEND_KINDS and _over_daily_cap(tx):
        reasons.append("tx_daily_cap")
    return reasons


def _sell_reasons(tx: TxIntent) -> list[str]:
    reasons: list[str] = []
    if tx.plan_step != tx.expected_step:
        reasons.append("tx_plan_step")
    if tx.amount_tokens != tx.expected_amount_tokens:
        reasons.append("tx_plan_amount")
    if not tx.plan_hash or tx.plan_hash != tx.committed_plan_hash:
        reasons.append("tx_plan_hash")
    return reasons


def _bad_withdraw(tx: TxIntent) -> bool:
    if not tx.cold_address:
        return True
    return tx.destination != tx.cold_address


def _over_daily_cap(tx: TxIntent) -> bool:
    return tx.daily_sol_spent + tx.sol_amount > tx.daily_sol_cap


def _hash(tx: TxIntent) -> str:
    payload = "|".join(
        (
            tx.creator_wallet,
            ",".join(tx.signers),
            tx.kind,
            str(tx.sol_amount),
            str(tx.amount_tokens),
            tx.plan_step,
            tx.plan_hash,
            tx.destination,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
