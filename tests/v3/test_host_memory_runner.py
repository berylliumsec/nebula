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
from nebula.v3.memory_budget import MemoryBudgetService
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


async def _run(session, command: str) -> tuple[int, str]:
    process = await session.run("p", command, ".")
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
    job_id = session.memory_job.job_id
    group = host_group_id(job_id)

    code, output = await _run(session, "echo hi > made.txt; cat /proc/self/cgroup")
    assert code == 0 and output.strip().endswith(group)
    assert (workspace / "made.txt").read_text() == "hi\n"

    code, output = await _run(
        session,
        "echo $$ > /sys/fs/cgroup/cgroup.procs || echo moved-denied; "
        "sudo -n true 2>/dev/null || echo sudo-denied; "
        "grep -E '^(NoNewPrivs|CapEff)' /proc/self/status",
    )
    assert "moved-denied" in output and "sudo-denied" in output
    assert "NoNewPrivs:\t1" in output and "CapEff:\t0000000000000000" in output

    code, output = await _run(session, "(setsid sleep 300 &); echo detached")
    assert code == 0 and "detached" in output
    job = await controller.service.get_job(job_id)
    assert job.state == "running" and job.reserved_bytes == 256 * MIB

    await manager.close_session(managed.entity.id)
    job = await controller.service.get_job(job_id)
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


# -- Host session admission must not stall on idle reservations --------------


@pytest.fixture
async def full_budget(budgets, monkeypatch):
    """One Host session fills the aggregate budget, as 18 idle sessions did live."""
    store, controller = budgets
    status = await controller.service.status()
    await controller.service.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(
                update={
                    "default_job": MemoryLimits(max_bytes=8 * GIB),
                    "aggregate_bytes": 8 * GIB,
                }
            ),
        ),
        actor_id="fixture",
    )
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
    return await manager._get_or_create_session(
        engagement_id="project", owner_kind="chat", owner_id=owner
    )


async def test_waiting_host_admission_does_not_block_other_owners(
    full_budget, tmp_path
):
    store, controller, _runner = full_budget
    manager = _host_manager(store, controller, tmp_path)
    first = await _host_session(manager, "chat-a")
    waiting = asyncio.create_task(_host_session(manager, "chat-b"))
    try:
        for _ in range(50):
            await asyncio.sleep(0.05)
            jobs = await controller.service.list_jobs()
            if any(job.state == "queued" for job in jobs):
                break
        assert any(job.state == "queued" for job in jobs)
        # The owner that already has a session keeps working while chat-b waits.
        again = await asyncio.wait_for(_host_session(manager, "chat-a"), timeout=2)
        assert again is first
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    jobs = await controller.service.list_jobs()
    assert not any(job.state == "queued" for job in jobs)
    await manager.shutdown()


async def test_host_admission_wait_is_bounded_and_settles_the_queued_job(
    full_budget, tmp_path, monkeypatch
):
    from nebula.v3 import memory_runtime
    from nebula.v3.automation_runtime import AutomationRuntimeUnavailable

    store, controller, _runner = full_budget
    monkeypatch.setattr(memory_runtime, "QUEUED_ADMISSION_SECONDS", 0.3)
    manager = _host_manager(store, controller, tmp_path)
    await _host_session(manager, "chat-a")
    with pytest.raises(AutomationRuntimeUnavailable, match="memory budget"):
        await manager.session(
            engagement_id="project", owner_kind="chat", owner_id="chat-b"
        )
    jobs = {
        job.resource_kind + ":" + job.state
        for job in await controller.service.list_jobs()
    }
    assert "host_command_session:queued" not in jobs
    assert manager._session_lock.locked() is False
    await manager.shutdown()


async def test_idle_host_session_releases_its_reservation(full_budget, tmp_path):
    store, controller, runner = full_budget
    manager = _host_manager(store, controller, tmp_path)
    idle = await _host_session(manager, "chat-a")
    job_id = idle.backend.memory_job.job_id
    assert await manager.release_idle_host_sessions(idle_seconds=60) == []
    assert await manager.release_idle_host_sessions(idle_seconds=0) == [idle.entity.id]
    job = await controller.service.get_job(job_id)
    assert job.state == "finished" and job.reserved_bytes == 0
    assert runner.removed == [job_id]
    # The freed budget admits the next owner immediately.
    other = await asyncio.wait_for(_host_session(manager, "chat-b"), timeout=2)
    assert isinstance(other.backend, ManagedHostRuntimeSession)
    # The idle owner gets a fresh worker on its next command.
    assert await manager.release_idle_host_sessions(idle_seconds=0) == [other.entity.id]
    fresh = await asyncio.wait_for(_host_session(manager, "chat-a"), timeout=2)
    assert fresh.entity.id != idle.entity.id
    await manager.shutdown()


async def test_busy_host_session_is_not_released(full_budget, tmp_path):
    store, controller, _runner = full_budget
    manager = _host_manager(store, controller, tmp_path)
    managed = await _host_session(manager, "chat-a")
    managed.active_uses += 1
    assert await manager.release_idle_host_sessions(idle_seconds=0) == []
    managed.active_uses -= 1

    class Unfinished:
        final_task = asyncio.get_running_loop().create_future()

    managed.processes["p"] = Unfinished()
    assert await manager.release_idle_host_sessions(idle_seconds=0) == []
    managed.processes.pop("p").final_task.cancel()
    assert await manager.release_idle_host_sessions(idle_seconds=0) == [
        managed.entity.id
    ]
    await manager.shutdown()
