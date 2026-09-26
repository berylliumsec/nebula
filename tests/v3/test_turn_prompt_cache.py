"""A long tool turn keeps its provider prompt cache and replays compact history.

Every routing step re-sends the steps before it. The request prefix must stay
byte-identical between steps so a provider's prompt cache keeps serving it:
the tool-history checkpoint advances in blocks and rides after the
conversation, never in the instructions. Old failures fold like every other
step, keeping the facts the model needs, and a routing response's reasoning is
stored once rather than copied into each ToolCall and ``tool.proposed`` event.
"""

import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from nebula.v3.chat import ChatService, PreparedChat
from nebula.v3.chat_turn_ledger import (
    CHECKPOINT_STEP_INTERVAL,
    RECENT_RESPONSE_GROUPS,
    ChatTurnLedger,
)
from nebula.v3.database import (
    ChatTurnCheckpointRow,
    ChatTurnStepEventRow,
    RunEventRow,
)
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
    RiskClass,
    ScopePolicy,
    ToolCall as PersistedToolCall,
    ToolCallStatus,
    utc_now,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderFlavor,
    ProviderKind,
    ToolCall,
    _openai_usage,
)
from nebula.v3.runtime_platform import RuntimeToolComponents
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_failures import tool_failure
from nebula.v3.tool_results import serialize_model_result
from nebula.v3.tools import (
    InvalidToolArguments,
    ParallelismPolicy,
    PolicyDenied,
    StoreToolLedger,
    ToolExecutionResult,
    ToolSpec,
)
from nebula.v3.policy import PolicyDecision, PolicyEffect
from tests.v3.test_chat_tool_loop import ScriptedProvider, _response

STEPS = 48
FAIL_EVERY = 3
HEADING = "EARLIER TOOL HISTORY CHECKPOINT"

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

# The Chat Completions payload an OpenRouter route would be sent: what its
# prompt cache sees.
WIRE = OpenAICompatibleProvider(
    ProviderConfig(
        id="provider",
        kind=ProviderKind.OPENAI_COMPATIBLE,
        flavor=ProviderFlavor.OPENROUTER,
        base_url="https://openrouter.ai/api/v1",
        default_model="model-a",
        model_allowlist=["model-a"],
        capabilities=ModelCapabilities(streaming=True, tools=True),
    )
)


class ReservingBroker:
    """Reserves each call as the real broker does, then answers or fails."""

    def __init__(self, store: NebulaStore) -> None:
        self.ledger = StoreToolLedger(store, enforce_run_budget=False)
        self.calls = 0

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        self.calls += 1
        call = await self.ledger.reserve(invocation, SPEC)
        if self.calls % FAIL_EVERY == 0:
            await self.ledger.transition(call, ToolCallStatus.FAILED, error="boom")
            raise RuntimeError("the probe failed")
        await self.ledger.transition(call, ToolCallStatus.COMPLETE, result={})
        return ToolExecutionResult(
            output={"value": invocation.arguments["value"], "blob": "r" * 600}
        )


class WireProvider(ScriptedProvider):
    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(responses)
        self.routing: list[ModelRequest] = []
        self.wires: list[dict] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.metadata.get("operation"):
            self.routing.append(request)
            self.wires.append(WIRE._payload(request, "model-a"))
        return await super().complete(request)


def _state(index: int) -> dict:
    thought = f"thinking about step {index} " + "t" * 400
    return {
        "provider_id": "provider",
        "model": "model-a",
        "reasoning": thought,
        "reasoning_details": [
            {
                "type": "reasoning.text",
                "text": thought,
                "signature": f"sig-{index}",
                "format": "anthropic-claude-v1",
            }
        ],
    }


def _signature(index: int) -> dict:
    return {"extra_content": {"google": {"thought_signature": f"call-sig-{index}"}}}


def _routing(index: int) -> ModelResponse:
    return ModelResponse(
        provider_id="provider",
        model="model-a",
        reasoning=f"thinking about step {index}",
        tool_calls=[
            ToolCall(
                id=f"call-{index}",
                name="safe_read",
                arguments={"value": str(index)},
                provider_metadata=_signature(index),
            )
        ],
        usage=ModelUsage(
            input_tokens=100,
            output_tokens=5,
            total_tokens=105,
            cached_input_tokens=90,
            cache_creation_input_tokens=6,
        ),
        finish_reason="tool_calls",
        reasoning_state=_state(index),
    )


