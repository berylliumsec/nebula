import pytest
from nebula.v3.chat import ChatService
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import ToolCall
from nebula.v3.storage import RevisionConflict
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_chat_tool_loop import _prepared, _response, RecordingBroker

pytestmark = pytest.mark.anyio


async def test_provider_saved_effect_then_bookkeeping_collision(tmp_path):
    store, service, prepared, _provider = await _prepared(
        tmp_path, [], RecordingBroker()
    )
    original_append = service.turn_ledger.append
    effect_calls = []

    async def append(turn_id, entry, **kwargs):
        sequence = await original_append(turn_id, entry, **kwargs)
        current = await store.get(ChatTurn, turn_id)
        await store.update(
            ChatTurn,
            turn_id,
            {
                "request_snapshot": {
                    **current.request_snapshot,
                    "concurrent_writer": "kept",
                }
            },
            expected_revision=current.revision,
        )
        return sequence

    service.turn_ledger.append = append
    effect_calls.append("effect completed before bookkeeping")
    entry = {
        "step": 0,
        "tool_call_id": "effect-1",
        "name": "safe_read",
        "status": "complete",
        "budget_class": "execution",
        "arguments": {},
        "provider_result": "saved",
    }
    saved = await service._save_tool_step(prepared.turn, entry)
    assert effect_calls == ["effect completed before bookkeeping"]
    assert saved.request_snapshot["concurrent_writer"] == "kept"
    assert saved.execution_tool_calls == saved.next_step == 1
    assert len(await service.turn_ledger.history(saved)) == 1


@pytest.mark.parametrize("conflicts", [1, 5])
async def test_provider_effect_runs_once_when_post_effect_projection_conflicts(
    tmp_path,
    conflicts,
):
    broker = RecordingBroker()
    store, service, prepared, _provider = await _prepared(
        tmp_path,
        [
            _response(
                calls=[
                    ToolCall(
                        id="call-1", name="safe_read", arguments={"value": "saved"}
                    )
                ]
            ),
            _response(
                calls=[ToolCall(id="finish-1", name="finish_response", arguments={})]
            ),
            _response(text="Read saved."),
        ],
        broker,
    )
    original_update = store.update
    conflicted = 0

    async def update(model, entity_id, changes, **kwargs):
        nonlocal conflicted
        if (
            model is ChatTurn
            and "ledger_sequence" in changes
            and broker.calls
            and conflicted < conflicts
        ):
            conflicted += 1
            current = await store.get(ChatTurn, entity_id)
            await original_update(
                ChatTurn,
                entity_id,
                {"request_snapshot": {**current.request_snapshot, "peer": "kept"}},
                expected_revision=current.revision,
            )
        return await original_update(model, entity_id, changes, **kwargs)

    store.update = update
    completion = await service.complete(prepared)
    saved = await store.get(ChatTurn, prepared.turn.id)
    assert completion.message.content == "Read saved."
    assert saved.status == ChatTurnStatus.COMPLETE
    assert saved.request_snapshot["peer"] == "kept"
    assert conflicted == conflicts and len(broker.calls) == 1
    assert saved.execution_tool_calls == 1


async def test_provider_retry_exhaustion_recovers_durable_ledger_without_replay(
    tmp_path,
):
    store, service, prepared, _provider = await _prepared(
        tmp_path, [], RecordingBroker()
    )
    original_update = store.update

    async def refuse_projection(model, entity_id, changes, **kwargs):
        if model is ChatTurn and "ledger_sequence" in changes:
            raise RevisionConflict("projection busy")
        return await original_update(model, entity_id, changes, **kwargs)

    store.update = refuse_projection
    entry = {
        "step": 0,
        "tool_call_id": "effect-1",
        "name": "safe_read",
        "status": "complete",
        "budget_class": "execution",
        "provider_result": "saved",
    }
    projected = await service._save_tool_step(prepared.turn, entry)
    assert projected.next_step == projected.execution_tool_calls == 1
    assert (await store.get(ChatTurn, prepared.turn.id)).next_step == 0
    store.update = original_update
    restarted = ChatService(store)
    saved = await restarted._reconcile_tool_bookkeeping(
        await store.get(ChatTurn, prepared.turn.id)
    )
    assert saved.next_step == saved.execution_tool_calls == 1
    assert len(await restarted.turn_ledger.history(saved)) == 1
    again = await restarted._reconcile_tool_bookkeeping(saved)
    assert again.revision == saved.revision


async def test_provider_bookkeeping_keeps_terminal_state_and_other_tool_steps(tmp_path):
    store, service, prepared, _provider = await _prepared(
        tmp_path, [], RecordingBroker()
    )
    first = {
        "step": 0,
        "tool_call_id": "first",
        "name": "safe_read",
        "status": "running",
        "budget_class": "execution",
    }
    second = {
        "step": 1,
        "tool_call_id": "second",
        "name": "safe_read",
        "status": "complete",
        "budget_class": "execution",
    }
    saved = await service._save_tool_step(prepared.turn, first)
    saved = await service._save_tool_step(saved, second)
    saved = await store.update(
        ChatTurn,
        saved.id,
        {"status": ChatTurnStatus.CANCELLED},
        expected_revision=saved.revision,
    )
    saved = await service._update_tool_step(
        prepared.turn, {**first, "status": "complete"}, status=ChatTurnStatus.ROUTING
    )
    assert saved.status == ChatTurnStatus.CANCELLED
    assert saved.execution_tool_calls == saved.next_step == 2
    assert [(item["tool_call_id"], item["status"]) for item in saved.tool_history] == [
        ("first", "complete"),
        ("second", "complete"),
    ]


@pytest.mark.parametrize(
    "waiting", [ChatTurnStatus.WAITING_APPROVAL, ChatTurnStatus.WAITING_CALLBACK]
)
async def test_stale_reconciliation_preserves_current_waiting_state(tmp_path, waiting):
    store, service, prepared, _provider = await _prepared(
        tmp_path, [], RecordingBroker()
    )
    entry = {
        "step": 0,
        "tool_call_id": "effect-1",
        "name": "safe_read",
        "status": "complete",
        "budget_class": "execution",
    }
    sequence = await service.turn_ledger.append(prepared.turn.id, entry)
    current = await store.get(ChatTurn, prepared.turn.id)
    await store.update(
        ChatTurn,
        current.id,
        {"status": waiting, "approval_id": "approval-new", "ledger_sequence": sequence},
        expected_revision=current.revision,
    )
    saved = await service._reconcile_tool_bookkeeping(prepared.turn)
    assert saved.status == waiting
    assert saved.approval_id == "approval-new"
