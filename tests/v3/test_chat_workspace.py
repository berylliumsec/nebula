from fastapi import FastAPI
from fastapi.testclient import TestClient
from nebula.v3.chat_workspace import workspace_router
from nebula.v3.chat_naming import substantive_prompt, should_name
from nebula.v3.chat import ChatService
from nebula.v3.domain import ChatSession, ChatMessage, Engagement
from nebula.v3.storage import NebulaStore


def workspace(tmp_path):
    store = NebulaStore(tmp_path / "chat.db")
    store.create(Engagement(id="p", name="Project"))
    store.create(Engagement(id="other", name="Other"))
    for identity, project in [("s", "p"), ("outside", "other")]:
        store.create(ChatSession(id=identity, engagement_id=project, title="Hello", provider_profile_id="provider", model="m"))
        for i, text in enumerate(["Hello", "Review the document", "Document reviewed"], 1):
            store.create(ChatMessage(id=f"{identity}-{i}", engagement_id=project, session_id=identity, sequence=i, role="assistant" if i == 3 else "user", content=text))
    app = FastAPI()
    app.include_router(workspace_router(store))
    return store, TestClient(app, raise_server_exceptions=False)


def test_search_is_project_scoped_paginated_and_literal(tmp_path):
    _, client = workspace(tmp_path)
    data = client.get("/chat/projects/p/search", params={"q": "document", "limit": 1}).json()
    assert len(data["items"]) == 1 and data["next_offset"] == 1
    assert data["items"][0]["session_id"] == "s"
    assert client.get("/chat/projects/p/search", params={"q": "%"}).json()["items"] == []


def test_bookmark_revision_and_cross_session_boundary(tmp_path):
    store, client = workspace(tmp_path)
    path = "/chat/sessions/s/bookmarks/s-2"
    response = client.put(path, json={"active": True, "expected_revision": 0})
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    assert client.get("/chat/projects/p/search?bookmarked=true").json()["items"][0]["message_id"] == "s-2"
    assert client.put(path, json={"active": False, "expected_revision": revision}).status_code == 200
    assert client.get("/chat/projects/p/search?bookmarked=true").json()["items"] == []
    assert client.put(path, json={"active": True, "expected_revision": revision}).status_code >= 400
    assert client.put("/chat/sessions/s/bookmarks/outside-2", json={"active": True, "expected_revision": 0}).status_code >= 400
    store.delete_chat_session("s")
    from nebula.v3.domain import ChatBookmark
    assert store.count(ChatBookmark) == 0


def test_edit_branch_excludes_edited_message_and_keeps_original(tmp_path):
    store, _ = workspace(tmp_path)
    service = ChatService(store)
    empty = service.fork_session("s", before_message_id="s-1")
    assert service.session_messages(empty.id) == []
    fork = service.fork_session("s", before_message_id="s-2")
    assert [m.content for m in service.session_messages(fork.id)] == ["Hello"]
    assert len(service.session_messages("s")) == 3


def test_naming_waits_for_content_and_preserves_manual_titles(tmp_path):
    store, _ = workspace(tmp_path)
    assert substantive_prompt(["Hello!", "Thanks", "Review the document"]) == "Review the document"
    assert substantive_prompt(["Hi"]) == ""
    session = store.get(ChatSession, "s")
    assert should_name(session)
    assert not should_name(session.model_copy(update={"metadata": {"initial_title_state": "operator"}}))
