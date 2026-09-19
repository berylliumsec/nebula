import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import nebula.v3.chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.providers import ToolCall
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_suggestions import (
    CATALOG_LOAD,
    CATALOG_SEARCH,
    MAX_CHOICE_TOOLS,
    NONE_OPTION,
    JevClient,
    ToolCatalogBroker,
    build_questions,
    build_state,
    catalog_components,
    loaded_tool_names,
    suggest_tools,
    suggestion_instructions,
    suggestions_enabled,
)
from nebula.v3.tools import InvalidToolArguments, ToolInvocation, ToolSpec
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response

MCP_TOOL = "mcp.tracker.search_issues"


def _spec(name: str, description: str, *, source: str | None = "mcp:tracker"):
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        source_id=source,
    )


def _jev_answers(probabilities: dict[str, float], *, action=0.9, prose=0.1):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "gate_action": {"type": "noul", "noul": action},
            "gate_prose": {"type": "noul", "noul": prose},
            "tools_0": {
                "type": "choice",
                "choice": max(probabilities, key=probabilities.get),
                "probabilities": probabilities,
                "confidence": 0.8,
            },
        },
        "usage": {"input_tokens": 321, "output_tokens": 0},
    }


def _client(handler) -> JevClient:
    return JevClient(api_key="test-key", transport=httpx.MockTransport(handler))


def test_jev_receives_only_redacted_operator_text_and_tool_summaries():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.7, NONE_OPTION: 0.3}))

    deferred = {MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")}
    receipt = asyncio.run(
        suggest_tools(
            _client(handler),
            deferred=deferred,
            operator_messages=[
                "earlier ask",
                "find issues; token Bearer abcdefghijklmnopqrstuvwxyz0123",
            ],
        )
    )

    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["model"] == "jev-latest"
    assert body["state"] == {
        "operator_request": "find issues; token Bearer [REDACTED]",
        "earlier_operator_messages": ["earlier ask"],
    }
    assert body["questions"]["tools_0"]["criteria"] == {
        MCP_TOOL: "Search tracker issues.",
        NONE_OPTION: "None of the listed tools is needed for operator_request.",
    }
    assert receipt.status == "suggested"
    assert receipt.preloaded == [MCP_TOOL]
    assert receipt.deferred == [MCP_TOOL]
    assert receipt.model == "jev-1.13.0"
    assert receipt.input_tokens == 321


def test_low_gate_suggests_nothing_even_with_a_confident_choice():
    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(
                    200,
                    json=_jev_answers({MCP_TOOL: 0.9}, action=0.1, prose=0.9),
                )
            ),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search.")},
            operator_messages=["explain what an MCP server is"],
        )
    )
    assert receipt.status == "no_tool_needed"
    assert receipt.preloaded == [] and receipt.suggested == []


def test_moderate_probability_is_a_hint_without_preloading():
    other = "mcp.tracker.create_issue"
    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(
                    200,
                    json=_jev_answers({MCP_TOOL: 0.3, other: 0.05, NONE_OPTION: 0.65}),
                )
            ),
            deferred={
                MCP_TOOL: _spec(MCP_TOOL, "Search."),
                other: _spec(other, "Create."),
            },
            operator_messages=["look for related bugs"],
        )
    )
    assert receipt.preloaded == []
    assert receipt.suggested == [MCP_TOOL]


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(529, json={"error": "overloaded"}),
        lambda _: httpx.Response(200, json={"answers": {}}),
        lambda request: (_ for _ in ()).throw(
            httpx.ConnectTimeout("timed out", request=request)
        ),
    ],
)
def test_jev_failure_never_raises_and_keeps_the_catalog(handler):
    receipt = asyncio.run(
        suggest_tools(
            _client(handler),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search.")},
            operator_messages=["find issues"],
        )
    )
    assert receipt.status == "unavailable"
    assert receipt.deferred == [MCP_TOOL]
    assert receipt.error


