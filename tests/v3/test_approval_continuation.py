"""Approval delivery contracts; disposable Core, no commands or vendor calls."""

import asyncio

import httpx
import pytest

from nebula.v3.api import create_app
from nebula.v3.domain import Approval, ChatSession, ChatTurn, HarnessTurn, ToolCall
from tests.v3.test_harnesses import _runtime


def fixture(tmp_path, tool_name="run_command"):
    store, project, profile, _, _, runtime = _runtime(tmp_path)
    session = runtime.create_session(
        engagement_id=project.id,
        profile_id=profile.id,
        model="test-model",
        mcp_server_ids=[],
    )
    chat = store.create(
        ChatSession(
            title="Approval fixture",
            engagement_id=project.id,
            backend="harness",
            harness_profile_id=profile.id,
            harness_session_id=session.id,
            model="test-model",
        )
    )
    owner = store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            backend="harness",
            model="test-model",
            status="waiting_approval",
        )
    )
    turn = store.create(
        HarnessTurn(
            engagement_id=project.id,
            harness_session_id=session.id,
            chat_session_id=chat.id,
            chat_turn_id=owner.id,
            origin="chat",
            prompt="Read fixture",
            status="waiting_approval",
        )
    )
    call = store.create(
        ToolCall(
            engagement_id=project.id,
            run_id=owner.id,
            origin="chat",
            chat_turn_id=owner.id,
            tool_name=tool_name,
            risk_class="passive",
            metadata={"harness_turn_id": turn.id},
            status="waiting_approval",
        )
    )
    approval = store.create(
        Approval(
            engagement_id=project.id,
            run_id=owner.id,
            origin="chat",
            chat_session_id=chat.id,
            chat_turn_id=owner.id,
            tool_call_id=call.id,
            risk_class="passive",
            policy_rationale="Fixture confirmation",
            requested_by="test",
            exact_request={
                "tool_name": tool_name,
                "arguments": {"path": "fixture.txt"},
            },
        )
    )
    store.update(
        ChatTurn,
        owner.id,
        {"approval_id": approval.id, "harness_turn_id": turn.id},
        expected_revision=owner.revision,
    )
    app = create_app(store, auth_token="fixture", harness_runtime_service=runtime)
    return app, store, runtime, approval, turn, owner


