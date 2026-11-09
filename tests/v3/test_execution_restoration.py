"""Prepared controllers remain inert until their durable restoration is verified."""

from contextlib import asynccontextmanager
import asyncio
from dataclasses import asdict
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

from nebula.v3.database import ProjectHandoffRow
from nebula.v3.deployments import checkpoint_digest
from nebula.v3.runtime_coordination import CoordinationConflict, LeaseLost
from nebula.v3.runtime_project_handoff import project_sql_checkpoint
from tests.v3.test_execution_worker import runtime as runtime, until
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def transferred(runtime):
    async with runtime.coordination.database.session() as session:
        checkpoint = await project_sql_checkpoint(session, "project")
        session.add(
            ProjectHandoffRow(
                project_id="project",
                revision=1,
                source_runtime_id="source",
                source_fencing_token=0,
                target_runtime_id="old",
                target_version="fixture",
                phase="transferred",
                admission_held=True,
                blockers=[],
                checkpoint=checkpoint,
                admitted_turn_ids=[],
                updated_at_ms=0,
            )
        )
    return checkpoint_digest(checkpoint)


async def test_activation_requires_verified_checkpoint(runtime):
    digest, activations = await transferred(runtime), []

    @asynccontextmanager
    async def application(context, lease):
        app = FastAPI()

        async def activate():
            activations.append(lease)

        async def tick():
            pass

        app.state.runtime_activate = activate
        app.state.runtime_handoff = SimpleNamespace(restored_digest=digest, tick=tick)
        try:
            yield app
        finally:
            context.restore_cleanup_completed = True

    runtime.execution.application_factory = application
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "prepared")
    assert not activations
    with pytest.raises(CoordinationConflict):
        await runtime.execution.activate({})
    await runtime.coordination.verified(
        runtime.lease, restored_checkpoint_digest=digest
    )
    await runtime.execution.activate({})
    await until(lambda: runtime.execution.state == "ready")
    await runtime.execution.activate({})
    assert activations == [runtime.lease]


async def test_failed_clean_restoration_relinquishes_without_reviving_lease(runtime):
    await transferred(runtime)

    @asynccontextmanager
    async def unavailable(context, lease):
        try:
            raise RuntimeError("inert construction failure")
            yield
        finally:
            context.restore_cleanup_completed = True

    runtime.execution.application_factory = unavailable
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "failed")
    await runtime.execution.relinquish({})
    await runtime.coordination.coordination.heartbeat("old")
    with pytest.raises(LeaseLost):
        async with runtime.coordination.coordination.fenced(runtime.lease):
            pass
    assert runtime.execution.state == "fenced"


async def test_relinquish_keeps_verification_outside_its_audit_to_expiry_boundary(
    runtime, monkeypatch
):
    digest = await transferred(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.coordination.coordination._check

    async def barrier(session, lease, *, lock):
        await original(session, lease, lock=lock)
        if not lock and not entered.is_set():
            entered.set()
            await release.wait()

    monkeypatch.setattr(runtime.coordination.coordination, "_check", barrier)
    relinquish = asyncio.create_task(
        runtime.coordination.relinquish_failed_restoration(runtime.lease)
    )
    verification = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        verification = asyncio.create_task(
            runtime.coordination.verified(
                runtime.lease, restored_checkpoint_digest=digest
            )
        )
        await asyncio.sleep(0)
        assert not verification.done()
        release.set()
        await relinquish
        with pytest.raises(LeaseLost):
            await verification
    finally:
        release.set()
        await asyncio.gather(
            relinquish,
            *([verification] if verification else []),
            return_exceptions=True,
        )
