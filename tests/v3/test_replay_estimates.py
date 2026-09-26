"""A tool turn's request estimate counts what its route is sent and billed.

Replayed tool history is estimated in the shape adapters send it: each
routing response once, its reasoning's text once however many fields repeat
it, and none of Core's bookkeeping. The reasoning a turn replays is budgeted:
the newest response always keeps its reasoning, and earlier ones let it go
only when clearing every earlier result could not bring a request that
crossed its target to its watermark, so the request changes only at crossings.
Capacity checks count JSON as sent.
"""

import asyncio
import json

import httpx

from nebula.v3.context import (
    estimate_model_request,
    estimate_model_request_parts,
    estimate_tokens,
    replay_wire_form,
)
from nebula.v3.providers import (
    AnthropicProvider,
    GeminiProvider,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolResult,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolChoice,
)
from tests.v3.test_in_turn_context_pruning import (
    ANSWER,
    LongTurnProvider,
    ScanBroker,
    _capacity,
    _extends,
    _long_turn,
    _turn_requests,
)

THOUGHT = "I should read the next file in the chain before answering. " * 20


def _state(model: str = "model-a", thought: str = THOUGHT) -> dict:
    return {
        "provider_id": "provider",
        "model": model,
        "reasoning": thought,
        "reasoning_details": [
            {"type": "reasoning.text", "text": thought, "format": "unknown", "index": 0}
        ],
    }


def _result(index: int, group: str, state: dict | None = None) -> ModelToolResult:
    return ModelToolResult(
        call_id=f"call-{index}",
        name="workspace.read",
        arguments={"path": f"chain/file-{index}.txt"},
        output={"lines": [{"line": 1, "text": f"KEY: DELTA-{6900 + index}"}]},
        response_group=group,
        reasoning_state=state,
    )


def _request(results: list[ModelToolResult]) -> ModelRequest:
    return ModelRequest(
        model="model-a",
        messages=[ModelMessage(role="user", content="Follow the chain.")],
        tool_results=results,
    )


def test_replayed_reasoning_is_counted_once_per_response_and_text():
    """DeepSeek on OpenRouter bills a replayed thought once.

    Core counted each result's copy of its response's reasoning state, with
    the thought twice in it (``reasoning`` and ``reasoning_details``) and the
    route and model it came from.
    """

    without = estimate_model_request_parts(
        _request([_result(index, "group-1") for index in range(3)])
    ).tool_results
    batch = estimate_model_request_parts(
        _request([_result(index, "group-1", _state()) for index in range(3)])
    ).tool_results
    # The thought, once, plus the short format labels it is filed under.
    assert batch - without <= estimate_tokens(THOUGHT) + 20
    (form,) = replay_wire_form(
        _request([_result(index, "group-1", _state()) for index in range(3)])
    )
    assert form["reasoning"].count(THOUGHT) == 1
    assert "provider" not in form["reasoning"]
    assert [call["id"] for call in form["calls"]] == ["call-0", "call-1", "call-2"]
    # Reasoning written by another model is not sent, so it is not counted.
    other = estimate_model_request_parts(
        _request([_result(0, "group-1", _state(model="model-b"))])
    ).tool_results
    assert (
        other
        == estimate_model_request_parts(_request([_result(0, "group-1")])).tool_results
    )


class ThinkingLongTurn(LongTurnProvider):
    """A long scan turn whose every routing response carries a long thought."""

    async def complete(self, request: ModelRequest):
        response = await super().complete(request)
        if not response.tool_calls:
            return response
        thought = f"step {self.issued}: " + THOUGHT * 8
        return response.model_copy(
            update={
                "reasoning": thought,
                "reasoning_state": {
                    "provider_id": "provider",
                    "model": "model-a",
                    "reasoning": thought,
                },
            }
        )


def _thinking(result: ModelToolResult) -> bool:
    """Whether a replayed result still carries reasoning, not just its stamp."""

    return bool(set(result.reasoning_state or {}) - {"provider_id", "model"})


def _cleared(result: ModelToolResult) -> bool:
    return isinstance(result.output, dict) and bool(result.output.get("output_cleared"))


