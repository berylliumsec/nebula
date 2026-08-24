from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import BrowserAction, BrowserHandoff, BrowserIdentity, BrowserSession, BrowserTrafficExchange, BrowserWebSocketFrame, Engagement, ScopePolicy
from nebula.v3.storage import NebulaStore


def _auth():
    return {"Authorization": "Bearer test-token"}


def _project(client: TestClient, store: NebulaStore) -> tuple[Engagement, ScopePolicy]:
    response = client.post("/api/v1/engagements", headers=_auth(), json={"name": "Browser lab"})
    assert response.status_code == 201
    project = store.get(Engagement, response.json()["id"])
    scope = store.get(ScopePolicy, project.scope_policy_id)
    scope = store.update(
        ScopePolicy,
        scope.id,
        {"allowed_domains": ["app.example.test"], "allowed_cidrs": ["203.0.113.0/24"], "allowed_ports": [443]},
        expected_revision=scope.revision,
    )
    return project, scope


def _workspace(client: TestClient, project: Engagement) -> dict:
    response = client.get(f"/api/v1/engagements/{project.id}/browser-workspace", headers=_auth())
    assert response.status_code == 200
    return response.json()


def _synced_session(client: TestClient, project: Engagement) -> tuple[dict, dict]:
    workspace = _workspace(client, project)
    identity = workspace["identities"][0]
    session = workspace["sessions"][0]
    response = client.put(
        f"/api/v1/browser-sessions/{session['id']}/tabs",
        headers=_auth(),
        json={
            "expected_revision": session["revision"],
            "tabs": [{"id": "tab-1", "url": "https://app.example.test/", "title": "App", "position": 0, "last_scope_state": "in_scope", "last_scope_revision": 2}],
            "active_tab_id": "tab-1",
            "device_owner": "desktop-1",
        },
    )
    assert response.status_code == 200, response.text
    return identity, response.json()


def test_workspace_bootstrap_is_durable_and_idempotent(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, artifact_store=ArtifactStore(tmp_path / "artifacts"), auth_token="test-token"))
    project, _ = _project(client, store)

    first = _workspace(client, project)
    second = _workspace(client, project)

    assert [item["id"] for item in first["identities"]] == [item["id"] for item in second["identities"]]
    assert [item["id"] for item in first["sessions"]] == [item["id"] for item in second["sessions"]]
    assert store.count(BrowserIdentity) == 1
    assert store.count(BrowserSession) == 1


def test_traffic_is_scope_bound_and_redacts_reusable_secrets(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, artifact_store=ArtifactStore(tmp_path / "artifacts"), auth_token="test-token"))
    project, _ = _project(client, store)
    _, session = _synced_session(client, project)

    response = client.post(
        f"/api/v1/browser-sessions/{session['id']}/traffic",
        headers=_auth(),
        json={
            "tab_id": "tab-1",
            "method": "GET",
            "url": "https://app.example.test/account",
            "protocol": "h2",
            "status_code": 200,
            "request_headers": {"Authorization": "Bearer reusable", "Accept": "application/json", "X-CSRF-Token": "small"},
            "response_headers": {"Set-Cookie": "sid=secret", "Content-Type": "application/json"},
        },
    )
    assert response.status_code == 201, response.text
    exchange = response.json()
    assert exchange["request_headers"]["Authorization"].startswith("<redacted:sha256:")
    assert exchange["request_headers"]["X-CSRF-Token"].startswith("<redacted:sha256:")
    assert exchange["response_headers"]["Set-Cookie"].startswith("<redacted:sha256:")
    assert exchange["request_headers"]["Accept"] == "application/json"
    assert "reusable" not in response.text and "sid=secret" not in response.text

    outside = client.post(
        f"/api/v1/browser-sessions/{session['id']}/traffic",
        headers=_auth(),
        json={"tab_id": "tab-1", "method": "GET", "url": "https://outside.example/"},
    )
    assert outside.status_code == 422
    assert "outside" in outside.json()["detail"]
    assert store.count(BrowserTrafficExchange) == 1


