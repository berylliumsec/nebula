"""Core-owned subagents that run on a provider model.

A parent provider turn delegates a task with ``start_subagent``. Core creates a
child conversation bound to the same provider, model and capabilities, runs it
as an ordinary background provider turn, and reports the child's final answer
back: as a ``wait_subagents`` tool result when the parent is waiting, as an
update Core hands the parent before its next tool step while it works, and as a
durable result message in the parent conversation once the parent is idle.
Children cannot start their own subagents.

Parent and child talk both ways. The parent sends with ``message_subagent``: a
working child reads the message before its next step, a child waiting on a
question takes it as the answer, and a finished child runs another round with
it in the same conversation. The child sends with ``message_parent``,
optionally pausing until the parent replies; its messages reach the parent the
same way reports do. A question the parent cannot answer because its response
has ended is closed with that reason, so no child waits on an idle
conversation.

Every way a subagent ends reaches the parent: a start failure as the tool
error, and a finished, failed, stopped or interrupted round as a report that
carries the error, the tool steps that failed, the last step and any message
the child never read. A failure in Core's own bookkeeping fails the subagent
with that cause instead of leaving it running.

A harness chat (Codex, Grok) delegates the same way through the Nebula gateway
tools ``subagent.start``/``wait``/``list``/``message``/``stop``. Its children
run on the provider model the operator picked for that chat. The harness waits
inside the gateway call for a bounded time; while its turn runs, updates are
steered into it when the harness supports that, and whatever it has not
received is handed to it at the start of its next turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Collection, Iterable, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from .diagnostics import record_caught_exception
from .domain import (
    CHAT_SUBAGENT_TERMINAL_STATUSES,
    Approval,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatGoalUsageCharge,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatSubagent,
    ChatSubagentMessage,
    ChatSubagentMessageDirection,
    ChatSubagentMessageStatus,
    ChatSubagentStatus,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from .providers import REASONING_EFFORTS, ReasoningEffort
from .runtime_platform import RuntimeToolComponents
from .storage import ConflictError, NotFoundError
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec

if TYPE_CHECKING:
    from .chat import ChatService
    from .storage import NebulaStore

# Subagents are unlimited unless the operator sets how many may run at once
# for a conversation. The ceiling only bounds that setting.
SUBAGENT_LIMIT_CEILING = 100
RESULT_CHARACTERS = 12_000
MESSAGE_CHARACTERS = 20_000
RECENT_STEPS = 4
# Failed tool steps a report names; the count covers the rest.
REPORTED_TOOL_FAILURES = 8
# A harness waits inside one gateway call, which holds every other Nebula tool
# call of that session and must end well before the vendor's own tool timeout
# (Codex: 900 s). Unfinished children come back as still running. Harnesses
# whose timeout Nebula does not know wait for less (see harnesses.py).
HARNESS_WAIT_DEFAULT_SECONDS = 300
HARNESS_WAIT_MAX_SECONDS = 600
HARNESS_REPORT_CONTEXT_CHARACTERS = 40_000
SUBAGENT_TOOL_NAMES = frozenset(
    {
        "start_subagent",
        "wait_subagents",
        "list_subagents",
        "message_subagent",
        "stop_subagent",
    }
)
SUBAGENT_CHILD_TOOL_NAMES = frozenset({"message_parent", "read_parent_messages"})

SUBAGENT_ROUTING_INSTRUCTIONS = """
Subagents: start_subagent delegates one independent, multi-step task to a child
assistant with the same model and tools; it returns immediately and runs in
parallel. Give it a complete, self-contained task. Do not delegate single
lookups. Call wait_subagents when you need their reports before answering;
subagents that finish after your answer report back in the conversation.
message_subagent sends a subagent new instructions or answers its question; a
finished subagent starts another round with it. Messages, questions and
reports that arrive while you work are delivered as list_subagents results. A
subagent waiting for your reply stays paused until you answer or your response
ends. Reports name what failed: the error, failed tool steps and unread
messages."""

SUBAGENT_EFFORT_DESCRIPTION = (
    "How hard the subagent reasons: lower for routine, mechanical work, higher "
    "for hard analysis. Leave it unset to use this conversation's level, or the "
    "model's default when it has none."
)

SUBAGENT_CHILD_INSTRUCTIONS = """

You are a subagent. Another assistant working with the operator delegated one
task to you. Complete only that task with the available tools, then finish.
Your final answer is returned to that assistant as your report: lead with the
findings, keep it concise and factual, and say what you could not verify.
message_parent sends that assistant a message while you work: use it for a
finding it must act on before your report, or, with wait_for_reply, for a
decision only it can make. Do not send progress chatter. Its messages to you
arrive as read_parent_messages results and take precedence over the original
task where they differ."""

SUPERVISOR_APPROVAL_INSTRUCTIONS = (
    "A subagent is blocked on a tool approval. You own the child lifecycle: "
    "use stop_subagent, then message_subagent to restart it with safer or more "
    "specific instructions when appropriate. Do not leave the child waiting "
    "for the operator to manage directly."
)

PARENT_IDLE_NOTE = (
    "The delegating assistant is not working right now (its response has ended), "
    "so no reply is coming. Continue with your best judgment and state the "
    "assumption in your report. Your question was posted to its conversation."
)
IDLE_QUESTION_NOTE = (
    "It asked while you were not working and continued without your answer. "
    "Reply with message_subagent if it still needs one."
)


def subagent_limit(value: Any) -> int | None:
    """The operator's running-at-once limit from a snapshot, if one was set."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= SUBAGENT_LIMIT_CEILING else None


def subagent_routing_instructions(limit: int | None) -> str:
    """Routing instructions for a provider turn that may start subagents."""

    if limit is None:
        return SUBAGENT_ROUTING_INSTRUCTIONS
    return SUBAGENT_ROUTING_INSTRUCTIONS + (
        f" The operator allows at most {limit} running at once."
    )


def harness_subagent_instructions(
    model: str,
    wait_seconds: int = HARNESS_WAIT_DEFAULT_SECONDS,
    limit: int | None = None,
) -> str:
    """Developer instructions for a harness session with provider subagents."""

    return (
        "Provider subagents: subagent.start hands one independent, multi-step task "
        f"to a child assistant on the Nebula provider model {model} with this "
        "project's command runtime and MCP servers. It returns immediately and "
        "runs in parallel"
        + (f"; at most {limit} run at once" if limit is not None else "")
        + ". Children "
        "cannot see this conversation, so give complete, self-contained "
        "instructions and the expected report. Do not delegate single lookups. "
        "Call subagent.wait when you need their reports; it waits up to "
        f"{wait_seconds} seconds and returns anything still "
        "running, so call it again if needed. subagent.message sends a subagent "
        "new instructions or answers its question; a finished subagent starts "
        "another round with it. Subagents can message you and ask questions: "
        "their messages come back in subagent results and Nebula subagent "
        "updates, and a subagent waiting for your reply stays paused until you "
        "answer or your turn ends. Reports name what failed. Reports and "
        "messages that arrive after your turn ends are given to you at the start "
        "of your next turn. "
    )


_TERMINAL_TURN_STATUS = {
    ChatTurnStatus.COMPLETE: ChatSubagentStatus.COMPLETED,
    ChatTurnStatus.FAILED: ChatSubagentStatus.FAILED,
    ChatTurnStatus.CANCELLED: ChatSubagentStatus.STOPPED,
    ChatTurnStatus.INTERRUPTED: ChatSubagentStatus.INTERRUPTED,
}
_DETAIL_ARGUMENTS = ("command", "path", "query", "url", "target", "pattern", "name")
_FAILED_STEP_STATUSES = frozenset({"failed", "denied", "cancelled", "timed_out"})


class SubagentWaitPending(Exception):
    """Raised when a turn must pause: a parent until its subagents report, or
    a subagent until its parent replies.

    ``wait`` is kept on the paused tool step: ``{"ids", "mode"}`` for a parent,
    ``{"reply_to"}`` for a subagent's question.
    """

    def __init__(self, wait: dict[str, Any], summary: str):
        super().__init__(summary)
        self.wait = wait
        self.summary = summary


@dataclass
class ParentUpdate:
    """Messages and reports a parent has not received yet."""

    views: list[dict[str, Any]] = field(default_factory=list)
    messages: list[ChatSubagentMessage] = field(default_factory=list)
    records: list[ChatSubagent] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.records)


def is_subagent_session(session: ChatSession) -> bool:
    return isinstance(session.metadata.get("subagent_id"), str)


