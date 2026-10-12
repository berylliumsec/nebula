"""Real local HTTP and SOCKS5 handshakes for the headless egress connector."""

import asyncio

import pytest
from pydantic import SecretStr

from nebula.v3.credentials import CredentialCreateRequest, CredentialStore
from nebula.v3.domain import BrowserSession, Engagement
from nebula.v3.security_upstream import (
    SecurityUpstreamConnector,
    parse_upstream_settings,
)
from nebula.v3.storage import NebulaStore


def test_upstream_requires_a_bounded_explicit_endpoint_and_credential():
    for value in (
        "http://proxy.test",
        "socks5://user:password@proxy.test:1080",
        "https://proxy.test:8443/path",
        "http://proxy.test:8080/?target=another-host",
    ):
        with pytest.raises(ValueError, match="upstream proxy"):
            parse_upstream_settings(value)
    with pytest.raises(ValueError, match="credential"):
        parse_upstream_settings("socks5://proxy.test:1080", "malformed")


def test_headless_connector_uses_current_http_and_authenticated_socks5_settings(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    project = store.create(Engagement(name="Upstream test"))
    session = store.create(
        BrowserSession(
            engagement_id=project.id,
            identity_id="agent-upstream-test",
            name="Headless security",
            runtime_kind="agent_security",
        )
    )
    credentials = CredentialStore()
    reference = credentials.create(
        CredentialCreateRequest(
            secret=SecretStr("operator:protected-secret"), persistence="session"
        )
    ).reference
    connector = SecurityUpstreamConnector(store, session.id, credentials)
    seen: list[str] = []

    async def http_proxy(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        assert request.startswith(b"CONNECT example.test:443 HTTP/1.1\r\n")
        assert b"Proxy-Authorization" not in request
        seen.append("http")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
        writer.write(b"pong")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def socks_proxy(reader, writer):
        assert await reader.readexactly(3) == b"\x05\x01\x02"
        writer.write(b"\x05\x02")
        await writer.drain()
        version, username_length = await reader.readexactly(2)
        assert version == 1
        assert await reader.readexactly(username_length) == b"operator"
        password_length = (await reader.readexactly(1))[0]
        assert await reader.readexactly(password_length) == b"protected-secret"
        writer.write(b"\x01\x00")
        await writer.drain()
        assert await reader.readexactly(4) == b"\x05\x01\x00\x03"
        host_length = (await reader.readexactly(1))[0]
        assert await reader.readexactly(host_length) == b"example.test"
        assert int.from_bytes(await reader.readexactly(2), "big") == 443
        seen.append("socks5")
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await writer.drain()
        assert await reader.readexactly(4) == b"ping"
        writer.write(b"pong")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run():
        http_server = await asyncio.start_server(http_proxy, "127.0.0.1", 0)
        socks_server = await asyncio.start_server(socks_proxy, "127.0.0.1", 0)
        try:
            for scheme, port, credential_ref in (
                ("http", http_server.sockets[0].getsockname()[1], None),
                ("socks5", socks_server.sockets[0].getsockname()[1], reference),
            ):
                latest = store.get(BrowserSession, session.id)
                store.update(
                    BrowserSession,
                    session.id,
                    {
                        "upstream_proxy_enabled": True,
                        "upstream_proxy_url": f"{scheme}://127.0.0.1:{port}",
                        "upstream_proxy_credential_ref": credential_ref,
                    },
                    expected_revision=latest.revision,
                )
                reader, writer = await connector.connect("example.test", 443)
                writer.write(b"ping")
                await writer.drain()
                assert await reader.readexactly(4) == b"pong"
                writer.close()
                await writer.wait_closed()
        finally:
            http_server.close()
            socks_server.close()
            await http_server.wait_closed()
            await socks_server.wait_closed()

    asyncio.run(run())
    assert seen == ["http", "socks5"]
