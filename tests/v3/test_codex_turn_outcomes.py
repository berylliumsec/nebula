"""How a Codex app-server turn ends: answers, plans, failures and usage.

Each test drives a real ``CodexAppServerConnection`` over a scripted stand-in for
``_CodexRpc`` that replays Codex v2 notifications in the shapes defined by
``codex-rs/app-server-protocol`` (v2). Runtime tests add the real
``HarnessRuntimeService`` so the durable chat message and error are checked too.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    AgentRun,
    ChatMessage,
    ChatTokenUsage,
    ChatTurn,
    Engagement,
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessTurn,
    HarnessTurnStatus,
    RunBudget,
    utc_now,
)
from nebula.v3.harnesses import (
    ADAPTER_CONTRACT_VERSION,
    MAX_NORMALIZED_TEXT,
    AdapterOpenRequest,
    CodexAppServerConnection,
    HarnessAdapter,
    HarnessError,
    HarnessEvent,
    HarnessHealth,
    HarnessPermissionDecision,
    HarnessRuntimeService,
    HarnessTransportError,
    PermissionTicket,
    _acp_plan_entries,
)
from nebula.v3.model_pricing import codex_model_pricing
from nebula.v3.storage import NebulaStore

THREAD = "thread-outcomes"
TURN = "turn-outcomes"
MODEL = "gpt-5.4"


class ScriptedRpc:
    """Queues a scripted Codex notification list when the turn starts."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.running_turns: dict[str, str] = {}
        self.connection_state = "connected"
        self.closed = False

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "turn/start":
            for item in self.script:
                await self.events.put(item)
            return {"turn": {"id": TURN, "status": "inProgress", "items": []}}
        return {}

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def close(self) -> None:
        self.closed = True
        self.connection_state = "disconnected"


async def _allow(request: Any) -> PermissionTicket:
    future: asyncio.Future[HarnessPermissionDecision] = (
        asyncio.get_running_loop().create_future()
    )
    future.set_result(HarnessPermissionDecision(allowed=True))
    return PermissionTicket("approval-outcomes", request.vendor_request_id, future)


def _connection(rpc: ScriptedRpc) -> CodexAppServerConnection:
    return CodexAppServerConnection(
        rpc,  # type: ignore[arg-type]
        external_session_id=THREAD,
        permission_handler=_allow,
    )


def _run(
    script: list[dict[str, Any]], **kwargs: Any
) -> tuple[list[HarnessEvent], BaseException | None]:
    async def scenario() -> tuple[list[HarnessEvent], BaseException | None]:
        events: list[HarnessEvent] = []
        try:
            async for event in _connection(ScriptedRpc(script)).run_turn(
                "hello", model=MODEL, **kwargs
            ):
                events.append(event)
        except Exception as exc:  # the test inspects the failure itself
            return events, exc
        return events, None

    return asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def _events(script: list[dict[str, Any]], **kwargs: Any) -> list[HarnessEvent]:
    events, error = _run(script, **kwargs)
    assert error is None, f"{type(error).__name__}: {str(error)[:300]}"
    return events


def _n(method: str, **params: Any) -> dict[str, Any]:
    params.setdefault("threadId", THREAD)
    return {"method": method, "params": params}


