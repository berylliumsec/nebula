"""A chat whose saved vendor session can no longer be resumed carries on.

Grok, Claude and Codex each keep their own transcript. When that transcript is
gone (pruned, deleted, or the runtime cannot load sessions at all), resuming
fails the same way on every later turn. Nebula owns the conversation, so it
starts a fresh vendor session and hands the saved conversation over, exactly
once, and only when the vendor clearly said the session does not exist.

Each scenario runs a real ``HarnessRuntimeService`` and the real vendor adapter:
Grok against a scripted ACP agent subprocess, Claude through the real
``ClaudeSDKClient`` over a scripted CLI transport, and Codex over a scripted
app-server connection. The first turn creates the vendor session, then Core
"restarts" (a second service over the same store) so the next turn must resume.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import claude_agent_sdk
import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    Engagement,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTurn,
    HarnessTurnStatus,
)
from nebula.v3.harnesses import (
    ClaudeAgentSdkAdapter,
    CodexAppServerAdapter,
    GrokAcpAdapter,
    HarnessAdapter,
    HarnessProviderError,
    HarnessRuntimeService,
    _AcpRpc,
)
from nebula.v3.storage import NebulaStore

TURN_DEADLINE_SECONDS = 20.0
RECREATED = "vendor_session_recreated"


class Conversation:
    """One Nebula chat on a real runtime service; ``restart`` mimics a Core restart."""

    def __init__(self, tmp_path: Path, kind: HarnessKind, adapter: HarnessAdapter):
        self.tmp_path = tmp_path
        self.adapter = adapter
        self.store = NebulaStore(tmp_path / "nebula.db")
        self.engagement = self.store.create(Engagement(id="eng-a", name="Engagement A"))
        self.profile = self.store.create(
            HarnessProfile(
                id="harness-a",
                name="Harness fixture",
                kind=kind,
                executable="/bin/true",
                default_model="model-a",
                privacy={"local_only": True, "permits_sensitive_data": True},
            )
        )
        self.service = self._service()
        self.chat_id: str | None = None

    def _service(self) -> HarnessRuntimeService:
        return HarnessRuntimeService(
            self.store,
            credential_store=CredentialStore(),
            workspace_resolver=lambda _: self.tmp_path,
            adapter_factory=lambda _: self.adapter,
        )

    async def restart(self) -> None:
        await self.service.shutdown()
        self.service = self._service()

    async def send(self, prompt: str) -> tuple[HarnessTurn, str | None]:
        chat, owner, turn = self.service.prepare_chat(
            engagement_id=self.engagement.id,
            profile_id=self.profile.id,
            model=None,
            prompt=prompt,
            chat_session_id=self.chat_id,
            harness_session_id=None,
            mcp_server_ids=[],
        )
        self.chat_id = chat.id
        await asyncio.wait_for(
            self.service.start_chat_turn(turn.id), timeout=TURN_DEADLINE_SECONDS
        )
        owner = self.store.get(ChatTurn, owner.id)
        answer = (
            self.store.get(ChatMessage, owner.final_message_id).content
            if owner.final_message_id
            else None
        )
        return self.store.get(HarnessTurn, turn.id), answer

    def chat(self) -> ChatSession:
        assert self.chat_id is not None
        return self.store.get(ChatSession, self.chat_id)

    def session(self, session_id: str) -> HarnessSession:
        return self.store.get(HarnessSession, session_id)

    def statuses(self, turn_id: str) -> list[dict[str, Any]]:
        return [
            event.payload
            for event in self.service.activity_events(turn_id).events
            if event.type == "status"
        ]


def assert_recreated(
    conversation: Conversation, turn: HarnessTurn, previous_session_id: str
) -> HarnessSession:
    """The turn ran on a fresh Nebula session forked for the lost vendor session."""

    assert turn.status == HarnessTurnStatus.COMPLETE, turn.error
    assert turn.harness_session_id != previous_session_id
    replacement = conversation.session(turn.harness_session_id)
    assert replacement.metadata["forked_from_session_id"] == previous_session_id
    assert replacement.metadata["fork_reason"] == "vendor_session_unavailable"
    assert turn.metadata["session_rollover_reason"] == "vendor_session_unavailable"
    chat = conversation.chat()
    assert chat.harness_session_id == replacement.id
    assert chat.metadata["harness_session_rollovers"][-1] == {
        **chat.metadata["harness_session_rollovers"][-1],
        "from_session_id": previous_session_id,
        "to_session_id": replacement.id,
        "reason": "vendor_session_unavailable",
    }
    recreated = [
        status
        for status in conversation.statuses(turn.id)
        if status.get("phase") == RECREATED
    ]
    assert len(recreated) == 1
    assert recreated[0]["previous_session_id"] == previous_session_id
    assert recreated[0]["detail"]
    # The handoff carries the earlier exchange, not the prompt being answered.
    assert "user: FIRST QUESTION" in turn.prompt
    assert "assistant: FIRST ANSWER" in turn.prompt
    assert "user: SECOND QUESTION" not in turn.prompt
    return replacement


def assert_not_recreated(
    conversation: Conversation, turn: HarnessTurn, session_id: str, external_id: str
) -> None:
    assert turn.status == HarnessTurnStatus.INTERRUPTED
    assert turn.harness_session_id == session_id
    assert conversation.chat().harness_session_id == session_id
    assert conversation.session(session_id).external_session_id == external_id
    assert not any(
        status.get("phase") == RECREATED for status in conversation.statuses(turn.id)
    )


# --- Grok: the real adapter against a scripted ACP agent -------------------

AGENT_SOURCE = r"""
import asyncio
import json
import sys

