"""Durable, idempotent transaction intent and outcome ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from solders.pubkey import Pubkey

from core.execution_policy import TradeLimitExceeded
from core.transaction_state import TransactionOutcome, TransactionStatus


def default_transaction_ledger_path(wallet: str | Pubkey) -> Path:
    """Return the wallet-wide ledger shared by every trading platform."""
    canonical_wallet = str(Pubkey.from_string(str(wallet)))
    return Path(".state") / "transaction-ledgers" / f"{canonical_wallet}.sqlite3"


_LEGACY_PLATFORM_LEDGER_SUFFIXES = ("pump_fun", "lets_bonk")
_MAX_RISK_SESSION_ID_LENGTH = 128
_MAX_OPERATION_KEY_LENGTH = 512
_MAX_OPERATION_GENERATION = 0x7FFF_FFFF_FFFF_FFFF
_SHA256_HEX_LENGTH = 64


def resolve_transaction_ledger_path(wallet: str | Pubkey) -> Path:
    """Return the shared ledger unless unreconciled platform ledgers exist."""
    shared_path = default_transaction_ledger_path(wallet)
    canonical_wallet = shared_path.stem
    legacy_paths = tuple(
        shared_path.with_name(f"{canonical_wallet}-{suffix}.sqlite3")
        for suffix in _LEGACY_PLATFORM_LEDGER_SUFFIXES
        if shared_path.with_name(f"{canonical_wallet}-{suffix}.sqlite3").exists()
    )
    if legacy_paths:
        names = ", ".join(str(path) for path in legacy_paths)
        raise LedgerConflict(  # noqa: TRY003
            "Found legacy platform-scoped transaction ledger(s): "
            f"{names}. Reconcile them into {shared_path} before live execution; "
            "silently starting a new wallet ledger could replay an unresolved "
            "transaction or reset cumulative risk."
        )
    return shared_path


class LedgerConflict(RuntimeError):
    """Raised when an idempotency key is reused for different transaction data."""


class EvidencePersistenceError(RuntimeError):
    """Raised when durable trade evidence cannot be persisted."""


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    """A submission reservation bound to one exact signed wire transaction."""

    signature: str
    intent_id: str
    signer: str
    quote_amount_raw: int | None
    fee_lamports: int | None
    blockhash: str
    last_valid_block_height: int
    state: str
    wire_bytes: bytes | None
    receipt_destinations: tuple[str, ...] | None
    evidence_profile_id: str | None = None
    risk_session_id: str | None = None
    quote_mint: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """A submission whose terminal on-chain outcome is not yet known."""

    intent_id: str
    signer: str
    quote_amount_raw: int | None
    fee_lamports: int | None
    signature: str
    blockhash: str
    last_valid_block_height: int
    submitted_at: str
    state: str
    wire_bytes: bytes | None
    receipt_destinations: tuple[str, ...] | None
    evidence_profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRiskTotals:
    """Cumulative conservative reservations for one configured risk session."""

    quote_amount_raw_by_mint: dict[str, int]
    fee_lamports: int
    submission_count: int


class TransactionLedger:
    """Small SQLite-WAL ledger for idempotent transaction submissions."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            check_same_thread=False,
        )
        self._closed = False
        self._connection.row_factory = sqlite3.Row
        journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if journal_mode.lower() != "wal":
            self._connection.close()
            raise RuntimeError("transaction ledger requires SQLite WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._create_schema()
        except sqlite3.Error as exc:
            self._connection.close()
            message = "could not initialize evidence ledger"
            raise EvidencePersistenceError(message) from exc

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for narrow operational inspection."""
        return self._connection

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence_profiles (
                    profile_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('live', 'dry_run', 'simulation', 'paper')
                    ),
                    settings_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS evidence_events (
                    event_id TEXT PRIMARY KEY,
                    profile_id TEXT REFERENCES evidence_profiles(profile_id),
                    category TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS evidence_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    signature TEXT NOT NULL,
                    commitment TEXT NOT NULL CHECK (
                        commitment IN ('confirmed', 'finalized')
                    ),
                    profile_id TEXT REFERENCES evidence_profiles(profile_id),
                    observed_fee_lamports TEXT,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    signer TEXT NOT NULL,
                    quote_amount_raw TEXT,
                    fee_lamports TEXT,
                    message_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    signature TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                    blockhash TEXT NOT NULL,
                    last_valid_block_height INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'submitted' CHECK (
                        state IN ('prepared', 'submitted')
                    ),
                    wire_bytes BLOB,
                    receipt_destinations TEXT,
                    evidence_profile_id TEXT REFERENCES evidence_profiles(profile_id),
                    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS submissions_intent_idx
                    ON submissions(intent_id);

                CREATE TABLE IF NOT EXISTS outcomes (
                    signature TEXT PRIMARY KEY REFERENCES submissions(signature),
                    status TEXT NOT NULL CHECK (
                        status IN ('success', 'reverted', 'expired', 'unknown')
                    ),
                    error TEXT,
                    slot INTEGER,
                    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS risk_reservations (
                    signature TEXT PRIMARY KEY
                        REFERENCES submissions(signature) ON DELETE CASCADE,
                    risk_session_id TEXT NOT NULL,
                    signer TEXT NOT NULL,
                    quote_amount_raw TEXT NOT NULL,
                    quote_mint TEXT NOT NULL,
                    fee_lamports TEXT NOT NULL,
                    reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS risk_reservations_session_signer_idx
                    ON risk_reservations(risk_session_id, signer);

                CREATE TABLE IF NOT EXISTS operation_intents (
                    operation_key TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    intent_id TEXT NOT NULL UNIQUE
                        REFERENCES intents(intent_id)
                );
                """
            )

            intent_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(intents)"
                ).fetchall()
            }
            if "message_hash" not in intent_columns:
                self._connection.execute(
                    "ALTER TABLE intents ADD COLUMN message_hash TEXT"
                )

            submission_columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(submissions)"
                ).fetchall()
            }
            if "state" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN state TEXT "
                    "NOT NULL DEFAULT 'submitted'"
                )
            if "wire_bytes" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN wire_bytes BLOB"
                )
            if "receipt_destinations" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN receipt_destinations TEXT"
                )
            if "evidence_profile_id" not in submission_columns:
                self._connection.execute(
                    "ALTER TABLE submissions ADD COLUMN evidence_profile_id TEXT "
                    "REFERENCES evidence_profiles(profile_id)"
                )

    @staticmethod
    def _canonical_json(value: dict) -> str:
        """Encode finite JSON without ambiguous coercion of object keys."""
        if not isinstance(value, dict):
            message = "evidence must be a JSON object"
            raise TypeError(message)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    message = "evidence object keys must be strings"
                    raise TypeError(message)
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif item is not None and not isinstance(item, str | int | float | bool):
                message = "evidence must contain only JSON values"
                raise TypeError(message)
        return encoded

    @classmethod
    def _evidence_id(cls, value: dict) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    def record_evidence_profile(
        self, kind: str, settings: dict, sources: dict[str, str]
    ) -> str:
        """Retain an immutable, content-addressed execution profile."""
        if kind not in {"live", "dry_run", "simulation", "paper"}:
            message = "invalid evidence profile kind"
            raise ValueError(message)
        settings_json = self._canonical_json(settings)
        sources_json = self._canonical_json(sources)
        if any(not isinstance(value, str) for value in sources.values()):
            message = "evidence sources must map names to strings"
            raise TypeError(message)
        profile_id = self._evidence_id(
            {"kind": kind, "settings": settings, "sources": sources}
        )
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO evidence_profiles (
                        profile_id, kind, settings_json, sources_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(profile_id) DO NOTHING
                    """,
                    (profile_id, kind, settings_json, sources_json),
                )
        except sqlite3.Error as exc:
            message = "could not persist evidence profile"
            raise EvidencePersistenceError(message) from exc
        return profile_id

    def _record_trade_evidence_locked(
        self, profile_id: str | None, category: str, payload: dict
    ) -> str:
        """Insert within the caller's transaction, including prepared release."""
        if not isinstance(category, str) or not category:
            message = "evidence category must be a non-empty string"
            raise ValueError(message)
        payload_json = self._canonical_json(payload)
        event_id = self._evidence_id(
            {"profile_id": profile_id, "category": category, "payload": payload}
        )
        self._connection.execute(
            """
            INSERT INTO evidence_events (event_id, profile_id, category, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, profile_id, category, payload_json),
        )
        return event_id

    def record_trade_evidence(
        self, profile_id: str | None, category: str, payload: dict
    ) -> str:
        """Retain repeated observations once and changed observations separately."""
        try:
            with self._lock, self._connection:
                return self._record_trade_evidence_locked(profile_id, category, payload)
        except sqlite3.Error as exc:
            message = "could not persist trade evidence"
            raise EvidencePersistenceError(message) from exc

    def record_receipt_evidence(
        self,
        signature: str,
        commitment: str,
        result: dict,
        *,
        profile_id: str | None = None,
    ) -> str:
        """Retain public receipt observations, never substituting a fee budget."""
        if not isinstance(signature, str) or not signature:
            message = "receipt signature must be a non-empty string"
            raise ValueError(message)
        if commitment not in {"confirmed", "finalized"}:
            message = "receipt commitment must be confirmed or finalized"
            raise ValueError(message)
        payload_json = self._canonical_json(result)
        receipt_id = self._evidence_id(
            {
                "signature": signature,
                "commitment": commitment,
                "profile_id": profile_id,
                "result": result,
            }
        )
        meta = result.get("meta")
        fee = meta.get("fee") if isinstance(meta, dict) else None
        observed_fee = (
            str(fee)
            if isinstance(fee, int) and not isinstance(fee, bool) and 0 <= fee < 2**64
            else None
        )
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO evidence_receipts (
                        receipt_id, signature, commitment, profile_id,
                        observed_fee_lamports, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(receipt_id) DO NOTHING
                    """,
                    (
                        receipt_id,
                        signature,
                        commitment,
                        profile_id,
                        observed_fee,
                        payload_json,
                    ),
                )
        except sqlite3.Error as exc:
            message = "could not persist receipt evidence"
            raise EvidencePersistenceError(message) from exc
        return receipt_id

    @staticmethod
    def _validate_raw_amount(name: str, value: int | None) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{name} must be a non-negative integer or None")

    @staticmethod
    def _encode_receipt_destinations(
        destinations: tuple[str, ...] | None,
    ) -> str | None:
        """Encode the exact native-quote recipients bound to a wire transaction."""
        if destinations is None:
            return None
        if not isinstance(destinations, tuple):
            raise TypeError("receipt_destinations must be a tuple or None")
        if any(not isinstance(item, str) or not item for item in destinations):
            raise ValueError("receipt_destinations must contain non-empty strings")
        if len(set(destinations)) != len(destinations):
            raise ValueError("receipt_destinations must not contain duplicates")
        return json.dumps(list(destinations), separators=(",", ":"))

    @staticmethod
    def _decode_receipt_destinations(raw_value: object) -> tuple[str, ...] | None:
        """Decode durable receipt recipients, rejecting corrupt ledger state."""
        if raw_value is None:
            return None
        if not isinstance(raw_value, str):
            raise LedgerConflict("receipt destinations are not stored as JSON text")
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise LedgerConflict("receipt destinations contain invalid JSON") from exc
        if (
            not isinstance(decoded, list)
            or any(not isinstance(item, str) or not item for item in decoded)
            or len(set(decoded)) != len(decoded)
        ):
            raise LedgerConflict("receipt destinations contain invalid values")
        return tuple(decoded)

    @classmethod
    def _validate_session_risk_limits(
        cls,
        risk_session_id: str,
        max_session_quote_raw: int,
        max_session_fee_lamports: int,
    ) -> None:
        if not isinstance(risk_session_id, str) or not risk_session_id.strip():
            raise ValueError("risk_session_id must be a non-empty string")  # noqa: TRY003
        if len(risk_session_id) > _MAX_RISK_SESSION_ID_LENGTH:
            raise ValueError(  # noqa: TRY003
                "risk_session_id must be at most "
                f"{_MAX_RISK_SESSION_ID_LENGTH} characters"
            )
        cls._validate_raw_amount("max_session_quote_raw", max_session_quote_raw)
        cls._validate_raw_amount("max_session_fee_lamports", max_session_fee_lamports)

    def _reserve_session_risk_locked(  # noqa: PLR0913
        self,
        *,
        signature: str,
        risk_session_id: str,
        signer: str,
        quote_mint: str,
        quote_amount_raw: int,
        fee_lamports: int,
        max_session_quote_raw: int,
        max_session_fee_lamports: int,
    ) -> None:
        existing = self._connection.execute(
            """
            SELECT risk_session_id, signer, quote_mint, quote_amount_raw, fee_lamports
            FROM risk_reservations WHERE signature = ?
            """,
            (signature,),
        ).fetchone()
        expected = (
            risk_session_id,
            signer,
            quote_mint,
            str(quote_amount_raw),
            str(fee_lamports),
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise LedgerConflict(  # noqa: TRY003
                    f"risk reservation for {signature!r} is bound to different data"
                )
            return

        rows = self._connection.execute(
            """
            SELECT quote_mint, quote_amount_raw, fee_lamports
            FROM risk_reservations
            WHERE risk_session_id = ? AND signer = ?
            """,
            (risk_session_id, signer),
        ).fetchall()
        reserved_quote = sum(
            int(row["quote_amount_raw"])
            for row in rows
            if str(row["quote_mint"]) == quote_mint
        )
        reserved_fees = sum(int(row["fee_lamports"]) for row in rows)
        proposed_quote = reserved_quote + quote_amount_raw
        proposed_fees = reserved_fees + fee_lamports
        if proposed_quote > max_session_quote_raw:
            raise TradeLimitExceeded(  # noqa: TRY003
                f"session quote amount {proposed_quote} for {quote_mint} "
                f"exceeds limit {max_session_quote_raw}"
            )
        if proposed_fees > max_session_fee_lamports:
            raise TradeLimitExceeded(  # noqa: TRY003
                f"session fee amount {proposed_fees} exceeds "
                f"limit {max_session_fee_lamports}"
            )
        self._connection.execute(
            """
            INSERT INTO risk_reservations (
                signature, risk_session_id, signer, quote_mint,
                quote_amount_raw, fee_lamports
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                signature,
                risk_session_id,
                signer,
                quote_mint,
                str(quote_amount_raw),
                str(fee_lamports),
            ),
        )

    def get_session_risk_totals(
        self,
        risk_session_id: str,
        signer: str,
    ) -> SessionRiskTotals:
        """Return durable conservative exposure reserved for one session."""
        if not isinstance(risk_session_id, str) or not risk_session_id.strip():
            raise ValueError("risk_session_id must be a non-empty string")  # noqa: TRY003
        if not isinstance(signer, str) or not signer:
            raise ValueError("signer must be a non-empty string")  # noqa: TRY003
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT quote_mint, quote_amount_raw, fee_lamports
                FROM risk_reservations
                WHERE risk_session_id = ? AND signer = ?
                """,
                (risk_session_id, signer),
            ).fetchall()
        quote_totals: dict[str, int] = {}
        for row in rows:
            quote_mint = str(row["quote_mint"])
            quote_totals[quote_mint] = quote_totals.get(quote_mint, 0) + int(
                row["quote_amount_raw"]
            )
        return SessionRiskTotals(
            quote_amount_raw_by_mint=quote_totals,
            fee_lamports=sum(int(row["fee_lamports"]) for row in rows),
            submission_count=len(rows),
        )

    def reserve_operation_intent(self, operation_key: str, signer: str) -> str:
        """Reuse unresolved work or allocate a new terminal-safe generation."""
        if (
            not isinstance(operation_key, str)
            or not operation_key
            or len(operation_key) > _MAX_OPERATION_KEY_LENGTH
        ):
            raise ValueError(  # noqa: TRY003
                "operation_key must be a non-empty string of at most "
                f"{_MAX_OPERATION_KEY_LENGTH} characters"
            )
        if not isinstance(signer, str) or not signer:
            raise ValueError("signer must be a non-empty string")  # noqa: TRY003

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                current = self._connection.execute(
                    """
                    SELECT
                        operation_intents.generation,
                        operation_intents.intent_id,
                        intents.signer
                    FROM operation_intents
                    JOIN intents
                      ON intents.intent_id = operation_intents.intent_id
                    WHERE operation_intents.operation_key = ?
                    """,
                    (operation_key,),
                ).fetchone()
                if current is not None:
                    if str(current["signer"]) != signer:
                        raise LedgerConflict(  # noqa: TRY003, TRY301
                            "operation key is bound to a different signer"
                        )
                    current_intent = str(current["intent_id"])
                    submission = self._connection.execute(
                        "SELECT 1 FROM submissions WHERE intent_id = ? LIMIT 1",
                        (current_intent,),
                    ).fetchone()
                    unresolved = self._connection.execute(
                        """
                        SELECT 1
                        FROM submissions AS s
                        LEFT JOIN outcomes AS o ON o.signature = s.signature
                        WHERE s.intent_id = ?
                          AND (o.status IS NULL OR o.status = 'unknown')
                        LIMIT 1
                        """,
                        (current_intent,),
                    ).fetchone()
                    if submission is None or unresolved is not None:
                        self._connection.commit()
                        return current_intent
                    generation = int(current["generation"]) + 1
                else:
                    generation = 1

                if generation > _MAX_OPERATION_GENERATION:
                    raise LedgerConflict(  # noqa: TRY003, TRY301
                        "operation generation is exhausted"
                    )
                intent_id = f"{operation_key}:{generation}"
                self._connection.execute(
                    """
                    INSERT INTO intents (
                        intent_id, signer, quote_amount_raw,
                        fee_lamports, message_hash
                    ) VALUES (?, ?, NULL, NULL, NULL)
                    """,
                    (intent_id, signer),
                )
                if current is None:
                    self._connection.execute(
                        """
                        INSERT INTO operation_intents (
                            operation_key, generation, intent_id
                        ) VALUES (?, ?, ?)
                        """,
                        (operation_key, generation, intent_id),
                    )
                else:
                    cursor = self._connection.execute(
                        """
                        UPDATE operation_intents
                        SET generation = ?, intent_id = ?
                        WHERE operation_key = ?
                        """,
                        (generation, intent_id, operation_key),
                    )
                    if cursor.rowcount != 1:
                        raise LedgerConflict(  # noqa: TRY003, TRY301
                            "operation generation changed during reservation"
                        )
                self._connection.commit()
                return intent_id  # noqa: TRY300
            except Exception:
                self._connection.rollback()
                raise

    def record_intent(
        self,
        intent_id: str,
        signer: str,
        quote_amount_raw: int | None,
        fee_lamports: int | None,
        message_hash: str | None = None,
    ) -> None:
        """Create an intent, or accept an exact replay of the same intent."""
        if not intent_id or not signer:
            raise ValueError("intent_id and signer are required")
        self._validate_raw_amount("quote_amount_raw", quote_amount_raw)
        self._validate_raw_amount("fee_lamports", fee_lamports)
        if message_hash is not None and (
            len(message_hash) != _SHA256_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in message_hash)
        ):
            raise ValueError(  # noqa: TRY003
                "message_hash must be a lowercase SHA-256 digest"
            )

        values = (
            intent_id,
            signer,
            None if quote_amount_raw is None else str(quote_amount_raw),
            None if fee_lamports is None else str(fee_lamports),
            message_hash,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT
                        intent_id, signer, quote_amount_raw, fee_lamports, message_hash
                    FROM intents WHERE intent_id = ?
                    """,
                    (intent_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        """
                        INSERT INTO intents (
                            intent_id, signer, quote_amount_raw, fee_lamports, message_hash
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                elif tuple(row) != values:
                    has_submission = self._connection.execute(
                        "SELECT 1 FROM submissions WHERE intent_id = ? LIMIT 1",
                        (intent_id,),
                    ).fetchone()
                    if has_submission is not None:
                        raise LedgerConflict(  # noqa: TRY003, TRY301
                            f"intent id {intent_id!r} is already bound to different data"
                        )
                    self._connection.execute(
                        """
                        UPDATE intents
                        SET signer = ?, quote_amount_raw = ?, fee_lamports = ?,
                            message_hash = ?
                        WHERE intent_id = ?
                        """,
                        (
                            signer,
                            str(quote_amount_raw)
                            if quote_amount_raw is not None
                            else None,
                            str(fee_lamports) if fee_lamports is not None else None,
                            message_hash,
                            intent_id,
                        ),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    @classmethod
    def _submission_record_from_row(
        cls,
        row: sqlite3.Row,
    ) -> SubmissionRecord:
        wire_bytes = row["wire_bytes"]
        return SubmissionRecord(
            signature=str(row["signature"]),
            intent_id=str(row["intent_id"]),
            blockhash=str(row["blockhash"]),
            last_valid_block_height=int(row["last_valid_block_height"]),
            state=str(row["state"]),
            signer=str(row["signer"]),
            quote_amount_raw=(
                None
                if row["quote_amount_raw"] is None
                else int(row["quote_amount_raw"])
            ),
            fee_lamports=(
                None if row["fee_lamports"] is None else int(row["fee_lamports"])
            ),
            wire_bytes=None if wire_bytes is None else bytes(wire_bytes),
            receipt_destinations=cls._decode_receipt_destinations(
                row["receipt_destinations"]
            ),
            evidence_profile_id=row["evidence_profile_id"],
            risk_session_id=(
                None if row["risk_session_id"] is None else str(row["risk_session_id"])
            ),
            quote_mint=(None if row["quote_mint"] is None else str(row["quote_mint"])),
        )

    def get_active_submission_record(self, intent_id: str) -> SubmissionRecord | None:
        """Return the safest reusable submission for an intent."""
        if not intent_id:
            raise ValueError("intent_id is required")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    s.signature,
                    s.intent_id,
                    i.signer,
                    i.quote_amount_raw,
                    i.fee_lamports,
                    s.blockhash,
                    s.last_valid_block_height,
                    s.state,
                    s.wire_bytes,
                    s.receipt_destinations,
                    s.evidence_profile_id,
                    r.risk_session_id,
                    r.quote_mint
                FROM submissions AS s
                JOIN intents AS i ON i.intent_id = s.intent_id
                LEFT JOIN outcomes AS o ON o.signature = s.signature
                LEFT JOIN risk_reservations AS r ON r.signature = s.signature
                WHERE s.intent_id = ?
                  AND (
                      o.status IS NULL
                      OR o.status IN ('unknown', 'success')
                  )
                ORDER BY
                    CASE o.status
                        WHEN 'success' THEN 0
                        WHEN 'unknown' THEN 1
                        ELSE 2
                    END,
                    s.submitted_at DESC,
                    s.rowid DESC
                LIMIT 1
                """,
                (intent_id,),
            ).fetchone()
        return None if row is None else self._submission_record_from_row(row)

    def get_latest_submission_record(
        self,
        intent_id: str,
    ) -> SubmissionRecord | None:
        """Return the latest submission for an intent, including terminal outcomes."""
        if not intent_id:
            raise ValueError("intent_id is required")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    s.signature,
                    s.intent_id,
                    i.signer,
                    i.quote_amount_raw,
                    i.fee_lamports,
                    s.blockhash,
                    s.last_valid_block_height,
                    s.state,
                    s.wire_bytes,
                    s.receipt_destinations,
                    s.evidence_profile_id,
                    r.risk_session_id,
                    r.quote_mint
                FROM submissions AS s
                JOIN intents AS i ON i.intent_id = s.intent_id
                LEFT JOIN risk_reservations AS r ON r.signature = s.signature
                WHERE s.intent_id = ?
                ORDER BY s.submitted_at DESC, s.rowid DESC
                LIMIT 1
                """,
                (intent_id,),
            ).fetchone()
        return None if row is None else self._submission_record_from_row(row)

    def get_active_submission(self, intent_id: str) -> str | None:
        """Return a submission that is successful or not yet resolved."""
        record = self.get_active_submission_record(intent_id)
        return None if record is None else record.signature

    def list_nonterminal_submissions(self, signer: str) -> list[dict[str, str]]:
        """Return every submission for a signer with no terminal outcome yet."""
        if not signer:
            raise ValueError("signer is required")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT s.intent_id, s.signature, s.state,
                       COALESCE(o.status, 'none') AS outcome
                FROM submissions AS s
                JOIN intents AS i ON i.intent_id = s.intent_id
                LEFT JOIN outcomes AS o ON o.signature = s.signature
                WHERE i.signer = ?
                  AND (o.status IS NULL OR o.status = 'unknown')
                ORDER BY s.submitted_at ASC, s.rowid ASC
                """,
                (signer,),
            ).fetchall()
        return [
            {
                "intent_id": str(row["intent_id"]),
                "signature": str(row["signature"]),
                "state": str(row["state"]),
                "outcome": str(row["outcome"]),
            }
            for row in rows
        ]

    def get_receipt_destinations(self, signature: str) -> tuple[str, ...] | None:
        """Return exact native-quote receipt destinations for a submission."""
        if not signature:
            raise ValueError("signature is required")
        with self._lock:
            row = self._connection.execute(
                "SELECT receipt_destinations FROM submissions WHERE signature = ?",
                (signature,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_receipt_destinations(row["receipt_destinations"])

    @staticmethod
    def _validate_submission_state(state: str) -> None:
        if state not in {"prepared", "submitted"}:
            raise ValueError("state must be 'prepared' or 'submitted'")

    def record_submission(  # noqa: PLR0915
        self,
        intent_id: str,
        signature: str,
        blockhash: str,
        last_valid_block_height: int,
        *,
        wire_bytes: bytes | None = None,
        state: str = "submitted",
        receipt_destinations: tuple[str, ...] | None = None,
        quote_mint: str | None = None,
        risk_session_id: str | None = None,
        max_session_quote_raw: int | None = None,
        max_session_fee_lamports: int | None = None,
        intent_message_hash: str | None = None,
        evidence_profile_id: str | None = None,
    ) -> str:
        """Atomically reserve one reusable exact-wire submission."""
        if not intent_id or not signature or not blockhash:
            raise ValueError("intent_id, signature, and blockhash are required")
        if (
            isinstance(last_valid_block_height, bool)
            or not isinstance(last_valid_block_height, int)
            or last_valid_block_height < 0
        ):
            raise ValueError("last_valid_block_height must be a non-negative integer")
        self._validate_submission_state(state)
        if wire_bytes is not None and not isinstance(
            wire_bytes, (bytes, bytearray, memoryview)
        ):
            raise TypeError("wire_bytes must be bytes-like or None")
        normalized_wire = None if wire_bytes is None else bytes(wire_bytes)
        normalized_destinations = self._encode_receipt_destinations(
            receipt_destinations
        )
        risk_parameters = (
            quote_mint,
            risk_session_id,
            max_session_quote_raw,
            max_session_fee_lamports,
        )
        enforce_session_risk = any(value is not None for value in risk_parameters)
        if intent_message_hash is not None and (
            not isinstance(intent_message_hash, str)
            or len(intent_message_hash) != _SHA256_HEX_LENGTH
            or any(
                character not in "0123456789abcdef" for character in intent_message_hash
            )
        ):
            raise ValueError(  # noqa: TRY003
                "intent_message_hash must be a lowercase SHA-256 digest"
            )
        if enforce_session_risk and intent_message_hash is None:
            raise ValueError(  # noqa: TRY003
                "intent_message_hash is required with session risk parameters"
            )
        if enforce_session_risk:
            if any(value is None for value in risk_parameters):
                raise ValueError(  # noqa: TRY003
                    "quote_mint, risk_session_id, max_session_quote_raw, and "
                    "max_session_fee_lamports must be provided together"
                )
            quote_mint = cast("str", quote_mint)
            risk_session_id = cast("str", risk_session_id)
            max_session_quote_raw = cast("int", max_session_quote_raw)
            max_session_fee_lamports = cast("int", max_session_fee_lamports)
            if not isinstance(quote_mint, str) or not quote_mint:
                raise ValueError("quote_mint must be a non-empty string")  # noqa: TRY003
            self._validate_session_risk_limits(
                risk_session_id,
                max_session_quote_raw,
                max_session_fee_lamports,
            )

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                active = self._connection.execute(
                    """
                    SELECT
                        s.signature, i.signer, i.message_hash,
                        i.quote_amount_raw, i.fee_lamports
                    FROM submissions AS s
                    JOIN intents AS i ON i.intent_id = s.intent_id
                    LEFT JOIN outcomes AS o ON o.signature = s.signature
                    WHERE s.intent_id = ?
                      AND (
                          o.status IS NULL
                          OR o.status IN ('unknown', 'success')
                      )
                    ORDER BY
                        CASE o.status
                            WHEN 'success' THEN 0
                            WHEN 'unknown' THEN 1
                            ELSE 2
                        END,
                        s.submitted_at DESC,
                        s.rowid DESC
                    LIMIT 1
                    """,
                    (intent_id,),
                ).fetchone()
                if (
                    active is not None
                    and intent_message_hash is not None
                    and active["message_hash"] != intent_message_hash
                ):
                    raise LedgerConflict(  # noqa: TRY003, TRY301
                        "intent changed before submission"
                    )
                if active is not None and str(active["signature"]) != signature:
                    if enforce_session_risk:
                        if (
                            active["quote_amount_raw"] is None
                            or active["fee_lamports"] is None
                        ):
                            raise LedgerConflict(  # noqa: TRY003, TRY301
                                f"submission {active['signature']!r} "
                                "lacks risk metadata"
                            )
                        self._reserve_session_risk_locked(
                            signature=str(active["signature"]),
                            risk_session_id=risk_session_id,
                            signer=str(active["signer"]),
                            quote_mint=quote_mint,
                            quote_amount_raw=int(active["quote_amount_raw"]),
                            fee_lamports=int(active["fee_lamports"]),
                            max_session_quote_raw=max_session_quote_raw,
                            max_session_fee_lamports=max_session_fee_lamports,
                        )
                    self._connection.commit()
                    return str(active["signature"])

                self._connection.execute(
                    """
                    INSERT INTO submissions (
                        signature, intent_id, blockhash, last_valid_block_height,
                        state, wire_bytes, receipt_destinations, evidence_profile_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(signature) DO NOTHING
                    """,
                    (
                        signature,
                        intent_id,
                        blockhash,
                        last_valid_block_height,
                        state,
                        normalized_wire,
                        normalized_destinations,
                        evidence_profile_id,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT
                        s.signature,
                        s.intent_id,
                        s.blockhash,
                        s.last_valid_block_height,
                        s.state,
                        s.wire_bytes,
                        s.receipt_destinations,
                        i.signer,
                        i.message_hash,
                        i.quote_amount_raw,
                        i.fee_lamports,
                        o.status AS outcome_status
                    FROM submissions AS s
                    JOIN intents AS i ON i.intent_id = s.intent_id
                    LEFT JOIN outcomes AS o ON o.signature = s.signature
                    WHERE s.signature = ?
                    """,
                    (signature,),
                ).fetchone()
                if row is None:
                    raise LedgerConflict(
                        f"submission {signature!r} disappeared during reservation"
                    )
                if (
                    intent_message_hash is not None
                    and row["message_hash"] != intent_message_hash
                ):
                    raise LedgerConflict("intent changed before submission")
                outcome_status = row["outcome_status"]
                if outcome_status in {
                    TransactionStatus.SUCCESS.value,
                    TransactionStatus.REVERTED.value,
                    TransactionStatus.EXPIRED.value,
                }:
                    raise LedgerConflict(
                        f"signature {signature!r} already has terminal outcome "
                        f"{outcome_status!r}"
                    )
                if (
                    str(row["intent_id"]) != intent_id
                    or str(row["blockhash"]) != blockhash
                    or int(row["last_valid_block_height"]) != last_valid_block_height
                ):
                    raise LedgerConflict(
                        f"signature {signature!r} is already bound to different data"
                    )
                existing_wire = row["wire_bytes"]
                if (
                    normalized_wire is not None
                    and existing_wire is not None
                    and bytes(existing_wire) != normalized_wire
                ):
                    raise LedgerConflict(
                        f"signature {signature!r} is bound to different wire bytes"
                    )
                existing_destinations = row["receipt_destinations"]
                if normalized_destinations is not None:
                    if existing_destinations is None:
                        raise LedgerConflict(
                            f"signature {signature!r} has no durable receipt context"
                        )
                    if self._decode_receipt_destinations(existing_destinations) != (
                        receipt_destinations
                    ):
                        raise LedgerConflict(
                            f"signature {signature!r} is bound to different "
                            "receipt destinations"
                        )
                existing_state = str(row["state"])
                if existing_state == "submitted" and state == "prepared":
                    raise LedgerConflict(
                        f"submission {signature!r} cannot return to prepared state"
                    )
                if (existing_state == "prepared" and state == "submitted") or (
                    existing_wire is None and normalized_wire is not None
                ):
                    cursor = self._connection.execute(
                        """
                        UPDATE submissions
                        SET state = CASE
                                WHEN state = 'prepared' AND ? = 'submitted'
                                THEN 'submitted'
                                ELSE state
                            END,
                            wire_bytes = COALESCE(wire_bytes, ?)
                        WHERE signature = ?
                        """,
                        (state, normalized_wire, signature),
                    )
                    if cursor.rowcount != 1:
                        raise LedgerConflict(
                            f"submission {signature!r} changed during reservation"
                        )
                if enforce_session_risk:
                    if row["quote_amount_raw"] is None or row["fee_lamports"] is None:
                        raise LedgerConflict(  # noqa: TRY003, TRY301
                            f"submission {signature!r} lacks risk metadata"
                        )
                    self._reserve_session_risk_locked(
                        signature=signature,
                        risk_session_id=risk_session_id,
                        signer=str(row["signer"]),
                        quote_mint=quote_mint,
                        quote_amount_raw=int(row["quote_amount_raw"]),
                        fee_lamports=int(row["fee_lamports"]),
                        max_session_quote_raw=max_session_quote_raw,
                        max_session_fee_lamports=max_session_fee_lamports,
                    )
                self._connection.commit()
                return signature
            except sqlite3.Error as exc:
                try:
                    self._connection.rollback()
                finally:
                    message = "could not persist submission provenance"
                    raise EvidencePersistenceError(message) from exc
            except Exception:
                self._connection.rollback()
                raise

    def mark_submission_submitted(self, signature: str) -> None:
        """Mark a prepared wire transaction as eligible for recovery."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    UPDATE submissions SET state = 'submitted'
                    WHERE signature = ? AND state = 'prepared'
                      AND NOT EXISTS (
                          SELECT 1 FROM outcomes
                          WHERE signature = submissions.signature
                      )
                    """,
                    (signature,),
                )
                if cursor.rowcount == 1:
                    self._connection.commit()
                    return

                row = self._connection.execute(
                    """
                    SELECT s.state, o.status AS outcome_status
                    FROM submissions AS s
                    LEFT JOIN outcomes AS o ON o.signature = s.signature
                    WHERE s.signature = ?
                    """,
                    (signature,),
                ).fetchone()
                if row is None:
                    raise LedgerConflict(f"submission {signature!r} is not tracked")
                if row["outcome_status"] is not None:
                    raise LedgerConflict(
                        f"submission {signature!r} already has an outcome"
                    )
                if str(row["state"]) != "submitted":
                    raise LedgerConflict(f"submission {signature!r} is not prepared")
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def release_prepared_submission(self, signature: str) -> bool:
        """Remove a reservation only when no network send was attempted."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                prepared = self._connection.execute(
                    """
                    SELECT s.intent_id, s.wire_bytes, s.evidence_profile_id,
                           i.quote_amount_raw, i.fee_lamports
                    FROM submissions AS s
                    JOIN intents AS i ON i.intent_id = s.intent_id
                    WHERE s.signature = ? AND s.state = 'prepared'
                      AND NOT EXISTS (
                          SELECT 1 FROM outcomes
                          WHERE signature = s.signature
                      )
                    """,
                    (signature,),
                ).fetchone()
                if prepared is not None:
                    wire = prepared["wire_bytes"]
                    self._record_trade_evidence_locked(
                        prepared["evidence_profile_id"],
                        "submission_released",
                        {
                            "signature": signature,
                            "intent_id": prepared["intent_id"],
                            "quote_amount_raw": (
                                None
                                if prepared["quote_amount_raw"] is None
                                else int(prepared["quote_amount_raw"])
                            ),
                            "fee_budget_lamports": (
                                None
                                if prepared["fee_lamports"] is None
                                else int(prepared["fee_lamports"])
                            ),
                            "wire_sha256": (
                                None
                                if wire is None
                                else hashlib.sha256(bytes(wire)).hexdigest()
                            ),
                        },
                    )
                cursor = self._connection.execute(
                    """
                    DELETE FROM submissions
                    WHERE signature = ? AND state = 'prepared'
                      AND NOT EXISTS (
                          SELECT 1 FROM outcomes WHERE signature = submissions.signature
                      )
                    """,
                    (signature,),
                )
                if cursor.rowcount == 1 and prepared is not None:
                    self._connection.execute(
                        """
                        DELETE FROM intents
                        WHERE intent_id = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM submissions
                              WHERE intent_id = intents.intent_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM operation_intents
                              WHERE intent_id = intents.intent_id
                          )
                        """,
                        (str(prepared["intent_id"]),),
                    )
                self._connection.commit()
                return cursor.rowcount == 1
            except sqlite3.Error as exc:
                try:
                    self._connection.rollback()
                finally:
                    message = "could not persist prepared release evidence"
                    raise EvidencePersistenceError(message) from exc
            except Exception:
                self._connection.rollback()
                raise

    def record_outcome(
        self,
        outcome: TransactionOutcome,
        *,
        allow_prepared: bool = False,
    ) -> None:
        """Atomically record evidence without replacing a final status."""
        if not isinstance(allow_prepared, bool):
            raise TypeError("allow_prepared must be a boolean")
        values = (
            outcome.signature,
            outcome.status.value,
            outcome.error,
            outcome.slot,
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                submission = self._connection.execute(
                    """
                    UPDATE submissions SET state = 'submitted'
                    WHERE signature = ?
                      AND (
                          state = 'submitted'
                          OR (? = 1 AND state = 'prepared')
                      )
                    """,
                    (outcome.signature, int(allow_prepared)),
                )
                if submission.rowcount != 1:
                    row = self._connection.execute(
                        "SELECT state FROM submissions WHERE signature = ?",
                        (outcome.signature,),
                    ).fetchone()
                    if row is None:
                        raise LedgerConflict(
                            f"submission {outcome.signature!r} is not tracked"
                        )
                    raise LedgerConflict(
                        f"submission {outcome.signature!r} is not submitted"
                    )

                existing = self._connection.execute(
                    """
                    SELECT status, error, slot
                    FROM outcomes WHERE signature = ?
                    """,
                    (outcome.signature,),
                ).fetchone()
                if existing is None:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO outcomes (signature, status, error, slot)
                        VALUES (?, ?, ?, ?)
                        """,
                        values,
                    )
                    if cursor.rowcount != 1:
                        raise LedgerConflict(
                            f"could not record outcome for {outcome.signature!r}"
                        )
                    self._connection.commit()
                    return

                existing_status = TransactionStatus(existing["status"])
                if (
                    existing_status is TransactionStatus.UNKNOWN
                    and outcome.status is TransactionStatus.UNKNOWN
                ):
                    # UNKNOWN is an observation, not final evidence. Retain the
                    # first diagnostic while allowing later polls to converge.
                    self._connection.commit()
                    return
                if existing_status is outcome.status:
                    if (
                        existing["error"] != outcome.error
                        or existing["slot"] != outcome.slot
                    ):
                        raise LedgerConflict(
                            f"outcome for {outcome.signature!r} has "
                            "conflicting evidence"
                        )
                    self._connection.commit()
                    return
                if existing_status is not TransactionStatus.UNKNOWN:
                    raise LedgerConflict(
                        f"final outcome for {outcome.signature!r} cannot be replaced"
                    )
                cursor = self._connection.execute(
                    """
                    UPDATE outcomes
                    SET status = ?, error = ?, slot = ?,
                        observed_at = CURRENT_TIMESTAMP
                    WHERE signature = ? AND status = 'unknown'
                    """,
                    (
                        outcome.status.value,
                        outcome.error,
                        outcome.slot,
                        outcome.signature,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LedgerConflict(
                        f"outcome for {outcome.signature!r} changed concurrently"
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_outcome(self, signature: str) -> TransactionOutcome | None:
        """Return the latest recorded outcome for a signature."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT status, error, slot FROM outcomes WHERE signature = ?
                """,
                (signature,),
            ).fetchone()
        if row is None:
            return None
        return TransactionOutcome(
            status=TransactionStatus(row["status"]),
            signature=signature,
            error=row["error"],
            slot=row["slot"],
        )

    def get_last_valid_block_height(self, signature: str) -> int | None:
        """Return the validity height retained for a submitted signature."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT last_valid_block_height
                FROM submissions WHERE signature = ?
                """,
                (signature,),
            ).fetchone()
        return None if row is None else int(row["last_valid_block_height"])

    def list_recoverable(self, limit: int = 100) -> list[RecoveryRecord]:
        """List unresolved submissions, including prepared exact-wire sends."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    i.intent_id,
                    i.signer,
                    i.quote_amount_raw,
                    i.fee_lamports,
                    s.signature,
                    s.blockhash,
                    s.last_valid_block_height,
                    s.submitted_at,
                    s.state,
                    s.wire_bytes,
                    s.receipt_destinations,
                    s.evidence_profile_id
                FROM submissions AS s
                JOIN intents AS i ON i.intent_id = s.intent_id
                LEFT JOIN outcomes AS o ON o.signature = s.signature
                WHERE o.signature IS NULL OR o.status = 'unknown'
                ORDER BY s.submitted_at, s.rowid
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            RecoveryRecord(
                intent_id=str(row["intent_id"]),
                signer=str(row["signer"]),
                quote_amount_raw=(
                    None
                    if row["quote_amount_raw"] is None
                    else int(row["quote_amount_raw"])
                ),
                fee_lamports=(
                    None if row["fee_lamports"] is None else int(row["fee_lamports"])
                ),
                signature=str(row["signature"]),
                blockhash=str(row["blockhash"]),
                last_valid_block_height=int(row["last_valid_block_height"]),
                submitted_at=str(row["submitted_at"]),
                state=str(row["state"]),
                wire_bytes=(
                    None if row["wire_bytes"] is None else bytes(row["wire_bytes"])
                ),
                receipt_destinations=self._decode_receipt_destinations(
                    row["receipt_destinations"]
                ),
                evidence_profile_id=row["evidence_profile_id"],
            )
            for row in rows
        ]

    def close(self) -> None:
        """Close the ledger connection idempotently."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> TransactionLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
