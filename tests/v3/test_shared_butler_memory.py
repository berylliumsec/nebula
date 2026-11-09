"""Shared host memory uses authenticated IPC and the same project SQL fence."""

import asyncio
from dataclasses import asdict
import secrets
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.butler_memory import (
    MemoryConflictError,
    MemoryNotFoundError,
    UnsafeMemoryError,
)
from nebula.v3.database import ButlerMemoryItemRow
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import LEASE_MS, LeaseLost
from nebula.v3.runtime_ipc import RPC_PATH, RuntimeRpcClient, RuntimeRpcServer
from nebula.v3.shared_butler_memory import SharedButlerMemory
from nebula.v3.shared_memory_rpc import (
    MEMORY_OPERATION,
    RemoteButlerMemory,
    register_shared_memory,
)
from tests.v3.test_butler_memory import LocalEmbedding
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def boundary(cluster, tmp_path):
    coordinators, stores, clock = cluster
    host = SharedButlerMemory(coordinators[0], tmp_path / "host-index")
    clients, servers = [], []

    def peer(memory, runtime_id):
        server = RuntimeRpcServer(secrets.token_bytes(32))
        register_shared_memory(server, memory, runtime_id=runtime_id)
        client = RuntimeRpcClient(
            "unix:///isolated/host-memory",
            server.secret,
            transport=httpx.ASGITransport(app=server.app),
        )
        servers.append(server)
        clients.append(client)
        return RemoteButlerMemory(client)

    leases = [
        await c.claim_project(project, runtime)
        for c, project, runtime in zip(
            coordinators, ("project", "other"), ("old", "new")
        )
    ]
    proxies = [peer(host, runtime) for runtime in ("old", "new")]
    try:
        yield SimpleNamespace(
            host=host,
            proxies=proxies,
            leases=leases,
            clients=clients,
            coordinators=coordinators,
            stores=stores,
            clock=clock,
            peer=peer,
            servers=servers,
            tmp_path=tmp_path,
        )
    finally:
        await asyncio.gather(*(client.close() for client in clients))


async def remember(
    memory,
    project="project",
    *,
    scope="project_lesson",
    content="Check source evidence before finalizing the current report.",
    kind="verified_core_result",
):
    return await memory._save(
        source_project_id=project,
        scope=scope,
        content=content,
        source_kind=kind,
        source_label="Verified synthetic result",
        source_ref="synthetic-source",
    )


async def test_global_recollections_redact_source_and_local_memories_stay_scoped(
    boundary,
):
    own, other = boundary.proxies
    with execution_context(boundary.leases[0]):
        preference = await own.capture_operator_preference(
            "project", "I prefer concise progress summaries.", source_ref="private-note"
        )
        general = await remember(own, scope="global_lesson")
        local = await remember(
            own, content="Keep the selected project's private evidence in its notebook."
        )
        assert local.id in {item.id for item in (await own.list("project")).items}
    with execution_context(boundary.leases[1]):
        visible = (await other.list("other")).items
        assert {item.id for item in visible} == {preference.id, general.id}
        assert all(
            item.source_ref is None and item.engagement_id is None for item in visible
        )
        assert [
            item.id for item in (await other.search("other", "source evidence")).items
        ] == [general.id]
        context = await other.turn_context("other", "evidence")
        assert "concise progress" in context and "private-note" not in context
        assert "private evidence" not in context


async def test_two_generations_deduplicate_shared_global_content(boundary):
    async def save(proxy, lease):
        with execution_context(lease):
            return await remember(proxy, lease.project_id, scope="global_lesson")

    first, second = await asyncio.gather(
        *(save(proxy, lease) for proxy, lease in zip(boundary.proxies, boundary.leases))
    )
    assert first.id == second.id
    async with boundary.coordinators[0].database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ButlerMemoryItemRow))
            == 1
        )


async def test_concurrent_memory_edits_conflict_without_lost_updates(boundary):
    own = boundary.proxies[0]
    with execution_context(boundary.leases[0]):
        saved = await remember(own)
        results = await asyncio.gather(
            *(
                own.update(
                    "project",
                    saved.id,
                    f"Keep the saved evidence current before the {name} review.",
                    expected_revision=saved.revision,
                )
                for name in ("first", "second")
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, MemoryConflictError) for item in results) == 1
        current = next(item for item in results if not isinstance(item, BaseException))
        assert current.revision == 2
        with pytest.raises(MemoryConflictError):
            await own.delete("project", saved.id, expected_revision=1)
        await own.delete("project", saved.id, expected_revision=2)
        assert not (await own.list("project")).items


async def test_shared_memory_and_cursors_survive_host_replacement(boundary):
    own = boundary.proxies[0]
    with execution_context(boundary.leases[0]):
        saved = await remember(own)
        await own._advance_event_cursor("project", 25)
        replacement = SharedButlerMemory(
            boundary.coordinators[1], boundary.tmp_path / "host-index"
        )
        restarted = boundary.peer(replacement, "old")
        assert (await restarted.list("project")).items[0].id == saved.id
        await restarted._advance_event_cursor("project", 12)
        assert await restarted._read_event_cursor("project") == 25
        assert not replacement.path.exists()
        assert not hasattr(own, "_collection") and not hasattr(own, "path")


