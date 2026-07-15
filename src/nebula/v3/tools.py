"""Typed tool plugins and the mandatory policy/approval execution broker."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator
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
    Artifact,
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
from .tool_results import (
    MAX_CAPTURE_BYTES,
    MAX_GENERATED_BYTES,
    MAX_GENERATED_FILES,
    MAX_MODEL_ARTIFACT_REFS,
    ParserState,
    StreamCapture,
    ToolParserReceipt,
    ToolResultStatus,
    ToolTimingReceipt,
    ToolResultReceipt,
    ToolOutputService,
    WorkspaceOutputService,
    artifact_ref,
    bytes_are_searchable,
)


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
    budget_class: Literal["execution", "artifact_query"] = "execution"
    capture_paths: list[str] = Field(default_factory=list, max_length=32)

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
        for value in self.capture_paths:
            path = Path(value)
            if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
                raise ValueError("capture paths must be fixed workspace-relative paths")
        if len(self.capture_paths) != len(set(self.capture_paths)):
            raise ValueError("capture paths must be unique")
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
        oci_execution_fields = (self.image, self.executable, self.manifest_digest)
        if any(value is not None for value in oci_execution_fields):
            if (
                any(value is None for value in oci_execution_fields)
                or self.pack_id is None
            ):
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
    receipt: ToolResultReceipt | None = None
    result_artifact_id: str | None = None
    stdout_artifact_path: Path | None = Field(default=None, exclude=True)
    stderr_artifact_path: Path | None = Field(default=None, exclude=True)
    output_directory: Path | None = Field(default=None, exclude=True)
    mcp_content_blocks: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    observed_stdout_bytes: int = Field(default=0, ge=0)
    observed_stderr_bytes: int = Field(default=0, ge=0)
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def model_result(self) -> dict[str, Any]:
        return (
            self.receipt.as_model_result() if self.receipt is not None else self.output
        )


def _legacy_action_receipt(
    call: PersistedToolCall, spec: ToolSpec
) -> ToolResultReceipt:
    """Keep pre-v2 action bytes durable without replaying them into model context."""

    legacy = call.result if isinstance(call.result, dict) else {}
    execution = legacy.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    exit_code = legacy.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        nested = legacy.get("output")
        nested_exit = nested.get("exit_code") if isinstance(nested, dict) else None
        exit_code = (
            nested_exit
            if isinstance(nested_exit, int) and not isinstance(nested_exit, bool)
            else None
        )
    return ToolResultReceipt(
        tool_call_id=call.id,
        tool_name=spec.name,
        tool_version=spec.version,
        status=ToolResultStatus.COMPLETED,
        exit_code=exit_code,
        timing=ToolTimingReceipt(
            started_at=(
                str(execution["started_at"])
                if execution.get("started_at") is not None
                else None
            ),
            completed_at=(
                str(execution["completed_at"])
                if execution.get("completed_at") is not None
                else None
            ),
            duration_seconds=(
                float(execution["duration_seconds"])
                if isinstance(execution.get("duration_seconds"), (int, float))
                else None
            ),
        ),
        incomplete=True,
        warnings=[
            "Historical pre-v2 action output was retained unchanged and omitted from model context."
        ],
    )


class ToolExecutionCancelled(asyncio.CancelledError):
    """Carry partial captured evidence through cooperative cancellation."""

    def __init__(self, result: ToolExecutionResult) -> None:
        super().__init__("tool execution cancelled")
        self.result = result


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


class InvocationAnalysisTool(AnalysisTool):
    """A trusted bounded retriever that also needs owner/workspace context."""

    def __init__(
        self,
        spec: ToolSpec,
        handler: Callable[[ToolInvocation], Awaitable[dict[str, Any]]],
    ) -> None:
        super().__init__(
            spec, lambda arguments: _unreachable_analysis_handler(arguments)
        )
        self._invocation_handler = handler

    async def execute(
        self,
        invocation: ToolInvocation,
        runner: SandboxRunner,
    ) -> ToolExecutionResult:
        del runner
        return ToolExecutionResult(output=await self._invocation_handler(invocation))


async def _unreachable_analysis_handler(arguments: dict[str, Any]) -> dict[str, Any]:
    del arguments
    raise AssertionError("invocation analysis handler was called without context")


CommandBuilder = Callable[[dict[str, Any]], list[str]]
OutputParser = Callable[[str, str, int | None], dict[str, Any]]


class SandboxCommandTool(ToolPlugin):
    """An argv-only command adapter; no shell or host process is available."""

    def __init__(
        self,
        spec: ToolSpec,
        *,
        output_parser: OutputParser | None = None,
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
        workspace = invocation.workspace.expanduser().resolve(strict=True)
        output_directory = Path(
            tempfile.mkdtemp(
                prefix=f".nebula-output-{invocation.id[:12]}-", dir=workspace
            )
        )
        # The engagement workspace is protected by its host-only parent, while
        # the rootless container runs as an unmapped non-root UID and needs to
        # create files in this one dedicated bind mount.
        output_directory.chmod(0o777)
        stdout_descriptor, stdout_name = tempfile.mkstemp(
            prefix=f"nebula-{invocation.id[:12]}-stdout-"
        )
        stderr_descriptor, stderr_name = tempfile.mkstemp(
            prefix=f"nebula-{invocation.id[:12]}-stderr-"
        )
        stdout_stream = os.fdopen(stdout_descriptor, "w+b")
        stderr_stream = os.fdopen(stderr_descriptor, "w+b")
        stdout_capture = StreamCapture(stdout_stream, limit=MAX_CAPTURE_BYTES)
        stderr_capture = StreamCapture(stderr_stream, limit=MAX_CAPTURE_BYTES)
        started_at = utc_now()

        async def capture(stream: str, chunk: bytes) -> None:
            (stdout_capture if stream == "stdout" else stderr_capture).write(chunk)

        command = [value.replace("{output_dir}", "/nebula-output") for value in command]
        try:
            result = await runner.run_stream(
                SandboxRequest(
                    image=self.image,
                    command=command,
                    workspace=invocation.workspace,
                    workspace_access=SandboxWorkspaceAccess(
                        self.spec.filesystem_access
                    ),
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
                    output_directory=output_directory,
                    retain_output=self.output_parser is not None,
                    environment={"NEBULA_OUTPUT_DIR": "/nebula-output"},
                    limits=self.spec.resource_limits.model_copy(
                        update={"timeout_seconds": self.spec.timeout_seconds}
                    ),
                ),
                on_chunk=capture,
            )
            stdout_capture.flush()
            stderr_capture.flush()
        except asyncio.CancelledError:
            stdout_capture.flush()
            stderr_capture.flush()
            completed_at = utc_now()
            raise ToolExecutionCancelled(
                ToolExecutionResult(
                    output={},
                    exit_code=None,
                    output_truncated=(
                        stdout_capture.truncated or stderr_capture.truncated
                    ),
                    stdout_artifact_path=Path(stdout_name),
                    stderr_artifact_path=Path(stderr_name),
                    output_directory=output_directory,
                    observed_stdout_bytes=stdout_capture.observed_bytes,
                    observed_stderr_bytes=stderr_capture.observed_bytes,
                    stdout_truncated=stdout_capture.truncated,
                    stderr_truncated=stderr_capture.truncated,
                    execution={
                        "command": command,
                        "image": self.image,
                        "runtime": "container",
                        "started_at": started_at.isoformat(),
                        "completed_at": completed_at.isoformat(),
                        "duration_seconds": max(
                            0.0, (completed_at - started_at).total_seconds()
                        ),
                        "timed_out": False,
                        "cancelled": True,
                    },
                )
            )
        except BaseException:
            stdout_stream.close()
            stderr_stream.close()
            Path(stdout_name).unlink(missing_ok=True)
            Path(stderr_name).unlink(missing_ok=True)
            shutil.rmtree(output_directory, ignore_errors=True)
            raise
        finally:
            if not stdout_stream.closed:
                stdout_stream.close()
            if not stderr_stream.closed:
                stderr_stream.close()
        parser_error: str | None = None
        output: dict[str, Any] = {}
        if self.output_parser is not None and result.output_truncated:
            parser_error = (
                "parser input exceeded its bounded stdout/stderr buffer; "
                "raw captured artifacts remain available"
            )
        elif self.output_parser is not None:
            try:
                output = self.output_parser(
                    result.stdout, result.stderr, result.exit_code
                )
            except Exception as exc:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_003",
                    "A handled toolbox operation raised an exception.",
                    exc,
                    stage="tools",
                )
                parser_error = _bounded_execution_error(exc)
        effective_stdout = result.stdout
        effective_stderr = result.stderr
        effective_exit_code = result.exit_code
        effective_timed_out = result.timed_out
        legacy_envelope = output.get("protocol") == "nebula.toolbox/v1"
        if legacy_envelope:
            if isinstance(output.get("stdout"), str):
                effective_stdout = output["stdout"]
            if isinstance(output.get("stderr"), str):
                effective_stderr = output["stderr"]
            if isinstance(output.get("exit_code"), int):
                effective_exit_code = output["exit_code"]
            effective_timed_out = bool(output.get("timed_out", effective_timed_out))
            Path(stdout_name).unlink(missing_ok=True)
            Path(stderr_name).unlink(missing_ok=True)
        return ToolExecutionResult(
            output=output,
            stdout=effective_stdout,
            stderr=effective_stderr,
            exit_code=effective_exit_code,
            output_truncated=(stdout_capture.truncated or stderr_capture.truncated),
            parser_error=parser_error,
            stdout_artifact_path=None if legacy_envelope else Path(stdout_name),
            stderr_artifact_path=None if legacy_envelope else Path(stderr_name),
            output_directory=output_directory,
            observed_stdout_bytes=(
                len(effective_stdout.encode("utf-8"))
                if legacy_envelope
                else stdout_capture.observed_bytes
            ),
            observed_stderr_bytes=(
                len(effective_stderr.encode("utf-8"))
                if legacy_envelope
                else stderr_capture.observed_bytes
            ),
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            execution={
                "command": result.command,
                "image": result.image,
                "runtime": result.runtime,
                "started_at": result.started_at.isoformat(),
                "completed_at": result.completed_at.isoformat(),
                "duration_seconds": result.duration_seconds,
                "timed_out": effective_timed_out,
                "legacy_toolbox_envelope": legacy_envelope,
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
        result_artifact_id: str | None = None,
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
    ) -> ToolExecutionResult: ...


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
            metadata={"budget_class": spec.budget_class},
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
        result_artifact_id: str | None = None,
    ) -> PersistedToolCall:
        changes: dict[str, Any] = {"status": status}
        if result is not None:
            changes["result"] = result
        if error is not None:
            changes["error"] = error
        if approval_id is not None:
            changes["approval_id"] = approval_id
        if result_artifact_id is not None:
            changes["result_artifact_id"] = result_artifact_id
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


def _path_has_symlink_component(root: Path, candidate: Path) -> bool:
    """Reject generated evidence reached through any symlink component."""

    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _regular_files_beneath(root: Path) -> Iterator[Path]:
    """Walk regular files deterministically without materializing a full tree."""

    if root.is_file() and not root.is_symlink():
        yield root
        return
    for directory, child_directories, filenames in os.walk(
        root, topdown=True, followlinks=False
    ):
        parent = Path(directory)
        child_directories[:] = sorted(
            name for name in child_directories if not (parent / name).is_symlink()
        )
        for filename in sorted(filenames):
            candidate = parent / filename
            if candidate.is_symlink():
                continue
            try:
                if candidate.is_file():
                    yield candidate
            except OSError:
                continue


class StoreToolEvidenceRecorder:
    """Persist raw streams separately and return only a compact v2 receipt."""

    def __init__(self, store: NebulaStore, artifact_store: ArtifactStore) -> None:
        self.store = store
        self.artifact_store = artifact_store

    async def record(
        self,
        call: PersistedToolCall,
        invocation: ToolInvocation,
        spec: ToolSpec,
        result: ToolExecutionResult,
    ) -> ToolExecutionResult:
        stored_items: list[Any] = []
        try:
            return await self._record(
                call, invocation, spec, result, stored_items=stored_items
            )
        except BaseException:
            for stored in stored_items:
                await asyncio.to_thread(self.artifact_store.discard_new_blob, stored)
            raise
        finally:
            for temporary_path in (
                result.stdout_artifact_path,
                result.stderr_artifact_path,
            ):
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            if result.output_directory is not None:
                shutil.rmtree(result.output_directory, ignore_errors=True)

    async def _record(
        self,
        call: PersistedToolCall,
        invocation: ToolInvocation,
        spec: ToolSpec,
        result: ToolExecutionResult,
        *,
        stored_items: list[Any],
    ) -> ToolExecutionResult:
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
        script = invocation.arguments.get("script")
        if script_sha256 is None and isinstance(script, str):
            script_sha256 = hashlib.sha256(script.encode("utf-8")).hexdigest()
        common_metadata = {
            "tool_call_id": call.id,
            "run_id": call.run_id,
            "chat_session_id": call.chat_session_id,
            "chat_turn_id": call.chat_turn_id,
            "task_id": call.task_id,
            "tool": spec.name,
            "tool_version": spec.version,
            "tool_pack": spec.pack_id,
            "manifest_digest": spec.manifest_digest,
        }
        artifact_entities: list[Artifact] = []
        references = []
        warnings: list[str] = []

        async def store_stream(
            *,
            kind: Literal["stdout", "stderr"],
            path: Path | None,
            fallback: str,
            observed: int,
            truncated: bool,
        ) -> None:
            media_type = "text/plain"
            if path is not None and path.is_file():
                with path.open("rb") as probe:
                    searchable = bytes_are_searchable(
                        probe.read(8192), media_type=media_type
                    )
                stored = await asyncio.to_thread(
                    self.artifact_store.put_file_with_status,
                    path,
                    engagement_id=invocation.engagement_id,
                    filename=f"tool-call-{call.id}-{kind}.txt",
                    media_type=media_type,
                    source=f"tool:{spec.name}@{spec.version}:{kind}",
                    metadata={
                        **common_metadata,
                        "kind": kind,
                        "searchable": searchable,
                        "observed_byte_count": observed,
                        "truncated": truncated,
                    },
                )
            else:
                payload = fallback.encode("utf-8")
                searchable = bytes_are_searchable(payload, media_type=media_type)
                stored = await asyncio.to_thread(
                    self.artifact_store.put_bytes_with_status,
                    payload,
                    engagement_id=invocation.engagement_id,
                    filename=f"tool-call-{call.id}-{kind}.txt",
                    media_type=media_type,
                    source=f"tool:{spec.name}@{spec.version}:{kind}",
                    metadata={
                        **common_metadata,
                        "kind": kind,
                        "searchable": searchable,
                        "observed_byte_count": observed or len(payload),
                        "truncated": truncated,
                    },
                )
            stored_items.append(stored)
            artifact_entities.append(stored.artifact)
            references.append(
                artifact_ref(
                    stored.artifact,
                    kind=kind,
                    observed_byte_count=observed or stored.artifact.size,
                    searchable=searchable,
                    truncated=truncated,
                )
            )

        await store_stream(
            kind="stdout",
            path=result.stdout_artifact_path,
            fallback=result.stdout,
            observed=result.observed_stdout_bytes,
            truncated=result.stdout_truncated,
        )
        await store_stream(
            kind="stderr",
            path=result.stderr_artifact_path,
            fallback=result.stderr,
            observed=result.observed_stderr_bytes,
            truncated=result.stderr_truncated,
        )

        parser_configured = bool(
            spec.parser or spec.parser_contract or result.output or result.parser_error
        )
        parsed_artifact_id: str | None = None
        if parser_configured and result.parser_error is None:
            parsed_payload = json.dumps(
                result.output,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            parsed = await asyncio.to_thread(
                self.artifact_store.put_bytes_with_status,
                parsed_payload,
                engagement_id=invocation.engagement_id,
                filename=f"tool-call-{call.id}-parsed.json",
                media_type="application/json",
                source=f"tool:{spec.name}@{spec.version}:parser",
                metadata={
                    **common_metadata,
                    "kind": "parsed",
                    "searchable": True,
                },
            )
            stored_items.append(parsed)
            artifact_entities.append(parsed.artifact)
            references.append(
                artifact_ref(parsed.artifact, kind="parsed", searchable=True)
            )
            parsed_artifact_id = parsed.artifact.id
        elif result.parser_error:
            warnings.append(f"optional parser failed: {result.parser_error}")

        for index, block in enumerate(result.mcp_content_blocks):
            block_type = str(block.get("type") or "unknown")
            media_type = "application/json"
            payload: bytes
            filename = f"tool-call-{call.id}-mcp-{index:03d}.json"
            if block_type == "text" and isinstance(block.get("text"), str):
                payload = block["text"].encode("utf-8")
                media_type = "text/plain"
                filename = f"tool-call-{call.id}-mcp-{index:03d}.txt"
            elif block_type == "image" and isinstance(block.get("data"), str):
                try:
                    payload = base64.b64decode(block["data"], validate=True)
                    media_type = str(
                        block.get("mimeType") or "application/octet-stream"
                    )
                    filename = f"tool-call-{call.id}-mcp-{index:03d}.bin"
                except (ValueError, TypeError):
                    payload = json.dumps(block, ensure_ascii=False).encode()
                    warnings.append(f"MCP image block {index} had invalid base64 data")
            else:
                payload = json.dumps(
                    block, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            searchable = bytes_are_searchable(payload, media_type=media_type)
            stored = await asyncio.to_thread(
                self.artifact_store.put_bytes_with_status,
                payload,
                engagement_id=invocation.engagement_id,
                filename=filename,
                media_type=media_type,
                source=f"mcp:{spec.name}:{block_type}",
                metadata={
                    **common_metadata,
                    "kind": "mcp_content",
                    "block_type": block_type,
                    "block_index": index,
                    "searchable": searchable,
                },
            )
            stored_items.append(stored)
            artifact_entities.append(stored.artifact)
            references.append(
                artifact_ref(stored.artifact, kind="mcp_content", searchable=searchable)
            )

        generated_count = 0
        generated_bytes = 0
        if result.output_directory is not None and result.output_directory.is_dir():
            for path in _regular_files_beneath(result.output_directory):
                if (
                    _path_has_symlink_component(result.output_directory, path)
                    or not path.is_file()
                ):
                    continue
                if generated_count >= MAX_GENERATED_FILES:
                    warnings.append(
                        f"generated file limit reached ({MAX_GENERATED_FILES}); remaining files were not promoted"
                    )
                    break
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if generated_bytes + size > MAX_GENERATED_BYTES:
                    warnings.append(
                        "generated output exceeded the 100 MiB combined limit; remaining files were not promoted"
                    )
                    break
                relative = path.relative_to(result.output_directory).as_posix()
                stored = await asyncio.to_thread(
                    self.artifact_store.put_file_with_status,
                    path,
                    engagement_id=invocation.engagement_id,
                    filename=relative,
                    source=f"tool:{spec.name}@{spec.version}:generated",
                    metadata={
                        **common_metadata,
                        "kind": "generated_file",
                        "relative_path": relative,
                    },
                )
                with self.artifact_store.open(stored.artifact) as probe:
                    searchable = bytes_are_searchable(
                        probe.read(8192), media_type=stored.artifact.media_type
                    )
                stored.artifact.metadata["searchable"] = searchable
                stored_items.append(stored)
                artifact_entities.append(stored.artifact)
                references.append(
                    artifact_ref(
                        stored.artifact,
                        kind="generated_file",
                        searchable=searchable,
                    )
                )
                generated_count += 1
                generated_bytes += size

        capture_limit_reached = False
        workspace = invocation.workspace.expanduser().resolve(strict=True)
        for configured in spec.capture_paths:
            if capture_limit_reached:
                break
            unresolved = workspace / configured
            if _path_has_symlink_component(workspace, unresolved):
                warnings.append(
                    f"configured capture path contains a symlink: {configured}"
                )
                continue
            try:
                root = unresolved.resolve(strict=True)
            except OSError:
                warnings.append(
                    f"configured capture path was not created: {configured}"
                )
                continue
            if root != workspace and workspace not in root.parents:
                warnings.append(
                    f"configured capture path escaped the workspace: {configured}"
                )
                continue
            if unresolved.is_symlink() or (not root.is_file() and not root.is_dir()):
                warnings.append(
                    f"configured capture path is not a regular file: {configured}"
                )
                continue
            for path in _regular_files_beneath(root):
                if _path_has_symlink_component(workspace, path) or not path.is_file():
                    continue
                resolved = path.resolve(strict=True)
                if resolved != workspace and workspace not in resolved.parents:
                    warnings.append("a configured generated file escaped the workspace")
                    continue
                if generated_count >= MAX_GENERATED_FILES:
                    warnings.append(
                        f"generated file limit reached ({MAX_GENERATED_FILES}); remaining files were not promoted"
                    )
                    capture_limit_reached = True
                    break
                size = resolved.stat().st_size
                if generated_bytes + size > MAX_GENERATED_BYTES:
                    warnings.append(
                        "generated output exceeded the 100 MiB combined limit; remaining files were not promoted"
                    )
                    capture_limit_reached = True
                    break
                relative = resolved.relative_to(workspace).as_posix()
                stored = await asyncio.to_thread(
                    self.artifact_store.put_file_with_status,
                    resolved,
                    engagement_id=invocation.engagement_id,
                    filename=relative,
                    source=f"tool:{spec.name}@{spec.version}:workspace-generated",
                    metadata={
                        **common_metadata,
                        "kind": "generated_file",
                        "relative_path": relative,
                        "capture_path": configured,
                    },
                )
                with self.artifact_store.open(stored.artifact) as probe:
                    searchable = bytes_are_searchable(
                        probe.read(8192), media_type=stored.artifact.media_type
                    )
                stored.artifact.metadata["searchable"] = searchable
                stored_items.append(stored)
                artifact_entities.append(stored.artifact)
                references.append(
                    artifact_ref(
                        stored.artifact,
                        kind="generated_file",
                        searchable=searchable,
                    )
                )
                generated_count += 1
                generated_bytes += size

        timed_out = bool(result.execution.get("timed_out"))
        if result.execution.get("cancelled") is True:
            status = ToolResultStatus.CANCELLED
        elif timed_out:
            status = ToolResultStatus.TIMED_OUT
        elif result.exit_code is not None and result.exit_code != 0:
            status = ToolResultStatus.FAILED
        else:
            status = ToolResultStatus.COMPLETED
        truncated = result.output_truncated or any(ref.truncated for ref in references)
        incomplete = (
            truncated
            or status in {ToolResultStatus.TIMED_OUT, ToolResultStatus.CANCELLED}
            or bool(warnings and any("limit" in item for item in warnings))
        )
        if truncated:
            warnings.append(
                "captured output was truncated after the configured retention limit"
            )
        parser_state = (
            ParserState.FAILED
            if result.parser_error
            else ParserState.COMPLETED
            if parser_configured
            else ParserState.NOT_CONFIGURED
        )
        model_references = references[:MAX_MODEL_ARTIFACT_REFS]
        if len(references) > len(model_references):
            warnings.append(
                f"{len(references) - len(model_references)} additional generated artifacts are available through tool_output.search"
            )
        warning_count = len(warnings)
        warnings = [re.sub(r"\s+", " ", item).strip()[:240] for item in warnings[:8]]
        if warning_count > len(warnings):
            warnings.append(
                f"{warning_count - len(warnings)} additional warnings are available in execution diagnostics"
            )
        receipt = ToolResultReceipt(
            tool_call_id=call.id,
            tool_name=spec.name,
            tool_version=spec.version,
            status=status,
            exit_code=result.exit_code,
            timing=ToolTimingReceipt(
                started_at=result.execution.get("started_at"),
                completed_at=result.execution.get("completed_at"),
                duration_seconds=result.execution.get("duration_seconds"),
            ),
            artifacts=model_references,
            truncated=truncated,
            incomplete=incomplete,
            parser=ToolParserReceipt(
                state=parser_state,
                artifact_id=parsed_artifact_id,
                contract=spec.parser_contract,
            ),
            warnings=warnings,
        )
        receipt_payload = json.dumps(
            receipt.as_model_result(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        receipt_stored = await asyncio.to_thread(
            self.artifact_store.put_bytes_with_status,
            receipt_payload,
            engagement_id=invocation.engagement_id,
            filename=f"tool-call-{call.id}-receipt.json",
            media_type="application/json",
            source=f"tool:{spec.name}@{spec.version}:receipt",
            metadata={
                **common_metadata,
                "kind": "receipt",
                "searchable": False,
                "tool_call_id": call.id,
                "image": spec.image,
                "interface_catalog_digest": interface_catalog_digest,
                "script_sha256": script_sha256,
            },
        )
        stored_items.append(receipt_stored)
        artifact_entities.append(receipt_stored.artifact)
        evidence = Evidence(
            engagement_id=invocation.engagement_id,
            evidence_type="tool_execution",
            title=f"{spec.name} execution",
            artifact_id=receipt_stored.artifact.id,
            tool_call_id=call.id,
            sha256=receipt_stored.artifact.sha256,
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
                "status": status.value,
                "artifact_ids": [item.artifact_id for item in references],
            },
        )
        try:
            await asyncio.to_thread(
                self.store.create_many, [*artifact_entities, evidence]
            )
        except Exception as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.tools.caught_failure_007",
                "A handled toolbox operation raised an exception.",
                caught_error,
                stage="tools",
            )
            for stored in stored_items:
                await asyncio.to_thread(self.artifact_store.discard_new_blob, stored)
            raise
        finally:
            for temporary_path in (
                result.stdout_artifact_path,
                result.stderr_artifact_path,
            ):
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            if result.output_directory is not None:
                shutil.rmtree(result.output_directory, ignore_errors=True)
        return result.model_copy(
            update={
                "output": receipt.as_model_result(),
                "artifacts": [item.model_dump(mode="json") for item in references],
                "stdout": "",
                "stderr": "",
                "receipt": receipt,
                "result_artifact_id": receipt_stored.artifact.id,
                "evidence_ids": [evidence.id],
                "stdout_artifact_path": None,
                "stderr_artifact_path": None,
                "output_directory": None,
                "mcp_content_blocks": [],
            }
        )


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


def register_artifact_retrieval_tools(
    registry: ToolRegistry,
    *,
    output_service: ToolOutputService,
) -> None:
    """Install the four trusted, bounded retrieval capabilities."""

    common_output = {"type": "object", "additionalProperties": True}

    async def tool_search(invocation: ToolInvocation) -> dict[str, Any]:
        return await asyncio.to_thread(
            output_service.search,
            engagement_id=invocation.engagement_id,
            owner_id=invocation.run_id,
            **invocation.arguments,
        )

    async def tool_read(invocation: ToolInvocation) -> dict[str, Any]:
        return await asyncio.to_thread(
            output_service.read,
            engagement_id=invocation.engagement_id,
            owner_id=invocation.run_id,
            **invocation.arguments,
        )

    async def workspace_search(invocation: ToolInvocation) -> dict[str, Any]:
        service = WorkspaceOutputService(invocation.workspace)
        return await asyncio.to_thread(service.search, **invocation.arguments)

    async def workspace_read(invocation: ToolInvocation) -> dict[str, Any]:
        service = WorkspaceOutputService(invocation.workspace)
        return await asyncio.to_thread(service.read, **invocation.arguments)

    definitions: list[
        tuple[
            str,
            str,
            dict[str, Any],
            Callable[[ToolInvocation], Awaitable[dict[str, Any]]],
        ]
    ] = [
        (
            "tool_output.search",
            "Search immutable output artifacts from a prior tool call. Returns only bounded, redacted, line-numbered untrusted excerpts.",
            {
                "type": "object",
                "properties": {
                    "tool_call_id": {"type": "string"},
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "mode": {
                        "type": "string",
                        "enum": ["literal", "regex"],
                        "default": "literal",
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 0,
                    },
                    "match_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                    "cursor": {"type": ["string", "null"]},
                },
                "required": ["tool_call_id", "query"],
                "additionalProperties": False,
            },
            tool_search,
        ),
        (
            "tool_output.read",
            "Read at most 200 lines and 8 KiB from one authorized tool artifact.",
            {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "starting_line": {"type": "integer", "minimum": 1, "default": 1},
                    "line_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 100,
                    },
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            tool_read,
        ),
        (
            "workspace.search",
            "Search authorized engagement workspace files with bounded, redacted output.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "path": {"type": "string", "default": "."},
                    "mode": {
                        "type": "string",
                        "enum": ["literal", "regex"],
                        "default": "literal",
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 0,
                    },
                    "match_limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                    "cursor": {"type": ["string", "null"]},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            workspace_search,
        ),
        (
            "workspace.read",
            "Read at most 200 lines and 8 KiB from an authorized workspace file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "starting_line": {"type": "integer", "minimum": 1, "default": 1},
                    "line_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 100,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            workspace_read,
        ),
    ]
    for name, description, input_schema, handler in definitions:
        registry.register(
            InvocationAnalysisTool(
                ToolSpec(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=common_output,
                    risk_class=RiskClass.LOCAL_READ,
                    budget_class="artifact_query",
                ),
                handler,
            )
        )


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
            if (
                isinstance(call.result, dict)
                and call.result.get("schema") == "nebula.tool-result/v2"
            ):
                receipt = ToolResultReceipt.model_validate(call.result)
                cached = ToolExecutionResult(
                    output=receipt.as_model_result(),
                    artifacts=[
                        item.model_dump(mode="json") for item in receipt.artifacts
                    ],
                    receipt=receipt,
                    result_artifact_id=call.result_artifact_id,
                )
            elif isinstance(plugin, AnalysisTool):
                # Trusted bounded analysis/retrieval results are not action
                # output and retain their original replay behavior.
                legacy_output = (
                    call.result
                    if isinstance(call.result, dict)
                    else {
                        "schema": "nebula.bounded-result/v1",
                        "status": "incomplete",
                        "warning": "historical analysis result was not an object",
                    }
                )
                cached = ToolExecutionResult(output=legacy_output)
            else:
                # Do not rewrite historical evidence, but never replay its raw
                # action payload into a modern model request.
                receipt = _legacy_action_receipt(call, plugin.spec)
                cached = ToolExecutionResult(
                    output=receipt.as_model_result(),
                    receipt=receipt,
                    result_artifact_id=call.result_artifact_id,
                )
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
                    if result.parser_error is None and (
                        plugin.spec.parser
                        or plugin.spec.parser_contract
                        or result.output
                    ):
                        try:
                            self._validate(
                                plugin.spec.output_schema, result.output, "output"
                            )
                        except InvalidToolArguments as exc:
                            # Parsing is optional enrichment.  A schema problem
                            # must not erase a successful process execution or
                            # its raw evidence.
                            result = result.model_copy(
                                update={"parser_error": _bounded_execution_error(exc)}
                            )
                    result = await self.evidence_recorder.record(
                        running, invocation, plugin.spec, result
                    )
                else:
                    self._validate(plugin.spec.output_schema, result.output, "output")
            except asyncio.CancelledError as caught_error:
                record_caught_exception(
                    "toolbox",
                    "toolbox.tools.caught_failure_009",
                    "A handled toolbox operation raised an exception.",
                    caught_error,
                    stage="tools",
                )
                partial = getattr(caught_error, "result", None)
                if (
                    isinstance(partial, ToolExecutionResult)
                    and self.evidence_recorder is not None
                ):
                    try:
                        recorded = await asyncio.shield(
                            self.evidence_recorder.record(
                                running, invocation, plugin.spec, partial
                            )
                        )
                    except Exception as exc:
                        await self.ledger.transition(
                            running,
                            ToolCallStatus.FAILED,
                            error=(
                                "artifact persistence failed during cancellation: "
                                + _bounded_execution_error(exc)
                            ),
                        )
                    else:
                        await self.ledger.transition(
                            running,
                            ToolCallStatus.CANCELLED,
                            result=(
                                recorded.receipt.as_model_result()
                                if recorded.receipt is not None
                                else recorded.output
                            ),
                            result_artifact_id=recorded.result_artifact_id,
                        )
                else:
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
            terminal_status = ToolCallStatus.COMPLETE
            terminal_error: str | None = None
            if result.receipt is not None and result.receipt.status in {
                ToolResultStatus.FAILED,
                ToolResultStatus.TIMED_OUT,
                ToolResultStatus.CANCELLED,
            }:
                terminal_status = ToolCallStatus.FAILED
                terminal_error = f"tool execution {result.receipt.status.value}"
            await self.ledger.transition(
                running,
                terminal_status,
                result=(
                    result.receipt.as_model_result()
                    if result.receipt is not None
                    else result.output
                ),
                error=terminal_error,
                result_artifact_id=result.result_artifact_id,
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
    # Exception messages from optional/custom parsers are not trusted metadata:
    # they can echo the complete tool output (or secrets found in it).  Receipts
    # and persisted call summaries are model-facing, so retain only the failure
    # class here.  The full exception is still available to the internal
    # diagnostic logger at each call site.
    return f"{exc.__class__.__name__}: details withheld; inspect captured artifacts"


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
