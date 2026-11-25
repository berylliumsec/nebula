"""Operator action requests opened by a goal's own agent.

A Core-owned goal sometimes needs something only a person can do: replug or
power-cycle a device, restart a host, attach a credential, approve an account
change, or look at hardware. Butler could already record that as a typed
``Needs you:`` request (``operator_action_requests``); goal agents could not,
so they wrote the need into ordinary Work check-ins and the Butler board never
showed it. ``goal.request_operator_action`` gives the goal's agent that path,
for provider goals (a Core goal tool beside ``goal.complete``) and for harness
goals (a native gateway tool beside ``goal.block``). Both use
``request_from_goal_turn`` and the existing record, verification, auto-check,
de-duplication, notification, goal-block and sweep-resume logic.

Operator autonomy is in force: goals decide research questions themselves.
The tool is only for operator-only physical or account actions. Core enforces
what it can check deterministically (see ``guardrail_refusal``) and leaves the
rest to the tool description:

* the caller must be the turn that owns a running Core goal;
* Core-only actions (``review_model_refusal``, ``answer_question``) are
  refused;
* a physical action must name a concrete device or host, not a placeholder;
* a reason that asks for a decision ("should I", "which ... should",
  "do you want") is refused for every action, and any question for
  ``other``, with a message to decide and continue;
* at most ``MAX_OPEN_PER_GOAL`` open requests per goal.

``handle_headless_question`` serves the Claude harness when its model calls
the interactive ``AskUserQuestion`` tool during a headless Nebula turn: a
research decision is answered with "decide yourself", an operator-only
physical or account request is opened (``answer_question``) and blocks the
goal, and a turn that is not a goal is told to ask in its reply.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

from .diagnostics import record_caught_exception, record_diagnostic
from .domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    HarnessTurn,
    RiskClass,
    ToolCallOrigin,
)
from .operator_action_requests import (
    _CHECK_SCHEMA,
    _EVIDENCE_LIST,
    _block_goal,
    ACTION_LABEL,
    ACTIONS,
    GOAL_WAIT_MARKER,
    HOST_ACTIONS,
    PAYLOAD_FENCE,
    CheckEvaluator,
    DEFAULT_NOTIFIER,
    OperatorActionNotifier,
    OperatorActionRecord,
    _request_rows,
    create_request,
    parse_record,
)
from .storage import NebulaStore
from .tools import (
    IdempotencyBehavior,
    InvalidToolArguments,
    ToolExecutionResult,
    ToolInvocation,
    ToolNotPermitted,
    ToolSpec,
)

GOAL_REQUEST_TOOL = "goal.request_operator_action"
# Raised by Core itself, never by a goal agent through the tool.
CORE_ONLY_ACTIONS = frozenset({"review_model_refusal", "answer_question"})
GOAL_ACTIONS: tuple[str, ...] = tuple(
    action for action in ACTIONS if action not in CORE_ONLY_ACTIONS
)
MAX_OPEN_PER_GOAL = 3
QUESTION_SUBJECT = "goal chat"
ASK_USER_TOOL = "AskUserQuestion"

_PLACEHOLDER_SUBJECTS = frozenset(
    {
        "?",
        "-",
        "n/a",
        "na",
        "none",
        "tbd",
        "unknown",
        "any",
        "all",
        "operator",
        "the operator",
        "you",
    }
)
# Words that name a kind of machine, not one machine.
_GENERIC_MACHINES = frozenset(
    {
        "device",
        "devices",
        "host",
        "hosts",
        "machine",
        "machines",
        "computer",
        "server",
        "mac",
        "phone",
        "iphone",
        "ipad",
        "hardware",
        "lab",
        "target",
        "test device",
        "research mac",
        "research device",
    }
)
_DECISION = re.compile(
    r"\b(should i|shall i|should we|shall we|do you want|would you like|"
    r"would you prefer|which (?:one|option|approach|lane|target|path|hypothesis)"
    r"|your (?:decision|preference|call|choice)|let me know (?:which|whether|if)"
    r"|(?:please )?(?:decide|choose|pick) (?:which|whether|between)"
    r"|what should|how should|whether to)\b",
    re.IGNORECASE,
)
_QUESTION_LEAD = re.compile(
    r"^\s*(should|shall|which|what|whether|do you|would you|how|where|when|"
    r"is it ok|can we|may i)\b",
    re.IGNORECASE,
)
# The operator is asked to act with their hands or their account.
_OPERATOR_ONLY = re.compile(
    r"\b(power[- ]?cycle|replug|re-plug|unplug|plug (?:it |them |the \S+ )?"
    r"(?:back )?in|physically|by hand|(?:press|hold) (?:and hold )?the "
    r"(?:power|side|home|reset) button|unlock (?:the |your )?(?:iphone|ipad|"
    r"phone|device|mac)|enter (?:the |your )?passcode|attach (?:a |the )?"
    r"credential|(?:provide|enter|paste|share|add|rotate) (?:a |an |the |your )?"
    r"(?:new )?(?:password|passcode|api key|token|secret|credential|one-time code"
    r"|2fa code|mfa code|otp)|approve (?:the |an |this |my )?(?:account|billing|"
    r"payment|purchase|subscription|sign-?in|login)|(?:restart|reboot) (?:the |"
    r"your )?(?:host|machine|server|mac|router|device|iphone|ipad|phone))\b",
    re.IGNORECASE,
)
# The agent asks for a choice it can make itself.
_SELF_DECISION = re.compile(
    r"\b(should i|shall i|do you want me to|would you like me to|which|"
    r"would you prefer|is it ok (?:if|to) i|may i|can i)\b",
    re.IGNORECASE,
)

DECIDE_YOURSELF = (
    "Operator autonomy is in force for this goal: decide this yourself from the "
    "evidence, record the decision and its reason in your next report_progress "
    "or Work check-in, and continue. Use goal.request_operator_action only for "
    "an operator-only physical or account action."
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(GOAL_ACTIONS)},
        "device_or_host": {
            "type": "string",
            "minLength": 1,
            "maxLength": 253,
            "description": "The exact device or host name. Physical actions need a concrete one.",
        },
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "What the operator must do and why only they can. An instruction, not a question.",
        },
        "evidence": {**_EVIDENCE_LIST, "maxItems": 10},
        "verification": {
            **_EVIDENCE_LIST,
            "minItems": 1,
            "maxItems": 6,
            "description": "Required: the checks you ran that show only the operator can do this.",
        },
        "urgency": {
            "type": "string",
            "enum": ["low", "normal", "high", "blocking"],
            "default": "normal",
        },
        "check": _CHECK_SCHEMA,
        "block_goal": {
            "type": "boolean",
            "default": False,
            "description": "Block this goal until the request closes; Core resumes it then. End the turn after a blocked result.",
        },
    },
    "required": ["action", "device_or_host", "reason", "verification"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Ask the operator for an operator-only physical or account action: replug, "
    "plug in or power-cycle a device, restart a host, attach a credential, "
    "approve an account change, or a physical check. Never use it for research "
    "decisions or questions: operator autonomy is in force, so decide those "
    "yourself and continue. Verify first and pass what you checked as "
    "verification. Give an auto-check that shows when it is done (required for "
    "host and device actions); if it already passes the request is refused. An "
    "open request for the same action, device and goal is returned instead of a "
    "new one. The request shows at the top of the Butler board and notifies the "
    "operator. block_goal blocks this goal until the request closes, and Core "
    "resumes it then; end the turn after a blocked result. Do not write "
    "'NEEDS YOU' into Work check-ins instead."
)


def goal_request_spec() -> ToolSpec:
    return ToolSpec(
        name=GOAL_REQUEST_TOOL,
        display_name="Request operator action",
        description=DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.WORKSPACE_WRITE,
        idempotency=IdempotencyBehavior.KEY_REQUIRED,
        budget_class="execution",
        timeout_seconds=60,
        uses_execution_capacity=False,
    )


def gateway_tool() -> dict[str, Any]:
    """The harness gateway catalog entry (MCP shape)."""

    return {
        "name": GOAL_REQUEST_TOOL,
        "description": DESCRIPTION,
        "inputSchema": _INPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _subject_refusal(action: str, device_or_host: str) -> str | None:
    subject = _norm(device_or_host)
    bare = subject.removeprefix("the ").removeprefix("a ").removeprefix("my ")
    if subject in _PLACEHOLDER_SUBJECTS or not re.search(r"[0-9a-z]", subject):
        return "Name the exact device, host or account this is about."
    if action in HOST_ACTIONS and bare in _GENERIC_MACHINES:
        return (
            f"{ACTION_LABEL[action]} needs a concrete device or host name "
            f"(for example its hostname or inventory name), not {device_or_host!r}."
        )
    return None


def _reason_refusal(action: str, reason: str) -> str | None:
    text = " ".join(str(reason).split())
    if _DECISION.search(text):
        return (
            "This reads as a research decision. "
            + DECIDE_YOURSELF
            + " State in the reason what the operator must do, as an instruction."
        )
    if action == "other" and ("?" in text or _QUESTION_LEAD.search(text)):
        return (
            "An 'other' request must say what the operator must do, not ask a "
            "question. " + DECIDE_YOURSELF
        )
    return None


def guardrail_refusal(arguments: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(code, message)`` when Core refuses the request before saving it."""

    action = str(arguments.get("action") or "")
    if action in CORE_ONLY_ACTIONS or action not in GOAL_ACTIONS:
        return ("action_not_allowed", f"{action!r} is not a goal request action.")
    subject = _subject_refusal(action, str(arguments.get("device_or_host") or ""))
    if subject:
        return ("device_not_concrete", subject)
    reason = _reason_refusal(action, str(arguments.get("reason") or ""))
    if reason:
        return ("research_question", reason)
    return None


