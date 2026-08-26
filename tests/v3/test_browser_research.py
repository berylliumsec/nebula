import base64

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    BrowserAttack,
    BrowserAttackResult,
    BrowserCrawlJob,
    BrowserInterceptItem,
    BrowserRepeaterTab,
    BrowserSiteNode,
    BrowserTokenAnalysis,
    Engagement,
    Evidence,
    Finding,
    ScopePolicy,
)
from nebula.v3.storage import NebulaStore


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _setup(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
        )
    )
    created = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "Burp parity lab"}
    )
    assert created.status_code == 201, created.text
    project = store.get(Engagement, created.json()["id"])
    scope = store.get(ScopePolicy, project.scope_policy_id)
    store.update(
        ScopePolicy,
        scope.id,
        {
            "allowed_domains": ["app.example.test"],
            "allowed_ports": [443],
        },
        expected_revision=scope.revision,
    )
    workspace = client.get(
        f"/api/v1/engagements/{project.id}/browser-workspace", headers=_auth()
    ).json()
    identity = workspace["identities"][0]
    session = workspace["sessions"][0]
    synced = client.put(
        f"/api/v1/browser-sessions/{session['id']}/tabs",
        headers=_auth(),
        json={
            "expected_revision": session["revision"],
            "tabs": [
                {
                    "id": "tab-1",
                    "url": "https://app.example.test/",
                    "title": "App",
                    "position": 0,
                    "last_scope_state": "in_scope",
                    "last_scope_revision": 2,
                }
            ],
            "active_tab_id": "tab-1",
            "device_owner": "desktop-1",
        },
    )
    assert synced.status_code == 200, synced.text
    return store, client, project, identity, synced.json()


def test_proxy_capture_populates_durable_target_map_without_secrets(tmp_path):
    store, client, project, _, session = _setup(tmp_path)
    capture = client.post(
        f"/api/v1/browser-sessions/{session['id']}/traffic",
        headers=_auth(),
        json={
            "tab_id": "tab-1",
            "method": "GET",
            "url": "https://app.example.test/api/users?page=1",
            "protocol": "h2",
            "status_code": 200,
            "request_headers": {"Authorization": "Bearer secret"},
            "response_headers": {"Content-Type": "application/json"},
            "request_header_lines": [
                ["X-Trace", "first"],
                ["X-Trace", "second"],
                ["Cookie", "session=secret"],
            ],
            "http2_pseudo_headers": [
                [":method", "GET"],
                [":path", "/api/users?page=1"],
            ],
            "timing": {"connect_ms": 4, "wait_ms": 8},
            "rule_effect_ids": ["rule-1"],
        },
    )
    assert capture.status_code == 201, capture.text
    assert capture.json()["request_header_lines"][:2] == [
        ["X-Trace", "first"],
        ["X-Trace", "second"],
    ]
    assert capture.json()["request_header_lines"][2][1].startswith("<redacted:sha256:")
    assert capture.json()["http2_pseudo_headers"][1] == [
        ":path",
        "/api/users?page=1",
    ]

    research = client.get(
        f"/api/v1/engagements/{project.id}/browser-research", headers=_auth()
    )
    assert research.status_code == 200, research.text
    node = research.json()["site_nodes"][0]
    assert node["kind"] == "api"
    assert node["parameter_names"] == ["page"]
    assert node["last_exchange_id"] == capture.json()["id"]
    assert "secret" not in research.text
    assert store.count(BrowserSiteNode) == 1


