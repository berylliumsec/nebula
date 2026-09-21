"""Stopped or interrupted harness chat turns end cleanly and keep their partial answer."""

from __future__ import annotations

import asyncio
import gc
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatTurn,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessTurn,
    HarnessTurnStatus,
    utc_now,
)
from nebula.v3.harnesses import (
    ADAPTER_CONTRACT_VERSION,
    AdapterOpenRequest,
    CodexAppServerConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessRuntimeService,
)
from nebula.v3.storage import NebulaStore

THREAD = "thread-stopped"
TURN = "turn-stopped"
HEADERS = {"Authorization": "Bearer test-token"}
INTERNAL_ERROR = "harness turn completed without a durable message"


def notification(method: str, **params: Any) -> dict[str, Any]:
    params.setdefault("threadId", THREAD)
    return {"method": method, "params": params}


def agent_delta(item_id: str, delta: str, turn_id: str = TURN) -> dict[str, Any]:
    return notification(
        "item/agentMessage/delta", turnId=turn_id, itemId=item_id, delta=delta
    )


def turn_completed(status: str = "completed", turn_id: str = TURN) -> dict[str, Any]:
    return notification(
        "turn/completed",
        turn={"id": turn_id, "status": status, "error": None, "items": []},
    )


class ScriptedRpc:
    """Stands in for the Codex app-server: turn/start queues scripted notifications."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.connection_state = "connected"

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "turn/start":
            for item in self.script:
                await self.events.put(item)
            return {"turn": {"id": TURN, "status": "inProgress", "items": []}}
        return {}

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        return None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def close(self) -> None:
        return None


class OperatorStopRpc(ScriptedRpc):
    """Streams part of an answer, then the operator presses Stop mid-turn."""

    def __init__(self, runtime: HarnessRuntimeService, store: NebulaStore) -> None:
        super().__init__([])
        self.runtime = runtime
        self.store = store

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "turn/start":
            await self.events.put(agent_delta("msg-1", "Partial findings so far"))

            async def operator_stop() -> None:
                await asyncio.sleep(0.3)
                running = [
                    turn
                    for turn in self.store.list_entities(HarnessTurn, limit=100)
                    if turn.status == HarnessTurnStatus.RUNNING
                ]
                # The same call POST /harness-turns/{id}/stop makes.
                await self.runtime.cancel_turn(
                    running[0].id, reason="Stopped by operator"
                )

            asyncio.get_running_loop().create_task(operator_stop())
            return {"turn": {"id": TURN, "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            await asyncio.sleep(0.1)
            await self.events.put(turn_completed("interrupted"))
        return {}


class CodexAdapter(HarnessAdapter):
    kind = HarnessKind.CODEX_APP_SERVER

    def __init__(self, rpc_factory) -> None:
        self.rpc_factory = rpc_factory

    async def probe(self, profile, credential_store) -> HarnessHealth:
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=profile.kind,
            harness_version="fixture-1",
            capabilities=HarnessCapabilities(
                steering=True,
                adapter_version=ADAPTER_CONTRACT_VERSION + "/codex-v2",
                checked_at=utc_now(),
            ),
        )

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        return CodexAppServerConnection(
            self.rpc_factory(),
            external_session_id=THREAD,
            permission_handler=request.permission_handler,
            interaction_handler=request.interaction_handler,
        )


class ScriptedConnection(HarnessConnection):
    """A vendor connection that streams part of an answer and then ends the turn."""

    adapter_version = ADAPTER_CONTRACT_VERSION + "/fixture"
    external_session_id = "fixture-session"

    def __init__(self, terminal: str) -> None:
        self.terminal = terminal

    async def run_turn(self, prompt: str, *, model: str, **_: Any):
        yield HarnessEvent(type="started")
        yield HarnessEvent(type="message_delta", delta="Working on it")
        yield HarnessEvent(type=self.terminal, message="cancelled")  # type: ignore[arg-type]

    async def steer(self, text: str) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None


class ScriptedAdapter(HarnessAdapter):
    kind = HarnessKind.GROK_ACP

    def __init__(self, terminal: str) -> None:
        self.terminal = terminal

    async def probe(
        self, profile, credential_store
    ) -> HarnessHealth:  # pragma: no cover
        raise NotImplementedError

    async def open(self, request: AdapterOpenRequest) -> HarnessConnection:
        return ScriptedConnection(self.terminal)


class DiagnosticRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def record(
        self, level: str, feature: str, event_code: str, message: str, **_: Any
    ) -> None:
        self.records.append((level, event_code))


def build_runtime(tmp_path: Path, kind: HarnessKind, adapter: HarnessAdapter):
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(
        HarnessProfile(
            id="harness-a",
            name="Fixture",
            kind=kind,
            executable="/bin/true",
            default_model="fixture-model",
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    return store, engagement, profile, runtime


def chat_body(engagement: Engagement, profile: HarnessProfile, *, stream: bool = True):
    return {
        "backend": "harness",
        "engagement_id": engagement.id,
        "harness_profile_id": profile.id,
        "model": "fixture-model",
        "mcp_server_ids": [],
        "stream": stream,
        "messages": [{"role": "user", "content": "Summarize the scan"}],
    }


def sse_frames(text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]


def assistant_messages(store: NebulaStore) -> list[ChatMessage]:
    return [
        message
        for message in store.list_entities(ChatMessage, limit=50)
        if message.role == ChatRole.ASSISTANT
    ]


def wait_for_assistant(store: NebulaStore) -> list[ChatMessage]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        messages = assistant_messages(store)
        if messages:
            return messages
        time.sleep(0.02)
    return assistant_messages(store)


def assert_clean_stop(frames: list[dict[str, Any]], recorder: DiagnosticRecorder):
    assert INTERNAL_ERROR not in [frame.get("detail") for frame in frames]
    assert "error" not in [frame["type"] for frame in frames]
    assert frames[-1]["type"] == "cancelled"
    assert frames[-1]["turn_id"]
    assert ("error", "harnesses.stream.failed") not in recorder.records


def test_codex_interrupted_turn_ends_stream_cleanly_and_keeps_partial_answer(
    tmp_path,
):
    adapter = CodexAdapter(
        lambda: ScriptedRpc(
            [
                agent_delta("msg-1", "Partial findings: port 22 open"),
                turn_completed("interrupted"),
            ]
        )
    )
    store, engagement, profile, runtime = build_runtime(
        tmp_path, HarnessKind.CODEX_APP_SERVER, adapter
    )
    recorder = DiagnosticRecorder()
    app = create_app(
        store,
        auth_token="test-token",
        harness_runtime_service=runtime,
        diagnostic_manager=recorder,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/completions",
            headers=HEADERS,
            json=chat_body(engagement, profile),
        )
        frames = sse_frames(response.text)
        assert_clean_stop(frames, recorder)
        chat_turn = store.get(ChatTurn, frames[-1]["turn_id"])
        assert chat_turn.status.value == "interrupted"
        [message] = assistant_messages(store)
        assert message.content == "Partial findings: port 22 open"
        assert message.finish_reason == "interrupted"
        assert message.metadata["interrupted"] is True
        assert message.metadata["harness_turn_id"] == chat_turn.harness_turn_id

        # A viewer that attaches later gets the same clean terminal frame.
        follow = client.get(
            f"/api/v1/chat/turns/{chat_turn.id}/events", headers=HEADERS
        )
        followed = sse_frames(follow.text)
        assert followed[-1]["type"] == "cancelled"
        assert "error" not in [frame["type"] for frame in followed]


def test_operator_stop_while_stream_is_attached_ends_with_cancelled_frame(tmp_path):
    holder: dict[str, Any] = {}
    adapter = CodexAdapter(lambda: holder["rpc"]())
    store, engagement, profile, runtime = build_runtime(
        tmp_path, HarnessKind.CODEX_APP_SERVER, adapter
    )
    holder["rpc"] = lambda: OperatorStopRpc(runtime, store)
    recorder = DiagnosticRecorder()
    app = create_app(
        store,
        auth_token="test-token",
        harness_runtime_service=runtime,
        diagnostic_manager=recorder,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/completions",
            headers=HEADERS,
            json=chat_body(engagement, profile),
        )
        frames = sse_frames(response.text)
        assert_clean_stop(frames, recorder)
        assert frames[-1]["detail"] == "Stopped by operator"
        assert store.get(ChatTurn, frames[-1]["turn_id"]).status.value == "cancelled"
        [message] = wait_for_assistant(store)
        assert message.content == "Partial findings so far"
        assert message.finish_reason == "interrupted"


def test_codex_goal_interruption_keeps_answers_of_completed_goal_turns(tmp_path):
    goal = {
        "threadId": THREAD,
        "objective": "Audit the VPN appliance",
        "status": "active",
        "tokenBudget": 500_000,
        "tokensUsed": 1_000,
        "timeUsedSeconds": 10,
    }
    script = [
        notification("thread/goal/updated", turnId=TURN, goal=goal),
        # This app-server build sends the completed answer without deltas.
        notification(
            "item/completed",
            turnId=TURN,
            item={
                "id": "m1",
                "type": "agentMessage",
                "text": "Stage 1: enumerated 12 hosts, 3 expose SSH.",
                "phase": "final_answer",
            },
        ),
        turn_completed(),
        notification(
            "turn/started", turn={"id": "turn-2", "status": "inProgress", "items": []}
        ),
        agent_delta("m2", "Stage 2: scanning", turn_id="turn-2"),
        turn_completed("interrupted", turn_id="turn-2"),
    ]
    adapter = CodexAdapter(lambda: ScriptedRpc(script))
    store, engagement, profile, runtime = build_runtime(
        tmp_path, HarnessKind.CODEX_APP_SERVER, adapter
    )
    app = create_app(store, auth_token="test-token", harness_runtime_service=runtime)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/completions",
            headers=HEADERS,
            json=chat_body(engagement, profile),
        )
        frames = sse_frames(response.text)
        assert frames[-1]["type"] == "cancelled"
        [message] = assistant_messages(store)
        assert message.content == (
            "Stage 1: enumerated 12 hosts, 3 expose SSH.\n\nStage 2: scanning"
        )
        interrupted = [frame for frame in frames if frame["type"] == "interrupted"]
        # The saved answer is not echoed through the activity stream a second time.
        assert "Stage 1" not in json.dumps(interrupted)


def test_agent_cancelled_turn_is_not_an_internal_error_for_non_streamed_requests(
    tmp_path,
):
    store, engagement, profile, runtime = build_runtime(
        tmp_path, HarnessKind.GROK_ACP, ScriptedAdapter("interrupted")
    )
    app = create_app(store, auth_token="test-token", harness_runtime_service=runtime)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/completions",
            headers=HEADERS,
            json=chat_body(engagement, profile, stream=False),
        )
        assert response.status_code == 409
        assert INTERNAL_ERROR not in response.text
        assert "stopped" in response.json()["detail"].lower()
        [message] = assistant_messages(store)
        assert message.content == "Working on it"


def test_core_restart_interruption_still_reports_its_error_to_followers(tmp_path):
    """Only a stop the harness itself reported settles as stopped; an uncertain
    outcome after a Core restart keeps its explanatory error frame."""

    store, engagement, profile, runtime = build_runtime(
        tmp_path, HarnessKind.GROK_ACP, ScriptedAdapter("interrupted")
    )
    _, chat_turn, harness_turn = runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Summarize the scan",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )
    store.update(
        HarnessTurn,
        harness_turn.id,
        {
            "status": HarnessTurnStatus.INTERRUPTED,
            "error": "Nebula Core restarted while the harness outcome was uncertain",
        },
        expected_revision=harness_turn.revision,
    )
    latest = store.get(ChatTurn, chat_turn.id)
    store.update(
        ChatTurn,
        chat_turn.id,
        {
            "status": "interrupted",
            "error": "Nebula Core restarted while the harness outcome was uncertain",
        },
        expected_revision=latest.revision,
    )
    app = create_app(store, auth_token="test-token", harness_runtime_service=runtime)
    with TestClient(app) as client:
        follow = client.get(
            f"/api/v1/chat/turns/{chat_turn.id}/events", headers=HEADERS
        )
        frames = sse_frames(follow.text)
        assert frames[-1] == {
            "type": "error",
            "detail": "Nebula Core restarted while the harness outcome was uncertain",
        }


def test_turns_ended_by_the_vendor_leave_no_unretrieved_task(tmp_path):
    for terminal in ("interrupted", "error"):
        seen: list[BaseException | None] = []

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(
                lambda _loop, context: seen.append(context.get("exception"))
            )
            store, engagement, profile, runtime = build_runtime(
                tmp_path / terminal, HarnessKind.GROK_ACP, ScriptedAdapter(terminal)
            )
            _, _, turn = runtime.prepare_chat(
                engagement_id=engagement.id,
                profile_id=profile.id,
                model=None,
                prompt="Summarize the scan",
                chat_session_id=None,
                harness_session_id=None,
                mcp_server_ids=[],
            )
            await runtime.start_chat_turn(turn.id)
            await asyncio.sleep(0.05)
            gc.collect()
            await asyncio.sleep(0.05)
            await runtime.shutdown()

        (tmp_path / terminal).mkdir()
        asyncio.run(scenario())
        assert seen == [], terminal


async def _events(*events: HarnessEvent) -> AsyncIterator[HarnessEvent]:
    for event in events:
        yield event


def test_coalescer_retrieves_its_finished_read_when_closed_early():
    from nebula.v3.harnesses import _coalesce_activity_deltas

    seen: list[BaseException | None] = []

    async def scenario() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: seen.append(context.get("exception"))
        )
        coalesced = _coalesce_activity_deltas(
            _events(HarnessEvent(type="interrupted", message="interrupted"))
        )
        assert (await anext(coalesced)).type == "interrupted"
        # Let the eager read of the exhausted source finish before closing.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await coalesced.aclose()
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert seen == []
