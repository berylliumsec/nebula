"""The Grok harness follows the Agent Client Protocol's turn and permission rules.

Protocol-level tests drive the real ``GrokAcpAdapter`` and ``_AcpRpc`` over a
scripted ACP agent subprocess that logs every frame it reads and writes, so a
missing or late client reply is visible in its log. Answer-assembly tests use an
in-memory transport.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatMessage,
    ChatTurn,
    Engagement,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    GrokAcpAdapter,
    GrokAcpConnection,
    HarnessAdapter,
    HarnessConfigurationError,
    HarnessPermissionDecision,
    HarnessRuntimeService,
    PermissionTicket,
    _AcpRpc,
)
from nebula.v3.storage import NebulaStore

AGENT_SOURCE = r"""
import asyncio
import json
import sys

script = json.load(open(sys.argv[1]))
log = open(sys.argv[2], "a", buffering=1)
prompts = list(script["prompts"])


def record(entry):
    log.write(json.dumps(entry) + "\n")


async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=10_000_000)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    replies = {}
    cancel = asyncio.Event()
    queue = asyncio.Queue()

    def send(value):
        frame = {"jsonrpc": "2.0", **value}
        record({"out": frame})
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    async def await_reply(request_id, future, timeout):
        try:
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            reply = "TIMEOUT"
        record({"reply_for": request_id, "reply": reply})

    async def run_prompts():
        while True:
            prompt_id, params = await queue.get()
            steps = prompts.pop(0) if prompts else [{"stop": "end_turn"}]
            session_id = params.get("sessionId")
            cancel.clear()
            for step in steps:
                if "update" in step:
                    send(
                        {
                            "method": "session/update",
                            "params": {"sessionId": session_id, "update": step["update"]},
                        }
                    )
                elif "request" in step:
                    future = loop.create_future()
                    replies[step["id"]] = future
                    send({"id": step["id"], **step["request"]})
                    waiter = await_reply(step["id"], future, step.get("timeout", 5))
                    if step.get("wait", True):
                        await waiter
                    else:
                        asyncio.ensure_future(waiter)
                elif "wait_cancel" in step:
                    try:
                        await asyncio.wait_for(cancel.wait(), step.get("timeout", 5))
                    except asyncio.TimeoutError:
                        record({"cancel": "TIMEOUT"})
                elif "sleep" in step:
                    await asyncio.sleep(step["sleep"])
                elif "stop" in step:
                    send({"id": prompt_id, "result": {"stopReason": step["stop"]}})

    asyncio.ensure_future(run_prompts())
    while line := await reader.readline():
        message = json.loads(line)
        record({"in": message})
        method = message.get("method")
        if method is None:
            future = replies.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(
                    {key: message[key] for key in ("result", "error") if key in message}
                )
            continue
        if method == "initialize":
            send(
                {
                    "id": message["id"],
                    "result": {
                        "protocolVersion": 1,
                        "agentInfo": {"name": "scripted", "version": "0"},
                        "authMethods": [{"id": "cached_token"}],
                        "agentCapabilities": {
                            "loadSession": script.get("load_session", True)
                        },
                    },
                }
            )
        elif method == "session/new":
            send({"id": message["id"], "result": {"sessionId": "sess-1"}})
        elif method == "session/prompt":
            await queue.put((message["id"], message["params"]))
        elif method == "session/cancel":
            cancel.set()
        elif "id" in message:
            send({"id": message["id"], "result": {}})


