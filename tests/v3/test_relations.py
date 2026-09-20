from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Asset,
    Engagement,
    Evidence,
    Finding,
    KnowledgeSource,
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
from sqlalchemy import func, select
from nebula.v3.database import ResourceRelationRow


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


def test_browser_exchange_is_a_resolvable_relation_endpoint(tmp_path):
    # The endpoint map named a kind no entity declares, so every exchange edge
    # was refused as "not found" although the exchange row existed.
    from nebula.v3.domain import ENTITY_MODEL_BY_KIND, BrowserTrafficExchange, Evidence
    from nebula.v3.relations import RESOURCE_ENTITY_KINDS

    assert set(RESOURCE_ENTITY_KINDS.values()) <= set(ENTITY_MODEL_BY_KIND)

    store = NebulaStore(tmp_path / "exchange-relations.db")
    project = store.create(Engagement(name="Exchanges"))
    evidence = store.create(
        Evidence(engagement_id=project.id, evidence_type="http", title="Proof")
    )
    exchange = store.create(
        BrowserTrafficExchange(
            engagement_id=project.id,
            session_id="session-1",
            tab_id="tab-1",
            identity_id="identity-1",
            method="GET",
            url="https://target.example/login",
            scope_state="in_scope",
            scope_policy_id="scope-1",
            scope_policy_revision=1,
        )
    )
    service = ResourceRelationService(store)

    relation = service.create(
        project.id,
        ResourceRelationCreate(
            source=_ref(project.id, ResourceKind.EVIDENCE, evidence),
            predicate=RelationPredicate.PRODUCED_BY,
            target=_ref(project.id, ResourceKind.BROWSER_EXCHANGE, exchange),
        ),
    )

    assert relation.target.kind is ResourceKind.BROWSER_EXCHANGE
    assert relation.target.id == exchange.id


def _edge_ids(client: TestClient, headers, project_id: str, kind: str, entity_id: str):
    response = client.get(
        f"/api/v1/projects/{project_id}/relations",
        headers=headers,
        params={"resource_kind": kind, "resource_id": entity_id},
    )
    assert response.status_code == 200
    return {
        (item["predicate"], item["source"]["id"], item["target"]["id"]): item["id"]
        for item in response.json()
    }


def _relation_client(store: NebulaStore, tmp_path) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="relation-token",
        )
    )
    return client, {"Authorization": "Bearer relation-token"}


def test_duplicate_legacy_array_ids_are_rejected_before_any_write(tmp_path):
    store = NebulaStore(tmp_path / "relation-duplicates.db")
    project = store.create(Engagement(name="Duplicate relations"))
    asset = store.create(Asset(engagement_id=project.id, name="Gateway"))
    client, headers = _relation_client(store, tmp_path)

    duplicate = client.post(
        "/api/v1/findings",
        headers=headers,
        json={
            "engagement_id": project.id,
            "title": "Issue",
            "asset_ids": [asset.id, asset.id],
        },
    )
    assert duplicate.status_code == 422
    assert "asset_ids" in duplicate.json()["detail"]
    assert asset.id in duplicate.json()["detail"]
    assert store.count(Finding) == 0

    finding = client.post(
        "/api/v1/findings",
        headers=headers,
        json={"engagement_id": project.id, "title": "Issue", "asset_ids": [asset.id]},
    ).json()
    patched = client.patch(
        f"/api/v1/findings/{finding['id']}",
        headers=headers,
        json={"expected_revision": 1, "changes": {"asset_ids": [asset.id, asset.id]}},
    )
    assert patched.status_code == 422
    assert "asset_ids" in patched.json()["detail"]
    stored = store.get(Finding, finding["id"])
    assert stored.revision == 1
    assert stored.asset_ids == [asset.id]
    assert set(_edge_ids(client, headers, project.id, "finding", finding["id"])) == {
        ("affects", finding["id"], asset.id)
    }

    report = client.post(
        "/api/v1/reports",
        headers=headers,
        json={
            "engagement_id": project.id,
            "title": "Report",
            "finding_ids": [finding["id"]],
        },
    ).json()
    body = dict(report)
    body["finding_ids"] = [finding["id"], finding["id"]]
    replaced = client.put(f"/api/v1/reports/{report['id']}", headers=headers, json=body)
    assert replaced.status_code == 422
    assert "finding_ids" in replaced.json()["detail"]
    assert store.get(Report, report["id"]).revision == 1


