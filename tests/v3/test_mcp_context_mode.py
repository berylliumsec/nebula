"""MCP schema presence belongs to a turn when the operator chooses on demand."""

import json
from dataclasses import replace

import pytest
import httpx

from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    chat_assistant_config,
    update_chat_assistant_config,
)
from nebula.v3.chat_schedules import ChatScheduleService
from nebula.v3.control_assistant_work import (
    CHAT_SETTINGS_FIELDS,
    START_GOAL_CONVERSATION_SCHEMA,
)
from nebula.v3.domain import ChatSession, ChatTurn, ScopePolicy
from nebula.v3.provider_tool_manifest import MANIFEST_KEY, read_manifest
from nebula.v3.tool_catalog import CATALOG_LOAD, catalog_snapshot
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_provider_tools_context import (
    MCP_TOOL,
    _call,
    _chat_requests,
    _load,
    _manifest,
    _next,
    _section,
    _service,
    _use,
)
from tests.v3.test_tool_suggestions import _jev_answers
from nebula.v3.tool_suggestions import JevClient

pytestmark = pytest.mark.anyio
SELECTED = "mcp.notes.read_note"


async def _save(service, session_id, mode):
    session = await service.store.get(ChatSession, session_id)
    return await update_chat_assistant_config(
        service.store,
        session,
        ChatAssistantConfigPatch(mcp_context_mode=mode),
        expected_revision=session.revision,
    )


@pytest.mark.parametrize("project_on_demand", [False, True])
async def test_selected_sources_start_as_brief_directory_even_if_project_flag_off(
    tmp_path,
    monkeypatch,
    project_on_demand,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    scope = await service.store.get(ScopePolicy, "scope-jev")
    await service.store.update(
        ScopePolicy,
        scope.id,
        {"on_demand_tools": project_on_demand},
        expected_revision=scope.revision,
    )
    request.mcp_context_mode = "on_demand"
    first = await service.prepare_async(request)
    assert first.turn.request_snapshot["mcp_context_mode"] == "on_demand"
    selected = next(
        item for item in first.tool_manifest.definitions if item.name == SELECTED
    )
    assert selected.representation == "deferred" and not selected.schema_present
    await service.complete(first)
    sent = _chat_requests(provider)[0]
    directory = _section(sent)
    assert directory["loaded"] == []
    entry = next(
        item for item in directory["available_not_loaded"] if item["name"] == SELECTED
    )
    assert set(entry) == {"name", "source", "purpose"}
    assert len(entry["purpose"]) <= 120
    assert "input_schema" not in json.dumps(directory)
    assert {SELECTED, MCP_TOOL}.isdisjoint(tool.name for tool in sent.tools)
    assert CATALOG_LOAD in {tool.name for tool in sent.tools}


async def test_load_is_this_turn_only_and_old_manifest_and_replay_receipts_survive(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    request.mcp_context_mode = "on_demand"
    provider.script = [
        _call(CATALOG_LOAD, {"names": [SELECTED, MCP_TOOL]}, "load"),
        _use(),
        {"text": "Used this turn."},
    ]
    first = await service.prepare_async(request)
    await service.complete(first)
    sent = _chat_requests(provider)
    assert _section(sent[0])["loaded"] == []
    assert {item["name"] for item in _section(sent[1])["loaded"]} == {
        SELECTED,
        MCP_TOOL,
    }
    loaded_result = sent[1].tool_results[0].output["loaded"]
    assert all(set(item) == {"name", "definition_digest"} for item in loaded_result)
    old = await _manifest(service, first.session.id)
    assert {SELECTED, MCP_TOOL} <= old.present_digests.keys()
    second = await service.prepare_async(_next(request, first.session.id))
    assert not {SELECTED, MCP_TOOL} & second.tool_manifest.present_digests.keys()
    assert second.tool_manifest.revision > old.revision
    await service.complete(second)
    assert _section(_chat_requests(provider)[-1])["loaded"] == []
    async with service.store.database.session() as database_session:
        retained = await read_manifest(
            database_session,
            project_id=old.project_id,
            session_id=old.session_id,
            revision=old.revision,
        )
    assert retained == old


@pytest.mark.parametrize("jev", [False, True])
async def test_ranker_preloads_become_hints_only(tmp_path, monkeypatch, jev):
    client = (
        JevClient(
            api_key="synthetic",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json=_jev_answers({MCP_TOOL: 0.8}),
                )
            ),
        )
        if jev
        else None
    )
    service, request, provider = await _service(
        tmp_path,
        monkeypatch,
        preload=not jev,
        jev=client,
    )
    request.mcp_context_mode = "on_demand"
    prepared = await service.prepare_async(request)
    receipt = catalog_snapshot(prepared.turn.request_snapshot)
    assert receipt["preloaded"] == []
    assert MCP_TOOL in receipt["suggested"]
    assert MCP_TOOL not in prepared.tool_manifest.present_digests
    if jev:
        assert prepared.turn.request_snapshot["tool_suggestions"]["preloaded"] == []
    await service.complete(prepared)
    assert _section(_chat_requests(provider)[0])["loaded"] == []


