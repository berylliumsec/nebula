from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from nebula.v3.butler_goal_hygiene import (
    RESUME_HISTORY_KEY,
    ButlerGoalHygiene,
    classify_reason,
    run_hygiene_tick,
)
from nebula.v3.butler_recovery import ButlerRecoveryService
from nebula.v3.butler_supervisor_policy import SupervisorDisabled, load_policy
from nebula.v3.chat_goals import ChatGoalService, GoalUpdate, GoalWrite
from nebula.v3.domain import (
    GOAL_COMPLETION_PROPOSAL_KEY,
    GOAL_COMPLETION_REVIEW_REASON,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    ChatTurnStatus,
    Engagement,
    ProjectHold,
    ProviderProfile,
    WorkItem,
    utc_now,
)
from nebula.v3.recovery_markers import OPERATOR_RECOVERY_PAUSE
from nebula.v3.storage import NebulaStore
from nebula.v3.work import WorkCreate, WorkPatch, WorkService


class FakeWork:
    """ControlAssistantWork's goal and Work methods without the HTTP hop."""

    def __init__(self, store: NebulaStore) -> None:
        self.store = store
        self.goals = ChatGoalService(store)
        self.work_service = WorkService(store)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def update_goal(self, project_id: str, arguments: dict[str, Any]):
        self.calls.append(("update_goal", arguments))
        current = await self.goals.get(arguments["session_id"])
        if arguments["expected_revision"] != current.revision:
            raise ValueError("goal changed; read its current revision before editing")
        fields = set(GoalUpdate.model_fields) - {"expected_revision"}
        body = GoalUpdate.model_validate(
            {
                **{
                    key: getattr(current, key)
                    for key in fields
                    if hasattr(current, key)
                },
                **{key: value for key, value in arguments.items() if key in fields},
                "expected_revision": current.revision,
            }
        )
        await self.goals.update(current.session_id, body)

    async def goal_action(self, project_id: str, arguments: dict[str, Any]):
        self.calls.append(("goal_action", arguments))
        await self.goals.write(
            arguments["session_id"],
            GoalWrite.model_validate(
                {k: v for k, v in arguments.items() if k != "session_id"}
            ),
        )

    async def create_work_item(self, project_id: str, arguments: dict[str, Any]):
        self.calls.append(("create_work_item", arguments))
        item = await self.work_service.create(
            project_id, WorkCreate.model_validate(arguments), actor_id="operator"
        )
        return {"item": item.model_dump(mode="json")}

    async def update_work_item(self, project_id: str, arguments: dict[str, Any]):
        self.calls.append(("update_work_item", arguments))
        await self.work_service.patch(
            project_id,
            arguments["item_id"],
            WorkPatch.model_validate(
                {k: v for k, v in arguments.items() if k != "item_id"}
            ),
        )


def _policy(tmp_path, project_ids, *, enabled=True, unlimited=True):
    env = {
        "NEBULA_V3_DATA_DIR": str(tmp_path / "data"),
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "NEBULA_BUTLER_HYGIENE_PROJECT_IDS": ",".join(project_ids),
        "NEBULA_BUTLER_HYGIENE_DEFAULT_STAGE": "triage",
    }
    if enabled:
        env["NEBULA_BUTLER_SUPERVISOR_ENABLED"] = "1"
    if unlimited:
        env["NEBULA_BUTLER_HYGIENE_UNLIMITED_BUDGETS"] = "1"
    return load_policy(env)