def _agent_item(
    item_id: str, text: str = "", phase: str | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {"id": item_id, "type": "agentMessage", "text": text}
    if phase is not None:
        item["phase"] = phase
    return item


def _agent_started(item_id: str, phase: str | None = None) -> dict[str, Any]:
    return _n("item/started", turnId=TURN, item=_agent_item(item_id, phase=phase))


def _agent_delta(item_id: str, delta: str, turn_id: str = TURN) -> dict[str, Any]:
    return _n("item/agentMessage/delta", turnId=turn_id, itemId=item_id, delta=delta)


def _agent_completed(
    item_id: str, text: str, phase: str | None = None, turn_id: str = TURN
) -> dict[str, Any]:
    return _n("item/completed", turnId=turn_id, item=_agent_item(item_id, text, phase))


def _turn_completed(
    status: str = "completed",
    *,
    error: Any = None,
    items: list[dict[str, Any]] | None = None,
    turn_id: str = TURN,
) -> dict[str, Any]:
    return _n(
        "turn/completed",
        turn={
            "id": turn_id,
            "status": status,
            "error": error,
            "items": items or [],
            "itemsView": "summary" if items else "notLoaded",
        },
    )


def _turn_error(info: Any, message: str) -> dict[str, Any]:
    return {"message": message, "codexErrorInfo": info, "additionalDetails": None}


def _live_text(events: list[HarnessEvent]) -> str:
    return "".join(
        event.delta or "" for event in events if event.type == "message_delta"
    )


# --- Runtime fixture: the real HarnessRuntimeService over a scripted Codex ---


class ScriptedCodexAdapter(HarnessAdapter):
    kind = HarnessKind.CODEX_APP_SERVER

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.rpcs: list[ScriptedRpc] = []

    async def probe(
        self, profile: HarnessProfile, credential_store: CredentialStore
    ) -> HarnessHealth:
        del credential_store
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

    async def open(self, request: AdapterOpenRequest) -> CodexAppServerConnection:
        rpc = ScriptedRpc(self.script)
        self.rpcs.append(rpc)
        return CodexAppServerConnection(
            rpc,  # type: ignore[arg-type]
            external_session_id=THREAD,
            permission_handler=request.permission_handler,
            interaction_handler=request.interaction_handler,
        )

    async def list_external_sessions(self, profile, credential_store, workspace):  # type: ignore[no-untyped-def]
        del profile, credential_store, workspace
        return []


def _runtime(
    tmp_path: Path, script: list[dict[str, Any]]
) -> tuple[
    NebulaStore, Engagement, HarnessProfile, ScriptedCodexAdapter, HarnessRuntimeService
]:
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(id="eng-a", name="Engagement A"))
    profile = store.create(
        HarnessProfile(
            id="harness-a",
            name="Codex fixture",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/bin/true",
            default_model=MODEL,
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    adapter = ScriptedCodexAdapter(script)
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
        adapter_factory=lambda _: adapter,
    )
    return store, engagement, profile, adapter, runtime


def _stream_chat(
    runtime: HarnessRuntimeService, engagement: Engagement, profile: HarnessProfile
) -> tuple[ChatTurn, HarnessTurn, list[HarnessEvent]]:
    _, chat_turn, turn = runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Summarize the scan",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )

    async def collect() -> list[HarnessEvent]:
        stream: AsyncIterator[HarnessEvent] = runtime.stream_turn(turn.id)
        return [event async for event in stream]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=10))
    return chat_turn, turn, events


def _assistant_messages(store: NebulaStore) -> list[ChatMessage]:
    return [
        message
        for message in store.list_entities(ChatMessage, limit=50)
        if message.role.value == "assistant"
    ]


# --- CODEX-3: an oversize final answer is bounded, not fatal ---


def test_codex_completed_message_over_normalized_limit_is_truncated_not_fatal():
    events = _events(
        [_agent_completed("msg-1", "A" * (MAX_NORMALIZED_TEXT + 1)), _turn_completed()]
    )

    completed = events[-1]
    assert completed.type == "completed"
    assert completed.message is not None
    assert len(completed.message) <= MAX_NORMALIZED_TEXT
    assert completed.message.startswith("A" * 100_000)
    assert "truncated" in completed.message[-200:].lower()
    assert completed.payload["message_truncated"] is True


