"""A running goal and a queued request say when no worker is executing them.

The API worker answers the goal read and a saved request's status from SQL,
without the project's worker. A goal stored as ``running`` kept reading
"running" while the project's worker was gone or held, and a queued goal
action read only ``queued``. Both now carry why execution is waiting.
"""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import update

from nebula.v3.database import (
    DeploymentOperationRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
)
from nebula.v3.domain import ChatGoal, ChatGoalStatus, ChatSession, Engagement
from nebula.v3.runtime_coordination import RuntimeCoordination
from tests.v3.test_deployment_inventory_reads import AUTH, core as core
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def committed(core):
    async with core.database.session() as session:
        yield session
        await session.commit()


async def running_goal(store, status=ChatGoalStatus.RUNNING):
    await store.create(Engagement(id="project", name="Synthetic project"))
    session = await store.create(
        ChatSession(
            engagement_id="project",
            title="Goal chat",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    await store.create(
        ChatGoal(
            engagement_id="project",
            session_id=session.id,
            objective="Synthetic objective",
            completion_criteria=["Synthetic criterion"],
            status=status,
        )
    )
    return session


async def ready_worker(core):
    """A registered, leased, ready execution worker owning the project."""
    coordination = RuntimeCoordination(core.database)
    await coordination.register_runtime("worker", "fixture")
    lease = await coordination.claim_project("project", "worker")
    async with committed(core) as session:
        session.add(
            RuntimeProcessRow(
                runtime_id="worker",
                version="fixture",
                role="execution",
                pid=1,
                process_birth="birth",
                endpoint="http://127.0.0.1:9",
                credential_reference="credential",
                state="ready",
                updated_at_ms=1,
            )
        )
    return lease


async def goal_read(core, session_id):
    response = await core.client.get(
        f"/api/v1/chat/sessions/{session_id}/goal", headers=AUTH
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_running_goal_reads_waiting_until_a_ready_worker_owns_it(core):
    session = await running_goal(core.store)

    goal = await goal_read(core, session.id)
    assert goal["status"] == "running"
    assert goal["execution"]["reason"] == "starting"

    lease = await ready_worker(core)
    assert (await goal_read(core, session.id))["execution"] is None

    # A held transfer admits no work even with a live owner.
    async with committed(core) as db:
        db.add(
            ProjectHandoffRow(
                project_id="project",
                revision=1,
                source_runtime_id="old",
                source_fencing_token=1,
                target_runtime_id="worker",
                target_version="fixture",
                phase="transferred",
                admission_held=True,
                blockers=["Synthetic restoration blocker"],
                checkpoint={},
                updated_at_ms=1,
            )
        )
    held = (await goal_read(core, session.id))["execution"]
    assert held["reason"] == "held"
    assert "Synthetic restoration blocker" in held["detail"]
    async with committed(core) as db:
        await db.execute(
            update(ProjectHandoffRow).values(admission_held=False, phase="verified")
        )

    # The owner's lease expired: recovery is pending, nothing is running.
    async with committed(core) as db:
        await db.execute(update(ProjectOwnershipRow).values(lease_expires_at_ms=0))
    assert (await goal_read(core, session.id))["execution"]["reason"] == "starting"

    # Automatic recovery of that exact owner stopped without a result.
    async with committed(core) as db:
        db.add(
            DeploymentOperationRow(
                operation_id="auto-recovery:synthetic",
                actor_id="core",
                action="reconcile_project",
                request_hash="0" * 64,
                parameters={
                    "expected_owner": {
                        "project_id": "project",
                        "runtime_id": "worker",
                        "fencing_token": lease.fencing_token,
                    }
                },
                state="uncertain",
                claim_owner="core",
                result={"reason": "Synthetic stream needs reconciliation"},
                created_at_ms=1,
                updated_at_ms=1,
            )
        )
    uncertain = (await goal_read(core, session.id))["execution"]
    assert uncertain["reason"] == "held"
    assert "Synthetic stream needs reconciliation" in uncertain["detail"]


async def test_paused_goal_has_no_execution_state(core):
    session = await running_goal(core.store, status=ChatGoalStatus.PAUSED)
    goal = await goal_read(core, session.id)
    assert (goal["status"], goal["execution"]) == ("paused", None)


async def test_queued_goal_action_status_says_why_it_waits(core):
    session = await running_goal(core.store)
    goal = await goal_read(core, session.id)
    accepted = await core.client.post(
        f"/api/v1/chat/sessions/{session.id}/goal/actions",
        json={"expected_revision": goal["revision"], "action": "pause"},
        headers={**AUTH, "Idempotency-Key": "synthetic-pause"},
    )
    assert accepted.status_code == 202, accepted.text
    command = accepted.headers["X-Nebula-Command-Id"]
    status = await core.client.get(f"/api/v1/runtime/requests/{command}", headers=AUTH)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["state"] == "queued"
    assert body["execution"]["reason"] == "starting"
    assert "worker" in body["execution"]["detail"]