def _audit(tmp_path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _project(store: NebulaStore, name: str = "Research"):
    project = await store.create(Engagement(name=name))
    profile = await store.create(
        ProviderProfile(
            name=f"Local {name}",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    return project, profile


async def _goal(
    store: NebulaStore,
    project,
    profile,
    *,
    status: ChatGoalStatus,
    reason: str | None = None,
    objective: str = "Map the attack surface\nThen validate it",
    **fields: Any,
) -> ChatGoal:
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Goal chat",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    now = utc_now()
    return await store.create(
        ChatGoal(
            engagement_id=project.id,
            session_id=session.id,
            objective=objective,
            completion_criteria=["Done"],
            status=status,
            started_at=now,
            active_since=now if status == ChatGoalStatus.RUNNING else None,
            paused_at=now if status == ChatGoalStatus.PAUSED else None,
            blocked_reason=reason,
            **fields,
        )
    )


async def test_disabled_policy_refuses_and_hook_is_noop(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Goal step budget is exhausted.",
        step_budget=5,
        current_step=5,
    )
    work = FakeWork(store)
    disabled = _policy(tmp_path, [project.id], enabled=False)
    with pytest.raises(SupervisorDisabled):
        await run_hygiene_tick(store, work, disabled)
    tool = ButlerGoalHygiene(store, work, policy_loader=lambda: disabled)
    (action,) = tool.actions()
    assert action.spec.name == "nebula.supervisor.hygiene_tick"
    with pytest.raises(SupervisorDisabled):
        await action._handler({})

    # The periodic-check hook does nothing while the policy is off (default).
    monkeypatch.delenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", raising=False)
    monkeypatch.setenv("NEBULA_BUTLER_HYGIENE_PROJECT_IDS", project.id)
    monkeypatch.setenv(
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG", str(tmp_path / "audit.jsonl")
    )
    service = ButlerRecoveryService(store, SimpleNamespace(work=work))
    await service._run_goal_hygiene()
    assert work.calls == []
    assert (await store.get(ChatGoal, goal.id)).revision == goal.revision
    assert _audit(tmp_path) == []


async def test_hook_runs_tick_when_enabled_and_survives_errors(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(
        store, project, profile, status=ChatGoalStatus.RUNNING, token_budget=100
    )
    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("NEBULA_BUTLER_HYGIENE_PROJECT_IDS", project.id)
    monkeypatch.setenv("NEBULA_BUTLER_HYGIENE_UNLIMITED_BUDGETS", "1")
    monkeypatch.setenv(
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG", str(tmp_path / "audit.jsonl")
    )
    work = FakeWork(store)
    await ButlerRecoveryService(store, SimpleNamespace(work=work))._run_goal_hygiene()
    assert (await store.get(ChatGoal, goal.id)).token_budget is None

    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    import nebula.v3.butler_goal_hygiene as hygiene

    monkeypatch.setattr(hygiene, "run_hygiene_tick", broken)
    # An error inside the tick is recorded, never raised into the check.
    await ButlerRecoveryService(store, SimpleNamespace(work=work))._run_goal_hygiene()
    # Without Butler's agent (no ControlAssistantWork) the hook does nothing.
    await ButlerRecoveryService(store, None)._run_goal_hygiene()


async def test_budgets_nulled_and_child_budget_kept(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.RUNNING,
        token_budget=1_000,
        time_budget_seconds=3_600,
        step_budget=10,
        child_budget=4,
    )
    work = FakeWork(store)
    result = await run_hygiene_tick(store, work, _policy(tmp_path, [project.id]))
    saved = await store.get(ChatGoal, goal.id)
    assert (saved.token_budget, saved.time_budget_seconds, saved.step_budget) == (
        None,
        None,
        None,
    )
    assert saved.child_budget == 4
    assert result["counts"]["null_budgets:applied"] == 1
    assert any(
        entry["action"] == "null_budgets" and entry["tool"] == "goal_hygiene"
        for entry in _audit(tmp_path)
    )


async def test_budgets_kept_when_policy_does_not_unlimit(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(
        store, project, profile, status=ChatGoalStatus.RUNNING, step_budget=10
    )
    await run_hygiene_tick(
        store, FakeWork(store), _policy(tmp_path, [project.id], unlimited=False)
    )
    assert (await store.get(ChatGoal, goal.id)).step_budget == 10


async def test_transient_goals_resumed_and_budget_pause_resumed_after_nulling(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    stalled = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.BLOCKED,
        reason=(
            "The model returned no answer 4 times in a row. The goal is blocked "
            "rather than retrying indefinitely; resume it to try again, or choose "
            "another model."
        ),
        consecutive_stalls=3,
    )
    failed = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Response failed before the goal finished. Review the error, then resume the goal.",
    )
    spent = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Goal step budget is exhausted.",
        step_budget=5,
        current_step=5,
    )
    work = FakeWork(store)
    result = await run_hygiene_tick(store, work, _policy(tmp_path, [project.id]))
    for goal in (stalled, failed, spent):
        saved = await store.get(ChatGoal, goal.id)
        assert saved.status == ChatGoalStatus.RUNNING, saved.blocked_reason
        assert len(saved.metadata[RESUME_HISTORY_KEY]) == 1
    assert result["counts"]["resume:applied"] == 3
    # Each resumed top-level goal now has a staged Work item.
    items = await store.find_entities(WorkItem, {}, engagement_id=project.id)
    assert len(items) == 3
    assert {item.stage for item in items} == {"triage"}


async def test_awaiting_review_operator_paused_and_held_goals_not_resumed(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    review = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.BLOCKED,
        reason=GOAL_COMPLETION_REVIEW_REASON,
        metadata={GOAL_COMPLETION_PROPOSAL_KEY: {"completion_summary": "done"}},
    )
    bare_pause = await _goal(store, project, profile, status=ChatGoalStatus.PAUSED)
    stopped = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Response stopped by the operator. Resume the goal when ready.",
    )
    recovery_mark = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Response failed before the goal finished. Review the error, then resume the goal.",
        metadata={OPERATOR_RECOVERY_PAUSE: utc_now().isoformat()},
    )
    decision = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.BLOCKED,
        reason="Provider choice needs an operator decision before continuing.",
    )
    held_project, held_profile = await _project(store, "Held")
    held = await _goal(
        store,
        held_project,
        held_profile,
        status=ChatGoalStatus.PAUSED,
        reason="Response failed before the goal finished. Review the error, then resume the goal.",
    )
    await store.create(ProjectHold(engagement_id=held_project.id, reason="deploy"))
    work = FakeWork(store)
    result = await run_hygiene_tick(
        store, work, _policy(tmp_path, [project.id, held_project.id])
    )
    assert not any(name == "goal_action" for name, _ in work.calls)
    reasons = {
        entry["goal_id"]: entry["reason"]
        for entry in result["actions"]
        if entry["action"] == "resume"
    }
    assert reasons[review.id] == "awaiting_completion_review"
    assert reasons[bare_pause.id] == "no_reason_operator_pause"
    assert reasons[stopped.id].startswith("core_reason_not_transient")
    assert reasons[recovery_mark.id] == "operator_paused_during_recovery"
    assert reasons[decision.id] == "operator_decision"
    assert reasons[held.id] == "project_held"
    for goal in (review, bare_pause, stopped, recovery_mark, decision, held):
        assert (await store.get(ChatGoal, goal.id)).status == goal.status


