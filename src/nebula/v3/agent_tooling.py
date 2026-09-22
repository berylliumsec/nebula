"""Bounded model specialists that can use only brokered tool capabilities."""

from __future__ import annotations

from .diagnostics import record_caught_exception, record_diagnostic

import asyncio
import json
import re
import shlex
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .context import DEFAULT_MAX_OUTPUT_TOKENS
from .domain import Approval, ChatTokenUsage, RiskClass, RunBudget, ScopePolicy
from .orchestration import (
    MissionError,
    call_records,
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
    ToolCall,
    ToolChoice,
    ToolDefinition,
    _GEMINI_SYNTHETIC_CALL_ID,
)
from .redaction import redact_text
from .tools import ApprovalRequired, InvalidToolArguments, PolicyDenied, ToolBroker, ToolInvocation, ToolSpec
from .tool_failures import tool_failure, unavailable_tool_failure
from .tool_results import (
    ToolResultStatus,
    sanitize_model_history_result,
    serialize_model_result,
)


_ROLE_BY_PREFIX: tuple[tuple[str, SpecialistRole], ...] = (
    ("run_command", SpecialistRole.NETWORK_SERVICE),
    ("process_io", SpecialistRole.NETWORK_SERVICE),
    ("mcp.", SpecialistRole.NETWORK_SERVICE),
    ("browser.", SpecialistRole.NETWORK_SERVICE),
    ("proxy.", SpecialistRole.NETWORK_SERVICE),
    ("environment.", SpecialistRole.NETWORK_SERVICE),
    ("nmap.", SpecialistRole.NETWORK_SERVICE),
    ("nuclei.", SpecialistRole.WEB_API),
    ("nikto.", SpecialistRole.WEB_API),
    ("searchsploit.", SpecialistRole.VULNERABILITY_INTELLIGENCE),
    ("semgrep.", SpecialistRole.CODE_ANALYSIS),
)
FINISH_TOOL = "nebula.finish_task"
_FINISH_FIELDS = frozenset({"status", "summary", "rationale"})
# Stop reasons that mean the response was cut off by the output-token limit,
# as the adapters report them (compared case-insensitively).
_OUTPUT_LIMIT_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
# Routing responses in a row that ran nothing before the task stops as blocked.
_MAX_CONSECUTIVE_ROUTING_DEVIATIONS = 3
# Record fields that let a later turn replay a routing response as the one
# message that issued it (see ``_with_replay_state``). They are for the
# provider that made the response, never for another task or model.
_REPLAY_FIELDS = ("response_group", "reasoning_state", "provider_metadata")
_NO_ACTION_FEEDBACK = (
    "Your previous response contained no routing action. Call one of the "
    f"supplied tools, or call {FINISH_TOOL} with status, summary and rationale "
    "once the task is done or blocked."
)


