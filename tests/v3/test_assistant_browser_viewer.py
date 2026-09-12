"""Operator viewer endpoints cannot mutate the assistant's browser."""

import pytest
from fastapi.testclient import TestClient
from nebula.v3.api import create_app
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
