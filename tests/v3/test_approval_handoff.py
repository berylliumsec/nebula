"""Exact native handoff bookkeeping; no tools or external adapters execute."""

import asyncio

import httpx
import pytest

from nebula.v3.domain import Approval, ChatSession, HarnessTurn, ToolCall
from nebula.v3.session_state import session_state
from tests.v3.test_approval_continuation import fixture


async def decide_native(tmp_path):
    app, store, runtime, approval, turn, owner = fixture(tmp_path, "read_fixture")
    call = store.get(ToolCall, approval.tool_call_id)
    store.update(
        ToolCall,
        call.id,
        {"metadata": {**call.metadata, "adapter_handoff": "transport_write"}},
        expected_revision=call.revision,
    )
    future = asyncio.get_running_loop().create_future()
    runtime._approval_futures[approval.id] = future
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer fixture"},
    ) as client:
        response = await client.post(
            f"/api/v1/approvals/{approval.id}/decision", json={"decision": "approve"}
        )
        assert response.status_code == 200, response.text
    assert future.done()
    return (
        store,
        runtime,
        store.get(Approval, approval.id),
        turn,
        owner,
        response.json(),
    )


def test_decision_response_distinguishes_waiter_delivery_from_adapter_handoff(tmp_path):
    async def scenario():
        store, runtime, approval, turn, owner, response = await decide_native(tmp_path)
        assert response["continuation"]["status"] == "delivered"
        assert response["continuation"]["adapter_status"] == "pending"
        assert response["continuation"]["adapter_handoff"] == "transport_write"
        # The legacy running reservation is not evidence of execution progress.
        current = store.get(HarnessTurn, turn.id)
        store.update(
            HarnessTurn,
            turn.id,
            {"status": "running"},
            expected_revision=current.revision,
        )
        state = session_state(store, store.get(ChatSession, owner.session_id), runtime)
        assert state["execution"] == "continuing"
        assert state["decisions"][0]["progress"] == "not_observed"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["sent", "failed"])
def test_adapter_handoff_is_bound_idempotent_and_does_not_claim_execution(
    tmp_path, status
):
    from nebula.v3.approval_delivery import record_adapter_handoff
    from nebula.v3.storage import ConflictError

    async def scenario():
        store, runtime, approval, turn, owner, _ = await decide_native(tmp_path)
        with pytest.raises(ConflictError):
            record_adapter_handoff(store, approval.id, "unrelated-turn", status)
        saved = record_adapter_handoff(store, approval.id, turn.id, status)
        assert saved.status.value == "approved"
        assert saved.continuation.status == "delivered"
        assert saved.continuation.adapter_status == status
        assert record_adapter_handoff(store, approval.id, turn.id, status) == saved
        # A late conflicting receipt cannot rewrite a previously recorded handoff.
        assert (
            record_adapter_handoff(
                store, approval.id, turn.id, "failed" if status == "sent" else "sent"
            )
            == saved
        )
        state = session_state(store, store.get(ChatSession, owner.session_id), runtime)
        assert state["execution"] == (
            "continuing" if status == "sent" else "interrupted"
        )
        assert state["decisions"][0]["progress"] == "not_observed"

    asyncio.run(scenario())


def test_only_ordered_execution_activity_in_the_owning_turn_advances_progress(tmp_path):
    from nebula.v3.approval_delivery import record_adapter_handoff

    async def scenario():
        store, runtime, approval, turn, owner, _ = await decide_native(tmp_path)
        store.append_operation_event(
            turn.id,
            "harness_turn",
            turn.engagement_id,
            "harness.message_delta",
            {"delta": "queued before handoff"},
        )
        record_adapter_handoff(store, approval.id, turn.id, "sent")
        store.append_operation_event(
            "unrelated-turn",
            "harness_turn",
            turn.engagement_id,
            "harness.message_delta",
            {"delta": "other"},
        )
        store.append_operation_event(
            turn.id,
            "harness_turn",
            turn.engagement_id,
            "harness.status",
            {"phase": "running"},
        )
        chat = store.get(ChatSession, owner.session_id)
        state = session_state(store, chat, runtime)
        assert state["decisions"][0]["progress"] == "not_observed"
        store.append_operation_event(
            turn.id,
            "harness_turn",
            turn.engagement_id,
            "harness.message_delta",
            {"delta": "inert reply"},
        )
        progressed = session_state(store, chat, runtime)
        assert progressed["decisions"][0]["progress"] == "observed"
        assert progressed["revision"] > state["revision"]

    asyncio.run(scenario())


