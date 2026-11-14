"""Bounded, authenticated local forwarding independent of execution lifetime."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import hmac
from http.cookiejar import CookieJar
import json
import os
import secrets
import stat
import time
from typing import Any, Literal
from builtins import BaseExceptionGroup

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .async_utils import finish_cleanup
from .deployment_protocol import IPC_PROTOCOL
from .diagnostics import record_caught_exception, record_diagnostic
from .runtime_ipc import RuntimeIpcError

_PREFIX = b"x-nebula-internal-"
# Runtime endpoints that may hold forwarding channels at once. Idle endpoints
# are evicted least-recently-used, so this bounds connections, not projects.
_ENDPOINT_BUDGET = 8
_HOPS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_FORWARDED = {
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-proto",
    b"x-forwarded-host",
    b"x-forwarded-port",
    b"x-real-ip",
}
_WEBSOCKET = {
    b"sec-websocket-key",
    b"sec-websocket-version",
    b"sec-websocket-extensions",
    b"sec-websocket-protocol",
}


def _signature(
    secret: bytes, stamp: str, nonce: str, scheme: str, lane: str, client: str
) -> str:
    return hmac.new(
        secret,
        b"\0".join(
            part.encode()
            for part in (IPC_PROTOCOL, "http", stamp, nonce, scheme, lane, client)
        ),
        hashlib.sha256,
    ).hexdigest()


async def _runtime_unavailable(scope, receive, send) -> None:
    await JSONResponse(
        {
            "detail": "The runtime connection is unavailable. Reconnect to read durable progress."
        },
        status_code=503,
    )(scope, receive, send)


def _hop_headers(headers: list[tuple[bytes, bytes]]) -> set[bytes]:
    extra: set[bytes] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            extra.update(item.strip().lower() for item in value.split(b","))
    return _HOPS | extra


def _client_address(raw: str) -> tuple[str, int] | None:
    if raw == "":
        return None
    if len(raw) > 512:
        raise ValueError("client address exceeds its bound")
    value = json.loads(raw)
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not 1 <= len(value[0]) <= 255
        or type(value[1]) is not int
        or not 0 <= value[1] <= 65535
    ):
        raise ValueError("invalid private client address")
    return value[0], value[1]


def internal_headers(
    secret: bytes,
    scheme: str,
    *,
    lane: str = "read",
    client: tuple[str, int] | None = None,
    clock: Callable[[], float] = time.time,
) -> list[tuple[bytes, bytes]]:
    if (
        len(secret) != 32
        or scheme not in {"http", "https", "ws", "wss"}
        or lane not in {"read", "work", "control"}
    ):
        raise ValueError(
            "private forwarding requires a 256-bit key and explicit scheme/lane"
        )
    address = json.dumps(client, separators=(",", ":")) if client is not None else ""
    _client_address(address)
    stamp, nonce = str(int(clock())), secrets.token_hex(24)
    return [
        (b"x-nebula-internal-protocol", IPC_PROTOCOL.encode()),
        (b"x-nebula-internal-time", stamp.encode()),
        (b"x-nebula-internal-nonce", nonce.encode()),
        (b"x-nebula-internal-scheme", scheme.encode()),
        (b"x-nebula-internal-lane", lane.encode()),
        (b"x-nebula-internal-client", address.encode()),
        (
            b"x-nebula-internal-mac",
            _signature(secret, stamp, nonce, scheme, lane, address).encode(),
        ),
    ]


class PrivateRuntimeHttp:
    """Authenticate an owner-only local channel, preserving public authority.

    Client metadata and lane are authenticated, never taken from public forwarded
    headers. Control replay capacity is separate from work and viewer traffic.
    """

    def __init__(
        self,
        app,
        secret: bytes,
        *,
        clock: Callable[[], float] = time.time,
        nonce_capacity: int = 4096,
        control_nonce_capacity: int = 256,
    ):
        if (
            len(secret) != 32
            or not 1 <= nonce_capacity <= 4096
            or not 1 <= control_nonce_capacity <= 4096
        ):
            raise ValueError(
                "private forwarding requires bounded capacity and a 256-bit key"
            )
        self.app, self.secret, self.clock = app, secret, clock
        self.seen: dict[str, OrderedDict[str, int]] = {
            lane: OrderedDict() for lane in ("control", "read", "work")
        }
        self.capacities = {
            "read": nonce_capacity,
            "work": nonce_capacity,
            "control": control_nonce_capacity,
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        headers = {
            name.lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        protocol = headers.get(b"x-nebula-internal-protocol")
        stamp, nonce = (
            headers.get(b"x-nebula-internal-time", ""),
            headers.get(b"x-nebula-internal-nonce", ""),
        )
        scheme, supplied = (
            headers.get(b"x-nebula-internal-scheme", ""),
            headers.get(b"x-nebula-internal-mac", ""),
        )
        lane, address = (
            headers.get(b"x-nebula-internal-lane", ""),
            headers.get(b"x-nebula-internal-client", ""),
        )
        now = int(self.clock())
        allowed_schemes = (
            {"ws", "wss"} if scope["type"] == "websocket" else {"http", "https"}
        )
        valid = (
            protocol == IPC_PROTOCOL
            and stamp.isdecimal()
            and len(stamp) <= 20
            and abs(now - int(stamp)) <= 30
            and len(nonce) == 48
            and all(char in "0123456789abcdef" for char in nonce)
            and len(supplied) == 64
            and all(char in "0123456789abcdef" for char in supplied)
            and scheme in allowed_schemes
            and lane in self.seen
            and len(address) <= 512
            and hmac.compare_digest(
                supplied, _signature(self.secret, stamp, nonce, scheme, lane, address)
            )
        )
        client = None
        try:
            client = _client_address(address)
        except (ValueError, TypeError):
            # diagnostic-expected: malformed authenticated client metadata fails private-channel authentication closed.
            valid = False
        for seen in self.seen.values():
            for old, timestamp in list(seen.items()):
                if timestamp < now - 30:
                    seen.pop(old)
        status = (
            401
            if not valid
            else 409
            if any(nonce in seen for seen in self.seen.values())
            else 429
            if len(self.seen[lane]) >= self.capacities[lane]
            else 0
        )
        if status:
            if scope["type"] == "websocket":
                await send(
                    {
                        "type": "websocket.close",
                        "code": 4401,
                        "reason": "authenticated private channel required",
                    }
                )
            else:
                await JSONResponse(
                    {"detail": "authenticated private runtime channel required"},
                    status_code=status,
                )(scope, receive, send)
            return
        self.seen[lane][nonce] = int(stamp)
        selected = {
            **scope,
            "scheme": scheme,
            "client": client,
            "headers": [
                (name, value)
                for name, value in scope.get("headers", [])
                if not name.lower().startswith(_PREFIX)
                and name.lower() not in _FORWARDED
            ],
        }
        await self.app(selected, receive, send)


@dataclass(frozen=True)
class HttpUpstream:
    endpoint: str
    credential: bytes
    lane: Literal["control", "read", "work"] = "read"

    def socket_path(self) -> str:
        if not self.endpoint.startswith("unix:///") or "\x00" in self.endpoint:
            raise ValueError(
                "runtime forwarding accepts only an explicit local Unix socket"
            )
        return self.endpoint[7:]


class _NoCookieJar(CookieJar):
    def set_cookie(self, cookie, *args, **kwargs):
        return None

    def set_cookie_if_ok(self, cookie, request):
        return None


def _require_private_socket_directory(path: str) -> None:
    """Forward viewer credentials only into a directory no other user can enter.

    A recorded worker socket can outlive its directory, for example after
    /tmp cleanup at boot. Another local user could recreate that public path
    and would then receive forwarded Authorization and Cookie headers.
    """
    try:
        details = os.lstat(os.path.dirname(path))
    except OSError as error:
        raise RuntimeIpcError(
            "The runtime socket directory is not private; saved work remains queued"
        ) from error
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise RuntimeIpcError(
            "The runtime socket directory is not private; saved work remains queued"
        )


class RuntimeHttpProxy:
    """A viewer owns connections, never an upstream producer or process."""

    def __init__(
        self,
        select: Callable[[dict], Awaitable[HttpUpstream]],
        *,
        transport_factory: Callable[[str], Any] | None = None,
        max_websockets: int = 64,
    ):
        if not 1 <= max_websockets <= 256:
            raise ValueError("WebSocket capacity must be between 1 and 256")
        self.select, self.transport_factory = select, transport_factory
        self.clients: dict[tuple[str, bool], httpx.AsyncClient] = {}
        self.websocket_tasks: dict[asyncio.Task, str] = {}
        self.websockets: dict[asyncio.Task, ClientConnection] = {}
        self.max_websockets = max_websockets
        self.closed = False
        self.retired_endpoints: OrderedDict[str, None] = OrderedDict()
        # Forwarded HTTP exchanges in flight per endpoint, and every endpoint
        # holding pooled clients, ordered from least to most recently admitted.
        self.active: dict[str, int] = {}
        self.recent: OrderedDict[str, None] = OrderedDict()

    def _check_open(self, endpoint: str) -> None:
        if self.closed or endpoint in self.retired_endpoints:
            raise httpx.PoolTimeout("this runtime forwarding channel has closed")

    def _admit_client(self, endpoint: str) -> list[httpx.AsyncClient]:
        """Admit an endpoint to the pooled-client budget; return evicted clients.

        The budget bounds pooled HTTP channels, not the projects a viewer may
        read. When it is full, the least recently used endpoint with no
        forwarded exchange in flight gives up its clients and reconnects on its
        next use. Only a budget of busy endpoints refuses. Viewer WebSockets
        hold their own connections and are bounded by `max_websockets`.
        """
        self._check_open(endpoint)
        endpoints = {key[0] for key in self.clients}
        for stale in [item for item in self.recent if item not in endpoints]:
            del self.recent[stale]
        evicted: list[httpx.AsyncClient] = []
        if endpoint not in endpoints and len(endpoints) >= _ENDPOINT_BUDGET:
            idle = next(
                (item for item in self.recent if not self.active.get(item)), None
            )
            if idle is None:
                raise httpx.PoolTimeout(
                    "the private runtime endpoint budget is occupied"
                )
            evicted = [
                self.clients.pop(key) for key in list(self.clients) if key[0] == idle
            ]
            del self.recent[idle]
        self.recent[endpoint] = None
        self.recent.move_to_end(endpoint)
        return evicted

    async def _close_evicted(self, clients: list[httpx.AsyncClient]) -> None:
        try:
            await finish_cleanup(
                self._close_resources(clients, set(), asyncio.current_task())
            )
        except BaseExceptionGroup as error:
            # The evicted channel was idle; its close failure must not fail the
            # request that replaced it, but caller cancellation still applies.
            cancelled, failures = error.split(asyncio.CancelledError)
            if failures is not None:
                record_caught_exception(
                    "api",
                    "api.runtime.channel_eviction_failed",
                    "An idle runtime channel could not close cleanly.",
                    failures,
                    stage="cleanup",
                )
            if cancelled is not None:
                raise asyncio.CancelledError() from error

    def _release(self, endpoint: str) -> None:
        remaining = self.active.get(endpoint, 0) - 1
        if remaining > 0:
            self.active[endpoint] = remaining
        else:
            self.active.pop(endpoint, None)

    async def _client(self, target: HttpUpstream) -> httpx.AsyncClient:
        """Reserve the endpoint for one exchange; the caller must `_release` it."""
        evicted = self._admit_client(target.endpoint)
        control = target.lane == "control"
        key = (target.endpoint, control)
        try:
            if key not in self.clients:
                limit = 16 if control else 64
                transport = (
                    self.transport_factory(target.endpoint)
                    if self.transport_factory
                    else httpx.AsyncHTTPTransport(
                        uds=target.socket_path(),
                        limits=httpx.Limits(
                            max_connections=limit,
                            max_keepalive_connections=min(limit, 16),
                        ),
                    )
                )
                self.clients[key] = httpx.AsyncClient(
                    transport=transport,
                    base_url="http://nebula-runtime",
                    trust_env=False,
                    cookies=_NoCookieJar(),
                    timeout=httpx.Timeout(connect=5, read=None, write=30, pool=5),
                )
        except BaseException:
            if evicted:
                await self._close_evicted(evicted)
            raise
        client = self.clients[key]
        # Reserve before the first await so a concurrent admission cannot
        # evict the channel this exchange is about to use.
        self.active[target.endpoint] = self.active.get(target.endpoint, 0) + 1
        if evicted:
            try:
                await self._close_evicted(evicted)
                # Retirement or shutdown may have closed this client meanwhile.
                self._check_open(target.endpoint)
                if self.clients.get(key) is not client:
                    raise httpx.PoolTimeout(
                        "this runtime forwarding channel has closed"
                    )
            except BaseException:
                self._release(target.endpoint)
                raise
        return client

    async def _close_resources(
        self, clients: list[httpx.AsyncClient], endpoints: set[str] | None, caller
    ):
        tasks = [
            task
            for task, endpoint in list(self.websocket_tasks.items())
            if (endpoints is None or endpoint in endpoints)
        ]
        connections = [
            self.websockets[task] for task in tasks if task in self.websockets
        ]
        pending = [
            task for task in tasks if task not in self.websockets and task is not caller
        ]
        for task in pending:
            task.cancel()
        results = await asyncio.gather(
            *(client.aclose() for client in clients),
            *(
                connection.close(code=1012, reason="Runtime viewer channel closed")
                for connection in connections
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        tasks = [task for task in tasks if task is not caller]
        if tasks:
            _done, waiting = await asyncio.wait(tasks, timeout=5)
            for task in waiting:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures.extend(
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            )
        if failures:
            raise BaseExceptionGroup(
                "Private runtime channel cleanup did not complete", failures
            )

    async def close(self):
        self.closed = True
        clients, self.clients = self.clients, {}
        await finish_cleanup(
            self._close_resources(list(clients.values()), None, asyncio.current_task())
        )

    async def discard_endpoint(self, endpoint: str):
        self.retired_endpoints[endpoint] = None
        while len(self.retired_endpoints) > 256:
            self.retired_endpoints.popitem(last=False)
        clients = [
            client
            for key in list(self.clients)
            if key[0] == endpoint
            and (client := self.clients.pop(key, None)) is not None
        ]
        await finish_cleanup(
            self._close_resources(clients, {endpoint}, asyncio.current_task())
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        target = await self.select(scope)
        socket_path = target.socket_path()
        if self.transport_factory is None or scope["type"] == "websocket":
            _require_private_socket_directory(socket_path)
        if scope["type"] == "websocket":
            await self._websocket(target, scope, receive, send)
            return
        headers = scope.get("headers", [])
        excluded = _hop_headers(headers) | _FORWARDED
        outgoing = [
            (name, value)
            for name, value in headers
            if name.lower() not in excluded and not name.lower().startswith(_PREFIX)
        ]
        outgoing.extend(
            internal_headers(
                target.credential,
                scope.get("scheme", "http"),
                lane=target.lane,
                client=scope.get("client"),
            )
        )
        request = Request(scope, receive)
        raw_path = scope.get("raw_path", scope["path"].encode("utf-8"))
        if scope.get("query_string"):
            raw_path += b"?" + scope["query_string"]
        try:
            client = await self._client(target)
        except httpx.PoolTimeout:
            # diagnostic-expected: a closed or retired channel is an expected reconnect; a busy budget is recorded below.
            if not (self.closed or target.endpoint in self.retired_endpoints):
                record_diagnostic(
                    "warning",
                    "api",
                    "api.runtime.endpoint_budget_occupied",
                    "Every runtime forwarding channel was busy, so a read was refused.",
                    stage="forward",
                    retryable=True,
                )
            await _runtime_unavailable(scope, receive, send)
            return
        try:
            try:
                upstream_request = client.build_request(
                    scope["method"],
                    httpx.URL(raw_path=raw_path),
                    headers=outgoing,
                    content=request.stream(),
                )
                response = await client.send(upstream_request, stream=True)
            except (httpx.HTTPError, OSError):
                # diagnostic-expected: local runtime transport loss becomes the bounded reconnect response.
                await _runtime_unavailable(scope, receive, send)
                return
            try:
                stream = StreamingResponse(
                    response.aiter_raw(), status_code=response.status_code
                )
                excluded = _hop_headers(response.headers.raw)
                stream.raw_headers = [
                    (name.lower(), value)
                    for name, value in response.headers.raw
                    if name.lower() not in excluded
                    and not name.lower().startswith(_PREFIX)
                ]
                await stream(scope, receive, send)
            finally:
                await finish_cleanup(response.aclose())
        finally:
            self._release(target.endpoint)

    async def _websocket(self, target: HttpUpstream, scope, receive, send):
        first = await receive()
        if first["type"] != "websocket.connect":
            return
        task = asyncio.current_task()
        assert task is not None
        try:
            # A viewer WebSocket owns its own connection, bounded by
            # max_websockets rather than the pooled-client endpoint budget.
            self._check_open(target.endpoint)
            if len(self.websocket_tasks) >= self.max_websockets:
                raise httpx.PoolTimeout("the viewer WebSocket budget is occupied")
        except httpx.PoolTimeout:
            # diagnostic-expected: viewer capacity exhaustion closes this connection with the retryable capacity code.
            await send(
                {
                    "type": "websocket.close",
                    "code": 1013,
                    "reason": "Viewer capacity unavailable; reconnect shortly",
                }
            )
            return
        self.websocket_tasks[task] = target.endpoint
        try:
            await self._forward_websocket(target, scope, receive, send, task)
        finally:
            self.websocket_tasks.pop(task, None)
            self.websockets.pop(task, None)

    async def _forward_websocket(self, target, scope, receive, send, task):
        headers = scope.get("headers", [])
        excluded = _hop_headers(headers) | _FORWARDED | _WEBSOCKET
        outgoing = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers
            if name.lower() not in excluded
            and name.lower() != b"host"
            and not name.lower().startswith(_PREFIX)
        ]
        outgoing.extend(
            (name.decode(), value.decode())
            for name, value in internal_headers(
                target.credential,
                scope.get("scheme", "ws"),
                lane=target.lane,
                client=scope.get("client"),
            )
        )
        host = next(
            (
                value.decode("latin-1")
                for name, value in headers
                if name.lower() == b"host"
            ),
            "nebula-runtime",
        )
        path = scope.get("raw_path", scope["path"].encode()).decode("ascii")
        if scope.get("query_string"):
            path += "?" + scope["query_string"].decode("ascii")
        try:
            async with unix_connect(
                target.socket_path(),
                uri="ws://" + host + path,
                additional_headers=outgoing,
                subprotocols=scope.get("subprotocols", []),
                max_size=2 * 1024**2,
                max_queue=16,
                open_timeout=5,
                close_timeout=2,
            ) as upstream:
                if self.closed or target.endpoint in self.retired_endpoints:
                    await send({"type": "websocket.close", "code": 1012})
                    return
                self.websockets[task] = upstream
                await send(
                    {"type": "websocket.accept", "subprotocol": upstream.subprotocol}
                )

                async def incoming():
                    while True:
                        message = await receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message["type"] == "websocket.receive":
                            await upstream.send(
                                message.get("text")
                                if message.get("text") is not None
                                else message["bytes"]
                            )

                async def outgoing_frames():
                    async for data in upstream:
                        await send(
                            {
                                "type": "websocket.send",
                                **(
                                    {"text": data}
                                    if isinstance(data, str)
                                    else {"bytes": data}
                                ),
                            }
                        )

                tasks = [
                    # diagnostic-expected: this viewer pump is cancelled, joined, and its failure classified below.
                    asyncio.create_task(incoming()),
                    # diagnostic-expected: this viewer pump is cancelled, joined, and its failure classified below.
                    asyncio.create_task(outgoing_frames()),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pump in tasks:
                        pump.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for error in results:
                        if isinstance(error, BaseException) and not isinstance(
                            error,
                            (
                                asyncio.CancelledError,
                                OSError,
                                ConnectionClosed,
                                TimeoutError,
                            ),
                        ):
                            record_caught_exception(
                                "api",
                                "api.runtime.websocket_pump_failed",
                                "A runtime viewer connection could not continue.",
                                error,
                                stage="stream",
                            )
                await send(
                    {"type": "websocket.close", "code": upstream.close_code or 1001}
                )
        except InvalidStatus as error:
            # diagnostic-expected: upstream authentication or readiness rejection becomes a bounded close code.
            status = error.response.status_code
            await send(
                {
                    "type": "websocket.close",
                    "code": 4401 if status == 401 else 4403 if status == 403 else 1012,
                }
            )
        except (OSError, ConnectionClosed, TimeoutError):
            # diagnostic-expected: known local transport loss closes the viewer for cursor-based reconnect.
            await send(
                {
                    "type": "websocket.close",
                    "code": 1012,
                    "reason": "Runtime connection changed; reconnect using the saved cursor",
                }
            )
