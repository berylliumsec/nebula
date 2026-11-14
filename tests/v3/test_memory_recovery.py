"""Dead-owner cleanup uses durable identities and actual bounded OCI groups."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryLaunchOwner,
    MemoryLaunchGroups,
    MemoryLimits,
    MemoryRunningUpdate,
    MemoryBudgetError,
)
from nebula.v3.memory_recovery import MemoryReconciler, runner_fingerprint
from nebula.v3.memory_runtime import MemoryComputeController
from nebula.v3.memory_policy import (
    MemoryEnforcementUnavailable,
    MemoryEnforcementEvidence,
)
from nebula.v3.process_identity import process_birth, process_origin
from tests.v3.test_memory_budget import budgets as budgets, capability, evidence
from tests.v3.test_memory_enforcement import kernel as kernel
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def create(service, identity="job"):
    return await service.create_job(
        identity,
        project_id="synthetic",
        resource_kind="fixture",
        resource_id=identity,
        capabilities=capability(),
    )


@pytest.fixture
async def memory_diagnostics(tmp_path, monkeypatch):
    from nebula.v3 import diagnostics

    manager = diagnostics.DiagnosticManager(
        tmp_path / "diagnostic-fixture", level_override="debug", watch_settings=False
    )
    monkeypatch.setattr(diagnostics, "_manager", manager)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        yield manager, errors
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous)
        manager.close()


async def test_reconciliation_diagnostics_preserve_charge_and_join_callbacks(
    budgets, memory_diagnostics, monkeypatch
):
    service, _ = budgets
    manager, errors = memory_diagnostics
    created = await create(service)
    reserved = await service.reserve(created.id, capability())
    await service.claim_start(
        created.id,
        reserved.revision,
        owner=MemoryLaunchOwner(
            process_id=2_000_000_000,
            process_birth="absent-fixture",
            runner_fingerprint="a" * 64,
            process_origin=process_origin(),
        ),
    )
    observed = asyncio.Event()

    async def unavailable(_job):
        observed.set()
        raise RuntimeError("Synthetic probe failure; no runner contacted")

    reconciler = MemoryReconciler(service, unavailable)
    monkeypatch.setattr(reconciler, "_reconcile", unavailable)
    await reconciler.start()
    await asyncio.wait_for(observed.wait(), 5)
    await reconciler.close()
    await asyncio.sleep(0)
    manager.flush()
    records = [
        json.loads(line)
        for line in (manager.log_dir / "runtime.log").read_text().splitlines()
    ]
    assert any(row["event_code"] == "memory.reconciliation.held" for row in records)
    assert errors == []
    assert (await service.status()).reserved_bytes == reserved.reserved_bytes


async def test_live_owner_and_unknown_create_remain_charged(budgets):
    service, _ = budgets

    async def forbidden(_job):
        raise AssertionError("no cleanup runner should be contacted")

    await create(service, "live")
    reserved = await service.reserve("live", capability())
    await service.claim_start(
        "live",
        reserved.revision,
        owner=MemoryLaunchOwner(
            process_id=os.getpid(),
            process_birth=process_birth(os.getpid()),
            runner_fingerprint="a" * 64,
            process_origin=process_origin(),
        ),
    )
    await create(service, "dead-unknown")
    reserved = await service.reserve("dead-unknown", capability())
    await service.claim_start(
        "dead-unknown",
        reserved.revision,
        owner=MemoryLaunchOwner(
            process_id=2_000_000_000,
            process_birth="absent-synthetic-owner",
            runner_fingerprint="a" * 64,
            process_origin=process_origin(),
        ),
    )
    assert await MemoryReconciler(service, forbidden).once() == []
    assert (await service.status()).reserved_bytes == 4 * GIB
    assert (await service.get_job("live")).state == "starting"
    assert (await service.get_job("dead-unknown")).state == "uncertain"


async def test_auxiliary_identity_survives_usage_and_live_application(budgets):
    service, _ = budgets
    await create(service)
    reserved = await service.reserve("job", capability())
    claimed = await service.claim_start("job", reserved.revision)
    live = await service.started(
        "job",
        claimed.revision,
        evidence(claimed, auxiliary_group_ids=["owned-helper"], peak_bytes=17 * MIB),
    )
    observed = await service.observe("job", evidence(live, current_bytes=MIB))
    assert observed.evidence.auxiliary_group_ids == ["owned-helper"]
    assert observed.evidence.peak_bytes == 17 * MIB
    with pytest.raises(MemoryBudgetError, match="ownership"):
        await service.observe(
            "job", evidence(live, auxiliary_group_ids=["another-helper"])
        )
    pending = await service.request_running_update(
        "job",
        MemoryRunningUpdate(
            expected_revision=live.revision,
            limits=MemoryLimits(max_bytes=GIB),
            acknowledge_possible_termination=True,
        ),
        capability(),
        actor_id="operator",
    )
    applied = await service.applied_running_update(
        "job", pending.revision, evidence(pending, limits=MemoryLimits(max_bytes=GIB))
    )
    assert applied.evidence.auxiliary_group_ids == ["owned-helper"]
    with pytest.raises(MemoryBudgetError, match="same owned"):
        await service.finish(
            "job",
            applied.revision,
            evidence(
                applied,
                empty=True,
                reason="completed",
                limits=MemoryLimits(max_bytes=GIB),
                auxiliary_group_ids=["another-helper"],
            ),
        )


async def test_changed_runner_cannot_remove_a_lost_jobs_groups(budgets):
    service, _ = budgets
    await create(service)
    reserved = await service.reserve("job", capability())
    claimed = await service.claim_start(
        "job",
        reserved.revision,
        owner=MemoryLaunchOwner(
            process_id=2_000_000_000,
            process_birth="absent",
            runner_fingerprint="a" * 64,
            process_origin=process_origin(),
        ),
    )
    await service.groups_created(
        "job", claimed.revision, MemoryLaunchGroups(group_id="b" * 64)
    )

    async def resolve(_job):
        return SimpleNamespace(
            profile=SimpleNamespace(
                model_dump=lambda **_kw: {"socket": "different-runner"}
            )
        )

    assert await MemoryReconciler(service, resolve).once() == []
    assert (await service.get_job("job")).reserved_bytes == 2 * GIB


@pytest.mark.parametrize("origin", [None, "foreign-pid-namespace"])
async def test_foreign_or_unknown_pid_namespace_never_authorizes_cleanup(
    budgets, origin
):
    service, _ = budgets
    await create(service)
    reserved = await service.reserve("job", capability())
    claimed = await service.claim_start(
        "job",
        reserved.revision,
        owner=MemoryLaunchOwner(
            process_id=2_000_000_000,
            process_birth="not-inspectable-here",
            process_origin=origin,
            runner_fingerprint="a" * 64,
        ),
    )
    await service.groups_created(
        "job", claimed.revision, MemoryLaunchGroups(group_id="b" * 64)
    )

    async def forbidden(_job):
        raise AssertionError("foreign PID lookup contacted a cleanup runner")

    assert await MemoryReconciler(service, forbidden).once() == []
    assert (await service.status()).reserved_bytes == 2 * GIB


@pytest.mark.parametrize("changed_daemon", [False, True])
async def test_daemon_authority_and_uncertain_limit_cleanup(budgets, changed_daemon):
    service, _ = budgets
    removed = []

    class Runner:
        profile = SimpleNamespace(
            runtime_type=SimpleNamespace(value="docker"),
            model_dump=lambda **_kwargs: {"socket": "same-socket"},
        )

        async def available(self):
            return True, "synthetic bookkeeping fixture"

        async def _capture(self, operation, *_args):
            if operation == "info":
                return (
                    json.dumps(
                        {
                            "ID": "replacement-daemon"
                            if changed_daemon
                            else "original-daemon"
                        }
                    ),
                    "",
                    0,
                )
            return (
                json.dumps(
                    {
                        "Id": "b" * 64,
                        "Config": {"Labels": {"nebula.memory.job": "job"}},
                        "HostConfig": {"Memory": 3 * GIB, "MemorySwap": 3 * GIB},
                        "State": {"OOMKilled": False},
                    }
                ),
                "",
                0,
            )

        async def _force_remove(self, group_id, *, require_removed):
            assert require_removed
            removed.append(group_id)

    runner = Runner()
    await create(service)
    reserved = await service.reserve("job", capability())
    claimed = await service.claim_start(
        "job",
        reserved.revision,
        owner=MemoryLaunchOwner(
            process_id=2_000_000_000,
            process_birth="absent",
            process_origin=process_origin(),
            runner_fingerprint=runner_fingerprint(runner),
            runner_identity="docker:original-daemon",
        ),
    )
    bound = await service.groups_created(
        "job", claimed.revision, MemoryLaunchGroups(group_id="b" * 64)
    )
    await service.started(
        "job", bound.revision, evidence(bound).model_copy(update={"group_id": "b" * 64})
    )

    async def resolve(_job):
        return runner

    released = await MemoryReconciler(service, resolve).once()
    result = await service.get_job("job")
    if changed_daemon:
        assert released == [] and removed == [] and result.reserved_bytes == 2 * GIB
    else:
        assert released == ["job"] and removed == ["b" * 64]
        assert result.reserved_bytes == 0 and result.application_state == "uncertain"
        assert result.evidence.application_uncertain
        assert result.evidence.applied.max_bytes == 2 * GIB


@pytest.mark.parametrize("released", [False, True])
async def test_normal_cleanup_does_not_free_capacity_on_a_replacement_daemon(
    budgets, tmp_path, released, memory_diagnostics
):
    service, _ = budgets
    from nebula.v3.sandbox import (
        SandboxRequest,
        RunnerProfile,
        ContainerRuntimeType,
        RunnerPlatform,
        RunnerIsolationMode,
    )

    class Runner:
        profile = RunnerProfile(
            runtime_type=ContainerRuntimeType.DOCKER,
            executable=Path("/usr/bin/docker"),
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
            socket="unix:///isolated/fixture.sock",
        )
        identity = "original-daemon"

        async def available(self):
            return True, "nonexecuting callback fixture"

        async def _capture(self, operation, *_args):
            assert operation == "info"
            return (
                json.dumps(
                    {
                        "ID": self.identity,
                        "CgroupVersion": "2",
                        "MemoryLimit": True,
                        "MemTotal": 16 * GIB,
                    }
                ),
                "",
                0,
            )

        async def run_stream(self, request, *, memory_guard, **_options):
            await memory_guard.on_groups_created(MemoryLaunchGroups(group_id="b" * 64))
            proof = MemoryEnforcementEvidence(
                job_id="cleanup-authority",
                group_id="b" * 64,
                applied=memory_guard.policy.limits,
                protected_process_tree=True,
                termination_reason="running",
                detail="synthetic callback evidence",
            )
            if released:
                await memory_guard.on_ready(proof)
            self.identity = "replacement-daemon"
            if released:
                await memory_guard.on_finished(
                    proof.model_copy(
                        update={"group_empty": True, "termination_reason": "cancelled"}
                    )
                )
            else:
                await memory_guard.on_unstarted_cleanup(
                    "b" * 64, "absence returned by replacement daemon"
                )

    controller = MemoryComputeController(service, tmp_path)
    with pytest.raises(MemoryEnforcementUnavailable, match="daemon identity"):
        await controller.run_sandbox(
            Runner(),
            SandboxRequest(
                workspace=tmp_path,
                image="registry.invalid/fixture@sha256:" + "a" * 64,
                command=["never-executed"],
            ),
            job_id="cleanup-authority",
            project_id="synthetic",
            resource_kind="fixture",
            resource_id="cleanup",
            requested=MemoryLimits(max_bytes=GIB),
        )
    result = await service.get_job("cleanup-authority")
    assert result.state == "uncertain" and result.reserved_bytes == GIB


_WORKER = r"""
import asyncio, json, sys
from pathlib import Path
from nebula.v3.database import Database
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import MemoryAdministratorLimits
from nebula.v3.memory_runtime import MemoryComputeController
from nebula.v3.sandbox import RunnerProfile, SandboxRequest
from tests.v3.test_memory_enforcement import IsolatedKernelFixtureRunner

