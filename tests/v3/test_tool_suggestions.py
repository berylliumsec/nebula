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
    Engagement,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    utc_now,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_catalog import (
    CATALOG_CALL,
    CATALOG_LOAD,
    CATALOG_SEARCH,
    catalog_instructions,
)
from nebula.v3.tool_suggestions import (
    MAX_CHOICE_OPTIONS,
    SuggestionCache,
    MAX_SKILL_INSTRUCTION_CHARS,
    MAX_STATE_SKILL_CHARS,
    NONE_OPTION,
    JevClient,
    build_questions,
    build_state,
    mcp_sources,
    suggest_tools,
    suggestions_enabled,
)
from nebula.v3.tools import ToolSpec
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_chat_tool_loop import RecordingBroker

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


def _choice(probabilities: dict[str, float]) -> dict:
    return {
        "type": "choice",
        "choice": max(probabilities, key=probabilities.get),
        "probabilities": probabilities,
        "confidence": 0.8,
    }


def _jev_answers(
    probabilities: dict[str, float], *, sources: dict[str, float] | None = None
):
    answers = {"tools_0": _choice(probabilities)}
    if sources:
        answers["sources_0"] = _choice(sources)
    return {
        "model": "jev-1.13.0",
        "answers": answers,
        "usage": {"input_tokens": 321, "output_tokens": 0},
    }


def _mcp_profile(identifier: str, name: str, *, instructions=None, tools=()):
    return SimpleNamespace(
        id=identifier,
        name=name,
        capabilities=SimpleNamespace(
            instructions=instructions,
            tools=[SimpleNamespace(name=item) for item in tools],
        ),
    )


def _client(handler) -> JevClient:
    return JevClient(api_key="test-key", transport=httpx.MockTransport(handler))


def _skill(name: str, instructions: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, instructions=instructions)


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
    # No source was described, so the source stands on its tool names alone.
    assert body["questions"]["sources_0"]["criteria"] == {
        "mcp:tracker": f"mcp:tracker. Tools: {MCP_TOOL}",
        NONE_OPTION: "None of the listed sources can help with operator_request.",
    }
    assert not any(key.startswith("gate") for key in body["questions"])
    assert receipt.status == "suggested"
    assert receipt.preloaded == [MCP_TOOL]
    assert receipt.deferred == [MCP_TOOL]
    assert receipt.model == "jev-1.13.0"
    assert receipt.input_tokens == 321


def test_none_of_these_keeps_a_confident_tool_out_of_the_prompt():
    """ "None of these" replaces the old "does this need a tool at all" gate."""

    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(
                    200, json=_jev_answers({MCP_TOOL: 0.55, NONE_OPTION: 0.45})
                )
            ),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search.")},
            operator_messages=["find issues"],
        )
    )
    # It cleared PRELOAD_THRESHOLD and beat the none option, so its schema rides
    # along with the turn.
    assert receipt.preloaded == [MCP_TOOL] and receipt.suggested == []

    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(
                    200, json=_jev_answers({MCP_TOOL: 0.55, NONE_OPTION: 0.6})
                )
            ),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search.")},
            operator_messages=["explain what an MCP server is"],
        )
    )
    # Same probability, but Jev rated "none of these" higher: it stays a hint
    # rather than spending prompt tokens on the schema.
    assert receipt.preloaded == [] and receipt.suggested == [MCP_TOOL]