def _long_turn(tmp_path: Path):
    store = NebulaStore(tmp_path / "prompt-cache.db")
    project = store.create(Engagement(id="project", name="Prompt cache"))
    profile = store.create(
        ProviderProfile(
            id="provider",
            name="Provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            capabilities={"streaming": True, "tool_calling": True},
            metadata={
                "model_descriptors": [
                    {
                        "id": "model-a",
                        "context_window": 1_000_000,
                        "max_output_tokens": 8192,
                    }
                ]
            },
        )
    )
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=project.id,
            title="Prompt cache",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={
                "message_count": 1,
                "last_sequence": 1,
                "initial_title_state": "generated",
            },
        )
    )
    user = store.create(
        ChatMessage(
            id="user-message",
            engagement_id=project.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Read every value.",
        )
    )
    turn = store.create(
        ChatTurn(
            id="turn",
            engagement_id=project.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            status=ChatTurnStatus.ROUTING,
            queued_at=utc_now(),
            tools_enabled=True,
            max_tool_calls=STEPS + 5,
        )
    )
    provider = WireProvider(
        [*(_routing(index) for index in range(STEPS)), _response(text="Done.")]
    )
    broker = ReservingBroker(store)
    prepared = PreparedChat(
        provider=provider,
        provider_profile=profile,
        model_request=ModelRequest(
            model="model-a",
            messages=[
                ModelMessage(role="user", content="Earlier question."),
                ModelMessage(role="assistant", content="Earlier answer."),
                ModelMessage(role="user", content=user.content),
            ],
        ),
        resolved_model="model-a",
        citations=[],
        engagement_id=project.id,
        session=session,
        pending_session=None,
        stored_messages=[user],
        new_messages=[],
        tools_enabled=True,
        tool_components=RuntimeToolComponents(
            broker=broker,
            scope=ScopePolicy(engagement_id=project.id),
            workspace=tmp_path,
            specs={SPEC.name: SPEC},
            runtime_digest="prompt-cache",
        ),
        turn=turn,
        inputs_persisted=True,
    )
    return store, ChatService(store, worker_id="worker"), prepared, provider, broker


def _shared_messages(earlier: list[dict], later: list[dict]) -> int:
    shared = 0
    while shared < min(len(earlier), len(later)) and earlier[shared] == later[shared]:
        shared += 1
    return shared


def test_routing_requests_extend_each_other_between_checkpoint_advances(tmp_path):
    """TURN-3: the checkpoint rewrote the system prompt on every step.

    It was appended to the instructions and advanced whenever one response
    group left the recent window, so after the first checkpoint almost no
    request shared a prefix with the one before it.
    """

    store, service, prepared, provider, broker = _long_turn(tmp_path)

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == "Done."
    assert broker.calls == STEPS
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.COMPLETE
    routing = provider.routing[:STEPS]
    wires = provider.wires[:STEPS]
    # The instructions and tools are the same bytes on every routing step.
    assert len({request.instructions for request in routing}) == 1
    assert len({json.dumps(wire["tools"], sort_keys=True) for wire in wires}) == 1
    with store.database.session() as session:
        checkpoints = list(
            session.scalars(
                select(ChatTurnCheckpointRow).order_by(
                    ChatTurnCheckpointRow.through_step
                )
            )
        )
    assert 1 <= len(checkpoints) <= STEPS // CHECKPOINT_STEP_INTERVAL
    advances = []
    for index, (earlier, later) in enumerate(zip(wires, wires[1:]), 1):
        earlier_messages, later_messages = earlier["messages"], later["messages"]
        shared = _shared_messages(earlier_messages, later_messages)
        if shared == len(earlier_messages):
            continue
        # Only an advance changes an earlier message, and then only from the
        # operator message the checkpoint rides with: the system prompt and
        # the conversation before it stay cached.
        advances.append(index)
        assert shared == 3
        assert HEADING in later_messages[3]["content"]
    assert len(advances) == len(checkpoints)
    for request, wire in zip(routing, wires):
        assert HEADING not in (request.instructions or "")
        messages = wire["messages"]
        assert [message["role"] for message in messages[:4]] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert messages[3]["content"].startswith("Read every value.")
        assert all(HEADING not in str(message) for message in messages[4:])
        # Every replayed response carries its reasoning back to the route.
        replayed = [
            message for message in messages[4:] if message["role"] == "assistant"
        ]
        assert len(replayed) == len(request.tool_results)
        for message in replayed:
            (call,) = message["tool_calls"]
            index = int(call["id"].removeprefix("call-"))
            assert message["reasoning_details"] == _state(index)["reasoning_details"]
            assert message["reasoning"] == _state(index)["reasoning"]
    # Nothing writes the unread checkpoint pointer onto the turn any more.
    assert turn.checkpoint_through_step == 0
    # The provider's cache hits and writes are counted with the turn's usage.
    assert turn.usage.cached_input_tokens == 90 * STEPS
    assert turn.usage.cache_creation_input_tokens == 6 * STEPS


