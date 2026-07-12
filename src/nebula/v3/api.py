"""Versioned FastAPI surface for the Nebula 3 core."""

from __future__ import annotations

import asyncio
import base64
import hmac
import secrets
from pathlib import Path
from typing import Any, Callable

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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError
from starlette.middleware.cors import CORSMiddleware

from .artifacts import ArtifactStore, ArtifactStoreError
from .database import Database
from .domain import (
    ENTITY_MODEL_BY_KIND,
    Approval,
    ApprovalStatus,
    Artifact,
    Entity,
    NebulaModel,
    ProviderProfile,
    RunEvent,
    utc_now,
)
from .providers import (
    PROVIDER_CATALOG,
    ProviderHealth,
    provider_from_profile,
)
from .pty import HumanPtyService
from .storage import ConflictError, NebulaStore, NotFoundError

READ_ONLY_RESOURCES = {
    "agent_attempts",
    "approvals",
    "artifacts",
    "runs",
    "source_snapshots",
    "tasks",
    "tool_calls",
}
APPEND_ONLY_RESOURCES = {"evidence"}

API_PREFIX = "/api/v1"


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

    app = FastAPI(
        title="Nebula 3 Core API",
        version="3.0.0-alpha.1",
        description="Local-first, UI-independent security engagement control plane.",
    )
    app.state.store = store
    app.state.artifact_store = artifact_store
    app.state.auth_token = token
    app.state.allow_unauthenticated = allow_unauthenticated
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

    @app.get(f"{API_PREFIX}/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": app.version,
            "mode": "local"
            if store.database.engine.dialect.name == "sqlite"
            else "team",
            # Runner health belongs to the separately configured worker. The
            # API never assumes that presence of a container CLI makes it safe.
            "runner": "unavailable",
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
        if approval.expires_at is not None and approval.expires_at <= utc_now():
            expired = store.update(
                Approval,
                approval.id,
                {
                    "status": ApprovalStatus.EXPIRED,
                    "decided_by": "system",
                    "decided_at": utc_now(),
                    "decision_note": "approval expired before an operator decision",
                },
                expected_revision=approval.revision,
            )
            store.append_event(
                approval.run_id,
                "approval.expired",
                {"approval_id": approval.id, "status": expired.status.value},
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
        changes: dict[str, Any] = {
            "status": status_by_decision[request.decision],
            "decided_by": "operator",
            "decided_at": utc_now(),
            "decision_note": request.reason,
        }
        if request.edited_arguments is not None:
            exact = dict(approval.exact_request)
            exact["arguments"] = request.edited_arguments
            changes["exact_request"] = exact
        updated = store.update(
            Approval,
            approval.id,
            changes,
            expected_revision=approval.revision,
        )
        store.append_event(
            approval.run_id,
            "approval.resolved",
            {
                "approval_id": approval.id,
                "status": updated.status.value,
                "decided_by": updated.decided_by,
            },
            actor_id=updated.decided_by,
            idempotency_key=f"approval:{approval.id}:resolved",
        )
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
        return store.overview(engagement_id)

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
        return await provider_from_profile(profile).health()

    @app.post(
        f"{API_PREFIX}/provider-health/refresh",
        response_model=list[ProviderHealth],
        tags=["providers"],
        dependencies=[Depends(require_auth)],
    )
    async def refresh_all_provider_health() -> list[ProviderHealth]:
        profiles = store.list_entities(ProviderProfile, limit=1000)
        return list(
            await asyncio.gather(
                *(provider_from_profile(profile).health() for profile in profiles)
            )
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
                for value in websocket.headers.get(
                    "sec-websocket-protocol", ""
                ).split(",")
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
            if supplied and protocol_token and not hmac.compare_digest(
                supplied, protocol_token
            ):
                await websocket.close(code=4401, reason="conflicting authentication tokens")
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
            )

    for resource, model in ENTITY_MODEL_BY_KIND.items():
        _register_crud_routes(
            app,
            store,
            require_auth,
            resource,
            model,
            read_only=resource in READ_ONLY_RESOURCES,
            append_only=resource in APPEND_ONLY_RESOURCES,
        )

    if static_dir is not None:
        frontend = Path(static_dir).expanduser().resolve()
        if not (frontend / "index.html").is_file():
            raise ValueError("static_dir must contain a built index.html")
        app.mount("/", StaticFiles(directory=frontend, html=True), name="workspace")

    return app
def _register_crud_routes(
    app: FastAPI,
    store: NebulaStore,
    require_auth: Callable[..., Any],
    resource: str,
    model: type[Entity],
    *,
    read_only: bool = False,
    append_only: bool = False,
) -> None:
    """Register typed routes while preserving concrete OpenAPI schemas."""

    def make_create() -> Callable[..., Any]:
        async def create_entity(entity: Any) -> Entity:
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
            return store.list_entities(
                model,
                engagement_id=engagement_id,
                offset=offset,
                limit=limit,
            )

        list_entities.__name__ = f"list_{resource.replace('-', '_')}"
        list_entities.__annotations__["return"] = list[model]  # type: ignore[valid-type]
        return list_entities

    def make_get() -> Callable[..., Any]:
        async def get_entity(entity_id: str) -> Entity:
            return store.get(model, entity_id)

        get_entity.__name__ = f"get_{resource.replace('-', '_')}"
        get_entity.__annotations__["return"] = model
        return get_entity

    def make_replace() -> Callable[..., Any]:
        async def replace_entity(
            entity_id: str,
            entity: Any,
            if_match: int | None = Header(default=None, alias="If-Match"),
        ) -> Entity:
            return store.replace(model, entity_id, entity, expected_revision=if_match)

        replace_entity.__name__ = f"replace_{resource.replace('-', '_')}"
        replace_entity.__annotations__["entity"] = model
        replace_entity.__annotations__["return"] = model
        return replace_entity

    def make_patch() -> Callable[..., Any]:
        async def patch_entity(entity_id: str, patch: PatchRequest) -> Entity:
            return store.update(
                model,
                entity_id,
                patch.changes,
                expected_revision=patch.expected_revision,
            )

        patch_entity.__name__ = f"patch_{resource.replace('-', '_')}"
        patch_entity.__annotations__["return"] = model
        return patch_entity

    def make_delete() -> Callable[..., Any]:
        async def delete_entity(entity_id: str) -> Response:
            store.delete(model, entity_id)
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