def operator_only_question(text: str) -> bool:
    """Whether a question asks the operator to act with hands or account.

    Deterministic and deliberately narrow: a request to replug, power-cycle,
    unlock, restart a machine, give a credential or approve an account change,
    and not a question about whether the agent itself should do something.
    """

    flat = " ".join(str(text).split())
    return bool(_OPERATOR_ONLY.search(flat)) and not _SELF_DECISION.search(flat)


# ---------------------------------------------------------------------------
# Goal ownership and blocking
# ---------------------------------------------------------------------------


async def owned_goal(
    store: NebulaStore, *, project_id: str, session_id: str, chat_turn_id: str
) -> tuple[ChatGoal, ChatTurn]:
    """The running goal this chat turn owns, or ToolNotPermitted."""

    from .chat_goals import ChatGoalService

    turn = await store.get(ChatTurn, chat_turn_id)
    goal = await ChatGoalService(store).get(session_id)
    if (
        turn.session_id != session_id
        or turn.engagement_id != project_id
        or goal.engagement_id != project_id
        or turn.goal_id != goal.id
        or goal.status != ChatGoalStatus.RUNNING
        or not goal.execution_claim_id
        or goal.execution_claim_id not in {turn.id, turn.execution_claim_id}
    ):
        raise ToolNotPermitted(
            "this turn no longer owns a running Core goal",
            rule="goal.operator_request_owner",
        )
    return goal, turn


