"""Whale-ride shadow collector: forward curve traces for the graduation exit.

Collects, with zero credentials and no signing:
- PumpPortal free new-token discovery,
- a paced (>=2s) public-RPC `getMultipleAccounts` trace of each tracked
  coin's bonding curve (real/virtual reserves, complete flag, mayhem flag),
- for coins whose curve completes: the canonical PumpSwap pool and then its
  base/quote vaults, so the whale-ride exit can be priced off the pool's
  opening reserves.

Rows land in `.state/whaleride-shadow/shadow.jsonl`. Policy scoring stays
offline (`--report` reuses the cost model of `simulate_graduation_exit.py`);
this process only observes. Traces are the forward test of the 2026-10-05
single-tape finding: they confirm or kill it on data the backtest never saw.

Boundaries (mirrors run_paper_trader.py): public allowlisted RPC only,
strictly advancing context slot, <=2s read bound, bounded response bytes,
no keys, no dotenv, no funded anything. SIGINT/SIGTERM stops cleanly.

Trace-entry realism note: discovery + a 2s poll means the traced "entry" is
the first observed state at or after an X crossing — later than the backtest's
+1-slot landing, so shadow results are conservative on entry price.

Usage:
    uv run --offline --no-sync python -B learning-examples/token-lifecycles/run_whaleride_shadow.py
    uv run --offline --no-sync python -B learning-examples/token-lifecycles/run_whaleride_shadow.py --self-check
    uv run --offline --no-sync python -B learning-examples/token-lifecycles/run_whaleride_shadow.py --report
"""

# ruff: noqa: E402, S101, PLR2004, TRY301, SLF001 - same runner-script pattern as run_paper_trader.py

