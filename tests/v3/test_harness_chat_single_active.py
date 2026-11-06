"""A stale harness send must not fork a second turn in one conversation."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.chat import ChatHistoryConflict
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatBackend,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore


async def test_butler_harness_reuses_saved_chat_and_vendor_session_after_core_restart(
    tmp_path,
):
    store = NebulaStore(tmp_path / "butler-harness-history.db")
    engagement = await store.create(Engagement(name="Butler history"))
    profile = await store.create(
        HarnessProfile(
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    first_core = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )
    vendor = await first_core.create_session(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model="test-model",
        control_assistant=True,
    )
    vendor = await store.update(
        HarnessSession,
        vendor.id,
        {"external_session_id": "saved-codex-thread"},
        expected_revision=vendor.revision,
    )
    chat = await store.create(
        ChatSession(
            engagement_id=engagement.id,
            title="Butler",
            backend=ChatBackend.HARNESS,
            harness_profile_id=profile.id,
            harness_session_id=vendor.id,
            model=vendor.model,
            metadata={
                "control_assistant": True,
                "context_management": "runtime_managed",
            },
        )
    )
    (
        await store.create(
            ChatMessage(
                engagement_id=engagement.id,
                session_id=chat.id,
                sequence=1,
                role=ChatRole.USER,
                content="Remember the earlier project question.",
            )
        )
    )

    restarted = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )
    continued_chat, _, continued_turn = await restarted.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Continue that question.",
        chat_session_id=chat.id,
        harness_session_id=vendor.id,
        mcp_server_ids=[],
        control_assistant=True,
    )
    assert continued_chat.id == chat.id
    assert continued_chat.harness_session_id == vendor.id
    assert continued_turn.harness_session_id == vendor.id
    assert (
        await store.get(HarnessSession, vendor.id)
    ).external_session_id == "saved-codex-thread"
    assert (
        len((await store.list_entities(HarnessSession, engagement_id=engagement.id)))
        == 1
    )
    assert [
        message.content
        for message in (await store.list_session_entities(ChatMessage, chat.id))
    ] == ["Remember the earlier project question.", "Continue that question."]


async def test_busy_chat_rejects_second_harness_send_without_fork_or_message(tmp_path):
    store = NebulaStore(tmp_path / "single-active.db")
    engagement = await store.create(Engagement(name="Single active chat"))
    profile = await store.create(
        HarnessProfile(
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )
    chat, first_chat_turn, _ = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Continue the research",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    original_session_id = chat.harness_session_id
    original_revision = chat.revision

    with pytest.raises(ChatHistoryConflict, match="already has an active response"):
        (
            await runtime.prepare_chat(
                engagement_id=engagement.id,
                profile_id=profile.id,
                model=None,
                prompt="Second prompt from a stale tab",
                chat_session_id=chat.id,
                harness_session_id=original_session_id,
                mcp_server_ids=[],
            )
        )

    headers = {"Authorization": "Bearer test-token"}
    client = TestClient(
        create_app(store, auth_token="test-token", harness_runtime_service=runtime)
    )
    refused = await client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={
            "backend": "harness",
            "engagement_id": engagement.id,
            "session_id": chat.id,
            "harness_session_id": original_session_id,
            "harness_profile_id": profile.id,
            "messages": [{"role": "user", "content": "Another stale send"}],
        },
    )
    assert refused.status_code == 409, refused.text
    assert "already has an active response" in refused.json()["detail"]
    pending = await client.get(
        f"/api/v1/chat/sessions/{chat.id}/pending-turn", headers=headers
    )
    assert pending.status_code == 200
    assert pending.json()["id"] == first_chat_turn.id
    assert (
        await store.get(ChatSession, chat.id)
    ).harness_session_id == original_session_id
    assert (await store.get(ChatSession, chat.id)).revision == original_revision
    assert len((await store.list_entities(ChatTurn, engagement_id=engagement.id))) == 1
    assert (
        len((await store.list_entities(HarnessTurn, engagement_id=engagement.id))) == 1
    )
    assert (
        len((await store.list_entities(HarnessSession, engagement_id=engagement.id)))
        == 1
    )
    assert [
        message.content
        for message in (
            await store.list_entities(ChatMessage, engagement_id=engagement.id)
        )
    ] == ["Continue the research"]

    stopped = await client.post(
        f"/api/v1/chat/turns/{first_chat_turn.id}/cancel", headers=headers
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert (
        await client.get(
            f"/api/v1/chat/sessions/{chat.id}/pending-turn", headers=headers
        )
    ).json() is None
    resumed_chat, resumed_turn, _ = await runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Resume after the first turn stopped",
        chat_session_id=chat.id,
        harness_session_id=original_session_id,
        mcp_server_ids=[],
    )
    assert resumed_chat.id == chat.id
    assert resumed_chat.harness_session_id == original_session_id
    assert resumed_turn.id != first_chat_turn.id


pytestmark = pytest.mark.anyio
