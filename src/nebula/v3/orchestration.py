"""Durable supervisor/specialist mission orchestration on LangGraph.

The graph owns scheduling and checkpoints; specialists remain bounded services
with explicit tool allowlists.  Only concise rationales, structured outputs, and
evidence references enter persisted state.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, model_validator

from .domain import (
    AgentAttempt,
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatTokenUsage,
    ContextOwnerType,
    ContextSnapshotStatus,
    ContextSourceReference,
    ProviderProfile,
    RiskClass,
    RunBudget,
    RunStatus,
    Task,
    TaskStatus,
    utc_now,
)
from .context import (
    ContextCallBudget,
    ContextCapacityError,
    ContextCompactionError,
    ContextCompactor,
    ContextSource,
    estimate_tokens,
    lexical_score,
    memory_text,
    resolve_context_limits,
)
from .providers import ModelMessage, ModelProvider, ModelRequest
from .storage import ConflictError, NebulaStore
from .tools import ApprovalRequired


class MissionError(RuntimeError):
    pass


@dataclass
class _ContextBatchSpend:
    usage: ChatTokenUsage = field(default_factory=ChatTokenUsage)
    cost_usd: float = 0.0


class BudgetExceeded(MissionError):
    pass


class SpecialistApprovalRequired(ApprovalRequired):
    """Approval checkpoint that retains model spend incurred before the pause."""

    def __init__(
        self,
        approval: Approval,
        *,
        usage: ChatTokenUsage | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        super().__init__(approval)
        self.usage = usage or ChatTokenUsage()
        self.cost_usd = cost_usd


class SpecialistRole(str, Enum):
    SCOPE_PLANNING = "scope_planning"
    PASSIVE_RECON = "passive_recon"
    NETWORK_SERVICE = "network_service"
    WEB_API = "web_api"
    VULNERABILITY_INTELLIGENCE = "vulnerability_intelligence"
    CODE_ANALYSIS = "code_analysis"
    EVIDENCE_VERIFICATION = "evidence_verification"
    REPORTING_REMEDIATION = "reporting_remediation"


class SpecialistOutcome(str, Enum):
    """Whether one specialist turn needs more work or ends its task."""

    CONTINUE = "continue"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class PlannedTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    role: SpecialistRole
    title: str
    instructions: str
    depends_on: list[str] = Field(default_factory=list)
    parent_task_id: str | None = None
    delegation_depth: int = Field(default=0, ge=0)
    target: str | None = None
    risk_class: RiskClass = RiskClass.LOCAL_READ
    allowed_tools: frozenset[str] | None = None


class MissionPlan(BaseModel):
    summary: str
    rationale: str
    tasks: list[PlannedTask] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_dag(self) -> "MissionPlan":
        identifiers = [task.id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("mission task identifiers must be unique")
        known = set(identifiers)
        for task in self.tasks:
            unknown = set(task.depends_on) - known
            if unknown:
                raise ValueError(f"task {task.id} has unknown dependencies: {unknown}")
            if task.id in task.depends_on:
                raise ValueError("a task cannot depend on itself")
        self._assert_acyclic()
        return self

    def _assert_acyclic(self) -> None:
        dependencies = {task.id: set(task.depends_on) for task in self.tasks}
        ready = [task_id for task_id, deps in dependencies.items() if not deps]
        visited: set[str] = set()
        while ready:
            task_id = ready.pop()
            visited.add(task_id)
            for candidate, deps in dependencies.items():
                deps.discard(task_id)
                if not deps and candidate not in visited and candidate not in ready:
                    ready.append(candidate)
        if len(visited) != len(dependencies):
            raise ValueError("mission plan contains a dependency cycle")


class SpecialistResult(BaseModel):
    summary: str
    rationale: str = ""
    outcome: SpecialistOutcome = SpecialistOutcome.COMPLETE
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    reproducible_steps: list[str] = Field(default_factory=list)
    candidate_finding_ids: list[str] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class VerificationResult(BaseModel):
    accepted: bool
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class Supervisor(Protocol):
    async def plan(
        self, objective: str, context: Mapping[str, Any], budget: RunBudget
    ) -> MissionPlan: ...

    async def synthesize(
        self,
        objective: str,
        plan: MissionPlan,
        results: Mapping[str, SpecialistResult],
    ) -> str: ...


class Specialist(Protocol):
    role: SpecialistRole
    allowed_tools: frozenset[str]

    async def run(self, context: "SpecialistContext") -> SpecialistResult: ...


class Verifier(Protocol):
    async def verify(
        self, task: PlannedTask, result: SpecialistResult
    ) -> VerificationResult: ...


class SpecialistContext(BaseModel):
    engagement_id: str
    run_id: str
    task: PlannedTask
    objective: str
    prior_results: dict[str, SpecialistResult]
    prior_context: str | None = None
    prior_turns: list[SpecialistResult] = Field(default_factory=list)
    retry_errors: list[str] = Field(default_factory=list)
    turn_index: int = Field(default=1, ge=1)
    remaining_tool_calls: int = Field(default=0, ge=0)
    allowed_tools: frozenset[str]
    approval_response: dict[str, Any] | None = None


class EvidenceVerifier:
    """Default independent gate: candidates need evidence and reproduction data."""

    async def verify(
        self, task: PlannedTask, result: SpecialistResult
    ) -> VerificationResult:
        del task
        if result.candidate_finding_ids and not result.evidence_ids:
            return VerificationResult(
                accepted=False,
                rationale="candidate findings have no linked evidence",
            )
        if result.candidate_finding_ids and not result.reproducible_steps:
            return VerificationResult(
                accepted=False,
                rationale="candidate findings have no reproducible verification steps",
                evidence_ids=result.evidence_ids,
            )
        return VerificationResult(
            accepted=True,
            rationale="evidence and reproducibility requirements are satisfied",
            evidence_ids=result.evidence_ids,
        )


class StaticSupervisor:
    """Deterministic stub used by doctor/tests and offline imported engagements."""

    async def plan(
        self, objective: str, context: Mapping[str, Any], budget: RunBudget
    ) -> MissionPlan:
        del context, budget
        return MissionPlan(
            summary="Analyze supplied engagement data and prepare a reviewable result",
            rationale="offline stub performs analysis only and cannot execute tools",
            tasks=[
                PlannedTask(
                    role=SpecialistRole.SCOPE_PLANNING,
                    title="Review scope and objective",
                    instructions=objective,
                    risk_class=RiskClass.LOCAL_READ,
                )
            ],
        )

    async def synthesize(
        self,
        objective: str,
        plan: MissionPlan,
        results: Mapping[str, SpecialistResult],
    ) -> str:
        del plan
        summaries = "; ".join(result.summary for result in results.values())
        return f"{objective}: {summaries}" if summaries else objective


class StaticSpecialist:
    role = SpecialistRole.SCOPE_PLANNING
    allowed_tools: frozenset[str] = frozenset()

    async def run(self, context: SpecialistContext) -> SpecialistResult:
        return SpecialistResult(
            summary="Scope reviewed in analysis-only mode",
            rationale="no approved executable runner or model was requested",
            output={"objective": context.objective, "task": context.task.title},
        )


class ModelSpecialist:
    """Analysis-only specialist backed by an explicitly selected model provider."""

    allowed_tools: frozenset[str] = frozenset()

    def __init__(
        self,
        provider: ModelProvider,
        *,
        role: SpecialistRole = SpecialistRole.SCOPE_PLANNING,
        model: str | None = None,
        max_output_tokens: int = 2048,
    ) -> None:
        if not provider.config.enabled:
            raise MissionError(f"provider {provider.config.id!r} is disabled")
        self.provider = provider
        self.role = role
        self.model = model
        self.max_output_tokens = max_output_tokens

    async def run(self, context: SpecialistContext) -> SpecialistResult:
        prior: str | dict[str, str] = context.prior_context or {
            task_id: result.summary for task_id, result in context.prior_results.items()
        }
        request = ModelRequest(
            model=self.model,
            instructions=(
                "You are a bounded Nebula security-analysis specialist. Analyze only "
                "the supplied objective and task. Do not claim to run commands, access "
                "systems, or use tools. Return a concise analyst-facing result with "
                "clear assumptions and no private chain-of-thought."
            ),
            messages=[
                ModelMessage(
                    role="user",
                    content=(
                        f"Mission objective: {context.objective}\n"
                        f"Task: {context.task.title}\n"
                        f"Instructions: {context.task.instructions}\n"
                        f"Prior task summaries: {prior}\n"
                        f"Prior retry feedback: {context.retry_errors[-5:]}"
                    ),
                )
            ],
            max_output_tokens=self.max_output_tokens,
            metadata={
                "engagement_id": context.engagement_id,
                "run_id": context.run_id,
                "task_id": context.task.id,
                "specialist_role": self.role.value,
            },
        )
        response = await self.provider.complete(request)
        if response.tool_calls:
            raise MissionError(
                "analysis-only model returned tool calls without broker authorization"
            )
        summary = response.text.strip()
        if not summary:
            raise MissionError("model returned an empty analysis result")
        input_rate = float(
            self.provider.config.options.get("input_cost_per_million", 0)
        )
        output_rate = float(
            self.provider.config.options.get("output_cost_per_million", 0)
        )
        cost = (
            response.usage.input_tokens * input_rate
            + response.usage.output_tokens * output_rate
        ) / 1_000_000
        return SpecialistResult(
            summary=summary,
            rationale=(
                f"analysis-only model call via {response.provider_id}/{response.model}; "
                "no executable tools were exposed"
            ),
            output={
                "provider_id": response.provider_id,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "provider_request_id": response.provider_request_id,
            },
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=cost,
            tool_calls=0,
        )


class MissionState(TypedDict, total=False):
    engagement_id: str
    run_id: str
    objective: str
    context: dict[str, Any]
    budget: dict[str, Any]
    started_at: str
    plan: dict[str, Any]
    task_status: dict[str, str]
    attempts: dict[str, int]
    retry_counts: dict[str, int]
    retry_errors: dict[str, list[str]]
    task_history: dict[str, list[dict[str, Any]]]
    results: dict[str, dict[str, Any]]
    verification: dict[str, dict[str, Any]]
    verification_tool_calls: dict[str, int]
    verification_fingerprints: dict[str, str]
    waiting_approvals: dict[str, dict[str, Any]]
    approval_responses: dict[str, dict[str, Any]]
    errors: dict[str, str]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    tool_calls: int
    final_summary: str


class MissionRuntime:
    """Compile and run one durable, bounded LangGraph control plane."""

    def __init__(
        self,
        *,
        store: NebulaStore,
        checkpointer: BaseCheckpointSaver[Any],
        supervisor: Supervisor,
        specialists: Mapping[SpecialistRole, Specialist],
        verifier: Verifier | None = None,
    ) -> None:
        self.store = store
        self.checkpointer = checkpointer
        self.supervisor = supervisor
        self.specialists = dict(specialists)
        self.verifier = verifier or EvidenceVerifier()
        self._context_lock = asyncio.Lock()
        self.graph = self._build_graph().compile(checkpointer=checkpointer)

    def _build_graph(self) -> StateGraph[MissionState]:
        graph = StateGraph(MissionState)
        graph.add_node("plan", self._plan)
        graph.add_node("dispatch", self._dispatch)
        graph.add_node("approval", self._approval)
        graph.add_node("verify", self._verify)
        graph.add_node("fail", self._fail)
        graph.add_node("synthesize", self._synthesize)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "dispatch")
        graph.add_conditional_edges(
            "dispatch",
            self._route_after_dispatch,
            {
                "approval": "approval",
                "verify": "verify",
                "dispatch": "dispatch",
                "fail": "fail",
                "synthesize": "synthesize",
            },
        )
        graph.add_edge("approval", "dispatch")
        graph.add_edge("verify", "dispatch")
        graph.add_edge("fail", END)
        graph.add_edge("synthesize", END)
        return graph

    async def start(
        self,
        *,
        engagement_id: str,
        objective: str,
        budget: RunBudget | None = None,
        context: dict[str, Any] | None = None,
        run_id: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> MissionState:
        run = AgentRun(
            id=run_id or str(uuid4()),
            engagement_id=engagement_id,
            objective=objective,
            budget=budget or RunBudget(),
            status=RunStatus.PLANNING,
            started_at=utc_now(),
            supervisor_provider_id=provider_id,
            supervisor_model=model,
        )
        started_payload = {
            "objective": objective,
            "budget": run.budget.model_dump(mode="json"),
        }
        try:
            run, _ = self.store.create_with_event(
                run,
                run_id=run.id,
                event_type="run.started",
                event_payload=started_payload,
                idempotency_key="run:started",
            )
        except ConflictError:
            existing = self.store.get(AgentRun, run.id)
            if existing.status in {
                RunStatus.COMPLETE,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                raise MissionError(f"run {run.id} is already terminal")
            run = existing
            self.store.append_event(
                run.id,
                "run.started",
                started_payload,
                idempotency_key="run:started",
            )
        state: MissionState = {
            "engagement_id": engagement_id,
            "run_id": run.id,
            "objective": objective,
            "context": context or {},
            "budget": run.budget.model_dump(mode="json"),
            "started_at": (run.started_at or utc_now()).isoformat(),
            "task_status": {},
            "attempts": {},
            "retry_counts": {},
            "retry_errors": {},
            "task_history": {},
            "results": {},
            "verification": {},
            "verification_tool_calls": {},
            "verification_fingerprints": {},
            "waiting_approvals": {},
            "approval_responses": {},
            "errors": {},
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "tool_calls": 0,
        }
        return cast(
            MissionState,
            await self.graph.ainvoke(
                state,
                config={"configurable": {"thread_id": run.id}},
            ),
        )

    async def resume(self, run_id: str, response: dict[str, Any]) -> MissionState:
        return cast(
            MissionState,
            await self.graph.ainvoke(
                Command(resume=response),
                config={"configurable": {"thread_id": run_id}},
            ),
        )

    async def stream(
        self,
        state_or_command: MissionState | Command[Any],
        *,
        run_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self.graph.astream_events(
            state_or_command,
            config={"configurable": {"thread_id": run_id}},
            version="v2",
        ):
            # LangGraph events can contain provider internals.  Expose only the
            # lifecycle envelope here; typed Nebula run events carry details.
            yield {
                "event": event.get("event"),
                "name": event.get("name"),
                "run_id": run_id,
            }

    async def _plan(self, state: MissionState) -> MissionState:
        if state.get("plan"):
            return {}
        budget = RunBudget.model_validate(state["budget"])
        plan = await self.supervisor.plan(
            state["objective"], state.get("context", {}), budget
        )
        if any(
            task.delegation_depth > budget.max_delegation_depth for task in plan.tasks
        ):
            raise BudgetExceeded("planned delegation depth exceeds mission budget")
        statuses = {task.id: TaskStatus.PENDING.value for task in plan.tasks}
        for item in plan.tasks:
            candidate = Task(
                id=item.id,
                engagement_id=state["engagement_id"],
                run_id=state["run_id"],
                parent_task_id=item.parent_task_id,
                specialist_role=item.role.value,
                title=item.title,
                instructions=item.instructions,
                depends_on=item.depends_on,
                risk_class=item.risk_class,
            )
            try:
                self.store.create(candidate)
            except ConflictError as exc:
                existing = self.store.get(Task, candidate.id)
                stable_fields = {
                    "id",
                    "engagement_id",
                    "run_id",
                    "parent_task_id",
                    "specialist_role",
                    "title",
                    "instructions",
                    "depends_on",
                    "risk_class",
                }
                if any(
                    getattr(existing, field) != getattr(candidate, field)
                    for field in stable_fields
                ):
                    raise MissionError(
                        f"planned task {candidate.id} conflicts with durable state"
                    ) from exc
        run = self.store.get(AgentRun, state["run_id"])
        self.store.update_with_event(
            AgentRun,
            state["run_id"],
            {
                "status": RunStatus.RUNNING,
                "metadata": {
                    **run.metadata,
                    "total_tasks": len(plan.tasks),
                    "completed_tasks": 0,
                    "spent_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "tool_calls": 0,
                },
            },
            expected_revision=run.revision,
            run_id=state["run_id"],
            event_type="run.planned",
            event_payload={
                "summary": plan.summary,
                "rationale": plan.rationale,
                "tasks": [task.model_dump(mode="json") for task in plan.tasks],
            },
            idempotency_key="run:planned",
        )
        return {"plan": plan.model_dump(mode="json"), "task_status": statuses}

    async def _dispatch(self, state: MissionState) -> MissionState:
        plan = MissionPlan.model_validate(state["plan"])
        statuses = dict(state["task_status"])
        try:
            self._enforce_budget(state)
        except BudgetExceeded as exc:
            errors = dict(state.get("errors", {}))
            for task in plan.tasks:
                if statuses.get(task.id) in {
                    TaskStatus.COMPLETE.value,
                    TaskStatus.CANCELLED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.BLOCKED.value,
                }:
                    continue
                statuses[task.id] = TaskStatus.BLOCKED.value
                errors[task.id] = str(exc)
                current = self.store.get(Task, task.id)
                self.store.update_with_event(
                    Task,
                    task.id,
                    {"status": TaskStatus.BLOCKED, "completed_at": utc_now()},
                    expected_revision=current.revision,
                    run_id=state["run_id"],
                    event_type="task.blocked",
                    event_payload={
                        "task_id": task.id,
                        "summary": str(exc),
                        "reason": "mission budget exhausted",
                    },
                    idempotency_key=f"task:{task.id}:budget-blocked",
                )
            return {"task_status": statuses, "errors": errors}
        results = dict(state.get("results", {}))
        attempts = dict(state.get("attempts", {}))
        retry_counts = dict(state.get("retry_counts", {}))
        retry_errors = {
            task_id: list(items)
            for task_id, items in state.get("retry_errors", {}).items()
        }
        task_history = {
            task_id: list(items)
            for task_id, items in state.get("task_history", {}).items()
        }
        waiting = dict(state.get("waiting_approvals", {}))
        approval_responses = dict(state.get("approval_responses", {}))
        errors = dict(state.get("errors", {}))
        budget = RunBudget.model_validate(state["budget"])
        context_batch_spend = _ContextBatchSpend()

        ready = [
            task
            for task in plan.tasks
            if statuses[task.id] in {TaskStatus.PENDING.value, TaskStatus.READY.value}
            and all(
                statuses.get(dep) == TaskStatus.COMPLETE.value
                for dep in task.depends_on
            )
        ]
        batch: list[PlannedTask] = []
        per_target: dict[str, int] = {}
        for task in ready:
            target = task.target or "__local__"
            if per_target.get(target, 0) >= budget.per_target_active_operations:
                continue
            batch.append(task)
            per_target[target] = per_target.get(target, 0) + 1
            if len(batch) >= budget.max_concurrency:
                break
        if not batch:
            if any(
                status in {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
                for status in statuses.values()
            ):
                return {}
            unfinished = [
                task_id
                for task_id, status in statuses.items()
                if status not in {TaskStatus.COMPLETE.value, TaskStatus.CANCELLED.value}
            ]
            if unfinished and not waiting and not self._needs_verification(state):
                raise MissionError(f"mission task graph is blocked: {unfinished}")
            return {}

        remaining_batch_tool_calls = max(
            0, budget.max_tool_calls - state.get("tool_calls", 0)
        )
        tool_slots_by_task = {
            task.id: max(0, remaining_batch_tool_calls - index)
            for index, task in enumerate(batch)
        }

        async def execute_one(
            task: PlannedTask,
        ) -> tuple[
            PlannedTask,
            SpecialistResult | None,
            Approval | None,
            str | None,
            ChatTokenUsage,
            float,
        ]:
            context_usage = ChatTokenUsage()
            context_cost = 0.0
            attempt = attempts.get(task.id, 0) + 1
            attempts[task.id] = attempt
            attempt_id = f"{state['run_id']}:{task.id}:{attempt}"
            try:
                self.store.create(
                    AgentAttempt(
                        id=attempt_id,
                        engagement_id=state["engagement_id"],
                        run_id=state["run_id"],
                        task_id=task.id,
                        agent_role=task.role.value,
                        attempt_number=attempt,
                        status=TaskStatus.RUNNING,
                        input={"instructions": task.instructions},
                        started_at=utc_now(),
                    )
                )
            except ConflictError:
                existing_attempt = self.store.get(AgentAttempt, attempt_id)
                self.store.update(
                    AgentAttempt,
                    attempt_id,
                    {"status": TaskStatus.RUNNING, "error": None},
                    expected_revision=existing_attempt.revision,
                )
            current = self.store.get(Task, task.id)
            self.store.update_with_event(
                Task,
                task.id,
                {
                    "status": TaskStatus.RUNNING,
                    "attempt_count": attempt,
                    "started_at": utc_now(),
                },
                expected_revision=current.revision,
                run_id=state["run_id"],
                event_type="task.started",
                event_payload={
                    "task_id": task.id,
                    "attempt": attempt,
                    "role": task.role.value,
                },
                idempotency_key=f"task:{task.id}:attempt:{attempt}:started",
            )
            specialist = self.specialists.get(task.role)
            if specialist is None:
                return (
                    task,
                    None,
                    None,
                    f"no specialist is registered for {task.role.value}",
                    context_usage,
                    context_cost,
                )
            try:
                dependency_ids = self._dependency_closure(plan, task)
                prior_results = {
                    key: SpecialistResult.model_validate(value)
                    for key, value in results.items()
                    if key in dependency_ids
                }
                (
                    prior_context,
                    context_usage,
                    context_cost,
                ) = await self._mission_model_context(
                    state=state,
                    task=task,
                    attempt=attempt,
                    specialist=specialist,
                    prior_results=prior_results,
                    budget=budget,
                    batch_spend=context_batch_spend,
                )
                remaining_tool_calls = tool_slots_by_task[task.id]
                elapsed = (
                    utc_now() - datetime.fromisoformat(state["started_at"])
                ).total_seconds()
                remaining_seconds = max(0.001, budget.max_duration_seconds - elapsed)
                result = await asyncio.wait_for(
                    specialist.run(
                        SpecialistContext(
                            engagement_id=state["engagement_id"],
                            run_id=state["run_id"],
                            task=task,
                            objective=state["objective"],
                            prior_results=prior_results,
                            prior_context=prior_context,
                            prior_turns=[
                                SpecialistResult.model_validate(item)
                                for item in task_history.get(task.id, [])
                            ],
                            retry_errors=retry_errors.get(task.id, []),
                            turn_index=attempt,
                            remaining_tool_calls=remaining_tool_calls,
                            allowed_tools=(
                                specialist.allowed_tools
                                if remaining_tool_calls > 0
                                else frozenset()
                            ),
                            approval_response=state.get("approval_responses", {}).get(
                                task.id
                            ),
                        )
                    ),
                    timeout=remaining_seconds,
                )
                return (
                    task,
                    result,
                    None,
                    None,
                    context_usage,
                    context_cost,
                )
            except ApprovalRequired as exc:
                approval_usage = getattr(exc, "usage", ChatTokenUsage())
                approval_cost = float(getattr(exc, "cost_usd", 0.0))
                context_usage = self._add_context_usage(context_usage, approval_usage)
                context_cost += approval_cost
                return (
                    task,
                    None,
                    exc.approval,
                    None,
                    context_usage,
                    context_cost,
                )
            except TimeoutError as exc:
                elapsed = (
                    utc_now() - datetime.fromisoformat(state["started_at"])
                ).total_seconds()
                error = (
                    "mission duration budget exceeded while specialist was running"
                    if elapsed >= budget.max_duration_seconds - 0.001
                    else f"specialist runtime timed out: {exc}"
                )
                return (
                    task,
                    None,
                    None,
                    error,
                    context_usage,
                    context_cost,
                )
            except Exception as exc:
                if isinstance(exc, ContextCompactionError):
                    context_usage = exc.usage
                    context_cost = self._context_usage_cost(specialist, context_usage)
                return (
                    task,
                    None,
                    None,
                    str(exc),
                    context_usage,
                    context_cost,
                )

        completed = await asyncio.gather(*(execute_one(task) for task in batch))
        token_input = state.get("input_tokens", 0)
        token_output = state.get("output_tokens", 0)
        cost = state.get("cost_usd", 0.0)
        tool_calls = state.get("tool_calls", 0)
        for task, result, approval, error, context_usage, context_cost in completed:
            persisted = self.store.get(Task, task.id)
            token_input += context_usage.input_tokens
            token_output += context_usage.output_tokens
            cost += context_cost
            context_total = context_usage.input_tokens + context_usage.output_tokens
            if budget.max_tokens is not None and (
                token_input + token_output > budget.max_tokens
            ):
                error = "mission token budget exceeded by context compaction"
                result = None
                approval = None
            if budget.max_cost_usd is not None and cost > budget.max_cost_usd:
                error = "mission cost budget exceeded by context compaction"
                result = None
                approval = None
            if approval:
                # A human checkpoint is a continuation of the same attempt, not
                # a model/tool failure and therefore consumes no retry budget.
                attempts[task.id] = max(0, attempts[task.id] - 1)
                statuses[task.id] = TaskStatus.WAITING_APPROVAL.value
                waiting[task.id] = approval.model_dump(mode="json")
                self.store.update(
                    Task,
                    task.id,
                    {
                        "status": TaskStatus.WAITING_APPROVAL,
                        "metadata": {
                            **persisted.metadata,
                            "agent_turns": attempts[task.id] + 1,
                            "runtime_retries": retry_counts.get(task.id, 0),
                        },
                    },
                    expected_revision=persisted.revision,
                )
                attempt_entity = self.store.get(
                    AgentAttempt,
                    f"{state['run_id']}:{task.id}:{attempts[task.id] + 1}",
                )
                self.store.update(
                    AgentAttempt,
                    attempt_entity.id,
                    {
                        "status": TaskStatus.WAITING_APPROVAL,
                        "tokens_used": attempt_entity.tokens_used + context_total,
                        "cost_usd": attempt_entity.cost_usd + context_cost,
                    },
                    expected_revision=attempt_entity.revision,
                )
                continue
            if result is not None:
                projected_tokens = (
                    token_input
                    + token_output
                    + result.input_tokens
                    + result.output_tokens
                )
                projected_cost = cost + result.cost_usd
                projected_tools = tool_calls + result.tool_calls
                if projected_tools > budget.max_tool_calls:
                    error = "mission tool-call budget exceeded"
                elif (
                    budget.max_tokens is not None
                    and projected_tokens > budget.max_tokens
                ):
                    error = "mission token budget exceeded"
                elif (
                    budget.max_cost_usd is not None
                    and projected_cost > budget.max_cost_usd
                ):
                    error = "mission cost budget exceeded"
            if error:
                errors[task.id] = error
                retry_errors.setdefault(task.id, []).append(error[:1_000])
                budget_blocked = self._is_budget_error(error)
                if result is not None:
                    token_input += result.input_tokens
                    token_output += result.output_tokens
                    cost += result.cost_usd
                    tool_calls += result.tool_calls
                    task_history.setdefault(task.id, []).append(
                        result.model_dump(mode="json")
                    )
                if not budget_blocked:
                    retry_counts[task.id] = retry_counts.get(task.id, 0) + 1
                retrying = (
                    not budget_blocked
                    and retry_counts.get(task.id, 0) <= budget.max_retries
                )
                terminal_status = (
                    TaskStatus.BLOCKED if budget_blocked else TaskStatus.FAILED
                )
                statuses[task.id] = (
                    TaskStatus.PENDING.value if retrying else terminal_status.value
                )
                self.store.update_with_event(
                    Task,
                    task.id,
                    {
                        "status": (TaskStatus.PENDING if retrying else terminal_status),
                        "completed_at": None if retrying else utc_now(),
                        "metadata": {
                            **persisted.metadata,
                            "agent_turns": attempts[task.id],
                            "runtime_retries": retry_counts.get(task.id, 0),
                        },
                    },
                    expected_revision=persisted.revision,
                    run_id=state["run_id"],
                    event_type=(
                        "task.retry_scheduled"
                        if retrying
                        else "task.blocked"
                        if budget_blocked
                        else "task.failed"
                    ),
                    event_payload={
                        "task_id": task.id,
                        "error": error,
                        "summary": error,
                        "retry_count": retry_counts.get(task.id, 0),
                    },
                    idempotency_key=(
                        f"task:{task.id}:attempt:{attempts[task.id]}:"
                        f"{'retry' if retrying else 'blocked' if budget_blocked else 'failed'}"
                    ),
                )
                attempt_entity = self.store.get(
                    AgentAttempt,
                    f"{state['run_id']}:{task.id}:{attempts[task.id]}",
                )
                self.store.update(
                    AgentAttempt,
                    attempt_entity.id,
                    {
                        "status": (
                            TaskStatus.BLOCKED if budget_blocked else TaskStatus.FAILED
                        ),
                        "error": error,
                        "output": (
                            result.model_dump(mode="json")
                            if result is not None
                            else attempt_entity.output
                        ),
                        "tokens_used": (
                            attempt_entity.tokens_used
                            + context_total
                            + (
                                result.input_tokens + result.output_tokens
                                if result
                                else 0
                            )
                        ),
                        "cost_usd": (
                            attempt_entity.cost_usd
                            + context_cost
                            + (result.cost_usd if result else 0.0)
                        ),
                        "completed_at": utc_now(),
                    },
                    expected_revision=attempt_entity.revision,
                )
                continue
            assert result is not None
            approval_responses.pop(task.id, None)
            errors.pop(task.id, None)
            token_input += result.input_tokens
            token_output += result.output_tokens
            cost += result.cost_usd
            tool_calls += result.tool_calls
            attempt_entity = self.store.get(
                AgentAttempt,
                f"{state['run_id']}:{task.id}:{attempts[task.id]}",
            )
            self.store.update(
                AgentAttempt,
                attempt_entity.id,
                {
                    "status": (
                        TaskStatus.BLOCKED
                        if result.outcome == SpecialistOutcome.BLOCKED
                        else TaskStatus.COMPLETE
                    ),
                    "output": result.model_dump(mode="json"),
                    "tokens_used": (
                        attempt_entity.tokens_used
                        + result.input_tokens
                        + result.output_tokens
                        + context_total
                    ),
                    "cost_usd": (
                        attempt_entity.cost_usd + result.cost_usd + context_cost
                    ),
                    "completed_at": utc_now(),
                },
                expected_revision=attempt_entity.revision,
            )
            task_metadata = {
                **persisted.metadata,
                "agent_turns": attempts[task.id],
                "runtime_retries": retry_counts.get(task.id, 0),
            }
            if result.outcome == SpecialistOutcome.CONTINUE:
                task_history.setdefault(task.id, []).append(
                    result.model_dump(mode="json")
                )
                statuses[task.id] = TaskStatus.PENDING.value
                self.store.update_with_event(
                    Task,
                    task.id,
                    {
                        "status": TaskStatus.PENDING,
                        "completed_at": None,
                        "metadata": task_metadata,
                    },
                    expected_revision=persisted.revision,
                    run_id=state["run_id"],
                    event_type="task.turn_completed",
                    event_payload={
                        "task_id": task.id,
                        "turn": attempts[task.id],
                        "summary": result.summary,
                        "outcome": result.outcome.value,
                        "evidence_ids": result.evidence_ids,
                    },
                    idempotency_key=(
                        f"task:{task.id}:attempt:{attempts[task.id]}:turn-completed"
                    ),
                )
                self.store.append_event(
                    state["run_id"],
                    "task.continuing",
                    {
                        "task_id": task.id,
                        "turn": attempts[task.id],
                        "summary": "Specialist scheduled another investigative turn",
                    },
                    idempotency_key=(
                        f"task:{task.id}:attempt:{attempts[task.id]}:continuing"
                    ),
                )
                continue
            results[task.id] = result.model_dump(mode="json")
            if result.outcome == SpecialistOutcome.BLOCKED:
                statuses[task.id] = TaskStatus.BLOCKED.value
                blocker = result.rationale or result.summary
                errors[task.id] = blocker
                task_history.setdefault(task.id, []).append(
                    result.model_dump(mode="json")
                )
                self.store.update_with_event(
                    Task,
                    task.id,
                    {
                        "status": TaskStatus.BLOCKED,
                        "completed_at": utc_now(),
                        "metadata": task_metadata,
                    },
                    expected_revision=persisted.revision,
                    run_id=state["run_id"],
                    event_type="task.blocked",
                    event_payload={
                        "task_id": task.id,
                        "summary": result.summary,
                        "reason": blocker,
                        "evidence_ids": result.evidence_ids,
                    },
                    idempotency_key=(
                        f"task:{task.id}:attempt:{attempts[task.id]}:blocked"
                    ),
                )
                continue
            # Completion is provisional until the independent verifier accepts it.
            statuses[task.id] = TaskStatus.RUNNING.value
            self.store.update(
                Task,
                task.id,
                {"status": TaskStatus.RUNNING, "metadata": task_metadata},
                expected_revision=persisted.revision,
            )
        run = self.store.get(AgentRun, state["run_id"])
        self.store.update(
            AgentRun,
            run.id,
            {
                "metadata": {
                    **run.metadata,
                    "total_tasks": len(plan.tasks),
                    "completed_tasks": sum(
                        status == TaskStatus.COMPLETE.value
                        for status in statuses.values()
                    ),
                    "spent_usd": cost,
                    "input_tokens": token_input,
                    "output_tokens": token_output,
                    "tool_calls": tool_calls,
                }
            },
            expected_revision=run.revision,
        )
        return {
            "task_status": statuses,
            "results": results,
            "attempts": attempts,
            "retry_counts": retry_counts,
            "retry_errors": retry_errors,
            "task_history": task_history,
            "waiting_approvals": waiting,
            "approval_responses": approval_responses,
            "errors": errors,
            "input_tokens": token_input,
            "output_tokens": token_output,
            "cost_usd": cost,
            "tool_calls": tool_calls,
        }

    async def _mission_model_context(
        self,
        *,
        state: MissionState,
        task: PlannedTask,
        attempt: int,
        specialist: Specialist,
        prior_results: dict[str, SpecialistResult],
        budget: RunBudget,
        batch_spend: _ContextBatchSpend,
    ) -> tuple[str | None, ChatTokenUsage, float]:
        if not prior_results:
            return None, ChatTokenUsage(), 0.0
        provider = getattr(specialist, "provider", None)
        if not isinstance(provider, ModelProvider):
            return None, ChatTokenUsage(), 0.0
        model = getattr(specialist, "model", None) or provider.config.default_model
        if not isinstance(model, str) or not model:
            return None, ChatTokenUsage(), 0.0
        try:
            profile = self.store.get(ProviderProfile, provider.config.id)
        except Exception:
            # Directly constructed test/runtime providers can still use the
            # deterministic summaries already present on SpecialistResult.
            return None, ChatTokenUsage(), 0.0
        prior_payload = {
            task_id: result.model_dump(mode="json")
            for task_id, result in prior_results.items()
        }
        prompt = (
            f"Mission objective: {state['objective']}\n"
            f"Task: {task.title}\nInstructions: {task.instructions}\n"
            f"Prior results: {json.dumps(prior_payload, sort_keys=True)}"
        )
        limits = resolve_context_limits(
            profile,
            requested_output_tokens=getattr(specialist, "max_output_tokens", None),
        )
        if estimate_tokens(prompt, message_count=1) <= limits.target_input_tokens:
            return None, ChatTokenUsage(), 0.0
        started_key = f"context:{task.id}:attempt:{attempt}:started"
        self.store.append_event(
            state["run_id"],
            "context.compaction_started",
            {"task_id": task.id, "attempt": attempt},
            idempotency_key=started_key,
        )
        spend_recorded = False
        try:
            async with self._context_lock:
                remaining_tokens = (
                    None
                    if budget.max_tokens is None
                    else budget.max_tokens
                    - state.get("input_tokens", 0)
                    - state.get("output_tokens", 0)
                    - batch_spend.usage.input_tokens
                    - batch_spend.usage.output_tokens
                )
                remaining_cost = (
                    None
                    if budget.max_cost_usd is None
                    else budget.max_cost_usd
                    - state.get("cost_usd", 0.0)
                    - batch_spend.cost_usd
                )
                if remaining_tokens is not None and remaining_tokens <= 0:
                    raise ContextCompactionError(
                        "insufficient mission token budget for context compaction"
                    )
                if remaining_cost is not None and remaining_cost <= 0:
                    raise ContextCompactionError(
                        "insufficient mission cost budget for context compaction"
                    )
                through = 0
                while True:
                    run_events = self.store.replay_events(
                        state["run_id"],
                        after_sequence=through,
                        limit=10_000,
                    )
                    if not run_events:
                        break
                    through = run_events[-1].sequence
                    if len(run_events) < 10_000:
                        break
                result = await ContextCompactor(self.store).compact(
                    owner_type=ContextOwnerType.AGENT_RUN,
                    owner_id=state["run_id"],
                    engagement_id=state["engagement_id"],
                    provider_profile=profile,
                    provider=provider,
                    model=model,
                    compacted_through=through,
                    objective=state["objective"],
                    budget=ContextCallBudget(
                        max_tokens=remaining_tokens,
                        max_cost_usd=remaining_cost,
                    ),
                    sources=[
                        ContextSource(
                            reference=ContextSourceReference(
                                source_kind="task_result",
                                source_id=task_id,
                            ),
                            content=json.dumps(
                                value.model_dump(mode="json"),
                                sort_keys=True,
                                ensure_ascii=False,
                            ),
                        )
                        for task_id, value in prior_results.items()
                    ],
                )
                snapshot = result.snapshot
                if (
                    snapshot.status != ContextSnapshotStatus.READY
                    or snapshot.memory is None
                ):
                    raise ContextCompactionError(
                        "mission context snapshot is not ready"
                    )
                usage = snapshot.usage if result.created else ChatTokenUsage()
                cost = snapshot.cost_usd if result.created else 0.0
                try:
                    assembled_context = self._assemble_mission_context(
                        state=state,
                        task=task,
                        memory=memory_text(snapshot.memory),
                        prior_results=prior_results,
                        target_tokens=limits.target_input_tokens,
                    )
                except ContextCompactionError as exc:
                    exc.usage = usage
                    raise
                batch_spend.usage = self._add_context_usage(batch_spend.usage, usage)
                batch_spend.cost_usd += cost
                spend_recorded = True
            self.store.append_event(
                state["run_id"],
                "context.compacted",
                {
                    "task_id": task.id,
                    "attempt": attempt,
                    "snapshot_id": snapshot.id,
                    "created": result.created,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": cost,
                },
                idempotency_key=f"context:{task.id}:attempt:{attempt}:completed",
            )
            return assembled_context, usage, cost
        except Exception as exc:
            usage = (
                exc.usage
                if isinstance(exc, ContextCompactionError)
                else ChatTokenUsage()
            )
            cost = self._context_usage_cost(specialist, usage)
            if not spend_recorded:
                async with self._context_lock:
                    batch_spend.usage = self._add_context_usage(
                        batch_spend.usage, usage
                    )
                    batch_spend.cost_usd += cost
            self.store.append_event(
                state["run_id"],
                "context.compaction_failed",
                {
                    "task_id": task.id,
                    "attempt": attempt,
                    "error": "required mission context compaction failed",
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
                idempotency_key=f"context:{task.id}:attempt:{attempt}:failed",
            )
            if isinstance(exc, ContextCompactionError):
                raise
            raise ContextCompactionError(
                "required mission context compaction failed", usage=usage
            ) from exc

    @staticmethod
    def _dependency_closure(plan: MissionPlan, task: PlannedTask) -> set[str]:
        dependencies = {item.id: set(item.depends_on) for item in plan.tasks}
        closure = set(task.depends_on)
        pending = list(closure)
        while pending:
            dependency = pending.pop()
            for ancestor in dependencies.get(dependency, set()):
                if ancestor not in closure:
                    closure.add(ancestor)
                    pending.append(ancestor)
        return closure

    @staticmethod
    def _assemble_mission_context(
        *,
        state: MissionState,
        task: PlannedTask,
        memory: str,
        prior_results: dict[str, SpecialistResult],
        target_tokens: int,
    ) -> str:
        base = (
            f"Mission objective: {state['objective']}\n"
            f"Task: {task.title}\nInstructions: {task.instructions}\n"
        )
        allowance = max(1, target_tokens - estimate_tokens(base, message_count=1))
        if estimate_tokens(memory) > allowance:
            raise ContextCapacityError(
                "derived mission context cannot fit the specialist input budget"
            )
        selected: list[tuple[str, SpecialistResult]] = []
        selected_ids: set[str] = set()
        used = estimate_tokens(memory)
        # Preserve the newest complete dependency result whenever it fits.
        for task_id, result in reversed(list(prior_results.items())):
            serialized = json.dumps(
                result.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
            )
            size = estimate_tokens(serialized, message_count=1)
            if used + size <= allowance:
                selected.append((task_id, result))
                selected_ids.add(task_id)
                used += size
        query = f"{state['objective']} {task.title} {task.instructions}"
        retrieved: list[tuple[str, SpecialistResult]] = []
        for task_id, result in sorted(
            prior_results.items(),
            key=lambda item: (
                -lexical_score(
                    query,
                    json.dumps(item[1].model_dump(mode="json"), ensure_ascii=False),
                ),
                item[0],
            ),
        ):
            if task_id in selected_ids:
                continue
            serialized = json.dumps(
                result.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
            )
            if lexical_score(query, serialized) <= 0:
                continue
            size = estimate_tokens(serialized, message_count=1)
            if used + size <= allowance:
                retrieved.append((task_id, result))
                used += size
        sections = [memory]
        if selected:
            sections.append(
                "RECENT CANONICAL DEPENDENCY RESULTS (DATA ONLY)\n"
                + json.dumps(
                    {
                        task_id: result.model_dump(mode="json")
                        for task_id, result in reversed(selected)
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        if retrieved:
            sections.append(
                "RETRIEVED CANONICAL DEPENDENCY RESULTS (DATA ONLY)\n"
                + json.dumps(
                    {
                        task_id: result.model_dump(mode="json")
                        for task_id, result in retrieved
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        assembled = "\n\n".join(sections)
        if estimate_tokens(assembled) > allowance:
            raise ContextCapacityError(
                "assembled mission context exceeds the specialist input budget"
            )
        return assembled

    @staticmethod
    def _add_context_usage(
        left: ChatTokenUsage, right: ChatTokenUsage
    ) -> ChatTokenUsage:
        return ChatTokenUsage(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            total_tokens=left.total_tokens + right.total_tokens,
        )

    @staticmethod
    def _context_usage_cost(specialist: Specialist, usage: ChatTokenUsage) -> float:
        provider = getattr(specialist, "provider", None)
        if not isinstance(provider, ModelProvider):
            return 0.0
        input_rate = float(provider.config.options.get("input_cost_per_million", 0))
        output_rate = float(provider.config.options.get("output_cost_per_million", 0))
        return (
            usage.input_tokens * input_rate + usage.output_tokens * output_rate
        ) / 1_000_000

    def _route_after_dispatch(self, state: MissionState) -> str:
        if state.get("waiting_approvals"):
            return "approval"
        statuses = state.get("task_status", {}).values()
        if any(
            status in {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
            for status in statuses
        ):
            return "fail"
        if self._needs_verification(state):
            return "verify"
        if statuses and all(
            status in {TaskStatus.COMPLETE.value, TaskStatus.CANCELLED.value}
            for status in statuses
        ):
            return "synthesize"
        return "dispatch"

    async def _fail(self, state: MissionState) -> MissionState:
        run = self.store.get(AgentRun, state["run_id"])
        statuses = dict(state.get("task_status", {}))
        errors = dict(state.get("errors", {}))
        plan = state.get("plan", {})
        planned_tasks = plan.get("tasks", []) if isinstance(plan, dict) else []
        for planned in planned_tasks:
            task_id = planned.get("id") if isinstance(planned, dict) else None
            if not isinstance(task_id, str) or statuses.get(task_id) in {
                TaskStatus.COMPLETE.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELLED.value,
            }:
                continue
            reason = "mission stopped because another task was blocked or failed"
            statuses[task_id] = TaskStatus.BLOCKED.value
            errors.setdefault(task_id, reason)
            current = self.store.get(Task, task_id)
            self.store.update_with_event(
                Task,
                task_id,
                {"status": TaskStatus.BLOCKED, "completed_at": utc_now()},
                expected_revision=current.revision,
                run_id=state["run_id"],
                event_type="task.blocked",
                event_payload={
                    "task_id": task_id,
                    "summary": reason,
                    "reason": reason,
                },
                idempotency_key=f"task:{task_id}:run-failed-blocked",
            )

        summary = "mission failed: " + "; ".join(
            f"{task_id}: {error}" for task_id, error in errors.items()
        )
        partial_observations: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        result_groups: list[tuple[str, list[dict[str, Any]]]] = [
            (task_id, [raw])
            for task_id, raw in state.get("results", {}).items()
            if isinstance(raw, dict)
        ]
        result_groups.extend(
            (task_id, [raw for raw in history if isinstance(raw, dict)])
            for task_id, history in state.get("task_history", {}).items()
        )
        for task_id, raw_results in result_groups:
            for raw in raw_results:
                result = SpecialistResult.model_validate(raw)
                tool = str(result.output.get("tool") or "")
                key = (task_id, tool, result.summary)
                if key in seen:
                    continue
                seen.add(key)
                partial_observations.append(
                    {
                        "task_id": task_id,
                        "outcome": result.outcome.value,
                        "tool": tool or None,
                        "status": result.output.get("status"),
                        "summary": result.summary,
                        "commands": result.reproducible_steps,
                        "evidence_ids": result.evidence_ids,
                    }
                )
        partial = [item["summary"] for item in partial_observations]
        if partial:
            summary += ". Partial results: " + "; ".join(partial)
        self.store.update_with_event(
            AgentRun,
            run.id,
            {
                "status": RunStatus.FAILED,
                "completed_at": utc_now(),
                "metadata": {
                    **run.metadata,
                    "total_tasks": len(planned_tasks),
                    "completed_tasks": sum(
                        status == TaskStatus.COMPLETE.value
                        for status in statuses.values()
                    ),
                    "spent_usd": state.get("cost_usd", 0.0),
                    "input_tokens": state.get("input_tokens", 0),
                    "output_tokens": state.get("output_tokens", 0),
                    "tool_calls": state.get("tool_calls", 0),
                    "final_summary": summary,
                    "partial_observations": partial_observations,
                },
            },
            expected_revision=run.revision,
            run_id=run.id,
            event_type="run.failed",
            event_payload={
                "summary": summary,
                "errors": errors,
                "partial_observations": partial_observations,
            },
            idempotency_key="run:failed",
        )
        return {
            "final_summary": summary,
            "task_status": statuses,
            "errors": errors,
        }

    async def _approval(self, state: MissionState) -> MissionState:
        waiting = dict(state.get("waiting_approvals", {}))
        task_id = next(iter(waiting))
        card = waiting[task_id]
        response = interrupt(
            {
                "kind": "tool_approval",
                "task_id": task_id,
                "approval": card,
            }
        )
        if not isinstance(response, dict):
            raise MissionError("approval resume value must be an object")
        status = ApprovalStatus(response.get("status", ApprovalStatus.REJECTED.value))
        statuses = dict(state["task_status"])
        responses = dict(state.get("approval_responses", {}))
        waiting.pop(task_id, None)
        if status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.EDITED,
            ApprovalStatus.REJECTED,
        }:
            statuses[task_id] = TaskStatus.PENDING.value
            responses[task_id] = response
        else:
            statuses[task_id] = TaskStatus.CANCELLED.value
        current = self.store.get(Task, task_id)
        self.store.update_with_event(
            Task,
            task_id,
            {"status": TaskStatus(statuses[task_id])},
            expected_revision=current.revision,
            run_id=state["run_id"],
            event_type="approval.resolved",
            event_payload={"task_id": task_id, "status": status.value},
            idempotency_key=f"approval:{card.get('id', task_id)}:{status.value}",
        )
        if status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.EDITED,
            ApprovalStatus.REJECTED,
        }:
            plan = MissionPlan.model_validate(state["plan"])
            changed = True
            while changed:
                changed = False
                for task in plan.tasks:
                    if statuses.get(task.id) in {
                        TaskStatus.COMPLETE.value,
                        TaskStatus.CANCELLED.value,
                        TaskStatus.FAILED.value,
                    }:
                        continue
                    if any(
                        statuses.get(dependency) == TaskStatus.CANCELLED.value
                        for dependency in task.depends_on
                    ):
                        statuses[task.id] = TaskStatus.CANCELLED.value
                        dependent = self.store.get(Task, task.id)
                        self.store.update_with_event(
                            Task,
                            task.id,
                            {"status": TaskStatus.CANCELLED, "completed_at": utc_now()},
                            expected_revision=dependent.revision,
                            run_id=state["run_id"],
                            event_type="task.cancelled",
                            event_payload={
                                "task_id": task.id,
                                "reason": "required predecessor was not approved",
                            },
                            idempotency_key=f"task:{task.id}:dependency-cancelled",
                        )
                        changed = True
        return {
            "waiting_approvals": waiting,
            "approval_responses": responses,
            "task_status": statuses,
        }

    async def _verify(self, state: MissionState) -> MissionState:
        plan = MissionPlan.model_validate(state["plan"])
        verification = dict(state.get("verification", {}))
        errors = dict(state.get("errors", {}))
        statuses = dict(state.get("task_status", {}))
        results = dict(state.get("results", {}))
        task_history = {
            task_id: list(items)
            for task_id, items in state.get("task_history", {}).items()
        }
        retry_errors = {
            task_id: list(items)
            for task_id, items in state.get("retry_errors", {}).items()
        }
        verification_tool_calls = dict(state.get("verification_tool_calls", {}))
        verification_fingerprints = dict(state.get("verification_fingerprints", {}))
        budget = RunBudget.model_validate(state["budget"])
        tasks = {task.id: task for task in plan.tasks}
        for task_id, raw in list(results.items()):
            if task_id in verification:
                continue
            result = SpecialistResult.model_validate(raw)
            evidence_fingerprint = json.dumps(
                {
                    "candidate_finding_ids": sorted(result.candidate_finding_ids),
                    "evidence_ids": sorted(result.evidence_ids),
                    "reproducible_steps": sorted(result.reproducible_steps),
                },
                sort_keys=True,
            )
            if verification_fingerprints.get(task_id) == evidence_fingerprint:
                verdict = VerificationResult(
                    accepted=False,
                    rationale=(
                        "verification requires new evidence or reproducible steps; "
                        "the submitted evidence package is unchanged"
                    ),
                    evidence_ids=result.evidence_ids,
                )
            else:
                verdict = await self.verifier.verify(tasks[task_id], result)
            verification_event = {
                "task_id": task_id,
                "accepted": verdict.accepted,
                "rationale": verdict.rationale,
                "evidence_ids": verdict.evidence_ids,
            }
            verification_key = (
                f"task:{task_id}:attempt:{state.get('attempts', {}).get(task_id, 0)}:"
                "verification"
            )
            current = self.store.get(Task, task_id)
            if verdict.accepted:
                statuses[task_id] = TaskStatus.COMPLETE.value
                errors.pop(task_id, None)
                verification[task_id] = verdict.model_dump(mode="json")
                self.store.update_with_event(
                    Task,
                    task_id,
                    {"status": TaskStatus.COMPLETE, "completed_at": utc_now()},
                    expected_revision=current.revision,
                    run_id=state["run_id"],
                    event_type="task.completed",
                    event_payload={
                        "task_id": task_id,
                        "summary": result.summary,
                        "evidence_ids": result.evidence_ids,
                    },
                    idempotency_key=(
                        f"task:{task_id}:attempt:"
                        f"{state.get('attempts', {}).get(task_id, 0)}:completed"
                    ),
                )
                self.store.append_event(
                    state["run_id"],
                    "task.verified",
                    verification_event,
                    idempotency_key=verification_key,
                )
                continue

            current_tool_calls = state.get("tool_calls", 0)
            previous_rejection = verification_tool_calls.get(task_id)
            can_investigate = current_tool_calls < budget.max_tool_calls and (
                previous_rejection is None or current_tool_calls > previous_rejection
            )
            errors[task_id] = verdict.rationale
            verification_fingerprints[task_id] = evidence_fingerprint
            if can_investigate:
                verification_tool_calls[task_id] = current_tool_calls
                task_history.setdefault(task_id, []).append(raw)
                retry_errors.setdefault(task_id, []).append(
                    f"Verification rejected the result: {verdict.rationale}"[:1_000]
                )
                results.pop(task_id, None)
                verification.pop(task_id, None)
                statuses[task_id] = TaskStatus.PENDING.value
                self.store.update_with_event(
                    Task,
                    task_id,
                    {"status": TaskStatus.PENDING, "completed_at": None},
                    expected_revision=current.revision,
                    run_id=state["run_id"],
                    event_type="task.verification_failed",
                    event_payload=verification_event,
                    idempotency_key=verification_key,
                )
                self.store.append_event(
                    state["run_id"],
                    "task.continuing",
                    {
                        "task_id": task_id,
                        "summary": (
                            "Verification requested additional evidence: "
                            f"{verdict.rationale}"
                        ),
                        "reason": "verification feedback",
                    },
                    idempotency_key=f"{verification_key}:continuing",
                )
                continue

            statuses[task_id] = TaskStatus.BLOCKED.value
            self.store.append_event(
                state["run_id"],
                "task.verification_failed",
                verification_event,
                idempotency_key=verification_key,
            )
            current = self.store.get(Task, task_id)
            self.store.update_with_event(
                Task,
                task_id,
                {"status": TaskStatus.BLOCKED, "completed_at": utc_now()},
                expected_revision=current.revision,
                run_id=state["run_id"],
                event_type="task.blocked",
                event_payload={
                    "task_id": task_id,
                    "summary": verdict.rationale,
                    "reason": "verification could not be satisfied within budget",
                },
                idempotency_key=f"{verification_key}:blocked",
            )
        return {
            "verification": verification,
            "verification_tool_calls": verification_tool_calls,
            "verification_fingerprints": verification_fingerprints,
            "errors": errors,
            "task_status": statuses,
            "results": results,
            "task_history": task_history,
            "retry_errors": retry_errors,
        }

    async def _synthesize(self, state: MissionState) -> MissionState:
        plan = MissionPlan.model_validate(state["plan"])
        results = {
            key: SpecialistResult.model_validate(value)
            for key, value in state.get("results", {}).items()
        }
        summary = await self.supervisor.synthesize(state["objective"], plan, results)
        run = self.store.get(AgentRun, state["run_id"])
        self.store.update_with_event(
            AgentRun,
            run.id,
            {
                "status": RunStatus.COMPLETE,
                "completed_at": utc_now(),
                "metadata": {
                    **run.metadata,
                    "total_tasks": len(plan.tasks),
                    "completed_tasks": sum(
                        status == TaskStatus.COMPLETE.value
                        for status in state.get("task_status", {}).values()
                    ),
                    "spent_usd": state.get("cost_usd", 0.0),
                    "input_tokens": state.get("input_tokens", 0),
                    "output_tokens": state.get("output_tokens", 0),
                    "tool_calls": state.get("tool_calls", 0),
                    "final_summary": summary,
                },
            },
            expected_revision=run.revision,
            run_id=run.id,
            event_type="run.completed",
            event_payload={
                "summary": summary,
                "input_tokens": state.get("input_tokens", 0),
                "output_tokens": state.get("output_tokens", 0),
                "cost_usd": state.get("cost_usd", 0),
                "tool_calls": state.get("tool_calls", 0),
            },
            idempotency_key="run:completed",
        )
        return {"final_summary": summary}

    @staticmethod
    def _needs_verification(state: MissionState) -> bool:
        return any(
            task_id not in state.get("verification", {})
            for task_id in state.get("results", {})
        )

    @staticmethod
    def _enforce_budget(state: MissionState) -> None:
        budget = RunBudget.model_validate(state["budget"])
        started = datetime.fromisoformat(state["started_at"])
        elapsed = (utc_now() - started).total_seconds()
        if elapsed >= budget.max_duration_seconds:
            raise BudgetExceeded("mission duration budget exceeded")
        total_tokens = state.get("input_tokens", 0) + state.get("output_tokens", 0)
        if budget.max_tokens is not None and total_tokens >= budget.max_tokens:
            raise BudgetExceeded("mission token budget exceeded")
        if (
            budget.max_cost_usd is not None
            and state.get("cost_usd", 0) > budget.max_cost_usd
        ):
            raise BudgetExceeded("mission cost budget exceeded")
        if state.get("tool_calls", 0) > budget.max_tool_calls:
            raise BudgetExceeded("mission tool-call budget exceeded")

    @staticmethod
    def _is_budget_error(error: str) -> bool:
        normalized = error.lower()
        return any(
            marker in normalized
            for marker in (
                "mission duration budget",
                "mission token budget",
                "mission cost budget",
                "mission tool-call budget",
                "insufficient mission token budget",
                "insufficient mission cost budget",
                "exhausted its tool-call budget",
            )
        )


@asynccontextmanager
async def sqlite_mission_runtime(
    *,
    checkpoint_path: str | Path,
    store: NebulaStore,
    supervisor: Supervisor,
    specialists: Mapping[SpecialistRole, Specialist],
    verifier: Verifier | None = None,
) -> AsyncIterator[MissionRuntime]:
    """Open a strict, persistent local LangGraph checkpointer."""

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    path = Path(checkpoint_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as checkpointer:
        await checkpointer.setup()
        yield MissionRuntime(
            store=store,
            checkpointer=checkpointer,
            supervisor=supervisor,
            specialists=specialists,
            verifier=verifier,
        )


__all__ = [
    "BudgetExceeded",
    "EvidenceVerifier",
    "MissionError",
    "MissionPlan",
    "MissionRuntime",
    "ModelSpecialist",
    "PlannedTask",
    "SpecialistContext",
    "SpecialistApprovalRequired",
    "SpecialistOutcome",
    "SpecialistResult",
    "SpecialistRole",
    "StaticSpecialist",
    "StaticSupervisor",
    "VerificationResult",
    "sqlite_mission_runtime",
]
