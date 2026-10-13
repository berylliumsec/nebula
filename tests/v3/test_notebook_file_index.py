import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import Engagement
from nebula.v3.notebook_file_index import FileIndexUnavailable, list_indexed_notebooks
from nebula.v3.notebooks import NotebookService
from nebula.v3.storage import NebulaStore


def _inventory(tmp_path, root, *, complete=1, pending=0):
    path = tmp_path / "inventory.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE repositories (id INTEGER PRIMARY KEY, root TEXT UNIQUE,
                complete INTEGER, pending_files INTEGER, last_reconciled_at TEXT);
            CREATE TABLE files (repository_id INTEGER, path TEXT, size INTEGER, mtime_ns INTEGER);
        """)
        connection.execute(
            "INSERT INTO repositories VALUES (1, ?, ?, ?, ?)",
            (str(root), complete, pending, datetime.now(timezone.utc).isoformat()),
        )
        connection.execute("INSERT INTO files VALUES (1, 'a.ipynb', 12, 1000000000)")
    return path


def test_file_index_lists_exact_project_and_rejects_unready_index(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    path = _inventory(tmp_path, root)
    assert [item["path"] for item in list_indexed_notebooks(str(path), root)] == [
        "a.ipynb"
    ]
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(FileIndexUnavailable, match="does not contain"):
        list_indexed_notebooks(str(path), other)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE repositories SET pending_files=1")
    with pytest.raises(FileIndexUnavailable, match="reconciling"):
        list_indexed_notebooks(str(path), root)


def test_project_prefers_index_then_falls_back_and_removal_persists(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "store.db")
    root = tmp_path / "workspace"
    root.mkdir()
    project = store.create(Engagement(name="Index project", workspace_path=str(root)))
    service = NotebookService(
        store=store,
        workspace_resolver=lambda _: root,
        automation_runtime=None,
        data_root=tmp_path / "data",
    )
    index = _inventory(tmp_path, root)
    scans = []
    monkeypatch.setattr(
        service.documents,
        "list",
        lambda _: scans.append(True) or [{"path": "fallback.ipynb"}],
    )

    async def scenario():
        assert (await service.set_file_index(project.id, str(index)))[
            "state"
        ] == "connected"
        assert [item["path"] for item in await service.list_async(project.id)] == [
            "a.ipynb"
        ]
        assert not scans
        await service.create(project.id, "new.ipynb")
        assert [item["path"] for item in await service.list_async(project.id)] == [
            "a.ipynb",
            "new.ipynb",
        ]
        with sqlite3.connect(index) as connection:
            connection.execute("UPDATE repositories SET pending_files=1")
        assert [
            item["path"] for item in await service.list_async(project.id, refresh=True)
        ] == ["fallback.ipynb"]
        assert service.file_index_status(project.id)["state"] == "fallback"
        assert len(scans) == 1
        await service.set_file_index(project.id, None)
        assert service.file_index_status(project.id)["state"] == "none"
        assert [item["path"] for item in await service.list_async(project.id)] == [
            "fallback.ipynb"
        ]
        assert len(scans) == 2

    asyncio.run(scenario())


def test_host_index_browser_pages_sqlite_files(tmp_path):
    browser = tmp_path / "indexes"
    browser.mkdir()
    for name in ("a.db", "b.sqlite", "c.sqlite3", "ignored.txt"):
        (browser / name).touch()
    store = NebulaStore(tmp_path / "store.db")
    with TestClient(create_app(store, auth_token="test-token")) as client:
        first = client.get(
            "/api/v1/workspace-folders",
            params={
                "path": str(browser),
                "include_sqlite_files": True,
                "limit": 2,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert first.status_code == 200
        assert [item["name"] for item in first.json()["files"]] == ["a.db", "b.sqlite"]
        second = client.get(
            "/api/v1/workspace-folders",
            params={
                "path": str(browser),
                "include_sqlite_files": True,
                "limit": 2,
                "offset": 2,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert second.status_code == 200
        assert [item["name"] for item in second.json()["files"]] == ["c.sqlite3"]
