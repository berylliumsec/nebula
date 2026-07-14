"""Operator-reviewed, container-only code execution and durable replay."""

from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import socket
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from .artifacts import ArtifactStore, StoredArtifact
from .assistant_code import LANGUAGE_ALIASES, parse_fenced_code_blocks, utf8_slice
from .domain import (
    Artifact,
    ChatMessage,
    ChatRole,
    Engagement,
    Evidence,
    ExecutionLimitsSnapshot,
    ExecutionNetworkMode,
    ExecutionNetworkSnapshot,
    ExecutionOrigin,
    ExecutionOriginKind,
    ExecutionRuntimeSnapshot,
    NebulaModel,
    OperatorExecution,
    OperatorExecutionStatus,
    RiskClass,
    ScopePolicy,
    WorkspaceChange,
    utc_now,
)
from .policy import PolicyEffect, PolicyEngine, PolicyRequest
from .redaction import StatefulRedactor, redacted_display
from .sandbox import (
    EgressRule,
    SandboxExecutionKind,
    SandboxLimits,
    SandboxNetwork,
    SandboxRequest,
    SandboxUnavailable,
    SandboxWorkspaceAccess,
)
from .storage import NebulaStore, NotFoundError
from .tool_platform import OperatorRuntimeResolution, ToolPlatform, ToolPlatformError

MAX_SOURCE_BYTES = 200_000
OUTPUT_CHUNK_BYTES = 32_768
PREVIEW_TTL_SECONDS = 300
WORKSPACE_MAX_BYTES = 5 * 1024 * 1024 * 1024
WORKSPACE_MAX_ENTRIES = 50_000
WORKSPACE_MAX_FILE_BYTES = 1024 * 1024 * 1024
TERMINAL_EXECUTION_STATUSES = {
    OperatorExecutionStatus.COMPLETED,
    OperatorExecutionStatus.DENIED,
    OperatorExecutionStatus.TIMED_OUT,
    OperatorExecutionStatus.CANCELLED,
    OperatorExecutionStatus.FAILED,
    OperatorExecutionStatus.INTERRUPTED,
}


class ExecutionServiceError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class ExecutionNetworkRequest(NebulaModel):
    mode: ExecutionNetworkMode = ExecutionNetworkMode.NONE
    target: str | None = Field(default=None, max_length=2048)
    ports: list[int] = Field(default_factory=list, max_length=1024)

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, values: list[int]) -> list[int]:
        if any(
            isinstance(value, bool) or value < 1 or value > 65_535 for value in values
        ):
            raise ValueError("ports must be integers between 1 and 65535")
        return sorted(set(values))

    @model_validator(mode="after")
    def complete_network_request(self) -> "ExecutionNetworkRequest":
        if self.mode == ExecutionNetworkMode.SCOPED:
            if not self.target or not self.ports:
                raise ValueError("scoped network mode requires target and ports")
        elif self.target is not None or self.ports:
            raise ValueError("offline execution cannot request a target or ports")
        return self


