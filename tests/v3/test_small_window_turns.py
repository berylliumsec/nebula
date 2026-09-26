"""A long tool turn on a small window changes its request rarely, and late.

Every step re-sends the turn's earlier results. A request that crosses its
target clears some of them, and advanced the checkpoint first: the checkpoint
rides on the current message, so the request changed from there, ahead of its
whole tool history, and the provider's prefix cache missed all of it. On a
small window the rest of the request (instructions, function declarations,
conversation) can fill the room below the target by itself; then every step
crossed, advanced and cleared, and some steps cleared even the newest result,
the one the model was deciding on. Without room below the target a crossing
now keeps the checkpoint where it is while clearing can bring the request
below the target, folds every step but the newest when it cannot, runs on to
the input capacity when even that leaves no room, and compacts the
conversation once before the newest result would be cleared.
"""

import asyncio
from pathlib import Path

from nebula.v3 import chat as chat_module
from nebula.v3.chat import CLEARING_HEADROOM_STEPS, ChatService
from nebula.v3.context import calibrated_request_estimate, resolve_context_limits
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.providers import ModelRequest, ToolChoice
from tests.v3.test_midturn_compaction import (
    ANSWER,
    ProbeBroker,
    TurnProvider,
    _turn,
    _turn_requests,
)

STEPS = 24


def _run(
    tmp_path: Path,
    monkeypatch,
    *,
    window: int,
    history: int,
    calibration: float | None = None,
):
    recorded: list[str] = []
    real = chat_module.record_diagnostic

    def record(level, feature, event_code, message, **fields):
        recorded.append(event_code)
        return real(level, feature, event_code, message, **fields)

    monkeypatch.setattr(chat_module, "record_diagnostic", record)
    broker = ProbeBroker()
    provider = TurnProvider(calls=STEPS)
    store, service, prepared = _turn(
        tmp_path, provider, broker, history=history, window=window
    )
    prepared.estimate_calibration = calibration
    completion = asyncio.run(service.complete(prepared))
    assert completion.message.content == ANSWER
    # Every step ran, once, in order, and the turn completed.
    assert broker.calls == [str(step) for step in range(1, STEPS + 1)]
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    assert len(routing) == STEPS + 1
    limits = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    )
    # Nothing is sent past the input capacity.
    for request in routing:
        assert (
            calibrated_request_estimate(request, calibration, hard=True)
            <= limits.input_capacity
        )
    return prepared, provider, routing, limits, recorded


def _early_changes(routing: list[ModelRequest]) -> int:
    """Requests that changed ahead of their tool history: the conversation or
    the checkpoint on the current message, not just a result cleared."""

    return sum(
        earlier.messages != later.messages or earlier.instructions != later.instructions
        for earlier, later in zip(routing, routing[1:])
    )


def _blind(routing: list[ModelRequest]) -> int:
    """Requests whose newest result, the one the model decides on, was cleared."""

    return sum(
        isinstance(request.tool_results[-1].output, dict)
        and bool(request.tool_results[-1].output.get("output_cleared"))
        for request in routing
        if request.tool_results
    )


def test_a_small_window_changes_its_request_late_not_at_every_step(
    tmp_path, monkeypatch
):
    """On a 12K window with some conversation, 23 of 24 steps changed the
    request, 16 of them ahead of its tool history (the checkpoint advancing
    at every crossing). Now the checkpoint folds once and the requests run on
    to the capacity while there is no room below the target. Folded, the
    replay leaves room below the capacity for more steps, so the conversation
    is not compacted (it was, and sent 8% more uncached tokens)."""

    prepared, provider, routing, limits, recorded = _run(
        tmp_path, monkeypatch, window=12_288, history=4
    )

    assert _early_changes(routing) <= 3
    assert not _blind(routing)
    assert recorded.count("chat.tool_history.capacity_ceiling") >= 1
    sizes = [calibrated_request_estimate(request, None) for request in routing]
    assert max(sizes) > limits.target_input_tokens
    assert not prepared.midturn_compactions
    assert not provider.compactions


