import asyncio
import json

from fastapi import FastAPI
import httpx
import pytest

from nebula.v3.database import DeploymentHostRow, RuntimeProcessRow, RuntimeResourceRow
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import ChatSession
from nebula.v3.runtime_coordination import CoordinationConflict, LEASE_MS
from nebula.v3.runtime_delivery import HttpIntent
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_http import PrivateRuntimeHttp, RuntimeHttpProxy
from nebula.v3.runtime_routing import RuntimeRouting
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_http import server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def intent(path="/api/v1/chat/completions", query=b"", payload=None):
    return HttpIntent.capture(
        "POST",
        path,
        query,
        [(b"content-type", b"application/json")],
        json.dumps(payload or {}).encode(),
    )


async def registered(routing, runtime_id="old", role="execution"):
    reference, key = routing.credentials.create()
    async with routing.coordination.database.session() as session:
        session.add(
            RuntimeProcessRow(
                runtime_id=runtime_id,
                role=role,
                version="synthetic",
                pid=999999,
                process_birth="inert-not-a-process",
                endpoint=f"unix:///tmp/synthetic-{runtime_id}.sock",
                credential_reference=reference,
                state="ready",
                updated_at_ms=1,
            )
        )
    return key


@pytest.mark.parametrize("reference", ["path", "query", "body", "nested"])
async def test_reference_resolution_uses_durable_project_identity(
    cluster, tmp_path, reference
):
    (first, _), stores, _clock = cluster
    await stores[0].create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Synthetic routing chat",
            model="synthetic-model",
            provider_profile_id="synthetic-provider",
        )
    )
    routing = RuntimeRouting(first, tmp_path / "private")
    requests = {
        "path": intent("/api/v1/chat/sessions/chat/goal"),
        "query": intent(query=b"engagement_id=project"),
        "body": intent(payload={"session_id": "chat"}),
        "nested": intent(payload={"context": {"project_id": "project"}}),
    }
    assert await routing.project_for(requests[reference]) == "project"


async def test_conflicting_or_missing_projects_are_not_sent_to_global_worker(
    cluster, tmp_path
):
    (first, _), stores, _clock = cluster
    await stores[0].create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Synthetic routing chat",
            model="synthetic-model",
            provider_profile_id="synthetic-provider",
        )
    )
    routing = RuntimeRouting(first, tmp_path / "private")
    with pytest.raises(CoordinationConflict, match="multiple projects"):
        await routing.project_for(
            intent(
                "/api/v1/chat/sessions/chat/goal", payload={"engagement_id": "other"}
            )
        )
    with pytest.raises(CoordinationConflict, match="does not exist"):
        await routing.project_for(intent(payload={"engagement_id": "missing"}))
    assert (
        await routing.project_for(intent("/api/v1/settings/defaults"))
        == GLOBAL_EXECUTION_PROJECT
    )


async def test_viewer_routes_only_to_live_execution_owner_and_credential(
    cluster, tmp_path
):
    (first, _), stores, clock = cluster
    await stores[0].create(
        ChatSession(
            id="chat",
            engagement_id="project",
            title="Synthetic routing chat",
            model="synthetic-model",
            provider_profile_id="synthetic-provider",
        )
    )
    routing = RuntimeRouting(first, tmp_path / "private")
    key = await registered(routing)
    await first.claim_project("project", "old")
    target = await routing.upstream_for(
        {"path": "/api/v1/chat/sessions/chat", "method": "GET"}
    )
    assert target.endpoint == "unix:///tmp/synthetic-old.sock"
    assert target.credential == key and target.lane == "read"
    clock[0] += LEASE_MS + 1
    with pytest.raises(RuntimeIpcError, match="no live"):
        await routing.upstream_for({"path": "/api/v1/chat/sessions/chat"})


