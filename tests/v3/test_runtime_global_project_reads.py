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


async def test_butler_board_lists_goal_chats_in_three_sections(project_reads):
    from nebula.v3.domain import (
        Approval,
        ChatGoal,
        ChatGoalStatus,
        EngagementStatus,
        RiskClass,
    )

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

    async def goal(session, status, **fields):
        if status == ChatGoalStatus.COMPLETED:
            fields.setdefault("completion_summary", "Done")
            fields.setdefault("completion_evidence", [{"kind": "note"}])
        if status == ChatGoalStatus.BLOCKED:
            fields.setdefault("blocked_reason", "Needs a device")
        return await store.create(
            ChatGoal(
                engagement_id=session.engagement_id,
                session_id=session.id,
                objective=f"Goal of {session.title}",
                completion_criteria=["Report"],
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
    await goal(waiting, ChatGoalStatus.RUNNING)
    working = await chat(bounty, "Fuzz parser")
    await turn(working, ChatTurnStatus.ROUTING)
    await goal(
        working,
        ChatGoalStatus.RUNNING,
        plan=["Build", "Fuzz", "Reproduce"],
        current_step=1,
    )
    failed = await chat(bounty, "Symbol fetch")
    await turn(failed, ChatTurnStatus.FAILED, error="Download timed out")
    await goal(failed, ChatGoalStatus.RUNNING)
    between = await chat(bounty, "Between steps")
    await turn(between, ChatTurnStatus.FAILED, error="old failure")
    await turn(between, ChatTurnStatus.COMPLETE)
    await goal(between, ChatGoalStatus.RUNNING)
    paused = await chat(research, "Paused triage")
    await goal(paused, ChatGoalStatus.PAUSED)
    blocked = await chat(bounty, "Blocked repro")
    await goal(blocked, ChatGoalStatus.BLOCKED)
    long_block = await chat(bounty, "Long block")
    await goal(long_block, ChatGoalStatus.BLOCKED, blocked_reason="x " * 60)
    proposal = await chat(research, "Proposed sweep", proposed_by="butler")
    await goal(proposal, ChatGoalStatus.DRAFT)
    completed = [await chat(research, f"Done {index}") for index in range(3)]
    for item in completed:
        await goal(item, ChatGoalStatus.COMPLETED)
    cancelled = await chat(bounty, "Dropped idea")
    await goal(cancelled, ChatGoalStatus.CANCELLED)
    # Never listed: chats without a goal, an operator's unstarted draft,
    # Butler's own chats, side chats, subagents and archived chats or projects.
    no_goal = await chat(research, "No goal at all")
    await turn(no_goal, ChatTurnStatus.ROUTING)
    await goal(await chat(research, "Operator draft"), ChatGoalStatus.DRAFT)
    await goal(
        await chat(research, "Butler", control_assistant=True),
        ChatGoalStatus.RUNNING,
    )
    await goal(await chat(research, "Side", side_chat=True), ChatGoalStatus.RUNNING)
    await goal(
        await chat(research, "Child", subagent_id="subagent-1"),
        ChatGoalStatus.RUNNING,
    )
    await goal(
        await chat(research, "Archived chat", archived_at="2026-11-01T00:00:00Z"),
        ChatGoalStatus.RUNNING,
    )
    hidden = await chat(archived, "Archived project chat")
    await turn(hidden, ChatTurnStatus.ROUTING)
    await goal(hidden, ChatGoalStatus.RUNNING)

    answer = await project_reads.client.get("/api/v1/butler/board", headers=AUTH)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["counts"] == {"active": 4, "idle": 4, "completed": 4}
    assert body["states"] == {"waiting": 2, "working": 1, "failed": 1}
    assert body["projects"] == [
        {
            "id": bounty.id,
            "name": "Bounty",
            "counts": {"active": 3, "idle": 2, "completed": 1},
        },
        {
            "id": research.id,
            "name": "Research",
            "counts": {"active": 1, "idle": 2, "completed": 3},
        },
    ]
    assert [
        (item["title"], item["section"], item["state"]) for item in body["items"]
    ] == [
        ("Kernel fuzz", "active", "waiting"),
        ("Fuzz parser", "active", "working"),
        ("Symbol fetch", "active", "failed"),
        ("Between steps", "active", "idle"),
        ("Proposed sweep", "idle", "waiting"),
        ("Long block", "idle", "idle"),
        ("Blocked repro", "idle", "idle"),
        ("Paused triage", "idle", "idle"),
    ]
    items = {item["title"]: item for item in body["items"]}
    first = items["Kernel fuzz"]
    assert first["project"] == {"id": research.id, "name": "Research"}
    assert first["attention"]["kind"] == "approval"
    assert first["attention"]["action_id"] == approval.id
    assert first["now"] == "Approve run_command: make install"
    assert items["Fuzz parser"]["now"] == "Working"
    assert items["Fuzz parser"]["goal"] == {
        "objective": "Goal of Fuzz parser",
        "status": "running",
        "current_step": 1,
        "plan_length": 3,
        "revision": 1,
    }
    assert items["Symbol fetch"]["now"] == "Download timed out"
    assert items["Between steps"]["now"] is None
    assert items["Proposed sweep"]["attention"]["kind"] == "proposal"
    assert items["Paused triage"]["now"] == "Paused"
    assert items["Blocked repro"]["now"] == "Blocked: Needs a device"
    assert items["Long block"]["now"] == "Blocked"
    assert body["section"] is None and body["next_offset"] is None
    assert body["truncated"] is False

    # Completed pages on request, in total or in one project.
    page = await project_reads.client.get(
        "/api/v1/butler/board",
        params={"section": "completed", "limit": 2},
        headers=AUTH,
    )
    assert page.status_code == 200, page.text
    first_page = page.json()
    assert first_page["section"] == "completed"
    assert first_page["counts"] == body["counts"]
    assert len(first_page["items"]) == 2 and first_page["next_offset"] == 2
    rest = await project_reads.client.get(
        "/api/v1/butler/board",
        params={"section": "completed", "limit": 2, "offset": 2},
        headers=AUTH,
    )
    assert rest.json()["next_offset"] is None
    seen = first_page["items"] + rest.json()["items"]
    assert {item["session_id"] for item in seen} == {
        cancelled.id,
        *(item.id for item in completed),
    }
    assert {item["now"] for item in seen} == {"Completed", "Cancelled"}
    assert {item["goal"]["status"] for item in seen} == {"completed", "cancelled"}
    in_bounty = await project_reads.client.get(
        "/api/v1/butler/board",
        params={"section": "completed", "project_id": bounty.id},
        headers=AUTH,
    )
    assert [item["title"] for item in in_bounty.json()["items"]] == ["Dropped idea"]
    active_page = await project_reads.client.get(
        "/api/v1/butler/board",
        params={"section": "active", "limit": 3},
        headers=AUTH,
    )
    assert [item["title"] for item in active_page.json()["items"]] == [
        "Kernel fuzz",
        "Fuzz parser",
        "Symbol fetch",
    ]
    assert active_page.json()["next_offset"] == 3
    # The older spelling still pages Idle.
    legacy = await project_reads.client.get(
        "/api/v1/butler/board", params={"state": "idle"}, headers=AUTH
    )
    assert [item["section"] for item in legacy.json()["items"]] == ["idle"] * 4

    found = await project_reads.client.get(
        "/api/v1/butler/board", params={"q": "bounty"}, headers=AUTH
    )
    assert found.json()["counts"] == {"active": 3, "idle": 2, "completed": 1}
    assert [item["title"] for item in found.json()["items"]] == [
        "Fuzz parser",
        "Symbol fetch",
        "Between steps",
        "Long block",
        "Blocked repro",
    ]
    by_title = await project_reads.client.get(
        "/api/v1/butler/board", params={"q": "no goal"}, headers=AUTH
    )
    assert by_title.json()["items"] == [] and by_title.json()["projects"] == []

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


async def test_butler_case_stage_reaches_case_chats_and_goal_cards(project_reads):
    from datetime import timedelta

    from nebula.v3.butler_case_stage import cases_for_goal_chats
    from nebula.v3.domain import ButlerRecoveryCase, ChatGoal, ChatGoalStatus, utc_now

    store = project_reads.store
    project = await store.create(Engagement(name="Bounty"))

    async def chat(title, **metadata):
        return await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id="provider",
                model="model",
                metadata=metadata,
            )
        )

    async def goal(session):
        await store.create(
            ChatGoal(
                engagement_id=project.id,
                session_id=session.id,
                objective=f"Goal of {session.title}",
                completion_criteria=["Report"],
                status=ChatGoalStatus.RUNNING,
            )
        )

    sequence = iter(range(1, 100))

    async def case(source, status, **fields):
        number = next(sequence)
        return await store.create(
            ButlerRecoveryCase(
                id=f"case-{number}",
                engagement_id=project.id,
                trigger="immediate",
                signal="goal.blocked",
                source_event_id=f"event-{number}",
                source_event_sequence=number,
                source_session_id=source.id if source else None,
                authority_revision=1,
                summary="Goal blocked",
                status=status,
                **fields,
            )
        )

    butler = await chat("Butler", control_assistant=True)
    blocked = await chat("Blocked goal")
    await goal(blocked)
    repaired = await chat("Repaired goal")
    await goal(repaired)
    dropped = await chat("Dropped case goal")
    await goal(dropped)
    plain = await chat("Quiet goal")
    await goal(plain)

    # A case Butler resolved earlier is superseded by the newer open one.
    await case(blocked, "resolved")
    open_case = await case(blocked, "needs_attention")
    investigation = await chat(
        "Butler check · Goal blocked",
        control_assistant=True,
        butler_initiated=True,
        butler_recovery_case_id=open_case.id,
    )
    open_case = await store.update(
        ButlerRecoveryCase,
        open_case.id,
        {"investigation_session_id": investigation.id},
        expected_revision=open_case.revision,
    )
    resolved = await case(repaired, "resolved")
    await case(dropped, "cancelled")
    orphan = await chat(
        "Butler check · deleted case",
        control_assistant=True,
        butler_initiated=True,
        butler_recovery_case_id="case-missing",
    )

    listed = await project_reads.client.get(
        "/api/v1/chat-sessions",
        params={"engagement_id": project.id},
        headers=AUTH,
    )
    single = await project_reads.client.get(
        f"/api/v1/chat-sessions/{investigation.id}", headers=AUTH
    )
    assert listed.status_code == single.status_code == 200, listed.text
    by_id = {item["id"]: item["metadata"] for item in listed.json()}
    assert by_id[investigation.id]["butler_case_status"] == "needs_attention"
    assert single.json()["metadata"]["butler_case_status"] == "needs_attention"
    # Only case chats carry a stage, and a missing case leaves none.
    assert "butler_case_status" not in by_id[butler.id]
    assert "butler_case_status" not in by_id[orphan.id]
    assert "butler_case_status" not in by_id[blocked.id]
    # The stage is read-time only; nothing is written to the chat.
    stored = await store.get(ChatSession, investigation.id)
    assert "butler_case_status" not in stored.metadata

    board = await project_reads.client.get("/api/v1/butler/board", headers=AUTH)
    assert board.status_code == 200, board.text
    cards = {item["session_id"]: item for item in board.json()["items"]}
    assert cards[blocked.id]["butler_case"] == {
        "id": open_case.id,
        "status": "needs_attention",
        "session_id": investigation.id,
        "updated_at": cards[blocked.id]["butler_case"]["updated_at"],
    }
    assert cards[repaired.id]["butler_case"]["status"] == "resolved"
    assert cards[dropped.id]["butler_case"] is None
    assert cards[plain.id]["butler_case"] is None

    # A resolved case stops showing on its goal after a day; open ones stay.
    async with store.database.session() as database:
        later = await cases_for_goal_chats(
            database,
            [blocked.id, repaired.id],
            [project.id],
            now=resolved.updated_at + timedelta(hours=25),
        )
    assert set(later) == {blocked.id}
    assert later[blocked.id]["status"] == "needs_attention"
    assert utc_now() - resolved.updated_at < timedelta(hours=1)
