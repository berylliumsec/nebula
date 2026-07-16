"""Versioned FastAPI surface for the Nebula 3 core."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import asyncio
import base64
import binascii
import hmac
import json
import re
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Mapping
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError, model_validator
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Scope

from . import chat as chat_runtime
from .artifacts import ArtifactStore, ArtifactStoreError
from .api_validation import ApiEntityValidator
from .chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompactionError,
    ChatConfigurationError,
    ChatError,
    ChatHistoryConflict,
    ChatPrivacyError,
    ChatResponseMessage,
    ChatService,
)
from .container_terminal import (
    ContainerTerminalCapacity,
    ContainerTerminalCapabilities,
    ContainerTerminalError,
    ContainerTerminalExit,
    ContainerTerminalOutput,
    ContainerTerminalPreflightRequest,
    ContainerTerminalPreflightResponse,
    ContainerTerminalRecoveryListResponse,
    ContainerTerminalRecoveryResponse,
    ContainerTerminalService,
    ContainerTerminalStartRequest,
    ContainerTerminalStartResponse,
    MAX_TERMINAL_INPUT_BYTES,
    TERMINAL_MAX_DURATION_SECONDS,
)
from .database import Database
from .diagnostics import (
    DiagnosticManager,
    current_operation_id,
    current_request_id,
    diagnostic_context,
    diagnostic_error_feature,
    diagnostic_error_id,
    gather_diagnostic,
    get_diagnostics,
    install_asyncio_exception_hook,
    new_error_id,
    new_request_id,
    record_diagnostic,
)
from .context import (
    DEFAULT_CONTEXT_WINDOW,
    ContextCompactor,
    ContextStatus,
    estimate_tokens,
    memory_text,
    resolve_context_limits,
)
from .credentials import (
    CredentialCreateRequest,
    CredentialError,
    CredentialStatus,
    CredentialStore,
    CredentialUnavailableError,
)
from .domain import (
    ENTITY_MODEL_BY_KIND,
    AgentAttempt,
    AgentRun,
    Approval,
    ApprovalStatus,
    Artifact,
    ChatBackend,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ChatTokenUsage,
    ContextOwnerType,
    ContextSnapshotStatus,
    Engagement,
    EngagementToolAssignment,
    Entity,
    Evidence,
    GeneratedDraft,
    HarnessInteraction,
    HarnessInteractionStatus,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    KnowledgeSource,
    MissionGrant,
    NebulaModel,
    OperationEvent,
    OperatorProfile,
    OperatorExecution,
    OperatorExecutionStatus,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    Report,
    Task,
    ReportRender,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    RunBudget,
    RunBackend,
    RunEvent,
    RunStatus,
    ScopePolicy,
    ToolCall,
    ToolPackInstallation,
    ToolPackInstallationStatus,
    ToolCallOrigin,
    utc_now,
)
from .evidence import (
    EvidenceReferenceError,
    EvidenceTooLargeError,
    EvidenceUploadRequest,
    InvalidEvidenceUploadError,
    upload_evidence,
)
from .exporter import ExportError, export_engagement
from .executions import (
    ExecutionCapabilities,
    ExecutionPreflightRequest,
    ExecutionPreflightResponse,
    ExecutionService,
    ExecutionServiceError,
    ExecutionStartRequest,
)
from .execution_ai import (
    DraftEditRequest,
    DraftNoteRequest,
    DraftTransitionRequest,
    ExecutionAIError,
    ExecutionAIService,
    ExecutionChatAttachRequest,
    ExecutionChatAttachment,
)
from .knowledge import (
    MAX_DOCUMENT_BYTES,
    DocumentTooLargeError,
    InvalidDocumentError,
    UnsupportedDocumentError,
    ingest_document,
    knowledge_summary,
    reindex_document,
)
from .missions import (
    MAX_API_MISSION_COST_USD,
    MAX_API_MISSION_DURATION_SECONDS,
    MAX_API_MISSION_RETRIES,
    MAX_API_MISSION_TOKENS,
    MissionCapacityError,
    MissionConfigurationError,
    MissionService,
    MissionServiceUnavailable,
    MissionStateError,
)
from .harnesses import (
    HarnessActivityEventList,
    HarnessConfigurationError,
    HarnessError,
    HarnessRuntimeService,
    HarnessSessionActivity,
    HarnessStateError,
    HarnessUnavailableError,
    harness_catalog,
)
from .mcp import McpProbeError, McpProbeReport, McpProbeService
from .operators import OperatorProfileService
from .providers import (
    ModelMessage,
    ModelRequest,
    PROVIDER_CATALOG,
    ProviderError,
    ProviderFlavor,
    ProviderHealth,
    ToolChoice,
    ToolDefinition,
    provider_from_profile,
)
from .reporting import ReportRenderError, ReportRenderService
from .report_signoff import ReportSignoffRequest, sign_off_report
from .setup import (
    ImagePreparationCancellationRequest,
    ImagePreparationRequest,
    RunnerSelectionRequest,
    SetupControlResponse,
    SetupEvent,
    SetupService,
    SetupServiceError,
    SetupStatus,
    bootstrap_scratch_project,
)
from .writing_ai import (
    WritingAIError,
    WritingAIService,
    WritingTransformRequest,
    WritingTransformResponse,
)
from .storage import ConflictError, NebulaStore, NotFoundError
from .terminal_history import (
    TerminalAuditImmutableError,
    TerminalCommandHistory,
    TerminalCommandHistoryClearResult,
    TerminalCommandHistoryPreferenceUpdate,
    TerminalCommandHistoryStatus,
    TerminalCommandPage,
    TerminalCommandStatus,
    TerminalRecordingTools,
    TerminalRecordingToolsConflict,
    TerminalRecordingToolsUpdate,
)
from .tool_platform import ToolPlatform, ToolPlatformError
from .toolpack_sdk import (
    CustomToolDefinition,
    generate_custom_tool_project,
    pack_tool_pack,
    custom_tool_manifest,
)
from .toolpacks import manifest_digest
from .tool_results import (
    ToolOutputAccessError,
    ToolOutputQueryError,
    ToolOutputService,
)
from .version import __version__, build_metadata
from .workspace import (
    WorkspaceListing,
    WorkspacePreview,
    WorkspacePromotionRequest,
    WorkspaceResetRequest,
    WorkspaceResetResult,
    WorkspaceService,
    WorkspaceUploadResult,
)

READ_ONLY_RESOURCES = {
    "agent_attempts",
    "approvals",
    "artifacts",
    "chat_messages",
    "chat_sessions",
    "chat_turns",
    "chat_turns",
    "evidence",
    "knowledge",
    "generated_drafts",
    "operator_executions",
    "report_renders",
    "runs",
    "harness_sessions",
    "harness_turns",
    "source_snapshots",
    "tasks",
    "tool_calls",
}
APPEND_ONLY_RESOURCES: set[str] = set()
CUSTOM_RESOURCES = {
    "chat_turns",
    "context_snapshots",
    "operator_profiles",
    "runner_profiles",
}

API_PREFIX = "/api/v1"
PROVIDER_CAPABILITY_PROBE_TIMEOUT_SECONDS = 30
TOOL_PACK_EVENT_POLL_SECONDS = 0.25
TOOL_PACK_EVENT_HEARTBEAT_TICKS = 20


def _websocket_protocol_secret(
    protocols: list[str], prefix: str, *, decode_base64: bool
) -> str | None:
    matches = [
        value.removeprefix(prefix) for value in protocols if value.startswith(prefix)
    ]
    if len(matches) != 1 or not matches[0]:
        return None
    if not decode_base64:
        return matches[0]
    try:
        return base64.urlsafe_b64decode(
            matches[0] + "=" * (-len(matches[0]) % 4)
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as caught_error:
        record_caught_exception(
            "api",
            "api.api.caught_failure_001",
            "A handled api operation raised an exception.",
            caught_error,
            stage="api",
        )
        return None


class SpaStaticFiles(StaticFiles):
    """Serve the workspace index for extensionless browser navigation routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_002",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            is_navigation = (
                exc.status_code == 404
                and scope.get("method") in {"GET", "HEAD"}
                and path != "api"
                and not path.startswith("api/")
                and not Path(path).suffix
            )
            if not is_navigation:
                raise
            return await super().get_response("index.html", scope)


class EventAppendRequest(NebulaModel):
    event_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=300)


class EventList(NebulaModel):
    events: list[RunEvent]
    next_sequence: int


class OperationEventList(NebulaModel):
    events: list[OperationEvent]
    next_sequence: int


class HarnessInteractionDecisionRequest(NebulaModel):
    action: Literal["answer", "decline", "cancel"]
    response: dict[str, Any] = Field(default_factory=dict)


class PatchRequest(NebulaModel):
    changes: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class ChatSessionRenameRequest(NebulaModel):
    title: str = Field(min_length=1, max_length=300)
    expected_revision: int | None = Field(default=None, ge=1)


class ProviderCapabilityVerifyRequest(NebulaModel):
    model: str = Field(min_length=1, max_length=500)
    expected_revision: int = Field(ge=1)


class ProviderCapabilityVerifyResponse(NebulaModel):
    provider_id: str
    provider_revision: int
    verification: ProviderCapabilityVerification


class LocalProviderDetection(NebulaModel):
    flavor: ProviderFlavor
    display_name: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=1, max_length=2_048)
    models: list[str] = Field(default_factory=list, max_length=256)


class ChatTurnSummary(NebulaModel):
    id: str
    session_id: str
    status: ChatTurnStatus
    approval_id: str | None = None
    harness_turn_id: str | None = None
    tool_call_ids: list[str] = Field(default_factory=list)
    revision: int = Field(ge=1)


class ApprovalDecisionRequest(NebulaModel):
    decision: str = Field(pattern=r"^(approve|reject|stop)$")
    reason: str | None = None
    edited_arguments: dict[str, Any] | None = None


class ToolOutputSearchRequest(NebulaModel):
    query: str = Field(min_length=1, max_length=512)
    mode: str = Field(default="literal", pattern=r"^(literal|regex)$")
    case_sensitive: bool = False
    context_lines: int = Field(default=0, ge=0, le=5)
    match_limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=4096)


class ToolOutputReadRequest(NebulaModel):
    starting_line: int = Field(default=1, ge=1)
    line_count: int = Field(default=100, ge=1, le=200)


class KnowledgeIngestRequest(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=1024)
    media_type: str | None = Field(default=None, max_length=200)
    content_base64: str = Field(
        min_length=1,
        max_length=4 * ((MAX_DOCUMENT_BYTES + 2) // 3),
    )


class MissionStartRequest(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=10_000)
    backend: RunBackend = RunBackend.NATIVE
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    harness_session_id: str | None = Field(default=None, min_length=1, max_length=200)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=500)
    max_duration_seconds: int = Field(
        default=900, ge=1, le=MAX_API_MISSION_DURATION_SECONDS
    )
    max_tokens: int = Field(default=32_000, ge=1, le=MAX_API_MISSION_TOKENS)
    max_cost_usd: float | None = Field(default=None, ge=0, le=MAX_API_MISSION_COST_USD)
    max_retries: int = Field(default=1, ge=0, le=MAX_API_MISSION_RETRIES)
    tool_names: list[str] = Field(default_factory=list, max_length=64)
    max_tool_calls: int = Field(default=0, ge=0, le=100)
    max_artifact_queries: int = Field(default=200, ge=0, le=1000)
    max_concurrency: int = Field(default=1, ge=1, le=2)
    allow_cloud_tool_results: bool = False

    @model_validator(mode="after")
    def runtime_is_discriminated(self) -> "MissionStartRequest":
        if self.backend == RunBackend.NATIVE:
            if not self.provider_id or not self.model:
                raise ValueError("native missions require provider_id and model")
            if self.harness_profile_id or self.harness_session_id:
                raise ValueError(
                    "native missions cannot include harness runtime fields"
                )
        elif not self.harness_profile_id or self.provider_id:
            raise ValueError(
                "harness missions require harness_profile_id and no provider_id"
            )
        return self


class HarnessSteerRequest(NebulaModel):
    text: str = Field(min_length=1, max_length=20_000)


class HarnessCheckpointRewindRequest(NebulaModel):
    checkpoint_id: str = Field(min_length=1, max_length=500)


class McpProbeRequest(NebulaModel):
    engagement_id: str | None = Field(default=None, min_length=1, max_length=200)


class HarnessMissionHandoffRequest(NebulaModel):
    objective: str | None = Field(default=None, min_length=1, max_length=10_000)
    max_duration_seconds: int = Field(
        default=900, ge=1, le=MAX_API_MISSION_DURATION_SECONDS
    )
    max_tokens: int = Field(default=32_000, ge=1, le=MAX_API_MISSION_TOKENS)
    max_cost_usd: float | None = Field(default=None, ge=0, le=MAX_API_MISSION_COST_USD)
    max_tool_calls: int = Field(default=100, ge=0, le=100)
    max_artifact_queries: int = Field(default=200, ge=0, le=1000)
    allow_cloud_tool_results: bool = False


class ScopePolicyUpdateRequest(NebulaModel):
    allowed_cidrs: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_urls: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=list)
    not_before: datetime | None = None
    not_after: datetime | None = None
    prohibited_actions: list[str] = Field(default_factory=list)
    local_only: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=256)
    grants: list[MissionGrant] = Field(default_factory=list)
    expected_revision: int | None = Field(default=None, ge=1)


class EngagementToolAssignmentRequest(NebulaModel):
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_names: list[str] = Field(default_factory=list, max_length=64)
    enabled: bool = True
    expected_revision: int | None = Field(default=None, ge=1)


class RunnerProfileRequest(NebulaModel):
    name: str = Field(min_length=1, max_length=200)
    runtime: RunnerRuntime
    executable: str
    context: str | None = Field(default=None, max_length=500)
    socket: str | None = Field(default=None, max_length=2048)
    platform: str = Field(pattern=r"^linux/(amd64|arm64)$")
    isolation: RunnerIsolation
    enabled: bool = True
    egress_helper_image: str | None = None
    seccomp_profile: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class ToolPackInstallRequest(NebulaModel):
    catalog_id: str = Field(min_length=1, max_length=500)
    version: str | None = Field(default=None, max_length=100)
    runtime_profile_id: str = Field(min_length=1, max_length=200)


class ToolCollectionInstallRequest(NebulaModel):
    collection_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$"
    )
    runtime_profile_id: str = Field(min_length=1, max_length=200)


class LocalToolPackInstallRequest(NebulaModel):
    bundle_base64: str = Field(min_length=1, max_length=24_000_000)
    runtime_profile_id: str = Field(min_length=1, max_length=200)
    developer_mode_confirmed: bool = False


class CustomToolBundleResponse(NebulaModel):
    filename: str
    bundle_base64: str
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    permission_preview: dict[str, Any]


class MissionStopRequest(NebulaModel):
    reason: str = Field(default="Stopped by operator", max_length=1_000)


class OperatorProfileCreateRequest(NebulaModel):
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    role: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorProfileUpdateRequest(NebulaModel):
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    role: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class OperatorProfileActivateRequest(NebulaModel):
    expected_revision: int | None = Field(default=None, ge=1)


class ReportRenderRequest(NebulaModel):
    report_revision: int = Field(ge=1)


class DiagnosticsSettingsRequest(NebulaModel):
    schema_: Literal["nebula.diagnostics-settings/v1"] = Field(
        default="nebula.diagnostics-settings/v1", alias="schema"
    )
    global_level: Literal["debug", "info", "warning", "error", "critical"]
    feature_levels: dict[
        str, Literal["debug", "info", "warning", "error", "critical"]
    ] = Field(default_factory=dict, max_length=64)


class BrowserDiagnosticStackFrame(NebulaModel):
    module: str = Field(min_length=1, max_length=128)
    function: str = Field(min_length=1, max_length=128)
    line: int = Field(ge=0, le=10_000_000)


class BrowserDiagnosticEvent(NebulaModel):
    schema_: Literal["nebula.diagnostic/v1"] = Field(
        default="nebula.diagnostic/v1", alias="schema"
    )
    level: Literal["debug", "info", "warning", "error", "critical"]
    feature: Literal["interface"] = "interface"
    event_code: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$",
    )
    message: str = Field(min_length=1, max_length=2_048)
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    parent_operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    error_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    project_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    execution_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    outcome: str | None = Field(default=None, max_length=64)
    stage: str | None = Field(default=None, max_length=128)
    duration_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    retryable: bool | None = None
    safe_failure_cause: str | None = Field(default=None, max_length=2_048)
    exception_type: str | None = Field(default=None, min_length=1, max_length=128)
    stack_frames: list[BrowserDiagnosticStackFrame] = Field(
        default_factory=list, max_length=32
    )
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)


class BrowserDiagnosticBatch(NebulaModel):
    events: list[BrowserDiagnosticEvent] = Field(min_length=1, max_length=100)


