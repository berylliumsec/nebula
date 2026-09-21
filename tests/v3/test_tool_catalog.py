import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    McpApprovalMode,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    ScopePolicy,
    utc_now,
)
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.mcp import catalog_mcp_profiles, mcp_tool_runtime_name
from nebula.v3.providers import ToolCall
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_catalog import (
    CATALOG_CALL,
    CATALOG_LOAD,
    CATALOG_SEARCH,
    MAX_CATALOG_CALLS_PER_TURN,
    ToolCatalogBroker,
    catalog_components,
    catalog_instructions,
    deferrable_specs,
    fingerprint,
    index_document,
    loaded_tool_names,
    on_demand_enabled,
    rank_for_request,
    unwrap_call,
)
from nebula.v3.tools import InvalidToolArguments, ToolInvocation
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_knowledge_index import SecurityEmbeddingFunction
from tests.v3.test_tool_suggestions import MCP_TOOL, NOTES_TOOL, _mcp_service, _spec

CREDENTIAL_TOOL = "mcp.vault.rotate_password"
DATABASE_TOOL = "mcp.warehouse.run_sql"


class FakeIndex:
    """Scores by a fixed similarity per tool name; records what it indexed."""

    def __init__(self, similarities, *, state="ready", fail=False):
        self.similarities = similarities
        self.status = SimpleNamespace(state=state)
        self.fail = fail
        self.indexed: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.warmed = threading.Event()

    def index_tools(self, documents):
        if self.fail:
            raise RuntimeError("index offline")
        self.indexed.update(documents)

    def rank_tools(self, query, fingerprints, *, limit):
        del query
        scored = [
            (key, self.similarities.get(self.names.get(key), 0.0))
            for key in fingerprints
        ]
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]

    def prepare_model(self):
        self.warmed.set()

    def register(self, specs):
        self.names = {fingerprint(spec): name for name, spec in specs.items()}


def _catalog():
    return {
        MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues by keyword."),
        CREDENTIAL_TOOL: _spec(CREDENTIAL_TOOL, "Rotate a stored credential."),
        DATABASE_TOOL: _spec(DATABASE_TOOL, "Run a read-only query."),
    }


def _invoke(broker, name, arguments, tmp_path):
    return asyncio.run(
        broker.execute(
            ToolInvocation(
                engagement_id="e",
                run_id="turn",
                tool_name=name,
                arguments=arguments,
                workspace=tmp_path,
            ),
            ScopePolicy(engagement_id="e"),
        )
    ).output


def test_on_demand_loading_is_on_by_default_and_can_be_turned_off():
    assert on_demand_enabled(ScopePolicy(engagement_id="e"))
    assert not on_demand_enabled(ScopePolicy(engagement_id="e", on_demand_tools=False))
    # Jev ranks the deferred catalog, so opting into it keeps deferral on.
    assert on_demand_enabled(
        ScopePolicy(engagement_id="e", on_demand_tools=False, tool_suggestions=True)
    )


def test_always_loaded_tools_stay_out_of_the_deferred_catalog():
    specs = {**_catalog(), "run_command": _spec("run_command", "Run.", source=None)}
    scope = ScopePolicy(engagement_id="e", always_loaded_tools=[f" {MCP_TOOL} "])

    deferred = deferrable_specs(specs, always_loaded=scope.always_loaded_tools)

    assert MCP_TOOL not in deferred
    assert set(deferred) == {CREDENTIAL_TOOL, DATABASE_TOOL}
    # A pin for a tool this runtime no longer offers defers nothing extra.
    assert set(deferrable_specs(specs, always_loaded=["mcp.gone.tool"])) == set(
        _catalog()
    )


def test_selected_sources_stay_out_of_the_deferred_catalog():
    specs = {
        MCP_TOOL: _spec(MCP_TOOL, "Search.", source="mcp:tracker"),
        CREDENTIAL_TOOL: _spec(CREDENTIAL_TOOL, "Rotate.", source="mcp:vault"),
        DATABASE_TOOL: _spec(DATABASE_TOOL, "Query.", source="mcp:warehouse"),
    }

    deferred = deferrable_specs(
        specs, always_loaded=[DATABASE_TOOL], always_loaded_sources=["mcp:tracker"]
    )

    # A selected server goes out whole; a pin still keeps a single tool.
    assert set(deferred) == {CREDENTIAL_TOOL}


