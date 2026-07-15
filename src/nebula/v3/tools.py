"""Typed tool plugins and the mandatory policy/approval execution broker."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import ArtifactStore
from .domain import (
    Approval,
    ApprovalStatus,
    Evidence,
    RiskClass,
    ToolCallOrigin,
    ScopePolicy,
    ToolCall as PersistedToolCall,
    ToolCallStatus,
    utc_now,
)
from .policy import PolicyDecision, PolicyEffect, PolicyEngine, PolicyRequest
from .sandbox import (
    EgressRule,
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxRunner,
    SandboxWorkspaceAccess,
)
from .storage import ConflictError, NebulaStore, NotFoundError


class ToolBrokerError(RuntimeError):
    pass


class AmbiguousToolState(ToolBrokerError):
    """A recovered side effect cannot be repeated without an explicit retry."""


class UnknownTool(ToolBrokerError):
    pass


class InvalidToolArguments(ToolBrokerError):
    pass


class PolicyDenied(ToolBrokerError):
    def __init__(self, decision: PolicyDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class ApprovalRequired(ToolBrokerError):
    """A durable checkpoint: resume the same invocation with this approval."""

    def __init__(self, approval: Approval) -> None:
        super().__init__(approval.policy_rationale)
        self.approval = approval


class IdempotencyBehavior(str, Enum):
    SAFE = "safe"
    KEY_REQUIRED = "key_required"
    NON_IDEMPOTENT = "non_idempotent"


class ToolArgumentBinding(BaseModel):
    """Declaratively map one typed input property to deterministic argv."""

    model_config = ConfigDict(extra="forbid")

    argument: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
    kind: Literal["value", "repeat", "csv", "json", "boolean_flag", "positional"] = (
        "value"
    )
    flag: str | None = None

    @model_validator(mode="after")
    def binding_shape_is_safe(self) -> "ToolArgumentBinding":
        if self.kind == "positional":
            if self.flag is not None:
                raise ValueError("positional bindings cannot declare a flag")
        elif not self.flag or not self.flag.startswith("-") or "\x00" in self.flag:
            raise ValueError("non-positional bindings require a fixed option flag")
        return self


class ToolSpec(BaseModel):
    """Security and data contract for one installed tool capability."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(default="1", min_length=1)
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_class: RiskClass
    network_access: bool = False
    filesystem_access: str = Field(
        default="none", pattern=r"^(none|read|workspace_write)$"
    )
    credential_classes: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    resource_limits: SandboxLimits = Field(default_factory=SandboxLimits)
    parser: str | None = None
    idempotency: IdempotencyBehavior = IdempotencyBehavior.SAFE
    target_argument: str | None = None
    port_argument: str | None = None
    path_arguments: list[str] = Field(default_factory=list)
    action: str | None = None
    cloud_transfer: bool = False
    requires_approval: bool = False
    pack_id: str | None = Field(default=None, max_length=400)
    manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    image: str | None = None
    executable: str | None = None
    fixed_arguments: list[str] = Field(default_factory=list)
    argument_bindings: list[ToolArgumentBinding] = Field(default_factory=list)
    parser_contract: dict[str, Any] | None = None
    smoke_test_fixture: dict[str, Any] | None = None

    @field_validator("input_schema", "output_schema")
    @classmethod
    def valid_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_001",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tools",
            )
            raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        if value.get("type") != "object":
            raise ValueError("tool schemas must describe an object")
        return value

    @model_validator(mode="after")
    def security_contract_is_consistent(self) -> "ToolSpec":
        network_risks = {
            RiskClass.PASSIVE,
            RiskClass.ACTIVE_SCAN,
            RiskClass.CREDENTIAL_USE,
            RiskClass.EXPLOITATION,
            RiskClass.PERSISTENCE,
            RiskClass.DESTRUCTIVE,
        }
        if self.network_access and self.risk_class not in network_risks:
            raise ValueError("network tools must declare a network-capable risk class")
        if self.network_access and not self.target_argument:
            raise ValueError("network tools require a trusted target_argument mapping")
        properties = self.input_schema.get("properties", {})
        mapped = [
            value
            for value in [
                self.target_argument,
                self.port_argument,
                *self.path_arguments,
            ]
            if value
        ]
        unknown = [value for value in mapped if value not in properties]
        if unknown:
            raise ValueError(
                f"security argument mappings are absent from schema: {unknown}"
            )
        if self.target_argument and self.target_argument not in self.input_schema.get(
            "required", []
        ):
            raise ValueError("target_argument must be required by the input schema")
        execution_fields = (
            self.image,
            self.executable,
            self.pack_id,
            self.manifest_digest,
        )
        if any(value is not None for value in execution_fields):
            if any(value is None for value in execution_fields):
                raise ValueError(
                    "declarative tools require image, executable, pack_id, and "
                    "manifest_digest"
                )
            assert self.image is not None
            assert self.executable is not None
            if not _is_digest_pinned_image(self.image):
                raise ValueError("tool image must be pinned by SHA-256 without a tag")
            if not self.executable.startswith("/") or "\x00" in self.executable:
                raise ValueError("tool executable must be an absolute container path")
            if Path(self.executable).name.lower() in {
                "sh",
                "bash",
                "dash",
                "zsh",
                "fish",
                "cmd",
                "powershell",
                "pwsh",
            }:
                raise ValueError("shell interpreters cannot be tool executables")
            if self.input_schema.get("additionalProperties") is not False:
                raise ValueError(
                    "executable tool schemas must set additionalProperties=false"
                )
            if any("\x00" in value for value in self.fixed_arguments):
                raise ValueError("fixed arguments cannot contain NUL bytes")
            bound = [binding.argument for binding in self.argument_bindings]
            if len(bound) != len(set(bound)):
                raise ValueError("an input argument may be bound to argv only once")
            missing = [name for name in bound if name not in properties]
            if missing:
                raise ValueError(f"argv bindings are absent from schema: {missing}")
        return self


