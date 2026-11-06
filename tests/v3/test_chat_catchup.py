from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from datetime import timedelta
import pytest
from fastapi import FastAPI
from tests.v3.async_test_support import AsyncTestClient as TestClient
from tests.v3.test_chat_queue import setup_queue
from nebula.v3.chat_catchup import catchup_router, catchup_projection
from nebula.v3.domain import ChatMessage, ChatReadCursor, ChatSession, ChatTurn, utc_now
from nebula.v3.domain import Approval


async def setup(tmp_path):
    store, _, _ = await setup_queue(tmp_path)
    app = FastAPI()
    app.include_router(catchup_router(store, None))
    return store, TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "status", ["pending", "approved", "edited", "rejected", "expired", "cancelled"]
)
async def test_pending_notice_uses_durable_decision_without_replaying_turn(
    tmp_path, status
):
    store, client = await setup(tmp_path)
    approval = await store.create(
        Approval(
            engagement_id="p",
            run_id="",
            origin="chat",
            status=status,
            risk_class="passive",
            requested_by="test",
            policy_rationale="Review the file read",
            exact_request={"tool_name": "read_file"},
        )
    )
    turn = await store.create(
        ChatTurn(
            id="paused",
            model="fixture",
            provider_profile_id=(await store.get(ChatSession, "s")).provider_profile_id,
            engagement_id="p",
            session_id="s",
            status="waiting_approval",
            approval_id=approval.id,
        )
    )
    for _ in range(2):
        result = (await client.get("/chat/sessions/s/catch-up?device_id=test")).json()
        assert bool(result["pending"]) == (status == "pending")
        assert (await store.get(ChatTurn, turn.id)).revision == turn.revision
        assert (await store.get(ChatTurn, turn.id)).status.value == "waiting_approval"
        assert (await store.get(Approval, approval.id)).status.value == status


async def test_device_cursor_is_durable_monotonic_and_does_not_dismiss_pending(
    tmp_path,
):
    store, client = await setup(tmp_path)
    path = "/chat/sessions/s"
    first = (await client.get(path + "/catch-up?device_id=phone")).json()
    assert not first["initialized"] and first["items"] == []
    body = {
        "device_id": "phone",
        "expected_revision": 0,
        "through_at": first["through_at"],
    }
    assert (await client.put(path + "/read-cursor", json=body)).status_code == 200
    assert (await client.put(path + "/read-cursor", json=body)).status_code >= 400
    (
        await store.create(
            ChatMessage(
                id="user",
                engagement_id="p",
                session_id="s",
                role="user",
                content="Review this document",
                sequence=1,
            )
        )
    )
    (
        await store.create(
            ChatMessage(
                id="answer",
                engagement_id="p",
                session_id="s",
                role="assistant",
                content="Review completed",
                sequence=2,
            )
        )
    )
    pending_approval = await store.create(
        Approval(
            engagement_id="p",
            run_id="",
            origin="chat",
            requested_by="fixture",
            risk_class="passive",
            policy_rationale="Review the fixture read",
            exact_request={"tool_name": "read_file"},
        )
    )
    (
        await store.create(
            ChatTurn(
                engagement_id="p",
                session_id="s",
                model="model-a",
                provider_profile_id="provider-a",
                status="waiting_approval",
                approval_id=pending_approval.id,
            )
        )
    )
    newer = (await client.get(path + "/catch-up?device_id=phone")).json()
    assert any(item["message_id"] == "answer" for item in newer["items"])
    assert newer["pending"]
    assert not (await client.get(path + "/catch-up?device_id=desktop")).json()[
        "initialized"
    ]
    assert (
        await client.put(
            path + "/read-cursor",
            json={
                **body,
                "expected_revision": newer["revision"],
                "through_at": newer["through_at"],
            },
        )
    ).status_code == 200
    dismissed = (await client.get(path + "/catch-up?device_id=phone")).json()
    assert dismissed["items"] == [] and dismissed["pending"]
    assert (
        await client.put(
            path + "/read-cursor",
            json={**body, "expected_revision": dismissed["revision"]},
        )
    ).status_code >= 400


