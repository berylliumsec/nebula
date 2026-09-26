"""A long tool turn clears its oldest results instead of failing at the window.

Every routing step re-sends the results of the steps before it, so a turn that
keeps calling tools outgrows a finite context window. The turn keeps every call
and its result, cuts the oldest results to a receipt that says where the full
output is, and finishes: from the results it has when even that does not fit,
and after one retry with more cleared when the provider rejects the context.
"""

import asyncio
import json

import httpx
import pytest

from nebula.v3 import chat as chat_module
from nebula.v3.context import estimate_model_request, resolve_context_limits
from nebula.v3.domain import ChatTurn, ChatTurnStatus, ProviderProfile
from nebula.v3.providers import (
    ModelCapabilities,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderKind,
    ToolCall,
    ToolChoice,
)
from nebula.v3.tool_results import (
    MAX_EXCERPT_BYTES,
    NetworkPortObservation,
    ToolArtifactRef,
    ToolResultReceipt,
    ToolResultStatus,
)
from nebula.v3.tools import ToolExecutionResult
from tests.v3.test_chat_tool_loop import ScriptedProvider, _prepared, _response

ANSWER = "Every scanned host is summarised above."
WINDOW = 32_768


class ScanBroker:
    """Answers every call with a ~7 KB scan receipt backed by an artifact."""

    def __init__(self) -> None:
        self.calls = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls.append(invocation)
        step = len(self.calls)
        receipt = ToolResultReceipt(
            tool_call_id=f"scan-{step}",
            tool_name=invocation.tool_name,
            tool_version="1",
            status=ToolResultStatus.COMPLETED,
            exit_code=0,
            summary=f"Scan {step} finished.",
            observations=[
                NetworkPortObservation(
                    protocol="tcp",
                    port=1_000 + index,
                    state="open",
                    service=f"service-{step}-{index}-" + "b" * 40,
                )
                for index in range(46)
            ],
            artifacts=[
                ToolArtifactRef(
                    artifact_id=f"artifact-{step}",
                    kind="stdout",
                    media_type="text/plain",
                    byte_count=70_000,
                    observed_byte_count=70_000,
                    sha256="0" * 64,
                    searchable=True,
                )
            ],
        )
        return ToolExecutionResult(
            output={}, receipt=receipt, result_artifact_id=f"artifact-{step}"
        )


class LongTurnProvider(ScriptedProvider):
    """Keeps calling the tool until ``calls`` ran, then finishes and answers."""

    def __init__(self, calls: int) -> None:
        super().__init__([])
        self.calls = calls
        self.issued = 0
        self.finished = False

    async def complete(self, request: ModelRequest):
        self.requests.append(request)
        if request.metadata.get("operation"):
            return _response(text="Scan")
        if request.tool_choice == ToolChoice.NONE:
            return _response(text=ANSWER)
        if self.issued < self.calls:
            self.issued += 1
            return _response(
                calls=[
                    ToolCall(
                        id=f"call-{self.issued}",
                        name="safe_read",
                        arguments={"value": str(self.issued)},
                    )
                ]
            )
        self.finished = True
        return _response(
            calls=[ToolCall(id="finish", name="finish_response", arguments={})]
        )


def _long_turn(tmp_path, provider, broker):
    store, service, prepared, _ = _prepared(tmp_path, [], broker, max_tool_calls=None)
    profile = store.get(ProviderProfile, prepared.provider_profile.id)
    prepared.provider_profile = store.update(
        ProviderProfile,
        profile.id,
        {"metadata": {**profile.metadata, "options": {"context_window": WINDOW}}},
        expected_revision=profile.revision,
    )
    prepared.provider = provider
    return store, service, prepared


def _capacity(prepared, request: ModelRequest) -> int:
    return resolve_context_limits(
        prepared.provider_profile,
        model=request.model,
        requested_output_tokens=request.max_output_tokens,
        required_parameters={"tools"} if request.tools else set(),
    ).input_capacity


