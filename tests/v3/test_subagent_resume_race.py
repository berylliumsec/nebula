"""Subagent reports that finish together resume their parent exactly once.

Each finished subagent's delivery callback tries to resume the parked parent
turn. Two callbacks could both pass the "no active work" check before either
started the continuation; the loser's ``start_provider_turn`` then refused
with ``ChatHistoryConflict`` and the generic failure path failed the parent
turn and paused its goal while the winner's continuation was running.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from pathlib import Path

from nebula.v3.chat_subagents import CHAT_SUBAGENT_TERMINAL_STATUSES
from nebula.v3.domain import (
    ChatGoalStatus,
    ChatSession,
    ChatSubagent,
    ChatTurn,
    ChatTurnStatus,
)
from nebula.v3.providers import ModelRequest
from tests.v3.test_chat_subagent_lifecycle import _running_goal
from tests.v3.test_chat_subagents import (
    RoutedProvider,
    _call,
    _finish,
    _request,
    _response,
    _setup,
)
from tests.v3.test_subagent_delivery import _until_saved

ROUTES_REPORT = "ROUTES-REPORT: three routes mapped."
AUTH_REPORT = "AUTH-REPORT: cookies lack SameSite."


class _Rendezvous:
    """Hold the first ``parties`` arrivals until all of them have arrived."""

    def __init__(self, parties: int = 2) -> None:
        self.parties = parties
        self.arrived = 0
        self.ready = asyncio.Event()

    async def meet(self) -> bool:
        if self.arrived >= self.parties:
            return False
        self.arrived += 1
        if self.arrived == self.parties:
            self.ready.set()
        else:
            await asyncio.wait_for(self.ready.wait(), timeout=10)
        return True


def _request_text(request: ModelRequest) -> str:
    parts = [str(message.content) for message in request.messages]
    parts.extend(
        result.output if isinstance(result.output, str) else repr(result.output)
        for result in request.tool_results
    )
    return "\n".join(parts)


async def _park_parent(tmp_path: Path, provider: RoutedProvider):
    store, project, _, chat = await _setup(tmp_path, provider)
    provider.child_gate = asyncio.Event()
    prepared = await chat.prepare_async(
        _request(project, content="Map routes and audit auth.", allow_subagents=True)
    )
    parent_turn_id = await chat.start_provider_turn(prepared)

    async def parked() -> bool:
        turn = await store.get(ChatTurn, parent_turn_id)
        return (
            turn.status == ChatTurnStatus.WAITING_CALLBACK
            and turn.execution_claim_id is None
        )

    await _until_saved(parked)
    session = await store.get(ChatSession, prepared.session.id)
    goals, goal = await _running_goal(store, session)
    parent = await store.get(ChatTurn, parent_turn_id)
    await store.update(
        ChatTurn, parent.id, {"goal_id": goal.id}, expected_revision=parent.revision
    )
    return store, chat, session, parent_turn_id, goals


def _two_child_provider(parent_tail):
    return RoutedProvider(
        parent=[
            _call("p1", "start_subagent", task="Map the API routes.", name="Routes"),
            _call("p2", "start_subagent", task="Audit the auth module.", name="Auth"),
            _call("p3", "wait_subagents", subagent_ids=None, mode=None),
            *parent_tail,
        ],
        child=[_response(text=ROUTES_REPORT), _response(text=AUTH_REPORT)],
    )


async def _settled_without_failure(
    store, chat, session, parent_turn_id, goals, provider
) -> None:
    async def settled() -> bool:
        turn = await store.get(ChatTurn, parent_turn_id)
        return turn.status in {ChatTurnStatus.COMPLETE, ChatTurnStatus.FAILED}

    await _until_saved(settled)
    finished = await store.get(ChatTurn, parent_turn_id)
    assert finished.status == ChatTurnStatus.COMPLETE
    assert finished.error is None
    goal = await goals.get(session.id)
    assert goal.status == ChatGoalStatus.RUNNING
    assert goal.blocked_reason is None
    # The continuation received each report once.
    resumed = provider.parent_requests[3:]
    assert resumed
    seen = _request_text(resumed[0])
    assert seen.count(ROUTES_REPORT) == 1
    assert seen.count(AUTH_REPORT) == 1
    transcript = "\n".join(
        message.content for message in await chat.session_messages(session.id)
    )
    assert "could not resume" not in transcript
    assert ROUTES_REPORT not in transcript and AUTH_REPORT not in transcript
    for record in await chat.subagents.descendants_for_session(session.id):
        assert record.reported_at is not None


async def test_two_subagents_finishing_together_resume_the_parent_once(
    tmp_path: Path,
) -> None:
    provider = _two_child_provider([_finish("p4"), _response(text="Both done.")])
    store, chat, session, parent_turn_id, goals = await _park_parent(
        tmp_path, provider
    )
    subagents = chat.subagents

    # Both children finish before either delivery looks at the parent, and
    # both deliveries find the wait satisfied before either goes on to resume.
    delivery_meet = _Rendezvous()
    real_deliver = subagents._deliver

    async def deliver(record: ChatSubagent) -> None:
        if record.status in CHAT_SUBAGENT_TERMINAL_STATUSES:
            await delivery_meet.meet()
        await real_deliver(record)

    ready_meet = _Rendezvous()
    real_wait_ready = subagents.wait_ready

    async def wait_ready(wait) -> bool:
        ready = await real_wait_ready(wait)
        if ready and "ids" in wait:
            await ready_meet.meet()
        return ready

    prepare_calls: list[str] = []
    real_prepare_resume = chat.prepare_resume

    async def prepare_resume(turn_id: str):
        if turn_id == parent_turn_id:
            prepare_calls.append(turn_id)
        return await real_prepare_resume(turn_id)

    starts: list[str] = []
    real_start = chat.start_provider_turn

    async def start_provider_turn(prepared, *args, **kwargs):
        if prepared.turn is not None and prepared.turn.id == parent_turn_id:
            starts.append(prepared.turn.id)
        return await real_start(prepared, *args, **kwargs)

    subagents._deliver = deliver  # type: ignore[method-assign]
    subagents.wait_ready = wait_ready  # type: ignore[method-assign]
    chat.prepare_resume = prepare_resume  # type: ignore[method-assign]
    chat.start_provider_turn = start_provider_turn  # type: ignore[method-assign]
    provider.child_gate.set()

    await _settled_without_failure(
        store, chat, session, parent_turn_id, goals, provider
    )
    # The race was really exercised: both deliveries found the wait ready
    # together, and only one of them went on to resume the parent.
    assert delivery_meet.arrived == 2
    assert ready_meet.arrived == 2
    assert prepare_calls == [parent_turn_id]
    assert starts == [parent_turn_id]
    await chat.shutdown()


async def test_a_resume_that_loses_to_a_queued_continuation_leaves_it_alone(
    tmp_path: Path,
) -> None:
    """Another start wins between the delivery's check and its own start.

    The winner's runtime is registered and still waiting for admission, so the
    parked turn row has not changed yet: only fresh execution state shows that
    a continuation owns the turn.
    """

    provider = _two_child_provider([_finish("p4"), _response(text="Both done.")])
    store, chat, session, parent_turn_id, goals = await _park_parent(
        tmp_path, provider
    )
    subagents = chat.subagents
    real_prepare_resume = chat.prepare_resume
    real_start = chat.start_provider_turn
    competing: list[str] = []
    conflicts: list[Exception] = []

    async def prepare_resume(turn_id: str):
        prepared = await real_prepare_resume(turn_id)
        if turn_id == parent_turn_id and not competing:
            # An operator's Resume starts the continuation first.
            competing.append(
                await real_start(await real_prepare_resume(turn_id))
            )
        return prepared

    async def start_provider_turn(prepared, *args, **kwargs):
        try:
            return await real_start(prepared, *args, **kwargs)
        except Exception as exc:
            conflicts.append(exc)
            raise

    # Hold the winner between registering its runtime and claiming the turn,
    # as admission under load does, until the losing delivery is done.
    loser_done = asyncio.Event()
    real_resume = subagents._resume_waiting_turn

    async def resume_waiting_turn(turn: ChatTurn) -> None:
        try:
            await real_resume(turn)
        finally:
            if turn.id == parent_turn_id and competing:
                loser_done.set()

    real_claim = chat._claim_execution

    async def claim_execution(prepared, *args, **kwargs):
        if prepared.turn is not None and prepared.turn.id == parent_turn_id:
            await asyncio.wait_for(loser_done.wait(), timeout=10)
        return await real_claim(prepared, *args, **kwargs)

    chat.prepare_resume = prepare_resume  # type: ignore[method-assign]
    chat.start_provider_turn = start_provider_turn  # type: ignore[method-assign]
    chat._claim_execution = claim_execution  # type: ignore[method-assign]
    subagents._resume_waiting_turn = resume_waiting_turn  # type: ignore[method-assign]
    provider.child_gate.set()

    await _settled_without_failure(
        store, chat, session, parent_turn_id, goals, provider
    )
    assert competing == [parent_turn_id]
    assert len(conflicts) == 1
    assert "already has active work" in str(conflicts[0])
    await chat.shutdown()


async def test_a_parent_that_cannot_resume_still_fails_visibly(
    tmp_path: Path,
) -> None:
    provider = _two_child_provider([])
    store, chat, session, parent_turn_id, goals = await _park_parent(
        tmp_path, provider
    )

    async def broken_prepare_resume(turn_id: str):
        raise RuntimeError("provider went away")

    chat.prepare_resume = broken_prepare_resume  # type: ignore[method-assign]
    provider.child_gate.set()

    async def failed() -> bool:
        turn = await store.get(ChatTurn, parent_turn_id)
        return turn.status == ChatTurnStatus.FAILED

    await _until_saved(failed)
    finished = await store.get(ChatTurn, parent_turn_id)
    assert "could not resume" in (finished.error or "")
    assert not chat.has_active_provider_turn(parent_turn_id)

    async def paused() -> bool:
        return (await goals.get(session.id)).status == ChatGoalStatus.PAUSED

    await _until_saved(paused)
    goal = await goals.get(session.id)
    assert "could not resume" in (goal.blocked_reason or "")

    # The failed turn frees the conversation, so the reports post now.
    async def reports_posted() -> bool:
        transcript = "\n".join(
            message.content for message in await chat.session_messages(session.id)
        )
        return ROUTES_REPORT in transcript and AUTH_REPORT in transcript

    await _until_saved(reports_posted)
    await chat.shutdown()
