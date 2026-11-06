"""New objects need explicit explanatory value, without claiming semantic truth."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest
from sqlalchemy import insert
from nebula.v3.application_model.graph import GraphTransaction
from nebula.v3.application_model.persistence import graphs
from nebula.v3.application_model.service import ApplicationModelService, empty
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore


@pytest.fixture
async def model(tmp_path):
    store = NebulaStore(tmp_path / "model.db")
    project = await store.create(Engagement(name="Admission"))
    return store, project.id, ApplicationModelService(store)


def object_op(identifier, purpose=None, type="Page"):
    return dict(
        op="put_object",
        id=identifier,
        label=identifier,
        authentication_context="anonymous",
        classification={"value": type},
        properties={} if purpose is None else {"purpose": {"value": purpose}},
    )


def transaction(revision, *ops):
    return GraphTransaction(
        expected_revision=revision,
        idempotency_key=f"admit-{revision}",
        operations=list(ops),
    )


@pytest.mark.parametrize("purpose", [None, "", " \n\t", False, 4])
async def test_missing_or_invalid_purpose_rolls_back_whole_batch(model, purpose):
    _, project, service = model
    with pytest.raises(ValueError):
        (
            await service.transact(
                project,
                transaction(
                    0,
                    object_op("login", "Explain the login entry point."),
                    object_op("recipe", purpose),
                ),
            )
        )
    assert (await service.snapshot(project))["revision"] == 0
    assert (await service.workspace(project))["objects"] == []
    assert (await service.history(project))["edits"] == []


async def test_purpose_persists_and_does_not_upgrade_a_hypothesis(model):
    store, project, service = model
    purpose = "Explain the denied request; firewall versus application authorization remains uncertain."
    request = transaction(0, object_op("access", purpose, "AccessControl"))
    result = await service.transact(project, request)
    assert (await service.transact(project, request)) == result
    reopened = ApplicationModelService(NebulaStore(store.database))
    claim = (await reopened.workspace(project))["objects"][0]["properties"]["purpose"]
    assert claim["value"] == purpose
    assert claim["status"] == "hypothesized"
    assert claim["evidence"] == []
    with pytest.raises(ValueError, match="Explain what"):
        (
            await service.transact(
                project, transaction(1, object_op("access", " ", "AccessControl"))
            )
        )


async def test_existing_objects_remain_editable_without_backfill(model):
    store, project, service = model
    graph = empty(project)
    graph["revision"] = 1
    old = object_op("legacy")
    old.pop("op")
    graph["objects"]["legacy"] = old
    async with store.database.engine.begin() as connection:
        (
            await connection.execute(
                insert(graphs).values(project_id=project, revision=1, payload=graph)
            )
        )
    (await service.transact(project, transaction(1, object_op("legacy"))))
    assert (await service.workspace(project))["objects"][0]["properties"] == {}
    with pytest.raises(ValueError, match="properties.purpose"):
        (await service.transact(project, transaction(2, object_op("new"))))


async def test_discovery_and_shared_instructions_explain_admission(model):
    _, project, service = model
    from nebula.v3.application_model.tools import DESCRIPTIONS
    from nebula.v3.application_model.workflow import BROWSER_MODEL_WORKFLOW

    for type in (await service.schema(project))["types"]:
        purpose = next(p for p in type["properties"] if p["name"] == "purpose")
        assert "Required for new objects" in purpose["description"]
    assert "evidence" in DESCRIPTIONS["model.transact"]
    assert "model.transact" in BROWSER_MODEL_WORKFLOW


pytestmark = pytest.mark.anyio
