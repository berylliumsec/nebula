"""Harness turns, analysis and Missions respect a project hold.

A hold (#1232) refused chat turns, but harness analysis turns and harness
Missions started their own turns, and a due scheduled Mission or a delegated
Mission's synthesis would fail instead of waiting for the release.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta

import pytest

import nebula.v3.harnesses as harnesses_module
from nebula.v3.chat import ProjectHeld
from nebula.v3.domain import AgentRun, ProjectHold, RunBudget, RunStatus, utc_now
from tests.v3.test_harnesses import _runtime

pytestmark = pytest.mark.anyio


async def _hold(store, engagement_id: str) -> ProjectHold:
    return await store.create(
        ProjectHold(engagement_id=engagement_id, reason="Core deployment")
    )


async def test_harness_chat_and_analysis_turns_are_refused_while_held(tmp_path):
    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    await _hold(store, engagement.id)

    with pytest.raises(ProjectHeld, match="Core deployment"):
        await runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Continue",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
    with pytest.raises(ProjectHeld):
        await runtime.analyze_structured(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Name this conversation",
            nebula_tools=False,
        )


async def test_mission_starting_now_is_refused_while_held(tmp_path):
    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    await _hold(store, engagement.id)

    with pytest.raises(ProjectHeld):
        await runtime.start_mission(
            engagement_id=engagement.id,
            objective="Inspect",
            profile_id=profile.id,
            model=None,
            budget=RunBudget(),
        )
    assert await store.list_entities(AgentRun, engagement_id=engagement.id) == []


async def test_due_scheduled_mission_waits_for_the_release(tmp_path, monkeypatch):
    store, engagement, profile, _, _, runtime = await _runtime(tmp_path)
    monkeypatch.setattr(harnesses_module, "HELD_MISSION_RECHECK_SECONDS", 0.05)
    hold = await _hold(store, engagement.id)
    started: list[str] = []

    async def execute(run_id, _turn_ids):
        started.append(run_id)

    monkeypatch.setattr(runtime, "_execute_mission", execute)
    # A future occurrence is saved while held; it is not refused.
    run = await runtime.start_mission(
        engagement_id=engagement.id,
        objective="Inspect",
        profile_id=profile.id,
        model=None,
        budget=RunBudget(),
        scheduled_for=utc_now() + timedelta(milliseconds=100),
    )
    assert run.status == RunStatus.QUEUED

    await asyncio.sleep(0.5)
    assert started == []
    assert (await store.get(AgentRun, run.id)).status == RunStatus.QUEUED

    await store.update(
        ProjectHold, hold.id, {"status": "released"}, expected_revision=hold.revision
    )
    for _ in range(100):
        if started:
            break
        await asyncio.sleep(0.05)
    assert started == [run.id]
