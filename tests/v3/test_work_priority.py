"""High-priority chats and projects are taken first wherever work waits.

The operator marks a chat or project high instead of telling Butler. Waiting
provider turns, tool calls, Host commands and project workers then go to that
work first; nothing already running is interrupted.
"""

from tests.v3.async_test_support import (
    close_database_handles as close_database_handles,
)

import asyncio
from datetime import timedelta

import pytest

from nebula.v3 import work_priority
from nebula.v3.domain import ChatSession, Engagement, utc_now
from nebula.v3.host_load import HostLoadGate
from nebula.v3.memory_budget import MemoryBudgetService
from nebula.v3.runtime_capacity import WAITER_TTL_MS
from nebula.v3.storage import NebulaStore
from nebula.v3.work_priority import (
    high_project_ids,
    is_high,
    set_project_priority,
)
from tests.v3.host_load_fixture import ManualMonitor
from tests.v3.test_runtime_capacity import shared as shared
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fresh_cache():
    work_priority.forget()
    yield
    work_priority.forget()


async def _store(tmp_path) -> NebulaStore:
    store = NebulaStore(tmp_path / "priority.db")
    await store.create(Engagement(id="project", name="Project"))
    await store.create(Engagement(id="other", name="Other"))
    for session_id, parent, metadata in (
        ("main", None, {"priority": "high"}),
        ("subagent", "main", {"subagent_id": "sub-1"}),
        ("goal-child", "main", {"delegated_from_session_id": "main"}),
        ("fork", "main", {"forked_from_message_id": "m"}),
        ("plain", None, {}),
    ):
        await store.create(
            ChatSession(
                id=session_id,
                engagement_id="project",
                title=session_id,
                provider_profile_id="provider",
                model="model",
                parent_session_id=parent,
                metadata=metadata,
            )
        )
    return store


async def test_chat_priority_reaches_its_subagents_but_not_forks(tmp_path):
    store = await _store(tmp_path)

    for session_id in ("main", "subagent", "goal-child"):
        assert await is_high(store, "project", session_id), session_id
    for session_id in ("fork", "plain"):
        assert not await is_high(store, "project", session_id), session_id
    assert not await is_high(store, "project")
    assert await high_project_ids(store) == {"project"}


async def test_a_high_project_makes_every_chat_in_it_high(tmp_path):
    store = await _store(tmp_path)
    await set_project_priority(store, "other", "high")

    assert await is_high(store, "other")
    assert await is_high(store, "other", "anything")
    assert await high_project_ids(store) == {"project", "other"}

    await set_project_priority(store, "other", "normal")
    assert not await is_high(store, "other")
    assert "priority" not in (await store.get(Engagement, "other")).metadata


async def test_normal_work_waits_while_high_priority_work_is_short(shared):  # noqa: F811
    capacities, leases, _, clock = shared
    first, second = capacities
    # Both tool slots of the host (one, here) are busy.
    running = await first.acquire(leases[0], "running", {"tool": 1})

    assert await second.acquire(leases[1], "urgent", {"tool": 1}, high=True) is None
    await first.release(running)

    # The slot freed, but a fresh high-priority waiter still lacks it: the
    # normal request waits, and the high one takes the slot on its next poll.
    assert await first.acquire(leases[0], "routine", {"tool": 1}) is None
    urgent = await second.acquire(leases[1], "urgent", {"tool": 1}, high=True)
    assert urgent is not None and urgent.state == "held"
    # Normal work on a dimension the waiter does not need is never held back.
    assert await first.acquire(leases[0], "provider", {"provider": 1})
    await second.release(urgent)
    assert await first.acquire(leases[0], "routine", {"tool": 1})
    assert (await first.status())["used"] == {"provider": 1, "tool": 1}


async def test_a_vanished_high_waiter_stops_holding_normal_work_back(shared):  # noqa: F811
    capacities, leases, _, clock = shared
    first, second = capacities
    running = await first.acquire(leases[0], "running", {"tool": 1})
    assert await second.acquire(leases[1], "urgent", {"tool": 1}, high=True) is None
    await first.release(running)

    clock[0] += WAITER_TTL_MS + 1

    assert await first.acquire(leases[0], "routine", {"tool": 1})


async def test_a_high_slot_that_gives_up_withdraws_its_waiter(shared):  # noqa: F811
    capacities, leases, _, _ = shared
    first, second = capacities
    running = await first.acquire(leases[0], "running", {"tool": 1})

    async def wait_high():
        async with second.slot(leases[1], "urgent", {"tool": 1}, high=True):
            raise AssertionError("no slot was free")

    task = asyncio.create_task(wait_high())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await first.release(running)

    assert await first.acquire(leases[0], "routine", {"tool": 1})


