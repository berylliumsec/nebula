"""Core-owned completion action for the active provider goal turn."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, ValidationError, field_validator

from .butler_review_gating import closure_request_refusal, record_closure_request
from .chat_goal_addenda import GOAL_READ_TOOL_NAME, read_goal_page
from .chat_goals import ChatGoalService
from .domain import (
    GOAL_COMPLETION_PROPOSAL_KEY,
    GOAL_COMPLETION_REVIEW_REASON,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ScopePolicy,
    ToolCallOrigin,
    utc_now,
)
from .runtime_platform import RuntimeToolComponents
from .storage import NebulaStore
from .tools import (
    IdempotencyBehavior,
    ToolExecutionResult,
    ToolInvocation,
    ToolNotPermitted,
    ToolSpec,
)


GOAL_COMPLETE_TOOL_NAME = "goal.complete"
GOAL_TOOL_NAMES = frozenset({GOAL_COMPLETE_TOOL_NAME, GOAL_READ_TOOL_NAME})


class GoalCompletionProposal(BaseModel):
    completion_summary: str = Field(min_length=1, max_length=4_000)
    completion_evidence: list[str] = Field(min_length=1, max_length=12)

    @field_validator("completion_summary")
    @classmethod
    def summary_has_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("completion summary must contain text")
        return value

    @field_validator("completion_evidence")
    @classmethod
    def evidence_has_content(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 1_000 for item in cleaned):
            raise ValueError("each evidence item must contain 1 to 1000 characters")
        return cleaned


def _proposal_digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def accepted_goal_completion(
    history: Iterable[dict[str, Any]], *, goal_id: str, turn_id: str
) -> GoalCompletionProposal | None:
    """Read only a validated, successfully recorded completion tool result."""

    for entry in reversed(list(history)):
        if (
            entry.get("name") != GOAL_COMPLETE_TOOL_NAME
            or entry.get("status") != "complete"
        ):
            continue
        result = entry.get("provider_result")
        try:
            result = json.loads(result) if isinstance(result, str) else result
        except (
            json.JSONDecodeError
        ):  # diagnostic-expected: malformed tool history cannot prove completion
            continue
        arguments = entry.get("arguments")
        if not isinstance(result, dict) or not isinstance(arguments, dict):
            continue
        if (
            result.get("accepted") is True
            and result.get("goal_id") == goal_id
            and result.get("turn_id") == turn_id
            and result.get("request_digest") == _proposal_digest(arguments)
        ):
            try:
                return GoalCompletionProposal.model_validate(arguments)
            except ValidationError:  # diagnostic-expected: invalid historical arguments cannot prove completion
                continue
    return None


def agent_completion_changes(
    goal: ChatGoal,
    proposal: GoalCompletionProposal,
    *,
    turn_id: str,
    now: datetime,
) -> dict[str, Any]:
    """The goal changes an agent's accepted completion applies.

    Without closure review the goal completes on the agent's own report. A
    goal that requires review stops blocked instead, holding the proposal for
    the operator or Butler to approve or send back; nothing continues it.
    Ownership fields are left to the caller, which knows the claim it holds.
    """

    evidence = [
        {"kind": "agent_reported", "detail": detail, "turn_id": turn_id}
        for detail in proposal.completion_evidence
    ]
    stopped = {
        "active_since": None,
        "elapsed_seconds": goal.active_elapsed_seconds(now),
    }
    if not goal.completion_requires_review:
        return {
            **stopped,
            "status": ChatGoalStatus.COMPLETED,
            "completed_at": now,
            "completion_summary": proposal.completion_summary,
            "completion_evidence": evidence,
            "blocked_reason": None,
        }
    return {
        **stopped,
        "status": ChatGoalStatus.BLOCKED,
        "blocked_reason": GOAL_COMPLETION_REVIEW_REASON,
        "metadata": {
            **goal.metadata,
            GOAL_COMPLETION_PROPOSAL_KEY: {
                "completion_summary": proposal.completion_summary,
                "completion_evidence": evidence,
                "turn_id": turn_id,
                "proposed_at": now.isoformat(),
            },
        },
    }


async def _gate_closure_request(
    goals: ChatGoalService,
    goal: ChatGoal,
    turn_id: str,
    proposal: GoalCompletionProposal,
) -> None:
    """Rate-limit, sign-off and must-do checks on the goal's own closure request.

    Every request is recorded in ``metadata["closure_requests"]`` with its
    outcome. A proposal that claims a supervisor or reviewer sign-off is
    rejected outright: only Core's independent reviewer records approval.
    """

    now = utc_now()
    refusal = closure_request_refusal(
        goal,
        [proposal.completion_summary, *proposal.completion_evidence],
        now=now,
    )
    entry: dict[str, Any] = {
        "at": now.isoformat(),
        "turn_id": turn_id,
        "outcome": refusal[0] if refusal else "accepted",
    }
    if refusal:
        entry["reason"] = refusal[1][:1_000]
    await record_closure_request(goals.store, goals.get, goal.session_id, entry)
    if refusal:
        raise ToolNotPermitted(
            "closure request refused: " + refusal[1],
            rule=f"chat.goal_complete_{refusal[0]}",
        )


class GoalCompletionBroker:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Any | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        if invocation.tool_name == GOAL_READ_TOOL_NAME:
            return await self._read(invocation)
        if (
            invocation.tool_name != GOAL_COMPLETE_TOOL_NAME
            or invocation.origin != ToolCallOrigin.CHAT
            or invocation.requested_by not in {"chat-assistant", "harness-gateway"}
            or not invocation.chat_turn_id
            or not invocation.chat_session_id
            or invocation.run_id != invocation.chat_turn_id
        ):
            raise ToolNotPermitted(
                "goal completion requires the active goal's assistant turn",
                rule="chat.goal_complete_turn",
            )
        turn = await self.store.get(ChatTurn, invocation.chat_turn_id)
        goals = ChatGoalService(self.store)
        goal = await goals.get(invocation.chat_session_id)
        if (
            turn.session_id != invocation.chat_session_id
            or turn.engagement_id != invocation.engagement_id
            or turn.goal_id != goal.id
            or turn.status != ChatTurnStatus.ROUTING
            or not turn.execution_claim_id
            or turn.execution_claim_id != goal.execution_claim_id
            or turn.execution_owner_id != goal.execution_owner_id
            or goal.status != ChatGoalStatus.RUNNING
        ):
            raise ToolNotPermitted(
                "this turn no longer owns a running Core goal",
                rule="chat.goal_complete_owner",
            )
        if await goals.has_working_subagents(turn.session_id):
            raise ToolNotPermitted(
                "wait for this goal's working subagents before completing it",
                rule="chat.goal_complete_children",
            )
        proposal = GoalCompletionProposal.model_validate(invocation.arguments)
        await _gate_closure_request(goals, goal, turn.id, proposal)
        output: dict[str, Any] = {
            "accepted": True,
            "goal_id": goal.id,
            "turn_id": turn.id,
            "request_digest": _proposal_digest(invocation.arguments),
            "state": "pending_final_answer",
            "next_action": "Give the operator a concise final answer. Core will save the goal and answer together.",
        }
        if goal.completion_requires_review:
            output.update(
                {
                    "state": "pending_closure_review",
                    "next_action": (
                        "Give the operator a concise final answer. This goal "
                        "requires closure review: Core will hold it with your "
                        "summary and evidence until the operator or Butler "
                        "approves the completion or sends it back. Do not "
                        "describe the goal as complete."
                    ),
                }
            )
        return ToolExecutionResult(output=output)

    async def _read(self, invocation: ToolInvocation) -> ToolExecutionResult:
        if (
            invocation.origin != ToolCallOrigin.CHAT
            or invocation.requested_by not in {"chat-assistant", "harness-gateway"}
            or not invocation.chat_session_id
        ):
            raise ToolNotPermitted(
                "goal.read requires a goal conversation's assistant turn",
                rule="chat.goal_read_turn",
            )
        goal = await ChatGoalService(self.store).get(invocation.chat_session_id)
        if goal.engagement_id != invocation.engagement_id:
            raise ToolNotPermitted(
                "this goal belongs to another project", rule="chat.goal_read_project"
            )
        offset = invocation.arguments.get("offset", 0)
        if type(offset) is not int or offset < 0:
            offset = 0
        return ToolExecutionResult(output=read_goal_page(goal, offset))


def goal_read_spec() -> ToolSpec:
    return ToolSpec(
        name=GOAL_READ_TOOL_NAME,
        display_name="Read goal",
        description=(
            "Read this conversation's current Core-owned goal: objective, "
            "completion criteria, plan and supervisor notes, in pages. Use it "
            "when Core says the supervisor changed the goal or a note was "
            "omitted; pass next_offset to read on."
        ),
        input_schema={
            "type": "object",
            "properties": {"offset": {"type": "integer", "minimum": 0}},
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
        timeout_seconds=30,
        uses_execution_capacity=False,
    )


def goal_completion_spec() -> ToolSpec:
    return ToolSpec(
        name=GOAL_COMPLETE_TOOL_NAME,
        display_name="Complete goal",
        description=(
            "Complete this turn's Core-owned goal only after every completion criterion "
            "is met. Supply a concise summary and concrete evidence. Core validates "
            "turn ownership, saves the goal with the final answer, and stops automatic "
            "continuations. Evidence is recorded as agent-reported, not independently verified. "
            "A goal that requires closure review is held for the operator or Butler to "
            "approve instead of completing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "completion_summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4_000,
                },
                "completion_evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1_000},
                },
            },
            "required": ["completion_summary", "completion_evidence"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.WORKSPACE_WRITE,
        idempotency=IdempotencyBehavior.KEY_REQUIRED,
        budget_class="execution",
        timeout_seconds=30,
        uses_execution_capacity=False,
    )


def goal_completion_components(
    store: NebulaStore,
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
    include_read: bool = False,
) -> RuntimeToolComponents:
    """The goal's own tools. Provider turns also get ``goal.read``, which
    carries supervisor addenda; harness turns are steered instead."""

    specs = {GOAL_COMPLETE_TOOL_NAME: goal_completion_spec()}
    if include_read:
        specs[GOAL_READ_TOOL_NAME] = goal_read_spec()
    return RuntimeToolComponents(
        broker=GoalCompletionBroker(store),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:goal-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=specs,
        runtime_digest="",
    )
