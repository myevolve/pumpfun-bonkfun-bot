"""Read the live LaunchLab fee configuration — READ-ONLY, no funds.

The pump.fun endgame is measured: creator self-sweep -> fee annuity, LP
route locked (0 bps). The cross-venue question is whether letsbonk
(Raydium LaunchLab) differs: its fee model lives in two config accounts,

  GlobalConfig   trade_fee_rate, max_share_fee_rate, migrate_fee
                 (rates in hundredths of a bip, 1e-6)
  PlatformConfig fee_rate, creator_fee_rate, and the migration split
                 platform_scale / creator_scale / burn_scale — the
                 graduated pool's LP rights go to platform NFT, creator
                 NFT, or burn (MigrateType::CPSWAP)

Those scales answer the cross-venue LP question directly: if the
migrated liquidity is claimed by platform+creator NFTs or burned, the
follower is locked out of the letsbonk pool the same way PumpSwap locks
them out (0 bps). If liquidity stays public, the route is open by
construction and the next question is economic (fee income vs the
measured -91% dump IL).

Layouts are byte-packed borsh per idl/raydium_launchlab_idl.json:
  GlobalConfig:  epoch u64, curve_type u8, index u16, migrate_fee u64,
                 trade_fee_rate u64, max_share_fee_rate u64 ...
  PlatformConfig: epoch u64, platform_fee_wallet pk, platform_nft_wallet pk,
                 platform_scale u64, creator_scale u64, burn_scale u64,
                 fee_rate u64, name [u8;64], web [u8;256], img [u8;256],
                 cpswap_config pk, creator_fee_rate u64,
                 transfer_fee_extension_auth pk  (744 bytes)

Nothing here moves funds or authorizes a transaction.
"""

import argparse
import asyncio
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from idl_parser import IDLParser  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from core.client import SolanaClient  # noqa: E402
from platforms.letsbonk.address_provider import LetsBonkAddresses  # noqa: E402

_IDL_PARSER = IDLParser(str(REPO / "idl/raydium_launchlab_idl.json"))


def _u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _pk(buf: bytes, off: int) -> str:
    return str(Pubkey.from_bytes(buf[off : off + 32]))


def _str(buf: bytes, off: int, size: int) -> str:
    raw = buf[off : off + size].rstrip(b"\x00")
    return raw.decode("utf-8", "replace")


def decode_global(buf: bytes) -> dict:
    # On-chain accounts carry an 8-byte anchor discriminator; every IDL
    # offset shifts by 8. Verified against the live hexdump.
    return {
        "epoch": _u64(buf, 8),
        "curve_type": buf[16],
        "index": struct.unpack_from("<H", buf, 17)[0],
        "migrate_fee_lamports": _u64(buf, 19),
        "trade_fee_rate_1e6": _u64(buf, 27),
        "max_share_fee_rate_1e6": _u64(buf, 35),
    }


def decode_platform(buf: bytes) -> dict:
    if len(buf) < 760:
        raise SystemExit(
            f"PlatformConfig is {len(buf)} bytes, IDL layout + 8-byte "
            "anchor needs 760 — re-derive offsets before trusting this read"
        )
    # Anchor discriminator occupies 0-8; IDL offsets shift by 8.
    return {
        "epoch": _u64(buf, 8),
        "platform_fee_wallet": _pk(buf, 16),
        "platform_nft_wallet": _pk(buf, 48),
        "platform_scale": _u64(buf, 80),
        "creator_scale": _u64(buf, 88),
        "burn_scale": _u64(buf, 96),
        "fee_rate_1e6": _u64(buf, 104),
        "name": _str(buf, 112, 64),
        "creator_fee_rate_1e6": _u64(buf, 720),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", default=None, help="RPC endpoint override")
    args = parser.parse_args()
    rpc = args.rpc
    if rpc is None:
        for line in (REPO / ".state/configs/capital-research-rpc.env").open():
            if line.startswith("SOLANA_NODE_RPC_ENDPOINT"):
                rpc = line.split("=", 1)[1].strip().strip("\"'")
    if not rpc:
        raise SystemExit("no RPC endpoint: pass --rpc or set the projection")

    client = SolanaClient(rpc)
    try:
        gc_v, pc_v = await client.get_multiple_accounts(
            [LetsBonkAddresses.GLOBAL_CONFIG, LetsBonkAddresses.PLATFORM_CONFIG],
            commitment="processed",
        )
        g = decode_global(bytes(gc_v.data))
        p = decode_platform(bytes(pc_v.data))
        trade_bps = g["trade_fee_rate_1e6"] / 100
        print(
            f"curve trade fee: {trade_bps:.2f} bps "
            f"({g['trade_fee_rate_1e6'] / 1e4:.2f}%)"
        )
        print(
            f"discovery PlatformConfig ({p['name'] or 'unnamed'}): "
            f"fee {p['fee_rate_1e6'] / 1e4:.2f}% "
            f"migration split platform "
            f"{p['platform_scale'] / 1e6:.1%} / creator "
            f"{p['creator_scale'] / 1e6:.1%} / burn "
            f"{p['burn_scale'] / 1e6:.1%}"
        )

        # The per-platform config is NOT global: each LaunchLab platform
        # (letsbonk.fun, Spots.fun, ...) deploys its own. Find one live
        # pool from recent program activity and read ITS platform config.
        raw = await client.get_client()
        sigs = await raw.get_signatures_for_address(LetsBonkAddresses.PROGRAM, limit=5)
        platform_cfg = None
        for sig in sigs.value:
            tx = None
            for version in (1, 0):
                try:
                    tx = await raw.get_transaction(
                        sig.signature, max_supported_transaction_version=version
                    )
                    break
                except Exception:  # noqa: BLE001 - provider wants v1, core wants v0
                    continue
            if tx is None:
                continue
            inner = tx.value.transaction
            if inner.meta is None:
                continue
            keys = [str(k) for k in inner.transaction.message.account_keys]
            accounts = await client.get_multiple_accounts(
                [Pubkey.from_string(k) for k in keys], commitment="processed"
            )
            pool = next(
                (a for a in accounts if a is not None and len(a.data) == 429),
                None,
            )
            if pool is None:
                continue
            b = bytes(pool.data)
            decoded = _IDL_PARSER.decode_account_data(
                b, "PoolState", skip_discriminator=True
            )
            platform_cfg = str(decoded["platform_config"])
            print(f"live pool {keys[0][:12]}… platform_config {platform_cfg}")
            break
        if platform_cfg is None:
            print("no recent LaunchLab pool found; per-platform read skipped")
            return
        pc_v = await client.get_account_info(Pubkey.from_string(platform_cfg))
        if pc_v is None:
            raise SystemExit("pool's platform_config missing on chain")
        p = decode_platform(bytes(pc_v.data))
        print(
            f"{p['name'] or 'unnamed'} PlatformConfig: "
            f"fee {p['fee_rate_1e6'] / 1e4:.2f}% "
            f"creator {p['creator_fee_rate_1e6'] / 1e4:.2f}% "
            f"migration split platform "
            f"{p['platform_scale'] / 1e6:.1%} / creator "
            f"{p['creator_scale'] / 1e6:.1%} / burn "
            f"{p['burn_scale'] / 1e6:.1%}"
        )

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
