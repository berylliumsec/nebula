"""Production composition owns one deferred Chroma client behind the host lock."""

import asyncio
import ipaddress
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from nebula.v3.database import Database, RuntimeProcessRow
from nebula.v3.knowledge_index import ChromaKnowledgeIndex, KnowledgeIndexError
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.product_knowledge import (
    packaged_product_passages,
    search_product_knowledge,
)
from nebula.v3.runtime_host_services import HostSharedServices
from nebula.v3.runtime_launcher import discovery, operator_token
from nebula.v3.runtime_service import initialize_database, serve_runtime
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_release_store import bundle
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _TOKEN
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer " + _TOKEN}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module", autouse=True)
def generated_index():
    from scripts.build_product_knowledge import ensure

    ensure()


@pytest.fixture
def cold_model(monkeypatch):
    import nebula.v3.knowledge_index as index_module

    monkeypatch.setattr(index_module, "_default_model_ready", lambda: False)

    def forbidden_embedding(self, input):
        pytest.fail("Core startup must use packaged vectors without a model download")

    monkeypatch.setattr(
        index_module._TrackedDefaultEmbeddingFunction, "__call__", forbidden_embedding
    )


async def test_default_supervisor_opens_one_index_after_lock_and_reopens_same_digest(
    managed, monkeypatch, cold_model
):
    path = managed.data / "knowledge-index"
    monkeypatch.delenv("NEBULA_V3_KNOWLEDGE_INDEX_DIR", raising=False)
    original = ChromaKnowledgeIndex.__init__
    opened = []

    def observe(index, *args, **kwargs):
        owners = [
            host.shared_services for host in managed.hosts if host.shared_services
        ]
        assert len(owners) == 1 and owners[0].host.lock.descriptor is not None
        original(index, *args, **kwargs)
        opened.append(index)

    monkeypatch.setattr(ChromaKnowledgeIndex, "__init__", observe)
    first = RuntimeSupervisor(managed.host, operator_token=_TOKEN)
    assert first.shared_services.knowledge is None and not path.exists()
    digest, rows = packaged_product_passages()
    try:
        await first.start()
        index = first.shared_services.knowledge.index
        assert opened == [index] and index.path == path
        assert index.status.state == "required"
        assert first.shared_services.memory.embedding_index is index
        assert first.shared_services.product_guidance == {
            "state": "ready",
            "source_digest": digest,
            "passage_count": len(rows),
            "updated": True,
        }
        assert index._product_collection.metadata["source_digest"] == digest
    finally:
        await first.close()
    assert index._client._closed
    replacement = managed.new_host()
    second = RuntimeSupervisor(replacement, operator_token=_TOKEN)
    try:
        await second.start()
        reopened = second.shared_services.knowledge.index
        assert opened == [index, reopened] and reopened is not index
        assert reopened.path == path and not reopened._client._closed
        assert second.shared_services.memory.embedding_index is reopened
        assert second.shared_services.product_guidance["source_digest"] == digest
        assert second.shared_services.product_guidance["updated"] is False
        assert reopened._product_collection.count() == len(rows)
    finally:
        await second.close()
    assert reopened._client._closed


async def test_no_factory_runs_before_exclusive_host_and_shared_owner(managed):
    calls = []

    def forbidden(root):
        calls.append(root)
        pytest.fail("A competing host must not open the index")

    replacement = managed.new_host()
    detached = HostSharedServices(replacement, knowledge_index_factory=forbidden)
    try:
        with pytest.raises(RuntimeError, match="exclusive supervisor ownership"):
            await detached.start()
        with pytest.raises(BlockingIOError):
            await replacement.start()
        assert not calls
    finally:
        await detached.close()
    owner = HostSharedServices(managed.host)
    duplicate = HostSharedServices(managed.host, knowledge_index_factory=forbidden)
    try:
        await owner.start()
        with pytest.raises(RuntimeError, match="already active"):
            await duplicate.start()
        assert not calls and managed.host.shared_services is owner
    finally:
        await duplicate.close()
        await owner.close()


@pytest.mark.parametrize("failure", [ImportError, OSError, KnowledgeIndexError])
async def test_optional_index_failure_keeps_authenticated_recovery(
    managed, failure, monkeypatch
):
    import nebula.v3.runtime_host_services as host_module

    recorded = []
    monkeypatch.setattr(
        host_module,
        "record_caught_exception",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    def unavailable(_root):
        raise failure("Synthetic unavailable local knowledge backend")

    services = HostSharedServices(managed.host, knowledge_index_factory=unavailable)
    supervisor = RuntimeSupervisor(
        managed.host, shared_services=services, operator_token=_TOKEN
    )
    try:
        await supervisor.start()
        source, _installed = await managed.stage("recovery-knowledge")
        await supervisor.select_recovery_release("recovery-knowledge")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=supervisor.app), base_url="http://fixture"
        ) as client:
            assert (await client.get("/api/v1/health")).status_code == 401
            response = await client.get("/api/v1/health", headers=AUTH)
            assert response.status_code == 200 and not response.json()["api_ready"]
            assert (await client.get("/settings")).status_code == 200
        assert source.exists()
        assert services.server.started and services.recovery.task is not None
        assert services.product_guidance == {"state": "degraded"}
        assert services.knowledge is None and services.memory.embedding_index is None
        assert services.memory._index_status().state == "lexical_fallback"
        assert search_product_knowledge("memory budgets", limit=1)
        assert recorded[0][0][1] == "knowledge.host_index_initialization_failed"
        assert isinstance(recorded[0][0][3], failure)
    finally:
        await supervisor.close()


