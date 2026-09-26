"""A turn's tool work stays reachable after its output leaves the request.

Inside a long turn, folded steps become checkpoint receipts that say what each
call acted on, within a bound that grows with the model. After the turn, the
stored answer carries its tool activity, with the ids that read the full
output again, into every later request, and ``tool_output`` accepts those ids
from any later turn of the same conversation. Working notes the assistant
writes with ``notes.write`` survive both: a checkpoint carries them, and a new
turn starts with them.
"""

import asyncio
import json

import pytest

from nebula.v3 import chat as chat_module
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.browser_tools import combine_tool_components
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatRequestMessage,
    ChatService,
    _stored_model_text,
)
from nebula.v3.chat_turn_ledger import (
    CHECKPOINT_BYTE_CEILING,
    CHECKPOINT_BYTE_LIMIT,
    _canonical,
    checkpoint_byte_limit,
)
from nebula.v3.context import estimate_tokens
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    ChatWorkingNotes,
    Engagement,
    RiskClass,
    ToolCall as StoredToolCall,
    ToolCallOrigin,
    ToolCallStatus,
)
from nebula.v3.providers import ModelRequest, ToolCall, ToolChoice
from nebula.v3.runtime_platform import notes_components
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_activity import (
    ACTIVITY_BLOCK_BYTES,
    TOOL_ACTIVITY_HEADING,
    tool_activity_block,
)
from nebula.v3.tool_results import ToolOutputAccessError, ToolOutputService
from nebula.v3.tools import InvalidToolArguments, ToolBrokerError, ToolInvocation
from nebula.v3.working_notes import (
    NOTES_ROUTING_INSTRUCTIONS,
    NOTES_WRITE_TOOL_NAME,
    WORKING_NOTES_HEADING,
    WriteNotesTool,
    read_working_notes,
    write_working_notes,
)
from tests.v3.test_chat import FakeProvider, _profile
from tests.v3.test_chat_tool_loop import RecordingBroker, _prepared, _response
from tests.v3.test_in_turn_context_pruning import (
    ANSWER,
    LongTurnProvider,
    ScanBroker,
    _long_turn,
    _turn_requests,
)
from tests.v3.test_turn_prompt_cache import _ledger_turn

SECRET = "abcdefghijklmnopqrstuvwxyz0123"


def _step(step: int, status: str = "complete") -> dict:
    return {
        "step": step,
        "response_group": f"group-{step}",
        "model_call_id": f"call-{step}",
        "tool_call_id": f"tool-{step}",
        "name": "run_command",
        "arguments": {
            "command": (
                f"curl -H 'Authorization: Bearer {SECRET}' https://host-{step}.test/"
            ),
            "timeout_seconds": 30,
        },
        "status": status,
        "provider_result": json.dumps({"status": status, "preview": "x" * 200}),
        "result_summary": f"Fetched host {step}: " + "detail " * 100,
        "result_artifact_id": f"artifact-{step}",
    }


def test_checkpoint_receipts_say_what_each_step_did_within_a_scaled_bound(tmp_path):
    """F1: receipts were an 80-character summary under a fixed 16 KiB cap."""

    assert checkpoint_byte_limit(8_192) == CHECKPOINT_BYTE_LIMIT
    assert checkpoint_byte_limit(128_000) == CHECKPOINT_BYTE_LIMIT
    assert checkpoint_byte_limit(400_000) == 36_000
    assert checkpoint_byte_limit(1_000_000) == CHECKPOINT_BYTE_CEILING

    store, turn, ledger = _ledger_turn(tmp_path)
    entries = [
        _step(step, "failed" if step % 10 == 3 else "complete") for step in range(200)
    ]
    small = ledger._write_checkpoint(turn.id, entries, byte_limit=16 * 1024)
    wide_turn = store.create(turn.model_copy(update={"id": "wide-turn"}))
    wide = ledger._write_checkpoint(wide_turn.id, entries, byte_limit=64 * 1024)

    for checkpoint, limit in ((small, 16 * 1024), (wide, 64 * 1024)):
        summary = checkpoint.summary
        assert summary["schema"] == "nebula.chat-turn-checkpoint/v3"
        assert summary["step_fields"][3:5] == ["did", "summary"]
        encoded = _canonical(summary)
        assert len(encoded) <= limit
        assert SECRET.encode() not in encoded
        # The estimate is the request estimator's, not a separate bytes/4
        # guess capped at 4,000 tokens.
        assert checkpoint.token_estimate == estimate_tokens(encoded.decode())
        receipts = {receipt[0]: receipt for receipt in summary["steps"]}
        # Every failure survives the bound; the oldest successes went first.
        failures = [step for step in range(200) if step % 10 == 3]
        assert set(failures) <= set(receipts)
        kept_successes = sorted(set(receipts) - set(failures))
        assert kept_successes == [
            step for step in range(200) if step % 10 != 3 and step >= kept_successes[0]
        ]
        assert summary.get("omitted_steps", 0) == 200 - len(receipts)
        for step, receipt in receipts.items():
            assert receipt[3] == (
                "command=curl -H 'Authorization: Bearer [REDACTED]' "
                f"https://host-{step}.test/"
            )
            assert receipt[4].startswith(f"Fetched host {step}: detail")
            assert len(receipt[4]) <= 200
            assert receipt[5] == [f"artifact-{step}"]
    assert wide.summary.get("omitted_steps", 0) < small.summary["omitted_steps"]
    assert wide.token_estimate > 4_000