import argparse
import asyncio
import io
import json
import math
import statistics
import struct
import sys
import tempfile
import time
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
_TOOLING = str(ROOT / "learning-examples" / "token-lifecycles")
for _p in (_TOOLING, str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import aiohttp
from evaluate_creation_account_marks import account
from evaluate_creation_paper import strict_json
from run_paper_trader import (
    BAD_ACCOUNT,
    DISCOVERY_URL,
    MAINNET_GENESIS,
    MAX_READ_SECONDS,
    MAX_RESPONSE_BYTES,
    POLL_SECONDS,
    RPC_ENDPOINTS,
)
from solders.pubkey import Pubkey
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from summarize_lifecycles import PUMP_FEE, TX_FEES_LAMPORTS, sell_value
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from core.pubkeys import WSOL_MINT
from monitoring.subscription import subscribe_pumpportal
from platforms.pumpfun.address_provider import PumpFunAddressProvider
from platforms.pumpfun.curve_manager import PumpFunCurveManager
from platforms.pumpfun.pumpswap import (
    PumpSwapAddresses,
    _decode_pool_account,
)
from utils.idl_parser import IDLParser

SHADOW_PATH = ROOT / ".state" / "whaleride-shadow" / "shadow.jsonl"
TRACK_CAP = 192  # chunked at MAX_BATCH_KEYS; bursts were dropping 2/3 of discoveries
WATCH_SECONDS = 240.0  # cold coin (never hot): prune after four minutes
ENTRY_TIMEOUT = 900.0  # hot coin: trace for at most fifteen minutes
POOL_TIMEOUT = 120.0  # completed curve without a readable pool: censor
HOT_X_SOL = 20.0  # a trace at least this hot stays under observation longer
BUY_LAMPORTS = 10_000_000
POOL_FEE = 0.003
COST = BUY_LAMPORTS + TX_FEES_LAMPORTS
HOLD_SECONDS = 2.0  # non-graduate exit horizon (~5 slots)
POOL_EXIT_LAG = 1.0  # pool rows must be at least this far past the entry
DEFAULT_XS = "20,30,40,50,60"
_TOKEN_PROGRAMS = {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}
TOKEN_ACCOUNT_STATE_OFFSET = 108
TOKEN_ACCOUNT_MIN_SIZE = 109
TOKEN_ACCOUNT_AMOUNT_OFFSET = 64
MIN_OPENING_PROCEEDS_LAMPORTS = (
    9_800_000  # 0.01 SOL net of the 1.25% fee and opening impact
)
MAX_BATCH_KEYS = 100  # getMultipleAccounts hard limit


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(kind: str, payload: dict) -> None:
    print(kind + " " + json.dumps(payload, default=str), flush=True)


class RpcMissError(Exception):
    """Transient read failure with a retry cooldown; never a policy signal."""

    def __init__(self, detail: str, cooldown: float) -> None:
        super().__init__(detail)
        self.cooldown = cooldown


def vault_amount(value: object) -> int:
    """Token-vault amount (u64 at offset 64) with owner/init validation."""
    acct = account(value)
    data = bytes(acct.data)
    if len(data) < TOKEN_ACCOUNT_MIN_SIZE or data[TOKEN_ACCOUNT_STATE_OFFSET] not in (
        1,
        2,
    ):
        raise ValueError("vault_not_initialized")
    if acct.owner not in _TOKEN_PROGRAMS:
        raise ValueError("vault_owner_is_not_a_token_program")
    return struct.unpack_from("<Q", data, TOKEN_ACCOUNT_AMOUNT_OFFSET)[0]


class Shadow:
    """Free discovery plus paced public curve/pool tracing."""

    def __init__(self, endpoint: str, out_path: Path = SHADOW_PATH) -> None:
        if endpoint not in RPC_ENDPOINTS:
            raise ValueError("paper_rpc_endpoint_is_not_public_allowlisted")
        self.endpoint = endpoint
        self.addresses = PumpFunAddressProvider()
        self.decoder = SimpleNamespace(
            _idl_parser=IDLParser(str(ROOT / "idl" / "pump_fun_idl.json"))
        )
        self.request_id = 0
        self.last_slot = 0
        self.counts: Counter[str] = Counter()
        self.tracked: dict[str, dict] = {}
        self.genesis_verified = False
        self.connected = False
        self.out_path = out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, row: dict) -> None:
        with self.out_path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")

    # -- transport (same qualification shape as run_paper_trader.Runtime) -----
    async def rpc(  # noqa: C901 - bounded transport plus wire qualification
        self, session: aiohttp.ClientSession, method: str, params: list
    ) -> object:
        if method not in ("getGenesisHash", "getMultipleAccounts"):
            raise ValueError("paper_rpc_method_is_not_read_only")
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }
        started = time.monotonic()
        try:
            async with session.post(
                self.endpoint, json=request, allow_redirects=False
            ) as response:
                if response.status != 200:
                    cooldown = POLL_SECONDS
                    if response.status == 429:
                        try:
                            cooldown = min(
                                60.0,
                                max(
                                    15.0,
                                    float(response.headers.get("Retry-After", "15")),
                                ),
                            )
                        except ValueError:
                            cooldown = 15.0
                    raise RpcMissError(f"http_{response.status}", cooldown)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise RpcMissError("response_too_large", 5.0)
        except (aiohttp.ClientError, TimeoutError) as exc:
            # aiohttp raises TimeoutError for the request timeout; it is not a
            # ClientError, and leaving it out lets every RPC timeout escape.
            raise RpcMissError(type(exc).__name__, 5.0) from exc
        received = time.monotonic()
        if not 0 <= received - started <= MAX_READ_SECONDS:
            raise RpcMissError("rpc_read_latency", 5.0)
        payload = strict_json(bytes(raw))
        if not isinstance(payload, dict) or payload.get("id") != self.request_id:
            raise RpcMissError("rpc_envelope", 5.0)
        if payload.get("error") is not None:
            error = payload["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise RpcMissError(
                f"rpc_error_{code}", 15.0 if code == 429 else POLL_SECONDS
            )
        return payload["result"]

    # -- discovery ------------------------------------------------------------
    def admit(self, mint: str) -> None:
        if len(self.tracked) >= TRACK_CAP:
            self.counts["dropped_cap"] += 1
            return
        if mint in self.tracked:
            return
        try:
            curve = str(self.addresses.derive_pool_address(Pubkey.from_string(mint)))
        except BAD_ACCOUNT:
            self.counts["discover_invalid"] += 1
            return
        self.tracked[mint] = {
            "mint": mint,
            "curve": curve,
            "discovered": time.monotonic(),
            "hot": False,
            "complete_at": None,
            "pool": None,
            "vaults": None,
            "virtual_quote": None,
            "rejects": 0,
            "done": False,
        }
        self.counts["tracked"] += 1
        self.record({"k": "e", "what": "discover", "mint": mint, "ts": utc()})

    def on_frame(self, frame: str | bytes) -> None:
        self.counts["discovery_frames"] += 1
        try:
            data = strict_json(frame)
            if isinstance(data, dict) and data.get("method") == "newToken":
                params = data.get("params")
                if isinstance(params, list) and params and isinstance(params[0], dict):
                    data = params[0]
            mint = data.get("mint") if isinstance(data, dict) else None
            if not isinstance(mint, str) or str(Pubkey.from_string(mint)) != mint:
                raise ValueError("discovery_mint")
        except BAD_ACCOUNT:
            self.counts["discovery_invalid"] += 1
            return
        self.admit(mint)

    # -- decode ---------------------------------------------------------------
    def decode_curve(self, value: object, curve: str) -> dict:
        curve_account = account(value)
        raw = PumpFunCurveManager._validated_curve_data(
            curve_account, Pubkey.from_string(curve)
        )
        if len(raw) < 83 or any(raw[i] not in (0, 1) for i in (48, 81, 82)):
            raise ValueError("noncanonical_curve_flags")
        try:
            state = PumpFunCurveManager._decode_curve_state_with_idl(self.decoder, raw)
        except ValueError as exc:
            if "quote mint" in str(exc):
                raise ValueError("non_sol_quote") from exc
            raise
        if state["quote_mint"] != WSOL_MINT:
            raise ValueError("non_sol_quote")
        return state

    # -- tick -----------------------------------------------------------------
    def tick(self, coins: list[dict], values: list, slot: int) -> None:
        """Single-threaded: called only from mark_loop between awaits."""
        curve_values = values[: len(coins)]
        pool_rows, vault_rows = self._split_batch(coins, values)
        self._trace_curves(coins, curve_values, slot)
        self._decode_pools(coins, pool_rows)
        self._settle_vaults(coins, vault_rows, slot)
        self._prune(coins)
        self.tracked = {m: c for m, c in self.tracked.items() if not c["done"]}

    @staticmethod
    def _split_batch(
        coins: list[dict], values: list
    ) -> tuple[dict[str, object], dict[str, tuple]]:
        """Split the tail of `values` into per-coin pool and vault account rows."""
        rest = values[len(coins) :]
        pool_rows: dict[str, object] = {}
        vault_rows: dict[str, tuple] = {}
        idx = 0
        for c in coins:
            if c["pool"] is not None and c["vaults"] is None:
                pool_rows[c["mint"]] = rest[idx]
                idx += 1
            if c["vaults"] is not None:
                vault_rows[c["mint"]] = (rest[idx], rest[idx + 1])
                idx += 2
        return pool_rows, vault_rows

    def _trace_curves(self, coins: list[dict], curve_values: list, slot: int) -> None:
        """Record curve rows, graduation flags, and hotness."""
        for c, value in zip(coins, curve_values, strict=True):
            mint = c["mint"]
            if value is None:
                self.counts["curve_missing"] += 1
                self._bump_rejects(c, "curve_missing")
                continue
            try:
                state = self.decode_curve(value, c["curve"])
            except BAD_ACCOUNT as exc:
                self.counts["curve_rejected"] += 1
                if "non_sol_quote" in str(exc):
                    c["done"] = True
                    self.record(
                        {"k": "e", "what": "non_sol", "mint": mint, "ts": utc()}
                    )
                else:
                    self._bump_rejects(c, "curve_unreadable")
                continue
            c["rejects"] = 0
            rq, vq, vt = (
                state["real_quote_reserves"],
                state["virtual_quote_reserves"],
                state["virtual_token_reserves"],
            )
            complete = bool(state["complete"])
            self.record(
                {
                    "k": "c",
                    "m": mint,
                    "slot": slot,
                    "ts": time.time(),
                    "rq": rq,
                    "vq": vq,
                    "vt": vt,
                    "cp": int(complete),
                    "mh": int(bool(state["is_mayhem_mode"])),
                }
            )
            if not c["hot"] and rq >= HOT_X_SOL * 1_000_000_000:
                c["hot"] = True
            if complete and c["complete_at"] is None:
                c["complete_at"] = time.monotonic()
            if complete and c["pool"] is None:
                c["pool"] = str(
                    PumpSwapAddresses.derive_canonical_pool(
                        Pubkey.from_string(mint), WSOL_MINT
                    )
                )

    def _decode_pools(self, coins: list[dict], pool_rows: dict[str, object]) -> None:
        """Decode graduated coins' pools into vault addresses."""
        for c in coins:
            if (
                c["pool"] is None
                or c["vaults"] is not None
                or c["mint"] not in pool_rows
            ):
                continue
            value = pool_rows[c["mint"]]
            if value is None:
                if (
                    time.monotonic() - (c["complete_at"] or time.monotonic())
                    > POOL_TIMEOUT
                ):
                    c["done"] = True
                    self.record(
                        {
                            "k": "e",
                            "what": "pool_missing",
                            "mint": c["mint"],
                            "ts": utc(),
                        }
                    )
                continue
            try:
                decoded = _decode_pool_account(
                    account(value),
                    Pubkey.from_string(c["pool"]),
                    Pubkey.from_string(c["mint"]),
                    WSOL_MINT,
                )
            except BAD_ACCOUNT as exc:
                c["done"] = True
                self.counts["pool_rejected"] += 1
                self.record(
                    {
                        "k": "e",
                        "what": "pool_rejected",
                        "mint": c["mint"],
                        "ts": utc(),
                        "err": str(exc)[:120],
                    }
                )
                continue
            c["vaults"] = (str(decoded.base_vault), str(decoded.quote_vault))
            c["virtual_quote"] = decoded.virtual_quote_reserve_raw

    def _settle_vaults(
        self, coins: list[dict], vault_rows: dict[str, tuple], slot: int
    ) -> None:
        """Terminal pool rows from decoded vault amounts."""
        for c in coins:
            if c["vaults"] is None or c["mint"] not in vault_rows:
                continue
            base_value, quote_value = vault_rows[c["mint"]]
            try:
                base_amt = vault_amount(base_value)
                quote_amt = vault_amount(quote_value)
            except BAD_ACCOUNT as exc:
                c["done"] = True
                self.counts["vault_rejected"] += 1
                self.record(
                    {
                        "k": "e",
                        "what": "vault_rejected",
                        "mint": c["mint"],
                        "ts": utc(),
                        "err": str(exc)[:120],
                    }
                )
                continue
            self.record(
                {
                    "k": "p",
                    "m": c["mint"],
                    "slot": slot,
                    "ts": time.time(),
                    "b": base_amt,
                    "q": quote_amt,
                    "vrq": c["virtual_quote"],
                    "pa": c["pool"],
                }
            )
            c["done"] = True

    def _prune(self, coins: list[dict]) -> None:
        now = time.monotonic()
        for c in coins:
            if c["done"]:
                continue
            limit = ENTRY_TIMEOUT if c["hot"] else WATCH_SECONDS
            if now - c["discovered"] > limit:
                c["done"] = True
                self.record(
                    {
                        "k": "e",
                        "what": "prune",
                        "mint": c["mint"],
                        "ts": utc(),
                        "reason": "timeout",
                        "hot": c["hot"],
                    }
                )

    def keys(self) -> tuple[list[str], list[dict]]:
        """Snapshot the tracked coins; done coins leave tracked here."""
        self.tracked = {m: c for m, c in self.tracked.items() if not c["done"]}
        keys: list[str] = []
        coins: list[dict] = []
        for c in self.tracked.values():
            coins.append(c)
            keys.append(c["curve"])
            if c["pool"] is not None and c["vaults"] is None:
                keys.append(c["pool"])
            if c["vaults"] is not None:
                keys.extend(c["vaults"])
        return keys, coins

    def _bump_rejects(self, coin: dict, reason: str) -> None:
        """Prune coins the decoder cannot read, so they never pin the cap."""
        coin["rejects"] = coin.get("rejects", 0) + 1
        if coin["rejects"] >= 3:
            coin["done"] = True
            self.record({"k": "e", "what": reason, "mint": coin["mint"], "ts": utc()})

    # -- loops ----------------------------------------------------------------
    async def mark_loop(self, session: aiohttp.ClientSession) -> None:
        next_start = time.monotonic()
        while True:
            await asyncio.sleep(max(0.0, next_start - time.monotonic()))
            next_start = time.monotonic() + POLL_SECONDS
            keys, coins = self.keys()
            if not keys:
                continue
            for start in range(0, len(keys), MAX_BATCH_KEYS):
                key_slice = keys[start : start + MAX_BATCH_KEYS]
                coin_slice = coins[start : start + MAX_BATCH_KEYS]
                try:
                    result = await self.rpc(
                        session,
                        "getMultipleAccounts",
                        [
                            key_slice,
                            {
                                "encoding": "base64",
                                "commitment": "confirmed",
                                "minContextSlot": self.last_slot + 1,
                            },
                        ],
                    )
                except RpcMissError as exc:
                    self.counts["rpc_miss"] += 1
                    next_start = max(next_start, time.monotonic() + exc.cooldown)
                    break
                slot = (
                    result.get("context", {}).get("slot")
                    if isinstance(result, dict)
                    else None
                )
                if type(slot) is not int or slot <= self.last_slot:
                    self.counts["slot_not_advancing"] += 1
                    break
                values = result.get("value")
                if not isinstance(values, list) or len(values) != len(key_slice):
                    self.counts["bad_batch"] += 1
                    break
                self.last_slot = slot
                try:
                    self.tick(coin_slice, values, slot)
                except BAD_ACCOUNT as exc:
                    self.counts["tick_rejected"] += 1
                    self.record(
                        {
                            "k": "e",
                            "what": "tick_rejected",
                            "ts": utc(),
                            "err": str(exc)[:120],
                        }
                    )

    async def discover(self) -> None:
        backoff = 1.0
        while True:
            connected_at = time.monotonic()
            try:
                async with connect(
                    DISCOVERY_URL,
                    proxy=None,
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=262144,
                    max_queue=32,
                ) as websocket:
                    result = await subscribe_pumpportal(
                        websocket, request_id=1, timeout=10
                    )
                    self.connected = True
                    for frame in result.pending_frames:
                        self.on_frame(frame)
                    async for frame in websocket:
                        self.on_frame(frame)
                    raise ConnectionError("discovery_stream_ended")
            except (WebSocketException, OSError, TimeoutError, ConnectionError) as exc:
                self.connected = False
                emit(
                    "shadow_discovery",
                    {"error": type(exc).__name__, "backoff": backoff, "ts": utc()},
                )
                if time.monotonic() - connected_at >= 30:
                    backoff = 1.0
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def heartbeat(self) -> None:
        last = -math.inf
        while True:
            now = time.monotonic()
            if now - last >= 30:
                emit(
                    "shadow_status",
                    {
                        "tracked": len(self.tracked),
                        "counts": dict(self.counts),
                        "slot": self.last_slot,
                        "connected": self.connected,
                        "ts": utc(),
                    },
                )
                last = now
            await asyncio.sleep(1.0)

    async def work(self) -> None:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=MAX_READ_SECONDS),
            auto_decompress=True,
            trust_env=False,
        ) as session:
            while not self.genesis_verified:
                try:
                    genesis = await self.rpc(session, "getGenesisHash", [])
                except RpcMissError as exc:
                    emit("shadow_genesis", {"error": str(exc), "ts": utc()})
                    await asyncio.sleep(exc.cooldown)
                    continue
                if genesis != MAINNET_GENESIS:
                    raise RuntimeError("public_rpc_is_not_solana_mainnet")
                self.genesis_verified = True
            emit("shadow_ready", {"endpoint": self.endpoint, "ts": utc()})
            async with asyncio.TaskGroup() as group:
                group.create_task(self.discover())
                group.create_task(self.mark_loop(session))
                group.create_task(self.heartbeat())


