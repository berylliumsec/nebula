from __future__ import annotations

import asyncio
import json
import time
from functools import wraps
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    ChatSession,
    Evidence,
    ExecutionOrigin,
    ExecutionRuntimeSnapshot,
    Finding,
    GeneratedDraft,
    GeneratedDraftStatus,
    ModelCapabilities,
    Observation,
    OperatorExecution,
    OperatorExecutionStatus,
    ProviderPrivacy,
    ProviderProfile,
    HarnessKind,
    HarnessProfile,
    RunnerIsolation,
    RunnerRuntime,
    Engagement,
)
from nebula.v3.execution_ai import (
    DraftNoteRequest,
    DraftTransitionRequest,
    ExecutionAIError,
    ExecutionAIService,
    ExecutionChatAttachRequest,
    PostToolAssistantConfig,
    SOURCE_LIMIT,
)
from nebula.v3.providers import ModelResponse, ModelUsage, ProviderError
from nebula.v3.storage import NebulaStore


def async_test(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


class StubProvider:
    def __init__(self, *, local: bool = True) -> None:
        self.config = SimpleNamespace(local=local)
        self.requests = []
        self.response_text = ""
        self.failure: Exception | None = None

    def require(self, request):
        self.requests.append(request)
        return request.model

    async def complete(self, request):
        if not self.requests or self.requests[-1] is not request:
            self.requests.append(request)
        if self.failure:
            raise self.failure
        return ModelResponse(
            provider_id="provider-1",
            model=request.model or "model-1",
            text=self.response_text,
            usage=ModelUsage(input_tokens=12, output_tokens=8, total_tokens=20),
            provider_request_id="request-1",
        )


class StubHarnessRuntime:
    def __init__(self, response: str) -> None:
        self.response = response
        self.requests = []

    async def analyze_structured(self, **request):
        self.requests.append(request)
        return SimpleNamespace(
            id="harness-turn-1",
            response=self.response,
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


def _fixture(tmp_path, *, local: bool = True, strict: bool = True):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="AI execution notes"))
    source_bytes = (
        "password=supersecret123\nprint('start')\n" + "λ" * 20_000 + "\nprint('end')"
    ).encode()
    source = artifacts.put_bytes(
        source_bytes,
        engagement_id=engagement.id,
        filename="source.py",
        media_type="text/x-python",
    )
    store.create(source)
    evidence = store.create(
        Evidence(
            engagement_id=engagement.id,
            evidence_type="operator-execution",
            title="Execution output",
            artifact_id=source.id,
            sha256=source.sha256,
        )
    )
    execution = store.create(
        OperatorExecution(
            engagement_id=engagement.id,
            operator_id="operator",
            origin=ExecutionOrigin(kind="rerun", execution_id="prior-execution"),
            language="python",
            source_sha256=source.sha256,
            source_artifact_id=source.id,
            runtime=ExecutionRuntimeSnapshot(
                language="python",
                interpreter="/usr/bin/python3",
                arguments=["-I", "-B"],
                runtime_digest="sha256:" + "a" * 64,
                image="sha256:" + "b" * 64,
                runner_profile_id="runner-1",
                runner_profile_revision=1,
                runner_runtime=RunnerRuntime.PODMAN,
                runner_isolation=RunnerIsolation.ROOTLESS,
                runner_executable="/usr/bin/podman",
                runner_platform="linux/amd64",
            ),
            preview_fingerprint="c" * 64,
            request_fingerprint="d" * 64,
            client_idempotency_key="execution-key",
            status=OperatorExecutionStatus.COMPLETED,
            exit_code=0,
            evidence_id=evidence.id,
        )
    )
    store.append_operation_event(
        execution.id,
        "execution",
        engagement.id,
        "execution.stdout",
        {"stream": "stdout", "text": "api_key=anothersecret123\nhello\n"},
    )
    store.append_operation_event(
        execution.id,
        "execution",
        engagement.id,
        "execution.stderr",
        {"stream": "stderr", "text": "warning\x1b[31m\n"},
    )
    profile = store.create(
        ProviderProfile(
            id="provider-1",
            name="Provider",
            provider_type="vllm" if local else "openai",
            is_local=local,
            model_allowlist=["model-1"],
            capabilities=ModelCapabilities(strict_structured_output=strict),
            privacy=ProviderPrivacy(permits_sensitive_data=not local),
        )
    )
    provider = StubProvider(local=local)
    provider.response_text = json.dumps(
        {
            "title": "Execution note",
            "summary": "The script completed.",
            "observations": ["stdout contained hello"],
            "potential_findings": [
                {"title": "Possible issue", "rationale": "Needs verification"}
            ],
            "evidence_ids": [evidence.id],
        }
    )
    service = ExecutionAIService(
        store=store,
        artifact_store=artifacts,
        provider_factory=lambda selected: provider,
        operator_id=lambda: "operator",
    )
    return store, artifacts, engagement, execution, profile, evidence, provider, service