def test_keyword_search_and_load_are_bounded_to_deferred_tools(tmp_path):
    broker = ToolCatalogBroker(_catalog())

    found = _invoke(broker, CATALOG_SEARCH, {"query": "search issues"}, tmp_path)
    assert found["matches"][0]["name"] == MCP_TOOL
    assert found["search"] == "keyword"
    loaded = _invoke(broker, CATALOG_LOAD, {"names": [MCP_TOOL]}, tmp_path)
    assert loaded["loaded"][0]["input_schema"]["required"] == ["value"]
    assert CATALOG_CALL in loaded["note"]
    with pytest.raises(InvalidToolArguments, match="not on-demand"):
        _invoke(broker, CATALOG_LOAD, {"names": ["run_command"]}, tmp_path)
    with pytest.raises(InvalidToolArguments, match="not an on-demand tool"):
        _invoke(broker, CATALOG_CALL, {"name": "run_command"}, tmp_path)


def test_semantic_search_finds_a_tool_that_shares_no_keyword(tmp_path):
    specs = _catalog()
    index = FakeIndex({CREDENTIAL_TOOL: 0.8, DATABASE_TOOL: 0.2, MCP_TOOL: 0.1})
    index.register(specs)
    broker = ToolCatalogBroker(specs, index=index)

    found = _invoke(broker, CATALOG_SEARCH, {"query": "reset login secret"}, tmp_path)

    assert found["search"] == "semantic"
    assert found["matches"][0]["name"] == CREDENTIAL_TOOL
    assert set(index.indexed) == {fingerprint(spec) for spec in specs.values()}


def test_search_falls_back_to_keywords_when_the_index_is_not_ready(tmp_path):
    for index in (FakeIndex({}, state="downloading"), FakeIndex({}, fail=True)):
        broker = ToolCatalogBroker(_catalog(), index=index)
        found = _invoke(broker, CATALOG_SEARCH, {"query": "search issues"}, tmp_path)
        assert found["search"] == "keyword"
        assert found["matches"][0]["name"] == MCP_TOOL


def test_fingerprint_tracks_the_indexed_text():
    spec = _spec(MCP_TOOL, "Search tracker issues.")
    assert fingerprint(spec) == fingerprint(spec.model_copy())
    changed = spec.model_copy(update={"description": "Close tracker issues."})
    assert fingerprint(changed) != fingerprint(spec)
    document = index_document(spec)
    assert document.startswith("mcp tracker search issues\nmcp:tracker\n")
    assert "Parameters: value" in document


def test_pre_turn_ranking_preloads_only_confident_semantic_matches():
    specs = _catalog()
    index = FakeIndex({CREDENTIAL_TOOL: 0.45, MCP_TOOL: 0.3, DATABASE_TOOL: 0.1})
    index.register(specs)

    receipt = rank_for_request(index, specs, "my login stopped working")

    assert receipt.ranker == "semantic"
    assert receipt.preloaded == [CREDENTIAL_TOOL]
    assert receipt.suggested == [MCP_TOOL]
    assert receipt.deferred == sorted(specs)
    assert receipt.scores == {CREDENTIAL_TOOL: 0.45, MCP_TOOL: 0.3}

    fallback = rank_for_request(None, specs, "search tracker issues")
    assert fallback.ranker == "keyword"
    assert fallback.preloaded == []
    assert fallback.suggested[0] == MCP_TOOL


