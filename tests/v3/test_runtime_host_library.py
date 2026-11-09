"""Global Library acceptance, publication and downloads using an inert index.

These regressions exercise the public supervisor entry point. They need no
provider, embedding model, container image or independently opened index.
"""

import asyncio
import base64
from datetime import timedelta
import hashlib
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.database import (
    EntityRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeIndexStateRow,
)
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import Artifact, LibraryItem, PairedDeviceSession, utc_now
from nebula.v3.knowledge import MAX_DOCUMENT_BYTES
from nebula.v3.knowledge_index import (
    IndexedKnowledgeChunk,
    KnowledgeIndexError,
    KnowledgeIndexStatus,
)
from nebula.v3.runtime_delivery import HttpIntent, MAX_REQUEST_BYTES, _json
from nebula.v3.runtime_host_services import HostSharedServices
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_vault import MAX_CONTENT_BYTES
from nebula.v3.sandbox import ContainerSandboxRunner
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _TOKEN, managed as managed

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer " + _TOKEN}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class InertLibraryIndex:
    descriptor = {"index_backend": "inert", "index_version": "fixture-v1"}
    library_descriptor = descriptor | {"collection": "inert-library"}
    status = KnowledgeIndexStatus(state="ready", model="inert", total_bytes=0)

    def __init__(self, database):
        self.database = database
        self.chunks = {}
        self.calls = []
        self.entered, self.release = Event(), Event()
        self.release.set()
        self.failure = None

    def upsert_library_item(self, item, chunks):
        assert self.database.task_session() is None
        self.calls.append(("upsert", item.id))
        self.entered.set()
        assert self.release.wait(timeout=10)
        if self.failure is not None:
            raise self.failure
        self.chunks[item.id] = [
            IndexedKnowledgeChunk(
                id=chunk["id"],
                text=chunk["text"],
                source_id=item.id,
                artifact_id=chunk["artifact_id"],
                page=chunk.get("page"),
                distance=0,
                rank=index,
                scope="library",
                index_digest=item.metadata["runtime_index_digest"],
            )
            for index, chunk in enumerate(chunks, 1)
        ]

    def delete_library_item(self, item_id):
        assert self.database.task_session() is None
        self.calls.append(("delete", item_id))
        self.chunks.pop(item_id, None)

    def query_library(self, queries, *, limit):
        return [chunk for chunks in self.chunks.values() for chunk in chunks][:limit]


@pytest.fixture
async def library(managed, monkeypatch):
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", ())
    index = InertLibraryIndex(managed.database)
    services = HostSharedServices(managed.host, knowledge_index=index)
    root = RuntimeSupervisor(
        managed.host, operator_token=_TOKEN, shared_services=services
    )
    await root.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=root.app),
            base_url="https://fixture",
            headers=AUTH,
        ) as client:
            yield SimpleNamespace(
                root=root,
                client=client,
                index=index,
                services=services,
                host=services.host_settings,
                managed=managed,
                store=managed.store,
            )
    finally:
        index.release.set()
        await root.close()


async def upload(
    library,
    *,
    data=b"Reusable authentication and TLS checklist.",
    key="upload",
    filename="checklist.txt",
):
    return await library.client.post(
        "/api/v1/library/items/ingest",
        json={"filename": filename, "content_base64": base64.b64encode(data).decode()},
        headers={"Idempotency-Key": key},
    )


async def wait_for_index(library):
    async with asyncio.timeout(5):
        while not library.index.entered.is_set():
            await asyncio.sleep(0.001)


