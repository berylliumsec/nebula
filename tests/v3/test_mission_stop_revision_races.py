"""#1100: Mission Stop when the Mission's own work saves the run meanwhile.

Native: the Mission task moves the run from QUEUED to PLANNING while Stop
revokes browser control. Delegated: the run's watcher syncs the supervisor
task while Stop syncs it.
"""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import time
from pathlib import Path

import pytest

from nebula.v3.api import create_app
from nebula.v3.domain import (
    AgentRun,
    ChatTurn,
    Engagement,
    RunBackend,
    RunBudget,
    RunStatus,
    Task,
)
from nebula.v3.host_load import HostLoadGate, HostLoadMonitor, HostLoadSnapshot
from nebula.v3.mission_delegation import DelegatedMissionService
from nebula.v3.missions import MissionService
from nebula.v3.providers import ModelRequest, ModelResponse
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import AsyncTestClient as TestClient
from tests.v3.revision_window import before_update
from tests.v3.test_chat_subagents import RoutedProvider, _finish, _response, _setup
from tests.v3.test_missions import RecordingProvider, _auth, _profile, _start_payload

pytestmark = pytest.mark.anyio


class PausingRevoker:
    """Stands in for BrowserAutomationService.revoke_run with a pause point."""

    def __init__(self, store):
        self.store = store
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.revision_at_entry: int | None = None

    async def revoke_run(self, run_id, reason, actor_id):
        # The real revoke_run starts with store.get(AgentRun) and two
        # find_entities calls, all awaited (each yields to the event loop).
        run = await self.store.get(AgentRun, run_id)
        self.revision_at_entry = run.revision
        self.entered.set()
        await self.release.wait()
        return 0


class SwitchableHost(HostLoadMonitor):
    def __init__(self):
        super().__init__()
        self.busy = True

    def cached(self):
        return self.sample()

    def sample(self):
        return HostLoadSnapshot(
            sampled_at=time.monotonic(),
            cpu_capacity=4,
            cpu_ratio=1.0 if self.busy else 0.0,
            memory_available_ratio=1.0,
            busy=self.busy,
            reason="CPU use (100% >= 80%)" if self.busy else None,
            measurements_available=True,
        )


async def _until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_native_stop_wins_over_the_mission_starting(tmp_path):
    store = NebulaStore(tmp_path / "start-race.db")
    engagement = await store.create(Engagement(name="Stop race at start"))
    profile = await _profile(store)
    provider = RecordingProvider(profile)
    revoker = PausingRevoker(store)
    service = MissionService(
        store,
        checkpoint_path=tmp_path / "start-race-checkpoints.db",
        provider_factory=lambda selected: provider,
        browser_automation_service=revoker,
    )
    host = SwitchableHost()
    gate = HostLoadGate(host)
    app = create_app(
        store, auth_token="test-token", mission_service=service, host_load_gate=gate
    )
    async with TestClient(app) as client:
        started = await client.post(
            "/api/v1/missions",
            headers=_auth(),
            json=_start_payload(engagement, profile),
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]

        async def host_wait_recorded():
            return (await store.get(AgentRun, run_id)).metadata.get("host_load_wait")

        await _until(host_wait_recorded)
        stop_task = asyncio.ensure_future(
            client.post(
                f"/api/v1/runs/{run_id}/stop",
                headers=_auth(),
                json={"reason": "Operator stopped it as it started"},
            )
        )
        await asyncio.wait_for(revoker.entered.wait(), 10)
        # The Mission's own task saves the run while Stop is in its window.
        host.busy = False
        gate.wake()

        async def mission_started():
            run = await store.get(AgentRun, run_id)
            return run if run.status != RunStatus.QUEUED else None

        await _until(mission_started)
        revoker.release.set()
        response = await asyncio.wait_for(stop_task, 20)
        task = service._tasks.get(run_id)
        if task is not None:
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        final = await store.get(AgentRun, run_id)
        events = [event.event_type for event in await store.replay_events(run_id)]

    assert response.status_code < 300, response.text
    assert "run.stop_requested" in events
    assert final.status == RunStatus.CANCELLED


class PausedProvider(RoutedProvider):
    def __init__(self) -> None:
        super().__init__(parent=[_finish("done"), _response(text="Done.")], child=[])
        self.gate = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") != "conversation_naming":
            await self.gate.wait()
        return await super().complete(request)


async def test_delegated_stop_keeps_the_operators_reason(tmp_path: Path) -> None:
    provider = PausedProvider()
    store, project, profile, chat = await _setup(tmp_path, provider)
    delegated = DelegatedMissionService(store, chat, None)  # type: ignore[arg-type]
    run = await delegated.start(
        engagement_id=project.id,
        name="Stop review",
        objective="Check scope",
        backend=RunBackend.NATIVE,
        provider_id=profile.id,
        harness_profile_id=None,
        harness_session_id=None,
        model="model-a",
        subagent_provider_id=None,
        subagent_model=None,
        mcp_server_ids=[],
        stages=[],
        budget=RunBudget(),
        tools_enabled=False,
        allow_cloud_tool_results=False,
    )

    async def watcher_sync():
        # The run's own watcher pass landing inside Stop's sync.
        await delegated._sync(await store.get(AgentRun, run.id))

    window = before_update(store, "_sync", watcher_sync, model=Task)
    try:
        stopped = await delegated.stop(run.id, reason="Operator stopped the Mission")
        assert window.fired
        for _ in range(40):
            await asyncio.sleep(0.05)
        final = await store.get(AgentRun, run.id)
        turn = await store.get(ChatTurn, str(final.metadata["supervisor_chat_turn_id"]))
        assert stopped.status == RunStatus.CANCELLED
        assert final.status == RunStatus.CANCELLED
        assert final.metadata["final_summary"] == "Operator stopped the Mission"
        assert turn.status.value == "cancelled"
        events = [event.event_type for event in await store.replay_events(run.id)]
        assert events.count("run.cancelled") == 1
    finally:
        provider.gate.set()
        await delegated.shutdown()
        await chat.shutdown()
