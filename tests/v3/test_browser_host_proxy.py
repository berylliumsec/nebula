import asyncio
import base64
from urllib.parse import urlsplit

from nebula.v3.browser_host_proxy import BrowserHostProxy


def test_proxy_authentication_scope_and_header_isolation():
    async def exercise():
        received = []

        async def target(reader, writer):
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(target, "127.0.0.1", 0)
        origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"

        def authorize(url):
            if not url.startswith(origin + "/"):
                raise ValueError("outside scope")

        proxy = BrowserHostProxy(authorize)
        config = await proxy.start()
        port = urlsplit(config["server"]).port
        authorization = base64.b64encode(
            f"nebula:{config['password']}".encode()
        ).decode()

        async def request(destination, credentials=None):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            headers = f"GET {destination} HTTP/1.1\r\nHost: ignored.test\r\n"
            if credentials:
                headers += f"Proxy-Authorization: Basic {credentials}\r\n"
            writer.write((headers + "\r\n").encode())
            await writer.drain()
            result = await asyncio.wait_for(reader.read(), 3)
            writer.close()
            await writer.wait_closed()
            return result

        try:
            assert b"407" in await request(origin + "/")
            assert b"407" in await request(origin + "/", "wrong")
            assert b"403" in await request("http://outside.test/", authorization)
            assert received == []
            assert (await request(origin + "/page?value=1", authorization)).endswith(
                b"OK"
            )
            assert len(received) == 1
            assert received[0].startswith(b"GET /page?value=1 HTTP/1.1\r\n")
            assert b"ignored.test" not in received[0]
            assert b"Proxy-Authorization" not in received[0]
            assert config["password"].encode() not in received[0]
        finally:
            await proxy.close()
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())


def test_tunnel_rechecks_revocation_and_closes_active_connections():
    async def exercise():
        revoked = False
        destinations = []

        async def target(reader, writer):
            try:
                while chunk := await reader.read(100):
                    writer.write(chunk)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = server.sockets[0].getsockname()[1]

        def authorize(url):
            destinations.append(url)
            if revoked or url != f"https://127.0.0.1:{target_port}/":
                raise ValueError("scope revoked")

        proxy = BrowserHostProxy(authorize)
        config = await proxy.start()
        credentials = base64.b64encode(f"nebula:{config['password']}".encode()).decode()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", urlsplit(config["server"]).port
        )
        try:
            writer.write(
                f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nProxy-Authorization: Basic {credentials}\r\n\r\n".encode()
            )
            await writer.drain()
            assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            writer.write(b"controlled tunnel bytes")
            await writer.drain()
            assert (
                await asyncio.wait_for(reader.readexactly(23), 2)
                == b"controlled tunnel bytes"
            )
            revoked = True
            assert await asyncio.wait_for(reader.read(), 3) == b""
            assert len(destinations) >= 3
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()
            server.close()
            await server.wait_closed()
        assert not proxy._clients

    asyncio.run(exercise())
