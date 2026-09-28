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
from nebula.v3.artifacts import ArtifactStore
from nebula.v3 import context as context_module
from nebula.v3.context import estimate_model_request, resolve_context_limits
from nebula.v3.domain import (
    ChatTurn,
    ChatTurnStatus,
    ProviderProfile,
    RiskClass,
    ToolCall as StoredToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelRequest,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderKind,
    ToolCall,
    ToolChoice,
)
from nebula.v3.runtime_platform import output_retrieval_components
from nebula.v3.tool_results import (
    MAX_EXCERPT_BYTES,
    NetworkPortObservation,
    ToolArtifactRef,
    ToolResultReceipt,
    ToolResultStatus,
    ToolOutputService,
)
from nebula.v3.tools import ToolExecutionResult, ToolSpec
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


def test_tool_output_is_a_receipt_until_explicitly_read(tmp_path):
    """A lookup excerpt is visible once; ordinary output is a receipt from start."""

    store, service, prepared, _ = _prepared(tmp_path, [], ScanBroker())
    turn = store.get(ChatTurn, "turn")
    artifact_id = "artifact-1"
    entries = [
        {
            "step": 0,
            "model_call_id": "call-command",
            "tool_call_id": "tool-command",
            "response_group": "command",
            "name": "safe_read",
            "arguments": {"value": "one"},
            "status": "complete",
            "trusted_result": True,
            "provider_result": json.dumps({"output": "large command output " * 200}),
            "result_summary": "Command completed",
            "result_artifact_id": artifact_id,
        }
    ]

    def next_request():
        current = turn.model_copy(update={"tool_history": list(entries)})
        return service._with_tool_history(prepared, current, prepared.model_request)

    first = next_request()
    assert first.tool_results[0].output["output_cleared"] is True
    assert first.tool_results[0].output["artifact_ids"] == [artifact_id]
    assert "large command output" not in json.dumps(first.tool_results[0].output)

    entries.append(
        {
            "step": 1,
            "model_call_id": "call-read",
            "tool_call_id": "tool-read",
            "response_group": "read",
            "name": "tool_output.read",
            "arguments": {"artifact_id": artifact_id},
            "status": "complete",
            "trusted_result": True,
            "provider_result": json.dumps({"lines": [{"text": "requested excerpt"}]}),
            "result_summary": "Result fields: lines",
        }
    )
    second = next_request()
    assert second.tool_results[0].output["output_cleared"] is True
    assert second.tool_results[1].output["lines"][0]["text"] == "requested excerpt"

    entries.append(
        {
            "step": 2,
            "model_call_id": "call-later",
            "tool_call_id": "tool-later",
            "response_group": "later",
            "name": "safe_read",
            "arguments": {"value": "two"},
            "status": "complete",
            "trusted_result": True,
            "provider_result": json.dumps({"output": "later output " * 200}),
            "result_summary": "Later command completed",
            "result_artifact_id": "artifact-2",
        }
    )
    third = next_request()
    assert [result.output["output_cleared"] for result in third.tool_results] == [
        True,
        True,
        True,
    ]
    assert third.tool_results[1].output["artifact_ids"] == [artifact_id]
    assert "requested excerpt" not in json.dumps(third.tool_results[1].output)
    assert "requested excerpt" in entries[1]["provider_result"]

    entries.append(
        {
            "step": 3,
            "model_call_id": "call-failed",
            "tool_call_id": "tool-failed",
            "response_group": "failed",
            "name": "safe_read",
            "arguments": {"value": "three"},
            "status": "failed",
            "trusted_result": True,
            "provider_result": json.dumps({"detail": "Permission denied"}),
            "result_summary": "Permission denied",
            "result_artifact_id": "artifact-3",
        }
    )
    failure = next_request()
    assert failure.tool_results[-1].output["detail"] == "Permission denied"
    assert failure.tool_results[-1].is_error is True

    entries.append(
        {
            "step": 4,
            "model_call_id": "call-after-failure",
            "tool_call_id": "tool-after-failure",
            "response_group": "after-failure",
            "name": "safe_read",
            "arguments": {"value": "four"},
            "status": "complete",
            "trusted_result": True,
            "provider_result": json.dumps({"output": "final output"}),
            "result_summary": "Final command completed",
            "result_artifact_id": "artifact-4",
        }
    )
    later = next_request()
    assert later.tool_results[-2].output["output_cleared"] is True
    assert later.tool_results[-2].output["summary"] == "Permission denied"


