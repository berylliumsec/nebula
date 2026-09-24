"""Harness activity stays cheap per token, per poll and per follower.

A harness turn streams assistant text a few characters at a time; the status
rail polls session activity every two seconds; and every open conversation
follows its turn. None of these may cost work that grows with the project or
with how long the turn has streamed.
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nebula.v3 import harnesses as harness_module
from nebula.v3 import mcp as mcp_module
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    HarnessSession,
    HarnessTurn,
    HarnessTurnOrigin,
    HarnessTurnStatus,
)
from nebula.v3.harnesses import HarnessEvent, _coalesce_activity_deltas
from nebula.v3.mcp import McpGatewaySession
from tests.v3.test_harnesses import FakeAdapter, FakeConnection, _runtime

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "src" / "nebula" / "v3" / "mcp_gateway.py"
ENTRY = ROOT / "scripts" / "nebula_core_entry.py"


class ScriptedConnection(FakeConnection):
    """Streams a scripted answer; optionally waits on ``release`` mid-turn."""

    def __init__(self, request, script, *, release=None) -> None:
        super().__init__(request)
        self.script = script
        self.release = release

    async def run_turn(
        self, prompt: str, *, model: str, images=None
    ) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        self.external_session_id = self.external_session_id or "vendor-session-1"
        yield HarnessEvent(
            type="started",
            external_session_id=self.external_session_id,
            external_turn_id="vendor-turn-1",
        )
        async for event in self.script(self):
            yield event

    async def interrupt(self) -> None:
        # A vendor acknowledges an interrupt asynchronously; the turn is not
        # terminal until it does.
        await asyncio.sleep(0.2)
        self.interrupted = True


class ScriptedAdapter(FakeAdapter):
    def __init__(self, script, *, release=None) -> None:
        super().__init__()
        self.script = script
        self.release = release

    async def open(self, request):
        self.opens.append(request)
        connection = ScriptedConnection(request, self.script, release=self.release)
        self.connections.append(connection)
        return connection


def _scripted_runtime(tmp_path: Path, script, *, release=None):
    store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
    adapter = ScriptedAdapter(script, release=release)
    runtime.adapter_factory = lambda _kind: adapter
    return store, engagement, profile, adapter, runtime


def _prepare(runtime, engagement, profile, prompt="Report"):
    return runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt=prompt,
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )


def _ledger(store, turn_id: str, event_type: str) -> list:
    return [
        event
        for event in store.replay_operation_events(turn_id, limit=10_000)
        if event.event_type == event_type
    ]


# HARN-8: assistant text is coalesced like command output.


def test_streamed_answer_shares_durable_rows_and_replays_identically(tmp_path):
    answer = "".join(chr(ord("a") + index % 26) for index in range(500))

    async def script(_connection):
        for character in answer:
            yield HarnessEvent(type="message_delta", delta=character, item_id="msg")
            await asyncio.sleep(0.002)
        yield HarnessEvent(type="completed", message=answer)

    async def scenario() -> None:
        store, engagement, profile, _adapter, runtime = _scripted_runtime(
            tmp_path, script
        )
        chat, _chat_turn, turn = _prepare(runtime, engagement, profile)
        started = time.monotonic()
        live = [event async for event in runtime.stream_turn(turn.id)]
        elapsed = time.monotonic() - started

        rows = _ledger(store, turn.id, "harness.message_delta")
        # One row per flush window instead of one per token.
        assert len(rows) <= max(20, int(elapsed / 0.1) + 5), (len(rows), elapsed)
        live_text = "".join(
            event.delta or "" for event in live if event.type == "message_delta"
        )
        replay = runtime.activity_events(turn.id, limit=10_000).events
        replay_text = "".join(
            event.delta or "" for event in replay if event.type == "message_delta"
        )
        assert live_text == replay_text == answer
        # Live viewers and replay see the same fragments at the same positions.
        assert [
            (event.sequence, event.delta)
            for event in live
            if event.type == "message_delta"
        ] == [
            (event.sequence, event.delta)
            for event in replay
            if event.type == "message_delta"
        ]
        saved = [
            message
            for message in store.list_session_entities(ChatMessage, chat.id)
            if message.role == ChatRole.ASSISTANT
        ]
        assert [message.content for message in saved] == [answer]

    asyncio.run(scenario())


def test_coalescing_keeps_distinct_streams_apart_and_flushes_before_others():
    async def source() -> AsyncIterator[HarnessEvent]:
        for event in (
            HarnessEvent(type="message_delta", delta="Hel", item_id="a"),
            HarnessEvent(type="message_delta", delta="lo", item_id="a"),
            HarnessEvent(type="message_delta", delta="\n\nNext", item_id="b"),
            HarnessEvent(
                type="message_delta",
                delta=" goal",
                item_id="b",
                external_turn_id="goal-turn-2",
            ),
            HarnessEvent(type="item_upsert", item_kind="plan", item_id="plan"),
            HarnessEvent(type="message_delta", delta="!", item_id="b"),
        ):
            yield event

    async def scenario() -> list[HarnessEvent]:
        return [event async for event in _coalesce_activity_deltas(source())]

    events = asyncio.run(scenario())
    assert [(event.type, event.item_id, event.delta) for event in events] == [
        ("message_delta", "a", "Hello"),
        ("message_delta", "b", "\n\nNext"),
        ("message_delta", "b", " goal"),
        ("item_upsert", "plan", None),
        ("message_delta", "b", "!"),
    ]


def test_stop_inside_the_coalescing_window_keeps_the_last_text(tmp_path, monkeypatch):
    # A window long enough that nothing is flushed before Stop lands.
    monkeypatch.setattr(harness_module, "ACTIVITY_DELTA_FLUSH_SECONDS", 30.0)

    async def script(connection):
        yield HarnessEvent(type="message_delta", delta="Partial ", item_id="msg")
        yield HarnessEvent(type="message_delta", delta="finding", item_id="msg")
        connection.produced.set()
        await connection.release.wait()

    async def scenario() -> None:
        gate = asyncio.Event()
        store, engagement, profile, adapter, runtime = _scripted_runtime(
            tmp_path, script, release=gate
        )
        produced = asyncio.Event()
        original_open = adapter.open

        async def open_with_signal(request):
            connection = await original_open(request)
            connection.produced = produced
            return connection

        adapter.open = open_with_signal
        chat, _chat_turn, turn = _prepare(runtime, engagement, profile)
        runtime.start_chat_turn(turn.id)
        await asyncio.wait_for(produced.wait(), timeout=5)
        assert _ledger(store, turn.id, "harness.message_delta") == []
        await runtime.cancel_turn(turn.id, reason="Stopped by operator")
        deadline = time.monotonic() + 5
        while turn.id in runtime._chat_turn_tasks:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        saved = [
            message.content
            for message in store.list_session_entities(ChatMessage, chat.id)
            if message.role == ChatRole.ASSISTANT
        ]
        assert saved == ["Partial finding"]
        assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.CANCELLED
        await runtime.shutdown()

    asyncio.run(scenario())


# HARN-9: session activity is bounded and reports late state.


def test_session_activity_reports_state_after_long_streams_and_old_turns(
    tmp_path, monkeypatch
):
    store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
    other = runtime.create_session(
        engagement_id=engagement.id, profile_id=profile.id, model=None
    )
    # A full older page of the project's turns must not hide the reservation.
    with store.transaction() as transaction:
        for index in range(1_001):
            transaction.add(
                HarnessTurn(
                    id=f"older-{index:04d}",
                    engagement_id=engagement.id,
                    harness_session_id=other.id,
                    origin=HarnessTurnOrigin.ANALYSIS,
                    prompt="p",
                    status=HarnessTurnStatus.COMPLETE,
                )
            )
    _chat, _chat_turn, turn = _prepare(runtime, engagement, profile)
    session = store.get(HarnessSession, turn.harness_session_id)
    # Past the first 1,000 ledger rows the old status read ever looked at.
    for _ in range(1_000):
        runtime._persist_activity(
            turn, session, HarnessEvent(type="message_delta", delta="tok ")
        )
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(
            type="item_upsert",
            item_kind="plan",
            item_id="turn-plan",
            plan=[{"id": "p1", "title": "Late plan", "status": "in_progress"}],
        ),
    )
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(
            type="item_upsert",
            item_kind="goal",
            item_id="turn-goal",
            goal={"objective": "Finish the audit", "status": "running"},
        ),
    )
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(type="item_upsert", item_kind="mode", item_id="mode", mode="plan"),
    )

    activity = runtime.session_activity(session.id)
    assert activity.busy is True
    assert activity.turn_id == turn.id
    assert [entry.title for entry in activity.plan] == ["Late plan"]
    assert activity.goal is not None and activity.goal.objective == "Finish the audit"
    assert activity.mode == "plan"

    decoded: list[int] = []
    original = harness_module._activity_event_from_ledger

    def counting(durable):
        decoded.append(durable.sequence)
        return original(durable)

    monkeypatch.setattr(harness_module, "_activity_event_from_ledger", counting)
    # A cold read (after a Core restart) decodes only rows that carry turn
    # state, never the streamed text.
    runtime._turn_activity_states.clear()
    assert runtime.session_activity(session.id).mode == "plan"
    assert len(decoded) == 3

    decoded.clear()
    for _ in range(50):
        runtime._persist_activity(
            turn, session, HarnessEvent(type="message_delta", delta="tok ")
        )
    runtime._persist_activity(
        turn,
        session,
        HarnessEvent(
            type="item_upsert",
            item_kind="plan",
            item_id="turn-plan",
            plan=[{"id": "p1", "title": "Late plan", "status": "completed"}],
        ),
    )
    activity = runtime.session_activity(session.id)
    assert [entry.status for entry in activity.plan] == ["completed"]
    assert activity.goal is not None and activity.goal.status == "running"
    # A later poll reads only what was appended since the previous one.
    assert len(decoded) == 1


# HARN-11: per-turn work is scoped to the conversation, followers are woken.


def test_harness_turn_reads_only_its_own_conversation(tmp_path, monkeypatch):
    store, engagement, profile, _mcp, _adapter, runtime = _runtime(tmp_path)
    chat, _chat_turn, turn = _prepare(runtime, engagement, profile)
    with store.transaction() as transaction:
        for index in range(50):
            transaction.add(
                ChatMessage(
                    engagement_id=engagement.id,
                    session_id="another-chat",
                    sequence=index + 1,
                    role=ChatRole.USER,
                    content="unrelated",
                )
            )
    scanned: list[str] = []
    original = store.list_entities

    def recording(model, **kwargs):
        scanned.append(model.entity_kind)
        return original(model, **kwargs)

    monkeypatch.setattr(store, "list_entities", recording)

    async def scenario() -> None:
        async for _event in runtime.stream_turn(turn.id):
            pass
        _chat, _next_chat_turn, _next = runtime.prepare_chat(
            engagement_id=engagement.id,
            profile_id=profile.id,
            model=None,
            prompt="Next",
            chat_session_id=chat.id,
            harness_session_id=None,
            mcp_server_ids=[],
        )

    asyncio.run(scenario())
    assert ChatMessage.entity_kind not in scanned
    messages = sorted(
        store.list_session_entities(ChatMessage, chat.id), key=lambda m: m.sequence
    )
    assert [message.sequence for message in messages] == [1, 2, 3]
    assert [message.role for message in messages] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
        ChatRole.USER,
    ]


@pytest.mark.parametrize(
    ("last", "status"),
    [
        ("completed", HarnessTurnStatus.COMPLETE),
        # Settles after an awaited vendor interrupt, with no event after it.
        ("interrupted", HarnessTurnStatus.INTERRUPTED),
    ],
)
def test_idle_follower_is_woken_instead_of_polling(tmp_path, monkeypatch, last, status):
    monkeypatch.setattr(harness_module, "ACTIVITY_FOLLOW_IDLE_SECONDS", 30.0)

    async def script(connection):
        await connection.release.wait()
        yield HarnessEvent(type="message_delta", delta="Done", item_id="msg")
        yield HarnessEvent(type=last, message="Done")

    async def scenario() -> None:
        gate = asyncio.Event()
        store, engagement, profile, _adapter, runtime = _scripted_runtime(
            tmp_path, script, release=gate
        )
        _chat, _chat_turn, turn = _prepare(runtime, engagement, profile)
        reads: list[float] = []
        original = runtime.activity_events

        def counting(turn_id, **kwargs):
            reads.append(time.monotonic())
            return original(turn_id, **kwargs)

        monkeypatch.setattr(runtime, "activity_events", counting)
        received: list[tuple[str, float]] = []

        async def follow() -> None:
            async for event in runtime.follow_turn(turn.id):
                received.append((event.type, time.monotonic()))

        runtime.start_chat_turn(turn.id)
        follower = asyncio.create_task(follow())
        deadline = time.monotonic() + 5
        while not any(kind == "started" for kind, _ in received):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        reads.clear()
        await asyncio.sleep(1.0)
        # A quiet turn is not re-read ten times a second.
        assert len(reads) <= 1, len(reads)

        gate.set()
        # The idle fallback is 30 s here: the follower must be woken by the
        # new events and by the owner finishing the turn, never by the timer.
        await asyncio.wait_for(follower, timeout=5)
        kinds = [kind for kind, _ in received]
        assert kinds.index("message_delta") < kinds.index(last)
        assert store.get(HarnessTurn, turn.id).status == status
        await runtime.shutdown()

    asyncio.run(scenario())


# HARN-7: the gateway shim never imports Nebula Core.


def test_gateway_shim_is_a_standard_library_script():
    tree = ast.parse(SHIM.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "the shim runs as a script without a package"
            imported.add((node.module or "").split(".")[0])
    assert imported - {"__future__"} <= set(sys.stdlib_module_names), imported


def test_packaged_entry_serves_the_gateway_without_importing_core():
    probe = (
        "import runpy, sys\n"
        f"sys.argv = ['nebula-core', 'mcp-gateway', '--help']\n"
        "try:\n"
        f"    runpy.run_path({str(ENTRY)!r}, run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    code = exc.code\n"
        "print('exit', code)\n"
        "print('core', sorted(name for name in sys.modules if name.startswith('nebula')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": ""},
        check=True,
    )
    assert "--socket" in result.stdout
    assert "exit 0" in result.stdout
    assert "core []" in result.stdout


def test_gateway_shim_answers_mcp_over_stdio_from_its_launch_command():
    async def scenario() -> None:
        gateway = McpGatewaySession(
            list_tools=lambda params: {"tools": [{"name": "echo"}]},
            call_tool=lambda name, arguments: {},
        )
        launch = await gateway.start()
        assert launch.arguments[:2] == ("-I", str(SHIM.resolve()))
        config = launch.runtime_config()["nebula"]
        process = await asyncio.create_subprocess_exec(
            launch.command,
            *launch.arguments,
            env={**os.environ, **config["env"]},
            cwd=config["cwd"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
            )
            await process.stdin.drain()
            first = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            second = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            assert b'"protocolVersion"' in first
            assert b'"echo"' in second
        finally:
            if process.stdin is not None:
                process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=10)
            await gateway.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("replaced", [False, True])
def test_frozen_gateway_reuses_the_unpacked_core_only_for_the_same_binary(
    monkeypatch, replaced
):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEIcore")
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", sys.executable)
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    identity = mcp_module._executable_identity(sys.executable)
    assert identity is not None
    if replaced:
        identity = (*identity[:3], identity[3] + 1)
    monkeypatch.setattr(mcp_module, "_STARTUP_EXECUTABLE_IDENTITY", identity)

    async def scenario() -> dict[str, str]:
        gateway = McpGatewaySession(
            list_tools=lambda params: {"tools": []},
            call_tool=lambda name, arguments: {},
        )
        launch = await gateway.start()
        try:
            assert launch.arguments[0] == "mcp-gateway"
            return launch.runtime_config()["nebula"]["env"]
        finally:
            await gateway.close()

    environment = asyncio.run(scenario())
    assert environment["NEBULA_MCP_GATEWAY_TOKEN"]
    relaunch = {name for name in environment if name.startswith("_PYI_")}
    if replaced:
        # An upgraded binary on disk must unpack its own runtime.
        assert relaunch == set()
    else:
        assert relaunch == {
            "_PYI_APPLICATION_HOME_DIR",
            "_PYI_ARCHIVE_FILE",
            "_PYI_PARENT_PROCESS_LEVEL",
        }