class ExecutionPreflightRequest(NebulaModel):
    # Executable source is an exact-byte contract.  The base domain model trims
    # ordinary strings for human-entered metadata, which is unsafe here because
    # leading indentation and a final newline can change program behaviour.
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        protected_namespaces=(),
        str_strip_whitespace=False,
    )

    engagement_id: str = Field(min_length=1, max_length=200)
    language: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=200_000)
    origin: ExecutionOrigin
    network: ExecutionNetworkRequest = Field(default_factory=ExecutionNetworkRequest)

    @field_validator("source")
    @classmethod
    def source_is_bounded_utf8(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("source cannot contain NUL bytes")
        if len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds 200000 UTF-8 bytes")
        return value


class ExecutionStartRequest(ExecutionPreflightRequest):
    preview_token: str = Field(min_length=1, max_length=65_536)
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_idempotency_key: str = Field(min_length=1, max_length=300)


class ExecutionCapability(NebulaModel):
    language: str
    aliases: list[str]
    offline: bool
    scoped_network: bool
    detail: str | None = None


class ExecutionCapabilities(NebulaModel):
    engagement_id: str
    ready: bool
    runtimes: list[ExecutionCapability]
    limits: ExecutionLimitsSnapshot = Field(default_factory=ExecutionLimitsSnapshot)
    workspace: str = "/workspace"


class ExecutionPreflightResponse(NebulaModel):
    allowed: bool
    error_code: str | None = None
    detail: str
    canonical_language: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime: ExecutionRuntimeSnapshot | None = None
    network: ExecutionNetworkSnapshot | None = None
    limits: ExecutionLimitsSnapshot = Field(default_factory=ExecutionLimitsSnapshot)
    workspace: str = "/workspace"
    policy_rule: str | None = None
    preview_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    preview_token: str | None = None
    expires_at: datetime | None = None


class ExecutionEventList(NebulaModel):
    events: list[Any]
    next_sequence: int


class _WorkspaceLimitError(RuntimeError):
    pass


class ExecutionService:
    def __init__(
        self,
        *,
        store: NebulaStore,
        artifact_store: ArtifactStore,
        tool_platform: ToolPlatform | None,
        data_root: str | Path,
        operator_id: Callable[[], str] | None = None,
        max_concurrency: int = 2,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.tool_platform = tool_platform
        self.data_root = Path(data_root).expanduser().resolve()
        self.spool_root = self.data_root / "execution-spool"
        self.spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.spool_root.chmod(0o700)
        self.operator_id = operator_id or (lambda: "system")
        self._preview_secret = os.urandom(32)
        self._global_slots = asyncio.Semaphore(max_concurrency)
        self._engagement_locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._start_lock = asyncio.Lock()
        self._shutting_down = False

    def engagement_lock(self, engagement_id: str) -> asyncio.Lock:
        """Shared workspace-operation lock for one engagement."""

        return self._engagement_locks.setdefault(engagement_id, asyncio.Lock())

    async def startup(self) -> None:
        """Fail closed after a prior Core died mid-execution."""

        for execution in self._all_executions():
            if execution.status not in {
                OperatorExecutionStatus.QUEUED,
                OperatorExecutionStatus.RUNNING,
                OperatorExecutionStatus.CANCELLING,
            }:
                continue
            await self._cleanup_container(execution)
            if await self._recover_spool(execution):
                continue
            updated = self.store.update(
                OperatorExecution,
                execution.id,
                {
                    "status": OperatorExecutionStatus.INTERRUPTED,
                    "completed_at": utc_now(),
                    "error_code": "interrupted",
                    "error_detail": "Core restarted before execution reached a terminal state",
                },
                expected_revision=execution.revision,
            )
            self._event(
                updated,
                "execution.interrupted",
                {"status": updated.status.value, "error_code": "interrupted"},
                key="startup-interrupted",
            )

    async def _recover_spool(self, execution: OperatorExecution) -> bool:
        spool_dir = self.spool_root / execution.id
        stdout_path = spool_dir / "stdout.raw"
        stderr_path = spool_dir / "stderr.raw"
        if not stdout_path.is_file() and not stderr_path.is_file():
            return False
        try:
            spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in (stdout_path, stderr_path):
                path.touch(mode=0o600, exist_ok=True)
                path.chmod(0o600)
            stdout_text = redacted_display(
                stdout_path.read_bytes().decode("utf-8", errors="replace")
            )
            stderr_text = redacted_display(
                stderr_path.read_bytes().decode("utf-8", errors="replace")
            )
            await self._persist_terminal(
                execution.id,
                stdout_path,
                stderr_path,
                stdout_text,
                stderr_text,
                status=OperatorExecutionStatus.INTERRUPTED,
                error_code="interrupted",
                error_detail="Core restarted before execution reached a terminal state",
                exit_code=None,
                output_truncated=False,
                workspace_changes=[],
            )
        except Exception:
            return False
        shutil.rmtree(spool_dir, ignore_errors=True)
        return True

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def capabilities(self, engagement_id: str) -> ExecutionCapabilities:
        self.store.get(Engagement, engagement_id)
        runtime_rows: list[ExecutionCapability] = []
        aliases_by_language = {
            "bash": ["bash", "shell"],
            "sh": ["sh"],
            "python": ["python", "python3", "py"],
        }
        for language, aliases in aliases_by_language.items():
            results: dict[bool, str | None] = {}
            for network in (False, True):
                try:
                    self._resolve(engagement_id, language, network=network)
                    results[network] = None
                except (ToolPlatformError, ExecutionServiceError) as exc:
                    results[network] = str(exc)
            runtime_rows.append(
                ExecutionCapability(
                    language=language,
                    aliases=aliases,
                    offline=results[False] is None,
                    scoped_network=results[True] is None,
                    detail=results[False] or results[True],
                )
            )
        return ExecutionCapabilities(
            engagement_id=engagement_id,
            # The first Run surface is release-gated as one product: operators
            # should not see it until both offline and scoped egress are proven
            # available for at least one declared runtime.
            ready=any(row.offline and row.scoped_network for row in runtime_rows),
            runtimes=runtime_rows,
        )

    async def preflight(
        self, request: ExecutionPreflightRequest
    ) -> ExecutionPreflightResponse:
        try:
            source, canonical = self._validated_source(request)
            self._assert_workspace_limits(request.engagement_id)
            resolution = self._resolve(
                request.engagement_id,
                canonical,
                network=request.network.mode == ExecutionNetworkMode.SCOPED,
            )
            network, policy_rule, detail = await self._network_snapshot(
                request.engagement_id, request.network
            )
            runtime = self._runtime_snapshot(resolution)
            source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
            request_fingerprint = self.request_fingerprint(request)
            binding = {
                "request_fingerprint": request_fingerprint,
                "source_sha256": source_sha,
                "runtime": runtime.model_dump(mode="json"),
                "network": network.model_dump(mode="json"),
                "limits": ExecutionLimitsSnapshot().model_dump(mode="json"),
                "policy_rule": policy_rule,
                "operator_id": self.operator_id(),
            }
            preview_fingerprint = _digest_json(binding)
            expires = utc_now() + timedelta(seconds=PREVIEW_TTL_SECONDS)
            return ExecutionPreflightResponse(
                allowed=True,
                detail=detail,
                canonical_language=canonical,
                source_sha256=source_sha,
                runtime=runtime,
                network=network,
                policy_rule=policy_rule,
                preview_fingerprint=preview_fingerprint,
                preview_token=self._sign_preview(binding, preview_fingerprint, expires),
                expires_at=expires,
            )
        except ExecutionServiceError as exc:
            return ExecutionPreflightResponse(
                allowed=False,
                error_code=exc.code,
                detail=exc.detail,
            )
        except ToolPlatformError as exc:
            return ExecutionPreflightResponse(
                allowed=False,
                error_code="runtime_unavailable",
                detail=str(exc),
            )

    async def start(self, request: ExecutionStartRequest) -> OperatorExecution:
        base = ExecutionPreflightRequest.model_validate(
            request.model_dump(
                exclude={
                    "preview_token",
                    "preview_fingerprint",
                    "client_idempotency_key",
                }
            )
        )
        request_fingerprint = self.request_fingerprint(base)
        async with self._start_lock:
            existing = self._execution_for_idempotency(
                request.engagement_id, request.client_idempotency_key
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ExecutionServiceError(
                        "idempotency_conflict",
                        "idempotency key was reused for different execution input",
                    )
                return existing
            signed_binding = self._verify_preview_token(
                request.preview_token, request.preview_fingerprint
            )
            if signed_binding.get("request_fingerprint") != request_fingerprint:
                raise ExecutionServiceError(
                    "preview_stale", "execution preview does not match the request"
                )
            if signed_binding.get("operator_id") != self.operator_id():
                raise ExecutionServiceError(
                    "preview_stale", "active operator changed after execution review"
                )
            preview = await self.preflight(base)
            if not preview.allowed:
                return self._record_denied_start(
                    request,
                    request_fingerprint=request_fingerprint,
                    signed_binding=signed_binding,
                    error_code=preview.error_code or "policy_denied",
                    error_detail=preview.detail,
                )
            if preview.preview_fingerprint != request.preview_fingerprint:
                return self._record_denied_start(
                    request,
                    request_fingerprint=request_fingerprint,
                    signed_binding=signed_binding,
                    error_code="preview_stale",
                    error_detail=(
                        "runner, policy, target resolution, or source changed after review"
                    ),
                )
            assert preview.runtime is not None
            assert preview.network is not None
            assert preview.source_sha256 is not None
            source, canonical = self._validated_source(base)
            execution_id = str(uuid4())
            suffix = "py" if canonical == "python" else "sh"
            stored_source = self.artifact_store.put_bytes_with_status(
                source.encode("utf-8"),
                engagement_id=request.engagement_id,
                filename=f"execution-{execution_id}.{suffix}",
                media_type=(
                    "text/x-python" if canonical == "python" else "text/x-shellscript"
                ),
                source="operator-execution-source",
                metadata={"execution_id": execution_id},
            )
            execution = OperatorExecution(
                id=execution_id,
                engagement_id=request.engagement_id,
                operator_id=self.operator_id(),
                origin=request.origin,
                language=canonical,
                source_sha256=preview.source_sha256,
                source_artifact_id=stored_source.artifact.id,
                source_preview=redacted_display(source)[:4096],
                runtime=preview.runtime,
                network=preview.network,
                limits=preview.limits,
                policy_decision="allowed",
                preview_fingerprint=request.preview_fingerprint,
                request_fingerprint=request_fingerprint,
                client_idempotency_key=request.client_idempotency_key,
            )
            try:
                self.store.create_many([stored_source.artifact, execution])
            except Exception:
                self.artifact_store.discard_new_blob(stored_source)
                raise
            self._event(
                execution,
                "execution.queued",
                {"status": execution.status.value},
                key="queued",
            )
            task = asyncio.create_task(
                self._execute(execution.id), name=f"operator-execution-{execution.id}"
            )
            self._tasks[execution.id] = task
            task.add_done_callback(lambda _task: self._tasks.pop(execution.id, None))
            return execution

    def _record_denied_start(
        self,
        request: ExecutionStartRequest,
        *,
        request_fingerprint: str,
        signed_binding: dict[str, Any],
        error_code: str,
        error_detail: str,
    ) -> OperatorExecution:
        """Persist a terminal record for a valid, confirmed start that fails closed."""

        try:
            runtime = ExecutionRuntimeSnapshot.model_validate(signed_binding["runtime"])
            network = ExecutionNetworkSnapshot.model_validate(signed_binding["network"])
            limits = ExecutionLimitsSnapshot.model_validate(signed_binding["limits"])
            source_sha256 = str(signed_binding["source_sha256"])
        except (KeyError, ValueError) as exc:
            raise ExecutionServiceError(
                "preview_stale", "execution preview binding is incomplete"
            ) from exc
        actual_sha256 = hashlib.sha256(request.source.encode("utf-8")).hexdigest()
        if actual_sha256 != source_sha256:
            raise ExecutionServiceError(
                "preview_stale", "execution source changed after review"
            )
        execution_id = str(uuid4())
        suffix = "py" if runtime.language == "python" else "sh"
        stored_source = self.artifact_store.put_bytes_with_status(
            request.source.encode("utf-8"),
            engagement_id=request.engagement_id,
            filename=f"execution-{execution_id}.{suffix}",
            media_type=(
                "text/x-python"
                if runtime.language == "python"
                else "text/x-shellscript"
            ),
            source="operator-execution-source",
            metadata={"execution_id": execution_id},
        )
        execution = OperatorExecution(
            id=execution_id,
            engagement_id=request.engagement_id,
            operator_id=self.operator_id(),
            origin=request.origin,
            language=runtime.language,
            source_sha256=source_sha256,
            source_artifact_id=stored_source.artifact.id,
            source_preview=redacted_display(request.source)[:4096],
            runtime=runtime,
            network=network,
            limits=limits,
            policy_decision="denied",
            preview_fingerprint=request.preview_fingerprint,
            request_fingerprint=request_fingerprint,
            client_idempotency_key=request.client_idempotency_key,
            status=OperatorExecutionStatus.DENIED,
            error_code=error_code,
            error_detail=error_detail[:4000],
            completed_at=utc_now(),
        )
        try:
            self.store.create_many([stored_source.artifact, execution])
        except Exception:
            self.artifact_store.discard_new_blob(stored_source)
            raise
        self._event(
            execution,
            "execution.denied",
            {
                "status": execution.status.value,
                "error_code": execution.error_code,
            },
            key="denied",
        )
        return execution

    async def cancel(self, execution_id: str) -> OperatorExecution:
        execution = self.store.get(OperatorExecution, execution_id)
        if execution.status in TERMINAL_EXECUTION_STATUSES:
            return execution
        if execution.status != OperatorExecutionStatus.CANCELLING:
            execution = self.store.update(
                OperatorExecution,
                execution.id,
                {"status": OperatorExecutionStatus.CANCELLING},
                expected_revision=execution.revision,
            )
            self._event(
                execution,
                "execution.cancelling",
                {"status": execution.status.value},
                key="cancelling",
            )
        task = self._tasks.get(execution.id)
        if task is not None:
            task.cancel()
        return execution

    def list_executions(
        self,
        engagement_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        status: OperatorExecutionStatus | None = None,
        language: str | None = None,
        operator_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        query: str | None = None,
    ) -> list[OperatorExecution]:
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in (date_from, date_to)
        ):
            raise ExecutionServiceError(
                "invalid_date_filter",
                "execution date filters must include a timezone",
                status_code=422,
            )
        rows = self._all_executions(engagement_id)
        folded = query.casefold() if query else None
        operator_folded = operator_id.casefold() if operator_id else None
        filtered = [
            row
            for row in rows
            if (status is None or row.status == status)
            and (
                language is None
                or row.language == LANGUAGE_ALIASES.get(language, language)
            )
            and (
                operator_folded is None or operator_folded in row.operator_id.casefold()
            )
            and (date_from is None or row.queued_at >= date_from)
            and (date_to is None or row.queued_at < date_to)
            and (
                folded is None
                or folded in row.source_preview.casefold()
                or folded in row.operator_id.casefold()
            )
        ]
        filtered.sort(key=lambda row: (row.queued_at, row.id), reverse=True)
        return filtered[offset : offset + limit]

    def output_bytes(
        self, execution_id: str, stream: str, *, raw: bool
    ) -> tuple[bytes, str]:
        execution = self.store.get(OperatorExecution, execution_id)
        field = {
            ("stdout", True): "stdout_artifact_id",
            ("stderr", True): "stderr_artifact_id",
            ("stdout", False): "redacted_stdout_artifact_id",
            ("stderr", False): "redacted_stderr_artifact_id",
        }.get((stream, raw))
        if field is None:
            raise ValueError("stream must be stdout or stderr")
        artifact_id = getattr(execution, field)
        if not artifact_id:
            raise NotFoundError("execution output is not available")
        artifact = self.store.get(Artifact, artifact_id)
        if not self.artifact_store.verify(artifact):
            raise ExecutionServiceError(
                "output_integrity", "execution output failed integrity verification"
            )
        return self.artifact_store.read(artifact), artifact.media_type

    @staticmethod
    def request_fingerprint(request: ExecutionPreflightRequest) -> str:
        return _digest_json(request.model_dump(mode="json"))

    def _resolve(
        self, engagement_id: str, language: str, *, network: bool
    ) -> OperatorRuntimeResolution:
        if self.tool_platform is None:
            raise ExecutionServiceError(
                "runner_unavailable", "Toolbox execution is not configured"
            )
        return self.tool_platform.resolve_operator_runtime(
            engagement_id, language, network=network
        )

    def _validated_source(self, request: ExecutionPreflightRequest) -> tuple[str, str]:
        canonical = LANGUAGE_ALIASES.get(request.language.casefold())
        if canonical is None:
            raise ExecutionServiceError(
                "unsupported_language",
                f"language {request.language!r} is copy-only",
                status_code=422,
            )
        if request.origin.kind == ExecutionOriginKind.ASSISTANT_MESSAGE:
            assert request.origin.message_id is not None
            assert request.origin.block_ordinal is not None
            assert request.origin.block_sha256 is not None
            try:
                message = self.store.get(ChatMessage, request.origin.message_id)
            except NotFoundError as exc:
                raise ExecutionServiceError(
                    "origin_mismatch", "originating assistant message was not found"
                ) from exc
            if (
                message.engagement_id != request.engagement_id
                or message.role != ChatRole.ASSISTANT
            ):
                raise ExecutionServiceError(
                    "origin_mismatch",
                    "origin is not an assistant message in this engagement",
                )
            blocks = parse_fenced_code_blocks(message.content)
            if request.origin.block_ordinal >= len(blocks):
                raise ExecutionServiceError(
                    "origin_mismatch", "originating code block is not a closed fence"
                )
            block = blocks[request.origin.block_ordinal]
            if block.sha256 != request.origin.block_sha256:
                raise ExecutionServiceError(
                    "origin_mismatch", "originating code block hash does not match"
                )
            if block.canonical_language != canonical:
                raise ExecutionServiceError(
                    "origin_mismatch",
                    "requested language differs from the persisted fence",
                )
            try:
                source = utf8_slice(
                    block.source,
                    request.origin.selection_start_byte,
                    request.origin.selection_end_byte,
                )
            except ValueError as exc:
                raise ExecutionServiceError(
                    "origin_mismatch", str(exc), status_code=422
                ) from exc
        elif request.origin.kind == ExecutionOriginKind.RERUN:
            assert request.origin.execution_id is not None
            try:
                parent = self.store.get(OperatorExecution, request.origin.execution_id)
            except NotFoundError as exc:
                raise ExecutionServiceError(
                    "origin_mismatch", "rerun execution was not found"
                ) from exc
            if (
                parent.engagement_id != request.engagement_id
                or parent.language != canonical
            ):
                raise ExecutionServiceError(
                    "origin_mismatch", "rerun origin belongs to different input"
                )
            artifact = self.store.get(Artifact, parent.source_artifact_id)
            if not self.artifact_store.verify(artifact):
                raise ExecutionServiceError(
                    "origin_mismatch", "rerun source failed integrity verification"
                )
            source = self.artifact_store.read(artifact).decode("utf-8", errors="strict")
        else:
            assert request.origin.source_sha256 is not None
            observed_sha256 = hashlib.sha256(
                request.source.encode("utf-8")
            ).hexdigest()
            if not hmac.compare_digest(
                observed_sha256, request.origin.source_sha256
            ):
                raise ExecutionServiceError(
                    "origin_mismatch",
                    "selected source does not match its reviewed SHA-256",
                )
            source = request.source
        if source != request.source:
            raise ExecutionServiceError(
                "origin_mismatch", "submitted source differs from durable provenance"
            )
        return source, canonical

    async def _network_snapshot(
        self, engagement_id: str, request: ExecutionNetworkRequest
    ) -> tuple[ExecutionNetworkSnapshot, str, str]:
        engagement = self.store.get(Engagement, engagement_id)
        if not engagement.scope_policy_id:
            raise ExecutionServiceError(
                "policy_denied", "engagement requires a scope policy before execution"
            )
        policy = self.store.get(ScopePolicy, engagement.scope_policy_id)
        if request.mode == ExecutionNetworkMode.NONE:
            decision = PolicyEngine().evaluate(
                policy,
                PolicyRequest(
                    tool_name="environment.shell_local",
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    action="operator_code",
                ),
            )
            addresses: list[str] = []
        else:
            assert request.target is not None
            addresses = await asyncio.to_thread(_resolve_target, request.target)
            decision = PolicyEngine().evaluate(
                policy,
                PolicyRequest(
                    tool_name="environment.shell_network",
                    risk_class=RiskClass.ACTIVE_SCAN,
                    target=request.target,
                    ports=request.ports,
                    resolved_ips=addresses,
                    action="operator_code",
                ),
            )
        if decision.effect == PolicyEffect.REQUIRE_APPROVAL:
            raise ExecutionServiceError("approval_required", decision.reason)
        if decision.effect != PolicyEffect.ALLOW:
            raise ExecutionServiceError("policy_denied", decision.reason)
        if request.mode == ExecutionNetworkMode.NONE:
            snapshot = ExecutionNetworkSnapshot()
        else:
            snapshot = ExecutionNetworkSnapshot(
                mode=ExecutionNetworkMode.SCOPED,
                target=request.target,
                ports=request.ports,
                resolved_addresses=addresses,
                scope_policy_id=policy.id,
                scope_policy_revision=policy.revision,
            )
        return snapshot, decision.rule, decision.reason

    @staticmethod
    def _runtime_snapshot(
        resolution: OperatorRuntimeResolution,
    ) -> ExecutionRuntimeSnapshot:
        profile = resolution.profile
        return ExecutionRuntimeSnapshot(
            language=resolution.canonical_language,
            interpreter=resolution.runtime.interpreter,
            arguments=resolution.runtime.arguments,
            tool_pack_installation_id=resolution.installation.id,
            manifest_digest=resolution.installation.manifest_digest,
            image=resolution.image,
            runner_profile_id=profile.id,
            runner_profile_revision=profile.revision,
            runner_runtime=profile.runtime,
            runner_isolation=profile.isolation,
            runner_executable=profile.executable,
            runner_platform=profile.platform,
            runner_context=profile.context,
            runner_socket=profile.socket,
            trusted=resolution.trusted,
        )

    async def _execute(self, execution_id: str) -> None:
        execution = self.store.get(OperatorExecution, execution_id)
        lock = self.engagement_lock(execution.engagement_id)
        try:
            async with lock, self._global_slots:
                current = self.store.get(OperatorExecution, execution_id)
                if current.status == OperatorExecutionStatus.CANCELLING:
                    await self._terminal_without_output(
                        current,
                        OperatorExecutionStatus.CANCELLED,
                        "cancelled",
                        "execution was cancelled before launch",
                    )
                    return
                await self._run_isolated(current)
        except asyncio.CancelledError:
            current = self.store.get(OperatorExecution, execution_id)
            status = (
                OperatorExecutionStatus.INTERRUPTED
                if self._shutting_down
                else OperatorExecutionStatus.CANCELLED
            )
            code = "interrupted" if self._shutting_down else "cancelled"
            if current.status not in TERMINAL_EXECUTION_STATUSES:
                await self._terminal_without_output(
                    current, status, code, f"execution {code}"
                )
        except Exception as exc:
            current = self.store.get(OperatorExecution, execution_id)
            if current.status not in TERMINAL_EXECUTION_STATUSES:
                await self._terminal_without_output(
                    current,
                    OperatorExecutionStatus.FAILED,
                    _execution_failure_code(exc),
                    str(exc)[:4000],
                )

    async def _run_isolated(self, execution: OperatorExecution) -> None:
        network_enabled = execution.network.mode == ExecutionNetworkMode.SCOPED
        resolution = self._resolve(
            execution.engagement_id, execution.language, network=network_enabled
        )
        if self._runtime_snapshot(resolution) != execution.runtime:
            raise ExecutionServiceError(
                "preview_stale", "execution environment changed while queued"
            )
        if network_enabled:
            assert execution.network.scope_policy_id is not None
            policy = self.store.get(ScopePolicy, execution.network.scope_policy_id)
            if policy.revision != execution.network.scope_policy_revision:
                raise ExecutionServiceError(
                    "preview_stale", "scope policy changed while execution was queued"
                )
            current_addresses = await asyncio.to_thread(
                _resolve_target, execution.network.target or ""
            )
            if current_addresses != execution.network.resolved_addresses:
                raise ExecutionServiceError(
                    "preview_stale", "target DNS resolution changed while queued"
                )
        before = await asyncio.to_thread(_workspace_snapshot, resolution.workspace)
        execution = self.store.update(
            OperatorExecution,
            execution.id,
            {
                "status": OperatorExecutionStatus.RUNNING,
                "started_at": utc_now(),
            },
            expected_revision=execution.revision,
        )
        self._event(
            execution,
            "execution.running",
            {"status": execution.status.value},
            key="running",
        )
        spool_dir = self.spool_root / execution.id
        spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool_dir.chmod(0o700)
        stdout_path = spool_dir / "stdout.raw"
        stderr_path = spool_dir / "stderr.raw"
        for path in (stdout_path, stderr_path):
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
        event_lock = asyncio.Lock()
        decoders = {
            "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
            "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        }
        redactors = {"stdout": StatefulRedactor(), "stderr": StatefulRedactor()}
        redacted_parts: dict[str, list[str]] = {"stdout": [], "stderr": []}

        async def on_chunk(stream: str, data: bytes) -> None:
            path = stdout_path if stream == "stdout" else stderr_path
            async with event_lock:
                offset = path.stat().st_size
                with path.open("ab", buffering=0) as output:
                    output.write(data)
                    os.fsync(output.fileno())
                decoded = decoders[stream].decode(data, final=False)
                display = redactors[stream].feed(decoded)
                if display:
                    redacted_parts[stream].append(display)
                    self._event(
                        execution,
                        f"execution.{stream}",
                        {
                            "stream": stream,
                            "text": display,
                            "raw_offset": offset,
                            "raw_length": len(data),
                            "raw_sha256": hashlib.sha256(data).hexdigest(),
                        },
                    )

        environment: dict[str, str] = {}
        egress_rules: list[EgressRule] = []
        pinned_hosts: dict[str, str] = {}
        if network_enabled:
            environment = {
                "NEBULA_TARGET": execution.network.target or "",
                "NEBULA_PORTS": json.dumps(execution.network.ports),
            }
            egress_rules = [
                EgressRule(address=address, ports=execution.network.ports)
                for address in execution.network.resolved_addresses
            ]
            host = _target_host(execution.network.target or "")
            if host and execution.network.resolved_addresses:
                pinned_hosts[host] = execution.network.resolved_addresses[0]
        sandbox_request = SandboxRequest(
            image=resolution.image,
            command=[
                resolution.runtime.adapter,
                "code",
                "--language",
                execution.language,
            ],
            workspace=resolution.workspace,
            workspace_access=SandboxWorkspaceAccess.WRITE,
            environment=environment,
            network=(SandboxNetwork.SCOPED if network_enabled else SandboxNetwork.NONE),
            execution_kind=(
                SandboxExecutionKind.NETWORK_TOOL
                if network_enabled
                else SandboxExecutionKind.LOCAL_TOOL
            ),
            egress_rules=egress_rules,
            pinned_hosts=pinned_hosts,
            limits=SandboxLimits(
                cpu_count=execution.limits.cpu_count,
                memory_mb=execution.limits.memory_mb,
                pids=execution.limits.pids,
                timeout_seconds=execution.limits.timeout_seconds,
                output_bytes=execution.limits.output_bytes_per_stream,
            ),
        )
        source_artifact = self.store.get(Artifact, execution.source_artifact_id)
        source = self.artifact_store.read(source_artifact)
        runner_task = asyncio.create_task(
            resolution.runner.run_stream(
                sandbox_request,
                input_bytes=source,
                on_chunk=on_chunk,
                container_name=f"nebula-exec-{execution.id.replace('-', '')}",
            )
        )
        monitor_task = asyncio.create_task(
            self._monitor_workspace(resolution.workspace, runner_task)
        )
        result = None
        failure: Exception | None = None
        cancelled = False
        try:
            done, _ = await asyncio.wait(
                {runner_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor_task in done:
                monitor_error = monitor_task.exception()
                if monitor_error is not None:
                    runner_task.cancel()
                    await asyncio.gather(runner_task, return_exceptions=True)
                    raise monitor_error
            result = await runner_task
        except asyncio.CancelledError:
            cancelled = True
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
        except Exception as exc:
            failure = exc
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            async with event_lock:
                for stream in ("stdout", "stderr"):
                    tail = decoders[stream].decode(b"", final=True)
                    display = redactors[stream].feed(tail) + redactors[stream].finish()
                    if display:
                        redacted_parts[stream].append(display)
                        self._event(
                            execution,
                            f"execution.{stream}",
                            {"stream": stream, "text": display, "final": True},
                        )
        after = await asyncio.to_thread(_workspace_snapshot, resolution.workspace)
        changes = _workspace_changes(before, after)
        error_code: str | None
        error_detail: str | None
        exit_code: int | None
        if cancelled:
            terminal = (
                OperatorExecutionStatus.INTERRUPTED
                if self._shutting_down
                else OperatorExecutionStatus.CANCELLED
            )
            error_code = "interrupted" if self._shutting_down else "cancelled"
            error_detail = f"execution {error_code}"
            exit_code = None
            truncated = False
        elif isinstance(failure, _WorkspaceLimitError):
            terminal = OperatorExecutionStatus.FAILED
            error_code = "workspace_limit"
            error_detail = str(failure)
            exit_code = None
            truncated = False
        elif failure is not None:
            terminal = OperatorExecutionStatus.FAILED
            error_code = _execution_failure_code(failure)
            error_detail = str(failure)[:4000]
            exit_code = None
            truncated = False
        elif result is not None and result.timed_out:
            terminal = OperatorExecutionStatus.TIMED_OUT
            error_code = "timeout"
            error_detail = "execution exceeded its fixed runtime limit"
            exit_code = None
            truncated = result.output_truncated
        else:
            terminal = OperatorExecutionStatus.COMPLETED
            error_code = "output_limit" if result and result.output_truncated else None
            error_detail = (
                "captured output reached the per-stream limit"
                if result and result.output_truncated
                else None
            )
            exit_code = result.exit_code if result is not None else None
            truncated = bool(result and result.output_truncated)
        await asyncio.shield(
            self._persist_terminal(
                execution.id,
                stdout_path,
                stderr_path,
                "".join(redacted_parts["stdout"]),
                "".join(redacted_parts["stderr"]),
                status=terminal,
                error_code=error_code,
                error_detail=error_detail,
                exit_code=exit_code,
                output_truncated=truncated,
                workspace_changes=changes,
            )
        )
        shutil.rmtree(spool_dir, ignore_errors=True)
        if cancelled:
            raise asyncio.CancelledError

    async def _persist_terminal(
        self,
        execution_id: str,
        stdout_path: Path,
        stderr_path: Path,
        redacted_stdout: str,
        redacted_stderr: str,
        *,
        status: OperatorExecutionStatus,
        error_code: str | None,
        error_detail: str | None,
        exit_code: int | None,
        output_truncated: bool,
        workspace_changes: list[WorkspaceChange],
    ) -> OperatorExecution:
        execution = self.store.get(OperatorExecution, execution_id)
        stored: list[StoredArtifact] = []
        try:
            stored.extend(
                [
                    self.artifact_store.put_file_with_status(
                        stdout_path,
                        engagement_id=execution.engagement_id,
                        filename=f"execution-{execution.id}-stdout.raw",
                        media_type="application/octet-stream",
                        source="operator-execution-stdout",
                        metadata={"execution_id": execution.id, "stream": "stdout"},
                    ),
                    self.artifact_store.put_file_with_status(
                        stderr_path,
                        engagement_id=execution.engagement_id,
                        filename=f"execution-{execution.id}-stderr.raw",
                        media_type="application/octet-stream",
                        source="operator-execution-stderr",
                        metadata={"execution_id": execution.id, "stream": "stderr"},
                    ),
                    self.artifact_store.put_bytes_with_status(
                        redacted_stdout.encode("utf-8"),
                        engagement_id=execution.engagement_id,
                        filename=f"execution-{execution.id}-stdout.txt",
                        media_type="text/plain",
                        source="operator-execution-redacted-stdout",
                        metadata={"execution_id": execution.id, "redacted": True},
                    ),
                    self.artifact_store.put_bytes_with_status(
                        redacted_stderr.encode("utf-8"),
                        engagement_id=execution.engagement_id,
                        filename=f"execution-{execution.id}-stderr.txt",
                        media_type="text/plain",
                        source="operator-execution-redacted-stderr",
                        metadata={"execution_id": execution.id, "redacted": True},
                    ),
                ]
            )
            stdout, stderr, safe_stdout, safe_stderr = [
                item.artifact for item in stored
            ]
            manifest_payload = {
                "protocol": "nebula.operator-execution/v1",
                "execution_id": execution.id,
                "source": {
                    "artifact_id": execution.source_artifact_id,
                    "sha256": execution.source_sha256,
                },
                "stdout": _artifact_descriptor(stdout),
                "stderr": _artifact_descriptor(stderr),
                "redacted_stdout": _artifact_descriptor(safe_stdout),
                "redacted_stderr": _artifact_descriptor(safe_stderr),
                "runtime": execution.runtime.model_dump(mode="json"),
                "network": execution.network.model_dump(mode="json"),
                "limits": execution.limits.model_dump(mode="json"),
                "status": status.value,
                "exit_code": exit_code,
                "output_truncated": output_truncated,
            }
            manifest = self.artifact_store.put_bytes_with_status(
                json.dumps(
                    manifest_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8"),
                engagement_id=execution.engagement_id,
                filename=f"execution-{execution.id}-manifest.json",
                media_type="application/json",
                source="operator-execution-manifest",
                metadata={"execution_id": execution.id},
            )
            stored.append(manifest)
            evidence: Evidence | None = None
            if stdout.size or stderr.size:
                evidence = Evidence(
                    engagement_id=execution.engagement_id,
                    evidence_type="operator-execution",
                    title=f"Execution {execution.id[:8]} output",
                    description=(
                        f"{execution.language} exited with {exit_code}"
                        if exit_code is not None
                        else f"{execution.language} ended with {status.value}"
                    ),
                    artifact_id=manifest.artifact.id,
                    execution_id=execution.id,
                    sha256=manifest.artifact.sha256,
                    captured_by=(
                        execution.operator_id
                        if execution.operator_id != "local-operator"
                        else None
                    ),
                    source_version="nebula.operator-execution/v1",
                    metadata={
                        "stdout_artifact_id": stdout.id,
                        "stderr_artifact_id": stderr.id,
                        "output_truncated": output_truncated,
                    },
                )
            changes: dict[str, Any] = {
                "status": status,
                "completed_at": utc_now(),
                "exit_code": exit_code,
                "error_code": error_code,
                "error_detail": error_detail,
                "output_truncated": output_truncated,
                "stdout_artifact_id": stdout.id,
                "stderr_artifact_id": stderr.id,
                "redacted_stdout_artifact_id": safe_stdout.id,
                "redacted_stderr_artifact_id": safe_stderr.id,
                "manifest_artifact_id": manifest.artifact.id,
                "evidence_id": evidence.id if evidence else None,
                "workspace_changes": workspace_changes[:1000],
            }
            with self.store.transaction() as transaction:
                transaction.add_all([item.artifact for item in stored])
                if evidence is not None:
                    transaction.add(evidence)
                updated = transaction.update(
                    OperatorExecution,
                    execution.id,
                    changes,
                    expected_revision=execution.revision,
                )
        except Exception:
            for item in stored:
                self.artifact_store.discard_new_blob(item)
            raise
        self._event(
            updated,
            "execution.terminal",
            {
                "status": updated.status.value,
                "exit_code": updated.exit_code,
                "error_code": updated.error_code,
                "output_truncated": updated.output_truncated,
                "manifest_artifact_id": updated.manifest_artifact_id,
                "evidence_id": updated.evidence_id,
            },
            key="terminal",
        )
        return updated

    async def _terminal_without_output(
        self,
        execution: OperatorExecution,
        status: OperatorExecutionStatus,
        code: str,
        detail: str,
    ) -> OperatorExecution:
        updated = self.store.update(
            OperatorExecution,
            execution.id,
            {
                "status": status,
                "completed_at": utc_now(),
                "error_code": code,
                "error_detail": detail[:4000],
            },
            expected_revision=execution.revision,
        )
        self._event(
            updated,
            "execution.terminal",
            {"status": status.value, "error_code": code},
            key="terminal",
        )
        return updated

    async def _monitor_workspace(
        self, workspace: Path, runner_task: asyncio.Task[Any]
    ) -> None:
        while not runner_task.done():
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(_assert_workspace_limits, workspace)
            except _WorkspaceLimitError:
                raise

    def _assert_workspace_limits(self, engagement_id: str) -> None:
        if self.tool_platform is None:
            raise ExecutionServiceError(
                "runner_unavailable", "Toolbox execution is not configured"
            )
        try:
            _assert_workspace_limits(self.tool_platform.workspace_for(engagement_id))
        except _WorkspaceLimitError as exc:
            raise ExecutionServiceError("workspace_limit", str(exc)) from exc

    def _event(
        self,
        execution: OperatorExecution,
        event_type: str,
        payload: dict[str, Any],
        *,
        key: str | None = None,
    ) -> None:
        self.store.append_operation_event(
            execution.id,
            "execution",
            execution.engagement_id,
            event_type,
            payload,
            actor_id=execution.operator_id,
            idempotency_key=(f"execution:{execution.id}:{key}" if key else None),
        )

    def _sign_preview(
        self, binding: dict[str, Any], fingerprint: str, expires: datetime
    ) -> str:
        payload = json.dumps(
            {
                "binding": binding,
                "fingerprint": fingerprint,
                "expires": int(expires.timestamp()),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._preview_secret, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(signature)}"

    def _verify_preview_token(self, token: str, fingerprint: str) -> dict[str, Any]:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload = _unb64(encoded_payload)
            signature = _unb64(encoded_signature)
            expected = hmac.new(self._preview_secret, payload, hashlib.sha256).digest()
            decoded = json.loads(payload)
        except Exception as exc:
            raise ExecutionServiceError(
                "preview_stale", "execution preview token is invalid"
            ) from exc
        if not hmac.compare_digest(signature, expected):
            raise ExecutionServiceError(
                "preview_stale", "execution preview token is invalid"
            )
        if decoded.get("fingerprint") != fingerprint:
            raise ExecutionServiceError(
                "preview_stale", "execution preview token does not match the request"
            )
        if int(decoded.get("expires", 0)) <= int(utc_now().timestamp()):
            raise ExecutionServiceError(
                "preview_stale", "execution preview has expired"
            )
        binding = decoded.get("binding")
        if not isinstance(binding, dict) or _digest_json(binding) != fingerprint:
            raise ExecutionServiceError(
                "preview_stale", "execution preview binding is invalid"
            )
        return binding

    def _execution_for_idempotency(
        self, engagement_id: str, key: str
    ) -> OperatorExecution | None:
        for execution in self._all_executions(engagement_id):
            if execution.client_idempotency_key == key:
                return execution
        return None

    def _all_executions(
        self, engagement_id: str | None = None
    ) -> list[OperatorExecution]:
        rows: list[OperatorExecution] = []
        offset = 0
        while True:
            page = self.store.list_entities(
                OperatorExecution,
                engagement_id=engagement_id,
                offset=offset,
                limit=1_000,
            )
            rows.extend(page)
            if len(page) < 1_000:
                return rows
            offset += len(page)

    async def _cleanup_container(self, execution: OperatorExecution) -> None:
        try:
            resolution = self._resolve(
                execution.engagement_id,
                execution.language,
                network=execution.network.mode == ExecutionNetworkMode.SCOPED,
            )
            await resolution.runner._force_remove(
                f"nebula-exec-{execution.id.replace('-', '')}"
            )
        except Exception:
            return


def _digest_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _target_host(value: str) -> str:
    candidate = value.strip()
    if "://" in candidate:
        return (urlsplit(candidate).hostname or "").rstrip(".").lower()
    if candidate.startswith("[") and "]" in candidate:
        return candidate[1 : candidate.index("]")].lower()
    if candidate.count(":") == 1 and candidate.rsplit(":", 1)[1].isdigit():
        candidate = candidate.rsplit(":", 1)[0]
    return candidate.rstrip(".").lower()


def _resolve_target(value: str) -> list[str]:
    host = _target_host(value)
    if not host:
        raise ExecutionServiceError("policy_denied", "network target is invalid")
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        addresses = {
            str(ipaddress.ip_address(sockaddr[0]))
            for _family, _type, _protocol, _canonical, sockaddr in socket.getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise ExecutionServiceError(
            "policy_denied", "network target could not be resolved"
        ) from exc
    if not addresses:
        raise ExecutionServiceError(
            "policy_denied", "network target did not resolve to an address"
        )
    return sorted(
        addresses, key=lambda item: (ipaddress.ip_address(item).version, item)
    )


def _workspace_snapshot(workspace: Path) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for root, directories, files in os.walk(workspace, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(root) / name).is_symlink()
        )
        for name in sorted([*directories, *files]):
            path = Path(root) / name
            relative = path.relative_to(workspace).as_posix()
            metadata = path.lstat()
            kind = (
                "symlink"
                if path.is_symlink()
                else "directory"
                if path.is_dir()
                else "file"
            )
            result[relative] = (kind, metadata.st_size, metadata.st_mtime_ns)
    return result


def _workspace_changes(
    before: dict[str, tuple[str, int, int]],
    after: dict[str, tuple[str, int, int]],
) -> list[WorkspaceChange]:
    changes: list[WorkspaceChange] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append(
                WorkspaceChange(path=path, change="added", size=after[path][1])
            )
        elif path not in after:
            changes.append(WorkspaceChange(path=path, change="deleted"))
        elif before[path] != after[path]:
            changes.append(
                WorkspaceChange(path=path, change="modified", size=after[path][1])
            )
    return changes[:1000]


def _assert_workspace_limits(workspace: Path) -> None:
    entries = 0
    allocated = 0
    for root, directories, files in os.walk(workspace, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(root) / name).is_symlink()
        ]
        for name in [*directories, *files]:
            path = Path(root) / name
            metadata = path.lstat()
            entries += 1
            if entries > WORKSPACE_MAX_ENTRIES:
                raise _WorkspaceLimitError(
                    f"workspace exceeds {WORKSPACE_MAX_ENTRIES} entries"
                )
            if path.is_file() and not path.is_symlink():
                if metadata.st_size > WORKSPACE_MAX_FILE_BYTES:
                    raise _WorkspaceLimitError(
                        "workspace contains a file larger than 1 GiB"
                    )
                blocks = getattr(metadata, "st_blocks", 0)
                allocated += blocks * 512 if blocks else metadata.st_size
                if allocated > WORKSPACE_MAX_BYTES:
                    raise _WorkspaceLimitError("workspace exceeds the 5 GiB limit")


def _artifact_descriptor(artifact: Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "sha256": artifact.sha256,
        "size": artifact.size,
        "media_type": artifact.media_type,
    }


def _execution_failure_code(error: Exception) -> str:
    if isinstance(error, ExecutionServiceError):
        return error.code
    if isinstance(error, (SandboxUnavailable, ToolPlatformError)):
        return "runtime_unavailable"
    return "runner_unavailable"


__all__ = [
    "ExecutionCapabilities",
    "ExecutionCapability",
    "ExecutionNetworkRequest",
    "ExecutionPreflightRequest",
    "ExecutionPreflightResponse",
    "ExecutionService",
    "ExecutionServiceError",
    "ExecutionStartRequest",
    "MAX_SOURCE_BYTES",
    "PREVIEW_TTL_SECONDS",
    "TERMINAL_EXECUTION_STATUSES",
]
