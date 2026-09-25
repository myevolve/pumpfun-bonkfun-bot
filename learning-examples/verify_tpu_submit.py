"""Verify TPU-direct submit path: leader resolution, framing, containment.

Offline: python learning-examples/verify_tpu_submit.py --self-check
Live leader lookup (read-only RPC, provider projection):
    python learning-examples/verify_tpu_submit.py --live \
      --credentials .state/wallets/live-readiness.secrets

The live lookup resolves the TPU addresses of slots now+1..now+4 and optionally
fires one malformed probe datagram with --probe; validators drop invalid
packets, no signing and no funds are involved. This is the transport audit for
the bot's TPU-direct submission channel (src/core/tpu.py).
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# Offline assertions only; live lookup is read-only plus one optional probe.
# ruff: noqa: S101, PLR2004, ANN204, ARG005, PLC0415, SLF001, TRY003


def self_check() -> None:
    from core.tpu import TpuSubmitter, resolve_tpu_targets

    # Mocked RPC reads: current slot, four leaders, two nodes with TPUs.
    leaders = ["LeadeR1" * 4, "LeadeR2" * 4, "LeadeR3" * 4, "LeadeR4" * 4]

    class FakeNode:
        def __init__(self, pubkey: str, tpu: str | None, tpu_quic: str | None):
            self.pubkey = pubkey
            self.tpu = tpu
            self.tpu_quic = tpu_quic

    nodes = [
        FakeNode(leaders[0], "10.0.0.1:8001", "10.0.0.1:8003"),
        FakeNode(leaders[1], None, "10.0.0.2:8003"),  # no classic TPU port
        FakeNode(leaders[2], "10.0.0.3:8001", None),
        FakeNode("9MFfWXdTmtWi9CdnduujpKGVP2gCgyjtyD9e2XC7wLCe", "10.0.0.9:8001", None),
    ]
    targets = resolve_tpu_targets([n.__dict__ for n in nodes], leaders, forward_slots=3)
    # Leader 0 and 2 have classic TPU ports; leader 1 is QUIC-only (skip);
    # leader 3 is not in the node set (skip). The unrelated node never matches.
    assert targets == [("10.0.0.1", 8001, "udp"), ("10.0.0.3", 8001, "udp")], targets
    assert len(leaders) == 4

    submitter = TpuSubmitter(
        lambda operation: (_ for _ in ()).throw(RuntimeError),
        rpc_endpoint="http://unused",
    )
    submitter._tpu_targets = targets
    # UDP payload is the raw serialized transaction, no envelope.
    payload = b"\x01" + bytes(range(64)) + b"\x02" + bytes(64)
    assert submitter.send(payload) == 2  # one datagram per reachable leader
    # Failure containment: send never raises even with a dead target. The
    # kernel may deliver to a closed port without error, so assert only that
    # the send is contained and the counter reflects OS acceptance.
    submitter._tpu_targets = [("127.0.0.1", 1, "udp")]
    assert submitter.send(payload) in (0, 1)
    # QUIC targets are exercised by send_quic against live leaders only.
    print("PASS: leader resolution, packet framing, failure containment")


async def live_lookup(credentials: Path, *, send_probe: bool) -> None:
    """Resolve leader TPUs from the epoch leader schedule + cluster nodes."""
    from solana.rpc.async_api import AsyncClient
    from solders.pubkey import Pubkey

    from core.tpu import resolve_tpu_targets

    secrets = json_loads_safe(credentials)
    rpc = secrets["SOLANA_NODE_RPC_ENDPOINT"]
    async with AsyncClient(rpc) as client:
        epoch_info = await client.get_epoch_info()
        epoch = epoch_info.value.epoch
        schedule = await client.get_leader_schedule()
    # leader_schedule maps identity -> list of slot indexes within the epoch.
    by_slot: dict[int, str] = {}
    offset = epoch * epoch_info.value.slots_in_epoch
    for identity, indexes in (schedule.value or {}).items():
        identity_text = str(identity)
        for index in indexes:
            absolute = offset + int(index)
            if absolute > int(epoch_info.value.absolute_slot):
                by_slot.setdefault(absolute, identity_text)
    upcoming = sorted(by_slot)[:4]
    nodes = list((await AsyncClient(rpc).get_cluster_nodes()).value)
    leaders = [Pubkey.from_string(by_slot[s]) for s in upcoming]
    targets = resolve_tpu_targets(nodes, leaders, forward_slots=4)
    print(f"slots={upcoming} tpu_targets={targets}")
    if send_probe and targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b"\x00" * 64, targets[0])
        sock.close()
        print("probe datagram sent (malformed, will be dropped)")


def json_loads_safe(path: Path) -> dict:
    from dotenv import dotenv_values

    return dict(dotenv_values(Path(path)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--live", action="store_true")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.live:
        if args.credentials is None:
            raise SystemExit("--live requires --credentials")
        asyncio.run(live_lookup(args.credentials, send_probe=args.probe))
        return
    raise SystemExit("choose --self-check or --live")


if __name__ == "__main__":
    main()
