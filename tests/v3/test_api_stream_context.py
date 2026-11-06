import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio

from nebula.v3.api import _correlated_stream
from nebula.v3 import diagnostics


async def test_stream_correlation_does_not_leak_and_closes_from_another_task():
    async def scenario():
        observed = []

        async def source():
            try:
                observed.append(diagnostics._request_id.get())
                yield b"event"
            finally:
                observed.append("closed")

        stream = _correlated_stream(
            source(), request_id="stream-request", operation_id="stream-operation"
        )
        assert await anext(stream) == b"event"
        await asyncio.create_task(stream.aclose())
        assert diagnostics._request_id.get() is None
        assert observed == ["stream-request", "closed"]

    (await scenario())


async def test_quiet_chat_heartbeat_arrives_within_the_streaming_status_budget():
    async def scenario():
        release = asyncio.Event()
        closed = asyncio.Event()

        async def source():
            try:
                await release.wait()
                assert diagnostics._request_id.get() == "quiet-chat"
                yield b"event"
            finally:
                closed.set()

        stream = _correlated_stream(
            source(),
            request_id="quiet-chat",
            operation_id="quiet-operation",
            heartbeat=True,
        )
        try:
            assert (
                await asyncio.wait_for(anext(stream), timeout=3) == b": keepalive\n\n"
            )
            assert diagnostics._request_id.get() is None
            assert not closed.is_set()
            release.set()
            assert await asyncio.wait_for(anext(stream), timeout=3) == b"event"
        finally:
            await asyncio.create_task(stream.aclose())
        assert closed.is_set()
        assert diagnostics._request_id.get() is None

    (await scenario())


pytestmark = pytest.mark.anyio