@pytest.mark.parametrize("tool_name", ["run_command", "read_file"])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_delivers_to_exact_waiter_and_retry_does_not_redeliver(
    tmp_path, tool_name, decision
):
    async def scenario():
        app, store, runtime, approval, turn, _ = fixture(tmp_path, tool_name)
        waiter = asyncio.get_running_loop().create_future()
        unrelated = asyncio.get_running_loop().create_future()
        runtime._approval_futures[approval.id] = waiter
        runtime._approval_futures["unrelated"] = unrelated
        runtime._broker_approval_ids.add(approval.id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            url = f"/api/v1/approvals/{approval.id}/decision"
            response = await client.post(url, json={"decision": decision})
            assert response.status_code == 200, response.text
            assert waiter.done(), "Recorded decision never reached the harness waiter"
            assert waiter.result().allowed == (decision == "approve")
            assert not unrelated.done()
            assert response.json()["continuation"]["status"] == "delivered"
            assert response.json()["continuation"]["harness_turn_id"] == turn.id
            revision = store.get(Approval, approval.id).revision
            retry = await client.post(url, json={"decision": decision})
            assert retry.status_code == 200
            assert retry.json() == response.json()
            assert store.get(Approval, approval.id).revision == revision
            conflict = await client.post(
                url, json={"decision": "reject" if decision == "approve" else "approve"}
            )
            assert conflict.status_code == 409
            assert (
                conflict.json()["detail"]["approval"]["status"]
                == response.json()["status"]
            )

    asyncio.run(scenario())


def test_missing_waiter_preserves_decision_and_interrupts_instead_of_replaying(
    tmp_path,
):
    async def scenario():
        app, store, _, approval, turn, owner = fixture(tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer fixture"},
        ) as client:
            response = await client.post(
                f"/api/v1/approvals/{approval.id}/decision",
                json={"decision": "approve"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "approved"
            assert response.json()["continuation"]["status"] == "failed"
            assert store.get(HarnessTurn, turn.id).status.value == "interrupted"
            assert store.get(ChatTurn, owner.id).status.value == "interrupted"

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_resurrect_terminal_turn(tmp_path):
    async def scenario():
        _, store, runtime, approval, turn, owner = fixture(tmp_path)
        task = asyncio.create_task(runtime._wait_for_broker_approval(turn, approval))
        await asyncio.sleep(0)
        current = store.get(HarnessTurn, turn.id)
        store.update(
            HarnessTurn,
            current.id,
            {"status": "cancelled"},
            expected_revision=current.revision,
        )
        current_owner = store.get(ChatTurn, owner.id)
        store.update(
            ChatTurn,
            owner.id,
            {"status": "cancelled"},
            expected_revision=current_owner.revision,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert store.get(HarnessTurn, turn.id).status.value == "cancelled"
        assert store.get(ChatTurn, owner.id).status.value == "cancelled"

    asyncio.run(scenario())


def test_restart_reconciles_recorded_but_undelivered_decision(tmp_path):
    async def scenario():
        _, store, runtime, approval, turn, owner = fixture(tmp_path)
        store.update(
            Approval,
            approval.id,
            {
                "status": "approved",
                "continuation": {
                    "harness_turn_id": turn.id,
                    "status": "pending",
                },
            },
            expected_revision=approval.revision,
        )
        await runtime.startup()
        assert store.get(Approval, approval.id).status.value == "approved"
        assert store.get(Approval, approval.id).continuation.status == "failed"
        assert store.get(HarnessTurn, turn.id).status.value == "interrupted"
        assert store.get(ChatTurn, owner.id).status.value == "interrupted"
        assert not runtime._approval_futures

    asyncio.run(scenario())


def test_resolving_one_request_keeps_the_other_pending(tmp_path):
    async def scenario():
        _, store, runtime, approval, turn, owner = fixture(tmp_path)
        second = store.create(approval.model_copy(update={"id": "second-request"}))
        first_task = asyncio.create_task(
            runtime._wait_for_broker_approval(turn, approval)
        )
        second_task = asyncio.create_task(
            runtime._wait_for_broker_approval(turn, second)
        )
        await asyncio.sleep(0)
        decided = store.update(
            Approval,
            approval.id,
            {"status": "approved"},
            expected_revision=approval.revision,
        )
        await runtime.resolve_approval(decided)
        await first_task
        assert store.get(HarnessTurn, turn.id).status.value == "waiting_approval"
        assert store.get(ChatTurn, owner.id).approval_id == second.id
        assert not second_task.done()
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_task

    asyncio.run(scenario())


def test_activity_write_failure_reconciles_waiter_without_execution(
    tmp_path, monkeypatch
):
    async def scenario():
        _, store, runtime, approval, turn, owner = fixture(tmp_path)
        waiter = asyncio.get_running_loop().create_future()
        runtime._approval_futures[approval.id] = waiter
        runtime._broker_approval_ids.add(approval.id)
        decided = store.update(
            Approval,
            approval.id,
            {
                "status": "approved",
                "continuation": {"harness_turn_id": turn.id, "status": "pending"},
            },
            expected_revision=approval.revision,
        )

        def unavailable(*args, **kwargs):
            raise RuntimeError("injected activity write failure")

        monkeypatch.setattr(runtime, "_persist_activity", unavailable)
        with pytest.raises(RuntimeError, match="injected activity"):
            await runtime.resolve_approval(decided)
        assert waiter.cancelled(), (
            "An undeliverable decision must not leave an orphan waiter"
        )
        assert approval.id not in runtime._approval_futures
        assert store.get(Approval, approval.id).continuation.status == "failed"
        assert store.get(HarnessTurn, turn.id).status.value == "interrupted"
        assert store.get(ChatTurn, owner.id).status.value == "interrupted"

    asyncio.run(scenario())