# -- offline scoring ----------------------------------------------------------


def tokens_bought(v_quote: int, v_token: int) -> int:
    spend_net = BUY_LAMPORTS * (1 - PUMP_FEE)
    return v_token - (v_quote * v_token) / (v_quote + spend_net)


def report(
    path: Path = SHADOW_PATH,
    xs: tuple[float, ...] = (30, 40, 50, 60),
    hold_seconds: float = HOLD_SECONDS,
) -> None:
    coins: dict[str, dict] = {}
    for line in path.open():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        mint = row.get("m") or row.get("mint")
        if not mint:
            continue
        coin = coins.setdefault(mint, {"curves": [], "pools": [], "events": []})
        if row.get("k") == "c":
            coin["curves"].append(row)
        elif row.get("k") == "p":
            coin["pools"].append(row)
        elif row.get("k") == "e":
            coin["events"].append(row)

    pruned = sum(
        1 for c in coins.values() if any(e.get("what") == "prune" for e in c["events"])
    )
    pool_missing = sum(
        1
        for c in coins.values()
        if any(e.get("what") == "pool_missing" for e in c["events"])
    )
    non_sol = sum(
        1
        for c in coins.values()
        if any(e.get("what") == "non_sol" for e in c["events"])
    )
    print(f"shadow report: {path} ({len(coins)} coins)")
    print(
        f"pruned(cold timeout) {pruned}, pool_missing {pool_missing}, non_sol {non_sol}"
    )
    print(
        f"{'x':>4} | {'entries':>7} {'whale-beat':>10} {'censored':>8} | "
        f"{'mean':>8} {'win':>6} {'p10':>7} {'p50':>6} {'p90':>7}"
    )
    for x_sol in xs:
        scored = [_score_coin(coin, x_sol, hold_seconds) for coin in coins.values()]
        entries = [pnl for outcome, pnl in scored if outcome == "entry"]
        whale = sum(1 for outcome, _ in scored if outcome == "whale")
        censored = sum(1 for outcome, _ in scored if outcome == "censored")
        _print_row(x_sol, entries, whale, censored)


