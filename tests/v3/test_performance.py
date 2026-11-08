from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import hashlib
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient
from sqlalchemy import literal, select, text
from sqlalchemy.exc import DBAPIError

from nebula.v3.api import create_app
from nebula.v3.database import Database
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.domain import Engagement
from nebula.v3.performance import (
    EventLoopLagMonitor,
    PerformanceMiddleware,
    PerformanceTelemetry,
    _Request,
    current_performance,
)
from nebula.v3.read_workers import read_in_worker
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore


AUTH = {"Authorization": "Bearer synthetic-performance-token"}


def _operations(performance):
    return {item["operation"]: item for item in performance.snapshot()["operations"]}


def test_counts_recent_percentiles_and_memory_limits():
    performance = PerformanceTelemetry(max_operations=2, max_slow=3, slow_ms=5)
    for index in range(200):
        performance.record("read", index)
    performance.record("write", 10, "error")
    for index in range(20):
        performance.record(f"extra-{index}", 10)
    snapshot = performance.snapshot()
    read = _operations(performance)["read"]
    assert read["count"] == 200
    assert read["total_ms"] == sum(range(200))
    assert read["sample_count"] == 128
    assert read["p50_ms"] == 135
    assert read["p95_ms"] == 193
    assert read["max_ms"] == 199
    assert _operations(performance)["write"]["errors"] == 1
    assert _operations(performance)["other"]["count"] == 20
    assert len(snapshot["operations"]) == 3
    assert len(snapshot["recent_slow"]) == 3
    assert snapshot["overflow_measurements"] == 20


def test_measurements_are_thread_safe_and_isolated_between_cores():
    performance = PerformanceTelemetry()

    def record():
        for _ in range(500):
            performance.record("read", 1)

    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(lambda _: record(), range(6)))
    assert _operations(performance)["read"]["count"] == 3000
    assert PerformanceTelemetry().snapshot()["operations"] == []


def test_spans_record_failure_and_cancellation_and_close_inherited_requests():
    performance = PerformanceTelemetry(slow_ms=0)
    request = _Request("req_synthetic")
    with performance.activate(request):
        with pytest.raises(ValueError), performance.span("failed"):
            raise ValueError("canary-private-value")
        with pytest.raises(asyncio.CancelledError), performance.span("cancelled"):
            raise asyncio.CancelledError
        assert current_performance() is performance
        performance.close_request(request)
        performance.record("detached", 100)
    assert current_performance() is None
    operations = _operations(performance)
    assert operations["failed"]["errors"] == 1
    assert operations["cancelled"]["cancelled"] == 1
    assert operations["cancelled"]["errors"] == 0
    assert "request_id" not in performance.snapshot()["recent_slow"][0]
    assert request.phases == {}
    assert "canary-private-value" not in json.dumps(performance.snapshot())


async def test_sql_fingerprints_errors_expanding_parameters_and_source_privacy(
    tmp_path,
):
    database = Database(tmp_path / "sql.db", bootstrap=False)
    try:
        async with database.engine.connect() as connection:
            (await connection.execute(select(literal("canary-secret-a")))).all()
            (await connection.execute(select(literal("canary-secret-b")))).all()
            (
                await connection.execute(
                    select(literal(1)).where(literal(1).in_([1, 2]))
                )
            ).all()
            (
                await connection.execute(
                    select(literal(1)).where(literal(1).in_([1, 2, 3]))
                )
            ).all()
            with pytest.raises(DBAPIError):
                (
                    await connection.execute(
                        text("SELECT * FROM canary_private_missing_table")
                    )
                )
        operations = list(_operations(database.performance).values())
        assert sorted(item["count"] for item in operations) == [1, 2, 2]
        assert sum(item["errors"] for item in operations) == 1
        encoded = json.dumps(database.performance.snapshot())
        assert "canary" not in encoded
        assert "SELECT" not in encoded
    finally:
        (await database.dispose())

    store = NebulaStore(tmp_path / "source.db")
    try:
        # Migration DDL is excluded from runtime operation cardinality.
        assert not any(
            name.startswith("db.execute")
            for name in _operations(store.database.performance)
        )
        store.database.performance.slow_ms = 0
        (await store.list_entities(Engagement))
        assert any(
            item.get("source", "").startswith("nebula/v3/storage.py:")
            for item in store.database.performance.snapshot()["recent_slow"]
        )
    finally:
        (await store.database.dispose())