def test_a_checkpoint_carries_the_working_notes_outside_the_receipt_bound(tmp_path):
    _, turn, ledger = _ledger_turn(tmp_path)
    notes = {"content": "- [x] scanned\n- [ ] report", "revision": 3}
    reads = []

    def working_notes():
        reads.append(True)
        return notes

    for step in range(8):
        ledger.append(turn.id, _step(step))
        checkpoint, _ = ledger.compacted_history(turn, working_notes=working_notes)
    # Nothing folded yet, so the notes were never read.
    assert checkpoint is None and reads == []
    for step in range(8, 40):
        ledger.append(turn.id, _step(step))
        checkpoint, _ = ledger.compacted_history(
            turn, byte_limit=16 * 1024, working_notes=working_notes
        )
    assert checkpoint is not None
    assert checkpoint.summary["working_notes"] == notes
    # One read per checkpoint written, not per request.
    assert 1 <= len(reads) <= 3
    without_notes = {
        key: value
        for key, value in checkpoint.summary.items()
        if key != "working_notes"
    }
    assert len(_canonical(without_notes)) <= 16 * 1024


def _result(index: int, status: str = "complete") -> dict:
    return {
        "tool_call_id": f"tool-call-{index}",
        "capability": "run_command",
        "display_name": "Command runtime",
        "status": status,
        "brief": f"command=cat /srv/data/file-{index}.txt",
        "summary": "Tool execution completed; inspect artifacts with tool_output.search",
        "evidence_ids": [],
        "result_artifact_id": f"artifact-{index}",
        "artifacts": [{"artifact_id": f"artifact-{index}", "kind": "stdout"}],
    }


def _message(
    sequence: int, role: ChatRole, content: str, metadata: dict | None = None
) -> ChatMessage:
    return ChatMessage(
        id=f"message-{sequence}",
        engagement_id="project",
        session_id="session",
        sequence=sequence,
        role=role,
        content=content,
        metadata=metadata or {},
    )


def test_an_answer_carries_its_tool_activity_into_every_later_request():
    """F4: the next turn saw only the answer's prose, not which tools ran."""

    answer = _message(
        2,
        ChatRole.ASSISTANT,
        "The config sets port 8443.",
        {"tool_results": [_result(1), _result(2, "failed")]},
    )
    stored = [_message(1, ChatRole.USER, "Read the config."), answer]
    history, _ = ChatService._merge_history(
        stored, [ChatRequestMessage(role=ChatRole.USER, content="Which file?")]
    )
    text = history[1].content
    assert text.startswith("The config sets port 8443.\n\n" + TOOL_ACTIVITY_HEADING)
    activity = json.loads(text.split(TOOL_ACTIVITY_HEADING + "\n", 1)[1])
    assert activity == {
        "steps": [
            {
                "tool": "run_command",
                "status": "complete",
                "did": "command=cat /srv/data/file-1.txt",
                "summary": (
                    "Tool execution completed; inspect artifacts with "
                    "tool_output.search"
                ),
                "tool_call_id": "tool-call-1",
                "artifact_ids": ["artifact-1"],
            },
            {
                "tool": "run_command",
                "status": "failed",
                "did": "command=cat /srv/data/file-2.txt",
                "summary": (
                    "Tool execution completed; inspect artifacts with "
                    "tool_output.search"
                ),
                "tool_call_id": "tool-call-2",
                "artifact_ids": ["artifact-2"],
            },
        ]
    }
    # The transcript the operator reads is unchanged.
    assert answer.content == "The config sets port 8443."
    # Two turns later the answer is the same bytes, so the prefix cache holds.
    later = [
        *stored,
        _message(3, ChatRole.USER, "Which file?"),
        _message(4, ChatRole.ASSISTANT, "config.yaml", {"tool_results": []}),
    ]
    history_later, _ = ChatService._merge_history(
        later, [ChatRequestMessage(role=ChatRole.USER, content="Thanks.")]
    )
    assert history_later[1].content == text
    assert history_later[3].content == "config.yaml"
    # Compaction and the context meter read the same text.
    assert _stored_model_text(answer) == text


