"""Codex app-server transport limits, stderr, error notices and server requests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    HarnessConnectionMode,
    HarnessKind,
    HarnessProfile,
    HarnessSession,
    HarnessTransport,
)
from nebula.v3.harnesses import (
    AdapterOpenRequest,
    CodexAppServerAdapter,
    CodexAppServerConnection,
    HarnessEvent,
    HarnessPermissionDecision,
    HarnessTransportError,
    PermissionTicket,
    _CodexRpc,
)
from nebula.v3.mcp import MAX_MCP_MESSAGE_BYTES

THREAD = "thread-transport"
TURN = "turn-transport"


async def _allow(request: Any) -> PermissionTicket:
    future: asyncio.Future[HarnessPermissionDecision] = (
        asyncio.get_running_loop().create_future()
    )
    future.set_result(HarnessPermissionDecision(allowed=True))
    return PermissionTicket("approval-transport", request.vendor_request_id, future)


def _fake_app_server(tmp_path: Path, body: str) -> HarnessProfile:
    """Spawn profile for a Codex stand-in; ``body`` handles each line from Nebula."""

    executable = tmp_path / "codex-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "def out(value):\n"
        "    sys.stdout.write(json.dumps(value) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n" + textwrap.indent(textwrap.dedent(body), "    "),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return HarnessProfile(
        name="Codex fixture",
        kind=HarnessKind.CODEX_APP_SERVER,
        executable=str(executable),
    )


class _ScriptedRpc:
    """Stand-in for ``_CodexRpc``: ``turn/start`` queues the scripted frames."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.errors: list[tuple[Any, int, str]] = []
        self.running_turns: dict[str, str] = {}
        self.connection_state = "connected"

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "turn/start":
            for frame in self.script:
                await self.events.put(frame)
            return {"turn": {"id": TURN, "status": "inProgress", "items": []}}
        return {}

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self.errors.append((request_id, code, message))

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def close(self) -> None:
        return None


def _run(
    script: list[dict[str, Any]],
) -> tuple[list[HarnessEvent], BaseException | None, _ScriptedRpc]:
    rpc = _ScriptedRpc(script)
    connection = CodexAppServerConnection(
        rpc,  # type: ignore[arg-type]
        external_session_id=THREAD,
        permission_handler=_allow,
    )

    async def scenario() -> tuple[list[HarnessEvent], BaseException | None]:
        events: list[HarnessEvent] = []
        try:
            async for event in connection.run_turn("hello", model="gpt-test"):
                events.append(event)
        except Exception as exc:  # noqa: BLE001 - the tests inspect the outcome
            return events, exc
        return events, None

    events, error = asyncio.run(asyncio.wait_for(scenario(), 10))
    return events, error, rpc


def _notification(method: str, **params: Any) -> dict[str, Any]:
    params.setdefault("threadId", THREAD)
    return {"method": method, "params": params}


def _agent_completed(text: str) -> dict[str, Any]:
    return _notification(
        "item/completed",
        turnId=TURN,
        item={"id": "msg-1", "type": "agentMessage", "text": text},
    )


def _turn_completed(status: str = "completed", error: Any = None) -> dict[str, Any]:
    return _notification(
        "turn/completed",
        turn={"id": TURN, "status": status, "error": error, "items": []},
    )


# CODEX-2: message size limits ------------------------------------------------


