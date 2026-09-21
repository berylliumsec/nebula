"""Claude harness turns stay isolated on the CLI's single ordered stream.

The real ``claude_agent_sdk.ClaudeSDKClient`` runs over a scripted CLI
transport: the SDK parses every message, routes interrupt control requests and
reports process exits exactly as it does for the real CLI subprocess. Only the
subprocess is replaced.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import claude_agent_sdk
import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatMessage,
    ChatTurn,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessTurn,
    HarnessTurnStatus,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    ClaudeAgentSdkConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessRuntimeService,
    HarnessTransportError,
    HarnessTurnFailedError,
)
from nebula.v3.storage import NebulaStore

SESSION = "claude-session-1"
WAIT = "WAIT"
TURN_DEADLINE_SECONDS = 10.0


def stream_text(
    text: str, *, index: int = 0, parent: str | None = None
) -> list[dict[str, Any]]:
    return [
        _stream_event(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            },
            parent=parent,
        ),
        _stream_event(
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": text},
            },
            parent=parent,
        ),
        _stream_event({"type": "content_block_stop", "index": index}, parent=parent),
    ]


def _stream_event(event: dict[str, Any], *, parent: str | None = None) -> dict:
    return {
        "type": "stream_event",
        "uuid": f"se-{json.dumps(event, sort_keys=True)[:40]}",
        "session_id": SESSION,
        "parent_tool_use_id": parent,
        "event": event,
    }


def message_start(message_id: str) -> dict[str, Any]:
    return _stream_event(
        {"type": "message_start", "message": {"id": message_id, "content": []}}
    )


def assistant(
    blocks: list[dict[str, Any]],
    *,
    parent: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"model": "claude-test", "content": blocks, "usage": None}
    if message_id is not None:
        message["id"] = message_id
    return {
        "type": "assistant",
        "uuid": f"asst-{json.dumps(blocks, sort_keys=True)[:40]}",
        "session_id": SESSION,
        "parent_tool_use_id": parent,
        "message": message,
    }


def user_text(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"user-{text[:20]}",
        "session_id": SESSION,
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def tool_round(tool_id: str, name: str = "Read") -> list[dict[str, Any]]:
    return [
        _stream_event(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {},
                },
            }
        ),
        assistant(
            [{"type": "tool_use", "id": tool_id, "name": name, "input": {"p": "a"}}]
        ),
        {
            "type": "user",
            "uuid": f"user-{tool_id}",
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": "hi"}
                ],
            },
        },
    ]


def result(
    subtype: str = "success",
    *,
    is_error: bool = False,
    text: str | None = "ok",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": is_error,
        "num_turns": 1,
        "session_id": SESSION,
        "result": text,
        "usage": {"input_tokens": 3, "output_tokens": 2},
        **extra,
    }


def answer(text: str) -> list[dict[str, Any]]:
    return [
        *stream_text(text),
        assistant([{"type": "text", "text": text}]),
        result(text=text),
    ]


INTERRUPTED_TAIL = [
    user_text("[Request interrupted by user]"),
    result("error_during_execution", text=None, terminal_reason="aborted_streaming"),
]


class ScriptedCli(claude_agent_sdk.Transport):
    """The Claude Code CLI's stdio contract, scripted per user message.

    Each user message runs the next script. A step is a message to emit,
    ``WAIT`` (block until an interrupt arrives, emit ``on_interrupt`` and end
    the turn), ``("SLEEP", seconds)`` or ``("EXIT", code)`` (end stdout; the SDK
    reports a non-zero code as a process failure). Like the CLI, user messages
    are processed one at a time, in order, and an interrupt that arrives while
    idle stops nothing.
    """

    def __init__(
        self,
        turns: list[list[Any]],
        *,
        on_interrupt: list[dict[str, Any]] | None = None,
    ) -> None:
        self.turns = list(turns)
        self.on_interrupt = list(
            INTERRUPTED_TAIL if on_interrupt is None else on_interrupt
        )
        self.prompts: list[str] = []
        self.interrupts = 0
        self.emitted = 0
        self._out: asyncio.Queue[Any] = asyncio.Queue()
        self._scripts: asyncio.Queue[list[Any]] = asyncio.Queue()
        self._interrupted = asyncio.Event()
        self._ready = False
        self._runner: asyncio.Future[None] | None = None

    async def connect(self) -> None:
        self._ready = True
        self._runner = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while True:
            script = await self._scripts.get()
            self._interrupted.clear()
            for step in script:
                if step == WAIT:
                    await self._interrupted.wait()
                    for item in self.on_interrupt:
                        await self._out.put(item)
                    break
                if isinstance(step, tuple) and step[0] == "SLEEP":
                    await asyncio.sleep(step[1])
                    continue
                if isinstance(step, tuple) and step[0] == "EXIT":
                    self._ready = False
                    await self._out.put(step)
                    return
                await self._out.put(step)
                self.emitted += 1

    def delivered(self, count: int) -> bool:
        """The CLI wrote ``count`` script messages and the SDK read them all."""

        return self.emitted >= count and self._out.empty()

    async def write(self, data: str) -> None:
        if not self._ready:
            raise claude_agent_sdk.CLIConnectionError(
                "Cannot write to terminated process (exit code: 1)"
            )
        message = json.loads(data)
        if message.get("type") == "control_request":
            await self._out.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": {},
                    },
                }
            )
            if message["request"].get("subtype") == "interrupt":
                self.interrupts += 1
                self._interrupted.set()
            return
        if message.get("type") == "user":
            self.prompts.append(str(message["message"]["content"]))
            script = self.turns.pop(0) if self.turns else [result(text="")]
            await self._scripts.put(script)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._out.get()
            if isinstance(item, tuple) and item[0] == "EXIT":
                if item[1]:
                    raise claude_agent_sdk.ProcessError(
                        f"Command failed with exit code {item[1]}",
                        exit_code=item[1],
                        stderr="Check stderr output for details",
                    )
                return
            yield item

    async def close(self) -> None:
        self._ready = False
        if self._runner is not None:
            self._runner.cancel()

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None


async def _no_permission(_request: Any) -> Any:
    raise AssertionError("no permission request expected")


async def _connection(cli: ScriptedCli, workspace: Path) -> ClaudeAgentSdkConnection:
    client = claude_agent_sdk.ClaudeSDKClient(
        options=claude_agent_sdk.ClaudeAgentOptions(), transport=cli
    )
    await client.connect()
    return ClaudeAgentSdkConnection(
        client,
        permission_handler=_no_permission,
        sdk=claude_agent_sdk,
        external_session_id=None,
        workspace=workspace,
    )


async def _turn(
    connection: ClaudeAgentSdkConnection, prompt: str
) -> list[HarnessEvent]:
    async def collect() -> list[HarnessEvent]:
        return [event async for event in connection.run_turn(prompt, model="m")]

    return await asyncio.wait_for(collect(), timeout=TURN_DEADLINE_SECONDS)


class ScriptedAdapter(HarnessAdapter):
    kind = HarnessKind.CLAUDE_AGENT_SDK

    def __init__(self, clis: Callable[[], ScriptedCli]) -> None:
        self.clis = clis
        self.opened: list[ScriptedCli] = []

    async def probe(self, profile: Any, credential_store: Any) -> HarnessHealth:
        raise NotImplementedError

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        cli = self.clis()
        self.opened.append(cli)
        return await _connection(cli, request.workspace)


class Chat:
    """A real HarnessRuntimeService whose Claude adapter speaks to scripted CLIs."""

    def __init__(
        self,
        tmp_path: Path,
        clis: Callable[[], ScriptedCli] | None = None,
        *,
        adapter: HarnessAdapter | None = None,
    ) -> None:
        self.store = NebulaStore(tmp_path / "nebula.db")
        self.engagement = self.store.create(Engagement(id="eng-a", name="Engagement A"))
        self.profile = self.store.create(
            HarnessProfile(
                id="claude-a",
                name="Claude fixture",
                kind=HarnessKind.CLAUDE_AGENT_SDK,
                executable="/bin/true",
                default_model="claude-test",
                privacy={"local_only": True, "permits_sensitive_data": True},
            )
        )
        if adapter is None:
            assert clis is not None
            adapter = ScriptedAdapter(clis)
        self.adapter: Any = adapter
        self.service = HarnessRuntimeService(
            self.store,
            credential_store=CredentialStore(),
            workspace_resolver=lambda _: tmp_path,
            adapter_factory=lambda _: self.adapter,
        )
        self.chat_id: str | None = None
        self.harness_session_id: str | None = None

    def prepare(self, prompt: str) -> tuple[ChatTurn, HarnessTurn]:
        chat, owner, turn = self.service.prepare_chat(
            engagement_id=self.engagement.id,
            profile_id=self.profile.id,
            model=None,
            prompt=prompt,
            chat_session_id=self.chat_id,
            harness_session_id=self.harness_session_id,
            mcp_server_ids=[],
        )
        self.chat_id = chat.id
        self.harness_session_id = turn.harness_session_id
        return owner, turn

    async def send(self, prompt: str) -> tuple[HarnessTurn, str | None]:
        owner, turn = self.prepare(prompt)
        await asyncio.wait_for(
            self.service.start_chat_turn(turn.id), timeout=TURN_DEADLINE_SECONDS
        )
        return self.store.get(HarnessTurn, turn.id), self.answer(owner.id)

    def answer(self, owner_id: str) -> str | None:
        owner = self.store.get(ChatTurn, owner_id)
        if not owner.final_message_id:
            return None
        return self.store.get(ChatMessage, owner.final_message_id).content


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached")


@pytest.mark.parametrize("interrupt_is_error", [False, True])
def test_claude_stop_drains_the_interrupted_turn_before_the_next_prompt(
    tmp_path: Path, interrupt_is_error: bool
) -> None:
    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [*stream_text("FIRST partial "), WAIT],
                answer("SECOND ANSWER"),
                answer("THIRD ANSWER"),
            ],
            on_interrupt=[
                user_text("[Request interrupted by user]"),
                result(
                    "error_during_execution",
                    is_error=interrupt_is_error,
                    text=None,
                    terminal_reason="aborted_streaming",
                ),
            ],
        )
        chat = Chat(tmp_path, lambda: cli)
        _owner, first = chat.prepare("first")
        task = chat.service.start_chat_turn(first.id)
        await _until(
            lambda: any(
                event.type == "message_delta"
                for event in chat.service.activity_events(first.id).events
            )
        )
        await chat.service.cancel_turn(first.id, reason="Operator stop")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert chat.store.get(HarnessTurn, first.id).status == (
            HarnessTurnStatus.CANCELLED
        )

        second, second_answer = await chat.send("second")
        third, third_answer = await chat.send("third")

        assert (second.status, second_answer) == (
            HarnessTurnStatus.COMPLETE,
            "SECOND ANSWER",
        )
        assert (third.status, third_answer) == (
            HarnessTurnStatus.COMPLETE,
            "THIRD ANSWER",
        )
        assert cli.interrupts == 1
        # The stop settled the stream, so the session kept its CLI process.
        assert len(chat.adapter.opened) == 1
        await chat.service.shutdown()

    asyncio.run(scenario())


def test_claude_abandoned_turn_is_stopped_and_drained_by_the_next_prompt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        cli = ScriptedCli([[*stream_text("FIRST partial "), WAIT], answer("SECOND")])
        connection = await _connection(cli, tmp_path)
        events = connection.run_turn("first", model="m")
        async for event in events:
            if event.type == "message_delta":
                break
        # The consumer went away without stopping the vendor turn.
        await events.aclose()

        second = await _turn(connection, "second")

        assert second[-1].type == "completed"
        assert second[-1].message == "SECOND"
        assert cli.interrupts == 1
        assert cli.prompts == ["first", "second"]
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("still_running", [False, True])
def test_claude_background_turn_the_cli_started_is_not_the_next_answer(
    tmp_path: Path, still_running: bool
) -> None:
    injected_turn = [
        user_text(
            "<task-notification>scan done</task-notification>",
            origin={"kind": "task-notification"},
        ),
        *stream_text("Background scan finished: 3 hosts."),
        ("SLEEP", 0.4 if still_running else 0),
        result(
            text="Background scan finished: 3 hosts.",
            origin={"kind": "task-notification"},
        ),
    ]

    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [
                    *answer("Started the scan in the background."),
                    ("SLEEP", 0.05),
                    *injected_turn,
                ],
                answer("ANSWER TO SECOND PROMPT"),
            ]
        )
        connection = await _connection(cli, tmp_path)
        first = await _turn(connection, "first")
        assert first[-1].message == "Started the scan in the background."
        # The operator types the next message later, after (or while) the CLI
        # ran a turn of its own for the finished background task: the first
        # answer (5 messages), then the injected prompt, its three stream
        # events and, unless it is still running, its result.
        await _until(lambda: cli.delivered(9 if still_running else 10))
        await asyncio.sleep(0.05)

        second = await _turn(connection, "second")

        assert second[-1].type == "completed"
        assert second[-1].message == "ANSWER TO SECOND PROMPT"
        deltas = "".join(
            event.delta or "" for event in second if event.type == "message_delta"
        )
        assert deltas == "ANSWER TO SECOND PROMPT"
        background = [
            event
            for event in second
            if event.type == "notice" and event.payload.get("out_of_band") is True
        ]
        assert [event.summary for event in background] == [
            "Background scan finished: 3 hosts."
        ]
        assert cli.interrupts == 0
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["crash", "eof", "idle_exit"])
def test_claude_cli_exit_discards_the_connection_and_the_next_turn_reopens(
    tmp_path: Path, failure: str
) -> None:
    first_turn = {
        "crash": [*stream_text("partial "), ("EXIT", 137)],
        "eof": [*stream_text("partial "), ("EXIT", 0)],
        "idle_exit": [*answer("first answer"), ("SLEEP", 0.05), ("EXIT", 1)],
    }[failure]
    scripts = [[first_turn], [answer("fresh answer")]]

    async def scenario() -> None:
        chat = Chat(tmp_path, lambda: ScriptedCli(scripts.pop(0)))

        first, first_answer = await chat.send("first")
        if failure == "idle_exit":
            assert (first.status, first_answer) == (
                HarnessTurnStatus.COMPLETE,
                "first answer",
            )
            connection = chat.service._connections[first.harness_session_id]
            await _until(lambda: connection.connection_state == "disconnected")
        else:
            # A stream that ends without a result is not a completed turn.
            assert first.status == HarnessTurnStatus.INTERRUPTED
            assert first_answer is None
            assert first.harness_session_id not in chat.service._connections

        second, second_answer = await chat.send("second")

        assert (second.status, second_answer) == (
            HarnessTurnStatus.COMPLETE,
            "fresh answer",
        )
        assert len(chat.adapter.opened) == 2
        await chat.service.shutdown()

    asyncio.run(scenario())


def test_claude_turn_failure_interrupts_the_cli_and_isolates_the_next_turn(
    tmp_path: Path,
) -> None:
    # SDK 0.2.118 cannot parse a task notification without ``output_file``.
    malformed = {
        "type": "system",
        "subtype": "task_notification",
        "task_id": "t1",
        "status": "completed",
        "summary": "done",
        "uuid": "u1",
        "session_id": SESSION,
    }

    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [*stream_text("FIRST partial "), malformed, WAIT],
                answer("SECOND ANSWER"),
            ]
        )
        chat = Chat(tmp_path, lambda: cli)

        first, _ = await chat.send("first")
        second, second_answer = await chat.send("second")

        assert first.status == HarnessTurnStatus.INTERRUPTED
        assert "output_file" in (first.error or "")
        assert cli.interrupts == 1
        assert (second.status, second_answer) == (
            HarnessTurnStatus.COMPLETE,
            "SECOND ANSWER",
        )
        assert len(chat.adapter.opened) == 1
        await chat.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("final_result", ["The file says hi.", None])
def test_claude_answer_excludes_pre_tool_narration(
    tmp_path: Path, final_result: str | None
) -> None:
    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [
                    *stream_text("I'll read the file first."),
                    assistant([{"type": "text", "text": "I'll read the file first."}]),
                    *tool_round("t1"),
                    *stream_text("The file says hi."),
                    assistant([{"type": "text", "text": "The file says hi."}]),
                    result(text=final_result),
                ]
            ]
        )
        connection = await _connection(cli, tmp_path)

        events = await _turn(connection, "read it")

        assert events[-1].type == "completed"
        assert events[-1].message == "The file says hi."
        commentary = [
            event
            for event in events
            if event.type == "output_delta" and event.stream == "commentary"
        ]
        assert "".join(event.delta or "" for event in commentary) == (
            "I'll read the file first."
        )
        assert {event.item_kind for event in commentary} == {"reasoning"}
        tool_started = next(event for event in events if event.type == "tool_started")
        assert events.index(commentary[-1]) < events.index(tool_started)
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("final_result", ["Final: all good.", None])
def test_claude_answer_excludes_subagent_text(
    tmp_path: Path, final_result: str | None
) -> None:
    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [
                    *tool_round("task1", "Agent"),
                    *stream_text("SUBAGENT INTERNAL NOTES", parent="task1"),
                    assistant(
                        [{"type": "text", "text": "SUBAGENT INTERNAL NOTES"}],
                        parent="task1",
                    ),
                    *stream_text("Final: all good."),
                    assistant([{"type": "text", "text": "Final: all good."}]),
                    result(text=final_result),
                ]
            ]
        )
        connection = await _connection(cli, tmp_path)

        events = await _turn(connection, "delegate")

        assert events[-1].message == "Final: all good."
        streamed = "".join(
            event.delta or "" for event in events if event.type == "message_delta"
        )
        assert "SUBAGENT" not in streamed
        await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("frame", "expected", "reason_code"),
    [
        (
            result(
                "error_max_turns",
                is_error=True,
                text=None,
                errors=["Reached maximum number of turns (8)"],
                terminal_reason="max_turns",
            ),
            "Reached maximum number of turns (8)",
            None,
        ),
        (
            result(
                is_error=True,
                text="API Error: 529 Overloaded",
                errors=[],
                api_error_status=529,
            ),
            "API Error: 529 Overloaded",
            "dependency_unavailable",
        ),
        (
            result("error_during_execution", is_error=True, text=None),
            "error_during_execution",
            None,
        ),
        (
            result(is_error=True, text=None, api_error_status=500),
            "API error (HTTP 500)",
            "dependency_unavailable",
        ),
    ],
)
def test_claude_error_result_reports_the_cli_reason(
    tmp_path: Path, frame: dict[str, Any], expected: str, reason_code: str | None
) -> None:
    async def scenario() -> None:
        cli = ScriptedCli([[*stream_text("working"), frame]])
        connection = await _connection(cli, tmp_path)
        seen: list[HarnessEvent] = []

        with pytest.raises(Exception) as caught:
            async for event in connection.run_turn("go", model="m"):
                seen.append(event)

        assert str(caught.value) == f"Claude turn failed: {expected}"
        # The CLI reported a turn outcome; its stream is still usable.
        assert isinstance(caught.value, HarnessTurnFailedError)
        assert not isinstance(caught.value, HarnessTransportError)
        assert caught.value.reason_code == reason_code
        notice = next(
            event
            for event in seen
            if event.type == "notice" and event.title == "Claude turn notices"
        )
        assert notice.payload["subtype"] == frame["subtype"]
        assert notice.payload["api_error_status"] == frame.get("api_error_status")
        assert "terminal_reason" in notice.payload
        await connection.close()

    asyncio.run(scenario())


def test_claude_budget_error_keeps_the_connection(tmp_path: Path) -> None:
    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [
                    *stream_text("spending"),
                    result(
                        "error_max_budget_usd",
                        is_error=True,
                        text=None,
                        errors=["Reached maximum budget ($1.00)"],
                    ),
                ],
                answer("SECOND ANSWER"),
            ]
        )
        chat = Chat(tmp_path, lambda: cli)

        first, _ = await chat.send("first")
        second, second_answer = await chat.send("second")

        assert first.status == HarnessTurnStatus.INTERRUPTED
        assert first.error == "Claude turn failed: Reached maximum budget ($1.00)"
        assert (second.status, second_answer) == (
            HarnessTurnStatus.COMPLETE,
            "SECOND ANSWER",
        )
        assert len(chat.adapter.opened) == 1
        await chat.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("message_ids", [True, False])
def test_claude_one_thinking_block_is_one_reasoning_item(
    tmp_path: Path, message_ids: bool
) -> None:
    def start(message_id: str) -> list[dict[str, Any]]:
        return [message_start(message_id)] if message_ids else []

    async def scenario() -> None:
        cli = ScriptedCli(
            [
                [
                    *start("msg_1"),
                    _stream_event(
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "thinking", "thinking": ""},
                        }
                    ),
                    _stream_event(
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "thinking_delta", "thinking": "hmm"},
                        }
                    ),
                    _stream_event({"type": "content_block_stop", "index": 0}),
                    assistant(
                        [{"type": "thinking", "thinking": "hmm", "signature": "sig"}],
                        message_id="msg_1",
                    ),
                    # The next model call restarts its block indexes at 0.
                    *start("msg_2"),
                    *stream_text("answer", index=0),
                    result(text="answer"),
                ]
            ]
        )
        connection = await _connection(cli, tmp_path)

        events = await _turn(connection, "think")

        reasoning = [
            (event.item_id, event.item_status)
            for event in events
            if event.item_kind == "reasoning" and event.type == "item_upsert"
        ]
        assert [status for _, status in reasoning] == ["streaming", "completed"]
        assert len({item_id for item_id, _ in reasoning}) == 1
        assert events[-1].message == "answer"
        await connection.close()

    asyncio.run(scenario())


class RefusingInterruptConnection(HarnessConnection):
    """A turn that fails on Nebula's side and a vendor that refuses the stop."""

    adapter_version = "test"

    def __init__(self, *, fail: bool) -> None:
        self.external_session_id = "vendor-session"
        self.fail = fail
        self.closed = False

    async def run_turn(
        self,
        prompt: str,
        *,
        model: str,
        mode: str | None = None,
        skill: Any = None,
        images: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        yield HarnessEvent(type="started", vendor=HarnessKind.CLAUDE_AGENT_SDK)
        if self.fail:
            raise RuntimeError("Nebula could not handle a vendor message")
        yield HarnessEvent(
            type="completed", vendor=HarnessKind.CLAUDE_AGENT_SDK, message="done"
        )

    async def steer(self, text: str) -> None:
        raise NotImplementedError

    async def interrupt(self) -> None:
        raise RuntimeError("the vendor refused the interrupt")

    async def close(self) -> None:
        self.closed = True


class OpeningAdapter(HarnessAdapter):
    kind = HarnessKind.CLAUDE_AGENT_SDK

    def __init__(self) -> None:
        self.opened: list[RefusingInterruptConnection] = []

    async def probe(self, profile: Any, credential_store: Any) -> HarnessHealth:
        raise NotImplementedError

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        connection = RefusingInterruptConnection(fail=not self.opened)
        self.opened.append(connection)
        return connection


def test_failed_turn_whose_interrupt_fails_discards_the_connection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        adapter = OpeningAdapter()
        chat = Chat(tmp_path, adapter=adapter)

        first, _ = await chat.send("first")
        second, second_answer = await chat.send("second")

        assert first.status == HarnessTurnStatus.INTERRUPTED
        # The abandoned vendor turn could not be stopped, so its connection
        # is closed rather than handed the next prompt.
        assert adapter.opened[0].closed is True
        assert len(adapter.opened) == 2
        assert (second.status, second_answer) == (HarnessTurnStatus.COMPLETE, "done")
        await chat.service.shutdown()

    asyncio.run(scenario())
