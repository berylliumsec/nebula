"""Bounded API-facing lifecycle for analysis-only Nebula missions."""

from __future__ import annotations

from .diagnostics import (
    create_diagnostic_task,
    gather_diagnostic,
    record_caught_exception,
)

import asyncio
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .domain import (
    AgentAttempt,
    AgentRun,
    Approval,
    ApprovalStatus,
    Engagement,
    McpApprovalMode,
    ProviderProfile,
    RiskClass,
    RunBudget,
    RunBackend,
    RunStatus,
    ScopePolicy,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    utc_now,
)
from .orchestration import (
    MissionRuntime,
    MissionPlan,
    ModelSpecialist,
    PlannedTask,
    Specialist,
    SpecialistResult,
    SpecialistRole,
    StaticSupervisor,
    Supervisor,
    sqlite_mission_runtime,
)
from .providers import ModelProvider, ProviderError, provider_from_profile
from .privacy import ProviderPrivacyViolation, validate_engagement_provider_privacy
from .mcp import McpProbeError, mcp_tool_runtime_name, resolve_mcp_profiles
from .storage import ConflictError, NebulaStore

MAX_API_MISSION_DURATION_SECONDS = 3_600
MAX_API_MISSION_TOKENS = 200_000
MAX_API_MISSION_COST_USD = 100.0
MAX_API_MISSION_RETRIES = 2

_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETE,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}
_TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETE,
    TaskStatus.BLOCKED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}


class MissionServiceError(RuntimeError):
    """Base class for operator-safe lifecycle errors."""


class MissionConfigurationError(MissionServiceError):
    """The requested provider, model, or budget is not configured safely."""


class MissionCapacityError(MissionServiceError):
    """The local Core has reached its bounded background mission capacity."""


class MissionStateError(MissionServiceError):
    """The requested lifecycle transition is not valid for the durable run."""


class MissionServiceUnavailable(MissionServiceError):
    """The local mission service cannot accept work."""


class ExplicitStageSupervisor(StaticSupervisor):
    """Turn an operator-authored stage list into a durable sequential graph."""

    def __init__(self, stages: list[dict[str, str]]) -> None:
        self.stages = stages

    async def plan(
        self, objective: str, context: Mapping[str, object], budget: RunBudget
    ) -> MissionPlan:
        del context, budget
        tasks: list[PlannedTask] = []
        previous: list[str] = []
        for stage in self.stages:
            task = PlannedTask(
                role=SpecialistRole.SCOPE_PLANNING,
                title=stage["title"],
                instructions=(
                    f"Overall mission objective: {objective}\n"
                    f"Current stage objective: {stage['objective']}"
                ),
                depends_on=previous,
                risk_class=RiskClass.LOCAL_READ,
            )
            tasks.append(task)
            previous = [task.id]
        return MissionPlan(
            summary="Execute the operator-authored mission stages in order",
            rationale="Stage boundaries and ordering were explicitly selected by the operator.",
            tasks=tasks,
        )

    async def synthesize(
        self,
        objective: str,
        plan: MissionPlan,
        results: Mapping[str, SpecialistResult],
    ) -> str:
        sections = ["## Mission result", objective]
        by_id = {task.id: task for task in plan.tasks}
        for task_id, result in results.items():
            task = by_id.get(task_id)
            sections.extend([f"### {task.title if task else 'Stage'}", result.summary])
        return "\n\n".join(item for item in sections if item)


ProviderFactory = Callable[[ProviderProfile], ModelProvider]
RuntimeFactory = Callable[..., AbstractAsyncContextManager[MissionRuntime]]


@dataclass(frozen=True)
class MissionComponents:
    """Fully validated runtime capabilities for one durable mission."""

    supervisor: Supervisor
    specialists: Mapping[SpecialistRole, Specialist]
    context: dict[str, object] = field(default_factory=dict)


ToolComponentsFactory = Callable[[AgentRun, ModelProvider], MissionComponents]


def default_checkpoint_path(store: NebulaStore) -> Path | None:
    """Keep local mission checkpoints beside the authoritative SQLite database."""

    if store.database.engine.dialect.name != "sqlite":
        return None
    database_path = store.database.engine.url.database
    if not database_path or database_path == ":memory:":
        return None
    return (
        Path(database_path).expanduser().resolve().with_name("mission-checkpoints.db")
    )