async def test_expired_worker_cannot_change_shared_memory_after_project_claim(boundary):
    own = boundary.proxies[0]
    stale = boundary.leases[0]
    with execution_context(stale):
        saved = await remember(own)
    boundary.clock[0] += LEASE_MS + 1
    coordinator = boundary.coordinators[1]
    await coordinator.register_runtime("replacement", "next-release")
    lease = await coordinator.claim_project("project", "replacement")
    current = boundary.peer(boundary.host, "replacement")
    with execution_context(stale):
        with pytest.raises(LeaseLost):
            await own.delete("project", saved.id, expected_revision=1)
        with pytest.raises(LeaseLost):
            await own._advance_event_cursor("project", 40)
    with execution_context(lease):
        assert (await current.list("project")).items[0].id == saved.id
        assert await current._read_event_cursor("project") == 0


async def test_context_and_peer_identity_cannot_select_another_project(boundary):
    own = boundary.proxies[0]
    with pytest.raises(LeaseLost):
        await own.list("project")
    with execution_context(boundary.leases[0]):
        with pytest.raises(LeaseLost):
            await own.list("other")
    rejected = await own.client.call(
        MEMORY_OPERATION,
        {
            "lease": asdict(boundary.leases[1]),
            "action": "list",
            "arguments": {},
        },
    )
    assert rejected["ok"] is False and rejected["error"] == "lease_lost"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=boundary.servers[0].app),
        base_url="http://isolated",
    ) as anonymous:
        assert (await anonymous.post(RPC_PATH, json={})).status_code == 401


async def test_foreign_local_memory_cannot_be_edited_or_deleted(boundary):
    with execution_context(boundary.leases[0]):
        saved = await remember(boundary.proxies[0])
    with execution_context(boundary.leases[1]):
        with pytest.raises(MemoryNotFoundError):
            await boundary.proxies[1].update(
                "other",
                saved.id,
                "Replace this with unrelated synthetic content.",
                expected_revision=1,
            )
        with pytest.raises(MemoryNotFoundError):
            await boundary.proxies[1].delete("other", saved.id, expected_revision=1)


async def test_review_novelty_and_cursors_use_host_persistence(boundary):
    own = boundary.proxies[0]
    content = "When progress stalls, compare cited evidence before revising the plan because the next step depends on current observations."
    with execution_context(boundary.leases[0]):
        assert await own._novel_review_lesson("project", content)
        await remember(own, content=content, kind="butler_review_candidate")
        assert not await own._novel_review_lesson("project", content)
        await own._advance_event_cursor("project", 2)
        assert await own._read_event_cursor("project") == 2
    with execution_context(boundary.leases[1]):
        assert await boundary.proxies[1]._novel_review_lesson("other", content)
        assert await boundary.proxies[1]._read_event_cursor("other") == 0


async def test_invalid_candidates_and_protocol_calls_do_not_persist_or_echo_secrets(
    boundary,
):
    own = boundary.proxies[0]
    with execution_context(boundary.leases[0]):
        with pytest.raises(UnsafeMemoryError) as rejected:
            await remember(
                own,
                content="Remember API key sk-test-sensitive-123456789012345 for later.",
            )
        assert "sk-test-sensitive" not in str(rejected.value)
        with pytest.raises(ValueError, match="supported bounds"):
            await own.list("project", limit=1000)
        result = await own.client.call(
            MEMORY_OPERATION,
            {
                "lease": asdict(boundary.leases[0]),
                "action": "arbitrary-sql",
                "arguments": {"sql": "DELETE FROM entities"},
            },
        )
        assert result["ok"] is False and result["error"] == "invalid"
        assert not (await own.list("project")).items


async def test_lease_loss_during_memory_write_rolls_back_sql_before_indexing(
    boundary, monkeypatch
):
    def expire():
        boundary.clock[0] += LEASE_MS + 1
        return "2026-11-09T00:00:00+00:00"

    monkeypatch.setattr(boundary.host, "_utc", expire)
    with execution_context(boundary.leases[0]):
        with pytest.raises(LeaseLost):
            await remember(boundary.proxies[0])
    async with boundary.coordinators[0].database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ButlerMemoryItemRow))
            == 0
        )
    assert boundary.host._collection is None


async def test_one_host_index_serves_workers_and_deleted_vectors_do_not_resurrect(
    boundary,
):
    boundary.host.embedding_index = LocalEmbedding()
    with execution_context(boundary.leases[0]):
        saved = await remember(boundary.proxies[0], scope="global_lesson")
        assert boundary.host._collection is not None
        assert boundary.host._collection.count() == 1
    with execution_context(boundary.leases[1]):
        found = await boundary.proxies[1].search("other", "proof evidence")
        assert (
            found.index_status.state == "vector_ready" and found.items[0].id == saved.id
        )
        await boundary.proxies[1].delete("other", saved.id, expected_revision=1)
    with execution_context(boundary.leases[0]):
        assert not (await boundary.proxies[0].search("project", "proof evidence")).items
        assert boundary.host._collection.count() == 0
    assert all(not hasattr(proxy, "_collection") for proxy in boundary.proxies)
