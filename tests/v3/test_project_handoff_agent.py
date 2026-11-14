"""A source proves its safe boundary and the target verifies committed state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select

from nebula.v3.database import (
    ProjectHandoffRow,
    RuntimeResourceRow,
    ProviderTurnQueueRow,
    EntityRow,
)
from nebula.v3.deployments import checkpoint_digest
from nebula.v3.deployment_protocol import ResourceSnapshot
from nebula.v3.domain import Engagement, Observation, ChatTurn, utc_now
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_project_handoff import ProjectHandoffAgent, restored_checkpoint
from tests.v3.test_project_handoff_admission import project as project
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def agent(project):
    chat = SimpleNamespace(
        park_queued_for_handoff=AsyncMock(),
        restore_locally_parked_turns=AsyncMock(),
        _naming_tasks=set(),
        _precompactions={},
    )
    state = SimpleNamespace(
        control_assistant_work=SimpleNamespace(
            chat=chat, queue=SimpleNamespace(_inflight=set())
        ),
        automation_runtime=SimpleNamespace(_sessions={}, _processes={}),
        notebooks=SimpleNamespace(workers={}, _drains={}),
        container_terminal_service=SimpleNamespace(
            _sessions={}, _finishing_sessions={}
        ),
        debug_service=SimpleNamespace(_sessions={}, _attach_tasks={}),
        harness_runtime_service=SimpleNamespace(
            _connections={},
            _active={},
            _mission_tasks={},
            _scheduled_mission_tasks={},
            _chat_turn_tasks={},
            _gateway_work={},
        ),
        mission_service=SimpleNamespace(_tasks={}, _scheduled_tasks={}),
        execution_service=SimpleNamespace(_tasks={}),
        report_render_service=SimpleNamespace(_tasks={}),
        execution_ai_service=SimpleNamespace(_tasks={}),
    )
    item = ProjectHandoffAgent(
        SimpleNamespace(coordinator=project.coordinator),
        project.lease,
        SimpleNamespace(state=state),
    )
    await project.coordinator.request_handoff("one", project.target)
    return item


async def row(project):
    async with project.coordinator.database.session() as session:
        return await session.get(ProjectHandoffRow, "one")


async def test_current_provider_finishes_without_reopening_root_admission(
    project, agent
):
    await project.coordinator.resource(
        project.lease,
        ResourceSnapshot(
            id="provider-segment",
            project_id="one",
            runtime_id=project.source,
            fencing_token=project.lease.fencing_token,
            kind="provider_turn",
            state="active",
            restoration="durable",
            detail="The current response is finishing",
        ),
    )
    assert await agent.tick() == "finishing"
    handoff = await row(project)
    assert (
        handoff.admission_held
        and "The current response is finishing" in handoff.blockers
    )


async def test_resident_handle_pins_and_releases_only_after_it_closes(project, agent):
    agent.app.state.container_terminal_service._sessions["inert-terminal"] = object()
    assert await agent.tick() == "finishing"
    handoff = await row(project)
    assert not handoff.admission_held and any(
        "container_terminal_service" in text for text in handoff.blockers
    )
    async with project.coordinator.database.session() as session:
        resources = list(await session.scalars(select(RuntimeResourceRow)))
    assert len(resources) == 1 and resources[0].restoration == "pinned"
    agent.app.state.container_terminal_service._sessions.clear()
    assert await agent.tick() == "transferable"
    assert (await row(project)).admission_held
    lease = await project.coordinator.transfer("one")
    assert await restored_checkpoint(project.coordinator, lease) == checkpoint_digest(
        (await row(project)).checkpoint
    )
    agent.chat.restore_locally_parked_turns.assert_awaited_once()


async def test_follow_up_dispatch_in_flight_keeps_the_project_finishing(project, agent):
    # The dispatcher has claimed a queued follow-up in memory; SQL shows no
    # turn yet. Transferring now would leave the target a claim it can only
    # review as a crash, so the source keeps finishing with admission held.
    agent.queue._inflight.add("follow-up")
    assert await agent.tick() == "finishing"
    handoff = await row(project)
    assert handoff.admission_held
    assert "A queued follow-up is being dispatched" in handoff.blockers
    with pytest.raises(CoordinationConflict):
        await project.coordinator.transfer("one")
    agent.queue._inflight.clear()
    assert await agent.tick() == "transferable"
    lease = await project.coordinator.transfer("one")
    assert lease.fencing_token == project.lease.fencing_token + 1


async def test_changed_project_revision_rejects_both_transfer_and_stale_restore(
    project, agent
):
    assert await agent.tick() == "transferable"
    await project.durable.update(
        Engagement, "one", {"name": "Changed after checkpoint"}
    )
    with pytest.raises(CoordinationConflict, match="fresh boundary"):
        await project.coordinator.transfer("one")
    assert (await row(project)).phase == "transferable"
    assert await agent.tick() == "transferable"
    lease = await project.coordinator.transfer("one")
    assert await restored_checkpoint(project.coordinator, lease)
    await project.durable.update(
        Engagement, "one", {"name": "Injected unexpected writer"}
    )
    with pytest.raises(CoordinationConflict, match="changed before restoration"):
        await restored_checkpoint(project.coordinator, lease)
    assert (await row(project)).admission_held


async def test_held_capacity_blocks_checkpoint_and_late_reservation_blocks_transfer(
    project, agent
):
    reservation = await project.capacity.acquire(
        project.lease, "current-provider", {"provider": 1}
    )
    assert await agent.tick() == "finishing"
    assert any("completion evidence" in text for text in (await row(project)).blockers)
    await project.capacity.release(reservation)
    assert await agent.tick() == "transferable"
    tool = await project.capacity.acquire(project.lease, "late-tool", {"tool": 1})
    with pytest.raises(CoordinationConflict, match="changed after"):
        await project.coordinator.transfer("one")
    await project.capacity.release(tool)
    assert await agent.tick() == "transferable"
    await project.coordinator.transfer("one")


async def test_missing_participant_contract_pins_a_usable_old_project(project, agent):
    del agent.app.state.debug_service._sessions
    assert await agent.tick() == "finishing"
    handoff = await row(project)
    assert not handoff.admission_held
    assert any("debug_service._sessions" in text for text in handoff.blockers)
    agent.app.state.debug_service._sessions = {}
    assert await agent.tick() == "transferable"


async def test_checkpoint_streams_past_batch_boundary_without_pinning(
    project, agent, monkeypatch
):
    await project.durable.create(
        Observation(
            engagement_id="one",
            observation_type="note",
            title="Synthetic",
            body="No external effect",
        )
    )
    monkeypatch.setattr("nebula.v3.runtime_project_handoff.CHECKPOINT_BATCH_ROWS", 1)
    assert await agent.tick() == "transferable"
    assert (await row(project)).admission_held


async def test_active_record_after_many_completed_records_still_pins(project, agent):
    now = utc_now()
    async with project.coordinator.database.session() as session:
        await session.execute(
            insert(EntityRow),
            [
                {
                    "id": f"historic-{index:08d}",
                    "kind": "tool_calls",
                    "engagement_id": "one",
                    "revision": 1,
                    "payload": {"status": "complete"},
                    "created_at": now,
                    "updated_at": now,
                }
                for index in range(10_001)
            ],
        )
        await session.execute(
            insert(EntityRow),
            [
                {
                    "id": "zz-active",
                    "kind": "tool_calls",
                    "engagement_id": "one",
                    "revision": 1,
                    "payload": {"status": "running"},
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
    assert await agent.tick() == "finishing"
    handoff = await row(project)
    assert not handoff.admission_held and any(
        "zz-active" in text for text in handoff.blockers
    )


async def test_running_provider_admission_without_resource_cannot_transfer(
    project, agent
):
    await project.durable.create(
        ChatTurn(
            id="orphan-running",
            engagement_id="one",
            session_id="saved",
            provider_profile_id="provider",
            model="model",
            status="routing",
        )
    )
    async with project.coordinator.database.session() as session:
        session.add(
            ProviderTurnQueueRow(
                turn_id="orphan-running",
                lane="foreground",
                state="running",
                lease_owner="lost-admission",
            )
        )
    assert await agent.tick() == "finishing"
    current = await row(project)
    assert current.admission_held and any(
        "running provider admission" in item for item in current.blockers
    )
    async with project.coordinator.database.session() as session:
        current = await session.get(ProjectHandoffRow, "one")
        current.phase, current.blockers = "transferable", []
    with pytest.raises(CoordinationConflict, match="changed after"):
        await project.coordinator.transfer("one")
