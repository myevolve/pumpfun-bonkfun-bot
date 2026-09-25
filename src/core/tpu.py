"""TPU-direct submission: QUIC streams to current slot leaders (tpuQuic).

Mainnet reality (measured 2026-09-23): 0 of 3,780 cluster nodes publish the
legacy UDP ``tpu`` port; 3,345 publish ``tpuQuic``. This module therefore uses
QUIC (ALPN ``solana-tpu``) to the leaders' ``tpuQuic`` addresses, with the
documented wire framing: one unidirectional stream per transaction, a
4-byte little-endian transaction-length header, then the raw wire bytes.

Fire-and-forget secondary channel alongside the HTTP RPC submission.
Validators deduplicate by signature, so racing the RPC path is harmless.
Failures are contained: neither resolution nor send may break the primary
submission flow. Server certificates are self-signed against the validator
identity, so verification is disabled and identity binding is not enforced
here (the RPC path remains the authoritative fallback).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import time
import typing
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)


def resolve_tpu_targets(
    nodes: list, leaders: list, *, forward_slots: int
) -> list[tuple[str, int, str]]:
    """Map the leaders for the next ``forward_slots`` slots to (host, port, kind).

    ``kind`` is ``"quic"`` when the node publishes ``tpuQuic`` and ``"udp"``
    for the legacy plain-UDP ``tpu`` port. QUIC-only nodes are normal on
    current mainnet. Nodes without either port and leader keys absent from the
    node set are skipped. Duplicate (host, port) collapse.
    """
    normalized = [
        json.loads(node.to_json()) if hasattr(node, "to_json") else node
        for node in nodes
    ]
    by_pubkey = {str(node["pubkey"]): node for node in normalized}
    targets: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()
    for leader in leaders[:forward_slots]:
        node = by_pubkey.get(str(leader))
        if node is None:
            continue
        for field, kind in (("tpu", "udp"), ("tpuQuic", "quic")):
            address = node.get(field)
            if not address:
                continue
            parsed = urlsplit(f"//{address}")
            if parsed.hostname is None or parsed.port is None:
                continue
            target = (parsed.hostname, parsed.port)
            if target in seen:
                continue
            seen.add(target)
            targets.append((*target, kind))
            break
    return targets


class TpuSubmitter:
    """Maintain upcoming leader TPU targets and submit via QUIC or UDP."""

    REFRESH_SECONDS = 2.0

    def __init__(
        self,
        rpc_read: typing.Callable[
            [typing.Callable[[object], typing.Awaitable[object]]],
            typing.Awaitable[object],
        ],
        *,
        rpc_endpoint: str,
        forward_slots: int = 3,
    ) -> None:
        """``rpc_read`` runs an idempotent RPC read against the trading node."""
        self._rpc_read = rpc_read
        self._rpc_endpoint = rpc_endpoint
        self._forward_slots = forward_slots
        self._tpu_targets: list[tuple[str, int, str]] = []
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self.sent_count = 0
        self.last_refresh_error: str | None = None
        self.last_refresh_monotonic: float | None = None

    def start(self) -> None:
        """Start the background leader refresher (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Cancel the refresher."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _refresh_loop(self) -> None:
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    slot = await self._rpc_call(session, "getSlot", [])
                    leaders = await self._rpc_call(
                        session,
                        "getSlotLeaders",
                        [int(slot) + 1, self._forward_slots],
                    )
                    nodes = await self._rpc_call(session, "getClusterNodes", [])
                resolved = resolve_tpu_targets(
                    list(nodes), list(leaders), forward_slots=self._forward_slots
                )
                self._tpu_targets = resolved
                self.last_refresh_monotonic = time.monotonic()
                self.last_refresh_error = None
            except Exception as exc:  # noqa: BLE001 - background task only
                self.last_refresh_error = type(exc).__name__
                logger.debug(f"TPU leader refresh failed: {exc!s}")
            await asyncio.sleep(self.REFRESH_SECONDS)

    async def _rpc_call(
        self, session: aiohttp.ClientSession, method: str, params: list
    ) -> list | int:
        """One raw JSON-RPC call against the trading node; returns result."""
        async with session.post(
            self._rpc_endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            payload = await response.json()
        if payload.get("error") is not None:
            raise RuntimeError(f"RPC {method} failed: {payload['error'].get('code')}")
        return payload["result"]

    def send(self, wire: bytes) -> int:
        """Fire-and-forget one datagram per cached UDP target (legacy path).

        Returns the number of datagrams handed to the OS; never raises.
        """
        delivered = 0
        for host, port, kind in self._tpu_targets:
            if kind != "udp":
                continue
            try:
                self._socket_send(wire, (host, port))
                delivered += 1
                self.sent_count += 1
            except OSError as exc:
                logger.debug(f"TPU UDP send to {host}:{port} failed: {exc!s}")
        return delivered

    async def send_quic(self, wire: bytes) -> int:
        """Open a QUIC stream per leader and push the framed transaction.

        Returns the number of leaders that accepted the stream; never raises.
        Each connection is short-lived: the handshake (~1 RTT) is paid per
        submission, which is still materially cheaper than the HTTP RPC path
        measured at 1.2-1.5 s end-to-end.
        """
        from aioquic.asyncio.client import connect
        from aioquic.quic.configuration import QuicConfiguration

        delivered = 0
        for host, port, kind in self._tpu_targets:
            if kind != "quic":
                continue
            started = time.monotonic()
            try:
                config = QuicConfiguration(
                    is_client=True, alpn_protocols=["solana-tpu"]
                )
                config.verify_mode = ssl.CERT_NONE
                config.server_name = None
                async with connect(
                    host, port, configuration=config, wait_connected=True
                ) as quic:
                    writer = await quic.create_stream()
                    writer.write(len(wire).to_bytes(4, "little") + wire)
                    await writer.drain()
                delivered += 1
                self.sent_count += 1
                logger.debug(
                    f"TPU QUIC submit to {host}:{port} ok in "
                    f"{time.monotonic() - started:.3f}s"
                )
            except Exception as exc:  # noqa: BLE001 - containment required
                logger.debug(
                    f"TPU QUIC submit to {host}:{port} failed: {type(exc).__name__}"
                )
        return delivered

    def _socket_send(self, wire: bytes, target: tuple[str, int]) -> None:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setblocking(False)  # noqa: FBT003 - stdlib socket API
        self._socket.sendto(wire, target)

    @property
    def targets(self) -> list[tuple[str, int, str]]:
        """Currently cached leader TPU targets."""
        return list(self._tpu_targets)
