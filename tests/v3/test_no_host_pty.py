from __future__ import annotations

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore


def test_host_terminal_route_is_not_registered(tmp_path):
    app = create_app(
        NebulaStore(tmp_path / "nebula.db"),
        auth_token="test-token",
    )
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.endswith("/terminal/ws") for path in paths)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/health", headers={"Authorization": "Bearer test-token"}
        )
    assert response.status_code == 200
    assert response.json()["human_pty"] == "unavailable"
