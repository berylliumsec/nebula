import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import nebula.v3.chat_goals as chat_goals_module
from nebula.v3.chat import ChatCompletionRequest, ChatConfigurationError, ChatService
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.context import estimate_model_request
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurn,
    Engagement,
    ProviderProfile,
    utc_now,
)
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.skill_catalog import SkillSnapshot
from nebula.v3.providers import ModelMessage, ModelRequest
from tests.v3.test_chat import FakeProvider


def setup_goal(tmp_path):
    store = NebulaStore(tmp_path / "goals.db")
    store.create(Engagement(id="project", name="Project"))
    provider = store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Goal chat",
            provider_profile_id=provider.id,
            model="model",
        )
    )
    return store, ChatGoalService(store)


def test_parent_cancel_stops_child_and_reserves_budget(tmp_path):
    store, service = setup_goal(tmp_path)
    parent = service.create(
        "session",
        GoalCreate(
            objective="Parent work",
            completion_criteria=["Children stay bounded"],
            child_budget=1,
        ),
    )
    started = service.write(
        "session", GoalWrite(expected_revision=parent.revision, action="start")
    )
    child = service.start_child(
        "session",
        GoalCreate(objective="Child work", completion_criteria=["Isolated"]),
    )
    assert child.parent_goal_id == started.id
    assert child.status == ChatGoalStatus.DRAFT
    parent_after = service.get("session")
    assert parent_after.children_started == 1
    assert child.session_id in parent_after.child_session_ids
    with pytest.raises(ConflictError, match="child budget is exhausted"):
        service.start_child(
            "session",
            GoalCreate(objective="Another child", completion_criteria=["No"]),
        )
    cancelled = service.write(
        "session",
        GoalWrite(expected_revision=parent_after.revision, action="cancel"),
    )
    assert cancelled.status == ChatGoalStatus.CANCELLED
    assert service.get(child.session_id).status == ChatGoalStatus.CANCELLED


def test_goal_requires_explicit_start_and_revision_safe_lifecycle(tmp_path):
    store, service = setup_goal(tmp_path)
    goal = service.create(
        "session",
        GoalCreate(
            objective="Inspect the workspace safely",
            completion_criteria=["Focused checks pass"],
            plan=["Inspect", "Validate"],
            token_budget=10_000,
            time_budget_seconds=3600,
            step_budget=8,
            child_budget=0,
        ),
    )
    assert goal.status == ChatGoalStatus.DRAFT
    assert goal.started_at is None
    with pytest.raises(ConflictError):
        service.create(
            "session", GoalCreate(objective="Duplicate", completion_criteria=["Done"])
        )

    running = service.write(
        "session", GoalWrite(expected_revision=goal.revision, action="start")
    )
    assert running.status == ChatGoalStatus.RUNNING
    assert running.started_at is not None
    with pytest.raises(ConflictError):
        service.write(
            "session", GoalWrite(expected_revision=goal.revision, action="pause")
        )
    paused = service.write(
        "session", GoalWrite(expected_revision=running.revision, action="pause")
    )
    resumed = service.write(
        "session", GoalWrite(expected_revision=paused.revision, action="resume")
    )
    completed = service.write(
        "session",
        GoalWrite(
            expected_revision=resumed.revision,
            action="complete",
            completion_summary="The focused checks passed.",
            completion_evidence=[{"kind": "test", "result": "passed"}],
        ),
    )
    assert completed.status == ChatGoalStatus.COMPLETED
    assert completed.completion_evidence[0]["result"] == "passed"
    assert store.count(ChatGoal) == 1


