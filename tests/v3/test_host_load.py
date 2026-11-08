from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
import threading
import time

import httpx
import pytest

from nebula.v3.api import create_app
from nebula.v3.domain import (
    AgentRun,
    ChatMessage,
    ChatTurn,
    Engagement,
    HarnessTurn,
    ProviderProfile,
    RunBudget,
    RunStatus,
)
from nebula.v3.host_load import HostLoadGate, HostLoadMonitor, HostLoadSnapshot
from nebula.v3.missions import MissionService
from nebula.v3.storage import NebulaStore
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from tests.v3.test_chat_api import ApiChatProvider
from tests.v3.test_harnesses import _runtime
from tests.v3.test_missions import RecordingProvider, _gated_runtime_factory, _profile


class ManualMonitor(HostLoadMonitor):
    def __init__(self, busy=True, capacity=4):
        super().__init__(interval=0.01)
        self.value = HostLoadSnapshot(
            time.monotonic(),
            capacity,
            0.95 if busy else 0.1,
            0.5,
            busy,
            "Waiting for host CPU capacity" if busy else None,
            True,
        )

    def cached(self):
        return self.value

    def sample(self):
        return self.value

    def recover(self):
        self.value = replace(
            self.value,
            sampled_at=self.value.sampled_at + 1,
            cpu_ratio=0.1,
            busy=False,
            reason=None,
        )


def _os_fixture(tmp_path, monkeypatch):
    proc, cg = tmp_path / "proc", tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    (cg / "cpu.max").write_text("50000 100000\n")
    (cg / "cpu.stat").write_text("usage_usec 0\n")
    (cg / "memory.max").write_text("1000\n")
    (cg / "memory.current").write_text("500\n")
    monkeypatch.setattr("nebula.v3.host_load.os.cpu_count", lambda: 8)
    monkeypatch.setattr(
        "nebula.v3.host_load.os.sched_getaffinity", lambda _: set(range(8))
    )
    monkeypatch.setattr("nebula.v3.host_load.os.getloadavg", lambda: (0, 0, 0))
    clock = [1.0]
    monitor = HostLoadMonitor(proc=proc, cgroup=cg, clock=lambda: clock[0])
    return monitor, proc, cg, clock


def test_cpu_quota_cache_and_threshold(tmp_path, monkeypatch):
    monitor, _, cg, clock = _os_fixture(tmp_path, monkeypatch)
    first = monitor.sample()
    assert first.cpu_capacity == 0.5 and not first.busy
    original = monitor._text
    monkeypatch.setattr(
        monitor, "_text", lambda _: pytest.fail("cached sample did I/O")
    )
    assert monitor.sample() is first
    monkeypatch.setattr(monitor, "_text", original)
    for usage, busy in [(475000, True), (875000, True), (975000, False)]:
        clock[0] += 1
        (cg / "cpu.stat").write_text(f"usage_usec {usage}\n")
        assert monitor.sample().busy is busy


def test_memory_pressure_uses_tighter_host_or_container_limit(tmp_path, monkeypatch):
    monitor, proc, cg, clock = _os_fixture(tmp_path, monkeypatch)
    (cg / "memory.current").write_text("950\n")
    assert monitor.sample().pressure_metric == "memory"
    clock[0] += 1
    (cg / "memory.current").write_text("890\n")
    assert monitor.sample().busy
    clock[0] += 1
    (cg / "memory.current").write_text("500\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 50 kB\n")
    assert monitor.sample().busy
    clock[0] += 1
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    assert not monitor.sample().busy


def test_host_average_is_telemetry_and_runnable_tasks_are_admission(
    tmp_path, monkeypatch
):
    monitor, proc, _, clock = _os_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("nebula.v3.host_load.os.getloadavg", lambda: (2, 2, 2))
    (proc / "stat").write_text(
        "cpu 100 0 100 800 0 0 0 0 0 0\nprocs_running 0\nprocs_blocked 27\n"
    )
    sample = monitor.sample()
    assert sample.load_average_ratio == 4.0
    assert sample.runnable_ratio == 0
    assert not sample.busy
    clock[0] += 1
    (proc / "stat").write_text(
        "cpu 200 0 200 1600 0 0 0 0 0 0\nprocs_running 7\nprocs_blocked 27\n"
    )
    assert monitor.sample().pressure_metric == "load"


