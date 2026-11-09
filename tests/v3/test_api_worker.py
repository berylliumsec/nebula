"""Public authentication and real SQL delivery without an execution factory."""

import asyncio
from datetime import timedelta
import hashlib
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.api_worker import create_api_worker
from nebula.v3.database import Database, RuntimeCommandRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import Engagement, PairedDeviceSession, utc_now
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.runtime_delivery import DurableHttpRunner, RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from nebula.v3.storage import NebulaStore
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
_TOKEN = "synthetic-api-worker-operator"
_AUTH = {"Authorization": "Bearer " + _TOKEN, "Idempotency-Key": "synthetic-key"}
_POOL = PoolSettings(5, 12, 5, 2)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def boundary(location, tmp_path):
    host = NebulaStore(Database(location, pool_settings=_POOL))
    await host.create(Engagement(id="project", name="Synthetic API project"))
    coordination = RuntimeCoordination(host.database)
    await coordination.register_runtime("execution", "v1")
    lease = await coordination.claim_project("project", "execution")
    vault = RuntimeVault(tmp_path / "private")
    stores, clients, apps = [], [], []

    async def project_for(_intent):
        return "project"

    async def upstream_for(_scope):
        pytest.fail(
            "this selected journey must not fall back to a private HTTP invocation"
        )

    def api(**options):
        store = NebulaStore(Database(location, bootstrap=False, pool_settings=_POOL))
        stores.append(store)
        delivery = RuntimeDelivery(
            RuntimeCoordination(store.database), RuntimeVault(vault.root)
        )
        app = create_api_worker(
            store,
            delivery,
            operator_token=_TOKEN,
            project_for=project_for,
            upstream_for=upstream_for,
            command_start_timeout=0.03,
            poll_interval=0.01,
            **options,
        )
        apps.append(app)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://synthetic.test"
        )
        clients.append(client)
        return app, client

    try:
        yield SimpleNamespace(
            host=host,
            coordination=coordination,
            lease=lease,
            api=api,
            delivery=RuntimeDelivery(coordination, vault),
        )
    finally:
        for client in clients:
            await client.aclose()
        for app in apps:
            await app.state.viewer_proxy.close()
        for store in [*stores, host]:
            await store.event_history.close()
            await store.database.dispose()


async def reply(_scope, _receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 201,
            "headers": [
                (b"content-type", b"application/json"),
                (b"set-cookie", b"one=synthetic"),
                (b"set-cookie", b"two=synthetic"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b'{"saved":true}'})


async def test_api_replacement_replays_saved_response_without_reexecution(boundary):
    first_app, first = boundary.api()
    accepted = await first.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "inert"}
    )
    assert accepted.status_code == 202
    command = accepted.json()["receipt"]["command_id"]
    # The first API closes before any execution begins.
    await first.aclose()
    await first_app.state.viewer_proxy.close()
    calls = []

    async def execution(scope, receive, send):
        calls.append(scope["state"]["runtime_command_id"])
        await reply(scope, receive, send)

    runner = DurableHttpRunner(boundary.delivery, execution, operator_token=_TOKEN)
    assert await runner.run_one(boundary.lease, lane="work")
    _, replacement = boundary.api()
    response = await replacement.get(
        f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
    )
    assert response.status_code == 201 and response.json() == {"saved": True}
    assert response.headers.get_list("set-cookie") == ["one=synthetic", "two=synthetic"]
    retry = await replacement.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "inert"}
    )
    assert retry.status_code == 201 and retry.headers["x-nebula-command-id"] == command
    assert not await runner.run_one(boundary.lease, lane="work")
    assert calls == [command]
    changed = await replacement.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "different"}
    )
    assert changed.status_code == 409
    beyond = await replacement.get(
        f"/api/v1/runtime/requests/{command}/response?after=3", headers=_AUTH
    )
    assert beyond.status_code == 201 and beyond.content == b""


async def test_live_memory_apply_uses_control_while_original_work_is_running(boundary):
    _, client = boundary.api()
    work_entered, release_work = asyncio.Event(), asyncio.Event()
    handled = []

    async def application(scope, _receive, send):
        handled.append(scope["path"])
        if scope["path"] == "/api/v1/synthetic":
            work_entered.set()
            await release_work.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"observed":true}'})

    accepted = await client.post(
        "/api/v1/synthetic", headers={**_AUTH, "Idempotency-Key": "allocator"}, json={}
    )
    assert accepted.status_code == 202
    runner = DurableHttpRunner(boundary.delivery, application, operator_token=_TOKEN)
    work = asyncio.create_task(runner.run_one(boundary.lease, lane="work"))
    try:
        await asyncio.wait_for(work_entered.wait(), timeout=3)
        accepted = await client.post(
            "/api/v1/memory-budgets/jobs/original-job/apply",
            headers={**_AUTH, "Idempotency-Key": "apply"},
            json={"acknowledged": True},
        )
        assert accepted.status_code == 202
        command = accepted.json()["receipt"]["command_id"]
        async with boundary.host.database.session() as session:
            row = await session.get(RuntimeCommandRow, command)
            assert row.lane == "control"
        assert await runner.run_one(boundary.lease, lane="control")
        response = await client.get(
            f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
        )
        assert response.status_code == 200 and response.json() == {"observed": True}
        assert not work.done()
        assert handled == [
            "/api/v1/synthetic",
            "/api/v1/memory-budgets/jobs/original-job/apply",
        ]
    finally:
        release_work.set()
        await work