def test_missing_api_key_is_reported_without_a_request(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert JevClient.from_environment() is None
    receipt = asyncio.run(
        suggest_tools(
            None,
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search.")},
            operator_messages=["x"],
        )
    )
    assert receipt.status == "unavailable"
    assert "No TypeSafe key" in (receipt.error or "")


def test_large_catalogs_are_split_under_the_choice_option_limit():
    specs = [_spec(f"mcp.s.tool_{index:03d}", "Does a thing.") for index in range(300)]
    questions = build_questions(specs)
    chunks = [value for key, value in questions.items() if key.startswith("tools_")]
    assert len(chunks) == 2
    assert all(len(item["criteria"]) <= MAX_CHOICE_TOOLS + 1 for item in chunks)
    assert sum(len(item["criteria"]) - 1 for item in chunks) == 300


def test_state_requires_an_operator_message():
    with pytest.raises(Exception, match="no operator message"):
        build_state(["   "])


def test_local_only_scope_never_enables_suggestions():
    assert suggestions_enabled(ScopePolicy(engagement_id="e", tool_suggestions=True))
    assert not suggestions_enabled(
        ScopePolicy(engagement_id="e", tool_suggestions=True, local_only=True)
    )
    assert not suggestions_enabled(ScopePolicy(engagement_id="e"))


def test_catalog_search_and_load_are_bounded_to_deferred_tools(tmp_path):
    broker = ToolCatalogBroker(
        {
            MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues by keyword."),
            "mcp.tracker.close_issue": _spec("mcp.tracker.close_issue", "Close one."),
        }
    )

    def invoke(name, arguments):
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

    found = invoke(CATALOG_SEARCH, {"query": "search issues"})
    assert found["matches"][0]["name"] == MCP_TOOL
    loaded = invoke(CATALOG_LOAD, {"names": [MCP_TOOL]})
    assert loaded["loaded"][0]["input_schema"]["required"] == ["value"]
    with pytest.raises(InvalidToolArguments, match="not on-demand"):
        invoke(CATALOG_LOAD, {"names": ["run_command"]})


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
    second = catalog_components(components, deferred=[MCP_TOOL])
    assert first is not None and second is not None
    assert first.runtime_digest == second.runtime_digest
    assert catalog_components(components, deferred=[]) is None


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
        "status": "suggested",
        "deferred": [MCP_TOOL],
        "preloaded": list(preloaded),
        "suggested": [] if preloaded else [MCP_TOOL],
    }
    catalog = catalog_components(components, deferred=[MCP_TOOL])
    prepared.tool_components = chat_module.combine_tool_components(components, catalog)
    turn = store.update(
        ChatTurn,
        prepared.turn.id,
        {"request_snapshot": {"tool_suggestions": receipt}},
        expected_revision=prepared.turn.revision,
    )
    prepared.turn = turn
    return store, service, prepared, provider, broker


def _tool_names(request):
    return {tool.name for tool in request.tools}


def test_deferred_tool_is_advertised_only_after_it_is_loaded(tmp_path):
    responses = [
        _response(
            calls=[
                ToolCall(id="c1", name=CATALOG_LOAD, arguments={"names": [MCP_TOOL]})
            ]
        ),
        _response(calls=[ToolCall(id="c2", name=MCP_TOOL, arguments={"value": "x"})]),
        _response(calls=[ToolCall(id="c3", name="finish_response", arguments={})]),
        _response(text="Found it."),
    ]
    store, service, prepared, provider, broker = _deferred_loop(tmp_path, responses)

    completion = asyncio.run(service.complete(prepared))

    chip = completion.tool_suggestions
    assert chip["suggested"] == [MCP_TOOL] and chip["used"] == [MCP_TOOL]
    assert chip["unloaded_count"] == 0 and chip["on_demand_count"] == 1
    assert "probabilities" not in chip
    saved = [
        item
        for item in store.list_entities(ChatMessage, limit=100)
        if item.role == ChatRole.ASSISTANT
    ]
    assert saved[-1].metadata["tool_suggestions"] == chip
    first, second = provider.requests[0], provider.requests[1]
    assert MCP_TOOL not in _tool_names(first)
    assert {CATALOG_SEARCH, CATALOG_LOAD, "safe_read"} <= _tool_names(first)
    assert "On-demand tools: 1 tools" in first.instructions
    assert json.dumps({"loaded": [], "not_loaded": [MCP_TOOL]}) in first.instructions
    assert MCP_TOOL in _tool_names(second)
    assert [call.tool_name for call in broker.calls] == [MCP_TOOL]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_preloaded_tool_is_callable_from_the_first_step(tmp_path):
    responses = [
        _response(calls=[ToolCall(id="c1", name=MCP_TOOL, arguments={"value": "x"})]),
        _response(calls=[ToolCall(id="c2", name="finish_response", arguments={})]),
        _response(text="Done."),
    ]
    _, service, prepared, provider, broker = _deferred_loop(
        tmp_path, responses, preloaded=[MCP_TOOL]
    )

    asyncio.run(service.complete(prepared))

    assert MCP_TOOL in _tool_names(provider.requests[0])
    assert len(broker.calls) == 1