def test_goal_block_and_cancel_are_explicit_and_session_delete_cascades(tmp_path):
    store, service = setup_goal(tmp_path)
    goal = service.create(
        "session",
        GoalCreate(objective="Investigate", completion_criteria=["Evidence retained"]),
    )
    running = service.write(
        "session", GoalWrite(expected_revision=goal.revision, action="start")
    )
    blocked = service.write(
        "session",
        GoalWrite(
            expected_revision=running.revision,
            action="block",
            reason="Three unchanged failures",
        ),
    )
    assert blocked.status == ChatGoalStatus.BLOCKED
    assert blocked.consecutive_stalls == 3
    resumed = service.write(
        "session", GoalWrite(expected_revision=blocked.revision, action="resume")
    )
    cancelled = service.write(
        "session", GoalWrite(expected_revision=resumed.revision, action="cancel")
    )
    assert cancelled.status == ChatGoalStatus.CANCELLED
    store.delete_chat_session("session")
    assert store.count(ChatGoal) == 0


def test_running_goal_is_injected_linked_and_charged_by_provider_turn(tmp_path):
    store, goals = setup_goal(tmp_path)
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Inspect the workspace safely",
            completion_criteria=["Evidence is retained"],
            plan=["Inspect", "Summarize"],
            token_budget=10_000,
            step_budget=2,
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    provider = FakeProvider("provider", local=False)
    chat = ChatService(store, provider_factory=lambda _: provider)
    request = ChatCompletionRequest(
        engagement_id="project",
        session_id="session",
        goal_id=running.id,
        provider_id="provider",
        model="model-a",
        messages=[{"role": "user", "content": "Perform the next bounded step"}],
        include_knowledge=False,
        stream=True,
    )

    prepared = chat.prepare(request)
    assert "Core-owned goal context" in prepared.model_request.instructions
    assert "Inspect the workspace safely" in prepared.model_request.instructions
    response = asyncio.run(chat.complete(prepared))

    goal = goals.get("session")
    turns = store.list_entities(ChatTurn, engagement_id="project")
    assert response.turn_id == turns[0].id
    assert turns[0].goal_id == goal.id
    assert goal.current_step == 1
    assert goal.linked_turn_ids == [turns[0].id]
    assert goal.usage.total_tokens == 7

    paused = goals.write(
        "session", GoalWrite(expected_revision=goal.revision, action="pause")
    )
    with pytest.raises(ChatConfigurationError, match="must be running"):
        chat.prepare(request.model_copy(update={"goal_id": paused.id}))


def test_goal_budget_caps_output_and_nonstream_turn_remains_durable(tmp_path):
    store, goals = setup_goal(tmp_path)
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Fit the next provider request",
            completion_criteria=["Request stays within budget"],
            token_budget=10_000,
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    provider = FakeProvider("provider", local=False)
    chat = ChatService(store, provider_factory=lambda _: provider)
    raw = ModelRequest(
        model="model-a",
        messages=[ModelMessage(role="user", content="Continue")],
        max_output_tokens=2_048,
    )
    estimated_input = estimate_model_request(raw)
    constrained_goal = store.update(
        ChatGoal,
        running.id,
        {"token_budget": estimated_input + 17},
        expected_revision=running.revision,
    )

    fitted = chat._fit_goal_request_budget(constrained_goal.id, raw)

    assert fitted.max_output_tokens == 17
    resumed = store.update(
        ChatGoal,
        constrained_goal.id,
        {"token_budget": 10_000},
        expected_revision=constrained_goal.revision,
    )
    prepared = chat.prepare(
        ChatCompletionRequest(
            engagement_id="project",
            session_id="session",
            goal_id=resumed.id,
            provider_id="provider",
            model="model-a",
            messages=[{"role": "user", "content": "Complete without streaming"}],
            include_knowledge=False,
            stream=False,
        )
    )
    assert prepared.turn is not None
    response = asyncio.run(chat.complete(prepared))
    assert response.turn_id == prepared.turn.id
    assert store.get(ChatTurn, prepared.turn.id).status.value == "complete"
    assert store.get(ChatGoal, resumed.id).usage.total_tokens == 7


