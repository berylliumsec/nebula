"""Bounded model specialists that can use only brokered tool capabilities."""

from __future__ import annotations

from .diagnostics import record_caught_exception

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
    ToolChoice,
    ToolDefinition,
)
from .redaction import redact_text
from .tools import ApprovalRequired, PolicyDenied, ToolBroker, ToolInvocation, ToolSpec
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


def _recorded_call_ids(output: Mapping[str, Any]) -> list[str]:
    return [
        str(record["model_call_id"])
        for record in call_records(output)
        if isinstance(record.get("model_call_id"), str)
    ]


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
        max_output_tokens: int = 2048,
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
        # The whole response is validated before any of it reaches the broker,
        # so a malformed batch still produces no partial effect.
        batch = self._routing_batch(response, context, allowed)
        if batch[0].name == FINISH_TOOL:
            return self._finish_result(context, batch[0].arguments, usage)

        executed: list[SpecialistResult] = []
        slots = context.remaining_tool_calls
        for call in batch:
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
                executed.append(
                    await self._execute_invocation(
                        context,
                        invocation,
                        model_call_id=call.id,
                        # One routing call produced the batch, so its spend is
                        # charged once, to the first call that runs.
                        usage=usage if not executed else (0, 0),
                    )
                )
            except SpecialistApprovalRequired as pause:
                # The calls that already ran are handed to the mission with the
                # checkpoint, so their observations survive the pause and are
                # never executed a second time on resume.
                if executed:
                    pause.partial_result = self._merge_turn(executed)
                raise
        if not executed:
            raise MissionError(
                "the routing batch could not run a call within the mission "
                "tool-call budget"
            )
        return self._merge_turn(executed)

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
        spec = self.specs[invocation.tool_name]
        arguments = dict(invocation.arguments)
        if "cwd" in spec.path_arguments:
            arguments["cwd"] = "."
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
            detail = self._safe_text(f"{type(exc).__name__}: {exc}")
            provider_result = {"status": status, "detail": detail}
            summary = f"{invocation.tool_name} failed: {detail}"
            evidence_ids = []
            reproducible = []
            exit_code = None
            output_truncated = False
            trusted_result = False
        else:
            failed = self._tool_result_failed(result)
            provider_result = serialize_model_result(result.model_result())
            try:
                delivered = json.loads(provider_result)
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
            trusted_result = result.receipt is None

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

        recorded = [
            record
            for turn in context.prior_turns
            for record in call_records(turn.output)
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

    @staticmethod
    def _routing_batch(
        response: Any,
        context: SpecialistContext,
        allowed: frozenset[str],
    ) -> list[Any]:
        """Validate a whole routing response before any of it reaches the broker.

        A response may carry several independent routing actions. Rejecting a
        malformed batch as a unit keeps the guarantee that a bad routing step
        never produces a partial effect.
        """

        if response.text.strip():
            raise MissionError("specialist returned prose during required routing")
        if not response.tool_calls:
            raise MissionError("specialist returned no routing action")
        seen = {
            call_id
            for turn in context.prior_turns
            for call_id in _recorded_call_ids(turn.output)
        }
        batch: list[Any] = []
        for call in response.tool_calls:
            if call.id in seen:
                raise MissionError(
                    "specialist repeated a completed routing call id; "
                    "refusing duplicate execution"
                )
            seen.add(call.id)
            if call.name == FINISH_TOOL:
                if not batch:
                    batch.append(call)
                # A finish queued behind tool calls never runs: the next turn
                # must inspect those observations before finishing.
                break
            if call.name not in allowed:
                raise MissionError(f"model requested unavailable tool {call.name!r}")
            batch.append(call)
        return batch

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
            if context.prior_turns[index].output.get("status") in {
                "failed",
                "denied",
                "incomplete",
            }:
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
