from __future__ import annotations

import json
from typing import Any

from .focus import DEFAULT_FOCUS_CLAIM_THRESHOLD, FocusBuilder, FocusItem, FocusRequest, FocusSet
from .models import WorldSnapshot
from .retrieval import WorldRetriever


def _clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def _focus_line(item: FocusItem, *, with_reason: bool = False) -> str:
    confidence = f" conf={item.confidence:.2f}" if item.confidence is not None else ""
    reason = f" ({item.reason})" if with_reason and item.reason else ""
    return (
        f"- [{item.epistemic_status}] {item.kind}:{item.type} {item.label}"
        f"{confidence}{reason}"
    )


class WorldContextBuilder:
    """Build a compact, deterministic Agent-facing projection of world state."""

    def __init__(
        self,
        *,
        max_observations: int = 20,
        max_failures: int = 10,
        max_relations: int = 20,
        max_chars: int = 16000,
        focus_max_chars: int = 2000,
        focus_claim_threshold: float = DEFAULT_FOCUS_CLAIM_THRESHOLD,
        focus_enabled: bool = True,
        retriever: WorldRetriever | None = None,
        focus_builder: FocusBuilder | None = None,
    ) -> None:
        self.max_observations = max_observations
        self.max_failures = max_failures
        self.max_relations = max_relations
        self.max_chars = max_chars
        self.focus_max_chars = focus_max_chars
        self.focus_claim_threshold = focus_claim_threshold
        self.focus_enabled = focus_enabled
        self.focus_builder = focus_builder or FocusBuilder()
        self.retriever = retriever or WorldRetriever(
            limits={
                "observations": max_observations,
                "failures": max_failures,
                "relations": max_relations,
            }
        )

    def build_focus(
        self,
        snapshot: WorldSnapshot,
        *,
        reference: str,
        subgoal: str | None = None,
    ) -> FocusSet:
        """Derive the bounded working set for one solver round."""
        return self.focus_builder.build(
            snapshot,
            FocusRequest(
                reference=reference,
                subgoal=subgoal,
                claim_threshold=self.focus_claim_threshold,
            ),
        )

    @staticmethod
    def _valid_values(collection: dict) -> list[Any]:
        return [item for item in collection.values() if item.is_valid_at()]

    @staticmethod
    def _state_meta(item: Any) -> dict:
        metadata = item.model_dump(
            mode="json",
            include={
                "provenance",
                "observed_at",
                "valid_from",
                "expires_at",
                "supersedes",
                "contradicts",
            },
            exclude_none=True,
            exclude_defaults=True,
        )
        provenance = getattr(item, "provenance", None)
        trust = getattr(provenance, "epistemic_status", "unclassified")
        if trust != "unclassified":
            metadata.setdefault("provenance", {})["epistemic_status"] = trust
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
        focus: FocusSet | None = None,
    ) -> str:
        """Render the Agent context, optionally prefixed by a bounded focus block.

        The focus block is rendered first and owns its own character budget, so
        it can never be truncated away by the wider state projection. When
        ``focus`` is ``None`` or focus rendering is disabled the output is
        byte-identical to the body-only projection.
        """
        body = self._render_body(snapshot, query=query)
        if focus is None or not self.focus_enabled:
            return body
        return self._render_focus(focus) + "\n" + body

    def _render_focus(self, focus: FocusSet) -> str:
        budget = max(1, self.focus_max_chars)
        marker = "[focus truncated to character budget]"
        lines: list[str] = []
        used = 0
        truncated = False

        def add(line: str, *, is_marker: bool = False) -> bool:
            nonlocal used, truncated
            addition = len(line) + 1
            reserve = 0 if is_marker else len(marker) + 1
            if used + addition + reserve > budget:
                if not is_marker:
                    truncated = True
                return False
            lines.append(line)
            used += addition
            return True

        def add_section(title: str, items: list[FocusItem]) -> None:
            if not add("") or not add(f"### {title}"):
                return
            if not items:
                add("- none")
                return
            for item in items:
                if not add(_focus_line(item)):
                    return

        add("## Focus")
        add(f"reference: {_clip(focus.reference, 300) or 'none'}")
        add(f"subgoal: {_clip(focus.subgoal, 300) if focus.subgoal else 'none'}")
        add_section("Established facts", focus.established)
        add_section("Open unknowns", focus.unknowns)
        if add("") and add("### Next focus"):
            if focus.next_focus is None:
                add("- none")
            else:
                add(_focus_line(focus.next_focus, with_reason=True))
        if focus.missing:
            add("")
            add("missing: " + ", ".join(focus.missing))
        if truncated:
            add(marker, is_marker=True)
        return "\n".join(lines) + "\n"

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

        lines.extend(["", "## Observations"])
        observations = sorted(selected["observations"], key=lambda item: item.id)
        observations = observations[-self.max_observations :]
        if observations:
            for observation in observations:
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

        lines.extend(["", "## Actions"])
        actions = sorted(selected["actions"], key=lambda item: item.id)
        if actions:
            for action in actions:
                lines.append(
                    self._line(
                        action.status,
                        action.id,
                        action.type,
                        {"target": action.target, **action.attributes, **self._state_meta(action)},
                    )
                )
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
