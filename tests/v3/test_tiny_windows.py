"""8–9K windows (Ollama-style local models) keep a long tool turn working.

An 8K window's input is most of it spoken for before the turn reads anything:
instructions, function declarations and a conversation of a few earlier
exchanges. Each file read is about 2,000 tokens. The turn then had room for
barely one result, and on a turn of 24 reads the checkpoint folded at nearly
every step. Two things made it worse:

* the conversation, compacted mid-turn to make room, kept two of six earlier
  exchanges verbatim and filled the room they left with retrieved excerpts:
  the compaction was sized by the capacity, because the headroom it was
  asked to leave pushed its target below the current message;
* the checkpoint's receipts were bounded at 16 KiB (about 5,400 tokens), most
  of the window, so a long turn's receipts grew until they filled it.

The compaction is now sized to leave the conversation as the memory and the
current message, and it is made only when it frees a step's worth, or keeps
the newest result whole. The receipts take at most 15% of a small window.
"""

import asyncio
import json
from pathlib import Path

from nebula.v3 import chat as chat_module
from nebula.v3.chat import _RETRIEVED_EXCERPTS_HEADING
from nebula.v3.chat_turn_ledger import (
    CHECKPOINT_BYTE_CEILING,
    CHECKPOINT_BYTE_LIMIT,
    CHECKPOINT_MIN_BYTES,
    _canonical,
    checkpoint_byte_limit,
)
from nebula.v3.context import calibrated_request_estimate, resolve_context_limits
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import ModelRequest, ModelResponse, ModelUsage, ToolChoice
from nebula.v3.tools import ToolExecutionResult
from tests.v3 import test_midturn_compaction as midturn
from tests.v3.test_in_turn_context_pruning import ANSWER, ScanBroker
from tests.v3.test_tool_history_memory import _ledger_turn, _step

STEPS = 24
FACTS = [
    "staging database host is db-stage-17.internal on port 6432",
    "API key rotation is every 45 days",
    "on-call starts with team Kestrel",
    "artifacts are signed with key id SIG-4471",
    "incident channel is #ops-bridge-9",
    "max error budget is 0.4% per week",
]
BRIEFINGS = [
    f"Project briefing part {index + 1}. Key fact: The {fact}. "
    + "Background paragraph describing the deployment pipeline. " * 30
    + "\nReply with just 'Noted.'"
    for index, fact in enumerate(FACTS)
]
# Eight functions, about 1,700 tokens of declarations.
SPECS = [midturn.SPEC] + [
    midturn.SPEC.model_copy(
        update={
            "name": f"tool_{index}",
            "description": "A tool that does a thing with the workspace. " * 12,
        }
    )
    for index in range(7)
]


class FileBroker(midturn.ProbeBroker):
    """Each call reads a ~5,600-character file, about 1,900 tokens."""

    async def execute(self, invocation, scope, *, approval=None):
        del scope, approval
        value = invocation.arguments["value"]
        self.calls.append(value)
        return ToolExecutionResult(
            output={
                "value": value,
                "text": f"file {value} PLANTED-{value} " + "line of the file. " * 310,
            }
        )


class ThinkingProvider(midturn.TurnProvider):
    """Short reasoning per step; a compactor that writes about 2,800 chars."""

    async def turn_response(self, request: ModelRequest) -> ModelResponse:
        response = await super().turn_response(request)
        if not response.tool_calls:
            return response
        thought = f"step {self.issued}: read the next file. " + "t" * 260
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

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation") == "context_compaction":
            self.compactions.append(request)
            summary = (
                "Briefing facts: "
                + "; ".join(FACTS)
                + ". "
                + "The operator shared pipeline background. " * 55
            )
            return ModelResponse(
                provider_id="provider",
                model="model-a",
                text=json.dumps({"summary": summary}),
                usage=ModelUsage(input_tokens=40, output_tokens=10, total_tokens=50),
                finish_reason="stop",
            )
        return await super().complete(request)


