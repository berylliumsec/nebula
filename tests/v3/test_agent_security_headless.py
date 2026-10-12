"""Real Core-owned Playwright identity through the separate security sidecar."""

import asyncio
import os
from pathlib import Path

import pytest

from nebula.v3.agent_security import AgentSecuritySessions
from nebula.v3.agent_security_tools import SecurityToolsRuntime
from nebula.v3.browser_host import ManagedBrowserHost
from nebula.v3.browser_security import BrowserSecurityService
from nebula.v3.browserd import BrowserdSecurityRequest
from nebula.v3.domain import Engagement, ScopePolicy
from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.storage import NebulaStore
from pydantic import SecretStr


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


@pytest.mark.skipif(
    not os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    or not os.environ.get("NEBULA_MITMDUMP_EXECUTABLE"),
    reason="verified Playwright Chromium and mitmdump sidecar are not both staged",
)
def test_headless_security_mcp_uses_protected_socks5_upstream(tmp_path, monkeypatch):
    addon = Path(__file__).parents[2] / "security-proxy" / "addon.py"
    monkeypatch.setenv("NEBULA_SECURITY_PROXY_ADDON", str(addon))
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Headless SOCKS5 integration"))
    credentials = CredentialStore()
    reference = credentials.create(
        CredentialCreateRequest(
            secret=SecretStr("security:protected-secret"), persistence="session"
        )
    ).reference
    proxied: list[tuple[str, int]] = []

    async def target(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n"
            b"Content-Type: text/plain\r\nConnection: close\r\n\r\nthrough"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def socks_proxy(reader, writer):
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x02"
            writer.write(b"\x05\x02")
            await writer.drain()
            version, username_length = await reader.readexactly(2)
            assert version == 1
            assert await reader.readexactly(username_length) == b"security"
            password_length = (await reader.readexactly(1))[0]
            assert await reader.readexactly(password_length) == b"protected-secret"
            writer.write(b"\x01\x00")
            await writer.drain()
            version, command, reserved, address_type = await reader.readexactly(4)
            assert (version, command, reserved) == (5, 1, 0)
            if address_type == 1:
                host = ".".join(str(part) for part in await reader.readexactly(4))
            else:
                assert address_type == 3
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode("ascii")
            port = int.from_bytes(await reader.readexactly(2), "big")
            proxied.append((host, port))
            target_reader, target_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            target_writer.write(await reader.readuntil(b"\r\n\r\n"))
            await target_writer.drain()
            writer.write(await target_reader.read())
            await writer.drain()
            target_writer.close()
            await target_writer.wait_closed()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run():
        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        socks_server = await asyncio.start_server(socks_proxy, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        socks_port = socks_server.sockets[0].getsockname()[1]
        scope = store.create(
            ScopePolicy(
                engagement_id=project.id,
                allowed_cidrs=["127.0.0.1/32"],
                allowed_ports=[target_port],
            )
        )
        store.update(Engagement, project.id, {"scope_policy_id": scope.id})
        host = ManagedBrowserHost(
            store,
            tmp_path / "security-browser",
            headless=True,
            credentials=credentials,
        )
        service = AgentSecuritySessions(store, host)
        runtime = SecurityToolsRuntime(store, service)
        try:
            session = await service.ensure(project.id)
            result = await runtime.call_tool(
                project.id,
                "upstream_proxy",
                {
                    "enabled": True,
                    "url": f"socks5://127.0.0.1:{socks_port}",
                    "credential_ref": reference,
                },
            )
            assert result["structuredContent"]["upstream_proxy_configured"] is True
            response = await service.request(
                project.id,
                BrowserdSecurityRequest(
                    identity_id=session.identity_id,
                    method="GET",
                    url=f"http://127.0.0.1:{target_port}/through-socks",
                ),
            )
            assert response.status_code == 200
            assert response.body_base64 == "dGhyb3VnaA=="
            assert proxied == [("127.0.0.1", target_port)]
            assert len(BrowserSecurityService(store).workspace(project.id).traffic) == 1
        finally:
            await runtime.close()
            await host.close()
            target_server.close()
            socks_server.close()
            await target_server.wait_closed()
            await socks_server.wait_closed()

    asyncio.run(run())
