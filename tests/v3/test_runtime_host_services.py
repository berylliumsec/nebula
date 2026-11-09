"""Real private host socket and detached inert workers, without live services."""

import asyncio
from pathlib import Path
import secrets
import threading
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.database import RuntimeInstanceRow, RuntimeProcessRow
from nebula.v3.execution_scope import execution_context
from nebula.v3.process_identity import same_process
from nebula.v3.runtime_host_services import (
    HOST_SOCKET_NAME,
    HostSharedServices,
    host_service_client,
)
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.shared_memory_rpc import RemoteButlerMemory
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _TOKEN
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_shared_butler_memory import remember

pytestmark = pytest.mark.anyio
_LIMITS = {"provider": 3, "background": 1, "tool": 2, "cpu": 2}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def peer(managed, identity, *, secret=None):
    if secret is None:
        async with managed.database.session() as session:
            row = await session.get(RuntimeProcessRow, identity.runtime_id)
            secret = managed.host.credentials.read(row.credential_reference)
    client = host_service_client(SimpleNamespace(identity=identity), secret)
    return RemoteButlerMemory(client)


@pytest.fixture
async def shared(managed):
    services = HostSharedServices(managed.host, capacity_limits=_LIMITS)
    await services.start()
    clients = []
    try:
        await managed.stage("v1")
        identity = await managed.host.spawn("v1", "execution")
        lease = await managed.coordinator.coordination.claim_project(
            "admitted-project", identity.runtime_id
        )
        remote = await peer(managed, identity)
        clients.append(remote.client)
        yield SimpleNamespace(
            services=services,
            managed=managed,
            identity=identity,
            lease=lease,
            remote=remote,
            clients=clients,
        )
    finally:
        await services.close()
        await asyncio.gather(*(client.close() for client in clients))


async def test_real_private_listener_serves_one_host_knowledge_index(managed, tmp_path):
    from nebula.v3.artifacts import ArtifactStore
    from nebula.v3.index_calls import index_call
    from nebula.v3.knowledge import ingest_document
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.remote_knowledge import RemoteKnowledgeIndex
    from tests.v3.test_knowledge_index import SecurityEmbeddingFunction

    index = ChromaKnowledgeIndex(
        tmp_path / "host-knowledge", embedding_function=SecurityEmbeddingFunction()
    )
    services = HostSharedServices(
        managed.host, knowledge_index=index, capacity_limits=_LIMITS
    )
    await services.start()
    memory, remote = None, None
    try:
        await managed.stage("v1")
        identity = await managed.host.spawn("v1", "execution")
        lease = await managed.coordinator.coordination.claim_project(
            "admitted-project", identity.runtime_id
        )
        memory = await peer(managed, identity)
        remote = RemoteKnowledgeIndex(memory.client, managed.data)
        with execution_context(lease):
            await remote.refresh()
            source = await ingest_document(
                store=managed.store,
                artifact_store=ArtifactStore(managed.data / "artifacts"),
                engagement_id="admitted-project",
                filename="synthetic.txt",
                data=b"Authentication login evidence.",
                knowledge_index=remote,
            )
            assert (
                await index_call(remote.query, "admitted-project", ["login"], limit=8)
            )[0].source_id == source.id
        assert services.memory.embedding_index is index
    finally:
        if remote is not None:
            await remote.close()
        await services.close()
        if memory is not None:
            await memory.client.close()


async def test_second_service_owner_cannot_replace_a_live_socket(shared):
    original = Path(shared.services.endpoint[7:]).stat().st_ino
    other = HostSharedServices(shared.managed.host)
    try:
        with pytest.raises(RuntimeIpcError, match="owner is already active"):
            await other.start()
        assert shared.managed.host.shared_services is shared.services
        assert Path(shared.services.endpoint[7:]).stat().st_ino == original
        with execution_context(shared.lease):
            assert (await remember(shared.remote, "admitted-project")).revision == 1
    finally:
        await other.close()
    assert shared.managed.host.shared_services is shared.services


async def test_shared_service_capabilities_require_a_valid_private_peer(shared):
    result = await shared.remote.client.call("host.services/v1", {})
    assert result == {
        "protocol": "nebula.host-services/v1",
        "memory": True,
        "capacity": True,
        "knowledge": False,
        "product_guidance": {"state": "unavailable"},
    }
    with pytest.raises(RuntimeIpcError):
        await shared.remote.client.call("host.services/v1", {"unexpected": True})


async def test_concurrent_starts_use_one_service_task_and_socket(managed):
    services = HostSharedServices(managed.host, capacity_limits=_LIMITS)
    try:
        await asyncio.gather(services.start(), services.start())
        task, inode = services.task, Path(services.endpoint[7:]).stat().st_ino
        await services.start()
        assert (
            services.task is task and Path(services.endpoint[7:]).stat().st_ino == inode
        )
        assert managed.host.shared_services is services
    finally:
        await services.close()
    assert managed.host.shared_services is None


