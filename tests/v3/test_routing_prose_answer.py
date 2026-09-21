"""A routing reply without a tool call is the turn's answer.

Z.ai serves GLM with automatic tool selection only, so a routing step's
``tool_choice: "required"`` is advisory there: a question that needs no tool
(or no more tools) is answered in plain text, with no call and no
``finish_response``. Every loop harness (opencode ``session/prompt.ts``,
Codex, the Vercel AI SDK, Cline, pi-mono) ends its loop on a response with no
tool calls and uses that text as the answer. Core does the same instead of
discarding the answer and paying for a second full-context synthesis, while a
reply that is not a usable answer still goes to the synthesis.
"""

import asyncio
import json

import httpx
import pytest

import nebula.v3.chat as chat_module
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatRole,
    ChatTurn,
    ChatTurnStatus,
)
from nebula.v3.providers import (
    ModelCapabilities,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
)
from nebula.v3.native_hooks import discover_native_hooks, snapshot_native_hook
from tests.v3.test_chat import _write_native_hook
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared
from tests.v3.test_native_hook_turn_ends import PAYLOAD_HOOK

GLM = "z-ai/glm-5.3-flash"


def _completion(message: dict, finish: str, *, total_tokens: int = 3) -> dict:
    """One OpenRouter Chat Completions body, as GLM's route returns it."""

    return {
        "id": "gen-glm",
        "model": GLM,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {
            "prompt_tokens": total_tokens - 1,
            "completion_tokens": 1,
            "total_tokens": total_tokens,
        },
    }


def _answer(
    content: str, *, finish: str = "stop", reasoning: str = "", total_tokens: int = 3
) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return _completion(message, finish, total_tokens=total_tokens)


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return _completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "tool_calls",
    )


def _synthesis(text: str) -> httpx.Response:
    """A streamed tool-free synthesis, which only a fallback should request."""

    chunks = [
        {"id": "gen-syn", "model": GLM, "choices": [{"index": 0, "delta": {}}]},
        {
            "id": "gen-syn",
            "model": GLM,
            "choices": [{"index": 0, "delta": {"content": text}}],
        },
        {
            "id": "gen-syn",
            "model": GLM,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _glm_turn(tmp_path, script: list, broker: RecordingBroker | None = None):
    """A tool turn whose provider is GLM on OpenRouter, behind a wire script."""

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if not script:
            raise AssertionError("the wire script was exhausted")
        item = script.pop(0)
        return (
            item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)
        )

    broker = broker or RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    prepared.provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model=GLM,
            model_allowlist=[GLM],
            capabilities=ModelCapabilities(
                streaming=True, tools=True, strict_tools=True
            ),
        ),
        transport=httpx.MockTransport(handler),
    )
    prepared.resolved_model = GLM
    prepared.model_request = prepared.model_request.model_copy(update={"model": GLM})
    return store, service, prepared, broker, seen


def _stream(service, prepared):
    async def scenario():
        return [event async for event in service.stream(prepared)]

    return asyncio.run(scenario())


def _shown(events, name: str) -> str:
    return "".join(payload["delta"] for event, payload in events if event == name)


def _recorded(monkeypatch) -> list[tuple[str, str]]:
    recorded: list[tuple[str, str]] = []
    original = chat_module.record_diagnostic

    def capture(level, feature, event_code, message, **fields):
        recorded.append((level, event_code))
        return original(level, feature, event_code, message, **fields)

    monkeypatch.setattr(chat_module, "record_diagnostic", capture)
    return recorded


def test_glm_prose_reply_on_the_first_routing_step_is_the_answer(tmp_path, monkeypatch):
    recorded = _recorded(monkeypatch)
    store, service, prepared, broker, seen = _glm_turn(
        tmp_path,
        [
            _answer("Hello!", reasoning="A greeting needs no tool."),
            _synthesis("A second, paid-for answer."),
        ],
    )

    events = _stream(service, prepared)

    assert _shown(events, "delta") == "Hello!"
    assert _shown(events, "reasoning_delta") == "A greeting needs no tool."
    [done] = [payload for event, payload in events if event == "done"]
    assert done["message"]["content"] == "Hello!"
    assert done["finish_reason"] == "stop"
    # The routing reply was the answer: no synthesis request followed it.
    assert len(seen) == 1
    assert seen[0]["tool_choice"] == "required"
    assert broker.calls == []
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.execution_claim_id is None
    assert turn.usage.total_tokens == 3
    assert done["usage"]["total_tokens"] == 3
    [stored] = [
        item
        for item in service.session_messages("session")
        if item.role == ChatRole.ASSISTANT
    ]
    assert stored.id == turn.final_message_id
    assert stored.content == "Hello!"
    assert stored.reasoning == "A greeting needs no tool."
    assert stored.finish_reason == "stop"
    # A normal ending is not a deviation worth a warning on every GLM turn.
    assert ("warning", "chat.routing.prose_with_required_tool") not in recorded
    assert not [level for level, _ in recorded if level in {"warning", "error"}]


