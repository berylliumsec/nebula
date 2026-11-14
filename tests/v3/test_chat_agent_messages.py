from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest

from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatService,
    ChatToolResultConsentRequired,
    _stored_model_text,
)
from nebula.v3.chat_agent_messages import (
    AgentMessageService,
    resolve_agent_messaging_choice,
)
from nebula.v3.chat_goals import ChatGoalService, GoalConversationCreate
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
    HarnessTurn,
    HarnessTurnStatus,
    ProviderProfile,
    ProviderCapabilityVerification,
    ProviderVerificationStatus,
    ScopePolicy,
    ToolCallOrigin,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.harnesses import HarnessConfigurationError
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import InvalidToolArguments, ToolInvocation, ToolNotPermitted
from tests.v3.test_chat import FakeProvider


async def _session(
    store: NebulaStore, engagement_id: str, title: str, **metadata
) -> ChatSession:
    profiles = await store.list_entities(ProviderProfile, limit=1)
    profile = (
        profiles[0]
        if profiles
        else (
            await store.create(
                ProviderProfile(
                    id="agent-message-provider",
                    name="Agent message provider",
                    provider_type="vllm",
                    is_local=True,
                    model_allowlist=["model-a"],
                    metadata={"default_model": "model-a"},
                )
            )
        )
    )
    return await store.create(
        ChatSession(
            engagement_id=engagement_id,
            title=title,
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"allow_agent_messaging": True, **metadata},
        )
    )


def _invocation(
    session: ChatSession, tmp_path, *, key: str = "call-1"
) -> ToolInvocation:
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


async def test_peer_discovery_is_opted_in_main_agent_and_project_scoped(tmp_path):
    store = NebulaStore(tmp_path / "peers.db")
    project = await store.create(Engagement(name="Project"))
    other_project = await store.create(Engagement(name="Other"))
    sender = await _session(store, project.id, "Coordinator")
    active = await _session(store, project.id, "Active peer")
    (await _session(store, project.id, "Disabled", allow_agent_messaging=False))
    (await _session(store, project.id, "Archived", archived_at="2026-09-22T10:00:00Z"))
    (await _session(store, project.id, "Temporary", temporary_assistant=True))
    (await _session(store, project.id, "Side chat", side_chat=True))
    settings = await _session(
        store, project.id, "Settings assistant", control_assistant=True
    )
    (
        await store.create(
            ChatSession(
                engagement_id=project.id,
                title="Subagent",
                provider_profile_id=sender.provider_profile_id,
                model="model-a",
                parent_session_id=sender.id,
                metadata={"allow_agent_messaging": True, "subagent_id": "child-1"},
            )
        )
    )
    (await _session(store, other_project.id, "Foreign"))
    (
        await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=active.id,
                provider_profile_id=active.provider_profile_id,
                model="model-a",
                status=ChatTurnStatus.ROUTING,
            )
        )
    )

    output = await AgentMessageService(store).list_output(_invocation(sender, tmp_path))

    assert output["agents"] == [
        {
            "session_id": active.id,
            "title": "Active peer",
            "backend": "provider",
            "model": "model-a",
            "state": "routing",
        },
        {
            "session_id": settings.id,
            "title": "Settings assistant",
            "backend": "provider",
            "model": "model-a",
            "state": "idle",
        },
    ]


async def test_new_choice_uses_project_default_and_existing_choice_is_authoritative(
    tmp_path,
):
    store = NebulaStore(tmp_path / "defaults.db")
    project = await store.create(Engagement(name="Project"))
    assert (await resolve_agent_messaging_choice(store, project.id, None, None)) is True
    assert (
        await resolve_agent_messaging_choice(store, project.id, None, False)
    ) is False
    assert (
        await resolve_agent_messaging_choice(
            store, project.id, None, None, control_assistant=True
        )
    ) is True
    disabled = await _session(store, project.id, "Old off", allow_agent_messaging=False)
    enabled = await _session(store, project.id, "Old on")
    assert (
        await resolve_agent_messaging_choice(store, project.id, disabled, None)
    ) is False
    assert (
        await resolve_agent_messaging_choice(store, project.id, enabled, None)
    ) is True
    assert (
        await resolve_agent_messaging_choice(store, project.id, enabled, False)
    ) is False
    assert (
        await resolve_agent_messaging_choice(
            store,
            project.id,
            (await _session(store, project.id, "Side", side_chat=True)),
            True,
        )
    ) is False


