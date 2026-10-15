"""Persisted project authority and bounded Butler action dispatch.

Only Core's reviewed decisions can reach this service. A standing authority is
never a grant to approve requests, message a child, or execute arbitrary model
text. Action bodies are fixed Core templates and go through phase-1 controls.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import Field, model_validator

from .butler_oversight import (
    ButlerEventPayload,
    ButlerEvidenceRef,
    _append_project_event,
    project_state_id,
    project_stream_id,
)
from .domain import (
    ButlerAction,
    ButlerAgentReport,
    ButlerAuthorityPolicy,
    ButlerDecision,
    ButlerProjectState,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Entity,
    Engagement,
    EngagementStatus,
    NebulaModel,
    OperationEvent,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError

from .butler_precedent import read_precedents

if TYPE_CHECKING:
    from .butler_memory import ButlerMemoryService


Authority = Literal["watch", "steer", "delegate"]
DelegateAction = Literal["pause_urgent_machine_blocker"]
SUPPORTED_ACTIONS = (
    "request_parent_status",
    "request_parent_plan_reassessment",
    "pause_urgent_machine_blocker",
)


def policy_id(project_id: str) -> str:
    return "butler-authority:" + hashlib.sha256(project_id.encode()).hexdigest()


def action_id(decision_id: str, kind: str) -> str:
    return (
        "butler-action:" + hashlib.sha256(f"{decision_id}\0{kind}".encode()).hexdigest()
    )


class AuthorityWrite(NebulaModel):
    authority: Authority
    delegate_actions: list[DelegateAction] = Field(default_factory=list, max_length=1)
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def grant_requires_delegate(self) -> "AuthorityWrite":
        if self.authority != "delegate" and self.delegate_actions:
            raise ValueError("delegated actions require Delegate authority")
        if len(self.delegate_actions) != len(set(self.delegate_actions)):
            raise ValueError("delegated actions must be unique")
        return self


def _effective_actions(policy: ButlerAuthorityPolicy | None) -> list[str]:
    if policy is None or policy.authority == "watch":
        return []
    actions = ["request_parent_status", "request_parent_plan_reassessment"]
    if policy.authority == "delegate":
        actions.extend(policy.delegate_actions)
    return actions


def authority_view(project_id: str, policy: ButlerAuthorityPolicy | None) -> dict:
    return {
        "project_id": project_id,
        "authority": policy.authority if policy else "watch",
        "delegate_actions": list(policy.delegate_actions) if policy else [],
        "revision": policy.revision if policy else 0,
        "supported_actions": list(SUPPORTED_ACTIONS),
        "effective_actions": _effective_actions(policy),
    }


EntityT = TypeVar("EntityT", bound=Entity)


def _entity(transaction: Any, model: type[EntityT], entity_id: str) -> EntityT | None:
    from .database import EntityRow

    row = transaction.session.get(EntityRow, entity_id)
    if row is None:
        return None
    if row.kind != model.entity_kind:
        raise ConflictError("Butler action entity kind changed")
    return model.model_validate(row.payload)


class ButlerAuthorityService:
    """One project lease owns action planning; durable rows survive restart."""

    def __init__(
        self,
        store: NebulaStore,
        work: Any,
        *,
        memory_service: ButlerMemoryService | None = None,
    ) -> None:
        self.store = store
        self.work = work
        self.memory_service = memory_service

    def get(self, project_id: str) -> dict:
        self.store.get(Engagement, project_id)
        return authority_view(
            project_id,
            self.store.find_entity(ButlerAuthorityPolicy, "engagement_id", project_id),
        )

    def _repeat_still_current(
        self, project_id: str, goal: ChatGoal, event_payload: ButlerEventPayload
    ) -> bool:
        refs = [ref for ref in event_payload.evidence_refs if ref.kind == "tool_call"]
        if len(refs) != 3 or any(ref.project_id != project_id for ref in refs):
            return False
        try:
            last = self.store.get(ToolCall, refs[-1].id)
        except NotFoundError:
            # diagnostic-expected: missing cited evidence invalidates this pending action.
            return False
        if (
            last.engagement_id != project_id
            or last.chat_session_id != goal.session_id
            or last.status != ToolCallStatus.FAILED
        ):
            return False
        # The event is durable historical evidence. A later different, active,
        # or successful tool call means the exact repeat is no longer current.
        for call in self.store.list_session_entities(
            ToolCall, goal.session_id, newest_first=True, limit=50
        ):
            if call.id == last.id:
                return True
            if (
                call.engagement_id != project_id
                or call.status != ToolCallStatus.FAILED
                or call.tool_name != last.tool_name
                or call.arguments != last.arguments
                or call.risk_class != last.risk_class
                or (
                    isinstance(call.result, dict)
                    and call.result.get("side_effects") == "unknown"
                )
                or not call.chat_turn_id
            ):
                return False
            try:
                turn = self.store.get(ChatTurn, call.chat_turn_id)
            except NotFoundError:
                # diagnostic-expected: a missing cited turn cancels the stale request.
                return False
            if (
                turn.engagement_id != project_id
                or turn.goal_id != goal.id
                or turn.status != ChatTurnStatus.COMPLETE
            ):
                return False
        return False

    def write(self, project_id: str, request: AuthorityWrite) -> dict:
        project = self.store.get(Engagement, project_id)
        if project.status == EngagementStatus.ARCHIVED:
            raise ConflictError("Restore the project before changing Butler authority")
        with self.store.transaction(immediate=True) as transaction:
            previous = _entity(
                transaction, ButlerAuthorityPolicy, policy_id(project_id)
            )
            actual_revision = previous.revision if previous else 0
            if request.expected_revision != actual_revision:
                raise ConflictError("Butler authority changed; reload before saving")
            if previous is None:
                saved = ButlerAuthorityPolicy(
                    id=policy_id(project_id),
                    engagement_id=project_id,
                    authority=request.authority,
                    delegate_actions=request.delegate_actions,
                )
                transaction.add(saved)
            else:
                saved = transaction.update(
                    ButlerAuthorityPolicy,
                    previous.id,
                    {
                        "authority": request.authority,
                        "delegate_actions": request.delegate_actions,
                    },
                    expected_revision=previous.revision,
                )
            _append_project_event(
                transaction,
                engagement_id=project_id,
                event_type="butler.authority_changed",
                payload=ButlerEventPayload(
                    project_id=project_id,
                    source="core",
                    actor_role="core",
                    summary=f"Operator set Butler authority to {saved.authority}.",
                    metrics={
                        "authority": saved.authority,
                        "revision": saved.revision,
                        "delegate_pause_granted": (
                            "pause_urgent_machine_blocker" in saved.delegate_actions
                        ),
                    },
                    verification_state="core_observed",
                ),
                idempotency_key=f"authority:{saved.id}:revision:{saved.revision}",
                now=utc_now(),
            )
        return authority_view(project_id, saved)

    def _can_act_in_project(self, project_id: str) -> bool:
        project = self.store.get(Engagement, project_id)
        return project.status != EngagementStatus.ARCHIVED and project.work_enabled

    def _source_event(
        self, project_id: str, decision: ButlerDecision
    ) -> OperationEvent:
        events = self.store.replay_operation_events(
            project_stream_id(project_id),
            after_sequence=decision.source_sequence - 1,
            through_sequence=decision.source_sequence,
            limit=1,
        )
        if len(events) != 1 or events[0].id != decision.source_event_id:
            raise ConflictError("Butler decision source event is unavailable")
        event = events[0]
        if (
            event.engagement_id != project_id
            or event.event_type != decision.source_event_type
        ):
            raise ConflictError(
                "Butler decision source event does not match its project"
            )
        payload = ButlerEventPayload.model_validate(event.payload)
        if (
            payload.project_id != project_id
            or payload.goal_id != decision.goal_id
            or payload.session_id != decision.session_id
            or payload.actor_role != decision.actor_role
            or payload.verification_state != decision.source_verification_state
        ):
            raise ConflictError("Butler decision source does not match its goal")
        return event

    def _proposal(
        self,
        project_id: str,
        decision: ButlerDecision,
        policy: ButlerAuthorityPolicy | None,
    ) -> ButlerAction | None:
        if decision.engagement_id != project_id or decision.status != "reviewed":
            return None
        if not self._can_act_in_project(project_id):
            return None
        if not _effective_actions(policy):
            return None
        event = self._source_event(project_id, decision)
        # A standing grant applies to subsequent observations only. Replaying
        # old Watch decisions after an upgrade or changing authority cannot
        # authorize a previously observed event retroactively.
        if policy is None or event.occurred_at < policy.updated_at:
            return None
        payload = ButlerEventPayload.model_validate(event.payload)
        goal = self.store.get(ChatGoal, decision.goal_id)
        session = self.store.get(ChatSession, decision.session_id)
        if (
            goal.engagement_id != project_id
            or session.engagement_id != project_id
            or goal.session_id != session.id
            or goal.status != ChatGoalStatus.RUNNING
        ):
            return None
        kind: (
            Literal[
                "request_parent_status",
                "request_parent_plan_reassessment",
                "pause_urgent_machine_blocker",
            ]
            | None
        ) = None
        report_id: str | None = None
        if (
            event.event_type == "goal.progress_overdue"
            and decision.choice in {"ask", "steer"}
            and payload.verification_state == "core_observed"
        ):
            kind = "request_parent_status"
        elif (
            event.event_type == "goal.plan_changed"
            and decision.choice == "steer"
            and payload.verification_state == "core_observed"
            and payload.metrics.get("plan_changed") is True
            and payload.metrics.get("goal_revision") == goal.revision
            and len(decision.why_now.strip()) >= 20
            and any(
                ref.get("kind") == "goal"
                and ref.get("id") == goal.id
                and ref.get("project_id") == project_id
                for ref in decision.evidence_refs
            )
        ):
            kind = "request_parent_plan_reassessment"
        elif (
            event.event_type == "goal.read_only_tool_failures_repeated"
            and decision.choice == "steer"
            and payload.verification_state == "core_observed"
            and payload.metrics.get("attempts") == 3
            and payload.metrics.get("goal_revision") == goal.revision
            and len(decision.why_now.strip()) >= 20
            and any(
                ref.get("kind") == "tool_call"
                and ref.get("project_id") == project_id
                and any(
                    source.kind == "tool_call" and source.id == ref.get("id")
                    for source in payload.evidence_refs
                )
                for ref in decision.evidence_refs
            )
            and self._repeat_still_current(project_id, goal, payload)
        ):
            kind = "request_parent_plan_reassessment"
        elif (
            event.event_type == "agent.attention_reported"
            and decision.choice == "steer"
            and payload.verification_state == "agent_reported"
            and decision.actor_role == "main_agent"
            and policy is not None
            and policy.authority == "delegate"
            and "pause_urgent_machine_blocker" in policy.delegate_actions
        ):
            refs = [ref for ref in payload.evidence_refs if ref.kind == "agent_report"]
            if len(refs) != 1:
                return None
            report = self.store.get(ButlerAgentReport, refs[0].id)
            if (
                report.engagement_id != project_id
                or report.goal_id != goal.id
                or report.session_id != session.id
                or report.report_type != "attention"
                or report.details.get("condition")
                not in {"machine_down", "machine_unreachable"}
                or report.details.get("urgency") != "urgent"
            ):
                return None
            kind = "pause_urgent_machine_blocker"
            report_id = report.id
        if kind is None:
            return None
        return ButlerAction(
            id=action_id(decision.id, kind),
            engagement_id=project_id,
            decision_id=decision.id,
            source_event_id=event.id,
            goal_id=goal.id,
            source_session_id=session.id,
            action_type=kind,
            status="planned",
            idempotency_key=action_id(decision.id, kind),
            report_id=report_id,
            verification_state=payload.verification_state,
            summary=(
                "Ask the parent main agent for a progress checkpoint."
                if kind == "request_parent_status"
                else (
                    "Ask the parent main agent to reassess the repeated read-only tool failure."
                    if event.event_type == "goal.read_only_tool_failures_repeated"
                    else "Ask the parent main agent to reassess the changed goal plan."
                    if kind == "request_parent_plan_reassessment"
                    else "Pause the main goal after an urgent agent-reported machine blocker."
                )
            ),
        )

    def plan_next(self, project_id: str, worker_id: str) -> bool:
        state = self.store.find_entity(ButlerProjectState, "engagement_id", project_id)
        if (
            state is None
            or state.lease_owner != worker_id
            or not state.lease_expires_at
            or state.lease_expires_at <= utc_now()
        ):
            return False
        events = self.store.replay_operation_events(
            project_stream_id(project_id),
            after_sequence=state.last_action_event_sequence,
            event_types=["butler.decision_recorded"],
            limit=1,
        )
        if not events:
            return False
        event = events[0]
        payload = ButlerEventPayload.model_validate(event.payload)
        refs = [ref for ref in payload.evidence_refs if ref.kind == "decision"]
        if (
            event.engagement_id != project_id
            or payload.project_id != project_id
            or len(refs) != 1
        ):
            raise ConflictError("invalid Butler decision event")
        decision = self.store.get(ButlerDecision, refs[0].id)
        if decision.engagement_id != project_id:
            raise ConflictError("Butler decision belongs to another project")
        policy = self.store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", project_id
        )
        proposal = self._proposal(project_id, decision, policy)
        with self.store.transaction(immediate=True) as transaction:
            current = _entity(
                transaction, ButlerProjectState, project_state_id(project_id)
            )
            if (
                current is None
                or current.lease_owner != worker_id
                or not current.lease_expires_at
                or current.lease_expires_at <= utc_now()
                or current.last_action_event_sequence >= event.sequence
            ):
                return False
            current_policy = _entity(
                transaction, ButlerAuthorityPolicy, policy_id(project_id)
            )
            if proposal is not None and (
                current_policy is None
                or policy is None
                or current_policy.revision != policy.revision
                or proposal.action_type not in _effective_actions(current_policy)
            ):
                proposal = None
            if (
                proposal is not None
                and _entity(transaction, ButlerAction, proposal.id) is None
            ):
                transaction.add(proposal)
                _append_project_event(
                    transaction,
                    engagement_id=project_id,
                    event_type="butler.action_planned",
                    payload=self._action_event_payload(proposal, "planned"),
                    idempotency_key=f"action:{proposal.id}:planned",
                    now=utc_now(),
                )
            transaction.update(
                ButlerProjectState,
                current.id,
                {"last_action_event_sequence": event.sequence},
                expected_revision=current.revision,
            )
        return True

    def _pause_reason(self, action: ButlerAction) -> str:
        return (
            f"Butler action {action.id}: paused under this project's explicit "
            "delegation after an urgent, agent-reported machine blocker. "
            "The machine status is not independently verified."
        )

    def _queue_item(self, action: ButlerAction) -> dict | None:
        if not action.target_session_id:
            return None
        queue = self.work.queue.get(action.target_session_id)
        return next(
            (item for item in queue.items if item.get("key") == action.idempotency_key),
            None,
        )

    def _action_event_payload(
        self, action: ButlerAction, status: str
    ) -> ButlerEventPayload:
        return ButlerEventPayload(
            project_id=action.engagement_id,
            goal_id=action.goal_id,
            session_id=action.source_session_id,
            source="core",
            actor_role="core",
            summary=f"Butler {action.action_type} is {status}.",
            metrics={"action_type": action.action_type, "status": status},
            verification_state="core_observed",
            evidence_refs=[
                ButlerEvidenceRef(
                    kind="action", id=action.id, project_id=action.engagement_id
                ),
                ButlerEvidenceRef(
                    kind="decision",
                    id=action.decision_id,
                    project_id=action.engagement_id,
                ),
            ],
        )

    def _cancel(self, action: ButlerAction, reason: str) -> None:
        with self.store.transaction(immediate=True) as transaction:
            current = _entity(transaction, ButlerAction, action.id)
            if current is None or current.status != "planned":
                return
            updated = transaction.update(
                ButlerAction,
                current.id,
                {
                    "status": "cancelled",
                    "outcome": {"reason": reason, "delivered": False},
                    "finished_at": utc_now(),
                },
                expected_revision=current.revision,
            )
            _append_project_event(
                transaction,
                engagement_id=updated.engagement_id,
                event_type="butler.action_cancelled",
                payload=self._action_event_payload(updated, "cancelled"),
                idempotency_key=f"action:{updated.id}:cancelled",
                now=utc_now(),
            )

    def _defer_for_memory(self, action: ButlerAction) -> None:
        """Keep a known-undelivered action retryable while memory is unavailable."""

        with self.store.transaction(immediate=True) as transaction:
            current = _entity(transaction, ButlerAction, action.id)
            if current is None or current.status != "planned":
                return
            outcome = {"reason": "memory_unavailable", "delivered": False}
            if current.outcome == outcome:
                return
            updated = transaction.update(
                ButlerAction,
                current.id,
                {"outcome": outcome},
                expected_revision=current.revision,
            )
            _append_project_event(
                transaction,
                engagement_id=updated.engagement_id,
                event_type="butler.action_waiting_memory",
                payload=self._action_event_payload(updated, "waiting for memory"),
                idempotency_key=f"action:{updated.id}:waiting-memory",
                now=utc_now(),
            )

    def _cancel_for_changed_precedent(
        self, action: ButlerAction, decision: ButlerDecision, digest: str
    ) -> None:
        """Cancel before delivery and enqueue a fresh cited semantic review."""

        source = self._source_event(action.engagement_id, decision)
        original = ButlerEventPayload.model_validate(source.payload)
        with self.store.transaction(immediate=True) as transaction:
            current = _entity(transaction, ButlerAction, action.id)
            if current is None or current.status != "planned":
                return
            updated = transaction.update(
                ButlerAction,
                current.id,
                {
                    "status": "cancelled",
                    "outcome": {
                        "reason": "precedent_changed_re_review_requested",
                        "delivered": False,
                    },
                    "finished_at": utc_now(),
                },
                expected_revision=current.revision,
            )
            _append_project_event(
                transaction,
                engagement_id=updated.engagement_id,
                event_type="butler.action_cancelled",
                payload=self._action_event_payload(updated, "cancelled"),
                idempotency_key=f"action:{updated.id}:cancelled",
                now=utc_now(),
            )
            # Preserve the original source type, actor and verification state.
            # This is a Core re-review request, never a new agent observation.
            _append_project_event(
                transaction,
                engagement_id=updated.engagement_id,
                event_type=source.event_type,
                payload=ButlerEventPayload(
                    project_id=updated.engagement_id,
                    goal_id=original.goal_id,
                    session_id=original.session_id,
                    source="core_recovery",
                    causal_event_id=source.id,
                    actor_role=original.actor_role,
                    parent_goal_id=original.parent_goal_id,
                    summary="Butler is reviewing the cited condition again because saved precedent changed.",
                    metrics={
                        **dict(list(original.metrics.items())[:15]),
                        "precedent_recheck": True,
                    },
                    verification_state=original.verification_state,
                    evidence_refs=original.evidence_refs,
                    incident_key=original.incident_key,
                ),
                idempotency_key=f"precedent-recheck:{updated.id}:{digest}",
                now=utc_now(),
            )

    def _finish(
        self, action: ButlerAction, status: str, outcome: dict[str, Any]
    ) -> None:
        with self.store.transaction(immediate=True) as transaction:
            current = _entity(transaction, ButlerAction, action.id)
            if current is None or current.status != "dispatching":
                return
            updated = transaction.update(
                ButlerAction,
                current.id,
                {"status": status, "outcome": outcome, "finished_at": utc_now()},
                expected_revision=current.revision,
            )
            _append_project_event(
                transaction,
                engagement_id=updated.engagement_id,
                event_type=f"butler.action_{status}",
                payload=self._action_event_payload(updated, status),
                idempotency_key=f"action:{updated.id}:{status}",
                now=utc_now(),
            )

    def _reconcile(self, action: ButlerAction) -> None:
        if action.action_type in {
            "request_parent_status",
            "request_parent_plan_reassessment",
        }:
            item = self._queue_item(action)
            if item is not None:
                self._finish(action, "dispatched", {"queue_item_id": item["id"]})
            else:
                self._finish(action, "uncertain", {"reason": "dispatch_interrupted"})
            return
        goal = self.store.get(ChatGoal, action.goal_id)
        if (
            goal.engagement_id == action.engagement_id
            and goal.status == ChatGoalStatus.PAUSED
            and goal.blocked_reason == self._pause_reason(action)
        ):
            self._finish(action, "applied", {"goal_id": goal.id})
        else:
            self._finish(action, "uncertain", {"reason": "dispatch_interrupted"})

    async def dispatch(self, action: ButlerAction, worker_id: str) -> None:
        if action.status == "dispatching":
            self._reconcile(action)
            return
        if action.status != "planned":
            return
        policy = self.store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", action.engagement_id
        )
        if not self._can_act_in_project(action.engagement_id):
            self._cancel(action, "project_not_active")
            return
        if action.action_type not in _effective_actions(policy):
            self._cancel(action, "authority_changed")
            return
        try:
            decision = self.store.get(ButlerDecision, action.decision_id)
            proposal = self._proposal(action.engagement_id, decision, policy)
        except (NotFoundError, ConflictError):
            # diagnostic-expected: source changes cancel the saved action below.
            proposal = None
        if (
            proposal is None
            or proposal.id != action.id
            or proposal.action_type != action.action_type
        ):
            self._cancel(action, "source_changed")
            return
        if self.memory_service is not None:
            try:
                current_precedent = read_precedents(
                    self.memory_service,
                    action.engagement_id,
                    decision.source_event_type,
                )
            except Exception:
                # diagnostic-expected: a failed precedent read durably defers this action.
                self._defer_for_memory(action)
                return
            if decision.precedent_digest != current_precedent.digest:
                self._cancel_for_changed_precedent(
                    action, decision, current_precedent.digest
                )
                return
        goal = self.store.get(ChatGoal, action.goal_id)
        detail = self.work.chat_detail(action.engagement_id, action.source_session_id)
        target = detail.get("message_via_session_id")
        if action.action_type in {
            "request_parent_status",
            "request_parent_plan_reassessment",
        }:
            if not target:
                self._cancel(action, "main_agent_unavailable")
                return
            target_detail = self.work.chat_detail(action.engagement_id, target)
            if target_detail.get("kind") != "main":
                self._cancel(action, "main_agent_unavailable")
                return
        else:
            if detail.get("kind") != "main" or goal.parent_goal_id:
                self._cancel(action, "main_goal_required")
                return
            target = goal.session_id
        with self.store.transaction(immediate=True) as transaction:
            state = _entity(
                transaction, ButlerProjectState, project_state_id(action.engagement_id)
            )
            current = _entity(transaction, ButlerAction, action.id)
            current_policy = _entity(
                transaction, ButlerAuthorityPolicy, policy_id(action.engagement_id)
            )
            if (
                state is None
                or state.lease_owner != worker_id
                or not state.lease_expires_at
                or state.lease_expires_at <= utc_now()
                or current is None
                or current.status != "planned"
                or current_policy is None
                or policy is None
                or current_policy.revision != policy.revision
                or action.action_type not in _effective_actions(current_policy)
            ):
                return
            action = transaction.update(
                ButlerAction,
                current.id,
                {
                    "status": "dispatching",
                    "target_session_id": target,
                    "started_at": utc_now(),
                },
                expected_revision=current.revision,
            )
        try:
            if action.action_type in {
                "request_parent_status",
                "request_parent_plan_reassessment",
            }:
                subject = (
                    "a child goal" if decision.actor_role == "subagent" else "the goal"
                )
                if action.action_type == "request_parent_status":
                    text = (
                        f"Butler observed that {subject} missed a scheduled progress checkpoint "
                        f"(goal {goal.id}). Please report what changed, evidence gained, "
                        "the next step, blockers, and when the next checkpoint is expected. "
                        "Keep the work within its existing objective and approval boundaries."
                    )
                else:
                    if (
                        decision.source_event_type
                        == "goal.read_only_tool_failures_repeated"
                    ):
                        text = (
                            f"Butler observed that the same read-only tool invocation for {subject} "
                            f"(goal {goal.id}) failed in three separate completed turns. "
                            "Inspect the saved tool outcomes before repeating that exact call. "
                            "Choose a bounded alternative or explain why another attempt is warranted, "
                            "then report the next checkpoint to Core. This does not establish that "
                            "the wider goal failed. Route any child guidance through the parent main "
                            "agent and keep existing approval boundaries."
                        )
                    else:
                        text = (
                            f"Butler reviewed a saved plan change for {subject} (goal {goal.id}). "
                            "Before continuing, compare the current plan with the existing "
                            "objective and evidence. If a step is repeating without new evidence, "
                            "stop that step and choose one bounded alternative. Report the revised "
                            "next step and checkpoint to Core. Route any child guidance through "
                            "the parent main agent and keep existing approval boundaries."
                        )
                await self.work.message_chat(
                    action.engagement_id,
                    {"session_id": target, "text": text, "delivery": "next"},
                    idempotency_key=action.idempotency_key,
                )
                item = self._queue_item(action)
                if item is None:
                    raise ConflictError("queued Butler request is not visible")
                self._finish(action, "dispatched", {"queue_item_id": item["id"]})
            else:
                await self.work.goal_action(
                    action.engagement_id,
                    {
                        "session_id": goal.session_id,
                        "expected_revision": goal.revision,
                        "action": "pause",
                        "reason": self._pause_reason(action),
                    },
                )
                self._reconcile(action)
        except Exception:
            # The control may have committed immediately before the exception.
            # diagnostic-expected: reconcile records the observed outcome or uncertainty;
            # never blindly retry an uncertain external side effect.
            self._reconcile(action)

    async def process_project(self, project_id: str, worker_id: str) -> None:
        for _ in range(20):
            if not self.plan_next(project_id, worker_id):
                break
        actions = self.store.find_entities(
            ButlerAction,
            {"status": ["planned", "dispatching"]},
            engagement_id=project_id,
            limit=20,
        )
        for action in actions:
            await self.dispatch(action, worker_id)


def authority_router(service: ButlerAuthorityService) -> APIRouter:
    router = APIRouter(tags=["butler"])

    @router.get("/engagements/{project_id}/butler/authority")
    def get_authority(project_id: str) -> dict:
        return service.get(project_id)

    @router.put("/engagements/{project_id}/butler/authority")
    def put_authority(project_id: str, request: AuthorityWrite) -> dict:
        try:
            return service.write(project_id, request)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
