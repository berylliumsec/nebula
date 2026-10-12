import asyncio
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from nebula.v3.agent_security_tools import (
    SecurityToolsRuntime,
    _attack_payloads,
    PROFILE_ID,
    PROFILE_NAME,
)
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.browserd import BrowserdSecurityResponse
from nebula.v3.browser_security import (
    BrowserSecurityService,
    BrowserTrafficRecordRequest,
)
from nebula.v3.domain import (
    Artifact,
    BrowserAttack,
    BrowserIdentity,
    BrowserRepeaterResult,
    BrowserSession,
    BrowserTabState,
    Engagement,
    Evidence,
    Finding,
    McpServerProfile,
    McpTransport,
    ScopePolicy,
)
from nebula.v3.storage import NebulaStore


class StubSessions:
    def __init__(self, session):
        self.session = session
        self.requests = []
        self.stopped = []

    async def ensure(self, project_id):
        assert project_id == self.session.engagement_id
        return self.session

    async def request(self, project_id, request):
        assert project_id == self.session.engagement_id
        assert request.identity_id == self.session.identity_id
        self.requests.append(request)
        return BrowserdSecurityResponse(
            url=request.url,
            status_code=200,
            headers={"content-type": "text/plain"},
            body_base64="b2s=",
            body_bytes=2,
            truncated=False,
        )

    async def stop(self, project_id, *, reason):
        self.stopped.append((project_id, reason))
        return 1


def _project(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Security tools"))
    scope = store.create(
        ScopePolicy(
            engagement_id=project.id,
            allowed_domains=["example.test"],
            allowed_ports=[443],
        )
    )
    store.update(Engagement, project.id, {"scope_policy_id": scope.id})
    identity = store.create(
        BrowserIdentity(
            engagement_id=project.id,
            name="Headless",
            metadata={"runtime_kind": "agent_security"},
        )
    )
    session = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=identity.id,
            runtime_kind="agent_security",
            name="Headless",
            tabs=[BrowserTabState(id="headless-tab")],
            active_tab_id="headless-tab",
        )
    )
    return store, project, session


def test_security_tools_profile_is_optional_and_schemas_are_valid(tmp_path):
    store, _, session = _project(tmp_path)
    runtime = SecurityToolsRuntime(store, StubSessions(session))
    profile = runtime.register_profile()
    assert profile.id == PROFILE_ID
    assert profile.name == PROFILE_NAME
    assert profile.transport == McpTransport.BUILTIN
    assert profile.enabled is True
    assert profile.required is False
    assert store.get(McpServerProfile, PROFILE_ID).capabilities.checked_at is not None
    for tool in profile.capabilities.tools:
        Draft202012Validator.check_schema(tool.input_schema)
        assert tool.open_world is False
    assert runtime.register_profile().id == PROFILE_ID
    store.update(
        McpServerProfile,
        PROFILE_ID,
        {
            "name": "security-tools",
            "capabilities": {"protocol_version": "2025-06-18", "tools": []},
        },
        expected_revision=profile.revision,
    )
    refreshed = runtime.register_profile()
    assert refreshed.name == PROFILE_NAME
    assert {item.name for item in refreshed.capabilities.tools} >= {
        "traffic",
        "har_export",
        "token_analysis",
        "evidence_from_exchange",
        "finding_candidate",
    }


