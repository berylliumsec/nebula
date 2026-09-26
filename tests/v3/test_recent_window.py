"""A turn's recent window is bounded by tokens as well as response groups.

The newest response groups stay whole in every request; older steps may fold
into the turn's checkpoint, where their receipts keep what they did (and, for
a lookup, what it found). Counted in groups alone, a model that batches five
calls per response kept about forty steps out of every checkpoint, so once
they outgrew the request they were cleared in place instead. The window now
stops at a share of the model's input capacity, never below the newest
group, and steps still fold in blocks, so the prefix stays stable.
"""

import asyncio
import json

from nebula.v3 import chat as chat_module
from nebula.v3.chat_turn_ledger import (
    RECENT_RESPONSE_GROUPS,
    RECENT_WINDOW_MIN_TOKENS,
    ChatTurnLedger,
    _token_estimate,
    recent_window_tokens,
)
from nebula.v3.context import estimate_model_request, resolve_context_limits
from nebula.v3.providers import ModelRequest, ToolCall, ToolChoice
from tests.v3.test_chat_tool_loop import _response
from tests.v3.test_in_turn_context_pruning import (
    ANSWER,
    LongTurnProvider,
    _cleared,
    _extends,
    _long_turn,
    _turn_requests,
)
from tests.v3.test_midturn_compaction import ProbeBroker

BATCH = 5


def _entry(step: int, group: int, size: int = 3_000) -> dict:
    return {
        "step": step,
        "response_group": f"group-{group}",
        "model_call_id": f"call-{step}",
        "tool_call_id": f"tool-{step}",
        "name": "workspace.read",
        "arguments": {"path": f"data/file-{step}.txt"},
        "status": "complete",
        "provider_result": json.dumps({"status": "complete", "content": "x" * size}),
        "result_summary": f"Read file {step}.",
    }


def _batched(groups: int, *, size: int = 3_000) -> list[dict]:
    return [
        _entry(group * BATCH + index, group, size)
        for group in range(groups)
        for index in range(BATCH)
    ]


def test_the_window_keeps_whole_groups_newest_first_within_its_budget():
    history = _batched(6)
    newest_two = _token_estimate(history[-2 * BATCH : -BATCH]) + _token_estimate(
        history[-BATCH:]
    )

    kept = ChatTurnLedger._recent_groups(history, RECENT_RESPONSE_GROUPS, newest_two)
    assert kept == {"group-5", "group-4"}
    assert ChatTurnLedger._recent_groups(
        history, RECENT_RESPONSE_GROUPS, newest_two - 1
    ) == {"group-5"}
    # Counted in groups alone, all six stay whole.
    assert ChatTurnLedger._recent_groups(history, RECENT_RESPONSE_GROUPS, None) == {
        f"group-{index}" for index in range(6)
    }
    # The newest group is kept whatever its size ...
    assert ChatTurnLedger._recent_groups(history, RECENT_RESPONSE_GROUPS, 1) == {
        "group-5"
    }
    # ... the group count still caps the window, and a deeper fold keeps none.
    assert ChatTurnLedger._recent_groups(history, 1, 10**9) == {"group-5"}
    assert ChatTurnLedger._recent_groups(history, 0, 10**9) == set()


def test_the_budget_is_a_share_of_input_capacity_with_a_floor():
    assert recent_window_tokens(1_000) == RECENT_WINDOW_MIN_TOKENS
    assert recent_window_tokens(24_576) == 6_144
    assert recent_window_tokens(1_000_000) == 250_000


class BatchingProvider(LongTurnProvider):
    """Issues its calls five to a response, as DeepSeek readily does."""

    async def complete(self, request: ModelRequest):
        if (
            request.metadata.get("operation")
            or request.tool_choice == ToolChoice.NONE
            or self.issued >= self.calls
        ):
            return await super().complete(request)
        self.requests.append(request)
        calls = []
        for _ in range(min(BATCH, self.calls - self.issued)):
            self.issued += 1
            calls.append(
                ToolCall(
                    id=f"call-{self.issued}",
                    name="safe_read",
                    arguments={"value": str(self.issued)},
                )
            )
        return _response(calls=calls)


def _batched_turn(tmp_path, monkeypatch, *, token_bound: bool):
    if not token_bound:
        monkeypatch.setattr(chat_module, "recent_window_tokens", lambda _capacity: None)
    # Twelve responses of five ~2 KB results on a 32K window.
    provider = BatchingProvider(calls=60)
    store, service, prepared = _long_turn(tmp_path, provider, ProbeBroker())
    completion = asyncio.run(service.complete(prepared))
    assert completion.message.content == ANSWER
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    return prepared, routing


def _covered(request: ModelRequest) -> int:
    content = str(request.messages[-1].content)
    marker = "EARLIER TOOL HISTORY CHECKPOINT"
    if marker not in content:
        return 0
    summary = json.loads(content.split(marker, 1)[1].split("\n", 1)[1])
    return sum(last - first + 1 for first, last in summary["covered_steps"])


def test_batched_steps_fold_into_the_checkpoint_instead_of_being_cleared(
    tmp_path, monkeypatch
):
    prepared, routing = _batched_turn(tmp_path, monkeypatch, token_bound=True)
    last = routing[-1]

    # The older batches folded: their receipts are in the checkpoint ...
    assert _covered(last) >= 40
    # ... and nothing had to be cleared in place: what is replayed is whole.
    for request in routing:
        assert not [result for result in request.tool_results if _cleared(result)]
    for request in routing:
        limits = resolve_context_limits(
            prepared.provider_profile,
            model=request.model,
            requested_output_tokens=request.max_output_tokens,
            required_parameters={"tools"},
        )
        assert estimate_model_request(request) <= limits.input_capacity
    # Folding happens in blocks, so the prefix changes once per advance, not
    # per step: here twice in twelve steps.
    changes = sum(
        not _extends(earlier, later) for earlier, later in zip(routing, routing[1:])
    )
    assert 1 <= changes <= 3


def test_counted_in_groups_alone_the_same_batches_were_cleared_in_place(
    tmp_path, monkeypatch
):
    """The defect: eight batches of five stayed whole, so they were cleared."""

    _, routing = _batched_turn(tmp_path, monkeypatch, token_bound=False)
    last = routing[-1]

    cleared = [result for result in last.tool_results if _cleared(result)]
    assert _covered(last) <= 15
    assert len(cleared) >= 30