def test_a_turn_without_room_for_its_headroom_clears_late(tmp_path, monkeypatch):
    """On an 8K window the headroom for three steps is more than the target
    leaves: every crossing advanced the checkpoint, and 10 of 24 steps
    changed the request ahead of its tool history. Clearing alone keeps it
    below the target, so it changes only from the results it clears."""

    prepared, provider, routing, limits, recorded = _run(
        tmp_path, monkeypatch, window=8_192, history=0
    )

    assert _early_changes(routing) <= 2
    assert not _blind(routing)
    assert not provider.compactions
    assert max(calibrated_request_estimate(request, None) for request in routing) <= (
        limits.target_input_tokens
    )


def test_the_newest_result_stays_whole_when_the_conversation_fills_the_window(
    tmp_path, monkeypatch
):
    """With a conversation that nearly fills an 8K window, the checkpoint
    folded at every step and five requests cleared even the newest result.
    The conversation is compacted once instead."""

    prepared, provider, routing, limits, recorded = _run(
        tmp_path, monkeypatch, window=8_192, history=4
    )

    assert not _blind(routing)
    assert _early_changes(routing) <= 4
    assert [cause for _, cause in prepared.midturn_compactions] == ["step_room"]
    assert recorded.count("chat.context.midturn_compacted") == 1


def test_a_prose_calibrated_turn_counts_its_room_as_the_provider_does(
    tmp_path, monkeypatch
):
    """With DeepSeek's prose calibration (0.6) the conversation's share is
    smaller than its estimate, the tool history's is not; the room and the
    compaction's size are both counted that way. Five requests had cleared
    the newest result."""

    prepared, provider, routing, limits, recorded = _run(
        tmp_path, monkeypatch, window=12_288, history=8, calibration=0.6
    )

    assert not _blind(routing)
    assert _early_changes(routing) <= 3
    assert [cause for _, cause in prepared.midturn_compactions] == ["step_room"]


def test_a_window_with_room_is_unchanged(tmp_path, monkeypatch):
    """A 32K window has room below its target for many steps: it clears as
    it did, never runs to its capacity and compacts nothing."""

    prepared, provider, routing, limits, recorded = _run(
        tmp_path, monkeypatch, window=32_768, history=4
    )

    assert _early_changes(routing) <= 1
    assert not provider.compactions
    assert not prepared.runs_to_capacity
    assert max(calibrated_request_estimate(request, None) for request in routing) <= (
        limits.target_input_tokens
    )


def test_a_crossing_leaves_room_for_several_average_steps():
    headroom = ChatService._clearing_headroom
    # A large window keeps its block: a tenth of the working capacity.
    assert headroom(96_000, 128_000, step=700) == 12_800
    # A small window kept half its target, about one step of file reads.
    assert headroom(7_875, 10_500) == 7_875 - 7_875 // 2
    assert headroom(7_875, 10_500, step=1_400) == CLEARING_HEADROOM_STEPS * 1_400
    # A batch of five reads per response on a 128K window: three of them.
    assert headroom(96_000, 128_000, step=10_000) == 30_000


def test_a_mid_turn_compaction_counts_tool_history_as_the_provider_does(tmp_path):
    """With a prose calibration (DeepSeek, 0.6) the compaction budgeted the
    conversation as if the request's tool history, JSON, counted 0.6 of its
    estimate too: it found the conversation already small enough and left a
    request over its target."""

    from nebula.v3.providers import ModelToolResult

    provider = TurnProvider(calls=0)
    store, service, prepared = _turn(tmp_path, provider, ProbeBroker(), history=10)
    prepared.estimate_calibration = 0.6
    turn = store.get(ChatTurn, "turn")
    limits = resolve_context_limits(
        prepared.provider_profile, model="model-a", required_parameters={"tools"}
    )
    history = [
        ModelToolResult(
            call_id=f"call-{index}",
            name="safe_read",
            arguments={"value": str(index)},
            output={"text": f"result {index} " + "x" * 3_000},
        )
        for index in range(2)
    ]
    request = prepared.model_request.model_copy(update={"tool_results": history})
    assert calibrated_request_estimate(request, 0.6) > limits.target_input_tokens

    updated = asyncio.run(
        service._compact_mid_turn(prepared, turn, request, cause="context_full")
    )

    assert updated is not None
    rebuilt = request.model_copy(update={"messages": prepared.model_request.messages})
    assert calibrated_request_estimate(rebuilt, 0.6) <= limits.target_input_tokens
