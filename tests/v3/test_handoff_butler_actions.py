"""A Butler action keeps one known outcome when its project changes worker.

These use real execution kernels on fenced source and target stores, the real
Butler oversight, review and authority path, and the handoff coordinator.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_authority import AuthorityWrite
from nebula.v3.butler_oversight import watch_id
from nebula.v3.butler_review import review_pending
from nebula.v3.chat_goals import ChatGoalService, GoalCreate, GoalWrite
from nebula.v3.database import Database, ProjectHandoffRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    ButlerAction,
    ButlerGoalWatch,
    ButlerProjectState,
    ChatSession,
    ChatTurn,
    ProviderProfile,
    utc_now,
)
from nebula.v3.execution_kernel import create_execution_kernel
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_chat_stream import RuntimeChatJournal
from nebula.v3.runtime_coordination import CoordinationConflict, HEARTBEAT_SECONDS
from nebula.v3.runtime_project_handoff import restored_checkpoint
from nebula.v3.storage import NebulaStore
from tests.v3.test_butler_authority import _Reviewer
from tests.v3.test_chat import FakeProvider
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_handoff_participants import project as project
from tests.v3.test_project_handoff_agent import agent as agent
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def kernel(project, store, root):
    app = create_execution_kernel(
        store,
        auth_token="test-token",
        artifact_store=ArtifactStore(root / "artifacts"),
        execution_data_root=root,
        runtime_capacity=project.capacity,
        stream_journal=RuntimeChatJournal(project.coordinator.coordination, store),
        enable_executable_missions=False,
    )
    chat = app.state.control_assistant_work.chat
    chat.provider_factory = lambda _profile: FakeProvider("provider", local=True)
    chat.provider_scheduler.host_load = None
    return app


@pytest.fixture
async def source(project, agent, tmp_path):
    async def heartbeat():
        # Both execution workers renew their leases as running workers do.
        while True:
            for runtime in (project.source, project.target):
                await project.coordinator.coordination.heartbeat(runtime)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    renew = asyncio.create_task(heartbeat())
    app = kernel(project, project.store, tmp_path / "source")
    # The handoff agent inspects this worker's real Butler and chat services.
    agent.app.state.butler_authority = app.state.butler_authority
    agent.app.state.control_assistant_work = app.state.control_assistant_work
    agent.chat = app.state.control_assistant_work.chat
    try:
        yield app
    finally:
        try:
            with execution_context(project.lease):
                await agent.chat.shutdown()
        finally:
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)


async def planned_status_request(app):
    """Plan one granted parent status request through the real Butler path."""
    store = app.state.store
    await store.create(
        ProviderProfile(
            id="provider",
            name="Inert",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    main = await store.create(
        ChatSession(
            id="main",
            engagement_id="one",
            title="Main agent",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    goals = ChatGoalService(store)
    draft = await goals.create(
        main.id,
        GoalCreate(
            objective="Check the service",
            completion_criteria=["Report an observed result"],
        ),
    )
    goal = await goals.write(
        main.id, GoalWrite(expected_revision=draft.revision, action="start")
    )
    oversight = app.state.butler_oversight
    authority = app.state.butler_authority
    assert await oversight.replay_project("one")
    await authority.write(
        "one",
        AuthorityWrite(authority="steer", delegate_actions=[], expected_revision=0),
    )
    await store.update(
        ButlerGoalWatch,
        watch_id(goal.id),
        {"progress_due_at": utc_now() - timedelta(seconds=1)},
    )
    assert await oversight.fire_due_deadlines("one") == 1
    decision = await review_pending(
        store, "one", oversight.worker_id, _Reviewer("ask"), oversight.memory_service
    )
    assert decision is not None and decision.status == "reviewed"
    assert await authority.plan_next("one", oversight.worker_id)
    (action,) = await store.find_entities(ButlerAction, {}, engagement_id="one")
    assert action.status == "planned"
    return main, action


async def parent_turns(store, session_id):
    return await store.find_entities(
        ChatTurn, {}, engagement_id="one", session_id=session_id
    )


async def handoff(project):
    async with project.coordinator.database.session() as session:
        return await session.get(ProjectHandoffRow, "one")


@asynccontextmanager
async def target(project, lease, tmp_path):
    """Restore the transferred project and run the target's Butler pass."""
    store = NebulaStore(
        Database(
            project.location,
            bootstrap=False,
            execution_runtime_id=project.target,
            pool_settings=PoolSettings(5, 12, 2, 5),
        )
    )
    try:
        app = kernel(project, store, tmp_path / "target")
        digest = await restored_checkpoint(project.coordinator, lease)
        await project.coordinator.verified(lease, restored_checkpoint_digest=digest)
        with execution_context(lease):
            oversight = app.state.butler_oversight
            state = await store.find_entity(ButlerProjectState, "engagement_id", "one")
            # The source's 60 s Butler lease lapses before a new owner claims it.
            await store.update(
                ButlerProjectState,
                state.id,
                {"lease_expires_at": utc_now() - timedelta(seconds=1)},
            )
            assert await oversight.replay_project("one")
            await app.state.butler_authority.process_project("one", oversight.worker_id)
            yield store
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def test_butler_dispatch_in_flight_settles_on_its_source_before_transfer(
    project, agent, source, tmp_path
):
    work = source.state.control_assistant_work
    send = work.message_chat
    gate, entered = asyncio.Event(), asyncio.Event()

    async def message_after_gate(project_id, arguments, *, idempotency_key):
        entered.set()
        await gate.wait()
        return await send(project_id, arguments, idempotency_key=idempotency_key)

    work.message_chat = message_after_gate
    with execution_context(project.lease):
        main, action = await planned_status_request(source)
        dispatch = asyncio.create_task(
            source.state.butler_authority.dispatch(
                action, source.state.butler_oversight.worker_id
            )
        )
    try:
        with execution_context(project.lease):
            await asyncio.wait_for(entered.wait(), 60)
            # SQL says dispatching; whether the parent message is sent is
            # known only to this in-memory dispatch.
            stored = await project.store.get(ButlerAction, action.id)
            assert stored.status == "dispatching"
            assert await parent_turns(project.store, main.id) == []
            assert await agent.tick() == "finishing"
        current = await handoff(project)
        assert current.admission_held
        assert "A Butler action is being dispatched" in current.blockers
        with pytest.raises(CoordinationConflict):
            await project.coordinator.transfer("one")
    finally:
        gate.set()
        with execution_context(project.lease):
            await asyncio.wait_for(dispatch, 60)

    with execution_context(project.lease):
        settled = await project.store.get(ButlerAction, action.id)
        (turn,) = await parent_turns(project.store, main.id)
        assert settled.status == "dispatched"
        assert settled.outcome == {"turn_id": turn.id, "delivery": "started"}
        # The new parent turn waits for the update like any other new root.
        assert await agent.tick() == "transferable"
    lease = await project.coordinator.transfer("one")
    async with target(project, lease, tmp_path) as store:
        moved = await store.get(ButlerAction, action.id)
        assert (moved.status, moved.revision) == ("dispatched", settled.revision)
        assert [item.id for item in await parent_turns(store, main.id)] == [turn.id]


async def test_interrupted_dispatch_moves_without_pinning_and_stays_uncertain(
    project, agent, source, tmp_path
):
    """No process holds this outcome, so a new owner reconciles it as before."""
    with execution_context(project.lease):
        main, action = await planned_status_request(source)
        # A previous incarnation committed dispatching and stopped before it
        # saved an outcome. The handoff cannot show the message was not sent.
        await project.store.update(
            ButlerAction,
            action.id,
            {
                "status": "dispatching",
                "target_session_id": main.id,
                "started_at": utc_now(),
            },
            expected_revision=action.revision,
        )
        assert await agent.tick() == "transferable"
    lease = await project.coordinator.transfer("one")
    async with target(project, lease, tmp_path) as store:
        settled = await store.get(ButlerAction, action.id)
        assert settled.status == "uncertain"
        assert settled.outcome == {"reason": "dispatch_interrupted"}
        # Core never resends an uncertain action on its new owner.
        assert await parent_turns(store, main.id) == []
