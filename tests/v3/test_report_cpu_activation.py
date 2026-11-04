"""Real Core activation, owned affinity and draining PDF worker transitions."""

import asyncio
import os
import threading
import time

from fastapi.testclient import TestClient
import pytest

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.cpu_workers import CpuWorkerBusy, CpuWorkerPool
from nebula.v3.domain import Engagement, Observation, Report, ReportRender
from nebula.v3.report_cpu_executor import ReportCpuExecutor
from nebula.v3.storage import NebulaStore
from tests.v3.cpu_worker_fixtures import hold, identify
from tests.v3.test_cpu_workers import until

HEADERS = {"Authorization": "Bearer synthetic-cpu-activation"}


def fixture(root):
    store = NebulaStore(root / "core.db")
    artifacts = ArtifactStore(root / "artifacts")
    store.create(Engagement(id="project", name="Synthetic worker activation"))
    store.create(
        Observation(
            id="note",
            engagement_id="project",
            observation_type="note",
            title="Saved source",
            body="Synthetic report content. " * 200,
        )
    )
    store.create(
        Report(
            id="report",
            engagement_id="project",
            title="Worker activation PDF",
            observation_ids=["note"],
        )
    )
    return store, artifacts, make_app(root, store, artifacts)


def make_app(root, store, artifacts):
    return create_app(
        store,
        artifact_store=artifacts,
        auth_token="synthetic-cpu-activation",
        execution_data_root=root / "host",
    )