def test_failed_steps_fold_outside_the_recent_window(tmp_path):
    """TURN-4: every failed or denied step was replayed whole on every request.

    One in three calls fails. Once outside the recent window the failures are
    folded into the checkpoint like any other step, so the replay stays
    bounded, and the checkpoint keeps what the model needs of each.
    """

    store, service, prepared, provider, _ = _long_turn(tmp_path)

    asyncio.run(service.complete(prepared))

    last = provider.routing[STEPS - 1]
    # The recent window plus at most one block not yet folded.
    assert len(last.tool_results) < RECENT_RESPONSE_GROUPS + CHECKPOINT_STEP_INTERVAL
    replayed_steps = [
        int(result.call_id.removeprefix("call-")) for result in last.tool_results
    ]
    assert replayed_steps == list(range(STEPS - len(replayed_steps) - 1, STEPS - 1))
    block = str(last.messages[-1].content).split(HEADING, 1)[1]
    checkpoint = json.loads(block.split("\n", 1)[1])
    assert checkpoint["schema"] == "nebula.chat-turn-checkpoint/v2"
    assert checkpoint["covered_steps"] == [[0, replayed_steps[0] - 1]]
    assert checkpoint["step_fields"][-1] == "failure"
    receipts = {receipt[0]: receipt for receipt in checkpoint["steps"]}
    for step in range(replayed_steps[0]):
        receipt = receipts[step]
        if (step + 1) % FAIL_EVERY:
            assert receipt[2] == "complete"
            assert len(receipt) < 6
            continue
        assert receipt[2] == "failed"
        failure = receipt[5]
        assert (
            failure["arguments_sha256"]
            == hashlib.sha256(
                json.dumps({"value": str(step)}, separators=(",", ":")).encode()
            ).hexdigest()[:16]
        )
        assert failure["category"] == "execution_failed"
        assert failure["side_effects"] == "unknown"
        assert failure["retry_safe"] is False


def test_tool_calls_name_their_ledger_intent_instead_of_copying_reasoning(tmp_path):
    """TURN-4: each ToolCall and tool.proposed event carried the reasoning again."""

    store, service, prepared, _, _ = _long_turn(tmp_path)

    asyncio.run(service.complete(prepared))

    calls = store.list_entities(PersistedToolCall, limit=100)
    assert len(calls) == STEPS
    for call in calls:
        intent = call.metadata["provider_history_intent"]
        step = call.metadata["provider_step"]
        assert "reasoning_state" not in intent
        assert "response_text" not in intent
        assert "provider_metadata" not in intent
        assert intent["ledger_event"] == f"intent:{step}:call-{step}"
        # Recovery reads the replay state back from the ledger intent row.
        restored = service._replayable_intent("turn", intent)
        assert "ledger_event" not in restored
        assert restored["reasoning_state"] == _state(step)
        assert restored["provider_metadata"] == _signature(step)
        assert restored["response_group"] == intent["response_group"]
    with store.database.session() as session:
        proposed = list(
            session.scalars(
                select(RunEventRow).where(RunEventRow.event_type == "tool.proposed")
            )
        )
        rows = list(session.scalars(select(ChatTurnStepEventRow)))
    assert len(proposed) == STEPS
    assert all("thinking about step" not in json.dumps(row.payload) for row in proposed)
    assert all("call-sig-" not in json.dumps(row.payload) for row in proposed)
    # The ledger keeps one copy per step: the intent row. The result row names it.
    holders = [row for row in rows if "reasoning_state" in row.payload]
    assert sorted(row.step for row in holders) == list(range(STEPS))
    assert all(row.event_type == "started" for row in holders)
    # A ToolCall recorded before the change keeps the copy it carries.
    legacy = {"step": 3, "model_call_id": "old", "reasoning_state": {"old": True}}
    assert service._replayable_intent("turn", legacy) == legacy