def test_tool_activity_is_bounded_and_keeps_every_failure():
    results = [
        _result(index, "failed" if index in {3, 97, 150, 301} else "complete")
        for index in range(400)
    ]
    block = tool_activity_block(results)
    assert block == tool_activity_block(results)
    assert len(block.encode()) <= ACTIVITY_BLOCK_BYTES
    activity = json.loads(block.split("\n", 1)[1])
    kept = [
        int(step["tool_call_id"].removeprefix("tool-call-"))
        for step in activity["steps"]
    ]
    assert {3, 97, 150, 301} <= set(kept)
    # In the order they ran; the successes kept are the most recent ones.
    assert kept == sorted(kept)
    successes = [index for index in kept if index not in {3, 97, 150, 301}]
    assert successes == list(range(400 - len(successes), 400))
    assert activity["omitted"] == 400 - len(kept)


def test_a_saved_answer_records_what_each_tool_call_acted_on(tmp_path):
    broker = RecordingBroker()
    store, service, prepared, _ = _prepared(
        tmp_path,
        [
            _response(
                calls=[
                    ToolCall(id="call-1", name="safe_read", arguments={"value": "a"})
                ]
            ),
            _response(
                calls=[ToolCall(id="finish", name="finish_response", arguments={})]
            ),
            _response(text="Read a."),
        ],
        broker,
    )

    completion = asyncio.run(service.complete(prepared))

    answer = store.get(ChatMessage, completion.message.id)
    (result,) = answer.metadata["tool_results"]
    assert result["brief"] == '{"value":"a"}'
    text = _stored_model_text(answer)
    assert text.startswith("Read a.\n\n" + TOOL_ACTIVITY_HEADING)
    assert result["tool_call_id"] in text


def test_tool_output_reads_an_earlier_turn_of_the_same_conversation_only(tmp_path):
    """The ids an answer carries forward are readable from its later turns."""

    store = NebulaStore(tmp_path / "output.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store.create(Engagement(id="project", name="Output"))
    for session_id in ("session-a", "session-b"):
        store.create(
            ChatSession(
                id=session_id,
                engagement_id="project",
                title=session_id,
                provider_profile_id="provider",
                model="model-a",
            )
        )
    for turn_id, session_id in (
        ("turn-1", "session-a"),
        ("turn-2", "session-a"),
        ("turn-other", "session-b"),
    ):
        store.create(
            ChatTurn(
                id=turn_id,
                engagement_id="project",
                session_id=session_id,
                provider_profile_id="provider",
                model="model-a",
                status=ChatTurnStatus.COMPLETE,
            )
        )
    store.create(
        StoredToolCall(
            id="earlier-call",
            engagement_id="project",
            run_id="turn-1",
            origin=ToolCallOrigin.CHAT,
            chat_session_id="session-a",
            tool_name="run_command",
            risk_class=RiskClass.WORKSPACE_WRITE,
            status=ToolCallStatus.COMPLETE,
        )
    )
    stdout = store.create(
        artifacts.put_bytes(
            b"listen 8443\nserver_name example.test\n",
            engagement_id="project",
            filename="stdout.txt",
            media_type="text/plain",
            metadata={"tool_call_id": "earlier-call", "kind": "stdout"},
        )
    )
    service = ToolOutputService(store, artifacts)

    read = service.read(
        engagement_id="project", owner_id="turn-2", artifact_id=stdout.id
    )
    assert [line["text"] for line in read["lines"]] == [
        "listen 8443",
        "server_name example.test",
    ]
    search = service.search(
        engagement_id="project",
        owner_id="turn-2",
        tool_call_id="earlier-call",
        query="8443",
    )
    assert search["matches"]
    with pytest.raises(ToolOutputAccessError):
        service.read(
            engagement_id="project", owner_id="turn-other", artifact_id=stdout.id
        )
    with pytest.raises(ToolOutputAccessError):
        service.search(
            engagement_id="project",
            owner_id="turn-other",
            tool_call_id="earlier-call",
            query="8443",
        )


def _invocation(content, *, session_id="session", turn_id="turn-1"):
    return ToolInvocation(
        engagement_id="project",
        run_id=turn_id,
        origin=ToolCallOrigin.CHAT,
        chat_session_id=session_id,
        chat_turn_id=turn_id,
        tool_name=NOTES_WRITE_TOOL_NAME,
        arguments={"content": content},
        workspace=".",
    )


def test_notes_write_replaces_the_conversation_notes(tmp_path):
    store = NebulaStore(tmp_path / "notes.db")
    store.create(Engagement(id="project", name="Notes"))
    tool = WriteNotesTool(store)

    first = asyncio.run(tool.execute(_invocation("- [ ] scan"), None))
    second = asyncio.run(
        tool.execute(_invocation("- [x] scan\n- [ ] report", turn_id="turn-2"), None)
    )

    assert first.output["revision"] == 1
    assert second.output["revision"] == 2
    notes = read_working_notes(store, "session")
    assert notes is not None
    assert notes.content == "- [x] scan\n- [ ] report"
    assert notes.turn_id == "turn-2"
    # Another conversation keeps its own notes.
    asyncio.run(tool.execute(_invocation("other", session_id="session-2"), None))
    assert read_working_notes(store, "session").content == notes.content
    assert read_working_notes(store, "session-2").content == "other"
    # Bytes, not characters, bound the notes.
    with pytest.raises(InvalidToolArguments, match="at most 8192 bytes"):
        asyncio.run(tool.execute(_invocation("é" * 5_000), None))
    with pytest.raises(ToolBrokerError):
        asyncio.run(tool.execute(_invocation("x", session_id=None), None))
    assert read_working_notes(store, "session").revision == 2
    # Deleting the conversation deletes its notes.
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Notes",
            provider_profile_id="provider",
            model="model-a",
        )
    )
    store.delete_chat_session("session")
    assert read_working_notes(store, "session") is None
    assert read_working_notes(store, "session-2") is not None


