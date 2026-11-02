from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Artifact, Engagement, KnowledgeSource
from nebula.v3.storage import NebulaStore


def test_butler_sources_page_metadata_and_prepare_only_owned_revisions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    for i in range(51):
        store.create(
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
    foreign = store.create(
        KnowledgeSource(
            engagement_id=other.id, name="foreign-private", source_type="text"
        )
    )
    capabilities = ButlerCapabilities(
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
            store.get(KnowledgeSource, source["source_id"]).revision
            == source["revision"]
        )
        assert (await call("add", {"input_kind": "url"}, change=True))[
            "client_action"
        ] == {"kind": "source_add", "project_id": project.id, "input_kind": "url"}
        current = store.get(Engagement, project.id)
        store.update(
            Engagement,
            project.id,
            {"status": "archived"},
            expected_revision=current.revision,
        )
        with pytest.raises(ValueError, match="restore"):
            await call("add", change=True)

    asyncio.run(journey())


def test_source_reindex_and_remove_fence_project_and_revision_and_retain_original(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    app = create_app(store, auth_token="test-token", artifact_store=artifacts)
    project = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    client = TestClient(app, headers={"Authorization": "Bearer test-token"})
    created = client.post(
        "/api/v1/knowledge/ingest",
        json={
            "engagement_id": project.id,
            "filename": "source.txt",
            "content_base64": base64.b64encode(b"Synthetic source content").decode(),
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()
    original = store.get(Artifact, source["artifact_id"])
    path = f"/api/v1/knowledge/{source['id']}"
    foreign = client.post(
        f"{path}/reindex?engagement_id={other.id}",
        headers={"If-Match": str(source["revision"])},
    )
    assert foreign.status_code == 422
    source = store.update(
        KnowledgeSource,
        source["id"],
        {"name": "renamed-source"},
        expected_revision=source["revision"],
    )
    stale = {"If-Match": str(source.revision - 1)}
    assert (
        client.post(
            f"{path}/reindex?engagement_id={project.id}", headers=stale
        ).status_code
        == 409
    )
    assert (
        client.delete(f"{path}?engagement_id={project.id}", headers=stale).status_code
        == 409
    )
    saved = client.post(
        f"{path}/reindex?engagement_id={project.id}",
        headers={"If-Match": str(source.revision)},
    )
    assert saved.status_code == 200, saved.text
    observed = saved.json()
    assert observed["status"] == "ready" and observed["revision"] > source.revision
    assert observed["artifact_id"] == original.id
    current = {"If-Match": str(observed["revision"])}
    assert (
        client.delete(f"{path}?engagement_id={other.id}", headers=current).status_code
        == 404
    )
    assert (
        client.delete(f"{path}?engagement_id={project.id}", headers=current).status_code
        == 204
    )
    assert client.get(path).status_code == 404
    assert store.get(Artifact, original.id) == original
    assert artifacts.verify(original)
