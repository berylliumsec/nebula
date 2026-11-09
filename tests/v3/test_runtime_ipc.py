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