def test_a_new_turn_starts_with_the_notes_and_the_context_api_shows_them(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "notes-turn.db")
    engagement = store.create(Engagement(id="project", name="Notes"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda _: provider)
    service = ChatService(store)
    session = store.create(
        ChatSession(
            id="session",
            engagement_id=engagement.id,
            title="Notes",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )

    def prepare(content: str):
        return service.prepare(
            ChatCompletionRequest(
                provider_id=profile.id,
                engagement_id=engagement.id,
                session_id=session.id,
                messages=[{"role": "user", "content": content}],
                include_knowledge=False,
            )
        )

    plain = prepare("What is next?")
    assert plain.model_request.messages[-1].content == "What is next?"
    assert service.context_status(session.id).working_notes is None

    notes = write_working_notes(
        store,
        engagement_id=engagement.id,
        session_id=session.id,
        content="## Findings\n- port 8443 (tool-call-1)\n## Todo\n- [ ] report",
        turn_id="turn-1",
    )
    noted = prepare("What is next?")
    last = noted.model_request.messages[-1]
    assert last.role == "user"
    operator, block = last.content.split("\n\n", 1)
    assert operator == "What is next?"
    heading, payload = block.split("\n", 1)
    assert heading == WORKING_NOTES_HEADING
    assert json.loads(payload) == {
        "content": notes.content,
        "revision": 1,
        "updated_at": notes.updated_at.isoformat(),
    }
    assert "WORKING NOTES" not in (noted.model_request.instructions or "")
    status = service.context_status(session.id)
    assert status.working_notes is not None
    assert status.working_notes.model_dump() == {
        "content": notes.content,
        "revision": 1,
        "updated_at": notes.updated_at.isoformat(),
        "turn_id": "turn-1",
    }


class NotingProvider(LongTurnProvider):
    """Writes working notes first, then keeps calling the scan tool."""

    async def complete(self, request: ModelRequest):
        if (
            not request.metadata.get("operation")
            and request.tool_choice != ToolChoice.NONE
            and not request.tool_results
        ):
            self.requests.append(request)
            return _response(
                calls=[
                    ToolCall(
                        id="notes-1",
                        name=NOTES_WRITE_TOOL_NAME,
                        arguments={"content": "- port 1000 open on scan 1"},
                    )
                ]
            )
        return await super().complete(request)


def test_notes_written_early_in_a_long_turn_survive_its_checkpoint(tmp_path):
    broker = ScanBroker()
    provider = NotingProvider(calls=30)
    store, service, prepared = _long_turn(tmp_path, provider, broker)
    components = prepared.tool_components
    prepared.tool_components = combine_tool_components(
        components, notes_components(store, components.scope, tmp_path)
    )

    completion = asyncio.run(service.complete(prepared))

    assert completion.message.content == ANSWER
    notes = store.get(ChatWorkingNotes, read_working_notes(store, "session").id)
    assert notes.content == "- port 1000 open on scan 1"
    routing = [
        request
        for request in _turn_requests(provider)
        if request.tool_choice == ToolChoice.AUTO
    ]
    assert all(
        NOTES_ROUTING_INSTRUCTIONS in request.instructions for request in routing
    )
    last = routing[-1]
    # The notes call itself folded into the checkpoint long ago ...
    assert "notes-1" not in [result.call_id for result in last.tool_results]
    # ... and the checkpoint carries the notes it wrote.
    block = str(last.messages[-1].content).split("EARLIER TOOL HISTORY CHECKPOINT", 1)[
        1
    ]
    checkpoint = json.loads(block.split("\n", 1)[1])
    assert checkpoint["working_notes"]["content"] == "- port 1000 open on scan 1"
    assert checkpoint["working_notes"]["revision"] == 1
