"""Core-owned subagents for provider-backend chats.

A parent provider turn delegates a task with ``start_subagent``. Core creates a
child conversation bound to the same provider, model and capabilities, runs it
as an ordinary background provider turn, and reports the child's final answer
back: as a ``wait_subagents`` tool result when the parent is waiting, and as a
durable result message in the parent conversation once the parent is idle.
Children cannot start their own subagents.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5

from .diagnostics import record_caught_exception
from .domain import (
    CHAT_SUBAGENT_TERMINAL_STATUSES,
    Approval,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from .runtime_platform import RuntimeToolComponents
from .storage import ConflictError, NotFoundError
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec

if TYPE_CHECKING:
    from .chat import ChatService
    from .storage import NebulaStore

MAX_ACTIVE_SUBAGENTS = 3
MAX_SUBAGENTS_PER_TURN = 6
RESULT_CHARACTERS = 12_000
RECENT_STEPS = 4
SUBAGENT_TOOL_NAMES = frozenset(
    {"start_subagent", "wait_subagents", "list_subagents", "stop_subagent"}
)

SUBAGENT_ROUTING_INSTRUCTIONS = """
Subagents: start_subagent delegates one independent, multi-step task to a child
assistant with the same model and tools; it returns immediately and runs in
parallel. Give it a complete, self-contained task. Do not delegate single
lookups. Call wait_subagents when you need their reports before answering;
subagents that finish after your answer report back in the conversation."""

SUBAGENT_CHILD_INSTRUCTIONS = """

