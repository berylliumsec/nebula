from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    AgentRun,
    Approval,
    ApprovalStatus,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ToolCallOrigin,
    utc_now,
)
from nebula.v3.providers import (
    ModelResponse,
    OpenAICompatibleProvider,
    ProviderFlavor,
    ProviderHealth,
    ToolCall,
    ToolChoice,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.version import __version__


@pytest.fixture
def api(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(
        store,
        artifact_store=artifacts,
        auth_token="test-token",
        allow_internal_event_append=True,
    )
    return TestClient(app), store, artifacts


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_health_and_data_routes_require_auth(api):
    client, _, _ = api
    assert client.get("/api/v1/health").status_code == 401
    assert (
        client.get(
            "/api/v1/health", headers={"Authorization": "Bearer wrong-token"}
        ).status_code
        == 401
    )
    response = client.get("/api/v1/health", headers=_auth())
    assert response.status_code == 200
    assert response.json()["journal_mode"] == "wal"
    assert response.json()["human_pty"] == "unavailable"
    assert response.json()["container_terminal"] == "unavailable"
    assert response.json()["version"] == __version__
    assert {
        "commit",
        "target",
        "build_timestamp",
        "distribution_channel",
    } <= response.json().keys()
    assert client.get("/api/v1/engagements").status_code == 401
    assert client.get("/api/v1/engagements", headers=_auth()).status_code == 200
    catalog = client.get("/api/v1/provider-catalog", headers=_auth())
    assert catalog.status_code == 200
    assert any(
        item["flavor"] == "vllm" and item["local"] is True for item in catalog.json()
    )


def test_local_provider_discovery_probes_only_fixed_services(api, monkeypatch):
    client, _, _ = api
    observed: list[tuple[ProviderFlavor, str]] = []

    async def health(runtime):
        observed.append((runtime.config.flavor, runtime.config.base_url))
        if runtime.config.flavor == ProviderFlavor.VLLM:
            return ProviderHealth(
                provider_id=runtime.config.id,
                healthy=True,
                models=["security-model", "security-model"],
            )
        return ProviderHealth(provider_id=runtime.config.id, healthy=False)

    monkeypatch.setattr(OpenAICompatibleProvider, "health", health)
    response = client.get("/api/v1/providers/discover-local", headers=_auth())

    assert response.status_code == 200
    assert response.json() == [
        {
            "flavor": "vllm",
            "display_name": "vLLM",
            "endpoint": "http://127.0.0.1:8000/v1",
            "models": ["security-model"],
        }
    ]
    assert set(observed) == {
        (ProviderFlavor.OLLAMA, "http://127.0.0.1:11434/v1"),
        (ProviderFlavor.VLLM, "http://127.0.0.1:8000/v1"),
        (ProviderFlavor.LM_STUDIO, "http://127.0.0.1:1234/v1"),
    }


def test_vllm_profile_health_discovers_models_through_the_api(api, monkeypatch):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            name="Lab vLLM",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["security-model"],
        )
    )

    async def healthy(runtime):
        assert runtime.config.flavor == ProviderFlavor.VLLM
        assert runtime.config.base_url == "http://127.0.0.1:8000/v1"
        return ProviderHealth(
            provider_id=runtime.config.id,
            healthy=True,
            models=["security-model", "vision-model"],
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "health", healthy)

    response = client.post(f"/api/v1/providers/{profile.id}/health", headers=_auth())

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": profile.id,
        "healthy": True,
        "models": ["security-model"],
        "detail": None,
    }


