from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.browser_companion import BrowserCompanion
from nebula.v3.agent_security import AgentSecuritySessions
from nebula.v3.agent_security_tools import PROFILE_ID
from nebula.v3.browser_security import (
    BrowserSecurityService,
    BrowserTrafficRecordRequest,
    BrowserWorkflowError,
)
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.domain import (
    AutomationProjectPolicy,
    BrowserIdentity,
    BrowserSession,
    ChatSession,
    Engagement,
    McpServerProfile,
    ScopePolicy,
)
from nebula.v3.storage import NebulaStore


def _auth():
    return {"Authorization": "Bearer test-token"}


async def test_live_headless_enablement_preserves_lan_browser_mode(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("NEBULA_ONE_BROWSER_CUTOVER", raising=False)
    monkeypatch.setenv("NEBULA_AGENT_SECURITY_ENABLED", "1")
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(Engagement(name="LAN browser and headless security"))
    app = create_app(store, auth_token="test-token")

    async with TestClient(app) as client:
        response = await client.get("/api/v1/desktop-browser-mode", headers=_auth())
        assert response.status_code == 200
        assert response.json() == {
            "one_browser_cutover": False,
            "agent_security_enabled": True,
        }
        profile = await store.get(McpServerProfile, PROFILE_ID)
        assert profile.enabled is True
        assert profile.name == "browser-security-testing-tools"
        assert (
            app.state.mcp_probe_service.security_tools is app.state.agent_security_tools
        )
        assert (
            app.state.harness_runtime_service.mcp_service.security_tools
            is app.state.agent_security_tools
        )
        workspace = await BrowserSecurityService(store).workspace(project.id)
        assert workspace.sessions
        assert all(
            session.runtime_kind != "agent_security" for session in workspace.sessions
        )


async def test_project_approval_is_durable_and_separate_from_command_approvals(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_ONE_BROWSER_CUTOVER", "1")
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(Engagement(name="Agent security"))
    async with TestClient(create_app(store, auth_token="test-token")) as client:
        url = f"/api/v1/engagements/{project.id}/browser-security-approval"
        original = await client.get(url, headers=_auth())
        assert original.status_code == 200
        assert original.json()["allow_all"] is False
        allowed = await client.put(
            url,
            headers=_auth(),
            json={"allow_all": True, "expected_revision": original.json()["revision"]},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["allow_all"] is True
        assert (await client.get(url, headers=_auth())).json()["allow_all"] is True
        policy = (
            await store.list_entities(AutomationProjectPolicy, engagement_id=project.id)
        )[0]
        assert policy.approval_policy.value == "on_boundary"
        stale = await client.put(
            url,
            headers=_auth(),
            json={"allow_all": False, "expected_revision": original.json()["revision"]},
        )
        assert stale.status_code == 409
        identity = await store.create(
            BrowserIdentity(engagement_id=project.id, name="Visible")
        )
        visible = await store.create(
            BrowserSession(
                engagement_id=project.id,
                identity_id=identity.id,
                name="Visible",
                runtime_kind="electron_visible",
                metadata={"browser_companion_version": 1, "assistant_paused": False},
            )
        )
        revoked = await client.put(
            url,
            headers=_auth(),
            json={"allow_all": False, "expected_revision": allowed.json()["revision"]},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["allow_all"] is False
        assert (await store.get(BrowserSession, visible.id)).metadata[
            "assistant_paused"
        ] is True


async def test_visible_navigation_checks_project_scope_each_time(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(Engagement(name="Scoped browser"))
    scope = await store.create(ScopePolicy(engagement_id=project.id))
    project = await store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id},
        expected_revision=project.revision,
    )
    (
        await store.update(
            ScopePolicy,
            scope.id,
            {
                "allowed_domains": ["app.example.test"],
                "allowed_ports": [443],
            },
            expected_revision=scope.revision,
        )
    )
    async with TestClient(create_app(store, auth_token="test-token")) as client:
        url = f"/api/v1/engagements/{project.id}/browser-navigation-check"
        allowed = await client.post(
            url, headers=_auth(), json={"url": "https://app.example.test/page"}
        )
        assert allowed.status_code == 200, allowed.text
        denied = await client.post(
            url, headers=_auth(), json={"url": "https://elsewhere.example.test/"}
        )
        assert denied.status_code == 403
        assert (
            await client.post(url, headers=_auth(), json={"url": "file:///etc/passwd"})
        ).status_code == 403


async def test_cutover_reuses_one_visible_session_and_rejects_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_ONE_BROWSER_CUTOVER", "1")
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(Engagement(name="Visible browser"))
    legacy_identity = await store.create(
        BrowserIdentity(engagement_id=project.id, name="Old")
    )
    legacy = await store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=legacy_identity.id,
            name="Old",
            metadata={"browser_companion_version": 1},
        )
    )
    chats = [
        (
            await store.create(
                ChatSession(
                    engagement_id=project.id,
                    title=f"Chat {index}",
                    model="fixture",
                    provider_profile_id="provider",
                )
            )
        )
        for index in range(2)
    ]
    service = BrowserCompanion(store, BrowserEngineRegistry([]))

    class Adapter:
        async def ensure_identity(self, identity_id, project_id):
            assert project_id == project.id

    async def adapter():
        return Adapter()

    async def tabs(session_id, request):
        return {"tabs": []}

    monkeypatch.setattr(service, "adapter", adapter)
    monkeypatch.setattr(service, "request", tabs)
    with pytest.raises(ValueError, match="historical"):
        (await service.session(legacy.id))

    async def run():
        first = await service.open(project.id, chats[0].id)
        second = await service.open(project.id, chats[1].id)
        assert first["session_id"] == second["session_id"]
        assert second["conversation_id"] == chats[1].id
        visible = await store.get(BrowserSession, first["session_id"])
        assert visible.runtime_kind == "electron_visible"
        assert visible.metadata["conversation_id"] == chats[1].id

    (await run())


async def test_cutover_never_bootstraps_native_research_or_captures_visible_traffic(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_ONE_BROWSER_CUTOVER", "1")
    store = NebulaStore(tmp_path / "nebula.db")
    project = await store.create(Engagement(name="Visible only"))
    service = BrowserSecurityService(store)
    assert (await service.workspace(project.id)).sessions == []
    identity = await store.create(
        BrowserIdentity(engagement_id=project.id, name="Visible")
    )
    visible = await store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=identity.id,
            name="Visible",
            runtime_kind="electron_visible",
        )
    )
    with pytest.raises(BrowserWorkflowError, match="headless agent security"):
        (
            await service.record_traffic(
                visible.id,
                BrowserTrafficRecordRequest(
                    tab_id="never-captured", method="GET", url="https://example.test/"
                ),
                "operator",
            )
        )