def _bounded(text: str, limit: int = RESULT_CHARACTERS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _known_effort(value: Any) -> ReasoningEffort | None:
    """A reasoning level a provider model can be asked for, else None."""

    return cast(ReasoningEffort, value) if value in REASONING_EFFORTS else None


def _step_detail(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in _DETAIL_ARGUMENTS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded(" ".join(value.split()), 120)
    return ""


def _step_error(entry: dict[str, Any]) -> str:
    """The reason a tool step failed, as its tool result recorded it."""

    persisted = entry.get("provider_result")
    if isinstance(persisted, str):
        try:
            persisted = json.loads(persisted)
        except (
            ValueError
        ):  # diagnostic-expected: plain-text results are reported as they are
            return _bounded(" ".join(persisted.split()), 300)
    if isinstance(persisted, dict):
        for key in ("detail", "error", "summary"):
            value = persisted.get(key)
            if isinstance(value, str) and value.strip():
                return _bounded(" ".join(value.split()), 300)
    summary = entry.get("result_summary")
    return _bounded(str(summary), 300) if summary else ""


def recoverable_after_core_restart(turn: ChatTurn) -> bool:
    """Whether Core owns automatic continuation of an interrupted turn."""

    if turn.status != ChatTurnStatus.INTERRUPTED:
        return False
    recovery = turn.request_snapshot.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("auto_resume_attempted_at"):
        return False
    if recovery.get("cause") not in {"core_shutdown", "core_restart"} and not (
        recovery.get("cause") is None
        and (turn.error or "").startswith(("Core stopped ", "Core restarted "))
    ):
        return False
    return not (
        recovery.get("unknown_tool_call_ids")
        or recovery.get("unknown_hook_execution_ids")
    )


# Compatibility for callers outside the lifecycle service.  Automatic recovery
# now covers both a graceful stop and a process restart.
safely_stopped_by_core = recoverable_after_core_restart


def _step_view(entry: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "step": entry.get("step"),
        "tool": str(entry.get("name") or ""),
        "status": str(entry.get("status") or ""),
    }
    detail = _step_detail(entry.get("arguments"))
    if detail:
        view["detail"] = detail
    return view


def _add_usage(first: ChatTokenUsage, second: ChatTokenUsage) -> ChatTokenUsage:
    return ChatTokenUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
    )


def _message_view(message: ChatSubagentMessage) -> dict[str, Any]:
    view: dict[str, Any] = {"message_id": message.id, "content": message.content}
    if message.expects_reply:
        view["question"] = True
        view["awaiting_reply"] = message.awaiting_reply
    note = _parent_note(message)
    if note:
        view["note"] = note
    return view


def _parent_note(message: ChatSubagentMessage) -> str | None:
    """Why a subagent's question closed without an answer, told to the parent."""

    if not message.expects_reply or message.awaiting_reply or not message.note:
        return None
    return IDLE_QUESTION_NOTE if message.note == PARENT_IDLE_NOTE else message.note


def _step_line(label: str, step: dict[str, Any]) -> str:
    detail = f" {step['detail']}" if step.get("detail") else ""
    return f"{label}: {step.get('tool')}{detail} ({step.get('status')})"


def _failure_lines(view: dict[str, Any]) -> list[str]:
    """What went wrong in a finished round, one line per fact."""

    lines: list[str] = []
    last = view.get("last_step")
    if isinstance(last, dict) and view.get("status") not in {"running", "completed"}:
        lines.append(_step_line("Last step", last))
    for failure in view.get("tool_failures", []):
        lines.append(
            _step_line(f"Failed step {failure.get('step')}", failure)
            + f": {failure.get('error') or 'no detail recorded'}"
        )
    if view.get("tool_failures_total"):
        lines.append(f"{view['tool_failures_total']} tool steps failed in all.")
    for message in view.get("undelivered_messages", []):
        lines.append(
            "Your message was not delivered "
            f"({message.get('note') or 'no reason recorded'}): {message['content']}"
        )
    return lines


def _view_text(view: dict[str, Any]) -> str:
    """One subagent's update as plain text, for a harness prompt or steer."""

    lines = [
        f"- {view['name']} (subagent_id {view['subagent_id']}, {view['status']}"
        + (f", round {view['round']}" if view.get("round") else "")
        + ")"
    ]
    for message in view.get("messages", []):
        if message.get("awaiting_reply"):
            label = "Question, waiting for your reply via subagent.message"
        elif message.get("question"):
            label = "Question"
        else:
            label = "Message"
        lines.append(f"{label}: {message['content']}")
        if message.get("note"):
            lines.append(f"({message['note']})")
    if view.get("waiting_for") == "your_reply":
        lines.append("It is paused until you reply with subagent.message.")
    elif view.get("waiting_for") == "operator_approval":
        approval = view.get("approval") or {}
        lines.append(
            "It is waiting for the operator to approve "
            f"{approval.get('tool') or 'a tool call'}."
        )
    if "report" in view:
        if view.get("report") or not view.get("error"):
            lines.append(f"Report: {view.get('report') or 'No report was produced.'}")
        if view.get("error"):
            lines.append(f"Error: {view['error']}")
        lines.extend(_failure_lines(view))
    return "\n  ".join(lines)


def update_text(update: ParentUpdate) -> str:
    """A parent update as plain text for a harness."""

    return _bounded(
        "\n\n".join(_view_text(view) for view in update.views),
        HARNESS_REPORT_CONTEXT_CHARACTERS,
    )


class SubagentService:
    """Start, observe, message, stop and deliver provider-chat subagents."""

    def __init__(self, store: NebulaStore, chat: ChatService):
        self.store = store
        self.chat = chat
        # Replaced on every settle so each harness wait wakes once per change.
        self._changed = asyncio.Event()
        # Parent conversations inside a harness subagent.wait: the wait returns
        # updates itself, so they are not steered into the turn as well.
        self._harness_waits: dict[str, int] = {}
        self._steer_locks: dict[str, asyncio.Lock] = {}
        # Set by the harness runtime: add text to the running harness turn of
        # a chat. Returns whether the harness took it.
        self.harness_steer: Callable[[str, str], Awaitable[bool]] | None = None

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    # -- queries -----------------------------------------------------------

    def for_session(self, parent_session_id: str) -> list[ChatSubagent]:
        return sorted(
            self.store.find_entities(
                ChatSubagent, {"parent_session_id": parent_session_id}
            ),
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
        except NotFoundError:  # diagnostic-expected: stale subagent reference means the session is not a live child
            return None

    def _child_turn(self, record: ChatSubagent) -> ChatTurn | None:
        if not record.child_turn_id:
            return None
        try:
            return self.store.get(ChatTurn, record.child_turn_id)
        except NotFoundError:  # diagnostic-expected: child turn not created or deleted; callers use the record alone
            return None

    def _owned(self, parent_session_id: str, subagent_id: str) -> ChatSubagent:
        try:
            record = self.get(subagent_id)
        except NotFoundError as exc:
            raise InvalidToolArguments(f"unknown subagent id {subagent_id!r}") from exc
        if record.parent_session_id != parent_session_id:
            raise InvalidToolArguments(f"unknown subagent id {subagent_id!r}")
        return record

    @staticmethod
    def active(records: Iterable[ChatSubagent]) -> list[ChatSubagent]:
        return [
            item
            for item in records
            if item.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
        ]

    def messages_for(
        self,
        subagent_id: str,
        direction: ChatSubagentMessageDirection | None = None,
        status: ChatSubagentMessageStatus | None = None,
    ) -> list[ChatSubagentMessage]:
        filters: dict[str, Any] = {"subagent_id": subagent_id}
        if direction is not None:
            filters["direction"] = direction.value
        if status is not None:
            filters["status"] = status.value
        return self.store.find_entities(ChatSubagentMessage, filters)

    def open_question(self, subagent_id: str) -> ChatSubagentMessage | None:
        for message in reversed(
            self.messages_for(subagent_id, ChatSubagentMessageDirection.TO_PARENT)
        ):
            if message.awaiting_reply:
                return message
        return None

    def _reported(self, record: ChatSubagent) -> bool:
        """Whether the parent model already received this round's report."""

        if record.reported_at is not None:
            return True
        # A provider parent reads a posted result from its history. Records
        # from before reported_at covered provider parents have only the post.
        return (
            record.parent_backend == ChatBackend.PROVIDER
            and record.result_message_id is not None
        )

    def view(self, record: ChatSubagent) -> dict[str, Any]:
        """Return the operator-facing state, overlaying the live child turn."""

        turn = self._child_turn(record)
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
        question: dict[str, Any] | None = None
        if record.status == ChatSubagentStatus.RUNNING and turn is not None:
            if turn.status == ChatTurnStatus.INTERRUPTED:
                state = "recovering"
            if turn.status == ChatTurnStatus.WAITING_APPROVAL and turn.approval_id:
                state = "waiting_approval"
                try:
                    pending = self.store.get(Approval, turn.approval_id)
                except NotFoundError:  # diagnostic-expected: approval deleted; the view reports it as pending
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
            open_question = self.open_question(record.id)
            if open_question is not None:
                question = {"id": open_question.id, "content": open_question.content}
        usage = (
            _add_usage(record.usage, turn.usage)
            if turn is not None and record.status == ChatSubagentStatus.RUNNING
            else record.usage
        )
        finished = record.finished_at or utc_now()
        provider_profile_id, model = record.provider_profile_id, record.model
        if model is None:
            # Records from before children could run on another model.
            try:
                child = self.store.get(ChatSession, record.child_session_id)
                provider_profile_id, model = child.provider_profile_id, child.model
            except NotFoundError:  # diagnostic-expected: child conversation deleted; the view omits its model
                pass
        return {
            "id": record.id,
            "name": record.name,
            "task": record.task,
            "status": state,
            "parent_session_id": record.parent_session_id,
            "parent_turn_id": record.parent_turn_id,
            "parent_backend": record.parent_backend.value,
            "provider_profile_id": provider_profile_id,
            "model": model,
            "reasoning_effort": record.reasoning_effort,
            "child_session_id": record.child_session_id,
            "child_turn_id": record.child_turn_id,
            "rounds": record.rounds,
            "step_count": turn.next_step if turn is not None else 0,
            "recent_steps": recent,
            "approval": approval,
            "question": question,
            "usage": usage.model_dump(mode="json"),
            "started_at": record.started_at.isoformat(),
            "finished_at": record.finished_at.isoformat()
            if record.finished_at
            else None,
            "elapsed_seconds": max(0.0, (finished - record.started_at).total_seconds()),
            "result": record.result,
            "error": (
                turn.error
                if state == "recovering" and turn is not None
                else record.error
            ),
            "result_message_id": record.result_message_id,
        }

    def _model_view(
        self, record: ChatSubagent, *, include_result: bool
    ) -> dict[str, Any]:
        """What the parent model sees of one subagent."""

        turn = self._child_turn(record)
        history = list(turn.tool_history) if turn is not None else []
        payload: dict[str, Any] = {
            "subagent_id": record.id,
            "name": record.name,
            "status": record.status.value,
            "steps": turn.next_step if turn is not None else 0,
        }
        if record.rounds > 1:
            payload["round"] = record.rounds
        if record.status == ChatSubagentStatus.RUNNING:
            question = self.open_question(record.id)
            if question is not None:
                payload["waiting_for"] = "your_reply"
                payload["question_id"] = question.id
            elif (
                turn is not None
                and turn.status == ChatTurnStatus.WAITING_APPROVAL
                and history
            ):
                payload["waiting_for"] = "operator_approval"
                payload["approval"] = {
                    key: value
                    for key, value in _step_view(history[-1]).items()
                    if key in {"tool", "detail"}
                }
            if history:
                payload["last_step"] = _step_view(history[-1])
            return payload
        if not include_result:
            return payload
        payload["report"] = record.result or None
        payload["error"] = record.error
        if history and record.status != ChatSubagentStatus.COMPLETED:
            payload["last_step"] = _step_view(history[-1])
        failures = [
            {**_step_view(entry), "error": _step_error(entry)}
            for entry in history
            if entry.get("status") in _FAILED_STEP_STATUSES
        ]
        if failures:
            payload["tool_failures"] = failures[-REPORTED_TOOL_FAILURES:]
            if len(failures) > REPORTED_TOOL_FAILURES:
                payload["tool_failures_total"] = len(failures)
        round_started = turn.created_at if turn is not None else record.started_at
        undelivered = [
            {
                "message_id": item.id,
                "content": _bounded(item.content, 500),
                "note": item.note,
            }
            for item in self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_CHILD,
                ChatSubagentMessageStatus.UNDELIVERED,
            )
            if item.created_at >= round_started
        ]
        if undelivered:
            payload["undelivered_messages"] = undelivered
        return payload

    def _parent_update(
        self, records: Iterable[ChatSubagent], *, all_reports: bool, everyone: bool
    ) -> ParentUpdate:
        """Views of ``records`` with what the parent has not received.

        A finished subagent carries its report when ``all_reports`` is set or
        the parent has not received it; ``everyone`` keeps subagents that have
        nothing new.
        """

        update = ParentUpdate()
        for record in records:
            unreported = (
                record.status in CHAT_SUBAGENT_TERMINAL_STATUSES
                and not self._reported(record)
            )
            pending = self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_PARENT,
                ChatSubagentMessageStatus.PENDING,
            )
            if not (everyone or unreported or pending):
                continue
            view = self._model_view(record, include_result=all_reports or unreported)
            if pending:
                view["messages"] = [_message_view(item) for item in pending]
                update.messages.extend(pending)
            if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES and "report" in view:
                update.records.append(record)
            update.views.append(view)
        return update

    def pending_update(
        self, parent_session_id: str, *, everyone: bool = False
    ) -> ParentUpdate:
        return self._parent_update(
            self.for_session(parent_session_id), all_reports=False, everyone=everyone
        )

    def mark_delivered(self, update: ParentUpdate) -> None:
        self._mark_messages(update.messages, ChatSubagentMessageStatus.DELIVERED)
        self.mark_reported(item.id for item in update.records)

    def _mark_messages(
        self,
        messages: Iterable[ChatSubagentMessage],
        status: ChatSubagentMessageStatus,
        note: str | None = None,
    ) -> None:
        now = utc_now()
        for message in messages:
            for _ in range(3):
                try:
                    latest = self.store.get(ChatSubagentMessage, message.id)
                except (
                    NotFoundError
                ):  # diagnostic-expected: message deleted with its conversation
                    break
                if latest.status != ChatSubagentMessageStatus.PENDING:
                    break
                try:
                    self.store.update(
                        ChatSubagentMessage,
                        latest.id,
                        {
                            "status": status,
                            **(
                                {"delivered_at": now}
                                if status == ChatSubagentMessageStatus.DELIVERED
                                else {}
                            ),
                            **({"note": _bounded(note, 1_000)} if note else {}),
                        },
                        expected_revision=latest.revision,
                    )
                    break
                except ConflictError:  # diagnostic-expected: a concurrent post or close won; reread and retry
                    continue

    def _add_message(
        self,
        record: ChatSubagent,
        direction: ChatSubagentMessageDirection,
        content: str,
        *,
        expects_reply: bool = False,
        idempotency_key: str | None = None,
    ) -> ChatSubagentMessage:
        return self.store.create(
            ChatSubagentMessage(
                engagement_id=record.engagement_id,
                subagent_id=record.id,
                parent_session_id=record.parent_session_id,
                direction=direction,
                content=_bounded(content, MESSAGE_CHARACTERS),
                expects_reply=expects_reply,
                awaiting_reply=expects_reply,
                idempotency_key=idempotency_key,
            )
        )

    def _existing_message(
        self, record: ChatSubagent, idempotency_key: str | None
    ) -> ChatSubagentMessage | None:
        if not idempotency_key:
            return None
        found = self.store.find_entities(
            ChatSubagentMessage,
            {"subagent_id": record.id, "idempotency_key": idempotency_key},
        )
        return found[0] if found else None

    def _close_question(self, message: ChatSubagentMessage, note: str | None) -> None:
        for _ in range(3):
            try:
                latest = self.store.get(ChatSubagentMessage, message.id)
            except (
                NotFoundError
            ):  # diagnostic-expected: message deleted with its conversation
                return
            if not latest.awaiting_reply:
                return
            try:
                self.store.update(
                    ChatSubagentMessage,
                    latest.id,
                    {
                        "awaiting_reply": False,
                        **({"note": _bounded(note, 1_000)} if note else {}),
                    },
                    expected_revision=latest.revision,
                )
                return
            except (
                ConflictError
            ):  # diagnostic-expected: a concurrent delivery won; reread and retry
                continue

    # -- start -------------------------------------------------------------

    async def start(
        self,
        invocation: ToolInvocation,
        *,
        task: str,
        name: str | None,
        context: str | None,
        reasoning_effort: str | None = None,
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
        requested_effort = _known_effort(reasoning_effort)
        if reasoning_effort is not None and requested_effort is None:
            raise InvalidToolArguments(
                "reasoning_effort must be one of " + ", ".join(REASONING_EFFORTS)
            )
        label = " ".join((name or task).split())[:80] or "Subagent"
        siblings = self.for_session(parent_session.id)
        for existing in siblings:
            if (
                existing.parent_request.get("idempotency_key")
                == invocation.idempotency_key
            ):
                return existing
        snapshot = parent_turn.request_snapshot
        limit = self._limit(parent_turn)
        if limit is not None and len(self.active(siblings)) >= limit:
            raise InvalidToolArguments(
                f"the operator allows {limit} running at once and {limit} "
                "already are; wait for one to finish"
            )
        mcp_server_ids = [
            item for item in snapshot.get("mcp_server_ids", []) if isinstance(item, str)
        ]
        if parent_turn.backend == ChatBackend.HARNESS:
            setting = snapshot.get("provider_subagent")
            if not isinstance(setting, dict):
                raise InvalidToolArguments(
                    "provider subagents are turned off for this conversation"
                )
            provider_id = str(setting.get("provider_profile_id") or "")
            model = str(setting.get("model") or "")
            # Children use Nebula's command runtime whenever the harness
            # session has one; its vendor-native shell stays with the harness.
            runtime_snapshot = snapshot.get("command_runtime_snapshot")
            tools_enabled = (
                isinstance(runtime_snapshot, dict)
                and bool(runtime_snapshot.get("tool_names"))
                and self.chat.automation_tool_platform is not None
            )
            allow_subagents = False
            # The harness's own level carries over when a provider model takes
            # it too; a vendor-only level leaves the child at its default.
            runtime_options = snapshot.get("harness_runtime_options")
            inherited_effort = _known_effort(
                runtime_options.get("reasoning_effort")
                if isinstance(runtime_options, dict)
                else None
            )
        else:
            provider_id = parent_turn.provider_profile_id or ""
            model = parent_turn.model
            tools_enabled = bool(snapshot.get("include_oci_tools", False))
            allow_subagents = bool(snapshot.get("allow_subagents", False))
            # A child is a new turn, so it reads the conversation's current
            # level like any other; the operator may have changed it mid-turn.
            inherited_effort = _known_effort(
                parent_session.metadata.get("reasoning_effort")
            )
        effort = requested_effort or inherited_effort
        if not provider_id or not model:
            raise InvalidToolArguments("subagents need a provider model")
        parent_request: dict[str, Any] = {
            "idempotency_key": invocation.idempotency_key,
            "tools_enabled": tools_enabled,
            "mcp_server_ids": mcp_server_ids,
            "allow_subagents": allow_subagents,
            "max_active_subagents": limit if allow_subagents else None,
        }
        subagent_id = str(uuid4())
        child_session = ChatSession(
            id=str(uuid4()),
            engagement_id=parent_session.engagement_id,
            title=f"Subagent · {label}"[:300],
            provider_profile_id=provider_id,
            model=model,
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
            parent_backend=parent_turn.backend,
            child_session_id=child_session.id,
            provider_profile_id=provider_id,
            model=model,
            reasoning_effort=effort,
            name=label,
            task=_bounded(task, 20_000),
            parent_request=parent_request,
        )
        with self.store.transaction() as transaction:
            transaction.add(child_session)
            transaction.add(record)
        content = (
            task
            if not context or not context.strip()
            else (
                f"{task}\n\nContext from the delegating assistant:\n{context.strip()}"
            )
        )
        child_turn_id: str | None = None
        try:
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    provider_id=provider_id,
                    engagement_id=parent_session.engagement_id,
                    session_id=child_session.id,
                    model=model,
                    messages=[
                        ChatRequestMessage(
                            role=ChatRole.USER, content=_bounded(content, 60_000)
                        )
                    ],
                    include_knowledge=False,
                    tools_enabled=tools_enabled,
                    mcp_server_ids=mcp_server_ids,
                    ssh_environment_ids=self._ssh_environment_ids(record),
                    # A provider parent only reached tool routing after its own
                    # cloud-transfer confirmation (or with a local provider). A
                    # harness chat's operator consented by turning on provider
                    # subagents for this model.
                    allow_cloud_tool_results=True,
                    reasoning_effort=effort,
                    stream=True,
                )
            )
            if prepared.turn is None:
                raise RuntimeError("subagent turn was not created")
            child_turn_id = prepared.turn.id
            record = self.store.update(
                ChatSubagent,
                record.id,
                {"child_turn_id": child_turn_id},
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
            error = _bounded(
                f"Subagent could not start ({type(exc).__name__}): {exc}", 1_000
            )
            self._discard_unstarted(record, child_session, child_turn_id, error)
            raise InvalidToolArguments(error) from exc
        return record

    @staticmethod
    def _ssh_environment_ids(record: ChatSubagent) -> list[str] | None:
        # Harness chats have no SSH selection; never hand a child every host.
        return [] if record.parent_backend == ChatBackend.HARNESS else None

    @staticmethod
    def _limit(parent_turn: ChatTurn) -> int | None:
        snapshot = parent_turn.request_snapshot
        harness_setting = snapshot.get("provider_subagent")
        return subagent_limit(
            harness_setting.get("max_active")
            if parent_turn.backend == ChatBackend.HARNESS
            and isinstance(harness_setting, dict)
            else snapshot.get("max_active_subagents")
        )

    def _discard_unstarted(
        self,
        record: ChatSubagent,
        child_session: ChatSession,
        child_turn_id: str | None,
        error: str,
    ) -> None:
        """Remove what a subagent that never ran left behind.

        The failure goes back to the model as the tool result, so keeping the
        record would only leave an empty "Subagent · …" conversation in the
        list and post the same failure again once the parent replies. If the
        cleanup itself fails the record is kept terminal, so it neither blocks
        deleting the parent nor counts against the concurrency cap.
        """

        try:
            if child_turn_id is not None:
                self.chat.cancel_turn(child_turn_id)
            self.store.delete_chat_session(child_session.id)
            self.store.delete(ChatSubagent, record.id)
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.subagent.start_cleanup_failed",
                "A subagent that could not start left its records behind.",
                exc,
                stage="subagent-start",
            )
            try:
                latest = self.get(record.id)
            except (
                NotFoundError
            ):  # diagnostic-expected: the record went with the child conversation
                return
            if latest.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
                return
            self.store.update(
                ChatSubagent,
                latest.id,
                {
                    "status": ChatSubagentStatus.FAILED,
                    "finished_at": utc_now(),
                    "error": error,
                    # The parent already has this failure as its tool error.
                    "reported_at": utc_now(),
                },
                expected_revision=latest.revision,
            )

    # -- rounds ------------------------------------------------------------

    async def _start_round(
        self,
        record: ChatSubagent,
        *,
        parent_turn_id: str | None,
        usage: ChatTokenUsage | None = None,
    ) -> ChatSubagent:
        """Run the child again, in its own conversation, on the parent's
        unread messages. The record is running again when this returns."""

        from .chat import ChatCompletionRequest, ChatRequestMessage

        pending = self.messages_for(
            record.id,
            ChatSubagentMessageDirection.TO_CHILD,
            ChatSubagentMessageStatus.PENDING,
        )
        if not pending:
            raise RuntimeError("there is no unread message to start a round with")
        body = (
            pending[0].content
            if len(pending) == 1
            else "\n\n".join(
                f"{index}. {item.content}" for index, item in enumerate(pending, 1)
            )
        )
        model = record.model
        provider_id = record.provider_profile_id
        if not model or not provider_id:
            child = self.store.get(ChatSession, record.child_session_id)
            model = model or child.model
            provider_id = provider_id or child.provider_profile_id
        flags = record.parent_request
        prepared = await self.chat.prepare_async(
            ChatCompletionRequest(
                provider_id=provider_id or "",
                engagement_id=record.engagement_id,
                session_id=record.child_session_id,
                model=model,
                messages=[
                    ChatRequestMessage(
                        role=ChatRole.USER,
                        content=_bounded(
                            "The delegating assistant sent you "
                            + ("a message" if len(pending) == 1 else "messages")
                            + f" after your last report:\n\n{body}",
                            60_000,
                        ),
                    )
                ],
                include_knowledge=False,
                tools_enabled=bool(flags.get("tools_enabled")),
                mcp_server_ids=list(flags.get("mcp_server_ids") or []),
                ssh_environment_ids=self._ssh_environment_ids(record),
                allow_cloud_tool_results=True,
                reasoning_effort=_known_effort(record.reasoning_effort),
                stream=True,
            )
        )
        if prepared.turn is None:
            raise RuntimeError("subagent turn was not created")
        try:
            latest = self.get(record.id)
            record = self.store.update(
                ChatSubagent,
                latest.id,
                {
                    "status": ChatSubagentStatus.RUNNING,
                    "finished_at": None,
                    "result": "",
                    "error": None,
                    "result_message_id": None,
                    "reported_at": None,
                    "child_turn_id": prepared.turn.id,
                    "rounds": latest.rounds + 1,
                    **({"usage": usage} if usage is not None else {}),
                    **(
                        {"parent_turn_id": parent_turn_id}
                        if parent_turn_id is not None
                        else {}
                    ),
                },
                expected_revision=latest.revision,
            )
        except Exception:
            self.chat.cancel_turn(prepared.turn.id)
            raise
        self._mark_messages(pending, ChatSubagentMessageStatus.DELIVERED)
        self.chat.start_provider_turn(prepared)
        self._notify()
        return record

    # -- messages ----------------------------------------------------------

    async def send_to_child(
        self,
        parent_session_id: str,
        subagent_id: str,
        content: str,
        *,
        parent_turn_id: str | None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Deliver a parent's message to one of its subagents."""

        record = self._owned(parent_session_id, subagent_id)
        content = content.strip()
        if not content:
            raise InvalidToolArguments("message must say something")
        existing = self._existing_message(record, idempotency_key)
        if existing is not None:
            return {
                "subagent_id": record.id,
                "message_id": existing.id,
                "status": record.status.value,
                "delivery": "already_sent",
            }
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            limit = (
                self._limit(self.store.get(ChatTurn, parent_turn_id))
                if parent_turn_id
                else None
            )
            if (
                limit is not None
                and len(self.active(self.for_session(parent_session_id))) >= limit
            ):
                raise InvalidToolArguments(
                    f"{record.name} has {record.status.value}, and another round "
                    f"would exceed the operator's limit of {limit} running at "
                    "once; wait for one to finish"
                )
            message = self._add_message(
                record,
                ChatSubagentMessageDirection.TO_CHILD,
                content,
                idempotency_key=idempotency_key,
            )
            previous = record.status.value
            try:
                record = await self._start_round(record, parent_turn_id=parent_turn_id)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.round_start_failed",
                    "A message could not start another subagent round.",
                    exc,
                    stage="subagent-message",
                )
                reason = _bounded(
                    f"Another round could not start ({type(exc).__name__}): {exc}",
                    1_000,
                )
                self._mark_messages(
                    [message], ChatSubagentMessageStatus.UNDELIVERED, reason
                )
                raise InvalidToolArguments(
                    f"{reason} The message was not delivered."
                ) from exc
            return {
                "subagent_id": record.id,
                "message_id": message.id,
                "status": record.status.value,
                "round": record.rounds,
                "delivery": "new_round",
                "note": (
                    f"It had {previous}. Your message started round "
                    f"{record.rounds} in the same conversation, which keeps its "
                    "earlier work; its report arrives like the first."
                ),
            }
        message = self._add_message(
            record,
            ChatSubagentMessageDirection.TO_CHILD,
            content,
            idempotency_key=idempotency_key,
        )
        question = self.open_question(record.id)
        if question is not None:
            self._close_question(question, None)
        turn = self._child_turn(record)
        output: dict[str, Any] = {
            "subagent_id": record.id,
            "message_id": message.id,
            "status": record.status.value,
        }
        if question is not None:
            output["delivery"] = "answered"
            output["note"] = (
                "It was paused on its question; your message is the answer and "
                "it has resumed."
            )
            if turn is not None:
                await self._resume_waiting_turn(turn)
        elif turn is not None and turn.status == ChatTurnStatus.WAITING_APPROVAL:
            pending_step = turn.tool_history[-1] if turn.tool_history else {}
            output["delivery"] = "queued"
            output["note"] = (
                "It is waiting for the operator to approve "
                f"{pending_step.get('name') or 'a tool call'}; it reads your "
                "message when it continues."
            )
        elif turn is not None and turn.status == ChatTurnStatus.FINALIZING:
            output["delivery"] = "queued"
            output["note"] = (
                "It is writing its report; your message starts another round "
                "right after it."
            )
        else:
            output["delivery"] = "queued"
            output["note"] = "It reads your message before its next step."
        return output

    async def send_to_parent(
        self,
        invocation: ToolInvocation,
        content: str,
        *,
        wait_for_reply: bool,
    ) -> ChatSubagentMessage:
        """Deliver a subagent's message, or question, to its parent."""

        record = self._invoking_child(invocation)
        content = content.strip()
        if not content:
            raise InvalidToolArguments("message must say something")
        existing = self._existing_message(record, invocation.idempotency_key)
        if existing is not None:
            return existing
        message = self._add_message(
            record,
            ChatSubagentMessageDirection.TO_PARENT,
            content,
            expects_reply=wait_for_reply,
            idempotency_key=invocation.idempotency_key,
        )
        if not wait_for_reply:
            # A question is delivered once the child has paused for it, so a
            # reply always finds a turn to resume.
            await self._deliver(record)
        return message

    def read_parent_messages(self, invocation: ToolInvocation) -> dict[str, Any]:
        record = self._invoking_child(invocation)
        return self._child_inbox(record) or {
            "messages": [],
            "note": "No new messages from the delegating assistant.",
        }

    def _invoking_child(self, invocation: ToolInvocation) -> ChatSubagent:
        if not invocation.chat_session_id:
            raise InvalidToolArguments("only a subagent can message its parent")
        session = self.store.get(ChatSession, invocation.chat_session_id)
        record = self._for_child_session(session)
        if record is None:
            raise InvalidToolArguments("only a subagent can message its parent")
        if (
            record.status in CHAT_SUBAGENT_TERMINAL_STATUSES
            or record.child_turn_id != invocation.chat_turn_id
        ):
            raise InvalidToolArguments("this subagent round has already ended")
        return record

    def _child_inbox(self, record: ChatSubagent) -> dict[str, Any] | None:
        pending = self.messages_for(
            record.id,
            ChatSubagentMessageDirection.TO_CHILD,
            ChatSubagentMessageStatus.PENDING,
        )
        if not pending:
            return None
        self._mark_messages(pending, ChatSubagentMessageStatus.DELIVERED)
        return {
            "messages": [
                {"message_id": item.id, "content": item.content} for item in pending
            ],
            "note": (
                "From the assistant that delegated your task. Act on them; they "
                "take precedence over the original task where they differ."
            ),
        }

    def routing_delivery(
        self, turn: ChatTurn, tool_names: Collection[str]
    ) -> tuple[str, dict[str, Any], str] | None:
        """What a working provider turn must receive before its next step.

        Returns the tool whose result carries it, the result and a summary:
        ``read_parent_messages`` for a subagent with unread parent messages,
        ``list_subagents`` for a parent with unread subagent messages or
        reports.
        """

        try:
            session = self.store.get(ChatSession, turn.session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: conversation deleted mid-turn; nothing to deliver
            return None
        if is_subagent_session(session):
            if "read_parent_messages" not in tool_names:
                return None
            record = self._for_child_session(session)
            if record is None or record.child_turn_id != turn.id:
                return None
            inbox = self._child_inbox(record)
            if inbox is None:
                return None
            count = len(inbox["messages"])
            return (
                "read_parent_messages",
                inbox,
                f"{count} message{'' if count == 1 else 's'} from the delegating assistant",
            )
        if "list_subagents" not in tool_names or not self._has_news(session.id):
            return None
        update = self.pending_update(session.id, everyone=True)
        self.mark_delivered(update)
        output: dict[str, Any] = {
            "delivered_by": "nebula",
            "note": (
                "Core delivered this update because your subagents sent messages "
                "or finished since your last step."
            ),
            "subagents": update.views,
        }
        waiting = [
            view["subagent_id"]
            for view in update.views
            if view.get("waiting_for") == "your_reply"
        ]
        if waiting:
            output["awaiting_your_reply"] = waiting
        return "list_subagents", output, self._update_summary(update)

    def _has_news(self, parent_session_id: str) -> bool:
        """Cheaply: does the parent have an unread subagent message or report?"""

        if any(
            item.status in CHAT_SUBAGENT_TERMINAL_STATUSES and not self._reported(item)
            for item in self.for_session(parent_session_id)
        ):
            return True
        return bool(
            self.store.find_entities(
                ChatSubagentMessage,
                {
                    "parent_session_id": parent_session_id,
                    "direction": ChatSubagentMessageDirection.TO_PARENT.value,
                    "status": ChatSubagentMessageStatus.PENDING.value,
                },
                limit=1,
            )
        )

    @staticmethod
    def _update_summary(update: ParentUpdate) -> str:
        parts: list[str] = []
        if update.messages:
            count = len(update.messages)
            parts.append(f"{count} subagent message{'' if count == 1 else 's'}")
        if update.records:
            count = len(update.records)
            parts.append(f"{count} subagent report{'' if count == 1 else 's'}")
        return " and ".join(parts) + " received" if parts else "Subagent status"

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
            self._notify()
            await self._deliver(record)
        return record

    async def stop_for_parent_turn(self, parent_turn_id: str) -> None:
        for record in self.store.find_entities(
            ChatSubagent,
            {
                "parent_turn_id": parent_turn_id,
                "status": ChatSubagentStatus.RUNNING.value,
            },
        ):
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

    def resolve_wait(
        self, parent_session_id: str, ids: list[str] | None
    ) -> list[ChatSubagent]:
        records = self.for_session(parent_session_id)
        if ids:
            by_id = {item.id: item for item in records}
            missing = [item for item in ids if item not in by_id]
            if missing:
                raise InvalidToolArguments(
                    f"unknown subagent ids: {', '.join(missing)}"
                )
            return [by_id[item] for item in dict.fromkeys(ids)]
        return self.active(records) or [
            item for item in records if item.result_message_id is None
        ]

    def wait_satisfied(self, ids: list[str], mode: str) -> bool:
        """A parent wait ends when its subagents finish (all or any), or as
        soon as one of them sends a message or waits on a question."""

        records = [self.get(item) for item in ids]
        for record in records:
            if self.open_question(record.id) is not None or self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_PARENT,
                ChatSubagentMessageStatus.PENDING,
            ):
                return True
        finished = [
            item for item in records if item.status in CHAT_SUBAGENT_TERMINAL_STATUSES
        ]
        return bool(finished) if mode == "any" else len(finished) == len(records)

    def wait_ready(self, wait: dict[str, Any]) -> bool:
        reply_to = wait.get("reply_to")
        if isinstance(reply_to, str):
            try:
                question = self.store.get(ChatSubagentMessage, reply_to)
            except NotFoundError:  # diagnostic-expected: question deleted with its conversation; stop waiting
                return True
            return not question.awaiting_reply
        ids = [str(item) for item in wait.get("ids") or []]
        return self.wait_satisfied(ids, "any" if wait.get("mode") == "any" else "all")

    def wait_result(self, wait: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The tool result that ends a satisfied wait, and its summary."""

        reply_to = wait.get("reply_to")
        if isinstance(reply_to, str):
            try:
                question = self.store.get(ChatSubagentMessage, reply_to)
                record = self.get(question.subagent_id)
            except (
                NotFoundError
            ):  # diagnostic-expected: the conversation went away while it waited
                return {
                    "reply": None,
                    "note": "The delegating conversation no longer exists.",
                }, "No reply"
            inbox = self._child_inbox(record)
            if inbox is not None:
                count = len(inbox["messages"])
                return inbox, f"{count} repl{'y' if count == 1 else 'ies'} received"
            return {
                "reply": None,
                "note": question.note or "No reply arrived.",
            }, "No reply"
        ids = [str(item) for item in wait.get("ids") or []]
        output = self.wait_output(ids)
        received = len(ids) - len(output["still_running"])
        summary = f"{received} subagent report{'' if received == 1 else 's'} received"
        if output.get("awaiting_your_reply"):
            summary += "; a subagent is waiting for your reply"
        elif any(view.get("messages") for view in output["subagents"]):
            summary += "; subagent messages received"
        return output, summary

    def wait_output(self, ids: list[str]) -> dict[str, Any]:
        records = [self.get(item) for item in ids]
        update = self._parent_update(records, all_reports=True, everyone=True)
        self.mark_delivered(update)
        output: dict[str, Any] = {
            "subagents": update.views,
            "still_running": [
                item.id
                for item in records
                if item.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
            ],
        }
        waiting = [
            view["subagent_id"]
            for view in update.views
            if view.get("waiting_for") == "your_reply"
        ]
        if waiting:
            output["awaiting_your_reply"] = waiting
            output["note"] = (
                "These subagents are paused until you answer their question with "
                "a message; waiting again returns at once while they are."
            )
        return output

    def list_output(self, parent_session_id: str) -> dict[str, Any]:
        update = self.pending_update(parent_session_id, everyone=True)
        self.mark_delivered(update)
        return {"subagents": update.views}

    # -- harness parents ---------------------------------------------------

    def validate_harness_setting(
        self,
        engagement_id: str,
        provider_profile_id: str,
        model: str,
        max_active: int | None = None,
    ) -> dict[str, Any]:
        """Check a harness chat's subagent model before any turn relies on it.

        Children always run with tools, so the model must have passed the tool
        check, and a cloud provider must accept project data. Turning provider
        subagents on is the operator's consent to send the tool results.
        """

        from .chat import ChatConfigurationError, ChatPrivacyError

        provider_profile_id = provider_profile_id.strip()
        model = model.strip()
        if not provider_profile_id or not model:
            raise ChatConfigurationError(
                "provider subagents need a provider and a model"
            )
        try:
            profile = self.store.get(ProviderProfile, provider_profile_id)
        except NotFoundError as exc:
            raise ChatConfigurationError(
                f"subagent provider {provider_profile_id!r} does not exist"
            ) from exc
        if not profile.enabled:
            raise ChatConfigurationError(
                f"subagent provider {profile.name!r} is disabled"
            )
        if profile.model_allowlist and model not in profile.model_allowlist:
            raise ChatConfigurationError(
                f"model {model!r} is not allowed by provider {profile.name!r}"
            )
        if not profile.tools_verified_for(model):
            raise ChatConfigurationError(
                f"subagent model {model!r} has not passed the tool check; verify it "
                "before using it for subagents"
            )
        provider = self.chat.provider_factory(profile)
        self.chat._enforce_engagement_privacy(
            self.store.get(Engagement, engagement_id), provider
        )
        if not provider.config.local and not profile.privacy.permits_sensitive_data:
            raise ChatPrivacyError(
                f"provider {profile.name!r} does not permit project data, so it "
                "cannot run subagents"
            )
        if max_active is not None and subagent_limit(max_active) is None:
            raise ChatConfigurationError(
                f"the subagent limit must be between 1 and {SUBAGENT_LIMIT_CEILING}"
            )
        # No key means no limit, so settings saved before limits existed match.
        return {
            "provider_profile_id": profile.id,
            "model": model,
            **({"max_active": max_active} if max_active is not None else {}),
        }

    async def wait_for(
        self,
        parent_session_id: str,
        ids: list[str] | None,
        mode: str,
        timeout_seconds: float,
        *,
        still_waiting: Callable[[], bool],
    ) -> dict[str, Any]:
        """Wait inside a harness gateway call, bounded, then report."""

        records = self.resolve_wait(parent_session_id, ids)
        if not records:
            raise InvalidToolArguments("there are no subagents to wait for")
        resolved = [item.id for item in records]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        self._harness_waits[parent_session_id] = (
            self._harness_waits.get(parent_session_id, 0) + 1
        )
        try:
            while not self.wait_satisfied(resolved, mode) and still_waiting():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                changed = self._changed
                try:
                    # Settles wake the wait; the short poll also covers a change
                    # made by another worker or a missed notification.
                    await asyncio.wait_for(changed.wait(), timeout=min(remaining, 2.0))
                except (
                    asyncio.TimeoutError
                ):  # diagnostic-expected: poll interval elapsed; re-check the children
                    pass
            return self.wait_output(resolved)
        finally:
            remaining_waits = self._harness_waits.get(parent_session_id, 1) - 1
            if remaining_waits > 0:
                self._harness_waits[parent_session_id] = remaining_waits
            else:
                self._harness_waits.pop(parent_session_id, None)

    def mark_reported(self, ids: Iterable[str]) -> None:
        """Record that a parent model received these finished reports."""

        for subagent_id in ids:
            try:
                record = self.get(subagent_id)
            except (
                NotFoundError
            ):  # diagnostic-expected: record deleted with its conversation
                continue
            if (
                record.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
                or record.reported_at is not None
            ):
                continue
            try:
                self.store.update(
                    ChatSubagent,
                    record.id,
                    {"reported_at": utc_now()},
                    expected_revision=record.revision,
                )
            except ConflictError:  # diagnostic-expected: a concurrent settle or report won; a later turn re-checks it
                continue

    def harness_update(self, parent_session_id: str) -> ParentUpdate:
        """What a harness parent has not received, for its next prompt."""

        return self.pending_update(parent_session_id)

    @staticmethod
    def harness_report_context(update: ParentUpdate) -> str:
        """Reports and messages a harness has not seen, for its next turn."""

        if not update:
            return ""
        return (
            "\n\nNebula provider subagent reports and messages that arrived after "
            "your last turn:\n" + update_text(update)
        )

    async def harness_turn_settled(
        self, parent_session_id: str, parent_turn_id: str, *, stopped: bool
    ) -> None:
        """A harness chat turn ended: stop its children if it was stopped, and
        post every finished report now that the conversation is idle."""

        if stopped:
            await self.stop_for_parent_turn(parent_turn_id)
        await self.deliver_pending(parent_session_id)

    async def _steer_harness(self, parent_session_id: str) -> None:
        """Add unread messages and reports to a running harness turn."""

        if self.harness_steer is None or self._harness_waits.get(parent_session_id):
            return
        lock = self._steer_locks.setdefault(parent_session_id, asyncio.Lock())
        async with lock:
            update = self.pending_update(parent_session_id)
            if not update:
                return
            text = (
                "Nebula subagent update (your subagents sent this while you work):\n"
                + update_text(update)
            )
            try:
                taken = await self.harness_steer(parent_session_id, text)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.harness_steer_failed",
                    "A subagent update could not be added to the running harness turn.",
                    exc,
                    stage="subagent-deliver",
                )
                return
            if taken:
                self.mark_delivered(update)

    # -- lifecycle hooks ---------------------------------------------------

    async def turn_settled(self, turn_id: str) -> None:
        """React after any provider turn stops producing: child or parent side."""

        try:
            turn = self.store.get(ChatTurn, turn_id)
            session = self.store.get(ChatSession, turn.session_id)
        except NotFoundError:  # diagnostic-expected: turn or session deleted before settling; nothing to update
            return
        record = self._for_child_session(session)
        if record is not None and record.child_turn_id == turn.id:
            try:
                await self._child_settled(record, turn)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.settle_failed",
                    "Subagent state could not be updated after its response settled.",
                    exc,
                    stage="subagent-settle",
                )
                await self._fail_record(
                    record.id,
                    "Nebula could not record this subagent's result "
                    f"({type(exc).__name__}): {exc}",
                    usage=turn.usage,
                )
            return
        if turn.status == ChatTurnStatus.WAITING_CALLBACK:
            await self._resume_waiting_turn(turn)
        elif turn.status in {
            ChatTurnStatus.COMPLETE,
            ChatTurnStatus.FAILED,
            ChatTurnStatus.CANCELLED,
        }:
            await self.deliver_pending(session.id)

    async def _fail_record(
        self, subagent_id: str, error: str, *, usage: ChatTokenUsage | None = None
    ) -> None:
        """Fail a subagent Core lost track of, and tell its parent why."""

        for _ in range(3):
            try:
                record = self.get(subagent_id)
            except (
                NotFoundError
            ):  # diagnostic-expected: record deleted with its conversation
                return
            if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
                return
            try:
                record = self.store.update(
                    ChatSubagent,
                    record.id,
                    {
                        "status": ChatSubagentStatus.FAILED,
                        "finished_at": utc_now(),
                        "error": _bounded(error, 1_000),
                        **(
                            {"usage": _add_usage(record.usage, usage)}
                            if usage is not None
                            else {}
                        ),
                    },
                    expected_revision=record.revision,
                )
            except (
                ConflictError
            ):  # diagnostic-expected: reread a concurrent update and retry
                continue
            self._close_child_messages(record, "The subagent failed before reading it.")
            self._notify()
            await self._deliver(record)
            return

    def _close_child_messages(self, record: ChatSubagent, note: str) -> None:
        """A round ended: parent messages it never read and its open question
        are closed with the reason, which the report then carries."""

        self._mark_messages(
            self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_CHILD,
                ChatSubagentMessageStatus.PENDING,
            ),
            ChatSubagentMessageStatus.UNDELIVERED,
            note,
        )
        question = self.open_question(record.id)
        if question is not None:
            self._close_question(question, "The subagent ended before a reply.")

    async def _child_settled(self, record: ChatSubagent, turn: ChatTurn) -> None:
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            return
        if self.chat.shutting_down and safely_stopped_by_core(turn):
            # The next Core will reclaim this safe turn. Reporting an
            # interruption now would let the parent move on without its child.
            return
        if turn.status == ChatTurnStatus.WAITING_CALLBACK:
            await self._child_paused(record, turn)
            return
        if turn.status == ChatTurnStatus.WAITING_APPROVAL:
            await self._child_waiting_approval(record, turn)
            return
        status = _TERMINAL_TURN_STATUS.get(turn.status)
        if status is None:
            return
        result = ""
        if status == ChatSubagentStatus.COMPLETED and turn.final_message_id:
            try:
                result = self.store.get(ChatMessage, turn.final_message_id).content
            except NotFoundError:  # diagnostic-expected: final message deleted; the record keeps an empty report
                result = ""
        error = (
            None
            if status == ChatSubagentStatus.COMPLETED
            else (turn.error or status.value)
        )
        if self.chat.shutting_down and status in {
            ChatSubagentStatus.STOPPED,
            ChatSubagentStatus.INTERRUPTED,
        }:
            status = ChatSubagentStatus.INTERRUPTED
            error = "Core shut down while this subagent was running."
        usage = _add_usage(record.usage, turn.usage)
        unread_note = f"The subagent {status.value} before reading it."
        if (
            status == ChatSubagentStatus.COMPLETED
            and not self.chat.shutting_down
            and self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_CHILD,
                ChatSubagentMessageStatus.PENDING,
            )
        ):
            # The parent wrote while this round was finishing. Its report goes
            # to the parent as a message and the next round takes the unread
            # messages, so neither side loses anything.
            finished_round = record.rounds
            # Debit the completed turn before advancing the record to its next
            # round, so restart cannot lose the old child identity.
            self._charge_parent_goal(record, turn)
            try:
                record = await self._start_round(
                    record, parent_turn_id=None, usage=usage
                )
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.round_start_failed",
                    "Unread parent messages could not start another subagent round.",
                    exc,
                    stage="subagent-message",
                )
                unread_note = _bounded(
                    "Another round could not start for it "
                    f"({type(exc).__name__}): {exc}",
                    1_000,
                )
            else:
                self._add_message(
                    record,
                    ChatSubagentMessageDirection.TO_PARENT,
                    f"Report for round {finished_round} (it is now working on your "
                    f"newer message in round {record.rounds}):\n\n"
                    + (_bounded(result) or "No report was produced."),
                )
                await self._deliver(record)
                return
        self._close_child_messages(record, unread_note)
        parent_turn = self._parent_turn(record)
        pending_charge = (
            turn.id
            if parent_turn is not None
            and parent_turn.goal_id
            and turn.usage.total_tokens
            else None
        )
        try:
            record = self.store.update(
                ChatSubagent,
                record.id,
                {
                    "status": status,
                    "finished_at": utc_now(),
                    "usage": usage,
                    "result": _bounded(result),
                    "error": _bounded(error, 1_000) if error else None,
                    "pending_goal_charge_turn_id": pending_charge,
                },
                expected_revision=record.revision,
            )
        except ConflictError:  # diagnostic-expected: another settle path already recorded this terminal state
            return
        self._notify()
        self._charge_parent_goal(record, turn)
        await self._deliver(record)

    async def _child_waiting_approval(
        self, record: ChatSubagent, turn: ChatTurn
    ) -> None:
        """Give a blocked child back to its supervisor, never the operator."""

        pending_step = turn.tool_history[-1] if turn.tool_history else {}
        tool = str(pending_step.get("name") or "a tool call")
        detail = _step_detail(pending_step.get("arguments"))
        request = tool + (f" ({detail})" if detail else "")
        key = f"nebula:subagent-supervisor-approval:{turn.id}:{turn.approval_id or ''}"
        if self._existing_message(record, key) is None:
            self._add_message(
                record,
                ChatSubagentMessageDirection.TO_PARENT,
                f"{record.name} is blocked on approval for {request}. "
                + SUPERVISOR_APPROVAL_INSTRUCTIONS,
                idempotency_key=key,
            )

        try:
            supervisor = self.chat.pending_turn(record.parent_session_id)
        except NotFoundError:  # diagnostic-expected: the parent conversation was deleted with its supervisor
            return
        if supervisor is not None:
            await self._deliver(record)
            return

        # No model turn remains to own the decision. The child provider task is
        # settling on this stack, so cancel its durable turn directly instead
        # of asking stop_provider_turn to cancel and await the current task.
        cancelled = self.chat.cancel_turn(turn.id)
        await self._child_settled(self.get(record.id), cancelled)

    def _charge_parent_goal(self, record: ChatSubagent, turn: ChatTurn) -> None:
        parent_turn = self._parent_turn(record)
        if (
            parent_turn is None
            or not parent_turn.goal_id
            or not turn.usage.total_tokens
        ):
            return
        charge_id = str(
            uuid5(
                NAMESPACE_URL,
                f"nebula:chat-goal-charge:{parent_turn.goal_id}:{turn.id}",
            )
        )
        try:
            self.store.get(ChatGoalUsageCharge, charge_id)
            latest_record = self.get(record.id)
            if latest_record.pending_goal_charge_turn_id == turn.id:
                self.store.update(
                    ChatSubagent,
                    latest_record.id,
                    {"pending_goal_charge_turn_id": None},
                    expected_revision=latest_record.revision,
                )
            return
        except NotFoundError:  # diagnostic-expected: fall back to charging the goal
            pass
        for _ in range(3):
            try:
                goal = self.store.get(ChatGoal, parent_turn.goal_id)
            except NotFoundError:  # diagnostic-expected: the parent goal was removed
                return
            combined = _add_usage(goal.usage, turn.usage)
            changes: dict[str, Any] = {"usage": combined}
            if (
                goal.status == ChatGoalStatus.RUNNING
                and goal.token_budget is not None
                and combined.total_tokens >= goal.token_budget
            ):
                paused_at = utc_now()
                changes.update(
                    {
                        "status": ChatGoalStatus.PAUSED,
                        "paused_at": paused_at,
                        "active_since": None,
                        "elapsed_seconds": goal.active_elapsed_seconds(paused_at),
                        "blocked_reason": "Token budget exhausted by subagent work.",
                    }
                )
            charge = ChatGoalUsageCharge(
                id=charge_id,
                engagement_id=record.engagement_id,
                goal_id=goal.id,
                subagent_id=record.id,
                child_turn_id=turn.id,
                usage=turn.usage,
            )
            try:
                latest_record = self.get(record.id)
                with self.store.transaction() as transaction:
                    transaction.add(charge)
                    transaction.update(
                        ChatGoal,
                        goal.id,
                        changes,
                        expected_revision=goal.revision,
                    )
                    if latest_record.pending_goal_charge_turn_id == turn.id:
                        transaction.update(
                            ChatSubagent,
                            latest_record.id,
                            {"pending_goal_charge_turn_id": None},
                            expected_revision=latest_record.revision,
                        )
                return
            except ConflictError:  # diagnostic-expected: verify concurrent charge
                try:
                    self.store.get(ChatGoalUsageCharge, charge_id)
                    return
                except (
                    NotFoundError
                ):  # diagnostic-expected: retry missing concurrent charge
                    continue
        raise ConflictError(
            "subagent usage could not be charged after concurrent updates"
        )

    async def _child_paused(self, record: ChatSubagent, turn: ChatTurn) -> None:
        """A subagent paused on its question: resume it if the answer is
        already there, otherwise hand the question to its parent."""

        wait = self.pending_wait(turn)
        if wait is None or not isinstance(wait.get("reply_to"), str):
            return
        if self.wait_ready(wait):
            await self._resume_waiting_turn(turn)
            return
        await self._deliver(record)

    def _parent_turn(self, record: ChatSubagent) -> ChatTurn | None:
        try:
            return self.store.get(ChatTurn, record.parent_turn_id)
        except NotFoundError:  # diagnostic-expected: parent turn deleted; delivery falls back to the session
            return None

    async def _deliver(self, record: ChatSubagent) -> None:
        """Hand the parent what this subagent has for it.

        A parent waiting on its subagents resumes; a running harness is
        steered; an idle conversation gets it posted. A working provider parent
        receives it before its next step.
        """

        self._notify()
        try:
            pending = self.chat.pending_turn(record.parent_session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: parent conversation deleted; nobody is left to tell
            return
        if pending is None:
            await self.deliver_pending(record.parent_session_id)
        elif pending.status == ChatTurnStatus.WAITING_CALLBACK:
            await self._resume_waiting_turn(pending)
        elif record.parent_backend == ChatBackend.HARNESS:
            await self._steer_harness(record.parent_session_id)

    async def _resume_waiting_turn(self, turn: ChatTurn) -> None:
        """Resume a parent or subagent turn whose subagent wait is satisfied."""

        try:
            latest = self.store.get(ChatTurn, turn.id)
        except (
            NotFoundError
        ):  # diagnostic-expected: the turn was deleted with its conversation
            return
        wait = self.pending_wait(latest)
        if wait is None or not self.wait_ready(wait):
            return
        if self.chat.has_active_provider_turn(latest.id):
            return
        try:
            self.chat.start_provider_turn(self.chat.prepare_resume(latest.id))
        except Exception as exc:
            record_caught_exception(
                "chat",
                "chat.subagent.parent_resume_failed",
                "A paused response could not resume after its subagent wait ended.",
                exc,
                stage="subagent-deliver",
            )
            await self._fail_unresumable(latest, exc)

    async def _fail_unresumable(self, turn: ChatTurn, exc: Exception) -> None:
        """Fail a waiting turn visibly instead of leaving it parked forever.

        Nothing retries the resume once the wait is satisfied, and the
        operator's own Resume repeats the same failure. A failed parent frees
        the conversation for a new message and lets the reports post now; a
        failed subagent reports the failure to its parent.
        """

        try:
            latest = self.store.get(ChatTurn, turn.id)
            session = self.store.get(ChatSession, latest.session_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: the turn was deleted with its conversation
            return
        if (
            latest.status != ChatTurnStatus.WAITING_CALLBACK
            or latest.execution_claim_id is not None
        ):
            return
        record = self._for_child_session(session)
        child = record is not None and record.child_turn_id == latest.id
        reason = (
            "The parent's reply arrived but this subagent could not resume"
            if child
            else "Subagent reports are ready but the response could not resume"
        )
        try:
            latest = self.store.update(
                ChatTurn,
                latest.id,
                {
                    "status": ChatTurnStatus.FAILED,
                    "error": _bounded(f"{reason} ({type(exc).__name__}): {exc}", 1_000),
                },
                expected_revision=latest.revision,
            )
        except (
            ConflictError
        ):  # diagnostic-expected: another worker took the turn over; its state stands
            return
        # Before the held reports post, so the note follows the message the
        # failed turn was answering.
        self.chat.record_turn_outcome(latest.id)
        if child and record is not None:
            await self._child_settled(self.get(record.id), latest)
        else:
            await self.deliver_pending(latest.session_id)

    @staticmethod
    def pending_wait(turn: ChatTurn) -> dict[str, Any] | None:
        if turn.status != ChatTurnStatus.WAITING_CALLBACK or not turn.tool_history:
            return None
        wait = turn.tool_history[-1].get("subagent_wait")
        return wait if isinstance(wait, dict) else None

    async def deliver_pending(self, parent_session_id: str) -> None:
        """The parent conversation is idle: close questions it cannot answer,
        post what it has not received, and let a running goal pick it up."""

        try:
            if self.chat.pending_turn(parent_session_id) is not None:
                return
        except (
            NotFoundError
        ):  # diagnostic-expected: parent conversation deleted; nothing to post into
            return
        await self._close_questions(parent_session_id)
        records = self.for_session(parent_session_id)
        posted: list[ChatSubagent] = []
        for record in records:
            if (
                record.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
                or record.result_message_id is not None
            ):
                continue
            try:
                self._post_result(record)
                posted.append(record)
            except Exception as exc:
                # Unposted, the report stays unreceived, so it reaches the
                # parent before its next step or in its next prompt instead.
                record_caught_exception(
                    "chat",
                    "chat.subagent.result_post_failed",
                    "A subagent report could not be added to the conversation.",
                    exc,
                    stage="subagent-deliver",
                )
        messages = [
            item
            for item in self.store.find_entities(
                ChatSubagentMessage,
                {
                    "parent_session_id": parent_session_id,
                    "direction": ChatSubagentMessageDirection.TO_PARENT.value,
                    "status": ChatSubagentMessageStatus.PENDING.value,
                },
            )
            if item.posted_message_id is None
        ]
        posted_messages: list[ChatSubagentMessage] = []
        for message in messages:
            try:
                self._post_message(message)
                posted_messages.append(message)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.message_post_failed",
                    "A subagent message could not be added to the conversation.",
                    exc,
                    stage="subagent-deliver",
                )
        if not posted and not posted_messages:
            return
        # A failure or a message may need the goal's attention now; plain
        # reports wait until every subagent has finished.
        urgent = bool(posted_messages) or any(
            item.status != ChatSubagentStatus.COMPLETED for item in posted
        )
        if urgent or not self.active(self.for_session(parent_session_id)):
            trigger = (
                posted[-1] if posted else self.get(posted_messages[-1].subagent_id)
            )
            await self._continue_goal(trigger)

    async def _close_questions(self, parent_session_id: str) -> None:
        """Release subagents waiting on a parent that can no longer answer."""

        for message in self.store.find_entities(
            ChatSubagentMessage,
            {
                "parent_session_id": parent_session_id,
                "direction": ChatSubagentMessageDirection.TO_PARENT.value,
            },
        ):
            if not message.awaiting_reply:
                continue
            self._close_question(message, PARENT_IDLE_NOTE)
            try:
                record = self.get(message.subagent_id)
            except (
                NotFoundError
            ):  # diagnostic-expected: record deleted with its conversation
                continue
            turn = self._child_turn(record)
            if turn is not None:
                await self._resume_waiting_turn(turn)

    def _append_to_parent(
        self,
        record: ChatSubagent,
        message_id: str,
        content: str,
        metadata: dict[str, Any],
        *,
        usage: ChatTokenUsage | None = None,
        updates: Callable[[Any], object],
    ) -> None:
        session = self.store.get(ChatSession, record.parent_session_id)
        messages = self.chat.session_messages(session.id)
        sequence = (
            max(
                [item.sequence for item in messages]
                + [int(session.metadata.get("last_sequence") or 0)]
            )
            + 1
        )
        message = ChatMessage(
            id=message_id,
            engagement_id=record.engagement_id,
            session_id=session.id,
            sequence=sequence,
            role=ChatRole.ASSISTANT,
            content=content,
            provider_profile_id=session.provider_profile_id,
            model=session.model,
            usage=usage or ChatTokenUsage(),
            metadata={
                "subagent_id": record.id,
                "subagent_name": record.name,
                "child_session_id": record.child_session_id,
                **metadata,
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
            updates(transaction)

    def _post_result(self, record: ChatSubagent) -> None:
        heading = {
            ChatSubagentStatus.COMPLETED: "Subagent finished",
            ChatSubagentStatus.FAILED: "Subagent failed",
            ChatSubagentStatus.STOPPED: "Subagent stopped",
            ChatSubagentStatus.INTERRUPTED: "Subagent interrupted",
        }.get(record.status, "Subagent update")
        finished = record.finished_at or utc_now()
        # Round one keeps the id posted before rounds existed.
        key = f"nebula:subagent-result:{record.id}" + (
            f":round:{record.rounds}" if record.rounds > 1 else ""
        )
        message_id = str(uuid5(NAMESPACE_URL, key))
        # A provider parent reads the post from its history; a harness gets
        # the report in its next prompt instead.
        reported = (
            {"reported_at": utc_now()}
            if record.parent_backend == ChatBackend.PROVIDER
            and record.reported_at is None
            else {}
        )
        self._append_to_parent(
            record,
            message_id,
            f"{heading}: {record.name}\n\n{self._report_text(record)}",
            {
                "kind": "subagent_result",
                "subagent_status": record.status.value,
                "subagent_round": record.rounds,
                "elapsed_seconds": max(
                    0.0, (finished - record.started_at).total_seconds()
                ),
            },
            usage=record.usage,
            updates=lambda transaction: transaction.update(
                ChatSubagent,
                record.id,
                {"result_message_id": message_id, **reported},
                expected_revision=record.revision,
            ),
        )

    def _report_text(self, record: ChatSubagent) -> str:
        """A finished subagent's report, with exactly what went wrong."""

        parts = [record.result or ("" if record.error else "No report was produced.")]
        if record.error:
            parts.append(f"Error: {record.error}")
        details = _failure_lines(self._model_view(record, include_result=True))
        if details:
            parts.append("\n".join(details))
        return "\n\n".join(part for part in parts if part)

    def _post_message(self, message: ChatSubagentMessage) -> None:
        record = self.get(message.subagent_id)
        latest = self.store.get(ChatSubagentMessage, message.id)
        if latest.posted_message_id is not None:
            return
        heading = (
            "Question from subagent"
            if latest.expects_reply
            else "Message from subagent"
        )
        note = _parent_note(latest)
        suffix = f"\n\n({note})" if note else ""
        posted_id = str(uuid5(NAMESPACE_URL, f"nebula:subagent-message:{latest.id}"))
        # A provider parent reads it from its history from now on; a harness
        # still receives it at the start of its next turn.
        delivered = (
            {
                "status": ChatSubagentMessageStatus.DELIVERED,
                "delivered_at": utc_now(),
            }
            if record.parent_backend == ChatBackend.PROVIDER
            else {}
        )
        self._append_to_parent(
            record,
            posted_id,
            f"{heading} {record.name}:\n\n{latest.content}{suffix}",
            {
                "kind": "subagent_message",
                "subagent_message_id": latest.id,
                "question": latest.expects_reply,
            },
            updates=lambda transaction: transaction.update(
                ChatSubagentMessage,
                latest.id,
                {"posted_message_id": posted_id, **delivered},
                expected_revision=latest.revision,
            ),
        )

    async def _continue_goal(self, record: ChatSubagent) -> None:
        """Let a running goal pick up what arrived after its reply."""

        from .chat import ChatCompletionRequest, ChatRequestMessage

        parent_turn = self._parent_turn(record)
        if parent_turn is None or not parent_turn.goal_id:
            return
        try:
            goal = self.store.get(ChatGoal, parent_turn.goal_id)
        except (
            NotFoundError
        ):  # diagnostic-expected: goal removed; there is nothing to continue
            return
        if goal.status != ChatGoalStatus.RUNNING or goal.execution_claim_id is not None:
            return
        flags = record.parent_request
        try:
            # The operator may have picked another model or effort while the
            # goal ran; the conversation holds that choice, the turn does not.
            session = self.store.get(ChatSession, record.parent_session_id)
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    provider_id=session.provider_profile_id
                    or parent_turn.provider_profile_id,
                    engagement_id=record.engagement_id,
                    session_id=record.parent_session_id,
                    goal_id=goal.id,
                    model=session.model or parent_turn.model,
                    reasoning_effort=_known_effort(
                        session.metadata.get("reasoning_effort")
                    ),
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
                    max_active_subagents=subagent_limit(
                        flags.get("max_active_subagents")
                    ),
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
            # Pausing says why on the goal; resuming it later delivers the
            # reports and messages that are still waiting.
            self.chat._pause_running_session_goal(
                record.parent_session_id,
                _bounded(
                    "Subagent reports arrived but the goal could not continue "
                    f"({type(exc).__name__}): {exc}",
                    1_000,
                ),
            )

    async def reconcile_after_restart(self, *, preserve_graceful: bool = False) -> None:
        """Reconcile child turns before deciding whether their rounds finished."""

        # Settlement, delivery, and goal charging are separate durable writes.
        # Re-run them for terminal children so a crash between those writes is
        # repaired without duplicating a message or usage debit.
        offset = 0
        while page := self.store.list_entities(
            ChatSubagent, offset=offset, limit=1_000
        ):
            offset += len(page)
            for record in page:
                if record.status not in CHAT_SUBAGENT_TERMINAL_STATUSES:
                    continue
                turn = self._child_turn(record)
                if turn is not None and turn.id == record.pending_goal_charge_turn_id:
                    self._charge_parent_goal(record, turn)
                await self._deliver(record)

        for record in self.store.find_entities(
            ChatSubagent, {"status": ChatSubagentStatus.RUNNING.value}
        ):
            turn = self._child_turn(record)
            if turn is not None and turn.status == ChatTurnStatus.WAITING_APPROVAL:
                continue
            if turn is not None and turn.status == ChatTurnStatus.WAITING_CALLBACK:
                # A question outlives the restart; its parent may not have.
                await self._child_paused(record, turn)
                continue
            if turn is not None and self.chat.has_active_provider_turn(turn.id):
                # The startup recovery pass already reclaimed this child.
                continue
            if preserve_graceful and turn is not None and safely_stopped_by_core(turn):
                # Keep the parent wait and the child record intact until the
                # post-startup recovery pass has had a chance to reclaim it.
                continue
            if (
                turn is not None
                and turn.status in _TERMINAL_TURN_STATUS
                and turn.status != ChatTurnStatus.INTERRUPTED
            ):
                await self._child_settled(record, turn)
                continue
            if (
                turn is not None
                and turn.status == ChatTurnStatus.INTERRUPTED
                and turn.request_snapshot.get("recovery", {}).get("required") is True
            ):
                # The child remains a live round. Its own interrupted-response
                # card owns recovery; reporting a terminal interruption to the
                # parent here would contradict that resumable turn.
                self._notify()
                continue
            self._close_child_messages(
                record, "Core restarted before the subagent read it."
            )
            self.store.update(
                ChatSubagent,
                record.id,
                {
                    "status": ChatSubagentStatus.INTERRUPTED,
                    "finished_at": utc_now(),
                    "usage": _add_usage(record.usage, turn.usage)
                    if turn is not None
                    else record.usage,
                    "error": "Core restarted while this subagent was running.",
                },
                expected_revision=record.revision,
            )
            self._notify()
            # A parent parked in wait_subagents survives the restart, so deliver
            # the interruption the same way a settled child is: resume a waiting
            # parent, or post the report once the parent is idle.
            await self._deliver(self.get(record.id))


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
        if name == "message_parent":
            message = await self.service.send_to_parent(
                invocation,
                str(arguments.get("message") or ""),
                wait_for_reply=arguments.get("wait_for_reply") is True,
            )
            if not message.expects_reply:
                return ToolExecutionResult(
                    output={
                        "message_id": message.id,
                        "note": "Sent. The delegating assistant receives it before "
                        "its next step, or when it next works.",
                    }
                )
            wait = {"reply_to": message.id}
            if self.service.wait_ready(wait):
                output, _ = self.service.wait_result(wait)
                return ToolExecutionResult(output=output)
            raise SubagentWaitPending(
                wait, "Waiting for the delegating assistant to reply."
            )
        if name == "read_parent_messages":
            return ToolExecutionResult(
                output=self.service.read_parent_messages(invocation)
            )
        if name == "start_subagent":
            record = await self.service.start(
                invocation,
                task=str(arguments.get("task") or ""),
                name=arguments.get("name")
                if isinstance(arguments.get("name"), str)
                else None,
                context=arguments.get("context")
                if isinstance(arguments.get("context"), str)
                else None,
                reasoning_effort=arguments.get("reasoning_effort")
                if isinstance(arguments.get("reasoning_effort"), str)
                else None,
            )
            if record.status == ChatSubagentStatus.FAILED:
                raise InvalidToolArguments(record.error or "subagent could not start")
            return ToolExecutionResult(
                output={
                    "subagent_id": record.id,
                    "name": record.name,
                    "reasoning_effort": record.reasoning_effort or "model default",
                    "status": "running",
                    "note": "Running in parallel. Call wait_subagents when you need its report.",
                }
            )
        if name == "list_subagents":
            return ToolExecutionResult(output=self.service.list_output(session_id))
        if name == "message_subagent":
            return ToolExecutionResult(
                output=await self.service.send_to_child(
                    session_id,
                    str(arguments.get("subagent_id") or ""),
                    str(arguments.get("message") or ""),
                    parent_turn_id=invocation.chat_turn_id,
                    idempotency_key=invocation.idempotency_key,
                )
            )
        if name == "stop_subagent":
            record = self.service._owned(
                session_id, str(arguments.get("subagent_id") or "")
            )
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
            count = len(resolved)
            raise SubagentWaitPending(
                {"ids": resolved, "mode": mode},
                f"Waiting for {count} subagent{'' if count == 1 else 's'} to report.",
            )
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
                "reasoning_effort": {
                    "type": ["string", "null"],
                    "enum": [*REASONING_EFFORTS, None],
                    "description": SUBAGENT_EFFORT_DESCRIPTION,
                },
            },
        ),
        _spec(
            "wait_subagents",
            "Pause until subagents finish and return their reports. Omit ids to wait "
            "for every running subagent. Returns early when one of them sends you a "
            "message or waits on a question.",
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
        _spec(
            "list_subagents",
            "List this conversation's subagents with their status, their messages "
            "to you and any report you have not received.",
            {},
        ),
        _spec(
            "message_subagent",
            "Send one of your subagents a message: new instructions, a correction "
            "or the answer to its question. A running subagent reads it before its "
            "next step; a finished one starts another round with it.",
            {
                "subagent_id": {"type": "string"},
                "message": {
                    "type": "string",
                    "description": "What it should know or do. It cannot see this conversation.",
                },
            },
        ),
        _spec(
            "stop_subagent",
            "Stop a running subagent.",
            {"subagent_id": {"type": "string"}},
        ),
    ]
    return {spec.name: spec for spec in specs}


