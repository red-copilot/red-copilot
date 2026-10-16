"""Deterministic focus set: the bounded working set for the next solver round.

The focus set is a derived projection of authoritative World state. It selects a
small, goal-relevant subset of established facts and open unknowns so the next
Agent round starts from a narrowed working set instead of the full projection.
It never mutates World state, is recomputed on demand, and stays deterministic
so the same snapshot always yields the same focus.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import KIND_COLLECTIONS, TRUST_RANK, WorldSnapshot

DEFAULT_FOCUS_CLAIM_THRESHOLD = 0.7
DEFAULT_MAX_ESTABLISHED = 8
DEFAULT_MAX_UNKNOWNS = 5

_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
# Record kinds that can be treated as established facts of the current state.
_ESTABLISHED_KINDS = ("observation", "capability", "artifact", "entity", "relation")
# Record kinds that represent an open question the next round could resolve.
_UNKNOWN_KINDS = ("hypothesis", "constraint", "failure")
# Below-threshold claims are not established facts, but they are still worth
# surfacing: they are unverified leads the next round can confirm or discard.
_LEAD_KINDS = _ESTABLISHED_KINDS
# Only records the Harness or an independent producer authored can displace a
# record from the established set. Agent claims never displace established facts.
_DISPLACING_STATUSES = frozenset({"evidence", "verified"})
_UNKNOWN_KIND_WEIGHT = {"constraint": 1.5, "hypothesis": 1.0, "failure": 0.8}
_LEAD_KIND_WEIGHT = {
    "observation": 1.2,
    "capability": 1.2,
    "entity": 1.0,
    "artifact": 1.0,
    "relation": 0.8,
}


class FocusRequest(BaseModel):
    """Immutable inputs that scope the working set for one solver round."""

    model_config = ConfigDict(frozen=True)

    reference: str = ""
    subgoal: str | None = None
    claim_threshold: float = Field(default=DEFAULT_FOCUS_CLAIM_THRESHOLD, ge=0.0, le=1.0)
    max_established: int = Field(default=DEFAULT_MAX_ESTABLISHED, ge=1)
    max_unknowns: int = Field(default=DEFAULT_MAX_UNKNOWNS, ge=1)


class FocusItem(BaseModel):
    """One ranked record inside the focus set."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    type: str
    label: str
    epistemic_status: str = "unclassified"
    scope: str | None = None
    confidence: float | None = None
    score: float = 0.0
    reason: str = ""


class FocusSet(BaseModel):
    """The derived working set for one solver round."""

    model_config = ConfigDict(frozen=True)

    reference: str = ""
    subgoal: str | None = None
    established: list[FocusItem] = Field(default_factory=list)
    unknowns: list[FocusItem] = Field(default_factory=list)
    next_focus: FocusItem | None = None
    missing: list[str] = Field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text)}


def _compact(value: Any) -> str:
    if not value:
        return ""
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        text = str(value)
    return text[:120]


def _label(kind: str, record: Any) -> str:
    if kind == "observation":
        text = f"{record.type} {_compact(record.content)}"
    elif kind == "entity":
        text = f"{record.type} {_compact(record.attributes)}"
    elif kind == "capability":
        text = record.type + (f" scope={record.scope}" if record.scope else "")
    elif kind == "artifact":
        text = record.type + (f" uri={record.uri}" if record.uri else "")
    elif kind == "relation":
        text = f"{record.type} {record.source}->{record.target}"
    elif kind == "hypothesis":
        text = record.statement
    elif kind == "constraint":
        text = record.description
    elif kind == "failure":
        text = record.message
    else:
        text = getattr(record, "type", kind)
    return " ".join(str(text).split())[:160]


def _search_text(kind: str, record: Any) -> str:
    scope = getattr(record, "scope", None) or ""
    type_name = getattr(record, "type", kind)
    return f"{kind} {type_name} {scope} {_label(kind, record)}"


def _lexical(text: str, query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens & _tokens(text)) / len(query_tokens)


def _recency_bonus(snapshot: WorldSnapshot, kinds: tuple[str, ...]) -> dict[str, float]:
    dated = [
        record
        for kind in kinds
        for record in getattr(snapshot, KIND_COLLECTIONS[kind]).values()
        if getattr(record, "observed_at", None) is not None
    ]
    dated.sort(key=lambda record: (record.observed_at, record.id), reverse=True)
    if not dated:
        return {}
    denominator = max(1, len(dated) - 1)
    return {record.id: 1.0 - (index / denominator) for index, record in enumerate(dated)}


def _displaced_ids(snapshot: WorldSnapshot) -> set[str]:
    displaced: set[str] = set()
    for collection in ("observations", "capabilities", "artifacts", "entities", "relations"):
        for record in getattr(snapshot, collection).values():
            if record.provenance.epistemic_status in _DISPLACING_STATUSES:
                displaced.update(record.supersedes)
                displaced.update(record.contradicts)
    return displaced