def test_nothing_clearing_the_threshold_is_recorded_as_no_tool_needed():
    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(
                    200, json=_jev_answers({MCP_TOOL: 0.05, NONE_OPTION: 0.95})
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


def test_servers_are_described_by_their_handshake_instructions():
    sources = mcp_sources(
        [
            _mcp_profile(
                "tracker",
                "issue-tracker",
                instructions="  Issue tracker.\n  Search, file and close bugs. ",
            ),
            _mcp_profile("vault", "secrets", tools=["read_secret", "rotate"]),
        ]
    )

    assert sources["mcp:tracker"].description == (
        "Issue tracker. Search, file and close bugs."
    )
    # An McpServerProfile has no description field, so a server that sent no
    # handshake instructions is described by the tools it exposes.
    assert sources["mcp:vault"].description == ""

    questions = build_questions(
        [
            _spec(MCP_TOOL, "Search tracker issues."),
            _spec("mcp.vault.read_secret", "Read a secret.", source="mcp:vault"),
        ],
        sources=sources,
    )

    assert questions["sources_0"]["criteria"] == {
        "mcp:tracker": "issue-tracker. Issue tracker. Search, file and close bugs.",
        "mcp:vault": "secrets. Tools: mcp.vault.read_secret",
        NONE_OPTION: "None of the listed sources can help with operator_request.",
    }


def test_the_server_ranking_decides_between_equally_rated_tools():
    tracker_tool, vault_tool = MCP_TOOL, "mcp.vault.read_secret"
    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(
                    200,
                    json=_jev_answers(
                        {tracker_tool: 0.6, vault_tool: 0.6},
                        sources={"mcp:tracker": 0.9, "mcp:vault": 0.05},
                    ),
                )
            ),
            deferred={
                tracker_tool: _spec(tracker_tool, "Search issues."),
                vault_tool: _spec(vault_tool, "Read a secret.", source="mcp:vault"),
            },
            operator_messages=["find the login bug"],
            sources=mcp_sources(
                [
                    _mcp_profile("tracker", "issue-tracker", instructions="Issues."),
                    _mcp_profile("vault", "secrets", instructions="Secrets."),
                ]
            ),
        )
    )

    # Same tool probability: the source's rank is what preloads one schema and
    # leaves the other as a hint.
    assert receipt.preloaded == [tracker_tool]
    assert receipt.suggested == [vault_tool]
    assert receipt.sources == ["issue-tracker"]
    assert receipt.source_probabilities == {"mcp:tracker": 0.9, "mcp:vault": 0.05}


def test_only_the_top_three_tools_survive():
    probabilities = {
        f"mcp.tracker.tool_{index}": 0.6 - index / 20 for index in range(6)
    }
    receipt = asyncio.run(
        suggest_tools(
            _client(
                lambda _: httpx.Response(200, json=_jev_answers(dict(probabilities)))
            ),
            deferred={name: _spec(name, "Does a thing.") for name in probabilities},
            operator_messages=["work the queue"],
        )
    )

    picks = [*receipt.preloaded, *receipt.suggested]
    assert picks == sorted(probabilities, key=lambda name: -probabilities[name])[:3]
    # Three picks at most, of which at most two carry their schema.
    assert receipt.preloaded == ["mcp.tracker.tool_0", "mcp.tracker.tool_1"]
    assert receipt.suggested == ["mcp.tracker.tool_2"]
    assert len(receipt.probabilities) == len(probabilities)


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
            skills=[_skill("triage", "Search the tracker first.")],
        )
    )
    assert receipt.status == "unavailable"
    assert receipt.deferred == [MCP_TOOL]
    assert receipt.error
    # The request was built, so the receipt still names what left the host.
    assert receipt.skills == ["triage"]


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
    assert all(len(item["criteria"]) <= MAX_CHOICE_OPTIONS + 1 for item in chunks)
    assert sum(len(item["criteria"]) - 1 for item in chunks) == 300


def test_state_requires_an_operator_message():
    with pytest.raises(Exception, match="no operator message"):
        build_state(["   "])


def test_selected_skill_instructions_reach_jev_redacted_and_bounded():
    state = build_state(
        ["triage the finding"],
        [
            _skill("recon", "x" * (MAX_SKILL_INSTRUCTION_CHARS + 500)),
            _skill("blank", "   "),
            _skill("triage", "Check the tracker first. api_key: hunter2hunter2"),
        ],
    )

    skills = state["operator_selected_skills"]
    assert [item["name"] for item in skills] == ["recon", "triage"]
    assert len(skills[0]["instructions"]) == MAX_SKILL_INSTRUCTION_CHARS
    assert skills[1]["instructions"] == "Check the tracker first. api_key: [REDACTED]"


