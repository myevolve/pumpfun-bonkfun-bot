from __future__ import annotations

import pytest

from utils.logger import _redact, _sanitize_argument, register_secret


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "mnemonic: 'alpha bravo charlie delta'",
            "mnemonic: [REDACTED]",
        ),
        (
            'seed phrase = "alpha bravo charlie delta" followed',
            "seed phrase = [REDACTED] followed",
        ),
        ("private_key='single-token'", "private_key=[REDACTED]"),
        (
            "mnemonic: 'alpha bravo charlie delta",
            "mnemonic: [REDACTED]",
        ),
        (
            'seed phrase = "alpha bravo charlie delta',
            "seed phrase = [REDACTED]",
        ),
        ("api_key=plain-token", "api_key=[REDACTED]"),
        (
            "authorization: Bearer opaque-token",
            "authorization: [REDACTED]",
        ),
        (
            "connected https://user:password@example.com/rpc",
            "connected https://[REDACTED]@example.com/rpc",
        ),
        (
            "connected https://example.com/rpc?api-key=opaque&region=us",
            "connected https://example.com/rpc?api-key=[REDACTED]&region=us",
        ),
        (
            "connected wss://user:password@example.com/socket",
            "connected wss://[REDACTED]@example.com/socket",
        ),
        (
            "metadata={'x-token': 'opaque-token'}",
            "metadata={'x-token': [REDACTED]}",
        ),
    ],
)
def test_sensitive_labels_redact_quoted_and_single_token_values(
    message: str,
    expected: str,
) -> None:
    assert _redact(message) == expected


def test_sensitive_mapping_fields_are_redacted() -> None:
    assert _sanitize_argument(
        {
            "api_key": "plain-api-key",
            "geyser_api_token": "plain-token",
            "authorization": "Bearer opaque-token",
            "x-token": "plain-token",
            "next_token": "public-cursor",
            "index_token": "public-index",
            "region": "us",
        }
    ) == {
        "api_key": "[REDACTED]",
        "geyser_api_token": "[REDACTED]",
        "authorization": "[REDACTED]",
        "x-token": "[REDACTED]",
        "next_token": "public-cursor",
        "index_token": "public-index",
        "region": "us",
    }


def test_registered_secret_contract_is_base58_only() -> None:
    with pytest.raises(ValueError, match="Base58"):
        register_secret("opaque_api-token")


def test_registered_base58_secret_is_redacted() -> None:
    secret = "A" * 32
    register_secret(secret)
    assert _redact(f"credential {secret}") == "credential [REDACTED]"


def test_recursive_mapping_is_sanitized_without_crashing() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    assert _sanitize_argument(recursive) == {"self": "[RECURSIVE]"}
