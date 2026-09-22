"""A turn-completed hook receives the answer the turn is about to store.

A Codex Stop hook reads ``last_assistant_message``: a completion hook's common
job is to inspect the final answer (a claim guard, a status check). The
``chat.turn.completed`` payload carries it as ``assistant_message`` on every
completion path, bounded to 64 KiB with ``assistant_message_truncated`` saying
whether it was cut; failed and cancelled events carry both keys too, so one
hook serves all three ends.
"""

import asyncio
import json

import pytest

from nebula.v3.chat import _turn_end_hook_payload
from nebula.v3.domain import ChatTurn, ChatTurnStatus
from nebula.v3.native_hooks import discover_native_hooks, snapshot_native_hook
from nebula.v3.providers import ModelRequest, ModelResponse, ModelUsage
from tests.v3.test_chat import FakeProvider, _write_native_hook
from tests.v3.test_native_hook_turn_ends import _service
from tests.v3.test_routing_prose_answer import _answer, _glm_turn, _synthesis

LIMIT = 64 * 1024
HOOK_EVENTS = ["chat.turn.completed", "chat.turn.failed", "chat.turn.cancelled"]
PAYLOAD_KEYS = {
    "provider_id",
    "model",
    "finish_reason",
    "detail",
    "assistant_message",
    "assistant_message_truncated",
}

# Like an operator's port of a Codex Stop hook: echoes its envelope and refuses
# a completion event whose payload has no answer text to inspect.
ANSWER_GUARD_HOOK = (
    "#!/bin/sh\n"
    "python3 -c 'import json,sys; envelope=json.load(sys.stdin); "
    "print(json.dumps(envelope)); "
    'message = envelope["payload"].get("assistant_message"); '
    'completed = envelope["event"] == "chat.turn.completed"; '
    "raise SystemExit(2 if completed and not isinstance(message, str) else 0)'\n"
)


def test_routing_answer_completed_hook_receives_the_answer(tmp_path):
    workspace = tmp_path / "workspace"
    _write_native_hook(
        workspace,
        "answer-guard",
        events=HOOK_EVENTS,
        script=ANSWER_GUARD_HOOK,
        failure_policy="block",
    )
    store, service, prepared, _, _ = _glm_turn(
        tmp_path, [_answer("Hello!"), _synthesis("A second, paid-for answer.")]
    )
    prepared.hook_snapshots = [
        snapshot_native_hook("answer-guard", discover_native_hooks(workspace))
    ]

    completion = asyncio.run(service.complete(prepared))

    # A tool turn whose routing reply is the answer ends on that reply, and
    # its completed hook sees it as a synthesized answer's hook would.
    assert completion.message.content == "Hello!"
    assert store.get(ChatTurn, "turn").status == ChatTurnStatus.COMPLETE
    executions = [
        item
        for item in service.list_turn_hook_executions("turn")
        if item.event_name == "chat.turn.completed"
    ]
    assert [item.status for item in executions] == ["complete"]
    payload = json.loads(executions[0].stdout)["payload"]
    assert set(payload) == PAYLOAD_KEYS
    assert payload["assistant_message"] == "Hello!"
    assert payload["assistant_message_truncated"] is False


class LongAnswerProvider(FakeProvider):
    """Answers with more text than a hook payload carries."""

    # 70,001 bytes; after the one-byte "a" the 64 KiB cut falls mid-"é".
    ANSWER = "a" + "é" * 35_000

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.requests.append(request)
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model-a",
            text=self.ANSWER,
            usage=ModelUsage(input_tokens=4, output_tokens=3, total_tokens=7),
            finish_reason="stop",
        )


def test_completed_hook_answer_is_bounded_and_flagged(tmp_path):
    async def scenario():
        _, service, _, request, hook_dir = _service(
            tmp_path,
            LongAnswerProvider,
            hook_id="answer-guard",
            events=HOOK_EVENTS,
            # Hook stdout is itself capped, so this hook keeps its envelope.
            script="#!/bin/sh\ncat > envelope.json\n",
            failure_policy="block",
        )
        prepared = await service.prepare_async(request)
        completion = await service.complete(prepared)
        await service.shutdown()
        return hook_dir, completion

    hook_dir, completion = asyncio.run(scenario())

    # The stored answer is whole; only the hook's copy is bounded.
    assert completion.message.content == LongAnswerProvider.ANSWER
    envelope = json.loads((hook_dir / "envelope.json").read_text(encoding="utf-8"))
    assert envelope["event"] == "chat.turn.completed"
    payload = envelope["payload"]
    assert set(payload) == PAYLOAD_KEYS
    assert payload["assistant_message_truncated"] is True
    # Cut on a character boundary: the torn "é" is dropped, not mangled.
    assert payload["assistant_message"] == LongAnswerProvider.ANSWER[:32_768]
    assert len(payload["assistant_message"].encode("utf-8")) == LIMIT - 1


@pytest.mark.parametrize(
    "answer,sent,truncated",
    [
        ("x" * LIMIT, "x" * LIMIT, False),
        ("x" * (LIMIT + 1), "x" * LIMIT, True),
        (None, None, False),
    ],
    ids=["at-limit", "over-limit", "no-answer"],
)
def test_turn_end_payload_bounds_the_answer_at_64_kib(answer, sent, truncated):
    payload = _turn_end_hook_payload("stop", None, answer)

    assert payload == {
        "finish_reason": "stop",
        "detail": None,
        "assistant_message": sent,
        "assistant_message_truncated": truncated,
    }