class OwnTurnGoalBlocker:
    """``_block_goal``'s ``work`` for the goal's own turn.

    Blocks on the goal's newest revision, after checking again that this turn
    still owns it (the same rule as the harness ``goal.block`` tool).
    """

    def __init__(self, store: NebulaStore, chat_turn_id: str) -> None:
        self.store = store
        self.chat_turn_id = chat_turn_id

    async def goal_action(self, project_id: str, arguments: dict[str, Any]) -> None:
        from .chat_goals import ChatGoalService

        if arguments.get("action") != "block":
            raise ValueError("a goal turn can only block its own goal")
        turn = await self.store.get(ChatTurn, self.chat_turn_id)

        def still_owned(latest: ChatGoal) -> None:
            if (
                latest.engagement_id != project_id
                or latest.id != turn.goal_id
                or latest.status != ChatGoalStatus.RUNNING
                or latest.execution_claim_id not in {turn.id, turn.execution_claim_id}
            ):
                from .storage import ConflictError

                raise ConflictError("This turn no longer owns a running Core goal")

        await ChatGoalService(self.store).write_latest(
            str(arguments["session_id"]),
            "block",
            reason=str(arguments["reason"]),
            check=still_owned,
        )


async def _open_for_goal(
    store: NebulaStore, session_id: str, action: str, device_or_host: str
) -> tuple[int, bool]:
    """Open typed requests of this goal, and whether one already matches."""

    rows = await _request_rows(
        store,
        done=False,
        description_contains=[f"```{PAYLOAD_FENCE}", session_id],
    )
    count = 0
    matched = False
    for item, _name in rows:
        record = parse_record(item.description)
        if (
            record is not None
            and record.status == "open"
            and record.goal_session_id == session_id
        ):
            count += 1
            matched = matched or (
                record.action == action
                and _norm(record.device_or_host) == _norm(device_or_host)
            )
    return count, matched


def _record_of(
    request: Mapping[str, Any], project_id: str, session_id: str
) -> OperatorActionRecord:
    """The record a request summary describes, for blocking its goal."""

    return OperatorActionRecord.model_validate(
        {
            key: request.get(key)
            for key in ("action", "device_or_host", "reason", "verification", "check")
        }
        | {
            "evidence": list(request.get("evidence") or []),
            "urgency": request.get("urgency") or "normal",
            "project_id": project_id,
            "goal_session_id": session_id,
        }
    )