You are a subagent. Another assistant working with the operator delegated one
task to you. Complete only that task with the available tools, then finish.
Your final answer is returned to that assistant as your report: lead with the
findings, keep it concise and factual, and say what you could not verify."""

_TERMINAL_TURN_STATUS = {
    ChatTurnStatus.COMPLETE: ChatSubagentStatus.COMPLETED,
    ChatTurnStatus.FAILED: ChatSubagentStatus.FAILED,
    ChatTurnStatus.CANCELLED: ChatSubagentStatus.STOPPED,
    ChatTurnStatus.INTERRUPTED: ChatSubagentStatus.INTERRUPTED,
}
_DETAIL_ARGUMENTS = ("command", "path", "query", "url", "target", "pattern", "name")


class SubagentWaitPending(Exception):
    """Raised by wait_subagents when the parent turn must pause for children."""

    def __init__(self, subagent_ids: list[str], mode: str):
        super().__init__("waiting for subagents")
        self.subagent_ids = subagent_ids
        self.mode = mode


def is_subagent_session(session: ChatSession) -> bool:
    return isinstance(session.metadata.get("subagent_id"), str)


def _bounded(text: str, limit: int = RESULT_CHARACTERS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _step_detail(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in _DETAIL_ARGUMENTS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded(" ".join(value.split()), 120)
    return ""


class SubagentService:
    """Start, observe, stop and deliver provider-chat subagents."""

    def __init__(self, store: NebulaStore, chat: ChatService):
        self.store = store
        self.chat = chat

    # -- queries -----------------------------------------------------------

    def _all(self) -> Iterable[ChatSubagent]:
        offset = 0
        while page := self.store.list_entities(ChatSubagent, offset=offset, limit=1_000):
            yield from page
            offset += len(page)

    def for_session(self, parent_session_id: str) -> list[ChatSubagent]:
        return sorted(
            (item for item in self._all() if item.parent_session_id == parent_session_id),
            key=lambda item: (item.started_at, item.id),
        )

    def get(self, subagent_id: str) -> ChatSubagent:
        return self.store.get(ChatSubagent, subagent_id)

    def _for_child_session(self, session: ChatSession) -> ChatSubagent | None:
        subagent_id = session.metadata.get("subagent_id")
        if not isinstance(subagent_id, str):
            return None
        try:
            return self.get(subagent_id)
        except NotFoundError:
            return None

    @staticmethod
    def active(records: Iterable[ChatSubagent]) -> list[ChatSubagent]:
        return [
            item for item in records if item.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
        ]

    def view(self, record: ChatSubagent) -> dict[str, Any]:
        """Return the operator-facing state, overlaying the live child turn."""

        turn: ChatTurn | None = None
        if record.child_turn_id:
            try:
                turn = self.store.get(ChatTurn, record.child_turn_id)
            except NotFoundError:
                turn = None
        history = list(turn.tool_history) if turn is not None else []
        recent = [
            {
                "tool": str(entry.get("name") or ""),
                "detail": _step_detail(entry.get("arguments")),
                "status": str(entry.get("status") or ""),
            }
            for entry in history[-RECENT_STEPS:]
        ]
        state = record.status.value
        approval: dict[str, Any] | None = None
        if record.status == ChatSubagentStatus.RUNNING and turn is not None:
            if turn.status == ChatTurnStatus.WAITING_APPROVAL and turn.approval_id:
                state = "waiting_approval"
                try:
                    pending = self.store.get(Approval, turn.approval_id)
                except NotFoundError:
                    pending = None
                pending_entry = history[-1] if history else {}
                approval = {
                    "id": turn.approval_id,
                    "status": pending.status.value if pending else "pending",
                    "tool": str(pending_entry.get("name") or ""),
                    "detail": _step_detail(pending_entry.get("arguments")),
                    "risk_class": pending.risk_class.value if pending else None,
                    "rationale": pending.policy_rationale if pending else None,
                }
        usage = turn.usage if turn is not None and record.status == ChatSubagentStatus.RUNNING else record.usage
        finished = record.finished_at or utc_now()
        return {
            "id": record.id,
            "name": record.name,
            "task": record.task,
            "status": state,
            "parent_session_id": record.parent_session_id,
            "parent_turn_id": record.parent_turn_id,
            "child_session_id": record.child_session_id,
            "child_turn_id": record.child_turn_id,
            "step_count": turn.next_step if turn is not None else 0,
            "recent_steps": recent,
            "approval": approval,
            "usage": usage.model_dump(mode="json"),
            "started_at": record.started_at.isoformat(),
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            "elapsed_seconds": max(0.0, (finished - record.started_at).total_seconds()),
            "result": record.result,
            "error": record.error,
            "result_message_id": record.result_message_id,
        }

    def _model_view(self, record: ChatSubagent, *, include_result: bool) -> dict[str, Any]:
        view = self.view(record)
        payload: dict[str, Any] = {
            "subagent_id": record.id,
            "name": record.name,
            "status": view["status"],
            "steps": view["step_count"],
        }
        if include_result and record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            payload["report"] = record.result or None
            payload["error"] = record.error
        return payload

    # -- start -------------------------------------------------------------

    async def start(
        self, invocation: ToolInvocation, *, task: str, name: str | None, context: str | None
    ) -> ChatSubagent:
        from .chat import ChatCompletionRequest, ChatRequestMessage

        if not invocation.chat_turn_id:
            raise InvalidToolArguments("subagents require a provider chat turn")
        parent_turn = self.store.get(ChatTurn, invocation.chat_turn_id)
        parent_session = self.store.get(ChatSession, parent_turn.session_id)
        if is_subagent_session(parent_session):
            raise InvalidToolArguments("subagents cannot start their own subagents")
        task = task.strip()
        if not task:
            raise InvalidToolArguments("task must describe the delegated work")
        label = " ".join((name or task).split())[:80] or "Subagent"
        siblings = self.for_session(parent_session.id)
        for existing in siblings:
            if existing.parent_request.get("idempotency_key") == invocation.idempotency_key:
                return existing
        if len(self.active(siblings)) >= MAX_ACTIVE_SUBAGENTS:
            raise InvalidToolArguments(
                f"{MAX_ACTIVE_SUBAGENTS} subagents are already running; wait for one to finish"
            )
        if sum(1 for item in siblings if item.parent_turn_id == parent_turn.id) >= MAX_SUBAGENTS_PER_TURN:
            raise InvalidToolArguments(
                f"this response already started {MAX_SUBAGENTS_PER_TURN} subagents"
            )
        snapshot = parent_turn.request_snapshot
        tools_enabled = bool(snapshot.get("include_oci_tools", False))
        mcp_server_ids = [
            item for item in snapshot.get("mcp_server_ids", []) if isinstance(item, str)
        ]
        parent_request: dict[str, Any] = {
            "idempotency_key": invocation.idempotency_key,
            "tools_enabled": tools_enabled,
            "mcp_server_ids": mcp_server_ids,
            "allow_subagents": bool(snapshot.get("allow_subagents", False)),
        }
        subagent_id = str(uuid4())
        child_session = ChatSession(
            id=str(uuid4()),
            engagement_id=parent_session.engagement_id,
            title=f"Subagent · {label}"[:300],
            provider_profile_id=parent_turn.provider_profile_id,
            model=parent_turn.model,
            parent_session_id=parent_session.id,
            metadata={
                "subagent_id": subagent_id,
                "subagent_parent_session_id": parent_session.id,
                "subagent_parent_turn_id": parent_turn.id,
            },
        )
        record = ChatSubagent(
            id=subagent_id,
            engagement_id=parent_session.engagement_id,
            parent_session_id=parent_session.id,
            parent_turn_id=parent_turn.id,
            child_session_id=child_session.id,
            name=label,
            task=_bounded(task, 20_000),
            parent_request=parent_request,
        )
        with self.store.transaction() as transaction:
            transaction.add(child_session)
            transaction.add(record)
        content = task if not context or not context.strip() else (
            f"{task}\n\nContext from the delegating assistant:\n{context.strip()}"
        )
        try:
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    provider_id=parent_turn.provider_profile_id,
                    engagement_id=parent_session.engagement_id,
                    session_id=child_session.id,
                    model=parent_turn.model,
                    messages=[
                        ChatRequestMessage(role=ChatRole.USER, content=_bounded(content, 60_000))
                    ],
                    include_knowledge=False,
                    tools_enabled=tools_enabled,
                    mcp_server_ids=mcp_server_ids,
                    # The parent turn only reached tool routing after its own
                    # cloud-transfer confirmation (or with a local provider).
                    allow_cloud_tool_results=True,
                    stream=True,
                )
            )
            if prepared.turn is None:
                raise RuntimeError("subagent turn was not created")
            record = self.store.update(
                ChatSubagent,
                record.id,
                {"child_turn_id": prepared.turn.id},
                expected_revision=record.revision,
            )
            self.chat.start_provider_turn(prepared)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.subagent.start_failed",
                "A subagent could not be started.",
                exc,
                stage="subagent-start",
            )
            latest = self.get(record.id)
            return self.store.update(
                ChatSubagent,
                latest.id,
                {
                    "status": ChatSubagentStatus.FAILED,
                    "finished_at": utc_now(),
                    "error": _bounded(f"Subagent could not start: {exc}", 1_000),
                },
                expected_revision=latest.revision,
            )
        return record

    # -- stop --------------------------------------------------------------

    async def stop(self, subagent_id: str) -> ChatSubagent:
        record = self.get(subagent_id)
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            return record
        if record.child_turn_id:
            await self.chat.stop_provider_turn(record.child_turn_id)
            turn = self.store.get(ChatTurn, record.child_turn_id)
            await self._child_settled(self.get(record.id), turn)
        record = self.get(record.id)
        if record.status not in CHAT_SUBAGENT_TERMINAL_STATUSES:
            record = self.store.update(
                ChatSubagent,
                record.id,
                {
                    "status": ChatSubagentStatus.STOPPED,
                    "finished_at": utc_now(),
                    "error": "Stopped before it started.",
                },
                expected_revision=record.revision,
            )
            await self._deliver(record)
        return record

    async def stop_for_parent_turn(self, parent_turn_id: str) -> None:
        for record in self.active(self._all()):
            if record.parent_turn_id == parent_turn_id:
                try:
                    await self.stop(record.id)
                except Exception as exc:
                    record_caught_exception(
                        "chat",
                        "chat.subagent.cascade_stop_failed",
                        "A subagent could not be stopped with its parent response.",
                        exc,
                        stage="subagent-stop",
                    )

    # -- waiting -----------------------------------------------------------

    def resolve_wait(self, parent_session_id: str, ids: list[str] | None) -> list[ChatSubagent]:
        records = self.for_session(parent_session_id)
        if ids:
            by_id = {item.id: item for item in records}
            missing = [item for item in ids if item not in by_id]
            if missing:
                raise InvalidToolArguments(f"unknown subagent ids: {', '.join(missing)}")
            return [by_id[item] for item in dict.fromkeys(ids)]
        return self.active(records) or [
            item for item in records if item.result_message_id is None
        ]

    def wait_satisfied(self, ids: list[str], mode: str) -> bool:
        records = [self.get(item) for item in ids]
        finished = [item for item in records if item.status in CHAT_SUBAGENT_TERMINAL_STATUSES]
        return bool(finished) if mode == "any" else len(finished) == len(records)

    def wait_output(self, ids: list[str]) -> dict[str, Any]:
        records = [self.get(item) for item in ids]
        return {
            "subagents": [self._model_view(item, include_result=True) for item in records],
            "still_running": [
                item.id for item in records if item.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
            ],
        }

    # -- lifecycle hooks ---------------------------------------------------

    async def turn_settled(self, turn_id: str) -> None:
        """React after any provider turn stops producing: child or parent side."""

        try:
            turn = self.store.get(ChatTurn, turn_id)
            session = self.store.get(ChatSession, turn.session_id)
        except NotFoundError:
            return
        record = self._for_child_session(session)
        if record is not None and record.child_turn_id == turn.id:
            await self._child_settled(record, turn)
            return
        if turn.status == ChatTurnStatus.WAITING_CALLBACK:
            self._resume_waiting_parent(turn)
        elif turn.status in {
            ChatTurnStatus.COMPLETE,
            ChatTurnStatus.FAILED,
            ChatTurnStatus.CANCELLED,
        }:
            await self.deliver_pending(session.id)

    async def _child_settled(self, record: ChatSubagent, turn: ChatTurn) -> None:
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            return
        status = _TERMINAL_TURN_STATUS.get(turn.status)
        if status is None:
            return
        result = ""
        if status == ChatSubagentStatus.COMPLETED and turn.final_message_id:
            try:
                result = self.store.get(ChatMessage, turn.final_message_id).content
            except NotFoundError:
                result = ""
        error = None if status == ChatSubagentStatus.COMPLETED else (turn.error or status.value)
        if status == ChatSubagentStatus.STOPPED and self.chat.shutting_down:
            status = ChatSubagentStatus.INTERRUPTED
            error = "Core shut down while this subagent was running."
        try:
            record = self.store.update(
                ChatSubagent,
                record.id,
                {
                    "status": status,
                    "finished_at": utc_now(),
                    "usage": turn.usage,
                    "result": _bounded(result),
                    "error": _bounded(error, 1_000) if error else None,
                },
                expected_revision=record.revision,
            )
        except ConflictError:
            return
        parent_turn = self._parent_turn(record)
        if parent_turn is not None and parent_turn.goal_id and turn.usage.total_tokens:
            try:
                self.chat._charge_goal(
                    parent_turn.goal_id,
                    turn.usage,
                    exhausted_reason="Token budget exhausted by subagent work.",
                )
            except NotFoundError:
                pass
        await self._deliver(record)

    def _parent_turn(self, record: ChatSubagent) -> ChatTurn | None:
        try:
            return self.store.get(ChatTurn, record.parent_turn_id)
        except NotFoundError:
            return None

    async def _deliver(self, record: ChatSubagent) -> None:
        """Resume a waiting parent, or post the report once the parent is idle."""

        pending = self.chat.pending_turn(record.parent_session_id)
        if pending is None:
            await self.deliver_pending(record.parent_session_id)
        elif pending.status == ChatTurnStatus.WAITING_CALLBACK:
            self._resume_waiting_parent(pending)

    def _resume_waiting_parent(self, turn: ChatTurn) -> None:
        wait = self.pending_wait(turn)
        if wait is None or not self.wait_satisfied(wait["ids"], wait["mode"]):
            return
        if self.chat.has_active_provider_turn(turn.id):
            return
        try:
            self.chat.start_provider_turn(self.chat.prepare_resume(turn.id))
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.subagent.parent_resume_failed",
                "Subagent results arrived but the waiting response could not resume.",
                exc,
                stage="subagent-deliver",
            )

    @staticmethod
    def pending_wait(turn: ChatTurn) -> dict[str, Any] | None:
        if turn.status != ChatTurnStatus.WAITING_CALLBACK or not turn.tool_history:
            return None
        wait = turn.tool_history[-1].get("subagent_wait")
        return wait if isinstance(wait, dict) else None

    async def deliver_pending(self, parent_session_id: str) -> None:
        """Post finished, undelivered reports into an idle parent conversation."""

        if self.chat.pending_turn(parent_session_id) is not None:
            return
        records = self.for_session(parent_session_id)
        undelivered = [
            item
            for item in records
            if item.status in CHAT_SUBAGENT_TERMINAL_STATUSES and item.result_message_id is None
        ]
        for record in undelivered:
            try:
                self._post_result(record)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.result_post_failed",
                    "A subagent report could not be added to the conversation.",
                    exc,
                    stage="subagent-deliver",
                )
                return
        if undelivered and not self.active(self.for_session(parent_session_id)):
            await self._continue_goal(undelivered[-1])

    def _post_result(self, record: ChatSubagent) -> None:
        session = self.store.get(ChatSession, record.parent_session_id)
        messages = self.chat.session_messages(session.id)
        sequence = max(
            [item.sequence for item in messages]
            + [int(session.metadata.get("last_sequence") or 0)]
        ) + 1
        heading = {
            ChatSubagentStatus.COMPLETED: "Subagent finished",
            ChatSubagentStatus.FAILED: "Subagent failed",
            ChatSubagentStatus.STOPPED: "Subagent stopped",
            ChatSubagentStatus.INTERRUPTED: "Subagent interrupted",
        }.get(record.status, "Subagent update")
        body = record.result or record.error or "No report was produced."
        finished = record.finished_at or utc_now()
        message = ChatMessage(
            id=str(uuid5(NAMESPACE_URL, f"nebula:subagent-result:{record.id}")),
            engagement_id=record.engagement_id,
            session_id=session.id,
            sequence=sequence,
            role=ChatRole.ASSISTANT,
            content=f"{heading}: {record.name}\n\n{body}",
            provider_profile_id=session.provider_profile_id,
            model=session.model,
            usage=record.usage,
            metadata={
                "kind": "subagent_result",
                "subagent_id": record.id,
                "subagent_name": record.name,
                "subagent_status": record.status.value,
                "child_session_id": record.child_session_id,
                "elapsed_seconds": max(0.0, (finished - record.started_at).total_seconds()),
            },
        )
        with self.store.transaction() as transaction:
            transaction.update(
                ChatSession,
                session.id,
                {
                    "metadata": {
                        **session.metadata,
                        "message_count": sequence,
                        "last_sequence": sequence,
                    }
                },
                expected_revision=session.revision,
            )
            transaction.add(message)
            transaction.update(
                ChatSubagent,
                record.id,
                {"result_message_id": message.id},
                expected_revision=record.revision,
            )

    async def _continue_goal(self, record: ChatSubagent) -> None:
        """Let a running goal pick up reports that arrived after its reply."""

        from .chat import ChatCompletionRequest, ChatRequestMessage

        parent_turn = self._parent_turn(record)
        if parent_turn is None or not parent_turn.goal_id:
            return
        try:
            goal = self.store.get(ChatGoal, parent_turn.goal_id)
        except NotFoundError:
            return
        if goal.status != ChatGoalStatus.RUNNING or goal.execution_claim_id is not None:
            return
        flags = record.parent_request
        try:
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    provider_id=parent_turn.provider_profile_id,
                    engagement_id=record.engagement_id,
                    session_id=record.parent_session_id,
                    goal_id=goal.id,
                    model=parent_turn.model,
                    messages=[
                        ChatRequestMessage(
                            role=ChatRole.USER,
                            content="Subagent reports are ready. Continue the conversation goal.",
                        )
                    ],
                    include_knowledge=False,
                    tools_enabled=bool(flags.get("tools_enabled")),
                    mcp_server_ids=list(flags.get("mcp_server_ids") or []),
                    allow_subagents=bool(flags.get("allow_subagents")),
                    allow_cloud_tool_results=True,
                    stream=True,
                )
            )
            self.chat.start_provider_turn(prepared)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.subagent.goal_continue_failed",
                "Subagent reports arrived but the running goal could not continue.",
                exc,
                stage="subagent-deliver",
            )

    async def reconcile_after_restart(self) -> None:
        """Finish records whose child turn cannot continue; never auto-resume."""

        for record in self.active(self._all()):
            turn: ChatTurn | None = None
            if record.child_turn_id:
                try:
                    turn = self.store.get(ChatTurn, record.child_turn_id)
                except NotFoundError:
                    turn = None
            if turn is not None and turn.status in {
                ChatTurnStatus.WAITING_APPROVAL,
                ChatTurnStatus.WAITING_CALLBACK,
            }:
                continue
            if turn is not None and turn.status in _TERMINAL_TURN_STATUS and turn.status != ChatTurnStatus.INTERRUPTED:
                await self._child_settled(record, turn)
                continue
            self.store.update(
                ChatSubagent,
                record.id,
                {
                    "status": ChatSubagentStatus.INTERRUPTED,
                    "finished_at": utc_now(),
                    "usage": turn.usage if turn is not None else record.usage,
                    "error": "Core restarted while this subagent was running.",
                },
                expected_revision=record.revision,
            )
            if self.chat.pending_turn(record.parent_session_id) is None:
                try:
                    self._post_result(self.get(record.id))
                except Exception as exc:
                    record_caught_exception(
                        "chat",
                        "chat.subagent.result_post_failed",
                        "A subagent report could not be added to the conversation.",
                        exc,
                        stage="subagent-restart",
                    )


class SubagentBroker:
    def __init__(self, service: SubagentService):
        self.service = service

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Any | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        arguments = invocation.arguments
        session_id = invocation.chat_session_id
        if not session_id:
            raise InvalidToolArguments("subagents require a provider chat session")
        name = invocation.tool_name
        if name == "start_subagent":
            record = await self.service.start(
                invocation,
                task=str(arguments.get("task") or ""),
                name=arguments.get("name") if isinstance(arguments.get("name"), str) else None,
                context=arguments.get("context") if isinstance(arguments.get("context"), str) else None,
            )
            if record.status == ChatSubagentStatus.FAILED:
                raise InvalidToolArguments(record.error or "subagent could not start")
            return ToolExecutionResult(
                output={
                    "subagent_id": record.id,
                    "name": record.name,
                    "status": "running",
                    "note": "Running in parallel. Call wait_subagents when you need its report.",
                }
            )
        if name == "list_subagents":
            return ToolExecutionResult(
                output={
                    "subagents": [
                        self.service._model_view(item, include_result=False)
                        for item in self.service.for_session(session_id)
                    ]
                }
            )
        if name == "stop_subagent":
            subagent_id = str(arguments.get("subagent_id") or "")
            try:
                record = self.service.get(subagent_id)
            except NotFoundError as exc:
                raise InvalidToolArguments(f"unknown subagent id {subagent_id!r}") from exc
            if record.parent_session_id != session_id:
                raise InvalidToolArguments(f"unknown subagent id {subagent_id!r}")
            record = await self.service.stop(record.id)
            return ToolExecutionResult(
                output={"subagent_id": record.id, "status": record.status.value}
            )
        if name == "wait_subagents":
            mode = "any" if arguments.get("mode") == "any" else "all"
            raw_ids = arguments.get("subagent_ids")
            ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else None
            records = self.service.resolve_wait(session_id, ids)
            if not records:
                raise InvalidToolArguments("there are no subagents to wait for")
            resolved = [item.id for item in records]
            if self.service.wait_satisfied(resolved, mode):
                return ToolExecutionResult(output=self.service.wait_output(resolved))
            raise SubagentWaitPending(resolved, mode)
        raise InvalidToolArguments(f"unsupported subagent capability {name!r}")


def _spec(name: str, description: str, properties: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
    )


def subagent_specs() -> dict[str, ToolSpec]:
    specs = [
        _spec(
            "start_subagent",
            "Delegate one independent multi-step task to a parallel subagent that "
            "uses the same model and tools. Returns immediately with its id.",
            {
                "task": {
                    "type": "string",
                    "description": "Complete, self-contained instructions and the expected report.",
                },
                "name": {
                    "type": ["string", "null"],
                    "description": "Short label shown to the operator, e.g. 'Map API routes'.",
                },
                "context": {
                    "type": ["string", "null"],
                    "description": "Facts from this conversation the subagent needs.",
                },
            },
        ),
        _spec(
            "wait_subagents",
            "Pause until subagents finish and return their reports. Omit ids to wait "
            "for every running subagent.",
            {
                "subagent_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "mode": {
                    "type": ["string", "null"],
                    "enum": ["all", "any", None],
                    "description": "all (default) waits for every listed subagent; any returns on the first.",
                },
            },
        ),
        _spec("list_subagents", "List this conversation's subagents and their status.", {}),
        _spec(
            "stop_subagent",
            "Stop a running subagent.",
            {"subagent_id": {"type": "string"}},
        ),
    ]
    return {spec.name: spec for spec in specs}


def subagent_components(
    service: SubagentService,
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    specs = subagent_specs()
    digest = hashlib.sha256(
        json.dumps(
            {name: spec.model_dump(mode="json") for name, spec in specs.items()},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return RuntimeToolComponents(
        broker=SubagentBroker(service),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=specs,
        runtime_digest=f"subagents-{digest[:16]}",
    )


__all__ = [
    "MAX_ACTIVE_SUBAGENTS",
    "MAX_SUBAGENTS_PER_TURN",
    "SUBAGENT_CHILD_INSTRUCTIONS",
    "SUBAGENT_ROUTING_INSTRUCTIONS",
    "SUBAGENT_TOOL_NAMES",
    "SubagentBroker",
    "SubagentService",
    "SubagentWaitPending",
    "is_subagent_session",
    "subagent_components",
    "subagent_specs",
]