def test_catalog_instructions_carry_preloaded_schemas_as_data():
    specs = _catalog()
    text = catalog_instructions(
        {
            "deferred": sorted(specs),
            "preloaded": [CREDENTIAL_TOOL],
            "suggested": [MCP_TOOL],
        },
        specs,
    )
    assert "On-demand tools: 3 tools" in text
    assert f'"name":"{CREDENTIAL_TOOL}"' in text
    assert '"input_schema"' in text
    assert f'not loaded: ["{MCP_TOOL}"]' in text
    assert catalog_instructions({"deferred": []}, specs) == ""


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"name": MCP_TOOL, "arguments": {"value": "x"}}, (MCP_TOOL, {"value": "x"})),
        ({"name": MCP_TOOL, "arguments": '{"value": "x"}'}, (MCP_TOOL, {"value": "x"})),
        ({"name": MCP_TOOL}, (MCP_TOOL, {})),
        ({"name": "run_command", "arguments": {}}, None),
        ({"arguments": {}}, None),
    ],
)
def test_unwrap_call_accepts_only_on_demand_targets(arguments, expected):
    assert unwrap_call(arguments, [MCP_TOOL]) == expected


def test_loaded_names_come_from_preloads_loads_and_direct_calls():
    receipt = {"deferred": ["a", "b", "c", "d"], "preloaded": ["a"]}
    history = [
        {"name": CATALOG_LOAD, "status": "complete", "arguments": {"names": ["b"]}},
        {"name": CATALOG_LOAD, "status": "failed", "arguments": {"names": ["c"]}},
        {"name": "d", "status": "complete", "arguments": {}},
    ]
    assert loaded_tool_names(receipt, history) == {"a", "b", "d"}


def test_catalog_digest_is_stable_for_resume(tmp_path):
    components = RuntimeToolComponents(
        broker=RecordingBroker(),
        scope=ScopePolicy(engagement_id="e"),
        workspace=tmp_path,
        specs={MCP_TOOL: _spec(MCP_TOOL, "Search.")},
    )
    first = catalog_components(components, deferred=[MCP_TOOL])
    second = catalog_components(components, deferred=[MCP_TOOL], index=FakeIndex({}))
    assert first is not None and second is not None
    assert first.runtime_digest == second.runtime_digest
    assert set(first.specs) == {CATALOG_SEARCH, CATALOG_LOAD, CATALOG_CALL}
    assert catalog_components(components, deferred=[]) is None


def test_chroma_tool_collection_embeds_each_fingerprint_once(tmp_path):
    embedding = SecurityEmbeddingFunction()
    calls = []
    original = embedding.__call__

    class Counting(SecurityEmbeddingFunction):
        def __call__(self, input):
            calls.append(list(input))
            return original(input)

    index = ChromaKnowledgeIndex(tmp_path / "index", embedding_function=Counting())
    documents = {
        "fp-credential": "rotate password credential",
        "fp-database": "run sql against postgres",
        "fp-network": "scan a tls listener port",
    }
    index.index_tools(documents)
    index.index_tools(documents)
    embedded = [text for batch in calls for text in batch]
    assert sorted(embedded) == sorted(documents.values())

    ranked = index.rank_tools(
        "my login password expired", ["fp-credential", "fp-database"], limit=5
    )
    assert [key for key, _ in ranked] == ["fp-credential", "fp-database"]
    assert ranked[0][1] > ranked[1][1]
    # Candidates outside the turn's catalog are never returned.
    assert index.rank_tools("tls port", ["fp-database"], limit=5)[0][0] == "fp-database"


def _deferred_loop(tmp_path, responses, *, preloaded=()):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(tmp_path, responses, broker)
    components = prepared.tool_components
    mcp = _spec(MCP_TOOL, "Search tracker issues.")
    # RecordingBroker serves both the built-in and the MCP-sourced spec.
    components = RuntimeToolComponents(
        broker=broker,
        scope=components.scope,
        workspace=components.workspace,
        specs={**components.specs, MCP_TOOL: mcp},
        runtime_digest=components.runtime_digest,
    )
    receipt = {
        "deferred": [MCP_TOOL],
        "preloaded": list(preloaded),
        "suggested": [] if preloaded else [MCP_TOOL],
        "ranker": "semantic",
    }
    catalog = catalog_components(components, deferred=[MCP_TOOL])
    prepared.tool_components = chat_module.combine_tool_components(components, catalog)
    turn = store.update(
        ChatTurn,
        prepared.turn.id,
        {"request_snapshot": {"tool_catalog": receipt}},
        expected_revision=prepared.turn.revision,
    )
    prepared.turn = turn
    return store, service, prepared, provider, broker


