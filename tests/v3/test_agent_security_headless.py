"""Real Core-owned Playwright identity through the separate security sidecar."""

import asyncio
import os
from pathlib import Path

import pytest

from nebula.v3.agent_security import AgentSecuritySessions
from nebula.v3.browser_host import ManagedBrowserHost
from nebula.v3.browser_security import BrowserSecurityService
from nebula.v3.browserd import BrowserdSecurityRequest
from nebula.v3.domain import Engagement, ScopePolicy
from nebula.v3.storage import NebulaStore


@pytest.mark.skipif(
    not os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    or not os.environ.get("NEBULA_MITMDUMP_EXECUTABLE"),
    reason="verified Playwright Chromium and mitmdump sidecar are not both staged",
)
def test_real_headless_identity_captures_only_its_own_scoped_request(
    tmp_path, monkeypatch
):
    addon = Path(__file__).parents[2] / "security-proxy" / "addon.py"
    monkeypatch.setenv("NEBULA_SECURITY_PROXY_ADDON", str(addon))
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Headless integration"))

    async def fixture(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n"
            b"Content-Type: text/plain\r\nConnection: close\r\n\r\nheadless"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run():
        server = await asyncio.start_server(fixture, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        scope = store.create(
            ScopePolicy(
                engagement_id=project.id,
                allowed_cidrs=["127.0.0.1/32"],
                allowed_ports=[port],
            )
        )
        store.update(Engagement, project.id, {"scope_policy_id": scope.id})
        host = ManagedBrowserHost(store, tmp_path / "security-browser", headless=True)
        service = AgentSecuritySessions(store, host)
        try:
            session = await service.ensure(project.id)
            assert session.runtime_kind == "agent_security"
            response = await service.request(
                project.id,
                BrowserdSecurityRequest(
                    identity_id=session.identity_id,
                    method="GET",
                    url=f"http://127.0.0.1:{port}/fixture",
                ),
            )
            assert response.status_code == 200
            assert response.body_base64 == "aGVhZGxlc3M="
            traffic = BrowserSecurityService(store).workspace(project.id).traffic
            assert len(traffic) == 1
            assert traffic[0].session_id == session.id
            assert await service.stop(project.id, reason="test stop") == 1
            with pytest.raises(ValueError, match="stopped"):
                await service.request(
                    project.id,
                    BrowserdSecurityRequest(
                        identity_id=session.identity_id,
                        method="GET",
                        url=f"http://127.0.0.1:{port}/after-stop",
                    ),
                )
        finally:
            await host.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
