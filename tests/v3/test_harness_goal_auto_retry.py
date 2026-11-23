"""A running harness goal continues once after a transient vendor failure."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from nebula.v3 import harnesses
from nebula.v3.chat_goals import ChatGoalService, GoalConversationCreate, GoalWrite
from nebula.v3.domain import (
    ChatBackend,
    ChatGoalStatus,
    ChatTokenUsage,
    ChatTurn,
    ChatTurnStatus,
    HarnessTurn,
)
from nebula.v3.harnesses import (
    HarnessEvent,
    HarnessProviderError,
    HarnessTransportClosedError,
    HarnessTransportError,
    HarnessTurnFailedError,
    HarnessUnresponsiveError,
    _harness_goal_retryable_failure,
)
from nebula.v3.recovery_markers import GOAL_PROVIDER_RETRY, TURN_GOAL_AUTO_RETRY
from tests.v3.test_harnesses import FakeConnection, _runtime

DROP = "The Codex app-server transport closed"


def _drop() -> BaseException:
    return HarnessTransportClosedError(DROP)


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (HarnessTransportClosedError("Codex app-server transport closed"), True),
        (HarnessTransportClosedError("The Claude CLI closed its message stream"), True),
        (
            HarnessUnresponsiveError("Codex stopped responding", reason_code="timeout"),
            True,
        ),
        (
            HarnessUnresponsiveError("Grok exited", reason_code="transport_closed"),
            True,
        ),
        (ConnectionResetError("reset by peer"), True),
        (
            HarnessTurnFailedError(
                "stream disconnected",
                vendor_error_info={"responseStreamDisconnected": {}},
            ),
            True,
        ),
        (
            HarnessTurnFailedError(
                "connection failed",
                vendor_error_info={"httpConnectionFailed": {"httpStatusCode": 502}},
            ),
            True,
        ),
        (
            HarnessTurnFailedError(
                "connection failed",
                vendor_error_info={"httpConnectionFailed": {"httpStatusCode": 401}},
            ),
            False,
        ),
        (
            HarnessTurnFailedError("rate", vendor_error_info="rateLimitExceeded"),
            True,
        ),
        (
            HarnessTurnFailedError("server", vendor_error_info="internalServerError"),
            True,
        ),
        (
            HarnessTurnFailedError(
                "Claude turn failed: API Error: 529 overloaded",
                vendor_error_info={"subtype": "error", "api_error_status": 529},
            ),
            True,
        ),
        (
            HarnessTurnFailedError(
                "Claude turn failed: not logged in · Please run /login",
                reason_code="authentication_failed",
                vendor_error_info={"subtype": "error", "api_error_status": None},
            ),
            False,
        ),
        (
            HarnessTurnFailedError(
                "Claude turn failed: invalid request",
                vendor_error_info={"subtype": "error", "api_error_status": 400},
            ),
            False,
        ),
        (
            HarnessTurnFailedError("Unauthorized", vendor_error_info="unauthorized"),
            False,
        ),
        (
            HarnessTurnFailedError(
                "You've hit your usage limit", vendor_error_info="usageLimitExceeded"
            ),
            False,
        ),
        (
            HarnessTurnFailedError(
                "context window exceeded", vendor_error_info="contextWindowExceeded"
            ),
            False,
        ),
        (
            HarnessTurnFailedError("blocked", vendor_error_info="cyberPolicy"),
            False,
        ),
        (
            HarnessTurnFailedError("bad", vendor_error_info="badRequest"),
            False,
        ),
        # Left to the goal's capacity recovery, which checks effects first.
        (
            HarnessTurnFailedError(
                "Selected model is at capacity.", vendor_error_info="serverOverloaded"
            ),
            False,
        ),
        (HarnessTurnFailedError("Codex turn failed: something odd"), False),
        (HarnessProviderError("grok", {"code": -32603, "http_status": 503}), True),
        (HarnessProviderError("grok", {"code": -32603, "http_status": 400}), False),
        (
            HarnessProviderError(
                "grok", {"message": "insufficient quota", "http_status": 429}
            ),
            False,
        ),
        (HarnessTransportError("Codex returned malformed JSON"), False),
        (HarnessTransportError("uncertain transport loss"), False),
        (RuntimeError("Nebula defect"), False),
        (OSError("No such file or directory"), False),
    ],
)
def test_transient_harness_failure_classification(error, retryable):
    assert _harness_goal_retryable_failure(error) is retryable


class ScriptedConnection(FakeConnection):
    """Each turn takes its outcome from the shared script: an error or a reply."""

    script: list[Callable[[], BaseException] | None] = []
    gate: asyncio.Event | None = None

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        yield HarnessEvent(
            type="started",
            external_session_id="retry-session",
            external_turn_id=f"retry-turn-{len(self.prompts)}",
        )
        yield HarnessEvent(
            type="usage",
            usage=ChatTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome()
        yield HarnessEvent(type="completed", message="Goal step done")


async def _scenario(tmp_path, script, *, step_budget: int | None = 3, gate=None):
    store, _engagement, _profile, _mcp, adapter, runtime = await _runtime(tmp_path)
    prompts: list[str] = []

    class Connection(ScriptedConnection):
        pass

    Connection.script = list(script)
    Connection.gate = gate

    async def open_scripted(request):
        connection = Connection(request)
        connection.prompts = prompts
        adapter.connections.append(connection)
        return connection

    adapter.open = open_scripted
    # Each turn's task settles its goal after leaving _chat_turn_tasks, so
    # tests await the tasks themselves.
    started: list[asyncio.Task[None]] = []
    start_chat_turn = runtime.start_chat_turn

    async def tracked_start(turn_id: str) -> asyncio.Task[None]:
        task = await start_chat_turn(turn_id)
        started.append(task)
        return task

    runtime.start_chat_turn = tracked_start
    runtime.started_test_tasks = started
    goals = ChatGoalService(store, runtime)
    created = await goals.create_conversation(
        GoalConversationCreate(
            engagement_id="eng-a",
            backend=ChatBackend.HARNESS,
            harness_profile_id="harness-a",
            model="test-model",
            tools_enabled=False,
            objective="Investigate the project and retain evidence",
            completion_criteria=["The result has evidence"],
            step_budget=step_budget,
        )
    )
    await goals.write(
        created.session.id,
        GoalWrite(expected_revision=created.goal.revision, action="start"),
    )
    return store, runtime, goals, created.session.id, prompts


async def _settled(runtime) -> None:
    async with asyncio.timeout(10):
        while pending := [
            task for task in runtime.started_test_tasks if not task.done()
        ]:
            await asyncio.wait(pending)


async def _turns(store, session_id) -> list[ChatTurn]:
    return await store.list_session_entities(ChatTurn, session_id)


async def _reconcile(runtime) -> list[str]:
    # The reconciler acts on a goal it saw unchanged on two passes.
    assert await runtime.reconcile_idle_running_goals() == []
    return await runtime.reconcile_idle_running_goals()


async def test_transient_failure_keeps_goal_running_and_continues_once(tmp_path):
    store, runtime, goals, session_id, prompts = await _scenario(
        tmp_path, [_drop], step_budget=2
    )
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)

    goal = await goals.get(session_id)
    [failed] = await _turns(store, session_id)
    assert failed.status == ChatTurnStatus.INTERRUPTED
    assert goal.status == ChatGoalStatus.RUNNING
    assert goal.execution_claim_id is None
    assert goal.metadata[GOAL_PROVIDER_RETRY]["failed_turn_id"] == failed.id
    assert DROP in goal.metadata[GOAL_PROVIDER_RETRY]["error"]
    assert isinstance(failed.request_snapshot.get(TURN_GOAL_AUTO_RETRY), dict)
    harness_turn = await store.get(HarnessTurn, failed.harness_turn_id)
    assert harness_turn.metadata["diagnostic"]["goal_retryable"] is True

    assert await _reconcile(runtime) == [goal.id]
    await _settled(runtime)
    turns = await _turns(store, session_id)
    assert len(turns) == 2
    assert turns[1].status == ChatTurnStatus.COMPLETE
    assert "cut off by a transient" in prompts[1]
    goal = await goals.get(session_id)
    # A completed turn ends the failure streak; the step budget then pauses.
    assert GOAL_PROVIDER_RETRY not in goal.metadata
    assert goal.status == ChatGoalStatus.PAUSED
    assert goal.blocked_reason == "Goal step budget is exhausted."
    # Nothing is dispatched twice.
    assert await _reconcile(runtime) == []
    assert len(await _turns(store, session_id)) == 2
    await runtime.shutdown()


async def test_second_transient_failure_in_a_row_pauses(tmp_path):
    store, runtime, goals, session_id, prompts = await _scenario(
        tmp_path, [_drop, _drop]
    )
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)
    assert (await goals.get(session_id)).status == ChatGoalStatus.RUNNING
    assert len(await _reconcile(runtime)) == 1
    await _settled(runtime)

    goal = await goals.get(session_id)
    turns = await _turns(store, session_id)
    assert len(turns) == 2
    assert all(turn.status == ChatTurnStatus.INTERRUPTED for turn in turns)
    assert goal.status == ChatGoalStatus.PAUSED
    assert DROP in (goal.blocked_reason or "")
    # The second failure is not marked as retried.
    assert TURN_GOAL_AUTO_RETRY not in turns[1].request_snapshot
    assert await _reconcile(runtime) == []
    assert len(await _turns(store, session_id)) == 2

    # An operator resume starts a new streak.
    resumed = await goals.write(
        session_id, GoalWrite(expected_revision=goal.revision, action="resume")
    )
    assert GOAL_PROVIDER_RETRY not in resumed.metadata
    await _settled(runtime)
    await runtime.shutdown()


async def test_completed_turn_rearms_the_retry(tmp_path):
    store, runtime, goals, session_id, _prompts = await _scenario(
        tmp_path, [_drop, None, _drop], step_budget=None
    )
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)
    # The retry completes and the goal continues; the next turn drops again
    # and is retried again instead of pausing.
    await _reconcile(runtime)
    await _settled(runtime)
    turns = await _turns(store, session_id)
    goal = await goals.get(session_id)
    assert [turn.status for turn in turns] == [
        ChatTurnStatus.INTERRUPTED,
        ChatTurnStatus.COMPLETE,
        ChatTurnStatus.INTERRUPTED,
    ]
    assert goal.status == ChatGoalStatus.RUNNING
    assert goal.metadata[GOAL_PROVIDER_RETRY]["failed_turn_id"] == turns[2].id
    await goals.write(
        session_id, GoalWrite(expected_revision=goal.revision, action="pause")
    )
    await runtime.shutdown()


@pytest.mark.parametrize(
    "error",
    [
        lambda: HarnessTurnFailedError(
            "Unauthorized",
            reason_code="authentication_failed",
            vendor_error_info="unauthorized",
        ),
        lambda: HarnessTurnFailedError(
            "You've hit your usage limit",
            reason_code="quota_exhausted",
            vendor_error_info="usageLimitExceeded",
        ),
        lambda: HarnessTransportError("uncertain transport loss"),
    ],
    ids=["sign-in", "quota", "unknown-transport"],
)
async def test_non_transient_failure_pauses_without_retry(tmp_path, error):
    store, runtime, goals, session_id, _prompts = await _scenario(tmp_path, [error])
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)
    goal = await goals.get(session_id)
    assert goal.status == ChatGoalStatus.PAUSED
    assert GOAL_PROVIDER_RETRY not in goal.metadata
    [turn] = await _turns(store, session_id)
    assert TURN_GOAL_AUTO_RETRY not in turn.request_snapshot
    assert await _reconcile(runtime) == []
    await runtime.shutdown()


async def test_operator_stop_pauses_without_retry(tmp_path):
    gate = asyncio.Event()
    store, runtime, goals, session_id, prompts = await _scenario(
        tmp_path, [_drop], gate=gate
    )
    await runtime.dispatch_running_goal(session_id, "Begin work")
    async with asyncio.timeout(10):
        while not prompts:
            await asyncio.sleep(0.01)
    [turn] = await _turns(store, session_id)
    await runtime.cancel_turn(turn.harness_turn_id, reason="Operator stop")
    await _settled(runtime)
    goal = await goals.get(session_id)
    assert goal.status == ChatGoalStatus.PAUSED
    assert GOAL_PROVIDER_RETRY not in goal.metadata
    assert await _reconcile(runtime) == []
    assert len(await _turns(store, session_id)) == 1
    await runtime.shutdown()


async def test_failure_while_core_shuts_down_is_not_retried(tmp_path):
    gate = asyncio.Event()
    store, runtime, goals, session_id, prompts = await _scenario(
        tmp_path, [_drop], gate=gate
    )
    await runtime.dispatch_running_goal(session_id, "Begin work")
    async with asyncio.timeout(10):
        while not prompts:
            await asyncio.sleep(0.01)
    # Shutdown has begun when the vendor stream drops; restart recovery owns it.
    runtime._closed = True
    gate.set()
    await _settled(runtime)
    goal = await goals.get(session_id)
    assert goal.status == ChatGoalStatus.PAUSED
    assert GOAL_PROVIDER_RETRY not in goal.metadata
    runtime._closed = False
    await runtime.shutdown()


async def test_held_project_is_neither_armed_nor_continued(tmp_path, monkeypatch):
    store, runtime, goals, session_id, _prompts = await _scenario(
        tmp_path, [_drop, _drop]
    )
    held = False

    async def fake_is_held(_store, _engagement_id, _session_id=None):
        return held

    monkeypatch.setattr(harnesses, "is_held", fake_is_held)
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)
    goal = await goals.get(session_id)
    assert goal.status == ChatGoalStatus.RUNNING
    # A hold placed after the failure keeps the continuation from starting.
    held = True
    assert await _reconcile(runtime) == []
    assert len(await _turns(store, session_id)) == 1
    assert (await goals.get(session_id)).status == ChatGoalStatus.RUNNING
    await runtime.shutdown()


async def test_failure_in_held_project_pauses_without_mark(tmp_path, monkeypatch):
    store, runtime, goals, session_id, _prompts = await _scenario(tmp_path, [_drop])

    async def fake_is_held(_store, _engagement_id, _session_id=None):
        return True

    monkeypatch.setattr(harnesses, "is_held", fake_is_held)
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)
    goal = await goals.get(session_id)
    assert goal.status == ChatGoalStatus.PAUSED
    assert GOAL_PROVIDER_RETRY not in goal.metadata
    await runtime.shutdown()


async def test_paused_goal_is_not_continued(tmp_path):
    store, runtime, goals, session_id, _prompts = await _scenario(tmp_path, [_drop])
    await runtime.dispatch_running_goal(session_id, "Begin work")
    await _settled(runtime)
    goal = await goals.get(session_id)
    assert GOAL_PROVIDER_RETRY in goal.metadata
    await goals.write(
        session_id, GoalWrite(expected_revision=goal.revision, action="pause")
    )
    assert await _reconcile(runtime) == []
    assert len(await _turns(store, session_id)) == 1
    assert (await goals.get(session_id)).status == ChatGoalStatus.PAUSED
    await runtime.shutdown()


async def _rpc_failure(tmp_path, body: str) -> BaseException:
    import sys

    from nebula.v3.harnesses import _CodexRpc

    script = tmp_path / "app_server.py"
    script.write_text(body, encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    rpc = _CodexRpc(process=process)
    await rpc.start()
    try:
        with pytest.raises(HarnessTransportError) as raised:
            await asyncio.wait_for(rpc.request("turn/start", {}), timeout=10)
        return raised.value
    finally:
        await asyncio.wait_for(rpc.close(), timeout=15)


async def test_app_server_exit_mid_request_is_a_closed_transport(tmp_path):
    error = await _rpc_failure(
        tmp_path, "import sys\nsys.stdin.readline()\nsys.exit(3)\n"
    )
    assert isinstance(error, HarnessTransportClosedError)
    assert _harness_goal_retryable_failure(error)


async def test_malformed_app_server_reply_is_not_retryable(tmp_path):
    error = await _rpc_failure(
        tmp_path,
        "import sys, time\nsys.stdin.readline()\n"
        "print('{\"id\": 1, \"result\": ', flush=True)\ntime.sleep(30)\n",
    )
    assert not isinstance(error, HarnessTransportClosedError)
    assert not _harness_goal_retryable_failure(error)