async def test_direct_host_close_drains_an_accepted_index_operation(
    shared, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = shared.services.memory._index_one

    def held(row):
        entered.set()
        release.wait(10)
        return original(row)

    monkeypatch.setattr(shared.services.memory, "_index_one", held)
    accepted, closing = None, None
    try:
        with execution_context(shared.lease):
            accepted = asyncio.create_task(remember(shared.remote, "admitted-project"))
        assert await asyncio.to_thread(entered.wait, 3)
        closing = asyncio.create_task(shared.managed.host.close())
        async with asyncio.timeout(3):
            while not shared.services.closed:
                await asyncio.sleep(0.01)
        assert not closing.done() and shared.managed.host.lock.descriptor is not None
        assert shared.managed.host.shared_services is shared.services
        release.set()
        assert (await asyncio.wait_for(accepted, 5)).revision == 1
        await asyncio.wait_for(closing, 5)
        assert shared.managed.host.lock.descriptor is None
        assert shared.managed.host.shared_services is None
        assert same_process(shared.identity.pid, shared.identity.process_birth)
    finally:
        release.set()
        if accepted is not None:
            await asyncio.gather(accepted, return_exceptions=True)
        if closing is not None:
            await closing


async def test_direct_close_during_service_initialization_prevents_late_socket_publication(
    managed, monkeypatch
):
    from nebula.v3.shared_memory_import import CompatibilityMemoryImport

    entered, release = asyncio.Event(), asyncio.Event()
    original = CompatibilityMemoryImport.run

    async def held(importer):
        result = await original(importer)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(CompatibilityMemoryImport, "run", held)
    services = HostSharedServices(managed.host)
    starting = asyncio.create_task(services.start())
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(managed.host.close())
        async with asyncio.timeout(3):
            while not services.closed:
                await asyncio.sleep(0.01)
        assert not closing.done() and managed.host.lock.descriptor is not None
        release.set()
        with pytest.raises(RuntimeError, match="startup was detached"):
            await starting
        await asyncio.wait_for(closing, 5)
        assert services.task is None and managed.host.shared_services is None
        assert managed.host.lock.descriptor is None
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await services.close()
        if closing is not None:
            await closing


async def test_empty_explicit_capacity_is_held_without_claiming_a_service_owner(
    managed,
):
    services = HostSharedServices(managed.host, capacity_limits={})
    try:
        with pytest.raises(ValueError, match="explicit supported dimensions"):
            await services.start()
        assert managed.host.shared_services is None and services.task is None
        assert (await services.capacity.status())["revision"] == 0
    finally:
        await services.close()


async def test_host_synchronizes_current_product_guidance_and_preserves_it_on_restart(
    managed, tmp_path
):
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex
    from nebula.v3.product_knowledge import packaged_product_passages
    from tests.v3.test_knowledge_index import SecurityEmbeddingFunction

    index = ChromaKnowledgeIndex(
        tmp_path / "product-index", embedding_function=SecurityEmbeddingFunction()
    )
    digest, rows = packaged_product_passages()
    first = HostSharedServices(managed.host, knowledge_index=index)
    try:
        await first.start()
        assert first.product_guidance == {
            "state": "ready",
            "source_digest": digest,
            "passage_count": len(rows),
            "updated": True,
        }
        assert index._product_collection.count() == len(rows)
        assert index._product_collection.metadata["source_digest"] == digest
    finally:
        await first.close()
    second = HostSharedServices(managed.host, knowledge_index=index)
    try:
        await second.start()
        assert second.product_guidance["source_digest"] == digest
        assert second.product_guidance["updated"] is False
    finally:
        await second.close()


async def test_guidance_mirror_failure_is_reported_while_packaged_help_remains_available(
    managed, tmp_path, monkeypatch
):
    from nebula.v3.knowledge_index import ChromaKnowledgeIndex, KnowledgeIndexError
    from nebula.v3.product_knowledge import search_product_knowledge
    from tests.v3.test_knowledge_index import SecurityEmbeddingFunction

    index = ChromaKnowledgeIndex(
        tmp_path / "product-index", embedding_function=SecurityEmbeddingFunction()
    )

    def failed():
        raise KnowledgeIndexError("Synthetic unavailable mirror")

    monkeypatch.setattr(index, "sync_product_knowledge", failed)
    services = HostSharedServices(managed.host, knowledge_index=index)
    try:
        await services.start()
        assert (
            services.product_guidance == {"state": "degraded"}
            and services.server.started
        )
        assert search_product_knowledge("memory budgets", limit=2)
    finally:
        await services.close()


async def test_private_host_socket_requires_both_peer_identity_and_its_credential(
    shared,
):
    with execution_context(shared.lease):
        saved = await remember(shared.remote, "admitted-project")
        assert (await shared.remote.list("admitted-project")).items[0].id == saved.id
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=shared.services.endpoint[7:]),
        base_url="http://isolated",
    ) as anonymous:
        assert (
            await anonymous.post("/internal/nebula-runtime/v1/call", json={})
        ).status_code == 401
    wrong = await peer(shared.managed, shared.identity, secret=secrets.token_bytes(32))
    shared.clients.append(wrong.client)
    with execution_context(shared.lease), pytest.raises(RuntimeIpcError, match="401"):
        await wrong.list("admitted-project")
    assert not shared.services.memory.path.exists()