def _tools(request):
    return [tool.model_dump(mode="json") for tool in request.tools]


def _finish(call_id):
    return _response(calls=[ToolCall(id=call_id, name="finish_response", arguments={})])


def test_tools_array_is_identical_across_search_load_and_call(tmp_path):
    responses = [
        _response(
            calls=[
                ToolCall(id="c1", name=CATALOG_SEARCH, arguments={"query": "issues"})
            ]
        ),
        _response(
            calls=[
                ToolCall(id="c2", name=CATALOG_LOAD, arguments={"names": [MCP_TOOL]})
            ]
        ),
        _response(
            calls=[
                ToolCall(
                    id="c3",
                    name=CATALOG_CALL,
                    arguments={"name": MCP_TOOL, "arguments": {"value": "x"}},
                )
            ]
        ),
        _finish("c4"),
        _response(text="Found it."),
    ]
    store, service, prepared, provider, broker = _deferred_loop(tmp_path, responses)

    asyncio.run(service.complete(prepared))

    routing = [item for item in provider.requests if item.tools]
    assert len(routing) == 4
    assert all(_tools(item) == _tools(routing[0]) for item in routing)
    assert all(item.instructions == routing[0].instructions for item in routing)
    names = [tool.name for tool in routing[0].tools]
    assert MCP_TOOL not in names
    assert {CATALOG_SEARCH, CATALOG_LOAD, CATALOG_CALL, "safe_read"} <= set(names)
    call_tool = next(tool for tool in routing[0].tools if tool.name == CATALOG_CALL)
    assert call_tool.strict is False
    assert "On-demand tools: 1 tools" in routing[0].instructions

    # The broker ran the real tool with the unwrapped arguments.
    assert [(call.tool_name, call.arguments) for call in broker.calls] == [
        (MCP_TOOL, {"value": "x"})
    ]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    entry = next(item for item in turn.tool_history if item["model_call_id"] == "c3")
    assert entry["name"] == MCP_TOOL and entry["status"] == "complete"
    # The provider sees the call it actually issued when history is replayed.
    replayed = next(item for item in routing[3].tool_results if item.call_id == "c3")
    assert replayed.name == CATALOG_CALL
    assert replayed.arguments == {"name": MCP_TOOL, "arguments": {"value": "x"}}
    # Final synthesis lists the used tool but no unused on-demand tools.
    final = provider.requests[-1]
    assert not final.tools and MCP_TOOL in final.instructions


def test_final_synthesis_omits_on_demand_tools_that_were_never_loaded(tmp_path):
    responses = [_finish("c1"), _response(text="Nothing needed.")]
    _, service, prepared, provider, _ = _deferred_loop(tmp_path, responses)

    asyncio.run(service.complete(prepared))

    final = provider.requests[-1]
    assert "BEGIN COMMAND-RUNTIME CAPABILITIES" in final.instructions
    inventory = final.instructions.split("BEGIN COMMAND-RUNTIME CAPABILITIES (JSON)\n")[
        1
    ].split("\n")[0]
    assert MCP_TOOL not in {item["name"] for item in json.loads(inventory)}


def test_preloaded_tool_is_callable_from_the_first_step(tmp_path):
    responses = [
        _response(
            calls=[
                ToolCall(
                    id="c1",
                    name=CATALOG_CALL,
                    arguments={"name": MCP_TOOL, "arguments": {"value": "x"}},
                )
            ]
        ),
        _finish("c2"),
        _response(text="Done."),
    ]
    _, service, prepared, provider, broker = _deferred_loop(
        tmp_path, responses, preloaded=[MCP_TOOL]
    )

    asyncio.run(service.complete(prepared))

    first = provider.requests[0]
    assert MCP_TOOL not in {tool.name for tool in first.tools}
    assert "Already loaded for this request" in first.instructions
    assert '"input_schema"' in first.instructions
    assert [call.tool_name for call in broker.calls] == [MCP_TOOL]


