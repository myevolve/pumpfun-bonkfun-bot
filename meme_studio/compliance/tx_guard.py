"""tx_guard: one creator wallet, create-only buys, plan-matched sells."""

from __future__ import annotations

import hashlib
import logging
import math

from meme_studio.compliance.models import LintResult, TxIntent

_LOG = logging.getLogger(__name__)
_KNOWN_KINDS = frozenset(
    {"create_buy", "buy", "sell", "token_transfer", "sol_withdraw"}
)
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
    reasons.extend(_numeric_reasons(tx))
    if tx.kind not in _KNOWN_KINDS:
        reasons.append("tx_kind_unsupported")
        return reasons
    reasons.extend(_kind_reasons(tx))
    if _usable(tx.wallet_balance_sol) and _usable(tx.wallet_balance_cap_sol):
        balance = tx.wallet_balance_sol
        cap = tx.wallet_balance_cap_sol
        if balance is not None and cap is not None and balance > cap:
            reasons.append("tx_balance_cap")
    return reasons


def _numeric_reasons(tx: TxIntent) -> list[str]:
    values = (
        tx.sol_amount,
        tx.amount_tokens,
        tx.expected_amount_tokens,
        tx.daily_sol_spent,
        tx.dev_buy_cap_sol,
    )
    optional = (tx.daily_sol_cap, tx.wallet_balance_sol, tx.wallet_balance_cap_sol)
    if any(_bad_number(value) for value in values):
        return ["tx_amount"]
    if any(value is not None and _bad_number(value) for value in optional):
        return ["tx_amount"]
    reasons: list[str] = []
    if tx.daily_sol_cap is None or tx.wallet_balance_cap_sol is None:
        reasons.append("tx_cap_unconfigured")
    return reasons


def _bad_number(value: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return not math.isfinite(value) or value < 0


def _usable(value: float | None) -> bool:
    return value is not None and not _bad_number(value)


def _kind_reasons(tx: TxIntent) -> list[str]:
    reasons: list[str] = []
    if tx.kind == "buy":
        reasons.append("tx_buy_after_create")
    if tx.kind == "create_buy" and _over_dev_buy(tx):
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


def _over_dev_buy(tx: TxIntent) -> bool:
    return tx.sol_amount > tx.dev_buy_cap_sol


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
    if tx.daily_sol_cap is None:
        return False
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
