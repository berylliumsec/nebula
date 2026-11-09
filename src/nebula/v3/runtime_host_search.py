"""Bounded public search through the sole host owner and an exact project lease."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import Field, ValidationError, model_validator

from .database import EntityRow
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .domain import Engagement, NebulaModel, RiskClass, ScopePolicy, WebSearchEngine
from .domain import WebSearchSettings
from .execution_scope import current_execution_lease
from .memory_policy import (
    MemoryBudgetConflict,
    MemoryEnforcementEvidence,
    MemoryEnforcementUnavailable,
)
from .policy import PolicyEngine, PolicyRequest
from .redaction import redact_text
from .runtime_coordination import CoordinationConflict, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .shared_memory_rpc import MemoryAuthority
from .storage import NotFoundError
from .tools import WEB_SEARCH_TOOL_NAME
from .web_search import (
    DEFAULT_RESULTS,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    SETTINGS_ID,
    Freshness,
    SearchRuntimeUnavailable,
    WebSearchError,
    WebSearchResult,
    scope_disclosure_conflict,
    web_search_enabled,
)

HOST_SEARCH_QUERY = "host.web-search.query/v1"


class SearchArguments(NebulaModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS, repr=False)
    count: int = Field(default=DEFAULT_RESULTS, ge=1, le=MAX_RESULTS, strict=True)
    freshness: Freshness = "any"


class HostSearchRequest(SearchArguments):
    lease: MemoryAuthority
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_revision: int = Field(ge=1, strict=True)


class SearchMemoryEvidence(MemoryEnforcementEvidence):
    auxiliary_group_ids: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=8
    )


class HostSearchFailure(NebulaModel):
    detail: str = Field(min_length=1, max_length=1_000)
    memory_job_id: str | None = Field(default=None, max_length=200)
    memory_state: str | None = Field(default=None, max_length=80)
    memory_outcome: str | None = Field(default=None, max_length=80)
    memory_enforcement: SearchMemoryEvidence | None = None


class HostSearchResponse(NebulaModel):
    lease: MemoryAuthority
    scope_policy_id: str = Field(min_length=1, max_length=200)
    scope_revision: int = Field(ge=1, strict=True)
    results: list[WebSearchResult] = Field(default_factory=list, max_length=MAX_RESULTS)
    failure: HostSearchFailure | None = None

    @model_validator(mode="after")
    def one_outcome(self):
        if self.failure is not None and self.results:
            raise ValueError("Search failure cannot contain results")
        for index, result in enumerate(self.results, start=1):
            url = urlsplit(result.url)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("Search results require a public-web URL")
            if result.rank != index:
                raise ValueError("Search results must have consecutive ranks")
        return self


class RemoteSearchUnavailable(SearchRuntimeUnavailable):
    def __init__(self, failure: HostSearchFailure):
        super().__init__(failure.detail)
        self.memory_job_id = failure.memory_job_id
        self.memory_state = failure.memory_state
        self.memory_outcome = failure.memory_outcome
        self.memory_enforcement = (
            failure.memory_enforcement.model_dump(mode="json")
            if failure.memory_enforcement is not None
            else None
        )


def _allow_query(scope: ScopePolicy, query: str) -> None:
    if not web_search_enabled(scope):
        raise ValueError("Public web search is disabled for this project")
    decision = PolicyEngine().evaluate(
        scope,
        PolicyRequest(
            tool_name=WEB_SEARCH_TOOL_NAME,
            risk_class=RiskClass.LOCAL_READ,
            cloud_transfer=True,
        ),
    )
    if not decision.allowed:
        raise ValueError(decision.reason)
    if scope_disclosure_conflict(query, scope) is not None:
        raise ValueError("Search query would disclose an in-scope target")


async def _authorize(session, request: HostSearchRequest) -> tuple[str, ...]:
    project_row = await session.get(EntityRow, request.lease.project_id)
    if project_row is None or project_row.kind != Engagement.entity_kind:
        raise ValueError("Project is unavailable")
    project = Engagement.model_validate(project_row.payload)
    if project.scope_policy_id != request.scope_policy_id:
        raise ValueError("Project scope changed")
    scope_row = await session.get(EntityRow, request.scope_policy_id)
    if scope_row is None or scope_row.kind != ScopePolicy.entity_kind:
        raise ValueError("Project scope is unavailable")
    scope = ScopePolicy.model_validate(scope_row.payload)
    if (
        scope.engagement_id != request.lease.project_id
        or scope.revision != request.scope_revision
    ):
        raise ValueError("Project scope changed")
    _allow_query(scope, request.query)
    settings_row = await session.get(EntityRow, SETTINGS_ID)
    if settings_row is None:
        engines = list(WebSearchEngine)[:3]
    elif settings_row.kind == WebSearchSettings.entity_kind:
        engines = WebSearchSettings.model_validate(settings_row.payload).engines
    else:
        raise ValueError("Host search settings are unavailable")
    return tuple(engine.value for engine in engines)


def _failure(error: Exception) -> HostSearchFailure:
    values = {
        "detail": redact_text(str(error))[:1_000] or "Host search is unavailable",
    }
    for name, bound in (
        ("memory_job_id", 200),
        ("memory_state", 80),
        ("memory_outcome", 80),
    ):
        value = getattr(error, name, None)
        if isinstance(value, str):
            values[name] = value[:bound]
    evidence = getattr(error, "memory_enforcement", None)
    if isinstance(evidence, dict):
        try:
            values["memory_enforcement"] = SearchMemoryEvidence.model_validate(evidence)
        except ValidationError:
            # diagnostic-expected: malformed optional evidence is never forwarded.
            pass
    return HostSearchFailure.model_validate(values)


def register_host_search(rpc, runtime_id, coordination, search_runtime):
    """Bind a worker incarnation to the host's existing query service only."""

    async def query(payload):
        try:
            request = HostSearchRequest.model_validate(payload)
            lease = ProjectLease(**request.lease.model_dump())
            if (
                lease.runtime_id != runtime_id
                or lease.project_id == GLOBAL_EXECUTION_PROJECT
            ):
                raise ValueError("Search requires this worker's project grant")
            async with coordination.fenced(lease) as session:
                engines = await _authorize(session, request)

            # Admission, provisioning and HTTP may wait. Never hold the SQL
            # ownership fence through those operations, or create a local owner.
            results, failure = [], None
            try:
                results = await search_runtime.search(
                    request.query, count=request.count, freshness=request.freshness
                )
            except (
                WebSearchError,
                MemoryBudgetConflict,
                MemoryEnforcementUnavailable,
            ) as error:
                failure = _failure(error)

            async with coordination.fenced(lease) as session:
                if await _authorize(session, request) != engines:
                    raise ValueError("Host search engines changed during the query")
                response = HostSearchResponse(
                    lease=request.lease,
                    scope_policy_id=request.scope_policy_id,
                    scope_revision=request.scope_revision,
                    results=results,
                    failure=failure,
                )
                if len(response.results) > request.count:
                    raise ValueError("Host search exceeded the requested result bound")
                return response.model_dump(mode="json")
        except (ValueError, CoordinationConflict):
            # No query, target, service details or another project's state leaks
            # through an unauthorised or stale grant.
            raise HTTPException(
                403, "Host search is unavailable for this project grant or scope"
            ) from None

    rpc.register(HOST_SEARCH_QUERY, query)


