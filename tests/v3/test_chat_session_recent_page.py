"""A conversation sidebar read is bounded and ordered by recent activity."""

import pytest
from tests.v3.async_test_support import (
    inspect_database,
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta

from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import ChatGoal, ChatSession, Engagement, utc_now
from nebula.v3.storage import NebulaStore


async def test_recent_conversation_page_is_scoped_ordered_and_indexed(tmp_path):
    store = NebulaStore(tmp_path / "recent.db")
    (await store.create(Engagement(id="project-a", name="Project A")))
    (await store.create(Engagement(id="project-b", name="Project B")))
    base = utc_now() - timedelta(days=1)
    (
        await store.create_many(
            [
                ChatSession(
                    id="old",
                    engagement_id="project-a",
                    title="Old",
                    provider_profile_id="provider",
                    model="model",
                    created_at=base,
                    updated_at=base,
                ),
                ChatSession(
                    id="latest",
                    engagement_id="project-a",
                    title="Latest",
                    provider_profile_id="provider",
                    model="model",
                    created_at=base + timedelta(seconds=1),
                    updated_at=base + timedelta(minutes=3),
                ),
                ChatSession(
                    id="middle",
                    engagement_id="project-a",
                    title="Middle",
                    provider_profile_id="provider",
                    model="model",
                    created_at=base + timedelta(seconds=2),
                    updated_at=base + timedelta(minutes=2),
                ),
                ChatSession(
                    id="other-project",
                    engagement_id="project-b",
                    title="Other",
                    provider_profile_id="provider",
                    model="model",
                    created_at=base + timedelta(seconds=3),
                    updated_at=base + timedelta(minutes=4),
                ),
            ]
        )
    )
    (
        await store.create_many(
            [
                ChatSession(
                    id=f"side-{index}",
                    engagement_id="project-a",
                    title=f"Side {index}",
                    provider_profile_id="provider",
                    model="model",
                    metadata={"side_chat": True},
                    created_at=base + timedelta(minutes=5, seconds=index),
                    updated_at=base + timedelta(minutes=5, seconds=index),
                )
                for index in range(101)
            ]
        )
    )

    headers = {"Authorization": "Bearer test-token"}
    async with TestClient(create_app(store, auth_token="test-token")) as client:
        first = await client.get(
            "/api/v1/chat-sessions",
            params={"engagement_id": "project-a", "recent_first": "true", "limit": 2},
            headers=headers,
        )
        second = await client.get(
            "/api/v1/chat-sessions",
            params={
                "engagement_id": "project-a",
                "recent_first": "true",
                "offset": 2,
                "limit": 2,
            },
            headers=headers,
        )
        original = await client.get(
            "/api/v1/chat-sessions",
            params={"engagement_id": "project-a", "limit": 2},
            headers=headers,
        )

    assert first.status_code == second.status_code == original.status_code == 200
    assert [row["id"] for row in first.json()] == ["latest", "middle"]
    assert [row["id"] for row in second.json()] == ["old"]
    assert [row["id"] for row in original.json()] == ["old", "latest"]
    assert "ix_entities_kind_engagement_updated_id" in {
        index["name"]
        for index in (await inspect_database(store.database, "get_indexes", "entities"))
    }


async def test_older_butler_recovery_chat_has_read_only_sidebar_provenance(tmp_path):
    store = NebulaStore(tmp_path / "recent.db")
    (await store.create(Engagement(id="project-a", name="Project A")))
    (
        await store.create_many(
            [
                ChatSession(
                    id="recovery",
                    engagement_id="project-a",
                    title="Paused goal",
                    provider_profile_id="provider",
                    model="model",
                ),
                ChatSession(
                    id="ordinary",
                    engagement_id="project-a",
                    title="Recover paused goal",
                    provider_profile_id="provider",
                    model="model",
                ),
            ]
        )
    )
    (
        await store.create(
            ChatGoal(
                engagement_id="project-a",
                session_id="recovery",
                objective="Unblock",
                completion_criteria=["Report result"],
                metadata={"butler_recovery_case_id": "case-1"},
            )
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    async with TestClient(create_app(store, auth_token="test-token")) as client:
        listed = await client.get(
            "/api/v1/chat-sessions",
            params={"engagement_id": "project-a", "recent_first": "true"},
            headers=headers,
        )
        recovered = await client.get("/api/v1/chat-sessions/recovery", headers=headers)
    assert listed.status_code == recovered.status_code == 200
    by_id = {item["id"]: item for item in listed.json()}
    assert by_id["recovery"]["metadata"]["butler_initiated"] is True
    assert recovered.json()["metadata"]["butler_initiated"] is True
    assert "butler_initiated" not in by_id["ordinary"]["metadata"]
    assert "butler_initiated" not in (await store.get(ChatSession, "recovery")).metadata


pytestmark = pytest.mark.anyio