def _is_digest_pinned_image(image: str) -> bool:
    """Accept repository@sha256:digest and reject a mutable tag before @."""

    match = re.fullmatch(
        r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
        r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+)@sha256:[0-9a-f]{64}",
        image,
    )
    if match is None:
        return False
    return ":" not in match.group("repository").rsplit("/", 1)[-1]


def build_declared_command(spec: ToolSpec, arguments: dict[str, Any]) -> list[str]:
    """Build argv only from a validated declarative spec and typed values."""

    if spec.executable is None:
        raise InvalidToolArguments("tool has no declarative executable")
    command = [spec.executable, *spec.fixed_arguments]
    for binding in spec.argument_bindings:
        if binding.argument not in arguments:
            continue
        value = arguments[binding.argument]
        if binding.kind == "boolean_flag":
            if not isinstance(value, bool):
                raise InvalidToolArguments(
                    f"{binding.argument} must be a boolean for boolean_flag"
                )
            if value:
                assert binding.flag is not None
                command.append(binding.flag)
            continue
        if binding.kind == "repeat":
            if not isinstance(value, list):
                raise InvalidToolArguments(
                    f"{binding.argument} must be an array for repeat"
                )
            assert binding.flag is not None
            for item in value:
                command.extend([binding.flag, _argv_scalar(binding.argument, item)])
            continue
        if binding.kind == "csv":
            if not isinstance(value, list):
                raise InvalidToolArguments(
                    f"{binding.argument} must be an array for csv"
                )
            assert binding.flag is not None
            command.extend(
                [
                    binding.flag,
                    ",".join(_argv_scalar(binding.argument, item) for item in value),
                ]
            )
            continue
        if binding.kind == "json":
            assert binding.flag is not None
            try:
                rendered_json = json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_002",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="tools",
                )
                raise InvalidToolArguments(
                    f"{binding.argument} must be JSON serializable"
                ) from exc
            if "\x00" in rendered_json:
                raise InvalidToolArguments(f"{binding.argument} contains a NUL byte")
            command.extend([binding.flag, rendered_json])
            continue
        rendered = _argv_scalar(binding.argument, value)
        if binding.kind == "positional":
            if rendered.startswith("-"):
                raise InvalidToolArguments(
                    f"{binding.argument} cannot be interpreted as an option"
                )
            command.append(rendered)
        else:
            assert binding.flag is not None
            command.extend([binding.flag, rendered])
    return command


