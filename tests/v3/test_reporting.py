from __future__ import annotations

import asyncio
import time
import zipfile
from functools import wraps

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    Observation,
    Report,
    ReportRender,
    ReportRenderStatus,
)
from nebula.v3.reporting import ReportRenderError, ReportRenderService
from nebula.v3.storage import NebulaStore


def async_test(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


@async_test
async def test_pdf_is_valid_cached_and_warns_without_interpreting_content(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="PDF engagement"))
    observation = store.create(
        Observation(
            engagement_id=engagement.id,
            observation_type="operator_note",
            title="Γειά — Привет — unsupported 💥",
            body=(
                "<img src='file:///etc/passwd'> "
                "<a href='https://example.invalid/track'>do not fetch</a>"
            ),
        )
    )
    report = store.create(
        Report(
            engagement_id=engagement.id,
            title="Server-rendered report",
            executive_summary="Latin, Ελληνικά, Кириллица",
            observation_ids=[observation.id],
        )
    )
    service = ReportRenderService(store=store, artifact_store=artifacts)

    queued = await service.request_render(report.id, report_revision=report.revision)
    await service._tasks[queued.id]
    completed = store.get(ReportRender, queued.id)

    assert completed.status == ReportRenderStatus.COMPLETED
    assert any("U+1F4A5" in warning for warning in completed.warnings)
    artifact, path = service.pdf(completed.id)
    assert artifacts.verify(artifact)
    reader = PdfReader(path)
    assert len(reader.pages) >= 2
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Server-rendered report" in text
    assert "DRAFT" in text
    assert "file:///etc/passwd" in text
    assert "<img" in text

    cached = await service.request_render(report.id, report_revision=report.revision)
    assert cached.id == completed.id
    assert service.pdf(cached.id)[1].read_bytes() == path.read_bytes()


@async_test
async def test_pdf_requires_exact_saved_revision_and_valid_artifacts(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="Revision checks"))
    report = store.create(
        Report(
            engagement_id=engagement.id,
            title="Broken references",
            artifact_ids=["missing-artifact"],
        )
    )
    service = ReportRenderService(store=store, artifact_store=artifacts)

    with pytest.raises(ReportRenderError, match="save the report revision"):
        await service.request_render(report.id, report_revision=report.revision + 1)
    with pytest.raises(ReportRenderError, match="missing artifacts"):
        await service.request_render(report.id, report_revision=report.revision)


def test_report_and_bundle_api_are_authenticated_and_sensitive_export_is_explicit(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(name="API report"))
    report = store.create(Report(engagement_id=engagement.id, title="API PDF"))
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(
        create_app(store, artifact_store=artifacts, auth_token="test-token")
    ) as client:
        assert client.post(f"/api/v1/reports/{report.id}/renders").status_code == 401
        queued_response = client.post(
            f"/api/v1/reports/{report.id}/renders",
            headers=headers,
            json={"report_revision": report.revision},
        )
        assert queued_response.status_code == 202
        render_id = queued_response.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detail = client.get(f"/api/v1/report-renders/{render_id}", headers=headers)
            if detail.json()["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.02)
        assert detail.json()["status"] == "completed"
        pdf = client.get(f"/api/v1/report-renders/{render_id}/pdf", headers=headers)
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF-")

        refused = client.post(
            f"/api/v1/engagements/{engagement.id}/export-bundle", headers=headers
        )
        assert refused.status_code == 428
        bundle = client.post(
            f"/api/v1/engagements/{engagement.id}/export-bundle",
            headers={
                **headers,
                "X-Nebula-Sensitive-Data-Acknowledged": "true",
            },
        )
        assert bundle.status_code == 200
        assert bundle.headers["x-nebula-bundle-version"] == "2"
        bundle_path = tmp_path / "api.nebula.zip"
        bundle_path.write_bytes(bundle.content)
        with zipfile.ZipFile(bundle_path) as archive:
            assert "entities/report_renders.json" in archive.namelist()
            assert "operation_events.json" in archive.namelist()