def test_glm_prose_reply_after_a_tool_step_answers_from_its_results(tmp_path):
    store, service, prepared, broker, seen = _glm_turn(
        tmp_path,
        [
            _call("call_-8231", "safe_read", {"value": "a"}),
            _answer("The value is a."),
            _synthesis("A second, paid-for answer."),
        ],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "The value is a."
    assert [call.arguments for call in broker.calls] == [{"value": "a"}]
    # Two routing requests and no synthesis; the answering one carried the
    # tool result it answered from.
    assert len(seen) == 2
    assert all(payload.get("tool_choice") != "none" for payload in seen)
    replayed = [message for message in seen[1]["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in replayed] == ["call_-8231"]
    assert json.loads(replayed[0]["content"])["value"] == "a"
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert [entry["status"] for entry in turn.tool_history] == ["complete"]
    assert turn.usage.total_tokens == 6
    assert completion.usage.total_tokens == 6
    [stored] = [
        item
        for item in service.session_messages("session")
        if item.role == ChatRole.ASSISTANT
    ]
    assert stored.content == "The value is a."
    assert stored.metadata["tool_results"][0]["capability"] == "safe_read"


@pytest.mark.parametrize(
    "routing",
    [
        # Cut off by the output limit: the answer may be half written.
        _answer("The value is", finish="length"),
        # Ended for a call the reply does not carry.
        _answer("Reading the value.", finish="tool_calls"),
        # A tool-call frame Core could not read is not an answer.
        _answer(
            '<｜DSML｜ calls> <｜DSML｜ invoke name="safe_read">'
            "the value</｜DSML｜ invoke> </｜DSML｜ calls>"
        ),
        # So is an answer that trails into one.
        _answer('Let me check. <｜DSML｜ calls> <｜DSML｜ invoke name="safe_read">'),
    ],
    ids=["output-limit", "call-finish", "control-frame", "trailing-frame"],
)
def test_prose_that_is_not_a_usable_answer_still_goes_to_synthesis(tmp_path, routing):
    store, service, prepared, broker, seen = _glm_turn(
        tmp_path, [routing, _synthesis("The value is a.")]
    )

    events = _stream(service, prepared)

    assert _shown(events, "delta") == "The value is a."
    assert "DSML" not in _shown(events, "reasoning_delta")
    assert len(seen) == 2
    # The second request is the synthesis: tools declared, calling off.
    assert seen[1].get("tool_choice") == "none"
    assert broker.calls == []
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE


def test_reasoning_only_routing_reply_still_goes_to_synthesis(tmp_path):
    store, service, prepared, broker, seen = _glm_turn(
        tmp_path,
        [
            _answer("", reasoning="Nothing to call; the value is known."),
            _synthesis("The value is a."),
        ],
    )

    events = _stream(service, prepared)

    assert _shown(events, "delta") == "The value is a."
    assert len(seen) == 2
    assert seen[1].get("tool_choice") == "none"
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    assert turn.reasoning == "Nothing to call; the value is known."


def test_goal_turn_answered_by_routing_is_charged_once_and_completes(tmp_path):
    store, service, prepared, _, seen = _glm_turn(
        tmp_path,
        [
            _answer("Hello!", total_tokens=5_000),
            _synthesis("A second, paid-for answer."),
        ],
    )
    goal = store.create(
        ChatGoal(
            id="goal",
            engagement_id="project",
            session_id="session",
            objective="Greet the operator",
            completion_criteria=["The operator was greeted"],
            status=ChatGoalStatus.RUNNING,
            # The answering reply spends the rest of the budget. The answer is
            # already written, so the turn completes on it, as a synthesis
            # that spent the budget would.
            token_budget=5_000,
        )
    )
    prepared.turn = store.update(
        ChatTurn,
        "turn",
        {"goal_id": goal.id},
        expected_revision=prepared.turn.revision,
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Hello!"
    assert len(seen) == 1
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    charged = store.get(ChatGoal, goal.id)
    assert charged.usage.total_tokens == 5_000
    assert charged.status == ChatGoalStatus.PAUSED


def test_routing_answer_runs_the_completed_hook_once(tmp_path):
    workspace = tmp_path / "workspace"
    _write_native_hook(
        workspace,
        "audit",
        events=["chat.turn.started", "chat.turn.completed"],
        script=PAYLOAD_HOOK,
        failure_policy="block",
    )
    store, service, prepared, _, seen = _glm_turn(
        tmp_path, [_answer("Hello!"), _synthesis("A second, paid-for answer.")]
    )
    prepared.hook_snapshots = [
        snapshot_native_hook("audit", discover_native_hooks(workspace))
    ]

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Hello!"
    assert len(seen) == 1
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    executions = service.list_turn_hook_executions("turn")
    # The turn ended once, on the routing answer, as a synthesis ends it.
    assert [(item.event_name, item.status) for item in executions] == [
        ("chat.turn.started", "complete"),
        ("chat.turn.completed", "complete"),
    ]
    payload = json.loads(executions[1].stdout)["payload"]
    assert payload["finish_reason"] == "stop"
    assert payload["detail"] is None