def _argv_scalar(name: str, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise InvalidToolArguments(f"{name} must be a string or number")
    rendered = str(value)
    if "\x00" in rendered:
        raise InvalidToolArguments(f"{name} contains a NUL byte")
    return rendered


class ToolInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    engagement_id: str
    run_id: str
    origin: ToolCallOrigin = ToolCallOrigin.MISSION
    chat_session_id: str | None = None
    chat_turn_id: str | None = None
    task_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    workspace: Path
    target: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    resolved_ips: list[str] = Field(default_factory=list)
    credential_class: str | None = None
    idempotency_key: str | None = None
    requested_by: str = "agent"


class ToolExecutionResult(BaseModel):
    output: dict[str, Any]
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    output_truncated: bool = False
    parser_error: str | None = Field(default=None, max_length=1_000)
    execution: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class PreparedToolCall(BaseModel):
    call: PersistedToolCall
    decision: PolicyDecision
    invocation: ToolInvocation
    approval: Approval | None = None
    cached_result: ToolExecutionResult | None = None


class ToolPlugin(ABC):
    spec: ToolSpec

    @abstractmethod
    async def execute(
        self,
        invocation: ToolInvocation,
        runner: SandboxRunner,
    ) -> ToolExecutionResult:
        raise NotImplementedError


class AnalysisTool(ToolPlugin):
    """A trusted, non-executable parser/retriever registered by application code."""

    def __init__(
        self,
        spec: ToolSpec,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        if spec.network_access or spec.risk_class not in {
            RiskClass.LOCAL_READ,
            RiskClass.WORKSPACE_WRITE,
        }:
            raise ValueError("analysis tools cannot declare network or active risk")
        self.spec = spec
        self._handler = handler

    async def execute(
        self,
        invocation: ToolInvocation,
        runner: SandboxRunner,
    ) -> ToolExecutionResult:
        del runner
        return ToolExecutionResult(output=await self._handler(invocation.arguments))


CommandBuilder = Callable[[dict[str, Any]], list[str]]
OutputParser = Callable[[str, str, int | None], dict[str, Any]]


class SandboxCommandTool(ToolPlugin):
    """An argv-only command adapter; no shell or host process is available."""

    def __init__(
        self,
        spec: ToolSpec,
        *,
        output_parser: OutputParser,
        image: str | None = None,
        command_builder: CommandBuilder | None = None,
        network_name: str | None = None,
    ) -> None:
        if spec.input_schema.get("additionalProperties") is not False:
            raise ValueError(
                "executable tool schemas must set additionalProperties=false"
            )
        self.spec = spec
        selected_image = image or spec.image
        if selected_image is None:
            raise ValueError("command tools require an image")
        self.image: str = selected_image
        self.command_builder = command_builder or (
            lambda arguments: build_declared_command(spec, arguments)
        )
        self.output_parser = output_parser
        self.network_name = network_name

    async def execute(
        self,
        invocation: ToolInvocation,
        runner: SandboxRunner,
    ) -> ToolExecutionResult:
        command = self.command_builder(invocation.arguments)
        if not command or any(not isinstance(value, str) for value in command):
            raise InvalidToolArguments(
                "command adapter must return a non-empty argv list"
            )
        pins: dict[str, str] = {}
        if invocation.target and invocation.resolved_ips:
            parsed = urlsplit(invocation.target)
            host = (
                parsed.hostname
                if parsed.scheme
                else invocation.target.split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
            )
            if host is None:
                raise InvalidToolArguments("network target URL requires a hostname")
            host = host.rstrip(".").lower()
            # The first address is used in /etc/hosts; the egress boundary must
            # independently allow only the complete policy-approved set.
            pins[host] = invocation.resolved_ips[0]
        egress_rules: list[EgressRule] = []
        if self.spec.network_access:
            if not invocation.resolved_ips:
                raise InvalidToolArguments(
                    "network tools require broker-resolved destination addresses"
                )
            ports = _egress_ports(self.spec, invocation.arguments, invocation.target)
            if not ports:
                raise InvalidToolArguments(
                    "network tools require policy-mapped destination ports"
                )
            egress_rules = [
                EgressRule(address=address, ports=ports)
                for address in invocation.resolved_ips
            ]
        result = await runner.run(
            SandboxRequest(
                image=self.image,
                command=command,
                workspace=invocation.workspace,
                workspace_access=SandboxWorkspaceAccess(self.spec.filesystem_access),
                network=(
                    SandboxNetwork.SCOPED
                    if self.spec.network_access
                    else SandboxNetwork.NONE
                ),
                execution_kind=(
                    SandboxExecutionKind.NETWORK_TOOL
                    if self.spec.network_access
                    else SandboxExecutionKind.LOCAL_TOOL
                ),
                egress_rules=egress_rules,
                pinned_hosts=pins,
                limits=self.spec.resource_limits.model_copy(
                    update={"timeout_seconds": self.spec.timeout_seconds}
                ),
            )
        )
        parser_error: str | None = None
        try:
            output = self.output_parser(result.stdout, result.stderr, result.exit_code)
        except Exception as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_003",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tools",
            )
            output = {}
            parser_error = _bounded_execution_error(exc)
        return ToolExecutionResult(
            output=output,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            output_truncated=result.output_truncated,
            parser_error=parser_error,
            execution={
                "command": result.command,
                "image": result.image,
                "runtime": result.runtime,
                "started_at": result.started_at.isoformat(),
                "completed_at": result.completed_at.isoformat(),
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
            },
        )


def _egress_ports(
    spec: ToolSpec, arguments: dict[str, Any], target: str | None
) -> list[int]:
    ports: list[int] = []
    if spec.port_argument and spec.port_argument in arguments:
        value = arguments[spec.port_argument]
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, bool) or not isinstance(candidate, int):
                raise InvalidToolArguments("mapped destination ports must be integers")
            if not 1 <= candidate <= 65535:
                raise InvalidToolArguments(
                    "mapped destination ports must be between 1 and 65535"
                )
            ports.append(candidate)
    if not ports and target:
        parsed = urlsplit(target)
        if parsed.scheme in {"http", "https"}:
            try:
                ports.append(parsed.port or (443 if parsed.scheme == "https" else 80))
            except ValueError as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_004",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="tools",
                )
                raise InvalidToolArguments(
                    "target URL contains an invalid port"
                ) from exc
    return sorted(set(ports))