def test_interception_requires_opt_in_and_is_single_decision(tmp_path):
    store, client, _, _, session = _setup(tmp_path)
    disabled = client.post(
        f"/api/v1/browser-sessions/{session['id']}/intercepts",
        headers=_auth(),
        json={
            "tab_id": "tab-1",
            "transaction_id": "tx-1",
            "phase": "request",
            "method": "POST",
            "url": "https://app.example.test/profile",
            "headers": [["Authorization", "Bearer secret"]],
        },
    )
    assert disabled.status_code == 422

    enabled = client.put(
        f"/api/v1/browser-sessions/{session['id']}/capture-settings",
        headers=_auth(),
        json={
            "expected_revision": session["revision"],
            "capture_mode": "headers",
            "proxy_enabled": True,
            "trust_acknowledged": True,
            "interception_enabled": True,
            "upstream_proxy_enabled": False,
        },
    )
    assert enabled.status_code == 200, enabled.text
    paused = client.post(
        f"/api/v1/browser-sessions/{session['id']}/intercepts",
        headers=_auth(),
        json={
            "tab_id": "tab-1",
            "transaction_id": "tx-1",
            "phase": "request",
            "method": "POST",
            "url": "https://app.example.test/profile",
            "headers": [["Authorization", "Bearer secret"], ["X-Test", "one"]],
        },
    )
    assert paused.status_code == 201, paused.text
    assert paused.json()["headers"][0][1].startswith("<redacted:sha256:")
    assert "Bearer secret" not in paused.text

    forwarded = client.post(
        f"/api/v1/browser-intercepts/{paused.json()['id']}/decision",
        headers=_auth(),
        json={
            "expected_revision": paused.json()["revision"],
            "decision": "forward",
            "operator_id": "operator-1",
            "headers": [["X-Test", "two"]],
        },
    )
    assert forwarded.status_code == 200, forwarded.text
    assert forwarded.json()["state"] == "forwarded"
    repeated = client.post(
        f"/api/v1/browser-intercepts/{paused.json()['id']}/decision",
        headers=_auth(),
        json={
            "expected_revision": forwarded.json()["revision"],
            "decision": "drop",
            "operator_id": "operator-1",
        },
    )
    assert repeated.status_code == 422
    assert store.count(BrowserInterceptItem) == 1


def test_bounded_crawl_lifecycle_enforces_scope_and_request_budget(tmp_path):
    store, client, project, identity, session = _setup(tmp_path)
    created = client.post(
        f"/api/v1/engagements/{project.id}/browser-crawls",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "identity_id": identity["id"],
            "start_url": "https://app.example.test/docs",
            "max_depth": 2,
            "max_requests": 3,
            "max_concurrency": 1,
            "max_duration_seconds": 30,
            "max_body_bytes": 4096,
        },
    )
    assert created.status_code == 201, created.text
    crawl = created.json()
    assert crawl["state"] == "draft"
    assert store.count(BrowserCrawlJob) == 1

    for action, expected in (("queue", "queued"), ("start", "running")):
        response = client.post(
            f"/api/v1/browser-crawls/{crawl['id']}/state",
            headers=_auth(),
            json={
                "expected_revision": crawl["revision"],
                "action": action,
                "actor_id": "operator",
            },
        )
        assert response.status_code == 200, response.text
        crawl = response.json()
        assert crawl["state"] == expected

    exhausted = client.post(
        f"/api/v1/browser-crawls/{crawl['id']}/state",
        headers=_auth(),
        json={
            "expected_revision": crawl["revision"],
            "action": "complete",
            "actor_id": "native-browser",
            "requests_completed": 4,
            "checkpoint": 4,
        },
    )
    assert exhausted.status_code == 422

    outside = client.post(
        f"/api/v1/engagements/{project.id}/browser-crawls",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "identity_id": identity["id"],
            "start_url": "https://outside.example.invalid/",
        },
    )
    assert outside.status_code == 422


