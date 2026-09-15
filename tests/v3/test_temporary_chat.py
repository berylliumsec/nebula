from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from nebula.v3.chat import ChatService
from nebula.v3.chat_workspace import workspace_router
from nebula.v3.domain import ChatSession, ChatMessage, Engagement, HarnessSession
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.temporary_chat import temporary_chat_router


@pytest.fixture
def popup(tmp_path):
    store = NebulaStore(tmp_path / "popup.db")
    store.create(Engagement(id="p", name="Project"))
    store.create(Engagement(id="other", name="Other"))
    store.create(
        ChatSession(
            id="main",
            engagement_id="p",
            title="Main",
            provider_profile_id="provider",
            model="m",
        )
    )
    store.create(
        ChatMessage(
            id="message",
            engagement_id="p",
            session_id="main",
            sequence=1,
            role="user",
            content="Original history",
        )
    )
    service = ChatService(store)
    runtime = Mock()
    runtime.cancel_turn = AsyncMock()
    runtime.close_session = AsyncMock()
    app = FastAPI()
    app.include_router(temporary_chat_router(store, lambda: service, runtime))
    app.include_router(workspace_router(store))
    return store, TestClient(app), runtime


def create(client):
    response = client.post(
        "/chat/temporary-sessions", json={"engagement_id": "p", "session_id": "main"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_snapshot_is_independent_hidden_and_discarded(popup):
    store, client, _ = popup
    branch = create(client)
    messages = ChatService(store).session_messages(branch)
    assert [(m.content, m.source_message_id) for m in messages] == [
        ("Original history", "message")
    ]
    assert messages[0].id != "message"
    assert [c.id for c in store.list_entities(ChatSession)] == ["main"]
    assert store.count(ChatSession) == 1
    assert len(store.list_entities(ChatSession, include_temporary=True)) == 2
    store.create(
        ChatMessage(
            engagement_id="p",
            session_id=branch,
            sequence=2,
            role="assistant",
            content="Popup-only answer",
        )
    )
    assert client.get("/chat/projects/p/search?q=Popup-only").json()["items"] == []
    assert client.delete(f"/chat/temporary-sessions/{branch}").status_code == 204
    assert client.delete(f"/chat/temporary-sessions/{branch}").status_code == 204
    with pytest.raises(NotFoundError):
        store.get(ChatSession, branch)
    assert len(ChatService(store).session_messages("main")) == 1


def test_cannot_discard_main_or_copy_another_project(popup):
    store, client, _ = popup
    assert client.delete("/chat/temporary-sessions/main").status_code == 409
    assert (
        client.post(
            "/chat/temporary-sessions",
            json={"engagement_id": "other", "session_id": "main"},
        ).status_code
        == 409
    )
    assert store.count(ChatSession) == 1


def test_new_question_without_saved_history_is_hidden(popup):
    store, client, _ = popup
    response = client.post(
        "/chat/temporary-sessions",
        json={"engagement_id": "p", "provider_id": "provider", "model": "m"},
    )
    assert response.status_code == 201, response.text
    assert ChatService(store).session_messages(response.json()["id"]) == []
    assert store.count(ChatSession) == 1


def test_harness_fork_uses_fresh_runtime_and_snapshot(popup):
    store, client, runtime = popup
    main_runtime = store.create(
        HarnessSession(
            id="runtime-main",
            engagement_id="p",
            harness_profile_id="harness",
            model="m",
        )
    )
    main = store.get(ChatSession, "main")
    store.update(
        ChatSession,
        main.id,
        {
            "backend": "harness",
            "provider_profile_id": None,
            "harness_profile_id": "harness",
            "harness_session_id": main_runtime.id,
        },
    )
    fresh = store.create(
        HarnessSession(
            id="runtime-popup",
            engagement_id="p",
            harness_profile_id="harness",
            model="m",
        )
    )
    runtime.create_session.return_value = fresh
    branch = create(client)
    saved = store.get(ChatSession, branch)
    assert saved.harness_session_id == fresh.id
    assert saved.metadata["harness_context_handoff_pending"] is True
    assert client.delete(f"/chat/temporary-sessions/{branch}").status_code == 204
    runtime.close_session.assert_awaited_once_with(fresh.id)
    assert store.get(HarnessSession, main_runtime.id)
    with pytest.raises(NotFoundError):
        store.get(HarnessSession, fresh.id)


def test_hidden_branch_has_no_universal_search_projection(popup):
    from nebula.v3.search import project_search_document

    store, client, _ = popup
    branch = create(client)
    assert (
        project_search_document(
            "chat_sessions", store.get(ChatSession, branch).model_dump(mode="json")
        )
        is None
    )
    assert (
        project_search_document(
            "chat_sessions", store.get(ChatSession, "main").model_dump(mode="json")
        )
        is not None
    )


def test_expired_branch_is_collected_after_restart(popup):
    from datetime import timedelta
    from nebula.v3.domain import utc_now
    import time

    store, client, _ = popup
    store.create(
        ChatSession(
            id="expired",
            engagement_id="p",
            title="Expired",
            provider_profile_id="provider",
            model="m",
            created_at=utc_now() - timedelta(hours=2),
            metadata={"temporary_assistant": True},
        )
    )
    with client:
        for _ in range(100):
            if not any(
                row.id == "expired"
                for row in store.list_entities(ChatSession, include_temporary=True)
            ):
                break
            time.sleep(0.01)
        else:
            pytest.fail("Expired popup was not collected")
    assert store.get(ChatSession, "main")
