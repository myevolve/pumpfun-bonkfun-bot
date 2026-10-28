# ruff: noqa: TRY003
"""Resolve Solana transport settings from an explicit environment mapping.

Paper replay does not call this module. Endpoints must be Chainstack or an
explicitly named paid host. Public clusters and known free providers are
rejected even if a caller tries to allow them. This module does not open
credential files and does not include endpoint values in errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_FREE_PROVIDER_SUFFIXES = (
    "solana.com",
    "projectserum.com",
    "ankr.com",
    "publicnode.com",
    "solanabeach.io",
    "extrnode.com",
    "rpcpool.com",
)
_ALLOW_SUFFIXES = ("chainstack.com",)
_PAID_HOSTS_NAME = "SOLANA_PAID_RPC_HOSTS"


def resolve_solana_endpoint(environ: Mapping[str, str]) -> str:
    """Return SOLANA_NODE_RPC_ENDPOINT or raise if it is missing or not allowed."""
    return _require_allowed_url(environ, "SOLANA_NODE_RPC_ENDPOINT")


def resolve_geyser(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return the configured Geyser endpoint and token.

    Args:
        environ: Explicit name-to-value mapping. Only GEYSER_ENDPOINT,
            GEYSER_API_TOKEN, and SOLANA_PAID_RPC_HOSTS are read.

    Returns:
        Endpoint URL and token.

    Raises:
        ValueError: A required name is missing, or the host is not allowed.
    """
    endpoint = _require_allowed_url(environ, "GEYSER_ENDPOINT")
    token = environ.get("GEYSER_API_TOKEN")
    if not isinstance(token, str) or not token:
        raise ValueError("GEYSER_API_TOKEN is UNKNOWN")
    return endpoint, token


def _require_allowed_url(environ: Mapping[str, str], name: str) -> str:
    url = environ.get(name)
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{name} is UNKNOWN")
    host = _host(url)
    if host is None or _is_free(host):
        raise ValueError(f"public Solana endpoint is not allowed for {name}")
    if not _allowed(host, _extra_hosts(environ)):
        raise ValueError(f"endpoint host is not an allowed paid provider for {name}")
    return url.strip()


def _extra_hosts(environ: Mapping[str, str]) -> frozenset[str]:
    raw = environ.get(_PAID_HOSTS_NAME, "")
    if not isinstance(raw, str) or not raw.strip():
        return frozenset()
    return frozenset(_canonical_host(part) for part in raw.split(",") if part.strip())


def _allowed(host: str, extra: frozenset[str]) -> bool:
    if _is_free(host):
        return False
    if any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOW_SUFFIXES):
        return True
    return host in extra and not _is_free(host)


def _is_free(host: str) -> bool:
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _FREE_PROVIDER_SUFFIXES
    )


def _host(url: str) -> str | None:
    rest = url.strip()
    lower = rest.lower()
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if lower.startswith(prefix):
            rest = rest[len(prefix) :]
            break
    else:
        return None
    host = rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0]
    return _canonical_host(host)


def _canonical_host(host: str) -> str:
    return host.strip().rstrip(".").lower()
