"""A release reads and updates documents a newer release wrote, without loss.

Two releases share one database during a rolling deploy, and a code rollback
runs the older release against documents the newer one already wrote. These
cases simulate the newer release by adding fields to stored payloads directly.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from nebula.v3.database import Database, EntityRow
from datetime import timedelta

from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    MissionGrant,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _SETTINGS

pytestmark = pytest.mark.anyio


@pytest.fixture
async def store(location):
    store = NebulaStore(Database(location, pool_settings=_SETTINGS))
    await store.database.initialize()
    try:
        yield store
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def _newer_release_writes(store, entity_id, change):
    """Edit the stored document the way a newer release with more fields would."""
    async with store.database.session() as session:
        row = await session.get(EntityRow, entity_id)
        payload = dict(row.payload)
        change(payload)
        await session.execute(
            update(EntityRow).where(EntityRow.id == entity_id).values(payload=payload)
        )
        await session.commit()


async def _stored(store, entity_id):
    async with store.database.session() as session:
        return dict(
            (
                await session.execute(
                    select(EntityRow.payload).where(EntityRow.id == entity_id)
                )
            ).scalar_one()
        )


async def _turn(store):
    project = await store.create(Engagement(name="Forward compatibility"))
    provider = await store.create(
        ProviderProfile(name="Local", provider_type="vllm", is_local=True)
    )
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Rolling",
            provider_profile_id=provider.id,
            model="model-a",
        )
    )
    return await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            provider_profile_id=provider.id,
            model="model-a",
            status=ChatTurnStatus.QUEUED,
        )
    )


async def test_unknown_fields_are_read_and_kept_through_an_update(store):
    turn = await _turn(store)

    def newer(payload):
        payload["release_n_plus_one_flag"] = {"kept": True}
        payload["usage"] = {**payload["usage"], "reasoning_cache_tokens": 7}

    await _newer_release_writes(store, turn.id, newer)

    # The older release reads the document: no CorruptRecordError, and the
    # unknown fields are never interpreted.
    read = await store.get(ChatTurn, turn.id)
    assert read.status == ChatTurnStatus.QUEUED
    assert "release_n_plus_one_flag" not in read.model_dump()
    listed = await store.list(ChatTurn, limit=10)
    assert [item.id for item in listed] == [turn.id]

    # It updates other fields; the newer release's data survives unchanged.
    updated = await store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.ROUTING},
        expected_revision=read.revision,
    )
    assert updated.status == ChatTurnStatus.ROUTING
    stored = await _stored(store, turn.id)
    assert stored["status"] == "routing"
    assert stored["release_n_plus_one_flag"] == {"kept": True}
    assert stored["usage"]["reasoning_cache_tokens"] == 7

    # A field the older release rewrites is replaced as a whole value.
    rewritten = await store.update(
        ChatTurn,
        turn.id,
        {"usage": {"input_tokens": 3}},
        expected_revision=updated.revision,
    )
    assert rewritten.usage.input_tokens == 3
    stored = await _stored(store, turn.id)
    assert "reasoning_cache_tokens" not in stored["usage"]
    assert stored["release_n_plus_one_flag"] == {"kept": True}


async def test_unknown_fields_in_list_elements_stay_with_their_element(store):
    project = await store.create(Engagement(name="Forward list"))
    expires = utc_now() + timedelta(days=1)
    policy = await store.create(
        ScopePolicy(
            engagement_id=project.id,
            grants=[
                MissionGrant(
                    risk_classes=[RiskClass.PASSIVE],
                    expires_at=expires,
                    granted_by="operator",
                ),
                MissionGrant(
                    risk_classes=[RiskClass.PASSIVE],
                    tool_names=["probe"],
                    expires_at=expires,
                    granted_by="operator",
                ),
            ],
        )
    )

    def newer(payload):
        payload["grants"][1]["approval_note"] = "kept with the second grant"

    await _newer_release_writes(store, policy.id, newer)
    read = await store.get(ScopePolicy, policy.id)
    assert [grant.tool_names for grant in read.grants] == [[], ["probe"]]
    await store.update(
        ScopePolicy, policy.id, {"local_only": True}, expected_revision=read.revision
    )
    stored = await _stored(store, policy.id)
    assert stored["local_only"] is True
    assert "approval_note" not in stored["grants"][0]
    assert stored["grants"][1]["approval_note"] == "kept with the second grant"


async def test_requests_and_other_kinds_stay_strict(store):
    turn = await _turn(store)
    # Request validation never tolerates unknown fields.
    with pytest.raises(ValidationError):
        ChatTurn.model_validate(
            {**turn.model_dump(mode="json"), "release_n_plus_one_flag": 1}
        )
    # A stored document is decoded leniently only as its own kind.
    async with store.database.session() as session:
        row = await session.get(EntityRow, turn.id)
        with pytest.raises(ValidationError):
            ChatSession.model_validate(row.payload)
