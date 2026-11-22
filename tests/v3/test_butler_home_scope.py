"""Butler's home project: hidden, pinned, and able to read other projects.

Butler's chats run on the worker that owns ``BUTLER_HOME_PROJECT`` so a busy
project's serialized transactions never delay them. Inside ``observer_reads``
that worker reads other projects' committed rows without their locks, while
row locks and writes stay fenced to its own project. See
docs/design/butler-home-worker.md.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from nebula.v3.api_crud import _refuse_reserved_project
from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import (
    BUTLER_HOME_FEATURE,
    BUTLER_HOME_PROJECT,
    GLOBAL_EXECUTION_PROJECT,
)
from nebula.v3.domain import ChatSession, ChatTurn, Engagement, ProviderProfile
from nebula.v3.execution_scope import execution_context, observer_reads
from nebula.v3.runtime_coordination import (
    CoordinationConflict,
    LeaseLost,
    RuntimeCoordination,
)
from nebula.v3.search import project_search_document
from nebula.v3.storage import ConflictError, NebulaStore, NotFoundError
from tests.v3.test_deployment_lifecycle import manifest
from tests.v3.test_execution_scope import execution as execution
from tests.v3.test_mixed_release_idle_retirement import (
    CURRENT_FEATURES,
    mixed as mixed,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 2, 5)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def home(location):
    control = NebulaStore(Database(location, pool_settings=SETTINGS))
    runtime = None
    try:
        for project in (BUTLER_HOME_PROJECT, "target"):
            await control.create(Engagement(id=project, name=project))
            await control.create(
                ChatTurn(
                    id=f"turn-{project}",
                    engagement_id=project,
                    session_id=f"chat-{project}",
                    model="inert",
                    provider_profile_id="provider",
                )
            )
        await control.create(
            ProviderProfile(
                id="provider", name="Inert provider", provider_type="openrouter"
            )
        )
        coordinator = RuntimeCoordination(control.database)
        await coordinator.register_runtime("butler", "fixture")
        lease = await coordinator.claim_project(BUTLER_HOME_PROJECT, "butler")
        runtime = NebulaStore(
            Database(
                location,
                bootstrap=False,
                execution_runtime_id="butler",
                pool_settings=SETTINGS,
            )
        )
        yield control, runtime, lease
    finally:
        if runtime:
            await runtime.event_history.close()
            await runtime.database.dispose()
        await control.event_history.close()
        await control.database.dispose()


async def test_home_worker_sees_only_its_project_outside_observer_reads(home):
    _, runtime, _ = home
    assert (await runtime.get(ChatTurn, f"turn-{BUTLER_HOME_PROJECT}")).id
    with pytest.raises(NotFoundError):
        await runtime.get(ChatTurn, "turn-target")


@pytest.mark.parametrize("leased", [False, True])
async def test_observer_reads_see_other_projects_committed_rows(home, leased):
    _, runtime, lease = home
    if leased:
        with execution_context(lease), observer_reads():
            turn = await runtime.get(ChatTurn, "turn-target")
            projects = await runtime.list_entities(Engagement)
    else:
        with observer_reads():
            turn = await runtime.get(ChatTurn, "turn-target")
            projects = await runtime.list_entities(Engagement)
    assert turn.engagement_id == "target"
    # Butler's own project stays hidden even from Butler's project list.
    assert [project.id for project in projects] == ["target"]


async def test_observer_reads_cannot_write_another_projects_rows(home):
    control, runtime, _ = home
    before = await control.get(ChatTurn, "turn-target")
    with observer_reads():
        with pytest.raises((LeaseLost, CoordinationConflict, ConflictError)):
            await runtime.update(ChatTurn, "turn-target", {"model": "changed"})
        with pytest.raises((LeaseLost, CoordinationConflict)):
            await runtime.create(
                ChatTurn(
                    id="turn-injected",
                    engagement_id="target",
                    session_id="chat-target",
                    model="inert",
                    provider_profile_id="provider",
                )
            )
        # Writes to Butler's own project still work while observing.
        saved = await runtime.update(
            ChatTurn, f"turn-{BUTLER_HOME_PROJECT}", {"model": "home-changed"}
        )
    assert saved.model == "home-changed"
    after = await control.get(ChatTurn, "turn-target")
    assert (after.model, after.revision) == (before.model, before.revision)
    with pytest.raises(NotFoundError):
        await control.get(ChatTurn, "turn-injected")


async def test_observer_row_locks_keep_the_owned_project_predicate(home):
    _, runtime, _ = home
    with observer_reads():
        async with runtime.database.session() as session:
            locked_target = await session.scalar(
                select(EntityRow).where(EntityRow.id == "turn-target").with_for_update()
            )
            locked_home = await session.scalar(
                select(EntityRow)
                .where(EntityRow.id == f"turn-{BUTLER_HOME_PROJECT}")
                .with_for_update()
            )
            plain_target = await session.scalar(
                select(EntityRow).where(EntityRow.id == "turn-target")
            )
    # A locking read of another project's row matches nothing, so Butler can
    # never block that project's worker; its own rows lock as usual.
    assert locked_target is None
    assert locked_home is not None
    assert plain_target is not None


async def test_observer_reads_are_refused_on_any_other_worker(execution):
    _, runtime, _, _ = execution
    with observer_reads():
        with pytest.raises(CoordinationConflict, match="Butler home worker"):
            await runtime.get(ChatTurn, "turn-other")
    # Leaving the context restores the ordinary owned-project scope.
    with pytest.raises(NotFoundError):
        await runtime.get(ChatTurn, "turn-other")


async def test_store_listings_hide_reserved_projects(location):
    store = NebulaStore(Database(location, pool_settings=SETTINGS))
    try:
        for project in (BUTLER_HOME_PROJECT, GLOBAL_EXECUTION_PROJECT, "mine"):
            await store.create(Engagement(id=project, name=project))
        listed = [project.id for project in await store.list_entities(Engagement)]
        found = [
            project.id
            for project in await store.find_entities(Engagement, {"status": "draft"})
        ]
        scanned = [
            project.id async for project in store.iter_readable_entities(Engagement)
        ]
        assert listed == found == scanned == ["mine"]
        assert await store.count_entities(Engagement) == 1
        assert await store.count(Engagement) == 1
        assert (await store.overview())["counts"]["engagements"] == 1
        # Reading Core's own project by id still works.
        assert (await store.get(Engagement, BUTLER_HOME_PROJECT)).id == (
            BUTLER_HOME_PROJECT
        )
    finally:
        await store.event_history.close()
        await store.database.dispose()


def test_reserved_projects_are_not_searchable_or_editable():
    assert (
        project_search_document(
            "engagements", {"id": BUTLER_HOME_PROJECT, "name": "Butler"}
        )
        is None
    )
    assert (
        project_search_document("engagements", {"id": "mine", "name": "Mine"})
        is not None
    )
    for project in (BUTLER_HOME_PROJECT, GLOBAL_EXECUTION_PROJECT):
        with pytest.raises(ConflictError):
            _refuse_reserved_project(Engagement, project)
    _refuse_reserved_project(Engagement, "mine")
    _refuse_reserved_project(ChatSession, BUTLER_HOME_PROJECT)


def test_release_builds_declare_butler_home():
    import json

    declared = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src/nebula/v3/runtime-compatibility.json"
        ).read_text()
    )["manifest"]["features"]
    assert BUTLER_HOME_FEATURE in declared


async def test_admission_starts_butler_home_only_on_a_release_that_hosts_it(
    mixed,
):
    from nebula.v3.runtime_auto_admission import RuntimeAutoAdmission

    await mixed.coordinator.stage(
        manifest("butler", features=[*CURRENT_FEATURES, BUTLER_HOME_FEATURE]),
        Path("/synthetic/releases/butler"),
        "a" * 64,
    )
    await mixed.store.create(Engagement(id=BUTLER_HOME_PROJECT, name="Butler"))
    await mixed.store.create(
        ChatSession(
            engagement_id=BUTLER_HOME_PROJECT,
            title="Butler thread",
            provider_profile_id="synthetic-provider",
            model="synthetic-model",
        )
    )
    prepared: list[str] = []

    async def prepare_execution(version):
        prepared.append(version)
        raise RuntimeError("synthetic: no process host")

    def admission(version):
        async def retire_idle_project(_project_id):
            return True

        async def reconcile_idle_project_retirements():
            return []

        return RuntimeAutoAdmission(
            SimpleNamespace(
                coordinator=mixed.coordinator,
                store=mixed.store,
                prepare_execution=prepare_execution,
                host=SimpleNamespace(
                    retire_idle_project=retire_idle_project,
                    reconcile_idle_project_retirements=(
                        reconcile_idle_project_retirements
                    ),
                ),
            ),
            default_version=version,
        )

    older = admission("current")
    assert not await older._hosts_butler_home("current")
    for _tick in range(2):
        await older.reconcile()
    assert prepared == []

    newer = admission("butler")
    assert await newer._hosts_butler_home("butler")
    await newer.reconcile()
    assert prepared == ["butler"]
