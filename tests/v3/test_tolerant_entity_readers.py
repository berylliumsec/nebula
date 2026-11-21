"""Rows a newer release wrote stay readable and keep their unknown keys.

During a rolling release (or after a code rollback) release N reads entity
rows that release N+1 saved with fields N does not define. N must decode
them, and every rewrite by N must keep N+1's fields, while API input and
update changes keep rejecting unknown keys. Runs on SQLite and PostgreSQL.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from nebula.v3.database import Database, DeploymentHostRow, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    AgentRun,
    BrowserSession,
    BrowserTabState,
    MissionGrant,
    Observation,
    ScopePolicy,
    utc_now,
)
from nebula.v3.operators import OperatorProfileService
from nebula.v3.relations import ResourceRelationService
from nebula.v3.runtime_coordination import OperationRejected
from nebula.v3.storage import CorruptRecordError, NebulaStore
from pydantic import BaseModel, ConfigDict

from nebula.v3.stored_entities import (
    decode_stored,
    ENTITY_FIELD_ADDITION_FEATURE,
    TOLERANT_ENTITY_READ_FEATURE,
    dump_with_unknown,
)
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio

FUTURE_TOP = {"mode": "n-plus-one", "weights": [1, 2]}
FUTURE_GRANT = "n-plus-one grant note"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def store(location):
    database = Database(location, pool_settings=PoolSettings(5, 12, 2, 5))
    store = NebulaStore(database)
    try:
        yield store
    finally:
        await store.event_history.close()
        await database.dispose()


def _grant(note: str = "") -> MissionGrant:
    now = utc_now()
    return MissionGrant(
        risk_classes=[],
        tool_names=[note] if note else [],
        granted_at=now,
        expires_at=now + timedelta(days=1),
        granted_by="synthetic-operator",
    )


async def _policy_saved_by_newer_release(store: NebulaStore) -> ScopePolicy:
    """A scope policy row as release N+1 would leave it."""

    policy = await store.create(
        ScopePolicy(
            engagement_id="synthetic-project",
            grants=[_grant("first"), _grant("second")],
        )
    )
    async with store.database.session() as session:
        row = await session.get(EntityRow, policy.id)
        payload = dict(row.payload)
        payload["future_top"] = FUTURE_TOP
        payload["grants"] = [dict(item) for item in payload["grants"]]
        payload["grants"][1]["future_grant_note"] = FUTURE_GRANT
        await session.execute(
            update(EntityRow).where(EntityRow.id == policy.id).values(payload=payload)
        )
        await session.commit()
    return policy


async def _stored_payload(store: NebulaStore, entity_id: str) -> dict:
    async with store.database.session() as session:
        return dict(
            await session.scalar(
                select(EntityRow.payload).where(EntityRow.id == entity_id)
            )
        )


async def test_a_row_with_unknown_keys_reads_instead_of_failing(store):
    policy = await _policy_saved_by_newer_release(store)

    read = await store.get(ScopePolicy, policy.id)
    listed = await store.list_entities(ScopePolicy)

    assert read.grants[1].tool_names == ["second"]
    assert [item.id for item in listed] == [policy.id]
    written = dump_with_unknown(read)
    assert written["future_top"] == FUTURE_TOP
    assert written["grants"][1]["future_grant_note"] == FUTURE_GRANT
    assert "future_grant_note" not in written["grants"][0]


async def test_a_row_that_is_otherwise_invalid_is_still_corrupt(store):
    policy = await _policy_saved_by_newer_release(store)
    async with store.database.session() as session:
        row = await session.get(EntityRow, policy.id)
        payload = dict(row.payload)
        payload["max_concurrency"] = "not a number"
        await session.execute(
            update(EntityRow).where(EntityRow.id == policy.id).values(payload=payload)
        )
        await session.commit()

    with pytest.raises(CorruptRecordError):
        await store.get(ScopePolicy, policy.id)


@pytest.mark.parametrize(
    "update_path",
    ["update", "transaction", "replace", "run_event", "operation_event"],
)
async def test_every_update_path_keeps_the_newer_release_keys(store, update_path):
    await store.create(
        AgentRun(id="run", engagement_id="synthetic-project", objective="Synthetic")
    )
    policy = await _policy_saved_by_newer_release(store)
    current = await store.get(ScopePolicy, policy.id)
    # Rewrite the list holding the nested unknown key, as a release that only
    # knows the old grant fields would, plus an ordinary scalar change.
    grants = [current.grants[0], current.grants[1].model_copy()]
    changes = {
        "local_only": True,
        "grants": [grant.model_dump(mode="json") for grant in grants],
    }
    if update_path == "update":
        updated = await store.update(
            ScopePolicy, policy.id, changes, expected_revision=current.revision
        )
    elif update_path == "transaction":
        async with store.transaction() as transaction:
            updated = await transaction.update(
                ScopePolicy, policy.id, changes, expected_revision=current.revision
            )
    elif update_path == "replace":
        updated = await store.replace(
            ScopePolicy,
            policy.id,
            current.model_copy(update={"local_only": True, "grants": grants}),
            expected_revision=current.revision,
        )
    elif update_path == "run_event":
        updated, _ = await store.update_with_event(
            ScopePolicy,
            policy.id,
            changes,
            expected_revision=current.revision,
            run_id="run",
            event_type="synthetic.changed",
            event_payload={},
        )
    else:
        updated, _ = await store.update_with_operation_event(
            ScopePolicy,
            policy.id,
            changes,
            expected_revision=current.revision,
            operation_id="operation",
            operation_kind="synthetic",
            engagement_id="synthetic-project",
            event_type="synthetic.changed",
            event_payload={},
        )

    stored = await _stored_payload(store, policy.id)
    assert updated.local_only is True
    assert stored["local_only"] is True
    assert stored["revision"] == current.revision + 1
    assert stored["future_top"] == FUTURE_TOP
    assert stored["grants"][1]["future_grant_note"] == FUTURE_GRANT
    assert "future_grant_note" not in stored["grants"][0]
    # A second rewrite through the returned entity keeps them too.
    again = await store.update(
        ScopePolicy,
        policy.id,
        {"local_only": False},
        expected_revision=updated.revision,
    )
    stored = await _stored_payload(store, again.id)
    assert stored["future_top"] == FUTURE_TOP
    assert stored["grants"][1]["future_grant_note"] == FUTURE_GRANT


async def test_a_nested_key_is_dropped_rather_than_moved_to_another_item(store):
    policy = await _policy_saved_by_newer_release(store)
    current = await store.get(ScopePolicy, policy.id)
    first, second = current.grants

    # Grants have no id: the item that held the key ("second") moved from
    # index 1 to 0 and a new grant took index 1, so neither may gain it.
    await store.update(
        ScopePolicy,
        policy.id,
        {
            "grants": [
                second.model_dump(mode="json"),
                _grant("third").model_dump(mode="json"),
            ]
        },
        expected_revision=current.revision,
    )
    stored = await _stored_payload(store, policy.id)
    assert [grant.get("future_grant_note") for grant in stored["grants"]] == [
        None,
        None,
    ]
    assert stored["future_top"] == FUTURE_TOP

    # An id-less item whose own content changed loses the key as well.
    policy = await _policy_saved_by_newer_release(store)
    current = await store.get(ScopePolicy, policy.id)
    edited = current.grants[1].model_copy(update={"tool_names": ["edited"]})
    await store.update(
        ScopePolicy,
        policy.id,
        {"grants": [g.model_dump(mode="json") for g in (current.grants[0], edited)]},
        expected_revision=current.revision,
    )
    stored = await _stored_payload(store, policy.id)
    assert "future_grant_note" not in stored["grants"][1]


async def test_a_nested_key_follows_its_item_by_id(store):
    browser = await store.create(
        BrowserSession(
            engagement_id="synthetic-project",
            identity_id="synthetic-identity",
            name="Synthetic browser",
            tabs=[BrowserTabState(id="tab-a"), BrowserTabState(id="tab-b")],
        )
    )
    async with store.database.session() as session:
        row = await session.get(EntityRow, browser.id)
        payload = dict(row.payload)
        payload["tabs"] = [dict(tab) for tab in payload["tabs"]]
        payload["tabs"][1]["future_tab_note"] = "b"
        await session.execute(
            update(EntityRow).where(EntityRow.id == browser.id).values(payload=payload)
        )
        await session.commit()
    current = await store.get(BrowserSession, browser.id)
    tab_a, tab_b = current.tabs

    # Reordered, retitled and appended: the key stays with tab-b.
    await store.update(
        BrowserSession,
        browser.id,
        {
            "tabs": [
                tab_b.model_copy(update={"title": "Moved"}).model_dump(mode="json"),
                tab_a.model_dump(mode="json"),
                BrowserTabState(id="tab-c").model_dump(mode="json"),
            ]
        },
        expected_revision=current.revision,
    )

    stored = await _stored_payload(store, browser.id)
    assert [tab.get("future_tab_note") for tab in stored["tabs"]] == ["b", None, None]
    assert stored["tabs"][0]["title"] == "Moved"


async def test_a_copy_saved_as_a_new_row_does_not_inherit_unknown_keys(store):
    policy = await _policy_saved_by_newer_release(store)
    current = await store.get(ScopePolicy, policy.id)

    copy = await store.create(current.model_copy(update={"id": "synthetic-copy"}))

    stored = await _stored_payload(store, copy.id)
    assert "future_top" not in stored
    assert all("future_grant_note" not in grant for grant in stored["grants"])


class _Left(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    left: int = 0


class _Right(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    right: int = 0


class _Holder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member: _Left | _Right


def test_an_ambiguous_union_is_never_guessed():
    # Pydantic reports "left" as extra for _Right; stripping it would lose 7.
    # Union members are never guessed, so the row stays unreadable instead.
    with pytest.raises(ValidationError):
        decode_stored(_Holder, {"member": {"kind": "l", "left": 7, "new": 1}})
    assert decode_stored(_Holder, {"member": {"kind": "l", "left": 7}}).member.left == 7


async def test_changes_and_input_still_reject_unknown_keys(store):
    policy = await _policy_saved_by_newer_release(store)
    current = await store.get(ScopePolicy, policy.id)

    with pytest.raises(ValidationError):
        await store.update(
            ScopePolicy,
            policy.id,
            {"not_a_field": True},
            expected_revision=current.revision,
        )
    with pytest.raises(ValidationError):
        # Changing a newer release's value is not this release's to make.
        await store.update(
            ScopePolicy,
            policy.id,
            {"future_top": {"mode": "rewritten"}},
            expected_revision=current.revision,
        )
    with pytest.raises(ValidationError):
        await store.update(
            ScopePolicy,
            policy.id,
            {"grants": [{**_grant().model_dump(mode="json"), "extra": 1}]},
            expected_revision=current.revision,
        )
    with pytest.raises(ValidationError):
        ScopePolicy.model_validate(
            {**current.model_dump(mode="json"), "future_top": FUTURE_TOP}
        )
    with pytest.raises(ValidationError):
        await store.create(
            ScopePolicy.model_validate(
                {"engagement_id": "synthetic-project", "not_a_field": 1}
            )
        )
    assert (await _stored_payload(store, policy.id))["revision"] == current.revision


async def _add_future_key(store: NebulaStore, entity_id: str) -> None:
    async with store.database.session() as session:
        row = await session.get(EntityRow, entity_id)
        await session.execute(
            update(EntityRow)
            .where(EntityRow.id == entity_id)
            .values(payload={**row.payload, "future_top": FUTURE_TOP})
        )
        await session.commit()


async def test_relation_and_operator_rewrites_keep_unknown_keys(store):
    observation = await store.create(
        Observation(
            engagement_id="synthetic-project",
            observation_type="synthetic",
            title="Before",
        )
    )
    await _add_future_key(store, observation.id)
    current = await store.get(Observation, observation.id)

    saved = await ResourceRelationService(store).replace_legacy_entity(
        current,
        current.model_copy(update={"title": "After"}),
        expected_revision=current.revision,
    )

    stored = await _stored_payload(store, observation.id)
    assert saved.title == stored["title"] == "After"
    assert stored["future_top"] == FUTURE_TOP

    service = OperatorProfileService(store)
    profile = await service.create_profile(display_name="Synthetic operator")
    await _add_future_key(store, profile.id)

    await service.update_profile(profile.id, {"display_name": "Renamed"})

    stored = await _stored_payload(store, profile.id)
    assert stored["display_name"] == "Renamed"
    assert stored["future_top"] == FUTURE_TOP


async def _stage(host, version, features):
    await host[0].stage(
        manifest(version, features=features),
        Path("/isolated/releases") / version,
        "d" * 64,
    )


async def _slots(host, *, target, rollback):
    async with host[0].database.session() as session:
        row = await session.get(DeploymentHostRow, "host")
        row.target_version = target
        row.rollback_version = rollback
        await session.commit()


async def test_a_field_addition_waits_for_tolerant_readers(host):
    await _stage(host, "tolerant", [TOLERANT_ENTITY_READ_FEATURE])
    await _stage(
        host,
        "adds-fields",
        [TOLERANT_ENTITY_READ_FEATURE, ENTITY_FIELD_ADDITION_FEATURE],
    )
    # v1 predates tolerant readers and is still running.
    assert await worker(host, "v1") is not None
    with pytest.raises(OperationRejected, match="v1"):
        await worker(host, "adds-fields")


async def test_a_field_addition_waits_for_the_current_target(host):
    await _stage(host, "tolerant", [TOLERANT_ENTITY_READ_FEATURE])
    await _stage(
        host,
        "adds-fields",
        [TOLERANT_ENTITY_READ_FEATURE, ENTITY_FIELD_ADDITION_FEATURE],
    )
    # No worker runs v1, but new admissions and recovery would start it.
    await _slots(host, target="v1", rollback=None)
    with pytest.raises(OperationRejected, match="v1"):
        await worker(host, "adds-fields")

    # A stale rollback slot does not block: the target replaces it.
    await _slots(host, target="tolerant", rollback="v1")
    assert await worker(host, "adds-fields") is not None


async def test_after_a_field_addition_only_tolerant_releases_run(host):
    coordinator = host[0]
    await _stage(host, "tolerant", [TOLERANT_ENTITY_READ_FEATURE])
    await _stage(
        host,
        "adds-fields",
        [TOLERANT_ENTITY_READ_FEATURE, ENTITY_FIELD_ADDITION_FEATURE],
    )
    await _slots(host, target="tolerant", rollback=None)
    assert await worker(host, "adds-fields") is not None
    await _slots(host, target="adds-fields", rollback="tolerant")

    # Rows may now hold its fields, so no intolerant release may come back.
    with pytest.raises(OperationRejected, match="adds-fields"):
        await worker(host, "v1")
    with pytest.raises(OperationRejected, match="adds-fields"):
        await coordinator.target_execution("v1")
    async with coordinator.database.session() as session:
        revision = (await session.get(DeploymentHostRow, "host")).revision
    with pytest.raises(OperationRejected, match="adds-fields"):
        await coordinator.rollback_code("v1", expected_revision=revision)
    # A tolerant release remains a valid rollback.
    await coordinator.rollback_code("tolerant", expected_revision=revision)


async def test_a_release_without_field_additions_rolls_as_before(host):
    await _stage(host, "tolerant", [TOLERANT_ENTITY_READ_FEATURE])
    assert await worker(host, "v1") is not None
    assert await worker(host, "tolerant") is not None


def test_a_new_entity_field_needs_a_reviewed_addition_flag():
    from nebula.v3.domain import ENTITY_MODELS
    from nebula.v3.runtime_bundle import _check_entity_fields, load_compatibility
    from nebula.v3.stored_entities import entity_fields_digest

    manifest = load_compatibility()
    assert TOLERANT_ENTITY_READ_FEATURE in manifest["features"]
    current = entity_fields_digest(ENTITY_MODELS)

    class _Added(ScopePolicy):
        future_field: str | None = None

    assert entity_fields_digest((*ENTITY_MODELS, _Added)) != current
    with pytest.raises(ValueError, match="entity fields changed"):
        _check_entity_fields(
            {
                "entity_fields_sha256": "0" * 64,
                "entity_fields_tolerant_baseline_sha256": "0" * 64,
            },
            manifest,
        )
    reviewed = {
        "entity_fields_sha256": current,
        "entity_fields_tolerant_baseline_sha256": "1" * 64,
    }
    with pytest.raises(ValueError, match=ENTITY_FIELD_ADDITION_FEATURE):
        _check_entity_fields(reviewed, manifest)
    _check_entity_fields(
        reviewed,
        {
            **manifest,
            "features": [*manifest["features"], ENTITY_FIELD_ADDITION_FEATURE],
        },
    )