def _turn_requests(provider) -> list[ModelRequest]:
    return [
        request
        for request in provider.requests
        if not request.metadata.get("operation")
    ]


def _cleared(result) -> bool:
    return isinstance(result.output, dict) and result.output.get("output_cleared")


def _checkpointed(request: ModelRequest) -> bool:
    """Whether the tool-history checkpoint rides with the last user message."""

    last = request.messages[-1]
    return (
        last.role == "user"
        and "EARLIER TOOL HISTORY CHECKPOINT" in str(last.content)
        and "EARLIER TOOL HISTORY CHECKPOINT" not in (request.instructions or "")
    )


def test_long_tool_turn_clears_old_results_instead_of_failing(tmp_path):
    """HIST-3: 20 × 7 KB results on a 32K window used to fail after 13 calls."""

    broker = ScanBroker()
    provider = LongTurnProvider(calls=20)
    store, service, prepared = _long_turn(tmp_path, provider, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    # No step ran twice and none was skipped.
    assert [call.arguments["value"] for call in broker.calls] == [
        str(step) for step in range(1, 21)
    ]
    # Each result is a whole ~7 KB receipt, under the 8 KiB replay bound.
    for entry in turn.tool_history:
        assert 7_000 < len(entry["provider_result"].encode()) < MAX_EXCERPT_BYTES
    requests = _turn_requests(provider)
    for request in requests:
        assert estimate_model_request(request) <= _capacity(prepared, request)
    routing = [r for r in requests if r.tool_choice == ToolChoice.AUTO]
    synthesis = [r for r in requests if r.tool_choice == ToolChoice.NONE]
    assert len(routing) == 21 and len(synthesis) == 1
    # Before checkpointing every prior call is replayed. Once the deterministic
    # checkpoint is present, the newest calls after it remain full: at least
    # the latest eight provider groups. The routing instructions never change.
    assert len({request.instructions for request in routing}) == 1
    for step, request in enumerate(routing):
        expected = [f"call-{index}" for index in range(1, step + 1)]
        replayed = [result.call_id for result in request.tool_results]
        assert replayed == expected[len(expected) - len(replayed) :]
        if replayed != expected:
            assert len(replayed) >= 8
            assert _checkpointed(request)
    for request in (routing[-1], synthesis[0]):
        results = request.tool_results
        cleared = [result for result in results if _cleared(result)]
        # Checkpointing can make receipt clearing unnecessary. Otherwise the
        # oldest replayed results are the cleared ones and the newest are whole.
        if not cleared:
            assert _checkpointed(request)
            assert results[-1].output["observations"]
            continue
        assert len(cleared) < len(results)
        assert results[: len(cleared)] == cleared
        assert results[-1].output["observations"]
        first = cleared[0].output
        first_entry = next(
            entry
            for entry in turn.tool_history
            if entry["model_call_id"] == results[0].call_id
        )
        assert first["tool_call_id"] == first_entry["tool_call_id"]
        assert first["artifact_ids"] == [first_entry["result_artifact_id"]]
        assert first["summary"] == first_entry["result_summary"]
        assert "tool_output.read" in first["note"]
        assert "tool_output.search" in first["note"]
        assert not cleared[0].is_error


def test_routing_that_cannot_fit_answers_from_results_instead_of_failing(
    tmp_path, monkeypatch
):
    """Routing-only overhead fills the window: the turn answers from its results.

    Once even cleared results no longer fit, the replayed steps fold into the
    checkpoint and routing goes on from its receipts, until even that no
    longer fits; the answer then carries every step, replayed or folded.
    """

    broker = ScanBroker()
    provider = LongTurnProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, broker)
    capacity = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    ).input_capacity
    # Routing instructions (a large on-demand catalog, say) leave room for the
    # first step and a few cleared results, never for a whole one.
    monkeypatch.setattr(
        chat_module,
        "_CHAT_TOOL_INSTRUCTIONS",
        chat_module._CHAT_TOOL_INSTRUCTIONS + "R" * ((capacity - 1_500) * 3),
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    # Core stopped routing; the model never had to finish it.
    assert provider.finished is False
    assert 2 <= len(broker.calls) < 30
    requests = _turn_requests(provider)
    for request in requests:
        assert estimate_model_request(request) <= _capacity(prepared, request)
    (synthesis,) = [r for r in requests if r.tool_choice == ToolChoice.NONE]
    replayed = [int(r.call_id.removeprefix("call-")) for r in synthesis.tool_results]
    assert replayed == list(
        range(len(broker.calls) - len(replayed) + 1, len(broker.calls) + 1)
    )
    folded = set(range(1, len(broker.calls) + 1)) - set(replayed)
    if folded:
        # No step is left out: what is not replayed is in the checkpoint.
        assert _checkpointed(synthesis)
        block = str(synthesis.messages[-1].content).split(
            "EARLIER TOOL HISTORY CHECKPOINT", 1
        )[1]
        checkpoint = json.loads(block.split("\n", 1)[1])
        covered = {
            step + 1
            for first, last in checkpoint["covered_steps"]
            for step in range(first, last + 1)
        }
        assert folded <= covered


_CONTEXT_REJECTION = {
    "error": {
        "message": (
            "This model's maximum context length is 16384 tokens. However, "
            "your messages resulted in 17012 tokens."
        ),
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
    }
}


def _wire_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": "gen-1",
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def _text_stream(text: str) -> httpx.Response:
    chunks = [
        {"id": "gen-s", "model": "model-a", "choices": [{"index": 0, "delta": delta}]}
        for delta in ({"content": text}, {})
    ]
    chunks[-1]["choices"][0]["finish_reason"] = "stop"
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def test_context_rejection_after_a_tool_step_clears_results_and_retries(tmp_path):
    """ROUTE-13: a server smaller than its configured window rejects step 3.

    The server holds one whole result. Routing step 3 carries two, is
    rejected with a 400, and is sent once more with the older result cleared.
    That result stays cleared for the rest of the turn, so the synthesis is
    not rejected again. No tool runs twice.
    """

    accepted: list[dict] = []
    rejected: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tool_messages = [
            message for message in body["messages"] if message["role"] == "tool"
        ]
        if sum(len(message["content"]) for message in tool_messages) > 9_000:
            rejected.append(body)
            return httpx.Response(400, json=_CONTEXT_REJECTION)
        accepted.append(body)
        if not body.get("tools"):
            # Naming the conversation, if Core asks.
            return httpx.Response(
                200,
                json={
                    "id": "gen-n",
                    "model": "model-a",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Scan"},
                        }
                    ],
                },
            )
        if body.get("tool_choice") == "none":
            return _text_stream(ANSWER)
        if len(tool_messages) < 2:
            step = len(tool_messages) + 1
            return httpx.Response(
                200, json=_wire_call(f"call-{step}", "safe_read", {"value": str(step)})
            )
        return httpx.Response(200, json=_wire_call("finish", "finish_response", {}))

    broker = ScanBroker()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            id="provider",
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://127.0.0.1:8000/v1",
            default_model="model-a",
            model_allowlist=["model-a"],
            local=True,
            capabilities=ModelCapabilities(
                streaming=True, tools=True, strict_tools=True
            ),
            options={"retry_backoff_seconds": 0},
        ),
        transport=httpx.MockTransport(handler),
    )
    store, service, prepared = _long_turn(tmp_path, provider, broker)

    async def scenario():
        return [event async for event in service.stream(prepared)]

    events = asyncio.run(scenario())

    done = [payload for name, payload in events if name == "done"]
    assert done[-1]["message"]["content"] == ANSWER
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    assert [call.arguments["value"] for call in broker.calls] == ["1", "2"]
    # Routing step 3 was rejected once and retried; the synthesis kept the
    # result that retry cleared. OpenAI-compatible automatic routing omits
    # tool_choice on the wire.
    assert [body.get("tool_choice") for body in rejected] == [None]
    retried = [
        body
        for body in accepted
        if sum(message["role"] == "tool" for message in body["messages"]) == 2
    ]
    assert [body.get("tool_choice") for body in retried] == [None, "none"]
    for body in retried:
        first, second = [
            json.loads(message["content"])
            for message in body["messages"]
            if message["role"] == "tool"
        ]
        assert first["output_cleared"] is True
        assert first["artifact_ids"] == ["artifact-1"]
        assert second["observations"]
        # The calls are replayed unchanged, under their own ids.
        calls = [
            call["id"]
            for message in body["messages"]
            if message["role"] == "assistant"
            for call in message.get("tool_calls") or []
        ]
        assert calls == ["call-1", "call-2"]


