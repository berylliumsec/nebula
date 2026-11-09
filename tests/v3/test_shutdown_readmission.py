"""Quiesced projects return automatically without resetting durable fences."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nebula.v3.database import (
    DeploymentHostRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
)
from nebula.v3.domain import (
    ChatGoal,
    ChatSchedule,
    ChatTurn,
    Engagement,
    utc_now,
)
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from tests.v3.runtime_continuity_fixture import completed_response
from tests.v3.runtime_readmission_fixture import OwnedReadmissionCore, candidate_bundle
from tests.v3.test_deployment_lifecycle import (
    host as host,
    manifest,
    stop_worker,
    worker,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def admission(host):
    coordinator, store = host
    await coordinator.stage(
        manifest("readmit", features=["project.quiesce/v1"]),
        Path("/isolated/readmit"),
        "d" * 64,
    )
    await coordinator.coordination.register_runtime("api", "readmit", role="api")
    await coordinator.target_execution("readmit")
    async with coordinator.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "api"
    calls, leases = [], {}

    async def prepare(version):
        identity = "auto-" + str(len(calls))
        calls.append(identity)
        await coordinator.coordination.register_runtime(identity, version)
        return SimpleNamespace(runtime_id=identity)

    async def attach(project_id, runtime_id):
        leases[project_id] = await coordinator.coordination.claim_project(
            project_id, runtime_id
        )
        return leases[project_id]

    automatic = RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=coordinator,
            store=store,
            prepare_execution=prepare,
            attach_execution=attach,
        ),
        default_version="readmit",
    )
    return SimpleNamespace(
        coordinator=coordinator,
        store=store,
        automatic=automatic,
        calls=calls,
        leases=leases,
        host=host,
    )


async def used(value, *, actual_process=False):
    if actual_process:
        runtime_id = await worker(value.host, "readmit")
    else:
        runtime_id = "previous-owner"
        await value.coordinator.coordination.register_runtime(runtime_id, "readmit")
    return await value.coordinator.coordination.claim_project("one", runtime_id)


async def quiesce(value, lease):
    await value.coordinator.prepare_shutdown(
        expected_revision=(await value.coordinator.status()).revision
    )
    checkpoint = await value.coordinator.quiesce_checkpoint(lease, local_blockers=[])
    await value.coordinator.release_quiesced_project(lease, checkpoint=checkpoint)


async def reopen_sql(value):
    await value.coordinator.begin_shutdown(
        expected_revision=(await value.coordinator.status()).revision
    )
    await value.coordinator.resume_after_shutdown()
    async with value.coordinator.database.session() as session:
        (await session.get(DeploymentHostRow, "host")).api_runtime_id = "api"


async def test_quiesced_and_fresh_projects_readmit_without_resetting_fences(admission):
    value = admission
    lease = await used(value)
    await quiesce(value, lease)
    await reopen_sql(value)
    for project in ("one", "two"):
        await value.coordinator.coordination.submit(
            project_id=project,
            actor_id="operator",
            idempotency_key="reopened-" + project,
            name="inert",
            payload={},
        )
    await value.automatic.reconcile()
    assert value.leases["one"].fencing_token == lease.fencing_token + 1
    assert value.leases["two"].fencing_token == 1
    assert len(value.calls) == 2
    with pytest.raises(LeaseLost):
        async with value.coordinator.coordination.fenced(lease):
            pass
    async with value.coordinator.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "one")).command_sequence == 1


@pytest.mark.parametrize("blocked", ["expired-owner", "uncertain-effect", "archived"])
async def test_non_null_or_archived_projects_remain_held(admission, blocked):
    value = admission
    lease = await used(value)
    if blocked == "archived":
        await quiesce(value, lease)
        await reopen_sql(value)
        await value.store.update(Engagement, "one", {"status": "archived"})
    elif blocked == "expired-owner":
        async with value.coordinator.database.session() as session:
            (await session.get(ProjectOwnershipRow, "one")).lease_expires_at_ms = 0
    else:
        receipt = await value.coordinator.coordination.submit(
            project_id="one",
            actor_id="operator",
            idempotency_key="uncertain",
            name="inert",
            payload={},
        )
        async with value.coordinator.database.session() as session:
            row = await session.get(RuntimeCommandRow, receipt.command_id)
            row.state, row.runtime_id, row.fencing_token = (
                "uncertain",
                lease.runtime_id,
                lease.fencing_token,
            )
        with pytest.raises(CoordinationConflict):
            await value.coordinator.prepare_shutdown(
                expected_revision=(await value.coordinator.status()).revision
            )
    await value.automatic.reconcile()
    assert not value.calls
    async with value.coordinator.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "one")
        assert owner.fencing_token == lease.fencing_token
        assert owner.runtime_id == (None if blocked == "archived" else lease.runtime_id)


async def test_partial_shutdown_abort_keeps_unknown_worker_until_positive_retirement(
    admission,
):
    value = admission
    lease = await used(value, actual_process=True)
    await quiesce(value, lease)
    await value.automatic.reconcile()
    assert not value.calls  # The global shutdown hold is still authoritative.
    await value.coordinator.abort_shutdown()
    await value.automatic.reconcile()
    assert not value.calls  # The unassigned closed/unknown worker remains resident.
    proof = await value.coordinator.begin_retirement(lease.runtime_id)
    with pytest.raises(CoordinationConflict, match="still alive"):
        await value.coordinator.retired(proof)
    stop_worker(value.host, lease.runtime_id)
    await value.coordinator.retired(proof)
    await value.automatic.reconcile()
    assert value.leases["one"].fencing_token == lease.fencing_token + 1


@pytest.fixture
async def core(tmp_path, location):
    from scripts.build_product_knowledge import ensure
    from nebula.v3.knowledge_index import _default_model_ready

    assert _default_model_ready(), (
        "Use only the already-cached model for bundle preparation"
    )
    ensure()
    value = OwnedReadmissionCore(
        tmp_path / "core", location, candidate_bundle(tmp_path)
    )
    try:
        await value.initialize()
        yield value
    finally:
        await value.close()


async def test_actual_core_shutdown_reopen_readmits_existing_project_and_preserves_pauses(
    core,
):
    project = (
        await completed_response(
            core.client,
            await core.client.post(
                "/api/v1/engagements",
                json={"name": "Existing readmission project", "metadata": {}},
                headers={"Idempotency-Key": "create-project"},
            ),
        )
    ).json()
    provider = (
        await completed_response(
            core.client,
            await core.client.post(
                "/api/v1/providers",
                json={
                    "name": "Inert readmission provider",
                    "provider_type": "vllm",
                    "is_local": True,
                    "model_allowlist": ["model-a"],
                },
                headers={"Idempotency-Key": "create-provider"},
            ),
        )
    ).json()

    async def chat(key):
        return await completed_response(
            core.client,
            await core.client.post(
                "/api/v1/chat/completions",
                json={
                    "engagement_id": project["id"],
                    "provider_id": provider["id"],
                    "model": "model-a",
                    "include_knowledge": False,
                    "stream": True,
                    "messages": [{"role": "user", "content": key}],
                },
                headers={"Idempotency-Key": key},
            ),
        )

    first = await chat("before-safe-shutdown")
    assert "event: done" in first.text and '"delta":"First "' in first.text
    turns = await core.store.list_entities(ChatTurn, engagement_id=project["id"])
    assert len(turns) == 1 and turns[0].status == "complete"
    paused = await core.store.create(
        ChatGoal(
            engagement_id=project["id"],
            session_id=turns[0].session_id,
            objective="Remain paused",
            completion_criteria=["No automatic resume"],
            status="paused",
            elapsed_seconds=12,
        )
    )
    disabled = await core.store.create(
        ChatSchedule(
            engagement_id=project["id"],
            session_id=turns[0].session_id,
            provider_profile_id=provider["id"],
            model="model-a",
            interval_seconds=3600,
            next_run_at=utc_now(),
            enabled=False,
        )
    )
    async with core.database.session() as session:
        row = await session.get(ProjectOwnershipRow, project["id"])
        old = ProjectLease(project["id"], row.runtime_id, row.fencing_token)
        processes = list(await session.scalars(select(RuntimeProcessRow)))
        old_processes = [(row.pid, row.process_birth) for row in processes]
    from nebula.v3.deployments import DeploymentCoordinator
    from nebula.v3.process_identity import same_process

    await core.wait_for_chat_maintenance(project["id"])
    await core.stop("stop-used-project")
    assert all(not same_process(pid, birth) for pid, birth in old_processes)
    async with core.database.session() as session:
        row = await session.get(ProjectOwnershipRow, project["id"])
        assert row.runtime_id is None and row.fencing_token == old.fencing_token
        assert all(
            row.state == "retired"
            for row in await session.scalars(select(RuntimeProcessRow))
        )
    await core.start()
    async with asyncio.timeout(45):
        while True:
            async with core.database.session() as session:
                row = await session.get(ProjectOwnershipRow, project["id"])
                if row.runtime_id is not None:
                    new = ProjectLease(project["id"], row.runtime_id, row.fencing_token)
                    break
            await asyncio.sleep(0.05)
    assert (
        new.runtime_id != old.runtime_id and new.fencing_token == old.fencing_token + 1
    )
    with pytest.raises(LeaseLost):
        async with DeploymentCoordinator(core.database).coordination.fenced(old):
            pass
    assert (
        await core.store.get(ChatGoal, paused.id)
    ).model_dump() == paused.model_dump()
    assert (
        await core.store.get(ChatSchedule, disabled.id)
    ).model_dump() == disabled.model_dump()
    second = await chat("after-automatic-readmission")
    assert "event: done" in second.text and '"delta":"last"' in second.text
    after = await core.store.list_entities(ChatTurn, engagement_id=project["id"])
    assert len(after) == 2 and all(turn.status == "complete" for turn in after)
    assert turns[0].id in {turn.id for turn in after}
    await core.wait_for_chat_maintenance(project["id"])
    await core.stop("stop-reopened-project")
    print(
        {
            "origin": core.origin,
            "old_fence": old.fencing_token,
            "new_fence": new.fencing_token,
            "old_workers_absent": True,
            "paused_preserved": True,
            "disabled_preserved": True,
            "completed_turns": len(after),
            "database": core.database.engine.dialect.name,
        }
    )
