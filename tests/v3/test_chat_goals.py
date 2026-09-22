import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import nebula.v3.chat_goals as chat_goals_module
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatConfigurationError,
    ChatRuntimeSwitchRequest,
    ChatService,
)
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.context import estimate_model_request
from nebula.v3.domain import (
    CHAT_GOAL_CHILD_LIMIT,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
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


def test_goal_resume_requires_interrupted_turn_recovery(tmp_path):
    store, goals = setup_goal(tmp_path)
    goal = goals.create(
        "session",
        GoalCreate(objective="Continue", completion_criteria=["Done"]),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=goal.revision, action="start")
    )
    paused = goals.write(
        "session", GoalWrite(expected_revision=running.revision, action="pause")
    )
    turn = store.create(
        ChatTurn(
            engagement_id="project",
            session_id="session",
            goal_id=goal.id,
            provider_profile_id="provider",
            model="model",
            status=ChatTurnStatus.INTERRUPTED,
            request_snapshot={"recovery": {"required": True, "cause": "core_shutdown"}},
        )
    )
    with pytest.raises(ConflictError, match="interrupted response needs recovery"):
        goals.write(
            "session", GoalWrite(expected_revision=paused.revision, action="resume")
        )
    assert goals.get("session").status == ChatGoalStatus.PAUSED
    store.update(
        ChatTurn,
        turn.id,
        {"request_snapshot": {"recovery": {"required": False}}},
        expected_revision=turn.revision,
    )
    assert goals.write(
        "session", GoalWrite(expected_revision=paused.revision, action="resume")
    ).status == ChatGoalStatus.RUNNING


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


def test_running_goal_read_reports_live_elapsed_without_changing_revision(
    tmp_path, monkeypatch
):
    _, service = setup_goal(tmp_path)
    created = service.create(
        "session", GoalCreate(objective="Inspect", completion_criteria=["Done"])
    )
    started_at = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(chat_goals_module, "utc_now", lambda: started_at)
    running = service.write(
        "session", GoalWrite(expected_revision=created.revision, action="start")
    )
    monkeypatch.setattr(
        chat_goals_module, "utc_now", lambda: started_at + timedelta(seconds=125)
    )

    snapshot = service.read("session")

    assert snapshot.elapsed_seconds == pytest.approx(125)
    assert snapshot.revision == running.revision
    assert service.get("session").elapsed_seconds == 0


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


