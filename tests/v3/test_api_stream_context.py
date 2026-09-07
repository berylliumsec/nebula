import asyncio

from nebula.v3.api import _correlated_stream
from nebula.v3 import diagnostics


def test_stream_correlation_does_not_leak_and_closes_from_another_task():
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

    asyncio.run(scenario())
