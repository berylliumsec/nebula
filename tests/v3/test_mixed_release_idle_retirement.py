"""Idle-project retirement during a rolling release with older workers resident.

A rolling release restarts the supervisor on the new release while projects
stay on the previous release's execution workers until the deployer hands
them off. Releases before chat-scoped admission can quiesce only under a host
shutdown, so asking one to retire an idle project is refused every time. The
new supervisor must respect what each worker's release supports, and must hold
a project's admission only while its worker can actually quiesce.
"""

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from nebula.v3.database import (
    Database,
    DeploymentHostRow,
    DeploymentReceiptRow,
    ProjectOwnershipRow,
    RuntimeProcessRow,
)
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployments import DeploymentCoordinator
from nebula.v3.domain import ChatSession, Engagement
from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission
from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_ipc import RuntimeRpcClient
from nebula.v3.runtime_processes import RuntimeProcessHost
from nebula.v3.storage import NebulaStore
from tests.v3.test_deployment_lifecycle import manifest
from tests.v3.test_execution_worker import runtime as runtime, until
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 5, 2)
# The live release 5ec14b4 declares project.quiesce/v1 (host-shutdown
# quiescence) but cannot quiesce under a per-project retirement hold.
OLDER_FEATURES = ["project.handoff/v1", "project.quiesce/v1"]
# Releases since chat-scoped admission quiesce under a project retirement hold.
IDLE_RETIREMENT_FEATURE = "project.idle-retirement/v1"
CURRENT_FEATURES = [*OLDER_FEATURES, IDLE_RETIREMENT_FEATURE]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def mixed(location):
    database = Database(location, pool_settings=SETTINGS)
    await database.initialize()
    database._bootstrap = False
    store = NebulaStore(database)
    coordinator = DeploymentCoordinator(database)
    for version, features in (("older", OLDER_FEATURES), ("current", CURRENT_FEATURES)):
        await coordinator.stage(
            manifest(version, features=features),
            Path("/synthetic/releases") / version,
            "a" * 64,
        )
    async with coordinator.coordination._write() as session:
        host = await coordinator._host(session)
        # A promoted API keeps reconcile on its project admission path.
        host.api_runtime_id = "synthetic-api"
    try:
        yield SimpleNamespace(database=database, store=store, coordinator=coordinator)
    finally:
        await store.event_history.close()
        await database.dispose()


async def owned_project(value, project_id, runtime_id, version, *, chat="none"):
    await value.store.create(Engagement(id=project_id, name="Synthetic " + project_id))
    if chat != "none":
        await value.store.create(
            ChatSession(
                engagement_id=project_id,
                title="Synthetic conversation",
                provider_profile_id="synthetic-provider",
                model="synthetic-model",
                metadata=(
                    {"archived_at": "2026-11-14T00:00:00+00:00"}
                    if chat == "archived"
                    else {}
                ),
            )
        )
    await value.coordinator.coordination.register_runtime(runtime_id, version)
    async with value.database.session() as session:
        session.add(
            RuntimeProcessRow(
                runtime_id=runtime_id,
                version=version,
                role="execution",
                pid=0,
                process_birth="synthetic-no-process",
                endpoint="unix:///synthetic/" + runtime_id,
                credential_reference="synthetic-" + runtime_id,
                state="ready",
                updated_at_ms=0,
            )
        )
    return await value.coordinator.coordination.claim_project(project_id, runtime_id)


def admission(value, retired):
    async def retire_idle_project(project_id):
        retired.append(project_id)
        return True

    async def reconcile_idle_project_retirements():
        return []

    return RuntimeAutoAdmission(
        SimpleNamespace(
            coordinator=value.coordinator,
            store=value.store,
            host=SimpleNamespace(
                retire_idle_project=retire_idle_project,
                reconcile_idle_project_retirements=reconcile_idle_project_retirements,
            ),
        ),
        default_version="current",
    )


