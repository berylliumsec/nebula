from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import json

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.chat_workspace import bookmark_id
from nebula.v3.domain import (
    ButlerRecoveryCase,
    ChatBookmark,
    ChatMessage,
    ChatSession,
    Engagement,
    StructuredResult,
)
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError, GuidedToolError


async def fixture(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    session = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Document review",
            provider_profile_id="inert",
            model="inert",
        )
    )
    foreign = await store.create(
        ChatSession(
            engagement_id=other.id,
            title="PRIVATE_FOREIGN_CHAT",
            provider_profile_id="inert",
            model="inert",
        )
    )
    message = await store.create(
        ChatMessage(
            engagement_id=project.id,
            session_id=session.id,
            sequence=1,
            role="user",
            content="Document review bookmark target",
        )
    )
    outside = await store.create(
        ChatMessage(
            engagement_id=other.id,
            session_id=foreign.id,
            sequence=1,
            role="user",
            content="PRIVATE_FOREIGN_MESSAGE document",
        )
    )
    app = create_app(store, auth_token="synthetic-data-token", artifact_store=artifacts)
    capabilities = ButlerCapabilities(
        store,
        artifacts,
        project.id,
        settings_api=SettingsAssistantApi(app, "synthetic-data-token"),
    )
    return store, project, session, message, outside, app, capabilities


async def invoke(capabilities, name, arguments=None, *, change=False):
    return await capabilities.invoke(
        {"operation": f"nebula.data.{name}", "arguments": arguments or {}},
        change=change,
    )


async def test_butler_saved_results_are_scoped_paged_and_read_without_rewriting(
    tmp_path,
):
    store, project, _, _, outside, _, capabilities = await fixture(tmp_path)
    payload = {"unicode": "☃" * 3000, "items": [{"id": i} for i in range(10)]}
    for i in range(26):
        (
            await store.create(
                StructuredResult(
                    engagement_id=project.id,
                    title=f"Result {i}",
                    result=payload,
                    hints={"view": "table"},
                )
            )
        )
    foreign = await store.create(
        StructuredResult(
            engagement_id=outside.engagement_id,
            title="PRIVATE_FOREIGN_RESULT",
            result={"private": True},
        )
    )

    async def scenario():
        assert (await capabilities.search({"query": "saved structured Results"}))[
            "matches"
        ]
        page = await invoke(capabilities, "results")
        assert len(page["results"]) == 20 and page["next_offset"] == 20
        last = await invoke(capabilities, "results", {"offset": 20})
        assert len(last["results"]) == 6 and last["next_offset"] is None
        assert foreign.id not in str(page) + str(last)
        selected = page["results"][0]
        detail = await invoke(capabilities, "result", {"result_id": selected["id"]})
        assert detail["revision"] == selected["revision"] and "result" not in detail
        arguments = {"result_id": detail["id"], "expected_revision": detail["revision"]}
        offset = 0
        pieces = []
        while True:
            part = await invoke(
                capabilities, "read_result", {**arguments, "offset": offset}
            )
            assert len(part["text"]) <= 4096 and part["encoding"] == "json-ascii"
            pieces.append(part["text"])
            if part["next_offset"] is None:
                break
            offset = part["next_offset"]
        assert json.loads("".join(pieces)) == payload
        assert json.loads(
            (
                await invoke(
                    capabilities, "read_result", {**arguments, "field": "hints"}
                )
            )["text"]
        ) == {"view": "table"}
        with pytest.raises(ValueError, match="current revision"):
            await invoke(
                capabilities, "read_result", {**arguments, "expected_revision": 999}
            )
        with pytest.raises(ValueError, match="selected project"):
            await invoke(capabilities, "result", {"result_id": foreign.id})
        with pytest.raises(CapabilityInputError, match="invalid arguments"):
            await invoke(
                capabilities, "read_result", {**arguments, "field": "engagement_id"}
            )
        assert (await store.get(StructuredResult, detail["id"])).result == payload

    (await scenario())