def test_state_keeps_the_newest_skills_inside_the_window():
    state = build_state(
        ["go"],
        [
            _skill(f"skill-{index}", "y" * MAX_SKILL_INSTRUCTION_CHARS)
            for index in range(5)
        ],
    )

    skills = state["operator_selected_skills"]
    assert [item["name"] for item in skills] == ["skill-3", "skill-4"]
    assert sum(len(item["instructions"]) for item in skills) <= MAX_STATE_SKILL_CHARS


def test_state_omits_the_skill_key_when_none_is_selected():
    assert "operator_selected_skills" not in build_state(["go"])


def test_questions_name_the_skills_only_when_state_carries_them():
    specs = [_spec(MCP_TOOL, "Search tracker issues.")]

    plain = build_questions(specs)
    assert "operator_selected_skills" not in json.dumps(plain)

    with_skills = build_questions(specs, with_skills=True)
    assert all(
        "operator_selected_skills" in question["instructions"]
        for question in with_skills.values()
    )
    assert with_skills["tools_0"]["criteria"] == plain["tools_0"]["criteria"]


def test_receipt_records_the_skills_that_were_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.7, NONE_OPTION: 0.3}))

    receipt = asyncio.run(
        suggest_tools(
            _client(handler),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")},
            operator_messages=["work the queue"],
            skills=[_skill("triage", "Search the tracker before anything else.")],
        )
    )

    assert seen["body"]["state"]["operator_selected_skills"] == [
        {"name": "triage", "instructions": "Search the tracker before anything else."}
    ]
    assert receipt.skills == ["triage"]


def _counting_client(counter, probabilities=None, sources=None):
    def handler(_):
        counter.append(1)
        return httpx.Response(
            200, json=_jev_answers(probabilities or {MCP_TOOL: 0.8}, sources=sources)
        )

    return _client(handler)


