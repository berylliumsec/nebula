from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from dataclasses import replace
import time

import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import ChatTurn, ChatTurnStatus, Engagement, utc_now
from nebula.v3.host_load import HostLoadGate, HostLoadSnapshot, HostPressureThresholds
from nebula.v3.host_pressure import HostPressureSettings, HostPressureUpdate
from nebula.v3.provider_scheduler import ProviderScheduler, ProviderSchedulerConfig
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.storage import NebulaStore
from tests.v3.test_host_load import ManualMonitor
from tests.v3.test_chat_throughput import _store


@pytest.mark.parametrize("metric", ["cpu", "memory", "load", "io"])
@pytest.mark.parametrize("value,busy", [(0.799, False), (0.8, True), (0.801, True)])
def test_each_available_metric_at_boundary(metric, value, busy):
    fields = {
        "cpu": "cpu_ratio",
        "memory": "memory_available_ratio",
        "load": "load_average_ratio",
        "io": "io_pressure_ratio",
    }
    snapshot = HostLoadSnapshot(time.monotonic(), 8, None, None, False, None, True)
    snapshot = replace(
        snapshot, **{fields[metric]: 1 - value if metric == "memory" else value}
    )
    result = snapshot.evaluate(HostPressureThresholds())
    assert result.busy is busy
    assert result.pressure_metric == (metric if busy else None)
    # Prior busy state does not create a lower recovery threshold.
    result = replace(snapshot, busy=True).evaluate(HostPressureThresholds())
    assert result.busy is busy


def test_unknown_metrics_do_not_create_pressure():
    snapshot = HostLoadSnapshot(time.monotonic(), 8, None, None, True, "stale", False)
    assert not snapshot.evaluate(HostPressureThresholds()).busy


async def test_saved_thresholds_apply_to_cached_readings_and_survive_restart(tmp_path):
    monitor = ManualMonitor()
    monitor.value = replace(monitor.value, cpu_ratio=0.85)
    gate = HostLoadGate(monitor)
    settings = HostPressureSettings(tmp_path / "pressure.json", gate)
    await settings.startup()
    before = await settings.status()
    assert before["waiting"]
    waiter = asyncio.create_task(gate.wait())
    await asyncio.sleep(0.03)
    assert not waiter.done()
    await settings.update(
        HostPressureUpdate(
            host_id=before["host_id"],
            expected_revision=before["revision"],
            thresholds=HostPressureThresholds(cpu=0.9),
        )
    )
    await asyncio.wait_for(waiter, 1)
    recovered = HostPressureSettings(settings.path, HostLoadGate(monitor))
    await recovered.startup()
    after = await recovered.status()
    assert after["saved_thresholds"] == after["effective_thresholds"]
    assert after["saved_thresholds"]["cpu"] == 0.9
    assert after["revision"] == 2 and not after["waiting"]


@pytest.mark.parametrize(
    "overrides,expected",
    [(False, (60, 58)), (True, (12, 10)), ("provider_only", (7, 5))],
)
async def test_authenticated_settings_and_effective_startup_limits(
    tmp_path, monkeypatch, overrides, expected
):
    for name in ("NEBULA_PROVIDER_CONCURRENCY", "NEBULA_BACKGROUND_CONCURRENCY"):
        monkeypatch.delenv(name, raising=False)
    if overrides == "provider_only":
        monkeypatch.setenv("NEBULA_PROVIDER_CONCURRENCY", "7")
    elif overrides:
        monkeypatch.setenv("NEBULA_PROVIDER_CONCURRENCY", "12")
        monkeypatch.setenv("NEBULA_BACKGROUND_CONCURRENCY", "10")
    store = NebulaStore(tmp_path / "core.db")
    await store.create(Engagement(id="project", name="Synthetic pressure"))
    app = create_app(
        store, auth_token="synthetic-pressure-token", execution_data_root=tmp_path
    )
    async with TestClient(app) as client:
        assert (await client.get("/api/v1/host-pressure")).status_code == 401
        headers = {"Authorization": "Bearer synthetic-pressure-token"}
        before = (await client.get("/api/v1/host-pressure", headers=headers)).json()
        limits = before["concurrency"]
        assert (limits["provider_limit"], limits["background_limit"]) == expected
        assert before["effective_thresholds"] == dict(
            cpu=0.8, memory=0.8, load=0.8, io=0.8
        )
        update = {
            "host_id": before["host_id"],
            "expected_revision": before["revision"],
            "thresholds": dict(cpu=0.9, memory=0.85, load=0.95, io=0.7),
        }
        assert (
            await client.put("/api/v1/host-pressure", json=update)
        ).status_code == 401
        catalog = await app.state.control_assistant_work.catalog("project")
        assert "nebula.work.host_pressure" in catalog["actions"]
        assert catalog["host_admission"]["settings_route"] == "/api/v1/host-pressure"
        gateway = SettingsAssistantApi(app, "synthetic-pressure-token")
        assert (
            gateway.schema(path="host-pressure", method="PUT")["path"]
            == "/api/v1/host-pressure"
        )
        result = await gateway.request(
            "project", method="PUT", path="host-pressure", body=update
        )
        assert result["read_back"] is True
        assert result["client_action"]["kind"] == "host_pressure_refresh"
        after = (await client.get("/api/v1/host-pressure", headers=headers)).json()
        assert after["effective_thresholds"] == update["thresholds"]
        assert (
            await client.put("/api/v1/host-pressure", headers=headers, json=update)
        ).status_code == 409
        update["expected_revision"] = after["revision"]
        update["thresholds"]["cpu"] = 0
        assert (
            await client.put("/api/v1/host-pressure", headers=headers, json=update)
        ).status_code == 422
    await store.database.dispose()