def test_legacy_edits_only_sync_the_arrays_the_request_changed(tmp_path):
    store = NebulaStore(tmp_path / "relation-delta.db")
    project = store.create(Engagement(name="Delta relations"))
    asset = store.create(Asset(engagement_id=project.id, name="Gateway"))
    other_asset = store.create(Asset(engagement_id=project.id, name="Bastion"))
    evidence = store.create(
        Evidence(engagement_id=project.id, evidence_type="capture", title="Proof")
    )
    client, headers = _relation_client(store, tmp_path)
    finding = client.post(
        "/api/v1/findings",
        headers=headers,
        json={"engagement_id": project.id, "title": "Issue", "asset_ids": [asset.id]},
    ).json()
    linked = client.post(
        f"/api/v1/projects/{project.id}/relations",
        headers=headers,
        json={
            "source": {"project_id": project.id, "kind": "evidence", "id": evidence.id},
            "predicate": "supports",
            "target": {
                "project_id": project.id,
                "kind": "finding",
                "id": finding["id"],
            },
        },
    )
    assert linked.status_code == 201
    affects = ("affects", finding["id"], asset.id)
    supports = ("supports", evidence.id, finding["id"])
    before = _edge_ids(client, headers, project.id, "finding", finding["id"])
    assert set(before) == {affects, supports}

    # A title-only patch must leave both edges untouched: same rows, same ids.
    renamed = client.patch(
        f"/api/v1/findings/{finding['id']}",
        headers=headers,
        json={"changes": {"title": "Renamed"}},
    )
    assert renamed.status_code == 200
    assert renamed.json()["asset_ids"] == [asset.id]
    assert renamed.json()["evidence_ids"] == [evidence.id]
    assert _edge_ids(client, headers, project.id, "finding", finding["id"]) == before

    # Patching one array reconciles only that array's edges.
    reassigned = client.patch(
        f"/api/v1/findings/{finding['id']}",
        headers=headers,
        json={"changes": {"asset_ids": [other_asset.id]}},
    )
    assert reassigned.status_code == 200
    moved = ("affects", finding["id"], other_asset.id)
    after = _edge_ids(client, headers, project.id, "finding", finding["id"])
    assert set(after) == {moved, supports}
    assert after[supports] == before[supports]

    # An edge deleted through the relations API stays deleted after the next edit.
    revision = next(
        item["revision"]
        for item in client.get(
            f"/api/v1/projects/{project.id}/relations",
            headers=headers,
            params={"resource_kind": "finding", "resource_id": finding["id"]},
        ).json()
        if item["id"] == after[supports]
    )
    deleted = client.delete(
        f"/api/v1/projects/{project.id}/relations/{after[supports]}",
        headers=headers,
        params={"expected_revision": revision},
    )
    assert deleted.status_code == 204
    renamed = client.patch(
        f"/api/v1/findings/{finding['id']}",
        headers=headers,
        json={"changes": {"title": "Renamed twice"}},
    )
    assert renamed.status_code == 200
    assert renamed.json()["evidence_ids"] == []
    assert _edge_ids(client, headers, project.id, "finding", finding["id"]) == {
        moved: after[moved]
    }

    # A GET -> PUT round trip with a title change keeps the edge rows as well.
    body = client.get(f"/api/v1/findings/{finding['id']}", headers=headers).json()
    body["title"] = "Replaced"
    replaced = client.put(
        f"/api/v1/findings/{finding['id']}", headers=headers, json=body
    )
    assert replaced.status_code == 200
    assert replaced.json()["asset_ids"] == [other_asset.id]
    assert replaced.json()["evidence_ids"] == []
    assert _edge_ids(client, headers, project.id, "finding", finding["id"]) == {
        moved: after[moved]
    }


