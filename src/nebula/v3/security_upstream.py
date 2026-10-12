"""Scoped headless browser egress through an optional operator proxy.

The Core policy proxy checks the target before this connector is called. This
module only establishes a tunnel; it never decides whether a target is in scope.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from .credentials import CredentialStore
from .domain import BrowserSession
from .storage import NebulaStore


@dataclass(frozen=True)
class UpstreamSettings:
    scheme: str
    host: str
    port: int
    credential: str | None = None


def parse_upstream_settings(
    url: str, credential: str | None = None
) -> UpstreamSettings:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("upstream proxy has an invalid port") from exc
    if (
        parsed.scheme.lower() not in {"http", "https", "socks5"}
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "upstream proxy requires an HTTP(S) or SOCKS5 URL with a host and explicit port, without credentials or a path"
        )
    if credential is not None:
        username, separator, password = credential.partition(":")
        if (
            not separator
            or not username
            or not password
            or len(username.encode("utf-8")) > 255
            or len(password.encode("utf-8")) > 255
            or "\r" in credential
            or "\n" in credential
        ):
            raise ValueError(
                "upstream proxy credential must contain a bounded username:password pair"
            )
    return UpstreamSettings(parsed.scheme.lower(), parsed.hostname, port, credential)


def _authority(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"{host}:{port}"
    return f"[{address}]:{port}" if address.version == 6 else f"{address}:{port}"


async def _http_tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    credential: str | None,
) -> None:
    authority = _authority(host, port)
    fields = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        "Proxy-Connection: keep-alive",
    ]
    if credential is not None:
        secret = base64.b64encode(credential.encode("utf-8")).decode("ascii")
        fields.append(f"Proxy-Authorization: Basic {secret}")
    writer.write(("\r\n".join(fields) + "\r\n\r\n").encode("ascii"))
    await asyncio.wait_for(writer.drain(), 10)
    response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    if len(response) > 32_768:
        raise ValueError("upstream proxy response exceeded its header limit")
    status = response.split(b"\r\n", 1)[0]
    if not (status.startswith(b"HTTP/1.1 200 ") or status.startswith(b"HTTP/1.0 200 ")):
        raise ValueError("upstream proxy refused the target tunnel")


async def _socks5_tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    credential: str | None,
) -> None:
    selected_method = b"\x02" if credential is not None else b"\x00"
    writer.write(b"\x05\x01" + selected_method)
    await asyncio.wait_for(writer.drain(), 10)
    if await asyncio.wait_for(reader.readexactly(2), 10) != b"\x05" + selected_method:
        raise ValueError("upstream SOCKS5 proxy refused authentication")
    if credential is not None:
        username, password = credential.split(":", 1)
        user_bytes = username.encode("utf-8")
        pass_bytes = password.encode("utf-8")
        writer.write(
            b"\x01"
            + bytes([len(user_bytes)])
            + user_bytes
            + bytes([len(pass_bytes)])
            + pass_bytes
        )
        await asyncio.wait_for(writer.drain(), 10)
        if await asyncio.wait_for(reader.readexactly(2), 10) != b"\x01\x00":
            raise ValueError("upstream SOCKS5 proxy refused credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        encoded = host.encode("idna")
        if not 1 <= len(encoded) <= 255:
            raise ValueError("upstream SOCKS5 destination name is invalid")
        target = b"\x03" + bytes([len(encoded)]) + encoded
    else:
        target = (b"\x01" if address.version == 4 else b"\x04") + address.packed
    writer.write(b"\x05\x01\x00" + target + port.to_bytes(2, "big"))
    await asyncio.wait_for(writer.drain(), 10)
    response = await asyncio.wait_for(reader.readexactly(4), 10)
    if response[:2] != b"\x05\x00" or response[2] != 0:
        raise ValueError("upstream SOCKS5 proxy refused the target tunnel")
    if response[3] == 1:
        length = 4
    elif response[3] == 4:
        length = 16
    elif response[3] == 3:
        length = (await asyncio.wait_for(reader.readexactly(1), 10))[0]
    else:
        raise ValueError("upstream SOCKS5 proxy returned an invalid address")
    await asyncio.wait_for(reader.readexactly(length + 2), 10)


class SecurityUpstreamConnector:
    """Resolve the current session setting and protected secret per connection."""

    def __init__(
        self, store: NebulaStore, session_id: str, credentials: CredentialStore
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.credentials = credentials

    def settings(self) -> UpstreamSettings | None:
        session = self.store.get(BrowserSession, self.session_id)
        if not session.upstream_proxy_enabled:
            return None
        if not session.upstream_proxy_url:
            raise ValueError("enabled upstream proxy has no address")
        credential = (
            self.credentials.resolve(
                session.upstream_proxy_credential_ref
            ).get_secret_value()
            if session.upstream_proxy_credential_ref
            else None
        )
        return parse_upstream_settings(session.upstream_proxy_url, credential)

    async def connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        settings = self.settings()
        if settings is None:
            return await asyncio.wait_for(asyncio.open_connection(host, port), 10)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                settings.host,
                settings.port,
                ssl=ssl.create_default_context()
                if settings.scheme == "https"
                else None,
                server_hostname=settings.host if settings.scheme == "https" else None,
            ),
            10,
        )
        try:
            if settings.scheme == "socks5":
                await _socks5_tunnel(reader, writer, host, port, settings.credential)
            else:
                await _http_tunnel(reader, writer, host, port, settings.credential)
            return reader, writer
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
