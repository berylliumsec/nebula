"""Durable project-scoped Watch foundation for conversation goals.

Core records goal transitions before waking this worker. The immutable project
stream is an operation-event stream whose operation ID is derived from the
project ID; no chat transcript or raw tool result is copied into its packets.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from contextlib import suppress
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field, field_validator, model_validator

from .database import EntityRow
from .diagnostics import create_diagnostic_task, record_caught_exception
from .domain import (
    Artifact,
    ButlerAccountCredit,
    ButlerAction,
    ButlerAgentReport,
    ButlerAuthorityPolicy,
    ButlerDecision,
    ButlerGoalWatch,
    ButlerHarnessQuota,
    ButlerIncident,
    ButlerProjectState,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessTurn,
    HarnessProfile,
    NebulaModel,
    OperationEvent,
    ProviderProfile,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError

if TYPE_CHECKING:
    from .butler_memory import ButlerMemoryService
    from .butler_review import ButlerReviewer
    from .storage import StoreTransaction


PROJECT_STREAM_KIND = "butler_project"
PROGRESS_INTERVAL = timedelta(minutes=30)
MAX_CHECKPOINT_INTERVAL = timedelta(hours=2)
LEASE_DURATION = timedelta(seconds=60)
OVERVIEW_PAGE_SIZE = 50


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def project_stream_id(engagement_id: str) -> str:
    return _stable_id("butler-project", engagement_id)


def watch_id(goal_id: str) -> str:
    return _stable_id("butler-watch", goal_id)


def project_state_id(engagement_id: str) -> str:
    return _stable_id("butler-state", engagement_id)


def incident_id(engagement_id: str, incident_key: str) -> str:
    return _stable_id("butler-incident", f"{engagement_id}\0{incident_key}")


def _progress_incident_key(goal_id: str) -> str:
    return f"goal-progress-overdue:{goal_id}"


class ButlerEvidenceRef(NebulaModel):
    kind: Literal[
        "goal",
        "chat_turn",
        "harness_turn",
        "artifact",
        "resource",
        "agent_report",
        "incident",
        "decision",
        "action",
    ]
    id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)


class ButlerEventPayload(NebulaModel):
    schema_version: Literal["nebula.butler-event/v1"] = "nebula.butler-event/v1"
    project_id: str = Field(min_length=1, max_length=200)
    goal_id: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    source: Literal[
        "core",
        "core_recovery",
        "agent",
        "harness",
        "provider",
        "openrouter_credits_api",
    ]
    causal_event_id: str | None = Field(default=None, max_length=200)
    actor_id: str | None = Field(default=None, max_length=200)
    actor_role: Literal["main_agent", "subagent", "core"]
    parent_goal_id: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=1, max_length=500)
    metrics: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    verification_state: Literal[
        "core_observed",
        "agent_reported",
        "provider_observed",
        "vendor_reported",
        "verified",
        "unknown",
    ]
    evidence_refs: list[ButlerEvidenceRef] = Field(default_factory=list, max_length=10)
    incident_key: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def bounded_project_packet(self) -> "ButlerEventPayload":
        if any(ref.project_id != self.project_id for ref in self.evidence_refs):
            raise ValueError("event evidence must belong to its project")
        if len(self.metrics) > 16 or any(
            len(key) > 80 or isinstance(value, str) and len(value) > 200
            for key, value in self.metrics.items()
        ):
            raise ValueError("event metrics exceed the compact packet limit")
        return self


class ReportEvidenceRef(NebulaModel):
    """An agent-selected reference; Core resolves its project before storage."""

    kind: Literal["goal", "chat_turn", "harness_turn", "artifact"]
    id: str = Field(min_length=1, max_length=200)


class GoalProgressReport(NebulaModel):
    what_changed: str = Field(min_length=1, max_length=500)
    evidence_gained: list[str] = Field(min_length=1, max_length=8)
    criterion_advanced: str = Field(min_length=1, max_length=300)
    next_step: str = Field(min_length=1, max_length=500)
    blockers: list[str] = Field(default_factory=list, max_length=8)
    next_checkpoint_at: datetime | None = None
    evidence_refs: list[ReportEvidenceRef] = Field(default_factory=list, max_length=8)

    @field_validator("evidence_gained", "blockers")
    @classmethod
    def bounded_lines(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("report entries must contain 1 to 300 characters")
        return values

    @field_validator("next_checkpoint_at")
    @classmethod
    def checkpoint_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("next checkpoint must include a timezone")
        return value


class GoalAttentionReport(NebulaModel):
    condition: Literal["machine_unreachable", "machine_down", "other"]
    machine: str | None = Field(default=None, max_length=253)
    symptom: str = Field(min_length=1, max_length=500)
    impact: str = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(min_length=1, max_length=8)
    attempted_recovery: str = Field(min_length=1, max_length=500)
    urgency: Literal["routine", "soon", "urgent"]
    help_requested: str = Field(min_length=1, max_length=500)
    evidence_refs: list[ReportEvidenceRef] = Field(default_factory=list, max_length=8)

    @field_validator("evidence")
    @classmethod
    def bounded_evidence(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("report evidence must contain 1 to 300 characters")
        return values

    @model_validator(mode="after")
    def machine_condition_has_identity(self) -> "GoalAttentionReport":
        if self.condition in {"machine_unreachable", "machine_down"}:
            if not self.machine:
                raise ValueError("machine reports require a hostname or IP address")
            normalize_machine(self.machine)
        return self


class IncidentAcknowledgeRequest(NebulaModel):
    expected_revision: int = Field(ge=1)


def normalize_machine(value: str) -> str:
    """Canonicalize only explicit host/IP identity, without network lookups."""

    candidate = value.strip().lower().rstrip(".")
    address = (
        candidate[1:-1]
        if candidate.startswith("[") and candidate.endswith("]")
        else candidate
    )
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        pass
    if len(candidate) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
        candidate,
    ):
        raise ValueError("machine must be a hostname or IP address")
    return candidate


def _entity_in_transaction(transaction: StoreTransaction, model: type, entity_id: str):
    row = transaction.session.get(EntityRow, entity_id)
    if row is None:
        return None
    if row.kind != model.entity_kind:
        raise ConflictError(f"entity {entity_id} has an unexpected kind")
    return model.model_validate(row.payload)


def _coverage_reason() -> str:
    return (
        "Validated progress reports and turn-output review are not connected yet; "
        "Watch currently sees goal lifecycle only."
    )


def _ensure_project_state(transaction: StoreTransaction, engagement_id: str) -> None:
    if (
        _entity_in_transaction(
            transaction, ButlerProjectState, project_state_id(engagement_id)
        )
        is None
    ):
        transaction.add(
            ButlerProjectState(
                id=project_state_id(engagement_id), engagement_id=engagement_id
            )
        )


def _watch_for_goal(
    goal: ChatGoal, *, now: datetime, recovered: bool = False
) -> ButlerGoalWatch:
    running = goal.status == ChatGoalStatus.RUNNING
    return ButlerGoalWatch(
        id=watch_id(goal.id),
        engagement_id=goal.engagement_id,
        goal_id=goal.id,
        session_id=goal.session_id,
        parent_goal_id=goal.parent_goal_id,
        status=goal.status,
        progress_due_at=now + PROGRESS_INTERVAL if running else None,
        deadline_generation=1 if running else 0,
        coverage_reason=(
            "Watch was restored from the current goal after Core restart; "
            "earlier goal events may be unavailable. " + _coverage_reason()
            if recovered
            else _coverage_reason()
        )[:500],
    )


def _append_project_event(
    transaction: StoreTransaction,
    *,
    engagement_id: str,
    event_type: str,
    payload: ButlerEventPayload,
    idempotency_key: str,
    now: datetime,
) -> OperationEvent:
    if payload.project_id != engagement_id:
        raise ValueError("oversight event project does not match its stream")
    stream_id = project_stream_id(engagement_id)
    transaction.lock_operation_stream(stream_id)
    _ensure_project_state(transaction, engagement_id)
    event = transaction.append_operation_event(
        stream_id,
        PROJECT_STREAM_KIND,
        engagement_id,
        event_type,
        payload.model_dump(mode="json"),
        idempotency_key=idempotency_key,
        occurred_at=now,
    )
    transaction.oversight_projects_changed.add(engagement_id)
    return event


def _resolve_progress_incident(
    transaction: StoreTransaction, *, goal: ChatGoal, reason: str, now: datetime
) -> None:
    key = _progress_incident_key(goal.id)
    record_id = incident_id(goal.engagement_id, key)
    incident = _entity_in_transaction(transaction, ButlerIncident, record_id)
    if incident is None or incident.status == "resolved":
        return
    transaction.update(
        ButlerIncident,
        incident.id,
        {"status": "resolved", "resolved_at": now},
        expected_revision=incident.revision,
    )
    _append_project_event(
        transaction,
        engagement_id=goal.engagement_id,
        event_type="incident.resolved",
        payload=ButlerEventPayload(
            project_id=goal.engagement_id,
            goal_id=goal.id,
            session_id=goal.session_id,
            source="core",
            actor_role="core",
            summary=reason,
            verification_state="core_observed",
            evidence_refs=[
                ButlerEvidenceRef(
                    kind="incident", id=incident.id, project_id=goal.engagement_id
                ),
                ButlerEvidenceRef(
                    kind="goal", id=goal.id, project_id=goal.engagement_id
                ),
            ],
            incident_key=key,
        ),
        idempotency_key=f"progress-overdue-resolved:{incident.id}:{incident.revision + 1}",
        now=now,
    )


def record_goal_transition(
    transaction: StoreTransaction, before: ChatGoal, after: ChatGoal
) -> None:
    """Record a changed goal status and its Watch in the goal write transaction."""

    if before.id != after.id or before.engagement_id != after.engagement_id:
        raise ValueError("goal transition changed identity or project")
    if before.status == after.status:
        return
    now = utc_now()
    prior = _entity_in_transaction(transaction, ButlerGoalWatch, watch_id(after.id))
    if prior is None:
        watch = _watch_for_goal(after, now=now)
        transaction.add(watch)
    else:
        running = after.status == ChatGoalStatus.RUNNING
        watch = transaction.update(
            ButlerGoalWatch,
            prior.id,
            {
                "status": after.status,
                "progress_due_at": now + PROGRESS_INTERVAL if running else None,
                "deadline_generation": (
                    prior.deadline_generation + 1
                    if running
                    else prior.deadline_generation
                ),
                "overdue_emitted_generation": None
                if running
                else prior.overdue_emitted_generation,
                "closed_at": None,
            },
            expected_revision=prior.revision,
        )
    action = (
        "started"
        if after.status == ChatGoalStatus.RUNNING
        and before.status == ChatGoalStatus.DRAFT
        else "resumed"
        if after.status == ChatGoalStatus.RUNNING
        else after.status.value
    )
    if after.status in {ChatGoalStatus.COMPLETED, ChatGoalStatus.CANCELLED}:
        _resolve_progress_incident(
            transaction,
            goal=after,
            reason="Goal stopped waiting for a progress checkpoint.",
            now=now,
        )
    payload = ButlerEventPayload(
        project_id=after.engagement_id,
        goal_id=after.id,
        session_id=after.session_id,
        source="core",
        actor_role="subagent" if after.parent_goal_id else "main_agent",
        parent_goal_id=after.parent_goal_id,
        summary=f"Goal {action}.",
        metrics={
            "goal_revision": after.revision,
            "watch_generation": watch.deadline_generation,
        },
        verification_state="core_observed",
        evidence_refs=[
            ButlerEvidenceRef(kind="goal", id=after.id, project_id=after.engagement_id)
        ],
    )
    _append_project_event(
        transaction,
        engagement_id=after.engagement_id,
        event_type=f"goal.{action}",
        payload=payload,
        idempotency_key=f"goal:{after.id}:revision:{after.revision}:status",
        now=now,
    )


def record_goal_plan_change(
    transaction: StoreTransaction, before: ChatGoal, after: ChatGoal
) -> None:
    """Wake semantic review only for a material edit to a watched goal."""

    if before.id != after.id or before.engagement_id != after.engagement_id:
        raise ValueError("goal edit changed identity or project")
    if before.status == ChatGoalStatus.DRAFT or not any(
        (
            before.objective != after.objective,
            before.completion_criteria != after.completion_criteria,
            before.plan != after.plan,
        )
    ):
        return
    now = utc_now()
    _append_project_event(
        transaction,
        engagement_id=after.engagement_id,
        event_type="goal.plan_changed",
        payload=ButlerEventPayload(
            project_id=after.engagement_id,
            goal_id=after.id,
            session_id=after.session_id,
            source="core",
            actor_role="subagent" if after.parent_goal_id else "main_agent",
            parent_goal_id=after.parent_goal_id,
            summary="Goal objective, criteria, or plan changed.",
            metrics={
                "goal_revision": after.revision,
                "objective_changed": before.objective != after.objective,
                "criteria_changed": before.completion_criteria
                != after.completion_criteria,
                "plan_changed": before.plan != after.plan,
            },
            verification_state="core_observed",
            evidence_refs=[
                ButlerEvidenceRef(
                    kind="goal", id=after.id, project_id=after.engagement_id
                )
            ],
        ),
        idempotency_key=f"goal:{after.id}:plan:{after.revision}",
        now=now,
    )


def _report_evidence(
    transaction: StoreTransaction,
    *,
    project_id: str,
    references: list[ReportEvidenceRef],
) -> list[ButlerEvidenceRef]:
    kinds = {
        "goal": ChatGoal,
        "chat_turn": ChatTurn,
        "harness_turn": HarnessTurn,
        "artifact": Artifact,
    }
    resolved: list[ButlerEvidenceRef] = []
    for reference in references:
        record = _entity_in_transaction(
            transaction, kinds[reference.kind], reference.id
        )
        if record is None or record.engagement_id != project_id:
            raise ConflictError(
                "report evidence is missing or belongs to another project"
            )
        resolved.append(
            ButlerEvidenceRef(
                kind=reference.kind, id=reference.id, project_id=project_id
            )
        )
    return resolved


def _machine_incident(
    transaction: StoreTransaction,
    *,
    goal: ChatGoal,
    report: ButlerAgentReport,
    details: GoalAttentionReport,
    evidence_refs: list[ButlerEvidenceRef],
    now: datetime,
) -> tuple[ButlerIncident, str]:
    assert details.machine is not None
    machine = normalize_machine(details.machine)
    key = f"machine:{machine}"
    record_id = incident_id(goal.engagement_id, key)
    previous = _entity_in_transaction(transaction, ButlerIncident, record_id)
    # The report's prose is retained as an attributed claim. The incident
    # heading is Core-generated so untrusted content cannot pose as a finding.
    heading = f"Agent reports {machine} is down or unreachable."

    def claim_line(label: str, value: str) -> str:
        compact = re.sub(r"\s+", " ", value).strip()
        return f"Agent-reported {label}: {compact}"[:300]

    new_evidence_summary = [
        claim_line("symptom", details.symptom),
        *(claim_line("evidence", value) for value in details.evidence[:6]),
        claim_line("recovery attempt", details.attempted_recovery),
    ]
    report_ref = ButlerEvidenceRef(
        kind="agent_report", id=report.id, project_id=goal.engagement_id
    )
    new_refs = [report_ref, *evidence_refs]
    if previous is None:
        incident = ButlerIncident(
            id=record_id,
            engagement_id=goal.engagement_id,
            incident_key=key,
            source="agent_report",
            verification_state="agent_reported",
            summary=heading,
            impact=details.impact,
            next_action=details.help_requested,
            evidence_refs=[ref.model_dump() for ref in new_refs],
            evidence_summary=new_evidence_summary,
            goal_ids=[goal.id],
            session_ids=[goal.session_id],
            observed_at=now,
        )
        transaction.add(incident)
        event_type = "incident.opened"
    else:
        prior_refs = [
            ButlerEvidenceRef.model_validate(value) for value in previous.evidence_refs
        ]
        refs = list(
            {(ref.kind, ref.id): ref for ref in [*prior_refs, *new_refs]}.values()
        )[-20:]
        incident = transaction.update(
            ButlerIncident,
            previous.id,
            {
                "status": "open" if previous.status == "resolved" else previous.status,
                "resolved_at": None,
                "acknowledged_at": (
                    None if previous.status == "resolved" else previous.acknowledged_at
                ),
                "summary": heading,
                "impact": details.impact,
                "next_action": details.help_requested,
                "evidence_refs": [ref.model_dump() for ref in refs],
                "evidence_summary": list(
                    dict.fromkeys([*previous.evidence_summary, *new_evidence_summary])
                )[-8:],
                "goal_ids": list(dict.fromkeys([*previous.goal_ids, goal.id]))[-100:],
                "session_ids": list(
                    dict.fromkeys([*previous.session_ids, goal.session_id])
                )[-100:],
            },
            expected_revision=previous.revision,
        )
        event_type = (
            "incident.reopened" if previous.status == "resolved" else "incident.updated"
        )
    _append_project_event(
        transaction,
        engagement_id=goal.engagement_id,
        event_type=event_type,
        payload=ButlerEventPayload(
            project_id=goal.engagement_id,
            goal_id=goal.id,
            session_id=goal.session_id,
            source="core",
            actor_role="core",
            summary=heading,
            metrics={"incident_status": incident.status},
            verification_state="agent_reported",
            evidence_refs=[
                ButlerEvidenceRef(
                    kind="incident", id=incident.id, project_id=goal.engagement_id
                ),
                report_ref,
            ],
            incident_key=key,
        ),
        idempotency_key=f"incident:{incident.id}:report:{report.id}",
        now=now,
    )
    return incident, key


def ingest_agent_report(
    store: NebulaStore,
    *,
    turn_id: str,
    idempotency_key: str,
    body: GoalProgressReport | GoalAttentionReport,
    source: Literal["provider_tool", "harness_adapter"],
    expected_project_id: str | None = None,
    expected_session_id: str | None = None,
) -> tuple[ButlerAgentReport, ButlerIncident | None]:
    """Bind an agent report to Core's turn, then commit facts and alert together."""

    if not idempotency_key or len(idempotency_key) > 300:
        raise ValueError("a bounded report idempotency key is required")
    report_type = "progress" if isinstance(body, GoalProgressReport) else "attention"
    report_id = _stable_id("butler-report", f"{turn_id}\0{idempotency_key}")
    details = body.model_dump(mode="json")
    with store.transaction(immediate=True) as transaction:
        turn = _entity_in_transaction(transaction, ChatTurn, turn_id)
        if turn is None or turn.goal_id is None:
            raise ConflictError("agent report requires a Core-owned goal turn")
        if (
            expected_project_id is not None
            and turn.engagement_id != expected_project_id
            or expected_session_id is not None
            and turn.session_id != expected_session_id
        ):
            raise ConflictError("agent report turn does not match its Core invocation")
        if (
            source == "provider_tool"
            and turn.backend != ChatBackend.PROVIDER
            or source == "harness_adapter"
            and turn.backend != ChatBackend.HARNESS
        ):
            raise ConflictError("agent report source does not match the turn runtime")
        transaction.lock_operation_stream(project_stream_id(turn.engagement_id))
        existing = _entity_in_transaction(transaction, ButlerAgentReport, report_id)
        if existing is not None:
            if (
                existing.turn_id != turn_id
                or existing.idempotency_key != idempotency_key
                or existing.report_type != report_type
                or existing.details != details
                or existing.source != source
            ):
                raise ConflictError("report key was reused with different content")
            incident = (
                _entity_in_transaction(
                    transaction,
                    ButlerIncident,
                    incident_id(
                        existing.engagement_id,
                        f"machine:{normalize_machine(body.machine)}",
                    ),
                )
                if isinstance(body, GoalAttentionReport)
                and body.condition in {"machine_unreachable", "machine_down"}
                and body.machine
                else None
            )
            return existing, incident
        if source == "provider_tool" and (
            not turn.tools_enabled
            or turn.status
            not in {
                ChatTurnStatus.ROUTING,
                ChatTurnStatus.WAITING_CALLBACK,
                ChatTurnStatus.FINALIZING,
            }
        ):
            raise ConflictError("new provider reports require an active tool turn")
        goal = _entity_in_transaction(transaction, ChatGoal, turn.goal_id)
        session = _entity_in_transaction(transaction, ChatSession, turn.session_id)
        watch = _entity_in_transaction(
            transaction, ButlerGoalWatch, watch_id(turn.goal_id)
        )
        if (
            goal is None
            or session is None
            or watch is None
            or goal.session_id != turn.session_id
            or goal.engagement_id != turn.engagement_id
            or session.engagement_id != turn.engagement_id
            or watch.engagement_id != turn.engagement_id
            or watch.session_id != turn.session_id
        ):
            raise ConflictError(
                "agent report goal, session, or Watch binding is invalid"
            )
        if goal.status not in {ChatGoalStatus.RUNNING, ChatGoalStatus.BLOCKED}:
            raise ConflictError("agent report requires an active goal")
        if report_type == "progress" and goal.status != ChatGoalStatus.RUNNING:
            raise ConflictError("progress reports require a running goal")
        now = utc_now()
        evidence_refs = _report_evidence(
            transaction,
            project_id=goal.engagement_id,
            references=body.evidence_refs,
        )
        report = ButlerAgentReport(
            id=report_id,
            engagement_id=goal.engagement_id,
            goal_id=goal.id,
            session_id=goal.session_id,
            turn_id=turn.id,
            report_type=report_type,
            source=source,
            actor_role="subagent" if goal.parent_goal_id else "main_agent",
            idempotency_key=idempotency_key,
            details=details,
            evidence_refs=[ref.model_dump() for ref in evidence_refs],
            reported_at=now,
        )
        transaction.add(report)
        coverage_changes = {
            "report_capability": "validated",
            "coverage_reason": (
                "Core accepted a schema-checked report from this goal; its claims remain agent-reported."
            ),
        }
        incident = None
        key = None
        if isinstance(body, GoalProgressReport):
            due = now + PROGRESS_INTERVAL
            if body.next_checkpoint_at is not None:
                due = max(
                    now + timedelta(minutes=5),
                    min(body.next_checkpoint_at, now + MAX_CHECKPOINT_INTERVAL),
                )
            transaction.update(
                ButlerGoalWatch,
                watch.id,
                {
                    **coverage_changes,
                    "last_progress_at": now,
                    "progress_due_at": due,
                    "deadline_generation": watch.deadline_generation + 1,
                    "overdue_emitted_generation": None,
                },
                expected_revision=watch.revision,
            )
            _resolve_progress_incident(
                transaction,
                goal=goal,
                reason="Core received a schema-checked progress report for this goal.",
                now=now,
            )
        else:
            transaction.update(
                ButlerGoalWatch,
                watch.id,
                coverage_changes,
                expected_revision=watch.revision,
            )
            if body.condition in {"machine_unreachable", "machine_down"}:
                incident, key = _machine_incident(
                    transaction,
                    goal=goal,
                    report=report,
                    details=body,
                    evidence_refs=evidence_refs,
                    now=now,
                )
        _append_project_event(
            transaction,
            engagement_id=goal.engagement_id,
            event_type=f"agent.{report_type}_reported",
            payload=ButlerEventPayload(
                project_id=goal.engagement_id,
                goal_id=goal.id,
                session_id=goal.session_id,
                source="agent",
                actor_role=report.actor_role,
                parent_goal_id=goal.parent_goal_id,
                summary=f"Agent submitted a {report_type} report.",
                metrics={"report_type": report_type},
                verification_state="agent_reported",
                evidence_refs=[
                    ButlerEvidenceRef(
                        kind="agent_report", id=report.id, project_id=goal.engagement_id
                    ),
                    *evidence_refs,
                ],
                incident_key=key,
            ),
            idempotency_key=f"report:{report.id}",
            now=now,
        )
    return report, incident