def test_successful_provider_turn_automatically_continues_running_goal(tmp_path):
    async def scenario() -> None:
        store, goals = setup_goal(tmp_path)
        draft = goals.create(
            "session",
            GoalCreate(
                objective="Finish two bounded steps",
                completion_criteria=["Both steps are evidenced"],
                step_budget=2,
            ),
        )
        running = goals.write(
            "session", GoalWrite(expected_revision=draft.revision, action="start")
        )
        session = store.get(ChatSession, "session")
        store.update(
            ChatSession,
            session.id,
            {"metadata": {**session.metadata, "reasoning_effort": "high"}},
            expected_revision=session.revision,
        )
        provider = FakeProvider("provider", local=False)
        provider.config.model_allowlist.append("model")
        chat = ChatService(store, provider_factory=lambda _: provider)
        await chat.dispatch_running_goal(
            "session",
            "Begin work on the active conversation goal without another operator message.",
        )

        for _ in range(200):
            turns = store.list_entities(ChatTurn, engagement_id="project")
            goal = goals.get("session")
            if (
                len(turns) == 2
                and all(turn.status.value == "complete" for turn in turns)
                and goal.status == ChatGoalStatus.PAUSED
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the automatic goal continuation did not settle")

        messages = chat.session_messages("session")
        assert [turn.goal_id for turn in turns] == [running.id, running.id]
        assert goal.current_step == 2
        assert goal.blocked_reason == "Goal step budget is exhausted."
        goal_requests = [
            request
            for request in provider.requests
            if not request.metadata.get("operation")
        ]
        assert [request.reasoning_effort for request in goal_requests] == [
            "high",
            "high",
        ]
        assert any(
            message.role.value == "user"
            and "without another operator message" in message.content
            for message in messages
        )
        assert any(
            message.role.value == "user"
            and "Is the goal complete?" in message.content
            and "continue making concrete progress" in message.content
            for message in messages
        )
        await chat.shutdown()

    asyncio.run(scenario())


def test_model_and_effort_picked_mid_turn_drive_the_goal_continuation(tmp_path):
    """A running goal is always mid-response, so a switch has to land next turn."""

    async def scenario() -> None:
        store, goals = setup_goal(tmp_path)
        draft = goals.create(
            "session",
            GoalCreate(
                objective="Finish two bounded steps",
                completion_criteria=["Both steps are evidenced"],
                step_budget=2,
            ),
        )
        goals.write(
            "session", GoalWrite(expected_revision=draft.revision, action="start")
        )

        class OperatorSwitchesMidTurn(FakeProvider):
            switched = False

            async def complete(self, request):
                if not request.metadata.get("operation") and not self.switched:
                    self.switched = True
                    assert chat.pending_turn("session") is not None
                    session = store.get(ChatSession, "session")
                    chat.apply_runtime_switch(
                        "session",
                        ChatRuntimeSwitchRequest(
                            provider_id="provider",
                            model="model-b",
                            expected_session_revision=session.revision,
                        ),
                    )
                    session = store.get(ChatSession, "session")
                    store.update(
                        ChatSession,
                        session.id,
                        {"metadata": {**session.metadata, "reasoning_effort": "low"}},
                        expected_revision=session.revision,
                    )
                return await super().complete(request)

        provider = OperatorSwitchesMidTurn("provider", local=False)
        provider.config.model_allowlist.extend(["model", "model-b"])
        chat = ChatService(store, provider_factory=lambda _: provider)
        await chat.dispatch_running_goal(
            "session",
            "Begin work on the active conversation goal without another operator message.",
        )

        for _ in range(200):
            turns = store.list_entities(ChatTurn, engagement_id="project")
            if len(turns) == 2 and all(
                turn.status.value == "complete" for turn in turns
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the automatic goal continuation did not settle")

        turns.sort(key=lambda turn: turn.created_at)
        # The running turn kept what it started with; the next one switched.
        assert [turn.model for turn in turns] == ["model", "model-b"]
        goal_requests = [
            request
            for request in provider.requests
            if not request.metadata.get("operation")
        ]
        assert [request.model for request in goal_requests] == ["model", "model-b"]
        assert [request.reasoning_effort for request in goal_requests] == [
            None,
            "low",
        ]
        # Settling either turn never wrote the operator's choice back.
        session = store.get(ChatSession, "session")
        assert session.model == "model-b"
        assert session.metadata["reasoning_effort"] == "low"
        await chat.shutdown()

    asyncio.run(scenario())


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
    # One clock read per action: start, pause, resume, pause. A pause stamps
    # the parent and any started children with the same instant.
    clock = iter(
        [
            base,
            base + timedelta(seconds=5),
            base + timedelta(seconds=100),
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


def test_block_reports_wrong_status_and_missing_reason_distinctly(tmp_path):
    store, service = setup_goal(tmp_path)
    goal = service.create(
        "session",
        GoalCreate(objective="Investigate", completion_criteria=["Evidence retained"]),
    )
    running = service.write(
        "session", GoalWrite(expected_revision=goal.revision, action="start")
    )

    # A running goal blocked without a reason is a validation failure (422).
    with pytest.raises(HTTPException) as missing_reason:
        service.write(
            "session",
            GoalWrite(expected_revision=running.revision, action="block"),
        )
    assert missing_reason.value.status_code == 422
    assert "reason" in missing_reason.value.detail

    paused = service.write(
        "session", GoalWrite(expected_revision=running.revision, action="pause")
    )

    # A paused goal blocked WITH a reason is a state conflict (409), and the
    # message must name the status, not the reason the caller already supplied.
    with pytest.raises(ConflictError) as wrong_status:
        service.write(
            "session",
            GoalWrite(
                expected_revision=paused.revision,
                action="block",
                reason="Three unchanged failures",
            ),
        )
    assert "running" in str(wrong_status.value)
    assert "reason" not in str(wrong_status.value)
    assert store.get(ChatGoal, goal.id).status == ChatGoalStatus.PAUSED


def _goal_request(goal_id: str, content: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        engagement_id="project",
        session_id="session",
        goal_id=goal_id,
        provider_id="provider",
        model="model-a",
        messages=[{"role": "user", "content": content}],
        include_knowledge=False,
        stream=True,
    )


def test_goal_turns_continue_past_a_plan_shorter_than_the_step_budget(tmp_path):
    """``current_step`` counts turns under the step budget; the plan is guidance.

    A two-line plan with a step budget of four used to crash the third send
    inside ``_persist_turn_inputs`` (``current goal step exceeds the plan``),
    leaving the goal RUNNING and every later send failing the same way.
    """

    store, goals = setup_goal(tmp_path)
    draft = goals.create(
        "session",
        GoalCreate(
            objective="Inspect the workspace safely",
            completion_criteria=["Evidence is retained"],
            plan=["Inspect", "Summarize"],
            step_budget=4,
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    provider = FakeProvider("provider", local=False)
    chat = ChatService(store, provider_factory=lambda _: provider)

    for index in range(4):
        prepared = chat.prepare(_goal_request(running.id, f"Step {index}"))
        assert "Core-owned goal context" in prepared.model_request.instructions
        asyncio.run(chat.complete(prepared))
        goal = goals.get("session")
        assert goal.status == ChatGoalStatus.RUNNING
        assert goal.current_step == index + 1

    assert len(store.list_entities(ChatTurn, engagement_id="project")) == 4
    assert goals.get("session").linked_turn_ids == [
        turn.id for turn in store.list_entities(ChatTurn, engagement_id="project")
    ]

    # The step budget, not the plan length, is what ends the goal cleanly.
    with pytest.raises(ChatConfigurationError, match="step budget is exhausted"):
        chat.prepare(_goal_request(running.id, "One more"))
    exhausted = goals.get("session")
    assert exhausted.status == ChatGoalStatus.PAUSED
    assert exhausted.blocked_reason == "Goal step budget is exhausted."
    assert exhausted.current_step == 4
    assert len(store.list_entities(ChatTurn, engagement_id="project")) == 4

    # A goal with a plan and no step budget is bounded by tokens/time only.
    assert (
        ChatGoal(
            engagement_id="project",
            session_id="session",
            objective="Unbounded",
            completion_criteria=["Done"],
            plan=["Only step"],
            current_step=3,
        ).current_step
        == 3
    )


def test_charging_a_terminal_goal_records_usage_without_resurrecting_it(tmp_path):
    store, goals = setup_goal(tmp_path)
    provider = FakeProvider("provider", local=False)
    chat = ChatService(store, provider_factory=lambda _: provider)
    over_budget = ChatTokenUsage(input_tokens=6, output_tokens=6, total_tokens=12)

    draft = goals.create(
        "session",
        GoalCreate(
            objective="Inspect", completion_criteria=["Evidence"], token_budget=10
        ),
    )
    running = goals.write(
        "session", GoalWrite(expected_revision=draft.revision, action="start")
    )
    cancelled = goals.write(
        "session", GoalWrite(expected_revision=running.revision, action="cancel")
    )

    charged = chat._charge_goal(cancelled.id, over_budget)

    assert charged.status == ChatGoalStatus.CANCELLED
    assert charged.usage.total_tokens == 12
    assert charged.completed_at == cancelled.completed_at
    assert charged.blocked_reason is None
    assert charged.paused_at is None
    with pytest.raises(ConflictError, match="paused or blocked"):
        goals.write(
            "session", GoalWrite(expected_revision=charged.revision, action="resume")
        )

    completed = store.update(
        ChatGoal,
        charged.id,
        {
            "status": ChatGoalStatus.COMPLETED,
            "completion_summary": "Done.",
            "completion_evidence": [{"kind": "note"}],
        },
        expected_revision=charged.revision,
    )
    charged_again = chat._charge_goal(completed.id, over_budget)
    assert charged_again.status == ChatGoalStatus.COMPLETED
    assert charged_again.usage.total_tokens == 24
    assert charged_again.completion_summary == "Done."

    # A running goal that crosses its budget still pauses with the reason.
    active = store.update(
        ChatGoal,
        charged_again.id,
        {"status": ChatGoalStatus.RUNNING, "active_since": utc_now()},
        expected_revision=charged_again.revision,
    )
    paused = chat._charge_goal(active.id, over_budget)
    assert paused.status == ChatGoalStatus.PAUSED
    assert paused.blocked_reason == "Token budget exhausted during provider response."
    assert paused.usage.total_tokens == 36


def test_parent_pause_skips_draft_children_and_commits_with_the_parent(tmp_path):
    store, service = setup_goal(tmp_path)
    parent = service.create(
        "session",
        GoalCreate(
            objective="Parent work",
            completion_criteria=["Children stay bounded"],
            child_budget=2,
        ),
    )
    service.write(
        "session", GoalWrite(expected_revision=parent.revision, action="start")
    )
    draft_child = service.start_child(
        "session", GoalCreate(objective="Draft child", completion_criteria=["x"])
    )
    started_child = service.start_child(
        "session", GoalCreate(objective="Started child", completion_criteria=["y"])
    )
    started_child = service.write(
        started_child.session_id,
        GoalWrite(expected_revision=started_child.revision, action="start"),
    )

    paused = service.write(
        "session",
        GoalWrite(expected_revision=service.get("session").revision, action="pause"),
    )

    assert paused.status == ChatGoalStatus.PAUSED
    # The draft child never passed Start, so the parent's pause leaves it alone.
    untouched = service.get(draft_child.session_id)
    assert untouched.status == ChatGoalStatus.DRAFT
    assert untouched.revision == draft_child.revision
    assert untouched.blocked_reason is None
    with pytest.raises(ConflictError, match="paused or blocked"):
        service.write(
            draft_child.session_id,
            GoalWrite(expected_revision=untouched.revision, action="resume"),
        )
    stopped = service.get(started_child.session_id)
    assert stopped.status == ChatGoalStatus.PAUSED
    assert stopped.blocked_reason == "Parent goal paused; child work is paused."
    assert stopped.started_at is not None

    # Resume everything, then make the parent's own update lose a revision race
    # while the pause is in flight: neither the parent nor the child may change.
    resumed = service.write(
        "session", GoalWrite(expected_revision=paused.revision, action="resume")
    )
    running_child = service.write(
        stopped.session_id,
        GoalWrite(expected_revision=stopped.revision, action="resume"),
    )
    real_list_children = service.list_children

    def list_children_while_another_device_writes(session_id: str) -> list[ChatGoal]:
        children = real_list_children(session_id)
        store.update(
            ChatGoal,
            resumed.id,
            {"metadata": {"touched_elsewhere": True}},
            expected_revision=resumed.revision,
        )
        return children

    service.list_children = list_children_while_another_device_writes  # type: ignore[method-assign]
    with pytest.raises(ConflictError):
        service.write(
            "session", GoalWrite(expected_revision=resumed.revision, action="pause")
        )

    assert service.get("session").status == ChatGoalStatus.RUNNING
    child_after = service.get(running_child.session_id)
    assert child_after.status == ChatGoalStatus.RUNNING
    assert child_after.revision == running_child.revision


def test_child_budget_is_bounded_by_the_child_list_cap(tmp_path):
    store, service = setup_goal(tmp_path)
    assert CHAT_GOAL_CHILD_LIMIT == 32
    with pytest.raises(ValidationError, match="less than or equal to 32"):
        GoalCreate(
            objective="Too many",
            completion_criteria=["x"],
            child_budget=CHAT_GOAL_CHILD_LIMIT + 1,
        )
    assert (
        GoalCreate(
            objective="Enough",
            completion_criteria=["x"],
            child_budget=CHAT_GOAL_CHILD_LIMIT,
        ).child_budget
        == CHAT_GOAL_CHILD_LIMIT
    )

    # A goal persisted with a larger budget (or one whose list is already full)
    # is refused before any child session or goal is created.
    full = store.create(
        ChatGoal(
            engagement_id="project",
            session_id="session",
            objective="Parent",
            completion_criteria=["x"],
            status=ChatGoalStatus.RUNNING,
            started_at=utc_now(),
            active_since=utc_now(),
            child_budget=40,
            children_started=CHAT_GOAL_CHILD_LIMIT,
            child_session_ids=[
                f"child-{index}" for index in range(CHAT_GOAL_CHILD_LIMIT)
            ],
        )
    )
    goals_before = store.count(ChatGoal)
    sessions_before = store.count(ChatSession)

    with pytest.raises(ConflictError, match="cannot list more than 32 children"):
        service.start_child(
            "session", GoalCreate(objective="Child 33", completion_criteria=["x"])
        )

    assert store.count(ChatGoal) == goals_before
    assert store.count(ChatSession) == sessions_before
    parent = service.get("session")
    assert parent.revision == full.revision
    assert parent.children_started == CHAT_GOAL_CHILD_LIMIT
