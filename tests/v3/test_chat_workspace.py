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
        store.create(
            ChatSession(
                id=identity,
                engagement_id=project,
                title="Hello",
                provider_profile_id="provider",
                model="m",
            )
        )
        for i, text in enumerate(
            ["Hello", "Review the document", "Document reviewed"], 1
        ):
            store.create(
                ChatMessage(
                    id=f"{identity}-{i}",
                    engagement_id=project,
                    session_id=identity,
                    sequence=i,
                    role="assistant" if i == 3 else "user",
                    content=text,
                )
            )
    app = FastAPI()
    app.include_router(workspace_router(store))
    return store, TestClient(app, raise_server_exceptions=False)


def test_search_is_project_scoped_paginated_and_literal(tmp_path):
    _, client = workspace(tmp_path)
    data = client.get(
        "/chat/projects/p/search", params={"q": "document", "limit": 1}
    ).json()
    assert len(data["items"]) == 1 and data["next_offset"] == 1
    assert data["items"][0]["session_id"] == "s"
    assert (
        client.get("/chat/projects/p/search", params={"q": "%"}).json()["items"] == []
    )


def test_bookmark_revision_and_cross_session_boundary(tmp_path):
    store, client = workspace(tmp_path)
    path = "/chat/sessions/s/bookmarks/s-2"
    response = client.put(path, json={"active": True, "expected_revision": 0})
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    assert (
        client.get("/chat/projects/p/search?bookmarked=true").json()["items"][0][
            "message_id"
        ]
        == "s-2"
    )
    assert (
        client.put(
            path, json={"active": False, "expected_revision": revision}
        ).status_code
        == 200
    )
    assert client.get("/chat/projects/p/search?bookmarked=true").json()["items"] == []
    assert (
        client.put(
            path, json={"active": True, "expected_revision": revision}
        ).status_code
        >= 400
    )
    assert (
        client.put(
            "/chat/sessions/s/bookmarks/outside-2",
            json={"active": True, "expected_revision": 0},
        ).status_code
        >= 400
    )
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
    assert (
        substantive_prompt(["Hello!", "Thanks", "Review the document"])
        == "Review the document"
    )
    assert substantive_prompt(["Hi"]) == ""
    session = store.get(ChatSession, "s")
    assert should_name(session)
    assert not should_name(
        session.model_copy(update={"metadata": {"initial_title_state": "operator"}})
    )


def test_results_only_project_owned_retained_sources(tmp_path):
    from nebula.v3.chat_results import results_router
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.knowledge import ingest_document

    store, _ = workspace(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    source = ingest_document(
        store=store,
        artifact_store=artifacts,
        engagement_id="p",
        filename="notes.txt",
        data=b"Exact source\nwith whitespace\n",
    )
    app = FastAPI()
    app.include_router(results_router(store, artifacts))
    client = TestClient(app, raise_server_exceptions=False)
    preview = client.get(f"/chat/projects/p/sources/{source.id}/preview")
    assert preview.status_code == 200, preview.text
    assert "Exact source" in preview.json()["text"]
    assert (
        client.get(f"/chat/projects/other/sources/{source.id}/preview").status_code
        >= 400
    )
    store.create(
        ChatMessage(
            engagement_id="p",
            session_id="s",
            role="assistant",
            sequence=4,
            content="Example:\n```text\nretained code\n```",
        )
    )
    results = client.get("/chat/sessions/s/results").json()
    assert results["items"][0]["text"] == "retained code\n"
    assert results["items"][0]["artifact_id"] is None


def _retract(store, message_id):
    message = store.get(ChatMessage, message_id)
    store.update(
        ChatMessage,
        message_id,
        {"metadata": {**message.metadata, "retracted_at": "2026-09-19T00:00:00+00:00"}},
        expected_revision=message.revision,
    )


def _walk(client, path, params):
    seen, offset = [], 0
    while offset is not None:
        response = client.get(path, params={**params, "offset": offset})
        assert response.status_code == 200, response.text
        data = response.json()
        seen.extend(data["items"])
        offset = data["next_offset"]
    return seen


def test_search_pages_are_complete_after_an_in_place_edit(tmp_path):
    store, client = workspace(tmp_path)
    for i, text in ((4, "document four"), (5, "document five")):
        store.create(
            ChatMessage(
                id=f"s-{i}",
                engagement_id="p",
                session_id="s",
                sequence=i,
                role="user",
                content=text,
            )
        )
    _retract(store, "s-2")

    items = _walk(client, "/chat/projects/p/search", {"q": "document", "limit": 2})

    assert sorted(item["message_id"] for item in items) == ["s-3", "s-4", "s-5"]


def test_results_pages_are_complete_after_an_in_place_edit(tmp_path):
    from nebula.v3.chat_results import results_router

    store, _ = workspace(tmp_path)
    for i in range(4, 9):
        store.create(
            ChatMessage(
                id=f"s-{i}",
                engagement_id="p",
                session_id="s",
                sequence=i,
                role="assistant",
                content=f"```text\nresult {i}\n```",
            )
        )
    _retract(store, "s-4")
    app = FastAPI()
    app.include_router(results_router(store, None))
    client = TestClient(app, raise_server_exceptions=False)

    items = _walk(client, "/chat/sessions/s/results", {"limit": 2})

    assert sorted(item["message_id"] for item in items) == ["s-5", "s-6", "s-7", "s-8"]
    assert _walk(client, "/chat/sessions/s/context-sources", {}) == []


def test_results_survive_a_missing_diff_artifact(tmp_path):
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.chat_results import results_router
    from nebula.v3.domain import Artifact

    store, _ = workspace(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    digest = "ab" * 32
    store.create(
        ChatMessage(
            id="s-9",
            engagement_id="p",
            session_id="s",
            sequence=9,
            role="assistant",
            content="Changed files.",
            metadata={"harness_turn_id": "turn-1"},
        )
    )
    store.create(
        Artifact(
            id="diff-1",
            engagement_id="p",
            sha256=digest,
            size=12,
            storage_path=str(
                artifacts.path_for_digest(digest).relative_to(artifacts.root)
            ),
            source="harness-file-diff",
            metadata={"harness_turn_id": "turn-1"},
        )
    )
    app = FastAPI()
    app.include_router(results_router(store, artifacts))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/chat/sessions/s/results")

    assert response.status_code == 200, response.text
    (item,) = [row for row in response.json()["items"] if row["kind"] == "file_change"]
    assert item["artifact_id"] == "diff-1"
    assert "unavailable" in item["text"].casefold()
