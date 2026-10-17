"""Agent-authored blackboard: inbox grammar, merge and graph rendering.

Three solvers never write the graph itself. Each one appends lines to its own
inbox file (``shared/inbox/<session>.md``), and the runtime merges every inbox
into one deterministic ``intent-fact-graph.md`` that is mounted back into the
containers read-only. Only the runtime writes the graph, so the merge is the
single authority for what "the other agents said" means.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

FACT = "fact"
INTENT = "intent"
QUESTION = "question"
FLAG = "flag"
NOTE = "note"
UNPARSED = "unparsed"
STRUCTURED_KINDS = (FACT, INTENT)
SIMPLE_KINDS = (QUESTION, FLAG, NOTE)

CONFIDENCE_LEVELS = ("low", "medium", "high")
CONFIDENCE_RANK = {level: index for index, level in enumerate(CONFIDENCE_LEVELS)}
INTENT_STATUSES = ("open", "doing", "done", "dropped")
INTENT_STATUS_RANK = {"dropped": 0, "open": 1, "doing": 2, "done": 3}

GRAPH_FILENAME = "intent-fact-graph.md"
INBOX_DIRNAME = "inbox"
SNAPSHOT_DIRNAME = "snapshots"

MAX_FIELD_CHARS = 600
MAX_LINE_CHARS = 2000

_LINE = re.compile(r"^(fact|intent|question|flag|note)\s*:\s*(.*)$", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Case- and whitespace-insensitive identity for dedupe and joins."""

    return _WHITESPACE.sub(" ", text.strip()).casefold()


@dataclass(frozen=True)
class ParsedLine:
    kind: str
    session: str
    round: int
    raw: str
    text: str = ""
    evidence: str = ""
    confidence: str = ""
    target: str = ""
    status: str = ""
    reason: str = ""

    @property
    def is_unparsed(self) -> bool:
        return self.kind == UNPARSED


@dataclass(frozen=True)
class InboxRead:
    session: str
    round: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FactEntry:
    text: str
    evidence: str
    confidence: str
    first_session: str
    first_round: int
    sessions: tuple[str, ...]

    @property
    def corroborated(self) -> bool:
        """True when more than one session contributed this fact."""

        return len(self.sessions) > 1


@dataclass(frozen=True)
class IntentEntry:
    text: str
    target: str
    status: str
    first_session: str
    first_round: int
    sessions: tuple[str, ...]


@dataclass(frozen=True)
class SimpleEntry:
    kind: str
    text: str
    session: str
    round: int


@dataclass(frozen=True)
class UnparsedLine:
    session: str
    round: int
    text: str
    reason: str


@dataclass(frozen=True)
class BoardSnapshot:
    round: int
    facts: tuple[FactEntry, ...]
    intents: tuple[IntentEntry, ...]
    questions: tuple[SimpleEntry, ...]
    flags: tuple[SimpleEntry, ...]
    notes: tuple[SimpleEntry, ...]
    unparsed: tuple[UnparsedLine, ...]

    def fact_by_text(self) -> dict[str, FactEntry]:
        return {normalise(fact.text): fact for fact in self.facts}

    @property
    def stats(self) -> dict[str, int]:
        return {
            "facts": len(self.facts),
            "corroborated_facts": sum(1 for fact in self.facts if fact.corroborated),
            "intents": len(self.intents),
            "open_intents": sum(1 for intent in self.intents if intent.status != "done"),
            "questions": len(self.questions),
            "flags": len(self.flags),
            "notes": len(self.notes),
            "unparsed": len(self.unparsed),
        }


def _split_fields(body: str) -> list[str]:
    return [part.strip() for part in body.split("|")]


def _clip(text: str) -> str:
    clipped = text.strip()
    return clipped if len(clipped) <= MAX_FIELD_CHARS else clipped[:MAX_FIELD_CHARS].rstrip()