async def test_resume_backoff_limits_three_per_hour(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    now = utc_now()
    recent = [(now - timedelta(minutes=minutes)).isoformat() for minutes in (5, 20, 40)]
    old = (now - timedelta(hours=2)).isoformat()
    limited = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Response failed before the goal finished. Review the error, then resume the goal.",
        metadata={RESUME_HISTORY_KEY: recent},
    )
    aged = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Response failed before the goal finished. Review the error, then resume the goal.",
        metadata={RESUME_HISTORY_KEY: [old, *recent[:2]]},
    )
    result = await run_hygiene_tick(
        store, FakeWork(store), _policy(tmp_path, [project.id])
    )
    assert (await store.get(ChatGoal, limited.id)).status == ChatGoalStatus.PAUSED
    saved = await store.get(ChatGoal, aged.id)
    assert saved.status == ChatGoalStatus.RUNNING
    # The stale entry is pruned; two recent plus this resume remain.
    assert len(saved.metadata[RESUME_HISTORY_KEY]) == 3
    skipped = [
        entry
        for entry in result["actions"]
        if entry["goal_id"] == limited.id and entry["action"] == "resume"
    ]
    assert skipped[0]["reason"] == "backoff"


async def test_work_item_created_once_and_stageless_item_staged(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    goal = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.RUNNING,
        objective="  First   line of the objective  \nSecond line",
    )
    other = await _goal(store, project, profile, status=ChatGoalStatus.RUNNING)
    stageless = await store.create(
        WorkItem(
            engagement_id=project.id,
            title="Existing",
            status="in_progress",
            assignee_session_id=other.session_id,
        )
    )
    policy = _policy(tmp_path, [project.id])
    work = FakeWork(store)
    await run_hygiene_tick(store, work, policy)
    await run_hygiene_tick(store, work, policy)
    items = await store.find_entities(
        WorkItem, {"assignee_session_id": goal.session_id}, engagement_id=project.id
    )
    assert len(items) == 1
    (item,) = items
    assert item.title == "First line of the objective"
    assert (item.stage, item.priority, item.status) == ("triage", "high", "in_progress")
    assert (await store.get(WorkItem, stageless.id)).stage == "triage"
    assert (
        len(
            await store.find_entities(
                WorkItem,
                {"assignee_session_id": other.session_id},
                engagement_id=project.id,
            )
        )
        == 1
    )
    assert [name for name, _ in work.calls].count("create_work_item") == 1