def test_codex_stdio_turn_survives_an_image_echo_over_the_mcp_limit(tmp_path):
    # Codex echoes the user's input, image data URL included, in item/started
    # and item/completed for the userMessage item.
    profile = _fake_app_server(
        tmp_path,
        """
        if msg.get("method") != "turn/start":
            continue
        turn = {"id": "turn-1", "status": "inProgress", "items": []}
        out({"id": msg["id"], "result": {"turn": turn}})
        user = {"type": "userMessage", "id": "user-1", "content": msg["params"]["input"]}
        for method in ("item/started", "item/completed"):
            out({"method": method, "params": {"threadId": "th", "turnId": "turn-1", "item": user}})
        answer = {"type": "agentMessage", "id": "answer-1", "text": "A login form."}
        out({"method": "item/completed", "params": {"threadId": "th", "turnId": "turn-1", "item": answer}})
        done = {"id": "turn-1", "status": "completed", "items": []}
        out({"method": "turn/completed", "params": {"threadId": "th", "turn": done}})
        """,
    )
    image = {
        "media_type": "image/png",
        "data": base64.b64encode(os.urandom(4_800_000)).decode(),
    }
    assert len(image["data"]) > 1.5 * MAX_MCP_MESSAGE_BYTES

    async def scenario() -> None:
        rpc = await CodexAppServerAdapter()._connect(
            profile, CredentialStore(), (), tmp_path
        )
        try:
            connection = CodexAppServerConnection(
                rpc, external_session_id="th", permission_handler=_allow
            )
            events = [
                event
                async for event in connection.run_turn(
                    "What is on this screen?", model="gpt-test", images=[image]
                )
            ]
            assert events[-1].type == "completed"
            assert events[-1].message == "A login form."
            assert rpc.connection_state == "connected"
            assert rpc.process is not None and rpc.process.returncode is None
        finally:
            await rpc.close()

    asyncio.run(asyncio.wait_for(scenario(), 60))


class _ChunkedStdout:
    """Serves bytes in small reads so lines straddle chunk boundaries."""

    def __init__(self, data: bytes, chunk: int) -> None:
        self.data = data
        self.chunk = chunk
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        step = self.chunk if size < 0 else min(self.chunk, size)
        piece = self.data[self.offset : self.offset + step]
        self.offset += len(piece)
        return piece