def test_call_to_an_unknown_tool_is_an_error_the_model_can_correct(tmp_path):
    responses = [
        _response(
            calls=[
                ToolCall(
                    id="c1",
                    name=CATALOG_CALL,
                    arguments={"name": "mcp.tracker.missing", "arguments": {}},
                )
            ]
        ),
        _finish("c2"),
        _response(text="That tool does not exist."),
    ]
    store, service, prepared, provider, broker = _deferred_loop(tmp_path, responses)

    asyncio.run(service.complete(prepared))

    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    entry = turn.tool_history[0]
    assert entry["name"] == CATALOG_CALL and entry["status"] == "failed"
    assert "not an on-demand tool" in json.dumps(entry["provider_result"])


def test_discovery_limit_is_refused_without_changing_the_tools_array(tmp_path):
    search = [
        _response(
            calls=[
                ToolCall(
                    id=f"s{i}",
                    name=CATALOG_SEARCH,
                    arguments={"query": "tracker issues"},
                )
            ]
        )
        for i in range(MAX_CATALOG_CALLS_PER_TURN + 1)
    ]
    responses = [*search, _finish("done"), _response(text="Stopped searching.")]
    store, service, prepared, provider, _ = _deferred_loop(tmp_path, responses)
    turn = store.update(
        ChatTurn,
        prepared.turn.id,
        {"max_artifact_queries": None, "max_tool_calls": None},
        expected_revision=prepared.turn.revision,
    )
    prepared.turn = turn

    asyncio.run(service.complete(prepared))

    routing = [item for item in provider.requests if item.tools]
    assert all(_tools(item) == _tools(routing[0]) for item in routing)
    history = store.get(ChatTurn, "turn").tool_history
    assert [item["status"] for item in history][-1] == "failed"
    assert "search limit" in json.dumps(history[-1]["provider_result"])
    assert all(item["status"] == "complete" for item in history[:-1])


def test_direct_call_to_an_unloaded_deferred_tool_still_runs(tmp_path):
    responses = [
        _response(calls=[ToolCall(id="c1", name=MCP_TOOL, arguments={"value": "x"})]),
        _finish("c2"),
        _response(text="Done."),
    ]
    _, service, prepared, provider, broker = _deferred_loop(tmp_path, responses)

    asyncio.run(service.complete(prepared))

    assert MCP_TOOL not in {tool.name for tool in provider.requests[1].tools}
    assert [call.tool_name for call in broker.calls] == [MCP_TOOL]


def test_prepare_defers_by_default_and_ranks_locally(tmp_path, monkeypatch):
    def fail(_):
        raise AssertionError("Jev must not be called")

    service, request = _mcp_service(tmp_path, monkeypatch, lambda: fail(None))
    original = service.tool_platform.chat_components

    def default_scope(**kwargs):
        components = original(**kwargs)
        return RuntimeToolComponents(
            broker=components.broker,
            scope=components.scope.model_copy(update={"tool_suggestions": False}),
            workspace=components.workspace,
            specs=components.specs,
            runtime_digest=components.runtime_digest,
        )

    service.tool_platform.chat_components = default_scope
    index = FakeIndex({MCP_TOOL: 0.9}, state="required")
    service.knowledge_index = index

    prepared = service.prepare(request)

    snapshot = prepared.turn.request_snapshot
    assert snapshot["tool_suggestions"] is None
    # The model was not ready, so ranking fell back to keywords and the
    # model download started in the background.
    assert snapshot["tool_catalog"]["ranker"] == "keyword"
    assert snapshot["tool_catalog"]["deferred"] == [MCP_TOOL]
    assert set(prepared.tool_components.specs) == {
        MCP_TOOL,
        NOTES_TOOL,
        CATALOG_SEARCH,
        CATALOG_LOAD,
        CATALOG_CALL,
    }
    # The selected server was built with every other usable one, and only
    # the other one's tool is on demand.
    assert service.tool_platform.calls == [["notes", "tracker"]]
    assert index.warmed.wait(5)