async def test_authentication_csrf_and_key_checks_precede_acceptance(boundary):
    _, client = boundary.api()
    assert (await client.post("/api/v1/synthetic", json={})).status_code == 401
    assert (
        await client.post(
            "/api/v1/synthetic", headers={"Authorization": "Bearer " + _TOKEN}, json={}
        )
    ).status_code == 428
    assert (await client.get("/api/v1/deployment/status")).status_code == 401
    assert (
        await client.get("/api/v1/deployment/status", headers=_AUTH)
    ).status_code == 200
    now = utc_now()
    await boundary.host.create(
        PairedDeviceSession(
            id="browser",
            name="Synthetic paired browser",
            token_sha256=hashlib.sha256(b"device-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"csrf-token").hexdigest(),
            last_used_at=now,
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )
    client.cookies.set("nebula_device", "device-token")
    client.cookies.set("nebula_csrf", "csrf-token")
    assert (
        await client.post(
            "/api/v1/synthetic",
            headers={"Idempotency-Key": "forged", "X-Nebula-CSRF": "csrf-token"},
            json={},
        )
    ).status_code == 403
    async with boundary.host.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeCommandRow))
            == 0
        )
    accepted = await client.post(
        "/api/v1/synthetic",
        headers={
            "Idempotency-Key": "paired",
            "X-Nebula-CSRF": "csrf-token",
            "Origin": "https://synthetic.test",
        },
        json={},
    )
    assert accepted.status_code == 202
    command = accepted.json()["receipt"]["command_id"]
    async with boundary.host.database.session() as session:
        row = await session.get(RuntimeCommandRow, command)
        assert row.actor_id == "device:browser"
    # A second authenticated device cannot read another device's request body.
    operator = await client.get(f"/api/v1/runtime/requests/{command}", headers=_AUTH)
    assert operator.status_code == 404


async def test_viewer_cancellation_does_not_cancel_execution_or_block_controls(
    boundary,
):
    _, client = boundary.api(maximum_waiting_views=1)
    accepted = await client.post("/api/v1/synthetic", headers=_AUTH, json={})
    command = accepted.json()["receipt"]["command_id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def producer(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"data: first\n\n",
                "more_body": True,
            }
        )
        entered.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"data: done\n\n"})

    worker = asyncio.create_task(
        DurableHttpRunner(boundary.delivery, producer, operator_token=_TOKEN).run_one(
            boundary.lease, lane="work"
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    reader = asyncio.create_task(
        client.get(f"/api/v1/runtime/requests/{command}/response", headers=_AUTH)
    )
    await asyncio.sleep(0.02)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert not worker.done()
    control = await client.post(
        "/api/v1/chat/turns/synthetic/cancel",
        headers={**_AUTH, "Idempotency-Key": "stop"},
        json={},
    )
    assert control.status_code == 202
    assert await DurableHttpRunner(
        boundary.delivery, reply, operator_token=_TOKEN
    ).run_one(boundary.lease, lane="control")
    release.set()
    assert await asyncio.wait_for(worker, 5)
    reconnect = await client.get(
        f"/api/v1/runtime/requests/{command}/response", headers=_AUTH
    )
    assert (
        reconnect.status_code == 200
        and reconnect.content == b"data: first\n\ndata: done\n\n"
    )


async def test_api_factory_rejects_migration_ownership(boundary):
    with pytest.raises(ValueError, match="never migrate"):
        create_api_worker(
            boundary.host,
            boundary.delivery,
            operator_token=_TOKEN,
            project_for=None,
            upstream_for=None,
        )


@pytest.mark.parametrize(
    "path",
    [
        "harness-interactions/synthetic/decision",
        "browser-companion/synthetic/actions/synthetic-action",
        "browser-intercepts/synthetic/decision",
        "browser-actions/synthetic/decision",
        "harness-turns/synthetic/steer",
        "runs/synthetic/steer",
    ],
)
async def test_decisions_and_revocation_bypass_running_work(boundary, path):
    _, client = boundary.api()
    work = await client.post("/api/v1/synthetic", headers=_AUTH, json={})
    assert work.status_code == 202
    running = await boundary.coordination.claim_next(boundary.lease, lane="work")
    assert running is not None
    control = await client.request(
        "DELETE" if path.startswith("auth/devices/") else "POST",
        "/api/v1/" + path,
        headers={**_AUTH, "Idempotency-Key": "decision"},
        json={},
    )
    assert control.status_code == 202
    calls = []

    async def resolve(scope, receive, send):
        calls.append(scope["path"])
        await reply(scope, receive, send)

    assert await DurableHttpRunner(
        boundary.delivery, resolve, operator_token=_TOKEN
    ).run_one(boundary.lease, lane="control")
    assert calls == ["/api/v1/" + path]
    assert await boundary.coordination.claim_next(boundary.lease, lane="work") is None


def test_api_role_import_does_not_import_the_execution_factory_or_chat_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import nebula.v3.api_worker; "
                "assert 'nebula.v3.execution_kernel' not in sys.modules; "
                "assert 'nebula.v3.chat' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
