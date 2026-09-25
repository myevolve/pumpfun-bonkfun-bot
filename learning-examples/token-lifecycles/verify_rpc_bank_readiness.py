"""Offline: classify minimum-slot refusals without hiding other RPC failures."""

from copy import deepcopy

import simulate_menu_orca_pair as evidence


def error_code(request: object, response: object) -> str:
    """Return the machine-readable refusal, never accept an error as a result."""
    try:
        evidence.rpc_results(request, response)
    except evidence.StudyError as exc:
        return str(exc)
    raise AssertionError


def self_check() -> None:
    """Check correlated single/batch readiness and fail-closed error boundaries."""
    request = evidence.single(
        "getMultipleAccounts",
        [[evidence.atomic.CLOCK], {"minContextSlot": 448657208}],
    )
    refusal = {"jsonrpc": "2.0", "id": 0, "error": {"code": -32016}}
    ready_code = "rpc_min_context_slot_not_reached"
    evidence.require(
        error_code(request, refusal) == ready_code, "readiness_unclassified"
    )

    calls = [request, {**deepcopy(request), "id": 1}]
    replies = [{"jsonrpc": "2.0", "id": 1, "result": {}}, refusal]
    evidence.require(
        error_code(calls, replies) == ready_code, "partial_batch_readiness"
    )
    replies[0] = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}}
    evidence.require(
        error_code(calls, replies) == "rpc_api_error", "mixed_error_was_hidden"
    )
    replies.reverse()
    replies[1]["error"] = {"code": "invalid"}
    evidence.require(
        error_code(calls, replies) == "rpc_error_shape", "later_bad_error_was_hidden"
    )
    evidence.require(
        error_code(request, {**refusal, "id": 1}) == "rpc_response_correlation",
        "uncorrelated_error_was_censored",
    )
    no_floor = deepcopy(request)
    no_floor["params"][1].clear()
    evidence.require(
        error_code(no_floor, refusal) == "rpc_api_error", "unrequested_floor_censored"
    )
    evidence.require(
        error_code({**request, "method": "sendTransaction"}, refusal)
        == "rpc_api_error",
        "non_read_error_was_censored",
    )
    public = {"meta": {"logMessages": ["https://arweave.net/public-metadata"]}}
    secrets = ("synthetic-private-token",)
    evidence.require(
        not evidence.contains_secret(public, secrets, public_data=True),
        "public_metadata_refused",
    )
    evidence.require(
        evidence.contains_secret(public, secrets), "provider_error_url_not_redacted"
    )
    public["meta"]["logMessages"].append(secrets[0])
    evidence.require(
        evidence.contains_secret(public, secrets, public_data=True),
        "known_secret_allowed_in_public_data",
    )
    print(
        "PASS: bank readiness, fatal error boundaries, public metadata and secret rejection"
    )


if __name__ == "__main__":
    self_check()