@pytest.mark.parametrize("chunk", [1, 7, 64, 65_536])
def test_codex_rpc_drops_one_oversized_line_and_keeps_reading(monkeypatch, chunk):
    monkeypatch.setattr(_CodexRpc, "max_message_bytes", 128, raising=False)
    frames = [
        {"method": "first", "params": {}},
        {"method": "item/completed", "params": {"blob": "x" * 300}},
        {"id": 1, "result": {"blob": "y" * 300}},
        {"jsonrpc": "2.0", "id": 3, "result": {"blob": "z" * 300}},
        {"id": 2, "result": {"ok": True}},
        {"method": "last", "params": {}},
    ]
    data = b"".join(
        json.dumps(frame, separators=(",", ":")).encode() + b"\n" for frame in frames
    )

    async def scenario() -> None:
        rpc = _CodexRpc(
            process=SimpleNamespace(
                stdout=_ChunkedStdout(data, chunk), stderr=None, returncode=0
            )
        )
        loop = asyncio.get_running_loop()
        oversized = loop.create_future()
        oversized_jsonrpc = loop.create_future()
        normal = loop.create_future()
        rpc._pending.update({1: oversized, 2: normal, 3: oversized_jsonrpc})

        await rpc._reader()

        # The request whose response was dropped fails instead of hanging.
        for future in (oversized, oversized_jsonrpc):
            with pytest.raises(HarnessTransportError, match="message limit"):
                await future
        assert await normal == {"ok": True}
        received = [rpc.events.get_nowait() for _ in range(rpc.events.qsize())]
        assert [item["method"] for item in received[:-1]] == ["first", "last"]
        assert isinstance(received[-1], HarnessTransportError)
        assert "transport closed" in str(received[-1])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "transport", [HarnessTransport.WEBSOCKET, HarnessTransport.UNIX]
)
def test_codex_endpoint_connections_accept_frames_over_one_mib(tmp_path, transport):
    import websockets

    blob = "B" * (1536 * 1024)

    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            await websocket.send(
                json.dumps({"id": message["id"], "result": {"blob": blob}})
            )

    async def scenario() -> None:
        if transport == HarnessTransport.UNIX:
            socket_path = tmp_path / "codex.sock"
            server = await websockets.unix_serve(
                handler, str(socket_path), max_size=None
            )
            endpoint = f"unix://{socket_path}"
        else:
            server = await websockets.serve(handler, "127.0.0.1", 0, max_size=None)
            port = next(iter(server.sockets)).getsockname()[1]
            endpoint = f"ws://127.0.0.1:{port}"
        profile = HarnessProfile(
            name="Codex endpoint",
            kind=HarnessKind.CODEX_APP_SERVER,
            connection_mode=HarnessConnectionMode.ENDPOINT,
            transport=transport,
            endpoint=endpoint,
        )
        try:
            rpc = await CodexAppServerAdapter()._connect(
                profile, CredentialStore(), (), tmp_path
            )
            try:
                result = await asyncio.wait_for(rpc.request("thread/read", {}), 10)
                assert result == {"blob": blob}
                assert rpc.connection_state == "connected"
            finally:
                await rpc.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_codex_resume_does_not_ask_for_the_thread_history(tmp_path):
    class ResumeRpc:
        def __init__(self) -> None:
            self.events: asyncio.Queue[Any] = asyncio.Queue()
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.connection_state = "connected"

        async def request(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, params))
            if method == "initialize":
                return {"userAgent": "codex-cli/0.154.0"}
            if method == "thread/resume":
                return {"thread": {"id": params["threadId"]}}
            return {}

        async def notify(self, method: str, params: Any = None) -> None:
            return None

        async def close(self) -> None:
            return None

    rpc = ResumeRpc()

    class ResumeAdapter(CodexAppServerAdapter):
        async def _connect(self, *_args: Any, **_kwargs: Any) -> Any:
            return rpc

    profile = HarnessProfile(
        name="Codex", kind=HarnessKind.CODEX_APP_SERVER, executable="/bin/true"
    )
    session = HarnessSession(
        engagement_id="project",
        harness_profile_id=profile.id,
        model="gpt-test",
        external_session_id="thread-existing",
    )

    async def scenario() -> None:
        await ResumeAdapter().open(
            AdapterOpenRequest(
                profile=profile,
                session=session,
                workspace=tmp_path,
                mcp_profiles=(),
                credential_store=CredentialStore(),
                permission_handler=_allow,
            )
        )

    asyncio.run(scenario())
    resume = [params for method, params in rpc.calls if method == "thread/resume"]
    assert len(resume) == 1
    # Nebula never reads thread.turns; the full history can exceed any bound.
    assert resume[0]["excludeTurns"] is True


# CODEX-11: error, hook and terminal-interaction notifications ------------------


def test_codex_retrying_error_is_a_reconnecting_notice():
    events, error, _ = _run(
        [
            _notification(
                "error",
                turnId=TURN,
                willRetry=True,
                error={
                    "message": "Reconnecting... 1/5",
                    "codexErrorInfo": {
                        "responseStreamDisconnected": {"httpStatusCode": 502}
                    },
                    "additionalDetails": "stream disconnected before completion",
                },
            ),
            _agent_completed("Done."),
            _turn_completed(),
        ]
    )

    assert error is None
    notices = [event for event in events if event.type == "notice"]
    assert len(notices) == 1
    notice = notices[0]
    assert notice.title == "Reconnecting…"
    assert notice.item_id == "codex-reconnecting"
    assert "Reconnecting... 1/5" in (notice.summary or "")
    assert "stream disconnected before completion" in (notice.summary or "")
    assert notice.payload["severity"] == "warning"
    assert notice.payload["retrying"] is True
    assert events[-1].type == "completed"
    assert events[-1].message == "Done."