def parse_line(*, session: str, round: int, raw: str) -> ParsedLine | None:
    """Parse one inbox line; ``None`` for blank lines, ``unparsed`` otherwise."""

    stripped = raw.strip()
    if not stripped:
        return None
    if len(stripped) > MAX_LINE_CHARS:
        return ParsedLine(
            kind=UNPARSED,
            session=session,
            round=round,
            raw=stripped[:MAX_LINE_CHARS],
            reason="line_too_long",
        )
    match = _LINE.match(stripped)
    if match is None:
        return ParsedLine(
            kind=UNPARSED, session=session, round=round, raw=stripped, reason="unknown_kind"
        )
    kind = match.group(1).lower()
    body = match.group(2).strip()
    if not body:
        return ParsedLine(kind=UNPARSED, session=session, round=round, raw=stripped, reason="empty")
    if kind in SIMPLE_KINDS:
        return ParsedLine(kind=kind, session=session, round=round, raw=stripped, text=_clip(body))

    parts = _split_fields(body)
    text = _clip(parts[0])
    fields: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition(":")
        if not separator:
            return ParsedLine(
                kind=UNPARSED,
                session=session,
                round=round,
                raw=stripped,
                reason="malformed_field",
            )
        fields[key.strip().lower()] = value.strip()
    if not text:
        return ParsedLine(kind=UNPARSED, session=session, round=round, raw=stripped, reason="no_text")

    if kind == FACT:
        confidence = fields.get("confidence", "").lower()
        if confidence not in CONFIDENCE_RANK:
            return ParsedLine(
                kind=UNPARSED,
                session=session,
                round=round,
                raw=stripped,
                reason="bad_confidence",
            )
        return ParsedLine(
            kind=FACT,
            session=session,
            round=round,
            raw=stripped,
            text=text,
            evidence=_clip(fields.get("evidence", "")) or "-",
            confidence=confidence,
        )

    status = fields.get("status", "").lower()
    if status not in INTENT_STATUS_RANK:
        return ParsedLine(
            kind=UNPARSED,
            session=session,
            round=round,
            raw=stripped,
            reason="bad_intent_status",
        )
    target = _clip(fields.get("target", "")) or "-"
    return ParsedLine(
        kind=INTENT,
        session=session,
        round=round,
        raw=stripped,
        text=text,
        target=target,
        status=status,
    )


def read_inbox(
    *,
    session: str,
    path: Path,
    round: int,
) -> InboxRead:
    """Read one inbox file in full; the whole file is the session's position."""

    if not path.exists():
        return InboxRead(session=session, round=round, lines=())
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return InboxRead(session=session, round=round, lines=())
    return InboxRead(session=session, round=round, lines=tuple(text.splitlines()))


def merge_reads(reads: Sequence[InboxRead], *, round: int) -> BoardSnapshot:
    """Merge every inbox into one deterministic board snapshot."""

    facts: dict[str, FactEntry] = {}
    intends: dict[tuple[str, str], IntentEntry] = {}
    simples: dict[tuple[str, str], SimpleEntry] = {}
    unparsed: list[UnparsedLine] = []

    for read in sorted(reads, key=lambda item: item.session):
        for raw in read.lines:
            parsed = parse_line(session=read.session, round=read.round, raw=raw)
            if parsed is None:
                continue
            if parsed.is_unparsed:
                unparsed.append(
                    UnparsedLine(
                        session=parsed.session,
                        round=parsed.round,
                        text=parsed.raw,
                        reason=parsed.reason,
                    )
                )
                continue
            if parsed.kind == FACT:
                _merge_fact(facts, parsed)
            elif parsed.kind == INTENT:
                _merge_intent(intends, parsed)
            else:
                key = (parsed.kind, normalise(parsed.text))
                simples.setdefault(
                    key,
                    SimpleEntry(
                        kind=parsed.kind,
                        text=parsed.text,
                        session=parsed.session,
                        round=parsed.round,
                    ),
                )

    ordered_facts = tuple(
        sorted(
            facts.values(),
            key=lambda fact: (
                -len(fact.sessions),
                fact.first_round,
                fact.first_session,
                normalise(fact.text),
            ),
        )
    )
    ordered_intents = tuple(
        sorted(
            intends.values(),
            key=lambda intent: (
                intent.first_round,
                intent.first_session,
                normalise(intent.text),
                intent.target,
            ),
        )
    )
    return BoardSnapshot(
        round=round,
        facts=ordered_facts,
        intents=ordered_intents,
        questions=_simple(simples, QUESTION),
        flags=_simple(simples, FLAG),
        notes=_simple(simples, NOTE),
        unparsed=tuple(
            sorted(unparsed, key=lambda item: (item.session, item.round, item.text))
        ),
    )