async def test_new_goal_conversation_uses_default_and_explicit_off(tmp_path):
    store = NebulaStore(tmp_path / "goal-default.db")
    project = await store.create(Engagement(name="Project"))
    provider = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    service = ChatGoalService(store)
    body = GoalConversationCreate(
        engagement_id=project.id,
        provider_id=provider.id,
        model="model-a",
        objective="Coordinate review",
        completion_criteria=["Reviewed"],
    )
    assert (await service.create_conversation(body)).session.metadata[
        "allow_agent_messaging"
    ] is True
    assert (
        await service.create_conversation(
            body.model_copy(update={"allow_agent_messaging": False})
        )
    ).session.metadata["allow_agent_messaging"] is False


async def test_provider_offers_peer_tools_on_first_verified_turn_and_keeps_text_chat_usable(
    tmp_path,
):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "provider-default.db")
        project = await store.create(Engagement(name="Project"))
        profile = await store.create(
            ProviderProfile(
                name="Local",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a", "model-plain"],
                capability_verifications={
                    "model-a": ProviderCapabilityVerification(
                        model="model-a", status=ProviderVerificationStatus.VERIFIED
                    )
                },
                privacy={"local_only": True},
            )
        )
        provider = FakeProvider(profile.id, local=True)
        provider.config.model_allowlist.append("model-plain")
        service = ChatService(store, provider_factory=lambda _: provider)

        def request(model: str, choice: bool | None = None) -> ChatCompletionRequest:
            return ChatCompletionRequest(
                engagement_id=project.id,
                provider_id=profile.id,
                model=model,
                messages=[{"role": "user", "content": "Coordinate"}],
                include_knowledge=False,
                allow_agent_messaging=choice,
            )

        enabled = await service.prepare_async(request("model-a"))
        assert enabled.tool_components is not None
        assert {"list_agents", "send_agent_message", "read_agent_messages"} <= {
            *enabled.tool_components.specs
        }
        await service.complete(enabled)
        assert any(
            "Peer agents:" in (sent.instructions or "") for sent in provider.requests
        )
        assert any(
            "list_agents" in {tool.name for tool in sent.tools}
            for sent in provider.requests
        )
        assert enabled.source_request.allow_agent_messaging is True
        disabled = await service.prepare_async(request("model-a", False))
        assert (
            disabled.tool_components is None
            or "list_agents" not in disabled.tool_components.specs
        )
        plain = await service.prepare_async(request("model-plain"))
        assert plain.tools_enabled is False
        assert plain.source_request.allow_agent_messaging is True
        await service.complete(plain)
        await service.shutdown()

    (await scenario())


async def test_forks_inherit_the_source_choice_and_side_chats_cannot_message(tmp_path):
    store = NebulaStore(tmp_path / "fork-choice.db")
    project = await store.create(Engagement(name="Project"))
    service = ChatService(store)
    for enabled in (False, True):
        source = await _session(
            store, project.id, f"Source {enabled}", allow_agent_messaging=enabled
        )
        message = await store.create(
            ChatMessage(
                engagement_id=project.id,
                session_id=source.id,
                sequence=1,
                role=ChatRole.USER,
                content="Review this",
            )
        )
        fork = await service.fork_session(source.id, through_message_id=message.id)
        assert fork.metadata["allow_agent_messaging"] is enabled
        side = await service.fork_session(
            source.id, through_latest=True, side_chat=True
        )
        assert (
            await resolve_agent_messaging_choice(store, project.id, side, True)
        ) is False


async def test_cloud_provider_messaging_needs_consent_and_explicit_off_recovers(
    tmp_path,
):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "provider-cloud-consent.db")
        project = await store.create(Engagement(name="Project", work_enabled=False))
        profile = await store.create(
            ProviderProfile(
                name="Cloud",
                provider_type="custom",
                endpoint="https://provider.invalid/v1",
                is_local=False,
                model_allowlist=["model-a"],
                capability_verifications={
                    "model-a": ProviderCapabilityVerification(
                        model="model-a", status=ProviderVerificationStatus.VERIFIED
                    )
                },
                privacy={"permits_sensitive_data": True},
            )
        )
        provider = FakeProvider(profile.id, local=False)
        service = ChatService(store, provider_factory=lambda _: provider)
        request = ChatCompletionRequest(
            engagement_id=project.id,
            provider_id=profile.id,
            model="model-a",
            messages=[{"role": "user", "content": "Coordinate"}],
            include_knowledge=False,
        )

        with pytest.raises(ChatToolResultConsentRequired) as refusal:
            await service.prepare_async(request)
        assert refusal.value.families == ("agent messaging",)
        assert (await store.list_entities(ChatSession)) == []
        assert (await store.list_entities(ChatTurn)) == []

        plain = await service.prepare_async(
            request.model_copy(update={"allow_agent_messaging": False})
        )
        assert plain.tools_enabled is False
        await service.complete(plain)
        consented = await service.prepare_async(
            request.model_copy(
                update={
                    "allow_agent_messaging": False,
                    "allow_cloud_tool_results": True,
                }
            )
        )
        assert consented.tool_components is not None
        assert "work_agents" in consented.tool_components.specs
        assert "list_agents" not in consented.tool_components.specs
        await service.complete(consented)
        await service.shutdown()

    (await scenario())


