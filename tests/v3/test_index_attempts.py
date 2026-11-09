"""An old indexing result cannot overwrite a newer source attempt."""

import asyncio
import threading

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import KnowledgeSource, LibraryItem
from nebula.v3.knowledge import (
    ingest_document,
    ingest_library_item,
    reindex_document,
    reindex_library_item,
)
from nebula.v3.knowledge_index import KnowledgeIndexError
from nebula.v3.storage import ConflictError
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_embedding_warmup_carries_the_exact_project_context(cluster):
    from types import SimpleNamespace
    from nebula.v3.chat import ChatService
    from nebula.v3.execution_scope import current_execution_lease, execution_context

    coordinators, stores, _ = cluster
    lease = await coordinators[0].claim_project("project", "old")
    entered = threading.Event()
    observed = []

    class Index:
        status = SimpleNamespace(state="required")

        def prepare_model(self):
            observed.append(current_execution_lease())
            entered.set()

        def index_tools(self, *args):
            pass

        def rank_tools(self, *args):
            pass

    index = Index()
    service = ChatService(stores[0], knowledge_index=index)
    try:
        with execution_context(lease):
            assert service._tool_index() is index
        assert await asyncio.to_thread(entered.wait, 3)
        assert observed == [lease]
        await asyncio.to_thread(service._embedding_warmup.join, 3)
    finally:
        await service.shutdown()


class GateIndex:
    descriptor = {
        "index_backend": "inert",
        "index_version": "1",
        "index_collection": "synthetic",
    }
    library_descriptor = descriptor

    def __init__(self, *, fail=False):
        self.fail = fail
        self.entered, self.proceed = threading.Event(), threading.Event()
        self.source = None

    def upsert_source(self, source, chunks):
        self.source = source
        self.entered.set()
        if not self.proceed.wait(10):
            raise RuntimeError("Synthetic indexing gate was not released")
        if self.fail:
            raise KnowledgeIndexError("Synthetic index failure")

    upsert_library_item = upsert_source


@pytest.mark.parametrize("kind", ["document", "library"])
@pytest.mark.parametrize("initial", ["ingest", "reindex"])
@pytest.mark.parametrize("failed", [True, False])
@pytest.mark.parametrize("newer_state", ["indexing", "ready"])
async def test_older_index_result_cannot_change_a_newer_attempt(
    cluster, tmp_path, kind, initial, failed, newer_state
):
    _, stores, _ = cluster
    model = KnowledgeSource if kind == "document" else LibraryItem
    artifacts = ArtifactStore(tmp_path / "artifacts")
    first, second = GateIndex(fail=failed), GateIndex()
    source_task, newer_task = None, None

    async def ingest(store, index):
        kwargs = dict(
            store=store,
            artifact_store=artifacts,
            filename="synthetic.txt",
            data=b"Authentication evidence is stored in the synthetic notebook.",
            knowledge_index=index,
        )
        return (
            await ingest_document(engagement_id="project", **kwargs)
            if kind == "document"
            else await ingest_library_item(**kwargs)
        )

    async def reindex(store, identity, index):
        kwargs = dict(store=store, artifact_store=artifacts, knowledge_index=index)
        return (
            await reindex_document(source_id=identity, **kwargs)
            if kind == "document"
            else await reindex_library_item(item_id=identity, **kwargs)
        )

    try:
        if initial == "ingest":
            source_task = asyncio.create_task(ingest(stores[0], first))
        else:
            seed = await ingest(stores[0], None)
            source_task = asyncio.create_task(reindex(stores[0], seed.id, first))
        assert await asyncio.to_thread(first.entered.wait, 3)
        submitted = first.source
        newer_task = asyncio.create_task(reindex(stores[1], submitted.id, second))
        assert await asyncio.to_thread(second.entered.wait, 3)
        if newer_state == "ready":
            second.proceed.set()
            await newer_task
        newer = await stores[1].get(model, submitted.id)
        assert newer.status == newer_state
        assert (
            newer.metadata["runtime_index_request"]
            != submitted.metadata["runtime_index_request"]
        )
        first.proceed.set()
        with pytest.raises(ConflictError, match="newer indexing request"):
            await source_task
        observed = await stores[1].get(model, submitted.id)
        assert observed.status == newer.status and observed.revision == newer.revision
        assert observed.metadata == newer.metadata
        second.proceed.set()
        assert (await newer_task).status == "ready"
    finally:
        first.proceed.set()
        second.proceed.set()
        await asyncio.gather(
            *(task for task in (source_task, newer_task) if task is not None),
            return_exceptions=True,
        )
