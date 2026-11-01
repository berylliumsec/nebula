"""Notes use the selected project, Core references and browser revision guards."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Asset, Engagement, Evidence, Observation, Report
from nebula.v3.relations import ResourceRelationService
from nebula.v3.storage import NebulaStore


def test_butler_notes_discovery_is_bounded_project_owned_and_revision_checked(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    asset = store.create(Asset(engagement_id=selected.id, name="Inventory host"))
    evidence = store.create(
        Evidence(
            engagement_id=selected.id, title="Retained proof", evidence_type="manual"
        )
    )
    foreign = store.create(
        Observation(engagement_id=other.id, observation_type="note", title="Foreign")
    )
    foreign_asset = store.create(Asset(engagement_id=other.id, name="Foreign asset"))
    hidden = store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="execution_result",
            title="Generated execution",
        )
    )
    notes = [
        store.create(
            Observation(
                engagement_id=selected.id,
                observation_type="note",
                title=f"Note {n}",
                body="x" * 9000,
            )
        )
        for n in range(52)
    ]
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )

    async def invoke(operation, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notes.{operation}", "arguments": arguments or {}},
            change=change,
        )

    async def journey():
        assert "notes" in {
            row["id"] for row in (await capabilities.search({}))["categories"]
        }
        first = await invoke("list")
        assert len(first["notes"]) == 50 and first["next_offset"] == 50
        assert len((await invoke("list", {"offset": 50}))["notes"]) == 2
        assert hidden.id not in str(first) and foreign.id not in str(first)
        detail = await invoke("detail", {"note_id": notes[0].id})
        assert len(detail["body"]) == 8000 and detail["next_body_offset"] == 8000
        assert (
            len(
                (await invoke("detail", {"note_id": notes[0].id, "body_offset": 8000}))[
                    "body"
                ]
            )
            == 1000
        )
        assert (await invoke("link_targets", {"kind": "asset"}))["items"] == [
            {"id": asset.id, "title": asset.name, "revision": asset.revision}
        ]
        created = await invoke(
            "create",
            {
                "title": "New note",
                "body": "Unverified observation",
                "asset_ids": [asset.id],
                "evidence_ids": [evidence.id],
            },
            change=True,
        )
        assert created["state"] == "pending_browser_apply"
        assert created["client_action"]["kind"] == "note_create"
        assert created["client_action"]["project_id"] == selected.id
        assert (
            len(
                store.find_entities(
                    Observation, {"title": "New note"}, engagement_id=selected.id
                )
            )
            == 0
        )
        changed = await invoke(
            "update",
            {
                "note_id": notes[0].id,
                "expected_revision": notes[0].revision,
                "body": "Edited",
            },
            change=True,
        )
        assert changed["client_action"]["body"] == "Edited"
        assert store.get(Observation, notes[0].id).body == "x" * 9000
        for operation, arguments in (
            ("detail", {"note_id": foreign.id}),
            ("detail", {"note_id": hidden.id}),
            ("create", {"title": "Bad link", "asset_ids": [foreign_asset.id]}),
            (
                "update",
                {
                    "note_id": notes[0].id,
                    "expected_revision": notes[0].revision + 1,
                    "body": "Stale",
                },
            ),
        ):
            with pytest.raises(ValueError):
                await invoke(operation, arguments, change=operation != "detail")

    asyncio.run(journey())


def test_butler_note_draft_route_preserves_links_and_core_deletion_guards(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Selected"))
    other = store.create(Engagement(name="Other"))
    asset = store.create(Asset(engagement_id=project.id, name="Host"))
    foreign = store.create(Asset(engagement_id=other.id, name="Foreign"))
    client = TestClient(create_app(store, auth_token="test-token"))
    auth = {"Authorization": "Bearer test-token"}
    body = {
        "note_id": str(uuid4()),
        "engagement_id": project.id,
        "title": "Saved note",
        "body": "Retained body",
        "asset_ids": [asset.id],
    }
    assert (
        client.post("/api/v1/observations/butler-drafts", json=body).status_code == 401
    )
    created = client.post("/api/v1/observations/butler-drafts", headers=auth, json=body)
    assert created.status_code == 201, created.text
    note = created.json()
    assert note["id"] == body["note_id"] and note["asset_ids"] == [asset.id]
    duplicate = client.post(
        "/api/v1/observations/butler-drafts", headers=auth, json=body
    )
    assert duplicate.status_code == 409
    bad = client.post(
        "/api/v1/observations/butler-drafts",
        headers=auth,
        json={**body, "note_id": str(uuid4()), "asset_ids": [foreign.id]},
    )
    assert bad.status_code in (400, 422), bad.text
    path = f"/api/v1/observations/{note['id']}"
    edited = client.patch(
        path,
        headers=auth,
        json={
            "expected_revision": note["revision"],
            "changes": {"body": "Edited body"},
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["asset_ids"] == [asset.id]
    stale = client.patch(
        path,
        headers=auth,
        json={"expected_revision": note["revision"], "changes": {"body": "Stale body"}},
    )
    assert stale.status_code == 409
    relations = ResourceRelationService(store)
    report = relations.create_legacy_entity(
        Report(
            engagement_id=project.id,
            title="Retaining report",
            observation_ids=[note["id"]],
        )
    )
    blocked = client.delete(
        path, headers={**auth, "If-Match": str(edited.json()["revision"])}
    )
    assert blocked.status_code == 409, blocked.text
    unlinked = client.patch(
        f"/api/v1/reports/{report.id}",
        headers=auth,
        json={"expected_revision": report.revision, "changes": {"observation_ids": []}},
    )
    assert unlinked.status_code == 200, unlinked.text
    deleted = client.delete(
        path, headers={**auth, "If-Match": str(edited.json()["revision"])}
    )
    assert deleted.status_code == 204, deleted.text
    assert client.get(path, headers=auth).status_code == 404
    assert store.get(Asset, asset.id).name == "Host"
