"""Real controller death and SQL prelaunch races; no runner workload is launched."""

import asyncio
from contextlib import nullcontext
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from nebula.v3.database import Database, MemoryBudgetAuditRow, ProjectOwnershipRow
from nebula.v3.domain import Engagement
from nebula.v3.execution_memory import ExecutionMemoryBudget
from nebula.v3.execution_scope import current_execution_lease, execution_context
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.memory_policy import (
    GIB,
    MIB,
    MemoryAdministratorLimits,
    MemoryBudgetConflict,
    MemoryBudgetUpdate,
    MemoryLaunchOwner,
    MemoryLimits,
)
from nebula.v3.memory_recovery import MemoryReconciler, runner_fingerprint
from nebula.v3.memory_runtime import MemoryComputeController, probe_memory_runner
from nebula.v3.process_identity import (
    ProcessIdentityUnavailable,
    process_birth,
    process_origin,
)
from nebula.v3.runtime_coordination import LeaseLost, ProjectLease, RuntimeCoordination
from nebula.v3.sandbox import SandboxContainerUser
from nebula.v3.storage import NebulaStore

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="requires exact Linux PID/birth death proof",
    ),
]
ADMIN = MemoryAdministratorLimits(
    capacity_bytes=16 * GIB,
    maximum_job_bytes=8 * GIB,
    maximum_aggregate_bytes=14 * GIB,
    minimum_reserve_bytes=2 * GIB,
)
REQUEST = SimpleNamespace(
    container_user=SandboxContainerUser.NON_ROOT, network=SimpleNamespace(value="none")
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_diagnostics(monkeypatch):
    monkeypatch.setattr(
        "nebula.v3.memory_recovery.record_caught_exception",
        lambda *_args, **_kwargs: None,
    )


class Runner:
    profile = SimpleNamespace(
        runtime_type=SimpleNamespace(value="docker"),
        model_dump=lambda **_: {"runtime": "docker", "socket": "synthetic-no-daemon"},
    )

    def __init__(self, service, marker=None):
        self.service, self.marker = service, marker
        self.entered, self.resume = asyncio.Event(), asyncio.Event()
        self.info_count, self.launches = 0, 0

    async def available(self):
        return True, "Inert prelaunch identity fixture"

    async def _capture(self, operation, *_args):
        assert operation == "info", (
            "prelaunch recovery must not inspect or remove groups"
        )
        self.info_count += 1
        if self.info_count == 2:
            job = await self.service.get_job("crash-job")
            if self.marker:
                lease = current_execution_lease()
                temporary = self.marker.with_suffix(".partial")
                temporary.write_text(
                    json.dumps(
                        {
                            "job": job.model_dump(mode="json"),
                            "transaction_active": self.service.database.task_session()
                            is not None,
                            "lease": asdict(lease) if lease else None,
                            "launches": self.launches,
                        }
                    )
                )
                temporary.replace(self.marker)
            self.entered.set()
            await self.resume.wait()
        return (
            json.dumps(
                {
                    "CgroupVersion": "2",
                    "MemoryLimit": True,
                    "MemTotal": 16 * GIB,
                    "ID": "synthetic-prelaunch-daemon",
                }
            ),
            "",
            0,
        )

    async def run_stream(self, *_args, **_kwargs):
        self.launches += 1
        raise AssertionError("the fixture must not cross the runner launch boundary")


async def setup(root):
    database = Database(root / "ledger.db")
    service = MemoryBudgetService(database, administrator=lambda: ADMIN)
    capability = await probe_memory_runner(Runner(service), profile_id="fixture")
    status = await service.status([capability])
    await service.save(
        MemoryBudgetUpdate(
            expected_revision=status.revision,
            policy=status.policy.model_copy(
                update={
                    "aggregate_bytes": 32 * MIB,
                    "default_job": MemoryLimits(max_bytes=32 * MIB),
                }
            ),
        ),
        actor_id="fixture",
        capabilities=[capability],
    )
    return database, service, capability


def controller_task(service, root, runner):
    return asyncio.create_task(
        MemoryComputeController(service, root).run_sandbox(
            runner,
            REQUEST,
            job_id="crash-job",
            project_id="synthetic",
            resource_kind="fixture",
            resource_id="prelaunch",
            profile_id="fixture",
            requested=MemoryLimits(max_bytes=32 * MIB),
        )
    )


async def child(root, mode):
    database = Database(root / "ledger.db")
    coordination = RuntimeCoordination(database)
    context = nullcontext()
    if mode == "execution":
        await coordination.register_runtime("old", "inert-fixture")
        lease = await coordination.claim_project("synthetic", "old")
        service = ExecutionMemoryBudget(
            coordination, lease, administrator=lambda: ADMIN
        )
        context = execution_context(lease)
    else:
        service = MemoryBudgetService(database, administrator=lambda: ADMIN)
    with context:
        runner = Runner(service, root / "checkpoint.json")
        await controller_task(service, root, runner)


async def no_runner(_job):
    raise AssertionError("never-launched recovery cannot claim runner cleanup")


@pytest.mark.parametrize("mode", ["host", "execution"])
async def test_sigkill_after_atomic_reservation_recovers_capacity(tmp_path, mode):
    database, _, capability = await setup(tmp_path)
    if mode == "execution":
        store = NebulaStore(database)
        await store.create(
            Engagement(id="synthetic", name="Isolated prelaunch fixture")
        )
        await store.event_history.close()
    await database.dispose()
    repository = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "PYTHONPATH": str(repository / "src") + os.pathsep + str(repository),
    }
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--child", str(tmp_path), mode],
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        async with asyncio.timeout(15):
            while not (tmp_path / "checkpoint.json").exists():
                if process.poll() is not None:
                    raise AssertionError(process.communicate())
                await asyncio.sleep(0.01)
        checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
        before = checkpoint["job"]
        owner = MemoryLaunchOwner.model_validate(before["launch_owner"])
        assert before["state"] == "reserved" and before["revision"] == 2
        assert before["reserved_bytes"] == 32 * MIB
        assert before["launch_groups"] is None and before["evidence"] is None
        assert (
            owner.process_id == process.pid
            and process_birth(process.pid) == owner.process_birth
        )
        assert (
            owner.process_origin == process_origin()
            and owner.runner_fingerprint == runner_fingerprint(Runner(None))
        )
        assert not checkpoint["transaction_active"] and checkpoint["launches"] == 0
        assert bool(checkpoint["lease"]) == (mode == "execution")
        process.kill()
        stdout, stderr = await asyncio.to_thread(process.communicate, timeout=5)
        assert process.returncode == -9 and process_birth(owner.process_id) is None
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=5)
    fresh = Database(tmp_path / "ledger.db")
    service = MemoryBudgetService(fresh, administrator=lambda: ADMIN)
    try:
        assert [job.id for job in await service.reconciliation_jobs()] == ["crash-job"]
        if mode == "execution":
            coordination = RuntimeCoordination(fresh)
            old = ProjectLease(**checkpoint["lease"])
            async with fresh.session() as session:
                await session.execute(
                    update(ProjectOwnershipRow)
                    .where(ProjectOwnershipRow.project_id == "synthetic")
                    .values(lease_expires_at_ms=0)
                )
            await coordination.register_runtime("replacement", "inert-replacement")
            lease = await coordination.claim_project("synthetic", "replacement")
            stale = ExecutionMemoryBudget(
                coordination, old, administrator=lambda: ADMIN
            )
            with execution_context(old), pytest.raises(LeaseLost):
                await stale.release_prelaunch(
                    "crash-job", 2, owner=owner, owner_dead=True
                )
            replacement = ExecutionMemoryBudget(
                coordination, lease, administrator=lambda: ADMIN
            )
            with execution_context(lease):
                assert await MemoryReconciler(replacement, no_runner).once() == [
                    "crash-job"
                ]
        else:
            assert await MemoryReconciler(service, no_runner).once() == ["crash-job"]
        assert await MemoryReconciler(service, no_runner).once() == []
        after = await service.get_job("crash-job")
        assert (after.state, after.revision, after.reserved_bytes, after.outcome) == (
            "finished",
            3,
            0,
            "worker_lost",
        )
        assert after.evidence is None and after.launch_groups is None
        with pytest.raises(MemoryBudgetConflict):
            await service.claim_start(after.id, 2, owner=owner)
        async with fresh.session() as session:
            audit = await session.scalar(
                select(MemoryBudgetAuditRow).where(
                    MemoryBudgetAuditRow.action == "job.prelaunch_released"
                )
            )
            assert audit.result["never_launched"] and audit.result["owner_dead"]
            assert audit.result["groups_cleaned"] is False
        await service.create_job(
            "next",
            project_id="synthetic",
            resource_kind="fixture",
            resource_id="next",
            capabilities=capability,
        )
        assert (await service.reserve("next", capability)).state == "reserved"
        (tmp_path / "receipt.json").write_text(
            json.dumps(
                {
                    "mode": mode,
                    "checkpoint": checkpoint,
                    "child_exit": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "recovered": after.model_dump(mode="json"),
                    "audit": audit.result,
                },
                indent=2,
            )
        )
    finally:
        await fresh.dispose()