async def main():
    config=json.loads(Path(sys.argv[1]).read_text())
    runner=IsolatedKernelFixtureRunner(profile=RunnerProfile.model_validate(config['profile']), allow_unpinned_images=True, workspace_roots=[Path(config['workspace'])])
    service=MemoryBudgetService(Database(config['database']), administrator=lambda: MemoryAdministratorLimits.model_validate(config['administrator']))
    controller=MemoryComputeController(service, Path(config['root']))
    job=await controller.open_persistent(runner, SandboxRequest.model_validate(config['request']), job_id='crashed-owner', project_id='synthetic', resource_kind='fixture', resource_id='worker', container_name=config['name'])
    Path(config['ready']).write_text('ready')
    await asyncio.Event().wait()

asyncio.run(main())
"""


async def test_crashed_controller_releases_real_group_after_verified_cleanup(
    kernel, tmp_path
):
    runner, service, _controller, workspace, _image = kernel
    from tests.v3.test_memory_enforcement import request
    from uuid import uuid4

    ready = tmp_path / "owner-ready"
    config = tmp_path / "owner.json"
    config.write_text(
        json.dumps(
            {
                "profile": runner.profile.model_dump(mode="json"),
                "workspace": str(workspace),
                "database": str(tmp_path / "fixture.db"),
                "root": str(tmp_path),
                "administrator": service.administrator().model_dump(mode="json"),
                "request": request(kernel, 4, hold=60).model_dump(mode="json"),
                "name": "nebula-crash-" + uuid4().hex,
                "ready": str(ready),
            }
        )
    )
    script = tmp_path / "owner.py"
    script.write_text(_WORKER)
    child = None
    with (tmp_path / "owner.log").open("wb") as output:
        try:
            child = subprocess.Popen(
                [sys.executable, str(script), str(config)],
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2])
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                },
            )
            deadline = time.monotonic() + 20
            while not ready.exists():
                assert child.poll() is None, (tmp_path / "owner.log").read_text()
                assert time.monotonic() < deadline, (tmp_path / "owner.log").read_text()
                await asyncio.sleep(0.05)
            before = await service.get_job("crashed-owner")
            assert (
                before.state == "running"
                and before.launch_groups.group_id == before.evidence.group_id
            )
            assert before.launch_owner.runner_fingerprint == runner_fingerprint(runner)

            async def resolve(_job):
                return runner

            reconciler = MemoryReconciler(service, resolve)
            assert await reconciler.once() == []
            assert (await service.status()).reserved_bytes == 32 * MIB
            child.kill()
            await asyncio.to_thread(child.wait, timeout=5)
            assert await reconciler.once() == ["crashed-owner"]
            result = await service.get_job("crashed-owner")
            assert result.state == "finished" and result.outcome == "worker_lost"
            assert result.evidence.group_empty and result.reserved_bytes == 0
            assert (await service.status()).reserved_bytes == 0
            assert (await runner._capture("inspect", before.evidence.group_id))[2] != 0
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            for job in await service.list_jobs():
                if job.launch_groups:
                    await runner._force_remove(
                        job.launch_groups.group_id, require_removed=True
                    )


EARLIER_BOOT = "00000000-1111-4222-8333-444444444444"


def _earlier_boot_owner(**changes):
    kind, _boot, label, inode = process_origin().split(":")
    values = dict(
        process_id=2_000_000_000,
        process_birth=f"linux:{EARLIER_BOOT}:12345",
        runner_fingerprint="a" * 64,
        process_origin=":".join((kind, EARLIER_BOOT, label, inode)),
    )
    values.update(changes)
    return MemoryLaunchOwner(**values)


async def _no_runner(_job):
    raise AssertionError("a prelaunch release never contacts a cleanup runner")


@pytest.mark.skipif(process_origin() is None, reason="Linux process origins")
@pytest.mark.parametrize("boot_ended", [True, False, None])
async def test_prelaunch_owner_from_an_earlier_boot_is_released_only_with_lease_proof(
    budgets, boot_ended
):
    """After a reboot the old owner's charge is freed once its boot is quiet."""
    service, _ = budgets
    await create(service)
    await service.reserve("job", capability(), owner=_earlier_boot_owner())
    asked = []

    async def ended(boot):
        asked.append(boot)
        return boot_ended

    reconciler = MemoryReconciler(
        service,
        _no_runner,
        previous_boot_ended=None if boot_ended is None else ended,
    )
    released = await reconciler.once()
    job = await service.get_job("job")
    if boot_ended:
        assert released == ["job"] and asked == [EARLIER_BOOT]
        assert (job.state, job.outcome, job.reserved_bytes) == (
            "finished",
            "worker_lost",
            0,
        )
    else:
        # A live lease from that boot, or a reconciler that cannot check
        # leases (a project worker), keeps the reservation charged.
        assert released == [] and job.state == "reserved"
        assert job.reserved_bytes == 2 * GIB


