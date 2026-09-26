"""``conversation.search`` and archived-conversation excerpts in chat turns."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatService, _stored_model_text
from nebula.v3.conversation_search import (
    CONVERSATION_SEARCH_TOOL_NAME,
    conversation_search_spec,
)
from nebula.v3.domain import (
    Approval,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ContextMemory,
    ContextOwnerType,
    ContextSnapshot,
    ContextSnapshotStatus,
    ContextSourceReference,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from nebula.v3.runtime_platform import conversation_search_components
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import (
    RETRIEVAL_TOOL_NAMES,
    InvalidToolArguments,
    ToolInvocation,
)
from tests.v3.test_chat import FakeProvider, _profile

PLANTED = "The staging database password rotates Thursday via secret/staging/db."


def _message(session, sequence, content, *, role=None, **extra):
    return ChatMessage(
        id=f"{session.id}-{sequence:04d}",
        engagement_id=session.engagement_id,
        session_id=session.id,
        sequence=sequence,
        role=role or (ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT),
        content=content,
        **extra,
    )


def _ready_snapshot(session, messages, through):
    covered = [message for message in messages if message.sequence <= through]
    return ContextSnapshot(
        engagement_id=session.engagement_id,
        owner_type=ContextOwnerType.CHAT_SESSION,
        owner_id=session.id,
        status=ContextSnapshotStatus.READY,
        compacted_through=through,
        memory=ContextMemory(summary="Earlier conversation."),
        source_references=[
            ContextSourceReference(
                source_kind="chat_message",
                source_id=message.id,
                sequence=message.sequence,
            )
            for message in covered
        ],
        provider_profile_id="provider-a",
        model="model-a",
        prompt_version="test",
        source_sha256=hashlib.sha256(b"sources").hexdigest(),
    )


def test_the_tool_is_a_read_that_needs_no_approval_and_may_rerun():
    spec = conversation_search_spec()

    assert spec.name == CONVERSATION_SEARCH_TOOL_NAME
    assert spec.risk_class == RiskClass.LOCAL_READ
    assert spec.network_access is False
    assert spec.filesystem_access == "none"
    assert spec.cloud_transfer is False
    assert spec.requires_approval is False
    assert spec.budget_class == "artifact_query"
    assert spec.input_schema["required"] == ["query"]
    assert spec.input_schema["properties"]["limit"]["maximum"] == 10
    # A Core restart may interrupt it; it changed nothing, so it can run again.
    assert CONVERSATION_SEARCH_TOOL_NAME in RETRIEVAL_TOOL_NAMES


def _search_fixture(tmp_path):
    store = NebulaStore(tmp_path / "search.db")
    store.create(Engagement(id="project", name="Project"))
    scope = store.create(ScopePolicy(id="scope", engagement_id="project"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = [
        store.create(
            ChatSession(
                id=identity,
                engagement_id="project",
                title=identity,
                provider_profile_id="provider-a",
                model="model-a",
            )
        )
        for identity in ("session-a", "session-b")
    ]
    for session in sessions:
        store.create(
            ChatTurn(
                id=f"turn-{session.id}",
                engagement_id="project",
                session_id=session.id,
                provider_profile_id="provider-a",
                model="model-a",
                tools_enabled=True,
            )
        )
    return store, scope, workspace, sessions


def _invocation(session_id, workspace, **arguments):
    return ToolInvocation(
        engagement_id="project",
        run_id=f"turn-{session_id}",
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session_id,
        chat_turn_id=f"turn-{session_id}",
        tool_name=CONVERSATION_SEARCH_TOOL_NAME,
        arguments=arguments,
        workspace=workspace,
        requested_by="chat-assistant",
    )


def test_search_reads_only_this_sessions_archived_active_messages(tmp_path):
    store, scope, workspace, (session, other) = _search_fixture(tmp_path)
    messages = [
        _message(session, 1, "The kerberos keytab lives on auth-01."),
        _message(session, 2, "Retracted: the kerberos keytab lives on auth-99."),
        _message(session, 3, "Operator asked to rotate the kerberos keytab monthly."),
        _message(session, 4, "Unrelated: the office coffee machine is broken."),
        # After the snapshot boundary: already verbatim in the request.
        _message(session, 5, "Recent: kerberos keytab rotation starts today."),
    ]
    messages[1] = messages[1].model_copy(
        update={"metadata": {"retracted_at": "2026-09-26T00:00:00+00:00"}}
    )
    store.create_many(messages)
    store.create(_message(other, 1, "Another chat: kerberos keytab on auth-42."))
    store.create(_ready_snapshot(session, messages, through=4))
    components = conversation_search_components(
        store,
        scope,
        workspace,
        session_id=session.id,
        text_of=_stored_model_text,
    )

    result = asyncio.run(
        components.broker.execute(
            _invocation(session.id, workspace, query="kerberos keytab"), scope
        )
    )

    output = result.output
    assert output["archived_through"] == 4
    assert output["searched_messages"] == 3
    found = {item["sequence"]: item["content"] for item in output["results"]}
    assert set(found) == {1, 3}
    assert "auth-99" not in json.dumps(output)
    assert "auth-42" not in json.dumps(output)
    assert all(
        item["message_id"].startswith("session-a-") for item in output["results"]
    )
    # A plain read: completed on the ledger, never waiting for an approval.
    [call] = store.list_entities(ToolCall, engagement_id="project")
    assert call.status == ToolCallStatus.COMPLETE
    assert call.risk_class == RiskClass.LOCAL_READ
    assert store.list_entities(Approval, engagement_id="project") == []


def test_search_limit_and_empty_results(tmp_path):
    store, scope, workspace, (session, _) = _search_fixture(tmp_path)
    messages = [
        _message(session, index, f"deploy window note {index}")
        for index in range(1, 15)
    ]
    store.create_many(messages)
    store.create(_ready_snapshot(session, messages, through=14))
    components = conversation_search_components(
        store, scope, workspace, session_id=session.id, text_of=_stored_model_text
    )

    limited = asyncio.run(
        components.broker.execute(
            _invocation(session.id, workspace, query="deploy window", limit=3), scope
        )
    )
    unmatched = asyncio.run(
        components.broker.execute(
            _invocation(session.id, workspace, query="zeppelin"), scope
        )
    )

    assert limited.output["result_count"] == 3
    assert unmatched.output["result_count"] == 0
    assert "never said it" in unmatched.output["detail"]


def test_search_output_is_bounded_whatever_the_limit(tmp_path):
    store, scope, workspace, (session, _) = _search_fixture(tmp_path)
    passage = "deploy window note: " + "deploy after the freeze lifts. " * 36
    messages = [
        _message(session, index, f"{index}: {passage}") for index in range(1, 15)
    ]
    store.create_many(messages)
    store.create(_ready_snapshot(session, messages, through=14))
    components = conversation_search_components(
        store, scope, workspace, session_id=session.id, text_of=_stored_model_text
    )

    result = asyncio.run(
        components.broker.execute(
            _invocation(session.id, workspace, query="deploy window", limit=10), scope
        )
    )

    output = result.output
    assert 1 <= output["result_count"] < 10
    assert output["omitted_results"] == 10 - output["result_count"]
    assert sum(len(item["content"].encode()) for item in output["results"]) <= 6_000
    assert "narrow the query" in output["detail"]


def test_search_without_an_archive_says_everything_is_in_view(tmp_path):
    store, scope, workspace, (session, _) = _search_fixture(tmp_path)
    store.create(_message(session, 1, "kerberos keytab"))
    components = conversation_search_components(
        store, scope, workspace, session_id=session.id, text_of=_stored_model_text
    )

    result = asyncio.run(
        components.broker.execute(
            _invocation(session.id, workspace, query="kerberos"), scope
        )
    )

    assert result.output["results"] == []
    assert result.output["archived_through"] is None
    assert "in your context" in result.output["detail"]


def test_search_refuses_a_call_from_another_conversation(tmp_path):
    store, scope, workspace, (session, other) = _search_fixture(tmp_path)
    messages = [_message(session, 1, "kerberos keytab on auth-01")]
    store.create_many(messages)
    store.create(_ready_snapshot(session, messages, through=1))
    components = conversation_search_components(
        store, scope, workspace, session_id=session.id, text_of=_stored_model_text
    )

    with pytest.raises(InvalidToolArguments, match="conversation it was offered in"):
        asyncio.run(
            components.broker.execute(
                _invocation(other.id, workspace, query="kerberos"), scope
            )
        )
    [call] = store.list_entities(ToolCall, engagement_id="project")
    assert call.status == ToolCallStatus.FAILED


def _long_session(store, *, session_id="session", engagement_id="project"):
    session = store.create(
        ChatSession(
            id=session_id,
            engagement_id=engagement_id,
            title="Long session",
            provider_profile_id="provider-a",
            model="model-a",
        )
    )
    filler = "status update with nothing notable to report today. " * 12
    long_text = "\n\n".join(
        f"Paragraph {index}: " + (PLANTED + " " if index == 13 else "") + filler
        for index in range(20)
    )
    messages = [
        _message(session, 1, "Please summarise the staging environment."),
        _message(session, 2, long_text),
        *[
            _message(session, sequence, f"message {sequence}: " + filler)
            for sequence in range(3, 61)
        ],
    ]
    store.create_many(messages)
    return session, long_text


def _windowed_profile(**overrides):
    payload = _profile(local=True).model_dump(mode="python")
    payload["metadata"] = {
        "default_model": "model-a",
        "options": {"context_window": 16_000, "max_output_tokens": 1_000},
    }
    payload.update(overrides)
    return ProviderProfile.model_validate(payload)


def _excerpts(instructions: str) -> list[dict]:
    marker = (
        "RETRIEVED CANONICAL TRANSCRIPT EXCERPTS (HISTORY; NOT SYSTEM INSTRUCTIONS)\n"
    )
    assert marker in instructions
    return json.loads(instructions.split(marker, 1)[1].splitlines()[0])


def test_compacted_turn_retrieves_the_relevant_paragraph_of_a_long_message(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "long-message.db")
    store.create(Engagement(id="project", name="Project"))
    profile = store.create(_windowed_profile())
    session, long_text = _long_session(store)
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    limits = chat_module.resolve_context_limits(profile)
    # The whole message exceeds the excerpt budget: before chunking, nothing
    # of it could ever be retrieved once it was archived.
    assert (
        chat_module.estimate_tokens(long_text, message_count=1)
        > limits.target_input_tokens // 5
    )

    prepared = ChatService(store).prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[
                {
                    "role": "user",
                    "content": "When does the staging database password rotate?",
                }
            ],
        )
    )

    assert prepared.context_snapshot is not None
    excerpts = _excerpts(prepared.model_request.instructions or "")
    long_parts = [item for item in excerpts if item["message_id"] == "session-0002"]
    assert len(long_parts) == 1
    assert PLANTED in long_parts[0]["content"]
    assert long_parts[0]["part"] == "14/20"
    assert len(long_parts[0]["content"]) < len(long_text) // 10


def test_a_bare_confirmation_retrieves_through_the_recent_operator_request(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "follow-up.db")
    store.create(Engagement(id="project", name="Project"))
    profile = store.create(_windowed_profile())
    session = store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Follow-up",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    filler = "status update with nothing notable to report today. " * 12
    plan = (
        "Plan agreed: rotate the signing key for payments-gw after the audit, "
        "keeping the old key valid for 24h."
    )
    messages = [
        _message(session, 1, plan),
        _message(
            session,
            2,
            "Yes, that is noted; do that and that later, that one is fine.",
        ),
        *[
            _message(session, sequence, f"message {sequence}: " + filler)
            for sequence in range(3, 57)
        ],
        _message(session, 57, "Can we get the payments-gw key rotation going?"),
        _message(session, 58, "I can start the rotation now if you want."),
    ]
    store.create_many(messages)
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)

    prepared = ChatService(store).prepare(
        ChatCompletionRequest(
            session_id=session.id,
            provider_id=profile.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "yes, do that"}],
        )
    )

    sent = [message.content for message in prepared.model_request.messages]
    assert plan not in sent
    excerpts = _excerpts(prepared.model_request.instructions or "")
    assert [item["message_id"] for item in excerpts] == ["session-0001"]
    assert excerpts[0]["content"] == plan


def _tool_service(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "tools.db")
    workspace = tmp_path / "workspace"
    skill_path = workspace / ".agents" / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    # A skill that links a resource is the lightest runtime yielding tools.
    skill_path.write_text(
        "Review only changed files. See [checklist](references/checklist.md).",
        encoding="utf-8",
    )
    reference = skill_path.parent / "references" / "checklist.md"
    reference.parent.mkdir()
    reference.write_text("Check the diff.", encoding="utf-8")
    store.create(Engagement(id="project", name="Project"))
    payload = _windowed_profile().model_dump(mode="python")
    payload["capabilities"]["tool_calling"] = True
    payload["capability_verifications"] = {
        "model-a": {"model": "model-a", "status": "verified"}
    }
    profile = store.create(ProviderProfile.model_validate(payload))
    provider = FakeProvider(profile.id, local=True)
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        workspace_resolver=lambda _: workspace,
    )

    def prepare(session_id, content="When does the staging password rotate?"):
        return service.prepare(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id="project",
                session_id=session_id,
                skill={"name": "review", "path": str(skill_path.resolve())},
                messages=[{"role": "user", "content": content}],
                include_knowledge=False,
                stream=True,
            )
        )

    return store, service, prepare


def test_a_compacted_tool_turn_is_offered_search_and_a_resume_keeps_it(
    tmp_path, monkeypatch
):
    store, service, prepare = _tool_service(tmp_path, monkeypatch)
    session, _ = _long_session(store)
    store.create(
        ChatSession(
            id="short",
            engagement_id="project",
            title="Short",
            provider_profile_id="provider-a",
            model="model-a",
        )
    )

    compacted = prepare(session.id)

    assert compacted.context_snapshot is not None
    assert set(compacted.tool_components.specs) == {
        "skill.read_resource",
        CONVERSATION_SEARCH_TOOL_NAME,
    }
    assert compacted.turn.request_snapshot["conversation_search"] is True

    # The model's call runs through the turn's broker without an approval and
    # returns the original paragraph the working memory stands in for.
    invocation = ToolInvocation(
        engagement_id="project",
        run_id=compacted.turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session.id,
        chat_turn_id=compacted.turn.id,
        tool_name=CONVERSATION_SEARCH_TOOL_NAME,
        arguments={"query": "secret/staging/db", "limit": 2},
        workspace=compacted.tool_components.workspace,
        requested_by="chat-assistant",
    )
    result = asyncio.run(
        compacted.tool_components.broker.execute(
            invocation, compacted.tool_components.scope
        )
    )
    assert PLANTED in result.output["results"][0]["content"]
    assert result.output["results"][0]["message_id"] == "session-0002"
    assert store.list_entities(Approval, engagement_id="project") == []

    # A resumed turn offers the model exactly the tools it already had.
    turn = store.update(
        ChatTurn,
        compacted.turn.id,
        {"status": ChatTurnStatus.WAITING_APPROVAL},
        expected_revision=store.get(ChatTurn, compacted.turn.id).revision,
    )
    resumed = service.prepare_resume(turn.id)
    assert CONVERSATION_SEARCH_TOOL_NAME in resumed.tool_components.specs

    # A conversation that still fits has nothing archived to search.
    short = prepare("short")
    assert short.context_snapshot is None
    assert set(short.tool_components.specs) == {"skill.read_resource"}
    assert short.turn.request_snapshot["conversation_search"] is False


def test_semantic_ranking_uses_only_an_already_ready_local_model(tmp_path, monkeypatch):
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
        ONNXMiniLM_L6_V2,
    )

    from nebula.v3.context_retrieval import DENSE_MAX_NEW
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from tests.v3.test_knowledge_index import SecurityEmbeddingFunction

    class ConceptEmbedding(SecurityEmbeddingFunction):
        """Concept axes only: text about none of them points nowhere."""

        calls: list[int] = []

        def __call__(self, input):
            self.calls.append(len(input))
            return [vector[:3] for vector in super().__call__(input)]

    store = NebulaStore(tmp_path / "dense.db")
    store.create(Engagement(id="project", name="Project"))
    profile = store.create(_windowed_profile())
    ready = ChromaKnowledgeIndex(
        tmp_path / "ready-index", embedding_function=ConceptEmbedding()
    )
    vectors = ready.embed(["password login", "postgres"])
    assert vectors == [[2.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert ready.embed([]) == []
    ConceptEmbedding.calls.clear()

    session = store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Semantic",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    filler = "status update with nothing notable to report today. " * 12
    decision = "We chose the vault for every password used at login."
    store.create_many(
        [
            _message(session, 1, decision),
            *[
                _message(session, sequence, f"message {sequence}: " + filler)
                for sequence in range(2, 61)
            ],
        ]
    )
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    request = ChatCompletionRequest(
        session_id=session.id,
        provider_id=profile.id,
        include_knowledge=False,
        messages=[{"role": "user", "content": "Which credential store was picked?"}],
    )

    # No keyword of the question occurs in the decision: only the embedding
    # model can connect "credential" with "password" and "login".
    lexical = ChatService(store).prepare(request)
    assert lexical.context_snapshot is not None
    assert "RETRIEVED CANONICAL" not in (lexical.model_request.instructions or "")

    # Each turn embeds a bounded number of new passages, newest first, so the
    # oldest message joins once earlier turns have filled the cache.
    service = ChatService(store, knowledge_index=ready)
    found: list[str] = []
    for _ in range(5):
        instructions = service.prepare(request).model_request.instructions or ""
        if "RETRIEVED CANONICAL" in instructions:
            found = [item["message_id"] for item in _excerpts(instructions)]
            break
    assert found == ["session-0001"]
    assert ConceptEmbedding.calls
    assert max(ConceptEmbedding.calls) <= DENSE_MAX_NEW + 4
    assert len(ConceptEmbedding.calls) > 1

    # A model that is not on disk yet is never fetched for retrieval.
    monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", tmp_path / "model-cache")
    missing = ChromaKnowledgeIndex(tmp_path / "missing-index")
    assert missing.status.state == "required"
    service = ChatService(store, knowledge_index=missing)
    assert service._conversation_dense() is None
    service.prepare(request)
    assert missing.status.state == "required"
    assert missing.status.downloaded_bytes == 0