async def test_controller_reference_follows_resource_project_instead_of_global(
    cluster, tmp_path
):
    (first, _), _, _clock = cluster
    routing = RuntimeRouting(first, tmp_path / "private")
    key = await registered(routing, "new")
    await first.claim_project("other", "new")
    async with first.database.session() as session:
        session.add(
            RuntimeResourceRow(
                id="pty-session",
                project_id="other",
                runtime_id="new",
                fencing_token=1,
                kind="pty",
                state="active",
                restoration="pinned",
                detail="inert synthetic PTY reference",
                checkpoint={},
                updated_at_ms=1,
            )
        )
    target = await routing.upstream_for(
        {"path": "/api/v1/terminals/pty-session/ws", "type": "websocket"}
    )
    assert target.credential == key and target.endpoint.endswith("new.sock")


async def test_public_listener_uses_only_the_promoted_healthy_api_role(
    cluster, tmp_path
):
    (first, _), _, _clock = cluster
    routing = RuntimeRouting(first, tmp_path / "private")
    await first.register_runtime("api", "synthetic", role="api")
    key = await registered(routing, "api", "api")
    async with first.database.session() as session:
        session.add(
            DeploymentHostRow(
                key="host",
                revision=1,
                api_runtime_id="api",
                detail="Synthetic promoted API",
            )
        )
    assert (await routing.api_upstream({})).credential == key
    async with first.database.session() as session:
        process = await session.get(RuntimeProcessRow, "api")
        process.state = "draining"
    with pytest.raises(RuntimeIpcError, match="unavailable"):
        await routing.api_upstream({})


async def test_reference_scanning_is_bounded_and_invalid_json_never_routes(
    cluster, tmp_path
):
    (first, _), _, _clock = cluster
    routing = RuntimeRouting(first, tmp_path / "private")
    oversized = intent(payload={"reference_ids": [str(i) for i in range(300)]})
    with pytest.raises(CoordinationConflict, match="too many"):
        await routing.project_for(oversized)
    malformed = HttpIntent.capture(
        "POST",
        "/api/v1/chat/completions",
        b"",
        [(b"content-type", b"application/json")],
        b"{bad json",
    )
    with pytest.raises(CoordinationConflict, match="Invalid JSON"):
        await routing.project_for(malformed)


async def test_promoted_api_controls_cross_first_hop_when_all_view_connections_are_busy(
    cluster, tmp_path
):
    (first, _), _, _clock = cluster
    routing = RuntimeRouting(first, tmp_path / "private")
    await first.register_runtime("api", "synthetic", role="api")
    key = await registered(routing, "api", "api")
    application = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    @application.get("/held")
    async def held():
        nonlocal count
        count += 1
        if count == 64:
            entered.set()
        await release.wait()
        return {"released": True}

    @application.post("/api/v1/chat/turns/synthetic/cancel")
    async def stop():
        return {"control": "accepted"}

    gate = PrivateRuntimeHttp(application, key)
    async with server(gate) as endpoint:
        async with first.database.session() as session:
            row = await session.get(RuntimeProcessRow, "api")
            row.endpoint = endpoint
            session.add(
                DeploymentHostRow(
                    key="host", revision=1, api_runtime_id="api", detail="Synthetic API"
                )
            )
        proxy = RuntimeHttpProxy(routing.api_upstream)
        requests = []
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=proxy),
                base_url="http://stable-listener",
            ) as client:
                requests = [asyncio.create_task(client.get("/held")) for _ in range(64)]
                await asyncio.wait_for(entered.wait(), 10)
                response = await asyncio.wait_for(
                    client.post("/api/v1/chat/turns/synthetic/cancel"), 2
                )
                assert response.status_code == 200 and response.json() == {
                    "control": "accepted"
                }
                assert len(gate.seen["control"]) == 1
                release.set()
                assert all(
                    response.status_code == 200
                    for response in await asyncio.gather(*requests)
                )
        finally:
            release.set()
            if requests:
                await asyncio.gather(*requests, return_exceptions=True)
            await proxy.close()
