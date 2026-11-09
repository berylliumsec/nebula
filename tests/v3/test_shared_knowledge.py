"""Shared-index ownership and provenance with local deterministic Chroma only."""

import asyncio
from dataclasses import asdict
import secrets
import threading
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import RuntimeIndexStateRow
from nebula.v3.domain import KnowledgeSource
from nebula.v3.execution_scope import execution_context
from nebula.v3.index_calls import index_call, index_digest
from nebula.v3.knowledge import ingest_document, ingest_library_item, reindex_document
from nebula.v3.knowledge_index import ChromaKnowledgeIndex, KnowledgeIndexError
from nebula.v3.knowledge_rerank import RerankerStatus
from nebula.v3.remote_knowledge import RemoteKnowledgeIndex
from nebula.v3.runtime_coordination import LEASE_MS, LeaseLost
from nebula.v3.runtime_ipc import RuntimeRpcClient, RuntimeRpcServer
from nebula.v3.shared_knowledge_rpc import INDEX_OPERATION, SharedKnowledge
from tests.v3.test_knowledge_index import SecurityEmbeddingFunction
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def boundary(cluster, tmp_path):
    coordinators, stores, clock = cluster
    root = tmp_path / "host"
    index = ChromaKnowledgeIndex(
        root / "knowledge-index", embedding_function=SecurityEmbeddingFunction()
    )
    host = SimpleNamespace(
        data_root=root, coordinator=SimpleNamespace(coordination=coordinators[0])
    )
    service = SharedKnowledge(host, index)
    leases = [
        await c.claim_project(project, runtime)
        for c, project, runtime in zip(
            coordinators, ("project", "other"), ("old", "new")
        )
    ]
    clients, proxies = [], []

    def peer(runtime_id):
        server = RuntimeRpcServer(secrets.token_bytes(32))
        service.register(server, runtime_id)
        client = RuntimeRpcClient(
            "unix:///isolated/shared-index",
            server.secret,
            transport=httpx.ASGITransport(app=server.app),
        )
        clients.append(client)
        remote = RemoteKnowledgeIndex(client, root)
        proxies.append(remote)
        return remote

    remotes = [peer(name) for name in ("old", "new")]
    for remote, lease in zip(remotes, leases):
        with execution_context(lease):
            await remote.refresh()
    try:
        yield SimpleNamespace(
            index=index,
            service=service,
            remotes=remotes,
            leases=leases,
            stores=stores,
            coordinators=coordinators,
            clock=clock,
            peer=peer,
            root=root,
            artifacts=ArtifactStore(root / "artifacts"),
        )
    finally:
        await asyncio.gather(*(remote.close() for remote in proxies))
        await asyncio.gather(*(client.close() for client in clients))


async def ingest(
    boundary, *, index=0, data=b"Authentication login requires a valid credential."
):
    with execution_context(boundary.leases[index]):
        return await ingest_document(
            store=boundary.stores[index],
            artifact_store=boundary.artifacts,
            engagement_id=boundary.leases[index].project_id,
            filename="synthetic.txt",
            data=data,
            knowledge_index=boundary.remotes[index],
        )


async def test_shared_chroma_keeps_project_results_and_provenance_separate(boundary):
    own = await ingest(boundary)
    other = await ingest(
        boundary, index=1, data=b"Database storage uses SQL transactions."
    )
    for index, expected in enumerate((own, other)):
        with execution_context(boundary.leases[index]):
            matches = await index_call(
                boundary.remotes[index].query,
                boundary.leases[index].project_id,
                ["login database"],
                limit=8,
            )
            assert {item.source_id for item in matches} == {expected.id}
            assert all(
                item.index_digest == expected.metadata["runtime_index_digest"]
                for item in matches
            )
            assert boundary.remotes[index].status.state == "ready"
            assert not hasattr(boundary.remotes[index], "_collection")


