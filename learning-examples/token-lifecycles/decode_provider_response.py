"""Project provider responses into bounded diagnostics, never arbitrary text.

Read-only helpers for research transports. Diagnostics describe what a provider
reported; they do not identify the account's plan. The explicit retry policy
below applies only to bounded read-only research calls. Running this module
executes only offline disclosure, parsing and retry-policy checks.
"""

# Protocol bounds and synthetic boundary cases deliberately use literal values.
# ruff: noqa: PLR2004

from __future__ import annotations

import json
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_ERROR_BODY_BYTES = 8192
RATE_LIMIT_MAX_ATTEMPTS = 3
RATE_LIMIT_MAX_DELAY_SECONDS = 5.0
RATE_LIMIT_MAX_ELAPSED_SECONDS = 10.0
_MAX_HEADER_INTEGER = 2**53 - 1
_MAX_RETRY_SECONDS = 366 * 24 * 60 * 60
_RATE_HEADERS = (
    "RateLimit-Limit",
    "RateLimit-Remaining",
    "RateLimit-Reset",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)
_HTTP_CLASSES = {
    400: "invalid_request",
    401: "authentication",
    403: "forbidden",
    413: "payload_too_large",
    429: "rate_or_connection_limit",
    500: "internal_error",
    502: "upstream_unavailable",
    503: "service_unavailable",
    504: "gateway_timeout",
}
_RPC_CLASSES = {
    -32700: "invalid_json",
    -32600: "invalid_request",
    -32601: "method_unavailable",
    -32602: "invalid_parameters",
    -32603: "internal_error",
}
_MESSAGE_CLASSES = {
    "too many requests": "rate_or_connection_limit",
    "rate limit exceeded": "rate_limit",
    "connection limit exceeded": "connection_limit",
    "quota exceeded": "quota_exhausted",
    "monthly request quota exceeded": "quota_exhausted",
    "unauthorized": "authentication",
}


def _decimal(value: str, maximum: int) -> int | None:
    text = value.strip(" \t")
    if not 1 <= len(text) <= 16 or not text.isascii() or not text.isdecimal():
        return None
    number = int(text)
    return number if number <= maximum else None


def headers_evidence(status: int, headers: Mapping[str, str]) -> dict:
    """Retain status and fixed numeric fields, not the header dictionary.

    Reset fields retain their names and numbers without inventing units. Retry-
    After accepts bounded delta seconds or a canonical IMF-fixdate. Invalid and
    absent are distinct; neither changes the caller's transport policy.
    """
    retry: dict = {"state": "absent"}
    value = headers.get("Retry-After")
    if value is not None:
        retry = {"state": "invalid"}
        seconds = _decimal(value, _MAX_RETRY_SECONDS)
        if seconds is not None:
            retry = {"state": "delay_seconds", "seconds": seconds}
        elif len(value) == 29 and value.isascii() and value.endswith(" GMT"):
            try:
                date = parsedate_to_datetime(value).astimezone(UTC)
                if (
                    1970 <= date.year <= 2100
                    and date.strftime("%a, %d %b %Y %H:%M:%S GMT") == value
                ):
                    retry = {
                        "state": "http_date",
                        "unix_seconds": int(date.timestamp()),
                    }
            except (ValueError, TypeError, OverflowError):
                pass
    budgets = {}
    invalid = []
    for name in _RATE_HEADERS:
        value = headers.get(name)
        if value is None:
            continue
        number = _decimal(value, _MAX_HEADER_INTEGER)
        if number is None:
            invalid.append(name)
        else:
            budgets[name] = number
    return {
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "http_status_class": _HTTP_CLASSES.get(status, "unknown"),
        "retry_after": retry,
        "rate_limit_headers": budgets,
        "invalid_rate_limit_headers": invalid,
    }


def rate_limit_retry_delay(
    diagnostics: dict, attempt: int, *, now_unix: float
) -> float | None:
    """Return a bounded 429 delay, or refuse another attempt.

    Retry-After is a minimum, never a value to clamp down. An invalid directive
    also refuses retries: it may represent a delay beyond the parser's bound.
    Callers still own body completeness, elapsed deadlines and request budgets.
    """
    if (
        diagnostics.get("http_status") != 429
        or type(attempt) is not int
        or not 1 <= attempt < RATE_LIMIT_MAX_ATTEMPTS
    ):
        return None
    retry = diagnostics.get("retry_after", {"state": "absent"})
    delay = float(2 ** (attempt - 1))
    if retry["state"] == "delay_seconds":
        delay = max(delay, retry["seconds"])
    elif retry["state"] == "http_date":
        delay = max(delay, retry["unix_seconds"] - now_unix)
    elif retry["state"] != "absent":
        return None
    return delay if delay <= RATE_LIMIT_MAX_DELAY_SECONDS else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_diagnostic_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite_diagnostic_json_number")


