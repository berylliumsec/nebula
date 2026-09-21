"""A failed or stopped turn leaves a well-formed history the model can learn from.

Before this, a provider turn that failed or was stopped left only the
operator's message in the transcript: the next request carried two user
messages in a row (strict chat templates and Bedrock reject that) and the
model never learned which tools the failed turn had already run, so it could
run a side-effecting command again. Subagent reports posted after the
parent's answer made assistant,assistant pairs, and the compactor's repair
request dropped the model's own previous output between two user messages.
"""

import asyncio
import json

import pytest

from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.context import ContextCompactor, ContextSource, ContextSourceReference
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ContextOwnerType,
    Engagement,
)
from nebula.v3.model_catalog import ModelDescriptor
from nebula.v3.providers import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    ProviderContextLengthError,
    ProviderHealth,
    StreamEventType,
    ToolCall,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_context import MemoryProvider, _message, _owner
from tests.v3.test_context import _profile as _context_profile


class FailOnceProvider(FakeProvider):
    """Fails the first answer request the way an exhausted upstream does."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id, local=True)
        self.failures = 1

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.metadata.get("operation") and self.failures:
            self.failures -= 1
            self.requests.append(request)
            raise RuntimeError("upstream 500 after retries")
        return await super().complete(request)


class BlockingProvider(FakeProvider):
    """Starts an answer and never finishes it, so the operator has to stop it."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id, local=True)
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest):
        del request
        self.started.set()
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="Working. ")
        await asyncio.Event().wait()
        raise AssertionError("stopped provider stream resumed unexpectedly")