class ToolLedger(Protocol):
    async def reserve(
        self,
        invocation: ToolInvocation,
        spec: ToolSpec,
    ) -> PersistedToolCall: ...

    async def transition(
        self,
        call: PersistedToolCall,
        status: ToolCallStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        approval_id: str | None = None,
    ) -> PersistedToolCall: ...

    async def request_approval(
        self,
        call: PersistedToolCall,
        invocation: ToolInvocation,
        decision: PolicyDecision,
        spec: ToolSpec,
    ) -> Approval: ...

    async def get_approval(self, approval_id: str) -> Approval: ...

    async def expire_approval(self, approval: Approval) -> Approval: ...


class ToolEvidenceRecorder(Protocol):
    async def record(
        self,
        call: PersistedToolCall,
        invocation: ToolInvocation,
        spec: ToolSpec,
        result: ToolExecutionResult,
    ) -> list[str]: ...


class StoreToolLedger:
    """Persist every state before the broker exposes or acts on it."""

    def __init__(self, store: NebulaStore, *, enforce_run_budget: bool = True) -> None:
        self.store = store
        self.enforce_run_budget = enforce_run_budget

    @staticmethod
    def _call_id(invocation: ToolInvocation) -> str:
        if invocation.idempotency_key:
            return str(
                uuid5(
                    NAMESPACE_URL,
                    f"nebula:{invocation.run_id}:{invocation.idempotency_key}",
                )
            )
        return invocation.id

    async def reserve(
        self,
        invocation: ToolInvocation,
        spec: ToolSpec,
    ) -> PersistedToolCall:
        call_id = self._call_id(invocation)
        proposed = PersistedToolCall(
            id=call_id,
            engagement_id=invocation.engagement_id,
            run_id=invocation.run_id,
            origin=invocation.origin,
            chat_session_id=invocation.chat_session_id,
            chat_turn_id=invocation.chat_turn_id,
            task_id=invocation.task_id,
            tool_name=invocation.tool_name,
            risk_class=spec.risk_class,
            arguments=invocation.arguments,
            idempotency_key=invocation.idempotency_key,
        )
        try:
            if self.enforce_run_budget:
                call = await asyncio.to_thread(self.store.reserve_tool_call, proposed)
                if (
                    call.tool_name != proposed.tool_name
                    or call.arguments != proposed.arguments
                    or call.run_id != proposed.run_id
                ):
                    raise ToolBrokerError(
                        "idempotency key was reused for a different request"
                    )
            else:
                call = await asyncio.to_thread(self.store.create, proposed)
            await asyncio.to_thread(
                self.store.append_event,
                invocation.run_id,
                "tool.proposed",
                {"tool_call": call.model_dump(mode="json")},
                actor_id=invocation.requested_by,
                idempotency_key=f"tool:{call.id}:proposed",
            )
            return call
        except ConflictError as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_005",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tools",
            )
            existing = await asyncio.to_thread(
                self.store.get, PersistedToolCall, call_id
            )
            if (
                existing.tool_name != proposed.tool_name
                or existing.arguments != proposed.arguments
                or existing.run_id != proposed.run_id
            ):
                raise ToolBrokerError(
                    "idempotency key was reused for a different request"
                )
            return existing

    async def transition(
        self,
        call: PersistedToolCall,
        status: ToolCallStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        approval_id: str | None = None,
    ) -> PersistedToolCall:
        changes: dict[str, Any] = {"status": status}
        if result is not None:
            changes["result"] = result
        if error is not None:
            changes["error"] = error
        if approval_id is not None:
            changes["approval_id"] = approval_id
        if status == ToolCallStatus.RUNNING:
            changes["started_at"] = utc_now()
        if status in {
            ToolCallStatus.COMPLETE,
            ToolCallStatus.FAILED,
            ToolCallStatus.DENIED,
            ToolCallStatus.CANCELLED,
        }:
            changes["completed_at"] = utc_now()
        updated, _ = await asyncio.to_thread(
            self.store.update_with_event,
            PersistedToolCall,
            call.id,
            changes,
            expected_revision=call.revision,
            run_id=call.run_id,
            event_type=f"tool.{status.value}",
            event_payload={"tool_call_id": call.id, "status": status.value},
            idempotency_key=(f"tool:{call.id}:{call.revision + 1}:{status.value}"),
        )
        return updated

    async def request_approval(
        self,
        call: PersistedToolCall,
        invocation: ToolInvocation,
        decision: PolicyDecision,
        spec: ToolSpec,
    ) -> Approval:
        approval_id = str(uuid5(NAMESPACE_URL, f"nebula:approval:{call.id}"))
        try:
            approval = await asyncio.to_thread(self.store.get, Approval, approval_id)
        except NotFoundError as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_006",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tools",
            )
            exact_request: dict[str, Any] = {
                "tool_name": invocation.tool_name,
                "arguments": invocation.arguments,
            }
            if spec.executable is not None:
                exact_request.update(
                    {
                        "argv": build_declared_command(spec, invocation.arguments),
                        "pack_id": spec.pack_id,
                        "manifest_digest": spec.manifest_digest,
                        "image": spec.image,
                    }
                )
            approval = Approval(
                id=approval_id,
                engagement_id=invocation.engagement_id,
                run_id=invocation.run_id,
                origin=invocation.origin,
                chat_session_id=invocation.chat_session_id,
                chat_turn_id=invocation.chat_turn_id,
                task_id=invocation.task_id,
                tool_call_id=call.id,
                risk_class=spec.risk_class,
                exact_request=exact_request,
                target=invocation.target,
                credential_class=invocation.credential_class,
                expected_effects=[spec.description],
                policy_rationale=decision.reason,
                requested_by=invocation.requested_by,
            )
            approval = await asyncio.to_thread(self.store.create, approval)
            await asyncio.to_thread(
                self.store.append_event,
                call.run_id,
                "approval.requested",
                {"approval": approval.model_dump(mode="json")},
                actor_id=invocation.requested_by,
                idempotency_key=f"approval:{approval.id}:requested",
            )
        if call.status != ToolCallStatus.WAITING_APPROVAL:
            await self.transition(
                call,
                ToolCallStatus.WAITING_APPROVAL,
                approval_id=approval.id,
            )
        return approval

    async def get_approval(self, approval_id: str) -> Approval:
        return await asyncio.to_thread(self.store.get, Approval, approval_id)

    async def expire_approval(self, approval: Approval) -> Approval:
        updated, _ = await asyncio.to_thread(
            self.store.update_with_event,
            Approval,
            approval.id,
            {
                "status": ApprovalStatus.EXPIRED,
                "decided_by": "system",
                "decided_at": utc_now(),
                "decision_note": "approval expired before execution",
            },
            expected_revision=approval.revision,
            run_id=approval.run_id,
            event_type="approval.expired",
            event_payload={
                "approval_id": approval.id,
                "status": ApprovalStatus.EXPIRED.value,
            },
            actor_id="system",
            idempotency_key=f"approval:{approval.id}:expired",
        )
        return updated


