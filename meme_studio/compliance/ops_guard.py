"""ops_guard: one X account, no paid engagement, no secrets, drafts only."""

from __future__ import annotations

import hashlib
import logging
import re

from meme_studio.compliance.blocklists import paid_engagement_hit
from meme_studio.compliance.models import LintResult, OpsCheck

_LOG = logging.getLogger(__name__)
_SEND_ACTIONS = frozenset({"publish", "send", "post"})
_FORBIDDEN = {
    "like": "action_forbidden:like",
    "bulk_follow": "action_forbidden:bulk_follow",
    "bulk_unfollow": "action_forbidden:bulk_unfollow",
    "bulk_list_add": "action_forbidden:bulk_list_add",
    "browser_automation": "action_forbidden:browser_automation",
    "ai_reply": "action_forbidden:ai_reply",
}
_SECRET = re.compile(
    r"solana_private_key|private[_ -]?key|api_key\s*=|authorization:\s*bearer|"
    r"bot\d+:[A-Za-z0-9_-]{20,}",
    re.IGNORECASE,
)
_SECOND_ACCOUNT = 1


def ops_guard(check: OpsCheck) -> LintResult:
    """Refuse a second account, a paid-engagement dependency, or a send.

    Phase 0 has no publisher. ``publish``, ``send``, and ``post`` fail closed.
    """
    reasons = tuple(dict.fromkeys(_reasons(check)))
    result = LintResult(
        passed=not reasons,
        reasons=reasons,
        human_approval_required=False,
        payload_hash=_hash(check),
    )
    _LOG.info(
        "ops_guard %s %s %s",
        result.status,
        result.payload_hash,
        ",".join(result.reasons),
    )
    return result


def _reasons(check: OpsCheck) -> list[str]:
    reasons: list[str] = []
    if len(check.x_accounts) > _SECOND_ACCOUNT:
        reasons.append("ops_second_account")
    if any(paid_engagement_hit(item) for item in check.dependencies):
        reasons.append("ops_paid_engagement")
    if _SECRET.search(check.log_text):
        reasons.append("ops_secret")
    if check.action in _SEND_ACTIONS:
        reasons.append("ops_phase")
    forbidden = _FORBIDDEN.get(check.action)
    if forbidden:
        reasons.append(forbidden)
    return reasons


def _hash(check: OpsCheck) -> str:
    payload = "|".join(
        (
            ",".join(check.x_accounts),
            ",".join(check.dependencies),
            check.phase,
            check.action,
            hashlib.sha256(check.log_text.encode("utf-8")).hexdigest(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