async def test_new_supervisor_never_asks_an_older_release_worker_to_retire(mixed):
    # Rehearsal shape: an archived-chat project and a chatless project owned by
    # live-release workers, next to an idle project already on the new release.
    await owned_project(mixed, "archived", "older-archived", "older", chat="archived")
    await owned_project(mixed, "chatless", "older-chatless", "older")
    await owned_project(mixed, "migrated", "current-migrated", "current")
    await owned_project(mixed, "open", "older-open", "older", chat="open")
    retired = []
    automatic = admission(mixed, retired)

    for _tick in range(3):
        await automatic.reconcile()

    # Only the worker whose release can quiesce outside a host shutdown is
    # asked; the older workers wait for the deployer's handoff instead of a
    # retirement request every tick.
    assert retired == ["migrated"] * 3
    async with mixed.database.session() as session:
        assert not list(
            await session.scalars(
                select(ProjectOwnershipRow.project_id).where(
                    ProjectOwnershipRow.retirement_requested.is_(True)
                )
            )
        )


class _Worker:
    """The execution worker's status and quiescence replies over RPC."""

    def __init__(self, coordinator, lease, state):
        self.coordinator, self.lease, self.state = coordinator, lease, state
        self.calls = []

    def status(self):
        return {
            "runtime_id": self.lease.runtime_id,
            "state": self.state,
            "lease": {
                "project_id": self.lease.project_id,
                "runtime_id": self.lease.runtime_id,
                "fencing_token": self.lease.fencing_token,
            },
            "cleanup_complete": False,
            "quiesce_checkpoint": None,
            "failure": None,
        }

    async def rpc(self, runtime_id):
        assert runtime_id == self.lease.runtime_id
        worker = self

        class Client:
            async def call(self, method, payload, timeout):
                worker.calls.append(method)
                if method == "execution.quiesce/v1":
                    # ExecutionWorker.quiesce refuses before stopping anything
                    # when the project has not activated yet.
                    return {
                        **worker.status(),
                        "quiesce_blockers": [
                            "Only a ready idle project can quiesce; its worker is "
                            + worker.state
                        ],
                    }
                assert method == "execution.status/v1"
                return worker.status()

            async def close(self):
                pass

        return Client()


async def retirement_receipts(value, project_id):
    async with value.database.session() as session:
        return list(
            await session.scalars(
                select(DeploymentReceiptRow.action)
                .where(
                    DeploymentReceiptRow.result["project_id"].as_string() == project_id
                )
                .order_by(DeploymentReceiptRow.occurred_at_ms)
            )
        )


@pytest.mark.parametrize("state", ["prepared", "starting"])
async def test_handed_off_target_is_not_held_until_it_activates(mixed, state):
    # A verified handoff target stays prepared until the supervisor activates
    # it. Holding its admission then left the hold in place with no way to
    # abort it, so reads and chats for the project failed until it activated.
    lease = await owned_project(mixed, "handed-off", "current-target", "current")
    worker = _Worker(mixed.coordinator, lease, state)
    retired = []

    async def retire(runtime_id):
        retired.append(runtime_id)

    host = SimpleNamespace(coordinator=mixed.coordinator, rpc=worker.rpc, retire=retire)
    for _tick in range(3):
        assert await RuntimeProcessHost.retire_idle_project(host, "handed-off") is False

    assert retired == [] and "execution.quiesce/v1" not in worker.calls
    assert await retirement_receipts(mixed, "handed-off") == []
    async with mixed.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "handed-off")
        assert (owner.runtime_id, owner.retirement_requested) == (
            "current-target",
            False,
        )

    # Once active and idle, the same worker retires normally.
    worker.state = "ready"
    assert await mixed.coordinator.request_project_retirement(lease) is True