def status(client):
    response = client.get("/api/v1/host-cpu-budget", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def save(client, value, enabled, cpus=1):
    return client.put(
        "/api/v1/host-cpu-budget",
        headers=HEADERS,
        json={
            "host_id": value["host_id"],
            "expected_revision": value["revision"],
            "cpu_limit": cpus,
            "enabled": enabled,
        },
    )


def observed(client, expected):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        value = status(client)
        if value["executor_state"] == expected:
            return value
        assert value["apply_state"] != "failed", value
        time.sleep(0.02)
    raise AssertionError(f"Worker state did not reach {expected}: {value}")


def assert_stopped(pids):
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_normal_core_enables_real_workers_renders_and_disables(tmp_path):
    store, artifacts, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            initial = observed(client, "disabled")
            assert initial["enabled"] is False and initial["worker_count"] == 0
            assert (
                initial["activation_supported"]
                and initial["executor_scope"] == "report-pdfs"
            )
            assert save(client, initial, True).status_code == 200
            running = observed(client, "running")
            assert (
                running["apply_state"] == "applied"
                and running["applied_cpu_limit"] == 1
            )
            assert running["worker_count"] == 1 and running["worker_pids"] != [
                os.getpid()
            ]
            assert len(running["worker_affinities"][0]) == 1
            assert os.getpriority(os.PRIO_PROCESS, running["worker_pids"][0]) >= 10
            response = client.post(
                "/api/v1/reports/report/renders",
                headers=HEADERS,
                json={"report_revision": 1},
            )
            assert response.status_code in (200, 202), response.text
            render_id = response.json()["id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                render = store.get(ReportRender, render_id)
                if render.status.value not in {"queued", "rendering"}:
                    break
                time.sleep(0.02)
            assert render.status.value == "completed", render.error_detail
            service = app.state.report_render_service
            artifact, path = service.pdf(render_id)
            assert artifacts.verify(artifact) and path.read_bytes().startswith(b"%PDF-")
            prepared = service._prepare_render("report", 1)
            expected, _ = service._build_pdf(prepared.snapshot)
            assert path.read_bytes() == expected
            pool = app.state.report_cpu_executor.pool
            assert pool.snapshot()["completed"] == 1
            assert save(client, running, False).status_code == 200
            disabled = observed(client, "disabled")
            assert disabled["enabled"] is False and disabled["worker_count"] == 0
            assert_stopped(running["worker_pids"])
    finally:
        store.database.dispose()


def test_restart_restores_enabled_policy_and_omitted_enabled_preserves_it(tmp_path):
    store, artifacts, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            initial = observed(client, "disabled")
            assert save(client, initial, True).status_code == 200
            first = observed(client, "running")
        assert_stopped(first["worker_pids"])
        restarted = make_app(tmp_path, store, artifacts)
        with TestClient(restarted) as client:
            current = observed(client, "running")
            assert current["enabled"] and current["host_id"] == first["host_id"]
            assert not set(current["worker_pids"]) & set(first["worker_pids"])
            response = client.put(
                "/api/v1/host-cpu-budget",
                headers=HEADERS,
                json={
                    "host_id": current["host_id"],
                    "expected_revision": current["revision"],
                    "cpu_limit": 1,
                },
            )
            assert response.status_code == 200 and response.json()["enabled"] is True
            assert save(client, first, False).status_code == 409
            assert status(client)["enabled"] is True
        assert_stopped(current["worker_pids"])
    finally:
        store.database.dispose()


def test_affinity_read_failure_keeps_worker_counts_unknown_until_recovery(
    tmp_path, monkeypatch
):
    store, _, app = fixture(tmp_path)
    release, started = tmp_path / "release", tmp_path / "started"
    work = []
    try:
        with TestClient(app) as client:
            assert save(client, status(client), True).status_code == 200
            running = observed(client, "running")
            executor = app.state.report_cpu_executor

            async def start_jobs():
                work.append(
                    asyncio.create_task(executor.run(hold, str(release), str(started)))
                )
                await until(started.exists)
                work.append(asyncio.create_task(executor.run(identify, "queued")))
                await until(lambda: executor.pool.snapshot()["queued"] == 1)

            async def finish_jobs():
                release.touch()
                await asyncio.wait_for(asyncio.gather(*work), timeout=10)

            original = os.sched_getaffinity

            def failed_affinity(pid):
                if pid in running["worker_pids"]:
                    raise OSError("Synthetic live worker affinity read failure")
                return original(pid)

            try:
                client.portal.call(start_jobs)
                with monkeypatch.context() as patch:
                    patch.setattr(os, "sched_getaffinity", failed_affinity)
                    unavailable = status(client)
                    assert unavailable["apply_state"] == "unavailable"
                    for field in (
                        "applied_cpu_limit",
                        "worker_count",
                        "active_jobs",
                        "queued_jobs",
                        "worker_pids",
                        "worker_affinities",
                    ):
                        assert unavailable[field] is None
                    actual = executor.pool.snapshot()
                    assert actual["active"] == actual["queued"] == 1
                    assert actual["workers"][0]["alive"]
                recovered = status(client)
                assert recovered["worker_pids"] == running["worker_pids"]
                assert [
                    recovered[field]
                    for field in ("worker_count", "active_jobs", "queued_jobs")
                ] == [1, 1, 1]
            finally:
                client.portal.call(finish_jobs)
            assert save(client, recovered, False).status_code == 200
            assert observed(client, "disabled")["worker_count"] == 0
            assert_stopped(running["worker_pids"])
    finally:
        release.touch()
        store.database.dispose()


def test_reduction_repins_and_drains_before_replacement(tmp_path):
    async def scenario():
        executor = ReportCpuExecutor()
        release = tmp_path / "release"
        work = []
        try:
            await executor.request(True, 2)
            await until(lambda: executor.state == "running")
            original = executor.pool
            old_pids = [worker["pid"] for worker in original.snapshot()["workers"]]
            for index in range(2):
                work.append(
                    asyncio.create_task(
                        executor.run(
                            hold, str(release), str(tmp_path / f"started-{index}")
                        )
                    )
                )
            await until(
                lambda: all(
                    (tmp_path / f"started-{index}").exists() for index in range(2)
                )
            )
            queued = asyncio.create_task(
                executor.run(identify, "accepted before resize")
            )
            await until(lambda: original.snapshot()["queued"] == 1)
            await executor.request(True, 1)
            await until(
                lambda: (
                    original.cpu_affinity is not None
                    and len(original.cpu_affinity) == 1
                )
            )
            facts = await executor.observe()
            assert (
                facts["executor_state"] == "draining"
                and facts["applied_cpu_limit"] == 1
            )
            with pytest.raises(CpuWorkerBusy):
                await executor.run(identify)
            release.touch()
            await asyncio.gather(*work)
            assert (await queued)[1] == "accepted before resize"
            await until(lambda: executor.state == "running" and executor.limit == 1)
            assert (
                original.snapshot()["closed"] and original.snapshot()["admitted"] == 0
            )
            assert_stopped(old_pids)
            assert (await executor.observe())["worker_count"] == 1
        finally:
            release.touch()
            await executor.shutdown()

    asyncio.run(scenario())


def test_disable_drains_cancelled_running_work_and_accepted_queue(tmp_path):
    async def scenario():
        executor = ReportCpuExecutor()
        release, started = tmp_path / "release", tmp_path / "started"
        try:
            await executor.request(True, 1)
            await until(lambda: executor.state == "running")
            original = executor.pool
            pids = (await executor.observe())["worker_pids"]
            work = asyncio.create_task(executor.run(hold, str(release), str(started)))
            await until(started.exists)
            queued = asyncio.create_task(executor.run(identify, "queued"))
            await until(lambda: original.snapshot()["queued"] == 1)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            await executor.request(False, 1)
            assert executor.state == "draining" and original.snapshot()["admitted"] == 2
            with pytest.raises(CpuWorkerBusy):
                await executor.run(identify)
            release.touch()
            assert (await queued)[1] == "queued"
            await until(lambda: executor.state == "disabled")
            assert original.snapshot()["discarded_results"] == 1
            assert_stopped(pids)
            assert (await executor.run(identify))[0] == os.getpid()
        finally:
            release.touch()
            await executor.shutdown()

    asyncio.run(scenario())


def test_enable_waits_for_uncancellable_legacy_thread_after_caller_cancellation(
    tmp_path,
):
    async def scenario():
        executor = ReportCpuExecutor()
        release, started = tmp_path / "release", tmp_path / "started"
        try:
            work = asyncio.create_task(executor.run(hold, str(release), str(started)))
            await until(started.exists)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            await executor.request(True, 1)
            await asyncio.sleep(0)
            assert executor.state == "draining" and executor.pool is None
            release.touch()
            await until(lambda: executor.state == "running")
            assert (await executor.run(identify))[0] != os.getpid()
        finally:
            release.touch()
            await executor.shutdown()

    asyncio.run(scenario())


def test_failed_start_keeps_saved_enabled_state_and_retry_starts_workers(tmp_path):
    store, _, app = fixture(tmp_path)
    executor = app.state.report_cpu_executor

    def fail(**kwargs):
        raise RuntimeError("Synthetic activation failure")

    executor.pool_factory = fail
    try:
        with TestClient(app) as client:
            initial = observed(client, "disabled")
            assert save(client, initial, True).status_code == 200
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                failed = status(client)
                if failed["apply_state"] == "failed":
                    break
                time.sleep(0.01)
            assert (
                failed["enabled"]
                and failed["apply_state"] == "failed"
                and not failed["worker_pids"]
            )
            executor.pool_factory = CpuWorkerPool
            assert save(client, failed, True).status_code == 200
            assert observed(client, "running")["applied_cpu_limit"] == 1
    finally:
        store.database.dispose()


def test_unsupported_activation_and_sub_cpu_limits_do_not_change_policy(
    tmp_path, monkeypatch
):
    store, _, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            initial = observed(client, "disabled")
            assert save(client, initial, True, 0.5).status_code == 422
            assert status(client)["revision"] == initial["revision"]
            monkeypatch.setattr(
                ReportCpuExecutor,
                "support_reason",
                staticmethod(lambda: "Synthetic unsupported host"),
            )
            unsupported = status(client)
            assert not unsupported["activation_supported"]
            assert save(client, unsupported, True).status_code == 422
            assert status(client)["revision"] == initial["revision"]
    finally:
        store.database.dispose()


def test_legacy_policy_migrates_to_disabled_without_starting_workers(tmp_path):
    store, artifacts, app = fixture(tmp_path)
    try:
        with TestClient(app) as client:
            initial = observed(client, "disabled")
        path = tmp_path / "host" / "host-cpu-budget.json"
        import json

        policy = json.loads(path.read_text())
        policy.pop("enabled")
        path.write_text(json.dumps(policy))
        with TestClient(make_app(tmp_path, store, artifacts)) as client:
            value = observed(client, "disabled")
            assert (
                value["enabled"] is False and value["revision"] == initial["revision"]
            )
            assert not value["worker_pids"]
    finally:
        store.database.dispose()


def test_shutdown_during_startup_removes_partially_started_pool():
    async def scenario():
        executor = ReportCpuExecutor()
        await executor.request(True, 2)
        await until(lambda: executor.pool is not None)
        pool = executor.pool
        await executor.shutdown()
        assert executor.state == "disabled"
        assert not any(worker["alive"] for worker in pool.snapshot()["workers"])

    asyncio.run(scenario())


def test_butler_activates_and_reads_actual_running_then_disabled_workers(tmp_path):
    from nebula.v3.butler_capabilities import ButlerCapabilities
    from nebula.v3.settings_assistant_api import SettingsAssistantApi

    store, artifacts, app = fixture(tmp_path)
    butler = ButlerCapabilities(
        store, artifacts, "project", work=app.state.control_assistant_work
    )
    gateway = SettingsAssistantApi(app, "synthetic-cpu-activation")

    async def scenario():
        async with app.router.lifespan_context(app):
            found = await butler.search({"query": "enable background PDF workers"})
            assert any(
                row["operation"] == "nebula.work.host_cpu_budget"
                for row in found["matches"]
            )

            async def read():
                return await butler.invoke(
                    {"operation": "nebula.work.host_cpu_budget", "arguments": {}},
                    change=False,
                )

            current = await read()
            enabled = await gateway.request(
                "project",
                method="PUT",
                path="host-cpu-budget",
                body={
                    "host_id": current["host_id"],
                    "expected_revision": current["revision"],
                    "cpu_limit": 1,
                    "enabled": True,
                },
            )
            assert enabled["read_back"] and enabled["result"]["enabled"]
            async with asyncio.timeout(20):
                while (running := await read())["executor_state"] != "running":
                    await asyncio.sleep(0.02)
            assert running["apply_state"] == "applied" and running["worker_count"] == 1
            disabled = await gateway.request(
                "project",
                method="PUT",
                path="host-cpu-budget",
                body={
                    "host_id": running["host_id"],
                    "expected_revision": running["revision"],
                    "cpu_limit": 1,
                    "enabled": False,
                },
            )
            assert disabled["read_back"] and disabled["result"]["enabled"] is False
            async with asyncio.timeout(20):
                while (done := await read())["executor_state"] != "disabled":
                    await asyncio.sleep(0.02)
            assert done["worker_count"] == 0
            assert_stopped(running["worker_pids"])

    try:
        asyncio.run(scenario())
    finally:
        store.database.dispose()


def test_cancelled_legacy_caller_still_records_late_thread_failure(monkeypatch):
    events = []

    def record(*args, **kwargs):
        events.append((args, kwargs))
        return "synthetic-legacy-error"

    monkeypatch.setattr("nebula.v3.diagnostics.record_diagnostic", record)

    async def scenario():
        executor = ReportCpuExecutor()
        entered, release = threading.Event(), threading.Event()

        def fail_after_release():
            entered.set()
            assert release.wait(5)
            raise RuntimeError("Synthetic late PDF failure")

        try:
            work = asyncio.create_task(executor.run(fail_after_release))
            assert await asyncio.to_thread(entered.wait, 5)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            assert len(executor._legacy) == 1
            release.set()
            await until(lambda: not executor._legacy)
            assert any(
                args[2] == "reports.cpu_activation.legacy_failed.failed"
                and isinstance(kwargs.get("exception"), RuntimeError)
                for args, kwargs in events
            )
        finally:
            release.set()
            await executor.shutdown()

    asyncio.run(scenario())


def test_shutdown_cancels_transition_but_waits_for_actual_legacy_thread():
    async def scenario():
        executor = ReportCpuExecutor()
        entered, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def held_thread():
            entered.set()
            assert release.wait(5)
            finished.set()

        stopping = None
        try:
            work = asyncio.create_task(executor.run(held_thread))
            assert await asyncio.to_thread(entered.wait, 5)
            inner = next(iter(executor._legacy))
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            await executor.request(True, 1)
            await asyncio.sleep(0)
            assert executor.state == "draining"
            stopping = asyncio.create_task(executor.shutdown())
            await until(lambda: executor._transition.done())
            assert not inner.cancelled() and len(executor._legacy) == 1
            assert not stopping.done() and not finished.is_set()
            release.set()
            await stopping
            assert finished.is_set() and not executor._legacy and executor.pool is None
        finally:
            release.set()
            if stopping is not None:
                await stopping
            else:
                await executor.shutdown()

    asyncio.run(scenario())


def test_later_core_startup_failure_stops_restored_workers(tmp_path, monkeypatch):
    from nebula.v3.host_cpu_budget import HostCpuPolicy

    store, _, app = fixture(tmp_path)
    service = app.state.host_cpu_budget
    value = asyncio.run(service.status())
    service._write(
        HostCpuPolicy(host_id=value.host_id, revision=2, enabled=True, cpu_limit=1)
    )
    executor = app.state.report_cpu_executor
    pids = []

    async def fail_later():
        await until(lambda: executor.state == "running")
        pids.extend((await executor.observe())["worker_pids"])
        raise RuntimeError("Synthetic later Core startup failure")

    monkeypatch.setattr(app.state.scope_import_service, "startup", fail_later)
    try:
        with pytest.raises(RuntimeError, match="Synthetic later Core startup failure"):
            with TestClient(app):
                pytest.fail("Core must not complete the failed startup")
        assert pids and executor.state == "disabled" and executor.pool is None
        assert_stopped(pids)
    finally:
        store.database.dispose()