script = json.load(open(sys.argv[1]))
log = open(sys.argv[2], "a", buffering=1)
prompts = list(script["prompts"])


async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=10_000_000)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )

    def send(value):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **value}) + "\n")
        sys.stdout.flush()

    log.write(json.dumps({"spawned": True}) + "\n")
    while line := await reader.readline():
        message = json.loads(line)
        log.write(json.dumps({"in": message}) + "\n")
        method = message.get("method")
        if method is None:
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
            send({"id": message["id"], "result": {"sessionId": script["session_id"]}})
        elif method == "session/load":
            if script.get("load_error"):
                send({"id": message["id"], "error": script["load_error"]})
            else:
                send({"id": message["id"], "result": {}})
        elif method == "session/prompt":
            text = prompts.pop(0) if prompts else ""
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": message["params"]["sessionId"],
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            )
            send({"id": message["id"], "result": {"stopReason": "end_turn"}})
        elif "id" in message:
            send({"id": message["id"], "result": {}})


asyncio.run(main())
"""


class ScriptedGrokAdapter(GrokAcpAdapter):
    """The real Grok adapter; only the ``grok agent stdio`` process is scripted."""

    def __init__(self, workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.agent_path = workdir / "scripted_acp_agent.py"
        self.agent_path.write_text(AGENT_SOURCE)
        self.script_path = workdir / "agent_script.json"
        self.log_path = workdir / "agent_log.jsonl"

    def script(self, **script: Any) -> None:
        self.script_path.write_text(json.dumps(script))
        self.log_path.write_text("")

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

    def inbound(self, method: str) -> list[dict[str, Any]]:
        frames = [
            json.loads(line).get("in")
            for line in self.log_path.read_text().splitlines()
            if line.strip()
        ]
        return [
            frame
            for frame in frames
            if isinstance(frame, dict) and frame.get("method") == method
        ]

    def spawned(self) -> int:
        return sum(
            1
            for line in self.log_path.read_text().splitlines()
            if json.loads(line).get("spawned")
        )


async def _grok_first_turn(tmp_path: Path) -> tuple[Conversation, ScriptedGrokAdapter]:
    adapter = ScriptedGrokAdapter(tmp_path / "agent")
    adapter.script(session_id="sess-1", prompts=["FIRST ANSWER"])
    conversation = Conversation(tmp_path, HarnessKind.GROK_ACP, adapter)
    turn, answer = await conversation.send("FIRST QUESTION")
    assert turn.status == HarnessTurnStatus.COMPLETE, turn.error
    assert answer == "FIRST ANSWER"
    assert conversation.session(turn.harness_session_id).external_session_id == "sess-1"
    await conversation.restart()
    return conversation, adapter


GROK_FS_NOT_FOUND = {
    # grok 1.0.40 answers session/load for a session it no longer has this way.
    "code": -32603,
    "message": "Path not found.",
    "data": {
        "detail": "No such file or directory (os error 2)",
        "code": "FS_NOT_FOUND",
    },
}
ACP_RESOURCE_NOT_FOUND = {"code": -32002, "message": "Resource not found: session"}


@pytest.mark.parametrize(
    "refusal",
    [
        {"load_error": GROK_FS_NOT_FOUND},
        {"load_error": ACP_RESOURCE_NOT_FOUND},
        {"load_session": False},
    ],
    ids=["grok-fs-not-found", "acp-resource-not-found", "load-session-unsupported"],
)
def test_grok_session_that_cannot_be_loaded_continues_on_a_fresh_session(
    tmp_path: Path, refusal: dict[str, Any]
) -> None:
    async def scenario() -> None:
        conversation, adapter = await _grok_first_turn(tmp_path)
        first_session_id = conversation.chat().harness_session_id
        assert first_session_id is not None
        adapter.script(
            session_id="sess-2", prompts=["SECOND ANSWER", "THIRD ANSWER"], **refusal
        )

        turn, answer = await conversation.send("SECOND QUESTION")

        replacement = assert_recreated(conversation, turn, first_session_id)
        assert answer == "SECOND ANSWER"
        assert replacement.external_session_id == "sess-2"
        # One refused resume, then one fresh session; never a loop.
        assert adapter.spawned() == 2
        loads = adapter.inbound("session/load")
        assert [frame["params"]["sessionId"] for frame in loads] == (
            ["sess-1"] if "load_error" in refusal else []
        )
        assert len(adapter.inbound("session/new")) == 1
        [prompt] = adapter.inbound("session/prompt")
        sent = json.dumps(prompt["params"]["prompt"])
        assert "FIRST ANSWER" in sent and "SECOND QUESTION" in sent

        # The chat stays on the fresh session for the next turn.
        turn, answer = await conversation.send("THIRD QUESTION")
        assert turn.status == HarnessTurnStatus.COMPLETE, turn.error
        assert answer == "THIRD ANSWER"
        assert turn.harness_session_id == replacement.id
        await conversation.service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "load_error",
    [
        {"code": -32000, "message": "Authentication required"},
        {"code": -32603, "message": "Internal error", "data": {"code": "IO_ERROR"}},
    ],
    ids=["auth-required", "internal-error"],
)
def test_grok_load_failures_other_than_not_found_keep_the_saved_session(
    tmp_path: Path, load_error: dict[str, Any]
) -> None:
    async def scenario() -> None:
        conversation, adapter = await _grok_first_turn(tmp_path)
        first_session_id = conversation.chat().harness_session_id
        assert first_session_id is not None
        adapter.script(session_id="sess-2", prompts=["SECOND"], load_error=load_error)

        turn, answer = await conversation.send("SECOND QUESTION")

        assert answer is None
        assert_not_recreated(conversation, turn, first_session_id, "sess-1")
        assert adapter.spawned() == 1
        assert adapter.inbound("session/new") == []
        await conversation.service.shutdown()

    asyncio.run(scenario())


# --- Claude: the real adapter and SDK client over a scripted CLI -----------

NO_CONVERSATION = "No conversation found with session ID: claude-session-1"


def _claude_result(session_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": session_id,
        "result": "ok",
        "usage": {"input_tokens": 3, "output_tokens": 2},
        **fields,
    }


def _claude_answer(session_id: str, text: str) -> list[dict[str, Any]]:
    def stream(event: dict[str, Any], suffix: str) -> dict[str, Any]:
        return {
            "type": "stream_event",
            "uuid": f"se-{session_id}-{suffix}",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "event": event,
        }

    return [
        stream(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            "start",
        ),
        stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            "delta",
        ),
        stream({"type": "content_block_stop", "index": 0}, "stop"),
        {
            "type": "assistant",
            "uuid": f"asst-{session_id}",
            "session_id": session_id,
            "parent_tool_use_id": None,
            "message": {
                "model": "claude-test",
                "content": [{"type": "text", "text": text}],
                "usage": None,
            },
        },
        _claude_result(session_id, result=text),
    ]


class ScriptedClaudeCli(claude_agent_sdk.Transport):
    """The Claude Code CLI's stdio contract for one process.

    ``refusal`` makes the process behave like ``claude --resume <gone id>``
    (checked against Claude Code 2.1.209 and 2.1.277): it prints the reason on
    stderr, emits it as an error ``result`` and exits 1 without answering
    ``initialize``.
    """

    def __init__(
        self,
        *,
        session_id: str,
        answer: str,
        refusal: str | None = None,
        stderr: Callable[[str], None] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.answer = answer
        self.refusal = refusal
        self.stderr = stderr
        self.mcp_servers = list(mcp_servers or [])
        self.prompts: list[str] = []
        self._out: asyncio.Queue[Any] = asyncio.Queue()
        self._ready = False

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        if not self._ready:
            raise claude_agent_sdk.CLIConnectionError(
                "Cannot write to terminated process (exit code: 1)"
            )
        message = json.loads(data)
        if message.get("type") == "control_request":
            if self.refusal is not None:
                self._ready = False
                if self.stderr is not None:
                    self.stderr(self.refusal)
                await self._out.put(
                    _claude_result(
                        self.session_id,
                        subtype="error_during_execution",
                        is_error=True,
                        num_turns=0,
                        result=None,
                        errors=[self.refusal],
                    )
                )
                await self._out.put(("EXIT", 1))
                return
            response: dict[str, Any] = {}
            if message["request"].get("subtype") == "mcp_status":
                # Nebula waits for its required gateway server before a turn.
                response = {
                    "mcpServers": [
                        {"name": name, "status": "connected"}
                        for name in self.mcp_servers
                    ]
                }
            await self._out.put(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message["request_id"],
                        "response": response,
                    },
                }
            )
            return
        if message.get("type") == "user":
            self.prompts.append(str(message["message"]["content"]))
            for item in _claude_answer(self.session_id, self.answer):
                await self._out.put(item)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._out.get()
            if isinstance(item, tuple) and item[0] == "EXIT":
                raise claude_agent_sdk.ProcessError(
                    f"Command failed with exit code {item[1]}",
                    exit_code=item[1],
                    stderr="Check stderr output for details",
                )
            yield item

    async def close(self) -> None:
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None


class ClaudeCliPlan:
    """Decides what each spawned CLI does, from the options the adapter built."""

    def __init__(self) -> None:
        self.refusal: str | None = None
        self.answers = ["FIRST ANSWER"]
        self.session_ids = ["claude-session-1"]
        self.spawned: list[tuple[str | None, ScriptedClaudeCli]] = []

    def client(self, *, options: Any) -> Any:
        resume = options.resume
        refusal = self.refusal if resume else None
        cli = ScriptedClaudeCli(
            session_id=resume if refusal else self.session_ids.pop(0),
            answer="" if refusal else self.answers.pop(0),
            refusal=refusal,
            stderr=options.stderr,
            mcp_servers=list(options.mcp_servers or {}),
        )
        self.spawned.append((resume, cli))
        return claude_agent_sdk.ClaudeSDKClient(options=options, transport=cli)


def _claude_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Conversation, ClaudeCliPlan]:
    plan = ClaudeCliPlan()
    sdk = SimpleNamespace(
        **{
            name: getattr(claude_agent_sdk, name)
            for name in dir(claude_agent_sdk)
            if not name.startswith("_")
        },
        __version__=claude_agent_sdk.__version__,
    )
    sdk.ClaudeSDKClient = plan.client
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(lambda: sdk))
    conversation = Conversation(
        tmp_path, HarnessKind.CLAUDE_AGENT_SDK, ClaudeAgentSdkAdapter()
    )
    return conversation, plan


async def _claude_first_turn(conversation: Conversation) -> str:
    turn, answer = await conversation.send("FIRST QUESTION")
    assert turn.status == HarnessTurnStatus.COMPLETE, turn.error
    assert answer == "FIRST ANSWER"
    session = conversation.session(turn.harness_session_id)
    assert session.external_session_id == "claude-session-1"
    await conversation.restart()
    return session.id


def test_claude_conversation_the_cli_cannot_resume_continues_on_a_fresh_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        conversation, plan = _claude_conversation(tmp_path, monkeypatch)
        first_session_id = await _claude_first_turn(conversation)
        plan.refusal = NO_CONVERSATION
        plan.answers.append("SECOND ANSWER")
        plan.session_ids.append("claude-session-2")

        turn, answer = await conversation.send("SECOND QUESTION")

        replacement = assert_recreated(conversation, turn, first_session_id)
        assert answer == "SECOND ANSWER"
        assert replacement.external_session_id == "claude-session-2"
        assert [resume for resume, _cli in plan.spawned] == [
            None,
            "claude-session-1",
            None,
        ]
        fresh = plan.spawned[-1][1]
        assert len(fresh.prompts) == 1
        assert "FIRST ANSWER" in fresh.prompts[0]
        assert "SECOND QUESTION" in fresh.prompts[0]
        await conversation.service.shutdown()

    asyncio.run(scenario())


def test_claude_cli_failures_other_than_a_missing_conversation_keep_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        conversation, plan = _claude_conversation(tmp_path, monkeypatch)
        first_session_id = await _claude_first_turn(conversation)
        plan.refusal = "Invalid API key · Please run /login"

        turn, answer = await conversation.send("SECOND QUESTION")

        assert answer is None
        assert_not_recreated(conversation, turn, first_session_id, "claude-session-1")
        assert [resume for resume, _cli in plan.spawned] == [None, "claude-session-1"]
        await conversation.service.shutdown()

    asyncio.run(scenario())


# --- Codex: the real adapter over a scripted app-server connection ---------


class ScriptedCodexRpc:
    """The Codex app-server requests the adapter and connection make."""

    def __init__(self, plan: CodexPlan) -> None:
        self.plan = plan
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.running_turns: dict[str, str] = {}
        self.connection_state = "connected"

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "initialize":
            return {"userAgent": "codex-cli/0.154.0"}
        if method == "thread/resume":
            if self.plan.resume_error is not None:
                raise HarnessProviderError("Codex app-server", self.plan.resume_error)
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/start":
            if self.plan.start_error is not None:
                raise HarnessProviderError("Codex app-server", self.plan.start_error)
            return {"thread": {"id": self.plan.thread_ids.pop(0)}}
        if method == "turn/start":
            thread_id = params["threadId"]
            text = self.plan.answers.pop(0)
            item = {"id": "msg-1", "type": "agentMessage", "text": text}
            for method_name, payload in (
                ("item/started", {"item": {**item, "text": ""}}),
                ("item/agentMessage/delta", {"itemId": "msg-1", "delta": text}),
                ("item/completed", {"item": item}),
                (
                    "turn/completed",
                    {
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "error": None,
                            "items": [],
                            "itemsView": "notLoaded",
                        }
                    },
                ),
            ):
                await self.events.put(
                    {
                        "method": method_name,
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn-1",
                            **payload,
                        },
                    }
                )
            return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
        return {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        self.connection_state = "disconnected"


class CodexPlan:
    def __init__(self) -> None:
        self.resume_error: dict[str, Any] | None = None
        self.start_error: dict[str, Any] | None = None
        self.thread_ids = ["thread-1"]
        self.answers = ["FIRST ANSWER"]
        self.rpcs: list[ScriptedCodexRpc] = []


class ScriptedCodexAdapter(CodexAppServerAdapter):
    def __init__(self, plan: CodexPlan) -> None:
        self.plan = plan

    async def _connect(self, *_args: Any, **_kwargs: Any) -> Any:
        rpc = ScriptedCodexRpc(self.plan)
        self.plan.rpcs.append(rpc)
        return rpc


async def _codex_first_turn(tmp_path: Path) -> tuple[Conversation, CodexPlan, str]:
    plan = CodexPlan()
    conversation = Conversation(
        tmp_path, HarnessKind.CODEX_APP_SERVER, ScriptedCodexAdapter(plan)
    )
    turn, answer = await conversation.send("FIRST QUESTION")
    assert turn.status == HarnessTurnStatus.COMPLETE, turn.error
    assert answer == "FIRST ANSWER"
    assert conversation.session(turn.harness_session_id).external_session_id == (
        "thread-1"
    )
    await conversation.restart()
    return conversation, plan, turn.harness_session_id


def _thread_methods(plan: CodexPlan) -> list[str]:
    return [
        method
        for rpc in plan.rpcs
        for method, _params in rpc.calls
        if method.startswith("thread/")
    ]


# What codex-cli 0.154.0 answers thread/resume for a thread it has no rollout for.
NO_ROLLOUT = {"code": -32600, "message": "no rollout found for thread id thread-1"}


def test_codex_thread_without_a_rollout_continues_on_a_fresh_thread(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        conversation, plan, first_session_id = await _codex_first_turn(tmp_path)
        plan.resume_error = NO_ROLLOUT
        plan.thread_ids.append("thread-2")
        plan.answers.append("SECOND ANSWER")

        turn, answer = await conversation.send("SECOND QUESTION")

        replacement = assert_recreated(conversation, turn, first_session_id)
        assert answer == "SECOND ANSWER"
        assert replacement.external_session_id == "thread-2"
        assert _thread_methods(plan) == [
            "thread/start",
            "thread/resume",
            "thread/start",
        ]
        await conversation.service.shutdown()

    asyncio.run(scenario())


def test_codex_recreates_the_thread_once_even_when_the_fresh_thread_fails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        conversation, plan, first_session_id = await _codex_first_turn(tmp_path)
        plan.resume_error = NO_ROLLOUT
        plan.start_error = {"code": -32603, "message": "model provider unavailable"}

        turn, answer = await conversation.send("SECOND QUESTION")

        assert answer is None
        assert turn.status == HarnessTurnStatus.INTERRUPTED
        assert "model provider unavailable" in (turn.error or "")
        assert turn.harness_session_id != first_session_id
        assert conversation.chat().harness_session_id == turn.harness_session_id
        assert _thread_methods(plan) == [
            "thread/start",
            "thread/resume",
            "thread/start",
        ]
        await conversation.service.shutdown()

    asyncio.run(scenario())


def test_codex_resume_errors_other_than_a_missing_rollout_keep_the_thread(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        conversation, plan, first_session_id = await _codex_first_turn(tmp_path)
        plan.resume_error = {
            "code": -32600,
            "message": "session thread-1 is archived. Run `codex unarchive "
            "thread-1` to unarchive it first.",
        }

        turn, answer = await conversation.send("SECOND QUESTION")

        assert answer is None
        assert_not_recreated(conversation, turn, first_session_id, "thread-1")
        assert _thread_methods(plan) == ["thread/start", "thread/resume"]
        await conversation.service.shutdown()

    asyncio.run(scenario())