def test_prepare_sends_only_selected_servers_when_on_demand_loading_is_off(
    tmp_path, monkeypatch
):
    service, request = _mcp_service(
        tmp_path,
        monkeypatch,
        lambda: None,
        scope={"tool_suggestions": False, "on_demand_tools": False},
    )

    prepared = service.prepare(request)

    # Without deferral the other server's tools would land in every request,
    # so it is not offered at all.
    assert service.tool_platform.calls == [["notes"]]
    assert prepared.turn.request_snapshot["tool_catalog"] is None
    assert prepared.turn.request_snapshot["mcp_catalog_snapshot"] == []
    assert set(prepared.tool_components.specs) == {NOTES_TOOL}


def test_on_demand_servers_never_turn_tools_on_for_a_plain_chat(tmp_path, monkeypatch):
    service, request = _mcp_service(tmp_path, monkeypatch, lambda: None, selected=())

    prepared = service.prepare(request)

    assert prepared.tools_enabled is False
    assert service.tool_platform.calls == []


def test_resume_rebuilds_the_on_demand_catalog_from_the_turn(tmp_path, monkeypatch):
    service, request = _mcp_service(
        tmp_path, monkeypatch, lambda: None, scope={"tool_suggestions": False}
    )
    prepared = service.prepare(request)
    turn = service.store.update(
        ChatTurn,
        prepared.turn.id,
        {"status": ChatTurnStatus.WAITING_APPROVAL},
        expected_revision=prepared.turn.revision,
    )
    # Changing the live server after the pause must not change what the
    # paused turn was offered.
    tracker = service.store.get(McpServerProfile, "tracker")
    service.store.update(
        McpServerProfile,
        tracker.id,
        {"enabled": False},
        expected_revision=tracker.revision,
    )

    resumed = service.prepare_resume(turn.id)

    assert service.tool_platform.calls[-1] == ["notes", "tracker"]
    assert {MCP_TOOL, NOTES_TOOL, CATALOG_CALL} <= set(resumed.tool_components.specs)


def test_prepare_keeps_an_always_loaded_tool_in_the_function_list(
    tmp_path, monkeypatch
):
    service, request = _mcp_service(tmp_path, monkeypatch, lambda: None)
    original = service.tool_platform.chat_components

    def pinned(**kwargs):
        components = original(**kwargs)
        return RuntimeToolComponents(
            broker=components.broker,
            scope=components.scope.model_copy(
                update={
                    "tool_suggestions": False,
                    "always_loaded_tools": [MCP_TOOL],
                }
            ),
            workspace=components.workspace,
            specs=components.specs,
            runtime_digest=components.runtime_digest,
        )

    service.tool_platform.chat_components = pinned

    prepared = service.prepare(request)

    # The pin covers the one on-demand tool and the selected server is sent
    # whole, so nothing is left to defer and the catalog tools are omitted.
    assert prepared.turn.request_snapshot["tool_catalog"] is None
    assert set(prepared.tool_components.specs) == {MCP_TOOL, NOTES_TOOL}


def test_scope_update_without_the_field_keeps_on_demand_loading(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
        )
    )
    headers = {"Authorization": "Bearer test-token"}
    engagement = store.create(Engagement(id="eng-scope", name="Scope"))
    url = f"/api/v1/engagements/{engagement.id}/scope"

    created = client.put(url, json={"local_only": False}, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["on_demand_tools"] is True

    off = client.put(url, json={"on_demand_tools": False}, headers=headers)
    assert off.json()["on_demand_tools"] is False
    kept = client.put(url, json={"allowed_domains": ["example.com"]}, headers=headers)
    assert kept.json()["on_demand_tools"] is False


def _scope_client(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
        )
    )
    engagement = store.create(Engagement(id="eng-scope", name="Scope"))
    return store, client, engagement, {"Authorization": "Bearer test-token"}


