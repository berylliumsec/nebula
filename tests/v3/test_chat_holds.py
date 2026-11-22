"""A chat hold freezes chosen chats while the rest of the project keeps working.

When the operator's priorities shift they hold the research in progress, start
new goals in the same project, and later release exactly what they held. These
tests drive the real goal, chat and hold services on a real store.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from datetime import timedelta

import pytest

from nebula.v3.chat import ChatService, ProjectHeld
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ProjectHold,
    utc_now,
)
from nebula.v3.project_hold_gate import HOLD_MARKER, ProjectHoldConflict, is_held
from nebula.v3.project_holds import (
    ChatHoldPlace,
    ChatHoldRelease,
    HoldPlace,
    HoldRelease,
)
from nebula.v3.chat_goals import GoalWrite
from tests.v3.test_chat import FakeProvider
from tests.v3.test_project_holds import _goal, _setup

pytestmark = pytest.mark.anyio


async def _chat(store, session_id, parent=None, **metadata) -> ChatSession:
    return await store.create(
        ChatSession(
            id=session_id,
            engagement_id="project",
            title=session_id,
            provider_profile_id="provider",
            model="model",
            parent_session_id=parent,
            metadata=metadata,
        )
    )


def _chat_service(store) -> ChatService:
    return ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=False)
    )


async def test_chat_hold_freezes_only_that_chat_and_new_work_still_starts(tmp_path):
    store, goals, service, recorder = await _setup(tmp_path)
    held_goal = await _goal(store)
    other_goal = await _goal(store, "other")
    chat = _chat_service(store)

    batch_id, holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session"], reason="New priority")
    )

    assert [hold.session_id for hold in holds] == ["session"]
    assert holds[0].batch_id == batch_id
    paused = await goals.get("session")
    assert paused.status == ChatGoalStatus.PAUSED
    assert paused.metadata[HOLD_MARKER] == holds[0].id
    assert [item.goal_id for item in holds[0].paused_goals] == [held_goal.id]
    # The other chat keeps running and the project stays open for new chats.
    assert (await goals.get("other")).status == ChatGoalStatus.RUNNING
    assert other_goal.id not in {item.goal_id for item in holds[0].paused_goals}
    await chat.require_project_open("project")
    await chat.require_project_open("project", "other")
    view = await service.view("project")
    assert view["held"] is False
    assert [item["session_id"] for item in view["chat_holds"]] == ["session"]

    # The held chat refuses new turns and goal resumes from anyone.
    with pytest.raises(ProjectHeld, match="chat is on hold"):
        await chat.require_project_open("project", "session")
    with pytest.raises(ProjectHoldConflict):
        await goals.write(
            "session", GoalWrite(expected_revision=paused.revision, action="resume")
        )
    assert (await service.chat_view("session"))["held"] is True
    assert (await service.chat_view("other"))["held"] is False

    released = await service.release_chats(
        "project", ChatHoldRelease(batch_id=batch_id)
    )

    assert [hold.status for hold in released] == ["released"]
    resumed = await goals.get("session")
    assert resumed.status == ChatGoalStatus.RUNNING
    assert HOLD_MARKER not in resumed.metadata
    assert recorder.dispatched == ["session"]
    await chat.require_project_open("project", "session")


async def test_chat_hold_covers_subagent_and_goal_children_but_not_forks(tmp_path):
    store, _goals, service, _recorder = await _setup(tmp_path)
    await _chat(store, "subagent", parent="session", subagent_id="sub-1")
    await _chat(store, "grandchild", parent="subagent", subagent_id="sub-2")
    await _chat(
        store, "goal-child", parent="session", delegated_from_session_id="session"
    )
    await _chat(store, "fork", parent="session", forked_from_message_id="message")

    await service.place_chats("project", ChatHoldPlace(session_ids=["session"]))

    for covered in ("session", "subagent", "grandchild", "goal-child"):
        assert await is_held(store, "project", covered), covered
    assert not await is_held(store, "project", "fork")
    # Callers that do not name a chat see only project-wide holds.
    assert not await is_held(store, "project")


async def test_child_goals_are_marked_and_restored_with_their_parent(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    parent = await _goal(store)
    await _chat(
        store, "goal-child", parent="session", delegated_from_session_id="session"
    )
    child = await store.create(
        ChatGoal(
            engagement_id="project",
            session_id="goal-child",
            parent_goal_id=parent.id,
            objective="Inspect a part",
            completion_criteria=["Done"],
            status=ChatGoalStatus.RUNNING,
            started_at=utc_now(),
            active_since=utc_now(),
        )
    )
    await store.update(
        ChatGoal,
        parent.id,
        {"child_session_ids": ["goal-child"]},
        expected_revision=parent.revision,
    )

    batch_id, holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session"])
    )

    assert {item.goal_id for item in holds[0].paused_goals} == {parent.id, child.id}
    assert (await goals.get("goal-child")).metadata[HOLD_MARKER] == holds[0].id

    await service.release_chats("project", ChatHoldRelease(batch_id=batch_id))

    assert (await goals.get("session")).status == ChatGoalStatus.RUNNING
    assert (await goals.get("goal-child")).status == ChatGoalStatus.RUNNING


async def test_hold_all_active_takes_each_busy_top_chat_once(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    await _goal(store)
    await _goal(store, "idle", status=ChatGoalStatus.PAUSED)
    await _chat(store, "busy")
    await _chat(store, "subagent", parent="busy", subagent_id="sub-1")
    await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="subagent",
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.ROUTING,
        )
    )

    _batch, holds = await service.place_chats("project", ChatHoldPlace(all_active=True))

    assert sorted(hold.session_id for hold in holds) == ["busy", "session"]
    assert not await is_held(store, "project", "idle")
    # Holding the same chat again returns its existing hold.
    _again, repeat = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session"])
    )
    assert repeat[0].id in {hold.id for hold in holds}


async def test_release_one_chat_of_a_batch_keeps_the_rest_held(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    await _goal(store)
    await _goal(store, "other")
    batch_id, _holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session", "other"])
    )

    await service.release_chats("project", ChatHoldRelease(session_ids=["other"]))

    assert (await goals.get("other")).status == ChatGoalStatus.RUNNING
    assert (await goals.get("session")).status == ChatGoalStatus.PAUSED
    assert await is_held(store, "project", "session")

    await service.release_chats("project", ChatHoldRelease(batch_id=batch_id))
    assert (await goals.get("session")).status == ChatGoalStatus.RUNNING


async def test_goal_moves_to_the_hold_that_still_covers_it(tmp_path):
    store, goals, service, _recorder = await _setup(tmp_path)
    await _goal(store)
    batch_id, holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session"])
    )
    project_hold = await service.place("project", HoldPlace(reason="Core deployment"))

    released = await service.release_chats(
        "project", ChatHoldRelease(batch_id=batch_id)
    )

    goal = await goals.get("session")
    assert goal.status == ChatGoalStatus.PAUSED
    assert goal.metadata[HOLD_MARKER] == project_hold.id
    assert released[0].restored_goals[0].outcome == f"still held by {project_hold.id}"

    await service.release("project", project_hold.id, HoldRelease())
    assert (await goals.get("session")).status == ChatGoalStatus.RUNNING
    assert holds[0].id != project_hold.id


async def test_chat_hold_stops_only_its_own_turns_after_grace(tmp_path):
    store, _goals, service, recorder = await _setup(tmp_path)
    await _chat(store, "other")
    turns = {}
    for session_id in ("session", "other"):
        turns[session_id] = await store.create(
            ChatTurn(
                engagement_id="project",
                session_id=session_id,
                provider_profile_id="provider",
                model="model",
                status=ChatTurnStatus.ROUTING,
            )
        )
    _batch, holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session"], grace_seconds=600)
    )
    assert holds[0].status == "holding" and recorder.provider_stops == []

    await store.update(
        ProjectHold,
        holds[0].id,
        {"grace_until": utc_now() - timedelta(seconds=1)},
        expected_revision=holds[0].revision,
    )
    await service.tick("project")

    assert recorder.provider_stops == [turns["session"].id]


async def test_chat_hold_api_and_butler_tools_act_without_confirmation(tmp_path):
    from tests.v3.async_test_support import AsyncTestClient as TestClient
    from tests.v3.test_control_assistant_work import _app
    from nebula.v3.chat_goals import ChatGoalService, GoalCreate
    from nebula.v3.control_assistant_work import dispatch_work_tool
    from nebula.v3.domain import Engagement, ProviderProfile

    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Research"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    chats = []
    goals = ChatGoalService(store)
    for title in ("Old direction", "Also old"):
        chat = await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        draft = await goals.create(
            chat.id, GoalCreate(objective="Inspect", completion_criteria=["Done"])
        )
        await store.update(
            ChatGoal,
            draft.id,
            {
                "status": ChatGoalStatus.RUNNING,
                "started_at": utc_now(),
                "active_since": utc_now(),
            },
            expected_revision=draft.revision,
        )
        chats.append(chat)
    client = TestClient(app)
    auth = {"Authorization": "Bearer test-token"}
    holds_path = f"/api/v1/engagements/{project.id}/holds"
    assert (
        await client.post(holds_path, json={"session_ids": [chats[0].id]})
    ).status_code == 401

    placed = await client.post(
        holds_path, headers=auth, json={"session_ids": [chats[0].id]}
    )
    assert placed.status_code == 200
    body = placed.json()
    assert body["held"] is False and body["batch_id"]
    assert [hold["session_id"] for hold in body["chat_holds"]] == [chats[0].id]
    chat_hold = await client.get(
        f"/api/v1/chat-sessions/{chats[0].id}/hold", headers=auth
    )
    assert chat_hold.json()["held"] is True

    released = await client.post(
        f"{holds_path}/release", headers=auth, json={"batch_id": body["batch_id"]}
    )
    assert released.status_code == 200
    assert released.json()["chat_holds"] == []
    assert (await goals.get(chats[0].id)).status == ChatGoalStatus.RUNNING

    # Butler holds every busy chat in one call and releases them by batch.
    work = app.state.control_assistant_work
    held = await dispatch_work_tool(
        work, project.id, "nebula.work.hold_chats", {"all_active": True}
    )
    assert sorted(held["held_chats"]) == sorted(chat.id for chat in chats)
    for chat in chats:
        assert (await goals.get(chat.id)).status == ChatGoalStatus.PAUSED
    read = await dispatch_work_tool(work, project.id, "nebula.work.holds", {})
    assert len(read["chat_holds"]) == 2
    freed = await dispatch_work_tool(
        work, project.id, "nebula.work.release_chats", {"batch_id": held["batch_id"]}
    )
    assert sorted(freed["released_chats"]) == sorted(chat.id for chat in chats)
    for chat in chats:
        assert (await goals.get(chat.id)).status == ChatGoalStatus.RUNNING

    # The whole project, through Butler, and back.
    whole = await dispatch_work_tool(
        work, project.id, "nebula.work.hold_project", {"reason": "Stop everything"}
    )
    assert whole["project_held"] is True
    back = await dispatch_work_tool(work, project.id, "nebula.work.release_project", {})
    assert back["released"] is True and back["project_held"] is False


async def test_background_butler_never_holds_or_releases_work(tmp_path):
    from tests.v3.test_butler_capabilities import _scheduled_capabilities

    (
        _store,
        project,
        chat,
        _foreign,
        policy,
        _,
        scheduled,
    ) = await _scheduled_capabilities(tmp_path)

    for operation, arguments in (
        ("nebula.work.hold_chats", {"session_ids": [chat.id]}),
        ("nebula.work.release_project", {}),
    ):
        with pytest.raises(ValueError, match="only the operator's Butler"):
            await scheduled.invoke(
                {
                    "operation": "nebula.global.work_change",
                    "arguments": {
                        "project_id": project.id,
                        "expected_authority_revision": policy.revision,
                        "operation": operation,
                        "arguments": arguments,
                    },
                },
                change=True,
            )
    assert not await is_held(_store, project.id, chat.id)


async def test_butler_board_marks_held_chats_with_their_batch(tmp_path):
    from tests.v3.async_test_support import AsyncTestClient as TestClient
    from tests.v3.test_control_assistant_work import _app
    from nebula.v3.chat_goals import ChatGoalService, GoalCreate
    from nebula.v3.domain import Engagement, ProviderProfile

    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Research"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    goals = ChatGoalService(store)
    chats = []
    for title in ("Old direction", "New direction"):
        chat = await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        draft = await goals.create(
            chat.id, GoalCreate(objective=title, completion_criteria=["Done"])
        )
        await store.update(
            ChatGoal,
            draft.id,
            {
                "status": ChatGoalStatus.RUNNING,
                "started_at": utc_now(),
                "active_since": utc_now(),
            },
            expected_revision=draft.revision,
        )
        chats.append(chat)
    client = TestClient(app)
    auth = {"Authorization": "Bearer test-token"}
    placed = await client.post(
        f"/api/v1/engagements/{project.id}/holds",
        headers=auth,
        json={"session_ids": [chats[0].id]},
    )
    batch = placed.json()["batch_id"]

    board = await client.get("/api/v1/butler/board", headers=auth)

    assert board.status_code == 200, board.text
    items = {item["title"]: item for item in board.json()["items"]}
    assert items["Old direction"]["hold"]["batch_id"] == batch
    assert items["Old direction"]["hold"]["reason"] == "Priorities changed"
    assert items["New direction"]["hold"] is None


async def test_a_held_goal_keeps_its_running_turn_until_the_grace_ends(tmp_path):
    """The hold's pause must not fail the goal's in-flight turn.

    It used to clear the goal's execution claim, so the turn failed at its
    next ownership check ("chat goal ownership changed") and, after a quick
    release, paused the goal again with no hold mark.
    """

    from types import SimpleNamespace
    from uuid import uuid4

    from nebula.v3.chat import PreparedChat

    store, goals, service, _recorder = await _setup(tmp_path)
    worker, claim, now = "worker-a", str(uuid4()), utc_now()
    goal = await _goal(
        store,
        execution_owner_id=worker,
        execution_claim_id=claim,
        execution_claimed_at=now,
    )
    turn = await store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.ROUTING,
            execution_owner_id=worker,
            execution_claim_id=claim,
            execution_claimed_at=now,
        )
    )
    prepared = PreparedChat.__new__(PreparedChat)
    prepared.turn = turn
    prepared.execution_claim_id = claim

    async def unchanged(latest):
        return latest

    # Only the ownership check runs; tool bookkeeping is not under test.
    worker_view = SimpleNamespace(
        store=store, worker_id=worker, _reconcile_tool_bookkeeping=unchanged
    )

    batch_id, _holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session"], grace_seconds=600)
    )

    paused = await goals.get("session")
    assert paused.status == ChatGoalStatus.PAUSED
    assert paused.execution_claim_id == claim
    # The running turn passes its ownership check and may finish.
    await ChatService._assert_execution_owner(worker_view, prepared)

    await service.release_chats("project", ChatHoldRelease(batch_id=batch_id))

    resumed = await goals.get("session")
    assert resumed.status == ChatGoalStatus.RUNNING
    assert HOLD_MARKER not in resumed.metadata
    await ChatService._assert_execution_owner(worker_view, prepared)


async def _routing_turns(store, session_ids):
    turns = {}
    for session_id in session_ids:
        if session_id != "session":
            await _chat(store, session_id)
        turns[session_id] = await store.create(
            ChatTurn(
                engagement_id="project",
                session_id=session_id,
                provider_profile_id="provider",
                model="model",
                status=ChatTurnStatus.ROUTING,
            )
        )
    return turns


async def test_placing_and_reading_holds_never_stop_turns(tmp_path):
    """Placement and reads return at once; only Core's pass stops turns.

    A stop waits for its turn to unwind. When placement stopped turns inside
    the request, holding 70 busy chats kept the project's command lane busy
    for 41 minutes and every read of the hold timed out.
    """

    store, _goals, service, recorder = await _setup(tmp_path)
    turns = await _routing_turns(store, ["session", "other"])

    _batch, holds = await service.place_chats(
        "project", ChatHoldPlace(session_ids=["session", "other"], grace_seconds=0)
    )
    await service.view("project")
    await service.chat_view("session")
    assert recorder.provider_stops == []
    assert {hold.status for hold in holds} == {"holding"}

    await service.tick("project")
    assert sorted(recorder.provider_stops) == sorted(t.id for t in turns.values())


async def test_a_pass_stops_turns_together_and_reads_do_not_wait(tmp_path):
    import anyio

    store, _goals, service, _recorder = await _setup(tmp_path)
    turns = await _routing_turns(store, ["session", "a", "b", "c"])
    await service.place_chats(
        "project",
        ChatHoldPlace(session_ids=["session", "a", "b", "c"], grace_seconds=0),
    )
    started: list[str] = []
    release = anyio.Event()

    async def slow_stop(turn_id: str) -> None:
        started.append(turn_id)
        await release.wait()
        turn = await store.get(ChatTurn, turn_id)
        await store.update(
            ChatTurn,
            turn_id,
            {"status": ChatTurnStatus.CANCELLED},
            expected_revision=turn.revision,
        )

    service.stop_provider_turn = slow_stop

    # The periodic pass returns while its stops are still unwinding.
    with anyio.fail_after(5):
        await service.tick_all()
        while len(started) < len(turns):
            await anyio.sleep(0.01)
    # Every stop started before any finished: they run together.
    assert sorted(started) == sorted(t.id for t in turns.values())

    # A read and a second periodic pass neither wait nor stop again.
    with anyio.fail_after(5):
        view = await service.view("project")
        await service.tick_all()
    assert len(view["chat_holds"]) == 4 and len(started) == 4

    release.set()
    with anyio.fail_after(5):
        await service.wait_for_passes()
    view = await service.view("project")
    assert {hold["status"] for hold in view["chat_holds"]} == {"held"}
    assert all(
        [item["outcome"] for item in hold["interrupted_turns"]] == ["stopped"]
        for hold in view["chat_holds"]
    )
