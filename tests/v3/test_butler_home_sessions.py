"""Butler home sessions: created on first use, focused on one project.

A Butler session reserved in ``BUTLER_HOME_PROJECT`` runs on Butler's own
worker and reads its focus project through observer reads. Changes to the
focus project are refused until Butler's action command exists, and a
local-only project never reaches a cloud model through Butler's home. See
docs/design/butler-home-worker.md.
"""

import asyncio
from types import SimpleNamespace

import pytest

from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.butler_home import (
    BUTLER_HOME_FLAG,
    butler_home_enabled,
    ensure_butler_home,
    is_home_reservation,
    target_release_hosts_butler_home,
)
from nebula.v3.butler_sessions import (
    ButlerFocus,
    ButlerSessionReservation,
    reserve_butler_session,
)
from nebula.v3.chat import butler_focus_project
from nebula.v3.database import Database, DeploymentHostRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.deployment_protocol import BUTLER_HOME_PROJECT
from nebula.v3.domain import (
    ChatBackend,
    ChatSession,
    ChatTurn,
    Engagement,
    EngagementStatus,
    ProviderProfile,
    ScopePolicy,
)
from nebula.v3.runtime_platform import RuntimePlatform, RuntimePlatformError
from nebula.v3.setup import create_engagement_with_default_scope
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError, RiskClass, ToolSpec
from tests.v3.test_butler_home_scope import home as home
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
SETTINGS = PoolSettings(5, 12, 2, 5)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def store(location):
    value = NebulaStore(Database(location, pool_settings=SETTINGS))
    try:
        yield value
    finally:
        await value.event_history.close()
        await value.database.dispose()


async def test_home_project_is_created_once_and_stays_hidden(store):
    created = await asyncio.gather(*(ensure_butler_home(store) for _ in range(4)))
    assert {project.id for project in created} == {BUTLER_HOME_PROJECT}
    assert created[0].metadata == {"system": "butler"}
    assert (
        await store.get(ScopePolicy, f"scope:{BUTLER_HOME_PROJECT}")
    ).local_only is (False)
    assert await store.list_entities(Engagement) == []
    # An operator can still name a project "Butler".
    mine = await create_engagement_with_default_scope(
        store,
        Engagement(name="Butler"),
        actor_id="operator",
        request_id="create-butler-named-project",
    )
    assert [project.id for project in await store.list_entities(Engagement)] == [
        mine.id
    ]


def test_home_reservation_requests_are_recognized(monkeypatch):
    path = "/api/v1/chat/control-assistant/session"
    home_body = b'{"engagement_id": "%s", "backend": "provider"}' % (
        BUTLER_HOME_PROJECT.encode()
    )
    assert is_home_reservation("POST", path, home_body)
    assert not is_home_reservation("POST", path, b'{"engagement_id": "mine"}')
    assert not is_home_reservation("POST", path, b"{not json")
    assert not is_home_reservation("GET", path, home_body)
    monkeypatch.delenv(BUTLER_HOME_FLAG, raising=False)
    assert not butler_home_enabled()
    monkeypatch.setenv(BUTLER_HOME_FLAG, "on")
    assert butler_home_enabled()


async def test_a_staged_target_without_the_feature_refuses_the_home(store):
    from pathlib import Path

    from nebula.v3.deployments import DeploymentCoordinator
    from tests.v3.test_deployment_lifecycle import manifest

    await store.database.initialize()
    assert await target_release_hosts_butler_home(store)
    coordinator = DeploymentCoordinator(store.database)
    await coordinator.stage(
        manifest("older", features=["project.handoff/v1"]),
        Path("/synthetic/releases/older"),
        "a" * 64,
    )
    async with coordinator.coordination._write() as session:
        host = await session.get(DeploymentHostRow, "host")
        host.target_version = "older"
    assert not await target_release_hosts_butler_home(store)


def test_reservation_shapes():
    provider = dict(backend=ChatBackend.PROVIDER, provider_id="provider")
    focused = ButlerSessionReservation(
        engagement_id=BUTLER_HOME_PROJECT,
        focus=ButlerFocus(project="target"),
        **provider,
    )
    assert focused.focus.project == "target"
    with pytest.raises(ValueError, match="home session"):
        ButlerSessionReservation(
            engagement_id="target", focus=ButlerFocus(project="other"), **provider
        )
    with pytest.raises(ValueError, match="Core project"):
        ButlerSessionReservation(
            engagement_id=BUTLER_HOME_PROJECT,
            focus=ButlerFocus(project=BUTLER_HOME_PROJECT),
            **provider,
        )
    with pytest.raises(ValueError, match="provider Butler"):
        ButlerSessionReservation(
            engagement_id=BUTLER_HOME_PROJECT,
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness",
        )