def test_earlier_reasoning_is_the_last_thing_a_crossing_lets_go(tmp_path, monkeypatch):
    """Replayed thoughts are billed, and they are often the model's only note
    of what a cleared result held: they go only when clearing every earlier
    result could not bring the request to its watermark, at that crossing,
    and the newest response always keeps its own."""

    import nebula.v3.chat as chat_module

    recorded: list[tuple[str, dict]] = []
    real = chat_module.record_diagnostic

    def record(level, feature, event_code, message, **fields):
        recorded.append((event_code, fields.get("metadata") or {}))
        return real(level, feature, event_code, message, **fields)

    monkeypatch.setattr(chat_module, "record_diagnostic", record)
    broker = ScanBroker()
    provider = ThinkingLongTurn(calls=24)
    store, service, prepared = _long_turn(tmp_path, provider, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO and request.tool_results
    ]
    let_go: set[str] = set()
    for earlier, later in zip(routing, routing[1:]):
        # The newest response always replays its reasoning.
        assert _thinking(later.tool_results[-1])
        # A thought once let go stays gone.
        assert not any(
            _thinking(result)
            for result in later.tool_results
            if result.call_id in let_go
        )
        newly = {
            result.call_id
            for result in later.tool_results[:-1]
            if not _thinking(result) and result.call_id not in let_go
        }
        if newly:
            # Only at a crossing, and only once every earlier result it
            # replays is a receipt.
            assert not _extends(earlier, later)
            assert all(_cleared(result) for result in later.tool_results[:-1])
        let_go |= newly
    assert let_go
    # Each crossing that lets reasoning go says how many responses it let go.
    drops = [
        metadata
        for event_code, metadata in recorded
        if event_code == "chat.tool_history.reasoning_dropped"
    ]
    assert drops
    assert all(metadata["count"] >= 1 for metadata in drops)
    # Each response is let go at most once.
    assert sum(metadata["count"] for metadata in drops) <= len(
        {
            result.response_group or result.call_id
            for request in routing
            for result in request.tool_results
        }
    )


def _batch_request(model: str) -> ModelRequest:
    """Two replayed responses; the earlier one has let its reasoning go.

    It keeps the stamp of the route and model that wrote it, as
    ``_budgeted_reasoning`` leaves it.
    """

    return ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="Read two files.")],
        tool_results=[
            _result(1, "group-1", {"provider_id": "provider", "model": model}),
            _result(2, "group-2", _state(model=model)),
        ],
    )


def test_routes_accept_earlier_steps_without_their_reasoning(monkeypatch):
    """DeepSeek V4 gets its empty placeholder; Claude keeps the last thinking."""

    openrouter = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="deepseek/deepseek-v4.1-flash",
            model_allowlist=["deepseek/deepseek-v4.1-flash"],
        )
    )
    payload = openrouter._payload(
        _batch_request("deepseek/deepseek-v4.1-flash"), "deepseek/deepseek-v4.1-flash"
    )
    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert assistants[0]["reasoning_details"] == []
    assert assistants[1]["reasoning_details"][0]["text"] == THOUGHT

    claude = "claude-sonnet-5"
    thinking = [{"type": "thinking", "thinking": THOUGHT, "signature": "SIG"}]
    sent: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": claude,
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )

    monkeypatch.setenv("NEBULA_REPLAY_ESTIMATE_KEY", "key")
    anthropic = AnthropicProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.ANTHROPIC,
            flavor=ProviderFlavor.ANTHROPIC,
            base_url="https://api.anthropic.com",
            default_model=claude,
            model_allowlist=[claude],
            api_key_env="NEBULA_REPLAY_ESTIMATE_KEY",
            capabilities=ModelCapabilities(tools=True),
        ),
        transport=httpx.MockTransport(answer),
    )
    request = _batch_request(claude)
    request.tool_results[1].reasoning_state = {
        "provider_id": "provider",
        "model": claude,
        "thinking_blocks": thinking,
    }
    asyncio.run(anthropic.complete(request))
    (payload,) = sent
    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert [block["type"] for block in assistants[0]["content"]] == ["tool_use"]
    assert assistants[1]["content"][: len(thinking)] == thinking


def test_an_earlier_step_keeps_its_call_signatures():
    """Gemini 3 validates the signature of every step in the current turn.

    Letting a step's reasoning go keeps its stamp, so the signature each of
    its calls came with still goes back, natively and on an OpenAI-compatible
    route, and never the sentinel for a call that has one.
    """

    from nebula.v3.chat import _budgeted_reasoning

    model = "gemini-3-flash"
    compatible_signature = {"google": {"thought_signature": "SIG-COMPAT"}}
    results = [
        _result(1, "group-1", _state(model=model)).model_copy(
            update={
                "provider_metadata": {
                    "thought_signature": "SIG-1",
                    "extra_content": compatible_signature,
                }
            }
        ),
        _result(2, "group-2", _state(model=model)),
    ]
    budgeted = _budgeted_reasoning(results, {"group-1", "group-2"})
    assert budgeted[0].reasoning_state == {"provider_id": "provider", "model": model}
    # The newest response keeps its reasoning whatever was let go.
    assert budgeted[1].reasoning_state == results[1].reasoning_state
    request = ModelRequest(
        model=model,
        messages=[ModelMessage(role="user", content="Read two files.")],
        tool_results=budgeted,
    )

    compatible = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.CUSTOM,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            default_model=model,
            model_allowlist=[model],
        )
    )._payload(request, model)
    calls = [
        call
        for message in compatible["messages"]
        if message["role"] == "assistant"
        for call in message["tool_calls"]
    ]
    assert calls[0]["extra_content"] == compatible_signature

    sent: list[dict] = []

    def answer(http: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "Done."}]},
                    }
                ]
            },
        )

    gemini = GeminiProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.GEMINI,
            flavor=ProviderFlavor.GEMINI,
            base_url="https://generativelanguage.googleapis.com",
            default_model=model,
            model_allowlist=[model],
            api_key_value="fake-test-value",
            capabilities=ModelCapabilities(tools=True),
        ),
        transport=httpx.MockTransport(answer),
    )
    asyncio.run(gemini.complete(request))
    (payload,) = sent
    signatures = [
        part.get("thoughtSignature")
        for content in payload["contents"]
        if content["role"] == "model"
        for part in content["parts"]
        if "functionCall" in part
    ]
    assert signatures[0] == "SIG-1"


