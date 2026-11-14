"""Stable listener checks use retained API processes and synthetic data only."""

import asyncio
from datetime import timedelta
import hashlib
import threading
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.process_identity import same_process
from nebula.v3.domain import Engagement, PairedDeviceSession, utc_now
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_http import HttpUpstream
from nebula.v3.runtime_routes import forwarding_lane
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_release_store import bundle
from tests.v3.test_runtime_http import server
from tests.v3.test_runtime_processes import _API_WORKER, _TOKEN
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def supervised(managed):
    admitted = []

    async def admission(manifest, role):
        admitted.append((manifest.version, role))

    supervisor = RuntimeSupervisor(
        managed.host, operator_token=_TOKEN, worker_admission=admission
    )
    await supervisor.start()
    try:
        yield SimpleNamespace(supervisor=supervisor, managed=managed, admitted=admitted)
    finally:
        await supervisor.close()


async def test_one_listener_routes_static_builds_through_api_promotion_and_code_rollback(
    supervised,
):
    root, fixture = supervised.supervisor, supervised.managed
    await fixture.stage("v1", _API_WORKER)
    await fixture.stage("v2", _API_WORKER)
    first = await root.prepare_api("v1")
    assert await root.prepare_api("v1") == first
    status = await root.coordinator.status()
    await root.promote_api(first.runtime_id, expected_revision=status.revision)
    async with server(root.app) as endpoint:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
            base_url="http://same-listener",
            trust_env=False,
        ) as client:
            one = await client.get("/")
            assert (
                one.status_code == 200 and one.headers["x-nebula-api-version"] == "v1"
            )
            assert (await client.get("/api/v1/deployment/status")).status_code == 401
            assert (
                await client.get("/internal/nebula-runtime/v1/call")
            ).status_code == 404
            second = await root.prepare_api("v2")
            assert same_process(first.pid, first.process_birth)
            assert (await client.get("/")).headers["x-nebula-api-version"] == "v1"
            status = await root.coordinator.status()
            await root.promote_api(second.runtime_id, expected_revision=status.revision)
            assert (await client.get("/")).headers["x-nebula-api-version"] == "v2"
            await fixture.store.update(
                Engagement,
                "synthetic-project",
                {"name": "Committed while v2 was active"},
            )
            # Retire the inactive API, launch its retained compatible code and
            # route back. SQL is never restored from a deployment snapshot.
            rollback = await root.prepare_api("v1")
            status = await root.coordinator.status()
            await root.promote_api(
                rollback.runtime_id, expected_revision=status.revision
            )
            assert (await client.get("/")).headers["x-nebula-api-version"] == "v1"
            assert (
                await fixture.store.get(Engagement, "synthetic-project")
            ).name == "Committed while v2 was active"
            assert supervised.admitted == [("v1", "api"), ("v2", "api"), ("v1", "api")]


async def test_supervisor_close_leaves_ready_api_alive_and_new_host_rediscovers_it(
    supervised,
):
    root, fixture = supervised.supervisor, supervised.managed
    await fixture.stage("v1", _API_WORKER)
    identity = await root.prepare_api("v1")
    await root.close()
    assert same_process(identity.pid, identity.process_birth)
    replacement = RuntimeSupervisor(fixture.new_host(), operator_token=_TOKEN)
    try:
        await replacement.start()
        observed = await replacement.host.rediscover()
        assert any(item.identity == identity and item.reachable for item in observed)
        assert await replacement.prepare_api("v1") == identity
    finally:
        await replacement.close()


async def test_missing_worker_reserve_holds_new_candidate_before_process_creation(
    managed,
):
    root = RuntimeSupervisor(managed.host, operator_token=_TOKEN)
    await root.start()
    try:
        await managed.stage("v1", _API_WORKER)
        with pytest.raises(CoordinationConflict, match="memory reserve"):
            await root.prepare_api("v1")
        assert not managed.host.children
    finally:
        await root.close()


async def test_close_waits_for_an_admitted_release_copy_before_releasing_host_authority(
    supervised, tmp_path, monkeypatch
):
    root = supervised.supervisor
    source = tmp_path / "stage-input"
    digest = bundle(source, version="stage-only")
    entered, release = threading.Event(), threading.Event()
    original = root.retained.install

    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(root.retained, "install", slow_copy)
    staging = asyncio.create_task(root.stage(source, manifest_sha256=digest))
    assert await asyncio.to_thread(entered.wait, 3)
    closing = asyncio.create_task(root.close())
    try:
        await asyncio.sleep(0.05)
        released_early = root.host.lock.descriptor is None
    finally:
        release.set()
        await asyncio.gather(staging, closing)
    assert not released_early


