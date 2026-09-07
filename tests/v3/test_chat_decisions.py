import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.v3.test_chat_queue import setup_queue, enqueue
from nebula.v3.chat_decisions import decisions_router, decision_snapshot
from nebula.v3.domain import ChatSession, ChatMessage, ChatTurn


def fixture(tmp_path):
    store, queue, request = setup_queue(tmp_path)
    store.create(
        ChatMessage(
            id="source",
            engagement_id="p",
            session_id="s",
            sequence=1,
            role="user",
            content="Use plain language. Keep answers short.",
        )
    )
    app = FastAPI()
    app.include_router(decisions_router(store))
    client = TestClient(app, raise_server_exceptions=False)
    return store, queue, request, client


def test_explicit_revision_provenance_and_project_promotion(tmp_path):
    store, _, _, client = fixture(tmp_path)
    path = "/chat/sessions/s/decisions/d"
    created = client.put(
        path,
        json={
            "expected_revision": 0,
            "kind": "constraint",
            "text": "Use plain language",
            "source_message_id": "source",
            "source_selection": "Use plain language.",
        },
    )
    assert created.status_code == 200, created.text
    assert (
        client.put(
            path,
            json={
                "expected_revision": 1,
                "text": "Use plain language and short paragraphs",
            },
        ).status_code
        == 200
    )
    assert (
        client.put(path, json={"expected_revision": 1, "text": "stale"}).status_code
        >= 400
    )
    item = client.get("/chat/sessions/s/decisions").json()[0]
    assert item["history"][0]["text"] == "Use plain language"
    assert item["source_selection"] == "Use plain language."
    assert (
        client.put(path, json={"expected_revision": 2, "action": "promote"}).status_code
        == 200
    )
    store.create(
        ChatSession(
            id="other",
            engagement_id="p",
            title="Other",
            model="model-a",
            provider_profile_id="provider-a",
        )
    )
    snapshot = decision_snapshot(store, "other", "p")
    assert len(snapshot) == 1 and snapshot[0]["scope"] == "project"
    assert snapshot[0]["source_message_id"] == "source"
    assert len(decision_snapshot(store, "s", "p")) == 1


def test_sources_must_be_exact_and_forks_keep_applicable_provenance(tmp_path):
    store, queue, _, client = fixture(tmp_path)
    path = "/chat/sessions/s/decisions/d"
    assert (
        client.put(
            path,
            json={
                "expected_revision": 0,
                "text": "Decision",
                "source_message_id": "source",
                "source_selection": "Invented text",
            },
        ).status_code
        == 422
    )
    assert (
        client.put(
            path,
            json={
                "expected_revision": 0,
                "text": "Decision",
                "source_message_id": "source",
                "source_selection": "Use plain language.",
            },
        ).status_code
        == 200
    )
    before = queue.chat.fork_session("s", before_message_id="source")
    assert decision_snapshot(store, before.id, "p") == []
    after = queue.chat.fork_session("s", through_message_id="source")
    copied = decision_snapshot(store, after.id, "p")
    assert copied[0]["source_message_id"] == "source"
    assert copied[0]["id"] != "d"


def test_queued_dispatch_uses_current_decisions_and_freezes_revision(tmp_path):
    store, service, request, client = fixture(tmp_path)
    path = "/chat/sessions/s/decisions/d"

    async def run():
        queue = enqueue(service, request, paused=True)
        assert (
            client.put(
                path, json={"expected_revision": 0, "text": "First decision"}
            ).status_code
            == 200
        )
        assert (
            client.put(
                path, json={"expected_revision": 1, "text": "Current dispatch decision"}
            ).status_code
            == 200
        )
        from nebula.v3.chat_queue import QueueWrite

        queue = service.write(
            "s", QueueWrite(action="resume", expected_revision=queue.revision)
        )
        await service.step(queue)
        item = service.get("s").items[0]
        turn = store.get(ChatTurn, item["turn_id"])
        assert turn.request_snapshot["operator_decisions"][0]["revision"] == 2
        assert (
            "Current dispatch decision"
            in turn.request_snapshot["model_request"]["instructions"]
        )
        assert (
            client.put(
                path, json={"expected_revision": 2, "text": "Only subsequent turns"}
            ).status_code
            == 200
        )
        assert (
            store.get(ChatTurn, item["turn_id"]).request_snapshot["operator_decisions"][
                0
            ]["text"]
            == "Current dispatch decision"
        )
        await service.chat.shutdown()

    asyncio.run(run())


def test_managed_chat_records_cannot_bypass_workflow_routes(tmp_path):
    from nebula.v3.api import create_app
    from nebula.v3.storage import NebulaStore

    app = create_app(NebulaStore(tmp_path / "routes.db"))
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    for kind in ("chat_bookmarks", "chat_queues", "chat_decisions"):
        assert not any(f"/{kind}" in path for path in paths)


def test_harness_submits_and_freezes_explicit_context(tmp_path):
    from tests.v3.test_harnesses import _runtime
    from nebula.v3.domain import ChatDecision

    store, project, profile, _, _, runtime = _runtime(tmp_path)
    chat, _, _ = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="Hello",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    # The first turn is deliberately not executed; create a project entry and
    # prepare a separate chat to verify the shared scope boundary.
    decision = store.create(
        ChatDecision(
            engagement_id=project.id,
            scope="project",
            kind="constraint",
            text="Explain uncertainty explicitly",
        )
    )
    _, turn, harness_turn = runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=profile.id,
        model=None,
        prompt="Explain the plan",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    assert turn.request_snapshot["operator_decisions"][0]["id"] == decision.id
    assert "Explain uncertainty explicitly" in harness_turn.prompt
    store.update(
        ChatDecision,
        decision.id,
        {"text": "Only future work"},
        expected_revision=decision.revision,
    )
    assert "Only future work" not in harness_turn.prompt
