"""Authoritative typed resource relations and reciprocal lineage queries."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError

from .database import EntityRow, ResourceRelationRow
from .domain import (
    Entity,
    Evidence,
    Finding,
    Observation,
    RelationPredicate,
    Report,
    ReportStatus,
    ResourceKind,
    ResourceRef,
    ResourceRelation,
    ResourceRelationCreate,
    ResourceRelationSet,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError


RESOURCE_ENTITY_KINDS: dict[ResourceKind, str] = {
    ResourceKind.PROJECT: "engagements",
    ResourceKind.CONVERSATION: "chat_sessions",
    ResourceKind.NOTE: "observations",
    ResourceKind.SOURCE: "knowledge",
    ResourceKind.LIBRARY_ITEM: "library_items",
    ResourceKind.ASSET: "assets",
    ResourceKind.EVIDENCE: "evidence",
    ResourceKind.FINDING: "findings",
    ResourceKind.REPORT: "reports",
    ResourceKind.TERMINAL_COMMAND: "command_executions",
    ResourceKind.BROWSER_SESSION: "browser_sessions",
    ResourceKind.BROWSER_ASSESSMENT: "browser_assessments",
    ResourceKind.BROWSER_EXCHANGE: "browser_traffic_exchanges",
    ResourceKind.MISSION: "runs",
    ResourceKind.TERMINAL_SESSION: "automation_sessions",
    ResourceKind.RECEIPT: "action_intents",
    ResourceKind.EXECUTION: "operator_executions",
    ResourceKind.APPROVAL: "approvals",
    ResourceKind.ARTIFACT: "artifacts",
}

VALID_ENDPOINTS: dict[RelationPredicate, set[tuple[ResourceKind, ResourceKind]]] = {
    RelationPredicate.AFFECTS: {(ResourceKind.FINDING, ResourceKind.ASSET)},
    RelationPredicate.SUPPORTS: {(ResourceKind.EVIDENCE, ResourceKind.FINDING)},
    RelationPredicate.INCLUDES: {
        (ResourceKind.REPORT, ResourceKind.FINDING),
        (ResourceKind.REPORT, ResourceKind.NOTE),
    },
    RelationPredicate.REFERENCES: {
        (ResourceKind.NOTE, ResourceKind.EVIDENCE),
        (ResourceKind.NOTE, ResourceKind.SOURCE),
        (ResourceKind.CONVERSATION, ResourceKind.EVIDENCE),
        (ResourceKind.CONVERSATION, ResourceKind.SOURCE),
    },
    RelationPredicate.PRODUCED_BY: {
        (ResourceKind.EVIDENCE, ResourceKind.EXECUTION),
        (ResourceKind.EVIDENCE, ResourceKind.TERMINAL_COMMAND),
        (ResourceKind.EVIDENCE, ResourceKind.BROWSER_EXCHANGE),
    },
    RelationPredicate.DERIVED_FROM: {
        (ResourceKind.EVIDENCE, ResourceKind.EVIDENCE),
        (ResourceKind.EVIDENCE, ResourceKind.ARTIFACT),
    },
}

INVERSE_LABELS: dict[RelationPredicate, str] = {
    RelationPredicate.AFFECTS: "affected by",
    RelationPredicate.SUPPORTS: "supported by",
    RelationPredicate.INCLUDES: "included in",
    RelationPredicate.REFERENCES: "referenced by",
    RelationPredicate.PRODUCED_BY: "produced",
    RelationPredicate.DERIVED_FROM: "source of",
}

LEGACY_RELATION_MODELS = (Observation, Evidence, Finding, Report)


def _relation(row: ResourceRelationRow) -> ResourceRelation:
    return ResourceRelation(
        id=row.id,
        project_id=row.project_id,
        source=ResourceRef(
            project_id=row.project_id,
            kind=ResourceKind(row.source_kind),
            id=row.source_id,
            revision=row.source_revision,
        ),
        predicate=RelationPredicate(row.predicate),
        target=ResourceRef(
            project_id=row.project_id,
            kind=ResourceKind(row.target_kind),
            id=row.target_id,
            revision=row.target_revision,
        ),
        attribution=row.attribution,
        provenance=row.provenance,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ResourceRelationService:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    @staticmethod
    def _validate_shape(
        predicate: RelationPredicate, source: ResourceRef, target: ResourceRef
    ) -> None:
        if (source.kind, target.kind) not in VALID_ENDPOINTS[predicate]:
            raise ValueError(
                f"{predicate.value} does not accept {source.kind.value} -> {target.kind.value}"
            )

    @staticmethod
    def _validate_endpoint(session, ref: ResourceRef, project_id: str) -> EntityRow:
        expected_kind = RESOURCE_ENTITY_KINDS.get(ref.kind)
        if expected_kind is None:
            raise ValueError(f"{ref.kind.value} is not a durable relation endpoint")
        row = session.get(EntityRow, ref.id)
        if row is None or row.kind != expected_kind:
            raise NotFoundError(
                f"relation endpoint not found: {ref.kind.value}/{ref.id}"
            )
        if row.engagement_id != project_id:
            raise ValueError(f"relation endpoint {ref.id} belongs to another project")
        if ref.project_id != project_id:
            raise ValueError(f"relation reference {ref.id} has the wrong project")
        if ref.revision is not None and row.revision != ref.revision:
            raise ConflictError(
                f"revision conflict: expected {ref.revision}, found {row.revision}"
            )
        return row

    def create(
        self, project_id: str, request: ResourceRelationCreate
    ) -> ResourceRelation:
        self._validate_shape(request.predicate, request.source, request.target)
        with self.store.database.session() as session:
            source = self._validate_endpoint(session, request.source, project_id)
            target = self._validate_endpoint(session, request.target, project_id)
            now = utc_now()
            row = ResourceRelationRow(
                id=str(uuid4()),
                project_id=project_id,
                source_kind=request.source.kind.value,
                source_id=request.source.id,
                source_revision=source.revision,
                predicate=request.predicate.value,
                target_kind=request.target.kind.value,
                target_id=request.target.id,
                target_revision=target.revision,
                attribution=request.attribution,
                provenance=request.provenance,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                # diagnostic-expected: the unique edge constraint is the durable
                # duplicate-prevention authority exposed as a revision conflict.
                raise ConflictError("resource relation already exists") from exc
            return _relation(row)

    def list_relations(
        self,
        project_id: str,
        *,
        resource: ResourceRef | None = None,
        predicate: RelationPredicate | None = None,
        limit: int = 200,
    ) -> list[ResourceRelation]:
        statement = select(ResourceRelationRow).where(
            ResourceRelationRow.project_id == project_id
        )
        if resource is not None:
            if resource.project_id != project_id:
                raise ValueError("relation query reference has the wrong project")
            statement = statement.where(
                or_(
                    (ResourceRelationRow.source_kind == resource.kind.value)
                    & (ResourceRelationRow.source_id == resource.id),
                    (ResourceRelationRow.target_kind == resource.kind.value)
                    & (ResourceRelationRow.target_id == resource.id),
                )
            )
        if predicate is not None:
            statement = statement.where(
                ResourceRelationRow.predicate == predicate.value
            )
        statement = statement.order_by(
            ResourceRelationRow.created_at, ResourceRelationRow.id
        ).limit(min(max(limit, 1), 500))
        with self.store.database.session() as session:
            return [_relation(row) for row in session.scalars(statement)]

    def delete(self, project_id: str, relation_id: str, expected_revision: int) -> None:
        with self.store.database.session() as session:
            row = session.get(ResourceRelationRow, relation_id)
            if row is None or row.project_id != project_id:
                raise NotFoundError(f"resource relation not found: {relation_id}")
            if row.revision != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, found {row.revision}"
                )
            if (
                row.predicate == RelationPredicate.INCLUDES.value
                and row.source_kind == ResourceKind.REPORT.value
            ):
                report = session.get(EntityRow, row.source_id)
                if (
                    report is not None
                    and report.payload.get("status") == ReportStatus.FINAL.value
                ):
                    raise ConflictError("final report relations are retained")
            session.delete(row)

    def reconcile(self, request: ResourceRelationSet) -> list[ResourceRelation]:
        desired = {(target.kind.value, target.id): target for target in request.targets}
        if len(desired) != len(request.targets):
            raise ValueError("relation targets must be unique")
        with self.store.database.session() as session:
            source = self._validate_endpoint(
                session, request.source, request.project_id
            )
            if (
                request.expected_source_revision is not None
                and source.revision != request.expected_source_revision
            ):
                raise ConflictError(
                    f"revision conflict: expected {request.expected_source_revision}, found {source.revision}"
                )
            for target in request.targets:
                self._validate_shape(request.predicate, request.source, target)
                self._validate_endpoint(session, target, request.project_id)
            existing = list(
                session.scalars(
                    select(ResourceRelationRow).where(
                        ResourceRelationRow.project_id == request.project_id,
                        ResourceRelationRow.source_kind == request.source.kind.value,
                        ResourceRelationRow.source_id == request.source.id,
                        ResourceRelationRow.predicate == request.predicate.value,
                    )
                )
            )
            for row in existing:
                if (row.target_kind, row.target_id) not in desired:
                    if (
                        request.source.kind == ResourceKind.REPORT
                        and source.payload.get("status") == ReportStatus.FINAL.value
                    ):
                        raise ConflictError("final report relations are retained")
                    session.delete(row)
            present = {(row.target_kind, row.target_id) for row in existing}
            now = utc_now()
            for target in request.targets:
                if (target.kind.value, target.id) in present:
                    continue
                target_row = session.get(EntityRow, target.id)
                session.add(
                    ResourceRelationRow(
                        id=str(uuid4()),
                        project_id=request.project_id,
                        source_kind=request.source.kind.value,
                        source_id=request.source.id,
                        source_revision=source.revision,
                        predicate=request.predicate.value,
                        target_kind=target.kind.value,
                        target_id=target.id,
                        target_revision=target_row.revision if target_row else None,
                        attribution=request.attribution,
                        provenance=request.provenance,
                        revision=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.flush()
            return [
                _relation(row)
                for row in session.scalars(
                    select(ResourceRelationRow)
                    .where(
                        ResourceRelationRow.project_id == request.project_id,
                        ResourceRelationRow.source_kind == request.source.kind.value,
                        ResourceRelationRow.source_id == request.source.id,
                        ResourceRelationRow.predicate == request.predicate.value,
                    )
                    .order_by(ResourceRelationRow.created_at, ResourceRelationRow.id)
                )
            ]

    @staticmethod
    def _controlled_relation_filter(entity: Entity):
        project_id = getattr(entity, "engagement_id")
        if isinstance(entity, Finding):
            return or_(
                (ResourceRelationRow.project_id == project_id)
                & (ResourceRelationRow.source_kind == ResourceKind.FINDING.value)
                & (ResourceRelationRow.source_id == entity.id)
                & (ResourceRelationRow.predicate == RelationPredicate.AFFECTS.value),
                (ResourceRelationRow.project_id == project_id)
                & (ResourceRelationRow.target_kind == ResourceKind.FINDING.value)
                & (ResourceRelationRow.target_id == entity.id)
                & (ResourceRelationRow.predicate == RelationPredicate.SUPPORTS.value),
            )
        kind = (
            ResourceKind.NOTE
            if isinstance(entity, Observation)
            else ResourceKind.EVIDENCE
            if isinstance(entity, Evidence)
            else ResourceKind.REPORT
        )
        predicates = (
            [RelationPredicate.REFERENCES.value]
            if isinstance(entity, Observation)
            else [RelationPredicate.SUPPORTS.value]
            if isinstance(entity, Evidence)
            else [RelationPredicate.INCLUDES.value]
        )
        return (
            (ResourceRelationRow.project_id == project_id)
            & (ResourceRelationRow.source_kind == kind.value)
            & (ResourceRelationRow.source_id == entity.id)
            & (ResourceRelationRow.predicate.in_(predicates))
        )

    @staticmethod
    def _desired_legacy_edges(
        entity: Entity,
    ) -> list[tuple[ResourceKind, str, RelationPredicate, ResourceKind, str]]:
        if isinstance(entity, Finding):
            return [
                (
                    ResourceKind.FINDING,
                    entity.id,
                    RelationPredicate.AFFECTS,
                    ResourceKind.ASSET,
                    item,
                )
                for item in entity.asset_ids
            ] + [
                (
                    ResourceKind.EVIDENCE,
                    item,
                    RelationPredicate.SUPPORTS,
                    ResourceKind.FINDING,
                    entity.id,
                )
                for item in entity.evidence_ids
            ]
        if isinstance(entity, Evidence):
            return (
                [
                    (
                        ResourceKind.EVIDENCE,
                        entity.id,
                        RelationPredicate.SUPPORTS,
                        ResourceKind.FINDING,
                        entity.finding_id,
                    )
                ]
                if entity.finding_id
                else []
            )
        if isinstance(entity, Observation):
            return [
                (
                    ResourceKind.NOTE,
                    entity.id,
                    RelationPredicate.REFERENCES,
                    ResourceKind.EVIDENCE,
                    item,
                )
                for item in entity.evidence_ids
            ]
        if isinstance(entity, Report):
            return [
                (
                    ResourceKind.REPORT,
                    entity.id,
                    RelationPredicate.INCLUDES,
                    ResourceKind.FINDING,
                    item,
                )
                for item in entity.finding_ids
            ] + [
                (
                    ResourceKind.REPORT,
                    entity.id,
                    RelationPredicate.INCLUDES,
                    ResourceKind.NOTE,
                    item,
                )
                for item in entity.observation_ids
            ]
        return []

    def _sync_legacy_edges(self, session, entity: Entity) -> None:
        session.execute(
            delete(ResourceRelationRow).where(self._controlled_relation_filter(entity))
        )
        project_id = getattr(entity, "engagement_id")
        now = utc_now()
        for (
            source_kind,
            source_id,
            predicate,
            target_kind,
            target_id,
        ) in self._desired_legacy_edges(entity):
            source = self._validate_endpoint(
                session,
                ResourceRef(project_id=project_id, kind=source_kind, id=source_id),
                project_id,
            )
            target = self._validate_endpoint(
                session,
                ResourceRef(project_id=project_id, kind=target_kind, id=target_id),
                project_id,
            )
            session.add(
                ResourceRelationRow(
                    id=str(uuid4()),
                    project_id=project_id,
                    source_kind=source_kind.value,
                    source_id=source_id,
                    source_revision=source.revision,
                    predicate=predicate.value,
                    target_kind=target_kind.value,
                    target_id=target_id,
                    target_revision=target.revision,
                    attribution="legacy-api",
                    provenance={"source": "legacy-array-write"},
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()

    def create_legacy_entity(self, entity: Entity) -> Entity:
        """Atomically create a compatibility entity and its authoritative edges."""

        with self.store.database.session() as session:
            row = EntityRow(
                id=entity.id,
                kind=entity.entity_kind,
                engagement_id=getattr(entity, "engagement_id"),
                revision=entity.revision,
                payload=entity.model_dump(mode="json"),
                created_at=entity.created_at,
                updated_at=entity.updated_at,
            )
            session.add(row)
            try:
                session.flush()
                self._sync_legacy_edges(session, entity)
            except IntegrityError as exc:
                # diagnostic-expected: legacy create is one atomic conflict domain.
                raise ConflictError(f"entity already exists: {entity.id}") from exc
            return entity

    def replace_legacy_entity(
        self, current: Entity, candidate: Entity, *, expected_revision: int
    ) -> Entity:
        """Atomically replace legacy fields and reconcile their edge projection."""

        with self.store.database.session() as session:
            row = session.get(EntityRow, current.id)
            if row is None or row.kind != current.entity_kind:
                raise NotFoundError(
                    f"{current.entity_kind} entity not found: {current.id}"
                )
            if row.revision != expected_revision:
                raise ConflictError(
                    f"revision conflict: expected {expected_revision}, found {row.revision}"
                )
            updated = candidate.model_copy(
                update={
                    "id": current.id,
                    "created_at": current.created_at,
                    "updated_at": utc_now(),
                    "revision": current.revision + 1,
                }
            )
            row.payload = updated.model_dump(mode="json")
            row.revision = updated.revision
            row.updated_at = updated.updated_at
            row.engagement_id = getattr(updated, "engagement_id")
            self._sync_legacy_edges(session, updated)
            return updated

    def project_legacy(self, entity: Entity) -> Entity:
        """Project compatibility arrays from edges so they cannot be stale authority."""

        if not isinstance(entity, LEGACY_RELATION_MODELS):
            return entity
        project_id = getattr(entity, "engagement_id")
        relations = self.list_relations(
            project_id,
            resource=ResourceRef(
                project_id=project_id,
                kind=ResourceKind.NOTE
                if isinstance(entity, Observation)
                else ResourceKind.EVIDENCE
                if isinstance(entity, Evidence)
                else ResourceKind.FINDING
                if isinstance(entity, Finding)
                else ResourceKind.REPORT,
                id=entity.id,
            ),
            limit=500,
        )
        changes: dict[str, object] = {}
        if isinstance(entity, Finding):
            changes["asset_ids"] = [
                item.target.id
                for item in relations
                if item.predicate == RelationPredicate.AFFECTS
                and item.source.id == entity.id
            ]
            changes["evidence_ids"] = [
                item.source.id
                for item in relations
                if item.predicate == RelationPredicate.SUPPORTS
                and item.target.id == entity.id
            ]
        elif isinstance(entity, Evidence):
            changes["finding_id"] = next(
                (
                    item.target.id
                    for item in relations
                    if item.predicate == RelationPredicate.SUPPORTS
                    and item.source.id == entity.id
                ),
                None,
            )
        elif isinstance(entity, Observation):
            changes["evidence_ids"] = [
                item.target.id
                for item in relations
                if item.predicate == RelationPredicate.REFERENCES
                and item.source.id == entity.id
                and item.target.kind == ResourceKind.EVIDENCE
            ]
        elif isinstance(entity, Report):
            changes["finding_ids"] = [
                item.target.id
                for item in relations
                if item.predicate == RelationPredicate.INCLUDES
                and item.source.id == entity.id
                and item.target.kind == ResourceKind.FINDING
            ]
            changes["observation_ids"] = [
                item.target.id
                for item in relations
                if item.predicate == RelationPredicate.INCLUDES
                and item.source.id == entity.id
                and item.target.kind == ResourceKind.NOTE
            ]
        return entity.model_copy(update=changes)
