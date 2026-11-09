"""Shared validated entity routes; no execution lifecycle or producer construction."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import Field

from .async_utils import MaybeAsync, call_async
from .api_validation import ApiEntityValidator
from .domain import (
    AutomationProjectPolicy,
    ChatSession,
    Engagement,
    Entity,
    HarnessProfile,
    HarnessSession,
    HarnessWorkspaceAccess,
    KnowledgeSource,
    McpServerProfile,
    MissionGrant,
    NebulaModel,
    Observation,
    ProviderProfile,
    ProviderVerificationStatus,
    Report,
    ReportStatus,
    ScopePolicy,
    ToolCall,
    utc_now,
)
from .knowledge import knowledge_summary
from .relations import LEGACY_RELATION_MODELS, ResourceRelationService
from .setup import create_engagement_with_default_scope
from .storage import ConflictError, NebulaStore
from .tool_results import without_results_api_key

API_PREFIX = "/api/v1"


class PatchRequest(NebulaModel):
    changes: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class ObservationReportDependency(NebulaModel):
    id: str
    title: str
    status: ReportStatus


class ObservationDependencies(NebulaModel):
    observation_id: str
    deletable: bool
    reports: list[ObservationReportDependency]


class StructuredConflictError(ConflictError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


async def _observation_dependencies(
    store: NebulaStore, observation_id: str
) -> ObservationDependencies:
    observation = await store.get(Observation, observation_id)
    reports: list[ObservationReportDependency] = []
    reports = [
        ObservationReportDependency(
            id=report.id, title=report.title, status=report.status
        )
        async for report in store.iter_reference_candidates(
            Report,
            [("observation_ids", observation_id, True)],
            engagement_id=observation.engagement_id,
        )
    ]
    reports.sort(key=lambda report: (report.status != ReportStatus.FINAL, report.title))
    return ObservationDependencies(
        observation_id=observation_id,
        deletable=not reports,
        reports=reports,
    )


class EngagementCreateRequest(Engagement):
    """Creation-only consent and retry key; neither is persisted on the project."""

    host_access_acknowledged: bool = False
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


def _public_entity(entity: Entity) -> Entity:
    """``entity`` as the generic read routes return it.

    A background command's tool call recorded before the callback key left
    its receipt still holds the key at rest; it is never returned.
    """

    if isinstance(entity, ToolCall):
        result = without_results_api_key(entity.result)
        if result is not entity.result:
            return entity.model_copy(update={"result": result})
    return entity


def _resolve_engagement_workspace_path(workspace_path: str) -> str:
    """Return the canonical folder a project links, or reject an unusable one.

    Creation stores this resolved form so a later workspace read never resolves
    a relative or missing path against Core's working directory. Generic
    replacement and patch cannot change a project's folder.
    """

    try:
        linked_workspace = Path(workspace_path).expanduser().resolve(strict=True)
    except OSError as exc:  # diagnostic-expected: the 422 below tells the operator the folder is unusable.
        raise HTTPException(
            status_code=422,
            detail="project workspace folder does not exist or is inaccessible",
        ) from exc
    if not linked_workspace.is_dir() or linked_workspace == Path("/"):
        raise HTTPException(
            status_code=422,
            detail="project workspace must be an existing non-root folder",
        )
    return str(linked_workspace)


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


def _invalidate_harness_home_verification(
    current: HarnessProfile, candidate: HarnessProfile
) -> HarnessProfile:
    if current.home_directory == candidate.home_directory:
        return candidate
    return candidate.model_copy(update={"capabilities": type(candidate.capabilities)()})


def _attribute_scope_grants(
    saved: list[MissionGrant], stored: list[MissionGrant], operator_id: str
) -> list[MissionGrant]:
    """Credit each grant in a saved scope to the operator who authorized it.

    A saved grant with the same authority as a stored grant that no earlier
    saved grant claimed is that grant saved again, and keeps the stored
    ``granted_by`` and ``granted_at``. Grants carry no id, so among stored
    grants of equal authority the one whose attribution the client echoed is
    claimed first; the echo only chooses between stored attributions. Any
    other grant is new or changed, and is credited to ``operator_id`` at the
    save time. What the client names as granter or grant time is never kept.
    """

    now = utc_now()
    unclaimed = list(stored)
    attributed: list[MissionGrant] = []
    for position, grant in enumerate(saved, start=1):
        matches = [
            index
            for index, existing in enumerate(unclaimed)
            if existing.authority() == grant.authority()
        ]
        echoed = [
            index
            for index in matches
            if unclaimed[index].granted_by == grant.granted_by
            and unclaimed[index].granted_at == grant.granted_at
        ]
        if matches:
            existing = unclaimed.pop((echoed or matches)[0])
            attributed.append(
                grant.model_copy(
                    update={
                        "granted_by": existing.granted_by,
                        "granted_at": existing.granted_at,
                    }
                )
            )
            continue
        if grant.expires_at <= now:
            raise ValueError(
                f"grant {position} expired at {grant.expires_at.isoformat()}; "
                "a new or changed grant must expire after it is saved. "
                "Set a later expiry or remove the grant."
            )
        attributed.append(
            grant.model_copy(update={"granted_by": operator_id, "granted_at": now})
        )
    return attributed


def _invalidate_provider_verification(
    current: ProviderProfile,
    candidate: ProviderProfile,
) -> ProviderProfile:
    """Fail closed when any compatibility-sensitive provider field changes."""

    changed = _provider_contract_fingerprint(current) != _provider_contract_fingerprint(
        candidate
    )
    old_options = current.metadata.get("options") or {}
    new_options = candidate.metadata.get("options") or {}
    routing_changed = (
        current.provider_type == "openrouter"
        and candidate.provider_type == "openrouter"
        and any(
            old_options.get(key) != new_options.get(key)
            for key in ("openrouter_providers", "openrouter_zdr")
        )
    )
    metadata = dict(candidate.metadata)
    if routing_changed:
        # The cached model catalog and endpoint limits describe the previous
        # routing policy. Refreshing health will rebuild the eligible catalog.
        for key in (
            "model_descriptors",
            "model_catalog_revision",
            "route_catalog_revision",
        ):
            metadata.pop(key, None)
    verifications = {} if changed else current.capability_verifications
    has_verified_model = any(
        item.status == ProviderVerificationStatus.VERIFIED
        and item.contract_version == "required-tool-v1"
        for item in verifications.values()
    )
    return candidate.model_copy(
        update={
            "metadata": metadata,
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


def _register_crud_routes(
    app: FastAPI,
    store: NebulaStore,
    relation_service: ResourceRelationService,
    require_auth: Callable[..., Any],
    entity_validator: ApiEntityValidator,
    resource: str,
    model: type[Entity],
    *,
    listing_capacity: Callable[..., Any],
    read_only: bool = False,
    append_only: bool = False,
    after_create: Callable[[Entity], Any] | None = None,
    after_write: Callable[[str], Any] | None = None,
    delete_archived_project: Callable[[Engagement], Any] | None = None,
    actor_id: MaybeAsync[[], str],
) -> None:
    """Register typed routes while preserving concrete OpenAPI schemas.

    ``actor_id`` names the operator a write is attributed to, as the
    dedicated routes attribute theirs.
    """

    async def attribute_scope_grants(entity: Any, current: Any = None) -> Any:
        # A grant records who authorized it. These routes take the client's
        # whole record, so, as the project scope route does, Core keeps the
        # stored attribution of each grant saved unchanged and credits new or
        # changed grants to the operator writing them, instead of accepting
        # whoever the client names.
        if not isinstance(entity, ScopePolicy):
            return entity
        stored = current.grants if isinstance(current, ScopePolicy) else []
        return entity.model_copy(
            update={
                "grants": _attribute_scope_grants(
                    entity.grants, stored, await call_async(actor_id)
                )
            }
        )

    def enforce_harness_command_boundary(entity: Any) -> Any:
        if not isinstance(entity, HarnessProfile):
            return entity
        return entity.model_copy(
            update={
                "native_capabilities": entity.native_capabilities.model_copy(
                    update={
                        "workspace_access": HarnessWorkspaceAccess.NONE,
                        "shell": False,
                    }
                )
            }
        )

    def make_create() -> Callable[..., Any]:
        async def create_entity(entity: Any) -> Entity:
            protected = {"id", "created_at", "updated_at", "revision"}.intersection(
                entity.model_fields_set
            )
            if protected:
                raise ValueError(
                    f"cannot set server-managed fields: {sorted(protected)}"
                )
            host_access_acknowledged = False
            project_request_id = None
            if isinstance(entity, EngagementCreateRequest):
                host_access_acknowledged = entity.host_access_acknowledged
                project_request_id = entity.request_id
                entity = Engagement.model_validate(
                    entity.model_dump(
                        exclude={"host_access_acknowledged", "request_id"}
                    )
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
            entity = enforce_harness_command_boundary(entity)
            entity = await attribute_scope_grants(entity)
            if isinstance(entity, Engagement) and entity.workspace_path:
                entity = entity.model_copy(
                    update={
                        "workspace_path": _resolve_engagement_workspace_path(
                            entity.workspace_path
                        )
                    }
                )
            (await entity_validator.validate_create(entity))
            created = (
                (
                    await create_engagement_with_default_scope(
                        store,
                        entity,
                        host_access_acknowledged=host_access_acknowledged,
                        actor_id=await call_async(actor_id),
                        request_id=project_request_id,
                    )
                )
                if isinstance(entity, Engagement)
                else (await relation_service.create_legacy_entity(entity))
                if isinstance(entity, LEGACY_RELATION_MODELS)
                else (await store.create(entity))
            )
            if after_create is not None:
                after_create(created)
            if after_write is not None:
                after_write(created.id)
            return created

        create_entity.__name__ = f"create_{resource.replace('-', '_')}"
        create_entity.__annotations__ = {
            "entity": EngagementCreateRequest if model is Engagement else model,
            "return": model,
        }
        return create_entity

    def make_list() -> Callable[..., Any]:
        # Keep all synchronous reads and enrichment in FastAPI's bounded AnyIO
        # worker pool. Sessions are opened and closed by storage in that worker.
        async def list_entities(
            engagement_id: str | None = None,
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=100, ge=1, le=1000),
            recent_first: bool = False,
        ) -> list[Entity]:
            entities = await store.list_entities(
                model,
                engagement_id=engagement_id,
                offset=offset,
                limit=limit,
                recent_first=recent_first and model is ChatSession,
            )
            if model is ChatSession:
                legacy_ids = await store.butler_recovery_session_ids(
                    item.id
                    for item in entities
                    if isinstance(item, ChatSession)
                    and item.metadata.get("butler_initiated") is not True
                )
                entities = [
                    item.model_copy(
                        update={"metadata": {**item.metadata, "butler_initiated": True}}
                    )
                    if isinstance(item, ChatSession) and item.id in legacy_ids
                    else item
                    for item in entities
                ]
            if model is HarnessSession and entities:
                (
                    chat_titles,
                    run_titles,
                    first_turns,
                ) = await store.harness_session_title_sources(
                    [item.id for item in entities], engagement_id=engagement_id
                )

                named_sessions: list[Entity] = []
                for entity in entities:
                    if not isinstance(entity, HarnessSession):
                        continue
                    first_turn = first_turns.get(entity.id)
                    turn_title = None
                    if first_turn is not None:
                        turn_title = (
                            first_turn.response
                            if first_turn.prompt.startswith(
                                "Name this conversation from its first exchange"
                            )
                            else first_turn.prompt
                        )
                    display_name = (
                        chat_titles.get(entity.id)
                        or run_titles.get(entity.id)
                        or turn_title
                    )
                    normalized_name = (
                        re.sub(r"\s+", " ", display_name).strip()[:160]
                        if isinstance(display_name, str)
                        else ""
                    )
                    named_sessions.append(
                        entity.model_copy(
                            update={
                                "display_name": normalized_name or "Unattached session"
                            }
                        )
                    )
                return named_sessions
            if model is KnowledgeSource:
                return [
                    knowledge_summary(entity)
                    for entity in entities
                    if isinstance(entity, KnowledgeSource)
                ]
            return [
                _public_entity(entity)
                for entity in (await relation_service.project_legacy_many(entities))
            ]

        list_entities.__name__ = f"list_{resource.replace('-', '_')}"
        list_entities.__annotations__["return"] = list[model]  # type: ignore[valid-type]
        return list_entities

    def make_get() -> Callable[..., Any]:
        async def get_entity(entity_id: str) -> Entity:
            entity = await store.get(model, entity_id)
            if (
                isinstance(entity, ChatSession)
                and entity.metadata.get("butler_initiated") is not True
                and entity.id in (await store.butler_recovery_session_ids([entity.id]))
            ):
                entity = entity.model_copy(
                    update={"metadata": {**entity.metadata, "butler_initiated": True}}
                )
            entity = (
                knowledge_summary(entity)
                if isinstance(entity, KnowledgeSource)
                else entity
            )
            return _public_entity((await relation_service.project_legacy(entity)))

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
            current = await store.get(model, entity_id)
            if if_match is not None and current.revision != if_match:
                raise ConflictError(
                    f"revision conflict: expected {if_match}, found {current.revision}"
                )
            (await entity_validator.validate_update(current, entity))
            if isinstance(current, ProviderProfile) and isinstance(
                entity, ProviderProfile
            ):
                entity = _invalidate_provider_verification(current, entity)
            elif isinstance(current, HarnessProfile) and isinstance(
                entity, HarnessProfile
            ):
                entity = _invalidate_harness_home_verification(current, entity)
            entity = enforce_harness_command_boundary(entity)
            entity = await attribute_scope_grants(entity, current)
            expected_revision = current.revision if if_match is None else if_match
            if isinstance(current, LEGACY_RELATION_MODELS):
                return await relation_service.replace_legacy_entity(
                    current, entity, expected_revision=expected_revision
                )
            saved = await store.replace(
                model, entity_id, entity, expected_revision=expected_revision
            )
            if after_write is not None:
                after_write(saved.id)
            return saved

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
            # Legacy relation arrays are edge projections: start the patch from
            # them so untouched arrays keep edges written via the relations API.
            current = await relation_service.project_legacy(
                (await store.get(model, entity_id))
            )
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
            candidate = enforce_harness_command_boundary(candidate)
            changes = dict(patch.changes)
            if isinstance(candidate, ScopePolicy) and "grants" in changes:
                # A patch that leaves the grants alone keeps who granted them.
                candidate = await attribute_scope_grants(candidate, current)
                changes["grants"] = [
                    grant.model_dump(mode="python") for grant in candidate.grants
                ]
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
            elif isinstance(current, HarnessProfile) and isinstance(
                candidate, HarnessProfile
            ):
                candidate = _invalidate_harness_home_verification(current, candidate)
                changes = {
                    key: value
                    for key, value in candidate.model_dump(mode="python").items()
                    if key not in {"id", "created_at", "updated_at", "revision"}
                    and value != getattr(current, key)
                }
            (await entity_validator.validate_update(current, candidate))
            expected_revision = (
                current.revision
                if patch.expected_revision is None
                else patch.expected_revision
            )
            if isinstance(current, LEGACY_RELATION_MODELS):
                return await relation_service.replace_legacy_entity(
                    current, candidate, expected_revision=expected_revision
                )
            saved = await store.update(
                model,
                entity_id,
                changes,
                expected_revision=expected_revision,
            )
            if after_write is not None:
                after_write(saved.id)
            return saved

        patch_entity.__name__ = f"patch_{resource.replace('-', '_')}"
        patch_entity.__annotations__["return"] = model
        return patch_entity

    def make_delete() -> Callable[..., Any]:
        async def delete_entity(
            entity_id: str,
            if_match: int | None = Header(default=None, alias="If-Match"),
        ) -> Response:
            current = await store.get(model, entity_id)
            if (
                isinstance(current, McpServerProfile)
                and current.metadata.get("built_in") == "notebook_v1"
            ):
                raise ConflictError(
                    "Notebook is bundled with this project; disable its MCP profile to revoke agent access"
                )
            if if_match is not None and current.revision != if_match:
                raise ConflictError(
                    f"revision conflict: expected {if_match}, found {current.revision}"
                )
            if model is Engagement:
                assert isinstance(current, Engagement)
                if current.status == "archived" and delete_archived_project is not None:
                    if if_match is None:
                        raise ConflictError(
                            "Refresh the project and supply its revision in If-Match before deleting."
                        )
                    await delete_archived_project(current)
                    return Response(status_code=204)
                owned_scope: ScopePolicy | None = None
                if current.scope_policy_id:
                    candidate = await store.get(ScopePolicy, current.scope_policy_id)
                    if candidate.engagement_id == current.id:
                        owned_scope = candidate
                owned_policies = list(
                    [
                        item
                        async for item in store.iter_reference_candidates(
                            AutomationProjectPolicy,
                            [("engagement_id", entity_id, False)],
                            engagement_id=entity_id,
                        )
                    ]
                )
                owned_configuration_ids = [policy.id for policy in owned_policies]
                if owned_scope is not None:
                    owned_configuration_ids.append(owned_scope.id)
                if await store.engagement_has_dependents(
                    entity_id,
                    exclude_entity_ids=owned_configuration_ids,
                ):
                    raise ConflictError(
                        "engagement cannot be deleted while owned entities exist; "
                        "archive it instead"
                    )
                if owned_scope is not None or owned_policies:
                    async with store.transaction() as transaction:
                        for policy in owned_policies:
                            (
                                await transaction.delete(
                                    AutomationProjectPolicy,
                                    policy.id,
                                    expected_revision=policy.revision,
                                )
                            )
                        if owned_scope is not None:
                            (
                                await transaction.delete(
                                    ScopePolicy,
                                    owned_scope.id,
                                    expected_revision=owned_scope.revision,
                                )
                            )
                        (
                            await transaction.delete(
                                Engagement,
                                current.id,
                                expected_revision=current.revision,
                            )
                        )
                    return Response(status_code=204)
            if model is ProviderProfile:
                if await store.provider_has_history_references(entity_id):
                    raise ConflictError(
                        "provider profile cannot be deleted while durable chat or run "
                        "history references it"
                    )
            if model is Observation:
                dependencies = await _observation_dependencies(store, entity_id)
                if dependencies.reports:
                    final_reports = [
                        report
                        for report in dependencies.reports
                        if report.status == ReportStatus.FINAL
                    ]
                    if final_reports:
                        names = ", ".join(
                            f"“{report.title}”" for report in final_reports[:3]
                        )
                        detail = (
                            f"This note is retained because final report {names} includes it. "
                            "Final reports are immutable."
                        )
                    else:
                        names = ", ".join(
                            f"“{report.title}”" for report in dependencies.reports[:3]
                        )
                        detail = (
                            f"Remove this note from report {names} before deleting it."
                        )
                    raise StructuredConflictError("note_referenced_by_report", detail)
            # The scan reads every kind, so keep it off the event loop.
            await entity_validator.validate_delete(current)
            # Always guard the final delete with the revision we validated so a
            # concurrent update cannot be removed using stale relationship data.
            (await store.delete(model, entity_id, expected_revision=current.revision))
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
        dependencies=[*dependencies, Depends(listing_capacity)],
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