async def test_agent_security_identity_is_headless_and_project_isolated(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    projects = [
        (await store.create(Engagement(name=f"Project {index}"))) for index in range(2)
    ]
    for project in projects:
        scope = await store.create(
            ScopePolicy(engagement_id=project.id, allowed_domains=["example.test"])
        )
        (await store.update(Engagement, project.id, {"scope_policy_id": scope.id}))

    class Adapter:
        async def _request(self, method, path, body):
            assert method == "POST" and path == "/v1/identities/ensure"
            assert (await store.get(BrowserIdentity, body["identity_id"])).metadata[
                "runtime_kind"
            ] == "agent_security"

            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"tab_ids": [f"tab-{body['identity_id']}"]}

            return Response()

    class HeadlessHost:
        headless = True

        def __init__(self):
            self.store = store
            self.closed = []

        async def adapter(self):
            return Adapter()

        async def close_identity(self, identity_id):
            self.closed.append(identity_id)

    host = HeadlessHost()
    service = AgentSecuritySessions(store, host)

    async def run():
        first = await service.ensure(projects[0].id)
        again = await service.ensure(projects[0].id)
        other = await service.ensure(projects[1].id)
        assert first.id == again.id
        assert first.identity_id != other.identity_id
        assert first.runtime_kind == other.runtime_kind == "agent_security"
        assert first.active_tab_id == f"tab-{first.identity_id}"
        assert other.active_tab_id == f"tab-{other.identity_id}"
        assert await service.stop(projects[0].id, reason="Operator emergency stop") == 1
        assert host.closed == [first.identity_id]
        assert (await store.get(BrowserSession, first.id)).status.value == "paused"
        assert (await store.get(BrowserSession, other.id)).status.value == "active"

    (await run())


pytestmark = pytest.mark.anyio
