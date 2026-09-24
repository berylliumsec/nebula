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

Every delivery fits the bound its receiver reads it under: a provider tool
result the model-delivery bound, a harness tool result or prompt its own. Only
news travels (a message not read, a report not received), what does not fit
waits for the next delivery, and a report or message too long for one result
reaches a working provider turn in numbered parts. Nothing counts as received
until it went out whole: a Core-added step once saved, a harness prompt once
the vendor accepted it.

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
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Sequence,
    cast,
)
from uuid import NAMESPACE_URL, uuid4, uuid5

from .diagnostics import create_diagnostic_task, record_caught_exception
from .environments import enabled_snapshot_ssh_ids
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
from .storage import ConflictError, NotFoundError, StoreTransaction
from .tool_results import MAX_EXCERPT_BYTES, model_result_bytes
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec

if TYPE_CHECKING:
    from .chat import ChatService
    from .storage import NebulaStore

# Subagents are unlimited unless the operator sets how many may run at once
# for a conversation. The ceiling only bounds that setting.
SUBAGENT_LIMIT_CEILING = 100
# A report's own bound (ChatSubagent.result). Delivery no longer needs a
# smaller one: a long report reaches the parent in parts.
RESULT_CHARACTERS = 20_000
# A report past RESULT_CHARACTERS keeps its opening and its end, where
# reports conclude, and says what was cut between them.
REPORT_TAIL_CHARACTERS = 3_000
MESSAGE_CHARACTERS = 20_000
RECENT_STEPS = 4
FINISHED_STEP_CACHE = 4_096
# Failed tool steps a report names; the count covers the rest.
REPORTED_TOOL_FAILURES = 8
# A harness waits inside one gateway call, which holds every other Nebula tool
# call of that session and must end well before the vendor's own tool timeout
# (Codex: 900 s). Unfinished children come back as still running. Harnesses
# whose timeout Nebula does not know wait for less (see harnesses.py).
HARNESS_WAIT_DEFAULT_SECONDS = 300
HARNESS_WAIT_MAX_SECONDS = 600
HARNESS_REPORT_CONTEXT_CHARACTERS = 40_000
# Every tool result a provider model receives must fit the model-delivery
# bound once serialized (tool_results.serialize_model_result); a larger one
# reaches the model only as a placeholder. Reports and messages are packed to
# fit, and one too long for a result of its own reaches a working turn in
# numbered parts, one Core-added step each. Nothing counts as received until
# it went out whole.
PROVIDER_RESULT_BYTES = MAX_EXCERPT_BYTES
# A harness receives subagent results as MCP tool output. Grok cuts a result
# above 20,000 bytes (its default MCP output cap), so results stay below it;
# one report or message larger than this on its own still goes whole.
HARNESS_RESULT_BYTES = 16_000
# Results Core adds before one provider routing step; the rest arrive before
# the next one.
CORE_DELIVERY_RESULTS = 8
# What a report carries besides its text (error, failed steps, unread
# messages), so its last part fits one result beside the subagent's status.
REPORT_DETAIL_BYTES = 4_096
# The contract a paused parent turn resumes against. Bump it only for a change
# a paused turn cannot continue with, such as a tool removed or renamed or an
# argument removed or retyped. Descriptions and new ToolSpec fields leave it
# alone, so a Core update resumes parents parked in wait_subagents.
SUBAGENT_TOOLS_CONTRACT = "subagents-v1"
# Before contract versions the digest hashed every ToolSpec field.
_LEGACY_SUBAGENT_DIGEST = re.compile(r"subagents-[0-9a-f]{16}")
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
GOAL_BUDGET_SUBAGENT_NOTE = "Token budget exhausted by subagent work."
GOAL_BUDGET_STOP_NOTE = (
    "The goal's token budget ran out, so this subagent was stopped. Raise the "
    "budget and resume the goal to continue."
)
RETRACTED_NOTE = (
    "The operator edited the message this subagent was working for, so this "
    "was not delivered."
)
PARENT_STOPPED_NOTE = (
    "The delegating response was stopped, so this subagent was stopped with it."
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


class SubagentRoundSuperseded(Exception):
    """A round was prepared for a subagent that changed meanwhile.

    It was stopped, or another round or settle got there first; the prepared
    turn is cancelled before it runs.
    """


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
    """Messages and reports a parent has not received yet.

    ``messages`` and ``records`` are what ``views`` carry in full; ``left``
    counts news that did not fit and still waits, and ``omitted`` subagents
    without news that were not listed.
    """

    views: list[dict[str, Any]] = field(default_factory=list)
    messages: list[ChatSubagentMessage] = field(default_factory=list)
    records: list[ChatSubagent] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    left: int = 0
    omitted: int = 0

    def __bool__(self) -> bool:
        return bool(self.messages or self.records)


@dataclass
class DeliveryItem:
    """One report or message a receiver has not had, as the view carrying it.

    ``make(text, part, last)`` builds that view for all of ``text`` or for one
    numbered part of it; only the last part carries the rest of the view.
    Items of one ``key`` keep their order; views with the same ``merge_key``
    share one entry of a result.
    """

    key: str
    text: str
    make: Callable[[str, str | None, bool], dict[str, Any]]
    source: Any = None
    merge_key: str | None = None

    def view(self) -> dict[str, Any]:
        return self.make(self.text, None, True)


@dataclass
class PackedResult:
    views: list[dict[str, Any]]
    # The items this result completes; a part before the last completes none.
    delivered: list[DeliveryItem]


@dataclass
class CoreDelivery:
    """Results Core adds to a working provider turn, one step each.

    ``commit`` records what they carry as received and runs only once every
    step is saved, so a crash in between delivers again instead of losing it.
    """

    tool_name: str
    results: list[tuple[dict[str, Any], str]]
    commit: Callable[[], None]


# Measured in place of a part's real number, so relabelling never grows it.
_PART_PLACEHOLDER = "999/999"

Render = Callable[[list[dict[str, Any]], bool], dict[str, Any]]
Fits = Callable[[dict[str, Any]], bool]


def provider_result_fits(output: dict[str, Any]) -> bool:
    return model_result_bytes(output) <= PROVIDER_RESULT_BYTES


def harness_result_fits(output: dict[str, Any]) -> bool:
    return model_result_bytes(output) <= HARNESS_RESULT_BYTES


def _merge_view(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = {**existing, **new}
    if "messages" in existing or "messages" in new:
        merged["messages"] = [*existing.get("messages", []), *new.get("messages", [])]
    return merged


def _with_view(
    keys: list[str | None],
    views: list[dict[str, Any]],
    item: DeliveryItem,
    view: dict[str, Any] | None = None,
) -> tuple[list[str | None], list[dict[str, Any]]]:
    view = item.view() if view is None else view
    if item.merge_key is not None and item.merge_key in keys:
        index = keys.index(item.merge_key)
        return keys, [
            *views[:index],
            _merge_view(views[index], view),
            *views[index + 1 :],
        ]
    return [*keys, item.merge_key], [*views, view]


def _parts(item: DeliveryItem, render: Render, fits: Fits) -> list[dict[str, Any]]:
    """``item`` in numbered parts that each fit a result on their own.

    Empty when not even an empty part fits, which bounded views rule out.
    """

    def fit(text: str, last: bool) -> bool:
        return fits(render([item.make(text, _PART_PLACEHOLDER, last)], True))

    if not fit("", True):
        return []
    chunks: list[str] = []
    rest = item.text
    while not fit(rest, True):
        # The longest prefix that fits as a part before the last.
        low, high, best = 1, len(rest), 0
        while low <= high:
            middle = (low + high) // 2
            if fit(rest[:middle], False):
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if best == 0:
            return []
        chunks.append(rest[:best])
        rest = rest[best:]
    chunks.append(rest)
    return [
        item.make(chunk, f"{index}/{len(chunks)}", index == len(chunks))
        for index, chunk in enumerate(chunks, 1)
    ]


def pack_delivery(
    items: Sequence[DeliveryItem],
    render: Render,
    fits: Fits,
    *,
    parts: bool = False,
    force_first: bool = False,
    max_results: int = 1,
) -> tuple[list[PackedResult], list[DeliveryItem]]:
    """Pack ``items``, in order, into results that each fit, and return the
    results with the items left for later.

    ``render(views, more)`` builds a result, where ``more`` says something was
    left out; sizes are checked as if it were, so the final result fits too.

    Without ``parts`` one result is built. An item that does not fit waits,
    with every later item of its key, so one sender's updates keep their
    order; ``force_first`` admits the first item anyway, for a receiver with
    no other way to get it. With ``parts`` up to ``max_results`` results are
    built in strict order, and an item too long for a result of its own goes
    in numbered parts, one result each; packing never stops inside one.
    """

    if not parts:
        keys: list[str | None] = []
        views: list[dict[str, Any]] = []
        delivered: list[DeliveryItem] = []
        left: list[DeliveryItem] = []
        blocked: set[str] = set()
        for item in items:
            if item.key in blocked:
                left.append(item)
                continue
            candidate_keys, candidate = _with_view(keys, views, item)
            if fits(render(candidate, True)) or (force_first and not delivered):
                keys, views = candidate_keys, candidate
                delivered.append(item)
            else:
                left.append(item)
                blocked.add(item.key)
        return ([PackedResult(views, delivered)] if delivered else []), left
    results: list[PackedResult] = []
    queue = list(items)
    unsplittable: list[DeliveryItem] = []
    while queue and len(results) < max_results:
        keys, views, delivered = [], [], []
        while queue:
            candidate_keys, candidate = _with_view(keys, views, queue[0])
            if fits(render(candidate, True)):
                keys, views = candidate_keys, candidate
                delivered.append(queue.pop(0))
                continue
            if not delivered:
                item = queue.pop(0)
                split = _parts(item, render, fits)
                if not split:
                    unsplittable.append(item)
                    continue
                results.extend(PackedResult([view], []) for view in split[:-1])
                results.append(PackedResult([split[-1]], [item]))
            break
        if delivered:
            results.append(PackedResult(views, delivered))
    return results, [*unsplittable, *queue]


def _answer_retryable(turn: ChatTurn) -> bool:
    """Whether the operator can still retry this failed turn's final answer.

    The same condition ``prepare_resume`` accepts: the tools finished and
    only the answer is missing, so the turn is not over for good.
    """

    recovery = turn.request_snapshot.get("final_answer_recovery")
    attempts = recovery.get("attempts") if isinstance(recovery, dict) else None
    return (
        turn.status == ChatTurnStatus.FAILED
        and turn.final_message_id is None
        and isinstance(attempts, int)
        and not isinstance(attempts, bool)
        and attempts >= 2
    )


def _retracted(record: ChatSubagent) -> bool:
    """Whether the operator edited away the reply that delegated to it."""

    return isinstance(record.parent_request.get("_retracted"), dict)


def is_subagent_session(session: ChatSession) -> bool:
    return isinstance(session.metadata.get("subagent_id"), str)


def _bounded(text: str, limit: int = RESULT_CHARACTERS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _report_excerpt(text: str, limit: int = RESULT_CHARACTERS) -> str:
    """A child's final answer as its report, within ``limit``.

    A report cut silently reads as unfinished, and the parent asks the child
    to send it again; this keeps the opening and the conclusion and says how
    much is missing between them.
    """

    text = text.strip()
    if len(text) <= limit:
        return text
    tail = text[-REPORT_TAIL_CHARACTERS:].lstrip()

    def marker(omitted: int) -> str:
        return (
            f"\n\n[… {omitted} characters of this report were cut here to fit "
            "the report limit; the end of the report follows …]\n\n"
        )

    head = text[: limit - len(tail) - len(marker(len(text)))].rstrip()
    return head + marker(len(text) - len(head) - len(tail)) + tail


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


def restart_recovery_pending(turn: ChatTurn) -> bool:
    """Whether a child turn still owns a live Core-restart recovery.

    Unlike ``recoverable_after_core_restart``, this intentionally ignores
    unresolved effect receipts. Those receipts delay continuation; they do
    not make the child round terminal while Core reconciles them.
    """

    if turn.status != ChatTurnStatus.INTERRUPTED:
        return False
    recovery = turn.request_snapshot.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("required") is not True:
        return False
    return recovery.get("cause") in {"core_shutdown", "core_restart"} or (
        recovery.get("cause") is None
        and (turn.error or "").startswith(("Core stopped ", "Core restarted "))
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


def _subtract_usage(first: ChatTokenUsage, second: ChatTokenUsage) -> ChatTokenUsage:
    """Remove one prematurely settled turn debit without going below zero."""

    return ChatTokenUsage(
        input_tokens=max(0, first.input_tokens - second.input_tokens),
        output_tokens=max(0, first.output_tokens - second.output_tokens),
        total_tokens=max(0, first.total_tokens - second.total_tokens),
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
    if view.get("undelivered_messages_total"):
        lines.append(
            f"{view['undelivered_messages_total']} of your messages were not "
            "delivered in all."
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
    if view.get("messages_follow"):
        lines.append(
            f"{view['messages_follow']} more of its messages did not fit here; "
            "subagent.list returns them."
        )
    if view.get("report_follows"):
        lines.append("Its report did not fit here; subagent.list returns it.")
    return "\n  ".join(lines)


HARNESS_MORE_TEXT = (
    "More subagent updates did not fit here; subagent.list returns them."
)


def _updates_text(views: list[dict[str, Any]], more: bool) -> str:
    return "\n\n".join(
        [*(_view_text(view) for view in views), *([HARNESS_MORE_TEXT] if more else [])]
    )


def harness_text_fits(output: dict[str, Any]) -> bool:
    """Whether a harness prompt or steer update stays within its bound."""

    return (
        len(_updates_text(output["subagents"], bool(output.get("more"))))
        <= HARNESS_REPORT_CONTEXT_CHARACTERS
    )


def update_text(update: ParentUpdate) -> str:
    """A parent update as plain text for a harness.

    Packed to ``HARNESS_REPORT_CONTEXT_CHARACTERS`` already, so nothing it
    marks as received is cut here.
    """

    return _updates_text(update.views, bool(update.left or update.omitted))


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _has_message_part(view: dict[str, Any]) -> bool:
    return any("part" in message for message in view.get("messages", []))


def _parts_summary(result: PackedResult) -> str:
    """The summary of a Core step carrying one part of a long item."""

    for view in result.views:
        label = view.get("report_part")
        if isinstance(label, str):
            return f"Part {label} of a subagent report"
        for message in view.get("messages", [view]):
            label = message.get("part")
            if isinstance(label, str):
                return f"Part {label} of a message"
    return ""


def _update_of(items: Iterable[DeliveryItem]) -> ParentUpdate:
    update = ParentUpdate()
    for item in items:
        if isinstance(item.source, ChatSubagentMessage):
            update.messages.append(item.source)
        elif isinstance(item.source, ChatSubagent):
            update.records.append(item.source)
    return update


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
        # Step counts and last steps of finished rounds, which never change.
        self._finished_steps: OrderedDict[
            tuple[str, str, str], tuple[int, list[dict[str, Any]]]
        ] = OrderedDict()
        # Subagents being stopped, with the reason their report gives. A round
        # never starts for one, and its stopped round records the reason.
        self._stopping: dict[str, str | None] = {}
        # Child turn -> (subagent, parent goal) for charging usage as it
        # accrues. A settle rereads the parent turn, so a stale entry can only
        # delay a debit, never lose one.
        self._child_goals: dict[str, tuple[str, str | None]] = {}
        self._background: set[asyncio.Task[Any]] = set()
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

    def _steps(
        self, record: ChatSubagent, turn: ChatTurn | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        """The current round's step count and its last ``RECENT_STEPS`` steps.

        Read from the end of the ledger, never the whole history: the list
        polls this for every subagent the conversation ever had. A finished
        round's steps do not change, so they are kept once read.
        """

        if not record.child_turn_id:
            return 0, []
        # An interrupted round may still be recovering, and so still working.
        key = (
            (
                record.child_turn_id,
                record.status.value,
                record.finished_at.isoformat() if record.finished_at else "",
            )
            if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES
            and record.status != ChatSubagentStatus.INTERRUPTED
            else None
        )
        if key is not None and key in self._finished_steps:
            self._finished_steps.move_to_end(key)
            return self._finished_steps[key]
        tail = self.chat.turn_ledger.tail(record.child_turn_id, RECENT_STEPS)
        if tail is None:
            # No ledger rows: a turn from before the ledger keeps its steps
            # on itself, and a turn without tool steps has none.
            turn = turn or self._child_turn(record)
            history = self.chat._turn_history(turn) if turn is not None else []
            tail = (turn.next_step if turn is not None else 0, history[-RECENT_STEPS:])
        elif turn is not None:
            tail = (turn.next_step, tail[1])
        if key is not None:
            self._finished_steps[key] = tail
            while len(self._finished_steps) > FINISHED_STEP_CACHE:
                self._finished_steps.popitem(last=False)
        return tail

    def view(self, record: ChatSubagent) -> dict[str, Any]:
        """Return the operator-facing state, overlaying the live child turn.

        Only a running or interrupted round reads its child turn; a finished
        one is described by the record and the end of its ledger.
        """

        turn = (
            self._child_turn(record)
            if record.status
            in {ChatSubagentStatus.RUNNING, ChatSubagentStatus.INTERRUPTED}
            else None
        )
        step_count, steps = self._steps(record, turn)
        recent = [
            {
                "tool": str(entry.get("name") or ""),
                "detail": _step_detail(entry.get("arguments")),
                "status": str(entry.get("status") or ""),
            }
            for entry in steps
        ]
        recovering = turn is not None and restart_recovery_pending(turn)
        state = "recovering" if recovering else record.status.value
        approval: dict[str, Any] | None = None
        question: dict[str, Any] | None = None
        if record.status == ChatSubagentStatus.RUNNING and turn is not None:
            if turn.status == ChatTurnStatus.WAITING_APPROVAL and turn.approval_id:
                state = "waiting_approval"
                try:
                    pending = self.store.get(Approval, turn.approval_id)
                except NotFoundError:  # diagnostic-expected: approval deleted; the view reports it as pending
                    pending = None
                pending_entry = steps[-1] if steps else {}
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
            "step_count": step_count,
            "recent_steps": recent,
            "approval": approval,
            "question": question,
            "usage": usage.model_dump(mode="json"),
            "started_at": record.started_at.isoformat(),
            "finished_at": record.finished_at.isoformat()
            if record.finished_at and not recovering
            else None,
            "elapsed_seconds": max(0.0, (finished - record.started_at).total_seconds()),
            "result": "" if recovering else record.result,
            "error": (
                turn.error
                if state == "recovering" and turn is not None
                else record.error
            ),
            "result_message_id": record.result_message_id,
        }

    def _header(self, record: ChatSubagent) -> dict[str, Any]:
        """What the parent model sees of one subagent besides its news."""

        payload: dict[str, Any] = {
            "subagent_id": record.id,
            "name": record.name,
            "status": record.status.value,
        }
        if record.rounds > 1:
            payload["round"] = record.rounds
        if record.status != ChatSubagentStatus.RUNNING:
            payload["steps"] = self._steps(record)[0]
            return payload
        turn = self._child_turn(record)
        steps, recent = self._steps(record, turn)
        payload["steps"] = steps
        question = self.open_question(record.id)
        if question is not None:
            payload["waiting_for"] = "your_reply"
            payload["question_id"] = question.id
        elif (
            turn is not None
            and turn.status == ChatTurnStatus.WAITING_APPROVAL
            and recent
        ):
            payload["waiting_for"] = "operator_approval"
            payload["approval"] = {
                key: value
                for key, value in _step_view(recent[-1]).items()
                if key in {"tool", "detail"}
            }
        if recent:
            payload["last_step"] = _step_view(recent[-1])
        return payload

    def _report_details(self, record: ChatSubagent) -> dict[str, Any]:
        """What a finished round's report carries besides its text.

        Failed steps and unread messages are kept newest first while they fit
        ``REPORT_DETAIL_BYTES``; the totals count the rest.
        """

        turn = self._child_turn(record)
        history = list(self.chat._turn_history(turn)) if turn is not None else []
        details: dict[str, Any] = {"error": record.error}
        if history and record.status != ChatSubagentStatus.COMPLETED:
            details["last_step"] = _step_view(history[-1])
        failures = [
            {**_step_view(entry), "error": _step_error(entry)}
            for entry in history
            if entry.get("status") in _FAILED_STEP_STATUSES
        ]
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
        for key, found, limit in (
            ("tool_failures", failures, REPORTED_TOOL_FAILURES),
            ("undelivered_messages", undelivered, len(undelivered)),
        ):
            kept: list[dict[str, Any]] = []
            for entry in reversed(found[-limit:] if limit else []):
                if (
                    model_result_bytes({**details, key: [entry, *kept]})
                    > REPORT_DETAIL_BYTES
                ):
                    break
                kept.insert(0, entry)
            if kept:
                details[key] = kept
            if len(kept) < len(found):
                details[f"{key}_total"] = len(found)
        return details

    def _model_view(
        self, record: ChatSubagent, *, include_result: bool
    ) -> dict[str, Any]:
        """What the parent model sees of one subagent, with its whole report."""

        payload = self._header(record)
        if include_result and record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            payload["report"] = record.result or None
            payload.update(self._report_details(record))
        return payload

    @staticmethod
    def _message_item(
        header: dict[str, Any], message: ChatSubagentMessage
    ) -> DeliveryItem:
        base = _message_view(message)

        def make(text: str, part: str | None, last: bool) -> dict[str, Any]:
            del last
            view = {**base, "content": text, **({"part": part} if part else {})}
            return {**header, "messages": [view]}

        return DeliveryItem(
            key=header["subagent_id"],
            merge_key=header["subagent_id"],
            text=message.content,
            make=make,
            source=message,
        )

    def _report_item(
        self, header: dict[str, Any], record: ChatSubagent
    ) -> DeliveryItem:
        details = self._report_details(record)

        def make(text: str, part: str | None, last: bool) -> dict[str, Any]:
            view = {**header, "report": text if part else (text or None)}
            if part:
                view["report_part"] = part
            if last:
                view.update(details)
            return view

        return DeliveryItem(
            key=header["subagent_id"],
            merge_key=header["subagent_id"],
            text=record.result or "",
            make=make,
            source=record,
        )

    def _news(
        self, records: Iterable[ChatSubagent], *, everyone: bool
    ) -> list[tuple[ChatSubagent, dict[str, Any], list[DeliveryItem]]]:
        """Each subagent with what the parent has not received: messages it
        has not read and a finished round's report, in that order.

        ``everyone`` keeps subagents without news too. Those with news come
        first, then running ones, then finished ones.
        """

        entries: list[tuple[ChatSubagent, dict[str, Any], list[DeliveryItem]]] = []
        for record in records:
            if _retracted(record):
                # Its delegating reply was edited away; the conversation that
                # replaced it never hears from it unless it messages it again.
                continue
            pending = self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_PARENT,
                ChatSubagentMessageStatus.PENDING,
            )
            unreported = (
                record.status in CHAT_SUBAGENT_TERMINAL_STATUSES
                and not self._reported(record)
            )
            if not (everyone or pending or unreported):
                continue
            header = self._header(record)
            items = [self._message_item(header, message) for message in pending]
            if unreported:
                items.append(self._report_item(header, record))
            entries.append((record, header, items))
        return sorted(
            entries,
            key=lambda entry: (
                0
                if entry[2]
                else 1
                if entry[0].status == ChatSubagentStatus.RUNNING
                else 2
            ),
        )

    def _parent_update(
        self,
        records: Iterable[ChatSubagent],
        *,
        everyone: bool,
        render: Render,
        fits: Fits,
        force_first: bool = False,
    ) -> ParentUpdate:
        """What ``records`` have for the parent, packed into one result.

        Only news travels in full; what does not fit is flagged on its
        subagent (``report_follows``, ``messages_follow``) and waits. With
        ``everyone`` the subagents without news follow as far as there is
        room; a finished one says its report was received earlier.
        """

        entries = self._news(records, everyone=everyone)
        results, _ = pack_delivery(
            [item for _, _, items in entries for item in items],
            render,
            fits,
            force_first=force_first,
        )
        delivered = list(results[0].delivered) if results else []

        def news_views(done: set[int]) -> list[dict[str, Any]]:
            views: list[dict[str, Any]] = []
            for record, header, items in entries:
                if not items:
                    continue
                view = dict(header)
                for item in items:
                    if id(item) in done:
                        view = _merge_view(view, item.view())
                waiting = [item for item in items if id(item) not in done]
                messages = sum(1 for item in waiting if item.source is not record)
                if messages:
                    view["messages_follow"] = messages
                if any(item.source is record for item in waiting):
                    view["report_follows"] = True
                if len(waiting) < len(items) or everyone:
                    views.append(view)
            return views

        # The flags cost a few bytes the packing did not see; give back the
        # latest item until they fit as well.
        views = news_views({id(item) for item in delivered})
        while (
            delivered
            and not fits(render(views, True))
            and not (force_first and len(delivered) == 1)
        ):
            delivered.pop()
            views = news_views({id(item) for item in delivered})
        omitted = 0
        for record, header, items in entries:
            if items:
                continue
            view = dict(header)
            if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
                view["report_received_earlier"] = True
            if fits(render([*views, view], True)):
                views.append(view)
            else:
                omitted += 1
        left = sum(len(items) for _, _, items in entries) - len(delivered)
        return ParentUpdate(
            views=views,
            messages=[
                item.source
                for item in delivered
                if isinstance(item.source, ChatSubagentMessage)
            ],
            records=[
                item.source
                for item in delivered
                if isinstance(item.source, ChatSubagent)
            ],
            output=render(views, bool(left or omitted)),
            left=left,
            omitted=omitted,
        )

    @staticmethod
    def _more_note(harness: bool) -> str:
        return (
            "Some updates did not fit in this result (report_follows, "
            "messages_follow, or subagents not listed); "
            + (
                "call subagent.list to receive them."
                if harness
                else "Core delivers them before your next step."
            )
        )

    def pending_update(self, parent_session_id: str) -> ParentUpdate:
        """News a harness parent has not received, packed for its prompt or a
        steer (``update_text``)."""

        return self._parent_update(
            self.for_session(parent_session_id),
            everyone=False,
            render=lambda views, more: {"subagents": views, "more": more},
            fits=harness_text_fits,
            force_first=True,
        )

    def with_harness_updates(
        self, parent_session_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """``result`` of a harness subagent call, carrying the news the harness
        has not received as far as the result has room; those are marked."""

        def render(views: list[dict[str, Any]], more: bool) -> dict[str, Any]:
            return {
                **result,
                **({"updates": views} if views else {}),
                **({"more_updates": self._more_note(True)} if more else {}),
            }

        update = self._parent_update(
            self.for_session(parent_session_id),
            everyone=False,
            render=render,
            fits=harness_result_fits,
        )
        self.mark_delivered(update)
        return update.output

    def mark_delivered(self, update: ParentUpdate) -> None:
        self._mark_messages(update.messages, ChatSubagentMessageStatus.DELIVERED)
        self.mark_reported(item.id for item in update.records)

    def mark_ids_delivered(
        self, *, report_ids: Iterable[str], message_ids: Iterable[str]
    ) -> None:
        """Record that a harness received the reports and messages its prompt
        carried, once the vendor accepted that prompt."""

        messages: list[ChatSubagentMessage] = []
        for message_id in dict.fromkeys(message_ids):
            try:
                messages.append(self.store.get(ChatSubagentMessage, message_id))
            except (
                NotFoundError
            ):  # diagnostic-expected: message deleted with its conversation
                continue
        self._mark_messages(messages, ChatSubagentMessageStatus.DELIVERED)
        self.mark_reported(report_ids)

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
                    ssh_environment_ids=self._ssh_environment_ids(record, parent_turn),
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

    def _ssh_environment_ids(
        self, record: ChatSubagent, parent_turn: ChatTurn | None
    ) -> list[str]:
        """The SSH hosts a child may use: those its parent turn was given.

        Never None, which would mean every enabled host: a harness chat has no
        SSH selection and an unknown parent gives none, so a child can never
        reach a host the operator did not give the turn that delegated to it.
        """

        if record.parent_backend == ChatBackend.HARNESS or parent_turn is None:
            return []
        return enabled_snapshot_ssh_ids(
            self.store, parent_turn.request_snapshot.get("ssh_environment_snapshot")
        )

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
        unread messages. The record is running again when this returns.

        Preparing the round awaits, so the record is compared with the one
        the round was started from before the new turn runs: a stop, or a
        settle or round that got there first, cancels the prepared turn
        instead of leaving it running under a record that no longer owns it.
        """

        from .chat import ChatCompletionRequest, ChatRequestMessage

        if record.id in self._stopping:
            raise SubagentRoundSuperseded(f"{record.name} is being stopped")
        started_from = (record.status, record.child_turn_id)
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
        # The round runs for the parent turn that sent the message, or, when
        # Core starts it after a report, for the turn the child already has.
        driving_turn = self._parent_turn(record, parent_turn_id)
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
                ssh_environment_ids=self._ssh_environment_ids(record, driving_turn),
                allow_cloud_tool_results=True,
                reasoning_effort=_known_effort(record.reasoning_effort),
                stream=True,
            )
        )
        if prepared.turn is None:
            raise RuntimeError("subagent turn was not created")
        try:
            latest = self.get(record.id)
            unread = [
                item
                for item in self.messages_for(
                    record.id,
                    ChatSubagentMessageDirection.TO_CHILD,
                    ChatSubagentMessageStatus.PENDING,
                )
                if item.id in {message.id for message in pending}
            ]
            if (
                record.id in self._stopping
                or (latest.status, latest.child_turn_id) != started_from
                or len(unread) != len(pending)
            ):
                raise SubagentRoundSuperseded(
                    f"{record.name} changed while its next round was prepared"
                )
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
                    # A reply that messages a retracted subagent adopts it again.
                    **(
                        {
                            "parent_request": {
                                key: value
                                for key, value in latest.parent_request.items()
                                if key != "_retracted"
                            }
                        }
                        if parent_turn_id is not None and _retracted(latest)
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
            except SubagentRoundSuperseded as exc:  # diagnostic-expected: it was stopped or started another round first; the model is told
                reason = f"Another round did not start: {exc}."
                self._mark_messages(
                    [message], ChatSubagentMessageStatus.UNDELIVERED, reason
                )
                raise InvalidToolArguments(
                    f"{reason} The message was not delivered."
                ) from exc
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
            pending_step = (
                self.chat._turn_history(turn)[-1]
                if self.chat._turn_history(turn)
                else {}
            )
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

    _CHILD_INBOX_NOTE = (
        "From the assistant that delegated your task. Act on them; they "
        "take precedence over the original task where they differ."
    )

    def _child_inbox_items(self, record: ChatSubagent) -> list[DeliveryItem]:
        items: list[DeliveryItem] = []
        for message in self.messages_for(
            record.id,
            ChatSubagentMessageDirection.TO_CHILD,
            ChatSubagentMessageStatus.PENDING,
        ):

            def make(
                text: str,
                part: str | None,
                last: bool,
                message_id: str = message.id,
            ) -> dict[str, Any]:
                del last
                return {
                    "message_id": message_id,
                    "content": text,
                    **({"part": part} if part else {}),
                }

            # One key: the parent's messages keep their order.
            items.append(
                DeliveryItem(
                    key="parent", text=message.content, make=make, source=message
                )
            )
        return items

    def _child_render(self, views: list[dict[str, Any]], more: bool) -> dict[str, Any]:
        output: dict[str, Any] = {"messages": views, "note": self._CHILD_INBOX_NOTE}
        if more:
            output["more_messages"] = (
                "More messages did not fit in this result; Core delivers them "
                "before your next step."
            )
        return output

    def _child_inbox(self, record: ChatSubagent) -> dict[str, Any] | None:
        """Unread parent messages for a working subagent, as one result that
        fits; only the messages it carries are marked read."""

        items = self._child_inbox_items(record)
        if not items:
            return None
        results, left = pack_delivery(items, self._child_render, provider_result_fits)
        delivered = results[0].delivered if results else []
        self._mark_messages(
            [item.source for item in delivered], ChatSubagentMessageStatus.DELIVERED
        )
        return self._child_render(results[0].views if results else [], bool(left))

    def _core_results(
        self,
        items: list[DeliveryItem],
        render: Render,
        summary: Callable[[PackedResult], str],
    ) -> tuple[list[tuple[dict[str, Any], str]], list[DeliveryItem]]:
        """``items`` as results Core adds before a routing step, in parts
        where one is too long, and the items they carry in full."""

        results, left = pack_delivery(
            items,
            render,
            provider_result_fits,
            parts=True,
            max_results=CORE_DELIVERY_RESULTS,
        )
        return [
            (
                render(result.views, bool(left) and index == len(results)),
                summary(result),
            )
            for index, result in enumerate(results, 1)
        ], [item for result in results for item in result.delivered]

    def routing_delivery(
        self, turn: ChatTurn, tool_names: Collection[str]
    ) -> CoreDelivery | None:
        """What a working provider turn must receive before its next step.

        Returns the results of the steps Core adds for it:
        ``read_parent_messages`` for a subagent with unread parent messages,
        ``list_subagents`` for a parent with unread subagent messages or
        reports. Each result fits the model-delivery bound, and one message
        or report too long for a result arrives in numbered parts.
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
            items = self._child_inbox_items(record)
            if not items:
                return None
            child_results, delivered = self._core_results(
                items,
                self._child_render,
                lambda result: (
                    _parts_summary(result)
                    or _count(len(result.delivered), "message")
                    + " from the delegating assistant"
                ),
            )
            return CoreDelivery(
                "read_parent_messages",
                child_results,
                lambda: self._mark_messages(
                    [item.source for item in delivered],
                    ChatSubagentMessageStatus.DELIVERED,
                ),
            )
        if "list_subagents" not in tool_names or not self._has_news(session.id):
            return None
        entries = self._news(self.for_session(session.id), everyone=False)

        def render(views: list[dict[str, Any]], more: bool) -> dict[str, Any]:
            note = (
                "Core delivered this update because your subagents sent messages "
                "or finished since your last step."
            )
            if any("report_part" in view or _has_message_part(view) for view in views):
                note += (
                    " A report or message too long for one result arrives in "
                    "numbered parts (report_part, part) over consecutive results."
                )
            output: dict[str, Any] = {
                "delivered_by": "nebula",
                "note": note,
                "subagents": views,
            }
            waiting = [
                view["subagent_id"]
                for view in views
                if view.get("waiting_for") == "your_reply"
            ]
            if waiting:
                output["awaiting_your_reply"] = waiting
            if more:
                output["more_updates"] = (
                    "More updates are ready; Core delivers them before your next step."
                )
            return output

        parent_results, delivered = self._core_results(
            [item for _, _, items in entries for item in items],
            render,
            lambda result: (
                _parts_summary(result)
                or self._update_summary(_update_of(result.delivered))
            ),
        )
        if not parent_results:
            return None
        return CoreDelivery(
            "list_subagents",
            parent_results,
            lambda: self.mark_delivered(_update_of(delivered)),
        )

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

    async def stop(
        self, subagent_id: str, *, reason: str | None = None
    ) -> ChatSubagent:
        """Stop a running subagent; ``reason`` is the error its report gives.

        Nothing it has not read may start another round while it stops, and
        a round that started anyway, in the window before this call, is
        stopped too: when this returns no turn of the subagent is running.
        """

        record = self.get(subagent_id)
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            return record
        self._stopping[record.id] = _bounded(reason, 1_000) if reason else None
        try:
            self._mark_messages(
                self.messages_for(
                    record.id,
                    ChatSubagentMessageDirection.TO_CHILD,
                    ChatSubagentMessageStatus.PENDING,
                ),
                ChatSubagentMessageStatus.UNDELIVERED,
                "The subagent stopped before reading it.",
            )
            stopped_turns: set[str] = set()
            while True:
                record = self.get(subagent_id)
                turn_id = record.child_turn_id
                if (
                    record.status in CHAT_SUBAGENT_TERMINAL_STATUSES
                    or not turn_id
                    or turn_id in stopped_turns
                ):
                    break
                stopped_turns.add(turn_id)
                await self.chat.stop_provider_turn(turn_id)
                latest = self.get(record.id)
                if latest.child_turn_id == turn_id:
                    await self._child_settled(latest, self.store.get(ChatTurn, turn_id))
            record = self.get(record.id)
            if record.status not in CHAT_SUBAGENT_TERMINAL_STATUSES:
                record = self.store.update(
                    ChatSubagent,
                    record.id,
                    {
                        "status": ChatSubagentStatus.STOPPED,
                        "finished_at": utc_now(),
                        "error": self._stopping.get(record.id)
                        or "Stopped before it started.",
                    },
                    expected_revision=record.revision,
                )
                self._notify()
                await self._deliver(record)
        finally:
            self._stopping.pop(subagent_id, None)
        return record

    def retract(
        self, transaction: StoreTransaction, record: ChatSubagent, *, retraction_id: str
    ) -> None:
        """Detach a subagent whose delegating reply the operator edited away.

        Its reports and messages are never posted or delivered to the edited
        conversation: it counts as received, and the mark tells delivery to
        pass it by.
        """

        if _retracted(record):
            return
        transaction.update(
            ChatSubagent,
            record.id,
            {
                "parent_request": {
                    **record.parent_request,
                    "_retracted": {
                        "retraction_id": retraction_id,
                        "at": utc_now().isoformat(),
                    },
                },
                **({"reported_at": utc_now()} if record.reported_at is None else {}),
            },
            expected_revision=record.revision,
        )

    def close_retracted_messages(self, record: ChatSubagent) -> None:
        self._mark_messages(
            self.messages_for(
                record.id,
                ChatSubagentMessageDirection.TO_PARENT,
                ChatSubagentMessageStatus.PENDING,
            ),
            ChatSubagentMessageStatus.UNDELIVERED,
            RETRACTED_NOTE,
        )

    async def stop_retracted(self, parent_session_id: str) -> None:
        """Stop the running subagents of an exchange the operator edited away."""

        for record in self.active(self.for_session(parent_session_id)):
            if not _retracted(record):
                continue
            try:
                await self.stop(
                    record.id,
                    reason=(
                        "The operator edited the message this subagent was "
                        "working for, so it was stopped."
                    ),
                )
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.retracted_stop_failed",
                    "A subagent of an edited message could not be stopped.",
                    exc,
                    stage="subagent-stop",
                )

    async def stop_for_parent_turn(
        self, parent_turn_id: str, *, reason: str | None = None
    ) -> None:
        for record in self.store.find_entities(
            ChatSubagent,
            {
                "parent_turn_id": parent_turn_id,
                "status": ChatSubagentStatus.RUNNING.value,
            },
        ):
            try:
                await self.stop(record.id, reason=reason)
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
        # With none running, the finished ones whose reports the parent has
        # not received; a goal parent that never goes idle would otherwise
        # wait on every child it ever had.
        return self.active(records) or [
            item
            for item in records
            if item.status in CHAT_SUBAGENT_TERMINAL_STATUSES
            and not self._reported(item)
            and not _retracted(item)
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
                if not count:
                    return inbox, "The reply arrives before the next step"
                return inbox, f"{count} repl{'y' if count == 1 else 'ies'} received"
            return {
                "reply": None,
                "note": question.note or "No reply arrived.",
            }, "No reply"
        ids = [str(item) for item in wait.get("ids") or []]
        output = self.wait_output(ids)
        received = sum(
            1
            for view in output["subagents"]
            if "report" in view and "report_part" not in view
        )
        summary = f"{received} subagent report{'' if received == 1 else 's'} received"
        if output.get("awaiting_your_reply"):
            summary += "; a subagent is waiting for your reply"
        elif any(view.get("messages") for view in output["subagents"]):
            summary += "; subagent messages received"
        if output.get("more_updates"):
            summary += "; more follow"
        return output, summary

    def wait_output(
        self, ids: list[str], *, harness: bool = False, mark: bool = True
    ) -> dict[str, Any]:
        """The result of a satisfied or timed-out wait on ``ids``.

        It carries each report and message the parent has not received as far
        as the result has room; a report received earlier is not sent again.
        ``mark`` is off when nobody is left to read the result.
        """

        records = [self.get(item) for item in ids]
        still_running = [
            item.id
            for item in records
            if item.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
        ]

        def render(views: list[dict[str, Any]], more: bool) -> dict[str, Any]:
            output: dict[str, Any] = {
                "subagents": views,
                "still_running": still_running,
            }
            waiting = [
                view["subagent_id"]
                for view in views
                if view.get("waiting_for") == "your_reply"
            ]
            if waiting:
                output["awaiting_your_reply"] = waiting
                output["note"] = (
                    "These subagents are paused until you answer their question "
                    "with a message; waiting again returns at once while they are."
                )
            if more:
                output["more_updates"] = self._more_note(harness)
            return output

        update = self._parent_update(
            records,
            everyone=True,
            render=render,
            fits=harness_result_fits if harness else provider_result_fits,
            force_first=harness,
        )
        if mark:
            self.mark_delivered(update)
        return update.output

    def list_output(
        self, parent_session_id: str, *, harness: bool = False
    ) -> dict[str, Any]:
        def render(views: list[dict[str, Any]], more: bool) -> dict[str, Any]:
            return {
                "subagents": views,
                **({"more_updates": self._more_note(harness)} if more else {}),
            }

        update = self._parent_update(
            self.for_session(parent_session_id),
            everyone=True,
            render=render,
            fits=harness_result_fits if harness else provider_result_fits,
            force_first=harness,
        )
        self.mark_delivered(update)
        return update.output

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
        """Wait inside a harness gateway call, bounded, then report.

        When the harness turn ended during the wait nobody reads the result,
        so nothing in it counts as received; the next prompt carries it.
        """

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
            return self.wait_output(resolved, harness=True, mark=still_waiting())
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
        self,
        parent_session_id: str,
        parent_turn_id: str,
        *,
        stopped: bool,
        failed: bool = False,
    ) -> None:
        """A harness chat turn ended: stop its children if it was stopped or
        failed, and post every finished report now that the conversation is
        idle."""

        if stopped:
            await self.stop_for_parent_turn(parent_turn_id, reason=PARENT_STOPPED_NOTE)
        elif failed:
            await self.stop_for_parent_turn(
                parent_turn_id,
                reason="The delegating response failed, so this subagent was stopped.",
            )
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
            await self._stop_unsupervised_children(turn)
            await self.deliver_pending(session.id)

    async def _stop_unsupervised_children(self, turn: ChatTurn) -> None:
        """Stop what a parent response started when it ended without finishing.

        A completed parent keeps its children: their reports post to the
        conversation, and a running goal continues with them. A failed or
        stopped one leaves nobody to supervise them (its goal is paused), so
        they stop now and their reports say why. Parked and restart-
        interrupted parents are not ended and never come here; a failed
        answer the operator can still retry keeps its children like a
        completed one.
        """

        if turn.status == ChatTurnStatus.COMPLETE or _answer_retryable(turn):
            return
        if turn.status == ChatTurnStatus.CANCELLED:
            reason = PARENT_STOPPED_NOTE
        else:
            reason = _bounded(
                "The delegating response failed, so this subagent was stopped"
                + (f": {turn.error}" if turn.error else "."),
                1_000,
            )
        await self.stop_for_parent_turn(turn.id, reason=reason)

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

    async def _child_settled(
        self, record: ChatSubagent, turn: ChatTurn, *, deliver: bool = True
    ) -> None:
        """Record what one round's turn did; ``deliver`` hands it on at once.

        Only the record's current turn settles it: a turn a newer round
        replaced has nothing left to say about the record.
        """

        if record.child_turn_id != turn.id:
            return
        if restart_recovery_pending(turn):
            # The turn is the recovery authority. This must hold even when
            # unresolved effects prevent immediate continuation, and even for
            # a late callback from an overlapping Core worker.
            return
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            if (
                record.status != ChatSubagentStatus.INTERRUPTED
                or not self._restart_recovery_fenced(record, turn)
            ):
                return
            # A previous binary can finish unwinding after the new Core has
            # fenced this resumed child. Restore the composed record before
            # recording the new worker's actual terminal outcome.
            record = self._restore_restart_record(record, turn)
        if turn.status == ChatTurnStatus.WAITING_CALLBACK:
            await self._child_paused(record, turn)
            return
        if turn.status == ChatTurnStatus.WAITING_APPROVAL:
            await self._child_waiting_approval(record, turn, deliver=deliver)
            return
        status = _TERMINAL_TURN_STATUS.get(turn.status)
        if status is None:
            return
        # The turn produces no more usage; its settle below charges the rest.
        self._child_goals.pop(turn.id, None)
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
        if status == ChatSubagentStatus.STOPPED and self._stopping.get(record.id):
            error = self._stopping[record.id]
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
            except SubagentRoundSuperseded:  # diagnostic-expected: a stop or another settle owns the record; record this round's outcome
                pass
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
                    + (
                        _report_excerpt(result, MESSAGE_CHARACTERS - 200)
                        or "No report was produced."
                    ),
                )
                if deliver:
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
                    "result": _report_excerpt(result),
                    "error": _bounded(error, 1_000) if error else None,
                    "pending_goal_charge_turn_id": pending_charge,
                },
                expected_revision=record.revision,
            )
        except ConflictError:  # diagnostic-expected: another settle path already recorded this terminal state
            return
        self._notify()
        self._charge_parent_goal(record, turn)
        if deliver:
            await self._deliver(record)

    @staticmethod
    def _restart_recovery_marker(record: ChatSubagent) -> dict[str, Any] | None:
        marker = record.parent_request.get("_core_restart_recovery")
        return marker if isinstance(marker, dict) else None

    def _restart_recovery_fenced(self, record: ChatSubagent, turn: ChatTurn) -> bool:
        marker = self._restart_recovery_marker(record)
        return marker is not None and marker.get("child_turn_id") == turn.id

    def _restore_restart_record(
        self, record: ChatSubagent, turn: ChatTurn
    ) -> ChatSubagent:
        """Restore or fence the subagent side of one resumed child turn.

        ``parent_request`` is an existing cross-version-safe envelope: an old
        binary preserves this marker even if it writes after the new Core. A
        generation gives a recovered terminal report a fresh deterministic ID
        when an earlier false interruption was already posted.
        """

        latest = self.get(record.id)
        marker = self._restart_recovery_marker(latest) or {}
        generation = marker.get("generation", 0)
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = 0
        superseded = [
            item
            for item in marker.get("superseded_result_message_ids", [])
            if isinstance(item, str) and item
        ]
        changes: dict[str, Any] = {}
        if latest.status == ChatSubagentStatus.INTERRUPTED:
            if latest.result_message_id and latest.result_message_id not in superseded:
                superseded.append(latest.result_message_id)
            generation += 1
            changes.update(
                {
                    "status": ChatSubagentStatus.RUNNING,
                    "finished_at": None,
                    "usage": _subtract_usage(latest.usage, turn.usage),
                    "result": "",
                    "error": None,
                    "result_message_id": None,
                    "reported_at": None,
                    "pending_goal_charge_turn_id": None,
                }
            )
        elif latest.status != ChatSubagentStatus.RUNNING:
            raise ConflictError(
                f"restart recovery cannot reopen a {latest.status.value} subagent"
            )
        changes["parent_request"] = {
            **latest.parent_request,
            "_core_restart_recovery": {
                "child_turn_id": turn.id,
                "generation": generation,
                "fenced_at": utc_now().isoformat(),
                "superseded_result_message_ids": superseded,
            },
        }
        restored = self.store.update(
            ChatSubagent,
            latest.id,
            changes,
            expected_revision=latest.revision,
        )
        self._notify()
        return restored

    def fence_restart_resume(self, subagent_id: str, turn: ChatTurn) -> ChatSubagent:
        """Fence a resumed child against a late terminal write by an old Core."""

        return self._restore_restart_record(self.get(subagent_id), turn)

    async def _child_waiting_approval(
        self, record: ChatSubagent, turn: ChatTurn, *, deliver: bool = True
    ) -> None:
        """Give a blocked child back to its supervisor, never the operator.

        The same rule holds when it blocks and when its supervisor's response
        ends later (``deliver_pending``): with no model turn left to decide,
        the child is stopped with that reason instead of waiting forever.
        """

        pending_step = (
            self.chat._turn_history(turn)[-1] if self.chat._turn_history(turn) else {}
        )
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
            if deliver:
                await self._deliver(record)
            return

        # No model turn remains to own the decision. The child provider task is
        # settling on this stack (or has already parked), so cancel its durable
        # turn directly instead of asking stop_provider_turn to cancel and
        # await the current task.
        owned = record.id not in self._stopping
        if owned:
            self._stopping[record.id] = _bounded(
                f"It was blocked on approval for {request} and the delegating "
                "assistant was no longer working to decide, so it was stopped.",
                1_000,
            )
        try:
            cancelled = self.chat.cancel_turn(turn.id)
            await self._child_settled(self.get(record.id), cancelled, deliver=deliver)
        finally:
            if owned:
                self._stopping.pop(record.id, None)

    def _charge_parent_goal(self, record: ChatSubagent, turn: ChatTurn) -> None:
        """Settle a round's goal debit: whatever the accrual left uncharged."""

        parent_turn = self._parent_turn(record)
        if parent_turn is None or not parent_turn.goal_id:
            return
        self._true_up_goal_charge(parent_turn.goal_id, record.id, turn)

    def _child_goal(self, turn: ChatTurn) -> tuple[str, str | None] | None:
        """The subagent a child turn runs for and the goal its parent serves."""

        cached = self._child_goals.get(turn.id)
        if cached is not None:
            return cached
        if not turn.request_snapshot.get("subagent_child"):
            return None
        try:
            session = self.store.get(ChatSession, turn.session_id)
        except NotFoundError:  # diagnostic-expected: child conversation deleted mid-turn; nothing to charge
            return None
        record = self._for_child_session(session)
        if record is None or record.child_turn_id != turn.id:
            return None
        parent_turn = self._parent_turn(record)
        link = (record.id, parent_turn.goal_id if parent_turn is not None else None)
        self._child_goals[turn.id] = link
        return link

    def child_goal_id(self, turn: ChatTurn) -> str | None:
        """The goal whose budget a subagent turn spends, if its parent has one."""

        link = self._child_goal(turn)
        return link[1] if link is not None else None

    def charge_child_usage(self, turn: ChatTurn) -> None:
        """Debit a working child's usage to its parent's goal as it accrues.

        Called after each provider response of a child turn, so the goal's
        token budget bounds its subagents while they run instead of only when
        a round settles. The durable charge per (goal, child turn) holds what
        was debited so far; every accrual, settle and restart repair adds
        only the difference, so none of them charges a token twice.
        """

        link = self._child_goal(turn)
        if link is None or link[1] is None:
            return
        self._true_up_goal_charge(link[1], link[0], turn)

    def _true_up_goal_charge(
        self, goal_id: str, subagent_id: str, turn: ChatTurn
    ) -> None:
        charge_id = str(
            uuid5(NAMESPACE_URL, f"nebula:chat-goal-charge:{goal_id}:{turn.id}")
        )
        for _ in range(3):
            try:
                goal = self.store.get(ChatGoal, goal_id)
            except NotFoundError:  # diagnostic-expected: the parent goal was removed
                return
            try:
                charge: ChatGoalUsageCharge | None = self.store.get(
                    ChatGoalUsageCharge, charge_id
                )
            except NotFoundError:  # diagnostic-expected: this turn's first debit
                charge = None
            try:
                record: ChatSubagent | None = self.get(subagent_id)
            except NotFoundError:  # diagnostic-expected: record deleted with its conversation; the debit still stands
                record = None
            clear_pending = (
                record is not None and record.pending_goal_charge_turn_id == turn.id
            )
            delta = _subtract_usage(
                turn.usage, charge.usage if charge is not None else ChatTokenUsage()
            )
            if delta == ChatTokenUsage():
                if clear_pending and record is not None:
                    try:
                        self.store.update(
                            ChatSubagent,
                            record.id,
                            {"pending_goal_charge_turn_id": None},
                            expected_revision=record.revision,
                        )
                    except (
                        ConflictError
                    ):  # diagnostic-expected: reread a concurrent settle and retry
                        continue
                return
            combined = _add_usage(goal.usage, delta)
            changes: dict[str, Any] = {"usage": combined}
            exhausted = (
                goal.status == ChatGoalStatus.RUNNING
                and goal.token_budget is not None
                and combined.total_tokens >= goal.token_budget
            )
            if exhausted:
                paused_at = utc_now()
                changes.update(
                    {
                        "status": ChatGoalStatus.PAUSED,
                        "paused_at": paused_at,
                        "active_since": None,
                        "elapsed_seconds": goal.active_elapsed_seconds(paused_at),
                        "blocked_reason": GOAL_BUDGET_SUBAGENT_NOTE,
                    }
                )
            try:
                with self.store.transaction() as transaction:
                    if charge is None:
                        transaction.add(
                            ChatGoalUsageCharge(
                                id=charge_id,
                                engagement_id=turn.engagement_id,
                                goal_id=goal.id,
                                subagent_id=subagent_id,
                                child_turn_id=turn.id,
                                usage=turn.usage,
                            )
                        )
                    else:
                        transaction.update(
                            ChatGoalUsageCharge,
                            charge.id,
                            {"usage": turn.usage},
                            expected_revision=charge.revision,
                        )
                    transaction.update(
                        ChatGoal,
                        goal.id,
                        changes,
                        expected_revision=goal.revision,
                    )
                    if clear_pending and record is not None:
                        transaction.update(
                            ChatSubagent,
                            record.id,
                            {"pending_goal_charge_turn_id": None},
                            expected_revision=record.revision,
                        )
            except ConflictError:  # diagnostic-expected: a concurrent debit or goal write won; reread and charge the rest
                continue
            if exhausted:
                self.stop_goal_subagents_soon(goal, GOAL_BUDGET_STOP_NOTE)
            return
        raise ConflictError(
            "subagent usage could not be charged after concurrent updates"
        )

    def stop_goal_subagents_soon(self, goal: ChatGoal, reason: str) -> None:
        """Stop a goal's running subagents once its budget has paused it.

        The pause is found while a turn records usage, often one of these
        children, and stopping awaits their tasks, so it runs as its own task.
        """

        if not self.active(self.for_session(goal.session_id)):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # diagnostic-expected: no event loop means no subagent task runs in this process
            return
        task = create_diagnostic_task(
            self._stop_goal_subagents(goal.id, goal.session_id, reason),
            feature="chat",
            event_code="chat.subagent.goal_stop",
            failure_message="A paused goal's subagents could not be stopped.",
            name=f"nebula-goal-subagent-stop-{goal.id}",
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _stop_goal_subagents(
        self, goal_id: str, session_id: str, reason: str
    ) -> None:
        for record in self.active(self.for_session(session_id)):
            parent_turn = self._parent_turn(record)
            if parent_turn is None or parent_turn.goal_id != goal_id:
                continue
            try:
                await self.stop(record.id, reason=reason)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.goal_stop_failed",
                    "A subagent could not be stopped after its goal's budget ran out.",
                    exc,
                    stage="subagent-stop",
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

    def _parent_turn(
        self, record: ChatSubagent, turn_id: str | None = None
    ) -> ChatTurn | None:
        try:
            return self.store.get(ChatTurn, turn_id or record.parent_turn_id)
        except NotFoundError:  # diagnostic-expected: parent turn deleted; delivery falls back to the session
            return None

    async def _deliver(self, record: ChatSubagent) -> None:
        """Hand the parent what this subagent has for it.

        A parent waiting on its subagents resumes; a running harness is
        steered; an idle conversation gets it posted. A working provider parent
        receives it before its next step.
        """

        self._notify()
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            self._settle_goal_time(record.parent_session_id)
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
            # Children it still had running would otherwise work on with
            # nobody to report to.
            await self._stop_unsupervised_children(latest)
            await self.deliver_pending(latest.session_id)

    def pending_wait(self, turn: ChatTurn) -> dict[str, Any] | None:
        history = self.chat._turn_history(turn)
        if turn.status != ChatTurnStatus.WAITING_CALLBACK or not history:
            return None
        wait = history[-1].get("subagent_wait")
        return wait if isinstance(wait, dict) else None

    async def deliver_pending(self, parent_session_id: str) -> None:
        """The parent conversation is idle: stop children blocked on a
        decision it can no longer make, close questions it cannot answer,
        post what it has not received, and let a running goal pick it up."""

        try:
            if self.chat.pending_turn(parent_session_id) is not None:
                return
        except (
            NotFoundError
        ):  # diagnostic-expected: parent conversation deleted; nothing to post into
            return
        await self._release_blocked_children(parent_session_id)
        self._settle_goal_time(parent_session_id)
        await self._close_questions(parent_session_id)
        records = self.for_session(parent_session_id)
        posted: list[ChatSubagent] = []
        for record in records:
            if (
                record.status not in CHAT_SUBAGENT_TERMINAL_STATUSES
                or record.result_message_id is not None
                or _retracted(record)
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
        retracted = {item.id for item in records if _retracted(item)}
        for message in messages:
            if message.subagent_id in retracted:
                self._mark_messages(
                    [message], ChatSubagentMessageStatus.UNDELIVERED, RETRACTED_NOTE
                )
                continue
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

    def _settle_goal_time(self, parent_session_id: str) -> None:
        """A subagent finished: close the goal's active time if nothing works."""

        from .chat_goals import ChatGoalService

        ChatGoalService(self.store).settle_active_time(parent_session_id)

    async def _release_blocked_children(self, parent_session_id: str) -> None:
        """Stop children still blocked on approval now that no supervisor runs.

        A child that blocked while its supervisor worked was handed to it; if
        that response ended without acting, the child would otherwise wait
        forever, holding a running-at-once slot and the goal. Their reports
        are posted by the caller in the same pass.
        """

        for record in self.active(self.for_session(parent_session_id)):
            turn = self._child_turn(record)
            if (
                turn is None
                or turn.status != ChatTurnStatus.WAITING_APPROVAL
                or self.chat.has_active_provider_turn(turn.id)
            ):
                continue
            try:
                await self._child_waiting_approval(record, turn, deliver=False)
            except Exception as exc:
                record_caught_exception(
                    "chat",
                    "chat.subagent.blocked_release_failed",
                    "A subagent blocked on approval could not be stopped after its "
                    "supervisor's response ended.",
                    exc,
                    stage="subagent-deliver",
                )

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
        marker = self._restart_recovery_marker(record)
        recovered_generation = (
            marker.get("generation")
            if marker is not None
            and marker.get("child_turn_id") == record.child_turn_id
            and isinstance(marker.get("generation"), int)
            and not isinstance(marker.get("generation"), bool)
            else 0
        )
        heading = {
            ChatSubagentStatus.COMPLETED: "Subagent finished",
            ChatSubagentStatus.FAILED: "Subagent failed",
            ChatSubagentStatus.STOPPED: "Subagent stopped",
            ChatSubagentStatus.INTERRUPTED: "Subagent interrupted",
        }.get(record.status, "Subagent update")
        if recovered_generation and record.status == ChatSubagentStatus.COMPLETED:
            heading = "Subagent recovered and finished"
        finished = record.finished_at or utc_now()
        # Round one keeps the id posted before rounds existed.
        key = (
            f"nebula:subagent-result:{record.id}"
            + (f":recovery:{recovered_generation}" if recovered_generation else "")
            + (f":round:{record.rounds}" if record.rounds > 1 else "")
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
                "recovered_after_core_restart": bool(recovered_generation),
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
        details = _failure_lines(
            {"status": record.status.value, **self._report_details(record)}
        )
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

        from .chat import ChatCompletionRequest, ChatHistoryConflict, ChatRequestMessage
        from .chat_schedules import ChatScheduleService

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
        try:
            # Like every turn Core starts for a goal, this one runs with what
            # the operator last chose: model, effort, tools, MCP servers, SSH
            # hosts, hooks, subagents and agent messaging. The conversation
            # and its newest turn hold those choices; the child's copy of its
            # first parent's request does not.
            session = self.store.get(ChatSession, record.parent_session_id)
            settings = ChatScheduleService(self.store).turn_settings(session.id)
            prepared = await self.chat.prepare_async(
                ChatCompletionRequest(
                    provider_id=session.provider_profile_id
                    or parent_turn.provider_profile_id,
                    engagement_id=record.engagement_id,
                    session_id=record.parent_session_id,
                    goal_id=goal.id,
                    model=session.model or parent_turn.model,
                    reasoning_effort=settings.reasoning_effort,
                    messages=[
                        ChatRequestMessage(
                            role=ChatRole.USER,
                            content="Subagent reports are ready. Continue the conversation goal.",
                        )
                    ],
                    include_knowledge=False,
                    tools_enabled=settings.tools_enabled,
                    mcp_server_ids=settings.mcp_server_ids,
                    ssh_environment_ids=settings.ssh_environment_ids,
                    hook_ids=settings.hook_ids,
                    allow_subagents=settings.allow_subagents,
                    allow_agent_messaging=settings.allow_agent_messaging,
                    max_active_subagents=settings.max_active_subagents,
                    allow_cloud_tool_results=settings.allow_cloud_tool_results,
                    stream=True,
                )
            )
            self.chat.start_provider_turn(prepared)
        except ChatHistoryConflict:
            # diagnostic-expected: an operator message or another goal
            # continuation started first; losing that race is the intended
            # outcome, and the reports reach whichever turn won.
            return
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
                if (
                    record.status == ChatSubagentStatus.INTERRUPTED
                    and turn is not None
                    and restart_recovery_pending(turn)
                ):
                    try:
                        self._restore_restart_record(record, turn)
                    except ConflictError:  # diagnostic-expected: recovery rereads a concurrent old-worker write
                        pass
                    continue
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
    return RuntimeToolComponents(
        broker=SubagentBroker(service),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=subagent_specs(),
        # The versioned contract, not a hash of the specs: Nebula's own tools
        # change with Core, and a parked parent must resume across that.
        runtime_digest=SUBAGENT_TOOLS_CONTRACT,
    )


def contract_digest_segment(segment: str) -> str:
    """One ``+``-joined segment of a recorded runtime digest, with the
    subagent tools' pre-version fingerprint read as contract version 1."""

    if _LEGACY_SUBAGENT_DIGEST.fullmatch(segment):
        return SUBAGENT_TOOLS_CONTRACT
    return segment


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
    "CoreDelivery",
    "DeliveryItem",
    "GOAL_BUDGET_STOP_NOTE",
    "HARNESS_RESULT_BYTES",
    "HARNESS_WAIT_DEFAULT_SECONDS",
    "HARNESS_WAIT_MAX_SECONDS",
    "PARENT_STOPPED_NOTE",
    "PROVIDER_RESULT_BYTES",
    "SUBAGENT_TOOLS_CONTRACT",
    "SUBAGENT_CHILD_INSTRUCTIONS",
    "SUBAGENT_CHILD_TOOL_NAMES",
    "SUBAGENT_EFFORT_DESCRIPTION",
    "SUBAGENT_LIMIT_CEILING",
    "SUBAGENT_ROUTING_INSTRUCTIONS",
    "SUBAGENT_TOOL_NAMES",
    "ParentUpdate",
    "SubagentBroker",
    "SubagentRoundSuperseded",
    "SubagentService",
    "SubagentWaitPending",
    "contract_digest_segment",
    "harness_result_fits",
    "harness_subagent_instructions",
    "is_subagent_session",
    "pack_delivery",
    "provider_result_fits",
    "subagent_child_components",
    "subagent_child_specs",
    "subagent_components",
    "subagent_limit",
    "subagent_routing_instructions",
    "subagent_specs",
    "update_text",
]