async def request_from_goal_turn(
    store: NebulaStore,
    *,
    project_id: str,
    session_id: str,
    chat_turn_id: str,
    arguments: Mapping[str, Any],
    runtime: str,
    evaluator: CheckEvaluator | None = None,
    notifier: OperatorActionNotifier | None = DEFAULT_NOTIFIER,
) -> dict[str, Any]:
    """Open one operator action request for the goal this turn owns."""

    try:
        Draft202012Validator(_INPUT_SCHEMA).validate(dict(arguments))
    except SchemaValidationError as exc:
        # diagnostic-expected: the model reads the schema error and corrects its call.
        raise InvalidToolArguments(exc.message) from exc
    goal, _turn = await owned_goal(
        store, project_id=project_id, session_id=session_id, chat_turn_id=chat_turn_id
    )
    refusal = guardrail_refusal(arguments)
    if refusal is not None:
        return {"created": False, "refusal": refusal[0], "message": refusal[1]}
    open_count, matched = await _open_for_goal(
        store,
        session_id,
        str(arguments["action"]),
        str(arguments["device_or_host"]),
    )
    # A repeat of an open request still returns it (and can block on it).
    if open_count >= MAX_OPEN_PER_GOAL and not matched:
        return {
            "created": False,
            "refusal": "too_many_open",
            "message": (
                f"This goal already has {MAX_OPEN_PER_GOAL} open operator action "
                "requests. Wait for one to close, or continue other work."
            ),
        }
    try:
        result = await create_request(
            store,
            {
                **{
                    key: value
                    for key, value in arguments.items()
                    if key != "block_goal"
                },
                "project_id": project_id,
                "goal_session_id": session_id,
                "created_by": f"goal-turn:{chat_turn_id}"[:200],
            },
            evaluator=evaluator,
            notifier=notifier,
        )
    except ValueError as exc:
        # diagnostic-expected: an incomplete request (no auto-check for a host action, bad lines) goes back to the model to correct.
        raise InvalidToolArguments(str(exc)[:1000]) from exc
    request = result.get("request")
    block = None
    if (
        arguments.get("block_goal")
        and isinstance(request, Mapping)
        and (result.get("created") or result.get("refusal") == "duplicate")
    ):
        block = await _block_goal(
            store,
            OwnTurnGoalBlocker(store, chat_turn_id),
            str(request["id"]),
            _record_of(request, project_id, session_id),
        )
    record_diagnostic(
        "info",
        "butler",
        "butler.operator_action.goal_request",
        "A goal agent asked for an operator action.",
        outcome="created" if result.get("created") else str(result.get("refusal")),
        stage="operator-action-goal",
        session_id=session_id,
        metadata={
            "goal_id": goal.id,
            "turn_id": chat_turn_id,
            "runtime": runtime,
            "action": str(arguments.get("action")),
            "goal_block": block,
        },
    )
    output: dict[str, Any] = {**result, "goal_block": block}
    if block == "blocked":
        output["next_action"] = (
            "This goal is blocked until the operator finishes the request; Core "
            "resumes it when the request closes. End this turn with a short "
            "handoff and do not retry the action yourself."
        )
    elif result.get("created") or result.get("refusal") == "duplicate":
        output["next_action"] = (
            "The request is on the Butler board. Continue goal work that does "
            "not depend on it."
        )
    return output


async def execute_goal_request(
    store: NebulaStore, invocation: ToolInvocation
) -> ToolExecutionResult:
    """The provider goal tool (``GoalCompletionBroker`` routes here)."""

    if (
        invocation.tool_name != GOAL_REQUEST_TOOL
        or invocation.origin != ToolCallOrigin.CHAT
        or invocation.requested_by not in {"chat-assistant", "harness-gateway"}
        or not invocation.chat_turn_id
        or not invocation.chat_session_id
        or invocation.run_id != invocation.chat_turn_id
    ):
        raise ToolNotPermitted(
            "operator action requests need the active goal's assistant turn",
            rule="goal.operator_request_turn",
        )
    output = await request_from_goal_turn(
        store,
        project_id=invocation.engagement_id,
        session_id=invocation.chat_session_id,
        chat_turn_id=invocation.chat_turn_id,
        arguments=invocation.arguments,
        runtime="harness"
        if invocation.runtime_session_kind == "harness"
        else "provider",
    )
    return ToolExecutionResult(output=output)


