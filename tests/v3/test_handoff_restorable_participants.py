"""Idle vendor sessions and sleeping Mission schedules never pin a handoff.

An idle harness connection used to pin its project (the resident connection
and its ``idle`` session row), and so did a scheduled Mission whose timer was
only sleeping. Both now move: the source detaches the idle connection and the
target's next turn resumes the same vendor thread; a sleeping schedule is
re-armed from SQL by the target's startup and fires there exactly once, while
the source's leftover timer is fenced out of the database.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from nebula.v3.credentials import CredentialStore
from nebula.v3.database import Database, EntityRow
from nebula.v3.database_pool import PoolSettings
from nebula.v3.domain import (
    AgentRun,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
    ProviderProfile,
    RunStatus,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.harnesses import (
    ADAPTER_CONTRACT_VERSION,
    ClaudeAgentSdkConnection,
    CodexAppServerConnection,
    GrokAcpConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessRuntimeService,
)
from nebula.v3.missions import MissionService
from nebula.v3.storage import NebulaStore
from tests.v3.test_project_handoff_admission import project as project
from tests.v3.test_project_handoff_agent import agent as agent, row
from tests.v3.test_deployment_lifecycle import host as host
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- Inert harness fixture -------------------------------------------------


class InertConnection(HarnessConnection):
    adapter_version = ADAPTER_CONTRACT_VERSION + "/handoff-fixture"

    def __init__(self, adapter: "InertAdapter", request):
        self.adapter = adapter
        self.request = request
        # A saved vendor thread is resumed; a new session starts one.
        self.external_session_id = (
            request.session.external_session_id or "vendor-thread-1"
        )
        self.closed = False

    async def run_turn(self, prompt, *, model, **options):
        self.adapter.prompts.append((self.external_session_id, prompt))
        yield HarnessEvent(type="started", external_session_id=self.external_session_id)
        gate = self.adapter.gate
        if gate is not None:
            self.adapter.running.set()
            await gate.wait()
        yield HarnessEvent(type="message_delta", delta="Inert reply.")
        yield HarnessEvent(type="completed", message="Inert reply.")

    async def steer(self, text):
        pass

    async def interrupt(self):
        if self.adapter.gate is not None:
            self.adapter.gate.set()

    async def close(self):
        self.adapter.closing.set()
        if self.adapter.close_gate is not None:
            await self.adapter.close_gate.wait()
        self.closed = True

    def handoff_detachable(self):
        return self.adapter.detachable


class InertAdapter(HarnessAdapter):
    def __init__(self):
        self.opened: list[tuple[str, str | None]] = []
        self.connections: list[InertConnection] = []
        self.prompts: list[tuple[str, str]] = []
        self.gate: asyncio.Event | None = None
        self.running = asyncio.Event()
        self.detachable = True
        self.closing = asyncio.Event()
        self.close_gate: asyncio.Event | None = None

    async def open(self, request):
        self.opened.append((request.session.id, request.session.external_session_id))
        connection = InertConnection(self, request)
        self.connections.append(connection)
        return connection

    async def probe(self, profile, credential_store):
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=profile.kind,
            harness_version="fixture",
            capabilities=HarnessCapabilities(
                models=["model-a"], interruption=True, checked_at=utc_now()
            ),
        )


@pytest.fixture
async def harness(tmp_path):
    """The inert harness world; its store is closed so pytest can exit."""

    store = NebulaStore(tmp_path / "harness-handoff.db")
    try:
        yield await _harness(store, tmp_path)
    finally:
        await store.event_history.close()
        await store.database.dispose()


async def _harness(store, tmp_path):
    engagement = await store.create(Engagement(id="eng-a", name="Handoff"))
    profile = await store.create(
        HarnessProfile(
            id="harness-a",
            name="Inert harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model="model-a",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    adapter = InertAdapter()

    def service():
        return HarnessRuntimeService(
            store,
            credential_store=CredentialStore(),
            workspace_resolver=lambda _: tmp_path,
            adapter_factory=lambda _: adapter,
        )

    return store, engagement, profile, adapter, service


async def _send(service, engagement, profile, chat_id, prompt):
    chat, owner, turn = await service.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt=prompt,
        chat_session_id=chat_id,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    return chat, owner, turn, await service.start_chat_turn(turn.id)


async def test_idle_vendor_session_detaches_and_the_target_resumes_it(harness):
    store, engagement, profile, adapter, service = harness
    source = service()
    chat, _, first, task = await _send(source, engagement, profile, None, "First")
    await asyncio.wait_for(task, 20)
    assert (await store.get(HarnessTurn, first.id)).status == HarnessTurnStatus.COMPLETE
    session_id = first.harness_session_id
    session = await store.get(HarnessSession, session_id)
    assert session.status == HarnessSessionStatus.IDLE
    assert session.external_session_id == "vendor-thread-1"
    assert list(source._connections) == [session_id]
    assert await source.handoff_blockers(engagement.id) == {}

    # Another project's connections are left alone.
    assert await source.release_idle_connections_for_handoff("other") == []
    assert list(source._connections) == [session_id]

    assert await source.release_idle_connections_for_handoff(engagement.id) == [
        session_id
    ]
    assert source._connections == {} and adapter.connections[0].closed
    # The session row keeps its saved vendor thread for the next owner.
    detached = await store.get(HarnessSession, session_id)
    assert detached.status == HarnessSessionStatus.IDLE
    assert detached.external_session_id == "vendor-thread-1"

    # The target (a separate runtime over the same durable state) resumes the
    # same vendor thread on the conversation's next turn.
    target = service()
    _, _, second, task = await _send(target, engagement, profile, chat.id, "Second")
    await asyncio.wait_for(task, 20)
    second = await store.get(HarnessTurn, second.id)
    assert second.status == HarnessTurnStatus.COMPLETE
    assert second.harness_session_id == session_id
    assert adapter.opened == [(session_id, None), (session_id, "vendor-thread-1")]
    assert adapter.prompts[-1][0] == "vendor-thread-1"
    await source.shutdown()
    await target.shutdown()


async def test_busy_vendor_session_stays_attached_and_pins(harness):
    store, engagement, profile, adapter, service = harness
    source = service()
    chat, _, first, task = await _send(source, engagement, profile, None, "First")
    await asyncio.wait_for(task, 20)
    adapter.gate = asyncio.Event()
    _, _, second, task = await _send(source, engagement, profile, chat.id, "Busy")
    await asyncio.wait_for(adapter.running.wait(), 20)
    session_id = second.harness_session_id
    assert await source.release_idle_connections_for_handoff(engagement.id) == []
    assert list(source._connections) == [session_id]
    assert not adapter.connections[0].closed
    # A connecting turn holds the session lock before it reaches _active.
    assert await source.handoff_blockers(engagement.id)
    adapter.gate.set()
    await asyncio.wait_for(task, 20)
    assert (await store.get(HarnessTurn, second.id)).status == HarnessTurnStatus.COMPLETE
    assert await source.handoff_blockers(engagement.id) == {}
    assert await source.release_idle_connections_for_handoff(engagement.id) == [
        session_id
    ]
    await source.shutdown()


async def test_vendor_work_of_its_own_keeps_the_connection(harness):
    store, engagement, profile, adapter, service = harness
    source = service()
    _, _, first, task = await _send(source, engagement, profile, None, "First")
    await asyncio.wait_for(task, 20)
    adapter.detachable = False
    assert await source.release_idle_connections_for_handoff(engagement.id) == []
    assert list(source._connections) == [first.harness_session_id]
    assert not adapter.connections[0].closed
    await source.shutdown()


async def test_a_turn_sent_during_the_detach_resumes_instead_of_forking(harness):
    store, engagement, profile, adapter, service = harness
    source = service()
    chat, _, first, task = await _send(source, engagement, profile, None, "First")
    await asyncio.wait_for(task, 20)
    session_id = first.harness_session_id
    adapter.close_gate = asyncio.Event()
    release = asyncio.create_task(
        source.release_idle_connections_for_handoff(engagement.id)
    )
    await asyncio.wait_for(adapter.closing.wait(), 20)
    # The detach holds the session lock while the vendor process closes.
    _, _, second, task = await _send(source, engagement, profile, chat.id, "Next")
    await asyncio.sleep(0.1)
    adapter.close_gate.set()
    assert await asyncio.wait_for(release, 20) == [session_id]
    await asyncio.wait_for(task, 20)
    second = await store.get(HarnessTurn, second.id)
    assert second.status == HarnessTurnStatus.COMPLETE
    assert second.harness_session_id == session_id
    assert second.metadata.get("forked_from_session_id") is None
    assert adapter.opened[-1] == (session_id, "vendor-thread-1")
    await source.shutdown()


async def test_queued_chat_turn_pins_but_queued_mission_stage_does_not(harness):
    store, engagement, profile, adapter, service = harness
    source = service()
    _, _, turn = await source.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Waiting for host capacity",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    # Queued with no task or lock yet: only the queued row shows it.
    assert list(await source.handoff_blockers(engagement.id)) == [
        "harness-queued/" + turn.id
    ]
    # A queued Mission stage is restored by the next owner's startup.
    stage = await store.create(
        turn.model_copy(
            update={
                "id": "mission-stage",
                "origin": HarnessTurnOrigin.MISSION,
                "run_id": "scheduled-run",
                "chat_turn_id": None,
                "chat_session_id": None,
                "revision": 1,
            }
        )
    )
    assert stage.status == HarnessTurnStatus.QUEUED
    current = await store.get(HarnessTurn, turn.id)
    await store.update(
        HarnessTurn,
        turn.id,
        {"status": HarnessTurnStatus.INTERRUPTED},
        expected_revision=current.revision,
    )
    assert await source.handoff_blockers(engagement.id) == {}
    await source.shutdown()


async def test_sleeping_harness_schedule_is_restorable_until_it_wakes(harness):
    store, engagement, profile, adapter, service = harness
    source = service()
    task = asyncio.create_task(
        source._scheduled_mission_execute(
            "scheduled-run", [], utc_now() + timedelta(hours=1)
        )
    )
    source._scheduled_mission_tasks["scheduled-run"] = task
    await asyncio.sleep(0)
    assert source.restorable_handles() == {
        "_scheduled_mission_tasks": {"scheduled-run"}
    }
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert source.restorable_handles() == {"_scheduled_mission_tasks": set()}
    assert source._scheduled_mission_tasks == {}
    await source.shutdown()


def test_adapters_detach_only_when_the_vendor_has_nothing_of_its_own():
    claude = ClaudeAgentSdkConnection(
        None,
        permission_handler=None,
        sdk=None,
        external_session_id="claude-session",
        workspace=None,
    )
    assert claude.handoff_detachable()
    claude._track_task(SimpleNamespace(task_id="bash-1"), "TaskStartedMessage")
    assert not claude.handoff_detachable()
    # An update that changes other fields carries no status: still running.
    claude._track_task(
        SimpleNamespace(task_id="bash-1", patch={"description": "renamed"}),
        "TaskUpdatedMessage",
    )
    assert not claude.handoff_detachable()
    claude._track_task(
        SimpleNamespace(task_id="bash-1", patch={"status": "completed"}),
        "TaskUpdatedMessage",
    )
    assert claude.handoff_detachable()
    claude._track_task(SimpleNamespace(task_id="agent-2"), "TaskProgressMessage")
    assert not claude.handoff_detachable()
    claude._track_task(
        SimpleNamespace(task_id="agent-2", status="stopped"), "TaskNotificationMessage"
    )
    assert claude.handoff_detachable()
    claude._inbox.put_nowait(object())
    assert not claude.handoff_detachable()

    codex = CodexAppServerConnection(
        None, external_session_id="thread", permission_handler=None
    )
    # Native shell can leave background terminals in the app-server.
    assert not codex.handoff_detachable()
    codex.native_shell = False
    assert codex.handoff_detachable()
    codex._awaiting_goal_turn = True
    assert not codex.handoff_detachable()

    grok = GrokAcpConnection(None, external_session_id="grok", permission_handler=None)
    # Without session/load a later process could not resume the session.
    assert not grok.handoff_detachable()
    grok.load_session = True
    assert grok.handoff_detachable()
    grok._open_permissions.add(1)
    assert not grok.handoff_detachable()


# --- Handoff inventory -----------------------------------------------------


async def _entity(project, identity, kind, status):
    now = utc_now()
    async with project.coordinator.database.session() as session:
        await session.execute(
            insert(EntityRow),
            [
                {
                    "id": identity,
                    "kind": kind,
                    "engagement_id": "one",
                    "revision": 1,
                    "payload": {"status": status},
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )


async def test_idle_harness_session_detaches_before_the_inventory(project, agent):
    await _entity(project, "idle-harness", "harness_sessions", "idle")
    await _entity(project, "new-harness", "harness_sessions", "starting")
    harness = agent.app.state.harness_runtime_service
    harness._connections["idle-harness"] = object()
    calls = []
    blockers = {}

    async def detach(project_id):
        calls.append(project_id)
        if not harness.keep_open:
            harness._connections.clear()
        return []

    harness.release_idle_connections_for_handoff = detach
    async def harness_blockers(project_id):
        assert project_id == "one"
        return dict(blockers)

    harness.handoff_blockers = harness_blockers
    # A connection the service keeps (busy) still pins the project.
    harness.keep_open = True
    assert await agent.tick() == "finishing"
    assert any("harness_runtime_service" in text for text in (await row(project)).blockers)
    harness.keep_open = False
    # A connecting turn or goal dispatch still pins through handoff_blockers.
    blockers["harness-session-lock/idle-harness"] = "idle-harness is connecting"
    assert await agent.tick() == "finishing"
    assert "idle-harness is connecting" in (await row(project)).blockers
    blockers.clear()
    # Idle and starting rows are durable state the target resumes or opens.
    assert await agent.tick() == "transferable"
    assert calls == ["one", "one", "one"]
    await _entity(project, "busy-harness", "harness_sessions", "running")
    assert await agent.tick() == "finishing"
    assert any("busy-harness remains running" in text for text in (await row(project)).blockers)


async def test_restorable_handles_do_not_pin_but_other_entries_do(project, agent):
    loop = asyncio.get_running_loop()
    sleeping, starting = loop.create_future(), loop.create_future()
    missions = agent.app.state.mission_service
    missions._scheduled_tasks["sleeping"] = sleeping
    missions.restorable_handles = lambda: {"_scheduled_tasks": {"sleeping"}}
    assert await agent.tick() == "transferable"
    missions._scheduled_tasks["starting"] = starting
    assert await agent.tick() == "finishing"
    assert any("1 resident handle(s) in mission_service" in text for text in (await row(project)).blockers)
    sleeping.cancel()
    starting.cancel()


# --- Scheduled Mission across a real transfer ------------------------------


class _ScheduleClock:
    """asyncio for the missions module, with schedule timers the test wakes."""

    def __init__(self):
        self.wake = asyncio.Event()

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay):
        if delay > 60:
            await self.wake.wait()
        else:
            await asyncio.sleep(delay)


async def test_scheduled_mission_fires_exactly_once_after_handoff(
    project, agent, tmp_path, monkeypatch
):
    import nebula.v3.missions as missions_module

    clock = _ScheduleClock()
    monkeypatch.setattr(missions_module, "asyncio", clock)
    profile = await project.durable.create(
        ProviderProfile(
            name="Lab model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            is_local=True,
            model_allowlist=["security-model"],
        )
    )
    scheduled_for = utc_now() + timedelta(hours=1)
    run = await project.durable.create(
        AgentRun(
            engagement_id="one",
            objective="Review the bounded scope",
            status=RunStatus.QUEUED,
            supervisor_provider_id=profile.id,
            supervisor_model="security-model",
            metadata={"origin": "api", "scheduled_for": scheduled_for.isoformat()},
        )
    )
    starts: list[str] = []

    def mission_service(name, store):
        service = MissionService(
            store,
            checkpoint_path=tmp_path / f"{name}-checkpoints.db",
            provider_factory=lambda _profile: object(),
        )

        async def execute(queued, _provider):
            # The decisive transition every real start makes first.
            latest = await store.get(AgentRun, queued.id)
            await store.update(
                AgentRun,
                latest.id,
                {"status": RunStatus.RUNNING},
                expected_revision=latest.revision,
            )
            starts.append(name)

        service._execute = execute
        return service

    source = mission_service("source", project.store)
    with execution_context(project.lease):
        await source.startup()
    assert set(source._scheduled_tasks) == {run.id}
    await asyncio.sleep(0)
    assert source.restorable_handles() == {"_scheduled_tasks": {run.id}}

    agent.app.state.mission_service = source
    assert await agent.tick() == "transferable"
    handoff = await row(project)
    assert not any("mission_service" in text for text in handoff.blockers)
    lease = await project.coordinator.transfer("one")
    assert lease.runtime_id == project.target

    target_store = NebulaStore(
        Database(
            project.location,
            bootstrap=False,
            execution_runtime_id=project.target,
            pool_settings=PoolSettings(5, 12, 2, 5),
        )
    )
    target = mission_service("target", target_store)
    try:
        with execution_context(lease):
            await target.startup()
        assert set(target._scheduled_tasks) == {run.id}
        # Both timers wake; only the owner of the project can start the run.
        source_timer = source._scheduled_tasks[run.id]
        clock.wake.set()
        await asyncio.wait(
            [source_timer, target._scheduled_tasks[run.id]], timeout=15
        )
        assert source_timer.done()
        for _ in range(100):
            if starts:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        assert starts == ["target"]
        assert (await project.durable.get(AgentRun, run.id)).status == RunStatus.RUNNING
    finally:
        await source.shutdown()
        await target.shutdown()
        await target_store.event_history.close()
        await target_store.database.dispose()
