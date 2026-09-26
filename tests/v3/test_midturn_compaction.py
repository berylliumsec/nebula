"""A running tool turn compacts its conversation instead of stopping or failing.

A turn's tool history grows with every step. Once even its older results are
cleared the request can still outgrow the window: the conversation ahead of the
history is then compacted, as it would be between turns, and the turn goes on.
The ledger, its checkpoint and replay are sent as they were, and no tool runs
again. The answer always gets the room it needs: only a request whose current
message, instructions and checkpoint cannot fit fails, and it says so.
"""

import asyncio
import json
from pathlib import Path

import pytest

from nebula.v3 import chat as chat_module
from nebula.v3.chat import ChatConfigurationError, ChatService, PreparedChat
from nebula.v3.chat_snapshot_parts import resolve_request_snapshot
from nebula.v3.context import estimate_model_request, resolve_context_limits
from nebula.v3.conversation_search import CONVERSATION_SEARCH_TOOL_NAME
from nebula.v3.domain import (
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ContextOwnerType,
    ContextSnapshot,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
)
from nebula.v3.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderContextLengthError,
    ToolCall,
    ToolChoice,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ParallelismPolicy, ToolExecutionResult, ToolSpec
from tests.v3.test_chat_tool_loop import ScriptedProvider, _response
from tests.v3.test_in_turn_context_pruning import ScanBroker

ANSWER = "Every host checked; the findings are above."
MEMORY = "EARLIER CONVERSATION, COMPACTED BY NEBULA"
WINDOW = 16_384
SPEC = ToolSpec(
    name="safe_read",
    description="Return one bounded value.",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "additionalProperties": True},
    risk_class=RiskClass.LOCAL_READ,
    parallelism=ParallelismPolicy.SERIAL,
)


