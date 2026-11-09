"""SQL-only Settings routes for the public runtime, without execution owners.

The caller authenticates the request and supplies its actor context. Mutations
join GlobalSqlDelivery's transaction; no route here resolves credentials,
contacts a provider, discovers an executable, or constructs a controller.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Response
from pydantic import Field, ValidationError
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from .database import EntityRow, MemoryBudgetPolicyRow, MemoryJobRow
from .domain import (
    NebulaModel,
    OperatorProfile,
    ProviderProfile,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
)
from .model_catalog import ModelDescriptor
from .operators import OperatorProfileService
from .storage import ConflictError, NebulaStore, NotFoundError

# These operations require an authenticated host service. They are deliberately
# not mounted on the atomic SQL app: a successful SQL commit cannot prove an
# external probe, credential operation, installation, or provider invocation.
GLOBAL_SETTINGS_HOST_ROUTES = (
    ("GET", r"/api/v1/providers/discover-local"),
    ("GET", r"/api/v1/providers/openrouter/upstream-providers"),
    ("POST", r"/api/v1/providers/[^/]+/health"),
    ("POST", r"/api/v1/provider-health/refresh"),
    ("POST", r"/api/v1/providers/[^/]+/capabilities/verify"),
    ("POST", r"/api/v1/providers/[^/]+/openrouter/credits/refresh"),
    ("POST", r"/api/v1/runner-profiles/[^/]+/probe"),
    ("POST", r"/api/v1/harnesses/[^/]+/health"),
)


def is_global_settings_host_route(method: str, path: str) -> bool:
    return any(
        method.upper() == verb and re.fullmatch(pattern, path) is not None
        for verb, pattern in GLOBAL_SETTINGS_HOST_ROUTES
    )


class OperatorCreate(NebulaModel):
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    role: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorUpdate(NebulaModel):
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    role: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class OperatorActivate(NebulaModel):
    expected_revision: int | None = Field(default=None, ge=1)


class RunnerSettingsRequest(NebulaModel):
    name: str = Field(min_length=1, max_length=200)
    runtime: RunnerRuntime
    executable: str
    context: str | None = Field(default=None, max_length=500)
    socket: str | None = Field(default=None, max_length=2048)
    platform: str = Field(pattern=r"^linux/(amd64|arm64)$")
    isolation: RunnerIsolation
    enabled: bool = True
    seccomp_profile: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class RunnerCreate(RunnerSettingsRequest):
    id: str | None = Field(default=None, min_length=1, max_length=200)


class JoinedOperatorProfiles(OperatorProfileService):
    """Retain legacy profile invariants on the current receipt transaction."""

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.store.transaction(immediate=True):
            session = self.store.database.task_session()
            if session is None:
                raise RuntimeError("Global settings require an owned SQL transaction")
            connection = await session.connection()
            if connection.dialect.name == "postgresql":
                # Match the legacy writer's empty-set lock. Merely locking
                # existing profile rows cannot serialize the first insertion.
                await connection.exec_driver_sql(
                    "LOCK TABLE entities IN SHARE ROW EXCLUSIVE MODE"
                )
            yield connection


_RUNNER_TRUST_FIELDS = (
    "runtime",
    "executable",
    "context",
    "socket",
    "platform",
    "isolation",
    "seccomp_profile",
)
_MAX_MODELS = 1000
_MAX_CATALOG_BYTES = 512 * 1024


def _stored_models(profile: ProviderProfile) -> tuple[list[dict[str, Any]], bool]:
    """Project stored display data only; never infer live health or authority."""
    raw = profile.metadata.get("model_descriptors", [])
    raw = raw if isinstance(raw, list) else []
    partial = len(raw) > _MAX_MODELS or len(profile.model_allowlist) > _MAX_MODELS
    models: dict[str, dict[str, Any]] = {}
    used = 0
    for value in raw[:_MAX_MODELS]:
        try:
            model = ModelDescriptor.model_validate(value)
        except ValidationError:
            # diagnostic-expected: invalid stored display metadata is omitted and explicitly marks the catalog partial.
            partial = True
            continue
        if not model.id or len(model.id) > 500:
            partial = True
            continue
        item = model.model_dump(mode="json")
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if size + used > _MAX_CATALOG_BYTES:
            partial = True
            continue
        if model.id not in models:
            models[model.id] = item
            used += size
    for identity in profile.model_allowlist[:_MAX_MODELS]:
        if identity in models:
            continue
        item = ModelDescriptor(id=identity, name=identity).model_dump(mode="json")
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if len(models) >= _MAX_MODELS or used + size > _MAX_CATALOG_BYTES:
            partial = True
            break
        models[identity] = item
        used += size
    return sorted(models.values(), key=lambda item: item["id"]), partial


async def _runner_referenced(store: NebulaStore, profile_id: str) -> bool:
    async with store.database.session() as session:
        for statement in (
            select(
                exists().where(
                    or_(
                        EntityRow.payload["runner_profile_id"].as_string()
                        == profile_id,
                        EntityRow.payload["runtime"]["runner_profile_id"].as_string()
                        == profile_id,
                    )
                )
            ),
            select(
                exists().where(
                    MemoryJobRow.snapshot["profile_id"].as_string() == profile_id
                )
            ),
            select(
                exists().where(
                    MemoryBudgetPolicyRow.policy["profile_overrides"][profile_id]
                    .as_string()
                    .is_not(None)
                )
            ),
        ):
            if await session.scalar(statement):
                return True
    return False


def mount_global_settings(
    app: FastAPI, store: NebulaStore, actor: ContextVar[str]
) -> None:
    """Mount before legacy read routes; pass the same GlobalSqlStore and actor."""
    operators = JoinedOperatorProfiles(store)

    def authenticated() -> str:
        identity = actor.get()
        if identity != "operator" and not identity.startswith("device:"):
            raise HTTPException(403, "An authenticated Core actor is required")
        return identity

    auth = [Depends(authenticated)]

    @app.get("/api/v1/operator-profiles", dependencies=auth)
    async def list_operators() -> list[OperatorProfile]:
        return await operators.list_profiles()

    @app.get("/api/v1/operator-profiles/active", dependencies=auth)
    async def active_operator() -> OperatorProfile:
        return await operators.active_profile()

    @app.get("/api/v1/operator-profiles/{profile_id}", dependencies=auth)
    async def get_operator(profile_id: str) -> OperatorProfile:
        return await operators.get_profile(profile_id)

    @app.post("/api/v1/operator-profiles", status_code=201, dependencies=auth)
    async def create_operator(body: OperatorCreate) -> OperatorProfile:
        return await operators.create_profile(**body.model_dump())

    @app.patch("/api/v1/operator-profiles/{profile_id}", dependencies=auth)
    async def update_operator(profile_id: str, body: OperatorUpdate) -> OperatorProfile:
        changes = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if changes.get("display_name", "present") is None:
            raise ValueError("display_name cannot be null")
        if changes.get("metadata", {}) is None:
            raise ValueError("metadata cannot be null")
        return await operators.update_profile(
            profile_id, changes, expected_revision=body.expected_revision
        )

    @app.post("/api/v1/operator-profiles/{profile_id}/activate", dependencies=auth)
    async def activate_operator(
        profile_id: str, body: OperatorActivate
    ) -> OperatorProfile:
        return await operators.activate_profile(
            profile_id, expected_revision=body.expected_revision
        )

    @app.delete(
        "/api/v1/operator-profiles/{profile_id}", status_code=204, dependencies=auth
    )
    async def delete_operator(
        profile_id: str, if_match: int | None = Header(default=None, alias="If-Match")
    ) -> Response:
        await operators.delete_profile(profile_id, expected_revision=if_match)
        return Response(status_code=204)

    @app.get("/api/v1/runner-profiles", dependencies=auth)
    async def list_runners(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=1000),
    ) -> list[RunnerProfile]:
        return await store.list_entities(RunnerProfile, offset=offset, limit=limit)

    @app.get("/api/v1/runner-profiles/{profile_id}", dependencies=auth)
    async def get_runner(profile_id: str) -> RunnerProfile:
        return await store.get(RunnerProfile, profile_id)

    @app.post("/api/v1/runner-profiles", status_code=201, dependencies=auth)
    async def create_runner(body: RunnerCreate) -> RunnerProfile:
        if body.expected_revision is not None:
            raise ValueError("A new runner cannot have an expected revision")
        payload = body.model_dump(exclude={"expected_revision", "id"})
        if body.id is not None:
            payload["id"] = body.id
        profile = RunnerProfile(
            **payload, last_health_detail="Saved; host verification is required."
        )
        async with store.transaction(immediate=True) as transaction:
            await transaction.add(profile)
            return profile

    @app.put("/api/v1/runner-profiles/{profile_id}", dependencies=auth)
    async def put_runner(
        body: RunnerSettingsRequest,
        profile_id: str = Path(min_length=1, max_length=200),
    ) -> RunnerProfile:
        payload = body.model_dump(exclude={"expected_revision"})
        candidate = RunnerProfile(id=profile_id, **payload)
        async with store.transaction(immediate=True) as transaction:
            try:
                current = await store.get(RunnerProfile, profile_id)
            except NotFoundError:
                if body.expected_revision is not None:
                    raise ConflictError(
                        "Runner profile changed; read it again"
                    ) from None
                unverified = candidate.model_copy(
                    update={
                        "last_health_detail": "Saved; host verification is required."
                    }
                )
                await transaction.add(unverified)
                return unverified
            if (
                any(
                    getattr(current, field) != getattr(candidate, field)
                    for field in _RUNNER_TRUST_FIELDS
                )
                or not candidate.enabled
            ):
                payload.update(
                    healthy=False,
                    last_health_at=None,
                    last_health_detail="Saved; host verification is required."
                    if candidate.enabled
                    else "Runner profile is disabled.",
                )
            return await transaction.update(
                RunnerProfile,
                current.id,
                payload,
                expected_revision=body.expected_revision or current.revision,
            )

    @app.delete(
        "/api/v1/runner-profiles/{profile_id}", status_code=204, dependencies=auth
    )
    async def delete_runner(
        profile_id: str, if_match: int | None = Header(default=None, alias="If-Match")
    ) -> Response:
        async with store.transaction(immediate=True) as transaction:
            if await _runner_referenced(store, profile_id):
                raise ConflictError(
                    "Runner profile is still referenced by saved policy or work; disable it instead"
                )
            await transaction.delete(
                RunnerProfile, profile_id, expected_revision=if_match
            )
        return Response(status_code=204)

    @app.get("/api/v1/providers/{provider_id}/models", dependencies=auth)
    async def stored_models(
        provider_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        profile = await store.get(ProviderProfile, provider_id)
        items, partial = _stored_models(profile)
        end = offset + limit
        return {
            "provider_id": profile.id,
            "provider_revision": profile.revision,
            "source": "stored_profile",
            "live_probe": False,
            "items": items[offset:end],
            "next_offset": end if end < len(items) else None,
            "partial": partial,
        }