@async_test
async def test_post_tool_config_generates_suggestion_and_automatic_note(tmp_path):
    store, _artifacts, engagement, execution, profile, evidence, provider, service = _fixture(tmp_path)
    config = service.set_config(engagement.id, PostToolAssistantConfig(
        suggest_next_steps=True, take_notes=True, provider_id=profile.id, model="model-1"
    ))
    assert service.get_config(engagement.id) == config
    provider.response_text = json.dumps({
        "title": "AI-generated execution note",
        "summary": "The command completed.",
        "observations": ["stdout contained hello"],
        "potential_findings": [],
        "evidence_ids": [evidence.id],
        "next_step": {
            "title": "Inspect the service",
            "rationale": "Confirm the observed response.",
            "command": "curl -I https://example.test",
            "language": "bash",
            "network_target": "example.test",
            "network_ports": [443],
        },
    })
    draft = await service.generate(execution.id, DraftNoteRequest(
        provider_id=profile.id, model="model-1", suggest_next_steps=True,
        take_notes=True, automatic=True,
    ))
    await service._tasks[draft.id]
    result = store.get(GeneratedDraft, draft.id)
    assert result.status == GeneratedDraftStatus.ACCEPTED
    assert result.content and result.content.next_step
    assert result.content.next_step.command.startswith("curl")
    note = store.get(Observation, result.observation_id)
    assert note.observation_type == "ai_tool_note"
    assert note.metadata["provenance"] == "ai-generated"
    dismissed = service.dismiss_suggestion(result.id)
    assert dismissed.metadata["dismissed"] is True


def test_post_tool_config_requires_runtime_before_enablement(tmp_path):
    _store, _artifacts, engagement, _execution, _profile, _evidence, _provider, service = _fixture(tmp_path)
    with pytest.raises(ExecutionAIError) as refusal:
        service.set_config(engagement.id, PostToolAssistantConfig(suggest_next_steps=True))
    assert refusal.value.code == "configuration_invalid"
    assert service.get_config(engagement.id).suggest_next_steps is False


@async_test
async def test_post_tool_assistant_supports_tool_disabled_harness_analysis(tmp_path):
    store, artifacts, engagement, execution, _profile, evidence, _provider, _service = _fixture(tmp_path)
    harness = store.create(HarnessProfile(
        name="Local Codex",
        kind=HarnessKind.CODEX_APP_SERVER,
        executable="/usr/bin/codex",
        default_model="model-1",
        privacy=ProviderPrivacy(local_only=True),
    ))
    runtime = StubHarnessRuntime(json.dumps({
        "title": "Harness note",
        "summary": "Execution completed.",
        "observations": ["stdout contained hello"],
        "potential_findings": [],
        "evidence_ids": [evidence.id],
        "next_step": {
            "title": "Inspect output",
            "rationale": "Confirm the result.",
            "command": "printf '%s\\n' hello",
            "language": "bash",
            "network_ports": [],
        },
    }))
    service = ExecutionAIService(
        store=store,
        artifact_store=artifacts,
        harness_runtime=runtime,  # type: ignore[arg-type]
    )
    config = service.set_config(engagement.id, PostToolAssistantConfig(
        suggest_next_steps=True,
        backend_kind="harness",
        harness_profile_id=harness.id,
        model="model-1",
    ))
    assert config.backend_kind == "harness"
    draft = await service.generate(execution.id, DraftNoteRequest(
        backend_kind="harness",
        harness_profile_id=harness.id,
        model="model-1",
        suggest_next_steps=True,
        take_notes=False,
        automatic=True,
    ))
    await service._tasks[draft.id]
    result = store.get(GeneratedDraft, draft.id)
    assert result.status == GeneratedDraftStatus.READY
    assert result.content and result.content.next_step.command.startswith("printf")
    assert result.metadata["backend_kind"] == "harness"
    assert runtime.requests[0]["profile_id"] == harness.id
    assert "normal project container tools are available" in runtime.requests[0]["prompt"]
    assert "hello" in runtime.requests[0]["files"]["stdout.txt"]
    assert "anothersecret123" not in runtime.requests[0]["files"]["stdout.txt"]


