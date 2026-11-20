"""A goal's active plan step: set through the goal PATCH and progress reports."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest

from nebula.v3.butler_oversight import GoalProgressReport, ingest_agent_report
from nebula.v3.chat_goals import GoalCreate, GoalUpdate, GoalWrite
from nebula.v3.storage import ConflictError
from tests.v3.test_butler_oversight import _setup, _turn

pytestmark = pytest.mark.anyio


async def test_goal_update_sets_keeps_and_clears_the_active_plan_step(tmp_path):
    _, store, goals = await _setup(tmp_path)
    goal = await goals.create(
        "session-project",
        GoalCreate(
            objective="Research",
            completion_criteria=["Evidence"],
            plan=["One", "Two", "Three"],
        ),
    )

    def update(plan, **fields):
        return GoalUpdate(
            expected_revision=goal.revision,
            objective="Research",
            completion_criteria=["Evidence"],
            plan=plan,
            **fields,
        )

    goal = await goals.update(
        "session-project", update(["One", "Two", "Three"], active_plan_index=2)
    )
    assert goal.active_plan_index == 2
    goal = await goals.update(
        "session-project", update(["One", "Two", "Three", "Four"])
    )
    assert goal.active_plan_index == 2
    goal = await goals.update("session-project", update(["One"]))
    assert goal.active_plan_index is None
    goal = await goals.update(
        "session-project", update(["One", "Two"], active_plan_index=1)
    )
    goal = await goals.update(
        "session-project", update(["One", "Two"], active_plan_index=None)
    )
    assert goal.active_plan_index is None


async def test_progress_report_moves_the_active_plan_step(tmp_path):
    _, store, goals = await _setup(tmp_path)
    draft = await goals.create(
        "session-project",
        GoalCreate(
            objective="Research",
            completion_criteria=["Evidence"],
            plan=["Build", "Fuzz", "Reproduce"],
        ),
    )
    goal = await goals.write(
        "session-project", GoalWrite(expected_revision=draft.revision, action="start")
    )
    turn = await _turn(store, goal, "turn-plan")

    def report(**fields):
        return GoalProgressReport(
            what_changed="Built the harness.",
            evidence_gained=["It links."],
            criterion_advanced="Harness exists.",
            next_step="Fuzz it.",
            **fields,
        )

    saved, _ = await ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="plan-1",
        body=report(plan_index=1),
        source="provider_tool",
    )
    assert saved.details["plan_index"] == 1
    assert (await goals.get("session-project")).active_plan_index == 1
    with pytest.raises(ConflictError, match="outside the goal plan"):
        await ingest_agent_report(
            store,
            turn_id=turn.id,
            idempotency_key="plan-2",
            body=report(plan_index=3),
            source="provider_tool",
        )
    plain, _ = await ingest_agent_report(
        store,
        turn_id=turn.id,
        idempotency_key="plan-3",
        body=report(),
        source="provider_tool",
    )
    assert "plan_index" not in plain.details
    assert (await goals.get("session-project")).active_plan_index == 1