class SilentThenAnsweringProvider(FakeProvider):
    """Returns empty answers until told to answer, like a stalled reasoning model."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id, local=True)
        self.answer = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("operation"):
            return await super().complete(request)
        self.requests.append(request)
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text="Recovered answer." if self.answer else "",
            usage=ModelUsage(input_tokens=4, output_tokens=1, total_tokens=5),
            finish_reason="stop",
        )


def _service(tmp_path, provider):
    store = NebulaStore(tmp_path / "turn-outcome.db")
    engagement = store.create(Engagement(id="eng", name="Outcomes"))
    profile = store.create(_profile(local=True))
    return (
        store,
        engagement,
        profile,
        ChatService(store, provider_factory=lambda _: provider),
    )


def _request(engagement, profile, content, *, session_id=None, stream=True, **extra):
    return ChatCompletionRequest(
        provider_id=profile.id,
        engagement_id=engagement.id,
        session_id=session_id,
        messages=[{"role": "user", "content": content}],
        include_knowledge=False,
        stream=stream,
        **extra,
    )


async def _run_streamed(service, prepared) -> str:
    turn_id = service.start_provider_turn(prepared)
    with pytest.raises(Exception):
        async for _ in service.follow_provider_turn(turn_id):
            pass
    return turn_id


def _outcome_notes(service, session_id, *, include_replaced=False):
    return [
        message
        for message in service.session_messages(
            session_id, include_replaced=include_replaced
        )
        if message.metadata.get("kind") == "turn_outcome"
    ]


def _roles(request: ModelRequest) -> list[str]:
    return [message.role for message in request.messages]


def test_failed_turn_saves_an_outcome_note_so_the_next_request_alternates(tmp_path):
    provider = FailOnceProvider("provider-a")
    store, engagement, profile, service = _service(tmp_path, provider)
    first = service.prepare(
        _request(engagement, profile, "Scan 10.0.0.5 and summarise open ports.")
    )
    turn_id = asyncio.run(_run_streamed(service, first))
    session_id = first.turn.session_id

    assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.FAILED
    transcript = service.session_messages(session_id)
    assert [message.role for message in transcript] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
    ]
    note = transcript[-1]
    assert note.content.startswith("Response failed: upstream 500 after retries")
    assert note.metadata["kind"] == "turn_outcome"
    assert note.metadata["chat_turn_id"] == turn_id
    assert note.metadata["turn_status"] == "failed"
    # The UI already shows a saved message with this finish reason as
    # stopped (#491), so the note reads as an outcome rather than an answer.
    assert note.finish_reason == "interrupted"
    assert store.get(ChatSession, session_id).metadata["last_sequence"] == note.sequence

    second = service.prepare(
        _request(engagement, profile, "Try again please.", session_id=session_id)
    )
    assert _roles(second.model_request) == ["user", "assistant", "user"]
    assert second.model_request.messages[1].content == note.content


def test_failed_tool_turn_note_names_the_steps_that_already_ran(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, provider = _prepared(
        tmp_path,
        [
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ]
            )
        ],
        broker,
    )

    async def fail_after_script(request: ModelRequest) -> ModelResponse:
        provider.requests.append(request)
        if request.metadata.get("operation") == "conversation_naming":
            return _response(text="Tool result")
        if provider.responses:
            return provider.responses.pop(0)
        raise RuntimeError("upstream 502 while routing")

    provider.complete = fail_after_script
    with pytest.raises(Exception, match="upstream 502"):
        asyncio.run(service.complete(prepared))

    assert len(broker.calls) == 1
    turn = store.get(ChatTurn, "turn")
    assert turn.status == ChatTurnStatus.FAILED
    notes = _outcome_notes(service, "session")
    assert len(notes) == 1
    note = notes[0]
    assert note.content.startswith("Response failed: upstream 502 while routing")
    assert "Completed tool steps:" in note.content
    assert 'safe_read {"value": "a"} →' in note.content
    assert note.metadata["tool_call_ids"] == turn.tool_call_ids
    assert [item["capability"] for item in note.metadata["tool_results"]] == [
        "safe_read"
    ]
    assert [message.role for message in service.session_messages("session")] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
    ]


def test_stopped_turn_saves_an_outcome_note(tmp_path):
    provider = BlockingProvider("provider-a")
    store, engagement, profile, service = _service(tmp_path, provider)

    async def scenario():
        prepared = await service.prepare_async(
            _request(engagement, profile, "Enumerate the web server.")
        )
        turn_id = service.start_provider_turn(prepared)
        await asyncio.wait_for(provider.started.wait(), 5)
        await service.stop_provider_turn(turn_id)
        return prepared.turn.session_id, turn_id

    session_id, turn_id = asyncio.run(scenario())

    assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.CANCELLED
    notes = _outcome_notes(service, session_id)
    assert len(notes) == 1
    assert notes[0].content.startswith("Response stopped")
    assert notes[0].metadata["turn_status"] == "cancelled"
    assert notes[0].finish_reason == "interrupted"
    following = service.prepare(
        _request(engagement, profile, "Continue from there.", session_id=session_id)
    )
    assert _roles(following.model_request) == ["user", "assistant", "user"]


def test_stopping_a_turn_parked_on_approval_says_the_call_never_ran(tmp_path):
    store, engagement, profile, service = _service(
        tmp_path, FakeProvider("provider-a", local=True)
    )
    session = store.create(
        ChatSession(
            id="parked",
            engagement_id=engagement.id,
            title="Parked",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"last_sequence": 1, "message_count": 1},
        )
    )
    store.create(
        ChatMessage(
            engagement_id=engagement.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Clean up the scratch directory.",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            tools_enabled=True,
            status=ChatTurnStatus.WAITING_APPROVAL,
            tool_history=[
                {
                    "step": 0,
                    "tool_call_id": "tool-call-1",
                    "name": "shell_command",
                    "arguments": {"command": "rm -rf /tmp/scratch"},
                    "status": "waiting_approval",
                }
            ],
        )
    )

    service.cancel_turn(turn.id)
    # A second stop of the same turn must not add a second note.
    service.cancel_turn(turn.id)

    notes = _outcome_notes(service, session.id)
    assert len(notes) == 1
    assert notes[0].content.startswith("Response stopped")
    assert "Completed tool steps:" not in notes[0].content
    assert "shell_command" in notes[0].content
    assert "rm -rf /tmp/scratch" in notes[0].content
    assert "did not run" in notes[0].content


def test_a_subagent_wait_that_cannot_resume_records_its_outcome(
    tmp_path,
):
    store, engagement, profile, service = _service(
        tmp_path, FakeProvider("provider-a", local=True)
    )
    session = store.create(
        ChatSession(
            id="waiting",
            engagement_id=engagement.id,
            title="Waiting",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"last_sequence": 1, "message_count": 1},
        )
    )
    store.create(
        ChatMessage(
            engagement_id=engagement.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Split the sweep across subagents and wait.",
        )
    )
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            tools_enabled=True,
            status=ChatTurnStatus.WAITING_CALLBACK,
            tool_history=[
                {
                    "step": 0,
                    "tool_call_id": "tool-call-1",
                    "name": "wait_for_subagents",
                    "arguments": {"ids": ["sub-a"]},
                    "status": "waiting_callback",
                    "subagent_wait": {"ids": ["sub-a"]},
                }
            ],
        )
    )

    asyncio.run(
        service.subagents._fail_unresumable(turn, RuntimeError("provider went away"))
    )

    assert store.get(ChatTurn, turn.id).status == ChatTurnStatus.FAILED
    transcript = service.session_messages(session.id)
    assert [message.role for message in transcript] == [
        ChatRole.USER,
        ChatRole.ASSISTANT,
    ]
    assert transcript[1].content.startswith(
        "Response failed: Subagent reports are ready but the response could not resume"
    )
    assert "wait_for_subagents" in transcript[1].content


def test_final_answer_retry_takes_the_place_of_the_outcome_note(tmp_path):
    provider = SilentThenAnsweringProvider("provider-a")
    store, engagement, profile, service = _service(tmp_path, provider)
    prepared = service.prepare(_request(engagement, profile, "Summarise the findings."))
    turn_id = asyncio.run(_run_streamed(service, prepared))
    session_id = prepared.turn.session_id
    assert service.recoverable_final_answer_turn(session_id).id == turn_id
    assert len(_outcome_notes(service, session_id)) == 1

    provider.answer = True

    async def finish_the_answer():
        service.start_provider_turn(service.prepare_resume(turn_id))
        return [event async for event, _ in service.follow_provider_turn(turn_id)]

    assert asyncio.run(finish_the_answer())[-1] == "done"

    transcript = service.session_messages(session_id, include_replaced=True)
    assert [(message.role, message.content) for message in transcript] == [
        (ChatRole.USER, "Summarise the findings."),
        (ChatRole.ASSISTANT, "Recovered answer."),
    ]
    assert store.get(ChatTurn, turn_id).status == ChatTurnStatus.COMPLETE


def test_edit_in_place_retracts_the_outcome_note_with_its_message(tmp_path):
    provider = FailOnceProvider("provider-a")
    store, engagement, profile, service = _service(tmp_path, provider)
    first = service.prepare(_request(engagement, profile, "Scan the wrong host."))
    asyncio.run(_run_streamed(service, first))
    session_id = first.turn.session_id
    user_message = service.session_messages(session_id)[0]
    assert len(_outcome_notes(service, session_id)) == 1

    service.rewind_session(session_id, before_message_id=user_message.id)
    edited = service.prepare(
        _request(engagement, profile, "Scan the right host.", session_id=session_id)
    )

    assert _roles(edited.model_request) == ["user"]
    assert edited.model_request.messages[0].content == "Scan the right host."
    assert _outcome_notes(service, session_id) == []
    retracted = _outcome_notes(service, session_id, include_replaced=True)
    assert len(retracted) == 1
    assert retracted[0].metadata["retracted_reason"] == "operator_edit"


def test_a_client_sending_full_history_without_the_note_still_appends(tmp_path):
    provider = FailOnceProvider("provider-a")
    store, engagement, profile, service = _service(tmp_path, provider)
    first = service.prepare(_request(engagement, profile, "First question."))
    asyncio.run(_run_streamed(service, first))
    session_id = first.turn.session_id

    second = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session_id,
            messages=[
                {"role": "user", "content": "First question."},
                {"role": "user", "content": "Second question."},
            ],
            include_knowledge=False,
        )
    )

    assert _roles(second.model_request) == ["user", "assistant", "user"]
    assert [message.content for message in second.new_messages] == ["Second question."]


def _parent_with_subagent_posts(store, engagement, profile) -> ChatSession:
    session = store.create(
        ChatSession(
            id="parent",
            engagement_id=engagement.id,
            title="Parent",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"last_sequence": 4, "message_count": 4},
        )
    )
    for sequence, role, text, metadata in [
        (1, ChatRole.USER, "Enumerate both hosts using subagents.", {}),
        (
            2,
            ChatRole.ASSISTANT,
            "Started two subagents; I will summarise when they report.",
            {},
        ),
        (
            3,
            ChatRole.ASSISTANT,
            "Subagent finished: host-a\n\n22/tcp open",
            {"kind": "subagent_result", "subagent_id": "sub-a"},
        ),
        (
            4,
            ChatRole.ASSISTANT,
            "Message from subagent host-b:\n\n80/tcp open",
            {"kind": "subagent_message", "subagent_id": "sub-b"},
        ),
    ]:
        store.create(
            ChatMessage(
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=role,
                content=text,
                metadata=metadata,
            )
        )
    return session


def test_subagent_posts_join_the_preceding_assistant_message(tmp_path):
    store, engagement, profile, service = _service(
        tmp_path, FakeProvider("provider-a", local=True)
    )
    session = _parent_with_subagent_posts(store, engagement, profile)

    prepared = service.prepare(
        _request(engagement, profile, "Summarise the results.", session_id=session.id)
    )

    assert _roles(prepared.model_request) == ["user", "assistant", "user"]
    joined = prepared.model_request.messages[1].content
    assert joined == (
        "Started two subagents; I will summarise when they report.\n\n"
        "Subagent finished: host-a\n\n22/tcp open\n\n"
        "Message from subagent host-b:\n\n80/tcp open"
    )
    # The transcript keeps each report as its own message for the UI cards.
    stored = service.session_messages(session.id)
    assert [message.metadata.get("kind") for message in stored[:4]] == [
        None,
        None,
        "subagent_result",
        "subagent_message",
    ]


def test_context_length_recovery_also_joins_subagent_posts(tmp_path):
    class RejectOnceProvider(FakeProvider):
        def __init__(self, provider_id: str) -> None:
            super().__init__(provider_id, local=True)
            self.rejected = False

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if not request.metadata.get("operation"):
                self.requests.append(request)
                if not self.rejected:
                    self.rejected = True
                    raise ProviderContextLengthError("context length exceeded")
            return await super().complete(request)

        async def health(self) -> ProviderHealth:
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=True,
                models=["model-a"],
                model_descriptors=[
                    ModelDescriptor(
                        id="model-a",
                        name="model-a",
                        context_window=32_768,
                        max_output_tokens=2_048,
                    )
                ],
            )

    provider = RejectOnceProvider("provider-a")
    store, engagement, profile, service = _service(tmp_path, provider)
    session = _parent_with_subagent_posts(store, engagement, profile)
    prepared = service.prepare(
        _request(engagement, profile, "Summarise the results.", session_id=session.id)
    )

    async def answer():
        turn_id = service.start_provider_turn(prepared)
        return [event async for event, _ in service.follow_provider_turn(turn_id)]

    assert asyncio.run(answer())[-1] == "done"
    retried = [
        request
        for request in provider.requests
        if request.metadata.get("context_length_recovery") == "1"
    ]
    assert retried
    assert all(_roles(request) == ["user", "assistant", "user"] for request in retried)


@pytest.mark.parametrize(
    "first_output, calls",
    [
        ('{"summary": "Unsupported", "confirmed_facts": [{"text": "x"}]}', []),
        ("", [ToolCall(id="call-1", name="lookup", arguments={})]),
    ],
)
def test_compaction_repair_replays_the_previous_output_as_its_own_turn(
    tmp_path, first_output, calls
):
    class RepairingProvider(MemoryProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            first = len(self.requests) == 1
            return ModelResponse(
                provider_id=self.config.id,
                model="model-a",
                text=first_output if first else json.dumps({"summary": "Repaired."}),
                tool_calls=calls if first else [],
                usage=ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                finish_reason="stop",
            )

    store = NebulaStore(tmp_path / "compaction-repair.db")
    profile = _context_profile()
    session = _owner(store, profile)
    _message(store, session, message_id="message-1", sequence=1, content="Fact")
    provider = RepairingProvider(profile.id)

    result = asyncio.run(
        ContextCompactor(store).compact(
            owner_type=ContextOwnerType.CHAT_SESSION,
            owner_id=session.id,
            engagement_id=session.engagement_id,
            provider_profile=profile,
            provider=provider,
            model="model-a",
            sources=[
                ContextSource(
                    ContextSourceReference(
                        source_kind="chat_message",
                        source_id="message-1",
                        sequence=1,
                    ),
                    "Fact",
                )
            ],
            compacted_through=1,
        )
    )

    assert result.snapshot.memory is not None
    assert result.snapshot.memory.summary == "Repaired."
    repair = provider.requests[1]
    assert _roles(repair) == ["user", "assistant", "user"]
    assert repair.messages[0].content == provider.requests[0].messages[0].content
    replayed = repair.messages[1].content
    assert isinstance(replayed, str) and replayed.strip()
    assert "failed validation" in repair.messages[2].content
    if first_output:
        assert replayed == first_output
        assert first_output not in repair.messages[2].content


def test_outcome_note_is_bounded_for_long_failures(tmp_path):
    store, engagement, profile, service = _service(
        tmp_path, FakeProvider("provider-a", local=True)
    )
    session = store.create(
        ChatSession(
            id="long",
            engagement_id=engagement.id,
            title="Long",
            provider_profile_id=profile.id,
            model="model-a",
            metadata={"last_sequence": 1, "message_count": 1},
        )
    )
    store.create(
        ChatMessage(
            engagement_id=engagement.id,
            session_id=session.id,
            sequence=1,
            role=ChatRole.USER,
            content="Run the sweep.",
        )
    )
    history = [
        {
            "step": index,
            "tool_call_id": f"tool-call-{index}",
            "name": "shell_command",
            "arguments": {"command": f"probe --target host-{index} " + "x" * 400},
            "status": "complete",
            "provider_result": json.dumps({"status": "complete"}),
            "result_summary": "Tool execution completed " + "y" * 400,
        }
        for index in range(40)
    ]
    turn = store.create(
        ChatTurn(
            engagement_id=engagement.id,
            session_id=session.id,
            provider_profile_id=profile.id,
            model="model-a",
            tools_enabled=True,
            status=ChatTurnStatus.ROUTING,
            tool_history=history,
        )
    )
    store.update(
        ChatTurn,
        turn.id,
        {"status": ChatTurnStatus.FAILED, "error": "boom " * 190},
        expected_revision=turn.revision,
    )

    note = service.record_turn_outcome(turn.id)

    assert note is not None
    assert len(note.content) <= 6_000
    assert "host-39" in note.content
    assert "earlier" in note.content
    assert service.record_turn_outcome(turn.id) is None