def test_codex_goal_run_joined_answers_over_limit_are_truncated_not_fatal():
    def goal(status: str, turn_id: str) -> dict[str, Any]:
        return _n(
            "thread/goal/updated",
            turnId=turn_id,
            goal={
                "threadId": THREAD,
                "objective": "Audit the VPN appliance",
                "status": status,
                "tokenBudget": 500_000,
                "tokensUsed": 1_000,
                "timeUsedSeconds": 10,
            },
        )

    script = [
        goal("active", TURN),
        _agent_completed("m1", "a" * 60_000, "final_answer"),
        _turn_completed(),
    ]
    for index in range(2, 5):
        turn_id = f"turn-{index}"
        script += [
            _n(
                "turn/started",
                turn={"id": turn_id, "status": "inProgress", "items": []},
            ),
            _agent_completed(
                f"m{index}", chr(96 + index) * 60_000, "final_answer", turn_id=turn_id
            ),
        ]
        if index == 4:
            script.append(goal("complete", turn_id))
        script.append(_turn_completed(turn_id=turn_id))

    events = _events(script)

    completed = events[-1]
    assert completed.type == "completed"
    assert completed.message is not None
    assert len(completed.message) <= MAX_NORMALIZED_TEXT
    assert completed.message.startswith("a" * 60_000 + "\n\n" + "b" * 60_000)
    assert completed.payload["message_truncated"] is True


def test_codex_oversize_answer_is_saved_as_a_bounded_chat_message(tmp_path):
    store, engagement, profile, _, runtime = _runtime(
        tmp_path,
        [_agent_completed("msg-1", "A" * (MAX_NORMALIZED_TEXT + 1)), _turn_completed()],
    )

    chat_turn, turn, events = _stream_chat(runtime, engagement, profile)

    assert not any(event.type == "error" for event in events)
    assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
    saved = store.get(ChatTurn, chat_turn.id)
    assert saved.status.value == "complete"
    [message] = _assistant_messages(store)
    assert saved.final_message_id == message.id
    assert len(message.content) <= MAX_NORMALIZED_TEXT
    assert message.content.startswith("A" * 100_000)


def test_complete_owner_bounds_a_message_longer_than_chat_messages_allow(tmp_path):
    store, engagement, profile, _, runtime = _runtime(tmp_path, [])
    _, chat_turn, turn = runtime.prepare_chat(
        engagement_id=engagement.id,
        profile_id=profile.id,
        model=None,
        prompt="Summarize the scan",
        chat_session_id=None,
        harness_session_id=None,
        mcp_server_ids=[],
    )

    runtime._complete_owner(turn, "B" * 250_000, ChatTokenUsage())

    [message] = _assistant_messages(store)
    assert len(message.content) <= MAX_NORMALIZED_TEXT
    assert message.content.startswith("B" * 100_000)
    assert store.get(ChatTurn, chat_turn.id).final_message_id == message.id


# --- CODEX-4: a failure after the final answer keeps the answer ---

_HOOK_FAILURE = _turn_error(
    "other", "after_agent hook 'audit' failed and aborted turn completion: exit 1"
)
_FAILED_AFTER_ANSWER = [
    _agent_started("msg-1", "final_answer"),
    _agent_delta("msg-1", "Final report: 3 critical findings."),
    _agent_completed("msg-1", "Final report: 3 critical findings.", "final_answer"),
    _n("error", turnId=TURN, willRetry=False, error=_HOOK_FAILURE),
    _turn_completed("failed", error=_HOOK_FAILURE),
]


def test_codex_failed_turn_after_final_answer_completes_with_the_answer():
    events = _events(_FAILED_AFTER_ANSWER)

    completed = events[-1]
    assert completed.type == "completed"
    assert completed.message == "Final report: 3 critical findings."
    assert completed.payload["turn_error"]["message"] == _HOOK_FAILURE["message"]
    warnings = [
        event
        for event in events
        if event.type == "notice" and event.payload.get("severity") == "warning"
    ]
    assert warnings
    assert _HOOK_FAILURE["message"] in (warnings[-1].summary or "")


def test_codex_failed_turn_after_final_answer_saves_the_answer(tmp_path):
    store, engagement, profile, _, runtime = _runtime(tmp_path, _FAILED_AFTER_ANSWER)

    chat_turn, turn, events = _stream_chat(runtime, engagement, profile)

    assert store.get(HarnessTurn, turn.id).status == HarnessTurnStatus.COMPLETE
    assert store.get(ChatTurn, chat_turn.id).status.value == "complete"
    assert [message.content for message in _assistant_messages(store)] == [
        "Final report: 3 critical findings."
    ]
    assert any(
        event.type == "notice"
        and _HOOK_FAILURE["message"] in (event.summary or "")
        and event.payload.get("severity") == "warning"
        for event in events
    )


