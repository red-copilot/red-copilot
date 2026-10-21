"""Append-only causal ledger shadow for run traces.

M1 deliberately keeps the v2 trace, World and result files authoritative. This
module records the same events beside them so replay equivalence can be measured
before any production read path is switched to the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_DB_NAME = "ledger.db"
LEDGER_STORE_SCHEMA_VERSION = "harness/ledger-store/v1"
LEDGER_EVENT_SCHEMA_VERSION = "harness/ledger-event/v1"
LEDGER_SHADOW_ERROR_SCHEMA_VERSION = "harness/ledger-shadow-error/v1"
GENESIS_HASH = "0" * 64
SHADOW_LEDGER_ENV = "HARNESS_LEDGER_SHADOW"

_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MAX_IDENTIFIER_CHARS = 256


class LedgerError(RuntimeError):
    """Base class for ledger contract failures."""


class LedgerConflict(LedgerError):
    """An event ID was reused with different immutable content."""


class LedgerCorruption(LedgerError):
    """The persisted sequence or hash chain failed verification."""


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    ledger_seq: int
    event_id: str
    run_id: str
    attempt_id: str | None
    producer: str
    trust_tier: str
    kind: str
    schema_version: str
    occurred_at: str
    causation_id: str | None
    correlation_id: str | None
    payload: dict[str, Any]
    artifact_refs: tuple[str, ...]
    prev_hash: str
    record_hash: str
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ledger_seq": self.ledger_seq,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "producer": self.producer,
            "trust_tier": self.trust_tier,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
            "artifact_refs": list(self.artifact_refs),
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
            "content_hash": self.content_hash,
        }


def ledger_db_path(trace_path: Path) -> Path:
    return trace_path.parent / LEDGER_DB_NAME


def shadow_ledger_enabled(value: str | None = None) -> bool:
    """Resolve the M1 shadow flag.

    The shadow is enabled by default so ordinary runs exercise the migration.
    An explicit false value keeps the v2-only rollback path available.
    """
    raw = os.environ.get(SHADOW_LEDGER_ENV) if value is None else value
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_VALUES


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_identifier(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_IDENTIFIER_CHARS:
        raise ValueError(f"{field} must be a non-empty string of at most 256 characters")
    return value


def _producer_and_trust(payload: Mapping[str, Any], actor: Any) -> tuple[str, str]:
    claimed = payload.get("producer")
    producer = claimed if isinstance(claimed, str) and claimed else actor
    if not isinstance(producer, str) or not producer:
        producer = "harness"
    if producer.startswith("agent"):
        return producer, "untrusted"
    if producer == "gateway":
        return producer, "external"
    return producer, "authorized"


def _artifact_refs(payload: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    direct = payload.get("artifact_ref")
    if isinstance(direct, str) and direct:
        refs.append(direct)
    listed = payload.get("artifact_refs")
    if isinstance(listed, (list, tuple)):
        refs.extend(item for item in listed if isinstance(item, str) and item)
    evidence = payload.get("evidence")
    if isinstance(evidence, (list, tuple)):
        for item in evidence:
            if isinstance(item, Mapping):
                ref = item.get("artifact_ref")
                if isinstance(ref, str) and ref:
                    refs.append(ref)
    return tuple(dict.fromkeys(refs))


def _optional_identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= _MAX_IDENTIFIER_CHARS else None


class LedgerStore:
    """Single-writer SQLite store for one Run's append-only causal ledger."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger_records (
                ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                attempt_id TEXT,
                producer TEXT NOT NULL,
                trust_tier TEXT NOT NULL,
                kind TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                causation_id TEXT,
                correlation_id TEXT,
                payload_json TEXT NOT NULL,
                artifact_refs_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS ledger_records_no_update
            BEFORE UPDATE ON ledger_records
            BEGIN
                SELECT RAISE(ABORT, 'ledger is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS ledger_records_no_delete
            BEFORE DELETE ON ledger_records
            BEGIN
                SELECT RAISE(ABORT, 'ledger is append-only');
            END;
            """
        )
        row = self._connection.execute(
            "SELECT value FROM ledger_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO ledger_meta(key, value) VALUES ('schema_version', ?)",
                (LEDGER_STORE_SCHEMA_VERSION,),
            )
            self._connection.commit()
        elif row["value"] != LEDGER_STORE_SCHEMA_VERSION:
            raise LedgerError(
                f"unsupported ledger schema {row['value']!r}; expected {LEDGER_STORE_SCHEMA_VERSION}"
            )

    def append_trace_event(self, event: Mapping[str, Any]) -> LedgerRecord:
        event_id = _safe_identifier(event.get("event_id"), "event_id")
        run_id = _safe_identifier(event.get("run_id"), "run_id")
        kind = _safe_identifier(event.get("type"), "event type")
        payload_value = event.get("data", {})
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        actor = event.get("actor", "harness")
        producer, trust_tier = _producer_and_trust(payload, actor)
        attempt_id = _optional_identifier(payload.get("executor_id")) or _optional_identifier(
            payload.get("session_id")
        )
        causation_id = _optional_identifier(event.get("parent_event_id"))
        action_id = _optional_identifier(event.get("action_id"))
        correlation_id = (
            action_id
            or _optional_identifier(payload.get("tool_call_id"))
            or _optional_identifier(payload.get("executor_id"))
        )
        occurred_at = event.get("ts")
        if not isinstance(occurred_at, str) or not occurred_at:
            occurred_at = datetime.now(UTC).isoformat()
        return self.append(
            event_id=event_id,
            run_id=run_id,
            attempt_id=attempt_id,
            producer=producer,
            trust_tier=trust_tier,
            kind=kind,
            schema_version=LEDGER_EVENT_SCHEMA_VERSION,
            occurred_at=occurred_at,
            causation_id=causation_id,
            correlation_id=correlation_id,
            payload=payload,
            artifact_refs=_artifact_refs(payload),
        )

    def append(
        self,
        *,
        event_id: str,
        run_id: str,
        kind: str,
        occurred_at: str,
        producer: str,
        trust_tier: str,
        payload: Mapping[str, Any],
        attempt_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        schema_version: str = LEDGER_EVENT_SCHEMA_VERSION,
        artifact_refs: Sequence[str] = (),
    ) -> LedgerRecord:
        payload_dict = dict(payload)
        refs = tuple(artifact_refs)
        content_hash = _sha256_json(
            {
                "event_id": event_id,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "producer": producer,
                "trust_tier": trust_tier,
                "kind": kind,
                "schema_version": schema_version,
                "occurred_at": occurred_at,
                "causation_id": causation_id,
                "correlation_id": correlation_id,
                "payload": payload_dict,
                "artifact_refs": list(refs),
            }
        )
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT * FROM ledger_records WHERE event_id = ?", (event_id,)
                ).fetchone()
                if existing is not None:
                    self._connection.commit()
                    if existing["content_hash"] != content_hash:
                        raise LedgerConflict(f"event_id {event_id!r} was reused with new content")
                    return _row_to_record(existing)

                previous = self._connection.execute(
                    "SELECT ledger_seq, record_hash FROM ledger_records "
                    "ORDER BY ledger_seq DESC LIMIT 1"
                ).fetchone()
                ledger_seq = 1 if previous is None else int(previous["ledger_seq"]) + 1
                prev_hash = GENESIS_HASH if previous is None else str(previous["record_hash"])
                record_hash = _sha256_json(
                    {
                        "ledger_seq": ledger_seq,
                        "content_hash": content_hash,
                        "prev_hash": prev_hash,
                    }
                )
                self._connection.execute(
                    """
                    INSERT INTO ledger_records(
                        ledger_seq, event_id, run_id, attempt_id, producer, trust_tier,
                        kind, schema_version, occurred_at, causation_id, correlation_id,
                        payload_json, artifact_refs_json, prev_hash, record_hash, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ledger_seq,
                        event_id,
                        run_id,
                        attempt_id,
                        producer,
                        trust_tier,
                        kind,
                        schema_version,
                        occurred_at,
                        causation_id,
                        correlation_id,
                        _canonical_json(payload_dict),
                        _canonical_json(list(refs)),
                        prev_hash,
                        record_hash,
                        content_hash,
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM ledger_records WHERE ledger_seq = ?", (ledger_seq,)
                ).fetchone()
                self._connection.commit()
                if row is None:
                    raise LedgerError("ledger insert did not produce a record")
                return _row_to_record(row)
            except Exception:
                self._connection.rollback()
                raise

    def records(self) -> list[LedgerRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM ledger_records ORDER BY ledger_seq"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def verify(self) -> None:
        records = self.records()
        previous_hash = GENESIS_HASH
        for expected_seq, record in enumerate(records, start=1):
            if record.ledger_seq != expected_seq:
                raise LedgerCorruption(
                    f"ledger sequence gap: expected {expected_seq}, got {record.ledger_seq}"
                )
            expected_content = _sha256_json(
                {
                    "event_id": record.event_id,
                    "run_id": record.run_id,
                    "attempt_id": record.attempt_id,
                    "producer": record.producer,
                    "trust_tier": record.trust_tier,
                    "kind": record.kind,
                    "schema_version": record.schema_version,
                    "occurred_at": record.occurred_at,
                    "causation_id": record.causation_id,
                    "correlation_id": record.correlation_id,
                    "payload": record.payload,
                    "artifact_refs": list(record.artifact_refs),
                }
            )
            if record.content_hash != expected_content:
                raise LedgerCorruption(f"ledger content hash mismatch at seq {record.ledger_seq}")
            if record.prev_hash != previous_hash:
                raise LedgerCorruption(f"ledger previous hash mismatch at seq {record.ledger_seq}")
            expected_record = _sha256_json(
                {
                    "ledger_seq": record.ledger_seq,
                    "content_hash": record.content_hash,
                    "prev_hash": record.prev_hash,
                }
            )
            if record.record_hash != expected_record:
                raise LedgerCorruption(f"ledger record hash mismatch at seq {record.ledger_seq}")
            previous_hash = record.record_hash

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _row_to_record(row: sqlite3.Row) -> LedgerRecord:
    payload = json.loads(row["payload_json"])
    artifact_refs = json.loads(row["artifact_refs_json"])
    return LedgerRecord(
        ledger_seq=int(row["ledger_seq"]),
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        attempt_id=row["attempt_id"],
        producer=str(row["producer"]),
        trust_tier=str(row["trust_tier"]),
        kind=str(row["kind"]),
        schema_version=str(row["schema_version"]),
        occurred_at=str(row["occurred_at"]),
        causation_id=row["causation_id"],
        correlation_id=row["correlation_id"],
        payload=dict(payload) if isinstance(payload, dict) else {},
        artifact_refs=tuple(item for item in artifact_refs if isinstance(item, str)),
        prev_hash=str(row["prev_hash"]),
        record_hash=str(row["record_hash"]),
        content_hash=str(row["content_hash"]),
    )