def _entry(step: int, status: str = "complete", **fields) -> dict:
    return {
        "step": step,
        "response_group": f"group-{step}",
        "model_call_id": f"call-{step}",
        "tool_call_id": f"tool-{step}",
        "name": "workspace.read",
        "arguments": {"path": f"file-{step}.txt"},
        "status": status,
        "provider_result": json.dumps({"status": status, "preview": "x" * 200}),
        "result_summary": f"Read file {step}",
        **fields,
    }


def _ledger_turn(tmp_path: Path) -> tuple[NebulaStore, ChatTurn, ChatTurnLedger]:
    store, _, prepared, _, _ = _long_turn(tmp_path)
    assert prepared.turn is not None
    return store, prepared.turn, ChatTurnLedger(store.database)


def test_replay_stays_bounded_with_fifty_failed_steps(tmp_path):
    """TURN-4: 50 failures used to be replayed on every later request."""

    _, turn, ledger = _ledger_turn(tmp_path)
    denied = tool_failure(
        SPEC,
        {"value": "x"},
        PolicyDenied(PolicyDecision(effect=PolicyEffect.DENY, reason="no", rule="r")),
        phase="before_execution",
    )
    invalid = tool_failure(
        SPEC,
        {"value": 1},
        InvalidToolArguments("wrong type for value"),
        phase="before_execution",
    )
    unknown = {"schema": "nebula.restart-uncertain/v1", "status": "unknown"}
    sizes = []
    for step in range(60):
        if step < 50:
            kind = step % 3
            entry = _entry(
                step,
                "denied" if kind == 0 else "failed",
                provider_result=serialize_model_result(
                    denied if kind == 0 else invalid if kind == 1 else unknown
                ),
                **({"recovered_from_restart_unknown": True} if kind == 2 else {}),
            )
        else:
            entry = _entry(step)
        ledger.append(turn.id, entry)
        checkpoint, replay = ledger.compacted_history(turn)
        sizes.append(len(json.dumps(replay)))
    assert len(replay) < RECENT_RESPONSE_GROUPS + CHECKPOINT_STEP_INTERVAL
    assert max(sizes) < 24 * 1_000
    assert checkpoint is not None
    facts = {receipt[0]: receipt[5] for receipt in checkpoint.summary["steps"]}
    assert facts[0]["category"] == "permission_denied"
    assert facts[0]["retry_safe"] is False
    assert facts[1]["category"] == "invalid_arguments"
    assert facts[1]["invalid_input"] == "value"
    assert facts[1]["retry_safe"] is True
    assert facts[2] == {
        "arguments_sha256": hashlib.sha256(
            json.dumps({"path": "file-2.txt"}, separators=(",", ":")).encode()
        ).hexdigest()[:16],
        "category": "outcome_unknown",
        "side_effects": "unknown",
        "retry_safe": False,
    }


def test_checkpoint_advances_in_blocks_and_replay_only_grows_between(tmp_path):
    _, turn, ledger = _ledger_turn(tmp_path)
    previous = None
    advances = 0
    for step in range(80):
        ledger.append(turn.id, _entry(step))
        checkpoint, replay = ledger.compacted_history(turn)
        if previous is not None:
            earlier_checkpoint, earlier_replay = previous
            if checkpoint == earlier_checkpoint:
                # Between advances each replay extends the one before it.
                assert replay[: len(earlier_replay)] == earlier_replay
                assert len(replay) == len(earlier_replay) + 1
            else:
                advances += 1
                assert len(replay) == RECENT_RESPONSE_GROUPS
        previous = (checkpoint, replay)
    # The first checkpoint included: one per block that left the window.
    assert advances == (80 - RECENT_RESPONSE_GROUPS) // CHECKPOINT_STEP_INTERVAL


