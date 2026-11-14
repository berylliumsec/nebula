"""Local RPC authenticates both directions, versions and bounded replay state."""

import json
import secrets
import time
from uuid import uuid4

import httpx
import pytest

from nebula.v3.deployment_protocol import IPC_PROTOCOL
from nebula.v3.runtime_ipc import (
    MAX_RPC_BYTES,
    RPC_PATH,
    RuntimeCredentials,
    RuntimeIpcError,
    RuntimeRpcClient,
    RuntimeRpcServer,
    _encode,
    _mac,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_host_credentials_are_private_and_references_cannot_escape(tmp_path):
    credentials = RuntimeCredentials(tmp_path / "credentials")
    reference, key = credentials.create()
    assert credentials.read(reference) == key
    assert (credentials.directory / reference).stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeIpcError):
        credentials.read("../outside")
    (credentials.directory / reference).chmod(0o644)
    with pytest.raises(RuntimeIpcError, match="private"):
        credentials.read(reference)


def test_reserved_credential_reference_is_immutable_and_cannot_escape(tmp_path):
    credentials = RuntimeCredentials(tmp_path / "credentials")
    reference = str(uuid4())
    actual, key = credentials.create(reference=reference)
    assert actual == reference and credentials.read(reference) == key
    with pytest.raises(FileExistsError):
        credentials.create(reference=reference)
    with pytest.raises(RuntimeIpcError):
        credentials.create(reference="../escape")
    assert len(list(credentials.directory.iterdir())) == 1


async def test_authenticated_rpc_returns_only_matching_authenticated_response():
    key = secrets.token_bytes(32)
    server = RuntimeRpcServer(key)

    async def status(payload):
        return {"observed": payload["revision"]}

    server.register("host.status", status)
    client = RuntimeRpcClient(
        "unix:///isolated/host.sock", key, transport=httpx.ASGITransport(app=server.app)
    )
    try:
        assert await client.call("host.status", {"revision": 3}) == {"observed": 3}
    finally:
        await client.close()


async def test_client_stops_reading_an_unbounded_response_before_buffering_it():
    class Endless(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self):
            while True:
                self.reads += 1
                yield b"x" * (64 * 1024)

        async def aclose(self):
            self.closed = True

    stream = Endless()

    async def oversized(_request):
        return httpx.Response(200, stream=stream)

    client = RuntimeRpcClient(
        "unix:///isolated/host.sock",
        secrets.token_bytes(32),
        transport=httpx.MockTransport(oversized),
    )
    try:
        with pytest.raises(RuntimeIpcError, match="bound"):
            await client.call("host.status", {})
        assert stream.reads == MAX_RPC_BYTES // (64 * 1024) + 1
        assert stream.closed
    finally:
        await client.close()


async def test_replay_and_wrong_credentials_never_dispatch_a_second_operation():
    key = secrets.token_bytes(32)
    server = RuntimeRpcServer(key)
    calls = []

    async def handler(payload):
        calls.append(payload)
        return {"saved": True}

    server.register("host.change", handler)
    body = _encode(
        {
            "protocol": IPC_PROTOCOL,
            "request_id": "accepted",
            "operation": "host.change",
            "payload": {"revision": 1},
        }
    )
    timestamp, nonce = str(int(time.time())), secrets.token_hex(24)
    headers = {
        "X-Nebula-IPC-Time": timestamp,
        "X-Nebula-IPC-Nonce": nonce,
        "X-Nebula-IPC-MAC": _mac(key, "request", timestamp, nonce, body),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://local"
    ) as client:
        assert (
            await client.post(RPC_PATH, content=body, headers=headers)
        ).status_code == 200
        assert (
            await client.post(RPC_PATH, content=body, headers=headers)
        ).status_code == 409
        headers["X-Nebula-IPC-MAC"] = "a" * 64
        assert (
            await client.post(RPC_PATH, content=body, headers=headers)
        ).status_code == 401
    assert calls == [{"revision": 1}]


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "nebula.runtime/v2"},
        {"operation": {}},
        {"payload": []},
        {"unexpected": True},
    ],
)
async def test_incompatible_or_malformed_signed_envelopes_are_rejected(change):
    key = secrets.token_bytes(32)
    server = RuntimeRpcServer(key)
    body = _encode(
        {
            **{
                "protocol": IPC_PROTOCOL,
                "request_id": "fixture",
                "operation": "host.status",
                "payload": {},
            },
            **change,
        }
    )
    timestamp, nonce = str(int(time.time())), secrets.token_hex(24)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://local"
    ) as client:
        response = await client.post(
            RPC_PATH,
            content=body,
            headers={
                "X-Nebula-IPC-Time": timestamp,
                "X-Nebula-IPC-Nonce": nonce,
                "X-Nebula-IPC-MAC": _mac(key, "request", timestamp, nonce, body),
            },
        )
    assert response.status_code == 422