class StoreToolEvidenceRecorder:
    """Capture a canonical immutable execution envelope as Artifact + Evidence."""

    def __init__(self, store: NebulaStore, artifact_store: ArtifactStore) -> None:
        self.store = store
        self.artifact_store = artifact_store

    async def record(
        self,
        call: PersistedToolCall,
        invocation: ToolInvocation,
        spec: ToolSpec,
        result: ToolExecutionResult,
    ) -> list[str]:
        toolbox_metadata = result.output.get("metadata")
        if not isinstance(toolbox_metadata, dict):
            toolbox_metadata = {}
        interface_catalog_digest = toolbox_metadata.get("catalog_digest")
        script_sha256 = toolbox_metadata.get("script_sha256")
        if not isinstance(interface_catalog_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", interface_catalog_digest
        ):
            interface_catalog_digest = None
        if not isinstance(script_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", script_sha256
        ):
            script_sha256 = None
        envelope = {
            "schema": "nebula.tool-evidence.v1",
            "tool_call_id": call.id,
            "run_id": call.run_id,
            "task_id": call.task_id,
            "tool": {"name": spec.name, "version": spec.version},
            "tool_pack": {
                "identity": spec.pack_id,
                "manifest_digest": spec.manifest_digest,
                "image": spec.image,
                "parser": spec.parser_contract,
                "interface_catalog_digest": interface_catalog_digest,
            },
            "risk_class": spec.risk_class.value,
            "arguments": invocation.arguments,
            "target": invocation.target,
            "resolved_ips": invocation.resolved_ips,
            "output": result.output,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "output_truncated": result.output_truncated,
            "parser_error": result.parser_error,
            "execution": result.execution,
            "script_sha256": script_sha256,
        }
        payload = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        stored = await asyncio.to_thread(
            self.artifact_store.put_bytes_with_status,
            payload,
            engagement_id=invocation.engagement_id,
            filename=f"tool-call-{call.id}.json",
            media_type="application/json",
            source=f"tool:{spec.name}@{spec.version}",
            metadata={
                "tool_call_id": call.id,
                "run_id": call.run_id,
                "tool_pack": spec.pack_id,
                "manifest_digest": spec.manifest_digest,
                "image": spec.image,
                "interface_catalog_digest": interface_catalog_digest,
                "script_sha256": script_sha256,
            },
        )
        evidence = Evidence(
            engagement_id=invocation.engagement_id,
            evidence_type="tool_execution",
            title=f"{spec.name} execution",
            artifact_id=stored.artifact.id,
            tool_call_id=call.id,
            sha256=stored.artifact.sha256,
            captured_by=invocation.requested_by,
            source_version=(
                f"{spec.pack_id}:{spec.name}@{spec.version}"
                if spec.pack_id
                else f"{spec.name}@{spec.version}"
            ),
            metadata={
                "target": invocation.target,
                "image": result.execution.get("image"),
                "manifest_digest": spec.manifest_digest,
                "tool_pack": spec.pack_id,
                "interface_catalog_digest": interface_catalog_digest,
                "script_sha256": script_sha256,
                "exit_code": result.exit_code,
            },
        )
        try:
            await asyncio.to_thread(self.store.create_many, [stored.artifact, evidence])
        except Exception as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_007",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tools",
            )
            await asyncio.to_thread(self.artifact_store.discard_new_blob, stored)
            raise
        return [evidence.id]


class ToolRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ToolPlugin] = {}

    def register(self, plugin: ToolPlugin) -> None:
        if plugin.spec.name in self._plugins:
            raise ValueError(f"tool is already registered: {plugin.spec.name}")
        self._plugins[plugin.spec.name] = plugin

    def get(self, name: str) -> ToolPlugin:
        try:
            return self._plugins[name]
        except KeyError as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_008",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tools",
            )
            raise UnknownTool(name) from exc

    def specs(self) -> list[ToolSpec]:
        return [plugin.spec for plugin in self._plugins.values()]


class ToolBroker:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        runner: SandboxRunner,
        ledger: ToolLedger,
        workspace_resolver: Callable[[str], Path],
        dns_resolver: Callable[[str], Awaitable[list[str]]] | None = None,
        evidence_recorder: ToolEvidenceRecorder | None = None,
    ) -> None:
        self.registry = registry
        self.policy_engine = policy_engine
        self.runner = runner
        self.ledger = ledger
        self.workspace_resolver = workspace_resolver
        self.dns_resolver = dns_resolver or _resolve_addresses
        self.evidence_recorder = evidence_recorder
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
    ) -> PreparedToolCall:
        plugin = self.registry.get(invocation.tool_name)
        self._validate(plugin.spec.input_schema, invocation.arguments, "input")
        invocation = await self._canonicalize(invocation, plugin.spec)
        if (
            plugin.spec.idempotency == IdempotencyBehavior.KEY_REQUIRED
            and not invocation.idempotency_key
        ):
            raise InvalidToolArguments("this tool requires an idempotency key")
        call = await self.ledger.reserve(invocation, plugin.spec)
        if call.status == ToolCallStatus.COMPLETE and call.result is not None:
            cached = ToolExecutionResult.model_validate(call.result)
            return PreparedToolCall(
                call=call,
                decision=PolicyDecision(
                    effect=PolicyEffect.ALLOW,
                    reason="completed idempotent call was replayed",
                    rule="idempotent_replay",
                ),
                invocation=invocation,
                cached_result=cached,
            )
        if call.status in {
            ToolCallStatus.RUNNING,
            ToolCallStatus.FAILED,
            ToolCallStatus.CANCELLED,
            ToolCallStatus.DENIED,
        }:
            raise AmbiguousToolState(
                f"tool call {call.id} is {call.status.value}; create an explicit new retry request"
            )
        decision = self.policy_engine.evaluate(
            scope,
            PolicyRequest(
                tool_name=plugin.spec.name,
                risk_class=plugin.spec.risk_class,
                target=invocation.target,
                port=invocation.port,
                ports=_mapped_ports(plugin.spec, invocation.arguments),
                resolved_ips=invocation.resolved_ips,
                credential_class=invocation.credential_class,
                writes_outside_workspace=False,
                action=plugin.spec.action,
                cloud_transfer=plugin.spec.cloud_transfer,
            ),
        )
        if decision.effect == PolicyEffect.ALLOW and plugin.spec.requires_approval:
            decision = PolicyDecision(
                effect=PolicyEffect.REQUIRE_APPROVAL,
                reason="the installed capability explicitly requires operator approval",
                rule="tool_contract_approval",
                normalized_target=decision.normalized_target,
                matched_grant_index=decision.matched_grant_index,
            )
        if decision.effect == PolicyEffect.DENY:
            await self.ledger.transition(
                call, ToolCallStatus.DENIED, error=decision.reason
            )
            raise PolicyDenied(decision)
        if decision.effect == PolicyEffect.REQUIRE_APPROVAL:
            approval = await self.ledger.request_approval(
                call, invocation, decision, plugin.spec
            )
            return PreparedToolCall(
                call=call,
                decision=decision,
                invocation=invocation,
                approval=approval,
            )
        return PreparedToolCall(call=call, decision=decision, invocation=invocation)

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Approval | None = None,
    ) -> ToolExecutionResult:
        lock_key = invocation.idempotency_key or invocation.id
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            prepared = await self.prepare(invocation, scope)
            invocation = prepared.invocation
            if prepared.cached_result is not None:
                return prepared.cached_result
            if prepared.approval is not None:
                if approval is not None and approval.id != prepared.approval.id:
                    raise ToolBrokerError("approval does not belong to this tool call")
                # Only the durable operator-authored record is authoritative.
                # A model/client cannot promote the pending object it received.
                supplied = await self.ledger.get_approval(prepared.approval.id)
                if supplied.expires_at is not None and supplied.expires_at <= utc_now():
                    supplied = await self.ledger.expire_approval(supplied)
                    expired = PolicyDecision(
                        effect=PolicyEffect.DENY,
                        reason="operator approval expired before execution",
                        rule="approval_expired",
                    )
                    await self.ledger.transition(
                        prepared.call,
                        ToolCallStatus.DENIED,
                        error=expired.reason,
                    )
                    raise PolicyDenied(expired)
                if supplied.status not in {
                    ApprovalStatus.APPROVED,
                    ApprovalStatus.EDITED,
                }:
                    if supplied.status in {
                        ApprovalStatus.REJECTED,
                        ApprovalStatus.EXPIRED,
                        ApprovalStatus.CANCELLED,
                    }:
                        await self.ledger.transition(
                            prepared.call,
                            ToolCallStatus.DENIED,
                            error=f"approval {supplied.status.value}",
                        )
                        raise PolicyDenied(
                            PolicyDecision(
                                effect=PolicyEffect.DENY,
                                reason=f"operator {supplied.status.value} the request",
                                rule="approval_decision",
                            )
                        )
                    raise ApprovalRequired(prepared.approval)
                if not supplied.decided_by or supplied.decided_at is None:
                    raise ToolBrokerError(
                        "durable approvals require operator identity and decision time"
                    )
                invocation = self._apply_approved_edit(invocation, supplied)
                plugin = self.registry.get(invocation.tool_name)
                self._validate(
                    plugin.spec.input_schema, invocation.arguments, "edited input"
                )
                invocation = await self._canonicalize(invocation, plugin.spec)
                # Any edit receives a fresh deterministic policy evaluation.
                if supplied.status == ApprovalStatus.EDITED:
                    edited = self.policy_engine.evaluate(
                        scope,
                        PolicyRequest(
                            tool_name=plugin.spec.name,
                            risk_class=plugin.spec.risk_class,
                            target=invocation.target,
                            port=invocation.port,
                            ports=_mapped_ports(plugin.spec, invocation.arguments),
                            resolved_ips=invocation.resolved_ips,
                            credential_class=invocation.credential_class,
                            action=plugin.spec.action,
                            cloud_transfer=plugin.spec.cloud_transfer,
                        ),
                    )
                    if edited.effect == PolicyEffect.DENY:
                        raise PolicyDenied(edited)
            plugin = self.registry.get(invocation.tool_name)
            running = await self.ledger.transition(
                prepared.call, ToolCallStatus.RUNNING
            )
            try:
                result = await plugin.execute(invocation, self.runner)
                if not isinstance(plugin, AnalysisTool):
                    if self.evidence_recorder is None:
                        raise ToolBrokerError(
                            "executable tools require an immutable evidence recorder"
                        )
                    evidence_ids = await self.evidence_recorder.record(
                        running, invocation, plugin.spec, result
                    )
                    result = result.model_copy(update={"evidence_ids": evidence_ids})
                if result.parser_error is not None:
                    raise ToolBrokerError(
                        f"tool output parsing failed: {result.parser_error}"
                    )
                self._validate(plugin.spec.output_schema, result.output, "output")
            except asyncio.CancelledError as caught_error:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_009",
                    "A handled toolbox operation raised an exception.",
                    caught_error,
                    stage="tools",
                )
                await self.ledger.transition(running, ToolCallStatus.CANCELLED)
                raise
            except Exception as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_010",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="tools",
                )
                await self.ledger.transition(
                    running, ToolCallStatus.FAILED, error=str(exc)
                )
                raise
            await self.ledger.transition(
                running,
                ToolCallStatus.COMPLETE,
                result=result.model_dump(mode="json"),
            )
            return result

    async def _canonicalize(
        self, invocation: ToolInvocation, spec: ToolSpec
    ) -> ToolInvocation:
        workspace = (
            self.workspace_resolver(invocation.engagement_id)
            .expanduser()
            .resolve(strict=True)
        )
        supplied_workspace = invocation.workspace.expanduser().resolve(strict=False)
        if supplied_workspace != workspace:
            raise InvalidToolArguments(
                "tool workspace does not match the engagement-owned workspace"
            )
        arguments = dict(invocation.arguments)
        target: str | None = None
        if spec.target_argument:
            value = arguments.get(spec.target_argument)
            if not isinstance(value, str) or not value.strip():
                raise InvalidToolArguments(
                    "mapped tool target must be a non-empty string"
                )
            target = value.strip()
            if invocation.target and invocation.target.strip() != target:
                raise InvalidToolArguments(
                    "caller target does not match the tool's mapped target argument"
                )
        elif invocation.target is not None:
            raise InvalidToolArguments("this tool does not declare a target argument")

        ports = _mapped_ports(spec, arguments)
        port = ports[0] if len(ports) == 1 else None
        if invocation.port is not None and invocation.port not in ports:
            raise InvalidToolArguments(
                "caller port does not match the tool's mapped port argument"
            )
        for field in spec.path_arguments:
            values = arguments.get(field)
            paths = values if isinstance(values, list) else [values]
            container_paths: list[str] = []
            for value in paths:
                if not isinstance(value, str):
                    raise InvalidToolArguments(
                        f"mapped path argument {field!r} is invalid"
                    )
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                candidate = candidate.expanduser().resolve(strict=False)
                if candidate != workspace and workspace not in candidate.parents:
                    raise InvalidToolArguments(
                        f"mapped path argument {field!r} escapes the engagement workspace"
                    )
                if spec.filesystem_access == "read" and not candidate.exists():
                    raise InvalidToolArguments(
                        f"mapped path argument {field!r} does not exist"
                    )
                relative = candidate.relative_to(workspace)
                container_paths.append(
                    "/workspace"
                    if relative == Path(".")
                    else f"/workspace/{relative.as_posix()}"
                )
            arguments[field] = (
                container_paths if isinstance(values, list) else container_paths[0]
            )
        if invocation.credential_class and (
            invocation.credential_class not in spec.credential_classes
        ):
            raise InvalidToolArguments(
                "requested credential class is not declared by this tool"
            )

        resolved_ips: list[str] = []
        if target:
            host = _target_host(target)
            try:
                resolved_ips = [str(ipaddress.ip_address(host))]
            except ValueError as caught_error:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_011",
                    "A handled toolbox operation raised an exception.",
                    caught_error,
                    stage="tools",
                )
                resolved_ips = sorted(
                    {
                        str(ipaddress.ip_address(value))
                        for value in await self.dns_resolver(host)
                    }
                )
                if not resolved_ips and spec.network_access:
                    raise InvalidToolArguments("target hostname did not resolve")
        return invocation.model_copy(
            update={
                "workspace": workspace,
                "arguments": arguments,
                "target": target,
                "port": port,
                "resolved_ips": resolved_ips,
            }
        )

    @staticmethod
    def _validate(schema: dict[str, Any], value: Any, label: str) -> None:
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as exc:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_012",
                "A handled toolbox operation raised an exception.",
                exc,
                stage="tools",
            )
            path = ".".join(str(part) for part in exc.absolute_path)
            where = f" at {path}" if path else ""
            raise InvalidToolArguments(
                f"invalid tool {label}{where}: {exc.message}"
            ) from exc

    @staticmethod
    def _apply_approved_edit(
        invocation: ToolInvocation, approval: Approval
    ) -> ToolInvocation:
        if approval.status != ApprovalStatus.EDITED:
            return invocation
        request = approval.exact_request
        if request.get("tool_name") != invocation.tool_name:
            raise ToolBrokerError("an approval edit cannot change the tool identity")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            raise ToolBrokerError("edited approval does not contain object arguments")
        return invocation.model_copy(update={"arguments": arguments})