def test_direct_call_to_an_unloaded_deferred_tool_still_runs(tmp_path):
    responses = [
        _response(calls=[ToolCall(id="c1", name=MCP_TOOL, arguments={"value": "x"})]),
        _response(calls=[ToolCall(id="c2", name="finish_response", arguments={})]),
        _response(text="Done."),
    ]
    _, service, prepared, provider, broker = _deferred_loop(tmp_path, responses)

    asyncio.run(service.complete(prepared))

    assert MCP_TOOL not in _tool_names(provider.requests[0])
    assert MCP_TOOL in _tool_names(provider.requests[1])
    assert [call.tool_name for call in broker.calls] == [MCP_TOOL]


class _McpPlatform:
    def __init__(self, workspace):
        self.workspace = workspace

    def chat_components(self, *, engagement_id, **_):
        return RuntimeToolComponents(
            broker=RecordingBroker(),
            scope=ScopePolicy(
                id=f"scope:{engagement_id}",
                engagement_id=engagement_id,
                tool_suggestions=True,
            ),
            workspace=self.workspace,
            specs={MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")},
            runtime_digest="mcp-runtime",
        )


def _mcp_service(tmp_path, monkeypatch, client_factory):
    store = NebulaStore(tmp_path / "chat-suggestions.db")
    engagement = store.create(Engagement(id="eng-jev", name="Jev"))
    payload = _profile(local=True).model_dump(mode="python")
    payload["capabilities"]["tool_calling"] = True
    payload["capability_verifications"] = {
        "model-a": {"model": "model-a", "status": "verified"}
    }
    profile = store.create(ProviderProfile.model_validate(payload))
    provider = FakeProvider(profile.id, local=True)
    provider.config.capabilities.tools = True
    provider.config.capabilities.strict_tools = True
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    monkeypatch.setattr(
        chat_module,
        "resolve_mcp_profiles",
        lambda store, ids: (
            SimpleNamespace(id="tracker", model_dump=lambda **_: {"id": "tracker"}),
        ),
    )
    service = ChatService(
        store,
        tool_platform=_McpPlatform(tmp_path),
        tool_suggestion_client=client_factory,
    )
    request = ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id=engagement.id,
        mcp_server_ids=["tracker"],
        messages=[{"role": "user", "content": "find the login bug"}],
        include_knowledge=False,
        stream=True,
    )
    return service, request


def test_prepare_records_the_jev_receipt_and_adds_catalog_tools(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.8}))

    service, request = _mcp_service(tmp_path, monkeypatch, lambda: _client(handler))

    prepared = service.prepare(request)

    assert len(calls) == 1
    assert calls[0]["state"]["operator_request"] == "find the login bug"
    receipt = prepared.turn.request_snapshot["tool_suggestions"]
    assert receipt["status"] == "suggested"
    assert receipt["preloaded"] == [MCP_TOOL]
    assert {CATALOG_SEARCH, CATALOG_LOAD, MCP_TOOL} == set(
        prepared.tool_components.specs
    )
    assert "Likely relevant" in suggestion_instructions(receipt)


def test_prepare_skips_jev_when_the_engagement_has_not_opted_in(tmp_path, monkeypatch):
    def fail(_):
        raise AssertionError("Jev must not be called")

    service, request = _mcp_service(tmp_path, monkeypatch, lambda: _client(fail))
    original = service.tool_platform.chat_components

    def opted_out(**kwargs):
        components = original(**kwargs)
        return RuntimeToolComponents(
            broker=components.broker,
            scope=components.scope.model_copy(update={"tool_suggestions": False}),
            workspace=components.workspace,
            specs=components.specs,
            runtime_digest=components.runtime_digest,
        )

    service.tool_platform.chat_components = opted_out

    prepared = service.prepare(request)

    assert prepared.turn.request_snapshot["tool_suggestions"] is None
    assert set(prepared.tool_components.specs) == {MCP_TOOL}


