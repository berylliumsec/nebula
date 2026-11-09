"""Real durable conversation participants restore on the verified target owner."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nebula.v3.chat import ChatService, ChatCompletionRequest, ChatRequestMessage
from nebula.v3.automation_runtime import AutomationRuntimeManager
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    Approval,
    ChatGoal,
    ChatSchedule,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    ProviderProfile,
    CommandExecution,
    ToolCall,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_chat_stream import RuntimeChatJournal
from nebula.v3.runtime_capacity import RuntimeCapacity
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_project_handoff import restored_checkpoint
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider
from tests.v3.test_project_handoff_agent import agent as agent
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def project(host, location):
    coordinator, durable = host
    for version in ("participant-source", "participant-target"):
        await coordinator.stage(
            manifest(
                version, features=["project.handoff/v1", "project.participants/v1"]
            ),
            Path("/isolated") / version,
            "a" * 64,
        )
    source = await worker(host, "participant-source")
    target = await worker(host, "participant-target")
    lease = await coordinator.coordination.claim_project("one", source)
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
    try:
        yield SimpleNamespace(
            coordinator=coordinator,
            durable=durable,
            source=source,
            target=target,
            lease=lease,
            store=store,
            capacity=capacity,
            location=location,
        )
    finally:
        await store.event_history.close()
        await store.database.dispose()


@pytest.fixture
async def source(project, agent):
    await project.durable.create(
        ProviderProfile(
            id="provider",
            name="Inert",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    chat = ChatService(
        project.store,
        provider_factory=lambda _: FakeProvider("provider", local=True),
        runtime_capacity=project.capacity,
        stream_journal=RuntimeChatJournal(
            project.coordinator.coordination, project.store
        ),
    )
    chat.provider_scheduler.host_load = None
    agent.chat = chat
    agent.app.state.control_assistant_work.chat = chat
    try:
        yield chat
    finally:
        with execution_context(project.lease):
            await chat.shutdown()


async def session(project, identity):
    return await project.durable.create(
        ChatSession(
            id=identity,
            engagement_id="one",
            title=identity,
            provider_profile_id="provider",
            model="model-a",
        )
    )


@asynccontextmanager
async def target(project, agent):
    with execution_context(project.lease):
        assert await agent.tick() == "transferable"
    lease = await project.coordinator.transfer("one")
    store = NebulaStore(
        Database(
            project.location,
            bootstrap=False,
            execution_runtime_id=project.target,
            pool_settings=PoolSettings(5, 12, 2, 5),
        )
    )
    chat = ChatService(
        store,
        provider_factory=lambda _: FakeProvider("provider", local=True),
        runtime_capacity=project.capacity,
        stream_journal=RuntimeChatJournal(project.coordinator.coordination, store),
    )
    chat.provider_scheduler.host_load = None
    try:
        digest = await restored_checkpoint(project.coordinator, lease)
        await project.coordinator.verified(lease, restored_checkpoint_digest=digest)
        with execution_context(lease):
            chat.restoring_handoff = True
            await chat.startup()
            yield chat, store
    finally:
        with execution_context(lease):
            await chat.shutdown()
        await store.event_history.close()
        await store.database.dispose()


async def test_due_schedule_keeps_one_occurrence_identity_on_target(
    project, agent, source
):
    await session(project, "scheduled")
    goal = await project.durable.create(
        ChatGoal(
            id="goal",
            engagement_id="one",
            session_id="scheduled",
            objective="Bounded fixture",
            completion_criteria=["One response"],
            status="running",
            step_budget=1,
            started_at=utc_now(),
        )
    )
    schedule = await project.durable.create(
        ChatSchedule(
            id="schedule",
            engagement_id="one",
            session_id="scheduled",
            provider_profile_id="provider",
            model="model-a",
            interval_seconds=3600,
            next_run_at=utc_now() - timedelta(seconds=1),
        )
    )
    async with target(project, agent) as (chat, store):
        while pending := [
            item.task
            for item in chat._active_provider_turns.values()
            if item.task is not None and not item.task.done()
        ]:
            await asyncio.gather(*pending)
        latest = await store.get(ChatSchedule, schedule.id)
        async with asyncio.timeout(8):
            while latest.last_status != "complete":
                await asyncio.sleep(0.01)
                latest = await store.get(ChatSchedule, schedule.id)
        turns = await store.list_entities(ChatTurn, engagement_id="one")
        assert len(turns) == 1 and turns[0].goal_id == goal.id
        assert latest.last_turn_id == turns[0].id and latest.last_status == "complete"
        await chat.restore_handoff_participants()
        assert len(await store.list_entities(ChatTurn, engagement_id="one")) == 1


@pytest.mark.parametrize("decided", [False, True])
async def test_approval_receipt_and_prior_goal_pause_survive_transfer(
    project, agent, source, decided
):
    await session(project, "approval")
    await session(project, "paused")
    turn = await project.durable.create(
        ChatTurn(
            id="approval-turn",
            engagement_id="one",
            session_id="approval",
            provider_profile_id="provider",
            model="model-a",
            status="waiting_approval",
        )
    )
    await project.durable.create(
        ChatGoal(
            id="waiting-goal",
            engagement_id="one",
            session_id="approval",
            objective="Wait",
            completion_criteria=["Confirmed"],
            status="running",
            started_at=utc_now(),
        )
    )
    paused = await project.durable.create(
        ChatGoal(
            id="paused-goal",
            engagement_id="one",
            session_id="paused",
            objective="Previously paused",
            completion_criteria=["Preserve"],
            status="paused",
            paused_at=utc_now(),
            elapsed_seconds=12,
        )
    )
    approval = await project.durable.create(
        Approval(
            id="approval-record",
            engagement_id="one",
            run_id=turn.id,
            origin="chat",
            chat_turn_id=turn.id,
            chat_session_id="approval",
            risk_class="passive",
            policy_rationale="Fixture",
            requested_by="operator",
            exact_request={"tool_name": "read_file"},
            status="approved" if decided else "pending",
            continuation={
                "harness_turn_id": "durable-reference",
                "status": "delivered" if decided else "pending",
                "adapter_status": "sent" if decided else "not_required",
            },
        )
    )
    disabled = await project.durable.create(
        ChatSchedule(
            id="disabled-schedule",
            engagement_id="one",
            session_id="paused",
            provider_profile_id="provider",
            model="model-a",
            interval_seconds=3600,
            next_run_at=utc_now() - timedelta(seconds=1),
            enabled=False,
        )
    )
    async with target(project, agent) as (_chat, store):
        assert (
            await store.get(Approval, approval.id)
        ).model_dump() == approval.model_dump()
        assert (
            await store.get(ChatGoal, paused.id)
        ).model_dump() == paused.model_dump()
        assert (await store.get(ChatGoal, "waiting-goal")).status == "running"
        assert (
            await store.get(ChatSchedule, disabled.id)
        ).model_dump() == disabled.model_dump()
        assert (await store.get(ChatTurn, turn.id)).status == "waiting_approval"


async def test_waiting_child_keeps_parent_link_and_round(project, agent, source):
    await session(project, "parent")
    await session(project, "child")
    parent = await project.durable.create(
        ChatTurn(
            id="parent-turn",
            engagement_id="one",
            session_id="parent",
            provider_profile_id="provider",
            model="model-a",
            status="waiting_callback",
        )
    )
    child = await project.durable.create(
        ChatTurn(
            id="child-turn",
            engagement_id="one",
            session_id="child",
            provider_profile_id="provider",
            model="model-a",
            status="waiting_callback",
        )
    )
    record = await project.durable.create(
        ChatSubagent(
            id="child-record",
            engagement_id="one",
            parent_session_id="parent",
            parent_turn_id=parent.id,
            child_session_id="child",
            child_turn_id=child.id,
            name="Child",
            task="Wait for input",
            rounds=2,
        )
    )
    async with target(project, agent) as (_chat, store):
        assert (
            await store.get(ChatSubagent, record.id)
        ).model_dump() == record.model_dump()
        assert (await store.get(ChatTurn, parent.id)).status == "waiting_callback"
        assert (await store.get(ChatTurn, child.id)).status == "waiting_callback"


async def test_received_callback_moves_existing_turn_without_replaying_command(
    project, agent, source
):
    with execution_context(project.lease):
        prepared = await source.prepare_async(
            ChatCompletionRequest(
                provider_id="provider",
                engagement_id="one",
                model="model-a",
                messages=[
                    ChatRequestMessage(role="user", content="Use the saved result")
                ],
                include_knowledge=False,
                stream=True,
            )
        )
    turn = prepared.turn
    call = await project.durable.create(
        ToolCall(
            id="callback-call",
            engagement_id="one",
            run_id=turn.id,
            chat_turn_id=turn.id,
            origin="chat",
            tool_name="run_command",
            risk_class="passive",
            status="running",
            started_at=utc_now(),
        )
    )
    await project.durable.create(
        CommandExecution(
            id=AutomationRuntimeManager._execution_id("finished-command"),
            engagement_id="one",
            session_id="automation",
            process_id="finished-command",
            command="inert fixture",
            command_sha256="a" * 64,
            runtime_digest="sha256:" + "b" * 64,
            policy_revision=1,
            status="completed",
            exit_code=0,
            completed_at=utc_now(),
            metadata={
                "results_received": True,
                "results_summary": "The saved result is complete",
            },
        )
    )
    await project.durable.update(
        ChatTurn,
        turn.id,
        {
            "status": "waiting_callback",
            "tool_history": [
                {
                    "step": 0,
                    "model_call_id": "saved-call",
                    "tool_call_id": call.id,
                    "name": "run_command",
                    "status": "waiting_callback",
                    "process_id": "finished-command",
                    "arguments": {"command": "inert fixture", "background": True},
                }
            ],
        },
    )
    async with target(project, agent) as (chat, store):
        async with asyncio.timeout(8):
            events = [event async for event in chat.follow_provider_turn(turn.id)]
        assert any(kind == "done" for kind, _payload in events)
        assert (await store.get(ToolCall, call.id)).status == "complete"
        assert len(await store.list_entities(ToolCall, engagement_id="one")) == 1
        assert [
            item.id for item in await store.list_entities(ChatTurn, engagement_id="one")
        ] == [turn.id]
        assert await chat.reconcile_waiting_callbacks() == []


async def test_unknown_approval_delivery_pins_without_replaying(project, agent, source):
    await session(project, "approval")
    await project.durable.create(
        Approval(
            id="unknown-delivery",
            engagement_id="one",
            run_id="waiting",
            origin="chat",
            chat_session_id="approval",
            risk_class="passive",
            policy_rationale="Fixture",
            requested_by="operator",
            exact_request={"tool_name": "read_file"},
            status="approved",
            continuation={
                "harness_turn_id": "vendor-turn",
                "status": "delivered",
                "adapter_status": "unknown",
            },
        )
    )
    with execution_context(project.lease):
        assert await agent.tick() == "finishing"
    status = next(
        item
        for item in (await project.coordinator.status()).projects
        if item.project_id == "one"
    )
    assert not status.admission_held
    assert any("without replay" in item for item in status.blockers)


async def test_missing_callback_result_pins_without_starting_a_provider(
    project, agent, source
):
    await session(project, "callback-wait")
    await project.durable.create(
        ChatTurn(
            id="unconfirmed-callback",
            engagement_id="one",
            session_id="callback-wait",
            provider_profile_id="provider",
            model="model-a",
            status="waiting_callback",
            tool_history=[
                {
                    "step": 0,
                    "tool_call_id": "missing",
                    "model_call_id": "old-call",
                    "name": "run_command",
                    "process_id": "unknown-process",
                    "status": "waiting_callback",
                    "arguments": {},
                }
            ],
        )
    )
    with execution_context(project.lease):
        assert await agent.tick() == "finishing"
    status = next(
        item
        for item in (await project.coordinator.status()).projects
        if item.project_id == "one"
    )
    assert not status.admission_held
    assert any("no confirmed result" in item for item in status.blockers)
    assert not source._active_provider_turns


async def test_legacy_execution_target_rejects_durable_participant_transfer(host):
    coordinator, store = host
    source = await worker(host, "v1")
    target_id = await worker(host, "v2")
    lease = await coordinator.coordination.claim_project("one", source)
    await store.create(
        ChatGoal(
            id="legacy-goal",
            engagement_id="one",
            session_id="saved",
            objective="Preserve",
            completion_criteria=["Bounded"],
            status="paused",
        )
    )
    with pytest.raises(CoordinationConflict, match="participant restoration support"):
        await coordinator.request_handoff("one", target_id)
    async with coordinator.coordination.fenced(lease):
        pass


async def test_participant_feature_is_required_of_execution_not_api(
    project, source, host
):
    await session(project, "goal")
    await project.durable.create(
        ChatGoal(
            id="api-compatibility-goal",
            engagement_id="one",
            session_id="goal",
            objective="Preserve",
            completion_criteria=["Bounded"],
            status="paused",
        )
    )
    api = await worker(host, "v1", "api")
    status = await project.coordinator.status()
    await project.coordinator.promote_api(api, expected_revision=status.revision)
    assert (await project.coordinator.status()).api_version == "v1"
