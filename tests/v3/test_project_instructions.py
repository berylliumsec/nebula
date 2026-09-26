import asyncio

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatCompletionRequest, ChatConfigurationError, ChatService
from nebula.v3.domain import ChatMessage, ChatRole, ChatSession, Engagement
from nebula.v3.project_instructions import (
    MAX_PROJECT_INSTRUCTIONS_BYTES,
    ProjectInstructionsError,
    load_project_instructions,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _profile

RULES = "Always run `make lint` before proposing a change."


def test_missing_agents_md_is_not_an_error(tmp_path):
    assert load_project_instructions(tmp_path) is None
    assert load_project_instructions(tmp_path / "absent-workspace") is None


def test_agents_md_is_read_exactly_and_hashed(tmp_path):
    (tmp_path / "AGENTS.md").write_text(RULES, encoding="utf-8")

    loaded = load_project_instructions(tmp_path)

    assert loaded is not None
    assert loaded.content == RULES
    assert loaded.size_bytes == len(RULES)
    assert loaded.truncated is False
    assert len(loaded.sha256) == 64


def test_agents_md_symlink_must_stay_inside_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "agents.md").write_text(RULES, encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to(workspace / "docs" / "agents.md")
    loaded = load_project_instructions(workspace)
    assert loaded is not None and loaded.content == RULES

    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ProjectInstructionsError, match="inside the project workspace"):
        load_project_instructions(workspace)

    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").mkdir()
    with pytest.raises(ProjectInstructionsError, match="regular file"):
        load_project_instructions(workspace)


def test_oversized_agents_md_is_truncated_on_a_character_boundary(tmp_path):
    # A two-byte character straddles the cap.
    body = "a" * (MAX_PROJECT_INSTRUCTIONS_BYTES - 1) + "é" + "tail"
    (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")

    loaded = load_project_instructions(tmp_path)

    assert loaded is not None
    assert loaded.truncated is True
    assert loaded.size_bytes == len(body.encode("utf-8"))
    assert loaded.content == "a" * (MAX_PROJECT_INSTRUCTIONS_BYTES - 1)


def _chat(tmp_path, monkeypatch, *, resolver=True):
    store = NebulaStore(tmp_path / "agents-md.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engagement = store.create(Engagement(id="eng-agents", name="AGENTS.md project"))
    profile = store.create(_profile(local=True))
    session = store.create(
        ChatSession(
            id="session-agents",
            engagement_id=engagement.id,
            title="Rules",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(
        store, workspace_resolver=(lambda _: workspace) if resolver else None
    )
    return store, workspace, engagement, profile, session, provider, service


def _request(profile, engagement, session, content="Plan the next step."):
    return ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id=engagement.id,
        session_id=session.id,
        messages=[{"role": "user", "content": content}],
        include_knowledge=False,
        stream=True,
    )


def test_every_turn_rereads_agents_md_into_the_instructions(tmp_path, monkeypatch):
    store, workspace, engagement, profile, session, _provider, service = _chat(
        tmp_path, monkeypatch
    )
    (workspace / "AGENTS.md").write_text(RULES, encoding="utf-8")

    first = service.prepare(_request(profile, engagement, session))

    assert RULES in (first.model_request.instructions or "")
    assert first.turn is not None
    receipt = first.turn.request_snapshot["project_instructions"]
    assert receipt["path"] == "AGENTS.md"
    assert receipt["truncated"] is False
    asyncio.run(service.complete(first))

    (workspace / "AGENTS.md").write_text(
        "Use the staging cluster only.", encoding="utf-8"
    )
    second = service.prepare(_request(profile, engagement, session, "And now?"))

    instructions = second.model_request.instructions or ""
    assert "Use the staging cluster only." in instructions
    assert RULES not in instructions
    assert second.turn is not None
    assert (
        second.turn.request_snapshot["project_instructions"]["sha256"]
        != receipt["sha256"]
    )


def test_agents_md_survives_compaction_verbatim(tmp_path, monkeypatch):
    store, workspace, engagement, profile, session, provider, service = _chat(
        tmp_path, monkeypatch
    )
    (workspace / "AGENTS.md").write_text(RULES, encoding="utf-8")
    store.create_many(
        [
            ChatMessage(
                id=f"message-{sequence:04d}",
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=f"message {sequence}",
            )
            for sequence in range(1, 1_003)
        ]
    )

    prepared = service.prepare(_request(profile, engagement, session))

    instructions = prepared.model_request.instructions or ""
    # The derived memory is history in the conversation; the rules stay the
    # system's own instructions.
    assert "DERIVED WORKING MEMORY" not in instructions
    assert "DERIVED WORKING MEMORY" in str(prepared.model_request.messages[0].content)
    assert RULES in instructions
    compaction_inputs = [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "context_compaction"
    ]
    assert compaction_inputs
    # AGENTS.md is never summarized: it is not part of the compacted transcript.
    assert all(
        RULES not in message.content
        for request in compaction_inputs
        for message in request.messages
    )
    with_rules = service.context_status(session.id).estimated_input_tokens
    (workspace / "AGENTS.md").unlink()
    without_rules = service.context_status(session.id).estimated_input_tokens
    assert with_rules > without_rules


def test_chat_without_a_workspace_still_runs(tmp_path, monkeypatch):
    _store, _workspace, engagement, profile, session, _provider, service = _chat(
        tmp_path, monkeypatch, resolver=False
    )

    prepared = service.prepare(_request(profile, engagement, session))

    assert prepared.turn is not None
    assert prepared.turn.request_snapshot["project_instructions"] is None


def test_unusable_agents_md_is_reported_before_the_turn(tmp_path, monkeypatch):
    _store, workspace, engagement, profile, session, _provider, service = _chat(
        tmp_path, monkeypatch
    )
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to(outside)

    with pytest.raises(ChatConfigurationError, match="AGENTS.md could not be used"):
        service.prepare(_request(profile, engagement, session))


def test_agents_md_may_link_to_a_parent_programs_agents_md(tmp_path):
    program = tmp_path / "program"
    workspace = program / "projects" / "research"
    workspace.mkdir(parents=True)
    (program / "AGENTS.md").write_text(RULES, encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to("../../AGENTS.md")

    loaded = load_project_instructions(workspace)

    assert loaded is not None and loaded.content == RULES

    # Only an AGENTS.md in an ancestor qualifies, not other files or sibling trees.
    (program / "notes.md").write_text("secret", encoding="utf-8")
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").symlink_to("../../notes.md")
    with pytest.raises(ProjectInstructionsError, match="parent folders"):
        load_project_instructions(workspace)
    sibling = tmp_path / "other"
    sibling.mkdir()
    (sibling / "AGENTS.md").write_text("elsewhere", encoding="utf-8")
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").symlink_to(sibling / "AGENTS.md")
    with pytest.raises(ProjectInstructionsError, match="parent folders"):
        load_project_instructions(workspace)