# --- CODEX-5: a failed turn is not a transport failure ---


@pytest.mark.parametrize(
    ("info", "reason_code"),
    [
        ("usageLimitExceeded", "quota_exhausted"),
        ("serverOverloaded", "dependency_unavailable"),
        (
            {"responseTooManyFailedAttempts": {"httpStatusCode": 500}},
            "dependency_unavailable",
        ),
        (
            {"responseStreamDisconnected": {"httpStatusCode": None}},
            "dependency_unavailable",
        ),
        ({"httpConnectionFailed": {"httpStatusCode": 502}}, "dependency_unavailable"),
        ("contextWindowExceeded", "invalid_input"),
        ("unauthorized", "authentication_failed"),
    ],
)
def test_codex_failed_turn_raises_a_turn_failure_with_codex_message(info, reason_code):
    message = "Codex could not finish this turn."
    events, error = _run([_turn_completed("failed", error=_turn_error(info, message))])

    assert [event.type for event in events] == ["started"]
    assert isinstance(error, HarnessError)
    assert not isinstance(error, HarnessTransportError)
    assert type(error).__name__ == "HarnessTurnFailedError"
    assert str(error).startswith(message)
    assert "codexErrorInfo" not in str(error)
    assert getattr(error, "reason_code", None) == reason_code
    assert getattr(error, "vendor_error_info", None) == info
    if info == "contextWindowExceeded":
        assert "compact" in str(error).lower()


def test_codex_turn_failure_keeps_connection_and_maps_error_info(tmp_path):
    usage_limit = _turn_error(
        "usageLimitExceeded", "You've hit your usage limit. Try again in 2 hours."
    )
    store, engagement, profile, adapter, runtime = _runtime(
        tmp_path, [_turn_completed("failed", error=usage_limit)]
    )

    _, turn, events = _stream_chat(runtime, engagement, profile)

    error = events[-1]
    assert error.type == "error"
    assert error.message == usage_limit["message"]
    assert error.reason_code == "quota_exhausted"
    assert error.retryable is False
    failed = store.get(HarnessTurn, turn.id)
    assert failed.status == HarnessTurnStatus.INTERRUPTED
    assert failed.error == usage_limit["message"]
    # The Codex process is healthy: the next turn reuses it instead of respawning.
    [rpc] = adapter.rpcs
    assert rpc.closed is False
    assert failed.harness_session_id in runtime._connections


def test_codex_turn_failure_reason_reaches_the_chat_stream_error_frame(tmp_path):
    overloaded = _turn_error(
        "serverOverloaded",
        "Selected model is at capacity. Please try a different model.",
    )
    store, engagement, profile, adapter, runtime = _runtime(
        tmp_path, [_turn_completed("failed", error=overloaded)]
    )
    app = create_app(store, auth_token="t", harness_runtime_service=runtime)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": "Bearer t"},
            json={
                "backend": "harness",
                "engagement_id": engagement.id,
                "harness_profile_id": profile.id,
                "model": MODEL,
                "mcp_server_ids": [],
                "stream": True,
                "messages": [{"role": "user", "content": "Summarize the scan"}],
            },
        )
        # Core shutdown closes it later; the failed turn itself must not.
        assert adapter.rpcs[0].closed is False

    frames: list[tuple[str, dict[str, Any]]] = []
    for block in response.text.split("\n\n"):
        lines = block.strip().splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if name and data:
            frames.append((name, json.loads(data)))
    [frame] = [data for name, data in frames if name == "error"]
    assert frame["reason_code"] == "dependency_unavailable"
    assert frame["retryable"] is True
    assert frame["detail"] == overloaded["message"]


# --- CODEX-6: a proposed plan is the plan-mode answer ---

_PLAN_TEXT = "# Plan\n1. Enumerate hosts\n2. Scan ports"