def test_backup_disk_pressure_does_not_hold_admission(tmp_path, monkeypatch):
    monitor, proc, cg, _ = _os_fixture(tmp_path, monkeypatch)
    (proc / "pressure").mkdir()
    (proc / "pressure/io").write_text(
        "some avg10=91.00 avg60=88.00 avg300=80.00 total=1\n"
    )
    (proc / "stat").write_text(
        "cpu 100 0 100 800 0 0 0 0 0 0\nprocs_running 0\nprocs_blocked 27\n"
    )
    monkeypatch.setattr("nebula.v3.host_load.os.getloadavg", lambda: (30, 30, 30))
    (cg / "io.pressure").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    sample = monitor.sample()
    assert sample.host_io_pressure_ratio == 0.91
    assert sample.load_average_ratio == 60.0
    assert sample.io_pressure_ratio == 0
    assert not sample.busy


def test_default_io_pressure_uses_core_cgroup(tmp_path):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/mercury/agents/core\n")
    monitor = HostLoadMonitor(proc=proc)
    assert monitor._io_cgroup == Path("/sys/fs/cgroup/mercury/agents/core")


def test_missing_optional_measurements_do_not_strand_work(tmp_path, monkeypatch):
    monitor = HostLoadMonitor(proc=tmp_path / "missing", cgroup=tmp_path / "missing")

    def unavailable():
        raise OSError("synthetic unsupported counter")

    monkeypatch.setattr("nebula.v3.host_load.os.getloadavg", unavailable)
    sample = monitor.sample()
    assert not sample.measurements_available and not sample.busy
    assert sample.cpu_ratio is None and sample.memory_available_ratio is None


async def test_concurrent_refresh_is_shared_and_survives_waiter_cancellation(
    tmp_path, monkeypatch
):
    monitor = HostLoadMonitor(proc=tmp_path, cgroup=tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    sample = HostLoadSnapshot(time.monotonic(), 4, 0.1, 0.5, False, None, True)

    def read():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        monitor._cached = sample
        return sample

    monkeypatch.setattr(monitor, "sample", read)
    gate = HostLoadGate(monitor)

    async def exercise():
        tasks = [asyncio.create_task(gate.snapshot()) for _ in range(24)]
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            tasks[0].cancel()
            release.set()
            values = await asyncio.gather(*tasks, return_exceptions=True)
            assert isinstance(values[0], asyncio.CancelledError)
            assert all(value is sample for value in values[1:])
            assert len(calls) == 1
        finally:
            release.set()

    (await exercise())


async def test_pressure_wait_is_fifo_and_cancellation_does_not_leak():
    monitor = ManualMonitor()
    monitor.interval = 2
    gate = HostLoadGate(monitor)
    started = []

    async def start(i):
        await gate.wait()
        started.append(i)

    tasks = [asyncio.create_task(start(i)) for i in range(6)]
    await asyncio.sleep(0.04)
    assert started == []
    tasks[0].cancel()
    monitor.recover()
    gate.wake()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 0.5)
    assert started == [1, 2, 3, 4, 5]
    assert not gate._waiters


async def test_refresh_failure_is_observed_after_every_waiter_cancels(
    tmp_path, monkeypatch
):
    monitor = HostLoadMonitor(proc=tmp_path, cgroup=tmp_path)
    entered, release = threading.Event(), threading.Event()
    diagnostics = []

    def read():
        entered.set()
        assert release.wait(3)
        raise RuntimeError("synthetic sampler failure")

    monkeypatch.setattr(monitor, "sample", read)
    monkeypatch.setattr(
        "nebula.v3.diagnostics.record_diagnostic",
        lambda *args, **kwargs: diagnostics.append((args, kwargs)),
    )
    gate = HostLoadGate(monitor)

    async def exercise():
        waiter = asyncio.create_task(gate.snapshot())
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release.set()
            for _ in range(200):
                if diagnostics:
                    break
                await asyncio.sleep(0.005)
            assert len(diagnostics) == 1
            args, fields = diagnostics[0]
            assert args[:3] == ("error", "host_load", "host_load.refresh.failed")
            assert str(fields["exception"]) == "synthetic sampler failure"
        finally:
            release.set()

    (await exercise())


