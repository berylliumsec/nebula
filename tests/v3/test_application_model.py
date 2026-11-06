"""Project graph durability, provenance, concurrency and migration boundaries."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from copy import deepcopy
import importlib
from pathlib import Path

import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, inspect

from nebula.v3.storage import NebulaStore
from nebula.v3.domain import Engagement, Observation
from nebula.v3.database import EntityRow
from nebula.v3.application_model.service import ApplicationModelService
from nebula.v3.application_model.graph import GraphTransaction


@pytest.fixture
async def fixture(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Site A"))
    evidence = await store.create(
        Observation(
            engagement_id=project.id,
            title="Recorded response",
            observation_type="browser_capture",
            source="browser_companion",
            metadata={
                "browser_session_id": "browser-a",
                "status": "complete",
                "url": "http://site-a.test/login?secret=x",
            },
        )
    )
    return store, project.id, evidence


def obj(identifier="page", type="Page", **kwargs):
    properties = kwargs.pop("properties", {})
    if type != "Asset":
        properties = {
            "purpose": {
                "value": f"Explain the responsibility of {identifier} in the application workflow."
            },
            **properties,
        }
    return dict(
        op="put_object",
        id=identifier,
        label=identifier,
        classification={"value": type},
        authentication_context="anonymous",
        properties=properties,
        **kwargs,
    )


def tx(revision, *operations, key=None):
    return GraphTransaction(
        expected_revision=revision,
        idempotency_key=key or f"edit-{revision}",
        operations=list(operations),
    )


def ref(evidence, role="supporting"):
    return dict(
        kind="observations", id=evidence.id, revision=evidence.revision, role=role
    )


async def test_transaction_persistence_claim_history_and_acceptance(fixture):
    store, project, evidence = fixture
    service = ApplicationModelService(store)
    op = obj(
        properties={
            "url": dict(
                value="http://site-a.test/login",
                status="observed",
                evidence=[ref(evidence)],
            )
        }
    )
    first = await service.transact(project, tx(0, op))
    assert (await service.transact(project, tx(0, op))) == first
    updated = deepcopy(op)
    updated["classification"].update(
        review="accepted", evidence=[ref(evidence, "conflicting")]
    )
    (await service.transact(project, tx(1, updated)))
    graph = await ApplicationModelService(NebulaStore(store.database)).workspace(
        project
    )
    assert len(graph["objects"]) == 1
    assert graph["objects"][0]["classification"]["status"] == "hypothesized"
    assert graph["objects"][0]["properties"]["url"]["status"] == "observed"
    history = (await service.history(project))["edits"]
    assert len(history) == 2
    assert (
        history[1]["changes"][0]["before"]["classification"]["review"] == "unreviewed"
    )
    assert (
        graph["objects"][0]["classification"]["sources"][0]["context"][
            "browser_session_id"
        ]
        == "browser-a"
    )
    assert "secret=x" not in str(
        (await service.evidence(project, "observations", evidence.id))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"classification": {"value": "Page", "status": "observed"}},
        {"properties": {"missing": {"value": "x"}}},
        {"properties": {"url": {"value": "http://site-a.test/?token=secret"}}},
        {"properties": {"url": {"value": "Bearer sensitive"}}},
    ],
)
async def test_invalid_claims_roll_back(fixture, change):
    store, project, _ = fixture
    service = ApplicationModelService(store)
    bad = {**obj(), **change}
    with pytest.raises((ValueError, HTTPException)):
        (await service.transact(project, tx(0, obj("valid"), bad)))
    assert (await service.snapshot(project))["revision"] == 0
    assert not (await service.history(project))["edits"]


async def test_project_and_evidence_revision_isolation(fixture):
    store, project, evidence = fixture
    other = await store.create(Engagement(name="Other"))
    service = ApplicationModelService(store)
    operation = obj()
    operation["classification"]["evidence"] = [ref(evidence)]
    with pytest.raises(HTTPException) as failure:
        (await service.transact(other.id, tx(0, operation)))
    assert failure.value.status_code == 404
    with pytest.raises(HTTPException):
        (await service.evidence(other.id, "observations", evidence.id))
    operation["classification"]["evidence"][0]["revision"] += 1
    with pytest.raises(HTTPException) as failure:
        (await service.transact(project, tx(0, operation)))
    assert failure.value.status_code == 409


async def test_relationship_inheritance_and_tombstones(fixture):
    store, project, _ = fixture
    service = ApplicationModelService(store)
    relation = dict(
        op="put_relationship",
        id="query-edge",
        type="reads_from",
        source="query",
        target="db",
        claim={"value": True},
    )
    (
        await service.transact(
            project, tx(0, obj("query", "Operation"), obj("db", "Storage"), relation)
        )
    )
    assert len((await service.neighborhood(project, "query"))["objects"]) == 2
    (
        await service.transact(
            project,
            tx(
                1,
                dict(
                    op="dismiss", kind="object", id="db", reason="Unsupported topology"
                ),
            ),
        )
    )
    assert not (await service.snapshot(project))["relationships"]
    assert await store.list_entities(Observation, engagement_id=project)
    with pytest.raises(HTTPException):
        (await service.transact(project, tx(2, obj("db", "Storage"))))


async def test_context_identity_and_property_dismissal(fixture):
    store, project, _ = fixture
    service = ApplicationModelService(store)
    one = obj(
        properties={
            "url": {"value": "http://site-a.test/account"},
            "display_name": {"value": "Account"},
        }
    )
    (await service.transact(project, tx(0, one)))
    duplicate = {**one, "id": "other"}
    with pytest.raises(HTTPException):
        (await service.transact(project, tx(1, duplicate)))
    duplicate["authentication_context"] = "signed-in"
    (await service.transact(project, tx(1, duplicate)))
    (
        await service.transact(
            project,
            tx(
                2,
                dict(
                    op="dismiss",
                    kind="property",
                    id="page",
                    property="display_name",
                    reason="Unsubstantiated",
                ),
            ),
        )
    )
    with pytest.raises(HTTPException):
        (await service.transact(project, tx(3, one)))


async def test_concurrent_writers_and_idempotent_retries(fixture):
    store, project, _ = fixture

    async def write(i):
        try:
            return await ApplicationModelService(NebulaStore(store.database)).transact(
                project, tx(0, obj(str(i)), key=f"writer-{i}")
            )
        except HTTPException as exc:
            return exc.status_code

    results = list((await asyncio.gather(*(write(_item) for _item in [1, 2]))))
    assert sum(r == 409 for r in results) == 1
    assert (
        len((await ApplicationModelService(store).workspace(project))["objects"]) == 1
    )
    with pytest.raises(HTTPException):
        (
            await ApplicationModelService(store).transact(
                project,
                tx(
                    0,
                    obj("different"),
                    key="writer-" + ("1" if results[0] != 409 else "2"),
                ),
            )
        )


async def test_custom_schema_transaction_and_isolation(fixture):
    store, project, _ = fixture
    service = ApplicationModelService(store)
    definition = dict(
        name="custom.TenantBoundary",
        label="Tenant boundary",
        category="Security",
        description="Declared tenant boundary",
        extends="SecurityPolicy",
        properties=[],
        identity_hints=["name"],
        evidence_examples=["Recorded policy"],
    )
    (
        await service.transact(
            project,
            tx(
                0,
                dict(op="define_type", definition=definition),
                obj("boundary", "custom.TenantBoundary"),
            ),
        )
    )
    assert any(
        t["name"] == "custom.TenantBoundary"
        for t in (await service.schema(project))["types"]
    )
    other = await store.create(Engagement(name="Other"))
    assert len((await service.schema(other.id))["types"]) == 33
    assert (await service.search(project, type="SecurityPolicy"))["total"] == 1


async def test_legacy_records_survive_but_cannot_seed_new_inventory(fixture):
    from nebula.v3.application_model.persistence import graphs
    from nebula.v3.application_model.service import empty

    store, project, _ = fixture
    graph = empty(project)
    graph["revision"] = 1
    old = obj(
        "old-asset", "Asset", properties={"url": {"value": "http://site-a.test/logo"}}
    )
    old.pop("op")
    graph["objects"]["old-asset"] = old
    graph["types"] = [
        dict(
            name="custom.LegacyDatabase",
            label="Legacy database",
            category="Dependencies",
            description="Pre-v2 specialization",
            extends="Database",
            properties=[],
            identity_hints=["name"],
            evidence_examples=["Historical record"],
        )
    ]
    async with store.database.engine.begin() as connection:
        (
            await connection.execute(
                insert(graphs).values(project_id=project, revision=1, payload=graph)
            )
        )
    service = ApplicationModelService(store)
    assert "Asset" not in {t["name"] for t in (await service.schema(project))["types"]}
    assert "custom.LegacyDatabase" not in {
        t["name"] for t in (await service.schema(project))["types"]
    }
    displayed = await service.view(project, object_id="old-asset")
    assert displayed["objects"][0]["id"] == "old-asset"
    assert next(t for t in displayed["schema"]["types"] if t["name"] == "Asset")[
        "legacy"
    ]
    with pytest.raises(ValueError, match="legacy-only"):
        (await service.transact(project, tx(1, obj("new-asset", "Asset"))))
    (
        await service.transact(
            project,
            tx(1, {"op": "put_object", **old, "label": "Retained legacy asset"}),
        )
    )
    assert (await ApplicationModelService(NebulaStore(store.database)).search(project))[
        "objects"
    ][0]["label"] == "Retained legacy asset"
    assert (await service.relationship_options(project, "old-asset", "old-asset"))[
        "options"
    ] == []


async def test_custom_types_cannot_reopen_inventory(fixture):
    store, project, _ = fixture
    service = ApplicationModelService(store)
    with pytest.raises(ValueError, match="specialize an active mechanism"):
        (
            await service.transact(
                project,
                tx(
                    0,
                    dict(
                        op="define_type",
                        definition=dict(
                            name="custom.Link",
                            label="Link",
                            category="Structure",
                            description="Inventory link",
                            extends="Asset",
                            properties=[],
                            identity_hints=["url"],
                            evidence_examples=["Page link"],
                        ),
                    ),
                ),
            )
        )
    assert (await service.snapshot(project))["revision"] == 0


async def test_mechanism_chain_persists_without_per_observation_edits(fixture):
    store, project, evidence = fixture
    service = ApplicationModelService(store)
    endpoint = obj(
        "endpoint",
        "Endpoint",
        properties={
            "url": {"value": "http://site-a.test/session"},
            "method": {"value": "POST"},
            "purpose": {"value": "Establishes login context"},
        },
    )
    edge = dict(
        op="put_relationship",
        id="session-edge",
        type="establishes_session",
        source="login",
        target="session",
        claim={"value": True},
    )
    (
        await service.transact(
            project,
            tx(
                0,
                obj("login", "AuthenticationFlow"),
                obj("session", "Session"),
                endpoint,
                edge,
            ),
        )
    )
    restarted = ApplicationModelService(NebulaStore(store.database))
    assert (await restarted.workspace(project))["revision"] == 1
    assert (await restarted.snapshot(project))["objects"]["endpoint"]["properties"][
        "method"
    ]["value"] == "POST"
    assert any(
        o["type"] == "establishes_session"
        for o in (await restarted.relationship_options(project, "login", "session"))[
            "options"
        ]
    )


def test_versioned_reset_preserves_original_and_unrelated_entities(tmp_path):
    # Run the actual migration against a database at the previous revision.
    from sqlalchemy import create_engine, text
    from alembic import command
    from alembic.config import Config

    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[2] / "src/nebula/v3/migrations")
    )
    config.set_main_option("sqlalchemy.url", str(engine.url))
    command.upgrade(config, "0013_application_model_outbox")
    migration = importlib.import_module(
        "nebula.v3.migrations.versions.0014_application_graph"
    )
    with engine.begin() as connection:
        for i, kind in enumerate(
            (
                *migration.EXPERIMENTAL_KINDS,
                "observations",
                "evidence",
                "browser_traffic",
                "notes",
                "application_model_future",
            )
        ):
            connection.execute(
                insert(EntityRow).values(
                    id=str(i),
                    kind=kind,
                    engagement_id="p",
                    revision=1,
                    created_at=__import__("datetime").datetime.now(),
                    updated_at=__import__("datetime").datetime.now(),
                    payload={"untouched": kind},
                )
            )
        connection.execute(
            text(
                "INSERT INTO application_model_outbox (id,engagement_id,model_session_id,source_kind,source_id,source_revision,adapter_version,payload,status,attempts,created_at) VALUES ('q','p','m','evidence','e',1,'1','{}','pending',0,CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        kinds = set(connection.execute(select(EntityRow.kind)).scalars())
        assert kinds == {
            "observations",
            "evidence",
            "browser_traffic",
            "notes",
            "application_model_future",
        }
        assert "application_model_outbox" not in inspect(connection).get_table_names()
    command.upgrade(config, "head")


async def test_project_deletion_cannot_resurrect_previous_graph(fixture):
    from nebula.v3.application_model.persistence import graphs, edits

    store, project, _ = fixture
    service = ApplicationModelService(store)
    (await service.transact(project, tx(0, obj())))
    (await store.delete(Engagement, project))
    async with store.database.engine.connect() as connection:
        assert (await connection.execute(select(graphs))).all() == []
        assert (await connection.execute(select(edits))).all() == []
    (await store.create(Engagement(id=project, name="New project")))
    assert (await service.workspace(project))["objects"] == []


pytestmark = pytest.mark.anyio
