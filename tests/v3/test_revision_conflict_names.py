"""A revision conflict names the record whose revision moved.

"revision conflict: expected 17, found 18" alone cannot be traced to code: every
optimistic write in Core raises it. The record's kind and id point at the write
that lost; nothing from the record's content belongs in the message.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import re

import pytest
from sqlalchemy import Update, update
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    Asset,
    Engagement,
    Finding,
    RelationPredicate,
    Report,
    ResourceKind,
    ResourceRef,
    ResourceRelationCreate,
    ResourceRelationSet,
)
from nebula.v3.operators import OperatorProfileService
from nebula.v3.relations import ResourceRelationService
from nebula.v3.storage import ConflictError, NebulaStore, revision_conflict

PRIVATE = "payload-text-that-must-not-be-echoed"


def _named(kind: str, record_id: str, expected: int, found: int) -> str:
    return re.escape(
        f"revision conflict on {kind} {record_id}: expected {expected}, found {found}"
    )


def _ref(project_id: str, kind: ResourceKind, entity, revision=None) -> ResourceRef:
    return ResourceRef(
        project_id=project_id,
        kind=kind,
        id=entity.id,
        revision=entity.revision if revision is None else revision,
    )


def test_message_keeps_the_words_clients_match_and_the_error_type():
    error = revision_conflict("chat_sessions", "chat-1", 17, 18)

    assert isinstance(error, ConflictError)
    assert str(error) == (
        "revision conflict on chat_sessions chat-1: expected 17, found 18"
    )
    assert str(error).startswith("revision conflict")
    assert str(error).endswith("expected 17, found 18")


async def test_store_update_and_delete_name_the_record_without_its_content(tmp_path):
    store = NebulaStore(tmp_path / "names.db")
    project = await store.create(Engagement(name="Names", description=PRIVATE))
    await store.update(
        Engagement, project.id, {"description": "moved"}, expected_revision=1
    )

    with pytest.raises(ConflictError, match=_named("engagements", project.id, 1, 2)):
        await store.update(
            Engagement, project.id, {"description": PRIVATE}, expected_revision=1
        )
    with pytest.raises(ConflictError, match=_named("engagements", project.id, 1, 2)):
        await store.replace(
            Engagement,
            project.id,
            project.model_copy(update={"description": PRIVATE}),
            expected_revision=1,
        )
    with pytest.raises(ConflictError, match=_named("engagements", project.id, 1, 2)):
        async with store.transaction() as transaction:
            await transaction.delete(Engagement, project.id, expected_revision=1)
    with pytest.raises(
        ConflictError, match=_named("engagements", project.id, 1, 2)
    ) as deleted:
        await store.delete(Engagement, project.id, expected_revision=1)
    assert PRIVATE not in str(deleted.value)
    assert (await store.get(Engagement, project.id)).revision == 2


async def test_event_backed_updates_name_the_record(tmp_path):
    store = NebulaStore(tmp_path / "events.db")
    project = await store.create(Engagement(name="Events"))
    asset = await store.create(Asset(engagement_id=project.id, name="Gateway"))

    with pytest.raises(ConflictError, match=_named("assets", asset.id, 4, 1)):
        await store.update_with_event(
            Asset,
            asset.id,
            {"name": PRIVATE},
            expected_revision=4,
            run_id="run",
            event_type="synthetic.changed",
            event_payload={},
        )
    with pytest.raises(ConflictError, match=_named("assets", asset.id, 4, 1)):
        await store.update_with_operation_event(
            Asset,
            asset.id,
            {"name": PRIVATE},
            expected_revision=4,
            operation_id="operation",
            operation_kind="synthetic",
            engagement_id=project.id,
            event_type="synthetic.changed",
            event_payload={},
        )
    with pytest.raises(ConflictError, match=_named("engagements", project.id, 4, 1)):
        await store.relink_project_workspace(
            project.id, str(tmp_path / "elsewhere"), expected_revision=4
        )


async def test_a_write_that_lands_mid_update_names_the_record(tmp_path):
    store = NebulaStore(tmp_path / "interleaved.db")
    project = await store.create(Engagement(name="Interleaved"))

    expected = re.escape(
        f"engagements entity {project.id} changed while the update was in "
        "progress (expected revision 1)"
    )
    with pytest.raises(ConflictError, match=expected) as raised:
        async with store.transaction() as transaction:
            # The update reads this row, then another write moves its revision
            # before the guarded UPDATE runs.
            read = await transaction.session.get(EntityRow, project.id)
            assert read is not None and read.revision == 1
            await transaction.session.execute(
                update(EntityRow)
                .where(EntityRow.id == project.id)
                .values(revision=2)
                .execution_options(synchronize_session=False)
            )
            await transaction.update(Engagement, project.id, {"description": PRIVATE})
    assert PRIVATE not in str(raised.value)


class _MovesTheRowFirst:
    """A ledger connection on which another write lands before the guarded UPDATE."""

    def __init__(self, connection, entity_id: str):
        self._connection = connection
        self._entity_id = entity_id

    def __getattr__(self, name):
        return getattr(self._connection, name)

    async def execute(self, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "entities":
            await self._connection.execute(
                update(EntityRow)
                .where(EntityRow.id == self._entity_id)
                .values(revision=EntityRow.revision + 1)
            )
        return await self._connection.execute(statement, *args, **kwargs)


@pytest.mark.parametrize("operation", ["run", "operation"])
async def test_an_event_backed_transition_that_loses_the_race_names_the_record(
    tmp_path, monkeypatch, operation
):
    store = NebulaStore(tmp_path / "race.db")
    project = await store.create(Engagement(name="Race"))
    asset = await store.create(Asset(engagement_id=project.id, name="Gateway"))
    connect = store.database.connect

    async def moved_first():
        return _MovesTheRowFirst(await connect(), asset.id)

    monkeypatch.setattr(store.database, "connect", moved_first)
    expected = re.escape(
        f"assets entity {asset.id} transition lost an optimistic lock race "
        "(expected revision 1)"
    )
    with pytest.raises(ConflictError, match=expected) as raised:
        if operation == "run":
            await store.update_with_event(
                Asset,
                asset.id,
                {"name": PRIVATE},
                expected_revision=1,
                run_id="run",
                event_type="synthetic.changed",
                event_payload={},
            )
        else:
            await store.update_with_operation_event(
                Asset,
                asset.id,
                {"name": PRIVATE},
                expected_revision=1,
                operation_id="operation",
                operation_kind="synthetic",
                engagement_id=project.id,
                event_type="synthetic.changed",
                event_payload={},
            )
    assert PRIVATE not in str(raised.value)
    monkeypatch.setattr(store.database, "connect", connect)
    assert (await store.get(Asset, asset.id)).name == "Gateway"


async def test_relation_conflicts_name_the_endpoint_or_edge(tmp_path):
    store = NebulaStore(tmp_path / "relations.db")
    project = await store.create(Engagement(name="Relations"))
    asset = await store.create(Asset(engagement_id=project.id, name="Gateway"))
    finding = await store.create(Finding(engagement_id=project.id, title="Issue"))
    service = ResourceRelationService(store)

    with pytest.raises(ConflictError, match=_named("findings", finding.id, 3, 1)):
        await service.create(
            project.id,
            ResourceRelationCreate(
                source=_ref(project.id, ResourceKind.FINDING, finding, revision=3),
                predicate=RelationPredicate.AFFECTS,
                target=_ref(project.id, ResourceKind.ASSET, asset),
                attribution="operator-1",
            ),
        )
    relation = await service.create(
        project.id,
        ResourceRelationCreate(
            source=_ref(project.id, ResourceKind.FINDING, finding),
            predicate=RelationPredicate.AFFECTS,
            target=_ref(project.id, ResourceKind.ASSET, asset),
            attribution="operator-1",
        ),
    )
    with pytest.raises(
        ConflictError, match=_named("resource_relations", relation.id, 2, 1)
    ):
        await service.delete(project.id, relation.id, expected_revision=2)
    with pytest.raises(ConflictError, match=_named("findings", finding.id, 3, 1)):
        await service.reconcile(
            ResourceRelationSet(
                project_id=project.id,
                source=_ref(project.id, ResourceKind.FINDING, finding),
                predicate=RelationPredicate.AFFECTS,
                targets=[_ref(project.id, ResourceKind.ASSET, asset)],
                expected_source_revision=3,
                attribution="operator-1",
            )
        )
    with pytest.raises(ConflictError, match=_named("findings", finding.id, 3, 1)):
        await service.replace_legacy_entity(
            finding,
            finding.model_copy(update={"title": PRIVATE}),
            expected_revision=3,
        )


async def test_operator_profile_conflicts_name_the_profile(tmp_path):
    store = NebulaStore(tmp_path / "operators.db")
    operators = OperatorProfileService(store)
    profile = await operators.create_profile(display_name="Operator")

    with pytest.raises(
        ConflictError,
        match=_named("operator_profiles", profile.id, 9, profile.revision),
    ):
        await operators.update_profile(
            profile.id, {"display_name": PRIVATE}, expected_revision=9
        )
    with pytest.raises(
        ConflictError,
        match=_named("operator_profiles", profile.id, 9, profile.revision),
    ):
        await operators.activate_profile(profile.id, expected_revision=9)


@pytest.fixture
def api(tmp_path):
    store = NebulaStore(tmp_path / "api.db")
    return TestClient(create_app(store, auth_token="test-token")), store


def _auth(**headers: str) -> dict[str, str]:
    return {"Authorization": "Bearer test-token", **headers}


async def test_api_409_names_the_record_for_if_match_and_expected_revision(api):
    client, store = api
    project = await store.create(Engagement(name="API"))
    asset = await store.create(Asset(engagement_id=project.id, name="Gateway"))
    report = await store.create(Report(engagement_id=project.id, title="Report"))
    await store.update(Asset, asset.id, {"name": "moved"}, expected_revision=1)
    named = f"revision conflict on assets {asset.id}: expected 1, found 2"

    patched = await client.patch(
        f"/api/v1/assets/{asset.id}",
        headers=_auth(),
        json={"changes": {"name": PRIVATE}, "expected_revision": 1},
    )
    replacement = (
        await client.get(f"/api/v1/assets/{asset.id}", headers=_auth())
    ).json()
    replacement["name"] = PRIVATE
    replaced = await client.put(
        f"/api/v1/assets/{asset.id}",
        headers=_auth(**{"If-Match": "1"}),
        json=replacement,
    )
    deleted = await client.delete(
        f"/api/v1/assets/{asset.id}", headers=_auth(**{"If-Match": "1"})
    )
    for response in (patched, replaced, deleted):
        assert response.status_code == 409
        body = response.json()
        assert body["detail"] == named
        assert body["operator_detail"] == named
        assert body["reason_code"] == "stale_state"
        assert body["code"].endswith(".conflict_error")
        assert PRIVATE not in response.text

    signed = await client.post(
        f"/api/v1/reports/{report.id}/sign-off",
        headers=_auth(),
        json={"expected_revision": 5, "operator_id": "operator-1"},
    )
    assert signed.status_code == 409
    assert signed.json()["detail"] == (
        f"revision conflict on reports {report.id}: expected 5, found 1"
    )
