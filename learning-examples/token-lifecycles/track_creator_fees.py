# ruff: noqa: TRY003, PLR2004, PTH123 - censoring paths are
# the point; thresholds are layout constants; one linear census.
"""Read-only creator-vault WSOL observations for the existing research cohort.

Rows retain pool/mint membership, including missing or invalid accounts, but
the balance belongs to the full creator-vault identity, potentially shared by
many pools. A balance change is NOT attributed fees, payouts, earnings, or a
daily rate: collections and other transfers are not identified here.

Schema v2 appends to .state/learning/creator_fee_snapshots.jsonl without
rewriting history. Legacy rows have truncated identities and possible synthetic
zeros, so cannot establish a v2 baseline. Missing/unavailable/invalid accounts
are explicitly censored with null amounts, never converted into zero.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import summarize_creator_fees

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from solders.pubkey import Pubkey  # noqa: E402
from spl.token.instructions import (  # noqa: E402
    get_associated_token_address,
)

from core.client import RpcUnavailableError, SolanaClient  # noqa: E402
from core.pubkeys import SystemAddresses  # noqa: E402
from platforms.pumpfun.pumpswap import (  # noqa: E402
    WSOL_MINT,
    PumpSwapAddresses,
    _decode_pool_account,
    _decode_token_account_amount,
)

STATE = REPO / ".state/learning/creator_fee_snapshots.jsonl"


def rpc_from_env() -> str:
    for line in (REPO / ".state/configs/capital-research-rpc.env").open():
        if line.startswith("SOLANA_NODE_RPC_ENDPOINT"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no RPC endpoint in the service-only projection")


def previous_balances() -> dict[str, dict]:
    """Retain the immediately previous row, including censors, for each pool."""
    if not STATE.exists():
        return {}
    last = {}
    with STATE.open() as source:
        for number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(
                    f"Malformed snapshot history at line {number}"
                ) from None
            if not isinstance(row, dict) or not isinstance(row.get("pool"), str):
                raise TypeError(f"Invalid snapshot history row at line {number}")
            last[row["pool"]] = row
    return last


def cohort_pools() -> list[dict]:
    """Keep the existing tape whale-band plus instrument grad_marks cohort."""
    mints: set[str] = set()
    for line in open(REPO / "learning-examples/token-lifecycles/lifecycles_24h.jsonl"):
        d = json.loads(line)
        trades = d.get("trades") or []
        if not trades:
            continue
        # whale self-sweep: final real_sol in the whale band, same creator
        rsol = trades[-1][5] / 1e9
        if 60 <= rsol <= 1000:
            mints.add(d["mint"])
    db_path = REPO / ".state/learning/lessons.sqlite3"
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        grad_mints = [
            r[0]
            for r in db.execute(
                "SELECT l.mint FROM lessons l JOIN grad_marks g ON"
                " g.entry_id=l.id WHERE g.open_price IS NOT NULL"
                " GROUP BY l.mint ORDER BY MAX(l.id) DESC"
            )
        ]
    finally:
        db.close()
    # Preserve existing priority: instrument grad_marks, then the tape cohort.
    # A historical zero cannot establish whether a creator collected anything.
    grad_set = set(grad_mints)
    recent = grad_mints + [m for m in sorted(mints, reverse=True) if m not in grad_set]
    return [{"mint": m} for m in recent]


def observe_vault(
    account: object, ata: Pubkey, vault: Pubkey
) -> tuple[str, int | None]:
    """Validate WSOL mint, token program, authority, size and initialization."""
    if account is None:
        return "vault_missing", None
    try:
        amount = _decode_token_account_amount(
            account,
            ata,
            token_program=SystemAddresses.TOKEN_PROGRAM,
            mint=WSOL_MINT,
            authority=vault,
        )
    except ValueError:
        return "vault_invalid", None
    return "observed", amount


def make_snapshot(  # noqa: PLR0913 - explicit immutable observation identity
    mint: str,
    pool: Pubkey,
    ts: str,
    observation_status: str,
    *,
    coin_creator: Pubkey | None = None,
    amount: int | None = None,
    previous: dict | None = None,
) -> dict:
    vault = (
        PumpSwapAddresses.derive_creator_vault(coin_creator)
        if coin_creator is not None
        else None
    )
    ata = get_associated_token_address(vault, WSOL_MINT) if vault is not None else None
    if observation_status != "observed":
        amount = None
    elif (
        coin_creator is None
        or type(amount) is not int
        or not 0 <= amount <= 0xFFFF_FFFF_FFFF_FFFF
    ):
        raise ValueError(
            "Observed creator-vault balance requires an identity and raw u64"
        )
    row = {
        "schema_version": 2,
        "ts": ts,
        "pool": str(pool),
        "mint": mint,
        "observation_status": observation_status,
        "balance_scope": "creator_vault",
        "coin_creator": str(coin_creator) if coin_creator is not None else None,
        "creator_vault": str(vault) if vault is not None else None,
        "creator_vault_ata": str(ata) if ata is not None else None,
        "vault_wsol_raw": amount,
        "vault_wsol": amount / 1e9 if amount is not None else None,
    }
    return {**row, **summarize_creator_fees.pair_baseline(row, previous)}


async def read_accounts(
    client: SolanaClient, keys: list[Pubkey]
) -> list[tuple[str, object]]:
    """One client call per batch; only transport exhaustion is unavailable."""
    results = []
    for start in range(0, len(keys), 100):
        chunk = keys[start : start + 100]
        try:
            accounts = await client.get_multiple_accounts(chunk, commitment="confirmed")
        except RpcUnavailableError:
            # Only exhausted transport reads are unavailable; permanent errors
            # and implementation defects must fail the snapshot.
            results.extend([("unavailable", None)] * len(chunk))
            continue
        if not isinstance(accounts, list) or len(accounts) != len(chunk):
            raise ValueError("Malformed multi-account response")
        results.extend(
            ("missing" if account is None else "available", account)
            for account in accounts
        )
    return results


async def collect_snapshots(
    client: SolanaClient, pools: list[dict], previous: dict[str, dict], ts: str
) -> list[dict]:
    derived = [
        (
            p["mint"],
            Pubkey.from_string(p["mint"]),
            PumpSwapAddresses.derive_canonical_pool(
                Pubkey.from_string(p["mint"]), WSOL_MINT
            ),
        )
        for p in pools
    ]
    accounts = await read_accounts(client, [pool for _, _, pool in derived])
    entries = []
    vaults = {}
    for (mint_str, mint, pool), (read_status, account) in zip(
        derived, accounts, strict=True
    ):
        creator = None
        if read_status == "available":
            try:
                creator = _decode_pool_account(
                    account, pool, mint, WSOL_MINT
                ).coin_creator
                status = "observed"
            except ValueError:
                status = "pool_invalid"
        else:
            status = f"pool_{read_status}"
        if creator is not None:
            vault = PumpSwapAddresses.derive_creator_vault(creator)
            vaults[get_associated_token_address(vault, WSOL_MINT)] = vault
        entries.append((mint_str, pool, creator, status))
    # A creator can own many cohort pools: read its ATA only once.
    vault_accounts = await read_accounts(client, list(vaults))
    observations = {
        ata: (
            ("vault_unavailable", None)
            if status == "unavailable"
            else observe_vault(account, ata, vault)
        )
        for (ata, vault), (status, account) in zip(
            vaults.items(), vault_accounts, strict=True
        )
    }
    rows = []
    for mint, pool, creator, pool_status in entries:
        status = pool_status
        amount = None
        if creator is not None:
            ata = get_associated_token_address(
                PumpSwapAddresses.derive_creator_vault(creator), WSOL_MINT
            )
            status, amount = observations[ata]
        rows.append(
            make_snapshot(
                mint,
                pool,
                ts,
                status,
                coin_creator=creator,
                amount=amount,
                previous=previous.get(str(pool)),
            )
        )
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=None)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    rpc = args.rpc or rpc_from_env()
    pools = cohort_pools()[: args.limit]
    previous = previous_balances()
    client = SolanaClient(rpc)
    try:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = await collect_snapshots(client, pools, previous, now)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with STATE.open("a") as target:
            for row in rows:
                target.write(json.dumps(row, sort_keys=True) + "\n")
        observed = [row for row in rows if row["observation_status"] == "observed"]
        vaults = {row["creator_vault_ata"] for row in observed}
        paired = {
            row["creator_vault_ata"]
            for row in observed
            if row["baseline_status"] == "paired"
        }
        print(
            f"snapshot: {len(rows)} cohort pools; {len(observed)} observed, "
            f"{len(rows) - len(observed)} censored; {len(vaults)} unique creator "
            f"vaults observed ({len(paired)} with an identity-matched baseline)"
        )
        print(
            "Creator-vault balance changes only: shared across pools; "
            "not attributed fees, payouts, earnings, or daily rates."
        )
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - fatal CLI boundary redacts service details
        # Fail visibly without a traceback that can echo service credentials.
        raise SystemExit(f"snapshot failed: {type(error).__name__}") from None