def test_repeater_and_intruder_lifecycles_are_durable_and_budgeted(tmp_path):
    store, client, project, identity, session = _setup(tmp_path)
    repeater = client.post(
        f"/api/v1/engagements/{project.id}/browser-repeater-tabs",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "identity_id": identity["id"],
            "name": "Authorization check",
            "method": "GET",
            "url": "https://app.example.test/api/profile",
            "headers": [["Accept", "application/json"]],
        },
    )
    assert repeater.status_code == 201, repeater.text
    assert store.count(BrowserRepeaterTab) == 1

    attack = client.post(
        f"/api/v1/engagements/{project.id}/browser-attacks",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "identity_id": identity["id"],
            "name": "Identifier boundaries",
            "strategy": "sniper",
            "method": "GET",
            "url_template": "https://app.example.test/api/users/§id§",
            "positions": ["id"],
            "payload_sets": [{"kind": "curated", "name": "boundary_numbers"}],
            "transforms": ["url_encode"],
            "max_requests": 2,
        },
    )
    assert attack.status_code == 201, attack.text
    current = attack.json()
    for action in ("queue", "start", "pause", "resume"):
        moved = client.post(
            f"/api/v1/browser-attacks/{current['id']}/state",
            headers=_auth(),
            json={
                "expected_revision": current["revision"],
                "action": action,
                "actor_id": "operator-1",
            },
        )
        assert moved.status_code == 200, moved.text
        current = moved.json()
    assert current["state"] == "running"
    for sequence in range(2):
        result = client.post(
            f"/api/v1/browser-attacks/{current['id']}/results",
            headers=_auth(),
            json={
                "sequence": sequence,
                "payloads": [str(sequence)],
                "status_code": 200,
            },
        )
        assert result.status_code == 201, result.text
    exhausted = client.post(
        f"/api/v1/browser-attacks/{current['id']}/results",
        headers=_auth(),
        json={"sequence": 2, "payloads": ["2"], "status_code": 200},
    )
    assert exhausted.status_code == 422
    assert store.count(BrowserAttack) == 1
    assert store.count(BrowserAttackResult) == 2


def test_decoder_comparer_sequencer_har_and_finding_promotion(tmp_path):
    store, client, project, _, session = _setup(tmp_path)
    encoded = client.post(
        "/api/v1/browser-utilities/decode",
        headers=_auth(),
        json={"operation": "base64_encode", "value": "nebula"},
    )
    assert encoded.status_code == 200
    assert encoded.json()["result"] == base64.b64encode(b"nebula").decode()
    compared = client.post(
        "/api/v1/browser-utilities/compare",
        headers=_auth(),
        json={"mode": "json", "left": '{"b":2,"a":1}', "right": '{"a":1,"b":3}'},
    )
    assert compared.status_code == 200
    assert compared.json()["equal"] is False
    assert compared.json()["diff"]

    sequencer = client.post(
        f"/api/v1/engagements/{project.id}/browser-token-analyses",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "name": "Session tokens",
            "samples": ["abc1", "abc2", "abc2"],
        },
    )
    assert sequencer.status_code == 201, sequencer.text
    assert sequencer.json()["collision_count"] == 1
    assert store.count(BrowserTokenAnalysis) == 1

    imported = client.post(
        f"/api/v1/engagements/{project.id}/browser-har/import",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "har": {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": "https://app.example.test/har",
                                "headers": [{"name": "Cookie", "value": "secret"}],
                            },
                            "response": {
                                "status": 200,
                                "headers": [
                                    {"name": "Content-Type", "value": "text/html"}
                                ],
                            },
                        }
                    ]
                }
            },
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {
        "session_id": session["id"],
        "entries": 1,
        "imported": 1,
        "skipped": 0,
        "bodies_imported": 0,
        "redaction": "headers and bodies containing reusable secrets are not imported",
    }
    exported = client.get(
        f"/api/v1/engagements/{project.id}/browser-har/export",
        headers=_auth(),
        params={"session_id": session["id"]},
    )
    assert exported.status_code == 200
    assert '"value":"secret"' not in exported.text

    evidence = store.create(
        Evidence(
            engagement_id=project.id,
            evidence_type="browser-response",
            title="Authorization variance",
            sha256="a" * 64,
        )
    )
    node = store.list_entities(BrowserSiteNode, engagement_id=project.id, limit=100)[0]
    finding = client.post(
        f"/api/v1/engagements/{project.id}/browser-findings",
        headers=_auth(),
        json={
            "title": "Possible authorization variance",
            "severity": "medium",
            "evidence_ids": [evidence.id],
            "site_node_ids": [node.id],
        },
    )
    assert finding.status_code == 201, finding.text
    assert finding.json()["status"] == "candidate"
    assert finding.json()["metadata"]["browser_site_node_ids"] == [node.id]
    assert store.count(Finding) == 1