async def test_service_replacement_keeps_workers_memory_and_host_capacity(shared):
    with execution_context(shared.lease):
        saved = await remember(shared.remote, "admitted-project")
        await shared.remote._advance_event_cursor("admitted-project", 12)
    endpoint = shared.services.endpoint
    await shared.services.close()
    assert same_process(shared.identity.pid, shared.identity.process_birth)
    replacement = HostSharedServices(
        shared.managed.host, capacity_limits=_LIMITS | {"provider": 7}
    )
    try:
        await replacement.start()
        assert replacement.endpoint == endpoint
        assert (await replacement.capacity.status())["limits"] == _LIMITS
        with execution_context(shared.lease):
            assert (await shared.remote.list("admitted-project")).items[
                0
            ].id == saved.id
            assert await shared.remote._read_event_cursor("admitted-project") == 12
    finally:
        await replacement.close()
    assert not Path(endpoint[7:]).exists()


async def test_api_peers_and_expired_execution_peers_cannot_call_host_memory(shared):
    api = await shared.managed.host.spawn("v1", "api")
    forbidden = await peer(shared.managed, api)
    shared.clients.append(forbidden.client)
    with execution_context(shared.lease), pytest.raises(RuntimeIpcError, match="401"):
        await forbidden.list("admitted-project")
    async with shared.managed.database.session() as session:
        runtime = await session.get(RuntimeInstanceRow, shared.identity.runtime_id)
        runtime.lease_expires_at_ms = 0
    with execution_context(shared.lease), pytest.raises(RuntimeIpcError, match="401"):
        await shared.remote.list("admitted-project")


async def test_supervisor_detach_joins_host_index_work_before_releasing_ownership(
    managed, monkeypatch
):
    services = HostSharedServices(managed.host, capacity_limits=_LIMITS)
    supervisor = RuntimeSupervisor(
        managed.host, operator_token=_TOKEN, shared_services=services
    )
    await supervisor.start()
    remote, accepted, closing = None, None, None
    entered, release = threading.Event(), threading.Event()
    original = services.memory._index_one

    def held(row):
        entered.set()
        release.wait(10)
        return original(row)

    monkeypatch.setattr(services.memory, "_index_one", held)
    try:
        await managed.stage("v1")
        identity = await managed.host.spawn("v1", "execution")
        lease = await managed.coordinator.coordination.claim_project(
            "admitted-project", identity.runtime_id
        )
        remote = await peer(managed, identity)
        with execution_context(lease):
            accepted = asyncio.create_task(remember(remote, "admitted-project"))
        assert await asyncio.to_thread(entered.wait, 3)
        closing = asyncio.create_task(supervisor.close())
        async with asyncio.timeout(3):
            while not services.closed:
                await asyncio.sleep(0.01)
        assert not closing.done() and managed.host.lock.descriptor is not None
        release.set()
        assert (await asyncio.wait_for(accepted, 5)).revision == 1
        await asyncio.wait_for(closing, 5)
        assert managed.host.lock.descriptor is None
        assert same_process(identity.pid, identity.process_birth)
        assert not Path(services.endpoint[7:]).exists()
    finally:
        release.set()
        if accepted is not None:
            await asyncio.gather(accepted, return_exceptions=True)
        await supervisor.close()
        if remote is not None:
            await remote.client.close()


@pytest.mark.parametrize("kind", ["regular", "symlink"])
async def test_host_services_refuse_to_replace_a_non_socket_path(
    managed, tmp_path, kind
):
    path = managed.host.socket_root / HOST_SOCKET_NAME
    original = tmp_path / "preserved-file"
    original.write_text("Synthetic data must be preserved")
    if kind == "symlink":
        path.symlink_to(original)
    else:
        path.write_text("Synthetic data must be preserved")
    services = HostSharedServices(managed.host)
    try:
        with pytest.raises(RuntimeIpcError, match="owned Unix socket"):
            await services.start()
        assert (
            original.read_text()
            == path.read_text()
            == "Synthetic data must be preserved"
        )
    finally:
        await services.close()
