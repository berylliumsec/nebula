import base64
from datetime import timedelta

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    BrowserAttack,
    BrowserAttackResult,
    BrowserCrawlJob,
    BrowserInterceptItem,
    BrowserRepeaterTab,
    BrowserRepeaterResult,
    BrowserSiteNode,
    BrowserTokenAnalysis,
    BrowserTrafficExchange,
    Engagement,
    Evidence,
    Finding,
    ScopePolicy,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.row_horizon_fixture import PAGE, seed_older_copies


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
    current_repeater = repeater.json()
    for action in ("queue", "start"):
        moved = client.post(
            f"/api/v1/browser-repeater-tabs/{current_repeater['id']}/state",
            headers=_auth(),
            json={
                "expected_revision": current_repeater["revision"],
                "action": action,
                "actor_id": "operator-1",
            },
        )
        assert moved.status_code == 200, moved.text
        current_repeater = moved.json()
    result = client.post(
        f"/api/v1/browser-repeater-tabs/{current_repeater['id']}/results",
        headers=_auth(),
        json={
            "expected_revision": current_repeater["revision"],
            "status_code": 200,
            "response_headers": [
                ["Set-Cookie", "session=secret"],
                ["Content-Type", "application/json"],
            ],
            "response_bytes": 42,
            "duration_ms": 8,
            "actor_id": "native-browser",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["response_headers"][0][1].startswith("<redacted:sha256:")
    assert store.count(BrowserRepeaterResult) == 1
    workspace = client.get(
        f"/api/v1/engagements/{project.id}/browser-research", headers=_auth()
    ).json()
    saved_repeater = workspace["repeater_tabs"][0]
    assert saved_repeater["state"] == "ready"
    assert saved_repeater["request_count"] == 1
    assert saved_repeater["history_exchange_ids"] == [result.json()["id"]]

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
            "payload_source": {
                "kind": "upload",
                "display_name": "boundaries.txt",
                "sha256": "a" * 64,
                "value_count": 5,
            },
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
    saved_attack = store.get(BrowserAttack, current["id"])
    assert saved_attack.state == "complete"
    assert saved_attack.metadata["payload_source"] == {
        "kind": "upload",
        "display_name": "boundaries.txt",
        "sha256": "a" * 64,
        "value_count": 5,
        "prompt_version": None,
        "model": None,
        "provider_profile_id": None,
    }


def test_intruder_requires_real_markers_and_strategy_payload_cardinality(tmp_path):
    _, client, project, identity, session = _setup(tmp_path)
    base = {
        "session_id": session["id"],
        "identity_id": identity["id"],
        "name": "Invalid attack",
        "strategy": "sniper",
        "method": "GET",
        "url_template": "https://app.example.test/api/users/static",
        "positions": ["id"],
        "payload_sets": [{"kind": "list", "values": ["1"]}],
    }
    missing = client.post(
        f"/api/v1/engagements/{project.id}/browser-attacks",
        headers=_auth(),
        json=base,
    )
    assert missing.status_code == 422
    assert "missing position markers" in missing.text

    wrong_sets = client.post(
        f"/api/v1/engagements/{project.id}/browser-attacks",
        headers=_auth(),
        json={
            **base,
            "strategy": "pitchfork",
            "url_template": "https://app.example.test/api/§id§/§role§",
            "positions": ["id", "role"],
        },
    )
    assert wrong_sets.status_code == 422
    assert "requires 2 payload set" in wrong_sets.text


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


def _enable_interception(client, session):
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
    return enabled.json()


def test_intercepts_after_a_page_of_history_stay_single_visible_and_expire(tmp_path):
    store, client, project, _, session = _setup(tmp_path)
    session = _enable_interception(client, session)
    seed_older_copies(
        store,
        BrowserInterceptItem(
            engagement_id=project.id,
            session_id=session["id"],
            tab_id="tab-1",
            identity_id=session["identity_id"],
            transaction_id="decided",
            phase="request",
            method="GET",
            url="https://app.example.test/",
            state="forwarded",
            expires_at=utc_now(),
        ),
        vary=lambda index: {"transaction_id": f"decided-{index}"},
    )
    body = {
        "tab_id": "tab-1",
        "transaction_id": "tx-new",
        "phase": "request",
        "method": "POST",
        "url": "https://app.example.test/profile",
    }

    paused = client.post(
        f"/api/v1/browser-sessions/{session['id']}/intercepts",
        headers=_auth(),
        json=body,
    )
    assert paused.status_code == 201, paused.text
    # The native proxy retries a breakpoint it did not hear back about; the
    # retry must land on the same receipt, not pause the request twice.
    retried = client.post(
        f"/api/v1/browser-sessions/{session['id']}/intercepts",
        headers=_auth(),
        json=body,
    )
    assert retried.json()["id"] == paused.json()["id"]
    assert store.count(BrowserInterceptItem) == PAGE + 1

    research = client.get(
        f"/api/v1/engagements/{project.id}/browser-research", headers=_auth()
    ).json()
    listed = {item["id"]: item["state"] for item in research["intercepts"]}
    assert listed[paused.json()["id"]] == "paused"

    store.update(
        BrowserInterceptItem,
        paused.json()["id"],
        {"expires_at": utc_now() - timedelta(seconds=1)},
    )
    research = client.get(
        f"/api/v1/engagements/{project.id}/browser-research", headers=_auth()
    ).json()
    listed = {item["id"]: item["state"] for item in research["intercepts"]}
    assert listed[paused.json()["id"]] == "interrupted"


def test_site_map_and_har_export_reach_records_after_a_page_of_history(tmp_path):
    store, client, project, _, session = _setup(tmp_path)
    seed_older_copies(
        store,
        BrowserSiteNode(
            engagement_id=project.id,
            session_id=session["id"],
            identity_id=session["identity_id"],
            url="https://app.example.test/old",
            scope_policy_id=project.scope_policy_id,
            scope_policy_revision=1,
        ),
        vary=lambda index: {"url": f"https://app.example.test/old/{index}"},
    )
    seed_older_copies(
        store,
        BrowserTrafficExchange(
            engagement_id=project.id,
            session_id="other-session",
            tab_id="tab-1",
            identity_id=session["identity_id"],
            method="GET",
            url="https://app.example.test/old",
            scope_state="in_scope",
            scope_policy_id=project.scope_policy_id,
            scope_policy_revision=1,
        ),
    )

    for _ in range(2):
        node = client.post(
            f"/api/v1/engagements/{project.id}/browser-site-nodes",
            headers=_auth(),
            json={"session_id": session["id"], "url": "https://app.example.test/new"},
        )
        assert node.status_code == 201, node.text
    assert store.count(BrowserSiteNode) == PAGE + 1

    captured = client.post(
        f"/api/v1/browser-sessions/{session['id']}/traffic",
        headers=_auth(),
        json={
            "tab_id": "tab-1",
            "method": "GET",
            "url": "https://app.example.test/new",
            "status_code": 200,
        },
    )
    assert captured.status_code == 201, captured.text
    exported = client.get(
        f"/api/v1/engagements/{project.id}/browser-har/export",
        headers=_auth(),
        params={"session_id": session["id"]},
    ).json()
    assert [entry["request"]["url"] for entry in exported["log"]["entries"]] == [
        "https://app.example.test/new"
    ]
    workspace = client.get(
        f"/api/v1/engagements/{project.id}/browser-workspace", headers=_auth()
    ).json()
    assert len(workspace["traffic"]) == PAGE
    assert workspace["traffic"][-1]["id"] == captured.json()["id"]


def test_intruder_and_repeater_results_after_a_page_of_history_stay_owned(tmp_path):
    store, client, project, identity, session = _setup(tmp_path)
    seed_older_copies(
        store,
        BrowserAttackResult(
            engagement_id=project.id, attack_id="earlier-attack", sequence=0
        ),
        vary=lambda index: {"sequence": index},
    )
    seed_older_copies(
        store,
        BrowserRepeaterResult(
            engagement_id=project.id, tab_id="earlier-tab", sequence=0
        ),
        vary=lambda index: {"sequence": index},
    )
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
            "max_requests": 3,
        },
    )
    assert attack.status_code == 201, attack.text
    current = attack.json()
    for action in ("queue", "start"):
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
    for _ in range(2):
        result = client.post(
            f"/api/v1/browser-attacks/{current['id']}/results",
            headers=_auth(),
            json={"sequence": 0, "payloads": ["0"], "status_code": 200},
        )
        assert result.status_code == 201, result.text
    owned = store.find_entities(BrowserAttackResult, {"attack_id": current["id"]})
    assert len(owned) == 1
    assert store.get(BrowserAttack, current["id"]).request_count == 1

    repeater = client.post(
        f"/api/v1/engagements/{project.id}/browser-repeater-tabs",
        headers=_auth(),
        json={
            "session_id": session["id"],
            "identity_id": identity["id"],
            "name": "Authorization check",
            "method": "GET",
            "url": "https://app.example.test/api/profile",
        },
    )
    assert repeater.status_code == 201, repeater.text
    tab = repeater.json()
    for action in ("queue", "start"):
        moved = client.post(
            f"/api/v1/browser-repeater-tabs/{tab['id']}/state",
            headers=_auth(),
            json={
                "expected_revision": tab["revision"],
                "action": action,
                "actor_id": "operator-1",
            },
        )
        assert moved.status_code == 200, moved.text
        tab = moved.json()
    recorded = client.post(
        f"/api/v1/browser-repeater-tabs/{tab['id']}/results",
        headers=_auth(),
        json={
            "expected_revision": tab["revision"],
            "status_code": 200,
            "actor_id": "native-browser",
        },
    )
    assert recorded.status_code == 200, recorded.text
    tab = store.get(BrowserRepeaterTab, tab["id"])
    deleted = client.delete(
        f"/api/v1/browser-repeater-tabs/{tab.id}",
        headers=_auth(),
        params={"expected_revision": tab.revision},
    )
    assert deleted.status_code == 204, deleted.text
    assert store.find_entities(BrowserRepeaterResult, {"tab_id": tab.id}) == []
    assert store.count(BrowserRepeaterResult) == PAGE
