"""Shared paired-device transport; independent of Core and terminal rendering."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import ssl
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Generator, Iterator
from urllib.parse import quote
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import typer


class ClientError(Exception):
    """An actionable failure that can be shown without a traceback."""


class ApiError(ClientError):
    def __init__(self, message: str, status: int, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


def terminal_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def origin(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ClientError(
            "Use a Core origin such as https://nebula.example:8765."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/", "/api/v1", "/api/v1/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ClientError(
            "Use a Core HTTP(S) origin without credentials, a query or fragment."
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = (
        f":{port}" if port and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    )
    return f"{parsed.scheme}://{host}{suffix}"


def loopback(value: str) -> bool:
    host = urlsplit(value).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    path: Path
    data: dict[str, Any]
    core: str | None

    @property
    def entry(self) -> dict[str, Any]:
        return self.data.get("cores", {}).get(self.core, {})

    def save(self, entry: dict[str, Any] | None) -> None:
        self.data.setdefault("cores", {})
        if entry is None:
            self.data["cores"].pop(self.core, None)
            if self.data.get("active_core") == self.core:
                self.data["active_core"] = next(iter(self.data["cores"]), None)
        else:
            self.data["cores"][self.core] = entry
            self.data["active_core"] = self.core
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".chat-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def require_core(settings: Settings) -> str:
    if not settings.core:
        raise ClientError("Choose a Core with --core https://HOST:PORT, then run pair.")
    return settings.core


def check_response(response: httpx.Response, *, pairing: bool = False) -> None:
    if response.is_success:
        return
    if response.is_redirect:
        raise ClientError(
            "Core redirected the request. Use its final HTTPS origin and pair there."
        )
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    if isinstance(detail, dict):
        detail = (
            detail.get("message")
            or detail.get("detail")
            or "Core rejected the supplied values."
        )
    elif isinstance(detail, list):
        detail = (
            "; ".join(
                ".".join(str(part) for part in item.get("loc", []))
                + ": "
                + str(item.get("msg", "Invalid value"))
                for item in detail
                if isinstance(item, dict)
            )
            or "Core rejected the supplied values."
        )
    if not isinstance(detail, str):
        detail = "Core rejected the supplied values."
    if response.status_code == 401 and not pairing:
        detail = "Pairing expired or was revoked. Run pair again for this Core."
    raise ApiError(
        f"Core ({response.status_code}): {terminal_text(detail)[:1500]}",
        response.status_code,
    )


class _MutationIdempotency(httpx.Auth):
    """Give each dispatched mutation an identity without replaying it."""

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if "Idempotency-Key" not in request.headers:
                request.headers["Idempotency-Key"] = str(uuid4())
        yield request


def connection_options(
    settings: Settings,
    *,
    paired: bool = True,
    ca_cert: Path | None = None,
    allow_http: bool = False,
) -> dict[str, Any]:
    core = require_core(settings)
    entry = settings.entry if paired else {}
    if paired and not all(
        entry.get(key) for key in ("device_token", "csrf_token", "device_id")
    ):
        raise ClientError("This Core is not paired. Run pair first.")
    if core.startswith("http:") and not (allow_http or entry.get("allow_http")):
        raise ClientError(
            "Pairing requires HTTPS. For an explicitly enabled test LAN, use pair --allow-http."
        )
    certificate = ca_cert or entry.get("ca_cert")
    verification: ssl.SSLContext | bool = (
        ssl.create_default_context(cafile=str(certificate)) if certificate else True
    )
    headers = {"Origin": core, "User-Agent": "nebula-chat"}
    cookies = {}
    if paired:
        headers["X-Nebula-CSRF"] = entry["csrf_token"]
        cookies = {
            "nebula_device": entry["device_token"],
            "nebula_csrf": entry["csrf_token"],
        }
    return dict(
        base_url=core + "/api/v1/",
        auth=_MutationIdempotency(),
        headers=headers,
        cookies=cookies,
        verify=verification,
        follow_redirects=False,
        timeout=httpx.Timeout(30, read=90),
    )


@contextmanager
def connection(
    settings: Settings,
    *,
    paired: bool = True,
    ca_cert: Path | None = None,
    allow_http: bool = False,
) -> Iterator[httpx.Client]:
    with httpx.Client(
        **connection_options(
            settings, paired=paired, ca_cert=ca_cert, allow_http=allow_http
        )
    ) as client:
        yield client


def request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    check_response(response)
    return response.json() if response.content else None


@contextmanager
def user_errors() -> Iterator[None]:
    try:
        yield
    except (ClientError, OSError, httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, httpx.HTTPError):
            message = "Cannot reach Core securely. Check its address, connection and CA certificate, then reconnect. No message was automatically retried."
        else:
            message = terminal_text(exc)
        typer.echo(message, err=True)
        raise typer.Exit(1) from exc


def key(value: Any) -> str:
    """Encode opaque identifiers and host paths as one URL component."""
    return quote(str(value), safe="")


class AsyncApi:
    """One exact-origin connection. Writes and submissions are never retried."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        options = connection_options(settings)
        if transport is not None:
            options["transport"] = transport
        self.client = httpx.AsyncClient(**options)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if path.startswith(("/", "http:", "https:")):
            raise ClientError("Use a path within this Core's API.")
        response = await self.client.request(method, path, **kwargs)
        check_response(response)
        return response.json() if response.content else None

    async def page(
        self, path: str, *, offset: int = 0, **params: Any
    ) -> list[dict[str, Any]]:
        return await self.request(
            "GET", path, params={**params, "offset": offset, "limit": 100}
        )

    async def events(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        # Core can spend minutes preparing a turn before acceptance; once
        # accepted its stream includes heartbeats. This is never a retry budget.
        async with self.client.stream(
            method,
            path,
            params=params,
            timeout=httpx.Timeout(30, read=300),
            headers={"Accept": "text/event-stream"},
            json=body,
        ) as response:
            if not response.is_success:
                await response.aread()
                check_response(response)
            event = "message"
            data: list[str] = []
            size = 0
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        try:
                            payload = json.loads("\n".join(data))
                        except ValueError as exc:
                            raise ClientError(
                                "Core sent an invalid stream. Review /status and /history before retrying."
                            ) from exc
                        if not isinstance(payload, dict):
                            raise ClientError(
                                "Core sent an invalid stream object. Review /status."
                            )
                        yield event, payload
                    event, data, size = "message", [], 0
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    size += len(line)
                    if size > 8 * 1024 * 1024:
                        raise ClientError(
                            "Core sent an oversized stream frame. Reconnect and review /history."
                        )
                    data.append(line[5:].removeprefix(" "))