# ---------------------------------------------------------------------------
# Claude AskUserQuestion in a headless turn
# ---------------------------------------------------------------------------


def question_text(tool_input: Mapping[str, Any]) -> list[str]:
    """The questions (and their option labels) as short lines."""

    lines: list[str] = []
    questions = tool_input.get("questions")
    if isinstance(questions, list):
        for entry in questions[:8]:
            if not isinstance(entry, Mapping):
                continue
            text = " ".join(str(entry.get("question") or "").split())
            options = [
                " ".join(str(option.get("label") or "").split())
                for option in entry.get("options") or []
                if isinstance(option, Mapping)
            ]
            if options:
                text = f"{text} Options: {', '.join(item for item in options if item)}"
            if text.strip():
                lines.append(text[:300])
    elif isinstance(tool_input.get("question"), str):
        lines.append(" ".join(tool_input["question"].split())[:300])
    return lines


async def handle_headless_question(
    store: NebulaStore,
    harness_turn: HarnessTurn,
    tool_input: Mapping[str, Any],
    *,
    notifier: OperatorActionNotifier | None = DEFAULT_NOTIFIER,
) -> str:
    """The tool result Claude reads for AskUserQuestion. Never raises."""

    lines = question_text(tool_input)
    try:
        goal_id = None
        if harness_turn.chat_turn_id and harness_turn.chat_session_id:
            chat_turn = await store.get(ChatTurn, harness_turn.chat_turn_id)
            goal_id = chat_turn.goal_id
        if goal_id is None:
            outcome = "not_goal"
            message = (
                "Nebula runs this turn without an interactive prompt, so "
                "AskUserQuestion is not delivered to anyone. Decide what you can "
                "yourself; if only the operator can answer, ask in your reply "
                "and end the turn."
            )
        elif not lines or not operator_only_question(" ".join(lines)):
            outcome = "decide_yourself"
            message = DECIDE_YOURSELF + " Nothing was sent to the operator."
        else:
            assert harness_turn.chat_session_id and harness_turn.chat_turn_id
            goal, _turn = await owned_goal(
                store,
                project_id=harness_turn.engagement_id,
                session_id=harness_turn.chat_session_id,
                chat_turn_id=harness_turn.chat_turn_id,
            )
            result = await create_request(
                store,
                {
                    "action": "answer_question",
                    "device_or_host": QUESTION_SUBJECT,
                    "reason": ("The goal agent needs the operator: " + " ".join(lines))[
                        :1000
                    ],
                    "goal_session_id": goal.session_id,
                    "evidence": lines[:10],
                    "verification": [
                        "Core classified the question as an operator-only physical "
                        "or account action.",
                        "The agent asked through AskUserQuestion in a headless goal "
                        "turn, which nothing relays to the operator.",
                    ],
                    "urgency": "high",
                    "created_by": f"goal-turn:{harness_turn.chat_turn_id}"[:200],
                },
                notifier=notifier,
            )
            request = result.get("request") or {}
            request_id = str(request.get("id") or "")
            record = _record_of(request, goal.engagement_id, goal.session_id)
            block = await _block_goal(
                store,
                OwnTurnGoalBlocker(store, harness_turn.chat_turn_id),
                request_id,
                record,
            )
            outcome = f"request:{block}"
            message = (
                f"Nebula does not relay AskUserQuestion. Core opened operator action "
                f"request {request_id} ({GOAL_WAIT_MARKER}{request_id}) with this "
                "question on the Butler board"
                + (
                    " and blocked the goal; Core resumes it when the operator "
                    "closes the request. Read that Work item's updates for the "
                    "answer then. End this turn with a short handoff."
                    if block == "blocked"
                    else f" (goal block: {block}). Continue work that does not "
                    "depend on the answer."
                )
            )
    except Exception as exc:
        record_caught_exception(
            "harnesses",
            "harnesses.claude.ask_user_failed",
            "A headless AskUserQuestion could not be turned into an operator request.",
            exc,
            stage="ask-user",
        )
        return (
            "Nebula does not relay AskUserQuestion in this turn. "
            + DECIDE_YOURSELF
            + " If only the operator can act, call goal.request_operator_action."
        )
    record_diagnostic(
        "info",
        "harnesses",
        "harnesses.claude.ask_user_question",
        "A Claude harness model called AskUserQuestion during a headless turn.",
        outcome=outcome,
        stage="ask-user",
        session_id=harness_turn.chat_session_id,
        metadata={"harness_turn_id": harness_turn.id, "questions": len(lines)},
    )
    return message
