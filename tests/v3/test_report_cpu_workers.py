"""The process prototype preserves Core's saved-report and artifact ownership.

Since 64e0379 ("Enforce managed compute memory budgets") PDF computation is held
before pool startup or dispatch until a memory-managed CPU runner exists
(docs/design/memory-budgets.md). Each journey below therefore ends at that hold:
the render is saved as a durable failure with no rendering event, the hold is
recorded as a blocked, unreserved memory job, and the injected affinity-only pool
never starts or receives work.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import os
import time

from tests.v3.async_test_support import AsyncTestClient as TestClient
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
from nebula.v3.reporting import ReportRenderError, ReportRenderService
from nebula.v3.storage import NebulaStore
from tests.v3.cpu_worker_fixtures import blocked_pdf, hold
from tests.v3.test_cpu_workers import until

MEMORY_HOLD = "PDF computation requires a memory-managed CPU runner"


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


async def held_render(store, service, render_id):
    saved = await store.get(ReportRender, render_id)
    assert saved.status == ReportRenderStatus.FAILED and saved.pdf_artifact_id is None
    assert MEMORY_HOLD in saved.error_detail
    assert [
        event.event_type for event in await store.replay_operation_events(saved.id)
    ] == ["report_render.queued", "report_render.failed"]
    with pytest.raises(ReportRenderError, match="not ready"):
        await service.pdf(saved.id)
    return saved


async def finished(service, render_id):
    # A route-started render saves its failure before appending its event;
    # join its task (if it is still running) so both are visible.
    task = service._tasks.get(render_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=10)


async def held_jobs(service):
    return sorted(
        (job.state, job.resource_kind, job.resource_id, job.reserved_bytes)
        for job in await service.memory_compute.service.list_jobs()
    )


async def pdf_artifacts(store):
    return [
        item
        for item in await store.list_entities(Artifact)
        if item.media_type == "application/pdf"
    ]


async def test_process_pdf_matches_threaded_bytes_warnings_and_cache(tmp_path):
    async def scenario():
        store, artifacts, report, pool, service = await fixture(tmp_path, workers=2)
        try:
            await service.startup()
            assert pool.snapshot()["workers"] == []
            queued = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[queued.id]
            saved = await held_render(store, service, queued.id)
            assert saved.warnings == [] and not await pdf_artifacts(store)
            # A held render is never served from cache; an explicit retry is
            # held again until a protected runner is configured.
            retry = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[retry.id]
            assert retry.id != saved.id
            await held_render(store, service, retry.id)
            assert await held_jobs(service) == sorted(
                ("blocked", "report_pdf", identity, 0)
                for identity in (saved.id, retry.id)
            )
            assert pool.snapshot()["accepted"] == 0 and pool.snapshot()["workers"] == []
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
            # The memory hold refuses before the pool's own size limit is
            # consulted, so the pool neither accepts nor rejects the job.
            saved = await held_render(store, service, queued.id)
            assert "snapshot size limit" not in saved.error_detail
            assert await held_jobs(service) == [("blocked", "report_pdf", saved.id, 0)]
            assert pool.snapshot()["accepted"] == 0 and pool.snapshot()["rejected"] == 0
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
            await service._tasks[queued.id]
            # Held before dispatch: the render never joins the busy pool's queue.
            await held_render(store, service, queued.id)
            assert pool.snapshot()["queued"] == 0
            assert pool.snapshot()["cancelled_queued"] == 0
            release.touch()
            await held
            retry = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[retry.id]
            assert retry.id != queued.id
            await held_render(store, service, retry.id)
            assert (await store.get(ReportRender, queued.id)).pdf_artifact_id is None
            assert pool.snapshot()["accepted"] == pool.snapshot()["completed"] == 1
            assert not await pdf_artifacts(store)
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
            await service._tasks[queued.id]
            # The blocking renderer is never started, so no late worker output
            # exists to commit.
            await held_render(store, service, queued.id)
            assert not started.exists()
            assert pool.snapshot()["active"] == pool.snapshot()["admitted"] == 0
            assert (await store.get(ReportRender, queued.id)).pdf_artifact_id is None
            assert not await pdf_artifacts(store)
            assert pool.snapshot()["discarded_results"] == 0
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
            await service._tasks[queued.id]
            # No worker is started, so there is no worker to lose; the render
            # saves exactly one failure, the memory hold.
            saved = await held_render(store, service, queued.id)
            assert not started.exists() and pool.snapshot()["workers"] == []
            assert pool.snapshot()["worker_exits"] == 0
            monkeypatch.setattr(reporting, "render_pdf_snapshot", original)
            retry = await service.request_render(
                report.id, report_revision=report.revision
            )
            await service._tasks[retry.id]
            await held_render(store, service, retry.id)
            assert await held_jobs(service) == sorted(
                ("blocked", "report_pdf", identity, 0)
                for identity in (saved.id, retry.id)
            )
            assert pool.snapshot()["workers"] == [] and not await pdf_artifacts(store)
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
        await service._tasks[queued.id]
        await held_render(store, service, queued.id)
        async with asyncio.timeout(3):
            await service.shutdown()
        # The held render was settled before shutdown and is not rewritten; no
        # computation or child process was ever started.
        assert (
            await store.get(ReportRender, queued.id)
        ).status == ReportRenderStatus.FAILED
        assert (await store.get(ReportRender, queued.id)).pdf_artifact_id is None
        assert not started.exists()
        assert not any(worker["alive"] for worker in pool.snapshot()["workers"])
        (await store.database.dispose())
        reopened = NebulaStore(tmp_path / "core.db")
        assert (
            await reopened.get(ReportRender, queued.id)
        ).status == ReportRenderStatus.FAILED
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
            if detail.json()["status"] not in {"queued", "rendering"}:
                break
            assert time.monotonic() < deadline, detail.text
            await asyncio.sleep(0.01)
        assert detail.json()["status"] == "failed", detail.text
        assert MEMORY_HOLD in detail.json()["error_detail"]
        pdf = await client.get(
            f"/api/v1/report-renders/{identity}/pdf", headers=headers
        )
        assert pdf.status_code == 409, pdf.text
        await finished(service, identity)
        await held_render(store, service, identity)
        retry = await client.post(
            f"/api/v1/reports/{report.id}/renders",
            headers=headers,
            json={"report_revision": report.revision},
        )
        assert retry.status_code == 202 and retry.json()["id"] != identity
        await finished(service, retry.json()["id"])
        await held_render(store, service, retry.json()["id"])
        assert pool.snapshot()["accepted"] == 0 and pool.snapshot()["workers"] == []
    assert not any(worker["alive"] for worker in pool.snapshot()["workers"])
    (await store.database.dispose())


pytestmark = pytest.mark.anyio
