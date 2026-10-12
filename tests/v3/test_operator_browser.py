"""Operator screencast control stays separate from the passive Assistant viewer."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.browser_companion import BrowserCompanion, validated_operator_input
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.domain import BrowserIdentity, BrowserSession, Engagement
from nebula.v3.storage import NebulaStore


@pytest.mark.parametrize(
    "event",
    [
        {
            "kind": "mouse",
            "type": "mousePressed",
            "x": 8,
            "y": 10,
            "button": "left",
            "clickCount": 1,
        },
        {"kind": "text", "text": "hello"},
        {"kind": "resize", "width": 1280, "height": 800},
        {
            "kind": "touch",
            "type": "touchStart",
            "touchPoints": [{"id": 0, "x": 12, "y": 14}],
        },
    ],
)
def test_operator_input_accepts_bounded_events(event):
    assert validated_operator_input(event)["kind"] == event["kind"]


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "script", "code": "alert(1)"},
        {"kind": "mouse", "type": "mousePressed", "x": float("inf"), "y": 10},
        {"kind": "mouse", "type": "mousePressed", "x": -1, "y": 10},
        {"kind": "text", "text": "x" * 257},
        {"kind": "resize", "width": 99999, "height": 800},
        {
            "kind": "touch",
            "type": "touchStart",
            "touchPoints": [{"id": 1, "x": 12, "y": 14}],
        },
    ],
)
def test_operator_input_rejects_malformed_or_excessive_events(event):
    with pytest.raises(ValueError):
        validated_operator_input(event)


def test_operator_session_is_distinct_from_assistant_session(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Browser project"))
    service = BrowserCompanion(store, BrowserEngineRegistry([]))

    class Adapter:
        async def ensure_identity(self, identity_id):
            return None

    async def request(self, session_id, operation):
        return {"tabs": []}

    async def adapter(self):
        return Adapter()

    monkeypatch.setattr(BrowserCompanion, "adapter", adapter)
    monkeypatch.setattr(BrowserCompanion, "request", request)

    async def run():
        assistant = await service.open(project.id)
        operator = await service.open(project.id, mode="operator")
        assert assistant["session_id"] != operator["session_id"]
        assert (await service.open(project.id, mode="operator"))[
            "session_id"
        ] == operator["session_id"]
        assert (
            service.operator_session(operator["session_id"]).metadata["browser_surface"]
            == "operator"
        )
        with pytest.raises(ValueError, match="operator browser"):
            service.operator_session(assistant["session_id"])

    asyncio.run(run())


def test_operator_operations_require_auth_and_operator_session(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Browser project"))
    assistant_identity = store.create(
        BrowserIdentity(engagement_id=project.id, name="Assistant")
    )
    operator_identity = store.create(
        BrowserIdentity(engagement_id=project.id, name="Operator")
    )
    assistant = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=assistant_identity.id,
            name="Assistant",
            metadata={"browser_companion_version": 1},
        )
    )
    operator = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=operator_identity.id,
            name="Operator",
            metadata={"browser_companion_version": 1, "browser_surface": "operator"},
        )
    )
    calls = []

    async def request(self, session_id, operation):
        calls.append((session_id, operation.operation))
        return {"tabs": []}

    monkeypatch.setattr(BrowserCompanion, "request", request)
    app = create_app(store, auth_token="operator-test")
    with TestClient(app) as client:
        path = f"/api/v1/browser-companion/{operator.id}/operator-operations"
        assert client.post(path, json={"operation": "tabs"}).status_code == 401
        headers = {"Authorization": "Bearer operator-test"}
        assert (
            client.post(path, headers=headers, json={"operation": "script"}).status_code
            == 422
        )
        assert (
            client.post(path, headers=headers, json={"operation": "fill"}).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/v1/browser-companion/{assistant.id}/operator-operations",
                headers=headers,
                json={"operation": "tabs"},
            ).status_code
            == 409
        )
        assert (
            client.post(path, headers=headers, json={"operation": "tabs"}).status_code
            == 200
        )
        assert calls == [(operator.id, "tabs")]
