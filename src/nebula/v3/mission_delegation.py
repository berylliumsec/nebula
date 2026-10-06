"""Durable Mission supervision through the same child tools as Conversations."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any
from uuid import uuid4

from .chat import ChatCompletionRequest, ChatRequestMessage, ChatRole, ChatService
from .diagnostics import create_diagnostic_task, record_caught_exception
from .domain import (
    AgentRun,
    ChatMessage,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RunBackend,
    RunBudget,
    RunStatus,
    Task,
    TaskStatus,
    utc_now,
)
from .harnesses import HarnessRuntimeService
from .missions import MissionConfigurationError
from .providers import provider_from_profile, usage_cost_usd
from .storage import NebulaStore, NotFoundError

_TERMINAL_TURNS = {
    ChatTurnStatus.COMPLETE,
    ChatTurnStatus.FAILED,
    ChatTurnStatus.CANCELLED,
    ChatTurnStatus.INTERRUPTED,
}
_TERMINAL_RUNS = {
    RunStatus.COMPLETE,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
}
_TERMINAL_CHILDREN = {
    ChatSubagentStatus.COMPLETED,
    ChatSubagentStatus.FAILED,
    ChatSubagentStatus.STOPPED,
    ChatSubagentStatus.INTERRUPTED,
}


class DelegatedMissionService:
    """Mirror a supervisor conversation and its dynamic children into a Mission.

    The chat and subagent records remain execution authority. Mission rows are
    durable projections for status, task history, budgets and the operator UI.
    """

    def __init__(
        self, store: NebulaStore, chat: ChatService, harness: HarnessRuntimeService
    ) -> None:
        self.store = store
        self.chat = chat
        self.harness = harness
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._harness_costs: dict[str, tuple[int, float]] = {}

    async def startup(self) -> None:
        for run in self.store.iter_readable_entities(AgentRun):
            if (
                run.metadata.get("supervisor_mode") == "conversation"
                and run.status not in _TERMINAL_RUNS
            ):
                self._watch(run.id)

    async def shutdown(self) -> None:
        watchers = list(self._watchers.values())
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        self._watchers.clear()

    @staticmethod
    def _prompt(objective: str, stages: list[dict[str, str]]) -> str:
        parts = [
            "Supervise this Mission and produce a final result with evidence:",
            objective,
            "You can create child tasks as work emerges. First inspect subagent.capabilities; "
            "assign each child's enabled project tools, MCP servers, skills, hooks, "
            "knowledge, command runtime and further delegation as its work requires. "
            "Use subagent.start and wait/list to collect results. Apply the existing "
            "approval policy. Wait for every child before reporting completion.",
        ]
        if stages:
            parts.append("Operator checkpoints, in order:")
            parts.extend(f"{item['title']}: {item['objective']}" for item in stages)
        return "\n\n".join(parts)

    async def start(
        self,
        *,
        engagement_id: str,
        name: str,
        objective: str,
        backend: RunBackend,
        provider_id: str | None,
        harness_profile_id: str | None,
        harness_session_id: str | None,
        model: str,
        subagent_provider_id: str | None,
        subagent_model: str | None,
        mcp_server_ids: list[str],
        stages: list[dict[str, str]],
        budget: RunBudget,
        tools_enabled: bool,
        allow_cloud_tool_results: bool,
        harness_reasoning_effort: str | None = None,
        harness_service_tier: str | None = None,
        retry_of_run_id: str | None = None,
    ) -> AgentRun:
        self.store.get(Engagement, engagement_id)
        if not objective.strip():
            raise MissionConfigurationError("mission objective cannot be empty")
        prompt = self._prompt(objective, stages)
        if backend == RunBackend.NATIVE:
            profile = self.store.get(ProviderProfile, provider_id or "")
            if not profile.tools_verified_for(model):
                raise MissionConfigurationError(
                    "supervisor delegation requires verified tool calling for the selected model"
                )
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    engagement_id=engagement_id,
                    provider_id=profile.id,
                    model=model,
                    messages=[ChatRequestMessage(role=ChatRole.USER, content=prompt)],
                    mcp_server_ids=mcp_server_ids,
                    tools_enabled=tools_enabled,
                    allow_subagents=True,
                    max_active_subagents=budget.max_concurrency,
                    max_artifact_queries=budget.max_artifact_queries,
                    allow_cloud_knowledge=allow_cloud_tool_results,
                    allow_cloud_tool_results=allow_cloud_tool_results,
                    stream=True,
                )
            )
            session = prepared.session or prepared.pending_session
            turn = prepared.turn
            if session is None or turn is None:
                raise ValueError("supervisor conversation was not persisted")
            harness_turn = None
        else:
            if not subagent_provider_id or not subagent_model:
                raise MissionConfigurationError(
                    "harness Mission subagents require a provider and model"
                )
            session, turn, harness_turn = self.harness.prepare_chat(
                engagement_id=engagement_id,
                profile_id=harness_profile_id or "",
                model=model,
                prompt=prompt,
                chat_session_id=None,
                harness_session_id=harness_session_id,
                mcp_server_ids=mcp_server_ids,
                title=name,
                allow_remote_mcp=allow_cloud_tool_results,
                allow_cloud_knowledge=allow_cloud_tool_results,
                max_artifact_queries=budget.max_artifact_queries,
                harness_reasoning_effort=harness_reasoning_effort,
                harness_service_tier=harness_service_tier,
                provider_subagent={
                    "provider_profile_id": subagent_provider_id,
                    "model": subagent_model,
                    "max_active": budget.max_concurrency,
                },
            )
        run = self.store.create(
            AgentRun(
                id=str(uuid4()),
                engagement_id=engagement_id,
                objective=objective.strip(),
                status=RunStatus.RUNNING,
                backend=backend,
                supervisor_provider_id=provider_id
                if backend == RunBackend.NATIVE
                else None,
                supervisor_model=model,
                harness_profile_id=harness_profile_id
                if backend == RunBackend.HARNESS
                else None,
                harness_session_id=session.harness_session_id
                if backend == RunBackend.HARNESS
                else None,
                budget=budget,
                started_at=utc_now(),
                runtime_snapshot={
                    "mcp_server_ids": mcp_server_ids,
                    "remote_mcp_confirmed": allow_cloud_tool_results,
                    "subagent_provider_id": subagent_provider_id,
                    "subagent_model": subagent_model,
                    "tools_enabled": tools_enabled,
                    "runtime_options": {
                        "reasoning_effort": harness_reasoning_effort,
                        "service_tier": harness_service_tier,
                    },
                },
                metadata={
                    "name": name.strip() or objective.strip(),
                    "origin": "api",
                    "supervisor_mode": "conversation",
                    "supervisor_chat_session_id": session.id,
                    "supervisor_chat_turn_id": turn.id,
                    "stages": stages,
                    "total_tasks": 1,
                    "completed_tasks": 0,
                    **({"retry_of_run_id": retry_of_run_id} if retry_of_run_id else {}),
                },
            )
        )
        self.store.create(
            Task(
                id=f"supervisor-{run.id}",
                engagement_id=engagement_id,
                run_id=run.id,
                specialist_role="supervisor",
                title="Mission supervisor",
                instructions=objective,
                status=TaskStatus.RUNNING,
                started_at=run.started_at,
                metadata={"chat_session_id": session.id},
            )
        )
        self.store.append_event(
            run.id,
            "run.started",
            {"summary": "Mission supervisor started."},
            actor_id="system",
        )
        self.store.append_event(
            run.id,
            "task.started",
            {
                "task_id": f"supervisor-{run.id}",
                "summary": "Mission supervisor started.",
            },
            actor_id="system",
        )
        if harness_turn is None:
            self.chat.start_provider_turn(prepared)
        else:
            self.harness.start_chat_turn(harness_turn.id)
        self._watch(run.id)
        return run

    def _watch(self, run_id: str) -> None:
        existing = self._watchers.get(run_id)
        if existing is not None and not existing.done():
            return
        task = create_diagnostic_task(
            self._run_watch(run_id),
            feature="missions",
            event_code="missions.delegated_supervisor",
            failure_message="A delegated Mission supervisor watcher failed.",
            name=f"nebula-delegated-mission-{run_id}",
        )
        self._watchers[run_id] = task
        task.add_done_callback(
            lambda done: (
                self._watchers.pop(run_id, None)
                if self._watchers.get(run_id) is done
                else None
            )
        )

    def _children(self, root_session_id: str) -> list[tuple[ChatSubagent, str | None]]:
        queue = deque([(root_session_id, None)])
        seen = {root_session_id}
        result: list[tuple[ChatSubagent, str | None]] = []
        while queue:
            session_id, parent_id = queue.popleft()
            for child in self.store.find_entities(
                ChatSubagent, {"parent_session_id": session_id}
            ):
                if child.child_session_id in seen:
                    raise ValueError("Mission child graph has a cycle")
                seen.add(child.child_session_id)
                result.append((child, parent_id))
                queue.append((child.child_session_id, child.id))
        return result

    def _sync(
        self, run: AgentRun
    ) -> tuple[AgentRun, ChatTurn, list[tuple[ChatSubagent, str | None]]]:
        session_id = str(run.metadata["supervisor_chat_session_id"])
        root_turn_id = str(run.metadata["supervisor_chat_turn_id"])
        root_turn = self.store.get(ChatTurn, root_turn_id)
        children = self._children(session_id)
        for child, parent_id in children:
            task_id = f"mission-child-{child.id}"
            status = {
                ChatSubagentStatus.RUNNING: TaskStatus.RUNNING,
                ChatSubagentStatus.COMPLETED: TaskStatus.COMPLETE,
                ChatSubagentStatus.FAILED: TaskStatus.FAILED,
                ChatSubagentStatus.STOPPED: TaskStatus.CANCELLED,
                ChatSubagentStatus.INTERRUPTED: TaskStatus.BLOCKED,
            }[child.status]
            changes = {
                "status": status,
                "completed_at": child.finished_at,
                "metadata": {
                    "chat_session_id": child.child_session_id,
                    "capabilities": child.parent_request.get("capabilities", {}),
                    "result": child.result,
                    "error": child.error,
                },
            }
            try:
                task = self.store.get(Task, task_id)
            except NotFoundError:
                # diagnostic-expected: the first projection creates this child task.
                self.store.create(
                    Task(
                        id=task_id,
                        engagement_id=run.engagement_id,
                        run_id=run.id,
                        parent_task_id=f"mission-child-{parent_id}"
                        if parent_id
                        else f"supervisor-{run.id}",
                        specialist_role="subagent",
                        title=child.name,
                        instructions=child.task,
                        started_at=child.started_at,
                        **changes,
                    )
                )
                self.store.append_event(
                    run.id,
                    "task.started",
                    {"task_id": task_id, "summary": f"Subagent {child.name} started."},
                    actor_id="system",
                    idempotency_key=f"delegated:{task_id}:started",
                )
                if status in {
                    TaskStatus.COMPLETE,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.BLOCKED,
                }:
                    self.store.append_event(
                        run.id,
                        f"task.{status.value}",
                        {
                            "task_id": task_id,
                            "summary": (
                                child.result
                                or child.error
                                or f"Subagent {child.name} {status.value}."
                            )[:4_000],
                        },
                        actor_id="system",
                        idempotency_key=f"delegated:{task_id}:{status.value}",
                    )
            else:
                if task.status != status or task.metadata != changes["metadata"]:
                    self.store.update(
                        Task, task.id, changes, expected_revision=task.revision
                    )
                    if task.status != status:
                        self.store.append_event(
                            run.id,
                            f"task.{status.value}",
                            {
                                "task_id": task_id,
                                "summary": (
                                    child.result
                                    or child.error
                                    or f"Subagent {child.name} {status.value}."
                                )[:4_000],
                            },
                            actor_id="system",
                            idempotency_key=f"delegated:{task_id}:{status.value}",
                        )
        supervisor = self.store.get(Task, f"supervisor-{run.id}")
        supervisor_status = {
            ChatTurnStatus.COMPLETE: TaskStatus.COMPLETE,
            ChatTurnStatus.FAILED: TaskStatus.FAILED,
            ChatTurnStatus.CANCELLED: TaskStatus.CANCELLED,
            ChatTurnStatus.INTERRUPTED: TaskStatus.BLOCKED,
        }.get(
            root_turn.status,
            TaskStatus.WAITING_APPROVAL
            if root_turn.status == ChatTurnStatus.WAITING_APPROVAL
            else TaskStatus.RUNNING,
        )
        if (
            root_turn.status == ChatTurnStatus.COMPLETE
            and children
            and (
                any(child.status == ChatSubagentStatus.RUNNING for child, _ in children)
                or not run.metadata.get("synthesis_started")
            )
        ):
            supervisor_status = TaskStatus.RUNNING
        if supervisor.status != supervisor_status:
            self.store.update(
                Task,
                supervisor.id,
                {
                    "status": supervisor_status,
                    "completed_at": utc_now()
                    if supervisor_status
                    in {
                        TaskStatus.COMPLETE,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                        TaskStatus.BLOCKED,
                    }
                    else None,
                },
                expected_revision=supervisor.revision,
            )
            if supervisor_status in {
                TaskStatus.COMPLETE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.BLOCKED,
            }:
                self.store.append_event(
                    run.id,
                    f"task.{supervisor_status.value}",
                    {
                        "task_id": supervisor.id,
                        "summary": f"Mission supervisor {supervisor_status.value}.",
                    },
                    actor_id="system",
                    idempotency_key=f"delegated:{supervisor.id}:{supervisor_status.value}",
                )
        completed = int(supervisor_status == TaskStatus.COMPLETE) + sum(
            child.status == ChatSubagentStatus.COMPLETED for child, _ in children
        )
        total = 1 + len(children)
        waiting = root_turn.status == ChatTurnStatus.WAITING_APPROVAL or any(
            self.store.get(ChatTurn, child.child_turn_id).status
            == ChatTurnStatus.WAITING_APPROVAL
            for child, _ in children
            if child.child_turn_id and child.status == ChatSubagentStatus.RUNNING
        )
        metadata = {**run.metadata, "completed_tasks": completed, "total_tasks": total}
        status = RunStatus.WAITING_APPROVAL if waiting else RunStatus.RUNNING
        changes: dict[str, Any] = {}
        if metadata != run.metadata:
            changes["metadata"] = metadata
        if (
            status != run.status
            and run.status not in _TERMINAL_RUNS
            and run.status != RunStatus.CANCELLING
        ):
            changes["status"] = status
        if changes:
            run = self.store.update(
                AgentRun, run.id, changes, expected_revision=run.revision
            )
        return run, root_turn, children

    def _usage(
        self, run: AgentRun, children: list[tuple[ChatSubagent, str | None]]
    ) -> tuple[int, float, int, int]:
        sessions = [
            str(run.metadata["supervisor_chat_session_id"]),
            *(child.child_session_id for child, _ in children),
        ]
        tokens = calls = artifact_queries = 0
        cost = 0.0
        for session_id in sessions:
            for turn in self.store.list_session_entities(ChatTurn, session_id):
                tokens += turn.usage.total_tokens
                calls += turn.execution_tool_calls
                artifact_queries += turn.artifact_queries
                if turn.provider_profile_id:
                    profile = self.store.get(ProviderProfile, turn.provider_profile_id)
                    cost += usage_cost_usd(
                        provider_from_profile(profile).config, turn.model, turn.usage
                    )
                elif turn.harness_turn_id:
                    cursor, reported_cost = self._harness_costs.get(
                        turn.harness_turn_id, (0, 0.0)
                    )
                    while True:
                        activity = self.harness.activity_events(
                            turn.harness_turn_id, after_sequence=cursor
                        )
                        for event in activity.events:
                            detailed = event.detailed_usage
                            if detailed is not None and detailed.cost_usd is not None:
                                reported_cost = detailed.cost_usd
                        if activity.next_sequence == cursor:
                            break
                        cursor = activity.next_sequence
                        if len(activity.events) < 1_000:
                            break
                    self._harness_costs[turn.harness_turn_id] = (cursor, reported_cost)
                    cost += reported_cost
        return tokens, cost, calls, artifact_queries

    async def _run_watch(self, run_id: str) -> None:
        try:
            while True:
                run = self.store.get(AgentRun, run_id)
                if run.status in _TERMINAL_RUNS:
                    return
                run, root_turn, children = self._sync(run)
                tokens, cost, calls, artifact_queries = self._usage(run, children)
                limit_reason = None
                if (
                    run.budget.max_duration_seconds is not None
                    and run.started_at
                    and (utc_now() - run.started_at).total_seconds()
                    >= run.budget.max_duration_seconds
                ):
                    limit_reason = "Mission duration limit reached"
                elif (
                    run.budget.max_tokens is not None
                    and tokens >= run.budget.max_tokens
                ):
                    limit_reason = "Mission token limit reached"
                elif (
                    run.budget.max_cost_usd is not None
                    and cost >= run.budget.max_cost_usd
                ):
                    limit_reason = "Mission cost limit reached"
                elif (
                    run.budget.max_tool_calls is not None
                    and calls >= run.budget.max_tool_calls
                ):
                    limit_reason = "Mission execution call limit reached"
                elif (
                    run.budget.max_artifact_queries is not None
                    and artifact_queries >= run.budget.max_artifact_queries
                ):
                    limit_reason = "Mission artifact query limit reached"
                if limit_reason:
                    await self.stop(
                        run_id, reason=limit_reason, status=RunStatus.FAILED
                    )
                    return
                if root_turn.status in _TERMINAL_TURNS:
                    if root_turn.status != ChatTurnStatus.COMPLETE:
                        status = (
                            RunStatus.INTERRUPTED
                            if root_turn.status == ChatTurnStatus.INTERRUPTED
                            else RunStatus.CANCELLED
                            if root_turn.status == ChatTurnStatus.CANCELLED
                            else RunStatus.FAILED
                        )
                        await self.stop(
                            run_id,
                            reason=root_turn.error or "Mission supervisor stopped",
                            status=status,
                        )
                        return
                    if all(child.status in _TERMINAL_CHILDREN for child, _ in children):
                        # A child may have finished after the parent response. A
                        # fresh supervisor turn incorporates those late reports.
                        if children and not run.metadata.get("synthesis_started"):
                            await self._synthesize(run)
                            continue
                        final_turn = self.store.get(
                            ChatTurn, str(run.metadata["supervisor_chat_turn_id"])
                        )
                        if final_turn.final_message_id:
                            summary = self.store.get(
                                ChatMessage, final_turn.final_message_id
                            ).content
                        else:
                            summary = (
                                final_turn.content or "Mission supervisor completed."
                            )
                        self._finish(run, RunStatus.COMPLETE, summary)
                        return
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_caught_exception(
                "missions",
                "missions.delegated_supervisor.failed",
                "Delegated Mission tracking failed.",
                exc,
                stage="mission-supervision",
            )
            run = self.store.get(AgentRun, run_id)
            if run.status not in _TERMINAL_RUNS:
                reason = "Mission tracking stopped unexpectedly. Review the saved state and retry."
                try:
                    await self.stop(run_id, reason=reason, status=RunStatus.INTERRUPTED)
                except Exception as stop_error:
                    record_caught_exception(
                        "missions",
                        "missions.delegated_supervisor.stop_failed",
                        "A delegated Mission could not stop its work after tracking failed.",
                        stop_error,
                        stage="mission-supervision",
                    )
                    self._finish(run, RunStatus.INTERRUPTED, reason)

    async def _synthesize(self, run: AgentRun) -> None:
        session_id = str(run.metadata["supervisor_chat_session_id"])
        prompt = "All delegated work has settled. Review the child reports in this conversation and give the final Mission result with evidence and unresolved issues."
        if run.backend == RunBackend.NATIVE:
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    engagement_id=run.engagement_id,
                    session_id=session_id,
                    provider_id=run.supervisor_provider_id,
                    model=run.supervisor_model,
                    messages=[ChatRequestMessage(role=ChatRole.USER, content=prompt)],
                    mcp_server_ids=run.runtime_snapshot.get("mcp_server_ids", []),
                    tools_enabled=run.runtime_snapshot.get("tools_enabled", False),
                    allow_cloud_tool_results=run.runtime_snapshot.get(
                        "remote_mcp_confirmed", False
                    ),
                    stream=True,
                )
            )
            turn = prepared.turn
            if turn is None:
                raise ValueError("Mission synthesis turn was not persisted")
            self.chat.start_provider_turn(prepared)
        else:
            session, turn, harness_turn = self.harness.prepare_chat(
                engagement_id=run.engagement_id,
                profile_id=run.harness_profile_id or "",
                model=run.supervisor_model,
                prompt=prompt,
                chat_session_id=session_id,
                harness_session_id=run.harness_session_id,
                mcp_server_ids=run.runtime_snapshot.get("mcp_server_ids", []),
                allow_remote_mcp=run.runtime_snapshot.get(
                    "remote_mcp_confirmed", False
                ),
            )
            self.harness.start_chat_turn(harness_turn.id)
        current = self.store.get(AgentRun, run.id)
        self.store.update(
            AgentRun,
            run.id,
            {
                "metadata": {
                    **current.metadata,
                    "synthesis_started": True,
                    "supervisor_chat_turn_id": turn.id,
                }
            },
            expected_revision=current.revision,
        )

    def _finish(self, run: AgentRun, status: RunStatus, summary: str) -> AgentRun:
        latest = self.store.get(AgentRun, run.id)
        if latest.status in _TERMINAL_RUNS:
            return latest
        tokens, cost, calls, artifact_queries = self._usage(
            latest, self._children(str(latest.metadata["supervisor_chat_session_id"]))
        )
        updated, _ = self.store.update_with_event(
            AgentRun,
            latest.id,
            {
                "status": status,
                "completed_at": utc_now(),
                "metadata": {
                    **latest.metadata,
                    "final_summary": summary[:20_000],
                    "spent_usd": cost,
                    "tokens_used": tokens,
                    "tool_calls_used": calls,
                    "artifact_queries_used": artifact_queries,
                },
            },
            expected_revision=latest.revision,
            run_id=latest.id,
            event_type=(
                "run.completed"
                if status == RunStatus.COMPLETE
                else "run.cancelled"
                if status == RunStatus.CANCELLED
                else "run.failed"
            ),
            event_payload={"summary": summary[:20_000]},
            actor_id="system",
            idempotency_key="delegated:finished",
        )
        return updated

    async def stop(
        self, run_id: str, *, reason: str, status: RunStatus = RunStatus.CANCELLED
    ) -> AgentRun:
        run = self.store.get(AgentRun, run_id)
        if run.status in _TERMINAL_RUNS:
            return run
        for child, _ in reversed(
            self._children(str(run.metadata["supervisor_chat_session_id"]))
        ):
            if child.status == ChatSubagentStatus.RUNNING:
                await self.chat.subagents.stop(child.id, reason=reason)
        turn = self.store.get(ChatTurn, str(run.metadata["supervisor_chat_turn_id"]))
        if turn.status not in _TERMINAL_TURNS:
            if run.backend == RunBackend.HARNESS and turn.harness_turn_id:
                await self.harness.cancel_turn(turn.harness_turn_id, reason=reason)
            else:
                await self.chat.stop_provider_turn(turn.id)
        self._sync(self.store.get(AgentRun, run_id))
        return self._finish(run, status, reason)