@pytest.mark.parametrize("state", ["prepared", "starting", "failed"])
async def test_refusal_before_quiescence_releases_the_hold_in_any_state(mixed, state):
    lease = await owned_project(mixed, "held", "current-held", "current")
    assert await mixed.coordinator.request_project_retirement(lease) is True
    worker = _Worker(mixed.coordinator, lease, state)

    await mixed.coordinator.abort_project_retirement(
        lease,
        worker_status={
            **worker.status(),
            "quiesce_blockers": ["Only a ready idle project can quiesce"],
        },
    )

    async with mixed.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "held")
        assert (owner.runtime_id, owner.retirement_requested) == ("current-held", False)
    assert await retirement_receipts(mixed, "held") == [
        "project.retirement_requested",
        "project.retirement_aborted",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"state": "quiescing"},
        {"state": "closed"},
        {"quiesce_checkpoint": {"project_id": "held"}},
        {"quiesce_blockers": []},
        {"runtime_id": "another-worker"},
    ],
)
async def test_a_worker_that_began_quiescing_keeps_its_hold(mixed, change):
    lease = await owned_project(mixed, "held", "current-held", "current")
    assert await mixed.coordinator.request_project_retirement(lease) is True
    worker = _Worker(mixed.coordinator, lease, "ready")
    status = {
        **worker.status(),
        "quiesce_blockers": ["A provider response is still active"],
        **change,
    }

    with pytest.raises(CoordinationConflict):
        await mixed.coordinator.abort_project_retirement(lease, worker_status=status)

    async with mixed.database.session() as session:
        assert (await session.get(ProjectOwnershipRow, "held")).retirement_requested
        assert (await session.get(DeploymentHostRow, "host")) is not None


async def prepared_target(runtime):
    """Attach the real execution worker, which waits for activation."""

    @asynccontextmanager
    async def application(context, lease):
        app = FastAPI()
        chat = SimpleNamespace(
            _active_provider_turns={}, _naming_tasks=set(), _precompactions={}
        )

        async def tick():
            pass

        async def activate():
            pass

        app.state.runtime_handoff = SimpleNamespace(
            chat=chat, handles=lambda: {}, restored_digest="a" * 64, tick=tick
        )
        # A handoff or recovery target restores without starting producers
        # until the supervisor activates it.
        app.state.runtime_activate = activate
        try:
            yield app
        finally:
            context.restore_cleanup_completed = True

    runtime.execution.application_factory = application
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "prepared")

    async def rpc(runtime_id):
        assert runtime_id == runtime.lease.runtime_id
        return RuntimeRpcClient(
            "unix:///isolated/worker.sock",
            b"k" * 32,
            transport=httpx.ASGITransport(
                app=runtime.context.rpc.app, raise_app_exceptions=False
            ),
        )

    retired = []

    async def retire(runtime_id):
        retired.append(runtime_id)

    return SimpleNamespace(
        host=SimpleNamespace(coordinator=runtime.coordination, rpc=rpc, retire=retire),
        retired=retired,
        rpc=rpc,
    )


async def test_real_prepared_worker_is_left_unheld_by_idle_retirement(runtime):
    target = await prepared_target(runtime)

    for _tick in range(3):
        assert (
            await RuntimeProcessHost.retire_idle_project(target.host, "project")
            is False
        )

    assert target.retired == []
    assert runtime.execution.state == "prepared"
    async with runtime.coordination.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "project")
        assert (owner.runtime_id, owner.retirement_requested) == ("old", False)
        assert not list(
            await session.scalars(
                select(DeploymentReceiptRow.action).where(
                    DeploymentReceiptRow.action.like("project.retirement%")
                )
            )
        )


async def test_real_prepared_worker_refusal_releases_an_existing_hold(runtime):
    target = await prepared_target(runtime)
    # A hold set before this release's readiness check, by an earlier host.
    assert await runtime.coordination.request_project_retirement(runtime.lease)
    client = await target.rpc("old")
    try:
        refusal = await client.call("execution.quiesce/v1", {}, timeout=5)
    finally:
        await client.close()
    assert refusal["state"] == "prepared" and refusal["quiesce_blockers"]

    await runtime.coordination.abort_project_retirement(
        runtime.lease, worker_status=refusal
    )

    async with runtime.coordination.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "project")
        assert (owner.runtime_id, owner.retirement_requested) == ("old", False)
    assert runtime.execution.state == "prepared"
