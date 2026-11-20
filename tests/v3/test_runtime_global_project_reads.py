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
from tests.v3.test_runtime_global_reads import AUTH

pytest_plugins = (
    "tests.v3.test_runtime_coordination",
    "tests.v3.test_runtime_global_reads",
)

pytestmark = pytest.mark.anyio


def anyio_backend():
    return "asyncio"


@pytest.fixture
async def project_reads(reads):
    return reads


async def test_ownerless_child_work_and_chat_reads_stay_project_scoped(project_reads):
    parent = await project_reads.store.create(Engagement(name="Parent"))
    child = await project_reads.store.create(
        Engagement(name="Child", parent_engagement_id=parent.id)
    )
    other = await project_reads.store.create(Engagement(name="Other"))

    child_item = await project_reads.store.create(
        WorkItem(engagement_id=child.id, title="Child work")
    )
    await project_reads.store.create(
        WorkItem(engagement_id=other.id, title="Other work")
    )
    update = await project_reads.store.create(
        WorkUpdate(
            engagement_id=child.id,
            item_id=child_item.id,
            summary="Child update",
            status="in_progress",
            actor_kind="operator",
            actor_id="operator",
        )
    )
    chat = await project_reads.store.create(
        ChatSession(
            engagement_id=child.id,
            title="Child chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    await project_reads.store.create(
        ChatTurn(
            engagement_id=child.id,
            session_id=chat.id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.INTERRUPTED,
        )
    )
    await project_reads.store.create(
        ChatMessage(
            engagement_id=child.id,
            session_id=chat.id,
            sequence=1,
            role="user",
            content="A child-only searchable phrase",
        )
    )

    work = await project_reads.client.get(
        f"/api/v1/engagements/{child.id}/work", headers=AUTH
    )
    assert work.status_code == 200, work.text
    assert [item["id"] for item in work.json()] == [child_item.id]

    updates = await project_reads.client.get(
        f"/api/v1/engagements/{child.id}/work/{child_item.id}/updates",
        headers=AUTH,
    )
    assert updates.status_code == 200, updates.text
    assert [item["id"] for item in updates.json()] == [update.id]

    sessions = await project_reads.client.get(
        "/api/v1/chat-sessions", params={"engagement_id": child.id}, headers=AUTH
    )
    assert sessions.status_code == 200, sessions.text
    assert [item["id"] for item in sessions.json()] == [chat.id]

    activity = await project_reads.client.get(
        "/api/v1/chat/session-activity",
        params={"engagement_id": child.id},
        headers=AUTH,
    )
    assert activity.status_code == 200, activity.text
    assert activity.json() == [
        {"session_id": chat.id, "state": "idle", "turn_id": None}
    ]
    assert activity.headers["etag"]
    unchanged = await project_reads.client.get(
        "/api/v1/chat/session-activity",
        params={"engagement_id": child.id},
        headers={**AUTH, "If-None-Match": activity.headers["etag"]},
    )
    assert unchanged.status_code == 304

    for _ in range(2):
        await project_reads.store.create(
            ChatTurn(
                engagement_id=child.id,
                session_id=chat.id,
                provider_profile_id="provider",
                model="model",
                status=ChatTurnStatus.QUEUED,
            )
        )
    duplicate_turns = await project_reads.client.get(
        "/api/v1/chat/session-activity",
        params={"engagement_id": child.id},
        headers=AUTH,
    )
    assert duplicate_turns.status_code == 409

    search = await project_reads.client.get(
        f"/api/v1/chat/projects/{child.id}/search",
        params={"q": "child-only searchable"},
        headers=AUTH,
    )
    assert search.status_code == 200, search.text
    assert [item["session_id"] for item in search.json()["items"]] == [chat.id]

    async with project_reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )


