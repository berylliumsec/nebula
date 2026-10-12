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


def test_capture_chain_preserves_scope_and_strips_browser_proxy_credentials():
    async def exercise():
        received = []

        async def capture(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            received.append(header)
            if header.startswith(b"CONNECT "):
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                chunk = await reader.readexactly(4)
                writer.write(chunk)
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(capture, "127.0.0.1", 0)
        capture_port = server.sockets[0].getsockname()[1]
        allowed = []

        def authorize(destination):
            allowed.append(destination)
            if not destination.startswith(
                "http://allowed.test/"
            ) and not destination.startswith("https://allowed.test:443/"):
                raise ValueError("outside scope")

        proxy = BrowserHostProxy(authorize, capture_proxy=("127.0.0.1", capture_port))
        config = await proxy.start()
        browser_port = urlsplit(config["server"]).port
        credentials = base64.b64encode(f"nebula:{config['password']}".encode()).decode()

        async def connect(request):
            reader, writer = await asyncio.open_connection("127.0.0.1", browser_port)
            writer.write(request.replace(b"CREDENTIALS", credentials.encode()))
            await writer.drain()
            return reader, writer

        try:
            reader, writer = await connect(
                b"GET http://outside.test/ HTTP/1.1\r\nProxy-Authorization: Basic CREDENTIALS\r\n\r\n"
            )
            assert b"403" in await asyncio.wait_for(reader.read(), 3)
            writer.close()
            await writer.wait_closed()
            assert received == []

            reader, writer = await connect(
                b"GET http://allowed.test/page HTTP/1.1\r\nProxy-Authorization: Basic CREDENTIALS\r\n\r\n"
            )
            assert (await asyncio.wait_for(reader.read(), 3)).endswith(b"OK")
            writer.close()
            await writer.wait_closed()
            assert received[0].startswith(b"GET http://allowed.test/page HTTP/1.1\r\n")
            assert b"Proxy-Authorization" not in received[0]
            assert config["password"].encode() not in received[0]

            reader, writer = await connect(
                b"CONNECT allowed.test:443 HTTP/1.1\r\nProxy-Authorization: Basic CREDENTIALS\r\n\r\n"
            )
            assert b"200" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            writer.write(b"ping")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), 3) == b"ping"
            writer.close()
            await writer.wait_closed()
            assert received[1].startswith(b"CONNECT allowed.test:443 HTTP/1.1\r\n")
            assert b"Proxy-Authorization" not in received[1]
            assert allowed.count("https://allowed.test:443/") >= 2
        finally:
            await proxy.close()
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())


def test_capture_chain_rejects_non_loopback_or_invalid_ports():
    for address in (("example.test", 8000), ("127.0.0.1", 0)):
        try:
            BrowserHostProxy(lambda _destination: None, capture_proxy=address)
        except ValueError as exc:
            assert "loopback listener" in str(exc)
        else:
            raise AssertionError("Unsafe capture proxy accepted")


def test_capture_proxy_tunnel_denial_never_opens_browser_tunnel():
    async def exercise():
        async def deny(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(deny, "127.0.0.1", 0)
        proxy = BrowserHostProxy(
            lambda _destination: None,
            capture_proxy=("127.0.0.1", server.sockets[0].getsockname()[1]),
        )
        config = await proxy.start()
        credentials = base64.b64encode(f"nebula:{config['password']}".encode()).decode()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", urlsplit(config["server"]).port
        )
        try:
            writer.write(
                f"CONNECT allowed.test:443 HTTP/1.1\r\nProxy-Authorization: Basic {credentials}\r\n\r\n".encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n")
            assert b"200 Connection Established" not in response
        finally:
            writer.close()
            await writer.wait_closed()
            await proxy.close()
            server.close()
            await server.wait_closed()

    asyncio.run(exercise())
