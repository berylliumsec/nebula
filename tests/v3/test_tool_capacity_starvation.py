"""Tool slots: control-plane tools never queue, and a queued tool says so and gives up.

Long tools can hold every process and host tool slot. Core's own in-process
control-plane built-ins skip both pools; any other tool reports that it is
queued while it waits, and a wait past the bound fails the call with
``CapacityReached`` instead of stalling the turn.
"""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nebula.v3.chat import ChatService
from nebula.v3.chat_agent_messages import agent_message_specs
from nebula.v3.chat_goal_tools import goal_completion_spec
from nebula.v3.domain import ChatTurn, ChatTurnStatus, RiskClass, ScopePolicy
from nebula.v3.execution_scope import execution_context
from nebula.v3.provider_scheduler import ProviderSchedulerConfig
from nebula.v3.providers import ToolCall
from nebula.v3.runtime_capacity import RuntimeCapacity
from nebula.v3.runtime_coordination import RuntimeCoordination

from nebula.v3.tool_capacity import HOST_TOOL_CAPACITY_WAITING, TOOL_CAPACITY_WAITING
from nebula.v3.tool_catalog import CATALOG_CALL, catalog_components
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.tools import ToolSpec
from nebula.v3.work import work_components, work_roster_components
from nebula.v3.working_notes import notes_write_spec
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _control_spec() -> ToolSpec:
    return ToolSpec(
        name="control_read",
        description="Read one of Core's own records.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        budget_class="artifact_query",
        uses_execution_capacity=False,
    )


def _script(name: str) -> list:
    return [
        _response(calls=[ToolCall(id="call", name=name, arguments={"value": "v"})]),
        _response(calls=[ToolCall(id="finish", name="finish_response", arguments={})]),
        _response(text="Done."),
    ]


async def _capacity_service(tmp_path: Path, name: str, *, wait_seconds: int = 600):
    broker = RecordingBroker()
    store, _, prepared, provider = await _prepared(
        tmp_path, _script(name), broker, extra_specs=[_control_spec()]
    )
    coordination = RuntimeCoordination(store.database)
    await coordination.register_runtime("execution", "fixture")
    lease = await coordination.claim_project("project", "execution")
    capacity = RuntimeCapacity(coordination)
    await capacity.configure({"provider": 1, "tool": 1})
    service = ChatService(
        store, runtime_capacity=capacity, provider_factory=lambda _: provider
    )
    service.provider_scheduler.config = ProviderSchedulerConfig(
        global_tool_limit=1,
        per_turn_tool_limit=1,
        tool_capacity_wait_seconds=wait_seconds,
    )
    service._global_tool_slots = asyncio.Semaphore(1)
    return store, prepared, broker, lease, capacity, service


async def _close(store, service, task=None):
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await service.shutdown()
    await store.event_history.close()
    await store.database.dispose()


async def test_control_plane_tool_runs_while_every_tool_slot_is_held(tmp_path):
    store, prepared, broker, lease, capacity, service = await _capacity_service(
        tmp_path, "control_read"
    )
    # Long tools hold the only process slot and the only host slot.
    await service._global_tool_slots.acquire()
    held = await capacity.acquire(lease, "long-tool", {"tool": 1})
    try:
        with execution_context(lease):
            turn_id = await service.start_provider_turn(prepared)
            events = await asyncio.wait_for(_events(service, turn_id), timeout=10)
        assert events[-1] == "done"
        assert "tool_queued" not in events
        assert [call.tool_name for call in broker.calls] == ["control_read"]
        assert (await store.get(ChatTurn, turn_id)).status == ChatTurnStatus.COMPLETE
        # It never took a host reservation of its own.
        assert (await capacity.status())["held_reservations"] == 1
    finally:
        service._global_tool_slots.release()
        await capacity.release(held)
        await _close(store, service)


async def _events(service, turn_id):
    return [event async for event, _ in service.follow_provider_turn(turn_id)]


