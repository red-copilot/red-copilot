from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .models import WorldSnapshot, is_bookkeeping_record
from .retrieval import WorldRetriever

# Offline token estimate: deterministic ASCII-equivalent heuristic with no
# vocabulary or model dependency. Four characters per token is conservative
# for English/tooling text; the estimate only bounds context assembly, never
# billing (usage accounting stays on provider telemetry).
CHARS_PER_TOKEN = 4

# Per-action lines are a bounded tail, not a full log: the 2026-10-16 projection
# rendered every command with its full provenance blob and reached ~18KB of
# bookkeeping that crowded out the findings. Semantic observations are listed in
# full (bounded by ``max_observations``); Harness bookkeeping is summarised.
RECENT_ACTION_LINES = 8


def estimate_tokens(text: str) -> int:
    """Deterministic offline token estimate used for context budgeting."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


class WorldContextBuilder:
    """Build a compact, deterministic Agent-facing projection of world state."""

    def __init__(
        self,
        *,
        max_observations: int = 20,
        max_failures: int = 10,
        max_ruled_out: int = 10,
        max_relations: int = 20,
        max_chars: int = 16000,
        token_budget: int | None = None,
        retriever: WorldRetriever | None = None,
    ) -> None:
        self.max_observations = max_observations
        self.max_failures = max_failures
        self.max_ruled_out = max_ruled_out
        self.max_relations = max_relations
        self.max_chars = max_chars
        if token_budget is not None:
            if token_budget < 1:
                raise ValueError("token_budget must be a positive number of tokens")
            self.max_chars = min(self.max_chars, token_budget * CHARS_PER_TOKEN)
        self.retriever = retriever or WorldRetriever(
            limits={
                "observations": max_observations,
                "failures": max_failures,
                "relations": max_relations,
            }
        )

    @staticmethod
    def _valid_values(collection: dict) -> list[Any]:
        return [item for item in collection.values() if item.is_valid_at()]

    @staticmethod
    def _state_meta(item: Any) -> dict:
        """Bounded metadata for the Agent-facing line.

        The full provenance record belongs in the trace and the audit, not in
        the projection: actor, event and source ids were the bulk of the ~18KB
        context the 2026-10-16 runs carried. The trust tier stays, because
        "claim" versus "evidence" is the part a decision depends on.
        """
        metadata = item.model_dump(
            mode="json",
            include={"observed_at", "valid_from", "expires_at", "supersedes", "contradicts"},
            exclude_none=True,
            exclude_defaults=True,
        )
        provenance = getattr(item, "provenance", None)
        trust = getattr(provenance, "epistemic_status", "unclassified")
        if trust != "unclassified":
            metadata["epistemic_status"] = trust
        return metadata

    @staticmethod
    def _line(prefix: str, object_id: str, type_name: str, payload: dict) -> str:
        compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"- {prefix} {object_id} [{type_name}] {compact}"

    def render(
        self,
        snapshot: WorldSnapshot,
        *,
        query: str | None = None,
        budget: Sequence[str] | None = None,
    ) -> str:
        """Render the Harness fact view and the remaining-budget header."""
        body = self._render_body(snapshot, query=query)
        if not budget:
            return body
        budget_lines = [
            f"budget: {' '.join(str(line).split())[:200]}" for line in budget if str(line).strip()
        ]
        return "\n".join(("## Budget", *budget_lines, "", body))

    def _render_body(self, snapshot: WorldSnapshot, *, query: str | None = None) -> str:
        selected = self.retriever.retrieve(snapshot, query=query)
        lines = [
            "# Red Harness World Context",
            f"revision: {snapshot.revision}",
            "",
            "## Goals",
        ]

        goals = sorted(
            selected["goals"],
            key=lambda item: (-item.priority, item.id),
        )
        if goals:
            for goal in goals:
                lines.append(
                    self._line(
                        goal.status,
                        goal.id,
                        "goal",
                        {
                            "description": goal.description,
                            "priority": goal.priority,
                            "parent_id": goal.parent_id,
                            **goal.attributes,
                            **self._state_meta(goal),
                        },
                    )
                )
        else:
            lines.append("- none")

        lines.extend(["", "## Entities"])
        entities = sorted(selected["entities"], key=lambda item: item.id)
        if entities:
            for entity in entities:
                lines.append(
                    self._line(
                        "entity",
                        entity.id,
                        entity.type,
                        {**entity.attributes, **self._state_meta(entity)},
                    )
                )
        else:
            lines.append("- none")

        lines.extend(["", "## Capabilities"])
        capabilities = sorted(selected["capabilities"], key=lambda item: item.id)
        if capabilities:
            for capability in capabilities:
                lines.append(
                    self._line(
                        "capability",
                        capability.id,
                        capability.type,
                        {
                            "subject": capability.subject,
                            "scope": capability.scope,
                            **capability.attributes,
                            **self._state_meta(capability),
                        },
                    )
                )
        else:
            lines.append("- none")

        lines.extend(["", "## Open hypotheses"])
        hypotheses = sorted(selected["hypotheses"], key=lambda item: (-item.confidence, item.id))
        hypotheses = [item for item in hypotheses if item.status in {"open", "supported"}]
        if hypotheses:
            for hypothesis in hypotheses:
                lines.append(
                    self._line(
                        hypothesis.status,
                        hypothesis.id,
                        "hypothesis",
                        {
                            "statement": hypothesis.statement,
                            "confidence": hypothesis.confidence,
                            **hypothesis.attributes,
                            **self._state_meta(hypothesis),
                        },
                    )
                )
        else:
            lines.append("- none")

        # Ruled-out paths are rendered straight from the snapshot instead of the
        # retrieval ranking: a dead end matters precisely because it is not
        # relevant to the current query, so relevance ranking would hide it.
        ruled_out = [
            item
            for item in snapshot.hypotheses.values()
            if item.status in {"refuted", "superseded"} and item.is_valid_at()
        ]
        if ruled_out:
            ruled_out.sort(key=lambda item: item.id)
            ruled_out.sort(
                key=lambda item: (
                    item.observed_at or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )
            lines.extend(["", "## Ruled out"])
            for hypothesis in ruled_out[: self.max_ruled_out]:
                lines.append(
                    self._line(
                        hypothesis.status,
                        hypothesis.id,
                        "hypothesis",
                        {
                            "statement": hypothesis.statement,
                            "confidence": hypothesis.confidence,
                            **hypothesis.attributes,
                            **self._state_meta(hypothesis),
                        },
                    )
                )
            omitted = len(ruled_out) - self.max_ruled_out
            if omitted > 0:
                lines.append(f"- ({omitted} more ruled-out hypotheses omitted)")

        lines.extend(["", "## Artifacts"])
        artifacts = sorted(selected["artifacts"], key=lambda item: item.id)
        if artifacts:
            for artifact in artifacts:
                lines.append(
                    self._line(
                        "artifact",
                        artifact.id,
                        artifact.type,
                        {"uri": artifact.uri, **artifact.attributes, **self._state_meta(artifact)},
                    )
                )
        else:
            lines.append("- none")

        # Observations come from the snapshot rather than the retrieval slice:
        # bookkeeping (one ``tool.execution`` per command) is the bulk of the
        # record and would otherwise fill the limit and hide the findings.
        all_observations = [
            item for item in self._valid_values(snapshot.observations)
        ]
        semantic = [item for item in all_observations if not is_bookkeeping_record(item)]
        bookkeeping = [item for item in all_observations if is_bookkeeping_record(item)]
        lines.extend(["", "## Observations"])
        semantic.sort(key=lambda item: item.id)
        semantic = semantic[-self.max_observations :]
        if semantic:
            for observation in semantic:
                lines.append(
                    self._line(
                        "observation",
                        observation.id,
                        observation.type,
                        {
                            "confidence": observation.confidence,
                            "source": observation.source,
                            **observation.content,
                            **self._state_meta(observation),
                        },
                    )
                )
        else:
            lines.append("- none")
        if bookkeeping:
            failed = sum(
                1
                for item in bookkeeping
                if str((item.content or {}).get("status")) == "failed"
            )
            lines.append(
                f"- ({len(bookkeeping)} tool executions omitted; {failed} failed. "
                "See Actions for the call tally.)"
            )

        lines.extend(["", "## Actions"])
        # The tally is sourced from the snapshot, not the retrieval slice, so it
        # counts what actually happened; only the tail is bounded.
        actions = sorted(self._valid_values(snapshot.actions), key=lambda item: item.id)
        if actions:
            tally = Counter(action.status for action in actions)
            lines.append(
                "- {total} recorded: {tally}".format(
                    total=len(actions),
                    tally=", ".join(
                        f"{count} {status}" for status, count in sorted(tally.items())
                    ),
                )
            )
            for action in actions[-RECENT_ACTION_LINES:]:
                payload = {"target": action.target}
                duration = (action.attributes or {}).get("duration_ms")
                if duration is not None:
                    payload["duration_ms"] = duration
                lines.append(
                    self._line(
                        action.status,
                        action.id,
                        action.type,
                        {k: v for k, v in payload.items() if v is not None}
                        | self._state_meta(action),
                    )
                )
            omitted = len(actions) - RECENT_ACTION_LINES
            if omitted > 0:
                lines.append(f"- ({omitted} earlier calls omitted; the tally above counts them)")
        else:
            lines.append("- none")

        lines.extend(["", "## Constraints"])
        constraints = sorted(selected["constraints"], key=lambda item: item.id)
        if constraints:
            for constraint in constraints:
                lines.append(
                    self._line(
                        constraint.status,
                        constraint.id,
                        constraint.type,
                        {
                            "description": constraint.description,
                            "scope": constraint.scope,
                            **constraint.attributes,
                            **self._state_meta(constraint),
                        },
                    )
                )
        else:
            lines.append("- none")

        lines.extend(["", "## Failures"])
        failures = sorted(selected["failures"], key=lambda item: item.id)
        failures = failures[-self.max_failures :]
        if failures:
            for failure in failures:
                lines.append(
                    self._line(
                        "failure",
                        failure.id,
                        failure.type,
                        {
                            "message": failure.message,
                            "action_id": failure.action_id,
                            "recoverable": failure.recoverable,
                            **failure.attributes,
                            **self._state_meta(failure),
                        },
                    )
                )
        else:
            lines.append("- none")

        lines.extend(["", "## Relations"])
        relations = sorted(selected["relations"], key=lambda item: item.id)
        relations = relations[-self.max_relations :]
        if relations:
            for relation in relations:
                lines.append(
                    self._line(
                        "relation",
                        relation.id,
                        relation.type,
                        {
                            "source": relation.source,
                            "target": relation.target,
                            **relation.attributes,
                            **self._state_meta(relation),
                        },
                    )
                )
        else:
            lines.append("- none")

        lines.extend(
            [
                "",
                (
                    "Use this as durable task state, not as ground truth. Observations and "
                    "hypotheses may be incomplete, stale, or conflicting."
                ),
            ]
        )
        rendered = "\n".join(lines) + "\n"
        if len(rendered) <= self.max_chars:
            return rendered

        marker = "[context truncated to character budget]"
        compact_lines: list[str] = []
        index = 0
        while index < len(lines):
            if (
                lines[index].startswith("## ")
                and index + 1 < len(lines)
                and lines[index + 1] == "- none"
            ):
                index += 2
                continue
            compact_lines.append(lines[index])
            index += 1

        kept: list[str] = []
        used = 0
        for line in compact_lines:
            addition = len(line) + 1
            if used + addition + len(marker) + 1 <= self.max_chars:
                kept.append(line)
                used += addition
                continue
            if line.startswith("- ") and used + len(marker) + 8 < self.max_chars:
                available = self.max_chars - used - len(marker) - 6
                kept.append(line[:available].rstrip() + " …")
                used += len(kept[-1]) + 1
            break
        kept.append(marker)
        return "\n".join(kept) + "\n"
