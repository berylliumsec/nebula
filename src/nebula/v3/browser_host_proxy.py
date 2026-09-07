"""Private browser egress with live scope checks; no interception or tool surface."""

from __future__ import annotations

import asyncio
import base64
import hmac
import secrets
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit


class BrowserHostProxy:
    def __init__(self, authorize: Callable[[str], None]) -> None:
        self.authorize = authorize
        self.password = secrets.token_urlsafe(32)
        self._authorization = "Basic " + base64.b64encode(
            ("nebula:" + self.password).encode("ascii")
        ).decode("ascii")
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> dict[str, str]:
        if self._server is None:
            self._server = await asyncio.start_server(
                self._accept, "127.0.0.1", 0, limit=32768
            )
        port = self._server.sockets[0].getsockname()[1]
        return {
            "server": f"http://127.0.0.1:{port}",
            "username": "nebula",
            "password": self.password,
        }

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        if len(self._clients) >= 64:
            writer.close()
            await writer.wait_closed()
            return
        self._clients.add(task)
        upstream: asyncio.StreamWriter | None = None
        pumps: list[asyncio.Task] = []
        forwarding = False
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = headers.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            if (
                version not in {"HTTP/1.0", "HTTP/1.1"}
                or not method.isascii()
                or not method.isalpha()
            ):
                raise ValueError("Invalid HTTP request.")
            fields: list[tuple[str, str]] = []
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                if not name or any(character.isspace() for character in name):
                    raise ValueError("Invalid HTTP header.")
                fields.append((name, value.strip()))
            credentials = [
                value for name, value in fields if name.lower() == "proxy-authorization"
            ]
            if len(credentials) != 1 or not hmac.compare_digest(
                credentials[0].encode("iso-8859-1"), self._authorization.encode("ascii")
            ):
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="Nebula browser"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'
                )
                await writer.drain()
                return
            tunnel = method == "CONNECT"
            parsed = urlsplit("https://" + target if tunnel else target)
            if (
                parsed.scheme not in ({"https"} if tunnel else {"http"})
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or (tunnel and (parsed.path or parsed.query))
            ):
                raise ValueError("Unsupported browser destination.")
            port = parsed.port if parsed.port is not None else (443 if tunnel else 80)
            if not 1 <= port <= 65535:
                raise ValueError("Invalid browser destination port.")
            destination = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
            )
            self.authorize(destination)
            remote_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname, port), 10
            )
            assert upstream is not None
            # Revalidate after DNS/connect; no bytes reach a newly revoked scope.
            self.authorize(destination)
            if tunnel:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                outgoing = [f"{method} {path} HTTP/1.1", f"Host: {parsed.netloc}"]
                outgoing.extend(
                    f"{name}: {value}"
                    for name, value in fields
                    if name.lower()
                    not in {
                        "proxy-authorization",
                        "proxy-connection",
                        "connection",
                        "host",
                    }
                )
                upgrade = any(
                    name.lower() == "upgrade" and value.lower() == "websocket"
                    for name, value in fields
                )
                outgoing.append(
                    "Connection: Upgrade" if upgrade else "Connection: close"
                )
                upstream.write(
                    ("\r\n".join(outgoing) + "\r\n\r\n").encode("iso-8859-1")
                )
                await upstream.drain()
            forwarding = True

            async def relay(
                source: asyncio.StreamReader, sink: asyncio.StreamWriter
            ) -> None:
                idle = 0
                while idle < 300:
                    self.authorize(destination)
                    try:
                        chunk = await asyncio.wait_for(source.read(65536), 1)
                    except TimeoutError:
                        # diagnostic-expected: periodic wake checks live revocation.
                        idle += 1
                        continue
                    if not chunk:
                        return
                    idle = 0
                    self.authorize(destination)
                    sink.write(chunk)
                    await asyncio.wait_for(sink.drain(), 10)

            # diagnostic-expected: both relays are cancelled and drained below.
            pumps = [
                # diagnostic-expected: cancelled and drained in the enclosing finally.
                asyncio.create_task(relay(reader, upstream)),
                # diagnostic-expected: cancelled and drained in the enclosing finally.
                asyncio.create_task(relay(remote_reader, writer)),
            ]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        except (
            ValueError,
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            # diagnostic-expected: bounded rejection; never reflect destination or credentials.
            if not forwarding and not writer.is_closing():
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except OSError:
                        # diagnostic-expected: a revoked or disconnected peer is already gone.
                        pass
            self._clients.discard(task)