def _ask(cache, counter, *, deferred=None, message="find the login bug"):
    return asyncio.run(
        suggest_tools(
            _counting_client(counter),
            deferred=deferred or {MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")},
            operator_messages=[message],
            cache=cache,
        )
    )


def test_an_identical_request_is_answered_from_the_cache():
    cache, counter = SuggestionCache(), []

    first = _ask(cache, counter)
    second = _ask(cache, counter)

    assert len(counter) == 1
    assert not first.cached and second.cached
    assert second.preloaded == first.preloaded == [MCP_TOOL]
    # The turn that reused the answer spent nothing upstream.
    assert first.input_tokens == 321 and second.input_tokens is None


def test_a_changed_catalog_or_message_asks_again():
    cache, counter = SuggestionCache(), []
    _ask(cache, counter)

    _ask(cache, counter, message="rotate the database password")
    assert len(counter) == 2

    _ask(
        cache,
        counter,
        deferred={
            MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues."),
            "mcp.vault.rotate": _spec(
                "mcp.vault.rotate", "Rotate.", source="mcp:vault"
            ),
        },
    )
    assert len(counter) == 3

    # A server re-probed into a new description changes the source criterion,
    # so the ranking it produced is not reused either.
    asyncio.run(
        suggest_tools(
            _counting_client(counter),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")},
            operator_messages=["find the login bug"],
            sources=mcp_sources(
                [_mcp_profile("tracker", "issue-tracker", instructions="Issues.")]
            ),
            cache=cache,
        )
    )
    assert len(counter) == 4


def test_a_failed_call_is_never_cached():
    cache, counter = SuggestionCache(), []
    failing = asyncio.run(
        suggest_tools(
            _client(lambda _: httpx.Response(529, json={"error": "overloaded"})),
            deferred={MCP_TOOL: _spec(MCP_TOOL, "Search tracker issues.")},
            operator_messages=["find the login bug"],
            cache=cache,
        )
    )

    assert failing.status == "unavailable" and len(cache) == 0
    assert not _ask(cache, counter).cached and len(counter) == 1


def test_the_cache_keeps_the_newest_entries_only():
    cache, counter = SuggestionCache(max_entries=2), []
    for index in range(3):
        _ask(cache, counter, message=f"question {index}")

    assert len(cache) == 2 and len(counter) == 3
    # The oldest request was evicted, so asking it again calls out.
    assert not _ask(cache, counter, message="question 0").cached
    assert _ask(cache, counter, message="question 2").cached


def test_local_only_scope_never_enables_suggestions():
    assert suggestions_enabled(ScopePolicy(engagement_id="e", tool_suggestions=True))
    assert not suggestions_enabled(
        ScopePolicy(engagement_id="e", tool_suggestions=True, local_only=True)
    )
    assert not suggestions_enabled(ScopePolicy(engagement_id="e"))


NOTES_TOOL = "mcp.notes.read_note"


class _McpPlatform:
    """Builds one spec per usable tool of the servers it is handed.

    The scope is the project's stored one when it has one, else a Jev opt-in,
    so a test can turn on-demand loading off where the chat service reads it.
    """

    def __init__(self, workspace, store):
        self.workspace = workspace
        self.store = store
        self.calls: list[list[str]] = []

    def chat_components(self, *, engagement_id, mcp_profiles=(), **_):
        self.calls.append([profile.id for profile in mcp_profiles])
        engagement = self.store.get(Engagement, engagement_id)
        scope = (
            self.store.get(ScopePolicy, engagement.scope_policy_id)
            if engagement.scope_policy_id
            else ScopePolicy(
                id=f"scope:{engagement_id}",
                engagement_id=engagement_id,
                tool_suggestions=True,
            )
        )
        return RuntimeToolComponents(
            broker=RecordingBroker(),
            scope=scope,
            workspace=self.workspace,
            specs={
                f"mcp.{profile.id}.{tool.name}": _spec(
                    f"mcp.{profile.id}.{tool.name}",
                    tool.description,
                    source=f"mcp:{profile.id}",
                )
                for profile in mcp_profiles
                for tool in profile.capabilities.tools
            },
            runtime_digest="mcp-runtime",
        )


def _server(identifier, name, tool, description, *, instructions=None, **fields):
    return McpServerProfile(
        id=identifier,
        name=name,
        transport="stdio",
        command=f"/usr/bin/{identifier}",
        enabled=True,
        trusted_stdio=True,
        capabilities=McpCapabilitySnapshot(
            checked_at=utc_now(),
            instructions=instructions,
            tools=[McpToolSnapshot(name=tool, description=description)],
        ),
        **fields,
    )


def _mcp_service(
    tmp_path,
    monkeypatch,
    client_factory,
    skill: str | None = None,
    *,
    selected=("notes",),
    scope: dict | None = None,
):
    """A project with two probed servers: ``notes`` is selected for the chat,
    ``tracker`` (MCP_TOOL) is only offered on demand."""

    store = NebulaStore(tmp_path / "chat-suggestions.db")
    if scope is not None:
        store.create(ScopePolicy(id="scope-jev", engagement_id="eng-jev", **scope))
    engagement = store.create(
        Engagement(
            id="eng-jev",
            name="Jev",
            scope_policy_id="scope-jev" if scope is not None else None,
        )
    )
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
    store.create(_server("notes", "notes", "read_note", "Read a saved note."))
    store.create(
        _server(
            "tracker",
            "issue-tracker",
            "search_issues",
            "Search tracker issues.",
            instructions="Issue tracker for this codebase.",
        )
    )
    service = ChatService(
        store,
        tool_platform=_McpPlatform(tmp_path, store),
        tool_suggestion_client=client_factory,
        workspace_resolver=lambda _: tmp_path,
    )
    selection = None
    if skill is not None:
        entrypoint = tmp_path / ".agents" / "skills" / "triage" / "SKILL.md"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text(skill, encoding="utf-8")
        selection = {"name": "triage", "path": str(entrypoint.resolve())}
    request = ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id=engagement.id,
        mcp_server_ids=list(selected),
        skill=selection,
        messages=[{"role": "user", "content": "find the login bug"}],
        include_knowledge=False,
        stream=True,
    )
    return service, request


