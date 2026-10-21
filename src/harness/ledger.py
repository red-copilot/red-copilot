"""Append-only causal ledger shadow for run traces.

M1 deliberately keeps the v2 trace, World and result files authoritative. This
module records the same events beside them so replay equivalence can be measured
before any production read path is switched to the ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .secureio import read_regular_text

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


@dataclass(frozen=True, slots=True)
class LedgerProjection:
    run_id: str
    terminal: dict[str, Any] | None
    objective_status: str | None
    actions: tuple[dict[str, Any], ...]
    last_action: dict[str, Any] | None
    active_actions: dict[str, dict[str, Any]]
    budget: dict[str, int | float]
    submissions: dict[str, int]
    world_revision: int
    event_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "terminal": dict(self.terminal) if self.terminal is not None else None,
            "objective_status": self.objective_status,
            "actions": [dict(action) for action in self.actions],
            "last_action": dict(self.last_action) if self.last_action is not None else None,
            "active_actions": {
                key: dict(value) for key, value in self.active_actions.items()
            },
            "budget": dict(self.budget),
            "submissions": dict(self.submissions),
            "world_revision": self.world_revision,
            "event_count": self.event_count,
        }


@dataclass(frozen=True, slots=True)
class LedgerComparison:
    ok: bool
    mismatches: tuple[dict[str, Any], ...]
    projection: LedgerProjection

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mismatches": [dict(item) for item in self.mismatches],
            "projection": self.projection.as_dict(),
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

def import_trace(trace_path: Path, ledger_path: Path) -> list[LedgerRecord]:
    """Import one historical trace JSONL into a fresh or continuing ledger."""
    store = LedgerStore(ledger_path)
    imported: list[LedgerRecord] = []
    try:
        for line_number, raw in enumerate(
            read_regular_text(trace_path, max_bytes=64 * 1024 * 1024).splitlines(), start=1
        ):
            if not raw.strip():
                continue
            event = json.loads(raw)
            if not isinstance(event, Mapping):
                raise TypeError(f"trace line {line_number} is not a JSON object")
            imported.append(store.append_trace_event(event))
        store.verify()
    finally:
        store.close()
    return imported


def _valid_revision(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _project_action(record: LedgerRecord, status: str) -> dict[str, Any]:
    payload = record.payload
    action: dict[str, Any] = {
        "tool_call_id": record.correlation_id,
        "status": status,
    }
    tool = payload.get("tool")
    if isinstance(tool, str) and tool:
        action["tool"] = tool
    skill_id = payload.get("skill_id")
    if isinstance(skill_id, str) and skill_id:
        action["skill_id"] = skill_id
    return action


def project_records(records: Sequence[LedgerRecord]) -> LedgerProjection:
    """Rebuild the M1 comparison view using only append-only ledger records."""
    from .budget import UsageMetrics, charge_usage_event

    ordered = sorted(records, key=lambda record: record.ledger_seq)
    metrics = UsageMetrics()
    last_action: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = []
    active_actions: dict[str, dict[str, Any]] = {}
    terminal: dict[str, Any] | None = None
    objective_status: str | None = None
    world_revision = 0
    submissions = {"proposed": 0, "accepted": 0, "rejected": 0}

    for record in ordered:
        payload = record.payload
        kind = record.kind
        if kind == "tool.call":
            action = _project_action(record, "running")
            last_action = action
            actions.append(dict(action))
            if record.correlation_id:
                active_actions[record.correlation_id] = dict(action)
        elif kind == "tool.result":
            status = payload.get("execution_status")
            if status not in {"running", "succeeded", "failed"}:
                status = "failed" if bool(payload.get("is_error")) else "succeeded"
            action = _project_action(record, status)
            last_action = action
            actions.append(dict(action))
            if record.correlation_id:
                active_actions.pop(record.correlation_id, None)

        if kind in {"model.request", "model.usage", "tool.call", "tool.denied"}:
            charge_usage_event(metrics, kind, payload)

        if kind == "benchmark.submission.proposed":
            submissions["proposed"] += 1
        elif kind == "benchmark.submission.result":
            accepted = payload.get("accepted")
            if accepted is True:
                submissions["accepted"] += 1
            elif accepted is False:
                submissions["rejected"] += 1
        elif kind == "objective.verdict":
            status = payload.get("status")
            if isinstance(status, str):
                objective_status = status
        elif kind == "run.finished":
            status = payload.get("status")
            success = payload.get("success")
            score = payload.get("score")
            terminal = {
                "status": status if isinstance(status, str) else None,
                "success": success if isinstance(success, bool) else None,
                "score": float(score)
                if isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(float(score))
                else None,
            }

        for key in ("world_revision", "revision_after", "revision"):
            revision = _valid_revision(payload.get(key))
            if revision is not None:
                world_revision = max(world_revision, revision)

    return LedgerProjection(
        run_id=ordered[0].run_id if ordered else "",
        terminal=terminal,
        objective_status=objective_status,
        actions=tuple(actions),
        last_action=last_action,
        active_actions=active_actions,
        budget={
            "tokens": metrics.total_tokens,
            "model_calls": metrics.model_calls,
            "tool_calls": metrics.tool_calls,
            "cost_usd": metrics.cost_usd,
        },
        submissions=submissions,
        world_revision=world_revision,
        event_count=len(ordered),
    )


def _same_number(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    return left == right


def _action_identity(action: Any) -> dict[str, Any] | None:
    if not isinstance(action, Mapping):
        return None
    return {
        "tool_call_id": action.get("tool_call_id"),
        "status": action.get("status"),
    }


def compare_ledger_to_v2(
    records: Sequence[LedgerRecord],
    result: Mapping[str, Any],
    progress: Mapping[str, Any],
) -> LedgerComparison:
    """Compare the replayed view with the authoritative v2 projections."""
    projection = project_records(records)
    mismatches: list[dict[str, Any]] = []

    def mismatch(field: str, projected: Any, v2: Any) -> None:
        mismatches.append({"field": field, "projected": projected, "v2": v2})

    projected_terminal = projection.terminal or {}
    for field in ("status", "success", "score"):
        if not _same_number(projected_terminal.get(field), result.get(field)):
            mismatch(
                f"terminal.{field}",
                projected_terminal.get(field),
                result.get(field),
            )

    objective = progress.get("objective_verdict")
    v2_objective_status = objective.get("status") if isinstance(objective, Mapping) else None
    if projection.objective_status != v2_objective_status:
        mismatch(
            "objective.status",
            projection.objective_status,
            v2_objective_status,
        )

    v2_last_action = progress.get("last_action")
    if _action_identity(projection.last_action) != _action_identity(v2_last_action):
        mismatch(
            "actions.last",
            _action_identity(projection.last_action),
            _action_identity(v2_last_action),
        )

    active = progress.get("active_actions")
    expected_active = active if isinstance(active, Mapping) else {}
    projected_active = {
        key: value.get("status") for key, value in projection.active_actions.items()
    }
    v2_active = {
        key: value.get("status") if isinstance(value, Mapping) else None
        for key, value in expected_active.items()
    }
    if projected_active != v2_active:
        mismatch("actions.active", projected_active, v2_active)

    budget_used = progress.get("budget_used")
    charged = budget_used if isinstance(budget_used, Mapping) else {}
    expected_budget = {
        "tokens": charged.get("tokens", 0),
        "model_calls": progress.get("model_calls", 0),
        "tool_calls": progress.get("tool_calls", 0),
        "cost_usd": charged.get("cost_usd", 0),
    }
    for key, expected in expected_budget.items():
        projected = projection.budget.get(key)
        if not _same_number(projected, expected):
            mismatch(f"budget.{key}", projected, expected)

    metrics = result.get("metrics")
    v2_submissions = metrics.get("submissions_attempted", 0) if isinstance(metrics, Mapping) else 0
    if projection.submissions["proposed"] != v2_submissions:
        mismatch(
            "submissions.proposed",
            projection.submissions["proposed"],
            v2_submissions,
        )

    return LedgerComparison(ok=not mismatches, mismatches=tuple(mismatches), projection=projection)


def write_ledger_comparison(ledger_path: Path, run_dir: Path) -> dict[str, Any]:
    """Verify and compare one ledger, then publish a deterministic projection."""
    store = LedgerStore(ledger_path)
    try:
        records = store.records()
        store.verify()
    finally:
        store.close()
    result_value = json.loads(read_regular_text(run_dir / "result.json"))
    progress_value = json.loads(read_regular_text(run_dir / "progress.json"))
    if not isinstance(result_value, Mapping) or not isinstance(progress_value, Mapping):
        raise TypeError("result.json and progress.json must contain JSON objects")
    comparison = compare_ledger_to_v2(records, result_value, progress_value)
    payload = {
        "schema_version": "harness/ledger-comparison/v1",
        "ledger_store_schema": LEDGER_STORE_SCHEMA_VERSION,
        "ledger_head": records[-1].record_hash if records else GENESIS_HASH,
        **comparison.as_dict(),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ledger-comparison-", dir=run_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, run_dir / "ledger.comparison.json")
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return payload