def test_result_without_command_artifact_gets_readable_receipt(tmp_path):
    """Graph and other non-command results are persisted before compact replay."""

    store, _service, prepared, _ = _prepared(tmp_path, [], ScanBroker())
    artifacts = ArtifactStore(tmp_path / "result-artifacts")
    service = chat_module.ChatService(store, artifact_store=artifacts)
    store.create(
        StoredToolCall(
            id="graph-call",
            engagement_id="project",
            run_id="turn",
            origin=ToolCallOrigin.CHAT,
            chat_session_id="session",
            tool_name="model.search",
            risk_class=RiskClass.PASSIVE,
            status=ToolCallStatus.COMPLETE,
        )
    )
    result = {"objects": [{"id": "host-1", "name": "example.test"}], "total": 1}
    spec = ToolSpec(
        name="model.search",
        description="Search the project graph.",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        risk_class=RiskClass.PASSIVE,
    )
    fields, waiting = service._tool_result_entry(
        ToolExecutionResult(output=result), spec=spec, call_id="graph-call"
    )
    assert not waiting
    artifact_id = fields["result_artifact_id"]
    assert isinstance(artifact_id, str)
    repeated, _ = service._tool_result_entry(
        ToolExecutionResult(output=result), spec=spec, call_id="graph-call"
    )
    assert repeated["result_artifact_id"] == artifact_id
    assert len(store.list_tool_call_artifacts("project", "graph-call")) == 1
    capabilities = output_retrieval_components(
        store, artifacts, prepared.tool_components
    )
    assert {"tool_output.search", "tool_output.read"} <= set(capabilities.specs)
    assert "workspace.read" not in capabilities.specs
    receipt = service._with_tool_history(
        prepared,
        store.get(ChatTurn, "turn").model_copy(
            update={
                "tool_history": [{
                    "model_call_id": "model-call",
                    "tool_call_id": "graph-call",
                    "response_group": "graph",
                    "name": "model.search",
                    "arguments": {"query": "host"},
                    **fields,
                }]
            }
        ),
        prepared.model_request,
    ).tool_results[0].output
    assert receipt["output_cleared"] is True
    assert receipt["artifact_ids"] == [artifact_id]
    assert "example.test" not in json.dumps(receipt)
    original = ToolOutputService(store, artifacts).read(
        engagement_id="project", owner_id="turn", artifact_id=artifact_id
    )
    assert "example.test" in original["lines"][0]["text"]


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
    # Every call stays in the protocol until it is checkpointed, but ordinary
    # outputs are receipts even in the newest group.
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
        scans = [result for result in results if result.name == "safe_read"]
        assert scans and all(_cleared(result) for result in scans)
        first = scans[0].output
        first_entry = next(
            entry
            for entry in turn.tool_history
            if entry["model_call_id"] == scans[0].call_id
        )
        assert first["tool_call_id"] == first_entry["tool_call_id"]
        assert first["artifact_ids"] == [first_entry["result_artifact_id"]]
        assert first["summary"] == first_entry["result_summary"]
        assert "tool_output.read" in first["note"]
        assert "tool_output.search" in first["note"]
        assert not scans[0].is_error


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


def test_default_receipts_avoid_context_rejection_after_tool_steps(tmp_path):
    """Two large durable results fit a smaller server when sent as receipts."""

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
    assert not rejected
    later = [
        body
        for body in accepted
        if sum(message["role"] == "tool" for message in body["messages"]) == 2
    ]
    assert [body.get("tool_choice") for body in later] == [None, "none"]
    for body in later:
        first, second = [
            json.loads(message["content"])
            for message in body["messages"]
            if message["role"] == "tool"
        ]
        assert first["output_cleared"] is True
        assert first["artifact_ids"] == ["artifact-1"]
        assert second["output_cleared"] is True
        assert second["artifact_ids"] == ["artifact-2"]
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


def _large_window_turn(tmp_path, provider, broker, **options):
    """A turn whose model publishes a 1M-token window (no configured cap)."""

    store, service, prepared, _ = _prepared(tmp_path, [], broker, max_tool_calls=None)
    profile = store.get(ProviderProfile, prepared.provider_profile.id)
    descriptors = [
        {"id": "model-a", "context_window": 1_000_000, "max_output_tokens": 32_000}
    ]
    prepared.provider_profile = store.update(
        ProviderProfile,
        profile.id,
        {
            "metadata": {
                **profile.metadata,
                "model_descriptors": descriptors,
                "options": options,
            }
        },
        expected_revision=profile.revision,
    )
    prepared.provider = provider
    return store, service, prepared


def test_a_large_window_clears_tool_results_at_the_working_ceiling(
    tmp_path, monkeypatch
):
    """Stream X: context rot applies to tool results too.

    A 1M-token model would carry a tool turn to 75% of its window before
    clearing anything. The working ceiling (held small here so the turn stays
    short) sizes the target, and a crossing clears down from it; the hard
    capacity is still the model's.
    """

    monkeypatch.setattr(context_module, "WORKING_CONTEXT_CEILING", 12_000)
    broker = ScanBroker()
    provider = LongTurnProvider(calls=20)
    store, service, prepared = _large_window_turn(tmp_path, provider, broker)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    limits = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    )
    assert (limits.binding_limit, limits.target_input_tokens) == ("ceiling", 12_000)
    assert limits.input_capacity > 900_000
    # Results were cleared to stay near the ceiling, far below the window.
    assert any(_cleared(r) for req in routing for r in req.tool_results)
    assert max(estimate_model_request(req) for req in routing) < 2 * 12_000


def test_a_configured_window_above_the_ceiling_still_uses_default_receipts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(context_module, "WORKING_CONTEXT_CEILING", 12_000)
    broker = ScanBroker()
    provider = LongTurnProvider(calls=20)
    store, service, prepared = _large_window_turn(
        tmp_path, provider, broker, context_window=1_000_000
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    # A larger configured window no longer opts back into repeated output.
    assert all(_cleared(r) for req in routing for r in req.tool_results)
    assert max(estimate_model_request(req) for req in routing) < 2 * 12_000
