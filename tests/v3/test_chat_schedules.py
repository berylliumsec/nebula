import asyncio
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_schedules import (
    ChatScheduleService,
    ScheduleCreate,
    ScheduleWrite,
)
from nebula.v3.domain import (
    ChatSchedule,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    McpServerProfile,
    McpTransport,
    ProviderProfile,
    SshEnvironment,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider
import nebula.v3.chat as chat_module


def test_schedule_skips_when_a_turn_is_still_active(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Scheduled",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    goals = ChatGoalService(store)
    created = goals.create(
        "session",
        GoalCreate(
            objective="Keep going", completion_criteria=["Scheduled work stays bounded"]
        ),
    )
    goals.write(
        "session", GoalWrite(expected_revision=created.revision, action="start")
    )
    schedules = ChatScheduleService(store)
    schedule = schedules.create("session", ScheduleCreate(interval_seconds=3600))
    store.update(
        type(schedule),
        schedule.id,
        {"next_run_at": utc_now() - timedelta(seconds=1)},
        expected_revision=schedule.revision,
    )
    store.create(
        ChatTurn(
            id="active",
            engagement_id="project",
            session_id="session",
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            request_snapshot={"recovery": {"required": False}, "context_usage": {}},
        )
    )
    monkeypatch.setattr(
        chat_module,
        "provider_from_profile",
        lambda _: FakeProvider(profile.id, local=True),
    )
    asyncio.run(ChatService(store).fire_due_schedules())
    latest = schedules.get("session")
    assert latest.last_status == "skipped"
    assert "still active" in (latest.skip_reason or "")


def test_schedule_http_create_uses_json_body(tmp_path):
    store = NebulaStore(tmp_path / "schedule-http.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            metadata={"default_model": "model-a"},
        )
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Scheduled",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    client = TestClient(create_app(store, auth_token="test-token"))
    created = client.post(
        "/api/v1/chat/sessions/session/schedule",
        headers={"Authorization": "Bearer test-token"},
        json={"interval_seconds": 3600},
    )
    assert created.status_code == 200, created.text
    assert created.json()["interval_seconds"] == 3600


def _scheduled_session(store: NebulaStore, session_id: str = "session"):
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    store.create(
        ChatSession(
            id=session_id,
            engagement_id="project",
            title="Scheduled",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    schedules = ChatScheduleService(store)
    schedule = schedules.create(session_id, ScheduleCreate(interval_seconds=3600))
    return store.update(
        type(schedule),
        schedule.id,
        {"next_run_at": utc_now() - timedelta(seconds=1)},
        expected_revision=schedule.revision,
    )


def test_deleting_a_conversation_removes_its_schedule(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    schedule = _scheduled_session(store)

    store.delete_chat_session("session")

    assert store.list_entities(type(schedule)) == []


def test_orphaned_schedule_does_not_stop_the_scheduler(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    orphan = _scheduled_session(store, "orphan")
    # A schedule row that outlived its conversation (older Core releases left it behind).
    store.delete(ChatSession, "orphan")

    asyncio.run(ChatService(store).fire_due_schedules())

    assert store.list_entities(type(orphan)) == []


def test_schedule_without_its_provider_is_paused_not_fatal(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    schedule = _scheduled_session(store)
    store.delete(ProviderProfile, "provider")

    asyncio.run(ChatService(store).fire_due_schedules())

    latest = ChatScheduleService(store).get("session")
    assert latest.enabled is False
    assert latest.last_status == "skipped"
    assert "Provider was removed" in (latest.skip_reason or "")
    assert latest.revision == schedule.revision + 1


def _running_goal(
    store: NebulaStore, session_id: str = "session", *, step_budget: int | None = None
):
    goals = ChatGoalService(store)
    created = goals.create(
        session_id,
        GoalCreate(
            objective="Keep going",
            completion_criteria=["Bounded"],
            step_budget=step_budget,
        ),
    )
    return goals.write(
        session_id, GoalWrite(expected_revision=created.revision, action="start")
    )


async def _fire_and_settle(service: ChatService) -> None:
    """Fire due schedules, then wait for the turns they dispatched."""

    await service.fire_due_schedules()
    while pending := [
        runtime.task
        for runtime in service._active_provider_turns.values()
        if runtime.task is not None and not runtime.task.done()
    ]:
        await asyncio.gather(*pending, return_exceptions=True)


def test_due_schedule_runs_a_goal_turn(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store)
    # One step, so the goal pauses instead of continuing after the occurrence.
    goal = _running_goal(store, step_budget=1)
    provider = FakeProvider("provider", local=True)

    asyncio.run(
        _fire_and_settle(ChatService(store, provider_factory=lambda _: provider))
    )

    latest = ChatScheduleService(store).get("session")
    assert latest.last_status == "complete", latest.skip_reason
    turns = store.list_entities(ChatTurn, engagement_id="project")
    assert [item.status for item in turns] == [ChatTurnStatus.COMPLETE]
    assert turns[0].goal_id == goal.id
    assert latest.last_turn_id == turns[0].id
    assert latest.skip_reason is None


def test_scheduled_occurrence_reuses_the_conversation_tool_settings(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store)
    _running_goal(store)
    for server_id, enabled in (("mcp-1", True), ("mcp-off", False)):
        store.create(
            McpServerProfile(
                id=server_id,
                name=server_id,
                transport=McpTransport.STREAMABLE_HTTP,
                url="http://127.0.0.1:1/mcp",
                enabled=enabled,
            )
        )
    store.create(SshEnvironment(id="ssh-1", alias="lab", enabled=True))
    store.create(
        ChatTurn(
            id="manual",
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model-a",
            status=ChatTurnStatus.COMPLETE,
            tools_enabled=True,
            request_snapshot={
                "include_oci_tools": True,
                # A server disabled since the last send is dropped, not fatal.
                "mcp_server_ids": ["mcp-1", "mcp-off"],
                # A host removed since the last send is dropped, not fatal.
                "ssh_environment_snapshot": [
                    {"id": "ssh-1", "alias": "lab"},
                    {"id": "ssh-gone", "alias": "gone"},
                ],
                "allow_subagents": True,
                "context_usage": {},
            },
        )
    )
    service = ChatService(
        store, provider_factory=lambda _: FakeProvider("provider", local=True)
    )
    captured = []

    async def capture(request):
        captured.append(request)
        raise RuntimeError("stop before the provider")

    service.prepare_async = capture  # type: ignore[method-assign]

    asyncio.run(service.fire_due_schedules())

    assert len(captured) == 1
    request = captured[0]
    assert request.tools_enabled is True
    assert request.mcp_server_ids == ["mcp-1"]
    assert request.ssh_environment_ids == ["ssh-1"]
    assert request.allow_subagents is True
    assert request.allow_cloud_tool_results is True


def test_schedule_follows_the_conversation_model(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    schedule = _scheduled_session(store)
    assert schedule.model == "model-a"
    # The operator picked another model in the conversation after scheduling it.
    session = store.get(ChatSession, "session")
    store.update(
        ChatSession, "session", {"model": "model-b"}, expected_revision=session.revision
    )
    store.update(
        ProviderProfile,
        "provider",
        {"model_allowlist": ["model-a", "model-b"]},
        expected_revision=store.get(ProviderProfile, "provider").revision,
    )
    _running_goal(store, step_budget=1)
    provider = FakeProvider("provider", local=True)
    provider.config = provider.config.model_copy(
        update={"model_allowlist": ["model-a", "model-b"]}
    )

    asyncio.run(
        _fire_and_settle(ChatService(store, provider_factory=lambda _: provider))
    )

    latest = ChatScheduleService(store).get("session")
    assert latest.last_status == "complete", latest.skip_reason
    assert latest.model == "model-b"
    turns = store.list_entities(ChatTurn, engagement_id="project")
    assert [item.model for item in turns] == ["model-b"]


def test_paused_goal_skip_names_the_resume_action(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store)
    running = _running_goal(store)
    ChatGoalService(store).write(
        "session", GoalWrite(expected_revision=running.revision, action="pause")
    )

    asyncio.run(
        ChatService(
            store, provider_factory=lambda _: FakeProvider("provider", local=True)
        ).fire_due_schedules()
    )

    latest = ChatScheduleService(store).get("session")
    assert latest.last_status == "skipped"
    assert latest.skip_reason == "Goal is paused; scheduled work waits for Resume."


def test_archiving_pauses_the_schedule_and_unarchiving_resumes_it(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store)
    client = TestClient(create_app(store, auth_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}

    archived = client.patch(
        "/api/v1/chat-sessions/session", headers=headers, json={"archived": True}
    )
    assert archived.status_code == 200, archived.text
    paused = ChatScheduleService(store).get("session")
    assert paused.enabled is False
    assert "archived" in (paused.skip_reason or "")

    unarchived = client.patch(
        "/api/v1/chat-sessions/session", headers=headers, json={"archived": False}
    )
    assert unarchived.status_code == 200, unarchived.text
    resumed = ChatScheduleService(store).get("session")
    assert resumed.enabled is True
    assert resumed.skip_reason is None
    assert resumed.next_run_at > utc_now()

    # A schedule the operator paused stays paused across archive and unarchive.
    ChatScheduleService(store).write(
        "session", ScheduleWrite(expected_revision=resumed.revision, enabled=False)
    )
    for archived_state in (True, False):
        response = client.patch(
            "/api/v1/chat-sessions/session",
            headers=headers,
            json={"archived": archived_state},
        )
        assert response.status_code == 200, response.text
        assert ChatScheduleService(store).get("session").enabled is False


def test_archived_conversation_never_runs_its_schedule(tmp_path):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store)
    _running_goal(store)
    session = store.get(ChatSession, "session")
    store.update(
        ChatSession,
        "session",
        {"metadata": {**session.metadata, "archived_at": utc_now().isoformat()}},
        expected_revision=session.revision,
    )

    asyncio.run(
        ChatService(
            store, provider_factory=lambda _: FakeProvider("provider", local=True)
        ).fire_due_schedules()
    )

    latest = ChatScheduleService(store).get("session")
    assert latest.last_status == "skipped"
    assert "archived" in (latest.skip_reason or "")
    assert store.list_entities(ChatTurn, engagement_id="project") == []


def test_naive_next_run_at_is_rejected(tmp_path):
    with pytest.raises(ValidationError, match="timezone"):
        ChatSchedule(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="model-a",
            interval_seconds=3600,
            next_run_at=datetime(2026, 9, 21, 9, 0),
        )
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    schedule = _scheduled_session(store)
    client = TestClient(create_app(store, auth_token="test-token"))
    # Only the schedule service writes run times: no API route accepts one
    # from a client, so a naive value cannot reach the scheduler's tick.
    patched = client.patch(
        f"/api/v1/chat-schedules/{schedule.id}",
        headers={"Authorization": "Bearer test-token"},
        json={"changes": {"next_run_at": "2026-09-21T09:00:00"}},
    )
    assert patched.status_code == 404, patched.text
    assert store.get(ChatSchedule, schedule.id).next_run_at == schedule.next_run_at


def test_one_failing_schedule_does_not_stop_the_others(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store, "broken")
    store.create(
        ProviderProfile(
            id="provider-b",
            name="Provider B",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    store.create(
        ChatSession(
            id="healthy",
            engagement_id="project",
            title="Healthy",
            provider_profile_id="provider-b",
            model="model-a",
        )
    )
    schedules = ChatScheduleService(store)
    healthy = schedules.create("healthy", ScheduleCreate(interval_seconds=3600))
    store.update(
        ChatSchedule,
        healthy.id,
        {"next_run_at": utc_now() - timedelta(seconds=1)},
        expected_revision=healthy.revision,
    )
    original = ChatScheduleService.reconcile

    def broken_reconcile(self, schedule):
        if schedule.session_id == "broken":
            raise RuntimeError("corrupt schedule row")
        return original(self, schedule)

    monkeypatch.setattr(ChatScheduleService, "reconcile", broken_reconcile)
    store.delete(ProviderProfile, "provider-b")

    asyncio.run(ChatService(store).fire_due_schedules())

    paused = schedules.get("healthy")
    assert paused.enabled is False
    assert "Provider was removed" in (paused.skip_reason or "")


class _GatedProvider(FakeProvider):
    """Answers a scheduled occurrence only once the test lets it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        if request.metadata.get("operation") != "conversation_naming":
            self.entered.set()
            await self.release.wait()
        return await super().complete(request)


def test_a_scheduled_occurrence_streams_to_the_viewer_the_page_attaches(
    tmp_path, monkeypatch
):
    """The chat page follows turns Core starts by itself, schedules included.

    An occurrence is dispatched like goal auto-continue, so its frames stream
    to a viewer the page attaches from the session snapshot, and the schedule
    records how it settled.
    """

    import nebula.v3.api as api_module

    services: list[ChatService] = []

    class RecordedChatService(ChatService):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            services.append(self)

    monkeypatch.setattr(api_module, "ChatService", RecordedChatService)
    store = NebulaStore(tmp_path / "schedules.db")
    store.create(Engagement(id="project", name="Project"))
    _scheduled_session(store)
    # One step, so the goal pauses instead of continuing after the occurrence.
    _running_goal(store, step_budget=1)
    provider = _GatedProvider("provider", local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    auth = {"Authorization": "Bearer test-token"}

    async def entered():
        await asyncio.wait_for(provider.entered.wait(), 5)

    with TestClient(create_app(store, auth_token="test-token")) as client:
        client.portal.call(services[0].fire_due_schedules)
        client.portal.call(entered)
        started = ChatScheduleService(store).get("session")
        assert started.last_status == "started"
        turn_id = started.last_turn_id

        # What the page reads to decide to attach a viewer.
        state = client.get("/api/v1/chat/sessions/session/state", headers=auth)
        assert state.status_code == 200, state.text
        snapshot = state.json()
        assert snapshot["turn_id"] == turn_id
        assert snapshot["busy"] is True
        assert snapshot["execution"] in {"queued", "running"}

        # The follow route attaches to this runtime (its gate) and streams the
        # frames it produces from here on.
        assert services[0].has_provider_turn_stream(turn_id)

        async def follow_while_it_runs():
            frames: list[dict] = []

            async def collect():
                async for _, payload in services[0].follow_provider_turn(turn_id):
                    frames.append(payload)

            follower = asyncio.create_task(collect())
            await asyncio.sleep(0)
            provider.release.set()
            await asyncio.wait_for(follower, 5)
            return frames

        frames = client.portal.call(follow_while_it_runs)

        types = [frame["type"] for frame in frames]
        assert types[0] == "queued" and "admitted" in types
        assert types[-1] == "done"
        assert frames[-1]["message"]["content"].startswith("Evidence-backed answer")
        assert {frame["epoch"] for frame in frames} == {frames[0]["epoch"]}
        assert ChatScheduleService(store).get("session").last_status == "complete"
        assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.COMPLETE