def test_active_time_budget_pauses_before_dispatch_and_excludes_paused_time(tmp_path):
    store, goals = setup_goal(tmp_path)
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Finish within active time",
            completion_criteria=["No dispatch after expiry"],
            time_budget_seconds=10,
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    running = store.update(
        ChatGoal,
        running.id,
        {"active_since": utc_now() - timedelta(seconds=12)},
        expected_revision=running.revision,
    )
    provider = FakeProvider("provider", local=False)
    chat = ChatService(store, provider_factory=lambda _: provider)

    with pytest.raises(ChatConfigurationError, match="active-time budget"):
        chat.prepare(
            ChatCompletionRequest(
                engagement_id="project",
                session_id="session",
                goal_id=running.id,
                provider_id="provider",
                model="model-a",
                messages=[{"role": "user", "content": "Continue"}],
                include_knowledge=False,
                stream=True,
            )
        )

    exhausted = goals.get("session")
    assert exhausted.status == ChatGoalStatus.PAUSED
    assert exhausted.elapsed_seconds >= 12
    assert exhausted.active_since is None
    assert provider.requests == []


def test_child_budget_is_reserved_once_and_fails_closed(tmp_path):
    _, goals = setup_goal(tmp_path)
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Delegate once",
            completion_criteria=["One child at most"],
            child_budget=1,
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )

    reserved = goals.reserve_child(running.id, expected_revision=running.revision)

    assert reserved.children_started == 1
    with pytest.raises(ConflictError, match="child budget is exhausted"):
        goals.reserve_child(reserved.id, expected_revision=reserved.revision)


def test_goal_skills_are_explicitly_replaced_only_at_safe_revision(tmp_path):
    store, goals = setup_goal(tmp_path)
    draft = goals.create(
        "session",
        GoalCreate(objective="Use reviewed guidance", completion_criteria=["Done"]),
    )
    snapshot = SkillSnapshot(
        name="review",
        path=str(tmp_path / ".agents/skills/review/SKILL.md"),
        source="project",
        root=str(tmp_path / ".agents/skills"),
        sha256="a" * 64,
        instructions="Review carefully.",
    )

    attached = goals.replace_skills(
        "session", expected_revision=draft.revision, snapshots=[snapshot]
    )
    assert attached.skill_snapshots == [snapshot.model_dump(mode="json")]
    with pytest.raises(ConflictError, match="reload before retrying"):
        goals.replace_skills("session", expected_revision=draft.revision, snapshots=[])

    claimed = store.update(
        ChatGoal,
        attached.id,
        {
            "execution_owner_id": "worker",
            "execution_claim_id": "claim",
            "execution_claimed_at": utc_now(),
        },
        expected_revision=attached.revision,
    )
    with pytest.raises(ConflictError, match="wait for active goal work"):
        goals.replace_skills(
            "session", expected_revision=claimed.revision, snapshots=[]
        )

    released = store.update(
        ChatGoal,
        claimed.id,
        {
            "execution_owner_id": None,
            "execution_claim_id": None,
            "execution_claimed_at": None,
        },
        expected_revision=claimed.revision,
    )
    cleared = goals.replace_skills(
        "session", expected_revision=released.revision, snapshots=[]
    )
    assert cleared.skill_snapshots == []


def test_paused_wall_time_does_not_consume_active_goal_budget(tmp_path, monkeypatch):
    _, goals = setup_goal(tmp_path)
    base = datetime(2026, 9, 18, tzinfo=timezone.utc)
    clock = iter(
        [
            base,
            base + timedelta(seconds=5),
            base + timedelta(seconds=5),
            base + timedelta(seconds=100),
            base + timedelta(seconds=103),
            base + timedelta(seconds=103),
        ]
    )
    monkeypatch.setattr(chat_goals_module, "utc_now", lambda: next(clock))
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Count active time only",
            completion_criteria=["Paused time is excluded"],
            time_budget_seconds=10,
        ),
    )

    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    paused = goals.write(
        "session", GoalWrite(expected_revision=running.revision, action="pause")
    )
    resumed = goals.write(
        "session", GoalWrite(expected_revision=paused.revision, action="resume")
    )
    paused_again = goals.write(
        "session", GoalWrite(expected_revision=resumed.revision, action="pause")
    )

    assert paused.elapsed_seconds == pytest.approx(5)
    assert paused_again.elapsed_seconds == pytest.approx(8)
