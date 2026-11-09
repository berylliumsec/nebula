"""Mutation identity across independently installed terminal transports."""

import asyncio
from uuid import UUID

import httpx
import pytest

from nebula.terminal.transport import (
    AsyncApi,
    ApiError,
    Settings,
    connection_options,
    request,
)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_mutation_keys_cover_requests_streams_and_do_not_retry(tmp_path, asynchronous):
    settings = Settings(
        tmp_path / "chat.json",
        {
            "cores": {
                "https://core.example": {
                    "device_id": "fixture",
                    "device_token": "fixture-device",
                    "csrf_token": "fixture-csrf",
                }
            }
        },
        "https://core.example",
    )
    seen = []

    def handler(req):
        seen.append(req)
        assert req.headers["x-nebula-csrf"] == "fixture-csrf"
        assert "fixture-device" in req.headers["cookie"]
        if req.url.path.endswith("fail"):
            return httpx.Response(503, json={"detail": "unavailable"})
        if req.url.path.endswith("stream"):
            return httpx.Response(200, text='event: done\ndata: {"ok":true}\n\n')
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    methods = ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "POST"]

    async def run():
        api = AsyncApi(settings, transport=transport)
        try:
            for method in methods:
                await api.request(method, "fixture")
            await api.request(
                "POST", "fixture", headers={"idempotency-key": "explicit-fixture-key"}
            )
            assert [item async for item in api.events("POST", "stream", body={})] == [
                ("done", {"ok": True})
            ]
            assert [item async for item in api.events("GET", "stream")] == [
                ("done", {"ok": True})
            ]
            with pytest.raises(ApiError):
                await api.request("POST", "fail")
        finally:
            await api.close()

    if asynchronous:
        asyncio.run(run())
    else:
        with httpx.Client(
            **connection_options(settings), transport=transport
        ) as client:
            for method in methods:
                request(client, method, "fixture")
            request(
                client,
                "POST",
                "fixture",
                headers={"idempotency-key": "explicit-fixture-key"},
            )
            with client.stream("POST", "stream", json={}) as response:
                assert "done" in response.read().decode()
            with client.stream("GET", "stream") as response:
                assert "done" in response.read().decode()
            with pytest.raises(ApiError):
                request(client, "POST", "fail")
    assert len(seen) == 12
    keys = []
    for req in seen:
        key = req.headers.get("Idempotency-Key")
        if req.method in {"GET", "HEAD", "OPTIONS"}:
            assert key is None
        elif key != "explicit-fixture-key":
            assert str(UUID(key)) == key
            keys.append(key)
    assert len(keys) == len(set(keys)) == 7
    assert seen[8].headers["Idempotency-Key"] == "explicit-fixture-key"
    assert sum(req.url.path.endswith("fail") for req in seen) == 1
