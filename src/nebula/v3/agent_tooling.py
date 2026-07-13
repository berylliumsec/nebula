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
from .tool_interfaces import ToolInterfaceCatalog, ToolInterfaceError
from .tools import PolicyDenied, ToolBroker, ToolInvocation, ToolSpec


_ROLE_BY_PREFIX: tuple[tuple[str, SpecialistRole], ...] = (
    ("environment.", SpecialistRole.NETWORK_SERVICE),
    ("nmap.", SpecialistRole.NETWORK_SERVICE),
    ("nuclei.", SpecialistRole.WEB_API),
    ("nikto.", SpecialistRole.WEB_API),
    ("searchsploit.", SpecialistRole.VULNERABILITY_INTELLIGENCE),
    ("semgrep.", SpecialistRole.CODE_ANALYSIS),
)
_RISK_PRIORITY = {
    RiskClass.LOCAL_READ: 0,
    RiskClass.WORKSPACE_WRITE: 1,
    RiskClass.PASSIVE: 2,
    RiskClass.ACTIVE_SCAN: 3,
    RiskClass.CREDENTIAL_USE: 4,
    RiskClass.EXPLOITATION: 5,
    RiskClass.PERSISTENCE: 6,
    RiskClass.DESTRUCTIVE: 7,
}


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

        stages = [
            [name for name in selected if name == "environment.search"],
            [name for name in selected if name == "environment.help"],
            [
                name
                for name in selected
                if name.startswith(("environment.run_", "environment.shell_"))
            ],
        ]
        staged = {name for names in stages for name in names}
        stages.extend([[name] for name in selected if name not in staged])
        stages = [names for names in stages if names]
        if len(stages) > budget.max_concurrency * 8:
            raise MissionError("selected tool set produces an excessive task graph")

        scope_summary = context.get(
            "scope_summary", "operator-approved engagement scope"
        )
        tasks: list[PlannedTask] = []
        previous_stage: list[str] = []
        for names in stages:
            role = role_for_tool(names[0])
            risks = [self.specs[name].risk_class for name in names]
            risk = max(risks, key=_RISK_PRIORITY.__getitem__)
            task = PlannedTask(
                role=role,
                title=f"Use Toolbox capability {', '.join(names)}",
                instructions=(
                    f"Objective: {objective}\n"
                    f"Capability for this stage: {', '.join(names)}\n"
                    f"Hard scope: {scope_summary}\n"
                    "Use exactly one capability named for this stage when it helps. "
                    "For network execution, use {target} in the command argument list "
                    "so the broker-pinned target is the one the program receives. "
                    "Never invent targets or capabilities outside the supplied schemas."
                ),
                depends_on=previous_stage,
                delegation_depth=1,
                risk_class=risk,
                allowed_tools=frozenset(names),
            )
            tasks.append(task)
            previous_stage = [task.id]
        return MissionPlan(
            summary="Discover and execute tools in the selected Toolbox environment",
            rationale=(
                "The Core generated this graph deterministically from the exact "
                "environment lock; models cannot add roles or container images."
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
    """Expose strict environment schemas and execute one brokered call per task."""

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
        interface_catalog: ToolInterfaceCatalog | None = None,
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
        self.interface_catalog = interface_catalog

    async def run(self, context: SpecialistContext) -> SpecialistResult:
        allowed = self.allowed_tools & context.allowed_tools
        if context.task.allowed_tools is not None:
            allowed &= context.task.allowed_tools
        if not allowed:
            raise MissionError(
                f"{self.role.value} has no tools within the mission lock"
            )

        first_usage = (0, 0)
        if context.approval_response:
            invocation, model_call_id = await self._approved_invocation(context)
        else:
            interface_context = ""
            selected_interface: dict[str, Any] | None = None
            if self.interface_catalog is not None and any(
                name.startswith(("environment.run_", "environment.shell_"))
                for name in allowed
            ):
                selection, selection_usage = await self._select_interface(context)
                first_usage = selection_usage
                mode = selection["mode"]
                if mode == "structured":
                    try:
                        selected_interface = self.interface_catalog.command(
                            selection["tool"], selection["command_path"]
                        )
                    except ToolInterfaceError as exc:
                        raise MissionError(str(exc)) from exc
                    allowed = frozenset(
                        name for name in allowed if name.startswith("environment.run_")
                    )
                    interface_context = (
                        "The Core selected and injected this exact interface. Use the "
                        "same tool and command_path in the structured invocation; do not "
                        "invent option IDs or positional IDs:\n"
                        f"{json.dumps(selected_interface, sort_keys=True)}"
                    )
                else:
                    allowed = frozenset(
                        name
                        for name in allowed
                        if name.startswith("environment.shell_")
                    )
                    interface_context = (
                        "Use the full command-line fallback inside the Toolbox container. "
                        "The requested command is not constrained by the catalog. Inspect "
                        "availability and exact syntax with command -v, --version, and "
                        f"--help when useful. Intended command: {selection['tool']}"
                    )
                if not allowed:
                    raise MissionError(
                        f"the mission did not grant a capability for {mode} execution"
                    )
            response = await self.provider.complete(
                ModelRequest(
                    model=self.model,
                    instructions=(
                        "You are a Nebula security specialist working inside a disposable "
                        "Toolbox container. Request at most one supplied capability for "
                        "this stage. Search the environment before selecting unfamiliar "
                        "commands. Use only explicit in-scope targets present in the task. "
                        "Full Bash is allowed only through environment.shell_local or "
                        "environment.shell_network; it still runs inside the disposable "
                        "container and never on the host. If no "
                        "execution is necessary, explain why.\n"
                        f"{interface_context}"
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
                first_usage[0] + response.usage.input_tokens,
                first_usage[1] + response.usage.output_tokens,
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
            if selected_interface is not None:
                requested_tool = call.arguments.get("tool")
                invocation_payload = call.arguments.get("invocation")
                if requested_tool != selected_interface["tool"]["name"]:
                    raise MissionError(
                        "model changed the catalogued tool after interface selection"
                    )
                if (
                    not isinstance(invocation_payload, dict)
                    or invocation_payload.get("command_path")
                    != selected_interface["command"]["path"]
                ):
                    raise MissionError(
                        "model changed the command path after interface selection"
                    )
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

    async def _select_interface(
        self, context: SpecialistContext
    ) -> tuple[dict[str, Any], tuple[int, int]]:
        assert self.interface_catalog is not None
        selection_tool = ToolDefinition(
            name="nebula.select_environment_command",
            description=(
                "Select whether to use one exact catalogued command interface or the "
                "full container command-line fallback. This is planning only and does "
                "not execute a tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["structured", "shell"]},
                    "tool": {"type": "string", "minLength": 1, "maxLength": 200},
                    "command_path": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string"},
                    },
                    "rationale": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "required": ["mode", "tool", "command_path", "rationale"],
                "additionalProperties": False,
            },
            strict=True,
        )
        response = await self.provider.complete(
            ModelRequest(
                model=self.model,
                instructions=(
                    "Choose one command interface for the task. Prefer a catalogued "
                    "structured interface. Choose shell only for an uncatalogued command "
                    "or a pipeline/workflow that needs shell syntax. Make exactly one "
                    "selection tool call; it does not execute anything."
                ),
                messages=[
                    ModelMessage(
                        role="user",
                        content=(
                            f"Objective: {context.objective}\n"
                            f"Task: {context.task.instructions}\n"
                            "Exact-version Toolbox index: "
                            f"{json.dumps(self.interface_catalog.compact_index(), sort_keys=True)}"
                        ),
                    )
                ],
                tools=[selection_tool],
                parallel_tool_calls=False,
                max_output_tokens=min(self.max_output_tokens, 1024),
                metadata=self._metadata(context),
            )
        )
        if len(response.tool_calls) != 1:
            raise MissionError(
                "specialist did not make exactly one interface selection"
            )
        call = response.tool_calls[0]
        if call.name != selection_tool.name:
            raise MissionError("specialist requested an invalid interface selector")
        selection = call.arguments
        if set(selection) != {"mode", "tool", "command_path", "rationale"}:
            raise MissionError("interface selection has invalid fields")
        if selection["mode"] not in {"structured", "shell"}:
            raise MissionError("interface selection has an invalid mode")
        if not isinstance(selection["tool"], str) or not selection["tool"]:
            raise MissionError("interface selection is missing a tool name")
        if not isinstance(selection["command_path"], list) or any(
            not isinstance(item, str) for item in selection["command_path"]
        ):
            raise MissionError("interface selection has an invalid command path")
        return selection, (
            response.usage.input_tokens,
            response.usage.output_tokens,
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
    def _prior(context: SpecialistContext) -> str | dict[str, str]:
        return context.prior_context or {
            key: result.summary for key, result in context.prior_results.items()
        }

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
