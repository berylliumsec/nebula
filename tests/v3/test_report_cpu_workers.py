"""The process prototype preserves Core's saved-report and artifact ownership."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import hashlib
import io
import os
import time

from tests.v3.async_test_support import AsyncTestClient as TestClient
from pypdf import PdfReader
import pytest

from nebula.v3 import reporting
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.cpu_workers import CpuWorkerPool
from nebula.v3.domain import (
    Artifact,
    Engagement,
    Observation,
    Report,
    ReportRender,
    ReportRenderStatus,
)
from nebula.v3.reporting import ReportRenderService
from nebula.v3.storage import NebulaStore
from tests.v3.cpu_worker_fixtures import blocked_pdf, hold
from tests.v3.test_cpu_workers import until


async def fixture(tmp_path, **pool_options):
    store = NebulaStore(tmp_path / "core.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    project = await store.create(
        Engagement(id="project", name="Synthetic worker report")
    )
    note = await store.create(
        Observation(
            id="note",
            engagement_id=project.id,
            observation_type="note",
            title="Saved Unicode Γειά 💥",
            body="Exact saved content <literal markup>.\n"
            + ("Synthetic paragraph. " * 200),
        )
    )
    report = await store.create(
        Report(
            id="report",
            engagement_id=project.id,
            title="CPU worker PDF",
            observation_ids=[note.id],
        )
    )
    pool = CpuWorkerPool(**pool_options)
    service = ReportRenderService(
        store=store, artifact_store=artifacts, cpu_workers=pool
    )
    return store, artifacts, report, pool, service


async def test_process_pdf_matches_threaded_bytes_warnings_and_cache(tmp_path):
    async def scenario():
        store, artifacts, report, pool, service = await fixture(tmp_path, workers=2)
        try:
            await service.startup()
            snapshot = (
                await service._prepare_render(report.id, report.revision)
            ).snapshot
            expected, warnings = service._build_pdf(snapshot)
            queued = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[queued.id]
            saved = await store.get(ReportRender, queued.id)
            assert saved.status == ReportRenderStatus.COMPLETED
            artifact, path = await service.pdf(saved.id)
            assert path.read_bytes() == expected and saved.warnings == warnings
            assert artifacts.verify(artifact)
            assert "Exact saved content" in "\n".join(
                page.extract_text() or "" for page in PdfReader(path).pages
            )
            cached = await service.request_render(
                report.id, report_revision=report.revision
            )
            assert cached.id == saved.id and pool.snapshot()["accepted"] == 1
            assert pool.snapshot()["coordinator_pid"] == os.getpid()
        finally:
            await service.shutdown()
            (await store.database.dispose())

    (await scenario())


async def test_pool_rejection_is_a_durable_failure_without_a_rendering_event(tmp_path):
    async def scenario():
        store, _artifacts, report, pool, service = await fixture(
            tmp_path, workers=1, max_input_bytes=1, max_total_input_bytes=1
        )
        try:
            await service.startup()
            queued = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[queued.id]
            saved = await store.get(ReportRender, queued.id)
            assert (
                saved.status == ReportRenderStatus.FAILED
                and saved.pdf_artifact_id is None
            )
            assert "snapshot size limit" in saved.error_detail
            events = await store.replay_operation_events(saved.id)
            assert [event.event_type for event in events] == [
                "report_render.queued",
                "report_render.failed",
            ]
            assert pool.snapshot()["accepted"] == 0 and pool.snapshot()["rejected"] == 1
        finally:
            await service.shutdown()
            (await store.database.dispose())

    (await scenario())


async def test_cancelled_queued_report_is_not_executed_and_retry_succeeds(tmp_path):
    async def scenario():
        store, _artifacts, report, pool, service = await fixture(
            tmp_path, workers=1, queued=1
        )
        release, started = tmp_path / "release", tmp_path / "started"
        held = None
        try:
            await service.startup()
            held = asyncio.create_task(pool.run(hold, str(release), str(started)))
            await until(started.exists)
            queued = await service.request_render(
                report.id, report_revision=report.revision
            )
            task = service._tasks[queued.id]
            await until(lambda: pool.snapshot()["queued"] == 1)
            assert (
                await store.get(ReportRender, queued.id)
            ).status == ReportRenderStatus.QUEUED
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                await store.get(ReportRender, queued.id)
            ).status == ReportRenderStatus.INTERRUPTED
            assert pool.snapshot()["cancelled_queued"] == 1
            release.touch()
            await held
            retry = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[retry.id]
            assert retry.id != queued.id
            assert (
                await store.get(ReportRender, retry.id)
            ).status == ReportRenderStatus.COMPLETED
            assert (await store.get(ReportRender, queued.id)).pdf_artifact_id is None
        finally:
            release.touch()
            await service.shutdown()
            (await store.database.dispose())

    (await scenario())


async def test_cancelled_running_report_cannot_commit_late_worker_output(
    tmp_path, monkeypatch
):
    async def scenario():
        store, _artifacts, report, pool, service = await fixture(
            tmp_path, workers=1, queued=0
        )
        release, started = tmp_path / "release", tmp_path / "started"
        report = await store.update(
            Report,
            report.id,
            {"executive_summary": f"{release}|{started}"},
            expected_revision=report.revision,
        )
        monkeypatch.setattr(reporting, "render_pdf_snapshot", blocked_pdf)
        try:
            await service.startup()
            queued = await service.request_render(
                report.id, report_revision=report.revision
            )
            task = service._tasks[queued.id]
            await until(started.exists)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                await store.get(ReportRender, queued.id)
            ).status == ReportRenderStatus.INTERRUPTED
            assert pool.snapshot()["active"] == 1
            release.touch()
            await until(lambda: pool.snapshot()["admitted"] == 0)
            assert (await store.get(ReportRender, queued.id)).pdf_artifact_id is None
            assert not [
                item
                for item in (await store.list_entities(Artifact))
                if item.media_type == "application/pdf"
            ]
            assert pool.snapshot()["discarded_results"] == 1
        finally:
            release.touch()
            await service.shutdown()
            (await store.database.dispose())

    (await scenario())


async def test_worker_loss_saves_one_failure_then_a_real_pdf_retry_succeeds(
    tmp_path, monkeypatch
):
    async def scenario():
        store, _artifacts, report, pool, service = await fixture(tmp_path, workers=1)
        release, started = tmp_path / "release", tmp_path / "started"
        report = await store.update(
            Report,
            report.id,
            {"executive_summary": f"{release}|{started}"},
            expected_revision=report.revision,
        )
        original = reporting.render_pdf_snapshot
        monkeypatch.setattr(reporting, "render_pdf_snapshot", blocked_pdf)
        try:
            await service.startup()
            queued = await service.request_render(
                report.id, report_revision=report.revision
            )
            task = service._tasks[queued.id]
            await until(started.exists)
            pid = int(started.read_text())
            assert pool._workers[0].process.pid == pid
            pool._workers[0].process.kill()
            await task
            saved = await store.get(ReportRender, queued.id)
            assert (
                saved.status == ReportRenderStatus.FAILED
                and saved.pdf_artifact_id is None
            )
            assert "worker exited" in saved.error_detail
            assert [
                event.event_type
                for event in (await store.replay_operation_events(saved.id))
            ].count("report_render.failed") == 1
            monkeypatch.setattr(reporting, "render_pdf_snapshot", original)
            retry = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[retry.id]
            assert (
                await store.get(ReportRender, retry.id)
            ).status == ReportRenderStatus.COMPLETED
            assert pool.snapshot()["workers"][0]["pid"] != pid
            assert (await service.pdf(retry.id))[1].read_bytes().startswith(b"%PDF-")
        finally:
            release.touch()
            await service.shutdown()
            (await store.database.dispose())

    (await scenario())


async def test_core_shutdown_interrupts_saved_work_and_removes_children(
    tmp_path, monkeypatch
):
    async def scenario():
        store, _artifacts, report, pool, service = await fixture(
            tmp_path, workers=1, shutdown_timeout=0.05
        )
        release, started = tmp_path / "release", tmp_path / "started"
        report = await store.update(
            Report,
            report.id,
            {"executive_summary": f"{release}|{started}"},
            expected_revision=report.revision,
        )
        monkeypatch.setattr(reporting, "render_pdf_snapshot", blocked_pdf)
        await service.startup()
        queued = await service.request_render(
            report.id, report_revision=report.revision
        )
        await until(started.exists)
        async with asyncio.timeout(3):
            await service.shutdown()
        assert (
            await store.get(ReportRender, queued.id)
        ).status == ReportRenderStatus.INTERRUPTED
        assert (await store.get(ReportRender, queued.id)).pdf_artifact_id is None
        assert not any(worker["alive"] for worker in pool.snapshot()["workers"])
        (await store.database.dispose())
        reopened = NebulaStore(tmp_path / "core.db")
        assert (
            await reopened.get(ReportRender, queued.id)
        ).status == ReportRenderStatus.INTERRUPTED
        (await reopened.database.dispose())

    (await scenario())


async def test_actual_core_report_routes_use_workers_with_normal_auth(tmp_path):
    store, artifacts, report, pool, service = await fixture(tmp_path, workers=2)
    app = create_app(
        store,
        artifact_store=artifacts,
        report_render_service=service,
        auth_token="synthetic-worker-token",
        execution_data_root=tmp_path / "execution",
    )
    headers = {"Authorization": "Bearer synthetic-worker-token"}
    async with TestClient(app) as client:
        assert (
            await client.post(f"/api/v1/reports/{report.id}/renders")
        ).status_code == 401
        response = await client.post(
            f"/api/v1/reports/{report.id}/renders",
            headers=headers,
            json={"report_revision": report.revision},
        )
        assert response.status_code == 202, response.text
        identity = response.json()["id"]
        deadline = time.monotonic() + 10
        while True:
            detail = await client.get(
                f"/api/v1/report-renders/{identity}", headers=headers
            )
            assert detail.status_code == 200
            if detail.json()["status"] == "completed":
                break
            assert time.monotonic() < deadline, detail.text
            time.sleep(0.01)
        pdf = await client.get(
            f"/api/v1/report-renders/{identity}/pdf", headers=headers
        )
        assert pdf.status_code == 200
        assert len(PdfReader(io.BytesIO(pdf.content)).pages) >= 2
        saved = await store.get(ReportRender, identity)
        assert (
            hashlib.sha256(pdf.content).hexdigest()
            == (await store.get(Artifact, saved.pdf_artifact_id)).sha256
        )
        cached = await client.post(
            f"/api/v1/reports/{report.id}/renders",
            headers=headers,
            json={"report_revision": report.revision},
        )
        assert cached.status_code == 202 and cached.json()["id"] == identity
    assert not any(worker["alive"] for worker in pool.snapshot()["workers"])
    (await store.database.dispose())


pytestmark = pytest.mark.anyio