def acknowledge_incident(
    store: NebulaStore,
    *,
    engagement_id: str,
    record_id: str,
    expected_revision: int,
) -> ButlerIncident:
    """Acknowledge one project incident; repeat submissions return the saved state."""

    store.get(Engagement, engagement_id)
    with store.transaction(immediate=True) as transaction:
        transaction.lock_operation_stream(project_stream_id(engagement_id))
        incident = _entity_in_transaction(transaction, ButlerIncident, record_id)
        if incident is None or incident.engagement_id != engagement_id:
            raise NotFoundError("Butler incident not found in this project")
        if incident.status == "acknowledged":
            return incident
        if incident.status != "open":
            raise ConflictError("only an open incident can be acknowledged")
        if incident.revision != expected_revision:
            raise ConflictError("incident changed; refresh before acknowledging")
        now = utc_now()
        updated = transaction.update(
            ButlerIncident,
            incident.id,
            {"status": "acknowledged", "acknowledged_at": now},
            expected_revision=incident.revision,
        )
        _append_project_event(
            transaction,
            engagement_id=engagement_id,
            event_type="incident.acknowledged",
            payload=ButlerEventPayload(
                project_id=engagement_id,
                source="core",
                actor_role="core",
                summary="Operator acknowledged a Butler incident.",
                verification_state="core_observed",
                evidence_refs=[
                    ButlerEvidenceRef(
                        kind="incident", id=incident.id, project_id=engagement_id
                    )
                ],
                incident_key=incident.incident_key,
            ),
            idempotency_key=f"incident:{incident.id}:ack:{updated.revision}",
            now=now,
        )
    return updated


