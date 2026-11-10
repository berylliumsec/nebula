"""Unlimited reference inventories keep the existing checkpoint digest in batches."""

import hashlib
import json

import pytest
from sqlalchemy import insert, or_, select

from nebula.v3.database import EntityRow, ProviderToolManifestRow, ProviderTurnQueueRow
from nebula.v3.domain import ChatSession, ChatTurn, utc_now
from nebula.v3.runtime_project_handoff import (
    CHECKPOINT_BATCH_ROWS,
    project_sql_checkpoint,
)
from tests.v3.test_chat import _profile
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def legacy_digest(session, project_id):
    rows = (
        await session.execute(
            select(EntityRow.id, EntityRow.kind, EntityRow.revision)
            .where(
                or_(
                    EntityRow.engagement_id == project_id,
                    (EntityRow.kind == "engagements") & (EntityRow.id == project_id),
                )
            )
            .order_by(EntityRow.id)
        )
    ).all()
    queues = (
        await session.execute(
            select(
                ProviderTurnQueueRow.turn_id,
                ProviderTurnQueueRow.state,
                ProviderTurnQueueRow.lease_owner,
            )
            .join(EntityRow, EntityRow.id == ProviderTurnQueueRow.turn_id)
            .where(EntityRow.engagement_id == project_id)
            .order_by(ProviderTurnQueueRow.turn_id)
        )
    ).all()
    manifests = (
        await session.execute(
            select(
                ProviderToolManifestRow.session_id,
                ProviderToolManifestRow.revision,
                ProviderToolManifestRow.digest,
            )
            .where(ProviderToolManifestRow.project_id == project_id)
            .order_by(
                ProviderToolManifestRow.session_id, ProviderToolManifestRow.revision
            )
        )
    ).all()
    parts = [list(map(list, rows)), list(map(list, queues))]
    if manifests:
        parts.append(list(map(list, manifests)))
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("with_manifests", [False, True])
async def test_batched_checkpoint_preserves_exact_v1_digest(host, with_manifests):
    coordinator, store = host
    profile = await store.create(_profile(local=True))
    chat = await store.create(
        ChatSession(
            id="session-\u00e9",
            engagement_id="one",
            title="Synthetic checkpoint",
            provider_profile_id=profile.id,
            model="synthetic",
        )
    )
    turn = await store.create(
        ChatTurn(
            id="turn-\u00e9",
            engagement_id="one",
            session_id=chat.id,
            provider_profile_id=profile.id,
            model="synthetic",
        )
    )
    async with coordinator.database.session() as session:
        session.add(
            ProviderTurnQueueRow(
                turn_id=turn.id,
                lane="interactive",
                state="parked",
                lease_owner="owner-\u00e9" if with_manifests else None,
            )
        )
        if with_manifests:
            for revision in (2, 1):
                session.add(
                    ProviderToolManifestRow(
                        project_id="one",
                        session_id=chat.id,
                        revision=revision,
                        digest=str(revision) * 64,
                        definitions=[],
                    )
                )
    async with coordinator.database.session() as session:
        checkpoint = await project_sql_checkpoint(session, "one")
        assert checkpoint["sql_sha256"] == await legacy_digest(session, "one")
        assert checkpoint["queue_count"] == 1
        assert checkpoint["format"] == "nebula.project-checkpoint/v1"


async def test_checkpoint_accepts_more_than_old_limit_in_multiple_batches(host):
    coordinator, _store = host
    count = 20 * CHECKPOINT_BATCH_ROWS + 9
    now = utc_now()
    async with coordinator.database.session() as session:
        await session.execute(
            insert(EntityRow),
            [
                {
                    "id": f"synthetic-{index:08d}",
                    "kind": "observations",
                    "engagement_id": "one",
                    "revision": 1,
                    "payload": {},
                    "created_at": now,
                    "updated_at": now,
                }
                for index in range(count)
            ],
        )
    async with coordinator.database.session() as session:
        checkpoint = await project_sql_checkpoint(session, "one")
        assert checkpoint["entity_count"] == count + 1
        assert checkpoint["queue_count"] == 0
        assert checkpoint["sql_sha256"] == await legacy_digest(session, "one")
        assert set(checkpoint) == {
            "format",
            "project_id",
            "entity_count",
            "queue_count",
            "sql_sha256",
        }


async def test_empty_checkpoint_keeps_two_empty_inventories(host):
    coordinator, _store = host
    async with coordinator.database.session() as session:
        checkpoint = await project_sql_checkpoint(session, "no-such-project")
    assert checkpoint["entity_count"] == checkpoint["queue_count"] == 0
    assert checkpoint["sql_sha256"] == hashlib.sha256(b"[[],[]]").hexdigest()