async def test_ownerless_project_chat_search_excludes_other_projects_and_private_chats(
    project_reads,
):
    project = await project_reads.store.create(Engagement(name="Search project"))
    other = await project_reads.store.create(Engagement(name="Other search project"))
    visible = await project_reads.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Visible",
            provider_profile_id="provider",
            model="model",
        )
    )
    hidden = await project_reads.store.create(
        ChatSession(
            engagement_id=project.id,
            title="Side chat",
            provider_profile_id="provider",
            model="model",
            metadata={"side_chat": True},
        )
    )
    foreign = await project_reads.store.create(
        ChatSession(
            engagement_id=other.id,
            title="Foreign",
            provider_profile_id="provider",
            model="model",
        )
    )
    for session in (visible, hidden, foreign):
        await project_reads.store.create(
            ChatMessage(
                engagement_id=session.engagement_id,
                session_id=session.id,
                sequence=1,
                role="user",
                content="distinct needle",
            )
        )

    response = await project_reads.client.get(
        f"/api/v1/chat/projects/{project.id}/search",
        params={"q": "needle"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert [item["session_id"] for item in response.json()["items"]] == [visible.id]

    wrong_project = await project_reads.client.get(
        f"/api/v1/chat/projects/{project.id}/search",
        params={"session_id": foreign.id},
        headers=AUTH,
    )
    assert wrong_project.status_code == 404


async def test_linked_project_chats_page_active_first_and_skip_empty_projects(
    project_reads,
):
    store = project_reads.store
    parent = await store.create(Engagement(name="Program"))
    for index in range(40):
        await store.create(
            Engagement(name=f"Empty {index:02d}", parent_engagement_id=parent.id)
        )
    busy = await store.create(
        Engagement(name="Busy child", parent_engagement_id=parent.id)
    )
    waiting_child = await store.create(
        Engagement(name="Approvals child", parent_engagement_id=parent.id)
    )
    grandchild = await store.create(
        Engagement(name="Grandchild", parent_engagement_id=busy.id)
    )
    other = await store.create(Engagement(name="Unrelated"))

    async def chat(project, title, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider",
                model="model",
                metadata=metadata,
            )
        )

    # The oldest chat waits for an approval; it must still lead page one.
    needs_you = await chat(waiting_child, "Needs approval")
    running = await chat(busy, "Running chat")
    for session, status in (
        (needs_you, ChatTurnStatus.WAITING_APPROVAL),
        (running, ChatTurnStatus.QUEUED),
    ):
        await store.create(
            ChatTurn(
                engagement_id=session.engagement_id,
                session_id=session.id,
                provider_profile_id="provider",
                model="model",
                status=status,
            )
        )
    idle = [await chat(busy, f"Idle chat {index:02d}") for index in range(12)]
    await chat(busy, "Hidden side chat", side_chat=True)
    await chat(parent, "Parent-owned chat")
    await chat(grandchild, "Grandchild chat")
    await chat(other, "Unrelated chat")

    seen = []
    offset = 0
    pages = 0
    while offset is not None:
        answer = await project_reads.client.get(
            f"/api/v1/chat/projects/{parent.id}/linked-chats",
            params={"offset": offset, "limit": 5},
            headers=AUTH,
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        seen.extend(body["items"])
        offset = body["next_offset"]
        pages += 1
    assert pages == 3
    assert [(item["session"]["id"], item["state"]) for item in seen[:2]] == [
        (needs_you.id, "waiting"),
        (running.id, "working"),
    ]
    assert seen[0]["project"] == {"id": waiting_child.id, "name": "Approvals child"}
    assert seen[0]["turn_id"]
    assert [item["session"]["id"] for item in seen[2:]] == [
        session.id for session in reversed(idle)
    ]
    assert {item["state"] for item in seen[2:]} == {"idle"}

    for query, expected in (
        ("idle chat 03", [idle[3].id]),
        ("approvals", [needs_you.id]),
        ("side chat", []),
    ):
        found = await project_reads.client.get(
            f"/api/v1/chat/projects/{parent.id}/linked-chats",
            params={"q": query},
            headers=AUTH,
        )
        assert found.status_code == 200, found.text
        assert [item["session"]["id"] for item in found.json()["items"]] == expected

    first = await project_reads.client.get(
        f"/api/v1/chat/projects/{parent.id}/linked-chats", headers=AUTH
    )
    assert first.headers["etag"]
    unchanged = await project_reads.client.get(
        f"/api/v1/chat/projects/{parent.id}/linked-chats",
        headers={**AUTH, "If-None-Match": first.headers["etag"]},
    )
    assert unchanged.status_code == 304

    empty = await project_reads.client.get(
        f"/api/v1/chat/projects/{other.id}/linked-chats", headers=AUTH
    )
    assert empty.json() == {"items": [], "next_offset": None}

    async with project_reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )


async def test_butler_board_lists_every_project_needing_you_first(project_reads):
    from nebula.v3.domain import Approval, ChatGoal, EngagementStatus, RiskClass

    store = project_reads.store
    research = await store.create(Engagement(name="Research"))
    bounty = await store.create(Engagement(name="Bounty"))
    archived = await store.create(
        Engagement(name="Old", status=EngagementStatus.ARCHIVED)
    )

    async def chat(project, title, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider",
                model="model",
                metadata=metadata,
            )
        )

    async def turn(session, status, **fields):
        return await store.create(
            ChatTurn(
                engagement_id=session.engagement_id,
                session_id=session.id,
                provider_profile_id="provider",
                model="model",
                status=status,
                **fields,
            )
        )

    waiting = await chat(research, "Kernel fuzz")
    approval = await store.create(
        Approval(
            engagement_id=research.id,
            run_id="chat-run",
            origin="chat",
            chat_session_id=waiting.id,
            risk_class=RiskClass.HIGH_IMPACT_ACTION,
            exact_request={
                "tool_name": "run_command",
                "arguments": {"command": "make install"},
            },
            policy_rationale="Installs on the host",
            requested_by="model",
        )
    )
    await turn(waiting, ChatTurnStatus.WAITING_APPROVAL, approval_id=approval.id)
    working = await chat(bounty, "Fuzz parser")
    await turn(working, ChatTurnStatus.ROUTING)
    await store.create(
        ChatGoal(
            engagement_id=bounty.id,
            session_id=working.id,
            objective="Find a crash",
            completion_criteria=["A reproducible crash"],
            plan=["Build", "Fuzz", "Reproduce"],
            current_step=1,
        )
    )
    failed = await chat(bounty, "Symbol fetch")
    await turn(failed, ChatTurnStatus.FAILED, error="Download timed out")
    recovered = await chat(bounty, "Recovered later")
    await turn(recovered, ChatTurnStatus.FAILED, error="old failure")
    await turn(recovered, ChatTurnStatus.COMPLETE)
    idle = [await chat(research, f"Idle {index}") for index in range(3)]
    await chat(research, "Butler", control_assistant=True)
    await chat(research, "Side", side_chat=True)
    await chat(research, "Archived chat", archived_at="2026-11-01T00:00:00Z")
    hidden = await chat(archived, "Archived project chat")
    await turn(hidden, ChatTurnStatus.ROUTING)

    answer = await project_reads.client.get("/api/v1/butler/board", headers=AUTH)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["counts"] == {"waiting": 1, "working": 1, "failed": 1, "idle": 4}
    assert [(item["title"], item["state"]) for item in body["items"]] == [
        ("Kernel fuzz", "waiting"),
        ("Fuzz parser", "working"),
        ("Symbol fetch", "failed"),
    ]
    first, second, third = body["items"]
    assert first["project"] == {"id": research.id, "name": "Research"}
    assert first["attention"]["kind"] == "approval"
    assert first["attention"]["action_id"] == approval.id
    assert first["now"] == "Approve run_command: make install"
    assert second["now"] == "Working"
    assert second["goal"] == {
        "objective": "Find a crash",
        "status": "draft",
        "current_step": 1,
        "plan_length": 3,
        "revision": 1,
    }
    assert second["started_by"] is None
    assert third["now"] == "Download timed out"
    assert body["truncated"] is False

    idle_page = await project_reads.client.get(
        "/api/v1/butler/board",
        params={"state": "idle", "limit": 2},
        headers=AUTH,
    )
    assert idle_page.status_code == 200, idle_page.text
    first_page = idle_page.json()
    assert len(first_page["items"]) == 2 and first_page["next_offset"] == 2
    rest = await project_reads.client.get(
        "/api/v1/butler/board",
        params={"state": "idle", "limit": 2, "offset": 2},
        headers=AUTH,
    )
    seen = {item["session_id"] for item in first_page["items"] + rest.json()["items"]}
    assert seen == {recovered.id, *(item.id for item in idle)}
    assert rest.json()["next_offset"] is None

    found = await project_reads.client.get(
        "/api/v1/butler/board", params={"q": "bounty"}, headers=AUTH
    )
    assert [item["title"] for item in found.json()["items"]] == [
        "Fuzz parser",
        "Symbol fetch",
    ]

    unchanged = await project_reads.client.get(
        "/api/v1/butler/board",
        headers={**AUTH, "If-None-Match": answer.headers["etag"]},
    )
    assert unchanged.status_code == 304

    async with project_reads.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )


