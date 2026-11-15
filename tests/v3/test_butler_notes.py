"""Notes use the selected project, Core references, Core-saved edits and browser-confirmed deletion."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from uuid import uuid4

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_notes import ButlerNotes
from nebula.v3.domain import Asset, Engagement, Evidence, Observation, Report
from nebula.v3.relations import ResourceRelationService
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError


async def test_butler_notes_retains_accepted_execution_note_identity_and_provenance(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    note = await store.create(
        Observation(
            engagement_id=project.id,
            observation_type="ai_execution_note",
            title="Reviewed synthetic explanation",
            body="Operator-reviewed content",
            source="operator-accepted-ai-draft",
            metadata={
                "generated_draft_id": "synthetic-draft",
                "accepted_by": "operator",
                "provenance": "ai-generated/operator-edited/operator-accepted",
            },
        )
    )
    foreign = await store.create(
        note.model_copy(update={"id": str(uuid4()), "engagement_id": other.id})
    )
    hidden = await store.create(
        note.model_copy(
            update={"id": str(uuid4()), "observation_type": "execution_result"}
        )
    )
    capabilities = await ButlerCapabilities.create(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )

    async def invoke(operation, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.notes.{operation}", "arguments": arguments or {}},
            change=change,
        )

    async def journey():
        rows = (await invoke("list"))["notes"]
        assert [row["note_id"] for row in rows] == [note.id]
        assert rows[0]["note_type"] == "ai_execution_note"
        detail = await invoke("detail", {"note_id": note.id})
        assert detail["body"] == note.body and detail["revision"] == note.revision
        update = await invoke(
            "update",
            {
                "note_id": note.id,
                "expected_revision": note.revision,
                "body": "Reviewed edit",
            },
            change=True,
        )
        assert update["state"] == "core_records_saved" and update["read_back"] is True
        assert update["result"]["revision"] > note.revision
        assert update["client_action"] == {
            "kind": "note_saved",
            "project_id": project.id,
            "note_id": note.id,
            "title": note.title,
            "revision": update["result"]["revision"],
            "operation": "updated",
        }
        for invalid in (foreign, hidden):
            with pytest.raises(ValueError):
                await invoke("detail", {"note_id": invalid.id})
        with pytest.raises(ValueError):
            await invoke(
                "update",
                {
                    "note_id": note.id,
                    "expected_revision": note.revision,
                    "body": "Stale",
                },
                change=True,
            )
        retained = await store.get(Observation, note.id)
        assert retained.observation_type == note.observation_type
        assert retained.source == note.source and retained.metadata == note.metadata
        assert retained.body == "Reviewed edit"

    (await journey())


async def test_butler_notes_discovery_is_bounded_project_owned_and_revision_checked(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    selected = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    asset = await store.create(Asset(engagement_id=selected.id, name="Inventory host"))
    evidence = await store.create(
        Evidence(
            engagement_id=selected.id, title="Retained proof", evidence_type="manual"
        )
    )
    foreign = await store.create(
        Observation(engagement_id=other.id, observation_type="note", title="Foreign")
    )
    foreign_asset = await store.create(
        Asset(engagement_id=other.id, name="Foreign asset")
    )
    hidden = await store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="execution_result",
            title="Generated execution",
        )
    )
    notes = [
        (
            await store.create(
                Observation(
                    engagement_id=selected.id,
                    observation_type="note",
                    title=f"Note {n}",
                    body="x" * 9000,
                )
            )
        )
        for n in range(52)
    ]
    capabilities = await ButlerCapabilities.create(
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
                "request_id": "discovery-create-1",
            },
            change=True,
        )
        assert created["state"] == "core_records_saved"
        assert created["client_action"]["kind"] == "note_saved"
        assert created["client_action"]["project_id"] == selected.id
        assert (
            len(
                (
                    await store.find_entities(
                        Observation, {"title": "New note"}, engagement_id=selected.id
                    )
                )
            )
            == 1
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
        assert changed["read_back"] is True
        assert (await store.get(Observation, notes[0].id)).body == "Edited"
        for operation, arguments in (
            ("detail", {"note_id": foreign.id}),
            ("detail", {"note_id": hidden.id}),
            (
                "create",
                {
                    "title": "Bad link",
                    "asset_ids": [foreign_asset.id],
                    "request_id": "discovery-bad-link",
                },
            ),
            (
                "update",
                {
                    "note_id": notes[0].id,
                    "expected_revision": notes[0].revision,
                    "body": "Stale",
                },
            ),
        ):
            with pytest.raises(ValueError):
                await invoke(operation, arguments, change=operation != "detail")

    (await journey())


async def test_butler_note_draft_route_preserves_links_and_core_deletion_guards(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    asset = await store.create(Asset(engagement_id=project.id, name="Host"))
    foreign = await store.create(Asset(engagement_id=other.id, name="Foreign"))
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
        await client.post("/api/v1/observations/butler-drafts", json=body)
    ).status_code == 401
    created = await client.post(
        "/api/v1/observations/butler-drafts", headers=auth, json=body
    )
    assert created.status_code == 201, created.text
    note = created.json()
    assert note["id"] == body["note_id"] and note["asset_ids"] == [asset.id]
    duplicate = await client.post(
        "/api/v1/observations/butler-drafts", headers=auth, json=body
    )
    assert duplicate.status_code == 409
    bad = await client.post(
        "/api/v1/observations/butler-drafts",
        headers=auth,
        json={**body, "note_id": str(uuid4()), "asset_ids": [foreign.id]},
    )
    assert bad.status_code in (400, 422), bad.text
    path = f"/api/v1/observations/{note['id']}"
    edited = await client.patch(
        path,
        headers=auth,
        json={
            "expected_revision": note["revision"],
            "changes": {"body": "Edited body"},
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["asset_ids"] == [asset.id]
    stale = await client.patch(
        path,
        headers=auth,
        json={"expected_revision": note["revision"], "changes": {"body": "Stale body"}},
    )
    assert stale.status_code == 409
    relations = ResourceRelationService(store)
    report = await relations.create_legacy_entity(
        Report(
            engagement_id=project.id,
            title="Retaining report",
            observation_ids=[note["id"]],
        )
    )
    blocked = await client.delete(
        path, headers={**auth, "If-Match": str(edited.json()["revision"])}
    )
    assert blocked.status_code == 409, blocked.text
    unlinked = await client.patch(
        f"/api/v1/reports/{report.id}",
        headers=auth,
        json={"expected_revision": report.revision, "changes": {"observation_ids": []}},
    )
    assert unlinked.status_code == 200, unlinked.text
    deleted = await client.delete(
        path, headers={**auth, "If-Match": str(edited.json()["revision"])}
    )
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(path, headers=auth)).status_code == 404
    assert (await store.get(Asset, asset.id)).name == "Host"


async def test_butler_notes_create_and_update_save_in_core_without_a_browser(
    tmp_path,
):
    """A terminal or Workbench chat has no browser to apply a note action.

    Core itself must write the note, read it back, and make a retry with the same
    request key replay the saved note instead of creating a second one.
    """
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    asset = await store.create(Asset(engagement_id=project.id, name="Host"))
    evidence = await store.create(
        Evidence(engagement_id=project.id, title="Proof", evidence_type="manual")
    )
    foreign = await store.create(
        Evidence(engagement_id=other.id, title="Foreign", evidence_type="manual")
    )
    capabilities = await ButlerCapabilities.create(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )

    async def invoke(operation, arguments):
        return await capabilities.invoke(
            {"operation": f"nebula.notes.{operation}", "arguments": arguments},
            change=True,
        )

    async def project_notes():
        return await store.find_entities(
            Observation, {"observation_type": ["note"]}, engagement_id=project.id
        )

    request = {
        "title": "  Terminal note  ",
        "body": "Observed from the terminal.",
        "asset_ids": [asset.id],
        "evidence_ids": [evidence.id],
        "request_id": "terminal-note-0001",
    }
    created = await invoke("create", request)
    assert created["state"] == "core_records_saved"
    assert created["read_back"] is True and created["replayed"] is False
    saved = await store.get(Observation, created["result"]["note_id"])
    assert saved.engagement_id == project.id and saved.observation_type == "note"
    assert (saved.title, saved.body) == ("Terminal note", request["body"])
    assert (saved.asset_ids, saved.evidence_ids) == ([asset.id], [evidence.id])
    assert created["result"]["revision"] == saved.revision
    assert created["client_action"] == {
        "kind": "note_saved",
        "project_id": project.id,
        "note_id": saved.id,
        "title": "Terminal note",
        "revision": saved.revision,
        "operation": "created",
    }
    assert "read its saved fields back" in created["message"]

    replayed = await invoke("create", request)
    assert replayed["replayed"] is True
    assert replayed["result"]["note_id"] == saved.id
    assert [note.id for note in await project_notes()] == [saved.id]
    with pytest.raises(ValueError, match="different note content"):
        await invoke("create", {**request, "body": "Changed body"})
    with pytest.raises(ValueError):
        await invoke(
            "create",
            {
                **request,
                "request_id": "terminal-note-0002",
                "evidence_ids": [foreign.id],
            },
        )
    with pytest.raises(CapabilityInputError, match="request_id"):
        await invoke("create", {"title": "No key"})
    assert [note.id for note in await project_notes()] == [saved.id]

    updated = await invoke(
        "update",
        {
            "note_id": saved.id,
            "expected_revision": saved.revision,
            "title": "Terminal note, revised",
        },
    )
    assert updated["state"] == "core_records_saved" and updated["read_back"] is True
    current = await store.get(Observation, saved.id)
    assert current.title == "Terminal note, revised" and current.body == saved.body
    assert current.asset_ids == [asset.id] and current.evidence_ids == [evidence.id]
    assert current.revision > saved.revision
    assert updated["result"]["revision"] == current.revision
    assert updated["client_action"]["operation"] == "updated"
    with pytest.raises(ValueError, match="note changed"):
        await invoke(
            "update",
            {"note_id": saved.id, "expected_revision": saved.revision, "body": "Lost"},
        )
    with pytest.raises(ValueError):
        await invoke(
            "update", {"note_id": saved.id, "expected_revision": current.revision}
        )
    assert (await store.get(Observation, saved.id)).revision == current.revision


async def test_butler_notes_cannot_change_an_archived_project(tmp_path):
    """An archived project is read-only to Butler: no note create, edit or delete."""
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    note = await store.create(
        Observation(engagement_id=project.id, observation_type="note", title="Keep")
    )
    capabilities = await ButlerCapabilities.create(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )
    await store.update(
        Engagement,
        project.id,
        {"status": "archived"},
        expected_revision=project.revision,
    )
    attempts = {
        "create": {"title": "No write", "request_id": "archived-0001"},
        "update": {
            "note_id": note.id,
            "expected_revision": note.revision,
            "body": "No write",
        },
        "delete": {"note_id": note.id, "expected_revision": note.revision},
    }
    for operation, arguments in attempts.items():
        with pytest.raises(ValueError, match="restore the selected project"):
            await capabilities.invoke(
                {"operation": f"nebula.notes.{operation}", "arguments": arguments},
                change=True,
            )
    notes = await store.find_entities(
        Observation, {"observation_type": ["note"]}, engagement_id=project.id
    )
    assert [(row.id, row.title, row.body, row.revision) for row in notes] == [
        (note.id, "Keep", note.body, note.revision)
    ]


async def test_butler_note_delete_says_it_waits_for_web_app_confirmation(tmp_path):
    """Deletion stays operator-confirmed and never reads as done in a terminal."""
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    note = await store.create(
        Observation(engagement_id=project.id, observation_type="note", title="Keep")
    )
    capabilities = await ButlerCapabilities.create(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )
    result = await capabilities.invoke(
        {
            "operation": "nebula.notes.delete",
            "arguments": {"note_id": note.id, "expected_revision": note.revision},
        },
        change=True,
    )
    assert result["state"] == "pending_browser_apply"
    assert result["deleted"] is False
    assert result["operator_confirmation_required"] is True
    assert result["message"].startswith("Not deleted.")
    assert "web app" in result["message"]
    assert result["client_action"] == {
        "kind": "note_delete",
        "project_id": project.id,
        "note_id": note.id,
        "expected_revision": note.revision,
        "name": "Keep",
    }
    assert (await store.get(Observation, note.id)).title == "Keep"
    with pytest.raises(ValueError, match="note changed"):
        await capabilities.invoke(
            {
                "operation": "nebula.notes.delete",
                "arguments": {"note_id": note.id, "expected_revision": 99},
            },
            change=True,
        )
    descriptions = {
        tool.spec.name: tool.spec.description
        for tool in ButlerNotes(store, project.id).actions()
    }
    assert "deletes nothing" in descriptions["nebula.notes.delete"]
    for name in ("nebula.notes.create", "nebula.notes.update"):
        assert "read_back true" in descriptions[name]
        assert "browser" not in descriptions[name]


pytestmark = pytest.mark.anyio
