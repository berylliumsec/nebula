from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.domain import ChatSession, Engagement, ProviderProfile
from nebula.v3.live_voice import LIVE_SESSIONS_URL, create_live_session
from nebula.v3.storage import NebulaStore


def test_live_transport_sends_client_delegation_and_keeps_secret_out_of_result():
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(
            201, json={"session": {"id": "live_123"}, "transport": {"sdp": "answer"}}
        )

    result = asyncio.run(
        create_live_session(
            api_key="private-key",
            project_name="Selected",
            sdp="v=0\r\nclient-offer",
            transport=httpx.MockTransport(upstream),
        )
    )
    assert seen["url"] == LIVE_SESSIONS_URL
    assert seen["auth"] == "Bearer private-key"
    assert seen["body"]["session"]["model"] == "gpt-live-1"
    assert seen["body"]["session"]["delegation"] == {"type": "client"}
    assert seen["body"]["transport"] == {"type": "webrtc", "sdp": "v=0\r\nclient-offer"}
    assert result == {"session_id": "live_123", "sdp": "answer"}
    assert "private-key" not in str(result)


def test_voice_session_requires_auth_and_selected_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="test-token")
    client = TestClient(app)
    body = {"engagement_id": "missing", "sdp": "v=0\r\nclient-offer"}
    assert client.post("/api/v1/voice/session", json=body).status_code == 401
    assert (
        client.post(
            "/api/v1/voice/session",
            headers={"Authorization": "Bearer test-token"},
            json=body,
        ).status_code
        == 404
    )


def test_voice_session_uses_direct_openai_credential_and_roster_is_project_bound(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "core.db")
    first = store.create(Engagement(name="First"))
    second = store.create(Engagement(name="Second"))
    store.create(
        ProviderProfile(
            name="Other endpoint",
            provider_type="openai",
            endpoint="https://example.com/v1",
            secret_ref="env:OTHER_KEY",
        )
    )
    direct = store.create(
        ProviderProfile(
            name="Direct OpenAI", provider_type="openai", secret_ref="env:DIRECT_KEY"
        )
    )
    store.create(
        ChatSession(
            engagement_id=first.id,
            title="First chat",
            provider_profile_id=direct.id,
            model="test",
        )
    )
    second_chat = store.create(
        ChatSession(
            engagement_id=second.id,
            title="Second chat",
            provider_profile_id=direct.id,
            model="test",
        )
    )
    monkeypatch.setenv("OTHER_KEY", "must-not-send")
    monkeypatch.setenv("DIRECT_KEY", "direct-key")
    observed = {}

    async def fake_live(*, api_key: str, project_name: str, sdp: str):
        observed.update(api_key=api_key, project_name=project_name, sdp=sdp)
        return {"session_id": "live_456", "sdp": "answer"}

    monkeypatch.setattr("nebula.v3.api.create_live_session", fake_live)
    client = TestClient(create_app(store, auth_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    offer = "v=0\r\nclient-offer\r\n"
    response = client.post(
        "/api/v1/voice/session",
        headers=headers,
        json={"engagement_id": first.id, "sdp": offer},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"session_id": "live_456", "sdp": "answer"}
    assert observed == {
        "api_key": "direct-key",
        "project_name": "First",
        "sdp": offer,
    }
    roster = client.get(f"/api/v1/voice/work?engagement_id={first.id}", headers=headers)
    assert roster.status_code == 200, roster.text
    assert [chat["title"] for chat in roster.json()["chats"]] == ["First chat"]
    foreign = client.get(
        f"/api/v1/voice/work/chats/{second_chat.id}?engagement_id={first.id}",
        headers=headers,
    )
    assert foreign.status_code == 404


def test_voice_session_reports_missing_key_and_rejects_non_offer(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    client = TestClient(create_app(store, auth_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    invalid = client.post(
        "/api/v1/voice/session",
        headers=headers,
        json={"engagement_id": project.id, "sdp": "not a valid offer"},
    )
    assert invalid.status_code == 422
    missing = client.post(
        "/api/v1/voice/session",
        headers=headers,
        json={"engagement_id": project.id, "sdp": "v=0\r\nclient-offer"},
    )
    assert missing.status_code == 503
    assert "Configure an enabled OpenAI provider" in missing.json()["detail"]


@pytest.mark.parametrize(
    ("upstream_status", "recovery"),
    [
        (400, "fresh offer"),
        (403, "GPT-Live access"),
        (429, "Wait a moment"),
        (503, "service availability"),
    ],
)
def test_voice_upstream_http_error_returns_actionable_502_without_leaking_details(
    tmp_path, monkeypatch, upstream_status, recovery
):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    manager = DiagnosticManager(tmp_path / "diagnostics", watch_settings=False)
    recorded = []
    original_record = manager.record

    def capture_record(level, feature, event_code, message, **fields):
        if event_code == "voice.session_upstream_failed":
            recorded.append(fields)
        return original_record(level, feature, event_code, message, **fields)

    monkeypatch.setattr(manager, "record", capture_record)

    async def rejected_live(*, api_key: str, project_name: str, sdp: str):
        assert api_key == "private-key"
        request = httpx.Request("POST", LIVE_SESSIONS_URL)
        response = httpx.Response(
            upstream_status,
            json={"error": {"message": "private-upstream-message"}},
            request=request,
        )
        raise httpx.HTTPStatusError(
            "private-upstream-message", request=request, response=response
        )

    monkeypatch.setattr("nebula.v3.api.create_live_session", rejected_live)
    try:
        with TestClient(
            create_app(store, auth_token="test-token", diagnostic_manager=manager),
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                "/api/v1/voice/session",
                headers={"Authorization": "Bearer test-token"},
                json={"engagement_id": project.id, "sdp": "v=0\r\nclient-offer"},
            )
        assert response.status_code == 502, response.text
        assert recovery in response.json()["detail"]
        assert "private-upstream-message" not in response.text
        assert "private-key" not in response.text
        assert recorded == [{"metadata": {"http_status": upstream_status}}]
    finally:
        manager.close()
