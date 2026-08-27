from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Asset,
    Engagement,
    Finding,
    RelationPredicate,
    Report,
    ReportStatus,
    ResourceKind,
    ResourceRef,
    ResourceRelationCreate,
    ResourceRelationSet,
    utc_now,
)
from nebula.v3.relations import ResourceRelationService
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError


def _ref(project_id: str, kind: ResourceKind, entity) -> ResourceRef:
    return ResourceRef(
        project_id=project_id, kind=kind, id=entity.id, revision=entity.revision
    )


def test_relations_are_typed_reciprocal_unique_and_revision_guarded(tmp_path):
    store = NebulaStore(tmp_path / "relations.db")
    project = store.create(Engagement(name="Relations"))
    asset = store.create(Asset(engagement_id=project.id, name="Gateway"))
    finding = store.create(Finding(engagement_id=project.id, title="Issue"))
    service = ResourceRelationService(store)
    request = ResourceRelationCreate(
        source=_ref(project.id, ResourceKind.FINDING, finding),
        predicate=RelationPredicate.AFFECTS,
        target=_ref(project.id, ResourceKind.ASSET, asset),
        attribution="operator-1",
    )

    relation = service.create(project.id, request)
    reciprocal = service.list_relations(
        project.id, resource=_ref(project.id, ResourceKind.ASSET, asset)
    )
    assert [item.id for item in reciprocal] == [relation.id]
    with pytest.raises(ConflictError, match="already exists"):
        service.create(project.id, request)
    with pytest.raises(ConflictError, match="revision conflict"):
        service.delete(project.id, relation.id, expected_revision=2)


def test_relations_reject_cross_project_dangling_and_invalid_predicate(tmp_path):
    store = NebulaStore(tmp_path / "invalid-relations.db")
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    finding = store.create(Finding(engagement_id=first.id, title="Issue"))
    other_asset = store.create(Asset(engagement_id=second.id, name="Other"))
    service = ResourceRelationService(store)

    with pytest.raises(ValueError, match="another project"):
        service.create(
            first.id,
            ResourceRelationCreate(
                source=_ref(first.id, ResourceKind.FINDING, finding),
                predicate=RelationPredicate.AFFECTS,
                target=ResourceRef(
                    project_id=first.id,
                    kind=ResourceKind.ASSET,
                    id=other_asset.id,
                ),
            ),
        )
    with pytest.raises(NotFoundError, match="not found"):
        service.create(
            first.id,
            ResourceRelationCreate(
                source=_ref(first.id, ResourceKind.FINDING, finding),
                predicate=RelationPredicate.AFFECTS,
                target=ResourceRef(
                    project_id=first.id, kind=ResourceKind.ASSET, id="missing"
                ),
            ),
        )
    with pytest.raises(ValueError, match="does not accept"):
        service.create(
            first.id,
            ResourceRelationCreate(
                source=_ref(first.id, ResourceKind.FINDING, finding),
                predicate=RelationPredicate.SUPPORTS,
                target=ResourceRef(
                    project_id=first.id,
                    kind=ResourceKind.ASSET,
                    id=other_asset.id,
                ),
            ),
        )


def test_reconcile_is_atomic_and_final_report_edges_are_retained(tmp_path):
    store = NebulaStore(tmp_path / "reconcile.db")
    project = store.create(Engagement(name="Report relations"))
    finding = store.create(Finding(engagement_id=project.id, title="Issue"))
    report = store.create(Report(engagement_id=project.id, title="Report"))
    service = ResourceRelationService(store)
    relation = service.reconcile(
        ResourceRelationSet(
            project_id=project.id,
            source=_ref(project.id, ResourceKind.REPORT, report),
            predicate=RelationPredicate.INCLUDES,
            targets=[_ref(project.id, ResourceKind.FINDING, finding)],
            expected_source_revision=report.revision,
        )
    )[0]
    report = store.update(
        Report,
        report.id,
        {
            "status": ReportStatus.FINAL,
            "signed_off_by": "operator-1",
            "signed_off_at": utc_now(),
        },
        expected_revision=report.revision,
    )

    with pytest.raises(ConflictError, match="retained"):
        service.delete(project.id, relation.id, expected_revision=relation.revision)
    with pytest.raises(ConflictError, match="retained"):
        service.reconcile(
            ResourceRelationSet(
                project_id=project.id,
                source=_ref(project.id, ResourceKind.REPORT, report),
                predicate=RelationPredicate.INCLUDES,
                targets=[],
                expected_source_revision=report.revision,
            )
        )


def test_legacy_api_arrays_are_atomic_edge_projections(tmp_path):
    store = NebulaStore(tmp_path / "relation-api.db")
    project = store.create(Engagement(name="API relations"))
    asset = store.create(Asset(engagement_id=project.id, name="Gateway"))
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="relation-token",
        )
    )
    headers = {"Authorization": "Bearer relation-token"}

    created = client.post(
        "/api/v1/findings",
        headers=headers,
        json={
            "engagement_id": project.id,
            "title": "Issue",
            "asset_ids": [asset.id],
        },
    )
    assert created.status_code == 201
    finding = created.json()
    edges = client.get(
        f"/api/v1/projects/{project.id}/relations",
        headers=headers,
        params={"resource_kind": "finding", "resource_id": finding["id"]},
    )
    assert edges.status_code == 200
    assert [(item["predicate"], item["target"]["id"]) for item in edges.json()] == [
        ("affects", asset.id)
    ]

    relation = edges.json()[0]
    deleted = client.delete(
        f"/api/v1/projects/{project.id}/relations/{relation['id']}",
        headers=headers,
        params={"expected_revision": relation["revision"]},
    )
    assert deleted.status_code == 204
    projected = client.get(f"/api/v1/findings/{finding['id']}", headers=headers)
    assert projected.status_code == 200
    assert projected.json()["asset_ids"] == []