def test_a_scan_turn_estimate_follows_the_wire():
    """The replay estimate stays within the bytes the route is sent."""

    results = [
        _result(index, f"group-{index}", _state(thought=f"thought {index} " * 50))
        for index in range(8)
    ]
    request = _request(results)
    wire = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            flavor=ProviderFlavor.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            default_model="model-a",
            model_allowlist=["model-a"],
        )
    )._payload(request, "model-a")
    sent = estimate_tokens(
        json.dumps(
            [m for m in wire["messages"] if m["role"] in {"assistant", "tool"}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    counted = estimate_model_request_parts(request).tool_results
    # The route is sent each thought twice and renders it once: the estimate
    # counts it once, well under the raw bytes, never below the thoughts'
    # own text.
    assert counted < sent
    assert counted > sum(
        estimate_tokens(f"thought {index} " * 50) for index in range(8)
    )
    assert estimate_model_request(request) > counted


def test_a_capacity_check_counts_tool_history_as_sent(tmp_path):
    """Calibration relaxes the text of a request, not its JSON.

    A conversation calibrated to 0.6 (DeepSeek counts prose that much below
    Core's byte estimate) let a request through while 0.8 of its raw estimate
    fit the capacity. Replayed tool results are JSON, which DeepSeek counts at
    1.02 of their estimate, so a request of them would overfill the window.
    """

    from nebula.v3.chat import ChatService
    from nebula.v3.context import calibrated_request_estimate, resolve_context_limits
    from nebula.v3.domain import ProviderProfile

    profile = ProviderProfile(
        id="provider",
        name="Provider",
        provider_type="vllm",
        is_local=True,
        model_allowlist=["model-a"],
        capabilities={"streaming": True, "tool_calling": True},
        metadata={"options": {"context_window": 16_384}},
    )
    capacity = resolve_context_limits(
        profile, model="model-a", required_parameters={"tools"}
    ).input_capacity
    # One result whose JSON alone is 1.1 times the capacity.
    result = _result(0, "group-0").model_copy(
        update={"output": {"text": "k" * (capacity * 33 // 10)}}
    )
    heavy = _request([result])
    raw = estimate_model_request(heavy)
    assert 0.8 * raw <= capacity < raw
    assert calibrated_request_estimate(heavy, 0.6, hard=True) > capacity
    assert not ChatService._fits_request_capacity(profile, heavy, 0.6)
    # Text alone is still relaxed as before.
    prose = ModelRequest(
        model="model-a",
        messages=[ModelMessage(role="user", content="y" * (capacity * 3 * 6 // 5))],
    )
    assert ChatService._fits_request_capacity(profile, prose, 0.8)


def test_a_capacity_check_scales_tool_history_up_but_never_down():
    from nebula.v3.context import calibrated_request_estimate

    request = _request([_result(0, "group-0")])
    parts = estimate_model_request_parts(request)
    text = parts.instructions + parts.conversation
    structured = parts.tool_schemas + parts.tool_results
    assert calibrated_request_estimate(request, None, hard=True) == text + structured
    assert (
        calibrated_request_estimate(request, 0.6, hard=True)
        == -(-text * 8 // 10) + structured
    )
    # A provider that counts more than Core (Claude) scales both up.
    assert calibrated_request_estimate(request, 1.2, hard=True) >= int(
        1.2 * (text + structured)
    )


def test_a_prose_calibrated_tool_turn_clears_at_its_target(tmp_path, monkeypatch):
    """Clearing counts the request as the capacity check does.

    A conversation whose calibration was learnt from prose (DeepSeek, 0.6)
    let clearing measure a tool turn at 0.6 of its estimate, while the
    capacity check counts its JSON whole: the requests never crossed their
    target and every step failed the capacity check instead, folding its
    history into the checkpoint (24 folds and 3 conversation compactions on
    a 9K window, where clearing alone had done).
    """

    import nebula.v3.chat as chat_module
    from nebula.v3.context import calibrated_request_estimate

    recorded: list[str] = []
    real = chat_module.record_diagnostic

    def record(level, feature, event_code, message, **fields):
        recorded.append(event_code)
        return real(level, feature, event_code, message, **fields)

    monkeypatch.setattr(chat_module, "record_diagnostic", record)
    broker = ScanBroker()
    provider = LongTurnProvider(calls=24)
    store, service, prepared = _long_turn(tmp_path, provider, broker)
    prepared.estimate_calibration = 0.6

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert "chat.tool_history.cleared" in recorded
    assert "chat.tool_history.folded" not in recorded
    assert "chat.context.midturn_compacted" not in recorded
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO and request.tool_results
    ]
    assert len(routing) >= 24
    for request in routing:
        assert calibrated_request_estimate(request, 0.6, hard=True) <= _capacity(
            prepared, request
        )