async def test_concurrent_close_callers_join_the_same_cleanup(supervised, monkeypatch):
    root = supervised.supervisor
    entered, release = asyncio.Event(), asyncio.Event()
    original = root.proxy.close

    async def delayed_close():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(root.proxy, "close", delayed_close)
    first = asyncio.create_task(root.close())
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(root.close())
    try:
        await asyncio.sleep(0.02)
        returned_early = second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert not returned_early


async def test_control_capacity_is_reserved_for_requests_with_a_credential(
    supervised, monkeypatch
):
    root, fixture = supervised.supervisor, supervised.managed
    now = utc_now()
    for device, revoked in (("live", None), ("revoked", now)):
        await fixture.store.create(
            PairedDeviceSession(
                id=device,
                name="Synthetic paired browser",
                token_sha256=hashlib.sha256(device.encode()).hexdigest(),
                csrf_sha256=hashlib.sha256(b"csrf").hexdigest(),
                last_used_at=now,
                idle_expires_at=now + timedelta(days=1),
                absolute_expires_at=now + timedelta(days=2),
                revoked_at=revoked,
            )
        )

    async def promoted(scope):
        return HttpUpstream(
            "unix:///synthetic/api.sock", b"k" * 32, forwarding_lane(scope)
        )

    monkeypatch.setattr(root.routing, "api_upstream", promoted)

    async def lane(method, path, *headers):
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(name.encode(), value.encode()) for name, value in headers],
        }
        return (await root._api_upstream(scope)).lane

    stop = "/api/v1/chat/turns/turn/cancel"
    decision = "/api/v1/approvals/approval/decision"
    assert await lane("POST", stop, ("authorization", "Bearer " + _TOKEN)) == "control"
    assert await lane("POST", decision, ("cookie", "nebula_device=live")) == "control"
    # Without a credential the request is forwarded as ordinary work; the API
    # worker still authenticates it and answers 401.
    assert await lane("POST", stop) == "work"
    assert await lane("POST", decision, ("authorization", "Bearer guess")) == "work"
    assert await lane("POST", stop, ("cookie", "nebula_device=revoked")) == "work"
    assert await lane("POST", stop, ("cookie", "nebula_device=unknown")) == "work"
    assert (
        await lane(
            "POST", "/api/v1/chat/completions", ("authorization", "Bearer " + _TOKEN)
        )
        == "work"
    )
    assert await lane("GET", stop) == "read"


async def test_unauthenticated_traffic_cannot_deny_an_operator_stop(supervised):
    """Real supervisor and API worker processes: neither a volume of
    unauthenticated control-path requests nor uploads that never finish keep
    the operator's Stop from reaching the API worker."""
    root, fixture = supervised.supervisor, supervised.managed
    await fixture.stage("v1", _API_WORKER)
    api = await root.prepare_api("v1")
    status = await root.coordinator.status()
    await root.promote_api(api.runtime_id, expected_revision=status.revision)
    async with server(root.app) as endpoint:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=endpoint[7:]),
            base_url="http://same-listener",
            trust_env=False,
            timeout=30,
        ) as client:

            async def operator_stop(turn):
                return await client.post(
                    f"/api/v1/chat/turns/{turn}/cancel",
                    json={},
                    headers={
                        "Authorization": "Bearer " + _TOKEN,
                        "Idempotency-Key": "stop-" + turn,
                    },
                )

            def reached_the_api_worker(response):
                # The unknown turn names no project: the API worker's command
                # path answered, not a capacity refusal on the way to it.
                return response.status_code == 409 and "project does not exist" in (
                    response.text
                )

            concurrency = asyncio.Semaphore(16)

            async def stranger(index):
                async with concurrency:
                    response = await client.post(
                        f"/api/v1/chat/turns/flood-{index}/cancel", json={}
                    )
                    return response.status_code

            # More than the 256 control requests the API worker's replay set
            # held per 30 s window before requests were numbered.
            assert set(await asyncio.gather(*(stranger(i) for i in range(300)))) == {
                401
            }
            assert reached_the_api_worker(await operator_stop("after-volume"))

            # More unfinished uploads than the 16 control connections.
            writers = []
            try:
                for index in range(20):
                    _reader, writer = await asyncio.open_unix_connection(endpoint[7:])
                    writer.write(
                        b"POST /api/v1/chat/turns/slow-%d/cancel HTTP/1.1\r\n"
                        b"Host: same-listener\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 4096\r\n\r\n{" % index
                    )
                    await writer.drain()
                    writers.append(writer)
                async with asyncio.timeout(10):
                    while sum(root.proxy.active.values()) < 20:
                        await asyncio.sleep(0.05)
                # Queued behind them, the Stop waited out the proxy's 5 s
                # connection wait and was answered 503.
                assert reached_the_api_worker(await operator_stop("during-uploads"))
            finally:
                for writer in writers:
                    writer.close()