async def test_missing_saved_mode_retains_legacy_despite_new_client_default(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    await service.complete(first)
    session = await service.store.get(ChatSession, first.session.id)
    metadata = {
        key: value
        for key, value in session.metadata.items()
        if key != "mcp_context_mode"
    }
    await service.store.update(
        ChatSession,
        session.id,
        {"metadata": metadata},
        expected_revision=session.revision,
    )
    old = await service.store.get(ChatSession, session.id)
    assert (await chat_assistant_config(service.store, old))[
        "mcp_context_mode"
    ] == "always_loaded"
    request.mcp_context_mode = "on_demand"
    second = await service.prepare_async(_next(request, session.id))
    assert second.source_request.mcp_context_mode == "always_loaded"
    assert SELECTED in second.tool_manifest.present_digests
    await service.complete(second)
    with pytest.raises(ValueError, match="choose on_demand or always_loaded"):
        ChatAssistantConfigPatch(mcp_context_mode=None)


async def test_legacy_and_explicit_always_loaded_keep_selected_and_carried_schemas(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    assert request.mcp_context_mode == "always_loaded"
    provider.script = [_load(), {"text": "Loaded legacy schema."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    await _save(service, first.session.id, "always_loaded")
    second = await service.prepare_async(_next(request, first.session.id))
    assert {SELECTED, MCP_TOOL} <= second.tool_manifest.present_digests.keys()
    assert (
        next(
            item for item in second.tool_manifest.definitions if item.name == SELECTED
        ).representation
        == "native"
    )
    await service.complete(second)
    assert SELECTED in {tool.name for tool in _chat_requests(provider)[-1].tools}


@pytest.mark.parametrize(
    "initial,next_mode",
    [
        ("always_loaded", "on_demand"),
        ("on_demand", "always_loaded"),
    ],
)
async def test_inflight_change_is_saved_for_next_turn_not_current_or_resume(
    tmp_path,
    monkeypatch,
    initial,
    next_mode,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    request.mcp_context_mode = initial
    first = await service.prepare_async(request)
    original = await service.store.get(ChatTurn, first.turn.id)
    saved = await _save(service, first.session.id, next_mode)
    assert (await chat_assistant_config(service.store, saved))[
        "mcp_context_mode"
    ] == next_mode
    frozen = await service.store.get(ChatTurn, first.turn.id)
    assert frozen.request_snapshot == original.request_snapshot
    resumed = await service.prepare_resume(first.turn.id)
    assert resumed.tool_manifest == first.tool_manifest
    assert (
        resumed.turn.request_snapshot[MANIFEST_KEY]
        == original.request_snapshot[MANIFEST_KEY]
    )
    await service.complete(first)
    second = await service.prepare_async(_next(request, first.session.id))
    assert second.source_request.mcp_context_mode == next_mode
    assert (SELECTED in second.tool_manifest.present_digests) == (
        next_mode == "always_loaded"
    )
    await service.complete(second)


@pytest.mark.parametrize("mode", ["on_demand", "always_loaded"])
async def test_project_pins_stay_native_and_exclusions_stay_absent(
    tmp_path,
    monkeypatch,
    mode,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    scope = await service.store.get(ScopePolicy, "scope-jev")
    await service.store.update(
        ScopePolicy,
        scope.id,
        {"always_loaded_tools": [SELECTED, MCP_TOOL]},
        expected_revision=scope.revision,
    )
    request.mcp_context_mode = mode
    request.excluded_mcp_server_ids = ["tracker"]
    first = await service.prepare_async(request)
    assert SELECTED in first.tool_manifest.present_digests
    assert MCP_TOOL not in first.tool_components.specs
    await service.complete(first)
    assert SELECTED in {tool.name for tool in _chat_requests(provider)[0].tools}
    assert MCP_TOOL not in json.dumps(
        _chat_requests(provider)[0].model_dump(mode="json")
    )


async def test_saved_mode_follows_fork_and_automatic_settings_and_is_discoverable(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    await service.complete(first)
    await _save(service, first.session.id, "on_demand")
    settings = await ChatScheduleService(service.store).turn_settings(first.session.id)
    assert settings.mcp_context_mode == "on_demand"
    fork = await service.fork_session(first.session.id, through_latest=True)
    assert fork.metadata["mcp_context_mode"] == "on_demand"
    second = await service.prepare_async(_next(request, fork.id))
    assert second.source_request.mcp_context_mode == "on_demand"
    assert SELECTED not in second.tool_manifest.present_digests
    await service.complete(second)
    for fields in [CHAT_SETTINGS_FIELDS, START_GOAL_CONVERSATION_SCHEMA["properties"]]:
        assert fields["mcp_context_mode"]["enum"] == ["on_demand", "always_loaded"]


async def test_loaded_on_demand_schema_survives_approval_resume_and_mode_change(
    tmp_path,
    monkeypatch,
):
    from nebula.v3.chat import ChatError
    from nebula.v3.domain import Approval, ApprovalStatus, utc_now
    from nebula.v3.policy import PolicyEngine
    from nebula.v3.sandbox import AnalysisOnlyRunner
    from nebula.v3.tools import AnalysisTool, StoreToolLedger, ToolBroker, ToolRegistry
    from tests.v3.test_tool_suggestions import _McpPlatform

    class ApprovalPlatform(_McpPlatform):
        async def chat_components(self, **fields):
            components = await super().chat_components(**fields)
            registry = ToolRegistry()

            async def read(arguments):
                return {"value": arguments["value"]}

            for name, spec in components.specs.items():
                registry.register(
                    AnalysisTool(
                        spec.model_copy(update={"requires_approval": name == MCP_TOOL}),
                        read,
                    )
                )
            return replace(
                components,
                broker=ToolBroker(
                    registry=registry,
                    policy_engine=PolicyEngine(),
                    runner=AnalysisOnlyRunner(),
                    ledger=StoreToolLedger(self.store),
                    workspace_resolver=lambda _: self.workspace,
                ),
                specs={spec.name: spec for spec in registry.specs()},
            )

    service, request, provider = await _service(tmp_path, monkeypatch)
    service.tool_platform = ApprovalPlatform(tmp_path, service.store)
    request.mcp_context_mode = "on_demand"
    provider.script = [_load(), _use("approval")]
    first = await service.prepare_async(request)
    with pytest.raises(ChatError, match="waiting for operator approval"):
        await service.complete(first)
    loaded = await _manifest(service, first.session.id)
    assert MCP_TOOL in loaded.present_digests
    paused = await service.store.get(ChatTurn, first.turn.id)
    await _save(service, first.session.id, "always_loaded")
    approval = await service.store.get(Approval, paused.approval_id)
    await service.store.update(
        Approval,
        approval.id,
        {
            "status": ApprovalStatus.APPROVED,
            "decided_by": "synthetic",
            "decided_at": utc_now(),
        },
        expected_revision=approval.revision,
    )
    resumed = await service.prepare_resume(paused.id)
    assert resumed.tool_manifest == loaded
    assert resumed.turn.request_snapshot["mcp_context_mode"] == "on_demand"
    provider.script = [{"text": "Resumed exact loaded schema."}]
    await service.complete(resumed)
    assert MCP_TOOL in {
        item["name"] for item in _section(_chat_requests(provider)[-1])["loaded"]
    }
    session = await service.store.get(ChatSession, first.session.id)
    assert session.metadata["mcp_context_mode"] == "always_loaded"
