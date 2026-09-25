"""A repeated start returns the subagent the first call started, as it is now.

A vendor harness retries a tool call with the same arguments, and a provider
step can be replayed; both reach the child the first call started (the same
idempotency key). The result must carry that child's real status: a child
that finished is not "running", one that ran and failed is not a start
failure, and a start that never ran fails again the way it failed first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nebula.v3.domain import (
    ChatSubagent,
    ChatSubagentStatus,
    ChatTurn,
    ChatTurnStatus,
    HarnessTurn,
    HarnessTurnStatus,
    ProviderProfile,
    utc_now,
)
from nebula.v3.tool_failures import FAILURE_SCHEMA
from nebula.v3.tools import ToolCallOrigin, ToolInvocation
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _call,
    _drain,
    _finish,
    _request,
    _response,
    _setup,
    _until,
)
from tests.v3.test_chat_subagent_lifecycle import _child, _session, _turn
from tests.v3.test_harness_provider_subagents import (
    ScriptedConnection,
    _payload,
    _prepare,
)
from tests.v3.test_harness_provider_subagents import _setup as _harness_setup


def _settled(store, record_id: str) -> bool:
    return store.get(ChatSubagent, record_id).status != ChatSubagentStatus.RUNNING


def test_harness_retry_of_a_start_reports_the_childs_real_status(tmp_path: Path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        child = chat.provider_factory(store.get(ProviderProfile, "provider"))
        child.answers = ["Mapped 4 routes."]
        child.gate = asyncio.Event()
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            first = _payload(await connection.call("subagent.start", task="Map."))
            seen["first"] = first
            # The vendor retries the same call while the child still works.
            seen["running"] = _payload(
                await connection.call("subagent.start", task="Map.")
            )
            child.gate.set()
            await _until(lambda: _settled(store, first["subagent_id"]))
            # And again once it finished and Core handed the harness its report.
            seen["finished"] = _payload(
                await connection.call("subagent.start", task="Map.")
            )
            return "Mapped."

        adapter.script = script
        _, _, turn = _prepare(runtime, project, harness, "Map the routes.")
        await runtime.start_chat_turn(turn.id)

        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
        assert seen["first"]["status"] == "running"
        assert seen["running"]["subagent_id"] == seen["first"]["subagent_id"]
        assert seen["running"]["status"] == "running"
        finished = seen["finished"]
        assert finished["subagent_id"] == seen["first"]["subagent_id"]
        assert finished["status"] == "completed"
        assert "Running in parallel" not in finished["note"]
        assert "received its report earlier" in finished["note"]
        assert "subagent.message" in finished["note"]
        # One child, never a second one for the retries.
        (record,) = store.list_entities(ChatSubagent)
        assert record.id == finished["subagent_id"]
        assert record.reported_at is not None
        await chat.shutdown()

    asyncio.run(scenario())


def test_a_finished_child_whose_report_is_unread_points_to_it(tmp_path: Path):
    store, _, _, chat = _setup(tmp_path, RoutedProvider([], []))
    session = _session(store)
    parent = _turn(store, session, status=ChatTurnStatus.ROUTING)
    record = _child(
        store,
        session,
        parent,
        status=ChatSubagentStatus.COMPLETED,
        finished_at=utc_now(),
        child_status=ChatTurnStatus.COMPLETE,
    )

    provider = chat.subagents.start_output(record, harness=False)
    assert provider["status"] == "completed"
    assert provider["note"].endswith("Call wait_subagents for its report.")
    assert "model" not in provider
    harness = chat.subagents.start_output(record, harness=True)
    assert harness["status"] == "completed"
    assert harness["model"] == "model-a"
    assert "subagent.wait" in harness["note"]


def test_harness_retry_of_a_start_whose_child_failed_is_not_a_start_failure(
    tmp_path: Path,
):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        # No answers: the child's first request fails.
        seen: dict = {}

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            first = _payload(await connection.call("subagent.start", task="Scan."))
            await _until(lambda: _settled(store, first["subagent_id"]))
            seen["retry"] = await connection.call("subagent.start", task="Scan.")
            return "Scanned."

        adapter.script = script
        _, _, turn = _prepare(runtime, project, harness, "Scan it.")
        await runtime.start_chat_turn(turn.id)

        # The child ran and failed: not a start failure, and not running.
        retry = _payload(seen["retry"])
        assert retry["status"] == "failed"
        assert "Running in parallel" not in retry["note"]
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.FAILED
        # Core handed the harness its failure report when it settled.
        assert record.reported_at is not None
        assert "received its report earlier" in retry["note"]
        await chat.shutdown()

    asyncio.run(scenario())


def test_harness_retry_of_a_start_that_never_ran_fails_again(tmp_path: Path):
    async def scenario() -> None:
        store, project, harness, chat, adapter, runtime = _harness_setup(tmp_path)
        seen: dict = {}
        original = chat.prepare_async
        parent: dict = {}

        async def child_prepare_fails(request):
            if request.session_id != parent["session_id"]:
                raise RuntimeError("child provider offline")
            return await original(request)

        chat.prepare_async = child_prepare_fails
        # The cleanup of the unstarted child fails too, so its record stays.
        chat.store.delete_chat_session = _raise_cleanup  # type: ignore[method-assign]

        async def script(connection: ScriptedConnection, prompt: str) -> str:
            del prompt
            seen["first"] = await connection.call("subagent.start", task="Probe.")
            seen["retry"] = await connection.call("subagent.start", task="Probe.")
            return "Probed."

        adapter.script = script
        parent_chat, _, turn = _prepare(runtime, project, harness, "Probe it.")
        parent["session_id"] = parent_chat.id
        await runtime.start_chat_turn(turn.id)

        for key in ("first", "retry"):
            response = seen[key]
            assert response["isError"] is True, key
            failure = response["structuredContent"]
            assert failure["schema"] == FAILURE_SCHEMA
            assert failure["category"] == "execution_failed", key
            assert "child provider offline" not in json.dumps(response)
        (record,) = store.list_entities(ChatSubagent)
        assert record.status == ChatSubagentStatus.FAILED
        assert record.parent_request["not_started"] is True
        await chat.shutdown()

    asyncio.run(scenario())


def _raise_cleanup(session_id: str) -> None:
    raise RuntimeError("cleanup failed")


def test_provider_replay_of_a_start_reports_the_childs_real_status(tmp_path: Path):
    async def scenario() -> None:
        provider = RoutedProvider(
            parent=[
                _call("p1", "start_subagent", task="Map.", name=None, context=None),
                _finish("p2"),
                _response(text="Started."),
            ],
            child=[_response(text="Mapped 4 routes.")],
        )
        store, project, _, chat = _setup(tmp_path, provider)
        prepared = await chat.prepare_async(
            _request(project, content="Delegate.", allow_subagents=True)
        )
        parent_turn_id = chat.start_provider_turn(prepared)
        await _drain(chat, parent_turn_id)
        (record,) = store.list_entities(ChatSubagent)
        await _until(lambda: _settled(store, record.id))
        parent = store.get(ChatTurn, parent_turn_id)
        assert parent.status == ChatTurnStatus.COMPLETE

        # The same step replayed (same idempotency key) reaches the same child.
        replay = ToolInvocation(
            engagement_id=project.id,
            run_id=parent.id,
            origin=ToolCallOrigin.CHAT,
            chat_session_id=parent.session_id,
            chat_turn_id=parent.id,
            tool_name="start_subagent",
            arguments={"task": "Map.", "name": None, "context": None},
            workspace=tmp_path,
            idempotency_key=f"chat:{parent.id}:step:0",
        )
        components = prepared.tool_components
        result = await components.broker.execute(replay, components.scope)
        assert result.output["subagent_id"] == record.id
        assert result.output["status"] == "completed"
        assert "Running in parallel" not in result.output["note"]
        # The idle parent already has its report as a posted result message.
        assert "message_subagent" in result.output["note"]

        # A child that ran and failed is reported as failed, not as a start
        # that could not run.
        store.update(
            ChatSubagent,
            record.id,
            {
                "status": ChatSubagentStatus.FAILED,
                "error": "The subagent's provider returned an error.",
                "finished_at": utc_now(),
            },
            expected_revision=store.get(ChatSubagent, record.id).revision,
        )
        failed = await components.broker.execute(replay, components.scope)
        assert failed.output["status"] == "failed"
        assert "Running in parallel" not in failed.output["note"]
        assert [item.id for item in store.list_entities(ChatSubagent)] == [record.id]
        await chat.shutdown()

    asyncio.run(scenario())
