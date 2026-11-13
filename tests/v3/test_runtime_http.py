"""Local channel security and viewer lifetime without operational services."""

import asyncio
from builtins import BaseExceptionGroup
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import secrets
import tempfile

from fastapi import FastAPI, Request, WebSocket
import httpx
import pytest
from starlette.responses import JSONResponse
import uvicorn
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from nebula.v3.runtime_http import (
    HttpUpstream,
    PrivateRuntimeHttp,
    RuntimeHttpProxy,
    internal_headers,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SocketServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


@asynccontextmanager
async def server(app):
    with tempfile.TemporaryDirectory(prefix="nebula-http-") as directory:
        path = Path(directory) / "runtime.sock"
        instance = SocketServer(
            uvicorn.Config(
                app,
                uds=str(path),
                log_level="error",
                access_log=False,
                lifespan="off",
                timeout_graceful_shutdown=1,
            )
        )
        task = asyncio.create_task(instance.serve())
        try:
            async with asyncio.timeout(5):
                while not instance.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield "unix://" + str(path)
        finally:
            instance.should_exit = True
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.parametrize("change", ["credential", "expired", "protocol"])
async def test_private_socket_rejects_missing_or_incompatible_authentication(change):
    key = secrets.token_bytes(32)
    calls = []
    app = FastAPI()

    @app.get("/read")
    async def read():
        calls.append(True)
        return {"read": True}

    gate = PrivateRuntimeHttp(app, key, clock=lambda: 1000)
    headers = dict(
        internal_headers(
            b"b" * 32 if change == "credential" else key,
            "http",
            clock=lambda: 900 if change == "expired" else 1000,
        )
    )
    if change == "protocol":
        headers[b"x-nebula-internal-protocol"] = b"nebula.runtime/v2"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url="http://local"
    ) as client:
        assert (await client.get("/read", headers=headers)).status_code == 401
        assert (await client.get("/read")).status_code == 401
    assert calls == []


async def test_private_nonce_replay_does_not_dispatch_twice():
    key = secrets.token_bytes(32)
    app = FastAPI()
    calls = []

    @app.post("/change")
    async def change():
        calls.append(True)
        return {"saved": True}

    gate = PrivateRuntimeHttp(app, key)
    headers = internal_headers(key, "http")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url="http://local"
    ) as client:
        assert (await client.post("/change", headers=headers)).status_code == 200
        assert (await client.post("/change", headers=headers)).status_code == 409
    assert calls == [True]


async def test_viewer_nonce_saturation_leaves_control_capacity_available():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.api_route("/call", methods=["GET", "POST"])
    async def call():
        return {"accepted": True}

    gate = PrivateRuntimeHttp(app, key, nonce_capacity=1, control_nonce_capacity=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url="http://local"
    ) as client:
        assert (
            await client.get("/call", headers=internal_headers(key, "http"))
        ).status_code == 200
        assert (
            await client.get("/call", headers=internal_headers(key, "http"))
        ).status_code == 429
        assert (
            await client.post(
                "/call", headers=internal_headers(key, "http", lane="control")
            )
        ).status_code == 200


@pytest.mark.parametrize(
    "field,value",
    [
        (b"x-nebula-internal-client", b'["127.0.0.1",1]'),
        (b"x-nebula-internal-lane", b"control"),
    ],
)
async def test_client_address_and_lane_cannot_be_changed_without_authentication(
    field, value
):
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/read")
    async def read():
        raise AssertionError("modified private metadata was accepted")

    headers = dict(internal_headers(key, "http", client=("192.0.2.1", 1200)))
    headers[field] = value
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=PrivateRuntimeHttp(app, key)),
        base_url="http://local",
    ) as client:
        assert (await client.get("/read", headers=headers)).status_code == 401


