"""Butler's background checks reach a project whose Butler lives in Butler's home.

A Butler session reserved in ``BUTLER_HOME_PROJECT`` and focused on a project
runs on Butler's own worker. That project's worker cannot read Butler's home,
so the reservation shares its runtime through ``ButlerFocusRuntime``. With it
the project can host Nebula-wide checks, and a recovery case gets a Butler
chat in the project instead of failing with "no configured chat". See
docs/design/butler-home-worker.md.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from nebula.v3.butler_authority import policy_id
from nebula.v3.butler_oversight import (
    ButlerEventPayload,
    _append_project_event,
    project_state_id,
)
from nebula.v3.butler_recovery import (
    GLOBAL_STATE_ID,
    HOST_HINTS,
    ButlerRecoveryAgent,
    ButlerRecoveryService,
)
from nebula.v3.butler_sessions import (
    ButlerFocus,
    ButlerSessionReservation,
    focus_runtime_id,
    reserve_butler_session,
)
from nebula.v3.chat import ChatConfigurationError
from nebula.v3.database import Database
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import BUTLER_HOME_PROJECT
from nebula.v3.domain import (
    ButlerAuthorityPolicy,
    ButlerFocusRuntime,
    ButlerGlobalState,
    ButlerProjectState,
    ButlerRecoveryCase,
    ChatBackend,
    ChatSession,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    ScopePolicy,
    utc_now,
)
from nebula.v3.runtime_coordination import RuntimeCoordination
from nebula.v3.storage import NebulaStore
from tests.v3.test_butler_home_scope import home as home
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 2, 5)
WORKER = "oversight-worker"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_mandate(root) -> None:
    folder = root / ".butler"
    folder.mkdir(exist_ok=True)
    (folder / "BUTLER.md").write_text("Check idle work.", encoding="utf-8")


async def _act(store, project_id, authority="act"):
    await store.create(
        ButlerAuthorityPolicy(
            id=policy_id(project_id), engagement_id=project_id, authority=authority
        )
    )
    await store.create(
        ButlerProjectState(
            id=project_state_id(project_id),
            engagement_id=project_id,
            lease_owner=WORKER,
            lease_expires_at=utc_now() + timedelta(minutes=10),
        )
    )


async def _verified_provider(store, profile_id="provider"):
    profile = await store.get(ProviderProfile, profile_id)
    await store.update(
        ProviderProfile,
        profile.id,
        {
            "is_local": True,
            "metadata": {**profile.metadata, "default_model": "inert"},
            "capability_verifications": {
                "inert": ProviderCapabilityVerification(
                    model="inert", status=ProviderVerificationStatus.VERIFIED
                ).model_dump(mode="json")
            },
        },
    )


class _Stop(Exception):
    """Stops an investigation where it would start the model turn."""


def _agent(store, root):
    async def prepare_async(_request):
        raise _Stop

    return ButlerRecoveryAgent(
        store,
        provider_factory=lambda _: None,
        harness_runtime=None,
        artifact_store=None,
        work=SimpleNamespace(chat=SimpleNamespace(prepare_async=prepare_async)),
        memory=None,
        instructions_root=root,
    )


async def test_home_butler_focused_on_an_act_project_hosts_and_investigates(
    home,  # noqa: F811
    location,  # noqa: F811
    tmp_path,
):
    control, butler, _ = home
    await _verified_provider(control)
    await _act(control, "target")
    _write_mandate(tmp_path)

    home_session = await reserve_butler_session(
        butler,
        None,
        ButlerSessionReservation(
            engagement_id=BUTLER_HOME_PROJECT,
            backend=ChatBackend.PROVIDER,
            provider_id="provider",
            focus=ButlerFocus(project="target"),
        ),
    )
    shared = await control.get(ButlerFocusRuntime, focus_runtime_id("target"))
    assert (shared.session_id, shared.profile_id, shared.model) == (
        home_session.id,
        "provider",
        "inert",
    )

    coordinator = RuntimeCoordination(control.database)
    await coordinator.register_runtime("target-worker", "fixture")
    await coordinator.claim_project("target", "target-worker")
    worker = NebulaStore(
        Database(
            location,
            bootstrap=False,
            execution_runtime_id="target-worker",
            pool_settings=SETTINGS,
        )
    )
    try:
        # The target's worker cannot see Butler's home session itself.
        assert not [
            session
            async for session in worker.iter_readable_entities(
                ChatSession, engagement_id=BUTLER_HOME_PROJECT
            )
        ]
        recovery = ButlerRecoveryService(worker, instructions_root=tmp_path)
        assert await recovery._global_anchor() == "target"
        status = await recovery.global_status("target")
        assert (status["status"], status["host_project_id"]) == ("ready", "target")
        assert await recovery.fire_global_periodic_check("target", WORKER)

        assert await recovery.capture_events("target", WORKER) == 1
        (case,) = await worker.find_entities(
            ButlerRecoveryCase, {}, engagement_id="target"
        )
        agent = _agent(worker, tmp_path)
        with pytest.raises(_Stop):
            await agent._run(case)
        parent = await agent._butler_session("target")
        assert parent.engagement_id == "target"
        assert parent.metadata["control_assistant"] is True
        assert (parent.provider_profile_id, parent.model) == ("provider", "inert")
        child = await worker.get(
            ChatSession,
            (await worker.get(ButlerRecoveryCase, case.id)).investigation_session_id,
        )
        assert child.parent_session_id == parent.id
        # The next check reuses the project's Butler chat.
        assert (await agent._butler_session("target")).id == parent.id
    finally:
        await worker.event_history.close()
        await worker.database.dispose()


@pytest.fixture
async def store(tmp_path):
    value = NebulaStore(tmp_path / "core.db")
    try:
        yield value
    finally:
        await value.event_history.close()
        await value.database.dispose()


async def test_project_butler_chat_still_hosts_and_parents_checks(store, tmp_path):
    project = await store.create(Engagement(name="Selected"))
    await store.create(
        ProviderProfile(id="provider", name="Inert", provider_type="ollama")
    )
    await _verified_provider(store)
    await _act(store, project.id)
    own = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Butler",
            model="inert",
            provider_profile_id="provider",
            metadata={"control_assistant": True},
        )
    )
    _write_mandate(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert await recovery._global_anchor() == project.id
    assert (await recovery.global_status(project.id))["status"] == "ready"
    assert (await _agent(store, tmp_path)._butler_session(project.id)).id == own.id


async def test_without_a_host_checks_stop_and_status_names_the_fix(store, tmp_path):
    watched = await store.create(Engagement(name="Watched"))
    bare = await store.create(Engagement(name="Act without Butler"))
    await store.create(
        ProviderProfile(id="provider", name="Inert", provider_type="ollama")
    )
    await _verified_provider(store)
    await _act(store, watched.id, authority="watch")
    await _act(store, bare.id)
    # A home runtime for a project without Act does not make it a host.
    await store.create(
        ButlerFocusRuntime(
            id=focus_runtime_id(watched.id),
            focus_project=watched.id,
            source_project=BUTLER_HOME_PROJECT,
            session_id="home-session",
            backend=ChatBackend.PROVIDER,
            profile_id="provider",
            model="inert",
        )
    )
    _write_mandate(tmp_path)
    recovery = ButlerRecoveryService(store, instructions_root=tmp_path)
    assert await recovery._global_anchor() is None
    # BUTLER.md replaces per-project checks; with no host, none can run.
    for project in (watched, bare):
        assert not await recovery.fire_global_periodic_check(project.id, WORKER)
        assert not await recovery.fire_periodic_check(project.id, WORKER)

    status = await recovery.global_status(watched.id)
    assert (status["status"], status["host_project_id"]) == ("needs_act_host", None)
    assert (status["host_blocker"], status["host_hint"]) == (
        "not_act",
        HOST_HINTS["not_act"],
    )
    status = await recovery.global_status(bare.id)
    assert status["host_blocker"] == "no_butler"
    assert "Open Butler in this project" in status["host_hint"]
    with pytest.raises(ChatConfigurationError, match="no model for this project"):
        await _agent(store, tmp_path)._butler_session(bare.id)

    # A host another worker ran recently shows as ready from this project.
    await store.create(
        ButlerGlobalState(
            id=GLOBAL_STATE_ID,
            last_periodic_at=utc_now() - timedelta(minutes=2),
            last_project_id="elsewhere",
        )
    )
    status = await recovery.global_status(watched.id)
    assert (status["status"], status["host_project_id"]) == ("ready", "elsewhere")


@pytest.fixture
async def two_projects(location):  # noqa: F811
    control = NebulaStore(Database(location, pool_settings=SETTINGS))
    workers: dict[str, NebulaStore] = {}
    try:
        await control.create(
            ProviderProfile(id="provider", name="Inert", provider_type="ollama")
        )
        await _verified_provider(control)
        coordinator = RuntimeCoordination(control.database)
        for project in ("host", "other"):
            await control.create(Engagement(id=project, name=project))
            await _act(control, project)
            await coordinator.register_runtime(f"{project}-worker", "fixture")
            await coordinator.claim_project(project, f"{project}-worker")
            workers[project] = NebulaStore(
                Database(
                    location,
                    bootstrap=False,
                    execution_runtime_id=f"{project}-worker",
                    pool_settings=SETTINGS,
                )
            )
        yield control, workers
    finally:
        for value in (*workers.values(), control):
            await value.event_history.close()
            await value.database.dispose()


async def test_an_act_project_without_butler_investigates_on_the_hosts_runtime(
    two_projects, tmp_path
):
    """Live shape: one Act project has Butler chats, the other Act projects none."""

    control, workers = two_projects
    # A Butler chat saved before Butler shared its runtime.
    own = await control.create(
        ChatSession(
            engagement_id="host",
            title="Butler",
            model="inert",
            provider_profile_id="provider",
            metadata={"control_assistant": True},
        )
    )
    _write_mandate(tmp_path)
    host = ButlerRecoveryService(workers["host"], instructions_root=tmp_path)
    other = ButlerRecoveryService(workers["other"], instructions_root=tmp_path)
    agent = _agent(workers["other"], tmp_path)
    with pytest.raises(ChatConfigurationError, match="no model for this project"):
        await agent._butler_session("other")

    assert await host._global_anchor() == "host"
    assert await other._global_anchor() is None
    assert await host.fire_global_periodic_check("host", WORKER)
    shared = await control.get(ButlerFocusRuntime, focus_runtime_id("*"))
    assert (shared.source_project, shared.session_id) == ("host", own.id)
    # The other project's worker cannot read the host, but sees the clock.
    status = await other.global_status("other")
    assert (status["status"], status["host_project_id"]) == ("ready", "host")

    async with workers["other"].transaction(immediate=True) as transaction:
        await _append_project_event(
            transaction,
            engagement_id="other",
            event_type="goal.blocked",
            payload=ButlerEventPayload(
                project_id="other",
                source="core",
                actor_role="core",
                summary="A goal is blocked",
                verification_state="core_observed",
            ),
            idempotency_key="goal-blocked-1",
            now=utc_now(),
        )
    assert await other.capture_events("other", WORKER) == 1
    (case,) = await workers["other"].find_entities(
        ButlerRecoveryCase, {}, engagement_id="other"
    )
    assert case.signal == "goal.blocked"
    with pytest.raises(_Stop):
        await agent._run(case)
    parent = await agent._butler_session("other")
    assert (parent.engagement_id, parent.provider_profile_id, parent.model) == (
        "other",
        "provider",
        "inert",
    )
    saved = await workers["other"].get(ButlerRecoveryCase, case.id)
    child = await workers["other"].get(ChatSession, saved.investigation_session_id)
    assert child.parent_session_id == parent.id


async def test_a_shared_cloud_runtime_never_crosses_a_local_only_project(store):
    project = await store.create(Engagement(name="Local only"))
    scope = await store.create(ScopePolicy(engagement_id=project.id, local_only=True))
    await store.update(Engagement, project.id, {"scope_policy_id": scope.id})
    await store.create(
        ProviderProfile(id="cloud", name="Cloud", provider_type="openrouter")
    )
    await store.create(
        ButlerFocusRuntime(
            id=focus_runtime_id("*"),
            focus_project="*",
            source_project="elsewhere",
            session_id="butler-elsewhere",
            backend=ChatBackend.PROVIDER,
            profile_id="cloud",
            model="cloud-model",
        )
    )
    with pytest.raises(ChatConfigurationError, match="local models"):
        await _agent(store, None)._butler_session(project.id)
    assert not [
        session
        async for session in store.iter_readable_entities(
            ChatSession, engagement_id=project.id
        )
    ]


async def test_a_failed_investigation_keeps_its_cause_in_the_outcome(store):
    project = await store.create(Engagement(name="Selected"))
    await _act(store, project.id)
    case = await store.create(
        ButlerRecoveryCase(
            id="case-failed",
            engagement_id=project.id,
            trigger="periodic",
            signal="butler.global_periodic_check",
            source_event_id="event-failed",
            source_event_sequence=1,
            summary="Scheduled check",
            authority_revision=1,
            status="investigating",
        )
    )

    class _Failing:
        async def run(self, _case):
            raise ValueError("A delegated Butler investigation needs attention")

    service = ButlerRecoveryService(store, _Failing())
    await service._investigate_case(case)
    saved = await store.get(ButlerRecoveryCase, case.id)
    assert saved.status == "needs_attention"
    assert saved.outcome == (
        "Butler could not finish the investigation "
        "(ValueError: A delegated Butler investigation needs attention)."
    )