@async_test
async def test_draft_context_is_bounded_redacted_deduplicated_and_accepts_once(
    tmp_path,
):
    store, _artifacts, _engagement, execution, profile, evidence, provider, service = (
        _fixture(tmp_path)
    )
    context, fingerprint, metadata = service._context(execution)
    decoded = json.loads(context)
    assert len(decoded["source_excerpt"].encode()) <= SOURCE_LIMIT
    assert "supersecret123" not in context
    assert "anothersecret123" not in context
    assert "[REDACTED]" in context
    assert "<0x1B>" in context
    assert metadata["categories"]

    draft = await service.generate(
        execution.id,
        DraftNoteRequest(provider_id=profile.id, model="model-1"),
    )
    await service._tasks[draft.id]
    ready = store.get(GeneratedDraft, draft.id)
    assert ready.status == GeneratedDraftStatus.READY
    assert ready.context_fingerprint == fingerprint
    assert ready.usage and ready.usage.total_tokens == 20
    assert provider.requests[-1].response_schema is not None
    assert provider.requests[-1].tools == []

    duplicate = await service.generate(
        execution.id,
        DraftNoteRequest(provider_id=profile.id, model="model-1"),
    )
    assert duplicate.id == ready.id
    accepted = service.accept(
        ready.id, DraftTransitionRequest(expected_revision=ready.revision)
    )
    retried = service.accept(
        accepted.id, DraftTransitionRequest(expected_revision=accepted.revision)
    )
    assert retried.observation_id == accepted.observation_id
    observations = store.list_entities(
        Observation, engagement_id=execution.engagement_id
    )
    assert len(observations) == 1
    assert observations[0].observation_type == "ai_execution_note"
    assert observations[0].evidence_ids == [evidence.id]
    assert (
        observations[0].metadata["potential_findings"][0]["title"] == "Possible issue"
    )
    assert store.count(Finding, engagement_id=execution.engagement_id) == 0


@async_test
async def test_provider_failure_is_retryable_and_strict_prose_never_falls_back(
    tmp_path,
):
    store, _artifacts, _engagement, execution, profile, _evidence, provider, service = (
        _fixture(tmp_path)
    )
    provider.failure = ProviderError("temporary provider outage")
    draft = await service.generate(
        execution.id,
        DraftNoteRequest(provider_id=profile.id, model="model-1"),
    )
    await service._tasks[draft.id]
    failed = store.get(GeneratedDraft, draft.id)
    assert failed.status == GeneratedDraftStatus.FAILED
    assert "temporary provider outage" in (failed.error_detail or "")

    provider.failure = None
    provider.response_text = "Here is a useful note, but not strict JSON."
    retry = await service.generate(
        execution.id,
        DraftNoteRequest(provider_id=profile.id, model="model-1"),
    )
    assert retry.id == failed.id
    await service._tasks[retry.id]
    strict_failure = store.get(GeneratedDraft, retry.id)
    assert strict_failure.status == GeneratedDraftStatus.FAILED
    assert (
        strict_failure.error_detail
        == "provider did not return the required strict draft schema"
    )


@async_test
async def test_cloud_transfer_requires_confirmation_and_chat_attachment_stays_inert(
    tmp_path,
):
    store, _artifacts, engagement, execution, profile, _evidence, _provider, service = (
        _fixture(tmp_path, local=False)
    )
    with pytest.raises(ExecutionAIError) as refusal:
        await service.generate(
            execution.id,
            DraftNoteRequest(provider_id=profile.id, model="model-1"),
        )
    assert refusal.value.code == "cloud_confirmation_required"

    attachment = service.attach_to_chat(
        execution.id,
        ExecutionChatAttachRequest(
            provider_id=profile.id,
            model="model-1",
            cloud_confirmed=True,
        ),
    )
    assert attachment.session.engagement_id == engagement.id
    assert attachment.context_message.role == "user"
    assert "JSON DATA ONLY" in attachment.context_message.content
    assert "supersecret123" not in attachment.context_message.content
    assert store.count(ChatSession, engagement_id=engagement.id) == 1


def test_execution_ai_api_is_closed_and_protected(tmp_path):
    store, artifacts, _engagement, execution, profile, _evidence, _provider, service = (
        _fixture(tmp_path)
    )
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(
        create_app(
            store,
            artifact_store=artifacts,
            auth_token="test-token",
            execution_ai_service=service,
        )
    ) as client:
        assert (
            client.post(
                "/api/v1/generated-drafts", headers=headers, json={}
            ).status_code
            == 405
        )
        invalid = client.post(
            f"/api/v1/executions/{execution.id}/draft-notes",
            headers=headers,
            json={
                "provider_id": profile.id,
                "model": "model-1",
                "cloud_confirmed": False,
                "unexpected": True,
            },
        )
        assert invalid.status_code == 422
        queued = client.post(
            f"/api/v1/executions/{execution.id}/draft-notes",
            headers=headers,
            json={"provider_id": profile.id, "model": "model-1"},
        )
        assert queued.status_code == 202
        draft_id = queued.json()["id"]
        for _ in range(100):
            detail = client.get(f"/api/v1/generated-drafts/{draft_id}", headers=headers)
            if detail.json()["status"] != "generating":
                break
            time.sleep(0.01)
        assert detail.json()["status"] == "ready"
        delete = client.delete(f"/api/v1/generated-drafts/{draft_id}", headers=headers)
        assert delete.status_code == 405