def _simple(simples: Mapping[tuple[str, str], SimpleEntry], kind: str) -> tuple[SimpleEntry, ...]:
    return tuple(
        sorted(
            (entry for key, entry in simples.items() if key[0] == kind),
            key=lambda entry: (entry.round, entry.session, normalise(entry.text)),
        )
    )


def _merge_fact(facts: dict[str, FactEntry], parsed: ParsedLine) -> None:
    key = normalise(parsed.text)
    existing = facts.get(key)
    if existing is None:
        facts[key] = FactEntry(
            text=parsed.text,
            evidence=parsed.evidence,
            confidence=parsed.confidence,
            first_session=parsed.session,
            first_round=parsed.round,
            sessions=(parsed.session,),
        )
        return
    sessions = tuple(sorted({*existing.sessions, parsed.session}))
    # Keep the most confident statement; ties go to the earlier contributor.
    take_new = CONFIDENCE_RANK[parsed.confidence] > CONFIDENCE_RANK[existing.confidence]
    facts[key] = FactEntry(
        text=parsed.text if take_new else existing.text,
        evidence=parsed.evidence if take_new else existing.evidence,
        confidence=parsed.confidence if take_new else existing.confidence,
        first_session=existing.first_session,
        first_round=existing.first_round,
        sessions=sessions,
    )


def _merge_intent(intends: dict[tuple[str, str], IntentEntry], parsed: ParsedLine) -> None:
    key = (normalise(parsed.text), parsed.target.casefold())
    existing = intends.get(key)
    if existing is None:
        intends[key] = IntentEntry(
            text=parsed.text,
            target=parsed.target,
            status=parsed.status,
            first_session=parsed.session,
            first_round=parsed.round,
            sessions=(parsed.session,),
        )
        return
    sessions = tuple(sorted({*existing.sessions, parsed.session}))
    advanced = INTENT_STATUS_RANK[parsed.status] > INTENT_STATUS_RANK[existing.status]
    intends[key] = IntentEntry(
        text=existing.text,
        target=existing.target,
        status=parsed.status if advanced else existing.status,
        first_session=existing.first_session,
        first_round=existing.first_round,
        sessions=sessions,
    )


