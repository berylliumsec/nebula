"""Mutation identity across independently installed terminal transports."""

import asyncio
from uuid import UUID

import httpx
import pytest

from nebula.terminal.transport import (
    AcceptedRequest,
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


def _settings(tmp_path):
    return Settings(
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


def _receipt(command="command-a"):
    return httpx.Response(
        202,
        json={
            "state": "accepted",
            "receipt": {"command_id": command, "project_id": "project-a"},
            "detail": "Saved; waiting for its execution owner",
        },
        headers={"X-Nebula-Command-Id": command, "Retry-After": "1"},
    )


@pytest.fixture
def no_wait(monkeypatch):
    waits = []

    async def wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr("nebula.terminal.transport.asyncio.sleep", wait)
    return waits


def test_accepted_mutation_reads_its_saved_response_without_resending(
    tmp_path, no_wait
):
    seen = []
    polls = iter(
        [
            httpx.Response(202, json={"state": "queued", "command_id": "command-a"}),
            httpx.Response(200, json={"id": "chat-a", "title": "Renamed"}),
        ]
    )

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "PATCH":
            return _receipt()
        assert request.url.path == "/api/v1/runtime/requests/command-a/response"
        assert "Idempotency-Key" not in request.headers
        return next(polls)

    async def run():
        api = AsyncApi(_settings(tmp_path), transport=httpx.MockTransport(handler))
        try:
            return await api.request("PATCH", "chat-sessions/chat-a", json={})
        finally:
            await api.close()

    assert asyncio.run(run()) == {"id": "chat-a", "title": "Renamed"}
    assert [method for method, _ in seen] == ["PATCH", "GET", "GET"]
    assert no_wait == [1.0, 1.0]


def test_accepted_mutation_stops_waiting_without_resending(tmp_path, no_wait):
    seen = []

    def handler(request):
        seen.append(request.method)
        if request.method == "PATCH":
            return _receipt()
        return httpx.Response(202, json={"state": "queued", "command_id": "command-a"})

    async def run():
        api = AsyncApi(_settings(tmp_path), transport=httpx.MockTransport(handler))
        api.receipt_wait = 0
        try:
            await api.request("PATCH", "chat-sessions/chat-a", json={})
        finally:
            await api.close()

    with pytest.raises(AcceptedRequest, match="Nothing was resent") as raised:
        asyncio.run(run())
    assert raised.value.command_id == "command-a"
    assert seen == ["PATCH"]


def test_other_accepted_bodies_and_reconciliation_conflicts_are_not_receipts(
    tmp_path, no_wait
):
    def handler(request):
        if request.url.path.endswith("job"):
            return httpx.Response(202, json={"id": "job-a", "state": "accepted"})
        if request.method == "PATCH":
            return _receipt()
        return httpx.Response(
            409,
            json={
                "detail": "The saved request needs reconciliation. It has not been retried.",
                "command_id": "command-a",
                "state": "uncertain",
            },
        )

    async def run():
        api = AsyncApi(_settings(tmp_path), transport=httpx.MockTransport(handler))
        try:
            assert await api.request("POST", "job") == {
                "id": "job-a",
                "state": "accepted",
            }
            with pytest.raises(ApiError, match="needs reconciliation"):
                await api.request("PATCH", "chat-sessions/chat-a", json={})
        finally:
            await api.close()

    asyncio.run(run())


def test_accepted_stream_reports_saved_then_reads_the_saved_turn(tmp_path, no_wait):
    seen = []
    polls = iter(
        [
            httpx.Response(202, json={"state": "queued", "command_id": "command-a"}),
            httpx.Response(
                200,
                text='event: started\ndata: {"type":"started","turn_id":"turn-a"}\n\n'
                'event: done\ndata: {"type":"done","turn_id":"turn-a"}\n\n',
            ),
        ]
    )

    def handler(request):
        seen.append((request.method, request.url.path, request.url.query))
        if request.method == "POST":
            return _receipt()
        return next(polls)

    async def run():
        api = AsyncApi(_settings(tmp_path), transport=httpx.MockTransport(handler))
        try:
            return [item async for item in api.events("POST", "chat/completions")]
        finally:
            await api.close()

    assert asyncio.run(run()) == [
        ("accepted", {"type": "accepted", "command_id": "command-a"}),
        ("started", {"type": "started", "turn_id": "turn-a"}),
        ("done", {"type": "done", "turn_id": "turn-a"}),
    ]
    assert [row[:2] for row in seen] == [
        ("POST", "/api/v1/chat/completions"),
        ("GET", "/api/v1/runtime/requests/command-a/response"),
        ("GET", "/api/v1/runtime/requests/command-a/response"),
    ]
    assert seen[1][2] == b"after=0"


@pytest.mark.parametrize(
    "final",
    [
        {"id": "run-a", "status": "queued"},
        {"state": "queued", "command_id": "command-a"},
    ],
)
def test_a_202_marked_complete_is_the_saved_answer_not_a_receipt(
    tmp_path, no_wait, final
):
    seen = []
    polls = iter(
        [
            httpx.Response(
                202,
                json={"state": "running", "command_id": "command-a"},
                headers={"X-Nebula-Request-State": "pending"},
            ),
            httpx.Response(
                202,
                json=final,
                headers={
                    "X-Nebula-Command-Id": "command-a",
                    "X-Nebula-Request-State": "complete",
                },
            ),
        ]
    )

    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            response = _receipt()
            response.headers["X-Nebula-Request-State"] = "pending"
            return response
        return next(polls)

    async def run():
        api = AsyncApi(_settings(tmp_path), transport=httpx.MockTransport(handler))
        try:
            return await api.request("POST", "missions", json={})
        finally:
            await api.close()

    assert asyncio.run(run()) == final
    assert seen == ["POST", "GET", "GET"]


@pytest.mark.parametrize("marker", [{"X-Nebula-Request-State": "pending"}, {}])
def test_saved_chat_acceptance_is_followed_to_its_turn_stream(
    tmp_path, no_wait, marker
):
    seen = []

    def handler(request):
        seen.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"state": "accepted", "turn_id": "turn-a", "session_id": "s"},
                headers={"X-Nebula-Command-Id": "command-a", **marker},
            )
        return httpx.Response(
            200,
            text='event: done\ndata: {"type":"done","turn_id":"turn-a"}\n\n',
            headers={"X-Nebula-Request-State": "complete"},
        )

    async def run():
        api = AsyncApi(_settings(tmp_path), transport=httpx.MockTransport(handler))
        try:
            return [item async for item in api.events("POST", "chat/completions")]
        finally:
            await api.close()

    assert asyncio.run(run()) == [
        ("accepted", {"type": "accepted", "command_id": "command-a"}),
        ("done", {"type": "done", "turn_id": "turn-a"}),
    ]
    assert seen == ["POST", "GET"]
