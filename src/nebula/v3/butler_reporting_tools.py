"""Goal-scoped provider tools that submit attributed reports to Core."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .butler_oversight import (
    GoalAttentionReport,
    GoalProgressReport,
    ingest_agent_report,
)
from .domain import RiskClass, ScopePolicy, ToolCallOrigin
from .runtime_platform import RuntimeToolComponents
from .storage import NebulaStore
from .tools import ToolExecutionResult, ToolInvocation, ToolNotPermitted, ToolSpec


REPORT_TOOL_NAMES = frozenset({"report_progress", "report_attention"})

_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["goal", "chat_turn", "harness_turn", "artifact"],
        },
        "id": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["kind", "id"],
    "additionalProperties": False,
}


def _report_spec(
    name: str, description: str, properties: dict, required: list[str]
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
        timeout_seconds=30,
    )


def report_specs() -> dict[str, ToolSpec]:
    def text(maximum: int = 500) -> dict[str, Any]:
        return {"type": "string", "minLength": 1, "maxLength": maximum}

    def lines() -> dict[str, Any]:
        return {"type": "array", "items": text(300), "minItems": 1, "maxItems": 8}

    refs = {"type": "array", "items": _REF_SCHEMA, "maxItems": 8}
    progress = _report_spec(
        "report_progress",
        "Report a meaningful goal checkpoint to Core. State the changed work, evidence, completion criterion, and next step. The report is an attributed agent claim, not a verified finding; continue the work after reporting.",
        {
            "what_changed": text(),
            "evidence_gained": lines(),
            "criterion_advanced": text(300),
            "next_step": text(),
            "blockers": {"type": "array", "items": text(300), "maxItems": 8},
            "next_checkpoint_at": {"type": ["string", "null"], "format": "date-time"},
            "evidence_refs": refs,
        },
        ["what_changed", "evidence_gained", "criterion_advanced", "next_step"],
    )
    attention = _report_spec(
        "report_attention",
        "Tell Core about a goal blocker or concern. For a down or unreachable machine, set condition and a hostname or IP; Core alerts the operator as agent-reported without waiting for verification.",
        {
            "condition": {
                "type": "string",
                "enum": ["machine_unreachable", "machine_down", "other"],
            },
            "machine": {"type": ["string", "null"], "maxLength": 253},
            "symptom": text(),
            "impact": text(),
            "evidence": lines(),
            "attempted_recovery": text(),
            "urgency": {"type": "string", "enum": ["routine", "soon", "urgent"]},
            "help_requested": text(),
            "evidence_refs": refs,
        },
        [
            "condition",
            "symptom",
            "impact",
            "evidence",
            "attempted_recovery",
            "urgency",
            "help_requested",
        ],
    )
    return {spec.name: spec for spec in (progress, attention)}


class ButlerReportBroker:
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
            invocation.origin != ToolCallOrigin.CHAT
            or invocation.requested_by != "chat-assistant"
            or invocation.chat_turn_id is None
            or invocation.chat_session_id is None
            or invocation.run_id != invocation.chat_turn_id
        ):
            raise ToolNotPermitted(
                "goal reports require a Core-owned assistant turn",
                rule="butler.goal_report_turn",
            )
        if invocation.tool_name == "report_progress":
            body = GoalProgressReport.model_validate(invocation.arguments)
        elif invocation.tool_name == "report_attention":
            body = GoalAttentionReport.model_validate(invocation.arguments)
        else:
            raise ToolNotPermitted(
                "unsupported goal report tool", rule="butler.goal_report_tool"
            )
        report, incident = await asyncio.to_thread(
            ingest_agent_report,
            self.store,
            turn_id=invocation.chat_turn_id,
            idempotency_key=invocation.idempotency_key or invocation.id,
            body=body,
            source="provider_tool",
            expected_project_id=invocation.engagement_id,
            expected_session_id=invocation.chat_session_id,
        )
        return ToolExecutionResult(
            output={
                "report_id": report.id,
                "accepted": True,
                "verification_state": "agent_reported",
                "incident_id": incident.id if incident is not None else None,
            }
        )


def report_components(
    store: NebulaStore,
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents:
    return RuntimeToolComponents(
        broker=ButlerReportBroker(store),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:butler-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs=report_specs(),
        # These are Core-owned tools; resuming a turn must not require a
        # profile/runtime fingerprint for them.
        runtime_digest="",
    )