async def test_worker_queue_is_separate_and_actual_work_survives_waiter_cancellation(
    monkeypatch,
):
    performance = PerformanceTelemetry(slow_ms=0)
    entered = threading.Event()
    release = threading.Event()
    workers = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr("nebula.v3.read_workers._READERS", workers)

    def blocked():
        assert current_performance() is performance
        entered.set()
        assert release.wait(3)
        return 1

    def quick():
        return 2

    blocked.__module__ = quick.__module__ = "nebula.fixture"

    async def scenario():
        with performance.activate(_Request("req_worker")):
            first = asyncio.create_task(read_in_worker(blocked))
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(read_in_worker(quick))
            try:
                await asyncio.sleep(0.025)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            finally:
                release.set()
            assert await second == 2

    try:
        (await scenario())
    finally:
        release.set()
        workers.shutdown()
    operations = _operations(performance)
    queued = next(
        item
        for name, item in operations.items()
        if name.startswith("worker.queue") and "quick" in name
    )
    executed = next(
        item
        for name, item in operations.items()
        if name.startswith("worker.run") and "quick" in name
    )
    assert queued["max_ms"] >= 20
    assert queued["max_ms"] > executed["max_ms"]
    assert (
        sum(
            item["count"]
            for name, item in operations.items()
            if name.startswith("worker.run")
        )
        == 2
    )
    assert any(
        item.get("request_id") == "req_worker"
        for item in performance.snapshot()["recent_slow"]
    )


def test_event_loop_lag_measures_lateness_and_stops_its_timer(monkeypatch):
    performance = PerformanceTelemetry()
    now = [10.0]
    scheduled = []

    def call_at(due, callback):
        handle = SimpleNamespace(cancelled=False)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        scheduled.append((due, callback, handle))
        return handle

    loop = SimpleNamespace(time=lambda: now[0], call_at=call_at)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    monitor = EventLoopLagMonitor(performance)
    monitor.start()
    assert scheduled[0][0] == 11
    now[0] = 11.4
    scheduled[0][1]()
    assert _operations(performance)["runtime.event_loop_lag"]["max_ms"] == 400
    monitor.stop()
    assert scheduled[-1][2].cancelled


