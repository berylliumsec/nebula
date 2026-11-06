"""Keep converted integration fixtures and their resources on one event loop."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from nebula.v3.chat import ChatService
from nebula.v3.database import Database
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore
from nebula.v3.async_utils import drain_callback_tasks, finish_cleanup


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def close_database_handles(monkeypatch, anyio_backend):
    databases = []
    services = []
    clients = []
    stores = []
    original_client = AsyncTestClient.__init__

    def client(self, *args, **kwargs):
        original_client(self, *args, **kwargs)
        clients.append(self)

    original_database = Database.__init__
    original_chat = ChatService.__init__
    original_harness = HarnessRuntimeService.__init__
    original_store = NebulaStore.__init__

    def database(self, *args, **kwargs):
        original_database(self, *args, **kwargs)
        databases.append(self)

    def chat(self, *args, **kwargs):
        original_chat(self, *args, **kwargs)
        services.append(self)

    def harness(self, *args, **kwargs):
        original_harness(self, *args, **kwargs)
        services.append(self)

    def store(self, *args, **kwargs):
        original_store(self, *args, **kwargs)
        stores.append(self)

    monkeypatch.setattr(AsyncTestClient, "__init__", client)
    monkeypatch.setattr(Database, "__init__", database)
    monkeypatch.setattr(ChatService, "__init__", chat)
    monkeypatch.setattr(HarnessRuntimeService, "__init__", harness)
    monkeypatch.setattr(NebulaStore, "__init__", store)
    yield
    stack = AsyncExitStack()
    for database in databases:
        stack.push_async_callback(database.dispose)
    for store in stores:
        stack.push_async_callback(store.event_history.close)
    stack.push_async_callback(drain_callback_tasks)
    for service in services:
        stack.push_async_callback(service.shutdown)
    for client in clients:
        stack.push_async_callback(client.aclose)
    await finish_cleanup(stack.aclose())


class AsyncTestClient(httpx.AsyncClient):
    """Run HTTP, WebSockets and Core lifespan on the fixture's owning loop."""

    def __init__(self, app, base_url="http://testserver", **kwargs):
        raise_errors = kwargs.pop("raise_server_exceptions", True)
        self.client_address = kwargs.pop("client", ("testclient", 50000))
        super().__init__(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=raise_errors,
                client=self.client_address,
            ),
            base_url=base_url,
            follow_redirects=kwargs.pop("follow_redirects", True),
            **kwargs,
        )
        self.app = app
        self._lifespan_stack = AsyncExitStack()
        self._start_lock = asyncio.Lock()
        self._started_lifespan = False

    async def _start(self):
        async with self._start_lock:
            if not self._started_lifespan:
                await self._lifespan_stack.enter_async_context(
                    self.app.router.lifespan_context(self.app)
                )
                self._started_lifespan = True

    async def __aenter__(self):
        await self._start()
        return self

    async def __aexit__(self, *_exc):
        await self.aclose()

    async def aclose(self):
        try:
            await super().aclose()
        finally:
            await self._lifespan_stack.aclose()

    async def request(self, *args, **kwargs):
        # Match Starlette: only an explicit client context starts lifespan.
        return await super().request(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return await super().get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await super().post(*args, **kwargs)

    async def put(self, *args, **kwargs):
        return await super().put(*args, **kwargs)

    async def patch(self, *args, **kwargs):
        return await super().patch(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await super().delete(*args, **kwargs)

    async def head(self, *args, **kwargs):
        return await super().head(*args, **kwargs)

    async def options(self, *args, **kwargs):
        return await super().options(*args, **kwargs)

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        async with super().stream(*args, **kwargs) as response:
            yield response

    @asynccontextmanager
    async def websocket_connect(
        self, url, subprotocols=None, **kwargs
    ) -> AsyncIterator["_AsyncWebSocket"]:
        websocket = _AsyncWebSocket(self, url, subprotocols or [], **kwargs)
        try:
            await websocket.connect()
            yield websocket
        finally:
            await websocket.close()


class _AsyncWebSocket:
    def __init__(self, client, url, subprotocols, *, headers=None, params=None):

        request = client.build_request("GET", url, headers=headers, params=params)
        if subprotocols:
            request.headers.setdefault(
                "Sec-WebSocket-Protocol", ", ".join(subprotocols)
            )
        self.app = client.app
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": "ws",
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?", 1)[0],
            "root_path": "",
            "query_string": request.url.query,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "client": client.client_address,
            "server": (request.url.host, request.url.port or 80),
            "subprotocols": subprotocols,
            "state": {},
        }
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.task = None
        self.accepted_subprotocol = None
        self.extra_headers = []

    async def _message(self):

        receive = asyncio.create_task(self.outgoing.get())
        try:
            done, _ = await asyncio.wait(
                {receive, self.task}, return_when=asyncio.FIRST_COMPLETED
            )
            if receive not in done and self.outgoing.empty():
                self.task.result()
                raise RuntimeError("WebSocket application exited without a message")
            message = await receive
        finally:
            if not receive.done():
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(
                message.get("code", 1000), message.get("reason", "")
            )
        return message

    async def connect(self):

        await self.incoming.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(
            self.app(self.scope, self.incoming.get, self.outgoing.put)
        )
        message = await self._message()
        assert message["type"] == "websocket.accept", message
        self.accepted_subprotocol = message.get("subprotocol")
        self.extra_headers = message.get("headers", [])

    async def send_json(self, value, mode="text"):

        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if mode == "binary":
            await self.send_bytes(payload.encode())
        else:
            await self.send_text(payload)

    async def send_text(self, value):
        await self.incoming.put({"type": "websocket.receive", "text": value})

    async def send_bytes(self, value):
        await self.incoming.put({"type": "websocket.receive", "bytes": value})

    async def receive_json(self, mode="text"):

        value = (
            await self.receive_bytes()
            if mode == "binary"
            else await self.receive_text()
        )
        return json.loads(value)

    async def receive_text(self):
        return (await self._message())["text"]

    async def receive_bytes(self):
        return (await self._message())["bytes"]

    async def receive(self):
        return await self._message()

    async def close(self, code=1000, reason=None):

        if self.task is None:
            return
        await self.incoming.put({"type": "websocket.disconnect", "code": code})
        done, _ = await asyncio.wait({self.task}, timeout=1)
        if not done:
            self.task.cancel()
        results = await asyncio.gather(self.task, return_exceptions=True)
        if done and isinstance(results[0], Exception):
            raise results[0]