def test_scope_update_without_the_field_keeps_the_opt_in(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    engagement = store.create(Engagement(id="eng-scope", name="Scope"))
    url = f"/api/v1/engagements/{engagement.id}/scope"

    enabled = client.put(url, json={"tool_suggestions": True}, headers=headers)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["tool_suggestions"] is True

    kept = client.put(
        url,
        json={"local_only": False, "allowed_domains": ["example.com"]},
        headers=headers,
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["tool_suggestions"] is True

    cleared = client.put(url, json={"tool_suggestions": False}, headers=headers)
    assert cleared.json()["tool_suggestions"] is False


@pytest.fixture
def typesafe_api(tmp_path, monkeypatch):
    from nebula.v3.credentials import CredentialStore

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    sent_keys = []

    async def fake_system_one(self, state, questions):
        sent_keys.append(self.api_key)
        if self.api_key == "bad-key":
            raise httpx.ConnectError("refused")
        return {"model": "jev-1.13.0", "answers": {"connection_test": {"noul": 0.9}}}

    monkeypatch.setattr(JevClient, "system_one", fake_system_one)
    store = NebulaStore(tmp_path / "nebula.db")
    credentials = CredentialStore()
    app = create_app(
        store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        auth_token="test-token",
        credential_store=credentials,
    )
    return TestClient(app), store, credentials, sent_keys


HEADERS = {"Authorization": "Bearer test-token"}
TYPESAFE_URL = "/api/v1/integrations/typesafe"


def _save(client, secret):
    return client.put(
        TYPESAFE_URL,
        json={"secret": secret, "persistence": "session"},
        headers=HEADERS,
    )


def test_typesafe_key_is_saved_tested_and_never_returned(typesafe_api):
    from nebula.v3.domain import ToolSuggestionSettings
    from nebula.v3.tool_suggestions import resolve_jev_client

    client, store, credentials, sent_keys = typesafe_api
    empty = client.get(TYPESAFE_URL, headers=HEADERS).json()
    assert empty["source"] is None and empty["available"] is False

    saved = _save(client, "ts-secret-1")
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert "ts-secret-1" not in saved.text and "session:" not in saved.text
    assert body["source"] == "session" and body["available"] is True
    assert body["last_test"]["ok"] is True
    assert body["last_test"]["model"] == "jev-1.13.0"
    assert sent_keys == ["ts-secret-1"]
    reference = store.get(ToolSuggestionSettings, "typesafe").secret_ref
    assert reference.startswith("session:")
    # The generic CRUD API never exposes the settings record.
    crud = client.get("/api/v1/tool-suggestion-settings", headers=HEADERS)
    assert crud.status_code in {404, 405}
    # Chat turns resolve the same saved key.
    assert resolve_jev_client(store, credentials).api_key == "ts-secret-1"


def test_replacing_the_key_discards_the_old_secret(typesafe_api):
    from nebula.v3.domain import ToolSuggestionSettings

    client, store, credentials, _ = typesafe_api
    _save(client, "ts-secret-1")
    first = store.get(ToolSuggestionSettings, "typesafe").secret_ref
    _save(client, "ts-secret-2")
    assert credentials.status(first).available is False
    second = store.get(ToolSuggestionSettings, "typesafe").secret_ref
    assert credentials.resolve(second).get_secret_value() == "ts-secret-2"


def test_failed_test_is_recorded_and_retest_uses_the_saved_key(typesafe_api):
    client, _, _, sent_keys = typesafe_api
    failed = _save(client, "bad-key").json()
    assert failed["available"] is True
    assert failed["last_test"]["ok"] is False
    assert "refused" in failed["last_test"]["error"]
    retest = client.post(TYPESAFE_URL + "/test", headers=HEADERS).json()
    assert retest["last_test"]["ok"] is False
    assert sent_keys == ["bad-key", "bad-key"]


def test_environment_key_is_the_fallback_and_remove_restores_it(
    typesafe_api, monkeypatch
):
    client, _, _, sent_keys = typesafe_api
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    assert client.get(TYPESAFE_URL, headers=HEADERS).json()["source"] == "environment"
    assert client.post(TYPESAFE_URL + "/test", headers=HEADERS).json()["last_test"][
        "ok"
    ]
    _save(client, "ts-secret-1")
    removed = client.delete(TYPESAFE_URL, headers=HEADERS).json()
    assert removed["source"] == "environment"
    assert removed["last_test"] is None
    assert sent_keys == ["env-key", "ts-secret-1"]


def test_projects_using_counts_opted_in_scopes_that_are_not_local_only(typesafe_api):
    client, store, _, _ = typesafe_api
    store.create(ScopePolicy(id="s1", engagement_id="a", tool_suggestions=True))
    store.create(
        ScopePolicy(id="s2", engagement_id="b", tool_suggestions=True, local_only=True)
    )
    store.create(ScopePolicy(id="s3", engagement_id="c"))
    assert client.get(TYPESAFE_URL, headers=HEADERS).json()["projects_using"] == 1
