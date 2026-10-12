"""Operator viewer endpoints cannot mutate the assistant's browser."""

import pytest
from fastapi.testclient import TestClient
from nebula.v3.api import create_app
from nebula.v3.browser_companion import BrowserCompanion
from nebula.v3.domain import BrowserIdentity, BrowserSession, Engagement
from nebula.v3.storage import NebulaStore


@pytest.mark.parametrize(
    "operation",
    ["navigate", "new_tab", "close_tab", "click", "fill", "select", "upload"],
)
def test_operator_page_operations_are_retired(tmp_path, operation):
    app = create_app(NebulaStore(tmp_path / "core.db"), auth_token="viewer-test")
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/browser-companion/unused/operations",
            headers={"Authorization": "Bearer viewer-test"},
            json={"operation": operation, "url": "http://localhost/fixture"},
        )
        assert result.status_code == 403
        assert "Assistant" in result.json()["detail"]


def test_viewer_preserves_auth_and_cannot_propose_or_select(tmp_path):
    app = create_app(NebulaStore(tmp_path / "core.db"), auth_token="viewer-test")
    with TestClient(app) as client:
        base = "/api/v1/browser-companion/unused"
        headers = {"Authorization": "Bearer viewer-test"}
        assert (
            client.post(
                base + "/operations", json={"operation": "navigate"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                base + "/actions", headers=headers, json={"operation": "upload"}
            ).status_code
            == 403
        )
        assert client.put(base + "/active-tab/tab", headers=headers).status_code == 403


def test_working_knowledge_is_internal_and_does_not_change_permissions():
    from nebula.v3.application_model.workflow import BROWSER_MODEL_WORKFLOW

    for invariant in ["browser.companion", "model.transact"]:
        assert invariant in BROWSER_MODEL_WORKFLOW


def test_linked_navigation_requires_auth_and_rejects_unsafe_urls(tmp_path, monkeypatch):
    calls = []

    async def request(self, session_id, operation):
        calls.append((session_id, operation))
        return {"url": operation.url, "title": "Linked page"}

    monkeypatch.setattr(BrowserCompanion, "request", request)
    app = create_app(NebulaStore(tmp_path / "core.db"), auth_token="viewer-test")
    with TestClient(app) as client:
        path = "/api/v1/browser-companion/browser-1/linked-navigation"
        valid = {"tab_id": "tab-1", "url": "https://example.test/review"}
        assert client.post(path, json=valid).status_code == 401
        for url in [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:password@example.test/",
            "https://example.test/\nHost:other.test",
        ]:
            refused = client.post(
                path,
                headers={"Authorization": "Bearer viewer-test"},
                json={**valid, "url": url},
            )
            assert refused.status_code == 409
        assert calls == []
        opened = client.post(
            path, headers={"Authorization": "Bearer viewer-test"}, json=valid
        )
        assert opened.status_code == 200
        assert opened.json()["url"] == valid["url"]
        assert calls[0][0] == "browser-1"
        assert calls[0][1].operation == "navigate"
        assert calls[0][1].tab_id == "tab-1"


def test_linked_navigation_keeps_project_scope_authoritative(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Scoped browser"))
    identity = store.create(BrowserIdentity(engagement_id=project.id, name="Browser"))
    session = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id=identity.id,
            name="Browser",
            metadata={"browser_companion_version": 1},
        )
    )

    async def adapter(self):
        return object()

    async def forbidden(*args, **kwargs):
        pytest.fail("Out-of-scope link reached the browser runtime")

    monkeypatch.setattr(BrowserCompanion, "adapter", adapter)
    monkeypatch.setattr(BrowserCompanion, "_companion_call", forbidden)
    app = create_app(store, auth_token="viewer-test")
    with TestClient(app) as client:
        refused = client.post(
            f"/api/v1/browser-companion/{session.id}/linked-navigation",
            headers={"Authorization": "Bearer viewer-test"},
            json={"tab_id": "tab-1", "url": "https://example.test/review"},
        )
        assert refused.status_code == 409
        assert "scope" in refused.json()["detail"].lower()
