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
