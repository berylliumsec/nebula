"""Operator reset: scope, durable retries, stale evidence and atomic rollback."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest
from fastapi import HTTPException
from tests.v3.async_test_support import AsyncTestClient as TestClient
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
async def setup(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Reset this project"))
    other = await store.create(Engagement(name="Keep this project"))

    async def observation(project_id, source):
        return await store.create(
            Observation(
                engagement_id=project_id,
                title="Capture",
                observation_type="browser_capture",
                source=source,
            )
        )

    captured = await observation(project.id, "browser_companion")
    kept = await observation(project.id, "manual")
    other_capture = await observation(other.id, "browser_companion")
    service = ApplicationModelService(store)
    transaction = GraphTransaction(
        expected_revision=0,
        idempotency_key="original",
        operations=[
            {
                "op": "put_object",
                "id": "page",
                "properties": {
                    "purpose": {
                        "value": "Identify the entry point into the login workflow."
                    }
                },
                "label": "Page",
                "classification": {"value": "Page"},
                "authentication_context": "anonymous",
            }
        ],
    )
    (await service.transact(project.id, transaction))
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


async def test_reset_scope_persistence_and_fresh_evidence(setup):
    store, service, project, captured, kept, other, transaction, request = setup
    assert (await preview(service, project)) == dict(
        revision=1, objects=1, relationships=0, captures=1, custom_definitions=0
    )
    assert (await reset(service, project, request)) == dict(
        revision=2, cleared_captures=1
    )
    reopened = ApplicationModelService(NebulaStore(store.database))
    assert (await reopened.workspace(project))["objects"] == []
    assert (await reopened.snapshot(project))["revision"] == 2
    assert (await store.get(Observation, kept.id)).id == kept.id
    assert (await store.get(Observation, other.id)).id == other.id
    assert (await reopened.evidence_list(project))["evidence"] == []
    for source in (captured, kept):
        with pytest.raises(HTTPException) as error:
            (await reopened.evidence(project, "observations", source.id))
        assert error.value.status_code == 404
    fresh = await store.create(
        Observation(
            engagement_id=project,
            title="Fresh",
            observation_type="browser_capture",
            source="browser_companion",
        )
    )
    assert (await reopened.evidence(project, "observations", fresh.id))[
        "id"
    ] == fresh.id
    assert len((await reopened.history(project))["edits"]) == 1
    with pytest.raises(HTTPException) as error:
        (await reopened.transact(project, transaction))
    assert error.value.status_code == 409


async def test_retry_does_not_clear_new_captures_or_model(setup):
    store, service, project, _, _, _, _, request = setup
    result = await reset(service, project, request)
    fresh = await store.create(
        Observation(
            engagement_id=project,
            title="Fresh",
            observation_type="browser_capture",
            source="browser_companion",
        )
    )
    (
        await service.transact(
            project,
            GraphTransaction(
                expected_revision=2,
                idempotency_key="fresh",
                operations=[
                    {
                        "op": "put_object",
                        "id": "new",
                        "properties": {
                            "purpose": {
                                "value": "Identify the entry point into the fresh login workflow."
                            }
                        },
                        "label": "New",
                        "classification": {"value": "Page"},
                        "authentication_context": "anonymous",
                    }
                ],
            ),
        )
    )
    assert (await reset(service, project, request)) == result
    assert (await store.get(Observation, fresh.id)).id == fresh.id
    assert (await service.snapshot(project))["revision"] == 3
    assert len((await service.workspace(project))["objects"]) == 1


async def test_exact_capture_kinds_and_project_boundary(setup):
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
    async with store.database.engine.begin() as connection:
        for owner in (project, other.engagement_id):
            for kind in (*cleared, *retained):
                (
                    await connection.execute(
                        insert(EntityRow).values(
                            id=f"{owner}-{kind}",
                            kind=kind,
                            engagement_id=owner,
                            payload={},
                            created_at=utc_now(),
                            updated_at=utc_now(),
                        )
                    )
                )
    assert (await preview(service, project))["captures"] == 4
    assert (await reset(service, project, request))["cleared_captures"] == 4
    async with store.database.engine.connect() as connection:
        ids = set((await connection.execute(select(EntityRow.id))).scalars())
    for kind in cleared:
        assert f"{project}-{kind}" not in ids
    for kind in retained:
        assert f"{project}-{kind}" in ids
    for kind in (*cleared, *retained):
        assert f"{other.engagement_id}-{kind}" in ids


async def test_conflict_and_key_reuse_leave_data_intact(setup):
    _, service, project, _, _, _, _, request = setup
    for bad in (
        request.model_copy(update={"expected_revision": 0}),
        request.model_copy(update={"idempotency_key": "original"}),
    ):
        with pytest.raises(HTTPException) as error:
            (await reset(service, project, bad))
        assert error.value.status_code == 409
        assert (await preview(service, project))["captures"] == 1
        assert (await preview(service, project))["objects"] == 1


async def test_reset_rolls_back_deletions_on_database_failure(setup):
    store, service, project, _, _, _, _, request = setup
    from nebula.v3.application_model.persistence import graphs

    async with store.database.engine.begin() as connection:
        (
            await connection.execute(
                text(
                    f"CREATE TRIGGER fail_reset BEFORE UPDATE ON {graphs.name} BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
                )
            )
        )
    with pytest.raises(Exception, match="test rollback"):
        (await reset(service, project, request))
    assert (await preview(service, project))["captures"] == 1
    assert (await preview(service, project))["objects"] == 1
    assert len((await service.history(project))["edits"]) == 1


async def test_reset_http_auth_confirmation_and_empty_project(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Empty"))
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
    async with TestClient(app) as client:
        assert (await client.get(base + "/reset-preview")).status_code == 401
        assert (await client.post(base + "/reset", json=body)).status_code == 401
        assert (
            await client.post(
                base + "/reset", headers=auth, json={**body, "confirmation": "yes"}
            )
        ).status_code == 422
        assert (
            await client.post(base + "/reset", headers=auth, json=body)
        ).json() == dict(revision=1, cleared_captures=0)
        assert (
            await client.post(base + "/reset", headers=auth, json=body)
        ).json() == dict(revision=1, cleared_captures=0)
        assert (
            await client.get(
                "/api/v1/engagements/missing/application-model/reset-preview",
                headers=auth,
            )
        ).status_code == 404


pytestmark = pytest.mark.anyio