@dataclass(frozen=True)
class _RoutingAction:
    """One call of a routing response, and why Core answers it unrun, if it does."""

    call: ToolCall
    routing_error: str | None = None
    detail: str = ""
    # The id the provider sent, when Core replaced a reused one.
    provider_call_id: str | None = None


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
        explicit_stages = context.get("stages")
        if isinstance(explicit_stages, list) and explicit_stages:
            names = list(selected)
            risks = [self.specs[name].risk_class for name in names]
            risk = max(risks, key=_RISK_PRIORITY.__getitem__)
            for item in explicit_stages:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                stage_objective = str(item.get("objective", "")).strip()
                if not title or not stage_objective:
                    continue
                task = PlannedTask(
                    role=role_for_tool(names[0]),
                    title=title,
                    instructions=(
                        f"Overall objective: {objective}\n"
                        f"Current stage: {stage_objective}\n"
                        f"Capabilities available: {', '.join(names)}\n"
                        f"Hard scope: {scope_summary}"
                    ),
                    depends_on=previous_stage,
                    delegation_depth=1,
                    risk_class=risk,
                    allowed_tools=frozenset(names),
                )
                tasks.append(task)
                previous_stage = [task.id]
            if not tasks:
                raise MissionError("operator-authored mission stages are empty")
            return MissionPlan(
                summary="Execute the operator-authored mission stages in order",
                rationale="The Core preserved explicit stage boundaries while retaining the frozen tool contract.",
                tasks=tasks,
            )
        for role, names in role_groups.items():
            risks = [self.specs[name].risk_class for name in names]
            risk = max(risks, key=_RISK_PRIORITY.__getitem__)
            task = PlannedTask(
                role=role,
                title=f"Use command-runtime capability {', '.join(names)}",
                instructions=(
                    f"Objective: {objective}\n"
                    f"Capabilities available to this specialist: {', '.join(names)}\n"
                    f"Scope: {scope_summary}"
                ),
                depends_on=previous_stage,
                delegation_depth=1,
                risk_class=risk,
                allowed_tools=frozenset(names),
            )
            tasks.append(task)
            previous_stage = [task.id]
        return MissionPlan(
            summary="Run commands in the pinned project automation environment",
            rationale=(
                "The Core generated this graph from the fixed command contract and "
                "frozen runtime digest; models cannot add container images."
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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if not provider.config.enabled:
            raise MissionError(f"provider {provider.config.id!r} is disabled")
        role_specs = {
            name: spec
            for name, spec in specs.items()
            if spec.budget_class == "artifact_query" or role_for_tool(name) == role
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
        retrieval_tools = frozenset(
            name
            for name, spec in self.specs.items()
            if spec.budget_class == "artifact_query"
        )
        if context.task.allowed_tools is not None:
            allowed &= context.task.allowed_tools | retrieval_tools
        if (
            context.remaining_tool_calls is not None
            and context.remaining_tool_calls <= 0
        ):
            allowed &= retrieval_tools
        if context.approval_response:
            if (
                context.remaining_tool_calls is not None
                and context.remaining_tool_calls <= 0
            ):
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
        commentary = self._routing_commentary(response, context)
        # The whole response is classified before any of it reaches the broker.
        # A call Core will not run is answered with a failed observation the
        # model reads on its next turn; the calls around it still run.
        actions = self._routing_batch(response, context, allowed)
        if not actions:
            return self._no_action_turn(context, response, commentary, usage)
        if actions[0].call.name == FINISH_TOOL and actions[0].routing_error is None:
            finished = self._finish_result(context, actions[0].call.arguments, usage)
            return self._with_commentary(finished, commentary)

        executed: list[SpecialistResult] = []
        brokered = False
        slots = context.remaining_tool_calls
        for action in actions:
            call = action.call
            if action.routing_error is not None:
                executed.append(
                    self._routing_error_result(
                        action, usage=usage if not executed else (0, 0)
                    )
                )
                continue
            if self.specs[call.name].budget_class != "artifact_query":
                if slots is not None and slots <= 0:
                    # An earlier call in this batch spent the last slot. The
                    # queued remainder is dropped rather than spent, and the
                    # next turn routes again with what the mission can afford.
                    break
                if slots is not None:
                    slots -= 1
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
                    f"task:{context.task.id}:turn:{context.turn_index}:"
                    f"model-call:{call.id}"
                ),
                requested_by=self.role.value,
            )
            try:
                result = await self._execute_invocation(
                    context,
                    invocation,
                    model_call_id=call.id,
                    # One routing call produced the batch, so its spend is
                    # charged once, to the first call that runs.
                    usage=usage if not executed else (0, 0),
                )
                if action.provider_call_id is not None:
                    result.output["provider_call_id"] = action.provider_call_id
                executed.append(result)
                brokered = True
            except SpecialistApprovalRequired as pause:
                # The calls that already ran are handed to the mission with the
                # checkpoint, so their observations survive the pause and are
                # never executed a second time on resume.
                if executed:
                    pause.partial_result = self._with_commentary(
                        self._merge_turn(
                            self._with_replay_state(executed, actions, response)
                        ),
                        commentary,
                    )
                raise
        if not executed:
            raise MissionError(
                "the routing batch could not run a call within the mission "
                "tool-call budget"
            )
        turn = self._with_commentary(
            self._merge_turn(self._with_replay_state(executed, actions, response)),
            commentary,
        )
        if brokered:
            return turn
        # Nothing ran: every call in the response was answered unrun.
        return self._routing_deviation_turn(context, turn, usage)

    def _routing_request(
        self, context: SpecialistContext, allowed: frozenset[str]
    ) -> ModelRequest:
        tools = [self._definition(self.specs[name]) for name in sorted(allowed)]
        tools.append(self._finish_tool())
        return ModelRequest(
            model=self.model,
            instructions=self._routing_instructions(context),
            messages=[ModelMessage(role="user", content=self._prompt(context))],
            tools=tools,
            tool_choice=ToolChoice.REQUIRED,
            # A model may batch independent actions into one routing response.
            # The specialist still brokers them one at a time, in order, so
            # every call keeps its own invocation, budget slot and approval.
            parallel_tool_calls=True,
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
        arguments = self._brokered_arguments(invocation.tool_name, invocation.arguments)
        invocation = invocation.model_copy(update={"arguments": arguments})
        self._reject_unchanged_failed_invocation(
            context, invocation.tool_name, invocation.arguments
        )

        try:
            result = await self.broker.execute(invocation, self.scope)
        except ApprovalRequired as exc:
            record_caught_exception(
                "missions",
                "missions.agent_tooling.caught_failure_004",
                "A handled missions operation raised an exception.",
                exc,
                stage="agent_tooling",
            )
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
            record_caught_exception(
                "missions",
                "missions.agent_tooling.caught_failure_005",
                "A handled missions operation raised an exception.",
                denial,
                stage="agent_tooling",
            )
            status = "denied"
            provider_result: dict[str, Any] | str = tool_failure(
                self.specs[invocation.tool_name], invocation.arguments, denial,
                phase="before_execution", call_id=invocation.id,
            )
            summary = str(provider_result["problem"])
            evidence_ids: list[str] = []
            reproducible: list[str] = []
            exit_code = None
            output_truncated = False
            trusted_result = False
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "missions",
                "missions.agent_tooling.caught_failure_006",
                "A handled missions operation raised an exception.",
                caught_error,
                stage="agent_tooling",
            )
            raise
        except Exception as exc:
            record_caught_exception(
                "missions",
                "missions.agent_tooling.caught_failure_007",
                "A handled missions operation raised an exception.",
                exc,
                stage="agent_tooling",
            )
            status = "failed"
            provider_result = tool_failure(
                self.specs[invocation.tool_name], invocation.arguments, exc,
                phase="before_execution"
                if getattr(exc, "_nebula_before_execution", False)
                else "after_execution",
                call_id=invocation.id,
            )
            summary = str(provider_result["problem"])
            evidence_ids = []
            reproducible = []
            exit_code = None
            output_truncated = False
            trusted_result = False
        else:
            failed = self._tool_result_failed(result)
            provider_result = (
                tool_failure(
                    self.specs[invocation.tool_name], invocation.arguments,
                    TimeoutError("tool execution timed out")
                    if result.execution.get("timed_out") is True
                    or (result.receipt and result.receipt.status == ToolResultStatus.TIMED_OUT)
                    else asyncio.CancelledError("tool execution was cancelled")
                    if result.receipt and result.receipt.status == ToolResultStatus.CANCELLED
                    else RuntimeError(f"tool returned failure receipt: {result.model_result()!r}"),
                    phase="after_execution", call_id=invocation.id,
                )
                if failed else serialize_model_result(result.model_result())
            )
            if failed and isinstance(provider_result, dict):
                provider_result["result_receipt"] = {
                    "status": result.receipt.status.value if result.receipt else "failed",
                    "artifact_id": result.result_artifact_id,
                }
            try:
                delivered = (
                    json.loads(provider_result)
                    if isinstance(provider_result, str)
                    else provider_result
                )
            except (
                json.JSONDecodeError
            ):  # diagnostic-expected: untrusted tool results fail closed
                delivered = {}
            delivery_truncated = (
                isinstance(delivered, dict)
                and delivered.get("schema") == "nebula.bounded-result/v1"
            )
            output_truncated = (
                result.output_truncated
                or bool(result.receipt is not None and result.receipt.incomplete)
                or delivery_truncated
            )
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
            parsed_exit = result.model_result().get("exit_code")
            exit_code = (
                parsed_exit
                if isinstance(parsed_exit, int) and not isinstance(parsed_exit, bool)
                else result.exit_code
            )
            trusted_result = result.receipt is None and not failed

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
                "trusted_result": trusted_result,
                "exit_code": exit_code,
                "output_truncated": output_truncated,
            },
            evidence_ids=evidence_ids,
            reproducible_steps=reproducible,
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=self._cost(*usage),
            tool_calls=(
                0
                if self.specs[invocation.tool_name].budget_class == "artifact_query"
                else 1
            ),
        )

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
        extra = sorted(set(arguments) - _FINISH_FIELDS)
        if extra:
            # The finish action has no effect of its own, so fields beyond its
            # schema carry no authority: they are dropped, not a failed turn.
            record_diagnostic(
                "warning",
                "missions",
                "missions.routing.finish_extra_arguments",
                "A specialist finished with fields outside the finish schema; "
                "they were ignored.",
                outcome="recovered",
                stage="routing",
                run_id=context.run_id,
                metadata={
                    "task_id": context.task.id,
                    "argument_keys": [key[:64] for key in extra[:20]],
                },
            )
            arguments = {
                key: value for key, value in arguments.items() if key in _FINISH_FIELDS
            }
        if set(arguments) != _FINISH_FIELDS:
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

        # A call Core answered without running it is not an unresolved tool
        # result: nothing ran, and the model has already read why.
        recorded = [
            record
            for turn in context.prior_turns
            for record in call_records(turn.output)
            if record.get("routing_error") is None
        ]
        if status == "complete" and recorded:
            last_status = recorded[-1].get("status")
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
                "tool": record.get("tool"),
                "status": record.get("status"),
                "summary": turn.summary,
                "evidence_ids": turn.evidence_ids,
            }
            for turn in context.prior_turns
            for record in call_records(turn.output)
            if record.get("tool")
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

    def _routing_batch(
        self,
        response: Any,
        context: SpecialistContext,
        allowed: frozenset[str],
    ) -> list[_RoutingAction]:
        """Classify a whole routing response before any of it reaches the broker.

        A response may carry several independent routing actions. A call Core
        will not run (a tool that is not offered, a call cut off by the output
        limit or one the adapter could not read, a repeat of a call that
        already ran) is kept in order, flagged with the reason, and answered
        with a failed observation instead of failing the turn. The broker
        never sees it.
        """

        seen: set[str] = set()
        # Brokered calls by every id they went by: Core's and, for a call whose
        # reused id Core replaced, the provider's.
        ran: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for turn in context.prior_turns:
            for record in call_records(turn.output):
                call_ids = {
                    value
                    for value in (
                        record.get("model_call_id"),
                        record.get("provider_call_id"),
                    )
                    if isinstance(value, str)
                }
                seen.update(call_ids)
                arguments = record.get("arguments")
                if record.get("routing_error") is None and isinstance(arguments, dict):
                    for call_id in call_ids:
                        ran.setdefault(call_id, []).append(
                            (str(record.get("tool")), arguments)
                        )
        truncated = (
            response.finish_reason or ""
        ).lower() in _OUTPUT_LIMIT_FINISH_REASONS
        if truncated and response.tool_calls:
            self._routing_warning(
                "missions.routing.output_limit_calls",
                "A specialist routing response hit the output-token limit; its "
                "calls were answered unrun so the model re-issues them.",
                response,
                context,
                calls=len(response.tool_calls),
            )
        actions: list[_RoutingAction] = []
        for position, issued in enumerate(response.tool_calls):
            call = issued
            identity = (
                issued.name,
                self._brokered_arguments(issued.name, issued.arguments),
            )
            if issued.id in seen:
                # Providers reuse ids (per-response counters, synthetic Gemini
                # ids). The call keeps its place under an id Core makes unique,
                # so history still pairs every call with its own result.
                call = issued.model_copy(
                    update={
                        "id": self._core_call_id(context, position, issued.id, seen)
                    }
                )
                self._routing_warning(
                    "missions.routing.repeated_call_id",
                    "A specialist reused a routing call id; the call was given "
                    "a Core-unique id.",
                    response,
                    context,
                )
                if issued.invalid_reason is None and identity in ran.get(issued.id, []):
                    actions.append(
                        _RoutingAction(
                            call,
                            "already_ran",
                            f"This call repeats call {issued.id!r}, which already "
                            "ran with the same tool and arguments; its result is "
                            "in the history. It was not run again.",
                            provider_call_id=issued.id,
                        )
                    )
                    seen.add(call.id)
                    continue
            seen.update({issued.id, call.id})
            reissued = issued.id if call.id != issued.id else None
            if truncated:
                actions.append(
                    _RoutingAction(
                        call,
                        "output_limit",
                        f"Tool call {call.name!r} was not run: the response hit "
                        "the output token limit, so its arguments may be cut "
                        "off. Re-issue it with complete arguments, in a shorter "
                        "response if needed.",
                        provider_call_id=reissued,
                    )
                )
                continue
            if call.invalid_reason is not None:
                # The adapter could not read the call, so it has no arguments
                # to run; the model reads why and re-issues it.
                self._routing_warning(
                    "missions.routing.invalid_call",
                    "A specialist made a tool call Core could not read; the call "
                    "was answered with a failed observation.",
                    response,
                    context,
                    tool=call.name,
                )
                actions.append(
                    _RoutingAction(
                        call,
                        "invalid_call",
                        f"Tool call {call.name!r} was not run: "
                        f"{call.invalid_reason}; re-issue the call with "
                        "complete, valid JSON arguments.",
                        provider_call_id=reissued,
                    )
                )
                continue
            if call.name == FINISH_TOOL:
                if not actions:
                    actions.append(_RoutingAction(call))
                # A finish queued behind tool calls never runs: the next turn
                # must inspect those observations before finishing.
                break
            if call.name not in allowed:
                self._routing_warning(
                    "missions.routing.unavailable_tool",
                    "A specialist requested a tool it was not offered; the call "
                    "was answered with a failed observation.",
                    response,
                    context,
                    tool=call.name,
                )
                actions.append(
                    _RoutingAction(
                        call,
                        "unavailable_tool",
                        self._unavailable_detail(call.name, context, allowed),
                        provider_call_id=reissued,
                    )
                )
                continue
            ran.setdefault(issued.id, []).append(identity)
            actions.append(_RoutingAction(call, provider_call_id=reissued))
        return actions

    def _brokered_arguments(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The arguments the broker receives for a model call."""

        brokered = dict(arguments)
        spec = self.specs.get(tool_name)
        if spec is not None and "cwd" in spec.path_arguments:
            brokered["cwd"] = "."
        return brokered

    @staticmethod
    def _core_call_id(
        context: SpecialistContext,
        position: int,
        call_id: str,
        seen: set[str],
    ) -> str:
        """A unique replacement for a reused provider call id.

        Deterministic, so a replayed turn derives the same invocation id and
        idempotency key. Nine alphanumerics fit the strictest provider call-id
        format. An id Core synthesized for Gemini keeps its prefix, because
        such ids are never echoed back to Gemini.
        """

        attempt = 0
        while True:
            digest = uuid5(
                NAMESPACE_URL,
                (
                    f"nebula:model-call-id:{context.run_id}:{context.task.id}:"
                    f"{context.turn_index}:{len(context.prior_turns)}:{position}:"
                    f"{attempt}:{call_id}"
                ),
            ).hex
            candidate = (
                f"{_GEMINI_SYNTHETIC_CALL_ID}core:{digest[:16]}"
                if call_id.startswith(_GEMINI_SYNTHETIC_CALL_ID)
                else f"n{digest[:8]}"
            )
            if candidate not in seen:
                return candidate
            attempt += 1

    def _unavailable_detail(
        self,
        tool_name: str,
        context: SpecialistContext,
        allowed: frozenset[str],
    ) -> str:
        spec = self.specs.get(tool_name)
        if spec is None:
            reason = "this specialist is not offered a tool by that name"
        elif (
            context.remaining_tool_calls is not None
            and context.remaining_tool_calls <= 0
            and spec.budget_class != "artifact_query"
        ):
            reason = "the mission tool-call budget is spent"
        else:
            reason = "it is not enabled for this task"
        available = [*sorted(allowed), FINISH_TOOL]
        listed = ", ".join(available[:24]) + (", ..." if len(available) > 24 else "")
        return (
            f"Tool {tool_name!r} is not available: {reason}. It was not run. "
            f"Available: {listed}."
        )

    def _routing_error_result(
        self, action: _RoutingAction, *, usage: tuple[int, int]
    ) -> SpecialistResult:
        """A failed observation for a call Core answered without running it."""

        call = action.call
        spec = self.specs.get(call.name)
        failure = (
            tool_failure(
                spec, dict(call.arguments), InvalidToolArguments(action.detail),
                phase="before_execution", call_id=call.id,
            )
            if spec is not None
            else unavailable_tool_failure(call.name, action.detail, call_id=call.id)
        )
        safe_detail = self._safe_text(action.detail)
        failure["detail"] = safe_detail
        if action.routing_error != "invalid_call":
            failure["category"] = "call_not_run"
            failure["next_action"] = safe_detail
        output: dict[str, Any] = {
            "model_call_id": call.id,
            "tool": call.name,
            "arguments": dict(call.arguments),
            "status": "failed",
            "provider_result": failure,
            "trusted_result": False,
            "exit_code": None,
            "output_truncated": False,
            "routing_error": action.routing_error,
        }
        if action.provider_call_id is not None:
            output["provider_call_id"] = action.provider_call_id
        return SpecialistResult(
            summary=f"{call.name} was not run: {action.detail}"[:8_000],
            rationale=(
                f"routing call {call.id} was answered without running it; the "
                "next turn sees why"
            ),
            outcome=SpecialistOutcome.CONTINUE,
            output=output,
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=self._cost(*usage),
            tool_calls=0,
        )

    def _no_action_turn(
        self,
        context: SpecialistContext,
        response: Any,
        commentary: str,
        usage: tuple[int, int],
    ) -> SpecialistResult:
        """Ask again when a routing reply carried prose or nothing, not a call.

        The finish action needs an explicit complete or blocked status, which
        prose does not carry, and a reply such as "I'll scan the host next"
        is a plan rather than an answer. So the reply is kept as commentary
        and the model is asked again, bounded by the deviation limit.
        """

        self._routing_warning(
            "missions.routing.no_action",
            "A specialist routing response carried no routing action; the model "
            "was asked again.",
            response,
            context,
            status=(
                "text_without_tool_calls" if commentary else "empty_without_tool_calls"
            ),
        )
        turn = SpecialistResult(
            summary="The specialist replied without a routing action.",
            rationale="the next turn asks the model to call a tool or finish",
            outcome=SpecialistOutcome.CONTINUE,
            output={
                "status": "failed",
                "routing_error": "no_action",
                "routing_feedback": _NO_ACTION_FEEDBACK,
            },
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=self._cost(*usage),
            tool_calls=0,
        )
        return self._routing_deviation_turn(
            context, self._with_commentary(turn, commentary), usage
        )

    def _routing_deviation_turn(
        self,
        context: SpecialistContext,
        turn: SpecialistResult,
        usage: tuple[int, int],
    ) -> SpecialistResult:
        """Continue after a turn that ran nothing, or stop once it keeps happening."""

        turn.output["routing_deviation"] = True
        streak = 1
        for prior in reversed(context.prior_turns):
            if prior.output.get("routing_deviation") is not True:
                break
            streak += 1
        if streak < _MAX_CONSECUTIVE_ROUTING_DEVIATIONS:
            return turn
        record_diagnostic(
            "warning",
            "missions",
            "missions.routing.deviation_limit",
            "A specialist returned routing responses Core could not run several "
            "times in a row; the task stopped as blocked.",
            outcome="blocked",
            stage="routing",
            run_id=context.run_id,
            metadata={"task_id": context.task.id, "responses": streak},
        )
        records = call_records(turn.output)
        last = (
            records[-1].get("provider_result", {}).get("detail")
            if records
            else turn.output.get("routing_feedback")
        )
        blocked = self._finish_result(
            context,
            {
                "status": "blocked",
                "summary": (
                    f"The specialist stopped after {streak} routing responses in "
                    f"a row that Core could not run. Last: {last}"
                )[:8_000],
                "rationale": (
                    "the model kept returning routing responses with no runnable "
                    "action after being told why each one was not run"
                ),
            },
            usage,
        )
        blocked.output["routing_deviation"] = True
        if records:
            # The blocked result is what later tasks and the mission read;
            # replay state belongs to this task's routing history alone.
            blocked.output["calls"] = [
                {
                    key: value
                    for key, value in record.items()
                    if key not in _REPLAY_FIELDS
                }
                for record in records
            ]
        commentary = turn.output.get("commentary")
        return self._with_commentary(
            blocked, commentary if isinstance(commentary, str) else ""
        )

    def _routing_commentary(self, response: Any, context: SpecialistContext) -> str:
        """Prose a routing response carried, bounded; never a result."""

        if not response.text.strip():
            return ""
        if response.tool_calls:
            self._routing_warning(
                "missions.routing.prose_with_tool_calls",
                "A specialist returned prose beside its routing actions; the "
                "actions were routed and the prose was kept as commentary.",
                response,
                context,
            )
        return self._safe_text(response.text)

    @staticmethod
    def _with_replay_state(
        results: list[SpecialistResult],
        actions: list[_RoutingAction],
        response: Any,
    ) -> list[SpecialistResult]:
        """Tag one routing response's call records for replay on later turns.

        Every record gets the response's group key, so the provider is sent
        the calls back as the single message that issued them, as chat does.
        The response's reasoning state is kept once, on the first record, and
        each call keeps its own provider metadata. ``results`` follow
        ``actions`` in order: each action the batch reached left one record.
        """

        group = uuid4().hex
        state = getattr(response, "reasoning_state", None)
        for index, (result, action) in enumerate(zip(results, actions)):
            result.output["response_group"] = group
            if index == 0 and state:
                result.output["reasoning_state"] = state
            if action.call.provider_metadata:
                result.output["provider_metadata"] = action.call.provider_metadata
        return results

    @staticmethod
    def _with_commentary(result: SpecialistResult, commentary: str) -> SpecialistResult:
        if commentary:
            result.output["commentary"] = commentary
        return result

    @staticmethod
    def _routing_warning(
        event_code: str,
        message: str,
        response: Any,
        context: SpecialistContext,
        **metadata: Any,
    ) -> None:
        record_diagnostic(
            "warning",
            "missions",
            event_code,
            message,
            outcome="recovered",
            stage="routing",
            run_id=context.run_id,
            metadata={
                "task_id": context.task.id,
                "provider": response.provider_id,
                "model_id": response.model,
                "vendor_request_id": response.provider_request_id or "",
                "finish_reason": response.finish_reason or "",
                **metadata,
            },
        )

    def _merge_turn(self, results: list[SpecialistResult]) -> SpecialistResult:
        """Fold the calls a batch executed into the turn's single result."""

        if len(results) == 1:
            return results[0]
        records = [result.output for result in results]
        status = next(
            (
                str(record.get("status"))
                for record in records
                if record.get("status") != "complete"
            ),
            "complete",
        )
        return SpecialistResult(
            summary="; ".join(result.summary for result in results)[:8_000],
            rationale=(
                f"brokered {len(results)} calls in one turn; the next turn must "
                "inspect these observations before finishing"
            ),
            outcome=SpecialistOutcome.CONTINUE,
            output={"status": status, "calls": records},
            evidence_ids=list(
                dict.fromkeys(
                    evidence_id
                    for result in results
                    for evidence_id in result.evidence_ids
                )
            ),
            reproducible_steps=list(
                dict.fromkeys(
                    step for result in results for step in result.reproducible_steps
                )
            ),
            candidate_finding_ids=list(
                dict.fromkeys(
                    finding_id
                    for result in results
                    for finding_id in result.candidate_finding_ids
                )
            ),
            input_tokens=sum(result.input_tokens for result in results),
            output_tokens=sum(result.output_tokens for result in results),
            cost_usd=sum(result.cost_usd for result in results),
            tool_calls=sum(result.tool_calls for result in results),
        )

    def _routing_instructions(self, context: SpecialistContext) -> str:
        budget_note = (
            "No action tool-call limit is configured. Use only real capabilities "
            "that advance the objective."
            if context.remaining_tool_calls is None
            else (
                "No action tool-call slots remain. Artifact retrieval remains available; "
                "finish as complete only if the objective is satisfied after inspecting "
                "the necessary evidence, otherwise finish as blocked."
                if context.remaining_tool_calls <= 0
                else (
                    f"At most {context.remaining_tool_calls} real tool-call slots remain."
                )
            )
        )
        return (
            "Call one or more supplied routing actions and return no prose. Request "
            "several actions in the same response when they do not depend on each "
            "other; keep an action that needs an earlier observation for a later "
            "response. Nebula brokers a batch one call at a time, in the order you "
            "asked for. Finish with nebula.finish_task in a response of its own, "
            "once the observations you need are in hand. " + budget_note
        )

    def _prompt(self, context: SpecialistContext) -> str:
        earlier = context.prior_turns[:-8]
        earlier_summaries = [
            {
                "turn": index + 1,
                "summary": turn.summary[:500],
                "status": turn.output.get("status"),
                "tool": ", ".join(
                    str(record.get("tool"))
                    for record in call_records(turn.output)
                    if record.get("tool")
                )
                or None,
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
        feedback = (
            context.prior_turns[-1].output.get("routing_feedback")
            if context.prior_turns
            else None
        )
        if isinstance(feedback, str) and feedback:
            parts.append(f"Routing feedback on your previous response: {feedback}")
        if earlier_summaries:
            parts.append(
                "Earlier bounded turn summaries: "
                + json.dumps(earlier_summaries, ensure_ascii=False, sort_keys=True)
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
            # The latest real tool failure stays in view however old it is. A
            # call Core answered without running it is not one.
            if any(
                record.get("status") in {"failed", "denied", "incomplete"}
                and record.get("routing_error") is None
                for record in call_records(context.prior_turns[index].output)
            ):
                selected_indexes.add(index)
                break
        for index in sorted(selected_indexes):
            turn = context.prior_turns[index]
            for record in call_records(turn.output):
                call_id = record.get("model_call_id")
                tool_name = record.get("tool")
                provider_result = record.get("provider_result")
                if (
                    not isinstance(call_id, str)
                    or not isinstance(tool_name, str)
                    or not isinstance(provider_result, (dict, str))
                ):
                    continue
                arguments = record.get("arguments")
                group = record.get("response_group")
                state = record.get("reasoning_state")
                metadata = record.get("provider_metadata")
                history.append(
                    ModelToolResult(
                        call_id=call_id,
                        name=tool_name,
                        arguments=arguments if isinstance(arguments, dict) else {},
                        output=sanitize_model_history_result(
                            provider_result,
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            trusted_result=record.get("trusted_result") is True,
                        ),
                        is_error=record.get("status") != "complete",
                        # Turns recorded before these existed replay one call
                        # per message, as they always did.
                        response_group=group if isinstance(group, str) else None,
                        reasoning_state=state if isinstance(state, dict) else None,
                        provider_metadata=(
                            metadata if isinstance(metadata, dict) else None
                        ),
                    )
                )
        return history

    @staticmethod
    def _tool_result_failed(result: Any) -> bool:
        if result.receipt is not None:
            return result.receipt.status in {
                ToolResultStatus.FAILED,
                ToolResultStatus.TIMED_OUT,
                ToolResultStatus.CANCELLED,
            }
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
        return serialize_model_result(output)

    @classmethod
    def _result_summary(cls, tool_name: str, result: Any, status: str) -> str:
        if status == "failed":
            if (
                result.receipt is not None
                and result.receipt.status == ToolResultStatus.TIMED_OUT
            ):
                return f"{tool_name} timed out; searchable partial output was preserved"
            return f"{tool_name} failed with exit code {result.exit_code}; searchable partial output was preserved"
        if status == "incomplete":
            return f"{tool_name} completed with truncated output"
        if result.receipt is not None and result.receipt.warnings:
            return (
                f"{tool_name} completed with warnings; raw artifacts remain searchable"
            )
        return f"{tool_name} completed; inspect artifacts with tool_output.search"

    @staticmethod
    def _reject_unchanged_failed_invocation(
        context: SpecialistContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        for turn in reversed(context.prior_turns):
            for record in call_records(turn.output):
                if record.get("status") not in {"failed", "denied", "incomplete"}:
                    continue
                if record.get("routing_error") is not None:
                    # Core answered that call without running it, so repeating
                    # it (a re-issued cut-off call, say) is not a retry.
                    continue
                if (
                    record.get("tool") == tool_name
                    and record.get("arguments") == arguments
                ):
                    raise MissionError(
                        "the model repeated a failed invocation unchanged; inspect "
                        "the prior error and change the tool or arguments"
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