def _small_window_turn(tmp_path: Path, monkeypatch, window: int):
    """Six briefing exchanges, then 24 file reads, calibrated as DeepSeek."""

    monkeypatch.setattr(
        midturn,
        "_history",
        lambda count: [
            message for text in BRIEFINGS[: count // 2] for message in (text, "Noted.")
        ],
    )
    recorded: list[str] = []
    real = chat_module.record_diagnostic

    def record(level, feature, event_code, message, **fields):
        recorded.append(event_code)
        return real(level, feature, event_code, message, **fields)

    monkeypatch.setattr(chat_module, "record_diagnostic", record)
    broker = FileBroker()
    provider = ThinkingProvider(calls=STEPS)
    store, service, prepared = midturn._turn(
        tmp_path, provider, broker, history=12, window=window, specs=SPECS
    )
    prepared.estimate_calibration = 0.67
    completion = asyncio.run(service.complete(prepared))
    assert completion.message.content == midturn.ANSWER
    assert broker.calls == [str(step) for step in range(1, STEPS + 1)]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    routing = [
        request
        for request in midturn._turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    limits = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    )
    for request in routing:
        assert (
            calibrated_request_estimate(request, 0.67, hard=True)
            <= limits.input_capacity
        )
    return prepared, provider, routing, recorded


def _early_changes(routing: list[ModelRequest]) -> int:
    return sum(
        earlier.messages != later.messages
        for earlier, later in zip(routing, routing[1:])
    )


def _blind(routing: list[ModelRequest]) -> int:
    return sum(
        isinstance(request.tool_results[-1].output, dict)
        and bool(request.tool_results[-1].output.get("output_cleared"))
        for request in routing
        if request.tool_results
    )


def test_an_8k_turn_compacts_its_conversation_to_the_memory_once(tmp_path, monkeypatch):
    """11 of 24 steps changed the request ahead of its tool history; the
    compaction kept two earlier exchanges and excerpts of the rest."""

    prepared, provider, routing, recorded = _small_window_turn(
        tmp_path, monkeypatch, window=15_000
    )

    assert _early_changes(routing) <= 4
    assert not _blind(routing)
    assert [cause for _, cause in prepared.midturn_compactions] == ["step_room"]
    compacted = [request for request in routing if midturn._compacted(request)]
    assert compacted
    # The compaction happened at the first step that lacked room.
    assert routing.index(compacted[0]) <= 2
    for request in compacted:
        # The memory leads the current message: no earlier exchange is kept
        # verbatim, and no excerpt fills the room the compaction made.
        (message,) = request.messages
        assert midturn.MEMORY in str(message.content)
        assert "Project briefing part" not in str(message.content)
        assert _RETRIEVED_EXCERPTS_HEADING not in str(message.content)


def test_a_12k_turn_whose_replay_only_needs_folding_keeps_its_conversation(
    tmp_path, monkeypatch
):
    """With a checkpoint fold the replay leaves room below a 12K window's
    capacity for more steps: the conversation is not compacted."""

    prepared, provider, routing, recorded = _small_window_turn(
        tmp_path, monkeypatch, window=18_700
    )

    assert not provider.compactions
    assert not prepared.midturn_compactions
    assert _early_changes(routing) <= 2
    assert not _blind(routing)


def test_a_small_window_bounds_its_receipts_to_a_share_of_its_capacity(tmp_path):
    # 15% of the capacity below about 36,000 tokens, never below 2 KiB.
    assert checkpoint_byte_limit(6_692) == 1_003 * 3
    assert checkpoint_byte_limit(10_500) == 1_575 * 3
    assert checkpoint_byte_limit(4_000) == CHECKPOINT_MIN_BYTES
    # Unchanged from about 36,000 tokens: 16 KiB, then 3% up to 64 KiB.
    assert checkpoint_byte_limit(36_500) == CHECKPOINT_BYTE_LIMIT
    assert checkpoint_byte_limit(128_000) == CHECKPOINT_BYTE_LIMIT
    assert checkpoint_byte_limit(1_000_000) == CHECKPOINT_BYTE_CEILING

    store, turn, ledger = _ledger_turn(tmp_path)
    entries = [
        _step(step, "failed" if step % 10 == 3 else "complete") for step in range(40)
    ]
    limit = checkpoint_byte_limit(7_500)
    checkpoint = ledger._write_checkpoint(turn.id, entries, byte_limit=limit)
    summary = checkpoint.summary
    assert len(_canonical(summary)) <= limit
    # What it drops it counts; the newest receipts stay.
    kept = [receipt[0] for receipt in summary["steps"]]
    assert summary["omitted_steps"] == 40 - len(kept)
    assert kept[-1] == 39
    assert summary["covered_steps"] == [[0, 39]]


def test_a_long_turn_keeps_its_receipts_small_beside_a_small_window(
    tmp_path, monkeypatch
):
    """110 scans on a 16K window: the receipts grew to 16 KiB, 65% of the
    window, carried by every request after them."""

    broker = ScanBroker()
    provider = midturn.TurnProvider(calls=110)
    store, service, prepared = midturn._turn(
        tmp_path, provider, broker, history=0, window=16_384
    )
    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == midturn.ANSWER
    assert len(broker.calls) == 110
    limits = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    )
    largest = 0
    for request in midturn._turn_requests(provider):
        content = str(request.messages[-1].content)
        _, _, checkpoint = content.partition("EARLIER TOOL HISTORY CHECKPOINT")
        if checkpoint:
            receipts = json.loads(checkpoint.split("\n", 1)[1])
            largest = max(largest, len(_canonical(receipts)))
    # At most 15% of the window's working capacity, and the checkpoint's own
    # fields; it had reached 16 KiB.
    assert largest > 0
    assert largest <= checkpoint_byte_limit(limits.working_input_capacity) + 512
    assert largest <= 0.15 * limits.working_input_capacity * 3 + 512


def test_a_checkpoint_does_not_repeat_the_notes_its_message_carries(tmp_path):
    """A turn that starts with notes carries them on its current message; the
    checkpoint that rides on the same message carried them again, up to
    8 KiB twice, until a notes.write in the turn changes them."""

    from nebula.v3.chat import _with_trailing_block
    from nebula.v3.working_notes import working_notes_block, write_working_notes
    from tests.v3.test_in_turn_context_pruning import (
        LongTurnProvider,
        _long_turn,
        _turn_requests,
    )

    broker = ScanBroker()
    provider = LongTurnProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, broker)
    notes = write_working_notes(
        store,
        engagement_id=prepared.engagement_id,
        session_id="session",
        content="## Findings\n- " + "port 8443 open on every scanned host; " * 40,
        turn_id="turn-0",
    )
    block = working_notes_block(notes)
    prepared.model_request = prepared.model_request.model_copy(
        update={
            "messages": _with_trailing_block(prepared.model_request.messages, block)
        }
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    checkpointed = [
        request
        for request in _turn_requests(provider)
        if "EARLIER TOOL HISTORY CHECKPOINT" in str(request.messages[-1].content)
    ]
    assert checkpointed
    for request in checkpointed:
        content = str(request.messages[-1].content)
        assert content.count("port 8443 open on every scanned host") == 40
        receipts = json.loads(
            content.split("EARLIER TOOL HISTORY CHECKPOINT", 1)[1].split("\n", 1)[1]
        )
        assert "working_notes" not in receipts
