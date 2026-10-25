"""Narrow write port between trace recording and the causal ledger."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..ledger import LedgerRecord, LedgerStore


class LedgerAppender(Protocol):
    """Append-only ledger capabilities required by the trace recorder."""

    path: Path

    def append_trace_event(self, event: Mapping[str, Any]) -> LedgerRecord: ...

    def append_batch(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[LedgerRecord, ...]: ...

    def records(self) -> list[LedgerRecord]: ...

    def verify(self) -> None: ...

    def close(self) -> None: ...


class SQLiteLedgerAppender:
    """Delegate the kernel write port to one append-only SQLite store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._store = LedgerStore(self.path)

    def append_trace_event(self, event: Mapping[str, Any]) -> LedgerRecord:
        return self._store.append_trace_event(event)

    def append_batch(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[LedgerRecord, ...]:
        return self._store.append_batch(events)

    def records(self) -> list[LedgerRecord]:
        return self._store.records()

    def verify(self) -> None:
        self._store.verify()

    def close(self) -> None:
        self._store.close()
