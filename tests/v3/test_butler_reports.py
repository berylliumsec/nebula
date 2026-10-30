"""Butler report actions keep Core references and operator device boundaries."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Engagement, Finding, FindingStatus, Observation, Report
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


def test_report_core_api_checks_references_revision_and_finalization(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    finding = store.create(Finding(engagement_id=selected.id, title="Selected finding"))
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
                "arguments": {"title": "Stable draft", "finding_ids": [finding.id]},
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
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["id"] == action["report_id"]
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