def test_action_requires_approval_and_rechecks_scope_revision(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, artifact_store=ArtifactStore(tmp_path / "artifacts"), auth_token="test-token"))
    project, scope = _project(client, store)
    _, session = _synced_session(client, project)

    proposal = client.post(
        f"/api/v1/browser-sessions/{session['id']}/actions",
        headers=_auth(),
        json={
            "tab_id": "tab-1",
            "kind": "click",
            "locator": {"role": "button", "name": "Open account"},
            "arguments": {},
            "proposal": "Open the account details panel.",
            "proposed_by": "assistant:turn-1",
            "page_url": "https://app.example.test/account",
        },
    )
    assert proposal.status_code == 201, proposal.text
    action = proposal.json()
    denied_start = client.post(
        f"/api/v1/browser-actions/{action['id']}/start",
        headers=_auth(),
        json={"expected_revision": action["revision"], "device_id": "desktop-1"},
    )
    assert denied_start.status_code == 422

    approved = client.post(
        f"/api/v1/browser-actions/{action['id']}/decision",
        headers=_auth(),
        json={"expected_revision": action["revision"], "operator_id": "operator-1", "decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    approved_action = approved.json()
    assert approved_action["status"] == "approved"
    assert approved_action["approved_by"] == "operator-1"

    store.update(ScopePolicy, scope.id, {"allowed_domains": []}, expected_revision=scope.revision)
    stale = client.post(
        f"/api/v1/browser-actions/{action['id']}/start",
        headers=_auth(),
        json={"expected_revision": approved_action["revision"], "device_id": "desktop-1"},
    )
    assert stale.status_code == 422
    assert "scope changed" in stale.json()["detail"]
    assert store.get(BrowserAction, action["id"]).status.value == "approved"


def test_mobile_handoff_is_expiring_claimed_and_single_owner(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, artifact_store=ArtifactStore(tmp_path / "artifacts"), auth_token="test-token"))
    project, _ = _project(client, store)
    _, session = _synced_session(client, project)

    queued = client.post(
        f"/api/v1/browser-sessions/{session['id']}/handoffs",
        headers=_auth(),
        json={"requested_by_device_id": "phone-1", "command": "navigate", "url": "https://app.example.test/profile"},
    )
    assert queued.status_code == 201, queued.text
    handoff = queued.json()
    assert handoff["status"] == "queued"

    claimed = client.post(
        f"/api/v1/browser-handoffs/{handoff['id']}/claim",
        headers=_auth(),
        json={"expected_revision": handoff["revision"], "desktop_device_id": "desktop-1"},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    second_claim = client.post(
        f"/api/v1/browser-handoffs/{handoff['id']}/claim",
        headers=_auth(),
        json={"expected_revision": claimed.json()["revision"], "desktop_device_id": "desktop-2"},
    )
    assert second_claim.status_code == 422
    assert store.count(BrowserHandoff) == 1


def test_replay_is_inert_until_approval_and_rejects_secret_headers(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, artifact_store=ArtifactStore(tmp_path / "artifacts"), auth_token="test-token"))
    project, _ = _project(client, store)
    _, session = _synced_session(client, project)
    base = {
        "tab_id": "tab-1",
        "kind": "replay",
        "locator": {},
        "arguments": {"method": "POST", "url": "https://203.0.113.10/api/profile", "headers": {"Content-Type": "application/json"}, "body": '{"name":"analyst"}'},
        "proposal": "Replay the captured profile request with a non-secret edit.",
        "proposed_by": "operator-1",
        "page_url": "https://app.example.test/",
    }
    proposed = client.post(f"/api/v1/browser-sessions/{session['id']}/actions", headers=_auth(), json=base)
    assert proposed.status_code == 201, proposed.text
    assert proposed.json()["status"] == "proposed"

    secret = {**base, "arguments": {**base["arguments"], "headers": {"Authorization": "Bearer secret"}}}
    denied = client.post(f"/api/v1/browser-sessions/{session['id']}/actions", headers=_auth(), json=secret)
    assert denied.status_code == 422
    assert "reusable secrets" in denied.json()["detail"]
    assert "Bearer secret" not in str(store.list_entities(BrowserAction, engagement_id=project.id, limit=100))


def test_websocket_payload_preview_requires_body_capture_mode(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, artifact_store=ArtifactStore(tmp_path / "artifacts"), auth_token="test-token"))
    project, _ = _project(client, store)
    _, session = _synced_session(client, project)
    exchange = client.post(
        f"/api/v1/browser-sessions/{session['id']}/traffic",
        headers=_auth(),
        json={"tab_id": "tab-1", "method": "GET", "url": "https://app.example.test/socket", "protocol": "websocket", "status_code": 101},
    ).json()
    frame = client.post(
        f"/api/v1/browser-sessions/{session['id']}/websocket-frames",
        headers=_auth(),
        json={"exchange_id": exchange["id"], "direction": "server", "opcode": "text", "payload_preview": "sensitive message", "payload_sha256": "a" * 64, "payload_bytes": 17},
    )
    assert frame.status_code == 201, frame.text
    assert frame.json()["payload_preview"] == ""
    assert frame.json()["truncated"] is True
    assert store.count(BrowserWebSocketFrame) == 1