def test_repeater_uses_headless_identity_and_saves_durable_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_ONE_BROWSER_CUTOVER", "1")
    store, project, session = _project(tmp_path)
    sessions = StubSessions(session)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    sessions.host = SimpleNamespace(artifact_store=artifacts)
    runtime = SecurityToolsRuntime(store, sessions)

    async def run():
        capture = await runtime.call_tool(project.id, "capture", {"mode": "bodies"})
        assert capture["structuredContent"]["capture_mode"] == "bodies"
        sessions.session = store.get(BrowserSession, session.id)
        result = await runtime.call_tool(
            project.id,
            "repeater",
            {
                "method": "GET",
                "url": "https://example.test/path",
            },
        )
        assert result["structuredContent"]["status_code"] == 200
        assert len(sessions.requests) == 1
        assert sessions.requests[0].url == "https://example.test/path"
        saved = store.get(
            BrowserRepeaterResult, result["structuredContent"]["result_id"]
        )
        assert saved.engagement_id == project.id
        assert saved.status_code == 200
        assert saved.response_body_artifact_id
        artifact = store.get(Artifact, saved.response_body_artifact_id)
        assert artifacts.read(artifact) == b"ok"
        exchange = BrowserSecurityService(store).record_traffic(
            session.id,
            BrowserTrafficRecordRequest(
                tab_id="headless-tab",
                method="GET",
                url="https://example.test/path",
                status_code=200,
                response_headers={"content-type": "text/plain"},
            ),
            "test-proxy",
        )
        traffic = await runtime.call_tool(project.id, "traffic", {"limit": 5})
        assert [item["id"] for item in traffic["structuredContent"]["exchanges"]] == [
            exchange.id
        ]
        har = await runtime.call_tool(project.id, "har_export", {"limit": 5})
        assert (
            har["structuredContent"]["har"]["log"]["entries"][0]["request"]["url"]
            == exchange.url
        )
        analysis = await runtime.call_tool(
            project.id,
            "token_analysis",
            {
                "samples": ["abc", "abd"],
                "source_exchange_ids": [exchange.id],
            },
        )
        assert analysis["structuredContent"]["sample_count"] == 2
        evidence = await runtime.call_tool(
            project.id,
            "evidence_from_exchange",
            {
                "exchange_id": exchange.id,
                "title": "Scoped response",
            },
        )
        evidence_id = evidence["structuredContent"]["evidence_id"]
        assert (
            store.get(Evidence, evidence_id).metadata["browser_exchange_id"]
            == exchange.id
        )
        finding = await runtime.call_tool(
            project.id,
            "finding_candidate",
            {
                "title": "Review this response",
                "evidence_ids": [evidence_id],
                "source_exchange_ids": [exchange.id],
            },
        )
        assert (
            store.get(Finding, finding["structuredContent"]["finding_id"]).status.value
            == "candidate"
        )
        await runtime.close()

    asyncio.run(run())


def test_interception_control_and_project_stop_are_separate_from_visible_browser(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_ONE_BROWSER_CUTOVER", "1")
    store, project, session = _project(tmp_path)
    sessions = StubSessions(session)
    runtime = SecurityToolsRuntime(store, sessions)

    async def run():
        enabled = await runtime.call_tool(project.id, "intercept", {"enabled": True})
        assert enabled["structuredContent"]["enabled"] is True
        assert store.get(BrowserSession, session.id).interception_enabled is True
        status = await runtime.call_tool(project.id, "status", {})
        assert status["structuredContent"]["interception_enabled"] is True
        stopped = await runtime.call_tool(project.id, "stop", {})
        assert stopped["structuredContent"]["stopped_sessions"] == 1
        assert sessions.stopped[0][0] == project.id
        await runtime.close()

    asyncio.run(run())


def test_intruder_payload_strategies_match_durable_sequence():
    common = dict(
        engagement_id="project",
        session_id="session",
        identity_id="identity",
        name="Intruder fixture",
        method="GET",
        url_template="https://example.test/?a=§a§&b=§b§",
        positions=["a", "b"],
        max_requests=20,
    )
    one = [{"kind": "list", "values": ["x", "y"]}]
    two = [
        {"kind": "list", "values": ["1", "2"]},
        {"kind": "list", "values": ["a", "b"]},
    ]
    sniper = BrowserAttack(strategy="sniper", payload_sets=one, **common)
    battering = BrowserAttack(strategy="battering_ram", payload_sets=one, **common)
    pitchfork = BrowserAttack(strategy="pitchfork", payload_sets=two, **common)
    cluster = BrowserAttack(strategy="cluster_bomb", payload_sets=two, **common)
    assert _attack_payloads(sniper, 0) == {"a": "x", "b": ""}
    assert _attack_payloads(sniper, 2) == {"a": "", "b": "x"}
    assert _attack_payloads(battering, 1) == {"a": "y", "b": "y"}
    assert _attack_payloads(pitchfork, 1) == {"a": "2", "b": "b"}
    assert _attack_payloads(cluster, 2) == {"a": "2", "b": "a"}