async def test_pressure_queue_releases_slots_cancels_and_recovers_in_order(tmp_path):
    store, project, profile, session = await _store(tmp_path)
    monitor = ManualMonitor()
    gate = HostLoadGate(monitor)
    config = ProviderSchedulerConfig(provider_limit=2, background_limit=1)
    scheduler = ProviderScheduler(
        store, worker_id="before-restart", config=config, host_load=gate
    )
    turns = []
    for lane in ("background", "direct", "direct"):
        turn = await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                capacity_lane=lane,
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        await scheduler.enqueue(turn)
        turns.append(turn)
    tasks = [asyncio.create_task(scheduler.admit(turn.id)) for turn in turns]
    try:
        for _ in range(100):
            if all([(await store.get(ChatTurn, t.id)).admission_reason for t in turns]):
                break
            await asyncio.sleep(0.01)
        assert scheduler._provider._value == 2 and scheduler._background._value == 1
        assert (
            "host pressure" in (await store.get(ChatTurn, turns[1].id)).admission_reason
        )
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        await scheduler.cancel(turns[1].id)
        # Simulate coordinator replacement while the durable queue remains.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        restarted = ProviderScheduler(
            store, worker_id="after-restart", config=config, host_load=gate
        )
        assert set(await restarted.recover()) == {turns[0].id, turns[2].id}
        pending = [asyncio.create_task(restarted.admit(turns[i].id)) for i in (0, 2)]
        monitor.recover()
        admissions = await asyncio.wait_for(asyncio.gather(*pending), 2)
        assert all(
            [
                (await store.get(ChatTurn, turns[i].id)).admission_reason is None
                for i in (0, 2)
            ]
        )
        for admission, i in zip(admissions, (0, 2)):
            await admission.release(turns[i].id)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await store.database.dispose()


async def test_capacity_wait_rechecks_pressure_and_releases_slots(tmp_path):
    store, project, profile, session = await _store(tmp_path)
    monitor = ManualMonitor(busy=False)
    scheduler = ProviderScheduler(
        store,
        worker_id="capacity",
        host_load=HostLoadGate(monitor),
        config=ProviderSchedulerConfig(provider_limit=1, background_limit=1),
    )
    turns = []
    for _ in range(2):
        turn = await store.create(
            ChatTurn(
                engagement_id=project.id,
                session_id=session.id,
                provider_profile_id=profile.id,
                model="model-a",
                status=ChatTurnStatus.QUEUED,
                queued_at=utc_now(),
            )
        )
        await scheduler.enqueue(turn)
        turns.append(turn)
    first = await scheduler.admit(turns[0].id)
    pending = asyncio.create_task(scheduler.admit(turns[1].id))
    try:
        for _ in range(100):
            if (
                await store.get(ChatTurn, turns[1].id)
            ).admission_reason == "Waiting for provider capacity":
                break
            await asyncio.sleep(0.01)
        assert (
            await store.get(ChatTurn, turns[1].id)
        ).admission_reason == "Waiting for provider capacity"
        monitor.value = replace(monitor.value, cpu_ratio=0.9)
        assert scheduler.admitted(turns[0].id)
        await first.release(turns[0].id)
        for _ in range(100):
            if "host pressure" in (
                (await store.get(ChatTurn, turns[1].id)).admission_reason or ""
            ):
                break
            await asyncio.sleep(0.01)
        assert not pending.done()
        assert scheduler._provider._value == 1
        assert (
            "host pressure" in (await store.get(ChatTurn, turns[1].id)).admission_reason
        )
        monitor.recover()
        second = await asyncio.wait_for(pending, 2)
        await second.release(turns[1].id)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await first.release(turns[0].id)
        await store.database.dispose()


pytestmark = pytest.mark.anyio