async def test_greeting_has_no_catchup_but_recorded_outputs_and_failures_do(tmp_path):
    store, _ = await setup(tmp_path)
    session = await store.get(ChatSession, "s")
    cursor = ChatReadCursor(
        engagement_id="p",
        session_id="s",
        device_id="d",
        through_at=utc_now() - timedelta(minutes=1),
    )
    (
        await store.create(
            ChatMessage(
                engagement_id="p",
                session_id="s",
                role="user",
                content="Hello!",
                sequence=1,
            )
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id="p",
                session_id="s",
                role="assistant",
                content="Hello, how can I help?",
                sequence=2,
            )
        )
    )
    assert (await catchup_projection(store, session, cursor))["items"] == []
    (
        await store.create(
            ChatMessage(
                id="output",
                engagement_id="p",
                session_id="s",
                role="assistant",
                content="```text\nRetained output\n```",
                sequence=3,
            )
        )
    )
    (
        await store.create(
            ChatTurn(
                engagement_id="p",
                session_id="s",
                model="model-a",
                provider_profile_id="provider-a",
                status="failed",
                error="Provider unavailable",
            )
        )
    )
    entries = (await catchup_projection(store, session, cursor))["items"]
    assert {entry["kind"] for entry in entries} == {"results", "failure"}


async def test_evidence_uses_recorded_sources_and_sanitizes_legacy_results(tmp_path):
    store, client = await setup(tmp_path)
    turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            model="model-a",
            provider_profile_id="provider-a",
            tool_history=[
                {
                    "name": "read_file",
                    "model_call_id": "call",
                    "status": "complete",
                    "provider_result": "legacy raw secret text",
                }
            ],
        )
    )
    (
        await store.create(
            ChatMessage(
                id="answer",
                engagement_id="p",
                session_id="s",
                role="assistant",
                sequence=1,
                content="Interpretation",
                citations=[
                    {
                        "source_id": "source",
                        "name": "Document",
                        "chunk_id": "c",
                        "excerpt": "Recorded citation",
                    }
                ],
                metadata={"chat_turn_id": turn.id},
            )
        )
    )
    evidence = await client.get("/chat/sessions/s/messages/answer/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["retrieved"][0]["excerpt"] == "Recorded citation"
    assert "legacy raw secret text" not in evidence.text
    assert "independently verified" in evidence.text
    (
        await store.create(
            ChatSession(
                id="other",
                engagement_id="p",
                model="model-a",
                provider_profile_id="provider-a",
                title="Other",
            )
        )
    )
    assert (
        await client.get("/chat/sessions/other/messages/answer/evidence")
    ).status_code == 404


async def test_fork_evidence_retains_provenance_and_explains_deleted_history(tmp_path):
    from nebula.v3.chat import ChatService

    store, client = await setup(tmp_path)
    turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            model="model-a",
            provider_profile_id="provider-a",
            status="complete",
        )
    )
    (
        await store.create(
            ChatMessage(
                id="answer",
                engagement_id="p",
                session_id="s",
                role="assistant",
                sequence=1,
                content="Original interpretation",
                citations=[
                    {
                        "source_id": "source",
                        "name": "Document",
                        "chunk_id": "c",
                        "excerpt": "Recorded citation",
                    }
                ],
                metadata={"chat_turn_id": turn.id},
            )
        )
    )
    service = ChatService(store)
    fork = await service.fork_session("s", through_message_id="answer")
    message = (await service.session_messages(fork.id))[0]
    path = f"/chat/sessions/{fork.id}/messages/{message.id}/evidence"
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["source_message_id"] == "answer"
    (await store.delete_chat_session("s"))
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert "no longer retained" in response.text
    assert response.json()["retrieved"][0]["excerpt"] == "Recorded citation"


async def test_conversation_deletion_removes_only_its_device_read_cursors(tmp_path):
    store, client = await setup(tmp_path)
    response = (await client.get("/chat/sessions/s/catch-up?device_id=phone")).json()
    assert (
        await client.put(
            "/chat/sessions/s/read-cursor",
            json={
                "device_id": "phone",
                "expected_revision": 0,
                "through_at": response["through_at"],
            },
        )
    ).status_code == 200
    assert (await store.count(ChatReadCursor)) == 1
    (await store.delete_chat_session("s"))
    assert (await store.count(ChatReadCursor)) == 0


pytestmark = pytest.mark.anyio