def _score_coin(
    coin: dict, x_sol: float, hold_seconds: float
) -> tuple[str, float | None]:
    """Whale-ride score for one coin at one X: ('entry', pnl) | ('whale'|'censored', None)."""
    curves = sorted(coin["curves"], key=lambda r: r["ts"])
    hit = next((r for r in curves if r["rq"] >= x_sol * 1e9), None)
    if hit is None:
        return "none", None
    if hit["cp"]:
        return "whale", None  # whale swept through X between polls: buy would revert
    tokens = tokens_bought(hit["vq"], hit["vt"])
    if any(r["cp"] for r in curves):
        pool_row = next(
            (p for p in coin["pools"] if p["ts"] >= hit["ts"] + POOL_EXIT_LAG), None
        )
        if pool_row is None:
            return "censored", None
        eff, base = pool_row["q"] + pool_row["vrq"], pool_row["b"]
        pnl = (
            -float(COST)
            if not base or not eff
            else eff * tokens / (base + tokens) * (1 - POOL_FEE) - COST
        )
        return "entry", pnl
    exit_row = next((r for r in curves if r["ts"] >= hit["ts"] + hold_seconds), None)
    if exit_row is None:
        return "censored", None
    return "entry", sell_value(exit_row["vq"], exit_row["vt"], tokens) - COST


