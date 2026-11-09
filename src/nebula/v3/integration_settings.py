"""Shared integration schemas and read projections for the authenticated host.

SQL owns saved configuration. The caller supplies the host's canonical credential
store and search runtime for observations; these helpers never install, start,
test, resolve, or change either dependency.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Literal, Protocol, cast, runtime_checkable

from fastapi import HTTPException
from pydantic import Field, SecretStr
from sqlalchemy import func, select

from .credentials import CredentialStatus
from .database import EntityRow
from .domain import (
    NebulaModel,
    ScopePolicy,
    ToolSuggestionTest,
    VpnProfile,
    WebSearchEngine,
    WebSearchRuntimeState,
    WebSearchTest,
)
from .storage import NebulaStore
from .tool_suggestions import ENV_KEY, KeySource
from .tool_suggestions import load_settings as load_typesafe_settings
from .vault_probe import VaultState
from .web_search import load_settings as load_web_search_settings

MAX_VPN_PROFILES = 1000
MAX_VPN_CATALOG_BYTES = 512 * 1024


class TypeSafeKeyRequest(NebulaModel):
    secret: SecretStr
    persistence: Literal["vault", "session"] = "vault"


class TypeSafeIntegrationStatus(NebulaModel):
    """Public view of the Jev key: never the key or its reference."""

    source: Literal["vault", "session", "environment"] | None = None
    available: bool = False
    vault_available: bool = False
    vault_state: VaultState = "unavailable"
    last_test: ToolSuggestionTest | None = None
    projects_using: int = Field(default=0, ge=0)


class WebSearchStatus(NebulaModel):
    """Public view of the local search runtime; it holds no secret."""

    runtime_available: bool = False
    runtime_detail: str = ""
    container_state: WebSearchRuntimeState = WebSearchRuntimeState.ABSENT
    image_digest: str | None = None
    port: int | None = None
    engines: list[WebSearchEngine] = Field(default_factory=list)
    last_test: WebSearchTest | None = None
    last_detail: str | None = None
    projects_using: int = Field(default=0, ge=0)


class WebSearchEngineRequest(NebulaModel):
    engines: list[WebSearchEngine] = Field(min_length=1, max_length=16)


class VpnProfileCreateRequest(NebulaModel):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\]+\.ovpn$")
    config: str = Field(min_length=1, max_length=15_000)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=4_096)
    persistence: Literal["vault", "session"] = "vault"


class VpnProfileDeleteRequest(NebulaModel):
    expected_revision: int = Field(ge=1)


class CredentialObservation(Protocol):
    def status(self, reference: str) -> CredentialStatus: ...

    @property
    def vault_state(self) -> VaultState: ...


@runtime_checkable
class SearchRuntimeObservation(Protocol):
    async def state(self) -> tuple[WebSearchRuntimeState, str]: ...


async def typesafe_integration_status(
    store: NebulaStore, credentials: CredentialObservation
) -> TypeSafeIntegrationStatus:
    """Combine saved opt-ins with a read of the canonical host key status."""
    settings = await load_typesafe_settings(store)
    statement = (
        select(func.count())
        .select_from(EntityRow)
        .where(
            EntityRow.kind == ScopePolicy.entity_kind,
            EntityRow.payload["tool_suggestions"].as_boolean().is_(True),
            func.coalesce(EntityRow.payload["local_only"].as_boolean(), False).is_(
                False
            ),
        )
    )
    async with store.database.session() as session:
        projects_using = int((await session.scalar(statement)) or 0)

    def observe() -> tuple[KeySource | None, bool, VaultState]:
        source: KeySource | None = None
        available = False
        if settings is not None and settings.secret_ref:
            status = credentials.status(settings.secret_ref)
            source, available = cast(KeySource, status.persistence), status.available
        elif os.environ.get(ENV_KEY, "").strip():
            source, available = "environment", True
        return source, available, credentials.vault_state

    # Vault status can open D-Bus connections. It must not block the API loop.
    source, available, vault_state = await asyncio.to_thread(observe)
    return TypeSafeIntegrationStatus(
        source=source,
        available=available,
        vault_available=vault_state == "available",
        vault_state=vault_state,
        last_test=settings.last_test if settings else None,
        projects_using=projects_using,
    )


async def web_search_integration_status(
    store: NebulaStore, search_runtime: SearchRuntimeObservation | None
) -> WebSearchStatus:
    """Report a supplied host observation, keeping saved metadata authoritative."""
    projects_using = await store.web_search_project_count()
    settings = await load_web_search_settings(store)
    if search_runtime is None:
        return WebSearchStatus(
            runtime_available=False,
            runtime_detail="Core has no container runtime configured",
            projects_using=projects_using,
            engines=settings.engines if settings else [],
            last_test=settings.last_test if settings else None,
        )
    state, detail = await search_runtime.state()
    return WebSearchStatus(
        runtime_available=state != WebSearchRuntimeState.ABSENT
        or "not created" in detail,
        runtime_detail=detail,
        container_state=state,
        image_digest=settings.image_digest if settings else None,
        port=settings.port if settings else None,
        engines=settings.engines if settings else [],
        last_test=settings.last_test if settings else None,
        last_detail=settings.last_detail if settings else None,
        projects_using=projects_using,
    )


async def public_vpn_profiles(
    store: NebulaStore,
    credentials: CredentialObservation,
    *,
    offset: int = 0,
    limit: int = MAX_VPN_PROFILES,
) -> tuple[list[dict[str, Any]], int | None]:
    """Return a bounded redacted page and its explicit next offset, if any."""
    if offset < 0 or not 1 <= limit <= MAX_VPN_PROFILES:
        raise ValueError("VPN catalog bounds are invalid")
    profiles: list[VpnProfile] = []
    budget = MAX_VPN_CATALOG_BYTES
    # Metadata is extensible, so check each envelope before loading another.
    while len(profiles) < limit:
        batch = await store.list_entities(
            VpnProfile, offset=offset + len(profiles), limit=1
        )
        if not batch:
            break
        profile = batch[0]
        budget -= len(profile.model_dump_json(exclude={"secret_ref"}).encode())
        if budget < 0:
            raise HTTPException(413, "VPN catalog is too large; read a smaller page.")
        profiles.append(profile)
    more = bool(
        await store.list_entities(VpnProfile, offset=offset + len(profiles), limit=1)
    )

    def observe() -> list[dict[str, Any]]:
        return [
            {
                **profile.model_dump(mode="json", exclude={"secret_ref"}),
                "available": credentials.status(profile.secret_ref).available,
            }
            for profile in profiles
        ]

    values = await asyncio.to_thread(observe)
    if len(json.dumps(values, ensure_ascii=False).encode()) > MAX_VPN_CATALOG_BYTES:
        raise HTTPException(413, "VPN catalog is too large; read a smaller page.")
    return values, offset + len(profiles) if more else None