def _plan_script(*before_completion: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *before_completion,
        _n(
            "item/started",
            turnId=TURN,
            item={"id": "plan-1", "type": "plan", "text": ""},
        ),
        _n(
            "item/plan/delta",
            turnId=TURN,
            itemId="plan-1",
            delta="# Plan\n1. Enumerate hosts\n",
        ),
        _n("item/plan/delta", turnId=TURN, itemId="plan-1", delta="2. Scan ports"),
        _n(
            "item/completed",
            turnId=TURN,
            item={"id": "plan-1", "type": "plan", "text": _PLAN_TEXT},
        ),
        _turn_completed(),
    ]


def test_codex_plan_mode_proposed_plan_becomes_completed_message():
    events = _events(_plan_script(), mode="plan")

    assert events[-1].type == "completed"
    assert events[-1].message == _PLAN_TEXT
    assert _live_text(events) == _PLAN_TEXT
    # Plan deltas are proposed-plan text, not step lists: no empty step upserts.
    assert not any(
        event.type == "item_upsert"
        and event.item_kind == "plan"
        and not event.plan
        and event.item_status == "streaming"
        for event in events
    )


def test_codex_proposed_plan_is_appended_to_the_agent_answer():
    events = _events(
        _plan_script(
            _agent_started("msg-1"),
            _agent_delta("msg-1", "Here is the plan."),
            _agent_completed("msg-1", "Here is the plan."),
        ),
        mode="plan",
    )

    assert events[-1].message == "Here is the plan.\n\n" + _PLAN_TEXT
    assert _live_text(events) == "Here is the plan.\n\n" + _PLAN_TEXT


# --- CODEX-7: whitespace-only messages and turn.items ---


def test_codex_whitespace_trailing_message_does_not_replace_answer():
    answer = "Answer: CVE-2026-1234 applies."
    events = _events(
        [
            _agent_started("msg-1"),
            _agent_delta("msg-1", answer),
            _agent_completed("msg-1", answer),
            _agent_started("msg-2"),
            _agent_completed("msg-2", "\n"),
            _turn_completed(),
        ]
    )

    assert events[-1].type == "completed"
    assert events[-1].message == answer


def test_codex_turn_completed_items_supply_missing_final_answer():
    answer = "The appliance runs firmware 9.1."
    events = _events(
        [
            _agent_started("commentary-1", "commentary"),
            _agent_delta("commentary-1", "Checking the banner."),
            _turn_completed(items=[_agent_item("msg-9", answer, "final_answer")]),
        ]
    )

    assert events[-1].message == answer


def test_codex_turn_completed_items_prefer_the_final_answer_phase():
    events = _events(
        [
            _agent_completed("msg-1", "Streamed draft."),
            _turn_completed(
                items=[
                    _agent_item("msg-1", "Final: port 443 only.", "final_answer"),
                    _agent_item("msg-2", "Phase-less trailer."),
                    _agent_item("msg-3", "Checking.", "commentary"),
                    _agent_item("msg-4", "  \n"),
                ]
            ),
        ]
    )

    assert events[-1].message == "Final: port 443 only."


# --- CODEX-8: turn usage is the growth of Codex's cumulative total ---


def _usage(total: tuple[int, int, int], last: tuple[int, int, int]) -> dict[str, Any]:
    def breakdown(value: tuple[int, int, int]) -> dict[str, int]:
        input_tokens, cached, output = value
        return {
            "inputTokens": input_tokens,
            "cachedInputTokens": cached,
            "outputTokens": output,
            "reasoningOutputTokens": 0,
            "totalTokens": input_tokens + output,
        }

    return _n(
        "thread/tokenUsage/updated",
        turnId=TURN,
        tokenUsage={
            "total": breakdown(total),
            "last": breakdown(last),
            "modelContextWindow": 272_000,
        },
    )