class ButlerOversightService:
    """Replay project events and fire one-shot deadlines without a browser tab."""

    def __init__(
        self,
        store: NebulaStore,
        reviewer: ButlerReviewer | None = None,
        memory_service: ButlerMemoryService | None = None,
    ) -> None:
        self.store = store
        self.reviewer = reviewer
        self.memory_service = memory_service
        self.authority_service = None
        self.worker_id = f"butler-worker-{uuid4()}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self.store.on_project_oversight_change(self.notify)

    def notify(self, engagement_id: str) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._wake_on_loop, engagement_id)

    def _wake_on_loop(self, engagement_id: str) -> None:
        signal = self._signals.setdefault(engagement_id, asyncio.Event())
        signal.set()
        if engagement_id not in self._tasks or self._tasks[engagement_id].done():
            self._tasks[engagement_id] = create_diagnostic_task(
                self._run_project(engagement_id),
                feature="butler",
                event_code="butler.project_worker_failed",
                failure_message="Project oversight stopped unexpectedly.",
                name=f"nebula-butler-{engagement_id[:32]}",
            )

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.reconcile_watches)
        for state in self.store.iter_readable_entities(ButlerProjectState):
            self._wake_on_loop(state.engagement_id)

    async def shutdown(self) -> None:
        self._loop = None
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._signals.clear()

    def reconcile_watches(self) -> None:
        """Restore missing watches from current goal state without inventing history."""

        for goal in self.store.iter_readable_entities(
            ChatGoal,
            {"status": ["running", "paused", "blocked"]},
        ):
            if self.store.find_entity(ButlerGoalWatch, "goal_id", goal.id):
                continue
            with self.store.transaction() as transaction:
                current = _entity_in_transaction(transaction, ChatGoal, goal.id)
                if current is None or current.status not in {
                    ChatGoalStatus.RUNNING,
                    ChatGoalStatus.PAUSED,
                    ChatGoalStatus.BLOCKED,
                }:
                    continue
                if _entity_in_transaction(
                    transaction, ButlerGoalWatch, watch_id(goal.id)
                ):
                    continue
                now = utc_now()
                transaction.lock_operation_stream(project_stream_id(goal.engagement_id))
                transaction.add(_watch_for_goal(current, now=now, recovered=True))
                _append_project_event(
                    transaction,
                    engagement_id=current.engagement_id,
                    event_type="goal.watch_recovered",
                    payload=ButlerEventPayload(
                        project_id=current.engagement_id,
                        goal_id=current.id,
                        session_id=current.session_id,
                        source="core_recovery",
                        actor_role=(
                            "subagent" if current.parent_goal_id else "main_agent"
                        ),
                        parent_goal_id=current.parent_goal_id,
                        summary="Watch restored from the current goal state; earlier events may be missing.",
                        metrics={"goal_revision": current.revision},
                        verification_state="core_observed",
                        evidence_refs=[
                            ButlerEvidenceRef(
                                kind="goal",
                                id=current.id,
                                project_id=current.engagement_id,
                            )
                        ],
                    ),
                    idempotency_key=f"goal:{current.id}:watch-recovered",
                    now=now,
                )

    def _claim_project(self, engagement_id: str) -> ButlerProjectState | None:
        state_id = project_state_id(engagement_id)
        with self.store.transaction() as transaction:
            state = _entity_in_transaction(transaction, ButlerProjectState, state_id)
            if state is None:
                return None
            now = utc_now()
            if (
                state.lease_owner not in {None, self.worker_id}
                and state.lease_expires_at is not None
                and state.lease_expires_at > now
            ):
                return None
            return transaction.update(
                ButlerProjectState,
                state.id,
                {
                    "lease_owner": self.worker_id,
                    "lease_expires_at": now + LEASE_DURATION,
                },
                expected_revision=state.revision,
            )

    def replay_project(self, engagement_id: str) -> bool:
        """Advance a durable cursor; terminal watches close only after their event."""

        try:
            state = self._claim_project(engagement_id)
        except ConflictError:
            return False
        if state is None:
            return False
        while True:
            page = self.store.replay_operation_events(
                project_stream_id(engagement_id),
                after_sequence=state.last_event_sequence,
                limit=100,
            )
            if not page:
                return True
            with self.store.transaction() as transaction:
                current = _entity_in_transaction(
                    transaction, ButlerProjectState, project_state_id(engagement_id)
                )
                now = utc_now()
                if (
                    current is None
                    or current.lease_owner != self.worker_id
                    or current.lease_expires_at is None
                    or current.lease_expires_at <= now
                ):
                    return False
                # Renew by CAS before applying the page. A stale worker cannot
                # commit a cursor or watch change after another Core takes over.
                state = transaction.update(
                    ButlerProjectState,
                    current.id,
                    {"lease_expires_at": now + LEASE_DURATION},
                    expected_revision=current.revision,
                )
                for event in page:
                    if (
                        event.engagement_id != engagement_id
                        or event.operation_kind != PROJECT_STREAM_KIND
                    ):
                        raise ValueError("project stream contains a foreign event")
                    payload = ButlerEventPayload.model_validate(event.payload)
                    if payload.project_id != engagement_id:
                        raise ValueError("project event payload has a foreign project")
                    if (
                        event.event_type in {"goal.completed", "goal.cancelled"}
                        and payload.goal_id
                    ):
                        watch = _entity_in_transaction(
                            transaction, ButlerGoalWatch, watch_id(payload.goal_id)
                        )
                        if (
                            watch is not None
                            and watch.closed_at is None
                            and watch.status
                            in {
                                ChatGoalStatus.COMPLETED,
                                ChatGoalStatus.CANCELLED,
                            }
                        ):
                            transaction.update(
                                ButlerGoalWatch,
                                watch.id,
                                {"closed_at": utc_now()},
                                expected_revision=watch.revision,
                            )
                state = transaction.update(
                    ButlerProjectState,
                    state.id,
                    {
                        "last_event_sequence": page[-1].sequence,
                        "lease_expires_at": utc_now() + LEASE_DURATION,
                    },
                    expected_revision=state.revision,
                )

    def fire_due_deadlines(self, engagement_id: str) -> int:
        """Emit each due generation once; overdue is a signal, not a finding."""

        fired = 0
        now = utc_now()
        for watch in self.store.iter_readable_entities(
            ButlerGoalWatch,
            {"status": ChatGoalStatus.RUNNING.value},
            engagement_id=engagement_id,
        ):
            if watch.progress_due_at is None or watch.progress_due_at > now:
                continue
            if watch.overdue_emitted_generation == watch.deadline_generation:
                continue
            try:
                with self.store.transaction() as transaction:
                    transaction.lock_operation_stream(project_stream_id(engagement_id))
                    state = _entity_in_transaction(
                        transaction, ButlerProjectState, project_state_id(engagement_id)
                    )
                    checked_at = utc_now()
                    if (
                        state is None
                        or state.lease_owner != self.worker_id
                        or state.lease_expires_at is None
                        or state.lease_expires_at <= checked_at
                    ):
                        return fired
                    # Claim the next lease interval before emitting a deadline.
                    # The renewal and event both roll back if ownership races.
                    transaction.update(
                        ButlerProjectState,
                        state.id,
                        {"lease_expires_at": checked_at + LEASE_DURATION},
                        expected_revision=state.revision,
                    )
                    current = _entity_in_transaction(
                        transaction, ButlerGoalWatch, watch.id
                    )
                    goal = _entity_in_transaction(transaction, ChatGoal, watch.goal_id)
                    if (
                        current is None
                        or goal is None
                        or goal.status != ChatGoalStatus.RUNNING
                        or current.status != ChatGoalStatus.RUNNING
                        or current.progress_due_at is None
                        or current.progress_due_at > utc_now()
                        or current.overdue_emitted_generation
                        == current.deadline_generation
                    ):
                        continue
                    transaction.update(
                        ButlerGoalWatch,
                        current.id,
                        {
                            "progress_due_at": None,
                            "overdue_emitted_generation": current.deadline_generation,
                        },
                        expected_revision=current.revision,
                    )
                    _append_project_event(
                        transaction,
                        engagement_id=engagement_id,
                        event_type="goal.progress_overdue",
                        payload=ButlerEventPayload(
                            project_id=engagement_id,
                            goal_id=goal.id,
                            session_id=goal.session_id,
                            source="core",
                            actor_role=(
                                "subagent" if goal.parent_goal_id else "main_agent"
                            ),
                            parent_goal_id=goal.parent_goal_id,
                            summary="Goal progress checkpoint is overdue; work may still be in progress.",
                            metrics={
                                "deadline_generation": current.deadline_generation
                            },
                            verification_state="core_observed",
                            evidence_refs=[
                                ButlerEvidenceRef(
                                    kind="goal", id=goal.id, project_id=engagement_id
                                )
                            ],
                        ),
                        idempotency_key=(
                            f"goal:{goal.id}:deadline:{current.deadline_generation}"
                        ),
                        now=utc_now(),
                    )
                    key = _progress_incident_key(goal.id)
                    record_id = incident_id(engagement_id, key)
                    previous = _entity_in_transaction(
                        transaction, ButlerIncident, record_id
                    )
                    if previous is None:
                        transaction.add(
                            ButlerIncident(
                                id=record_id,
                                engagement_id=engagement_id,
                                incident_key=key,
                                source="goal_checkpoint",
                                verification_state="core_observed",
                                summary="Progress checkpoint is overdue.",
                                impact=(
                                    "Core has not received a schema-checked progress "
                                    "report since this goal's previous checkpoint."
                                ),
                                next_action="Review the goal and request an update through its main agent.",
                                evidence_refs=[
                                    ButlerEvidenceRef(
                                        kind="goal",
                                        id=goal.id,
                                        project_id=engagement_id,
                                    ).model_dump()
                                ],
                                goal_ids=[goal.id],
                                session_ids=[goal.session_id],
                                observed_at=checked_at,
                            )
                        )
                        incident_event = "incident.opened"
                    elif previous.status == "resolved":
                        transaction.update(
                            ButlerIncident,
                            previous.id,
                            {
                                "status": "open",
                                "acknowledged_at": None,
                                "resolved_at": None,
                                "observed_at": checked_at,
                            },
                            expected_revision=previous.revision,
                        )
                        incident_event = "incident.reopened"
                    else:
                        incident_event = None
                    if incident_event is not None:
                        _append_project_event(
                            transaction,
                            engagement_id=engagement_id,
                            event_type=incident_event,
                            payload=ButlerEventPayload(
                                project_id=engagement_id,
                                goal_id=goal.id,
                                session_id=goal.session_id,
                                source="core",
                                actor_role="core",
                                summary="Progress checkpoint is overdue; work may still be in progress.",
                                verification_state="core_observed",
                                evidence_refs=[
                                    ButlerEvidenceRef(
                                        kind="incident",
                                        id=record_id,
                                        project_id=engagement_id,
                                    ),
                                    ButlerEvidenceRef(
                                        kind="goal",
                                        id=goal.id,
                                        project_id=engagement_id,
                                    ),
                                ],
                                incident_key=key,
                            ),
                            idempotency_key=(
                                f"progress-overdue-incident:{record_id}:{current.deadline_generation}"
                            ),
                            now=checked_at,
                        )
                fired += 1
            except ConflictError:
                continue
        return fired

    def _next_wait_seconds(self, engagement_id: str) -> float:
        now = utc_now()
        due_times = [
            watch.progress_due_at
            for watch in self.store.iter_readable_entities(
                ButlerGoalWatch,
                {"status": ChatGoalStatus.RUNNING.value},
                engagement_id=engagement_id,
            )
            if watch.progress_due_at is not None
        ]
        if not due_times:
            return 30.0
        return max(0.25, min(30.0, (min(due_times) - now).total_seconds()))

    async def _run_project(self, engagement_id: str) -> None:
        signal = self._signals[engagement_id]
        while True:
            signal.clear()
            try:
                owned = await asyncio.to_thread(self.replay_project, engagement_id)
                if owned:
                    await asyncio.to_thread(self.fire_due_deadlines, engagement_id)
                    from .butler_review import review_pending

                    await review_pending(
                        self.store,
                        engagement_id,
                        self.worker_id,
                        self.reviewer,
                        self.memory_service,
                    )
                    if self.authority_service is not None:
                        await self.authority_service.process_project(
                            engagement_id, self.worker_id
                        )
                    if self.memory_service is not None:
                        try:
                            for _ in range(5):
                                consumed = await asyncio.to_thread(
                                    self.memory_service.capture_pivotal_project_events,
                                    self.store,
                                    engagement_id,
                                    limit=100,
                                )
                                if consumed < 100:
                                    break
                        except Exception as exc:
                            record_caught_exception(
                                "butler",
                                "butler.memory_capture_failed",
                                "Butler memory will retry without stopping project oversight.",
                                exc,
                                stage="memory_capture",
                            )
                wait_seconds = await asyncio.to_thread(
                    self._next_wait_seconds, engagement_id
                )
            except Exception as exc:
                record_caught_exception(
                    "butler",
                    "butler.project_replay_failed",
                    "Project oversight will retry after a bounded delay.",
                    exc,
                    stage="oversight",
                )
                wait_seconds = 30.0
            try:
                await asyncio.wait_for(signal.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass


def butler_router(service: ButlerOversightService) -> APIRouter:
    router = APIRouter(tags=["butler"])

    @router.get("/engagements/{engagement_id}/butler/overview")
    def overview(
        engagement_id: str,
        watch_offset: int = 0,
        incident_offset: int = 0,
        resource_offset: int = 0,
        decision_offset: int = 0,
        action_offset: int = 0,
    ) -> dict:
        service.store.get(Engagement, engagement_id)
        if any(
            offset < 0
            for offset in (
                watch_offset,
                incident_offset,
                resource_offset,
                decision_offset,
                action_offset,
            )
        ):
            raise HTTPException(422, "overview offsets must be nonnegative")
        watch_filter = {"closed_at": None}
        watch_count = service.store.count_entities(
            ButlerGoalWatch, watch_filter, engagement_id=engagement_id
        )
        watches = service.store.find_entities(
            ButlerGoalWatch,
            watch_filter,
            engagement_id=engagement_id,
            offset=watch_offset,
            limit=OVERVIEW_PAGE_SIZE,
            newest_first=True,
        )
        watched_goals = []
        for watch in watches:
            if watch.closed_at is not None:
                continue
            try:
                goal = service.store.get(ChatGoal, watch.goal_id)
            except NotFoundError:
                continue
            if goal.engagement_id != engagement_id:
                raise ConflictError("watch points outside its project")
            watched_goals.append(
                {
                    "goal_id": goal.id,
                    "session_id": goal.session_id,
                    "parent_goal_id": goal.parent_goal_id,
                    "actor_role": "subagent" if goal.parent_goal_id else "main_agent",
                    "objective": goal.objective[:300],
                    "status": goal.status.value,
                    "progress_due_at": watch.progress_due_at,
                    "coverage": {
                        "report_capability": watch.report_capability,
                        "reason": watch.coverage_reason,
                    },
                }
            )
        incident_counts = {
            status: service.store.count_entities(
                ButlerIncident, {"status": status}, engagement_id=engagement_id
            )
            for status in ("open", "acknowledged", "resolved")
        }
        incident_total = incident_counts["open"] + incident_counts["acknowledged"]
        incidents = []
        if incident_offset < incident_counts["open"]:
            incidents = service.store.find_entities(
                ButlerIncident,
                {"status": "open"},
                engagement_id=engagement_id,
                offset=incident_offset,
                limit=OVERVIEW_PAGE_SIZE,
                newest_first=True,
            )
        remaining = OVERVIEW_PAGE_SIZE - len(incidents)
        if remaining > 0 and incident_offset + len(incidents) < incident_total:
            acknowledged_offset = max(0, incident_offset - incident_counts["open"])
            incidents.extend(
                service.store.find_entities(
                    ButlerIncident,
                    {"status": "acknowledged"},
                    engagement_id=engagement_id,
                    offset=acknowledged_offset,
                    limit=remaining,
                    newest_first=True,
                )
            )
        account_count = service.store.count_entities(
            ButlerAccountCredit, engagement_id=engagement_id
        )
        quota_count = service.store.count_entities(
            ButlerHarnessQuota, engagement_id=engagement_id
        )
        resource_count = account_count + quota_count
        account_states = (
            service.store.find_entities(
                ButlerAccountCredit,
                {},
                engagement_id=engagement_id,
                offset=resource_offset,
                limit=OVERVIEW_PAGE_SIZE,
                newest_first=True,
            )
            if resource_offset < account_count
            else []
        )
        remaining_resources = OVERVIEW_PAGE_SIZE - len(account_states)
        quota_states = (
            service.store.find_entities(
                ButlerHarnessQuota,
                {},
                engagement_id=engagement_id,
                offset=max(0, resource_offset - account_count),
                limit=remaining_resources,
                newest_first=True,
            )
            if remaining_resources > 0
            and resource_offset + len(account_states) < resource_count
            else []
        )
        resources = []
        for resource in account_states:
            profile = None
            for provider_id in resource.provider_ids:
                try:
                    candidate = service.store.get(ProviderProfile, provider_id)
                except NotFoundError:
                    continue
                if (
                    candidate.enabled
                    and candidate.provider_type == "openrouter"
                    and candidate.openrouter_management_secret_ref
                    and hashlib.sha256(
                        candidate.openrouter_management_secret_ref.encode("utf-8")
                    ).hexdigest()
                    == resource.account_key
                ):
                    profile = candidate
                    break
            resources.append(
                {
                    "kind": "openrouter_account",
                    "provider_id": profile.id if profile else resource.provider_id,
                    "provider_name": profile.name if profile else "OpenRouter account",
                    "status": "observed" if profile else "unavailable",
                    "remaining_usd": resource.remaining_usd if profile else None,
                    "observed_at": resource.observed_at if profile else None,
                    "threshold_state": resource.threshold_state
                    if profile
                    else "unknown",
                }
            )
        for resource in quota_states:
            try:
                profile = service.store.get(HarnessProfile, resource.harness_profile_id)
                available = profile.enabled
            except NotFoundError:
                profile = None
                available = False
            resources.append(
                {
                    "kind": "harness_quota",
                    "harness_profile_id": resource.harness_profile_id,
                    "harness_name": profile.name if profile else "Harness quota",
                    "status": "observed" if available else "unavailable",
                    "remaining_percent": resource.remaining_percent
                    if available
                    else None,
                    "observed_at": resource.observed_at if available else None,
                    "threshold_state": resource.threshold_state
                    if available
                    else "unknown",
                    "window_id": resource.window_id,
                    "window_label": resource.window_label,
                    "resets_at": resource.resets_at if available else None,
                    "source": resource.source,
                }
            )
        state = service.store.find_entity(
            ButlerProjectState, "engagement_id", engagement_id
        )
        decision_count = service.store.count_entities(
            ButlerDecision, engagement_id=engagement_id
        )
        decisions = service.store.find_entities(
            ButlerDecision,
            {},
            engagement_id=engagement_id,
            offset=decision_offset,
            limit=OVERVIEW_PAGE_SIZE,
            newest_first=True,
        )
        action_count = service.store.count_entities(
            ButlerAction, engagement_id=engagement_id
        )
        actions = service.store.find_entities(
            ButlerAction,
            {},
            engagement_id=engagement_id,
            offset=action_offset,
            limit=OVERVIEW_PAGE_SIZE,
            newest_first=True,
        )
        from .butler_authority import authority_view

        policy = service.store.find_entity(
            ButlerAuthorityPolicy, "engagement_id", engagement_id
        )
        authority = authority_view(engagement_id, policy)
        return {
            "project_id": engagement_id,
            "authority": authority["authority"],
            "authority_revision": authority["revision"],
            "delegate_actions": authority["delegate_actions"],
            "effective_actions": authority["effective_actions"],
            "watched_goals": watched_goals,
            "incidents": [item.model_dump(mode="json") for item in incidents],
            "resources": resources,
            "decisions": [item.model_dump(mode="json") for item in decisions],
            "actions": [item.model_dump(mode="json") for item in actions],
            "last_event_sequence": state.last_event_sequence if state else 0,
            "summary": {
                "watched_goals": watch_count,
                "open_incidents": incident_counts["open"],
                "acknowledged_incidents": incident_counts["acknowledged"],
                "resolved_incidents": incident_counts["resolved"],
                "account_resources": account_count,
                "harness_quota_resources": quota_count,
                "decisions": decision_count,
                "actions": action_count,
            },
            "next_watch_offset": (
                watch_offset + len(watches)
                if watch_offset + len(watches) < watch_count
                else None
            ),
            "next_incident_offset": (
                incident_offset + len(incidents)
                if incident_offset + len(incidents) < incident_total
                else None
            ),
            "next_resource_offset": (
                resource_offset + len(resources)
                if resource_offset + len(resources) < resource_count
                else None
            ),
            "next_decision_offset": (
                decision_offset + len(decisions)
                if decision_offset + len(decisions) < decision_count
                else None
            ),
            "next_action_offset": (
                action_offset + len(actions)
                if action_offset + len(actions) < action_count
                else None
            ),
            "capabilities": {
                "authority_levels": ["watch", "steer", "delegate"],
                "supported_actions": authority["supported_actions"],
                "incidents": True,
                "decisions": True,
                "actions": True,
                "agent_reports": True,
                "machine_alerts": True,
                "resource_alerts": True,
                "live_events": False,
            },
            "coverage_gaps": [
                "Agent reporting requires a goal turn with verified provider tools; harness report adapters are not connected yet.",
                "OpenRouter account checks require a separate management key and run after provider setup or use and manual refresh; external spending is not polled periodically.",
                "Harness quota alerts require authoritative Codex account windows or numeric Claude SDK rate-limit notices; Grok and missing percentages remain unavailable.",
                "Semantic review uses a project goal's provider when background evidence sharing is permitted; unsupported runtimes and model failures produce explicit unavailable decisions. Steer and Delegate use only explicitly advertised bounded actions.",
                "Live event push is not connected yet.",
            ],
        }

    @router.post(
        "/engagements/{engagement_id}/butler/incidents/{record_id}/acknowledge"
    )
    def acknowledge(
        engagement_id: str, record_id: str, body: IncidentAcknowledgeRequest
    ) -> ButlerIncident:
        return acknowledge_incident(
            service.store,
            engagement_id=engagement_id,
            record_id=record_id,
            expected_revision=body.expected_revision,
        )

    @router.get("/engagements/{engagement_id}/butler/events")
    def events(engagement_id: str, after_sequence: int = 0, limit: int = 100) -> dict:
        service.store.get(Engagement, engagement_id)
        if after_sequence < 0 or not 1 <= limit <= 100:
            raise HTTPException(422, "invalid Butler event page")
        page = service.store.replay_operation_events(
            project_stream_id(engagement_id),
            after_sequence=after_sequence,
            limit=limit,
        )
        for event in page:
            if (
                event.engagement_id != engagement_id
                or event.operation_kind != PROJECT_STREAM_KIND
            ):
                raise ConflictError("project stream contains a foreign event")
            payload = ButlerEventPayload.model_validate(event.payload)
            if payload.project_id != engagement_id:
                raise ConflictError("project event payload has a foreign project")
        return {
            "project_id": engagement_id,
            "events": [
                {
                    "id": event.id,
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "occurred_at": event.occurred_at,
                    "payload": event.payload,
                }
                for event in page
            ],
            "next_sequence": page[-1].sequence if page else after_sequence,
        }

    return router