def test_a_waiting_step_stays_whole_until_it_is_settled_and_folded(tmp_path):
    _, turn, ledger = _ledger_turn(tmp_path)
    ledger.append(turn.id, _entry(0, "waiting_approval", approval_id="approval-1"))
    for step in range(1, 30):
        ledger.append(turn.id, _entry(step))
    checkpoint, replay = ledger.compacted_history(turn)
    assert checkpoint is not None
    assert not checkpoint.covers(_entry(0, "waiting_approval"))
    assert replay[0]["step"] == 0
    # Settled, it stays whole until the next advance folds it too.
    ledger.append(turn.id, _entry(0, "complete"))
    settled, replay = ledger.compacted_history(turn)
    assert settled == checkpoint
    assert 0 in [entry["step"] for entry in replay]
    for step in range(30, 30 + CHECKPOINT_STEP_INTERVAL):
        ledger.append(turn.id, _entry(step))
    folded, replay = ledger.compacted_history(turn)
    assert folded is not None and folded.covers(_entry(0))
    assert 0 not in [entry["step"] for entry in replay]


def test_a_v1_checkpoint_still_replays_the_failures_it_left_out(tmp_path):
    """Turns running across the upgrade keep their failures visible."""

    store, turn, ledger = _ledger_turn(tmp_path)
    for step in range(30):
        ledger.append(turn.id, _entry(step, "failed" if step in {3, 7} else "complete"))
    with store.database.session() as session:
        session.add(
            ChatTurnCheckpointRow(
                id="v1",
                turn_id=turn.id,
                through_step=21,
                summary={
                    "schema": "nebula.chat-turn-checkpoint/v1",
                    "through_step": 21,
                    "steps": [],
                },
                digest="0" * 64,
                token_estimate=10,
                created_at=utc_now(),
            )
        )
    checkpoint, replay = ledger.compacted_history(turn)
    assert checkpoint is not None and checkpoint.through_step == 21
    assert [entry["step"] for entry in replay] == [3, 7, *range(22, 30)]
    # The first v2 checkpoint folds them.
    for step in range(30, 30 + CHECKPOINT_STEP_INTERVAL):
        ledger.append(turn.id, _entry(step))
    checkpoint, replay = ledger.compacted_history(turn)
    assert checkpoint is not None
    assert checkpoint.summary["schema"] == "nebula.chat-turn-checkpoint/v2"
    assert [entry["step"] for entry in replay][:1] != [3]
    assert {3, 7} <= {receipt[0] for receipt in checkpoint.summary["steps"]}


def test_a_step_stores_its_reasoning_once_across_its_rows(tmp_path):
    store, turn, ledger = _ledger_turn(tmp_path)
    shared = {
        "reasoning_state": {"provider_id": "p", "model": "m", "reasoning": "r" * 500},
        "response_text": "Reading the file.",
    }
    intent = {**_entry(0, "running"), **shared}
    first = ledger.append(turn.id, intent, idempotency_key="intent:0:call-0")
    second = ledger.append(turn.id, {**intent, "status": "complete"})
    third = ledger.append(
        turn.id,
        {**intent, "status": "failed", "result_summary": "reconciled"},
        idempotency_key="recorded-result:tool-0",
    )
    # A different response's reasoning is not a repeat.
    ledger.append(
        turn.id,
        {**_entry(1), "reasoning_state": {"provider_id": "p", "reasoning": "other"}},
    )
    with store.database.session() as session:
        rows = {
            row.sequence: row.payload
            for row in session.scalars(select(ChatTurnStepEventRow))
        }
    assert rows[first]["reasoning_state"] == shared["reasoning_state"]
    for sequence in (second, third):
        assert "reasoning_state" not in rows[sequence]
        assert "response_text" not in rows[sequence]
        assert rows[sequence]["replay_from"] == first
    history = ledger.history(turn)
    assert history[0] == {
        **intent,
        "status": "failed",
        "result_summary": "reconciled",
    }
    assert history[1]["reasoning_state"] == {"provider_id": "p", "reasoning": "other"}
    assert ledger.event(turn.id, "recorded-result:tool-0") == history[0]
    assert ledger.event(turn.id, "missing") is None
    # The same transition appended again is still recognised as a duplicate.
    assert ledger.append(turn.id, {**intent, "status": "complete"}) == second


def test_chat_completions_usage_reports_prompt_cache_hits():
    assert (
        _openai_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 64},
            }
        ).cached_input_tokens
        == 64
    )
    # DeepSeek names its hits separately.
    assert (
        _openai_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 4,
                "prompt_cache_hit_tokens": 80,
            }
        ).cached_input_tokens
        == 80
    )
    assert (
        _openai_usage(
            {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": None}}
        ).cached_input_tokens
        == 0
    )
