"""Versioned FastAPI surface for the Nebula 3 core."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Scope

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
    ChatService,
)
from .database import Database
from .context import (
    DEFAULT_CONTEXT_WINDOW,
    ContextCompactor,
    ContextStatus,
    estimate_tokens,
    memory_text,
    resolve_context_limits,
)
from .domain import (
    ENTITY_MODEL_BY_KIND,
    AgentAttempt,
    AgentRun,
    Approval,
    ApprovalStatus,
    Artifact,
    ChatMessage,
    ChatTokenUsage,
    ContextOwnerType,
    ContextSnapshotStatus,
    Engagement,
    EngagementToolAssignment,
    Entity,
    Evidence,
    KnowledgeSource,
    MissionGrant,
    NebulaModel,
    OperatorProfile,
    ProviderProfile,
    Task,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    RunBudget,
    RunEvent,
    RunStatus,
    ScopePolicy,
    ToolPackInstallation,
    ToolPackInstallationStatus,
    utc_now,
)
from .evidence import (
    EvidenceReferenceError,
    EvidenceTooLargeError,
    EvidenceUploadRequest,
    InvalidEvidenceUploadError,
    upload_evidence,
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
from .operators import OperatorProfileService
from .providers import (
    PROVIDER_CATALOG,
    ProviderError,
    ProviderHealth,
    provider_from_profile,
)
from .pty import HumanPtyService
from .storage import ConflictError, NebulaStore, NotFoundError
from .tool_platform import ToolPlatform, ToolPlatformError
from .version import __version__, build_metadata

READ_ONLY_RESOURCES = {
    "agent_attempts",
    "approvals",
    "artifacts",
    "chat_messages",
    "chat_sessions",
    "evidence",
    "knowledge",
    "runs",
    "source_snapshots",
    "tasks",
    "tool_calls",
}
APPEND_ONLY_RESOURCES: set[str] = set()
CUSTOM_RESOURCES = {
    "context_snapshots",
    "operator_profiles",
    "runner_profiles",
}

API_PREFIX = "/api/v1"
TOOL_PACK_EVENT_POLL_SECONDS = 0.25
TOOL_PACK_EVENT_HEARTBEAT_TICKS = 20


class SpaStaticFiles(StaticFiles):
    """Serve the workspace index for extensionless browser navigation routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
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


