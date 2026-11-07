"""Global Butler roster cost depends on Act projects, not the project roster."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, update

from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.database import EntityRow
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    Engagement,
    EngagementStatus,
    ScopePolicy,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)


async def _actions(tmp_path, *, runtime_local=False):
    store = NebulaStore(tmp_path / "roster.db")
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id="host",
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="synthetic-event",
            source_event_sequence=1,
            authority_revision=1,
            status="investigating",
            summary="Synthetic roster check",
        )
    )
    work = AsyncMock()
    work.catalog.return_value = {"runtimes": [{"id": "synthetic-runtime"}]}
    return store, ButlerGlobalActions(
        store, work, tmp_path, case.id, runtime_local=runtime_local
    )


async def test_roster_batches_authority_for_2500_projects(tmp_path, monkeypatch):
    store, actions = await _actions(tmp_path)
    projects = [
        Engagement(id=f"project-{i:04}", name=f"Project {i}") for i in range(2500)
    ]
    policies = [
        ButlerAuthorityPolicy(
            id=policy_id(p.id),
            engagement_id=p.id,
            authority="act" if i == 2499 else "watch",
        )
        for i, p in enumerate(projects)
    ]
    await store.create_many([*projects, *policies])
    statements = []

    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(store.database.engine.sync_engine, "before_cursor_execute", counted)
    original = store.iter_readable_entities
    scanned = []

    async def readable(model, filters=None, **kwargs):
        async for entity in original(model, filters, **kwargs):
            if model is Engagement:
                scanned.append(entity.id)
            yield entity

    monkeypatch.setattr(store, "iter_readable_entities", readable)
    try:
        result = await actions.projects({})
    finally:
        event.remove(
            store.database.engine.sync_engine, "before_cursor_execute", counted
        )
    assert [row["id"] for row in result["projects"]] == [projects[-1].id]
    assert result["projects"][0]["authority_revision"] == policies[-1].revision
    assert result["runtimes"] == [{"id": "synthetic-runtime"}]
    assert result["next_offset"] is None
    assert scanned == [projects[-1].id]
    assert len(statements) == 5  # Two case reads, policies, project IDs and payloads.


@pytest.mark.parametrize("runtime_local", [False, True])
async def test_roster_preserves_eligibility_privacy_and_oldest_policy(
    tmp_path, runtime_local
):
    store, actions = await _actions(tmp_path, runtime_local=runtime_local)
    now = utc_now()
    projects = [
        Engagement(id="visible", name="Visible"),
        Engagement(id="private", name="Private", scope_policy_id="private-scope"),
        Engagement(id="mismatch", name="Mismatch", scope_policy_id="wrong-scope"),
        Engagement(id="archived", name="Archived", status=EngagementStatus.ARCHIVED),
        Engagement(id="disabled", name="Disabled", work_enabled=False),
        Engagement(id="duplicate", name="Duplicate"),
        Engagement(id="corrupt", name="Corrupt"),
        Engagement(id="no-policy", name="No policy"),
    ]
    await store.create_many(
        [
            *projects,
            ScopePolicy(id="private-scope", engagement_id="private", local_only=True),
            ScopePolicy(id="wrong-scope", engagement_id="visible"),
            *[
                ButlerAuthorityPolicy(
                    id=policy_id(p.id), engagement_id=p.id, authority="act"
                )
                for p in projects
                if p.id not in {"duplicate", "no-policy"}
            ],
            ButlerAuthorityPolicy(
                id="old-watch",
                engagement_id="duplicate",
                authority="watch",
                created_at=now - timedelta(days=1),
            ),
            ButlerAuthorityPolicy(
                id="new-act", engagement_id="duplicate", authority="act", created_at=now
            ),
            ButlerAuthorityPolicy(
                id="orphan-act", engagement_id="missing", authority="act"
            ),
        ]
    )
    async with store.database.session() as session:
        await session.execute(
            update(EntityRow)
            .where(EntityRow.id == "corrupt")
            .values(payload={"id": "corrupt"})
        )
        await session.commit()
    result = await actions.projects({})
    expected = {"visible", "private", "mismatch"} if runtime_local else {"visible"}
    assert {row["id"] for row in result["projects"]} == expected


async def test_roster_paginates_act_projects_beyond_storage_page(tmp_path):
    store, actions = await _actions(tmp_path)
    now = utc_now()
    projects = [
        Engagement(id=f"project-{i:04}", name=f"Project {i}", created_at=now)
        for i in reversed(range(1005))
    ]
    await store.create_many(
        [
            *projects,
            *[
                ButlerAuthorityPolicy(
                    id=policy_id(p.id), engagement_id=p.id, authority="act"
                )
                for p in projects
            ],
        ]
    )
    first = await actions.projects({})
    assert [row["id"] for row in first["projects"]] == [
        f"project-{i:04}" for i in range(50)
    ]
    assert first["next_offset"] == 50
    last = await actions.projects({"offset": 1000})
    assert [row["id"] for row in last["projects"]] == [
        f"project-{i:04}" for i in range(1000, 1005)
    ]
    assert last["next_offset"] is None


async def test_roster_without_act_authority_reads_no_projects(tmp_path, monkeypatch):
    store, actions = await _actions(tmp_path)
    project = await store.create(Engagement(name="Watch only"))
    await store.create(
        ButlerAuthorityPolicy(engagement_id=project.id, authority="watch")
    )
    original = store.get

    async def get(model, entity_id):
        assert model is not Engagement
        return await original(model, entity_id)

    monkeypatch.setattr(store, "get", get)
    result = await actions.projects({})
    assert result["projects"] == []
    assert result["next_offset"] is None
    actions.work.catalog.assert_awaited_once_with("host")
