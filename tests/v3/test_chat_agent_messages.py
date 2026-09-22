import asyncio

import pytest

from nebula.v3.chat import _stored_model_text
from nebula.v3.chat_agent_messages import AgentMessageService
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatAgentMessage,
    ChatAgentMessageStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    ProviderProfile,
    ToolCallOrigin,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import InvalidToolArguments, ToolInvocation


def _session(store: NebulaStore, engagement_id: str, title: str, **metadata) -> ChatSession:
    profiles = store.list_entities(ProviderProfile, limit=1)
    profile = profiles[0] if profiles else store.create(
        ProviderProfile(
            id="agent-message-provider",
            name="Agent message provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"default_model": "model-a"},
        )
    )
    return store.create(
        ChatSession(
            engagement_id=engagement_id,
            title=title,
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"allow_agent_messaging": True, **metadata},
        )
    )


def _invocation(session: ChatSession, tmp_path, *, key: str = "call-1") -> ToolInvocation:
    return ToolInvocation(
        engagement_id=session.engagement_id,
        run_id="turn-1",
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session.id,
        chat_turn_id="turn-1",
        tool_name="send_agent_message",
        arguments={},
        workspace=tmp_path,
        idempotency_key=key,
    )


def test_peer_discovery_is_opted_in_main_agent_and_project_scoped(tmp_path):
    store = NebulaStore(tmp_path / "peers.db")
    project = store.create(Engagement(name="Project"))
    other_project = store.create(Engagement(name="Other"))
    sender = _session(store, project.id, "Coordinator")
    active = _session(store, project.id, "Active peer")
    _session(store, project.id, "Disabled", allow_agent_messaging=False)
    _session(store, project.id, "Archived", archived_at="2026-09-22T10:00:00Z")
    _session(store, project.id, "Temporary", temporary_assistant=True)
    store.create(
        ChatSession(
            engagement_id=project.id,
            title="Subagent",
            provider_profile_id=sender.provider_profile_id,
            model="model-a",
            parent_session_id=sender.id,
            metadata={"allow_agent_messaging": True, "subagent_id": "child-1"},
        )
    )
    _session(store, other_project.id, "Foreign")
    store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=active.id,
            provider_profile_id=active.provider_profile_id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
        )
    )

    output = AgentMessageService(store).list_output(_invocation(sender, tmp_path))

    assert output["agents"] == [
        {
            "session_id": active.id,
            "title": "Active peer",
            "backend": "provider",
            "model": "model-a",
            "state": "routing",
        }
    ]


def test_send_is_durable_visible_idempotent_and_rejects_invalid_targets(tmp_path):
    store = NebulaStore(tmp_path / "send.db")
    project = store.create(Engagement(name="Project"))
    other_project = store.create(Engagement(name="Other"))
    sender = _session(store, project.id, "Coordinator")
    recipient = _session(store, project.id, "Reviewer")
    disabled = _session(store, project.id, "Disabled", allow_agent_messaging=False)
    foreign = _session(store, other_project.id, "Foreign")
    service = AgentMessageService(store)
    invocation = _invocation(sender, tmp_path)

    first = asyncio.run(service.send(invocation, recipient.id, "Check the retry path."))
    second = asyncio.run(service.send(invocation, recipient.id, "Check the retry path."))

    assert first["message_id"] == second["message_id"]
    messages = store.list_entities(ChatAgentMessage, engagement_id=project.id)
    assert len(messages) == 1
    assert messages[0].status == ChatAgentMessageStatus.PENDING
    transcript = store.list_session_entities(ChatMessage, recipient.id)
    assert len(transcript) == 1
    assert transcript[0].role == ChatRole.SYSTEM
    assert transcript[0].content == "Check the retry path."
    assert transcript[0].metadata == {
        "kind": "agent_message",
        "agent_message_id": messages[0].id,
        "sender_session_id": sender.id,
        "sender_title": "Coordinator",
    }
    assert _stored_model_text(transcript[0]).startswith(
        f"Peer agent message from Coordinator ({sender.id}):"
    )

    for target in (sender, disabled, foreign):
        with pytest.raises(InvalidToolArguments, match="unknown peer agent"):
            asyncio.run(
                service.send(
                    _invocation(sender, tmp_path, key=f"invalid-{target.id}"),
                    target.id,
                    "This must not cross the boundary.",
                )
            )

    store.delete_chat_session(sender.id)
    assert service.inbox(recipient.id, mark=False)["messages"][0]["sender_title"] == (
        "Deleted conversation"
    )
    store.delete_chat_session(recipient.id)
    assert store.list_entities(ChatAgentMessage, engagement_id=project.id) == []