def body_evidence(body: bytes | None, request_id: int) -> dict:
    """Describe a complete bounded error body without retaining its contents.

    Exact message matches become fixed provider-reported enums, not root-cause
    conclusions. Identity mismatch, duplicate keys, nonfinite JSON, arbitrary
    messages/data and oversized bodies cannot supply a recognized reason.
    """
    result = {
        "body_kind": "unavailable",
        "rpc_identity_matches_request": False,
        "rpc_error_code": None,
        "provider_reported_reason": "unknown",
    }
    if body is None:
        return result
    if len(body) > MAX_ERROR_BODY_BYTES:
        return {**result, "body_kind": "over_diagnostic_limit"}
    try:
        value = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        return {**result, "body_kind": "not_strict_json"}
    result["body_kind"] = "json"
    if not (
        isinstance(value, dict)
        and value.get("jsonrpc") == "2.0"
        and type(request_id) is int
        and type(value.get("id")) is int
        and value["id"] == request_id
    ):
        return result
    result["rpc_identity_matches_request"] = True
    error = value.get("error")
    if not isinstance(error, dict) or "result" in value:
        return result
    code = error.get("code")
    if type(code) is int and -(2**31) <= code < 2**31:
        result["rpc_error_code"] = code
        result["provider_reported_reason"] = _RPC_CLASSES.get(code, "unknown")
    message = error.get("message")
    if isinstance(message, str) and len(message) <= 128:
        reason = _MESSAGE_CLASSES.get(message.strip().lower())
        if reason is not None:
            result["provider_reported_reason"] = reason
    return result


def self_check() -> None:
    """Check observable refusal distinctions and non-disclosure, entirely offline."""
    secret = "SYNTHETIC_PROVIDER_SECRET_DO_NOT_EMIT"  # noqa: S105 - synthetic disclosure sentinel
    headers = headers_evidence(
        429,
        {
            "Retry-After": "7",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Limit": secret,
            "Authorization": secret,
            "Set-Cookie": secret,
            "Location": "https://unused.invalid/" + secret,
        },
    )
    assert headers["http_status"] == 429  # noqa: S101
    assert headers["retry_after"] == {"state": "delay_seconds", "seconds": 7}  # noqa: S101
    assert headers["rate_limit_headers"] == {"X-RateLimit-Remaining": 0}  # noqa: S101
    assert headers_evidence(429, {})["retry_after"]["state"] == "absent"  # noqa: S101
    for value in (secret, "-1", "1.5", "\u0661", "9" * 100):
        assert (  # noqa: S101 - offline boundary check
            headers_evidence(429, {"Retry-After": value})["retry_after"]["state"]
            == "invalid"
        )
    assert (  # noqa: S101 - offline boundary check
        headers_evidence(429, {"Retry-After": "Tue, 15 Sep 2026 12:00:00 GMT"})[
            "retry_after"
        ]["state"]
        == "http_date"
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32000,
            "message": "connection limit exceeded",
            "data": secret,
        },
    }
    known = body_evidence(json.dumps(payload).encode(), 1)
    assert known["provider_reported_reason"] == "connection_limit"  # noqa: S101
    assert (  # noqa: S101 - offline boundary check
        body_evidence(json.dumps(payload).encode(), 2)["provider_reported_reason"]
        == "unknown"
    )
    payload["error"]["message"] = "quota exceeded " + secret
    unknown = body_evidence(json.dumps(payload).encode(), 1)
    assert unknown["provider_reported_reason"] == "unknown"  # noqa: S101
    for body in (
        b'{"id":1,"id":1}',
        b'{"x":NaN}',
        b"[" * 2000,
        b"x" * (MAX_ERROR_BODY_BYTES + 1),
    ):
        assert body_evidence(body, 1)["rpc_error_code"] is None  # noqa: S101
    assert rate_limit_retry_delay(headers_evidence(429, {}), 1, now_unix=0) == 1  # noqa: S101
    assert rate_limit_retry_delay(headers_evidence(429, {}), 2, now_unix=0) == 2  # noqa: S101
    assert rate_limit_retry_delay(headers_evidence(429, {}), 3, now_unix=0) is None  # noqa: S101
    for status in (200, 401, 403, 500, 503):
        assert (  # noqa: S101 - only HTTP rate limits are retryable
            rate_limit_retry_delay(headers_evidence(status, {}), 1, now_unix=0) is None
        )
    for value, expected in (
        ("0", 1),
        ("4", 4),
        ("6", None),
        ("-1", None),
        ("9" * 100, None),
    ):
        assert (  # noqa: S101 - never shorten an advertised or unparseable delay
            rate_limit_retry_delay(
                headers_evidence(429, {"Retry-After": value}), 1, now_unix=0
            )
            == expected
        )
    date_headers = headers_evidence(
        429, {"Retry-After": "Tue, 15 Sep 2026 12:00:00 GMT"}
    )
    due = date_headers["retry_after"]["unix_seconds"]
    assert rate_limit_retry_delay(date_headers, 1, now_unix=due - 4) == 4  # noqa: S101
    assert rate_limit_retry_delay(date_headers, 1, now_unix=due - 6) is None  # noqa: S101
    assert rate_limit_retry_delay(date_headers, 1, now_unix=due + 1) == 1  # noqa: S101
    assert secret not in json.dumps([headers, known, unknown])  # noqa: S101
    print(
        "provider response self-check passed (offline; no raw headers, body or secret emitted)"
    )


if __name__ == "__main__":
    self_check()