async def test_butler_threads_list_every_project_with_state(project_reads):
    from nebula.v3.domain import EngagementStatus

    store = project_reads.store
    research = await store.create(Engagement(name="Research"))
    bounty = await store.create(Engagement(name="Bounty"))
    archived = await store.create(
        Engagement(name="Old", status=EngagementStatus.ARCHIVED)
    )

    async def thread(project, title, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider",
                model="model",
                metadata={"control_assistant": True, "butler_thread": True, **metadata},
            )
        )

    idle = await thread(research, "Which chats need me?")
    working = await thread(bounty, "Pause the fuzzers")
    await store.create(
        ChatTurn(
            engagement_id=bounty.id,
            session_id=working.id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.ROUTING,
        )
    )
    await thread(research, "Archived thread", archived_at="2026-11-01T00:00:00Z")
    await thread(archived, "In an archived project")
    # The project's current Butler conversation is not a thread.
    await store.create(
        ChatSession(
            engagement_id=research.id,
            title="Butler",
            provider_profile_id="provider",
            model="model",
            metadata={"control_assistant": True},
        )
    )

    answer = await project_reads.client.get("/api/v1/butler/threads", headers=AUTH)
    assert answer.status_code == 200, answer.text
    items = {item["session_id"]: item for item in answer.json()["items"]}
    assert set(items) == {idle.id, working.id}
    assert items[working.id]["state"] == "working"
    assert items[working.id]["turn_id"]
    assert items[working.id]["project"] == {"id": bounty.id, "name": "Bounty"}
    assert items[idle.id]["state"] == "idle"
