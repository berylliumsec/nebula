from __future__ import annotations
import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore


async def test_host_terminal_route_is_not_registered(tmp_path):
    app = create_app(
        NebulaStore(tmp_path / "nebula.db"),
        auth_token="test-token",
    )
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any(path.endswith("/terminal/ws") for path in paths)
    assert "/api/v1/container-terminals/{session_id}/ws" in paths
    async with TestClient(app) as client:
        response = await client.get(
            "/api/v1/health", headers={"Authorization": "Bearer test-token"}
        )
    assert response.status_code == 200
    assert response.json()["human_pty"] == "unavailable"
    assert response.json()["container_terminal"] == "unavailable"


pytestmark = pytest.mark.anyio
