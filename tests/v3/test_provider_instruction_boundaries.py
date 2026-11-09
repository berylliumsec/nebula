"""Saved instruction authority is independent of Core's rendered tool section."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import hashlib
import json
from datetime import timedelta

import httpx
import pytest

import nebula.v3.chat as chat_module
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.butler_oversight import ButlerEventPayload, project_stream_id
from nebula.v3.butler_recovery import ButlerRecoveryAgent
from nebula.v3.chat import ChatHistoryConflict, ChatService
from nebula.v3.chat_snapshot_parts import (
    resolve_request_snapshot,
    split_request_snapshot,
    stage_snapshot_parts,
)
from nebula.v3.context import estimate_model_request
from nebula.v3.domain import (
    ButlerRecoveryCase,
    ChatGoal,
    ChatGoalStatus,
    ChatTurn,
    ChatTurnStatus,
    utc_now,
)
from nebula.v3.provider_tool_manifest import (
    HINTS_START,
    SECTION_END,
    SECTION_START,
    ToolManifest,
    apply_tools_context,
    initialize_tools_instruction_base,
    map_tools_instruction_base,
    reconcile,
    render_hints,
    render_tools,
    reset_tools_instruction_base,
    tools_instruction_base,
)
from nebula.v3.providers import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    ProviderConfig,
    ProviderContextLengthError,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import CorruptRecordError, NebulaStore
from tests.v3.test_provider_tool_manifest import _manifest
from tests.v3.test_provider_tools_context import (
    InertProvider,
    _chat_requests,
    _history,
    _next,
    _service,
)
from tests.v3.test_tool_suggestions import MCP_TOOL, _McpPlatform, _spec


RAW_INSTRUCTIONS = [
    "  KEEP FIRST\r\n"
    + SECTION_START
    + "KEEP MIDDLE"
    + SECTION_END
    + "\r\nKEEP LAST  ",
    "KEEP FIRST" + SECTION_START + "KEEP UNMATCHED START\n",
    "KEEP FIRST" + HINTS_START + "KEEP HINTS LINE\nKEEP LAST",
    "KEEP END " + SECTION_END + "\r\n保留🙂  ",
]


@pytest.mark.parametrize("body", RAW_INSTRUCTIONS)
def test_unowned_literal_text_survives_initial_repeat_and_tool_update(body):
    first = _manifest(loaded=True)
    request = ModelRequest(messages=[], instructions=body)
    once = apply_tools_context(request, first, receipt={"suggested": ["other"]})
    assert once.instructions.startswith(body)
    assert tools_instruction_base(once) == body
    assert apply_tools_context(once, first, receipt={"suggested": ["other"]}) == once
    spec = _spec(MCP_TOOL, "Updated complete schema description.")
    changed = reconcile(first, {spec.name: spec}, native=[], deferred=[spec.name])
    updated = apply_tools_context(once, changed)
    assert updated.instructions == body + render_tools(changed)
    assert tools_instruction_base(updated) == body


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("Routing instructions.\n\n", "\nPlan instructions."),
        ("Synthesize the answer.\n\n", "\nReference instructions."),
        ("Recover the final answer.\n\n", ""),
        ("", "\n\nAutomatic continuation note."),
    ],
)
def test_quoted_exact_managed_section_is_not_owned_after_request_copies(prefix, suffix):
    manifest = _manifest(loaded=True)
    receipt = {"suggested": ["other"], "source_hints": ["tracker"]}
    section = render_tools(manifest) + render_hints(receipt, manifest)
    base = (
        "Operator quotes this valid section verbatim:" + section + "\nKEEP AFTER QUOTE"
    )
    once = apply_tools_context(
        ModelRequest(messages=[], instructions=base), manifest, receipt=receipt
    )
    copied = once.model_copy(
        update={"instructions": prefix + once.instructions + suffix}
    )
    restored = ModelRequest.model_validate_json(copied.model_dump_json())
    updated = apply_tools_context(restored, manifest, receipt=receipt)
    assert tools_instruction_base(updated) == prefix + base + suffix
    assert updated.instructions == prefix + base + suffix + section
    assert apply_tools_context(updated, manifest, receipt=receipt) == updated


def test_named_base_transforms_preserve_owned_section_and_exact_operator_quote():
    manifest = _manifest(loaded=True)
    quoted = render_tools(manifest)
    request = apply_tools_context(
        ModelRequest(messages=[], instructions=quoted + "\nOld reminder"), manifest
    )
    transformed = map_tools_instruction_base(
        request, lambda text: text.replace("Old reminder", "New reminder")
    )
    assert tools_instruction_base(transformed) == quoted + "\nNew reminder"
    assert apply_tools_context(transformed, manifest) == transformed
    rebuilt = reset_tools_instruction_base(request, "New authority" + quoted)
    assert tools_instruction_base(rebuilt) == "New authority" + quoted
    assert (
        apply_tools_context(rebuilt, manifest).instructions
        == "New authority" + quoted + quoted
    )


def test_missing_or_ambiguous_owned_original_window_is_not_textually_guessed():
    manifest = _manifest(loaded=True)
    request = apply_tools_context(
        ModelRequest(messages=[], instructions="Authority"), manifest
    )
    for changed in (
        "Changed authority" + render_tools(manifest),
        request.instructions + "\n\n" + request.instructions,
    ):
        with pytest.raises(CorruptRecordError, match="changed or became ambiguous"):
            apply_tools_context(
                request.model_copy(update={"instructions": changed}), manifest
            )


@pytest.mark.parametrize("legacy", ["pre-feature", "empty-feature", "nonempty-feature"])
def test_legacy_initialization_never_infers_ownership_from_valid_looking_text(legacy):
    manifest = (
        _manifest(loaded=True)
        if legacy == "nonempty-feature"
        else ToolManifest(session_id="chat", project_id="project", revision=1)
    )
    body = "Keep literal text" + render_tools(_manifest(loaded=True))
    request = ModelRequest(messages=[], instructions=body)
    if legacy != "pre-feature":
        request = request.model_copy(
            update={
                "metadata": {
                    "provider_tool_manifest_revision": str(manifest.revision),
                    "provider_tool_manifest_digest": manifest.digest,
                }
            }
        )
    if legacy == "nonempty-feature":
        with pytest.raises(CorruptRecordError, match="authoritative raw base"):
            initialize_tools_instruction_base(request, manifest)
    else:
        initialized = initialize_tools_instruction_base(request, manifest)
        assert tools_instruction_base(initialized) == body
        assert apply_tools_context(
            initialized, manifest
        ).instructions == body + render_tools(manifest)


async def _butler_instructions(service, root, body, project_id):
    folder = root / ".butler"
    folder.mkdir()
    (folder / "BUTLER.md").write_bytes(body.encode("utf-8"))
    loaded = read_butler_instructions(root)
    assert loaded.text == body
    event = await service.store.append_operation_event(
        project_stream_id(project_id),
        "butler_project",
        project_id,
        "butler.global_periodic_check",
        ButlerEventPayload(
            project_id=project_id,
            source="core",
            actor_role="core",
            summary="Synthetic global instruction check",
            verification_state="core_observed",
            metrics={"instructions_sha256": loaded.sha256},
        ).model_dump(mode="json"),
    )
    case = ButlerRecoveryCase(
        engagement_id=project_id,
        trigger="periodic",
        signal="butler.global_periodic_check",
        source_event_id=event.id,
        source_event_sequence=event.sequence,
        authority_revision=1,
        summary="Synthetic periodic check",
    )
    agent = ButlerRecoveryAgent(
        service.store,
        provider_factory=service.provider_factory,
        harness_runtime=None,
        artifact_store=None,
        work=None,
        memory=None,
        instructions_root=root,
    )
    return await agent._global_instructions(case)


@pytest.mark.parametrize("body", RAW_INSTRUCTIONS)
async def test_actual_global_butler_prepare_preserves_raw_authority(
    tmp_path, monkeypatch, body
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    instructions = await _butler_instructions(
        service, tmp_path, body, request.engagement_id
    )
    request._butler_recovery_instructions = instructions
    prepared = await service.prepare_async(request)
    assert body in prepared.model_request.instructions
    assert instructions in tools_instruction_base(prepared.model_request)
    await service.complete(prepared)
    sent = _chat_requests(provider)
    assert sent and all(body in item.instructions for item in sent)
    assert all(instructions in tools_instruction_base(item) for item in sent)


async def test_snapshot_parts_restart_and_lossy_compaction_keep_raw_authority(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    raw = "Exact raw mandate\r\n" + SECTION_START + "KEEP MIDDLE" + SECTION_END + "🙂  "
    request._butler_recovery_instructions = raw
    first = await service.prepare_async(request)
    await service.complete(first)
    await _history(service, first.session.id)
    compacted = await service.prepare_async(_next(request, first.session.id))
    assert compacted.context_snapshot is not None
    compactors = [
        item
        for item in provider.requests
        if item.metadata.get("operation") == "context_compaction"
    ]
    assert compactors and all(
        raw not in (item.instructions or "") for item in compactors
    )
    turn = await service.store.get(ChatTurn, compacted.turn.id)
    assert "model_request" in turn.request_snapshot["snapshot_parts"]
    saved = await resolve_request_snapshot(
        service.store, turn.request_snapshot, session_id=turn.session_id
    )
    restored = ModelRequest.model_validate_json(json.dumps(saved["model_request"]))
    assert restored.core_tools_section == compacted.model_request.core_tools_section
    assert raw in tools_instruction_base(restored)
    assert apply_tools_context(
        restored, compacted.tool_manifest
    ).instructions.startswith(raw)
    paused = await service.store.update(
        ChatTurn,
        turn.id,
        {
            "status": ChatTurnStatus.INTERRUPTED,
            "request_snapshot": {
                **turn.request_snapshot,
                "recovery": {"required": True, "unknown_tool_call_ids": []},
            },
        },
        expected_revision=turn.revision,
    )
    await service.shutdown()
    await service.store.event_history.close()
    await service.store.database.dispose()
    store = NebulaStore(service.store.database.url)
    restarted_provider = InertProvider(request.provider_id)
    restarted = ChatService(
        store,
        provider_factory=lambda _: restarted_provider,
        tool_platform=_McpPlatform(tmp_path, store),
        workspace_resolver=lambda _: tmp_path,
        tool_suggestion_client=lambda: None,
    )
    resumed = await restarted.prepare_resume(paused.id)
    assert raw in resumed.base_instructions
    assert raw in tools_instruction_base(resumed.model_request)
    await restarted.complete(resumed)
    assert all(
        raw in tools_instruction_base(item)
        for item in _chat_requests(restarted_provider)
    )


async def test_ambiguous_legacy_resume_is_refused_before_activation_without_receipt_loss(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    prepared = await service.prepare_async(request)
    turn = await service.store.get(ChatTurn, prepared.turn.id)
    saved = await resolve_request_snapshot(
        service.store, turn.request_snapshot, session_id=turn.session_id
    )
    saved["model_request"].pop("core_tools_section")
    saved.update({"recovery": {"required": True, "unknown_tool_call_ids": []}})
    compact, parts = split_request_snapshot(
        saved, engagement_id=turn.engagement_id, session_id=turn.session_id
    )
    async with service.store.transaction(immediate=True) as transaction:
        await stage_snapshot_parts(transaction, parts)
        paused = await transaction.update(
            ChatTurn,
            turn.id,
            {"status": ChatTurnStatus.INTERRUPTED, "request_snapshot": compact},
            expected_revision=turn.revision,
        )
    with pytest.raises(ChatHistoryConflict, match="authoritative raw base"):
        await service.prepare_resume(paused.id)
    assert await service.store.get(ChatTurn, paused.id) == paused
    assert not _chat_requests(provider)


async def test_actual_context_rejection_reconstructs_base_without_stale_ownership(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    raw = "Keep this exact quoted authority:" + render_tools(_manifest(loaded=True))
    request._butler_recovery_instructions = raw
    provider.script = [
        ProviderContextLengthError("Synthetic context rejected"),
        {"text": "Recovered safely."},
    ]

    async def health():
        return ProviderHealth(
            provider_id=provider.config.id,
            healthy=True,
            models=["model-a"],
            model_descriptors=[
                {
                    "id": "model-a",
                    "name": "Synthetic exact limits",
                    "context_window": 16_000,
                    "max_output_tokens": 2_048,
                }
            ],
        )

    monkeypatch.setattr(provider, "health", health)
    prepared = await service.prepare_async(request)
    result = await service.complete(prepared)
    assert result.message.content == "Recovered safely."
    requests = _chat_requests(provider)
    assert len(requests) == 2
    assert all(raw in tools_instruction_base(item) for item in requests)


async def test_actual_goal_clock_updates_proof_without_confusing_operator_quote(
    tmp_path, monkeypatch
):
    service, request, _ = await _service(tmp_path, monkeypatch)
    prepared = await service.prepare_async(request)
    goal = await service.store.create(
        ChatGoal(
            engagement_id=request.engagement_id,
            session_id=prepared.session.id,
            objective="Synthetic timed goal",
            completion_criteria=["Read exact authority"],
            status=ChatGoalStatus.RUNNING,
            time_budget_seconds=3600,
            elapsed_seconds=3400,
            active_since=utc_now() - timedelta(seconds=1),
        )
    )
    prepared.turn = prepared.turn.model_copy(update={"goal_id": goal.id})
    quoted = render_tools(prepared.tool_manifest)
    original = apply_tools_context(
        ModelRequest(
            messages=[],
            instructions=quoted + chat_module._GOAL_TIME_TEN_MINUTE_REMINDER,
        ),
        prepared.tool_manifest,
    )

    async def fit(_prepared, current):
        return current

    monkeypatch.setattr(service, "_fit_turn_goal_request", fit)
    updated = await service._with_current_goal_time_instruction(prepared, original)
    assert quoted in tools_instruction_base(updated)
    assert chat_module._GOAL_TIME_TEN_MINUTE_REMINDER not in tools_instruction_base(
        updated
    )
    assert chat_module._GOAL_TIME_FIVE_MINUTE_REMINDER in tools_instruction_base(
        updated
    )
    assert apply_tools_context(updated, prepared.tool_manifest) == updated


@pytest.mark.parametrize("kind", list(ProviderKind))
async def test_internal_provenance_survives_snapshot_but_never_changes_provider_payload(
    kind, monkeypatch
):
    manifest = _manifest(loaded=True)
    request = apply_tools_context(
        ModelRequest(
            messages=[ModelMessage(role="user", content="Synthetic request")],
            instructions="  Exact authority\r\n🙂  ",
            metadata={"synthetic": "true"},
        ),
        manifest,
    )
    proof = request.core_tools_section
    assert (
        proof.instructions_sha256
        == hashlib.sha256(request.instructions.encode("utf-8")).hexdigest()
    )
    assert len(json.dumps(proof.model_dump())) < 400
    restored = ModelRequest.model_validate_json(request.model_dump_json())
    assert restored.core_tools_section == proof
    ordinary = restored.model_copy(update={"core_tools_section": None})
    assert estimate_model_request(ordinary) == estimate_model_request(restored)
    payloads = []

    def handler(http_request):
        payloads.append(json.loads(http_request.content))
        return httpx.Response(
            200,
            json={
                "output": [],
                "model": "model-a",
                "content": [{"type": "text", "text": "Safe."}],
                "stop_reason": "end_turn",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Safe."},
                        "finish_reason": "stop",
                    }
                ],
                "candidates": [
                    {"content": {"parts": [{"text": "Safe."}]}, "finishReason": "STOP"}
                ],
            },
        )

    config = ProviderConfig(
        id="synthetic",
        kind=kind,
        base_url="https://provider.invalid/v1",
        default_model="model-a",
        api_key_value="inert-fixture-key",
    )
    classes = {
        ProviderKind.OPENAI_RESPONSES: OpenAIResponsesProvider,
        ProviderKind.OPENAI_COMPATIBLE: OpenAICompatibleProvider,
        ProviderKind.ANTHROPIC: AnthropicProvider,
        ProviderKind.GEMINI: GeminiProvider,
        ProviderKind.BEDROCK: BedrockProvider,
    }

    class RuntimeClient:
        def converse(self, **kwargs):
            payloads.append(kwargs)
            return {
                "output": {"message": {"content": [{"text": "Safe."}]}},
                "stopReason": "end_turn",
            }

    monkeypatch.setattr(
        "nebula.v3.providers.boto3.client", lambda *args, **kwargs: RuntimeClient()
    )
    provider = classes[kind](config, transport=httpx.MockTransport(handler))
    await provider.complete(restored)
    await provider.complete(ordinary)
    assert len(payloads) == 2 and payloads[0] == payloads[1]
    wire = json.dumps(payloads[0])
    assert "core_tools_section" not in wire and "instructions_sha256" not in wire
    assert proof.instructions_sha256 not in wire
