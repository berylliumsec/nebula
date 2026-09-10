"""New objects need explicit explanatory value, without claiming semantic truth."""

import pytest
from sqlalchemy import insert
from nebula.v3.application_model.graph import GraphTransaction
from nebula.v3.application_model.persistence import graphs
from nebula.v3.application_model.service import ApplicationModelService, empty
from nebula.v3.domain import Engagement
from nebula.v3.storage import NebulaStore


@pytest.fixture
def model(tmp_path):
    store = NebulaStore(tmp_path / "model.db")
    project = store.create(Engagement(name="Admission"))
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
def test_missing_or_invalid_purpose_rolls_back_whole_batch(model, purpose):
    _, project, service = model
    with pytest.raises(ValueError):
        service.transact(
            project,
            transaction(
                0,
                object_op("login", "Explain the login entry point."),
                object_op("recipe", purpose),
            ),
        )
    assert service.snapshot(project)["revision"] == 0
    assert service.workspace(project)["objects"] == []
    assert service.history(project)["edits"] == []


def test_purpose_persists_and_does_not_upgrade_a_hypothesis(model):
    store, project, service = model
    purpose = "Explain the denied request; firewall versus application authorization remains uncertain."
    request = transaction(0, object_op("access", purpose, "AccessControl"))
    result = service.transact(project, request)
    assert service.transact(project, request) == result
    reopened = ApplicationModelService(NebulaStore(store.database))
    claim = reopened.workspace(project)["objects"][0]["properties"]["purpose"]
    assert claim["value"] == purpose
    assert claim["status"] == "hypothesized"
    assert claim["evidence"] == []
    with pytest.raises(ValueError, match="Explain what"):
        service.transact(
            project, transaction(1, object_op("access", " ", "AccessControl"))
        )


def test_existing_objects_remain_editable_without_backfill(model):
    store, project, service = model
    graph = empty(project)
    graph["revision"] = 1
    old = object_op("legacy")
    old.pop("op")
    graph["objects"]["legacy"] = old
    with store.database.engine.begin() as connection:
        connection.execute(
            insert(graphs).values(project_id=project, revision=1, payload=graph)
        )
    service.transact(project, transaction(1, object_op("legacy")))
    assert service.workspace(project)["objects"][0]["properties"] == {}
    with pytest.raises(ValueError, match="properties.purpose"):
        service.transact(project, transaction(2, object_op("new")))


def test_discovery_and_shared_instructions_explain_admission(model):
    _, project, service = model
    from nebula.v3.application_model.tools import DESCRIPTIONS
    from nebula.v3.application_model.workflow import BROWSER_MODEL_WORKFLOW

    for type in service.schema(project)["types"]:
        purpose = next(p for p in type["properties"] if p["name"] == "purpose")
        assert "Required for new objects" in purpose["description"]
    assert "properties.purpose" in DESCRIPTIONS["model.transact"]
    for example in (
        "OAuth",
        "API documentation",
        "forbidden",
        "JavaScript",
        "recipe",
        "make no edit",
        "not graph edges",
    ):
        assert example in BROWSER_MODEL_WORKFLOW
