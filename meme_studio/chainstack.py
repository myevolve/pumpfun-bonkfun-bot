# ruff: noqa: TRY003
"""Resolve Solana transport settings from an explicit environment mapping.

Paper replay does not call this module. A future read path must pass the
process environment (or a test double) and must not fall back to the public
cluster. This module does not open credential files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_PUBLIC_HOSTS = frozenset(
    {
        "api.mainnet-beta.solana.com",
        "api.devnet.solana.com",
        "api.testnet.solana.com",
    }
)


def resolve_solana_endpoint(environ: Mapping[str, str]) -> str:
    """Return SOLANA_NODE_RPC_ENDPOINT or raise if it is missing or public."""
    return _require_private_url(environ, "SOLANA_NODE_RPC_ENDPOINT")


def resolve_geyser(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return the configured Geyser endpoint and token.

    Args:
        environ: Explicit name-to-value mapping. Only GEYSER_ENDPOINT and
            GEYSER_API_TOKEN are read.

    Returns:
        Endpoint URL and token.

    Raises:
        ValueError: Either value is missing, or the endpoint is a public host.
    """
    endpoint = _require_private_url(environ, "GEYSER_ENDPOINT")
    token = environ.get("GEYSER_API_TOKEN")
    if not isinstance(token, str) or not token:
        raise ValueError("GEYSER_API_TOKEN is UNKNOWN")
    return endpoint, token


def _require_private_url(environ: Mapping[str, str], name: str) -> str:
    url = environ.get(name)
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{name} is UNKNOWN")
    if _host(url) in _PUBLIC_HOSTS:
        raise ValueError(f"public Solana endpoint is not allowed for {name}")
    return url


def _host(url: str) -> str:
    rest = url.strip()
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if rest.startswith(prefix):
            rest = rest[len(prefix) :]
            break
    return rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0].lower()