async def test_dry_run_changes_nothing(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, profile = await _project(store)
    running = await _goal(
        store, project, profile, status=ChatGoalStatus.RUNNING, token_budget=50
    )
    paused = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.PAUSED,
        reason="Goal token budget is exhausted.",
        token_budget=10,
    )
    work = FakeWork(store)
    tool = ButlerGoalHygiene(
        store, work, policy_loader=lambda: _policy(tmp_path, [project.id])
    )
    result = await tool.actions()[0]._handler({"dry_run": True})
    assert result["dry_run"] is True
    assert result["counts"]["null_budgets:planned"] == 2
    assert result["counts"]["resume:planned"] == 1
    assert result["counts"]["work_item:planned"] == 2
    assert work.calls == []
    for goal in (running, paused):
        assert (await store.get(ChatGoal, goal.id)).revision == goal.revision
    assert await store.find_entities(WorkItem, {}, engagement_id=project.id) == []
    assert _audit(tmp_path) == []


async def test_project_ids_must_be_policy_subset(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project, _ = await _project(store)
    with pytest.raises(ValueError, match="outside the hygiene policy"):
        await run_hygiene_tick(
            store,
            FakeWork(store),
            _policy(tmp_path, [project.id]),
            project_ids=["other"],
        )


def test_classify_reason_uses_turn_status_for_harness_stops():
    goal = ChatGoal(
        engagement_id="p",
        session_id="s",
        objective="o",
        completion_criteria=["c"],
        status=ChatGoalStatus.PAUSED,
        blocked_reason=(
            "The harness response stopped or failed. Review its saved state, "
            "then resume the goal."
        ),
    )
    assert classify_reason(goal, ChatTurnStatus.FAILED)[0] == "transient"
    assert classify_reason(goal, ChatTurnStatus.CANCELLED)[0] == "never"
    ended = goal.model_copy(
        update={
            "blocked_reason": "The goal's latest response ended cancelled and nothing is running for it."
        }
    )
    assert classify_reason(ended)[0] == "never"
    effect = goal.model_copy(
        update={
            "blocked_reason": "Project runtime stopped after a response ended while an effect was unconfirmed (x). Review it before resuming; no effect has been retried."
        }
    )
    assert classify_reason(effect)[0] == "never"
    agent_block = goal.model_copy(
        update={
            "status": ChatGoalStatus.BLOCKED,
            "blocked_reason": "chat_stream_failed: provider stream ended empty",
        }
    )
    assert classify_reason(agent_block)[0] == "transient"


async def test_real_control_assistant_work_nulls_budgets_and_creates_item(tmp_path):
    from nebula.v3.api import create_app
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.runtime_platform import RuntimePlatform

    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = RuntimePlatform(
        store=store, artifact_store=artifacts, data_root=tmp_path / "runtime"
    )
    app = create_app(
        store, auth_token="test-token", artifact_store=artifacts, tool_platform=runtime
    )
    work = app.state.control_assistant_work
    project, profile = await _project(store)
    goal = await _goal(
        store,
        project,
        profile,
        status=ChatGoalStatus.RUNNING,
        step_budget=20,
        time_budget_seconds=7_200,
        child_budget=2,
    )
    result = await run_hygiene_tick(store, work, _policy(tmp_path, [project.id]))
    assert result["errors"] == [], result
    saved = await store.get(ChatGoal, goal.id)
    assert (saved.step_budget, saved.time_budget_seconds, saved.child_budget) == (
        None,
        None,
        2,
    )
    (item,) = await store.find_entities(
        WorkItem, {"assignee_session_id": goal.session_id}, engagement_id=project.id
    )
    assert item.stage == "triage"
    created = [e for e in result["actions"] if e["action"] == "work_item"]
    assert created[0]["item_id"] == item.id
