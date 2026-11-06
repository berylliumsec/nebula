from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from unittest.mock import AsyncMock, Mock
from datetime import timedelta

from fastapi import FastAPI
from tests.v3.async_test_support import AsyncTestClient as TestClient
import pytest

from nebula.v3.chat import ChatService
from nebula.v3.chat_workspace import workspace_router
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessSession,
    utc_now,
)
from nebula.v3.storage import NebulaStore, NotFoundError
from nebula.v3.temporary_chat import temporary_chat_expired, temporary_chat_router


@pytest.fixture
async def popup(tmp_path):
    store = NebulaStore(tmp_path / "popup.db")
    (await store.create(Engagement(id="p", name="Project")))
    (await store.create(Engagement(id="other", name="Other")))
    (
        await store.create(
            ChatSession(
                id="main",
                engagement_id="p",
                title="Main",
                provider_profile_id="provider",
                model="m",
            )
        )
    )
    (
        await store.create(
            ChatMessage(
                id="message",
                engagement_id="p",
                session_id="main",
                sequence=1,
                role="user",
                content="Original history",
            )
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


async def test_snapshot_is_independent_hidden_and_discarded(popup):
    store, client, _ = popup
    branch = create(client)
    messages = await ChatService(store).session_messages(branch)
    assert [(m.content, m.source_message_id) for m in messages] == [
        ("Original history", "message")
    ]
    assert messages[0].id != "message"
    assert [c.id for c in (await store.list_entities(ChatSession))] == ["main"]
    assert (await store.count(ChatSession)) == 1
    assert len((await store.list_entities(ChatSession, include_temporary=True))) == 2
    (
        await store.create(
            ChatMessage(
                engagement_id="p",
                session_id=branch,
                sequence=2,
                role="assistant",
                content="Popup-only answer",
            )
        )
    )
    assert (await client.get("/chat/projects/p/search?q=Popup-only")).json()[
        "items"
    ] == []
    assert (
        await client.delete(f"/chat/temporary-sessions/{branch}")
    ).status_code == 204
    assert (
        await client.delete(f"/chat/temporary-sessions/{branch}")
    ).status_code == 204
    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, branch))
    assert len((await ChatService(store).session_messages("main"))) == 1


async def test_side_chat_is_hidden_and_discarded_without_touching_parent(popup):
    from nebula.v3.search import project_search_document

    store, client, _ = popup
    side = await ChatService(store).fork_session(
        "main", through_message_id="message", side_chat=True
    )
    assert side.parent_session_id == "main"
    assert [
        row["id"] for row in (await client.get("/chat/sessions/main/side-chats")).json()
    ] == [side.id]
    assert [row.id for row in (await store.list_entities(ChatSession))] == ["main"]
    assert (await store.count(ChatSession)) == 1
    assert (await store.overview("p"))["counts"]["chat_sessions"] == 1
    assert (
        project_search_document("chat_sessions", side.model_dump(mode="json")) is None
    )
    (
        await store.create(
            ChatMessage(
                engagement_id="p",
                session_id=side.id,
                sequence=2,
                role="assistant",
                content="Private side-only answer",
            )
        )
    )
    assert (await client.get("/chat/projects/p/search?q=Private%20side-only")).json()[
        "items"
    ] == []
    before = await store.get(ChatSession, side.id)
    assert (
        await client.post(f"/chat/temporary-sessions/{side.id}/keepalive")
    ).status_code == 204
    assert (await store.get(ChatSession, side.id)).revision == before.revision + 1
    assert (
        await client.delete(f"/chat/temporary-sessions/{side.id}")
    ).status_code == 204
    assert (
        await client.delete(f"/chat/temporary-sessions/{side.id}")
    ).status_code == 204
    assert (await client.get("/chat/sessions/main/side-chats")).json() == []
    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, side.id))
    assert [row.id for row in (await store.list_entities(ChatSession))] == ["main"]
    assert len((await ChatService(store).session_messages("main"))) == 1