async def _inert_provider(control):
    profile = await control.get(ProviderProfile, "provider")
    await control.update(
        ProviderProfile,
        profile.id,
        {"metadata": {**profile.metadata, "default_model": "inert"}},
    )


async def test_home_sessions_are_one_per_focus_project(home):
    control, runtime, _ = home
    await _inert_provider(control)
    await control.create(Engagement(id="second", name="Second"))
    await control.create(
        Engagement(id="old", name="Old", status=EngagementStatus.ARCHIVED)
    )

    async def reserve(project):
        return await reserve_butler_session(
            runtime,
            None,
            ButlerSessionReservation(
                engagement_id=BUTLER_HOME_PROJECT,
                backend=ChatBackend.PROVIDER,
                provider_id="provider",
                focus=ButlerFocus(project=project),
            ),
        )

    first = await reserve("target")
    assert first.engagement_id == BUTLER_HOME_PROJECT
    assert first.metadata["butler_focus_project"] == "target"
    assert butler_focus_project(first) == "target"
    assert (await reserve("target")).id == first.id
    second = await reserve("second")
    assert second.id != first.id
    with pytest.raises(Exception, match="Restore the project"):
        await reserve("old")
    # Only sessions in Butler's home carry a focus.
    assert (
        butler_focus_project(
            ChatSession(
                engagement_id="target",
                title="Butler",
                model="inert",
                provider_profile_id="provider",
                metadata={"butler_focus_project": "target"},
            )
        )
        is None
    )


class _ReadTarget:
    def __init__(self, store, risk):
        self.store = store
        self.spec = ToolSpec(
            name="nebula.test.read_turn",
            description="Read the target project's turn",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            risk_class=risk,
        )

    async def _handler(self, _payload):
        return {"model": (await self.store.get(ChatTurn, "turn-target")).model}


async def test_focus_reads_run_as_observer_and_changes_are_refused(home):
    _, runtime, _ = home
    capabilities = await ButlerCapabilities.create(
        runtime, None, "target", observer=True
    )
    capabilities._actions["nebula.test.read_turn"] = _ReadTarget(
        runtime, RiskClass.LOCAL_READ
    )
    result = await capabilities.invoke(
        {"operation": "nebula.test.read_turn", "arguments": {}}, change=False
    )
    assert result["model"] == "inert"
    assert result["butler_project_id"] == "target"

    capabilities._actions["nebula.test.read_turn"] = _ReadTarget(
        runtime, RiskClass.WORKSPACE_WRITE
    )
    with pytest.raises(CapabilityInputError) as refused:
        await capabilities.invoke(
            {"operation": "nebula.test.read_turn", "arguments": {}}, change=True
        )
    assert refused.value.reason == "home_change_unavailable"
    with pytest.raises(ValueError, match="background"):
        await ButlerCapabilities.create(
            runtime, None, "target", observer=True, background_case_id="case"
        )


async def test_local_only_focus_never_reaches_a_cloud_model(home):
    control, runtime, _ = home
    await control.create(ScopePolicy(id="scope:target", engagement_id="target"))
    target = await control.get(Engagement, "target")
    await control.update(Engagement, "target", {"scope_policy_id": "scope:target"})
    platform = SimpleNamespace(store=runtime)
    cloud = SimpleNamespace(config=SimpleNamespace(local=False))
    local = SimpleNamespace(config=SimpleNamespace(local=True))

    await RuntimePlatform._check_focus_privacy(platform, "target", cloud)
    await control.update(ScopePolicy, "scope:target", {"local_only": True})
    with pytest.raises(RuntimePlatformError, match="local models"):
        await RuntimePlatform._check_focus_privacy(platform, "target", cloud)
    await RuntimePlatform._check_focus_privacy(platform, "target", local)
    with pytest.raises(RuntimePlatformError, match="provider model"):
        await RuntimePlatform._check_focus_privacy(platform, "target", None)
    assert target.name == "target"
