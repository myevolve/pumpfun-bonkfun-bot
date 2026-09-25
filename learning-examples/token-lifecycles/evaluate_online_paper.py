"""Conditional Pump SOL paper quotes and forward-only exit-horizon learning.

No wallet, transactions, native fee attestation, or executable-fill assertion.
The runtime qualifies account provenance, latency and new-slot freshness before
passing marks. All economic amounts are integer lamports or raw base tokens.
"""

from __future__ import annotations

# Standalone raw-unit validation uses one domain-error boundary, not message-only exception classes.
# ruff: noqa: E402, PLR2004, TRY003, TRY004, TRY301
import copy
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from platforms.pumpfun.fee_schedule import (
    PumpFeeConfig,
    PumpFeeSnapshot,
    quote_buy_exact_in,
    quote_sell_exact_in,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

HOLDS = (10, 30, 60)
CONFIG = {
    "schema": 3,
    "holds_seconds": list(HOLDS),
    "entry_lamports": 10_000_000,
    "initial_cash_lamports": 1_000_000_000,
    "buy_network_lamports": 33_000,
    "buy_tip_lamports": 10_000,
    "unreturned_rent_lamports": 2_100_000,
    "sell_network_lamports": 27_000,
    "sell_tip_lamports": 10_000,
    "cleanup_lamports": 5_000,
    "adverse_haircut_bps_each_leg": 100,
    "discovery_expiry_seconds": 15,
    "exit_late_tolerance_seconds": 5,
    "max_exposures": 12,
    "max_tracked_cohorts": 12,
    "entry_policy": "shadow_warmup_then_positive_score_or_no_trade",
    "rolling_paired_cohorts": 60,
    "minimum_paired_cohorts": 5,
    "score": "mean_modeled_net_minus_2_sample_standard_errors_heuristic_not_CI",
    "fee_model": "decoded_fee_config_scenario_unattested",
    "fee_market_cap_supply": "observed_curve_supply_non_mayhem",
    "reserve_model": "entry_delta_overlay_assuming_observed_external_reserve_flows",
    "native_fee_attested": False,
    "creator_fee_override_verified": False,
}
ENTRY_DEBIT = sum(
    CONFIG[k]
    for k in (
        "entry_lamports",
        "buy_network_lamports",
        "buy_tip_lamports",
        "unreturned_rent_lamports",
    )
)
EXIT_COST = sum(
    CONFIG[k]
    for k in ("sell_network_lamports", "sell_tip_lamports", "cleanup_lamports")
)
ENTRY_RESERVATION = ENTRY_DEBIT + EXIT_COST
LIMITATIONS = [
    "Conditional paper quotes only: not real fills or profitability evidence.",
    "Decoded fee configuration is an unattested scenario; creator fee override is unverified.",
    "Runtime qualifies public RPC provenance/freshness; execution, inclusion and latency fills are unproven.",
    "One percent adverse haircut each leg; fixed network/tip/cleanup costs are assumptions.",
    "Rent is modeled unreturned although actual rent may refund; no cash replenishment.",
    "Only incomplete non-Mayhem SOL curves; no migration or unsupported-extension valuation.",
    "Paper entry reserve deltas persist across later marks; external traders' counterfactual responses are not modeled.",
    "Censored inventory remains unknown, never zero PnL; complete-pair selection can bias learning.",
    "Shadow-only cohorts use no portfolio cash; their modeled returns never replenish the portfolio.",
    "Mean minus two standard errors is a heuristic, not a confidence interval or permission to trade live.",
]


@dataclass(frozen=True)
class Mark:
    """A qualified public account snapshot; never a transaction or fill."""

    mint: str
    slot: int
    observed_at: float
    state: dict
    fees: PumpFeeSnapshot
    supply_raw: int
    proof: dict


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    """Do not silently stringify Pubkeys, bytes, nonfinite numbers or dict keys."""

    def check(item: object) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("durable values must be finite JSON primitives")

    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clock(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("clock must be finite nonnegative monotonic seconds")
    return value


def _uint(value: object, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= 2**64 - 1:
        raise ValueError(f"{field} must be a {'positive ' if positive else ''}u64")
    return value


def _mint(value: object) -> str:
    if type(value) is not str or str(Pubkey.from_string(value)) != value:
        raise ValueError("mint must be a canonical public key string")
    return value


def _fingerprint() -> dict:
    paths = [
        "learning-examples/token-lifecycles/evaluate_online_paper.py",
        "learning-examples/token-lifecycles/run_paper_trader.py",
        "learning-examples/token-lifecycles/evaluate_creation_paper.py",
        "learning-examples/token-lifecycles/evaluate_creation_account_marks.py",
        "src/platforms/pumpfun/fee_schedule.py",
        "src/platforms/pumpfun/curve_manager.py",
        "src/platforms/pumpfun/address_provider.py",
        "src/platforms/pumpfun/pumpswap.py",
        "src/core/pubkeys.py",
        "src/utils/idl_parser.py",
        "idl/pump_fun_idl.json",
        "pyproject.toml",
        "uv.lock",
    ]
    sources = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths
    }
    assumptions = {"config": CONFIG, "sources": sources}
    return {
        **assumptions,
        "sha256": hashlib.sha256(_json(assumptions).encode()).hexdigest(),
    }


def _safe_path(path: Path) -> Path:
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("paper database path must not traverse symlinks")
    if any(
        part.name.lower().startswith(".env") or part.name.upper() == "ENVDATA"
        for part in (path, *path.parents)
    ):
        raise ValueError("protected environment path is not a paper database")
    if path.suffix.lower() not in (".sqlite", ".sqlite3", ".db"):
        raise ValueError("paper database must have a .sqlite, .sqlite3 or .db suffix")
    if any(
        Path(str(path) + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("paper database sidecars must not be symlinks")
    return path


def _preflight(path: Path, fingerprint: dict) -> None:
    """Check nonempty existing files read-only, before any SQLite write pragma."""
    if not path.exists() or path.stat().st_size == 0:
        return
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if tables != {"paper_meta", "paper_state", "paper_events", "paper_seen"}:
            raise ValueError("refusing an unrelated or incompatible SQLite database")
        row = connection.execute(
            "SELECT fingerprint_json FROM paper_meta WHERE singleton=1"
        ).fetchone()
        if row is None or json.loads(row[0]) != fingerprint:
            raise ValueError(
                "incompatible paper source/config fingerprint; choose a new database"
            )
        row = connection.execute(
            "SELECT state_json FROM paper_state WHERE singleton=1"
        ).fetchone()
        if (
            row is None
            or json.loads(row[0]).get("fingerprint") != fingerprint["sha256"]
        ):
            raise ValueError("paper database has missing or incompatible state")
    finally:
        connection.close()


def read_status(path: Path) -> dict:
    """Read a committed status without creating a DB, migrating, or censoring it."""
    connection = sqlite3.connect(_safe_path(path).as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT status_json FROM paper_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ValueError("paper database has no committed state")
        return json.loads(row[0])
    finally:
        connection.close()


class PaperBook:
    """Single-threaded book. Runtime owns the exclusive writer file lock."""

    def __init__(self, path: Path) -> None:
        path = _safe_path(path)
        fingerprint = _fingerprint()
        _preflight(path, fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._closed = False
        self._failed = False
        self._events = []
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS paper_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), fingerprint_json TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS paper_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), state_json TEXT NOT NULL, status_json TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS paper_events (sequence INTEGER PRIMARY KEY, utc TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS paper_seen (mint TEXT PRIMARY KEY)"
            )
            stored = self._db.execute(
                "SELECT fingerprint_json FROM paper_meta WHERE singleton=1"
            ).fetchone()
            if stored is None:
                self._db.execute(
                    "INSERT INTO paper_meta VALUES (1, ?)", (_json(fingerprint),)
                )
                self._state = {
                    "fingerprint": fingerprint["sha256"],
                    "cash_lamports": CONFIG["initial_cash_lamports"],
                    "reserved_lamports": 0,
                    "known_paper_net_lamports": 0,
                    "active": {},
                    "unknown": {},
                    "rolling": [],
                    "next_id": 1,
                    "entries": 0,
                    "cohorts_started": 0,
                    "shadow_entries": 0,
                    "portfolio_skip_counts": {},
                    "closures": 0,
                    "accepted_marks": 0,
                    "rejected_marks": 0,
                    "discoveries": 0,
                    "expired_discoveries": 0,
                    "selected_closed": 0,
                    "paired_complete": 0,
                    "censored_cohorts": 0,
                    "settled_cohorts": 0,
                    "policy_revision": 0,
                    "best_hold_seconds": None,
                    "scores": {},
                    "arm_stats": {
                        str(h): {
                            "entered": 0,
                            "closed": 0,
                            "censored": 0,
                            "known_net_lamports": 0,
                        }
                        for h in HOLDS
                    },
                    "last_entry": None,
                    "last_cohort": None,
                    "last_closure": None,
                    "last_learning": None,
                    "last_mark": None,
                    "running": True,
                    "connected": False,
                    "runtime": {},
                    "observations": {},
                    "note_counts": {},
                }
                self._event(
                    "initialization",
                    {"fingerprint": fingerprint, "limitations": LIMITATIONS},
                )
            else:
                if json.loads(stored[0]) != fingerprint:
                    raise ValueError(
                        "incompatible paper source/config fingerprint; choose a new database, never reset history"
                    )
                row = self._db.execute(
                    "SELECT state_json FROM paper_state WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise ValueError("paper database is missing its durable state")
                self._state = json.loads(row[0])
                self._censor_all("process_restart_monotonic_clock_boundary")
                self._state.update(running=True, connected=False, runtime={})
                self._event(
                    "restart", {"unknown_inventory": len(self._state["unknown"])}
                )
            self._persist()
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._closed or self._failed:
            raise RuntimeError("paper book is closed or failed")
        before = copy.deepcopy(self._state)
        self._events = []
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            if self._events:
                self._persist()
            self._db.execute("COMMIT")
        except BaseException:
            self._state = before
            self._failed = True
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        finally:
            self._events = []

    def _event(self, kind: str, payload: dict) -> None:
        self._events.append((_utc(), kind, _json(payload)))

    def _persist(self) -> None:
        self._db.executemany(
            "INSERT INTO paper_events (utc, kind, payload_json) VALUES (?, ?, ?)",
            self._events,
        )
        self._db.execute(
            "INSERT INTO paper_state VALUES (1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json, status_json=excluded.status_json",
            (_json(self._state), _json(self.status())),
        )

    def _next_hold(self) -> int:
        return (
            self._state["best_hold_seconds"]
            or HOLDS[self._state["cohorts_started"] % len(HOLDS)]
        )

    def _entry_block(self) -> str | None:
        state = self._state
        hold = state["best_hold_seconds"]
        if hold is None:
            return "shadow_warmup"
        if state["scores"][str(hold)]["score_lamports"] <= 0:
            return "nonpositive_score"
        if state["cash_lamports"] < ENTRY_RESERVATION:
            return "insufficient_virtual_cash"
        selected_active = sum(
            item["phase"] == "entered"
            and item["portfolio_selected"]
            and item["arms"][str(item["hold_seconds"])]["status"] == "open"
            for item in state["active"].values()
        )
        if selected_active + len(state["unknown"]) >= CONFIG["max_exposures"]:
            return "exposure_capacity"
        return None

    def status(self) -> dict:
        state = self._state
        active = state["active"]
        entered = [item for item in active.values() if item["phase"] == "entered"]
        selected_active = sum(
            item["portfolio_selected"]
            and item["arms"][str(item["hold_seconds"])]["status"] == "open"
            for item in entered
        )
        entry_block = self._entry_block()
        result = {
            key: state[key]
            for key in (
                "cash_lamports",
                "reserved_lamports",
                "known_paper_net_lamports",
                "entries",
                "cohorts_started",
                "shadow_entries",
                "portfolio_skip_counts",
                "closures",
                "accepted_marks",
                "rejected_marks",
                "discoveries",
                "expired_discoveries",
                "selected_closed",
                "paired_complete",
                "censored_cohorts",
                "settled_cohorts",
                "policy_revision",
                "arm_stats",
                "last_entry",
                "last_cohort",
                "last_closure",
                "last_learning",
                "last_mark",
                "running",
                "connected",
                "runtime",
                "observations",
                "note_counts",
                "scores",
                "fingerprint",
            )
        }
        result.update(
            paper_only=True,
            native_fee_attested=False,
            creator_fee_override_verified=False,
            fee_model=CONFIG["fee_model"],
            policy_hold_seconds=self._next_hold(),
            policy_action="observe_only" if entry_block else "paper_buy",
            portfolio_entry_block=entry_block,
            policy_mode="conservative_score_with_no_trade"
            if state["best_hold_seconds"]
            else "shadow_warmup",
            selected_open=selected_active + len(state["unknown"]),
            selected_active=selected_active,
            unknown_inventory=len(state["unknown"]),
            unknown_positions=list(state["unknown"].values()),
            pending_discoveries=sum(
                item["phase"] == "pending" for item in active.values()
            ),
            tracked_cohorts=len(active),
            rolling_paired_count=len(state["rolling"]),
            cohort_denominator=state["cohorts_started"],
            limitations=LIMITATIONS,
            assumptions=CONFIG,
        )
        return copy.deepcopy(result)

    def tracked_mints(self) -> list[str]:
        return list(self._state["active"])

    def discover(self, mint: str, now: float) -> bool:
        _mint(mint)
        _clock(now)
        with self._transaction():
            self._expire(now)
            if self._db.execute(
                "SELECT 1 FROM paper_seen WHERE mint=?", (mint,)
            ).fetchone():
                return False
            if len(self._state["active"]) >= CONFIG["max_tracked_cohorts"]:
                self._event(
                    "discovery_rejected",
                    {"mint": mint, "reason": "observation_capacity"},
                )
                return False
            self._db.execute("INSERT INTO paper_seen VALUES (?)", (mint,))
            self._state["active"][mint] = {
                "mint": mint,
                "phase": "pending",
                "discovered_at": now,
                "last_slot": None,
                "last_observed_at": now,
            }
            self._state["discoveries"] += 1
            self._event("discovery", {"mint": mint, "observed_at": now})
            return True

    def note(self, kind: str, payload: dict) -> None:
        if type(kind) is not str or not kind or type(payload) is not dict:
            raise ValueError("note requires a nonempty kind and a JSON object")
        _json(payload)
        for key in ("running", "connected", "discovery_connected"):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        with self._transaction():
            state = self._state
            state["note_counts"][kind] = state["note_counts"].get(kind, 0) + 1
            state["observations"][kind] = copy.deepcopy(payload)
            if kind == "runtime":
                state["runtime"] = copy.deepcopy(payload)
                if "running" in payload:
                    state["running"] = payload["running"]
                if "discovery_connected" in payload:
                    state["connected"] = payload["discovery_connected"]
            if kind == "connection" and "connected" in payload:
                state["connected"] = payload["connected"]
            self._event(kind, payload)

    def _validated_mark(self, mark: Mark) -> tuple[dict, dict]:  # noqa: C901, PLR0912, PLR0915 - one explicit trust boundary
        _mint(mark.mint)
        _uint(mark.slot, "slot", positive=True)
        _clock(mark.observed_at)
        _uint(mark.supply_raw, "mint supply", positive=True)
        if type(mark.state) is not dict or type(mark.proof) is not dict:
            raise ValueError("mark state/proof must be objects")
        _json(mark.proof)
        if mark.proof.get("native_fee_attested") is not False:
            raise ValueError(
                "paper mark must explicitly disclaim native fee attestation"
            )
        if mark.proof.get("creator_fee_override_verified", False) is not False:
            raise ValueError("paper mark cannot claim verified creator fee overrides")
        if mark.proof.get("fee_model", CONFIG["fee_model"]) != CONFIG["fee_model"]:
            raise ValueError("mark uses an incompatible fee scenario")
        state = mark.state
        if (
            state.get("complete") is not False
            or state.get("is_mayhem_mode") is not False
        ):
            raise ValueError("complete, Mayhem or malformed curve is unsupported")
        if type(state.get("is_cashback_coin")) is not bool:
            raise ValueError("cashback flag must be canonical boolean")
        canonical = {
            key: state[key]
            for key in ("complete", "is_mayhem_mode", "is_cashback_coin")
        }
        for key in (
            "virtual_token_reserves",
            "virtual_quote_reserves",
            "real_token_reserves",
            "real_quote_reserves",
            "token_total_supply",
        ):
            canonical[key] = _uint(
                state.get(key),
                key,
                positive=key
                in (
                    "virtual_token_reserves",
                    "virtual_quote_reserves",
                    "token_total_supply",
                ),
            )
        for key in ("creator", "quote_mint"):
            value = state.get(key)
            if not isinstance(value, str | Pubkey):
                raise ValueError(f"{key} must be a public key")
            canonical[key] = str(
                Pubkey.from_string(value) if isinstance(value, str) else value
            )
        if canonical["quote_mint"] not in (
            str(Pubkey.default()),
            "So11111111111111111111111111111111111111112",
        ):
            raise ValueError("only SOL quote mint is supported")
        fees = mark.fees
        if not isinstance(fees, PumpFeeSnapshot) or not isinstance(
            fees.config, PumpFeeConfig
        ):
            raise ValueError("mark fee snapshot is malformed")
        _clock(fees.observed_at)
        _clock(fees.attested_at)
        if (
            fees.observed_at != mark.observed_at
            or fees.attested_at != 0
            or type(fees.attested_at) is bool
        ):
            raise ValueError("invalid fee observation clock or fabricated attestation")
        digest = fees.config.digest
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("fee config digest must be SHA256")
        tiers = fees.config.regular_tiers
        if not tiers:
            raise ValueError("fee config has no SOL tiers")
        previous = -1
        for tier in tiers:
            threshold = tier.market_cap_threshold_raw
            if (
                type(threshold) is not int
                or not 0 <= threshold < 2**128
                or threshold <= previous
            ):
                raise ValueError("fee tier thresholds must be ascending u128")
            previous = threshold
            rates = tier.fees
            for rate in (
                rates.lp_fee_bps,
                rates.protocol_fee_bps,
                rates.creator_fee_bps,
            ):
                if type(rate) is not int or not 0 <= rate <= 10_000:
                    raise ValueError("fee rates must be bounded integer bps")
            if (
                rates.lp_fee_bps != 0
                or rates.protocol_fee_bps + rates.creator_fee_bps > 10_000
            ):
                raise ValueError("unsupported curve fee rates")
        if tiers[0].market_cap_threshold_raw != 0:
            raise ValueError("fee tier schedule must start at zero")
        if canonical["real_token_reserves"] > mark.supply_raw:
            raise ValueError("real token reserves exceed current mint supply")
        # Holder burns do not change non-Mayhem curve fee-market-cap supply.
        quote_state = canonical
        evidence = {
            "mint": mark.mint,
            "slot": mark.slot,
            "observed_at": mark.observed_at,
            "curve_state": canonical,
            "supply_raw": mark.supply_raw,
            "fee_config_digest": digest,
            "fee_model": CONFIG["fee_model"],
            "native_fee_attested": False,
            "creator_fee_override_verified": False,
            "proof": copy.deepcopy(mark.proof),
        }
        return quote_state, evidence

    def on_mark(self, mark: Mark) -> None:  # noqa: C901, PLR0915 - atomic entry/exit transition
        try:
            if not isinstance(mark, Mark):
                raise ValueError("on_mark requires Mark")
            quote_state, evidence = self._validated_mark(mark)
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as exc:
            with self._transaction():
                self._state["rejected_marks"] += 1
                self._event("mark_rejected", {"reason": str(exc)})
            return
        with self._transaction():
            item = self._state["active"].get(mark.mint)
            if item is None:
                self._state["rejected_marks"] += 1
                self._event(
                    "mark_rejected", {"mint": mark.mint, "reason": "untracked_mint"}
                )
                return
            if mark.observed_at < item["last_observed_at"] or (
                item["last_slot"] is not None and mark.slot <= item["last_slot"]
            ):
                self._state["rejected_marks"] += 1
                self._event(
                    "mark_rejected",
                    {"mint": mark.mint, "reason": "nonforward_slot_or_clock"},
                )
                return
            self._expire(mark.observed_at)
            item = self._state["active"].get(mark.mint)
            if item is None:
                return
            item.update(last_slot=mark.slot, last_observed_at=mark.observed_at)
            self._state["accepted_marks"] += 1
            self._state["last_mark"] = {
                "mint": mark.mint,
                "slot": mark.slot,
                "observed_at": mark.observed_at,
            }
            self._event("mark", self._state["last_mark"])
            if item["phase"] == "pending":
                self._enter(item, mark, quote_state, evidence)
                return
            for hold in HOLDS:
                arm = item["arms"][str(hold)]
                if (
                    arm["status"] != "open"
                    or mark.observed_at < item["entered_at"] + hold
                ):
                    continue
                try:
                    # Keep our hypothetical deposit/removal in the pool instead of
                    # erasing it whenever a new native account snapshot arrives.
                    exit_state = dict(quote_state)
                    impact = item["entry_impact"]
                    for prefix in ("virtual", "real"):
                        token_key = f"{prefix}_token_reserves"
                        quote_key = f"{prefix}_quote_reserves"
                        exit_state[token_key] = _uint(
                            exit_state[token_key] - impact["token_out_raw"],
                            "paper adjusted token reserves",
                            positive=True,
                        )
                        exit_state[quote_key] = _uint(
                            exit_state[quote_key] + impact["quote_in_raw"],
                            "paper adjusted quote reserves",
                            positive=True,
                        )
                    quote = quote_sell_exact_in(
                        exit_state, item["quantity_raw"], mark.fees
                    )
                except (ValueError, TypeError, OverflowError) as exc:
                    self._event(
                        "exit_quote_rejected",
                        {
                            "cohort": item["id"],
                            "hold_seconds": hold,
                            "reason": str(exc),
                            "mark": evidence,
                        },
                    )
                    continue
                proceeds = quote.amount_out_raw * 99 // 100 - EXIT_COST
                net = proceeds - ENTRY_DEBIT
                arm.update(
                    status="closed",
                    net_lamports=net,
                    proceeds_lamports=proceeds,
                    closed_at=mark.observed_at,
                    slot=mark.slot,
                )
                stats = self._state["arm_stats"][str(hold)]
                stats["closed"] += 1
                stats["known_net_lamports"] += net
                selected = item["portfolio_selected"] and hold == item["hold_seconds"]
                if selected:
                    self._state["cash_lamports"] += proceeds + EXIT_COST
                    self._state["reserved_lamports"] -= ENTRY_RESERVATION
                    self._state["known_paper_net_lamports"] += net
                    self._state["selected_closed"] += 1
                self._state["closures"] += 1
                closure = {
                    "cohort": item["id"],
                    "mint": mark.mint,
                    "hold_seconds": hold,
                    "policy_revision": item["policy_revision"],
                    "selected": selected,
                    "net_lamports": net,
                    "proceeds_lamports": proceeds,
                    "observed_at": mark.observed_at,
                    "slot": mark.slot,
                }
                self._state["last_closure"] = closure
                self._event(
                    "arm_closure",
                    {
                        **closure,
                        "quote": asdict(quote),
                        "paper_quote_state": exit_state,
                        "mark": evidence,
                    },
                )
            self._settle(item)

    def _enter(self, item: dict, mark: Mark, quote_state: dict, evidence: dict) -> None:
        try:
            quote = quote_buy_exact_in(quote_state, CONFIG["entry_lamports"], mark.fees)
            quantity = quote.amount_out_raw * 99 // 100
            _uint(quantity, "haircut entry base quantity", positive=True)
        except (ValueError, TypeError, OverflowError) as exc:
            self._event(
                "entry_quote_rejected",
                {"mint": mark.mint, "reason": str(exc), "mark": evidence},
            )
            return
        hold = self._next_hold()
        block = self._entry_block()
        selected = block is None
        item.update(
            phase="entered",
            id=self._state["next_id"],
            entered_at=mark.observed_at,
            quantity_raw=quantity,
            hold_seconds=hold,
            policy_revision=self._state["policy_revision"],
            portfolio_selected=selected,
            portfolio_skip_reason=block,
            entry=evidence,
            entry_impact={
                "token_out_raw": quote.amount_out_raw,
                "quote_in_raw": quote.net_quote_raw,
            },
            arms={str(h): {"status": "open", "net_lamports": None} for h in HOLDS},
        )
        self._state["next_id"] += 1
        self._state["cohorts_started"] += 1
        if selected:
            self._state["entries"] += 1
            self._state["cash_lamports"] -= ENTRY_RESERVATION
            self._state["reserved_lamports"] += ENTRY_RESERVATION
        else:
            self._state["shadow_entries"] += 1
            counts = self._state["portfolio_skip_counts"]
            counts[block] = counts.get(block, 0) + 1
        for stats in self._state["arm_stats"].values():
            stats["entered"] += 1
        entry = {
            "cohort": item["id"],
            "mint": mark.mint,
            "hold_seconds": hold,
            "policy_revision": item["policy_revision"],
            "portfolio_selected": selected,
            "portfolio_skip_reason": block,
            "quantity_raw": quantity,
            "entry_impact": item["entry_impact"],
            "debit_lamports": ENTRY_RESERVATION if selected else 0,
            "exit_fee_escrow_lamports": EXIT_COST if selected else 0,
            "observed_at": mark.observed_at,
            "slot": mark.slot,
        }
        self._state["last_cohort"] = entry
        if selected:
            self._state["last_entry"] = entry
        self._event(
            "entry" if selected else "shadow_entry",
            {**entry, "quote": asdict(quote), "mark": evidence},
        )

    def _censor_arm(self, item: dict, hold: int, reason: str) -> None:
        arm = item["arms"][str(hold)]
        if arm["status"] != "open":
            return
        arm.update(status="censored", reason=reason, net_lamports=None)
        self._state["arm_stats"][str(hold)]["censored"] += 1
        selected = item["portfolio_selected"] and hold == item["hold_seconds"]
        if selected:
            self._state["unknown"][item["mint"]] = {
                "cohort": item["id"],
                "mint": item["mint"],
                "quantity_raw": item["quantity_raw"],
                "reserved_lamports": ENTRY_RESERVATION,
                "hold_seconds": hold,
                "policy_revision": item["policy_revision"],
                "reason": reason,
                "net_lamports": None,
            }
        self._event(
            "arm_censor",
            {
                "cohort": item["id"],
                "mint": item["mint"],
                "hold_seconds": hold,
                "selected": selected,
                "reason": reason,
                "net_lamports": None,
            },
        )

    def _settle(self, item: dict) -> None:
        arms = item["arms"]
        if any(arm["status"] == "open" for arm in arms.values()):
            return
        state = self._state
        paired = all(arm["status"] == "closed" for arm in arms.values())
        state["settled_cohorts"] += 1
        if paired:
            state["paired_complete"] += 1
            state["rolling"].append(
                {
                    "cohort": item["id"],
                    "nets": {h: arm["net_lamports"] for h, arm in arms.items()},
                }
            )
            state["rolling"] = state["rolling"][-CONFIG["rolling_paired_cohorts"] :]
        else:
            state["censored_cohorts"] += 1
        del state["active"][item["mint"]]
        if not paired:
            self._event(
                "cohort_censored", {"cohort": item["id"], "training_unchanged": True}
            )
            return
        scores = {}
        for hold in HOLDS:
            values = [row["nets"][str(hold)] for row in state["rolling"]]
            if values:
                mean = statistics.mean(values)
                se = (
                    statistics.stdev(values) / math.sqrt(len(values))
                    if len(values) > 1
                    else None
                )
                scores[str(hold)] = {
                    "n": len(values),
                    "mean_net_lamports": mean,
                    "standard_error_lamports": se,
                    "score_lamports": mean - 2 * se if se is not None else None,
                }
        state["scores"] = scores
        if len(state["rolling"]) >= CONFIG["minimum_paired_cohorts"]:
            state["best_hold_seconds"] = max(
                HOLDS, key=lambda h: scores[str(h)]["score_lamports"]
            )
        state["policy_revision"] += 1
        entry_block = self._entry_block()
        learning = {
            "revision": state["policy_revision"],
            "trigger_cohort": item["id"],
            "paired": paired,
            "training_count": len(state["rolling"]),
            "paired_complete": state["paired_complete"],
            "censored_cohorts": state["censored_cohorts"],
            "entered_denominator": state["cohorts_started"],
            "portfolio_entries": state["entries"],
            "settled_denominator": state["settled_cohorts"],
            "scores": scores,
            "hold_seconds": self._next_hold(),
            "policy_action": "observe_only" if entry_block else "paper_buy",
            "portfolio_entry_block": entry_block,
            "applies_to": "future_entries_only",
            "score_is_formal_confidence_interval": False,
        }
        state["last_learning"] = learning
        self._event("policy_revision", learning)

    def _expire(self, now: float) -> None:
        for item in list(self._state["active"].values()):
            if item["phase"] == "pending":
                if now > item["discovered_at"] + CONFIG["discovery_expiry_seconds"]:
                    self._state["expired_discoveries"] += 1
                    del self._state["active"][item["mint"]]
                    self._event(
                        "discovery_expired",
                        {"mint": item["mint"], "reason": "no_entry_within_15_seconds"},
                    )
                continue
            for hold in HOLDS:
                if (
                    now
                    > item["entered_at"] + hold + CONFIG["exit_late_tolerance_seconds"]
                ):
                    self._censor_arm(item, hold, "missing_qualified_mark_by_deadline")
            self._settle(item)

    def tick(self, now: float) -> None:
        _clock(now)
        with self._transaction():
            self._expire(now)

    def _censor_all(self, reason: str) -> None:
        for item in list(self._state["active"].values()):
            if item["phase"] == "pending":
                self._state["expired_discoveries"] += 1
                del self._state["active"][item["mint"]]
                self._event(
                    "discovery_censored", {"mint": item["mint"], "reason": reason}
                )
            else:
                for hold in HOLDS:
                    self._censor_arm(item, hold, reason)
                self._settle(item)

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self._failed:
                with self._transaction():
                    self._censor_all("graceful_stop_without_exit_mark")
                    self._state.update(running=False, connected=False)
                    self._state["runtime"].update(
                        running=False, discovery_connected=False
                    )
                    self._event(
                        "shutdown", {"unknown_inventory": len(self._state["unknown"])}
                    )
        finally:
            self._db.close()
            self._closed = True