async def test_discarding_running_side_reply_cancels_only_side_turn(popup):
    store, client, _ = popup
    side = await ChatService(store).fork_session(
        "main", through_message_id="message", side_chat=True
    )
    side_turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id=side.id,
            provider_profile_id="provider",
            model="m",
            status=ChatTurnStatus.ROUTING,
        )
    )
    parent_turn = await store.create(
        ChatTurn(
            engagement_id="p",
            session_id="main",
            provider_profile_id="provider",
            model="m",
            status=ChatTurnStatus.ROUTING,
        )
    )
    assert (
        await client.delete(f"/chat/temporary-sessions/{side.id}")
    ).status_code == 204
    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, side.id))
    with pytest.raises(NotFoundError):
        (await store.get(ChatTurn, side_turn.id))
    assert (await store.get(ChatTurn, parent_turn.id)).status == ChatTurnStatus.ROUTING


async def test_side_chat_remains_until_closed_while_popup_expires(popup):
    store, client, _ = popup
    side = await ChatService(store).fork_session(
        "main", through_message_id="message", side_chat=True
    )
    popup_chat = await store.get(ChatSession, create(client))
    old = utc_now() - timedelta(days=2)
    assert not temporary_chat_expired(side.model_copy(update={"updated_at": old}))
    assert temporary_chat_expired(popup_chat.model_copy(update={"updated_at": old}))


async def test_side_chat_discovery_is_scoped_to_its_parent(popup):
    store, client, _ = popup
    (
        await store.create(
            ChatSession(
                id="second",
                engagement_id="p",
                title="Second",
                provider_profile_id="provider",
                model="m",
            )
        )
    )
    (
        await store.create(
            ChatMessage(
                id="second-message",
                engagement_id="p",
                session_id="second",
                sequence=1,
                role="user",
                content="Second history",
            )
        )
    )
    service = ChatService(store)
    first_side = await service.fork_session(
        "main", through_message_id="message", side_chat=True
    )
    second_side = await service.fork_session(
        "second", through_message_id="second-message", side_chat=True
    )
    create(client)  # A popup is hidden but is not a side chat.
    assert [
        row["id"] for row in (await client.get("/chat/sessions/main/side-chats")).json()
    ] == [first_side.id]
    assert [
        row["id"]
        for row in (await client.get("/chat/sessions/second/side-chats")).json()
    ] == [second_side.id]
    assert (await client.get("/chat/sessions/missing/side-chats")).status_code == 404


async def test_multiple_side_chats_for_one_parent_survive_selective_close(popup):
    store, client, _ = popup
    service = ChatService(store)
    first = await service.fork_session(
        "main", through_message_id="message", side_chat=True
    )
    second = await service.fork_session(
        "main", through_message_id="message", side_chat=True
    )
    assert {
        row["id"] for row in (await client.get("/chat/sessions/main/side-chats")).json()
    } == {
        first.id,
        second.id,
    }

    assert (
        await client.delete(f"/chat/temporary-sessions/{second.id}")
    ).status_code == 204
    assert [
        row["id"] for row in (await client.get("/chat/sessions/main/side-chats")).json()
    ] == [first.id]
    assert (await store.get(ChatSession, first.id)).parent_session_id == "main"
    assert (await store.get(ChatSession, "main")).id == "main"


async def test_cannot_discard_main_or_copy_another_project(popup):
    store, client, _ = popup
    assert (await client.delete("/chat/temporary-sessions/main")).status_code == 409
    assert (
        await client.post(
            "/chat/temporary-sessions",
            json={"engagement_id": "other", "session_id": "main"},
        )
    ).status_code == 409
    assert (await store.count(ChatSession)) == 1


async def test_new_question_without_saved_history_is_hidden(popup):
    store, client, _ = popup
    response = await client.post(
        "/chat/temporary-sessions",
        json={"engagement_id": "p", "provider_id": "provider", "model": "m"},
    )
    assert response.status_code == 201, response.text
    assert (await ChatService(store).session_messages(response.json()["id"])) == []
    assert (await store.count(ChatSession)) == 1


