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
from nebula.v3.butler_oversight import project_state_id
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
    assert (shared.home_session_id, shared.profile_id, shared.model) == (
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
            home_session_id="home-session",
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
