import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.chat_schedules import ChatScheduleService, ScheduleCreate
from nebula.v3.domain import ChatSession, ChatTurn, ChatTurnStatus, Engagement, ProviderProfile, utc_now
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
        GoalCreate(objective="Keep going", completion_criteria=["Scheduled work stays bounded"]),
    )
    goals.write("session", GoalWrite(expected_revision=created.revision, action="start"))
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
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: FakeProvider(profile.id, local=True))
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