async def test_harness_fork_uses_fresh_runtime_and_snapshot(popup):
    store, client, runtime = popup
    main_runtime = await store.create(
        HarnessSession(
            id="runtime-main",
            engagement_id="p",
            harness_profile_id="harness",
            model="m",
        )
    )
    main = await store.get(ChatSession, "main")
    (
        await store.update(
            ChatSession,
            main.id,
            {
                "backend": "harness",
                "provider_profile_id": None,
                "harness_profile_id": "harness",
                "harness_session_id": main_runtime.id,
            },
        )
    )
    fresh = await store.create(
        HarnessSession(
            id="runtime-popup",
            engagement_id="p",
            harness_profile_id="harness",
            model="m",
        )
    )
    runtime.create_session.return_value = fresh
    branch = create(client)
    runtime.create_session.assert_called_once_with(
        engagement_id="p",
        profile_id="harness",
        model="m",
        mcp_server_ids=[],
        tools_enabled=False,
    )
    saved = await store.get(ChatSession, branch)
    assert saved.harness_session_id == fresh.id
    assert saved.metadata["harness_context_handoff_pending"] is True
    assert (
        await client.delete(f"/chat/temporary-sessions/{branch}")
    ).status_code == 204
    runtime.close_session.assert_awaited_once_with(fresh.id)
    assert await store.get(HarnessSession, main_runtime.id)
    with pytest.raises(NotFoundError):
        (await store.get(HarnessSession, fresh.id))


async def test_hidden_branch_has_no_universal_search_projection(popup):
    from nebula.v3.search import project_search_document

    store, client, _ = popup
    branch = create(client)
    assert (
        project_search_document(
            "chat_sessions",
            (await store.get(ChatSession, branch)).model_dump(mode="json"),
        )
        is None
    )
    assert (
        project_search_document(
            "chat_sessions",
            (await store.get(ChatSession, "main")).model_dump(mode="json"),
        )
        is not None
    )


async def test_expired_branch_is_collected_after_restart(popup):
    from datetime import timedelta
    from nebula.v3.domain import utc_now
    import time

    store, client, _ = popup
    (
        await store.create(
            ChatSession(
                id="expired",
                engagement_id="p",
                title="Expired",
                provider_profile_id="provider",
                model="m",
                created_at=utc_now() - timedelta(days=2),
                updated_at=utc_now() - timedelta(days=2),
                metadata={"temporary_assistant": True},
            )
        )
    )
    async with client:
        for _ in range(100):
            if not any(
                row.id == "expired"
                for row in (
                    await store.list_entities(ChatSession, include_temporary=True)
                )
            ):
                break
            time.sleep(0.01)
        else:
            pytest.fail("Expired popup was not collected")
    assert await store.get(ChatSession, "main")


async def test_open_branch_renews_until_explicit_close(popup):
    from datetime import timedelta
    from nebula.v3.domain import utc_now

    store, client, _ = popup
    branch = create(client)
    before = await store.get(ChatSession, branch)
    assert (
        await client.post("/chat/temporary-sessions/main/keepalive")
    ).status_code == 409
    assert (
        await client.post(f"/chat/temporary-sessions/{branch}/keepalive")
    ).status_code == 204
    renewed = await store.get(ChatSession, branch)
    assert renewed.updated_at >= before.updated_at
    assert renewed.revision == before.revision + 1
    assert renewed.updated_at + timedelta(days=1) > utc_now()
    assert [row.id for row in (await store.list_entities(ChatSession))] == ["main"]
    assert (
        await client.delete(f"/chat/temporary-sessions/{branch}")
    ).status_code == 204
    with pytest.raises(NotFoundError):
        (await store.get(ChatSession, branch))


pytestmark = pytest.mark.anyio
