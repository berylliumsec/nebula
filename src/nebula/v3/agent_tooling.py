"""Bounded model specialists that can use only brokered tool capabilities."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .domain import Approval, ChatTokenUsage, RiskClass, RunBudget, ScopePolicy
from .orchestration import (
    MissionError,
    MissionPlan,
    PlannedTask,
    SpecialistApprovalRequired,
    SpecialistContext,
    SpecialistOutcome,
    SpecialistResult,
    SpecialistRole,
)
from .providers import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelToolResult,
    ToolChoice,
    ToolDefinition,
)
from .redaction import redact_text
from .tool_interfaces import ToolInterfaceCatalog, ToolInterfaceError
from .tools import ApprovalRequired, PolicyDenied, ToolBroker, ToolInvocation, ToolSpec


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

        role_groups: dict[SpecialistRole, list[str]] = {}
        for name in selected:
            role_groups.setdefault(role_for_tool(name), []).append(name)
        if len(role_groups) > budget.max_concurrency * 8:
            raise MissionError("selected tool set produces an excessive task graph")

        scope_summary = context.get(
            "scope_summary", "operator-approved engagement scope"
        )
        tasks: list[PlannedTask] = []
        previous_stage: list[str] = []
        for role, names in role_groups.items():
            risks = [self.specs[name].risk_class for name in names]
            risk = max(risks, key=_RISK_PRIORITY.__getitem__)
            task = PlannedTask(
                role=role,
                title=f"Use Toolbox capability {', '.join(names)}",
                instructions=(
                    f"Objective: {objective}\n"
                    f"Capabilities available to this specialist: {', '.join(names)}\n"
                    f"Hard scope: {scope_summary}\n"
                    "Investigate iteratively until the objective is satisfied or a "
                    "specific blocker is proven. Alternate among search, help, and "
                    "execution capabilities when that helps diagnose a failed call. "
                    "For network execution, use {target} in the command argument list "
                    "so the broker-pinned target is the one the program receives. "
                    "Never repeat a failed call unchanged, and never invent targets or "
                    "capabilities outside the supplied schemas."
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
        sections = ["## Summary", objective]
        task_by_id = {task.id: task for task in plan.tasks}
        rendered_results = 0
        for task_id, result in results.items():
            if not result.summary and not result.reproducible_steps:
                continue
            rendered_results += 1
            task = task_by_id.get(task_id)
            sections.extend(
                [
                    f"### {task.title if task else 'Specialist result'}",
                    result.summary.strip(),
                ]
            )
            if result.reproducible_steps:
                sections.extend(
                    [
                        "**Commands used**",
                        "```bash\n" + "\n".join(result.reproducible_steps) + "\n```",
                    ]
                )
            if result.evidence_ids:
                sections.append("**Evidence:** " + ", ".join(result.evidence_ids))
        if not rendered_results:
            sections.append("No specialist produced a result.")
        return "\n\n".join(section for section in sections if section)


class BrokeredToolSpecialist:
    """Execute one durable investigative action per bounded specialist turn."""

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
        if context.remaining_tool_calls <= 0:
            allowed = frozenset()
        if context.approval_response:
            if context.remaining_tool_calls <= 0:
                return SpecialistResult(
                    summary="The approved operation cannot run because the mission "
                    "tool-call budget is exhausted.",
                    rationale="No brokered capability slots remain after approval.",
                    outcome=SpecialistOutcome.BLOCKED,
                    output={"status": "blocked", "observations": []},
                )
            invocation, model_call_id = await self._approved_invocation(context)
            return await self._execute_invocation(
                context,
                invocation,
                model_call_id=model_call_id,
                usage=(0, 0),
            )

        response = await self.provider.complete(self._routing_request(context, allowed))
        usage = (response.usage.input_tokens, response.usage.output_tokens)
        call = self._one_routing_call(response)
        if call.name == "nebula.finish_task":
            return self._finish_result(context, call.arguments, usage)

        selected_interface: dict[str, Any] | None = None
        if call.name == "nebula.select_environment_command":
            if self.interface_catalog is None:
                raise MissionError(
                    "specialist requested an unavailable interface selector"
                )
            selection = self._validate_interface_selection(call.arguments)
            mode = selection["mode"]
            interface_context: str
            if mode == "structured":
                try:
                    selected_interface = self.interface_catalog.command(
                        selection["tool"], selection["command_path"]
                    )
                except ToolInterfaceError as exc:
                    raise MissionError(str(exc)) from exc
                selected_allowed = frozenset(
                    name for name in allowed if name.startswith("environment.run_")
                )
                interface_context = (
                    "The Core selected and injected this exact interface. Use the "
                    "same tool and command_path in the structured invocation; do not "
                    "invent option IDs or positional IDs:\n"
                    f"{json.dumps(selected_interface, sort_keys=True)}"
                )
            else:
                selected_allowed = frozenset(
                    name for name in allowed if name.startswith("environment.shell_")
                )
                interface_context = (
                    "Use the full command-line fallback inside the Toolbox container. "
                    "Inspect availability and exact syntax with command -v, --version, "
                    f"and --help when useful. Intended command: {selection['tool']}"
                )
            if not selected_allowed:
                raise MissionError(
                    f"the mission did not grant a capability for {mode} execution"
                )
            execution_response = await self.provider.complete(
                self._execution_request(
                    context,
                    selected_allowed,
                    interface_context=interface_context,
                )
            )
            usage = (
                usage[0] + execution_response.usage.input_tokens,
                usage[1] + execution_response.usage.output_tokens,
            )
            call = self._one_routing_call(execution_response)
            if call.name not in selected_allowed:
                raise MissionError(f"model requested unavailable tool {call.name!r}")
        elif call.name not in allowed:
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
                (
                    f"nebula:model-tool:{context.run_id}:{context.task.id}:"
                    f"{context.turn_index}:{call.id}"
                ),
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
            idempotency_key=(
                f"task:{context.task.id}:turn:{context.turn_index}:model-call:{call.id}"
            ),
            requested_by=self.role.value,
        )
        return await self._execute_invocation(
            context,
            invocation,
            model_call_id=call.id,
            usage=usage,
        )

    def _routing_request(
        self, context: SpecialistContext, allowed: frozenset[str]
    ) -> ModelRequest:
        run_tools = frozenset(
            name
            for name in allowed
            if name.startswith(("environment.run_", "environment.shell_"))
        )
        direct = allowed - run_tools
        tools = [self._definition(self.specs[name]) for name in sorted(direct)]
        if run_tools and self.interface_catalog is not None:
            tools.append(self._interface_selector())
        else:
            tools.extend(
                self._definition(self.specs[name]) for name in sorted(run_tools)
            )
        tools.append(self._finish_tool())
        return ModelRequest(
            model=self.model,
            instructions=self._routing_instructions(context),
            messages=[ModelMessage(role="user", content=self._prompt(context))],
            tools=tools,
            tool_choice=ToolChoice.REQUIRED,
            parallel_tool_calls=False,
            tool_results=self._provider_tool_history(context),
            max_output_tokens=self.max_output_tokens,
            metadata=self._metadata(context),
        )

    def _execution_request(
        self,
        context: SpecialistContext,
        allowed: frozenset[str],
        *,
        interface_context: str,
    ) -> ModelRequest:
        return ModelRequest(
            model=self.model,
            instructions=(
                self._routing_instructions(context)
                + "\nThe prior routing step selected execution. Request exactly one "
                "of the supplied execution capabilities.\n" + interface_context
            ),
            messages=[ModelMessage(role="user", content=self._prompt(context))],
            tools=[self._definition(self.specs[name]) for name in sorted(allowed)],
            tool_choice=ToolChoice.REQUIRED,
            parallel_tool_calls=False,
            tool_results=self._provider_tool_history(context),
            max_output_tokens=self.max_output_tokens,
            metadata=self._metadata(context),
        )

    async def _execute_invocation(
        self,
        context: SpecialistContext,
        invocation: ToolInvocation,
        *,
        model_call_id: str,
        usage: tuple[int, int],
    ) -> SpecialistResult:
        spec = self.specs[invocation.tool_name]
        arguments = dict(invocation.arguments)
        if "cwd" in spec.path_arguments:
            arguments["cwd"] = "."
        if invocation.tool_name == "environment.help" and self.interface_catalog:
            tool_name = arguments.get("tool")
            command_path = arguments.get("command_path")
            tool = (
                self.interface_catalog.tools.get(tool_name)
                if isinstance(tool_name, str)
                else None
            )
            if tool is not None and isinstance(command_path, list):
                command_paths = [command["path"] for command in tool["commands"]]
                if command_path not in command_paths and len(command_paths) == 1:
                    arguments["command_path"] = command_paths[0]
        invocation = invocation.model_copy(update={"arguments": arguments})
        self._reject_unchanged_failed_invocation(
            context, invocation.tool_name, invocation.arguments
        )

        try:
            result = await self.broker.execute(invocation, self.scope)
        except ApprovalRequired as exc:
            raise SpecialistApprovalRequired(
                exc.approval,
                usage=ChatTokenUsage(
                    input_tokens=usage[0],
                    output_tokens=usage[1],
                    total_tokens=usage[0] + usage[1],
                ),
                cost_usd=self._cost(*usage),
            ) from exc
        except PolicyDenied as denial:
            status = "denied"
            provider_result: dict[str, Any] | str = {
                "status": status,
                "detail": self._safe_text(denial.decision.reason),
                "rule": denial.decision.rule,
            }
            summary = (
                f"{invocation.tool_name} was denied: "
                f"{self._safe_text(denial.decision.reason)}"
            )
            evidence_ids: list[str] = []
            reproducible: list[str] = []
            exit_code = None
            output_truncated = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = "failed"
            detail = self._safe_text(f"{type(exc).__name__}: {exc}")
            provider_result = {"status": status, "detail": detail}
            summary = f"{invocation.tool_name} failed: {detail}"
            evidence_ids = []
            reproducible = []
            exit_code = None
            output_truncated = False
        else:
            failed = self._tool_result_failed(result)
            broker_status = (
                "failed"
                if failed
                else "incomplete"
                if result.output_truncated
                else "complete"
            )
            provider_result = self._bounded_tool_result(
                {
                    "status": broker_status,
                    "output": result.output,
                    "exit_code": result.exit_code,
                    "output_truncated": result.output_truncated,
                    "timed_out": result.execution.get("timed_out") is True,
                    "parser_error": result.parser_error,
                    "stderr": result.stderr,
                }
            )
            bounded_result_truncated = bool(
                json.loads(provider_result).get("truncated") is True
            )
            output_truncated = result.output_truncated or bounded_result_truncated
            status = (
                "failed" if failed else "incomplete" if output_truncated else "complete"
            )
            summary = self._result_summary(invocation.tool_name, result, status)
            evidence_ids = result.evidence_ids
            command = result.execution.get("command")
            reproducible = (
                [shlex.join(str(part) for part in command)]
                if isinstance(command, list)
                else []
            )
            parsed_exit = result.output.get("exit_code")
            exit_code = (
                parsed_exit
                if isinstance(parsed_exit, int) and not isinstance(parsed_exit, bool)
                else result.exit_code
            )

        return SpecialistResult(
            summary=summary,
            rationale=(
                f"brokered {invocation.tool_name} call {model_call_id}; the next "
                "turn must inspect this observation before finishing"
            ),
            outcome=SpecialistOutcome.CONTINUE,
            output={
                "model_call_id": model_call_id,
                "tool": invocation.tool_name,
                "arguments": invocation.arguments,
                "status": status,
                "provider_result": provider_result,
                "exit_code": exit_code,
                "output_truncated": output_truncated,
            },
            evidence_ids=evidence_ids,
            reproducible_steps=reproducible,
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=self._cost(*usage),
            tool_calls=1,
        )

    @staticmethod
    def _interface_selector() -> ToolDefinition:
        return ToolDefinition(
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

    @staticmethod
    def _validate_interface_selection(selection: dict[str, Any]) -> dict[str, Any]:
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
        return selection

    @staticmethod
    def _finish_tool() -> ToolDefinition:
        return ToolDefinition(
            name="nebula.finish_task",
            description=(
                "Finish this specialist task only when its objective is satisfied or "
                "a specific blocker prevents further progress."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["complete", "blocked"],
                    },
                    "summary": {"type": "string", "minLength": 1, "maxLength": 8_000},
                    "rationale": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_000,
                    },
                },
                "required": ["status", "summary", "rationale"],
                "additionalProperties": False,
            },
            strict=True,
        )

    def _finish_result(
        self,
        context: SpecialistContext,
        arguments: dict[str, Any],
        usage: tuple[int, int],
    ) -> SpecialistResult:
        if set(arguments) != {"status", "summary", "rationale"}:
            raise MissionError("finish_task returned invalid fields")
        status = arguments.get("status")
        summary = arguments.get("summary")
        rationale = arguments.get("rationale")
        if status not in {"complete", "blocked"}:
            raise MissionError("finish_task returned an invalid status")
        if not isinstance(summary, str) or not summary.strip():
            raise MissionError("finish_task requires a non-empty summary")
        if not isinstance(rationale, str) or not rationale.strip():
            raise MissionError("finish_task requires a non-empty rationale")

        tool_turns = [
            turn
            for turn in context.prior_turns
            if isinstance(turn.output.get("model_call_id"), str)
        ]
        if status == "complete" and tool_turns:
            last_status = tool_turns[-1].output.get("status")
            if last_status in {"failed", "denied", "incomplete"}:
                raise MissionError(
                    "cannot complete while the latest tool result is unresolved; "
                    "make a corrected call or finish as blocked"
                )

        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for turn in context.prior_turns
                for evidence_id in turn.evidence_ids
            )
        )
        reproducible_steps = list(
            dict.fromkeys(
                step for turn in context.prior_turns for step in turn.reproducible_steps
            )
        )
        candidate_finding_ids = list(
            dict.fromkeys(
                finding_id
                for turn in context.prior_turns
                for finding_id in turn.candidate_finding_ids
            )
        )
        observations = [
            {
                "tool": turn.output.get("tool"),
                "status": turn.output.get("status"),
                "summary": turn.summary,
                "evidence_ids": turn.evidence_ids,
            }
            for turn in context.prior_turns
            if turn.output.get("tool")
        ]
        return SpecialistResult(
            summary=summary.strip(),
            rationale=rationale.strip(),
            outcome=(
                SpecialistOutcome.COMPLETE
                if status == "complete"
                else SpecialistOutcome.BLOCKED
            ),
            output={"status": status, "observations": observations},
            evidence_ids=evidence_ids,
            reproducible_steps=reproducible_steps,
            candidate_finding_ids=candidate_finding_ids,
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=self._cost(*usage),
        )

    @staticmethod
    def _one_routing_call(response: Any) -> Any:
        if response.text.strip():
            raise MissionError("specialist returned prose during required routing")
        if len(response.tool_calls) != 1:
            raise MissionError(
                "specialist must request exactly one sequential routing action"
            )
        return response.tool_calls[0]

    def _routing_instructions(self, context: SpecialistContext) -> str:
        budget_note = (
            "No real tool-call slots remain. You must finish as complete only if the "
            "objective is already satisfied; otherwise finish as blocked."
            if context.remaining_tool_calls <= 0
            else (
                f"At most {context.remaining_tool_calls} real tool-call slots remain."
            )
        )
        return (
            "You are a Nebula security specialist working through sequential, durable "
            "turns inside a disposable Toolbox container. Call exactly one supplied "
            "routing action and return no prose. Use a real capability when it advances "
            "the task. After a denial, timeout, truncation, nonzero exit, or other "
            "failure, inspect the exact result and make a specific changed call when a "
            "safe corrective path exists; never repeat the same failed arguments "
            "unchanged. Use search or help before guessing unfamiliar syntax. Call "
            "nebula.finish_task with status=complete only when the objective is met. "
            "Use status=blocked when policy, missing capability, or exhausted budget "
            "prevents further progress. Use only explicit in-scope targets. Full Bash "
            "is available only through the supplied shell capabilities and never runs "
            f"on the host. {budget_note}"
        )

    def _prompt(self, context: SpecialistContext) -> str:
        earlier = context.prior_turns[:-8]
        earlier_summaries = [
            {
                "turn": index + 1,
                "summary": turn.summary[:500],
                "status": turn.output.get("status"),
                "tool": turn.output.get("tool"),
            }
            for index, turn in enumerate(earlier)
        ]
        parts = [
            f"Mission objective: {context.objective}",
            f"Task: {context.task.title}",
            f"Instructions: {context.task.instructions}",
            f"Prior dependency results: {self._prior(context)}",
        ]
        if context.retry_errors:
            parts.append(
                "Prior runtime/verification feedback: "
                + json.dumps(context.retry_errors[-5:], ensure_ascii=False)
            )
        if earlier_summaries:
            parts.append(
                "Earlier bounded turn summaries: "
                + json.dumps(earlier_summaries, ensure_ascii=False, sort_keys=True)
            )
        if self.interface_catalog is not None:
            parts.append(
                "Exact-version Toolbox index: "
                + json.dumps(
                    self.interface_catalog.compact_index(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return "\n".join(parts)

    @staticmethod
    def _provider_tool_history(
        context: SpecialistContext,
    ) -> list[ModelToolResult]:
        history: list[ModelToolResult] = []
        selected_indexes = set(
            range(max(0, len(context.prior_turns) - 8), len(context.prior_turns))
        )
        for index in range(len(context.prior_turns) - 1, -1, -1):
            if context.prior_turns[index].output.get("status") in {
                "failed",
                "denied",
                "incomplete",
            }:
                selected_indexes.add(index)
                break
        for index in sorted(selected_indexes):
            turn = context.prior_turns[index]
            call_id = turn.output.get("model_call_id")
            tool_name = turn.output.get("tool")
            provider_result = turn.output.get("provider_result")
            if (
                not isinstance(call_id, str)
                or not isinstance(tool_name, str)
                or not isinstance(provider_result, (dict, str))
            ):
                continue
            arguments = turn.output.get("arguments")
            history.append(
                ModelToolResult(
                    call_id=call_id,
                    name=tool_name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    output=provider_result,
                    is_error=turn.output.get("status") != "complete",
                )
            )
        return history

    @staticmethod
    def _tool_result_failed(result: Any) -> bool:
        if result.parser_error:
            return True
        if result.exit_code not in {None, 0}:
            return True
        output_exit = result.output.get("exit_code")
        if (
            isinstance(output_exit, int)
            and not isinstance(output_exit, bool)
            and output_exit != 0
        ):
            return True
        if result.execution.get("timed_out") is True:
            return True
        return result.output.get("timed_out") is True

    @classmethod
    def _bounded_tool_result(cls, output: dict[str, Any]) -> str:
        normalized = json.loads(json.dumps(output, ensure_ascii=False, default=str))

        def redact_value(value: Any) -> Any:
            if isinstance(value, str):
                return redact_text(value)
            if isinstance(value, list):
                return [redact_value(item) for item in value]
            if isinstance(value, dict):
                return {str(key): redact_value(item) for key, item in value.items()}
            return value

        rendered = json.dumps(
            redact_value(normalized), ensure_ascii=False, sort_keys=True
        )
        limit = 8_000
        if len(rendered) <= limit:
            return rendered
        envelope = {
            "status": "incomplete",
            "truncated": True,
            "original_characters": len(rendered),
            "preview": rendered[:6_000],
        }
        bounded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        while len(bounded) > limit and envelope["preview"]:
            envelope["preview"] = envelope["preview"][:-256]
            bounded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        return bounded

    @classmethod
    def _result_summary(cls, tool_name: str, result: Any, status: str) -> str:
        if status == "failed":
            if result.parser_error:
                return (
                    f"{tool_name} output parsing failed: "
                    f"{cls._safe_text(result.parser_error)[:320]}"
                )
            detail = cls._safe_text(result.stderr or result.stdout or "")[:320]
            if not detail:
                detail = cls._safe_text(
                    str(
                        result.output.get("stderr") or result.output.get("stdout") or ""
                    )
                )[:320]
            suffix = f": {detail}" if detail else ""
            if (
                result.execution.get("timed_out") is True
                or result.output.get("timed_out") is True
            ):
                return f"{tool_name} timed out{suffix}"
            exit_code = result.exit_code
            output_exit = result.output.get("exit_code")
            if isinstance(output_exit, int) and not isinstance(output_exit, bool):
                exit_code = output_exit
            return f"{tool_name} failed with exit code {exit_code}{suffix}"
        if status == "incomplete":
            return f"{tool_name} completed with truncated output"
        return f"{tool_name} completed successfully"

    @staticmethod
    def _reject_unchanged_failed_invocation(
        context: SpecialistContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        for turn in reversed(context.prior_turns):
            if turn.output.get("status") not in {"failed", "denied", "incomplete"}:
                continue
            if (
                turn.output.get("tool") == tool_name
                and turn.output.get("arguments") == arguments
            ):
                raise MissionError(
                    "the model repeated a failed invocation unchanged; inspect the "
                    "prior error and change the tool or arguments"
                )

    @staticmethod
    def _safe_text(value: str) -> str:
        return redact_text(re.sub(r"\s+", " ", str(value))).strip()[:1_000]

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
        input_schema = deepcopy(spec.input_schema)
        properties = input_schema.get("properties")
        if "cwd" in spec.path_arguments and isinstance(properties, dict):
            properties["cwd"] = {
                "type": "string",
                "const": ".",
                "description": "Engagement workspace root; supplied by Nebula Core.",
            }
        return ToolDefinition(
            name=spec.name,
            description=spec.description,
            input_schema=input_schema,
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
            "agent_turn": str(context.turn_index),
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
