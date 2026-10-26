"""Core-owned completion action for the active provider goal turn."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, ValidationError, field_validator

from .chat_goals import ChatGoalService
from .domain import (
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    RiskClass,
    ScopePolicy,
    ToolCallOrigin,
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
GOAL_TOOL_NAMES = frozenset({GOAL_COMPLETE_TOOL_NAME})


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
        if (
            invocation.tool_name != GOAL_COMPLETE_TOOL_NAME
            or invocation.origin != ToolCallOrigin.CHAT
            or invocation.requested_by != "chat-assistant"
            or not invocation.chat_turn_id
            or not invocation.chat_session_id
            or invocation.run_id != invocation.chat_turn_id
        ):
            raise ToolNotPermitted(
                "goal completion requires the active goal's assistant turn",
                rule="chat.goal_complete_turn",
            )
        turn = self.store.get(ChatTurn, invocation.chat_turn_id)
        goals = ChatGoalService(self.store)
        goal = goals.get(invocation.chat_session_id)
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
        if goals.has_working_subagents(turn.session_id):
            raise ToolNotPermitted(
                "wait for this goal's working subagents before completing it",
                rule="chat.goal_complete_children",
            )
        GoalCompletionProposal.model_validate(invocation.arguments)
        return ToolExecutionResult(
            output={
                "accepted": True,
                "goal_id": goal.id,
                "turn_id": turn.id,
                "request_digest": _proposal_digest(invocation.arguments),
                "state": "pending_final_answer",
                "next_action": "Give the operator a concise final answer. Core will save the goal and answer together.",
            }
        )


def goal_completion_spec() -> ToolSpec:
    return ToolSpec(
        name=GOAL_COMPLETE_TOOL_NAME,
        display_name="Complete goal",
        description=(
            "Complete this turn's Core-owned goal only after every completion criterion "
            "is met. Supply a concise summary and concrete evidence. Core validates "
            "turn ownership, saves the goal with the final answer, and stops automatic "
            "continuations. Evidence is recorded as agent-reported, not independently verified."
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
    )


def goal_completion_components(
    store: NebulaStore,
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    return RuntimeToolComponents(
        broker=GoalCompletionBroker(store),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:goal-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs={GOAL_COMPLETE_TOOL_NAME: goal_completion_spec()},
        runtime_digest="",
    )
