from datetime import timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.v3.test_chat_queue import setup_queue
from nebula.v3.chat_catchup import catchup_router, catchup_projection
from nebula.v3.domain import ChatMessage, ChatReadCursor, ChatSession, ChatTurn, utc_now
from nebula.v3.domain import Approval


def setup(tmp_path):
    store, _, _ = setup_queue(tmp_path)
    app = FastAPI()
    app.include_router(catchup_router(store, None))
    return store, TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "status", ["pending", "approved", "edited", "rejected", "expired", "cancelled"]
)
def test_pending_notice_uses_durable_decision_without_replaying_turn(tmp_path, status):
    store, client = setup(tmp_path)
    approval = store.create(
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
    turn = store.create(
        ChatTurn(
            id="paused",
            model="fixture",
            provider_profile_id=store.get(ChatSession, "s").provider_profile_id,
            engagement_id="p",
            session_id="s",
            status="waiting_approval",
            approval_id=approval.id,
        )
    )
    for _ in range(2):
        result = client.get("/chat/sessions/s/catch-up?device_id=test").json()
        assert bool(result["pending"]) == (status == "pending")
        assert store.get(ChatTurn, turn.id).revision == turn.revision
        assert store.get(ChatTurn, turn.id).status.value == "waiting_approval"
        assert store.get(Approval, approval.id).status.value == status


def test_device_cursor_is_durable_monotonic_and_does_not_dismiss_pending(tmp_path):
    store, client = setup(tmp_path)
    path = "/chat/sessions/s"
    first = client.get(path + "/catch-up?device_id=phone").json()
    assert not first["initialized"] and first["items"] == []
    body = {
        "device_id": "phone",
        "expected_revision": 0,
        "through_at": first["through_at"],
    }
    assert client.put(path + "/read-cursor", json=body).status_code == 200
    assert client.put(path + "/read-cursor", json=body).status_code >= 400
    store.create(
        ChatMessage(
            id="user",
            engagement_id="p",
            session_id="s",
            role="user",
            content="Review this document",
            sequence=1,
        )
    )
    store.create(
        ChatMessage(
            id="answer",
            engagement_id="p",
            session_id="s",
            role="assistant",
            content="Review completed",
            sequence=2,
        )
    )
    store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            model="model-a",
            provider_profile_id="provider-a",
            status="waiting_approval",
        )
    )
    newer = client.get(path + "/catch-up?device_id=phone").json()
    assert any(item["message_id"] == "answer" for item in newer["items"])
    assert newer["pending"]
    assert not client.get(path + "/catch-up?device_id=desktop").json()["initialized"]
    assert (
        client.put(
            path + "/read-cursor",
            json={
                **body,
                "expected_revision": newer["revision"],
                "through_at": newer["through_at"],
            },
        ).status_code
        == 200
    )
    dismissed = client.get(path + "/catch-up?device_id=phone").json()
    assert dismissed["items"] == [] and dismissed["pending"]
    assert (
        client.put(
            path + "/read-cursor",
            json={**body, "expected_revision": dismissed["revision"]},
        ).status_code
        >= 400
    )


def test_greeting_has_no_catchup_but_recorded_outputs_and_failures_do(tmp_path):
    store, _ = setup(tmp_path)
    session = store.get(ChatSession, "s")
    cursor = ChatReadCursor(
        engagement_id="p",
        session_id="s",
        device_id="d",
        through_at=utc_now() - timedelta(minutes=1),
    )
    store.create(
        ChatMessage(
            engagement_id="p", session_id="s", role="user", content="Hello!", sequence=1
        )
    )
    store.create(
        ChatMessage(
            engagement_id="p",
            session_id="s",
            role="assistant",
            content="Hello, how can I help?",
            sequence=2,
        )
    )
    assert catchup_projection(store, session, cursor)["items"] == []
    store.create(
        ChatMessage(
            id="output",
            engagement_id="p",
            session_id="s",
            role="assistant",
            content="```text\nRetained output\n```",
            sequence=3,
        )
    )
    store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            model="model-a",
            provider_profile_id="provider-a",
            status="failed",
            error="Provider unavailable",
        )
    )
    entries = catchup_projection(store, session, cursor)["items"]
    assert {entry["kind"] for entry in entries} == {"results", "failure"}


def test_evidence_uses_recorded_sources_and_sanitizes_legacy_results(tmp_path):
    store, client = setup(tmp_path)
    turn = store.create(
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
    store.create(
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
    evidence = client.get("/chat/sessions/s/messages/answer/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["retrieved"][0]["excerpt"] == "Recorded citation"
    assert "legacy raw secret text" not in evidence.text
    assert "independently verified" in evidence.text
    store.create(
        ChatSession(
            id="other",
            engagement_id="p",
            model="model-a",
            provider_profile_id="provider-a",
            title="Other",
        )
    )
    assert (
        client.get("/chat/sessions/other/messages/answer/evidence").status_code == 404
    )


def test_fork_evidence_retains_provenance_and_explains_deleted_history(tmp_path):
    from nebula.v3.chat import ChatService

    store, client = setup(tmp_path)
    turn = store.create(
        ChatTurn(
            engagement_id="p",
            session_id="s",
            model="model-a",
            provider_profile_id="provider-a",
            status="complete",
        )
    )
    store.create(
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
    service = ChatService(store)
    fork = service.fork_session("s", through_message_id="answer")
    message = service.session_messages(fork.id)[0]
    path = f"/chat/sessions/{fork.id}/messages/{message.id}/evidence"
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["source_message_id"] == "answer"
    store.delete_chat_session("s")
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert "no longer retained" in response.text
    assert response.json()["retrieved"][0]["excerpt"] == "Recorded citation"


def test_conversation_deletion_removes_only_its_device_read_cursors(tmp_path):
    store, client = setup(tmp_path)
    response = client.get("/chat/sessions/s/catch-up?device_id=phone").json()
    assert (
        client.put(
            "/chat/sessions/s/read-cursor",
            json={
                "device_id": "phone",
                "expected_revision": 0,
                "through_at": response["through_at"],
            },
        ).status_code
        == 200
    )
    assert store.count(ChatReadCursor) == 1
    store.delete_chat_session("s")
    assert store.count(ChatReadCursor) == 0
