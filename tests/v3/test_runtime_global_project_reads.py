"""Ownerless child projects retain their durable Work and chat read views."""

import pytest
from sqlalchemy import func, select

from nebula.v3.database import ProjectOwnershipRow, RuntimeCommandRow
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    WorkItem,
    WorkUpdate,
)
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_global_reads import AUTH, reads

pytestmark = pytest.mark.anyio


def anyio_backend():
    return "asyncio"


async def test_ownerless_child_work_and_chat_reads_stay_project_scoped(reads):
    parent = await reads.store.create(Engagement(name="Parent"))
    child = await reads.store.create(
        Engagement(name="Child", parent_engagement_id=parent.id)
    )
    other = await reads.store.create(Engagement(name="Other"))

    child_item = await reads.store.create(
        WorkItem(engagement_id=child.id, title="Child work")
    )
    await reads.store.create(WorkItem(engagement_id=other.id, title="Other work"))
    update = await reads.store.create(
        WorkUpdate(
            engagement_id=child.id,
            item_id=child_item.id,
            summary="Child update",
            status="in_progress",
            actor_kind="operator",
            actor_id="operator",
        )
    )
    chat = await reads.store.create(
        ChatSession(
            engagement_id=child.id,
            title="Child chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    await reads.store.create(
        ChatTurn(
            engagement_id=child.id,
            session_id=chat.id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.INTERRUPTED,
        )
    )
    await reads.store.create(
        ChatMessage(
            engagement_id=child.id,
            session_id=chat.id,
            sequence=1,
            role="user",
            content="A child-only searchable phrase",
        )
    )

    work = await reads.client.get(f"/api/v1/engagements/{child.id}/work", headers=AUTH)
    assert work.status_code == 200, work.text
    assert [item["id"] for item in work.json()] == [child_item.id]

    updates = await reads.client.get(
        f"/api/v1/engagements/{child.id}/work/{child_item.id}/updates",
        headers=AUTH,
    )
    assert updates.status_code == 200, updates.text
    assert [item["id"] for item in updates.json()] == [update.id]

    sessions = await reads.client.get(
        "/api/v1/chat-sessions", params={"engagement_id": child.id}, headers=AUTH
    )
    assert sessions.status_code == 200, sessions.text
    assert [item["id"] for item in sessions.json()] == [chat.id]

    activity = await reads.client.get(
        "/api/v1/chat/session-activity",
        params={"engagement_id": child.id},
        headers=AUTH,
    )
    assert activity.status_code == 200, activity.text
    assert activity.json() == [
        {"session_id": chat.id, "state": "idle", "turn_id": None}
    ]
    assert activity.headers["etag"]
    unchanged = await reads.client.get(
        "/api/v1/chat/session-activity",
        params={"engagement_id": child.id},
        headers={**AUTH, "If-None-Match": activity.headers["etag"]},
    )
    assert unchanged.status_code == 304

    for _ in range(2):
        await reads.store.create(
            ChatTurn(
                engagement_id=child.id,
                session_id=chat.id,
                provider_profile_id="provider",
                model="model",
                status=ChatTurnStatus.QUEUED,
            )
        )
    duplicate_turns = await reads.client.get(
        "/api/v1/chat/session-activity",
        params={"engagement_id": child.id},
        headers=AUTH,
    )
    assert duplicate_turns.status_code == 409

    search = await reads.client.get(
        f"/api/v1/chat/projects/{child.id}/search",
        params={"q": "child-only searchable"},
        headers=AUTH,
    )
    assert search.status_code == 200, search.text
    assert [item["session_id"] for item in search.json()["items"]] == [chat.id]

    async with reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )


async def test_ownerless_project_chat_search_excludes_other_projects_and_private_chats(
    reads,
):
    project = await reads.store.create(Engagement(name="Search project"))
    other = await reads.store.create(Engagement(name="Other search project"))
    visible = await reads.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Visible",
            provider_profile_id="provider",
            model="model",
        )
    )
    hidden = await reads.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Side chat",
            provider_profile_id="provider",
            model="model",
            metadata={"side_chat": True},
        )
    )
    foreign = await reads.store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign",
            provider_profile_id="provider",
            model="model",
        )
    )
    for session in (visible, hidden, foreign):
        await reads.store.create(
            ChatMessage(
                engagement_id=session.engagement_id,
                session_id=session.id,
                sequence=1,
                role="user",
                content="distinct needle",
            )
        )

    response = await reads.client.get(
        f"/api/v1/chat/projects/{project.id}/search",
        params={"q": "needle"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert [item["session_id"] for item in response.json()["items"]] == [visible.id]

    wrong_project = await reads.client.get(
        f"/api/v1/chat/projects/{project.id}/search",
        params={"session_id": foreign.id},
        headers=AUTH,
    )
    assert wrong_project.status_code == 404
