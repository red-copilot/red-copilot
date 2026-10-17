from __future__ import annotations

from collections.abc import Sequence

from .progress import ProgressLedger
from .verification import (
    ActionVerdict,
    AuthorizedActionVerdict,
    EvidenceRef,
    ObservationRecord,
    VerifierRegistry,
    now_utc,
)

# Backward compatible import name. The serialized verdict now has typed IDs,
# producer, capture time, execution status, world revision and evidence refs.
ActionVerification = ActionVerdict


class ActionVerifier:
    """Conservative verifier: Agent text cannot establish a verified effect.

    Only a typed verdict handed in by Harness-controlled integration code is
    considered. Agent event dictionaries never enter this channel.
    """

    def __init__(self, registry: VerifierRegistry | None = None) -> None:
        self.registry = registry or VerifierRegistry()

    def verify(
        self,
        progress: ProgressLedger,
        *,
        run_id: str,
        planned_world_revision: int | None = None,
        current_world_revision: int | None = None,
        evidence_world_revision: int | None = None,
        trusted_verdict: AuthorizedActionVerdict | None = None,
        observations: Sequence[ObservationRecord] = (),
    ) -> ActionVerdict | None:
        expected = progress.expected_observation
        actual = progress.actual_observation
        last_action = progress.last_action or {}
        action_id = last_action.get("tool_call_id")
        if not isinstance(action_id, str) or not action_id or len(action_id) > 256:
            return None

        execution_status = last_action.get("status")
        if execution_status not in {"succeeded", "failed"}:
            execution_status = "unknown"

        reasons: list[str] = []
        revision = current_world_revision
        if revision is None:
            revision = planned_world_revision if planned_world_revision is not None else 0
        trusted_revision = evidence_world_revision
        if trusted_revision is None:
            trusted_revision = current_world_revision

        world_revision_changed = (
            planned_world_revision is not None
            and current_world_revision is not None
            and planned_world_revision != current_world_revision
        )
        if world_revision_changed:
            reasons.append("world_revision_changed")

        if execution_status == "failed":
            return self._verdict(
                run_id=run_id,
                action_id=action_id,
                revision=revision,
                execution_status="failed",
                status="pending",
                expected=expected,
                actual=actual,
                reasons=("action_failed",),
            )

        verdict = (
            trusted_verdict.verdict if self.registry.is_authorized_action(trusted_verdict) else None
        )
        if verdict is not None:
            refs_match = bool(verdict.evidence) and all(
                ref.run_id == run_id
                and ref.action_id == action_id
                and ref.producer == verdict.producer
                and trusted_revision is not None
                and ref.world_revision == trusted_revision
                for ref in verdict.evidence
            )
            verdict_matches = (
                verdict.run_id == run_id
                and verdict.action_id == action_id
                and verdict.producer in {"tool_adapter", "task_verifier", "benchmark_evaluator"}
                and verdict.execution_status == "succeeded"
                and trusted_revision is not None
                and verdict.world_revision == trusted_revision
                and refs_match
            )
            if verdict_matches:
                reasons = list(verdict.replan_reasons)
                if verdict.status == "contradicted":
                    reasons.append("trusted_evidence_contradicted")
                return self._verdict(
                    run_id=run_id,
                    action_id=action_id,
                    revision=trusted_revision,
                    execution_status="succeeded",
                    status=verdict.status,
                    expected=expected,
                    actual=(verdict.actual_observation or actual),
                    evidence=verdict.evidence,
                    reasons=reasons,
                )
            has_stale_revision = trusted_revision is not None and (
                verdict.world_revision != trusted_revision
                or any(ref.world_revision != trusted_revision for ref in verdict.evidence)
            )
            if verdict.run_id == run_id and verdict.action_id == action_id and has_stale_revision:
                # Stale trusted evidence is actionable: the Agent should know
                # that what it holds no longer speaks for the current state.
                return self._verdict(
                    run_id=run_id,
                    action_id=action_id,
                    revision=trusted_revision if trusted_revision is not None else revision,
                    execution_status=execution_status,
                    status="pending",
                    expected=expected,
                    actual=verdict.actual_observation or actual,
                    reasons=("stale_evidence_revision",),
                )

        if expected is None:
            return None

        # The comparison baseline is a Harness observation, never the Agent's
        # own text. With no observation the honest answer is "unavailable": the
        # verdict is recorded, it does not ask for a replan, and the loop does
        # not spend a turn on it. This is what replaced the 656 pending verdicts
        # of the 2026-10-16 sweep, every one of them reasoned
        # `expected_observation_missing`.
        observation = self._latest_observation(observations, run_id=run_id, revision=revision)
        if observation is None:
            reasons.append("observation_unavailable")
            return self._verdict(
                run_id=run_id,
                action_id=action_id,
                revision=revision,
                execution_status=execution_status,
                status="unavailable",
                expected=expected,
                actual=actual,
                reasons=reasons,
            )

        reasons.append("observation_recorded")
        return self._verdict(
            run_id=run_id,
            action_id=action_id,
            revision=revision,
            execution_status=execution_status,
            status="pending",
            expected=expected,
            actual=observation.summary,
            observation=observation,
            reasons=reasons,
        )

    @staticmethod
    def _latest_observation(
        observations: Sequence[ObservationRecord],
        *,
        run_id: str,
        revision: int | None,
    ) -> ObservationRecord | None:
        """Newest Harness observation for this run at or before the revision.

        An observation captured after the action under review cannot speak for
        it, so the revision bound is a ceiling rather than an equality.
        """
        candidates = [
            item
            for item in observations
            if item.run_id == run_id
            and (revision is None or item.world_revision <= revision)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.world_revision, item.captured_at))
        return candidates[-1]

    @staticmethod
    def _verdict(
        *,
        run_id: str,
        action_id: str,
        revision: int,
        execution_status: str,
        status: str,
        expected: str | None,
        actual: str | None,
        evidence: tuple[EvidenceRef, ...] = (),
        reasons: tuple[str, ...] | list[str] = (),
        observation: ObservationRecord | None = None,
    ) -> ActionVerdict:
        unique_reasons = tuple(dict.fromkeys(reasons))
        return ActionVerdict(
            run_id=run_id,
            action_id=action_id,
            producer="harness_verifier",
            captured_at=now_utc(),
            world_revision=revision,
            execution_status=execution_status,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            expected_observation=expected,
            actual_observation=actual,
            observation=observation,
            evidence=evidence,
            # Only a contradicted effect, a failed execution or stale trusted
            # evidence asks the Agent to change course. "Pending" and
            # "unavailable" are records, and the sweep data shows they were the
            # bulk of the traffic that bought nothing.
            replan_required=(
                status == "contradicted"
                or "action_failed" in unique_reasons
                or "stale_evidence_revision" in unique_reasons
            ),
            replan_reasons=unique_reasons,
        )