def test_legacy_projection_pages_past_the_relation_list_cap(tmp_path):
    store = NebulaStore(tmp_path / "relation-paging.db")
    project = store.create(Engagement(name="Large report"))
    finding_ids = [
        store.create(Finding(engagement_id=project.id, title=f"Finding {index}")).id
        for index in range(505)
    ]
    client, headers = _relation_client(store, tmp_path)
    created = client.post(
        "/api/v1/reports",
        headers=headers,
        json={
            "engagement_id": project.id,
            "title": "Everything",
            "finding_ids": finding_ids,
        },
    )
    assert created.status_code == 201
    report = created.json()

    loaded = client.get(f"/api/v1/reports/{report['id']}", headers=headers)
    assert loaded.status_code == 200
    assert sorted(loaded.json()["finding_ids"]) == sorted(finding_ids)
    listed = client.get(f"/api/v1/reports?engagement_id={project.id}", headers=headers)
    assert listed.status_code == 200
    assert sorted(listed.json()[0]["finding_ids"]) == sorted(finding_ids)

    # A GET -> PUT round trip must not shed the inclusions past the list cap.
    body = dict(loaded.json())
    body["title"] = "Everything, retitled"
    replaced = client.put(f"/api/v1/reports/{report['id']}", headers=headers, json=body)
    assert replaced.status_code == 200
    assert sorted(replaced.json()["finding_ids"]) == sorted(finding_ids)
    with store.database.session() as session:
        edge_count = session.scalar(
            select(func.count())
            .select_from(ResourceRelationRow)
            .where(ResourceRelationRow.source_id == report["id"])
        )
    assert edge_count == 505
    reloaded = client.get(f"/api/v1/reports/{report['id']}", headers=headers)
    assert sorted(reloaded.json()["finding_ids"]) == sorted(finding_ids)


def test_note_edits_keep_source_references_the_arrays_do_not_carry(tmp_path):
    store = NebulaStore(tmp_path / "relation-note.db")
    project = store.create(Engagement(name="Note relations"))
    evidence = store.create(
        Evidence(engagement_id=project.id, evidence_type="capture", title="Proof")
    )
    source = store.create(
        KnowledgeSource(engagement_id=project.id, name="RoE", source_type="pdf")
    )
    client, headers = _relation_client(store, tmp_path)
    note = client.post(
        "/api/v1/observations",
        headers=headers,
        json={
            "engagement_id": project.id,
            "observation_type": "note",
            "title": "Note",
            "evidence_ids": [evidence.id],
        },
    ).json()
    referenced = client.post(
        f"/api/v1/projects/{project.id}/relations",
        headers=headers,
        json={
            "source": {"project_id": project.id, "kind": "note", "id": note["id"]},
            "predicate": "references",
            "target": {"project_id": project.id, "kind": "source", "id": source.id},
        },
    )
    assert referenced.status_code == 201
    before = _edge_ids(client, headers, project.id, "note", note["id"])
    assert set(before) == {
        ("references", note["id"], evidence.id),
        ("references", note["id"], source.id),
    }

    renamed = client.patch(
        f"/api/v1/observations/{note['id']}",
        headers=headers,
        json={"changes": {"evidence_ids": []}},
    )
    assert renamed.status_code == 200
    assert renamed.json()["evidence_ids"] == []
    assert _edge_ids(client, headers, project.id, "note", note["id"]) == {
        ("references", note["id"], source.id): before[
            ("references", note["id"], source.id)
        ]
    }