def test_exact_model_capability_probe_persists_and_runtime_edit_requires_reverification(
    api, monkeypatch
):
    client, store, _ = api
    profile = store.create(
        ProviderProfile(
            name="Lab vLLM",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["coder-model"],
            metadata={"default_model": "coder-model"},
        )
    )

    async def valid_probe(_runtime, request):
        assert request.model == "coder-model"
        assert request.tool_choice == ToolChoice.REQUIRED
        assert len(request.tools) == 1
        nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
        return ModelResponse(
            provider_id=profile.id,
            model="coder-model",
            tool_calls=[
                ToolCall(
                    id="probe-call",
                    name="nebula_capability_probe",
                    arguments={"nonce": nonce},
                )
            ],
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "complete", valid_probe)
    verified = client.post(
        f"/api/v1/providers/{profile.id}/capabilities/verify",
        headers=_auth(),
        json={"model": "coder-model", "expected_revision": profile.revision},
    )

    assert verified.status_code == 200
    assert verified.json()["verification"]["status"] == "verified", verified.json()[
        "verification"
    ]["failure_detail"]
    stored = store.get(ProviderProfile, profile.id)
    assert stored.tools_verified_for("coder-model") is True
    assert stored.capabilities.tool_calling is True

    changed = client.patch(
        f"/api/v1/providers/{profile.id}",
        headers=_auth(),
        json={
            "changes": {
                "metadata": {
                    "default_model": "coder-model",
                    "options": {"timeout_seconds": 30},
                }
            },
            "expected_revision": stored.revision,
        },
    )

    assert changed.status_code == 200
    assert changed.json()["capability_verifications"] == {}
    assert changed.json()["capabilities"]["tool_calling"] is False


def test_chat_origin_approval_decision_does_not_require_an_agent_run(api):
    client, store, _ = api
    engagement = Engagement(id="eng-chat-approval", name="Chat approval")
    session = ChatSession(
        id="session-chat-approval",
        engagement_id=engagement.id,
        title="Approval chat",
        provider_profile_id="provider-chat",
        model="model-a",
    )
    turn = ChatTurn(
        id="turn-chat-approval",
        engagement_id=engagement.id,
        session_id=session.id,
        provider_profile_id="provider-chat",
        model="model-a",
        status=ChatTurnStatus.WAITING_APPROVAL,
        tools_enabled=True,
        approval_id="approval-chat",
    )
    approval = Approval(
        id="approval-chat",
        engagement_id=engagement.id,
        run_id=turn.id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session.id,
        chat_turn_id=turn.id,
        risk_class=RiskClass.ACTIVE_SCAN,
        exact_request={"tool_name": "safe.scan", "arguments": {"target": "host"}},
        policy_rationale="operator confirmation required",
        requested_by="chat-assistant",
    )
    store.create_many([engagement, session, turn, approval])

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={"decision": "approve"},
    )

    assert response.status_code == 200
    assert response.json()["origin"] == "chat"
    assert response.json()["status"] == "approved"
    assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.WAITING_APPROVAL


def test_disabled_provider_health_fails_closed_without_network(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "disabled-provider.db")
    profile = store.create(
        ProviderProfile(
            name="Disabled",
            provider_type="vllm",
            is_local=True,
            enabled=False,
        )
    )
    invalid = store.create(
        ProviderProfile(name="Invalid import", provider_type="not-a-provider")
    )

    async def should_not_run(_runtime):
        raise AssertionError("disabled provider health must not access the network")

    monkeypatch.setattr(OpenAICompatibleProvider, "health", should_not_run)
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.post(
        f"/api/v1/providers/{profile.id}/health",
        headers=_auth(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": profile.id,
        "healthy": False,
        "models": [],
        "detail": "provider profile is disabled",
    }
    refreshed = client.post("/api/v1/provider-health/refresh", headers=_auth())
    assert refreshed.status_code == 200
    by_id = {item["provider_id"]: item for item in refreshed.json()}
    assert by_id[profile.id]["detail"] == "provider profile is disabled"
    assert by_id[invalid.id]["healthy"] is False
    assert "unknown provider type" in by_id[invalid.id]["detail"]


def test_tauri_cors_and_audit_resources_are_fail_closed_by_default(tmp_path):
    store = NebulaStore(tmp_path / "secure.db")
    app = create_app(store, auth_token="test-token")
    client = TestClient(app)
    preflight = client.options(
        "/api/v1/engagements",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert client.post("/api/v1/runs/nope/events", headers=_auth()).status_code == 405
    assert client.post("/api/v1/approvals", headers=_auth(), json={}).status_code == 405
    assert (
        client.patch("/api/v1/tool-calls/nope", headers=_auth(), json={}).status_code
        == 405
    )
    assert client.delete("/api/v1/artifacts/nope", headers=_auth()).status_code == 405


def test_typed_crud_revision_and_overview(api):
    client, _, _ = api
    created = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "API engagement"}
    )
    assert created.status_code == 201
    engagement = created.json()
    engagement_id = engagement["id"]

    patched = client.patch(
        f"/api/v1/engagements/{engagement_id}",
        headers=_auth(),
        json={"changes": {"description": "updated"}, "expected_revision": 1},
    )
    assert patched.status_code == 200
    assert patched.json()["revision"] == 2
    assert patched.json()["description"] == "updated"

    stale = client.patch(
        f"/api/v1/engagements/{engagement_id}",
        headers=_auth(),
        json={"changes": {"description": "stale"}, "expected_revision": 1},
    )
    assert stale.status_code == 409
    overview = client.get(
        f"/api/v1/engagements/{engagement_id}/overview", headers=_auth()
    )
    assert overview.status_code == 200
    assert overview.json()["counts"]["engagements"] == 1

    assert (
        client.delete(
            f"/api/v1/engagements/{engagement_id}", headers=_auth()
        ).status_code
        == 204
    )
    assert (
        client.get(f"/api/v1/engagements/{engagement_id}", headers=_auth()).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/engagements/{engagement_id}/overview",
            headers=_auth(),
        ).status_code
        == 404
    )


