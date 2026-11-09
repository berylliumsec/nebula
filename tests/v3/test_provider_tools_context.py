"""Continuous provider journeys with inert local providers and synthetic tools."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from dataclasses import replace

import httpx
import pytest

import nebula.v3.chat as chat_module
from nebula.v3.chat import ChatRequestMessage, ChatService
from nebula.v3.context import estimate_model_request, estimate_tokens
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    McpServerProfile,
    ProviderProfile,
    ScopePolicy,
)
from nebula.v3.providers import (
    ModelResponse,
    ModelUsage,
    ProviderContextLengthError,
    ToolCall,
)
from nebula.v3.provider_tool_manifest import (
    MANIFEST_KEY,
    SECTION_START,
    read_manifest,
    render_tools,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_catalog import CATALOG_CALL, CATALOG_LOAD
from nebula.v3.tool_suggestions import (
    JevClient,
    NONE_OPTION,
    SuggestionCache,
    suggest_tools,
)
from tests.v3.test_chat import FakeProvider
from tests.v3.test_tool_catalog import FakeIndex
from tests.v3.test_tool_suggestions import (
    MCP_TOOL,
    _McpPlatform,
    _jev_answers,
    _mcp_service,
    _spec,
)


class InertProvider(FakeProvider):
    def __init__(self, identifier):
        super().__init__(identifier, local=True)
        self.config.capabilities.tools = True
        self.config.capabilities.strict_tools = True
        self.script = []

    async def complete(self, request):
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.requests.append(request)
        value = self.script.pop(0) if self.script else {"text": "Synthetic result."}
        if isinstance(value, Exception):
            raise value
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model-a",
            text=value.get("text", ""),
            tool_calls=value.get("calls", []),
            usage=ModelUsage(input_tokens=4, output_tokens=3, total_tokens=7),
            finish_reason="tool_calls" if value.get("calls") else "stop",
        )


async def _service(tmp_path, monkeypatch, *, jev=None, preload=False):
    service, request = await _mcp_service(
        tmp_path,
        monkeypatch,
        lambda: jev,
        scope={"tool_suggestions": jev is not None},
    )
    provider = InertProvider(request.provider_id)
    service.provider_factory = lambda _: provider
    if preload:
        index = FakeIndex({MCP_TOOL: 0.8})
        index.register({MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")})
        monkeypatch.setattr(service, "_tool_index", lambda: index)
    return service, request, provider


def _call(name, arguments, identifier):
    return {"calls": [ToolCall(id=identifier, name=name, arguments=arguments)]}


def _load(identifier="load"):
    return _call(CATALOG_LOAD, {"names": [MCP_TOOL]}, identifier)


def _use(identifier="use", value="ABC"):
    return _call(
        CATALOG_CALL, {"name": MCP_TOOL, "arguments": {"value": value}}, identifier
    )


def _next(request, session_id, text="Use the known tool."):
    return request.model_copy(
        update={
            "session_id": session_id,
            "messages": [
                ChatRequestMessage(role=ChatRole.USER, content=text),
            ],
        }
    )


def _chat_requests(provider):
    return [item for item in provider.requests if not item.metadata.get("operation")]


def _section(request):
    return json.loads(request.instructions.split(SECTION_START)[1].split("\n")[0])


async def _manifest(service, session_id):
    owner = await service.store.get(ChatSession, session_id)
    async with service.store.database.session() as session:
        return await read_manifest(
            session, project_id=owner.engagement_id, session_id=owner.id
        )


async def _history(service, session_id, count=8, repeat=240):
    await service._settle_precompaction(session_id)
    owner = await service.store.get(ChatSession, session_id)
    start = await service._next_sequence(owner)
    await service.store.create_many(
        [
            ChatMessage(
                engagement_id=owner.engagement_id,
                session_id=owner.id,
                sequence=start + i,
                role=ChatRole.USER if i % 2 == 0 else ChatRole.ASSISTANT,
                content=f"Synthetic old conversation {i}: "
                + "bounded context " * repeat,
            )
            for i in range(count)
        ]
    )


async def test_loaded_schema_is_single_canonical_definition_across_turns_compaction_and_restart(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    provider.script = [_load(), _use(), {"text": "Tool used."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    session_id = first.session.id
    known = await _manifest(service, session_id)
    schema = next(item for item in known.definitions if item.name == MCP_TOOL)
    assert schema.schema_present and schema.introduction_reason == "catalog_load"
    first_requests = _chat_requests(provider)
    saved_turn = await service.store.get(ChatTurn, first.turn.id)
    assert saved_turn.request_snapshot[MANIFEST_KEY] == known.reference()
    async with service.store.database.session() as database_session:
        for sent in first_requests:
            revision = await read_manifest(
                database_session,
                project_id=first.session.engagement_id,
                session_id=session_id,
                revision=int(sent.metadata["provider_tool_manifest_revision"]),
            )
            assert sent.metadata["provider_tool_manifest_digest"] == revision.digest
    assert _section(first_requests[0])["loaded"] == []
    assert all(item.instructions.count(SECTION_START) == 1 for item in first_requests)
    assert all(len(_section(item)["loaded"]) == 1 for item in first_requests[1:])
    replay = first_requests[1].tool_results[0]
    assert replay.name == CATALOG_LOAD and replay.arguments == {"names": [MCP_TOOL]}
    assert "input_schema" not in replay.output["loaded"][0]
    assert all(
        MCP_TOOL not in {tool.name for tool in item.tools} for item in first_requests
    )
    second = await service.prepare_async(_next(request, session_id))
    await service.complete(second)
    assert (
        _section(_chat_requests(provider)[-1])["loaded"][0]["input_schema"]
        == schema.input_schema
    )
    assert (await _manifest(service, session_id)).revision == known.revision

    await _history(service, session_id)
    compacted = await service.prepare_async(
        _next(request, session_id, "Use it after compaction.")
    )
    await service.complete(compacted)
    assert compacted.context_snapshot is not None
    assert (
        _section(_chat_requests(provider)[-1])["loaded"][0]["description"]
        == schema.description
    )
    assert all(
        '"input_schema"' not in (item.instructions or "")
        for item in provider.requests
        if item.metadata.get("operation") == "context_compaction"
    )
    await service.shutdown()
    await service.store.event_history.close()
    location = service.store.database.url
    await service.store.database.dispose()
    store = NebulaStore(location)
    restarted_provider = InertProvider(request.provider_id)
    restarted_provider.script = [_use("restart-use"), {"text": "Still usable."}]
    restarted = ChatService(
        store,
        provider_factory=lambda _: restarted_provider,
        tool_platform=_McpPlatform(tmp_path, store),
        workspace_resolver=lambda _: tmp_path,
        tool_suggestion_client=lambda: None,
    )
    resumed = await restarted.prepare_async(
        _next(request, session_id, "Use the exact saved tool after Core restart.")
    )
    result = await restarted.complete(resumed)
    assert result.message.content == "Still usable."
    assert (
        _section(_chat_requests(restarted_provider)[0])["loaded"][0]["input_schema"]
        == schema.input_schema
    )
    assert (await _manifest(restarted, session_id)).present_digests[
        MCP_TOOL
    ] == schema.definition_digest


async def test_preloaded_definition_is_not_duplicated_by_a_catalog_load_or_ranked_again(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch, preload=True)
    ranked = []
    original_rank = service._rank_deferred_tools

    async def observe(scope, deferred, **fields):
        ranked.append(sorted(deferred))
        return await original_rank(scope, deferred, **fields)

    monkeypatch.setattr(service, "_rank_deferred_tools", observe)
    provider.script = [_load(), {"text": "Loaded once."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    initial = await _manifest(service, first.session.id)
    for item in _chat_requests(provider):
        assert len(_section(item)["loaded"]) == 1
        assert _section(item)["available_not_loaded"] == []
        assert item.instructions.count('"input_schema"') == 1
        assert all(
            not isinstance(result.output, dict)
            or not result.output.get("loaded")
            or "input_schema" not in result.output["loaded"][0]
            for result in item.tool_results
        )
    second = await service.prepare_async(_next(request, first.session.id))
    await service.complete(second)
    assert ranked == [[MCP_TOOL], []]
    assert (await _manifest(service, first.session.id)).revision == initial.revision


async def test_jev_suggestions_remain_hints_and_exclude_loaded_current_digests(
    tmp_path, monkeypatch
):
    seen = []
    scores = [0.3, 0.8]

    def handler(raw):
        seen.append(json.loads(raw.content))
        score = scores.pop(0)
        return httpx.Response(
            200, json=_jev_answers({MCP_TOOL: score, NONE_OPTION: 0.1})
        )

    client = JevClient(api_key="inert", transport=httpx.MockTransport(handler))
    service, request, provider = await _service(tmp_path, monkeypatch, jev=client)
    first = await service.prepare_async(request)
    await service.complete(first)
    assert not (await _manifest(service, first.session.id)).present_digests.get(
        MCP_TOOL
    )
    assert _section(_chat_requests(provider)[0])["loaded"] == []
    second = await service.prepare_async(
        _next(request, first.session.id, "Load it for this different request.")
    )
    await service.complete(second)
    assert (await _manifest(service, first.session.id)).present_digests.get(MCP_TOOL)
    third = await service.prepare_async(
        _next(request, first.session.id, "Use it again.")
    )
    await service.complete(third)
    assert len(seen) == 2
    assert all(MCP_TOOL in body["questions"]["tools_0"]["criteria"] for body in seen)
    assert _section(_chat_requests(provider)[-1])["loaded"]


async def test_ranking_cache_tracks_complete_schema_constraints_without_sending_schemas():
    seen = []
    client = JevClient(
        api_key="inert",
        transport=httpx.MockTransport(
            lambda raw: (
                seen.append(json.loads(raw.content))
                or httpx.Response(
                    200, json=_jev_answers({MCP_TOOL: 0.3, NONE_OPTION: 0.2})
                )
            )
        ),
    )
    spec = _spec(MCP_TOOL, "Search.")
    cache = SuggestionCache()
    args = {"operator_messages": ["find issues"], "cache": cache}
    first = await suggest_tools(client, deferred={MCP_TOOL: spec}, **args)
    cached = await suggest_tools(client, deferred={MCP_TOOL: spec}, **args)
    changed = spec.model_copy(
        update={"input_schema": {**spec.input_schema, "required": []}}
    )
    after_change = await suggest_tools(client, deferred={MCP_TOOL: changed}, **args)
    assert not first.cached and cached.cached and not after_change.cached
    assert len(seen) == 2 and seen[0] == seen[1]
    assert all("input_schema" not in json.dumps(body) for body in seen)
    assert (
        await suggest_tools(client, deferred={}, operator_messages=[])
    ).status == "no_tool_needed"
    assert len(seen) == 2


async def test_failed_load_never_marks_the_schema_present(tmp_path, monkeypatch):
    service, request, provider = await _service(tmp_path, monkeypatch)
    provider.script = [
        _call(CATALOG_LOAD, {"names": ["unknown"]}, "failed"),
        {"text": "Unavailable."},
    ]
    first = await service.prepare_async(request)
    await service.complete(first)
    assert MCP_TOOL not in (await _manifest(service, first.session.id)).present_digests
    assert all(_section(item)["loaded"] == [] for item in _chat_requests(provider))


async def test_older_forks_inherit_only_their_boundary_revision_and_survive_source_deletion(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    first = await service.prepare_async(request)
    await service.complete(first)
    before = await service.session_messages(first.session.id)
    provider.script = [_load(), {"text": "Learned later."}]
    later = await service.prepare_async(_next(request, first.session.id))
    await service.complete(later)
    assert (await _manifest(service, first.session.id)).present_digests.get(MCP_TOOL)
    old_fork = await service.fork_session(
        first.session.id, through_message_id=before[-1].id
    )
    new_fork = await service.fork_session(first.session.id, through_latest=True)
    assert MCP_TOOL not in (await _manifest(service, old_fork.id)).present_digests
    assert MCP_TOOL in (await _manifest(service, new_fork.id)).present_digests
    fork_history = await service.session_messages(new_fork.id)
    nested = await service.fork_session(
        new_fork.id, through_message_id=fork_history[1].id
    )
    assert MCP_TOOL not in (await _manifest(service, nested.id)).present_digests
    await service.store.delete_chat_session(first.session.id)
    assert MCP_TOOL in (await _manifest(service, new_fork.id)).present_digests


async def test_provider_context_rejection_keeps_the_manifest_and_revision_on_retry(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    profile = await service.store.get(ProviderProfile, request.provider_id)
    await service.store.update(
        ProviderProfile,
        profile.id,
        {
            "metadata": {
                **profile.metadata,
                "model_descriptors": [
                    {
                        "id": "model-a",
                        "context_window": 16_000,
                        "max_output_tokens": 2_048,
                    }
                ],
            }
        },
        expected_revision=profile.revision,
    )
    request = request.model_copy(update={"max_output_tokens": 2_048})
    first = await service.prepare_async(request)
    await service.complete(first)
    await _history(service, first.session.id, count=4, repeat=150)
    provider.script = [
        _load(),
        ProviderContextLengthError("synthetic context too large"),
        _use("after-recovery"),
        {"text": "Recovered."},
    ]
    next_turn = await service.prepare_async(_next(request, first.session.id))
    assert next_turn.context_snapshot is None
    result = await service.complete(next_turn)
    assert result.message.content == "Recovered."
    requests = _chat_requests(provider)[1:]
    loaded = [item for item in requests if _section(item)["loaded"]]
    assert len(loaded) >= 2
    assert estimate_model_request(loaded[1]) < estimate_model_request(loaded[0])
    assert _section(loaded[1])["loaded"] == _section(loaded[0])["loaded"]
    assert (
        loaded[1].metadata["provider_tool_manifest_revision"]
        == loaded[0].metadata["provider_tool_manifest_revision"]
    )
    assert {item.metadata["provider_tool_manifest_digest"] for item in loaded} == {
        (await _manifest(service, first.session.id)).digest
    }
    assert all(item.instructions.count(SECTION_START) == 1 for item in requests)
    assert all(
        estimate_model_request(item)
        >= estimate_tokens(render_tools(next_turn.tool_manifest))
        for item in loaded
    )
    history = await service._turn_history(next_turn.turn)
    assert (
        sum(
            item["name"] == MCP_TOOL and item["status"] == "complete"
            for item in history
        )
        == 1
    )


async def test_source_disable_and_reenable_reconcile_before_new_requests(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    provider.script = [_load(), {"text": "Known."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    known = await _manifest(service, first.session.id)
    profile = await service.store.get(McpServerProfile, "tracker")
    disabled = await service.store.update(
        McpServerProfile,
        profile.id,
        {"enabled": False},
        expected_revision=profile.revision,
    )
    next_turn = await service.prepare_async(_next(request, first.session.id))
    await service.complete(next_turn)
    absent = await _manifest(service, first.session.id)
    assert MCP_TOOL not in absent.present_digests
    assert absent.revision > known.revision
    assert MCP_TOOL not in {item.name for item in absent.definitions}
    await service.store.update(
        McpServerProfile,
        profile.id,
        {"enabled": True},
        expected_revision=disabled.revision,
    )
    enabled = await service.prepare_async(_next(request, first.session.id))
    assert MCP_TOOL not in enabled.tool_manifest.present_digests
    assert (
        _section(enabled.model_request)["available_not_loaded"][0]["name"] == MCP_TOOL
    )
    provider.script = [_load("reenable"), {"text": "Known again."}]
    await service.complete(enabled)
    assert MCP_TOOL in (await _manifest(service, first.session.id)).present_digests


async def test_updated_complete_definition_replaces_the_loaded_version_before_the_next_request(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)

    class SchemaPlatform(_McpPlatform):
        async def chat_components(self, *, mcp_profiles=(), **fields):
            components = await super().chat_components(
                mcp_profiles=mcp_profiles, **fields
            )
            schemas = {
                f"mcp.{profile.id}.{tool.name}": tool.input_schema
                for profile in mcp_profiles
                for tool in profile.capabilities.tools
                if tool.input_schema.get("properties")
            }
            return replace(
                components,
                specs={
                    name: spec.model_copy(update={"input_schema": schemas[name]})
                    if name in schemas
                    else spec
                    for name, spec in components.specs.items()
                },
            )

    service.tool_platform = SchemaPlatform(tmp_path, service.store)
    provider.script = [_load(), {"text": "Loaded old version."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    old = await _manifest(service, first.session.id)
    source = await service.store.get(McpServerProfile, "tracker")
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "enum": ["ABC"],
                "pattern": "^[A-Z]{3}$",
                "minLength": 3,
            }
        },
        "required": ["value"],
        "additionalProperties": False,
    }
    updated_tool = source.capabilities.tools[0].model_copy(
        update={"description": "New exact purpose.", "input_schema": schema}
    )
    await service.store.update(
        McpServerProfile,
        source.id,
        {
            "capabilities": source.capabilities.model_copy(
                update={"tools": [updated_tool]}
            )
        },
        expected_revision=source.revision,
    )
    prepared = await service.prepare_async(_next(request, first.session.id))
    await service.complete(prepared)
    current = await _manifest(service, first.session.id)
    assert current.present_digests[MCP_TOOL] != old.present_digests[MCP_TOOL]
    loaded = _section(_chat_requests(provider)[-1])["loaded"]
    assert len(loaded) == 1 and loaded[0]["input_schema"] == schema
    assert loaded[0]["description"] == "New exact purpose."


async def test_unavailable_jev_uses_local_ranking_and_retained_schema_context(
    tmp_path, monkeypatch
):
    attempts = []
    client = JevClient(
        api_key="inert",
        transport=httpx.MockTransport(
            lambda raw: attempts.append(raw) or httpx.Response(503)
        ),
    )
    service, request, provider = await _service(tmp_path, monkeypatch, jev=client)
    provider.script = [_load(), {"text": "Local fallback worked."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    assert first.turn.request_snapshot["tool_suggestions"]["status"] == "unavailable"
    assert first.turn.request_snapshot["tool_catalog"]["ranker"] == "keyword"
    second = await service.prepare_async(_next(request, first.session.id))
    await service.complete(second)
    assert len(attempts) == 1
    assert _section(_chat_requests(provider)[-1])["loaded"][0]["name"] == MCP_TOOL


async def test_revocation_while_a_provider_request_is_in_flight_cannot_execute_a_known_tool(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch, preload=True)
    original = provider.complete
    changed = False

    async def revoke_before_response(model_request):
        nonlocal changed
        response = await original(model_request)
        if not model_request.metadata.get("operation") and not changed:
            changed = True
            source = await service.store.get(McpServerProfile, "tracker")
            await service.store.update(
                McpServerProfile,
                source.id,
                {"enabled": False},
                expected_revision=source.revision,
            )
        return response

    monkeypatch.setattr(provider, "complete", revoke_before_response)
    provider.script = [_use("revoked-call"), {"text": "The tool was revoked."}]
    prepared = await service.prepare_async(request)
    await service.complete(prepared)
    history = await service._turn_history(prepared.turn)
    assert history[-1]["status"] == "failed"
    assert (
        MCP_TOOL not in (await _manifest(service, prepared.session.id)).present_digests
    )
    assert all(
        SECTION_START not in (item.instructions or "") or not _section(item)["loaded"]
        for item in _chat_requests(provider)[1:]
    )


async def test_retained_definitions_exceeding_capacity_fail_clearly_without_truncation(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    provider.script = [_load(), {"text": "Loaded."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    retained = await _manifest(service, first.session.id)
    count = len(_chat_requests(provider))
    source = await service.store.get(McpServerProfile, "tracker")
    tool = source.capabilities.tools[0].model_copy(
        update={"description": "Exact retained description. " * 350}
    )
    await service.store.update(
        McpServerProfile,
        source.id,
        {"capabilities": source.capabilities.model_copy(update={"tools": [tool]})},
        expected_revision=source.revision,
    )
    with pytest.raises(
        chat_module.ChatConfigurationError,
        match="retained tool definitions.*context capacity",
    ):
        await service.prepare_async(_next(request, first.session.id))
    assert len(_chat_requests(provider)) == count
    assert await _manifest(service, first.session.id) == retained


@pytest.mark.parametrize("change_scope", [False, True])
async def test_approval_restart_restores_exact_manifest_and_retains_scope_guards(
    tmp_path, monkeypatch, change_scope
):
    from nebula.v3.domain import Approval, ApprovalStatus, utc_now
    from nebula.v3.policy import PolicyEngine
    from nebula.v3.sandbox import AnalysisOnlyRunner
    from nebula.v3.tools import AnalysisTool, StoreToolLedger, ToolBroker, ToolRegistry

    effects = []

    class ApprovalPlatform(_McpPlatform):
        async def chat_components(self, **fields):
            components = await super().chat_components(**fields)
            registry = ToolRegistry()

            async def effect(arguments):
                effects.append(dict(arguments))
                return {"value": arguments["value"]}

            for name, spec in components.specs.items():
                registry.register(
                    AnalysisTool(
                        spec.model_copy(update={"requires_approval": name == MCP_TOOL}),
                        effect,
                    )
                )
            broker = ToolBroker(
                registry=registry,
                policy_engine=PolicyEngine(),
                runner=AnalysisOnlyRunner(),
                ledger=StoreToolLedger(self.store),
                workspace_resolver=lambda _: self.workspace,
            )
            return replace(
                components,
                broker=broker,
                specs={spec.name: spec for spec in registry.specs()},
            )

    service, request, provider = await _service(tmp_path, monkeypatch)
    service.tool_platform = ApprovalPlatform(tmp_path, service.store)
    provider.script = [_load(), _use("approval")]
    first = await service.prepare_async(request)
    with pytest.raises(chat_module.ChatError, match="waiting for operator approval"):
        await service.complete(first)
    paused = await service.store.get(ChatTurn, first.turn.id)
    saved = await _manifest(service, first.session.id)
    assert MCP_TOOL in saved.present_digests and not effects
    assert paused.request_snapshot[MANIFEST_KEY] == saved.reference()
    await service.shutdown()
    await service.store.event_history.close()
    await service.store.database.dispose()
    store = NebulaStore(service.store.database.url)
    restarted_provider = InertProvider(request.provider_id)
    restarted = ChatService(
        store,
        provider_factory=lambda _: restarted_provider,
        tool_platform=ApprovalPlatform(tmp_path, store),
        workspace_resolver=lambda _: tmp_path,
        tool_suggestion_client=lambda: None,
    )
    if change_scope:
        scope = await store.get(ScopePolicy, "scope-jev")
        await store.update(
            ScopePolicy,
            scope.id,
            {"max_concurrency": 2},
            expected_revision=scope.revision,
        )
        with pytest.raises(chat_module.ChatHistoryConflict, match="scope changed"):
            await restarted.prepare_resume(paused.id)
        assert not effects and not _chat_requests(restarted_provider)
        return
    approval = await store.get(Approval, paused.approval_id)
    await store.update(
        Approval,
        approval.id,
        {
            "status": ApprovalStatus.APPROVED,
            "decided_by": "synthetic-operator",
            "decided_at": utc_now(),
        },
        expected_revision=approval.revision,
    )
    resumed = await restarted.prepare_resume(paused.id)
    assert resumed.tool_manifest == saved
    restarted_provider.script = [{"text": "Approved read."}]
    result = await restarted.complete(resumed)
    assert result.message.content == "Approved read."
    assert effects == [{"value": "ABC"}]
    assert (await _manifest(restarted, first.session.id)).present_digests[
        MCP_TOOL
    ] == saved.present_digests[MCP_TOOL]


async def test_background_compaction_preserves_exact_definitions_outside_its_summary(
    tmp_path, monkeypatch
):
    service, request, provider = await _service(tmp_path, monkeypatch)
    provider.script = [_load(), {"text": "Learned."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    known = await _manifest(service, first.session.id)
    await _history(service, first.session.id)
    state = chat_module._Precompaction()
    await service._precompact(
        first.session.id, request.provider_id, "model-a", None, state
    )
    assert state.started
    assert await _manifest(service, first.session.id) == known
    compactors = [
        item
        for item in provider.requests
        if item.metadata.get("operation") == "context_compaction"
    ]
    assert compactors and all(
        SECTION_START not in (item.instructions or "") for item in compactors
    )
    assert all(
        '"input_schema"'
        not in json.dumps([message.model_dump() for message in item.messages])
        for item in compactors
    )
    prepared = await service.prepare_async(_next(request, first.session.id))
    await service.complete(prepared)
    assert prepared.context_snapshot is not None
    assert render_tools(prepared.tool_manifest) == render_tools(known)


async def test_tool_result_clearing_keeps_the_loaded_definition_in_managed_context(
    tmp_path, monkeypatch
):
    from nebula.v3.domain import McpToolSnapshot
    from nebula.v3.tool_catalog import CATALOG_SEARCH

    service, request, provider = await _service(tmp_path, monkeypatch)
    source = await service.store.get(McpServerProfile, "tracker")
    tools = [
        *source.capabilities.tools,
        *[
            McpToolSnapshot(
                name=f"padding_{index:02d}",
                description="Padding matching. " + "catalogue descriptive detail " * 7,
            )
            for index in range(20)
        ],
    ]
    await service.store.update(
        McpServerProfile,
        source.id,
        {"capabilities": source.capabilities.model_copy(update={"tools": tools})},
        expected_revision=source.revision,
    )
    provider.script = [
        _load(),
        _call(
            CATALOG_SEARCH, {"query": "padding matching", "limit": 20}, "large-search"
        ),
        {"text": "Definitions survived clearing."},
    ]
    prepared = await service.prepare_async(request)
    await service.complete(prepared)
    assert prepared.cleared_tool_calls
    final = _chat_requests(provider)[-1]
    assert _section(final)["loaded"][0]["name"] == MCP_TOOL
    assert (
        _section(final)["loaded"][0]["input_schema"]
        == _spec(MCP_TOOL, "Search tracker issues.").input_schema
    )


@pytest.mark.parametrize("artifacts", ["absent", "available", "write_failed"])
async def test_large_successful_load_survives_delivery_bounds_later_turn_and_restart(
    tmp_path, monkeypatch, artifacts
):
    from nebula.v3.artifacts import ArtifactStore

    class FailingArtifacts:
        calls = 0

        def put_bytes_with_status(self, *args, **kwargs):
            self.calls += 1
            raise OSError("synthetic artifact write failure")

    service, request, provider = await _service(tmp_path, monkeypatch)
    if artifacts == "available":
        service.artifact_store = ArtifactStore(tmp_path / "artifacts")
    elif artifacts == "write_failed":
        service.artifact_store = FailingArtifacts()
    profile = await service.store.get(ProviderProfile, request.provider_id)
    await service.store.update(
        ProviderProfile,
        profile.id,
        {
            "metadata": {
                **profile.metadata,
                "model_descriptors": [
                    {
                        "id": "model-a",
                        "context_window": 16_000,
                        "max_output_tokens": 2_048,
                    }
                ],
            }
        },
        expected_revision=profile.revision,
    )
    source = await service.store.get(McpServerProfile, "tracker")
    description = ("Exact long description. " * 400).rstrip() + "."
    tool = source.capabilities.tools[0].model_copy(update={"description": description})
    await service.store.update(
        McpServerProfile,
        source.id,
        {"capabilities": source.capabilities.model_copy(update={"tools": [tool]})},
        expected_revision=source.revision,
    )
    request = request.model_copy(update={"max_output_tokens": 2_048})
    provider.script = [_load(), _use(), {"text": "Long schema introduced."}]
    first = await service.prepare_async(request)
    await service.complete(first)
    manifest = await _manifest(service, first.session.id)
    assert manifest.present_digests[MCP_TOOL]
    loaded = _section(_chat_requests(provider)[-1])["loaded"]
    assert len(loaded) == 1 and loaded[0]["description"] == description
    history = await service._turn_history(first.turn)
    load = next(item for item in history if item["name"] == CATALOG_LOAD)
    assert load["status"] == "complete"
    assert load["provider_tool_loaded"][MCP_TOOL] == manifest.present_digests[MCP_TOOL]
    assert len(load["provider_result"].encode()) < 8_192
    assert '"input_schema"' not in load["provider_result"]
    later = await service.prepare_async(_next(request, first.session.id))
    await service.complete(later)
    assert _section(_chat_requests(provider)[-1])["loaded"] == loaded
    if artifacts == "write_failed":
        assert service.artifact_store.calls == 0
    await service.shutdown()
    await service.store.event_history.close()
    await service.store.database.dispose()
    store = NebulaStore(service.store.database.url)
    restarted_provider = InertProvider(request.provider_id)
    restarted_provider.script = [
        _use("large-restart"),
        {"text": "Long schema survived restart."},
    ]
    restarted = ChatService(
        store,
        provider_factory=lambda _: restarted_provider,
        tool_platform=_McpPlatform(tmp_path, store),
        workspace_resolver=lambda _: tmp_path,
        tool_suggestion_client=lambda: None,
    )
    prepared = await restarted.prepare_async(_next(request, first.session.id))
    assert (
        await restarted.complete(prepared)
    ).message.content == "Long schema survived restart."
    assert _section(_chat_requests(restarted_provider)[0])["loaded"] == loaded
    assert all(
        item["name"] != CATALOG_LOAD
        for item in await restarted._turn_history(prepared.turn)
    )