def test_restart_after_waiter_delivery_preserves_delivery_and_records_unknown_handoff(
    tmp_path,
):
    async def scenario():
        store, runtime, approval, turn, owner, _ = await decide_native(tmp_path)
        await runtime.startup()
        retained = store.get(Approval, approval.id)
        assert retained.status.value == "approved"
        assert retained.continuation.status == "delivered"
        assert retained.continuation.adapter_status == "unknown"
        assert store.get(HarnessTurn, turn.id).status.value == "interrupted"
        assert (
            session_state(store, store.get(ChatSession, owner.session_id), runtime)[
                "pending"
            ]
            == []
        )
        assert not runtime._approval_futures

    asyncio.run(scenario())


@pytest.mark.parametrize("adapter", ["codex", "grok"])
@pytest.mark.parametrize("fail_write", [False, True])
def test_real_permission_consumers_acknowledge_one_exact_transport_write(
    tmp_path, adapter, fail_write
):
    from types import SimpleNamespace
    from nebula.v3.approval_delivery import record_adapter_handoff
    from nebula.v3.harnesses import (
        CodexAppServerConnection,
        GrokAcpConnection,
        HarnessPermissionDecision,
        PermissionTicket,
    )

    async def scenario():
        store, _, approval, turn, _, _ = await decide_native(tmp_path)
        future = asyncio.get_running_loop().create_future()
        future.set_result(
            HarnessPermissionDecision(allowed=True, approval_id=approval.id)
        )
        writes = []

        async def respond(request_id, result):
            writes.append((request_id, result))
            assert (
                store.get(Approval, approval.id).continuation.adapter_status
                == "pending"
            )
            if fail_write:
                raise OSError("injected transport exit")

        async def permission_handler(request):
            assert request.vendor_request_id == "original-request"
            return PermissionTicket(
                approval.id,
                approval.tool_call_id,
                future,
                lambda status: record_adapter_handoff(
                    store, approval.id, turn.id, status
                ),
            )

        rpc = SimpleNamespace(respond=respond)
        if adapter == "codex":
            connection = CodexAppServerConnection(
                rpc, external_session_id="inert", permission_handler=permission_handler
            )
            events = connection._approval(
                {"id": "original-request"},
                "item/commandExecution/requestApproval",
                {"command": "inert fixture; never executed"},
            )
        else:
            connection = GrokAcpConnection(
                rpc, external_session_id="inert", permission_handler=permission_handler
            )
            events = connection._permission(
                {"id": "original-request"},
                {"options": [{"optionId": "allow", "kind": "allow_once"}]},
            )
        if fail_write:
            with pytest.raises(OSError, match="injected transport"):
                _ = [event async for event in events]
        else:
            _ = [event async for event in events]
        assert len(writes) == 1 and writes[0][0] == "original-request"
        retained = store.get(Approval, approval.id)
        assert retained.continuation.adapter_status == (
            "failed" if fail_write else "sent"
        )
        assert retained.status.value == "approved"

    asyncio.run(scenario())


def test_receipt_storage_failure_after_write_never_resends_and_restart_is_uncertain(
    tmp_path,
):
    from types import SimpleNamespace
    from nebula.v3.harnesses import PermissionTicket, _respond_permission

    async def scenario():
        store, runtime, approval, _, _, _ = await decide_native(tmp_path)
        writes = []

        async def respond(request_id, result):
            writes.append(request_id)

        def unavailable(status):
            raise RuntimeError("injected receipt persistence failure")

        ticket = PermissionTicket(
            approval.id,
            approval.tool_call_id,
            asyncio.get_running_loop().create_future(),
            unavailable,
        )
        with pytest.raises(RuntimeError, match="injected receipt"):
            await _respond_permission(
                SimpleNamespace(respond=respond),
                ticket,
                "original",
                {"decision": "accept"},
            )
        assert writes == ["original"]
        assert store.get(Approval, approval.id).continuation.adapter_status == "pending"
        await runtime.startup()
        assert store.get(Approval, approval.id).continuation.adapter_status == "unknown"
        assert writes == ["original"]

    asyncio.run(scenario())


def test_missing_exact_binding_does_not_guess_a_turn_from_chat_history(tmp_path):
    from nebula.v3.harnesses import HarnessStateError

    async def scenario():
        _, store, runtime, approval, turn, _ = fixture(tmp_path)
        call = store.get(ToolCall, approval.tool_call_id)
        store.update(
            ToolCall, call.id, {"metadata": {}}, expected_revision=call.revision
        )
        decided = store.update(
            Approval,
            approval.id,
            {
                "status": "approved",
                "continuation": {"harness_turn_id": turn.id, "status": "pending"},
            },
            expected_revision=approval.revision,
        )
        future = asyncio.get_running_loop().create_future()
        runtime._approval_futures[approval.id] = future
        with pytest.raises(HarnessStateError, match="binding"):
            await runtime.resolve_approval(decided)
        assert future.cancelled()
        assert store.get(Approval, approval.id).continuation.status == "failed"
        assert store.get(HarnessTurn, turn.id).status.value == "interrupted"

    asyncio.run(scenario())