@pytest.mark.parametrize(
    "case",
    [
        "legacy-reserved",
        "legacy-starting",
        "foreign-origin",
        "uninspectable",
        "starting-unacknowledged",
    ],
)
async def test_unproven_or_crossed_launch_stays_charged(tmp_path, monkeypatch, case):
    database, service, capability = await setup(tmp_path)
    try:
        owner = MemoryLaunchOwner(
            process_id=2_000_000_000,
            process_birth="absent-synthetic-owner",
            process_origin=process_origin(),
            runner_fingerprint="a" * 64,
        )
        if case == "foreign-origin":
            owner = owner.model_copy(update={"process_origin": "foreign-pid-namespace"})
        await service.create_job(
            "held",
            project_id=None,
            resource_kind="fixture",
            resource_id="held",
            capabilities=capability,
        )
        job = await service.reserve(
            "held", capability, owner=None if case.startswith("legacy") else owner
        )
        if case in {"legacy-starting", "starting-unacknowledged"}:
            job = await service.claim_start(
                job.id, job.revision, owner=job.launch_owner
            )
        if case == "uninspectable":

            def unavailable(_pid):
                raise ProcessIdentityUnavailable("Synthetic unreadable procfs")

            monkeypatch.setattr("nebula.v3.process_identity.process_birth", unavailable)
        assert await MemoryReconciler(service, no_runner).once() == []
        after = await service.get_job(job.id)
        assert after.reserved_bytes == 32 * MIB
        assert after.state == (
            "uncertain" if case == "starting-unacknowledged" else job.state
        )
        assert after.evidence is None
    finally:
        await database.dispose()


