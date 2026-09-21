"""Contract, gating and containment for the local web.search runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3 import web_search
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    RiskClass,
    ScopePolicy,
    WebSearchEngine,
    WebSearchRuntimeState,
    WebSearchSettings,
)
from nebula.v3.policy import PolicyEngine, PolicyEffect, PolicyRequest
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_results import WebResultObservation
from nebula.v3.tools import (
    WEB_SEARCH_TOOL_NAME,
    ToolExecutionResult,
    ToolInvocation,
    _compact_tool_observations,
)
from nebula.v3.web_search import (
    CONTAINER_NAME,
    SETTINGS_ID,
    SearchRuntime,
    WebSearchError,
    WebSearchResult,
    WebSearchTool,
    normalize_results,
    scope_disclosure_conflict,
    settings_document,
    web_search_enabled,
    web_search_spec,
)


def _scope(**changes) -> ScopePolicy:
    return ScopePolicy(id="scope-1", engagement_id="project-1", **changes)


def _invocation(**arguments) -> ToolInvocation:
    return ToolInvocation(
        engagement_id="project-1",
        run_id="run-1",
        tool_name=WEB_SEARCH_TOOL_NAME,
        arguments=arguments,
        workspace=Path("/tmp"),
    )


class _StubRuntime:
    def __init__(self, results=None, error: str | None = None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[tuple[str, int, str]] = []

    async def search(self, query, *, count=5, freshness="any"):
        self.calls.append((query, count, freshness))
        if self.error is not None:
            raise WebSearchError(self.error)
        return self.results


# -- contract --------------------------------------------------------------


def test_spec_declares_egress_without_an_engagement_target():
    spec = web_search_spec()
    assert spec.name == "web.search"
    # A network-capable risk class would force a target_argument that the
    # policy engine then checks against engagement scope, denying every search.
    assert spec.risk_class == RiskClass.LOCAL_READ
    assert spec.network_access is False
    assert spec.target_argument is None
    # cloud_transfer is the declaration the policy engine actually enforces.
    assert spec.cloud_transfer is True
    assert spec.budget_class == "execution"
    assert spec.source_id is None, "web.search must stay in the fixed tools array"


def test_policy_allows_research_and_denies_it_for_local_only_projects():
    spec = web_search_spec()
    engine = PolicyEngine()

    def decide(scope: ScopePolicy) -> PolicyEffect:
        return engine.evaluate(
            scope,
            PolicyRequest(
                tool_name=spec.name,
                risk_class=spec.risk_class,
                cloud_transfer=spec.cloud_transfer,
            ),
        ).effect

    assert decide(_scope()) == PolicyEffect.ALLOW
    assert decide(_scope(local_only=True)) == PolicyEffect.DENY
    assert decide(_scope(prohibited_actions=["web.search"])) == PolicyEffect.DENY


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, False),
        ({"web_search": True}, True),
        ({"web_search": True, "local_only": True}, False),
        ({"local_only": True}, False),
    ],
)
def test_web_search_enabled_requires_opt_in_and_forbids_local_only(changes, expected):
    assert web_search_enabled(_scope(**changes)) is expected


# -- scope disclosure guard ------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("acme-client.com admin portal", "acme-client.com"),
        ("vpn gateway corp.example", "corp.example"),
        ("subdomain enumeration www.acme-client.com", "acme-client.com"),
        ("what changed in 10.1.2.3 last week", "10.1.2.3"),
        ("CVE-2024-3400 proof of concept", None),
        ("public resolver 8.8.8.8 behaviour", None),
        ("acme-client.community garden", None),
    ],
)
def test_guard_names_the_in_scope_token_a_query_would_disclose(query, expected):
    scope = _scope(
        allowed_domains=["acme-client.com", "*.corp.example"],
        allowed_cidrs=["10.1.0.0/16"],
    )
    assert scope_disclosure_conflict(query, scope) == expected


def test_guard_steps_aside_once_the_project_opts_into_disclosure():
    scope = _scope(allowed_domains=["acme-client.com"], web_search_discloses_scope=True)
    assert scope_disclosure_conflict("acme-client.com portal", scope) is None


def test_guard_reads_hosts_out_of_allowed_urls():
    scope = _scope(allowed_urls=["https://portal.acme-client.com/login"])
    assert (
        scope_disclosure_conflict("portal.acme-client.com outage", scope)
        == "portal.acme-client.com"
    )


# -- result normalization --------------------------------------------------


def test_normalize_bounds_dedupes_and_redacts_results():
    document = {
        "results": [
            {
                "url": "https://example.com/a",
                "title": "First",
                "content": "leaked Bearer abcdefghijklmnopqrst here",
                "engines": ["duckduckgo"],
                "publishedDate": "2026-01-02",
            },
            {"url": "https://example.com/a", "title": "Duplicate"},
            {"url": "ftp://example.com/c", "title": "Wrong scheme"},
            {"url": "https://example.com/b", "title": "Second", "content": "plain"},
        ]
    }
    results = normalize_results(document, count=5)
    assert [item.rank for item in results] == [1, 2]
    assert [item.url for item in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert "Bearer [REDACTED]" in results[0].snippet
    assert "abcdefghijklmnopqrst" not in results[0].snippet
    assert results[0].engine == "duckduckgo"
    assert results[0].published_at == "2026-01-02"


def test_normalize_honours_the_requested_count():
    document = {
        "results": [
            {"url": f"https://example.com/{index}", "title": f"Item {index}"}
            for index in range(10)
        ]
    }
    assert len(normalize_results(document, count=3)) == 3


def test_normalize_rejects_a_payload_that_is_not_an_object():
    with pytest.raises(WebSearchError):
        normalize_results(["not", "an", "object"], count=3)


# -- receipt observations --------------------------------------------------


def test_results_reach_the_receipt_as_bounded_web_result_observations():
    result = ToolExecutionResult(
        output={
            "tool": WEB_SEARCH_TOOL_NAME,
            "results": [
                {
                    "rank": 1,
                    "title": "Advisory",
                    "url": "https://vendor.example/advisory",
                    "snippet": "details",
                    "engine": "brave",
                    "published_at": "2026-02-02",
                }
            ],
        },
        exit_code=0,
    )
    observations = _compact_tool_observations(result)
    assert len(observations) == 1
    assert isinstance(observations[0], WebResultObservation)
    assert observations[0].kind == "web_result"
    assert observations[0].url == "https://vendor.example/advisory"


def test_the_receipt_summary_carries_a_refusal_into_the_transcript():
    from nebula.v3.tools import _compact_tool_summary
    from nebula.v3.tool_results import ToolResultStatus

    runtime = _StubRuntime()
    scope = _scope(web_search=True, allowed_domains=["acme-client.com"])
    result = asyncio.run(
        WebSearchTool(runtime, scope).execute(
            _invocation(query="acme-client.com vpn"), None
        )
    )
    summary = _compact_tool_summary(result, ToolResultStatus.FAILED)
    assert summary is not None
    assert "acme-client.com" in summary
    assert "Project > Policy" in summary


def test_the_receipt_summary_counts_a_successful_search():
    from nebula.v3.tools import _compact_tool_summary
    from nebula.v3.tool_results import ToolResultStatus

    runtime = _StubRuntime(
        results=[
            WebSearchResult(rank=1, title="A", url="https://a.example"),
            WebSearchResult(rank=2, title="B", url="https://b.example"),
        ]
    )
    result = asyncio.run(
        WebSearchTool(runtime, _scope(web_search=True)).execute(
            _invocation(query="tls handshake"), None
        )
    )
    summary = _compact_tool_summary(result, ToolResultStatus.COMPLETED)
    assert summary == "Found 2 results for \u201ctls handshake\u201d."


def test_other_tools_keep_an_empty_receipt_summary():
    from nebula.v3.tools import _compact_tool_summary
    from nebula.v3.tool_results import ToolResultStatus

    result = ToolExecutionResult(output={"tool": "nmap"}, exit_code=0)
    assert _compact_tool_summary(result, ToolResultStatus.COMPLETED) is None


def test_a_failed_search_contributes_no_observations():
    result = ToolExecutionResult(
        output={"tool": WEB_SEARCH_TOOL_NAME, "results": []}, exit_code=1
    )
    assert _compact_tool_observations(result) == []


def test_nmap_observations_still_work_after_the_union_change():
    result = ToolExecutionResult(
        output={"tool": "nmap"},
        stdout="22/tcp open ssh\n443/tcp open https\n",
        exit_code=0,
    )
    observations = _compact_tool_observations(result)
    assert [item.port for item in observations] == [22, 443]
    assert {item.kind for item in observations} == {"network_port"}


# -- tool execution --------------------------------------------------------


def test_execute_returns_ranked_results_and_a_redacted_query():
    runtime = _StubRuntime(
        results=[
            WebSearchResult(
                rank=1, title="Doc", url="https://example.com", snippet="body"
            )
        ]
    )
    tool = WebSearchTool(runtime, _scope(web_search=True))
    result = asyncio.run(
        tool.execute(_invocation(query="tls 1.3 handshake", count=3), None)
    )
    assert result.exit_code == 0
    assert result.output["tool"] == WEB_SEARCH_TOOL_NAME
    assert result.output["result_count"] == 1
    assert result.output["results"][0]["url"] == "https://example.com"
    assert runtime.calls == [("tls 1.3 handshake", 3, "any")]


def test_execute_refuses_a_scope_disclosure_before_any_egress():
    runtime = _StubRuntime()
    scope = _scope(web_search=True, allowed_domains=["acme-client.com"])
    tool = WebSearchTool(runtime, scope)
    result = asyncio.run(tool.execute(_invocation(query="acme-client.com vpn"), None))
    assert result.exit_code == 1
    assert runtime.calls == [], "the query must not reach upstream engines"
    assert "acme-client.com" in result.stderr
    # The refusal names the setting that would permit it.
    assert "Allow queries naming in-scope targets" in result.stderr


def test_execute_turns_a_runtime_failure_into_a_readable_result(monkeypatch):
    runtime = _StubRuntime(error="the search runtime is not installed")
    tool = WebSearchTool(runtime, _scope(web_search=True))
    recorded = []
    monkeypatch.setattr(
        web_search,
        "record_caught_exception",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    result = asyncio.run(tool.execute(_invocation(query="anything"), None))
    assert result.exit_code == 1
    assert "not installed" in result.stderr
    assert recorded[0][0][:3] == (
        "knowledge",
        "knowledge.web_search.failed",
        "Public web search could not complete.",
    )
    assert recorded[0][1] == {
        "stage": "search",
        "metadata": {"operation": "web_search"},
    }


# -- runtime configuration -------------------------------------------------


def test_generated_settings_enable_json_and_disable_the_bot_limiter():
    document = settings_document(
        [WebSearchEngine.DUCKDUCKGO, WebSearchEngine.BRAVE],
        secret_key="s" * 64,
        port=24_800,
    )
    assert "formats:\n    - json" in document
    assert "limiter: false" in document
    assert "- name: duckduckgo" in document
    assert "- name: brave" in document


def test_the_runtime_is_published_to_loopback_only(tmp_path):
    runtime = SearchRuntime(store=NebulaStore(tmp_path / "n.db"), data_root=tmp_path)
    argv = runtime._run_argv(
        image="docker.io/searxng/searxng@sha256:" + "0" * 64,
        port=24_800,
        configuration=tmp_path / "settings.yml",
    )
    publish = [item for item in argv if item.startswith("--publish=")]
    assert publish == ["--publish=127.0.0.1:24800:8080"]
    assert not any("0.0.0.0" in item for item in argv)
    assert f"--name={CONTAINER_NAME}" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv


def test_configuration_is_written_with_a_stable_private_secret(tmp_path):
    runtime = SearchRuntime(store=NebulaStore(tmp_path / "n.db"), data_root=tmp_path)
    first = runtime._write_configuration([WebSearchEngine.DUCKDUCKGO], 24_800)
    secret = (tmp_path / "search" / "secret").read_text(encoding="utf-8")
    runtime._write_configuration([WebSearchEngine.BRAVE], 24_800)
    assert (tmp_path / "search" / "secret").read_text(encoding="utf-8") == secret
    assert first.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "search" / "secret").stat().st_mode & 0o777 == 0o600


# -- api -------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(store, artifact_store=artifacts, auth_token="test-token")
    return TestClient(app), store


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_status_reports_an_unconfigured_runtime_without_failing(api):
    client, _ = api
    response = client.get("/api/v1/integrations/web-search", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["runtime_available"] is False
    assert body["container_state"] == WebSearchRuntimeState.ABSENT.value
    assert body["projects_using"] == 0


def test_status_counts_only_projects_that_can_actually_search(api):
    client, store = api
    store.create(WebSearchSettings(id=SETTINGS_ID))
    for index, changes in enumerate(
        [
            {"web_search": True},
            {"web_search": True, "local_only": True},
            {"web_search": False},
        ]
    ):
        store.create(
            ScopePolicy(
                id=f"scope-{index}", engagement_id=f"project-{index}", **changes
            )
        )
    body = client.get("/api/v1/integrations/web-search", headers=_auth()).json()
    assert body["projects_using"] == 1


def test_scope_update_round_trips_both_web_search_fields(api, tmp_path):
    client, store = api
    store.create(
        Engagement(
            id="project-1",
            name="Project",
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    response = client.put(
        "/api/v1/engagements/project-1/scope",
        headers=_auth(),
        json={"web_search": True, "web_search_discloses_scope": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["web_search"] is True
    assert body["web_search_discloses_scope"] is True


def test_scope_update_leaves_web_search_untouched_when_the_client_omits_it(
    api, tmp_path
):
    client, store = api
    store.create(
        Engagement(
            id="project-1",
            name="Project",
            workspace_path=str(tmp_path / "workspace"),
        )
    )
    client.put(
        "/api/v1/engagements/project-1/scope",
        headers=_auth(),
        json={"web_search": True},
    )
    # An older client that has never heard of the field must not clear it.
    body = client.put(
        "/api/v1/engagements/project-1/scope",
        headers=_auth(),
        json={"allowed_domains": ["example.com"]},
    ).json()
    assert body["web_search"] is True


# -- registration ----------------------------------------------------------


def _platform(tmp_path, store):
    from nebula.v3.runtime_platform import RuntimePlatform

    return RuntimePlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core",
    )


def _project(store, tmp_path, **scope_changes):
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    store.create(ScopePolicy(id="scope-1", engagement_id="project-1", **scope_changes))
    store.create(
        Engagement(
            id="project-1",
            name="Project",
            workspace_path=str(tmp_path / "workspace"),
            scope_policy_id="scope-1",
        )
    )


def test_opting_in_registers_web_search_with_no_other_tool_source(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    _project(store, tmp_path, web_search=True)
    components = _platform(tmp_path, store).chat_components(
        engagement_id="project-1",
        turn_id="turn-1",
        provider=None,
        model="m",
    )
    assert WEB_SEARCH_TOOL_NAME in components.specs


def test_an_opted_out_project_never_sees_the_tool(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    _project(store, tmp_path)
    components = _platform(tmp_path, store).chat_components(
        engagement_id="project-1",
        turn_id="turn-1",
        provider=None,
        model="m",
        allow_empty=True,
    )
    assert WEB_SEARCH_TOOL_NAME not in components.specs


def test_a_local_only_project_never_sees_the_tool(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    _project(store, tmp_path, web_search=True, local_only=True)
    components = _platform(tmp_path, store).chat_components(
        engagement_id="project-1",
        turn_id="turn-1",
        provider=None,
        model="m",
        allow_empty=True,
    )
    assert WEB_SEARCH_TOOL_NAME not in components.specs


def test_selecting_nothing_at_all_still_reports_no_tool_source(tmp_path):
    from nebula.v3.runtime_platform import RuntimePlatformError

    store = NebulaStore(tmp_path / "nebula.db")
    _project(store, tmp_path)
    with pytest.raises(RuntimePlatformError, match="no tool source"):
        _platform(tmp_path, store).chat_components(
            engagement_id="project-1",
            turn_id="turn-1",
            provider=None,
            model="m",
        )


def test_chat_treats_the_project_opt_in_as_a_tool_source(tmp_path):
    """The chat turn must build tool components for search alone.

    Without this the tool is registered by the platform but never reached,
    because chat only asks the platform when an MCP server or SSH environment
    is selected.
    """

    from nebula.v3.chat import ChatService

    store = NebulaStore(tmp_path / "nebula.db")
    _project(store, tmp_path, web_search=True)
    platform = _platform(tmp_path, store)

    service = ChatService(store, tool_platform=platform)
    assert service._web_search_selected("project-1") is True
    assert service._web_search_selected(None) is False
    assert service._web_search_selected("missing-project") is False

    # No platform means no runtime to register the tool into.
    assert ChatService(store)._web_search_selected("project-1") is False


def test_chat_ignores_the_opt_in_for_a_local_only_project(tmp_path):
    from nebula.v3.chat import ChatService

    store = NebulaStore(tmp_path / "nebula.db")
    _project(store, tmp_path, web_search=True, local_only=True)
    service = ChatService(store, tool_platform=_platform(tmp_path, store))
    assert service._web_search_selected("project-1") is False