async def test_harness_capability_and_cloud_consent_gate_default_messaging(tmp_path):
    store = NebulaStore(tmp_path / "harness-capability.db")
    project = await store.create(Engagement(name="Project"))
    no_mcp = await store.create(
        HarnessProfile(
            name="Text harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            capabilities={"mcp": False},
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    cloud = await store.create(
        HarnessProfile(
            name="Cloud harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            privacy={"permits_sensitive_data": True},
        )
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: None,
    )
    runtime.bind_agent_messages(AgentMessageService(store))

    async def prepare(profile_id: str, choice: bool | None = None):
        return await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile_id,
            model=None,
            prompt="Continue text chat",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            allow_agent_messaging=choice,
        )

    chat, _turn, harness_turn = await prepare(no_mcp.id)
    assert chat.metadata["allow_agent_messaging"] is True
    assert harness_turn.metadata["allow_agent_messaging"] is False
    session = await store.get(HarnessSession, harness_turn.harness_session_id)
    assert "agent.send" not in {
        item["name"] for item in (await runtime._gateway_catalog(session))["tools"]
    }
    with pytest.raises(
        HarnessConfigurationError, match="explicit operator confirmation"
    ):
        (await prepare(cloud.id))
    off, _turn, _harness_turn = await prepare(cloud.id, False)
    assert off.metadata["allow_agent_messaging"] is False


async def test_send_is_durable_visible_idempotent_and_rejects_invalid_targets(tmp_path):
    store = NebulaStore(tmp_path / "send.db")
    project = await store.create(Engagement(name="Project"))
    other_project = await store.create(Engagement(name="Other"))
    sender = await _session(store, project.id, "Coordinator")
    recipient = await _session(store, project.id, "Reviewer")
    disabled = await _session(
        store, project.id, "Disabled", allow_agent_messaging=False
    )
    foreign = await _session(store, other_project.id, "Foreign")
    service = AgentMessageService(store)
    invocation = _invocation(sender, tmp_path)

    first = await service.send(invocation, recipient.id, "Check the retry path.")
    second = await service.send(invocation, recipient.id, "Check the retry path.")

    assert first["message_id"] == second["message_id"]
    messages = await store.list_entities(ChatAgentMessage, engagement_id=project.id)
    assert len(messages) == 1
    assert messages[0].status == ChatAgentMessageStatus.PENDING
    transcript = await store.list_session_entities(ChatMessage, recipient.id)
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
            (
                await service.send(
                    _invocation(sender, tmp_path, key=f"invalid-{target.id}"),
                    target.id,
                    "This must not cross the boundary.",
                )
            )

    (await store.delete_chat_session(sender.id))
    assert (await service.inbox(recipient.id, mark=False))["messages"][0][
        "sender_title"
    ] == ("Deleted conversation")
    (await store.delete_chat_session(recipient.id))
    assert (await store.list_entities(ChatAgentMessage, engagement_id=project.id)) == []


async def test_delivery_marks_only_the_exact_provider_or_harness_snapshot(tmp_path):
    store = NebulaStore(tmp_path / "delivery.db")
    project = await store.create(Engagement(name="Project"))
    sender = await _session(store, project.id, "Coordinator")
    recipient = await _session(store, project.id, "Reviewer")
    service = AgentMessageService(store)
    first = await service.send(
        _invocation(sender, tmp_path, key="first"), recipient.id, "First"
    )
    second = await service.send(
        _invocation(sender, tmp_path, key="second"), recipient.id, "Second"
    )
    records = {item.id: item for item in (await store.list_entities(ChatAgentMessage))}

    (
        await service.mark_history_delivered(
            recipient.id, {records[first["message_id"]].transcript_message_id}
        )
    )

    assert (
        await store.get(ChatAgentMessage, first["message_id"])
    ).status == ChatAgentMessageStatus.DELIVERED
    assert (
        await store.get(ChatAgentMessage, second["message_id"])
    ).status == ChatAgentMessageStatus.PENDING
    (await service.mark_message_ids_delivered([second["message_id"]]))
    assert (
        await store.get(ChatAgentMessage, second["message_id"])
    ).status == ChatAgentMessageStatus.DELIVERED


async def test_sender_must_still_be_an_opted_in_main_agent(tmp_path):
    store = NebulaStore(tmp_path / "sender.db")
    project = await store.create(Engagement(name="Project"))
    sender = await _session(
        store, project.id, "Coordinator", allow_agent_messaging=False
    )
    (await _session(store, project.id, "Reviewer"))

    # A setting refuses the call, not its arguments: the model reads
    # permission_denied and does not repeat it.
    with pytest.raises(ToolNotPermitted, match="turned off"):
        (await AgentMessageService(store).list_output(_invocation(sender, tmp_path)))


async def test_harness_catalog_send_and_next_turn_delivery(tmp_path):
    async def scenario() -> None:
        store = NebulaStore(tmp_path / "harness.db")
        project = await store.create(Engagement(name="Project"))
        profile = await store.create(
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
        peer = await _session(store, project.id, "Peer reviewer")
        chat, chat_turn, harness_turn = await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Coordinate the review",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        assert chat.metadata["allow_agent_messaging"] is True
        session = await store.get(HarnessSession, harness_turn.harness_session_id)
        names = {
            item["name"] for item in (await runtime._gateway_catalog(session))["tools"]
        }
        assert {"agent.list", "agent.send", "agent.read"} <= names

        sent = await runtime._gateway_agent_message(
            session,
            harness_turn,
            "agent.send",
            {"session_id": peer.id, "message": "Please verify the retry boundary."},
        )
        assert sent["isError"] is False
        assert (await store.list_session_entities(ChatMessage, peer.id))[0].metadata[
            "kind"
        ] == "agent_message"

        incoming = await service.send(
            _invocation(peer, tmp_path, key="peer-reply"),
            chat.id,
            "The retry boundary is idempotent.",
        )
        (
            await store.update(
                ChatTurn, chat_turn.id, {"status": ChatTurnStatus.COMPLETE}
            )
        )
        (
            await store.update(
                HarnessTurn, harness_turn.id, {"status": HarnessTurnStatus.COMPLETE}
            )
        )
        _updated, _owner, next_turn = await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model=None,
            prompt="Continue",
            chat_session_id=chat.id,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        assert next_turn.metadata["agent_messages_delivered"] == [
            incoming["message_id"]
        ]
        # In the prompt, but received only once the vendor accepts it.
        assert (
            await store.get(ChatAgentMessage, incoming["message_id"])
        ).status == ChatAgentMessageStatus.PENDING
        (await runtime._mark_prompt_context_delivered(next_turn))
        assert (
            await store.get(ChatAgentMessage, incoming["message_id"])
        ).status == ChatAgentMessageStatus.DELIVERED

    (await scenario())


pytestmark = pytest.mark.anyio


async def test_core_owned_tool_families_carry_the_projects_stored_scope(tmp_path):
    """Regression: a verified local model with only Core-owned tool families.

    Agent messaging and Work roster start the component chain when no runtime
    supplies the scope. They must use the project's stored ScopePolicy: a
    placeholder id made every such turn fail with "project scope changed".
    """
    store = NebulaStore(tmp_path / "project-scope.db")
    project = await store.create(Engagement(name="Project"))
    scope = await store.create(
        ScopePolicy(id=f"scope:{project.id}", engagement_id=project.id)
    )
    project = await store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id},
        expected_revision=project.revision,
    )
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capability_verifications={
                "model-a": ProviderCapabilityVerification(
                    model="model-a", status=ProviderVerificationStatus.VERIFIED
                )
            },
            privacy={"local_only": True},
        )
    )
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)
    request = ChatCompletionRequest(
        engagement_id=project.id,
        provider_id=profile.id,
        model="model-a",
        messages=[{"role": "user", "content": "Coordinate"}],
        include_knowledge=False,
    )
    prepared = await service.prepare_async(request)
    assert prepared.tool_components is not None
    assert prepared.tool_components.scope.id == scope.id
    assert {"list_agents", "work_agents"} <= {*prepared.tool_components.specs}
    restarted = ChatService(store, provider_factory=lambda _: provider)
    resumed = await restarted.prepare_resume(prepared.turn.id)
    assert resumed.tool_components is not None
    assert resumed.tool_components.scope.id == scope.id
    await restarted.complete(resumed)
    await service.shutdown()
    await restarted.shutdown()