async def test_global_library_lifecycle_replays_and_retains_original_artifact(library):
    path = "/api/v1/library/items/ingest"
    assert (await library.client.post(path, json={})).status_code == 428
    assert (
        await library.client.get(
            "/api/v1/knowledge/index-status",
            headers={"Authorization": "Bearer invalid"},
        )
    ).status_code == 401
    first = await upload(library)
    retry = await upload(library)
    assert first.status_code == retry.status_code == 201, first.text
    assert first.json() == retry.json()
    assert first.headers["x-nebula-command-id"] == retry.headers["x-nebula-command-id"]
    item = first.json()
    assert item["status"] == "ready" and "chunks" not in item["metadata"]
    assert library.index.calls == [("upsert", item["id"])]
    assert (await library.client.get("/api/v1/knowledge/index-status")).json()[
        "model"
    ] == "inert"
    assert (
        await library.client.get(f"/api/v1/library/items/{item['id']}")
    ).json() == item
    reindexed = await library.client.post(
        f"/api/v1/library/items/{item['id']}/reindex",
        params={"expected_revision": item["revision"]},
        headers={"Idempotency-Key": "reindex"},
    )
    assert reindexed.status_code == 200, reindexed.text
    assert (
        reindexed.json()["metadata"]["runtime_index_request"]
        != item["metadata"]["runtime_index_request"]
    )
    stale = await library.client.delete(
        f"/api/v1/library/items/{item['id']}",
        params={"expected_revision": item["revision"]},
        headers={"Idempotency-Key": "stale-delete"},
    )
    assert stale.status_code == 409
    removed = await library.client.delete(
        f"/api/v1/library/items/{item['id']}",
        params={"expected_revision": reindexed.json()["revision"]},
        headers={"Idempotency-Key": "delete"},
    )
    assert removed.status_code == 204
    assert (await library.client.get("/api/v1/library/items")).json() == []
    retained = await library.client.get(
        f"/api/v1/artifacts/{item['artifact_id']}/content"
    )
    assert (
        retained.status_code == 200
        and retained.content == b"Reusable authentication and TLS checklist."
    )
    assert retained.headers["cache-control"] == "private, no-store"
    assert retained.headers["x-content-type-options"] == "nosniff"
    async with library.managed.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        assert await session.get(EntityRow, GLOBAL_EXECUTION_PROJECT) is None
        state = await session.get(RuntimeIndexStateRow, item["id"])
        assert state.source_kind == "library" and state.state == "deleted"


async def test_host_publication_is_retrievable_in_both_real_project_scopes(library):
    response = await upload(library)
    assert response.status_code == 201, response.text
    item = response.json()
    coordination = library.managed.coordinator.coordination
    await coordination.register_runtime("inert-reader", "fixture-v1")
    for project in ("admitted-project", "synthetic-project"):
        lease = await coordination.claim_project(project, "inert-reader")
        matches = await library.services.knowledge.dispatch(
            lease, "query", {"kind": "library", "queries": ["TLS"], "limit": 8}
        )
        assert [match["source_id"] for match in matches] == [item["id"]]
    async with coordination._write() as session:
        state = await session.get(RuntimeIndexStateRow, item["id"])
        assert state.state == "ready" and state.source_kind == "library"
        assert state.digest == item["metadata"]["runtime_index_digest"]
        assert state.request_id == item["metadata"]["runtime_index_request"]
        state.request_id = str(uuid4())
    assert not await library.services.knowledge.dispatch(
        lease, "query", {"kind": "library", "queries": ["TLS"], "limit": 8}
    )


async def test_superseded_source_cannot_publish_old_ready_state(library):
    library.index.release.clear()
    pending = asyncio.create_task(upload(library))
    try:
        await wait_for_index(library)
        item = (await library.store.list_entities(LibraryItem))[0]
        metadata = item.metadata | {"runtime_index_request": str(uuid4())}
        await library.store.update(
            LibraryItem,
            item.id,
            {"metadata": metadata},
            expected_revision=item.revision,
        )
    finally:
        library.index.release.set()
    response = await pending
    assert response.status_code == 409
    saved = await library.store.get(LibraryItem, item.id)
    assert saved.metadata["runtime_index_request"] == metadata["runtime_index_request"]
    assert saved.status == "indexing"
    async with library.managed.database.session() as session:
        state = await session.get(RuntimeIndexStateRow, item.id)
        assert (
            state.state == "failed"
            and state.request_id != metadata["runtime_index_request"]
        )


async def test_index_failure_keeps_saved_item_and_reindex_can_rebuild(library):
    library.index.failure = KnowledgeIndexError("inert failure")
    response = await upload(library)
    assert response.status_code == 201 and response.json()["status"] == "error"
    item = response.json()
    async with library.managed.database.session() as session:
        assert (await session.get(RuntimeIndexStateRow, item["id"])).state == "failed"
    library.index.failure = None
    rebuilt = await library.client.post(
        f"/api/v1/library/items/{item['id']}/reindex",
        params={"expected_revision": item["revision"]},
        headers={"Idempotency-Key": "rebuild"},
    )
    assert rebuilt.status_code == 200 and rebuilt.json()["status"] == "ready"