def subagent_child_specs() -> dict[str, ToolSpec]:
    specs = [
        _spec(
            "message_parent",
            "Send a message to the assistant that delegated your task: a finding it "
            "must act on before your report, or a question only it can answer. With "
            "wait_for_reply your work pauses until it answers or can no longer.",
            {
                "message": {
                    "type": "string",
                    "description": "Self-contained: it sees only this message.",
                },
                "wait_for_reply": {
                    "type": ["boolean", "null"],
                    "description": "Pause until the delegating assistant replies. Default false.",
                },
            },
        ),
        _spec(
            "read_parent_messages",
            "Return messages from the assistant that delegated your task that you "
            "have not read. New ones are also delivered before your next step.",
            {},
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


def subagent_child_components(
    service: SubagentService,
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    """The tools every subagent turn gets to talk to its parent.

    No runtime digest: they are Nebula's own and add nothing a paused turn
    must match on resume.
    """

    return RuntimeToolComponents(
        broker=SubagentBroker(service),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=subagent_child_specs(),
        runtime_digest="",
    )


__all__ = [
    "HARNESS_WAIT_DEFAULT_SECONDS",
    "HARNESS_WAIT_MAX_SECONDS",
    "SUBAGENT_CHILD_INSTRUCTIONS",
    "SUBAGENT_CHILD_TOOL_NAMES",
    "SUBAGENT_EFFORT_DESCRIPTION",
    "SUBAGENT_LIMIT_CEILING",
    "SUBAGENT_ROUTING_INSTRUCTIONS",
    "SUBAGENT_TOOL_NAMES",
    "ParentUpdate",
    "SubagentBroker",
    "SubagentService",
    "SubagentWaitPending",
    "harness_subagent_instructions",
    "is_subagent_session",
    "subagent_child_components",
    "subagent_child_specs",
    "subagent_components",
    "subagent_limit",
    "subagent_routing_instructions",
    "subagent_specs",
    "update_text",
]