async def test_global_library_queries_use_the_same_host_index_across_projects(boundary):
    with execution_context(boundary.leases[0]):
        item = await ingest_library_item(
            store=boundary.stores[0],
            artifact_store=boundary.artifacts,
            filename="global.txt",
            data=b"A reusable authentication and TLS checklist.",
            knowledge_index=boundary.remotes[0],
        )
    with execution_context(boundary.leases[1]):
        matches = await index_call(
            boundary.remotes[1].query_library, ["authentication"], limit=8
        )
        assert [match.source_id for match in matches] == [item.id]
        assert matches[0].scope == "library"


async def test_remote_index_refuses_unbound_foreign_and_event_loop_calls(boundary):
    with pytest.raises(LeaseLost):
        await index_call(boundary.remotes[0].query, "project", ["login"], limit=8)
    with execution_context(boundary.leases[0]):
        with pytest.raises(LeaseLost):
            await index_call(boundary.remotes[0].query, "other", ["login"], limit=8)
        with pytest.raises(KnowledgeIndexError, match="worker thread"):
            boundary.remotes[0].query("project", ["login"], limit=8)
    response = await boundary.remotes[0].client.call(
        INDEX_OPERATION,
        {
            "lease": asdict(boundary.leases[1]),
            "action": "query",
            "arguments": {"kind": "engagement", "queries": ["login"], "limit": 8},
        },
    )
    assert response["ok"] is False and response["error"] == "lease_lost"


async def test_reindex_and_delete_publish_only_current_sql_sources(boundary):
    source = await ingest(boundary)
    with execution_context(boundary.leases[0]):
        updated = await reindex_document(
            store=boundary.stores[0],
            artifact_store=boundary.artifacts,
            source_id=source.id,
            knowledge_index=boundary.remotes[0],
        )
        assert (
            updated.metadata["runtime_index_request"]
            != source.metadata["runtime_index_request"]
        )
        await boundary.stores[0].delete(KnowledgeSource, source.id)
        assert not await index_call(
            boundary.remotes[0].query, "project", ["login"], limit=8
        )
        await index_call(boundary.remotes[0].delete_source, source.id)
    assert boundary.index._collection.count() == 0
    async with boundary.coordinators[0].database.session() as session:
        assert (await session.get(RuntimeIndexStateRow, source.id)).state == "deleted"


async def test_lease_loss_during_indexing_cannot_publish_ready_and_new_owner_can_rebuild(
    boundary, monkeypatch
):
    original = boundary.index.upsert_source

    def expired(source, chunks):
        original(source, chunks)
        boundary.clock[0] += LEASE_MS + 1

    monkeypatch.setattr(boundary.index, "upsert_source", expired)
    with pytest.raises(LeaseLost):
        await ingest(boundary)
    source = (await boundary.stores[0].list_entities(KnowledgeSource))[0]
    async with boundary.coordinators[0].database.session() as session:
        assert (await session.get(RuntimeIndexStateRow, source.id)).state == "failed"
    monkeypatch.setattr(boundary.index, "upsert_source", original)
    coordinator = boundary.coordinators[1]
    await coordinator.register_runtime("replacement", "next-version")
    lease = await coordinator.claim_project("project", "replacement")
    replacement = boundary.peer("replacement")
    with execution_context(lease):
        await replacement.refresh()
        assert not await index_call(replacement.query, "project", ["login"], limit=8)
        rebuilt = await reindex_document(
            store=boundary.stores[1],
            artifact_store=boundary.artifacts,
            source_id=source.id,
            knowledge_index=replacement,
        )
        assert (await index_call(replacement.query, "project", ["login"], limit=8))[
            0
        ].source_id == rebuilt.id


