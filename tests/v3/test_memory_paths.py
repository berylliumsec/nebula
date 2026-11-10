"""Unsupported managed launchers hold before touching an unrestricted backend."""

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.automation_runtime import AutomationRuntimeManager
from nebula.v3.domain import (
    AutomationProjectPolicy,
    Engagement,
    SshEnvironment,
    ToolCallOrigin,
)
from nebula.v3.environments import build_ssh_tool_plugins
from nebula.v3 import environments
from nebula.v3.temporary_command_override import unmanaged_commands_allowed
from nebula.v3.harnesses import HarnessRuntimeService
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MemoryAdministratorLimits,
    MemoryEnforcementUnavailable,
)
from nebula.v3.memory_runtime import MemoryComputeController, managed_compute
from nebula.v3.notebooks import NotebookService
from nebula.v3.report_cpu_executor import ReportCpuExecutor
from nebula.v3.reporting import ReportRenderService
from nebula.v3.sandbox import (
    ContainerSandboxRunner,
    SandboxUnavailable,
    SandboxRequest,
    SandboxContainerUser,
    SandboxWorkspaceAccess,
    SandboxExecutionKind,
    SandboxNetwork,
    SandboxRootFilesystem,
)
from nebula.v3.storage import NebulaStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def memory(tmp_path, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "unsupported path attempted to spawn an unrestricted process"
        )

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    store = NebulaStore(tmp_path / "core.db")
    await store.create(Engagement(id="project", name="Synthetic launch paths"))
    admin = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
    )
    service = MemoryBudgetService(store.database, administrator=lambda: admin)
    controller = MemoryComputeController(service, tmp_path)
    try:
        yield store, controller
    finally:
        await store.database.dispose()


async def held(controller, kind):
    jobs = await controller.service.list_jobs()
    assert len(jobs) == 1 and jobs[0].state == "blocked"
    assert jobs[0].resource_kind == kind and jobs[0].reserved_bytes == 0


@pytest.mark.parametrize("owner", [False, True])
async def test_owner_mapped_compute_is_held_before_any_runner_operation(
    memory, tmp_path, owner
):
    _store, controller = memory
    request = SandboxRequest(
        image="registry.invalid/compute@sha256:" + "a" * 64,
        command=["synthetic-work"],
        workspace=tmp_path,
        workspace_access=SandboxWorkspaceAccess.READ,
        container_user=SandboxContainerUser.WORKSPACE_OWNER
        if owner
        else SandboxContainerUser.ROOT,
        execution_kind=SandboxExecutionKind.LOCAL_TOOL
        if owner
        else SandboxExecutionKind.HUMAN_TERMINAL,
        network=SandboxNetwork.NONE if owner else SandboxNetwork.UNRESTRICTED,
        root_filesystem=SandboxRootFilesystem.READ_ONLY
        if owner
        else SandboxRootFilesystem.WRITABLE,
    )
    with pytest.raises(MemoryEnforcementUnavailable, match="Owner-mapped"):
        await controller.run_sandbox(
            object(),
            request,
            job_id="never-launched",
            project_id="project",
            resource_kind="debugger",
            resource_id="debug",
        )
    await held(controller, "debugger")


async def test_host_command_session_does_not_fall_back_to_core_shell(memory, tmp_path):
    store, controller = memory
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
    with pytest.raises(MemoryEnforcementUnavailable, match="Host commands"):
        await manager._get_or_create_session(
            engagement_id="project", owner_kind="chat", owner_id="chat"
        )
    await held(controller, "host_command_session")


async def test_temporary_override_is_exact_project_only(memory, tmp_path, monkeypatch):
    store, controller = memory
    monkeypatch.setenv("NEBULA_TEMP_UNMANAGED_COMMAND_PROJECT_IDS", "project")
    assert unmanaged_commands_allowed("project")
    assert not unmanaged_commands_allowed("other")
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
    session = await manager._get_or_create_session(
        engagement_id="project", owner_kind="chat", owner_id="chat"
    )
    assert session.entity.execution_mode == "host"
    assert await controller.service.list_jobs() == []
    await manager.close_session(session.entity.id)