async def test_long_stream_lifetime_does_not_create_a_slow_response(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("nebula.v3.performance.time.perf_counter", lambda: clock[0])
    performance = PerformanceTelemetry(slow_ms=250)

    async def app(scope, _receive, send):
        scope["route"] = SimpleNamespace(path="/stream/{id}")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        clock[0] = 60
        await send({"type": "http.response.body", "body": b"data: private\n\n"})

    async def send(_message):
        pass

    (
        await PerformanceMiddleware(app, performance=performance)(
            {
                "type": "http",
                "method": "GET",
                "path": "/stream/private-id",
                "state": {},
            },
            None,
            send,
        )
    )
    operations = _operations(performance)
    assert operations["http.response GET /stream/{id}"]["max_ms"] == 0
    assert operations["http.stream GET /stream/{id}"]["max_ms"] == 60000
    assert performance.snapshot()["recent_slow"] == []
    assert "private" not in json.dumps(performance.snapshot())


async def test_api_authentication_correlation_route_privacy_and_polling_exclusion(
    tmp_path,
):
    store = NebulaStore(tmp_path / "api.db")
    performance = store.database.performance
    performance.slow_ms = 0
    app = create_app(store, auth_token="synthetic-performance-token")

    @app.get("/api/v1/performance-fixture/{item}")
    async def read_fixture(item: str):
        return await store.list_entities(Engagement)

    try:
        async with TestClient(app) as client:
            assert (
                await client.get("/api/v1/diagnostics/performance")
            ).status_code == 401
            response = await client.get(
                "/api/v1/performance-fixture/canary-private-id?secret=canary-query",
                headers=AUTH,
            )
            assert response.status_code == 200
            snapshot = await client.get("/api/v1/diagnostics/performance", headers=AUTH)
            assert snapshot.status_code == 200
            assert snapshot.headers["cache-control"] == "private, no-store"
            slow = next(
                item
                for item in snapshot.json()["recent_slow"]
                if item["operation"]
                == "http.response GET /api/v1/performance-fixture/{item}"
            )
            assert slow["request_id"] == response.headers["x-request-id"]
            phases = {item["operation"] for item in slow["phases"]}
            assert any(name.startswith("db.execute.select.") for name in phases)
            assert any(name.startswith("worker.queue ") for name in phases)
            assert any(name.startswith("worker.run ") for name in phases)
            assert "canary" not in snapshot.text
            assert not any(
                "http." in item["operation"]
                and "diagnostics/performance" in item["operation"]
                for item in snapshot.json()["operations"]
            )
    finally:
        (await store.database.dispose())


async def test_live_export_contains_manifested_performance_and_assistant_can_discover_it(
    tmp_path,
):
    manager = DiagnosticManager(tmp_path / "logs", watch_settings=False)
    store = NebulaStore(tmp_path / "export.db")
    project = await store.create(Engagement(name="Synthetic performance"))
    app = create_app(
        store, auth_token="synthetic-performance-token", diagnostic_manager=manager
    )
    gateway = SettingsAssistantApi(app, "synthetic-performance-token")
    try:
        async with TestClient(app) as client:
            schema = gateway.schema(path="diagnostics/performance", method="GET")
            assert schema["path"] == "/api/v1/diagnostics/performance"
            discovered = json.dumps(gateway.schema())
            assert "diagnostics/performance" in discovered
            observed = await gateway.request(
                project.id, method="GET", path="diagnostics/performance"
            )
            assert observed["result"]["schema"] == "nebula.performance/v1"
            assert observed["read_back"] is True
            response = await client.post("/api/v1/diagnostics/export", headers=AUTH)
            assert response.status_code == 200
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                payload = archive.read("performance.json")
                assert json.loads(payload)["schema"] == "nebula.performance/v1"
                manifest = json.loads(archive.read("SHA256SUMS.json"))
                assert (
                    manifest["sha256"]["performance.json"]
                    == hashlib.sha256(payload).hexdigest()
                )
        assert not list((manager.data_dir / "diagnostics-exports").glob("*.zip"))
    finally:
        manager.close()
        (await store.database.dispose())


async def test_provider_admission_wait_records_cancellation_without_leaking_capacity(
    tmp_path,
):
    from nebula.v3.domain import ChatTurn, utc_now
    from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
    from tests.v3.test_chat_throughput import _store

    store, project, profile, session = await _store(tmp_path)
    scheduler = ProviderScheduler(
        store,
        worker_id="fixture",
        config=ProviderSchedulerConfig(provider_limit=2, background_limit=1),
    )
    turns = [
        (
            await store.create(
                ChatTurn(
                    engagement_id=project.id,
                    session_id=session.id,
                    provider_profile_id=profile.id,
                    model="model-a",
                    capacity_lane="background",
                    queued_at=utc_now(),
                )
            )
        )
        for _ in range(2)
    ]
    for turn in turns:
        (await scheduler.enqueue(turn))

    async def scenario():
        admission = await scheduler.admit(turns[0].id)
        waiting = asyncio.create_task(scheduler.admit(turns[1].id))
        # Wait for the asynchronous store read to reach the capacity wait.
        for _ in range(200):
            if scheduler._background._waiters:
                break
            await asyncio.sleep(0.005)
        assert scheduler._background._waiters
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await admission.release(turns[0].id)
        second = await scheduler.admit(turns[1].id)
        await second.release(turns[1].id)

    try:
        (await scenario())
        operations = _operations(store.database.performance)
        assert operations["provider.capacity.background"]["count"] == 3
        assert operations["provider.capacity.background"]["cancelled"] == 1
        assert operations["provider.capacity.shared"]["count"] == 2
    finally:
        (await store.database.dispose())


pytestmark = pytest.mark.anyio