async def test_stale_query_chunks_are_filtered_after_a_new_digest_is_published(
    boundary, monkeypatch
):
    source = await ingest(boundary)
    original = boundary.index.query
    entered, proceed = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        matches = original(*args, **kwargs)
        entered.set()
        proceed.wait(5)
        return matches

    monkeypatch.setattr(boundary.index, "query", held)
    pending = None
    try:
        with execution_context(boundary.leases[0]):
            pending = asyncio.create_task(
                index_call(boundary.remotes[0].query, "project", ["login"], limit=8)
            )
        assert await asyncio.to_thread(entered.wait, 3)
        chunks = [
            {
                "id": "new-parser-chunk",
                "text": "The updated parser selected network TLS context.",
                "artifact_id": source.artifact_id,
            }
        ]
        newer = await boundary.stores[0].update(
            KnowledgeSource,
            source.id,
            {
                "status": "indexing",
                "metadata": {
                    **source.metadata,
                    "runtime_index_request": str(uuid4()),
                    "runtime_index_digest": index_digest(chunks),
                },
            },
            expected_revision=source.revision,
        )
        with execution_context(boundary.leases[0]):
            await index_call(boundary.remotes[0].upsert_source, newer, chunks)
            await boundary.stores[0].update(
                KnowledgeSource,
                newer.id,
                {"status": "ready"},
                expected_revision=newer.revision,
            )
        proceed.set()
        assert await asyncio.wait_for(pending, 5) == []
    finally:
        proceed.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


async def test_large_index_content_uses_encrypted_bulk_refs_and_known_complete_cleanup(
    boundary,
):
    data = "\n".join(
        f"{i:05d} Authentication storage TLS evidence: " + "synthetic detail " * 10
        for i in range(12000)
    ).encode()
    assert 2 * 1024**2 < len(data) < 4 * 1024**2
    source = await ingest(boundary, data=data)
    assert source.status == "ready" and source.document_count > 1000
    payloads = list((boundary.root / "runtime" / "index-payloads").iterdir())
    assert [path.name for path in payloads] == ["key"]


async def test_tools_and_dense_embeddings_use_the_host_model_without_local_workers(
    boundary,
):
    remote = boundary.remotes[0]
    with execution_context(boundary.leases[0]):
        await index_call(
            remote.index_tools,
            {
                "authentication-tool": "Read local login authentication configuration.",
                "network-tool": "List TLS network listeners.",
            },
        )
        ranked = await index_call(
            remote.rank_tools, "login", ["authentication-tool", "network-tool"], limit=2
        )
        assert ranked[0][0] == "authentication-tool"
        vectors = await index_call(remote.embed, ["authentication", "network TLS"])
        assert len(vectors) == 2 and len(vectors[0]) == 4
        assert remote.reranker is None


async def test_relevance_activation_and_scoring_stay_on_the_host(boundary):
    calls = []

    class Reranker:
        ready = False

        @property
        def status(self):
            return RerankerStatus(state="ready" if self.ready else "required")

        def ensure_started(self):
            self.ready = True

        def score(self, pairs):
            calls.append((threading.get_ident(), pairs))
            return [0.75 for _ in pairs]

    boundary.index.reranker = Reranker()
    remote = boundary.remotes[0]
    with execution_context(boundary.leases[0]):
        await remote.refresh()
        assert not remote.reranker.ready
        remote.reranker.ensure_started()
        async with asyncio.timeout(3):
            while not remote.reranker.ready:
                await asyncio.sleep(0.01)
        pairs = [("login", "Authentication evidence.")]
        assert await index_call(remote.reranker.score, pairs) == [0.75]
        assert calls[0][0] != threading.get_ident() and calls[0][1] == pairs
        with pytest.raises(KnowledgeIndexError):
            await index_call(remote.reranker.score, pairs * 129)
        assert len(calls) == 1


async def test_unknown_operations_and_tampered_bulk_content_do_not_index(boundary):
    remote = boundary.remotes[0]
    with execution_context(boundary.leases[0]):
        with pytest.raises(KnowledgeIndexError):
            await remote._rpc("arbitrary-method", {})
        with pytest.raises(KnowledgeIndexError):
            await remote._rpc(
                "bulk",
                {"kind": "engagement", "reference": "a" * 64, "nonce": str(uuid4())},
            )
    assert boundary.index._collection.count() == 0
