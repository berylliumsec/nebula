"""The handoff fence drains roots without blocking their admitted descendants."""

import asyncio
from types import SimpleNamespace

import pytest

from nebula.v3.chat import ChatCompletionRequest, ChatRequestMessage, ChatService
from nebula.v3.database import Database, ProjectHandoffRow, ProviderTurnQueueRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import ResourceSnapshot
from nebula.v3.deployments import checkpoint_digest
from nebula.v3.domain import (
    ChatSession,
    ChatSubagent,
    ChatTurn,
    ChatTurnStatus,
    ProviderProfile,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
from nebula.v3.runtime_capacity import RuntimeCapacity
from nebula.v3.runtime_chat_stream import RuntimeChatJournal
from nebula.v3.runtime_project_admission import turn_admission_allowed
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider
from tests.v3.test_deployment_lifecycle import host as host, pair
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def project(host, location):
    coordinator, durable = host
    source, target, lease = await pair(host)
    store = NebulaStore(
        Database(
            location,
            bootstrap=False,
            execution_runtime_id=source,
            pool_settings=PoolSettings(5, 12, 2, 5),
        )
    )
    capacity = RuntimeCapacity(coordinator.coordination)
    await capacity.configure({"provider": 2, "background": 1, "tool": 2, "cpu": 2})
    scheduler = ProviderScheduler(
        store,
        worker_id="source",
        runtime_capacity=capacity,
        config=ProviderSchedulerConfig(provider_limit=2, background_limit=1),
    )
    try:
        yield SimpleNamespace(
            coordinator=coordinator,
            durable=durable,
            source=source,
            target=target,
            lease=lease,
            store=store,
            scheduler=scheduler,
            capacity=capacity,
            location=location,
        )
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def queued(project, turn_id, session_id="chat"):
    turn = await project.durable.create(
        ChatTurn(
            id=turn_id,
            engagement_id="one",
            session_id=session_id,
            model="model-a",
            provider_profile_id="provider",
            status=ChatTurnStatus.QUEUED,
            queued_at=utc_now(),
        )
    )
    with execution_context(project.lease):
        await project.scheduler.enqueue(turn)
    return turn


async def queue_state(project, turn_id):
    async with project.coordinator.database.session() as session:
        return (await session.get(ProviderTurnQueueRow, turn_id)).state


async def test_admitted_descendant_finishes_while_an_unrelated_root_waits(project):
    parent = await queued(project, "parent")
    with execution_context(project.lease):
        admitted = await project.scheduler.admit(parent.id)
    await project.coordinator.request_handoff("one", project.target)
    async with project.coordinator.database.session() as session:
        assert (await session.get(ProjectHandoffRow, "one")).admitted_turn_ids == [
            "parent"
        ]
    await project.durable.create(
        ChatSubagent(
            id="child-link",
            engagement_id="one",
            parent_session_id="chat",
            parent_turn_id="parent",
            child_session_id="child-chat",
            provider_profile_id="provider",
            name="Synthetic child",
            task="Finish admitted work",
        )
    )
    child = await queued(project, "child", "child-chat")
    root = await queued(project, "new-root", "new-chat")
    with execution_context(project.lease):
        child_slot = await asyncio.wait_for(project.scheduler.admit(child.id), 5)
        waiting = asyncio.create_task(project.scheduler.admit(root.id))
        await child_slot.release(child.id)
        await asyncio.sleep(0.15)
        assert not waiting.done() and await queue_state(project, root.id) == "queued"
        # Pinning restores normal use of the old version, rather than imposing
        # a kill deadline or indefinitely holding messages for an unrestorable resource.
        await project.coordinator.resource(
            project.lease,
            ResourceSnapshot(
                id="terminal",
                project_id="one",
                runtime_id=project.source,
                fencing_token=project.lease.fencing_token,
                kind="terminal",
                state="active",
                restoration="pinned",
                detail="An open terminal must remain on this runtime",
            ),
        )
        await project.coordinator.checkpoint(project.lease, checkpoint={}, blockers=[])
        root_slot = await asyncio.wait_for(waiting, 5)
        await project.coordinator.resource(
            project.lease,
            ResourceSnapshot(
                id="terminal",
                project_id="one",
                runtime_id=project.source,
                fencing_token=project.lease.fencing_token,
                kind="terminal",
                state="closed",
                restoration="pinned",
                detail="Terminal closed",
            ),
        )
        await project.coordinator.checkpoint(project.lease, checkpoint={}, blockers=[])
        async with project.coordinator.database.session() as session:
            handoff = await session.get(ProjectHandoffRow, "one")
            assert handoff.admission_held and root.id in handoff.admitted_turn_ids
        await root_slot.release(root.id)
        await admitted.release(parent.id)


async def test_handoff_request_waits_for_an_atomic_provider_admission(
    project, monkeypatch
):
    turn = await queued(project, "racing-root")
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(session, candidate):
        assert await turn_admission_allowed(session, candidate)
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr("nebula.v3.provider_scheduler.turn_admission_allowed", held)
    with execution_context(project.lease):
        admission = asyncio.create_task(project.scheduler.admit(turn.id))
    await asyncio.wait_for(entered.wait(), 5)
    handoff = asyncio.create_task(
        project.coordinator.request_handoff("one", project.target)
    )
    try:
        await asyncio.sleep(0.05)
        assert not handoff.done()
    finally:
        release.set()
    slot = await asyncio.wait_for(admission, 5)
    await asyncio.wait_for(handoff, 5)
    async with project.coordinator.database.session() as session:
        assert (await session.get(ProjectHandoffRow, "one")).admitted_turn_ids == [
            turn.id
        ]
    with execution_context(project.lease):
        await slot.release(turn.id)


async def test_held_admission_releases_host_capacity_and_keeps_the_turn_queued(project):
    turn = await queued(project, "held")
    await project.coordinator.request_handoff("one", project.target)
    with execution_context(project.lease):
        waiting = asyncio.create_task(project.scheduler.admit(turn.id))
    try:
        async with asyncio.timeout(5):
            while "project update" not in project.scheduler.queue_detail(turn.id):
                await asyncio.sleep(0.01)
        assert not waiting.done()
        assert (await project.capacity.status())["used"].get("provider", 0) == 0
        turn = await project.durable.get(ChatTurn, turn.id)
        assert turn.status == ChatTurnStatus.QUEUED and turn.admitted_at is None
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    assert await queue_state(project, turn.id) == "queued"


@pytest.mark.parametrize(
    "queued_state",
    [ChatTurnStatus.QUEUED, ChatTurnStatus.ROUTING, ChatTurnStatus.FINALIZING],
)
async def test_queued_producer_moves_without_cancel_pause_or_a_second_turn(
    project, queued_state
):
    await project.durable.create(
        ProviderProfile(
            id="provider",
            name="Inert",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    source_calls, target_calls = [], []

    class Provider(FakeProvider):
        async def stream(self, request):
            target_calls.append("provider")
            async for frame in super().stream(request):
                yield frame

    def source_provider(profile):
        source_calls.append("provider construction")
        return Provider(profile.id, local=True)

    journal = RuntimeChatJournal(project.coordinator.coordination, project.store)
    source = ChatService(
        project.store,
        provider_factory=source_provider,
        stream_journal=journal,
        runtime_capacity=project.capacity,
    )
    target_store = NebulaStore(
        Database(
            project.location,
            bootstrap=False,
            execution_runtime_id=project.target,
            pool_settings=PoolSettings(5, 12, 2, 5),
        )
    )
    target = ChatService(
        target_store,
        provider_factory=lambda profile: Provider(profile.id, local=True),
        stream_journal=RuntimeChatJournal(
            project.coordinator.coordination, target_store
        ),
        runtime_capacity=project.capacity,
    )
    target.provider_scheduler.host_load = None
    try:
        await project.coordinator.request_handoff("one", project.target)
        with execution_context(project.lease):
            prepared = await source.prepare_async(
                ChatCompletionRequest(
                    provider_id="provider",
                    engagement_id="one",
                    model="model-a",
                    messages=[
                        ChatRequestMessage(role="user", content="Queued across handoff")
                    ],
                    include_knowledge=False,
                    stream=True,
                )
            )
            if queued_state != ChatTurnStatus.QUEUED:
                prepared.turn = await project.store.update(
                    ChatTurn,
                    prepared.turn.id,
                    {"status": queued_state},
                    expected_revision=prepared.turn.revision,
                )
            turn_id = await source.start_provider_turn(prepared)
            assert await source.park_queued_for_handoff() == [turn_id]
        assert await queue_state(project, turn_id) == "queued"
        assert (await project.durable.get(ChatTurn, turn_id)).status == queued_state
        assert target_calls == []
        frames = await journal.coordination.replay_stream(turn_id)
        assert not {frame.event_type for frame in frames} & {
            "cancelled",
            "paused",
            "error",
        }
        await project.coordinator.checkpoint(
            project.lease, checkpoint={"queued_turn": turn_id}, blockers=[]
        )
        lease = await project.coordinator.transfer("one")
        with execution_context(lease):
            target.restoring_handoff = True
            await target.startup()
            await project.coordinator.verified(
                lease,
                restored_checkpoint_digest=checkpoint_digest({"queued_turn": turn_id}),
            )
            async with asyncio.timeout(8):
                restored = [
                    frame async for frame in target.follow_provider_turn(turn_id)
                ]
        assert any(kind == "done" for kind, _ in restored)
        assert target_calls == ["provider"]
        assert len(await project.durable.list_entities(ChatTurn)) == 1
        assert len(await project.durable.list_entities(ChatSession)) == 1
    finally:
        with execution_context(project.lease):
            await source.shutdown()
        # The transfer may not have happened if an earlier assertion failed.
        if "lease" in locals():
            with execution_context(lease):
                await target.shutdown()
        await target_store.event_history.close()
        await target_store.database.dispose()
