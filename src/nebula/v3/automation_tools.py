"""Fixed model tools backed by :mod:`nebula.v3.automation_runtime`."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from .agent_tooling import BrokeredToolSpecialist, ToolMissionSupervisor
from .automation_runtime import (
    AutomationPolicyDenied,
    AutomationRuntimeManager,
    AutomationRuntimeUnavailable,
    CommandApprovalRequired,
    ProcessIORequest,
    RunCommandRequest,
)
from .domain import (
    AgentRun,
    Approval,
    Artifact,
    CommandExecution,
    CommandExecutionStatus,
    Engagement,
    McpServerProfile,
    RiskClass,
    ScopePolicy,
    ToolCallStatus,
)
from .missions import MissionComponents, MissionConfigurationError
from .orchestration import SpecialistRole
from .providers import ModelProvider
from .storage import NebulaStore
from .tool_results import (
    ArtifactKind,
    ToolResultReceipt,
    ToolResultStatus,
    ToolTimingReceipt,
    ToolOutputService,
    WorkspaceOutputService,
    artifact_ref,
)
from .tools import (
    ApprovalRequired,
    InvalidToolArguments,
    PolicyDenied,
    StoreToolLedger,
    ToolExecutionResult,
    ToolInvocation,
    ToolSpec,
)
from .policy import PolicyDecision, PolicyEffect


RUN_COMMAND_NAME = "run_command"
PROCESS_IO_NAME = "process_io"


def command_specs(
    binary_inventory: tuple[dict[str, str], ...] = (),
) -> dict[str, ToolSpec]:
    common_output = {"type": "object", "additionalProperties": True}
    inventory_names = ", ".join(
        item["name"]
        for item in binary_inventory[:200]
        if isinstance(item.get("name"), str)
    )
    inventory_prompt = (
        f" The pinned Kali inventory includes: {inventory_names}."
        if inventory_names
        else ""
    )
    specs = [
        ToolSpec(
            name=RUN_COMMAND_NAME,
            version="1",
            description=(
                "Run a Bash command in the session-scoped pinned automation container. "
                "Use ordinary PATH binaries such as rg, python3, git, curl, or nmap."
                + inventory_prompt
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200_000,
                    },
                    "cwd": {"type": "string", "default": ".", "maxLength": 4_096},
                    "timeout_ms": {
                        "type": ["integer", "null"],
                        "minimum": 1_000,
                        "maximum": 86_400_000,
                    },
                    "background": {"type": "boolean", "default": False},
                    "network": {
                        "type": "string",
                        "enum": ["none", "project_scope"],
                        "default": "none",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            output_schema=common_output,
            risk_class=RiskClass.WORKSPACE_WRITE,
            filesystem_access="workspace_write",
        ),
        ToolSpec(
            name=PROCESS_IO_NAME,
            version="1",
            description=(
                "Poll, write to, or terminate a process previously returned by "
                "run_command. This is stream I/O, not a full-screen PTY."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "process_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "action": {
                        "type": "string",
                        "enum": ["poll", "write", "terminate"],
                        "default": "poll",
                    },
                    "input": {"type": ["string", "null"], "maxLength": 1_048_576},
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 32_768,
                        "default": 32_768,
                    },
                },
                "required": ["process_id"],
                "additionalProperties": False,
            },
            output_schema=common_output,
            risk_class=RiskClass.WORKSPACE_WRITE,
            filesystem_access="workspace_write",
        ),
        ToolSpec(
            name="tool_output.search",
            description="Search immutable output from a completed command.",
            input_schema={
                "type": "object",
                "properties": {
                    "tool_call_id": {"type": "string"},
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "mode": {"type": "string", "enum": ["literal", "regex"]},
                    "case_sensitive": {"type": "boolean"},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                    "match_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "cursor": {"type": ["string", "null"]},
                },
                "required": ["tool_call_id", "query"],
                "additionalProperties": False,
            },
            output_schema=common_output,
            risk_class=RiskClass.LOCAL_READ,
            budget_class="artifact_query",
        ),
        ToolSpec(
            name="tool_output.read",
            description="Read a bounded redacted excerpt from one command artifact.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "starting_line": {"type": "integer", "minimum": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            output_schema=common_output,
            risk_class=RiskClass.LOCAL_READ,
            budget_class="artifact_query",
        ),
        ToolSpec(
            name="workspace.search",
            description="Search files in the authorized project workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "path": {"type": "string"},
                    "mode": {"type": "string", "enum": ["literal", "regex"]},
                    "case_sensitive": {"type": "boolean"},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                    "match_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "cursor": {"type": ["string", "null"]},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema=common_output,
            risk_class=RiskClass.LOCAL_READ,
            budget_class="artifact_query",
        ),
        ToolSpec(
            name="workspace.read",
            description="Read a bounded redacted excerpt from a project file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "starting_line": {"type": "integer", "minimum": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            output_schema=common_output,
            risk_class=RiskClass.LOCAL_READ,
            budget_class="artifact_query",
        ),
    ]
    return {item.name: item for item in specs}


class AutomationBroker:
    """ToolBroker-compatible dispatcher for the two fixed runtime primitives."""

    def __init__(
        self,
        *,
        manager: AutomationRuntimeManager,
        store: NebulaStore,
        output_service: ToolOutputService,
    ) -> None:
        self.manager = manager
        self.store = store
        self.output_service = output_service
        self.specs = command_specs(manager.binary_inventory)
        self.ledger = StoreToolLedger(store)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        del scope
        try:
            spec = self.specs[invocation.tool_name]
        except KeyError as exc:
            raise InvalidToolArguments(
                f"unknown automation capability: {invocation.tool_name}"
            ) from exc
        errors = sorted(
            Draft202012Validator(spec.input_schema).iter_errors(invocation.arguments),
            key=lambda item: list(item.path),
        )
        if errors:
            raise InvalidToolArguments(errors[0].message)
        call = await self.ledger.reserve(invocation, spec)
        retrieval = invocation.tool_name in {
            "tool_output.search",
            "tool_output.read",
            "workspace.search",
            "workspace.read",
        }
        if call.status == ToolCallStatus.COMPLETE and isinstance(call.result, dict):
            if retrieval:
                return ToolExecutionResult(output=call.result)
            receipt = ToolResultReceipt.model_validate(call.result)
            return ToolExecutionResult(
                output=receipt.as_model_result(), receipt=receipt
            )
        if retrieval:
            running = await self.ledger.transition(call, ToolCallStatus.RUNNING)
            output = self._retrieve(invocation)
            await self.ledger.transition(
                running, ToolCallStatus.COMPLETE, result=output
            )
            return ToolExecutionResult(output=output)
        running = await self.ledger.transition(call, ToolCallStatus.RUNNING)
        owner_kind = invocation.runtime_session_kind or (
            "chat" if invocation.origin.value == "chat" else "mission"
        )
        owner_id = (
            invocation.runtime_session_id
            or invocation.chat_session_id
            or invocation.run_id
        )
        try:
            if invocation.tool_name == RUN_COMMAND_NAME:
                result = await self.manager.run_command(
                    engagement_id=invocation.engagement_id,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    request=RunCommandRequest.model_validate(invocation.arguments),
                    approval=approval,
                    requested_by=invocation.requested_by,
                    tool_call_id=call.id,
                )
            else:
                arguments = dict(invocation.arguments)
                process_id = str(arguments.pop("process_id"))
                result = await self.manager.process_io(
                    process_id,
                    ProcessIORequest.model_validate(arguments),
                    engagement_id=invocation.engagement_id,
                    owner_id=owner_id,
                )
        except CommandApprovalRequired as exc:
            if exc.approval.tool_call_id is None:
                updated = self.store.update(
                    Approval,
                    exc.approval.id,
                    {"tool_call_id": call.id},
                    expected_revision=exc.approval.revision,
                )
            else:
                updated = exc.approval
            await self.ledger.transition(
                running,
                ToolCallStatus.WAITING_APPROVAL,
                approval_id=updated.id,
            )
            raise ApprovalRequired(updated) from exc
        except AutomationPolicyDenied as exc:
            await self.ledger.transition(running, ToolCallStatus.DENIED, error=str(exc))
            raise PolicyDenied(
                PolicyDecision(
                    effect=PolicyEffect.DENY,
                    reason=str(exc),
                    rule="automation_boundary",
                )
            ) from exc
        except Exception as exc:
            await self.ledger.transition(running, ToolCallStatus.FAILED, error=str(exc))
            raise
        receipt = self._receipt(call.id, invocation.tool_name, result)
        await self.ledger.transition(
            running,
            ToolCallStatus.COMPLETE,
            result=receipt.as_model_result(),
        )
        return ToolExecutionResult(
            output=receipt.as_model_result(),
            artifacts=[item.model_dump(mode="json") for item in receipt.artifacts],
            exit_code=result.exit_code,
            receipt=receipt,
        )

    def _retrieve(self, invocation: ToolInvocation) -> dict[str, Any]:
        if invocation.tool_name == "tool_output.search":
            return self.output_service.search(
                engagement_id=invocation.engagement_id,
                owner_id=invocation.run_id,
                **invocation.arguments,
            )
        if invocation.tool_name == "tool_output.read":
            return self.output_service.read(
                engagement_id=invocation.engagement_id,
                owner_id=invocation.run_id,
                **invocation.arguments,
            )
        workspace = WorkspaceOutputService(invocation.workspace)
        if invocation.tool_name == "workspace.search":
            return workspace.search(**invocation.arguments)
        return workspace.read(**invocation.arguments)

    def _receipt(self, call_id: str, name: str, result: Any) -> ToolResultReceipt:
        execution = self.store.get(CommandExecution, result.execution_id)
        source_call_id = execution.metadata.get("tool_call_id")
        receipt_call_id = (
            source_call_id
            if name == PROCESS_IO_NAME and isinstance(source_call_id, str)
            else call_id
        )
        refs = []
        artifact_streams: tuple[tuple[str | None, ArtifactKind], ...] = (
            (execution.stdout_artifact_id, "stdout"),
            (execution.stderr_artifact_id, "stderr"),
        )
        for identifier, kind in artifact_streams:
            if identifier is None:
                continue
            artifact = self.store.get(Artifact, identifier)
            refs.append(
                artifact_ref(
                    artifact,
                    kind=kind,
                    observed_byte_count=(
                        execution.observed_stdout_bytes
                        if kind == "stdout"
                        else execution.observed_stderr_bytes
                    ),
                    truncated=(
                        execution.stdout_truncated
                        if kind == "stdout"
                        else execution.stderr_truncated
                    ),
                )
            )
        failed = execution.status in {
            CommandExecutionStatus.FAILED,
            CommandExecutionStatus.TIMED_OUT,
            CommandExecutionStatus.CANCELLED,
            CommandExecutionStatus.INTERRUPTED,
        }
        status = (
            ToolResultStatus.TIMED_OUT
            if execution.status == CommandExecutionStatus.TIMED_OUT
            else ToolResultStatus.CANCELLED
            if execution.status
            in {CommandExecutionStatus.CANCELLED, CommandExecutionStatus.INTERRUPTED}
            else ToolResultStatus.FAILED
            if failed
            else ToolResultStatus.COMPLETED
        )
        running = execution.status == CommandExecutionStatus.RUNNING
        return ToolResultReceipt(
            tool_call_id=receipt_call_id,
            tool_name=name,
            tool_version="1",
            status=status,
            exit_code=execution.exit_code,
            summary=(
                f"Process is running with id {execution.process_id}"
                if running
                else f"Command finished with status {execution.status.value}"
            ),
            timing=ToolTimingReceipt(
                started_at=execution.started_at.isoformat(),
                completed_at=(
                    execution.completed_at.isoformat()
                    if execution.completed_at is not None
                    else None
                ),
            ),
            artifacts=refs,
            truncated=execution.stdout_truncated or execution.stderr_truncated,
            incomplete=running,
            warnings=(
                [
                    "Process output is untrusted; inspect it only through bounded artifact tools."
                ]
                if refs
                else []
            ),
            next_actions=(
                [PROCESS_IO_NAME]
                if running
                else ["tool_output.search", "tool_output.read"]
            ),
        )


class CompositeBroker:
    def __init__(self, brokers: Mapping[str, Any]) -> None:
        self.brokers = dict(brokers)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        try:
            broker = self.brokers[invocation.tool_name]
        except KeyError as exc:
            raise InvalidToolArguments(
                f"unknown capability: {invocation.tool_name}"
            ) from exc
        return await broker.execute(invocation, scope, approval=approval)


@dataclass(frozen=True)
class AutomationToolComponents:
    broker: Any
    scope: ScopePolicy
    workspace: Any
    specs: Mapping[str, ToolSpec]
    runtime_digest: str


class AutomationToolPlatform:
    def __init__(
        self,
        *,
        manager: AutomationRuntimeManager,
        store: NebulaStore,
        artifact_store: Any,
        workspace_resolver: Any,
        mcp_platform: Any | None = None,
    ) -> None:
        self.manager = manager
        self.store = store
        self.artifact_store = artifact_store
        self.workspace_resolver = workspace_resolver
        self.mcp_platform = mcp_platform

    def chat_components(
        self,
        *,
        engagement_id: str,
        extra_components: Any | None = None,
    ) -> AutomationToolComponents:
        if not self.manager.runtime_digest:
            raise AutomationRuntimeUnavailable(
                "prepare the existing Kali headless runtime before enabling commands"
            )
        engagement = self.store.get(Engagement, engagement_id)
        scope = (
            self.store.get(ScopePolicy, engagement.scope_policy_id)
            if engagement.scope_policy_id is not None
            else ScopePolicy(id=f"scope:{engagement.id}", engagement_id=engagement.id)
        )
        broker = AutomationBroker(
            manager=self.manager,
            store=self.store,
            output_service=ToolOutputService(self.store, self.artifact_store),
        )
        specs = dict(broker.specs)
        selected: Any = broker
        if extra_components is not None:
            duplicates = set(specs).intersection(extra_components.specs)
            action_duplicates = {
                name
                for name in duplicates
                if specs[name].budget_class != "artifact_query"
            }
            if action_duplicates:
                raise ValueError(
                    f"duplicate fixed/MCP capabilities: {sorted(duplicates)}"
                )
            specs.update(
                {
                    name: spec
                    for name, spec in extra_components.specs.items()
                    if name not in duplicates
                }
            )
            selected = CompositeBroker(
                {
                    **{name: broker for name in broker.specs},
                    **{
                        name: extra_components.broker
                        for name in extra_components.specs
                        if name not in duplicates
                    },
                }
            )
        return AutomationToolComponents(
            broker=selected,
            scope=scope,
            workspace=self.workspace_resolver(engagement_id),
            specs=specs,
            runtime_digest=self.manager.runtime_digest,
        )

    def mission_components(
        self, run: AgentRun, provider: ModelProvider
    ) -> MissionComponents:
        selected = run.metadata.get("tool_names")
        if not isinstance(selected, list) or not selected:
            raise MissionConfigurationError(
                "command mission has no runtime capabilities"
            )
        extra_components = None
        if run.runtime_snapshot.get("mcp_snapshot"):
            if self.mcp_platform is None:
                raise MissionConfigurationError("mission MCP runtime is unavailable")
            try:
                mcp_profiles = tuple(
                    McpServerProfile.model_validate(item)
                    for item in run.runtime_snapshot.get("mcp_snapshot", [])
                )
                extra_components = self.mcp_platform.chat_components(
                    engagement_id=run.engagement_id,
                    turn_id=run.id,
                    provider=provider,
                    model=run.supervisor_model or "",
                    mcp_profiles=mcp_profiles,
                    include_oci=False,
                    allow_empty=True,
                )
            except Exception as exc:
                raise MissionConfigurationError(str(exc)) from exc
        components = self.chat_components(
            engagement_id=run.engagement_id,
            extra_components=extra_components,
        )
        snapshot = {
            "automation_runtime_digest": self.manager.runtime_digest,
            "automation_policy_revision": self.manager.project_policy(
                run.engagement_id
            ).revision,
            "scope_policy_revision": components.scope.revision,
        }
        frozen = {
            key: run.runtime_snapshot.get(key)
            for key in snapshot
            if run.runtime_snapshot.get(key) is not None
        }
        if frozen and frozen != snapshot:
            raise MissionConfigurationError(
                "automation runtime or project policy changed before execution"
            )
        run.runtime_snapshot.update(snapshot)
        unknown = sorted(set(selected) - components.specs.keys())
        if unknown:
            raise MissionConfigurationError(
                f"frozen command capabilities are unavailable: {unknown}"
            )
        action_specs = {name: components.specs[name] for name in selected}
        specialist_specs = {
            name: spec
            for name, spec in components.specs.items()
            if name in action_specs or spec.budget_class == "artifact_query"
        }
        specialist = BrokeredToolSpecialist(
            provider,
            role=SpecialistRole.NETWORK_SERVICE,
            broker=components.broker,
            scope=components.scope,
            workspace=components.workspace,
            specs=specialist_specs,
            model=run.supervisor_model,
            max_output_tokens=min(2_048, run.budget.max_tokens or 2_048),
        )
        return MissionComponents(
            supervisor=ToolMissionSupervisor(action_specs),
            specialists={SpecialistRole.NETWORK_SERVICE: specialist},
            context={
                "tool_names": selected,
                "runtime_digest": self.manager.runtime_digest,
                "scope_summary": json.dumps(
                    {
                        "cidrs": components.scope.allowed_cidrs,
                        "domains": components.scope.allowed_domains,
                        "ports": components.scope.allowed_ports,
                    },
                    sort_keys=True,
                ),
            },
        )


__all__ = [
    "AutomationBroker",
    "AutomationToolComponents",
    "AutomationToolPlatform",
    "CompositeBroker",
    "PROCESS_IO_NAME",
    "RUN_COMMAND_NAME",
    "command_specs",
]