def test_context_rejection_with_nothing_left_to_clear_reads_the_current_turn(
    tmp_path,
):
    """ROUTE-13: the no-repeat guard read a turn that routing never refreshed."""

    from nebula.v3.chat import ChatConfigurationError
    from nebula.v3.providers import ProviderContextLengthError
    from tests.v3.test_chat_tool_loop import RecordingBroker

    class Rejecting(ScriptedProvider):
        async def complete(self, request):
            item = await super().complete(request)
            if isinstance(item, Exception):
                raise item
            return item

    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(tmp_path, [], broker)
    prepared.provider = Rejecting(
        [
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ]
            ),
            ProviderContextLengthError("maximum context length exceeded"),
        ]
    )

    # The one result is already smaller than its receipt: nothing can be
    # cleared, and the turn is refused as one whose tools already ran, not
    # sent to rebuild a conversation it has no source for.
    with pytest.raises(ChatConfigurationError, match="after tool routing began"):
        asyncio.run(service.complete(prepared))
    assert len(broker.calls) == 1
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.FAILED


def _extends(earlier: ModelRequest, later: ModelRequest) -> bool:
    """Whether ``later`` is ``earlier`` plus newer steps: a prefix cache hit."""

    return (
        later.instructions == earlier.instructions
        and later.messages == earlier.messages
        and later.tool_results[: len(earlier.tool_results)] == earlier.tool_results
    )