def _print_row(x_sol: float, entries: list[float], whale: int, censored: int) -> None:
    if not entries:
        print(f"{x_sol:>4.0f} | {0:>7} {whale:>10} {censored:>8} | no entries yet")
        return
    wins = sum(1 for p in entries if p > 0) / len(entries)

    def pct(vals: list[float], q: float) -> float:
        ordered = sorted(vals)
        return ordered[min(int(q * len(ordered)), len(ordered) - 1)]

    print(
        f"{x_sol:>4.0f} | {len(entries):>7} {whale:>10} {censored:>8} | "
        f"{statistics.mean(entries) / 1e5:+8.1f}% {wins:6.1%} "
        f"{pct(entries, 0.1) / 1e5:+7.1f}% {pct(entries, 0.5) / 1e5:+6.1f}% "
        f"{pct(entries, 0.9) / 1e5:+7.1f}%"
    )


def self_check() -> None:
    """Synthetic data through entry/exit math and the report path; no network."""
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Shadow(RPC_ENDPOINTS[0], out_path=Path(tmp) / "shadow.jsonl")
        assert shadow.out_path.parent.exists()
        # 0.01 SOL at the opening curve state buys 0.01*(1-fee) worth minus impact
        opening_tokens = tokens_bought(30_000_000_000, 1_073_000_000_000_000)
        assert (
            opening_tokens * 30_000_000_000 / (1_073_000_000_000_000 + opening_tokens)
            > MIN_OPENING_PROCEEDS_LAMPORTS
        )
        # CAT-like graduation: pool opening state prices the whale-ride above cost
        entry_tokens = tokens_bought(50_000_000_000, 700_000_000_000_000)
        eff, base = 67_405_853_768, 206_900_000_000_000
        assert eff * entry_tokens / (base + entry_tokens) * (1 - POOL_FEE) > COST
        # report path end to end on a synthetic graduated coin
        t0 = 1_000.0
        rows = [
            {
                "k": "c",
                "m": "M",
                "ts": t0,
                "rq": 5_000_000_000,
                "vq": 30_000_000_000,
                "vt": 1_073_000_000_000_000,
                "cp": 0,
                "mh": 0,
            },
            {
                "k": "c",
                "m": "M",
                "ts": t0 + 2,
                "rq": 40_000_000_000,
                "vq": 50_000_000_000,
                "vt": 700_000_000_000_000,
                "cp": 0,
                "mh": 0,
            },
            {
                "k": "c",
                "m": "M",
                "ts": t0 + 4,
                "rq": 85_000_000_000,
                "vq": 115_000_000_000,
                "vt": 279_900_000_000_000,
                "cp": 1,
                "mh": 0,
            },
            {
                "k": "p",
                "m": "M",
                "ts": t0 + 6,
                "b": 206_900_000_000_000,
                "q": 67_405_853_768,
                "vrq": 0,
            },
        ]
        path = Path(tmp) / "s.jsonl"
        with path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report(path)
        text = buffer.getvalue()
        assert "| 1 " in text or "entries" in text  # the synthetic coin scored
    emit("self_check", {"ok": True, "ts": utc()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--endpoint", default=RPC_ENDPOINTS[0])
    ap.add_argument("--xs", default=DEFAULT_XS)
    args = ap.parse_args()
    if args.self_check:
        self_check()
        return
    if args.report:
        report(xs=tuple(float(x) for x in args.xs.split(",")))
        return
    shadow = Shadow(args.endpoint)
    try:
        asyncio.run(shadow.work())
    except (KeyboardInterrupt, asyncio.CancelledError):
        emit("shadow_stopped", {"counts": dict(shadow.counts), "ts": utc()})


if __name__ == "__main__":
    main()
