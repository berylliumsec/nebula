"""One durable Butler transcript is reserved across clients and Core restarts."""

import asyncio
import pytest
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from asyncio import Barrier

from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_sessions import ButlerSessionReservation, reserve_butler_session
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatBackend,
    ChatSession,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
)
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import _profile


def _runtime(store: NebulaStore, tmp_path) -> HarnessRuntimeService:
    return HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )


async def test_provider_reservation_is_atomic_across_clients_and_core_restart(tmp_path):
    path = tmp_path / "butler-provider-reservation.db"
    store = NebulaStore(path)
    project = await store.create(Engagement(name="Selected project"))
    profile = await store.create(_profile(local=True))
    request = ButlerSessionReservation(
        engagement_id=project.id,
        backend=ChatBackend.PROVIDER,
        provider_id=profile.id,
    )
    barrier = Barrier(6)
    connections = [NebulaStore(path) for _ in range(6)]

    async def reserve(index: int) -> ChatSession:
        await barrier.wait()
        # Separate stores exercise the same database boundary used by two
        # Core processes, rather than one in-process lock.
        connection = connections[index]
        return await reserve_butler_session(
            connection, _runtime(connection, tmp_path), request
        )

    sessions = list((await asyncio.gather(*(reserve(_item) for _item in range(6)))))

    assert len({session.id for session in sessions}) == 1
    assert len((await store.list_entities(ChatSession, engagement_id=project.id))) == 1
    assert sessions[0].metadata["control_assistant"] is True
    restarted = NebulaStore(path)
    assert (
        await reserve_butler_session(restarted, _runtime(restarted, tmp_path), request)
    ).id == sessions[0].id


async def test_harness_reservation_commits_one_chat_and_vendor_binding(tmp_path):
    path = tmp_path / "butler-harness-reservation.db"
    store = NebulaStore(path)
    project = await store.create(Engagement(name="Selected project"))
    profile = await store.create(
        HarnessProfile(
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="test-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    request = ButlerSessionReservation(
        engagement_id=project.id,
        backend=ChatBackend.HARNESS,
        harness_profile_id=profile.id,
        model="test-model",
    )
    barrier = Barrier(4)
    connections = [NebulaStore(path) for _ in range(4)]

    async def reserve(index: int) -> ChatSession:
        await barrier.wait()
        connection = connections[index]
        return await reserve_butler_session(
            connection, _runtime(connection, tmp_path), request
        )

    sessions = list((await asyncio.gather(*(reserve(_item) for _item in range(4)))))

    assert len({session.id for session in sessions}) == 1
    assert len((await store.list_entities(ChatSession, engagement_id=project.id))) == 1
    vendors = await store.list_entities(HarnessSession, engagement_id=project.id)
    assert len(vendors) == 1
    assert vendors[0].id == sessions[0].harness_session_id
    assert vendors[0].metadata["control_assistant"] is True
    restarted = NebulaStore(path)
    assert (
        await reserve_butler_session(restarted, _runtime(restarted, tmp_path), request)
    ).id == sessions[0].id


async def test_reservation_api_is_project_scoped_and_reuses_active_session(tmp_path):
    store = NebulaStore(tmp_path / "butler-reservation-api.db")
    first = await store.create(Engagement(name="First project"))
    second = await store.create(Engagement(name="Second project"))
    profile = await store.create(_profile(local=True))
    app = create_app(store, auth_token="test-token")
    browser_mcp = app.state.agent_security_tools.register_profile()
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    payload = {
        "engagement_id": first.id,
        "backend": "provider",
        "provider_id": profile.id,
        "mcp_server_ids": [browser_mcp.id],
    }

    assert (
        await client.post("/api/v1/chat/control-assistant/session", json=payload)
    ).status_code == 401
    created = await client.post(
        "/api/v1/chat/control-assistant/session", json=payload, headers=headers
    )
    assert created.status_code == 200, created.text
    assert created.json()["model"] == "model-a"
    again = await client.post(
        "/api/v1/chat/control-assistant/session", json=payload, headers=headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == created.json()["id"]
    other = await client.post(
        "/api/v1/chat/control-assistant/session",
        json={**payload, "engagement_id": second.id},
        headers=headers,
    )
    assert other.status_code == 200, other.text
    assert other.json()["id"] != created.json()["id"]
    assert (
        await store.get(ChatSession, created.json()["id"])
    ).provider_profile_id == profile.id
    assert (await store.get(ChatSession, created.json()["id"])).metadata[
        "mcp_server_ids"
    ] == [browser_mcp.id]
    assert len((await store.list_entities(ChatSession, engagement_id=first.id))) == 1
    assert len((await store.list_entities(ChatSession, engagement_id=second.id))) == 1


pytestmark = pytest.mark.anyio