class PatchRequest(NebulaModel):
    changes: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class ApprovalDecisionRequest(NebulaModel):
    decision: str = Field(pattern=r"^(approve|reject|stop)$")
    reason: str | None = None
    edited_arguments: dict[str, Any] | None = None


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
    provider_id: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    max_duration_seconds: int = Field(
        default=900, ge=1, le=MAX_API_MISSION_DURATION_SECONDS
    )
    max_tokens: int = Field(default=32_000, ge=1, le=MAX_API_MISSION_TOKENS)
    max_cost_usd: float | None = Field(default=None, ge=0, le=MAX_API_MISSION_COST_USD)
    max_retries: int = Field(default=1, ge=0, le=MAX_API_MISSION_RETRIES)
    tool_names: list[str] = Field(default_factory=list, max_length=64)
    max_tool_calls: int = Field(default=0, ge=0, le=100)
    max_concurrency: int = Field(default=1, ge=1, le=2)


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
    enable_human_pty: bool = False,
    human_pty_root: str | Path | None = None,
    mission_service: MissionService | None = None,
    mission_checkpoint_path: str | Path | None = None,
    tool_platform: ToolPlatform | None = None,
    enable_executable_missions: bool | None = None,
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
    executable_missions_enabled = (
        tool_platform.execution_enabled
        if enable_executable_missions is None and tool_platform is not None
        else bool(enable_executable_missions)
    )

    missions = mission_service or MissionService(
        store,
        checkpoint_path=mission_checkpoint_path,
        tool_components_factory=(
            tool_platform.mission_components if tool_platform is not None else None
        ),
    )
    if missions.store is not store:
        raise ValueError("mission_service must use the API store")
    entity_validator = ApiEntityValidator(store)
    operators = OperatorProfileService(store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await missions.startup()
        try:
            yield
        finally:
            await missions.shutdown()

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
    app.state.mission_service = missions
    app.state.operator_profile_service = operators
    app.state.tool_platform = tool_platform
    app.state.executable_missions_enabled = executable_missions_enabled
    pty_service = (
        HumanPtyService(human_pty_root)
        if enable_human_pty and human_pty_root is not None
        else None
    )
    if enable_human_pty and pty_service is None:
        raise ValueError("enable_human_pty requires a human_pty_root")
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
        allow_headers=["Authorization", "Content-Type", "If-Match"],
    )

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
    async def not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def validation_handler(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors(include_url=False))},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ArtifactStoreError)
    async def artifact_error_handler(
        _: Request, exc: ArtifactStoreError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(MissionConfigurationError)
    async def mission_configuration_handler(
        _: Request, exc: MissionConfigurationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(MissionCapacityError)
    async def mission_capacity_handler(
        _: Request, exc: MissionCapacityError
    ) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    @app.exception_handler(MissionStateError)
    async def mission_state_handler(_: Request, exc: MissionStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(MissionServiceUnavailable)
    async def mission_unavailable_handler(
        _: Request, exc: MissionServiceUnavailable
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ToolPlatformError)
    async def tool_platform_error_handler(
        _: Request, exc: ToolPlatformError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ChatHistoryConflict)
    @app.exception_handler(ChatPrivacyError)
    async def chat_conflict_handler(_: Request, exc: ChatError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ChatConfigurationError)
    async def chat_configuration_handler(
        _: Request, exc: ChatConfigurationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ChatCompactionError)
    async def chat_compaction_handler(
        _: Request, exc: ChatCompactionError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "retryable": True},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(ChatError)
    @app.exception_handler(ProviderError)
    async def chat_provider_handler(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get(f"{API_PREFIX}/health", tags=["system"])
    async def health(_: str = Depends(require_auth)) -> dict[str, Any]:
        identity = build_metadata()
        return {
            "status": "ok",
            **identity,
            "mode": "local"
            if store.database.engine.dialect.name == "sqlite"
            else "team",
            # Runner health belongs to the separately configured worker. The
            # API never assumes that presence of a container CLI makes it safe.
            "runner": "unavailable",
            "human_pty": (
                "ready"
                if pty_service is not None and pty_service.available
                else "unavailable"
            ),
            "api_version": "v1",
            **store.database.health(),
        }

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
        approval_run = store.get(AgentRun, approval.run_id)
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
        active_operator = operators.active_profile_or_none()
        operator_id = active_operator.id if active_operator is not None else "operator"
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
        if approval_run.status == RunStatus.WAITING_APPROVAL:
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
        if artifact_store is None:
            raise HTTPException(
                status_code=503,
                detail="evidence upload requires an artifact store",
            )
        if request.captured_by is not None:
            try:
                operators.get_profile(request.captured_by)
            except NotFoundError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "captured_by references a missing operator profile: "
                        f"{request.captured_by}"
                    ),
                ) from exc
        try:
            return await asyncio.to_thread(
                upload_evidence,
                store=store,
                artifact_store=artifact_store,
                request=request,
            )
        except EvidenceTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except InvalidEvidenceUploadError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except EvidenceReferenceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
        active_operator = operators.active_profile_or_none()
        operator_id = active_operator.id if active_operator is not None else "operator"
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
        except ConflictError:
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
        if tool_platform is not None:
            tool_platform.validate_assignment(
                request.manifest_digest, request.tool_names
            )
        active_operator = operators.active_profile_or_none()
        operator_id = active_operator.id if active_operator is not None else "operator"
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
            "allowed_tool_names": request.tool_names,
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
                allowed_tool_names=request.tool_names,
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
            raise HTTPException(
                status_code=422, detail="tool-pack bundle is not valid base64"
            ) from exc
        return await tool_platform.install_local(
            bundle,
            runtime_profile_id=request.runtime_profile_id,
            confirm_permissions=request.developer_mode_confirmed,
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
            except (ValueError, UnicodeDecodeError):
                subprotocol_token = None
            break
        if (
            supplied
            and subprotocol_token
            and not hmac.compare_digest(supplied, subprotocol_token)
        ):
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = subprotocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        if tool_platform is None:
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
        cursor = after_sequence
        try:
            replay = tool_platform.events.replay(cursor)
            for event in replay.events:
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
                        await websocket.send_json(
                            {
                                "kind": "replay_gap",
                                "after_sequence": cursor,
                                "oldest_sequence": replay.oldest_sequence,
                                "latest_sequence": replay.latest_sequence,
                            }
                        )
                    for event in replay.events:
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
        except WebSocketDisconnect:
            return

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
        except NotFoundError:
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
        if request.tool_names and not executable_missions_enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    "executable missions remain release-gated until the complete "
                    "runner-isolation acceptance flow passes"
                ),
            )
        active_operator = operators.active_profile_or_none()
        operator_id = active_operator.id if active_operator is not None else "operator"
        budget = RunBudget(
            max_concurrency=request.max_concurrency,
            max_delegation_depth=1 if request.tool_names else 0,
            max_duration_seconds=request.max_duration_seconds,
            max_tokens=request.max_tokens,
            max_cost_usd=request.max_cost_usd,
            max_tool_calls=request.max_tool_calls,
            max_retries=request.max_retries,
            per_target_active_operations=1,
        )
        return await missions.start_mission(
            engagement_id=request.engagement_id,
            objective=request.objective,
            provider_id=request.provider_id,
            model=request.model,
            budget=budget,
            tool_names=request.tool_names,
            actor_id=operator_id,
        )

    @app.post(
        f"{API_PREFIX}/runs/{{run_id}}/stop",
        response_model=AgentRun,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def stop_mission(run_id: str, request: MissionStopRequest) -> AgentRun:
        active_operator = operators.active_profile_or_none()
        operator_id = active_operator.id if active_operator is not None else "operator"
        return await missions.stop_mission(
            run_id,
            reason=request.reason,
            actor_id=operator_id,
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
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UnsupportedDocumentError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except InvalidDocumentError as exc:
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
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UnsupportedDocumentError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except InvalidDocumentError as exc:
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

    @app.post(
        f"{API_PREFIX}/providers/{{provider_id}}/health",
        response_model=ProviderHealth,
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def refresh_provider_health(provider_id: str) -> ProviderHealth:
        profile = store.get(ProviderProfile, provider_id)
        return await _provider_health(profile)

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
                return await _provider_health(profile)

        return list(await asyncio.gather(*(checked(profile) for profile in profiles)))

    @app.post(
        f"{API_PREFIX}/chat/completions",
        response_model=ChatCompletionResponse,
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def create_chat_completion(request: ChatCompletionRequest) -> Any:
        service = ChatService(store)
        prepared = await service.prepare_async(request)
        if not request.stream:
            return await service.complete(prepared)

        async def event_stream() -> Any:
            try:
                async for event, payload in service.stream(prepared):
                    yield _server_sent_event(event, payload)
            except asyncio.CancelledError:
                raise
            except (ChatError, ProviderError, ConflictError) as exc:
                yield _server_sent_event("error", {"type": "error", "detail": str(exc)})
            except Exception:
                yield _server_sent_event(
                    "error",
                    {"type": "error", "detail": "chat stream failed"},
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        f"{API_PREFIX}/chat/sessions/{{session_id}}/messages",
        response_model=list[ChatMessage],
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def list_chat_session_messages(session_id: str) -> list[ChatMessage]:
        return ChatService(store).session_messages(session_id)

    @app.get(
        f"{API_PREFIX}/chat/sessions/{{session_id}}/context",
        response_model=ContextStatus,
        tags=["chat"],
        dependencies=[Depends(require_auth)],
    )
    async def get_chat_session_context(session_id: str) -> ContextStatus:
        return ChatService(store).context_status(session_id)

    @app.get(
        f"{API_PREFIX}/runs/{{run_id}}/context",
        response_model=ContextStatus,
        tags=["runs"],
        dependencies=[Depends(require_auth)],
    )
    async def get_run_context(run_id: str) -> ContextStatus:
        run = store.get(AgentRun, run_id)
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
            except (ValueError, UnicodeDecodeError):
                subprotocol_token = None
            break
        if (
            supplied
            and subprotocol_token
            and not hmac.compare_digest(supplied, subprotocol_token)
        ):
            await websocket.close(code=4401, reason="conflicting authentication tokens")
            return
        supplied = subprotocol_token or supplied
        if not allow_unauthenticated and (
            not supplied or not hmac.compare_digest(supplied, token)
        ):
            await websocket.close(code=4401, reason="valid bearer token required")
            return
        try:
            store.get(AgentRun, run_id)
        except NotFoundError:
            await websocket.close(code=4404, reason="agent run not found")
            return
        event_protocol = (
            "nebula.events.v1" if "nebula.events.v1" in offered_protocols else None
        )
        await websocket.accept(subprotocol=event_protocol)
        cursor = after
        try:
            while True:
                events = store.replay_events(run_id, after_sequence=cursor, limit=1000)
                if not events:
                    break
                for event in events:
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
                    idle_ticks = 0
                    for event in events:
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
        except WebSocketDisconnect:
            return

    if pty_service is not None:

        @app.websocket(f"{API_PREFIX}/sessions/{{session_id}}/terminal/ws")
        async def human_terminal_socket(
            websocket: WebSocket,
            session_id: str,
            columns: int = Query(default=120, ge=1, le=1000),
            rows: int = Query(default=40, ge=1, le=1000),
        ) -> None:
            supplied: str | None = None
            authorization = websocket.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                supplied = authorization[7:]
            offered = [
                value.strip()
                for value in websocket.headers.get("sec-websocket-protocol", "").split(
                    ","
                )
                if value.strip()
            ]
            protocol_token: str | None = None
            for protocol in offered:
                if protocol.startswith("nebula.auth."):
                    encoded = protocol.removeprefix("nebula.auth.")
                    try:
                        protocol_token = base64.urlsafe_b64decode(
                            encoded + "=" * (-len(encoded) % 4)
                        ).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        protocol_token = None
                    break
            if (
                supplied
                and protocol_token
                and not hmac.compare_digest(supplied, protocol_token)
            ):
                await websocket.close(
                    code=4401, reason="conflicting authentication tokens"
                )
                return
            supplied = protocol_token or supplied
            if not allow_unauthenticated and (
                not supplied or not hmac.compare_digest(supplied, token)
            ):
                await websocket.close(code=4401, reason="valid bearer token required")
                return
            if "nebula.terminal.v1" not in offered:
                await websocket.close(code=4406, reason="terminal protocol required")
                return
            await websocket.accept(subprotocol="nebula.terminal.v1")
            await pty_service.serve(
                websocket,
                session_id=session_id,
                columns=columns,
                rows=rows,
            )

    if artifact_store is not None:

        @app.get(
            f"{API_PREFIX}/artifacts/{{artifact_id}}/content",
            tags=["artifacts"],
            dependencies=[Depends(require_auth)],
        )
        async def artifact_content(artifact_id: str) -> FileResponse:
            artifact = store.get(Artifact, artifact_id)
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
    return f"event: {event}\ndata: {encoded}\n\n".encode()


async def _provider_health(profile: ProviderProfile) -> ProviderHealth:
    """Return bounded, allowlisted health without reviving disabled profiles."""

    if not profile.enabled:
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail="provider profile is disabled",
        )
    try:
        health = await provider_from_profile(profile).health()
    except (ProviderError, ValueError) as exc:
        return ProviderHealth(
            provider_id=profile.id,
            healthy=False,
            detail=str(exc),
        )
    except Exception as exc:
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
            entity_validator.validate_create(entity)
            return store.create(entity)

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
            return store.replace(
                model,
                entity_id,
                entity,
                expected_revision=current.revision if if_match is None else if_match,
            )

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
            entity_validator.validate_update(current, candidate)
            return store.update(
                model,
                entity_id,
                patch.changes,
                expected_revision=(
                    current.revision
                    if patch.expected_revision is None
                    else patch.expected_revision
                ),
            )

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
