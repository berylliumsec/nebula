"""Interactive Butler sees bounded shared Library metadata and exact handoffs."""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Artifact, Engagement, LibraryItem
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError


def test_butler_library_pages_metadata_and_refuses_stale_global_change(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    store.create(Engagement(name="Another project"))
    items = [
        store.create(
            LibraryItem(
                name=f"Shared note {index}",
                source_type="text",
                artifact_id=f"artifact-{index}",
                metadata={"chunks": ["private document bytes"], "filename": "note.txt"},
            )
        )
        for index in range(52)
    ]
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.library.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        found = await capabilities.search({"query": "shared library item"})
        assert any(
            row["operation"] == "nebula.library.reindex" for row in found["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.library.list"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (await capabilities.describe({"operation": "nebula.library.remove"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        assert (await capabilities.describe({"operation": "nebula.library.add"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        first = await invoke("list")
        assert len(first["items"]) == 50
        assert first["next_offset"] == 50
        assert len((await invoke("list", {"offset": 50}))["items"]) == 2
        assert "private document bytes" not in str(first)
        item = items[0]
        detail = await invoke("detail", {"item_id": item.id})
        assert detail["revision"] == item.revision
        assert detail["scope"] == "shared across every project"
        assert "metadata" not in detail and "artifact_id" not in detail
        add = await invoke("add", change=True)
        assert add["client_action"] == {
            "kind": "library_add",
            "project_id": selected.id,
        }
        assert "private document bytes" not in str(add)
        download = await invoke(
            "download",
            {"item_id": item.id, "expected_revision": item.revision},
            change=True,
        )
        assert download["client_action"] == {
            "kind": "library_download",
            "project_id": selected.id,
            "item_id": item.id,
            "expected_revision": item.revision,
            "name": item.name,
        }
        assert "artifact_id" not in str(download)
        prepared = await invoke(
            "remove",
            {"item_id": item.id, "expected_revision": item.revision},
            change=True,
        )
        assert prepared == {
            "state": "pending_browser_apply",
            "butler_operation": "nebula.library.remove",
            "butler_project_id": selected.id,
            "client_action": {
                "kind": "library_remove",
                "project_id": selected.id,
                "item_id": item.id,
                "expected_revision": item.revision,
                "name": item.name,
            },
        }
        assert store.get(LibraryItem, item.id).revision == item.revision
        with pytest.raises(
            CapabilityInputError, match="use nebula.capabilities.change"
        ):
            await invoke(
                "reindex", {"item_id": item.id, "expected_revision": item.revision}
            )
        store.update(
            LibraryItem,
            item.id,
            {"name": "Changed elsewhere"},
            expected_revision=item.revision,
        )
        with pytest.raises(ValueError, match="changed"):
            await invoke(
                "reindex",
                {"item_id": item.id, "expected_revision": item.revision},
                change=True,
            )
        with pytest.raises(ValueError, match="changed"):
            await invoke(
                "download",
                {"item_id": item.id, "expected_revision": item.revision},
                change=True,
            )

    asyncio.run(scenario())


def test_library_api_revision_guard_preserves_item_and_artifact(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    client = TestClient(
        create_app(store, artifact_store=artifacts, auth_token="test-token")
    )
    auth = {"Authorization": "Bearer test-token"}
    created = client.post(
        "/api/v1/library/items/ingest",
        headers=auth,
        json={
            "filename": "shared.txt",
            "content_base64": base64.b64encode(b"Safe shared note").decode(),
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()
    target = f"/api/v1/library/items/{item['id']}"
    stale = item["revision"] + 1
    reindex = client.post(f"{target}/reindex?expected_revision={stale}", headers=auth)
    assert reindex.status_code == 409
    assert store.get(LibraryItem, item["id"]).revision == item["revision"]
    removed = client.delete(f"{target}?expected_revision={stale}", headers=auth)
    assert removed.status_code == 409
    assert store.get(LibraryItem, item["id"]).revision == item["revision"]
    updated = client.post(
        f"{target}/reindex?expected_revision={item['revision']}", headers=auth
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] > item["revision"]
    assert updated.json()["status"] == "ready"
    assert (
        client.delete(
            f"{target}?expected_revision={item['revision']}", headers=auth
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"{target}?expected_revision={updated.json()['revision']}", headers=auth
        ).status_code
        == 204
    )
    assert client.get(target, headers=auth).status_code == 404
    assert store.count(Artifact) == 1