@pytest.mark.parametrize("entry", ["direct", "butler"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_core_queues_new_chats_and_butler_jobs_then_recovers_or_cancels(
    tmp_path, monkeypatch, entry, cancel
):
    store = NebulaStore(tmp_path / "core.db")
    (await store.create(Engagement(id="project", name="Synthetic host capacity")))
    (
        await store.create(
            ProviderProfile(
                id="provider",
                name="Synthetic provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
            )
        )
    )

    class Provider(ApiChatProvider):
        calls = 0

        async def complete(self, request):
            if request.metadata.get("operation") != "conversation_naming":
                self.calls += 1
            return await super().complete(request)

    provider = Provider("provider")
    monkeypatch.setattr("nebula.v3.chat.provider_from_profile", lambda _: provider)
    monitor = ManualMonitor()
    gate = HostLoadGate(monitor)
    app = create_app(
        store,
        auth_token="synthetic-host-token",
        execution_data_root=tmp_path,
        host_load_gate=gate,
    )
    work = app.state.control_assistant_work
    assert work.chat.provider_scheduler.host_load is gate
    assert app.state.harness_runtime_service.host_load is gate
    assert app.state.mission_service.host_load is gate

    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://core.test/api/v1",
                headers={"Authorization": "Bearer synthetic-host-token"},
            ) as client:
                pending = None
                if entry == "direct":
                    pending = asyncio.create_task(
                        client.post(
                            "/chat/completions",
                            json={
                                "engagement_id": "project",
                                "provider_id": "provider",
                                "model": "model-a",
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "Synthetic host capacity check",
                                    }
                                ],
                                "include_knowledge": False,
                                "stream": True,
                            },
                        )
                    )
                else:
                    result = await work.start_chat(
                        "project",
                        {
                            "backend": "provider",
                            "profile_id": "provider",
                            "model": "model-a",
                            "prompt": "Synthetic host capacity check",
                            "settings": {"include_knowledge": False},
                        },
                    )
                    assert result["status"] == "queued"
                turn = None
                for _ in range(300):
                    rows = await store.list_entities(ChatTurn, engagement_id="project")
                    if rows and rows[0].admission_reason:
                        turn = rows[0]
                        break
                    if pending is not None and pending.done():
                        response = await pending
                        pytest.fail(
                            f"chat ended instead of deferring: {response.status_code} {response.text[:300]}"
                        )
                    await asyncio.sleep(0.01)
                assert turn is not None and turn.status.value == "queued"
                assert "host pressure: CPU" in turn.admission_reason
                assert provider.calls == 0
                assert (await client.get("/host-load")).json()["busy"] is True
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://core.test/api/v1",
                ) as anonymous:
                    assert (await anonymous.get("/host-load")).status_code == 401
                assert (await work.catalog("project"))["host_admission"]["calibration"][
                    "busy"
                ] is True
                gateway = SettingsAssistantApi(app, "synthetic-host-token")
                assert (
                    gateway.schema(path="host-load", method="GET")["path"]
                    == "/api/v1/host-load"
                )
                assert (
                    await gateway.request("project", method="GET", path="host-load")
                )["result"]["busy"] is True
                # Durable state survives a second connection while queued.
                reopened = NebulaStore(tmp_path / "core.db")
                try:
                    assert (
                        await reopened.get(ChatTurn, turn.id)
                    ).status.value == "queued"
                finally:
                    (await reopened.database.engine.dispose())
                if cancel:
                    stopped = await client.post(f"/chat/turns/{turn.id}/cancel")
                    assert stopped.status_code == 200, stopped.text
                monitor.recover()
                if pending is not None:
                    response = await asyncio.wait_for(pending, 8)
                    assert response.status_code == 200, response.text
                for _ in range(300):
                    current = await store.get(ChatTurn, turn.id)
                    if current.status.value in {"complete", "cancelled", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                assert current.status.value == (
                    "cancelled" if cancel else "complete"
                ), current.error
                assert provider.calls == (0 if cancel else 1)
                if not cancel:
                    assert current.admission_reason is None
                    assert (
                        len(
                            (
                                await store.list_entities(
                                    ChatMessage, engagement_id="project"
                                )
                            )
                        )
                        == 2
                    )

    try:
        (await exercise())
    finally:
        (await store.database.engine.dispose())


@pytest.mark.parametrize("cancel", [False, True])
async def test_harness_waits_before_opening_runtime(tmp_path, cancel):
    store, project, profile, _, adapter, runtime = await _runtime(tmp_path)
    monitor = ManualMonitor()
    runtime.host_load = HostLoadGate(monitor)

    async def exercise():
        _, _, turn = await runtime.prepare_chat(
            engagement_id=project.id,
            profile_id=profile.id,
            model="test-model",
            prompt="Synthetic host admission",
            chat_session_id=None,
            harness_session_id=None,
            mcp_server_ids=[],
            include_knowledge=False,
        )
        task = await runtime.start_chat_turn(turn.id)
        try:
            for _ in range(200):
                if (await store.get(HarnessTurn, turn.id)).metadata.get(
                    "host_load_wait"
                ):
                    break
                await asyncio.sleep(0.01)
            assert not adapter.opens
            assert (await store.get(HarnessTurn, turn.id)).status.value == "queued"
            if cancel:
                await runtime.cancel_turn(turn.id, reason="Synthetic cancellation")
            monitor.recover()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)
            assert bool(adapter.opens) is (not cancel)
            assert (await store.get(HarnessTurn, turn.id)).status.value == (
                "cancelled" if cancel else "complete"
            )
        finally:
            await runtime.shutdown()

    (await exercise())


