"""The real chat tool loop waits for host capacity before invoking a tool."""

import asyncio

import pytest

from nebula.v3.chat import ChatService
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.execution_scope import execution_context
from nebula.v3.providers import ToolCall
from nebula.v3.runtime_capacity import RuntimeCapacity
from nebula.v3.runtime_coordination import RuntimeCoordination
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_chat_throughput import _approval_script, _approval_turn, _approve

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("parallel", [False, True])
async def test_tool_execution_waits_for_shared_capacity_then_commits_its_result(
    tmp_path,
    parallel,
):
    broker = RecordingBroker()
    store, _, prepared, provider = await _prepared(
        tmp_path,
        [
            _response(
                calls=[
                    ToolCall(
                        id="call", name="safe_read", arguments={"value": "bounded"}
                    )
                ]
                + (
                    [
                        ToolCall(
                            id="other-call",
                            name="safe_read",
                            arguments={"value": "second"},
                        )
                    ]
                    if parallel
                    else []
                )
            ),
            _response(
                calls=[ToolCall(id="finish", name="finish_response", arguments={})]
            ),
            _response(text="The bounded tool finished."),
        ],
        broker,
        parallel_safe=parallel,
    )
    coordination = RuntimeCoordination(store.database)
    await coordination.register_runtime("execution", "fixture")
    lease = await coordination.claim_project("project", "execution")
    waiting = asyncio.Event()

    class ObservedCapacity(RuntimeCapacity):
        async def acquire(self, lease, key, demands):
            result = await super().acquire(lease, key, demands)
            if result is None and key.startswith("tool:"):
                waiting.set()
            return result

    capacity = ObservedCapacity(coordination)
    await capacity.configure({"provider": 1, "tool": 1})
    held = await capacity.acquire(lease, "other-version-tool", {"tool": 1})
    service = ChatService(
        store, runtime_capacity=capacity, provider_factory=lambda _: provider
    )
    task = None
    try:
        with execution_context(lease):
            turn_id = await service.start_provider_turn(prepared)

            async def follow():
                return [
                    event async for event, _ in service.follow_provider_turn(turn_id)
                ]

            task = asyncio.create_task(follow())
        await asyncio.wait_for(waiting.wait(), 5)
        assert not broker.calls and not task.done()
        await capacity.release(held)
        events = await asyncio.wait_for(task, 5)
        assert events[-1] == "done" and "tool_completed" in events
        assert len(broker.calls) == (2 if parallel else 1)
        assert (await store.get(ChatTurn, turn_id)).status == ChatTurnStatus.COMPLETE
        assert (await capacity.status())["held_reservations"] == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await capacity.release(held)
        await service.shutdown()
        await store.event_history.close()
        await store.database.dispose()


async def test_approval_resume_also_waits_for_shared_tool_capacity(tmp_path):
    store, _, prepared, provider, broker = await _approval_turn(
        tmp_path, _approval_script()
    )
    coordination = RuntimeCoordination(store.database)
    await coordination.register_runtime("execution", "fixture")
    lease = await coordination.claim_project("project", "execution")
    waiting = asyncio.Event()

    class ObservedCapacity(RuntimeCapacity):
        async def acquire(self, lease, key, demands):
            result = await super().acquire(lease, key, demands)
            if result is None and key.startswith("tool:"):
                waiting.set()
            return result

    capacity = ObservedCapacity(coordination)
    await capacity.configure({"provider": 1, "tool": 1})
    service = ChatService(
        store, runtime_capacity=capacity, provider_factory=lambda _: provider
    )
    held, task = None, None
    try:
        with execution_context(lease):
            turn_id = await service.start_provider_turn(prepared)
        first = [event async for event, _ in service.follow_provider_turn(turn_id)]
        assert first[-1] == "approval_required"
        assert (await capacity.status())["held_reservations"] == 0
        await _approve(store)
        prepared.turn = await store.get(ChatTurn, turn_id)
        prepared.execution_claim_id = None
        held = await capacity.acquire(lease, "other-version-tool", {"tool": 1})
        with execution_context(lease):
            await service.start_provider_turn(prepared)

            async def follow():
                return [
                    event async for event, _ in service.follow_provider_turn(turn_id)
                ]

            task = asyncio.create_task(follow())
        await asyncio.wait_for(waiting.wait(), 5)
        assert broker.approved_runs == 0 and not task.done()
        await capacity.release(held)
        events = await asyncio.wait_for(task, 5)
        assert events[-1] == "done" and broker.approved_runs == 1
        assert (await capacity.status())["held_reservations"] == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if held is not None:
            await capacity.release(held)
        await service.shutdown()
        await store.event_history.close()
        await store.database.dispose()