class ProbeBroker:
    """Answers every call with a ~2 KB result; records what ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        value = invocation.arguments["value"]
        self.calls.append(value)
        return ToolExecutionResult(
            output={
                "value": value,
                "detail": f"Probe {value} answered: " + "observed " * 220,
            }
        )


class TurnProvider(ScriptedProvider):
    """Calls ``safe_read`` ``calls`` times, then answers; compacts on request."""

    def __init__(self, calls: int) -> None:
        super().__init__([])
        self.calls = calls
        self.issued = 0
        self.compactions: list[ModelRequest] = []

    async def turn_response(self, request: ModelRequest) -> ModelResponse:
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
        return _response(
            calls=[ToolCall(id="finish", name="finish_response", arguments={})]
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        operation = request.metadata.get("operation")
        if operation == "context_compaction":
            self.compactions.append(request)
            return ModelResponse(
                provider_id="provider",
                model="model-a",
                text=json.dumps({"summary": "The operator asked for host checks."}),
                usage=ModelUsage(input_tokens=40, output_tokens=10, total_tokens=50),
                finish_reason="stop",
            )
        self.requests.append(request)
        if operation:
            return _response(text="Checks")
        return await self.turn_response(request)


def _history(count: int) -> list[str]:
    return [
        f"Earlier exchange {index}: " + ("context detail " * 150)
        for index in range(1, count + 1)
    ]


def _turn(
    tmp_path: Path,
    provider: TurnProvider,
    broker: ProbeBroker,
    *,
    history: int,
    window: int = WINDOW,
    goal: bool = False,
    specs: list[ToolSpec] | None = None,
) -> tuple[NebulaStore, ChatService, PreparedChat]:
    """A tool turn whose conversation was sent whole when it started."""

    store = NebulaStore(tmp_path / "midturn.db")
    project = store.create(Engagement(id="project", name="Mid-turn"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
            metadata={"options": {"context_window": window}},
        )
    )
    contents = [*_history(history), "Check every host."]
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=project.id,
            title="Mid-turn",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={
                "message_count": len(contents),
                "last_sequence": len(contents),
                "initial_title_state": "generated",
            },
        )
    )
    stored = store.create_many(
        [
            ChatMessage(
                id=f"message-{sequence:02d}",
                engagement_id=project.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER if sequence % 2 else ChatRole.ASSISTANT,
                content=content,
            )
            for sequence, content in enumerate(contents, start=1)
        ]
    )
    if goal:
        store.create(
            ChatGoal(
                id="goal",
                engagement_id=project.id,
                session_id=session.id,
                objective="Check every host.",
                completion_criteria=["Every host is checked."],
                status=ChatGoalStatus.RUNNING,
                token_budget=1_000_000,
            )
        )
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id=project.id,
            session_id=session.id,
            goal_id="goal" if goal else None,
            provider_profile_id=profile.id,
            model="model-a",
            tools_enabled=True,
            max_tool_calls=None,
        )
    )
    prepared = PreparedChat(
        provider=provider,
        provider_profile=profile,
        model_request=ModelRequest(
            model="model-a",
            messages=[
                ModelMessage(role=message.role.value, content=message.content)
                for message in stored
            ],
            # As prepare sizes it for the model.
            max_output_tokens=resolve_context_limits(
                profile, model="model-a", required_parameters={"tools"}
            ).max_output_tokens,
        ),
        resolved_model="model-a",
        citations=[],
        engagement_id=project.id,
        session=session,
        pending_session=None,
        stored_messages=list(stored),
        new_messages=[],
        tools_enabled=True,
        tool_components=RuntimeToolComponents(
            broker=broker,
            scope=ScopePolicy(engagement_id=project.id),
            workspace=tmp_path,
            specs={spec.name: spec for spec in specs or [SPEC]},
            runtime_digest="midturn",
        ),
        turn=turn,
        inputs_persisted=True,
    )
    return store, ChatService(store, worker_id="worker"), prepared


def _capacity(prepared: PreparedChat, request: ModelRequest) -> int:
    return resolve_context_limits(
        prepared.provider_profile,
        model=request.model,
        requested_output_tokens=request.max_output_tokens,
        required_parameters={"tools"} if request.tools else set(),
    ).input_capacity


def _turn_requests(provider: TurnProvider) -> list[ModelRequest]:
    return [r for r in provider.requests if not r.metadata.get("operation")]


def _compacted(request: ModelRequest) -> bool:
    return MEMORY in str(request.messages[0].content)


def test_a_turn_that_outgrows_the_window_compacts_its_conversation_and_goes_on(
    tmp_path,
):
    """F12: routing stopped after two calls once the history filled the window.

    The conversation (ten earlier messages) was sent whole when the turn
    started. After two results, even with every result cleared, routing no
    longer fitted, so the turn answered from two of the thirty probes it
    meant to run. Now the conversation is compacted and routing goes on.
    """

    broker = ProbeBroker()
    provider = TurnProvider(calls=30)
    store, service, prepared = _turn(tmp_path, provider, broker, history=10)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    # Every probe ran, once, in order.
    assert broker.calls == [str(step) for step in range(1, 31)]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    requests = _turn_requests(provider)
    for request in requests:
        assert estimate_model_request(request) <= _capacity(prepared, request)
    routing = [r for r in requests if r.tool_choice == ToolChoice.AUTO]
    assert len(routing) == 31
    first = next(index for index, r in enumerate(routing) if _compacted(r))
    assert first >= 1
    assert not any(_compacted(r) for r in routing[:first])
    assert all(_compacted(r) for r in routing[first:])
    # The tool history is replayed as it was: the same calls, in order.
    before, after = routing[first - 1], routing[first]
    assert [r.call_id for r in after.tool_results][: len(before.tool_results)] == [
        r.call_id for r in before.tool_results
    ]
    # No message is left out: the snapshot covers exactly the messages the
    # request no longer carries verbatim.
    (snapshot,) = store.find_entities(
        ContextSnapshot,
        {"owner_type": ContextOwnerType.CHAT_SESSION.value, "owner_id": "session"},
    )
    covered = snapshot.compacted_through
    assert len(after.messages) == 11 - covered
    assert str(after.messages[-1].content).endswith("Check every host.")
    # The memory is the same bytes on every request after it.
    assert (
        len(
            {
                str(r.messages[0].content).split("END OF EARLIER CONVERSATION")[0]
                for r in routing[first:]
            }
        )
        == 1
    )
    # Older messages left the request, so the model can search them now, and
    # a resumed turn keeps the compacted conversation and the tool.
    assert all(
        CONVERSATION_SEARCH_TOOL_NAME in {tool.name for tool in r.tools}
        for r in routing[first:]
    )
    snapshot_values = resolve_request_snapshot(
        store, turn.request_snapshot, session_id="session"
    )
    assert snapshot_values["conversation_search"] is True
    assert snapshot_values["midturn_compactions"] == 1
    assert MEMORY in json.dumps(snapshot_values["model_request"]["messages"][0])
    assert completion.context_usage is not None
    assert completion.context_usage.total_tokens > 0


class RejectingProvider(TurnProvider):
    """A server that counts more than Core: it refuses big tool requests.

    Every request carrying a result above ``limit`` estimated tokens is
    refused, and the first ``refuse_first`` such requests whatever their size.
    """

    def __init__(self, calls: int, *, limit: int, refuse_first: int = 0) -> None:
        super().__init__(calls)
        self.limit = limit
        self.refuse_first = refuse_first
        self.rejected: list[ModelRequest] = []

    async def turn_response(self, request: ModelRequest) -> ModelResponse:
        if request.tool_results and (
            estimate_model_request(request) > self.limit
            or len(self.rejected) < self.refuse_first
        ):
            self.rejected.append(request)
            raise ProviderContextLengthError("maximum context length exceeded")
        return await super().turn_response(request)


def _recovered(provider: TurnProvider, kind: str) -> ModelRequest:
    return next(
        request
        for request in _turn_requests(provider)
        if request.metadata.get("context_length_recovery") == kind
    )


def test_a_rejection_clearing_cannot_fix_compacts_the_conversation(tmp_path):
    """F12: after a tool ran, a rejection clearing could not fix failed the turn.

    The one result is already smaller than its receipt, so clearing cannot
    shrink the rejected request. The conversation ahead of it is compacted
    for the retry instead; nothing runs again and the turn completes.
    """

    broker = ProbeBroker()
    provider = RejectingProvider(calls=4, limit=6_000)
    store, service, prepared = _turn(tmp_path, provider, broker, history=10)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert broker.calls == ["1", "2", "3", "4"]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    (rejected,) = provider.rejected
    retry = _recovered(provider, "compacted_conversation")
    assert _compacted(retry) and not _compacted(rejected)
    # The retry replays the same call and result.
    assert [r.call_id for r in retry.tool_results] == ["call-1"]
    assert retry.tool_results == rejected.tool_results
    assert provider.compactions


def test_a_rejection_after_clearing_compacts_the_conversation_once_more(tmp_path):
    """The retry with older results cleared was refused too: the conversation
    is compacted for one more retry; a third refusal would fail the turn."""

    broker = ScanBroker()
    provider = RejectingProvider(calls=3, limit=10**9, refuse_first=2)
    store, service, prepared = _turn(tmp_path, provider, broker, history=6)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert [call.arguments["value"] for call in broker.calls] == ["1", "2", "3"]
    first, second = provider.rejected
    assert "context_length_recovery" not in first.metadata
    assert second.metadata["context_length_recovery"] == "cleared_tool_results"
    assert second.tool_results[-1].output["output_cleared"] is True
    retry = _recovered(provider, "compacted_conversation")
    assert _compacted(retry)
    assert [r.call_id for r in retry.tool_results] == [
        r.call_id for r in first.tool_results
    ]


def test_the_answer_gets_room_when_the_synthesis_no_longer_fits(tmp_path, monkeypatch):
    """Stream R saw routing stop, then the answer request exceed capacity.

    Here the synthesis request is larger than routing's (its instructions are
    padded), so the turn used to fail after its tool ran. The conversation is
    compacted for the answer instead.
    """

    broker = ProbeBroker()
    provider = TurnProvider(calls=1)
    store, service, prepared = _turn(tmp_path, provider, broker, history=8)
    monkeypatch.setattr(
        chat_module,
        "_CHAT_TOOL_RESULT_INSTRUCTIONS",
        chat_module._CHAT_TOOL_RESULT_INSTRUCTIONS + " Summarise." * 600,
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert broker.calls == ["1"]
    requests = _turn_requests(provider)
    (synthesis,) = [r for r in requests if r.tool_choice == ToolChoice.NONE]
    assert _compacted(synthesis)
    assert estimate_model_request(synthesis) <= _capacity(prepared, synthesis)
    assert [r.call_id for r in synthesis.tool_results] == ["call-1"]
    assert not any(_compacted(r) for r in requests if r.tool_choice == ToolChoice.AUTO)


def test_an_answer_that_cannot_fit_fails_saying_what_does_not(tmp_path, monkeypatch):
    broker = ProbeBroker()
    provider = TurnProvider(calls=1)
    store, service, prepared = _turn(tmp_path, provider, broker, history=4)
    capacity = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    ).input_capacity
    monkeypatch.setattr(
        chat_module,
        "_CHAT_TOOL_RESULT_INSTRUCTIONS",
        chat_module._CHAT_TOOL_RESULT_INSTRUCTIONS + "S" * (capacity * 3),
    )

    with pytest.raises(ChatConfigurationError, match="cannot fit the model's input"):
        asyncio.run(service.complete(prepared))

    # The tool ran once and was not repeated by any fallback.
    assert broker.calls == ["1"]
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.FAILED
    assert "tool results are saved" in (turn.error or "")


def test_a_goal_pays_for_the_compaction_its_turn_needed(tmp_path):
    broker = ProbeBroker()
    provider = TurnProvider(calls=4)
    store, service, prepared = _turn(tmp_path, provider, broker, history=10, goal=True)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert provider.compactions
    goal = store.get(ChatGoal, "goal")
    # Each compactor call reports 50 tokens; every turn request reports 3.
    turn_tokens = 3 * len(_turn_requests(provider))
    assert goal.usage.total_tokens == turn_tokens + 50 * len(provider.compactions)


def test_the_headroom_left_for_later_steps_never_blocks_the_compaction(tmp_path):
    """A request whose other parts nearly fill the window still compacts.

    The headroom a compaction leaves below the target for the next steps
    lowers the target only. When the rest of the request leaves no room for
    it, the conversation is compacted to the capacity instead of refused.
    """

    broker = ProbeBroker()
    provider = TurnProvider(calls=0)
    store, service, prepared = _turn(tmp_path, provider, broker, history=10)
    turn = store.get(ChatTurn, "turn")
    capacity = _capacity(prepared, prepared.model_request)
    base = estimate_model_request(prepared.model_request)
    # Everything beside the conversation takes all but ~1,000 tokens.
    padding = "P" * ((capacity - 1_000) * 3)
    request = prepared.model_request.model_copy(
        update={"instructions": (prepared.model_request.instructions or "") + padding}
    )
    assert estimate_model_request(request) > capacity > base

    updated = asyncio.run(
        service._compact_mid_turn(prepared, turn, request, cause="context_full")
    )

    assert updated is not None
    assert _compacted(prepared.model_request)
    rebuilt = request.model_copy(update={"messages": prepared.model_request.messages})
    assert estimate_model_request(rebuilt) <= capacity


class ThinkingProvider(TurnProvider):
    """Each routing response carries long reasoning the route must replay."""

    async def turn_response(self, request: ModelRequest) -> ModelResponse:
        response = await super().turn_response(request)
        if not response.tool_calls:
            return response
        thought = f"thinking before call {self.issued} " + "t" * 6_000
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


def test_replayed_reasoning_that_fills_the_window_folds_into_the_checkpoint(
    tmp_path,
):
    """F12: a route replaying each step's reasoning outgrew any clearing.

    Clearing replaces outputs, not the reasoning each replayed call carries,
    and compacting a short conversation frees little. The replayed steps fold
    into the checkpoint instead, so routing goes on and the answer fits.
    """

    broker = ProbeBroker()
    provider = ThinkingProvider(calls=12)
    store, service, prepared = _turn(tmp_path, provider, broker, history=2)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert broker.calls == [str(step) for step in range(1, 13)]
    requests = _turn_requests(provider)
    for request in requests:
        assert estimate_model_request(request) <= _capacity(prepared, request)
    (synthesis,) = [r for r in requests if r.tool_choice == ToolChoice.NONE]
    replayed = {r.call_id for r in synthesis.tool_results}
    block = str(synthesis.messages[-1].content).split(
        "EARLIER TOOL HISTORY CHECKPOINT", 1
    )[1]
    covered = {
        f"call-{step + 1}"
        for first, last in json.loads(block.split("\n", 1)[1])["covered_steps"]
        for step in range(first, last + 1)
    }
    # Every call is replayed or folded, none both, none lost.
    assert replayed | covered == {f"call-{step}" for step in range(1, 13)}
    assert not replayed & covered


READ_SPEC = SPEC.model_copy(update={"name": "workspace.read"})


class AlternatingProvider(TurnProvider):
    """Reads a file, then probes, in turn: a lookup, then another tool."""

    async def turn_response(self, request: ModelRequest) -> ModelResponse:
        response = await super().turn_response(request)
        if not response.tool_calls or response.tool_calls[0].name != "safe_read":
            return response
        call = response.tool_calls[0]
        name = "workspace.read" if self.issued % 2 else "safe_read"
        return response.model_copy(
            update={"tool_calls": [call.model_copy(update={"name": name})]}
        )


def test_older_lookups_are_cleared_after_other_older_results(tmp_path, monkeypatch):
    """What the model looked up to answer from is cleared last.

    Stream F saw conversation.search find the right passages, then the answer
    say "unknown": the search results were the oldest, so they were cleared
    first. Other results are cleared before a lookup now.
    """

    # The order applies whenever results are cleared. A recent window bounded
    # by tokens folds these steps before any needs clearing (a folded lookup
    # keeps what it found in its receipt), so this keeps the window counted
    # in groups alone to reach clearing.
    monkeypatch.setattr(chat_module, "recent_window_tokens", lambda _capacity: None)
    broker = ScanBroker()
    provider = AlternatingProvider(calls=12)
    store, service, prepared = _turn(
        tmp_path,
        provider,
        broker,
        history=2,
        window=32_768,
        specs=[SPEC, READ_SPEC],
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    lookups_kept = False
    cleared_before: set[str] = set()
    for request in _turn_requests(provider):
        older = request.tool_results[:-1]

        def is_cleared(result) -> bool:
            return isinstance(result.output, dict) and bool(
                result.output.get("output_cleared")
            )

        newly = [r for r in older if is_cleared(r) and r.call_id not in cleared_before]
        # A lookup is newly cleared only once every other older result is.
        if any(r.name == "workspace.read" for r in newly):
            assert all(is_cleared(r) for r in older if r.name == "safe_read")
        if any(r.name == "safe_read" for r in newly) and any(
            r.name == "workspace.read" and not is_cleared(r) for r in older
        ):
            lookups_kept = True
        cleared_before |= {r.call_id for r in older if is_cleared(r)}
    assert lookups_kept


class SynthesisRejectingProvider(TurnProvider):
    """Refuses a big synthesis, then answers empty once, then answers."""

    def __init__(self, limit: int) -> None:
        super().__init__(calls=1)
        self.limit = limit
        self.syntheses: list[ModelRequest] = []

    async def turn_response(self, request: ModelRequest) -> ModelResponse:
        if request.tool_choice != ToolChoice.NONE:
            return await super().turn_response(request)
        self.syntheses.append(request)
        if estimate_model_request(request) > self.limit:
            raise ProviderContextLengthError("maximum context length exceeded")
        if sum(_compacted(r) for r in self.syntheses) == 1:
            return _response(text="")
        return _response(text=ANSWER)


def test_a_second_synthesis_asks_with_the_compacted_conversation(tmp_path):
    """After a rejection compacted the conversation, an answer that came back
    empty is asked for again with that conversation, not the refused one."""

    broker = ProbeBroker()
    provider = SynthesisRejectingProvider(limit=7_000)
    store, service, prepared = _turn(tmp_path, provider, broker, history=10)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    assert broker.calls == ["1"]
    refused, empty, answered = provider.syntheses
    assert not _compacted(refused)
    assert _compacted(empty) and _compacted(answered)
    assert [r.call_id for r in answered.tool_results] == ["call-1"]


def _lookup_entry(step: int) -> dict:
    lines = [
        "The review group skimmed the weekly dashboard.",
        f"KEY: DELTA-{6900 + step}",
        "The design circle walked through the glossary.",
        f"NEXT: chain/file-{step + 1}.txt",
    ]
    return {
        "step": step,
        "response_group": f"group-{step}",
        "model_call_id": f"call-{step}",
        "tool_call_id": f"tool-{step}",
        "name": "workspace.read",
        "arguments": {"path": f"chain/file-{step}.txt"},
        "status": "complete",
        "provider_result": json.dumps(
            {
                "schema": "nebula.workspace.read/v1",
                "path": f"chain/file-{step}.txt",
                "lines": [
                    {"line": number, "text": text}
                    for number, text in enumerate(lines * 20, start=1)
                ],
            }
        ),
        "result_summary": "Result fields: lines, path, schema",
    }


def test_a_cleared_or_folded_lookup_keeps_what_it_found(tmp_path):
    """A folded or cleared read says which codes and paths it read.

    Stream F's long file chain lost every key once its reads folded: a
    receipt said only that a read ran. It now names the identifiers the
    output held, so the answer can still use them.
    """

    from nebula.v3.chat import _cleared_tool_result
    from tests.v3.test_turn_prompt_cache import _ledger_turn

    _, turn, ledger = _ledger_turn(tmp_path)
    checkpoint = ledger._write_checkpoint(
        turn.id, [_lookup_entry(step) for step in range(3)]
    )
    summaries = [receipt[4] for receipt in checkpoint.summary["steps"]]
    assert summaries == [
        f"found DELTA-{6900 + step}, chain/file-{step + 1}.txt" for step in range(3)
    ]
    entry = _lookup_entry(7)
    receipt = _cleared_tool_result(entry, json.loads(entry["provider_result"]))
    assert receipt["output_cleared"] is True
    # What the call asked for is not repeated as a finding.
    assert receipt["found"] == ["DELTA-6907", "chain/file-8.txt"]
    command = {**entry, "name": "run_command"}
    assert "found" not in _cleared_tool_result(
        command, json.loads(entry["provider_result"])
    )
