"""Offline synthetic boundary check; never loads RPC config or historical state.

Run: .venv/bin/python -B learning-examples/token-lifecycles/verify_creator_fee_snapshots.py
"""
# ruff: noqa: S101, TRY003, PLR2004, PLC0415, C901, PLR0915 - one synthetic boundary scenario

import asyncio
import importlib.util
import json
import socket
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

import summarize_creator_fees as summary


def main() -> None:
    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network forbidden in offline verifier")

    with (
        patch.object(socket.socket, "connect", deny_network),
        patch.object(socket, "create_connection", deny_network),
    ):
        _verify()
    print("creator snapshot boundaries: OK (synthetic, no network)")


def _verify() -> None:
    spec = importlib.util.spec_from_file_location(
        "creator_tracker", Path(__file__).with_name("track_creator_fees.py")
    )
    tracker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tracker)
    from solders.account import Account
    from solders.pubkey import Pubkey

    from platforms.pumpfun.pumpswap import (
        PUMP_SWAP_POOL_DISCRIMINATOR,
        PUMP_SWAP_PROGRAM,
    )

    creator = Pubkey.from_bytes(bytes([1]) * 32)
    other_creator = Pubkey.from_bytes(bytes([2]) * 32)
    mint = Pubkey.from_bytes(bytes([3]) * 32)
    pool = tracker.PumpSwapAddresses.derive_canonical_pool(mint, tracker.WSOL_MINT)
    vault = tracker.PumpSwapAddresses.derive_creator_vault(creator)
    ata = tracker.get_associated_token_address(vault, tracker.WSOL_MINT)
    data = (
        bytes(tracker.WSOL_MINT)
        + bytes(vault)
        + struct.pack("<Q", 2_000_000_001)
        + bytes(36)
        + b"\x01"
        + bytes(56)
    )
    account = Account(1, data, tracker.SystemAddresses.TOKEN_PROGRAM)
    assert tracker.observe_vault(account, ata, vault) == ("observed", 2_000_000_001)
    assert tracker.observe_vault(None, ata, vault) == ("vault_missing", None)
    zero_data = data[:64] + bytes(8) + data[72:]
    assert tracker.observe_vault(
        Account(1, zero_data, tracker.SystemAddresses.TOKEN_PROGRAM), ata, vault
    ) == ("observed", 0)
    for bad_data, owner in (
        (b"", tracker.SystemAddresses.TOKEN_PROGRAM),
        (b"x", tracker.SystemAddresses.TOKEN_PROGRAM),
        (data, other_creator),
        (bytes(other_creator) + data[32:], tracker.SystemAddresses.TOKEN_PROGRAM),
        (
            data[:32] + bytes(other_creator) + data[64:],
            tracker.SystemAddresses.TOKEN_PROGRAM,
        ),
        (data[:108] + b"\x00" + data[109:], tracker.SystemAddresses.TOKEN_PROGRAM),
    ):
        assert tracker.observe_vault(Account(1, bad_data, owner), ata, vault) == (
            "vault_invalid",
            None,
        )

    def snapshot(ts: str, previous: dict | None = None, **kwargs: object) -> dict:
        return tracker.make_snapshot(
            str(mint),
            pool,
            ts,
            kwargs.get("status", "observed"),
            coin_creator=kwargs.get("creator", creator),
            amount=kwargs.get("amount", 2_000_000_001),
            previous=previous,
        )

    first = snapshot("2026-01-01T00:00:00Z")
    paired = snapshot("2026-01-01T00:01:00Z", first, amount=3_000_000_002)
    assert first["baseline_status"] == "no_baseline"
    assert paired["baseline_status"] == "paired"
    assert paired["balance_change_wsol_raw"] == 1_000_000_001
    assert paired["interval_seconds"] == 60
    assert paired["coin_creator"] == str(creator)
    assert paired["creator_vault"] == str(vault)
    assert paired["creator_vault_ata"] == str(ata)
    assert "vault_sol" not in paired and "delta_sol" not in paired
    assert (
        snapshot("2026-01-01T00:01:00Z", first, amount=0)["balance_change_wsol_raw"]
        == -2_000_000_001
    )
    censored = snapshot(
        "2026-01-01T00:01:00Z", first, status="vault_missing", amount=None
    )
    assert censored["vault_wsol"] is None
    assert censored["balance_change_wsol_raw"] is None
    assert censored["baseline_status"] == "current_censored"
    for baseline, expected in (
        ({"pool": str(pool), "vault_sol": 0}, "legacy_unverifiable"),
        (censored, "previous_censored"),
        ({**first, "vault_wsol_raw": True}, "previous_censored"),
        ({**first, "ts": "not-a-time"}, "previous_censored"),
        (snapshot("2026-01-01T00:00:00Z", creator=other_creator), "identity_changed"),
        ({**first, "creator_vault_ata": str(other_creator)}, "identity_changed"),
        ({**first, "ts": "2026-01-01T00:02:00Z"}, "non_increasing_timestamp"),
    ):
        row = snapshot("2026-01-01T00:02:00Z", baseline)
        assert row["baseline_status"] == expected, (expected, row)
        assert row["balance_change_wsol"] is None

    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "snapshots.jsonl"
        original = "".join(json.dumps(row) + "\n" for row in (first, censored))
        history.write_text(original)
        with patch.object(tracker, "STATE", history):
            assert tracker.previous_balances()[str(pool)] == censored
        assert history.read_text() == original

    class OfflineClient:
        def __init__(self, response: object) -> None:
            self.response = response
            self.calls = []

        async def get_multiple_accounts(self, keys: list, **_kwargs: object) -> object:
            self.calls.append(keys)
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    for response in (tracker.RpcUnavailableError("offline outage"),):
        client = OfflineClient(response)
        result = asyncio.run(tracker.read_accounts(client, [ata]))
        assert result == [("unavailable", None)]
        assert len(client.calls) == 1
    for malformed in ([], [None, None], None):
        try:
            asyncio.run(tracker.read_accounts(OfflineClient(malformed), [ata]))
        except ValueError:
            pass
        else:
            raise AssertionError("malformed account batches must fail the snapshot")
    permanent = OfflineClient(RuntimeError("offline implementation defect"))
    try:
        asyncio.run(tracker.read_accounts(permanent, [ata]))
    except RuntimeError:
        pass
    else:
        raise AssertionError("permanent account failures must fail the snapshot")
    client = OfflineClient([None])
    assert asyncio.run(tracker.read_accounts(client, [ata])) == [("missing", None)]

    # Exercise public snapshot output, including a shared creator and a missing pool.
    authority = tracker.PumpSwapAddresses.derive_pool_authority(mint)
    _, bump = Pubkey.find_program_address(
        [
            b"pool",
            struct.pack("<H", 0),
            bytes(authority),
            bytes(mint),
            bytes(tracker.WSOL_MINT),
        ],
        PUMP_SWAP_PROGRAM,
    )
    pool_data = (
        PUMP_SWAP_POOL_DISCRIMINATOR
        + bytes([bump])
        + bytes(2)
        + bytes(authority)
        + bytes(mint)
        + bytes(tracker.WSOL_MINT)
        + bytes(104)
        + bytes(creator)
        + bytes(18)
    )
    pool_account = Account(1, pool_data, PUMP_SWAP_PROGRAM)
    other_mint = Pubkey.from_bytes(bytes([4]) * 32)
    other_pool = tracker.PumpSwapAddresses.derive_canonical_pool(
        other_mint, tracker.WSOL_MINT
    )
    other_authority = tracker.PumpSwapAddresses.derive_pool_authority(other_mint)
    _, other_bump = Pubkey.find_program_address(
        [
            b"pool",
            bytes(2),
            bytes(other_authority),
            bytes(other_mint),
            bytes(tracker.WSOL_MINT),
        ],
        PUMP_SWAP_PROGRAM,
    )
    other_data = (
        pool_data[:8]
        + bytes([other_bump])
        + pool_data[9:11]
        + bytes(other_authority)
        + bytes(other_mint)
        + pool_data[75:]
    )

    class SnapshotClient(OfflineClient):
        async def get_multiple_accounts(self, keys: list, **_kwargs: object) -> list:
            self.calls.append(keys)
            if len(self.calls) == 1:
                return [pool_account, Account(1, other_data, PUMP_SWAP_PROGRAM), None]
            return [account]

    client = SnapshotClient(None)
    rows = asyncio.run(
        tracker.collect_snapshots(
            client,
            [{"mint": str(m)} for m in (mint, other_mint, other_creator)],
            {},
            "2026-01-01T00:00:00Z",
        )
    )
    assert len(rows) == 3
    assert [row["observation_status"] for row in rows] == [
        "observed",
        "observed",
        "pool_missing",
    ]
    assert rows[0]["creator_vault_ata"] == rows[1]["creator_vault_ata"]
    assert rows[2]["vault_wsol_raw"] is None and rows[2]["coin_creator"] is None
    assert client.calls[1] == [ata], "shared creator ATA must be read only once"
    assert rows[1]["pool"] == str(other_pool)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshots.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        report = summary.load_snapshot_summary(path)
        assert report["unique_observed_creator_vaults"] == 1
        assert report["observation_status_counts"] == {"observed": 2, "pool_missing": 1}
        path.write_text("".join(json.dumps(row) + "\n" for row in (first, paired)))
        observed = summary.load_snapshot_summary(path)["observations"][0]
        assert observed["balance_change_wsol_raw"] == 1_000_000_001
        claimed = snapshot("2026-01-01T00:02:00Z", paired, amount=0)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in (first, paired, claimed))
        )
        claimed_report = summary.load_snapshot_summary(path)
        claimed_obs = claimed_report["observations"][0]
        assert claimed_obs["balance_change_wsol_raw"] == -3_000_000_002
        assert claimed_obs["accrued_wsol"] == 3.000000002
        assert claimed_obs["withdrawn_wsol"] == 3.000000002
        assert claimed_obs["withdrawal_count"] == 1
        assert claimed_obs["implied_volume_sol"] == round(3.000000002 / 0.003, 6)
        assert claimed_report["pools_with_withdrawals"] == 1
        legacy = {
            "ts": first["ts"],
            "pool": str(pool),
            "vault_sol": 0,
        }
        path.write_text(json.dumps(legacy) + "\n")
        old = summary.load_snapshot_summary(path)["observations"][0]
        assert old["observation_status"] == "legacy_unverifiable"
        assert old["vault_wsol_raw"] is None
        path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    first,
                    {**paired, "balance_change_wsol_raw": 1},
                )
            )
        )
        try:
            summary.load_snapshot_summary(path)
        except ValueError:
            pass
        else:
            raise AssertionError("inconsistent baseline arithmetic must be refused")
        downgraded = {
            **paired,
            "baseline_status": "no_baseline",
            "baseline_ts": None,
            "interval_seconds": None,
            "balance_change_wsol_raw": None,
            "balance_change_wsol": None,
        }
        path.write_text("".join(json.dumps(row) + "\n" for row in (first, downgraded)))
        try:
            summary.load_snapshot_summary(path)
        except ValueError:
            pass
        else:
            raise AssertionError("a required baseline cannot be silently downgraded")


if __name__ == "__main__":
    main()