class MissionService:
    """Own tracked background missions and make every transition durable."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        checkpoint_path: str | Path | None = None,
        provider_factory: ProviderFactory = provider_from_profile,
        runtime_factory: RuntimeFactory = sqlite_mission_runtime,
        tool_components_factory: ToolComponentsFactory | None = None,
        max_active_missions: int = 4,
        cancellation_timeout_seconds: float = 5.0,
    ) -> None:
        if max_active_missions < 1:
            raise ValueError("max_active_missions must be positive")
        if cancellation_timeout_seconds <= 0:
            raise ValueError("cancellation_timeout_seconds must be positive")
        self.store = store
        self.checkpoint_path = (
            Path(checkpoint_path).expanduser().resolve()
            if checkpoint_path is not None
            else default_checkpoint_path(store)
        )
        self.provider_factory = provider_factory
        self.runtime_factory = runtime_factory
        self.tool_components_factory = tool_components_factory
        self.max_active_missions = max_active_missions
        self.cancellation_timeout_seconds = cancellation_timeout_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduled_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_reasons: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def active_run_ids(self) -> frozenset[str]:
        return frozenset(self._tasks)

    async def startup(self) -> None:
        """Fail stale API-owned runs whose work cannot survive a Core restart."""

        async with self._lock:
            if self._closed:
                raise MissionServiceUnavailable("mission service is shut down")
            self._discard_finished_tasks()
            owned_run_ids = set(self._tasks)

        offset = 0
        while True:
            page = self.store.list_entities(AgentRun, offset=offset, limit=1_000)
            for run in page:
                if run.backend != RunBackend.NATIVE:
                    continue
                scheduled_for = run.metadata.get("scheduled_for")
                if (
                    run.metadata.get("origin") == "api"
                    and run.status == RunStatus.QUEUED
                    and isinstance(scheduled_for, str)
                ):
                    try:
                        profile = self.store.get(
                            ProviderProfile, run.supervisor_provider_id or ""
                        )
                        provider = self.provider_factory(profile)
                        task = create_diagnostic_task(
                            self._scheduled_execute(
                                run.id, provider, datetime.fromisoformat(scheduled_for)
                            ),
                            feature="missions",
                            event_code="missions.scheduled",
                            failure_message="A scheduled Mission failed before start.",
                            name=f"nebula-scheduled-mission-{run.id}",
                        )
                        self._scheduled_tasks[run.id] = task
                        continue
                    except Exception as exc:
                        # diagnostic-expected: the durable run is finalized with the safe failure.
                        self._finalize_failed(
                            run.id,
                            f"scheduled mission could not be restored: {self._safe_error(exc)}",
                        )
                        continue
                if (
                    run.id in owned_run_ids
                    or run.status in _TERMINAL_RUN_STATUSES
                    or run.metadata.get("origin") != "api"
                ):
                    continue
                self._reconcile_interrupted_run(run.id)
            if len(page) < 1_000:
                break
            offset += len(page)

    async def start_mission(
        self,
        *,
        engagement_id: str,
        name: str | None = None,
        objective: str,
        provider_id: str,
        model: str,
        budget: RunBudget,
        stages: list[dict[str, str]] | None = None,
        scheduled_for: datetime | None = None,
        repeat_interval_seconds: int | None = None,
        retry_of_run_id: str | None = None,
        recurrence_of_run_id: str | None = None,
        series_id: str | None = None,
        tool_names: list[str] | None = None,
        mcp_server_ids: list[str] | None = None,
        allow_cloud_tool_results: bool = False,
        actor_id: str = "system",
    ) -> AgentRun:
        """Validate, queue, and schedule one explicit analysis-only mission."""

        clean_objective = objective.strip()
        clean_provider_id = provider_id.strip()
        clean_model = model.strip()
        selected_tools = list(dict.fromkeys(tool_names or ()))
        selected_stages = [
            {"title": item["title"].strip(), "objective": item["objective"].strip()}
            for item in (stages or [])
            if item.get("title", "").strip() and item.get("objective", "").strip()
        ]
        if scheduled_for is not None and scheduled_for.tzinfo is None:
            raise MissionConfigurationError(
                "scheduled mission time must include a timezone"
            )
        if repeat_interval_seconds is not None and repeat_interval_seconds < 3_600:
            raise MissionConfigurationError(
                "repeating missions must be at least one hour apart"
            )
        try:
            mcp_profiles = resolve_mcp_profiles(
                self.store, list(dict.fromkeys(mcp_server_ids or ()))
            )
        except (McpProbeError, ValueError) as exc:
            raise MissionConfigurationError(str(exc)) from exc
        selected_mcp_tools = [
            mcp_tool_runtime_name(profile.id, tool.name)
            for profile in mcp_profiles
            for tool in profile.capabilities.tools
            if (not profile.enabled_tools or tool.name in profile.enabled_tools)
            and tool.name not in profile.disabled_tools
            and profile.tool_overrides.get(tool.name) != McpApprovalMode.DENY
        ]
        selected_action_tools = [*selected_tools, *selected_mcp_tools]
        if not clean_objective:
            raise MissionConfigurationError("mission objective cannot be empty")
        if not clean_provider_id or not clean_model:
            raise MissionConfigurationError(
                "missions require an explicit provider and model"
            )
        if any(
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", name)
            for name in selected_tools
        ):
            raise MissionConfigurationError("mission tool names are invalid")
        self._validate_budget(budget, tool_names=selected_action_tools)
        if self.checkpoint_path is None:
            raise MissionServiceUnavailable(
                "API missions require a file-backed local SQLite checkpoint store"
            )

        engagement = self.store.get(Engagement, engagement_id)
        profile = self.store.get(ProviderProfile, clean_provider_id)
        if not profile.enabled:
            raise MissionConfigurationError(
                f"provider profile {clean_provider_id!r} is disabled"
            )
        if profile.model_allowlist and clean_model not in profile.model_allowlist:
            raise MissionConfigurationError(
                f"model {clean_model!r} is outside the provider profile allowlist"
            )
        try:
            provider = self.provider_factory(profile)
        except (ProviderError, ValueError) as exc:
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_001",
                "A handled missions operation raised an exception.",
                exc,
                stage="missions",
            )
            raise MissionConfigurationError(str(exc)) from exc
        if provider.config.id != profile.id:
            raise MissionConfigurationError(
                "provider runtime identity does not match the selected profile"
            )
        if not provider.config.enabled:
            raise MissionConfigurationError(
                f"provider profile {clean_provider_id!r} is disabled"
            )
        if selected_action_tools:
            if not profile.tools_verified_for(clean_model):
                raise MissionConfigurationError(
                    "executable missions require reliable strict structured tool calling "
                    "and successful verification for "
                    f"the exact selected model {clean_model!r}"
                )
            if not engagement.scope_policy_id:
                raise MissionConfigurationError(
                    "executable missions require an engagement scope policy"
                )
            scope = self.store.get(ScopePolicy, engagement.scope_policy_id)
            if scope.engagement_id != engagement.id:
                raise MissionConfigurationError(
                    "engagement scope policy ownership is inconsistent"
                )
            if budget.max_concurrency > scope.max_concurrency:
                raise MissionConfigurationError(
                    "mission concurrency exceeds the engagement scope policy"
                )
        try:
            validate_engagement_provider_privacy(self.store, engagement, provider)
        except ProviderPrivacyViolation as exc:
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_002",
                "A handled missions operation raised an exception.",
                exc,
                stage="missions",
            )
            raise MissionConfigurationError(str(exc)) from exc
        if mcp_profiles and not provider.config.local:
            if not profile.privacy.permits_sensitive_data:
                raise MissionConfigurationError(
                    "provider profile does not permit MCP result transfer"
                )
            if not allow_cloud_tool_results:
                raise MissionConfigurationError(
                    "cloud MCP result transfer requires explicit confirmation"
                )

        run = AgentRun(
            id=str(uuid4()),
            engagement_id=engagement_id,
            objective=clean_objective,
            status=RunStatus.QUEUED,
            supervisor_provider_id=profile.id,
            supervisor_model=clean_model,
            budget=budget,
            runtime_snapshot={
                "mcp_server_ids": [item.id for item in mcp_profiles],
                "mcp_snapshot": [item.model_dump(mode="json") for item in mcp_profiles],
                "remote_mcp_confirmed": allow_cloud_tool_results,
            },
            metadata={
                "name": name.strip() if name and name.strip() else clean_objective,
                "analysis_only": not selected_action_tools,
                "origin": "api",
                **({"stages": selected_stages} if selected_stages else {}),
                **(
                    {"scheduled_for": scheduled_for.isoformat()}
                    if scheduled_for
                    else {}
                ),
                **(
                    {"repeat_interval_seconds": repeat_interval_seconds}
                    if repeat_interval_seconds
                    else {}
                ),
                **({"retry_of_run_id": retry_of_run_id} if retry_of_run_id else {}),
                **(
                    {"recurrence_of_run_id": recurrence_of_run_id}
                    if recurrence_of_run_id
                    else {}
                ),
                **({"series_id": series_id} if series_id else {}),
                **(
                    {
                        "tool_names": selected_action_tools,
                        "command_tool_names": selected_tools,
                        "mcp_tool_names": selected_mcp_tools,
                    }
                    if selected_action_tools
                    else {}
                ),
            },
        )
        if selected_action_tools:
            if self.tool_components_factory is None:
                raise MissionServiceUnavailable(
                    "tool mission runtime is not configured"
                )
            try:
                # Build the locked registry before persistence so arbitrary
                # capability names and unavailable runners fail as explicit
                # configuration errors rather than queued background work.
                self.tool_components_factory(run, provider)
            except MissionServiceError as caught_error:
                record_caught_exception(
                    "missions",
                    "missions.missions.caught_failure_003",
                    "A handled missions operation raised an exception.",
                    caught_error,
                    stage="missions",
                )
                raise
            except Exception as exc:
                record_caught_exception(
                    "missions",
                    "missions.missions.caught_failure_004",
                    "A handled missions operation raised an exception.",
                    exc,
                    stage="missions",
                )
                raise MissionConfigurationError(
                    f"tool mission preflight failed: {self._safe_error(exc)}"
                ) from exc

        async with self._lock:
            if self._closed:
                raise MissionServiceUnavailable("mission service is shutting down")
            self._discard_finished_tasks()
            if len(self._tasks) >= self.max_active_missions:
                raise MissionCapacityError(
                    "local mission concurrency limit has been reached"
                )
            run, _ = self.store.create_with_event(
                run,
                run_id=run.id,
                event_type="run.queued",
                event_payload={
                    "objective": clean_objective,
                    "provider_id": profile.id,
                    "model": clean_model,
                    "budget": budget.model_dump(mode="json"),
                    "analysis_only": not selected_action_tools,
                    **(
                        {
                            "tool_names": selected_action_tools,
                            "command_tool_names": selected_tools,
                            "mcp_server_ids": [item.id for item in mcp_profiles],
                        }
                        if selected_action_tools
                        else {}
                    ),
                },
                actor_id=actor_id,
                idempotency_key="run:queued",
            )
            if scheduled_for is not None and scheduled_for > utc_now():
                task = create_diagnostic_task(
                    self._scheduled_execute(run.id, provider, scheduled_for),
                    feature="missions",
                    event_code="missions.scheduled",
                    failure_message="A scheduled Mission failed before start.",
                    name=f"nebula-scheduled-mission-{run.id}",
                )
                self._scheduled_tasks[run.id] = task
            else:
                task = create_diagnostic_task(
                    self._execute(run, provider),
                    feature="missions",
                    event_code="missions.run",
                    failure_message="A mission background task stopped unexpectedly.",
                    name=f"nebula-mission-{run.id}",
                )
                self._tasks[run.id] = task
            return run

    async def _scheduled_execute(
        self, run_id: str, provider: ModelProvider, scheduled_for: datetime
    ) -> None:
        try:
            delay = max(0.0, (scheduled_for - utc_now()).total_seconds())
            if delay:
                await asyncio.sleep(delay)
            run = self.store.get(AgentRun, run_id)
            if run.status != RunStatus.QUEUED or self._closed:
                return
            async with self._lock:
                self._scheduled_tasks.pop(run_id, None)
                self._tasks[run_id] = asyncio.current_task()  # type: ignore[assignment]
            await self._execute(run, provider)
        except asyncio.CancelledError:
            # diagnostic-expected: cancellation is translated into durable mission state below.
            if not self._closed:
                latest = self.store.get(AgentRun, run_id)
                if latest.status == RunStatus.CANCELLING:
                    reason, actor = self._cancel_reasons.get(
                        run_id, ("Stopped before scheduled start", "operator")
                    )
                    self._finalize_cancelled(run_id, reason, actor)
            return
        finally:
            self._scheduled_tasks.pop(run_id, None)

    async def stop_mission(
        self,
        run_id: str,
        *,
        reason: str = "Stopped by operator",
        actor_id: str = "system",
    ) -> AgentRun:
        """Cancel actual work or durably cancel a dormant non-terminal run."""

        clean_reason = reason.strip() or "Stopped by operator"
        run = self.store.get(AgentRun, run_id)
        if run.status in _TERMINAL_RUN_STATUSES:
            raise MissionStateError(
                f"run {run.id} is already terminal ({run.status.value})"
            )
        async with self._lock:
            task = self._tasks.get(run.id) or self._scheduled_tasks.get(run.id)
        if (task is None or task.done()) and run.metadata.get("origin") != "api":
            raise MissionStateError(
                "run is not owned by this API mission service; cancellation "
                "cannot be confirmed"
            )
        if run.status != RunStatus.CANCELLING:
            try:
                run, _ = self.store.update_with_event(
                    AgentRun,
                    run.id,
                    {"status": RunStatus.CANCELLING},
                    expected_revision=run.revision,
                    run_id=run.id,
                    event_type="run.stop_requested",
                    event_payload={"reason": clean_reason},
                    actor_id=actor_id,
                    idempotency_key="run:stop_requested",
                )
            except ConflictError as exc:
                record_caught_exception(
                    "missions",
                    "missions.missions.caught_failure_005",
                    "A handled missions operation raised an exception.",
                    exc,
                    stage="missions",
                )
                latest = self.store.get(AgentRun, run.id)
                if latest.status in _TERMINAL_RUN_STATUSES:
                    raise MissionStateError(
                        f"run {run.id} is already terminal ({latest.status.value})"
                    ) from exc
                raise

        async with self._lock:
            task = self._tasks.get(run.id) or self._scheduled_tasks.get(run.id)
            if task is not None and not task.done():
                self._cancel_reasons[run.id] = (clean_reason, actor_id)
                task.cancel()

        if task is None or task.done():
            return self._finalize_cancelled(run.id, clean_reason, actor_id)
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self.cancellation_timeout_seconds
            )
        except asyncio.CancelledError:
            # diagnostic-expected: operator cancellation is finalized as the requested state.
            return self._finalize_cancelled(run.id, clean_reason, actor_id)
        except asyncio.TimeoutError as caught_error:
            # CANCELLING is truthful while a provider ignores cancellation.
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_006",
                "A handled missions operation raised an exception.",
                caught_error,
                stage="missions",
            )
            return self.store.get(AgentRun, run.id)
        return self.store.get(AgentRun, run.id)

    async def resume_after_approval(
        self, approval: Approval, *, actor_id: str = "system"
    ) -> AgentRun:
        """Resume the durable graph using only the persisted operator decision."""

        if approval.status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.EDITED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.CANCELLED,
        }:
            raise MissionStateError("approval does not contain a terminal decision")
        run = self.store.get(AgentRun, approval.run_id)
        if run.status != RunStatus.WAITING_APPROVAL:
            raise MissionStateError(
                f"run {run.id} is not waiting for approval ({run.status.value})"
            )
        if not run.supervisor_provider_id:
            raise MissionStateError("approval run has no provider profile")
        profile = self.store.get(ProviderProfile, run.supervisor_provider_id)
        provider = self.provider_factory(profile)
        response: dict[str, object] = {
            "approval_id": approval.id,
            "status": approval.status.value,
            "decided_by": approval.decided_by,
            "decided_at": (
                approval.decided_at.isoformat() if approval.decided_at else None
            ),
        }
        async with self._lock:
            if self._closed:
                raise MissionServiceUnavailable("mission service is shutting down")
            self._discard_finished_tasks()
            if run.id in self._tasks:
                raise MissionStateError("mission already has active work")
            if len(self._tasks) >= self.max_active_missions:
                raise MissionCapacityError(
                    "local mission concurrency limit has been reached"
                )
            resumed, _ = self.store.update_with_event(
                AgentRun,
                run.id,
                {
                    "status": RunStatus.RUNNING,
                    "metadata": {**run.metadata, "waiting_approval": False},
                },
                expected_revision=run.revision,
                run_id=run.id,
                event_type="run.approval_resume_queued",
                event_payload={"approval_id": approval.id},
                actor_id=actor_id,
                idempotency_key=f"run:{run.id}:approval:{approval.id}:resume",
            )
            task = create_diagnostic_task(
                self._resume_execute(resumed, provider, response),
                feature="missions",
                event_code="missions.resume",
                failure_message="A resumed mission task stopped unexpectedly.",
                name=f"nebula-mission-resume-{run.id}",
            )
            self._tasks[run.id] = task
            return resumed

    async def shutdown(self) -> None:
        """Request cancellation for every owned task and wait only a bounded time."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            scheduled = [
                task for task in self._scheduled_tasks.values() if not task.done()
            ]
            self._scheduled_tasks.clear()
            run_ids = [
                run_id for run_id, task in self._tasks.items() if not task.done()
            ]
        for task in scheduled:
            task.cancel()
        if scheduled:
            await gather_diagnostic(
                *scheduled,
                feature="missions",
                event_code="missions.shutdown.scheduled",
                failure_message="A scheduled Mission timer did not stop cleanly.",
                stage="shutdown",
            )
        if not run_ids:
            return
        await gather_diagnostic(
            *(
                self.stop_mission(
                    run_id,
                    reason="Nebula Core is shutting down",
                    actor_id="system",
                )
                for run_id in run_ids
            ),
            feature="missions",
            event_code="missions.shutdown.stop_failed",
            failure_message="A mission could not be stopped cleanly during shutdown.",
            stage="shutdown",
        )

    async def _execute(self, queued: AgentRun, provider: ModelProvider) -> None:
        try:
            current = self.store.get(AgentRun, queued.id)
            if current.status == RunStatus.CANCELLING:
                reason, actor = self._cancel_reasons.get(
                    queued.id, ("Stopped before mission start", "operator")
                )
                self._finalize_cancelled(queued.id, reason, actor)
                return
            if current.status != RunStatus.QUEUED:
                raise MissionStateError(
                    f"run {current.id} cannot start from {current.status.value}"
                )
            started_at = utc_now()
            self.store.update(
                AgentRun,
                current.id,
                {"status": RunStatus.PLANNING, "started_at": started_at},
                expected_revision=current.revision,
            )
            components = self._components(queued, provider)
            assert self.checkpoint_path is not None
            async with self.runtime_factory(
                checkpoint_path=self.checkpoint_path,
                store=self.store,
                supervisor=components.supervisor,
                specialists=components.specialists,
            ) as runtime:
                try:
                    state = await runtime.start(
                        engagement_id=queued.engagement_id,
                        objective=queued.objective,
                        budget=queued.budget,
                        run_id=queued.id,
                        provider_id=queued.supervisor_provider_id,
                        model=queued.supervisor_model,
                        context=components.context,
                    )
                except asyncio.CancelledError as exc:
                    # LangGraph attaches its background-executor exit task to the
                    # cancellation. It must finish before the saver connection
                    # leaves this context or deferred checkpoint writes race a
                    # closed SQLite handle.
                    record_caught_exception(
                        "missions",
                        "missions.missions.caught_failure_007",
                        "A handled missions operation raised an exception.",
                        exc,
                        stage="missions",
                    )
                    await self._await_graph_cleanup(exc)
                    raise
            latest = self.store.get(AgentRun, queued.id)
            if latest.status not in _TERMINAL_RUN_STATUSES:
                if state.get("__interrupt__"):
                    metadata = dict(latest.metadata)
                    metadata["waiting_approval"] = True
                    self.store.update_with_event(
                        AgentRun,
                        latest.id,
                        {
                            "status": RunStatus.WAITING_APPROVAL,
                            "metadata": metadata,
                        },
                        expected_revision=latest.revision,
                        run_id=latest.id,
                        event_type="run.waiting_approval",
                        event_payload={
                            "reason": "mission requires an operator decision"
                        },
                        actor_id="system",
                        idempotency_key="run:waiting_approval",
                    )
                else:
                    self._finalize_failed(
                        latest.id, "mission ended without a terminal result"
                    )
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_008",
                "A handled missions operation raised an exception.",
                caught_error,
                stage="missions",
            )
            reason, actor = self._cancel_reasons.get(
                queued.id, ("Mission background task was cancelled", "system")
            )
            self._finalize_cancelled(queued.id, reason, actor)
        except Exception as exc:
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_009",
                "A handled missions operation raised an exception.",
                exc,
                stage="missions",
            )
            latest = self.store.get(AgentRun, queued.id)
            if latest.status == RunStatus.CANCELLING:
                reason, actor = self._cancel_reasons.get(
                    queued.id, ("Stopped by operator", "operator")
                )
                self._finalize_cancelled(queued.id, reason, actor)
            elif latest.status not in _TERMINAL_RUN_STATUSES:
                self._finalize_failed(queued.id, self._safe_error(exc))
        finally:
            recurrence_source = self.store.get(AgentRun, queued.id)
            async with self._lock:
                current_task = asyncio.current_task()
                if self._tasks.get(queued.id) is current_task:
                    self._tasks.pop(queued.id, None)
                self._cancel_reasons.pop(queued.id, None)
            if recurrence_source.status == RunStatus.COMPLETE and not self._closed:
                await self._schedule_recurrence(recurrence_source)

    async def _schedule_recurrence(self, prior: AgentRun) -> None:
        interval = prior.metadata.get("repeat_interval_seconds")
        if not isinstance(interval, int) or interval < 3_600:
            return
        await self.start_mission(
            engagement_id=prior.engagement_id,
            name=str(prior.metadata.get("name") or prior.objective),
            objective=prior.objective,
            provider_id=prior.supervisor_provider_id or "",
            model=prior.supervisor_model or "",
            budget=prior.budget,
            stages=prior.metadata.get("stages")
            if isinstance(prior.metadata.get("stages"), list)
            else [],
            scheduled_for=utc_now() + timedelta(seconds=interval),
            repeat_interval_seconds=interval,
            recurrence_of_run_id=prior.id,
            series_id=str(prior.metadata.get("series_id") or prior.id),
            tool_names=list(prior.metadata.get("command_tool_names") or []),
            mcp_server_ids=list(prior.runtime_snapshot.get("mcp_server_ids") or []),
            allow_cloud_tool_results=prior.runtime_snapshot.get("remote_mcp_confirmed")
            is True,
            actor_id="system",
        )

    async def _resume_execute(
        self,
        run: AgentRun,
        provider: ModelProvider,
        response: dict[str, object],
    ) -> None:
        try:
            components = self._components(run, provider)
            assert self.checkpoint_path is not None
            async with self.runtime_factory(
                checkpoint_path=self.checkpoint_path,
                store=self.store,
                supervisor=components.supervisor,
                specialists=components.specialists,
            ) as runtime:
                try:
                    state = await runtime.resume(run.id, response)
                except asyncio.CancelledError as exc:
                    record_caught_exception(
                        "missions",
                        "missions.missions.caught_failure_010",
                        "A handled missions operation raised an exception.",
                        exc,
                        stage="missions",
                    )
                    await self._await_graph_cleanup(exc)
                    raise
            latest = self.store.get(AgentRun, run.id)
            if latest.status not in _TERMINAL_RUN_STATUSES:
                if state.get("__interrupt__"):
                    self.store.update_with_event(
                        AgentRun,
                        latest.id,
                        {
                            "status": RunStatus.WAITING_APPROVAL,
                            "metadata": {
                                **latest.metadata,
                                "waiting_approval": True,
                            },
                        },
                        expected_revision=latest.revision,
                        run_id=latest.id,
                        event_type="run.waiting_approval",
                        event_payload={
                            "reason": "mission requires another operator decision"
                        },
                        actor_id="system",
                        idempotency_key=(
                            f"run:{latest.id}:waiting_approval:"
                            f"{response.get('approval_id')}"
                        ),
                    )
                else:
                    self._finalize_failed(
                        latest.id, "resumed mission ended without a terminal result"
                    )
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_011",
                "A handled missions operation raised an exception.",
                caught_error,
                stage="missions",
            )
            reason, actor = self._cancel_reasons.get(
                run.id, ("Mission background task was cancelled", "system")
            )
            self._finalize_cancelled(run.id, reason, actor)
        except Exception as exc:
            record_caught_exception(
                "missions",
                "missions.missions.caught_failure_012",
                "A handled missions operation raised an exception.",
                exc,
                stage="missions",
            )
            latest = self.store.get(AgentRun, run.id)
            if latest.status == RunStatus.CANCELLING:
                reason, actor = self._cancel_reasons.get(
                    run.id, ("Stopped by operator", "operator")
                )
                self._finalize_cancelled(run.id, reason, actor)
            elif latest.status not in _TERMINAL_RUN_STATUSES:
                self._finalize_failed(run.id, self._safe_error(exc))
        finally:
            async with self._lock:
                current_task = asyncio.current_task()
                if self._tasks.get(run.id) is current_task:
                    self._tasks.pop(run.id, None)
                self._cancel_reasons.pop(run.id, None)

    def _components(self, run: AgentRun, provider: ModelProvider) -> MissionComponents:
        tool_names = run.metadata.get("tool_names", [])
        if tool_names:
            if self.tool_components_factory is None:
                raise MissionServiceUnavailable(
                    "tool mission runtime is not configured"
                )
            components = self.tool_components_factory(run, provider)
            if not components.specialists:
                raise MissionConfigurationError(
                    "tool mission did not produce any bounded specialists"
                )
            if run.metadata.get("stages"):
                components.context["stages"] = run.metadata["stages"]
            return components
        max_tokens = run.budget.max_tokens or 2_048
        specialist = ModelSpecialist(
            provider,
            model=run.supervisor_model,
            max_output_tokens=min(2_048, max_tokens),
        )
        stages = run.metadata.get("stages")
        return MissionComponents(
            supervisor=(
                ExplicitStageSupervisor(stages)
                if isinstance(stages, list) and stages
                else StaticSupervisor()
            ),
            specialists={SpecialistRole.SCOPE_PLANNING: specialist},
        )

    def _finalize_cancelled(self, run_id: str, reason: str, actor_id: str) -> AgentRun:
        run = self.store.get(AgentRun, run_id)
        if run.status == RunStatus.CANCELLED:
            return run
        if run.status in {RunStatus.COMPLETE, RunStatus.FAILED}:
            return run
        self._cancel_open_work(run, reason)
        run = self.store.get(AgentRun, run_id)
        cancelled, _ = self.store.update_with_event(
            AgentRun,
            run.id,
            {"status": RunStatus.CANCELLED, "completed_at": utc_now()},
            expected_revision=run.revision,
            run_id=run.id,
            event_type="run.cancelled",
            event_payload={"reason": reason},
            actor_id=actor_id,
            idempotency_key="run:cancelled",
        )
        return cancelled

    def _finalize_failed(self, run_id: str, error: str) -> AgentRun:
        run = self.store.get(AgentRun, run_id)
        if run.status in _TERMINAL_RUN_STATUSES:
            return run
        self._fail_open_work(run, error)
        run = self.store.get(AgentRun, run_id)
        failed, _ = self.store.update_with_event(
            AgentRun,
            run.id,
            {
                "status": RunStatus.FAILED,
                "completed_at": utc_now(),
                "metadata": {**run.metadata, "error": error},
            },
            expected_revision=run.revision,
            run_id=run.id,
            event_type="run.failed",
            event_payload={"summary": "mission failed", "error": error},
            actor_id="system",
            idempotency_key="run:service_failed",
        )
        return failed

    def _reconcile_interrupted_run(self, run_id: str) -> AgentRun:
        error = (
            "Nebula Core restarted while this API mission was active; "
            "completion cannot be confirmed"
        )
        for _ in range(3):
            current = self.store.get(AgentRun, run_id)
            if current.status in _TERMINAL_RUN_STATUSES:
                return current
            if current.metadata.get("origin") != "api":
                return current
            try:
                return self._finalize_failed(run_id, error)
            except ConflictError as caught_error:
                record_caught_exception(
                    "missions",
                    "missions.missions.caught_failure_013",
                    "A handled missions operation raised an exception.",
                    caught_error,
                    stage="missions",
                )
                continue
        raise MissionServiceUnavailable(
            f"could not reconcile interrupted API mission {run_id!r}"
        )

    def _cancel_open_work(self, run: AgentRun, reason: str) -> None:
        for task in self._run_tasks(run):
            if task.run_id != run.id or task.status in _TERMINAL_TASK_STATUSES:
                continue
            self.store.update_with_event(
                Task,
                task.id,
                {"status": TaskStatus.CANCELLED, "completed_at": utc_now()},
                expected_revision=task.revision,
                run_id=run.id,
                event_type="task.cancelled",
                event_payload={"task_id": task.id, "reason": reason},
                actor_id="system",
                idempotency_key=f"task:{task.id}:service_cancelled",
            )
        for attempt in self._run_attempts(run):
            if attempt.run_id != run.id or attempt.status in _TERMINAL_TASK_STATUSES:
                continue
            self.store.update(
                AgentAttempt,
                attempt.id,
                {
                    "status": TaskStatus.CANCELLED,
                    "completed_at": utc_now(),
                    "error": reason,
                },
                expected_revision=attempt.revision,
            )
        for call in self._run_tool_calls(run):
            if call.status in {
                ToolCallStatus.COMPLETE,
                ToolCallStatus.FAILED,
                ToolCallStatus.DENIED,
                ToolCallStatus.CANCELLED,
            }:
                continue
            self.store.update_with_event(
                ToolCall,
                call.id,
                {
                    "status": ToolCallStatus.CANCELLED,
                    "completed_at": utc_now(),
                    "error": reason,
                },
                expected_revision=call.revision,
                run_id=run.id,
                event_type="tool.cancelled",
                event_payload={"tool_call_id": call.id, "reason": reason},
                actor_id="system",
                idempotency_key=f"tool:{call.id}:mission_cancelled",
            )

    def _fail_open_work(self, run: AgentRun, error: str) -> None:
        for task in self._run_tasks(run):
            if task.run_id != run.id or task.status in _TERMINAL_TASK_STATUSES:
                continue
            self.store.update_with_event(
                Task,
                task.id,
                {"status": TaskStatus.FAILED, "completed_at": utc_now()},
                expected_revision=task.revision,
                run_id=run.id,
                event_type="task.failed",
                event_payload={"task_id": task.id, "error": error},
                actor_id="system",
                idempotency_key=f"task:{task.id}:service_failed",
            )
        for attempt in self._run_attempts(run):
            if attempt.run_id != run.id or attempt.status in _TERMINAL_TASK_STATUSES:
                continue
            self.store.update(
                AgentAttempt,
                attempt.id,
                {
                    "status": TaskStatus.FAILED,
                    "completed_at": utc_now(),
                    "error": error,
                },
                expected_revision=attempt.revision,
            )

    def _run_tasks(self, run: AgentRun) -> Iterator[Task]:
        offset = 0
        while True:
            page = self.store.list_entities(
                Task,
                engagement_id=run.engagement_id,
                offset=offset,
                limit=1_000,
            )
            yield from (task for task in page if task.run_id == run.id)
            if len(page) < 1_000:
                return
            offset += len(page)

    def _run_attempts(self, run: AgentRun) -> Iterator[AgentAttempt]:
        offset = 0
        while True:
            page = self.store.list_entities(
                AgentAttempt,
                engagement_id=run.engagement_id,
                offset=offset,
                limit=1_000,
            )
            yield from (attempt for attempt in page if attempt.run_id == run.id)
            if len(page) < 1_000:
                return
            offset += len(page)

    def _run_tool_calls(self, run: AgentRun) -> Iterator[ToolCall]:
        offset = 0
        while True:
            page = self.store.list_entities(
                ToolCall,
                engagement_id=run.engagement_id,
                offset=offset,
                limit=1_000,
            )
            yield from (call for call in page if call.run_id == run.id)
            if len(page) < 1_000:
                return
            offset += len(page)

    def _discard_finished_tasks(self) -> None:
        for run_id, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(run_id, None)
                self._cancel_reasons.pop(run_id, None)

    @staticmethod
    async def _await_graph_cleanup(exc: asyncio.CancelledError) -> None:
        for value in exc.args:
            if not isinstance(value, asyncio.Future):
                continue
            try:
                await asyncio.shield(value)
            except (asyncio.CancelledError, Exception) as caught_error:
                # Cancellation remains authoritative; cleanup failures are
                # handled by durable run/task terminalization below.
                record_caught_exception(
                    "missions",
                    "missions.missions.caught_failure_014",
                    "A handled missions operation raised an exception.",
                    caught_error,
                    stage="missions",
                )
                pass

    def _validate_budget(self, budget: RunBudget, *, tool_names: list[str]) -> None:
        if tool_names:
            if self.tool_components_factory is None:
                raise MissionConfigurationError(
                    "executable mission tools are unavailable in this Core"
                )
            if budget.max_tool_calls is not None and budget.max_tool_calls < 1:
                raise MissionConfigurationError(
                    "executable missions require a positive tool-call budget"
                )
            if budget.max_tool_calls is not None and budget.max_tool_calls > 100:
                raise MissionConfigurationError("mission tool-call budget exceeds 100")
            if budget.max_concurrency > 2 or budget.max_delegation_depth != 1:
                raise MissionConfigurationError(
                    "executable missions allow concurrency up to 2 and delegation depth 1"
                )
        elif (
            budget.max_tool_calls not in {0, None}
            or budget.max_concurrency != 1
            or budget.max_delegation_depth != 0
        ):
            raise MissionConfigurationError(
                "analysis-only missions require zero tools and one non-delegating task"
            )
        if (
            budget.max_duration_seconds is not None
            and budget.max_duration_seconds > MAX_API_MISSION_DURATION_SECONDS
        ):
            raise MissionConfigurationError("mission duration exceeds the API limit")
        if (
            budget.max_tokens is not None
            and budget.max_tokens > MAX_API_MISSION_TOKENS
        ):
            raise MissionConfigurationError(
                "mission token budget exceeds the API limit"
            )
        if (
            budget.max_cost_usd is not None
            and budget.max_cost_usd > MAX_API_MISSION_COST_USD
        ):
            raise MissionConfigurationError("mission cost budget exceeds the API limit")
        if budget.max_retries > MAX_API_MISSION_RETRIES:
            raise MissionConfigurationError(
                "mission retry budget exceeds the API limit"
            )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        text = " ".join(str(exc).split()) or exc.__class__.__name__
        return text[:1_000]


__all__ = [
    "MAX_API_MISSION_COST_USD",
    "MAX_API_MISSION_DURATION_SECONDS",
    "MAX_API_MISSION_RETRIES",
    "MAX_API_MISSION_TOKENS",
    "MissionCapacityError",
    "MissionConfigurationError",
    "MissionComponents",
    "MissionService",
    "MissionServiceError",
    "MissionServiceUnavailable",
    "MissionStateError",
    "default_checkpoint_path",
]
