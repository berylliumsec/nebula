"""Butler memory survives restart without crossing project trust boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.chat import ChatCompletionRequest, ChatRequestMessage, ChatService
from nebula.v3.butler_memory import (
    ButlerMemoryService,
    MemoryConflictError,
    UnsafeMemoryError,
)
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    Engagement,
    Evidence,
    Finding,
    FindingStatus,
    HarnessKind,
    HarnessProfile,
    ChatRole,
    ProviderProfile,
    Report,
    ReportStatus,
    utc_now,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.operators import OperatorProfileService
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore


class LocalEmbedding:
    status = SimpleNamespace(state="ready")

    @staticmethod
    def embed(texts):
        vectors = []
        for text in texts:
            lowered = text.casefold()
            vectors.append(
                [
                    1.0
                    if any(word in lowered for word in ("evidence", "proof"))
                    else 0.0,
                    1.0 if "concise" in lowered else 0.0,
                    1.0 if "project" in lowered else 0.0,
                ]
            )
        return vectors


class PreparationProvider(ModelProvider):
    def __init__(self, provider_id: str):
        super().__init__(
            ProviderConfig(
                id=provider_id,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:8000/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(tools=True, strict_tools=True),
            )
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("Preparation must not call the provider")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.config.id, healthy=True, models=["model-a"]
        )


class ResponseProvider(PreparationProvider):
    def __init__(self, provider_id: str):
        super().__init__(provider_id)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model-a",
            text="I can help.",
            usage=ModelUsage(input_tokens=5, output_tokens=4, total_tokens=9),
            finish_reason="stop",
            provider_request_id="butler-memory-test",
        )


def _service(tmp_path, *, embedding=None):
    return ButlerMemoryService(
        tmp_path / "butler-memory.sqlite3",
        index_path=tmp_path / "butler-memory-index",
        embedding_index=embedding,
    )


def test_cross_project_generalized_lesson_and_preference_hide_source_and_local_lesson(
    tmp_path,
):
    memory = _service(tmp_path)
    preference = memory.capture_operator_preference(
        "project-a", "I prefer concise progress summaries.", source_ref="chat-a"
    )
    assert preference is not None
    global_lesson = memory.capture_verified_lesson(
        "project-a",
        "Check source evidence before escalating a research claim.",
        source_ref="goal-a",
        verification_state="verified",
        transferable=True,
        source_project_name="Project A",
    )
    local_lesson = memory.capture_verified_lesson(
        "project-a",
        "Project A scope review requires its saved evidence.",
        source_ref="goal-a",
        verification_state="verified",
    )
    own = {item.id: item for item in memory.list("project-a").items}
    assert own[preference.id].source_ref == "chat-a"
    assert own[global_lesson.id].source_ref == "goal-a"
    assert own[local_lesson.id].scope == "project_lesson"

    other = {item.id: item for item in memory.list("project-b").items}
    assert set(other) == {preference.id, global_lesson.id}
    assert all(item.source_ref is None for item in other.values())
    assert all(item.engagement_id is None for item in other.values())
    assert "project-a" not in str([item.model_dump() for item in other.values()])
    searched = memory.search("project-b", "evidence").items
    assert [item.id for item in searched] == [global_lesson.id]
    context = memory.turn_context("project-b", "source evidence")
    assert "I prefer concise progress summaries" in context
    assert "Check source evidence" in context
    assert "Project A scope review" not in context
    assert "goal-a" not in context


def test_restart_vector_search_and_delete_cannot_resurrect_a_memory(tmp_path):
    memory = _service(tmp_path, embedding=LocalEmbedding())
    lesson = memory.capture_verified_lesson(
        "project-a",
        "Check evidence before escalating a result.",
        source_ref="goal-a",
        verification_state="verified",
        transferable=True,
    )
    assert memory.search("project-b", "proof").items[0].id == lesson.id
    assert memory.search("project-b", "proof").index_status.state == "vector_ready"

    restarted = _service(tmp_path, embedding=LocalEmbedding())
    assert restarted.search("project-b", "proof").items[0].id == lesson.id
    restarted.delete("project-b", lesson.id, expected_revision=1)
    assert restarted.search("project-b", "proof").items == []
    assert _service(tmp_path, embedding=LocalEmbedding()).list("project-a").items == []


def test_empty_review_does_not_initialize_optional_vector_index(tmp_path, monkeypatch):
    memory = _service(tmp_path, embedding=LocalEmbedding())

    def unexpected_index_start():
        pytest.fail("Opening an empty Memory panel should not start Chroma")

    monkeypatch.setattr(memory, "_vector_collection", unexpected_index_start)
    listed = memory.list("project-a")
    assert listed.items == []
    assert listed.index_status.state == "lexical_fallback"


def test_edit_revision_is_authoritative_and_stale_delete_fails(tmp_path):
    memory = _service(tmp_path)
    lesson = memory.capture_verified_lesson(
        "project-a",
        "Verify result evidence before reporting a finding.",
        source_ref="goal-a",
        verification_state="verified",
    )
    updated = memory.update(
        "project-a",
        lesson.id,
        "Verify reproducible evidence before reporting a finding.",
        expected_revision=1,
    )
    assert updated.revision == 2
    with pytest.raises(MemoryConflictError):
        memory.update(
            "project-a", lesson.id, "Another safe lesson.", expected_revision=1
        )
    with pytest.raises(MemoryConflictError):
        memory.delete("project-a", lesson.id, expected_revision=1)
    assert memory.list("project-a").items[0].content == updated.content


def test_auto_capture_remembers_stable_status_checkpoint_preference(tmp_path):
    memory = _service(tmp_path)
    checkpoint = memory.capture_operator_preference(
        "project-a",
        "I always prefer concise status checkpoints that name the next action.",
    )
    assert checkpoint is not None
    assert checkpoint.scope == "global_preference"
    assert memory.search("project-b", "status checkpoint").items[0].id == checkpoint.id


def test_auto_capture_rejects_secrets_and_unverified_claims(tmp_path):
    memory = _service(tmp_path)
    assert memory.capture_operator_preference("project-a", "A machine is down.") is None
    assert (
        memory.capture_operator_preference(
            "project-a",
            "I prefer Project A reports in a separate folder.",
            source_project_name="Project A",
        )
        is None
    )
    assert (
        memory.capture_operator_preference(
            "project-a", "Always use API key sk-test-secret-123456789012345."
        )
        is None
    )
    assert (
        memory.capture_operator_preference(
            "project-a", "I prefer my SSN 123-45-6789 in reports."
        )
        is None
    )
    assert (
        memory.capture_operator_preference(
            "project-a", "I prefer an account number in replies."
        )
        is None
    )
    lesson = memory.capture_operator_lesson(
        "project-a",
        "Next time, verify evidence before reporting results.",
        source_ref="chat-a",
    )
    assert lesson is not None
    assert lesson.scope == "global_lesson"
    assert memory.list("project-b").items[0].content == lesson.content
    assert memory.list("project-b").items[0].source_ref is None
    assert (
        memory.capture_operator_lesson(
            "project-a", "Next time, use my SSN 123-45-6789 in reports."
        )
        is None
    )
    with pytest.raises(UnsafeMemoryError):
        memory.capture_verified_lesson(
            "project-a",
            "The target is down.",
            source_ref="goal-a",
            verification_state="agent_reported",  # type: ignore[arg-type]
        )
    with pytest.raises(UnsafeMemoryError):
        memory.capture_verified_lesson(
            "project-a",
            "Project A's specific path is safe everywhere.",
            source_ref="goal-a",
            verification_state="verified",
            transferable=True,
            source_project_name="Project A",
        )
    global_lesson = memory.capture_verified_lesson(
        "project-a",
        "Validate evidence before final report sign-off.",
        source_ref="report-a",
        verification_state="verified",
        transferable=True,
    )
    with pytest.raises(UnsafeMemoryError):
        memory.update(
            "project-b",
            global_lesson.id,
            "Validate project-a evidence before final report sign-off.",
            expected_revision=1,
        )
    memory.delete("project-a", global_lesson.id, expected_revision=1)
    assert [item.id for item in memory.list("project-a").items] == [lesson.id]


def test_memory_api_requires_auth_and_selected_project_and_supports_review_edit_delete(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.sqlite3")
    store.create(Engagement(id="project-a", name="Project A"))
    store.create(Engagement(id="project-b", name="Project B"))
    memory = _service(tmp_path)
    lesson = memory.capture_verified_lesson(
        "project-a",
        "Project A has a scoped source evidence rule.",
        source_ref="goal-a",
        verification_state="verified",
    )
    preference = memory.capture_operator_preference(
        "project-a", "I prefer concise updates.", source_ref="chat-a"
    )
    assert preference is not None
    client = TestClient(
        create_app(store, auth_token="test-token", butler_memory_service=memory)
    )
    assert (
        client.get("/api/v1/butler/memory?engagement_id=project-a").status_code == 401
    )
    headers = {"Authorization": "Bearer test-token"}
    listed = client.get(
        "/api/v1/butler/memory?engagement_id=project-b", headers=headers
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [preference.id]
    assert listed.json()["items"][0]["source_ref"] is None
    assert listed.json()["index_status"]["state"] == "lexical_fallback"
    cross_project = client.patch(
        f"/api/v1/butler/memory/{lesson.id}",
        headers=headers,
        json={
            "engagement_id": "project-b",
            "content": "Another safe rule.",
            "expected_revision": 1,
        },
    )
    assert cross_project.status_code == 404
    changed = client.patch(
        f"/api/v1/butler/memory/{preference.id}",
        headers=headers,
        json={
            "engagement_id": "project-b",
            "content": "I prefer short updates.",
            "expected_revision": 1,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    stale = client.delete(
        f"/api/v1/butler/memory/{preference.id}?engagement_id=project-b&expected_revision=1",
        headers=headers,
    )
    assert stale.status_code == 409
    removed = client.delete(
        f"/api/v1/butler/memory/{preference.id}?engagement_id=project-b&expected_revision=2",
        headers=headers,
    )
    assert removed.status_code == 204
    assert (
        client.post(
            "/api/v1/butler/memory/search",
            headers=headers,
            json={"engagement_id": "project-b", "query": "updates", "limit": 20},
        ).json()["items"]
        == []
    )


def test_signed_report_creates_authoritative_scoped_and_transferable_lessons(tmp_path):
    store = NebulaStore(tmp_path / "core.sqlite3")
    first = store.create(Engagement(id="project-a", name="Private Project A"))
    second = store.create(Engagement(id="project-b", name="Project B"))
    operator = OperatorProfileService(store).create_profile(display_name="Reviewer")
    evidence = store.create(
        Evidence(engagement_id=first.id, evidence_type="test", title="Reviewed proof")
    )
    finding = store.create(
        Finding(
            engagement_id=first.id,
            title="Sensitive finding title",
            status=FindingStatus.CONFIRMED,
            evidence_ids=[evidence.id],
            verifier_id=operator.id,
            verified_at=utc_now(),
            cwe_ids=["CWE-79"],
        )
    )
    report = store.create(
        Report(
            engagement_id=first.id,
            title="Sensitive report title",
            status=ReportStatus.REVIEW,
            finding_ids=[finding.id],
        )
    )
    memory = _service(tmp_path)
    client = TestClient(
        create_app(store, auth_token="test-token", butler_memory_service=memory)
    )
    response = client.post(
        f"/api/v1/reports/{report.id}/sign-off",
        headers={"Authorization": "Bearer test-token"},
        json={"expected_revision": report.revision, "operator_id": operator.id},
    )
    assert response.status_code == 200, response.text
    own = memory.list(first.id).items
    other = memory.list(second.id).items
    assert {item.scope for item in own} == {"project_lesson", "global_lesson"}
    assert {item.scope for item in other} == {"global_lesson"}
    assert len(other) == 2
    assert any("CWE-79" in item.content for item in other)
    assert any("Sensitive finding title" in item.content for item in own)
    assert all("Sensitive finding title" not in item.content for item in other)
    assert all(item.source_ref is None for item in other)
    assert all("Private Project A" not in item.content for item in other)
    assert all("Sensitive" not in item.content for item in other)
    assert any(
        item.source_ref == f"report:{report.id}:revision:{response.json()['revision']}"
        for item in own
    )
    memory.capture_signed_report(store, store.get(Report, report.id))
    assert len(memory.list(first.id).items) == 4


def test_memory_is_discovered_and_controlled_through_four_tool_gateway(tmp_path):
    store = NebulaStore(tmp_path / "core.sqlite3")
    first = store.create(Engagement(id="project-a", name="Project A"))
    second = store.create(Engagement(id="project-b", name="Project B"))
    memory = _service(tmp_path)
    memory.capture_verified_lesson(
        first.id,
        "This project's approved evidence requires source review.",
        source_ref="report-a",
        verification_state="verified",
    )
    capabilities = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        second.id,
        memory=memory,
        operator_message="I prefer concise progress summaries.",
        operator_turn_id="turn-b",
    )

    async def scenario():
        assert len(capabilities.plugins()) == 4
        found = await capabilities.search({"query": "memory preference"})
        assert "nebula.memory.remember_preference" in {
            item["operation"] for item in found["matches"]
        }
        listed = await capabilities.invoke(
            {"operation": "nebula.memory.list", "arguments": {}}, change=False
        )
        assert listed["items"] == []
        saved = await capabilities.invoke(
            {"operation": "nebula.memory.remember_preference", "arguments": {}},
            change=True,
        )
        assert saved["item"]["content"] == "I prefer concise progress summaries."
        assert saved["item"]["source_ref"] == "turn-b"
        forgotten = await capabilities.invoke(
            {
                "operation": "nebula.memory.forget",
                "arguments": {"id": saved["item"]["id"], "expected_revision": 1},
            },
            change=True,
        )
        assert forgotten["deleted"] is True
        assert memory.list(second.id).items == []

    asyncio.run(scenario())


def test_harness_butler_turn_reads_scoped_memory_without_foreign_source(tmp_path):
    store = NebulaStore(tmp_path / "core.sqlite3")
    first = store.create(Engagement(id="project-a", name="Project A"))
    second = store.create(Engagement(id="project-b", name="Project B"))
    memory = _service(tmp_path)
    memory.capture_operator_preference(
        first.id, "I prefer concise progress summaries.", source_ref="chat-a"
    )
    memory.capture_verified_lesson(
        first.id,
        "Check source evidence before escalating a research claim.",
        source_ref="report-a",
        verification_state="verified",
        transferable=True,
    )
    memory.capture_verified_lesson(
        first.id,
        "Project A scope review requires its saved evidence.",
        source_ref="report-a",
        verification_state="verified",
    )
    profile = store.create(
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
    runtime.bind_butler_memory(memory)
    _, _, turn = runtime.prepare_chat(
        engagement_id=second.id,
        profile_id=profile.id,
        model=None,
        prompt="What source evidence should I check?",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
        control_assistant=True,
    )
    assert "I prefer concise progress summaries" in turn.prompt
    assert "Check source evidence" in turn.prompt
    assert "Project A scope review" not in turn.prompt
    assert "report-a" not in turn.prompt


def test_provider_butler_turn_reads_scoped_memory_without_foreign_source(tmp_path):
    store = NebulaStore(tmp_path / "core.sqlite3")
    first = store.create(Engagement(id="project-a", name="Project A"))
    second = store.create(Engagement(id="project-b", name="Project B"))
    memory = _service(tmp_path)
    memory.capture_operator_preference(
        first.id, "I prefer concise progress summaries.", source_ref="chat-a"
    )
    memory.capture_verified_lesson(
        first.id,
        "Check source evidence before escalating a research claim.",
        source_ref="report-a",
        verification_state="verified",
        transferable=True,
    )
    memory.capture_verified_lesson(
        first.id,
        "Project A scope review requires its saved evidence.",
        source_ref="report-a",
        verification_state="verified",
    )
    profile = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    service = ChatService(
        store,
        provider_factory=lambda selected: PreparationProvider(selected.id),
    )
    service.butler_memory = memory
    prepared = asyncio.run(
        service.prepare_async(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id=second.id,
                model="model-a",
                control_assistant=True,
                messages=[
                    ChatRequestMessage(
                        role=ChatRole.USER,
                        content="What source evidence should I check?",
                    )
                ],
            )
        )
    )
    instructions = prepared.model_request.instructions
    assert "I prefer concise progress summaries" in instructions
    assert "Check source evidence" in instructions
    assert "Project A scope review" not in instructions
    assert "report-a" not in instructions


def test_api_butler_turn_auto_saves_operator_preference_and_reads_it_next_turn(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.sqlite3")
    project = store.create(Engagement(id="project-a", name="Project A"))
    provider_profile = store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    provider = ResponseProvider(provider_profile.id)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    memory = _service(tmp_path)
    client = TestClient(
        create_app(store, auth_token="test-token", butler_memory_service=memory)
    )
    headers = {"Authorization": "Bearer test-token"}
    first = client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={
            "engagement_id": project.id,
            "provider_id": provider_profile.id,
            "model": "model-a",
            "control_assistant": True,
            "messages": [
                {"role": "user", "content": "I prefer concise progress summaries."}
            ],
        },
    )
    assert first.status_code == 200, first.text
    saved = memory.list(project.id).items
    assert len(saved) == 1
    assert saved[0].content == "I prefer concise progress summaries."
    second = client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={
            "engagement_id": project.id,
            "provider_id": provider_profile.id,
            "session_id": first.json()["session_id"],
            "model": "model-a",
            "control_assistant": True,
            "messages": [{"role": "user", "content": "What changed today?"}],
        },
    )
    assert second.status_code == 200, second.text
    assert any(
        "What changed today?" in str(request.messages)
        and "I prefer concise progress summaries" in request.instructions
        for request in provider.requests
    )
    third = client.post(
        "/api/v1/chat/completions",
        headers=headers,
        json={
            "engagement_id": project.id,
            "provider_id": provider_profile.id,
            "session_id": first.json()["session_id"],
            "model": "model-a",
            "control_assistant": True,
            "messages": [
                {
                    "role": "user",
                    "content": "Next time, verify evidence before reporting results.",
                }
            ],
        },
    )
    assert third.status_code == 200, third.text
    another_project = store.create(Engagement(id="project-b", name="Project B"))
    cross_project = memory.list(another_project.id).items
    assert any(
        item.scope == "global_lesson" and "verify evidence" in item.content
        for item in cross_project
    )
    assert all(item.source_ref is None for item in cross_project)