class RemoteProjectSearch:
    """Query-only project adapter; it cannot provision, adopt or stop a service."""

    def __init__(self, client, store, lease: ProjectLease, *, owns_client=False):
        self.client, self.store, self.lease = client, store, lease
        self.owns_client = owns_client

    async def search(
        self, query: str, *, count: int = DEFAULT_RESULTS, freshness: Freshness = "any"
    ) -> list[WebSearchResult]:
        if current_execution_lease() != self.lease:
            raise SearchRuntimeUnavailable(
                "Search requires the current project execution grant"
            )
        try:
            arguments = SearchArguments(query=query, count=count, freshness=freshness)
            project = await self.store.get(Engagement, self.lease.project_id)
            if project.scope_policy_id is None:
                raise ValueError("Project scope is unavailable")
            scope = await self.store.get(ScopePolicy, project.scope_policy_id)
            if scope.engagement_id != self.lease.project_id:
                raise ValueError("Project scope belongs to another project")
            _allow_query(scope, arguments.query)
            request = HostSearchRequest(
                **arguments.model_dump(),
                lease=MemoryAuthority(**asdict(self.lease)),
                scope_policy_id=scope.id,
                scope_revision=scope.revision,
            )
        except (ValueError, NotFoundError) as error:
            raise SearchRuntimeUnavailable(
                "Search is unavailable under the current project policy"
            ) from error
        try:
            raw = await self.client.call(HOST_SEARCH_QUERY, request.model_dump())
            response = HostSearchResponse.model_validate(raw)
            if (
                response.lease.model_dump() != asdict(self.lease)
                or response.scope_policy_id != request.scope_policy_id
                or response.scope_revision != request.scope_revision
                or len(response.results) > request.count
            ):
                raise ValueError("Host search returned a different grant or scope")
        except (httpx.TransportError, RuntimeIpcError, ValueError) as error:
            raise SearchRuntimeUnavailable(
                "The host search service could not answer. Retry after the host is ready."
            ) from error
        if response.failure is not None:
            raise RemoteSearchUnavailable(response.failure)
        # Defense in depth for the model-facing boundary, even for a signed peer.
        return [
            result.model_copy(
                update={
                    "title": redact_text(result.title)[:MAX_TITLE_CHARS],
                    "snippet": redact_text(result.snippet)[:MAX_SNIPPET_CHARS],
                }
            )
            for result in response.results
        ]

    async def close(self) -> None:
        if self.owns_client:
            await self.client.close()