def test_always_loaded_tools_are_normalized_and_kept_by_older_clients(tmp_path):
    _, client, engagement, headers = _scope_client(tmp_path)
    url = f"/api/v1/engagements/{engagement.id}/scope"

    created = client.put(
        url,
        json={"always_loaded_tools": [f" {MCP_TOOL} ", MCP_TOOL, "", DATABASE_TOOL]},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    assert created.json()["always_loaded_tools"] == sorted([MCP_TOOL, DATABASE_TOOL])

    kept = client.put(url, json={"local_only": True}, headers=headers)
    assert kept.json()["always_loaded_tools"] == sorted([MCP_TOOL, DATABASE_TOOL])
    cleared = client.put(url, json={"always_loaded_tools": []}, headers=headers)
    assert cleared.json()["always_loaded_tools"] == []


def test_catalog_offers_every_other_usable_server_in_name_order(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")

    def server(identifier, name, **fields):
        fields.setdefault("enabled", True)
        return store.create(
            McpServerProfile(
                id=identifier,
                name=name,
                transport="stdio",
                command=f"/usr/bin/{identifier}",
                trusted_stdio=fields["enabled"],
                capabilities=McpCapabilitySnapshot(
                    checked_at=fields.pop("checked_at", utc_now()),
                    tools=[McpToolSnapshot(name="work", description="Does work.")],
                ),
                **fields,
            )
        )

    server("mcp-selected", "selected")
    server("mcp-z", "alpha")
    server("mcp-a", "beta")
    server("mcp-off", "off", enabled=False)
    server("mcp-unprobed", "unprobed", checked_at=None)
    server("mcp-denied", "denied", tool_overrides={"work": McpApprovalMode.DENY})
    server("mcp-trimmed", "trimmed", disabled_tools=["work"])

    catalog = catalog_mcp_profiles(store, exclude={"mcp-selected"})

    # Skipped quietly: nobody chose these, so none of them may fail the turn.
    assert [item.id for item in catalog] == ["mcp-z", "mcp-a"]


def test_tool_candidates_are_the_runtime_names_of_selectable_mcp_tools(tmp_path):
    store, client, engagement, headers = _scope_client(tmp_path)

    def tool(name):
        return McpToolSnapshot(name=name, description=f"  {name}  does work ")

    store.create(
        McpServerProfile(
            id="mcp-tracker",
            name="tracker",
            transport="stdio",
            command="/usr/bin/tracker",
            enabled=True,
            trusted_stdio=True,
            disabled_tools=["delete_everything"],
            tool_overrides={"rotate_password": McpApprovalMode.DENY},
            capabilities=McpCapabilitySnapshot(
                checked_at=utc_now(),
                tools=[
                    tool("search_issues"),
                    tool("delete_everything"),
                    tool("rotate_password"),
                ],
            ),
        )
    )
    store.create(
        McpServerProfile(
            id="mcp-offline",
            name="offline",
            transport="stdio",
            command="/usr/bin/offline",
            enabled=False,
            capabilities=McpCapabilitySnapshot(checked_at=utc_now(), tools=[tool("x")]),
        )
    )
    store.create(
        McpServerProfile(
            id="mcp-unprobed",
            name="unprobed",
            transport="stdio",
            command="/usr/bin/unprobed",
            enabled=True,
            trusted_stdio=True,
        )
    )

    found = client.get(
        f"/api/v1/engagements/{engagement.id}/scope/tool-candidates", headers=headers
    )
    assert found.status_code == 200, found.text
    assert found.json() == [
        {
            "name": mcp_tool_runtime_name("mcp-tracker", "search_issues"),
            "server_id": "mcp-tracker",
            "server_name": "tracker",
            "tool_name": "search_issues",
            "description": "search_issues does work",
        }
    ]
    missing = client.get(
        "/api/v1/engagements/eng-missing/scope/tool-candidates", headers=headers
    )
    assert missing.status_code == 404