def _focus_item(kind: str, record: Any, *, score: float, reason: str = "") -> FocusItem:
    return FocusItem(
        id=record.id,
        kind=kind,
        type=getattr(record, "type", kind),
        label=_label(kind, record),
        epistemic_status=record.provenance.epistemic_status,
        scope=getattr(record, "scope", None),
        confidence=getattr(record, "confidence", None),
        score=score,
        reason=reason,
    )


class FocusBuilder:
    """Rank established facts and open unknowns into a small working set."""

    def build(self, snapshot: WorldSnapshot, request: FocusRequest) -> FocusSet:
        query_tokens = _tokens(f"{request.reference} {request.subgoal or ''}")
        displaced = _displaced_ids(snapshot)
        established = self._established(snapshot, request, query_tokens, displaced)
        unknowns = self._unknowns(snapshot, request, query_tokens, displaced)

        missing: list[str] = []
        if not request.reference.strip():
            missing.append("reference")
        if not established:
            missing.append("established_facts")
        if not unknowns:
            missing.append("open_unknowns")

        return FocusSet(
            reference=request.reference,
            subgoal=request.subgoal,
            established=established,
            unknowns=unknowns,
            next_focus=unknowns[0] if unknowns else None,
            missing=missing,
        )

    @staticmethod
    def _established(
        snapshot: WorldSnapshot,
        request: FocusRequest,
        query_tokens: set[str],
        displaced: set[str],
    ) -> list[FocusItem]:
        recency = _recency_bonus(snapshot, _ESTABLISHED_KINDS)
        candidates: list[tuple[float, FocusItem]] = []
        for kind in _ESTABLISHED_KINDS:
            for record in getattr(snapshot, KIND_COLLECTIONS[kind]).values():
                if not record.is_valid_at() or record.id in displaced:
                    continue
                status = record.provenance.epistemic_status
                confidence = getattr(record, "confidence", None)
                # Agent-authored facts only enter the working set when they carry
                # a high enough self-reported confidence. They still render with
                # the `claim` label, so the Agent can see they are unverified.
                if status == "claim" and (
                    confidence is None or confidence < request.claim_threshold
                ):
                    continue
                score = (
                    3.0 * TRUST_RANK.get(status, 1)
                    + 5.0 * _lexical(_search_text(kind, record), query_tokens)
                    + (confidence if confidence is not None else 0.0)
                    + 0.5 * recency.get(record.id, 0.0)
                )
                candidates.append((score, _focus_item(kind, record, score=score)))
        candidates.sort(key=lambda entry: (-entry[0], entry[1].id))
        return [item for _, item in candidates[: request.max_established]]

    @staticmethod
    def _unknowns(
        snapshot: WorldSnapshot,
        request: FocusRequest,
        query_tokens: set[str],
        displaced: set[str],
    ) -> list[FocusItem]:
        recency = _recency_bonus(snapshot, (*_UNKNOWN_KINDS, *_LEAD_KINDS))
        candidates: list[tuple[float, FocusItem]] = []
        for kind in _UNKNOWN_KINDS:
            for record in getattr(snapshot, KIND_COLLECTIONS[kind]).values():
                if not record.is_valid_at():
                    continue
                status = getattr(record, "status", None)
                if kind == "hypothesis" and status != "open":
                    continue
                if kind == "constraint" and status != "active":
                    continue
                confidence = getattr(record, "confidence", None)
                uncertainty = 1.0 - confidence if isinstance(confidence, float) else 1.0
                weight = _UNKNOWN_KIND_WEIGHT.get(kind, 1.0)
                score = weight * (
                    5.0 * _lexical(_search_text(kind, record), query_tokens) + 2.0 * uncertainty
                ) + 0.5 * recency.get(record.id, 0.0)
                reason = {
                    "hypothesis": f"uncertainty={uncertainty:.2f}",
                    "constraint": "blocking constraint",
                    "failure": "failed attempt",
                }.get(kind, "")
                candidates.append(
                    (score, _focus_item(kind, record, score=score, reason=reason))
                )
        for kind in _LEAD_KINDS:
            for record in getattr(snapshot, KIND_COLLECTIONS[kind]).values():
                if not record.is_valid_at() or record.id in displaced:
                    continue
                if record.provenance.epistemic_status != "claim":
                    continue
                confidence = getattr(record, "confidence", None)
                if confidence is not None and confidence >= request.claim_threshold:
                    continue
                uncertainty = 1.0 - confidence if isinstance(confidence, float) else 1.0
                weight = _LEAD_KIND_WEIGHT.get(kind, 1.0)
                score = weight * (
                    5.0 * _lexical(_search_text(kind, record), query_tokens) + 2.0 * uncertainty
                ) + 0.5 * recency.get(record.id, 0.0)
                candidates.append(
                    (score, _focus_item(kind, record, score=score, reason="unverified lead"))
                )
        candidates.sort(key=lambda entry: (-entry[0], entry[1].id))
        return [item for _, item in candidates[: request.max_unknowns]]