@pytest.mark.skipif(process_origin() is None, reason="Linux process origins")
async def test_earlier_boot_owner_releases_its_prelaunch_reservation(budgets):
    service, _ = budgets
    await create(service)
    owner = _earlier_boot_owner()
    job = await service.reserve("job", capability(), owner=owner)
    released = await service.release_prelaunch(
        "job", job.revision, owner=owner, owner_dead=True
    )
    assert (released.state, released.outcome, released.reserved_bytes) == (
        "finished",
        "worker_lost",
        0,
    )


@pytest.mark.skipif(process_origin() is None, reason="Linux process origins")
async def test_same_boot_namespace_change_never_proves_owner_death(budgets):
    service, _ = budgets
    await create(service)
    kind, boot, label, _inode = process_origin().split(":")
    owner = MemoryLaunchOwner(
        process_id=2_000_000_000,
        process_birth=f"linux:{boot}:12345",
        runner_fingerprint="a" * 64,
        process_origin=":".join((kind, boot, label, "4026599999")),
    )
    job = await service.reserve("job", capability(), owner=owner)

    async def ended(_boot):
        raise AssertionError("a same-boot namespace change is not a boot change")

    reconciler = MemoryReconciler(service, _no_runner, previous_boot_ended=ended)
    assert await reconciler.once() == []
    with pytest.raises(MemoryBudgetError):
        await service.release_prelaunch(
            "job", job.revision, owner=owner, owner_dead=True
        )
    assert (await service.get_job("job")).reserved_bytes == 2 * GIB


@pytest.mark.skipif(process_origin() is None, reason="Linux process origins")
async def test_launched_earlier_boot_job_still_needs_verified_group_removal(budgets):
    """Owner death never frees groups; the verified-removal path still applies."""
    service, _ = budgets
    await create(service)
    owner = _earlier_boot_owner()
    reserved = await service.reserve("job", capability())
    claimed = await service.claim_start("job", reserved.revision, owner=owner)
    await service.groups_created(
        "job", claimed.revision, MemoryLaunchGroups(group_id="b" * 64)
    )
    resolved = []

    async def resolve(job):
        resolved.append(job.id)
        return SimpleNamespace(
            profile=SimpleNamespace(
                model_dump=lambda **_kw: {"socket": "different-runner"}
            )
        )

    async def ended(_boot):
        return True

    reconciler = MemoryReconciler(service, resolve, previous_boot_ended=ended)
    assert await reconciler.once() == []
    assert resolved == ["job"]
    assert (await service.get_job("job")).reserved_bytes == 2 * GIB
