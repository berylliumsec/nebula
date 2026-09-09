"""Operator reset: scope, durable retries, stale evidence and atomic rollback."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.application_model.graph import GraphReset, GraphTransaction
from nebula.v3.application_model.reset import preview, reset
from nebula.v3.application_model.service import ApplicationModelService
from nebula.v3.domain import Engagement, Observation, utc_now
from nebula.v3.database import EntityRow
from nebula.v3.storage import NebulaStore


@pytest.fixture
def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Reset this project"))
    other = store.create(Engagement(name="Keep this project"))

    def observation(project_id, source):
        return store.create(
            Observation(
                engagement_id=project_id,
                title="Capture",
                observation_type="browser_capture",
                source=source,
            )
        )

    captured = observation(project.id, "browser_companion")
    kept = observation(project.id, "manual")
    other_capture = observation(other.id, "browser_companion")
    service = ApplicationModelService(store)
    transaction = GraphTransaction(
        expected_revision=0,
        idempotency_key="original",
        operations=[
            {
                "op": "put_object",
                "id": "page",
                "label": "Page",
                "classification": {"value": "Page"},
                "authentication_context": "anonymous",
            }
        ],
    )
    service.transact(project.id, transaction)
    request = GraphReset(
        expected_revision=1,
        idempotency_key="reset-one",
        confirmation="clear-model-and-browser-captures",
    )
    return (
        store,
        service,
        project.id,
        captured,
        kept,
        other_capture,
        transaction,
        request,
    )


def test_reset_scope_persistence_and_fresh_evidence(setup):
    store, service, project, captured, kept, other, transaction, request = setup
    assert preview(service, project) == dict(
        revision=1, objects=1, relationships=0, captures=1, custom_definitions=0
    )
    assert reset(service, project, request) == dict(revision=2, cleared_captures=1)
    reopened = ApplicationModelService(NebulaStore(store.database))
    assert reopened.workspace(project)["objects"] == []
    assert reopened.snapshot(project)["revision"] == 2
    assert store.get(Observation, kept.id).id == kept.id
    assert store.get(Observation, other.id).id == other.id
    assert reopened.evidence_list(project)["evidence"] == []
    for source in (captured, kept):
        with pytest.raises(HTTPException) as error:
            reopened.evidence(project, "observations", source.id)
        assert error.value.status_code == 404
    fresh = store.create(
        Observation(
            engagement_id=project,
            title="Fresh",
            observation_type="browser_capture",
            source="browser_companion",
        )
    )
    assert reopened.evidence(project, "observations", fresh.id)["id"] == fresh.id
    assert len(reopened.history(project)["edits"]) == 1
    with pytest.raises(HTTPException) as error:
        reopened.transact(project, transaction)
    assert error.value.status_code == 409


def test_retry_does_not_clear_new_captures_or_model(setup):
    store, service, project, _, _, _, _, request = setup
    result = reset(service, project, request)
    fresh = store.create(
        Observation(
            engagement_id=project,
            title="Fresh",
            observation_type="browser_capture",
            source="browser_companion",
        )
    )
    service.transact(
        project,
        GraphTransaction(
            expected_revision=2,
            idempotency_key="fresh",
            operations=[
                {
                    "op": "put_object",
                    "id": "new",
                    "label": "New",
                    "classification": {"value": "Page"},
                    "authentication_context": "anonymous",
                }
            ],
        ),
    )
    assert reset(service, project, request) == result
    assert store.get(Observation, fresh.id).id == fresh.id
    assert service.snapshot(project)["revision"] == 3
    assert len(service.workspace(project)["objects"]) == 1


def test_exact_capture_kinds_and_project_boundary(setup):
    store, service, project, _, _, other, _, request = setup
    cleared = (
        "browser_traffic",
        "browser_websocket_frames",
        "browser_repeater_results",
    )
    retained = (
        "evidence",
        "artifacts",
        "findings",
        "sessions",
        "browser_sessions",
        "browser_identities",
        "browser_actions",
        "browser_commands",
    )
    # Storage-level sentinels prove the deletion predicate cannot include any
    # neighbouring document kind or another project, irrespective of payload.
    with store.database.engine.begin() as connection:
        for owner in (project, other.engagement_id):
            for kind in (*cleared, *retained):
                connection.execute(
                    insert(EntityRow).values(
                        id=f"{owner}-{kind}",
                        kind=kind,
                        engagement_id=owner,
                        payload={},
                        created_at=utc_now(),
                        updated_at=utc_now(),
                    )
                )
    assert preview(service, project)["captures"] == 4
    assert reset(service, project, request)["cleared_captures"] == 4
    with store.database.engine.connect() as connection:
        ids = set(connection.execute(select(EntityRow.id)).scalars())
    for kind in cleared:
        assert f"{project}-{kind}" not in ids
    for kind in retained:
        assert f"{project}-{kind}" in ids
    for kind in (*cleared, *retained):
        assert f"{other.engagement_id}-{kind}" in ids


def test_conflict_and_key_reuse_leave_data_intact(setup):
    _, service, project, _, _, _, _, request = setup
    for bad in (
        request.model_copy(update={"expected_revision": 0}),
        request.model_copy(update={"idempotency_key": "original"}),
    ):
        with pytest.raises(HTTPException) as error:
            reset(service, project, bad)
        assert error.value.status_code == 409
        assert preview(service, project)["captures"] == 1
        assert preview(service, project)["objects"] == 1


def test_reset_rolls_back_deletions_on_database_failure(setup):
    store, service, project, _, _, _, _, request = setup
    from nebula.v3.application_model.persistence import graphs

    with store.database.engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TRIGGER fail_reset BEFORE UPDATE ON {graphs.name} BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
            )
        )
    with pytest.raises(Exception, match="test rollback"):
        reset(service, project, request)
    assert preview(service, project)["captures"] == 1
    assert preview(service, project)["objects"] == 1
    assert len(service.history(project)["edits"]) == 1


def test_reset_http_auth_confirmation_and_empty_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Empty"))
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    base = f"/api/v1/engagements/{project.id}/application-model"
    auth = {"Authorization": "Bearer test-token"}
    body = dict(
        expected_revision=0,
        idempotency_key="empty-reset",
        confirmation="clear-model-and-browser-captures",
    )
    with TestClient(app) as client:
        assert client.get(base + "/reset-preview").status_code == 401
        assert client.post(base + "/reset", json=body).status_code == 401
        assert (
            client.post(
                base + "/reset", headers=auth, json={**body, "confirmation": "yes"}
            ).status_code
            == 422
        )
        assert client.post(base + "/reset", headers=auth, json=body).json() == dict(
            revision=1, cleared_captures=0
        )
        assert client.post(base + "/reset", headers=auth, json=body).json() == dict(
            revision=1, cleared_captures=0
        )
        assert (
            client.get(
                "/api/v1/engagements/missing/application-model/reset-preview",
                headers=auth,
            ).status_code
            == 404
        )