async def test_temporary_override_runs_ssh_for_exact_project(memory, monkeypatch):
    _store, controller = memory
    monkeypatch.setenv("NEBULA_TEMP_UNMANAGED_COMMAND_PROJECT_IDS", "project")

    async def synthetic_remote(*_args, **_kwargs):
        return 0, b"ready\n", b"", False

    monkeypatch.setattr(environments, "run_remote_command", synthetic_remote)
    plugin = build_ssh_tool_plugins(
        (SshEnvironment(alias="synthetic-host", enabled=True),),
        memory_compute=controller,
    )[0]
    result = await plugin.execute(
        SimpleNamespace(
            engagement_id="project",
            id="call",
            origin=ToolCallOrigin.CHAT,
            arguments={"command": "true"},
        ),
        None,
    )
    assert result.exit_code == 0 and result.stdout == "ready\n"
    assert await controller.service.list_jobs() == []


async def test_host_notebook_kernel_does_not_spawn_before_enforcement(memory, tmp_path):
    store, controller = memory
    notebook = NotebookService(
        store=store,
        workspace_resolver=lambda _: tmp_path,
        automation_runtime=SimpleNamespace(memory_compute=controller),
        data_root=tmp_path,
    )
    with pytest.raises(MemoryEnforcementUnavailable, match="Notebook"):
        await notebook._worker_command(
            "project",
            AutomationProjectPolicy(
                engagement_id="project",
                execution_mode="host",
                host_access_acknowledged=True,
            ),
        )
    await held(controller, "notebook_kernel")


async def test_raw_ssh_command_is_held_without_a_remote_memory_agent(memory):
    _store, controller = memory
    environment = SshEnvironment(
        alias="synthetic-host", display_name="Synthetic host", enabled=True
    )
    plugin = build_ssh_tool_plugins((environment,), memory_compute=controller)[0]
    with pytest.raises(MemoryEnforcementUnavailable, match="SSH host"):
        await plugin.execute(
            SimpleNamespace(
                engagement_id="project",
                id="call",
                origin=ToolCallOrigin.CHAT,
                arguments={"command": "synthetic-work"},
            ),
            None,
        )
    await held(controller, "ssh_command")


async def test_native_harness_cannot_use_its_unmanaged_open_method(memory):
    _store, controller = memory
    service = object.__new__(HarnessRuntimeService)
    service.memory_compute = controller

    class Native:
        async def open(self, _request):
            raise AssertionError("unmanaged harness opened")

    request = SimpleNamespace(
        session=SimpleNamespace(engagement_id="project", id="session")
    )
    with pytest.raises(MemoryEnforcementUnavailable, match="native harness"):
        await service._open_memory_managed(Native(), request)
    await held(controller, "harness_worker")


async def test_pdf_compute_has_no_unrestricted_thread_or_affinity_only_fallback(memory):
    _store, controller = memory
    executor = ReportCpuExecutor()
    executor.memory_compute = controller

    def forbidden():
        raise AssertionError("unrestricted report computation started")

    with managed_compute("project", "report_pdf", "render"):
        with pytest.raises(MemoryEnforcementUnavailable, match="PDF computation"):
            await executor.run(forbidden)
    await held(controller, "report_pdf")


@pytest.mark.parametrize("backend", ["none", "pool", "legacy-executor"])
async def test_report_service_holds_injected_executors_before_start_or_dispatch(
    memory, tmp_path, backend
):
    store, controller = memory

    class UnmanagedPool:
        async def start(self):
            raise AssertionError("unmanaged pool started")

        async def run(self, *_args, **_kwargs):
            raise AssertionError("unmanaged pool dispatched")

    executor = (
        None
        if backend == "none"
        else ReportCpuExecutor()
        if backend == "legacy-executor"
        else UnmanagedPool()
    )
    service = ReportRenderService(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        cpu_workers=executor,
    )
    service.memory_compute = controller
    await service.startup()
    with pytest.raises(MemoryEnforcementUnavailable, match="PDF computation"):
        await service._render_pdf_with_budget(
            SimpleNamespace(engagement_id="project", id="render"), {}
        )
    await held(controller, "report_pdf")


async def test_interactive_terminal_does_not_launch_outside_aggregate_accounting(
    memory,
):
    _store, controller = memory
    runner = ContainerSandboxRunner(memory_compute=controller)
    with managed_compute("project", "interactive_terminal", "terminal"):
        with pytest.raises(SandboxUnavailable, match="PTY adapter"):
            await runner.open_terminal(
                None, container_name="nebula-terminal-fixture", columns=80, rows=24
            )
    await held(controller, "interactive_terminal")
