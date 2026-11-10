"""Provider server exclusions are next-turn context choices, not revocations."""

import json

import pytest

from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    chat_assistant_config,
    update_chat_assistant_config,
)
from nebula.v3.chat_schedules import ChatScheduleService
from nebula.v3.chat_goals import ChatGoalService, GoalConversationCreate
from nebula.v3.domain import ChatSession, ChatTurn, McpServerProfile
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_provider_tools_context import (
    MCP_TOOL,
    _load,
    _manifest,
    _next,
    _service,
)

pytestmark = pytest.mark.anyio


async def test_goal_conversation_draft_preserves_context_mode_and_exclusions(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    body = GoalConversationCreate(
        engagement_id=request.engagement_id,
        provider_id=request.provider_id,
        model="model-a",
        objective="Preserve the operator's next-turn context choices.",
        completion_criteria=["The next goal retains exclusions and the loading mode."],
        mcp_server_ids=["notes"],
        excluded_mcp_server_ids=["tracker"],
        mcp_context_mode="on_demand",
    )
    session, _ = await ChatGoalService(service.store)._provider_conversation_entities(
        body
    )
    assert session.metadata["excluded_mcp_server_ids"] == ["tracker"]
    assert session.metadata["mcp_context_mode"] == "on_demand"


async def _save(service, session_id, **settings):
    session = await service.store.get(ChatSession, session_id)
    return await update_chat_assistant_config(
        service.store,
        session,
        ChatAssistantConfigPatch(**settings),
        expected_revision=session.revision,
    )


def _wire(prepared):
    return json.dumps(prepared.model_request.model_dump(mode="json"))


async def test_deselection_removes_all_server_context_and_recheck_restores(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    await service.complete(first)
    old_manifest = await _manifest(service, first.session.id)
    native_name = "mcp.notes.read_note"
    assert native_name in old_manifest.present_digests
    before = max(
        sum(
            len(json.dumps(tool.model_dump()))
            for tool in sent.tools
            if tool.name.startswith("mcp.notes.")
        )
        for sent in provider.requests
    )
    assert before > 0
    await _save(service, first.session.id, mcp_server_ids=[])
    # Even a stale composer cannot reinsert the deselected server.
    next_turn = await service.prepare_async(_next(request, first.session.id))
    assert next_turn.source_request.mcp_server_ids == []
    assert next_turn.source_request.excluded_mcp_server_ids == ["notes"]
    assert native_name not in next_turn.tool_components.specs
    assert native_name not in _wire(next_turn)
    assert native_name not in {
        item.name for item in next_turn.tool_manifest.definitions
    }
    assert next_turn.turn.request_snapshot["mcp_snapshot"] == []
    assert all(
        item["id"] != "notes"
        for item in next_turn.turn.request_snapshot["mcp_catalog_snapshot"]
    )
    assert (
        sum(
            len(json.dumps(tool.model_dump()))
            for tool in next_turn.model_request.tools
            if tool.name.startswith("mcp.notes.")
        )
        == 0
    )
    request_boundary = len(provider.requests)
    await service.complete(next_turn)
    assert all(
        tool.name != native_name
        for sent in provider.requests[request_boundary:]
        for tool in sent.tools
    )
    assert native_name in old_manifest.present_digests
    await _save(service, first.session.id, mcp_server_ids=["notes"])
    restored = await service.prepare_async(_next(request, first.session.id))
    assert restored.source_request.excluded_mcp_server_ids == []
    assert native_name in restored.tool_manifest.present_digests
    await service.complete(restored)


async def test_explicit_on_demand_exclusion_drops_loaded_schema_and_ranker_input(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    provider.script = [_load(), {"text": "Known."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    loaded = await _manifest(service, first.session.id)
    assert MCP_TOOL in loaded.present_digests
    untouched = await service.store.get(McpServerProfile, "tracker")
    await service.store.create(untouched.model_copy(update={"id": "untouched"}))
    ranked = []
    original = service._rank_deferred_tools

    async def rank(scope, specs, **kwargs):
        ranked.extend(specs)
        return await original(scope, specs, **kwargs)

    monkeypatch.setattr(service, "_rank_deferred_tools", rank)
    await _save(service, first.session.id, excluded_mcp_server_ids=["tracker"])
    second = await service.prepare_async(_next(request, first.session.id))
    assert MCP_TOOL not in ranked
    assert MCP_TOOL not in second.tool_components.specs
    assert MCP_TOOL not in second.model_request.instructions
    assert MCP_TOOL not in {tool.name for tool in second.model_request.tools}
    assert "mcp.untouched.search_issues" in second.tool_components.specs
    assert MCP_TOOL not in second.tool_manifest.present_digests
    assert all(
        item.source_id != "mcp:tracker" for item in second.tool_manifest.definitions
    )
    await service.complete(second)


async def test_pending_selection_does_not_change_frozen_active_turn_or_resume(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    session_id = first.session.id
    turn = await service.store.get(ChatTurn, first.turn.id)
    original = json.dumps(turn.request_snapshot, sort_keys=True)
    await _save(service, session_id, mcp_server_ids=[])
    assert (
        json.dumps(
            (await service.store.get(ChatTurn, turn.id)).request_snapshot,
            sort_keys=True,
        )
        == original
    )
    resumed = await service.prepare_resume(turn.id)
    assert "mcp.notes.read_note" in resumed.tool_manifest.present_digests
    assert "mcp.notes.read_note" in first.tool_manifest.present_digests
    await service.complete(first)
    assert (await service.store.get(ChatSession, session_id)).metadata[
        "excluded_mcp_server_ids"
    ] == ["notes"]


async def test_restart_fork_and_automatic_settings_preserve_exclusions(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    await service.complete(first)
    await _save(service, first.session.id, mcp_server_ids=[])
    settings = await ChatScheduleService(service.store).turn_settings(first.session.id)
    assert settings.excluded_mcp_server_ids == ["notes"]
    fork = await service.fork_session(first.session.id, through_latest=True)
    assert fork.metadata["excluded_mcp_server_ids"] == ["notes"]
    service.store = NebulaStore(service.store.database.url)
    saved = await chat_assistant_config(
        service.store,
        await service.store.get(ChatSession, fork.id),
    )
    assert saved["excluded_mcp_server_ids"] == ["notes"]
    next_turn = await service.prepare_async(_next(request, fork.id))
    assert "mcp.notes.read_note" not in next_turn.tool_components.specs
    await service.complete(next_turn)


async def test_old_metadata_defaults_empty_and_conflicting_selection_is_rejected(
    tmp_path,
    monkeypatch,
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    await service.complete(first)
    current = await chat_assistant_config(service.store, first.session)
    assert current["excluded_mcp_server_ids"] == []
    with pytest.raises(ValueError, match="selected and excluded"):
        await _save(
            service,
            first.session.id,
            mcp_server_ids=["notes"],
            excluded_mcp_server_ids=["notes"],
        )
    second = await service.prepare_async(_next(request, first.session.id))
    assert MCP_TOOL in second.tool_components.specs
    await service.complete(second)
