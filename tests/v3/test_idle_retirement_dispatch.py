"""Idle retirement must not stop a worker mid-dispatch or skip one silently.

A worker's idle-retirement quiescence refuses, as a handoff does, while its
follow-up queue or Butler authority holds a dispatch that SQL cannot see yet:
stopping it would leave the next owner a claim it can only review as a crash.
An idle worker whose release cannot quiesce stays resident until the deployer
hands it off, and says so once instead of looking like a leaked worker.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3 import runtime_auto_admission
from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.chat_queue import ChatQueueService, QueueWrite
from nebula.v3.database import (
    DeploymentReceiptRow,
    ProjectAdmissionHoldRow,
    ProjectOwnershipRow,
)
from nebula.v3.domain import ChatSession
from nebula.v3.runtime_ipc import RuntimeRpcClient
from nebula.v3.runtime_processes import RuntimeProcessHost
from nebula.v3.runtime_project_handoff import _HANDLES, ProjectHandoffAgent
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_execution_worker import runtime as runtime, until
from tests.v3.test_mixed_release_idle_retirement import (
    admission,
    mixed as mixed,
    owned_project,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _host(runtime, retired):
    async def rpc(runtime_id):
        return RuntimeRpcClient(
            "unix:///isolated/worker.sock",
            b"k" * 32,
            transport=httpx.ASGITransport(
                app=runtime.context.rpc.app, raise_app_exceptions=False
            ),
        )

    async def retire(runtime_id):
        retired.append(runtime_id)

    return SimpleNamespace(coordinator=runtime.coordination, rpc=rpc, retire=retire)


async def _ready(runtime, *, queue=None, butler=None, chat=None):
    """A ready worker whose app carries the real ProjectHandoffAgent."""

    @asynccontextmanager
    async def application(context, lease):
        app = FastAPI()
        for name, attributes in _HANDLES.items():
            setattr(app.state, name, SimpleNamespace(**{a: {} for a in attributes}))
        app.state.control_assistant_work = SimpleNamespace(
            chat=chat
            or SimpleNamespace(
                _active_provider_turns={}, _naming_tasks=set(), _precompactions={}
            ),
            queue=queue,
        )
        if butler is not None:
            app.state.butler_authority = butler
        app.state.runtime_handoff = ProjectHandoffAgent(context, lease, app)
        try:
            yield app
        finally:
            context.restore_cleanup_completed = True

    runtime.execution.application_factory = application
    await runtime.execution.attach(asdict(runtime.lease))
    await until(lambda: runtime.execution.state == "ready")


async def _retirement_receipts(runtime):
    async with runtime.coordination.database.session() as session:
        owner = await session.get(ProjectOwnershipRow, "project")
        actions = list(
            await session.scalars(
                select(DeploymentReceiptRow.action)
                .where(
                    DeploymentReceiptRow.action.in_(
                        ("project.quiesced", "project.retirement_aborted")
                    )
                )
                .order_by(DeploymentReceiptRow.occurred_at_ms)
            )
        )
    return owner, actions


async def _archived_chat_with_follow_up(runtime):
    store = runtime.store
    profile = await store.create(_profile(local=True))
    await store.create(
        ChatSession(
            id="s",
            engagement_id="project",
            provider_profile_id=profile.id,
            model="model-a",
            title="Archived with a queued follow-up",
        )
    )
    provider = FakeProvider(profile.id, local=True)
    chat = ChatService(store, provider_factory=lambda _: provider)
    queue = ChatQueueService(store, chat, None)
    request = ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id="project",
        session_id="s",
        model="model-a",
        messages=[{"role": "user", "content": "Queued follow-up"}],
        include_knowledge=False,
        tools_enabled=False,
    )
    await queue.write(
        "s",
        QueueWrite(
            action="enqueue", expected_revision=0, idempotency_key="k", request=request
        ),
    )
    # Archiving does not pause the queue: the follow-up still dispatches.
    current = await store.get(ChatSession, "s")
    await store.update(
        ChatSession,
        "s",
        {"metadata": {**current.metadata, "archived_at": "2026-11-15T00:00:00+00:00"}},
        expected_revision=current.revision,
    )
    return chat, queue


async def test_idle_retirement_waits_for_a_follow_up_dispatch_in_flight(runtime):
    chat, queue = await _archived_chat_with_follow_up(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = chat.prepare_async

    async def gated_prepare(request):
        # The claim has committed and the item is in queue._inflight; its
        # durable turn does not exist yet, so SQL sees nothing to wait for.
        entered.set()
        await release.wait()
        return await prepare(request)

    chat.prepare_async = gated_prepare
    await _ready(runtime, queue=queue, chat=chat)
    [current] = await queue.queues()
    dispatch = asyncio.create_task(queue.step(current))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        held = (await queue.get("s")).items[0]
        assert held["status"] == "claiming" and not held.get("turn_id")

        retired = []
        assert not await RuntimeProcessHost.retire_idle_project(
            _host(runtime, retired), "project"
        )
        assert retired == []
        assert runtime.execution.state == "ready"
        owner, actions = await _retirement_receipts(runtime)
        assert (owner.runtime_id, owner.fencing_token) == (
            runtime.lease.runtime_id,
            runtime.lease.fencing_token,
        )
        assert not owner.retirement_requested
        assert actions == ["project.retirement_aborted"]

        # The same worker delivers the follow-up instead of a successor
        # reviewing it as a crash.
        release.set()
        await asyncio.wait_for(dispatch, 10)
        assert not queue._inflight
        await until(
            lambda: all(turn.done for turn in chat._active_provider_turns.values())
        )
        after = await queue.get("s")
        assert all(
            item["status"] not in {"claiming", "needs_review"} for item in after.items
        )
    finally:
        release.set()
        dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)
        await queue.shutdown()
        await chat.shutdown()


async def test_idle_retirement_waits_for_a_butler_dispatch_in_flight(runtime):
    butler = SimpleNamespace(_dispatching={"butler-action"})
    await _ready(runtime, butler=butler)
    retired = []
    host = _host(runtime, retired)
    assert not await RuntimeProcessHost.retire_idle_project(host, "project")
    assert retired == [] and runtime.execution.state == "ready"
    owner, actions = await _retirement_receipts(runtime)
    assert owner.runtime_id == runtime.lease.runtime_id
    assert not owner.retirement_requested
    assert actions == ["project.retirement_aborted"]

    # Once the action's outcome is saved, the idle worker retires.
    butler._dispatching.clear()
    assert await RuntimeProcessHost.retire_idle_project(host, "project")
    owner, actions = await _retirement_receipts(runtime)
    assert owner.runtime_id is None
    assert actions == ["project.retirement_aborted", "project.quiesced"]
    assert retired == [runtime.lease.runtime_id]


async def test_older_release_idle_worker_skip_is_recorded_once(mixed, monkeypatch):
    recorded = []

    def capture(*args, **fields):
        recorded.append((args, fields))

    # raising=False: a release without the skip diagnostic must fail on the
    # missing record below, not on the patch itself.
    monkeypatch.setattr(
        runtime_auto_admission, "record_diagnostic", capture, raising=False
    )
    await owned_project(mixed, "chatless", "older-chatless", "older")
    await owned_project(mixed, "archived", "older-archived", "older", chat="archived")
    retired = []
    automatic = admission(mixed, retired)
    async with mixed.database.session() as session:
        receipts = await session.scalar(
            select(func.count()).select_from(DeploymentReceiptRow)
        )
    for _tick in range(5):
        await automatic.reconcile()

    # The #1111 rule stands: neither older worker is asked to retire.
    assert retired == []
    async with mixed.database.session() as session:
        owners = {
            row.project_id: row.runtime_id
            for row in await session.scalars(select(ProjectOwnershipRow))
        }
        assert not list(await session.scalars(select(ProjectAdmissionHoldRow)))
        assert receipts == await session.scalar(
            select(func.count()).select_from(DeploymentReceiptRow)
        )
    assert owners == {"chatless": "older-chatless", "archived": "older-archived"}
    # Each skipped worker says why exactly once across every pass.
    assert sorted(fields["project_id"] for _args, fields in recorded) == [
        "archived",
        "chatless",
    ]
    for args, fields in recorded:
        assert args[:3] == ("info", "runtime", "runtime.idle_retirement.skipped")
        assert "cannot quiesce for idle retirement" in args[3]
        assert fields["metadata"] == {
            "kind": "release_lacks_feature",
            "version": "older",
        }
        assert fields["force"] is True
