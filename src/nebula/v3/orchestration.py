"""Durable supervisor/specialist mission orchestration on LangGraph.

The graph owns scheduling and checkpoints; specialists remain bounded services
with explicit tool allowlists.  Only concise rationales, structured outputs, and
evidence references enter persisted state.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
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
    RiskClass,
    RunBudget,
    RunStatus,
    Task,
    TaskStatus,
    utc_now,
)
from .providers import ModelMessage, ModelProvider, ModelRequest
from .storage import ConflictError, NebulaStore
from .tools import ApprovalRequired


class MissionError(RuntimeError):
    pass


class BudgetExceeded(MissionError):
    pass


class SpecialistRole(str, Enum):
    SCOPE_PLANNING = "scope_planning"
    PASSIVE_RECON = "passive_recon"
    NETWORK_SERVICE = "network_service"
    WEB_API = "web_api"
    VULNERABILITY_INTELLIGENCE = "vulnerability_intelligence"
    CODE_ANALYSIS = "code_analysis"
    EVIDENCE_VERIFICATION = "evidence_verification"
    REPORTING_REMEDIATION = "reporting_remediation"


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
        prior = {
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
                        f"Prior task summaries: {prior}"
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
    results: dict[str, dict[str, Any]]
    verification: dict[str, dict[str, Any]]
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
            "results": {},
            "verification": {},
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
        self._enforce_budget(state)
        plan = MissionPlan.model_validate(state["plan"])
        statuses = dict(state["task_status"])
        results = dict(state.get("results", {}))
        attempts = dict(state.get("attempts", {}))
        waiting = dict(state.get("waiting_approvals", {}))
        errors = dict(state.get("errors", {}))
        budget = RunBudget.model_validate(state["budget"])

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
            if any(status == TaskStatus.FAILED.value for status in statuses.values()):
                return {}
            unfinished = [
                task_id
                for task_id, status in statuses.items()
                if status not in {TaskStatus.COMPLETE.value, TaskStatus.CANCELLED.value}
            ]
            if unfinished and not waiting and not self._needs_verification(state):
                raise MissionError(f"mission task graph is blocked: {unfinished}")
            return {}

        async def execute_one(
            task: PlannedTask,
        ) -> tuple[PlannedTask, SpecialistResult | None, Approval | None, str | None]:
            attempt = attempts.get(task.id, 0) + 1
            if attempt > budget.max_retries + 1:
                return task, None, None, "maximum retry count exceeded"
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
                )
            try:
                remaining_tool_calls = max(
                    0, budget.max_tool_calls - state.get("tool_calls", 0)
                )
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
                            prior_results={
                                key: SpecialistResult.model_validate(value)
                                for key, value in results.items()
                                if key in task.depends_on
                            },
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
                return task, result, None, None
            except ApprovalRequired as exc:
                return task, None, exc.approval, None
            except Exception as exc:
                return task, None, None, str(exc)

        completed = await asyncio.gather(*(execute_one(task) for task in batch))
        token_input = state.get("input_tokens", 0)
        token_output = state.get("output_tokens", 0)
        cost = state.get("cost_usd", 0.0)
        tool_calls = state.get("tool_calls", 0)
        for task, result, approval, error in completed:
            persisted = self.store.get(Task, task.id)
            if approval:
                # A human checkpoint is a continuation of the same attempt, not
                # a model/tool failure and therefore consumes no retry budget.
                attempts[task.id] = max(0, attempts[task.id] - 1)
                statuses[task.id] = TaskStatus.WAITING_APPROVAL.value
                waiting[task.id] = approval.model_dump(mode="json")
                self.store.update(
                    Task,
                    task.id,
                    {"status": TaskStatus.WAITING_APPROVAL},
                    expected_revision=persisted.revision,
                )
                attempt_entity = self.store.get(
                    AgentAttempt,
                    f"{state['run_id']}:{task.id}:{attempts[task.id] + 1}",
                )
                self.store.update(
                    AgentAttempt,
                    attempt_entity.id,
                    {"status": TaskStatus.WAITING_APPROVAL},
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
                    result = None
                elif (
                    budget.max_tokens is not None
                    and projected_tokens > budget.max_tokens
                ):
                    error = "mission token budget exceeded"
                    result = None
                elif (
                    budget.max_cost_usd is not None
                    and projected_cost > budget.max_cost_usd
                ):
                    error = "mission cost budget exceeded"
                    result = None
            if error:
                errors[task.id] = error
                retrying = attempts[task.id] <= budget.max_retries
                statuses[task.id] = (
                    TaskStatus.PENDING.value if retrying else TaskStatus.FAILED.value
                )
                self.store.update_with_event(
                    Task,
                    task.id,
                    {
                        "status": (
                            TaskStatus.PENDING if retrying else TaskStatus.FAILED
                        ),
                        "completed_at": None if retrying else utc_now(),
                    },
                    expected_revision=persisted.revision,
                    run_id=state["run_id"],
                    event_type=("task.retry_scheduled" if retrying else "task.failed"),
                    event_payload={"task_id": task.id, "error": error},
                    idempotency_key=(
                        f"task:{task.id}:attempt:{attempts[task.id]}:"
                        f"{'retry' if retrying else 'failed'}"
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
                        "status": TaskStatus.FAILED,
                        "error": error,
                        "completed_at": utc_now(),
                    },
                    expected_revision=attempt_entity.revision,
                )
                continue
            assert result is not None
            statuses[task.id] = TaskStatus.COMPLETE.value
            results[task.id] = result.model_dump(mode="json")
            token_input += result.input_tokens
            token_output += result.output_tokens
            cost += result.cost_usd
            tool_calls += result.tool_calls
            self.store.update_with_event(
                Task,
                task.id,
                {"status": TaskStatus.COMPLETE, "completed_at": utc_now()},
                expected_revision=persisted.revision,
                run_id=state["run_id"],
                event_type="task.completed",
                event_payload={
                    "task_id": task.id,
                    "summary": result.summary,
                    "evidence_ids": result.evidence_ids,
                },
                idempotency_key=f"task:{task.id}:attempt:{attempts[task.id]}:completed",
            )
            attempt_entity = self.store.get(
                AgentAttempt,
                f"{state['run_id']}:{task.id}:{attempts[task.id]}",
            )
            self.store.update(
                AgentAttempt,
                attempt_entity.id,
                {
                    "status": TaskStatus.COMPLETE,
                    "output": result.output,
                    "tokens_used": result.input_tokens + result.output_tokens,
                    "cost_usd": result.cost_usd,
                    "completed_at": utc_now(),
                },
                expected_revision=attempt_entity.revision,
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
            "waiting_approvals": waiting,
            "errors": errors,
            "input_tokens": token_input,
            "output_tokens": token_output,
            "cost_usd": cost,
            "tool_calls": tool_calls,
        }

    def _route_after_dispatch(self, state: MissionState) -> str:
        if state.get("waiting_approvals"):
            return "approval"
        if self._needs_verification(state):
            return "verify"
        statuses = state.get("task_status", {}).values()
        if any(status == TaskStatus.FAILED.value for status in statuses):
            return "fail"
        if statuses and all(
            status in {TaskStatus.COMPLETE.value, TaskStatus.CANCELLED.value}
            for status in statuses
        ):
            return "synthesize"
        return "dispatch"

    async def _fail(self, state: MissionState) -> MissionState:
        run = self.store.get(AgentRun, state["run_id"])
        summary = "mission failed: " + "; ".join(
            f"{task_id}: {error}" for task_id, error in state.get("errors", {}).items()
        )
        statuses = state.get("task_status", {})
        plan = state.get("plan", {})
        planned_tasks = plan.get("tasks", []) if isinstance(plan, dict) else []
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
                },
            },
            expected_revision=run.revision,
            run_id=run.id,
            event_type="run.failed",
            event_payload={
                "summary": summary,
                "errors": state.get("errors", {}),
            },
            idempotency_key="run:failed",
        )
        return {"final_summary": summary}

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
        if status in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED}:
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
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED}:
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
        tasks = {task.id: task for task in plan.tasks}
        for task_id, raw in state.get("results", {}).items():
            if task_id in verification:
                continue
            result = SpecialistResult.model_validate(raw)
            verdict = await self.verifier.verify(tasks[task_id], result)
            verification[task_id] = verdict.model_dump(mode="json")
            if not verdict.accepted:
                errors[task_id] = verdict.rationale
            self.store.append_event(
                state["run_id"],
                "task.verified" if verdict.accepted else "task.verification_failed",
                {
                    "task_id": task_id,
                    "accepted": verdict.accepted,
                    "rationale": verdict.rationale,
                    "evidence_ids": verdict.evidence_ids,
                },
                idempotency_key=f"task:{task_id}:verification",
            )
        return {"verification": verification, "errors": errors}

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
        if elapsed > budget.max_duration_seconds:
            raise BudgetExceeded("mission duration budget exceeded")
        total_tokens = state.get("input_tokens", 0) + state.get("output_tokens", 0)
        if budget.max_tokens is not None and total_tokens > budget.max_tokens:
            raise BudgetExceeded("mission token budget exceeded")
        if (
            budget.max_cost_usd is not None
            and state.get("cost_usd", 0) > budget.max_cost_usd
        ):
            raise BudgetExceeded("mission cost budget exceeded")
        if state.get("tool_calls", 0) > budget.max_tool_calls:
            raise BudgetExceeded("mission tool-call budget exceeded")


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
    "SpecialistResult",
    "SpecialistRole",
    "StaticSpecialist",
    "StaticSupervisor",
    "VerificationResult",
    "sqlite_mission_runtime",
]
