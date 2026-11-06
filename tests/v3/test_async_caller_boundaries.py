"""Converted callers preserve validation before selecting runtime capacity."""

import pytest

from nebula.v3.butler_global_capabilities import ButlerGlobalActions
from nebula.v3.domain import (
    ButlerRecoveryCase,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessModelOptions,
    HarnessProfile,
    HarnessRuntimeOption,
    ScopePolicy,
)
from tests.v3.test_async_postgresql import postgresql_url as postgresql_url
from tests.v3.test_butler_capabilities import _capabilities
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "privacy_case", ["no_sensitive", "no_consent", "local_scope", "allowed"]
)
async def test_recovery_harness_checks_privacy_before_quota_ranking(
    tmp_path, privacy_case
):
    store, project, _, _, ordinary = await _capabilities(tmp_path)
    options = HarnessCapabilities(
        models=["gpt-6-sol"],
        model_options=[
            HarnessModelOptions(
                model="gpt-6-sol",
                reasoning_efforts=[HarnessRuntimeOption(id="max", label="Max")],
                cyber_access_programs=["daybreakBlue"],
            )
        ],
    )
    remote = await store.create(
        HarnessProfile(
            name="Remote candidate",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            capabilities=options,
            privacy={
                "local_only": False,
                "permits_sensitive_data": privacy_case != "no_sensitive",
                "auto_share_tool_results": privacy_case
                not in {"no_sensitive", "no_consent"},
            },
        )
    )
    local = await store.create(
        HarnessProfile(
            name="Local fallback",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="gpt-6-sol",
            capabilities=options,
            privacy={"local_only": True},
        )
    )
    if privacy_case == "local_scope":
        scope = await store.create(
            ScopePolicy(engagement_id=project.id, local_only=True)
        )
        project = await store.update(
            Engagement,
            project.id,
            {"scope_policy_id": scope.id},
            expected_revision=project.revision,
        )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="synthetic",
            source_event_sequence=1,
            authority_revision=1,
            status="investigating",
            summary="Synthetic recovery",
        )
    )
    inspected = []

    async def quota(_project_id, arguments):
        inspected.append(arguments["profile_id"])
        return {
            "state": "available",
            "snapshots": [
                {
                    "remaining_percent": 100
                    if arguments["profile_id"] == remote.id
                    else 10
                }
            ],
        }

    ordinary._work.harness_quota = quota
    actions = ButlerGlobalActions(
        store, ordinary._work, tmp_path, case.id, runtime_local=True
    )
    selected = await actions._recovery_harness(project.id)
    assert selected.id == (remote.id if privacy_case == "allowed" else local.id)
    assert (remote.id in inspected) == (privacy_case == "allowed")


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_background_audit_steps_merge_concurrently_and_fence_authority(
    tmp_path, postgresql_url, dialect
):
    import asyncio
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.butler_authority import policy_id
    from nebula.v3.butler_capabilities import ButlerCapabilities
    from nebula.v3.database import Database
    from nebula.v3.database_pool import PoolSettings
    from nebula.v3.domain import ButlerAuthorityPolicy
    from nebula.v3.storage import NebulaStore

    database = (
        Database(tmp_path / "audit.db")
        if dialect == "sqlite"
        else Database(
            postgresql_url, pool_settings=PoolSettings(5, 12, 2, requested_size=4)
        )
    )
    store = NebulaStore(database)
    project = await store.create(Engagement(name="Concurrent audit", status="active"))
    policy = await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project.id),
            engagement_id=project.id,
            authority="act",
        )
    )
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="immediate",
            signal="provider.unavailable",
            source_event_id="synthetic",
            source_event_sequence=1,
            authority_revision=policy.revision,
            status="investigating",
            summary="Synthetic audit",
        )
    )
    capabilities = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        project.id,
        background_case_id=case.id,
    )

    async def step(number):
        identifier = await capabilities._begin_background_step(
            "nebula.work.chat_settings", {"number": number}
        )
        await asyncio.sleep(0)
        await capabilities._finish_background_step(
            identifier,
            operation="nebula.work.chat_settings",
            arguments={},
            result={"observed": number},
        )
        return identifier

    identifiers = await asyncio.gather(*(step(number) for number in range(8)))
    current = await store.get(ButlerRecoveryCase, case.id)
    assert {item["id"] for item in current.steps} == set(identifiers)
    assert len(current.steps) == 8
    assert all(item["status"] == "applied" for item in current.steps)
    await store.update(
        ButlerAuthorityPolicy,
        policy.id,
        {"authority": "watch"},
        expected_revision=policy.revision,
    )
    with pytest.raises(ValueError, match="authority or recovery case changed"):
        await capabilities._begin_background_step("nebula.work.chat_settings", {})
    assert (await store.get(ButlerRecoveryCase, case.id)).steps == current.steps
    assert database.pool_snapshot()["checked_out"] == 0


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_recent_messages_stream_a_bounded_window_and_close_cursor(
    tmp_path, postgresql_url, dialect
):
    from sqlalchemy import event, text
    from nebula.v3.database import Database, EntityRow
    from nebula.v3.database_pool import PoolSettings
    from nebula.v3.domain import ChatMessage, ChatSession
    from nebula.v3.storage import NebulaStore

    database = (
        Database(tmp_path / "messages.db")
        if dialect == "sqlite"
        else Database(
            postgresql_url, pool_settings=PoolSettings(5, 12, 2, requested_size=1)
        )
    )
    store = NebulaStore(database)
    chat = await store.create(
        ChatSession(
            engagement_id="project",
            title="Synthetic history",
            provider_profile_id="fixture",
            model="model",
        )
    )
    messages = [
        ChatMessage(
            engagement_id="project",
            session_id=chat.id,
            sequence=number,
            role="assistant",
            content=str(number),
            metadata={"retracted_at": "synthetic"} if number > 968 else {},
        )
        for number in range(1, 1001)
    ]
    async with store.transaction() as transaction:
        await transaction.add_chat_messages(messages)
    hydrated = []

    def loaded(row, _context):
        if row.kind == ChatMessage.entity_kind:
            hydrated.append(row.id)

    event.listen(EntityRow, "load", loaded)
    try:
        async with database.session() as session:
            recent = await store.recent_chat_messages(chat.id)
            assert [item.content for item in recent] == [
                str(number) for number in range(961, 969)
            ]
            assert len(hydrated) <= 64, (
                "the complete transcript was buffered before early exit"
            )
            if dialect == "postgresql":
                assert (
                    await session.scalar(text("SELECT count(*) FROM pg_cursors")) == 0
                )
            assert await session.scalar(text("SELECT 1")) == 1
        assert database.pool_snapshot()["checked_out"] == 0
    finally:
        event.remove(EntityRow, "load", loaded)


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_default_runtime_policy_joins_rollback_and_concurrent_admission(
    tmp_path, postgresql_url, dialect
):
    import asyncio
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.automation_runtime import AutomationRuntimeManager
    from nebula.v3.database import Database
    from nebula.v3.database_pool import PoolSettings
    from nebula.v3.domain import AutomationProjectPolicy
    from nebula.v3.storage import NebulaStore

    database = (
        Database(tmp_path / "policy.db")
        if dialect == "sqlite"
        else Database(
            postgresql_url, pool_settings=PoolSettings(5, 12, 2, requested_size=4)
        )
    )
    store = NebulaStore(database)
    project = await store.create(Engagement(name="Atomic policy"))
    manager = AutomationRuntimeManager(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "runtime",
        workspace_resolver=lambda _: tmp_path,
        runtime_image="synthetic/runtime@sha256:" + "a" * 64,
    )
    try:
        with pytest.raises(ValueError, match="abort admission"):
            async with store.transaction(immediate=True) as transaction:
                policy = await manager.project_policy(
                    project.id, transaction=transaction
                )
                assert (
                    await store.get(AutomationProjectPolicy, policy.id)
                ).id == policy.id
                raise ValueError("abort admission")
        assert (
            await store.find_entities(
                AutomationProjectPolicy, {}, engagement_id=project.id
            )
            == []
        )

        async def policy_for_admission(number):
            if number % 2:
                return await manager.project_policy(project.id)
            async with store.transaction(immediate=True) as transaction:
                return await manager.project_policy(project.id, transaction=transaction)

        policies = await asyncio.gather(
            *(policy_for_admission(number) for number in range(8))
        )
        assert len({policy.id for policy in policies}) == 1
        assert (
            len(
                await store.find_entities(
                    AutomationProjectPolicy, {}, engagement_id=project.id
                )
            )
            == 1
        )
        assert database.pool_snapshot()["checked_out"] == 0
    finally:
        await manager.shutdown()
