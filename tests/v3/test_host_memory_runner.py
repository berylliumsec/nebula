"""Host commands run only inside a protected memory-runner/v1 group.

The unit tests drive Core's admission, settlement and recovery against an
in-process fake runner. The kernel tests at the end run only when
``NEBULA_TEST_HOST_MEMORY_RUNNER_SOCKET`` names a disposable root runner
(``host_memory_runner.py``) whose client user is the test user.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from nebula.v3 import memory_recovery
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.automation_runtime import (
    AutomationRuntimeManager,
    ManagedHostRuntimeSession,
)
from nebula.v3.domain import AutomationProjectPolicy, Engagement
from nebula.v3.host_memory_client import (
    HOST_RUNNER_PROFILE_ID,
    HostMemoryRunner,
    HostRunnerError,
    host_group_id,
)
from nebula.v3.host_memory_runner import Runner, RunnerError, group_name
from nebula.v3.memory_budget import HostCommandSettingsUpdate, MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetUpdate,
    MemoryEnforcementUnavailable,
    MemoryLimits,
    MemoryRunnerCapabilities,
    MemoryRunningUpdate,
)
from nebula.v3.memory_recovery import MemoryReconciler
from nebula.v3.memory_runtime import MemoryComputeController
from nebula.v3.storage import NebulaStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeHostRunner:
    """Implements the client surface Core uses, backed by an in-memory group."""

    profile = None
    memory_profile_id = HOST_RUNNER_PROFILE_ID

    def __init__(self):
        self.groups: dict[str, dict] = {}
        self.removed: list[str] = []
        self.fail_create = False
        self.fail_remove = False
        self.authority = "host-runner:" + "a" * 64

    def memory_fingerprint(self) -> str:
        return "b" * 64

    async def memory_identity(self) -> str:
        return self.authority

    async def available(self):
        return True, "fake runner"

    async def capabilities(self) -> MemoryRunnerCapabilities:
        return MemoryRunnerCapabilities(
            environment="local",
            profile_id=HOST_RUNNER_PROFILE_ID,
            kernel_enforced=True,
            protected_process_tree=True,
            explicit_swap=True,
            pressure_threshold=True,
            live_update=True,
            capacity_bytes=16 * GIB,
            detail="fake",
        )

    def _report(self, job_id: str) -> dict:
        group = self.groups[job_id]
        return {"job_id": job_id, "group_id": host_group_id(job_id), **group}

    async def create(self, job_id: str, limits: MemoryLimits) -> dict:
        if self.fail_create:
            # The group may exist even though the response was lost.
            self.groups[job_id] = {}
            raise HostRunnerError("failed", "lost create response")
        self.groups[job_id] = {
            "max_bytes": limits.max_bytes,
            "swap_bytes": limits.swap_bytes,
            "high_bytes": limits.high_bytes,
            "current_bytes": 0,
            "peak_bytes": 0,
            "oom_kill_count": 0,
            "populated": False,
        }
        return self._report(job_id)

    async def observe(self, job_id: str):
        if job_id not in self.groups:
            return None
        return self._report(job_id)

    async def update(self, job_id: str, limits: MemoryLimits) -> dict:
        self.groups[job_id].update(
            max_bytes=limits.max_bytes,
            swap_bytes=limits.swap_bytes,
            high_bytes=limits.high_bytes,
        )
        return self._report(job_id)

    async def remove(self, job_id: str) -> dict:
        if self.fail_remove:
            raise HostRunnerError("unverified", "group still populated")
        self.removed.append(job_id)
        if job_id not in self.groups:
            return {
                "absent": True,
                "group_id": host_group_id(job_id),
                "group_empty": True,
            }
        report = self._report(job_id)
        del self.groups[job_id]
        report.update(group_empty=True, populated=False, current_bytes=0)
        return report


@pytest.fixture
async def budgets(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "NEBULA_HOST_MEMORY_RUNNER_SOCKET", str(tmp_path / "absent-runner.sock")
    )
    store = NebulaStore(tmp_path / "core.db")
    await store.create(Engagement(id="project", name="Synthetic host project"))
    admin = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
    )
    service = MemoryBudgetService(store.database, administrator=lambda: admin)
    initial = await service.status()
    await service.save(
        MemoryBudgetUpdate(
            expected_revision=initial.revision,
            policy=initial.policy.model_copy(
                update={"default_job": MemoryLimits(max_bytes=256 * MIB)}
            ),
        ),
        actor_id="fixture",
    )
    controller = MemoryComputeController(service, tmp_path)
    try:
        yield store, controller
    finally:
        await store.database.dispose()


async def _open(controller, runner, job_id="session-a"):
    return await controller.open_host_job(
        runner,
        job_id=job_id,
        project_id="project",
        resource_kind="host_command_session",
        resource_id=job_id,
    )


async def test_host_job_verifies_limits_before_use_and_releases_after_empty_group(
    budgets,
):
    _store, controller = budgets
    runner = FakeHostRunner()
    handle = await _open(controller, runner)
    job = await controller.service.get_job("session-a")
    assert job.state == "running" and job.application_state == "verified"
    assert job.reserved_bytes == 256 * MIB
    assert job.launch_groups.group_id == host_group_id("session-a")
    assert job.launch_owner.runner_identity == runner.authority
    assert job.evidence.applied == MemoryLimits(max_bytes=256 * MIB)
    assert job.snapshot.profile_id == HOST_RUNNER_PROFILE_ID
    await handle.close()
    job = await controller.service.get_job("session-a")
    assert job.state == "finished" and job.outcome == "completed"
    assert job.reserved_bytes == 0 and job.evidence.group_empty
    assert runner.removed == ["session-a"]


async def test_kernel_oom_evidence_ends_the_worker_with_a_memory_outcome(budgets):
    _store, controller = budgets
    runner = FakeHostRunner()
    handle = await _open(controller, runner)
    runner.groups["session-a"].update(oom_kill_count=1, peak_bytes=256 * MIB)
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await controller.service.get_job("session-a")).state == "finished":
            break
    job = await controller.service.get_job("session-a")
    assert handle.ended
    assert job.outcome == "memory_limit_exceeded"
    assert job.evidence.oom_victim_confirmed and job.evidence.group_empty
    assert job.reserved_bytes == 0


async def test_lost_create_response_releases_only_after_verified_removal(budgets):
    _store, controller = budgets
    runner = FakeHostRunner()
    runner.fail_create = True
    with pytest.raises(MemoryEnforcementUnavailable, match="lost create"):
        await _open(controller, runner)
    job = await controller.service.get_job("session-a")
    assert runner.removed == ["session-a"] and "session-a" not in runner.groups
    assert job.reserved_bytes == 0 and job.state == "finished"


async def test_unverified_cleanup_keeps_capacity_charged(budgets):
    _store, controller = budgets
    runner = FakeHostRunner()
    handle = await _open(controller, runner)
    runner.fail_remove = True
    with pytest.raises(MemoryEnforcementUnavailable, match="still populated"):
        await handle.close()
    job = await controller.service.get_job("session-a")
    assert job.state == "uncertain" and job.reserved_bytes == 256 * MIB


async def test_dead_owner_group_is_removed_before_capacity_is_released(
    budgets, monkeypatch
):
    _store, controller = budgets
    runner = FakeHostRunner()
    handle = await _open(controller, runner)
    handle.monitor.cancel()
    monkeypatch.setattr(memory_recovery, "launch_owner_ended", lambda *_: True)

    async def resolve(job):
        assert job.snapshot.profile_id == HOST_RUNNER_PROFILE_ID
        return runner

    reconciler = MemoryReconciler(controller.service, resolve)
    assert await reconciler.once() == ["session-a"]
    job = await controller.service.get_job("session-a")
    assert job.state == "finished" and job.outcome == "worker_lost"
    assert job.evidence.group_empty and job.reserved_bytes == 0
    assert runner.removed == ["session-a"]


async def test_dead_owner_cleanup_waits_for_the_original_runner_authority(
    budgets, monkeypatch
):
    _store, controller = budgets
    runner = FakeHostRunner()
    handle = await _open(controller, runner)
    handle.monitor.cancel()
    monkeypatch.setattr(memory_recovery, "launch_owner_ended", lambda *_: True)
    runner.authority = "host-runner:" + "c" * 64

    async def resolve(_job):
        return runner

    assert await MemoryReconciler(controller.service, resolve).once() == []
    job = await controller.service.get_job("session-a")
    assert job.reserved_bytes == 256 * MIB and runner.removed == []


async def test_running_limit_change_is_read_back_from_the_host_group(budgets):
    _store, controller = budgets
    runner = FakeHostRunner()
    handle = await _open(controller, runner)
    handle.monitor.cancel()
    job = await controller.service.get_job("session-a")
    raised = MemoryLimits(max_bytes=512 * MIB, high_bytes=384 * MIB)
    updated = await controller.apply_running(
        "session-a",
        # Adding a pressure threshold can throttle the job, so it needs consent.
        MemoryRunningUpdate(
            expected_revision=job.revision,
            limits=raised,
            acknowledge_possible_termination=True,
        ),
        "fixture",
    )
    assert updated.evidence.applied == raised
    assert runner.groups["session-a"]["max_bytes"] == 512 * MIB


async def test_host_session_without_runner_is_held_and_names_the_service(
    budgets, tmp_path
):
    store, controller = budgets
    await store.create(
        AutomationProjectPolicy(
            id=str(uuid5(NAMESPACE_URL, "nebula:automation-policy:project")),
            engagement_id="project",
            execution_mode="host",
            host_access_acknowledged=True,
        )
    )
    manager = AutomationRuntimeManager(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path,
        workspace_resolver=lambda _: tmp_path,
        runtime_image="registry.invalid/nebula-automation@sha256:" + "a" * 64,
    )
    manager.memory_compute = controller
    with pytest.raises(MemoryEnforcementUnavailable, match="nebula-memory-runner"):
        await manager._get_or_create_session(
            engagement_id="project", owner_kind="chat", owner_id="chat"
        )
    jobs = await controller.service.list_jobs()
    assert [job.state for job in jobs] == ["blocked"]


async def test_missing_runner_reports_unsupported_local_capability(tmp_path):
    capability = await HostMemoryRunner(tmp_path / "absent.sock").capabilities()
    assert capability.environment == "local"
    assert not capability.kernel_enforced and not capability.protected_process_tree
    assert "nebula-memory-runner" in capability.detail


def test_runner_group_names_match_core_launch_group_identity():
    assert group_name("session-a") == host_group_id("session-a")
    assert len(host_group_id("session-a")) == 64


@pytest.mark.parametrize(
    "request_fields",
    [
        {"max_bytes": 1, "swap_bytes": 0},
        {"max_bytes": 64 * MIB, "swap_bytes": -1},
        {"max_bytes": 64 * MIB, "swap_bytes": 0, "high_bytes": 128 * MIB},
        {"max_bytes": "64", "swap_bytes": 0},
    ],
)
def test_runner_rejects_unbounded_or_inconsistent_limits(request_fields):
    with pytest.raises(RunnerError):
        Runner._limits(request_fields)


# -- kernel tests against a disposable root runner -------------------------

REAL_SOCKET = os.environ.get("NEBULA_TEST_HOST_MEMORY_RUNNER_SOCKET")
real = pytest.mark.skipif(
    not REAL_SOCKET, reason="set NEBULA_TEST_HOST_MEMORY_RUNNER_SOCKET to a runner"
)


async def _admitted_run(session, process_id: str, command: str) -> tuple[int, str]:
    await session.admit(process_id, execution_id=process_id, labels={})
    result = await _run(session, command, process_id)
    session.settle_after_exit(process_id)
    return result


async def _until_finished(controller, job_id: str, seconds: float = 10) -> None:
    for _ in range(int(seconds * 10)):
        if (await controller.service.get_job(job_id)).state == "finished":
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{job_id} still holds its reservation")


async def _run(session, command: str, process_id: str = "p") -> tuple[int, str]:
    process = await session.run(process_id, command, ".")
    # wait() releases pipes a detached descendant still holds, as Core does.
    code = await process.wait()
    output = await process.stdout.read()
    error = await process.stderr.read()
    return code, (output + error).decode()


@real
async def test_real_host_session_contains_commands_and_releases_after_removal(
    budgets, tmp_path, monkeypatch
):
    monkeypatch.setenv("NEBULA_HOST_MEMORY_RUNNER_SOCKET", REAL_SOCKET)
    store, controller = budgets
    await store.create(
        AutomationProjectPolicy(
            id=str(uuid5(NAMESPACE_URL, "nebula:automation-policy:project")),
            engagement_id="project",
            execution_mode="host",
            host_access_acknowledged=True,
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = AutomationRuntimeManager(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path,
        workspace_resolver=lambda _: workspace,
        runtime_image="registry.invalid/nebula-automation@sha256:" + "a" * 64,
    )
    manager.memory_compute = controller
    managed = await manager._get_or_create_session(
        engagement_id="project", owner_kind="chat", owner_id="chat"
    )
    session = managed.backend
    assert isinstance(session, ManagedHostRuntimeSession)

    code, output = await _admitted_run(
        session, "a", "echo hi > made.txt; cat /proc/self/cgroup"
    )
    assert code == 0 and output.strip().endswith(host_group_id("command-a"))
    assert (workspace / "made.txt").read_text() == "hi\n"
    await _until_finished(controller, "command-a")

    code, output = await _admitted_run(
        session,
        "b",
        "echo $$ > /sys/fs/cgroup/cgroup.procs || echo moved-denied; "
        "sudo -n true 2>/dev/null || echo sudo-denied; "
        "grep -E '^(NoNewPrivs|CapEff)' /proc/self/status",
    )
    assert "moved-denied" in output and "sudo-denied" in output
    assert "NoNewPrivs:\t1" in output and "CapEff:\t0000000000000000" in output

    # A detached process keeps its command's reservation after the command exits.
    code, output = await _admitted_run(
        session, "c", "(setsid sleep 300 &); echo detached"
    )
    assert code == 0 and "detached" in output
    await asyncio.sleep(1)
    job = await controller.service.get_job("command-c")
    assert job.state == "running" and job.reserved_bytes == 256 * MIB
    assert job.labels["detached"] is True

    await manager.close_session(managed.entity.id)
    job = await controller.service.get_job("command-c")
    assert job.state == "finished" and job.outcome == "completed"
    assert job.reserved_bytes == 0 and job.evidence.group_empty


@real
async def test_real_oversized_command_has_kernel_oom_evidence(budgets, tmp_path):
    _store, controller = budgets
    runner = HostMemoryRunner(Path(REAL_SOCKET))
    handle = await _open(controller, runner, job_id="session-oom")
    process = await runner.spawn(
        "session-oom",
        ["/usr/bin/python3", "-c", "b = bytearray(600 * 2**20)"],
        str(tmp_path),
        {"PATH": "/usr/bin:/bin"},
    )
    assert await process.wait() == 137
    await process.close()
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await controller.service.get_job("session-oom")).state == "finished":
            break
    job = await controller.service.get_job("session-oom")
    assert handle.ended and job.outcome == "memory_limit_exceeded"
    assert job.evidence.oom_victim_confirmed and job.reserved_bytes == 0
    assert await runner.observe("session-oom") is None


def test_linux_deb_ships_the_runner_and_its_per_user_unit():
    import json

    root = Path(__file__).resolve().parents[2]
    deb = json.loads((root / "ui/src-tauri/tauri.conf.json").read_text())["bundle"][
        "linux"
    ]["deb"]
    files = deb["files"]
    script = "/usr/lib/nebula/memory-runner/host_memory_runner.py"
    unit_path = "/usr/lib/systemd/system/nebula-memory-runner@.service"
    assert files[script] == "../../src/nebula/v3/host_memory_runner.py"
    unit = (root / "ui/src-tauri" / files[unit_path]).resolve().read_text()
    assert f"/usr/bin/python3 -I {script} --client-user %i" in unit
    assert "--socket /run/nebula-memory-runner/%i.sock" in unit
    assert "Delegate=memory pids" in unit and "KillMode=control-group" in unit
    assert "ProtectHome" not in unit and "ProtectSystem" not in unit
    assert {"python3 (>= 3.9)", "util-linux"} <= set(deb["depends"])


@real
async def test_real_burst_of_parallel_commands_all_admit(budgets, tmp_path):
    """Parallel spawns once raced the privilege check and overflowed the backlog."""
    _store, controller = budgets
    runner = HostMemoryRunner(Path(REAL_SOCKET))
    handle = await _open(controller, runner, job_id="session-burst")
    try:

        async def one():
            process = await runner.spawn(
                "session-burst", ["/bin/true"], str(tmp_path), {"PATH": "/usr/bin:/bin"}
            )
            code = await process.wait()
            await process.close()
            return code

        assert await asyncio.gather(*(one() for _ in range(200))) == [0] * 200
    finally:
        await handle.close()
    job = await controller.service.get_job("session-burst")
    assert job.state == "finished" and job.reserved_bytes == 0


# -- Core-wide Host command queue (docs/design/host-command-queue.md) --------


@pytest.fixture
async def host_queue(budgets, monkeypatch):
    store, controller = budgets
    await store.create(
        AutomationProjectPolicy(
            id=str(uuid5(NAMESPACE_URL, "nebula:automation-policy:project")),
            engagement_id="project",
            execution_mode="host",
            host_access_acknowledged=True,
        )
    )
    runner = FakeHostRunner()
    monkeypatch.setattr(HostMemoryRunner, "from_environment", lambda: runner)
    return store, controller, runner


async def _limit(controller, running_at_once: int):
    current = await controller.service.host_command_settings()
    return await controller.service.save_host_command_settings(
        HostCommandSettingsUpdate(
            expected_revision=current.revision, running_at_once=running_at_once
        ),
        actor_id="fixture",
    )


def _host_manager(store, controller, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = AutomationRuntimeManager(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path,
        workspace_resolver=lambda _: workspace,
        runtime_image="registry.invalid/nebula-automation@sha256:" + "a" * 64,
    )
    manager.memory_compute = controller
    return manager


async def _host_session(manager, owner):
    return (
        await manager._get_or_create_session(
            engagement_id="project", owner_kind="chat", owner_id=owner
        )
    ).backend


async def _queued(controller, job_id):
    for _ in range(50):
        await asyncio.sleep(0.05)
        try:
            if (await controller.service.get_job(job_id)).state == "queued":
                return
        except Exception:
            continue
    raise AssertionError(f"{job_id} never queued")


async def test_host_session_reserves_nothing_until_a_command_is_admitted(
    host_queue, tmp_path
):
    store, controller, _runner = host_queue
    manager = _host_manager(store, controller, tmp_path)
    await _host_session(manager, "chat-a")
    assert await controller.service.list_jobs() == []
    await manager.shutdown()


async def test_running_at_once_limit_queues_and_admits_as_slots_free(
    host_queue, tmp_path
):
    store, controller, runner = host_queue
    await _limit(controller, 1)
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    first = await session.admit("a", execution_id="a", labels={"command": "make"})
    positions = []

    async def report(position):
        positions.append((position.position, position.running, position.limit))

    waiting = asyncio.create_task(
        session.admit("b", execution_id="b", labels={}, on_queued=report)
    )
    await _queued(controller, "command-b")
    view = await controller.service.host_command_view()
    assert [item.job_id for item in view.running] == ["command-a"]
    assert [(item.job_id, item.position) for item in view.queued] == [("command-b", 1)]
    assert view.running[0].command == "make"
    session.settle_after_exit("a")
    second = await asyncio.wait_for(waiting, timeout=5)
    assert first.ended and not second.ended
    assert positions[0] == (1, 1, 1)
    assert (await controller.service.get_job("command-a")).reserved_bytes == 0
    await manager.shutdown()
    assert runner.groups == {}


async def test_fair_order_admits_the_project_with_fewer_running_first(budgets):
    store, controller = budgets
    await store.create(Engagement(id="other", name="Other synthetic project"))
    await _limit(controller, 2)
    runner = FakeHostRunner()
    busy = await controller.open_host_job(
        runner,
        job_id="command-busy",
        project_id="project",
        resource_kind="host_command",
        resource_id="busy",
    )
    capability = await runner.capabilities()
    for job_id, project in (("command-older", "project"), ("command-newer", "other")):
        await controller.service.create_job(
            job_id,
            project_id=project,
            resource_kind="host_command",
            resource_id=job_id,
            capabilities=capability,
        )
        await asyncio.sleep(0.01)
    view = await controller.service.host_command_view()
    assert [item.job_id for item in view.queued] == ["command-newer", "command-older"]
    # The older command from the busy project may not jump the line.
    older = await controller.service.reserve("command-older", capability)
    assert older.state == "queued" and "1 Host command(s) ahead" in older.detail
    newer = await controller.service.reserve("command-newer", capability)
    assert newer.state == "reserved"
    await busy.close()


async def test_stale_queued_rows_are_skipped_and_reaped(budgets):
    from datetime import timedelta

    from nebula.v3.database import MemoryJobRow
    from nebula.v3.domain import utc_now

    store, controller = budgets
    capability = await FakeHostRunner().capabilities()
    for job_id in ("command-dead", "command-live"):
        await controller.service.create_job(
            job_id,
            project_id="project",
            resource_kind="host_command",
            resource_id=job_id,
            capabilities=capability,
        )
    async with store.database.session() as session:
        row = await session.get(MemoryJobRow, "command-dead")
        row.updated_at = utc_now() - timedelta(minutes=5)
    view = await controller.service.host_command_view()
    assert [item.job_id for item in view.queued] == ["command-live"]
    assert await controller.service.cancel_stale_queued() == ["command-dead"]
    dead = await controller.service.get_job("command-dead")
    assert dead.state == "finished" and dead.outcome == "cancelled"


async def test_operator_cancel_of_a_queued_command_stops_its_wait(host_queue, tmp_path):
    from nebula.v3.memory_runtime import HostCommandCancelled

    store, controller, _runner = host_queue
    await _limit(controller, 1)
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})
    waiting = asyncio.create_task(session.admit("b", execution_id="b", labels={}))
    await _queued(controller, "command-b")
    cancelled = await controller.service.request_host_command_cancel(
        "command-b", actor_id="operator"
    )
    assert cancelled.state == "finished" and cancelled.outcome == "cancelled"
    with pytest.raises(HostCommandCancelled, match="never started"):
        await asyncio.wait_for(waiting, timeout=5)
    await manager.shutdown()


async def test_cancel_api_stops_a_running_command_by_removing_its_group(
    host_queue, tmp_path
):
    from nebula.v3.memory_api import MemoryApi

    store, controller, runner = host_queue
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})
    api = object.__new__(MemoryApi)
    api.service, api.host_runner = controller.service, runner
    item = await api.cancel_host_command("command-a", "operator")
    assert item.cancel_requested and runner.removed == ["command-a"]
    assert await session.cancel_requested("a")
    await manager.shutdown()


async def test_detached_processes_keep_the_reservation_until_the_group_empties(
    host_queue, tmp_path, monkeypatch
):
    store, controller, runner = host_queue
    monkeypatch.setattr(ManagedHostRuntimeSession, "DETACHED_CHECK_SECONDS", 0.05)
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})
    runner.groups["command-a"]["populated"] = True  # `nohup server &` outlived it
    session.settle_after_exit("a")
    await asyncio.sleep(0.2)
    job = await controller.service.get_job("command-a")
    assert job.state == "running" and job.labels["detached"] is True
    runner.groups["command-a"]["populated"] = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        if (await controller.service.get_job("command-a")).state == "finished":
            break
    job = await controller.service.get_job("command-a")
    assert job.state == "finished" and job.reserved_bytes == 0
    await manager.shutdown()


async def test_unreadable_group_keeps_the_reservation(
    host_queue, tmp_path, monkeypatch
):
    store, controller, runner = host_queue
    monkeypatch.setattr(ManagedHostRuntimeSession, "DETACHED_CHECK_SECONDS", 0.05)
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})

    async def unreadable(_job_id):
        raise HostRunnerError("failed", "runner restarting")

    runner.observe = unreadable
    session.settle_after_exit("a")
    await asyncio.sleep(0.2)
    assert (await controller.service.get_job("command-a")).state == "running"
    await manager.shutdown()


async def test_host_command_settings_are_bounded_and_revision_checked(budgets):
    from pydantic import ValidationError

    from nebula.v3.memory_policy import MemoryBudgetConflict

    _store, controller = budgets
    assert (await controller.service.host_command_settings()).running_at_once == 8
    saved = await _limit(controller, 3)
    assert saved.running_at_once == 3 and saved.revision == 2
    with pytest.raises(MemoryBudgetConflict):
        await controller.service.save_host_command_settings(
            HostCommandSettingsUpdate(expected_revision=1, running_at_once=4),
            actor_id="fixture",
        )
    with pytest.raises(ValidationError):
        HostCommandSettingsUpdate(expected_revision=2, running_at_once=65)


async def test_stop_succeeds_when_the_worker_removed_the_group_first(
    host_queue, tmp_path
):
    from nebula.v3.memory_api import MemoryApi

    store, controller, runner = host_queue
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})
    real_remove = runner.remove

    async def racing_remove(job_id):
        await real_remove(job_id)  # the worker's cleanup won the race
        raise HostRunnerError("failed", "FileNotFoundError: cgroup.events")

    runner.remove = racing_remove
    api = object.__new__(MemoryApi)
    api.service, api.host_runner = controller.service, runner
    item = await api.cancel_host_command("command-a", "operator")
    assert item.cancel_requested
    await manager.shutdown()


async def test_operator_stop_settles_as_cancelled_not_worker_lost(host_queue, tmp_path):
    store, controller, runner = host_queue
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})
    await controller.service.request_host_command_cancel("command-a", actor_id="op")
    await runner.remove("command-a")
    for _ in range(50):
        await asyncio.sleep(0.1)
        job = await controller.service.get_job("command-a")
        if job.state == "finished":
            break
    assert job.state == "finished" and job.outcome == "cancelled"
    assert job.reserved_bytes == 0
    await manager.shutdown()


async def test_stopped_detached_command_settles_as_cancelled(
    host_queue, tmp_path, monkeypatch
):
    store, controller, runner = host_queue
    monkeypatch.setattr(ManagedHostRuntimeSession, "DETACHED_CHECK_SECONDS", 0.05)
    manager = _host_manager(store, controller, tmp_path)
    session = await _host_session(manager, "chat-a")
    await session.admit("a", execution_id="a", labels={})
    runner.groups["command-a"]["populated"] = True
    session.settle_after_exit("a")
    await asyncio.sleep(0.2)
    await controller.service.request_host_command_cancel("command-a", actor_id="op")
    runner.groups["command-a"]["populated"] = False  # the stop emptied the group
    for _ in range(40):
        await asyncio.sleep(0.05)
        job = await controller.service.get_job("command-a")
        if job.state == "finished":
            break
    assert job.outcome == "cancelled" and job.reserved_bytes == 0
    await manager.shutdown()