def test_codex_non_retry_error_is_recorded_before_the_failed_status():
    turn_error = {
        "message": "after_agent hook 'audit' failed",
        "codexErrorInfo": "other",
    }
    events, error, _ = _run(
        [
            _notification("error", turnId=TURN, willRetry=False, error=turn_error),
            _turn_completed("failed", error=turn_error),
        ]
    )

    assert "after_agent hook 'audit' failed" in str(error)
    notices = [event for event in events if event.type == "notice"]
    assert len(notices) == 1
    assert notices[0].title == "Codex error"
    assert "after_agent hook 'audit' failed" in (notices[0].summary or "")
    # The failed turn/completed repeats and reports this error, so the notice
    # stays out of the transcript instead of showing it twice.
    assert notices[0].payload["severity"] == "info"
    assert notices[0].payload["retrying"] is False
    assert not any("Unhandled Codex event" in (event.summary or "") for event in events)


def _hook_run(run_id: str, event_name: str, status: str, **extra: Any) -> dict:
    return {
        "id": run_id,
        "eventName": event_name,
        "handlerType": "command",
        "executionMode": "sync",
        "scope": "turn",
        "sourcePath": "/workspace/.codex/hooks.json",
        "displayOrder": 0,
        "status": status,
        "statusMessage": None,
        "startedAt": 1,
        "completedAt": None if status == "running" else 2,
        "durationMs": None if status == "running" else 1,
        "entries": [],
        **extra,
    }


def test_codex_hooks_read_the_nested_run():
    events, error, _ = _run(
        [
            _notification(
                "hook/started",
                turnId=TURN,
                run=_hook_run("hook-1", "preToolUse", "running"),
            ),
            _notification(
                "hook/completed",
                turnId=TURN,
                run=_hook_run(
                    "hook-1",
                    "preToolUse",
                    "blocked",
                    statusMessage="rm -rf is not allowed",
                ),
            ),
            _notification(
                "hook/completed",
                turnId=TURN,
                run=_hook_run("hook-2", "stop", "failed", statusMessage="exit 2"),
            ),
            _notification(
                "hook/completed",
                turnId=TURN,
                run=_hook_run("hook-3", "postToolUse", "completed"),
            ),
            _turn_completed(),
        ]
    )

    assert error is None
    hooks = [event for event in events if event.item_kind == "hook"]
    assert [(event.item_id, event.item_status, event.title) for event in hooks] == [
        ("hook-1", "running", "preToolUse"),
        ("hook-1", "failed", "preToolUse"),
        ("hook-2", "failed", "stop"),
        ("hook-3", "completed", "postToolUse"),
    ]
    assert hooks[1].summary == "Blocked by hook: rm -rf is not allowed"
    assert hooks[2].summary == "Hook failed: exit 2"
    assert hooks[1].payload["run"]["status"] == "blocked"


def test_codex_terminal_interaction_shows_the_stdin_sent():
    events, error, _ = _run(
        [
            _notification(
                "item/commandExecution/terminalInteraction",
                turnId=TURN,
                itemId="command-1",
                processId="proc-1",
                stdin="yes\n",
            ),
            _turn_completed(),
        ]
    )

    assert error is None
    deltas = [event for event in events if event.type == "output_delta"]
    assert [(event.item_id, event.delta) for event in deltas] == [
        ("command-1", "yes\n")
    ]


# CODEX-12: the stderr Codex printed before it died --------------------------------