def create_app(
    store: NebulaStore | None = None,
    *,
    database: Database | str | Path | None = None,
    artifact_store: ArtifactStore | None = None,
    auth_token: str | None = None,
    allow_unauthenticated: bool = False,
    allow_internal_event_append: bool = False,
    cors_origins: list[str] | None = None,
    static_dir: str | Path | None = None,
    mission_service: MissionService | None = None,
    harness_runtime_service: HarnessRuntimeService | None = None,
    mission_checkpoint_path: str | Path | None = None,
    tool_platform: ToolPlatform | None = None,
    enable_executable_missions: bool | None = None,
    execution_service: ExecutionService | None = None,
    execution_data_root: str | Path | None = None,
    container_terminal_service: ContainerTerminalService | None = None,
    workspace_service: WorkspaceService | None = None,
    report_render_service: ReportRenderService | None = None,
    execution_ai_service: ExecutionAIService | None = None,
    writing_ai_service: WritingAIService | None = None,
    credential_store: CredentialStore | None = None,
    bootstrap_workspace: bool = False,
    diagnostic_manager: DiagnosticManager | None = None,
    allow_browser_diagnostic_events: bool = False,
) -> FastAPI:
    """Build an app without importing or initializing any Qt component.

    When no token is supplied a cryptographically random local IPC token is
    generated and exposed as ``app.state.auth_token`` for the launching process.
    """

    if store is None:
        location = database or Path.home() / ".local/share/nebula/v3/nebula.db"
        store = NebulaStore(location)
    elif database is not None:
        raise ValueError("pass either store or database, not both")
    token = auth_token or secrets.token_urlsafe(32)
    if not token and not allow_unauthenticated:
        raise ValueError("auth_token cannot be empty")
    if mission_service is not None and mission_checkpoint_path is not None:
        raise ValueError(
            "pass either mission_service or mission_checkpoint_path, not both"
        )
    diagnostics = diagnostic_manager or get_diagnostics()

    def emit_diagnostic(
        level: str,
        feature: str,
        event_code: str,
        message: str,
        **fields: Any,
    ) -> str | None:
        if diagnostics is not None:
            return diagnostics.record(level, feature, event_code, message, **fields)
        return record_diagnostic(level, feature, event_code, message, **fields)

    def stream_error_frame(
        *,
        feature: str,
        code: str,
        detail: str,
        exception: BaseException | None = None,
        retryable: bool = False,
        expected: bool = False,
        request_id: str | None = None,
        session_id: str | None = None,
        execution_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record and return the compatible safe WebSocket/SSE error envelope."""

        level = "warning" if expected else "error"
        existing_error_id = (
            diagnostic_error_id(exception)
            if exception is not None and level == "error"
            else None
        )
        error_id = existing_error_id
        if existing_error_id is None:
            error_id = emit_diagnostic(
                level,
                feature,
                f"{feature}.stream.rejected"
                if expected
                else f"{feature}.stream.failed",
                f"A {feature.replace('-', ' ')} stream could not continue.",
                outcome="denied" if expected else "failure",
                stage="stream",
                retryable=retryable,
                safe_failure_cause=(
                    "The stream frame was rejected safely."
                    if expected
                    else "The streaming operation failed."
                ),
                exception=exception,
                request_id=request_id or current_request_id(),
                session_id=session_id,
                execution_id=execution_id,
                run_id=run_id,
                metadata={"code": code},
            )
        frame: dict[str, Any] = {
            "type": "error",
            "code": code,
            "detail": detail,
            "feature": feature,
            "retryable": retryable,
            "help_article": help_article_for(feature, code),
        }
        correlation_request = request_id or current_request_id()
        if correlation_request:
            frame["request_id"] = correlation_request
        if error_id:
            frame["error_id"] = error_id
        return frame

    if bootstrap_workspace:
        bootstrap_scratch_project(store)
    executable_missions_enabled = (
        tool_platform.execution_enabled
        if enable_executable_missions is None and tool_platform is not None
        else bool(enable_executable_missions)
    )

    credentials = credential_store or CredentialStore()

    def harness_workspace(engagement_id: str) -> Path:
        if tool_platform is None:
            raise HarnessUnavailableError(
                "harness execution requires an engagement workspace"
            )
        return tool_platform.workspace_for(engagement_id)

    harness_runtime = harness_runtime_service or HarnessRuntimeService(
        store,
        credential_store=credentials,
        workspace_resolver=harness_workspace,
        artifact_store=artifact_store,
        tool_platform=tool_platform,
    )
    if harness_runtime.store is not store:
        raise ValueError("harness_runtime_service must use the API store")
    if tool_platform is not None:
        harness_runtime.bind_tool_platform(tool_platform)
    mcp_probes = McpProbeService(
        store,
        credential_store=credentials,
        workspace_resolver=harness_workspace,
    )
    if tool_platform is not None:
        tool_platform.bind_mcp_service(mcp_probes)

    def provider_factory(profile: ProviderProfile):
        try:
            if profile.secret_ref and profile.secret_ref.startswith(
                ("vault:", "session:")
            ):
                return provider_from_profile(profile, credentials.resolve)
            return provider_from_profile(profile)
        except CredentialError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_003",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise ProviderError(str(exc)) from exc

    def chat_provider_factory(profile: ProviderProfile):
        # Keep ChatService's provider seam available to embedders, but resolve
        # opaque Core-managed references before a request leaves the process.
        if profile.secret_ref and profile.secret_ref.startswith(("vault:", "session:")):
            return provider_factory(profile)
        return chat_runtime.provider_from_profile(profile)

    missions = mission_service or MissionService(
        store,
        checkpoint_path=mission_checkpoint_path,
        provider_factory=provider_factory,
        tool_components_factory=(
            tool_platform.mission_components if tool_platform is not None else None
        ),
    )
    if missions.store is not store:
        raise ValueError("mission_service must use the API store")
    entity_validator = ApiEntityValidator(store)
    operators = OperatorProfileService(store)

    def chat_service() -> ChatService:
        return ChatService(
            store,
            tool_platform=tool_platform,
            provider_factory=chat_provider_factory,
            operator_id=active_operator_id,
        )

    def active_operator_id() -> str:
        active = operators.active_profile_or_none()
        # Work can begin before the user chooses a display name. Attribute that
        # technical activity to the system rather than inventing a human actor.
        return active.id if active is not None else "system"

    executions = execution_service
    if executions is None and artifact_store is not None and tool_platform is not None:
        executions = ExecutionService(
            store=store,
            artifact_store=artifact_store,
            tool_platform=tool_platform,
            data_root=execution_data_root or artifact_store.root.parent,
            operator_id=active_operator_id,
        )
    if executions is not None and executions.store is not store:
        raise ValueError("execution_service must use the API store")
    terminal_commands = TerminalCommandHistory(
        store.database,
        store=store,
        artifact_store=artifact_store,
    )
    inventory_loader = (
        getattr(tool_platform, "last_human_terminal_security_inventory", None)
        if tool_platform is not None
        else None
    )
    if callable(inventory_loader):
        cached_inventory = inventory_loader()
        if cached_inventory is not None:
            image_digest, manifest_sha256, default_tools = cached_inventory
            terminal_commands.register_tool_inventory(
                runtime_image_digest=image_digest,
                manifest_sha256=manifest_sha256,
                default_tools=default_tools,
            )
    container_terminals = container_terminal_service
    if container_terminals is None and tool_platform is not None:
        container_terminals = ContainerTerminalService(
            store=store,
            tool_platform=tool_platform,
            execution_service=executions,
            command_history=terminal_commands,
            operator_id=active_operator_id,
        )
    if container_terminals is not None and container_terminals.store is not store:
        raise ValueError("container_terminal_service must use the API store")
    if (
        container_terminals is not None
        and executions is not None
        and container_terminals.execution_service is None
    ):
        container_terminals.bind_execution_service(executions)
    if container_terminals is not None and container_terminals.command_history is None:
        container_terminals.bind_command_history(terminal_commands)
    workspaces = workspace_service
    if workspaces is None and artifact_store is not None and tool_platform is not None:
        workspaces = WorkspaceService(
            store=store,
            artifact_store=artifact_store,
            tool_platform=tool_platform,
            operator_id=active_operator_id,
        )
    if workspaces is not None and workspaces.store is not store:
        raise ValueError("workspace_service must use the API store")
    report_renders = report_render_service
    if report_renders is None and artifact_store is not None:
        report_renders = ReportRenderService(
            store=store,
            artifact_store=artifact_store,
            operator_id=active_operator_id,
        )
    if report_renders is not None and report_renders.store is not store:
        raise ValueError("report_render_service must use the API store")
    execution_ai = execution_ai_service
    if execution_ai is None and artifact_store is not None:
        execution_ai = ExecutionAIService(
            store=store,
            artifact_store=artifact_store,
            provider_factory=provider_factory,
            operator_id=active_operator_id,
        )
    if execution_ai is not None and execution_ai.store is not store:
        raise ValueError("execution_ai_service must use the API store")
    writing_ai = writing_ai_service or WritingAIService(
        store=store,
        provider_factory=provider_factory,
    )
    if writing_ai.store is not store:
        raise ValueError("writing_ai_service must use the API store")
    setup = SetupService(store, tool_platform)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        install_asyncio_exception_hook()
        started: list[tuple[str, str, Callable[[], Any]]] = []

        async def start_component(
            feature: str,
            component: str,
            startup: Callable[[], Any],
            shutdown: Callable[[], Any],
        ) -> None:
            try:
                result = startup()
                if asyncio.iscoroutine(result):
                    await result
            except BaseException as exc:
                emit_diagnostic(
                    "critical",
                    feature,
                    f"{feature}.{component}.startup_failed",
                    f"{component.replace('-', ' ').title()} could not start.",
                    outcome="failure",
                    stage="startup",
                    retryable=True,
                    exception=exc,
                    metadata={"component": component},
                )
                raise
            started.append((feature, component, shutdown))
            emit_diagnostic(
                "info",
                feature,
                f"{feature}.{component}.started",
                f"{component.replace('-', ' ').title()} started.",
                outcome="success",
                stage="startup",
                metadata={"component": component},
            )

        async def stop_components() -> list[BaseException]:
            failures: list[BaseException] = []
            while started:
                feature, component, shutdown = started.pop()
                try:
                    result = shutdown()
                    if asyncio.iscoroutine(result):
                        await result
                except BaseException as exc:
                    failures.append(exc)
                    emit_diagnostic(
                        "error",
                        feature,
                        f"{feature}.{component}.cleanup_failed",
                        f"{component.replace('-', ' ').title()} cleanup did not complete.",
                        outcome="failure",
                        stage="shutdown",
                        retryable=True,
                        exception=exc,
                        metadata={"component": component},
                    )
                else:
                    emit_diagnostic(
                        "info",
                        feature,
                        f"{feature}.{component}.stopped",
                        f"{component.replace('-', ' ').title()} stopped.",
                        outcome="success",
                        stage="shutdown",
                        metadata={"component": component},
                    )
            return failures

        try:
            await start_component("setup", "coordinator", setup.start, setup.shutdown)
            if container_terminals is not None:
                await start_component(
                    "terminal",
                    "container-service",
                    container_terminals.startup,
                    container_terminals.shutdown,
                )
            if executions is not None:
                await start_component(
                    "executions", "service", executions.startup, executions.shutdown
                )
            if report_renders is not None:
                await start_component(
                    "reports",
                    "renderer",
                    report_renders.startup,
                    report_renders.shutdown,
                )
            if execution_ai is not None:
                await start_component(
                    "executions",
                    "ai-service",
                    execution_ai.startup,
                    execution_ai.shutdown,
                )
            await start_component(
                "harnesses",
                "runtime",
                harness_runtime.startup,
                harness_runtime.shutdown,
            )
            await start_component(
                "missions", "service", missions.startup, missions.shutdown
            )
        except BaseException:
            await stop_components()
            raise
        try:
            yield
        finally:
            failures = await stop_components()
            if failures:
                raise RuntimeError(
                    f"{len(failures)} Nebula Core service cleanup operation(s) failed"
                ) from failures[0]

    app = FastAPI(
        title="Nebula 3 Core API",
        version=__version__,
        description="Local-first, UI-independent security engagement control plane.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.artifact_store = artifact_store
    app.state.auth_token = token
    app.state.allow_unauthenticated = allow_unauthenticated
    app.state.diagnostics = diagnostics
    app.state.allow_browser_diagnostic_events = allow_browser_diagnostic_events
    app.state.mission_service = missions
    app.state.harness_runtime_service = harness_runtime
    app.state.mcp_probe_service = mcp_probes
    app.state.operator_profile_service = operators
    app.state.credential_store = credentials
    app.state.tool_platform = tool_platform
    app.state.execution_service = executions
    app.state.container_terminal_service = container_terminals
    app.state.workspace_service = workspaces
    app.state.report_render_service = report_renders
    app.state.execution_ai_service = execution_ai
    app.state.writing_ai_service = writing_ai
    app.state.setup_service = setup
    app.state.terminal_command_history = terminal_commands
    app.state.executable_missions_enabled = executable_missions_enabled
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins
        or [
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "http://127.0.0.1:1420",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "If-Match",
            "Last-Event-ID",
            "X-Nebula-Operation-ID",
            "X-Nebula-Sensitive-Data-Acknowledged",
        ],
        expose_headers=["X-Request-ID"],
    )

    route_feature_by_tag = {
        "administration": "storage",
        "approvals": "missions",
        "artifacts": "evidence",
        "chat": "chat",
        "chat-messages": "chat",
        "chat-sessions": "chat",
        "chat-turns": "chat",
        "container-terminal": "terminal",
        "context-snapshots": "knowledge",
        "credentials": "providers",
        "diagnostics": "diagnostics",
        "engagement-tool-assignments": "toolbox",
        "engagements": "projects",
        "evidence": "evidence",
        "execution-ai": "executions",
        "executions": "executions",
        "exports": "storage",
        "findings": "findings",
        "generated-drafts": "executions",
        "harness-sessions": "harnesses",
        "harness-turns": "harnesses",
        "harnesses": "harnesses",
        "knowledge": "knowledge",
        "mcp": "harnesses",
        "mcp-servers": "harnesses",
        "observations": "notes",
        "operator-executions": "executions",
        "operator-profiles": "projects",
        "overview": "projects",
        "providers": "providers",
        "report-renders": "reports",
        "reports": "reports",
        "writing-ai": "reports",
        "runner-profiles": "sandbox",
        "runners": "sandbox",
        "runs": "missions",
        "setup": "setup",
        "source-snapshots": "knowledge",
        "system": "api",
        "tasks": "missions",
        "agent-attempts": "missions",
        "tool-calls": "missions",
        "tool-pack-installations": "toolbox",
        "tool-packs": "toolbox",
        "workspace": "workspace",
        **{
            tag: "projects"
            for tag in (
                "advisories",
                "assets",
                "correlations",
                "identities",
                "remediations",
                "scope-policies",
                "services",
                "software-components",
            )
        },
    }

    def request_feature(request: Request) -> str:
        route = request.scope.get("route")
        for tag in getattr(route, "tags", ()):
            feature = route_feature_by_tag.get(str(tag))
            if feature:
                return feature
        return "api"

    def exception_feature(exc: BaseException, request: Request | None = None) -> str:
        if isinstance(
            exc,
            (
                MissionConfigurationError,
                MissionCapacityError,
                MissionStateError,
                MissionServiceUnavailable,
            ),
        ):
            return "missions"
        if isinstance(exc, (HarnessError, McpProbeError)):
            return "harnesses"
        if isinstance(exc, ToolPlatformError):
            return "toolbox"
        if isinstance(exc, ContainerTerminalError):
            return "terminal"
        if isinstance(exc, (ExecutionServiceError, ExecutionAIError)):
            return "executions"
        if isinstance(exc, ReportRenderError):
            return "reports"
        if isinstance(exc, ArtifactStoreError):
            return "storage"
        if isinstance(exc, ExportError):
            return "evidence"
        if isinstance(exc, ChatError):
            return "chat"
        if isinstance(exc, ProviderError):
            return "providers"
        if isinstance(exc, (NotFoundError, ConflictError)):
            return request_feature(request) if request is not None else "projects"
        if request is not None and isinstance(
            exc,
            (HTTPException, RequestValidationError, ValidationError, ValueError),
        ):
            return request_feature(request)
        return "api"

    def exception_code(exc: BaseException, feature: str) -> str:
        supplied = getattr(exc, "code", None)
        if isinstance(supplied, str) and re.fullmatch(
            r"[a-z][a-z0-9._-]{2,159}", supplied
        ):
            return supplied
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
        return f"{feature}.{name}"

    def help_article_for(feature: str, code: str) -> str | None:
        if feature in {"storage", "diagnostics", "evidence"}:
            return "diagnostics"
        if feature == "terminal":
            return "human-terminal"
        if feature == "setup":
            return "runner-setup"
        if feature == "toolbox":
            return "toolbox"
        if feature == "harnesses":
            return "harnesses"
        if code.startswith("api."):
            return "core-startup"
        return None

    def diagnostic_error_response(
        request: Request,
        exc: BaseException,
        *,
        status_code: int,
        detail: Any,
        code: str | None = None,
        retryable: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        feature = diagnostic_error_feature(exc) or exception_feature(exc, request)
        explicit_code = code is not None
        stable_code = code or exception_code(exc, feature)
        severity = "error" if status_code >= 500 else "warning"
        request_id = getattr(request.state, "request_id", None)
        existing_error_id = diagnostic_error_id(exc) if severity == "error" else None
        error_id = existing_error_id or (
            new_error_id() if severity == "error" else None
        )
        recorded_id = existing_error_id
        if existing_error_id is None:
            recorded_id = emit_diagnostic(
                severity,
                feature,
                f"{stable_code}.request_failed",
                f"A {feature.replace('-', ' ')} request could not complete.",
                error_id=error_id,
                request_id=request_id,
                outcome="failure" if severity == "error" else "denied",
                stage="request",
                retryable=retryable,
                safe_failure_cause=(
                    "A service dependency or internal operation failed."
                    if status_code >= 500
                    else "The request was rejected safely."
                ),
                exception=exc,
                metadata={"http_status": status_code, "code": stable_code},
            )
        request.state.diagnostic_error_recorded = True
        request.state.diagnostic_error_id = recorded_id or error_id
        content: dict[str, Any] = {"detail": detail}
        if explicit_code:
            content["code"] = stable_code
        # Keep direct embedding/tests compatible when no diagnostics owner was
        # configured. Production Core always has one.
        if diagnostics is not None:
            content.update(
                {
                    "code": stable_code,
                    "feature": feature,
                    "request_id": request_id,
                    "retryable": retryable,
                    "help_article": help_article_for(feature, stable_code),
                }
            )
            if recorded_id or error_id:
                content["error_id"] = recorded_id or error_id
        elif retryable:
            content["retryable"] = True
        return JSONResponse(
            status_code=status_code,
            content=jsonable_encoder(content),
            headers=dict(headers or {}),
        )

    @app.middleware("http")
    async def diagnostic_request_middleware(
        request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        request_id = new_request_id()
        request.state.request_id = request_id
        request.state.diagnostic_error_recorded = False
        operation_id = request.headers.get("X-Nebula-Operation-ID")
        started = time.monotonic()
        with diagnostic_context(request_id=request_id, operation_id=operation_id):
            emit_diagnostic(
                "info",
                "api",
                "api.request.started",
                "An API request started.",
                outcome="started",
                metadata={"method": request.method},
            )
            try:
                response = await call_next(request)
            except Exception as exc:
                error_id = diagnostic_error_id(exc)
                failure_feature = diagnostic_error_feature(exc) or request_feature(
                    request
                )
                if error_id is None:
                    error_id = new_error_id()
                    emit_diagnostic(
                        "error",
                        failure_feature,
                        f"{failure_feature}.request.unhandled_exception",
                        "An API request failed because of an unhandled exception.",
                        error_id=error_id,
                        outcome="failure",
                        stage="dispatch",
                        duration_ms=(time.monotonic() - started) * 1000,
                        retryable=False,
                        exception=exc,
                        metadata={"method": request.method, "http_status": 500},
                    )
                content: dict[str, Any] = {
                    "detail": "The operation failed unexpectedly. No verified recovery procedure is available.",
                }
                if diagnostics is not None:
                    content.update(
                        {
                            "code": "api.unhandled_exception",
                            "feature": failure_feature,
                            "request_id": request_id,
                            "error_id": error_id,
                            "retryable": False,
                            "help_article": None,
                        }
                    )
                response = JSONResponse(status_code=500, content=content)
                request.state.diagnostic_error_recorded = True
                request.state.diagnostic_error_id = error_id
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            feature = request_feature(request)
            status_code = response.status_code
            if status_code >= 400:
                emit_diagnostic(
                    "error" if status_code >= 500 else "warning",
                    "api",
                    "api.request.failed"
                    if status_code >= 500
                    else "api.request.rejected",
                    "An API request returned a failure response.",
                    outcome="failure" if status_code >= 500 else "denied",
                    stage="response",
                    duration_ms=(time.monotonic() - started) * 1000,
                    retryable=status_code >= 500,
                    error_id=(
                        getattr(request.state, "diagnostic_error_id", None)
                        if status_code >= 500
                        else None
                    ),
                    metadata={
                        "method": request.method,
                        "route": route_template,
                        "http_status": status_code,
                    },
                )
            else:
                emit_diagnostic(
                    "info",
                    "api",
                    "api.request.completed",
                    "An API request completed.",
                    outcome="success" if status_code < 400 else "failure",
                    stage="response",
                    duration_ms=(time.monotonic() - started) * 1000,
                    metadata={
                        "method": request.method,
                        "route": route_template,
                        "http_status": status_code,
                    },
                )
            if feature not in {"api", "diagnostics"}:
                if status_code < 400:
                    emit_diagnostic(
                        "info",
                        feature,
                        f"{feature}.request.completed",
                        f"A {feature.replace('-', ' ')} operation completed.",
                        outcome="success",
                        stage="response",
                        duration_ms=(time.monotonic() - started) * 1000,
                        metadata={
                            "method": request.method,
                            "route": route_template,
                            "http_status": status_code,
                        },
                    )
                elif not request.state.diagnostic_error_recorded:
                    emit_diagnostic(
                        "error" if status_code >= 500 else "warning",
                        feature,
                        f"{feature}.request.failed"
                        if status_code >= 500
                        else f"{feature}.request.rejected",
                        f"A {feature.replace('-', ' ')} operation could not complete.",
                        outcome="failure" if status_code >= 500 else "denied",
                        stage="response",
                        duration_ms=(time.monotonic() - started) * 1000,
                        retryable=status_code >= 500,
                        metadata={
                            "method": request.method,
                            "route": route_template,
                            "http_status": status_code,
                        },
                    )
            response.headers["X-Request-ID"] = request_id
            return response

    bearer = HTTPBearer(auto_error=False)

    async def require_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> str:
        if allow_unauthenticated:
            return "unauthenticated-local-mode"
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=404, detail=str(exc))

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=422,
            detail=jsonable_encoder(exc.errors()),
            code="api.request_validation",
        )

    @app.exception_handler(ValidationError)
    async def validation_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=422,
            detail=jsonable_encoder(exc.errors(include_url=False)),
            code="api.model_validation",
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=422, detail=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=exc.status_code,
            detail=exc.detail,
            code=f"api.http_{exc.status_code}",
            retryable=exc.status_code >= 500,
            headers=exc.headers,
        )

    @app.exception_handler(ArtifactStoreError)
    async def artifact_error_handler(
        request: Request, exc: ArtifactStoreError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(MissionConfigurationError)
    async def mission_configuration_handler(
        request: Request, exc: MissionConfigurationError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=422, detail=str(exc))

    @app.exception_handler(MissionCapacityError)
    async def mission_capacity_handler(
        request: Request, exc: MissionCapacityError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request, exc, status_code=429, detail=str(exc), retryable=True
        )

    @app.exception_handler(MissionStateError)
    async def mission_state_handler(
        request: Request, exc: MissionStateError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(MissionServiceUnavailable)
    async def mission_unavailable_handler(
        request: Request, exc: MissionServiceUnavailable
    ) -> JSONResponse:
        return diagnostic_error_response(
            request, exc, status_code=503, detail=str(exc), retryable=True
        )

    @app.exception_handler(HarnessConfigurationError)
    async def harness_configuration_handler(
        request: Request, exc: HarnessConfigurationError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=422, detail=str(exc))

    @app.exception_handler(HarnessStateError)
    async def harness_state_handler(
        request: Request, exc: HarnessStateError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(HarnessUnavailableError)
    async def harness_unavailable_handler(
        request: Request, exc: HarnessUnavailableError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request, exc, status_code=503, detail=str(exc), retryable=True
        )

    @app.exception_handler(HarnessError)
    async def harness_error_handler(
        request: Request, exc: HarnessError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request, exc, status_code=502, detail=str(exc), retryable=True
        )

    @app.exception_handler(McpProbeError)
    async def mcp_probe_error_handler(
        request: Request, exc: McpProbeError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request, exc, status_code=502, detail=str(exc), retryable=True
        )

    @app.exception_handler(ToolPlatformError)
    async def tool_platform_error_handler(
        request: Request, exc: ToolPlatformError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(ExecutionServiceError)
    async def execution_error_handler(
        request: Request, exc: ExecutionServiceError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=exc.status_code,
            detail=exc.detail,
            code=exc.code,
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(ContainerTerminalError)
    async def container_terminal_error_handler(
        request: Request, exc: ContainerTerminalError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=exc.status_code,
            detail=exc.detail,
            code=exc.code,
            retryable=exc.status_code >= 500,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.exception_handler(ReportRenderError)
    async def report_render_error_handler(
        request: Request, exc: ReportRenderError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=exc.status_code,
            detail=exc.detail,
            code=exc.code,
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(ExecutionAIError)
    async def execution_ai_error_handler(
        request: Request, exc: ExecutionAIError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=exc.status_code,
            detail=exc.detail,
            code=exc.code,
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(WritingAIError)
    async def writing_ai_error_handler(
        request: Request, exc: WritingAIError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=exc.status_code,
            detail=exc.detail,
            code=exc.code,
            retryable=exc.status_code >= 500,
        )

    @app.exception_handler(ExportError)
    async def export_error_handler(request: Request, exc: ExportError) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(ChatHistoryConflict)
    @app.exception_handler(ChatPrivacyError)
    async def chat_conflict_handler(request: Request, exc: ChatError) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=409, detail=str(exc))

    @app.exception_handler(ChatConfigurationError)
    async def chat_configuration_handler(
        request: Request, exc: ChatConfigurationError
    ) -> JSONResponse:
        return diagnostic_error_response(request, exc, status_code=422, detail=str(exc))

    @app.exception_handler(ChatCompactionError)
    async def chat_compaction_handler(
        request: Request, exc: ChatCompactionError
    ) -> JSONResponse:
        return diagnostic_error_response(
            request,
            exc,
            status_code=503,
            detail=str(exc),
            retryable=True,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(ChatError)
    @app.exception_handler(ProviderError)
    async def chat_provider_handler(request: Request, exc: Exception) -> JSONResponse:
        return diagnostic_error_response(
            request, exc, status_code=502, detail=str(exc), retryable=True
        )

    def require_diagnostic_manager() -> DiagnosticManager:
        if diagnostics is None:
            raise HTTPException(
                status_code=503,
                detail="local diagnostics are not initialized for this embedded Core",
            )
        return diagnostics

    @app.get(
        f"{API_PREFIX}/diagnostics/settings",
        tags=["diagnostics"],
        dependencies=[Depends(require_auth)],
    )
    async def get_diagnostics_settings() -> dict[str, Any]:
        return require_diagnostic_manager().settings.as_dict()

    @app.put(
        f"{API_PREFIX}/diagnostics/settings",
        tags=["diagnostics"],
        dependencies=[Depends(require_auth)],
    )
    async def put_diagnostics_settings(
        request: DiagnosticsSettingsRequest,
    ) -> dict[str, Any]:
        manager = require_diagnostic_manager()
        settings = manager.update_settings(
            request.model_dump(mode="json", by_alias=True)
        )
        return settings.as_dict()

    @app.get(
        f"{API_PREFIX}/diagnostics/files",
        tags=["diagnostics"],
        dependencies=[Depends(require_auth)],
    )
    async def get_diagnostics_files() -> dict[str, Any]:
        manager = require_diagnostic_manager()
        return {"files": manager.list_files(), "health": manager.status()}

    @app.get(
        f"{API_PREFIX}/diagnostics/errors",
        tags=["diagnostics"],
        dependencies=[Depends(require_auth)],
    )
    async def get_diagnostics_errors(
        feature: str | None = Query(default=None, min_length=1, max_length=64),
        after: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        records = require_diagnostic_manager().recent_errors(
            feature=feature, after=after, limit=limit
        )
        return {"errors": records}

    @app.post(
        f"{API_PREFIX}/diagnostics/events",
        tags=["diagnostics"],
        dependencies=[Depends(require_auth)],
    )
    async def post_browser_diagnostics(
        batch: BrowserDiagnosticBatch,
    ) -> dict[str, Any]:
        if not allow_browser_diagnostic_events:
            raise HTTPException(
                status_code=403,
                detail="browser diagnostic ingress is disabled outside development mode",
            )
        manager = require_diagnostic_manager()
        error_ids: list[str] = []
        for event in batch.events:
            error_id = manager.record(
                event.level,
                event.feature,
                event.event_code,
                event.message,
                source="browser",
                error_id=event.error_id,
                request_id=event.request_id,
                operation_id=event.operation_id,
                parent_operation_id=event.parent_operation_id,
                project_id=event.project_id,
                run_id=event.run_id,
                execution_id=event.execution_id,
                session_id=event.session_id,
                outcome=event.outcome,
                stage=event.stage,
                duration_ms=event.duration_ms,
                retryable=event.retryable,
                safe_failure_cause=event.safe_failure_cause,
                exception_type=event.exception_type,
                stack_frames=[frame.model_dump() for frame in event.stack_frames],
                metadata=event.metadata,
            )
            if error_id:
                error_ids.append(error_id)
        return {"accepted": len(batch.events), "error_ids": error_ids}

    @app.post(
        f"{API_PREFIX}/diagnostics/export",
        tags=["diagnostics"],
        dependencies=[Depends(require_auth)],
    )
    async def export_diagnostics() -> FileResponse:
        manager = require_diagnostic_manager()
        export_dir = manager.data_dir / "diagnostics-exports"
        export_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = export_dir / f"nebula-diagnostics-{secrets.token_hex(8)}.zip"
        manager.export(destination)

        def remove_temporary_export() -> None:
            try:
                destination.unlink(missing_ok=True)
            except OSError as exc:
                record_caught_exception(
                    "api",
                    "api.api.caught_failure_007",
                    "A handled api operation raised an exception.",
                    exc,
                    stage="api",
                )
                manager.record(
                    "error",
                    "diagnostics",
                    "diagnostics.export_cleanup_failed",
                    "A temporary diagnostics export could not be removed.",
                    outcome="failure",
                    stage="export-cleanup",
                    retryable=True,
                    exception=exc,
                )

        return FileResponse(
            destination,
            media_type="application/zip",
            filename="nebula-diagnostics.zip",
            background=BackgroundTask(remove_temporary_export),
        )

    @app.get(f"{API_PREFIX}/health", tags=["system"])
    async def health(_: str = Depends(require_auth)) -> dict[str, Any]:
        identity = build_metadata()
        setup_status = await setup.status()
        diagnostic_health = (
            diagnostics.status()
            if diagnostics is not None
            else {
                "writable": False,
                "degraded": True,
            }
        )
        return {
            "status": "degraded" if diagnostic_health["degraded"] else "ok",
            **identity,
            "mode": (
                "local" if store.database.engine.dialect.name == "sqlite" else "team"
            ),
            # A CLI is available only after the same local/rootless validation
            # used by setup. Merely finding docker/podman is never sufficient.
            "runner": (
                "available"
                if setup_status.terminal.status == "ready"
                else setup_status.terminal.status
            ),
            # Compatibility field; the host-backed terminal implementation is gone.
            "human_pty": "unavailable",
            # This is the human-operated Kali container, never the legacy host PTY.
            "container_terminal": (
                "configured"
                if container_terminals is not None
                and (tool_platform is None or setup_status.terminal.status == "ready")
                else "unavailable"
            ),
            "api_version": "v1",
            "diagnostics": diagnostic_health,
            **store.database.health(),
        }

    @app.get(
        f"{API_PREFIX}/harness-catalog",
        tags=["harnesses"],
        dependencies=[Depends(require_auth)],
    )
    async def get_harness_catalog() -> list[Any]:
        return harness_catalog()

    @app.post(
        f"{API_PREFIX}/harnesses/{{profile_id}}/health",
        tags=["harnesses"],
        dependencies=[Depends(require_auth)],
    )
    async def check_harness_health(profile_id: str) -> Any:
        return await harness_runtime.health(profile_id)

    @app.get(
        f"{API_PREFIX}/harness-sessions/{{session_id}}/activity",
        response_model=HarnessSessionActivity,
        tags=["harnesses"],
        dependencies=[Depends(require_auth)],
    )
    async def get_harness_session_activity(
        session_id: str,
    ) -> HarnessSessionActivity:
        return harness_runtime.session_activity(session_id)

    @app.get(
        f"{API_PREFIX}/harness-turns/{{turn_id}}/events",
        response_model=HarnessActivityEventList,
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def replay_harness_turn_events(
        turn_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1_000, ge=1, le=10_000),
    ) -> HarnessActivityEventList:
        return harness_runtime.activity_events(
            turn_id, after_sequence=after, limit=limit
        )

    @app.get(
        f"{API_PREFIX}/harness-turns/{{turn_id}}/interactions",
        response_model=list[HarnessInteraction],
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def list_harness_turn_interactions(
        turn_id: str,
        interaction_status: HarnessInteractionStatus | None = Query(
            default=None, alias="status"
        ),
    ) -> list[HarnessInteraction]:
        turn = store.get(HarnessTurn, turn_id)
        return [
            item
            for item in store.list_entities(
                HarnessInteraction, engagement_id=turn.engagement_id, limit=1_000
            )
            if item.harness_turn_id == turn.id
            and (interaction_status is None or item.status == interaction_status)
        ]

    @app.post(
        f"{API_PREFIX}/harness-interactions/{{interaction_id}}/decision",
        response_model=HarnessInteraction,
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def decide_harness_interaction(
        interaction_id: str,
        request: HarnessInteractionDecisionRequest,
    ) -> HarnessInteraction:
        return await harness_runtime.resolve_interaction(
            interaction_id,
            action=request.action,
            response=request.response,
        )

    @app.post(
        f"{API_PREFIX}/harness-turns/{{turn_id}}/steer",
        response_model=HarnessTurn,
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def steer_harness_turn(
        turn_id: str, request: HarnessSteerRequest
    ) -> HarnessTurn:
        return await harness_runtime.steer_turn(
            turn_id, request.text, actor_id=active_operator_id()
        )

    @app.post(
        f"{API_PREFIX}/harness-turns/{{turn_id}}/stop",
        response_model=HarnessTurn,
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def stop_harness_turn(
        turn_id: str, request: MissionStopRequest
    ) -> HarnessTurn:
        return await harness_runtime.cancel_turn(turn_id, reason=request.reason)

    @app.post(
        f"{API_PREFIX}/harness-turns/{{turn_id}}/retry",
        response_model=HarnessTurn,
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def retry_harness_turn(turn_id: str) -> HarnessTurn:
        return await harness_runtime.retry_turn(turn_id, actor_id=active_operator_id())

    @app.post(
        f"{API_PREFIX}/harness-turns/{{turn_id}}/tasks/{{task_id}}/stop",
        response_model=HarnessTurn,
        tags=["harness-turns"],
        dependencies=[Depends(require_auth)],
    )
    async def stop_harness_subagent(turn_id: str, task_id: str) -> HarnessTurn:
        return await harness_runtime.stop_subagent(turn_id, task_id)

    @app.post(
        f"{API_PREFIX}/harness-sessions/{{session_id}}/checkpoints/rewind",
        response_model=HarnessSession,
        tags=["harness-sessions"],
        dependencies=[Depends(require_auth)],
    )
    async def rewind_harness_files(
        session_id: str, request: HarnessCheckpointRewindRequest
    ) -> HarnessSession:
        return await harness_runtime.rewind_files(session_id, request.checkpoint_id)

    @app.websocket(f"{API_PREFIX}/harness-turns/{{turn_id}}/events/ws")
    async def harness_turn_event_socket(
        websocket: WebSocket,
        turn_id: str,
        after: int = Query(default=0, ge=0),
    ) -> None:
        offered_protocols = [
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        supplied: str | None = None
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        protocol_token = _websocket_protocol_secret(
            offered_protocols, "nebula.auth.", decode_base64=True
        )
        if (
            supplied
            and protocol_token
            and not hmac.compare_digest(supplied, protocol_token)
        ):
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = protocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        try:
            harness_runtime.activity_events(turn_id, after_sequence=after, limit=1)
        except NotFoundError:
            await websocket.close(code=4404, reason="harness turn not found")
            return
        protocol = (
            "nebula.harness-activity.v1"
            if "nebula.harness-activity.v1" in offered_protocols
            else "nebula.events.v1"
            if "nebula.events.v1" in offered_protocols
            else None
        )
        await websocket.accept(subprotocol=protocol)
        try:
            async for event in harness_runtime.follow_turn(
                turn_id, after_sequence=after
            ):
                await websocket.send_json(
                    {"kind": "event", "event": event.model_dump(mode="json")}
                )
            await websocket.send_json({"kind": "complete"})
        except WebSocketDisconnect:
            return

    @app.post(
        f"{API_PREFIX}/harness-sessions/{{session_id}}/close",
        response_model=HarnessSession,
        tags=["harnesses"],
        dependencies=[Depends(require_auth)],
    )
    async def close_harness_session(session_id: str) -> HarnessSession:
        return await harness_runtime.close_session(session_id)

    @app.post(
        f"{API_PREFIX}/mcp-servers/{{profile_id}}/probe",
        response_model=McpProbeReport,
        tags=["mcp"],
        dependencies=[Depends(require_auth)],
    )
    async def probe_mcp_server(
        profile_id: str, request: McpProbeRequest
    ) -> McpProbeReport:
        return await mcp_probes.probe(profile_id, engagement_id=request.engagement_id)

    @app.get(
        f"{API_PREFIX}/setup/status",
        response_model=SetupStatus,
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def setup_status() -> SetupStatus:
        return await setup.status()

    @app.get(
        f"{API_PREFIX}/setup/events",
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def setup_events(
        after_sequence: int = Query(default=0, ge=0),
        follow: bool = Query(default=True),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        cursor = after_sequence
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                record_caught_exception(
                    "api",
                    "api.api.caught_failure_008",
                    "A handled api operation raised an exception.",
                    exc,
                    stage="api",
                )
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                ) from exc
            if cursor < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                )

        async def event_stream() -> Any:
            started_at = time.monotonic()
            event_count = 0
            outcome = "success"
            emit_diagnostic(
                "info",
                "setup",
                "setup.stream.started",
                "The setup event stream started.",
                outcome="started",
                stage="stream",
                metadata={"sequence_start": cursor},
            )
            try:
                async for event in setup.events(cursor, follow=follow):
                    if event is None:
                        yield b": keep-alive\n\n"
                    else:
                        event_count += 1
                        yield _setup_server_sent_event(event)
            except asyncio.CancelledError as exc:
                outcome = "cancelled"
                record_caught_exception(
                    "setup",
                    "setup.stream.cancelled",
                    "The setup event stream disconnected.",
                    exc,
                    stage="stream",
                )
                raise
            except Exception as exc:
                outcome = "failure"
                yield _server_sent_event(
                    "error",
                    stream_error_frame(
                        feature="setup",
                        code="setup_stream_failed",
                        detail="setup event stream failed",
                        exception=exc,
                        retryable=True,
                    ),
                )
            finally:
                emit_diagnostic(
                    "info",
                    "setup",
                    "setup.stream.ended",
                    "The setup event stream ended.",
                    outcome=outcome,
                    stage="stream",
                    duration_ms=(time.monotonic() - started_at) * 1000,
                    metadata={"count": event_count, "sequence_start": cursor},
                )

        return StreamingResponse(
            _correlated_stream(
                event_stream(),
                request_id=current_request_id(),
                operation_id=current_operation_id(),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        f"{API_PREFIX}/setup/runtime/refresh",
        response_model=SetupStatus,
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def refresh_setup_runtime() -> SetupStatus:
        return await setup.refresh()

    async def setup_control(operation: Any) -> SetupControlResponse:
        try:
            return await operation
        except SetupServiceError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_009",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.post(
        f"{API_PREFIX}/setup/runtime/select",
        response_model=SetupControlResponse,
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def select_setup_runtime(
        request: RunnerSelectionRequest,
    ) -> SetupControlResponse:
        return await setup_control(setup.select_runner(request))

    @app.post(
        f"{API_PREFIX}/setup/image/prepare",
        response_model=SetupControlResponse,
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def prepare_setup_image(
        request: ImagePreparationRequest,
    ) -> SetupControlResponse:
        return await setup_control(setup.prepare_image(request))

    @app.post(
        f"{API_PREFIX}/setup/image/retry",
        response_model=SetupControlResponse,
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def retry_setup_image(
        request: ImagePreparationRequest,
    ) -> SetupControlResponse:
        return await setup_control(setup.retry_image_preparation(request))

    @app.post(
        f"{API_PREFIX}/setup/image/cancel",
        response_model=SetupControlResponse,
        tags=["setup"],
        dependencies=[Depends(require_auth)],
    )
    async def cancel_setup_image(
        request: ImagePreparationCancellationRequest,
    ) -> SetupControlResponse:
        return await setup_control(setup.cancel_image_preparation(request))

    def require_execution_service() -> ExecutionService:
        if executions is None:
            raise ExecutionServiceError(
                "runner_unavailable",
                "operator execution is not configured",
                status_code=503,
            )
        return executions

    def require_container_terminal_service() -> ContainerTerminalService:
        if container_terminals is None:
            raise ContainerTerminalError(
                "runner_unavailable",
                "container terminal is not configured",
                status_code=503,
            )
        return container_terminals

    def require_workspace_service() -> WorkspaceService:
        if workspaces is None:
            raise ExecutionServiceError(
                "runner_unavailable",
                "engagement workspace is not configured",
                status_code=503,
            )
        return workspaces

    def require_report_render_service() -> ReportRenderService:
        if report_renders is None:
            raise ReportRenderError(
                "renderer_unavailable",
                "server-rendered PDF export is not configured",
                status_code=503,
            )
        return report_renders

    def require_execution_ai_service() -> ExecutionAIService:
        if execution_ai is None:
            raise ExecutionAIError(
                "ai_unavailable",
                "execution AI actions are not configured",
                status_code=503,
            )
        return execution_ai

    def require_writing_ai_service() -> WritingAIService:
        return writing_ai

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/container-terminal/capabilities",
        response_model=ContainerTerminalCapabilities,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def container_terminal_capabilities(
        engagement_id: str,
    ) -> ContainerTerminalCapabilities:
        return require_container_terminal_service().capabilities(engagement_id)

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/recording-tools",
        response_model=TerminalRecordingTools,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def terminal_recording_tools(
        engagement_id: str,
    ) -> TerminalRecordingTools:
        return terminal_commands.recording_tools(engagement_id)

    @app.put(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/recording-tools",
        response_model=TerminalRecordingTools,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def update_terminal_recording_tools(
        engagement_id: str,
        request: TerminalRecordingToolsUpdate,
    ) -> TerminalRecordingTools:
        try:
            return terminal_commands.update_recording_tools(
                engagement_id,
                request,
                actor_id=active_operator_id(),
            )
        except TerminalRecordingToolsConflict as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_010",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise ContainerTerminalError(exc.code, str(exc)) from exc

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/commands/status",
        response_model=TerminalCommandHistoryStatus,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def terminal_command_history_status(
        engagement_id: str,
    ) -> TerminalCommandHistoryStatus:
        return terminal_commands.status(engagement_id)

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/commands",
        response_model=TerminalCommandPage,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def list_terminal_commands(
        engagement_id: str,
        search: str | None = Query(default=None, max_length=4096),
        operator_id: str | None = Query(default=None, max_length=200),
        session_id: str | None = Query(default=None, max_length=200),
        command_status: TerminalCommandStatus | None = Query(
            default=None, alias="status"
        ),
        exit_code: int | None = Query(default=None),
        date_from: datetime | None = Query(default=None),
        date_to: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1_000),
    ) -> TerminalCommandPage:
        return terminal_commands.list(
            engagement_id,
            search=search,
            operator_id=operator_id,
            session_id=session_id,
            status=command_status,
            exit_code=exit_code,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=limit,
        )

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/commands/{{command_id}}/output",
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def terminal_command_output(
        engagement_id: str,
        command_id: str,
        raw: bool = Query(default=False),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=256 * 1024, ge=1, le=256 * 1024),
        sensitive_acknowledged: str | None = Header(
            default=None, alias="X-Nebula-Sensitive-Data-Acknowledged"
        ),
    ) -> Response:
        if raw and sensitive_acknowledged != "true":
            raise ContainerTerminalError(
                "sensitive_data_acknowledgement_required",
                "raw terminal output may contain unredacted secrets; acknowledge the warning to download it",
                status_code=428,
            )
        data, media_type = terminal_commands.output_bytes(
            engagement_id, command_id, raw=raw
        )
        if offset > len(data):
            raise ContainerTerminalError(
                "output_offset_invalid",
                "output offset is beyond the available terminal result",
                status_code=416,
            )
        page_end = min(len(data), offset + limit)
        if not raw:
            if offset < len(data) and data[offset] & 0xC0 == 0x80:
                raise ContainerTerminalError(
                    "output_offset_invalid",
                    "output offset is not a UTF-8 boundary",
                    status_code=416,
                )
            while page_end < len(data) and data[page_end] & 0xC0 == 0x80:
                page_end -= 1
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Nebula-Output-Total": str(len(data)),
            "X-Nebula-Output-Next": str(page_end),
        }
        if raw:
            headers.update(
                {
                    "Content-Disposition": f'attachment; filename="terminal-command-{command_id}.raw"',
                    "X-Nebula-Sensitive-Data": "unredacted",
                }
            )
        return Response(
            content=data[offset:page_end], media_type=media_type, headers=headers
        )

    @app.put(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/commands/status",
        response_model=TerminalCommandHistoryStatus,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def set_terminal_command_history_status(
        engagement_id: str,
        request: TerminalCommandHistoryPreferenceUpdate,
    ) -> TerminalCommandHistoryStatus:
        try:
            return terminal_commands.set_enabled(engagement_id, enabled=request.enabled)
        except TerminalAuditImmutableError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_011",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise ContainerTerminalError(exc.code, str(exc)) from exc

    @app.delete(
        f"{API_PREFIX}/engagements/{{engagement_id}}/terminal/commands",
        response_model=TerminalCommandHistoryClearResult,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def clear_terminal_commands(
        engagement_id: str,
    ) -> TerminalCommandHistoryClearResult:
        try:
            cleared = terminal_commands.clear(engagement_id)
        except TerminalAuditImmutableError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_012",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise ContainerTerminalError(exc.code, str(exc)) from exc
        return TerminalCommandHistoryClearResult(
            engagement_id=engagement_id, cleared=cleared
        )

    @app.post(
        f"{API_PREFIX}/container-terminal/preflight",
        response_model=ContainerTerminalPreflightResponse,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def preflight_container_terminal(
        request: ContainerTerminalPreflightRequest,
    ) -> ContainerTerminalPreflightResponse:
        return await require_container_terminal_service().preflight(request)

    @app.post(
        f"{API_PREFIX}/container-terminal/sessions",
        response_model=ContainerTerminalStartResponse,
        status_code=201,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def start_container_terminal(
        request: ContainerTerminalStartRequest,
        response: Response,
    ) -> ContainerTerminalStartResponse:
        response.headers["Cache-Control"] = "private, no-store"
        return await require_container_terminal_service().start(request)

    @app.get(
        f"{API_PREFIX}/container-terminal/capacity",
        response_model=ContainerTerminalCapacity,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def container_terminal_capacity(
        response: Response,
    ) -> ContainerTerminalCapacity:
        response.headers["Cache-Control"] = "private, no-store"
        return await require_container_terminal_service().capacity()

    @app.post(
        f"{API_PREFIX}/engagements/{{engagement_id}}/container-terminal/recover",
        response_model=ContainerTerminalRecoveryResponse,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def recover_container_terminal(
        engagement_id: str,
        response: Response,
    ) -> ContainerTerminalRecoveryResponse:
        response.headers["Cache-Control"] = "private, no-store"
        if container_terminals is None:
            store.get(Engagement, engagement_id)
            return ContainerTerminalRecoveryResponse(active=False)
        return await container_terminals.recover(engagement_id)

    @app.post(
        f"{API_PREFIX}/engagements/{{engagement_id}}/container-terminals/recover",
        response_model=ContainerTerminalRecoveryListResponse,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def recover_container_terminals(
        engagement_id: str,
        response: Response,
    ) -> ContainerTerminalRecoveryListResponse:
        response.headers["Cache-Control"] = "private, no-store"
        if container_terminals is None:
            store.get(Engagement, engagement_id)
            return ContainerTerminalRecoveryListResponse()
        return await container_terminals.recover_all(engagement_id)

    @app.delete(
        f"{API_PREFIX}/container-terminals/{{session_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["container-terminal"],
        dependencies=[Depends(require_auth)],
    )
    async def close_container_terminal(
        session_id: str,
        response: Response,
    ) -> None:
        response.headers["Cache-Control"] = "private, no-store"
        if container_terminals is not None:
            await container_terminals.close(session_id)

    @app.websocket(f"{API_PREFIX}/container-terminals/{{session_id}}/ws")
    async def container_terminal_socket(websocket: WebSocket, session_id: str) -> None:
        request_id = new_request_id()
        service = container_terminals
        if service is None:
            error_id = emit_diagnostic(
                "error",
                "terminal",
                "terminal.stream.unavailable",
                "The container terminal stream is unavailable.",
                outcome="failure",
                stage="stream-negotiation",
                retryable=True,
                request_id=request_id,
                session_id=session_id,
            )
            reason = "container terminal unavailable"
            if error_id:
                reason = f"{reason}; reference {error_id}"[:120]
            await websocket.close(code=4503, reason=reason)
            return
        offered_protocols = [
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        terminal_protocol = "nebula.container-terminal.v1"
        if terminal_protocol not in offered_protocols:
            emit_diagnostic(
                "warning",
                "terminal",
                "terminal.stream.protocol_rejected",
                "A terminal stream requested an unsupported protocol.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                session_id=session_id,
                metadata={"reason_code": "protocol-required"},
            )
            await websocket.close(code=4406, reason="terminal protocol required")
            return

        supplied: str | None = None
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        subprotocol_token = _websocket_protocol_secret(
            offered_protocols, "nebula.auth.", decode_base64=True
        )
        if (
            supplied
            and subprotocol_token
            and not hmac.compare_digest(supplied, subprotocol_token)
        ):
            emit_diagnostic(
                "warning",
                "terminal",
                "terminal.stream.authentication_denied",
                "Terminal stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                session_id=session_id,
                metadata={"reason_code": "conflicting-authentication"},
            )
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = subprotocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            emit_diagnostic(
                "warning",
                "terminal",
                "terminal.stream.authentication_denied",
                "Terminal stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                session_id=session_id,
                metadata={"reason_code": "authentication-required"},
            )
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        ticket = _websocket_protocol_secret(
            offered_protocols, "nebula.ticket.", decode_base64=False
        )
        if not ticket:
            emit_diagnostic(
                "warning",
                "terminal",
                "terminal.stream.ticket_rejected",
                "A terminal stream did not provide a valid one-use ticket.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                session_id=session_id,
                metadata={"reason_code": "ticket-required"},
            )
            await websocket.close(code=4401, reason="terminal ticket required")
            return
        raw_after_sequence = websocket.query_params.get("after_sequence", "0")
        if (
            not raw_after_sequence.isascii()
            or not raw_after_sequence.isdecimal()
            or len(raw_after_sequence) > 16
        ):
            emit_diagnostic(
                "warning",
                "terminal",
                "terminal.stream.replay_rejected",
                "A terminal replay cursor was malformed.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                session_id=session_id,
            )
            await websocket.close(code=4400, reason="invalid terminal replay sequence")
            return
        after_sequence = int(raw_after_sequence)
        if after_sequence > 9_007_199_254_740_991:
            emit_diagnostic(
                "warning",
                "terminal",
                "terminal.stream.replay_rejected",
                "A terminal replay cursor exceeded the supported range.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                session_id=session_id,
            )
            await websocket.close(code=4400, reason="invalid terminal replay sequence")
            return
        try:
            attachment = await service.attach(
                session_id,
                ticket,
                after_sequence=after_sequence,
            )
        except ContainerTerminalError as exc:
            frame = stream_error_frame(
                feature="terminal",
                code=exc.code,
                detail=exc.detail,
                exception=exc,
                retryable=exc.status_code >= 500,
                expected=exc.status_code < 500,
                request_id=request_id,
                session_id=session_id,
            )
            if exc.status_code == 404:
                close_code = 4404
            elif exc.code == "terminal_attached":
                close_code = 4409
            elif exc.status_code == 401 or exc.code.startswith("ticket_"):
                close_code = 4401
            elif exc.status_code >= 500:
                close_code = 4503
            else:
                close_code = 4400
            reference = frame.get("error_id")
            reason = exc.detail
            if reference:
                reason = f"{reason}; reference {reference}"
            await websocket.close(code=close_code, reason=reason[:120])
            return

        await websocket.accept(subprotocol=terminal_protocol)
        tasks: list[asyncio.Task[Any]] = []
        started_at = time.monotonic()
        output_count = 0
        input_count = 0
        last_sequence = after_sequence
        emit_diagnostic(
            "info",
            "terminal",
            "terminal.stream.connected",
            "A terminal stream connected.",
            outcome="started",
            stage="stream",
            request_id=request_id,
            session_id=session_id,
            metadata={
                "sequence_start": after_sequence,
                "truncated": attachment.replay_truncated,
            },
        )
        try:
            await websocket.send_json(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "max_duration_seconds": TERMINAL_MAX_DURATION_SECONDS,
                    "idle_timeout_seconds": int(service.idle_timeout_seconds),
                    "reconnect_ticket": attachment.reconnect_ticket,
                    "reconnect_grace_seconds": attachment.reconnect_grace_seconds,
                    "replay_max_bytes": attachment.replay_max_bytes,
                    "oldest_sequence": attachment.oldest_sequence,
                    "latest_sequence": attachment.latest_sequence,
                    "replay_truncated": attachment.replay_truncated,
                }
            )

            async def send_events() -> None:
                nonlocal output_count, last_sequence
                while True:
                    event = await service.next_event(attachment)
                    if isinstance(event, ContainerTerminalOutput):
                        output_count += 1
                        last_sequence = event.sequence
                        await websocket.send_json(
                            {
                                "type": "output",
                                "sequence": event.sequence,
                                "encoding": "base64",
                                "data": base64.b64encode(event.data).decode("ascii"),
                            }
                        )
                        continue
                    if not isinstance(event, ContainerTerminalExit):
                        raise RuntimeError("unsupported terminal broker event")
                    if event.error_code is not None:
                        await websocket.send_json(
                            stream_error_frame(
                                feature="terminal",
                                code=event.error_code,
                                detail=event.detail or "terminal session ended",
                                retryable=False,
                                request_id=request_id,
                                session_id=session_id,
                            )
                        )
                    await websocket.send_json(
                        {
                            "type": "exit",
                            "exit_code": event.exit_code,
                            "outcome": event.outcome,
                        }
                    )
                    return

            async def receive_input() -> str:
                nonlocal input_count
                while True:
                    encoded_message = await websocket.receive_text()
                    input_count += 1
                    if (
                        len(encoded_message.encode("utf-8", errors="replace"))
                        > MAX_TERMINAL_INPUT_BYTES + 16_384
                    ):
                        await websocket.send_json(
                            stream_error_frame(
                                feature="terminal",
                                code="input_limit",
                                detail="terminal frame exceeds the 1 MiB input boundary",
                                expected=True,
                                request_id=request_id,
                                session_id=session_id,
                            )
                        )
                        continue
                    try:
                        message = json.loads(encoded_message)
                    except json.JSONDecodeError as caught_error:
                        await websocket.send_json(
                            stream_error_frame(
                                feature="terminal",
                                code="invalid_frame",
                                detail="terminal frame must be valid JSON",
                                exception=caught_error,
                                expected=True,
                                request_id=request_id,
                                session_id=session_id,
                            )
                        )
                        continue
                    if not isinstance(message, dict):
                        await websocket.send_json(
                            stream_error_frame(
                                feature="terminal",
                                code="invalid_frame",
                                detail="terminal frame must be an object",
                                expected=True,
                                request_id=request_id,
                                session_id=session_id,
                            )
                        )
                        continue
                    frame_type = message.get("type")
                    if frame_type == "input":
                        value = message.get("data")
                        if not isinstance(value, str):
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code="invalid_frame",
                                    detail="terminal input must be text",
                                    expected=True,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            continue
                        try:
                            data = value.encode("utf-8", errors="strict")
                        except UnicodeEncodeError as caught_error:
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code="invalid_frame",
                                    detail="terminal input must be valid UTF-8",
                                    exception=caught_error,
                                    expected=True,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            continue
                        if len(data) > MAX_TERMINAL_INPUT_BYTES:
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code="input_limit",
                                    detail="terminal input frame exceeds 1 MiB",
                                    expected=True,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            continue
                        try:
                            await service.write_input(attachment, data)
                        except ContainerTerminalError as caught_error:
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code=caught_error.code,
                                    detail=caught_error.detail,
                                    exception=caught_error,
                                    retryable=caught_error.status_code >= 500,
                                    expected=caught_error.status_code < 500,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            return "ended"
                    elif frame_type == "resize":
                        columns = message.get("columns")
                        rows = message.get("rows")
                        if (
                            isinstance(columns, bool)
                            or isinstance(rows, bool)
                            or not isinstance(columns, int)
                            or not isinstance(rows, int)
                        ):
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code="invalid_frame",
                                    detail="terminal dimensions must be integers",
                                    expected=True,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            continue
                        try:
                            await service.resize(attachment, columns, rows)
                        except ValueError as exc:
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code="invalid_frame",
                                    detail=str(exc),
                                    exception=exc,
                                    expected=True,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            continue
                        except ContainerTerminalError as caught_error:
                            await websocket.send_json(
                                stream_error_frame(
                                    feature="terminal",
                                    code=caught_error.code,
                                    detail=caught_error.detail,
                                    exception=caught_error,
                                    retryable=caught_error.status_code >= 500,
                                    expected=caught_error.status_code < 500,
                                    request_id=request_id,
                                    session_id=session_id,
                                )
                            )
                            return "ended"
                    elif frame_type == "close":
                        await service.close_attachment(attachment)
                        return "closed"
                    else:
                        await websocket.send_json(
                            stream_error_frame(
                                feature="terminal",
                                code="invalid_frame",
                                detail="unsupported terminal frame type",
                                expected=True,
                                request_id=request_id,
                                session_id=session_id,
                            )
                        )

            # diagnostic-expected: both WebSocket pumps are awaited, their
            # terminal result is inspected, and cleanup is classified below.
            output_task = asyncio.create_task(
                send_events(), name=f"container-terminal-output-{session_id}"
            )
            # diagnostic-expected: paired with output_task in the same wait set.
            input_task = asyncio.create_task(
                receive_input(), name=f"container-terminal-input-{session_id}"
            )
            tasks = [output_task, input_task]
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if output_task in done:
                output_task.result()
            elif input_task in done:
                result = input_task.result()
                if result in {"closed", "ended"}:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(output_task),
                            timeout=1,
                        )
                    except (
                        asyncio.TimeoutError,
                        WebSocketDisconnect,
                        RuntimeError,
                    ) as caught_error:
                        emit_diagnostic(
                            "debug",
                            "terminal",
                            "terminal.stream.output_drain_ended",
                            "Terminal output draining ended during disconnect.",
                            outcome="disconnected",
                            stage="stream-cleanup",
                            exception=caught_error,
                        )
                        return
        except asyncio.CancelledError as caught_error:
            # ASGI servers may cancel the endpoint task as the peer closes the
            # WebSocket. Treat that as a disconnect so attachment cleanup is
            # completed and reconnect grace is established deterministically.
            record_caught_exception(
                "terminal",
                "terminal.stream.cancelled",
                "A terminal stream was cancelled during disconnect.",
                caught_error,
                stage="stream",
            )
            pass
        except WebSocketDisconnect as caught_error:
            record_caught_exception(
                "terminal",
                "terminal.stream.disconnected",
                "A terminal stream disconnected.",
                caught_error,
                stage="stream",
            )
            pass
        except RuntimeError as caught_error:
            # Starlette raises RuntimeError when a peer disappears between
            # receive/send calls; treat it as a disconnect, not a Core failure.
            if str(caught_error) == "unsupported terminal broker event":
                frame = stream_error_frame(
                    feature="terminal",
                    code="terminal_protocol_failure",
                    detail="terminal broker returned an unsupported event",
                    exception=caught_error,
                    retryable=False,
                    request_id=request_id,
                    session_id=session_id,
                )
                try:
                    await websocket.send_json(frame)
                except (RuntimeError, WebSocketDisconnect):
                    # diagnostic-expected: the protocol failure is already recorded.
                    pass
            else:
                emit_diagnostic(
                    "debug",
                    "terminal",
                    "terminal.stream.transport_disconnected",
                    "A terminal stream transport disappeared during I/O.",
                    outcome="disconnected",
                    stage="stream",
                    exception=caught_error,
                )
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await gather_diagnostic(
                    *tasks,
                    feature="terminal",
                    event_code="terminal.stream.cleanup_task_failed",
                    failure_message="A terminal stream pump did not stop cleanly.",
                    stage="stream-cleanup",
                )
            await service.detach(attachment)
            emit_diagnostic(
                "info",
                "terminal",
                "terminal.stream.disconnected",
                "A terminal stream ended.",
                outcome="stopped",
                stage="stream",
                duration_ms=(time.monotonic() - started_at) * 1000,
                request_id=request_id,
                session_id=session_id,
                metadata={
                    "count": output_count,
                    "item_count": input_count,
                    "sequence_start": after_sequence,
                    "sequence_end": last_sequence,
                    "truncated": attachment.replay_truncated,
                },
            )

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/execution-capabilities",
        response_model=ExecutionCapabilities,
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def execution_capabilities(engagement_id: str) -> ExecutionCapabilities:
        return require_execution_service().capabilities(engagement_id)

    @app.post(
        f"{API_PREFIX}/executions/preflight",
        response_model=ExecutionPreflightResponse,
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def preflight_execution(
        request: ExecutionPreflightRequest,
    ) -> ExecutionPreflightResponse:
        return await require_execution_service().preflight(request)

    @app.post(
        f"{API_PREFIX}/executions",
        response_model=OperatorExecution,
        status_code=202,
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def start_execution(request: ExecutionStartRequest) -> OperatorExecution:
        return await require_execution_service().start(request)

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/executions",
        response_model=list[OperatorExecution],
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def list_engagement_executions(
        engagement_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        execution_status: OperatorExecutionStatus | None = Query(
            default=None, alias="status"
        ),
        language: str | None = Query(default=None, max_length=32),
        operator_id: str | None = Query(default=None, max_length=200),
        date_from: datetime | None = Query(default=None),
        date_to: datetime | None = Query(default=None),
        query: str | None = Query(default=None, max_length=500),
    ) -> list[OperatorExecution]:
        store.get(Engagement, engagement_id)
        return require_execution_service().list_executions(
            engagement_id,
            offset=offset,
            limit=limit,
            status=execution_status,
            language=language,
            operator_id=operator_id,
            date_from=date_from,
            date_to=date_to,
            query=query,
        )

    @app.get(
        f"{API_PREFIX}/executions/{{execution_id}}",
        response_model=OperatorExecution,
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def execution_detail(execution_id: str) -> OperatorExecution:
        return store.get(OperatorExecution, execution_id)

    @app.post(
        f"{API_PREFIX}/executions/{{execution_id}}/cancel",
        response_model=OperatorExecution,
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def cancel_execution(execution_id: str) -> OperatorExecution:
        return await require_execution_service().cancel(execution_id)

    @app.get(
        f"{API_PREFIX}/executions/{{execution_id}}/events",
        response_model=OperationEventList,
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def replay_execution_events(
        execution_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=10_000),
    ) -> OperationEventList:
        store.get(OperatorExecution, execution_id)
        events = store.replay_operation_events(
            execution_id, after_sequence=after, limit=limit
        )
        return OperationEventList(
            events=events,
            next_sequence=events[-1].sequence if events else after,
        )

    @app.get(
        f"{API_PREFIX}/executions/{{execution_id}}/output/{{stream}}",
        tags=["executions"],
        dependencies=[Depends(require_auth)],
    )
    async def execution_output(
        execution_id: str,
        stream: str,
        raw: bool = Query(default=False),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=256 * 1024, ge=1, le=256 * 1024),
        sensitive_acknowledged: str | None = Header(
            default=None, alias="X-Nebula-Sensitive-Data-Acknowledged"
        ),
    ) -> Response:
        if raw and sensitive_acknowledged != "true":
            raise ExecutionServiceError(
                "sensitive_data_acknowledgement_required",
                "raw output may contain unredacted secrets; acknowledge the warning to download it",
                status_code=428,
            )
        data, media_type = require_execution_service().output_bytes(
            execution_id, stream, raw=raw
        )
        if offset > len(data):
            raise ExecutionServiceError(
                "output_offset_invalid",
                "output offset is beyond the available stream",
                status_code=416,
            )
        page_end = min(len(data), offset + limit)
        if not raw:
            if offset < len(data) and data[offset] & 0xC0 == 0x80:
                raise ExecutionServiceError(
                    "output_offset_invalid",
                    "output offset is not a UTF-8 boundary",
                    status_code=416,
                )
            while page_end < len(data) and data[page_end] & 0xC0 == 0x80:
                page_end -= 1
            if page_end == offset and offset < len(data):
                page_end = min(len(data), offset + 1)
                while page_end < len(data) and data[page_end] & 0xC0 == 0x80:
                    page_end += 1
        page = data[offset:page_end]
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Nebula-Output-Total": str(len(data)),
            "X-Nebula-Output-Next": str(page_end),
        }
        if raw:
            headers["Content-Disposition"] = (
                f'attachment; filename="execution-{execution_id}-{stream}.raw"'
            )
            headers["X-Nebula-Sensitive-Data"] = "unredacted"
        return Response(content=page, media_type=media_type, headers=headers)

    @app.websocket(f"{API_PREFIX}/executions/{{execution_id}}/events/ws")
    async def execution_event_socket(
        websocket: WebSocket,
        execution_id: str,
        after: int = Query(default=0, ge=0),
    ) -> None:
        request_id = new_request_id()
        supplied: str | None = None
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        offered_protocols = [
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        subprotocol_token: str | None = None
        for protocol in offered_protocols:
            if not protocol.startswith("nebula.auth."):
                continue
            encoded = protocol.removeprefix("nebula.auth.")
            try:
                subprotocol_token = base64.urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as caught_error:
                record_caught_exception(
                    "executions",
                    "executions.stream.authentication_rejected",
                    "An execution stream authentication value was malformed.",
                    caught_error,
                    stage="stream-negotiation",
                )
                subprotocol_token = None
            break
        if (
            supplied
            and subprotocol_token
            and not hmac.compare_digest(supplied, subprotocol_token)
        ):
            emit_diagnostic(
                "warning",
                "executions",
                "executions.stream.authentication_denied",
                "Execution event stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                execution_id=execution_id,
                metadata={"reason_code": "conflicting-authentication"},
            )
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = subprotocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            emit_diagnostic(
                "warning",
                "executions",
                "executions.stream.authentication_denied",
                "Execution event stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                execution_id=execution_id,
                metadata={"reason_code": "authentication-required"},
            )
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        try:
            store.get(OperatorExecution, execution_id)
        except NotFoundError as caught_error:
            record_caught_exception(
                "executions",
                "executions.stream.not_found",
                "The requested execution stream did not exist.",
                caught_error,
                stage="stream-negotiation",
            )
            await websocket.close(code=4404, reason="execution not found")
            return
        event_protocol = (
            "nebula.events.v1" if "nebula.events.v1" in offered_protocols else None
        )
        await websocket.accept(subprotocol=event_protocol)
        started_at = time.monotonic()
        event_count = 0
        cursor = after
        emit_diagnostic(
            "info",
            "executions",
            "executions.stream.connected",
            "An execution event stream connected.",
            outcome="started",
            stage="stream",
            request_id=request_id,
            execution_id=execution_id,
            metadata={"sequence_start": after},
        )
        try:
            while True:
                events = store.replay_operation_events(
                    execution_id, after_sequence=cursor, limit=1000
                )
                if events and events[0].sequence > cursor + 1:
                    emit_diagnostic(
                        "warning",
                        "executions",
                        "executions.stream.sequence_gap",
                        "An execution event sequence gap was detected.",
                        outcome="degraded",
                        stage="replay",
                        request_id=request_id,
                        execution_id=execution_id,
                        metadata={
                            "sequence_start": cursor,
                            "sequence_end": events[0].sequence,
                        },
                    )
                    await websocket.send_json(
                        {
                            "kind": "replay_gap",
                            "after_sequence": cursor,
                            "next_sequence": events[0].sequence,
                        }
                    )
                for event in events:
                    event_count += 1
                    await websocket.send_json(
                        {"kind": "event", "event": event.model_dump(mode="json")}
                    )
                    cursor = event.sequence
                if events:
                    continue
                await websocket.send_json(
                    {"kind": "replay_complete", "after_sequence": cursor}
                )
                break
            idle_ticks = 0
            while True:
                await asyncio.sleep(0.25)
                events = store.replay_operation_events(
                    execution_id, after_sequence=cursor, limit=1000
                )
                if events:
                    if events[0].sequence > cursor + 1:
                        emit_diagnostic(
                            "warning",
                            "executions",
                            "executions.stream.sequence_gap",
                            "An execution event sequence gap was detected.",
                            outcome="degraded",
                            stage="replay",
                            request_id=request_id,
                            execution_id=execution_id,
                            metadata={
                                "sequence_start": cursor,
                                "sequence_end": events[0].sequence,
                            },
                        )
                        await websocket.send_json(
                            {
                                "kind": "replay_gap",
                                "after_sequence": cursor,
                                "next_sequence": events[0].sequence,
                            }
                        )
                    idle_ticks = 0
                    for event in events:
                        event_count += 1
                        await websocket.send_json(
                            {
                                "kind": "event",
                                "event": event.model_dump(mode="json"),
                            }
                        )
                        cursor = event.sequence
                else:
                    idle_ticks += 1
                    if idle_ticks >= 20:
                        await websocket.send_json(
                            {"kind": "heartbeat", "after_sequence": cursor}
                        )
                        idle_ticks = 0
        except WebSocketDisconnect as caught_error:
            record_caught_exception(
                "executions",
                "executions.stream.disconnected",
                "An execution event stream disconnected.",
                caught_error,
                stage="stream",
            )
            return
        except Exception as exc:
            frame = stream_error_frame(
                feature="executions",
                code="execution_stream_failed",
                detail="execution event stream failed",
                exception=exc,
                retryable=True,
                request_id=request_id,
                execution_id=execution_id,
            )
            frame["kind"] = "error"
            try:
                await websocket.send_json(frame)
            except (RuntimeError, WebSocketDisconnect):
                # diagnostic-expected: the stream failure is already recorded.
                pass
        finally:
            emit_diagnostic(
                "info",
                "executions",
                "executions.stream.disconnected",
                "An execution event stream ended.",
                outcome="stopped",
                stage="stream",
                duration_ms=(time.monotonic() - started_at) * 1000,
                request_id=request_id,
                execution_id=execution_id,
                metadata={
                    "count": event_count,
                    "sequence_start": after,
                    "sequence_end": cursor,
                },
            )

    @app.post(
        f"{API_PREFIX}/executions/{{execution_id}}/draft-notes",
        response_model=GeneratedDraft,
        status_code=202,
        tags=["execution-ai"],
        dependencies=[Depends(require_auth)],
    )
    async def generate_execution_draft_note(
        execution_id: str, request: DraftNoteRequest
    ) -> GeneratedDraft:
        return await require_execution_ai_service().generate(execution_id, request)

    @app.patch(
        f"{API_PREFIX}/generated-drafts/{{draft_id}}",
        response_model=GeneratedDraft,
        tags=["execution-ai"],
        dependencies=[Depends(require_auth)],
    )
    async def edit_execution_draft_note(
        draft_id: str, request: DraftEditRequest
    ) -> GeneratedDraft:
        return require_execution_ai_service().edit(draft_id, request)

    @app.post(
        f"{API_PREFIX}/generated-drafts/{{draft_id}}/accept",
        response_model=GeneratedDraft,
        tags=["execution-ai"],
        dependencies=[Depends(require_auth)],
    )
    async def accept_execution_draft_note(
        draft_id: str, request: DraftTransitionRequest
    ) -> GeneratedDraft:
        return require_execution_ai_service().accept(draft_id, request)

    @app.post(
        f"{API_PREFIX}/generated-drafts/{{draft_id}}/reject",
        response_model=GeneratedDraft,
        tags=["execution-ai"],
        dependencies=[Depends(require_auth)],
    )
    async def reject_execution_draft_note(
        draft_id: str, request: DraftTransitionRequest
    ) -> GeneratedDraft:
        return require_execution_ai_service().reject(draft_id, request)

    @app.post(
        f"{API_PREFIX}/executions/{{execution_id}}/chat-attachments",
        response_model=ExecutionChatAttachment,
        status_code=201,
        tags=["execution-ai"],
        dependencies=[Depends(require_auth)],
    )
    async def attach_execution_to_chat(
        execution_id: str, request: ExecutionChatAttachRequest
    ) -> ExecutionChatAttachment:
        return require_execution_ai_service().attach_to_chat(execution_id, request)

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/workspace",
        response_model=WorkspaceListing,
        tags=["workspace"],
        dependencies=[Depends(require_auth)],
    )
    async def list_workspace(
        engagement_id: str,
        path: str = Query(default="", max_length=4096),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> WorkspaceListing:
        return require_workspace_service().list(
            engagement_id, path, offset=offset, limit=limit
        )

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/workspace/preview",
        response_model=WorkspacePreview,
        tags=["workspace"],
        dependencies=[Depends(require_auth)],
    )
    async def preview_workspace_file(
        engagement_id: str,
        path: str = Query(min_length=1, max_length=4096),
    ) -> WorkspacePreview:
        return require_workspace_service().preview(engagement_id, path)

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/workspace/download",
        tags=["workspace"],
        dependencies=[Depends(require_auth)],
    )
    async def download_workspace_file(
        engagement_id: str,
        path: str = Query(min_length=1, max_length=4096),
    ) -> StreamingResponse:
        download = require_workspace_service().download(engagement_id, path)

        async def download_chunks() -> Any:
            started_at = time.monotonic()
            chunk_count = 0
            byte_count = 0
            outcome = "success"
            emit_diagnostic(
                "info",
                "workspace",
                "workspace.download_stream.started",
                "A workspace download stream started.",
                outcome="started",
                stage="stream",
                project_id=engagement_id,
            )
            try:
                for chunk in download.chunks():
                    chunk_count += 1
                    byte_count += len(chunk)
                    yield chunk
            except asyncio.CancelledError as exc:
                outcome = "cancelled"
                record_caught_exception(
                    "workspace",
                    "workspace.download_stream.cancelled",
                    "A workspace download stream disconnected.",
                    exc,
                    stage="stream",
                )
                raise
            except Exception as exc:
                outcome = "failure"
                record_caught_exception(
                    "workspace",
                    "workspace.download_stream.failed",
                    "A workspace download stream failed.",
                    exc,
                    stage="stream",
                )
                raise
            finally:
                emit_diagnostic(
                    "info",
                    "workspace",
                    "workspace.download_stream.ended",
                    "A workspace download stream ended.",
                    outcome=outcome,
                    stage="stream",
                    duration_ms=(time.monotonic() - started_at) * 1000,
                    project_id=engagement_id,
                    metadata={"chunk_count": chunk_count, "byte_count": byte_count},
                )

        return StreamingResponse(
            _correlated_stream(
                download_chunks(),
                request_id=current_request_id(),
                operation_id=current_operation_id(),
            ),
            media_type=download.media_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(download.filename, safe="")
                ),
                "Content-Length": str(download.size),
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.put(
        f"{API_PREFIX}/engagements/{{engagement_id}}/workspace/file",
        response_model=WorkspaceUploadResult,
        status_code=201,
        tags=["workspace"],
        dependencies=[Depends(require_auth)],
    )
    async def upload_workspace_file(
        engagement_id: str,
        request: Request,
        path: str = Query(min_length=1, max_length=4096),
        overwrite: bool = Query(default=False),
    ) -> WorkspaceUploadResult:
        workspace = require_workspace_service()

        async def upload() -> WorkspaceUploadResult:
            return await workspace.upload(
                engagement_id,
                path,
                request.stream(),
                overwrite=overwrite,
            )

        # Uploads use a private file plus an atomic directory-fd rename and are
        # serialized against other API uploads by WorkspaceService. They may
        # safely coexist with a user's persistent terminal; destructive reset
        # remains guarded until the terminal stops.
        return await upload()

    @app.post(
        f"{API_PREFIX}/engagements/{{engagement_id}}/workspace/promote",
        response_model=Evidence,
        status_code=201,
        tags=["workspace"],
        dependencies=[Depends(require_auth)],
    )
    async def promote_workspace_file(
        engagement_id: str, request: WorkspacePromotionRequest
    ) -> Evidence:
        return require_workspace_service().promote(engagement_id, request)

    @app.post(
        f"{API_PREFIX}/engagements/{{engagement_id}}/workspace/reset",
        response_model=WorkspaceResetResult,
        tags=["workspace"],
        dependencies=[Depends(require_auth)],
    )
    async def reset_workspace(
        engagement_id: str, request: WorkspaceResetRequest
    ) -> WorkspaceResetResult:
        workspace = require_workspace_service()
        if container_terminals is None:
            return workspace.reset(engagement_id, request)
        async with container_terminals.guard_workspace_operation(engagement_id):
            return workspace.reset(engagement_id, request)

    @app.post(
        f"{API_PREFIX}/writing/transform",
        response_model=WritingTransformResponse,
        tags=["writing-ai"],
        dependencies=[Depends(require_auth)],
    )
    async def transform_writing(
        request: WritingTransformRequest,
    ) -> WritingTransformResponse:
        return await require_writing_ai_service().transform(request)

    @app.post(
        f"{API_PREFIX}/reports/{{report_id}}/sign-off",
        response_model=Report,
        tags=["reports"],
        dependencies=[Depends(require_auth)],
    )
    async def sign_off_saved_report(
        report_id: str, request: ReportSignoffRequest
    ) -> Report:
        return sign_off_report(store, report_id, request)

    @app.post(
        f"{API_PREFIX}/reports/{{report_id}}/renders",
        response_model=ReportRender,
        status_code=202,
        tags=["reports"],
        dependencies=[Depends(require_auth)],
    )
    async def render_report(
        report_id: str, request: ReportRenderRequest
    ) -> ReportRender:
        return await require_report_render_service().request_render(
            report_id, report_revision=request.report_revision
        )

    @app.get(
        f"{API_PREFIX}/report-renders/{{render_id}}/pdf",
        tags=["reports"],
        dependencies=[Depends(require_auth)],
    )
    async def download_report_pdf(render_id: str) -> FileResponse:
        artifact, path = require_report_render_service().pdf(render_id)
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=artifact.filename or f"report-{render_id}.pdf",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        f"{API_PREFIX}/engagements/{{engagement_id}}/export-bundle",
        tags=["exports"],
        dependencies=[Depends(require_auth)],
    )
    async def export_engagement_bundle(
        engagement_id: str,
        sensitive_acknowledged: str | None = Header(
            default=None, alias="X-Nebula-Sensitive-Data-Acknowledged"
        ),
    ) -> FileResponse:
        if sensitive_acknowledged != "true":
            raise HTTPException(
                status_code=428,
                detail=(
                    "engagement bundles contain unredacted evidence, raw execution "
                    "output, retained selected-tool terminal results, and terminal "
                    "command metadata; "
                    "acknowledge the sensitive-data warning before export"
                ),
            )
        if artifact_store is None:
            raise HTTPException(
                status_code=503,
                detail="engagement bundle export requires an artifact store",
            )
        engagement = store.get(Engagement, engagement_id)
        with tempfile.NamedTemporaryFile(
            prefix="nebula-export-",
            suffix=".nebula.zip",
            dir=artifact_store.root.parent,
            delete=False,
        ) as temporary:
            destination = Path(temporary.name)
        try:
            await asyncio.to_thread(
                export_engagement,
                engagement_id=engagement.id,
                destination=destination,
                store=store,
                artifact_store=artifact_store,
                overwrite=True,
            )
        except Exception as caught_error:
            record_caught_exception(
                "api",
                "api.api.caught_failure_026",
                "A handled api operation raised an exception.",
                caught_error,
                stage="api",
            )
            destination.unlink(missing_ok=True)
            raise
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", engagement.name).strip("-")
        return FileResponse(
            destination,
            media_type="application/zip",
            filename=f"{safe_name or 'engagement'}.nebula.zip",
            background=BackgroundTask(destination.unlink, missing_ok=True),
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Nebula-Sensitive-Data": "unredacted-evidence",
                "X-Nebula-Bundle-Version": "2",
            },
        )

    @app.post(
        f"{API_PREFIX}/approvals/{{approval_id}}/decision",
        response_model=Approval,
        tags=["approvals"],
        dependencies=[Depends(require_auth)],
    )
    async def decide_approval(
        approval_id: str, request: ApprovalDecisionRequest
    ) -> Approval:
        approval = store.get(Approval, approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise ConflictError("approval has already been resolved")
        approval_run = (
            store.get(AgentRun, approval.run_id)
            if approval.origin == ToolCallOrigin.MISSION
            else None
        )
        harness_turn: HarnessTurn | None = None
        if approval.tool_call_id:
            approval_call = store.get(ToolCall, approval.tool_call_id)
            harness_turn_id = approval_call.metadata.get("harness_turn_id")
            if isinstance(harness_turn_id, str):
                harness_turn = store.get(HarnessTurn, harness_turn_id)
        if harness_turn is not None and request.edited_arguments is not None:
            raise HTTPException(
                status_code=422,
                detail="harness approvals apply to the exact request; argument editing is disabled",
            )
        if approval.expires_at is not None and approval.expires_at <= utc_now():
            expired, _ = store.update_with_event(
                Approval,
                approval.id,
                {
                    "status": ApprovalStatus.EXPIRED,
                    "decided_by": "system",
                    "decided_at": utc_now(),
                    "decision_note": "approval expired before an operator decision",
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
            raise HTTPException(status_code=410, detail="approval has expired")
        status_by_decision = {
            "approve": (
                ApprovalStatus.EDITED
                if request.edited_arguments is not None
                else ApprovalStatus.APPROVED
            ),
            "reject": ApprovalStatus.REJECTED,
            "stop": ApprovalStatus.CANCELLED,
        }
        operator_id = active_operator_id()
        changes: dict[str, Any] = {
            "status": status_by_decision[request.decision],
            "decided_by": operator_id,
            "decided_at": utc_now(),
            "decision_note": request.reason,
        }
        if request.edited_arguments is not None:
            exact = dict(approval.exact_request)
            exact["arguments"] = request.edited_arguments
            # The signed declarative binding is rendered again by the broker
            # after schema and scope validation. Never retain an argv preview
            # that describes the pre-edit arguments.
            exact.pop("argv", None)
            changes["exact_request"] = exact
        updated, _ = store.update_with_event(
            Approval,
            approval.id,
            changes,
            expected_revision=approval.revision,
            run_id=approval.run_id,
            event_type="approval.resolved",
            event_payload={
                "approval_id": approval.id,
                "status": changes["status"].value,
                "decided_by": operator_id,
            },
            actor_id=operator_id,
            idempotency_key=f"approval:{approval.id}:resolved",
        )
        if harness_turn is not None:
            await harness_runtime.resolve_approval(updated)
            if request.decision == "stop":
                await harness_runtime.cancel_turn(
                    harness_turn.id,
                    reason=request.reason or "Stopped from an approval decision",
                )
                if harness_turn.run_id:
                    await harness_runtime.stop(
                        harness_turn.run_id,
                        reason=request.reason or "Stopped from an approval decision",
                        actor_id=operator_id,
                    )
            return updated
        if approval.origin == ToolCallOrigin.CHAT:
            if request.decision == "stop":
                chat_service().cancel_turn(approval.run_id)
            return updated
        if (
            approval_run is not None
            and approval_run.status == RunStatus.WAITING_APPROVAL
        ):
            if request.decision == "stop":
                await missions.stop_mission(
                    approval_run.id,
                    reason=request.reason or "Stopped from an approval decision",
                    actor_id=operator_id,
                )
            else:
                await missions.resume_after_approval(updated, actor_id=operator_id)
        return updated

    @app.get(
        f"{API_PREFIX}/overview",
        tags=["overview"],
        dependencies=[Depends(require_auth)],
    )
    async def global_overview() -> dict[str, Any]:
        return store.overview()

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/overview",
        tags=["overview"],
        dependencies=[Depends(require_auth)],
    )
    async def engagement_overview(engagement_id: str) -> dict[str, Any]:
        store.get(Engagement, engagement_id)
        return store.overview(engagement_id)

    @app.get(
        f"{API_PREFIX}/operator-profiles",
        response_model=list[OperatorProfile],
        tags=["operator-profiles"],
        dependencies=[Depends(require_auth)],
    )
    async def list_operator_profiles() -> list[OperatorProfile]:
        return operators.list_profiles()

    @app.get(
        f"{API_PREFIX}/operator-profiles/active",
        response_model=OperatorProfile,
        tags=["operator-profiles"],
        dependencies=[Depends(require_auth)],
    )
    async def active_operator_profile() -> OperatorProfile:
        return operators.active_profile()

    @app.post(
        f"{API_PREFIX}/operator-profiles",
        response_model=OperatorProfile,
        status_code=201,
        tags=["operator-profiles"],
        dependencies=[Depends(require_auth)],
    )
    async def create_operator_profile(
        request: OperatorProfileCreateRequest,
    ) -> OperatorProfile:
        return operators.create_profile(
            display_name=request.display_name,
            email=request.email,
            role=request.role,
            metadata=request.metadata,
        )

    @app.patch(
        f"{API_PREFIX}/operator-profiles/{{profile_id}}",
        response_model=OperatorProfile,
        tags=["operator-profiles"],
        dependencies=[Depends(require_auth)],
    )
    async def update_operator_profile(
        profile_id: str,
        request: OperatorProfileUpdateRequest,
    ) -> OperatorProfile:
        changes: dict[str, Any] = {}
        for field in ("display_name", "email", "role", "metadata"):
            if field in request.model_fields_set:
                changes[field] = getattr(request, field)
        if changes.get("display_name", "present") is None:
            raise ValueError("display_name cannot be null")
        if changes.get("metadata", {}) is None:
            raise ValueError("metadata cannot be null")
        return operators.update_profile(
            profile_id,
            changes,
            expected_revision=request.expected_revision,
        )

    @app.post(
        f"{API_PREFIX}/operator-profiles/{{profile_id}}/activate",
        response_model=OperatorProfile,
        tags=["operator-profiles"],
        dependencies=[Depends(require_auth)],
    )
    async def activate_operator_profile(
        profile_id: str,
        request: OperatorProfileActivateRequest,
    ) -> OperatorProfile:
        return operators.activate_profile(
            profile_id,
            expected_revision=request.expected_revision,
        )

    @app.delete(
        f"{API_PREFIX}/operator-profiles/{{profile_id}}",
        status_code=204,
        tags=["operator-profiles"],
        dependencies=[Depends(require_auth)],
    )
    async def delete_operator_profile(
        profile_id: str,
        if_match: int | None = Header(default=None, alias="If-Match"),
    ) -> Response:
        operators.delete_profile(profile_id, expected_revision=if_match)
        return Response(status_code=204)

    @app.post(
        f"{API_PREFIX}/evidence/upload",
        response_model=Evidence,
        status_code=201,
        tags=["evidence"],
        dependencies=[Depends(require_auth)],
    )
    async def upload_evidence_artifact(request: EvidenceUploadRequest) -> Evidence:
        capture_operation = (
            request.evidence_type == "terminal-screenshot"
            or request.source
            in {
                "terminal-screenshot",
                "terminal-screenshot-edit",
            }
        )
        if artifact_store is None:
            raise HTTPException(
                status_code=503,
                detail="evidence upload requires an artifact store",
            )
        if request.captured_by is not None:
            try:
                operators.get_profile(request.captured_by)
            except NotFoundError as exc:
                record_caught_exception(
                    "api",
                    "api.api.caught_failure_027",
                    "A handled api operation raised an exception.",
                    exc,
                    stage="api",
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "captured_by references a missing operator profile: "
                        f"{request.captured_by}"
                    ),
                ) from exc
        try:
            evidence = await asyncio.to_thread(
                upload_evidence,
                store=store,
                artifact_store=artifact_store,
                request=request,
            )
        except EvidenceTooLargeError as exc:
            if capture_operation:
                record_caught_exception(
                    "capture",
                    "capture.upload.size_rejected",
                    "A screenshot exceeded the protected evidence size limit.",
                    exc,
                    stage="upload-validation",
                )
            record_caught_exception(
                "api",
                "api.api.caught_failure_028",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except InvalidEvidenceUploadError as exc:
            if capture_operation:
                record_caught_exception(
                    "capture",
                    "capture.upload.validation_rejected",
                    "A screenshot failed safe validation.",
                    exc,
                    stage="upload-validation",
                )
            record_caught_exception(
                "api",
                "api.api.caught_failure_029",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except EvidenceReferenceError as exc:
            if capture_operation:
                record_caught_exception(
                    "capture",
                    "capture.lineage.rejected",
                    "Screenshot lineage validation failed safely.",
                    exc,
                    stage="lineage-validation",
                )
            record_caught_exception(
                "api",
                "api.api.caught_failure_030",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            if capture_operation:
                record_caught_exception(
                    "capture",
                    "capture.persistence.failed",
                    "A screenshot could not be preserved.",
                    exc,
                    stage="persistence",
                )
            raise
        if capture_operation:
            emit_diagnostic(
                "info",
                "capture",
                "capture.persistence.completed",
                "A screenshot was preserved with immutable lineage.",
                outcome="success",
                stage="derived-save" if request.parent_artifact_id else "original-save",
                project_id=request.engagement_id,
                metadata={
                    "entity_id": evidence.id,
                    "kind": "derived" if request.parent_artifact_id else "original",
                },
            )
        return evidence

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/scope",
        response_model=ScopePolicy,
        tags=["engagements"],
        dependencies=[Depends(require_auth)],
    )
    async def engagement_scope(engagement_id: str) -> ScopePolicy:
        engagement = store.get(Engagement, engagement_id)
        if not engagement.scope_policy_id:
            return ScopePolicy(id=f"scope:{engagement.id}", engagement_id=engagement.id)
        scope = store.get(ScopePolicy, engagement.scope_policy_id)
        if scope.engagement_id != engagement.id:
            raise ConflictError("engagement scope policy ownership is inconsistent")
        return scope

    @app.put(
        f"{API_PREFIX}/engagements/{{engagement_id}}/scope",
        response_model=ScopePolicy,
        tags=["engagements"],
        dependencies=[Depends(require_auth)],
    )
    async def replace_engagement_scope(
        engagement_id: str, request: ScopePolicyUpdateRequest
    ) -> ScopePolicy:
        engagement = store.get(Engagement, engagement_id)
        operator_id = active_operator_id()
        payload = request.model_dump(exclude={"expected_revision"})
        payload["grants"] = [
            grant.model_copy(update={"granted_by": operator_id})
            for grant in request.grants
        ]
        if engagement.scope_policy_id:
            current = store.get(ScopePolicy, engagement.scope_policy_id)
            if current.engagement_id != engagement.id:
                raise ConflictError("engagement scope policy ownership is inconsistent")
            return store.update(
                ScopePolicy,
                current.id,
                payload,
                expected_revision=request.expected_revision or current.revision,
            )

        scope_id = f"scope:{engagement.id}"
        candidate = ScopePolicy(
            id=scope_id,
            engagement_id=engagement.id,
            **payload,
        )
        try:
            scope = store.create(candidate)
        except ConflictError as caught_error:
            record_caught_exception(
                "api",
                "api.api.caught_failure_031",
                "A handled api operation raised an exception.",
                caught_error,
                stage="api",
            )
            scope = store.get(ScopePolicy, scope_id)
            if scope.engagement_id != engagement.id:
                raise
            scope = store.update(
                ScopePolicy,
                scope.id,
                payload,
                expected_revision=request.expected_revision or scope.revision,
            )
        store.update(
            Engagement,
            engagement.id,
            {"scope_policy_id": scope.id},
            expected_revision=engagement.revision,
        )
        return scope

    @app.get(
        f"{API_PREFIX}/engagements/{{engagement_id}}/tool-assignment",
        response_model=list[EngagementToolAssignment],
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def engagement_tool_assignments(
        engagement_id: str,
    ) -> list[EngagementToolAssignment]:
        store.get(Engagement, engagement_id)
        return [
            assignment
            for assignment in store.list_entities(EngagementToolAssignment, limit=1_000)
            if assignment.engagement_id == engagement_id
        ]

    @app.put(
        f"{API_PREFIX}/engagements/{{engagement_id}}/tool-assignment",
        response_model=EngagementToolAssignment,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def put_engagement_tool_assignment(
        engagement_id: str, request: EngagementToolAssignmentRequest
    ) -> EngagementToolAssignment:
        store.get(Engagement, engagement_id)
        installations = [
            item
            for item in store.list_entities(ToolPackInstallation, limit=1_000)
            if item.manifest_digest == request.manifest_digest
            and item.status == ToolPackInstallationStatus.READY
        ]
        if not installations:
            raise ConflictError(
                "tool assignment requires a verified ready pack installation"
            )
        assigned_tool_names = request.tool_names
        if tool_platform is not None:
            assigned_tool_names = tool_platform.normalize_assignment(
                request.manifest_digest, request.tool_names
            )
        operator_id = active_operator_id()
        existing = next(
            (
                assignment
                for assignment in store.list_entities(
                    EngagementToolAssignment, limit=1_000
                )
                if assignment.engagement_id == engagement_id
                and assignment.manifest_digest == request.manifest_digest
            ),
            None,
        )
        changes = {
            "allowed_tool_names": assigned_tool_names,
            "enabled": request.enabled,
            "assigned_by": operator_id,
        }
        if existing is not None:
            return store.update(
                EngagementToolAssignment,
                existing.id,
                changes,
                expected_revision=request.expected_revision or existing.revision,
            )
        assignment_id = str(
            uuid5(
                NAMESPACE_URL,
                f"nebula:tool-assignment:{engagement_id}:{request.manifest_digest}",
            )
        )
        return store.create(
            EngagementToolAssignment(
                id=assignment_id,
                engagement_id=engagement_id,
                manifest_digest=request.manifest_digest,
                allowed_tool_names=assigned_tool_names,
                enabled=request.enabled,
                assigned_by=operator_id,
            )
        )

    @app.get(
        f"{API_PREFIX}/tool-catalog",
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def tool_catalog() -> list[dict[str, Any]]:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        return await tool_platform.catalog()

    @app.get(
        f"{API_PREFIX}/tool-packs",
        response_model=list[ToolPackInstallation],
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def tool_pack_installations() -> list[ToolPackInstallation]:
        return store.list_entities(ToolPackInstallation, limit=1_000)

    @app.get(
        f"{API_PREFIX}/tools",
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def installed_tools() -> list[dict[str, Any]]:
        if tool_platform is None:
            return []
        return tool_platform.list_tools()

    @app.post(
        f"{API_PREFIX}/tool-packs/install",
        response_model=ToolPackInstallation,
        status_code=201,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def install_catalog_tool_pack(
        request: ToolPackInstallRequest,
    ) -> ToolPackInstallation:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        return await tool_platform.install_catalog(
            request.catalog_id,
            runtime_profile_id=request.runtime_profile_id,
            version=request.version,
        )

    @app.post(
        f"{API_PREFIX}/tool-collections/install",
        response_model=list[ToolPackInstallation],
        status_code=201,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def install_catalog_tool_collection(
        request: ToolCollectionInstallRequest,
    ) -> list[ToolPackInstallation]:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        return await tool_platform.install_collection(
            request.collection_id,
            runtime_profile_id=request.runtime_profile_id,
        )

    @app.post(
        f"{API_PREFIX}/tool-packs/generate",
        response_model=CustomToolBundleResponse,
        status_code=201,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def generate_custom_tool_pack(
        request: CustomToolDefinition,
    ) -> CustomToolBundleResponse:
        """Generate, validate, and archive a parser-free unsigned local pack."""

        def generate() -> tuple[bytes, str]:
            with tempfile.TemporaryDirectory(prefix="nebula-custom-tool-") as root:
                source = Path(root) / "source"
                destination = Path(root) / f"{request.pack_name}.nebula-toolpack"
                generate_custom_tool_project(source, request)
                pack_tool_pack(source, destination)
                return destination.read_bytes(), manifest_digest(
                    custom_tool_manifest(request)
                )

        bundle, digest = await asyncio.to_thread(generate)
        return CustomToolBundleResponse(
            filename=f"{request.pack_name}.nebula-toolpack",
            bundle_base64=base64.b64encode(bundle).decode("ascii"),
            manifest_digest=digest,
            permission_preview=request.permission_preview(),
        )

    @app.post(
        f"{API_PREFIX}/tool-packs/install-local",
        response_model=ToolPackInstallation,
        status_code=201,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def install_local_tool_pack(
        request: LocalToolPackInstallRequest,
    ) -> ToolPackInstallation:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        try:
            bundle = base64.b64decode(request.bundle_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_032",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(
                status_code=422, detail="tool-pack bundle is not valid base64"
            ) from exc
        return await tool_platform.install_local(
            bundle,
            runtime_profile_id=request.runtime_profile_id,
            confirm_permissions=request.developer_mode_confirmed,
            assigned_by=active_operator_id(),
        )

    @app.post(
        f"{API_PREFIX}/tool-packs/{{installation_id}}/verify",
        response_model=ToolPackInstallation,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def verify_tool_pack(installation_id: str) -> ToolPackInstallation:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        return await tool_platform.verify(installation_id)

    @app.post(
        f"{API_PREFIX}/tool-packs/{{installation_id}}/update",
        response_model=ToolPackInstallation,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def update_tool_pack(installation_id: str) -> ToolPackInstallation:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        return await tool_platform.update(installation_id)

    @app.delete(
        f"{API_PREFIX}/tool-packs/{{installation_id}}",
        status_code=204,
        tags=["tool-packs"],
        dependencies=[Depends(require_auth)],
    )
    async def disable_tool_pack(installation_id: str) -> Response:
        if tool_platform is None:
            raise HTTPException(
                status_code=501, detail="tool-pack platform is not configured"
            )
        tool_platform.disable(installation_id)
        return Response(status_code=204)

    @app.websocket(f"{API_PREFIX}/tool-packs/events/ws")
    async def tool_pack_event_socket(
        websocket: WebSocket,
        after_sequence: int = Query(default=0, ge=0),
    ) -> None:
        request_id = new_request_id()
        supplied: str | None = None
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        offered_protocols = [
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        subprotocol_token: str | None = None
        for protocol in offered_protocols:
            if not protocol.startswith("nebula.auth."):
                continue
            encoded = protocol.removeprefix("nebula.auth.")
            try:
                padding = "=" * (-len(encoded) % 4)
                subprotocol_token = base64.urlsafe_b64decode(encoded + padding).decode(
                    "utf-8"
                )
            except (ValueError, UnicodeDecodeError) as caught_error:
                record_caught_exception(
                    "toolbox",
                    "toolbox.stream.authentication_rejected",
                    "A Toolbox stream authentication value was malformed.",
                    caught_error,
                    stage="stream-negotiation",
                )
                subprotocol_token = None
            break
        if (
            supplied
            and subprotocol_token
            and not hmac.compare_digest(supplied, subprotocol_token)
        ):
            emit_diagnostic(
                "warning",
                "toolbox",
                "toolbox.stream.authentication_denied",
                "Toolbox event stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                metadata={"reason_code": "conflicting-authentication"},
            )
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = subprotocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            emit_diagnostic(
                "warning",
                "toolbox",
                "toolbox.stream.authentication_denied",
                "Toolbox event stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                metadata={"reason_code": "authentication-required"},
            )
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        if tool_platform is None:
            emit_diagnostic(
                "error",
                "toolbox",
                "toolbox.stream.unavailable",
                "The Toolbox event stream is unavailable.",
                outcome="failure",
                stage="stream-negotiation",
                retryable=True,
                request_id=request_id,
            )
            await websocket.close(
                code=4501, reason="tool-pack platform is not configured"
            )
            return
        event_protocol = (
            "nebula.tool-packs.v1"
            if "nebula.tool-packs.v1" in offered_protocols
            else None
        )
        await websocket.accept(subprotocol=event_protocol)
        started_at = time.monotonic()
        event_count = 0
        gap_count = 0
        cursor = after_sequence
        emit_diagnostic(
            "info",
            "toolbox",
            "toolbox.stream.connected",
            "A Toolbox event stream connected.",
            outcome="started",
            stage="stream",
            request_id=request_id,
            metadata={"sequence_start": after_sequence},
        )
        try:
            replay = tool_platform.events.replay(cursor)
            for event in replay.events:
                event_count += 1
                await websocket.send_json(
                    {"kind": "event", "event": event.model_dump(mode="json")}
                )
                cursor = event.sequence
            await websocket.send_json(
                {
                    "kind": "replay_complete",
                    "after_sequence": cursor,
                    "oldest_sequence": replay.oldest_sequence,
                    "latest_sequence": replay.latest_sequence,
                    "truncated": replay.truncated,
                }
            )

            idle_ticks = 0
            while True:
                await asyncio.sleep(TOOL_PACK_EVENT_POLL_SECONDS)
                replay = tool_platform.events.replay(cursor)
                if replay.events:
                    idle_ticks = 0
                    if replay.truncated:
                        gap_count += 1
                        emit_diagnostic(
                            "warning",
                            "toolbox",
                            "toolbox.stream.sequence_gap",
                            "A Toolbox stream replay gap was detected.",
                            outcome="degraded",
                            stage="replay",
                            request_id=request_id,
                            retryable=False,
                            metadata={
                                "sequence_start": cursor,
                                "sequence_end": replay.oldest_sequence,
                            },
                        )
                        await websocket.send_json(
                            {
                                "kind": "replay_gap",
                                "after_sequence": cursor,
                                "oldest_sequence": replay.oldest_sequence,
                                "latest_sequence": replay.latest_sequence,
                            }
                        )
                    for event in replay.events:
                        event_count += 1
                        await websocket.send_json(
                            {
                                "kind": "event",
                                "event": event.model_dump(mode="json"),
                            }
                        )
                        cursor = event.sequence
                else:
                    idle_ticks += 1
                    if idle_ticks >= TOOL_PACK_EVENT_HEARTBEAT_TICKS:
                        await websocket.send_json(
                            {
                                "kind": "heartbeat",
                                "after_sequence": cursor,
                                "oldest_sequence": replay.oldest_sequence,
                                "latest_sequence": replay.latest_sequence,
                            }
                        )
                        idle_ticks = 0
        except WebSocketDisconnect as caught_error:
            record_caught_exception(
                "toolbox",
                "toolbox.stream.disconnected",
                "A Toolbox event stream disconnected.",
                caught_error,
                stage="stream",
            )
            return
        except Exception as exc:
            frame = stream_error_frame(
                feature="toolbox",
                code="toolbox_stream_failed",
                detail="Toolbox event stream failed",
                exception=exc,
                retryable=True,
                request_id=request_id,
            )
            frame["kind"] = "error"
            try:
                await websocket.send_json(frame)
            except (RuntimeError, WebSocketDisconnect):
                # diagnostic-expected: the stream failure is already recorded.
                pass
        finally:
            emit_diagnostic(
                "info",
                "toolbox",
                "toolbox.stream.disconnected",
                "A Toolbox event stream ended.",
                outcome="stopped",
                stage="stream",
                duration_ms=(time.monotonic() - started_at) * 1000,
                request_id=request_id,
                metadata={
                    "count": event_count,
                    "warning_count": gap_count,
                    "sequence_start": after_sequence,
                    "sequence_end": cursor,
                },
            )

    @app.get(
        f"{API_PREFIX}/runner-profiles",
        response_model=list[RunnerProfile],
        tags=["runners"],
        dependencies=[Depends(require_auth)],
    )
    async def runner_profiles() -> list[RunnerProfile]:
        return store.list_entities(RunnerProfile, limit=1_000)

    @app.put(
        f"{API_PREFIX}/runner-profiles/{{profile_id}}",
        response_model=RunnerProfile,
        tags=["runners"],
        dependencies=[Depends(require_auth)],
    )
    async def put_runner_profile(
        profile_id: str, request: RunnerProfileRequest
    ) -> RunnerProfile:
        payload = request.model_dump(exclude={"expected_revision"})
        try:
            existing = store.get(RunnerProfile, profile_id)
        except NotFoundError as caught_error:
            record_caught_exception(
                "api",
                "api.api.caught_failure_035",
                "A handled api operation raised an exception.",
                caught_error,
                stage="api",
            )
            profile = store.create(RunnerProfile(id=profile_id, **payload))
        else:
            profile = store.update(
                RunnerProfile,
                existing.id,
                payload,
                expected_revision=request.expected_revision or existing.revision,
            )
        if tool_platform is not None:
            return await tool_platform.verify_runner(profile.id)
        return profile

    @app.post(
        f"{API_PREFIX}/missions",
        response_model=AgentRun,
        status_code=202,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def start_mission(request: MissionStartRequest) -> AgentRun:
        if (
            request.backend == RunBackend.NATIVE
            and request.tool_names
            and not executable_missions_enabled
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "executable missions remain release-gated until the complete "
                    "runner-isolation acceptance flow passes"
                ),
            )
        operator_id = active_operator_id()
        budget = RunBudget(
            max_concurrency=request.max_concurrency,
            max_delegation_depth=(
                1 if request.tool_names or request.mcp_server_ids else 0
            ),
            max_duration_seconds=request.max_duration_seconds,
            max_tokens=request.max_tokens,
            max_cost_usd=request.max_cost_usd,
            max_tool_calls=request.max_tool_calls,
            max_artifact_queries=request.max_artifact_queries,
            max_retries=request.max_retries,
            per_target_active_operations=1,
        )
        if request.backend == RunBackend.HARNESS:
            return await harness_runtime.start_mission(
                engagement_id=request.engagement_id,
                objective=request.objective,
                profile_id=request.harness_profile_id or "",
                model=request.model,
                budget=budget,
                harness_session_id=request.harness_session_id,
                mcp_server_ids=request.mcp_server_ids,
                actor_id=operator_id,
                allow_remote_mcp=request.allow_cloud_tool_results,
            )
        return await missions.start_mission(
            engagement_id=request.engagement_id,
            objective=request.objective,
            provider_id=request.provider_id or "",
            model=request.model or "",
            budget=budget,
            tool_names=request.tool_names,
            mcp_server_ids=request.mcp_server_ids,
            allow_cloud_tool_results=request.allow_cloud_tool_results,
            actor_id=operator_id,
        )

    @app.post(
        f"{API_PREFIX}/runs/{{run_id}}/stop",
        response_model=AgentRun,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def stop_mission(run_id: str, request: MissionStopRequest) -> AgentRun:
        operator_id = active_operator_id()
        run = store.get(AgentRun, run_id)
        if run.backend == RunBackend.HARNESS:
            return await harness_runtime.stop(
                run_id,
                reason=request.reason,
                actor_id=operator_id,
            )
        return await missions.stop_mission(
            run_id,
            reason=request.reason,
            actor_id=operator_id,
        )

    @app.post(
        f"{API_PREFIX}/runs/{{run_id}}/steer",
        response_model=HarnessTurn,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def steer_harness_run(
        run_id: str, request: HarnessSteerRequest
    ) -> HarnessTurn:
        return await harness_runtime.steer(
            run_id, request.text, actor_id=active_operator_id()
        )

    @app.post(
        f"{API_PREFIX}/runs/{{run_id}}/discuss",
        response_model=ChatSession,
        tags=["runs", "chat"],
        dependencies=[Depends(require_auth)],
    )
    async def discuss_harness_run(run_id: str) -> ChatSession:
        return harness_runtime.attach_run_to_chat(run_id)

    @app.post(
        f"{API_PREFIX}/chat/sessions/{{session_id}}/continue-as-mission",
        response_model=AgentRun,
        status_code=202,
        tags=["runs", "chat"],
        dependencies=[Depends(require_auth)],
    )
    async def continue_chat_as_mission(
        session_id: str, request: HarnessMissionHandoffRequest
    ) -> AgentRun:
        chat = store.get(ChatSession, session_id)
        if chat.backend != ChatBackend.HARNESS or not chat.harness_session_id:
            raise HarnessStateError("only harness chats can continue as a mission")
        messages = chat_service().session_messages(session_id)
        objective = request.objective or next(
            (
                message.content
                for message in reversed(messages)
                if message.role == ChatRole.USER
            ),
            "Continue the current analysis as a mission",
        )
        return await harness_runtime.start_mission(
            engagement_id=chat.engagement_id,
            objective=objective,
            profile_id=chat.harness_profile_id or "",
            model=chat.model,
            budget=RunBudget(
                max_concurrency=1,
                max_delegation_depth=0,
                max_duration_seconds=request.max_duration_seconds,
                max_tokens=request.max_tokens,
                max_cost_usd=request.max_cost_usd,
                max_tool_calls=request.max_tool_calls,
                max_artifact_queries=request.max_artifact_queries,
                max_retries=0,
                per_target_active_operations=1,
            ),
            harness_session_id=chat.harness_session_id,
            actor_id=active_operator_id(),
            allow_remote_mcp=request.allow_cloud_tool_results,
        )

    @app.post(
        f"{API_PREFIX}/knowledge/ingest",
        response_model=KnowledgeSource,
        status_code=201,
        tags=["knowledge"],
        dependencies=[Depends(require_auth)],
    )
    async def ingest_knowledge(request: KnowledgeIngestRequest) -> KnowledgeSource:
        if artifact_store is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge ingestion requires an artifact store",
            )
        try:
            content = base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_036",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(
                status_code=422,
                detail="content_base64 must be valid base64",
            ) from exc
        if len(content) > MAX_DOCUMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"document exceeds the "
                    f"{MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB limit"
                ),
            )
        try:
            created = await asyncio.to_thread(
                ingest_document,
                store=store,
                artifact_store=artifact_store,
                engagement_id=request.engagement_id,
                filename=request.filename,
                data=content,
                media_type=request.media_type,
            )
            return knowledge_summary(created)
        except DocumentTooLargeError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_037",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UnsupportedDocumentError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_038",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except InvalidDocumentError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_039",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        f"{API_PREFIX}/knowledge/{{knowledge_id}}/reindex",
        response_model=KnowledgeSource,
        tags=["knowledge"],
        dependencies=[Depends(require_auth)],
    )
    async def reindex_knowledge(knowledge_id: str) -> KnowledgeSource:
        if artifact_store is None:
            raise HTTPException(
                status_code=503,
                detail="knowledge reindexing requires an artifact store",
            )
        try:
            updated = await asyncio.to_thread(
                reindex_document,
                store=store,
                artifact_store=artifact_store,
                source_id=knowledge_id,
            )
            return knowledge_summary(updated)
        except DocumentTooLargeError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_040",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UnsupportedDocumentError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_041",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except InvalidDocumentError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_042",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete(
        f"{API_PREFIX}/knowledge/{{knowledge_id}}",
        status_code=204,
        tags=["knowledge"],
        dependencies=[Depends(require_auth)],
    )
    async def delete_knowledge(knowledge_id: str) -> Response:
        """Remove a retrieval source while retaining its immutable artifact."""

        store.delete(KnowledgeSource, knowledge_id)
        return Response(status_code=204)

    @app.get(
        f"{API_PREFIX}/admin/schema",
        tags=["administration"],
        dependencies=[Depends(require_auth)],
    )
    async def schema_information() -> dict[str, Any]:
        return {
            "schema_version": store.database.current_schema_version(),
            "dialect": store.database.engine.dialect.name,
            "resources": sorted(ENTITY_MODEL_BY_KIND),
        }

    @app.get(
        f"{API_PREFIX}/provider-catalog",
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def provider_catalog() -> list[dict[str, Any]]:
        return [
            entry.model_dump(mode="json")
            for entry in sorted(
                PROVIDER_CATALOG.values(), key=lambda item: item.display_name
            )
        ]

    @app.get(
        f"{API_PREFIX}/providers/discover-local",
        response_model=list[LocalProviderDetection],
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def discover_local_provider_services() -> list[LocalProviderDetection]:
        """Probe fixed loopback model endpoints without generating content."""

        return await _discover_local_provider_services(provider_factory)

    @app.post(
        f"{API_PREFIX}/credentials",
        response_model=CredentialStatus,
        status_code=201,
        tags=["credentials"],
        dependencies=[Depends(require_auth)],
    )
    async def create_provider_credential(
        request: CredentialCreateRequest,
    ) -> CredentialStatus:
        try:
            return credentials.create(request)
        except CredentialUnavailableError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_043",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get(
        f"{API_PREFIX}/credentials/{{reference}}/status",
        response_model=CredentialStatus,
        tags=["credentials"],
        dependencies=[Depends(require_auth)],
    )
    async def provider_credential_status(reference: str) -> CredentialStatus:
        try:
            return credentials.status(reference)
        except ValueError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_044",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete(
        f"{API_PREFIX}/credentials/{{reference}}",
        status_code=204,
        tags=["credentials"],
        dependencies=[Depends(require_auth)],
    )
    async def delete_provider_credential(reference: str) -> Response:
        try:
            credentials.delete(reference)
        except CredentialError as exc:
            record_caught_exception(
                "api",
                "api.api.caught_failure_045",
                "A handled api operation raised an exception.",
                exc,
                stage="api",
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.post(
        f"{API_PREFIX}/providers/{{provider_id}}/health",
        response_model=ProviderHealth,
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def refresh_provider_health(provider_id: str) -> ProviderHealth:
        profile = store.get(ProviderProfile, provider_id)
        return await _provider_health(profile, provider_factory)

    @app.post(
        f"{API_PREFIX}/providers/{{provider_id}}/capabilities/verify",
        response_model=ProviderCapabilityVerifyResponse,
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def verify_provider_capabilities(
        provider_id: str,
        request: ProviderCapabilityVerifyRequest,
    ) -> ProviderCapabilityVerifyResponse:
        profile = store.get(ProviderProfile, provider_id)
        if profile.revision != request.expected_revision:
            raise ConflictError(
                f"revision conflict: expected {request.expected_revision}, "
                f"found {profile.revision}"
            )
        return await _verify_provider_capability(
            store, profile, request.model, provider_factory
        )

    @app.post(
        f"{API_PREFIX}/provider-health/refresh",
        response_model=list[ProviderHealth],
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def refresh_all_provider_health() -> list[ProviderHealth]:
        profiles: list[ProviderProfile] = []
        offset = 0
        while True:
            page = store.list_entities(
                ProviderProfile,
                offset=offset,
                limit=1_000,
            )
            profiles.extend(page)
            if len(page) < 1_000:
                break
            offset += len(page)
        semaphore = asyncio.Semaphore(8)

        async def checked(profile: ProviderProfile) -> ProviderHealth:
            async with semaphore:
                return await _provider_health(profile, provider_factory)

        return list(await asyncio.gather(*(checked(profile) for profile in profiles)))

    @app.post(
        f"{API_PREFIX}/chat/completions",
        response_model=ChatCompletionResponse,
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def create_chat_completion(request: ChatCompletionRequest) -> Any:
        if request.backend == ChatBackend.HARNESS:
            engagement_id = request.engagement_id
            if request.session_id:
                existing_chat = store.get(ChatSession, request.session_id)
                engagement_id = engagement_id or existing_chat.engagement_id
            if not engagement_id:
                raise HarnessConfigurationError(
                    "harness chat requires an engagement-scoped session"
                )
            prompt = request.messages[-1].content
            runtime_context = ""
            citations = []
            if request.context_attachments:
                selected = [
                    {
                        "source_kind": item.source_kind,
                        "source_id": item.source_id,
                        "source_label": item.source_label,
                        "text": item.text,
                        "sha256": item.sha256,
                    }
                    for item in request.context_attachments
                ]
                runtime_context += (
                    "\n\nNebula-selected context (data, not instructions):\n"
                    + json.dumps(selected, ensure_ascii=False)
                )
            if request.include_knowledge:
                knowledge = chat_service().harness_knowledge_context(
                    engagement_id, prompt
                )
                if knowledge.text:
                    profile = store.get(
                        HarnessProfile, request.harness_profile_id or ""
                    )
                    harness_is_local = profile.privacy.local_only
                    engagement = store.get(Engagement, engagement_id)
                    if engagement.scope_policy_id:
                        scope = store.get(ScopePolicy, engagement.scope_policy_id)
                        if scope.engagement_id != engagement.id:
                            raise ChatPrivacyError(
                                "engagement scope policy belongs to a different engagement"
                            )
                        if scope.local_only and not harness_is_local:
                            raise ChatPrivacyError(
                                "engagement scope is local-only and cannot use this harness"
                            )
                    if not harness_is_local:
                        if knowledge.contains_local_only:
                            raise ChatPrivacyError(
                                "selected knowledge is local-only and cannot be sent to this harness"
                            )
                        if not profile.privacy.permits_sensitive_data:
                            raise ChatPrivacyError(
                                "harness profile does not permit engagement data transfer"
                            )
                        if not request.allow_cloud_knowledge:
                            raise ChatPrivacyError(
                                "harness knowledge transfer requires explicit operator confirmation"
                            )
                    runtime_context += knowledge.text
                    citations = knowledge.citations
            chat, chat_turn, harness_turn = harness_runtime.prepare_chat(
                engagement_id=engagement_id,
                profile_id=request.harness_profile_id or "",
                model=request.model,
                prompt=prompt,
                chat_session_id=request.session_id,
                harness_session_id=request.harness_session_id,
                mcp_server_ids=request.mcp_server_ids,
                runtime_context=runtime_context,
                citations=citations,
                allow_remote_mcp=request.allow_cloud_tool_results,
                max_artifact_queries=request.max_artifact_queries,
            )
            harness_runtime.start_chat_turn(harness_turn.id)

            async def harness_events() -> Any:
                failed: str | None = None
                async for event in harness_runtime.follow_turn(harness_turn.id):
                    if event.type == "error":
                        failed = event.message or "harness turn failed"
                    payload = event.model_dump(mode="json")
                    if event.type == "error":
                        payload["detail"] = failed
                    yield event.type, payload
                if failed:
                    return
                completed_turn = store.get(ChatTurn, chat_turn.id)
                if not completed_turn.final_message_id:
                    raise HarnessError(
                        "harness turn completed without a durable message"
                    )
                message = store.get(ChatMessage, completed_turn.final_message_id)
                response = ChatCompletionResponse(
                    turn_id=completed_turn.id,
                    session_id=chat.id,
                    backend=ChatBackend.HARNESS,
                    harness_profile_id=chat.harness_profile_id,
                    harness_session_id=chat.harness_session_id,
                    harness_turn_id=harness_turn.id,
                    model=chat.model,
                    message=ChatResponseMessage(
                        id=message.id,
                        role=ChatRole.ASSISTANT,
                        content=message.content,
                    ),
                    usage=completed_turn.usage,
                    finish_reason="stop",
                    citations=message.citations,
                )
                yield "done", {"type": "done", **response.model_dump(mode="json")}

            if not request.stream:
                completion: ChatCompletionResponse | None = None
                failure: str | None = None
                async for event_name, payload in harness_events():
                    if event_name == "error":
                        failure = str(payload.get("message") or "harness turn failed")
                    if event_name == "done":
                        body = dict(payload)
                        body.pop("type", None)
                        completion = ChatCompletionResponse.model_validate(body)
                if failure:
                    raise HarnessError(failure)
                if completion is None:
                    raise HarnessError("harness response ended before completion")
                return completion

            async def harness_event_stream() -> Any:
                started_at = time.monotonic()
                event_count = 0
                outcome = "success"
                emit_diagnostic(
                    "info",
                    "harnesses",
                    "harnesses.chat_stream.started",
                    "A harness chat stream started.",
                    outcome="started",
                    stage="stream",
                    run_id=harness_turn.run_id,
                )
                try:
                    async for event_name, payload in harness_events():
                        event_count += 1
                        yield _server_sent_event(event_name, payload)
                except asyncio.CancelledError as caught_error:
                    outcome = "detached"
                    record_caught_exception(
                        "harnesses",
                        "harnesses.chat_stream.cancelled",
                        "A harness chat stream disconnected.",
                        caught_error,
                        stage="stream",
                    )
                    raise
                except (HarnessError, ConflictError) as exc:
                    outcome = "failure"
                    yield _server_sent_event(
                        "error",
                        stream_error_frame(
                            feature="harnesses",
                            code="harness_stream_failed",
                            detail=str(exc),
                            exception=exc,
                            retryable=not isinstance(exc, ConflictError),
                            expected=isinstance(exc, ConflictError),
                            run_id=harness_turn.run_id,
                        ),
                    )
                finally:
                    emit_diagnostic(
                        "info",
                        "harnesses",
                        "harnesses.chat_stream.ended",
                        "A harness chat stream ended.",
                        outcome=outcome,
                        stage="stream",
                        duration_ms=(time.monotonic() - started_at) * 1000,
                        run_id=harness_turn.run_id,
                        metadata={"count": event_count},
                    )

            return StreamingResponse(
                _correlated_stream(
                    harness_event_stream(),
                    request_id=current_request_id(),
                    operation_id=current_operation_id(),
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        service = chat_service()
        prepared = await service.prepare_async(request)
        if not request.stream:
            return await service.complete(prepared)

        async def event_stream() -> Any:
            started_at = time.monotonic()
            event_count = 0
            outcome = "success"
            chat_session = prepared.session or prepared.pending_session
            emit_diagnostic(
                "info",
                "chat",
                "chat.stream.started",
                "A chat response stream started.",
                outcome="started",
                stage="stream",
                session_id=chat_session.id if chat_session else None,
            )
            try:
                async for event, payload in service.stream(prepared):
                    event_count += 1
                    yield _server_sent_event(event, payload)
            except asyncio.CancelledError as caught_error:
                outcome = "cancelled"
                record_caught_exception(
                    "chat",
                    "chat.stream.cancelled",
                    "A chat response stream disconnected.",
                    caught_error,
                    stage="stream",
                )
                raise
            except (ChatError, ProviderError, ConflictError) as exc:
                outcome = "failure"
                feature = "providers" if isinstance(exc, ProviderError) else "chat"
                yield _server_sent_event(
                    "error",
                    stream_error_frame(
                        feature=feature,
                        code="chat_stream_failed",
                        detail=str(exc),
                        exception=exc,
                        retryable=isinstance(exc, ProviderError),
                        expected=isinstance(exc, ConflictError),
                        session_id=chat_session.id if chat_session else None,
                    ),
                )
            except Exception as caught_error:
                outcome = "failure"
                yield _server_sent_event(
                    "error",
                    stream_error_frame(
                        feature="chat",
                        code="chat_stream_failed",
                        detail="chat stream failed",
                        exception=caught_error,
                        retryable=True,
                        session_id=chat_session.id if chat_session else None,
                    ),
                )
            finally:
                emit_diagnostic(
                    "info",
                    "chat",
                    "chat.stream.ended",
                    "A chat response stream ended.",
                    outcome=outcome,
                    stage="stream",
                    duration_ms=(time.monotonic() - started_at) * 1000,
                    session_id=chat_session.id if chat_session else None,
                    metadata={"count": event_count},
                )

        return StreamingResponse(
            _correlated_stream(
                event_stream(),
                request_id=current_request_id(),
                operation_id=current_operation_id(),
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        f"{API_PREFIX}/chat/turns/{{turn_id}}/resume",
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def resume_chat_turn(turn_id: str) -> StreamingResponse:
        service = chat_service()
        prepared = service.prepare_resume(turn_id)

        async def event_stream() -> Any:
            started_at = time.monotonic()
            event_count = 0
            outcome = "success"
            chat_session = prepared.session or prepared.pending_session
            emit_diagnostic(
                "info",
                "chat",
                "chat.resume_stream.started",
                "A resumed chat stream started.",
                outcome="started",
                stage="stream",
                session_id=chat_session.id if chat_session else None,
            )
            try:
                async for event, payload in service.stream(prepared):
                    event_count += 1
                    yield _server_sent_event(event, payload)
            except asyncio.CancelledError as caught_error:
                outcome = "cancelled"
                record_caught_exception(
                    "chat",
                    "chat.resume_stream.cancelled",
                    "A resumed chat stream disconnected.",
                    caught_error,
                    stage="stream",
                )
                raise
            except (ChatError, ProviderError, ConflictError) as exc:
                outcome = "failure"
                feature = "providers" if isinstance(exc, ProviderError) else "chat"
                yield _server_sent_event(
                    "error",
                    stream_error_frame(
                        feature=feature,
                        code="chat_resume_failed",
                        detail=str(exc),
                        exception=exc,
                        retryable=isinstance(exc, ProviderError),
                        expected=isinstance(exc, ConflictError),
                        session_id=chat_session.id if chat_session else None,
                    ),
                )
            finally:
                emit_diagnostic(
                    "info",
                    "chat",
                    "chat.resume_stream.ended",
                    "A resumed chat stream ended.",
                    outcome=outcome,
                    stage="stream",
                    duration_ms=(time.monotonic() - started_at) * 1000,
                    session_id=chat_session.id if chat_session else None,
                    metadata={"count": event_count},
                )

        return StreamingResponse(
            _correlated_stream(
                event_stream(),
                request_id=current_request_id(),
                operation_id=current_operation_id(),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get(
        f"{API_PREFIX}/chat/sessions/{{session_id}}/pending-turn",
        response_model=ChatTurnSummary | None,
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def get_pending_chat_turn(session_id: str) -> ChatTurnSummary | None:
        turn = chat_service().pending_turn(session_id)
        return _chat_turn_summary(turn) if turn is not None else None

    @app.post(
        f"{API_PREFIX}/chat/turns/{{turn_id}}/cancel",
        response_model=ChatTurnSummary,
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def cancel_chat_turn(turn_id: str) -> ChatTurnSummary:
        turn = store.get(ChatTurn, turn_id)
        if turn.backend == ChatBackend.HARNESS and turn.harness_turn_id:
            await harness_runtime.cancel_turn(
                turn.harness_turn_id, reason="Stopped by operator"
            )
            return _chat_turn_summary(store.get(ChatTurn, turn.id))
        return _chat_turn_summary(chat_service().cancel_turn(turn_id))

    @app.get(
        f"{API_PREFIX}/chat/sessions/{{session_id}}/messages",
        response_model=list[ChatMessage],
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def list_chat_session_messages(session_id: str) -> list[ChatMessage]:
        return chat_service().session_messages(session_id)

    @app.get(
        f"{API_PREFIX}/chat/sessions/{{session_id}}/context",
        response_model=ContextStatus,
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def get_chat_session_context(session_id: str) -> ContextStatus:
        session = store.get(ChatSession, session_id)
        if session.backend == ChatBackend.HARNESS:
            messages = chat_service().session_messages(session_id)
            estimated = sum(
                estimate_tokens(message.content, message_count=1)
                for message in messages
            )
            return ContextStatus(
                owner_type=ContextOwnerType.CHAT_SESSION,
                owner_id=session.id,
                status="runtime_managed",
                context_window=DEFAULT_CONTEXT_WINDOW,
                max_output_tokens=0,
                target_input_tokens=DEFAULT_CONTEXT_WINDOW,
                estimated_input_tokens=estimated,
            )
        return chat_service().context_status(session_id)

    @app.patch(
        f"{API_PREFIX}/chat-sessions/{{session_id}}",
        response_model=ChatSession,
        tags=["chat-sessions"],
        dependencies=[Depends(require_auth)],
    )
    async def rename_chat_session(
        session_id: str, request: ChatSessionRenameRequest
    ) -> ChatSession:
        current = store.get(ChatSession, session_id)
        if chat_service().pending_turn(session_id) is not None:
            raise ConflictError(
                "conversation cannot be renamed while a response is active"
            )
        return store.update(
            ChatSession,
            session_id,
            {"title": request.title},
            expected_revision=request.expected_revision or current.revision,
        )

    @app.delete(
        f"{API_PREFIX}/chat-sessions/{{session_id}}",
        status_code=204,
        tags=["chat-sessions"],
        dependencies=[Depends(require_auth)],
    )
    async def delete_chat_session(
        session_id: str,
        if_match: int | None = Header(default=None, alias="If-Match"),
    ) -> Response:
        store.get(ChatSession, session_id)
        store.delete_chat_session(session_id, expected_revision=if_match)
        return Response(status_code=204)

    @app.get(
        f"{API_PREFIX}/runs/{{run_id}}/context",
        response_model=ContextStatus,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def get_run_context(run_id: str) -> ContextStatus:
        run = store.get(AgentRun, run_id)
        if run.backend == RunBackend.HARNESS:
            turns = [
                turn
                for turn in store.list_entities(
                    HarnessTurn, engagement_id=run.engagement_id, limit=1_000
                )
                if turn.run_id == run.id
            ]
            return ContextStatus(
                owner_type=ContextOwnerType.AGENT_RUN,
                owner_id=run.id,
                status="runtime_managed",
                context_window=DEFAULT_CONTEXT_WINDOW,
                max_output_tokens=0,
                target_input_tokens=DEFAULT_CONTEXT_WINDOW,
                estimated_input_tokens=sum(
                    estimate_tokens(
                        (turn.prompt or "") + (turn.response or ""), message_count=1
                    )
                    for turn in turns
                ),
            )
        latest = ContextCompactor(store).latest(
            ContextOwnerType.AGENT_RUN, run.id, run.engagement_id
        )
        provider_id = (
            latest.provider_profile_id
            if latest is not None
            else run.supervisor_provider_id
        )
        if provider_id:
            profile = store.get(ProviderProfile, provider_id)
            limits = resolve_context_limits(profile)
            context_window = limits.context_window
            max_output_tokens = limits.max_output_tokens
            target_input_tokens = limits.target_input_tokens
        else:
            context_window = DEFAULT_CONTEXT_WINDOW
            max_output_tokens = min(2_048, context_window // 4)
            target_input_tokens = int((context_window - max_output_tokens) * 0.75)
        task_ids: set[str] = set()
        usage_by_task: dict[str, int] = {}
        estimated_input_tokens = 0
        offset = 0
        while True:
            page = store.list_entities(
                Task,
                engagement_id=run.engagement_id,
                offset=offset,
                limit=1_000,
            )
            for task in page:
                if task.run_id != run.id:
                    continue
                task_ids.add(task.id)
                task_tokens = estimate_tokens(
                    json.dumps(
                        {
                            "title": task.title,
                            "instructions": task.instructions,
                            "status": task.status.value,
                        },
                        ensure_ascii=False,
                    ),
                    message_count=1,
                )
                usage_by_task[task.id] = usage_by_task.get(task.id, 0) + task_tokens
                estimated_input_tokens += task_tokens
            if len(page) < 1_000:
                break
            offset += len(page)
        offset = 0
        while True:
            attempt_page = store.list_entities(
                AgentAttempt,
                engagement_id=run.engagement_id,
                offset=offset,
                limit=1_000,
            )
            for attempt in attempt_page:
                if attempt.run_id != run.id:
                    continue
                attempt_tokens = estimate_tokens(
                    json.dumps(
                        {
                            "input": attempt.input,
                            "output": attempt.output,
                            "error": attempt.error,
                        },
                        ensure_ascii=False,
                    ),
                    message_count=1,
                )
                usage_by_task[attempt.task_id] = (
                    usage_by_task.get(attempt.task_id, 0) + attempt_tokens
                )
                estimated_input_tokens += attempt_tokens
            if len(attempt_page) < 1_000:
                break
            offset += len(attempt_page)
        status = (
            "not_needed" if estimated_input_tokens <= target_input_tokens else "stale"
        )
        through = 0
        if latest is not None:
            cited_task_ids = {
                reference.source_id
                for reference in latest.source_references
                if reference.source_kind in {"task", "task_result"}
            }
            if latest.status == ContextSnapshotStatus.FAILED:
                status = "failed"
            elif task_ids - cited_task_ids:
                status = "stale"
            else:
                status = "ready"
            through = latest.compacted_through
            if latest.status == ContextSnapshotStatus.READY and latest.memory:
                estimated_input_tokens = estimate_tokens(
                    memory_text(latest.memory)
                ) + sum(
                    usage_by_task.get(task_id, 0)
                    for task_id in task_ids - cited_task_ids
                )
        return ContextStatus(
            owner_type=ContextOwnerType.AGENT_RUN,
            owner_id=run.id,
            status=status,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            target_input_tokens=target_input_tokens,
            estimated_input_tokens=estimated_input_tokens,
            compacted_through=through,
            source_references=latest.source_references if latest else [],
            compaction_usage=latest.usage if latest else ChatTokenUsage(),
            compaction_cost_usd=latest.cost_usd if latest else 0.0,
            snapshot=latest,
        )

    if allow_internal_event_append:

        @app.post(
            f"{API_PREFIX}/runs/{{run_id}}/events",
            response_model=RunEvent,
            status_code=201,
            tags=["runs"],
            dependencies=[Depends(require_auth)],
        )
        async def append_run_event(
            run_id: str, request: EventAppendRequest
        ) -> RunEvent:
            store.get(AgentRun, run_id)
            return store.append_event(
                run_id,
                request.event_type,
                request.payload,
                actor_id=request.actor_id,
                idempotency_key=request.idempotency_key,
            )

    @app.get(
        f"{API_PREFIX}/runs/{{run_id}}/events",
        response_model=EventList,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def replay_run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=10_000),
    ) -> EventList:
        store.get(AgentRun, run_id)
        events = store.replay_events(run_id, after_sequence=after, limit=limit)
        return EventList(
            events=events,
            next_sequence=events[-1].sequence if events else after,
        )

    @app.websocket(f"{API_PREFIX}/runs/{{run_id}}/events/ws")
    async def run_event_socket(
        websocket: WebSocket,
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> None:
        request_id = new_request_id()
        supplied: str | None = None
        authorization = websocket.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        offered_protocols = [
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        subprotocol_token: str | None = None
        for protocol in offered_protocols:
            if not protocol.startswith("nebula.auth."):
                continue
            encoded = protocol.removeprefix("nebula.auth.")
            try:
                padding = "=" * (-len(encoded) % 4)
                subprotocol_token = base64.urlsafe_b64decode(encoded + padding).decode(
                    "utf-8"
                )
            except (ValueError, UnicodeDecodeError) as caught_error:
                record_caught_exception(
                    "missions",
                    "missions.stream.authentication_rejected",
                    "A mission stream authentication value was malformed.",
                    caught_error,
                    stage="stream-negotiation",
                )
                subprotocol_token = None
            break
        if (
            supplied
            and subprotocol_token
            and not hmac.compare_digest(supplied, subprotocol_token)
        ):
            emit_diagnostic(
                "warning",
                "missions",
                "missions.stream.authentication_denied",
                "Mission event stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                run_id=run_id,
                metadata={"reason_code": "conflicting-authentication"},
            )
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = subprotocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            emit_diagnostic(
                "warning",
                "missions",
                "missions.stream.authentication_denied",
                "Mission event stream authentication was denied.",
                outcome="denied",
                stage="stream-negotiation",
                request_id=request_id,
                run_id=run_id,
                metadata={"reason_code": "authentication-required"},
            )
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        try:
            store.get(AgentRun, run_id)
        except NotFoundError as caught_error:
            record_caught_exception(
                "missions",
                "missions.stream.not_found",
                "The requested mission stream did not exist.",
                caught_error,
                stage="stream-negotiation",
            )
            await websocket.close(code=4404, reason="agent run not found")
            return
        event_protocol = (
            "nebula.events.v1" if "nebula.events.v1" in offered_protocols else None
        )
        await websocket.accept(subprotocol=event_protocol)
        started_at = time.monotonic()
        event_count = 0
        cursor = after
        emit_diagnostic(
            "info",
            "missions",
            "missions.stream.connected",
            "A mission event stream connected.",
            outcome="started",
            stage="stream",
            request_id=request_id,
            run_id=run_id,
            metadata={"sequence_start": after},
        )
        try:
            while True:
                events = store.replay_events(run_id, after_sequence=cursor, limit=1000)
                if not events:
                    break
                if events[0].sequence > cursor + 1:
                    emit_diagnostic(
                        "warning",
                        "missions",
                        "missions.stream.sequence_gap",
                        "A mission event sequence gap was detected.",
                        outcome="degraded",
                        stage="replay",
                        request_id=request_id,
                        run_id=run_id,
                        metadata={
                            "sequence_start": cursor,
                            "sequence_end": events[0].sequence,
                        },
                    )
                    await websocket.send_json(
                        {
                            "kind": "replay_gap",
                            "after_sequence": cursor,
                            "next_sequence": events[0].sequence,
                        }
                    )
                for event in events:
                    event_count += 1
                    await websocket.send_json(
                        {"kind": "event", "event": event.model_dump(mode="json")}
                    )
                    cursor = event.sequence
            await websocket.send_json(
                {"kind": "replay_complete", "after_sequence": cursor}
            )

            idle_ticks = 0
            while True:
                await asyncio.sleep(0.25)
                events = store.replay_events(run_id, after_sequence=cursor, limit=1000)
                if events:
                    if events[0].sequence > cursor + 1:
                        emit_diagnostic(
                            "warning",
                            "missions",
                            "missions.stream.sequence_gap",
                            "A mission event sequence gap was detected.",
                            outcome="degraded",
                            stage="replay",
                            request_id=request_id,
                            run_id=run_id,
                            metadata={
                                "sequence_start": cursor,
                                "sequence_end": events[0].sequence,
                            },
                        )
                        await websocket.send_json(
                            {
                                "kind": "replay_gap",
                                "after_sequence": cursor,
                                "next_sequence": events[0].sequence,
                            }
                        )
                    idle_ticks = 0
                    for event in events:
                        event_count += 1
                        await websocket.send_json(
                            {"kind": "event", "event": event.model_dump(mode="json")}
                        )
                        cursor = event.sequence
                else:
                    idle_ticks += 1
                    if idle_ticks >= 20:
                        await websocket.send_json(
                            {"kind": "heartbeat", "after_sequence": cursor}
                        )
                        idle_ticks = 0
        except WebSocketDisconnect as caught_error:
            record_caught_exception(
                "missions",
                "missions.stream.disconnected",
                "A mission event stream disconnected.",
                caught_error,
                stage="stream",
            )
            return
        except Exception as exc:
            frame = stream_error_frame(
                feature="missions",
                code="mission_stream_failed",
                detail="mission event stream failed",
                exception=exc,
                retryable=True,
                request_id=request_id,
                run_id=run_id,
            )
            frame["kind"] = "error"
            try:
                await websocket.send_json(frame)
            except (RuntimeError, WebSocketDisconnect):
                # diagnostic-expected: the stream failure is already recorded.
                pass
        finally:
            emit_diagnostic(
                "info",
                "missions",
                "missions.stream.disconnected",
                "A mission event stream ended.",
                outcome="stopped",
                stage="stream",
                duration_ms=(time.monotonic() - started_at) * 1000,
                request_id=request_id,
                run_id=run_id,
                metadata={
                    "count": event_count,
                    "sequence_start": after,
                    "sequence_end": cursor,
                },
            )

    if artifact_store is not None:

        @app.get(
            f"{API_PREFIX}/tool-calls/{{tool_call_id}}/artifacts",
            response_model=list[Artifact],
            tags=["artifacts"],
            dependencies=[Depends(require_auth)],
        )
        async def tool_call_artifacts(tool_call_id: str) -> list[Artifact]:
            call = store.get(ToolCall, tool_call_id)
            return sorted(
                [
                    item
                    for item in store.list_entities(
                        Artifact, engagement_id=call.engagement_id, limit=1_000
                    )
                    if item.metadata.get("tool_call_id") == call.id
                ],
                key=lambda item: (item.created_at, item.id),
            )

        @app.post(
            f"{API_PREFIX}/tool-calls/{{tool_call_id}}/output/search",
            tags=["artifacts"],
            dependencies=[Depends(require_auth)],
        )
        async def search_tool_call_output(
            tool_call_id: str, request: ToolOutputSearchRequest
        ) -> dict[str, Any]:
            call = store.get(ToolCall, tool_call_id)
            try:
                return await asyncio.to_thread(
                    ToolOutputService(store, artifact_store).search,
                    engagement_id=call.engagement_id,
                    owner_id=call.run_id,
                    tool_call_id=call.id,
                    **request.model_dump(),
                )
            except ToolOutputQueryError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ToolOutputAccessError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.post(
            f"{API_PREFIX}/artifacts/{{artifact_id}}/output/read",
            tags=["artifacts"],
            dependencies=[Depends(require_auth)],
        )
        async def read_tool_output_artifact(
            artifact_id: str, request: ToolOutputReadRequest
        ) -> dict[str, Any]:
            artifact = store.get(Artifact, artifact_id)
            call_id = artifact.metadata.get("tool_call_id")
            if not isinstance(call_id, str):
                raise HTTPException(status_code=404, detail="artifact is unavailable")
            call = store.get(ToolCall, call_id)
            try:
                return await asyncio.to_thread(
                    ToolOutputService(store, artifact_store).read,
                    engagement_id=call.engagement_id,
                    owner_id=call.run_id,
                    artifact_id=artifact.id,
                    **request.model_dump(),
                )
            except ToolOutputQueryError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ToolOutputAccessError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.get(
            f"{API_PREFIX}/artifacts/{{artifact_id}}/content",
            tags=["artifacts"],
            dependencies=[Depends(require_auth)],
        )
        async def artifact_content(
            artifact_id: str,
            sensitive_data_acknowledged: str | None = Header(
                default=None,
                alias="X-Nebula-Sensitive-Data-Acknowledged",
            ),
        ) -> FileResponse:
            artifact = store.get(Artifact, artifact_id)
            if (
                isinstance(artifact.metadata.get("tool_call_id"), str)
                and (sensitive_data_acknowledged or "").lower() != "true"
            ):
                raise HTTPException(
                    status_code=428,
                    detail=(
                        "raw tool artifact download requires explicit sensitive-data "
                        "acknowledgement"
                    ),
                )
            path = artifact_store.path_for(artifact)
            if not path.is_file():
                raise NotFoundError(f"artifact content not found: {artifact_id}")
            if not artifact_store.verify(artifact):
                raise ArtifactStoreError(
                    f"artifact content failed integrity verification: {artifact_id}"
                )
            return FileResponse(
                path,
                media_type=artifact.media_type,
                filename=artifact.filename,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "X-Content-Type-Options": "nosniff",
                    "X-Nebula-Artifact-SHA256": artifact.sha256,
                    "X-Nebula-Artifact-Bytes": str(artifact.size),
                    "X-Nebula-Artifact-Truncated": str(
                        bool(artifact.metadata.get("truncated"))
                    ).lower(),
                },
            )

    for resource, model in ENTITY_MODEL_BY_KIND.items():
        if resource in CUSTOM_RESOURCES:
            continue
        _register_crud_routes(
            app,
            store,
            require_auth,
            entity_validator,
            resource,
            model,
            read_only=resource in READ_ONLY_RESOURCES,
            append_only=resource in APPEND_ONLY_RESOURCES,
            after_create=(
                (
                    lambda entity: tool_platform.enable_default_local_packs(
                        entity.id,
                        assigned_by=active_operator_id(),
                    )
                )
                if model is Engagement and tool_platform is not None
                else None
            ),
        )
    _assert_unique_api_operations(app)

    if static_dir is not None:
        frontend = Path(static_dir).expanduser().resolve()
        if not (frontend / "index.html").is_file():
            raise ValueError("static_dir must contain a built index.html")
        app.mount("/", SpaStaticFiles(directory=frontend, html=True), name="workspace")

    return app


def _server_sent_event(event: str, payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    event_id = payload.get("sequence") or payload.get("id")
    identifier = (
        f"id: {str(event_id).replace(chr(10), '').replace(chr(13), '')}\n"
        if event_id is not None
        else ""
    )
    return f"{identifier}event: {event}\ndata: {encoded}\n\n".encode()


async def _correlated_stream(
    stream: AsyncIterator[bytes],
    *,
    request_id: str | None,
    operation_id: str | None,
) -> AsyncIterator[bytes]:
    """Preserve request correlation after the HTTP response starts streaming."""

    with diagnostic_context(request_id=request_id, operation_id=operation_id):
        async for item in stream:
            yield item


def _setup_server_sent_event(event: SetupEvent) -> bytes:
    payload = event.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (f"id: {event.sequence}\nevent: setup\ndata: {encoded}\n\n").encode()


def _chat_turn_summary(turn: ChatTurn) -> ChatTurnSummary:
    return ChatTurnSummary(
        id=turn.id,
        session_id=turn.session_id,
        status=turn.status,
        approval_id=turn.approval_id,
        harness_turn_id=turn.harness_turn_id,
        tool_call_ids=turn.tool_call_ids,
        revision=turn.revision,
    )


async def _discover_local_provider_services(
    provider_factory: Callable[[ProviderProfile], Any] | None = None,
) -> list[LocalProviderDetection]:
    """Discover only fixed, known loopback services with bounded model probes."""

    flavors = (
        ProviderFlavor.OLLAMA,
        ProviderFlavor.VLLM,
        ProviderFlavor.LM_STUDIO,
    )

    async def detect(flavor: ProviderFlavor) -> LocalProviderDetection | None:
        entry = PROVIDER_CATALOG[flavor]
        if not entry.local or not entry.default_base_url:
            return None
        profile = ProviderProfile(
            id=f"detected-{flavor.value}",
            name=entry.display_name,
            provider_type=flavor.value,
            endpoint=entry.default_base_url,
            is_local=True,
        )
        try:
            health = await asyncio.wait_for(
                _provider_health(profile, provider_factory), timeout=2.0
            )
        except asyncio.TimeoutError as caught_error:
            record_caught_exception(
                "api",
                "api.api.caught_failure_056",
                "A handled api operation raised an exception.",
                caught_error,
                stage="api",
            )
            return None
        if not health.healthy:
            return None
        models = [
            model.strip()
            for model in health.models
            if isinstance(model, str) and model.strip() and len(model.strip()) <= 500
        ][:256]
        return LocalProviderDetection(
            flavor=flavor,
            display_name=entry.display_name,
            endpoint=entry.default_base_url,
            models=list(dict.fromkeys(models)),
        )

    discovered = await asyncio.gather(*(detect(flavor) for flavor in flavors))
    return [candidate for candidate in discovered if candidate is not None]


async def _provider_health(
    profile: ProviderProfile,
    provider_factory: Callable[[ProviderProfile], Any] | None = None,
) -> ProviderHealth:
    """Return bounded, allowlisted health without reviving disabled profiles."""

    if not profile.enabled:
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail="provider profile is disabled",
        )
    try:
        health = await (provider_factory or provider_from_profile)(profile).health()
    except (ProviderError, ValueError) as exc:
        record_caught_exception(
            "api",
            "api.api.caught_failure_057",
            "A handled api operation raised an exception.",
            exc,
            stage="api",
        )
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail=str(exc),
        )
    except Exception as exc:
        record_caught_exception(
            "api",
            "api.api.caught_failure_058",
            "A handled api operation raised an exception.",
            exc,
            stage="api",
        )
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail=f"provider health check failed ({type(exc).__name__})",
        )
    models = health.models
    if profile.model_allowlist:
        allowed = set(profile.model_allowlist)
        models = [model for model in models if model in allowed]
    return health.model_copy(update={"models": list(dict.fromkeys(models))})


def _safe_verification_failure(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        detail = str(exc)
        detail = re.sub(
            r"(?i)(authorization|api[_-]?key|token|secret)\s*[:=]?\s*\S+",
            r"\1 [redacted]",
            detail,
        )
        return detail[:1_000]
    return f"capability probe failed ({type(exc).__name__})"


async def _verify_provider_capability(
    store: NebulaStore,
    profile: ProviderProfile,
    model: str,
    provider_factory: Callable[[ProviderProfile], Any] | None = None,
) -> ProviderCapabilityVerifyResponse:
    """Perform and durably record a harmless exact-model required-call probe."""

    if profile.model_allowlist and model not in profile.model_allowlist:
        raise ValueError("verification model must be present in model_allowlist")
    nonce = secrets.token_urlsafe(18)
    probe_name = "nebula_capability_probe"
    probe_profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={
                    "tool_calling": True,
                    "strict_structured_output": True,
                    "parallel_tool_calls": False,
                }
            )
        }
    )
    try:
        response = await asyncio.wait_for(
            (provider_factory or provider_from_profile)(probe_profile).complete(
                ModelRequest(
                    model=model,
                    instructions=(
                        "Capability verification. Call the supplied function exactly once "
                        "with the required nonce. Return no prose."
                    ),
                    messages=[
                        ModelMessage(
                            role="user",
                            content="Make the required capability-verification call now.",
                        )
                    ],
                    tools=[
                        ToolDefinition(
                            name=probe_name,
                            description="Echo a harmless one-time verification nonce.",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "nonce": {"type": "string", "enum": [nonce]}
                                },
                                "required": ["nonce"],
                                "additionalProperties": False,
                            },
                        )
                    ],
                    tool_choice=ToolChoice.REQUIRED,
                    parallel_tool_calls=False,
                    max_output_tokens=128,
                    temperature=0,
                )
            ),
            timeout=PROVIDER_CAPABILITY_PROBE_TIMEOUT_SECONDS,
        )
        if response.text.strip():
            raise ProviderError("probe returned prose instead of only a tool call")
        if len(response.tool_calls) != 1:
            raise ProviderError("probe did not return exactly one structured tool call")
        call = response.tool_calls[0]
        if not call.id:
            raise ProviderError("probe tool call omitted its call ID")
        if call.name != probe_name:
            raise ProviderError("probe returned the wrong function name")
        if call.arguments != {"nonce": nonce}:
            raise ProviderError(
                "probe returned arguments that failed strict validation"
            )
        finish_reason = (response.finish_reason or "").lower()
        if finish_reason not in {"tool_calls", "tool_use", "completed", "stop"}:
            raise ProviderError("probe returned an invalid finish reason")
        verification = ProviderCapabilityVerification(
            model=model,
            status=ProviderVerificationStatus.VERIFIED,
        )
    except Exception as exc:
        record_caught_exception(
            "api",
            "api.api.caught_failure_059",
            "A handled api operation raised an exception.",
            exc,
            stage="api",
        )
        verification = ProviderCapabilityVerification(
            model=model,
            status=ProviderVerificationStatus.FAILED,
            failure_detail=_safe_verification_failure(exc),
        )

    verifications = dict(profile.capability_verifications)
    verifications[model] = verification
    has_verified_model = any(
        item.status == ProviderVerificationStatus.VERIFIED
        and item.contract_version == "required-tool-v1"
        for item in verifications.values()
    )
    updated = store.update(
        ProviderProfile,
        profile.id,
        {
            "capability_verifications": verifications,
            # A health-discovered model may be verified before the operator has
            # configured an allowlist. Persist that explicit verification target
            # so subsequent profile reads and mission selectors do not forget it.
            "model_allowlist": profile.model_allowlist or [model],
            "capabilities": profile.capabilities.model_copy(
                update={
                    "tool_calling": has_verified_model,
                    "strict_structured_output": has_verified_model,
                    "parallel_tool_calls": False,
                }
            ),
        },
        expected_revision=profile.revision,
    )
    return ProviderCapabilityVerifyResponse(
        provider_id=profile.id,
        provider_revision=updated.revision,
        verification=verification,
    )


def _provider_contract_fingerprint(profile: ProviderProfile) -> tuple[Any, ...]:
    metadata = profile.metadata
    options = metadata.get("options")
    return (
        profile.provider_type,
        profile.endpoint,
        profile.secret_ref,
        profile.is_local,
        tuple(profile.model_allowlist),
        metadata.get("default_model"),
        json.dumps(options, sort_keys=True, default=str),
    )


def _invalidate_provider_verification(
    current: ProviderProfile,
    candidate: ProviderProfile,
) -> ProviderProfile:
    """Fail closed when any compatibility-sensitive provider field changes."""

    changed = _provider_contract_fingerprint(current) != _provider_contract_fingerprint(
        candidate
    )
    verifications = {} if changed else current.capability_verifications
    has_verified_model = any(
        item.status == ProviderVerificationStatus.VERIFIED
        and item.contract_version == "required-tool-v1"
        for item in verifications.values()
    )
    return candidate.model_copy(
        update={
            "capability_verifications": verifications,
            "capabilities": candidate.capabilities.model_copy(
                update={
                    "tool_calling": has_verified_model,
                    "strict_structured_output": has_verified_model,
                    "parallel_tool_calls": False,
                }
            ),
        }
    )


def _assert_unique_api_operations(app: FastAPI) -> None:
    """Fail startup when path-parameter names hide duplicate operations."""

    seen: dict[tuple[str, str], str] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(API_PREFIX):
            continue
        shape = re.sub(r"\{[^}]+\}", "{}", route.path)
        for method in route.methods:
            key = (method, shape)
            previous = seen.get(key)
            if previous is not None:
                raise RuntimeError(
                    f"duplicate API operation {method} {shape}: "
                    f"{previous} and {route.path}"
                )
            seen[key] = route.path


def _register_crud_routes(
    app: FastAPI,
    store: NebulaStore,
    require_auth: Callable[..., Any],
    entity_validator: ApiEntityValidator,
    resource: str,
    model: type[Entity],
    *,
    read_only: bool = False,
    append_only: bool = False,
    after_create: Callable[[Entity], Any] | None = None,
) -> None:
    """Register typed routes while preserving concrete OpenAPI schemas."""

    def make_create() -> Callable[..., Any]:
        async def create_entity(entity: Any) -> Entity:
            protected = {"id", "created_at", "updated_at", "revision"}.intersection(
                entity.model_fields_set
            )
            if protected:
                raise ValueError(
                    f"cannot set server-managed fields: {sorted(protected)}"
                )
            if isinstance(entity, ProviderProfile):
                entity = entity.model_copy(
                    update={
                        "capability_verifications": {},
                        "capabilities": entity.capabilities.model_copy(
                            update={
                                "tool_calling": False,
                                "strict_structured_output": False,
                                "parallel_tool_calls": False,
                            }
                        ),
                    }
                )
            entity_validator.validate_create(entity)
            created = store.create(entity)
            if after_create is not None:
                after_create(created)
            return created

        create_entity.__name__ = f"create_{resource.replace('-', '_')}"
        create_entity.__annotations__ = {"entity": model, "return": model}
        return create_entity

    def make_list() -> Callable[..., Any]:
        async def list_entities(
            engagement_id: str | None = None,
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=100, ge=1, le=1000),
        ) -> list[Entity]:
            entities = store.list_entities(
                model,
                engagement_id=engagement_id,
                offset=offset,
                limit=limit,
            )
            if model is KnowledgeSource:
                return [
                    knowledge_summary(entity)
                    for entity in entities
                    if isinstance(entity, KnowledgeSource)
                ]
            return entities

        list_entities.__name__ = f"list_{resource.replace('-', '_')}"
        list_entities.__annotations__["return"] = list[model]  # type: ignore[valid-type]
        return list_entities

    def make_get() -> Callable[..., Any]:
        async def get_entity(entity_id: str) -> Entity:
            entity = store.get(model, entity_id)
            return (
                knowledge_summary(entity)
                if isinstance(entity, KnowledgeSource)
                else entity
            )

        get_entity.__name__ = f"get_{resource.replace('-', '_')}"
        get_entity.__annotations__["return"] = model
        return get_entity

    def make_replace() -> Callable[..., Any]:
        async def replace_entity(
            entity_id: str,
            entity: Any,
            if_match: int | None = Header(default=None, alias="If-Match"),
        ) -> Entity:
            if entity.id != entity_id:
                raise ValueError("replacement id must match the resource id")
            current = store.get(model, entity_id)
            if if_match is not None and current.revision != if_match:
                raise ConflictError(
                    f"revision conflict: expected {if_match}, found {current.revision}"
                )
            entity_validator.validate_update(current, entity)
            if isinstance(current, ProviderProfile) and isinstance(
                entity, ProviderProfile
            ):
                entity = _invalidate_provider_verification(current, entity)
            replaced = store.replace(
                model,
                entity_id,
                entity,
                expected_revision=current.revision if if_match is None else if_match,
            )
            return replaced

        replace_entity.__name__ = f"replace_{resource.replace('-', '_')}"
        replace_entity.__annotations__["entity"] = model
        replace_entity.__annotations__["return"] = model
        return replace_entity

    def make_patch() -> Callable[..., Any]:
        async def patch_entity(entity_id: str, patch: PatchRequest) -> Entity:
            protected = {"id", "created_at", "updated_at", "revision"}.intersection(
                patch.changes
            )
            if protected:
                raise ValueError(f"cannot patch protected fields: {sorted(protected)}")
            current = store.get(model, entity_id)
            if (
                patch.expected_revision is not None
                and current.revision != patch.expected_revision
            ):
                raise ConflictError(
                    f"revision conflict: expected {patch.expected_revision}, "
                    f"found {current.revision}"
                )
            payload = current.model_dump(mode="python")
            payload.update(patch.changes)
            candidate = model.model_validate(payload)
            changes = dict(patch.changes)
            if isinstance(current, ProviderProfile) and isinstance(
                candidate, ProviderProfile
            ):
                candidate = _invalidate_provider_verification(current, candidate)
                changes = {
                    key: value
                    for key, value in candidate.model_dump(mode="python").items()
                    if key not in {"id", "created_at", "updated_at", "revision"}
                    and value != getattr(current, key)
                }
            entity_validator.validate_update(current, candidate)
            updated = store.update(
                model,
                entity_id,
                changes,
                expected_revision=(
                    current.revision
                    if patch.expected_revision is None
                    else patch.expected_revision
                ),
            )
            return updated

        patch_entity.__name__ = f"patch_{resource.replace('-', '_')}"
        patch_entity.__annotations__["return"] = model
        return patch_entity

    def make_delete() -> Callable[..., Any]:
        async def delete_entity(
            entity_id: str,
            if_match: int | None = Header(default=None, alias="If-Match"),
        ) -> Response:
            current = store.get(model, entity_id)
            if if_match is not None and current.revision != if_match:
                raise ConflictError(
                    f"revision conflict: expected {if_match}, found {current.revision}"
                )
            if model is Engagement:
                if store.engagement_has_dependents(entity_id):
                    raise ConflictError(
                        "engagement cannot be deleted while owned entities exist; "
                        "archive it instead"
                    )
            if model is ProviderProfile:
                if store.provider_has_history_references(entity_id):
                    raise ConflictError(
                        "provider profile cannot be deleted while durable chat or run "
                        "history references it"
                    )
            entity_validator.validate_delete(current)
            # Always guard the final delete with the revision we validated so a
            # concurrent update cannot be removed using stale relationship data.
            store.delete(model, entity_id, expected_revision=current.revision)
            return Response(status_code=204)

        delete_entity.__name__ = f"delete_{resource.replace('-', '_')}"
        return delete_entity

    base = f"{API_PREFIX}/{resource.replace('_', '-')}"
    tag = resource.replace("_", "-")
    dependencies = [Depends(require_auth)]
    if not read_only:
        app.add_api_route(
            base,
            make_create(),
            methods=["POST"],
            response_model=model,
            status_code=201,
            tags=[tag],
            dependencies=dependencies,
        )
    app.add_api_route(
        base,
        make_list(),
        methods=["GET"],
        response_model=list[model],  # type: ignore[valid-type]
        tags=[tag],
        dependencies=dependencies,
    )
    app.add_api_route(
        f"{base}/{{entity_id}}",
        make_get(),
        methods=["GET"],
        response_model=model,
        tags=[tag],
        dependencies=dependencies,
    )
    if not read_only and not append_only:
        app.add_api_route(
            f"{base}/{{entity_id}}",
            make_replace(),
            methods=["PUT"],
            response_model=model,
            tags=[tag],
            dependencies=dependencies,
        )
        app.add_api_route(
            f"{base}/{{entity_id}}",
            make_patch(),
            methods=["PATCH"],
            response_model=model,
            tags=[tag],
            dependencies=dependencies,
        )
        app.add_api_route(
            f"{base}/{{entity_id}}",
            make_delete(),
            methods=["DELETE"],
            status_code=204,
            tags=[tag],
            dependencies=dependencies,
        )
