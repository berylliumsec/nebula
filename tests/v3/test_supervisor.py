"""Stable listener checks use retained API processes and synthetic data only."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.process_identity import same_process
from nebula.v3.domain import Engagement
from nebula.v3.runtime_coordination import CoordinationConflict
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