async def test_proxy_preserves_origin_credentials_body_and_duplicate_cookies():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.post("/path/{item}")
    async def echo(item: str, request: Request):
        assert not any(
            name.startswith("x-nebula-internal-") for name in request.headers
        )
        response = JSONResponse(
            {
                "item": item,
                "query": request.url.query,
                "scheme": request.url.scheme,
                "host": request.headers["host"],
                "origin": request.headers["origin"],
                "authorization": request.headers["authorization"],
                "csrf": request.headers["x-nebula-csrf"],
                "body": await request.json(),
            }
        )
        response.set_cookie("device", "synthetic", secure=True)
        response.set_cookie("csrf", "synthetic", secure=True)
        return response

    async def select(_scope):
        return HttpUpstream("unix:///fixture.sock", key)

    proxy = RuntimeHttpProxy(
        select,
        transport_factory=lambda _endpoint: httpx.ASGITransport(
            app=PrivateRuntimeHttp(app, key)
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="https://public.example"
        ) as client:
            response = await client.post(
                "/path/value?q=one%20two",
                json={"message": "synthetic"},
                headers={
                    "Authorization": "Bearer synthetic",
                    "Origin": "https://public.example",
                    "X-Nebula-CSRF": "synthetic",
                    "X-Nebula-Internal-MAC": "forged",
                },
            )
        assert response.status_code == 200
        assert response.json() == {
            "item": "value",
            "query": "q=one%20two",
            "scheme": "https",
            "host": "public.example",
            "origin": "https://public.example",
            "authorization": "Bearer synthetic",
            "csrf": "synthetic",
            "body": {"message": "synthetic"},
        }
        assert len(response.headers.get_list("set-cookie")) == 2
    finally:
        await proxy.close()


async def test_shared_upstream_client_never_supplies_another_viewers_cookie():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.post("/pair")
    async def pair():
        response = JSONResponse({"paired": True})
        response.set_cookie("nebula_device", "first-viewer", secure=False)
        return response

    @app.get("/identity")
    async def identity(request: Request):
        return {"device": request.cookies.get("nebula_device")}

    async def select(_scope):
        return HttpUpstream("unix:///fixture.sock", key)

    proxy = RuntimeHttpProxy(
        select,
        transport_factory=lambda _endpoint: httpx.ASGITransport(
            app=PrivateRuntimeHttp(app, key)
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="http://public"
        ) as first:
            assert (await first.post("/pair")).status_code == 200
            assert (await first.get("/identity")).json() == {"device": "first-viewer"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="http://public"
        ) as anonymous:
            assert (await anonymous.get("/identity")).json() == {"device": None}
            assert (
                await anonymous.get(
                    "/identity", headers={"Cookie": "nebula_device=explicit-viewer"}
                )
            ).json() == {"device": "explicit-viewer"}
    finally:
        await proxy.close()


async def test_verified_client_address_survives_forwarding_without_public_spoof_headers():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.post("/local-only")
    async def local(request: Request):
        assert (
            "x-forwarded-for" not in request.headers
            and "forwarded" not in request.headers
        )
        return JSONResponse(
            {"client": request.client.host},
            status_code=200 if request.client.host == "127.0.0.1" else 403,
        )

    async def select(_scope):
        return HttpUpstream("unix:///fixture.sock", key)

    proxy = RuntimeHttpProxy(
        select,
        transport_factory=lambda _endpoint: httpx.ASGITransport(
            app=PrivateRuntimeHttp(app, key)
        ),
    )
    try:
        for address, expected in [("127.0.0.1", 200), ("192.0.2.3", 403)]:
            transport = httpx.ASGITransport(app=proxy, client=(address, 1200))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://public"
            ) as client:
                response = await client.post(
                    "/local-only",
                    headers={
                        "Forwarded": "for=127.0.0.1",
                        "X-Forwarded-For": "127.0.0.1",
                    },
                )
                assert response.status_code == expected and response.json() == {
                    "client": address
                }
    finally:
        await proxy.close()


async def test_closing_proxy_prevents_client_recreation_during_teardown(monkeypatch):
    key = secrets.token_bytes(32)
    app = FastAPI()
    calls = []

    @app.get("/read")
    async def read():
        calls.append(True)
        return {"read": True}

    async def select(_scope):
        return HttpUpstream("unix:///fixture.sock", key)

    proxy = RuntimeHttpProxy(
        select,
        transport_factory=lambda _endpoint: httpx.ASGITransport(
            app=PrivateRuntimeHttp(app, key)
        ),
    )
    closing, release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        assert (await viewer.get("/read")).status_code == 200
        upstream = next(iter(proxy.clients.values()))
        original = upstream.aclose

        async def delayed_close():
            closing.set()
            await release.wait()
            await original()

        monkeypatch.setattr(upstream, "aclose", delayed_close)
        task = asyncio.create_task(proxy.close())
        try:
            await asyncio.wait_for(closing.wait(), timeout=2)
            await proxy.discard_endpoint("unix:///fixture.sock")
            assert (await viewer.get("/read")).status_code == 503
            assert calls == [True]
        finally:
            release.set()
            await task
        assert proxy.clients == {}


@pytest.mark.parametrize("action", ["close", "discard"])
async def test_cancelled_teardown_joins_detached_client_cleanup(monkeypatch, action):
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/read")
    async def read():
        return {"read": True}

    async def select(_scope):
        return HttpUpstream("unix:///fixture.sock", key)

    proxy = RuntimeHttpProxy(
        select,
        transport_factory=lambda _endpoint: httpx.ASGITransport(
            app=PrivateRuntimeHttp(app, key)
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        assert (await viewer.get("/read")).status_code == 200
        upstream = next(iter(proxy.clients.values()))
        original = upstream.aclose

        async def delayed_close():
            entered.set()
            await release.wait()
            await original()

        monkeypatch.setattr(upstream, "aclose", delayed_close)
        operation = (
            proxy.close()
            if action == "close"
            else proxy.discard_endpoint("unix:///fixture.sock")
        )
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upstream.is_closed
        assert (await viewer.get("/read")).status_code == 503
    await proxy.close()


async def test_teardown_reports_failure_after_attempting_every_client(monkeypatch):
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/{path}")
    async def read(path):
        return {"path": path}

    async def select(scope):
        return HttpUpstream(
            "unix:///fixture.sock",
            key,
            "control" if scope["path"] == "/control" else "read",
        )

    proxy = RuntimeHttpProxy(
        select,
        transport_factory=lambda _endpoint: httpx.ASGITransport(
            app=PrivateRuntimeHttp(app, key)
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        assert (await viewer.get("/read")).status_code == 200
        assert (await viewer.get("/control")).status_code == 200
    first, second = list(proxy.clients.values())
    original = first.aclose

    async def fail_close():
        raise OSError("synthetic close failure")

    monkeypatch.setattr(first, "aclose", fail_close)
    try:
        with pytest.raises(BaseExceptionGroup, match="cleanup did not complete"):
            await proxy.close()
        assert second.is_closed
        assert proxy.closed and not proxy.clients
    finally:
        await original()


async def test_control_requests_remain_available_when_read_connections_are_full():
    key = secrets.token_bytes(32)
    release, full = asyncio.Event(), asyncio.Event()
    count = 0
    app = FastAPI()

    @app.get("/read")
    async def read():
        nonlocal count
        count += 1
        if count == 64:
            full.set()
        await release.wait()
        return {"done": True}

    @app.post("/control")
    async def control():
        return {"control_received": True}

    async with server(PrivateRuntimeHttp(app, key)) as endpoint:

        async def select(scope):
            return HttpUpstream(
                endpoint, key, "control" if scope["path"] == "/control" else "read"
            )

        proxy = RuntimeHttpProxy(select)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="http://public"
        ) as client:
            requests = [asyncio.create_task(client.get("/read")) for _ in range(64)]
            try:
                await asyncio.wait_for(full.wait(), timeout=5)
                response = await asyncio.wait_for(client.post("/control"), timeout=2)
                assert response.json() == {"control_received": True}
            finally:
                release.set()
                results = await asyncio.gather(*requests)
                await proxy.close()
            assert all(result.status_code == 200 for result in results)


async def test_websocket_duplex_frames_retain_public_origin_and_credentials():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.websocket("/events")
    async def events(websocket: WebSocket):
        assert websocket.headers["origin"] == "https://public.example"
        assert websocket.headers["authorization"] == "Bearer synthetic"
        assert websocket.url.scheme == "wss"
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.send_bytes(await websocket.receive_bytes())
        await websocket.close()

    async with server(PrivateRuntimeHttp(app, key)) as backend:

        async def select(_scope):
            return HttpUpstream(backend, key)

        proxy = RuntimeHttpProxy(select)
        # The outer private gate simulates a TLS-terminating stable listener.
        outer = secrets.token_bytes(32)
        async with server(PrivateRuntimeHttp(proxy, outer)) as frontend:
            headers = [
                (name.decode(), value.decode())
                for name, value in internal_headers(outer, "wss")
            ]
            headers.extend(
                [
                    ("Origin", "https://public.example"),
                    ("Authorization", "Bearer synthetic"),
                ]
            )
            async with unix_connect(
                frontend[7:],
                uri="ws://public.example/events",
                additional_headers=headers,
            ) as viewer:
                await viewer.send("synthetic π")
                assert await viewer.recv() == "synthetic π"
                await viewer.send(b"synthetic-bytes")
                assert await viewer.recv() == b"synthetic-bytes"
        await proxy.close()


@pytest.mark.parametrize("action", ["close", "discard"])
async def test_websocket_capacity_and_teardown_cover_live_viewers(action):
    key = secrets.token_bytes(32)
    app = FastAPI()
    active = 0

    @app.websocket("/hold")
    async def hold(websocket: WebSocket):
        nonlocal active
        await websocket.accept()
        active += 1
        try:
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass
        finally:
            active -= 1

    async with server(PrivateRuntimeHttp(app, key)) as endpoint:

        async def select(_scope):
            return HttpUpstream(endpoint, key)

        proxy = RuntimeHttpProxy(select, max_websockets=1)
        async with server(proxy) as frontend:
            async with unix_connect(frontend[7:], uri="ws://public/hold") as first:
                assert active == 1
                with pytest.raises(InvalidStatus):
                    async with unix_connect(frontend[7:], uri="ws://public/hold"):
                        raise AssertionError("viewer bypassed the WebSocket budget")
                if action == "close":
                    await asyncio.wait_for(proxy.close(), timeout=5)
                else:
                    await asyncio.wait_for(proxy.discard_endpoint(endpoint), timeout=5)
                with pytest.raises(ConnectionClosed):
                    await first.recv()
                assert active == 0
                assert not proxy.websocket_tasks and not proxy.websockets
                with pytest.raises(InvalidStatus):
                    async with unix_connect(frontend[7:], uri="ws://public/hold"):
                        raise AssertionError("closed endpoint was recreated")
        await proxy.close()


async def test_closing_a_viewer_does_not_cancel_an_upstream_owned_task():
    key = secrets.token_bytes(32)
    done = asyncio.Event()
    app = FastAPI()
    producer = None

    @app.post("/start")
    async def start():
        nonlocal producer

        async def produce():
            await done.wait()
            return "finished independently"

        producer = asyncio.create_task(produce())
        return {"accepted": True}

    async with server(PrivateRuntimeHttp(app, key)) as endpoint:

        async def select(_scope):
            return HttpUpstream(endpoint, key)

        proxy = RuntimeHttpProxy(select)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="http://public"
        ) as viewer:
            assert (await viewer.post("/start")).json() == {"accepted": True}
        await proxy.close()
        assert not producer.done()
        done.set()
        assert await producer == "finished independently"


def _endpoint_fleet(app, key, transports=None):
    """A proxy routing /p/<n>/... to synthetic runtime endpoint n."""

    async def select(scope):
        return HttpUpstream(f"unix:///fixture-{scope['path'].split('/')[2]}.sock", key)

    def transport(endpoint):
        if transports is not None and endpoint in transports:
            return transports[endpoint]
        return httpx.ASGITransport(app=PrivateRuntimeHttp(app, key))

    return RuntimeHttpProxy(select, transport_factory=transport)


async def test_reads_reach_more_runtime_endpoints_than_the_channel_budget():
    """Every project worker stays readable; idle channels are evicted, not kept."""
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/p/{project}/read")
    async def read(project: str):
        return {"project": project}

    proxy = _endpoint_fleet(app, key)
    seen: list[httpx.AsyncClient] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        for project in [*range(12), *reversed(range(12))]:
            response = await viewer.get(f"/p/{project}/read")
            assert response.status_code == 200, project
            assert response.json() == {"project": str(project)}
            seen.extend(
                client for client in proxy.clients.values() if client not in seen
            )
            assert len({key for key, _control in proxy.clients}) <= 8
    held = list(proxy.clients.values())
    assert len(seen) > len(held)
    assert all(client.is_closed for client in seen if client not in held)
    assert proxy.active == {}
    await proxy.close()


async def test_busy_runtime_endpoints_are_kept_and_a_full_budget_is_reported(
    monkeypatch,
):
    key = secrets.token_bytes(32)
    app = FastAPI()
    entered, release = asyncio.Semaphore(0), asyncio.Event()

    @app.get("/p/{project}/hold")
    async def hold(project: str):
        entered.release()
        await release.wait()
        return {"held": project}

    @app.get("/p/{project}/read")
    async def read(project: str):
        return {"project": project}

    recorded = []
    monkeypatch.setattr(
        "nebula.v3.runtime_http.record_diagnostic",
        lambda *args, **fields: recorded.append((args, fields)),
    )
    proxy = _endpoint_fleet(app, key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        held = [
            asyncio.create_task(viewer.get(f"/p/{project}/hold"))
            for project in range(8)
        ]
        for _ in held:
            await asyncio.wait_for(entered.acquire(), timeout=5)
        refused = await viewer.get("/p/8/read")
        assert refused.status_code == 503
        assert [args[2] for args, _fields in recorded] == [
            "api.runtime.endpoint_budget_occupied"
        ]
        release.set()
        assert [(await task).json() for task in held] == [
            {"held": str(project)} for project in range(8)
        ]
        assert (await viewer.get("/p/8/read")).json() == {"project": "8"}
    assert proxy.active == {}
    await proxy.close()


async def test_failed_forward_releases_its_channel_reservation():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/p/{project}/read")
    async def read(project: str):
        return {"project": project}

    class Unreachable(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("synthetic runtime transport loss")

    proxy = _endpoint_fleet(app, key, {"unix:///fixture-0.sock": Unreachable()})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        assert (await viewer.get("/p/0/read")).status_code == 503
        assert proxy.active == {}
        for project in range(1, 10):
            assert (await viewer.get(f"/p/{project}/read")).status_code == 200
    assert "unix:///fixture-0.sock" not in {key for key, _control in proxy.clients}
    await proxy.close()


async def test_busy_least_recent_endpoint_is_skipped_for_an_idle_one():
    key = secrets.token_bytes(32)
    app = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()

    @app.get("/p/{project}/hold")
    async def hold(project: str):
        entered.set()
        await release.wait()
        return {"held": project}

    @app.get("/p/{project}/read")
    async def read(project: str):
        return {"project": project}

    proxy = _endpoint_fleet(app, key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        held = asyncio.create_task(viewer.get("/p/0/hold"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        for project in range(1, 9):
            assert (await viewer.get(f"/p/{project}/read")).status_code == 200
        endpoints = {key for key, _control in proxy.clients}
        assert "unix:///fixture-0.sock" in endpoints
        assert "unix:///fixture-1.sock" not in endpoints
        release.set()
        assert (await held).json() == {"held": "0"}
    assert proxy.active == {}
    await proxy.close()


async def test_viewer_websockets_do_not_consume_the_pooled_client_budget():
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/p/{project}/read")
    async def read(project: str):
        return {"project": project}

    proxy = _endpoint_fleet(app, key)
    viewers = [asyncio.create_task(asyncio.Event().wait()) for _ in range(8)]
    try:
        for index, task in enumerate(viewers):
            proxy.websocket_tasks[task] = f"unix:///viewer-{index}.sock"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="http://public"
        ) as viewer:
            for project in range(12):
                response = await viewer.get(f"/p/{project}/read")
                assert response.status_code == 200, project
    finally:
        for task in viewers:
            task.cancel()
        await asyncio.gather(*viewers, return_exceptions=True)
        proxy.websocket_tasks.clear()
        await proxy.close()


async def test_retirement_during_eviction_returns_a_reconnect_response(monkeypatch):
    key = secrets.token_bytes(32)
    app = FastAPI()

    @app.get("/p/{project}/read")
    async def read(project: str):
        return {"project": project}

    proxy = _endpoint_fleet(app, key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://public"
    ) as viewer:
        for project in range(8):
            assert (await viewer.get(f"/p/{project}/read")).status_code == 200
        oldest = next(
            client
            for (endpoint, _control), client in proxy.clients.items()
            if endpoint == "unix:///fixture-0.sock"
        )
        original, closing, release = oldest.aclose, asyncio.Event(), asyncio.Event()

        async def slow_close():
            closing.set()
            await release.wait()
            await original()

        monkeypatch.setattr(oldest, "aclose", slow_close)
        pending = asyncio.create_task(viewer.get("/p/8/read"))
        await asyncio.wait_for(closing.wait(), timeout=5)
        await proxy.discard_endpoint("unix:///fixture-8.sock")
        release.set()
        response = await asyncio.wait_for(pending, timeout=5)
        assert response.status_code == 503
        assert response.json()["detail"].startswith("The runtime connection")
    assert proxy.active == {}
    await proxy.close()
