"""Desktop reattachment changes volatile bridge authority, never saved sessions."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from nebula.v3.browser_companion import BrowserCompanion, CompanionRequest
from nebula.v3.browser_companion_tools import attached_session
from nebula.v3.browser_engine import BrowserEngineRegistry
from nebula.v3.database import RuntimeProcessRow
from nebula.v3.domain import BrowserIdentity, BrowserSession, ChatSession
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_browser_bridge import (
    BRIDGE_OPERATION,
    BridgeRegistration,
    RemoteBrowserBridge,
)
from nebula.v3.runtime_coordination import LeaseLost
from nebula.v3.runtime_host_services import host_service_client
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _TOKEN, managed as managed

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer " + _TOKEN}
PATH = "/api/v1/desktop/browser-bridge"
TOKEN = "synthetic-bridge-token-0000000000"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def bridge_host(managed):
    root = RuntimeSupervisor(managed.host, operator_token=_TOKEN)
    await root.start()
    clock = [1000.0]
    root.shared_services.browser_bridge.clock = lambda: clock[0]
    clients = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=root.app), base_url="http://fixture"
        ) as public:
            yield SimpleNamespace(
                root=root, managed=managed, public=public, clock=clock, clients=clients
            )
    finally:
        for client in clients:
            await client.close()
        await root.close()


async def execution_peer(fixture):
    await fixture.managed.stage("v1")
    identity = await fixture.managed.host.spawn("v1", "execution")
    lease = await fixture.root.coordinator.coordination.claim_project(
        "admitted-project", identity.runtime_id
    )
    async with fixture.managed.database.session() as session:
        row = await session.get(RuntimeProcessRow, identity.runtime_id)
        secret = fixture.managed.host.credentials.read(row.credential_reference)
    client = host_service_client(SimpleNamespace(identity=identity), secret)
    fixture.clients.append(client)
    return lease, RemoteBrowserBridge(client)


def registration(endpoint="http://127.0.0.1:17890", **changes):
    return {
        "endpoint": endpoint,
        "token": TOKEN,
        "device_id": "desktop-test-device",
        **changes,
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:17890",
        "http://127.1:17890",
        "http://127.0.0.2:17890",
        "http://127.0.0.1:17890/",
        "http://127.0.0.1:17890/path",
        "http://127.0.0.1:17890?secret=x",
        "http://user@127.0.0.1:17890",
        "https://127.0.0.1:17890",
        "http://192.0.2.3:17890",
        "http://127.0.0.1:0",
    ],
)
async def test_bridge_rejects_noncanonical_or_remote_origins(endpoint):
    with pytest.raises(ValidationError):
        BridgeRegistration.model_validate(registration(endpoint))


async def test_bridge_registration_is_local_bounded_and_exact_viewer_scoped(
    bridge_host,
):
    client = bridge_host.public
    body = registration()
    assert (await client.post(PATH, json=body)).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=bridge_host.root.app, client=("192.0.2.3", 123)
        ),
        base_url="http://fixture",
    ) as remote:
        assert (await remote.post(PATH, headers=AUTH, json=body)).status_code == 403
    invalid = await client.post(
        PATH, headers=AUTH, json={**body, "token": "secret with spaces"}
    )
    assert invalid.status_code == 422 and "secret with spaces" not in invalid.text
    first = await client.post(PATH, headers=AUTH, json=body)
    assert first.status_code == 200 and TOKEN not in first.text
    assert first.headers["cache-control"] == "private, no-store"
    old_id = first.json()["registration_id"]
    refreshed = await client.post(
        PATH, headers=AUTH, json={**body, "registration_id": old_id}
    )
    assert refreshed.json()["registration_id"] == old_id
    assert (
        await client.post(
            PATH,
            headers=AUTH,
            json={
                **body,
                "registration_id": old_id,
                "token": "changed-viewer-token-000000",
            },
        )
    ).status_code == 409
    second = await client.post(
        PATH, headers=AUTH, json=registration(device_id="desktop-reopened")
    )
    new_id = second.json()["registration_id"]
    assert new_id != old_id
    assert (
        await client.post(PATH, headers=AUTH, json={**body, "registration_id": old_id})
    ).status_code == 409
    assert (
        await client.request(
            "DELETE",
            PATH,
            headers=AUTH,
            json={"device_id": body["device_id"], "registration_id": old_id},
        )
    ).status_code == 409
    assert (
        await client.post(PATH, headers=AUTH, content=b"x" * (16 * 1024 + 1))
    ).status_code == 413
    revoked = await client.request(
        "DELETE",
        PATH,
        headers=AUTH,
        json={"device_id": "desktop-reopened", "registration_id": new_id},
    )
    assert revoked.json() == {"revoked": True}
    broker = bridge_host.root.shared_services.browser_bridge
    assert broker.enabled and broker.viewer is None


async def test_private_bridge_requires_exact_live_project_grant_and_expires(
    bridge_host,
):
    lease, remote = await execution_peer(bridge_host)
    await bridge_host.public.post(PATH, headers=AUTH, json=registration())
    with pytest.raises(LeaseLost):
        await remote.resolve()
    with (
        execution_context(replace(lease, project_id=GLOBAL_EXECUTION_PROJECT)),
        pytest.raises(LeaseLost),
    ):
        await remote.resolve()
    with execution_context(lease):
        state = await remote.resolve()
        assert state.enabled and state.bridge.token.get_secret_value() == TOKEN
    with pytest.raises(RuntimeIpcError):
        await remote.client.call(
            BRIDGE_OPERATION,
            {"lease": asdict(replace(lease, project_id="synthetic-project"))},
        )
    with pytest.raises(RuntimeIpcError):
        await remote.client.call(
            BRIDGE_OPERATION,
            {"lease": asdict(replace(lease, fencing_token=lease.fencing_token + 1))},
        )
    bridge_host.clock[0] += 181
    with execution_context(lease):
        state = await remote.resolve()
        assert state.enabled and state.bridge is None
    await bridge_host.root.close()
    with (
        execution_context(lease),
        pytest.raises(
            ValueError, match="desktop browser is disconnected"
        ) as disconnected,
    ):
        await remote.resolve()
    assert isinstance(disconnected.value.__cause__, httpx.TransportError)


@asynccontextmanager
async def local_browser(label, *, disconnect=False):
    requests = []

    async def respond(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines = head.split("\r\n")
            headers = {
                name.lower(): value
                for name, value in (
                    line.split(": ", 1) for line in lines[1:] if ": " in line
                )
            }
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            requests.append((lines[0].split()[1], headers.get("authorization"), body))
            if disconnect:
                return
            result = (
                {"ready": True}
                if requests[-1][0] == "/v1/identities/ensure"
                else {
                    "tabs": [
                        {"id": label, "url": "https://example.test", "title": label}
                    ],
                    "active_tab_id": label,
                }
            )
            encoded = json.dumps(result).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(encoded)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + encoded
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        await server.wait_closed()


async def test_existing_companion_uses_reopened_viewer_without_worker_or_env_restart(
    bridge_host, monkeypatch
):
    lease, remote = await execution_peer(bridge_host)
    store = bridge_host.managed.store
    chat = await store.create(
        ChatSession(
            engagement_id=lease.project_id,
            title="Saved chat",
            model="fixture",
            provider_profile_id="fixture",
        )
    )
    identity = await store.create(
        BrowserIdentity(engagement_id=lease.project_id, name="Saved identity")
    )
    session = await store.create(
        BrowserSession(
            engagement_id=lease.project_id,
            identity_id=identity.id,
            name="Saved browser",
            runtime_kind="electron_visible",
            metadata={"browser_companion_version": 1, "conversation_id": chat.id},
        )
    )
    service = BrowserCompanion(store, BrowserEngineRegistry([]))
    service.bind_bridge(remote.resolve)
    monkeypatch.delenv("NEBULA_ONE_BROWSER_CUTOVER", raising=False)
    monkeypatch.setenv("NEBULA_VISIBLE_BROWSER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("NEBULA_VISIBLE_BROWSER_TOKEN", "obsolete-environment-secret")
    async with (
        local_browser("first") as (one, first_calls),
        local_browser("reopened") as (two, second_calls),
    ):
        before = await bridge_host.public.post(
            PATH, headers=AUTH, json=registration(one)
        )
        with execution_context(lease):
            assert (
                await attached_session(store, lease.project_id, chat.id) == session.id
            )
            held_adapter = await service.adapter()
            assert (
                await service.request(session.id, CompanionRequest(operation="tabs"))
            )["active_tab_id"] == "first"
        after = await bridge_host.public.post(
            PATH,
            headers=AUTH,
            json=registration(two, token="new-viewer-token-00000000000000"),
        )
        with execution_context(lease):
            other = BrowserCompanion(store, BrowserEngineRegistry([]))
            assert (
                await other.request(session.id, CompanionRequest(operation="tabs"))
            )["active_tab_id"] == "reopened"
            current = await held_adapter._request(
                "POST", "/v1/companion/" + identity.id, {"operation": "tabs"}
            )
            assert current.json()["active_tab_id"] == "reopened"
        assert all(call[1] == "Bearer " + TOKEN for call in first_calls)
        assert all(
            call[1] == "Bearer new-viewer-token-00000000000000" for call in second_calls
        )
        assert any(
            call[0] == "/v1/identities/ensure" and call[2]["identity_id"] == identity.id
            for call in second_calls
        )
        assert before.json()["registration_id"] != after.json()["registration_id"]
        await bridge_host.public.request(
            "DELETE",
            PATH,
            headers=AUTH,
            json={
                "registration_id": after.json()["registration_id"],
                "device_id": "desktop-test-device",
            },
        )
        with (
            execution_context(lease),
            pytest.raises(ValueError, match="desktop browser is disconnected"),
        ):
            await service.request(session.id, CompanionRequest(operation="tabs"))
        assert (await store.get(BrowserSession, session.id)).identity_id == identity.id


async def test_bridge_disconnect_does_not_retry_an_admitted_mutation(bridge_host):
    lease, remote = await execution_peer(bridge_host)
    service = BrowserCompanion(bridge_host.managed.store, BrowserEngineRegistry([]))
    service.bind_bridge(remote.resolve)
    async with local_browser("disconnect", disconnect=True) as (endpoint, requests):
        await bridge_host.public.post(PATH, headers=AUTH, json=registration(endpoint))
        with execution_context(lease):
            adapter = await service.adapter()
            with pytest.raises(httpx.TransportError):
                await adapter._request(
                    "POST", "/v1/companion/identity", {"operation": "click"}
                )
        assert len(requests) == 1