def test_run_event_rest_and_authenticated_websocket_replay(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Event replay"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="Replay events"))
    for number in (1, 2):
        response = client.post(
            f"/api/v1/runs/{run.id}/events",
            headers=_auth(),
            json={
                "event_type": "task.progress",
                "payload": {"number": number},
                "idempotency_key": f"event-{number}",
            },
        )
        assert response.status_code == 201
        assert response.json()["sequence"] == number

    replay = client.get(f"/api/v1/runs/{run.id}/events?after=1", headers=_auth()).json()
    assert [event["sequence"] for event in replay["events"]] == [2]

    with client.websocket_connect(
        f"/api/v1/runs/{run.id}/events/ws?after=0",
        subprotocols=["nebula.events.v1", "nebula.auth.dGVzdC10b2tlbg"],
    ) as websocket:
        assert websocket.accepted_subprotocol == "nebula.events.v1"
        assert websocket.receive_json()["event"]["sequence"] == 1
        assert websocket.receive_json()["event"]["sequence"] == 2
        assert websocket.receive_json() == {
            "kind": "replay_complete",
            "after_sequence": 2,
        }

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/runs/{run.id}/events/ws"):
            pass
    assert exc_info.value.code == 4401


def test_artifact_content_and_openapi_contract(api):
    client, store, artifacts = api
    engagement = client.post(
        "/api/v1/engagements", headers=_auth(), json={"name": "Artifacts"}
    ).json()
    artifact = artifacts.put_bytes(
        b"evidence", engagement_id=engagement["id"], filename="proof.txt"
    )
    store.create(artifact)
    response = client.get(f"/api/v1/artifacts/{artifact.id}/content", headers=_auth())
    assert response.status_code == 200
    assert response.content == b"evidence"

    schema = client.get("/openapi.json").json()
    assert "/api/v1/providers" in schema["paths"]
    assert "/api/v1/findings" in schema["paths"]
    request_schema = schema["paths"]["/api/v1/engagements"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/Engagement")


def test_static_workspace_supports_spa_reload_without_masking_missing_assets(
    tmp_path,
):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>Nebula workspace</main>")
    (frontend / "app.js").write_text("console.log('nebula')")
    client = TestClient(
        create_app(
            NebulaStore(tmp_path / "spa.db"),
            auth_token="test-token",
            static_dir=frontend,
        )
    )

    assert client.get("/settings").text == "<main>Nebula workspace</main>"
    assert client.get("/app.js").status_code == 200
    assert client.get("/missing.js").status_code == 404
    api_missing = client.get("/api/v1/not-a-route")
    assert api_missing.status_code == 404
    assert "Nebula workspace" not in api_missing.text


def test_approval_decision_is_revisioned_and_recorded_in_run_ledger(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Approval API"))
    run = store.create(
        AgentRun(engagement_id=engagement.id, objective="Approved operation")
    )
    approval = store.create(
        Approval(
            engagement_id=engagement.id,
            run_id=run.id,
            risk_class=RiskClass.ACTIVE_SCAN,
            exact_request={
                "tool_name": "scan.tcp",
                "arguments": {"ports": [80]},
            },
            target="192.0.2.8",
            policy_rationale="active scan requires a scoped operator decision",
            requested_by="network-specialist",
        )
    )

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={
            "decision": "approve",
            "reason": "Approved port 443 only",
            "edited_arguments": {"ports": [443]},
        },
    )

    assert response.status_code == 200
    decided = response.json()
    assert decided["status"] == ApprovalStatus.EDITED.value
    assert decided["revision"] == approval.revision + 1
    assert decided["exact_request"]["arguments"] == {"ports": [443]}
    assert decided["decided_by"] == "system"
    assert decided["decided_at"] is not None
    persisted = store.get(Approval, approval.id)
    assert persisted.status == ApprovalStatus.EDITED
    events = store.replay_events(run.id)
    assert len(events) == 1
    assert events[0].event_type == "approval.resolved"
    assert events[0].actor_id == "system"
    assert events[0].payload == {
        "approval_id": approval.id,
        "status": "edited",
        "decided_by": "system",
    }
    assert (
        client.post(
            f"/api/v1/approvals/{approval.id}/decision",
            headers=_auth(),
            json={"decision": "reject"},
        ).status_code
        == 409
    )


def test_expired_approval_is_durably_expired_instead_of_approved(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Expired approval"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="Do not run"))
    approval = store.create(
        Approval(
            engagement_id=engagement.id,
            run_id=run.id,
            risk_class=RiskClass.CREDENTIAL_USE,
            exact_request={"tool_name": "login.test", "arguments": {}},
            policy_rationale="credential use always requires approval",
            requested_by="web-specialist",
            expires_at=utc_now() - timedelta(seconds=1),
        )
    )

    response = client.post(
        f"/api/v1/approvals/{approval.id}/decision",
        headers=_auth(),
        json={"decision": "approve"},
    )

    assert response.status_code == 410
    expired = store.get(Approval, approval.id)
    assert expired.status == ApprovalStatus.EXPIRED
    assert expired.decided_by == "system"
    assert expired.decided_at is not None
    events = store.replay_events(run.id)
    assert [event.event_type for event in events] == ["approval.expired"]
    assert events[0].actor_id == "system"


def test_websocket_header_auth_survives_malformed_optional_auth_protocol(api):
    client, store, _ = api
    engagement = store.create(Engagement(name="Empty replay"))
    run = store.create(AgentRun(engagement_id=engagement.id, objective="No events yet"))
    with client.websocket_connect(
        f"/api/v1/runs/{run.id}/events/ws",
        headers=_auth(),
        subprotocols=["nebula.events.v1", "nebula.auth.not!base64"],
    ) as websocket:
        assert websocket.accepted_subprotocol == "nebula.events.v1"
        assert websocket.receive_json() == {
            "kind": "replay_complete",
            "after_sequence": 0,
        }


def test_run_event_routes_reject_a_missing_run(api):
    client, _, _ = api

    assert client.get("/api/v1/runs/missing/events", headers=_auth()).status_code == 404
    assert (
        client.post(
            "/api/v1/runs/missing/events",
            headers=_auth(),
            json={"event_type": "orphan"},
        ).status_code
        == 404
    )
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/runs/missing/events/ws",
            headers=_auth(),
            subprotocols=["nebula.events.v1"],
        ):
            pass
    assert exc_info.value.code == 4404


def test_websocket_rejects_conflicting_valid_credentials(api):
    client, _, _ = api
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/runs/empty/events/ws",
            headers=_auth(),
            subprotocols=[
                "nebula.events.v1",
                "nebula.auth.d3JvbmctdG9rZW4",
            ],
        ):
            pass
    assert exc_info.value.code == 4401