def test_delivery_marks_only_the_exact_provider_or_harness_snapshot(tmp_path):
    store = NebulaStore(tmp_path / "delivery.db")
    project = store.create(Engagement(name="Project"))
    sender = _session(store, project.id, "Coordinator")
    recipient = _session(store, project.id, "Reviewer")
    service = AgentMessageService(store)
    first = asyncio.run(
        service.send(_invocation(sender, tmp_path, key="first"), recipient.id, "First")
    )
    second = asyncio.run(
        service.send(_invocation(sender, tmp_path, key="second"), recipient.id, "Second")
    )
    records = {item.id: item for item in store.list_entities(ChatAgentMessage)}

    service.mark_history_delivered(
        recipient.id, {records[first["message_id"]].transcript_message_id}
    )

    assert store.get(ChatAgentMessage, first["message_id"]).status == ChatAgentMessageStatus.DELIVERED
    assert store.get(ChatAgentMessage, second["message_id"]).status == ChatAgentMessageStatus.PENDING
    service.mark_message_ids_delivered([second["message_id"]])
    assert store.get(ChatAgentMessage, second["message_id"]).status == ChatAgentMessageStatus.DELIVERED


def test_sender_must_still_be_an_opted_in_main_agent(tmp_path):
    store = NebulaStore(tmp_path / "sender.db")
    project = store.create(Engagement(name="Project"))
    sender = _session(store, project.id, "Coordinator", allow_agent_messaging=False)
    _session(store, project.id, "Reviewer")

    with pytest.raises(InvalidToolArguments, match="turned off"):
        AgentMessageService(store).list_output(_invocation(sender, tmp_path))


def test_harness_catalog_send_and_next_turn_delivery(tmp_path):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "harness.db")
        project = store.create(Engagement(name="Project"))
        profile = store.create(
            HarnessProfile(
                name="Harness",
                kind=HarnessKind.CODEX_APP_SERVER,
                executable="/bin/true",
                default_model="model-a",
                privacy={"local_only": True, "permits_sensitive_data": True},
            )
        )
        runtime = HarnessRuntimeService(
            store,
            credential_store=CredentialStore(),
            workspace_resolver=lambda _: tmp_path,
            adapter_factory=lambda _: None,
        )
        service = AgentMessageService(store)
        runtime.bind_agent_messages(service)
        peer = _session(store, project.id, "Peer reviewer")
        chat, _chat_turn, harness_turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Coordinate the review",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            allow_agent_messaging=True,
        )
        session = store.get(HarnessSession, harness_turn.harness_session_id)
        names = {item["name"] for item in runtime._gateway_catalog(session)["tools"]}
        assert {"agent.list", "agent.send", "agent.read"} <= names

        sent = await runtime._gateway_agent_message(
            session,
            harness_turn,
            "agent.send",
            {"session_id": peer.id, "message": "Please verify the retry boundary."},
        )
        assert sent["isError"] is False
        assert store.list_session_entities(ChatMessage, peer.id)[0].metadata["kind"] == "agent_message"

        incoming = await service.send(
            _invocation(peer, tmp_path, key="peer-reply"),
            chat.id,
            "The retry boundary is idempotent.",
        )
        _updated, _owner, next_turn = runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Continue",
            chat_session_id=chat.id,
            harness_session_id=None,
            mcp_server_ids=[],
            allow_agent_messaging=True,
        )
        assert next_turn.metadata["agent_messages_delivered"] == [incoming["message_id"]]
        assert store.get(ChatAgentMessage, incoming["message_id"]).status == ChatAgentMessageStatus.DELIVERED

    asyncio.run(scenario())
