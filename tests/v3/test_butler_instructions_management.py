"""User-owned Butler instructions are editable through authenticated Core state."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.butler_instructions import read_butler_instructions
from nebula.v3.storage import NebulaStore


def test_authenticated_butler_file_edit_create_reload_and_stale_conflict(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="fixture-token", execution_data_root=tmp_path)
    path = tmp_path / ".butler" / "BUTLER.md"
    headers = {"Authorization": "Bearer fixture-token"}
    with TestClient(app) as client:
        assert client.get("/api/v1/butler/instructions").status_code == 401
        missing = client.get("/api/v1/butler/instructions", headers=headers).json()
        assert missing["status"] == "missing" and missing["sha256"] is None
        assert not path.exists()
        assert (
            client.put(
                "/api/v1/butler/instructions",
                json={
                    "content": "Wake and inspect Act projects.\n",
                    "expected_sha256": None,
                },
            ).status_code
            == 401
        )
        created = client.put(
            "/api/v1/butler/instructions",
            json={
                "content": "Wake and inspect Act projects.\n",
                "expected_sha256": None,
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        first = created.json()
        assert first["status"] == "ready" and first["path"] == str(path)
        assert first["content"] == path.read_text(encoding="utf-8")
        assert read_butler_instructions(tmp_path).sha256 == first["sha256"]
        assert path.stat().st_mode & 0o777 == 0o600
        assert (
            client.get("/api/v1/butler/instructions", headers=headers).json() == first
        )
        updated = client.put(
            "/api/v1/butler/instructions",
            json={
                "content": "Choose a paused project when work is due.\n",
                "expected_sha256": first["sha256"],
            },
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        second = updated.json()
        assert second["sha256"] != first["sha256"]
        stale = client.put(
            "/api/v1/butler/instructions",
            json={"content": "Stale edit", "expected_sha256": first["sha256"]},
            headers=headers,
        )
        assert stale.status_code == 409
        assert path.read_text(encoding="utf-8") == second["content"]


def test_butler_file_editor_repairs_invalid_content_and_rejects_unsafe_paths(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    app = create_app(store, auth_token="fixture-token", execution_data_root=tmp_path)
    headers = {"Authorization": "Bearer fixture-token"}
    folder = tmp_path / ".butler"
    folder.mkdir()
    path = folder / "BUTLER.md"
    path.write_bytes(b"")
    with TestClient(app) as client:
        invalid = client.get("/api/v1/butler/instructions", headers=headers).json()
        assert invalid["status"] == "invalid" and invalid["sha256"]
        repaired = client.put(
            "/api/v1/butler/instructions",
            json={
                "content": "Only inspect due work.",
                "expected_sha256": invalid["sha256"],
            },
            headers=headers,
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["status"] == "ready"
        blank = client.put(
            "/api/v1/butler/instructions",
            json={"content": "   ", "expected_sha256": repaired.json()["sha256"]},
            headers=headers,
        )
        assert blank.status_code == 422
        path.unlink()
        path.symlink_to(tmp_path / "outside.md")
        unsafe = client.get("/api/v1/butler/instructions", headers=headers).json()
        assert unsafe["status"] == "invalid" and unsafe["sha256"] is None
        refused = client.put(
            "/api/v1/butler/instructions",
            json={"content": "Unsafe overwrite", "expected_sha256": None},
            headers=headers,
        )
        assert refused.status_code == 422
        assert path.is_symlink()
