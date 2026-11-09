"""Cached search admission and real isolated-kernel persistent service checks.

The kernel fixture runs an inert shell in the explicitly supplied image. It
does not contact SearXNG/upstream engines or certify a production daemon.
"""

import asyncio
import json

import pytest

from nebula.v3.domain import RunnerProfile, WebSearchRuntimeState
from nebula.v3.managed_search_runtime import (
    CachedSearchImage,
    ManagedSearchUnavailable,
    ManagedSearchRuntime,
    SearchContainerRunner,
)
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetUpdate,
    MemoryLimits,
)
from nebula.v3.memory_runtime import MemoryComputeController
from nebula.v3.runner_profiles import container_runner_profile
from nebula.v3.sandbox import (
    ContainerImagePreparer,
    ContainerSandboxRunner,
    SandboxUnavailable,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.web_search import EXPECTED_REPOSITORY, SearchRuntimeUnavailable
from tests.v3.test_memory_enforcement import IsolatedKernelFixtureRunner
from tests.v3.test_memory_enforcement import kernel as _kernel_fixture

kernel = _kernel_fixture

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def profile(**changes):
    return RunnerProfile(
        id="local",
        name="Synthetic search daemon",
        runtime="docker",
        executable="/usr/bin/docker",
        platform="linux/amd64",
        isolation="rootless",
        enabled=True,
        healthy=True,
        **changes,
    )


@pytest.fixture
async def cached(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    await store.create(profile())
    administrator = MemoryAdministratorLimits(
        capacity_bytes=16 * GIB,
        maximum_job_bytes=8 * GIB,
        maximum_aggregate_bytes=14 * GIB,
        minimum_reserve_bytes=2 * GIB,
    )
    budgets = MemoryBudgetService(store.database, administrator=lambda: administrator)
    controller = MemoryComputeController(budgets, tmp_path)
    calls = []
    document = {
        "RepoDigests": [EXPECTED_REPOSITORY + "@sha256:" + "a" * 64],
        "Config": {
            "Entrypoint": ["/image-entrypoint"],
            "Cmd": ["serve"],
            "WorkingDir": "/app",
        },
    }

    class Inspector(ContainerSandboxRunner):
        async def available(self):
            return True, "inert contract fixture"

        async def _capture(self, *arguments, **_options):
            calls.append(arguments)
            if arguments[0] == "image":
                return json.dumps(document), "", 0
            if arguments[0] == "info":
                return (
                    json.dumps(
                        {
                            "ID": "synthetic-daemon",
                            "CgroupVersion": "2",
                            "MemoryLimit": True,
                            "MemTotal": 16 * GIB,
                        }
                    ),
                    "",
                    0,
                )
            raise AssertionError("unmanaged operation: " + repr(arguments))

    class Runtime(ManagedSearchRuntime):
        def _runner(self, selected, **_kwargs):
            return Inspector(
                profile=container_runner_profile(selected),
                memory_profile_id=selected.id,
            )

    runtime = Runtime(store=store, data_root=tmp_path, memory_compute=controller)
    try:
        yield runtime, budgets, calls, document
    finally:
        await runtime.close()
        await store.database.dispose()


async def test_cached_install_is_inspection_and_pinning_without_daemon_compute(cached):
    runtime, budgets, calls, _ = cached
    assert (await runtime.initialization_capability(action="install"))["supported"]
    assert await runtime.install() == "sha256:" + "a" * 64
    assert all(call[0] == "image" for call in calls)
    assert await budgets.list_jobs() == []


@pytest.mark.parametrize("invalid", ["foreign-image", "missing-command"])
async def test_cached_image_must_prove_official_identity_and_default_command(
    cached, invalid
):
    runtime, _budgets, calls, document = cached
    if invalid == "foreign-image":
        document["RepoDigests"] = ["registry.invalid/unrelated@sha256:" + "a" * 64]
    else:
        document["Config"]["Entrypoint"] = []
        document["Config"]["Cmd"] = []
    assert not (await runtime.initialization_capability(action="start"))["supported"]
    with pytest.raises(SearchRuntimeUnavailable):
        await runtime.install()
    assert all(call[0] == "image" for call in calls)


async def test_pressure_threshold_is_rejected_only_when_selected_policy_requires_it(
    cached,
):
    runtime, budgets, _calls, _document = cached
    assert (await runtime.initialization_capability())["supported"]
    before = await budgets.status()
    await budgets.save(
        MemoryBudgetUpdate(
            expected_revision=before.revision,
            policy=before.policy.model_copy(
                update={
                    "default_job": MemoryLimits(max_bytes=64 * MIB, high_bytes=32 * MIB)
                }
            ),
        ),
        actor_id="synthetic-operator",
    )
    observed = await runtime.initialization_capability()
    assert not observed["supported"] and "memory.high" in observed["detail"]
    assert (await runtime.initialization_capability(action="install"))["supported"]


async def test_missing_cached_image_never_falls_back_to_pull(cached, monkeypatch):
    runtime, _budgets, calls, _document = cached
    inspector = runtime._runner(await runtime._profile())

    async def missing(*arguments, **_kwargs):
        calls.append(arguments)
        return "", "no such image", 1

    monkeypatch.setattr(inspector, "_capture", missing)
    monkeypatch.setattr(runtime, "_runner", lambda *_args, **_kwargs: inspector)
    capability = await runtime.initialization_capability(action="install")
    assert not capability["supported"] and "daemon runner" in capability["detail"]
    with pytest.raises(SearchRuntimeUnavailable, match="provisioning"):
        await runtime.install()
    assert all(call[0] == "image" for call in calls)


class IsolatedSearchRunner(SearchContainerRunner):
    available = IsolatedKernelFixtureRunner.available


class IsolatedSearchRuntime(ManagedSearchRuntime):
    """Real kernel boundary, synthetic cached image/health contract only."""

    def __init__(self, *, fixture_runner, fixture_image, command, **kwargs):
        super().__init__(**kwargs)
        self.fixture_runner, self.fixture_image, self.command = (
            fixture_runner,
            fixture_image,
            command,
        )

    def _runner(self, selected, *, configuration=None, port=0, image=None):
        kwargs = dict(
            profile=self.fixture_runner.profile,
            memory_compute=self.memory_compute,
            memory_profile_id=selected.id,
            allowed_environment={"HOME", "SEARXNG_SETTINGS_PATH"},
        )
        if image is None:
            return IsolatedKernelFixtureRunner(**kwargs)
        return IsolatedSearchRunner(
            configuration=configuration, port=port, image=image, **kwargs
        )

    async def _cached_image(self, _runner):
        return CachedSearchImage(
            self.fixture_image,
            self.fixture_image.rsplit("@", 1)[1],
            self.command,
            "/tmp",
        )

    async def _probe(self, _port):
        if self._job is None:
            return False
        _output, _error, code = await self._runner_instance._capture(
            "exec",
            "--user",
            "65532:65532",
            self._job.guard.container_id,
            "/bin/sh",
            "-c",
            "test -e /tmp/ready",
        )
        return code == 0


async def make_runtime(kernel, tmp_path, *, command=None):
    runner, service, controller, _workspace, image = kernel
    store = NebulaStore(service.database)
    existing = await store.list_entities(RunnerProfile)
    if not existing:
        selected = profile()
        await store.create(selected)
    return IsolatedSearchRuntime(
        store=store,
        data_root=tmp_path,
        memory_compute=controller,
        fixture_runner=runner,
        fixture_image=image,
        command=command
        or [
            "/bin/sh",
            "-c",
            'set -e; test -r "$SEARXNG_SETTINGS_PATH"; echo fixture-ready; touch /tmp/ready; sleep 60',
        ],
    )


async def test_real_service_uses_one_reservation_and_verified_nonroot_loopback_group(
    kernel, tmp_path
):
    runtime = await make_runtime(kernel, tmp_path)
    _runner, budgets, _controller, _workspace, _image = kernel
    try:
        port = await runtime.ensure_ready()
        job = await budgets.get_job(runtime._job.job_id)
        group = job.evidence.group_id
        assert job.project_id is None and job.resource_kind == "web_search_service"
        assert job.snapshot.limits == MemoryLimits(max_bytes=32 * MIB)
        assert job.state == "running" and job.reserved_bytes == 32 * MIB
        assert runtime._configuration_export.stat().st_mode & 0o777 == 0o700
        assert (
            runtime._configuration_export / "settings.yml"
        ).stat().st_mode & 0o777 == 0o444
        assert await runtime.ensure_ready() == port
        assert (await runtime.state())[0] == WebSearchRuntimeState.READY
        assert len(await budgets.list_jobs()) == 1
        log = runtime._runner_instance.log_directory / "stdout.log"
        await runtime.stop()
        after = await budgets.get_job(job.id)
        assert after.state == "finished" and after.reserved_bytes == 0
        assert after.evidence.group_empty and after.outcome == "cancelled"
        assert (await kernel[0]._capture("inspect", group))[2] != 0
        assert "fixture-ready" in log.read_text()
    finally:
        await runtime.close()


async def test_real_service_oom_has_positive_evidence_and_preserves_logs(
    kernel, tmp_path
):
    runtime = await make_runtime(
        kernel,
        tmp_path,
        command=[
            "/bin/sh",
            "-c",
            'set -e; test -r "$SEARXNG_SETTINGS_PATH"; echo fixture-ready; touch /tmp/ready; sleep 2; dd if=/dev/zero of=/tmp/balloon bs=1048576 count=48; sleep 60',
        ],
    )
    try:
        await runtime.ensure_ready()
        job_id = runtime._job.job_id
        log = runtime._runner_instance.log_directory / "stdout.log"
        await asyncio.wait_for(asyncio.shield(runtime._job.task), timeout=20)
        observed = await kernel[1].get_job(job_id)
        assert observed.outcome == "memory_limit_exceeded"
        assert (
            observed.evidence.oom_victim_confirmed and observed.evidence.oom_kill_count
        )
        assert observed.evidence.group_empty and observed.reserved_bytes == 0
        assert (await runtime.state())[0] == WebSearchRuntimeState.FAILED
        with pytest.raises(ManagedSearchUnavailable) as failed:
            await runtime.ensure_ready()
        assert failed.value.memory_job_id == job_id
        assert failed.value.memory_outcome == "memory_limit_exceeded"
        await runtime.stop()
        assert "fixture-ready" in log.read_text()
    finally:
        await runtime.close()


@pytest.mark.parametrize("action", ["pull", "build"])
async def test_image_daemon_compute_is_held_before_any_unrestricted_client(
    cached, monkeypatch, action
):
    runtime, budgets, _calls, _document = cached
    runner = runtime._runner(await runtime._profile())
    runner.memory_compute = runtime.memory_compute
    preparer = ContainerImagePreparer(
        runner=runner,
        platform="linux/amd64",
        source_reference="docker.io/kalilinux/kali-rolling:latest",
        expected_repository="docker.io/kalilinux/kali-rolling",
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unrestricted daemon client started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(SandboxUnavailable, match="memory-managed daemon runner"):
        await preparer._runtime_command(
            action, preparer.source_reference, timeout_seconds=10
        )
    jobs = await budgets.list_jobs()
    assert len(jobs) == 1 and jobs[0].state == "blocked"
    assert jobs[0].reserved_bytes == 0
    assert jobs[0].resource_kind == "container_image_" + action


async def test_image_daemon_compute_is_refused_without_a_budget_controller(
    cached, monkeypatch
):
    runtime, budgets, _calls, _document = cached
    runner = runtime._runner(await runtime._profile())
    preparer = ContainerImagePreparer(
        runner=runner,
        platform="linux/amd64",
        source_reference="docker.io/kalilinux/kali-rolling:latest",
        expected_repository="docker.io/kalilinux/kali-rolling",
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unrestricted daemon client started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(SandboxUnavailable, match="memory-managed daemon runner"):
        await preparer._runtime_command("build", "never-used", timeout_seconds=10)
    assert await budgets.list_jobs() == []


async def test_real_service_waits_at_aggregate_ceiling_without_launching(
    kernel, tmp_path
):
    budgets = kernel[1]
    before = await budgets.status()
    await budgets.save(
        MemoryBudgetUpdate(
            expected_revision=before.revision,
            policy=before.policy.model_copy(update={"aggregate_bytes": 32 * MIB}),
        ),
        actor_id="synthetic-operator",
    )
    first = await make_runtime(kernel, tmp_path / "first")
    second = await make_runtime(kernel, tmp_path / "second")
    waiting = None
    try:
        await first.ensure_ready()
        waiting = asyncio.create_task(second.ensure_ready())
        deadline = asyncio.get_running_loop().time() + 10
        while not any(job.state == "queued" for job in await budgets.list_jobs()):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        assert not waiting.done()
        assert (await budgets.status()).reserved_bytes == 32 * MIB
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        jobs = await budgets.list_jobs()
        assert len(jobs) == 2
        queued = next(job for job in jobs if job.id != first._job.job_id)
        assert queued.state == "finished" and queued.launch_groups is None
        assert queued.reserved_bytes == 0
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await second.close()
        await first.close()


@pytest.mark.parametrize("drift", ["publication", "authority"])
async def test_real_service_refuses_changed_publication_or_daemon_identity(
    kernel, tmp_path, drift
):
    runtime = await make_runtime(kernel, tmp_path)
    original = None
    try:
        await runtime.ensure_ready()
        runner, job_id = runtime._runner_instance, runtime._job.job_id
        original = runner._capture
        destructive = []

        async def altered(*arguments, **kwargs):
            if arguments[0] in {"rm", "kill", "stop"}:
                destructive.append(arguments)
            output, error, code = await original(*arguments, **kwargs)
            if not code and arguments[0] in {"inspect", "info"}:
                document = json.loads(output)
                if drift == "authority" and arguments[0] == "info":
                    document["ID"] = "different-disposable-daemon"
                elif drift == "publication" and arguments[0] == "inspect":
                    document["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostIp"] = (
                        "0.0.0.0"
                    )
                output = json.dumps(document)
            return output, error, code

        runner._capture = altered
        assert (await runtime.state())[0] == WebSearchRuntimeState.FAILED
        if drift == "authority":
            with pytest.raises(
                SearchRuntimeUnavailable, match="capacity remains charged"
            ):
                await runtime.stop()
            assert not destructive
            assert (await kernel[1].get_job(job_id)).reserved_bytes == 32 * MIB
        runner._capture = original
        await runtime.stop()
        assert (await kernel[1].get_job(job_id)).evidence.group_empty
    finally:
        if original is not None and runtime._runner_instance is not None:
            runtime._runner_instance._capture = original
        await runtime.close()