async def test_butler_bookmarks_use_core_revisions_and_read_back_saved_state(tmp_path):
    store, project, session, message, outside, _, capabilities = await fixture(tmp_path)

    async def scenario():
        search = await invoke(capabilities, "search_chats", {"query": "document"})
        assert [row["message_id"] for row in search["items"]] == [message.id]
        assert "PRIVATE_FOREIGN" not in str(search)
        assert (await invoke(capabilities, "bookmarks", {"session_id": session.id}))[
            "bookmarks"
        ] == []
        arguments = {
            "session_id": session.id,
            "message_id": message.id,
            "active": True,
            "expected_revision": 0,
        }
        receipt = await invoke(capabilities, "set_bookmark", arguments, change=True)
        assert receipt["read_back"] is True and receipt["bookmark"]["active"] is True
        assert receipt["client_action"] == {
            "kind": "bookmarks_refresh",
            "project_id": project.id,
            "session_id": session.id,
        }
        saved = await store.get(ChatBookmark, bookmark_id(session.id, message.id))
        assert saved.revision == receipt["bookmark"]["revision"]
        assert (await invoke(capabilities, "search_chats", {"bookmarked": True}))[
            "items"
        ][0]["message_id"] == message.id
        with pytest.raises(GuidedToolError):
            await invoke(
                capabilities,
                "set_bookmark",
                {**arguments, "active": False},
                change=True,
            )
        assert (await store.get(ChatBookmark, saved.id)).active is True
        with pytest.raises(ValueError, match="selected chat"):
            await invoke(
                capabilities,
                "set_bookmark",
                {**arguments, "message_id": outside.id},
                change=True,
            )
        receipt = await invoke(
            capabilities,
            "set_bookmark",
            {**arguments, "active": False, "expected_revision": saved.revision},
            change=True,
        )
        assert receipt["bookmark"]["active"] is False
        assert receipt["bookmark"]["revision"] > saved.revision
        assert (await invoke(capabilities, "search_chats", {"bookmarked": True}))[
            "items"
        ] == []

    (await scenario())


@pytest.mark.parametrize(
    "private_kind", ["control_assistant", "side_chat", "temporary_assistant"]
)
async def test_butler_conversation_navigation_refuses_private_chats(
    tmp_path, private_kind
):
    store, project, session, _, outside, _, capabilities = await fixture(tmp_path)
    private = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="PRIVATE_LOCAL_CHAT",
            provider_profile_id="inert",
            model="inert",
            metadata={private_kind: True},
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=private.id,
                sequence=1,
                role="user",
                content="PRIVATE_LOCAL_CONTENT document",
            )
        )
    )

    async def scenario():
        search = await invoke(capabilities, "search_chats", {"query": "document"})
        assert all(row["session_id"] == session.id for row in search["items"])
        assert "PRIVATE" not in str(search)
        for session_id in (private.id, outside.session_id):
            for name in ("search_chats", "bookmarks", "results"):
                with pytest.raises(ValueError, match="visible chat"):
                    await invoke(capabilities, name, {"session_id": session_id})

    (await scenario())


async def test_butler_result_removal_requires_browser_and_atomic_core_revision(
    tmp_path,
):
    store, project, _, _, outside, app, capabilities = await fixture(tmp_path)
    saved = await store.create(
        StructuredResult(
            engagement_id=project.id,
            title="Review result",
            result={"state": "retained"},
        )
    )

    async def scenario():
        receipt = await invoke(
            capabilities,
            "delete_result",
            {"result_id": saved.id, "expected_revision": saved.revision},
            change=True,
        )
        assert receipt["state"] == "pending_browser_apply"
        assert receipt["client_action"]["kind"] == "result_delete"
        assert (await store.get(StructuredResult, saved.id)).result == {
            "state": "retained"
        }
        with pytest.raises(ValueError, match="current revision"):
            await invoke(
                capabilities,
                "delete_result",
                {"result_id": saved.id, "expected_revision": 999},
                change=True,
            )

    (await scenario())
    changed = await store.update(
        StructuredResult,
        saved.id,
        {"title": "Operator changed result"},
        expected_revision=saved.revision,
    )
    client = TestClient(app, headers={"Authorization": "Bearer synthetic-data-token"})
    path = f"/api/v1/projects/{project.id}/structured-results/{saved.id}"
    assert (
        await client.delete(f"{path}?expected_revision={saved.revision}")
    ).status_code == 409
    assert (await store.get(StructuredResult, saved.id)).title == changed.title
    assert (
        await client.delete(
            f"/api/v1/projects/{outside.engagement_id}/structured-results/{saved.id}?expected_revision={changed.revision}"
        )
    ).status_code == 404
    assert (
        await client.delete(f"{path}?expected_revision={changed.revision}")
    ).status_code == 204
    assert (await client.get(path)).status_code == 404


async def test_scheduled_butler_cannot_discover_interactive_saved_data_changes(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    case = await store.create(
        ButlerRecoveryCase(
            engagement_id=project.id,
            trigger="immediate",
            signal="goal.blocked",
            source_event_id="synthetic-event",
            source_event_sequence=1,
            authority_revision=1,
            status="investigating",
            summary="Synthetic blocked goal",
        )
    )
    capabilities = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        project.id,
        background_case_id=case.id,
    )
    assert not any(name.startswith("nebula.data.") for name in capabilities._actions)


pytestmark = pytest.mark.anyio