async def test_unconfirmed_physical_index_write_is_not_published_or_replayed(
    library, monkeypatch
):
    original = library.index.upsert_library_item

    def uncertain(item, chunks):
        original(item, chunks)
        raise RuntimeError("inert failure after the physical write")

    monkeypatch.setattr(library.index, "upsert_library_item", uncertain)
    response = await upload(library, key="uncertain-index")
    assert response.status_code == 409 and response.json()["state"] == "uncertain"
    item = (await library.store.list_entities(LibraryItem))[0]
    assert item.status == "indexing" and item.id in library.index.chunks
    async with library.managed.database.session() as session:
        assert (await session.get(RuntimeIndexStateRow, item.id)).state == "failed"
    retry = await upload(library, key="uncertain-index")
    assert retry.status_code == 409 and len(library.index.calls) == 1


async def test_lost_confirmation_never_reexecutes_a_published_effect(
    library, monkeypatch
):
    vault = library.host.effect.delivery.vault
    original = vault.put

    def fail_confirmation(raw, *, purpose):
        if purpose.startswith("delivery:"):
            raise RuntimeIpcError("inert confirmation failure")
        return original(raw, purpose=purpose)

    monkeypatch.setattr(vault, "put", fail_confirmation)
    first = await upload(library, key="uncertain")
    assert first.status_code == 409 and first.json()["state"] == "uncertain"
    command = first.headers["x-nebula-command-id"]
    monkeypatch.setattr(vault, "put", original)
    retry = await upload(library, key="uncertain")
    assert retry.status_code == 409 and retry.headers["x-nebula-command-id"] == command
    assert len(library.index.calls) == 1
    item = (await library.store.list_entities(LibraryItem))[0]
    assert item.status == "ready"
    async with library.managed.database.session() as session:
        assert (await session.get(RuntimeIndexStateRow, item.id)).state == "ready"
        assert (await session.get(RuntimeCommandRow, command)).state == "uncertain"


async def test_disconnected_viewer_and_host_close_join_accepted_index_work(library):
    library.index.release.clear()
    viewer = asyncio.create_task(upload(library, key="joined"))
    close = None
    try:
        await wait_for_index(library)
        viewer.cancel()
        await asyncio.gather(viewer, return_exceptions=True)
        close = asyncio.create_task(library.root.close())
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        library.index.release.set()
    await close
    assert len(library.index.calls) == 1


def large_html(size):
    prefix, suffix = b"<p>Library fixture.</p><!--", b"-->"
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


async def test_concurrent_upload_is_refused_before_consuming_its_body(library):
    entered, release = asyncio.Event(), asyncio.Event()
    consumed = False

    async def first_body():
        entered.set()
        await release.wait()
        yield _json(
            {
                "filename": "bounded.txt",
                "content_base64": base64.b64encode(b"Bounded upload.").decode(),
            }
        )

    async def second_body():
        nonlocal consumed
        consumed = True
        yield b"this body must never be consumed"

    first = asyncio.create_task(
        library.client.post(
            "/api/v1/library/items/ingest",
            content=first_body(),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "first-upload",
            },
        )
    )
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        refused = await library.client.post(
            "/api/v1/library/items/ingest",
            content=second_body(),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "second-upload",
            },
        )
        assert refused.status_code == 429 and not consumed
    finally:
        release.set()
    accepted = await first
    assert accepted.status_code == 201 and len(library.index.calls) == 1


async def test_full_twenty_mib_upload_is_durable_and_streams_without_response_capture(
    library, monkeypatch
):
    data = large_html(MAX_DOCUMENT_BYTES)
    response = await upload(library, data=data, filename="large.html", key="large")
    assert response.status_code == 201, response.text
    item = response.json()
    command = response.headers["x-nebula-command-id"]
    page = await library.host.effect.delivery.page("operator", command, limit=128)
    assert sum(len(frame.content) for frame in page.frames) < 128 * 1024
    async with library.managed.database.session() as session:
        accepted = await session.get(RuntimeCommandRow, command)
        raw = library.host.effect.delivery.vault.get(
            accepted.payload["request_reference"], purpose="intent:operator"
        )
    assert len(raw) > MAX_CONTENT_BYTES
    assert HttpIntent.model_validate_json(raw).body()
    original = library.host.effect._response

    async def no_captured_download(intent, actor):
        assert "/artifacts/" not in intent.path
        return await original(intent, actor)

    monkeypatch.setattr(library.host.effect, "_response", no_captured_download)
    downloaded = await library.client.get(
        f"/api/v1/artifacts/{item['artifact_id']}/content"
    )
    assert downloaded.status_code == 200 and downloaded.content == data
    assert (
        downloaded.headers["x-nebula-artifact-sha256"]
        == hashlib.sha256(data).hexdigest()
    )
    assert int(downloaded.headers["x-nebula-artifact-bytes"]) == MAX_DOCUMENT_BYTES
    assert not library.host.library._downloads