def test_prepare_records_the_jev_receipt_and_adds_catalog_tools(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_jev_answers({MCP_TOOL: 0.8}, sources={"mcp:tracker": 0.9}),
        )

    service, request = _mcp_service(tmp_path, monkeypatch, lambda: _client(handler))

    prepared = service.prepare(request)

    assert len(calls) == 1
    assert calls[0]["state"]["operator_request"] == "find the login bug"
    # The on-demand servers are described to Jev and ranked alongside their
    # tools; the selected one is already loaded, so Jev never sees it.
    criteria = calls[0]["questions"]["sources_0"]["criteria"]
    assert set(criteria) == {"mcp:tracker", NONE_OPTION}
    assert criteria["mcp:tracker"] == "issue-tracker. Issue tracker for this codebase."
    assert NOTES_TOOL not in json.dumps(calls[0]["questions"])
    receipt = prepared.turn.request_snapshot["tool_suggestions"]
    assert receipt["status"] == "suggested"
    assert receipt["preloaded"] == [MCP_TOOL]
    assert receipt["sources"] == ["issue-tracker"]
    catalog = prepared.turn.request_snapshot["tool_catalog"]
    assert catalog["ranker"] == "jev" and catalog["preloaded"] == [MCP_TOOL]
    assert catalog["source_hints"] == ["issue-tracker"]
    assert {CATALOG_SEARCH, CATALOG_LOAD, CATALOG_CALL, MCP_TOOL, NOTES_TOOL} == set(
        prepared.tool_components.specs
    )
    snapshot = prepared.turn.request_snapshot
    assert snapshot["mcp_server_ids"] == ["notes"]
    assert [item["id"] for item in snapshot["mcp_catalog_snapshot"]] == ["tracker"]
    instructions = catalog_instructions(catalog, prepared.tool_components.specs)
    assert "Already loaded" in instructions
    assert '"issue-tracker"' in instructions
    # The ranking is kept, so a retry of this request would not ask again.
    assert len(service.suggestion_cache) == 1


def test_prepare_sends_the_selected_skill_instructions(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_jev_answers({MCP_TOOL: 0.8}))

    service, request = _mcp_service(
        tmp_path,
        monkeypatch,
        lambda: _client(handler),
        skill="Search the tracker for duplicates before filing anything.",
    )

    prepared = service.prepare(request)

    assert calls[0]["state"]["operator_selected_skills"] == [
        {
            "name": "triage",
            "instructions": "Search the tracker for duplicates before filing anything.",
        }
    ]
    assert calls[0]["questions"]["tools_0"]["instructions"].endswith(
        "part of fulfilling operator_request."
    )
    assert prepared.turn.request_snapshot["tool_suggestions"]["skills"] == ["triage"]


def test_unavailable_jev_falls_back_to_local_ranking(tmp_path, monkeypatch):
    def handler(_):
        return httpx.Response(529, json={"error": "overloaded"})

    service, request = _mcp_service(tmp_path, monkeypatch, lambda: _client(handler))

    prepared = service.prepare(request)

    snapshot = prepared.turn.request_snapshot
    assert snapshot["tool_suggestions"]["status"] == "unavailable"
    assert snapshot["tool_catalog"]["ranker"] == "keyword"
    assert snapshot["tool_catalog"]["deferred"] == [MCP_TOOL]


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

    # Deferral is on by default; only the Jev call is skipped.
    assert prepared.turn.request_snapshot["tool_suggestions"] is None
    assert prepared.turn.request_snapshot["tool_catalog"]["ranker"] == "keyword"


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