def render_graph(
    snapshot: BoardSnapshot,
    *,
    challenge: str,
    goal_lines: Sequence[str],
    sessions: Sequence[str],
) -> str:
    """Render the single shared graph; byte-identical for identical input."""

    lines: list[str] = [
        f"# Intent-fact graph - {challenge}",
        "",
        (
            f"round {snapshot.round} | sessions: {', '.join(sorted(sessions)) or 'none'} "
            f"| facts: {len(snapshot.facts)} | intents: {len(snapshot.intents)}"
        ),
        "",
        "This file is rendered by the runtime from every solver's inbox. Read it each round;",
        "write to your own inbox file only - never edit this file.",
        "",
        "## Goal state",
    ]
    lines.extend(goal_lines or ["- (no goals recorded yet)"])
    lines.extend(["", "## Confirmed facts (ranked by how many solvers corroborate them)"])
    if snapshot.facts:
        for fact in snapshot.facts:
            count = len(fact.sessions)
            lines.append(
                f"- [{count} solver{'s' if count != 1 else ''}] {fact.text} "
                f"(evidence: {fact.evidence}; confidence: {fact.confidence}; "
                f"first: {fact.first_session} round {fact.first_round})"
            )
    else:
        lines.append("- (none yet)")
    lines.extend(["", "## Open intents"])
    open_intents = [intent for intent in snapshot.intents if intent.status in ("open", "doing")]
    if open_intents:
        for intent in open_intents:
            lines.append(
                f"- [{intent.status}] {intent.text} -> {intent.target} "
                f"(by {intent.first_session} round {intent.first_round})"
            )
    else:
        lines.append("- (none yet)")
    lines.extend(["", "## Unknowns"])
    lines.extend(
        [f"- {entry.text} ({entry.session} round {entry.round})" for entry in snapshot.questions]
        or ["- (none yet)"]
    )
    lines.extend(["", "## Flag candidates"])
    lines.extend(
        [f"- {entry.text} ({entry.session} round {entry.round})" for entry in snapshot.flags]
        or ["- (none yet)"]
    )
    lines.extend(["", "## Notes"])
    lines.extend(
        [f"- {entry.text} ({entry.session} round {entry.round})" for entry in snapshot.notes]
        or ["- (none yet)"]
    )
    lines.extend(["", "## Unparsed lines (kept verbatim)"])
    lines.extend(
        [
            f"- [{entry.session} round {entry.round}] {entry.text} (reason: {entry.reason})"
            for entry in snapshot.unparsed
        ]
        or ["- (none)"]
    )
    lines.append("")
    return "\n".join(lines)


@dataclass
class BoardWriter:
    """Publishes the rendered graph and detects anything unexpected on disk."""

    graph_path: Path
    snapshots_dir: Path
    previous_render: str | None = None
    incidents: list[dict[str, object]] = field(default_factory=list)
    renders: int = 0

    def publish(self, *, round: int, text: str) -> dict[str, object]:
        on_disk = self._read_graph()
        incident: dict[str, object] | None = None
        if self.previous_render is not None and on_disk is not None and on_disk != self.previous_render:
            kind = "clobber" if len(on_disk) < len(self.previous_render) else "modified"
            incident = {
                "round": round,
                "kind": kind,
                "bytes_before": len(self.previous_render),
                "bytes_on_disk": len(on_disk),
            }
            self.incidents.append(incident)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Truncate in place: the file is bind-mounted read-only into running
        # containers, so replacing the inode would detach their mount.
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_path.write_text(text, encoding="utf-8")
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshots_dir / f"round-{round:03d}-{digest[:8]}.md"
        snapshot.write_text(text, encoding="utf-8")
        self.previous_render = text
        self.renders += 1
        return {
            "round": round,
            "hash": digest[:8],
            "bytes": len(text.encode("utf-8")),
            "snapshot": snapshot.name,
            "incident": incident,
        }

    def _read_graph(self) -> str | None:
        try:
            if not self.graph_path.exists():
                return None
            return self.graph_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @property
    def clobber_incidents(self) -> int:
        return sum(1 for incident in self.incidents if incident.get("kind") == "clobber")


def flag_candidates(snapshot: BoardSnapshot) -> list[str]:
    """Deduplicated flag candidates in first-seen order."""

    seen: dict[str, str] = {}
    for entry in snapshot.flags:
        value = entry.text.strip().strip("`'\"")
        if not value:
            continue
        seen.setdefault(normalise(value), value)
    return list(seen.values())


def crossed_sessions(snapshot: BoardSnapshot) -> list[dict[str, object]]:
    """Facts more than one solver contributed - the coordination evidence."""

    return [
        {"text": fact.text, "sessions": list(fact.sessions), "first_session": fact.first_session}
        for fact in snapshot.facts
        if fact.corroborated
    ]


def iter_inbox_paths(inbox_dir: Path, sessions: Iterable[str]) -> list[tuple[str, Path]]:
    return [(session, inbox_dir / f"{session}.md") for session in sessions]