def test_codex_transport_closed_error_carries_the_redacted_stderr_tail():
    script = (
        "import sys\n"
        "sys.stdin.readline()\n"
        "sys.stderr.write('warming up\\n' * 2000)\n"
        "sys.stderr.write('api_key=sk-live-abcdefghijklmnopqrstuvwxyz\\n')\n"
        "sys.stderr.write('\\x1b[31mERROR\\x1b[0m codex_app_server: sandbox unavailable\\n')\n"
        "sys.stderr.write('Error: failed to load config: invalid value for features.goals\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )

    async def scenario() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        rpc = _CodexRpc(process=process)
        await rpc.start()
        try:
            with pytest.raises(HarnessTransportError) as raised:
                await asyncio.wait_for(rpc.request("initialize", {}), 10)
        finally:
            await rpc.close()
        detail = str(raised.value)
        assert detail.startswith("Codex app-server transport closed")
        assert "failed to load config: invalid value for features.goals" in detail
        assert "ERROR codex_app_server: sandbox unavailable" in detail
        assert "\x1b" not in detail
        assert "sk-live-abcdefghijklmnopqrstuvwxyz" not in detail
        assert "[REDACTED" in detail
        # The operator-visible error is capped at 1,000 characters, so the tail
        # must fit whole instead of being cut before its last line.
        assert len(detail) <= 1_000

    asyncio.run(scenario())


def test_codex_transport_failed_error_carries_the_stderr_tail():
    class FailingStdout:
        async def read(self, _size: int = -1) -> bytes:
            raise OSError("pipe read failed")

    async def scenario() -> None:
        rpc = _CodexRpc(
            process=SimpleNamespace(stdout=FailingStdout(), stderr=None, returncode=0)
        )
        rpc.stderr_tail = "thread 'main' panicked at core/src/codex.rs:12\n"

        await rpc._reader()

        failure = await rpc.events.get()
        assert isinstance(failure, HarnessTransportError)
        assert str(failure).startswith("Codex app-server transport failed: OSError")
        assert "panicked at core/src/codex.rs:12" in str(failure)

    asyncio.run(scenario())


# CODEX-13: server requests Nebula does not implement --------------------------------


def test_codex_unknown_server_requests_get_method_not_found():
    events, error, rpc = _run(
        [
            {"id": 7, "method": "currentTime/read", "params": {"threadId": THREAD}},
            {
                "id": 8,
                "method": "item/tool/call",
                "params": {
                    "threadId": THREAD,
                    "turnId": TURN,
                    "callId": "call-1",
                    "tool": "lookup",
                    "arguments": {},
                },
            },
            {
                "id": 9,
                "method": "attestation/generate",
                "params": {"threadId": THREAD, "turnId": "turn-other"},
            },
            _agent_completed("Done."),
            _turn_completed(),
        ]
    )

    assert error is None
    assert [(request_id, code) for request_id, code, _ in rpc.errors] == [
        (7, -32601),
        (8, -32601),
        (9, -32601),
    ]
    assert "item/tool/call" in rpc.errors[1][2]
    assert rpc.responses == []
    assert events[-1].type == "completed"
    assert events[-1].message == "Done."


def test_codex_method_not_found_reply_reaches_the_app_server(tmp_path):
    profile = _fake_app_server(
        tmp_path,
        """
        if msg.get("method") == "turn/start":
            turn = {"id": "turn-1", "status": "inProgress", "items": []}
            out({"id": msg["id"], "result": {"turn": turn}})
            out({"id": 99, "method": "currentTime/read", "params": {"threadId": "th"}})
        elif msg.get("id") == 99:
            reply = {"type": "agentMessage", "id": "answer-1", "text": json.dumps(msg)}
            out({"method": "item/completed", "params": {"threadId": "th", "turnId": "turn-1", "item": reply}})
            done = {"id": "turn-1", "status": "completed", "items": []}
            out({"method": "turn/completed", "params": {"threadId": "th", "turn": done}})
        """,
    )

    async def scenario() -> None:
        rpc = await CodexAppServerAdapter()._connect(
            profile, CredentialStore(), (), tmp_path
        )
        try:
            connection = CodexAppServerConnection(
                rpc, external_session_id="th", permission_handler=_allow
            )
            events = [
                event
                async for event in connection.run_turn("What time is it?", model="m")
            ]
        finally:
            await rpc.close()
        reply = json.loads(events[-1].message or "{}")
        assert reply["id"] == 99
        assert reply["error"]["code"] == -32601
        assert "currentTime/read" in reply["error"]["message"]
        assert "result" not in reply

    asyncio.run(asyncio.wait_for(scenario(), 15))