asyncio.run(main())
"""


class ScriptedGrokAdapter(GrokAcpAdapter):
    """The real Grok adapter, launched against the scripted ACP agent."""

    def __init__(self, script: dict[str, Any], workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.agent_path = workdir / "scripted_acp_agent.py"
        self.agent_path.write_text(AGENT_SOURCE)
        self.script_path = workdir / "agent_script.json"
        self.script_path.write_text(json.dumps(script))
        self.log_path = workdir / "agent_log.jsonl"

    async def _connect(self, *_args: Any, **_kwargs: Any) -> _AcpRpc:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.agent_path),
            str(self.script_path),
            str(self.log_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10_000_000,
        )
        rpc = _AcpRpc(process=process)
        await rpc.start()
        return rpc

    def log(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def reply(self, request_id: int) -> Any:
        return next(
            entry["reply"]
            for entry in self.log()
            if entry.get("reply_for") == request_id
        )

    def index(self, predicate) -> int:
        return next(index for index, entry in enumerate(self.log()) if predicate(entry))

    async def wait_for_log(self, predicate, limit: float = 5.0) -> None:
        for _ in range(int(limit / 0.01)):
            if any(predicate(entry) for entry in self.log()):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the scripted agent never logged the expected frame")


def _chunk(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
        **extra,
    }


def _inbound(method: str | None = None, request_id: Any = None):
    def match(entry: dict[str, Any]) -> bool:
        frame = entry.get("in")
        if not isinstance(frame, dict):
            return False
        if method is not None and frame.get("method") != method:
            return False
        return request_id is None or frame.get("id") == request_id

    return match


def _permission_step(request_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "request": {
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess-1",
                "toolCall": {"toolCallId": "call-1", "title": "browser_navigate"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                    {
                        "optionId": "reject-once",
                        "name": "Reject",
                        "kind": "reject_once",
                    },
                ],
            },
        },
        "id": request_id,
        **extra,
    }


def _open_request(
    tmp_path: Path, permission_handler, *, external_session_id: str | None = None
) -> AdapterOpenRequest:
    profile = HarnessProfile(
        id="grok-a", name="Grok", kind=HarnessKind.GROK_ACP, executable="/bin/true"
    )
    return AdapterOpenRequest(
        profile=profile,
        session=HarnessSession(
            engagement_id="project",
            harness_profile_id=profile.id,
            model="grok-test",
            external_session_id=external_session_id,
        ),
        workspace=tmp_path,
        mcp_profiles=(),
        credential_store=CredentialStore(),
        permission_handler=permission_handler,
    )


async def _deny_permission(_request) -> PermissionTicket:
    future = asyncio.get_running_loop().create_future()
    future.set_result(HarnessPermissionDecision(allowed=False, reason="fixture"))
    return PermissionTicket(None, None, future)


def _runtime(tmp_path: Path, adapter: HarnessAdapter, *, native=None):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(
        HarnessProfile(
            id="grok-a",
            name="Grok fixture",
            kind=HarnessKind.GROK_ACP,
            executable="/bin/true",
            default_model="grok-test",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    if native is not None:
        profile = store.update(
            HarnessProfile,
            profile.id,
            {"native_capabilities": native},
            expected_revision=profile.revision,
        )
    service = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    return store, engagement, profile, service


def _chat_turn(service, engagement, profile, prompt, chat_id=None, session_id=None):
    return service.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt=prompt,
        chat_session_id=chat_id,
        harness_session_id=session_id,
        mcp_server_ids=[],
    )


def _final_text(store: NebulaStore, owner_id: str) -> str | None:
    owner = store.get(ChatTurn, owner_id)
    if not owner.final_message_id:
        return None
    return store.get(ChatMessage, owner.final_message_id).content


async def _wait_status(store, turn_id, statuses, limit: float = 5.0) -> None:
    for _ in range(int(limit / 0.01)):
        if store.get(HarnessTurn, turn_id).status in statuses:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(store.get(HarnessTurn, turn_id).status)


async def _stop(service, task, turn_id: str) -> None:
    await service.cancel_turn(turn_id, reason="Operator stop")
    try:
        await task
    except asyncio.CancelledError:
        # The chat producer is the task Stop cancels.
        pass


class MemoryRpc:
    """In-memory ACP transport: each session/prompt queues its updates, then answers."""

    def __init__(self, *turns: tuple[list[dict[str, Any]], dict[str, Any]]) -> None:
        self.turns = list(turns)
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method != "session/prompt":
            return {}
        updates, result = self.turns.pop(0)
        for update in updates:
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {"sessionId": "sess-1", "update": update},
                }
            )
        await asyncio.sleep(0.01)
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append((method, params or {}))

    async def close(self) -> None:
        return None


class MemoryGrokAdapter(HarnessAdapter):
    kind = HarnessKind.GROK_ACP

    def __init__(self, rpc: MemoryRpc) -> None:
        self.rpc = rpc

    async def probe(self, profile, credential_store):  # pragma: no cover
        raise NotImplementedError

    async def open(self, request: AdapterOpenRequest) -> GrokAcpConnection:
        return GrokAcpConnection(
            self.rpc,  # type: ignore[arg-type]
            external_session_id="sess-1",
            permission_handler=request.permission_handler,
        )


async def _turn(connection: GrokAcpConnection, prompt: str = "go") -> list[Any]:
    return [event async for event in connection.run_turn(prompt, model="grok-test")]


def _commentary(events: list[Any]) -> list[str]:
    items: dict[str, str] = {}
    for event in events:
        if event.type == "output_delta" and event.stream == "commentary":
            items[event.item_id] = items.get(event.item_id, "") + event.delta
    return list(items.values())


# HARN-5: ACP makes the cancelled prompt's response the turn boundary. Grok may
# keep streaming after session/cancel until it answers the prompt.
LATE_UPDATES_AFTER_CANCEL = {
    "prompts": [
        [
            {"update": _chunk("turn one starts")},
            {"wait_cancel": True},
            {"sleep": 0.3},
            {"update": _chunk("LATE TEXT FROM CANCELLED TURN ")},
            {"stop": "cancelled"},
        ],
        [{"update": _chunk("SECOND ANSWER")}, {"stop": "end_turn"}],
    ]
}


def _assert_second_prompt_followed_the_cancelled_answer(
    adapter: ScriptedGrokAdapter,
) -> None:
    cancelled_reply = adapter.index(
        lambda entry: (
            entry.get("out", {}).get("result", {}).get("stopReason") == "cancelled"
        )
    )
    prompts = [
        index
        for index, entry in enumerate(adapter.log())
        if _inbound("session/prompt")(entry)
    ]
    assert len(prompts) == 2
    assert adapter.index(_inbound("session/cancel")) < cancelled_reply
    assert cancelled_reply < prompts[1]


def test_next_prompt_waits_for_the_stopped_prompt_and_drops_its_late_updates(
    tmp_path,
):
    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(LATE_UPDATES_AFTER_CANCEL, tmp_path / "agent")
        store, engagement, profile, service = _runtime(tmp_path, adapter)
        chat, _owner, first = _chat_turn(service, engagement, profile, "first")
        task = service.start_chat_turn(first.id)
        await adapter.wait_for_log(
            lambda entry: "turn one starts" in json.dumps(entry.get("out", {}))
        )
        await _stop(service, task, first.id)

        _chat, owner, second = _chat_turn(
            service,
            engagement,
            profile,
            "second",
            chat_id=chat.id,
            session_id=store.get(HarnessTurn, first.id).harness_session_id,
        )
        await service.start_chat_turn(second.id)

        assert store.get(HarnessTurn, second.id).status == HarnessTurnStatus.COMPLETE
        assert _final_text(store, owner.id) == "SECOND ANSWER"
        _assert_second_prompt_followed_the_cancelled_answer(adapter)
        await service.shutdown()

    asyncio.run(scenario())


def test_stop_that_abandons_the_turn_at_a_yield_does_not_block_the_next_turn(
    tmp_path,
):
    # Stop can land while the activity coalescer holds a prefetched event, which
    # leaves the turn generator suspended at a ``yield`` with its finally unrun.
    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(LATE_UPDATES_AFTER_CANCEL, tmp_path / "agent")
        connection = await adapter.open(_open_request(tmp_path, _deny_permission))
        abandoned = connection.run_turn("first", model="grok-test")
        assert (await abandoned.__anext__()).type == "started"
        await adapter.wait_for_log(
            lambda entry: "turn one starts" in json.dumps(entry.get("out", {}))
        )
        await connection.interrupt()

        second = await asyncio.wait_for(_turn(connection, "second"), 10)
        await abandoned.aclose()
        await connection.close()

        assert second[-1].type == "completed"
        assert second[-1].message == "SECOND ANSWER"
        _assert_second_prompt_followed_the_cancelled_answer(adapter)

    asyncio.run(scenario())


# HARN-6: "The Client MUST respond to all pending session/request_permission
# requests with the cancelled outcome."
def test_stop_answers_a_pending_permission_request_with_cancelled(tmp_path):
    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(
            {
                "prompts": [
                    [
                        {"update": _chunk("Need approval")},
                        _permission_step(700, timeout=3),
                        {"wait_cancel": True},
                        {"stop": "cancelled"},
                    ],
                    [{"update": _chunk("second ok")}, {"stop": "end_turn"}],
                ]
            },
            tmp_path / "agent",
        )
        store, engagement, profile, service = _runtime(
            tmp_path, adapter, native=HarnessNativeCapabilities(skills=True)
        )
        chat, _owner, first = _chat_turn(service, engagement, profile, "first")
        task = service.start_chat_turn(first.id)
        await _wait_status(store, first.id, {HarnessTurnStatus.WAITING_APPROVAL})
        await _stop(service, task, first.id)

        _chat, owner, second = _chat_turn(
            service,
            engagement,
            profile,
            "second",
            chat_id=chat.id,
            session_id=store.get(HarnessTurn, first.id).harness_session_id,
        )
        await service.start_chat_turn(second.id)

        assert adapter.reply(700) == {"result": {"outcome": {"outcome": "cancelled"}}}
        answers = [entry for entry in adapter.log() if _inbound(None, 700)(entry)]
        assert len(answers) == 1
        assert adapter.index(_inbound(None, 700)) < adapter.index(
            _inbound("session/cancel")
        )
        assert _final_text(store, owner.id) == "second ok"
        await service.shutdown()

    asyncio.run(scenario())


def test_interrupt_answers_the_permission_once_before_session_cancel(tmp_path):
    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(
            {
                "prompts": [
                    [
                        {"update": _chunk("Need approval")},
                        _permission_step(700, timeout=3),
                        {"wait_cancel": True},
                        {"stop": "cancelled"},
                    ]
                ]
            },
            tmp_path / "agent",
        )
        decision = asyncio.get_running_loop().create_future()

        async def operator(_request) -> PermissionTicket:
            return PermissionTicket("approval-1", "call-1", decision)

        connection = await adapter.open(_open_request(tmp_path, operator))
        events: list[Any] = []

        async def consume() -> None:
            async for event in connection.run_turn("go", model="grok-test"):
                events.append(event)

        consumer = asyncio.create_task(consume())
        for _ in range(500):
            if any(event.type == "approval_required" for event in events):
                break
            await asyncio.sleep(0.01)
        await connection.interrupt()
        # The operator's late decision must not become a second answer.
        decision.set_result(HarnessPermissionDecision(allowed=True, reason="late"))
        await asyncio.wait_for(consumer, 5)
        await connection.close()

        assert adapter.reply(700) == {"result": {"outcome": {"outcome": "cancelled"}}}
        assert (
            len([entry for entry in adapter.log() if _inbound(None, 700)(entry)]) == 1
        )
        assert adapter.index(_inbound(None, 700)) < adapter.index(
            _inbound("session/cancel")
        )
        assert events[-1].type == "interrupted"

    asyncio.run(scenario())


# HARN-7: JSON-RPC requires a response to every request.
def test_unknown_agent_requests_get_method_not_found_in_and_between_turns(tmp_path):
    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(
            {
                "prompts": [
                    [
                        {"update": _chunk("Checking ")},
                        {
                            "request": {
                                "method": "fs/read_text_file",
                                "params": {
                                    "sessionId": "sess-1",
                                    "path": "/etc/hostname",
                                },
                            },
                            "id": 900,
                            "timeout": 2,
                        },
                        {
                            "request": {
                                "method": "_x.ai/consent/request",
                                "params": {"sessionId": "sess-1"},
                            },
                            "id": 901,
                            "timeout": 2,
                        },
                        {"update": _chunk("done")},
                        {"stop": "end_turn"},
                        {"sleep": 0.2},
                        {
                            "request": {
                                "method": "terminal/create",
                                "params": {"sessionId": "sess-1", "command": "id"},
                            },
                            "id": 902,
                            "timeout": 3,
                            "wait": False,
                        },
                        {**_permission_step(903, timeout=3), "wait": False},
                    ],
                    [{"update": _chunk("second")}, {"stop": "end_turn"}],
                ]
            },
            tmp_path / "agent",
        )
        connection = await adapter.open(_open_request(tmp_path, _deny_permission))
        first = await asyncio.wait_for(_turn(connection), 10)
        await adapter.wait_for_log(lambda entry: entry.get("out", {}).get("id") == 903)
        second = await asyncio.wait_for(_turn(connection, "again"), 10)
        await adapter.wait_for_log(lambda entry: entry.get("reply_for") == 903)
        await connection.close()

        for request_id in (900, 901, 902):
            reply = adapter.reply(request_id)
            assert reply != "TIMEOUT"
            assert reply["error"]["code"] == -32601
        assert adapter.reply(903) == {"result": {"outcome": {"outcome": "cancelled"}}}
        notices = [event.summary for event in first if event.type == "notice"]
        assert "Unhandled Grok ACP event: fs/read_text_file" in notices
        assert "Unhandled Grok ACP event: _x.ai/consent/request" in notices
        assert first[-1].message == "Checking done"
        assert second[-1].message == "second"

    asyncio.run(scenario())


# HARN-8: max_tokens and max_turn_requests are truncations, not clean answers.
@pytest.mark.parametrize("stop_reason", ["max_tokens", "max_turn_requests"])
def test_truncating_stop_reasons_keep_the_partial_answer_marked_incomplete(
    tmp_path, stop_reason
):
    async def scenario() -> None:
        rpc = MemoryRpc(([_chunk("Partial answer, cut")], {"stopReason": stop_reason}))
        store, engagement, profile, service = _runtime(tmp_path, MemoryGrokAdapter(rpc))
        _chat, owner, turn = _chat_turn(service, engagement, profile, "question")
        await service.start_chat_turn(turn.id)

        saved = _final_text(store, owner.id)
        assert saved is not None
        assert saved.startswith("Partial answer, cut\n\n")
        assert "incomplete" in saved and stop_reason in saved
        completed = next(
            event
            for event in service.activity_events(turn.id).events
            if event.type == "completed"
        )
        assert completed.payload["stop_reason"] == stop_reason
        assert completed.payload["truncated"] is True
        await service.shutdown()

    asyncio.run(scenario())


# HARN-8: the refused prompt is dropped from Grok's history, so it is no answer.
def test_refusal_is_an_error_and_not_a_verified_completion(tmp_path):
    async def scenario() -> None:
        connection = GrokAcpConnection(
            MemoryRpc(([_chunk("I can't help")], {"stopReason": "refusal"})),  # type: ignore[arg-type]
            external_session_id="sess-1",
            permission_handler=_deny_permission,
        )
        events = await _turn(connection)
        assert events[-1].type == "error"
        assert events[-1].reason_code == "refused"
        assert events[-1].payload["stop_reason"] == "refusal"
        assert not any(event.type == "completed" for event in events)

        rpc = MemoryRpc(([_chunk("I can't help")], {"stopReason": "refusal"}))
        store, engagement, profile, service = _runtime(tmp_path, MemoryGrokAdapter(rpc))
        _chat, owner, turn = _chat_turn(service, engagement, profile, "question")
        await service.start_chat_turn(turn.id)

        saved = store.get(HarnessTurn, turn.id)
        assert saved.status == HarnessTurnStatus.INTERRUPTED
        assert "refus" in (saved.error or "")
        assert _final_text(store, owner.id) is None
        assert store.get(HarnessProfile, profile.id).capabilities.turn_state != (
            "verified"
        )
        await service.shutdown()

    asyncio.run(scenario())


# HARN-11: a one-time operator decision never becomes a standing vendor rule.
ALLOW_ALWAYS = {
    "optionId": "allow_always",
    "name": "Always Allow",
    "kind": "allow_always",
}
ALLOW_ONCE = {"optionId": "allow", "name": "Allow", "kind": "allow_once"}
REJECT_ALWAYS = {
    "optionId": "reject_always",
    "name": "Always Reject",
    "kind": "reject_always",
}
REJECT_ONCE = {"optionId": "reject", "name": "Reject", "kind": "reject_once"}


@pytest.mark.parametrize(
    "allowed,options,outcome",
    [
        (
            True,
            [ALLOW_ALWAYS, ALLOW_ONCE, REJECT_ALWAYS, REJECT_ONCE],
            {"outcome": "selected", "optionId": "allow"},
        ),
        (
            False,
            [ALLOW_ALWAYS, ALLOW_ONCE, REJECT_ALWAYS, REJECT_ONCE],
            {"outcome": "selected", "optionId": "reject"},
        ),
        (
            True,
            [ALLOW_ALWAYS, REJECT_ONCE],
            {"outcome": "selected", "optionId": "reject"},
        ),
        (False, [ALLOW_ONCE, REJECT_ALWAYS], {"outcome": "cancelled"}),
        (True, [ALLOW_ALWAYS, REJECT_ALWAYS], {"outcome": "cancelled"}),
        (
            True,
            [
                {"optionId": "allow-always", "name": "Always"},
                {"optionId": "allow-once", "name": "Once"},
            ],
            {"outcome": "selected", "optionId": "allow-once"},
        ),
    ],
)
def test_permission_answers_pick_the_once_option_by_exact_kind(
    allowed, options, outcome
):
    async def scenario() -> None:
        writes: list[tuple[Any, dict[str, Any]]] = []

        async def respond(request_id, result):
            writes.append((request_id, result))

        async def operator(_request) -> PermissionTicket:
            future = asyncio.get_running_loop().create_future()
            future.set_result(HarnessPermissionDecision(allowed=allowed, reason="x"))
            return PermissionTicket(None, None, future)

        connection = GrokAcpConnection(
            SimpleNamespace(respond=respond),  # type: ignore[arg-type]
            external_session_id="sess-1",
            permission_handler=operator,
        )
        _ = [
            event
            async for event in connection._permission(
                {"id": 55},
                {"toolCall": {"title": "run_terminal_command"}, "options": options},
            )
        ]
        assert writes == [(55, {"outcome": outcome})]

    asyncio.run(scenario())


# HARN-13: non-text content blocks are rendered instead of dropped.
def test_links_resources_and_media_reach_the_answer():
    async def scenario() -> None:
        updates = [
            _chunk("Report written to "),
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {
                    "type": "resource_link",
                    "uri": "file:///work/report.md",
                    "name": "report.md",
                },
            },
            _chunk(". Diagram: "),
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "image", "mimeType": "image/png", "data": "iVBO"},
            },
            _chunk(" Notes: "),
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///work/notes.txt",
                        "mimeType": "text/plain",
                        "text": "EMBEDDED NOTES",
                    },
                },
            },
            _chunk(" "),
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "audio", "mimeType": "audio/wav", "data": "UklG"},
            },
        ]
        connection = GrokAcpConnection(
            MemoryRpc((updates, {"stopReason": "end_turn"})),  # type: ignore[arg-type]
            external_session_id="sess-1",
            permission_handler=_deny_permission,
        )
        events = await _turn(connection)
        assert events[-1].message == (
            "Report written to [report.md](file:///work/report.md). Diagram: "
            "[image: image/png] Notes: EMBEDDED NOTES [audio: audio/wav]"
        )

    asyncio.run(scenario())


# HARN-14: agent messages are grouped by ACP messageId; replays are dropped.
@pytest.mark.parametrize(
    "updates,answer,commentary",
    [
        pytest.param(
            [
                _chunk("Skeptic: verified the change.", messageId="m1"),
                _chunk("Tests ", messageId="m2"),
                _chunk("pass.", messageId="m2"),
                _chunk("Tests pass.", messageId="m3"),
            ],
            "Tests pass.",
            ["Skeptic: verified the change."],
            id="narration-then-replayed-answer",
        ),
        pytest.param(
            [_chunk("Tests "), _chunk("pass."), _chunk("Tests "), _chunk("pass.")],
            "Tests pass.",
            [],
            id="replay-split-into-chunks",
        ),
        pytest.param(
            [_chunk("Tests "), _chunk("pass.\n"), _chunk("Tests pass.")],
            "Tests pass.\n",
            [],
            id="replay-differs-in-whitespace",
        ),
        pytest.param(
            [
                _chunk("First, I checked the config.", messageId="m1"),
                {
                    "sessionUpdate": "plan",
                    "entries": [{"content": "Check", "status": "completed"}],
                },
                _chunk("The config is valid.", messageId="m2"),
            ],
            "The config is valid.",
            ["First, I checked the config."],
            id="separate-messages",
        ),
        pytest.param(
            [
                _chunk("I ran the suite. Tests pass.", messageId="m1"),
                _chunk("Tests  pass.", messageId="m2"),
            ],
            "I ran the suite. Tests pass.",
            [],
            id="replay-is-a-suffix",
        ),
    ],
)
def test_final_answer_is_the_last_distinct_agent_message(updates, answer, commentary):
    async def scenario() -> None:
        connection = GrokAcpConnection(
            MemoryRpc((updates, {"stopReason": "end_turn"})),  # type: ignore[arg-type]
            external_session_id="sess-1",
            permission_handler=_deny_permission,
        )
        events = await _turn(connection)
        streamed = "".join(
            event.delta for event in events if event.type == "message_delta"
        )
        assert events[-1].type == "completed"
        assert events[-1].message == answer
        assert streamed == answer
        assert _commentary(events) == commentary

    asyncio.run(scenario())


# HARN-15: usage_update is session context and cumulative cost, not token deltas.
def test_usage_update_becomes_detailed_usage_with_a_per_turn_cost(tmp_path):
    def usage(used: int, amount: float) -> dict[str, Any]:
        return {
            "update": {
                "sessionUpdate": "usage_update",
                "used": used,
                "size": 200_000,
                "cost": {"amount": amount, "currency": "USD"},
            }
        }

    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(
            {
                "prompts": [
                    [
                        {"update": _chunk("answer")},
                        usage(12_345, 0.02),
                        {"stop": "end_turn"},
                    ],
                    [
                        {"update": _chunk("again")},
                        usage(13_000, 0.05),
                        {"stop": "end_turn"},
                    ],
                ]
            },
            tmp_path / "new",
        )
        connection = await adapter.open(_open_request(tmp_path, _deny_permission))
        first = await asyncio.wait_for(_turn(connection), 10)
        second = await asyncio.wait_for(_turn(connection, "again"), 10)
        await connection.close()

        assert not any(
            event.type == "notice" and "usage_update" in (event.summary or "")
            for event in first + second
        )
        reported = [event for event in first + second if event.type == "usage"]
        assert len(reported) == 2
        assert all(event.usage is None for event in reported)
        detail = reported[0].detailed_usage
        assert detail is not None
        assert (detail.context_used, detail.context_window) == (12_345, 200_000)
        assert detail.total_tokens == 0
        assert detail.cost_usd == pytest.approx(0.02)
        assert reported[1].detailed_usage.cost_usd == pytest.approx(0.03)
        assert reported[1].payload["session_cost"] == {
            "amount": 0.05,
            "currency": "USD",
        }

        resumed = ScriptedGrokAdapter(
            {"prompts": [[usage(40_000, 0.07), {"stop": "end_turn"}]]},
            tmp_path / "loaded",
        )
        connection = await resumed.open(
            _open_request(tmp_path, _deny_permission, external_session_id="sess-1")
        )
        events = await asyncio.wait_for(_turn(connection), 10)
        await connection.close()
        loaded = next(event for event in events if event.type == "usage")
        # A loaded session's earlier cost is unknown, so no per-turn cost is invented.
        assert loaded.detailed_usage.cost_usd is None
        assert loaded.detailed_usage.context_used == 40_000
        assert loaded.payload["session_cost"] == {"amount": 0.07, "currency": "USD"}

    asyncio.run(scenario())


# HARN-9 (first part): session/load is only sent to an agent that advertises it.
def test_resume_requires_the_agent_to_advertise_load_session(tmp_path):
    async def scenario() -> None:
        adapter = ScriptedGrokAdapter(
            {"prompts": [], "load_session": False}, tmp_path / "agent"
        )
        with pytest.raises(HarnessConfigurationError, match="loadSession"):
            await adapter.open(
                _open_request(
                    tmp_path, _deny_permission, external_session_id="sess-gone"
                )
            )
        assert not any(_inbound("session/load")(entry) for entry in adapter.log())

    asyncio.run(scenario())