async def test_high_priority_waiters_pass_the_host_load_line_in_their_order():
    monitor = ManualMonitor()
    monitor.interval = 2
    gate = HostLoadGate(monitor)
    started = []

    async def start(name, high=False):
        await gate.wait(high=high)
        started.append(name)

    tasks = [asyncio.create_task(start("normal-1"))]
    await asyncio.sleep(0.01)
    tasks.append(asyncio.create_task(start("normal-2")))
    await asyncio.sleep(0.01)
    tasks.append(asyncio.create_task(start("high-1", high=True)))
    await asyncio.sleep(0.01)
    tasks.append(asyncio.create_task(start("high-2", high=True)))
    await asyncio.sleep(0.02)
    monitor.recover()
    gate.wake()
    await asyncio.wait_for(asyncio.gather(*tasks), 0.5)

    assert started == ["high-1", "high-2", "normal-1", "normal-2"]
    assert not gate._waiters and not gate._high


async def test_high_priority_host_commands_are_admitted_first():
    from types import SimpleNamespace

    now = utc_now()

    def job(job_id, project, age, priority=None):
        return SimpleNamespace(
            id=job_id,
            project_id=project,
            created_at=now - timedelta(seconds=age),
            labels={"priority": priority} if priority else {},
        )

    queued = [
        job("old-normal", "a", 30),
        job("other-normal", "b", 20),
        job("new-high", "a", 1, "high"),
    ]

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def __iter__(self):
            return iter(self.rows)

    class Session:
        async def execute(self, _statement):
            return Result([])

        async def scalars(self, _statement):
            return list(queued)

    ordered, running = await MemoryBudgetService._host_command_order(Session())

    assert running == 0
    assert [row.id for row in ordered] == ["new-high", "other-normal", "old-normal"]


async def test_operator_and_butler_mark_priority_and_the_board_leads_with_it(
    tmp_path,
):
    from tests.v3.async_test_support import AsyncTestClient as TestClient
    from tests.v3.test_control_assistant_work import _app
    from nebula.v3.chat_goals import ChatGoalService, GoalCreate
    from nebula.v3.control_assistant_work import dispatch_work_tool
    from nebula.v3.domain import ChatGoal, ChatGoalStatus, ProviderProfile

    store, app, _ = _app(tmp_path)
    project = await store.create(Engagement(name="Research"))
    profile = await store.create(
        ProviderProfile(
            name="Local",
            provider_type="ollama",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )
    goals = ChatGoalService(store)
    chats = []
    for title in ("Older direction", "New direction"):
        chat = await store.create(
            ChatSession(
                engagement_id=project.id,
                title=title,
                provider_profile_id=profile.id,
                model="model-a",
            )
        )
        draft = await goals.create(
            chat.id, GoalCreate(objective=title, completion_criteria=["Done"])
        )
        await store.update(
            ChatGoal,
            draft.id,
            {"status": ChatGoalStatus.PAUSED, "started_at": utc_now()},
            expected_revision=draft.revision,
        )
        chats.append(chat)
    client = TestClient(app)
    auth = {"Authorization": "Bearer test-token"}
    # The newest chat leads by activity; mark the older one high.
    older, newer = chats
    await store.update(
        ChatSession,
        newer.id,
        {"title": "New direction"},
        expected_revision=newer.revision,
    )

    marked = await client.patch(
        f"/api/v1/chat-sessions/{older.id}",
        headers=auth,
        json={"priority": "high"},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["metadata"]["priority"] == "high"
    assert await is_high(store, project.id, older.id)

    board = await client.get(
        "/api/v1/butler/board", params={"section": "idle"}, headers=auth
    )
    assert board.status_code == 200, board.text
    items = board.json()["items"]
    assert [item["session_id"] for item in items] == [older.id, newer.id]
    assert items[0]["priority"] == "high" and items[1]["priority"] == "normal"

    cleared = await client.patch(
        f"/api/v1/chat-sessions/{older.id}", headers=auth, json={"priority": "normal"}
    )
    assert "priority" not in cleared.json()["metadata"]
    assert (
        await client.put(
            f"/api/v1/engagements/{project.id}/priority",
            json={"priority": "high"},
        )
    ).status_code == 401
    whole = await client.put(
        f"/api/v1/engagements/{project.id}/priority",
        headers=auth,
        json={"priority": "high"},
    )
    assert whole.json()["metadata"]["priority"] == "high"
    assert await is_high(store, project.id, newer.id)

    # Butler sets and reads priority without a confirmation step.
    work = app.state.control_assistant_work
    project_normal = await dispatch_work_tool(
        work, project.id, "nebula.work.set_priority", {"priority": "normal"}
    )
    assert project_normal["priority"] == "normal"
    chat_high = await dispatch_work_tool(
        work,
        project.id,
        "nebula.work.set_priority",
        {"session_id": newer.id, "priority": "high"},
    )
    assert chat_high["priority"] == "high"
    overview = await work.overview(project.id)
    assert overview["project"]["priority"] == "normal"
    assert overview["chats"][0]["id"] == newer.id
    assert overview["chats"][0]["priority"] == "high"
