"""Per-conversation lookups must see their record however many rows Core holds.

``NebulaStore.list_entities`` pages a kind oldest first, so a lookup that reads
the first 1,000-row page and filters in Python silently misses every newer
conversation once the kind grows past that page.
"""

from datetime import timedelta

from nebula.v3.chat import ChatService
from nebula.v3.chat_goals import ChatGoalService
from nebula.v3.chat_schedules import ChatScheduleService
from nebula.v3.domain import (
    Artifact,
    ChatGoal,
    ChatSchedule,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import NebulaStore


def _turn(turn_id: str, session_id: str, status: ChatTurnStatus) -> ChatTurn:
    return ChatTurn(
        id=turn_id,
        engagement_id="project",
        session_id=session_id,
        provider_profile_id="provider",
        model="model-a",
        status=status,
        request_snapshot={"recovery": {"required": False}, "context_usage": {}},
    )


def _goal(goal_id: str, session_id: str) -> ChatGoal:
    return ChatGoal(
        id=goal_id,
        engagement_id="project",
        session_id=session_id,
        objective=f"Objective for {session_id}",
        completion_criteria=["done"],
    )


def _schedule(schedule_id: str, session_id: str) -> ChatSchedule:
    return ChatSchedule(
        id=schedule_id,
        engagement_id="project",
        session_id=session_id,
        provider_profile_id="provider",
        model="model-a",
        interval_seconds=3_600,
        next_run_at=utc_now() + timedelta(hours=1),
    )


def _artifact(artifact_id: str, tool_call_id: str, kind: str = "stdout") -> Artifact:
    return Artifact(
        id=artifact_id,
        engagement_id="project",
        sha256="0" * 64,
        size=1,
        storage_path=f"blobs/{artifact_id}",
        metadata={"tool_call_id": tool_call_id, "kind": kind},
    )


def _store(tmp_path) -> NebulaStore:
    store = NebulaStore(tmp_path / "chat.db")
    store.create(Engagement(id="project", name="Project"))
    store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    for session_id in ("busy", "other"):
        store.create(
            ChatSession(
                id=session_id,
                engagement_id="project",
                title=session_id,
                provider_profile_id="provider",
                model="model-a",
            )
        )
    return store


def test_pending_turn_sees_an_active_turn_beyond_the_first_thousand_rows(tmp_path):
    store = _store(tmp_path)
    store.create_many(
        [
            _turn(f"old-{index}", "other", ChatTurnStatus("complete"))
            for index in range(1_000)
        ]
    )
    store.create(_turn("active", "busy", ChatTurnStatus("routing")))

    pending = ChatService(store).pending_turn("busy")

    assert pending is not None
    assert pending.id == "active"


def test_goal_lookup_sees_a_goal_beyond_the_first_thousand_rows(tmp_path):
    store = _store(tmp_path)
    store.create_many([_goal(f"old-{index}", "other") for index in range(1_000)])
    store.create(_goal("current", "busy"))

    assert ChatGoalService(store).get("busy").id == "current"


def test_schedule_lookup_sees_a_schedule_beyond_the_first_thousand_rows(tmp_path):
    store = _store(tmp_path)
    store.create_many([_schedule(f"old-{index}", "other") for index in range(1_000)])
    store.create(_schedule("current", "busy"))

    assert ChatScheduleService(store).get("busy").id == "current"


def test_list_session_entities_filters_by_conversation_and_status(tmp_path):
    store = _store(tmp_path)
    store.create_many(
        [
            _turn("busy-done", "busy", ChatTurnStatus("complete")),
            _turn("busy-routing", "busy", ChatTurnStatus("routing")),
            _turn("other-routing", "other", ChatTurnStatus("routing")),
        ]
    )

    assert [item.id for item in store.list_session_entities(ChatTurn, "busy")] == [
        "busy-done",
        "busy-routing",
    ]
    assert [
        item.id
        for item in store.list_session_entities(ChatTurn, "busy", statuses=["routing"])
    ] == ["busy-routing"]


def test_tool_call_artifacts_are_found_beyond_the_first_thousand_rows(tmp_path):
    store = _store(tmp_path)
    store.create_many([_artifact(f"old-{index}", "call-old") for index in range(1_000)])
    store.create_many(
        [
            _artifact("new-stdout", "call-new"),
            _artifact("new-receipt", "call-new", kind="receipt"),
            _artifact("unrelated", "call-other"),
        ]
    )

    assert [
        item.id for item in store.list_tool_call_artifacts("project", "call-new")
    ] == ["new-stdout", "new-receipt"]
