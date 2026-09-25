"""Shared primitives for the six-venue cycle quote core (no signing here)."""

from __future__ import annotations

import base64
import hashlib
import struct

from solders.pubkey import Pubkey

AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
SOL = "So11111111111111111111111111111111111111112"
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
CLOCK = "SysvarC1ock11111111111111111111111111111111"
FEE_DENOMINATOR = 1_000_000
RAYDIUM_API = "https://api-v3.raydium.io"
AUTHORITY = {
    AMM: "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    CPMM: "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
}


class CycleError(RuntimeError):
    """Raised on any attestation failure; caller censors, never retries blind."""


def require(ok: bool, code: str) -> None:  # noqa: FBT001 - matches paper engine API
    if not ok:
        raise CycleError(code)


def u64(data: bytes, offset: int) -> int:
    """Read a protocol little-endian integer."""
    return struct.unpack_from("<Q", data, offset)[0]


def key(data: bytes, offset: int) -> str:
    """Read a protocol public key."""
    return str(Pubkey.from_bytes(data[offset : offset + 32]))


def checked_data(account: dict | None, owner: str, size: int) -> bytes:
    """Validate ownership and exact layout before decoding an account."""
    require(account is not None, "account_missing")
    require(account["owner"] == owner and not account["executable"], "account_owner")
    require(account["data"][1] == "base64", "account_encoding")
    raw = base64.b64decode(account["data"][0], validate=True)
    require(len(raw) == size, "account_layout")
    return raw


def discriminator(name: str) -> bytes:
    """Anchor account discriminator: sha256("account:<name>")[:8]."""
    return hashlib.sha256(f"account:{name}".encode()).digest()[:8]
