"""Bounded model specialists that can use only brokered tool capabilities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .domain import Approval, RiskClass, RunBudget, ScopePolicy
from .orchestration import (
    MissionError,
    MissionPlan,
    PlannedTask,
    SpecialistContext,
    SpecialistResult,
    SpecialistRole,
)
from .providers import ModelMessage, ModelProvider, ModelRequest, ToolDefinition
from .tools import PolicyDenied, ToolBroker, ToolInvocation, ToolSpec


_ROLE_BY_PREFIX: tuple[tuple[str, SpecialistRole], ...] = (
    ("nmap.", SpecialistRole.NETWORK_SERVICE),
    ("nuclei.", SpecialistRole.WEB_API),
    ("nikto.", SpecialistRole.WEB_API),
    ("searchsploit.", SpecialistRole.VULNERABILITY_INTELLIGENCE),
    ("semgrep.", SpecialistRole.CODE_ANALYSIS),
)


def role_for_tool(tool_name: str) -> SpecialistRole:
    for prefix, role in _ROLE_BY_PREFIX:
        if tool_name.startswith(prefix):
            return role
    raise MissionError(f"tool {tool_name!r} has no bounded specialist role")


class ToolMissionSupervisor:
    """Build a deterministic role graph from an operator-selected tool lock."""

    def __init__(self, specs: Mapping[str, ToolSpec]) -> None:
        self.specs = dict(specs)

    async def plan(
        self, objective: str, context: Mapping[str, Any], budget: RunBudget
    ) -> MissionPlan:
        selected = tuple(dict.fromkeys(context.get("tool_names", ())))
        if not selected:
            raise MissionError("tool mission context does not select any tools")
        unknown = sorted(set(selected) - self.specs.keys())
        if unknown:
            raise MissionError(f"mission selected unavailable tools: {unknown}")

        by_role: dict[SpecialistRole, list[str]] = {}
        for name in selected:
            by_role.setdefault(role_for_tool(name), []).append(name)
        if len(by_role) > budget.max_concurrency * 8:
            raise MissionError("selected tool set produces an excessive task graph")

        scope_summary = context.get(
            "scope_summary", "operator-approved engagement scope"
        )
        tasks = []
        for role, names in by_role.items():
            risks = [self.specs[name].risk_class for name in names]
            risk = RiskClass.ACTIVE_SCAN if RiskClass.ACTIVE_SCAN in risks else risks[0]
            tasks.append(
                PlannedTask(
                    role=role,
                    title=f"Run bounded {role.value.replace('_', ' ')} analysis",
                    instructions=(
                        f"Objective: {objective}\n"
                        f"Available tools: {', '.join(names)}\n"
                        f"Hard scope: {scope_summary}\n"
                        "Use at most one tool call. Do not invent targets, flags, or "
                        "capabilities outside the supplied schemas."
                    ),
                    delegation_depth=1,
                    risk_class=risk,
                )
            )
        return MissionPlan(
            summary="Execute the operator-selected Safe Foundation specialists",
            rationale=(
                "The Core generated this graph deterministically from the exact "
                "tool lock; models cannot add roles or tools."
            ),
            tasks=tasks,
        )

    async def synthesize(
        self,
        objective: str,
        plan: MissionPlan,
        results: Mapping[str, SpecialistResult],
    ) -> str:
        del plan
        summaries = [result.summary for result in results.values() if result.summary]
        if not summaries:
            return f"{objective}: no specialist produced a result"
        return f"{objective}: " + "; ".join(summaries)


class BrokeredToolSpecialist:
    """Expose strict tool schemas and execute one durable brokered call per task."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        role: SpecialistRole,
        broker: ToolBroker,
        scope: ScopePolicy,
        workspace: Path,
        specs: Mapping[str, ToolSpec],
        model: str | None = None,
        max_output_tokens: int = 2048,
    ) -> None:
        if not provider.config.enabled:
            raise MissionError(f"provider {provider.config.id!r} is disabled")
        role_specs = {
            name: spec for name, spec in specs.items() if role_for_tool(name) == role
        }
        if not role_specs:
            raise MissionError(f"no tools are assigned to specialist {role.value}")
        self.provider = provider
        self.role = role
        self.broker = broker
        self.scope = scope
        self.workspace = workspace.expanduser().resolve()
        self.specs = role_specs
        self.allowed_tools = frozenset(role_specs)
        self.model = model
        self.max_output_tokens = max_output_tokens

    async def run(self, context: SpecialistContext) -> SpecialistResult:
        allowed = self.allowed_tools & context.allowed_tools
        if not allowed:
            raise MissionError(
                f"{self.role.value} has no tools within the mission lock"
            )

        first_usage = (0, 0)
        if context.approval_response:
            invocation, model_call_id = await self._approved_invocation(context)
        else:
            response = await self.provider.complete(
                ModelRequest(
                    model=self.model,
                    instructions=(
                        "You are a bounded Nebula security specialist. You may request "
                        "exactly one supplied tool. Use only explicit in-scope targets "
                        "present in the task. Never construct shell commands or add "
                        "undeclared arguments. If no tool is necessary, explain why."
                    ),
                    messages=[
                        ModelMessage(
                            role="user",
                            content=(
                                f"Mission objective: {context.objective}\n"
                                f"Task: {context.task.title}\n"
                                f"Instructions: {context.task.instructions}\n"
                                f"Prior results: {self._prior(context)}"
                            ),
                        )
                    ],
                    tools=[
                        self._definition(self.specs[name]) for name in sorted(allowed)
                    ],
                    parallel_tool_calls=False,
                    max_output_tokens=self.max_output_tokens,
                    metadata=self._metadata(context),
                )
            )
            first_usage = (
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            if not response.tool_calls:
                summary = response.text.strip()
                if not summary:
                    raise MissionError(
                        "specialist returned neither analysis nor a tool call"
                    )
                return SpecialistResult(
                    summary=summary,
                    rationale="specialist declined executable action; no tool was called",
                    input_tokens=first_usage[0],
                    output_tokens=first_usage[1],
                    cost_usd=self._cost(*first_usage),
                )
            if len(response.tool_calls) != 1:
                raise MissionError(
                    "specialists may request exactly one tool call per task"
                )
            call = response.tool_calls[0]
            if call.name not in allowed:
                raise MissionError(f"model requested unavailable tool {call.name!r}")
            invocation_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"nebula:model-tool:{context.run_id}:{context.task.id}:{call.id}",
                )
            )
            invocation = ToolInvocation(
                id=invocation_id,
                engagement_id=context.engagement_id,
                run_id=context.run_id,
                task_id=context.task.id,
                tool_name=call.name,
                arguments=call.arguments,
                workspace=self.workspace,
                idempotency_key=f"task:{context.task.id}:model-call:{call.id}",
                requested_by=self.role.value,
            )
            model_call_id = call.id

        try:
            result = await self.broker.execute(invocation, self.scope)
        except PolicyDenied as denial:
            return SpecialistResult(
                summary=(
                    f"{invocation.tool_name} was not executed: {denial.decision.reason}"
                ),
                rationale=(
                    "The durable policy or operator decision denied the exact "
                    "request; no executable capability was invoked."
                ),
                output={
                    "tool": invocation.tool_name,
                    "denied": True,
                    "reason": denial.decision.reason,
                    "rule": denial.decision.rule,
                },
                input_tokens=first_usage[0],
                output_tokens=first_usage[1],
                cost_usd=self._cost(*first_usage),
                tool_calls=1,
            )
        summary_response = await self.provider.complete(
            ModelRequest(
                model=self.model,
                instructions=(
                    "Summarize the supplied parsed tool result for an analyst. State "
                    "limitations and do not claim a finding is confirmed. Do not call tools."
                ),
                messages=[
                    ModelMessage(
                        role="user",
                        content=(
                            f"Objective: {context.objective}\n"
                            f"Tool: {invocation.tool_name}\n"
                            f"Parsed result: {json.dumps(result.output, sort_keys=True)}"
                        ),
                    )
                ],
                max_output_tokens=self.max_output_tokens,
                metadata=self._metadata(context),
            )
        )
        if summary_response.tool_calls:
            raise MissionError(
                "post-tool synthesis attempted an unauthorized tool call"
            )
        summary = summary_response.text.strip() or f"{invocation.tool_name} completed"
        total_input = first_usage[0] + summary_response.usage.input_tokens
        total_output = first_usage[1] + summary_response.usage.output_tokens
        command = result.execution.get("command")
        reproducible = [" ".join(command)] if isinstance(command, list) else []
        return SpecialistResult(
            summary=summary,
            rationale=(
                f"brokered {invocation.tool_name} call {model_call_id}; output remains "
                "an observation pending independent verification"
            ),
            output={
                "tool": invocation.tool_name,
                "parsed": result.output,
                "exit_code": result.exit_code,
                "output_truncated": result.output_truncated,
            },
            evidence_ids=result.evidence_ids,
            reproducible_steps=reproducible,
            input_tokens=total_input,
            output_tokens=total_output,
            cost_usd=self._cost(total_input, total_output),
            tool_calls=1,
        )

    async def _approved_invocation(
        self, context: SpecialistContext
    ) -> tuple[ToolInvocation, str]:
        response = context.approval_response or {}
        approval_id = response.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise MissionError("approval resume is missing its durable approval id")
        approval: Approval = await self.broker.ledger.get_approval(approval_id)
        if approval.run_id != context.run_id or approval.task_id != context.task.id:
            raise MissionError("approval does not belong to this specialist task")
        exact = approval.exact_request
        tool_name = exact.get("tool_name")
        arguments = exact.get("arguments")
        if tool_name not in self.allowed_tools or not isinstance(arguments, dict):
            raise MissionError("approval contains an invalid tool request")
        if not approval.tool_call_id:
            raise MissionError("approval is not linked to a durable tool call")
        return (
            ToolInvocation(
                id=approval.tool_call_id,
                engagement_id=context.engagement_id,
                run_id=context.run_id,
                task_id=context.task.id,
                tool_name=tool_name,
                arguments=arguments,
                workspace=self.workspace,
                requested_by=self.role.value,
            ),
            approval.tool_call_id,
        )

    @staticmethod
    def _definition(spec: ToolSpec) -> ToolDefinition:
        return ToolDefinition(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            strict=True,
        )

    @staticmethod
    def _prior(context: SpecialistContext) -> dict[str, str]:
        return {key: result.summary for key, result in context.prior_results.items()}

    def _metadata(self, context: SpecialistContext) -> dict[str, str]:
        return {
            "engagement_id": context.engagement_id,
            "run_id": context.run_id,
            "task_id": context.task.id,
            "specialist_role": self.role.value,
        }

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        input_rate = float(
            self.provider.config.options.get("input_cost_per_million", 0)
        )
        output_rate = float(
            self.provider.config.options.get("output_cost_per_million", 0)
        )
        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


__all__ = [
    "BrokeredToolSpecialist",
    "ToolMissionSupervisor",
    "role_for_tool",
]
