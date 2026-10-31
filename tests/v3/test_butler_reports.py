"""Butler report actions keep Core references and operator device boundaries."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import (
    Engagement,
    Finding,
    FindingStatus,
    Observation,
    Report,
    ReportRender,
    ReportStatus,
)
from nebula.v3.relations import ResourceRelationService
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError


def test_butler_reports_page_sources_and_prepare_exact_project_handoffs(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    finding = store.create(
        Finding(
            engagement_id=selected.id,
            title="Verified service",
            status=FindingStatus.VALIDATED,
        )
    )
    note = store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="manual",
            title="Operator note",
            body="Private note body",
        )
    )
    foreign_finding = store.create(
        Finding(engagement_id=foreign.id, title="Foreign finding")
    )
    foreign_report = store.create(
        Report(engagement_id=foreign.id, title="Foreign report")
    )
    reports = [
        store.create(
            Report(
                engagement_id=selected.id,
                title=f"Report {index}",
                executive_summary="A" * 9000 if index == 0 else "",
            )
        )
        for index in range(52)
    ]
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.reports.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        matches = await capabilities.search({"query": "report PDF signoff draft"})
        assert any(
            row["operation"] == "nebula.reports.export_pdf"
            for row in matches["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.reports.update"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        first = await invoke("list")
        assert len(first["reports"]) == 50
        assert first["next_offset"] == 50
        assert len((await invoke("list", {"offset": 50}))["reports"]) == 2
        assert foreign_report.id not in str(first)
        source = await invoke("sources", {"kind": "finding"})
        assert source["items"] == [
            {
                "id": finding.id,
                "title": finding.title,
                "revision": finding.revision,
                "status": "validated",
            }
        ]
        assert "Private note body" not in str(await invoke("sources", {"kind": "note"}))
        detail = await invoke("detail", {"report_id": reports[0].id})
        assert len(detail["executive_summary"]) == 8000
        assert detail["next_summary_offset"] == 8000
        assert (
            len(
                (
                    await invoke(
                        "detail", {"report_id": reports[0].id, "summary_offset": 8000}
                    )
                )["executive_summary"]
            )
            == 1000
        )
        with pytest.raises(ValueError, match="outside the selected project"):
            await invoke("detail", {"report_id": foreign_report.id})
        create = await invoke(
            "create",
            {"title": "  New report  ", "observation_ids": [note.id]},
            change=True,
        )
        action = create["client_action"]
        assert create["state"] == "pending_browser_apply"
        assert action["kind"] == "report_create"
        assert action["title"] == "New report"
        assert action["finding_ids"] == [finding.id]
        assert action["observation_ids"] == [note.id]
        assert action["project_id"] == selected.id
        assert action["report_id"]
        with pytest.raises(ValueError, match="outside the selected project"):
            await invoke(
                "create",
                {"title": "Bad", "finding_ids": [foreign_finding.id]},
                change=True,
            )
        before = store.get(Report, reports[0].id)
        edit = await invoke(
            "update",
            {
                "report_id": before.id,
                "expected_revision": before.revision,
                "executive_summary": "Reviewed",
            },
            change=True,
        )
        assert edit["client_action"]["executive_summary"] == "Reviewed"
        assert store.get(Report, before.id).revision == before.revision
        with pytest.raises(ValueError, match="changed"):
            await invoke(
                "update",
                {
                    "report_id": before.id,
                    "expected_revision": before.revision + 1,
                    "title": "Stale",
                },
                change=True,
            )
        with pytest.raises(
            CapabilityInputError, match="use nebula.capabilities.change"
        ):
            await invoke(
                "export_pdf",
                {"report_id": before.id, "expected_revision": before.revision},
            )
        pdf = await invoke(
            "export_pdf",
            {"report_id": before.id, "expected_revision": before.revision},
            change=True,
        )
        assert pdf["client_action"]["kind"] == "report_export_pdf"
        assert "A" * 10 not in str(pdf)
        bundle = await invoke(
            "export_bundle",
            {"expected_project_revision": selected.revision},
            change=True,
        )
        assert bundle["client_action"] == {
            "kind": "report_export_bundle",
            "project_id": selected.id,
            "expected_project_revision": selected.revision,
        }

    asyncio.run(scenario())


def test_butler_report_note_transform_tracks_authoritative_relation_changes(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    note = store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="manual",
            title="TLS note",
            body="Port 443 was observed.",
        )
    )
    report = store.create(Report(engagement_id=selected.id, title="Assessment"))
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
        )
    )
    auth = {"Authorization": "Bearer test-token"}
    arguments = {
        "report_id": report.id,
        "expected_revision": report.revision,
        "observation_id": note.id,
        "expected_source_revision": note.revision,
        "instruction": "Rewrite this note as a report section.",
    }

    def invoke(operation: str, params: dict):
        return asyncio.run(
            capabilities.invoke(
                {"operation": f"nebula.reports.{operation}", "arguments": params},
                change=operation == "transform_note",
            )
        )

    relation = client.post(
        f"/api/v1/projects/{selected.id}/relations",
        headers=auth,
        json={
            "source": {"project_id": selected.id, "kind": "report", "id": report.id},
            "predicate": "includes",
            "target": {"project_id": selected.id, "kind": "note", "id": note.id},
        },
    )
    assert relation.status_code == 201, relation.text
    assert client.get(f"/api/v1/reports/{report.id}", headers=auth).json()[
        "observation_ids"
    ] == [note.id]
    assert invoke("detail", {"report_id": report.id})["observation_ids"] == [note.id]
    assert invoke("list", {})["reports"][0]["note_count"] == 1
    assert (
        invoke("transform_note", arguments)["client_action"]["observation_id"]
        == note.id
    )

    removed = client.delete(
        f"/api/v1/projects/{selected.id}/relations/{relation.json()['id']}",
        headers=auth,
        params={"expected_revision": relation.json()["revision"]},
    )
    assert removed.status_code == 204, removed.text
    current = client.get(f"/api/v1/reports/{report.id}", headers=auth).json()
    assert current["revision"] == report.revision
    assert current["observation_ids"] == []
    assert invoke("detail", {"report_id": report.id})["observation_ids"] == []
    assert invoke("list", {})["reports"][0]["note_count"] == 0
    with pytest.raises(ValueError, match="select the note"):
        invoke("transform_note", arguments)


def test_butler_report_note_transform_requires_linked_current_source_and_review(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    note = store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="manual",
            title="TLS note",
            body="port 443 observed; certificate review pending. " * 250,
        )
    )
    unlinked = store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="manual",
            title="Unlinked",
            body="Unlinked source",
        )
    )
    foreign_note = store.create(
        Observation(
            engagement_id=foreign.id,
            observation_type="manual",
            title="Foreign",
            body="Foreign private body",
        )
    )
    report = ResourceRelationService(store).create_legacy_entity(
        Report(engagement_id=selected.id, title="Assessment", observation_ids=[note.id])
    )
    assert isinstance(report, Report)
    final = store.create(
        Report(
            engagement_id=selected.id,
            title="Signed",
            status=ReportStatus.FINAL,
            observation_ids=[note.id],
            signed_off_by="Operator",
            signed_off_at=datetime.now(timezone.utc),
        )
    )
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )

    async def invoke(name, arguments, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.reports.{name}", "arguments": arguments},
            change=change,
        )

    async def scenario():
        assert any(
            row["operation"] == "nebula.reports.transform_note"
            for row in (
                await capabilities.search({"query": "transform linked report note"})
            )["matches"]
        )
        first = await invoke("source_note", {"observation_id": note.id})
        assert first["revision"] == note.revision
        assert len(first["body"]) == 8000
        assert first["next_body_offset"] == 8000
        assert (
            await invoke(
                "source_note", {"observation_id": note.id, "body_offset": 8000}
            )
        )["body"]
        with pytest.raises(ValueError, match="outside the selected project"):
            await invoke("source_note", {"observation_id": foreign_note.id})
        arguments = {
            "report_id": report.id,
            "expected_revision": report.revision,
            "observation_id": note.id,
            "expected_source_revision": note.revision,
            "instruction": "Rewrite for a technical stakeholder.",
        }
        with pytest.raises(
            CapabilityInputError, match="use nebula.capabilities.change"
        ):
            await invoke("transform_note", arguments)
        pending = await invoke("transform_note", arguments, change=True)
        assert pending == {
            "state": "pending_browser_apply",
            "butler_operation": "nebula.reports.transform_note",
            "butler_project_id": selected.id,
            "client_action": {
                "kind": "report_transform_note",
                "project_id": selected.id,
                "report_id": report.id,
                "expected_revision": report.revision,
                "observation_id": note.id,
                "expected_source_revision": note.revision,
                "instruction": "Rewrite for a technical stakeholder.",
            },
        }
        assert note.body[:100] not in str(pending)
        assert store.get(Report, report.id).revision == report.revision
        with pytest.raises(ValueError, match="report changed"):
            await invoke(
                "transform_note",
                {**arguments, "expected_revision": report.revision + 1},
                change=True,
            )
        with pytest.raises(ValueError, match="source note changed"):
            await invoke(
                "transform_note",
                {**arguments, "expected_source_revision": note.revision + 1},
                change=True,
            )
        with pytest.raises(ValueError, match="select the note"):
            await invoke(
                "transform_note",
                {**arguments, "observation_id": unlinked.id},
                change=True,
            )
        with pytest.raises(ValueError, match="outside the selected project"):
            await invoke(
                "transform_note",
                {**arguments, "observation_id": foreign_note.id},
                change=True,
            )
        with pytest.raises(ValueError, match="final reports are immutable"):
            await invoke(
                "transform_note",
                {
                    **arguments,
                    "report_id": final.id,
                    "expected_revision": final.revision,
                },
                change=True,
            )
        with pytest.raises(ValueError, match="provide an instruction"):
            await invoke(
                "transform_note", {**arguments, "instruction": "  "}, change=True
            )

    asyncio.run(scenario())


def test_report_core_api_checks_references_revision_and_finalization(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    finding = store.create(Finding(engagement_id=selected.id, title="Selected finding"))
    note = store.create(
        Observation(
            engagement_id=selected.id,
            observation_type="manual",
            title="Selected note",
            body="Observed",
        )
    )
    foreign_finding = store.create(
        Finding(engagement_id=foreign.id, title="Foreign finding")
    )
    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
        )
    )
    auth = {"Authorization": "Bearer test-token"}
    refused = client.post(
        "/api/v1/reports",
        headers=auth,
        json={
            "engagement_id": selected.id,
            "title": "Invalid",
            "finding_ids": [foreign_finding.id],
        },
    )
    assert refused.status_code in {400, 422}, refused.text
    prepared = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )
    action = asyncio.run(
        prepared.invoke(
            {
                "operation": "nebula.reports.create",
                "arguments": {
                    "title": "Stable draft",
                    "finding_ids": [finding.id],
                    "observation_ids": [note.id],
                },
            },
            change=True,
        )
    )["client_action"]
    assert (
        client.post(
            "/api/v1/reports",
            headers=auth,
            json={
                "id": action["report_id"],
                "engagement_id": selected.id,
                "title": "Generic route stays guarded",
            },
        ).status_code
        == 422
    )
    assert client.post(
        "/api/v1/reports/butler-drafts",
        headers=auth,
        json={
            "report_id": action["report_id"],
            "engagement_id": selected.id,
            "title": "Foreign reference",
            "finding_ids": [foreign_finding.id],
        },
    ).status_code in {400, 422}
    assert (
        client.post(
            "/api/v1/reports/butler-drafts",
            headers=auth,
            json={
                "report_id": "not-a-uuid",
                "engagement_id": selected.id,
                "title": "Invalid ID",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/reports/butler-drafts",
            headers=auth,
            json={
                "report_id": action["report_id"],
                "engagement_id": selected.id,
                "title": "Cannot sign off",
                "status": "final",
            },
        ).status_code
        == 422
    )
    created = client.post(
        "/api/v1/reports/butler-drafts",
        headers=auth,
        json={
            "report_id": action["report_id"],
            "engagement_id": selected.id,
            "title": action["title"],
            "finding_ids": action["finding_ids"],
            "observation_ids": action["observation_ids"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["id"] == action["report_id"]
    current = client.get(f"/api/v1/reports/{action['report_id']}", headers=auth)
    assert current.status_code == 200, current.text
    assert current.json()["finding_ids"] == [finding.id]
    assert current.json()["observation_ids"] == [note.id]
    listed = client.get(
        "/api/v1/reports", headers=auth, params={"engagement_id": selected.id}
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["observation_ids"] == [note.id]
    edges = client.get(
        f"/api/v1/projects/{selected.id}/relations",
        headers=auth,
        params={"resource_kind": "report", "resource_id": action["report_id"]},
    )
    assert edges.status_code == 200, edges.text
    assert {
        (edge["predicate"], edge["target"]["kind"], edge["target"]["id"])
        for edge in edges.json()
    } == {
        ("includes", "finding", finding.id),
        ("includes", "note", note.id),
    }
    assert (
        client.post(
            "/api/v1/reports/butler-drafts",
            headers=auth,
            json={
                "report_id": action["report_id"],
                "engagement_id": selected.id,
                "title": "Stable draft",
            },
        ).status_code
        == 409
    )
    stale = client.patch(
        f"/api/v1/reports/{action['report_id']}",
        headers=auth,
        json={
            "expected_revision": created.json()["revision"] + 1,
            "changes": {"title": "Stale"},
        },
    )
    assert stale.status_code == 409
    saved = client.patch(
        f"/api/v1/reports/{action['report_id']}",
        headers=auth,
        json={
            "expected_revision": created.json()["revision"],
            "changes": {"status": "review", "executive_summary": "Current summary"},
        },
    )
    assert saved.status_code == 200, saved.text
    report = client.get(f"/api/v1/reports/{action['report_id']}", headers=auth).json()
    assert report["revision"] == saved.json()["revision"]
    assert report["status"] == "review"
    assert report["finding_ids"] == [finding.id]
    assert report["observation_ids"] == [note.id]
    signoff = asyncio.run(
        prepared.invoke(
            {
                "operation": "nebula.reports.signoff",
                "arguments": {
                    "report_id": report["id"],
                    "expected_revision": report["revision"],
                },
            },
            change=True,
        )
    )
    assert signoff["client_action"]["kind"] == "report_signoff"
    assert client.patch(
        f"/api/v1/reports/{action['report_id']}",
        headers=auth,
        json={
            "expected_revision": report["revision"],
            "changes": {"status": "final"},
        },
    ).status_code in {400, 422}


def test_butler_report_delete_requires_current_project_and_core_confirmation(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    finding = store.create(Finding(engagement_id=selected.id, title="Source finding"))
    draft = store.create(
        Report(
            engagement_id=selected.id,
            title="Disposable draft",
            finding_ids=[finding.id],
        )
    )
    rendered = store.create(Report(engagement_id=selected.id, title="Rendered draft"))
    store.create(
        ReportRender(
            engagement_id=selected.id,
            report_id=rendered.id,
            report_revision=rendered.revision,
            input_fingerprint="0" * 64,
            template_version="test",
            renderer_version="test",
        )
    )
    final = store.create(
        Report(
            engagement_id=selected.id,
            title="Signed",
            status=ReportStatus.FINAL,
            signed_off_by="test-operator",
            signed_off_at=datetime.now(timezone.utc),
        )
    )
    foreign_draft = store.create(Report(engagement_id=foreign.id, title="Foreign"))
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )

    async def scenario():
        matches = await capabilities.search({"query": "delete a report draft"})
        assert any(
            row["operation"] == "nebula.reports.delete" for row in matches["matches"]
        )
        with pytest.raises(ValueError, match="outside the selected project"):
            await capabilities.invoke(
                {
                    "operation": "nebula.reports.delete",
                    "arguments": {
                        "report_id": foreign_draft.id,
                        "expected_revision": foreign_draft.revision,
                    },
                },
                change=True,
            )
        with pytest.raises(ValueError, match="changed"):
            await capabilities.invoke(
                {
                    "operation": "nebula.reports.delete",
                    "arguments": {
                        "report_id": draft.id,
                        "expected_revision": draft.revision + 1,
                    },
                },
                change=True,
            )
        with pytest.raises(ValueError, match="signed final report"):
            await capabilities.invoke(
                {
                    "operation": "nebula.reports.delete",
                    "arguments": {
                        "report_id": final.id,
                        "expected_revision": final.revision,
                    },
                },
                change=True,
            )
        with pytest.raises(ValueError, match="saved PDF render"):
            await capabilities.invoke(
                {
                    "operation": "nebula.reports.delete",
                    "arguments": {
                        "report_id": rendered.id,
                        "expected_revision": rendered.revision,
                    },
                },
                change=True,
            )
        pending = await capabilities.invoke(
            {
                "operation": "nebula.reports.delete",
                "arguments": {
                    "report_id": draft.id,
                    "expected_revision": draft.revision,
                },
            },
            change=True,
        )
        assert pending["state"] == "pending_browser_apply"
        assert pending["client_action"] == {
            "kind": "report_delete",
            "project_id": selected.id,
            "report_id": draft.id,
            "expected_revision": draft.revision,
            "title": draft.title,
        }
        assert store.get(Report, draft.id).id == draft.id

    asyncio.run(scenario())

    client = TestClient(
        create_app(
            store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            auth_token="test-token",
        )
    )
    auth = {"Authorization": "Bearer test-token"}
    route = f"/api/v1/reports/{draft.id}"
    stale = client.delete(route, headers={**auth, "If-Match": str(draft.revision + 1)})
    assert stale.status_code == 409, stale.text
    assert client.get(route, headers=auth).status_code == 200
    removed = client.delete(route, headers={**auth, "If-Match": str(draft.revision)})
    assert removed.status_code == 204, removed.text
    assert client.get(route, headers=auth).status_code == 404
    assert client.get(f"/api/v1/findings/{finding.id}", headers=auth).status_code == 200
    assert (
        client.delete(
            f"/api/v1/reports/{final.id}",
            headers={**auth, "If-Match": str(final.revision)},
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/api/v1/reports/{rendered.id}",
            headers={**auth, "If-Match": str(rendered.revision)},
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/reports/{rendered.id}", headers=auth).status_code == 200