async def test_mission_uses_same_calibration_without_interrupting_active_work(tmp_path):
    store = NebulaStore(tmp_path / "missions.db")
    project = await store.create(Engagement(name="Synthetic mission"))
    profile = await _profile(store)
    provider = RecordingProvider(profile)
    monitor = ManualMonitor()
    gate = HostLoadGate(monitor)

    async def exercise():
        gates = {}
        stats = {"active": 0, "max_active": 0, "resumed": 0}
        service = MissionService(
            store,
            provider_factory=lambda _: provider,
            runtime_factory=_gated_runtime_factory(store, gates, stats),
        )
        service.host_load = gate
        run = await service.start_mission(
            engagement_id=project.id,
            objective="Synthetic admission",
            provider_id=profile.id,
            model="security-model",
            budget=RunBudget(max_tool_calls=0, max_delegation_depth=0),
        )
        try:
            for _ in range(200):
                if (await store.get(AgentRun, run.id)).metadata.get("host_load_wait"):
                    break
                await asyncio.sleep(0.01)
            assert (
                stats["active"] == 0
                and (await store.get(AgentRun, run.id)).status == RunStatus.QUEUED
            )
            assert run.id in service._scheduled_tasks and run.id not in service._tasks
            monitor.recover()
            for _ in range(200):
                if stats["active"]:
                    break
                await asyncio.sleep(0.01)
            assert stats["active"] == 1
            monitor.value = replace(
                monitor.value,
                cpu_ratio=0.95,
                busy=True,
                reason="Waiting for host CPU capacity",
            )
            await asyncio.sleep(0.03)
            assert stats["active"] == 1
            gates[run.id].set()
            await service._tasks[run.id]
            assert (await store.get(AgentRun, run.id)).status == RunStatus.COMPLETE
        finally:
            await service.shutdown()

    (await exercise())


def test_idle_cpu_io_wait_is_not_cpu_saturation(tmp_path, monkeypatch):
    monitor, proc, cg, clock = _os_fixture(tmp_path, monkeypatch)
    (proc / "pressure").mkdir()
    (proc / "pressure/io").write_text(
        "some avg10=10.00 avg60=20.00 avg300=10.00 total=123\n"
    )
    (cg / "io.pressure").write_text(
        "some avg10=90.00 avg60=20.00 avg300=10.00 total=123\n"
    )
    (proc / "stat").write_text("cpu 100 0 100 800 0 0 0 0 0 0\n")
    monkeypatch.setattr("nebula.v3.host_load.os.getloadavg", lambda: (0.1, 0.1, 0.1))
    monitor.sample()
    clock[0] += 1
    (cg / "cpu.stat").write_text("usage_usec 5000\n")
    (proc / "stat").write_text("cpu 100 0 100 1600 200 0 0 0 0 0\n")
    sample = monitor.sample()
    assert sample.cpu_ratio == pytest.approx(0.01)
    assert sample.io_pressure_ratio == 0.9
    assert sample.host_io_pressure_ratio == 0.1
    assert sample.load_average_ratio == 0.2
    assert sample.pressure_metric == "io"
    assert sample.busy


pytestmark = pytest.mark.anyio