def test_cleared_results_stay_cleared_so_the_prefix_changes_once_per_crossing(
    tmp_path,
):
    """F3: past its target a turn cleared one more result on nearly every step.

    Clearing the fewest results that fit left each request just under the
    target, so the next step crossed it again and changed an earlier result
    (and advanced the checkpoint), missing the provider's prefix cache on
    almost every request. A crossing now clears down to a watermark well
    below the target, and a result once cleared stays cleared, so earlier
    bytes change once per crossing.
    """

    broker = ScanBroker()
    provider = LongTurnProvider(calls=40)
    store, service, prepared = _long_turn(tmp_path, provider, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert len(broker.calls) == 40
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    assert len(routing) == 41
    changes = 0
    cleared_before: set[str] = set()
    for earlier, later in zip(routing, routing[1:]):
        cleared = {result.call_id for result in later.tool_results if _cleared(result)}
        replayed = {result.call_id for result in later.tool_results}
        # A result once cleared is never replayed whole again.
        assert not (cleared_before & replayed) - cleared
        if not _extends(earlier, later):
            changes += 1
            # Only a crossing changes earlier bytes, and it takes the request
            # well below the target.
            limits = resolve_context_limits(
                prepared.provider_profile,
                model=later.model,
                requested_output_tokens=later.max_output_tokens,
                required_parameters={"tools"},
            )
            assert estimate_model_request(later) <= limits.target_input_tokens - 6_000
        cleared_before |= cleared
    # Each crossing buys several steps: the old behaviour changed the prefix
    # on 33 of these 40 steps.
    assert 1 <= changes <= len(routing) // 4