async def test_client_rejects_forged_response_and_oversized_messages():
    async def forged(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol": IPC_PROTOCOL,
                "request_id": body["request_id"],
                "result": {"promoted": True},
            },
            headers={"X-Nebula-IPC-MAC": "b" * 64},
        )

    client = RuntimeRpcClient(
        "unix:///isolated/host.sock",
        secrets.token_bytes(32),
        transport=httpx.MockTransport(forged),
    )
    try:
        with pytest.raises(RuntimeIpcError, match="authentication"):
            await client.call("host.promote", {})
        with pytest.raises(RuntimeIpcError, match="bound"):
            await client.call("host.promote", {"data": "x" * MAX_RPC_BYTES})
    finally:
        await client.close()


async def test_handler_refusal_reaches_the_caller_as_its_signed_reason():
    from nebula.v3.runtime_coordination import CoordinationConflict

    key = secrets.token_bytes(32)
    server = RuntimeRpcServer(key)

    async def unverified(_payload):
        raise RuntimeIpcError(
            "Project cleanup is unverified; its ownership is retained"
        )

    async def conflicted(_payload):
        raise CoordinationConflict("Previously paused work changed during quiescence")

    async def private(_payload):
        raise ValueError("synthetic-private-statement")

    server.register("execution.quiesce/v1", unverified)
    server.register("execution.release/v1", conflicted)
    server.register("execution.private/v1", private)
    # A real worker serves through uvicorn: an unexpected handler failure is a
    # bare 500 to the caller, never a raised server exception.
    client = RuntimeRpcClient(
        "unix:///isolated/worker.sock",
        key,
        transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
    )
    try:
        with pytest.raises(RuntimeIpcError) as refused:
            await client.call("execution.quiesce/v1", {})
        assert str(refused.value) == (
            "Project cleanup is unverified; its ownership is retained"
        )
        with pytest.raises(RuntimeIpcError) as conflict:
            await client.call("execution.release/v1", {})
        assert str(conflict.value) == "Previously paused work changed during quiescence"
        with pytest.raises(RuntimeIpcError) as hidden:
            await client.call("execution.private/v1", {})
        assert str(hidden.value) == "runtime IPC operation failed (500)"
    finally:
        await client.close()


async def test_unsigned_or_forged_refusal_is_only_a_transport_failure():
    async def forged(request):
        body = json.loads(request.content)
        return httpx.Response(
            409,
            json={
                "protocol": IPC_PROTOCOL,
                "request_id": body["request_id"],
                "refusal": {"type": "RuntimeIpcError", "detail": "forged reason"},
            },
            headers={"X-Nebula-IPC-MAC": "c" * 64},
        )

    client = RuntimeRpcClient(
        "unix:///isolated/worker.sock",
        secrets.token_bytes(32),
        transport=httpx.MockTransport(forged),
    )
    try:
        with pytest.raises(RuntimeIpcError) as failed:
            await client.call("execution.quiesce/v1", {})
        assert str(failed.value) == "runtime IPC operation failed (409)"
    finally:
        await client.close()
