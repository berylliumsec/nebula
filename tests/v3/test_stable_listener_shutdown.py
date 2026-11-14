"""The stable listener stops promptly even while viewer streams stay open."""

import asyncio
import socket
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route
import uvicorn

from nebula.v3.runtime_service import (
    STABLE_LISTENER_GRACE_SECONDS,
    relay_stop_bound,
    serve_listener_config,
    stable_listener_config,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_open_viewer_stream_cannot_hold_listener_shutdown_open():
    started = asyncio.Event()

    async def events(_request):
        async def stream():
            started.set()
            while True:
                # Like /api/v1/work/events: relayed keep-alives that never end.
                yield b": keep-alive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        stable_listener_config(
            Starlette(routes=[Route("/events", events)]),
            host="127.0.0.1",
            port=port,
        )
    )
    serving = asyncio.create_task(server.serve())
    async with httpx.AsyncClient(timeout=30) as client:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.05)
        async with client.stream("GET", f"http://127.0.0.1:{port}/events"):
            await started.wait()
            stopping = time.monotonic()
            server.should_exit = True
            await asyncio.wait_for(
                asyncio.shield(serving), STABLE_LISTENER_GRACE_SECONDS + 10
            )
            assert time.monotonic() - stopping < STABLE_LISTENER_GRACE_SECONDS + 5


async def _unused_app(scope, receive, send):  # pragma: no cover - never served
    raise AssertionError


@pytest.mark.parametrize(
    ("inherited", "bound"),
    [(False, STABLE_LISTENER_GRACE_SECONDS), (True, relay_stop_bound())],
)
def test_serve_listener_has_a_bound_for_both_kinds_of_start(inherited, bound):
    # #1021 gave every listener a bound and #1023 a longer one for an
    # inherited listener; composed, the keyword reached uvicorn twice and
    # every `nebula-core serve` exited right after its ready line.
    config = serve_listener_config(
        _unused_app,
        host="127.0.0.1",
        port=0,
        tls_cert=None,
        tls_key=None,
        inherited=inherited,
    )
    assert config.timeout_graceful_shutdown == bound
    assert bound > 0


@pytest.mark.parametrize("grace", [None, 0, -1, 2.5, True])
def test_a_stable_listener_never_waits_forever(grace):
    with pytest.raises(ValueError):
        stable_listener_config(_unused_app, grace_seconds=grace)
    with pytest.raises(TypeError):
        stable_listener_config(_unused_app, timeout_graceful_shutdown=grace)