async def test_document_and_ordinary_host_request_bounds_remain_enforced(library):
    too_large = await upload(
        library, data=large_html(MAX_DOCUMENT_BYTES + 1), filename="large.html"
    )
    assert too_large.status_code == 413
    generic = await library.client.post(
        "/api/v1/credentials",
        content=b"x" * (128 * 1024 + 1),
        headers={"Idempotency-Key": "generic-bound"},
    )
    assert generic.status_code == 413
    assert await library.store.count(LibraryItem) == 0 and not library.index.calls


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/chat/completions"),
        ("PUT", "/api/v1/library/items/ingest"),
        ("POST", "/api/v1/library/items/ingest/"),
        ("POST", "/api/v1/library/items/ingest/reindex"),
    ],
)
def test_larger_intent_allowance_is_exclusive_to_exact_library_ingest(method, path):
    raw = b"x" * (MAX_REQUEST_BYTES + 1)
    with pytest.raises(ValueError, match="bound"):
        HttpIntent.capture(method, path, b"", [], raw)
    forged = {
        "method": method,
        "path": path,
        "body_base64": base64.b64encode(raw).decode(),
    }
    with pytest.raises(ValueError, match="bound"):
        HttpIntent.model_validate_json(_json(forged))


async def test_library_download_checks_provenance_device_revocation_and_integrity(
    library,
):
    response = await upload(library)
    assert response.status_code == 201, response.text
    item = response.json()
    path = f"/api/v1/artifacts/{item['artifact_id']}/content"
    now = utc_now()
    device = await library.store.create(
        PairedDeviceSession(
            id="library-viewer",
            name="Synthetic Library viewer",
            token_sha256=hashlib.sha256(b"library-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"library-csrf").hexdigest(),
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=library.root.app),
        base_url="https://fixture",
        cookies={"nebula_device": "library-token", "nebula_csrf": "library-csrf"},
        headers={"Origin": "https://fixture", "X-Nebula-CSRF": "library-csrf"},
    ) as viewer:
        assert (await viewer.get(path)).status_code == 200
        await library.store.update(
            PairedDeviceSession, device.id, {"revoked_at": utc_now()}
        )
        assert (await viewer.get(path)).status_code == 401
    artifact = await library.store.get(Artifact, item["artifact_id"])
    forged = artifact.model_copy(
        update={
            "id": str(uuid4()),
            "source": "tool-output",
            "metadata": {"scope": "library"},
        }
    )
    await library.store.create(forged)
    assert await library.host.library.owns_artifact(forged.id)
    assert (
        await library.client.get(f"/api/v1/artifacts/{forged.id}/content")
    ).status_code == 404
    foreign = artifact.model_copy(
        update={"id": str(uuid4()), "engagement_id": "synthetic-project"}
    )
    await library.store.create(foreign)
    assert not await library.host.library.owns_artifact(foreign.id)
    blob = library.host.library.artifacts.path_for(artifact)
    blob.write_bytes(b"corrupted")
    failed = await library.client.get(path)
    assert failed.status_code == 409 and b"corrupted" not in failed.content


async def test_verified_download_keeps_open_blob_and_closes_on_host_detach(
    library, tmp_path
):
    response = await upload(library)
    assert response.status_code == 201, response.text
    artifact = await library.store.get(Artifact, response.json()["artifact_id"])
    token = library.host.actor.set("operator")
    try:
        download = await library.host.library.artifact_content(artifact.id)
    finally:
        library.host.actor.reset(token)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement bytes")
    replacement.replace(library.host.library.artifacts.path_for(artifact))
    assert download.source.read() == b"Reusable authentication and TLS checklist."
    await library.host.library.close()
    assert download.source.closed and not library.host.library._downloads
