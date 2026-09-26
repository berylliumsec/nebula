"""``knowledge.search`` for provider tool turns."""

from __future__ import annotations

import asyncio

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatService, _routing_instructions
from nebula.v3.domain import (
    Approval,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    KnowledgeSource,
    ProviderProfile,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from nebula.v3.knowledge_search import (
    KNOWLEDGE_SEARCH_ROUTING_INSTRUCTIONS,
    KNOWLEDGE_SEARCH_TOOL_NAME,
    knowledge_search_spec,
)
from nebula.v3.tools import RETRIEVAL_TOOL_NAMES, ToolInvocation
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_knowledge_rerank import (
    RUNNER_QUESTION,
    FakeReranker,
    _knowledge_project,
)

DOWNTIME_QUESTION = "When are we allowed to take the system down?"


def test_the_tool_is_a_read_that_needs_no_approval_and_may_rerun():
    spec = knowledge_search_spec()

    assert spec.name == KNOWLEDGE_SEARCH_TOOL_NAME
    assert spec.risk_class == RiskClass.LOCAL_READ
    assert spec.network_access is False
    assert spec.filesystem_access == "none"
    assert spec.requires_approval is False
    assert spec.budget_class == "artifact_query"
    assert spec.input_schema["required"] == ["query"]
    assert KNOWLEDGE_SEARCH_TOOL_NAME in RETRIEVAL_TOOL_NAMES
    # Its routing guidance joins only turns that are offered it.
    assert KNOWLEDGE_SEARCH_ROUTING_INSTRUCTIONS in _routing_instructions(
        {KNOWLEDGE_SEARCH_TOOL_NAME}, None
    )
    assert KNOWLEDGE_SEARCH_ROUTING_INSTRUCTIONS not in _routing_instructions(
        {"skill.read_resource"}, None
    )


def maintenance_scorer(pairs):
    """Rejects the operator's wording; accepts the words the runbook uses."""

    scores = []
    for question, passage in pairs:
        folded = question.casefold()
        if "maintenance window" in folded and "maintenance window" in passage:
            scores.append(1.0)
        elif "harbor" in folded and "harbor" in passage:
            scores.append(3.0)
        else:
            scores.append(-9.0)
    return scores


def _tool_turns(tmp_path, monkeypatch, *, local=True, scorer=maintenance_scorer):
    store, index, _, ids = _knowledge_project(tmp_path, FakeReranker(scorer))
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
    payload = _profile(local=local, permits_sensitive_data=not local).model_dump(
        mode="python"
    )
    payload["capabilities"]["tool_calling"] = True
    payload["capability_verifications"] = {
        "model-a": {"model": "model-a", "status": "verified"}
    }
    profile = store.create(ProviderProfile.model_validate(payload))
    provider = FakeProvider(profile.id, local=local)
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(
        store, knowledge_index=index, workspace_resolver=lambda _: workspace
    )

    def prepare(content: str, **extra):
        return service.prepare(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id="eng-a",
                skill={"name": "review", "path": str(skill_path.resolve())},
                messages=[{"role": "user", "content": content}],
                stream=True,
                **{"allow_cloud_tool_results": True, **extra},
            )
        )

    return store, service, prepare, ids


def _search(store, prepared, query: str):
    invocation = ToolInvocation(
        engagement_id="eng-a",
        run_id=prepared.turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=prepared.turn.session_id,
        chat_turn_id=prepared.turn.id,
        tool_name=KNOWLEDGE_SEARCH_TOOL_NAME,
        arguments={"query": query},
        workspace=prepared.tool_components.workspace,
        requested_by="chat-assistant",
    )
    return asyncio.run(
        prepared.tool_components.broker.execute(
            invocation, prepared.tool_components.scope
        )
    ).output


def test_a_turn_whose_attachment_missed_recovers_the_document_by_searching(
    tmp_path, monkeypatch
):
    store, service, prepare, ids = _tool_turns(tmp_path, monkeypatch)

    prepared = prepare(DOWNTIME_QUESTION)

    # The planner's searches missed the runbook's wording, so nothing was
    # attached; the turn is offered the search instead.
    assert KNOWLEDGE_SEARCH_TOOL_NAME in prepared.tool_components.specs
    assert not any(c.source_id in ids.values() for c in prepared.citations)
    assert prepared.turn.request_snapshot["knowledge_search"] is True
    output = _search(store, prepared, "maintenance window")
    match = output["matches"][0]
    assert match["source_id"] == ids["harbor.md"]
    assert match["relevance"] == "strong"
    assert "Tuesday from 02:00 to 04:00 UTC" in match["text"]
    assert match["chunk_id"] and match["citation"]
    # A search is ranked, not gated: nothing answers this, and it says so.
    unrelated = _search(store, prepared, RUNNER_QUESTION)
    assert {item["relevance"] for item in unrelated["matches"]} == {"weak"}
    calls = store.list_entities(ToolCall, engagement_id="eng-a")
    assert {call.status for call in calls} == {ToolCallStatus.COMPLETE}
    assert store.list_entities(Approval, engagement_id="eng-a") == []

    # A resumed turn offers the model exactly the tools it already had.
    turn = store.update(
        ChatTurn,
        prepared.turn.id,
        {"status": ChatTurnStatus.WAITING_APPROVAL},
        expected_revision=store.get(ChatTurn, prepared.turn.id).revision,
    )
    resumed = service.prepare_resume(turn.id)
    assert KNOWLEDGE_SEARCH_TOOL_NAME in resumed.tool_components.specs


def test_the_tool_list_does_not_change_with_the_message(tmp_path, monkeypatch):
    _, _, prepare, _ = _tool_turns(tmp_path, monkeypatch)

    first = prepare("Which TCP port does the harbor service listen on?")
    second = prepare("thanks!")
    without = prepare("Which port?", include_knowledge=False)

    assert list(first.tool_components.specs) == list(second.tool_components.specs)
    assert KNOWLEDGE_SEARCH_TOOL_NAME in first.tool_components.specs
    assert KNOWLEDGE_SEARCH_TOOL_NAME not in without.tool_components.specs


def test_a_cloud_turn_needs_knowledge_consent_and_never_reads_local_only_sources(
    tmp_path, monkeypatch
):
    store, _, prepare, ids = _tool_turns(tmp_path, monkeypatch, local=False)
    source = store.get(KnowledgeSource, ids["harbor.md"])
    store.update(
        KnowledgeSource,
        source.id,
        {"metadata": {**source.metadata, "local_only": True}},
        expected_revision=source.revision,
    )

    unconfirmed = prepare("What is the access procedure?")
    confirmed = prepare("What is the access procedure?", allow_cloud_knowledge=True)

    assert KNOWLEDGE_SEARCH_TOOL_NAME not in unconfirmed.tool_components.specs
    assert KNOWLEDGE_SEARCH_TOOL_NAME in confirmed.tool_components.specs
    # The runbook is local-only: a cloud model's search never returns it.
    found = _search(store, confirmed, "maintenance window")["matches"]
    assert found and ids["harbor.md"] not in {item["source_id"] for item in found}
    assert store.get(ChatSession, confirmed.turn.session_id)