async def test_cancelled_factory_is_joined_and_owned_client_closed_before_unlock(
    managed,
):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def close():
        assert managed.host.lock.descriptor is not None
        closed.set()

    def blocking(_root):
        assert managed.host.shared_services is services
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(close=close)

    services = HostSharedServices(managed.host, knowledge_index_factory=blocking)
    startup = asyncio.create_task(services.start())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        startup.cancel()
        await asyncio.sleep(0.03)
        assert not startup.done() and not closed.is_set()
        with pytest.raises(BlockingIOError):
            await managed.new_host().start()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert closed.is_set() and services.knowledge is None
        assert services.memory.embedding_index is None
        assert managed.host.shared_services is None
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        await services.close()


async def test_production_serve_runtime_syncs_owned_product_collection_on_lan(
    location, tmp_path, monkeypatch, cold_model
):
    addresses = {
        entry[4][0]
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        if not ipaddress.ip_address(entry[4][0]).is_loopback
    }
    assert addresses, "The production bootstrap fixture requires a non-loopback LAN"
    address = sorted(addresses)[0]
    data = tmp_path / "production-core"
    path = data / "custom-knowledge-index"
    monkeypatch.delenv("NEBULA_V3_DATABASE_URL", raising=False)
    assert operator_token(data, _TOKEN) == _TOKEN
    monkeypatch.setenv("NEBULA_V3_DATABASE_URL", str(location))
    budgets = {
        "ADMIN_RESERVE": "5",
        "OTHER_CONNECTIONS": "12",
        "PEAK_PROCESSES": "5",
        "POOL_SIZE": "2",
        "POOL_TIMEOUT": "5",
    }
    postgres = (
        make_url(Database._normalize_sync_url(location)).get_backend_name()
        == "postgresql"
    )
    for key, value in budgets.items():
        if postgres:
            monkeypatch.setenv("NEBULA_V3_DB_" + key, value)
        else:
            monkeypatch.delenv("NEBULA_V3_DB_" + key, raising=False)
    monkeypatch.setenv("NEBULA_V3_KNOWLEDGE_INDEX_DIR", str(path))
    monkeypatch.setenv("NEBULA_CALLBACK_ORIGIN", "http://fixture.invalid")
    database = Database(location, bootstrap=False)
    try:
        await initialize_database(database, data)
        # A saved floor-only policy deliberately holds API admission. The normal
        # production composition must still publish authenticated host recovery.
        await MemoryBudgetService(database).status()
    finally:
        await database.dispose()
    source = tmp_path / "production-bundle"
    digest = bundle(source, code=b"#!/bin/sh\nexit 90\n")
    created = []
    original = HostSharedServices.__init__

    def observe(services, *args, **kwargs):
        original(services, *args, **kwargs)
        assert services.host.lock.descriptor is None and not path.exists()
        assert services.knowledge is None
        created.append(services)

    monkeypatch.setattr(HostSharedServices, "__init__", observe)
    task = asyncio.create_task(
        serve_runtime(
            data_root=data,
            bundle=source,
            manifest_sha256=digest,
            host=address,
            port=0,
            token=_TOKEN,
        )
    )
    try:
        async with asyncio.timeout(30):
            while (connection := discovery(data)) is None:
                if task.done():
                    await task
                    pytest.fail("Core exited before publishing discovery")
                await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                base_url=connection["endpoint"],
                headers=AUTH,
                trust_env=False,
                timeout=2,
            ) as client:
                health = await client.get("/health")
                assert health.status_code == 200 and health.json()["api_ready"] is False
                assert (
                    await client.get("/health", headers={"Authorization": ""})
                ).status_code == 401
                assert (
                    await client.get(
                        connection["endpoint"].removesuffix("/api/v1") + "/settings"
                    )
                ).status_code == 200
                assert len(created) == 1
                services = created[0]
                index = services.knowledge.index
                current_digest, rows = packaged_product_passages()
                from scripts.build_product_knowledge import source_digest, source_paths

                assert current_digest == source_digest(source_paths())
                assert index.path == path
                assert services.memory.embedding_index is index
                assert index.status.state == "required"
                assert services.product_guidance["state"] == "ready"
                assert services.product_guidance["source_digest"] == current_digest
                assert (
                    index._product_collection.metadata["source_digest"]
                    == current_digest
                )
                assert index._product_collection.count() == len(rows)
                async with services.coordination.database.session() as session:
                    assert (
                        await session.scalar(
                            select(func.count()).select_from(RuntimeProcessRow)
                        )
                        == 0
                    )
                status = await services.host.coordinator.status()
                stopped = await client.post(
                    "/deployment/operations",
                    json={
                        "operation_id": "knowledge-bootstrap-stop",
                        "action": "shutdown",
                        "parameters": {"expected_revision": status.revision},
                    },
                )
                assert (
                    stopped.status_code == 200
                    and stopped.json()["state"] == "succeeded"
                ), stopped.text
                print(
                    f"Production Core knowledge proof: {connection['endpoint']} "
                    f"source_digest={current_digest} passages={len(rows)} "
                    f"database={services.coordination.database.engine.dialect.name}"
                )
            await task
        assert index._client._closed and discovery(data) is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