@pytest.mark.parametrize("pool", ["process", "host"])
async def test_queued_tool_reports_the_wait_then_runs_once_admitted(tmp_path, pool):
    store, prepared, broker, lease, capacity, service = await _capacity_service(
        tmp_path, "safe_read"
    )
    held = None
    if pool == "process":
        await service._global_tool_slots.acquire()
    else:
        held = await capacity.acquire(lease, "long-tool", {"tool": 1})
    queued = asyncio.Event()
    seen: list[tuple[str, dict]] = []
    task = None
    try:
        with execution_context(lease):
            turn_id = await service.start_provider_turn(prepared)

            async def follow():
                async for event, payload in service.follow_provider_turn(turn_id):
                    seen.append((event, payload))
                    if event == "tool_queued":
                        queued.set()

            task = asyncio.create_task(follow())
        await asyncio.wait_for(queued.wait(), 10)
        assert not broker.calls and not task.done()
        payload = next(payload for event, payload in seen if event == "tool_queued")
        expected = (
            TOOL_CAPACITY_WAITING if pool == "process" else HOST_TOOL_CAPACITY_WAITING
        )
        assert payload["detail"] == expected
        assert payload["capability"] == "safe_read"
        # The step reads queued, not running, while it waits.
        turn = await store.get(ChatTurn, turn_id)
        step = (await service._turn_history(turn))[-1]
        assert step["status"] == "queued" and step["queue_detail"] == expected

        if pool == "process":
            service._global_tool_slots.release()
        else:
            await capacity.release(held)
            held = None
        await asyncio.wait_for(task, 10)
        events = [event for event, _ in seen]
        assert events[-1] == "done"
        assert (
            events.index("tool_started")
            < events.index("tool_queued")
            < events.index("tool_admitted")
            < events.index("tool_completed")
        )
        assert len(broker.calls) == 1
        turn = await store.get(ChatTurn, turn_id)
        assert turn.status == ChatTurnStatus.COMPLETE
        assert [entry["status"] for entry in turn.tool_history] == ["complete"]
        assert (await capacity.status())["held_reservations"] == 0
    finally:
        if held is not None:
            await capacity.release(held)
        await _close(store, service, task)


async def test_tool_capacity_wait_expires_as_a_capacity_reached_result(tmp_path):
    store, prepared, broker, lease, capacity, service = await _capacity_service(
        tmp_path, "safe_read", wait_seconds=1
    )
    held = await capacity.acquire(lease, "long-tool", {"tool": 1})
    try:
        with execution_context(lease):
            turn_id = await service.start_provider_turn(prepared)
            events = await asyncio.wait_for(_events(service, turn_id), timeout=15)
        assert events[-1] == "done"
        assert not broker.calls
        turn = await store.get(ChatTurn, turn_id)
        assert turn.status == ChatTurnStatus.COMPLETE
        (entry,) = turn.tool_history
        assert entry["status"] == "failed"
        failure = json.loads(entry["provider_result"])
        assert failure["side_effects"] == "none" and failure["retry_safe"] is True
        assert failure["limit"]["resource"] == "tool_slots"
        # The released wait left nothing behind but the long tool's own slot.
        assert (await capacity.status())["held_reservations"] == 1
        assert not service._global_tool_slots.locked()
    finally:
        await capacity.release(held)
        await _close(store, service)


def test_only_in_process_control_plane_built_ins_skip_tool_capacity(tmp_path):
    scope = ScopePolicy(engagement_id="project")
    exempt = {
        **work_components(None, "project", tmp_path, scope).specs,  # type: ignore[arg-type]
        **work_roster_components(None, "project", tmp_path, scope).specs,  # type: ignore[arg-type]
        **agent_message_specs(),
        "notes.write": notes_write_spec(),
        "goal.complete": goal_completion_spec(),
    }
    catalog = catalog_components(
        RuntimeToolComponents(
            broker=RecordingBroker(),
            scope=scope,
            workspace=tmp_path,
            specs={"control_read": _control_spec()},
            runtime_digest="test",
        ),
        deferred=["control_read"],
    )
    assert catalog is not None
    for name, spec in catalog.specs.items():
        if name != CATALOG_CALL:
            exempt[name] = spec
    assert {
        "work_list",
        "work_get",
        "work_create",
        "work_check_in",
        "work_agents",
        "list_agents",
        "send_agent_message",
        "read_agent_messages",
        "notes.write",
        "goal.complete",
        "tool_catalog.search",
        "tool_catalog.load",
    } <= set(exempt)
    assert all(not spec.uses_execution_capacity for spec in exempt.values())
    # The catalog's call wrapper runs another tool, so it takes a slot.
    assert catalog.specs[CATALOG_CALL].uses_execution_capacity
    # A spec rebuilt from data always takes a slot.
    assert "uses_execution_capacity" not in notes_write_spec().model_dump()
    with pytest.raises(ValidationError, match="control-plane"):
        ToolSpec(
            **{
                **_control_spec().model_dump(),
                "filesystem_access": "read",
                "uses_execution_capacity": False,
            }
        )
