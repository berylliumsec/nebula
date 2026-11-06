from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import base64

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Artifact, Engagement, KnowledgeSource
from nebula.v3.storage import NebulaStore


async def test_butler_sources_page_metadata_and_prepare_only_owned_revisions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    for i in range(51):
        (
            await store.create(
                KnowledgeSource(
                    engagement_id=project.id,
                    name=f"source-{i}",
                    source_type="text",
                    artifact_id="artifact",
                    metadata={
                        "chunks": ["PRIVATE_BYTES"],
                        "source_url": "https://private.invalid/secret",
                    },
                )
            )
        )
    foreign = await store.create(
        KnowledgeSource(
            engagement_id=other.id, name="foreign-private", source_type="text"
        )
    )
    capabilities = await ButlerCapabilities.create(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )

    async def call(operation, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.sources.{operation}", "arguments": arguments or {}},
            change=change,
        )

    async def journey():
        assert (await capabilities.search({"query": "project sources reindex"}))[
            "matches"
        ]
        assert (await call("index_status"))["state"] == "disabled"
        first = await call("list")
        assert len(first["sources"]) == 50
        assert first["next_offset"] == 50
        last = await call("list", {"offset": 50})
        assert len(last["sources"]) == 1 and last["next_offset"] is None
        assert all(
            marker not in str(first) + str(last)
            for marker in ("PRIVATE_BYTES", "private.invalid", foreign.name)
        )
        source = first["sources"][0]
        for operation in ("detail", "remove", "reindex", "download"):
            with pytest.raises(ValueError, match="selected project"):
                await call(
                    operation,
                    {
                        "source_id": foreign.id,
                        **(
                            {"expected_revision": foreign.revision}
                            if operation != "detail"
                            else {}
                        ),
                    },
                    change=operation != "detail",
                )
        with pytest.raises(ValueError, match="current revision"):
            await call(
                "remove",
                {"source_id": source["source_id"], "expected_revision": 100},
                change=True,
            )
        prepared = await call(
            "reindex",
            {"source_id": source["source_id"], "expected_revision": source["revision"]},
            change=True,
        )
        assert prepared["state"] == "pending_browser_apply"
        assert prepared["client_action"]["kind"] == "source_reindex"
        assert (
            await store.get(KnowledgeSource, source["source_id"])
        ).revision == source["revision"]
        assert (await call("add", {"input_kind": "url"}, change=True))[
            "client_action"
        ] == {"kind": "source_add", "project_id": project.id, "input_kind": "url"}
        current = await store.get(Engagement, project.id)
        (
            await store.update(
                Engagement,
                project.id,
                {"status": "archived"},
                expected_revision=current.revision,
            )
        )
        with pytest.raises(ValueError, match="restore"):
            await call("add", change=True)

    (await journey())


async def test_source_reindex_and_remove_fence_project_and_revision_and_retain_original(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(store, auth_token="test-token", artifact_store=artifacts)
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})
    created = await client.post(
        "/api/v1/knowledge/ingest",
        json={
            "engagement_id": project.id,
            "filename": "source.txt",
            "content_base64": base64.b64encode(b"Synthetic source content").decode(),
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()
    original = await store.get(Artifact, source["artifact_id"])
    path = f"/api/v1/knowledge/{source['id']}"
    foreign = await client.post(
        f"{path}/reindex?engagement_id={other.id}",
        headers={"If-Match": str(source["revision"])},
    )
    assert foreign.status_code == 422
    source = await store.update(
        KnowledgeSource,
        source["id"],
        {"name": "renamed-source"},
        expected_revision=source["revision"],
    )
    stale = {"If-Match": str(source.revision - 1)}
    assert (
        await client.post(f"{path}/reindex?engagement_id={project.id}", headers=stale)
    ).status_code == 409
    assert (
        await client.delete(f"{path}?engagement_id={project.id}", headers=stale)
    ).status_code == 409
    saved = await client.post(
        f"{path}/reindex?engagement_id={project.id}",
        headers={"If-Match": str(source.revision)},
    )
    assert saved.status_code == 200, saved.text
    observed = saved.json()
    assert observed["status"] == "ready" and observed["revision"] > source.revision
    assert observed["artifact_id"] == original.id
    current = {"If-Match": str(observed["revision"])}
    assert (
        await client.delete(f"{path}?engagement_id={other.id}", headers=current)
    ).status_code == 404
    assert (
        await client.delete(f"{path}?engagement_id={project.id}", headers=current)
    ).status_code == 204
    assert (await client.get(path)).status_code == 404
    assert (await store.get(Artifact, original.id)) == original
    assert artifacts.verify(original)


pytestmark = pytest.mark.anyio