def invocation_digest(invocation: ToolInvocation) -> str:
    """Stable identifier suitable for audit display and external approval cards."""

    payload = invocation.model_dump(mode="json", exclude={"id", "workspace"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bounded_execution_error(exc: Exception) -> str:
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {detail}"[:1_000]


def _mapped_ports(spec: ToolSpec, arguments: dict[str, Any]) -> list[int]:
    if not spec.port_argument:
        return []
    value = arguments.get(spec.port_argument)
    values = value if isinstance(value, list) else [value]
    if not values or any(
        isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65535
        for item in values
    ):
        raise InvalidToolArguments(
            "mapped port argument must contain valid TCP/UDP ports"
        )
    return sorted(set(values))


def _target_host(target: str) -> str:
    candidate = target.strip()
    if "://" in candidate:
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidToolArguments(
                "mapped target must be an IP, hostname, or HTTP(S) URL"
            )
        return parsed.hostname.rstrip(".").lower()
    if candidate.startswith("[") and "]" in candidate:
        return candidate[1 : candidate.index("]")]
    if candidate.count(":") == 1 and candidate.rsplit(":", 1)[1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]
    return candidate.rstrip(".").lower()


async def _resolve_addresses(host: str) -> list[str]:
    def resolve() -> list[str]:
        return [
            str(result[4][0])
            for result in socket.getaddrinfo(
                host,
                None,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        ]

    try:
        return await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        record_caught_exception(
            "toolbox",
            "toolbox.tools.caught_failure_013",
            "A handled toolbox operation raised an exception.",
            exc,
            stage="tools",
        )
        raise InvalidToolArguments(f"could not resolve mapped target {host!r}") from exc


__all__ = [
    "AnalysisTool",
    "AmbiguousToolState",
    "ApprovalRequired",
    "IdempotencyBehavior",
    "InvalidToolArguments",
    "PolicyDenied",
    "PreparedToolCall",
    "SandboxCommandTool",
    "StoreToolLedger",
    "ToolBroker",
    "ToolExecutionResult",
    "ToolInvocation",
    "ToolPlugin",
    "ToolRegistry",
    "ToolSpec",
    "UnknownTool",
    "invocation_digest",
]