async def test_live_duplicate_controller_cancellation_fences_the_pending_launch(
    tmp_path,
):
    database, service, _ = await setup(tmp_path)
    runners = [Runner(service), Runner(service)]
    tasks = []
    try:
        for runner in runners:
            tasks.append(controller_task(service, tmp_path, runner))
            await asyncio.wait_for(runner.entered.wait(), 5)
        assert await MemoryReconciler(service, no_runner).once() == []
        before = await service.get_job("crash-job")
        assert (
            before.state == "reserved" and before.launch_owner.process_id == os.getpid()
        )
        tasks[0].cancel()
        assert isinstance(
            (await asyncio.gather(tasks[0], return_exceptions=True))[0],
            asyncio.CancelledError,
        )
        runners[1].resume.set()
        assert isinstance(
            (await asyncio.gather(tasks[1], return_exceptions=True))[0],
            MemoryBudgetConflict,
        )
        after = await service.get_job("crash-job")
        assert (after.state, after.revision, after.reserved_bytes, after.outcome) == (
            "finished",
            3,
            0,
            "cancelled",
        )
        assert (
            after.evidence is None and sum(runner.launches for runner in runners) == 0
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await database.dispose()


async def test_claim_and_prelaunch_release_have_one_revision_winner(tmp_path):
    database, service, capability = await setup(tmp_path)
    second_database = Database(tmp_path / "ledger.db")
    second = MemoryBudgetService(second_database, administrator=lambda: ADMIN)
    try:
        owner = MemoryLaunchOwner(
            process_id=os.getpid(),
            process_birth=process_birth(os.getpid()),
            process_origin=process_origin(),
            runner_fingerprint="a" * 64,
        )
        await service.create_job(
            "race",
            project_id=None,
            resource_kind="fixture",
            resource_id="race",
            capabilities=capability,
        )
        job = await service.reserve("race", capability, owner=owner)
        with pytest.raises(MemoryBudgetConflict, match="another launch owner"):
            await second.claim_start(
                job.id,
                job.revision,
                owner=owner.model_copy(update={"runner_fingerprint": "b" * 64}),
            )
        results = await asyncio.gather(
            service.release_prelaunch(job.id, job.revision, owner=owner),
            second.claim_start(job.id, job.revision, owner=owner),
            return_exceptions=True,
        )
        assert sum(isinstance(value, MemoryBudgetConflict) for value in results) == 1
        current = await service.get_job(job.id)
        assert current.revision == 3
        assert (current.state, current.reserved_bytes) in {
            ("finished", 0),
            ("starting", 32 * MIB),
        }
    finally:
        await second_database.dispose()
        await database.dispose()


if __name__ == "__main__":
    assert sys.argv[1] == "--child"
    asyncio.run(child(Path(sys.argv[2]), sys.argv[3]))