# A resumed thread already used (50_000 in, 2_000 out) before this turn. The turn
# makes three model requests; Codex re-sends the second update on a rate-limit refresh.
_TURN_USAGE = [
    _usage((60_000, 4_000, 2_500), (10_000, 4_000, 500)),
    _usage((71_000, 12_000, 2_900), (11_000, 8_000, 400)),
    _usage((71_000, 12_000, 2_900), (11_000, 8_000, 400)),
    _usage((83_000, 20_000, 3_200), (12_000, 8_000, 300)),
    _agent_completed("msg-1", "done"),
    _turn_completed(),
]


def test_codex_usage_is_turn_total_not_last_request():
    events = _events(_TURN_USAGE)

    usage = [event for event in events if event.type == "usage"][-1]
    assert usage.usage is not None
    assert (usage.usage.input_tokens, usage.usage.output_tokens) == (33_000, 1_200)
    assert usage.usage.total_tokens == 34_200
    detailed = usage.detailed_usage
    assert detailed is not None
    assert (detailed.input_tokens, detailed.output_tokens) == (33_000, 1_200)
    assert detailed.cached_input_tokens == 20_000
    assert detailed.total_tokens == 34_200
    # The context meter still reads the latest request's prompt size.
    assert detailed.context_used == 12_000
    assert detailed.context_window == 272_000
    pricing = codex_model_pricing(MODEL)
    assert pricing is not None
    assert detailed.cost_usd == pytest.approx(
        pricing.estimate_cost_usd(
            input_tokens=33_000, output_tokens=1_200, cached_input_tokens=20_000
        ),
        abs=1e-6,
    )


def test_codex_mission_spend_counts_every_model_request(tmp_path):
    store, engagement, profile, _, runtime = _runtime(tmp_path, _TURN_USAGE)

    async def scenario() -> AgentRun:
        run = await runtime.start_mission(
            engagement_id=engagement.id,
            name="Usage review",
            objective="Count every request",
            profile_id=profile.id,
            model=MODEL,
            budget=RunBudget(max_duration_seconds=10),
        )
        await runtime._mission_tasks[run.id]
        finished = store.get(AgentRun, run.id)
        await runtime.shutdown()
        return finished

    finished = asyncio.run(asyncio.wait_for(scenario(), timeout=20))

    pricing = codex_model_pricing(MODEL)
    assert pricing is not None
    assert finished.metadata["input_tokens"] == 33_000
    assert finished.metadata["output_tokens"] == 1_200
    assert finished.metadata["spent_usd"] == pytest.approx(
        pricing.estimate_cost_usd(
            input_tokens=33_000, output_tokens=1_200, cached_input_tokens=20_000
        ),
        abs=1e-6,
    )


# --- CODEX-9: Codex plan steps use `step` ---


def test_codex_turn_plan_updated_uses_step_titles():
    steps = [
        {"step": "Enumerate hosts", "status": "completed"},
        {"step": "Scan ports", "status": "inProgress"},
    ]

    assert [(entry.title, entry.status) for entry in _acp_plan_entries(steps)] == [
        ("Enumerate hosts", "completed"),
        ("Scan ports", "in_progress"),
    ]
    events = _events(
        [
            _n("turn/plan/updated", turnId=TURN, explanation=None, plan=steps),
            _turn_completed(),
        ]
    )
    [upsert] = [event for event in events if event.item_kind == "plan"]
    assert [entry.title for entry in upsert.plan] == ["Enumerate hosts", "Scan ports"]


# --- CODEX-10: separate agent messages stay separate in the live stream ---


def test_codex_distinct_agent_messages_are_separated_live():
    events = _events(
        [
            _agent_started("msg-1"),
            _agent_delta("msg-1", "I'll check the config."),
            _agent_completed("msg-1", "I'll check the config."),
            _agent_started("msg-2"),
            _agent_delta("msg-2", "The config "),
            _agent_delta("msg-2", "enables TLS 1.0."),
            _agent_completed("msg-2", "The config enables TLS 1.0."),
            _turn_completed(),
        ]
    )

    assert _live_text(events) == "I'll check the config.\n\nThe config enables TLS 1.0."
    assert events[-1].message == "The config enables TLS 1.0."
